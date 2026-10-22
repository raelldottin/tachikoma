from __future__ import annotations

"""Deterministic inventory classifier (Task D).

Pure domain logic — no network I/O, no filesystem access, no game mutation,
no Client dependency. Operates on normalized ``InventoryCatalog`` domain
objects (from_dicts/from_client built elsewhere); this module only READS
them.

Task D answers "what is this item and what protections apply". It does NOT
answer "is this surplus / needed / safe to mutate" — that is Tasks F and G:

- ``reservation`` is ALWAYS the empty tuple (Task F computes real ones);
- ``available_surplus`` is ALWAYS None (Task G computes it);
- quantity NEVER influences classification (no "high quantity => excess");
- SURPLUS_*/TRANSFERABLE/SELLABLE/DISCARDABLE classifications are never
  assigned in this slice;
- Role.TRANSFERABLE_CANDIDATE / MARKETPLACE_SALE_CANDIDATE /
  DISCARD_CANDIDATE are never assigned in this slice.

Primary buckets emitted: PROTECTED | RESERVED | CONSUMABLE | UNKNOWN only.
"""

from typing import Optional, Tuple

from .catalog import InventoryCatalog
from ._types import (
    ROLES_FORBIDDEN_THIS_SLICE,
    Classification,
    ClassifiedItem,
    Evidence,
    ItemDesign,
    ProtectionContext,
    Role,
)
from .policy import ProtectedItemPolicy, policy_limitations


# Evidence tiers for each classification input (fixed, per planning docs).
_EVIDENCE_ITEM_AND_DESIGN_ROWS = Evidence.CAPTURED_NOT_VERIFIED  # feed captured, not checksum-verified
_EVIDENCE_TRAINING_LINK = Evidence.CAPTURED_NOT_VERIFIED  # @TrainingDesignId on captured feed
_EVIDENCE_MODULE = Evidence.STATICALLY_DISCOVERED  # module endpoints uncaptured
_EVIDENCE_MARKER_ROLES = Evidence.INFERRED  # ItemType/subtype/tag-derived roles

# Training subtypes from the captured feed (worker report t_fd97b1ee §3 /
# synthesis §4): all RESERVED-WHEN-PRESENT — consumption resets costly state.
# InstantTraining also carries @TrainingDesignId; the others match by subtype
# alone. Subtype strings are exact (case-insensitive) captured values.
_TRAINING_SUBTYPES: Tuple[str, ...] = (
    "instanttraining",
    "instantxp",
    "reducefatigue",
    "resettraining",
    "reduceprestige",
    "rerollbonusstats",
    "specialcrispr",
)

# Strategic-reserve subtypes: Immunity items (timed PVP protection), keep all.
_STRATEGIC_SUBTYPES: Tuple[str, ...] = ("immunity",)

# Consumable/use subtypes: lootbox/openable + resource packs + the literal
# Consumable subtype (3 captured designs; they stay protected via their
# rarity/tags — role and protection are independent). Protection always
# wins over role in bucket assignment.
_CONSUMABLE_SUBTYPES: Tuple[str, ...] = (
    "instantprize",
    "lootbox",
    "consumable",
    "resourcepackage",
    "gaspack",
    "mineralpack",
    "fillgasstorage",
    "fillmineralstorage",
)

# Ammunition: Missile designs feed the RebuildAmmo3 manufacturing loop.
_AMMUNITION_TYPES: Tuple[str, ...] = ("missile",)


