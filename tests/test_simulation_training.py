"""Crew training in the simulated game: reads, the training state machine, consumables, faults, determinism.

Stdlib unittest, no network. Time moves only through ``game.clock.advance``; a snapshot ticks the clock by a
second, so no test takes one between an advance and the assertion that depends on the exact time.
"""

import collections
import copy
import json
import os
import random
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sdk.execution.runner import ActionRunner
from sdk.journal import GameplayJournal
from sdk.policy import POLICIES, ActionRequest, DesignData, Plan
from sdk.state import Completeness
from sdk.state.client_provider import ClientSnapshotProvider
from sdk.simulation.game import (
    Fault,
    SimulatedClock,
    SimulatedFaultError,
    SimulatedGame,
    build_games,
    load_scenario,
    run_scenario,
)

T0 = datetime(2027, 6, 1, 6, 0, tzinfo=timezone.utc)
STATS = ("Hp", "Pilot", "Repair", "Weapon", "Science", "Engine", "Attack", "Ability", "Stamina")
CREW_KEYS = {"@CharacterId", "@CharacterDesignId", "@CharacterName", "@RoomId", "@Level", "@Xp", "@Fatigue",
             "@TrainingDesignId", "@TrainingEndDate"} | {f"@{stat}Improvement" for stat in STATS}

DESIGN = DesignData.from_records(
    room_designs=[
        {"@RoomDesignId": "300", "@RoomName": "Gym"},
        {"@RoomDesignId": "301", "@UpgradeFromRoomDesignId": "300", "@PriceString": "mineral:100"},
        {"@RoomDesignId": "302", "@UpgradeFromRoomDesignId": "301"},
        {"@RoomDesignId": "400", "@RoomName": "Academy"},
        {"@RoomDesignId": "500", "@RoomName": "Bridge"},
    ],
    research_designs=[{"@ResearchDesignId": "610", "@GasCost": "10"}],
)


def training_design(design_id, stat, room, duration, fatigue, low, high, research=None):
    record = {"@TrainingDesignId": design_id, "@TrainingName": design_id, "@Rank": "1", "@TrainingStat": stat,
              "@Duration": str(duration), "@Fatigue": str(fatigue), "@MinGain": str(low), "@MaxGain": str(high),
              "@RequiredRoomDesignId": room}
    if research:
        record["@RequiredResearchDesignId"] = research
    return record


RULES: dict[str, Any] = {
    "fatigue_max": 100,
    "fatigue_recovery_seconds": 360,
    "character_designs": [
        {"@CharacterDesignId": "cd1", "@CharacterDesignName": "Recruit", "@Rarity": "Common",
         "@TrainingCapacity": "20", "@SpecialAbilityType": "None"},
        {"@CharacterDesignId": "cd-big", "@TrainingCapacity": "500"},
    ],
    "training_designs": [
        training_design("t-atk", "Attack", "300", 3600, 10, 4, 4),
        training_design("t-sci", "Science", "400", 3600, 10, 4, 4),
        training_design("t-atk2", "Attack", "300", 14400, 25, 6, 6, research="610"),
        training_design("t-301", "Attack", "301", 3600, 0, 1, 1),
        training_design("t-rand", "Pilot", "300", 3600, 0, 1, 10),
    ],
    "item_designs": [
        {"@ItemDesignId": "901", "@ItemName": "Grey ATK", "@ConsumableStat": "Attack", "@ConsumableGain": "1",
         "@ConsumableFatigue": "5"},
        {"@ItemDesignId": "950", "@ItemName": "Rest Pill", "@FatigueRecovery": "50"},
    ],
}

ROOMS = (
    {"@RoomId": "r-gym", "@RoomDesignId": "300", "@RoomStatus": "Normal"},
    {"@RoomId": "r-gym2", "@RoomDesignId": "301", "@RoomStatus": "Normal"},
    {"@RoomId": "r-acad", "@RoomDesignId": "400", "@RoomStatus": "Normal"},
    {"@RoomId": "r-bridge", "@RoomDesignId": "500", "@RoomStatus": "Normal"},
)


def member(cid="c1", room="r-gym", fatigue=0, design="cd1", **stats):
    record = {"@CharacterId": cid, "@CharacterDesignId": design, "@CharacterName": f"crew {cid}", "@RoomId": room,
              "@Level": "1", "@Fatigue": str(fatigue)}
    record.update({f"@{stat}Improvement": str(value) for stat, value in stats.items()})
    return record


def game(crew=(member(),), rooms=ROOMS, research=(), inventory=(), faults=(), training=RULES, training_rng=None,
         rng=None, alias="alpha", fault_rng=None):
    state = {"minerals": 100, "gas": 100, "rooms": [dict(room) for room in rooms],
             "research": [dict(entry) for entry in research], "inventory": [dict(item) for item in inventory],
             "crew": [dict(record) for record in crew]}
    if not crew:
        del state["crew"]
    return SimulatedGame(alias, state, DESIGN, rng or random.Random(0), SimulatedClock(T0), faults=faults,
                         fault_rng=fault_rng, training=training, training_rng=training_rng)


def characters(game_):
    game_.listAllCharactersOfUser()
    listed = game_.allCharactersOfUser["CharacterService"]["ListAllCharactersOfUser"]["Characters"]["Character"]
    return {record["@CharacterId"]: record for record in ([listed] if isinstance(listed, dict) else listed)}


def crew_record(game_, cid="c1"):
    return characters(game_)[cid]


class ReadShapeTests(unittest.TestCase):
    def test_no_crew_gives_an_empty_complete_crew_section(self):
        plain = game(crew=(), training=None)
        snapshot = plain.snapshot()
        self.assertEqual(snapshot.crew, ())
        self.assertEqual(snapshot.section("crew"), Completeness.COMPLETE)
        self.assertIs(plain.listAllCharactersOfUser(), True)
        self.assertEqual(plain.allCharactersOfUser,
                         {"CharacterService": {"ListAllCharactersOfUser": {"Characters": None}}})

    def test_crew_record_carries_every_training_field(self):
        record = crew_record(game())
        self.assertEqual(set(record), CREW_KEYS)
        self.assertTrue(all(isinstance(value, str) for value in record.values()))
        self.assertEqual({record[f"@{stat}Improvement"] for stat in STATS}, {"0"})
        self.assertEqual((record["@TrainingDesignId"], record["@TrainingEndDate"], record["@Xp"]), ("0", "", "0"))
        (crew,) = game().snapshot().crew
        self.assertEqual((crew.training_observed, crew.training_design_id, crew.training_end, crew.training_active),
                         (True, None, None, False))
        self.assertEqual((crew.character_id, crew.fatigue, crew.room_id, crew.level), ("c1", 0, "r-gym", 1))

    def test_reads_parse_through_the_live_snapshot_provider(self):
        g = game(crew=(member(), member("c2", room="r-acad", fatigue=30, Attack=3)),
                 inventory=({"@ItemId": "i1", "@ItemDesignId": "901", "@Quantity": "2"},))
        provided = ClientSnapshotProvider(g, "alpha", clock=lambda: T0)()
        expected = g.snapshot()
        self.assertEqual(provided.section("crew"), Completeness.COMPLETE)
        self.assertEqual(len(provided.crew), 2)
        self.assertEqual(provided.crew, expected.crew)
        self.assertEqual(provided.inventory, expected.inventory)
        empty = ClientSnapshotProvider(game(crew=(), training=None), "alpha", clock=lambda: T0)()
        self.assertEqual((empty.crew, empty.section("crew")), ((), Completeness.COMPLETE))
        self.assertEqual((empty.inventory, empty.section("inventory")), ((), Completeness.COMPLETE))

    def test_design_and_item_reads_match_client_shapes(self):
        item = {"@ItemId": "i1", "@ItemDesignId": "901", "@Quantity": "2"}
        g = game(inventory=(item,))
        self.assertIs(g.listAllTrainingDesigns2(), True)
        designs = g.trainingDesigns["TrainingService"]["ListAllTrainingDesigns"]["TrainingDesigns"]["TrainingDesign"]
        self.assertEqual(designs, RULES["training_designs"])
        self.assertIs(g.listAllCharacterDesigns2(), True)
        characters_ = g.allCharacterDesigns["CharacterService"]["ListAllCharacterDesigns"]
        self.assertEqual(characters_["CharacterDesigns"]["CharacterDesign"], RULES["character_designs"])
        self.assertEqual(characters_["CharacterDesigns"]["CharacterDesign"][0]["@TrainingCapacity"], "20")
        self.assertIs(g.listItemsOfAShip(), True)
        self.assertEqual(g.itemsOfAShip, {"ItemService": {"ListItemsOfAShip": {"Items": {"Item": item}}}})
        self.assertEqual(g.calls, [])  # reads are not calls and take no faults
        bare = game(crew=(), training=None)
        bare.listItemsOfAShip()
        bare.listAllTrainingDesigns2()
        self.assertEqual(bare.itemsOfAShip, {"ItemService": {"ListItemsOfAShip": {"Items": None}}})
        self.assertEqual(bare.trainingDesigns,
                         {"TrainingService": {"ListAllTrainingDesigns": {"TrainingDesigns": None}}})

    def test_returned_records_are_copies(self):
        inventory = [{"@ItemId": "i1", "@ItemDesignId": "901", "@Quantity": "2"}]
        crew = [member(Attack=1)]
        g = game(crew=crew, inventory=inventory)
        crew[0]["@AttackImprovement"] = "9"
        inventory[0]["@Quantity"] = "9"
        crew_record(g)["@AttackImprovement"] = "7"
        g.listItemsOfAShip()
        g.itemsOfAShip["ItemService"]["ListItemsOfAShip"]["Items"]["Item"]["@Quantity"] = "7"
        g.listAllTrainingDesigns2()
        g.trainingDesigns["TrainingService"]["ListAllTrainingDesigns"]["TrainingDesigns"]["TrainingDesign"][0][
            "@Duration"] = "1"
        self.assertEqual(crew_record(g)["@AttackImprovement"], "1")
        self.assertEqual(g.snapshot().inventory[0].quantity, 2)
        self.assertEqual(RULES["training_designs"][0]["@Duration"], "3600")


