"""Pure, total, deterministic crew-training planner.

``plan_training(ctx, strategy)`` returns decisions in ``ctx.crew`` order. Each crew gets exactly one primary decision
(StartTraining | UseConsumable | Skip), optionally preceded by a FinishTraining. A crew's decision depends only on
that crew, the context and the strategy, with one deliberate exception: consumable stock is shared. Each
UseConsumable reserves one unit, so crew planned later see the reduced stock and the plan never uses an item more
often than ``ctx.items`` holds it. Room and design choices never leak between crew.

Stock order (two passes): crew whose training is not finished are planned first, in ``ctx.crew`` order; crew whose
training is finished (design id set and end <= now) are planned afterwards, in ``ctx.crew`` order, against the stock
the first pass left. A finished crew's primary is advisory (see below), so it must never take a scarce item from a
crew whose decision is final. The output order still follows ``ctx.crew``.

Decision precedence per crew:
 1. training finished but not collected (design id set and end <= now) -> FinishTraining first, then continue
 2. name not in any role -> Skip unknown_role
 3. room (own room name unless blank, else ``ctx.rooms[room_id]``) not a training room -> Skip not_in_training_room
 4. capacity <= 0 -> Skip design_missing ("no training capacity"); total >= TP cap -> Skip tp_cap_reached
    (detail in training points: "<total> TP >= cap <cap> TP (<pct>% of capacity <capacity>)")
 5. design id set but no end date (unparseable) -> Skip training_in_progress ("training end unknown"), with no
    consumable: the crew may still be training, so nothing is started or used (fails closed);
    training in progress (end > now) -> consumable fallback, else Skip training_in_progress
 6. percent >= complete_percent -> consumable fallback, else Skip complete
 7. band's room type differs from the crew's room type -> Skip not_in_training_room ("move to <type>");
    no consumable here: the crew should move instead
 8. band research gate unmet (default: Fitness or Education course 101 for primary T1, 201 for T2, 203 for T3;
    secondary T1 ungated) -> research_missing; fatigue >= limit -> fatigued (consumable fallback for both)
 9. no eligible design (exact name, room type, lowest id) -> design_missing; design's own research missing ->
    research_missing (consumable fallback for both)
10. StartTraining

Consumables are a fallback only: never instead of a startable training, never at/above the TP cap, never for an
unknown role, outside a training room or while the training end is unknown, and only when the strategy maps an item
that is still in stock. The item boosts ``target_stat``: the stat of the design the crew would train in its current
band (looked up for the band slot's room type), else the crew's highest positive stat; a crew with neither gets no
consumable.

After a FinishTraining:
- The primary decision that follows a FinishTraining is advisory: it is based on the pre-finish stats, so it may
  name the wrong tier. The executor must not run it as-is. It rebuilds that crew from the FinishTraining response
  and replaces the advisory decision with ``replan_after_finish(ctx, rebuilt_crew, strategy, items=stock)``.
- The executor replans the finished crew in ``ctx.crew`` order. ``stock`` starts as
  ``remaining_stock(ctx.items, plan, exclude_character_ids=<finished crew ids>)`` (what every non-finished crew's
  UseConsumable left) and is reduced by each replan's UseConsumable (``remaining_stock(stock, replanned)``) before
  the next finished crew is replanned. That mirrors the planner's own second pass, so the executor never commits
  more of an item than the ship holds.

Deliberate changes from Client.manageTraining:
- Per-tier elapsed-time gates are removed: a training is finished iff ``training_end <= now``. The original compared
  the END date against now minus the training duration and so idled a full extra duration (required by the slice).
- The fatigue gate (``max_training_fatigue`` = 100, Skip fatigued) is new; the slice's closed Skip enum requires it.
- Captain detection: CrewState has no captain flag, so a crew whose stars have a ``captain_tp_caps`` entry (a 6-star
  Special) and whose design capacity reaches it is treated as a captain. The capacity (server TrainingCapacity)
  always bounds the cap, so the heuristic can never let a crew exceed the server's own limit.
- FinishTraining is emitted whenever a design id is set and end <= now. This relies on the server clearing
  TrainingDesignId after collection; the executor must treat a "nothing to finish" error as benign.
- Room names match exactly (stripped, case-sensitive); the original substring matching produced false positives.
  New room names are added to ``strategy.room_types`` (data), not to code.
- Skip design_missing also covers zero training capacity (the Skip enum is closed); the detail tells them apart.
- ``sdk.training.parse`` keeps its own envelope walker so ``sdk.training`` never imports ``sdk.client``.

Notes:
- Percent is the exact integer ceiling of ``total * 100 / training_capacity`` (the original float ``math.ceil`` was
  one too high for e.g. 7/100 and 55/100).
- The same character id appearing twice in ``ctx.crew`` is planned twice, in order (``sdk.training.parse`` never
  produces that: it keeps the first record per character id).
- ``TrainingDesign.tier`` and ``duration`` are informational; the role table's training name is the authority.
- The helpers below are public, supported API (see ``__all__``). Every helper that reads strategy data takes the
  strategy as a required argument; only the entry points default to ``DEFAULT_STRATEGY``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from sdk.training.model import (
    CANONICAL_STATS,
    PRICE_MALFORMED,
    PRICE_OK,
    PRICE_PREMIUM,
    PRICE_UNAVAILABLE,
    SESSION_ENDED_UNCLAIMED,
    SESSION_RUNNING,
    SESSION_UNKNOWN,
    CourseObservation,
    SKIP_COMPLETE,
    SKIP_DESIGN_MISSING,
    SKIP_FATIGUED,
    SKIP_NOT_IN_TRAINING_ROOM,
    SKIP_RESEARCH_MISSING,
    SKIP_TP_CAP_REACHED,
    SKIP_TRAINING_IN_PROGRESS,
    SKIP_UNKNOWN_ROLE,
    CrewState,
    Decision,
    FinishTraining,
    Skip,
    StartTraining,
    TrainingContext,
    TrainingDesign,
    TrainingSession,
    UseConsumable,
    frozen_mapping,
)
from sdk.training.strategy import (
    CREW_SKIP_CODES,
    DEFAULT_CREW_POLICY,
    DEFAULT_STRATEGY,
    STAT_CODES,
    ConsumableRule,
    CrewPolicy,
    Role,
    TierBand,
    TrainingStrategy,
)

__all__ = (
    "CrewDecision",
    "CrewObservation",
    "CrewPlan",
    "RoleAssignment",
    "RoomObservation",
    "TrainingInputs",
    "assign_roles",
    "band_for",
    "choose_course",
    "course_blocker",
    "crew_blocker",
    "design_research_ok",
    "effective_cap",
    "highest_stat",
    "percent",
    "pick_consumable",
    "pick_design",
    "plan_crew",
    "plan_training",
    "remaining_stock",
    "replan_after_finish",
    "research_ok",
    "resolve_room_name",
    "role_for",
    "room_type_for",
    "target_stat",
    "total_points",
    "training_state",
)

TRAINING_IDLE = "idle"
TRAINING_IN_PROGRESS = "in_progress"
TRAINING_FINISHED = "finished"
TRAINING_UNKNOWN = "unknown"
TRAINING_END_UNKNOWN = "training end unknown"


def percent(total: int, capacity: int) -> int:
    """Training progress as an exact integer ceiling of ``total * 100 / capacity``."""
    if capacity <= 0:
        raise ValueError(f"training capacity must be positive, got {capacity}")
    return -(-total * 100 // capacity)


def band_for(pct: int, strategy: TrainingStrategy) -> TierBand | None:
    """The (slot, tier) band for ``pct``; None once training is complete.

    ``strategy.bands`` is sorted and tiles 0..complete_percent-1 (validated), so a band always matches below
    ``complete_percent``. Percentages below the lowest band (only possible with negative stats) clamp to it.
    """
    if pct >= strategy.complete_percent:
        return None
    return next(band for band in strategy.bands if pct <= band.high)


def room_type_for(room_name: str | None, strategy: TrainingStrategy) -> str | None:
    """Canonical room type for an exact (whitespace-stripped, case-sensitive) room name, else None."""
    if not room_name:
        return None
    return strategy.room_types.get(room_name.strip())


def resolve_room_name(crew: CrewState, ctx: TrainingContext) -> str | None:
    """The crew's own room name wins unless blank; otherwise its room id is looked up in ``ctx.rooms``.

    Never borrows another crew's room. A whitespace-only name counts as missing, so the id lookup still applies.
    """
    if crew.room_name and crew.room_name.strip():
        return crew.room_name
    if crew.room_id is None:
        return None
    return ctx.rooms.get(crew.room_id)


def role_for(name: str, strategy: TrainingStrategy) -> Role | None:
    """The role whose character set contains ``name`` exactly (strategy validation keeps it unique)."""
    return next((role for role in strategy.roles if name in role.characters), None)


def effective_cap(crew: CrewState, strategy: TrainingStrategy) -> int:
    """Training-point cap from strategy data only: the design's capacity, limited by the strategy's TP tables.

    Stars come from ``strategy.rarity_stars`` (unknown rarity = 0 stars = capacity only). A crew whose stars have a
    ``captain_tp_caps`` entry and whose capacity reaches it is a captain and gets that cap; otherwise an
    ``exceptional_tp_caps[(stars, name)]`` entry wins over ``tp_caps[stars]``. A missing table entry leaves the
    capacity as the cap.
    """
    capacity = crew.training_capacity
    stars = strategy.rarity_stars.get(crew.rarity, 0)
    captain_cap = strategy.captain_tp_caps.get(stars)
    if captain_cap is not None and capacity >= captain_cap:
        table_cap = captain_cap
    else:
        table_cap = strategy.exceptional_tp_caps.get((stars, crew.name), strategy.tp_caps.get(stars))
    return capacity if table_cap is None else min(capacity, table_cap)


def research_ok(band: TierBand, research_levels: Mapping[str, int], strategy: TrainingStrategy) -> bool:
    """True when one gate alternative for the band is met (exact research-name keys); an empty gate is ungated."""
    alternatives = strategy.research_gates[(band.slot, band.tier)]
    if not alternatives:
        return True
    return any(research_levels.get(name, 0) >= level for name, level in alternatives)


def design_research_ok(design: TrainingDesign, research_levels: Mapping[str, int]) -> bool:
    """A design naming a required research needs that exact research at level >= 1."""
    if design.required_research is None:
        return True
    return research_levels.get(design.required_research, 0) >= 1


def pick_design(name: str, room_type: str, designs: Sequence[TrainingDesign]) -> TrainingDesign | None:
    """Exact-name design usable in ``room_type``; duplicates resolve to the lowest id (order-independent)."""
    eligible = [design for design in designs if design.name == name and design.required_room_type in (None, room_type)]
    return min(eligible, key=lambda design: design.id, default=None)


def highest_stat(stats: Mapping[str, int]) -> str | None:
    """The crew's highest positive stat (ties resolve to the earliest in CANONICAL_STATS); None when none is > 0."""
    best: str | None = None
    best_value = 0
    for stat in CANONICAL_STATS:
        value = stats.get(stat, 0)
        if value > best_value:
            best, best_value = stat, value
    return best


