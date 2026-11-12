"""Tests for sdk.state.ClientSnapshotProvider with a fake client. No network."""

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sdk.state import READ_METHODS, ClientSnapshotProvider, Completeness, SnapshotInputError
from sdk.state.client_provider import item_resource_kinds
from tests.synthetic_fixtures import SYNTHETIC_ACCESS_TOKEN, SYNTHETIC_EMAIL

REPO_ROOT = Path(os.path.abspath(__file__)).parents[1]
T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
INVENTORY = json.loads((REPO_ROOT / "docs/baseline/capability-inventory.json").read_text(encoding="utf-8"))
MUTATING = {entry["name"] for entry in INVENTORY["client_methods"] if entry["kind"] != "read-only"}

SHIP_PAYLOAD = {
    "ShipService": {
        "GetShipByUserId": {
            "Ship": {
                "@ShipLevel": "7",
                "@ShipDesignId": "107",
                "Rooms": {"Room": [{"@RoomId": "r1", "@RoomDesignId": "11", "@RoomStatus": "Normal"}]},
                "Researches": {"Research": {"@ResearchDesignId": "500", "@ResearchState": "Completed"}},
            }
        }
    }
}
CREW_PAYLOAD = {
    "CharacterService": {
        "ListAllCharactersOfUser": {
            "Characters": {"Character": [{"@CharacterId": "c1", "@Level": "9", "@RoomId": "r1"}]}
        }
    }
}
ITEMS_PAYLOAD = {
    "ItemService": {
        "ListItemsOfAShip": {
            "Items": {
                "Item": [
                    {"@ItemDesignId": "1", "@ItemType": "Mineral", "@Quantity": "1200"},
                    {"@ItemDesignId": "2", "@ItemType": "Gas", "@Quantity": "800"},
                    {"@ItemDesignId": "900", "@ItemType": "Equipment", "@Quantity": "2"},
                ]
            }
        }
    }
}
# Every attribute a live ListItemsOfAShip item carries (first G2 canary, 2026-11-12): none names a type.
LIVE_ITEM = {
    "@BonusEnhancementType": "None", "@BonusEnhancementValue": "0", "@Flags": "0", "@IsNew": "false",
    "@ItemDesignId": "0", "@ItemId": "0", "@LegendaryBonusEnhancementType": "None",
    "@LegendaryBonusEnhancementValue": "0", "@Quantity": "0", "@ShipId": "0", "@SkinKey": "0",
}
# ItemDesign records as listAllDesigns4 stores them in client.itemDesigns.
ITEM_DESIGNS = [
    {"@ItemDesignId": "1", "@ItemName": "Mineral", "@ItemType": "Mineral"},
    {"@ItemDesignId": "2", "@ItemName": "Gas", "@ItemType": "gas"},
    {"@ItemDesignId": "3", "@ItemName": "Mineral Crate", "@ItemType": "MINERAL"},
    {"@ItemDesignId": "900", "@ItemName": "Helmet", "@ItemType": "Equipment"},
]


class FakeClient:
    """Implements only the read methods; any other Client method fails the test."""

    def __init__(self, ship=SHIP_PAYLOAD, crew=CREW_PAYLOAD, items=ITEMS_PAYLOAD):
        self.payloads = {"getShipByUserId": ship, "listAllCharactersOfUser": crew, "listItemsOfAShip": items}
        self.calls = []
        self.raise_on = set()
        self.info = {"@Credits": "999", "@Trophy": "1500", "@Email": SYNTHETIC_EMAIL}
        self.accessToken = SYNTHETIC_ACCESS_TOKEN

    def _reader(self, name, attribute):
        def read():
            self.calls.append(name)
            if name in self.raise_on:
                raise ConnectionError("synthetic read failure")
            payload = self.payloads[name]
            if payload is None:
                return False
            setattr(self, attribute, payload)
            return True
        return read

    def __getattr__(self, name):
        attributes = {
            "getShipByUserId": "shipByUserId",
            "listAllCharactersOfUser": "allCharactersOfUser",
            "listItemsOfAShip": "itemsOfAShip",
        }
        if name in attributes:
            return self._reader(name, attributes[name])
        if name in MUTATING:
            raise AssertionError(f"provider called mutating or session method {name}")
        raise AttributeError(name)


def provide(client=None, **kwargs):
    client = client or FakeClient()
    provider = ClientSnapshotProvider(client, "alpha", clock=lambda: T0, **kwargs)
    return provider, provider(), client


