from __future__ import annotations

"""Inventory-domain value types.

Pure data: frozen dataclasses and enums. No I/O, no network, no mutation of
caller state. See docs/architecture-addendum-inventory-t_71d8b652.md §3.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, FrozenSet, Iterable, Mapping, Optional, Tuple


class Evidence(Enum):
    """Protocol-evidence tier for an endpoint or data claim.

    Confidence is minimum-across-inputs (gate verdict Q4): never upgrade an
    output above the weakest input tier. Declared order (top to bottom) is the
    strength order; ``weaker``/``weakest`` rely on it and are covered by tests.
    """

    VERIFIED = "VERIFIED"
    CAPTURED_NOT_VERIFIED = "CAPTURED_NOT_VERIFIED"
    CAPTURED_AMBIGUOUS = "CAPTURED_AMBIGUOUS"
    STATICALLY_DISCOVERED = "STATICALLY_DISCOVERED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def weaker(cls, a: "Evidence", b: "Evidence") -> "Evidence":
        """Return the weaker (less trustworthy) of two tiers.

        Strict: both arguments must be Evidence members. A non-member input
        is a programming error and must not be silently coerced — coercing it
        could silently promote a classification.
        """
        members = list(cls)
        return a if members.index(a) >= members.index(b) else b

    @classmethod
    def weakest(cls, tiers: "Iterable[Evidence]") -> "Evidence":
        """Minimum confidence across required inputs (Q4).

        Empty input returns UNKNOWN (fail closed: no evidence at all is the
        weakest possible state, never the strongest).
        """
        result: Optional[Evidence] = None
        for tier in tiers:
            result = tier if result is None else cls.weaker(result, tier)
        return result if result is not None else cls.UNKNOWN


class ReservationSource(Enum):
    TRAINING = "TRAINING"
    EQUIPMENT = "EQUIPMENT"
    CRAFT = "CRAFT"
    MODULE = "MODULE"
    AMMUNITION = "AMMUNITION"
    STRATEGIC = "STRATEGIC"


class Disposition(Enum):
    """Future disposition vocabulary, in the fixed priority order of the
    reduction ladder (from the task brief): KEEP/RESERVE -> USE -> CRAFT
    -> ALLOCATE -> TRANSFER -> SELL -> CONSUME -> DELETE.

    Only KEEP, USE, CRAFT and ALLOCATE exist as members this slice.
    TRANSFER, SELL, CONSUME and DELETE are DEFERRED Task G semantics
    (marketplace/transfer/mutation planning) and deliberately have no
    member here: constructing them is impossible, so no dry-run row can
    ever carry a deferred hint. A later slice adds the members when it
    adds the semantics that justify them.
    """

    KEEP = "KEEP"
    USE = "USE"
    CRAFT = "CRAFT"
    ALLOCATE = "ALLOCATE"


class Classification(Enum):
    PROTECTED = "PROTECTED"
    RESERVED = "RESERVED"
    SURPLUS_USABLE = "SURPLUS_USABLE"
    SURPLUS_CRAFTABLE = "SURPLUS_CRAFTABLE"
    TRANSFERABLE = "TRANSFERABLE"
    SELLABLE = "SELLABLE"
    CONSUMABLE = "CONSUMABLE"
    DISCARDABLE = "DISCARDABLE"
    UNKNOWN = "UNKNOWN"


class Role(Enum):
    """A non-mutually-exclusive role an item/design can hold (Task D).

    Declaration order is the stable output order for ``ClassifiedItem.roles``
    (determinism requirement). TRANSFERABLE_CANDIDATE,
    MARKETPLACE_SALE_CANDIDATE and DISCARD_CANDIDATE exist in the domain
    vocabulary but are NEVER assigned in this slice — they imply Task G
    surplus semantics that this slice must not produce.
    """

    CREW_TRAINING_MATERIAL = "CREW_TRAINING_MATERIAL"
    CREW_EQUIPMENT_CANDIDATE = "CREW_EQUIPMENT_CANDIDATE"
    CRAFTING_INGREDIENT = "CRAFTING_INGREDIENT"
    CRAFTING_INTERMEDIATE = "CRAFTING_INTERMEDIATE"
    SHIP_MODULE_CANDIDATE = "SHIP_MODULE_CANDIDATE"
    AMMUNITION_MATERIAL = "AMMUNITION_MATERIAL"
    STRATEGIC_RESERVE_CANDIDATE = "STRATEGIC_RESERVE_CANDIDATE"
    TRANSFERABLE_CANDIDATE = "TRANSFERABLE_CANDIDATE"
    MARKETPLACE_SALE_CANDIDATE = "MARKETPLACE_SALE_CANDIDATE"
    CONSUMABLE_USE_CANDIDATE = "CONSUMABLE_USE_CANDIDATE"
    DISCARD_CANDIDATE = "DISCARD_CANDIDATE"


# Roles that must never be assigned in this slice (Task G semantics).
# Exposed for tests and the verifier; the classifier asserts against it.
ROLES_FORBIDDEN_THIS_SLICE: Tuple[Role, ...] = (
    Role.TRANSFERABLE_CANDIDATE,
    Role.MARKETPLACE_SALE_CANDIDATE,
    Role.DISCARD_CANDIDATE,
)


@dataclass(frozen=True)
class Reservation:
    """A quantity held back for a future need (Task F computes real ones).

    Task D never creates a Reservation with a nonzero quantity; the field
    exists so the ClassifiedItem shape matches the architecture addendum §3
    contract for later slices without a breaking migration.
    """

    item_design_id: int
    source: ReservationSource
    quantity: int
    reason: str


@dataclass(frozen=True)
class ProtectionContext:
    """Account-state inputs the policy cannot derive from item/design rows.

    ``equipped_item_ids`` / ``room_item_ids`` are sets of ItemId (instance
    ids, NOT ItemDesignId — the two are never conflated).

    None means the data is UNAVAILABLE (limitation must be exposed; the
    corresponding rule cannot fire). An empty frozenset means the data was
    positively observed to be empty (rule evaluates and does not fire).
    """

    equipped_item_ids: Optional[FrozenSet[int]] = None
    room_item_ids: Optional[FrozenSet[int]] = None


@dataclass(frozen=True)
class ClassifiedItem:
    """Task D output: what an item IS and what protections apply.

    NOT what to do with it. ``reservation`` is ALWAYS the empty tuple in
    this slice (Task F computes real reservations); ``available_surplus`` is
    ALWAYS None (Task G computes it). ``roles`` are ordered by Role
    declaration order; ``protection_reasons`` in fixed policy rule order;
    ``classification_reasons`` in fixed derivation order.
    """

    item: Item
    design: Optional[ItemDesign]
    classification: Classification
    roles: Tuple[Role, ...] = ()
    protection_reasons: Tuple[str, ...] = ()
    classification_reasons: Tuple[str, ...] = ()
    evidence: Evidence = Evidence.UNKNOWN
    unknown: bool = False
    reservation: Tuple[Reservation, ...] = ()
    available_surplus: Optional[int] = None

    @property
    def protected(self) -> bool:
        """True when any protection reason fired (or classification is
        PROTECTED/UNKNOWN — belt and braces: the classifier must also set
        reasons, but protection never depends on reasons being populated)."""
        if self.classification in (Classification.PROTECTED, Classification.UNKNOWN):
            return True
        return bool(self.protection_reasons)


@dataclass(frozen=True)
class ItemDesign:
    """An ItemDesign row from the dynamic design feed.

    ItemDesignId is the global design key (account-independent).
    Unknown/extra attributes preserved in `raw`.
    """

    item_design_id: int
    item_name: str
    item_type: str = ""
    item_sub_type: str = ""
    rarity: str = ""
    module_type: Optional[str] = None
    tags: Tuple[str, ...] = ()
    # (ingredient_design_id, quantity) pairs parsed from @Ingredients
    ingredients: Tuple[Tuple[int, int], ...] = ()
    training_design_id: Optional[int] = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Item:
    """An owned item instance.

    ItemId is the account-scoped instance identifier. Only the six
    capture-observed attributes are modeled; extras are preserved in `raw`.
    """

    item_id: int
    ship_id: int
    item_design_id: int
    quantity: int
    is_new: bool
    skin_key: Optional[str]
    raw: Mapping[str, Any] = field(default_factory=dict)


# The six Item-node attributes observed across 4,168 captured Item tags
# (verification-ledger VeLedger-8cf2). Anything else on an Item node is
# unverified and must not be consumed.
CAPTURED_ITEM_ATTRIBUTES: Tuple[str, ...] = (
    "ItemId",
    "ShipId",
    "ItemDesignId",
    "Quantity",
    "IsNew",
    "SkinKey",
)
