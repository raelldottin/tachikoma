#!/usr/bin/env python3
"""Tests for inventory Slice 1 (Tasks B + C + E-minimal).

All inputs are sanitized/synthetic dictionaries. NO live Pixel Starships
traffic; tests never import requests, never hit the network, and never
attempt a destructive call (repo rule; safety invariants S1/S2).

Expected checksum values for the endpoint checksum tests are computed at
import time by applying the VERIFIED formulas to synthetic vectors
(tests/synthetic_fixtures.py), then asserted against the helpers — the tests
carry no capture-derived values, and real capture values never enter the repo.
"""

import hashlib
import os
import unittest
from unittest.mock import MagicMock, patch

import xmltodict

from sdk.inventory import (
    CAPTURED_ITEM_ATTRIBUTES,
    DesignCatalog,
    DuplicateItemIdError,
    InventoryCatalog,
    checksum_activate_item4,
    checksum_consume_item2,
    checksum_mark_items_not_new,
    checksum_purchase4,
    checksum_sell_item3,
    parse_ingredients,
    parse_item,
)
from sdk.inventory.checksums import UnsupportedNativeChecksum
from tests.synthetic_fixtures import (
    SYNTHETIC_ACCESS_TOKEN,
    SYNTHETIC_CDT_1,
    SYNTHETIC_CDT_2,
    SYNTHETIC_CHECKSUM_KEY,
    SYNTHETIC_SAVY_CHECKSUM,
)

# ---------------------------------------------------------------------------
# Sanitized fixtures (shape of captured data; all values synthetic)
# ---------------------------------------------------------------------------

DYNAMIC_FEED_XML = """<DesignService xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
  <ListAllDynamicDesigns>
    <ItemDesigns>
      <ItemDesign ItemDesignId="563" ItemDesignName="Small Mineral Crate" ItemType="Resource" ItemSubType="ResourcePackage" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="81" ItemDesignName="Scrap Iron" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="104" ItemDesignName="Carbon" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="" Tags=""/>
      <ItemDesign ItemDesignId="390" ItemDesignName="Makeshift Turret" ItemType="Craft" ItemSubType="Vehicle" Rarity="Elite" Ingredients="81x5|104x6" Tags="Craft"/>
      <ItemDesign ItemDesignId="777" ItemDesignName="Marked Module" ItemType="Module" ItemSubType="Module" ModuleType="Armor" Rarity="Hero" Ingredients="" Tags="Module,Event"/>
      <ItemDesign ItemDesignId="888" ItemDesignName="Null Marker Design" ItemType="Equipment" ItemSubType="EquipmentBody" Rarity="Common" Ingredients="0" TrainingDesignId="0" Tags=""/>
    </ItemDesigns>
  </ListAllDynamicDesigns>
</DesignService>"""

ITEMS_RESPONSE_DICT = {
    "ItemService": {
        "ListItemsOfAShip": {
            "Items": {
                "Item": [
                    {
                        "@ItemId": "1001",
                        "@ShipId": "55",
                        "@ItemDesignId": "563",
                        "@Quantity": "4",
                        "@IsNew": "false",
                        "@SkinKey": "",
                    },
                    {
                        "@ItemId": "1002",
                        "@ShipId": "55",
                        "@ItemDesignId": "81",
                        "@Quantity": "7",
                        "@IsNew": "true",
                        "@SkinKey": "",
                    },
                    {
                        "@ItemId": "1003",
                        "@ShipId": "55",
                        "@ItemDesignId": "563",
                        "@Quantity": "2",
                        "@IsNew": "false",
                        "@SkinKey": "",
                    },
                    {
                        "@ItemId": "1004",
                        "@ShipId": "55",
                        "@ItemDesignId": "9999",  # unknown design
                        "@Quantity": "3",
                        "@IsNew": "false",
                        "@SkinKey": "",
                    },
                    {
                        "@ItemId": "1005",
                        "@ShipId": "55",
                        "@ItemDesignId": "104",
                        "@Quantity": "0",  # zero-quantity stack retained by server
                        "@IsNew": "false",
                        "@SkinKey": "",
                    },
                ]
            }
        }
    }
}