class ClientSnapshotProviderTests(unittest.TestCase):
    def test_read_methods_are_read_only_in_the_inventory(self):
        kinds = {entry["name"]: entry["kind"] for entry in INVENTORY["client_methods"]}
        for name in READ_METHODS:
            self.assertEqual(kinds[name], "read-only", name)

    def test_full_reads_produce_snapshot(self):
        provider, snapshot, client = provide(game_version="0.999.55")
        self.assertEqual(client.calls, list(READ_METHODS))
        self.assertEqual(snapshot.account_alias, "alpha")
        self.assertEqual(snapshot.observed_at, T0)
        self.assertEqual(snapshot.hull_level, 7)
        self.assertEqual(snapshot.room("r1").design_id, "11")
        self.assertEqual(snapshot.research_state("500"), "Completed")
        self.assertEqual(snapshot.crew_member("c1").level, 9)
        self.assertEqual((snapshot.resources.minerals, snapshot.resources.gas), (1200, 800))
        self.assertEqual(provider.failed_reads, ())
        self.assertEqual(snapshot.game_version, "0.999.55")

    def test_login_time_values_are_not_reported(self):
        _, snapshot, _ = provide()
        self.assertIsNone(snapshot.resources.starbux)
        self.assertIsNone(snapshot.trophies)
        self.assertIs(snapshot.section("profile"), Completeness.MISSING)
        self.assertIs(snapshot.section("resources"), Completeness.PARTIAL)

    def test_failed_and_empty_reads_leave_sections_missing(self):
        client = FakeClient(crew=None)
        client.raise_on.add("getShipByUserId")
        provider, snapshot, _ = provide(client)
        for section in ("ship", "rooms", "research", "crew"):
            self.assertIs(snapshot.section(section), Completeness.MISSING, section)
        self.assertIs(snapshot.section("inventory"), Completeness.COMPLETE)
        self.assertEqual(provider.failed_reads, ("getShipByUserId", "listAllCharactersOfUser"))

    def test_untyped_items_do_not_become_resources(self):
        items = {"Items": {"Item": [{"@ItemDesignId": "1", "@Quantity": "5"}, {"@ItemDesignId": "2", "@Quantity": "6"}]}}
        _, snapshot, _ = provide(FakeClient(items=items))
        self.assertIsNone(snapshot.resources.minerals)
        self.assertIsNone(snapshot.resources.gas)

    def test_live_shaped_items_become_resources_through_their_item_designs(self):
        # Regression (first G2 canary, 2026-11-12): live ListItemsOfAShip items carry no @Type or
        # @ItemType, so minerals and gas were never observed and every cost-bearing action was refused.
        items = {"ItemService": {"ListItemsOfAShip": {"Items": {"Item": [
            dict(LIVE_ITEM, **{"@ItemDesignId": "1", "@Quantity": "1200"}),
            dict(LIVE_ITEM, **{"@ItemDesignId": "2", "@Quantity": "800"}),
            dict(LIVE_ITEM, **{"@ItemDesignId": "900", "@Quantity": "2"}),
        ]}}}}
        kinds = item_resource_kinds(ITEM_DESIGNS)
        self.assertEqual(kinds, {"1": "mineral", "2": "gas", "3": "mineral"})  # case-insensitive
        _, snapshot, _ = provide(FakeClient(items=items), resource_item_designs=kinds)
        self.assertEqual((snapshot.resources.minerals, snapshot.resources.gas), (1200, 800))
        _, snapshot, _ = provide(FakeClient(items=items))  # no designs: unobserved, never 0
        self.assertEqual((snapshot.resources.minerals, snapshot.resources.gas), (None, None))

    def test_ambiguous_or_unmapped_item_designs_leave_resources_unobserved(self):
        def items(*design_ids):
            return {"Items": {"Item": [dict(LIVE_ITEM, **{"@ItemDesignId": design_id, "@Quantity": "5"})
                                       for design_id in design_ids]}}
        kinds = item_resource_kinds(ITEM_DESIGNS)
        for design_ids in (
            ("1", "1", "2"),    # two mineral items: ambiguous
            ("1", "3", "2"),    # two mineral designs held
            ("900", "2"),       # no mineral item
            ("77", "2"),        # an item whose design was not read
        ):
            with self.subTest(design_ids=design_ids):
                _, snapshot, _ = provide(FakeClient(items=items(*design_ids)), resource_item_designs=kinds)
                self.assertEqual((snapshot.resources.minerals, snapshot.resources.gas), (None, 5))
        self.assertEqual(item_resource_kinds([
            {"@ItemDesignId": "5", "@ItemType": "MineralGas"},    # names both
            {"@ItemDesignId": "6", "@ItemType": "Mineral"},
            {"@ItemDesignId": "6", "@ItemType": "Gas"},           # the same id typed twice
            {"@ItemType": "Gas"},                                 # no id
            "not a record",
        ]), {})
        self.assertEqual(item_resource_kinds(None), {})
        # Verifier finding: a resource type and a non-resource type for one id conflict, in either order.
        for records in ([{"@ItemDesignId": "7", "@ItemType": "Mineral"}, {"@ItemDesignId": "7", "@ItemType": "Equipment"}],
                        [{"@ItemDesignId": "7", "@ItemType": "Equipment"}, {"@ItemDesignId": "7", "@ItemType": "Mineral"}]):
            with self.subTest(records=records):
                self.assertEqual(item_resource_kinds(records), {})
        self.assertEqual(item_resource_kinds([{"@ItemDesignId": "8", "@ItemType": "Gas"},
                                              {"@ItemDesignId": "8", "@ItemType": "gas"}]), {"8": "gas"})

    def test_an_items_own_type_still_decides(self):
        # The existing @Type/@ItemType path is kept: a design mapping never overrides an item's own type.
        _, snapshot, _ = provide(resource_item_designs={"1": "gas", "2": "mineral"})
        self.assertEqual((snapshot.resources.minerals, snapshot.resources.gas), (1200, 800))

    def test_no_secret_reaches_the_snapshot(self):
        _, snapshot, _ = provide()
        serialised = json.dumps(snapshot.to_dict())
        self.assertNotIn(SYNTHETIC_EMAIL, serialised)
        self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, serialised)

    def test_present_but_empty_containers_are_observed_and_empty(self):
        # Regression: <Researches/> parses to None and used to leave research "missing".
        ship = {"Ship": {"@ShipLevel": "1", "Rooms": {}, "Researches": None}}
        crew = {"CharacterService": {"ListAllCharactersOfUser": {"Characters": None}}}
        items = {"ItemService": {"ListItemsOfAShip": {"Items": None}}}
        provider, snapshot, _ = provide(FakeClient(ship=ship, crew=crew, items=items))
        for section in ("rooms", "research", "crew", "inventory"):
            self.assertIs(snapshot.section(section), Completeness.COMPLETE, section)
        self.assertEqual((snapshot.rooms, snapshot.research, snapshot.crew, snapshot.inventory), ((), (), (), ()))
        self.assertEqual(provider.failed_reads, ())

    def test_absent_containers_stay_missing(self):
        ship = {"Ship": {"@ShipLevel": "1"}}
        crew = {"CharacterService": {"ListAllCharactersOfUser": {}}}
        provider, snapshot, _ = provide(FakeClient(ship=ship, crew=crew))
        for section in ("rooms", "research", "crew"):
            self.assertIs(snapshot.section(section), Completeness.MISSING, section)
        self.assertEqual(provider.failed_reads, ("listAllCharactersOfUser",))

    def test_new_ship_with_no_research_gets_research_planned(self):
        from sdk.policy import DesignData, PolicyConfig, get_policy

        ship = {"Ship": {"@ShipLevel": "1", "Rooms": {"Room": {"@RoomId": "r1", "@RoomDesignId": "100"}},
                         "Researches": None}}
        _, snapshot, _ = provide(FakeClient(ship=ship))
        design = DesignData.from_records(
            research_designs=[{"@ResearchDesignId": "510", "@RootResearchDesignId": "510", "@GasCost": "50"}]
        )
        plan = get_policy("baseline-v1").plan(snapshot, design, PolicyConfig())
        self.assertIn(("add_research", {"research_design_id": "510"}),
                      [(request.action_id, dict(request.params)) for request in plan.requests])

    def test_invalid_alias_is_refused(self):
        with self.assertRaises(SnapshotInputError):
            ClientSnapshotProvider(FakeClient(), SYNTHETIC_EMAIL)

    def test_every_call_reads_again(self):
        provider, _, client = provide()
        provider()
        self.assertEqual(client.calls, list(READ_METHODS) * 2)


if __name__ == "__main__":
    unittest.main()
