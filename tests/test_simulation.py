"""Tests for sdk.simulation: deterministic offline scenarios through the real stack. No network."""

import copy
import json
import random
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sdk.fleet import registry_from_dict
from sdk.fleet.audit import audit_fleet
from sdk.fleet.orchestrator import spent_in_period
from sdk.journal import GameplayJournal
from sdk.policy import DesignData
from sdk.simulation import (
    SCENARIO_DIR,
    Fault,
    SimulatedClock,
    SimulatedFaultError,
    SimulatedGame,
    load_scenario,
    run_scenario,
)

BASELINE_SCENARIOS = ("single-account", "two-accounts")
FAULT_SCENARIOS = ("faults-contradicted-and-foreign", "faults-uncertain-outcomes")


def run(name, **kwargs):
    scenario = name if isinstance(name, dict) else load_scenario(name)
    with tempfile.TemporaryDirectory() as tmp:
        result = run_scenario(scenario, tmp, **kwargs)
        journals = {path.stem: GameplayJournal(path).read() for path in Path(tmp).glob("*.jsonl")}
    return result, journals


def reports(result):
    return result.progression_json, result.fleet_json, result.audit_json, result.calls, result.faults


def on_day(events, day):
    return [event for event in events if event.finished_at.startswith(f"2027-06-0{day + 1}")]


class ScenarioTests(unittest.TestCase):
    def test_shipped_scenarios_exist(self):
        self.assertEqual(sorted(p.stem for p in SCENARIO_DIR.glob("*.json")),
                         sorted(BASELINE_SCENARIOS + FAULT_SCENARIOS))

    def test_same_scenario_and_seed_give_identical_reports(self):
        for name in BASELINE_SCENARIOS + FAULT_SCENARIOS:
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

    def test_reused_workdir_is_refused(self):
        # Regression: a second run appended to the first run's journals, changing every report.
        with tempfile.TemporaryDirectory() as tmp:
            first = run_scenario(load_scenario("two-accounts"), tmp)
            journal = Path(tmp) / "alpha.jsonl"
            before = journal.read_bytes()
            with self.assertRaisesRegex(FileExistsError, "alpha.jsonl"):
                run_scenario(load_scenario("two-accounts"), tmp)
            self.assertEqual(journal.read_bytes(), before)
        fresh, _ = run("two-accounts")
        self.assertEqual(first.progression_json, fresh.progression_json)

    def test_unrelated_files_in_workdir_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "notes.txt").write_text("kept")
            (Path(tmp) / "charlie.jsonl").write_text("")  # not an account in this scenario
            result = run_scenario(load_scenario("two-accounts"), tmp)
            self.assertEqual((Path(tmp) / "notes.txt").read_text(), "kept")
        self.assertEqual(result.progression_json, run("two-accounts")[0].progression_json)

    def test_no_network_is_used(self):
        def blocked(*args, **kwargs):
            raise AssertionError("network access attempted")

        with patch.object(socket, "socket", blocked), patch.object(socket, "create_connection", blocked):
            result, _ = run("two-accounts")
        self.assertTrue(result.calls["alpha"])


