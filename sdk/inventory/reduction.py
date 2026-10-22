from __future__ import annotations

"""Task G: deterministic, fail-closed, DRY-RUN surplus/reduction planning.

Read-only. Builds on Task D (classification/protection) and Task F
(reservation ledger) and answers, per ItemId stack: "if reduction were
ever authorized, how much of this stack COULD be considered?" Every
result is informational only: ``actionable`` is False on every row, by
construction, and no executor, reconciler, endpoint enablement, or
network I/O exists anywhere in this module.

ARITHMETIC (per ItemDesignId, then per stack):

- observed     = sum of non-negative stack quantities of the design
                 (negative rows are malformed supply: candidate=0 on
                 the row that carries them, flagged, never summed in);
- total_reserved = ledger.total_reserved(design) (Task F allocation is
                 structurally <= observed; over-reserved ledgers can
                 only be hand-built adversarially and MUST fail closed);
- unreserved   = max(0, observed - total_reserved)
  (unreserved is an arithmetic remainder ONLY. It does NOT mean the
  units are reducible, sellable, consumable or disposable — every
  gating rule below runs after the subtraction);
- candidate    = 0..unreserved for eligible stacks and 0 for every
  blocked one, with the invariant 0 <= candidate <= unreserved
  <= observed enforced (never trusted from inputs). Candidates
  allocate against a per-design unreserved POOL in ascending ItemId
  order (deterministic), so the per-design SUM of candidates can
  never exceed the design's unreserved (no over-counting across
  stacks of one design). A stack whose pool share was consumed by
  lower-ItemId stacks is HOLD with BLOCKED_ALLOCATION_EXHAUSTED.

BLOCKING RULES (candidate=0, with a reason code on every blocked row):

- PROTECTED or UNKNOWN classification (Task D fail-closed surface);
- malformed stack row (negative quantity) — flagged MALFORMED_QUANTITY,
  counted as zero supply;
- over-reserved ledger (total_reserved > observed for the design):
  the design is OVER_RESERVED, every stack of the design gets
  candidate=0 (never a negative unreserved, never a partial trust of
  an impossible ledger);
- design-scoped unresolved-demand conditions (UNKNOWN_DESIGN,
  MALFORMED_QUANTITY, UNREASONABLE_QUANTITY, DEMAND_EXCEEDS_SUPPLY,
  DUPLICATE_CONFIG_ENTRY, DUPLICATE_INGREDIENT, RECIPE_CYCLE,
  CRAFT_OBJECTIVE_NOT_CRAFTABLE, SOURCE_UNAVAILABLE) — uncertainty
  blocks only the designs it is scoped to;
- APPLICABLE unsupported demand: the design carries the
  CREW_TRAINING_MATERIAL or AMMUNITION_MATERIAL role while the
  TRAINING/AMMUNITION sources are unsupported (always, this slice).
  Role presence + no support representation = unresolved demand for
  THAT design only (e.g. Half Day Immunity strategic-role stacks are
  not blocked by this rule; design 90 is decided under
  STRATEGIC_RESERVE_CANDIDATE below);
- STRATEGIC_RESERVE_CANDIDATE role: a strategic-intent role with no
  config-backed reservation is unresolved strategic intent — candidate=0
  with a KEEP/RESERVE hint (decision tested both ways);
- shared/multi-reference stacks (EQUIPMENT_SHARED_STACK,
  MODULE_MULTI_ATTACH, BOTH_EQUIPPED_AND_ROOM_ATTACHED): these stack
  conditions also block the affected stacks (per-unit split is
  unobservable — the same fail-closed direction as Task F).

Note: the design-level TRAINING/AMMUNITION SOURCE_UNSUPPORTED
conditions are emitted globally by Task F on EVERY plan (the sources
have no representation for any design); the applicability test is the
role match above, never the bare condition.

EVIDENCE (weakest-input, never promoted): a row's evidence is the
minimum across the feed rows (CAPTURED_NOT_VERIFIED), the design's
ledger evidence, and the Task D classification evidence. No row can
reach VERIFIED while ListItemsOfAShip is uncaptured (evidence lane
t_f4dd7bad: 0 captures in 50,755 records).

DISPOSITION HINTS: informational ONLY. Hints are drawn from the fixed
future order KEEP -> USE -> CRAFT -> ALLOCATE -> TRANSFER -> SELL ->
CONSUME -> DELETE; TRANSFER/SELL/CONSUME/DELETE are deferred and have
NO enum member this slice (constructing them is impossible). Blocked
rows hint KEEP; eligible rows hint ALLOCATE (equipment/module roles),
CRAFT (crafting roles) or USE (default). Strategic-role rows are HOLD
rows (unresolved intent) and hint KEEP with the strategic reason
carrying the RESERVE intent.

DETERMINISM: identical inputs produce identical results. Rows sort by
ascending ItemId; every list is sorted before construction; no
wall-clock, randomness, dict-order or set-order dependence. Pure: no
network I/O, no filesystem access, no mutation of inputs, no Client
construction, no account identity anywhere (multi-account planning is
out of scope by design).
"""

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from ._types import ClassifiedItem, Disposition, Evidence, Role
from .demand import ReservationLedger
from .demand import (
    BOTH_EQUIPPED_AND_ROOM_ATTACHED,
    CRAFT_OBJECTIVE_NOT_CRAFTABLE,
    DEMAND_EXCEEDS_SUPPLY,
    DUPLICATE_CONFIG_ENTRY,
    DUPLICATE_INGREDIENT,
    EQUIPMENT_SHARED_STACK,
    MALFORMED_QUANTITY,
    MODULE_MULTI_ATTACH,
    RECIPE_CYCLE,
    SOURCE_UNAVAILABLE,
    UNKNOWN_DESIGN,
    UNREASONABLE_QUANTITY,
)

