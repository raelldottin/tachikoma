"""Tests for ``scripts/trial_report.py --study`` (SPEC-12 R1, R4, R6).

The per-trial outputs are synthetic and made with the existing report builders (progression, ``--pvp`` and
``--fleet``) from the synthetic inputs of their own tests; the market trial result is written by hand in the
provisional format. Every test runs with sockets patched to raise, so nothing can reach the network.
"""

import io
import json
import socket
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import timedelta
from pathlib import Path
from unittest import mock

from scripts import trial_report as tr
from sdk.experiments import ADOPT, INSUFFICIENT, KEEP_RUNNING, ProportionSample
from tests import test_trial_report as progression
from tests import test_trial_report_fleet as fleet
from tests import test_trial_report_pvp as pvp
from tests.synthetic_fixtures import SYNTHETIC_EMAIL

STUDY_KEYS = {"study", "kind", "spec", "question", "status", "source", "trial_id", "hypothesis", "accounts",
              "evaluated_at", "trial_status", "recommendation", "arms", "windows", "game_versions", "min_sample",
              "sample_unit", "sample_sizes", "primary_result", "decide", "decide_note", "secondary_decisions",
              "costs", "failures", "limitations", "conclusion", "reached_min_sample", "inconclusive_reasons"}
MARKET_START = progression.WINDOW_START
MARKET_END = MARKET_START + timedelta(days=14)


def progression_report(failures_on=(), **plan_changes):
    plan = progression.make_plan(**plan_changes)
    snapshots = {"manual": progression.series(progression.WINDOW_START, progression.MANUAL_GROWTH),
                 "baseline-v1": progression.series(progression.AUTO_START, progression.AUTO_GROWTH)}
    journals = {"baseline-v1": progression.automated_journal(failures_on=failures_on)}
    minutes = tr.minutes_from_list(progression.minutes_payload(), plan)
    return tr.build_trial_report(plan, snapshots, journals, minutes, now=progression.NOW)


def pvp_report(focus=(3, 1), reactor=(1, 2), **plan_changes):
    """Attacks under each strategy: (wins, losses). The defaults leave both strategies short of 5 known results."""
    events = (pvp.attacks(pvp.T0 + timedelta(days=1), *focus, "focus")
              + pvp.attacks(pvp.SWITCH + timedelta(days=1), *reactor, "reactor"))
    return tr.build_pvp_report(pvp.make_plan(**plan_changes), events, pvp.adoption_log(), None, now=pvp.NOW)


def fleet_report(root):
    return fleet.build(fleet.Trial(Path(root) / "journals").standard())


def trade(trade_id, side, state, day, arm=None, price=100, proceeds=None, cost=None, fees=None,
          game_version="0.999.1"):
    payload = {"trade_id": trade_id, "side": side, "state": state,
               "at": (MARKET_START + timedelta(days=day)).isoformat(), "item_design_id": "101", "quantity": 1,
               "price": price, "proceeds": proceeds, "cost": cost, "fees": fees, "game_version": game_version}
    if arm is not None:
        payload["arm"] = arm
    return payload


def market_trial(trades=None, arms=("hold-v1", "relist-v1"), **changes):
    if trades is None:
        trades = [
            trade("s1", "sell", "completed", 1, "hold-v1", proceeds=80, fees=20),
            trade("s2", "sell", "completed", 2, "hold-v1", proceeds=85, fees=15),
            trade("b1", "buy", "completed", 3, "hold-v1", price=50, cost=55, fees=5),
            trade("s3", "sell", "listed", 4, "hold-v1"),
            trade("s4", "sell", "completed", 1, "relist-v1", proceeds=90, fees=10),
            trade("s5", "sell", "completed", 2, "relist-v1", proceeds=95, fees=5),
            trade("s6", "sell", "unknown", 3, "relist-v1", proceeds=70),
            trade("s7", "sell", "expired", 4, "relist-v1", game_version="0.999.2"),
        ]
    payload = {
        "report": "market-trial", "format_version": 1, "trial_id": "r4-market-trial",
        "hypothesis": "relisting returns more per completed trade than holding", "account_alias": "trial-ship",
        "arms": list(arms), "currency": "starbux", "min_sample": 2, "window_start": MARKET_START.isoformat(),
        "window_end": MARKET_END.isoformat(), "evaluated_at": (MARKET_END + timedelta(days=1)).isoformat(),
        "trades": trades, "operator_minutes": 12,
    }
    payload.update(changes)
    return payload


class StudyTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("network used"))
        patcher.start()
        self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    @staticmethod
    def study(report, study_id):
        return next(study for study in report["studies"] if study["study"] == study_id)

    def write(self, name, payload):
        path = self.root / name
        path.write_text(tr.to_json(payload), encoding="utf-8")
        return path


class AggregateTests(StudyTestCase):
    """S12-AC1: given synthetic trial outputs, including one inconclusive study (and one missing), the aggregate
    contains every field and marks the inconclusive study."""

    # Verifies: S12-R1, S12-R4
    def test_aggregate_has_every_field_and_marks_the_inconclusive_and_missing_studies(self):
        report = tr.build_study_report({"progression": progression_report(), "pvp": pvp_report(),
                                        "fleet": fleet_report(self.root)})
        self.assertEqual([s["study"] for s in report["studies"]], ["A", "C", "D", "R4-market"])
        for study in report["studies"]:
            with self.subTest(study=study["study"]):
                self.assertEqual(set(study), STUDY_KEYS)
                self.assertTrue(study["question"].endswith("?"))
        for study_id in ("A", "C", "D"):
            study = self.study(report, study_id)
            with self.subTest(study=study_id):
                self.assertEqual(study["status"], "reported")
                self.assertEqual(len(study["arms"]), 2)
                self.assertTrue(study["windows"])
                self.assertEqual(study["game_versions"], ["client 0.999.1"] if study_id == "D" else ["0.999.1"])
                self.assertEqual(set(study["sample_sizes"]), {arm["arm"] for arm in study["arms"]})
                self.assertIsInstance(study["min_sample"], int)
                self.assertEqual([row["arm"] for row in study["primary_result"]["arms"]],
                                 [arm["arm"] for arm in study["arms"]])
                for row in study["primary_result"]["arms"]:
                    self.assertEqual(set(row), {"arm", "n", "estimate", "interval_95"})
                self.assertIn(study["decide"]["decision"], (ADOPT, INSUFFICIENT, KEEP_RUNNING))
                self.assertTrue(study["costs"])
                self.assertTrue(study["limitations"])

        a, c, d, market = (self.study(report, s) for s in ("A", "C", "D", "R4-market"))
        self.assertEqual((a["conclusion"], a["reached_min_sample"], a["inconclusive_reasons"]), ("conclusive", True, []))
        self.assertEqual((d["conclusion"], d["reached_min_sample"]), ("conclusive", True))
        self.assertEqual((c["conclusion"], c["reached_min_sample"]), ("inconclusive", False))
        self.assertEqual(c["sample_sizes"], {pvp.FOCUS: 4, pvp.REACTOR: 3})
        self.assertIn(f"{pvp.FOCUS} has 4 known-result attack(s), under the minimum of 5", c["inconclusive_reasons"])
        self.assertEqual(market["status"], "missing")
        self.assertEqual((market["conclusion"], market["reached_min_sample"]), ("inconclusive", None))
        self.assertEqual(market["inconclusive_reasons"], ["no trial output was given for this study"])
        self.assertEqual(report["summary"], {"reported": ["A", "C", "D"], "missing": ["R4-market"],
                                             "conclusive": ["A", "D"], "inconclusive": ["C", "R4-market"],
                                             "aborted": []})

        markdown = tr.study_to_markdown(report)
        self.assertIn("| C | SPEC-08 | r3-pvp-trial | **inconclusive** | insufficient-evidence |", markdown)
        self.assertIn("| R4-market | SPEC-09 | n/a | **inconclusive** | n/a | none |", markdown)
        self.assertIn("## Study R4-market (market, SPEC-09)", markdown)
        self.assertIn("- Conclusion: **inconclusive**: no trial output was given for this study.", markdown)
        self.assertIn("| baseline-v1 | 6 | 4.00 | 3.06 to 4.94 |", markdown)

    # Verifies: S12-R4
    def test_no_trial_output_reports_every_study_as_inconclusive(self):
        report = tr.build_study_report({})
        self.assertEqual(len(report["studies"]), 4)
        for study in report["studies"]:
            self.assertEqual(set(study), STUDY_KEYS)
            self.assertEqual((study["status"], study["conclusion"]), ("missing", "inconclusive"))
        self.assertEqual(report["summary"]["inconclusive"], ["A", "C", "D", "R4-market"])
        self.assertIn("| none | n/a | n/a |", tr.study_to_markdown(report))

    # Verifies: S12-R4
    def test_a_trial_that_stopped_early_is_aborted_not_conclusive(self):
        report = tr.build_study_report({"progression": progression_report(failures_on=(1,), stop_thresholds={"max_uncertain": 0})})
        a = self.study(report, "A")
        self.assertEqual(a["trial_status"], "stopped-early")
        self.assertEqual((a["conclusion"], a["reached_min_sample"]), ("aborted", True))
        self.assertIn("the trial stopped early; its results are for reference only", a["inconclusive_reasons"])
        self.assertEqual(report["summary"]["aborted"], ["A"])

    # Verifies: S12-R4
    def test_enough_samples_but_no_decision_is_inconclusive(self):
        report = tr.build_study_report({"pvp": pvp_report(focus=(4, 2), reactor=(3, 3))})
        c = self.study(report, "C")
        self.assertTrue(c["reached_min_sample"])
        self.assertEqual(c["decide"]["decision"], INSUFFICIENT)
        self.assertEqual(c["conclusion"], "inconclusive")
        self.assertIn("decide() returned insufficient-evidence: the 95% intervals overlap", c["inconclusive_reasons"])


