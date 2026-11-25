"""Pure, total, deterministic crew-training planner.

``plan_training(ctx, strategy)`` folds ``_plan_crew`` over ``ctx.crew`` in order. Each crew gets exactly one primary
decision (StartTraining | UseConsumable | Skip), optionally preceded by a FinishTraining. ``_plan_crew`` sees only
its crew, the context and the strategy, so one crew can never leak state (room, design, stock) into another.

Decision precedence per crew:
 1. training finished but not collected (design id set and end <= now) -> FinishTraining first, then continue
 2. name not in any role -> Skip unknown_role
 3. room (own room name, else ``ctx.rooms[room_id]``) not a training room -> Skip not_in_training_room
 4. capacity <= 0 -> Skip design_missing ("no training capacity"); total >= TP cap -> Skip tp_cap_reached
    (detail in training points: "<total> TP >= cap <cap> TP (<pct>% of capacity <capacity>)")
 5. training in progress (end > now) -> consumable fallback, else Skip training_in_progress
 6. percent >= complete_percent -> consumable fallback, else Skip complete
 7. band's room type differs from the crew's room type -> Skip not_in_training_room ("move to <type>");
    no consumable here: the crew should move instead
 8. band research gate unmet -> research_missing; fatigue >= limit -> fatigued (consumable fallback for both)
 9. no eligible design (exact name, room type, lowest id) -> design_missing; design's own research missing ->
    research_missing (consumable fallback for both)
10. StartTraining

Consumables are a fallback only: never instead of a startable training, never at/above the TP cap, never for an
unknown role or outside a training room, and only when the strategy maps an item that is in stock.

Notes:
- Percent is the exact integer ceiling of ``total * 100 / training_capacity`` (the original float ``math.ceil`` was
  one too high for e.g. 7/100 and 55/100).
- After a FinishTraining the primary decision is based on pre-finish stats. The executor should rebuild that crew
  from the FinishTraining response and re-plan it with ``plan_training(replace(ctx, crew=(crew,)))``.
- The same character id appearing twice in ``ctx.crew`` is planned twice, independently, in order.
- ``TrainingDesign.tier`` is informational; the role table's training name is the authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from sdk.crew_leveling import CAPTAIN_TP, get_tp_cap
from sdk.training.model import (
    CANONICAL_STATS,
    CrewState,
    SKIP_COMPLETE,
    SKIP_DESIGN_MISSING,
    SKIP_FATIGUED,
    SKIP_NOT_IN_TRAINING_ROOM,
    SKIP_RESEARCH_MISSING,
    SKIP_TP_CAP_REACHED,
    SKIP_TRAINING_IN_PROGRESS,
    SKIP_UNKNOWN_ROLE,
    Decision,
    FinishTraining,
    Skip,
    StartTraining,
    TrainingContext,
    TrainingDesign,
    UseConsumable,
)
from sdk.training.strategy import (
    DEFAULT_STRATEGY,
    STAT_CODES,
    ConsumableRule,
    Role,
    TierBand,
    TrainingStrategy,
)

TRAINING_IDLE = "idle"
TRAINING_IN_PROGRESS = "in_progress"
TRAINING_FINISHED = "finished"


def percent(total: int, capacity: int) -> int:
    """Training progress as an exact integer ceiling of ``total * 100 / capacity``."""
    if capacity <= 0:
        raise ValueError(f"training capacity must be positive, got {capacity}")
    return -(-total * 100 // capacity)


def band_for(pct: int, strategy: TrainingStrategy = DEFAULT_STRATEGY) -> TierBand | None:
    """Return the (slot, tier) band for ``pct``; None once training is complete.

    Percentages below the lowest band (only possible with negative stats) clamp to the lowest band.
    """
    if pct >= strategy.complete_percent:
        return None
    ordered = sorted(strategy.bands, key=lambda band: band.low)
    for band in ordered:
        if pct <= band.high:
            return band
    return None


def room_type_for(room_name: str | None, strategy: TrainingStrategy = DEFAULT_STRATEGY) -> str | None:
    """Canonical room type for an exact (whitespace-stripped, case-sensitive) room name, else None."""
    if not room_name:
        return None
    return strategy.room_types.get(room_name.strip())


def resolve_room_name(crew: CrewState, ctx: TrainingContext) -> str | None:
    """The crew's own room name wins; otherwise look its room id up in ``ctx.rooms``. Never borrows another crew's."""
    if crew.room_name:
        return crew.room_name
    if crew.room_id is None:
        return None
    return ctx.rooms.get(crew.room_id)


def role_for(name: str, strategy: TrainingStrategy = DEFAULT_STRATEGY) -> Role | None:
    """Role whose character set contains ``name`` exactly (strategy validation keeps it unique)."""
    for role in strategy.roles:
        if name in role.characters:
            return role
    return None


def effective_cap(crew: CrewState, strategy: TrainingStrategy = DEFAULT_STRATEGY) -> int:
    """Training-point cap: the design's capacity, further limited by the crew_leveling TP table when it applies.

    A 6-star crew whose design capacity reaches ``CAPTAIN_TP`` is treated as a captain, so the table allows 200 TP
    instead of clamping it to the standard 6-star cap.
    """
    cap = crew.training_capacity
    stars = strategy.rarity_stars.get(crew.rarity, 0)
    is_captain = stars == 6 and cap >= CAPTAIN_TP
    table_cap = get_tp_cap(stars, crew.name, is_captain=is_captain) if stars else 0
    if table_cap > 0:
        cap = min(cap, table_cap)
    return cap


def research_ok(
    band: TierBand, research_levels: Mapping[str, int], strategy: TrainingStrategy = DEFAULT_STRATEGY
) -> bool:
    """True when any gate alternative for the band is met (exact research-name keys); empty gate = ungated."""
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


def highest_stat(stats: Mapping[str, int]) -> str:
    """The crew's highest stat; ties (including all-zero) resolve to the earliest in CANONICAL_STATS."""
    best = CANONICAL_STATS[0]
    for stat in CANONICAL_STATS[1:]:
        if stats.get(stat, 0) > stats.get(best, 0):
            best = stat
    return best


