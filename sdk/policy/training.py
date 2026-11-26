"""training-v1: fleet-v1's plan, then crew training planned by ``sdk.training`` from the same snapshot.

Pure, like every policy: no I/O, and the clock is ``snapshot.observed_at`` (never the wall clock).

- Conversion. The snapshot and ``DesignData`` are turned back into the raw envelopes ``sdk.training.build_context``
  parses (``training_payloads``), so the parser stays the single authority on what each attribute means. A crew
  member whose training inputs were not all observed (training state, any of the nine stats, fatigue) or whose id is
  not a positive whole number is left out with a ``training <id>`` rejection: the parser would read each gap as a
  value and plan from it. No crew section, or no TrainingDesign or CharacterDesign records, plans no training.
- Order. fleet-v1's requests come first (run.py manages training last), then every ``finish_training`` (safe to
  retry: stat gains are banked before a start that is not idempotent can stop the scheduler), then the starts and
  consumable uses, each block in crew order. ``max_requests`` cuts the whole plan, so training is cut first.
- Deferral. After a FinishTraining the planner adds an advisory primary planned from the pre-finish stats, which
  must not run as-is. A once-per-run pure plan cannot see the post-finish state, so every later decision for a crew
  finished in this plan (wherever it comes in the planner's order) is dropped and recorded as a rejection
  (``DEFERRED``, a Skip included); the next run plans it from fresh stats.
- Checks. Every training request goes through the catalog's preconditions at plan time (the 60-second finish grace,
  an idle crew member for a start, one inventory record holding the item); a failure is a rejection. Every other
  Skip is a ``training <id>`` rejection with its reason code and detail.
- Cost. ``add_training`` and ``use_consumable`` cost no minerals or gas, so they carry an explicit zero
  (``TRAINING_ZERO_COST``), which the execute gate requires before it runs them under caps. ``finish_training``
  plans no cost: it only ever runs after the grace, so it never buys a speed-up. No request names starbux.
- Consumables. The item designs' consumables are attached to a strategy that maps none of its own.
"""

from __future__ import annotations

import re
from dataclasses import replace
from types import MappingProxyType
from typing import Mapping, Optional

from sdk.policy.progression import (
    ActionRequest,
    DesignData,
    FleetV1,
    Plan,
    PolicyConfig,
    Rejection,
    ResearchDesign,
    _cap_requests,
    _unmet_preconditions,
)
from sdk.state import AccountSnapshot, Completeness
from sdk.state.snapshot import TRAINING_STAT_ATTRIBUTES, CrewMember
from sdk.training import (
    DEFAULT_STRATEGY,
    Skip,
    TrainingContext,
    TrainingStrategy,
    build_context,
    parse_consumable_designs,
    plan_training,
    with_consumables,
)
from sdk.training.actions import action_for

# Actions whose only cost is an inventory item (use_consumable) or a crew member's training slot (add_training): no
# mineral or gas price exists. Defined once, here: the execute gate (sdk.fleet.factory) imports it and runs these
# actions under caps only when the request declares an explicit zero for every capped kind (declares_zero_cost).
# TRAINING_ZERO_COST is that honest cost, one zero per factory.CAP_KINDS kind. tests.test_training_policy pins the
# set and checks that every request carrying the zero is one the gate accepts.
ZERO_COST_ACTIONS: frozenset[str] = frozenset({"add_training", "use_consumable"})
TRAINING_ZERO_COST: Mapping[str, int] = MappingProxyType({"minerals": 0, "gas": 0})
# Why the advisory primary after a FinishTraining is not planned (see the module docstring).
DEFERRED = "deferred to the next run: planned from the stats before finish_training, so it may name the wrong tier"
_POSITIVE_ID = re.compile(r"[1-9][0-9]*")


def _unobserved(member: CrewMember) -> Optional[str]:
    """Why ``member`` cannot be planned from what was observed, or None.

    The parser would read each of these gaps as a value (a dropped crew, an idle crew, a 0 stat or 0 fatigue), and
    plan from it; leaving the member out with the reason fails closed instead.
    """
    if not _POSITIVE_ID.fullmatch(member.character_id):
        return f"character id {member.character_id!r} is not a positive whole number"
    if not member.training_observed:
        return "training state not observed"
    if member.stat_total is None:
        return "stat improvements not observed"
    if member.fatigue is None:
        return "fatigue not observed"
    return None


def _character(member: CrewMember) -> dict[str, str]:
    """The Character record ``sdk.training``'s parser reads, rebuilt from a fully observed crew member."""
    record = {
        "@CharacterId": member.character_id,
        "@CharacterName": member.name or "",
        "@CharacterDesignId": member.design_id or "",
        "@Level": "" if member.level is None else str(member.level),
        "@RoomId": member.room_id or "",
        "@Fatigue": str(member.fatigue),
        "@TrainingDesignId": member.training_design_id or "0",
        "@TrainingEndDate": member.training_end or "",
    }
    for field_name, attribute in TRAINING_STAT_ATTRIBUTES:
        record[attribute] = str(getattr(member, field_name))
    return record


def _research_design(entry: ResearchDesign) -> dict[str, str]:
    """The ResearchDesign record ``sdk.training``'s parser reads: the level is the name's course, so id, root, name."""
    return {"@ResearchDesignId": entry.design_id, "@RootResearchDesignId": entry.root_id or "",
            "@ResearchName": entry.name}


