#!/usr/bin/env python3
"""Task H architect lane: inventory-plan renderer extraction output parity.

The renderer was mechanically extracted from sdk/commands.py
`_render_inventory_plan` into the pure sdk/inventory/plan_render.py
module (render_inventory_plan). These tests pin BYTE parity: the
extracted implementation must reproduce the exact pre-extraction
output of the `inventory plan` CLI verb on the canonical fixture
catalog (captured from the pre-extraction implementation on
2026-09-29, sha256 GOLDEN_PLAN_SHA256).

All inputs are sanitized/synthetic dictionaries; the Client is a
MagicMock. NO live Pixel Starships traffic; sockets are blocked in the
purity test.
"""

import hashlib
import socket
import unittest
from unittest.mock import MagicMock, patch

import xmltodict

from sdk.inventory import DesignCatalog, InventoryCatalog

# ---------------------------------------------------------------------------
# Fixtures: identical to tests/test_inventory_plan_cli.py (same shapes;
# duplicated deliberately so this parity file is self-contained).
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
                    {"@ItemId": "1001", "@ShipId": "55", "@ItemDesignId": "563", "@Quantity": "4", "@IsNew": "false", "@SkinKey": ""},
                    {"@ItemId": "1002", "@ShipId": "55", "@ItemDesignId": "81", "@Quantity": "7", "@IsNew": "true", "@SkinKey": ""},
                    {"@ItemId": "1004", "@ShipId": "55", "@ItemDesignId": "9999", "@Quantity": "3", "@IsNew": "false", "@SkinKey": ""},
                    {"@ItemId": "1006", "@ShipId": "55", "@ItemDesignId": "88", "@Quantity": "2", "@IsNew": "false", "@SkinKey": ""},
                    {"@ItemId": "1007", "@ShipId": "55", "@ItemDesignId": "91", "@Quantity": "40", "@IsNew": "false", "@SkinKey": ""},
                    {"@ItemId": "563", "@ShipId": "55", "@ItemDesignId": "81", "@Quantity": "5", "@IsNew": "false", "@SkinKey": ""},
                    {"@ItemId": "1009", "@ShipId": "55", "@ItemDesignId": "777", "@Quantity": "1", "@IsNew": "false", "@SkinKey": ""},
                    {"@ItemId": "1010", "@ShipId": "55", "@ItemDesignId": "390", "@Quantity": "2", "@IsNew": "false", "@SkinKey": ""},
                ]
            }
        }
    }
}

GOLDEN_PLAN_SHA256 = "0c4c2095c41dce0a3a273a43bf8aa24177ab1c5ef39652f8c0b540b162abb87d"

