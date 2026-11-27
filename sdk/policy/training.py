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
from dataclasses import dataclass, replace
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
    remaining_budget,
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
from sdk.training.model import PrestigeRecipe
from sdk.training.parse import parse_prestige_recipes
from sdk.training.planner import (
    CrewDecision,
    CrewObservation,
    CrewPlan,
    RoomObservation,
    TrainingInputs,
    plan_crew,
)
from sdk.training.prestige import PrestigeCandidate, ingredient_eligible, prestige_routes, prestige_targets
from sdk.training.strategy import DEFAULT_CREW_POLICY, CrewPolicy

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
            requests, rejected = self.training_block(snapshot, design, config, base)
        except ValueError as error:  # sdk.training validation; it must not cost the account fleet-v1's plan
            requests, rejected = (), (_not_planned(error),)
        kept, refused = _cap_requests(list(base.requests) + list(requests), list(base.rejected) + list(rejected),
                                      config.max_requests)
        return replace(base, requests=tuple(kept), rejected=tuple(refused))

    def training_block(self, snapshot: AccountSnapshot, design: DesignData, config: PolicyConfig,
                       base: Plan) -> tuple[tuple[ActionRequest, ...], tuple[Rejection, ...]]:
        """The training requests and rejections that follow ``base`` (fleet-v1's uncapped plan)."""
        return training_plan(snapshot, design, self.strategy)


def _not_planned(error: ValueError) -> Rejection:
    return Rejection("training", f"training not planned: {type(error).__name__}: {error}")


# --- training-v2 (SPEC-14 step 3: S14-R1 to S14-R4) ----------------------------------------------------------------
# training-v2 is training-v1's shape (fleet-v1's plan, then a training block, then ``max_requests``) with its own
# block: ``sdk.training.planner.plan_crew`` over fresh observations instead of ``sdk.training.plan_training``.
#
# - Observations (``crew_inputs``). Crew, rooms (with their design's name, type, level and capacity), the courses
#   (``snapshot.courses``, from the factory's design reader) and research come from the snapshot; training capacity
#   from the CharacterDesign records. Each crew's training session is judged at ``snapshot.observed_at``. None stays
#   unknown: a missing stat is never 0 (v1's parser would read it as 0), research is None unless the section was
#   completely observed, and a room's running sessions are None unless every crew phase in it is known. A crew
#   section that is not completely observed (missing or partial) leaves the roster unknown: nothing is planned.
# - Money. The block runs after the progression block, so fleet-v1's planned spend counts first: funds are the
#   balance above the reserve less every earlier request's mineral and gas cost, and the spend caps shrink the same
#   way (``progression.remaining_budget``: cost kinds read through ``runner.COST_KINDS``; an unknown kind plans no
#   training). ``max_requests`` leaves ``max_requests - len(base requests)`` slots.
# - Output. At most one ``add_training`` (the crew plan's start), costed at the course's price (TRAINING_ZERO_COST
#   for a free course), still checked against the catalog's preconditions. Every skip is a ``training <id>`` (or
#   ``training <role>`` for an uncovered role) rejection "<code>: <detail>". No ``finish_training`` and no
#   ``use_consumable``: training-v2 never claims a course, uses an item, moves crew or spends a Prestige ingredient.
# - Previews. ``crew_plan`` is the whole block as one value (``CrewPreview``: inputs, final decisions, requests and
#   rejections); the plan takes its requests and rejections and ``run.py --crew-plan`` prints its inputs and
#   decisions, so both previews show the same start or skip. The dry run journals the plan.

TRAINING_V2 = "training-v2"
_WHOLE = re.compile(r"[0-9]+")


def _positive_id(value: Optional[str]) -> Optional[int]:
    return int(value) if value is not None and _POSITIVE_ID.fullmatch(value) else None


def _capacities(design: DesignData) -> dict[int, int]:
    """CharacterDesign id -> @TrainingCapacity, for records where both are whole numbers (the id positive)."""
    capacities = {}
    for record in design.character_designs:
        attributes = record.as_dict()
        design_id = _positive_id(attributes.get("@CharacterDesignId"))
        capacity = attributes.get("@TrainingCapacity", "")
        if design_id is not None and _WHOLE.fullmatch(capacity):
            capacities[design_id] = int(capacity)
    return capacities


