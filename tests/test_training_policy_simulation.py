"""training-v1 end to end in the simulated game: daily plans start, finish and use consumables; faults; the gate.

Stdlib unittest, no network. The scenario is built here (not a shipped JSON file: tests/test_simulation.py pins the
shipped set) from the planner-compatible records in tests/training_fixtures.py.

``SimulatedGame`` has no fleet-v1 collection methods (grabFlyingStarbux, collectTaskReward, collectAvailableMarkers,
collectMiningDronesWithTravel), so the plain ``run_scenario`` tests stub them. The execute-path tests need no stubs:
those actions are left out of the authorization, so ``AuthorizedPolicy`` rejects them in the plan.
"""

import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sdk.fleet import registry_from_dict
from sdk.fleet.authorization import authorization_from_dict
from sdk.fleet.factory import ExecuteFleetOrchestrator, ExecutionGate
from sdk.fleet.orchestrator import AccountContext
from sdk.journal import GameplayJournal
from sdk.policy import POLICIES, DesignData, TrainingV1
from sdk.simulation.game import SimulatedClock, SimulatedGame, build_games, run_scenario
from tests.training_fixtures import CHARACTER_DESIGNS, DESIGN_RECORDS, ITEM_DESIGNS, ROOMS, TRAINING_DESIGNS

START = datetime.fromisoformat("2027-06-01T06:00:00+00:00")
TRAINING = ("add_training", "finish_training", "use_consumable")
FLEET_STUBS = {name: (lambda self: True) for name in
               ("grabFlyingStarbux", "collectTaskReward", "collectAvailableMarkers", "collectMiningDronesWithTravel")}


def c(cid, name, design, room, **stats):
    return {"@CharacterId": cid, "@CharacterDesignId": design, "@CharacterName": name, "@RoomId": room,
            "@Fatigue": "0", **{f"@{stat}Improvement": str(value) for stat, value in stats.items()}}


def scenario(days=6, faults=(), seed=7):
    return {
        "name": "training-v1", "seed": seed, "start": START.isoformat(), "days": days, "execute": True,
        "design": copy.deepcopy(DESIGN_RECORDS),
        "registry": {"version": 1, "accounts": [
            {"alias": "alpha", "objective": "grow", "policy_id": "training-v1", "credentials_secret": "ALPHA_AUTH",
             "budgets": {"minerals": 1000, "gas": 1000, "starbux": 0}, "max_actions": 20}]},
        "training": {"fatigue_max": 100, "fatigue_recovery_seconds": 360, "character_designs": CHARACTER_DESIGNS,
                     "training_designs": TRAINING_DESIGNS, "item_designs": ITEM_DESIGNS},
        "accounts": {"alpha": {
            "minerals": 500, "gas": 500, "starbux": 0, "trophies": 1000, "rooms": copy.deepcopy(ROOMS),
            "research": [{"@ResearchDesignId": "610", "@ResearchState": "Completed"}],
            "inventory": [{"@ItemId": "9001", "@ItemDesignId": "902", "@Quantity": "2"}],
            "crew": [c("101", "Huntress", "7001", "11"), c("102", "Delish", "7001", "12"),
                     c("103", "Cyber Duck", "7002", "11"), c("104", "C.P.U.", "7001", "12", Science=1),
                     c("105", "Ada", "7001", "11"), c("106", "Admiral Serena", "7001", "13"),
                     c("107", "Galactic Snow Maiden", "7003", "12", Weapon=80)],
            "faults": list(faults)}},
    }


def run_v1(scen):
    """run_scenario with the fleet-v1 collection methods stubbed; the result and alpha's journal."""
    with patch.multiple(SimulatedGame, create=True, **FLEET_STUBS), tempfile.TemporaryDirectory() as tmp:
        result = run_scenario(scen, tmp)
        return result, GameplayJournal(Path(tmp) / "alpha.jsonl").read()


def day(event):
    return (datetime.fromisoformat(event.started_at) - START).days


def events(journal, action_id, cid=None):
    """[(day, outcome)] of ``action_id`` events, for crew ``cid`` when given."""
    return [(day(event), event.outcome) for event in journal
            if event.action_id == action_id and (cid is None or event.params.get("character_id") == cid)]


def stat(record, name):
    return int(record[f"@{name}Improvement"])


def final_crew(result):
    return {record["@CharacterId"]: record for record in result.crew["alpha"]}


class MainScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result, cls.journal = run_v1(scenario())

    def test_training_v1_starts_and_finishes_across_days(self):
        self.assertEqual(events(self.journal, "add_training", "101"),
                         [(0, "verified"), (2, "verified"), (4, "verified")])
        self.assertEqual(events(self.journal, "finish_training", "101"),
                         [(1, "verified"), (3, "verified"), (5, "verified")])
        self.assertEqual({event.params["training_design_id"] for event in self.journal
                          if event.action_id == "add_training" and event.params["character_id"] == "101"}, {"5010"})
        self.assertEqual(stat(final_crew(self.result)["101"], "Attack"), 9)
        calls = self.result.calls["alpha"]
        self.assertEqual((calls.count("addTraining"), calls.count("finishTraining"), calls.count("activateItem3")),
                         (8, 8, 2))
        self.assertEqual([event for event in self.journal if event.action_id in TRAINING
                          and event.outcome != "verified"], [])

    def test_every_finish_is_catalog_verified_with_a_real_gain(self):
        log = self.result.training_log["alpha"]
        finishes = [(day(event), event.params["character_id"]) for event in self.journal
                    if event.action_id == "finish_training" and event.outcome == "verified"]
        self.assertEqual(len(finishes), 8)
        days = self.result.days["alpha"]
        for when, cid in finishes:
            with self.subTest(day=when, crew=cid):
                entry = next(entry for entry in log
                             if entry["op"] == "finish" and entry["day"] == when and entry["character_id"] == cid)
                (name, gain), = entry["gain"].items()
                self.assertGreater(gain, 0)
                if cid == "102":
                    self.assertIn(gain, (1, 2, 3))
                before = {record["@CharacterId"]: record for record in days[when]["crew"]}[cid]
                after = ({record["@CharacterId"]: record for record in days[when + 1]["crew"]}[cid]
                         if when + 1 < len(days) else final_crew(self.result)[cid])
                self.assertEqual(stat(after, name) - stat(before, name), gain)

    def test_tp_cap_is_never_exceeded_and_capped_crew_stop(self):
        capacity = {record["@CharacterDesignId"]: int(record["@TrainingCapacity"]) for record in CHARACTER_DESIGNS}
        snapshots = [record for start in self.result.days["alpha"] for record in start["crew"]]
        snapshots += list(self.result.crew["alpha"])
        self.assertTrue(snapshots)
        for record in snapshots:
            total = sum(int(value) for key, value in record.items() if key.endswith("Improvement"))
            self.assertLessEqual(total, capacity[record["@CharacterDesignId"]], record)
        duck = [entry for entry in self.journal if entry.action_id == "add_training"
                and entry.params["character_id"] == "103"]
        self.assertEqual([day(entry) for entry in duck], [0, 2])
        duck_total = sum(int(v) for k, v in final_crew(self.result)["103"].items() if k.endswith("Improvement"))
        self.assertEqual(duck_total, 10)
        maiden = dict(self.result.initial_crew["alpha"][6], **{"@Fatigue": "0"})
        self.assertEqual(final_crew(self.result)["107"], maiden)

    def test_crew_the_strategy_skips_never_train(self):
        skipped = {"105", "106", "107"}
        self.assertFalse(skipped & {entry["character_id"] for entry in self.result.training_log["alpha"]})
        self.assertFalse(skipped & {event.params.get("character_id") for event in self.journal
                                    if event.action_id in TRAINING})
        self.assertTrue({entry["character_id"] for entry in self.result.training_log["alpha"]})

    def test_consumable_is_used_until_stock_runs_out(self):
        self.assertEqual(events(self.journal, "use_consumable", "104"), [(0, "verified"), (1, "verified")])
        self.assertEqual({event.params["item_design_id"] for event in self.journal
                          if event.action_id == "use_consumable"}, {"902"})
        self.assertEqual(stat(final_crew(self.result)["104"], "Science"), 3)
        game_inventory = [entry for entry in self.result.training_log["alpha"] if entry["op"] == "consume"]
        self.assertEqual(len(game_inventory), 2)

    def test_fleet_v1_runs_before_training_each_day(self):
        self.assertEqual(events(self.journal, "collect_daily_reward")[:1], [(0, "verified")])
        for when in range(6):
            with self.subTest(day=when):
                fleet = [e.started_at for e in self.journal if day(e) == when and e.action_id not in TRAINING]
                training = [e.started_at for e in self.journal if day(e) == when and e.action_id in TRAINING]
                self.assertTrue(fleet and training)
                self.assertLess(max(fleet), min(training))


