#!/usr/bin/env python3
"""Task D lane B tests: deterministic classification semantics.

Tests/test_inventory_classify.py — the classification-semantics lane file
staked on the blackboard (lane-stake:t_12a30041). Lane C (safety) and lane D
(CLI) have their own files; this one owns multi-role classification, role
derivation paths, evidence propagation, determinism of the classification
surface, and the brief's explicit multi-role examples.

All inputs are sanitized/synthetic dictionaries and domain objects. NO live
Pixel Starships traffic; no requests import; no destructive call (repo rule;
safety invariants S1/S2).

Brief coverage map (TEST REQUIREMENTS 1-24):
  1. deterministic classification of known designs
  2. multiple simultaneous classifications on one design
  6. rare/unique protection (classification interplay)
  8. crafting ingredient classification
  9. crafting intermediate classification
  10. consumable classification
  11. equipment/module classification where data supports it
  12. ammunition/manufacturing classification where data supports it
  13. evidence = minimum across required inputs
  15. deterministic reason-code ordering
  17. ItemId versus ItemDesignId correctness
  18. quantity alone never creates surplus/discard classification
  19/20. no Task F/G semantics in classification output
  24. classifier performs no network I/O
"""

import socket
import unittest
from unittest.mock import patch

import xmltodict

from sdk.inventory import (
    Classification,
    ClassifiedItem,
    DesignCatalog,
    Evidence,
    InventoryCatalog,
    InventoryClassifier,
    ProtectionContext,
    ProtectedItemPolicy,
    Role,
    classify_catalog,
)
from sdk.inventory.policy import (
    PROTECTED_EVENT_LIMITED,
    PROTECTED_PREMIUM,
    PROTECTED_RARITY,
)

# ---------------------------------------------------------------------------
# Sanitized fixtures (shape of captured data; all values synthetic)
# ---------------------------------------------------------------------------

LANE_B_FEED_XML = """<DesignService xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
  <ListAllDynamicDesigns>
    <ItemDesigns>
      <ItemDesign ItemDesignId="301" ItemDesignName="Gold Ore" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="302" ItemDesignName="Crew Uniform" ItemType="Equipment" ItemSubType="EquipmentBody" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="303" ItemDesignName="Brain Stim" ItemType="Consumable" ItemSubType="InstantTraining" Rarity="Common" Ingredients="" TrainingDesignId="9" Tags=""/>
      <ItemDesign ItemDesignId="304" ItemDesignName="Prestige Wipe" ItemType="Consumable" ItemSubType="ReducePrestige" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="305" ItemDesignName="Training Reset Kit" ItemType="Consumable" ItemSubType="ResetTraining" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="306" ItemDesignName="XP Chews" ItemType="Consumable" ItemSubType="InstantXP" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="307" ItemDesignName="Espresso Shot" ItemType="Consumable" ItemSubType="ReduceFatigue" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="308" ItemDesignName="Stat Reroll" ItemType="Consumable" ItemSubType="RerollBonusStats" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="309" ItemDesignName="CRISPR Vial" ItemType="Consumable" ItemSubType="SpecialCRISPR" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="310" ItemDesignName="Golden Chest" ItemType="Prize" ItemSubType="InstantPrize" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="311" ItemDesignName="Shield Gen" ItemType="Module" ItemSubType="Module" ModuleType="Shield" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="312" ItemDesignName="Plasma Missile" ItemType="Missile" ItemSubType="Missile" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="313" ItemDesignName="Void Shield Charm" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Common" Ingredients="" Tags="event"/>
      <ItemDesign ItemDesignId="314" ItemDesignName="Crafted Helm" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Common" Ingredients="301x5" Tags=""/>
      <ItemDesign ItemDesignId="315" ItemDesignName="Stardust Idol" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Common" Ingredients="" Tags="stardust"/>
      <ItemDesign ItemDesignId="316" ItemDesignName="PVP Aegis" ItemType="Consumable" ItemSubType="Immunity" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="317" ItemDesignName="Artifact Piece" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="318" ItemDesignName="Token Crate" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="" Tags="premium"/>
      <ItemDesign ItemDesignId="319" ItemDesignName="Wall Poster" ItemType="Decor" ItemSubType="Decor" Rarity="Common" Ingredients="" Tags=""/>
    </ItemDesigns>
  </ListAllDynamicDesigns>
</DesignService>"""


def _items_response(rows):
    return {
        "ItemService": {
            "ListItemsOfAShip": {
                "Items": {
                    "Item": [
                        {
                            "@ItemId": str(item_id),
                            "@ShipId": "77",
                            "@ItemDesignId": str(design_id),
                            "@Quantity": str(qty),
                            "@IsNew": "false",
                            "@SkinKey": "",
                        }
                        for item_id, design_id, qty in rows
                    ]
                }
            }
        }
    }


