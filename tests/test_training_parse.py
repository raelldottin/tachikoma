from __future__ import annotations

"""Tests for sdk.training.parse — tolerant XML-dict to training model conversion (synthetic fixtures only)."""

import os
import random
import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import xmltodict

from sdk.training.model import CrewState, FinishTraining, Skip, StartTraining, TrainingDesign, UseConsumable
from sdk.training.parse import (
    build_context,
    classify_payload,
    extract_collection,
    parse_consumable_designs,
    parse_crew,
    parse_datetime,
    parse_items,
    parse_research_levels,
    parse_rooms,
    parse_training_designs,
    to_int,
)
from sdk.training.planner import plan_training
from sdk.training.strategy import DEFAULT_STRATEGY, with_consumables

UTC = timezone.utc
FIXTURES = Path(os.path.abspath(__file__)).parent / "fixtures" / "training"
FIXTURE_NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def load(name: str) -> dict:
    return xmltodict.parse((FIXTURES / name).read_text(encoding="utf-8"), xml_attribs=True)


def fixture_context():
    return build_context(
        now=FIXTURE_NOW,
        characters_raw=load("characters.xml"),
        character_designs_raw=load("character_designs.xml"),
        training_designs_raw=load("training_designs_list.xml"),
        rooms_raw=load("rooms.xml"),
        room_designs_raw=load("room_designs.xml"),
        researches_raw=load("researches.xml"),
        research_designs_raw=load("research_designs.xml"),
        items_raw=load("items.xml"),
    )


