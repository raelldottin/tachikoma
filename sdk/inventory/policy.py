from __future__ import annotations

"""Protected-item policy (Task D, lane C).

Pure fail-closed protection rules from
docs/security/protected-item-policy.md §2, evaluated against normalized
domain objects (Item + Optional[ItemDesign] + ProtectionContext).

This module performs NO network I/O, NO filesystem access, NO game mutation.
It never imports sdk.client and never instantiates a Client.

Rule order is FIXED (determinism): 1, 4, 5, 6, 7, 8, 10. ``evaluate`` returns
ALL matched reason codes as a tuple in that order — protection is boolean
(any reason => protected), but the full reason SET is preserved so CLI output
and later planners can explain every protection.

Rules 2 (equipment-plan reservation) and 3 (elected-craft-recipe inputs) are
Task F semantics and are NOT evaluated in this slice. Rule 9
(marketplace-valuable) requires ListSalesByItemDesignId, which is uncaptured
(STATICALLY_DISCOVERED only); it is structurally present but inert and
surfaced as a limitation — absence of marketplace data can never DEMOTE an
item, and no marketplace value is ever invented.

Fail-closed invariants (S3/S4, AD-1):

- unknown design (design=None) => PROTECTED (rule 10) regardless of anything
  else on the item;
- partial metadata never reduces protection: an empty/unrecognized rarity
  protects via INSUFFICIENT_EVIDENCE rather than passing the rule;
- missing context data (None) means a rule CANNOT fire, never that it
  evaluated negative — the classifier exposes this as a limitation;
- quantity NEVER influences protection or classification (Task G guard).
"""

from typing import Optional, Tuple

from ._types import Item, ItemDesign, ProtectionContext


# Reason codes — stable strings, part of the deterministic output contract.
PROTECTED_EQUIPPED = "PROTECTED_EQUIPPED"
PROTECTED_ROOM_MODULE = "PROTECTED_ROOM_MODULE"
PROTECTED_RARITY = "PROTECTED_RARITY"
PROTECTED_EVENT_LIMITED = "PROTECTED_EVENT_LIMITED"
PROTECTED_QUEST_PROGRESSION = "PROTECTED_QUEST_PROGRESSION"
PROTECTED_PREMIUM = "PROTECTED_PREMIUM"
PROTECTED_UNKNOWN_DESIGN = "PROTECTED_UNKNOWN_DESIGN"
PROTECTED_INSUFFICIENT_EVIDENCE = "PROTECTED_INSUFFICIENT_EVIDENCE"


# Rule 5 — protected rarities per protected-item-policy.md §2 rule 5 and the
# synthesis §13 rule 5 ("Rare/Epic/Hero/Unique/Legendary rarities").
PROTECTED_RARITIES: Tuple[str, ...] = (
    "rare",
    "epic",
    "hero",
    "unique",
    "legendary",
)

# Rarities positively known to be unprotected by rule 5. Anything outside
# BOTH sets (empty, or an unrecognized string such as a new rarity the
# captured vocabulary does not contain) fails closed via
# PROTECTED_INSUFFICIENT_EVIDENCE.
UNPROTECTED_RARITIES: Tuple[str, ...] = ("common", "uncommon")

# Rule 6 — event-limited / trader-channel tag markers. The 220-design tagged
# set observed in the captured dynamic feed (worker report t_fd97b1ee §3;
# synthesis §4): trader channels, harvest events, stardust/voidparticle,
# vip/uber, heroic/unique/threewins, plus the literal "event" marker.
EVENT_LIMITED_TAG_MARKERS: Tuple[str, ...] = (
    "event",
    "robotrader",
    "graytrader",
    "harvestor01",
    "harvestor02",
    "harvestor03",
    "harvestor04",
    "stardust",
    "voidparticle",
    "vip",
    "uber",
    "heroic",
    "unique",
    "threewins",
)

# Rule 7 — quest/progression markers. Ship blueprints are the captured
# progression class (16 designs, "KEEP — NEVER discard"); ItemType/subtype
# "quest"/"blueprint" match conservatively by substring on either field.
QUEST_PROGRESSION_MARKERS: Tuple[str, ...] = (
    "quest",
    "blueprint",
)

# Rule 8 — premium markers. Starbux-purchase-path proxy: premium-channel tags
# plus ItemSubType markers for starbux-denominated purchases (BattlePass skip
# subtypes observed as premium-tagged in the captured feed). "vip"/"uber"
# overlap rule 6; a design matching both emits BOTH reason codes.
PREMIUM_TAG_MARKERS: Tuple[str, ...] = (
    "premium",
    "vip",
    "uber",
    "battlepass",
)
PREMIUM_SUBTYPE_MARKERS: Tuple[str, ...] = (
    "battlepass",
)


