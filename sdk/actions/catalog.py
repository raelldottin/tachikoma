"""Typed gameplay action catalog with postcondition reconciliation (R1 / AUTO-003).

Each ``ActionSpec`` names the ``sdk.client.Client`` method that performs an
action, how safe it is to retry, what must hold before it runs, and how its
effect shows up in an ``AccountSnapshot`` diff. ``reconcile`` decides from
observed state alone whether the effect happened. An HTTP success is never
evidence of an effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Mapping, Optional

from sdk.state import AccountSnapshot, Completeness, CrewMember, SnapshotDelta, diff_snapshots
from sdk.state.profile_reads import ATTACKER, BattleRecord, int_or_none, matching_battle, parse_battles


class Idempotency(str, Enum):
    SAFE_TO_RETRY = "safe-to-retry"
    ONCE_PER_PERIOD = "once-per-period"
    NOT_IDEMPOTENT = "not-idempotent"


class ReconciliationStatus(str, Enum):
    VERIFIED = "verified"
    NOT_OBSERVED = "not-observed"
    CONTRADICTED = "contradicted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Reconciliation:
    action_id: str
    status: ReconciliationStatus
    reason: str
    response_ok: Optional[bool] = None


Params = Mapping[str, Any]
Precondition = Callable[[AccountSnapshot, Params], Optional[str]]
Postcondition = Callable[[SnapshotDelta, AccountSnapshot, AccountSnapshot, Params], tuple[ReconciliationStatus, str]]
# Reads client attributes only (never calls the game) before and after the call.
EvidenceProbe = Callable[[Any], Mapping[str, Any]]
# Turns the before/after probes, the reconciled status and the snapshot delta
# (None when the after-snapshot could not be observed) into journal evidence.
# Evidence may carry "outcome_supported": False (with "outcome_support_reason")
# when an observed effect is not backed by anything this call did; the runner
# then journals a verified outcome as unknown.
EvidenceBuilder = Callable[
    [Mapping[str, Any], Mapping[str, Any], ReconciliationStatus, Optional[SnapshotDelta]], Mapping[str, Any]
]


@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    client_method: str
    mutating: bool
    idempotency: Idempotency
    required_params: tuple[str, ...]
    preconditions: tuple[Precondition, ...]
    postcondition: Postcondition
    expected_effect: str
    evidence_probe: Optional[EvidenceProbe] = None
    evidence: Optional[EvidenceBuilder] = None
    # A canary (--fleet-execute --mode canary) may run this action: it is safe to retry, costs nothing and does
    # nothing beyond an effect its postcondition can reconcile. Set explicitly, so a new safe-to-retry action
    # never becomes a live canary without that decision.
    canary_safe: bool = False


class ActionSpecError(ValueError):
    """Raised for an unknown action or missing parameters."""


# finish_training runs only on a training that ended at least this long before the before-snapshot's observation.
# The observation time comes from the local clock, which may run ahead of the server's; the margin keeps a training
# that has not yet ended on the server from being finished early, which could cost starbux.
FINISH_TRAINING_GRACE = timedelta(seconds=60)


class _TrainingPhase(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    ENDED = "ended"


def _training_phase(member: CrewMember, at: datetime) -> Optional[_TrainingPhase]:
    """Where ``member``'s training stands at the moment ``at``, judged by its end time, not by whether an end is set.

    An ended training stays on the record until it is collected; an idle member may carry a placeholder end in the
    past with no training design.

    | Training observed | End                                       | Design reported | Phase    |
    |-------------------|-------------------------------------------|-----------------|----------|
    | no                | -                                         | -               | None     |
    | yes               | empty                                     | -               | idle     |
    | yes               | unreadable, or ``at`` not timezone-aware  | -               | None     |
    | yes               | after ``at``                              | either          | running  |
    | yes               | at or before ``at``                       | yes             | ended    |
    | yes               | at or before ``at``                       | no              | idle     |
    """
    if not member.training_observed:
        return None
    if not member.training_active:
        return _TrainingPhase.IDLE
    end = member.training_end_utc
    if end is None or at.utcoffset() is None:
        return None
    if end > at:
        return _TrainingPhase.RUNNING
    return _TrainingPhase.ENDED if member.training_design_id is not None else _TrainingPhase.IDLE


# --- preconditions -----------------------------------------------------------------


def _section_observed(name: str) -> Precondition:
    def check(snapshot: AccountSnapshot, params: Params) -> Optional[str]:
        if snapshot.section(name) is Completeness.MISSING:
            return f"{name} section was not observed before the action"
        return None

    return check


def _crew_member_present(snapshot: AccountSnapshot, params: Params) -> Optional[str]:
    if snapshot.crew_member(str(params["character_id"])) is None:
        return f"crew member {params['character_id']} is not in the snapshot"
    return None


def _room_idle(snapshot: AccountSnapshot, params: Params) -> Optional[str]:
    room = snapshot.room(str(params["room_id"]))
    if room is None:
        return f"room {params['room_id']} is not in the snapshot"
    if room.upgrading:
        return f"room {params['room_id']} is already upgrading"
    return None


def _is_int_at_least(value: Any, minimum: int) -> bool:
    """True for an ``int`` (not a ``bool``) of at least ``minimum``."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _placement_params_valid(snapshot: AccountSnapshot, params: Params) -> Optional[str]:
    """``place_room`` needs a numeric room design id and a non-negative integer row and column."""
    if not str(params.get("room_design_id") or "").strip().isdigit():
        return "room_design_id must be a numeric room design id"
    for name in ("row", "column"):
        if not _is_int_at_least(params.get(name), 0):
            return f"{name} must be a non-negative integer"
    return None


def _hull_design_id_valid(snapshot: AccountSnapshot, params: Params) -> Optional[str]:
    """``upgrade_ship`` needs a numeric hull (ship) design id."""
    if not str(params.get("ship_design_id") or "").strip().isdigit():
        return "ship_design_id must be a numeric hull design id"
    return None


def _numeric_id_param(name: str) -> Precondition:
    """The parameter ``name`` must be a numeric design id: a positive int (never a bool) or text matching ``[1-9][0-9]*``.

    The client gets the raw value, so whitespace-padded text or a non-ASCII digit is refused rather than stripped or
    converted. Zero-padded text (``"007"``) is refused too: the server reports ``"7"``, so the sent id would never match
    the reported one. Zero (``0``, ``"0"``, ``"00"``) is refused: the game uses it for "no design", never as a design id.
    """

    def check(snapshot: AccountSnapshot, params: Params) -> Optional[str]:
        value = params.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
            return f"{name} must be a numeric design id"
        return None

    return check


