"""Tests for sdk.scheduler.DailyScheduler with a fake game. No network traffic."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.execution import ActionRunner
from sdk.journal import GameplayEvent, GameplayJournal
from sdk.policy import DesignData, PolicyConfig, get_policy
from sdk.scheduler import DailyScheduler, SchedulerConfig
from sdk.state import AccountMismatchError, build_snapshot

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
DESIGN = DesignData.from_records(
    room_designs=[
        {"@RoomDesignId": "100", "@RoomName": "Engine"},
        {"@RoomDesignId": "101", "@UpgradeFromRoomDesignId": "100", "@PriceString": "mineral:300"},
    ],
    research_designs=[{"@ResearchDesignId": "510", "@RootResearchDesignId": "510", "@GasCost": "50"}],
)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


class FakeGame:
    """Client methods mutate state the way a successful server call would."""

    def __init__(self, clock):
        self.clock = clock
        self.alias = "alpha"
        self.calls = []
        self.broken = set()
        self.state = dict(
            resources={"minerals": "1000", "gas": "1000", "starbux": "5"},
            rooms=[{"@RoomId": "r1", "@RoomDesignId": "100", "@RoomStatus": "Normal"}],
            research=[],
            inventory=[],
        )

    def snapshot(self):
        return build_snapshot(self.alias, self.clock(), "fake-game", **self.state)

    def _set_resources(self, minerals, gas=None, starbux=None):
        current = self.state["resources"]
        self.state["resources"] = {
            "minerals": str(minerals),
            "gas": current["gas"] if gas is None else str(gas),
            "starbux": current["starbux"] if starbux is None else str(starbux),
        }

    def addResearch(self, design_id):
        self.calls.append("addResearch")
        if "addResearch" not in self.broken:
            self.state["research"] = [{"@ResearchDesignId": design_id, "@ResearchState": "Researching"}]
        return True

    def upgradeRoom(self, room_id, design_id):
        self.calls.append("upgradeRoom")
        if "upgradeRoom" not in self.broken:
            self.state["rooms"] = [{"@RoomId": room_id, "@RoomDesignId": "100", "@RoomStatus": "Upgrading"}]
        return True

    def collectAllResources(self):
        self.calls.append("collectAllResources")
        if "collectAllResources" in self.broken:
            self._set_resources(int(self.state["resources"]["minerals"]) - 10)
        else:
            self._set_resources(int(self.state["resources"]["minerals"]) + 25)
        return True

    def collectDailyReward(self):
        self.calls.append("collectDailyReward")
        self._set_resources(self.state["resources"]["minerals"], starbux=int(self.state["resources"]["starbux"]) + 1)
        return True


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = GameplayJournal(Path(self.tmp.name) / "events.jsonl")
        self.clock = Clock()
        self.game = FakeGame(self.clock)
        self.runner = ActionRunner("alpha", self.game, self.game.snapshot, self.journal, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def scheduler(self, **config):
        return DailyScheduler(
            self.runner, get_policy("baseline-v1"), DESIGN, PolicyConfig(), SchedulerConfig(**config), clock=self.clock
        )

    def test_dry_run_is_the_default_and_makes_no_calls(self):
        report = self.scheduler().run()
        self.assertTrue(report.dry_run)
        self.assertEqual(self.game.calls, [])
        self.assertTrue(all(result.event.dry_run for result in report.results))
        self.assertEqual(len(report.results), len(report.plan.requests))
        self.assertTrue(report.completed)

    def test_execute_runs_the_plan_in_order_and_verifies_each_action(self):
        report = self.scheduler().run(execute=True)
        self.assertEqual(self.game.calls, ["addResearch", "upgradeRoom", "collectAllResources", "collectDailyReward"])
        self.assertEqual([result.outcome for result in report.results], ["verified"] * 4)
        self.assertIsNone(report.stop_reason)

    def test_once_per_period_action_is_skipped_when_already_done(self):
        self.scheduler().run(execute=True)
        self.game.calls.clear()
        report = self.scheduler().run(execute=True)
        self.assertNotIn("collectDailyReward", self.game.calls)
        self.assertEqual([skip.request.action_id for skip in report.skipped], ["collect_daily_reward"])

    def test_once_per_period_action_runs_again_after_the_period(self):
        self.scheduler().run(execute=True)
        self.clock.now += timedelta(hours=25)
        self.game.calls.clear()
        self.scheduler().run(execute=True)
        self.assertIn("collectDailyReward", self.game.calls)

    def test_stop_file_halts_before_any_action(self):
        stop = Path(self.tmp.name) / "STOP"
        stop.touch()
        report = self.scheduler(stop_file=stop).run(execute=True)
        self.assertEqual(self.game.calls, [])
        self.assertIn("operator stop file present", report.stop_reason)

    def test_action_budget_caps_attempts(self):
        report = self.scheduler(max_actions=2).run(execute=True)
        self.assertEqual(len(report.results), 2)
        self.assertEqual(report.stop_reason, "action budget reached (2)")

    def test_contradicted_outcome_stops_the_run(self):
        self.game.broken.add("collectAllResources")
        report = self.scheduler().run(execute=True)
        self.assertEqual(report.results[-1].outcome, "contradicted")
        self.assertIn("collect_all_resources contradicted", report.stop_reason)
        self.assertNotIn("collectDailyReward", self.game.calls)

    def test_uncertain_non_idempotent_outcome_stops_the_run(self):
        self.game.broken.add("upgradeRoom")

        def ambiguous(room_id, design_id):
            self.game.calls.append("upgradeRoom")
            return object()

        self.game.upgradeRoom = ambiguous
        report = self.scheduler().run(execute=True)
        self.assertEqual(report.results[-1].outcome, "unknown")
        self.assertIn("upgrade_room outcome uncertain", report.stop_reason)
        self.assertEqual(self.game.calls, ["addResearch", "upgradeRoom"])

    def test_runner_refusal_stops_the_run(self):
        self.journal.append(
            GameplayEvent.create(
                account_alias="alpha", action_id="add_research", started_at=T0, finished_at=T0,
                outcome="unknown", reason="earlier timeout", attempted=True,
                params={"research_design_id": "510"},
            )
        )
        report = self.scheduler().run(execute=True)
        self.assertIn("runner refused add_research", report.stop_reason)
        self.assertEqual(self.game.calls, [])

    def test_snapshot_for_another_account_is_refused(self):
        self.game.alias = "bravo"
        with self.assertRaises(AccountMismatchError):
            self.scheduler().run()


if __name__ == "__main__":
    unittest.main()