# Exact pre-extraction `inventory plan` output on the fixture catalog
# (byte-for-byte; every line asserted below is also covered by this
# full-text equality).
GOLDEN_PLAN_OUTPUT = "=== Inventory plan (READ-ONLY Tasks D+F+G — classification + demand + dry-run reduction, nothing executable) ===\nClassification reports what each item IS and what protections apply. Demand reports what quantity known plans actually claim, with provenance. The dry-run section reports what COULD be considered if reduction were ever authorized: every row is informational (actionable=false), hints name no executor, and no row is a recommendation. Nothing below is actionable.\nItem stacks: 8\nClassified stacks: PROTECTED 3, RESERVED 4, CONSUMABLE 1\nZero-quantity stacks retained by server: 0 (never actionable)\n\nPer-item classification (ascending ItemId; 'candidate' marks a classification label only, never a recommendation):\n  item 563 | design 81 | qty 5 | RESERVED | protected: no | evidence INFERRED\n    name: Scrap Iron\n    roles: CRAFTING_INGREDIENT\n    classification reasons:\n      - crafting ingredient (Artifact): recipe input\n  item 1001 | design 563 | qty 4 | CONSUMABLE | protected: no | evidence INFERRED\n    name: Small Mineral Crate\n    roles: CONSUMABLE_USE_CANDIDATE\n    classification reasons:\n      - consumable/use candidate (classification only, no action implied)\n  item 1002 | design 81 | qty 7 | RESERVED | protected: no | evidence INFERRED\n    name: Scrap Iron\n    roles: CRAFTING_INGREDIENT\n    classification reasons:\n      - crafting ingredient (Artifact): recipe input\n  item 1004 | design 9999 | qty 3 | PROTECTED | protected: yes | evidence UNKNOWN | unresolved\n    name: unavailable (design absent from captured dynamic design feed)\n    protection reasons: PROTECTED_UNKNOWN_DESIGN\n    classification reasons:\n      - design absent from captured dynamic design feed\n  item 1006 | design 88 | qty 2 | RESERVED | protected: no | evidence CAPTURED_NOT_VERIFIED\n    name: Instant Training\n    roles: CREW_TRAINING_MATERIAL\n    classification reasons:\n      - training material: consumption resets costly-to-rebuild state\n  item 1007 | design 91 | qty 40 | RESERVED | protected: no | evidence INFERRED\n    name: Missile Alpha\n    roles: AMMUNITION_MATERIAL\n    classification reasons:\n      - ammunition: replenished by the RebuildAmmo3 loop\n  item 1009 | design 777 | qty 1 | PROTECTED | protected: yes | evidence STATICALLY_DISCOVERED\n    name: Marked Module\n    protection reasons: PROTECTED_RARITY, PROTECTED_EVENT_LIMITED\n    roles: SHIP_MODULE_CANDIDATE\n    classification reasons:\n      - protected by policy rules: PROTECTED_RARITY, PROTECTED_EVENT_LIMITED\n      - ship-module candidate: room-module endpoints uncaptured\n  item 1010 | design 390 | qty 2 | PROTECTED | protected: yes | evidence INFERRED\n    name: Makeshift Turret\n    protection reasons: PROTECTED_INSUFFICIENT_EVIDENCE\n    roles: CRAFTING_INTERMEDIATE\n    classification reasons:\n      - protected by policy rules: PROTECTED_INSUFFICIENT_EVIDENCE\n      - crafting intermediate: produced from recipe inputs\n\nLimitations (rules not evaluated or data unavailable; never a demotion):\n  - rule 2 equipment-plan reservation: Task F semantics, not evaluated this slice\n  - rule 3 elected-craft-recipe reservation: Task F semantics, not evaluated this slice\n  - rule 9 marketplace value: ListSalesByItemDesignId uncaptured (STATICALLY_DISCOVERED); inert, never demotes, never invented\n  - rule 1 equipped-item check: equipped ItemId set unavailable\n  - rule 4 room-module check: room ItemId set unavailable\n\nDemand/reservation ledger (Task F; ascending design id; requested/reserved per source; remaining quantity is NOT computed in this ledger — the Task G dry-run section below computes unreserved = max(0, observed - reserved)):\n\nUnresolved demand conditions (fail-closed; never a demotion):\n  [SOURCE_UNAVAILABLE] EQUIPMENT: equipped ItemId references were not provided (no ship payload); zero equipment reservations\n  [SOURCE_UNAVAILABLE] MODULE: room ItemId attachments were not provided (no ship payload); zero module reservations\n  [SOURCE_UNSUPPORTED] AMMUNITION: RebuildAmmo3 restocks server-side (ammoCategory only); no client-side authoritative ammo quantity source exists, so zero ammunition reservations\n  [SOURCE_UNSUPPORTED] CRAFT: the recipe graph (@Ingredients) is cataloged capability only; no crafting objective exists in code or config, so zero craft reservations\n  [SOURCE_UNSUPPORTED] STRATEGIC: no explicit strategic-reserve configuration was provided; no approved default exists, so zero strategic reservations\n  [SOURCE_UNSUPPORTED] TRAINING: the runtime training flow is one-at-a-time; no plan, quantity or per-character requirement representation exists, so zero training reservations\n\nDry-run reduction candidates (Task G; per stack; DRY-RUN ONLY — every row actionable=false; no executor exists):\nunreserved = max(0, observed - reserved); unreserved does not mean reducible. candidate respects 0<=candidate<=unreserved<=observed and is 0 for protected, unknown, malformed, over-reserved, shared/conflicted, or applicable-unsupported-demand stacks.\nStacks: HOLD 5, CANDIDATE 3, total candidate quantity 16 (informational only)\n    item 563 | design 81 | qty observed 12 | reserved 0 | unreserved 12 | candidate 5 | CANDIDATE | hint CRAFT | evidence UNKNOWN | actionable False\n    item 1001 | design 563 | qty observed 4 | reserved 0 | unreserved 4 | candidate 4 | CANDIDATE | hint USE | evidence UNKNOWN | actionable False\n    item 1002 | design 81 | qty observed 12 | reserved 0 | unreserved 12 | candidate 7 | CANDIDATE | hint CRAFT | evidence UNKNOWN | actionable False\n    item 1004 | design 9999 | qty observed 3 | reserved 0 | unreserved 3 | candidate 0 | HOLD | hint KEEP | evidence UNKNOWN | actionable False\n      reasons: BLOCKED_PROTECTED_OR_UNKNOWN\n    item 1006 | design 88 | qty observed 2 | reserved 0 | unreserved 2 | candidate 0 | HOLD | hint KEEP | evidence UNKNOWN | actionable False\n      reasons: BLOCKED_UNSUPPORTED_DEMAND\n    item 1007 | design 91 | qty observed 40 | reserved 0 | unreserved 40 | candidate 0 | HOLD | hint KEEP | evidence UNKNOWN | actionable False\n      reasons: BLOCKED_UNSUPPORTED_DEMAND\n    item 1009 | design 777 | qty observed 1 | reserved 0 | unreserved 1 | candidate 0 | HOLD | hint KEEP | evidence UNKNOWN | actionable False\n      reasons: BLOCKED_PROTECTED_OR_UNKNOWN\n    item 1010 | design 390 | qty observed 2 | reserved 0 | unreserved 2 | candidate 0 | HOLD | hint KEEP | evidence UNKNOWN | actionable False\n      reasons: BLOCKED_PROTECTED_OR_UNKNOWN\n\nDisposition hints (KEEP/USE/CRAFT/ALLOCATE) are future-order information only; TRANSFER/SELL/CONSUME/DELETE remain deferred and unassigned.\n\nNo surplus, deficit, disposable quantity, sale quantity or reduction action is executed or recommended in this slice; no role, reservation, condition or candidate shown here is a mutation recommendation. 'inventory apply' remains refused."