def _band_design(band: TierBand, role: Role, designs: Sequence[TrainingDesign]) -> TrainingDesign | None:
    """The design ``role`` trains in ``band``, looked up for that band slot's room type."""
    slot = role.slot(band.slot)
    return pick_design(slot.trainings[band.tier - 1], slot.room_type, designs)


def target_stat(crew: CrewState, designs: Sequence[TrainingDesign], strategy: TrainingStrategy) -> str | None:
    """The stat a consumable for ``crew`` should boost, or None when there is nothing to boost.

    The stat of the design the crew would train in its current band (role + band -> training name -> pick_design
    for the band slot's room type) when that design exists and names a canonical stat; otherwise the crew's highest
    positive stat (``highest_stat``). No role, no capacity or no band (complete) skips the design lookup.
    """
    role = role_for(crew.name, strategy)
    if role is not None and crew.training_capacity > 0:
        band = band_for(percent(total_points(crew.stats), crew.training_capacity), strategy)
        if band is not None:
            design = _band_design(band, role, designs)
            if design is not None and design.stat in STAT_CODES:
                return design.stat
    return highest_stat(crew.stats)


def pick_consumable(
    crew: CrewState,
    pct: int,
    stock: Mapping[int, int],
    designs: Sequence[TrainingDesign],
    strategy: TrainingStrategy,
) -> UseConsumable | None:
    """First consumable rule matching (pct, fatigue) whose item for ``target_stat`` is in ``stock``.

    The caller decides whether a consumable may be considered (fallback only) and owns the stock bookkeeping;
    this only checks the rules and the stock it is given. No target stat -> None.
    """
    stat = target_stat(crew, designs, strategy)
    if stat is None:
        return None
    code = STAT_CODES[stat]
    for rule in strategy.consumable_rules:
        if not _rule_matches(rule, pct, crew.fatigue):
            continue
        item_design_id = strategy.consumables.get((code, rule.tier))
        if item_design_id is not None and stock.get(item_design_id, 0) > 0:
            reason = f"{rule.tier} {code} consumable at {pct}% with fatigue {crew.fatigue}"
            return UseConsumable(crew.character_id, item_design_id, reason)
    return None