def pick_consumable(
    crew: CrewState, pct: int, ctx: TrainingContext, strategy: TrainingStrategy = DEFAULT_STRATEGY
) -> UseConsumable | None:
    """First consumable rule matching (pct, fatigue) whose item for the crew's highest stat is in stock.

    The caller decides *whether* a consumable may be considered (fallback only); this only checks rules and stock.
    """
    code = STAT_CODES[highest_stat(crew.stats)]
    for rule in strategy.consumable_rules:
        if not _rule_matches(rule, pct, crew.fatigue):
            continue
        item_design_id = strategy.consumables.get((code, rule.tier))
        if item_design_id is not None and ctx.items.get(item_design_id, 0) > 0:
            reason = f"{rule.tier} {code} consumable at {pct}% with fatigue {crew.fatigue}"
            return UseConsumable(crew.character_id, item_design_id, reason)
    return None


def _within(value: int, low: int, high: int | None) -> bool:
    return value >= low and (high is None or value <= high)


def _rule_matches(rule: ConsumableRule, pct: int, fatigue: int) -> bool:
    return _within(pct, rule.min_percent, rule.max_percent) and _within(fatigue, rule.min_fatigue, rule.max_fatigue)


def training_state(crew: CrewState, now: datetime) -> str:
    """In progress iff a design id is set and the end lies in the future; finished iff it ended at or before now."""
    if not crew.training_design_id or crew.training_end is None:
        return TRAINING_IDLE
    return TRAINING_IN_PROGRESS if crew.training_end > now else TRAINING_FINISHED


def total_points(stats: Mapping[str, int]) -> int:
    """Training points spent: the sum of the nine canonical stats (missing = 0)."""
    return sum(stats.get(stat, 0) for stat in CANONICAL_STATS)