class FaultScenarioTests(unittest.TestCase):
    """Injected faults, end to end through the runner, scheduler, orchestrator and audit."""

    def test_baseline_scenarios_have_no_faults_and_an_empty_fault_list_changes_nothing(self):
        for name in BASELINE_SCENARIOS:
            with self.subTest(name=name):
                scenario = load_scenario(name)
                self.assertFalse(any(state.get("faults") for state in scenario["accounts"].values()))
                inert = copy.deepcopy(scenario)
                for state in inert["accounts"].values():
                    state["faults"] = []
                plain, _ = run(name)
                self.assertEqual(reports(run(inert)[0]), reports(plain))
                self.assertEqual(plain.faults, {alias: () for alias in scenario["accounts"]})

    def test_fault_reports_are_byte_identical_for_the_same_seed(self):
        for name in FAULT_SCENARIOS:
            with self.subTest(name=name):
                first, second = run(name)[0], run(name)[0]
                self.assertEqual(reports(first), reports(second))
                self.assertTrue(any(first.faults.values()))

    def test_unobserved_effect_is_unknown_not_verified(self):
        # bravo's upgrade takes effect, then the call raises and its after-snapshot fails.
        result, journals = run("faults-uncertain-outcomes")
        self.assertIn("day 0 upgradeRoom #1: snapshot-failure", result.faults["bravo"])
        upgrade = next(e for e in journals["bravo"] if e.action_id == "upgrade_room")
        self.assertEqual((upgrade.outcome, upgrade.attempted), ("unknown", True))
        self.assertEqual(upgrade.error_class, "SimulatedFaultError")
        self.assertIsNone(upgrade.response_ok)
        self.assertIn("after-snapshot failed", upgrade.reason)
        self.assertIsNone(upgrade.cost.get("minerals_delta"))  # unobserved, never 0
        bravo_row = next(row for row in json.loads(result.fleet_json)["accounts"] if row["account_alias"] == "bravo")
        self.assertEqual(bravo_row["throughput"]["uncertain"], 1)
        self.assertEqual(result.fleet_runs[0].accounts[1].status, "stopped")

    def test_observed_effect_after_a_raise_is_verified_from_state(self):
        result, journals = run("faults-uncertain-outcomes")
        self.assertIn("day 0 addResearch #1: raise", result.faults["bravo"])
        research = journals["bravo"][0]
        self.assertEqual((research.action_id, research.outcome), ("add_research", "verified"))
        self.assertEqual(research.error_class, "SimulatedFaultError")
        self.assertIsNone(research.response_ok)
        self.assertEqual(research.cost["gas_delta"], -50)

    def test_non_idempotent_retry_is_blocked_after_an_ambiguous_response(self):
        result, journals = run("faults-uncertain-outcomes")
        first, retry = [e for e in journals["alpha"] if e.params == {"research_design_id": "520"}]
        self.assertEqual(first.outcome, "unknown")
        self.assertIn("ambiguous response", first.reason)
        self.assertIsNone(first.response_ok)
        self.assertEqual((retry.outcome, retry.attempted), ("skipped", False))
        self.assertEqual(retry.error_class, "UncertainPriorAttemptError")
        self.assertEqual(result.calls["alpha"], ("addResearch", "addResearch"))  # never called again
        day1 = result.fleet_runs[1].accounts[0]
        self.assertEqual(day1.status, "stopped")
        self.assertIn("uncertain prior attempt", day1.reason)

    def test_uncertain_spend_is_charged_against_budgets(self):
        result, journals = run("faults-uncertain-outcomes")
        upgrade = next(e for e in journals["bravo"] if e.action_id == "upgrade_room")
        self.assertEqual(upgrade.cost, {"minerals_uncertain": 300})
        day1 = result.fleet_runs[1].accounts[1]
        self.assertEqual(day1.spent_this_period, {"gas": 170, "minerals": 300})
        self.assertEqual(day1.budget_remaining["minerals"], 500)
        self.assertEqual(result.calls["bravo"].count("upgradeRoom"), 1)  # the 600-mineral upgrade no longer fits
        with tempfile.TemporaryDirectory() as tmp:
            journal = GameplayJournal(Path(tmp) / "bravo.jsonl")
            for event in journals["bravo"]:
                journal.append(event)
            since = datetime.fromisoformat(upgrade.finished_at)
            self.assertEqual(spent_in_period(journal, "bravo", since), {"minerals": 300})
        registry = load_scenario("faults-uncertain-outcomes")["registry"]
        registry["accounts"][1]["budgets"]["minerals"] = 299
        audit = audit_fleet(registry_from_dict(registry), journals)
        (incident,) = [i for i in audit.incidents if i.rule == "budget-exceeded"]
        self.assertEqual((incident.account_alias, incident.attempt_ids), ("bravo", (upgrade.attempt_id,)))
        self.assertIn("spent 300 minerals (300 of it from attempts whose effect was not observed)", incident.detail)

    def test_foreign_snapshot_ends_that_account_with_no_calls_while_others_continue(self):
        result, journals = run("faults-contradicted-and-foreign")
        self.assertEqual(result.faults["alpha"], ("day 0 snapshot #1: foreign-snapshot",))
        alpha_day0, bravo_day0 = result.fleet_runs[0].accounts
        self.assertEqual((alpha_day0.status, alpha_day0.reason, alpha_day0.attempted),
                         ("error", "snapshot belongs to 'bravo'", 0))
        self.assertEqual(on_day(journals["alpha"], 0), [])
        self.assertGreater(bravo_day0.attempted, 0)
        self.assertEqual(result.fleet_runs[1].accounts[0].status, "completed")  # the next day runs normally
        for alias, events in journals.items():
            self.assertEqual({event.account_alias for event in events}, {alias})
        self.assertEqual(json.loads(result.audit_json)["incidents"], [])

    def test_foreign_after_snapshot_is_unknown_and_stops_the_account(self):
        scenario = load_scenario("two-accounts")
        scenario["accounts"]["alpha"]["faults"] = [
            {"kind": "foreign-snapshot", "method": "upgradeRoom", "day": 0, "call": 1, "alias": "bravo"}]
        result, journals = run(scenario)
        upgrade = next(e for e in journals["alpha"] if e.action_id == "upgrade_room")
        self.assertEqual((upgrade.outcome, upgrade.error_class), ("unknown", "AccountMismatchError"))
        self.assertEqual(upgrade.cost, {"minerals_uncertain": 300})
        self.assertEqual(result.fleet_runs[0].accounts[0].status, "stopped")
        self.assertEqual(result.fleet_runs[0].accounts[1].status, "completed")

    def test_concurrent_player_spend_contradicts_the_collection_and_stops_the_account(self):
        result, journals = run("faults-contradicted-and-foreign")
        self.assertEqual(result.faults["bravo"], ("day 0 collectAllResources #1: concurrent-spend",))
        day0 = on_day(journals["bravo"], 0)
        self.assertEqual((day0[-1].action_id, day0[-1].outcome), ("collect_all_resources", "contradicted"))
        self.assertLess(day0[-1].cost["minerals_delta"], 0)  # the player's spend is observed, and charged
        bravo_day0 = result.fleet_runs[0].accounts[1]
        self.assertEqual(bravo_day0.status, "stopped")
        self.assertIn("contradicted", bravo_day0.reason)
        self.assertLess(bravo_day0.attempted, bravo_day0.planned)
        self.assertNotIn("collect_daily_reward", [e.action_id for e in day0])
        self.assertIn("collect_daily_reward", [e.action_id for e in on_day(journals["bravo"], 1)])

    def test_probabilistic_faults_follow_the_seed(self):
        scenario = load_scenario("single-account")
        scenario["accounts"]["alpha"]["faults"] = [
            {"kind": "ambiguous-response", "method": "collectDailyReward", "probability": 0.5}]
        half = run(scenario)[0]
        self.assertEqual(reports(half), reports(run(scenario)[0]))
        self.assertLessEqual(len(half.faults["alpha"]), half.calls["alpha"].count("collectDailyReward"))
        del scenario["accounts"]["alpha"]["faults"][0]["probability"]
        always = run(scenario)[0]
        self.assertEqual(len(always.faults["alpha"]), always.calls["alpha"].count("collectDailyReward"))