def _within(value: int, low: int, high: int | None) -> bool:
    return value >= low and (high is None or value <= high)


def _rule_matches(rule: ConsumableRule, pct: int, fatigue: int) -> bool:
    return _within(pct, rule.min_percent, rule.max_percent) and _within(fatigue, rule.min_fatigue, rule.max_fatigue)


def training_state(crew: CrewState, now: datetime) -> str:
    """idle (no design id), unknown (design id but no end date), in progress (end > now) or finished (end <= now)."""
    if not crew.training_design_id:
        return TRAINING_IDLE
    if crew.training_end is None:
        return TRAINING_UNKNOWN
    return TRAINING_IN_PROGRESS if crew.training_end > now else TRAINING_FINISHED


def total_points(stats: Mapping[str, int]) -> int:
    """Training points spent: the sum of the nine canonical stats (missing = 0)."""
    return sum(stats.get(stat, 0) for stat in CANONICAL_STATS)


def remaining_stock(
    items: Mapping[int, int], decisions: Iterable[Decision], exclude_character_ids: Iterable[int] = ()
) -> Mapping[int, int]:
    """``items`` minus one unit per UseConsumable in ``decisions`` (read-only copy; never below 0).

    UseConsumables of crew in ``exclude_character_ids`` are ignored (pass the finished crew's ids to drop their
    advisory primaries). Only items present in ``items`` are returned. ``items`` itself is not changed.
    """
    excluded = frozenset(exclude_character_ids)
    used: dict[int, int] = {}
    for decision in decisions:
        if isinstance(decision, UseConsumable) and decision.character_id not in excluded:
            used[decision.item_design_id] = used.get(decision.item_design_id, 0) + 1
    return frozen_mapping({item: max(quantity - used.get(item, 0), 0) for item, quantity in items.items()})