class InventoryClassifier:
    """Classify every item in a catalog: roles + protection + evidence.

    Deterministic: output order is ascending ItemId (the catalog's own
    deterministic order); reason codes are ordered by the fixed policy rule
    order and the fixed role declaration order. Same inputs -> identical
    output (R1).
    """

    def __init__(
        self,
        policy: Optional[ProtectedItemPolicy] = None,
        context: Optional[ProtectionContext] = None,
    ):
        # Frozen contract: default-constructed policy/context when omitted.
        self._policy = policy if policy is not None else ProtectedItemPolicy()
        self._context = context if context is not None else ProtectionContext()
        self._limitations: Tuple[str, ...] = ()

    @property
    def limitations(self) -> Tuple[str, ...]:
        """Unavailability statements gathered during the last classify() run
        (deterministic order). Empty before the first run."""
        return self._limitations

    def classify(self, catalog: InventoryCatalog) -> Tuple[ClassifiedItem, ...]:
        """Classify all items; deterministic ascending-ItemId output."""
        out: list[ClassifiedItem] = []
        limitations: list[str] = list(policy_limitations(self._context))
        for item in catalog.items():
            design = catalog.resolve(item)
            protection_reasons = self._policy.evaluate(item, design, self._context)
            roles, role_evidence = self._roles(design)
            classification, unknown = self._bucket(design, protection_reasons, roles)
            # Evidence = minimum across required inputs (Q4). Unknown design
            # => UNKNOWN tier; never promoted above the weakest input.
            evidence = self._evidence(design, role_evidence)
            out.append(
                ClassifiedItem(
                    item=item,
                    design=design,
                    classification=classification,
                    roles=roles,
                    protection_reasons=protection_reasons,
                    classification_reasons=self._classification_reasons(
                        design, protection_reasons, roles, unknown
                    ),
                    evidence=evidence,
                    unknown=unknown,
                    # Task F/G guards — always inert this slice:
                    reservation=(),
                    available_surplus=None,
                )
            )
        self._limitations = tuple(limitations)
        return tuple(out)

    # ------------------------------------------------------------------
    # Role derivation (multi-role; never mutually exclusive)
    # ------------------------------------------------------------------

    def _roles(
        self, design: Optional[ItemDesign]
    ) -> Tuple[Tuple[Role, ...], Tuple[Evidence, ...]]:
        """Derive roles from design metadata, in Role declaration order.

        Returns (roles, evidence_tiers) — tiers parallel to roles, used for
        the minimum-across-inputs rule.
        """
        if design is None:
            return (), ()
        roles: list[Role] = []
        tiers: list[Evidence] = []
        sub = (design.item_sub_type or "").lower()
        itype = (design.item_type or "").lower()

        if design.training_design_id is not None or sub in _TRAINING_SUBTYPES:
            roles.append(Role.CREW_TRAINING_MATERIAL)
            tiers.append(_EVIDENCE_TRAINING_LINK)
        if sub.startswith("equipment") or "equipment" in sub:
            roles.append(Role.CREW_EQUIPMENT_CANDIDATE)
            tiers.append(_EVIDENCE_MARKER_ROLES)
        if sub == "artifact":
            roles.append(Role.CRAFTING_INGREDIENT)
            tiers.append(_EVIDENCE_MARKER_ROLES)
        if design.ingredients:
            # A design WITH @Ingredients is a crafting INTERMEDIATE (it is
            # produced FROM those inputs); being an ingredient for other
            # recipes is a separate question the demand graph answers (Task F).
            roles.append(Role.CRAFTING_INTERMEDIATE)
            tiers.append(_EVIDENCE_MARKER_ROLES)
        if design.module_type is not None or sub == "module":
            roles.append(Role.SHIP_MODULE_CANDIDATE)
            tiers.append(_EVIDENCE_MODULE)
        if itype in _AMMUNITION_TYPES or sub == "missile":
            roles.append(Role.AMMUNITION_MATERIAL)
            tiers.append(_EVIDENCE_MARKER_ROLES)
        if any(s in sub for s in _STRATEGIC_SUBTYPES):
            roles.append(Role.STRATEGIC_RESERVE_CANDIDATE)
            tiers.append(_EVIDENCE_MARKER_ROLES)
        if any(s in sub for s in _CONSUMABLE_SUBTYPES):
            roles.append(Role.CONSUMABLE_USE_CANDIDATE)
            tiers.append(_EVIDENCE_MARKER_ROLES)

        # De-duplicate while preserving declaration order (defensive: each
        # role is appended at most once by construction; dedupe guards
        # against future marker sets overlapping the same Role).
        seen = set()
        unique_roles = tuple(r for r in roles if not (r in seen or seen.add(r)))
        tiers_by_role = {r: t for r, t in zip(roles, tiers)}
        unique_tiers = tuple(tiers_by_role[r] for r in unique_roles)
        # Safety: no forbidden role may ever be assigned this slice.
        assert not (set(unique_roles) & set(ROLES_FORBIDDEN_THIS_SLICE)), (
            "Task D must never assign transfer/sale/discard roles"
        )
        return unique_roles, unique_tiers

    # ------------------------------------------------------------------
    # Bucket classification
    # ------------------------------------------------------------------

    def _bucket(
        self,
        design: Optional[ItemDesign],
        protection_reasons: Tuple[str, ...],
        roles: Tuple[Role, ...],
    ) -> Tuple[Classification, bool]:
        """Primary bucket. Protection ALWAYS wins (S3: protection runs
        before bucket classification; a protected item is never demoted to a
        lower bucket by its roles)."""
        if design is None:
            return Classification.PROTECTED, True
        if protection_reasons:
            return Classification.PROTECTED, False
        # RESERVED-WHEN-PRESENT (conservative buckets from synthesis §5/§6/
        # §8): training material, module candidate, ammunition, strategic
        # reserve. This is classification-level conservatism, NOT Task F
        # reservation math — no quantities are computed.
        if any(
            r in roles
            for r in (
                Role.CREW_TRAINING_MATERIAL,
                Role.SHIP_MODULE_CANDIDATE,
                Role.AMMUNITION_MATERIAL,
                Role.STRATEGIC_RESERVE_CANDIDATE,
            )
        ):
            return Classification.RESERVED, False
        if Role.CONSUMABLE_USE_CANDIDATE in roles:
            return Classification.CONSUMABLE, False
        return Classification.RESERVED, False

    def _classification_reasons(
        self,
        design: Optional[ItemDesign],
        protection_reasons: Tuple[str, ...],
        roles: Tuple[Role, ...],
        unknown: bool,
    ) -> Tuple[str, ...]:
        """Deterministic human-readable reasons for the bucket assignment."""
        if unknown:
            return ("design absent from captured dynamic design feed",)
        reasons: list[str] = []
        if protection_reasons:
            reasons.append(
                "protected by policy rules: " + ", ".join(protection_reasons)
            )
        if Role.CREW_TRAINING_MATERIAL in roles:
            reasons.append("training material: consumption resets costly-to-rebuild state")
        if Role.SHIP_MODULE_CANDIDATE in roles:
            reasons.append("ship-module candidate: room-module endpoints uncaptured")
        if Role.AMMUNITION_MATERIAL in roles:
            reasons.append("ammunition: replenished by the RebuildAmmo3 loop")
        if Role.STRATEGIC_RESERVE_CANDIDATE in roles:
            reasons.append("strategic reserve (Immunity): held for timed PVP protection")
        if Role.CONSUMABLE_USE_CANDIDATE in roles:
            reasons.append("consumable/use candidate (classification only, no action implied)")
        if Role.CRAFTING_INGREDIENT in roles:
            reasons.append("crafting ingredient (Artifact): recipe input")
        if Role.CRAFTING_INTERMEDIATE in roles:
            reasons.append("crafting intermediate: produced from recipe inputs")
        if Role.CREW_EQUIPMENT_CANDIDATE in roles:
            reasons.append("crew-equipment candidate (classification only)")
        if not reasons:
            reasons.append("no protection rule fired and no role matched; default conservative bucket RESERVED")
        return tuple(reasons)

    # ------------------------------------------------------------------
    # Evidence propagation
    # ------------------------------------------------------------------

    def _evidence(
        self,
        design: Optional[ItemDesign],
        role_evidence: Tuple[Evidence, ...],
    ) -> Evidence:
        """Minimum confidence across required inputs (Q4) — never promoted
        above the weakest input."""
        tiers = [_EVIDENCE_ITEM_AND_DESIGN_ROWS]
        if design is None:
            return Evidence.UNKNOWN
        tiers.extend(role_evidence)
        return Evidence.weakest(tiers)


def classify_catalog(
    catalog: InventoryCatalog,
    policy: Optional[ProtectedItemPolicy] = None,
    context: Optional[ProtectionContext] = None,
) -> Tuple[ClassifiedItem, ...]:
    """Convenience pure function: classify a catalog in one call."""
    return InventoryClassifier(policy=policy, context=context).classify(catalog)
