"""Tests for scripts/trial_report.py (SPEC-07). Synthetic snapshots, journals, minutes and logs only.

Every test runs with sockets patched to raise, so nothing can reach the network.
"""

import io
import json
import socket
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import trial_report as tr
from sdk.experiments import ADOPT, INSUFFICIENT, KEEP_RUNNING, REJECT
from sdk.journal import GameplayEvent, GameplayJournal
from sdk.state import AccountMismatchError, build_snapshot
from tests.synthetic_fixtures import SYNTHETIC_EMAIL

ALIAS = "trial-ship"
WINDOW_START = datetime(2027, 1, 4, tzinfo=timezone.utc)
WINDOW_END = datetime(2027, 2, 1, tzinfo=timezone.utc)
NOW = datetime(2027, 1, 25, tzinfo=timezone.utc)
MANUAL_GROWTH = [(1, 0), (0, 0), (1, 1), (0, 1), (1, 0), (0, 0)]  # (room upgrades, research completions) per day
AUTO_GROWTH = [(2, 1), (3, 1), (2, 2), (3, 2), (2, 1), (3, 2)]


def plan_payload(**changes):
    payload = {
        "trial_id": "r2-progression-trial",
        "hypothesis": "baseline-v1 completes more upgrades and research per day than the manual routine",
        "account_alias": ALIAS,
        "arms": ["manual", "baseline-v1"],
        "metric": "growth-per-day",
        "higher_is_better": True,
        "min_sample": 5,
        "window_start": WINDOW_START.isoformat(),
        "window_end": WINDOW_END.isoformat(),
    }
    payload.update(changes)
    return payload


def make_plan(**changes):
    return tr.plan_from_dict(plan_payload(**changes))


def series(start, growth, alias=ALIAS, version="0.999.1", minerals_per_day=500, missing_research_on=()):
    """Daily snapshots at 06:00 UTC from ``start``; day k applies growth[k] between snapshot k and k+1."""
    rooms = [{"@RoomId": str(i), "@RoomDesignId": str(100 + i), "@RoomStatus": "Normal"} for i in range(8)]
    research = [{"@ResearchDesignId": "r-active", "@ResearchState": "Researching"}]
    snapshots, minerals, counter = [], 1000, 0
    for day in range(len(growth) + 1):
        if day:
            upgrades, completions = growth[day - 1]
            for room in rooms[:upgrades]:
                room["@RoomDesignId"] = str(int(room["@RoomDesignId"]) + 1000)
            for _ in range(completions):
                counter += 1
                research.append({"@ResearchDesignId": f"r{counter}", "@ResearchState": "Completed"})
            minerals += minerals_per_day
        snapshots.append(build_snapshot(
            alias, start + timedelta(days=day, hours=6), "synthetic", game_version=version,
            profile={"@Trophy": "1000"}, ship={"@ShipLevel": "5"},
            resources={"minerals": str(minerals), "gas": "200", "starbux": "50"},
            rooms=[dict(room) for room in rooms],
            research=None if day in missing_research_on else [dict(entry) for entry in research],
            inventory=[], crew=[],
        ))
    return snapshots


def event(at, outcome="verified", action_id="upgrade_room", alias=ALIAS, error_class=None, cost=None):
    return GameplayEvent.create(
        account_alias=alias, action_id=action_id, started_at=at, finished_at=at + timedelta(minutes=1),
        outcome=outcome, reason=f"synthetic {outcome}", attempted=True, error_class=error_class,
        cost=cost if cost is not None else {"minerals_delta": -100, "gas_delta": 0, "starbux_delta": 0},
    )


AUTO_START = WINDOW_START + timedelta(days=7)


def automated_journal(days=6, failures_on=()):
    events = []
    for day in range(days):
        base = AUTO_START + timedelta(days=day, hours=8)
        events.append(event(base, action_id="collect_all_resources",
                            cost={"minerals_delta": 300, "gas_delta": 50, "starbux_delta": 0}))
        events.append(event(base + timedelta(hours=1)))
        if day in failures_on:
            events.append(event(base + timedelta(hours=2), outcome="unknown", action_id="add_research",
                                error_class="TimeoutError", cost={}))
        else:
            events.append(event(base + timedelta(hours=2), action_id="add_research"))
    return events


def minutes_payload():
    entries = [{"day": (WINDOW_START + timedelta(days=d)).date().isoformat(), "arm": "manual",
                "operator_minutes": 30 + d} for d in range(6)]
    entries += [{"day": (AUTO_START + timedelta(days=d)).date().isoformat(), "arm": "baseline-v1",
                 "operator_minutes": 5 + d % 2} for d in range(6)]
    entries.append({"day": (AUTO_START + timedelta(days=6)).date().isoformat(), "arm": "baseline-v1",
                    "operator_minutes": None})
    return entries