def stats_of(record):
    return {stat: int(record[f"@{stat}Improvement"]) for stat in STATS}


def rules(**changes):
    return dict(copy.deepcopy(RULES), **changes)


class StateMachineTests(unittest.TestCase):
    def test_start_assigns_the_design_its_end_and_its_fatigue(self):
        g = game()
        self.assertIs(g.addTraining("t-atk", "c1"), True)
        record = crew_record(g)
        self.assertEqual((record["@TrainingDesignId"], record["@TrainingEndDate"], record["@Fatigue"]),
                         ("t-atk", "2027-06-01T07:00:00", "10"))
        (entry,) = g.training.log
        self.assertEqual({key: entry[key] for key in ("seq", "op", "character_id", "design_id", "accepted", "end",
                                                      "fatigue", "at")},
                         {"seq": 1, "op": "start", "character_id": "c1", "design_id": "t-atk", "accepted": True,
                          "end": "2027-06-01T07:00:00", "fatigue": 10, "at": "2027-06-01T06:00:00"})
        (crew,) = g.snapshot().crew
        self.assertEqual((crew.training_design_id, crew.training_end, crew.training_active),
                         ("t-atk", "2027-06-01T07:00:00", True))
        self.assertEqual(g.calls, ["addTraining"])
        state = {"rooms": [dict(ROOMS[0])], "crew": [member()]}
        local = SimulatedGame("alpha", state, DESIGN, random.Random(0),
                              SimulatedClock(datetime(2027, 6, 1, 8, 0, tzinfo=timezone(timedelta(hours=2)))),
                              training=RULES)
        local.addTraining("t-atk", "c1")
        self.assertEqual(crew_record(local)["@TrainingEndDate"], "2027-06-01T07:00:00")

    def test_start_while_training_is_refused_and_changes_nothing(self):
        g = game()
        g.addTraining("t-atk", "c1")
        before = crew_record(g)
        for design in ("t-atk", "t-sci"):
            with self.subTest(design=design):
                self.assertIs(g.addTraining(design, "c1"), False)
                self.assertEqual(crew_record(g), before)
                self.assertEqual((g.training.log[-1]["accepted"], g.training.log[-1]["code"]), (False, "busy"))
        self.assertEqual(before["@Fatigue"], "10")

    def test_a_finished_session_must_be_collected_before_the_next_starts(self):
        g = game()
        g.addTraining("t-atk", "c1")
        g.clock.advance(timedelta(hours=2))
        self.assertIs(g.addTraining("t-atk", "c1"), False)
        self.assertEqual(crew_record(g)["@TrainingEndDate"], "2027-06-01T07:00:00")

    def test_finish_waits_for_the_end_to_the_second(self):
        g = game()
        g.addTraining("t-atk", "c1")
        g.clock.advance(timedelta(minutes=59, seconds=59))
        self.assertIs(g.finishTraining("c1"), False)
        self.assertEqual(crew_record(g)["@AttackImprovement"], "0")
        self.assertEqual({key: g.training.log[-1][key] for key in ("code", "design_id", "stat", "gain")},
                         {"code": "early", "design_id": "t-atk", "stat": "Attack", "gain": {}})
        g.clock.advance(timedelta(seconds=1))
        self.assertIs(g.finishTraining("c1"), True)
        self.assertEqual(crew_record(g)["@AttackImprovement"], "4")

    def test_finish_adds_the_gain_to_the_design_stat_only_and_clears_the_session(self):
        g = game(crew=(member(**{stat: 1 for stat in STATS}),))
        g.addTraining("t-atk", "c1")
        g.clock.advance(timedelta(hours=1))
        self.assertIs(g.finishTraining("c1"), True)
        record = crew_record(g)
        self.assertEqual(stats_of(record), dict({stat: 1 for stat in STATS}, Attack=5))
        self.assertEqual((record["@TrainingDesignId"], record["@TrainingEndDate"]), ("0", ""))
        finished = g.trainingFinish["TrainingService"]["FinishTraining"]["Character"]
        self.assertEqual(finished, record)
        finished["@AttackImprovement"] = "0"  # the response is a copy
        self.assertEqual(crew_record(g)["@AttackImprovement"], "5")
        entry = g.training.log[-1]
        self.assertEqual((entry["op"], entry["accepted"], entry["gain"], entry["capped"], entry["total"]),
                         ("finish", True, {"Attack": 4}, False, 13))
        (crew,) = g.snapshot().crew
        self.assertEqual((crew.training_design_id, crew.training_active), (None, False))

    def test_finish_without_a_session_is_refused(self):
        g = game()
        self.assertIs(g.finishTraining("c1"), False)
        self.assertEqual(g.training.log[-1]["code"], "idle")
        self.assertIs(g.finishTraining("ghost"), False)
        self.assertEqual(g.training.log[-1]["code"], "unknown-character")
        self.assertFalse(hasattr(g, "trainingFinish"))
        self.assertEqual(g.calls, ["finishTraining", "finishTraining"])

    def test_gain_is_scaled_by_the_fatigue_at_the_start(self):
        for fatigue, gain in ((30, 2), (50, 2), (75, 1), (100, 0)):
            with self.subTest(fatigue=fatigue):
                g = game(crew=(member(fatigue=fatigue),), training=rules(fatigue_recovery_seconds=10 ** 6))
                self.assertIs(g.addTraining("t-atk", "c1"), True)  # full fatigue still trains, for nothing
                g.clock.advance(timedelta(hours=1))
                self.assertIs(g.finishTraining("c1"), True)
                self.assertEqual(crew_record(g)["@AttackImprovement"], str(gain))

    def test_gain_never_takes_total_stats_over_the_training_capacity(self):
        for start, attack, capped in ((16, 4, False), (18, 2, True), (20, 0, True)):
            with self.subTest(start=start):
                g = game(crew=(member(Hp=start),))
                g.addTraining("t-atk", "c1")
                g.clock.advance(timedelta(hours=1))
                self.assertIs(g.finishTraining("c1"), True)
                record = crew_record(g)
                self.assertEqual((int(record["@AttackImprovement"]), sum(stats_of(record).values())),
                                 (attack, min(20, start + 4)))
                entry = g.training.log[-1]
                self.assertEqual((entry["gain"], entry["capped"], entry["capacity"]),
                                 ({"Attack": attack}, capped, 20))

    def test_the_crew_member_must_be_in_a_room_that_trains_the_design(self):
        for room, design in (("r-bridge", "t-atk"), ("r-gym", "t-sci"), ("", "t-atk"), ("r-gym", "t-301")):
            with self.subTest(room=room, design=design):
                g = game(crew=(member(room=room),))
                before = crew_record(g)
                self.assertIs(g.addTraining(design, "c1"), False)
                self.assertEqual(crew_record(g), before)
                self.assertEqual(g.training.log[-1]["code"], "wrong-room")

    def test_an_upgraded_room_still_trains_its_root_designs(self):
        upgraded = game(crew=(member(room="r-gym2"),))
        self.assertIs(upgraded.addTraining("t-atk", "c1"), True)
        g = game()
        self.assertIs(g.addTraining("t-301", "c1"), False)
        g.upgradeRoom("r-gym", "301")
        self.assertIs(g.addTraining("t-301", "c1"), False)  # still upgrading: the room is 300 until the day ends
        g.finish_day()
        self.assertIs(g.addTraining("t-301", "c1"), True)

    def test_a_room_being_upgraded_starts_no_session_until_the_upgrade_completes(self):
        g = game(crew=(member(), member("c2", room="r-gym2")))
        g.addTraining("t-atk", "c2")
        g.upgradeRoom("r-gym", "301")
        g.upgradeRoom("r-gym2", "302")
        before = characters(g)
        self.assertIs(g.addTraining("t-atk", "c1"), False)
        self.assertEqual((characters(g), g.training.log[-1]["code"]), (before, "room-upgrading"))
        self.assertIs(g.addTraining("t-sci", "c1"), False)
        self.assertEqual(g.training.log[-1]["code"], "wrong-room")  # the room would not train it anyway
        g.clock.advance(timedelta(hours=1))
        self.assertIs(g.finishTraining("c2"), True)  # a session already running still finishes
        g.finish_day()
        self.assertIs(g.addTraining("t-atk", "c1"), True)

    def test_log_entries_name_the_room_stat_day_and_a_refusal_code(self):
        g = game()
        g.start_day(2)
        g.addTraining("t-atk", "c1")
        self.assertEqual(g.training.log[-1], {
            "seq": 1, "at": "2027-06-01T06:00:00", "day": 2, "op": "start", "character_id": "c1", "room_id": "r-gym",
            "design_id": "t-atk", "requested_design_id": "t-atk", "stat": "Attack", "accepted": True, "code": "ok",
            "detail": "", "end": "2027-06-01T07:00:00", "gain": {}, "capped": False, "fatigue": 10, "total": 0,
            "capacity": 20})
        g.addTraining("t-sci", "c1")
        refused = g.training.log[-1]
        self.assertEqual({key: refused[key] for key in ("seq", "accepted", "code", "design_id", "stat", "end")},
                         {"seq": 2, "accepted": False, "code": "busy", "design_id": "t-sci", "stat": "Science",
                          "end": None})
        self.assertIn("t-atk", refused["detail"])  # prose for people; tests and benchmarks read the code
        g.addTraining("t-atk", "ghost")
        self.assertEqual({key: g.training.log[-1][key] for key in ("room_id", "fatigue", "total", "capacity", "stat")},
                         {"room_id": None, "fatigue": None, "total": None, "capacity": None, "stat": "Attack"})
        g.clock.advance(timedelta(hours=1))
        g.finishTraining("c1")
        self.assertEqual({key: g.training.log[-1][key] for key in ("op", "design_id", "stat", "gain", "day")},
                         {"op": "finish", "design_id": "t-atk", "stat": "Attack", "gain": {"Attack": 4}, "day": 2})
        g.finishTraining("c1")
        self.assertEqual({key: g.training.log[-1][key] for key in ("code", "design_id", "stat")},
                         {"code": "idle", "design_id": None, "stat": None})

    def test_a_cyclic_upgrade_chain_is_walked_once(self):
        cyclic = DesignData(rooms=DESIGN.rooms + DesignData.from_records(room_designs=[
            {"@RoomDesignId": "700", "@UpgradeFromRoomDesignId": "701"},
            {"@RoomDesignId": "701", "@UpgradeFromRoomDesignId": "700"}]).rooms, research=DESIGN.research)
        state = {"rooms": [{"@RoomId": "r7", "@RoomDesignId": "700"}], "crew": [member(room="r7")]}
        g = SimulatedGame("alpha", state, cyclic, random.Random(0), SimulatedClock(T0), training=RULES)
        self.assertIs(g.addTraining("t-atk", "c1"), False)  # 300 is not in the 700/701 loop, and the walk ends

    def test_required_research_must_be_completed(self):
        for research in ((), ({"@ResearchDesignId": "610", "@ResearchState": "Researching"},),
                         ({"@ResearchDesignId": "611", "@ResearchState": "Completed"},)):
            with self.subTest(research=research):
                g = game(research=research)
                self.assertIs(g.addTraining("t-atk2", "c1"), False)
                self.assertEqual(g.training.log[-1]["code"], "no-research")
        g = game()
        g.addResearch("610")
        g.finish_day()
        self.assertIs(g.addTraining("t-atk2", "c1"), True)

    def test_unknown_crew_or_design_is_refused(self):
        g = game()
        before = crew_record(g)
        for design, cid, code in (("t-atk", "ghost", "unknown-character"), ("nope", "c1", "unknown-design"),
                                  ("nope", "ghost", "unknown-character")):
            with self.subTest(design=design, cid=cid):
                self.assertIs(g.addTraining(design, cid), False)
                self.assertEqual(g.training.log[-1]["code"], code)
        self.assertEqual(crew_record(g), before)
        self.assertEqual([entry["seq"] for entry in g.training.log], [1, 2, 3])

    def test_fatigue_recovers_one_point_per_period_and_stops_at_zero(self):
        g = game(crew=(member(fatigue=30),))
        g.clock.advance(timedelta(seconds=3599))
        self.assertEqual(crew_record(g)["@Fatigue"], "21")
        g.clock.advance(timedelta(seconds=1))
        self.assertEqual(crew_record(g)["@Fatigue"], "20")
        g.clock.advance(timedelta(hours=3))
        self.assertEqual(crew_record(g)["@Fatigue"], "0")

    def test_session_fatigue_is_capped_at_the_maximum(self):
        g = game(crew=(member(fatigue=95),))
        g.addTraining("t-atk", "c1")
        self.assertEqual(crew_record(g)["@Fatigue"], "100")

    def test_reads_and_snapshots_do_not_reset_recovery(self):
        g = game(crew=(member(fatigue=50),), training=rules(fatigue_recovery_seconds=3600))
        for _ in range(50):
            g.clock.advance(timedelta(seconds=1799))
            g.snapshot()  # one more second
            g.listAllCharactersOfUser()
        self.assertEqual(crew_record(g)["@Fatigue"], "25")  # 50 x 1800 s = 25 periods

    def test_fatigue_changes_keep_the_progress_toward_the_next_recovered_point(self):
        # Regression: every fatigue write restarted the recovery period, so a session or an item that adds no
        # fatigue still delayed the next recovered point by up to a whole period.
        hp = {"@ItemDesignId": "905", "@ConsumableStat": "Hp", "@ConsumableGain": "1"}
        block = rules(item_designs=RULES["item_designs"] + [hp])
        items = ({"@ItemId": "i5", "@ItemDesignId": "905", "@Quantity": "1"},
                 {"@ItemId": "i1", "@ItemDesignId": "901", "@Quantity": "1"})
        for action, expected in ((lambda g: None, "9"),
                                 (lambda g: g.addTraining("t-rand", "c1"), "9"),  # a session with no fatigue
                                 (lambda g: g.useConsumable("905", "c1"), "9"),  # an item with no fatigue
                                 (lambda g: g.useConsumable("901", "c1"), "14")):  # +5, then the point is due
            with self.subTest(expected=expected):
                g = game(crew=(member(fatigue=10),), training=block, inventory=items)
                g.clock.advance(timedelta(seconds=359))
                action(g)
                g.clock.advance(timedelta(seconds=1))
                self.assertEqual(crew_record(g)["@Fatigue"], expected)

    def test_recovery_starts_a_fresh_period_once_fatigue_is_back_to_zero(self):
        g = game(crew=(member(fatigue=1),))
        g.clock.advance(timedelta(seconds=360 + 359))  # at zero for 359 s
        g.addTraining("t-atk", "c1")  # +10
        g.clock.advance(timedelta(seconds=1))
        self.assertEqual(crew_record(g)["@Fatigue"], "10")
        g.clock.advance(timedelta(seconds=359))
        self.assertEqual(crew_record(g)["@Fatigue"], "9")


