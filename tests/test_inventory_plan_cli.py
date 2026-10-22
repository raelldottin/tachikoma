#!/usr/bin/env python3
"""Lane D CLI tests: `inventory plan` classification rendering (Task D).

All inputs are sanitized/synthetic dictionaries. NO live Pixel Starships
traffic; the Client is a MagicMock, network sockets are blocked in the
no-network test, and no destructive call is ever attempted (S1/S2).

Lane scope (workstream D of t_b49b0534): the read-only CLI surface renders
the pure classification exported by sdk.inventory. Task F (demand/
reservations) and Task G (surplus/reduction) semantics must not leak into
the output, and `inventory apply` must keep refusing unconditionally.
"""

import os
import socket
import unittest
from unittest.mock import MagicMock, patch

import xmltodict

from sdk.inventory import DesignCatalog, InventoryCatalog

# ---------------------------------------------------------------------------
# Sanitized fixtures (shape of captured data; all values synthetic)
# ---------------------------------------------------------------------------

DYNAMIC_FEED_XML = """<DesignService xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
  <ListAllDynamicDesigns>
    <ItemDesigns>
      <ItemDesign ItemDesignId="563" ItemDesignName="Small Mineral Crate" ItemType="Resource" ItemSubType="ResourcePackage" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="81" ItemDesignName="Scrap Iron" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="390" ItemDesignName="Makeshift Turret" ItemType="Craft" ItemSubType="Vehicle" Rarity="Elite" Ingredients="81x5|104x6" Tags="Craft"/>
      <ItemDesign ItemDesignId="777" ItemDesignName="Marked Module" ItemType="Module" ItemSubType="Module" ModuleType="Armor" Rarity="Hero" Ingredients="" Tags="Module,Event"/>
      <ItemDesign ItemDesignId="88" ItemDesignName="Instant Training" ItemType="Special" ItemSubType="InstantTraining" TrainingDesignId="12" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="91" ItemDesignName="Missile Alpha" ItemType="Missile" ItemSubType="Missile" Rarity="Common" Ingredients="" Tags=""/>
    </ItemDesigns>
  </ListAllDynamicDesigns>
</DesignService>"""

ITEMS_RESPONSE_DICT = {
    "ItemService": {
        "ListItemsOfAShip": {
            "Items": {
                "Item": [
                    {  # ResourcePackage -> CONSUMABLE bucket
                        "@ItemId": "1001",
                        "@ShipId": "55",
                        "@ItemDesignId": "563",
                        "@Quantity": "4",
                        "@IsNew": "false",
                        "@SkinKey": "",
                    },
                    {  # Artifact ingredient -> RESERVED (conservative default)
                        "@ItemId": "1002",
                        "@ShipId": "55",
                        "@ItemDesignId": "81",
                        "@Quantity": "7",
                        "@IsNew": "true",
                        "@SkinKey": "",
                    },
                    {  # unknown design 9999 -> PROTECTED + unresolved
                        "@ItemId": "1004",
                        "@ShipId": "55",
                        "@ItemDesignId": "9999",
                        "@Quantity": "3",
                        "@IsNew": "false",
                        "@SkinKey": "",
                    },
                    {  # training material -> RESERVED
                        "@ItemId": "1006",
                        "@ShipId": "55",
                        "@ItemDesignId": "88",
                        "@Quantity": "2",
                        "@IsNew": "false",
                        "@SkinKey": "",
                    },
                    {  # missile, high quantity -> RESERVED, never surplus/discard
                        "@ItemId": "1007",
                        "@ShipId": "55",
                        "@ItemDesignId": "91",
                        "@Quantity": "40",
                        "@IsNew": "false",
                        "@SkinKey": "",
                    },
                    {  # ItemId 563 collides with design id 563 (conflation probe)
                        "@ItemId": "563",
                        "@ShipId": "55",
                        "@ItemDesignId": "81",
                        "@Quantity": "5",
                        "@IsNew": "false",
                        "@SkinKey": "",
                    },
                    {  # Hero rarity + Event tag module -> PROTECTED (2 reasons)
                        "@ItemId": "1009",
                        "@ShipId": "55",
                        "@ItemDesignId": "777",
                        "@Quantity": "1",
                        "@IsNew": "false",
                        "@SkinKey": "",
                    },
                    {  # unrecognized "Elite" rarity -> PROTECTED, fail closed
                        "@ItemId": "1010",
                        "@ShipId": "55",
                        "@ItemDesignId": "390",
                        "@Quantity": "2",
                        "@IsNew": "false",
                        "@SkinKey": "",
                    },
                ]
            }
        }
    }
}