INTERVENTIONS = [{"at": (WINDOW_START + timedelta(days=2, hours=12)).isoformat(), "kind": "game-update",
                  "note": "balance patch released", "arm": "manual"}]


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("network used"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, plan=None, snapshots=None, journals=None, minutes=None, interventions=(), now=NOW):
        plan = plan or make_plan()
        if snapshots is None:
            snapshots = {"manual": series(WINDOW_START, MANUAL_GROWTH), "baseline-v1": series(AUTO_START, AUTO_GROWTH)}
        if journals is None:
            journals = {"baseline-v1": automated_journal()}
        return tr.build_trial_report(plan, snapshots, journals, minutes,
                                     tr.interventions_from_list(list(interventions), plan), now=now)

    @staticmethod
    def arm(report, name):
        return next(arm for arm in report["arms"] if arm["arm"] == name)


class ExitReportTests(OfflineTestCase):
    # Verifies: S07-R4, S07-R7
    def test_report_has_every_field_and_the_right_decision(self):
        plan = make_plan()
        report = self.build(plan, minutes=tr.minutes_from_list(minutes_payload(), plan), interventions=INTERVENTIONS)

        self.assertEqual(report["status"], ADOPT)
        self.assertEqual(report["decision"]["decision"], ADOPT)
        self.assertEqual((report["decision"]["baseline_n"], report["decision"]["candidate_n"]), (6, 6))
        self.assertEqual(report["game_version"], "0.999.1")
        self.assertIsNone(report["early_stop"])
        self.assertEqual(report["interventions"][0]["kind"], "game-update")
        self.assertTrue(any("serially correlated" in note for note in report["limitations"]))

        manual, auto = self.arm(report, "manual"), self.arm(report, "baseline-v1")
        self.assertEqual((manual["role"], auto["role"]), ("control", "candidate"))
        self.assertEqual(manual["sample_size"], 6)
        self.assertEqual(manual["growth_velocity"]["mean"], round(sum(map(sum, MANUAL_GROWTH)) / 6, 4))
        self.assertEqual(auto["growth_velocity"]["mean"], round(sum(map(sum, AUTO_GROWTH)) / 6, 4))
        self.assertEqual(auto["growth_velocity"]["room_design_advances"], sum(g[0] for g in AUTO_GROWTH))
        self.assertEqual(auto["growth_velocity"]["research_completions"], sum(g[1] for g in AUTO_GROWTH))
        self.assertIsNotNone(auto["growth_velocity"]["interval_95"])
        self.assertGreater(auto["growth_velocity"]["interval_95"][0], manual["growth_velocity"]["interval_95"][1])

        # Snapshot ledger: +500 minerals over each 24 h interval, the same computation in both arms.
        for arm in (manual, auto):
            self.assertEqual(arm["resources_per_hour"]["minerals"]["gained_per_hour"], round(500 / 24, 2))
            self.assertEqual(arm["resources_per_hour"]["minerals"]["spent"], 0)
            self.assertEqual(arm["resources_per_hour"]["gas"]["gained"], 0)

        self.assertEqual(auto["verified_success"], {"attempted": 18, "verified": 18, "rate": 1.0,
                                                    "interval_95": auto["verified_success"]["interval_95"]})
        self.assertEqual(auto["journal_resources"]["minerals"]["gained"], 6 * 300)
        self.assertEqual(auto["journal_resources"]["minerals"]["spent"], 6 * 200)
        self.assertEqual(auto["failures"]["failed"], 0)
        self.assertEqual(auto["failures"]["ledger"], [])

        self.assertEqual(manual["operator_minutes"]["n"], 6)
        self.assertEqual(manual["operator_minutes"]["mean"], 32.5)
        self.assertEqual(auto["operator_minutes"]["n"], 6)  # the null day is unobserved, not 0
        self.assertEqual(auto["operator_minutes"]["mean"], 5.5)
        self.assertEqual(report["operator_minutes_saved_per_account_day"], 27.0)

        markdown = tr.to_markdown(report)
        for heading in ("## Arms", "## Decision", "## Failure ledger", "## Interventions", "## Limitations"):
            self.assertIn(heading, markdown)
        self.assertIn("**adopt**", markdown)
        self.assertNotIn("## Early stop", markdown)
        self.assertEqual(json.loads(tr.to_json(report))["status"], ADOPT)

    # Verifies: S07-R7
    def test_decide_result_follows_the_sample_and_window(self):
        with self.subTest("below the minimum sample while the window is open"):
            report = self.build(make_plan(min_sample=10))
            self.assertEqual(report["decision"]["decision"], KEEP_RUNNING)
            self.assertTrue(any("under the planned minimum" in note for note in report["limitations"]))
        with self.subTest("below the minimum sample after the window closed"):
            report = self.build(make_plan(min_sample=10), now=WINDOW_END + timedelta(days=1))
            self.assertEqual(report["decision"]["decision"], INSUFFICIENT)
        with self.subTest("candidate worse"):
            report = self.build(snapshots={"manual": series(WINDOW_START, AUTO_GROWTH),
                                           "baseline-v1": series(AUTO_START, MANUAL_GROWTH)})
            self.assertEqual(report["decision"]["decision"], REJECT)
        with self.subTest("lower is better flips the verdict"):
            report = self.build(make_plan(higher_is_better=False))
            self.assertEqual(report["decision"]["decision"], REJECT)

    # Verifies: S07-R4
    def test_growth_is_computed_from_snapshots_never_journal_outcomes(self):
        flat = series(WINDOW_START, [(0, 0)] * 6)
        claims = automated_journal()  # 18 verified actions, 6 of them upgrade_room
        report = self.build(snapshots={"manual": flat, "baseline-v1": flat}, journals={"baseline-v1": claims})
        manual, auto = self.arm(report, "manual"), self.arm(report, "baseline-v1")
        self.assertEqual(auto["verified_success"]["verified"], 18)
        self.assertEqual(auto["growth_velocity"]["mean"], 0.0)
        for key in ("n", "mean", "interval_95", "room_design_advances", "research_completions"):
            self.assertEqual(manual["growth_velocity"][key], auto["growth_velocity"][key])

    # Verifies: S07-R4
    def test_research_started_or_still_running_is_not_growth(self):
        before, after = series(WINDOW_START, [(0, 0)])
        started = build_snapshot(
            ALIAS, after.observed_at, "synthetic",
            rooms=[{"@RoomId": "0", "@RoomDesignId": "100", "@RoomStatus": "Upgrading"}],
            research=[{"@ResearchDesignId": "r-active", "@ResearchState": "Researching"},
                      {"@ResearchDesignId": "r-new", "@ResearchState": "Researching"}],
        )
        units = tr.growth_units(tr.diff_snapshots(before, started))
        self.assertEqual(units, {"room_design_advances": 0, "research_completions": 0})

    # Verifies: S07-R4
    def test_unobserved_days_are_excluded_not_zero(self):
        manual = series(WINDOW_START, MANUAL_GROWTH, missing_research_on=(3,))
        gap = series(AUTO_START, AUTO_GROWTH)
        del gap[3]  # a two-day interval is not an account-day
        report = self.build(snapshots={"manual": manual, "baseline-v1": gap})
        manual_arm, auto_arm = self.arm(report, "manual"), self.arm(report, "baseline-v1")
        self.assertEqual(manual_arm["sample_size"], 4)
        self.assertEqual({d["reason"] for d in manual_arm["excluded_days"]},
                         {"rooms or research not observed on both sides"})
        self.assertEqual(auto_arm["sample_size"], 4)
        self.assertIn("not one account-day", auto_arm["excluded_days"][0]["reason"])

    # Verifies: S07-R4
    def test_exclude_day_intervention_removes_that_day_from_its_arm_only(self):
        log = [{"at": (WINDOW_START + timedelta(days=1, hours=9)).isoformat(), "kind": "premium-spend",
                "note": "starbux pack bought", "action": "exclude-day", "arm": "manual"}]
        report = self.build(interventions=log)
        manual = self.arm(report, "manual")
        self.assertEqual(manual["sample_size"], 5)
        self.assertEqual(manual["excluded_days"][0]["reason"], "excluded by the intervention log")
        self.assertEqual(self.arm(report, "baseline-v1")["sample_size"], 6)

    # Verifies: S07-R4
    def test_snapshots_of_another_account_are_refused(self):
        with self.assertRaises(AccountMismatchError):
            self.build(snapshots={"manual": series(WINDOW_START, MANUAL_GROWTH, alias="other-ship"),
                                  "baseline-v1": series(AUTO_START, AUTO_GROWTH)})

    # Verifies: S07-R7
    def test_unobserved_values_are_none_with_their_sample_size(self):
        report = self.build(journals={})
        manual = self.arm(report, "manual")
        self.assertEqual(manual["verified_success"], {"attempted": 0, "verified": 0, "rate": None,
                                                      "interval_95": None})
        self.assertIsNone(manual["failures"]["failure_rate"])
        self.assertEqual(manual["journal_resources"]["samples"], 0)
        self.assertIsNone(manual["journal_resources"]["hours"])
        self.assertIsNone(manual["journal_resources"]["minerals"]["gained"])
        self.assertIsNone(manual["journal_resources"]["minerals"]["gained_per_hour"])
        self.assertEqual(manual["operator_minutes"], {"n": 0, "mean": None, "interval_95": None,
                                                      "unit": "account-day", "total": None})
        self.assertIsNone(report["operator_minutes_saved_per_account_day"])

        empty = self.build(snapshots={"manual": [], "baseline-v1": []}, journals={})
        arm = self.arm(empty, "manual")
        self.assertEqual((arm["sample_size"], arm["growth_velocity"]["mean"]), (0, None))
        self.assertEqual(arm["resources_per_hour"]["minerals"]["samples"], 0)
        self.assertIsNone(arm["resources_per_hour"]["minerals"]["hours"])
        self.assertIsNone(arm["resources_per_hour"]["minerals"]["gained_per_hour"])
        self.assertIsNone(empty["game_version"])
        self.assertIn("No snapshot recorded a game version.", empty["limitations"])
        self.assertIn("n/a", tr.to_markdown(empty))

    # Verifies: S07-R7
    def test_failure_ledger_and_game_version_change_are_reported(self):
        auto = series(AUTO_START, AUTO_GROWTH)
        auto[4:] = [replace(snapshot, game_version="0.999.2") for snapshot in auto[4:]]
        report = self.build(snapshots={"manual": series(WINDOW_START, MANUAL_GROWTH), "baseline-v1": auto},
                            journals={"baseline-v1": automated_journal(failures_on=(2,))})
        arm = self.arm(report, "baseline-v1")
        self.assertEqual((arm["failures"]["failed"], arm["failures"]["uncertain"]), (1, 1))
        self.assertEqual(arm["failures"]["ledger"][0]["error_class"], "TimeoutError")
        self.assertEqual(arm["verified_success"]["rate"], round(17 / 18, 4))
        self.assertIsNone(report["game_version"])
        self.assertEqual(report["game_versions"], ["0.999.1", "0.999.2"])
        self.assertTrue(any("game version changed" in note for note in report["limitations"]))
        self.assertIn("TimeoutError", tr.to_markdown(report))

    # Verifies: S07-R7
    def test_secret_shaped_input_is_refused(self):
        plan = make_plan()
        with self.assertRaises(tr.TrialInputError) as caught:
            tr.interventions_from_list([{"at": NOW.isoformat(), "kind": "account-context", "note": SYNTHETIC_EMAIL}],
                                       plan)
        self.assertNotIn(SYNTHETIC_EMAIL, str(caught.exception))
        with self.assertRaises(tr.TrialInputError):
            tr.plan_from_dict(plan_payload(account_alias=SYNTHETIC_EMAIL))