def _training_finishable(snapshot: AccountSnapshot, params: Params) -> Optional[str]:
    """``finish_training`` runs only on a training that ended a full ``FINISH_TRAINING_GRACE`` before the observation.

    It must never pay to speed a running training up. The observation time comes from the local clock, so the grace
    covers a local clock that runs a little ahead of the server's.

    | # | Member before the call                                          | Result                          |
    |---|-----------------------------------------------------------------|---------------------------------|
    | 1 | absent (or crew unobserved)                                     | None: an earlier check reports  |
    | 2 | training not observed                                           | refused                         |
    | 3 | no training end                                                 | refused: not training           |
    | 4 | training end not a readable server time                         | refused                         |
    | 5 | observation time not timezone-aware                             | refused (never compared)        |
    | 6 | no reported training design (an idle member's placeholder end)  | refused: nothing to finish      |
    | 7 | end later than the grace before the observation                 | refused: could cost starbux     |
    | 8 | end at least the grace before the observation                   | passes                          |
    """
    character_id = str(params["character_id"])
    member = snapshot.crew_member(character_id)
    if member is None:
        return None
    if not member.training_observed:
        return f"training state of {character_id} was not observed before the action"
    if not member.training_active:
        return f"{character_id} is not training"
    end = member.training_end_utc
    if end is None:
        return f"training end of {character_id} is not a readable server time"
    if snapshot.observed_at.utcoffset() is None:
        # Snapshots built by build_snapshot, baselines and trial records are always aware; a naive one is refused
        # rather than compared, which would raise.
        return "observation time of the before-snapshot is not timezone-aware"
    if member.training_design_id is None:
        return f"{character_id} has no reported training design, so there is no training to finish"
    if end > snapshot.observed_at - FINISH_TRAINING_GRACE:
        return (
            f"{character_id}'s training ends at {member.training_end}, less than"
            f" {int(FINISH_TRAINING_GRACE.total_seconds())} seconds before the observation;"
            " finishing now could cost starbux"
        )
    return None


def _training_idle(snapshot: AccountSnapshot, params: Params) -> Optional[str]:
    """``add_training`` starts a training only on an idle crew member, judged by time (``_training_phase``).

    | # | Member before the call                                           | Result                          |
    |---|------------------------------------------------------------------|---------------------------------|
    | 1 | absent (or crew unobserved)                                      | None: an earlier check reports  |
    | 2 | training not observed                                            | refused                         |
    | 3 | training end not readable (or observation time not tz-aware)     | refused                         |
    | 4 | a training running (with or without a reported design)           | refused                         |
    | 5 | an ended training with a reported design, not yet collected      | refused: finish it first        |
    | 6 | idle: no end, or a placeholder end with no design                | passes                          |
    """
    character_id = str(params["character_id"])
    member = snapshot.crew_member(character_id)
    if member is None:
        return None
    if not member.training_observed:
        return f"training state of {character_id} was not observed before the action"
    phase = _training_phase(member, snapshot.observed_at)
    if phase is None:
        return f"training end of {character_id} is not a readable server time"
    if phase is _TrainingPhase.RUNNING:
        design = member.training_design_id or "(design not reported)"
        return f"{character_id} is already training {design} until {member.training_end}"
    if phase is _TrainingPhase.ENDED:
        return (
            f"{character_id} has an ended training {member.training_design_id} (ended {member.training_end})"
            " that was not collected"
        )
    return None


def _inventory_quantities(snapshot: AccountSnapshot) -> dict[str, Optional[int]]:
    """Quantity by item design id; the last record of a design wins, as in ``diff_snapshots``."""
    return {item.item_design_id: item.quantity for item in snapshot.inventory}


def _inventory_records(snapshot: AccountSnapshot, design: str) -> int:
    """How many inventory records hold ``design``. Above one, the per-design quantity is only the last stack's."""
    return sum(1 for item in snapshot.inventory if item.item_design_id == design)


def _item_in_inventory(snapshot: AccountSnapshot, params: Params) -> Optional[str]:
    """``use_consumable`` needs at least one of the item; an unobserved inventory is reported by its own check.

    A design held in several records is refused: ``useConsumable`` uses the first record, while the snapshot diff
    sees only the last, so the use could read as not-observed and a not-idempotent use would repeat.
    """
    if snapshot.section("inventory") is Completeness.MISSING:
        return None
    design = str(params["item_design_id"]).strip()
    records = _inventory_records(snapshot, design)
    if records > 1:
        return f"item {design} is held in {records} inventory records, so its use cannot be reconciled"
    quantities = _inventory_quantities(snapshot)
    if design not in quantities:
        return f"item {design} is not in the inventory"
    quantity = quantities[design]
    if quantity is None:
        return f"quantity of item {design} was not observed"
    if quantity < 1:
        return f"item {design} has quantity {quantity}"
    return None


def _research_not_started(snapshot: AccountSnapshot, params: Params) -> Optional[str]:
    state = snapshot.research_state(str(params["research_design_id"]))
    if state in ("Researching", "Completed"):
        return f"research {params['research_design_id']} is already {state.lower()}"
    return None


FLEET_POOL_ACTIONS = ("contribute", "withdraw")
FLEET_POOL_RESOURCES = ("minerals", "gas", "starbux")


def _fleet_pool_params_valid(snapshot: AccountSnapshot, params: Params) -> Optional[str]:
    """``fleet_pool_resources`` needs a known direction, a known resource and a positive integer amount."""
    if params.get("action") not in FLEET_POOL_ACTIONS:
        return f"action must be one of {', '.join(FLEET_POOL_ACTIONS)}"
    if params.get("resource_type") not in FLEET_POOL_RESOURCES:
        return f"resource_type must be one of {', '.join(FLEET_POOL_RESOURCES)}"
    if not _is_int_at_least(params.get("amount"), 1):
        return "amount must be a positive integer"
    return None


# --- postconditions ----------------------------------------------------------------


def _resources_collected(delta, before, after, params):
    values = {"minerals": delta.minerals, "gas": delta.gas}
    known = {name: value for name, value in values.items() if value is not None}
    if not known:
        return ReconciliationStatus.UNKNOWN, "minerals and gas were not observed on both sides"
    if any(value > 0 for value in known.values()):
        return ReconciliationStatus.VERIFIED, "resources increased: " + ", ".join(
            f"{name} {value:+d}" for name, value in sorted(known.items())
        )
    if any(value < 0 for value in known.values()):
        return ReconciliationStatus.CONTRADICTED, "resources decreased after a collection"
    if len(known) < len(values):
        return ReconciliationStatus.UNKNOWN, "no observed increase and one resource was not observed"
    return ReconciliationStatus.NOT_OBSERVED, "minerals and gas unchanged"


def _reward_received(delta, before, after, params):
    base = {"minerals": delta.minerals, "gas": delta.gas, "starbux": delta.starbux}
    inventory_observed = "inventory" not in delta.unknown_sections
    gains = dict(base)
    if inventory_observed:
        gains.update({f"item {item}": change for item, change in delta.inventory_quantity.items()})
    increased = sorted(name for name, value in gains.items() if value is not None and value > 0)
    if increased:
        return ReconciliationStatus.VERIFIED, "reward observed in " + ", ".join(increased)
    if any(value is None for value in base.values()) or not inventory_observed:
        return ReconciliationStatus.UNKNOWN, "no increase seen, but not every reward channel was observed on both sides"
    return ReconciliationStatus.NOT_OBSERVED, "no resource, starbux or item increase observed"