# Reason codes — stable strings, part of the deterministic output contract.
BLOCKED_PROTECTED_OR_UNKNOWN = "BLOCKED_PROTECTED_OR_UNKNOWN"
BLOCKED_MALFORMED_ROW = "BLOCKED_MALFORMED_ROW"
BLOCKED_OVER_RESERVED = "BLOCKED_OVER_RESERVED"
BLOCKED_DESIGN_CONDITION = "BLOCKED_DESIGN_CONDITION"
BLOCKED_STACK_CONDITION = "BLOCKED_STACK_CONDITION"
BLOCKED_UNSUPPORTED_DEMAND = "BLOCKED_UNSUPPORTED_DEMAND"
BLOCKED_STRATEGIC_ROLE = "BLOCKED_STRATEGIC_ROLE"
BLOCKED_ZERO_UNRESERVED = "BLOCKED_ZERO_UNRESERVED"
BLOCKED_ZERO_QUANTITY = "BLOCKED_ZERO_QUANTITY"
BLOCKED_ALLOCATION_EXHAUSTED = "BLOCKED_ALLOCATION_EXHAUSTED"

# Design-scoped ledger condition codes that block a design's candidates.
_DESIGN_BLOCKING_CONDITIONS = frozenset(
    {
        UNKNOWN_DESIGN,
        MALFORMED_QUANTITY,
        UNREASONABLE_QUANTITY,
        DEMAND_EXCEEDS_SUPPLY,
        DUPLICATE_CONFIG_ENTRY,
        DUPLICATE_INGREDIENT,
        RECIPE_CYCLE,
        CRAFT_OBJECTIVE_NOT_CRAFTABLE,
        SOURCE_UNAVAILABLE,
    }
)

# Stack-scoped ledger condition codes that block the referenced stacks.
_STACK_BLOCKING_CONDITIONS = frozenset(
    {
        EQUIPMENT_SHARED_STACK,
        MODULE_MULTI_ATTACH,
        BOTH_EQUIPPED_AND_ROOM_ATTACHED,
    }
)