MUTATION_URL_FRAGMENTS = (
    "/ItemService/ConsumeItem2",
    "/ItemService/ActivateItem3",
    "/ItemService/ActivateItem4",
    "/ItemService/CombineItem4",
    "/ItemService/DeleteItem3",
    "/ItemService/GiftItem",
    "/MarketService/SellItem3",
    "/MarketService/Purchase4",
    "/CharacterService/WearItem",
    "/CharacterService/UnequipItems",
    "/RoomService/AddItemIdToRoom",
    "/RoomService/MoveItemIdToRoom3",
    "/RoomService/RemoveItemIdFromRoom",
    "/RoomService/UpdateItemIdsForRoom2",
    "/RoomService/Refill2",
    "/ItemService/RepairBrokenModules",
    "/ItemService/DeleteAmmo",
)

ENV_BYPASS_VARS = (
    "INVENTORY_ENABLE_APPLY",
    "PSS_ENABLE_INVENTORY_MUTATIONS",
    "INVENTORY_APPLY",
    "PSS_INVENTORY_APPLY",
    "TACHIKOMA_INVENTORY_MUTATIONS",
)


def _make_registry():
    """Registry over a fully mocked Client (no network)."""
    from sdk.client import Client
    from sdk.commands import CommandRegistry

    client = MagicMock(spec=Client)
    client.dynamicDesigns = xmltodict.parse(DYNAMIC_FEED_XML)
    client.itemsOfAShip = ITEMS_RESPONSE_DICT
    return CommandRegistry(client)


def _run_plan(registry=None):
    registry = registry or _make_registry()
    designs = DesignCatalog.from_dynamic_dict(registry.client.dynamicDesigns)
    catalog = InventoryCatalog.from_dicts(registry.client.itemsOfAShip, designs)
    with patch("sdk.inventory.InventoryCatalog.from_client", return_value=catalog):
        return registry.get("inventory")["handler"](["plan"])


def _run_inventory(registry=None):
    registry = registry or _make_registry()
    designs = DesignCatalog.from_dynamic_dict(registry.client.dynamicDesigns)
    catalog = InventoryCatalog.from_dicts(registry.client.itemsOfAShip, designs)
    with patch("sdk.inventory.InventoryCatalog.from_client", return_value=catalog):
        return registry.get("inventory")["handler"]([])


def _item_lines(out):
    """The per-item classification rows, in emitted order."""
    return [ln for ln in out.splitlines() if ln.startswith("  item ")]