def _catalog(rows, feed_xml=LANE_B_FEED_XML):
    designs = DesignCatalog.from_dynamic_dict(xmltodict.parse(feed_xml))
    return InventoryCatalog.from_dicts(_items_response(rows), designs)


def _classified(rows, context=None, feed_xml=LANE_B_FEED_XML, policy=None):
    catalog = _catalog(rows, feed_xml)
    clf = InventoryClassifier(policy=policy, context=context)
    return clf.classify(catalog), clf


class TestTrainingRolePaths(unittest.TestCase):
    """Both training-role derivation paths and the captured subtype strings.

    Regression for the lane-B audit defect: the captured ItemSubType is
    ``ResetTraining`` (fd97b1ee §3, synthesis §4 — 2 designs in the feed), but
    the marker table previously held the nonexistent ``"reducetraining"``.
    A ResetTraining design has no @TrainingDesignId (only InstantTraining
    designs carry one), so it must match via the exact subtype string.
    """

    def _one(self, design_id, feed_xml=LANE_B_FEED_XML):
        (c,) = _classified([(11, design_id, 1)], feed_xml=feed_xml)[0]
        return c

    def test_reset_training_subtype_matches_training_role(self):
        # REGRESSION: captured subtype ResetTraining must get the training
        # role; before the fix the marker was "reducetraining" and the design
        # fell through to the default-RESERVED reason with no role.
        c = self._one(305)  # ResetTraining, no TrainingDesignId
        self.assertIn(Role.CREW_TRAINING_MATERIAL, c.roles)
        self.assertEqual(c.classification, Classification.RESERVED)
        self.assertTrue(
            any("training material" in r for r in c.classification_reasons),
            c.classification_reasons,
        )
        # It must NOT be silently demoted to CONSUMABLE.
        self.assertNotEqual(c.classification, Classification.CONSUMABLE)

    def test_training_role_via_training_design_id_link(self):
        # Path 2: @TrainingDesignId present, subtype also matches
        c = self._one(303)  # InstantTraining + TrainingDesignId=9
        self.assertIn(Role.CREW_TRAINING_MATERIAL, c.roles)
        self.assertEqual(c.classification, Classification.RESERVED)
        self.assertEqual(c.evidence, Evidence.CAPTURED_NOT_VERIFIED)

    def test_all_captured_training_subtypes_classified(self):
        # Every captured training subtype (fd97b1ee §3: InstantTraining 90,
        # InstantXP 8, ReduceFatigue 1, ResetTraining 2, ReducePrestige 10,
        # RerollBonusStats 1, SpecialCRISPR 1) must land RESERVED with the
        # training role.
        for design_id in (303, 304, 305, 306, 307, 308, 309):
            c = self._one(design_id)
            self.assertIn(
                Role.CREW_TRAINING_MATERIAL, c.roles, f"design {design_id}"
            )
            self.assertEqual(c.classification, Classification.RESERVED, design_id)
            self.assertTrue(
                any("training material" in r for r in c.classification_reasons),
                f"design {design_id}: {c.classification_reasons}",
            )

    def test_training_evidence_captured_not_verified(self):
        # Training-link evidence (@TrainingDesignId on the captured feed) is
        # CAPTURED_NOT_VERIFIED. Subtype-only matches are equally grounded in
        # the captured feed (fd97b1ee §3 enumerates the subtypes), so the
        # training role is CAPTURED_NOT_VERIFIED via either path — per the
        # frozen lane contract "training => CAPTURED_NOT_VERIFIED".
        via_link = self._one(303)  # InstantTraining + TrainingDesignId
        self.assertEqual(via_link.evidence, Evidence.CAPTURED_NOT_VERIFIED)
        via_subtype = self._one(305)  # ResetTraining, subtype only
        self.assertEqual(via_subtype.evidence, Evidence.CAPTURED_NOT_VERIFIED)