class EarlyStopTests(OfflineTestCase):
    # Verifies: S07-R8
    def test_failure_rate_threshold_stops_the_trial_and_the_report_says_so(self):
        plan = make_plan(stop_thresholds={"max_failure_rate": 0.1, "failure_rate_min_attempts": 6})
        report = self.build(plan, journals={"baseline-v1": automated_journal(failures_on=(1, 2))})
        self.assertEqual(report["status"], "stopped-early")
        stop = report["early_stop"]
        self.assertEqual(stop["day"], (AUTO_START + timedelta(days=1)).date().isoformat())
        self.assertEqual(stop["crossings"], [{"arm": "baseline-v1", "threshold": "max_failure_rate",
                                              "value": round(1 / 6, 4), "limit": 0.1}])
        self.assertIn("stopped early", report["limitations"][0])
        markdown = tr.to_markdown(report)
        self.assertIn("## Early stop", markdown)
        self.assertIn("**stopped-early**", markdown)
        self.assertIn("for reference only", markdown)

    # Verifies: S07-R8
    def test_count_thresholds_stop_on_the_first_day_they_are_exceeded(self):
        plan = make_plan(stop_thresholds={"max_uncertain": 1})
        report = self.build(plan, journals={"baseline-v1": automated_journal(failures_on=(0, 4))})
        self.assertEqual(report["early_stop"]["day"], (AUTO_START + timedelta(days=4)).date().isoformat())
        self.assertEqual(report["early_stop"]["crossings"][0]["threshold"], "max_uncertain")

    # Verifies: S07-R8
    def test_starbux_spent_by_automation_always_stops(self):
        journal = automated_journal()
        journal.append(event(AUTO_START + timedelta(days=3, hours=12), action_id="speed_up",
                             cost={"minerals_delta": 0, "gas_delta": 0, "starbux_delta": -5}))
        report = self.build(make_plan(stop_thresholds={"max_starbux_spent": 100}), journals={"baseline-v1": journal})
        self.assertEqual(report["stop_thresholds"]["max_starbux_spent"], 0)
        self.assertEqual(report["status"], "stopped-early")
        self.assertEqual(report["early_stop"]["crossings"][0]["threshold"], "max_starbux_spent")

    # Verifies: S07-R8
    def test_an_event_of_another_account_stops_and_is_not_counted(self):
        journal = automated_journal()
        journal.append(event(AUTO_START + timedelta(days=2, hours=12), alias="other-ship"))
        report = self.build(journals={"baseline-v1": journal})
        arm = self.arm(report, "baseline-v1")
        self.assertEqual((arm["cross_account_events"], arm["verified_success"]["attempted"]), (1, 18))
        self.assertEqual(report["early_stop"]["crossings"][0]["threshold"], "max_cross_account_events")

    # Verifies: S07-R8
    def test_abort_arm_intervention_stops(self):
        log = [{"at": (AUTO_START + timedelta(days=2, hours=3)).isoformat(), "kind": "automation-failure",
                "note": "uncertain upgrade not reconciled within a day", "arm": "baseline-v1", "action": "abort-arm"}]
        report = self.build(interventions=log)
        self.assertEqual(report["early_stop"], {
            "day": (AUTO_START + timedelta(days=2)).date().isoformat(),
            "crossings": [{"arm": "baseline-v1", "threshold": "intervention", "value": "automation-failure",
                           "limit": "abort-arm"}],
        })

    # Verifies: S07-R8
    def test_no_stop_when_no_threshold_is_crossed(self):
        plan = make_plan(stop_thresholds={"max_failure_rate": 0.5, "max_uncertain": 3})
        report = self.build(plan, journals={"baseline-v1": automated_journal(failures_on=(1,))})
        self.assertIsNone(report["early_stop"])
        self.assertEqual(report["status"], ADOPT)

    # Verifies: S07-R8
    def test_unknown_or_invalid_thresholds_are_refused(self):
        for thresholds in ({"max_failures": 2}, {"max_failure_rate": 1.5}, {"max_uncertain": -1}):
            with self.subTest(thresholds=thresholds), self.assertRaises(tr.TrialInputError):
                make_plan(stop_thresholds=thresholds)