class TestPlanClassificationOutput(unittest.TestCase):
    """Brief INVENTORY PLAN INTEGRATION: every required field is rendered."""

    def test_plan_header_and_disclaimer(self):
        out = _run_plan()
        self.assertIn("READ-ONLY", out.upper())
        self.assertIn("Nothing below is actionable", out)
        # Candidate terminology is labeled classification-only.
        self.assertIn("classification label only, never a recommendation", out)

    def test_plan_reports_item_and_design_ids_and_quantity(self):
        out = _run_plan()
        self.assertIn("  item 1001 | design 563 | qty 4", out)
        self.assertIn("  item 1002 | design 81 | qty 7", out)

    def test_plan_reports_design_names(self):
        out = _run_plan()
        self.assertIn("name: Small Mineral Crate", out)
        self.assertIn("name: Missile Alpha", out)

    def test_plan_reports_bucket_counts(self):
        out = _run_plan()
        # PROTECTED: 1004 (unknown), 1009 (rarity+event), 1010 (Elite)
        # RESERVED: 1002, 1006, 1007, 563 (artifact/training/missile)
        # CONSUMABLE: 1001 (resource package)
        self.assertIn("Classified stacks: PROTECTED 3, RESERVED 4, CONSUMABLE 1", out)

    def test_plan_reports_protected_flag_and_reasons(self):
        out = _run_plan()
        self.assertIn("| protected: yes |", out)
        self.assertIn("| protected: no |", out)
        # Rule 5 + rule 6 both fire on design 777 — full reason set, in order.
        self.assertIn(
            "protection reasons: PROTECTED_RARITY, PROTECTED_EVENT_LIMITED", out
        )
        # Unrecognized rarity fails closed with an explicit reason.
        self.assertIn("PROTECTED_INSUFFICIENT_EVIDENCE", out)

    def test_plan_reports_roles(self):
        out = _run_plan()
        self.assertIn("roles: CONSUMABLE_USE_CANDIDATE", out)
        self.assertIn("roles: CRAFTING_INGREDIENT", out)
        self.assertIn("roles: CREW_TRAINING_MATERIAL", out)
        self.assertIn("roles: AMMUNITION_MATERIAL", out)
        self.assertIn("roles: SHIP_MODULE_CANDIDATE", out)

    def test_plan_module_roles_and_evidence(self):
        out = _run_plan()
        # Module candidate (design 777) carries the STATICALLY_DISCOVERED
        # tier via the module-role input.
        self.assertIn("roles: SHIP_MODULE_CANDIDATE", out)
        self.assertIn("evidence STATICALLY_DISCOVERED", out)
        # Marker-derived roles are INFERRED (never promoted).
        self.assertIn("evidence INFERRED", out)

    def test_plan_unknown_design_unresolved(self):
        out = _run_plan()
        self.assertIn("  item 1004 | design 9999 | qty 3 | PROTECTED", out)
        self.assertIn("unresolved", out)
        self.assertIn("PROTECTED_UNKNOWN_DESIGN", out)
        self.assertIn(
            "name: unavailable (design absent from captured dynamic design feed)", out
        )

    def test_plan_zero_quantity_stack_classified_not_actionable(self):
        client_fixture = {
            "ItemService": {
                "ListItemsOfAShip": {
                    "Items": {
                        "Item": [
                            {
                                "@ItemId": "2000",
                                "@ShipId": "55",
                                "@ItemDesignId": "563",
                                "@Quantity": "0",
                                "@IsNew": "false",
                                "@SkinKey": "",
                            }
                        ]
                    }
                }
            }
        }
        from sdk.client import Client
        from sdk.commands import CommandRegistry

        client = MagicMock(spec=Client)
        client.dynamicDesigns = xmltodict.parse(DYNAMIC_FEED_XML)
        client.itemsOfAShip = client_fixture
        registry = CommandRegistry(client)
        designs = DesignCatalog.from_dynamic_dict(client.dynamicDesigns)
        catalog = InventoryCatalog.from_dicts(client_fixture, designs)
        with patch("sdk.inventory.InventoryCatalog.from_client", return_value=catalog):
            out = registry.get("inventory")["handler"](["plan"])
        self.assertIn("  item 2000 | design 563 | qty 0", out)
        self.assertIn("Zero-quantity stacks retained by server: 1", out)

    def test_plan_reports_limitations(self):
        out = _run_plan()
        self.assertIn("Limitations (rules not evaluated or data unavailable", out)
        self.assertIn("rule 2 equipment-plan reservation: Task F", out)
        self.assertIn("rule 9 marketplace value", out)
        # Default context carries no equipped/room sets -> limitations exposed.
        self.assertIn("rule 1 equipped-item check", out)
        self.assertIn("rule 4 room-module check", out)

    def test_plan_itemid_vs_itemdesignid_no_conflation(self):
        """ItemId 563 (a stack of design 81) must not read as design 563."""
        out = _run_plan()
        self.assertIn("  item 563 | design 81 | qty 5", out)
        # The design-563 stack keeps its own ItemId.
        self.assertIn("  item 1001 | design 563", out)


class TestPlanDeterminism(unittest.TestCase):
    """Brief point 16: deterministic CLI output (R1)."""

    def test_plan_byte_identical_across_runs(self):
        self.assertEqual(_run_plan(), _run_plan())

    def test_plan_item_order_ascending_itemid(self):
        out = _run_plan()
        ids = [int(ln.split()[1]) for ln in _item_lines(out)]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, [563, 1001, 1002, 1004, 1006, 1007, 1009, 1010])

    def test_inventory_view_still_deterministic(self):
        self.assertEqual(_run_inventory(), _run_inventory())


class TestPlanNoTaskFGSemantics(unittest.TestCase):
    """Brief DO-NOT clauses: no surplus/deficit/reservation/transfer/sale."""

    def test_no_affirmative_fg_terms_on_item_lines(self):
        out = _run_plan()
        for ln in _item_lines(out):
            for term in (
                "SURPLUS",
                "TRANSFERABLE",
                "SELLABLE",
                "DISCARDABLE",
                "surplus",
                "deficit",
                "reserved quantity",
                "transfer target",
                "sale quantity",
            ):
                self.assertNotIn(term, ln, f"item line leaks Task F/G term: {ln}")

    def test_no_fg_roles_in_output(self):
        out = _run_plan()
        for role in (
            "TRANSFERABLE_CANDIDATE",
            "MARKETPLACE_SALE_CANDIDATE",
            "DISCARD_CANDIDATE",
        ):
            self.assertNotIn(role, out)

    def test_high_quantity_never_becomes_surplus_or_discard(self):
        """Brief point 18: quantity alone never creates surplus/discard."""
        out = _run_plan()
        # item 1007 owns 40 missiles: RESERVED, no discard/sell/transfer role.
        self.assertIn("  item 1007 | design 91 | qty 40 | RESERVED", out)
        self.assertIn("| protected: no |", out)
        self.assertNotIn("DISCARD", out)
        self.assertNotIn("SURPLUS", out)

    def test_plan_states_fg_unavailable(self):
        out = _run_plan()
        # Task G dry-run section is rendered below the ledger; the
        # header must scope exactly what each section decides.
        self.assertIn("READ-ONLY Tasks D+F+G", out)
        self.assertIn(
            "The dry-run section reports what COULD be "
            "considered if reduction were ever authorized",
            out,
        )
        self.assertIn(
            "Dry-run reduction candidates (Task G; per stack; DRY-RUN "
            "ONLY — every row actionable=false; no executor exists)",
            out,
        )
        self.assertIn(
            "No surplus, deficit, disposable quantity, sale quantity or "
            "reduction action is executed or recommended in this slice",
            out,
        )
        # Task G rows are dry-run only: actionable=false on every row.
        for ln in out.splitlines():
            if ln.strip().startswith("item ") and "actionable" in ln:
                self.assertIn("actionable False", ln, ln)

    def test_classified_items_carry_inert_fg_fields(self):
        """The consumed ClassifiedItems keep reservation=() / available_surplus=None."""
        from sdk.inventory import InventoryClassifier, ProtectionContext

        designs = DesignCatalog.from_dynamic_dict(xmltodict.parse(DYNAMIC_FEED_XML))
        catalog = InventoryCatalog.from_dicts(ITEMS_RESPONSE_DICT, designs)
        classified = InventoryClassifier(context=ProtectionContext()).classify(catalog)
        self.assertTrue(classified)
        for ci in classified:
            self.assertEqual(ci.reservation, ())
            self.assertIsNone(ci.available_surplus)


