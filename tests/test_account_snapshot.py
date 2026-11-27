"""Tests for sdk.state account snapshots. Synthetic data only; no network."""

import dataclasses
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from sdk.state import (
    AccountMismatchError,
    Completeness,
    SnapshotInputError,
    build_snapshot,
    diff_snapshots,
)
from sdk.state.baseline import load_baseline, write_baseline
from sdk.training.model import (
    SESSION_ENDED_UNCLAIMED,
    SESSION_FINISHED,
    SESSION_NONE,
    SESSION_RUNNING,
    SESSION_UNKNOWN,
    CoursePrice,
    TrainingSession,
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

    def test_training_state_is_observed_only_when_present(self):
        idle = [dict(CREW[0], **{"@TrainingEndDate": "", "@TrainingDesignId": "0"}), CREW[1]]
        busy = [dict(CREW[0], **{"@TrainingEndDate": "2026-11-10T13:00:00", "@TrainingDesignId": "7"}), CREW[1]]
        before = full_snapshot(crew=idle)
        after = full_snapshot(observed_at=T0 + timedelta(seconds=1), crew=busy)
        self.assertIs(before.crew_member("c1").training_active, False)
        self.assertIsNone(before.crew_member("c1").training_design_id)
        self.assertIs(after.crew_member("c1").training_active, True)
        self.assertIsNone(after.crew_member("c2").training_active)
        self.assertEqual(
            diff_snapshots(before, after).crew_training,
            {"c1": ((None, None), ("7", "2026-11-10T13:00:00"))},
        )
        # c2 never reported training, so it has no training delta.
        self.assertNotIn("c2", diff_snapshots(before, after).crew_training)

    def test_any_zero_training_design_is_no_design(self):
        # The game uses design 0 for "no training"; a zero written with more digits is the same placeholder, not a
        # design id. Non-numeric text is kept as reported.
        for value, expected in (("0", None), ("00", None), ("000", None), (" 0", None), ("0 ", None), (0, None),
                                ("7", "7"), ("07", "07"), ("gas", "gas"), (" ", " "), ("", None)):
            with self.subTest(training_design_id=value):
                crew = [dict(CREW[0], **{"@TrainingEndDate": "", "@TrainingDesignId": value}), CREW[1]]
                self.assertEqual(full_snapshot(crew=crew).crew_member("c1").training_design_id, expected)

    def test_cross_account_comparison_is_refused(self):
        with self.assertRaises(AccountMismatchError):
            diff_snapshots(full_snapshot("alpha"), full_snapshot("bravo", observed_at=T0 + timedelta(seconds=1)))

    def test_reversed_time_order_is_refused(self):
        with self.assertRaises(SnapshotInputError):
            diff_snapshots(full_snapshot(observed_at=T0 + timedelta(seconds=1)), full_snapshot())


# The nine stat improvements a character record carries, spelled out literally (attribute -> field -> value). Distinct
# powers of two make every total unique, so a dropped or doubled stat changes the sum.
STAT_CASES = (
    ("@HpImprovement", "hp_improvement", 1),
    ("@PilotImprovement", "pilot_improvement", 2),
    ("@RepairImprovement", "repair_improvement", 4),
    ("@WeaponImprovement", "weapon_improvement", 8),
    ("@ScienceImprovement", "science_improvement", 16),
    ("@EngineImprovement", "engine_improvement", 32),
    ("@AttackImprovement", "attack_improvement", 64),
    ("@AbilityImprovement", "ability_improvement", 128),
    ("@StaminaImprovement", "stamina_improvement", 256),
)
STATS = {attribute: str(value) for attribute, _, value in STAT_CASES}


def crew_with_stats(stats=None, **extra):
    """c1 with the given stat attributes (default all nine), c2 with none."""
    return [dict(CREW[0], **(STATS if stats is None else stats), **extra), CREW[1]]


class CrewTrainingStatsTests(unittest.TestCase):
    def test_stat_improvements_are_read_into_fields_and_total(self):
        member = full_snapshot(crew=crew_with_stats()).crew_member("c1")
        for _, field_name, value in STAT_CASES:
            with self.subTest(field=field_name):
                self.assertEqual(getattr(member, field_name), value)
        self.assertEqual(member.stat_total, 511)

    def test_any_unobserved_stat_makes_the_total_unknown(self):
        for attribute, field_name, _ in STAT_CASES:
            with self.subTest(removed=attribute):
                stats = {key: value for key, value in STATS.items() if key != attribute}
                member = full_snapshot(crew=crew_with_stats(stats)).crew_member("c1")
                self.assertIsNone(getattr(member, field_name))
                self.assertIsNone(member.stat_total)
            with self.subTest(unparseable=attribute):
                member = full_snapshot(crew=crew_with_stats({**STATS, attribute: "x"})).crew_member("c1")
                self.assertIsNone(getattr(member, field_name))
                self.assertIsNone(member.stat_total)
        self.assertIsNone(full_snapshot().crew_member("c2").stat_total)

    def test_training_capacity_only_when_the_record_carries_it(self):
        carried = full_snapshot(crew=crew_with_stats(**{"@TrainingCapacity": "600"}))
        self.assertEqual(carried.crew_member("c1").training_capacity, 600)
        self.assertIsNone(carried.crew_member("c2").training_capacity)
        self.assertIsNone(full_snapshot(crew=crew_with_stats()).crew_member("c1").training_capacity)
        # Capacity is a limit, not an improvement: it never counts toward the total.
        self.assertEqual(carried.crew_member("c1").stat_total, 511)

    def test_training_end_utc(self):
        def end_of(value):
            return full_snapshot(crew=crew_with_stats(**{"@TrainingEndDate": value})).crew_member("c1").training_end_utc

        expected = datetime(2026, 11, 10, 13, 0, tzinfo=timezone.utc)
        naive = end_of("2026-11-10T13:00:00")
        self.assertEqual(naive, expected)
        self.assertEqual(naive.utcoffset(), timedelta(0))
        offset = end_of("2026-11-10T08:00:00-05:00")
        self.assertEqual(offset, expected)
        self.assertEqual(offset.utcoffset(), timedelta(0))
        # Surrounding whitespace is stripped before parsing; blank text is no time.
        self.assertEqual(end_of(" 2026-11-10T13:00:00\n"), expected)
        for value in ("", "soon", "   "):
            with self.subTest(value=value):
                self.assertIsNone(end_of(value))
        self.assertIsNone(full_snapshot().crew_member("c1").training_end_utc)

    def test_to_dict_carries_stats_and_is_json_serialisable(self):
        payload = json.loads(json.dumps(full_snapshot(crew=crew_with_stats()).to_dict()))
        self.assertEqual(payload["crew"][0]["stamina_improvement"], 256)
        self.assertEqual(payload["crew"][0]["hp_improvement"], 1)
        self.assertIsNone(payload["crew"][0]["training_capacity"])
        self.assertIsNone(payload["crew"][1]["hp_improvement"])

    def test_stat_total_change_only_for_crew_observed_on_both_sides(self):
        def delta(before_crew, after_crew):
            before = full_snapshot(crew=before_crew)
            after = full_snapshot(observed_at=T0 + timedelta(seconds=1), crew=after_crew)
            return diff_snapshots(before, after).crew_stat_total

        raised = crew_with_stats({**STATS, "@HpImprovement": "4"})
        self.assertEqual(delta(crew_with_stats(), raised), {"c1": 3})
        self.assertEqual(delta(raised, crew_with_stats()), {"c1": -3})
        self.assertEqual(delta(crew_with_stats(), crew_with_stats()), {})
        partial = crew_with_stats({key: value for key, value in STATS.items() if key != "@StaminaImprovement"})
        self.assertEqual(delta(crew_with_stats(), partial), {})
        self.assertEqual(delta(partial, raised), {})
        self.assertEqual(delta(None, raised), {})
        self.assertEqual(delta(crew_with_stats(), None), {})
        self.assertNotIn("c2", delta(crew_with_stats(), raised))
        # A crew member new after the call has no total before, so no change, however many stats it shows.
        joined = crew_with_stats() + [{"@CharacterId": "c3", **STATS}]
        self.assertEqual(delta(crew_with_stats(), joined), {})


class HullUpgradeFieldTests(unittest.TestCase):
    """The ship's @UpgradeShipDesignId, as the live ship record carries it (rooms/buy_room2.json ship_after)."""

    def live_ship(self):
        import os
        from pathlib import Path

        import xmltodict

        fixture = Path(os.path.abspath(__file__)).parents[1] / "tests/fixtures/live_sanitized/rooms/buy_room2.json"
        text = json.loads(fixture.read_text(encoding="utf-8"))["ship_after"]
        return xmltodict.parse(text)["ShipService"]["GetShipByUserId"]["Ship"]

    def test_no_upgrade_under_way_is_none_despite_a_stale_start_date(self):
        ship = self.live_ship()
        self.assertEqual((ship["@UpgradeShipDesignId"], ship["@UpgradeStartDate"]), ("0", "2026-10-19T19:15:13"))
        snapshot = build_snapshot("alpha", T0, "fixture", ship=ship)
        self.assertEqual((snapshot.ship_design_id, snapshot.hull_level, snapshot.upgrade_ship_design_id),
                         ("2", 1, None))

    def test_an_upgrade_under_way_is_read_and_left_out_of_equality(self):
        started = build_snapshot("alpha", T0, "fixture", ship={**SHIP, "@UpgradeShipDesignId": "107"})
        self.assertEqual(started.upgrade_ship_design_id, "107")
        self.assertEqual(started.to_dict()["upgrade_ship_design_id"], "107")
        self.assertEqual(started, build_snapshot("alpha", T0, "fixture", ship=SHIP))
        self.assertIsNone(build_snapshot("alpha", T0, "fixture", ship=SHIP).upgrade_ship_design_id)
        self.assertIsNone(build_snapshot("alpha", T0, "fixture").upgrade_ship_design_id)

    def test_a_padded_zero_or_a_leftover_equal_to_the_hull_is_no_upgrade(self):
        for value in (" 0 ", "", SHIP["@ShipDesignId"], f" {SHIP['@ShipDesignId']}"):
            with self.subTest(value=value):
                snapshot = build_snapshot("alpha", T0, "fixture", ship={**SHIP, "@UpgradeShipDesignId": value})
                self.assertIsNone(snapshot.upgrade_ship_design_id)
        padded = build_snapshot("alpha", T0, "fixture", ship={**SHIP, "@UpgradeShipDesignId": " 107 "})
        self.assertEqual(padded.upgrade_ship_design_id, "107")


TRAINING_DESIGNS = {
    "TrainingService": {
        "ListAllTrainingDesigns": {
            "TrainingDesigns": {
                "TrainingDesign": [
                    {"@TrainingDesignId": "40001", "@TrainingName": "Engineering Manual", "@RepairChance": "100",
                     "@Rank": "1", "@RequiredRoomType": "Academy", "@RequiredRoomLevel": "2",
                     "@RequiredResearchDesignId": "60001", "@CostString": "mineral:500"},
                    {"@TrainingDesignId": "40002", "@TrainingName": "Free Weights", "@HpChance": "100"},
                ]
            }
        }
    }
}
ROOM_DESIGNS = {
    "RoomService": {
        "ListRoomDesigns": {
            "RoomDesigns": {
                "RoomDesign": [
                    {"@RoomDesignId": "11", "@RoomName": "Academy", "@Level": "2", "@Capacity": "2"},
                    {"@RoomDesignId": "21", "@RoomName": "Gym", "@Level": "1"},
                    {"@RoomDesignId": "31", "@RoomName": "Closed Academy", "@Level": "1", "@Capacity": "0"},
                ]
            }
        }
    }
}
ERROR_PAYLOAD = {"TrainingService": {"@errorMessage": "Failed"}}


def room_facts(snapshot, room_id):
    room = snapshot.room(room_id)
    return room.design_name, room.level, room.capacity


# Verifies: S14-R2
class TrainingObservationTests(unittest.TestCase):
    def test_without_design_payloads_nothing_is_observed(self):
        snapshot = full_snapshot()
        self.assertIsNone(snapshot.courses)
        self.assertIsNone(snapshot.course(40001))
        self.assertEqual(room_facts(snapshot, "r1"), (None, None, None))

    def test_training_designs_become_courses(self):
        snapshot = full_snapshot(training_designs=TRAINING_DESIGNS)
        self.assertIsInstance(snapshot.courses, tuple)
        self.assertEqual([course.design_id for course in snapshot.courses], [40001, 40002])
        self.assertEqual(snapshot.course(40001).price, CoursePrice("ok", "mineral", 500))
        self.assertEqual(snapshot.course(40001).required_room_level, 2)
        self.assertEqual(snapshot.course(40002).price, CoursePrice("unavailable"))
        self.assertIsNone(snapshot.course(999))

    def test_an_empty_design_list_is_observed_and_an_error_is_not(self):
        empty = full_snapshot(training_designs={"TrainingService": {"ListAllTrainingDesigns": {"TrainingDesigns": None}}})
        self.assertEqual(empty.courses, ())
        self.assertIsNone(empty.course(40001))
        for payload in (ERROR_PAYLOAD, {"Unexpected": {}}, "junk"):
            with self.subTest(payload=payload):
                self.assertIsNone(full_snapshot(training_designs=payload).courses)

    def test_rooms_are_joined_with_their_design(self):
        rooms = ROOMS + [
            {"@RoomId": "r3", "@RoomDesignId": "31", "@RoomStatus": "Normal"},
            {"@RoomId": "r4", "@RoomDesignId": "99", "@RoomStatus": "Normal"},
            {"@RoomId": "r5", "@RoomStatus": "Normal"},
        ]
        snapshot = full_snapshot(rooms=rooms, room_designs=ROOM_DESIGNS)
        self.assertEqual(room_facts(snapshot, "r1"), ("Academy", 2, 2))
        self.assertEqual(room_facts(snapshot, "r2"), ("Gym", 1, None))
        self.assertEqual(room_facts(snapshot, "r3"), ("Closed Academy", 1, 0))
        self.assertEqual(room_facts(snapshot, "r4"), (None, None, None))
        self.assertEqual(room_facts(snapshot, "r5"), (None, None, None))
        self.assertEqual([room.status for room in snapshot.rooms], ["Normal", "Upgrading", "Normal", "Normal", "Normal"])
        self.assertTrue(snapshot.room("r2").upgrading)
        self.assertIs(snapshot.section("rooms"), Completeness.COMPLETE)

    def test_a_nameless_room_design_has_no_name(self):
        designs = {"RoomDesigns": {"RoomDesign": {"@RoomDesignId": "11", "@Level": "2"}}}
        self.assertEqual(room_facts(full_snapshot(room_designs=designs), "r1"), (None, 2, None))

    def test_a_room_design_error_observes_nothing(self):
        for payload in ({"RoomService": {"@errorMessage": "Failed"}}, {"Unexpected": {}}):
            with self.subTest(payload=payload):
                snapshot = full_snapshot(room_designs=payload)
                self.assertEqual(room_facts(snapshot, "r1"), (None, None, None))

    def test_design_facts_stay_out_of_equality_and_baselines(self):
        enriched = full_snapshot(training_designs=TRAINING_DESIGNS, room_designs=ROOM_DESIGNS)
        self.assertEqual(enriched, full_snapshot())
        with tempfile.TemporaryDirectory() as directory:
            path = write_baseline(enriched, directory, "0.999.55")
            loaded = load_baseline(path)
        self.assertEqual(loaded, enriched)
        self.assertIsNone(loaded.courses)

    def test_to_dict_keeps_its_top_level_keys_and_carries_room_facts(self):
        enriched = full_snapshot(training_designs=TRAINING_DESIGNS, room_designs=ROOM_DESIGNS).to_dict()
        payload = json.loads(json.dumps(enriched))
        self.assertEqual(set(payload), set(full_snapshot().to_dict()))
        self.assertNotIn("courses", payload)
        self.assertEqual(
            {key: payload["rooms"][0][key] for key in ("design_name", "level", "capacity")},
            {"design_name": "Academy", "level": 2, "capacity": 2},
        )

    def test_training_session_is_judged_at_the_observation_time(self):
        later = (T0 + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        earlier = (T0 - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        at_t0 = T0.strftime("%Y-%m-%dT%H:%M:%S")
        crew = [
            {"@CharacterId": "c1", "@TrainingEndDate": "", "@TrainingDesignId": "0"},
            {"@CharacterId": "c2", "@TrainingEndDate": later, "@TrainingDesignId": "7"},
            {"@CharacterId": "c3", "@TrainingEndDate": at_t0, "@TrainingDesignId": "7"},
            {"@CharacterId": "c4", "@TrainingEndDate": earlier, "@TrainingDesignId": "0"},
            {"@CharacterId": "c5", "@TrainingDesignId": "7"},
        ]
        snapshot = full_snapshot(crew=crew)
        self.assertEqual(snapshot.training_session("c1"), TrainingSession(None, None, SESSION_NONE))
        self.assertEqual(snapshot.training_session("c2"), TrainingSession(7, T0 + timedelta(hours=1), SESSION_RUNNING))
        self.assertEqual(snapshot.training_session("c3"), TrainingSession(7, T0, SESSION_ENDED_UNCLAIMED))
        self.assertEqual(snapshot.training_session("c4"), TrainingSession(None, T0 - timedelta(hours=1), SESSION_FINISHED))
        self.assertEqual(snapshot.training_session("c5"), TrainingSession(None, None, SESSION_UNKNOWN))
        self.assertIsNone(snapshot.training_session("c9"))
        two_hours_on = full_snapshot(crew=crew, observed_at=T0 + timedelta(hours=2))
        self.assertEqual(two_hours_on.training_session("c2").phase, SESSION_ENDED_UNCLAIMED)

    def test_a_missing_or_blank_course_is_not_an_explicit_zero(self):
        earlier = (T0 - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        later = (T0 + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        crew = [
            {"@CharacterId": "d1", "@TrainingEndDate": earlier},
            {"@CharacterId": "d2", "@TrainingEndDate": earlier, "@TrainingDesignId": ""},
            {"@CharacterId": "d3", "@TrainingEndDate": earlier, "@TrainingDesignId": " 0 "},
            {"@CharacterId": "d4", "@TrainingEndDate": earlier, "@TrainingDesignId": "00"},
            {"@CharacterId": "d5", "@TrainingEndDate": later},
        ]
        snapshot = full_snapshot(crew=crew)
        self.assertEqual(
            [snapshot.crew_member(cid).training_design_reported for cid in ("d1", "d2", "d3", "d4")],
            [None, "", "0", "00"],
        )
        self.assertEqual({snapshot.crew_member(cid).training_design_id for cid in ("d1", "d2", "d3", "d4")}, {None})
        self.assertEqual(snapshot.training_session("d1"), TrainingSession(None, None, SESSION_UNKNOWN))
        self.assertEqual(snapshot.training_session("d2"), TrainingSession(None, None, SESSION_UNKNOWN))
        self.assertEqual(snapshot.training_session("d3").phase, SESSION_FINISHED)
        self.assertEqual(snapshot.training_session("d4").phase, SESSION_FINISHED)
        self.assertEqual(snapshot.training_session("d5"), TrainingSession(None, T0 + timedelta(hours=1), SESSION_RUNNING))
        # The reported text stays out of equality and baselines; a loaded member falls back to its design id.
        self.assertEqual(snapshot, full_snapshot(crew=[{**record, "@TrainingDesignId": "0"} for record in crew]))
        ended = full_snapshot(crew=[{"@CharacterId": "c1", "@TrainingEndDate": earlier, "@TrainingDesignId": "7"}])
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_baseline(write_baseline(ended, directory, "0.999.55"))
        self.assertEqual(loaded, ended)
        self.assertIsNone(loaded.crew_member("c1").training_design_reported)
        self.assertEqual(loaded.training_session("c1"), ended.training_session("c1"))
        self.assertEqual(loaded.training_session("c1").phase, SESSION_ENDED_UNCLAIMED)

    def test_an_empty_end_is_no_training_and_a_missing_one_is_unknown(self):
        crew = [
            {"@CharacterId": "c1", "@TrainingEndDate": "", "@TrainingDesignId": "7"},
            {"@CharacterId": "c2", "@TrainingDesignId": "0"},
            {"@CharacterId": "c3", "@TrainingEndDate": "   ", "@TrainingDesignId": "7"},
        ]
        snapshot = full_snapshot(crew=crew)
        self.assertEqual(snapshot.training_session("c1"), TrainingSession(None, None, SESSION_NONE))
        self.assertEqual(snapshot.training_session("c2"), TrainingSession(None, None, SESSION_UNKNOWN))
        self.assertEqual(snapshot.training_session("c3"), TrainingSession(7, None, SESSION_UNKNOWN))

    def test_a_naive_observation_time_gives_an_unknown_session(self):
        later = (T0 + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        crew = [{"@CharacterId": "c1", "@TrainingEndDate": later, "@TrainingDesignId": "7"}]
        snapshot = dataclasses.replace(full_snapshot(crew=crew), observed_at=T0.replace(tzinfo=None))
        self.assertEqual(snapshot.training_session("c1"), TrainingSession(None, None, SESSION_UNKNOWN))
        self.assertIsNone(snapshot.training_session("c9"))

    def test_course_accepts_an_int_or_a_digit_string(self):
        snapshot = full_snapshot(training_designs=TRAINING_DESIGNS)
        for design_id in (40001, "40001", " 40001 ", "040001"):
            with self.subTest(design_id=design_id):
                self.assertEqual(snapshot.course(design_id).design_id, 40001)
        for design_id in ("x", "-40001", "40001.0", True, None, 0, "", 40001.0):
            with self.subTest(design_id=design_id):
                self.assertIsNone(snapshot.course(design_id))

    def test_room_type_is_joined_from_the_room_design(self):
        designs = {
            "RoomDesigns": {
                "RoomDesign": [
                    {"@RoomDesignId": "11", "@RoomName": "Academy", "@RoomType": " Training "},
                    {"@RoomDesignId": "21", "@RoomName": "Gym"},
                ]
            }
        }
        rooms = ROOMS + [{"@RoomId": "r3", "@RoomDesignId": "99", "@RoomStatus": "Normal"}]
        snapshot = full_snapshot(rooms=rooms, room_designs=designs)
        self.assertEqual([snapshot.room(room_id).room_type for room_id in ("r1", "r2", "r3")], ["Training", None, None])
        self.assertIsNone(full_snapshot().room("r1").room_type)
        self.assertEqual(snapshot, full_snapshot(rooms=rooms))


def training_crew(*members):
    """Crew records in rooms: each member is (character id, room id, design id, end)."""
    return [
        {"@CharacterId": cid, "@RoomId": room, "@TrainingDesignId": design, "@TrainingEndDate": end}
        for cid, room, design, end in members
    ]


# Verifies: S14-R2
class RunningSessionsTests(unittest.TestCase):
    later = (T0 + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    earlier = (T0 - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")

    def test_an_observed_zero_is_kept(self):
        snapshot = full_snapshot(crew=training_crew(("c1", "r1", "0", ""), ("c2", "r2", "7", self.later)))
        self.assertEqual(snapshot.running_sessions("r1"), 0)
        self.assertEqual(snapshot.running_sessions("r9"), 0)

    def test_running_members_of_the_room_are_counted(self):
        crew = training_crew(
            ("c1", "r1", "7", self.later),
            ("c2", "r1", "8", self.later),
            ("c3", "r1", "0", ""),
            ("c4", "r2", "7", self.later),
        )
        self.assertEqual(full_snapshot(crew=crew).running_sessions("r1"), 2)
        self.assertEqual(full_snapshot(crew=crew[:1]).running_sessions("r1"), 1)

    def test_an_ended_unclaimed_course_is_not_running(self):
        crew = training_crew(("c1", "r1", "7", self.earlier), ("c2", "r1", "8", self.later))
        self.assertEqual(full_snapshot(crew=crew).running_sessions("r1"), 1)

    def test_an_unknown_member_in_the_room_makes_the_count_unknown(self):
        crew = training_crew(("c1", "r1", "7", self.later), ("c2", "r1", "7", "soon"))
        self.assertIsNone(full_snapshot(crew=crew).running_sessions("r1"))
        unobserved = training_crew(("c1", "r1", "7", self.later)) + [{"@CharacterId": "c2", "@RoomId": "r1"}]
        self.assertIsNone(full_snapshot(crew=unobserved).running_sessions("r1"))
        elsewhere = training_crew(("c1", "r1", "7", self.later), ("c2", "r2", "7", "soon"))
        self.assertEqual(full_snapshot(crew=elsewhere).running_sessions("r1"), 1)

    def test_an_incomplete_crew_section_gives_no_count(self):
        partial = training_crew(("c1", "r1", "7", self.later)) + [{"@RoomId": "r1"}]
        snapshot = full_snapshot(crew=partial)
        self.assertIs(snapshot.section("crew"), Completeness.PARTIAL)
        self.assertIsNone(snapshot.running_sessions("r1"))
        missing = build_snapshot("alpha", T0, "synthetic-fixture")
        self.assertIsNone(missing.running_sessions("r1"))


if __name__ == "__main__":
    unittest.main()