ITEMS = ({"@ItemId": "i1", "@ItemDesignId": "901", "@Quantity": "2"},
         {"@ItemId": "i2", "@ItemDesignId": "950", "@Quantity": "1"},
         {"@ItemId": "i3", "@ItemDesignId": "999", "@Quantity": "4"})  # 999 is not a consumable


def quantity(game_, item_id):
    return next(item["@Quantity"] for item in game_.inventory if item["@ItemId"] == item_id)


def activation(game_):
    return game_.item["ItemService"]["ActivateItem3"]


class ConsumableTests(unittest.TestCase):
    def test_a_stat_consumable_adds_its_stat_and_fatigue_and_uses_one_item(self):
        g = game(inventory=ITEMS)
        self.assertIs(g.useConsumable(901, "c1"), True)
        record = crew_record(g)
        self.assertEqual((record["@AttackImprovement"], record["@Fatigue"]), ("1", "5"))
        self.assertEqual(quantity(g, "i1"), "1")
        self.assertEqual({item.item_design_id: item.quantity for item in g.snapshot().inventory},
                         {"901": 1, "950": 1, "999": 4})
        self.assertEqual(g.calls, ["activateItem3"])
        entry = g.training.log[-1]
        self.assertEqual((entry["op"], entry["design_id"], entry["gain"], entry["capped"], entry["accepted"]),
                         ("consume", "901", {"Attack": 1}, False, True))

    def test_consumables_work_while_training_and_fatigue_stays_capped(self):
        g = game(crew=(member(fatigue=90),), inventory=ITEMS)
        g.addTraining("t-atk", "c1")
        self.assertIs(g.useConsumable("901", "c1"), True)
        record = crew_record(g)
        self.assertEqual((record["@Fatigue"], record["@TrainingDesignId"], record["@AttackImprovement"]),
                         ("100", "t-atk", "1"))

    def test_an_item_out_of_stock_or_missing_is_not_used(self):
        g = game(inventory=({"@ItemId": "i1", "@ItemDesignId": "901", "@Quantity": "0"},))
        self.assertIs(g.useConsumable(901, "c1"), False)
        self.assertEqual(quantity(g, "i1"), "0")
        self.assertEqual(g.calls, ["activateItem3"])
        self.assertIn("out of stock", activation(g)["@errorMessage"])
        self.assertEqual((g.training.log[-1]["code"], g.training.log[-1]["design_id"]), ("out-of-stock", "901"))
        self.assertEqual(crew_record(g)["@AttackImprovement"], "0")
        none = game(inventory=ITEMS)
        self.assertIs(none.useConsumable(902, "c1"), False)
        self.assertEqual(none.calls, [])  # Client.useConsumable returns before calling the server
        self.assertIs(game().useConsumable(901, "c1"), False)  # no items at all

    def test_a_fatigue_consumable_recovers_fatigue_down_to_zero(self):
        for before, after in ((70, "20"), (30, "0")):
            with self.subTest(before=before):
                g = game(crew=(member(fatigue=before),), inventory=ITEMS)
                self.assertIs(g.useConsumable(950, "c1"), True)
                self.assertEqual(crew_record(g)["@Fatigue"], after)
                self.assertEqual(quantity(g, "i2"), "0")
                self.assertEqual(g.training.log[-1]["gain"], {})

    def test_a_stat_consumable_at_capacity_is_used_for_nothing(self):
        g = game(crew=(member(Hp=20),), inventory=ITEMS)
        self.assertIs(g.useConsumable(901, "c1"), True)
        self.assertEqual((quantity(g, "i1"), crew_record(g)["@AttackImprovement"]), ("1", "0"))
        self.assertEqual((g.training.log[-1]["gain"], g.training.log[-1]["capped"]), ({"Attack": 0}, True))

    def test_a_consumable_on_an_unknown_character_keeps_the_item(self):
        g = game(inventory=ITEMS)
        self.assertIs(g.useConsumable(901, "ghost"), False)
        self.assertEqual(quantity(g, "i1"), "2")
        self.assertIn("unknown character", activation(g)["@errorMessage"])
        self.assertEqual((g.training.log[-1]["accepted"], g.training.log[-1]["code"]), (False, "unknown-character"))

    def test_activate_item_responses_have_the_client_shape(self):
        g = game(inventory=ITEMS)
        self.assertIsNone(g.activateItem3(itemId="i1", targetId="c1"))
        self.assertEqual(activation(g), {"Item": dict(ITEMS[0], **{"@Quantity": "1"}), "Character": crew_record(g)})
        for item_id, code, design_id in (("i3", "not-consumable", "999"), ("nope", "unknown-item", None)):
            with self.subTest(item_id=item_id):
                self.assertIsNone(g.activateItem3(itemId=item_id, targetId="ghost"))
                self.assertEqual(list(g.item["ItemService"]["ActivateItem3"]), ["@errorMessage"])
                self.assertEqual((g.training.log[-1]["code"], g.training.log[-1]["design_id"]), (code, design_id))
        self.assertEqual(quantity(g, "i3"), "4")
        # Every item use that reaches the server is logged; item checks come before the crew member's.
        self.assertEqual([entry["code"] for entry in g.training.log], ["ok", "not-consumable", "unknown-item"])