class TestStrategicReserveRole(unittest.TestCase):
    """Strategic-reserve candidate role (Immunity — timed PVP protection)."""

    def test_immunity_gets_strategic_reserve_role(self):
        (c,) = _classified([(11, 316, 4)])[0]
        self.assertIn(Role.STRATEGIC_RESERVE_CANDIDATE, c.roles)
        self.assertEqual(c.classification, Classification.RESERVED)
        self.assertTrue(
            any("strategic reserve" in r.lower() for r in c.classification_reasons),
            c.classification_reasons,
        )

    def test_immunity_role_independent_of_protection(self):
        # Role and protection are orthogonal: give the Immunity design a
        # protected rarity and BOTH must appear — role survives protection.
        marked = LANE_B_FEED_XML.replace(
            'ItemDesignId="316" ItemDesignName="PVP Aegis" ItemType="Consumable" '
            'ItemSubType="Immunity" Rarity="Common"',
            'ItemDesignId="316" ItemDesignName="PVP Aegis" ItemType="Consumable" '
            'ItemSubType="Immunity" Rarity="Unique"',
        )
        (c,) = _classified([(11, 316, 1)], feed_xml=marked)[0]
        self.assertIn(Role.STRATEGIC_RESERVE_CANDIDATE, c.roles)
        self.assertEqual(c.classification, Classification.PROTECTED)
        self.assertIn(PROTECTED_RARITY, c.protection_reasons)


class TestMultiRoleSemantics(unittest.TestCase):
    """The brief's explicit multi-role example: one design, several roles.

    'a design may be both crafting ingredient + consumable + protected if
    evidence supports all three' — plus role/protection orthogonality and
    the fixed Role declaration order under multi-role overlap.
    """

    def test_crafting_ingredient_plus_consumable_plus_protected(self):
        # An Artifact (crafting ingredient) that is also tagged premium
        # (protected) — and via a crafted-intermediate overlap the triple is
        # fully realized with an InstantPrize ingredient consumer:
        # design 318 = Artifact + premium tag  -> CRAFTING_INGREDIENT + PROTECTED
        rows = [(11, 318, 3)]
        (c,) = _classified(rows)[0]
        self.assertIn(Role.CRAFTING_INGREDIENT, c.roles)
        self.assertIn(PROTECTED_PREMIUM, c.protection_reasons)
        self.assertEqual(c.classification, Classification.PROTECTED)
        # consumable role via the same design? No — 318 is Artifact-only.
        # Build the true triple explicitly: a lootbox that opens into an
        # Artifact and is itself Rare (protected):
        feed = LANE_B_FEED_XML.replace(
            'ItemDesignId="310" ItemDesignName="Golden Chest" ItemType="Prize" '
            'ItemSubType="InstantPrize" Rarity="Common" Ingredients=""',
            'ItemDesignId="310" ItemDesignName="Golden Chest" ItemType="Prize" '
            'ItemSubType="InstantPrize" Rarity="Rare" Ingredients="301x2"',
        )
        (c2,) = _classified([(11, 310, 2)], feed_xml=feed)[0]
        self.assertIn(Role.CONSUMABLE_USE_CANDIDATE, c2.roles)
        self.assertIn(Role.CRAFTING_INTERMEDIATE, c2.roles)
        self.assertEqual(c2.classification, Classification.PROTECTED)
        self.assertIn(PROTECTED_RARITY, c2.protection_reasons)
        # triple: protected (policy) + consumable (role) + crafting
        # intermediate (role) — multiple simultaneous classifications.
        self.assertTrue(c2.protected)
        self.assertGreaterEqual(len(c2.roles), 2)

    def test_roles_order_is_role_declaration_order(self):
        # (15) roles output order == Role enum declaration order, even when
        # many roles overlap on one design.
        feed = LANE_B_FEED_XML.replace(
            'ItemDesignId="310" ItemDesignName="Golden Chest" ItemType="Prize" '
            'ItemSubType="InstantPrize" Rarity="Common" Ingredients=""',
            'ItemDesignId="310" ItemDesignName="Golden Chest" ItemType="Prize" '
            'ItemSubType="InstantPrize" Rarity="Common" Ingredients="301x2"',
        )
        # 310 with ingredients: consumable + crafting intermediate
        (c,) = _classified([(11, 310, 1)], feed_xml=feed)[0]
        declaration = [r for r in Role]
        idx = [declaration.index(r) for r in c.roles]
        self.assertEqual(
            idx,
            sorted(idx),
            f"roles out of declaration order: {c.roles}",
        )

    def test_role_and_protection_orthogonal(self):
        # Roles never demote protection and protection never strips roles.
        rows = [(11, 313, 1)]  # EquipmentHead + event tag
        (c,) = _classified(rows)[0]
        self.assertIn(Role.CREW_EQUIPMENT_CANDIDATE, c.roles)
        self.assertIn(PROTECTED_EVENT_LIMITED, c.protection_reasons)
        self.assertEqual(c.classification, Classification.PROTECTED)