def plan_training(ctx: TrainingContext, strategy: TrainingStrategy = DEFAULT_STRATEGY) -> tuple[Decision, ...]:
    """Plan every crew; decisions follow ``ctx.crew`` order (see the module docstring for precedence and stock)."""
    finished = [training_state(crew, ctx.now) == TRAINING_FINISHED for crew in ctx.crew]
    stock = dict(ctx.items)
    primaries: dict[int, Decision] = {}
    for second_pass in (False, True):
        for index, crew in enumerate(ctx.crew):
            if finished[index] != second_pass:
                continue
            primary = _primary_decision(crew, ctx, strategy, stock)
            if isinstance(primary, UseConsumable):
                stock[primary.item_design_id] -= 1
            primaries[index] = primary
    decisions: list[Decision] = []
    for index, crew in enumerate(ctx.crew):
        if finished[index]:
            decisions.append(FinishTraining(crew.character_id, f"training ended {crew.training_end}"))
        decisions.append(primaries[index])
    return tuple(decisions)


def replan_after_finish(
    ctx: TrainingContext,
    crew: CrewState,
    strategy: TrainingStrategy = DEFAULT_STRATEGY,
    items: Mapping[int, int] | None = None,
) -> tuple[Decision, ...]:
    """The decision that replaces the advisory primary after a FinishTraining for ``crew``.

    ``crew`` is the crew rebuilt from the FinishTraining response (fresh stats). Its training fields are cleared
    before planning, so a response that still carries the finished training's id and end date can never produce
    another FinishTraining (no finish loop). Only ``crew`` is planned; ``ctx`` supplies everything else. ``items``
    overrides ``ctx.items`` as the stock (None -> ``ctx.items``); pass the stock remaining after every non-finished
    crew's UseConsumable and the earlier replans (see the module docstring). Returns exactly one primary decision.
    """
    idle = replace(crew, training_design_id=None, training_end=None)
    stocked = ctx if items is None else replace(ctx, items=items)
    return plan_training(replace(stocked, crew=(idle,)), strategy)


def _primary_decision(
    crew: CrewState, ctx: TrainingContext, strategy: TrainingStrategy, stock: Mapping[int, int]
) -> Decision:
    """Steps 2-10 of the module-level precedence; ``stock`` is what the crew planned earlier left of ``ctx.items``."""
    cid = crew.character_id

    def consumable_or(skip: Skip, pct: int) -> Decision:
        return pick_consumable(crew, pct, stock, ctx.designs, strategy) or skip

    role = role_for(crew.name, strategy)
    if role is None:
        return Skip(cid, SKIP_UNKNOWN_ROLE, f"{crew.name!r} has no training role")
    room_name = resolve_room_name(crew, ctx)
    room_type = room_type_for(room_name, strategy)
    if room_type is None:
        return Skip(cid, SKIP_NOT_IN_TRAINING_ROOM, f"room {room_name!r} is not a training room")
    capacity = crew.training_capacity
    if capacity <= 0:
        return Skip(cid, SKIP_DESIGN_MISSING, "no training capacity")
    total = total_points(crew.stats)
    pct = percent(total, capacity)
    cap = effective_cap(crew, strategy)
    if total >= cap:
        return Skip(cid, SKIP_TP_CAP_REACHED, f"{total} TP >= cap {cap} TP ({pct}% of capacity {capacity})")
    state = training_state(crew, ctx.now)
    if state == TRAINING_UNKNOWN:
        return Skip(cid, SKIP_TRAINING_IN_PROGRESS, TRAINING_END_UNKNOWN)
    if state == TRAINING_IN_PROGRESS:
        return consumable_or(Skip(cid, SKIP_TRAINING_IN_PROGRESS, f"training ends at {crew.training_end}"), pct)
    band = band_for(pct, strategy)
    if band is None:
        complete = f"{pct}% >= {strategy.complete_percent}%: training complete"
        return consumable_or(Skip(cid, SKIP_COMPLETE, complete), pct)
    slot = role.slot(band.slot)
    if room_type != slot.room_type:
        return Skip(cid, SKIP_NOT_IN_TRAINING_ROOM, f"move to {slot.room_type}")
    if not research_ok(band, ctx.research_levels, strategy):
        return consumable_or(Skip(cid, SKIP_RESEARCH_MISSING, f"{band.slot} T{band.tier} research gate not met"), pct)
    if crew.fatigue >= strategy.max_training_fatigue:
        fatigued = f"fatigue {crew.fatigue} >= {strategy.max_training_fatigue}"
        return consumable_or(Skip(cid, SKIP_FATIGUED, fatigued), pct)
    name = slot.trainings[band.tier - 1]
    design = _band_design(band, role, ctx.designs)
    if design is None:
        return consumable_or(Skip(cid, SKIP_DESIGN_MISSING, f"no design named {name} for {room_type}"), pct)
    if not design_research_ok(design, ctx.research_levels):
        gated = f"{name} requires research {design.required_research}"
        return consumable_or(Skip(cid, SKIP_RESEARCH_MISSING, gated), pct)
    return StartTraining(cid, design.id, f"{band.slot} T{band.tier} {name} at {pct}% in {room_type}")


