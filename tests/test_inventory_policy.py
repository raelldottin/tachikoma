#!/usr/bin/env python3
"""Task D lane C tests: protected-item fail-closed policy + adversarial
safety coverage (tests/test_inventory_policy.py).

All inputs are sanitized/synthetic dictionaries. NO live Pixel Starships
traffic; tests never import requests, never hit the network, and never
attempt a destructive call (repo rule; safety invariants S1/S2).

Covers the brief's TEST REQUIREMENTS 1-24 for the classification/policy
surface, with emphasis on the adversarial/fail-closed cases owned by this
lane:

 3. unknown design -> protected
 4. missing ItemDesign -> protected
 5. malformed/partial metadata -> fail closed
13. evidence = minimum across required inputs
14. evidence cannot be accidentally promoted
15. deterministic reason-code ordering
17. ItemId versus ItemDesignId correctness
18. quantity alone never creates surplus/discard classification
19. no Task F reservation behavior exists
20. no Task G surplus/reduction action exists
21. `inventory apply` still always refuses
22. no mutation endpoint becomes reachable
23. environment variables/config cannot bypass refusal
24. classifier performs no network I/O
"""

import os
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
    Item,
    ItemDesign,
    ProtectionContext,
    ProtectedItemPolicy,
    Role,
    ROLES_FORBIDDEN_THIS_SLICE,
)

# ---------------------------------------------------------------------------
# Sanitized fixtures (shape of captured data; all values synthetic)
# ---------------------------------------------------------------------------

