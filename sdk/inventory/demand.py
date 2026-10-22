from __future__ import annotations

"""Task F: pure inventory demand + reservation planning (READ-ONLY).

Answers, per ItemDesignId: "how many units of each ItemDesignId have
a known downstream purpose?" Classification (Task D) says what an item
CAN be used for; this module reserves only what repository-supported
state actually CLAIMS. Reservations never authorize any action, never
compute a surplus/disposable/reduction value (Task G prohibition),
and never reduce Task D protection: unknown or ambiguous demand only
ever adds conditions, never removes protection.

SUPPORTED demand sources (demand-source discovery lane t_b38f0330,
verified against the 2026-08-08 capture set):

- EQUIPMENT (current state): Character/@ItemIds references on a
  GetShipByUserId- or ListAllUserDataFirst2-shaped payload. Each
  distinct referenced ItemId claims its WHOLE stack: the per-unit
  split between multiple wearers of one stack is unobservable, and
  over-claiming is the fail-closed direction for a ledger whose only
  downstream consumer is protection. Repeated references to one
  ItemId are deduplicated to a single claim and surfaced as an
  ambiguity condition (edge E1: 16 live references over 11 stacks).
- MODULE (current state): Room/@ItemIds attachments, same whole-stack
  rule, deduplicated per distinct ItemId within and across rooms
  (edge E2). A stack both equipped and room-attached (edge E5) is
  claimed once, under EQUIPMENT (documented precedence), with a
  conflict condition; the live sets are disjoint but the rule is
  required.
- STRATEGIC: explicit configuration values ONLY
  (settings inventory.strategic_reserve {design: qty}). No approved
  default exists anywhere in the repository, so an absent/empty
  config section yields ZERO strategic reservations plus a
  SOURCE_UNSUPPORTED condition - never an invented retention number.
- CRAFT: recursive recipe expansion over ItemDesign/@Ingredients,
  driven ONLY by explicit crafting objectives
  (PlanningContext.crafting_objectives; empty by default). No
  crafting objective exists in code or config today, so default runs
  create zero craft reservations while the graph capability is
  retained for a future explicit objective. Expansion is GROSS
  requirements: intermediates AND their raw inputs are both claimed,
  because whether the plan would craft intermediates from raw inputs
  or use held stock is unobservable. Over-claiming is the fail-closed
  direction for a protection ledger; netting held stock against
  upstream demand would under-claim and is Task G-adjacent semantics
  this slice must not invent.

INTENTIONALLY UNSUPPORTED (zero reservations, surfaced as ledger
conditions - never silently omitted, never invented):

- TRAINING: the runtime training flow is one-at-a-time; no plan,
  quantity, or per-character requirement representation exists.
- AMMUNITION: RebuildAmmo3 restocks server-side (ammoCategory only);
  there is no client-side authoritative ammo quantity source.

OVERLAPPING DEMAND RULES:
- Distinct ItemIds of one design are independent claims and sum.
- Repeated references to one ItemId are ONE claim (deduplicated),
  never double-counted (edges E1/E2).
- Claims are allocated against observed supply in the fixed
  precedence EQUIPMENT, MODULE, CRAFT, TRAINING, AMMUNITION,
  STRATEGIC (current-state facts before planning intent before
  policy configuration). A design whose requests exceed its observed
  quantity gets per-entry reservations capped at supply plus a
  DEMAND_EXCEEDS_SUPPLY condition. Shortfalls are EXPOSED, never
  converted into acquisition, transfer or reduction semantics.

QUANTITY RULES: every quantity is a deterministic non-negative int.
Negative inventory rows, malformed reference tokens, malformed or
negative configuration values, duplicated configuration records and
unreasonable magnitudes (above MAX_REASONABLE_QUANTITY) yield
fail-closed conditions and zero reservations from the malformed
input. A demand referencing an UNKNOWN design reserves ZERO (fail
closed); Task D already protects those stacks independently of this
module.

EVIDENCE: an entry's evidence is the MINIMUM across its required
inputs (gate Q4) and is never promoted. Feed rows and current-state
references are CAPTURED_NOT_VERIFIED; explicit planning intent
(strategic configuration, crafting objectives) is INFERRED, so a
strategic reservation never rises above INFERRED even though the
design feed itself is stronger.

SAFETY (Task G prohibition): this module never exposes a surplus,
disposable, sale, consume, delete or transfer quantity and never
builds a reduction action. It performs no network I/O and no
filesystem access; the only transport-layer symbol it imports is the
pure `_extract_collection` helper (the same one-way boundary as
catalog.py / designs.py). Determinism: identical inputs produce
identical ledgers - entries sort by (design id, source precedence,
provenance); conditions sort by (code, detail, design).

Multi-account planning is out of scope by design: the planner
operates on one catalog + one planning context and carries no
account identity anywhere.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sdk.client import _extract_collection
from ._types import Evidence, Item, ProtectionContext, ReservationSource
from .catalog import InventoryCatalog

# Quantities above this bound are treated as pathological input rather
# than demand (card: "integer overflow / unreasonable values").
MAX_REASONABLE_QUANTITY = 10**9

# Live recipe graphs are shallow (902 nodes, measured max depth in the
# tens); deeper expansion is pathological, not plannable.
MAX_RECIPE_DEPTH = 64

# Evidence tiers for each demand input (fixed, per the planning docs and
# the demand-source discovery verdicts).
_EVIDENCE_FEED_ROWS = Evidence.CAPTURED_NOT_VERIFIED  # captured feed, not checksum-verified
_EVIDENCE_CURRENT_STATE = Evidence.CAPTURED_NOT_VERIFIED  # ship-payload reference facts
_EVIDENCE_PLANNING_INTENT = Evidence.INFERRED  # explicit configuration / objectives

# Allocation precedence against observed supply: ItemId-grounded
# current-state facts first, then objective-driven demand, then
# policy configuration. Unsupported sources (always zero this slice)
# keep a fixed position so the order is total and stable.
_ALLOCATION_ORDER: Tuple[ReservationSource, ...] = (
    ReservationSource.EQUIPMENT,
    ReservationSource.MODULE,
    ReservationSource.CRAFT,
    ReservationSource.TRAINING,
    ReservationSource.AMMUNITION,
    ReservationSource.STRATEGIC,
)
_ALLOCATION_INDEX = {source: index for index, source in enumerate(_ALLOCATION_ORDER)}

# Unresolved-demand condition codes (stable strings; output contract).
SOURCE_UNSUPPORTED = "SOURCE_UNSUPPORTED"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
UNKNOWN_ITEM_REFERENCE = "UNKNOWN_ITEM_REFERENCE"
UNKNOWN_DESIGN = "UNKNOWN_DESIGN"
EQUIPMENT_SHARED_STACK = "EQUIPMENT_SHARED_STACK"
MODULE_MULTI_ATTACH = "MODULE_MULTI_ATTACH"
BOTH_EQUIPPED_AND_ROOM_ATTACHED = "BOTH_EQUIPPED_AND_ROOM_ATTACHED"
RECIPE_CYCLE = "RECIPE_CYCLE"
DUPLICATE_INGREDIENT = "DUPLICATE_INGREDIENT"
DUPLICATE_CONFIG_ENTRY = "DUPLICATE_CONFIG_ENTRY"
MALFORMED_QUANTITY = "MALFORMED_QUANTITY"
UNREASONABLE_QUANTITY = "UNREASONABLE_QUANTITY"
DEMAND_EXCEEDS_SUPPLY = "DEMAND_EXCEEDS_SUPPLY"
CRAFT_OBJECTIVE_NOT_CRAFTABLE = "CRAFT_OBJECTIVE_NOT_CRAFTABLE"


@dataclass(frozen=True)
class UnresolvedDemand:
    """A fail-closed demand condition: an ambiguity, an unavailability,
    or a malformed input that the ledger surfaces instead of inventing
    semantics. Never a demotion, never a reduction authorization."""

    code: str
    detail: str
    design_id: Optional[int] = None


@dataclass(frozen=True)
class ReservationEntry:
    """One claimed quantity for one design from one demand source.

    ``requested_quantity`` is what the source claims; reserved is what
    could actually be held back against observed supply. Both are
    non-negative; provenance names the exact origin of the claim.
    """

    item_design_id: int
    source: ReservationSource
    requested_quantity: int
    reserved_quantity: int
    reason: str
    evidence: Evidence
    provenance: str


@dataclass(frozen=True)
class ReservationLedger:
    """Deterministic Task F output: reservation entries plus fail-closed
    conditions, keyed by ItemDesignId.

    Deliberately carries NO surplus/disposable/reduction field: the
    quantity that remains after reservations is Task G semantics and is
    not computed here.
    """

    entries: Tuple[ReservationEntry, ...] = ()
    conditions: Tuple[UnresolvedDemand, ...] = ()
    observed_by_design: Mapping[int, int] = field(default_factory=dict)

    def entries_for(self, design_id: int) -> Tuple[ReservationEntry, ...]:
        return tuple(e for e in self.entries if e.item_design_id == int(design_id))

    def total_requested(self, design_id: int) -> int:
        return sum(e.requested_quantity for e in self.entries_for(design_id))

    def total_reserved(self, design_id: int) -> int:
        return sum(e.reserved_quantity for e in self.entries_for(design_id))

    def observed_quantity(self, design_id: int) -> int:
        return self.observed_by_design.get(int(design_id), 0)

    def conditions_for(self, design_id: int) -> Tuple[UnresolvedDemand, ...]:
        key = int(design_id)
        return tuple(c for c in self.conditions if c.design_id == key)

    def evidence_for(self, design_id: int) -> Evidence:
        """Minimum across the design's required inputs (feed rows plus
        every entry). Never promoted above the weakest input; a design
        with no entries has no established evidence (fail closed)."""
        entries = self.entries_for(design_id)
        if not entries:
            return Evidence.UNKNOWN
        return Evidence.weakest([_EVIDENCE_FEED_ROWS] + [e.evidence for e in entries])

    def design_ids(self) -> Tuple[int, ...]:
        """Designs carrying entries or design-scoped conditions, ascending."""
        ids = {e.item_design_id for e in self.entries}
        ids.update(c.design_id for c in self.conditions if c.design_id is not None)
        return tuple(sorted(ids))

    @property
    def total_reserved_all(self) -> int:
        return sum(e.reserved_quantity for e in self.entries)


def _parse_id_refs(raw: Any) -> Tuple[Tuple[int, ...], Tuple[str, ...]]:
    """Parse a comma-separated ItemId reference list.

    Returns (ids, malformed_tokens). Absent/empty input is ((), ()) -
    positively no references, not an error. Negative ids parse but are
    impossible references and are routed to the malformed list.
    """
    if raw is None:
        return (), ()
    text = str(raw).strip()
    if not text:
        return (), ()
    ids: List[int] = []
    bad: List[str] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            bad.append("(empty segment)")
            continue
        try:
            value = int(token)
        except ValueError:
            bad.append(token)
            continue
        if value < 0:
            bad.append(str(value))
        else:
            ids.append(value)
    return tuple(ids), tuple(bad)


@dataclass(frozen=True)
class PlanningContext:
    """Pure Task F inputs: ItemId-scoped current-state reference counts
    plus explicit planning configuration.

    ``equipped_references`` / ``room_references`` map ItemId -> number of
    references (a set would silently hide the multi-reference ambiguity
    the ledger must surface). None means the reference set was NOT
    provided (source unavailable -> zero reservations + condition); an
    empty mapping means it was positively observed to be empty.

    ``strategic_reserve`` / ``crafting_objectives`` are raw configuration
    mappings; the planner validates them (design ids, non-negative
    integer quantities, duplicate records) and fails closed on anything
    malformed. ``parse_conditions`` carries fail-closed conditions the
    payload parser observed (malformed reference tokens).
    """

    equipped_references: Optional[Mapping[int, int]] = None
    room_references: Optional[Mapping[int, int]] = None
    strategic_reserve: Mapping[Any, Any] = field(default_factory=dict)
    crafting_objectives: Mapping[Any, Any] = field(default_factory=dict)
    parse_conditions: Tuple[UnresolvedDemand, ...] = ()

    @classmethod
    def from_ship_dict(
        cls,
        ship_response: Any,
        strategic_reserve: Optional[Mapping[Any, Any]] = None,
        crafting_objectives: Optional[Mapping[Any, Any]] = None,
    ) -> "PlanningContext":
        """Build from a parsed GetShipByUserId / ListAllUserDataFirst2
        response dict. Any wrapper shape works: Character and Room nodes
        are located structurally, so both captured payload shapes parse
        identically. Non-dict input yields all-unavailable reference
        sets (fail closed).

        A dict that contains no Character/Room nodes at all is treated
        as UNAVAILABLE, not as "positively empty": an error-shaped
        payload must never make the protection rules evaluate negative.
        Nested Character/Items equipment echoes (edge E4) are ignored -
        only the @ItemIds attributes are read, never nested Item rows.

        Malformed @ItemIds tokens become parse conditions; parseable
        tokens are still counted, because dropping the whole list would
        under-claim (the dangerous direction for a reservation ledger).
        """
        if not isinstance(ship_response, dict):
            return cls(
                equipped_references=None,
                room_references=None,
                strategic_reserve=dict(strategic_reserve or {}),
                crafting_objectives=dict(crafting_objectives or {}),
            )

        equipped: Dict[int, int] = {}
        rooms: Dict[int, int] = {}
        conditions: List[UnresolvedDemand] = []

        characters = _extract_collection(ship_response, "Character")
        for index, character in enumerate(characters):
            label = character.get("@CharacterId", f"#{index}")
            refs, bad = _parse_id_refs(character.get("@ItemIds"))
            for item_id in refs:
                equipped[item_id] = equipped.get(item_id, 0) + 1
            for token in bad:
                conditions.append(
                    UnresolvedDemand(
                        MALFORMED_QUANTITY,
                        f"Character {label} @ItemIds token {token!r} is not a valid ItemId",
                    )
                )

        room_nodes = _extract_collection(ship_response, "Room")
        for index, room in enumerate(room_nodes):
            label = room.get("@RoomId", f"#{index}")
            refs, bad = _parse_id_refs(room.get("@ItemIds"))
            for item_id in refs:
                rooms[item_id] = rooms.get(item_id, 0) + 1
            for token in bad:
                conditions.append(
                    UnresolvedDemand(
                        MALFORMED_QUANTITY,
                        f"Room {label} @ItemIds token {token!r} is not a valid ItemId",
                    )
                )

        return cls(
            equipped_references=dict(sorted(equipped.items())) if characters else None,
            room_references=dict(sorted(rooms.items())) if room_nodes else None,
            strategic_reserve=dict(strategic_reserve or {}),
            crafting_objectives=dict(crafting_objectives or {}),
            parse_conditions=tuple(conditions),
        )

    def protection_context(self) -> ProtectionContext:
        """Project the reference sets into the Task D protection context
        (rule 1 equipped / rule 4 room-module). Reference COUNTS are not
        needed for protection - only membership - and None stays None so
        unavailable data keeps its limitation instead of evaluating."""
        equipped = (
            frozenset(self.equipped_references)
            if self.equipped_references is not None
            else None
        )
        room_ids = (
            frozenset(self.room_references)
            if self.room_references is not None
            else None
        )
        return ProtectionContext(equipped_item_ids=equipped, room_item_ids=room_ids)


def _parse_config_design(raw: Any) -> Optional[int]:
    """A configuration design key: positive integer or digit string."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _parse_config_quantity(raw: Any) -> Optional[int]:
    """A configuration quantity: non-negative integer or digit string.
    Bools, floats, negatives and junk all fail closed (None)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _validated_config_keys(
    mapping: Mapping[Any, Any],
    label: str,
    conditions: List[UnresolvedDemand],
) -> Dict[int, Any]:
    """Validate configuration keys: parse each design key, flag malformed
    ones, and DROP duplicated keys entirely (merging or first-wins would
    invent which representation of the duplicate is authoritative)."""
    parsed: List[Tuple[int, Any]] = []
    for raw_key, raw_value in sorted(mapping.items(), key=lambda kv: (repr(kv[0]), repr(kv[1]))):
        design_id = _parse_config_design(raw_key)
        if design_id is None:
            conditions.append(
                UnresolvedDemand(
                    MALFORMED_QUANTITY,
                    f"{label} key {raw_key!r} is not a valid ItemDesignId",
                )
            )
            continue
        parsed.append((design_id, raw_value))

    counts: Dict[int, int] = {}
    for design_id, _ in parsed:
        counts[design_id] = counts.get(design_id, 0) + 1
    duplicated = {d for d, n in counts.items() if n > 1}
    for design_id in sorted(duplicated):
        values = [v for d, v in parsed if d == design_id]
        conditions.append(
            UnresolvedDemand(
                DUPLICATE_CONFIG_ENTRY,
                f"{label} lists design {design_id} more than once (raw values {values!r}); "
                "all duplicate entries dropped",
                design_id,
            )
        )
    return {d: v for d, v in parsed if d not in duplicated}


# Provisional demand row before allocation:
#   (design_id, source, requested_quantity, provenance, reason, evidence)
_ProvisionalRow = Tuple[int, ReservationSource, int, str, str, Evidence]


class DemandPlanner:
    """Deterministic, pure Task F planner: catalog + planning context ->
    ReservationLedger. No network I/O, no filesystem access, no
    mutation of inputs, no account dimension."""

    def plan(
        self,
        catalog: InventoryCatalog,
        context: Optional[PlanningContext] = None,
    ) -> ReservationLedger:
        ctx = context if context is not None else PlanningContext()
        conditions: List[UnresolvedDemand] = list(ctx.parse_conditions)
        rows: List[_ProvisionalRow] = []
        by_item_id = {item.item_id: item for item in catalog.items()}

        observed, supply_conditions = self._observed_quantities(catalog)
        conditions.extend(supply_conditions)

        # Sources with no repository-supported quantity representation
        # today: zero reservations, always surfaced (never silent).
        conditions.append(
            UnresolvedDemand(
                SOURCE_UNSUPPORTED,
                "TRAINING: the runtime training flow is one-at-a-time; no plan, "
                "quantity or per-character requirement representation exists, "
                "so zero training reservations",
            )
        )
        conditions.append(
            UnresolvedDemand(
                SOURCE_UNSUPPORTED,
                "AMMUNITION: RebuildAmmo3 restocks server-side (ammoCategory only); "
                "no client-side authoritative ammo quantity source exists, so zero "
                "ammunition reservations",
            )
        )

        conflict_ids = self._conflicts(ctx)
        conflict_conditions = self._conflict_conditions(by_item_id, conflict_ids)
        conditions.extend(conflict_conditions)

        equipment_rows, equipment_conditions = self._equipment_demand(catalog, ctx, by_item_id)
        rows.extend(equipment_rows)
        conditions.extend(equipment_conditions)

        module_rows, module_conditions = self._module_demand(catalog, ctx, by_item_id, conflict_ids)
        rows.extend(module_rows)
        conditions.extend(module_conditions)

        strategic_rows, strategic_conditions = self._strategic_demand(catalog, ctx)
        rows.extend(strategic_rows)
        conditions.extend(strategic_conditions)

        craft_rows, craft_conditions = self._craft_demand(catalog, ctx)
        rows.extend(craft_rows)
        conditions.extend(craft_conditions)

        ledger = self._finalize(rows, conditions, observed)
        return ledger

    # ------------------------------------------------------------------
    # Supply
    # ------------------------------------------------------------------

    def _observed_quantities(
        self, catalog: InventoryCatalog
    ) -> Tuple[Dict[int, int], List[UnresolvedDemand]]:
        """Observed supply per design. Negative row quantities are
        malformed server data: flagged and counted as zero so they can
        never REDUCE reservations against the design."""
        observed: Dict[int, int] = {}
        conditions: List[UnresolvedDemand] = []
        for item in catalog.items():
            quantity = item.quantity
            if quantity < 0:
                conditions.append(
                    UnresolvedDemand(
                        MALFORMED_QUANTITY,
                        f"ItemId {item.item_id} (design {item.item_design_id}) carries "
                        f"negative Quantity {quantity}; counted as zero supply",
                        item.item_design_id,
                    )
                )
                quantity = 0
            observed[item.item_design_id] = observed.get(item.item_design_id, 0) + quantity
        return observed, conditions

    # ------------------------------------------------------------------
    # Current-state reference sources (EQUIPMENT / MODULE)
    # ------------------------------------------------------------------

    def _conflicts(self, ctx: PlanningContext) -> frozenset:
        """ItemIds both equipped and room-attached (edge E5). Claimed
        once under EQUIPMENT; the MODULE claim is suppressed."""
        if ctx.equipped_references is None or ctx.room_references is None:
            return frozenset()
        return frozenset(set(ctx.equipped_references) & set(ctx.room_references))

    def _conflict_conditions(
        self, by_item_id: Mapping[int, Item], conflict_ids: frozenset
    ) -> List[UnresolvedDemand]:
        conditions: List[UnresolvedDemand] = []
        for item_id in sorted(conflict_ids):
            item = by_item_id.get(item_id)
            design_id = item.item_design_id if item is not None else None
            conditions.append(
                UnresolvedDemand(
                    BOTH_EQUIPPED_AND_ROOM_ATTACHED,
                    f"ItemId {item_id} is both equipped and room-attached; one physical "
                    "stack cannot satisfy both, claimed once under EQUIPMENT "
                    "(documented precedence), MODULE claim suppressed",
                    design_id,
                )
            )
        return conditions

    def _equipment_demand(
        self,
        catalog: InventoryCatalog,
        ctx: PlanningContext,
        by_item_id: Mapping[int, Item],
    ) -> Tuple[List[_ProvisionalRow], List[UnresolvedDemand]]:
        rows: List[_ProvisionalRow] = []
        conditions: List[UnresolvedDemand] = []
        if ctx.equipped_references is None:
            conditions.append(
                UnresolvedDemand(
                    SOURCE_UNAVAILABLE,
                    "EQUIPMENT: equipped ItemId references were not provided "
                    "(no ship payload); zero equipment reservations",
                )
            )
            return rows, conditions
        for item_id in sorted(ctx.equipped_references):
            refs = ctx.equipped_references[item_id]
            item = by_item_id.get(item_id)
            if item is None:
                conditions.append(
                    UnresolvedDemand(
                        UNKNOWN_ITEM_REFERENCE,
                        f"ItemId {item_id} is referenced as equipped but absent from "
                        "the inventory catalog",
                    )
                )
                continue
            design = catalog.resolve(item)
            if design is None:
                conditions.append(
                    UnresolvedDemand(
                        UNKNOWN_DESIGN,
                        f"ItemId {item_id} (design {item.item_design_id}) is equipped "
                        "but its design is absent from the captured dynamic design feed",
                        item.item_design_id,
                    )
                )
                continue
            if refs >= 2:
                conditions.append(
                    UnresolvedDemand(
                        EQUIPMENT_SHARED_STACK,
                        f"ItemId {item_id} (design {design.item_design_id}) is referenced "
                        f"by {refs} characters; per-unit split is unobservable, whole "
                        "stack claimed once (not double-counted)",
                        design.item_design_id,
                    )
                )
            quantity = max(0, item.quantity)
            rows.append(
                (
                    design.item_design_id,
                    ReservationSource.EQUIPMENT,
                    quantity,
                    f"Character/@ItemIds -> ItemId {item_id} ({refs} character references)",
                    "currently equipped by crew (current-state fact, not a plan)",
                    Evidence.weakest([_EVIDENCE_FEED_ROWS, _EVIDENCE_CURRENT_STATE]),
                )
            )
        return rows, conditions

    def _module_demand(
        self,
        catalog: InventoryCatalog,
        ctx: PlanningContext,
        by_item_id: Mapping[int, Item],
        conflict_ids: frozenset,
    ) -> Tuple[List[_ProvisionalRow], List[UnresolvedDemand]]:
        rows: List[_ProvisionalRow] = []
        conditions: List[UnresolvedDemand] = []
        if ctx.room_references is None:
            conditions.append(
                UnresolvedDemand(
                    SOURCE_UNAVAILABLE,
                    "MODULE: room ItemId attachments were not provided "
                    "(no ship payload); zero module reservations",
                )
            )
            return rows, conditions
        for item_id in sorted(ctx.room_references):
            if item_id in conflict_ids:
                continue  # claimed once under EQUIPMENT (precedence)
            refs = ctx.room_references[item_id]
            item = by_item_id.get(item_id)
            if item is None:
                conditions.append(
                    UnresolvedDemand(
                        UNKNOWN_ITEM_REFERENCE,
                        f"ItemId {item_id} is referenced as room-attached but absent "
                        "from the inventory catalog",
                    )
                )
                continue
            design = catalog.resolve(item)
            if design is None:
                conditions.append(
                    UnresolvedDemand(
                        UNKNOWN_DESIGN,
                        f"ItemId {item_id} (design {item.item_design_id}) is room-attached "
                        "but its design is absent from the captured dynamic design feed",
                        item.item_design_id,
                    )
                )
                continue
            if refs >= 2:
                conditions.append(
                    UnresolvedDemand(
                        MODULE_MULTI_ATTACH,
                        f"ItemId {item_id} (design {design.item_design_id}) appears "
                        f"{refs} times in room attachments (within/across rooms); "
                        "deduplicated to one whole-stack claim (not double-counted)",
                        design.item_design_id,
                    )
                )
            quantity = max(0, item.quantity)
            rows.append(
                (
                    design.item_design_id,
                    ReservationSource.MODULE,
                    quantity,
                    f"Room/@ItemIds -> ItemId {item_id} ({refs} room attachments)",
                    "currently attached to a ship room (current-state fact, not a plan)",
                    Evidence.weakest([_EVIDENCE_FEED_ROWS, _EVIDENCE_CURRENT_STATE]),
                )
            )
        return rows, conditions

    # ------------------------------------------------------------------
    # Configuration-driven sources (STRATEGIC / CRAFT objectives)
    # ------------------------------------------------------------------

    def _strategic_demand(
        self, catalog: InventoryCatalog, ctx: PlanningContext
    ) -> Tuple[List[_ProvisionalRow], List[UnresolvedDemand]]:
        rows: List[_ProvisionalRow] = []
        conditions: List[UnresolvedDemand] = []
        if not ctx.strategic_reserve:
            conditions.append(
                UnresolvedDemand(
                    SOURCE_UNSUPPORTED,
                    "STRATEGIC: no explicit strategic-reserve configuration was provided; "
                    "no approved default exists, so zero strategic reservations",
                )
            )
            return rows, conditions
        validated = _validated_config_keys(ctx.strategic_reserve, "strategic_reserve", conditions)
        for design_id in sorted(validated):
            raw_value = validated[design_id]
            quantity = _parse_config_quantity(raw_value)
            if quantity is None:
                conditions.append(
                    UnresolvedDemand(
                        MALFORMED_QUANTITY,
                        f"strategic_reserve[{design_id}] value {raw_value!r} is not a "
                        "valid non-negative integer quantity",
                        design_id,
                    )
                )
                continue
            if quantity == 0:
                continue  # explicit zero reserves nothing (valid configuration)
            design = catalog.design_catalog.get(design_id) if catalog.design_catalog else None
            if design is None:
                conditions.append(
                    UnresolvedDemand(
                        UNKNOWN_DESIGN,
                        f"strategic_reserve design {design_id} is absent from the "
                        "captured dynamic design feed",
                        design_id,
                    )
                )
                continue
            rows.append(
                (
                    design_id,
                    ReservationSource.STRATEGIC,
                    quantity,
                    f"config strategic_reserve[{design_id}]={quantity}",
                    "explicit strategic-reserve configuration (planning intent)",
                    Evidence.weakest([_EVIDENCE_FEED_ROWS, _EVIDENCE_PLANNING_INTENT]),
                )
            )
        return rows, conditions

    def _craft_demand(
        self, catalog: InventoryCatalog, ctx: PlanningContext
    ) -> Tuple[List[_ProvisionalRow], List[UnresolvedDemand]]:
        rows: List[_ProvisionalRow] = []
        conditions: List[UnresolvedDemand] = []
        if not ctx.crafting_objectives:
            conditions.append(
                UnresolvedDemand(
                    SOURCE_UNSUPPORTED,
                    "CRAFT: the recipe graph (@Ingredients) is cataloged capability "
                    "only; no crafting objective exists in code or config, so zero "
                    "craft reservations",
                )
            )
            return rows, conditions
        design_catalog = catalog.design_catalog
        validated = _validated_config_keys(ctx.crafting_objectives, "crafting_objectives", conditions)
        objectives: Dict[int, int] = {}
        for design_id in sorted(validated):
            raw_value = validated[design_id]
            quantity = _parse_config_quantity(raw_value)
            if quantity is None:
                conditions.append(
                    UnresolvedDemand(
                        MALFORMED_QUANTITY,
                        f"crafting_objectives[{design_id}] value {raw_value!r} is not a "
                        "valid non-negative integer quantity",
                        design_id,
                    )
                )
                continue
            if quantity == 0:
                continue  # explicit zero objective demands nothing
            design = design_catalog.get(design_id) if design_catalog else None
            if design is None:
                conditions.append(
                    UnresolvedDemand(
                        UNKNOWN_DESIGN,
                        f"crafting_objectives design {design_id} is absent from the "
                        "captured dynamic design feed",
                        design_id,
                    )
                )
                continue
            objectives[design_id] = quantity
            target_design = design_catalog.get(design_id) if design_catalog else None
            if target_design is not None and not target_design.ingredients:
                conditions.append(
                    UnresolvedDemand(
                        CRAFT_OBJECTIVE_NOT_CRAFTABLE,
                        f"crafting_objectives design {design_id} carries no recipe "
                        "(@Ingredients) in the captured design feed; its objective "
                        "creates no ingredient demand",
                        design_id,
                    )
                )

        # (ingredient_design, objective_design, depth) -> requested quantity.
        # Shared intermediates aggregate inside one objective at one depth;
        # distinct objectives/depths stay distinct provenance.
        aggregate: Dict[Tuple[int, int, int], int] = {}
        for objective_design in sorted(objectives):
            design = design_catalog.get(objective_design) if design_catalog else None
            if design is None:  # unreachable (validated above); defensive
                continue
            self._expand_recipe(
                design,
                objectives[objective_design],
                depth=1,
                path=(objective_design,),
                objective=objective_design,
                objective_qty=objectives[objective_design],
                design_catalog=design_catalog,
                aggregate=aggregate,
                conditions=conditions,
            )
        for ingredient_design, objective_design, depth in sorted(aggregate):
            rows.append(
                (
                    ingredient_design,
                    ReservationSource.CRAFT,
                    aggregate[(ingredient_design, objective_design, depth)],
                    f"craft objective design={objective_design} "
                    f"qty={objectives[objective_design]} depth={depth}",
                    "crafting-objective ingredient demand (recursive recipe expansion)",
                    Evidence.weakest([_EVIDENCE_FEED_ROWS, _EVIDENCE_PLANNING_INTENT]),
                )
            )
        return rows, conditions

    def _expand_recipe(
        self,
        design: Any,
        qty_needed: int,
        depth: int,
        path: Tuple[int, ...],
        objective: int,
        objective_qty: int,
        design_catalog: Any,
        aggregate: Dict[Tuple[int, int, int], int],
        conditions: List[UnresolvedDemand],
    ) -> None:
        """Deterministic depth-first expansion of one recipe level.

        - quantities multiply through depth (qty_needed * per-recipe qty);
        - duplicate ingredient designs in one recipe are SUMMED as a
          conservative over-claim, with a condition (never silent);
        - cyclic edges (design already on the expansion path) are cut
          with a RECIPE_CYCLE condition - never infinite recursion;
        - unknown ingredient designs fail closed (no demand recorded);
        - pathological magnitudes/depths are cut with conditions.
        """
        ingredients = design.ingredients
        if not ingredients:
            return
        if depth > MAX_RECIPE_DEPTH:
            conditions.append(
                UnresolvedDemand(
                    UNREASONABLE_QUANTITY,
                    f"craft expansion for objective design={objective} reached depth "
                    f"{depth} (bound {MAX_RECIPE_DEPTH}); deeper edges dropped",
                    objective,
                )
            )
            return

        per_design: Dict[int, int] = {}
        raw_counts: Dict[int, int] = {}
        for ing_id, ing_qty in ingredients:
            raw_counts[ing_id] = raw_counts.get(ing_id, 0) + 1
            if ing_qty <= 0:
                conditions.append(
                    UnresolvedDemand(
                        MALFORMED_QUANTITY,
                        f"recipe design {design.item_design_id} lists ingredient design "
                        f"{ing_id} with non-positive quantity {ing_qty}; edge dropped",
                        ing_id,
                    )
                )
                continue
            per_design[ing_id] = per_design.get(ing_id, 0) + ing_qty

        for ing_id in sorted(per_design):
            ing_qty = per_design[ing_id]
            if raw_counts.get(ing_id, 0) > 1:
                conditions.append(
                    UnresolvedDemand(
                        DUPLICATE_INGREDIENT,
                        f"recipe design {design.item_design_id} lists ingredient design "
                        f"{ing_id} {raw_counts[ing_id]} times; quantities summed "
                        f"({ing_qty}) as a conservative over-claim",
                        ing_id,
                    )
                )
            if ing_id in path:
                conditions.append(
                    UnresolvedDemand(
                        RECIPE_CYCLE,
                        f"recipe cycle detected: expansion path {' -> '.join(str(d) for d in path)} "
                        f"-> {ing_id}; cyclic edge dropped",
                        ing_id,
                    )
                )
                continue
            ing_design = design_catalog.get(ing_id) if design_catalog else None
            if ing_design is None:
                conditions.append(
                    UnresolvedDemand(
                        UNKNOWN_DESIGN,
                        f"craft ingredient design {ing_id} (required by design "
                        f"{design.item_design_id}) is absent from the captured "
                        "dynamic design feed",
                        ing_id,
                    )
                )
                continue
            total = qty_needed * ing_qty
            if total > MAX_REASONABLE_QUANTITY:
                conditions.append(
                    UnresolvedDemand(
                        UNREASONABLE_QUANTITY,
                        f"craft demand for design {ing_id} reached {total} (objective "
                        f"design={objective} qty={objective_qty}, depth {depth}); "
                        f"exceeds bound {MAX_REASONABLE_QUANTITY}, edge dropped",
                        ing_id,
                    )
                )
                continue
            key = (ing_id, objective, depth)
            aggregate[key] = aggregate.get(key, 0) + total
            self._expand_recipe(
                ing_design,
                total,
                depth + 1,
                path + (ing_id,),
                objective,
                objective_qty,
                design_catalog,
                aggregate,
                conditions,
            )

    # ------------------------------------------------------------------
    # Allocation + finalization
    # ------------------------------------------------------------------

    def _finalize(
        self,
        rows: List[_ProvisionalRow],
        conditions: List[UnresolvedDemand],
        observed: Dict[int, int],
    ) -> ReservationLedger:
        """Allocate reserved quantities against observed supply in the
        fixed source precedence, then sort entries and conditions
        deterministically and deduplicate conditions."""
        rows.sort(key=lambda r: (r[0], _ALLOCATION_INDEX[r[1]], r[3]))

        remaining = dict(observed)
        entries: List[ReservationEntry] = []
        for design_id, source, requested, provenance, reason, evidence in rows:
            if evidence is Evidence.UNKNOWN:
                conditions.append(
                    UnresolvedDemand(
                        UNKNOWN_DESIGN,
                        f"design {design_id} source {source.value} demand required "
                        "UNKNOWN metadata; reservation dropped",
                        design_id,
                    )
                )
                continue
            if requested > MAX_REASONABLE_QUANTITY:
                conditions.append(
                    UnresolvedDemand(
                        UNREASONABLE_QUANTITY,
                        f"design {design_id} source {source.value} requested {requested}; "
                        f"exceeds bound {MAX_REASONABLE_QUANTITY}, reservation dropped",
                        design_id,
                    )
                )
                continue
            reserve = min(requested, remaining.get(design_id, 0))
            remaining[design_id] = remaining.get(design_id, 0) - reserve
            entries.append(
                ReservationEntry(
                    item_design_id=design_id,
                    source=source,
                    requested_quantity=requested,
                    reserved_quantity=reserve,
                    reason=reason,
                    evidence=evidence,
                    provenance=provenance,
                )
            )

        requested_totals: Dict[int, int] = {}
        for entry in entries:
            requested_totals[entry.item_design_id] = (
                requested_totals.get(entry.item_design_id, 0) + entry.requested_quantity
            )
        for design_id in sorted(requested_totals):
            observed_qty = observed.get(design_id, 0)
            shortfall = requested_totals[design_id] - observed_qty
            if shortfall > 0:
                conditions.append(
                    UnresolvedDemand(
                        DEMAND_EXCEEDS_SUPPLY,
                        f"design {design_id}: requested {requested_totals[design_id]}, "
                        f"observed {observed_qty}, unresolvable {shortfall} "
                        "(exposed only; never an acquisition or reduction recommendation)",
                        design_id,
                    )
                )

        seen = set()
        unique: List[UnresolvedDemand] = []
        for condition in sorted(
            conditions,
            key=lambda c: (c.code, c.detail, c.design_id if c.design_id is not None else -1),
        ):
            key = (condition.code, condition.detail, condition.design_id)
            if key not in seen:
                seen.add(key)
                unique.append(condition)

        return ReservationLedger(
            entries=tuple(entries),
            conditions=tuple(unique),
            observed_by_design=dict(sorted(observed.items())),
        )
