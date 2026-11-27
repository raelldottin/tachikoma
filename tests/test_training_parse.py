"""Tests for sdk.training.parse — tolerant XML-dict to training model conversion (synthetic fixtures only)."""

from __future__ import annotations

import dataclasses
import inspect
import os
import random
import time
import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import xmltodict

from sdk.training.model import CrewState, FinishTraining, Skip, StartTraining, TrainingDesign, UseConsumable
from sdk.training.model import (
    CURRENCY_GAS,
    CURRENCY_MINERAL,
    CURRENCY_STARBUX,
    PRICE_CURRENCIES,
    PRICE_MALFORMED,
    PRICE_OK,
    PRICE_PREMIUM,
    PRICE_STATUSES,
    PRICE_UNAVAILABLE,
    SESSION_ENDED_UNCLAIMED,
    SESSION_FINISHED,
    SESSION_NONE,
    SESSION_PHASES,
    SESSION_RUNNING,
    SESSION_UNKNOWN,
    SPENDABLE_CURRENCIES,
    CourseObservation,
    CoursePrice,
    PrestigeRecipe,
    RoomDesignObservation,
    TrainingSession,
)
import sdk.training.parse as parse_module
from sdk.training.parse import (
    build_context,
    classify_payload,
    classify_payloads,
    extract_collection,
    parse_consumable_designs,
    parse_course_observations,
    parse_course_price,
    parse_crew,
    parse_datetime,
    parse_items,
    parse_prestige_recipes,
    parse_research_family_levels,
    parse_research_levels,
    parse_room_design_observations,
    parse_rooms,
    parse_training_designs,
    parse_training_session,
    research_course,
    to_int,
)
from sdk.training.planner import plan_training, research_ok
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
        # Live shape: each design's family is its root's name and its level its course number (or "LvN").
        designs = {
            "ResearchDesigns": {
                "ResearchDesign": [
                    {"@ResearchDesignId": "74", "@ResearchName": "Fitness 101", "@RootResearchDesignId": "74"},
                    {"@ResearchDesignId": "77", "@ResearchName": "Fitness 201", "@RootResearchDesignId": "74"},
                    {"@ResearchDesignId": "79", "@ResearchName": "Fitness 203", "@RootResearchDesignId": "74"},
                    {"@ResearchDesignId": "104", "@ResearchName": "Education 201", "@RootResearchDesignId": "0"},
                    {"@ResearchDesignId": "105", "@ResearchName": "Night Classes 202", "@RootResearchDesignId": "104"},
                    {"@ResearchDesignId": "38", "@ResearchName": "Advanced Training"},
                    {
                        "@ResearchDesignId": "39",
                        "@ResearchName": "Advanced Training Lv2",
                        "@RootResearchDesignId": "38",
                    },
                    {"@ResearchDesignId": "80", "@ResearchName": "Fitness 201", "@RootResearchDesignId": "74"},
                    {"@ResearchDesignId": "81", "@RootResearchDesignId": "74"},
                ]
            }
        }

        def levels(*records: dict) -> dict:
            return dict(parse_research_levels({"Researches": {"Research": list(records)}}, designs))

        def done(design_id: str, **extra: str) -> dict:
            return {"@ResearchDesignId": design_id, "@ResearchState": "Completed", **extra}

        # Each completed design counts under its family (max course) and under its own exact name (review #2).
        self.assertEqual(levels(done("77")), {"Fitness": 201, "Fitness 201": 201})
        all_fitness = {"Fitness": 203, "Fitness 101": 101, "Fitness 201": 201, "Fitness 203": 203}
        self.assertEqual(levels(done("77"), done("79"), done("74")), all_fitness)  # max wins, in any order
        self.assertEqual(levels({"@ResearchDesignId": "79", "@ResearchState": "Researching"}), {})
        self.assertEqual(levels({"@ResearchDesignId": "79", "@ResearchState": "completed"}), {})
        self.assertEqual(levels(done("104")), {"Education": 201, "Education 201": 201})  # no root: own name
        self.assertEqual(levels(done("105")), {"Education": 202, "Night Classes 202": 202})  # family from the root
        self.assertEqual(levels(done("38")), {"Advanced Training": 1})  # an unnumbered root is the line's level 1
        self.assertEqual(levels(done("39")), {"Advanced Training": 2, "Advanced Training Lv2": 2})
        self.assertEqual(levels(done("80"), done("77")), {"Fitness": 201, "Fitness 201": 201})  # shared exact name
        self.assertEqual(levels(done("81")), {"Fitness": 1, "research_design:81": 1})  # nameless: level 1
        # Only the design's name sets the level: an unverified @ResearchLevel or @Level is never read.
        self.assertEqual(levels(done("74", **{"@ResearchLevel": "203"})), {"Fitness": 101, "Fitness 101": 101})
        levelled = {"ResearchDesign": {"@ResearchDesignId": "74", "@ResearchName": "Fitness 101", "@Level": "3"}}
        levelled_levels = dict(parse_research_levels({"Research": done("74")}, levelled))
        self.assertEqual(levelled_levels, {"Fitness": 101, "Fitness 101": 101})
        self.assertEqual(levels(done("999"), done("x")), {})  # unknown ids ignored
        self.assertEqual(dict(parse_research_levels(None, designs)), {})
        self.assertEqual(dict(parse_research_levels({"Research": done("77")}, None)), {})

    def test_research_course_table(self) -> None:
        # The one research-name contract, shared with Client.getResearchLevel: "<family> <course>" (the live training
        # lines) or "<family> Lv<n>" (live lines such as "Advanced Training Lv2"); any other name is its own family
        # at level 1. A course number is a positive ASCII integer without a leading zero.
        cases = {
            "Fitness 101": ("Fitness", 101),
            "Fitness 203": ("Fitness", 203),
            "Education 202": ("Education", 202),
            "Fitness Center 301": ("Fitness Center", 301),
            " Fitness\t201 ": ("Fitness", 201),
            "Fitness  1": ("Fitness", 1),
            "Advanced Training": ("Advanced Training", 1),
            "Advanced Training Lv2": ("Advanced Training", 2),
            "Advanced Training Lv7": ("Advanced Training", 7),
            "Fitness": ("Fitness", 1),
            "": ("", 1),
            "   ": ("", 1),
            "201": ("201", 1),
            "Fitness201": ("Fitness201", 1),
            "Wolv2": ("Wolv2", 1),
            "Fitness 0": ("Fitness 0", 1),
            "Fitness 007": ("Fitness 007", 1),
            "Fitness 20a": ("Fitness 20a", 1),
            "Fitness ٢٠١": ("Fitness ٢٠١", 1),
            "Fitness Lv": ("Fitness Lv", 1),
            "Fitness Lv0": ("Fitness Lv0", 1),
            "Fitness lv2": ("Fitness lv2", 1),
            "Fitness Lv.3": ("Fitness Lv.3", 1),
            "Fitness Level 3": ("Fitness Level", 3),
            "Fitness " + "9" * 5000: ("Fitness", 0),
        }
        for name, expected in cases.items():
            with self.subTest(name=name[:40]):
                self.assertEqual(research_course(name), expected)

    def test_research_course_is_linear_in_the_name_length(self) -> None:
        # Regression (review): a lazy "(.+?)\s+" family regex took seconds on a name with a long whitespace run.
        started = time.perf_counter()
        self.assertEqual(research_course("a" + " " * 200_000 + "x"), ("a" + " " * 200_000 + "x", 1))
        self.assertEqual(research_course("Fitness" + " " * 200_000 + "203"), ("Fitness", 203))
        self.assertEqual(research_course("1 " * 100_000 + "z"), ("1 " * 100_000 + "z", 1))
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_family_levels_hold_only_family_keys(self) -> None:
        # Review: a line query must not be answered by a design's exact name. "Night Classes" is a course of the
        # Education line (its root), not a line of its own.
        designs = {
            "ResearchDesign": [
                {"@ResearchDesignId": "101", "@ResearchName": "Education 101", "@RootResearchDesignId": "101"},
                {"@ResearchDesignId": "107", "@ResearchName": "Night Classes", "@RootResearchDesignId": "101"},
                {"@ResearchDesignId": "77", "@ResearchName": "Fitness 201", "@RootResearchDesignId": "74"},
                {"@ResearchDesignId": "74", "@ResearchName": "Fitness 101", "@RootResearchDesignId": "74"},
            ]
        }

        def family(*design_ids: str) -> dict:
            return dict(parse_research_family_levels(completed(*design_ids), designs))

        self.assertEqual(family("107"), {"Education": 1})
        self.assertEqual(family("107", "101"), {"Education": 101})
        self.assertEqual(family("77", "74", "101"), {"Fitness": 201, "Education": 101})
        self.assertEqual(family(), {})
        researching = {"Research": {"@ResearchDesignId": "77", "@ResearchState": "Researching"}}
        self.assertEqual(dict(parse_research_family_levels(researching, designs)), {})
        self.assertEqual(dict(parse_research_family_levels(completed("999"), designs)), {})
        self.assertEqual(dict(parse_research_family_levels(completed("77"), None)), {})
        # The family keys agree with parse_research_levels' family keys.
        everything = parse_research_levels(completed("107", "77", "74", "101"), designs)
        self.assertEqual({k: everything[k] for k in ("Fitness", "Education")}, family("107", "77", "74", "101"))

    def test_a_course_beyond_the_digit_limit_is_never_a_level(self) -> None:
        # Its level reads as 0, so neither its family nor its exact name can pass any gate (each needs >= 1).
        huge = "Fitness " + "9" * 5000
        designs = {"ResearchDesign": {"@ResearchDesignId": "1", "@ResearchName": huge, "@RootResearchDesignId": "1"}}
        researches = {"Research": {"@ResearchDesignId": "1", "@ResearchState": "Completed"}}
        self.assertEqual(dict(parse_research_levels(researches, designs)), {"Fitness": 0, huge: 0})
        self.assertEqual(dict(parse_research_family_levels(researches, designs)), {"Fitness": 0})