class FaultTests(unittest.TestCase):
    def fault(self, days=3, **fault):
        return run_v1(scenario(days=days, faults=[fault]))

    def test_rejected_start_is_not_observed_and_retried_next_day(self):
        result, journal = self.fault(kind="reject", method="addTraining", day=0, call=1)
        self.assertEqual(events(journal, "add_training", "101")[:2], [(0, "not-observed"), (1, "verified")])
        first = next(e for e in journal if e.action_id == "add_training" and e.params["character_id"] == "101")
        self.assertIs(first.response_ok, False)
        self.assertEqual(events(journal, "add_training", "102")[:1], [(0, "verified")])
        self.assertEqual(events(journal, "add_training", "103")[:1], [(0, "verified")])
        self.assertEqual(result.fleet_runs[0].accounts[0].status, "completed")
        self.assertIn("day 0 addTraining #1: reject", result.faults["alpha"])

    def test_contradicted_start_stops_the_day_and_the_substitute_is_finished(self):
        result, journal = self.fault(kind="contradicted-effect", method="addTraining", day=0, call=1,
                                     training_design_id="5090")
        self.assertEqual(events(journal, "add_training", "101")[0], (0, "contradicted"))
        run = result.fleet_runs[0].accounts[0]
        self.assertEqual(run.status, "stopped")
        self.assertIn("add_training contradicted: 101 started different training 5090", run.reason)
        self.assertEqual([e for e in journal if day(e) == 0 and e.action_id in TRAINING
                          and e.params.get("character_id") != "101"], [])
        self.assertEqual(events(journal, "finish_training", "101")[:1], [(1, "verified")])
        finish = next(entry for entry in result.training_log["alpha"] if entry["op"] == "finish")
        self.assertEqual((finish["day"], finish["design_id"], finish["gain"]), (1, "5090", {"Ability": 2}))
        restarted = [(day(e), e.params["training_design_id"], e.outcome) for e in journal
                     if e.action_id == "add_training" and e.params["character_id"] == "101"]
        self.assertEqual(restarted[1:], [(2, "5010", "verified")])

    def test_uncertain_start_stops_and_blocks_the_retry(self):
        result, journal = self.fault(kind="raise", method="addTraining", day=0, call=1, effect=False)
        first = [e for e in journal if e.action_id == "add_training" and e.params["character_id"] == "101"]
        self.assertEqual((day(first[0]), first[0].outcome, first[0].error_class), (0, "unknown", "SimulatedFaultError"))
        self.assertIn("add_training outcome uncertain", result.fleet_runs[0].accounts[0].reason)
        retry = first[1]
        self.assertEqual((day(retry), retry.outcome, retry.attempted, retry.error_class),
                         (1, "skipped", False, "UncertainPriorAttemptError"))
        self.assertIn("runner refused add_training", result.fleet_runs[1].accounts[0].reason)
        self.assertEqual(result.calls["alpha"].count("addTraining"), 1)
        self.assertEqual([e for e in journal if day(e) == 1 and e.action_id == "add_training"
                          and e.params["character_id"] in ("102", "103")], [])
        # The day's fleet-v1 part still ran first: training is last, so the blocked start costs only training.
        fleet_day_1 = [e for e in journal if day(e) == 1 and e.action_id not in TRAINING and e.attempted]
        self.assertTrue(fleet_day_1)
        self.assertLess(max(e.started_at for e in fleet_day_1), retry.started_at)

    def test_a_raise_after_the_start_took_effect_is_verified(self):
        result, journal = self.fault(kind="raise", method="addTraining", day=0, call=1)
        first = next(e for e in journal if e.action_id == "add_training" and e.params["character_id"] == "101")
        self.assertEqual((day(first), first.outcome, first.error_class), (0, "verified", "SimulatedFaultError"))
        self.assertEqual(result.fleet_runs[0].accounts[0].status, "completed")

    def test_uncertain_finish_is_retried_the_next_day(self):
        result, journal = self.fault(kind="raise", method="finishTraining", day=1, call=1, effect=False)
        self.assertEqual(events(journal, "finish_training", "101"), [(1, "unknown"), (2, "verified")])
        self.assertEqual(events(journal, "finish_training", "102")[:1], [(1, "verified")])
        self.assertEqual(events(journal, "finish_training", "103")[:1], [(1, "verified")])
        self.assertNotIn((2, "verified"), events(journal, "add_training", "101"))
        self.assertEqual(result.fleet_runs[1].accounts[0].status, "completed")


def comparable(result, journal):
    return (result.progression_json, result.fleet_json, result.audit_json, result.training_json, result.calls,
            result.faults, [replace(event, attempt_id="") for event in journal])