REJECT = {"kind": "reject", "method": "addTraining"}
CONTRADICT = {"kind": "contradicted-effect", "method": "addTraining", "training_design_id": "t-atk2"}


def finished(game_, cid="c1"):
    game_.addTraining("t-atk", cid)
    game_.clock.advance(timedelta(hours=1))
    return game_


class TrainingFaultTests(unittest.TestCase):
    def test_training_faults_are_validated(self):
        for fault, message in (
            (dict(REJECT, method="upgradeRoom"), "applies to addTraining, finishTraining or activateItem3"),
            (dict(REJECT, method="snapshot"), "applies to addTraining, finishTraining or activateItem3"),
            (dict(CONTRADICT, method="finishTraining"), "applies to addTraining only"),
            ({"kind": "contradicted-effect", "method": "addTraining"}, "needs the training_design_id"),
            (dict(CONTRADICT, training_design_id=""), "needs the training_design_id"),
            (dict(CONTRADICT, training_design_id=7), "needs the training_design_id"),
            ({"kind": "raise", "method": "addTraining", "training_design_id": "t"},
             "inapplicable keys: training_design_id"),
            (dict(REJECT, effect=False), "inapplicable keys: effect"),
            (dict(REJECT, kind="explode"), "unknown fault kind"),
        ):
            with self.subTest(fault=fault), self.assertRaisesRegex(ValueError, message):
                Fault.from_dict(fault)
        for method in ("addTraining", "finishTraining", "activateItem3"):
            self.assertEqual(Fault.from_dict(dict(REJECT, method=method, day=1)), Fault("reject", method, day=1))
            self.assertEqual(Fault.from_dict({"kind": "raise", "method": method}).method, method)
        self.assertEqual(Fault.from_dict(CONTRADICT).training_design_id, "t-atk2")

    def test_an_unknown_substitute_design_is_refused_when_the_game_is_built(self):
        with self.assertRaisesRegex(ValueError, "unknown training design 'nope'"):
            game(faults=(dict(CONTRADICT, training_design_id="nope"),))

    def test_rejected_start_changes_nothing(self):
        g = game(faults=(dict(REJECT, call=1),))
        before = crew_record(g)
        self.assertIs(g.addTraining("t-atk", "c1"), False)
        self.assertEqual(g.fired, ["day 0 addTraining #1: reject"])
        self.assertEqual((g.calls, crew_record(g), g.training.log), (["addTraining"], before, []))
        self.assertIs(g.addTraining("t-atk", "c1"), True)  # only the first call was rejected

    def test_rejected_finish_keeps_the_session(self):
        g = finished(game(faults=({"kind": "reject", "method": "finishTraining"},)))
        self.assertIs(g.finishTraining("c1"), False)
        record = crew_record(g)
        self.assertEqual((record["@TrainingDesignId"], record["@AttackImprovement"]), ("t-atk", "0"))
        self.assertFalse(hasattr(g, "trainingFinish"))

    def test_rejected_item_is_kept(self):
        g = game(inventory=ITEMS, faults=({"kind": "reject", "method": "activateItem3"},))
        self.assertIs(g.useConsumable(901, "c1"), False)
        self.assertEqual(quantity(g, "i1"), "2")
        self.assertIn("@errorMessage", activation(g))
        self.assertEqual((crew_record(g)["@AttackImprovement"], g.training.log), ("0", []))

    def test_raise_and_ambiguous_response_honour_the_effect_flag(self):
        g = game(faults=({"kind": "raise", "method": "addTraining", "effect": False},))
        with self.assertRaises(SimulatedFaultError):
            g.addTraining("t-atk", "c1")
        self.assertEqual(crew_record(g)["@TrainingDesignId"], "0")
        g = game(faults=({"kind": "raise", "method": "addTraining"},))
        with self.assertRaises(SimulatedFaultError):
            g.addTraining("t-atk", "c1")
        self.assertEqual(crew_record(g)["@TrainingDesignId"], "t-atk")
        g = finished(game(faults=({"kind": "ambiguous-response", "method": "finishTraining"},)))
        self.assertIsNone(g.finishTraining("c1"))
        self.assertEqual(crew_record(g)["@AttackImprovement"], "4")
        g = game(inventory=ITEMS, faults=({"kind": "ambiguous-response", "method": "activateItem3"},))
        self.assertIs(g.useConsumable(901, "c1"), True)
        self.assertEqual(quantity(g, "i1"), "1")
        g = game(inventory=ITEMS, faults=({"kind": "raise", "method": "activateItem3", "effect": False},))
        with self.assertRaises(SimulatedFaultError):
            g.useConsumable(901, "c1")
        self.assertEqual(quantity(g, "i1"), "2")
        g = game(inventory=ITEMS, faults=({"kind": "ambiguous-response", "method": "activateItem3", "effect": False},))
        self.assertIs(g.useConsumable(901, "c1"), True)  # no reply, so no error: Client.useConsumable says True
        self.assertEqual((quantity(g, "i1"), g.item), ("2", None))

    def test_contradicted_effect_starts_the_substitute_design(self):
        g = game(faults=(CONTRADICT,))
        self.assertIs(g.addTraining("t-atk", "c1"), True)
        record = crew_record(g)
        self.assertEqual((record["@TrainingDesignId"], record["@TrainingEndDate"], record["@Fatigue"]),
                         ("t-atk2", "2027-06-01T10:00:00", "25"))  # the substitute's duration and fatigue
        entry = g.training.log[-1]
        self.assertEqual((entry["design_id"], entry["requested_design_id"]), ("t-atk2", "t-atk"))
        bridge = game(crew=(member(room="r-bridge"),), faults=(CONTRADICT,))
        self.assertIs(bridge.addTraining("t-atk", "c1"), False)  # a request the server refuses stays refused
        self.assertEqual((crew_record(bridge)["@TrainingDesignId"], bridge.fired),
                         ("0", ["day 0 addTraining #1: contradicted-effect"]))

    def test_a_substitute_equal_to_the_request_is_no_contradiction(self):
        # The fault fires, but the server started what was asked: the runner verifies it and the log agrees.
        g = game(faults=(dict(CONTRADICT, training_design_id="t-atk"),))
        with tempfile.TemporaryDirectory() as tmp:
            runner = ActionRunner("alpha", g, g.snapshot, GameplayJournal(Path(tmp) / "alpha.jsonl"), clock=g.clock)
            result = runner.run("add_training", {"training_design_id": "t-atk", "character_id": "c1"})
        self.assertEqual(result.outcome, "verified")
        self.assertEqual(g.fired, ["day 0 addTraining #1: contradicted-effect"])
        entry = g.training.log[-1]
        self.assertEqual((entry["design_id"], entry["requested_design_id"], entry["end"]),
                         ("t-atk", "t-atk", "2027-06-01T07:00:02"))

    def test_the_action_runner_reconciles_each_fault(self):
        params = {"training_design_id": "t-atk", "character_id": "c1"}
        for faults, outcome, response_ok, error_class in (
            ((), "verified", True, None),
            ((CONTRADICT,), "contradicted", True, None),
            ((REJECT,), "not-observed", False, None),
            (({"kind": "raise", "method": "addTraining"},), "verified", None, "SimulatedFaultError"),
            (({"kind": "snapshot-failure", "method": "addTraining"},), "unknown", True, "SimulatedFaultError"),
        ):
            with self.subTest(faults=faults), tempfile.TemporaryDirectory() as tmp:
                g = game(faults=faults)
                runner = ActionRunner("alpha", g, g.snapshot, GameplayJournal(Path(tmp) / "alpha.jsonl"),
                                      clock=g.clock)
                result = runner.run("add_training", params)
                self.assertEqual((result.outcome, result.event.response_ok, result.event.error_class),
                                 (outcome, response_ok, error_class))


