"""Tests for ``scripts/trial_report.py --pvp`` (SPEC-08 R3-R6). Synthetic journals and battle histories only.

Every test runs with sockets patched to raise, so nothing can reach the network.
"""

import io
import json
import socket
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import trial_report as tr
from sdk.experiments import KIND_PROPORTION, StudyPlan, per_arm_min_sample
from sdk.journal import GameplayEvent, GameplayJournal
from sdk.pvp import candidate_registry
from sdk.pvp.strategies import AdoptionLog, StrategyAdoption
from sdk.state import AccountMismatchError
from tests.synthetic_fixtures import SYNTHETIC_EMAIL

ALIAS = "trial-ship"
OTHER = "other-ship"
T0 = datetime(2027, 3, 1, tzinfo=timezone.utc)
SWITCH = T0 + timedelta(days=14)
END = T0 + timedelta(days=28)
NOW = END + timedelta(days=1)
FOCUS = "weapon-focus-fire@v1"
REACTOR = "reactor-shield-devastation@v1"


def config_version(key):
    strategy_id, _, version = key.rpartition("@v")
    return candidate_registry().get(strategy_id, int(version)).config_version


def plan_payload(**changes):
    payload = {
        "trial_id": "r3-pvp-trial",
        "hypothesis": "weapon-focus-fire wins more attacks than reactor-shield-devastation",
        "account_alias": ALIAS,
        "strategies": [FOCUS, REACTOR],
        "windows": [
            {"strategy": FOCUS, "start": T0.isoformat(), "end": SWITCH.isoformat()},
            {"strategy": REACTOR, "start": SWITCH.isoformat(), "end": END.isoformat()},
        ],
        "min_sample": 5,
        "game_version": "0.999.1",
    }
    payload.update(changes)
    return payload


def make_plan(**changes):
    return tr.pvp_plan_from_dict(plan_payload(**changes))


def adoption_log(focus_method="by-hand", reactor_method="bot-call", alias=ALIAS):
    return AdoptionLog([
        StrategyAdoption(alias, "weapon-focus-fire", 1, config_version(FOCUS), T0 + timedelta(hours=1), focus_method,
                         note="set native AI in the game"),
        StrategyAdoption(alias, "reactor-shield-devastation", 1, config_version(REACTOR), SWITCH + timedelta(hours=1),
                         reactor_method),
    ])


def attack(at, battle_id, trophies=12, outcome="verified", tier="outcome-observed", error_class=None, alias=ALIAS,
           cost=None, **evidence):
    payload = {"evidence_tier": tier, "battle_id": battle_id, "trophies_delta": trophies, **evidence}
    return GameplayEvent.create(
        account_alias=alias, action_id="run_pvp_battle", started_at=at, finished_at=at + timedelta(seconds=40),
        outcome=outcome, reason="synthetic battle", attempted=True, error_class=error_class, evidence=payload,
        cost=cost if cost is not None else {"minerals_delta": -150, "gas_delta": None, "starbux_delta": 0},
    )


def attacks(start, wins, losses, prefix, **evidence):
    """``wins`` then ``losses`` known-result attacks, 30 minutes apart from ``start``."""
    deltas = [12] * wins + [-9] * losses
    return [attack(start + timedelta(minutes=30 * i), f"{prefix}-{i}", trophies=delta, **evidence)
            for i, delta in enumerate(deltas)]


def record(battle_id, at, role="attacker", outcome="win", trophy_change=None, alias=ALIAS, **extra):
    return {"battle_id": battle_id, "account_alias": alias, "role": role, "outcome": outcome, "at": at.isoformat(),
            "trophy_change": trophy_change, **extra}


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("network used"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, events, plan=None, log=None, history=None, now=NOW):
        plan = plan or make_plan()
        records = None if history is None else tr.history_from_list(history, plan.account_alias)
        return tr.build_pvp_report(plan, events, log or adoption_log(), records, now=now)

    @staticmethod
    def row(section, key):
        return next(row for row in section["per_strategy"] if row["strategy"] == key)