class CommandLineTests(OfflineTestCase):
    # Verifies: S07-R7, S07-R8
    def test_cli_writes_json_and_markdown_from_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "plan.json").write_text(json.dumps(plan_payload(stop_thresholds={"max_uncertain": 0})))
            manual_dir = root / "manual"
            manual_dir.mkdir()
            for number, snapshot in enumerate(series(WINDOW_START, MANUAL_GROWTH)):
                (manual_dir / f"{number:02d}.json").write_text(json.dumps(snapshot.to_dict()))
            (root / "auto.json").write_text(json.dumps([s.to_dict() for s in series(AUTO_START, AUTO_GROWTH)]))
            journal = GameplayJournal(root / "events.jsonl")
            for item in automated_journal(failures_on=(3,)):
                journal.append(item)
            (root / "minutes.json").write_text(json.dumps(minutes_payload()))
            (root / "log.json").write_text(json.dumps(INTERVENTIONS))

            code = tr.main([
                "--plan", str(root / "plan.json"), "--snapshots", f"manual={manual_dir}",
                "--snapshots", f"baseline-v1={root / 'auto.json'}", "--journal", f"baseline-v1={root / 'events.jsonl'}",
                "--minutes", str(root / "minutes.json"), "--interventions", str(root / "log.json"),
                "--now", NOW.isoformat(), "--json", str(root / "report.json"), "--markdown", str(root / "report.md"),
            ])
            self.assertEqual(code, 0)
            report = json.loads((root / "report.json").read_text())
            markdown = (root / "report.md").read_text()

        self.assertEqual(report["status"], "stopped-early")
        self.assertEqual(report["decision"]["decision"], ADOPT)
        self.assertEqual([arm["sample_size"] for arm in report["arms"]], [6, 6])
        self.assertEqual(report["arms"][0]["operator_minutes"]["n"], 6)
        self.assertIn("## Early stop", markdown)
        self.assertIn("balance patch released", markdown)

    # Verifies: S07-R7
    def test_cli_reports_bad_input_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            plan.write_text(json.dumps(plan_payload(arms=["manual"])))
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = tr.main(["--plan", str(plan), "--now", NOW.isoformat()])
        self.assertEqual(code, 2)
        self.assertIn("exactly two arms", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
