"""Tests for sdk.fleet.orchestrator: sequential, isolated, budgeted fleet runs. Fake games, no network."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.fleet import registry_from_dict
from sdk.fleet.orchestrator import AccountContext, FleetOrchestrator, budgeted_policy_config, spent_in_period
from sdk.journal import GameplayEvent, GameplayJournal
from sdk.policy import DesignData, PolicyConfig
from sdk.state import build_snapshot
from tests.synthetic_fixtures import SYNTHETIC_EMAIL

T0 = datetime(2027, 4, 1, 6, 0, tzinfo=timezone.utc)
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
    def __init__(self, alias, clock, minerals="1000"):
        self.alias = alias
        self.snapshot_alias = alias
        self.clock = clock
        self.calls = []
        self.state = dict(resources={"minerals": minerals, "gas": "1000", "starbux": "5"},
                          rooms=[{"@RoomId": "r1", "@RoomDesignId": "100", "@RoomStatus": "Normal"}],
                          research=[], inventory=[])

    def snapshot(self):
        return build_snapshot(self.snapshot_alias, self.clock(), "fake-game", **self.state)

    def addResearch(self, design_id):
        self.calls.append("addResearch")
        self.state["research"] = [{"@ResearchDesignId": design_id, "@ResearchState": "Researching"}]
        return True

    def upgradeRoom(self, room_id, design_id):
        self.calls.append("upgradeRoom")
        minerals = int(self.state["resources"]["minerals"]) - 300
        self.state["resources"] = dict(self.state["resources"], minerals=str(minerals))
        self.state["rooms"] = [{"@RoomId": room_id, "@RoomDesignId": "100", "@RoomStatus": "Upgrading"}]
        return True

    def collectAllResources(self):
        self.calls.append("collectAllResources")
        minerals = int(self.state["resources"]["minerals"]) + 25
        self.state["resources"] = dict(self.state["resources"], minerals=str(minerals))
        return True

    def collectDailyReward(self):
        self.calls.append("collectDailyReward")
        self.state["resources"] = dict(self.state["resources"], starbux="6")
        return True


def profile(alias, **overrides):
    entry = {"alias": alias, "objective": "grow", "policy_id": "baseline-v1",
             "credentials_secret": f"{alias.upper()}_AUTH", "budgets": {"starbux": 0}}
    entry.update(overrides)
    return entry


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.clock = Clock()
        self.games = {}

    def tearDown(self):
        self.tmp.cleanup()

    def factory(self, profile):
        game = self.games.setdefault(profile.alias, FakeGame(profile.alias, self.clock))
        return AccountContext(game, game.snapshot, GameplayJournal(self.dir / f"{profile.alias}.jsonl"), DESIGN)

    def orchestrator(self, *entries, factory=None, **kwargs):
        registry = registry_from_dict({"version": 1, "accounts": list(entries)})
        return FleetOrchestrator(registry, factory or self.factory, clock=self.clock, timer=lambda: 0.0, **kwargs)

    def test_dry_run_is_the_default_and_calls_nothing(self):
        run = self.orchestrator(profile("alpha"), profile("bravo")).run()
        self.assertTrue(run.dry_run)
        self.assertEqual(run.mode, "sequential")
        self.assertEqual([a.status for a in run.accounts], ["completed", "completed"])
        self.assertEqual([a.attempted for a in run.accounts], [0, 0])
        self.assertTrue(all(not game.calls for game in self.games.values()))

    def test_execute_runs_each_account_against_its_own_game(self):
        run = self.orchestrator(profile("alpha"), profile("bravo")).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["completed", "completed"])
        for alias in ("alpha", "bravo"):
            self.assertIn("addResearch", self.games[alias].calls)
            events = GameplayJournal(self.dir / f"{alias}.jsonl").read()
            self.assertTrue(events)
            self.assertEqual({event.account_alias for event in events}, {alias})

    def test_disabled_accounts_are_not_touched(self):
        run = self.orchestrator(profile("alpha"), profile("bravo", enabled=False)).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["completed", "disabled"])
        self.assertNotIn("bravo", self.games)

    def test_one_accounts_failure_does_not_stop_the_next(self):
        def factory(profile):
            if profile.alias == "alpha":
                raise ConnectionError(f"login failed for {SYNTHETIC_EMAIL}")
            return self.factory(profile)

        run = self.orchestrator(profile("alpha"), profile("bravo"), factory=factory).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["error", "completed"])
        self.assertIn("ConnectionError", run.accounts[0].reason)
        self.assertNotIn(SYNTHETIC_EMAIL, run.accounts[0].reason)

    def test_foreign_snapshot_is_refused_without_calls(self):
        self.games["alpha"] = FakeGame("alpha", self.clock)
        self.games["alpha"].snapshot_alias = "bravo"
        run = self.orchestrator(profile("alpha"), profile("bravo")).run(execute=True)
        self.assertEqual(run.accounts[0].status, "error")
        self.assertEqual(self.games["alpha"].calls, [])
        self.assertEqual(run.accounts[1].status, "completed")

    def test_shared_journal_is_refused(self):
        def factory(profile):
            game = self.games.setdefault(profile.alias, FakeGame(profile.alias, self.clock))
            return AccountContext(game, game.snapshot, GameplayJournal(self.dir / "shared.jsonl"), DESIGN)

        run = self.orchestrator(profile("alpha"), profile("bravo"), factory=factory).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["completed", "error"])
        self.assertIn("already used by alpha", run.accounts[1].reason)
        self.assertEqual(self.games["bravo"].calls, [])

    def test_fleet_stop_file_stops_remaining_accounts(self):
        stop = self.dir / "STOP"
        stop.write_text("stop")
        run = self.orchestrator(profile("alpha"), profile("bravo"), stop_file=stop).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["not-run", "not-run"])
        self.assertIn("stop file", run.stop_reason)
        self.assertEqual(self.games, {})

    def test_budget_keeps_the_plan_within_remaining_spend(self):
        run = self.orchestrator(profile("alpha", budgets={"minerals": 200, "starbux": 0})).run(execute=True)
        [account] = run.accounts
        self.assertNotIn("upgradeRoom", self.games["alpha"].calls)
        self.assertEqual(account.budget_remaining, {"minerals": 200, "starbux": 0})

    def test_budget_counts_spend_already_journaled_this_period(self):
        journal = GameplayJournal(self.dir / "alpha.jsonl")
        journal.append(GameplayEvent.create(
            account_alias="alpha", action_id="upgrade_room", started_at=T0 - timedelta(hours=1),
            finished_at=T0 - timedelta(hours=1), outcome="verified", reason="earlier", attempted=True,
            cost={"minerals_delta": -900, "gas_delta": 0, "starbux_delta": 0}))
        run = self.orchestrator(profile("alpha", budgets={"minerals": 1000})).run(execute=True)
        self.assertEqual(run.accounts[0].spent_this_period, {"minerals": 900})
        self.assertNotIn("upgradeRoom", self.games["alpha"].calls)

    def test_balance_growth_before_the_planning_snapshot_adds_no_budget(self):
        # The orchestrator's first snapshot sees 100 minerals; by the scheduler's planning
        # snapshot the balance is 1000. A 50-mineral budget must still refuse the 300 upgrade.
        game = FakeGame("alpha", self.clock, minerals="100")
        snapshots = []

        def snapshot():
            snapshots.append(1)
            if len(snapshots) == 2:
                game.state["resources"] = dict(game.state["resources"], minerals="1000")
            return game.snapshot()

        def factory(profile):
            return AccountContext(game, snapshot, GameplayJournal(self.dir / "alpha.jsonl"), DESIGN)

        run = self.orchestrator(profile("alpha", budgets={"minerals": 50, "starbux": 0}),
                                factory=factory).run(execute=True)
        self.assertEqual(run.accounts[0].status, "completed")
        self.assertNotIn("upgradeRoom", game.calls)
        journal = GameplayJournal(self.dir / "alpha.jsonl")
        self.assertNotIn("minerals", spent_in_period(journal, "alpha", T0))


class BudgetHelperTests(unittest.TestCase):
    def test_caps_and_starbux(self):
        registry = registry_from_dict({"version": 1, "accounts": [
            profile("alpha", budgets={"minerals": 300, "gas": 2000}, max_actions=5),
            profile("bravo", budgets={}),
        ]})
        base = PolicyConfig(allow_starbux_spend=True, mineral_reserve=40, gas_reserve=7, max_gas_spend=1500)
        config, remaining = budgeted_policy_config(registry.get("alpha"), {"minerals": 100}, base)
        # Remaining budget becomes an absolute spend cap; the base reserves and a tighter base cap are kept.
        self.assertEqual((config.max_mineral_spend, config.max_gas_spend), (200, 1500))
        self.assertEqual((config.mineral_reserve, config.gas_reserve), (40, 7))
        self.assertEqual(remaining, {"minerals": 200, "gas": 2000, "starbux": 0})
        self.assertFalse(config.allow_starbux_spend)
        self.assertEqual(config.max_requests, 5)
        unlimited, remaining = budgeted_policy_config(registry.get("bravo"), {}, PolicyConfig(allow_starbux_spend=True))
        self.assertFalse(unlimited.allow_starbux_spend)
        self.assertEqual((unlimited.max_mineral_spend, unlimited.max_gas_spend, unlimited.mineral_reserve),
                         (None, None, 0))
        self.assertEqual(remaining, {"starbux": 0})

    def test_spent_in_period_ignores_old_foreign_and_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = GameplayJournal(Path(tmp) / "j.jsonl")
            for alias, hours, attempted in (("alpha", 1, True), ("alpha", 30, True), ("bravo", 1, True),
                                            ("alpha", 1, False)):
                journal.append(GameplayEvent.create(
                    account_alias=alias, action_id="x", started_at=T0 - timedelta(hours=hours),
                    finished_at=T0 - timedelta(hours=hours), outcome="verified" if attempted else "skipped",
                    reason="r", attempted=attempted, cost={"minerals_delta": -10, "gas_delta": 5}))
            self.assertEqual(spent_in_period(journal, "alpha", T0 - timedelta(hours=24)), {"minerals": 10})


if __name__ == "__main__":
    unittest.main()
