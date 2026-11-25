"""Tests for sdk.training.parse — tolerant XML-dict to training model conversion (synthetic fixtures only)."""

from __future__ import annotations

import inspect
import os
import random
import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import xmltodict

from sdk.training.model import CrewState, FinishTraining, Skip, StartTraining, TrainingDesign, UseConsumable
import sdk.training.parse as parse_module
from sdk.training.parse import (
    build_context,
    classify_payload,
    classify_payloads,
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


def summarize(decisions) -> list[tuple]:
    """(kind, character id, reason code | design id | item id) per decision; wording-independent."""
    rows: list[tuple] = []
    for decision in decisions:
        if isinstance(decision, FinishTraining):
            rows.append(("finish", decision.character_id, None))
        elif isinstance(decision, StartTraining):
            rows.append(("start", decision.character_id, decision.training_design_id))
        elif isinstance(decision, UseConsumable):
            rows.append(("use", decision.character_id, decision.item_design_id))
        elif isinstance(decision, Skip):
            rows.append(("skip", decision.character_id, decision.reason_code))
        else:
            rows.append(("other", decision))
    return rows


def fixture_context(**overrides: object):
    """build_context over the synthetic fixtures; a keyword in ``overrides`` replaces that payload."""
    payloads: dict[str, object] = {
        "characters_raw": load("characters.xml"),
        "character_designs_raw": load("character_designs.xml"),
        "training_designs_raw": load("training_designs_list.xml"),
        "rooms_raw": load("rooms.xml"),
        "room_designs_raw": load("room_designs.xml"),
        "researches_raw": load("researches.xml"),
        "research_designs_raw": load("research_designs.xml"),
        "items_raw": load("items.xml"),
    }
    unknown = sorted(set(overrides) - set(payloads))
    if unknown:
        raise TypeError(f"fixture_context: unknown payloads {unknown}")
    payloads.update(overrides)
    return build_context(
        now=FIXTURE_NOW,
        characters_raw=payloads["characters_raw"],
        character_designs_raw=payloads["character_designs_raw"],
        training_designs_raw=payloads["training_designs_raw"],
        rooms_raw=payloads["rooms_raw"],
        room_designs_raw=payloads["room_designs_raw"],
        researches_raw=payloads["researches_raw"],
        research_designs_raw=payloads["research_designs_raw"],
        items_raw=payloads["items_raw"],
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

    def test_to_int_bools_take_the_default(self) -> None:
        # bool is an int subclass, but True/False is not a count: it must not read as 1/0.
        self.assertEqual(to_int(True), 0)
        self.assertEqual(to_int(False), 0)
        self.assertEqual(to_int(True, default=-1), -1)
        self.assertEqual(to_int(False, default=7), 7)

    def test_to_int_truncates_non_whole_numbers_toward_zero(self) -> None:
        # Truncation, not rounding: a level or quantity of "7.6" never reads as 8.
        cases = (("7.6", 7), ("7.4", 7), (" 2.999 ", 2), ("-3.5", -3), ("-0.9", 0), (7.9, 7), (-7.9, -7))
        cases += (("+7", 7), ("+7.5", 7), ("0012", 12), ("9" * 30 + ".9", int("9" * 30)))
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(to_int(value), expected)

    def test_to_int_accepts_only_ascii_decimal_text(self) -> None:
        # Regression (review #17): int()/float() also accept underscores, Unicode digits, exponents and hex-ish
        # spellings, so "1_000" read as 1000 and "٣" as 3. Only [sign] ASCII digits [. ASCII digits] parse.
        rejected = ("1_000", "1_0.5", "٣", "１２", "7٣", "1e3", "2.5e1", "1E3", "0x10", "7.", ".5", "+-7", "- 7")
        rejected += ("7 8", "1.2.3", "∞", "")
        for value in rejected:
            with self.subTest(value=value):
                self.assertEqual(to_int(value), 0)
                self.assertEqual(to_int(value, default=-9), -9)

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

    def test_parse_datetime_rejects_surrounding_junk(self) -> None:
        # Fail closed: a timestamp with anything after it is malformed, not a prefix to salvage.
        junk = (
            "2026-01-02T03:04:05xyz",
            "2026-01-02T03:04:05Zjunk",
            "2026-01-02T03:04:05 Z",
            "2026-01-02T03:04:05+05:30:00",
            "2026-01-02T03:04:05.123.456",
            "2026-01-02T03:04:05.",
            "x2026-01-02T03:04:05",
        )
        for value in junk:
            with self.subTest(value=value):
                self.assertIsNone(parse_datetime(value))

    def test_parse_datetime_applies_offset_minutes(self) -> None:
        base = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        cases = {
            "2026-01-02T08:34:05+05:30": base,
            "2026-01-02T08:49:05+05:45": base,
            "2026-01-01T23:34:05-03:30": base,
            "2026-01-02T03:04:05-00:00": base,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_datetime(value), expected)

    def test_parse_datetime_rejects_malformed_offsets_and_non_ascii_digits(self) -> None:
        # Regression: minutes 60-99 were folded into the hour ("+05:99" read as +06:39), and Unicode digits
        # (which re's \d and int() both accept) parsed as a valid timestamp.
        malformed = (
            "2026-01-02T03:04:05+05:60",
            "2026-01-02T03:04:05+05:99",
            "2026-01-02T03:04:05+24:00",
            "2026-01-02T03:04:05+0530",
            "2026-01-02T03:04:05+5:30",
            "２０２６-01-02T03:04:05",
            "2026-01-02T03:04:05.١",
        )
        for value in malformed:
            with self.subTest(value=value):
                self.assertIsNone(parse_datetime(value))
        self.assertEqual(parse_datetime("2026-01-02T03:04:05+23:59"), datetime(2026, 1, 1, 3, 5, 5, tzinfo=UTC))


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

    def test_classify_payload_error_message_inside_a_list(self) -> None:
        # Regression (review #11): errorMessage detection only walked dicts, so an error beside records in a list
        # (several sibling elements) read as "ok".
        records = {"Characters": {"Character": [{"@CharacterId": "1"}]}}
        for key in ("@errorMessage", "errorMessage"):
            cases = (
                {"Svc": {"Op": [{key: "x"}]}},
                {**records, "Errors": [{key: "x"}]},
                {**records, "Errors": [[{"Deeper": {key: "x"}}]]},
            )
            for raw in cases:
                with self.subTest(key=key, raw=raw):
                    self.assertEqual(classify_payload(raw, "Character"), "error")
        self.assertEqual(classify_payload({**records, "Notes": [{"@message": "x"}, "text"]}, "Character"), "ok")

    def test_classify_payload_item_without_records_is_an_error(self) -> None:
        # Regression (review #12): an item element holding text instead of records read as "empty" (no data), which
        # an executor treats as nothing to do; it is an unexpected schema.
        junk_values = ("text", ["a", "b"], [None, "a"], 7, [7])
        for value in junk_values:
            for raw in ({"TrainingDesigns": {"TrainingDesign": value}}, {"Svc": {"TrainingDesign": value}}):
                with self.subTest(raw=raw):
                    self.assertEqual(classify_payload(raw, "TrainingDesign"), "error")
        for value in (None, [], [None]):
            with self.subTest(value=value):
                self.assertEqual(
                    classify_payload({"TrainingDesigns": {"TrainingDesign": value}}, "TrainingDesign"), "empty"
                )
        mixed = {"TrainingDesigns": {"TrainingDesign": ["junk", {"@TrainingDesignId": "1"}]}}
        self.assertEqual(classify_payload(mixed, "TrainingDesign"), "ok")

    def test_classify_payload_nested_error_message_beats_container(self) -> None:
        # An errorMessage nested beside the container (the live envelope shape) still marks the response broken,
        # whether the container is empty or holds records.
        for key in ("@errorMessage", "errorMessage"):
            empty = {"Svc": {"Op": {key: "x", "Characters": None}}}
            filled = {"Svc": {"Op": {key: "x", "Characters": {"Character": {"@CharacterId": "1"}}}}}
            for raw in (empty, filled):
                with self.subTest(key=key, raw=raw):
                    self.assertEqual(classify_payload(raw, "Character"), "error")
        self.assertEqual(classify_payload({"Svc": {"Op": {"Characters": None}}}, "Character"), "empty")
        ok = {"Svc": {"Op": {"Characters": {"Character": {"@CharacterId": "1"}}}}}
        self.assertEqual(classify_payload(ok, "Character"), "ok")

    def test_classify_payload_empty_envelope_for_every_item_key(self) -> None:
        # Regression: the default container was item_key + "s", so an empty <Researches/> read as "Researchs" and
        # was reported as a broken response.
        containers = {
            "Character": "Characters",
            "CharacterDesign": "CharacterDesigns",
            "TrainingDesign": "TrainingDesigns",
            "Room": "Rooms",
            "RoomDesign": "RoomDesigns",
            "Research": "Researches",
            "ResearchDesign": "ResearchDesigns",
            "Item": "Items",
            "ItemDesign": "ItemDesigns",
        }
        for item_key, container in containers.items():
            with self.subTest(item_key=item_key):
                self.assertEqual(classify_payload({"Service": {"List": {container: None}}}, item_key), "empty")
                self.assertEqual(classify_payload({"Service": {"List": {container: {}}}}, item_key), "empty")
                record = {"Service": {"List": {container: {item_key: {"@Id": "1"}}}}}
                self.assertEqual(classify_payload(record, item_key), "ok")
                self.assertEqual(classify_payload({"Service": {"List": {"Other": None}}}, item_key), "error")
        ship = load("ship_empty.xml")
        self.assertEqual(classify_payload(ship, "Research"), "empty")
        self.assertEqual(classify_payload(ship, "Room"), "empty")

    def test_classify_payloads_binds_item_keys_to_build_context_names(self) -> None:
        context_names = set(inspect.signature(build_context).parameters) - {"now"}
        everything = classify_payloads(**{name: None for name in context_names | {"item_designs_raw"}})
        self.assertEqual(set(everything), context_names | {"item_designs_raw"})
        self.assertEqual(set(everything.values()), {"error"})
        ship = load("ship_empty.xml")
        result = classify_payloads(
            rooms_raw=ship,
            researches_raw=ship,
            training_designs_raw=load("training_designs_list.xml"),
            characters_raw=load("training_designs_error.xml"),
        )
        expected = {
            "rooms_raw": "empty",
            "researches_raw": "empty",
            "training_designs_raw": "ok",
            "characters_raw": "error",
        }
        self.assertEqual(dict(result), expected)
        self.assertEqual(dict(classify_payloads()), {})
        with self.assertRaises(TypeError):
            classify_payloads(research_raw=ship)  # mistyped keyword


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

    def test_design_duration_clamped_to_zero_and_ten_years(self) -> None:
        # A negative duration is malformed and clamps to 0 (never a negative timedelta); a huge one clamps to the
        # ten-year ceiling so timedelta arithmetic cannot overflow.
        durations = {"-5": timedelta(0), "-1.5": timedelta(0), "60": timedelta(seconds=60)}
        durations[str(10**12)] = timedelta(days=3650)
        for raw, expected in durations.items():
            with self.subTest(duration=raw):
                (design,) = parse_training_designs({"TrainingDesign": {"@TrainingDesignId": "1", "@Duration": raw}})
                self.assertEqual(design.duration, expected)

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
        # Regression (review #2): the level was dropped ("Advanced Training Lv2" read as the family name, so Lv1
        # satisfied it); the exact research design name is kept.
        self.assertEqual(required("41"), "Advanced Training Lv2")
        nameless = {"ResearchDesign": {"@ResearchDesignId": "42", "@RootResearchDesignId": "40"}}
        self.assertEqual(required("42", nameless), "research_design:42")
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
            # Regression (review #5): an empty or missing end date means idle (the design id is dropped, matching
            # the server snapshot); a present but unparseable one keeps the id so the planner sees "unknown".
            ({"@TrainingDesignId": "31", "@TrainingEndDate": ""}, (None, None)),
            ({"@TrainingDesignId": "31", "@TrainingEndDate": "   "}, (None, None)),
            ({"@TrainingDesignId": "31"}, (None, None)),
            ({"@TrainingDesignId": "31", "@TrainingEndDate": "garbage"}, (31, None)),
            ({"@TrainingDesignId": "31", "@TrainingEndDate": "2026-13-01T00:00:00"}, (31, None)),
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

    def test_crew_duplicate_character_id_first_record_wins(self) -> None:
        # Regression (review #8): a repeated @CharacterId produced two CrewStates for one character (two plans,
        # possibly two trainings); the first record wins, as for every other id-keyed join.
        records = [
            CHARACTER,
            {**CHARACTER, "@CharacterName": "Impostor", "@Level": "1"},
            {**CHARACTER, "@CharacterId": "1002"},
        ]
        crew = parse_crew(characters_envelope(records), CHARACTER_DESIGNS)
        self.assertEqual(
            [(c.character_id, c.name, c.level) for c in crew], [(1001, "Delish", 20), (1002, "Delish", 20)]
        )

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

    def test_duplicate_ids_first_record_wins(self) -> None:
        # Every id-keyed join (room, room design, character design, research design, item design) keeps the first
        # record for a repeated id, so a later duplicate can never overwrite data already in the payload.
        room_designs = {
            "RoomDesign": [
                {"@RoomDesignId": "5", "@RoomName": "Gym"},
                {"@RoomDesignId": "5", "@RoomName": "Academy"},
                {"@RoomDesignId": "6", "@RoomName": "Galaxy Gym"},
            ]
        }
        rooms = {"Room": [{"@RoomId": "1", "@RoomDesignId": "5"}, {"@RoomId": "1", "@RoomDesignId": "6"}]}
        self.assertEqual(dict(parse_rooms(rooms, room_designs)), {1: "Gym"})

        character_designs = {
            "CharacterDesign": [
                {"@CharacterDesignId": "7", "@TrainingCapacity": "100", "@Rarity": "Epic"},
                {"@CharacterDesignId": "7", "@TrainingCapacity": "200", "@Rarity": "Legendary"},
            ]
        }
        (crew,) = parse_crew({"Character": {"@CharacterId": "3", "@CharacterDesignId": "7"}}, character_designs)
        self.assertEqual((crew.training_capacity, crew.rarity), (100, "Epic"))

        research_designs = {
            "ResearchDesign": [
                {"@ResearchDesignId": "10", "@ResearchName": "Fitness"},
                {"@ResearchDesignId": "10", "@ResearchName": "Education"},
            ]
        }
        researches = {"Research": {"@ResearchDesignId": "10", "@ResearchState": "Completed"}}
        self.assertEqual(dict(parse_research_levels(researches, research_designs)), {"Fitness": 1})

        records = [CHARACTER, {**CHARACTER, "@CharacterDesignId": "502"}]
        (crew,) = parse_crew(characters_envelope(records), CHARACTER_DESIGNS)
        self.assertEqual(crew.training_capacity, 150)

        item_designs = {
            "ItemDesign": [
                {"@ItemDesignId": "9", "@ItemName": "Common Weapon Consumable"},
                {"@ItemDesignId": "9", "@ItemName": "Hero HP Consumable"},
            ]
        }
        self.assertEqual(dict(parse_consumable_designs(item_designs)), {("WPN", "Common"): 9})

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
                    {
                        "@ResearchDesignId": "13",
                        "@ResearchName": "Fitness Lv2",
                        "@RootResearchDesignId": "10",
                        "@Level": "3",
                    },
                ]
            }
        }

        def levels(*records: dict) -> dict:
            return dict(parse_research_levels({"Researches": {"Research": list(records)}}, designs))

        def done(design_id: str, **extra: str) -> dict:
            return {"@ResearchDesignId": design_id, "@ResearchState": "Completed", **extra}

        # Each completed design counts under its family (max level) and under its own exact name (review #2).
        self.assertEqual(levels(done("11")), {"Fitness": 2, "Fitness Lv2": 2})
        all_fitness = {"Fitness": 3, "Fitness Lv2": 2, "Fitness Lv3": 3}
        self.assertEqual(levels(done("11"), done("12"), done("10")), all_fitness)  # max wins
        self.assertEqual(levels({"@ResearchDesignId": "12", "@ResearchState": "Researching"}), {})
        record_level = {"Fitness": 5, "Fitness Lv3": 5}
        self.assertEqual(levels(done("12", **{"@ResearchLevel": "5"})), record_level)  # record level first
        self.assertEqual(levels(done("21")), {"Education": 2, "Education II": 2})  # design @Level, root grouping
        self.assertEqual(levels(done("20")), {"Education": 1, "Education Level 1": 1})  # name suffix
        # design @Level beats a conflicting name suffix; a shared exact name keeps its highest level
        self.assertEqual(levels(done("13")), {"Fitness": 3, "Fitness Lv2": 3})
        self.assertEqual(levels(done("11"), done("13")), {"Fitness": 3, "Fitness Lv2": 3})
        self.assertEqual(levels(done("30")), {"Advanced Training": 1})  # default 1
        self.assertEqual(levels(done("12", **{"@ResearchLevel": "-2"})), {"Fitness": 3, "Fitness Lv3": 3})
        self.assertEqual(levels(done("999"), done("x")), {})  # unknown ids ignored
        self.assertEqual(dict(parse_research_levels(None, designs)), {})
        self.assertEqual(dict(parse_research_levels({"Research": done("11")}, None)), {})

    def test_level_suffix_needs_a_word_boundary(self) -> None:
        # Regression (review #10): "Sublevel 2" split into family "Sub" at level 2 and "Wolv2" into "Wo" at 2.
        designs = {
            "ResearchDesign": [
                {"@ResearchDesignId": "1", "@ResearchName": "Sublevel 2"},
                {"@ResearchDesignId": "2", "@ResearchName": "Wolv2"},
                {"@ResearchDesignId": "3", "@ResearchName": "Fitness Lv.3"},
                {"@ResearchDesignId": "4", "@ResearchName": "Fitness lv 4"},
                {"@ResearchDesignId": "5", "@ResearchName": "Education LEVEL5"},
                {"@ResearchDesignId": "6", "@ResearchName": "Shield Lv٣"},
            ]
        }

        def level_of(design_id: str) -> dict:
            researches = {"Research": {"@ResearchDesignId": design_id, "@ResearchState": "Completed"}}
            return dict(parse_research_levels(researches, designs))

        self.assertEqual(level_of("1"), {"Sublevel 2": 1})
        self.assertEqual(level_of("2"), {"Wolv2": 1})
        self.assertEqual(level_of("3"), {"Fitness": 3, "Fitness Lv.3": 3})
        self.assertEqual(level_of("4"), {"Fitness": 4, "Fitness lv 4": 4})
        self.assertEqual(level_of("5"), {"Education": 5, "Education LEVEL5": 5})
        self.assertEqual(level_of("6"), {"Shield Lv٣": 1})  # non-ASCII digits are not a level


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
        self.assertEqual(
            dict(ctx.research_levels), {"Fitness": 2, "Fitness Lv2": 2, "Education": 3, "Education Lv3": 3}
        )
        self.assertEqual(dict(ctx.items), {9101: 2, 9102: 1, 9104: 7})
        self.assertEqual(ctx.rooms[13], "Galaxy Gym Annex")
        self.assertEqual(ctx.now, FIXTURE_NOW)
        # Summaries only: the planner's reason/detail wording is pinned in test_training_planner, not here.
        expected = [
            ("start", 1001, 101),
            ("finish", 1002, None),
            ("start", 1002, 103),
            ("skip", 1003, "not_in_training_room"),
            ("skip", 1004, "training_in_progress"),
            ("skip", 1005, "not_in_training_room"),
            ("start", 1006, 106),
            ("skip", 1007, "unknown_role"),
            ("skip", 1008, "design_missing"),
            ("skip", 1009, "research_missing"),
        ]
        self.assertEqual(summarize(plan_training(ctx)), expected)

    def test_build_context_fixture_golden_with_consumables(self) -> None:
        consumables = parse_consumable_designs(load("item_designs.xml"))
        self.assertEqual(dict(consumables), {("WPN", "Common"): 9101, ("HP", "Hero"): 9102})
        decisions = plan_training(fixture_context(), with_consumables(DEFAULT_STRATEGY, consumables))
        rows = summarize(decisions)
        self.assertIn(("use", 1004, 9101), rows)
        self.assertEqual([row for row in rows if row[0] == "use"], [("use", 1004, 9101)])

    def test_build_context_resolves_design_research_gates(self) -> None:
        # build_context must hand the research catalog to the design parser: the fixture's gated design (108,
        # RequiredResearchDesignId 40) names the research family, not the unresolvable "research_design:40".
        designs = {design.id: design for design in fixture_context().designs}
        self.assertEqual(designs[108].required_research, "Advanced Training")
        self.assertEqual({d.id for d in designs.values() if d.required_research is not None}, {108})
        # End to end: once research 40 completes, crew 1009 starts the gated design instead of research_missing.
        researches = load("researches.xml")
        records = researches["ResearchService"]["ListAllResearches"]["Researches"]["Research"]
        self.assertEqual(records[2]["@ResearchDesignId"], "40")
        records[2]["@ResearchState"] = "Completed"
        ctx = fixture_context(researches_raw=researches)
        self.assertEqual(ctx.research_levels.get("Advanced Training"), 1)
        rows = summarize(plan_training(ctx))
        self.assertIn(("start", 1009, 108), rows)
        self.assertNotIn(("skip", 1009, "research_missing"), rows)

    def test_design_research_gate_needs_the_exact_level(self) -> None:
        # Regression (review #2): a design requiring "Fitness Lv3" was satisfied by Fitness Lv1 because only the
        # family name was kept. Crew 1001 (weapons, 20%) trains design 101, gated here on research 12.
        research_designs = {
            "ResearchDesigns": {
                "ResearchDesign": [
                    {"@ResearchDesignId": "10", "@ResearchName": "Fitness", "@RootResearchDesignId": "10"},
                    {"@ResearchDesignId": "11", "@ResearchName": "Fitness Lv2", "@RootResearchDesignId": "10"},
                    {"@ResearchDesignId": "12", "@ResearchName": "Fitness Lv3", "@RootResearchDesignId": "10"},
                ]
            }
        }
        training = load("training_designs_list.xml")
        records = training["TrainingService"]["ListAllTrainingDesigns2"]["TrainingDesigns"]["TrainingDesign"]
        self.assertEqual(records[0]["@TrainingDesignId"], "101")
        records[0]["@RequiredResearchDesignId"] = "12"

        def plan_with(*completed: str) -> list[tuple]:
            researches = {"Research": [{"@ResearchDesignId": rid, "@ResearchState": "Completed"} for rid in completed]}
            ctx = fixture_context(
                training_designs_raw=training, research_designs_raw=research_designs, researches_raw=researches
            )
            self.assertEqual({d.id: d.required_research for d in ctx.designs}[101], "Fitness Lv3")
            return [row for row in summarize(plan_training(ctx)) if row[1] == 1001]

        self.assertEqual(plan_with("10"), [("skip", 1001, "research_missing")])
        self.assertEqual(plan_with("10", "11"), [("skip", 1001, "research_missing")])
        self.assertEqual(plan_with("10", "11", "12"), [("start", 1001, 101)])
        self.assertEqual(plan_with("12"), [("start", 1001, 101)])

    def test_build_context_builds_the_research_catalog_once(self) -> None:
        # Regression (review #20): build_context derived the research catalog twice (designs and levels).
        with mock.patch.object(parse_module, "_research_catalog", wraps=parse_module._research_catalog) as spy:
            ctx = fixture_context()
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(ctx.research_levels, fixture_context().research_levels)
        self.assertEqual(ctx.designs, fixture_context().designs)

    def test_unparseable_training_end_plans_as_training_in_progress(self) -> None:
        # Review #5 end to end: crew 1004's end date is garbled; it keeps its design id and is never re-planned.
        characters = load("characters.xml")
        records = characters["CharacterService"]["ListAllCharactersOfUser"]["Characters"]["Character"]
        self.assertEqual(records[3]["@CharacterId"], "1004")
        records[3]["@TrainingEndDate"] = "2026-03-01T13:15:30junk"
        ctx = fixture_context(characters_raw=characters)
        crew = next(member for member in ctx.crew if member.character_id == 1004)
        self.assertEqual((crew.training_design_id, crew.training_end), (101, None))
        decisions = plan_training(ctx, with_consumables(DEFAULT_STRATEGY, {("WPN", "Common"): 9101}))
        self.assertIn(Skip(1004, "training_in_progress", "training end unknown"), decisions)
        self.assertFalse(any(getattr(d, "character_id", None) == 1004 and not isinstance(d, Skip) for d in decisions))

    def test_build_context_parses_rooms_once(self) -> None:
        # Regression: build_context parsed the room payloads twice (once for crew names, once for ctx.rooms).
        with mock.patch.object(parse_module, "parse_rooms", wraps=parse_module.parse_rooms) as spy:
            ctx = fixture_context()
        self.assertEqual(spy.call_count, 1)
        for crew in ctx.crew:
            with self.subTest(character_id=crew.character_id):
                self.assertEqual(crew.room_name, ctx.rooms.get(crew.room_id) if crew.room_id is not None else None)


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
