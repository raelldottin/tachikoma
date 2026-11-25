"""Pure, total, deterministic crew-training planner.

``plan_training(ctx, strategy)`` plans ``ctx.crew`` in order. Each crew gets exactly one primary decision
(StartTraining | UseConsumable | Skip), optionally preceded by a FinishTraining. A crew's decision depends only on
that crew, the context and the strategy, with one deliberate exception: consumable stock is shared. Each
UseConsumable reserves one unit, so later crew see the reduced stock and the plan never uses an item more often
than ``ctx.items`` holds it (crew order decides who gets a scarce item). Room and design choices never leak between
crew.

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
unknown role or outside a training room, and only when the strategy maps an item that is still in stock.

After a FinishTraining:
- The primary decision that follows a FinishTraining is advisory: it is based on the pre-finish stats, so it may
  name the wrong tier. The executor must not run it as-is. It rebuilds that crew from the FinishTraining response
  and replaces the advisory decision with ``replan_after_finish(ctx, rebuilt_crew, strategy)``, passing a ``ctx``
  whose ``items`` reflect what earlier decisions already used.

Notes:
- Percent is the exact integer ceiling of ``total * 100 / training_capacity`` (the original float ``math.ceil`` was
  one too high for e.g. 7/100 and 55/100).
- The same character id appearing twice in ``ctx.crew`` is planned twice, in order.
- ``TrainingDesign.tier`` is informational; the role table's training name is the authority.
- The helpers below are public, supported API (see ``__all__``). Every helper that reads strategy data takes the
  strategy as a required argument; only the entry points default to ``DEFAULT_STRATEGY``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from sdk.training.model import (
    CANONICAL_STATS,
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

__all__ = (
    "band_for",
    "design_research_ok",
    "effective_cap",
    "highest_stat",
    "percent",
    "pick_consumable",
    "pick_design",
    "plan_training",
    "replan_after_finish",
    "research_ok",
    "resolve_room_name",
    "role_for",
    "room_type_for",
    "total_points",
    "training_state",
)

TRAINING_IDLE = "idle"
TRAINING_IN_PROGRESS = "in_progress"
TRAINING_FINISHED = "finished"


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
    """The crew's own room name wins; otherwise look its room id up in ``ctx.rooms``. Never borrows another crew's."""
    if crew.room_name:
        return crew.room_name
    if crew.room_id is None:
        return None
    return ctx.rooms.get(crew.room_id)


def role_for(name: str, strategy: TrainingStrategy) -> Role | None:
    """Role whose character set contains ``name`` exactly (strategy validation keeps it unique)."""
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
    crew: CrewState, pct: int, stock: Mapping[int, int], strategy: TrainingStrategy
) -> UseConsumable | None:
    """First consumable rule matching (pct, fatigue) whose item for the crew's highest stat is in ``stock``.

    The caller decides *whether* a consumable may be considered (fallback only) and owns the stock bookkeeping;
    this only checks rules and the stock it is given.
    """
    code = STAT_CODES[highest_stat(crew.stats)]
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
    """In progress iff a design id is set and the end lies in the future; finished iff it ended at or before now."""
    if not crew.training_design_id or crew.training_end is None:
        return TRAINING_IDLE
    return TRAINING_IN_PROGRESS if crew.training_end > now else TRAINING_FINISHED


def total_points(stats: Mapping[str, int]) -> int:
    """Training points spent: the sum of the nine canonical stats (missing = 0)."""
    return sum(stats.get(stat, 0) for stat in CANONICAL_STATS)


def plan_training(ctx: TrainingContext, strategy: TrainingStrategy = DEFAULT_STRATEGY) -> tuple[Decision, ...]:
    """Plan every crew in ``ctx.crew`` order (see the module docstring for the per-crew precedence and stock)."""
    stock = dict(ctx.items)
    decisions: list[Decision] = []
    for crew in ctx.crew:
        primary = _primary_decision(crew, ctx, strategy, stock)
        if isinstance(primary, UseConsumable):
            stock[primary.item_design_id] -= 1
        if training_state(crew, ctx.now) == TRAINING_FINISHED:
            decisions.append(FinishTraining(crew.character_id, f"training ended at {crew.training_end}"))
        decisions.append(primary)
    return tuple(decisions)


def replan_after_finish(
    ctx: TrainingContext, crew: CrewState, strategy: TrainingStrategy = DEFAULT_STRATEGY
) -> tuple[Decision, ...]:
    """The decision that replaces the advisory primary after a FinishTraining for ``crew``.

    ``crew`` is the crew rebuilt from the FinishTraining response (fresh stats). Its training fields are cleared
    before planning, so a response that still carries the finished training's id and end date can never produce
    another FinishTraining (no finish loop). Only ``crew`` is planned; ``ctx`` supplies everything else, so pass
    one whose ``items`` already reflect earlier decisions. Returns exactly one primary decision.
    """
    idle = replace(crew, training_design_id=None, training_end=None)
    return plan_training(replace(ctx, crew=(idle,)), strategy)


def _primary_decision(
    crew: CrewState, ctx: TrainingContext, strategy: TrainingStrategy, stock: Mapping[int, int]
) -> Decision:
    """Steps 2-10 of the module-level precedence; ``stock`` is what earlier crew left of ``ctx.items``."""
    cid = crew.character_id

    def consumable_or(skip: Skip, pct: int) -> Decision:
        return pick_consumable(crew, pct, stock, strategy) or skip

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
    if training_state(crew, ctx.now) == TRAINING_IN_PROGRESS:
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
    design = pick_design(name, room_type, ctx.designs)
    if design is None:
        return consumable_or(Skip(cid, SKIP_DESIGN_MISSING, f"no design named {name} for {room_type}"), pct)
    if not design_research_ok(design, ctx.research_levels):
        gated = f"{name} requires research {design.required_research}"
        return consumable_or(Skip(cid, SKIP_RESEARCH_MISSING, gated), pct)
    return StartTraining(cid, design.id, f"{band.slot} T{band.tier} {name} at {pct}% in {room_type}")