def _training_started(delta, before, after, params):
    """A started training, decided from the crew member's training before and after the call.

    | # | Observed                                           | Status         |
    |---|----------------------------------------------------|----------------|
    | 1 | crew not observed on both sides                    | unknown        |
    | 2 | member gone after                                  | contradicted   |
    | 3 | member absent before, or training unobserved       | unknown        |
    | 4 | not training after                                 | not observed   |
    | 5 | same (design, end) as before: not this call's      | not observed   |
    | 6 | changed, but the end is not after the observation  | unknown        |
    |   | before the call (unreadable, or already over)      |                |
    | 7 | a new training of the requested design             | verified       |
    | 8 | a new training whose design is not reported        | unknown        |
    | 9 | a new training of another design                   | contradicted   |
    """
    character_id = str(params["character_id"])
    requested = str(params["training_design_id"]).strip()
    if "crew" in delta.unknown_sections:
        return ReconciliationStatus.UNKNOWN, "crew was not observed on both sides"
    old, new = before.crew_member(character_id), after.crew_member(character_id)
    if new is None:
        return ReconciliationStatus.CONTRADICTED, f"crew member {character_id} disappeared"
    if old is None or not (old.training_observed and new.training_observed):
        return ReconciliationStatus.UNKNOWN, f"training state of {character_id} was not observed on both sides"
    if not new.training_active:
        return ReconciliationStatus.NOT_OBSERVED, f"{character_id} is not training after the call"
    if (old.training_design_id, old.training_end) == (new.training_design_id, new.training_end):
        return (
            ReconciliationStatus.NOT_OBSERVED,
            f"training of {character_id} unchanged ({new.training_design_id} until {new.training_end})",
        )
    if _training_phase(new, before.observed_at) is not _TrainingPhase.RUNNING:
        return (
            ReconciliationStatus.UNKNOWN,
            f"{character_id} shows a training ending {new.training_end}, not after the observation before the call",
        )
    if new.training_design_id == requested:
        return ReconciliationStatus.VERIFIED, f"{character_id} is training {requested} until {new.training_end}"
    if new.training_design_id is None:
        return (
            ReconciliationStatus.UNKNOWN,
            f"{character_id} started a training ending {new.training_end}, but its design was not reported",
        )
    return ReconciliationStatus.CONTRADICTED, f"{character_id} started different training {new.training_design_id}"


def _training_finished(delta, before, after, params):
    """A finished training, decided by time: it had ended before the call, nothing runs after, and the stats rose.

    Whether the server then clears the end, rewrites it to the finish time, sets a placeholder or leaves the ended
    training in place does not matter (none of these is verified live). Phases (``_training_phase``) are judged at
    each snapshot's own observation time. The first matching row wins.

    | #  | Observed                                                          | Status        |
    |----|-------------------------------------------------------------------|---------------|
    | 1  | crew not observed on both sides                                   | unknown       |
    | 2  | member gone after                                                 | contradicted  |
    | 3  | member absent before                                              | unknown       |
    | 4  | stat improvements fell                                            | contradicted  |
    | 5  | starbux fell: a due finish never spends any                       | contradicted  |
    | 6  | training unobserved on either side                                | unknown       |
    | 7  | training after the call not judgeable (unreadable end)            | unknown       |
    | 8  | the same training still running after                             | not observed  |
    | 9  | any other training running after                                  | contradicted  |
    | 10 | training before the call not judgeable                            | unknown       |
    | 11 | not training before (idle, or a placeholder end)                  | not observed  |
    | 12 | the training was still running before the call                    | unknown       |
    | 13 | ended before; stat improvements not observed on both sides        | unknown       |
    | 14 | ended before; stat improvements rose                              | verified      |
    | 15 | ended before; the same training and the same stats after          | not observed  |
    | 16 | ended before; no gain, and the record before shows capacity reached | verified    |
    | 17 | ended before; no gain, capacity not shown                         | unknown       |
    """
    character_id = str(params["character_id"])
    if "crew" in delta.unknown_sections:
        return ReconciliationStatus.UNKNOWN, "crew was not observed on both sides"
    old, new = before.crew_member(character_id), after.crew_member(character_id)
    if new is None:
        return ReconciliationStatus.CONTRADICTED, f"crew member {character_id} disappeared"
    if old is None:
        return ReconciliationStatus.UNKNOWN, f"{character_id} was not observed before the call"
    gain = delta.crew_stat_total.get(character_id, 0)
    if gain < 0:
        return ReconciliationStatus.CONTRADICTED, f"stat improvements of {character_id} fell by {-gain}"
    if delta.starbux is not None and delta.starbux < 0:
        return ReconciliationStatus.CONTRADICTED, f"starbux fell by {-delta.starbux}; a due finish spends none"
    if not (old.training_observed and new.training_observed):
        return ReconciliationStatus.UNKNOWN, f"training state of {character_id} was not observed on both sides"
    old_phase, new_phase = _training_phase(old, before.observed_at), _training_phase(new, after.observed_at)
    same_training = (old.training_design_id, old.training_end) == (new.training_design_id, new.training_end)
    if new_phase is None:
        return (
            ReconciliationStatus.UNKNOWN,
            f"training end of {character_id} after the call ({new.training_end}) is not readable",
        )
    if new_phase is _TrainingPhase.RUNNING:
        if same_training:
            return (
                ReconciliationStatus.NOT_OBSERVED,
                f"{character_id} is still training {new.training_design_id} until {new.training_end}",
            )
        return (
            ReconciliationStatus.CONTRADICTED,
            f"{character_id} shows training {new.training_design_id} running until {new.training_end} after the finish",
        )
    if old_phase is None:
        return (
            ReconciliationStatus.UNKNOWN,
            f"training end of {character_id} before the call ({old.training_end}) is not readable",
        )
    if old_phase is _TrainingPhase.IDLE:
        return ReconciliationStatus.NOT_OBSERVED, f"{character_id} was not training before the call"
    if old_phase is _TrainingPhase.RUNNING:
        return (
            ReconciliationStatus.UNKNOWN,
            f"training of {character_id} was still running before the call (until {old.training_end})",
        )
    if old.stat_total is None or new.stat_total is None:
        return (
            ReconciliationStatus.UNKNOWN,
            f"training of {character_id} had ended, but stat improvements were not observed on both sides",
        )
    if gain > 0:
        return ReconciliationStatus.VERIFIED, f"training of {character_id} finished; stat improvements {gain:+d}"
    if same_training:
        return (
            ReconciliationStatus.NOT_OBSERVED,
            f"the ended training of {character_id} is still in place and stat improvements are unchanged",
        )
    if old.training_capacity is not None and old.stat_total >= old.training_capacity:
        return ReconciliationStatus.VERIFIED, f"training of {character_id} finished at capacity; no gain expected"
    return (
        ReconciliationStatus.UNKNOWN,
        f"training of {character_id} finished with no stat gain; capacity not observed (a training at capacity gains"
        " nothing)",
    )


