"""Typed gameplay action catalog with postcondition reconciliation (R1 / AUTO-003).

Each ``ActionSpec`` names the ``sdk.client.Client`` method that performs an
action, how safe it is to retry, what must hold before it runs, and how its
effect shows up in an ``AccountSnapshot`` diff. ``reconcile`` decides from
observed state alone whether the effect happened. An HTTP success is never
evidence of an effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional

from sdk.state import AccountSnapshot, Completeness, SnapshotDelta, diff_snapshots
from sdk.state.profile_reads import ATTACKER, BattleRecord, matching_battle, parse_battles


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


class ActionSpecError(ValueError):
    """Raised for an unknown action or missing parameters."""


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


def _research_not_started(snapshot: AccountSnapshot, params: Params) -> Optional[str]:
    state = snapshot.research_state(str(params["research_design_id"]))
    if state in ("Researching", "Completed"):
        return f"research {params['research_design_id']} is already {state.lower()}"
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
    character_id = str(params["character_id"])
    requested = str(params["training_design_id"])
    if "crew" in delta.unknown_sections:
        return ReconciliationStatus.UNKNOWN, "crew was not observed on both sides"
    old, new = before.crew_member(character_id), after.crew_member(character_id)
    if new is None:
        return ReconciliationStatus.CONTRADICTED, f"crew member {character_id} disappeared"
    if old is None or not (old.training_observed and new.training_observed):
        return ReconciliationStatus.UNKNOWN, f"training state of {character_id} was not observed on both sides"
    if new.training_design_id == requested and new.training_active:
        return ReconciliationStatus.VERIFIED, f"{character_id} is training {requested} until {new.training_end}"
    if not old.training_active and new.training_active and new.training_design_id is None:
        return ReconciliationStatus.VERIFIED, f"{character_id} started training ending {new.training_end}"
    if new.training_active and new.training_design_id not in (None, requested) and (
        (old.training_design_id, old.training_end) != (new.training_design_id, new.training_end)
    ):
        return ReconciliationStatus.CONTRADICTED, f"{character_id} started different training {new.training_design_id}"
    return ReconciliationStatus.NOT_OBSERVED, f"training of {character_id} unchanged"


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
            preconditions=(_section_observed("crew"), _crew_member_present),
            postcondition=_training_started,
            expected_effect="the crew member starts the requested training",
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