SYNTHETIC_ITEM_ID = "1001"
SYNTHETIC_CHARACTER_ID = "55159886"
SYNTHETIC_QUANTITY = "1"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _expected_consume_item2(
    cdt, item_id, quantity, character_id, ck=SYNTHETIC_CHECKSUM_KEY, sc=SYNTHETIC_SAVY_CHECKSUM
):
    # VERIFIED formula (3/3 captures): MD5(clientDateTime + itemId + quantity
    # + characterId + ChecksumKey + SavyChecksum)
    return _md5(cdt + item_id + quantity + character_id + ck + sc)


def _expected_activate_item4(cdt, ck=SYNTHETIC_CHECKSUM_KEY, sc=SYNTHETIC_SAVY_CHECKSUM):
    # VERIFIED formula (1/1 capture): MD5(clientDateTime + ChecksumKey + SavyChecksum)
    return _md5(cdt + ck + sc)


def _expected_mark_items_not_new(
    cdt, access_token, ck=SYNTHETIC_CHECKSUM_KEY, sc=SYNTHETIC_SAVY_CHECKSUM
):
    # VERIFIED formula (1/1 capture): MD5(clientDateTime + accessToken + CK + SC)
    return _md5(cdt + access_token + ck + sc)


class TestConsumeItem2Checksum(unittest.TestCase):
    """RT-1: checksum_consume_item2 reproduces the capture-verified formula."""

    def test_consume_item2_checksum_formula(self):
        expected = _expected_consume_item2(
            SYNTHETIC_CDT_1, SYNTHETIC_ITEM_ID, SYNTHETIC_QUANTITY, SYNTHETIC_CHARACTER_ID
        )
        actual = checksum_consume_item2(
            SYNTHETIC_CDT_1, SYNTHETIC_ITEM_ID, SYNTHETIC_QUANTITY, SYNTHETIC_CHARACTER_ID
        )
        self.assertEqual(actual, expected)

    def test_consume_item2_second_sample(self):
        expected = _expected_consume_item2(
            SYNTHETIC_CDT_2, SYNTHETIC_ITEM_ID, "2", SYNTHETIC_CHARACTER_ID
        )
        actual = checksum_consume_item2(
            SYNTHETIC_CDT_2, SYNTHETIC_ITEM_ID, "2", SYNTHETIC_CHARACTER_ID
        )
        self.assertEqual(actual, expected)

    def test_consume_item2_excludes_access_token(self):
        # Access token is on the wire but NOT in the preimage.
        with_token = _md5(
            SYNTHETIC_CDT_1
            + SYNTHETIC_ACCESS_TOKEN
            + SYNTHETIC_ITEM_ID
            + SYNTHETIC_QUANTITY
            + SYNTHETIC_CHARACTER_ID
            + SYNTHETIC_CHECKSUM_KEY
            + SYNTHETIC_SAVY_CHECKSUM
        )
        actual = checksum_consume_item2(
            SYNTHETIC_CDT_1, SYNTHETIC_ITEM_ID, SYNTHETIC_QUANTITY, SYNTHETIC_CHARACTER_ID
        )
        self.assertNotEqual(actual, with_token)

    def test_consume_item2_raises_without_keys(self):
        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_consume_item2(
                SYNTHETIC_CDT_1, SYNTHETIC_ITEM_ID, SYNTHETIC_QUANTITY,
                SYNTHETIC_CHARACTER_ID, checksum_key="", savy_checksum="",
            )


class TestMarkItemsNotNewChecksum(unittest.TestCase):
    """RT-2: checksum_mark_items_not_new reproduces the captured sample."""

    def test_mark_items_not_new_checksum(self):
        expected = _expected_mark_items_not_new(SYNTHETIC_CDT_1, SYNTHETIC_ACCESS_TOKEN)
        actual = checksum_mark_items_not_new(SYNTHETIC_CDT_1, SYNTHETIC_ACCESS_TOKEN)
        self.assertEqual(actual, expected)

    def test_mark_items_not_new_raises_without_keys(self):
        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_mark_items_not_new(
                SYNTHETIC_CDT_1, SYNTHETIC_ACCESS_TOKEN, checksum_key="", savy_checksum=""
            )