class KnownUnknownSplitTests(OfflineTestCase):
    # Verifies: S08-R3
    def test_only_verified_outcome_observed_attacks_without_error_are_known(self):
        start = T0 + timedelta(days=1)
        events = attacks(start, 2, 1, "known") + [
            attack(start + timedelta(hours=3), "fin", tier="finalised", trophies=None, outcome="unknown"),
            attack(start + timedelta(hours=4), "err", error_class="ConnectionError"),
            attack(start + timedelta(hours=5), "uncertain", outcome="uncertain"),
            attack(start + timedelta(hours=6), "zero", trophies=0),
        ]
        report = self.build(events)
        attack_section = report["attack"]
        self.assertEqual((attack_section["battles"], attack_section["known"], attack_section["unknown"]), (7, 3, 4))
        focus = self.row(attack_section, FOCUS)
        self.assertEqual((focus["battles"], focus["known"], focus["unknown"]), (7, 3, 4))
        self.assertEqual((focus["wins"], focus["losses"], focus["known_from_journal"]), (2, 1, 3))
        self.assertEqual(focus["win_rate"], round(2 / 3, 4))
        self.assertTrue(any("4 attack(s) had an unknown outcome" in note for note in report["limitations"]))

    # Verifies: S08-R3
    def test_battle_history_attacker_record_with_the_battle_id_makes_the_outcome_known(self):
        at = T0 + timedelta(days=2)
        events = [attack(at, "b-1", tier="finalised", trophies=None, outcome="unknown"),
                  attack(at + timedelta(hours=1), "b-2", tier="finalised", trophies=None, outcome="unknown")]
        history = [record("b-1", at + timedelta(seconds=5), outcome="loss", trophy_change=-11),
                   record("b-9", at, outcome="win")]  # no journaled battle: left out, but counted
        report = self.build(events, history=history)
        focus = self.row(report["attack"], FOCUS)
        self.assertEqual((focus["known"], focus["unknown"], focus["losses"]), (1, 1, 1))
        self.assertEqual(focus["known_from_battle_history"], 1)
        self.assertEqual(focus["net_trophies"], -11)
        self.assertEqual(report["battle_history"]["attacks_without_journal"], 1)

    # Verifies: S08-R3, S08-R5
    def test_a_defender_record_never_verifies_an_attack(self):
        at = T0 + timedelta(days=2)
        events = [attack(at, "shared", tier="finalised", trophies=None, outcome="unknown")]
        report = self.build(events, history=[record("shared", at, role="defender", outcome="win")])
        self.assertEqual((report["attack"]["known"], report["attack"]["unknown"]), (0, 1))
        self.assertEqual(self.row(report["defense"], FOCUS)["known"], 1)

    # Verifies: S08-R3
    def test_history_that_contradicts_a_known_journal_outcome_makes_it_unknown(self):
        at = T0 + timedelta(days=2)
        report = self.build([attack(at, "b-1", trophies=12)], history=[record("b-1", at, outcome="loss")])
        self.assertEqual((report["attack"]["known"], report["attack"]["contradicted"]), (0, 1))
        self.assertEqual(self.row(report["attack"], FOCUS)["unknown"], 1)

    # Verifies: S08-R3
    def test_an_unknown_history_outcome_does_not_erase_a_known_journal_outcome(self):
        at = T0 + timedelta(days=2)
        report = self.build([attack(at, "b-1", trophies=12)], history=[record("b-1", at, outcome="unknown")])
        focus = self.row(report["attack"], FOCUS)
        self.assertEqual((focus["known"], focus["wins"], focus["known_from_journal"]), (1, 1, 1))

    # Verifies: S08-R3
    def test_battles_outside_a_recorded_application_have_no_strategy_and_are_counted(self):
        events = [attack(T0 + timedelta(minutes=10), "before-adoption"),  # window open, strategy not yet applied
                  attack(END + timedelta(hours=2), "after-windows")]
        report = self.build(events)
        self.assertEqual(report["attack"]["unattributed"], {"battles": 2, "known": 2, "unknown": 0})
        self.assertEqual(self.row(report["attack"], FOCUS)["battles"], 0)

    # Verifies: S08-R3
    def test_other_accounts_battles_are_left_out_and_counted(self):
        report = self.build(attacks(T0 + timedelta(days=1), 1, 0, "mine") + [attack(T0 + timedelta(days=1), "x",
                                                                                     alias=OTHER)])
        self.assertEqual((report["attack"]["battles"], report["cross_account_events"]), (1, 1))