class ProtectedItemPolicy:
    """Deterministic, pure, fail-closed protection evaluation.

    evaluate(item, design, context) -> Tuple[str, ...] of ALL matched reason
    codes, in fixed rule order 1, 4, 5, 6, 7, 8, 10. Empty tuple means no
    rule fired (not protected by THIS policy — the classifier still never
    marks anything disposable in this slice).
    """

    def evaluate(
        self,
        item: Item,
        design: Optional[ItemDesign],
        context: ProtectionContext,
    ) -> Tuple[str, ...]:
        """Return all matched protection reason codes in fixed order.

        Fail-closed order note: rule 10 (unknown design) is evaluated LAST in
        the returned tuple for output stability, but an unknown design makes
        every design-reading rule (5-8) inapplicable anyway — a None design
        cannot match rarity/tag/quest/premium markers — so unknown => exactly
        (PROTECTED_UNKNOWN_DESIGN,).
        """
        if design is None:
            # Rule 10 — unknown designs are protected by default (AD-1).
            # No design-reading rule can fire; ItemId/quantity are irrelevant.
            return (PROTECTED_UNKNOWN_DESIGN,)

        reasons: list[str] = []

        # Rule 1 — currently equipped (ItemId must appear in the equipped
        # set; ItemDesignId is never used as an instance id here).
        if context.equipped_item_ids is not None and item.item_id in context.equipped_item_ids:
            reasons.append(PROTECTED_EQUIPPED)

        # Rule 4 — currently assigned to a room/module slot.
        if context.room_item_ids is not None and item.item_id in context.room_item_ids:
            reasons.append(PROTECTED_ROOM_MODULE)

        # Rule 5 — rare/unique rarities; unrecognized/empty rarity fails
        # closed (partial metadata never reduces protection).
        rarity = (design.rarity or "").strip().lower()
        if rarity in PROTECTED_RARITIES:
            reasons.append(PROTECTED_RARITY)
        elif rarity not in UNPROTECTED_RARITIES:
            # Not a known-safe value and not a known-protected value: the
            # captured vocabulary cannot classify it -> protect.
            reasons.append(PROTECTED_INSUFFICIENT_EVIDENCE)

        # Rules 6/7/8 — marker matches over design fields.
        if _tags_match(design, EVENT_LIMITED_TAG_MARKERS):
            reasons.append(PROTECTED_EVENT_LIMITED)
        if _quest_progression(design):
            reasons.append(PROTECTED_QUEST_PROGRESSION)
        if _premium(design):
            reasons.append(PROTECTED_PREMIUM)

        return tuple(reasons)


def _tags_match(design: ItemDesign, markers: Tuple[str, ...]) -> bool:
    """True when any parsed @Tags entry equals a marker (case-insensitive).

    Uses the normalized ``tags`` tuple from parse_item_design (sorted,
    lowercased comparison only — no re-parsing, no raw-dict reach-through).
    """
    return any(t.lower() in markers for t in design.tags)


def _quest_progression(design: ItemDesign) -> bool:
    """Rule 7: quest or progression items (ship blueprints, quest types)."""
    item_type = (design.item_type or "").lower()
    sub_type = (design.item_sub_type or "").lower()
    return any(m in item_type or m in sub_type for m in QUEST_PROGRESSION_MARKERS)


def _premium(design: ItemDesign) -> bool:
    """Rule 8: premium / Starbux-purchase-path proxies."""
    sub_type = (design.item_sub_type or "").lower()
    if any(m in sub_type for m in PREMIUM_SUBTYPE_MARKERS):
        return True
    return _tags_match(design, PREMIUM_TAG_MARKERS)


# Rule identifiers in evaluation order — exposed for tests and docs.
EVALUATED_RULES: Tuple[int, ...] = (1, 4, 5, 6, 7, 8, 10)
NOT_EVALUATED_RULES: Tuple[int, ...] = (2, 3, 9)


def policy_limitations(context: ProtectionContext) -> Tuple[str, ...]:
    """Deterministic, ordered limitation statements for unavailable inputs.

    A limitation is NOT a protection demotion: when data is unavailable the
    rule simply cannot fire, and the classifier surfaces why. Ordered:
    equipment-plan (rule 2, Task F), craft-recipe (rule 3, Task F),
    marketplace value (rule 9, uncaptured endpoint), equipped/room sets when
    the context does not carry them.
    """
    out: list[str] = []
    out.append("rule 2 equipment-plan reservation: Task F semantics, not evaluated this slice")
    out.append("rule 3 elected-craft-recipe reservation: Task F semantics, not evaluated this slice")
    out.append(
        "rule 9 marketplace value: ListSalesByItemDesignId uncaptured "
        "(STATICALLY_DISCOVERED); inert, never demotes, never invented"
    )
    if context.equipped_item_ids is None:
        out.append("rule 1 equipped-item check: equipped ItemId set unavailable")
    if context.room_item_ids is None:
        out.append("rule 4 room-module check: room ItemId set unavailable")
    return tuple(out)
