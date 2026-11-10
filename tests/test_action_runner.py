"""Tests for sdk.execution.ActionRunner with a fake client. No network traffic."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.actions import CATALOG
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
        result = self.runner.run("upgrade_room", {"room_id": "r1"}, dry_run=True)
        self.assertEqual(result.outcome, "skipped")
        self.assertTrue(result.event.dry_run)
        self.assertIn("would call Client.upgradeRooms", result.reason)
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
        self.runner.run("upgrade_room", {"room_id": "r1"}, dry_run=True)
        summary = summarize(self.journal.read())["alpha"]
        self.assertEqual((summary["attempted"], summary["verified"], summary["skipped"]), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