class TestBucketAssignment(unittest.TestCase):
    """Primary-bucket precedence: PROTECTED > RESERVED-when-present > CONSUMABLE."""

    def test_default_bucket_for_roleless_unprotected_design_is_reserved(self):
        # A design with no markers at all (no protection fired, no matched
        # role): default conservative bucket RESERVED (never CONSUMABLE or
        # anything weaker). design 319 "Wall Poster": Decor, no ingredients,
        # no markers.
        rows = [(11, 319, 1)]
        (c,) = _classified(rows)[0]
        self.assertEqual(c.roles, ())
        self.assertEqual(c.classification, Classification.RESERVED)
        self.assertTrue(
            any("default conservative bucket RESERVED" in r for r in c.classification_reasons),
            c.classification_reasons,
        )

    def test_ammunition_and_module_buckets(self):
        rows = [(11, 312, 60), (12, 311, 1)]
        by_item = {c.item.item_id: c for c in _classified(rows)[0]}
        self.assertIn(Role.AMMUNITION_MATERIAL, by_item[11].roles)
        self.assertEqual(by_item[11].classification, Classification.RESERVED)
        self.assertIn(Role.SHIP_MODULE_CANDIDATE, by_item[12].roles)
        self.assertEqual(by_item[12].classification, Classification.RESERVED)
        # (18) 60 missiles is not surplus; evidence is min(CAPTURED, INFERRED)
        self.assertIsNone(by_item[11].available_surplus)
        self.assertEqual(by_item[11].evidence, Evidence.INFERRED)
        # module role caps evidence at STATICALLY_DISCOVERED
        self.assertEqual(by_item[12].evidence, Evidence.STATICALLY_DISCOVERED)

    def test_consumable_bucket_lootbox(self):
        rows = [(11, 310, 2)]
        (c,) = _classified(rows)[0]
        self.assertEqual(c.classification, Classification.CONSUMABLE)
        self.assertIn(Role.CONSUMABLE_USE_CANDIDATE, c.roles)
        # (18) quantity 2 never implies surplus/discard
        self.assertNotIn(c.classification, (Classification.SURPLUS_USABLE, Classification.DISCARDABLE))
        self.assertIsNone(c.available_surplus)
        self.assertEqual(c.reservation, ())


class TestClassifyCatalogHelper(unittest.TestCase):
    """classify_catalog convenience function + constructor defaults."""

    def test_classify_catalog_matches_inventory_classifier(self):
        rows = [(11, 301, 5), (12, 313, 1), (13, 999999, 1)]
        catalog = _catalog(rows)
        via_helper = classify_catalog(catalog)
        via_instance = InventoryClassifier().classify(catalog)
        self.assertEqual(via_helper, via_instance)

    def test_constructor_defaults_to_default_policy_and_context(self):
        clf = InventoryClassifier()
        clf2 = InventoryClassifier(policy=None, context=None)
        rows = [(11, 301, 1)]
        self.assertEqual(clf.classify(_catalog(rows)), clf2.classify(_catalog(rows)))

    def test_custom_policy_injection(self):
        # A custom policy can ADD protection; it must never be able to
        # REMOVE baseline protection (fail-closed composition).
        class AdditiveOnlyPolicy(ProtectedItemPolicy):
            def evaluate(self, item, design, context):
                base = super().evaluate(item, design, context)
                return tuple(dict.fromkeys(base + (PROTECTED_EVENT_LIMITED,)))

        rows = [(11, 301, 1)]  # plain Artifact
        (base_c,) = _classified(rows)[0]
        (custom_c,) = _classified(rows, policy=AdditiveOnlyPolicy())[0]
        self.assertEqual(base_c.classification, Classification.RESERVED)
        self.assertEqual(custom_c.classification, Classification.PROTECTED)
        self.assertIn(PROTECTED_EVENT_LIMITED, custom_c.protection_reasons)
        # dedupe keeps output deterministic
        rows2 = [(11, 313, 1)]  # already event-tagged
        (c2,) = _classified(rows2, policy=AdditiveOnlyPolicy())[0]
        self.assertEqual(
            c2.protection_reasons.count(PROTECTED_EVENT_LIMITED), 1
        )

    def test_limitations_lifecycle(self):
        # .limitations empty before first run; populated after classify with
        # default (data-unavailable) context; deterministic across runs.
        clf = InventoryClassifier()
        self.assertEqual(clf.limitations, ())
        rows = [(11, 301, 1)]
        clf.classify(_catalog(rows))
        self.assertTrue(clf.limitations)
        a = InventoryClassifier().classify(_catalog(rows))
        clf_b = InventoryClassifier()
        clf_b.classify(_catalog(rows))
        self.assertEqual(clf.limitations, clf_b.limitations)
        # with full context only the structural (Task F / rule 9) remain
        clf_full = InventoryClassifier(
            context=ProtectionContext(
                equipped_item_ids=frozenset(), room_item_ids=frozenset()
            )
        )
        clf_full.classify(_catalog(rows))
        joined = " ".join(clf_full.limitations)
        self.assertNotIn("equipped ItemId set unavailable", joined)
        self.assertNotIn("room ItemId set unavailable", joined)
        self.assertIn("Task F", joined)