def random_sessions(training_rng=None, sessions=20, alias="alpha"):
    g = game(crew=(member(design="cd-big"),), training_rng=training_rng, alias=alias)
    for _ in range(sessions):
        g.addTraining("t-rand", "c1")
        g.clock.advance(timedelta(hours=1))
        g.finishTraining("c1")
    gains = [entry["gain"]["Pilot"] for entry in g.training.log if entry["op"] == "finish"]
    return gains, json.dumps({"crew": characters(g), "log": g.training.log}, sort_keys=True)


class RandomnessTests(unittest.TestCase):
    def test_training_never_draws_from_the_shared_generator(self):
        shared = random.Random(5)
        g = game(inventory=ITEMS, rng=shared)
        before = shared.getstate()
        g.addTraining("t-rand", "c1")
        g.clock.advance(timedelta(hours=1))
        g.finishTraining("c1")
        g.useConsumable(901, "c1")
        g.snapshot()
        g.listAllCharactersOfUser()
        self.assertEqual(shared.getstate(), before)

    def test_gains_follow_the_training_seed(self):
        gains, blob = random_sessions(random.Random("1:alpha:training"))
        self.assertEqual(random_sessions(random.Random("1:alpha:training")), (gains, blob))
        self.assertNotEqual(random_sessions(random.Random("2:alpha:training"))[0], gains)
        self.assertGreater(len(set(gains)), 1)  # 1-10 gains really vary
        self.assertTrue(all(1 <= gain <= 10 for gain in gains))

    def test_the_default_training_generator_is_seeded_from_the_alias(self):
        self.assertEqual(random_sessions(), random_sessions())
        self.assertNotEqual(random_sessions()[0], random_sessions(alias="bravo")[0])


class ReviewRegressionTests(unittest.TestCase):
    """Defects found in code review, each pinned before its fix."""

    def test_a_single_record_reads_as_one_mapping_like_xmltodict(self):
        # xmltodict (the Client's parser) gives a lone child element as a mapping, not a one-item list.
        item = {"@ItemId": "i1", "@ItemDesignId": "901", "@Quantity": "2"}
        g = game(inventory=(item,), training=rules(character_designs=RULES["character_designs"][:1]))
        g.listAllCharactersOfUser()
        lone = g.allCharactersOfUser["CharacterService"]["ListAllCharactersOfUser"]["Characters"]["Character"]
        self.assertIsInstance(lone, dict)
        self.assertEqual(lone["@CharacterId"], "c1")
        g.listItemsOfAShip()
        self.assertEqual(g.itemsOfAShip, {"ItemService": {"ListItemsOfAShip": {"Items": {"Item": item}}}})
        g.listAllCharacterDesigns2()
        self.assertEqual(g.allCharacterDesigns["CharacterService"]["ListAllCharacterDesigns"]["CharacterDesigns"],
                         {"CharacterDesign": RULES["character_designs"][0]})
        self.assertIs(g.useConsumable("901", "c1"), True)  # the lone item is still found
        self.assertEqual(quantity(g, "i1"), "1")
        provided = ClientSnapshotProvider(g, "alpha", clock=lambda: T0)()
        self.assertEqual([crew.character_id for crew in provided.crew], ["c1"])

    def test_a_naive_scenario_start_is_read_as_utc(self):
        # A start without an offset made finishTraining raise TypeError (naive against aware), and @TrainingEndDate
        # followed the machine's timezone.
        if not hasattr(time, "tzset"):
            self.skipTest("time.tzset is not available")
        previous = os.environ.get("TZ")

        def restore():
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        state = {"rooms": [dict(room) for room in ROOMS], "crew": [member()]}
        g = SimulatedGame("alpha", state, DESIGN, random.Random(0), SimulatedClock(T0.replace(tzinfo=None)),
                          training=RULES)
        self.assertIs(g.addTraining("t-atk", "c1"), True)
        self.assertEqual(crew_record(g)["@TrainingEndDate"], "2027-06-01T07:00:00")
        self.assertEqual(g.training.log[-1]["at"], "2027-06-01T06:00:00")
        g.clock.advance(timedelta(seconds=3599))
        self.assertIs(g.finishTraining("c1"), False)
        g.clock.advance(timedelta(seconds=1))
        self.assertIs(g.finishTraining("c1"), True)

    def test_a_research_requirement_of_zero_or_empty_means_none(self):
        # Live records say "no research" with "0" (or ""); they were refused as an unknown research design.
        for required in ("0", ""):
            with self.subTest(required=required):
                free = dict(training_design("t-free", "Attack", "300", 3600, 0, 1, 1), **{
                    "@RequiredResearchDesignId": required})
                g = game(training=rules(training_designs=RULES["training_designs"] + [free]))
                self.assertIs(g.addTraining("t-free", "c1"), True)

    def test_ids_given_as_json_numbers_still_match(self):
        # Numeric ids passed validation (which reads them through str) and then never matched a lookup.
        rooms = ({"@RoomId": 5, "@RoomDesignId": 301, "@RoomStatus": "Normal"},)
        research = ({"@ResearchDesignId": 610, "@ResearchState": "Completed"},)
        inventory = ({"@ItemId": 7, "@ItemDesignId": 901, "@Quantity": "1"},)
        g = game(crew=(member(room=5),), rooms=rooms, research=research, inventory=inventory)
        self.assertIs(g.useConsumable("901", "c1"), True)
        self.assertEqual(g.inventory[0]["@Quantity"], "0")
        self.assertIs(g.addTraining("t-atk2", "c1"), True)  # room 301 is an upgrade of 300; research 610 done

    def test_inventory_is_not_checked_without_a_training_block(self):
        # The new item checks broke scenarios that never use consumables.
        loose = ({"@ItemId": "i1", "@ItemDesignId": "1", "@Quantity": "-1"},
                 {"@ItemId": "i1", "@ItemDesignId": "2"})
        g = game(crew=(), training=None, inventory=loose)
        self.assertEqual(len(g.inventory), 2)


# The property loop's own model of the rules, written from the design above, not from the implementation.
LINEAGE = {"300": {"300"}, "301": {"301", "300"}, "302": {"302", "301", "300"}, "400": {"400"}, "500": {"500"}}
DESIGNS = {record["@TrainingDesignId"]: record for record in RULES["training_designs"]}
UPGRADE_PRICE = {"301": 100, "302": 0}  # minerals, from DESIGN
CAPACITY, FATIGUE_MAX = 20, 100
TIME = "%Y-%m-%dT%H:%M:%S"
START_CODES = {"ok", "unknown-character", "unknown-design", "busy", "wrong-room", "room-upgrading", "no-research"}
FINISH_CODES = {"ok", "unknown-character", "idle", "early"}
CONSUME_CODES = {"ok", "not-consumable", "out-of-stock", "unknown-character"}
TRAINING_CALLS = ("addTraining", "finishTraining", "activateItem3")
ROOM_IDS = tuple(room["@RoomId"] for room in ROOMS)