class TestActivateItem4Checksum(unittest.TestCase):
    """ActivateItem4 VERIFIED formula; evidence stays single-sample capped."""

    def test_activate_item4_checksum(self):
        expected = _expected_activate_item4(SYNTHETIC_CDT_1)
        actual = checksum_activate_item4(SYNTHETIC_CDT_1)
        self.assertEqual(actual, expected)

    def test_activate_item4_excludes_params(self):
        # itemId/quantity/targetId are on the wire but NOT in the preimage.
        with_params = _md5(
            SYNTHETIC_CDT_1
            + SYNTHETIC_ITEM_ID
            + SYNTHETIC_QUANTITY
            + SYNTHETIC_CHECKSUM_KEY
            + SYNTHETIC_SAVY_CHECKSUM
        )
        actual = checksum_activate_item4(SYNTHETIC_CDT_1)
        self.assertNotEqual(actual, with_params)


def _expected_sell_item3(
    access_token,
    item_id,
    quantity,
    currency_type,
    currency_value,
    cdt,
    ck=SYNTHETIC_CHECKSUM_KEY,
    sc=SYNTHETIC_SAVY_CHECKSUM,
):
    # VERIFIED formula (6/6 captures, 2026-10-05): MD5(accessToken +
    # itemId + quantity + currencyType + currencyValue + clientDateTime
    # + ChecksumKey + SavyChecksum)
    return _md5(
        access_token
        + item_id
        + quantity
        + currency_type
        + currency_value
        + cdt
        + ck
        + sc
    )


def _expected_purchase4(
    access_token,
    sale_id,
    cdt,
    ck=SYNTHETIC_CHECKSUM_KEY,
    sc=SYNTHETIC_SAVY_CHECKSUM,
):
    # VERIFIED formula (1/1 capture, 2026-10-05): MD5(accessToken +
    # saleId + clientDateTime + ChecksumKey + SavyChecksum)
    return _md5(access_token + sale_id + cdt + ck + sc)


class TestSellItem3Checksum(unittest.TestCase):
    """RT: checksum_sell_item3 reproduces the capture-verified formula
    (6/6 official-client samples in the 2026-10-05 Task J capture)."""

    def test_sell_item3_checksum_formula(self):
        expected = _expected_sell_item3(
            SYNTHETIC_ACCESS_TOKEN,
            SYNTHETIC_ITEM_ID,
            SYNTHETIC_QUANTITY,
            "Gas",
            "250000",
            SYNTHETIC_CDT_1,
        )
        actual = checksum_sell_item3(
            SYNTHETIC_ACCESS_TOKEN,
            SYNTHETIC_ITEM_ID,
            SYNTHETIC_QUANTITY,
            "Gas",
            "250000",
            SYNTHETIC_CDT_1,
        )
        self.assertEqual(actual, expected)

    def test_sell_item3_second_sample_mineral(self):
        # Second captured sample shape: different item/currency/date.
        expected = _expected_sell_item3(
            SYNTHETIC_ACCESS_TOKEN,
            "2002",
            "1",
            "Mineral",
            "50000",
            SYNTHETIC_CDT_2,
        )
        actual = checksum_sell_item3(
            SYNTHETIC_ACCESS_TOKEN,
            "2002",
            "1",
            "Mineral",
            "50000",
            SYNTHETIC_CDT_2,
        )
        self.assertEqual(actual, expected)

    def test_sell_item3_token_first_in_preimage(self):
        # accessToken comes FIRST, unlike the URL query order.
        url_order = _md5(
            SYNTHETIC_ITEM_ID
            + SYNTHETIC_QUANTITY
            + "Gas"
            + "250000"
            + SYNTHETIC_CDT_1
            + SYNTHETIC_ACCESS_TOKEN
            + SYNTHETIC_CHECKSUM_KEY
            + SYNTHETIC_SAVY_CHECKSUM
        )
        actual = checksum_sell_item3(
            SYNTHETIC_ACCESS_TOKEN,
            SYNTHETIC_ITEM_ID,
            SYNTHETIC_QUANTITY,
            "Gas",
            "250000",
            SYNTHETIC_CDT_1,
        )
        self.assertNotEqual(actual, url_order)

    def test_sell_item3_raises_without_keys(self):
        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_sell_item3(
                SYNTHETIC_ACCESS_TOKEN,
                SYNTHETIC_ITEM_ID,
                SYNTHETIC_QUANTITY,
                "Gas",
                "250000",
                SYNTHETIC_CDT_1,
                checksum_key="",
                savy_checksum="",
            )