class FaultTests(unittest.TestCase):
    def game(self, *faults):
        design = DesignData.from_records(research_designs=[{"@ResearchDesignId": "510", "@GasCost": "50"}])
        clock = SimulatedClock(datetime(2027, 6, 1, tzinfo=timezone.utc))
        return SimulatedGame("alpha", {"minerals": 100, "gas": 100}, design, random.Random(0), clock, faults=faults)

    def test_invalid_faults_are_refused(self):
        for fault, message in (
            ({"kind": "explode", "method": "addResearch"}, "unknown fault kind"),
            ({"kind": "raise", "method": "sendMessage"}, "unknown method"),
            ({"kind": "raise", "method": "snapshot"}, "client method"),
            ({"kind": "foreign-snapshot", "method": "snapshot"}, "alias"),
            ({"kind": "concurrent-spend", "method": "snapshot"}, "positive"),
        ):
            with self.subTest(fault=fault), self.assertRaisesRegex(ValueError, message):
                Fault.from_dict(fault)

    def test_malformed_fault_fields_fail_closed(self):
        # Regression: these were accepted silently. "calls" was ignored, so the fault fired on
        # every call; "false" was truthy; a negative spend added gas; probability 7 always fired.
        raise_ = {"kind": "raise", "method": "addResearch"}
        spend = {"kind": "concurrent-spend", "method": "snapshot"}
        for fault, message in (
            (dict(raise_, calls=2), "unknown or inapplicable keys: calls"),
            (dict(raise_, alias="bravo"), "inapplicable keys: alias"),
            (dict(spend, minerals=10, effect=False), "inapplicable keys: effect"),
            (dict(raise_, effect="false"), "effect must be true or false"),
            (dict(raise_, effect=0), "effect must be true or false"),
            (dict(spend, gas=-500), "gas must be a non-negative integer"),
            (dict(spend, minerals=10, gas=-1), "gas must be a non-negative integer"),
            (dict(spend, minerals="500"), "minerals must be a non-negative integer"),
            (dict(spend, minerals=2.5), "minerals must be a non-negative integer"),
            (dict(spend, minerals=True), "minerals must be a non-negative integer"),
            (dict(raise_, probability=7), "probability must be a number in"),
            (dict(raise_, probability=0), "probability must be a number in"),
            (dict(raise_, probability=-0.5), "probability must be a number in"),
            (dict(raise_, probability="0.5"), "probability must be a number in"),
            (dict(raise_, probability=float("nan")), "probability must be a number in"),
            (dict(raise_, call=0), "call must be a positive integer"),
            (dict(raise_, call=-1), "call must be a positive integer"),
            (dict(raise_, call="2"), "call must be a positive integer"),
            (dict(raise_, call=1.5), "call must be a positive integer"),
            (dict(raise_, call=True), "call must be a positive integer"),
            (dict(raise_, day=-1), "day must be a non-negative integer"),
            (dict(raise_, day="0"), "day must be a non-negative integer"),
            (dict(raise_, day=False), "day must be a non-negative integer"),
            ({"kind": "foreign-snapshot", "method": "snapshot", "alias": 7}, "needs the alias"),
        ):
            with self.subTest(fault=fault), self.assertRaisesRegex(ValueError, message):
                Fault.from_dict(fault)
        with self.assertRaisesRegex(ValueError, "probability"):
            Fault("raise", "addResearch", probability=7)  # constructing directly is checked too

    def test_well_formed_faults_are_accepted(self):
        self.assertEqual(Fault.from_dict({"kind": "raise", "method": "addResearch", "day": 0, "call": 1,
                                          "probability": 1, "effect": False}),
                         Fault("raise", "addResearch", day=0, call=1, probability=1, effect=False))
        self.assertEqual(Fault.from_dict({"kind": "concurrent-spend", "method": "snapshot", "minerals": 0,
                                          "gas": 5}).spend, {"minerals": 0, "gas": 5})
        self.assertEqual(Fault.from_dict({"kind": "raise", "method": "addResearch", "day": None}).day, None)

    def test_scenario_with_a_malformed_fault_is_refused_before_anything_runs(self):
        scenario = load_scenario("two-accounts")
        scenario["accounts"]["bravo"]["faults"] = [{"kind": "raise", "method": "addResearch", "calls": 2}]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "calls"):
                run_scenario(scenario, tmp)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_raise_without_effect_changes_nothing(self):
        game = self.game({"kind": "raise", "method": "addResearch", "effect": False})
        with self.assertRaises(SimulatedFaultError):
            game.addResearch("510")
        self.assertEqual((game.gas, game.research), (100, []))

    def test_raise_after_effect_keeps_the_effect(self):
        game = self.game({"kind": "raise", "method": "addResearch"})
        with self.assertRaises(SimulatedFaultError):
            game.addResearch("510")
        self.assertEqual((game.gas, game.research[0]["@ResearchState"]), (50, "Researching"))

    def test_ambiguous_response_returns_none_after_the_effect(self):
        game = self.game({"kind": "ambiguous-response", "method": "addResearch"})
        self.assertIsNone(game.addResearch("510"))
        self.assertEqual(game.gas, 50)

    def test_call_and_day_select_the_triggering_invocation(self):
        game = self.game({"kind": "snapshot-failure", "method": "snapshot", "day": 1, "call": 2})
        game.snapshot()
        game.snapshot()
        game.start_day(1)
        game.snapshot()
        with self.assertRaises(SimulatedFaultError):
            game.snapshot()
        game.snapshot()
        self.assertEqual(game.fired, ["day 1 snapshot #2: snapshot-failure"])

    def test_read_faults_on_a_method_hit_the_next_snapshot(self):
        game = self.game({"kind": "foreign-snapshot", "method": "addResearch", "alias": "bravo"},
                         {"kind": "concurrent-spend", "method": "snapshot", "call": 1, "minerals": 500})
        self.assertEqual(game.snapshot().resources.minerals, 0)  # the player's spend is capped at the balance
        game.addResearch("510")
        self.assertEqual(game.snapshot().account_alias, "bravo")
        self.assertEqual(game.snapshot().account_alias, "alpha")