LIVE_FITNESS = {"74": 101, "75": 102, "76": 103, "77": 201, "78": 202, "79": 203}
LIVE_EDUCATION = {"101": 101, "102": 102, "103": 103, "104": 201, "105": 202, "106": 203}


def completed(*design_ids: str) -> dict:
    return {"Researches": {"Research": [{"@ResearchDesignId": i, "@ResearchState": "Completed"} for i in design_ids]}}


class TestLiveResearchDesigns(unittest.TestCase):
    """The live ListAllResearchDesigns2 reply (design version 717, fetched 2026-11-26), trimmed to the training lines.

    Regression: the parser only knew a "Lv N"/"Level N" suffix, so "Fitness 201" was its own family at level 1 and
    no research-gated training tier ever opened on live data.
    """

    def setUp(self) -> None:
        self.designs = load("live_research_designs.xml")

    def test_fixture_is_the_live_reply(self) -> None:
        envelope = self.designs["ResearchService"]["ListAllResearchDesigns"]
        self.assertEqual(envelope["@version"], "717")
        records = extract_collection(self.designs, "ResearchDesign")
        rows = {r["@ResearchDesignId"]: (r["@ResearchName"], r["@RootResearchDesignId"]) for r in records}
        for design_id, course in LIVE_FITNESS.items():
            self.assertEqual(rows[design_id], (f"Fitness {course}", "74"))
        for design_id, course in LIVE_EDUCATION.items():
            self.assertEqual(rows[design_id], (f"Education {course}", "101"))
        self.assertEqual(rows["38"], ("Advanced Training", "38"))
        self.assertEqual(len(records), 19)
        self.assertFalse(any("@Level" in record for record in records))  # no level attribute live

    def test_all_six_fitness_courses_give_fitness_203(self) -> None:
        levels = parse_research_levels(completed(*LIVE_FITNESS), self.designs)
        self.assertEqual(levels["Fitness"], 203)
        self.assertEqual({design_id: levels[f"Fitness {c}"] for design_id, c in LIVE_FITNESS.items()}, LIVE_FITNESS)
        self.assertNotIn("Education", levels)

    def test_only_fitness_101_gives_101(self) -> None:
        levels = dict(parse_research_levels(completed("74"), self.designs))
        self.assertEqual(levels, {"Fitness": 101, "Fitness 101": 101})

    def test_each_course_of_each_line_counts_at_its_number(self) -> None:
        for family, courses in (("Fitness", LIVE_FITNESS), ("Education", LIVE_EDUCATION)):
            ids = list(courses)
            for index, design_id in enumerate(ids):
                with self.subTest(family=family, course=courses[design_id]):
                    levels = parse_research_levels(completed(*ids[: index + 1]), self.designs)
                    self.assertEqual(levels[family], courses[design_id])

    def test_advanced_training_lv_names_count_at_their_number(self) -> None:
        levels = parse_research_levels(completed("38", "39", "40"), self.designs)
        self.assertEqual((levels["Advanced Training"], levels["Advanced Training Lv3"]), (3, 3))

    def test_every_gated_tier_opens_with_all_courses_of_one_line(self) -> None:
        # The defect end to end: on live data every research-gated tier stayed shut with every Fitness course done.
        for courses in (LIVE_FITNESS, LIVE_EDUCATION):
            levels = parse_research_levels(completed(*courses), self.designs)
            for band in DEFAULT_STRATEGY.bands:
                with self.subTest(band=(band.slot, band.tier), line=next(iter(courses))):
                    self.assertTrue(research_ok(band, levels, DEFAULT_STRATEGY))
        nothing = parse_research_levels(completed(), self.designs)
        shut = [(b.slot, b.tier) for b in DEFAULT_STRATEGY.bands if not research_ok(b, nothing, DEFAULT_STRATEGY)]
        self.assertEqual(shut, [("primary", 1), ("primary", 2), ("primary", 3), ("secondary", 2), ("secondary", 3)])


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
        fitness = {"Fitness": 201, "Fitness 101": 101, "Fitness 102": 102, "Fitness 103": 103, "Fitness 201": 201}
        education = {f"Education {course}": course for course in (101, 102, 103, 201, 202, 203)}
        self.assertEqual(dict(ctx.research_levels), {**fitness, "Education": 203, **education})
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
        # RequiredResearchDesignId 38) names the research design, not the unresolvable "research_design:38".
        designs = {design.id: design for design in fixture_context().designs}
        self.assertEqual(designs[108].required_research, "Advanced Training")
        self.assertEqual({d.id for d in designs.values() if d.required_research is not None}, {108})
        # End to end: once research 38 completes, crew 1009 starts the gated design instead of research_missing.
        researches = load("researches.xml")
        records = researches["ResearchService"]["ListAllResearches"]["Researches"]["Research"]
        self.assertEqual(records[-1]["@ResearchDesignId"], "38")
        records[-1]["@ResearchState"] = "Completed"
        ctx = fixture_context(researches_raw=researches)
        self.assertEqual(ctx.research_levels.get("Advanced Training"), 1)
        rows = summarize(plan_training(ctx))
        self.assertIn(("start", 1009, 108), rows)
        self.assertNotIn(("skip", 1009, "research_missing"), rows)

    def test_design_research_gate_needs_the_exact_course(self) -> None:
        # Regression (review #2): a design requiring a higher course was satisfied by a lower one because only the
        # family name was kept. Crew 1001 (weapons, 20%) trains design 101, gated here on Fitness 203 (live id 79).
        training = load("training_designs_list.xml")
        records = training["TrainingService"]["ListAllTrainingDesigns2"]["TrainingDesigns"]["TrainingDesign"]
        self.assertEqual(records[0]["@TrainingDesignId"], "101")
        records[0]["@RequiredResearchDesignId"] = "79"

        def plan_with(*completed: str) -> list[tuple]:
            researches = {"Research": [{"@ResearchDesignId": rid, "@ResearchState": "Completed"} for rid in completed]}
            ctx = fixture_context(
                training_designs_raw=training,
                research_designs_raw=load("live_research_designs.xml"),
                researches_raw=researches,
            )
            self.assertEqual({d.id: d.required_research for d in ctx.designs}[101], "Fitness 203")
            return [row for row in summarize(plan_training(ctx)) if row[1] == 1001]

        self.assertEqual(plan_with("74"), [("skip", 1001, "research_missing")])
        self.assertEqual(plan_with("74", "75", "76", "77", "78"), [("skip", 1001, "research_missing")])
        self.assertEqual(plan_with("74", "75", "76", "77", "78", "79"), [("start", 1001, 101)])
        self.assertEqual(plan_with("79"), [("start", 1001, 101)])

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
    "Fitness 201",
    "Advanced Training Lv2",
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
    "Research": ("@ResearchDesignId", "@ResearchState"),
    "ResearchDesign": ("@ResearchDesignId", "@RootResearchDesignId", "@ResearchName"),
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


