"""Tests for sdk.state account snapshots. Synthetic data only; no network."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from sdk.state import (
    AccountMismatchError,
    Completeness,
    SnapshotInputError,
    build_snapshot,
    diff_snapshots,
)
from tests.synthetic_fixtures import SYNTHETIC_ACCESS_TOKEN, SYNTHETIC_EMAIL

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

PROFILE = {"@Name": "SyntheticCaptain", "@Trophy": "1200", "@Email": SYNTHETIC_EMAIL}
SHIP = {"@ShipLevel": "6", "@ShipDesignId": "106"}
ROOMS = [
    {"@RoomId": "r1", "@RoomDesignId": "11", "@RoomStatus": "Normal"},
    {"@RoomId": "r2", "@RoomDesignId": "21", "@RoomStatus": "Upgrading"},
]
CREW = [
    {"@CharacterId": "c1", "@CharacterDesignId": "300", "@CharacterName": "Crew One", "@Level": "10",
     "@Fatigue": "0", "@RoomId": "r1"},
    {"@CharacterId": "c2", "@CharacterDesignId": "301", "@CharacterName": "Crew Two", "@Level": "4",
     "@Fatigue": "12", "@RoomId": "r2"},
]
RESEARCH = {"@ResearchDesignId": "500", "@ResearchState": "Completed"}
INVENTORY = [{"@ItemDesignId": "900", "@Quantity": "3"}]
RESOURCES = {"minerals": "1000", "gas": "2000", "starbux": "15"}


def full_snapshot(alias="alpha", observed_at=T0, **overrides):
    kwargs = dict(
        game_version="0.999.55",
        profile=PROFILE,
        ship=SHIP,
        resources=RESOURCES,
        crew=CREW,
        rooms=ROOMS,
        research=RESEARCH,
        inventory=INVENTORY,
    )
    kwargs.update(overrides)
    return build_snapshot(alias, observed_at, "synthetic-fixture", **kwargs)


class BuildSnapshotTests(unittest.TestCase):
    def test_full_input_produces_complete_typed_snapshot(self):
        snapshot = full_snapshot()
        self.assertEqual(snapshot.hull_level, 6)
        self.assertEqual(snapshot.trophies, 1200)
        self.assertEqual(snapshot.resources.minerals, 1000)
        self.assertEqual(snapshot.resources.starbux, 15)
        self.assertEqual([member.character_id for member in snapshot.crew], ["c1", "c2"])
        self.assertTrue(snapshot.room("r2").upgrading)
        self.assertEqual(snapshot.research_state("500"), "Completed")
        self.assertEqual(snapshot.inventory[0].quantity, 3)
        for section in ("profile", "ship", "resources", "crew", "rooms", "research", "inventory"):
            self.assertIs(snapshot.section(section), Completeness.COMPLETE, section)

    def test_single_xml_element_is_accepted_as_one_record(self):
        snapshot = full_snapshot(research={"@ResearchDesignId": "501", "@ResearchState": "Researching"})
        self.assertEqual(len(snapshot.research), 1)

    def test_missing_sections_are_marked_missing_not_invented(self):
        snapshot = build_snapshot("alpha", T0, "synthetic-fixture")
        self.assertIsNone(snapshot.hull_level)
        self.assertIsNone(snapshot.trophies)
        self.assertIsNone(snapshot.resources.minerals)
        self.assertEqual(snapshot.crew, ())
        for section in ("profile", "ship", "resources", "crew", "rooms", "research", "inventory"):
            self.assertIs(snapshot.section(section), Completeness.MISSING, section)

    def test_records_without_identity_make_section_partial(self):
        snapshot = full_snapshot(rooms=ROOMS + [{"@RoomDesignId": "99"}])
        self.assertIs(snapshot.section("rooms"), Completeness.PARTIAL)
        self.assertEqual(len(snapshot.rooms), 2)
        self.assertIn("rooms: skipped 1 record(s) without @RoomId", snapshot.notes)

    def test_unparseable_numbers_become_unknown_and_partial(self):
        snapshot = full_snapshot(ship={"@ShipLevel": "six"}, resources={"minerals": "1", "gas": "x"})
        self.assertIsNone(snapshot.hull_level)
        self.assertIs(snapshot.section("ship"), Completeness.PARTIAL)
        self.assertIsNone(snapshot.resources.gas)
        self.assertIs(snapshot.section("resources"), Completeness.PARTIAL)

    def test_account_email_and_tokens_are_never_copied(self):
        profile = dict(PROFILE, **{"@AccessToken": SYNTHETIC_ACCESS_TOKEN})
        serialised = json.dumps(full_snapshot(profile=profile).to_dict())
        self.assertNotIn(SYNTHETIC_EMAIL, serialised)
        self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, serialised)

    def test_secret_shaped_values_in_copied_fields_are_refused(self):
        crew = [dict(CREW[0], **{"@CharacterName": SYNTHETIC_EMAIL})]
        with self.assertRaises(SnapshotInputError):
            full_snapshot(crew=crew)
        with self.assertRaises(SnapshotInputError):
            build_snapshot("alpha", T0, f"capture accessToken={SYNTHETIC_ACCESS_TOKEN}")

    def test_alias_must_not_be_an_email_or_display_name(self):
        for alias in (SYNTHETIC_EMAIL, "Synthetic Captain", "", "UPPER"):
            with self.subTest(alias=alias), self.assertRaises(SnapshotInputError):
                build_snapshot(alias, T0, "synthetic-fixture")

    def test_naive_timestamp_is_refused(self):
        with self.assertRaises(SnapshotInputError):
            build_snapshot("alpha", datetime(2026, 11, 10, 12, 0), "synthetic-fixture")

    def test_malformed_section_is_refused(self):
        with self.assertRaises(SnapshotInputError):
            full_snapshot(rooms="not-a-list")

    def test_to_dict_is_json_serialisable(self):
        payload = full_snapshot().to_dict()
        self.assertEqual(json.loads(json.dumps(payload))["completeness"]["crew"], "complete")
        self.assertTrue(payload["rooms"][1]["upgrading"])


class DiffSnapshotTests(unittest.TestCase):
    def test_observed_changes_are_reported(self):
        before = full_snapshot()
        after = full_snapshot(
            observed_at=T0 + timedelta(minutes=5),
            resources={"minerals": "1500", "gas": "1800", "starbux": "15"},
            rooms=[ROOMS[0], dict(ROOMS[1], **{"@RoomDesignId": "22", "@RoomStatus": "Normal"})],
            crew=[dict(CREW[0], **{"@Level": "11"}), CREW[1]],
            research=[RESEARCH, {"@ResearchDesignId": "501", "@ResearchState": "Researching"}],
            inventory=[{"@ItemDesignId": "900", "@Quantity": "1"}, {"@ItemDesignId": "901", "@Quantity": "2"}],
        )
        delta = diff_snapshots(before, after)
        self.assertEqual(delta.elapsed_seconds, 300)
        self.assertEqual((delta.minerals, delta.gas, delta.starbux), (500, -200, 0))
        self.assertEqual(delta.room_design, {"r2": ("21", "22")})
        self.assertEqual(delta.room_status, {"r2": ("Upgrading", "Normal")})
        self.assertEqual(delta.crew_level, {"c1": 1})
        self.assertEqual(delta.research_state, {"501": (None, "Researching")})
        self.assertEqual(delta.inventory_quantity, {"900": -2, "901": 2})
        self.assertEqual(delta.unknown_sections, ())

    def test_unknown_values_produce_unknown_deltas(self):
        before = full_snapshot(resources=None)
        after = full_snapshot(observed_at=T0 + timedelta(seconds=1))
        delta = diff_snapshots(before, after)
        self.assertIsNone(delta.minerals)
        self.assertEqual(delta.trophies, 0)

    def test_missing_section_on_one_side_is_unknown_not_removed(self):
        before = full_snapshot()
        after = full_snapshot(observed_at=T0 + timedelta(seconds=1), rooms=None, inventory=None)
        delta = diff_snapshots(before, after)
        self.assertEqual(delta.room_design, {})
        self.assertEqual(delta.inventory_quantity, {})
        self.assertEqual(delta.unknown_sections, ("rooms", "inventory"))

    def test_unparseable_inventory_quantity_is_unknown(self):
        before = full_snapshot()
        after = full_snapshot(
            observed_at=T0 + timedelta(seconds=1), inventory=[{"@ItemDesignId": "900", "@Quantity": "?"}]
        )
        self.assertEqual(diff_snapshots(before, after).inventory_quantity, {"900": None})

    def test_cross_account_comparison_is_refused(self):
        with self.assertRaises(AccountMismatchError):
            diff_snapshots(full_snapshot("alpha"), full_snapshot("bravo", observed_at=T0 + timedelta(seconds=1)))

    def test_reversed_time_order_is_refused(self):
        with self.assertRaises(SnapshotInputError):
            diff_snapshots(full_snapshot(observed_at=T0 + timedelta(seconds=1)), full_snapshot())


if __name__ == "__main__":
    unittest.main()