class ComparisonTests(OfflineTestCase):
    # Verifies: S08-R4
    def test_short_samples_report_insufficient_evidence_and_no_recommendation(self):
        events = attacks(T0 + timedelta(days=1), 4, 0, "f") + attacks(SWITCH + timedelta(days=1), 0, 4, "r")
        report = self.build(events)
        self.assertEqual(report["verdict"], "insufficient-evidence")
        self.assertIsNone(report["recommendation"])
        self.assertEqual(report["min_sample"], 5)
        self.assertIn("plan min_sample", report["min_sample_source"])
        self.assertTrue(report["limitations"][0].startswith("No recommendation"))

    # Verifies: S08-R4
    def test_unknown_outcomes_do_not_count_toward_the_minimum_sample(self):
        start = T0 + timedelta(days=1)
        unknown = [attack(start + timedelta(hours=10 + i), f"u{i}", tier="finalised", trophies=None,
                          outcome="unknown") for i in range(10)]
        events = attacks(start, 4, 0, "f") + unknown + attacks(SWITCH + timedelta(days=1), 0, 10, "r")
        report = self.build(events)
        self.assertEqual(self.row(report["attack"], FOCUS)["battles"], 14)
        self.assertEqual(report["verdict"], "insufficient-evidence")

    # Verifies: S08-R4
    def test_enough_known_battles_with_separated_intervals_recommend_a_strategy(self):
        events = attacks(T0 + timedelta(days=1), 10, 0, "f") + attacks(SWITCH + timedelta(days=1), 0, 10, "r")
        report = self.build(events, plan=make_plan(min_sample=10))
        self.assertEqual((report["verdict"], report["recommendation"]), ("recommend", FOCUS))
        self.assertFalse(report["attack"]["stratified"])
        self.assertTrue(any("not stratified" in note for note in report["limitations"]))

    # Verifies: S08-R4
    def test_minimum_sample_comes_from_the_study_plan_when_an_effect_size_is_given(self):
        effect = {"baseline_win_rate": 0.5, "minimum_effect": 0.2, "power": 0.8}
        plan = make_plan(effect=effect)
        expected = per_arm_min_sample(StudyPlan(
            study_id="r3-pvp-trial", hypothesis=plan.hypothesis, arms=(FOCUS, REACTOR), metric="attack-win-rate",
            metric_kind=KIND_PROPORTION, higher_is_better=True, baseline_value=0.5, minimum_effect=0.2,
            unit="battle", window_start=T0, window_end=END, power=0.8, min_sample=5,
        ))
        events = attacks(T0 + timedelta(days=1), 10, 0, "f") + attacks(SWITCH + timedelta(days=1), 0, 10, "r")
        report = self.build(events, plan=plan)
        self.assertEqual(report["min_sample"], expected)
        self.assertGreater(expected, 10)
        self.assertEqual(report["attack"]["comparison"]["min_sample"], expected)
        self.assertEqual(report["min_sample_source"], "sdk.experiments.study.per_arm_min_sample")
        self.assertEqual(report["verdict"], "insufficient-evidence")  # 10 per strategy is short of the plan
        self.assertEqual(report["study_plan"]["minimum_effect"], 0.2)

    # Verifies: S08-R4
    def test_comparison_is_stratified_by_observed_cohort_and_disagreeing_cohorts_block_a_recommendation(self):
        low, high = {"opponent_hull_level": 3}, {"opponent_hull_level": 9}
        f_start, r_start = T0 + timedelta(days=1), SWITCH + timedelta(days=1)
        events = (attacks(f_start, 20, 0, "f-low", **low) + attacks(f_start + timedelta(days=2), 0, 5, "f-high", **high)
                  + attacks(r_start, 0, 20, "r-low", **low) + attacks(r_start + timedelta(days=2), 5, 0, "r-high", **high))
        report = self.build(events)
        attack_section = report["attack"]
        self.assertTrue(attack_section["stratified"])
        self.assertEqual(attack_section["cohorts_observed"], ["hull 1-4 / unknown", "hull 8-10 / unknown"])
        self.assertEqual(set(attack_section["comparison"]["cohorts"]), set(attack_section["cohorts_observed"]))
        self.assertEqual(report["verdict"], "insufficient-evidence")
        self.assertIn("cohorts disagree", attack_section["comparison"]["reason"])
        self.assertIsNone(report["recommendation"])

    # Verifies: S08-R4
    def test_cohorts_from_battle_history_stratify_the_comparison(self):
        at = T0 + timedelta(days=1)
        events = [attack(at, "b-1")]
        history = [record("b-1", at, outcome="win", opponent_hull_level=12, opponent_trophies=1200)]
        report = self.build(events, history=history)
        self.assertEqual(report["attack"]["cohorts_observed"], ["hull 11-13 / trophies 1000-1499"])