def nest(inner: object, levels: int) -> object:
    """``inner`` wrapped in ``levels`` single-key dicts, so it sits ``levels`` dicts below the top level."""
    for _ in range(levels):
        inner = {"Wrap": inner}
    return inner


def nest_in_lists(inner: object, levels: int) -> object:
    """``inner`` wrapped in ``levels`` one-element lists (several sibling elements nest as lists)."""
    for _ in range(levels):
        inner = [inner]
    return inner


# The envelope search is bounded (a hostile or corrupt payload cannot exhaust the stack). The bound is part of the
# contract: records, containers, junk items and errorMessages are seen down to 64 levels below the top level.
SEARCH_DEPTH = 64


class TestEnvelopeSearchDepth(unittest.TestCase):
    def test_extract_collection_sees_records_down_to_the_depth_limit_and_no_deeper(self) -> None:
        record = {"@a": "1"}
        self.assertEqual(extract_collection(nest({"Item": record}, SEARCH_DEPTH), "Item"), [record])
        self.assertEqual(extract_collection(nest({"Item": record}, SEARCH_DEPTH + 1), "Item"), [])
        # The same bound applies through the typed parsers.
        deep_items = nest({"Item": {"@ItemDesignId": "7", "@Quantity": "2"}}, SEARCH_DEPTH)
        self.assertEqual(dict(parse_items(deep_items)), {7: 2})

    def test_classify_payload_sees_a_container_down_to_the_depth_limit(self) -> None:
        self.assertEqual(classify_payload(nest({"Characters": None}, SEARCH_DEPTH), "Character"), "empty")
        self.assertEqual(classify_payload(nest({"Characters": None}, SEARCH_DEPTH + 1), "Character"), "error")

    def test_classify_payload_sees_a_junk_item_at_the_depth_limit(self) -> None:
        # Text where records belong is an unexpected schema ("error"), not an empty item element ("empty").
        self.assertEqual(classify_payload(nest({"Character": "text"}, SEARCH_DEPTH), "Character"), "error")
        self.assertEqual(classify_payload(nest({"Character": None}, SEARCH_DEPTH), "Character"), "empty")

    def test_classify_payload_sees_an_error_message_at_the_depth_limit(self) -> None:
        records = {"Characters": {"Character": {"@CharacterId": "1"}}}
        self.assertEqual(classify_payload(records, "Character"), "ok")
        # "Meta" is one level down, so SEARCH_DEPTH - 1 wrappers put the errorMessage at the limit.
        for key in ("@errorMessage", "errorMessage"):
            with self.subTest(key=key, via="dicts"):
                raw = {**records, "Meta": nest({key: "x"}, SEARCH_DEPTH - 1)}
                self.assertEqual(classify_payload(raw, "Character"), "error")
            with self.subTest(key=key, via="lists"):
                raw = {**records, "Meta": nest_in_lists({key: "x"}, SEARCH_DEPTH - 1)}
                self.assertEqual(classify_payload(raw, "Character"), "error")

    def test_deep_clean_subtree_beside_records_is_not_an_error(self) -> None:
        # Running out of search depth is not evidence of an error: a clean subtree nested beyond the limit leaves
        # a payload with records "ok".
        records = {"Characters": {"Character": {"@CharacterId": "1"}}}
        for deep in (nest({}, SEARCH_DEPTH + 6), nest_in_lists({}, SEARCH_DEPTH + 6)):
            with self.subTest(deep=type(deep).__name__):
                self.assertEqual(classify_payload({**records, "Meta": deep}, "Character"), "ok")


# Every classify_payloads keyword with the record type it is bound to and that type's live container.
PAYLOAD_KEYWORDS = {
    "characters_raw": ("Character", "Characters"),
    "character_designs_raw": ("CharacterDesign", "CharacterDesigns"),
    "training_designs_raw": ("TrainingDesign", "TrainingDesigns"),
    "rooms_raw": ("Room", "Rooms"),
    "room_designs_raw": ("RoomDesign", "RoomDesigns"),
    "researches_raw": ("Research", "Researches"),
    "research_designs_raw": ("ResearchDesign", "ResearchDesigns"),
    "items_raw": ("Item", "Items"),
    "item_designs_raw": ("ItemDesign", "ItemDesigns"),
}


