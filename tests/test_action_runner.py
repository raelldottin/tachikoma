"""Tests for sdk.execution.ActionRunner with a fake client. No network traffic."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.actions import CATALOG
from sdk.actions.catalog import ROOM_FILL_EMPTY_NOTE, ROOM_FILL_UNREAD_REASON
from sdk.execution import CALL_ARGUMENTS, ActionRunner, UncertainPriorAttemptError
from sdk.journal import GameplayJournal, summarize
from sdk.state import AccountMismatchError, build_snapshot

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

    def test_every_catalog_action_has_call_arguments(self):
        self.assertEqual(set(CALL_ARGUMENTS), set(CATALOG))

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


if __name__ == "__main__":
    unittest.main()