class DefenseTests(OfflineTestCase):
    # Verifies: S08-R5
    def test_defenses_come_only_from_battle_history_and_are_reported_separately(self):
        events = attacks(T0 + timedelta(days=1), 3, 0, "f")
        history = [
            record("d-1", T0 + timedelta(days=2), role="defender", outcome="loss", trophy_change=-20),
            record("d-2", T0 + timedelta(days=3), role="defender", outcome="win", trophy_change=8),
            record("d-3", SWITCH + timedelta(days=1), role="defender", outcome="unknown"),
            record("d-4", T0 + timedelta(minutes=5), role="defender", outcome="win"),  # before any adoption
        ]
        report = self.build(events, history=history)
        defense = report["defense"]
        self.assertEqual(defense["source"], "battle-history defender records only")
        self.assertEqual((defense["battles"], defense["known"], defense["unknown"]), (4, 3, 1))
        focus, reactor = self.row(defense, FOCUS), self.row(defense, REACTOR)
        self.assertEqual((focus["battles"], focus["wins"], focus["losses"], focus["net_trophies"]), (2, 1, 1, -12))
        self.assertEqual((reactor["battles"], reactor["unknown"], reactor["win_rate"]), (1, 1, None))
        self.assertEqual(defense["unattributed"]["battles"], 1)
        self.assertEqual(defense["comparison"]["role"], "defender")
        self.assertNotIn("resource_cost", focus)
        self.assertEqual(self.row(report["attack"], FOCUS)["battles"], 3)  # attacks untouched by defenses
        self.assertEqual(report["verdict"], report["attack"]["comparison"]["verdict"])

    # Verifies: S08-R5
    def test_without_battle_history_defense_is_not_reported(self):
        report = self.build(attacks(T0 + timedelta(days=1), 3, 0, "f"))
        self.assertIsNone(report["defense"])
        self.assertFalse(report["battle_history"]["provided"])
        self.assertTrue(any("defense results are not reported" in note for note in report["limitations"]))

    # Verifies: S08-R5
    def test_journaled_attacks_never_count_as_defenses(self):
        report = self.build(attacks(T0 + timedelta(days=1), 3, 0, "f"), history=[])
        self.assertEqual(report["defense"]["battles"], 0)
        self.assertEqual(report["defense"]["comparison"]["verdict"], "insufficient-evidence")