def plan_training(ctx: TrainingContext, strategy: TrainingStrategy = DEFAULT_STRATEGY) -> tuple[Decision, ...]:
    """Plan every crew in ``ctx.crew`` order (see the module docstring for the per-crew precedence)."""
    decisions: list[Decision] = []
    for crew in ctx.crew:
        decisions.extend(_plan_crew(crew, ctx, strategy))
    return tuple(decisions)


def _plan_crew(crew: CrewState, ctx: TrainingContext, strategy: TrainingStrategy) -> tuple[Decision, ...]:
    """All decisions for one crew: an optional FinishTraining, then exactly one primary decision."""
    primary = _primary_decision(crew, ctx, strategy)
    if training_state(crew, ctx.now) == TRAINING_FINISHED:
        finish = FinishTraining(crew.character_id, f"training ended at {crew.training_end}")
        return (finish, primary)
    return (primary,)


def _consumable_or(skip: Skip, crew: CrewState, pct: int, ctx: TrainingContext, strategy: TrainingStrategy) -> Decision:
    """Consumable fallback: only reached when no training can start for an eligible crew."""
    return pick_consumable(crew, pct, ctx, strategy) or skip


def _primary_decision(crew: CrewState, ctx: TrainingContext, strategy: TrainingStrategy) -> Decision:
    """Steps 2-10 of the module-level precedence."""
    cid = crew.character_id
    role = role_for(crew.name, strategy)
    if role is None:
        return Skip(cid, SKIP_UNKNOWN_ROLE, f"{crew.name!r} has no training role")
    room_name = resolve_room_name(crew, ctx)
    room_type = room_type_for(room_name, strategy)
    if room_type is None:
        return Skip(cid, SKIP_NOT_IN_TRAINING_ROOM, f"room {room_name!r} is not a training room")
    if crew.training_capacity <= 0:
        return Skip(cid, SKIP_DESIGN_MISSING, "no training capacity")
    total = total_points(crew.stats)
    pct = percent(total, crew.training_capacity)
    cap = effective_cap(crew, strategy)
    if total >= cap:
        return Skip(cid, SKIP_TP_CAP_REACHED, f"{total} TP >= cap {cap} TP ({pct}% of capacity {crew.training_capacity})")
    if training_state(crew, ctx.now) == TRAINING_IN_PROGRESS:
        skip = Skip(cid, SKIP_TRAINING_IN_PROGRESS, f"training ends at {crew.training_end}")
        return _consumable_or(skip, crew, pct, ctx, strategy)
    band = band_for(pct, strategy)
    if band is None:
        skip = Skip(cid, SKIP_COMPLETE, f"{pct}% >= {strategy.complete_percent}%: training complete")
        return _consumable_or(skip, crew, pct, ctx, strategy)
    slot = role.slot(band.slot)
    if room_type != slot.room_type:
        return Skip(cid, SKIP_NOT_IN_TRAINING_ROOM, f"move to {slot.room_type}")
    if not research_ok(band, ctx.research_levels, strategy):
        skip = Skip(cid, SKIP_RESEARCH_MISSING, f"{band.slot} T{band.tier} research gate not met")
        return _consumable_or(skip, crew, pct, ctx, strategy)
    if crew.fatigue >= strategy.max_training_fatigue:
        skip = Skip(cid, SKIP_FATIGUED, f"fatigue {crew.fatigue} >= {strategy.max_training_fatigue}")
        return _consumable_or(skip, crew, pct, ctx, strategy)
    name = slot.trainings[band.tier - 1]
    design = pick_design(name, room_type, ctx.designs)
    if design is None:
        skip = Skip(cid, SKIP_DESIGN_MISSING, f"no design named {name} for {room_type}")
        return _consumable_or(skip, crew, pct, ctx, strategy)
    if not design_research_ok(design, ctx.research_levels):
        skip = Skip(cid, SKIP_RESEARCH_MISSING, f"{name} requires research {design.required_research}")
        return _consumable_or(skip, crew, pct, ctx, strategy)
    reason = f"{band.slot} T{band.tier} {name} at {pct}% in {room_type}"
    return StartTraining(cid, design.id, reason)