class SimulatedGameTests(unittest.TestCase):
    def game(self, price, **resources):
        design = DesignData.from_records(room_designs=[
            {"@RoomDesignId": "300"},
            {"@RoomDesignId": "301", "@UpgradeFromRoomDesignId": "300", "@PriceString": price},
        ])
        rooms = [{"@RoomId": "r1", "@RoomDesignId": "300", "@RoomStatus": "Normal"}]
        clock = SimulatedClock(datetime(2027, 6, 1, tzinfo=timezone.utc))
        return SimulatedGame("alpha", dict(resources, rooms=rooms), design, random.Random(0), clock)

    def test_gas_priced_upgrade_charges_gas(self):
        # Regression: only mineral prices were charged, so gas-priced upgrades were free.
        game = self.game("gas:200", minerals=1000, gas=250)
        game.upgradeRoom("r1", "301")
        self.assertEqual((game.gas, game.minerals), (50, 1000))
        self.assertEqual(game.rooms[0]["@RoomStatus"], "Upgrading")

    def test_unaffordable_gas_upgrade_is_refused(self):
        game = self.game("gas:200", minerals=1000, gas=199)
        game.upgradeRoom("r1", "301")
        self.assertEqual((game.gas, game.minerals), (199, 1000))
        self.assertEqual(game.rooms[0]["@RoomStatus"], "Normal")

    def test_mineral_priced_upgrade_still_charges_minerals(self):
        game = self.game("mineral:300", minerals=300, gas=0)
        game.upgradeRoom("r1", "301")
        self.assertEqual((game.minerals, game.rooms[0]["@RoomStatus"]), (0, "Upgrading"))
        refused = self.game("mineral:300", minerals=299, gas=5000)
        refused.upgradeRoom("r1", "301")
        self.assertEqual((refused.minerals, refused.gas, refused.rooms[0]["@RoomStatus"]), (299, 5000, "Normal"))

    def test_unmodelled_price_kind_is_refused(self):
        game = self.game("starbux:10", minerals=1000, gas=1000, starbux=50)
        game.upgradeRoom("r1", "301")
        self.assertEqual((game.starbux, game.rooms[0]["@RoomStatus"]), (50, "Normal"))

    def test_unpriced_upgrade_is_free(self):
        game = self.game("", minerals=0, gas=0)
        game.upgradeRoom("r1", "301")
        self.assertEqual(game.rooms[0]["@RoomStatus"], "Upgrading")


if __name__ == "__main__":
    unittest.main()