def _crew_observation(snapshot: AccountSnapshot, member: CrewMember) -> CrewObservation:
    values = [getattr(member, field_name) for field_name, _ in TRAINING_STAT_ATTRIBUTES]
    stats = None if None in values else {
        attribute.removeprefix("@"): value for (_, attribute), value in zip(TRAINING_STAT_ATTRIBUTES, values)}
    return CrewObservation(int(member.character_id), _positive_id(member.design_id), stats, member.fatigue,
                           member.room_id, snapshot.training_session(member.character_id))


def _roster_unknown(snapshot: AccountSnapshot) -> Optional[Rejection]:
    """Why the roster is unknown (the crew section not completely observed), or None."""
    section = snapshot.section("crew")
    if section is Completeness.MISSING:
        return Rejection("training", "crew section not observed")
    if section is not Completeness.COMPLETE:
        return Rejection("training", f"crew section {section.value}: the roster is unknown")
    return None


def crew_inputs(snapshot: AccountSnapshot, design: DesignData, *, funds: Mapping[str, Optional[int]],
                spend_caps: Mapping[str, Optional[int]],
                action_slots: Optional[int]) -> tuple[Optional[TrainingInputs], tuple[Rejection, ...]]:
    """``plan_crew``'s inputs from the snapshot and design data (see the section comment), and the crew left out.

    A crew section that is not completely observed (missing, or partial: a record without an id) leaves the roster
    unknown, so nothing is planned (None and why).
    """
    unknown = _roster_unknown(snapshot)
    if unknown is not None:
        return None, (unknown,)
    crew, rejected = [], []
    for member in snapshot.crew:
        if _POSITIVE_ID.fullmatch(member.character_id):
            crew.append(_crew_observation(snapshot, member))
        else:
            rejected.append(Rejection(f"training {member.character_id}",
                                      f"character id {member.character_id!r} is not a positive whole number"))
    rooms = {room.room_id: RoomObservation(room.room_id, room.design_name, room.room_type, room.level, room.capacity,
                                           room.status, snapshot.running_sessions(room.room_id))
             for room in snapshot.rooms}
    research = None
    if snapshot.section("research") is Completeness.COMPLETE:
        research = {int(entry.design_id): entry.state for entry in snapshot.research
                    if _POSITIVE_ID.fullmatch(entry.design_id)}
    inputs = TrainingInputs(tuple(crew), rooms, snapshot.courses, research, _capacities(design), funds, spend_caps,
                            action_slots)
    return inputs, tuple(rejected)


@dataclass(frozen=True)
class CrewPreview:
    """training-v2's block as one value, read by the plan (``training_block``) and by ``--crew-plan`` alike.

    ``base`` is fleet-v1's uncapped plan, ``inputs`` the observations the planner used and ``plan`` the planner's crew
    plan (both None when nothing could be planned). ``decisions`` are the final crew decisions: the plan's, except a
    start that failed the catalog's preconditions, which becomes a ``training_in_progress`` skip (the safety net; the
    add_training preconditions all concern the member's presence and idle training state). ``requests`` holds the
    start, if any, as an ``add_training`` request; ``rejected`` holds the crew left out (or why nothing was planned),
    then every skip as a ``training <id>`` (``training <role>``) rejection, a failed precondition as an
    ``add_training <id>`` rejection with the catalog's reasons."""

    base: Plan
    inputs: Optional[TrainingInputs]
    plan: Optional[CrewPlan]
    decisions: tuple[CrewDecision, ...]
    requests: tuple[ActionRequest, ...]
    rejected: tuple[Rejection, ...]