class StudyFieldTests(StudyTestCase):
    # Verifies: S12-R1
    def test_progression_and_fleet_figures_are_the_trial_reports_figures(self):
        a_report, d_report = progression_report(), fleet_report(self.root)
        report = tr.build_study_report({"progression": a_report, "fleet": d_report})
        a, d = self.study(report, "A"), self.study(report, "D")
        source = json.loads(tr.to_json(a_report))
        self.assertEqual(a["decide"], source["decision"])
        self.assertEqual(a["windows"], [{"label": "trial", **source["window"]}])
        for row, arm in zip(a["primary_result"]["arms"], source["arms"]):
            self.assertEqual((row["n"], row["estimate"], row["interval_95"]),
                             (arm["growth_velocity"]["n"], arm["growth_velocity"]["mean"],
                              arm["growth_velocity"]["interval_95"]))
        self.assertEqual(a["costs"]["operator_minutes_per_account_day"], {"manual": 32.5, "baseline-v1": 5.5})
        self.assertEqual(a["costs"]["operator_minutes_saved_per_account_day"], 27.0)
        self.assertEqual(a["costs"]["journal_starbux_spent"], {"manual": None, "baseline-v1": 0})
        self.assertEqual(a["failures"]["baseline-v1"]["attempted"], 18)

        fleet_source = json.loads(tr.to_json(d_report))
        self.assertEqual(d["decide"], fleet_source["decisions"]["verified-actions-per-hour"])
        self.assertEqual(set(d["secondary_decisions"]), {"failure-rate", "uncertain-rate"})
        self.assertEqual(d["recommendation"], "concurrent")
        self.assertEqual([w["label"] for w in d["windows"]], ["sequential", "concurrent"])
        self.assertEqual(d["sample_sizes"], {"sequential": 3, "concurrent": 3})
        self.assertEqual(d["costs"]["run_hours"], {"sequential": 3.0, "concurrent": 3.0})
        self.assertEqual(d["failures"]["sequential"]["failed"], 2)
        self.assertEqual(d["limitations"], fleet_source["limitations"])

    # Verifies: S12-R1
    def test_pvp_decide_is_regenerated_from_pooled_known_results(self):
        source = pvp_report(focus=(6, 1), reactor=(1, 6))
        c = self.study(tr.build_study_report({"pvp": source}), "C")
        rows = {row["strategy"]: row for row in source["attack"]["per_strategy"]}
        focus, reactor = (ProportionSample(rows[key]["wins"], rows[key]["known"]) for key in (pvp.FOCUS, pvp.REACTOR))
        self.assertEqual(c["decide"]["baseline_interval"], list(focus.interval()))
        self.assertEqual(c["decide"]["candidate_interval"], list(reactor.interval()))
        self.assertEqual((c["decide"]["baseline"], c["decide"]["candidate"]), (pvp.FOCUS, pvp.REACTOR))
        self.assertEqual(c["decide"]["decided_at"], source["evaluated_at"])
        self.assertIn("regenerated", c["decide_note"])
        self.assertEqual(c["primary_result"]["arms"][0]["estimate"], rows[pvp.FOCUS]["win_rate"])
        self.assertEqual(c["costs"]["resources_spent"][pvp.FOCUS], {"minerals": 1050})
        self.assertEqual(c["trial_status"], source["verdict"])

    # Verifies: S12-R1
    def test_market_trial_uses_completed_trades_only(self):
        report = tr.build_study_report({"market": market_trial()})
        market = self.study(report, "R4-market")
        primary = market["primary_result"]
        self.assertEqual(primary["market_return"], {"hold-v1": 80 + 85 - 55, "relist-v1": 90 + 95})
        self.assertEqual(market["sample_sizes"], {"hold-v1": 3, "relist-v1": 2})
        rows = {row["arm"]: row for row in primary["arms"]}
        self.assertEqual(rows["hold-v1"]["estimate"], round((80 + 85 - 55) / 3, 4))
        self.assertEqual(rows["relist-v1"]["estimate"], 92.5)
        self.assertIsNotNone(rows["relist-v1"]["interval_95"])
        self.assertEqual(market["costs"]["fees"], {"hold-v1": {"total": 40, "samples": 3},
                                                   "relist-v1": {"total": 15, "samples": 2}})
        self.assertEqual(market["costs"]["buy_cost"], {"hold-v1": 55, "relist-v1": None})
        self.assertEqual(market["failures"]["relist-v1"]["unknown"], 1)
        self.assertEqual(market["failures"]["relist-v1"]["expired"], 1)
        self.assertEqual(market["failures"]["hold-v1"]["unreconciled"], 1)
        self.assertEqual(market["game_versions"], ["0.999.1", "0.999.2"])
        self.assertEqual(market["decide"]["metric"], tr.MARKET_METRIC)
        self.assertEqual(market["decide"]["decision"], INSUFFICIENT)
        self.assertEqual(market["trial_status"], "complete")
        self.assertTrue(market["limitations"][0].startswith("The market-trial format is provisional"))
        self.assertIn("Market return (starbux, completed trades): hold-v1 110, relist-v1 185.",
                      tr.study_to_markdown(report))

    # Verifies: S12-R1, S12-R4
    def test_single_arm_market_trial_has_no_decide_and_is_judged_on_its_sample(self):
        trades = [trade("s1", "sell", "completed", 1, proceeds=80, fees=20), trade("b1", "buy", "requested", 2)]
        market = self.study(tr.build_study_report({"market": market_trial(trades, arms=("sell-buy-v1",))}),
                            "R4-market")
        self.assertIsNone(market["decide"])
        self.assertIn("single-arm", market["decide_note"])
        self.assertEqual(market["sample_sizes"], {"sell-buy-v1": 1})
        self.assertEqual(market["conclusion"], "inconclusive")
        self.assertEqual(market["inconclusive_reasons"],
                         ["sell-buy-v1 has 1 completed trade(s), under the minimum of 2"])
        self.assertIsNone(market["primary_result"]["arms"][0]["interval_95"])

        trades.append(trade("s2", "sell", "completed", 3, proceeds=82, fees=18))
        market = self.study(tr.build_study_report({"market": market_trial(trades, arms=("sell-buy-v1",))}),
                            "R4-market")
        self.assertEqual((market["conclusion"], market["inconclusive_reasons"]), ("conclusive", []))

    # Verifies: S12-R1
    def test_malformed_market_trials_are_refused(self):
        completed = trade("s1", "sell", "completed", 1, "hold-v1", proceeds=80)
        cases = {
            "unknown key": market_trial(extra=1),
            "secret": market_trial(notes=f"contact {SYNTHETIC_EMAIL}"),
            "version": market_trial(format_version=2),
            "three arms": market_trial(arms=("a", "b", "c")),
            "no proceeds": market_trial([{**completed, "proceeds": None}]),
            "outside window": market_trial([trade("s1", "sell", "listed", 30, "hold-v1")]),
            "buy listed": market_trial([trade("b1", "buy", "listed", 1, "hold-v1")]),
            "no arm": market_trial([{k: v for k, v in completed.items() if k != "arm"}]),
            "repeated id": market_trial([completed, completed]),
            "negative fee": market_trial([{**completed, "fees": -1}]),
        }
        for name, payload in cases.items():
            with self.subTest(case=name), self.assertRaises(tr.TrialInputError):
                tr.build_study_report({"market": payload})

    # Verifies: S12-R1
    def test_mismatched_or_malformed_trial_outputs_are_refused(self):
        with self.assertRaises(tr.TrialInputError):
            tr.build_study_report({"progression": pvp_report()})
        broken = json.loads(tr.to_json(progression_report()))
        del broken["arms"][0]["growth_velocity"]
        with self.assertRaises(tr.TrialInputError) as caught:
            tr.build_study_report({"progression": broken})
        self.assertNotIn("growth", str(caught.exception))
        with self.assertRaises(tr.TrialInputError):
            tr.build_study_report({"other": {}})
        secret = json.loads(tr.to_json(progression_report()))
        secret["limitations"].append(f"owner {SYNTHETIC_EMAIL}")
        with self.assertRaises(tr.TrialInputError):
            tr.build_study_report({"progression": secret})


