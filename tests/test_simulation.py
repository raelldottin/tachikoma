"""Tests for sdk.simulation: deterministic offline scenarios through the real stack. No network."""

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdk.journal import GameplayJournal
from sdk.simulation import SCENARIO_DIR, load_scenario, run_scenario


def run(name, **kwargs):
    with tempfile.TemporaryDirectory() as tmp:
        result = run_scenario(load_scenario(name), tmp, **kwargs)
        journals = {path.stem: GameplayJournal(path).read() for path in Path(tmp).glob("*.jsonl")}
    return result, journals


class ScenarioTests(unittest.TestCase):
    def test_shipped_scenarios_exist(self):
        self.assertEqual(sorted(p.stem for p in SCENARIO_DIR.glob("*.json")), ["single-account", "two-accounts"])

    def test_same_scenario_and_seed_give_identical_reports(self):
        for name in ("single-account", "two-accounts"):
            with self.subTest(name=name):
                first, _ = run(name)
                second, _ = run(name)
                self.assertEqual(first.progression_json, second.progression_json)
                self.assertEqual(first.fleet_json, second.fleet_json)
                self.assertEqual(first.audit_json, second.audit_json)
                self.assertEqual(first.calls, second.calls)

    def test_a_different_seed_changes_the_outcome(self):
        default, _ = run("single-account")
        other, _ = run("single-account", seed=8)
        self.assertNotEqual(default.progression_json, other.progression_json)

    def test_single_account_progresses_within_budget(self):
        result, journals = run("single-account")
        self.assertEqual(result.calls["alpha"].count("upgradeRoom"), 2)  # 300 + 400 within the 800 budget
        self.assertEqual([run.accounts[0].status for run in result.fleet_runs], ["completed"] * 3)
        fleet = json.loads(result.fleet_json)
        self.assertEqual(fleet["mode"], "sequential")
        self.assertEqual(fleet["fleet"]["attempted"], fleet["fleet"]["verified"])
        spent = sum(-(e.cost.get("minerals_delta") or 0) for e in journals["alpha"]
                    if (e.cost.get("minerals_delta") or 0) < 0)
        self.assertLessEqual(spent, 800)

    def test_two_accounts_stay_isolated(self):
        result, journals = run("two-accounts")
        self.assertEqual(json.loads(result.audit_json)["incidents"], [])
        for alias, events in journals.items():
            self.assertEqual({event.account_alias for event in events}, {alias})
        self.assertNotIn("upgradeRoom", result.calls["bravo"])  # 100-mineral budget is below the 300 upgrade

    def test_attempt_ids_are_renumbered_in_run_order(self):
        from datetime import datetime, timedelta, timezone

        from sdk.journal import GameplayEvent
        from sdk.simulation.game import _renumber

        t0 = datetime(2027, 6, 1, tzinfo=timezone.utc)

        def event(alias, minutes):
            start = t0 + timedelta(minutes=minutes)
            return GameplayEvent.create(account_alias=alias, action_id="x", started_at=start, finished_at=start,
                                        outcome="unknown", reason="r", attempted=True)

        journals = {"bravo": [event("bravo", 1)], "alpha": [event("alpha", 0), event("alpha", 2)]}
        renumbered = _renumber(journals)
        self.assertEqual([e.attempt_id for e in renumbered["alpha"]], ["sim-00001", "sim-00003"])
        self.assertEqual([e.attempt_id for e in renumbered["bravo"]], ["sim-00002"])

    def test_no_network_is_used(self):
        def blocked(*args, **kwargs):
            raise AssertionError("network access attempted")

        with patch.object(socket, "socket", blocked), patch.object(socket, "create_connection", blocked):
            result, _ = run("two-accounts")
        self.assertTrue(result.calls["alpha"])


if __name__ == "__main__":
    unittest.main()