class TestPayloadBinding(unittest.TestCase):
    def test_every_classify_payloads_keyword_reads_its_own_record_type(self) -> None:
        for keyword, (item_key, container) in PAYLOAD_KEYWORDS.items():
            with self.subTest(keyword=keyword):
                filled = {"Svc": {container: {item_key: {"@Id": "1"}}}}
                self.assertEqual(dict(classify_payloads(**{keyword: filled})), {keyword: "ok"})
                empty = {"Svc": {container: None}}
                self.assertEqual(dict(classify_payloads(**{keyword: empty})), {keyword: "empty"})
                # Records of another type are not this keyword's data: an unexpected schema.
                other = "Room" if item_key != "Room" else "Item"
                wrong = {"Svc": {other + "s": {other: {"@Id": "1"}}}}
                self.assertEqual(dict(classify_payloads(**{keyword: wrong})), {keyword: "error"})

    def test_unknown_item_key_defaults_to_its_plural_container(self) -> None:
        self.assertEqual(classify_payload({"Svc": {"Widgets": None}}, "Widget"), "empty")
        self.assertEqual(classify_payload({"Svc": {"Widgets": {"Widget": {"@Id": "1"}}}}, "Widget"), "ok")
        self.assertEqual(classify_payload({"Svc": {"Gadgets": None}}, "Widget"), "error")

    def test_an_item_key_naming_no_record_type_never_borrows_a_known_container(self) -> None:
        # The live-container table is keyed by record type only. The empty item key names no record type, so it
        # falls back to "s" and a known container (present but empty) must not make its payload read as "empty".
        for _item_key, container in PAYLOAD_KEYWORDS.values():
            with self.subTest(container=container):
                self.assertEqual(classify_payload({"Svc": {container: None}}, ""), "error")


class TestLowestValidIds(unittest.TestCase):
    """Id 1 and level 1 are ordinary values: every "positive" check must accept them."""

    def test_required_research_id_one_resolves(self) -> None:
        research_designs = {"ResearchDesign": {"@ResearchDesignId": "1", "@ResearchName": "Fitness 101"}}
        designs = designs_envelope(
            {"@TrainingDesignId": "1", "@TrainingName": "Bench Press", "@RequiredResearchDesignId": "1"}
        )
        (design,) = parse_training_designs(designs, research_designs)
        self.assertEqual((design.id, design.required_research), (1, "Fitness 101"))
        (unresolved,) = parse_training_designs(designs)
        self.assertEqual(unresolved.required_research, "research_design:1")

    def test_crew_room_id_and_training_design_id_one_are_kept(self) -> None:
        record = {
            **CHARACTER,
            "@RoomId": "1",
            "@TrainingDesignId": "1",
            "@TrainingEndDate": "2026-03-01T13:00:00",
        }
        rooms = {"Room": {"@RoomId": "1", "@RoomDesignId": "1"}}
        room_designs = {"RoomDesign": {"@RoomDesignId": "1", "@RoomName": "Academy"}}
        (crew,) = parse_crew(characters_envelope(record), CHARACTER_DESIGNS, rooms, room_designs)
        self.assertEqual((crew.room_id, crew.room_name), (1, "Academy"))
        self.assertEqual(crew.training_design_id, 1)
        self.assertEqual(crew.training_end, datetime(2026, 3, 1, 13, 0, tzinfo=UTC))

    def test_item_design_id_one_is_counted(self) -> None:
        items = {"Item": [{"@ItemDesignId": "1", "@Quantity": "2"}, {"@ItemDesignId": "1", "@Quantity": "3"}]}
        self.assertEqual(dict(parse_items(items)), {1: 5})

    def test_course_one_is_a_level(self) -> None:
        # A course number of 1 is an ordinary level, like the level 1 of an unnumbered name.
        designs = {"ResearchDesign": [{"@ResearchDesignId": "1", "@ResearchName": "Fitness 1"}]}
        researches = {"Research": {"@ResearchDesignId": "1", "@ResearchState": "Completed"}}
        self.assertEqual(dict(parse_research_levels(researches, designs)), {"Fitness": 1, "Fitness 1": 1})


class TestConsumableDesignFilter(unittest.TestCase):
    def test_non_consumable_items_with_stat_and_tier_words_are_ignored(self) -> None:
        designs = {
            "ItemDesign": [
                {"@ItemDesignId": "5", "@ItemName": "Legendary Weapon Mount", "@ItemSubType": "Equipment"},
                {"@ItemDesignId": "6", "@ItemName": "Hero HP Armor", "@Rarity": "Hero"},
                {"@ItemDesignId": "7", "@ItemName": "Legendary Weapon Consumable", "@ItemSubType": "Equipment"},
                {"@ItemDesignId": "8", "@ItemName": "Hero HP Serum", "@ItemSubType": "Consumable"},
            ]
        }
        self.assertEqual(dict(parse_consumable_designs(designs)), {("WPN", "Legendary"): 7, ("HP", "Hero"): 8})


# --- SPEC-14 observations: course prices, course and room design facts, training sessions, Prestige recipes ---

AWARE_END = datetime(2026, 3, 1, 13, 0, tzinfo=UTC)


def sample_course(**overrides: object) -> CourseObservation:
    fields: dict = dict(
        design_id=40001,
        name="Engineering Manual",
        rank=1,
        stat="RepairImprovement",
        required_room_name="Academy",
        required_room_level=2,
        required_research_design_id=60001,
        price=CoursePrice(PRICE_OK, CURRENCY_MINERAL, 500),
    )
    fields.update(overrides)
    return CourseObservation(**fields)