class ReportFieldTests(OfflineTestCase):
    # Verifies: S08-R6
    def test_report_records_version_counts_rates_trophies_cost_recommendation_and_limitations(self):
        events = attacks(T0 + timedelta(days=1), 3, 1, "f") + attacks(SWITCH + timedelta(days=1), 1, 2, "r")
        events.append(attack(SWITCH + timedelta(days=2), "r-free", cost={}))
        report = self.build(events)
        self.assertEqual(report["game_version"], "0.999.1")
        self.assertEqual([s["key"] for s in report["strategies"]], [FOCUS, REACTOR])
        focus, reactor = self.row(report["attack"], FOCUS), self.row(report["attack"], REACTOR)
        self.assertEqual((focus["battles"], focus["known"], focus["wins"]), (4, 4, 3))
        self.assertEqual(focus["win_rate"], 0.75)
        low, high = focus["interval_95"]
        self.assertLess(low, 0.75)
        self.assertGreater(high, 0.75)
        self.assertEqual(focus["net_trophies"], 3 * 12 - 9)
        self.assertEqual(focus["resource_cost"], {"source": focus["resource_cost"]["source"], "samples": 4,
                                                  "spent": {"minerals": 600}})
        self.assertEqual(reactor["resource_cost"]["samples"], 3)  # the battle without a cost is not a sample
        self.assertEqual((reactor["battles"], reactor["net_trophies"]), (4, 12 - 18 + 12))
        self.assertIn("recommendation", report)
        self.assertIsNone(report["recommendation"])
        self.assertEqual(report["verdict"], "insufficient-evidence")
        self.assertIsInstance(report["limitations"], list)

    # Verifies: S08-R6
    def test_by_hand_adoptions_mark_timing_as_approximate(self):
        report = self.build(attacks(T0 + timedelta(days=1), 2, 0, "f") + attacks(SWITCH + timedelta(days=1), 2, 0, "r"))
        self.assertTrue(report["approximate_timing"])
        self.assertEqual(self.row(report["attack"], FOCUS)["adoption_methods"], ["by-hand"])
        self.assertTrue(self.row(report["attack"], FOCUS)["approximate_timing"])
        self.assertFalse(self.row(report["attack"], REACTOR)["approximate_timing"])
        self.assertTrue(any("approximate timing" in note for note in report["limitations"]))
        bot = self.build(attacks(T0 + timedelta(days=1), 2, 0, "f"), log=adoption_log(focus_method="bot-call"))
        self.assertFalse(bot["approximate_timing"])
        self.assertFalse(any("approximate timing" in note for note in bot["limitations"]))

    # Verifies: S08-R6
    def test_missing_game_version_is_a_limitation(self):
        report = self.build([], plan=make_plan(game_version=None))
        self.assertIsNone(report["game_version"])
        self.assertIn("The plan records no game version.", report["limitations"])
        self.assertEqual(report["attack"]["battles"], 0)

    # Verifies: S08-R6
    def test_markdown_shows_every_section(self):
        events = attacks(T0 + timedelta(days=1), 3, 1, "f")
        history = [record("d-1", T0 + timedelta(days=2), role="defender", outcome="loss", trophy_change=-20)]
        markdown = tr.pvp_to_markdown(self.build(events, history=history))
        for text in ("# PvP strategy comparison: r3-pvp-trial", "Game version: 0.999.1", "## Attacks",
                     "## Defenses", "## Strategy applications", "## Limitations", "insufficient-evidence",
                     "by-hand (approximate timing)", "75% (", "minerals 600 (4)"):
            self.assertIn(text, markdown)