class ReproducibilityTests(StudyTestCase):
    def outputs(self):
        return [self.write("progression.json", progression_report()), self.write("pvp.json", pvp_report()),
                self.write("fleet.json", fleet_report(self.root)), self.write("market.json", market_trial())]

    def run_study(self, paths, name):
        out = self.root / name
        args = ["--study", "--json", str(out / "study.json"), "--markdown", str(out / "study.md")]
        for path in paths:
            args += ["--trial", str(path)]
        out.mkdir()
        self.assertEqual(tr.main(args), 0)
        return (out / "study.json").read_bytes(), (out / "study.md").read_bytes()

    # Verifies: S12-R6
    def test_the_same_trial_outputs_give_byte_identical_reports(self):
        paths = self.outputs()
        first = self.run_study(paths, "first")
        second = self.run_study(list(reversed(paths)), "second")
        self.assertEqual(first, second)
        report = json.loads(first[0])
        self.assertEqual(report["summary"]["reported"], ["A", "C", "D", "R4-market"])
        sources = {s["study"]: s["source"] for s in report["studies"]}
        self.assertEqual(sources["C"]["file"], "pvp.json")
        self.assertRegex(sources["C"]["sha256"], r"^sha256:[0-9a-f]{16}$")
        trials = {kind: json.loads(path.read_text("utf-8"))
                  for kind, path in zip(("progression", "pvp", "fleet", "market"), paths)}
        self.assertEqual(report, tr.build_study_report(trials, tr.load_study_inputs(paths)[1]))
        self.assertEqual(first[1].decode("utf-8"), tr.study_to_markdown(report))
        self.assertIn("| C | pvp.json | sha256:", first[1].decode("utf-8"))

        changed = json.loads(paths[1].read_text("utf-8"))
        changed["attack"]["per_strategy"][0]["wins"] -= 1
        paths[1].write_text(tr.to_json(changed), encoding="utf-8")
        third = self.run_study(paths, "third")
        self.assertNotEqual(first[0], third[0])

    # Verifies: S12-R6
    def test_study_reads_only_trial_outputs(self):
        paths = self.outputs()
        trial = str(paths[0])
        cases = [
            ["--study", "--trial", trial, "--plan", trial],
            ["--study", "--trial", trial, "--now", "2027-01-25T00:00:00+00:00"],
            ["--study", "--trial", trial, "--journal", "a=b"],
            ["--study", "--trial", trial, "--trial", trial],  # two outputs for one study
            ["--study", "--trial", str(self.root / "missing.json")],
            ["--study", "--trial", str(self.write("plan.json", progression.plan_payload()))],  # not an output
            ["--plan", trial, "--trial", trial],  # --trial without --study
        ]
        for args in cases:
            with self.subTest(args=args), redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(tr.main(args), 2)
                self.assertNotIn(str(self.root), stderr.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            tr.main(["--pvp"])  # every per-trial report still needs --plan
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            tr.main(["--study", "--fleet", "--trial", trial])

    # Verifies: S12-R6
    def test_study_without_outputs_prints_markdown(self):
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            self.assertEqual(tr.main(["--study"]), 0)
        self.assertIn("# Automation effectiveness study: figures", stdout.getvalue())
        self.assertEqual(stdout.getvalue().count("**inconclusive**"), 8)


if __name__ == "__main__":
    unittest.main()