# --- SPEC-14 training-v2: roles and courses (S14-R1, S14-R2) --------------------------------------------------------
# training-v2 plans from fresh observations (``CrewObservation``, ``RoomObservation``, ``model.CourseObservation``),
# never from ``CrewState``: None is "not observed" everywhere and always fails closed, while 0 is an observed value.
#
# Roles (``assign_roles``). Each of ``policy.roles`` is one specialist slot measured by one stat. Among crew whose nine
# stats were all observed, the assignment fills as many distinct slots as possible, then maximises the sum of the
# assigned stats, then gives the higher scores to the lower instance ids, then fills earlier slots (policy order)
# first. No crew holds two slots. A crew with any stat unobserved is never assigned (``stats_unknown``): design data
# carries no base stats to estimate from, and the TP cap check needs all nine. Only each slot's top
# ``len(policy.roles)`` crew (by stat, then id) can be in the best assignment, so the exhaustive search is bounded by
# the slot count, never by the roster size.


@dataclass(frozen=True)
class CrewObservation:
    """One crew member as fresh reads report it; None = not observed.

    ``stats`` holds all nine ``CANONICAL_STATS`` (stored read-only), or is None when any was not observed.
    ``design_id`` and ``fatigue`` are whole numbers (not bools) or None. ``session`` is the training judged at the
    observation time (``model.TrainingSession``).
    """

    character_id: int
    design_id: int | None
    stats: Mapping[str, int] | None
    fatigue: int | None
    room_id: str | None
    session: TrainingSession

    def __post_init__(self) -> None:
        if not _whole(self.character_id) or self.character_id <= 0:
            raise ValueError(f"character_id must be a positive int: {self.character_id!r}")
        for name in ("design_id", "fatigue"):
            value = getattr(self, name)
            if value is not None and not _whole(value):
                raise ValueError(f"{name} must be a whole number or None: {value!r}")
        if self.stats is not None:
            if set(self.stats) != set(CANONICAL_STATS):
                raise ValueError(f"stats must hold exactly the nine CANONICAL_STATS, got {sorted(self.stats)}")
            object.__setattr__(self, "stats", frozen_mapping(self.stats))