def _make_registry():
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


class TestRendererExtractionByteParity(unittest.TestCase):
    """The extracted pure renderer reproduces the hotspot byte-for-byte."""

    def test_registry_plan_output_sha256_matches_pre_extraction_golden(self):
        out = _run_plan()
        digest = hashlib.sha256(out.encode("utf-8")).hexdigest()
        self.assertEqual(digest, GOLDEN_PLAN_SHA256)

    def test_registry_plan_output_equals_full_golden_text(self):
        self.assertEqual(_run_plan(), GOLDEN_PLAN_OUTPUT)

    def test_plan_output_deterministic_across_calls(self):
        self.assertEqual(_run_plan(), _run_plan())

    def test_pure_renderer_direct_matches_registry_path(self):
        """Calling the pure module with the wrapper's inputs yields the
        same bytes as the registry path (the wrapper adds no text)."""
        from sdk.inventory import render_inventory_plan

        registry = _make_registry()
        designs = DesignCatalog.from_dynamic_dict(registry.client.dynamicDesigns)
        catalog = InventoryCatalog.from_dicts(registry.client.itemsOfAShip, designs)
        direct = render_inventory_plan(catalog, tuple(catalog.items()), None, {})
        with patch("sdk.inventory.InventoryCatalog.from_client", return_value=catalog):
            via_cli = registry.get("inventory")["handler"](["plan"])
        self.assertEqual(direct, via_cli)

    def test_extracted_method_body_removed_from_commands_hotspot(self):
        """The text-assembly lines must live in the pure module, not in
        the commands hotspot anymore."""
        import os

        path = os.path.join(
            os.path.dirname(__file__), "..", "sdk", "commands.py"
        )
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        # Representative assembly lines from the old hotspot body.
        self.assertNotIn("Dry-run reduction candidates (Task G; per stack", src)
        self.assertNotIn("Per-item classification (ascending ItemId", src)
        self.assertNotIn("Demand/reservation ledger (Task F; ascending design id", src)
        self.assertIn("render_inventory_plan", src)  # delegation present


class TestRendererPurity(unittest.TestCase):
    """The extracted renderer performs no network I/O."""

    def test_plan_renders_with_sockets_blocked(self):
        def _blocked(*args, **kwargs):
            raise AssertionError("network I/O attempted during inventory plan")

        with patch.object(socket, "socket", _blocked), patch.object(
            socket, "create_connection", _blocked
        ):
            out = _run_plan()
        self.assertIn("=== Inventory plan", out)


if __name__ == "__main__":
    unittest.main()