class InputTests(OfflineTestCase):
    # Verifies: S08-R5
    def test_battle_history_of_another_account_is_refused(self):
        with self.assertRaises(AccountMismatchError):
            tr.history_from_list([record("b-1", T0, alias=OTHER)], ALIAS)

    # Verifies: S08-R5
    def test_battle_history_rejects_unknown_keys_repeats_and_bad_values(self):
        cases = [
            [record("b-1", T0, extra="x")],
            [record("b-1", T0), record("b-1", T0)],
            [record("b-1", T0, role="spectator")],
            [record("b-1", T0, outcome="victory")],
            [record("b-1", T0, opponent_hull_level=40)],
            [record("b-1", T0, trophy_change="12")],
            [{"battle_id": "b-1", "account_alias": ALIAS, "role": "attacker", "outcome": "win"}],
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(tr.TrialInputError):
                tr.history_from_list(payload, ALIAS)
        both_roles = tr.history_from_list([record("b-1", T0), record("b-1", T0, role="defender")], ALIAS)
        self.assertEqual(len(both_roles), 2)

    # Verifies: S08-R4
    def test_plan_rejects_unregistered_strategies_foreign_windows_and_bad_samples(self):
        cases = [
            {"strategies": [FOCUS, "made-up@v1"]},
            {"strategies": [FOCUS, FOCUS]},
            {"windows": [{"strategy": "weapon-focus-fire@v2", "start": T0.isoformat(), "end": END.isoformat()}]},
            {"windows": [{"strategy": FOCUS, "start": T0.isoformat(), "end": END.isoformat()},
                         {"strategy": REACTOR, "start": SWITCH.isoformat(), "end": END.isoformat()}]},
            {"min_sample": 0},
            {"effect": {"baseline_win_rate": 0.5}},
            {"effect": {"baseline_win_rate": 0.9, "minimum_effect": 0.2}},
            {"surprise": True},
            {"account_alias": "Owner Name"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                make_plan(**changes)

    # Verifies: S08-R6
    def test_secret_shaped_plan_text_is_refused_without_echoing_it(self):
        with self.assertRaises(tr.TrialInputError) as caught:
            make_plan(hypothesis=f"owner {SYNTHETIC_EMAIL} prefers focus fire")
        self.assertNotIn(SYNTHETIC_EMAIL, str(caught.exception))
        with self.assertRaises(tr.TrialInputError) as caught:
            tr.history_from_list([{**record("b-1", T0), "battle_id": SYNTHETIC_EMAIL}], ALIAS)
        self.assertNotIn(SYNTHETIC_EMAIL, str(caught.exception))

    # Verifies: S08-R3
    def test_a_repeated_battle_id_in_the_journals_is_refused(self):
        events = [attack(T0 + timedelta(days=1), "b-1"), attack(T0 + timedelta(days=1, hours=1), "b-1")]
        with self.assertRaises(tr.TrialInputError):
            self.build(events)


class CommandLineTests(OfflineTestCase):
    def write_inputs(self, root, history=True):
        (root / "plan.json").write_text(json.dumps(plan_payload()))
        (root / "applications.json").write_text(adoption_log().to_json())
        journal = GameplayJournal(root / "events.jsonl")
        for item in attacks(T0 + timedelta(days=1), 3, 1, "f") + attacks(SWITCH + timedelta(days=1), 1, 3, "r"):
            journal.append(item)
        if history:
            (root / "history.json").write_text(json.dumps(
                [record("d-1", T0 + timedelta(days=2), role="defender", outcome="win", trophy_change=6)]))

    # Verifies: S08-R3, S08-R4, S08-R5, S08-R6
    def test_cli_writes_the_pvp_report_as_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_inputs(root)
            code = tr.main([
                "--pvp", "--plan", str(root / "plan.json"), "--journal", str(root / "events.jsonl"),
                "--adoptions", str(root / "applications.json"), "--battle-history", str(root / "history.json"),
                "--now", NOW.isoformat(), "--json", str(root / "report.json"), "--markdown", str(root / "report.md"),
            ])
            self.assertEqual(code, 0)
            report = json.loads((root / "report.json").read_text())
            markdown = (root / "report.md").read_text()
        self.assertEqual(report["report"], "pvp-strategy-comparison")
        self.assertEqual((report["attack"]["known"], report["attack"]["unknown"]), (8, 0))
        self.assertEqual(report["defense"]["known"], 1)
        self.assertEqual(report["verdict"], "insufficient-evidence")
        self.assertIn("## Defenses", markdown)

    # Verifies: S08-R6
    def test_cli_refuses_pvp_without_an_adoption_log_and_mixed_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_inputs(root, history=False)
            argv_cases = [
                (["--pvp", "--plan", str(root / "plan.json")], "needs --adoptions"),
                (["--pvp", "--plan", str(root / "plan.json"), "--adoptions", str(root / "applications.json"),
                  "--minutes", str(root / "plan.json")], "do not apply to --pvp"),
                (["--pvp", "--plan", str(root / "plan.json"), "--adoptions", str(root / "applications.json"),
                  "--journal", str(root / "missing.jsonl")], "not a file"),
                (["--plan", str(root / "plan.json"), "--adoptions", str(root / "applications.json")], "need --pvp"),
            ]
            for argv, message in argv_cases:
                stderr = io.StringIO()
                with self.subTest(message=message), redirect_stderr(stderr):
                    code = tr.main(argv + ["--now", NOW.isoformat()])
                    self.assertEqual(code, 2)
                    self.assertIn(message, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