def _final_decisions(plan: CrewPlan, snapshot: AccountSnapshot, excluded: tuple[Rejection, ...],
                     ) -> tuple[tuple[CrewDecision, ...], tuple[ActionRequest, ...], tuple[Rejection, ...]]:
    """The decisions after the precondition safety net, the start as a request, and every rejection (CrewPreview)."""
    decisions: list[CrewDecision] = []
    requests: list[ActionRequest] = []
    rejected = list(excluded)
    for decision in plan.decisions:
        if decision.course is None:
            owner = decision.role if decision.character_id is None else decision.character_id
            rejected.append(Rejection(f"training {owner}", f"{decision.code}: {decision.detail}"))
            decisions.append(decision)
            continue
        params = {"training_design_id": str(decision.course.design_id), "character_id": str(decision.character_id)}
        unmet = _unmet_preconditions("add_training", snapshot, params)
        if unmet:
            rejected.append(Rejection(f"add_training {decision.character_id}", unmet))
            decisions.append(CrewDecision(decision.role, decision.character_id, None, "training_in_progress",
                                          f"preconditions failed: {unmet}"))
            continue
        price = decision.course.price
        cost = {kind: price.amount if price.currency == currency else 0
                for kind, currency in (("minerals", "mineral"), ("gas", "gas"))}
        requests.append(ActionRequest("add_training", params, f"{decision.role} specialist: {decision.course.name}",
                                      cost))
        decisions.append(decision)
    return tuple(decisions), tuple(requests), tuple(rejected)


class TrainingV2(TrainingV1):
    """training-v1's shape with SPEC-14's crew policy as its training block (see the section comment above).

    ``reads_course_designs`` asks the fleet factory for a snapshot design reader, so ``snapshot.courses`` and the
    rooms' design facts are observed fresh on every snapshot. ``holds_paid_training`` tells the execute gate to hold
    every ``add_training`` until SPEC-14 step 4 adds paid execution.
    """

    name = TRAINING_V2
    reads_course_designs = True
    holds_paid_training = True
    crew_policy: CrewPolicy = DEFAULT_CREW_POLICY

    def crew_plan(self, snapshot: AccountSnapshot, design: DesignData, config: PolicyConfig,
                  base: Optional[Plan] = None) -> CrewPreview:
        """The crew plan after ``base`` (fleet-v1's uncapped plan, computed when not given)."""
        if base is None:
            base = FleetV1.plan(self, snapshot, design, replace(config, max_requests=None))
        slots = None if config.max_requests is None else config.max_requests - len(base.requests)
        try:
            funds, spend_caps = remaining_budget(snapshot, config, base.requests)
            inputs, excluded = crew_inputs(snapshot, design, funds=funds, spend_caps=spend_caps, action_slots=slots)
            crew = None if inputs is None else plan_crew(inputs, self.crew_policy)
        except ValueError as error:  # sdk.training validation or an unreadable earlier cost: keep fleet-v1's plan
            return CrewPreview(base, None, None, (), (), (_not_planned(error),))
        if crew is None:
            return CrewPreview(base, None, None, (), (), excluded)
        return CrewPreview(base, inputs, crew, *_final_decisions(crew, snapshot, excluded))

    def training_block(self, snapshot: AccountSnapshot, design: DesignData, config: PolicyConfig,
                       base: Plan) -> tuple[tuple[ActionRequest, ...], tuple[Rejection, ...]]:
        preview = self.crew_plan(snapshot, design, config, base)
        return preview.requests, preview.rejected


# --- the crew plan preview (run.py --crew-plan) --------------------------------------------------------------------
# Pure: the caller reads the Prestige recipes (``Client.prestigeCharacterTo``, None when a read failed) and prints the
# lines. One line per fact; crew and role lines carry no free text, so a skip line holds exactly one skip code.


def character_design_names(design: DesignData) -> dict[int, str]:
    """CharacterDesign id -> @CharacterDesignName, for records with a positive whole id and a name."""
    names = {}
    for record in design.character_designs:
        attributes = record.as_dict()
        design_id = _positive_id(attributes.get("@CharacterDesignId"))
        if design_id is not None and attributes.get("@CharacterDesignName"):
            names[design_id] = attributes["@CharacterDesignName"]
    return names


def prestige_target_ids(design: DesignData, crew_policy: CrewPolicy = DEFAULT_CREW_POLICY) -> tuple[int, ...]:
    """The character designs named as the policy's original Prestige targets, ordered by (name, id)."""
    return prestige_targets(character_design_names(design), crew_policy.prestige_targets)