# Verifies: S14-R2
class TestObservationModel(unittest.TestCase):
    def test_constants_are_pinned(self) -> None:
        self.assertEqual(
            (PRICE_OK, PRICE_UNAVAILABLE, PRICE_MALFORMED, PRICE_PREMIUM), ("ok", "unavailable", "malformed", "premium")
        )
        self.assertEqual((CURRENCY_MINERAL, CURRENCY_GAS, CURRENCY_STARBUX), ("mineral", "gas", "starbux"))
        self.assertEqual(
            (SESSION_NONE, SESSION_RUNNING, SESSION_ENDED_UNCLAIMED, SESSION_FINISHED, SESSION_UNKNOWN),
            ("none", "running", "ended_unclaimed", "finished", "unknown"),
        )
        self.assertEqual(PRICE_STATUSES, frozenset({"ok", "unavailable", "malformed", "premium"}))
        self.assertEqual(SPENDABLE_CURRENCIES, frozenset({"mineral", "gas"}))
        self.assertEqual(PRICE_CURRENCIES, frozenset({"mineral", "gas", "starbux"}))
        self.assertEqual(SESSION_PHASES, frozenset({"none", "running", "ended_unclaimed", "finished", "unknown"}))

    def test_observations_are_frozen(self) -> None:
        values = (
            (CoursePrice(PRICE_UNAVAILABLE), "status"),
            (sample_course(), "price"),
            (RoomDesignObservation(11, "Academy", 2, 2, None), "level"),
            (TrainingSession(None, None, SESSION_NONE), "phase"),
            (PrestigeRecipe(30001, 30001, 30002), "to_design_id"),
        )
        for value, attribute in values:
            with self.subTest(type=type(value).__name__), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(value, attribute, None)

    def test_unknown_price_status_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown price status"):
            CoursePrice("free")

    def test_an_ok_price_needs_a_spendable_currency_and_a_whole_amount(self) -> None:
        for currency, amount in ((CURRENCY_STARBUX, 5), (None, 5), (CURRENCY_MINERAL, None), (CURRENCY_MINERAL, -1)):
            with self.subTest(currency=currency, amount=amount), self.assertRaisesRegex(ValueError, "ok price"):
                CoursePrice(PRICE_OK, currency, amount)
        for currency in (CURRENCY_MINERAL, CURRENCY_GAS):
            with self.subTest(currency=currency):
                zero = CoursePrice(PRICE_OK, currency, 0)
                self.assertEqual((zero.currency, zero.amount), (currency, 0))
                self.assertTrue(zero.usable)

    def test_only_an_ok_price_is_usable(self) -> None:
        self.assertTrue(CoursePrice(PRICE_OK, CURRENCY_GAS, 300).usable)
        for price in (
            CoursePrice(PRICE_UNAVAILABLE),
            CoursePrice(PRICE_MALFORMED),
            CoursePrice(PRICE_PREMIUM, CURRENCY_STARBUX, 5),
            CoursePrice(PRICE_PREMIUM, CURRENCY_STARBUX, 0),
        ):
            with self.subTest(price=price):
                self.assertFalse(price.usable)

    def test_a_price_amount_is_none_or_a_whole_int(self) -> None:
        for amount in (True, False, 5.0, "5"):
            for status, currency in ((PRICE_OK, CURRENCY_MINERAL), (PRICE_PREMIUM, CURRENCY_STARBUX)):
                with self.subTest(status=status, amount=amount), self.assertRaisesRegex(
                    ValueError, "price amount must be None or an int"
                ):
                    CoursePrice(status, currency, amount)  # type: ignore[arg-type]

    def test_an_unreadable_price_carries_no_currency_or_amount(self) -> None:
        for status in (PRICE_UNAVAILABLE, PRICE_MALFORMED):
            for currency, amount in ((CURRENCY_MINERAL, None), (None, 0), (CURRENCY_GAS, 500)):
                with self.subTest(status=status, currency=currency, amount=amount), self.assertRaisesRegex(
                    ValueError, "price carries no currency or amount"
                ):
                    CoursePrice(status, currency, amount)
            with self.subTest(status=status):
                self.assertEqual((CoursePrice(status).currency, CoursePrice(status).amount), (None, None))

    def test_a_premium_price_needs_starbux_and_a_whole_amount(self) -> None:
        for currency, amount in ((CURRENCY_MINERAL, 5), (None, 5), (CURRENCY_STARBUX, None), (CURRENCY_STARBUX, -1)):
            with self.subTest(currency=currency, amount=amount), self.assertRaisesRegex(ValueError, "premium price"):
                CoursePrice(PRICE_PREMIUM, currency, amount)
        zero = CoursePrice(PRICE_PREMIUM, CURRENCY_STARBUX, 0)
        self.assertEqual((zero.currency, zero.amount), (CURRENCY_STARBUX, 0))

    def test_training_session_validates_phase_and_end(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown session phase"):
            TrainingSession(7, AWARE_END, "started")
        with self.assertRaisesRegex(ValueError, "end must be a timezone-aware datetime"):
            TrainingSession(7, datetime(2026, 3, 1, 13, 0), SESSION_RUNNING)
        for phase in (SESSION_NONE, SESSION_UNKNOWN):
            with self.subTest(phase=phase):
                self.assertEqual(TrainingSession(None, None, phase).phase, phase)
        for phase in (SESSION_RUNNING, SESSION_ENDED_UNCLAIMED, SESSION_FINISHED):
            with self.subTest(phase=phase):
                self.assertEqual(TrainingSession(None, AWARE_END, phase).phase, phase)
        self.assertEqual(TrainingSession(7, AWARE_END, SESSION_RUNNING).end, AWARE_END)

    def test_a_dated_phase_needs_an_end(self) -> None:
        for phase in (SESSION_RUNNING, SESSION_ENDED_UNCLAIMED, SESSION_FINISHED):
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, f"a {phase} session needs an end"):
                TrainingSession(7, None, phase)

    def test_no_training_and_an_unknown_session_carry_no_end(self) -> None:
        for phase in (SESSION_NONE, SESSION_UNKNOWN):
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, f"a {phase} session carries no end"):
                TrainingSession(None, AWARE_END, phase)
        with self.assertRaisesRegex(ValueError, "no training carries no course"):
            TrainingSession(7, None, SESSION_NONE)
        self.assertEqual(TrainingSession(7, None, SESSION_UNKNOWN).course_id, 7)

    def test_a_course_id_is_none_or_a_positive_int(self) -> None:
        for course in (0, -1, True, "7", 7.0):
            with self.subTest(course=course), self.assertRaisesRegex(
                ValueError, "course_id must be None or a positive int"
            ):
                TrainingSession(course, AWARE_END, SESSION_RUNNING)  # type: ignore[arg-type]
        self.assertEqual(TrainingSession(1, AWARE_END, SESSION_RUNNING).course_id, 1)


# Verifies: S14-R2
class TestCoursePrice(unittest.TestCase):
    def test_no_price_is_unavailable(self) -> None:
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertEqual(parse_course_price(value), CoursePrice(PRICE_UNAVAILABLE, None, None))

    def test_mineral_and_gas_prices_are_ok(self) -> None:
        self.assertEqual(parse_course_price("mineral:500"), CoursePrice(PRICE_OK, CURRENCY_MINERAL, 500))
        self.assertEqual(parse_course_price("gas:300"), CoursePrice(PRICE_OK, CURRENCY_GAS, 300))

    def test_an_observed_zero_price_is_ok_and_usable(self) -> None:
        price = parse_course_price("mineral:0")
        self.assertEqual(price, CoursePrice(PRICE_OK, CURRENCY_MINERAL, 0))
        self.assertTrue(price.usable)

    def test_a_starbux_price_is_premium_and_never_usable(self) -> None:
        self.assertEqual(parse_course_price("starbux:5"), CoursePrice(PRICE_PREMIUM, CURRENCY_STARBUX, 5))
        zero = parse_course_price("Starbux:0")
        self.assertEqual(zero, CoursePrice(PRICE_PREMIUM, CURRENCY_STARBUX, 0))
        self.assertFalse(zero.usable)

    def test_case_and_surrounding_whitespace_are_tolerated(self) -> None:
        for value in (" Mineral : 500 ", "MINERAL:500", "mineral:0500", "\tmineral:500\n"):
            with self.subTest(value=value):
                self.assertEqual(parse_course_price(value), CoursePrice(PRICE_OK, CURRENCY_MINERAL, 500))

    def test_anything_that_could_change_the_amount_is_malformed(self) -> None:
        values = (
            "mineral:5O0",
            "mineral:",
            ":500",
            "mineral:-5",
            "mineral:+5",
            "mineral:5.0",
            "mineral:1_000",
            "mineral:\u0665\u0660\u0660",
            "mineral500",
            "mineral:500:1",
            "diamond:500",
            "mineral :5 0",
            "mineral:" + "9" * 5000,
            500,
            {},
            [],
        )
        for value in values:
            with self.subTest(value=value if not isinstance(value, str) else value[:20]):
                self.assertEqual(parse_course_price(value), CoursePrice(PRICE_MALFORMED, None, None))

    def test_a_malformed_starbux_amount_is_malformed_not_premium(self) -> None:
        self.assertEqual(parse_course_price("starbux:5O0"), CoursePrice(PRICE_MALFORMED, None, None))