def _consumable_used(delta, before, after, params):
    """A used consumable, decided from the item's inventory quantity alone: one use takes exactly one item.

    The crew member's stat improvement and fatigue change are named in the reason when observed, for the journal
    only: fatigue falls on its own and a finished training raises stats, so neither decides the status. A rise is
    unknown, not contradicted: an item arriving at the same time (a reward, a purchase) must not halt the account.

    | # | Observed                                                  | Status        |
    |---|-----------------------------------------------------------|---------------|
    | 1 | crew observed on both sides and the member gone after     | contradicted  |
    | 2 | inventory not observed on both sides                      | unknown       |
    | 3 | item held in several inventory records on either side     | unknown       |
    | 4 | item absent from a partial inventory after                | unknown       |
    | 5 | item quantity unreadable on one side                      | unknown       |
    | 6 | quantity rose                                             | unknown       |
    | 7 | quantity fell by exactly 1 (to fewer, to 0, or to absent) | verified      |
    | 8 | quantity fell by more than 1                              | unknown       |
    | 9 | quantity unchanged                                        | not observed  |
    """
    character_id = str(params["character_id"])
    design = str(params["item_design_id"]).strip()
    if "crew" not in delta.unknown_sections and before.crew_member(character_id) is not None:
        if after.crew_member(character_id) is None:
            return ReconciliationStatus.CONTRADICTED, f"crew member {character_id} disappeared"
    note = _member_change_note(delta, before, after, character_id)
    if "inventory" in delta.unknown_sections:
        return ReconciliationStatus.UNKNOWN, "inventory was not observed on both sides" + note
    if max(_inventory_records(before, design), _inventory_records(after, design)) > 1:
        return ReconciliationStatus.UNKNOWN, f"item {design} is held in several inventory records" + note
    if design not in _inventory_quantities(after) and after.section("inventory") is Completeness.PARTIAL:
        return ReconciliationStatus.UNKNOWN, f"item {design} is absent from a partial inventory" + note
    change = delta.inventory_quantity.get(design, 0)
    if change is None:
        return ReconciliationStatus.UNKNOWN, f"quantity of item {design} was not readable on both sides" + note
    if change > 0:
        return (
            ReconciliationStatus.UNKNOWN,
            f"item {design} quantity rose by {change} after use; an item may have arrived at the same time" + note,
        )
    if change == -1:
        return ReconciliationStatus.VERIFIED, f"item {design} quantity fell by 1" + note
    if change < -1:
        return ReconciliationStatus.UNKNOWN, f"item {design} quantity fell by {-change}, more than one use" + note
    return ReconciliationStatus.NOT_OBSERVED, f"item {design} quantity unchanged" + note


def _member_change_note(delta, before, after, character_id: str) -> str:
    """``"; c1 stat improvements +n, fatigue -m"`` for what was observed on both sides, or ``""``; informational."""
    if "crew" in delta.unknown_sections:
        return ""
    old, new = before.crew_member(character_id), after.crew_member(character_id)
    if old is None or new is None:
        return ""
    parts = []
    if old.stat_total is not None and new.stat_total is not None:
        parts.append(f"stat improvements {new.stat_total - old.stat_total:+d}")
    if old.fatigue is not None and new.fatigue is not None:
        parts.append(f"fatigue {new.fatigue - old.fatigue:+d}")
    return f"; {character_id} {', '.join(parts)}" if parts else ""


def _room_placed(delta, before, after, params):
    """A room placement, decided from the ship's rooms alone (S13-R5); never names a room instance id.

    Verified only when a room of the requested design is at the requested row
    and column after the call and was not there before, in any status (a new
    room is first ``Building``). A room present before that is gone after is
    contradicted. A room changing design between the reads is not, since an
    upgrade finishing changes it too.
    """
    if "rooms" in delta.unknown_sections:
        return ReconciliationStatus.UNKNOWN, "rooms were not observed on both sides"
    design, row, column = str(params["room_design_id"]), int(params["row"]), int(params["column"])
    before_ids = {room.room_id for room in before.rooms}
    gone = before_ids - {room.room_id for room in after.rooms}
    if gone:
        return ReconciliationStatus.CONTRADICTED, f"{len(gone)} room(s) present before the placement are gone"
    placed = [room for room in after.rooms if room.room_id not in before_ids and room.design_id == design
              and room.row == row and room.column == column]
    if placed:
        status = placed[0].status or "status not observed"
        return ReconciliationStatus.VERIFIED, f"a room of design {design} appeared at row {row}, column {column} ({status})"
    return ReconciliationStatus.NOT_OBSERVED, f"no new room of design {design} at row {row}, column {column}"


def _hull_upgrade_started(delta, before, after, params):
    """A hull upgrade, decided from the ship record alone.

    Hull upgrades look timed: the ship record carries ``@UpgradeShipDesignId``
    (the hull it is upgrading to) beside ``@ShipDesignId``. Verified when the
    ship after the call shows the requested design as its hull (an instant
    upgrade) or as its upgrade under way, and did not already show it before
    (an effect present before the call is not this call's). ``ship`` is not a
    delta section, so both snapshots' ship sections are checked here.
    """
    if before.section("ship") is Completeness.MISSING or after.section("ship") is Completeness.MISSING:
        return ReconciliationStatus.UNKNOWN, "the ship was not observed on both sides"
    target = str(params["ship_design_id"]).strip()
    if target in (before.ship_design_id, before.upgrade_ship_design_id):
        return ReconciliationStatus.NOT_OBSERVED, f"the ship already showed hull design {target} before the call"
    if after.ship_design_id == target:
        return ReconciliationStatus.VERIFIED, f"the ship's hull design is now {target}"
    if after.upgrade_ship_design_id == target:
        return ReconciliationStatus.VERIFIED, f"the ship started upgrading to hull design {target}"
    return ReconciliationStatus.NOT_OBSERVED, f"the ship shows neither hull design {target} nor an upgrade to it"


def _room_upgrade_started(delta, before, after, params):
    room_id = str(params["room_id"])
    if "rooms" in delta.unknown_sections:
        return ReconciliationStatus.UNKNOWN, "rooms were not observed on both sides"
    if after.room(room_id) is None:
        return ReconciliationStatus.CONTRADICTED, f"room {room_id} disappeared after the upgrade request"
    status_change = delta.room_status.get(room_id)
    design_change = delta.room_design.get(room_id)
    if status_change and status_change[1] == "Upgrading":
        return ReconciliationStatus.VERIFIED, f"room {room_id} status is now Upgrading"
    if design_change:
        return ReconciliationStatus.VERIFIED, f"room {room_id} design changed {design_change[0]} -> {design_change[1]}"
    return ReconciliationStatus.NOT_OBSERVED, f"room {room_id} unchanged"