POLICY_FEED_XML = """<DesignService xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
  <ListAllDynamicDesigns>
    <ItemDesigns>
      <ItemDesign ItemDesignId="201" ItemDesignName="Plain Scrap" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="202" ItemDesignName="Rare Widget" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Rare" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="203" ItemDesignName="Event Hat" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Common" Ingredients="" Tags="Event"/>
      <ItemDesign ItemDesignId="204" ItemDesignName="Quest Blueprint" ItemType="Blueprint" ItemSubType="Blueprint" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="205" ItemDesignName="VIP Charm" ItemType="Equipment" ItemSubType="EquipmentAccessory" Rarity="Common" Ingredients="" Tags="vip"/>
      <ItemDesign ItemDesignId="206" ItemDesignName="Protein Shake" ItemType="Consumable" ItemSubType="InstantTraining" Rarity="Common" Ingredients="" TrainingDesignId="9" Tags=""/>
      <ItemDesign ItemDesignId="207" ItemDesignName="Loot Box" ItemType="Prize" ItemSubType="InstantPrize" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="208" ItemDesignName="Ion Rocket" ItemType="Missile" ItemSubType="Missile" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="209" ItemDesignName="Armor Module" ItemType="Module" ItemSubType="Module" ModuleType="Armor" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="210" ItemDesignName="Mystery Consumable" ItemType="Consumable" ItemSubType="Consumable" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="211" ItemDesignName="Immunity Field" ItemType="Consumable" ItemSubType="Immunity" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="212" ItemDesignName="Crafted Helm" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Common" Ingredients="201x5" Tags=""/>
      <ItemDesign ItemDesignId="213" ItemDesignName="Empty Rarity" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="214" ItemDesignName="Alien Relic" ItemType="Relic" ItemSubType="Relic" Rarity="Zealous" Ingredients="" Tags=""/>
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
                            "@ShipId": "55",
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


def _catalog(rows, feed_xml=POLICY_FEED_XML):
    designs = DesignCatalog.from_dynamic_dict(xmltodict.parse(feed_xml))
    return InventoryCatalog.from_dicts(_items_response(rows), designs)


def _classified(catalog_rows, context=None, feed_xml=POLICY_FEED_XML):
    catalog = _catalog(catalog_rows, feed_xml)
    clf = InventoryClassifier(context=context)
    return clf.classify(catalog), clf


class TestPolicyRules(unittest.TestCase):
    """Direct rule-level tests: fixed order, ALL matched codes, fail-closed."""

    def setUp(self):
        self.policy = ProtectedItemPolicy()
        self.ctx = ProtectionContext()
        self.designs = DesignCatalog.from_dynamic_dict(xmltodict.parse(POLICY_FEED_XML))

    def _item(self, item_id, design_id, qty=1):
        return Item(
            item_id=item_id,
            ship_id=55,
            item_design_id=design_id,
            quantity=qty,
            is_new=False,
            skin_key=None,
            raw={},
        )

    def test_unknown_design_protected_rule_10(self):
        # (3) unknown design -> protected; nothing else can fire
        reasons = self.policy.evaluate(self._item(1, 999999), None, self.ctx)
        self.assertEqual(reasons, ("PROTECTED_UNKNOWN_DESIGN",))

    def test_rule_1_equipped(self):
        d = self.designs.get(201)  # Common artifact, no markers
        ctx = ProtectionContext(equipped_item_ids=frozenset({11}))
        reasons = self.policy.evaluate(self._item(11, 201), d, ctx)
        self.assertEqual(reasons, ("PROTECTED_EQUIPPED",))

    def test_rule_4_room_module(self):
        d = self.designs.get(201)
        ctx = ProtectionContext(room_item_ids=frozenset({12}))
        reasons = self.policy.evaluate(self._item(12, 201), d, ctx)
        self.assertEqual(reasons, ("PROTECTED_ROOM_MODULE",))

    def test_rule_5_rarity_protected_values(self):
        for rarity in ("Rare", "Epic", "Hero", "Unique", "Legendary"):
            d = ItemDesign(item_design_id=300, item_name="x", rarity=rarity)
            reasons = self.policy.evaluate(self._item(1, 300), d, self.ctx)
            self.assertIn("PROTECTED_RARITY", reasons, rarity)

    def test_rule_5_rarity_common_uncommon_not_protected(self):
        for rarity in ("Common", "Uncommon"):
            d = ItemDesign(item_design_id=300, item_name="x", rarity=rarity)
            reasons = self.policy.evaluate(self._item(1, 300), d, self.ctx)
            self.assertNotIn("PROTECTED_RARITY", reasons, rarity)
            self.assertNotIn("PROTECTED_INSUFFICIENT_EVIDENCE", reasons)

    def test_rule_5_empty_or_unrecognized_rarity_fails_closed(self):
        # (5) malformed/partial metadata -> fail closed
        for rarity in ("", "   ", "Zealous", "unknown-rarity"):
            d = ItemDesign(item_design_id=300, item_name="x", rarity=rarity)
            reasons = self.policy.evaluate(self._item(1, 300), d, self.ctx)
            self.assertIn("PROTECTED_INSUFFICIENT_EVIDENCE", reasons, repr(rarity))

    def test_rule_6_event_tags(self):
        for tag in ("Event", "vip", "robotrader", "harvestor01", "threewins", "Unique"):
            d = ItemDesign(item_design_id=300, item_name="x", tags=(tag,))
            reasons = self.policy.evaluate(self._item(1, 300), d, self.ctx)
            self.assertIn("PROTECTED_EVENT_LIMITED", reasons, tag)

    def test_rule_7_quest_blueprint(self):
        d = self.designs.get(204)
        reasons = self.policy.evaluate(self._item(1, 204), d, self.ctx)
        self.assertEqual(reasons, ("PROTECTED_QUEST_PROGRESSION",))

    def test_rule_8_premium(self):
        d = self.designs.get(205)  # tags vip
        reasons = self.policy.evaluate(self._item(1, 205), d, self.ctx)
        self.assertIn("PROTECTED_PREMIUM", reasons)
        self.assertIn("PROTECTED_EVENT_LIMITED", reasons)  # vip matches rule 6 too

    def test_all_matched_reasons_returned_no_short_circuit(self):
        # (2) multiple simultaneous classifications; reason SET preserved
        d = ItemDesign(
            item_design_id=300,
            item_name="x",
            rarity="Legendary",  # rule 5
            tags=("Event",),  # rule 6
            item_type="Blueprint",  # rule 7
        )
        ctx = ProtectionContext(equipped_item_ids=frozenset({7}), room_item_ids=frozenset({7}))
        reasons = self.policy.evaluate(self._item(7, 300), d, ctx)
        # Fixed rule order 1,4,5,6,7,(8),10
        self.assertEqual(
            reasons,
            (
                "PROTECTED_EQUIPPED",
                "PROTECTED_ROOM_MODULE",
                "PROTECTED_RARITY",
                "PROTECTED_EVENT_LIMITED",
                "PROTECTED_QUEST_PROGRESSION",
            ),
        )

    def test_reason_code_order_deterministic(self):
        # (15) deterministic reason-code ordering — same input, same order
        d = ItemDesign(item_design_id=300, item_name="x", rarity="Epic", tags=("Event",))
        ctx = ProtectionContext(equipped_item_ids=frozenset({7}))
        a = self.policy.evaluate(self._item(7, 300), d, ctx)
        b = self.policy.evaluate(self._item(7, 300), d, ctx)
        self.assertEqual(a, b)
        self.assertEqual(a, ("PROTECTED_EQUIPPED", "PROTECTED_RARITY", "PROTECTED_EVENT_LIMITED"))

    def test_missing_context_none_means_rule_cannot_fire(self):
        # None (unavailable) != frozenset() (positively empty). With None the
        # rule cannot fire and a limitation is exposed — never a silent
        # negative that reduces protection.
        d = self.designs.get(201)
        reasons = self.policy.evaluate(self._item(1, 201), d, ProtectionContext())
        self.assertEqual(reasons, ())
        from sdk.inventory.policy import policy_limitations

        lims = policy_limitations(ProtectionContext())
        self.assertTrue(any("equipped ItemId set unavailable" in s for s in lims))
        self.assertTrue(any("room ItemId set unavailable" in s for s in lims))

    def test_quantity_never_affects_protection(self):
        # (18) quantity alone never creates surplus/discard classification
        for qty in (0, 1, 9, 999999):
            d = self.designs.get(201)
            reasons = self.policy.evaluate(self._item(1, 201, qty), d, self.ctx)
            self.assertEqual(reasons, (), qty)


class TestClassifierClassification(unittest.TestCase):
    """Classifier-level deterministic classification and evidence tests."""

    def test_known_designs_classified_deterministically(self):
        # (1) deterministic classification of known designs
        rows = [(11, 201, 5), (12, 202, 1), (13, 206, 3)]
        a, _ = _classified(rows)
        b, _ = _classified(rows)
        self.assertEqual(a, b)
        by_id = {c.item.item_id: c for c in a}
        self.assertEqual(by_id[11].classification, Classification.RESERVED)  # crafting ingredient
        self.assertEqual(by_id[12].classification, Classification.PROTECTED)  # Rare
        self.assertEqual(by_id[13].classification, Classification.RESERVED)  # training
        self.assertEqual([c.item.item_id for c in a], [11, 12, 13])  # ascending ItemId

    def test_multiple_roles_on_one_design(self):
        # (2) multiple simultaneous classifications on one design:
        # training material role + consumable-subtype overlap checks
        rows = [(11, 206, 2)]
        (c,) = _classified(rows)[0]
        self.assertIn(Role.CREW_TRAINING_MATERIAL, c.roles)
        # training bucket is RESERVED, not consumable, when both match
        self.assertEqual(c.classification, Classification.RESERVED)
        self.assertTrue(
            any("training material" in r for r in c.classification_reasons)
        )

    def test_consumable_classification(self):
        # (10) consumable classification (lootbox, no protection fired)
        rows = [(11, 207, 4)]
        (c,) = _classified(rows)[0]
        self.assertEqual(c.classification, Classification.CONSUMABLE)
        self.assertIn(Role.CONSUMABLE_USE_CANDIDATE, c.roles)
        # (18) quantity 4 doesn't make it surplus/discard
        self.assertNotIn(Classification.SURPLUS_USABLE, (c.classification,))
        self.assertIsNone(c.available_surplus)

    def test_true_consumable_subtype_is_role_and_protected(self):
        # synthesis §4: the 3 literal ItemSubType=Consumable designs are
        # role=consumable; the captured ones are Special/Rare/Unique-tagged
        # and thus PROTECTED — protection from markers, role independent.
        rows = [(11, 210, 2)]
        (c,) = _classified(rows)[0]
        self.assertIn(Role.CONSUMABLE_USE_CANDIDATE, c.roles)
        self.assertEqual(c.classification, Classification.CONSUMABLE)
        # same subtype WITH the captured marker profile -> PROTECTED
        marked_feed = POLICY_FEED_XML.replace(
            'ItemDesignId="210" ItemDesignName="Mystery Consumable" ItemType="Consumable" '
            'ItemSubType="Consumable" Rarity="Common"',
            'ItemDesignId="210" ItemDesignName="Mystery Consumable" ItemType="Consumable" '
            'ItemSubType="Consumable" Rarity="Unique"',
        )
        (c2,) = _classified(rows, feed_xml=marked_feed)[0]
        self.assertIn(Role.CONSUMABLE_USE_CANDIDATE, c2.roles)
        self.assertEqual(c2.classification, Classification.PROTECTED)
        self.assertIn("PROTECTED_RARITY", c2.protection_reasons)

    def test_equipment_and_module_roles(self):
        # (11) equipment/module classification where data supports it
        rows = [(11, 202, 1), (12, 209, 1)]
        classified = {c.item.item_id: c for c in _classified(rows)[0]}
        self.assertIn(Role.CREW_EQUIPMENT_CANDIDATE, classified[11].roles)
        self.assertIn(Role.SHIP_MODULE_CANDIDATE, classified[12].roles)
        # module evidence is STATICALLY_DISCOVERED (endpoints uncaptured)
        self.assertEqual(classified[12].evidence, Evidence.STATICALLY_DISCOVERED)

    def test_reasons_reflect_actual_protection_state(self):
        # Regression: classification_reasons must only mention policy
        # protection when a protection reason actually fired — an
        # unprotected item must not carry a "protected by policy" reason.
        rows = [(11, 207, 1)]  # lootbox: no protection fires
        (c,) = _classified(rows)[0]
        self.assertEqual(c.protection_reasons, ())
        self.assertFalse(c.protected)
        self.assertFalse(
            any("protected by policy" in r for r in c.classification_reasons)
        )
        # ...and a protected item's reasons must name the fired codes
        rows2 = [(11, 202, 1)]  # Rare -> PROTECTED_RARITY
        (c2,) = _classified(rows2)[0]
        self.assertIn("protected by policy rules: PROTECTED_RARITY", c2.classification_reasons)

    def test_ammunition_classification(self):
        # (12) ammunition/manufacturing classification
        rows = [(11, 208, 50)]
        (c,) = _classified(rows)[0]
        self.assertIn(Role.AMMUNITION_MATERIAL, c.roles)
        self.assertEqual(c.classification, Classification.RESERVED)
        # (18) 50 missiles is not surplus
        self.assertIsNone(c.available_surplus)
        self.assertNotEqual(c.classification, Classification.SURPLUS_USABLE)

    def test_crafting_ingredient_and_intermediate(self):
        # (8)/(9) crafting ingredient + intermediate classifications
        rows = [(11, 201, 7), (12, 212, 1)]
        classified = {c.item.item_id: c for c in _classified(rows)[0]}
        self.assertIn(Role.CRAFTING_INGREDIENT, classified[11].roles)
        self.assertIn(Role.CRAFTING_INTERMEDIATE, classified[12].roles)

    def test_unknown_design_fails_closed_in_classifier(self):
        # (3) unknown design -> PROTECTED + UNKNOWN evidence + unknown flag
        rows = [(11, 999999, 5)]
        (c,) = _classified(rows)[0]
        self.assertEqual(c.classification, Classification.PROTECTED)
        self.assertTrue(c.unknown)
        self.assertIsNone(c.design)
        self.assertEqual(c.evidence, Evidence.UNKNOWN)
        self.assertEqual(c.protection_reasons, ("PROTECTED_UNKNOWN_DESIGN",))
        self.assertTrue(c.protected)

    def test_missing_design_metadata_fails_closed(self):
        # (4) missing ItemDesign -> protected (catalog with no design
        # catalog resolves everything to None)
        catalog = InventoryCatalog.from_dicts(_items_response([(11, 201, 1)]), None)
        (c,) = InventoryClassifier().classify(catalog)
        self.assertEqual(c.classification, Classification.PROTECTED)
        self.assertTrue(c.unknown)
        self.assertEqual(c.evidence, Evidence.UNKNOWN)

    def test_partial_metadata_never_less_protected(self):
        # (5) a malformed/partial record must not become less protected than
        # a complete record: empty-rarity and unrecognized-rarity designs
        # land PROTECTED via INSUFFICIENT_EVIDENCE
        rows = [(11, 213, 1), (12, 214, 1), (13, 201, 1)]
        classified = {c.item.item_id: c for c in _classified(rows)[0]}
        self.assertEqual(classified[11].classification, Classification.PROTECTED)
        self.assertIn("PROTECTED_INSUFFICIENT_EVIDENCE", classified[11].protection_reasons)
        self.assertEqual(classified[12].classification, Classification.PROTECTED)
        self.assertIn("PROTECTED_INSUFFICIENT_EVIDENCE", classified[12].protection_reasons)
        # complete Common record is the only non-protected one
        self.assertNotEqual(classified[13].classification, Classification.PROTECTED)

    def test_itemid_vs_itemdesignid_correctness(self):
        # (17) ItemId versus ItemDesignId correctness: equipped sets match
        # ItemId, never ItemDesignId
        rows = [(201, 201, 1)]  # ItemId 201, design 201 — same number!
        ctx = ProtectionContext(equipped_item_ids=frozenset({201}))
        (c,) = _classified(rows, context=ctx)[0]
        self.assertIn("PROTECTED_EQUIPPED", c.protection_reasons)
        # but a DIFFERENT item whose ItemDesignId equals an equipped ItemId
        # is NOT protected by rule 1
        rows2 = [(999, 201, 1)]
        ctx2 = ProtectionContext(equipped_item_ids=frozenset({201}))
        (c2,) = _classified(rows2, context=ctx2)[0]
        self.assertNotIn("PROTECTED_EQUIPPED", c2.protection_reasons)


class TestAdversarialMarkers(unittest.TestCase):
    """Adversarial marker-matching semantics found during implementation
    (regression coverage): exact-token matching, case handling, whitespace."""

    def _evaluate(self, design):
        return ProtectedItemPolicy().evaluate(
            Item(item_id=1, ship_id=1, item_design_id=design.item_design_id,
                 quantity=1, is_new=False, skin_key=None, raw={}),
            design,
            ProtectionContext(),
        )

    def test_substrings_do_not_match_markers(self):
        # tags that CONTAIN markers as substrings must NOT match: matching
        # is exact-token, not substring
        d = ItemDesign(item_design_id=3, item_name="x", rarity="Common",
                       tags=("eventful", "vipguest", "ubercool"))
        self.assertEqual(self._evaluate(d), ())

    def test_marker_matching_case_insensitive(self):
        d = ItemDesign(item_design_id=4, item_name="x", rarity="COMMON", tags=("VIP",))
        reasons = self._evaluate(d)
        self.assertIn("PROTECTED_EVENT_LIMITED", reasons)
        self.assertIn("PROTECTED_PREMIUM", reasons)

    def test_whitespace_rarity_fails_closed(self):
        d = ItemDesign(item_design_id=5, item_name="x", rarity="   ")
        self.assertIn("PROTECTED_INSUFFICIENT_EVIDENCE", self._evaluate(d))

    def test_rarity_case_insensitive(self):
        for rarity in ("RARE", "Epic", "HERO", "unique", "Legendary"):
            d = ItemDesign(item_design_id=6, item_name="x", rarity=rarity)
            self.assertIn("PROTECTED_RARITY", self._evaluate(d), rarity)

    def test_extreme_quantities_change_nothing(self):
        # (18) negative/zero/huge quantity at the policy boundary
        d = ItemDesign(item_design_id=7, item_name="x", rarity="Common")
        for qty in (-5, 0, 10**12):
            it = Item(item_id=1, ship_id=1, item_design_id=7, quantity=qty,
                      is_new=False, skin_key=None, raw={})
            self.assertEqual(ProtectedItemPolicy().evaluate(it, d, ProtectionContext()), ())

    def test_empty_metadata_design_protected_in_classifier(self):
        # a design row that exists but carries empty strings for every
        # field must land PROTECTED via INSUFFICIENT_EVIDENCE — never crash
        # and never pass through unprotected
        feed = (
            '<D><ListAllDynamicDesigns><ItemDesigns>'
            '<ItemDesign ItemDesignId="1" ItemDesignName="a" ItemType="" '
            'ItemSubType="" Rarity="" Ingredients="" Tags=""/>'
            "</ItemDesigns></ListAllDynamicDesigns></D>"
        )
        designs = DesignCatalog.from_dynamic_dict(xmltodict.parse(feed))
        catalog = InventoryCatalog.from_dicts(
            {
                "ItemService": {
                    "ListItemsOfAShip": {
                        "Items": {
                            "Item": {
                                "@ItemId": "10",
                                "@ShipId": "1",
                                "@ItemDesignId": "1",
                                "@Quantity": "5",
                                "@IsNew": "false",
                                "@SkinKey": "",
                            }
                        }
                    }
                }
            },
            designs,
        )
        (c,) = InventoryClassifier().classify(catalog)
        self.assertEqual(c.classification, Classification.PROTECTED)
        self.assertIn("PROTECTED_INSUFFICIENT_EVIDENCE", c.protection_reasons)


class TestEvidencePropagation(unittest.TestCase):
    """(13) evidence = minimum across required inputs; (14) never promoted."""

    def test_module_evidence_is_static_not_promoted(self):
        rows = [(11, 209, 1)]
        (c,) = _classified(rows)[0]
        # module role requires STATICALLY_DISCOVERED; item/design rows are
        # CAPTURED_NOT_VERIFIED; min(...) = STATICALLY_DISCOVERED
        self.assertEqual(c.evidence, Evidence.STATICALLY_DISCOVERED)

    def test_marker_roles_evidence_inferred(self):
        rows = [(11, 207, 1)]  # InstantPrize -> consumable role INFERRED
        (c,) = _classified(rows)[0]
        self.assertEqual(c.evidence, Evidence.INFERRED)

    def test_training_evidence_captured_not_verified(self):
        rows = [(11, 206, 1)]
        (c,) = _classified(rows)[0]
        self.assertEqual(c.evidence, Evidence.CAPTURED_NOT_VERIFIED)

    def test_evidence_min_across_inputs(self):
        # design 212 has BOTH an ingredient-derived role (INFERRED) and is
        # Equipment (INFERRED): min is INFERRED, not stronger
        rows = [(11, 212, 1)]
        (c,) = _classified(rows)[0]
        self.assertEqual(c.evidence, Evidence.INFERRED)

    def test_evidence_never_promoted_above_weakest_input(self):
        # (14) combine a strong input (VERIFIED) with module (STATIC) —
        # weaker() must return STATIC, never VERIFIED
        self.assertEqual(
            Evidence.weaker(Evidence.VERIFIED, Evidence.STATICALLY_DISCOVERED),
            Evidence.STATICALLY_DISCOVERED,
        )
        self.assertEqual(
            Evidence.weakest(
                [Evidence.VERIFIED, Evidence.CAPTURED_NOT_VERIFIED, Evidence.INFERRED]
            ),
            Evidence.INFERRED,
        )
        # unknown design => UNKNOWN regardless
        rows = [(11, 999999, 1)]
        (c,) = _classified(rows)[0]
        self.assertEqual(c.evidence, Evidence.UNKNOWN)

    def test_weakest_empty_is_unknown_fail_closed(self):
        self.assertEqual(Evidence.weakest([]), Evidence.UNKNOWN)

    def test_evidence_helper_strictness(self):
        # non-member inputs are programming errors, not silently coerced
        with self.assertRaises((TypeError, ValueError)):
            Evidence.weaker(Evidence.VERIFIED, "INFERRED")  # type: ignore[arg-type]
        with self.assertRaises((TypeError, ValueError)):
            Evidence.weaker("VERIFIED", Evidence.VERIFIED)  # type: ignore[arg-type]


class TestTaskFGBoundaries(unittest.TestCase):
    """(19) no Task F reservation behavior; (20) no Task G surplus."""

    def test_no_reservation_computed(self):
        # (19) reservation is always empty; no ReservationPlan exists
        rows = [(11, 201, 100), (12, 206, 100)]
        classified, _ = _classified(rows)
        for c in classified:
            self.assertEqual(c.reservation, ())
        import sdk.inventory as inv

        self.assertFalse(hasattr(inv, "ReservationPlan"))
        self.assertFalse(hasattr(inv, "InventoryDemandPlanner"))

    def test_no_surplus_or_reduction_semantics(self):
        # (20) available_surplus always None; no reduction/transfer/executor
        rows = [(11, 201, 100000)]
        classified, _ = _classified(rows)
        for c in classified:
            self.assertIsNone(c.available_surplus)
        import sdk.inventory as inv

        for forbidden in ("InventoryReductionPlanner", "TransferPlanner", "InventoryExecutor"):
            self.assertFalse(hasattr(inv, forbidden), forbidden)

    def test_forbidden_classifications_never_assigned(self):
        rows = [(11, 201, 999999), (12, 207, 999999), (13, 206, 999999)]
        classified, _ = _classified(rows)
        forbidden = (
            Classification.SURPLUS_USABLE,
            Classification.SURPLUS_CRAFTABLE,
            Classification.TRANSFERABLE,
            Classification.SELLABLE,
            Classification.DISCARDABLE,
        )
        forbidden_roles = set(ROLES_FORBIDDEN_THIS_SLICE)
        for c in classified:
            self.assertNotIn(c.classification, forbidden)
            for r in c.roles:
                self.assertNotIn(r, forbidden_roles)

    def test_high_quantity_never_discardable(self):
        # (18) quantity alone never creates surplus/discard classification
        rows = [(11, 201, 10**9)]
        (c,) = _classified(rows)[0]
        self.assertNotEqual(c.classification, Classification.DISCARDABLE)
        self.assertNotEqual(c.classification, Classification.SURPLUS_USABLE)


class TestPurityAndSafety(unittest.TestCase):
    """(21)-(24): apply refusal, mutation unreachability, env bypass,
    no network I/O."""

    def _registry(self):
        from sdk.commands import CommandRegistry
        from sdk.client import Client
        from unittest.mock import MagicMock

        client = MagicMock(spec=Client)
        client.dynamicDesigns = xmltodict.parse(POLICY_FEED_XML)
        client.itemsOfAShip = _items_response([(11, 201, 1)])
        return CommandRegistry(client)

    def test_classified_helper_returns_tuples(self):
        rows = [(11, 201, 1)]
        out = _classified(rows)
        self.assertEqual(len(out), 2)
        classified = out[0]
        self.assertEqual(len(classified), 1)
        self.assertIsInstance(classified[0], ClassifiedItem)

    def test_inventory_apply_still_refuses(self):
        # (21) `inventory apply` still always refuses
        handler = self._registry().get("inventory apply")["handler"]
        for args in ([], ["--force"], ["--yes"], ["now"], ["sell", "11"], ["delete", "11"]):
            out = handler(args)
            self.assertIn("REFUSED", out, args)
            self.assertIn("Phase 8", out)
            self.assertIn("Phase 9", out)

    def test_inventory_apply_verified_flag_false(self):
        registry = self._registry()
        self.assertFalse(registry.is_verified("inventory apply"))

    def test_env_vars_cannot_bypass_refusal(self):
        # (23) environment variables/config cannot bypass refusal
        for var in (
            "INVENTORY_ENABLE_APPLY",
            "PSS_ENABLE_INVENTORY_MUTATIONS",
            "INVENTORY_APPLY",
            "PSS_INVENTORY_APPLY",
            "TACHIKOMA_INVENTORY_MUTATIONS",
        ):
            os.environ[var] = "1"
            try:
                out = self._registry().get("inventory apply")["handler"]([])
                self.assertIn("REFUSED", out, var)
            finally:
                del os.environ[var]
        # config-shaped section names likewise have no effect: the stub
        # reads nothing (verified by reading the source text)
        import inspect

        import sdk.commands as commands_mod

        src = inspect.getsource(commands_mod.CommandRegistry._cmd_inventory_apply)
        self.assertNotIn("os.environ", src)
        self.assertNotIn("getenv", src)

    def test_no_mutation_endpoint_reachable_from_classifier(self):
        # (22) no mutation endpoint becomes reachable: the classifier and
        # policy modules contain no Client construction, no requests, no
        # mutation URL fragments
        import sdk.inventory.classify as classify_mod
        import sdk.inventory.policy as policy_mod

        for mod in (classify_mod, policy_mod):
            assert mod.__file__ is not None
            with open(mod.__file__, encoding="utf-8") as f:
                src = f.read()
            for frag in (
                "ConsumeItem2",
                "ActivateItem3",
                "ActivateItem4",
                "CombineItem4",
                "DeleteItem3",
                "GiftItem",
                "SellItem3",
                "Purchase4",
                "WearItem",
                "UnequipItems",
                "AddItemIdToRoom",
                "MoveItemIdToRoom3",
                "RemoveItemIdFromRoom",
                "UpdateItemIdsForRoom2",
                "Refill2",
                "RepairBrokenModules",
                "DeleteAmmo",
            ):
                self.assertNotIn(frag, src, f"{mod.__name__} references {frag}")
            self.assertNotIn("import requests", src)
            self.assertNotIn("from sdk.client import Client", src)
            self.assertNotIn("Client(", src)

    def test_classifier_performs_no_network_io(self):
        # (24) classifier performs no network I/O: block the socket module
        # entirely and classify — must succeed without touching it.
        def _blocked(*args, **kwargs):
            raise AssertionError("network I/O attempted during classification")

        rows = [(11, 201, 1), (12, 202, 1), (13, 999999, 1)]
        with patch.object(socket, "socket", _blocked), patch.object(
            socket, "create_connection", _blocked
        ):
            classified, clf = _classified(rows)
        self.assertEqual(len(classified), 3)
        self.assertTrue(all(isinstance(c, ClassifiedItem) for c in classified))
        self.assertTrue(clf.limitations)  # limitations exposed, not hidden

    def test_client_py_still_never_imports_inventory(self):
        # architecture boundary: one-way dependency preserved
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "sdk", "client.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("import sdk.inventory", src)
        self.assertNotIn("from sdk.inventory", src)

    def test_limitations_exposed_not_invented(self):
        # unavailability is exposed; no protection value is invented
        from sdk.inventory.policy import policy_limitations

        lims = policy_limitations(ProtectionContext())
        self.assertEqual(len(lims), 5)  # rules 2, 3, 9 + both context sets
        joined = " ".join(lims)
        self.assertIn("Task F", joined)
        self.assertIn("ListSalesByItemDesignId", joined)


class TestDeterminism(unittest.TestCase):
    """Byte-level determinism of the whole classification surface."""

    def test_same_inputs_identical_outputs(self):
        rows = [
            (11, 201, 5),
            (12, 202, 1),
            (13, 203, 1),
            (14, 204, 1),
            (15, 205, 1),
            (16, 206, 2),
            (17, 999999, 1),
        ]
        ctx = ProtectionContext(
            equipped_item_ids=frozenset({11}), room_item_ids=frozenset({12})
        )
        a, clf_a = _classified(rows, context=ctx)
        b, clf_b = _classified(rows, context=ctx)
        self.assertEqual(a, b)
        self.assertEqual(clf_a.limitations, clf_b.limitations)

    def test_classified_item_frozen(self):
        rows = [(11, 201, 1)]
        (c,) = _classified(rows)[0]
        with self.assertRaises(Exception):
            c.classification = Classification.DISCARDABLE  # type: ignore[misc]

    def test_roles_declared_order_is_stable(self):
        # (15) roles output order == Role declaration order
        expected = [r for r in Role]
        self.assertEqual(len(expected), 11)
        rows = [(11, 212, 1)]
        (c,) = _classified(rows)[0]
        idx = [expected.index(r) for r in c.roles]
        self.assertEqual(idx, sorted(idx))


if __name__ == "__main__":
    unittest.main()