COURSE_RECORD = {
    "@TrainingDesignId": "40001",
    "@TrainingName": "Engineering Manual",
    "@RepairChance": "100",
    "@Rank": "1",
    "@RequiredRoomType": "Academy",
    "@RequiredRoomLevel": "2",
    "@RequiredResearchDesignId": "60001",
    "@CostString": "mineral:500",
}


def course_record(**overrides: object) -> dict:
    record: dict[str, object] = dict(COURSE_RECORD)
    for key, value in overrides.items():
        if value is None:
            record.pop("@" + key, None)
        else:
            record["@" + key] = value
    return record


# Verifies: S14-R2
class TestCourseObservations(unittest.TestCase):
    def test_no_records_give_no_courses(self) -> None:
        for raw in (None, {}, "junk", {"TrainingDesigns": None}, {"TrainingDesigns": {"TrainingDesign": "x"}}):
            with self.subTest(raw=raw):
                self.assertEqual(parse_course_observations(raw), ())

    def test_one_fake_shaped_record_becomes_one_course(self) -> None:
        raw = {"TrainingDesigns": {"TrainingDesign": COURSE_RECORD}}
        self.assertEqual(parse_course_observations(raw), (sample_course(),))

    def test_payload_order_first_duplicate_and_positive_ids(self) -> None:
        records = [
            course_record(TrainingDesignId="40003", TrainingName="Third"),
            course_record(TrainingDesignId="40001", TrainingName="First"),
            course_record(TrainingDesignId="40003", TrainingName="Duplicate"),
            course_record(TrainingDesignId="0"),
            course_record(TrainingDesignId="-4"),
            course_record(TrainingDesignId="x"),
            course_record(TrainingDesignId=None),
        ]
        courses = parse_course_observations({"TrainingDesign": records})
        self.assertEqual([(course.design_id, course.name) for course in courses], [(40003, "Third"), (40001, "First")])

    def test_an_observed_zero_stays_zero(self) -> None:
        raw = {"TrainingDesign": course_record(Rank="0", RequiredRoomLevel="0", RequiredResearchDesignId="0")}
        (course,) = parse_course_observations(raw)
        self.assertEqual((course.rank, course.required_room_level, course.required_research_design_id), (0, 0, 0))

    def test_missing_attributes_are_unknown(self) -> None:
        record = {"@TrainingDesignId": "40001"}
        (course,) = parse_course_observations({"TrainingDesign": record})
        self.assertEqual(
            course,
            CourseObservation(40001, "", None, None, None, None, None, CoursePrice(PRICE_UNAVAILABLE)),
        )

    def test_malformed_numbers_are_unknown_never_zero(self) -> None:
        for value in ("x", "-1", "2.5", "\u0662", "9" * 5000, ""):
            with self.subTest(value=value[:12]):
                record = course_record(Rank=value, RequiredRoomLevel=value, RequiredResearchDesignId=value)
                (course,) = parse_course_observations({"TrainingDesign": record})
                self.assertEqual((course.rank, course.required_room_level, course.required_research_design_id), (None,) * 3)

    def test_required_room_name_is_stripped_and_keeps_its_case(self) -> None:
        cases = ((" Academy ", "Academy"), ("gym", "gym"))
        for value, expected in cases:
            with self.subTest(value=value):
                (course,) = parse_course_observations({"TrainingDesign": course_record(RequiredRoomType=value)})
                self.assertEqual(course.required_room_name, expected)

    def test_a_blank_room_requirement_is_no_requirement_and_a_missing_one_is_unknown(self) -> None:
        cases = (("", ""), ("   ", ""), (None, None), ({}, None), (5, None))
        for value, expected in cases:
            with self.subTest(value=value):
                (course,) = parse_course_observations({"TrainingDesign": course_record(RequiredRoomType=value)})
                self.assertEqual(course.required_room_name, expected)

    def test_stat_is_the_highest_positive_chance(self) -> None:
        record = course_record(RepairChance="20", PilotChance="60", HpChance="0")
        (course,) = parse_course_observations({"TrainingDesign": record})
        self.assertEqual(course.stat, "PilotImprovement")
        (course,) = parse_course_observations({"TrainingDesign": course_record(RepairChance=None)})
        self.assertIsNone(course.stat)

    def test_real_xml_keeps_malformed_and_unavailable_prices_apart(self) -> None:
        xml = (
            "<TrainingService><ListAllTrainingDesigns><TrainingDesigns>"
            '<TrainingDesign TrainingDesignId="40001" TrainingName="Engineering Manual" RepairChance="100" Rank="1"'
            ' RequiredRoomType="Academy" RequiredRoomLevel="2" RequiredResearchDesignId="60001"'
            ' CostString="mineral:5O0"/>'
            '<TrainingDesign TrainingDesignId="40002" TrainingName="Free Weights" HpChance="100" Rank="1"/>'
            "</TrainingDesigns></ListAllTrainingDesigns></TrainingService>"
        )
        first, second = parse_course_observations(xmltodict.parse(xml, xml_attribs=True))
        self.assertEqual(first, sample_course(price=CoursePrice(PRICE_MALFORMED)))
        self.assertEqual(
            second,
            CourseObservation(40002, "Free Weights", 1, "HpImprovement", None, None, None, CoursePrice(PRICE_UNAVAILABLE)),
        )