def _whole(value: object) -> bool:
    """An int (not a bool) that is not negative."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class RoomObservation:
    """One room and its design's name, type, level and training capacity; ``running`` counts the running sessions in
    it (None = unknown). ``room_id`` is non-empty text; the names, type and status are text or None; the counts are
    whole numbers (not bools) or None. Anything else raises ValueError."""

    room_id: str
    design_name: str | None
    room_type: str | None
    level: int | None
    capacity: int | None
    status: str | None
    running: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.room_id, str) or not self.room_id:
            raise ValueError(f"room_id must be non-empty text: {self.room_id!r}")
        for name in ("design_name", "room_type", "status"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be text or None: {value!r}")
        for name in ("level", "capacity", "running"):
            value = getattr(self, name)
            if value is not None and not _whole(value):
                raise ValueError(f"{name} must be a whole number or None: {value!r}")


@dataclass(frozen=True)
class TrainingInputs:
    """Everything ``plan_crew`` reads, observed together.

    ``courses`` None = the course designs were not observed; ``research`` (design id -> state) None = the research
    section was not completely observed. ``capacities`` maps a character design id to its TrainingCapacity. ``funds``
    and ``spend_caps`` map "mineral" / "gas" to what is spendable above the reserve and left under the spend cap after
    earlier planned requests (None: unknown balance / no cap); both must name "mineral" and "gas", each an int (not a
    bool; a balance below the reserve is negative) or None. ``action_slots`` is the requests left under
    ``max_requests`` (None = unlimited). Mappings are stored read-only; crew ids must be distinct.
    """

    crew: tuple[CrewObservation, ...]
    rooms: Mapping[str, RoomObservation]
    courses: tuple[CourseObservation, ...] | None
    research: Mapping[int, str] | None
    capacities: Mapping[int, int]
    funds: Mapping[str, int | None]
    spend_caps: Mapping[str, int | None]
    action_slots: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "crew", tuple(self.crew))
        ids = [entry.character_id for entry in self.crew]
        if len(set(ids)) != len(ids):
            raise ValueError(f"crew ids must be distinct: {sorted(ids)}")
        object.__setattr__(self, "rooms", frozen_mapping(self.rooms))
        if self.courses is not None:
            object.__setattr__(self, "courses", tuple(self.courses))
        if self.research is not None:
            object.__setattr__(self, "research", frozen_mapping(self.research))
        object.__setattr__(self, "capacities", frozen_mapping(self.capacities))
        for name in ("funds", "spend_caps"):
            amounts = frozen_mapping(getattr(self, name))
            for currency in ("mineral", "gas"):
                if currency not in amounts:
                    raise ValueError(f"{name} must name {currency!r} (None when unknown or uncapped)")
            for currency, amount in amounts.items():
                if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
                    raise ValueError(f"{name}[{currency!r}] must be a whole number or None: {amount!r}")
            object.__setattr__(self, name, amounts)


@dataclass(frozen=True)
class RoleAssignment:
    """Covered role word -> character id (read-only), the uncovered role words in policy order, and the ids of crew
    with a stat not observed (ascending). Role words are non-empty text, ids positive ints; no id or word appears
    twice and no role is both covered and uncovered (else ValueError)."""

    roles: Mapping[str, int]
    uncovered: tuple[str, ...]
    stats_unknown: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", frozen_mapping(self.roles))
        object.__setattr__(self, "uncovered", tuple(self.uncovered))
        object.__setattr__(self, "stats_unknown", tuple(self.stats_unknown))
        words = [*self.roles, *self.uncovered]
        if not all(isinstance(word, str) and word for word in words):
            raise ValueError(f"role words must be non-empty text: {words!r}")
        ids = [*self.roles.values(), *self.stats_unknown]
        if not all(_whole(cid) and cid > 0 for cid in ids):
            raise ValueError(f"character ids must be positive ints: {ids!r}")
        if set(self.roles) & set(self.uncovered):
            raise ValueError(f"a role is both covered and uncovered: {sorted(set(self.roles) & set(self.uncovered))}")
        if len(set(words)) != len(words) or len(set(ids)) != len(ids):
            raise ValueError(f"role words and character ids must be distinct: {words!r}, {ids!r}")


def _assignment_choices(
    candidates: tuple[tuple[CrewObservation, ...], ...], chosen: tuple[CrewObservation | None, ...]
) -> Iterable[tuple[CrewObservation | None, ...]]:
    """Every way to give each remaining slot no crew or one of its candidates not chosen for an earlier slot."""
    if len(chosen) == len(candidates):
        yield chosen
        return
    used = {entry.character_id for entry in chosen if entry is not None}
    for option in (None, *candidates[len(chosen)]):
        if option is None or option.character_id not in used:
            yield from _assignment_choices(candidates, (*chosen, option))


def _assignment_key(roles: tuple[tuple[str, str], ...], choice: tuple[CrewObservation | None, ...]) -> tuple:
    """Smaller is better: more slots filled, a higher stat sum, higher scores on lower ids, earlier slots filled."""
    scored = [(entry.stats[stat], entry.character_id) for (_, stat), entry in zip(roles, choice) if entry is not None]
    return (
        -len(scored),
        -sum(score for score, _ in scored),
        sorted((-score, cid) for score, cid in scored),
        tuple(entry is None for entry in choice),
        tuple(entry.character_id for entry in choice if entry is not None),
    )


def assign_roles(crew: Sequence[CrewObservation], policy: CrewPolicy) -> RoleAssignment:
    """The best role assignment for ``crew`` (see the section comment above); deterministic in any input order."""
    known = [entry for entry in crew if entry.stats is not None]
    slots = len(policy.roles)
    candidates = tuple(
        tuple(sorted(known, key=lambda entry, stat=stat: (-entry.stats[stat], entry.character_id))[:slots])
        for _, stat in policy.roles
    )
    choice = min(_assignment_choices(candidates, ()), key=lambda chosen: _assignment_key(policy.roles, chosen))
    pairs = tuple(zip(policy.roles, choice))
    return RoleAssignment(
        frozen_mapping({word: entry.character_id for (word, _), entry in pairs if entry is not None}),
        tuple(word for (word, _), entry in pairs if entry is None),
        tuple(sorted(entry.character_id for entry in crew if entry.stats is None)),
    )


# Eligibility. ``crew_blocker`` checks the crew member and its room, ``course_blocker`` one course in that room; each
# returns a ``Blocker`` (skip code, detail, unknown) for the first failed check, or None. Every unknown fails closed;
# ``unknown`` is True when the check failed only because its data was not observed ("[u]" below), so the code names
# the check, not an observed failure.
#
# | Check (crew, in order) | Fails when                                                        | Code                 |
# |------------------------|-------------------------------------------------------------------|----------------------|
# | stats                  | [u] a stat was not observed                                       | stats_unknown        |
# | session                | running, or [u] unknown (the member may still be training)        | training_in_progress |
# |                        | ended but not claimed                                             | unclaimed_course     |
# | fatigue                | [u] unknown, or >= ``policy.max_fatigue``                         | fatigued             |
# | training points        | [u] the design or its capacity unknown, or the nine stats' sum >= it | tp_cap_reached    |
# | room                   | [u] no observed room or type, or not ``policy.training_room_type`` | not_in_training_room |
# | room status            | [u] unknown, or not ``policy.ready_status``                       | room_not_ready       |
# | room capacity          | [u] capacity or running sessions unknown, or running >= capacity  | room_capacity        |

Blocker = tuple[str, str, bool]


def crew_blocker(crew: CrewObservation, inputs: TrainingInputs, policy: CrewPolicy) -> Blocker | None:
    """Why ``crew`` cannot start any course now (see the table above), or None."""
    if crew.stats is None:
        return "stats_unknown", f"character {crew.character_id} has a stat that was not observed", True
    phase = crew.session.phase
    if phase in (SESSION_RUNNING, SESSION_UNKNOWN):
        return "training_in_progress", f"training session {phase}", phase == SESSION_UNKNOWN
    if phase == SESSION_ENDED_UNCLAIMED:
        return "unclaimed_course", f"course {crew.session.course_id} ended and was not claimed", False
    if crew.fatigue is None or crew.fatigue >= policy.max_fatigue:
        return "fatigued", f"fatigue {crew.fatigue}, limit {policy.max_fatigue}", crew.fatigue is None
    capacity = inputs.capacities.get(crew.design_id)
    total = sum(crew.stats.values())
    if capacity is None or total >= capacity:
        return "tp_cap_reached", f"{total} TP, design {crew.design_id} capacity {capacity}", capacity is None
    room = inputs.rooms.get(crew.room_id)
    if room is None or room.room_type != policy.training_room_type:
        return ("not_in_training_room", f"room {crew.room_id} is not a {policy.training_room_type} room",
                room is None or room.room_type is None)
    if room.status != policy.ready_status:
        return "room_not_ready", f"room {room.room_id} status {room.status}", room.status is None
    if room.capacity is None or room.running is None or room.running >= room.capacity:
        return ("room_capacity", f"room {room.room_id} runs {room.running} of {room.capacity} sessions",
                room.capacity is None or room.running is None)
    return None


# | Check (course, in order) | Fails when                                                          | Code                 |
# |--------------------------|---------------------------------------------------------------------|----------------------|
# | room type                | [u] requirement unknown; non-blank and [u] the room's name unknown, | not_in_training_room |
# |                          | or the name's type (``policy.room_type_of``, any case) not it       |                      |
# | room level (a minimum)   | [u] requirement unknown, or > 0 and the room's [u] unknown or lower | room_level           |
# | research                 | [u] requirement unknown, or non-zero and [u] research unobserved    | research_unknown     |
# |                          | non-zero and not ``policy.completed_research``                      | research_missing     |
# | price                    | unavailable                                                         | price_unavailable    |
# |                          | malformed                                                           | price_malformed      |
# |                          | premium (Starbux, never spent)                                      | premium_currency     |
# | funds                    | non-zero and above what its currency has spendable, or [u] unknown  | insufficient_funds   |
# | spend cap                | non-zero and above what the currency's cap leaves                   | cap_exceeded         |

_PRICE_SKIP_CODES: Mapping[str, str] = frozen_mapping(
    {PRICE_UNAVAILABLE: "price_unavailable", PRICE_MALFORMED: "price_malformed", PRICE_PREMIUM: "premium_currency"}
)


def course_blocker(
    course: CourseObservation, room: RoomObservation, inputs: TrainingInputs, policy: CrewPolicy
) -> Blocker | None:
    """Why ``course`` cannot start in ``room`` now (see the table above), or None. A price of 0 always fits."""
    required_room = course.required_room_name
    if required_room is None:
        return "not_in_training_room", f"course {course.design_id} room requirement not observed", True
    if required_room and policy.room_type_of(room.design_name) != required_room.casefold():
        return ("not_in_training_room",
                f"course {course.design_id} needs a {required_room} room, room {room.room_id} is a {room.design_name}",
                room.design_name is None)
    level = course.required_room_level
    if level is None or (level and (room.level is None or room.level < level)):
        return ("room_level", f"course {course.design_id} needs room level {level}, room level {room.level}",
                level is None or room.level is None)
    research = course.required_research_design_id
    if research is None or (research and inputs.research is None):
        return "research_unknown", f"course {course.design_id} research {research} not observed", True
    if research and inputs.research.get(research) != policy.completed_research:
        return ("research_missing", f"course {course.design_id} needs research {research} {policy.completed_research}",
                False)
    price = course.price
    if price.status != PRICE_OK:
        return _PRICE_SKIP_CODES[price.status], f"course {course.design_id} price {price.status}", False
    amount, currency = price.amount, price.currency
    spendable = inputs.funds.get(currency)
    if amount and (spendable is None or amount > spendable):
        return ("insufficient_funds", f"course {course.design_id} costs {amount} {currency}, {spendable} spendable",
                spendable is None)
    cap = inputs.spend_caps.get(currency)
    if amount and cap is not None and amount > cap:
        return ("cap_exceeded", f"course {course.design_id} costs {amount} {currency}, {cap} left under the cap",
                False)
    return None


def choose_course(
    stat: str, room: RoomObservation, inputs: TrainingInputs, policy: CrewPolicy
) -> CourseObservation | Blocker:
    """The lowest-id observed course that trains ``stat`` and can start in ``room`` now.

    Else the lowest-id such course's blocker, or ``design_missing`` when no observed course trains ``stat`` (unknown
    when the courses were not observed). Rank is not used: the planner trains one stat per role.
    """
    if inputs.courses is None:
        return "design_missing", f"{stat} courses: course designs not observed", True
    candidates = sorted((entry for entry in inputs.courses if entry.stat == stat), key=lambda entry: entry.design_id)
    if not candidates:
        return "design_missing", f"no observed course trains {stat}", False
    for candidate in candidates:
        if course_blocker(candidate, room, inputs, policy) is None:
            return candidate
    return course_blocker(candidates[0], room, inputs, policy)


@dataclass(frozen=True)
class CrewDecision:
    """One line of the crew plan: a role (None for a crew whose stats are unknown), its crew (None when the role is
    uncovered) and either the course to start or a skip code from ``CREW_SKIP_CODES``, never both. ``unknown`` is the
    skip's blocker label: True when the check failed because its data was not observed (only with a skip code)."""

    role: str | None
    character_id: int | None
    course: CourseObservation | None
    code: str | None
    detail: str
    unknown: bool = False

    def __post_init__(self) -> None:
        if (self.course is None) == (self.code is None):
            raise ValueError(f"a crew decision needs exactly one of a course and a skip code: {self!r}")
        if self.code is not None and self.code not in CREW_SKIP_CODES:
            raise ValueError(f"unknown crew skip code: {self.code!r}")
        if not isinstance(self.unknown, bool):
            raise ValueError(f"unknown must be a bool: {self.unknown!r}")
        if self.unknown and self.code is None:
            raise ValueError("only a skip can be unknown")