class TestInventoryRemainsPlainDigest(unittest.TestCase):
    """Brief: `inventory` stays a straightforward read-only catalog view."""

    def test_plain_view_has_no_classification_columns(self):
        out = _run_inventory()
        self.assertIn("=== Inventory (read-only) ===", out)
        self.assertIn("Item stacks: 8", out)
        # Per-design digest retained.
        self.assertIn("Small Mineral Crate", out)
        # design 81 totals: 1002 qty 7 + item 563 qty 5 = 12
        self.assertIn("| qty    12", out)

    def test_plain_view_reports_unknown_designs_protected(self):
        out = _run_inventory()
        self.assertIn("PROTECTED — unknown designs", out)
        self.assertIn("1 design ids, 3 total quantity", out)

    def test_plain_view_points_to_plan_for_classification(self):
        out = _run_inventory()
        self.assertIn("Classification and demand/reservations are reported by 'inventory plan'", out)

    def test_plain_view_has_no_reasons_roles_evidence_lines(self):
        out = _run_inventory()
        self.assertNotIn("protection reasons:", out)
        self.assertNotIn("roles:", out)
        self.assertNotIn("evidence ", out)


class TestApplyStillRefuses(unittest.TestCase):
    """Brief points 21/23: `inventory apply` refuses always, env-proof."""

    def test_apply_refuses_all_arg_shapes(self):
        registry = _make_registry()
        handler = registry.get("inventory apply")["handler"]
        for args in ([], ["--force"], ["--yes"], ["now"], ["sell", "1001"]):
            out = handler(args)
            self.assertIn("REFUSED", out, f"args={args}")
            self.assertIn("Phase 8", out)
            self.assertIn("Phase 9", out)

    def test_apply_refuses_under_env_vars(self):
        registry = _make_registry()
        handler = registry.get("inventory apply")["handler"]
        for var in ENV_BYPASS_VARS:
            os.environ[var] = "1"
            try:
                self.assertIn("REFUSED", handler([]))
            finally:
                del os.environ[var]

    def test_apply_still_unverified(self):
        registry = _make_registry()
        self.assertFalse(registry.is_verified("inventory apply"))
        self.assertTrue(registry.is_verified("inventory"))


class TestPlanSafetyAndPurity(unittest.TestCase):
    """Brief points 22/24 at the CLI layer."""

    def test_commands_source_has_no_mutation_urls(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "sdk", "commands.py"
        )
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for frag in MUTATION_URL_FRAGMENTS:
            self.assertNotIn(frag, src)

    def test_client_never_imports_inventory(self):
        path = os.path.join(os.path.dirname(__file__), "..", "sdk", "client.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("import sdk.inventory", src)
        self.assertNotIn("from sdk.inventory", src)

    def test_plan_runs_with_sockets_blocked(self):
        """Brief point 24: no network I/O anywhere in the plan path."""
        def _blocked(*args, **kwargs):
            raise AssertionError("network I/O attempted during inventory plan")

        with patch.object(socket, "socket", _blocked), patch.object(
            socket, "create_connection", _blocked
        ):
            out = _run_plan()
        self.assertIn("=== Inventory plan", out)

    def test_plan_reads_no_env_or_config_flags(self):
        path = os.path.join(os.path.dirname(__file__), "..", "sdk", "commands.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        # The inventory handlers must not consult env/config for behavior.
        self.assertNotIn("os.environ", src)
        self.assertNotIn("getenv", src)


if __name__ == "__main__":
    unittest.main()
