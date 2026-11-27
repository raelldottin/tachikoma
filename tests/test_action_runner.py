"""Tests for sdk.execution.ActionRunner with a fake client. No network traffic."""

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from sdk.actions import CATALOG, get_spec
from sdk.actions.catalog import ROOM_FILL_EMPTY_NOTE, ROOM_FILL_UNREAD_REASON, training_start_verified
from sdk.execution import ActionRunner, UncertainPriorAttemptError, call_arguments
from sdk.execution import runner as runner_module
from sdk.fleet.attempts import PENDING, RESOLVED, UNRESOLVED, VERIFIED, MemoryAttemptStore, TrainingGuard
from sdk.journal import GameplayJournal, summarize
from sdk.state import AccountMismatchError, build_snapshot
from tests import training_fixtures
from tests.test_action_catalog import DUE, STATS

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
BASE = dict(
    resources={"minerals": "100", "gas": "100", "starbux": "5"},
    crew=[{"@CharacterId": "c1", "@Level": "3"}],
    rooms=[{"@RoomId": "r1", "@RoomDesignId": "11", "@RoomStatus": "Normal"}],
    research=[{"@ResearchDesignId": "500", "@ResearchState": "Completed"}],
    inventory=[],
)


class FakeClock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


class FakeWorld:
    """Fake game: a client whose calls mutate state, and a snapshot provider."""

    def __init__(self, clock, alias="alpha"):
        self.clock = clock
        self.alias = alias
        self.state = {key: value for key, value in BASE.items()}
        self.calls = []
        self.result = True
        self.raise_on_call = None
        self.effect = None
        self.fail_after_snapshot = False
        self.snapshots = 0

    # Client surface ------------------------------------------------------------------
    def __getattr__(self, name):
        if name in {spec.client_method for spec in CATALOG.values()}:
            def method(*args):
                self.calls.append((name, args))
                if self.effect:
                    self.effect(self.state)
                if self.raise_on_call:
                    raise self.raise_on_call
                return self.result
            return method
        raise AttributeError(name)

    # Snapshot provider ---------------------------------------------------------------
    def snapshot(self):
        self.snapshots += 1
        if self.fail_after_snapshot and self.snapshots > 1:
            raise ConnectionError("synthetic read failure")
        return build_snapshot(self.alias, self.clock(), "fake-world", **self.state)


def gain_minerals(state):
    state["resources"] = {"minerals": "150", "gas": "100", "starbux": "5"}