def prestige_roster(snapshot: AccountSnapshot) -> Optional[tuple[PrestigeCandidate, ...]]:
    """Every crew member with a positive whole id as a possible ingredient; None when the roster is unknown (the crew
    section not completely observed)."""
    if _roster_unknown(snapshot) is not None:
        return None
    return tuple(PrestigeCandidate(int(member.character_id), _positive_id(member.design_id),
                                   ingredient_eligible(snapshot.training_session(member.character_id)))
                 for member in snapshot.crew if _POSITIVE_ID.fullmatch(member.character_id))


def _shown(value: object) -> object:
    return "unknown" if value is None else value


def _decision_line(decision: CrewDecision) -> str:
    """One crew or role line; a skip whose check lacked data ends ", eligibility unknown" (no skip code words)."""
    outcome = decision.course.name if decision.course is not None else f"skip {decision.code}"
    if decision.unknown:
        outcome += ", eligibility unknown"
    if decision.character_id is None:
        return f"  role {decision.role}: {outcome}"
    if decision.role is None:
        return f"  crew {decision.character_id}: {outcome}"
    return f"  crew {decision.character_id} {decision.role}: {outcome}"


def _prestige_lines(snapshot: AccountSnapshot, design: DesignData, crew_policy: CrewPolicy,
                    replies: Mapping[int, object]) -> list[str]:
    names = character_design_names(design)
    targets = prestige_target_ids(design, crew_policy)
    recipes = {target: tuple(sorted(parse_prestige_recipes(reply), key=_recipe_order))
               for target, reply in replies.items() if reply is not None}
    routes = prestige_routes(targets, recipes, prestige_roster(snapshot))
    lines = []
    for target in targets:
        name = names[target]
        mine = [route for route in routes if route.target_design_id == target]
        if replies.get(target) is None:
            lines.append(f"  prestige {name}: recipes unavailable")
        elif not mine:
            lines.append(f"  prestige {name}: no recipe")
        for route in mine:
            first, second = (names.get(design_id, f"design {design_id}") for design_id in route.ingredient_design_ids)
            unknown = f", eligibility unknown for {route.unknown}" if route.unknown else ""
            lines.append(f"  prestige {name} <- {first} + {second}: missing {_shown(route.missing)}{unknown}")
    return lines


def _recipe_order(recipe: PrestigeRecipe) -> tuple[int, int]:
    return tuple(sorted((recipe.first_design_id, recipe.second_design_id)))


def crew_plan_lines(alias: str, policy: TrainingV2, snapshot: AccountSnapshot, design: DesignData,
                    preview: CrewPreview, replies: Mapping[int, object]) -> tuple[str, ...]:
    """The ``--crew-plan`` lines for one account (README "Crew plan" lists every format).

    The facility, research and crew lines render the preview's own ``inputs`` and final ``decisions``, so they show
    exactly what the plan used. The preview has no journal, so the header says that spend journaled earlier in the
    budget period is not counted.
    """
    crew_policy = policy.crew_policy
    lines = [f"Crew plan for account '{alias}' ({policy.name}, observed {snapshot.observed_at.isoformat()}; spend "
             "journaled earlier this period is not counted)"]
    inputs = preview.inputs
    if inputs is not None:
        for room in sorted(inputs.rooms.values(), key=lambda entry: entry.room_id):
            if room.room_type == crew_policy.training_room_type:
                lines.append(f"  facility {room.room_id} {_shown(room.design_name)} level {_shown(room.level)}: "
                             f"status {_shown(room.status)}, training {_shown(room.running)}/{_shown(room.capacity)}")
        for research in sorted({course.required_research_design_id for course in inputs.courses or ()
                                if course.required_research_design_id}):
            state = None if inputs.research is None else inputs.research.get(research) or "not started"
            lines.append(f"  research {research}: {_shown(state)}")
    if preview.plan is None:
        lines.append("  crew: not planned")
    else:
        lines.extend(_decision_line(decision) for decision in preview.decisions)
    lines.extend(_prestige_lines(snapshot, design, crew_policy, replies))
    return tuple(lines)