def random_game(rnd, seed):
    crew = []
    for index in range(rnd.randint(1, 3)):
        stats = {stat: 0 for stat in STATS}
        for _ in range(rnd.randint(0, CAPACITY)):
            stats[rnd.choice(STATS)] += 1
        record = member(f"c{index + 1}", room=rnd.choice(sorted(ROOM_IDS) + [""]), fatigue=rnd.randint(0, 100),
                        **stats)
        if rnd.random() < 0.3:
            record["@TrainingDesignId"] = rnd.choice(sorted(DESIGNS))
            record["@TrainingEndDate"] = (T0 + timedelta(seconds=rnd.randint(0, 4 * 3600))).strftime(TIME)
        crew.append(record)
    research = [{"@ResearchDesignId": "610", "@ResearchState": rnd.choice(("Completed", "Researching"))}]
    inventory = [{"@ItemId": f"i{design}", "@ItemDesignId": design, "@Quantity": str(rnd.randint(0, 2))}
                 for design in ("901", "950", "999")]
    # Rejects are listed before the substitute, so when both fire the reject decides, as the first listed does.
    faults = ([{"kind": "raise", "method": method, "effect": False, "probability": 0.05} for method in TRAINING_CALLS]
              + [{"kind": "reject", "method": method, "probability": 0.1} for method in TRAINING_CALLS]
              + [{"kind": "contradicted-effect", "method": "addTraining", "probability": 0.2,
                  "training_design_id": rnd.choice(sorted(DESIGNS))}])
    return game(crew=crew, research=research[:rnd.randint(0, 1)], inventory=inventory, faults=faults,
                training_rng=random.Random(f"{seed}:training"), fault_rng=random.Random(f"{seed}:faults"))