def _research_started(delta, before, after, params):
    design_id = str(params["research_design_id"])
    if "research" in delta.unknown_sections:
        return ReconciliationStatus.UNKNOWN, "research was not observed on both sides"
    state = after.research_state(design_id)
    if state in ("Researching", "Completed"):
        return ReconciliationStatus.VERIFIED, f"research {design_id} is now {state}"
    if before.research_state(design_id) in ("Researching", "Completed") and state is None:
        return ReconciliationStatus.CONTRADICTED, f"research {design_id} disappeared"
    return ReconciliationStatus.NOT_OBSERVED, f"research {design_id} not started"


def _battle_outcome_observed(delta, before, after, params):
    if delta.trophies is None:
        return ReconciliationStatus.UNKNOWN, "trophies were not observed on both sides; battle outcome unknown"
    if delta.trophies:
        return ReconciliationStatus.VERIFIED, (
            f"trophies changed {delta.trophies:+d} (a defense in the same interval would also change trophies)"
        )
    return ReconciliationStatus.NOT_OBSERVED, "trophies unchanged; no battle outcome observed"


def _starbux_increased(delta, before, after, params):
    if delta.starbux is None:
        return ReconciliationStatus.UNKNOWN, "starbux was not observed on both sides"
    if delta.starbux > 0:
        return ReconciliationStatus.VERIFIED, f"starbux increased by {delta.starbux}"
    if delta.starbux < 0:
        return ReconciliationStatus.CONTRADICTED, f"starbux decreased by {-delta.starbux}"
    return ReconciliationStatus.NOT_OBSERVED, "starbux unchanged"


# Inter-fleet postconditions. A fleet coordination effect is not part of an account snapshot, so it can never be
# verified from one: the outcome stays unknown rather than being claimed from the call alone (see ``reconcile``).
def _fleet_effect_not_observable(delta, before, after, params):
    return ReconciliationStatus.UNKNOWN, "fleet coordination effects are not observable in an account snapshot"


# --- evidence ----------------------------------------------------------------------

# Battle evidence tiers, weakest first. A tier says how far the battle provably
# got. Only "outcome-observed" rests on game state; the others rest on the
# server accepting a request. FinaliseBattle15 carries an outcome type chosen by
# the client, so its response is never evidence of a win. A trophy change is
# attributed to this call's battle only if the call reached FinaliseBattle15:
# without it no attack result was reported, so the change came from elsewhere
# (for example a defense) and "outcome-observed" is not reached.
#
# Battle records (S03-R5). When the client's ``ListPvPBattles2`` response
# (``client.pvpBattles``) was replaced after the call began, its records are
# observed after the call. This call's battle id is one ``lastBattleId`` took
# during the call; an id left over from an earlier battle matches nothing. A
# record with this call's battle id decides what supports the outcome: an
# attack record supports it, with or without FinaliseBattle15, only when its
# outcome is decided and its trophy change has the sign of the observed one; a
# defense record never does, finalised or not. Without a matching record,
# support falls back to FinaliseBattle15 as above. The reconciled status still
# comes from the trophy change, so a record alone, with trophies unobserved,
# stays unknown. Evidence is built after a non-idempotent battle, so building it
# never raises: a failure journals the outcome as unsupported.
BATTLE_EVIDENCE_TIERS = ("request-sent", "battle-created", "battle-accepted", "finalised", "outcome-observed")
_BATTLE_RESULT_ATTRIBUTES = {
    "battle-created": "createBattle9Result",
    "battle-accepted": "acceptBattle5Result",
    "finalised": "finaliseBattle15Result",
}
# The battle step a trophy change must be backed by, in this call, to count as its outcome.
OUTCOME_SUPPORTING_STEP = "finalised"


_PROBE_FAILED = "probe_failed"
_EVIDENCE_FAILURE_REASON = "battle evidence could not be read, so nothing supports this battle's outcome"


def _battle_probe(client: Any) -> Mapping[str, Any]:
    try:
        probe = {tier: getattr(client, attribute, None) for tier, attribute in _BATTLE_RESULT_ATTRIBUTES.items()}
        probe["battle_id"] = getattr(client, "lastBattleId", None)
        probe["battles"] = getattr(client, "pvpBattles", None)
        probe["own_user_id"] = getattr(getattr(client, "user", None), "id", None)  # never journaled
    except Exception:  # the probe runs after the battle too and must not raise there
        return {_PROBE_FAILED: True}
    return probe


def _journaled_record(record: BattleRecord) -> dict:
    """The facts of a matching battle record that the journal keeps (no user ids)."""
    return {
        "role": record.role,
        "outcome": record.outcome,
        "server_outcome": record.server_outcome,
        "won": record.won,
        "trophy_change": record.trophy_change,
        "opponent_ship_design_id": record.opponent_ship_design_id,
        "opponent_trophies": record.opponent_trophies,
        "opponent_hull_level": record.opponent_hull_level,
        "battle_date": record.battle_date.isoformat() if record.battle_date else None,
    }


def _observed_battle_record(before: Mapping[str, Any], after: Mapping[str, Any],
                            battle_id: Optional[str]) -> Optional[BattleRecord]:
    """This call's battle record, from a battle list read after the call began; else ``None``.

    A record whose role is unknown (the own user id was not known) is not used.
    """
    battles = after.get("battles")
    if battles is None or battles is before.get("battles"):
        return None
    record = matching_battle(parse_battles(battles, after.get("own_user_id")), battle_id)
    return record if record is not None and record.role is not None else None


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _record_support(record: BattleRecord, delta: Optional[SnapshotDelta]) -> tuple[bool, str]:
    """Whether this call's battle record supports the observed outcome, and why not."""
    if record.role != ATTACKER:
        return False, ("the battle record for this battle id shows the account defending; "
                       "a defense never verifies an attack")
    if record.won is None:
        return False, "the battle record for this battle id does not say who won"
    observed = delta.trophies if delta is not None else None
    if observed is not None and record.trophy_change is not None and _sign(observed) != _sign(record.trophy_change):
        return False, "the battle record's trophy change disagrees in sign with the observed trophy change"
    # Without a trophy number in the record, the winner still fixes the sign: a win never loses trophies
    # and a loss never gains them.
    if observed is not None and record.trophy_change is None and observed != 0 \
            and _sign(observed) != (1 if record.won else -1):
        return False, "the battle record's winner disagrees with the observed trophy change"
    return True, ""


def _failed_battle_evidence() -> dict:
    """Evidence when it could not be built: nothing reached, nothing supported, no values."""
    return {
        "evidence_tier": BATTLE_EVIDENCE_TIERS[0],
        "steps": {name: False for name in BATTLE_EVIDENCE_TIERS[1:]},
        "battle_id": None,
        "client_reported_outcome_trusted": False,
        "trophies_delta": None,
        "outcome_supported": False,
        "outcome_support_reason": _EVIDENCE_FAILURE_REASON,
    }


def _battle_evidence(before: Mapping[str, Any], after: Mapping[str, Any], status: ReconciliationStatus,
                     delta: Optional[SnapshotDelta] = None):
    try:
        if before.get(_PROBE_FAILED) or after.get(_PROBE_FAILED):
            return _failed_battle_evidence()
        return _build_battle_evidence(before, after, status, delta)
    except Exception:  # built after a non-idempotent battle: never raise there
        return _failed_battle_evidence()


