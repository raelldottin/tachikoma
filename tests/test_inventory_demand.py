#!/usr/bin/env python3
"""Task F domain tests: demand/reservation ledger (tests/test_inventory_demand.py).

All inputs are sanitized/synthetic dictionaries shaped like the captured
2026-08-08 payloads (attribute names real, values invented). NO live Pixel
Starships traffic: the Client is a MagicMock, sockets are blocked wherever
the domain surface runs, and no destructive call is ever attempted.

Covers the swarm brief's 30 test-requirement points for the DOMAIN lane:
determinism, equipment/module demand from current-state reference sets,
crafting expansion (single-level, recursive, quantity multiplication,
shared intermediates, duplicates, cycles, missing designs, malformed
quantities), unknown-design fail-closed, overlapping-demand rules,
evidence minimum/no-promotion, classification-vs-demand and
quantity-vs-demand separation, protection preservation, ordering, CLI
integration, and the Task G / multi-account / mutation prohibitions.
"""

import socket
import unittest
from unittest.mock import MagicMock, patch

import xmltodict

from sdk.inventory import (
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
    SOURCE_UNSUPPORTED,
    UNKNOWN_DESIGN,
    UNKNOWN_ITEM_REFERENCE,
    UNREASONABLE_QUANTITY,
    DemandPlanner,
    DesignCatalog,
    Evidence,
    InventoryCatalog,
    InventoryClassifier,
    PlanningContext,
    ReservationEntry,
    ReservationLedger,
    ReservationSource,
)

# ---------------------------------------------------------------------------
# Sanitized fixtures — live-shape dynamic feed (D1/D2 attribute names).
# ---------------------------------------------------------------------------