class TestPurchase4Checksum(unittest.TestCase):
    """RT: checksum_purchase4 reproduces the captured sample's formula
    (1/1 official-client sample, 2026-10-05 Task J capture).

    SINGLE-SAMPLE evidence: the formula is wire-derived from one capture
    and the helper documents that ceiling; the endpoint stays unreachable.
    """

    def test_purchase4_checksum_formula(self):
        expected = _expected_purchase4(
            SYNTHETIC_ACCESS_TOKEN, "44400001", SYNTHETIC_CDT_1
        )
        actual = checksum_purchase4(
            SYNTHETIC_ACCESS_TOKEN, "44400001", SYNTHETIC_CDT_1
        )
        self.assertEqual(actual, expected)

    def test_purchase4_excludes_allow_conversion(self):
        # allowConversion is on the wire but NOT in the preimage.
        with_flag = _md5(
            SYNTHETIC_ACCESS_TOKEN
            + "44400001"
            + SYNTHETIC_CDT_1
            + "False"
            + SYNTHETIC_CHECKSUM_KEY
            + SYNTHETIC_SAVY_CHECKSUM
        )
        actual = checksum_purchase4(
            SYNTHETIC_ACCESS_TOKEN, "44400001", SYNTHETIC_CDT_1
        )
        self.assertNotEqual(actual, with_flag)

    def test_purchase4_raises_without_keys(self):
        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_purchase4(
                SYNTHETIC_ACCESS_TOKEN,
                "44400001",
                SYNTHETIC_CDT_1,
                checksum_key="",
                savy_checksum="",
            )