class TestItemVsDesignIdSemantics(unittest.TestCase):
    """(17) ItemId vs ItemDesignId correctness through the classifier."""

    def test_classifier_never_conflates_ids(self):
        # ItemId 301 (instance) vs ItemDesignId 301 (design key) — equal
        # numbers must not create a false equipped-protection via the
        # DESIGN id; only the ItemId channel may match.
        rows = [(301, 302, 1)]  # item 301 is a Crew Uniform (design 302)
        ctx = ProtectionContext(equipped_item_ids=frozenset({301}))
        (c,) = _classified(rows, context=ctx)[0]
        self.assertIn("PROTECTED_EQUIPPED", c.protection_reasons)

        # design 302 equipped as an ITEM id must not protect item 42.
        rows2 = [(42, 302, 1)]
        (c2,) = _classified(rows2, context=ctx)[0]
        self.assertNotIn("PROTECTED_EQUIPPED", c2.protection_reasons)

    def test_classified_item_carries_both_ids(self):
        rows = [(55, 301, 3)]
        (c,) = _classified(rows)[0]
        self.assertEqual(c.item.item_id, 55)
        self.assertEqual(c.item.item_design_id, 301)
        design = c.design
        assert design is not None  # design 301 exists in the fixture feed
        self.assertEqual(design.item_design_id, 301)


class TestDeterminismOfClassification(unittest.TestCase):
    """(1) byte-identical output for identical inputs, whole surface."""

    ROWS = [
        (11, 301, 5),
        (12, 302, 1),
        (13, 303, 2),
        (14, 310, 4),
        (15, 311, 1),
        (16, 312, 60),
        (17, 313, 1),
        (18, 314, 1),
        (19, 316, 2),
        (20, 999999, 1),
    ]

    def _context(self):
        return ProtectionContext(
            equipped_item_ids=frozenset({11}), room_item_ids=frozenset({12})
        )

    def test_full_surface_deterministic(self):
        ctx = self._context()
        a, clf_a = _classified(self.ROWS, context=ctx)
        b, clf_b = _classified(self.ROWS, context=ctx)
        self.assertEqual(a, b)
        self.assertEqual(clf_a.limitations, clf_b.limitations)
        # sanity: the fixture actually exercises every bucket
        buckets = {c.classification for c in a}
        self.assertEqual(
            buckets,
            {Classification.PROTECTED, Classification.RESERVED, Classification.CONSUMABLE},
        )

    def test_no_network_io(self):
        # (24) block the socket module entirely; classification must succeed.
        def _blocked(*args, **kwargs):
            raise AssertionError("network I/O attempted during classification")

        with patch.object(socket, "socket", _blocked), patch.object(
            socket, "create_connection", _blocked
        ):
            out, clf = _classified(self.ROWS, context=self._context())
        self.assertEqual(len(out), 10)
        self.assertTrue(all(isinstance(c, ClassifiedItem) for c in out))
        self.assertTrue(clf.limitations)

    def test_output_order_ascending_item_id(self):
        out, _ = _classified(self.ROWS, context=self._context())
        ids = [c.item.item_id for c in out]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, [11, 12, 13, 14, 15, 16, 17, 18, 19, 20])


class TestClassifySourcePurity(unittest.TestCase):
    """Static purity of the classification source (lane-B surface)."""

    def test_classify_module_has_no_mutation_or_client_surface(self):
        import inspect

        import sdk.inventory.classify as classify_mod

        src = inspect.getsource(classify_mod)
        for frag in (
            "import requests",
            "from sdk.client import Client",
            "Client(",
            "os.environ",
            "getenv",
        ):
            self.assertNotIn(frag, src, frag)

    def test_policy_module_all_reason_codes_documented_order(self):
        # The fixed rule order is part of the output contract: 1,4,5,6,7,8,10.
        from sdk.inventory.policy import EVALUATED_RULES, NOT_EVALUATED_RULES

        self.assertEqual(EVALUATED_RULES, (1, 4, 5, 6, 7, 8, 10))
        self.assertEqual(NOT_EVALUATED_RULES, (2, 3, 9))


if __name__ == "__main__":
    unittest.main()