def _build_battle_evidence(before: Mapping[str, Any], after: Mapping[str, Any], status: ReconciliationStatus,
                           delta: Optional[SnapshotDelta]):
    # A step happened during this call only if its parsed response object was replaced.
    reached = {tier: after.get(tier) is not None and after.get(tier) is not before.get(tier)
               for tier in _BATTLE_RESULT_ATTRIBUTES}
    # The battle id is this call's only if lastBattleId changed during it: a
    # created battle whose id could not be read leaves the previous battle's id.
    battle_id = after.get("battle_id")
    if not reached["battle-created"] or battle_id == before.get("battle_id"):
        battle_id = None
    battle_id = str(battle_id) if battle_id not in (None, "", "0") else None
    record = _observed_battle_record(before, after, battle_id)
    if record is not None:
        outcome_supported, support_reason = _record_support(record, delta)
    else:
        outcome_supported = bool(reached[OUTCOME_SUPPORTING_STEP])
        support_reason = (
            f"this call did not reach {OUTCOME_SUPPORTING_STEP}, so a trophy change is not this battle's outcome"
        )
    reached["outcome-observed"] = status is ReconciliationStatus.VERIFIED and outcome_supported
    tier = "request-sent"
    for name in BATTLE_EVIDENCE_TIERS[1:]:
        if reached.get(name):
            tier = name
    evidence = {
        "evidence_tier": tier,
        "steps": {name: bool(reached.get(name)) for name in BATTLE_EVIDENCE_TIERS[1:]},
        "battle_id": battle_id,
        "client_reported_outcome_trusted": False,
        "trophies_delta": delta.trophies if delta is not None else None,
        "outcome_supported": outcome_supported,
    }
    if record is not None:
        evidence["battle_record"] = _journaled_record(record)
    if not outcome_supported:
        evidence["outcome_support_reason"] = support_reason
    return evidence


# --- collection evidence -----------------------------------------------------------
#
# How full each room was just before and just after a collection. The probe reads
# the ship's room records (``client.rooms``) that the runner's before and after
# snapshots already fetched, and the room designs (``client.roomDesigns``); it never
# calls the game. Rooms are grouped by design, with the design's name, room type,
# manufacture type and capacity, so room instance ids never reach the journal. The
# evidence tells "nothing was waiting in any room" apart from "something was waiting
# and the collection was not seen". It never changes the reconciled outcome:
# what ``@CapacityUsed`` counts for each room type is not yet confirmed live, and
# the totals add up every room, storage included.
#
# ``Client.getShipByUserId`` replaces ``client.rooms`` only when the read
# succeeds, so after a failed read the list is left over from an earlier one.
# Room fill is therefore journaled only when both snapshots observed the rooms
# section and the after-call list is a different object from the one read
# before. Otherwise both sides are unobserved (``None``), never a stale value. A
# client with no room records on either side (a fake, say) journals no evidence.
ROOM_FILL_EMPTY_NOTE = "no room reported anything stored (CapacityUsed 0) before the collection"
ROOM_FILL_UNREAD_REASON = "the ship's rooms were not read fresh both before and after the collection"


def _room_records(value: Any) -> Optional[list]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [record for record in value if isinstance(record, Mapping)]
    return None


def _room_designs(client: Any) -> dict[str, Mapping[str, Any]]:
    """``client.roomDesigns`` as ``{design id: RoomDesign record}``; empty when they are not loaded."""
    designs = getattr(client, "roomDesigns", None)
    if not isinstance(designs, Mapping):
        return {}
    from sdk.client import _extract_collection  # imported here: the catalog itself makes no game calls

    return {str(record["@RoomDesignId"]).strip(): record for record in _extract_collection(designs, "RoomDesign")
            if str(record.get("@RoomDesignId") or "").strip()}


def _collection_probe(client: Any) -> Mapping[str, Any]:
    try:
        source = getattr(client, "rooms", None)
        rooms = _room_records(source)
        if rooms is None:
            return {_PROBE_FAILED: True}
        fills = {}
        for index, room in enumerate(rooms):
            # Room ids are kept in the probe only to pair rooms; they are never journaled.
            room_id = str(room.get("@RoomId") or "").strip() or f"#{index}"
            fills[room_id] = (str(room.get("@RoomDesignId") or "").strip() or None,
                              int_or_none(room.get("@CapacityUsed")))
    except Exception:  # the probe runs before and after the call and must not raise
        return {_PROBE_FAILED: True}
    try:
        designs = _room_designs(client)
    except Exception:  # designs only name the rooms; their fill stands without them
        designs = {}
    return {"fills": fills, "designs": designs, "source": source}


def _fill_total(fills: Mapping[str, tuple]) -> Optional[int]:
    """The summed fill of all rooms; ``None`` when no room was read or any room's fill is unknown."""
    values = [fill for _, fill in fills.values()]
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def _collection_evidence(before: Mapping[str, Any], after: Mapping[str, Any], status: ReconciliationStatus,
                         delta: Optional[SnapshotDelta]) -> Mapping[str, Any]:
    try:
        return _build_collection_evidence(before, after, status, delta)
    except Exception:  # evidence never breaks the journal entry
        return {"room_fill_observed": False}


def _build_collection_evidence(before: Mapping[str, Any], after: Mapping[str, Any],
                               status: ReconciliationStatus, delta: Optional[SnapshotDelta]) -> dict:
    if before.get(_PROBE_FAILED) and after.get(_PROBE_FAILED):
        return {}  # the client holds no room records on either side: nothing to report
    # A ship read that failed on either side leaves client.rooms from an earlier read: unobserved, not stale.
    stale = (delta is None or "rooms" in delta.unknown_sections
             or (not before.get(_PROBE_FAILED) and before.get("source") is after.get("source")))
    if stale:
        return {"room_fill_observed": False, "room_fill_before_total": None, "room_fill_after_total": None,
                "room_fill": [], "room_fill_unobserved_reason": ROOM_FILL_UNREAD_REASON}
    before_fills = {} if before.get(_PROBE_FAILED) else dict(before.get("fills") or {})
    after_fills = {} if after.get(_PROBE_FAILED) else dict(after.get("fills") or {})
    designs = {**(after.get("designs") or {}), **(before.get("designs") or {})}
    groups: dict[str, dict] = {}
    for room_id in sorted(set(before_fills) | set(after_fills)):
        design_id, fill_before = before_fills.get(room_id, (None, None))
        after_design, fill_after = after_fills.get(room_id, (None, None))
        design_id = design_id or after_design
        record = designs.get(design_id or "", {})
        group = groups.setdefault(design_id or "unknown", {
            "design_id": design_id,
            "name": str(record.get("@RoomName") or "") or None,
            "room_type": str(record.get("@RoomType") or "") or None,
            "manufacture_type": str(record.get("@ManufactureType") or "") or None,
            "design_capacity": int_or_none(record.get("@Capacity")),
            "rooms": 0,
            "fill_before": 0,
            "fill_after": 0,
        })
        group["rooms"] += 1
        for key, fill, seen in (("fill_before", fill_before, room_id in before_fills),
                                ("fill_after", fill_after, room_id in after_fills)):
            group[key] = None if group[key] is None or fill is None or not seen else group[key] + fill
    total_before = _fill_total(before_fills)
    evidence = {
        "room_fill_observed": bool(before_fills),
        "room_fill_before_total": total_before,
        "room_fill_after_total": _fill_total(after_fills),
        "room_fill": [groups[key] for key in sorted(groups)],
    }
    if status is ReconciliationStatus.NOT_OBSERVED and total_before == 0:
        evidence["room_fill_note"] = ROOM_FILL_EMPTY_NOTE
    return evidence