class TestCombineItem4CaptureRecorded(unittest.TestCase):
    """RT-3: the 2026-08-01 empty-itemIds CombineItem4 capture is recorded in
    the endpoint analysis doc as CAPTURED-BUT-AMBIGUOUS (not "no captures")."""

    def test_combine_item4_capture_recorded(self):
        doc_path = os.path.join(
            os.path.dirname(__file__), "..", "INVENTORY_ENDPOINT_ANALYSIS.md"
        )
        with open(doc_path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("CAPTURED-BUT-AMBIGUOUS", text)
        self.assertIn("empty `itemIds`", text)
        # The stale "ConsumeItem2 has no checksum" claim must be gone.
        self.assertNotIn("`/ItemService/ConsumeItem2?itemId=`", text)


class TestDynamicFeedParsing(unittest.TestCase):
    """Task B: dynamic ItemDesign parsing from sanitized captured data."""

    def test_parse_dynamic_feed(self):
        catalog = DesignCatalog.from_dynamic_dict(xmltodict.parse(DYNAMIC_FEED_XML))
        self.assertEqual(len(catalog), 6)
        self.assertEqual(len(catalog.all()), 6)  # nonzero ItemDesign count

    def test_parse_dynamic_feed_single_dict_form(self):
        single = {
            "DesignService": {
                "ListAllDynamicDesigns": {
                    "ItemDesigns": {
                        "ItemDesign": {
                            "@ItemDesignId": "563",
                            "@ItemName": "Small Mineral Crate",
                        }
                    }
                }
            }
        }
        catalog = DesignCatalog.from_dynamic_dict(single)
        self.assertEqual(len(catalog), 1)
        d = catalog.get(563)
        self.assertIsNotNone(d)
        self.assertEqual(d.item_name, "Small Mineral Crate")

    def test_ingredients_parsing(self):
        d = DesignCatalog.from_dynamic_dict(xmltodict.parse(DYNAMIC_FEED_XML)).get(390)
        self.assertIsNotNone(d)
        self.assertEqual(d.ingredients, ((81, 5), (104, 6)))

    def test_ingredients_malformed(self):
        for bad in ("abc", "81x", "81|x5", "|81x5"):
            with self.assertRaises(ValueError, msg=bad):
                parse_ingredients(bad)

    def test_ingredients_absent(self):
        self.assertIsNone(parse_ingredients(""))
        self.assertIsNone(parse_ingredients(None))

    def test_tags_and_rarity_preserved(self):
        d = DesignCatalog.from_dynamic_dict(xmltodict.parse(DYNAMIC_FEED_XML)).get(777)
        self.assertIsNotNone(d)
        self.assertEqual(d.tags, ("Event", "Module"))
        self.assertEqual(d.rarity, "Hero")
        self.assertEqual(d.module_type, "Armor")

    def test_missing_design_id_fails_closed(self):
        node = {"@ItemName": "NoId"}
        self.assertIsNone(parse_item(node))

    def test_missing_itemdesign_id_fails_closed(self):
        from sdk.inventory.designs import parse_item_design

        self.assertIsNone(parse_item_design({"@ItemName": "NoId"}))


class TestClientListAllDynamicDesigns(unittest.TestCase):
    """Task B: Client.listAllDynamicDesigns request/response handling, fully mocked."""

    def _make_client(self, payload: str):
        from sdk.client import Client

        client = MagicMock(spec=Client)
        client.latestVersion = {
            "SettingService": {
                "GetLatestSetting": {"Setting": {"@ItemDesignVersion": "311"}}
            }
        }
        resp = MagicMock()
        resp.content = payload.encode("utf-8")
        client.request.return_value = resp

        client.listAllDynamicDesigns = Client.listAllDynamicDesigns.__get__(client)
        client.getLatestVersion3 = lambda: None
        return client

    def test_success(self):
        client = self._make_client(DYNAMIC_FEED_XML)
        client.baseUrl = "https://api.pixelstarships.com"
        client.device = MagicMock(languageKey="en")
        self.assertTrue(client.listAllDynamicDesigns())
        mock_request = client.request
        url = mock_request.call_args[0][0]
        self.assertIn("DesignService/ListAllDynamicDesigns", url)
        self.assertIn("designVersion=311", url)
        catalog = DesignCatalog.from_dynamic_dict(client.dynamicDesigns)
        self.assertEqual(len(catalog), 6)

    def test_failure_no_response(self):
        client = self._make_client(DYNAMIC_FEED_XML)
        client.baseUrl = "https://api.pixelstarships.com"
        client.device = MagicMock(languageKey="en")
        client.request.return_value = None
        self.assertFalse(client.listAllDynamicDesigns())
        self.assertIsNone(getattr(client, "dynamicDesigns", None))

    def test_failure_bad_envelope(self):
        client = self._make_client("<Other/>")
        client.baseUrl = "https://api.pixelstarships.com"
        client.device = MagicMock(languageKey="en")
        self.assertFalse(client.listAllDynamicDesigns())


class TestInventoryCatalog(unittest.TestCase):
    """Task C: catalog normalization with the six supported Item attributes."""

    def setUp(self):
        self.designs = DesignCatalog.from_dynamic_dict(xmltodict.parse(DYNAMIC_FEED_XML))

    def test_six_attributes_consumed(self):
        """RT-4: exactly the six capture-observed attributes are modeled."""
        items = InventoryCatalog.from_dicts(ITEMS_RESPONSE_DICT, self.designs).items()
        self.assertEqual(len(items), 5)
        item = {i.item_id: i for i in items}[1001]
        self.assertEqual(item.item_id, 1001)
        self.assertEqual(item.ship_id, 55)
        self.assertEqual(item.item_design_id, 563)
        self.assertEqual(item.quantity, 4)
        self.assertFalse(item.is_new)
        self.assertIsNone(item.skin_key)
        # All six captured attribute names represented in raw
        for attr in CAPTURED_ITEM_ATTRIBUTES:
            self.assertIn(f"@{attr}", item.raw)

    def test_extra_attributes_land_in_raw_not_fields(self):
        node = dict(ITEMS_RESPONSE_DICT["ItemService"]["ListItemsOfAShip"]["Items"]["Item"][0])
        node["@Rarity"] = "Epic"  # NOT capture-observed on Item nodes
        item = parse_item(node)
        self.assertIsNotNone(item)
        self.assertIn("@Rarity", item.raw)
        self.assertFalse(hasattr(item, "rarity"))

    def test_itemid_vs_itemdesignid(self):
        catalog = InventoryCatalog.from_dicts(ITEMS_RESPONSE_DICT, self.designs)
        # two stacks of design 563 with distinct ItemIds
        by_design = catalog.by_design(563)
        self.assertEqual({i.item_id for i in by_design}, {1001, 1003})
        self.assertEqual(catalog.quantity_of(563), 6)
        self.assertEqual(catalog.by_design(1001), [])  # ItemId is not a design id

    def test_unknown_design_fails_closed(self):
        catalog = InventoryCatalog.from_dicts(ITEMS_RESPONSE_DICT, self.designs)
        unknown = [i for i in catalog.items() if catalog.resolve(i) is None]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0].item_design_id, 9999)

    def test_zero_quantity_stack_counted(self):
        catalog = InventoryCatalog.from_dicts(ITEMS_RESPONSE_DICT, self.designs)
        zero_items = [i for i in catalog.items() if i.quantity == 0]
        self.assertEqual(len(zero_items), 1)  # AD-5: retained, never actionable
        self.assertEqual(catalog.quantity_of(104), 0)

    def test_duplicate_item_id_fails_closed(self):
        dup = {
            "ItemService": {
                "ListItemsOfAShip": {
                    "Items": {
                        "Item": [
                            {"@ItemId": "1", "@ShipId": "1", "@ItemDesignId": "563"},
                            {"@ItemId": "1", "@ShipId": "1", "@ItemDesignId": "81"},
                        ]
                    }
                }
            }
        }
        with self.assertRaises(DuplicateItemIdError):
            InventoryCatalog.from_dicts(dup, self.designs)
        # Explicitly opting into dedupe keeps first occurrence (AD-4)
        catalog = InventoryCatalog.from_dicts(dup, self.designs, allow_duplicate_item_ids=True)
        self.assertEqual(len(catalog), 1)

    def test_deterministic_ordering(self):
        a = InventoryCatalog.from_dicts(ITEMS_RESPONSE_DICT, self.designs)
        b = InventoryCatalog.from_dicts(ITEMS_RESPONSE_DICT, self.designs)
        self.assertEqual([i.item_id for i in a.items()], [i.item_id for i in b.items()])
        self.assertEqual(
            [i.item_id for i in a.items()],
            sorted(i.item_id for i in a.items()),
        )

    def test_tolerant_defaults(self):
        node = {"@ItemId": "7", "@ItemDesignId": "563"}
        item = parse_item(node)
        self.assertIsNotNone(item)
        self.assertEqual(item.quantity, 1)
        self.assertFalse(item.is_new)