def start_research(state):
    state["research"] = BASE["research"] + [{"@ResearchDesignId": "501", "@ResearchState": "Researching"}]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = GameplayJournal(Path(self.tmp.name) / "events.jsonl")
        self.clock = FakeClock()
        self.world = FakeWorld(self.clock)
        self.runner = ActionRunner("alpha", self.world, self.world.snapshot, self.journal, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_call_arguments_are_the_required_params_in_order(self):
        spec = CATALOG["place_room"]
        self.assertEqual(call_arguments(spec, {"column": 3, "row": 2, "room_design_id": 42}), (42, 2, 3))
        self.assertEqual(call_arguments(CATALOG["collect_all_resources"], {}), ())

    def test_verified_action_is_called_once_and_journaled_with_cost(self):
        self.world.effect = gain_minerals
        result = self.runner.run("collect_all_resources")
        self.assertEqual(result.outcome, "verified")
        self.assertEqual(self.world.calls, [("collectAllResources", ())])
        event = self.journal.read()[0]
        self.assertTrue(event.attempted)
        self.assertEqual(event.cost["minerals_delta"], 50)
        self.assertEqual(event.response_ok, True)

    def test_room_upgrade_targets_one_room(self):
        def upgrade(state):
            state["rooms"] = [{"@RoomId": "r1", "@RoomDesignId": "11", "@RoomStatus": "Upgrading"}]

        self.world.effect = upgrade
        self.world.result = object()  # stands in for the raw HTTP response upgradeRoom returns
        result = self.runner.run("upgrade_room", {"room_id": "r1", "upgrade_room_design_id": "12"})
        self.assertEqual(self.world.calls, [("upgradeRoom", ("r1", "12"))])
        # The raw HTTP response is not a boolean, so only the observed status verifies it.
        self.assertEqual((result.outcome, result.event.response_ok), ("verified", None))

    def test_parameters_reach_the_client_method(self):
        self.world.effect = start_research
        result = self.runner.run("add_research", {"research_design_id": "501"})
        self.assertEqual(result.outcome, "verified")
        self.assertEqual(self.world.calls, [("addResearch", ("501",))])

    def test_http_success_without_effect_is_not_verified(self):
        result = self.runner.run("collect_all_resources")
        self.assertEqual(result.outcome, "not-observed")

    def test_ambiguous_response_without_effect_is_uncertain(self):
        self.world.result = "LAB_UPGRADE_REQUIRED"
        result = self.runner.run("add_research", {"research_design_id": "501"})
        self.assertEqual(result.outcome, "unknown")
        self.assertIn("ambiguous response", result.reason)

    def test_exception_is_journaled_as_uncertain_and_not_retried(self):
        self.world.raise_on_call = TimeoutError("synthetic timeout")
        result = self.runner.run("add_research", {"research_design_id": "501"})
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(result.event.error_class, "TimeoutError")
        self.assertEqual(len(self.world.calls), 1)

    def test_exception_with_observed_effect_is_verified_and_records_error(self):
        self.world.effect = start_research
        self.world.raise_on_call = TimeoutError("synthetic timeout")
        result = self.runner.run("add_research", {"research_design_id": "501"})
        self.assertEqual(result.outcome, "verified")
        self.assertEqual(result.event.error_class, "TimeoutError")

    def test_uncertain_non_idempotent_action_is_not_repeated(self):
        self.world.raise_on_call = TimeoutError("synthetic timeout")
        self.runner.run("add_research", {"research_design_id": "501"})
        self.world.raise_on_call = None
        with self.assertRaises(UncertainPriorAttemptError):
            self.runner.run("add_research", {"research_design_id": "501"})
        self.assertEqual(len(self.world.calls), 1)
        self.assertEqual(self.journal.read()[-1].error_class, "UncertainPriorAttemptError")
        # An operator may override after reconciling by hand.
        self.world.effect = start_research
        self.assertEqual(
            self.runner.run("add_research", {"research_design_id": "501"}, allow_after_uncertain=True).outcome,
            "verified",
        )

    def test_safe_to_retry_action_may_follow_an_uncertain_attempt(self):
        self.world.raise_on_call = TimeoutError("synthetic timeout")
        self.runner.run("collect_all_resources")
        self.world.raise_on_call = None
        self.world.effect = gain_minerals
        self.assertEqual(self.runner.run("collect_all_resources").outcome, "verified")

    # Regression (PR #272 review): the marker and drone collections return a count; 0 (nothing to collect)
    # was journaled as an ambiguous response, so every quiet run read as unknown.
    def test_a_count_is_a_completed_call(self):
        self.world.result = 0
        result = self.runner.run("collect_available_markers")
        self.assertEqual(result.outcome, "not-observed")
        self.assertIs(result.event.response_ok, True)
        self.world.result = False
        self.assertIs(self.runner.run("collect_available_markers").event.response_ok, False)
        self.world.result = -1
        self.assertEqual(self.runner.run("collect_available_markers").outcome, "unknown")

    def test_failed_preconditions_skip_without_calling(self):
        result = self.runner.run("add_research", {"research_design_id": "500"})
        self.assertEqual(result.outcome, "skipped")
        self.assertFalse(result.attempted)
        self.assertIn("already completed", result.reason)
        self.assertEqual(self.world.calls, [])

    def test_dry_run_makes_no_mutating_call(self):
        result = self.runner.run("upgrade_room", {"room_id": "r1", "upgrade_room_design_id": "12"}, dry_run=True)
        self.assertEqual(result.outcome, "skipped")
        self.assertTrue(result.event.dry_run)
        self.assertIn("would call Client.upgradeRoom", result.reason)
        self.assertEqual(self.world.calls, [])

    def test_snapshot_for_another_account_is_refused_before_any_call(self):
        self.world.alias = "bravo"
        with self.assertRaises(AccountMismatchError):
            self.runner.run("collect_all_resources")
        self.assertEqual(self.world.calls, [])
        event = self.journal.read()[0]
        self.assertEqual((event.account_alias, event.outcome), ("alpha", "skipped"))

    def test_after_snapshot_failure_is_uncertain(self):
        self.world.fail_after_snapshot = True
        result = self.runner.run("collect_all_resources")
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(result.event.error_class, "ConnectionError")

    def test_summary_reflects_runner_history(self):
        self.world.effect = gain_minerals
        self.runner.run("collect_all_resources")
        self.runner.run("upgrade_room", {"room_id": "r1", "upgrade_room_design_id": "12"}, dry_run=True)
        summary = summarize(self.journal.read())["alpha"]
        self.assertEqual((summary["attempted"], summary["verified"], summary["skipped"]), (1, 1, 1))


DUE_TRAINING = {"@CharacterId": "c1", "@Level": "3", "@TrainingEndDate": DUE,
                "@TrainingDesignId": "7", **STATS}


def finish_due_training(state):
    state["crew"] = [{**DUE_TRAINING, "@TrainingEndDate": "", "@TrainingDesignId": "0", "@HpImprovement": "2"}]


def use_item(state):
    state["inventory"] = []


class TrainingAndConsumableRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = GameplayJournal(Path(self.tmp.name) / "events.jsonl")
        self.clock = FakeClock()
        self.world = FakeWorld(self.clock)
        self.world.state["crew"] = [dict(DUE_TRAINING)]
        self.world.state["inventory"] = [{"@ItemDesignId": "900", "@Quantity": "1"}]
        self.runner = ActionRunner("alpha", self.world, self.world.snapshot, self.journal, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finish_training_calls_the_client_with_the_character(self):
        self.world.effect = finish_due_training
        result = self.runner.run("finish_training", {"character_id": "c1"})
        self.assertEqual(self.world.calls, [("finishTraining", ("c1",))])
        self.assertEqual(result.outcome, "verified", result.reason)

    def test_uncertain_finish_training_may_be_retried(self):
        self.world.raise_on_call = TimeoutError("synthetic timeout")
        self.assertEqual(self.runner.run("finish_training", {"character_id": "c1"}).outcome, "unknown")
        self.world.raise_on_call = None
        self.world.effect = finish_due_training
        self.assertEqual(self.runner.run("finish_training", {"character_id": "c1"}).outcome, "verified")
        self.assertEqual(len(self.world.calls), 2)

    def test_a_second_finish_after_a_verified_one_is_skipped_without_a_call(self):
        # Whether the server clears the training or leaves a placeholder end with no design, nothing is left to finish.
        def placeholder(state):
            state["crew"] = [{**DUE_TRAINING, "@TrainingEndDate": "0001-01-01T00:00:00", "@TrainingDesignId": "0",
                              "@HpImprovement": "2"}]

        for name, effect in (("cleared", finish_due_training), ("placeholder end", placeholder)):
            with self.subTest(server=name):
                self.world.state["crew"] = [dict(DUE_TRAINING)]
                self.world.calls.clear()
                self.world.effect = effect
                self.assertEqual(self.runner.run("finish_training", {"character_id": "c1"}).outcome, "verified")
                second = self.runner.run("finish_training", {"character_id": "c1"})
                self.assertEqual(second.outcome, "skipped", second.reason)
                self.assertEqual(self.world.calls, [("finishTraining", ("c1",))])

    def test_use_consumable_passes_the_design_then_the_character(self):
        self.world.effect = use_item
        result = self.runner.run("use_consumable", {"item_design_id": "900", "character_id": "c1"})
        self.assertEqual(self.world.calls, [("useConsumable", ("900", "c1"))])
        self.assertEqual(result.outcome, "verified", result.reason)

    def test_uncertain_use_consumable_blocks_a_retry(self):
        params = {"item_design_id": "900", "character_id": "c1"}
        self.world.raise_on_call = TimeoutError("synthetic timeout")
        self.assertEqual(self.runner.run("use_consumable", params).outcome, "unknown")
        self.world.raise_on_call = None
        with self.assertRaises(UncertainPriorAttemptError):
            self.runner.run("use_consumable", params)
        self.assertEqual(len(self.world.calls), 1)


def place_gas_collector(state):
    state["rooms"] = BASE["rooms"] + [{"@RoomId": "room-instance-5512", "@RoomDesignId": "42",
                                       "@RoomStatus": "Building", "@Row": "1", "@Column": "1"}]
    state["resources"] = {"minerals": "90", "gas": "100", "starbux": "5"}


class PlaceRoomRunnerTests(unittest.TestCase):
    """``place_room`` through the runner (SPEC-13, S13-R4 to R6)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.journal = GameplayJournal(self.path)
        self.clock = FakeClock()
        self.world = FakeWorld(self.clock)
        self.runner = ActionRunner("alpha", self.world, self.world.snapshot, self.journal, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    # Verifies: S13-R4, S13-R5
    def test_a_placement_is_verified_by_the_room_appearing_and_journals_no_room_id(self):
        self.world.effect = place_gas_collector
        result = self.runner.run("place_room", {"room_design_id": "42", "row": 1, "column": 1},
                                 expected_cost={"mineral": 10})
        self.assertEqual(result.outcome, "verified")
        self.assertEqual(self.world.calls, [("placeRoom", ("42", 1, 1))])
        self.assertEqual(result.event.cost["minerals_delta"], -10)
        self.assertNotIn("room-instance-5512", self.path.read_text(encoding="utf-8"))

    # Verifies: S13-R5, S13-R6
    def test_an_ambiguous_placement_blocks_every_later_placement_on_the_account(self):
        self.world.result = object()  # the raw response: not the captured success shape
        first = self.runner.run("place_room", {"room_design_id": "42", "row": 1, "column": 1})
        self.assertEqual(first.outcome, "unknown")
        for params in ({"room_design_id": "42", "row": 1, "column": 1},
                       {"room_design_id": "42", "row": 6, "column": 4},
                       {"room_design_id": "26", "row": 6, "column": 4}):
            with self.subTest(params=params), self.assertRaises(UncertainPriorAttemptError):
                self.runner.run("place_room", params)
        self.assertEqual(len(self.world.calls), 1)  # nothing after the uncertain call reached the game
        self.assertIn("a previous place_room attempt on this account is still uncertain", self.journal.read()[-1].reason)

    # Verifies: S13-R6
    def test_a_definite_success_without_a_room_is_not_observed_and_does_not_block(self):
        self.world.result = True
        self.assertEqual(self.runner.run("place_room", {"room_design_id": "42", "row": 1, "column": 1}).outcome,
                         "not-observed")
        self.world.effect = place_gas_collector
        self.assertEqual(self.runner.run("place_room", {"room_design_id": "42", "row": 1, "column": 1}).outcome,
                         "verified")

    def test_other_actions_keep_their_per_params_uncertainty(self):
        self.world.result = object()
        self.assertEqual(self.runner.run("upgrade_room", {"room_id": "r1", "upgrade_room_design_id": "12"}).outcome,
                         "unknown")
        self.world.state["rooms"] = BASE["rooms"] + [{"@RoomId": "r2", "@RoomDesignId": "11", "@RoomStatus": "Normal"}]
        self.world.result = True
        self.runner.run("upgrade_room", {"room_id": "r2", "upgrade_room_design_id": "12"})  # not blocked
        with self.assertRaises(UncertainPriorAttemptError):
            self.runner.run("upgrade_room", {"room_id": "r1", "upgrade_room_design_id": "12"})


LEVEL_1_SHIP = {"@ShipDesignId": "2", "@ShipLevel": "1", "@UpgradeShipDesignId": "0"}


def start_hull_upgrade(state):
    state["ship"] = {**LEVEL_1_SHIP, "@UpgradeShipDesignId": "3", "@UpgradeStartDate": "2026-11-10T12:00:05"}
    state["resources"] = {"minerals": "40", "gas": "100", "starbux": "5"}


class UpgradeShipRunnerTests(unittest.TestCase):
    """``upgrade_ship`` through the runner: verified by the ship record, any uncertain attempt blocks the account."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = GameplayJournal(Path(self.tmp.name) / "events.jsonl")
        self.clock = FakeClock()
        self.world = FakeWorld(self.clock)
        self.world.state["ship"] = LEVEL_1_SHIP
        self.runner = ActionRunner("alpha", self.world, self.world.snapshot, self.journal, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_started_upgrade_is_verified_from_a_raw_response(self):
        self.world.effect = start_hull_upgrade
        self.world.result = object()  # Client.upgradeShip returns the raw response
        result = self.runner.run("upgrade_ship", {"ship_design_id": "3"}, expected_cost={"mineral": 60})
        self.assertEqual(result.outcome, "verified", result.reason)
        self.assertEqual(self.world.calls, [("upgradeShip", ("3",))])
        self.assertEqual(result.event.cost["minerals_delta"], -60)
        self.assertEqual(result.event.evidence, {"server_reply": None})  # no upgradeShipReply on this client

    def test_an_unchanged_ship_after_a_raw_response_is_unknown_and_blocks_every_later_upgrade(self):
        self.world.result = object()
        first = self.runner.run("upgrade_ship", {"ship_design_id": "3"}, expected_cost={"mineral": 60})
        self.assertEqual(first.outcome, "unknown")
        self.assertEqual(first.event.cost["minerals_delta"], 0)
        for params in ({"ship_design_id": "3"}, {"ship_design_id": "4"}):
            with self.subTest(params=params), self.assertRaises(UncertainPriorAttemptError):
                self.runner.run("upgrade_ship", params)
        self.assertEqual(len(self.world.calls), 1)
        self.assertIn("a previous upgrade_ship attempt on this account is still uncertain",
                      self.journal.read()[-1].reason)


# Room instance ids that must never reach the journal.
COLLECTOR_ROOM_ID = "room-instance-7731"
SILO_ROOM_ID = "room-instance-7732"
ROOM_DESIGNS = {"RoomService": {"ListRoomDesigns": {"RoomDesigns": {"RoomDesign": [
    {"@RoomDesignId": "41", "@RoomName": "Mineral Collector", "@RoomType": "Mineral",
     "@ManufactureType": "Mineral", "@Capacity": "300"},
    {"@RoomDesignId": "42", "@RoomName": "Mineral Storage", "@RoomType": "Storage",
     "@ManufactureType": "Mineral", "@Capacity": "2000"},
]}}}}


class ShipClient:
    """A client whose collection empties the collector into minerals, and whose snapshot re-reads the ship.

    Like ``Client.getShipByUserId``, each snapshot replaces ``client.rooms``, so the runner's evidence probe
    sees the rooms as they were at that snapshot.
    """

    def __init__(self, clock, waiting=120, credited=None):
        self.clock = clock
        self.waiting = waiting
        self.credited = waiting if credited is None else credited
        self.minerals = 500
        self.roomDesigns = ROOM_DESIGNS
        self.rooms = None
        self.calls = 0

    def collectAllResources(self):
        self.calls += 1
        self.minerals += self.credited
        self.waiting = 0
        return True

    def ship_rooms(self):
        return [
            {"@RoomId": COLLECTOR_ROOM_ID, "@RoomDesignId": "41", "@RoomStatus": "Normal",
             "@CapacityUsed": str(self.waiting)},
            {"@RoomId": SILO_ROOM_ID, "@RoomDesignId": "42", "@RoomStatus": "Normal", "@CapacityUsed": "0"},
        ]

    def snapshot(self):
        self.rooms = self.ship_rooms()
        return build_snapshot("alpha", self.clock(), "fake-ship", rooms=self.rooms, crew=[], research=[],
                              inventory=[], resources={"minerals": str(self.minerals), "gas": "40", "starbux": "0"})


class CollectionRoomFillTests(unittest.TestCase):
    """A collection journals each room design's fill before and after it, without room ids (S07-R2 support)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.journal = GameplayJournal(self.path)
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, client):
        runner = ActionRunner("alpha", client, client.snapshot, self.journal, clock=self.clock)
        return runner.run("collect_all_resources")

    def test_a_verified_collection_shows_the_collector_emptied(self):
        result = self.collect(ShipClient(self.clock, waiting=120))
        self.assertEqual(result.outcome, "verified")
        evidence = result.event.evidence
        self.assertEqual((evidence["room_fill_observed"], evidence["room_fill_before_total"],
                          evidence["room_fill_after_total"]), (True, 120, 0))
        collector, silo = evidence["room_fill"]
        self.assertEqual(collector, {"design_id": "41", "name": "Mineral Collector", "room_type": "Mineral",
                                     "manufacture_type": "Mineral", "design_capacity": 300, "rooms": 1,
                                     "fill_before": 120, "fill_after": 0})
        self.assertEqual((silo["design_id"], silo["room_type"], silo["fill_before"]), ("42", "Storage", 0))
        self.assertNotIn("room_fill_note", evidence)

    def test_nothing_waiting_is_said_when_the_collection_is_not_observed(self):
        result = self.collect(ShipClient(self.clock, waiting=0))
        self.assertEqual(result.outcome, "not-observed")
        self.assertEqual(result.event.reason, "minerals and gas unchanged")
        self.assertEqual(result.event.evidence["room_fill_before_total"], 0)
        self.assertEqual(result.event.evidence["room_fill_note"], ROOM_FILL_EMPTY_NOTE)

    def test_a_collection_not_seen_while_rooms_held_resources_carries_no_empty_note(self):
        # The defect signal: something was waiting, the collector emptied, but minerals did not change.
        result = self.collect(ShipClient(self.clock, waiting=80, credited=0))
        self.assertEqual(result.outcome, "not-observed")
        evidence = result.event.evidence
        self.assertEqual((evidence["room_fill_before_total"], evidence["room_fill_after_total"]), (80, 0))
        self.assertNotIn("room_fill_note", evidence)

    def test_room_instance_ids_never_reach_the_journal(self):
        result = self.collect(ShipClient(self.clock))
        self.assertEqual(len(result.event.evidence["room_fill"]), 2)  # the rooms were journaled, by design
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(COLLECTOR_ROOM_ID, text)
        self.assertNotIn(SILO_ROOM_ID, text)

    def test_an_unreadable_fill_is_unknown_not_zero(self):
        client = ShipClient(self.clock, waiting=0)
        rooms = client.ship_rooms
        client.ship_rooms = lambda: [{**room, "@CapacityUsed": "n/a"} if room["@RoomDesignId"] == "41" else room
                                     for room in rooms()]
        evidence = self.collect(client).event.evidence
        self.assertIsNone(evidence["room_fill_before_total"])
        self.assertIsNone(evidence["room_fill"][0]["fill_before"])
        self.assertNotIn("room_fill_note", evidence)

    def failed_ship_read(self, client, side):
        """Like a failed ``getShipByUserId``: the snapshot has no rooms and ``client.rooms`` is left as it was."""
        snapshot = client.snapshot

        def read():
            if (side == "after") == bool(client.calls):
                return build_snapshot("alpha", self.clock(), "fake-ship", rooms=None, crew=[], research=[],
                                      inventory=[],
                                      resources={"minerals": str(client.minerals), "gas": "40", "starbux": "0"})
            return snapshot()

        client.snapshot = read

    def test_a_failed_ship_read_leaves_the_fill_unobserved_not_stale(self):
        for side in ("before", "after"):
            with self.subTest(side=side):
                client = ShipClient(self.clock, waiting=80, credited=0)
                client.rooms = client.ship_rooms()  # left over from an earlier read
                self.failed_ship_read(client, side)
                result = self.collect(client)
                self.assertEqual(result.outcome, "not-observed")
                evidence = result.event.evidence
                self.assertEqual((evidence["room_fill_observed"], evidence["room_fill_before_total"],
                                  evidence["room_fill_after_total"], evidence["room_fill"]), (False, None, None, []))
                self.assertEqual(evidence["room_fill_unobserved_reason"], ROOM_FILL_UNREAD_REASON)
                self.assertNotIn("room_fill_note", evidence)

    def test_missing_designs_still_journal_the_fill(self):
        client = ShipClient(self.clock)
        client.roomDesigns = None
        group = self.collect(client).event.evidence["room_fill"][0]
        self.assertEqual((group["design_id"], group["name"], group["room_type"], group["fill_before"]),
                         ("41", None, None, 120))


IDENTITY = "gid1-" + "c" * 32
START = {"training_design_id": "5010", "character_id": "101"}
COST = {"minerals": 500, "gas": 0}
STRICT_ADD_TRAINING = replace(get_spec("add_training"), postcondition=training_start_verified)


class Crash(BaseException):
    """A process that dies inside the request."""


class TrainingStartGuardTests(unittest.TestCase):
    """The intent before the request and the settlement after it (SPEC-14 safety invariants), through the module
    hooks ``write_training_intent`` and ``settle_training_intent`` (looked up at call time)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = GameplayJournal(Path(self.tmp.name) / "events.jsonl")
        self.clock = FakeClock()
        self.world = FakeWorld(self.clock)
        self.world.state["crew"] = [training_fixtures.crew("101")]
        self.world.effect = self.start
        self.store = MemoryAttemptStore()
        self.store.provision("alpha", IDENTITY, T0)
        self.recheck = Mock(return_value=None)
        self.guard = TrainingGuard(self.store, "alpha", IDENTITY, self.recheck, self.clock)

    def start(self, state):
        end = training_fixtures.server_time(self.clock.now + timedelta(hours=1))
        state["crew"] = [training_fixtures.crew("101", training="5010", end=end)]

    def runner(self, guard="default", specs="strict"):
        return ActionRunner("alpha", self.world, self.world.snapshot, self.journal, clock=self.clock,
                            specs={"add_training": STRICT_ADD_TRAINING} if specs == "strict" else specs,
                            attempt_guard=self.guard if guard == "default" else guard)

    def attempt(self):
        [attempt] = self.store.load("alpha").attempts
        return attempt

    def test_spec_override_is_used_for_the_verdict(self):
        spy = Mock(side_effect=training_start_verified)
        runner = self.runner(specs={"add_training": replace(STRICT_ADD_TRAINING, postcondition=spy)})
        self.assertEqual(runner.run("add_training", START, expected_cost=COST).outcome, "verified")
        spy.assert_called_once()
        self.assertIs(self.runner().spec_for("add_training"), STRICT_ADD_TRAINING)
        self.assertIs(ActionRunner("alpha", self.world, self.world.snapshot, self.journal).spec_for("add_training"),
                      get_spec("add_training"))

    def test_no_guard_no_specs_is_unchanged(self):
        with patch.object(runner_module, "write_training_intent") as write, \
                patch.object(runner_module, "settle_training_intent") as settle:
            result = ActionRunner("alpha", self.world, self.world.snapshot, self.journal,
                                  clock=self.clock).run("add_training", START, expected_cost=COST)
        self.assertEqual(result.outcome, "verified")
        write.assert_not_called()
        settle.assert_not_called()

    # Verifies: S14-R5
    def test_intent_is_written_through_the_module_hook_before_the_call(self):
        order = []
        real = runner_module.write_training_intent

        def write(store, alias, intent):
            order.append(("intent", alias, intent.character_id, intent.training_design_id, dict(intent.planned_cost),
                          len(self.world.calls)))
            real(store, alias, intent)

        with patch.object(runner_module, "write_training_intent", write):
            result = self.runner().run("add_training", START, expected_cost=COST)
        self.assertEqual(order, [("intent", "alpha", "101", "5010", {"minerals": 500}, 0)])
        self.assertEqual(self.world.calls, [("addTraining", ("5010", "101"))])
        self.assertEqual(result.outcome, "verified")
        self.assertEqual(self.attempt().state, VERIFIED)
        self.assertEqual(self.attempt().journal_attempt_id, result.event.attempt_id)
        self.recheck.assert_called_once()

    # Verifies: S14-R5
    def test_intent_failure_sends_nothing(self):
        with patch.object(runner_module, "write_training_intent", side_effect=OSError("disk full")):
            with self.assertRaises(runner_module.IntentNotPersistedError):
                self.runner().run("add_training", START, expected_cost=COST)
        self.assertEqual(self.world.calls, [])
        [event] = self.journal.read()
        self.assertEqual((event.outcome, event.attempted, dict(event.params)), ("skipped", False, START))
        self.assertTrue(event.reason.startswith("intent_not_persisted: "), event.reason)
        self.assertEqual(self.store.load("alpha").attempts, ())

    def test_settle_runs_after_the_outcome_is_journaled(self):
        seen = []
        real = runner_module.settle_training_intent

        def settle(store, alias, intent_id, settlement):
            seen.append(([event.attempt_id for event in self.journal.read()], dict(settlement)))
            real(store, alias, intent_id, settlement)

        with patch.object(runner_module, "settle_training_intent", settle):
            result = self.runner().run("add_training", START, expected_cost=COST)
        [(journaled, settlement)] = seen
        self.assertEqual(journaled, [result.event.attempt_id])
        self.assertEqual((settlement["outcome"], settlement["journal_attempt_id"]),
                         ("verified", result.event.attempt_id))

    # Verifies: S14-R5
    def test_settle_failure_keeps_the_result_and_raises(self):
        with patch.object(runner_module, "settle_training_intent", side_effect=OSError("disk full")):
            with self.assertRaises(runner_module.AttemptSettlementError) as caught:
                self.runner().run("add_training", START, expected_cost=COST)
        self.assertEqual(caught.exception.result.outcome, "verified")
        self.assertEqual([event.attempt_id for event in self.journal.read()], [caught.exception.result.event.attempt_id])
        self.assertTrue(str(caught.exception).startswith("unresolved_attempt: "))
        self.assertEqual(self.attempt().state, PENDING)  # still blocking
        self.assertIsInstance(caught.exception, UncertainPriorAttemptError)  # the scheduler stops the account

    def test_crash_during_the_call_propagates_without_settling(self):
        self.world.raise_on_call = Crash()
        with patch.object(runner_module, "settle_training_intent") as settle:
            with self.assertRaises(Crash):
                self.runner().run("add_training", START, expected_cost=COST)
        settle.assert_not_called()
        self.assertEqual(self.attempt().state, PENDING)

    def test_guard_refusals_send_nothing(self):
        self.store.write_intent("alpha", self.guard.intent({"training_design_id": "1", "character_id": "9"}, {}))
        with patch.object(runner_module, "write_training_intent") as write:
            with self.assertRaises(runner_module.TrainingStartRefusedError):
                self.runner().run("add_training", START, expected_cost=COST)
        write.assert_not_called()
        self.assertEqual(self.world.calls, [])
        event = self.journal.read()[-1]
        self.assertTrue(event.reason.startswith("unresolved_attempt: "), event.reason)
        # a recheck refusal skips the start without stopping the account
        store = MemoryAttemptStore()
        store.provision("alpha", IDENTITY, T0)
        guard = TrainingGuard(store, "alpha", IDENTITY, Mock(return_value="price_unavailable: changed"), self.clock)
        result = self.runner(guard=guard).run("add_training", START, expected_cost=COST)
        self.assertEqual((result.outcome, result.attempted), ("skipped", False))
        self.assertEqual(result.reason, "price_unavailable: changed")
        self.assertEqual(self.world.calls, [])

    def test_a_second_start_in_one_run_is_skipped(self):
        runner = self.runner()
        self.assertEqual(runner.run("add_training", START, expected_cost=COST).outcome, "verified")
        self.world.state["crew"].append(training_fixtures.crew("102", name="Delish"))
        other = {"training_design_id": "5010", "character_id": "102"}
        result = runner.run("add_training", other, expected_cost=COST)
        self.assertEqual((result.outcome, result.attempted), ("skipped", False))
        self.assertEqual(len(self.world.calls), 1)

    def test_guard_covers_only_its_actions(self):
        self.world.effect = gain_minerals
        with patch.object(runner_module, "write_training_intent") as write:
            result = self.runner().run("collect_all_resources", expected_cost={})
        write.assert_not_called()
        self.assertEqual(result.outcome, "verified")

    def test_after_snapshot_failure_settles_unknown(self):
        self.world.fail_after_snapshot = True
        result = self.runner().run("add_training", START, expected_cost=COST)
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual((self.attempt().state, self.attempt().outcome), (UNRESOLVED, "unknown"))

    def test_account_mismatch_after_send_settles_and_reraises(self):
        def other_account(state):
            self.start(state)
            self.world.alias = "bravo"

        self.world.effect = other_account
        with self.assertRaises(AccountMismatchError):
            self.runner().run("add_training", START, expected_cost=COST)
        self.assertEqual(self.attempt().state, UNRESOLVED)

    def test_a_reconciled_attempt_no_longer_blocks_the_same_params(self):
        self.world.fail_after_snapshot = True
        first = self.runner().run("add_training", START, expected_cost=COST)
        self.assertEqual(first.outcome, "unknown")
        self.world.fail_after_snapshot = False
        self.world.state["crew"] = [training_fixtures.crew("101")]
        fresh = TrainingGuard(self.store, "alpha", IDENTITY, None, self.clock)
        with self.assertRaises(UncertainPriorAttemptError):  # the journal's per-params uncertainty
            self.runner(guard=fresh).run("add_training", START, expected_cost=COST)
        intent_id = self.attempt().intent_id
        self.store.resolve("alpha", {intent_id: {"verdict": "not_running", "observed_phase": "none",
                                                 "observed_course_id": None, "observed_end": None}}, T0)
        self.assertEqual(self.attempt().state, RESOLVED)
        fresh = TrainingGuard(self.store, "alpha", IDENTITY, None, self.clock)
        self.assertEqual(self.runner(guard=fresh).run("add_training", START, expected_cost=COST).outcome, "verified")

    # Verifies: S14-R5 (regression C1/D2)
    def test_a_dry_run_without_a_guard_uses_the_journal_reconciled_view(self):
        self.world.fail_after_snapshot = True
        first = self.runner().run("add_training", START, expected_cost=COST)
        self.world.fail_after_snapshot = False
        self.world.state["crew"] = [training_fixtures.crew("101")]
        dry = ActionRunner("alpha", self.world, self.world.snapshot, self.journal, clock=self.clock)
        with self.assertRaises(UncertainPriorAttemptError):  # no view: the journal's uncertainty blocks
            dry.run("add_training", START, dry_run=True, expected_cost=COST)
        self.journal.reconciled_attempts = lambda: frozenset()
        with self.assertRaises(UncertainPriorAttemptError):  # nothing reconciled yet
            dry.run("add_training", START, dry_run=True, expected_cost=COST)
        self.journal.reconciled_attempts = lambda: frozenset({first.event.attempt_id})
        result = dry.run("add_training", START, dry_run=True, expected_cost=COST)
        self.assertEqual((result.outcome, result.attempted), ("skipped", False))
        self.assertTrue(result.reason.startswith("dry run: would call"), result.reason)
        self.assertEqual(self.world.calls, [("addTraining", ("5010", "101"))])  # only the first, real start
        # the journal is never filtered: the first start's uncertain spend is still there to be charged
        self.assertIn(first.event.attempt_id, [event.attempt_id for event in self.journal.read()])


if __name__ == "__main__":
    unittest.main()
