"""Regression tests: an attempt whose effect was not observed still charges its
planned cost against fleet budgets. Fake games, no network."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from sdk.execution import ActionRunner
from sdk.fleet import registry_from_dict
from sdk.fleet.audit import audit_fleet
from sdk.fleet.orchestrator import AccountContext, FleetOrchestrator, spent_in_period
from sdk.journal import GameplayEvent, GameplayJournal
from sdk.policy import get_policy
from sdk.reports.progression import compute_progression_metrics
from sdk.scheduler import DailyScheduler
from tests.test_fleet_orchestrator import DESIGN, T0, Clock, FakeGame, profile

UPGRADE = {"room_id": "r1", "upgrade_room_design_id": "101"}


class FailingAfterUpgrade:
    """Snapshot provider whose first read after ``upgradeRoom`` times out."""

    def __init__(self, game):
        self.game = game
        self.failed = False

    def __call__(self):
        if "upgradeRoom" in self.game.calls and not self.failed:
            self.failed = True
            raise TimeoutError("snapshot timed out")
        return self.game.snapshot()


def event(start_minutes=0, attempted=True, **cost):
    begin = T0 + timedelta(minutes=start_minutes)
    return GameplayEvent.create(
        account_alias="alpha", action_id="upgrade_room", started_at=begin, finished_at=begin + timedelta(minutes=1),
        outcome="unknown" if attempted else "skipped", reason="synthetic", attempted=attempted, cost=cost,
    )


class RunnerUncertainCostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = GameplayJournal(Path(self.tmp.name) / "alpha.jsonl")
        self.clock = Clock()
        self.game = FakeGame("alpha", self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, provider=None):
        return ActionRunner("alpha", self.game, provider or self.game.snapshot, self.journal, clock=self.clock)

    def test_after_snapshot_failure_records_the_planned_cost_as_uncertain(self):
        result = self.runner(FailingAfterUpgrade(self.game)).run("upgrade_room", UPGRADE,
                                                                 expected_cost={"mineral": 300})
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(dict(result.event.cost), {"minerals_uncertain": 300})
        self.assertEqual(dict(self.journal.read()[-1].cost), {"minerals_uncertain": 300})

    def test_call_exception_and_failed_after_snapshot_record_uncertain_cost(self):
        def raising(room_id, design_id):
            self.game.calls.append("upgradeRoom")
            raise ConnectionError("reset by peer")

        self.game.upgradeRoom = raising
        result = self.runner(FailingAfterUpgrade(self.game)).run("upgrade_room", UPGRADE,
                                                                 expected_cost={"gas": 50})
        self.assertEqual((result.outcome, result.event.error_class), ("unknown", "ConnectionError"))
        self.assertEqual(dict(result.event.cost), {"gas_uncertain": 50})

    def test_unobserved_resource_delta_records_uncertain_and_keeps_observed_deltas(self):
        def upgrade_hiding_minerals(room_id, design_id):
            self.game.calls.append("upgradeRoom")
            resources = {k: v for k, v in self.game.state["resources"].items() if k != "minerals"}
            self.game.state["resources"] = resources
            self.game.state["rooms"] = [{"@RoomId": room_id, "@RoomDesignId": "100", "@RoomStatus": "Upgrading"}]
            return True

        self.game.upgradeRoom = upgrade_hiding_minerals
        result = self.runner().run("upgrade_room", UPGRADE, expected_cost={"mineral": 300, "gas": 10})
        cost = dict(result.event.cost)
        self.assertIsNone(cost["minerals_delta"])
        self.assertEqual(cost["minerals_uncertain"], 300)
        self.assertEqual(cost["gas_delta"], 0)  # observed: no gas_uncertain
        self.assertNotIn("gas_uncertain", cost)

    def test_observed_spend_is_unchanged(self):
        result = self.runner().run("upgrade_room", UPGRADE, expected_cost={"mineral": 300})
        self.assertEqual(result.outcome, "verified")
        self.assertEqual(dict(result.event.cost), {"minerals_delta": -300, "gas_delta": 0, "starbux_delta": 0})

    def test_no_uncertain_cost_on_dry_run_skip_or_without_expected_cost(self):
        dry = self.runner().run("upgrade_room", UPGRADE, dry_run=True, expected_cost={"mineral": 300})
        self.assertEqual(dict(dry.event.cost), {})
        self.game.state["rooms"] = [{"@RoomId": "r1", "@RoomDesignId": "100", "@RoomStatus": "Upgrading"}]
        skipped = self.runner().run("upgrade_room", UPGRADE, expected_cost={"mineral": 300})
        self.assertEqual((skipped.outcome, dict(skipped.event.cost)), ("skipped", {}))
        self.game.state["rooms"] = [{"@RoomId": "r1", "@RoomDesignId": "100", "@RoomStatus": "Normal"}]
        unplanned = self.runner(FailingAfterUpgrade(self.game)).run("upgrade_room", UPGRADE)
        self.assertEqual((unplanned.outcome, dict(unplanned.event.cost)), ("unknown", {}))


class SchedulerAndFleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def test_scheduler_passes_each_requests_expected_cost(self):
        game = FakeGame("alpha", self.clock)
        journal = GameplayJournal(self.dir / "alpha.jsonl")
        runner = ActionRunner("alpha", game, FailingAfterUpgrade(game), journal, clock=self.clock)
        report = DailyScheduler(runner, get_policy("baseline-v1"), DESIGN, clock=self.clock).run(execute=True)
        upgrade = [r for r in report.results if r.action_id == "upgrade_room"][0]
        self.assertEqual(upgrade.outcome, "unknown")
        self.assertEqual(dict(upgrade.event.cost), {"minerals_uncertain": 300})

    def test_uncertain_attempt_is_charged_against_the_next_runs_budget(self):
        # The upgrade really spends 300 minerals but its after-snapshot fails.
        game = FakeGame("alpha", self.clock)
        provider = FailingAfterUpgrade(game)
        journal_path = self.dir / "alpha.jsonl"
        registry = registry_from_dict({"version": 1, "accounts": [
            profile("alpha", budgets={"starbux": 0, "minerals": 300})]})

        def factory(_profile):
            return AccountContext(game, provider, GameplayJournal(journal_path), DESIGN)

        orchestrator = FleetOrchestrator(registry, factory, clock=self.clock, timer=lambda: 0.0)
        orchestrator.run(execute=True)
        self.assertEqual(game.state["resources"]["minerals"], "700")
        self.assertEqual(spent_in_period(GameplayJournal(journal_path), "alpha", T0), {"minerals": 300})

        second = orchestrator.run(execute=True).accounts[0]
        self.assertEqual(second.spent_this_period["minerals"], 300)
        self.assertEqual(second.budget_remaining["minerals"], 0)


class ChargedSpendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = GameplayJournal(Path(self.tmp.name) / "alpha.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_spent_in_period_charges_uncertain_only_when_the_delta_is_unobserved(self):
        for item in (
            event(0, minerals_uncertain=300),                        # after-snapshot failed
            event(2, minerals_delta=None, gas_delta=-5, minerals_uncertain=200, gas_uncertain=99),
            event(4, minerals_delta=-100, gas_delta=0, starbux_delta=0),  # old event, observed
            event(6, attempted=False, minerals_uncertain=1000),      # never charged
        ):
            self.journal.append(item)
        self.assertEqual(spent_in_period(self.journal, "alpha", T0 - timedelta(hours=1)),
                         {"minerals": 600, "gas": 5})

    def test_audit_counts_uncertain_attempts_toward_budget_exceeded(self):
        registry = registry_from_dict({"version": 1, "accounts": [
            profile("alpha", budgets={"starbux": 0, "minerals": 500})]})
        report = audit_fleet(registry, {"alpha": [event(0, minerals_uncertain=300),
                                                  event(5, minerals_delta=-300)]})
        incidents = [i for i in report.incidents if i.rule == "budget-exceeded"]
        self.assertEqual(len(incidents), 1)
        self.assertIn("spent 600 minerals (300 of it from attempts whose effect was not observed)",
                      incidents[0].detail)
        self.assertEqual(len(incidents[0].attempt_ids), 2)

    def test_audit_ignores_uncertain_when_the_delta_was_observed(self):
        registry = registry_from_dict({"version": 1, "accounts": [
            profile("alpha", budgets={"starbux": 0, "minerals": 500})]})
        report = audit_fleet(registry, {"alpha": [event(0, minerals_delta=-300, minerals_uncertain=900)]})
        self.assertTrue(report.clean)

    def test_progression_report_counts_observed_deltas_only(self):
        metrics = compute_progression_metrics([event(0, minerals_uncertain=300),
                                               event(5, minerals_delta=-100, gas_delta=0)])
        self.assertEqual(metrics[0].resources_spent["minerals"], 100)
        self.assertEqual(metrics[0].resource_samples, 1)


if __name__ == "__main__":
    unittest.main()