# Verifies: S14-R2
class TestRoomDesignObservations(unittest.TestCase):
    def test_no_records_give_an_empty_mapping(self) -> None:
        for raw in (None, {}, "junk", {"RoomDesigns": None}):
            with self.subTest(raw=raw):
                self.assertEqual(dict(parse_room_design_observations(raw)), {})

    def test_records_are_keyed_by_positive_id_and_the_first_wins(self) -> None:
        single = {"RoomDesign": {"@RoomDesignId": "11", "@RoomName": "Academy", "@Level": "2", "@Capacity": "2"}}
        self.assertEqual(dict(parse_room_design_observations(single)), {11: RoomDesignObservation(11, "Academy", 2, 2, None)})
        records = [
            {"@RoomDesignId": "12", "@RoomName": "Gym", "@Level": "1", "@Capacity": "1"},
            {"@RoomDesignId": "11", "@RoomName": "Academy", "@Level": "2", "@Capacity": "2"},
            {"@RoomDesignId": "12", "@RoomName": "Duplicate", "@Level": "9", "@Capacity": "9"},
            {"@RoomDesignId": "0", "@RoomName": "Zero"},
            {"@RoomDesignId": "-3", "@RoomName": "Negative"},
            {"@RoomName": "No id"},
        ]
        designs = parse_room_design_observations({"RoomDesigns": {"RoomDesign": records}})
        self.assertEqual(
            dict(designs),
            {12: RoomDesignObservation(12, "Gym", 1, 1, None), 11: RoomDesignObservation(11, "Academy", 2, 2, None)},
        )

    def test_an_observed_zero_capacity_stays_zero(self) -> None:
        raw = {"RoomDesign": {"@RoomDesignId": "11", "@RoomName": "Academy", "@Level": "0", "@Capacity": "0"}}
        self.assertEqual(parse_room_design_observations(raw)[11], RoomDesignObservation(11, "Academy", 0, 0, None))

    def test_missing_or_malformed_level_and_capacity_are_unknown(self) -> None:
        raw = {"RoomDesign": {"@RoomDesignId": "11", "@RoomName": "Academy", "@Level": "2"}}
        self.assertEqual(parse_room_design_observations(raw)[11], RoomDesignObservation(11, "Academy", 2, None, None))
        for value in ("x", "-1", "1.5", ""):
            with self.subTest(value=value):
                record = {"@RoomDesignId": "11", "@RoomName": "Academy", "@Level": value, "@Capacity": value}
                self.assertEqual(
                    parse_room_design_observations({"RoomDesign": record})[11],
                    RoomDesignObservation(11, "Academy", None, None, None),
                )

    def test_name_is_stripped_and_missing_is_empty(self) -> None:
        raw = {"RoomDesign": [{"@RoomDesignId": "11", "@RoomName": "  Academy "}, {"@RoomDesignId": "12"}]}
        designs = parse_room_design_observations(raw)
        self.assertEqual((designs[11].name, designs[12].name), ("Academy", ""))

    def test_room_type_is_stripped_blank_is_empty_and_missing_is_unknown(self) -> None:
        records = [
            {"@RoomDesignId": "11", "@RoomName": "Academy", "@RoomType": " Training "},
            {"@RoomDesignId": "12", "@RoomType": "  "},
            {"@RoomDesignId": "13"},
            {"@RoomDesignId": "14", "@RoomType": {}},
        ]
        designs = parse_room_design_observations({"RoomDesigns": {"RoomDesign": records}})
        self.assertEqual(designs[11], RoomDesignObservation(11, "Academy", None, None, "Training"))
        self.assertEqual([designs[key].room_type for key in (12, 13, 14)], ["", None, None])

    def test_the_result_is_read_only(self) -> None:
        designs = parse_room_design_observations({"RoomDesign": {"@RoomDesignId": "11"}})
        with self.assertRaises(TypeError):
            designs[12] = RoomDesignObservation(12, "", None, None, None)  # type: ignore[index]


def server_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


# Verifies: S14-R2, S14-R5
class TestTrainingSession(unittest.TestCase):
    at = FIXTURE_NOW
    past = server_time(FIXTURE_NOW - timedelta(seconds=1))
    future = server_time(FIXTURE_NOW + timedelta(seconds=1))

    def session(self, course: object, end: object) -> TrainingSession:
        return parse_training_session(course, end, self.at)

    def test_an_empty_end_is_no_training_and_drops_the_course(self) -> None:
        for course, end in ((None, ""), ("0", ""), ("7", ""), (7, ""), ("", "")):
            with self.subTest(course=course, end=end):
                self.assertEqual(self.session(course, end), TrainingSession(None, None, SESSION_NONE))

    def test_a_missing_end_is_unknown(self) -> None:
        # Unlike an empty @TrainingEndDate (no training), a missing one says nothing about the training.
        for course in (None, "0", "7", ""):
            with self.subTest(course=course):
                self.assertEqual(self.session(course, None), TrainingSession(None, None, SESSION_UNKNOWN))

    def test_a_future_end_is_running(self) -> None:
        end = FIXTURE_NOW + timedelta(seconds=1)
        self.assertEqual(self.session("7", self.future), TrainingSession(7, end, SESSION_RUNNING))
        self.assertEqual(self.session("1", self.future), TrainingSession(1, end, SESSION_RUNNING))
        self.assertEqual(self.session(7, self.future), TrainingSession(7, end, SESSION_RUNNING))
        for course in ("0", "abc", None, "", 0):
            with self.subTest(course=course):
                self.assertEqual(self.session(course, self.future), TrainingSession(None, end, SESSION_RUNNING))
        self.assertIsNotNone(getattr(self.session("7", self.future).end, "tzinfo", None))

    def test_an_end_at_or_before_the_observation_is_over(self) -> None:
        at_text, past = server_time(FIXTURE_NOW), FIXTURE_NOW - timedelta(seconds=1)
        cases = (
            (("7", at_text), TrainingSession(7, FIXTURE_NOW, SESSION_ENDED_UNCLAIMED)),
            (("7", self.past), TrainingSession(7, past, SESSION_ENDED_UNCLAIMED)),
            (("1", self.past), TrainingSession(1, past, SESSION_ENDED_UNCLAIMED)),
            (("0", at_text), TrainingSession(None, FIXTURE_NOW, SESSION_FINISHED)),
            (("00", self.past), TrainingSession(None, past, SESSION_FINISHED)),
            ((" 0 ", "2020-01-01T00:00:00"), TrainingSession(None, datetime(2020, 1, 1, tzinfo=UTC), SESSION_FINISHED)),
            ((0, self.past), TrainingSession(None, past, SESSION_FINISHED)),
            ((7, self.past), TrainingSession(7, past, SESSION_ENDED_UNCLAIMED)),
        )
        for (course, end), expected in cases:
            with self.subTest(course=course, end=end):
                self.assertEqual(self.session(course, end), expected)

    def test_a_missing_or_blank_course_with_an_ended_timer_is_unknown(self) -> None:
        # Only an explicit zero says "no course": a missing or blank course could be an ended course not yet claimed.
        for course in (None, "", "   "):
            for end in (self.past, server_time(FIXTURE_NOW), "2020-01-01T00:00:00"):
                with self.subTest(course=course, end=end):
                    self.assertEqual(self.session(course, end), TrainingSession(None, None, SESSION_UNKNOWN))

    def test_a_malformed_course_with_an_ended_timer_is_unknown(self) -> None:
        for course in ("abc", "-7", " ", "7.0", True, False, -7, 7.0):
            with self.subTest(course=course):
                self.assertEqual(self.session(course, self.past), TrainingSession(None, None, SESSION_UNKNOWN))

    def test_a_whole_number_is_ascii_digits_or_a_non_bool_int(self) -> None:
        whole = parse_module._whole_or_none
        cases = (
            ("7", 7), (" 07 ", 7), ("0", 0), (0, 0), (7, 7), (10**30, 10**30),
            (True, None), (False, None), (-1, None), (7.0, None), ("-1", None), ("", None), (None, None),
            ("9" * 5000, None), ("٧", None),
        )
        for value, expected in cases:
            with self.subTest(value=str(value)[:12]):
                self.assertEqual(whole(value), expected)

    def test_an_unreadable_end_is_unknown(self) -> None:
        for end in ("soon", "   ", {}, 0, "2026-03-01 13:00:00"):
            with self.subTest(end=end):
                self.assertEqual(self.session("7", end), TrainingSession(7, None, SESSION_UNKNOWN))
        self.assertEqual(self.session("abc", "soon"), TrainingSession(None, None, SESSION_UNKNOWN))
        self.assertEqual(self.session("0", "soon"), TrainingSession(None, None, SESSION_UNKNOWN))

    def test_an_offset_end_is_compared_in_utc(self) -> None:
        # 13:00+02:00 is 11:00 UTC: an hour before the observation, so the training is over, not running.
        session = self.session("7", "2026-03-01T13:00:00+02:00")
        self.assertEqual(session, TrainingSession(7, datetime(2026, 3, 1, 11, 0, tzinfo=UTC), SESSION_ENDED_UNCLAIMED))
        session = self.session("7", "2026-03-01T13:00:00-02:00")
        self.assertEqual(session.phase, SESSION_RUNNING)

    def test_a_naive_observation_time_is_refused(self) -> None:
        for end in (None, self.future):
            with self.subTest(end=end), self.assertRaisesRegex(ValueError, "at must be a timezone-aware datetime"):
                parse_training_session("7", end, datetime(2026, 3, 1, 12, 0))