def training_payloads(snapshot: AccountSnapshot,
                      design: DesignData) -> tuple[Optional[dict[str, object]], tuple[Rejection, ...]]:
    """``build_context`` keyword payloads (all but ``now``) rebuilt from the snapshot and design data."""
    if snapshot.section("crew") is Completeness.MISSING:
        return None, (Rejection("training", "crew section not observed"),)
    if not design.training_designs:
        return None, (Rejection("training", "no TrainingDesign records in the design data"),)
    if not design.character_designs:
        return None, (Rejection("training", "no CharacterDesign records in the design data, "
                                            "so training capacity is unknown"),)
    characters, excluded = [], []
    for member in snapshot.crew:
        problem = _unobserved(member)
        if problem is None:
            characters.append(_character(member))
        else:
            excluded.append(Rejection(f"training {member.character_id}", problem))
    payloads: dict[str, object] = {
        "characters_raw": {"Character": characters},
        "character_designs_raw": {"CharacterDesign": [record.as_dict() for record in design.character_designs]},
        "training_designs_raw": {"TrainingDesign": [record.as_dict() for record in design.training_designs]},
        "rooms_raw": {"Room": [{"@RoomId": room.room_id, "@RoomDesignId": room.design_id or ""}
                               for room in snapshot.rooms]},
        "room_designs_raw": {"RoomDesign": [{"@RoomDesignId": room.design_id, "@RoomName": room.name}
                                            for room in design.rooms]},
        "researches_raw": {"Research": [{"@ResearchDesignId": entry.design_id, "@ResearchState": entry.state or ""}
                                        for entry in snapshot.research]},
        "research_designs_raw": {"ResearchDesign": [_research_design(entry) for entry in design.research]},
        "items_raw": None,
    }
    if snapshot.section("inventory") is not Completeness.MISSING:
        payloads["items_raw"] = {"Item": [
            {"@ItemDesignId": entry.item_design_id, "@Quantity": "" if entry.quantity is None else str(entry.quantity)}
            for entry in snapshot.inventory]}
    return payloads, tuple(excluded)


def training_context(snapshot: AccountSnapshot,
                     design: DesignData) -> tuple[Optional[TrainingContext], tuple[Rejection, ...]]:
    """The planner's context at ``snapshot.observed_at`` and the exclusions; else None and why."""
    payloads, rejected = training_payloads(snapshot, design)
    if payloads is None:
        return None, rejected
    return build_context(now=snapshot.observed_at, **payloads), rejected


def training_strategy(strategy: TrainingStrategy, design: DesignData) -> TrainingStrategy:
    """``strategy`` with the consumables the item designs name, unless it maps consumables of its own."""
    if strategy.consumables:
        return strategy
    consumables = parse_consumable_designs({"ItemDesign": [record.as_dict() for record in design.item_designs]})
    return with_consumables(strategy, consumables) if consumables else strategy


def training_plan(snapshot: AccountSnapshot, design: DesignData, strategy: TrainingStrategy = DEFAULT_STRATEGY,
                  ) -> tuple[tuple[ActionRequest, ...], tuple[Rejection, ...]]:
    """The training block (finishes, then starts and consumable uses) and every training rejection.

    See the module docstring for the order, the deferral, the plan-time checks and the costs.
    """
    context, excluded = training_context(snapshot, design)
    if context is None:
        return (), excluded
    rejected = list(excluded)
    finishes: list[ActionRequest] = []
    primaries: list[ActionRequest] = []
    finished: set[int] = set()  # crew with a FinishTraining in this plan: any later decision for them is advisory
    for decision in plan_training(context, training_strategy(strategy, design)):
        cid = decision.character_id
        after_finish = cid in finished
        if isinstance(decision, Skip):
            reason = DEFERRED if after_finish else f"{decision.reason_code}: {decision.detail}"
            rejected.append(Rejection(f"training {cid}", reason))
            continue
        action = action_for(decision)
        if action is None:  # action_for returns None only for a Skip, handled above
            raise TypeError(f"action_for mapped {decision!r} to no action")
        action_id, params = action
        label = f"{action_id} {cid}"
        if action_id == "finish_training":
            finished.add(cid)
        elif after_finish:
            rejected.append(Rejection(label, DEFERRED))
            continue
        unmet = _unmet_preconditions(action_id, snapshot, params)
        if unmet:
            rejected.append(Rejection(label, unmet))
            continue
        cost = dict(TRAINING_ZERO_COST) if action_id in ZERO_COST_ACTIONS else {}
        request = ActionRequest(action_id, dict(params), decision.reason, cost)
        (finishes if action_id == "finish_training" else primaries).append(request)
    return tuple(finishes + primaries), tuple(rejected)


class TrainingV1(FleetV1):
    """fleet-v1, then the crew training ``sdk.training`` plans from the same snapshot (see the module docstring).

    It holds only its frozen strategy (``DEFAULT_STRATEGY``, owner-approved, unless one is given).
    """

    name = "training-v1"
    models_training = True

    def __init__(self, strategy: TrainingStrategy = DEFAULT_STRATEGY) -> None:
        self.strategy = strategy

    def plan(self, snapshot: AccountSnapshot, design: DesignData, config: PolicyConfig) -> Plan:
        base = super().plan(snapshot, design, replace(config, max_requests=None))
        try:
            requests, rejected = training_plan(snapshot, design, self.strategy)
        except ValueError as error:  # sdk.training validation; it must not cost the account fleet-v1's plan
            requests, rejected = (), (Rejection("training", f"training not planned: {type(error).__name__}: {error}"),)
        kept, refused = _cap_requests(list(base.requests) + list(requests), list(base.rejected) + list(rejected),
                                      config.max_requests)
        return replace(base, requests=tuple(kept), rejected=tuple(refused))