class DeterminismTests(unittest.TestCase):
    def test_same_seed_gives_identical_runs(self):
        first, second = comparable(*run_v1(scenario(days=4))), comparable(*run_v1(scenario(days=4)))
        self.assertTrue(first[-1])
        self.assertEqual(first, second)

    def test_seed_changes_random_gains(self):
        # Every design's gain is drawn from 1..9 (the planner reads no gain), so the run's whole gain vector, one
        # draw per finish across every day and crew, matches between two seeds only by a very unlikely chance:
        # an unrelated change in how the simulator consumes its RNG cannot make this pass or fail by accident.
        def wide(seed):
            scen = scenario(seed=seed)
            for block in (scen["training"], scen["design"]):  # new lists: the fixtures' records stay untouched
                block["training_designs"] = [{**record, "@MinGain": "1", "@MaxGain": "9"}
                                             for record in block["training_designs"]]
            return scen

        def gains(scen):
            result, _ = run_v1(scen)
            return [(entry["day"], entry["character_id"], tuple(sorted(entry["gain"].items())))
                    for entry in result.training_log["alpha"] if entry["op"] == "finish"]

        seven, eight = gains(wide(7)), gains(wide(8))
        self.assertGreaterEqual(min(len(seven), len(eight)), 6)  # six or more draws of nine values each
        self.assertEqual(seven, gains(wide(7)))  # deterministic per seed
        self.assertNotEqual(seven, eight)


class StripCost:
    """training-v1 whose training requests lose their explicit zero: the gate must refuse them."""

    name = "training-v1"

    def __init__(self, policy):
        self.policy = policy

    def plan(self, snapshot, design, config):
        planned = self.policy.plan(snapshot, design, config)
        return replace(planned, requests=tuple(replace(request, expected_cost={}) if request.action_id in TRAINING
                                               else request for request in planned.requests))


def run_execute(scen, tmp):
    """run_scenario's day loop with ExecuteFleetOrchestrator: AuthorizedPolicy and GatedActionRunner on the path."""
    workdir = Path(tmp)
    clock = SimulatedClock(START)
    design = DesignData.from_records(**scen["design"])
    registry = registry_from_dict(scen["registry"])
    games = build_games(scen, scen["seed"], design, clock)
    authorization = authorization_from_dict({
        "version": 1, "owner": "owner", "purpose": "offline training-v1 execute test", "accounts": ["alpha"],
        "actions": ["collect_daily_reward", "add_training", "finish_training", "use_consumable"],
        "not_before": "2027-06-01T00:00:00Z", "expires": "2027-06-08T00:00:00Z",
        "caps": {"alpha": {"minerals": 1000, "gas": 1000}}, "modes": ["full"]})
    gate = ExecutionGate(authorization, registry, "full", clock)

    def factory(profile):
        game = games[profile.alias]
        return AccountContext(game, game.snapshot, GameplayJournal(workdir / f"{profile.alias}.jsonl"), design)

    runs, reports = [], []
    for when in range(int(scen["days"])):
        clock.start_day(when)
        for game in games.values():
            game.start_day(when)
        orchestrator = ExecuteFleetOrchestrator(registry, factory, gate=gate, clock=clock, timer=lambda: 0.0,
                                                stop_file=workdir / "STOP")
        runs.append(orchestrator.run(execute=True))
        reports.append(orchestrator.reports.get("alpha"))
    return runs, reports, games["alpha"], GameplayJournal(workdir / "alpha.jsonl").read()


class ExecutePathTests(unittest.TestCase):
    def test_execute_orchestrator_runs_training_through_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs, reports, game, journal = run_execute(scenario(days=4), tmp)
        self.assertEqual([run.accounts[0].status for run in runs], ["completed"] * 4)
        self.assertEqual([e for e in journal if e.error_class == "ExecutionRefusedError"], [])
        for cid in ("101", "102", "103"):
            with self.subTest(crew=cid):
                self.assertEqual(events(journal, "add_training", cid)[:1], [(0, "verified")])
                self.assertEqual(events(journal, "finish_training", cid), [(1, "verified"), (3, "verified")])
        self.assertEqual(events(journal, "use_consumable", "104"), [(0, "verified"), (1, "verified")])
        rejected = {item.candidate: item.reason for item in reports[0].plan.rejected}
        self.assertTrue(rejected["grab_flying_starbux"].startswith("not authorized: "))
        self.assertNotIn("grabFlyingStarbux", game.calls)

    def test_execute_gate_refuses_training_without_the_declaration(self):
        with patch.dict(POLICIES, {"training-v1": StripCost(TrainingV1())}), tempfile.TemporaryDirectory() as tmp:
            runs, _, game, journal = run_execute(scenario(days=1), tmp)
        run = runs[0].accounts[0]
        self.assertEqual(run.status, "stopped")
        self.assertIn("runner refused add_training: a non-idempotent action without a planned cost cannot be "
                      "checked against the caps", run.reason)
        self.assertNotIn("addTraining", game.calls)
        self.assertEqual(events(journal, "collect_daily_reward"), [(0, "verified")])  # the day ran up to training


if __name__ == "__main__":
    unittest.main()