# --- placement evidence ------------------------------------------------------------
#
# What the server said to a room placement (SPEC-13): ``Client.placeRoom`` keeps a
# summary of the BuyRoom2 reply (``client.placeRoomReply``: HTTP status, element
# names, error or message attribute text). The journal gets it only after each text
# is redacted and leak-scanned; a text the scan flags is withheld, never journaled.
# It explains an outcome; it never changes it.
PLACE_REPLY_WITHHELD = "withheld: the leak scan flagged this text"


def _reply_probe(attribute: str) -> EvidenceProbe:
    """A probe that reads the reply summary a client keeps in ``attribute`` (``None`` when it keeps none)."""

    def probe(client: Any) -> Mapping[str, Any]:
        try:
            return {"reply": getattr(client, attribute, None)}
        except Exception:  # the probe runs before and after the call and must not raise
            return {_PROBE_FAILED: True}

    return probe


def _clean_reply_text(text: Any) -> str:
    from sdk.redaction import redact_secrets
    from sdk.state.capture import scan_capture_text

    value = str(text)
    if redact_secrets(value) != value or scan_capture_text(value):
        return PLACE_REPLY_WITHHELD
    return value


def _placement_evidence(before: Mapping[str, Any], after: Mapping[str, Any], status: ReconciliationStatus,
                        delta: Optional[SnapshotDelta]) -> Mapping[str, Any]:
    try:
        reply = after.get("reply")
        if not isinstance(reply, Mapping) or reply is before.get("reply"):
            return {"server_reply": None}  # this call left no reply (the request raised, say)
        messages = [{"attribute": _clean_reply_text(entry.get("attribute")), "text": _clean_reply_text(entry.get("text"))}
                    for entry in reply.get("messages") or () if isinstance(entry, Mapping)]
        elements = [_clean_reply_text(name) for name in reply.get("elements") or ()]
        status_code = reply.get("http_status")
        return {"server_reply": {
            "http_status": status_code if isinstance(status_code, int) and not isinstance(status_code, bool) else None,
            "success_shape": reply.get("success_shape") is True,
            "parsed": reply.get("parsed") is True,
            "length": reply.get("length") if isinstance(reply.get("length"), int) else None,
            "elements": elements,
            "messages": messages,
        }}
    except Exception:  # evidence never breaks the journal entry
        return {"server_reply": None}


# Hull upgrade evidence: ``Client.upgradeShip`` returns the raw UpgradeShip response. A
# client that keeps a summary of it as ``client.upgradeShipReply`` (the shape of
# ``placeRoomReply``) gets it journaled like a placement's reply, after redaction and the
# leak scan; a client without one journals ``server_reply: None``. It never changes the outcome.


