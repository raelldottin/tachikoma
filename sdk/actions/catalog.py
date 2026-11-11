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
BATTLE_EVIDENCE_TIERS = ("request-sent", "battle-created", "battle-accepted", "finalised", "outcome-observed")
_BATTLE_RESULT_ATTRIBUTES = {
    "battle-created": "createBattle9Result",
    "battle-accepted": "acceptBattle5Result",
    "finalised": "finaliseBattle15Result",
}
# The battle step a trophy change must be backed by, in this call, to count as its outcome.
OUTCOME_SUPPORTING_STEP = "finalised"


def _battle_probe(client: Any) -> Mapping[str, Any]:
    probe = {tier: getattr(client, attribute, None) for tier, attribute in _BATTLE_RESULT_ATTRIBUTES.items()}
    probe["battle_id"] = getattr(client, "lastBattleId", None)
    return probe


def _battle_evidence(before: Mapping[str, Any], after: Mapping[str, Any], status: ReconciliationStatus,
                     delta: Optional[SnapshotDelta] = None):
    # A step happened during this call only if its parsed response object was replaced.
    reached = {tier: after.get(tier) is not None and after.get(tier) is not before.get(tier)
               for tier in _BATTLE_RESULT_ATTRIBUTES}
    outcome_supported = bool(reached[OUTCOME_SUPPORTING_STEP])
    reached["outcome-observed"] = status is ReconciliationStatus.VERIFIED and outcome_supported
    tier = "request-sent"
    for name in BATTLE_EVIDENCE_TIERS[1:]:
        if reached.get(name):
            tier = name
    battle_id = after.get("battle_id") if reached["battle-created"] else None
    evidence = {
        "evidence_tier": tier,
        "steps": {name: bool(reached.get(name)) for name in BATTLE_EVIDENCE_TIERS[1:]},
        "battle_id": str(battle_id) if battle_id not in (None, "", "0") else None,
        "client_reported_outcome_trusted": False,
        "trophies_delta": delta.trophies if delta is not None else None,
        "outcome_supported": outcome_supported,
    }
    if not outcome_supported:
        evidence["outcome_support_reason"] = (
            f"this call did not reach {OUTCOME_SUPPORTING_STEP}, so a trophy change is not this battle's outcome"
        )
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