# Roles whose demand source is unsupported this slice: role presence
# means unresolved demand APPLIES to the design.
_APPLICABLE_UNSUPPORTED_ROLES = (
    Role.CREW_TRAINING_MATERIAL,
    Role.AMMUNITION_MATERIAL,
)

# Feed-derived facts never rise above this tier while ListItemsOfAShip
# is uncaptured (evidence lane t_f4dd7bad).
_EVIDENCE_FEED_ROWS = Evidence.CAPTURED_NOT_VERIFIED

# Disposition hints allowed to be assigned this slice. TRANSFER, SELL,
# CONSUME and DELETE are deferred (no marketplace/transfer/mutation
# planning exists) and have NO enum member — they cannot be assigned.
_ASSIGNABLE_DISPOSITIONS = frozenset(
    {
        Disposition.KEEP,
        Disposition.USE,
        Disposition.CRAFT,
        Disposition.ALLOCATE,
    }
)


@dataclass(frozen=True)
class ReductionResult:
    """One dry-run row per inventory stack. Informational only.

    ``actionable`` is ALWAYS False: no row authorizes, recommends or
    enables any mutation. ``candidate_quantity`` is a bounded arithmetic
    observation (0 <= candidate <= unreserved <= observed), never a
    command.
    """

    item_id: int
    item_design_id: int
    observed_quantity: int
    total_reserved: int
    unreserved: int
    candidate_quantity: int
    state: str
    reasons: Tuple[str, ...]
    evidence: Evidence
    disposition_hint: Disposition
    actionable: bool = False

    def to_dict(self) -> Dict[str, object]:
        """Deterministic mapping view (for plan rendering and tests)."""
        return {
            "item_id": self.item_id,
            "item_design_id": self.item_design_id,
            "observed_quantity": self.observed_quantity,
            "total_reserved": self.total_reserved,
            "unreserved": self.unreserved,
            "candidate_quantity": self.candidate_quantity,
            "state": self.state,
            "reasons": list(self.reasons),
            "evidence": self.evidence.value,
            "disposition_hint": self.disposition_hint.value,
            "actionable": self.actionable,
        }


# Row states — stable strings, part of the output contract.
STATE_HOLD = "HOLD"
STATE_CANDIDATE = "CANDIDATE"


@dataclass(frozen=True)
class ReductionPlan:
    """Deterministic Task G output: one row per stack, ascending ItemId.

    Carries no executor, no reconciler, no action object and no
    account dimension. Every row has actionable=False.
    """

    rows: Tuple[ReductionResult, ...] = ()

    @property
    def actionable_rows(self) -> Tuple[ReductionResult, ...]:
        """Always empty — fail-closed proof surface for tests."""
        return ()

    def rows_for_design(self, design_id: int) -> Tuple[ReductionResult, ...]:
        return tuple(r for r in self.rows if r.item_design_id == int(design_id))

    def total_candidates(self) -> int:
        """Sum of candidate quantities; bounded by total unreserved."""
        return sum(r.candidate_quantity for r in self.rows)

    def to_dicts(self) -> List[Dict[str, object]]:
        return [r.to_dict() for r in self.rows]


def _stack_item_ids_for_conditions(
    ledger: ReservationLedger, codes: Tuple[str, ...]
) -> Dict[int, Tuple[str, ...]]:
    """Map ItemId -> (sorted condition codes) for stack-scoped conditions.

    Task F stack-scoped conditions name the ItemId inside their detail
    text (``"ItemId 123 is referenced ..."``). Parsing the first
    ``ItemId <n>`` token recovers the stack identity deterministically;
    codes are grouped and sorted for stable output.
    """
    mapping: Dict[int, List[str]] = {}
    for code in codes:
        for condition in ledger.conditions:
            if condition.code != code:
                continue
            detail = condition.detail
            marker = "ItemId "
            index = detail.find(marker)
            if index < 0:
                continue
            remainder = detail[index + len(marker) :]
            token = ""
            for char in remainder:
                if char.isdigit():
                    token += char
                else:
                    break
            if not token:
                continue
            item_id = int(token)
            mapping.setdefault(item_id, []).append(code)
    return {k: tuple(sorted(v)) for k, v in sorted(mapping.items())}