FEED_XML = """<DesignService xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
  <ListAllDynamicDesigns>
    <ItemDesigns>
      <ItemDesign ItemDesignId="501" ItemDesignName="Steel Plate" ItemType="Equipment" ItemSubType="EquipmentBody" Rarity="Common" Ingredients="" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="502" ItemDesignName="Plasma Bolt" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Common" Ingredients="" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="503" ItemDesignName="Brain Stim" ItemType="Consumable" ItemSubType="InstantTraining" Rarity="Common" Ingredients="" TrainingDesignId="77" Tags=""/>
      <ItemDesign ItemDesignId="504" ItemDesignName="Mystery Shard" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="0" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="505" ItemDesignName="Fused Core" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Common" Ingredients="501x2|502x3" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="506" ItemDesignName="Deep Core" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Common" Ingredients="505x2|504x1" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="507" ItemDesignName="Loop A" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="508x1" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="508" ItemDesignName="Loop B" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="507x1" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="509" ItemDesignName="Multi Mix" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Common" Ingredients="501x2|501x3|502x1" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="510" ItemDesignName="Big Ask" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="504x500000000" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="511" ItemDesignName="Dead End" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="999999x1" TrainingDesignId="0" Tags=""/>
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


def _ship_response(characters=(), rooms=()):
    """GetShipByUserId-shaped payload (live shape, synthetic values).

    ``characters``/``rooms`` are sequences of (node, refs) pairs.
    """
    return {
        "ShipService": {
            "GetShipByUserId": {
                "Ship": {
                    "ShipId": "6917397",
                    "Characters": {
                        "Character": [
                            dict(node, **{"@ItemIds": refs}) for node, refs in characters
                        ]
                    },
                    "Rooms": {"Room": [dict(node, **{"@ItemIds": refs}) for node, refs in rooms]},
                }
            }
        }
    }


def _catalog(rows):
    designs = DesignCatalog.from_dynamic_dict(xmltodict.parse(FEED_XML))
    return InventoryCatalog.from_dicts(_items_response(rows), designs)


def _plan(rows, context):
    return DemandPlanner().plan(_catalog(rows), context)


def _codes(ledger, design_id=None):
    return [
        c.code
        for c in ledger.conditions
        if design_id is None or c.design_id == design_id
    ]


class TestDeterministicLedger(unittest.TestCase):
    """Point 1/20: identical inputs -> identical ledgers, stable order."""

    ROWS = [(11, 501, 5), (12, 502, 4), (13, 503, 9)]

    def _context(self):
        ship = _ship_response(
            characters=[({"@CharacterId": "1"}, "11,11,12")],
            rooms=[({"@RoomId": "7"}, "13")],
        )
        return PlanningContext.from_ship_dict(ship)

    def test_plan_is_deterministic(self):
        a = _plan(self.ROWS, self._context())
        b = _plan(self.ROWS, self._context())
        self.assertEqual(a, b)

    def test_entries_sorted_by_design_source_precedence_provenance(self):
        ledger = _plan(
            [(11, 501, 5), (12, 501, 3)],
            PlanningContext(
                equipped_references={11: 1},
                strategic_reserve={501: 2},
            ),
        )
        entries = ledger.entries_for(501)
        self.assertEqual(
            [e.source for e in entries],
            [ReservationSource.EQUIPMENT, ReservationSource.STRATEGIC],
        )
        self.assertLessEqual(entries[0].provenance, entries[1].provenance)

    def test_conditions_sorted_and_deduplicated(self):
        ledger = _plan(
            [(11, 501, 5)],
            PlanningContext(
                equipped_references={11: 3, 12: 3},  # two shared-stack conditions
                strategic_reserve={501: 1, 501: 2},  # duplicate config
            ),
        )
        keys = [(c.code, c.detail, c.design_id) for c in ledger.conditions]
        self.assertEqual(keys, sorted(set(keys)))

    def test_ledger_is_frozen_and_hashable_shape(self):
        ledger = _plan(self.ROWS, self._context())
        with self.assertRaises(Exception):
            ledger.entries = ()
        self.assertIsInstance(ledger, ReservationLedger)


class TestEquipmentDemand(unittest.TestCase):
    """Point 3: EQUIPMENT demand from current-state reference sets."""

    def test_equipped_reference_claims_whole_stack(self):
        ledger = _plan(
            [(11, 501, 5)],
            PlanningContext(equipped_references={11: 1}),
        )
        (entry,) = ledger.entries_for(501)
        self.assertEqual(entry.source, ReservationSource.EQUIPMENT)
        self.assertEqual(entry.requested_quantity, 5)
        self.assertEqual(entry.reserved_quantity, 5)
        self.assertIn("ItemId 11", entry.provenance)

    def test_distinct_stacks_of_one_design_sum(self):
        # Two distinct equipped ItemIds of one design are independent
        # simultaneous claims (overlapping-demand rule).
        ledger = _plan(
            [(11, 501, 3), (12, 501, 2)],
            PlanningContext(equipped_references={11: 1, 12: 1}),
        )
        self.assertEqual(len(ledger.entries_for(501)), 2)
        self.assertEqual(ledger.total_requested(501), 5)
        self.assertEqual(ledger.total_reserved(501), 5)

    def test_repeated_references_dedupe_not_double_count(self):
        # Point 14 / edge E1: one stack referenced by 3 characters is
        # ONE claim of the whole stack, never 3x the quantity.
        ledger = _plan(
            [(11, 501, 5)],
            PlanningContext(equipped_references={11: 3}),
        )
        self.assertEqual(ledger.total_requested(501), 5)
        self.assertIn(EQUIPMENT_SHARED_STACK, _codes(ledger, 501))

    def test_equipped_reference_unknown_item_fails_closed(self):
        ledger = _plan(
            [(11, 501, 5)],
            PlanningContext(equipped_references={999: 1}),
        )
        self.assertEqual(ledger.entries, ())
        self.assertIn(UNKNOWN_ITEM_REFERENCE, _codes(ledger))

    def test_equipped_item_unknown_design_fails_closed(self):
        # Item present in catalog but design absent from the feed
        # (e.g. the D4 rows before the fix, or a genuinely new design).
        catalog = InventoryCatalog.from_dicts(
            _items_response([(11, 424242, 5)]),
            DesignCatalog.from_dynamic_dict(xmltodict.parse(FEED_XML)),
        )
        ledger = DemandPlanner().plan(
            catalog, PlanningContext(equipped_references={11: 1})
        )
        self.assertEqual(ledger.total_reserved(424242), 0)
        self.assertIn(UNKNOWN_DESIGN, _codes(ledger, 424242))

    def test_no_reference_set_means_source_unavailable(self):
        # None (not provided) != positively empty: unavailable, fail closed.
        ledger = _plan([(11, 501, 5)], PlanningContext())
        self.assertEqual(ledger.entries, ())
        self.assertIn(SOURCE_UNAVAILABLE, _codes(ledger))

    def test_error_ship_payload_is_unavailable_not_negative(self):
        # A non-dict ship payload must never evaluate the reference sets
        # as "positively empty" — that would be a silent negative claim.
        ctx = PlanningContext.from_ship_dict(None)
        self.assertIsNone(ctx.equipped_references)
        self.assertIsNone(ctx.room_references)

    def test_nested_equipment_echoes_are_not_inventoried(self):
        # Edge E4: GetShipByUserId repeats equipped items as nested
        # Character/Items children. Only @ItemIds attributes are read;
        # nested Item rows never reach the catalog parser.
        ship = {
            "ShipService": {
                "GetShipByUserId": {
                    "Ship": {
                        "Characters": {
                            "Character": {
                                "@CharacterId": "1",
                                "@ItemIds": "11",
                                "Items": {"Item": _items_response([(11, 501, 5)])[
                                    "ItemService"]["ListItemsOfAShip"]["Items"]["Item"]},
                            }
                        },
                        "Rooms": {},
                    }
                }
            }
        }
        ctx = PlanningContext.from_ship_dict(ship)
        self.assertEqual(ctx.equipped_references, {11: 1})
        ledger = _plan([(11, 501, 5)], ctx)
        self.assertEqual(ledger.total_reserved(501), 5)


class TestModuleDemand(unittest.TestCase):
    """Point 3 (module half): MODULE demand from room attachments."""

    def test_room_attachment_claims_whole_stack(self):
        ledger = _plan(
            [(13, 503, 9)],
            PlanningContext(room_references={13: 1}),
        )
        (entry,) = ledger.entries_for(503)
        self.assertEqual(entry.source, ReservationSource.MODULE)
        self.assertEqual(entry.requested_quantity, 9)

    def test_duplicate_room_references_dedupe(self):
        # Edge E2: same ItemId twice in one room / in two rooms -> one claim.
        ledger = _plan(
            [(13, 503, 2)],
            PlanningContext(room_references={13: 3}),
        )
        self.assertEqual(ledger.total_requested(503), 2)
        self.assertIn(MODULE_MULTI_ATTACH, _codes(ledger, 503))

    def test_both_equipped_and_room_attached_conflict(self):
        # Edge E5: one physical stack cannot satisfy both; claimed once
        # under EQUIPMENT (documented precedence), MODULE suppressed.
        ledger = _plan(
            [(12, 502, 4)],
            PlanningContext(equipped_references={12: 1}, room_references={12: 1}),
        )
        entries = ledger.entries_for(502)
        self.assertEqual([e.source for e in entries], [ReservationSource.EQUIPMENT])
        self.assertIn(BOTH_EQUIPPED_AND_ROOM_ATTACHED, _codes(ledger, 502))

    def test_broken_module_variant_is_preserved_not_consumed(self):
        # The live room attachments are broken-module variants; the
        # reservation is a preservation fact, never a repair/consume plan.
        ledger = _plan(
            [(13, 503, 1)],
            PlanningContext(room_references={13: 1}),
        )
        (entry,) = ledger.entries_for(503)
        self.assertIn("current-state fact, not a plan", entry.reason)


class TestCraftDemand(unittest.TestCase):
    """Points 4-9: recipe expansion driven by explicit objectives only."""

    def test_no_objective_means_zero_craft_reservations(self):
        # The recipe graph is capability, not demand.
        ledger = _plan([(1, 501, 99)], PlanningContext())
        craft = [e for e in ledger.entries if e.source == ReservationSource.CRAFT]
        self.assertEqual(craft, [])
        self.assertIn(SOURCE_UNSUPPORTED, _codes(ledger))

    def test_single_level_expansion(self):
        # Point 4: objective 2x design 505 = 4x501 + 6x502.
        ledger = _plan(
            [(1, 501, 99), (2, 502, 99)],
            PlanningContext(crafting_objectives={505: 2}),
        )
        by_design = {
            e.item_design_id: e.requested_quantity
            for e in ledger.entries
            if e.source == ReservationSource.CRAFT
        }
        self.assertEqual(by_design, {501: 4, 502: 6})

    def test_recursive_quantity_multiplication(self):
        # Points 5/6: objective 3x design 506 (needs 2x505 + 1x504, and
        # each 505 needs 2x501 + 3x502):
        #   505: 3*2 = 6 -> 501: 6*2 = 12, 502: 6*3 = 18; 504: 3*1 = 3.
        ledger = _plan(
            [(1, 501, 99), (2, 502, 99), (3, 504, 99)],
            PlanningContext(crafting_objectives={506: 3}),
        )
        by_design = {
            e.item_design_id: e.requested_quantity
            for e in ledger.entries
            if e.source == ReservationSource.CRAFT
        }
        self.assertEqual(by_design, {505: 6, 501: 12, 502: 18, 504: 3})

    def test_shared_intermediate_aggregates_across_objectives(self):
        # Point 7: two objectives both pulling design 501 through 505.
        ledger = _plan(
            [(1, 501, 99)],
            PlanningContext(crafting_objectives={505: 2, 506: 1}),
        )
        requests_501 = [
            e.requested_quantity
            for e in ledger.entries
            if e.source == ReservationSource.CRAFT and e.item_design_id == 501
        ]
        # 505 objective: 2*2=4 at depth 1; 506 objective: 1*2*2=4 at depth 2.
        self.assertEqual(requests_501, [4, 4])
        self.assertEqual(ledger.total_requested(501), 8)

    def test_duplicate_ingredient_segments_sum_with_condition(self):
        # Point 8: recipe 509 lists 501 twice (2x + 3x) -> conservative sum.
        ledger = _plan(
            [(1, 501, 99), (2, 502, 99)],
            PlanningContext(crafting_objectives={509: 1}),
        )
        (entry,) = [e for e in ledger.entries if e.item_design_id == 501]
        self.assertEqual(entry.requested_quantity, 5)
        self.assertIn(DUPLICATE_INGREDIENT, _codes(ledger, 501))

    def test_recipe_cycle_detected_not_infinite(self):
        # Point 9: 507 <-> 508 synthetic cycle must terminate with a
        # condition, not recurse forever. Live graph is acyclic (E6).
        ledger = _plan(
            [(1, 507, 99)],
            PlanningContext(crafting_objectives={507: 1}),
        )
        self.assertIn(RECIPE_CYCLE, _codes(ledger))
        # The non-cyclic edge still recorded demand before the cut.
        self.assertIn(508, ledger.design_ids())

    def test_missing_ingredient_design_fails_closed(self):
        # Point 10: recipe 511 requires unknown design 999999.
        ledger = _plan(
            [(1, 511, 99)],
            PlanningContext(crafting_objectives={511: 1}),
        )
        craft = [e for e in ledger.entries if e.source == ReservationSource.CRAFT]
        self.assertEqual(craft, [])
        self.assertIn(UNKNOWN_DESIGN, _codes(ledger, 999999))

    def test_objective_for_non_recipe_design_surfaced(self):
        # Regression (defect found during implementation): an objective
        # for a design with NO @Ingredients previously created nothing,
        # silently. It must surface a condition.
        ledger = _plan(
            [(1, 501, 99)],
            PlanningContext(crafting_objectives={501: 5}),
        )
        self.assertIn(CRAFT_OBJECTIVE_NOT_CRAFTABLE, _codes(ledger, 501))

    def test_gross_requirements_claim_intermediates_and_inputs(self):
        # Documented rule: whether the plan would craft intermediates or
        # use held stock is unobservable, so BOTH the intermediate and
        # its raw inputs are claimed (over-claim is fail-closed).
        ledger = _plan(
            [(1, 501, 99), (2, 502, 99), (3, 505, 99)],
            PlanningContext(crafting_objectives={505: 2}),
        )
        craft_designs = {
            e.item_design_id for e in ledger.entries if e.source == ReservationSource.CRAFT
        }
        # 505 itself is NOT an ingredient of the objective (the objective
        # produces it), so it is not claimed; its inputs are.
        self.assertEqual(craft_designs, {501, 502})

    def test_objective_zero_quantity_demands_nothing(self):
        ledger = _plan(
            [(1, 501, 99)],
            PlanningContext(crafting_objectives={505: 0}),
        )
        craft = [e for e in ledger.entries if e.source == ReservationSource.CRAFT]
        self.assertEqual(craft, [])


class TestQuantityRules(unittest.TestCase):
    """Points 11/12: malformed and unreasonable quantities fail closed."""

    def test_negative_inventory_row_counts_zero_supply(self):
        # A negative server row can never REDUCE reservations.
        catalog = InventoryCatalog.from_dicts(
            _items_response([(-1, 501, -7), (11, 501, 5)]),
            DesignCatalog.from_dynamic_dict(xmltodict.parse(FEED_XML)),
        )
        self.assertEqual(catalog.items()[0].item_id, -1)
        ledger = DemandPlanner().plan(
            catalog, PlanningContext(equipped_references={11: 1})
        )
        self.assertEqual(ledger.observed_quantity(501), 5)
        self.assertIn(MALFORMED_QUANTITY, _codes(ledger, 501))

    def test_malformed_reference_token_condition(self):
        ctx = PlanningContext.from_ship_dict(
            _ship_response(characters=[({"@CharacterId": "1"}, "11,abc,,-5")])
        )
        ledger = _plan([(11, 501, 5)], ctx)
        self.assertEqual(ctx.equipped_references, {11: 1})
        self.assertEqual(_codes(ledger).count(MALFORMED_QUANTITY), 3)

    def test_strategic_config_malformed_quantity_fails_closed(self):
        ledger = _plan(
            [(1, 501, 9)],
            PlanningContext(strategic_reserve={501: "-3"}),
        )
        self.assertEqual(
            [e for e in ledger.entries if e.source == ReservationSource.STRATEGIC],
            [],
        )
        self.assertIn(MALFORMED_QUANTITY, _codes(ledger, 501))

    def test_strategic_config_malformed_key_fails_closed(self):
        ledger = _plan(
            [(1, 501, 9)],
            PlanningContext(strategic_reserve={"abc": 5}),
        )
        self.assertEqual(ledger.entries, ())
        self.assertIn(MALFORMED_QUANTITY, _codes(ledger))

    def test_strategic_config_duplicate_entries_dropped(self):
        # Duplicate config records are dropped ENTIRELY — first-wins or
        # merging would invent which representation is authoritative.
        ledger = _plan(
            [(1, 501, 9)],
            PlanningContext(strategic_reserve={501: 2, "501": 5}),
        )
        self.assertEqual(ledger.entries, ())
        self.assertIn(DUPLICATE_CONFIG_ENTRY, _codes(ledger, 501))

    def test_strategic_config_explicit_zero_reserves_nothing(self):
        ledger = _plan(
            [(1, 501, 9)],
            PlanningContext(strategic_reserve={501: 0}),
        )
        self.assertEqual(ledger.entries, ())
        # Explicit zero is valid config (not unsupported), so no
        # SOURCE_UNSUPPORTED condition for strategic.
        self.assertNotIn(
            SOURCE_UNSUPPORTED,
            [c.detail[:9] for c in ledger.conditions if "STRATEGIC" in c.detail],
        )

    def test_strategic_config_unknown_design_fails_closed(self):
        ledger = _plan(
            [(1, 501, 9)],
            PlanningContext(strategic_reserve={424242: 5}),
        )
        self.assertEqual(ledger.entries, ())
        self.assertIn(UNKNOWN_DESIGN, _codes(ledger, 424242))

    def test_strategic_valid_config_reserves(self):
        ledger = _plan(
            [(1, 501, 9), (2, 502, 3)],
            PlanningContext(strategic_reserve={501: 4, "502": 2}),
        )
        strategic = {
            e.item_design_id: e.reserved_quantity
            for e in ledger.entries
            if e.source == ReservationSource.STRATEGIC
        }
        self.assertEqual(strategic, {501: 4, 502: 2})

    def test_unreasonable_craft_magnitude_cut(self):
        # Point 11/quantity bound: 1x objective of 510 needs 5e8 of 504 —
        # under the bound, reserved; a larger objective exceeds it and is cut.
        ledger = _plan(
            [(1, 504, 99)],
            PlanningContext(crafting_objectives={510: 1}),
        )
        (entry,) = [e for e in ledger.entries if e.item_design_id == 504]
        self.assertEqual(entry.requested_quantity, 500000000)
        big = _plan(
            [(1, 504, 99)],
            PlanningContext(crafting_objectives={510: 3}),
        )
        self.assertIn(UNREASONABLE_QUANTITY, _codes(big, 504))
        self.assertEqual(
            [e for e in big.entries if e.item_design_id == 504],
            [],
        )

    def test_non_positive_ingredient_quantity_dropped(self):
        # A zero-quantity ingredient segment parses (digits only) but
        # demands nothing: the edge is dropped with a condition.
        catalog = DesignCatalog.from_dynamic_dict(
            xmltodict.parse(FEED_XML.replace('Ingredients="501x2|502x3"', 'Ingredients="501x0|502x3"'))
        )
        inv = InventoryCatalog.from_dicts(_items_response([(1, 501, 9)]), catalog)
        ledger = DemandPlanner().plan(inv, PlanningContext(crafting_objectives={505: 1}))
        craft_501 = [
            e for e in ledger.entries
            if e.source == ReservationSource.CRAFT and e.item_design_id == 501
        ]
        self.assertEqual(craft_501, [])
        self.assertIn(MALFORMED_QUANTITY, _codes(ledger, 501))
        # 502 still demanded normally.
        craft_502 = [
            e for e in ledger.entries
            if e.source == ReservationSource.CRAFT and e.item_design_id == 502
        ]
        self.assertEqual(len(craft_502), 1)

    def test_negative_recipe_segment_fails_closed_at_parse(self):
        # A negative segment never survives parse_ingredients (isdigit
        # rejects it): the design is de-cataloged (pre-existing Task B
        # fail-closed behavior), so the objective hits an unknown design.
        catalog = DesignCatalog.from_dynamic_dict(
            xmltodict.parse(FEED_XML.replace('Ingredients="501x2|502x3"', 'Ingredients="501x-1|502x3"'))
        )
        self.assertIsNone(catalog.get(505))
        inv = InventoryCatalog.from_dicts(_items_response([(1, 501, 9)]), catalog)
        ledger = DemandPlanner().plan(inv, PlanningContext(crafting_objectives={505: 1}))
        self.assertEqual(ledger.entries, ())
        self.assertIn(UNKNOWN_DESIGN, _codes(ledger, 505))


class TestOverlappingDemand(unittest.TestCase):
    """Points 13/14: independent sources sum; duplicates never double-count."""

    def test_independent_sources_for_one_design_all_counted(self):
        # Equipment claims stack 11; a strategic reservation claims more
        # of the same design. Independent simultaneous requirements.
        ledger = _plan(
            [(11, 501, 5), (12, 501, 5)],
            PlanningContext(
                equipped_references={11: 1},
                strategic_reserve={501: 3},
            ),
        )
        entries = ledger.entries_for(501)
        self.assertEqual(
            [e.source for e in entries],
            [ReservationSource.EQUIPMENT, ReservationSource.STRATEGIC],
        )
        self.assertEqual(ledger.total_requested(501), 8)
        self.assertEqual(ledger.total_reserved(501), 8)

    def test_supply_shortfall_allocates_in_precedence_order(self):
        # Requests exceed supply: EQUIPMENT (current-state) wins first,
        # strategic gets the remainder, and the shortfall is EXPOSED —
        # never converted into an acquisition/reduction recommendation.
        ledger = _plan(
            [(11, 501, 4)],
            PlanningContext(
                equipped_references={11: 1},
                strategic_reserve={501: 5},
            ),
        )
        entries = ledger.entries_for(501)
        self.assertEqual(entries[0].reserved_quantity, 4)
        self.assertEqual(entries[1].requested_quantity, 5)
        self.assertEqual(entries[1].reserved_quantity, 0)
        self.assertIn(DEMAND_EXCEEDS_SUPPLY, _codes(ledger, 501))
        for condition in ledger.conditions_for(501):
            self.assertNotIn("purchase", condition.detail)
            self.assertNotIn("acquisition target", condition.detail)

    def test_duplicated_source_representation_never_double_counts(self):
        # The same underlying fact expressed twice (a character and a
        # room both referencing one stack) collapses to ONE claim.
        ledger = _plan(
            [(12, 502, 4)],
            PlanningContext(equipped_references={12: 2}, room_references={12: 2}),
        )
        self.assertEqual(ledger.total_requested(502), 4)


class TestEvidencePropagation(unittest.TestCase):
    """Points 15/16: evidence = minimum across required inputs."""

    def test_current_state_evidence_is_captured_not_verified(self):
        ledger = _plan(
            [(11, 501, 5)],
            PlanningContext(equipped_references={11: 1}),
        )
        (entry,) = ledger.entries_for(501)
        self.assertEqual(entry.evidence, Evidence.CAPTURED_NOT_VERIFIED)

    def test_planning_intent_caps_evidence_at_inferred(self):
        # Strategic config is INFERRED intent; even combined with the
        # stronger design feed, the entry never rises above INFERRED.
        ledger = _plan(
            [(1, 501, 9)],
            PlanningContext(strategic_reserve={501: 2}),
        )
        (entry,) = ledger.entries_for(501)
        self.assertEqual(entry.evidence, Evidence.INFERRED)
        self.assertEqual(ledger.evidence_for(501), Evidence.INFERRED)

    def test_craft_evidence_is_inferred(self):
        ledger = _plan(
            [(1, 501, 99)],
            PlanningContext(crafting_objectives={505: 1}),
        )
        for entry in ledger.entries:
            if entry.source == ReservationSource.CRAFT:
                self.assertEqual(entry.evidence, Evidence.INFERRED)

    def test_no_entries_means_unknown_evidence(self):
        ledger = _plan([(1, 501, 9)], PlanningContext())
        self.assertEqual(ledger.evidence_for(501), Evidence.UNKNOWN)
        self.assertEqual(ledger.evidence_for(424242), Evidence.UNKNOWN)

    def test_evidence_never_promoted_above_weakest_input(self):
        # Adversarial: the ledger's per-design evidence must be the
        # minimum over its entries, not the maximum.
        ledger = _plan(
            [(11, 501, 5)],
            PlanningContext(
                equipped_references={11: 1},
                strategic_reserve={501: 1},
            ),
        )
        self.assertEqual(ledger.evidence_for(501), Evidence.INFERRED)


class TestClassificationVsDemandSeparation(unittest.TestCase):
    """Points 18/19/17: roles and quantity never create reservations;
    protection never depends on demand."""

    def _classified(self, rows, context=None):
        context = context or PlanningContext()
        classifier = InventoryClassifier(context=context.protection_context())
        return classifier.classify(_catalog(rows)), context

    def test_classification_roles_alone_create_no_reservations(self):
        # Training role, crafting roles, equipment roles, module role,
        # ammunition role, strategic role — all present, zero claims.
        rows = [(1, 501, 1), (2, 503, 1), (3, 504, 1), (4, 505, 1), (5, 507, 1)]
        classified, ctx = self._classified(rows)
        ledger = DemandPlanner().plan(_catalog(rows), ctx)
        role_designs = set()
        for ci in classified:
            self.assertTrue(ci.roles)
            role_designs.add(ci.item.item_design_id)
        for design_id in role_designs:
            self.assertEqual(ledger.total_requested(design_id), 0, design_id)

    def test_quantity_alone_never_creates_demand(self):
        for qty in (0, 1, 999, 10**9, 10**18):
            ledger = _plan([(1, 501, qty)], PlanningContext())
            self.assertEqual(ledger.entries, (), qty)

    def test_unknown_demand_never_reduces_protection(self):
        # Unknown design in an equipped set: reservation dropped, but the
        # Task D classification of that stack stays PROTECTED.
        rows = [(11, 424242, 5)]
        catalog = _catalog(rows)
        ctx = PlanningContext(equipped_references={11: 1})
        classified = InventoryClassifier(context=ctx.protection_context()).classify(catalog)
        ledger = DemandPlanner().plan(catalog, ctx)
        (ci,) = classified
        self.assertTrue(ci.protected)
        self.assertEqual(ledger.total_reserved(424242), 0)

    def test_equipped_reservation_and_protection_are_independent(self):
        # The equipped stack is protected via rule 1 (protection context)
        # regardless of what the demand ledger says.
        rows = [(11, 501, 5)]
        ctx = PlanningContext(equipped_references={11: 1})
        catalog = _catalog(rows)
        classified = InventoryClassifier(context=ctx.protection_context()).classify(catalog)
        ledger = DemandPlanner().plan(catalog, ctx)
        (ci,) = classified
        self.assertIn("PROTECTED_EQUIPPED", ci.protection_reasons)
        self.assertEqual(ledger.total_reserved(501), 5)
        # Zero-qty equipped stack: protection holds, the claim requests
        # zero (observed supply is zero — no shortfall is invented), and
        # Task D protection is untouched by the ledger.
        ctx0 = PlanningContext(equipped_references={11: 1})
        catalog0 = _catalog([(11, 501, 0)])
        classified0 = InventoryClassifier(context=ctx0.protection_context()).classify(catalog0)
        ledger0 = DemandPlanner().plan(catalog0, ctx0)
        (ci0,) = classified0
        self.assertIn("PROTECTED_EQUIPPED", ci0.protection_reasons)
        (entry0,) = ledger0.entries_for(501)
        self.assertEqual(entry0.requested_quantity, 0)
        self.assertEqual(entry0.reserved_quantity, 0)
        self.assertNotIn(DEMAND_EXCEEDS_SUPPLY, _codes(ledger0, 501))


class TestUnsupportedSources(unittest.TestCase):
    """Point 2 + ammunition/strategic: unsupported means zero reservations
    plus an explicit condition — never silent, never invented."""

    def test_training_unsupported_surface(self):
        ledger = _plan([(1, 503, 9)], PlanningContext())
        training = [e for e in ledger.entries if e.source == ReservationSource.TRAINING]
        self.assertEqual(training, [])
        self.assertTrue(
            any(c.code == SOURCE_UNSUPPORTED and "TRAINING" in c.detail for c in ledger.conditions)
        )

    def test_ammunition_unsupported_surface(self):
        ledger = _plan([(1, 503, 9)], PlanningContext())
        ammo = [e for e in ledger.entries if e.source == ReservationSource.AMMUNITION]
        self.assertEqual(ammo, [])
        self.assertTrue(
            any(c.code == SOURCE_UNSUPPORTED and "AMMUNITION" in c.detail for c in ledger.conditions)
        )

    def test_strategic_unsupported_without_config(self):
        ledger = _plan([(1, 501, 9)], PlanningContext())
        strategic = [e for e in ledger.entries if e.source == ReservationSource.STRATEGIC]
        self.assertEqual(strategic, [])
        self.assertTrue(
            any(
                c.code == SOURCE_UNSUPPORTED and "STRATEGIC" in c.detail
                for c in ledger.conditions
            )
        )

    def test_all_six_sources_have_fixed_positions(self):
        # The allocation precedence is total and stable even though only
        # some sources can ever fire this slice.
        from sdk.inventory.demand import _ALLOCATION_ORDER

        self.assertEqual(len(_ALLOCATION_ORDER), 6)
        self.assertEqual(_ALLOCATION_ORDER[0], ReservationSource.EQUIPMENT)
        self.assertEqual(_ALLOCATION_ORDER[-1], ReservationSource.STRATEGIC)


class TestTaskGAndMultiAccountProhibition(unittest.TestCase):
    """Points 22/23/24: no surplus semantics, no account dimension."""

    def test_ledger_has_no_surplus_or_reduction_surface(self):
        ledger = _plan(
            [(11, 501, 5)],
            PlanningContext(equipped_references={11: 1}),
        )
        for forbidden in (
            "available_surplus",
            "disposable",
            "recommended_sale",
            "recommended_consume",
            "recommended_delete",
            "recommended_transfer",
            "reduction",
        ):
            self.assertFalse(hasattr(ledger, forbidden), forbidden)

    def test_entries_carry_no_reduction_fields(self):
        entry = ReservationEntry(
            item_design_id=501,
            source=ReservationSource.STRATEGIC,
            requested_quantity=1,
            reserved_quantity=1,
            reason="x",
            evidence=Evidence.INFERRED,
            provenance="test",
        )
        for forbidden in ("action", "disposition", "surplus", "endpoint"):
            self.assertFalse(hasattr(entry, forbidden), forbidden)

    def test_no_account_dimension_anywhere(self):
        import inspect

        from sdk.inventory import demand as demand_module

        source = inspect.getsource(demand_module)
        for forbidden in ("account_id", "AccountPortfolio", "deficit_match", "GiftItem"):
            self.assertNotIn(forbidden, source)

    def test_shortfall_exposes_never_recommends(self):
        ledger = _plan(
            [(1, 504, 1)],
            PlanningContext(crafting_objectives={510: 1}),
        )
        self.assertIn(DEMAND_EXCEEDS_SUPPLY, _codes(ledger, 504))
        for condition in ledger.conditions:
            low = condition.detail.lower()
            for banned in ("buy", "purchase", "gift", "sell", "delete", "consume"):
                self.assertNotIn(banned, low, condition.detail)


class TestDomainPurity(unittest.TestCase):
    """Point 28/29: no network I/O; sanitized fixtures only."""

    def test_planner_runs_with_sockets_blocked(self):
        def _blocked(*args, **kwargs):
            raise AssertionError("network I/O attempted inside demand planner")

        rows = [(11, 501, 5), (12, 502, 4)]
        ctx = PlanningContext(
            equipped_references={11: 1},
            room_references={12: 1},
            strategic_reserve={502: 1},
            crafting_objectives={505: 1},
        )
        with patch.object(socket, "socket", _blocked), patch.object(
            socket, "create_connection", _blocked
        ):
            ledger = _plan(rows, ctx)
        self.assertEqual(ledger.total_reserved(501), 5)

    def test_determinism_across_object_schedules(self):
        # Same inputs, fresh planner instances: byte-identical output.
        rows = [(11, 501, 5)]
        ctx = PlanningContext(
            equipped_references={11: 2}, strategic_reserve={502: 1, "501": 0}
        )
        first = DemandPlanner().plan(_catalog(rows), ctx)
        second = DemandPlanner().plan(_catalog(rows), ctx)
        self.assertEqual(first, second)


class TestPlanCliIntegration(unittest.TestCase):
    """Point 21: deterministic CLI output with demand/reservation data."""

    def _registry(self, ship=None, settings=None):
        from sdk.client import Client
        from sdk.commands import CommandRegistry

        client = MagicMock(spec=Client)
        client.dynamicDesigns = xmltodict.parse(FEED_XML)
        client.itemsOfAShip = _items_response([(11, 501, 5), (12, 502, 4), (13, 503, 9)])
        client.shipByUserId = ship
        if settings is not None:
            client.settings = settings
        return CommandRegistry(client)

    def _run_plan(self, registry):
        designs = DesignCatalog.from_dynamic_dict(registry.client.dynamicDesigns)
        catalog = InventoryCatalog.from_dicts(registry.client.itemsOfAShip, designs)
        with patch("sdk.inventory.InventoryCatalog.from_client", return_value=catalog):
            return registry.get("inventory")["handler"](["plan"])

    def test_plan_renders_demand_ledger(self):
        ship = _ship_response(
            characters=[({"@CharacterId": "1"}, "11,11,12")],
            rooms=[({"@RoomId": "7"}, "13")],
        )
        out = self._run_plan(self._registry(ship))
        self.assertIn("Demand/reservation ledger", out)
        self.assertIn("design 501 | observed 5 | requested 5 | reserved 5", out)
        self.assertIn("EQUIPMENT requested 5 reserved 5", out)
        self.assertIn("provenance: Character/@ItemIds -> ItemId 11 (2 character references)", out)
        self.assertIn("design 503 | observed 9 | requested 9 | reserved 9", out)
        self.assertIn("MODULE requested 9 reserved 9", out)
        self.assertIn("[EQUIPMENT_SHARED_STACK]", out)

    def test_plan_renders_strategic_from_explicit_config(self):
        ship = _ship_response(characters=[({"@CharacterId": "1"}, "11")])
        settings = {"inventory": {"strategic_reserve": {502: 2}}}
        out = self._run_plan(self._registry(ship, settings))
        self.assertIn("STRATEGIC requested 2 reserved 2", out)
        self.assertIn("config strategic_reserve[502]=2", out)

    def test_plan_renders_unsupported_sources(self):
        out = self._run_plan(self._registry())
        self.assertIn("[SOURCE_UNSUPPORTED]", out)
        self.assertIn("TRAINING", out)
        self.assertIn("AMMUNITION", out)
        self.assertIn("[SOURCE_UNAVAILABLE]", out)

    def test_plan_output_deterministic(self):
        registry = self._registry(
            _ship_response(
                characters=[({"@CharacterId": "1"}, "11")],
                rooms=[({"@RoomId": "7"}, "13")],
            )
        )
        self.assertEqual(self._run_plan(registry), self._run_plan(registry))

    def test_plan_demand_lines_carry_no_task_g_vocabulary(self):
        ship = _ship_response(characters=[({"@CharacterId": "1"}, "11")])
        out = self._run_plan(self._registry(ship))
        for line in out.splitlines():
            if line.startswith("  design ") or line.startswith("    "):
                for banned in ("surplus", "SURPLUS", "disposable", "sell ", "consume "):
                    self.assertNotIn(banned, line, line)

    def test_plan_states_remaining_quantity_not_computed(self):
        out = self._run_plan(self._registry())
        self.assertIn(
            "remaining quantity is NOT computed in this ledger", out
        )

    def test_apply_refusal_unchanged(self):
        registry = self._registry()
        out = registry.get("inventory apply")["handler"]([])
        self.assertIn("REFUSED", out)
        self.assertFalse(registry.is_verified("inventory apply"))

    def test_apply_refusal_survives_enabled_shaped_settings(self):
        # Point 26: attacker-shaped config must not change the refusal.
        settings = {
            "inventory": {
                "enabled": True,
                "apply": True,
                "strategic_reserve": {501: 99},
            }
        }
        registry = self._registry(settings=settings)
        out = registry.get("inventory apply")["handler"](["--force"])
        self.assertIn("REFUSED", out)

        from sdk.client import Client
        from sdk.commands import CommandRegistry

        plain = CommandRegistry(MagicMock(spec=Client)).get("inventory apply")["handler"]([])
        self.assertEqual(plain, registry.get("inventory apply")["handler"]([]))

    def test_strategic_reserve_bad_shape_never_enables_anything(self):
        # The config reader passes values to the pure planner; junk
        # shapes yield no reservations and no behavior change elsewhere.
        ship = _ship_response(characters=[({"@CharacterId": "1"}, "11")])
        for junk in (None, 5, "nope", [1, 2]):
            registry = self._registry(ship, {"inventory": {"strategic_reserve": junk}})
            out = self._run_plan(registry)
            self.assertIn("Demand/reservation ledger", out)
            self.assertIn("STRATEGIC", out)  # unsupported condition present


def _craft_adversarial_feed():
    """Synthetic feed for adversarial crafting-graph shapes (live-shaped
    attribute names, invented values): diamond fan-in, self-loop,
    back-edge cycle, a 100-node chain vs the depth bound, a magnitude
    edge, and a nested duplicate recipe."""
    rows = []

    def design(design_id, ingredients):
        rows.append(
            f'<ItemDesign ItemDesignId="{design_id}" ItemDesignName="AD{design_id}" '
            f'ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" '
            f'Ingredients="{ingredients}" TrainingDesignId="0" Tags=""/>'
        )

    # Diamond: 801 -> 802(2x804) + 803(3x804); 804 -> 805x1.
    design(801, "802x1|803x1")
    design(802, "804x2")
    design(803, "804x3")
    design(804, "805x1")
    design(805, "")
    # Self-loop and back-edge cycle: 810->810; 811->812->813->812.
    design(810, "810x1")
    design(811, "812x1")
    design(812, "813x1")
    design(813, "812x1")
    # 100-node chain 820..919 to exercise the depth bound.
    for i in range(820, 920):
        design(i, f"{i + 1}x1" if i < 919 else "")
    # Magnitude: 950 -> 951x1000000000; 951 -> 805x2.
    design(950, "951x1000000000")
    design(951, "805x2")
    # Nested duplicate: 960 -> 961x2; 961 lists 805 twice (1x + 4x).
    design(960, "961x2")
    design(961, "805x1|805x4")

    return (
        "<DesignService><ListAllDynamicDesigns><ItemDesigns>"
        + "".join(rows)
        + "</ItemDesigns></ListAllDynamicDesigns></DesignService>"
    )


def _craft_adversarial_catalog(rows=()):
    designs = DesignCatalog.from_dynamic_dict(xmltodict.parse(_craft_adversarial_feed()))
    return InventoryCatalog.from_dicts(_items_response(list(rows)), designs)


def _craft_requested(ledger, design_id):
    return sum(
        e.requested_quantity
        for e in ledger.entries
        if e.source == ReservationSource.CRAFT and e.item_design_id == design_id
    )


class TestCraftAdversarialGraphs(unittest.TestCase):
    """Adversarial crafting expansion shapes (lane t_c7889e74): diamond
    fan-in aggregation, self-loop cycles, back-edge cycles with
    pre-cycle demand retained, the depth bound, magnitude edge-drop
    cutting the subtree, nested duplicates, and objective-on-intermediate
    independence. Attempts to disprove, not confirm, the expansion."""

    def test_diamond_fan_in_aggregates_gross_within_one_objective(self):
        # 801 needs 1x802 and 1x803; 802 needs 2x804, 803 needs 3x804.
        # 804 is reached TWICE at depth 2 and must aggregate to 5 in ONE
        # entry (gross), never double-expanded children: 805 = 5*1 = 5.
        ledger = DemandPlanner().plan(
            _craft_adversarial_catalog(), PlanningContext(crafting_objectives={801: 1})
        )
        self.assertEqual(_craft_requested(ledger, 802), 1)
        self.assertEqual(_craft_requested(ledger, 803), 1)
        self.assertEqual(_craft_requested(ledger, 804), 5)
        self.assertEqual(_craft_requested(ledger, 805), 5)
        entries_804 = [
            e for e in ledger.entries if e.item_design_id == 804
        ]
        self.assertEqual(len(entries_804), 1, "diamond fan-in must merge to one entry")

    def test_self_loop_cycle_cut(self):
        ledger = DemandPlanner().plan(
            _craft_adversarial_catalog(), PlanningContext(crafting_objectives={810: 1})
        )
        self.assertIn(RECIPE_CYCLE, _codes(ledger))
        self.assertEqual(
            [e for e in ledger.entries if e.source == ReservationSource.CRAFT],
            [],
        )

    def test_back_edge_cycle_cut_keeps_pre_cycle_demand(self):
        # 811 -> 812 -> 813 -> 812: the 813->812 edge is a back edge and
        # must be cut with RECIPE_CYCLE, but the pre-cycle chain demand
        # (812 at depth 1, 813 at depth 2) must still be recorded.
        ledger = DemandPlanner().plan(
            _craft_adversarial_catalog(), PlanningContext(crafting_objectives={811: 1})
        )
        self.assertIn(RECIPE_CYCLE, _codes(ledger))
        self.assertEqual(_craft_requested(ledger, 812), 1)
        self.assertEqual(_craft_requested(ledger, 813), 1)

    def test_depth_bound_cuts_deeper_chain_with_condition(self):
        # A 100-node chain against MAX_RECIPE_DEPTH=64 must terminate,
        # record demand down to the bound, and surface the cut.
        from sdk.inventory.demand import MAX_RECIPE_DEPTH

        ledger = DemandPlanner().plan(
            _craft_adversarial_catalog(), PlanningContext(crafting_objectives={820: 1})
        )
        depths = [
            int(e.provenance.rsplit("depth=", 1)[1])
            for e in ledger.entries
            if e.source == ReservationSource.CRAFT
        ]
        self.assertTrue(depths, "chain must record some demand")
        self.assertLessEqual(max(depths), MAX_RECIPE_DEPTH)
        self.assertEqual(max(depths), MAX_RECIPE_DEPTH)
        self.assertIn(UNREASONABLE_QUANTITY, _codes(ledger))

    def test_magnitude_edge_drop_cuts_children(self):
        # 2 objectives of 950 need 2e9 of 951 (over the 1e9 bound): the
        # edge is dropped AND its subtree (951 -> 805) is not demanded.
        ledger = DemandPlanner().plan(
            _craft_adversarial_catalog(), PlanningContext(crafting_objectives={950: 2})
        )
        self.assertIn(UNREASONABLE_QUANTITY, _codes(ledger, 951))
        self.assertEqual(_craft_requested(ledger, 951), 0)
        self.assertEqual(_craft_requested(ledger, 805), 0)

    def test_nested_duplicate_ingredient_sums_with_condition(self):
        # 3 objectives of 960 -> 961:6 at depth 1; 961 lists 805 twice
        # (1+4=5) -> 805: 6*5=30 at depth 2, with DUPLICATE_INGREDIENT.
        ledger = DemandPlanner().plan(
            _craft_adversarial_catalog(), PlanningContext(crafting_objectives={960: 3})
        )
        self.assertEqual(_craft_requested(ledger, 961), 6)
        self.assertEqual(_craft_requested(ledger, 805), 30)
        self.assertIn(DUPLICATE_INGREDIENT, _codes(ledger, 805))

    def test_objective_on_intermediate_is_independent(self):
        # An objective on 804 (an intermediate of 801) creates demand for
        # 804's own children (805x1) scaled by its quantity, while the 801
        # objective still independently pulls 804's inputs through the
        # diamond (2x via 802 + 3x via 803 = 5 gross). 804 itself is never
        # self-claimed by its own objective.
        ledger = DemandPlanner().plan(
            _craft_adversarial_catalog(),
            PlanningContext(crafting_objectives={804: 5, 801: 1}),
        )
        self.assertEqual(_craft_requested(ledger, 804), 5)  # from 801's diamond only
        # 805: 5 (objective 804's own children) + 5 (801's diamond chain) = 10
        self.assertEqual(_craft_requested(ledger, 805), 10)
        objectives_for_805 = {
            e.provenance.split()[2] for e in ledger.entries if e.item_design_id == 805
        }
        self.assertEqual(objectives_for_805, {"design=801", "design=804"})

    def test_cross_objective_shared_intermediate_keeps_provenance(self):
        # Two objectives sharing intermediate 804 at the same depth keep
        # DISTINCT provenance entries that sum (no cross-objective
        # contamination, no silent merge).
        ledger = DemandPlanner().plan(
            _craft_adversarial_catalog(),
            PlanningContext(crafting_objectives={802: 2, 803: 1}),
        )
        entries_804 = [e for e in ledger.entries if e.item_design_id == 804]
        requested = sorted(e.requested_quantity for e in entries_804)
        self.assertEqual(requested, [3, 4])
        self.assertEqual(_craft_requested(ledger, 804), 7)
        for e in entries_804:
            self.assertIn("objective design=", e.provenance)

    def test_complex_multi_objective_graph_is_deterministic(self):
        ctx = PlanningContext(crafting_objectives={801: 2, 802: 1, 811: 1, 820: 1, 960: 1})
        first = DemandPlanner().plan(_craft_adversarial_catalog(), ctx)
        second = DemandPlanner().plan(_craft_adversarial_catalog(), ctx)
        self.assertEqual(first, second)
        self.assertTrue(any(e.source == ReservationSource.CRAFT for e in first.entries))


if __name__ == "__main__":
    unittest.main()