@dataclass(frozen=True)
class CrewPlan:
    """The role assignment and the decisions: one per role in policy order, then one per stats-unknown crew (by id).
    At most one decision starts a course."""

    assignment: RoleAssignment
    decisions: tuple[CrewDecision, ...]

    def __post_init__(self) -> None:
        if len([entry for entry in self.decisions if entry.course is not None]) > 1:
            raise ValueError("a crew plan has at most one start")

    @property
    def start(self) -> CrewDecision | None:
        """The decision that starts a course, or None."""
        return next((entry for entry in self.decisions if entry.course is not None), None)


def plan_crew(inputs: TrainingInputs, policy: CrewPolicy = DEFAULT_CREW_POLICY) -> CrewPlan:
    """Assign roles, then give each role's crew its course or the first reason it cannot start (S14-R1, S14-R2).

    Only the first eligible role (policy order) starts: one training start per account per run, and only while
    ``inputs.action_slots`` is None or positive; any later eligible role reports ``action_cap_reached``.
    """
    assignment = assign_roles(inputs.crew, policy)
    crew_by_id = {entry.character_id: entry for entry in inputs.crew}
    decisions: list[CrewDecision] = []
    started = False
    for word, stat in policy.roles:
        cid = assignment.roles.get(word)
        if cid is None:
            decisions.append(CrewDecision(word, None, None, "role_uncovered", f"no observed crew for the {stat} slot"))
            continue
        crew = crew_by_id[cid]
        outcome = crew_blocker(crew, inputs, policy) or choose_course(stat, inputs.rooms[crew.room_id], inputs, policy)
        if isinstance(outcome, tuple):
            decisions.append(CrewDecision(word, cid, None, *outcome))
        elif started:
            decisions.append(CrewDecision(word, cid, None, "action_cap_reached",
                                          f"course {outcome.design_id} waits: one training start per account per run"))
        elif inputs.action_slots is not None and inputs.action_slots < 1:
            decisions.append(CrewDecision(word, cid, None, "action_cap_reached",
                                          f"course {outcome.design_id} waits: {inputs.action_slots} requests left "
                                          "under max_requests"))
        else:
            started = True
            decisions.append(CrewDecision(word, cid, outcome, None, f"course {outcome.design_id} {outcome.name}"))
    decisions.extend(
        CrewDecision(None, cid, None, "stats_unknown", f"character {cid} has a stat that was not observed", True)
        for cid in assignment.stats_unknown
    )
    return CrewPlan(assignment, tuple(decisions))