class ReductionPlanner:
    """Deterministic, pure, fail-closed dry-run planner.

    Inputs: Task D classification (protection surface) + Task F
    reservation ledger (observed/reserved/conditions). Output: one
    HOLD/CANDIDATE row per stack. No I/O, no mutation, no config/env
    bypass surface (nothing here can enable anything).
    """

    def plan(
        self,
        classified: Sequence[ClassifiedItem],
        ledger: ReservationLedger,
    ) -> ReductionPlan:
        rows: List[ReductionResult] = []
        # Design-level blocking facts, computed once per design id.
        design_blocking: Dict[int, Tuple[str, ...]] = {}
        for design_id in ledger.design_ids():
            codes = tuple(
                sorted(
                    {
                        c.code
                        for c in ledger.conditions_for(design_id)
                        if c.code in _DESIGN_BLOCKING_CONDITIONS
                    }
                )
            )
            if codes:
                design_blocking[design_id] = codes

        # Stack-level blocking facts (shared/multi-attach/overlap).
        stack_blocking = _stack_item_ids_for_conditions(
            ledger,
            (
                EQUIPMENT_SHARED_STACK,
                MODULE_MULTI_ATTACH,
                BOTH_EQUIPPED_AND_ROOM_ATTACHED,
            ),
        )

        # Per-design arithmetic cache (deterministic; the enforced
        # ascending-ItemId iteration above drives both row order and
        # pool allocation; caches only avoid recomputation) plus the
        # per-design unreserved allocation pool.
        observed_cache: Dict[int, int] = {}
        unreserved_cache: Dict[int, int] = {}
        reserved_cache: Dict[int, int] = {}
        over_reserved_cache: Dict[int, bool] = {}
        design_pools: Dict[int, int] = {}

        # Deterministic allocation order is ENFORCED here, never trusted
        # from the caller's sequence: the documented contract allocates
        # pool shares in ascending ItemId order, so any input ordering
        # (the catalog normalizes today, but the planner's contract is
        # its own) yields byte-identical output.
        ordered = sorted(classified, key=lambda ci: ci.item.item_id)

        for ci in ordered:
            item = ci.item
            design_id = item.item_design_id

            if design_id not in observed_cache:
                observed = ledger.observed_quantity(design_id)
                total_reserved = ledger.total_reserved(design_id)
                observed_cache[design_id] = observed
                unreserved_cache[design_id] = max(0, observed - total_reserved)
                over_reserved_cache[design_id] = total_reserved > observed
                reserved_cache[design_id] = total_reserved
                design_pools[design_id] = max(0, observed - total_reserved)

            observed = observed_cache[design_id]
            unreserved = unreserved_cache[design_id]
            over_reserved = over_reserved_cache[design_id]
            total_reserved = reserved_cache[design_id]

            # Weakest-input evidence: feed rows + ledger evidence +
            # classification evidence; never promoted.
            tiers = [
                _EVIDENCE_FEED_ROWS,
                ledger.evidence_for(design_id),
                ci.evidence,
            ]
            evidence = Evidence.weakest(tiers)

            reasons: List[str] = []
            state = STATE_CANDIDATE

            if item.quantity < 0:
                reasons.append(BLOCKED_MALFORMED_ROW)
            elif ci.protected or ci.unknown:
                reasons.append(BLOCKED_PROTECTED_OR_UNKNOWN)
            elif over_reserved:
                reasons.append(BLOCKED_OVER_RESERVED)
            elif design_id in design_blocking:
                reasons.append(BLOCKED_DESIGN_CONDITION)
            elif item.item_id in stack_blocking:
                reasons.append(BLOCKED_STACK_CONDITION)
            elif item.quantity == 0:
                reasons.append(BLOCKED_ZERO_QUANTITY)
            elif unreserved <= 0:
                reasons.append(BLOCKED_ZERO_UNRESERVED)
            elif any(role in _APPLICABLE_UNSUPPORTED_ROLES for role in ci.roles):
                reasons.append(BLOCKED_UNSUPPORTED_DEMAND)
            elif Role.STRATEGIC_RESERVE_CANDIDATE in ci.roles:
                reasons.append(BLOCKED_STRATEGIC_ROLE)

            if reasons:
                state = STATE_HOLD
            elif unreserved <= 0 or item.quantity == 0:
                # Defense in depth: a row that reached here without a
                # reason must still never carry a positive candidate.
                reasons.append(BLOCKED_ZERO_UNRESERVED)
                state = STATE_HOLD

            # Candidate arithmetic against a per-design unreserved
            # POOL: ascending-ItemId allocation so the per-design sum
            # of candidates never exceeds unreserved. The planner
            # never TRUSTS the invariant; it ENFORCES it.
            candidate = 0
            if state == STATE_CANDIDATE:
                pool = design_pools.get(design_id, 0)
                candidate = min(item.quantity, unreserved, pool)
                if candidate < 0:
                    candidate = 0
                if candidate > unreserved or candidate > observed:
                    candidate = 0
                    reasons.append(BLOCKED_ZERO_UNRESERVED)
                    state = STATE_HOLD
                elif candidate == 0:
                    # Pool exhausted by lower-ItemId sibling stacks.
                    reasons.append(BLOCKED_ALLOCATION_EXHAUSTED)
                    state = STATE_HOLD
                else:
                    design_pools[design_id] = pool - candidate

            hint = self._hint(state, ci.roles)
            assert hint in _ASSIGNABLE_DISPOSITIONS, (
                "deferred dispositions (TRANSFER/SELL/CONSUME/DELETE) "
                "must never be assigned this slice"
            )

            rows.append(
                ReductionResult(
                    item_id=item.item_id,
                    item_design_id=design_id,
                    observed_quantity=observed,
                    total_reserved=total_reserved,
                    unreserved=unreserved,
                    candidate_quantity=candidate,
                    state=state,
                    reasons=tuple(reasons),
                    evidence=evidence,
                    disposition_hint=hint,
                    actionable=False,
                )
            )

        # Deterministic output: rows already arrive in ascending-ItemId
        # classification order; sort again to enforce it structurally.
        rows.sort(key=lambda r: r.item_id)
        return ReductionPlan(rows=tuple(rows))

    def _hint(
        self, state: str, roles: Tuple[Role, ...]
    ) -> Disposition:
        """Informational disposition hint from the fixed future order.

        KEEP -> USE -> CRAFT -> ALLOCATE -> (TRANSFER/SELL/CONSUME/
        DELETE deferred). This slice assigns only KEEP (blocked rows),
        ALLOCATE (equipment/module-role rows), CRAFT (crafting-role
        rows) and USE (everything else eligible). RESERVE-intent rows
        are HOLD rows (strategic role blocks them) and hint KEEP; the
        reasons carry the RESERVE intent. TRANSFER/SELL/CONSUME/DELETE
        have no enum member this slice and cannot be constructed.
        """
        if state == STATE_HOLD:
            return Disposition.KEEP
        if Role.CREW_EQUIPMENT_CANDIDATE in roles or Role.SHIP_MODULE_CANDIDATE in roles:
            return Disposition.ALLOCATE
        if Role.CRAFTING_INGREDIENT in roles or Role.CRAFTING_INTERMEDIATE in roles:
            return Disposition.CRAFT
        return Disposition.USE