class PropertyTests(unittest.TestCase):
    def test_random_operation_sequences_keep_every_invariant(self):
        seen = {"accepted": collections.Counter(), "codes": collections.Counter(), "faults": collections.Counter()}
        for seed in range(300):
            rnd = random.Random(seed)
            g = random_game(rnd, seed)
            ids = sorted(characters(g)) + ["ghost"]
            model = {"at_start": {cid: int(record["@Fatigue"]) for cid, record in characters(g).items()},
                     "targets": {}}
            for step in range(rnd.randint(10, 40)):
                with self.subTest(seed=seed, step=step):
                    self.check_one_operation(g, rnd, ids, model, seen)
        # The loop is not vacuous: every op is accepted often, and every refusal and fault turns up.
        self.assertTrue(all(seen["accepted"][op] >= 100 for op in ("start", "finish", "consume", "upgrade")), seen)
        self.assertEqual({code for code, count in seen["codes"].items() if count >= 10},
                         START_CODES | FINISH_CODES | CONSUME_CODES, seen)
        self.assertTrue(all(seen["faults"][kind] >= 20 for kind in ("raise", "reject", "contradicted-effect")), seen)
        self.assertGreaterEqual(seen["accepted"]["day"], 300)

    def check_one_operation(self, g, rnd, ids, model, seen):
        before, items = characters(g), {item["@ItemId"]: int(item["@Quantity"]) for item in g.inventory}
        rooms = {room["@RoomId"]: dict(room) for room in g.rooms}
        logged, fired, minerals = len(g.training.log), len(g.fired), g.minerals
        completed = any(e["@ResearchDesignId"] == "610" and e["@ResearchState"] == "Completed" for e in g.research)
        op, cid = rnd.choice(("start", "start", "finish", "finish", "consume", "advance", "snapshot", "upgrade",
                              "day")), rnd.choice(ids)
        old = before.get(cid)
        code, call, design_id = "ok", None, None
        if op == "start":
            design_id = rnd.choice(sorted(DESIGNS) + ["nope"])
            design, room = DESIGNS.get(design_id), rooms.get(old and old["@RoomId"], {})
            code = ("unknown-character" if old is None else "unknown-design" if design is None
                    else "busy" if old["@TrainingDesignId"] != "0"
                    else "wrong-room" if design["@RequiredRoomDesignId"] not in LINEAGE.get(room.get("@RoomDesignId"), ())
                    else "room-upgrading" if room["@RoomStatus"] == "Upgrading"
                    else "no-research" if "@RequiredResearchDesignId" in design and not completed else "ok")
            call = partial(g.addTraining, design_id, cid)
        elif op == "finish":
            code = ("unknown-character" if old is None else "idle" if old["@TrainingDesignId"] == "0"
                    else "early" if g.clock.now < datetime.strptime(old["@TrainingEndDate"], TIME).replace(
                        tzinfo=timezone.utc) else "ok")
            call = partial(g.finishTraining, cid)
        elif op == "consume":
            design_id = rnd.choice(("901", "950", "999", "902"))  # 902: no such item, so no server call at all
            code = ("no-item" if design_id == "902" else "not-consumable" if design_id == "999" else "out-of-stock" if items.get(f"i{design_id}") == 0
                    else "unknown-character" if old is None else "ok")
            call = partial(g.useConsumable, design_id, cid)
        elif op == "advance":
            # Exact session lengths (and a second either side) land on the end to the second.
            g.clock.advance(timedelta(seconds=rnd.choice((rnd.randint(0, 6 * 3600), 3599, 3600, 3601, 14400, 1))))
        elif op == "snapshot":
            for crew in g.snapshot().crew:
                self.assertEqual(crew.training_active, characters(g)[crew.character_id]["@TrainingDesignId"] != "0")
        elif op == "upgrade":
            room_id, target = rnd.choice(ROOM_IDS), rnd.choice(sorted(UPGRADE_PRICE))
            g.upgradeRoom(room_id, target)
            starts = rooms[room_id]["@RoomStatus"] == "Normal" and minerals >= UPGRADE_PRICE[target]
            if starts:
                model["targets"][room_id] = target
            self.assertEqual(g.rooms[ROOM_IDS.index(room_id)]["@RoomStatus"],
                             "Upgrading" if starts else rooms[room_id]["@RoomStatus"])
            self.assertEqual(g.minerals, minerals - UPGRADE_PRICE[target] * starts)
            seen["accepted"]["upgrade"] += starts
        else:  # a day boundary: upgrades complete, research completes, and the day's start is recorded
            day = g.day + 1
            g.clock.start_day(day)
            g.start_day(day)
            targets, model["targets"] = model["targets"], {}
            self.assertEqual({room["@RoomId"]: (room["@RoomDesignId"], room["@RoomStatus"]) for room in g.rooms},
                             {room_id: (targets.get(room_id, room["@RoomDesignId"]), "Normal")
                              for room_id, room in rooms.items()})
            self.assertTrue(all(entry["@ResearchState"] == "Completed" for entry in g.research))
            self.assertEqual((g.days[-1]["day"], list(g.days[-1]["crew"])), (day, list(characters(g).values())))
            seen["accepted"]["day"] += 1
        result = None
        if call is not None:
            try:
                result = call()
            except SimulatedFaultError:
                result = "raised"
        kinds = [line.rsplit(": ", 1)[1] for line in g.fired[fired:]]
        seen["faults"].update(kinds)
        blocked = "raise" in kinds or "reject" in kinds
        self.assertEqual(result, None if call is None else "raised" if "raise" in kinds else code == "ok" and not blocked)
        effective = call is not None and code == "ok" and not blocked
        substitute = next(fault.training_design_id for fault in g.faults if fault.kind == "contradicted-effect")
        started = substitute if op == "start" and "contradicted-effect" in kinds else design_id
        seen["accepted"][op] += effective
        # Every call that reaches the server logs one entry; one a fault blocks, or no call at all, logs none.
        reached = call is not None and not blocked and design_id != "902"
        self.assertEqual(len(g.training.log), logged + reached)
        after = characters(g)
        if reached:
            seen["codes"][code] += 1
            self.check_entry(g.training.log[-1], logged, op, cid, code, design_id, started, before, after, g)
        self.assertEqual(set(after), set(before))
        for other, record in after.items():
            stats, old_stats = stats_of(record), stats_of(before[other])
            fatigue, old_fatigue = int(record["@Fatigue"]), int(before[other]["@Fatigue"])
            self.assertLessEqual(sum(stats.values()), CAPACITY)
            self.assertTrue(0 <= fatigue <= FATIGUE_MAX)
            changed = {stat for stat in STATS if stats[stat] != old_stats[stat]}
            if other != cid or not effective:
                self.assertEqual(changed, set())
                self.assertLessEqual(fatigue, old_fatigue)  # only recovery happens to everyone else
                self.assertEqual(record["@TrainingDesignId"], before[other]["@TrainingDesignId"])
                continue
            room = CAPACITY - sum(old_stats.values())
            entry = g.training.log[-1]
            if op == "start":
                design = DESIGNS[started]
                self.assertEqual(fatigue, min(FATIGUE_MAX, old_fatigue + int(design["@Fatigue"])))
                self.assertEqual(changed, set())
                self.assertEqual((record["@TrainingDesignId"], record["@TrainingEndDate"]),
                                 (started, (g.clock.now + timedelta(seconds=int(design["@Duration"]))).strftime(TIME)))
                model["at_start"][cid] = old_fatigue
            elif op == "finish":
                design = DESIGNS[before[cid]["@TrainingDesignId"]]
                stat, scale = design["@TrainingStat"], FATIGUE_MAX - model["at_start"][cid]
                low, high = (int(design[key]) * scale // FATIGUE_MAX for key in ("@MinGain", "@MaxGain"))
                gained = stats[stat] - old_stats[stat]
                self.assertTrue(changed <= {stat})
                self.assertTrue(min(low, room) <= gained <= min(high, room))
                # Capped exactly when the capacity cut the gain: it then fills the capacity, below the gain due.
                if low == high:
                    self.assertEqual(entry["capped"], room < low)
                else:
                    self.assertTrue(not entry["capped"] or gained == room < high)
                self.assertEqual((record["@TrainingDesignId"], record["@TrainingEndDate"], fatigue),
                                 ("0", "", old_fatigue))
            elif design_id == "901":
                self.assertTrue(changed <= {"Attack"})
                self.assertEqual(stats["Attack"] - old_stats["Attack"], min(1, room))
                self.assertEqual(entry["capped"], room < 1)
                self.assertEqual(fatigue, min(FATIGUE_MAX, old_fatigue + 5))
            else:
                self.assertEqual((changed, fatigue, entry["capped"]), (set(), max(0, old_fatigue - 50), False))
        quantities = {item["@ItemId"]: int(item["@Quantity"]) for item in g.inventory}
        used = {f"i{design_id}"} if op == "consume" and effective else set()
        self.assertEqual(quantities, {key: value - (key in used) for key, value in items.items()})

    def check_entry(self, entry, logged, op, cid, code, design_id, started, before, after, g):
        """The log entry of one call that reached the server agrees with the call and the record read after it."""
        known = cid in after
        self.assertEqual((entry["seq"], entry["op"], entry["character_id"], entry["day"], entry["code"],
                          entry["accepted"]), (logged + 1, op, cid, g.day, code,
                                               code == "ok"))
        self.assertEqual(entry["at"], g.clock.now.strftime(TIME))
        self.assertEqual((entry["room_id"], entry["fatigue"], entry["total"]),
                         (before[cid]["@RoomId"], int(after[cid]["@Fatigue"]), sum(stats_of(after[cid]).values()))
                         if known else (None, None, None))
        if op == "start":
            self.assertEqual((entry["design_id"], entry["requested_design_id"]),
                             (started if code == "ok" else design_id, design_id))
        elif op == "finish":  # the session finished, or the one still running
            self.assertEqual(entry["design_id"], {"0": None}.get(before[cid]["@TrainingDesignId"],
                                                                 before[cid]["@TrainingDesignId"]) if known else None)
        else:
            self.assertEqual(entry["design_id"], design_id)
        delta = {stat: value - stats_of(before[cid])[stat] for stat, value in stats_of(after[cid]).items()} \
            if known else {}
        if code == "ok" and entry["stat"] is not None and op != "start":
            self.assertEqual(entry["gain"], {entry["stat"]: delta[entry["stat"]]})
        else:
            self.assertEqual((entry["gain"], entry["capped"]), ({}, False))
            self.assertEqual(set(delta.values()) - {0}, set())


def run_training(scenario):
    with tempfile.TemporaryDirectory() as tmp:
        return run_scenario(scenario, tmp)


def economy(result):
    return result.progression_json, result.fleet_json, result.audit_json, result.calls, result.faults


class TrainIdle:
    """A stand-in training policy: plan each listed crew member's design whenever they have no session."""

    name = "test-train-idle"

    def __init__(self, designs):
        self.designs = designs

    def plan(self, snapshot, design, config):
        requests = tuple(ActionRequest("add_training", {"training_design_id": self.designs[crew.character_id],
                                                        "character_id": crew.character_id}, "idle crew member")
                         for crew in snapshot.crew if crew.character_id in self.designs and not crew.training_active)
        return Plan(self.name, snapshot.account_alias, snapshot.observed_at, requests, ())


def run_with_policy(name, designs):
    """Run a shipped scenario with every account on ``TrainIdle(designs)``; return the result and alpha's journal."""
    scenario = load_scenario(name)
    for account in scenario["registry"]["accounts"]:
        account["policy_id"] = TrainIdle.name
    with patch.dict(POLICIES, {TrainIdle.name: TrainIdle(designs)}), tempfile.TemporaryDirectory() as tmp:
        result = run_scenario(scenario, tmp)
        return result, GameplayJournal(Path(tmp) / "alpha.jsonl").read()


class TrainingScenarioTests(unittest.TestCase):
    def test_crew_recovers_across_days_and_nothing_trains_without_a_policy(self):
        scenario = load_scenario("crew-training-single-account")
        result = run_training(scenario)
        for method in ("addTraining", "finishTraining", "activateItem3"):
            self.assertNotIn(method, result.calls["alpha"])
        crew = {record["@CharacterId"]: record for record in result.crew["alpha"]}
        self.assertEqual(sorted(crew), ["101", "102", "103"])
        idle, training = crew["101"], crew["102"]
        self.assertEqual((idle["@Fatigue"], idle["@TrainingDesignId"]), ("16", "0"))  # 40 - 48 h / 2 h
        self.assertEqual((training["@Fatigue"], training["@TrainingDesignId"], training["@TrainingEndDate"]),
                         ("0", "t-acad-1", "2027-06-01T08:00:00"))  # past its end, never finished for it
        for record in scenario["accounts"]["alpha"]["crew"]:
            self.assertEqual(stats_of(crew[record["@CharacterId"]]), stats_of(dict(
                {f"@{stat}Improvement": "0" for stat in STATS}, **record)))
        self.assertEqual(result.training_log, {"alpha": ()})

    def test_the_result_records_the_run_span_and_each_day_start(self):
        # What a benchmark needs from ScenarioResult alone: the span, and per day the crew and the training rooms.
        result = run_training(load_scenario("crew-training-single-account"))
        self.assertEqual((result.started_at, result.ended_at[:11]), ("2027-06-01T06:00:00", "2027-06-03T"))
        self.assertGreater(result.ended_at, "2027-06-03T06:00:00")
        days = result.days["alpha"]
        self.assertEqual([(day["day"], day["at"]) for day in days],
                         [(0, "2027-06-01T06:00:00"), (1, "2027-06-02T06:00:00"), (2, "2027-06-03T06:00:00")])
        fatigue = [{record["@CharacterId"]: record["@Fatigue"] for record in day["crew"]} for day in days]
        self.assertEqual(fatigue, [{"101": "40", "102": "20", "103": "0"}, {"101": "28", "102": "8", "103": "0"},
                                   {"101": "16", "102": "0", "103": "0"}])  # one point per 2 h
        self.assertEqual({day["day"]: day["training_rooms"] for day in days},
                         dict.fromkeys((0, 1, 2), {"r-gym": ("t-gym-1", "t-gym-2"), "r-acad": ("t-acad-1",)}))
        scenario_crew = load_scenario("crew-training-single-account")["accounts"]["alpha"]["crew"]
        self.assertEqual(result.initial_crew, {"alpha": days[0]["crew"]})
        self.assertEqual([dict(record, **{"@Fatigue": scenario["@Fatigue"]})
                          for record, scenario in zip(result.initial_crew["alpha"], scenario_crew)],
                         list(result.initial_crew["alpha"]))
        self.assertEqual(stats_of(result.initial_crew["alpha"][0]), dict.fromkeys(STATS, 0) | {"Attack": 5})
        self.assertEqual(json.loads(result.training_json), json.loads(json.dumps({
            "started_at": result.started_at, "ended_at": result.ended_at, "days": result.days, "crew": result.crew,
            "log": result.training_log})))

    def test_a_day_start_reads_the_crew_without_moving_the_clock(self):
        g = game(crew=(member(fatigue=30),))
        g.upgradeRoom("r-acad", "302")  # an unpriced upgrade, so a 400 room becomes a gym at the boundary
        g.clock.advance(timedelta(hours=1))
        now = g.clock.now
        g.start_day(1)
        self.assertEqual(g.clock.now, now)
        self.assertEqual(g.days, [{"day": 1, "at": "2027-06-01T07:00:00", "crew": (crew_record(g),),
                                   "training_rooms": {"r-gym": ("t-atk", "t-atk2", "t-rand"),
                                                      "r-gym2": ("t-atk", "t-atk2", "t-301", "t-rand"),
                                                      "r-acad": ("t-atk", "t-atk2", "t-301", "t-rand")}}])
        self.assertEqual(g.days[0]["crew"][0]["@Fatigue"], "20")

    def test_training_by_a_policy_reaches_the_journal_and_the_scenario_result(self):
        # No shipped policy trains yet; TrainIdle stands in for one through the real orchestrator, runner and journal.
        result, journal = run_with_policy("crew-training-single-account", {"101": "t-gym-1"})
        (entry,) = result.training_log["alpha"]
        self.assertEqual({key: entry[key] for key in ("op", "design_id", "accepted", "code", "day", "room_id")},
                         {"op": "start", "design_id": "t-gym-1", "accepted": True, "code": "ok", "day": 0,
                          "room_id": "r-gym"})
        self.assertEqual(entry["end"], (datetime.strptime(entry["at"], TIME) + timedelta(hours=1)).strftime(TIME))
        self.assertEqual(json.loads(result.training_json)["log"], {"alpha": [entry]})
        self.assertEqual([(event.action_id, event.outcome) for event in journal if event.action_id == "add_training"],
                         [("add_training", "verified")])
        self.assertEqual(result.calls["alpha"].count("addTraining"), 1)  # day 1: an unfinished session is not idle
        self.assertEqual([day["crew"][0]["@TrainingDesignId"] for day in result.days["alpha"]],
                         ["0", "t-gym-1", "t-gym-1"])
        self.assertEqual(result.initial_crew["alpha"][0]["@TrainingDesignId"], "0")

    def test_training_faults_fire_through_a_policy_run(self):
        result, journal = run_with_policy("crew-training-faults", {"101": "t-gym-1", "102": "t-acad-1"})
        self.assertEqual(result.faults, {"alpha": ("day 0 addTraining #1: contradicted-effect",)})
        self.assertEqual([(event.params["character_id"], event.outcome) for event in journal
                          if event.action_id == "add_training"], [("101", "contradicted"), ("102", "verified")])
        self.assertEqual([(entry["day"], entry["character_id"], entry["requested_design_id"], entry["design_id"])
                          for entry in result.training_log["alpha"]],
                         [(0, "101", "t-gym-1", "t-acad-1"), (1, "102", "t-acad-1", "t-acad-1")])

    def test_the_training_block_has_documented_defaults(self):
        g = game(training={"character_designs": RULES["character_designs"]})
        self.assertEqual((g.rules.fatigue_max, g.rules.recovery_seconds, dict(g.rules.trainings)), (100, 360, {}))

    def test_crew_does_not_change_the_economy(self):
        scenario = load_scenario("crew-training-single-account")
        bare = copy.deepcopy(scenario)
        del bare["training"]
        for state in bare["accounts"].values():
            del state["crew"]
        self.assertEqual(economy(run_training(scenario)), economy(run_training(bare)))

    def test_malformed_training_data_is_refused_before_anything_runs(self):
        def crew(index, **changes):
            return lambda s: s["accounts"]["alpha"]["crew"][index].update(changes)

        def setting(key, value):
            return lambda s: s["training"].update({key: value})

        def design(key, index, **changes):
            return lambda s: s["training"][key][index].update(changes)

        def remove(*path):
            def change(s):
                target = s
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
            return change

        inventory = ("accounts", "alpha", "inventory")
        cases = (
            (setting("typo", 1), "training block has unknown keys: typo"),
            (setting("fatigue_max", 0), "fatigue_max must be an integer >= 1"),
            (setting("fatigue_recovery_seconds", "x"), "fatigue_recovery_seconds must be an integer >= 1"),
            (remove("training"), "account alpha has crew but the scenario has no training block"),
            (remove("accounts", "alpha", "crew", 0, "@CharacterId"), "account alpha: a crew record has no @CharacterId"),
            (crew(1, **{"@CharacterId": "101"}), "account alpha: duplicate @CharacterId 101"),
            (crew(0, **{"@CharacterDesignId": "cd-x"}), "crew 101: unknown @CharacterDesignId 'cd-x'"),
            (crew(0, **{"@AttackImprovement": "x"}), "crew 101: @AttackImprovement must be an integer >= 0"),
            (crew(0, **{"@AttackImprovement": -1}), "@AttackImprovement must be an integer >= 0, not '-1'"),
            (crew(0, **{"@AttackImprovement": True}), "@AttackImprovement must be an integer >= 0"),
            (crew(0, **{"@AttackImprovement": 2.5}), "@AttackImprovement must be an integer >= 0"),
            (crew(0, **{"@AtackImprovement": "1"}), "crew 101: unknown keys: @AtackImprovement"),
            (crew(0, **{"@HpImprovement": "46"}), "crew 101: stats total 51 is over the training capacity 50"),
            (crew(0, **{"@RoomId": "r9"}), "crew 101: unknown @RoomId 'r9'"),
            (crew(0, **{"@Fatigue": "101"}), "crew 101: @Fatigue 101 is above the maximum 100"),
            (crew(0, **{"@Fatigue": "-1"}), "crew 101: @Fatigue must be an integer >= 0"),
            (crew(1, **{"@TrainingDesignId": "t-x"}), "crew 102: unknown @TrainingDesignId 't-x'"),
            (crew(1, **{"@TrainingEndDate": "tomorrow"}), "crew 102: @TrainingEndDate 'tomorrow' does not fit"),
            (crew(1, **{"@TrainingEndDate": ""}), "crew 102: @TrainingEndDate '' does not fit"),
            (crew(0, **{"@TrainingEndDate": "2027-06-01T08:00:00"}), "crew 101: @TrainingEndDate '2027-06-01T08"),
            (design("character_designs", 0, **{"@TrainingCapacity": "0"}), "@TrainingCapacity must be an integer >= 1"),
            (remove("training", "character_designs", 0, "@TrainingCapacity"),
             "character design cd-recruit: @TrainingCapacity must be an integer >= 1, not None"),
            (design("training_designs", 0, **{"@TrainingStat": "Atack"}),
             "training design t-gym-1: unknown @TrainingStat 'Atack'"),
            (design("training_designs", 0, **{"@Duration": "0"}), "t-gym-1: @Duration must be an integer >= 1"),
            (design("training_designs", 0, **{"@MinGain": "5"}), "t-gym-1: @MinGain 5 is above @MaxGain 3"),
            (design("training_designs", 0, **{"@Fatigue": "-1"}), "t-gym-1: @Fatigue must be an integer >= 0"),
            (design("training_designs", 0, **{"@RequiredRoomDesignId": "999"}),
             "t-gym-1: unknown @RequiredRoomDesignId '999'"),
            (design("training_designs", 0, **{"@RequiredResearchDesignId": "999"}),
             "t-gym-1: unknown @RequiredResearchDesignId '999'"),
            (design("training_designs", 1, **{"@TrainingDesignId": "t-gym-1"}),
             "training block: duplicate @TrainingDesignId t-gym-1"),
            # Regression: a null list raised TypeError, and a record without its id was filed under "None".
            (setting("training_designs", None), "training block: training_designs must be a list of records"),
            (setting("item_designs", {"901": {}}), "training block: item_designs must be a list of records"),
            (lambda s: s["training"]["character_designs"].append("cd-x"),
             "training block: character_designs must be a list of records"),
            (remove("training", "training_designs", 0, "@TrainingDesignId"),
             "training block: a training_designs record has no @TrainingDesignId"),
            (lambda s: s["training"]["item_designs"].append({"@ItemDesignId": "960"}), "item design 960 has no effect"),
            (lambda s: s["training"]["item_designs"].append({"@ItemDesignId": "961", "@ConsumableStat": "Attack",
                                                             "@ConsumableGain": "0"}), "item design 961 has no effect"),
            (design("item_designs", 0, **{"@ConsumableStat": "Atk"}), "item design 901: unknown @ConsumableStat 'Atk'"),
            (lambda s: s["accounts"]["alpha"]["inventory"].append({"@ItemId": "i1", "@ItemDesignId": "950",
                                                                   "@Quantity": "1"}),
             "account alpha: duplicate @ItemId i1"),
            (lambda s: s["accounts"]["alpha"]["inventory"][0].update({"@Quantity": "-1"}),
             "account alpha: item i1: @Quantity must be an integer >= 0"),
            (remove(*inventory, 0, "@Quantity"), "item i1: @Quantity must be an integer >= 0, not None"),
        )
        for change, message in cases:
            scenario = load_scenario("crew-training-single-account")
            change(scenario)
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, re.escape(message)):
                    run_scenario(scenario, tmp)
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_training_faults_reach_the_runner_from_the_scenario(self):
        scenario = load_scenario("crew-training-faults")
        clock = SimulatedClock(datetime.fromisoformat(scenario["start"]))
        games = build_games(scenario, scenario["seed"], DesignData.from_records(**scenario["design"]), clock)
        g = games["alpha"]
        with tempfile.TemporaryDirectory() as tmp:
            runner = ActionRunner("alpha", g, g.snapshot, GameplayJournal(Path(tmp) / "alpha.jsonl"), clock=clock)
            first = runner.run("add_training", {"training_design_id": "t-gym-1", "character_id": "101"})
            second = runner.run("add_training", {"training_design_id": "t-acad-1", "character_id": "102"})
        self.assertEqual((first.outcome, second.outcome), ("contradicted", "not-observed"))
        self.assertEqual(g.training.log[0]["design_id"], "t-acad-1")
        clock.start_day(1)
        g.start_day(1)
        with self.assertRaises(SimulatedFaultError):
            g.finishTraining("101")
        self.assertEqual(g.fired, ["day 0 addTraining #1: contradicted-effect", "day 0 addTraining #2: reject",
                                   "day 1 finishTraining #1: raise"])
        result = run_training(scenario)
        self.assertEqual(result.faults, {"alpha": ()})  # no policy trains yet, so the faults never trigger
        self.assertEqual(len(result.crew["alpha"]), 3)


if __name__ == "__main__":
    unittest.main()