class TestScalars(unittest.TestCase):
    def test_to_int_table(self) -> None:
        cases = (("7", 7), (" 7 ", 7), ("7.0", 7), (7, 7), ("", 0), (None, 0), ("abc", 0), ("nan", 0), ("inf", 0))
        cases += (("-3", -3), ({"x": 1}, 0), ([1], 0), (float("nan"), 0), (float("inf"), 0), ("1e400", 0))
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(to_int(value), expected)
        self.assertEqual(to_int("abc", default=-1), -1)
        self.assertEqual(to_int(None, default=5), 5)
        self.assertEqual(to_int("9" * 5000), 0)  # beyond int()'s digit limit: default, no exception

    def test_parse_datetime_table(self) -> None:
        invalid = (None, "", "   ", "garbage", "2026-13-01T00:00:00", "2026-01-01", 20260101)
        invalid += ("0001-01-01T00:00:00+02:00", "2026-01-02T03:04:05+25:00")
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(parse_datetime(value))
        base = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        cases = {
            "2026-01-02T03:04:05": base,
            " 2026-01-02T03:04:05 ": base,
            "2026-01-02T03:04:05.5": base.replace(microsecond=500000),
            "2026-01-02T03:04:05.123": base.replace(microsecond=123000),
            "2026-01-02T03:04:05.123456": base.replace(microsecond=123456),
            "2026-01-02T03:04:05.1234567": base.replace(microsecond=123456),
            "2026-01-02T03:04:05Z": base,
            "2026-01-02T03:04:05+00:00": base,
            "2026-01-02T05:04:05+02:00": base,
            "2026-01-02T01:04:05.250-02:00": base.replace(microsecond=250000),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                parsed = parse_datetime(value)
                self.assertEqual(parsed, expected)
                assert parsed is not None
                self.assertIs(parsed.tzinfo, UTC)


class TestCollections(unittest.TestCase):
    def test_extract_collection_shapes(self) -> None:
        for data in (None, "", [], {}, {"X": None}, {"Item": None}, {"Item": "text"}, {"Item": []}):
            with self.subTest(data=data):
                self.assertEqual(extract_collection(data, "Item"), [])
        self.assertEqual(extract_collection({"Item": {"@a": "1"}}, "Item"), [{"@a": "1"}])
        self.assertEqual(
            extract_collection({"Item": [{"@a": "1"}, "junk", None, {"@a": "2"}]}, "Item"),
            [
                {"@a": "1"},
                {"@a": "2"},
            ],
        )
        nested = {"Service": {"List": {"Items": {"Item": [{"@a": "1"}]}}}}
        self.assertEqual(extract_collection(nested, "Item"), [{"@a": "1"}])
        depth_first = {"A": {"B": {"Item": {"@a": "deep-first"}}}, "C": {"Item": {"@a": "later"}}}
        self.assertEqual(extract_collection(depth_first, "Item"), [{"@a": "deep-first"}])
        top_wins = {"Wrapper": {"Item": {"@a": "nested"}}, "Item": {"@a": "top"}}
        self.assertEqual(extract_collection(top_wins, "Item"), [{"@a": "top"}])
        empty_first = {"A": {"Item": None}, "B": {"Item": {"@a": "b"}}}
        self.assertEqual(extract_collection(empty_first, "Item"), [{"@a": "b"}])

    def test_classify_payload(self) -> None:
        envelope = {"TrainingService": {"ListAllTrainingDesigns2": {"TrainingDesigns": None}}}
        cases = (
            (None, "error"),
            ("<html>", "error"),
            (["TrainingDesign"], "error"),
            ({"TrainingService": {"ListAllTrainingDesigns2": {"@errorMessage": "Synthetic failure"}}}, "error"),
            ({"TrainingDesigns": {"TrainingDesign": {"@TrainingDesignId": "1"}}, "@errorMessage": "x"}, "error"),
            (envelope, "empty"),
            ({"TrainingDesigns": {}}, "empty"),
            ({"TrainingDesigns": {"TrainingDesign": []}}, "empty"),
            ({"TrainingService": {"ListAllTrainingDesigns2": {"Unexpected": {"@a": "1"}}}}, "error"),
            ({}, "error"),
            ({"TrainingDesign": {"@TrainingDesignId": "1"}}, "ok"),
            ({"TrainingDesigns": {"TrainingDesign": [{"@TrainingDesignId": "1"}, {"@TrainingDesignId": "2"}]}}, "ok"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(classify_payload(raw, "TrainingDesign"), expected)
        self.assertEqual(classify_payload({"Designs": None}, "TrainingDesign", "Designs"), "empty")


def designs_envelope(records) -> dict:
    return {"TrainingService": {"ListAllTrainingDesigns2": {"TrainingDesigns": {"TrainingDesign": records}}}}


BENCH = {
    "@TrainingDesignId": "31",
    "@TrainingName": "Bench Press",
    "@Rank": "1",
    "@Duration": "3600",
    "@RequiredResearchDesignId": "0",
    "@RequiredRoomType": "Gym",
    "@HpChance": "10",
    "@AttackChance": "60",
    "@StaminaChance": "30",
}


class TestTrainingDesigns(unittest.TestCase):
    def test_designs_missing_none_empty(self) -> None:
        for raw in (None, {}, {"TrainingDesigns": None}, designs_envelope(None), designs_envelope([]), "junk"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_training_designs(raw), ())

    def test_designs_single_dict_fields(self) -> None:
        (design,) = parse_training_designs(designs_envelope(BENCH))
        self.assertEqual(design.id, 31)
        self.assertEqual(design.name, "Bench Press")
        self.assertEqual(design.tier, 1)
        self.assertEqual(design.duration, timedelta(hours=1))
        self.assertEqual(design.stat, "AttackImprovement")
        self.assertIsNone(design.required_research)
        self.assertEqual(design.required_room_type, "gym")

    def test_designs_list_order_preserved(self) -> None:
        records = [{**BENCH, "@TrainingDesignId": str(i), "@TrainingName": f"T{i}"} for i in (5, 2, 9)]
        self.assertEqual([d.id for d in parse_training_designs(designs_envelope(records))], [5, 2, 9])

    def test_designs_skip_bad_ids_and_default_malformed(self) -> None:
        records = [
            {**BENCH, "@TrainingDesignId": "x"},
            {**BENCH, "@TrainingDesignId": "0"},
            {**BENCH, "@TrainingDesignId": "-4"},
            {key: value for key, value in BENCH.items() if key != "@TrainingDesignId"},
            {**BENCH, "@TrainingDesignId": "8", "@Rank": "x", "@Duration": ""},
            {"@TrainingDesignId": "9"},
        ]
        designs = parse_training_designs(designs_envelope(records))
        self.assertEqual([d.id for d in designs], [8, 9])
        self.assertEqual((designs[0].tier, designs[0].duration), (0, timedelta(0)))
        bare = designs[1]
        self.assertEqual(
            (bare.name, bare.tier, bare.stat, bare.required_research, bare.required_room_type),
            ("", 0, None, None, None),
        )

    def test_design_stat_from_highest_chance(self) -> None:
        cases = (
            ({"@WeaponChance": "5", "@RepairChance": "5"}, "RepairImprovement"),
            ({"@StaminaChance": "7", "@HpChance": "7", "@PilotChance": "2"}, "HpImprovement"),
            ({"@ScienceChance": "1"}, "ScienceImprovement"),
            ({"@HpChance": "0", "@PilotChance": "0"}, None),
            ({"@HpChance": "-3", "@PilotChance": "junk"}, None),
            ({}, None),
        )
        for chances, expected in cases:
            with self.subTest(chances=chances):
                (design,) = parse_training_designs({"TrainingDesign": {"@TrainingDesignId": "1", **chances}})
                self.assertEqual(design.stat, expected)

    def test_design_required_research(self) -> None:
        research_designs = {
            "ResearchDesigns": {
                "ResearchDesign": [
                    {"@ResearchDesignId": "40", "@ResearchName": "Advanced Training", "@RootResearchDesignId": "40"},
                    {
                        "@ResearchDesignId": "41",
                        "@ResearchName": "Advanced Training Lv2",
                        "@RootResearchDesignId": "40",
                    },
                ]
            }
        }

        def required(value: str, designs: object = research_designs) -> str | None:
            record = {"@TrainingDesignId": "1", "@RequiredResearchDesignId": value}
            return parse_training_designs({"TrainingDesign": record}, designs)[0].required_research

        self.assertIsNone(required("0"))
        self.assertIsNone(required(""))
        self.assertEqual(required("40"), "Advanced Training")
        self.assertEqual(required("41"), "Advanced Training")
        self.assertEqual(required("77"), "research_design:77")  # unresolvable: fails closed
        self.assertEqual(required("40", None), "research_design:40")

    def test_design_required_room_type_canonical(self) -> None:
        cases = (("Gym", "gym"), (" GYM ", "gym"), ("Academy", "academy"), ("", None), (None, None), ("Lab", None))
        for value, expected in cases:
            with self.subTest(value=value):
                record = {"@TrainingDesignId": "1"}
                if value is not None:
                    record["@RequiredRoomType"] = value
                self.assertEqual(parse_training_designs({"TrainingDesign": record})[0].required_room_type, expected)


STAT_ATTRS = {
    "@HpImprovement": "1",
    "@PilotImprovement": "2",
    "@RepairImprovement": "3",
    "@WeaponImprovement": "4",
    "@ScienceImprovement": "5",
    "@EngineImprovement": "6",
    "@AttackImprovement": "7",
    "@AbilityImprovement": "8",
    "@StaminaImprovement": "9",
}
CHARACTER = {
    "@CharacterId": "1001",
    "@CharacterName": "Delish",
    "@CharacterDesignId": "501",
    "@Level": "20",
    "@RoomId": "11",
    "@Fatigue": "12",
    "@TrainingDesignId": "0",
    "@TrainingEndDate": "",
    **STAT_ATTRS,
}
CHARACTER_DESIGN = {"@CharacterDesignId": "501", "@TrainingCapacity": "150", "@Rarity": "Legendary"}
CHARACTER_DESIGNS = {
    "CharacterService": {
        "ListAllCharacterDesigns": {
            "CharacterDesigns": {
                "CharacterDesign": [
                    CHARACTER_DESIGN,
                    {"@CharacterDesignId": "502", "@TrainingCapacity": "80", "@Rarity": "Epic"},
                ]
            }
        }
    }
}


def characters_envelope(records) -> dict:
    return {"CharacterService": {"ListAllCharactersOfUser": {"Characters": {"Character": records}}}}


class TestCrew(unittest.TestCase):
    def test_crew_shapes(self) -> None:
        for raw in (None, {}, characters_envelope(None), characters_envelope([]), "junk"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_crew(raw, CHARACTER_DESIGNS), ())
        (single,) = parse_crew(characters_envelope(CHARACTER), CHARACTER_DESIGNS)
        self.assertEqual((single.character_id, single.name, single.design_id), (1001, "Delish", 501))
        self.assertEqual(
            (single.level, single.fatigue, single.rarity, single.training_capacity), (20, 12, "Legendary", 150)
        )
        many = parse_crew(characters_envelope([CHARACTER, {**CHARACTER, "@CharacterId": "1002"}]), CHARACTER_DESIGNS)
        self.assertEqual([crew.character_id for crew in many], [1001, 1002])

    def test_crew_stats_without_at_missing_malformed(self) -> None:
        # Regression: stats were keyed '@HpImprovement' and looked up without '@', so consumables never fired.
        (crew,) = parse_crew(characters_envelope(CHARACTER), CHARACTER_DESIGNS)
        self.assertEqual(dict(crew.stats), {key[1:]: int(value) for key, value in STAT_ATTRS.items()})
        self.assertFalse(any(key.startswith("@") for key in crew.stats))
        broken = {key: value for key, value in CHARACTER.items() if key != "@PilotImprovement"}
        broken["@HpImprovement"] = "abc"
        (crew,) = parse_crew(characters_envelope(broken), CHARACTER_DESIGNS)
        self.assertEqual((crew.stats["HpImprovement"], crew.stats["PilotImprovement"]), (0, 0))
        self.assertEqual(crew.stats["StaminaImprovement"], 9)

    def test_crew_design_join_missing_design(self) -> None:
        # Regression: the original for-loop left the *last* design bound when no design matched.
        orphan = {**CHARACTER, "@CharacterDesignId": "999"}
        (crew,) = parse_crew(characters_envelope(orphan), CHARACTER_DESIGNS)
        self.assertEqual((crew.training_capacity, crew.rarity), (0, ""))
        (crew,) = parse_crew(characters_envelope(orphan), None)
        self.assertEqual((crew.training_capacity, crew.rarity), (0, ""))
        second = {**CHARACTER, "@CharacterDesignId": "502"}
        (crew,) = parse_crew(characters_envelope(second), CHARACTER_DESIGNS)
        self.assertEqual((crew.training_capacity, crew.rarity), (80, "Epic"))

    def test_crew_single_dict_character_designs(self) -> None:
        # Regression: a single CharacterDesign dict was iterated key by key and raised TypeError.
        single = {"CharacterDesigns": {"CharacterDesign": CHARACTER_DESIGN}}
        (crew,) = parse_crew(characters_envelope(CHARACTER), single)
        self.assertEqual((crew.training_capacity, crew.rarity), (150, "Legendary"))

    def test_crew_room_resolution_no_leakage(self) -> None:
        # Regression: a crew with no room inherited the previous crew's room name.
        rooms = {
            "Rooms": {"Room": [{"@RoomId": "11", "@RoomDesignId": "201"}, {"@RoomId": "12", "@RoomDesignId": "999"}]}
        }
        room_designs = {"RoomDesigns": {"RoomDesign": {"@RoomDesignId": "201", "@RoomName": "Galaxy Gym"}}}
        no_room = {key: value for key, value in CHARACTER.items() if key != "@RoomId"}
        records = [
            CHARACTER,
            {**no_room, "@CharacterId": "1002"},
            {**CHARACTER, "@CharacterId": "1003", "@RoomId": "0"},
            {**CHARACTER, "@CharacterId": "1004", "@RoomId": "77"},
            {**CHARACTER, "@CharacterId": "1005", "@RoomId": "12"},
        ]
        crew = parse_crew(characters_envelope(records), CHARACTER_DESIGNS, rooms, room_designs)
        self.assertEqual(
            [(c.room_id, c.room_name) for c in crew],
            [
                (11, "Galaxy Gym"),
                (None, None),
                (None, None),
                (77, None),
                (12, None),
            ],
        )

    def test_crew_training_fields(self) -> None:
        cases = (
            ({"@TrainingDesignId": "0", "@TrainingEndDate": ""}, (None, None)),
            ({"@TrainingDesignId": "", "@TrainingEndDate": "garbage"}, (None, None)),
            ({}, (None, None)),
            (
                {"@TrainingDesignId": "31", "@TrainingEndDate": "2026-01-01T10:00:00.1234567"},
                (31, datetime(2026, 1, 1, 10, 0, 0, 123456, tzinfo=UTC)),
            ),
        )
        for fields, expected in cases:
            with self.subTest(fields=fields):
                record = {key: value for key, value in CHARACTER.items() if not key.startswith("@Training")}
                (crew,) = parse_crew(characters_envelope({**record, **fields}), CHARACTER_DESIGNS)
                self.assertEqual((crew.training_design_id, crew.training_end), expected)
                if crew.training_end is not None:
                    self.assertIs(crew.training_end.tzinfo, UTC)

    def test_crew_skips_bad_character_ids(self) -> None:
        missing = {key: value for key, value in CHARACTER.items() if key != "@CharacterId"}
        records = [{**CHARACTER, "@CharacterId": "c1"}, missing, {**CHARACTER, "@CharacterId": "-2"}, CHARACTER]
        self.assertEqual([c.character_id for c in parse_crew(characters_envelope(records), CHARACTER_DESIGNS)], [1001])


class TestRoomsResearch(unittest.TestCase):
    def test_parse_rooms(self) -> None:
        rooms = {
            "RoomService": {
                "ListRoomsViaAccessToken": {
                    "Rooms": {
                        "Room": [
                            {"@RoomId": "11", "@RoomDesignId": "201"},
                            {"@RoomId": "12", "@RoomDesignId": "202"},
                            {"@RoomId": "13", "@RoomDesignId": "999"},
                            {"@RoomId": "x", "@RoomDesignId": "201"},
                        ]
                    }
                }
            }
        }
        designs = {
            "RoomDesigns": {
                "RoomDesign": [
                    {"@RoomDesignId": "201", "@RoomName": "Academy"},
                    {"@RoomDesignId": "202", "@RoomName": " GYM "},
                ]
            }
        }
        self.assertEqual(dict(parse_rooms(rooms, designs)), {11: "Academy", 12: "GYM"})
        self.assertEqual(dict(parse_rooms(None, designs)), {})
        self.assertEqual(dict(parse_rooms(rooms, None)), {})

    def test_research_levels(self) -> None:
        designs = {
            "ResearchDesigns": {
                "ResearchDesign": [
                    {"@ResearchDesignId": "10", "@ResearchName": "Fitness", "@RootResearchDesignId": "10"},
                    {"@ResearchDesignId": "11", "@ResearchName": "Fitness Lv2", "@RootResearchDesignId": "10"},
                    {"@ResearchDesignId": "12", "@ResearchName": "Fitness Lv3", "@RootResearchDesignId": "10"},
                    {"@ResearchDesignId": "20", "@ResearchName": "Education Level 1", "@RootResearchDesignId": "0"},
                    {
                        "@ResearchDesignId": "21",
                        "@ResearchName": "Education II",
                        "@RootResearchDesignId": "20",
                        "@Level": "2",
                    },
                    {"@ResearchDesignId": "30", "@ResearchName": "Advanced Training"},
                ]
            }
        }

        def levels(*records: dict) -> dict:
            return dict(parse_research_levels({"Researches": {"Research": list(records)}}, designs))

        def done(design_id: str, **extra: str) -> dict:
            return {"@ResearchDesignId": design_id, "@ResearchState": "Completed", **extra}

        self.assertEqual(levels(done("11")), {"Fitness": 2})
        self.assertEqual(levels(done("11"), done("12"), done("10")), {"Fitness": 3})  # max wins
        self.assertEqual(levels({"@ResearchDesignId": "12", "@ResearchState": "Researching"}), {})
        self.assertEqual(levels(done("12", **{"@ResearchLevel": "5"})), {"Fitness": 5})  # record level first
        self.assertEqual(levels(done("21")), {"Education": 2})  # design @Level, root grouping
        self.assertEqual(levels(done("20")), {"Education": 1})  # name suffix
        self.assertEqual(levels(done("30")), {"Advanced Training": 1})  # default 1
        self.assertEqual(levels(done("999"), done("x")), {})  # unknown ids ignored
        self.assertEqual(dict(parse_research_levels(None, designs)), {})
        self.assertEqual(dict(parse_research_levels({"Research": done("11")}, None)), {})


class TestItems(unittest.TestCase):
    def test_parse_items(self) -> None:
        items = {
            "ItemService": {
                "ListItemsOfAShip": {
                    "Items": {
                        "Item": [
                            {"@ItemId": "1", "@ItemDesignId": "9001", "@Quantity": "2"},
                            {"@ItemId": "2", "@ItemDesignId": "9001", "@Quantity": "3"},
                            {"@ItemId": "3", "@ItemDesignId": "9002"},
                            {"@ItemId": "4", "@ItemDesignId": "9003", "@Quantity": "lots"},
                            {"@ItemId": "5", "@ItemDesignId": "0", "@Quantity": "4"},
                            {"@ItemId": "6", "@ItemDesignId": "-1"},
                            {"@ItemId": "7", "@Quantity": "4"},
                            {"@ItemId": "8", "@ItemDesignId": "9004", "@Quantity": "-5"},
                        ]
                    }
                }
            }
        }
        self.assertEqual(dict(parse_items(items)), {9001: 5, 9002: 1, 9003: 0, 9004: 0})
        self.assertEqual(dict(parse_items(None)), {})
        self.assertEqual(dict(parse_items({"Item": {"@ItemDesignId": "7"}})), {7: 1})

    def test_consumable_designs(self) -> None:
        records = [
            {"@ItemDesignId": "300", "@ItemName": "WPN Common Consumable", "@ItemSubType": "Consumable"},
            {"@ItemDesignId": "299", "@ItemName": "WPN Common Consumable Pack", "@ItemSubType": "Consumable"},
            {"@ItemDesignId": "301", "@ItemName": "Shipment Hero Consumable", "@ItemSubType": "Consumable"},
            {"@ItemDesignId": "302", "@ItemName": "HP Hero Consumable", "@ItemSubType": "None"},
            {"@ItemDesignId": "303", "@ItemName": "Repair Serum", "@ItemSubType": "Consumable", "@Rarity": "Common"},
            {"@ItemDesignId": "304", "@ItemName": "RPR Hero Consumable"},
            {"@ItemDesignId": "305", "@ItemName": "Weapon Crate", "@ItemSubType": "Equipment"},
            {"@ItemDesignId": "306", "@ItemName": "STA Elite Booster", "@ItemSubType": "Consumable", "@Rarity": "Hero"},
            {"@ItemDesignId": "307", "@ItemName": "ENG Consumable", "@Rarity": "Epic"},
            {"@ItemDesignId": "308", "@ItemName": "ATK Consumable"},
            {"@ItemDesignId": "x", "@ItemName": "SCI Common Consumable", "@ItemSubType": "Consumable"},
        ]
        found = dict(parse_consumable_designs({"ItemDesigns": {"ItemDesign": records}}))
        self.assertEqual(
            found,
            {
                ("WPN", "Common"): 299,  # lowest id wins
                ("HP", "Hero"): 302,  # qualifies by name; "Shipment" never yields HP (whole tokens)
                ("RPR", "Common"): 303,  # REPAIR token, tier from @Rarity
                ("RPR", "Hero"): 304,
                ("STA", "Elite"): 306,  # a tier word in the name beats @Rarity
                ("ENG", "Epic"): 307,
            },
        )
        self.assertEqual(dict(parse_consumable_designs(None)), {})


    def test_consumable_designs_full_stat_words(self) -> None:
        # Regression: only REPAIR was recognised as a full stat word; WEAPON, PILOT, ... were silently ignored.
        words = ("Weapon", "Science", "Engine", "Pilot", "Hp", "Attack", "Ability", "Stamina", "Repair")
        codes = ("WPN", "SCI", "ENG", "PLT", "HP", "ATK", "ABL", "STA", "RPR")
        records = [
            {"@ItemDesignId": str(400 + index), "@ItemName": f"{word} Common Consumable", "@ItemSubType": "Consumable"}
            for index, word in enumerate(words)
        ]
        found = dict(parse_consumable_designs({"ItemDesigns": {"ItemDesign": records}}))
        self.assertEqual(found, {(code, "Common"): 400 + index for index, code in enumerate(codes)})


class TestFixtures(unittest.TestCase):
    def test_r5_fixture_shapes(self) -> None:
        cases = {
            "training_designs_list.xml": ("ok", 8),
            "training_designs_single.xml": ("ok", 1),
            "training_designs_empty.xml": ("empty", 0),
            "training_designs_error.xml": ("error", 0),
            "training_designs_unexpected.xml": ("error", 0),
        }
        for name, (status, count) in cases.items():
            with self.subTest(fixture=name):
                raw = load(name)
                self.assertEqual(classify_payload(raw, "TrainingDesign"), status)
                self.assertEqual(len(parse_training_designs(raw)), count)
        (single,) = parse_training_designs(load("training_designs_single.xml"))
        self.assertEqual(
            (single.id, single.name, single.stat, single.required_room_type),
            (104, "Bench Press", "AttackImprovement", "gym"),
        )
        (crew,) = parse_crew(load("character_single.xml"), load("character_designs.xml"))
        self.assertEqual((crew.character_id, crew.name, crew.training_capacity), (2001, "Delish", 100))

    def test_build_context_fixture_golden(self) -> None:
        ctx = fixture_context()
        self.assertEqual(dict(ctx.research_levels), {"Fitness": 2, "Education": 3})
        self.assertEqual(dict(ctx.items), {9101: 2, 9102: 1, 9104: 7})
        self.assertEqual(ctx.rooms[13], "Galaxy Gym Annex")
        self.assertEqual(ctx.now, FIXTURE_NOW)
        expected = (
            StartTraining(1001, 101, "primary T1 Read Expert Weapon Theory at 20% in academy"),
            FinishTraining(1002, "training ended at 2026-03-01 11:30:00+00:00"),
            StartTraining(1002, 103, "primary T2 Crew vs Wild at 55% in gym"),
            Skip(1003, "not_in_training_room", "room 'Galaxy Gym Annex' is not a training room"),
            Skip(1004, "training_in_progress", "training ends at 2026-03-01 13:15:30.123456+00:00"),
            Skip(1005, "not_in_training_room", "room None is not a training room"),
            StartTraining(1006, 106, "primary T1 Big Book of Science at 50% in academy"),
            Skip(1007, "unknown_role", "'Rando Crew' has no training role"),
            Skip(1008, "design_missing", "no training capacity"),
            Skip(1009, "research_missing", "Space Marine requires research Advanced Training"),
        )
        self.assertEqual(plan_training(ctx), expected)

    def test_build_context_fixture_golden_with_consumables(self) -> None:
        consumables = parse_consumable_designs(load("item_designs.xml"))
        self.assertEqual(dict(consumables), {("WPN", "Common"): 9101, ("HP", "Hero"): 9102})
        decisions = plan_training(fixture_context(), with_consumables(DEFAULT_STRATEGY, consumables))
        by_crew = {d.character_id: d for d in decisions if not isinstance(d, FinishTraining)}
        self.assertEqual(by_crew[1004], UseConsumable(1004, 9101, "Common WPN consumable at 10% with fatigue 0"))
        self.assertEqual(sum(isinstance(d, UseConsumable) for d in decisions), 1)


FUZZ_KEYS = (
    "Character",
    "Characters",
    "CharacterDesign",
    "TrainingDesign",
    "TrainingDesigns",
    "Room",
    "RoomDesign",
    "Research",
    "ResearchDesign",
    "Item",
    "ItemDesign",
    "@errorMessage",
    "@CharacterId",
    "@CharacterDesignId",
    "@CharacterName",
    "@RoomId",
    "@RoomDesignId",
    "@RoomName",
    "@TrainingDesignId",
    "@TrainingName",
    "@TrainingEndDate",
    "@TrainingCapacity",
    "@Rarity",
    "@Rank",
    "@Duration",
    "@RequiredResearchDesignId",
    "@RequiredRoomType",
    "@HpChance",
    "@HpImprovement",
    "@Fatigue",
    "@Level",
    "@ResearchDesignId",
    "@RootResearchDesignId",
    "@ResearchName",
    "@ResearchState",
    "@ResearchLevel",
    "@ItemDesignId",
    "@ItemName",
    "@ItemSubType",
    "@Quantity",
)
FUZZ_SCALARS = (
    None,
    "",
    "0",
    "1",
    "7",
    "-3",
    "99999999999999999999",
    "9" * 5000,
    "abc",
    "nan",
    "inf",
    "1e400",
    "Completed",
    "Gym",
    "WPN Hero Consumable",
    "Repair Common Consumable",
    "Fitness Lv2",
    "2026-01-01T00:00:00",
    "2026-01-01T00:00:00.1234567+02:00",
    "0001-01-01T00:00:00+05:00",
    "9999-12-31T23:59:59-05:00",
    0,
    -1,
    10**30,
    1.5,
    float("nan"),
    float("inf"),
    True,
)


FUZZ_ITEM_KEYS = tuple(key for key in FUZZ_KEYS if not key.startswith("@"))
FUZZ_ATTR_KEYS = tuple(key for key in FUZZ_KEYS if key.startswith("@"))


def garbage(rng: random.Random, depth: int = 0) -> object:
    """Random xmltodict-like structure: scalars, lists, record-shaped dicts and envelopes, with junk mixed in."""
    roll = rng.random()
    if depth >= 5 or roll < 0.2:
        return rng.choice(FUZZ_SCALARS)
    if roll < 0.35:
        return [garbage(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    if roll < 0.65:  # record: mostly scalar attributes, occasionally nested junk
        return {
            key: rng.choice(FUZZ_SCALARS) if rng.random() < 0.9 else garbage(rng, depth + 1)
            for key in rng.sample(FUZZ_ATTR_KEYS, rng.randint(3, 15))
        }
    envelope: dict = {}
    for _ in range(rng.randint(1, 4)):
        key = rng.choice(FUZZ_ITEM_KEYS) if rng.random() < 0.9 else rng.choice((1, None, "junk"))
        envelope[key] = garbage(rng, depth + 1)
    return envelope


FUZZ_TARGETS = {
    "Character": tuple(CHARACTER) + ("@TrainingEndDate",),
    "CharacterDesign": tuple(CHARACTER_DESIGN),
    "TrainingDesign": tuple(BENCH) + ("@WeaponChance", "@RepairChance"),
    "Room": ("@RoomId", "@RoomDesignId"),
    "RoomDesign": ("@RoomDesignId", "@RoomName"),
    "Research": ("@ResearchDesignId", "@ResearchState", "@ResearchLevel"),
    "ResearchDesign": ("@ResearchDesignId", "@RootResearchDesignId", "@ResearchName", "@Level"),
    "Item": ("@ItemDesignId", "@Quantity"),
    "ItemDesign": ("@ItemDesignId", "@ItemName", "@ItemSubType", "@Rarity"),
}


FUZZ_BIASED = {
    "@ResearchState": ("Completed",),
    "@ItemName": ("WPN Hero Consumable", "Repair Common Consumable", "STA Consumable"),
    "@ItemSubType": ("Consumable",),
    "@RoomName": ("GYM", "Academy", ""),
}


def fuzz_value(rng: random.Random, key: str) -> object:
    """Junk, biased so independently generated payloads still join (shared small ids) and hit real branches."""
    if key.endswith("Id") and rng.random() < 0.7:
        return rng.choice(("1", "7", " 7 ", "7.0"))
    if key in FUZZ_BIASED and rng.random() < 0.6:
        return rng.choice(FUZZ_BIASED[key])
    return rng.choice(FUZZ_SCALARS) if rng.random() < 0.85 else garbage(rng, 3)


def targeted(rng: random.Random, item_key: str) -> dict:
    """An envelope of mostly record-shaped dicts for ``item_key`` whose attribute values are random junk."""
    records: list = []
    for _ in range(rng.randint(1, 5)):
        if rng.random() < 0.15:
            records.append(garbage(rng, 3))
            continue
        attrs = FUZZ_TARGETS[item_key]
        records.append({key: fuzz_value(rng, key) for key in attrs if rng.random() < 0.8})
    payload = records[0] if len(records) == 1 and rng.random() < 0.5 else records
    return {"Service": {"List": {item_key + "s": {item_key: payload}}}}


class TestFuzz(unittest.TestCase):
    def test_parse_never_raises_fuzz(self) -> None:
        hits = dict.fromkeys(("designs", "crew", "rooms", "levels", "items", "consumables"), 0)
        for seed in range(300):
            rng = random.Random(seed)
            if seed % 2:
                a, b, c, d = (garbage(rng) for _ in range(4))
                chars, char_designs, trainings, rooms_raw, room_designs = a, b, c, d, a
                researches, research_designs, items_raw, item_designs = b, c, d, a
            else:
                a = targeted(rng, "Character")
                chars, char_designs = a, targeted(rng, "CharacterDesign")
                trainings, rooms_raw, room_designs = (
                    targeted(rng, "TrainingDesign"),
                    targeted(rng, "Room"),
                    targeted(rng, "RoomDesign"),
                )
                researches, research_designs = targeted(rng, "Research"), targeted(rng, "ResearchDesign")
                items_raw, item_designs = targeted(rng, "Item"), targeted(rng, "ItemDesign")
            with self.subTest(seed=seed):
                self.assertIsInstance(to_int(a), int)
                self.assertTrue(parse_datetime(a) is None or parse_datetime(a).tzinfo is UTC)
                self.assertTrue(all(isinstance(x, dict) for x in extract_collection(a, "Character")))
                self.assertIn(classify_payload(trainings, "TrainingDesign"), {"ok", "empty", "error"})
                designs = parse_training_designs(trainings, research_designs)
                self.assertTrue(isinstance(designs, tuple) and all(isinstance(x, TrainingDesign) for x in designs))
                rooms = parse_rooms(rooms_raw, room_designs)
                self.assertTrue(all(isinstance(k, int) and isinstance(v, str) for k, v in rooms.items()))
                crew = parse_crew(chars, char_designs, rooms_raw, room_designs)
                self.assertTrue(isinstance(crew, tuple) and all(isinstance(x, CrewState) for x in crew))
                levels = parse_research_levels(researches, research_designs)
                self.assertTrue(all(isinstance(k, str) and isinstance(v, int) for k, v in levels.items()))
                items = parse_items(items_raw)
                self.assertTrue(all(isinstance(k, int) and v >= 0 for k, v in items.items()))
                consumables = parse_consumable_designs(item_designs)
                self.assertIsInstance(consumables, Mapping)
                with_consumables(DEFAULT_STRATEGY, consumables)  # parse output is always a valid strategy input
                ctx = build_context(
                    now=FIXTURE_NOW,
                    characters_raw=chars,
                    character_designs_raw=char_designs,
                    training_designs_raw=trainings,
                    rooms_raw=rooms_raw,
                    room_designs_raw=room_designs,
                    researches_raw=researches,
                    research_designs_raw=research_designs,
                    items_raw=items_raw,
                )
                self.assertIsInstance(plan_training(ctx), tuple)
                for name, result in (
                    ("designs", designs),
                    ("crew", crew),
                    ("rooms", rooms),
                    ("levels", levels),
                    ("items", items),
                    ("consumables", consumables),
                ):
                    hits[name] += bool(result)
        for name, count in hits.items():  # the fuzz must actually reach record parsing, not just reject envelopes
            with self.subTest(coverage=name):
                self.assertGreaterEqual(count, 10, hits)


if __name__ == "__main__":
    unittest.main()