MUTATION_URL_FRAGMENTS = (
    "/ItemService/ConsumeItem2",
    "/ItemService/ActivateItem3",
    "/ItemService/ActivateItem4",
    "/ItemService/CombineItem4",
    "/ItemService/DeleteItem3",
    "/ItemService/GiftItem",
    "/MarketService/SellItem3",
    "/MarketService/ListSalesByItemDesignId",
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

PRODUCTION_ROOTS = ("sdk", "run.py")


class TestNoMutationPathReachable(unittest.TestCase):
    """T10 static scan: no inventory-mutation URL template appears in
    production code for this slice (S1). Checksum helpers are pure functions
    and contain no endpoint URLs."""

    def test_no_mutation_url_in_production_code(self):
        hits = []
        for root in PRODUCTION_ROOTS:
            files = []
            if os.path.isfile(root):
                files = [root]
            else:
                for dirpath, _, filenames in os.walk(root):
                    files.extend(
                        os.path.join(dirpath, f)
                        for f in filenames
                        if f.endswith(".py")
                    )
            for path in files:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                for frag in MUTATION_URL_FRAGMENTS:
                    if frag in text:
                        hits.append(f"{path}: {frag}")
        # The T10 scan greps for callable mutation URL templates; checksum
        # helpers carry doc-only references to their endpoint paths which are
        # not call sites. They are pure functions with no network code.
        # The pre-existing legacy activateItem3 call in sdk/client.py predates
        # this slice and is out of scope for the T10 slice-1 delta scan.
        allowed = {
            "sdk/client.py: /ItemService/ActivateItem3",
            "sdk/inventory/checksums.py: /ItemService/ConsumeItem2",
            "sdk/inventory/checksums.py: /ItemService/ActivateItem4",
            "sdk/inventory/checksums.py: /MarketService/SellItem3",
            "sdk/inventory/checksums.py: /MarketService/Purchase4",
        }
        hits = [h for h in hits if h not in allowed]
        self.assertEqual(hits, [])


class TestStarbuxSurfaceUnchanged(unittest.TestCase):
    """RT-6: this slice must not widen premium-currency exposure."""

    def test_starbux_cap_unchanged_surface(self):
        import subprocess

        diff = subprocess.run(
            ["git", "diff", "HEAD", "--", "sdk/client.py", "run.py"],
            capture_output=True,
            text=True,
            check=False,
            cwd=os.path.join(os.path.dirname(__file__), ".."),
        ).stdout
        for token in (
            "speedUpTravelling",
            "purchaseDrawWithStarbux",
            "addStarbuxWithProduct",
            "rebuildAmmoForRoom",
        ):
            for line in diff.splitlines():
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                    self.assertNotIn(
                        token,
                        line,
                        f"slice diff touches pre-existing starbux path: {line}",
                    )


class TestInventoryCommands(unittest.TestCase):
    """Task E-minimal: read-only verbs and the always-refusing apply stub (S2)."""

    def _registry(self, design_xml=DYNAMIC_FEED_XML, items_dict=ITEMS_RESPONSE_DICT):
        from sdk.commands import CommandRegistry
        from sdk.client import Client

        client = MagicMock(spec=Client)
        client.dynamicDesigns = xmltodict.parse(design_xml)
        client.itemsOfAShip = items_dict
        return CommandRegistry(client), DesignCatalog.from_dynamic_dict(client.dynamicDesigns)

    def _run(self, args, verb="inventory"):
        registry, designs = self._registry()
        catalog = InventoryCatalog.from_dicts(registry.client.itemsOfAShip, designs)
        with patch(
            "sdk.inventory.InventoryCatalog.from_client", return_value=catalog
        ):
            return registry.get(verb)["handler"](args)

    def test_inventory_read_only_summary(self):
        out = self._run([])
        self.assertIn("READ-ONLY", out.upper().replace("===", "").upper())
        self.assertIn("Small Mineral Crate", out)
        self.assertIn("| qty     6", out)
        self.assertIn("PROTECTED", out)  # unknown design 9999
        self.assertIn("Zero-quantity stacks retained by server: 1", out)

    def test_inventory_plan_read_only(self):
        out = self._run(["plan"])
        self.assertIn("READ-ONLY", out.upper())
        # Task G dry-run rows are informational only: actionable=false on
        # every row, no executor exists, nothing is a recommendation.
        self.assertIn("Nothing below is actionable", out)
        self.assertIn("no executor exists", out)

    def test_inventory_deterministic(self):
        self.assertEqual(self._run([]), self._run([]))  # AD-7

    def test_inventory_apply_always_refuses(self):
        handler = self._registry()[0].get("inventory apply")["handler"]
        for args in ([], ["--force"], ["--yes"], ["now"]):
            out = handler(args)
            self.assertIn("REFUSED", out)
            self.assertIn("Phase 8", out)
            self.assertIn("Phase 9", out)  # AD-10

    def test_inventory_apply_not_verified(self):
        registry = self._registry()[0]
        self.assertFalse(registry.is_verified("inventory apply"))
        self.assertTrue(registry.is_verified("inventory"))

    def test_inventory_apply_no_env_enable(self):
        for var in ("INVENTORY_ENABLE_APPLY", "PSS_ENABLE_INVENTORY_MUTATIONS", "INVENTORY_APPLY"):
            os.environ[var] = "1"
            try:
                out = self._registry()[0].get("inventory apply")["handler"]([])
                self.assertIn("REFUSED", out)
            finally:
                del os.environ[var]


if __name__ == "__main__":
    unittest.main()