def prestige(first: object, second: object, target: object) -> dict:
    record = {"@CharacterDesignId1": first, "@CharacterDesignId2": second, "@ToCharacterDesignId": target}
    return {key: value for key, value in record.items() if value is not None}


# Verifies: S14-R2
class TestPrestigeRecipes(unittest.TestCase):
    def test_no_payload_gives_no_recipes(self) -> None:
        for raw in (None, {}, "junk", {"Prestiges": None}):
            with self.subTest(raw=raw):
                self.assertEqual(parse_prestige_recipes(raw), ())

    def test_fake_xml_keeps_a_two_copy_recipe(self) -> None:
        xml = (
            "<CharacterService><PrestigeCharacterTo><Prestiges>"
            '<Prestige CharacterDesignId1="30001" CharacterDesignId2="30001" ToCharacterDesignId="30002"/>'
            "</Prestiges></PrestigeCharacterTo></CharacterService>"
        )
        self.assertEqual(
            parse_prestige_recipes(xmltodict.parse(xml, xml_attribs=True)), (PrestigeRecipe(30001, 30001, 30002),)
        )

    def test_payload_order_is_kept_and_identical_rows_both_count(self) -> None:
        rows = [prestige("30003", "30004", "30009"), prestige("30001", "30002", "30008"), prestige("30001", "30002", "30008")]
        self.assertEqual(
            parse_prestige_recipes({"Prestiges": {"Prestige": rows}}),
            (PrestigeRecipe(30003, 30004, 30009), PrestigeRecipe(30001, 30002, 30008), PrestigeRecipe(30001, 30002, 30008)),
        )

    def test_a_record_without_three_positive_ids_is_skipped(self) -> None:
        bad = (None, "0", "x", "-5", "2.5", "")
        rows = []
        for value in bad:
            rows += [prestige(value, "30002", "30008"), prestige("30001", value, "30008"), prestige("30001", "30002", value)]
        rows.append(prestige("30001", "30002", "30008"))
        self.assertEqual(parse_prestige_recipes({"Prestige": rows}), (PrestigeRecipe(30001, 30002, 30008),))


OBSERVATION_SCALARS = FUZZ_SCALARS + (
    "mineral:500",
    "gas:0",
    "starbux:5",
    " Mineral : 0500 ",
    "mineral:5O0",
    "mineral:",
    ":",
    "starbux:-1",
    "\u0665",
    "2026-03-01T13:00:00",
    "2026-03-01T13:00:00+02:00",
    "soon",
)
OBSERVATION_RECORDS = {
    "TrainingDesign": (
        "@TrainingDesignId", "@TrainingName", "@Rank", "@RequiredRoomType", "@RequiredRoomLevel",
        "@RequiredResearchDesignId", "@CostString", "@RepairChance", "@HpChance",
    ),
    "RoomDesign": ("@RoomDesignId", "@RoomName", "@Level", "@Capacity", "@RoomType"),
    "Prestige": ("@CharacterDesignId1", "@CharacterDesignId2", "@ToCharacterDesignId"),
}


def observation_value(rng: random.Random) -> object:
    return rng.choice(OBSERVATION_SCALARS) if rng.random() < 0.85 else garbage(rng, 3)


def observation_payload(rng: random.Random, item_key: str) -> object:
    """An envelope of records over ``item_key``'s attributes, or plain garbage."""
    if rng.random() < 0.15:
        return garbage(rng)
    attrs = OBSERVATION_RECORDS[item_key]
    records = [
        {key: observation_value(rng) for key in rng.sample(attrs, rng.randint(0, len(attrs)))}
        for _ in range(rng.randint(0, 4))
    ]
    value: object = records[0] if len(records) == 1 and rng.random() < 0.5 else records
    return {item_key + "s": {item_key: value}}


def optional(value: object, kind: type) -> bool:
    return value is None or (isinstance(value, kind) and not isinstance(value, bool))


# Verifies: S14-R2
class TestObservationFuzz(unittest.TestCase):
    def test_observation_parsers_never_raise_and_keep_their_types(self) -> None:
        for seed in range(300):
            rng = random.Random(seed)
            with self.subTest(seed=seed):
                price = parse_course_price(observation_value(rng))
                self.assertIn(price.status, PRICE_STATUSES)
                self.assertTrue(optional(price.amount, int))
                for course in parse_course_observations(observation_payload(rng, "TrainingDesign")):
                    self.assertIsInstance(course.design_id, int)
                    self.assertGreater(course.design_id, 0)
                    self.assertIsInstance(course.name, str)
                    for number in (course.rank, course.required_room_level, course.required_research_design_id):
                        self.assertTrue(optional(number, int) and (number is None or number >= 0))
                    self.assertTrue(optional(course.required_room_name, str))
                    self.assertIn(course.price.status, PRICE_STATUSES)
                designs = parse_room_design_observations(observation_payload(rng, "RoomDesign"))
                for design_id, design in designs.items():
                    self.assertEqual(design.design_id, design_id)
                    self.assertTrue(optional(design.level, int) and optional(design.capacity, int))
                    self.assertTrue(optional(design.room_type, str))
                for recipe in parse_prestige_recipes(observation_payload(rng, "Prestige")):
                    ids = (recipe.first_design_id, recipe.second_design_id, recipe.to_design_id)
                    self.assertTrue(all(isinstance(value, int) and value > 0 for value in ids))
                session = parse_training_session(observation_value(rng), observation_value(rng), FIXTURE_NOW)
                self.assertIn(session.phase, SESSION_PHASES)
                self.assertTrue(optional(session.course_id, int) and (session.course_id or 1) > 0)
                self.assertTrue(session.end is None or session.end.utcoffset() == timedelta(0))


if __name__ == "__main__":
    unittest.main()