CATALOG: dict[str, ActionSpec] = {
    spec.action_id: spec
    for spec in (
        ActionSpec(
            action_id="collect_all_resources",
            client_method="collectAllResources",
            mutating=True,
            idempotency=Idempotency.SAFE_TO_RETRY,
            required_params=(),
            preconditions=(_section_observed("resources"),),
            postcondition=_resources_collected,
            expected_effect="minerals and/or gas increase",
            evidence_probe=_collection_probe,
            evidence=_collection_evidence,
            canary_safe=True,
        ),
        ActionSpec(
            action_id="collect_daily_reward",
            client_method="collectDailyReward",
            mutating=True,
            idempotency=Idempotency.ONCE_PER_PERIOD,
            required_params=(),
            preconditions=(_section_observed("resources"),),
            postcondition=_reward_received,
            expected_effect="a resource, starbux or inventory item increases",
        ),
        ActionSpec(
            action_id="add_training",
            client_method="addTraining",
            mutating=True,
            idempotency=Idempotency.NOT_IDEMPOTENT,
            required_params=("training_design_id", "character_id"),
            preconditions=(
                _section_observed("crew"),
                _crew_member_present,
                _numeric_id_param("training_design_id"),
                _training_idle,
            ),
            postcondition=_training_started,
            expected_effect="the crew member starts the requested training",
        ),
        # Safe to retry: the retry rebuilds its before-snapshot, so after a finish that cleared the training the member
        # is idle and the precondition skips it, and a training started since ends in the future, which the
        # precondition refuses. It runs only on a training that ended FINISH_TRAINING_GRACE before the local clock's
        # observation, so it never pays to speed one up, and a starbux fall reconciles as contradicted. It changes
        # crew stats for good, so it is never a canary.
        ActionSpec(
            action_id="finish_training",
            client_method="finishTraining",
            mutating=True,
            idempotency=Idempotency.SAFE_TO_RETRY,
            required_params=("character_id",),
            preconditions=(_section_observed("crew"), _crew_member_present, _training_finishable),
            postcondition=_training_finished,
            expected_effect="a due training is cleared and the crew member's stat improvements rise",
        ),
        # useConsumable takes the item's design id: snapshots key the inventory by design and never keep item
        # instance ids (account-specific, so they stay out of journaled params). It finds the instance in the
        # client's cached items, which the runner's before-read refreshes. Each call uses up an item, so a repeat
        # would use another: not idempotent.
        ActionSpec(
            action_id="use_consumable",
            client_method="useConsumable",
            mutating=True,
            idempotency=Idempotency.NOT_IDEMPOTENT,
            required_params=("item_design_id", "character_id"),
            preconditions=(
                _section_observed("inventory"),
                _section_observed("crew"),
                _crew_member_present,
                _numeric_id_param("item_design_id"),
                _item_in_inventory,
            ),
            postcondition=_consumable_used,
            expected_effect="the consumable's inventory quantity falls by the use",
        ),
        ActionSpec(
            action_id="upgrade_room",
            client_method="upgradeRoom",
            mutating=True,
            idempotency=Idempotency.NOT_IDEMPOTENT,
            required_params=("room_id", "upgrade_room_design_id"),
            preconditions=(_section_observed("rooms"), _room_idle),
            postcondition=_room_upgrade_started,
            expected_effect="the room status becomes Upgrading or its design advances",
        ),
        # SPEC-13. Placement mode checks the layout (sdk.policy.placement) before the runner is called; a
        # catalog precondition sees only the snapshot and the params.
        ActionSpec(
            action_id="place_room",
            client_method="placeRoom",
            mutating=True,
            idempotency=Idempotency.NOT_IDEMPOTENT,
            required_params=("room_design_id", "row", "column"),
            preconditions=(_section_observed("rooms"), _placement_params_valid),
            postcondition=_room_placed,
            expected_effect="a room of the requested design appears at the requested row and column",
            evidence_probe=_reply_probe("placeRoomReply"),
            evidence=_placement_evidence,
        ),
        # Only --fleet-execute --mode upgrade-ship plans this action (HullUpgradePolicy in sdk.fleet.factory): it
        # resolves the race-matched next hull and its mineral cost before the runner is called.
        ActionSpec(
            action_id="upgrade_ship",
            client_method="upgradeShip",
            mutating=True,
            idempotency=Idempotency.NOT_IDEMPOTENT,
            required_params=("ship_design_id",),
            preconditions=(_section_observed("ship"), _hull_design_id_valid),
            postcondition=_hull_upgrade_started,
            expected_effect="the ship's hull design, or the hull upgrade under way, becomes the requested design",
            evidence_probe=_reply_probe("upgradeShipReply"),
            evidence=_placement_evidence,
        ),
        ActionSpec(
            action_id="add_research",
            client_method="addResearch",
            mutating=True,
            idempotency=Idempotency.NOT_IDEMPOTENT,
            required_params=("research_design_id",),
            preconditions=(_section_observed("research"), _research_not_started),
            postcondition=_research_started,
            expected_effect="the research state becomes Researching or Completed",
        ),
        ActionSpec(
            action_id="run_pvp_battle",
            client_method="runBattleEndToEnd",
            mutating=True,
            idempotency=Idempotency.NOT_IDEMPOTENT,
            required_params=(),
            preconditions=(_section_observed("ship"),),
            postcondition=_battle_outcome_observed,
            expected_effect="a battle outcome is observed as a trophy change",
            evidence_probe=_battle_probe,
            evidence=_battle_evidence,
        ),
        # --- Collections ported from the run.py daily loop ---
        ActionSpec(
            action_id="grab_flying_starbux",
            client_method="grabFlyingStarbux",
            mutating=True,
            # Each call grabs 1-5 of the day's free starbux (the server caps the day), so run.py calls it on
            # every loop; once per period would stop after the first small grab.
            idempotency=Idempotency.SAFE_TO_RETRY,
            required_params=(),
            # Starbux is not required: it comes only from the login profile, which only the plan's snapshot
            # carries, so the runner's before-snapshot never has it. Unobserved, the grab reconciles as unknown.
            preconditions=(_section_observed("resources"),),
            postcondition=_starbux_increased,
            expected_effect="free starbux increase",
        ),
        ActionSpec(
            action_id="collect_task_reward",
            client_method="collectTaskReward",
            mutating=True,
            idempotency=Idempotency.ONCE_PER_PERIOD,
            required_params=(),
            preconditions=(_section_observed("resources"),),
            postcondition=_reward_received,
            expected_effect="completed task rewards collected (resources, starbux, or items)",
        ),
        ActionSpec(
            action_id="collect_available_markers",
            client_method="collectAvailableMarkers",
            mutating=True,
            idempotency=Idempotency.SAFE_TO_RETRY,
            required_params=(),
            preconditions=(_section_observed("resources"),),
            postcondition=_resources_collected,
            expected_effect="user markers collected, minerals and/or gas increase",
        ),
        ActionSpec(
            action_id="collect_mining_drones",
            client_method="collectMiningDronesWithTravel",
            mutating=True,
            idempotency=Idempotency.SAFE_TO_RETRY,
            required_params=(),
            preconditions=(_section_observed("resources"), _section_observed("ship")),
            postcondition=_resources_collected,
            expected_effect="mining drones collected after travel, minerals and/or gas increase",
        ),
        # --- Inter-fleet actions ---
        ActionSpec(
            action_id="fleet_pool_resources",
            client_method="fleetPoolResources",
            mutating=True,
            # A repeated contribution or withdrawal moves the amount twice.
            idempotency=Idempotency.NOT_IDEMPOTENT,
            required_params=("action", "resource_type", "amount"),
            preconditions=(_fleet_pool_params_valid,),
            postcondition=_fleet_effect_not_observable,
            expected_effect="fleet reserve contribution or withdrawal recorded",
        ),
        ActionSpec(
            action_id="fleet_marketplace_sync",
            client_method="fleetMarketplaceSync",
            mutating=True,
            # A sync buys and sells: repeating an uncertain one could trade twice.
            idempotency=Idempotency.NOT_IDEMPOTENT,
            required_params=(),
            preconditions=(),
            postcondition=_fleet_effect_not_observable,
            expected_effect="fleet-wide marketplace buy/sell coordinated",
        ),
        ActionSpec(
            action_id="fleet_battle_coordinate",
            client_method="fleetBattleCoordinate",
            mutating=True,
            idempotency=Idempotency.NOT_IDEMPOTENT,
            required_params=("target_fleet", "battle_type"),
            preconditions=(),
            postcondition=_battle_outcome_observed,
            expected_effect="coordinated fleet battle scheduled and executed",
        ),
        ActionSpec(
            action_id="fleet_share_crew_xp",
            client_method="fleetShareCrewXp",
            mutating=True,
            idempotency=Idempotency.ONCE_PER_PERIOD,
            required_params=("source_account", "target_account", "character_id"),
            preconditions=(),
            postcondition=_fleet_effect_not_observable,
            expected_effect="crew training XP shared across fleet accounts",
        ),
    )
}


def get_spec(action_id: str) -> ActionSpec:
    try:
        return CATALOG[action_id]
    except KeyError:
        raise ActionSpecError(f"unknown action {action_id!r}") from None


def _require_params(spec: ActionSpec, params: Params) -> None:
    missing = [name for name in spec.required_params if params.get(name) in (None, "")]
    if missing:
        raise ActionSpecError(f"{spec.action_id} requires parameters: {', '.join(missing)}")


def check_preconditions(spec: ActionSpec, before: AccountSnapshot, params: Optional[Params] = None) -> list[str]:
    """Return the reasons the action must not run; an empty list means it may."""
    params = params or {}
    _require_params(spec, params)
    return [failure for check in spec.preconditions if (failure := check(before, params))]


def reconcile(
    spec: ActionSpec,
    before: AccountSnapshot,
    after: AccountSnapshot,
    params: Optional[Params] = None,
    response_ok: Optional[bool] = None,
) -> Reconciliation:
    """Decide from observed state whether the action's effect happened.

    ``response_ok`` is recorded for context only. It never turns an
    unobserved effect into ``VERIFIED``. Snapshots of different accounts raise
    ``AccountMismatchError`` from ``diff_snapshots``.
    """
    params = params or {}
    _require_params(spec, params)
    delta = diff_snapshots(before, after)
    status, reason = spec.postcondition(delta, before, after, params)
    return Reconciliation(action_id=spec.action_id, status=status, reason=reason, response_ok=response_ok)
