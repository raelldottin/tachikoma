"""Tests for ``scripts/trial_report.py --fleet`` (SPEC-10 R3-R6). Synthetic journals and run records only.

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
from sdk.experiments import ADOPT, INSUFFICIENT
from sdk.fleet import registry_from_dict
from sdk.journal import GameplayEvent, GameplayJournal
from tests.synthetic_fixtures import SYNTHETIC_EMAIL
from tests.test_fleet_execute import FleetExecuteTestCase

ALPHA, BETA = "alpha-ship", "beta-ship"
GID = {ALPHA: "gid1-" + "a" * 32, BETA: "gid1-" + "b" * 32}
T0 = datetime(2027, 4, 5, tzinfo=timezone.utc)  # sequential week, then concurrent week
SWITCH = T0 + timedelta(days=7)
END = T0 + timedelta(days=14)
NOW = END + timedelta(days=1)
AUTH_HASH = "sha256:" + "1" * 64
REGISTRY_HASH = "sha256:" + "2" * 64
# Verified actions per run (alpha, beta): the concurrent runs verify about twice as many per hour.
SEQUENTIAL = [(2, 2), (2, 3), (3, 3)]
CONCURRENT = [(5, 5), (5, 6), (6, 6)]


def plan_payload(**changes):
    payload = {
        "trial_id": "r5-fleet-trial",
        "hypothesis": "concurrent runs verify more actions per hour than sequential runs",
        "accounts": [ALPHA, BETA],
        "schedule": [{"mode": "sequential", "start": T0.isoformat(), "end": SWITCH.isoformat()},
                     {"mode": "concurrent", "start": SWITCH.isoformat(), "end": END.isoformat()}],
        "min_sample": 3,
        "window_start": T0.isoformat(),
        "window_end": END.isoformat(),
        "manual_minutes": {ALPHA: 30},
    }
    payload.update(changes)
    return payload


def registry_payload(identities=None):
    identities = GID if identities is None else identities
    accounts = []
    for alias in (ALPHA, BETA):
        profile = {"alias": alias, "objective": "grow", "policy_id": "baseline-v1",
                   "credentials_secret": alias.split("-")[0].upper() + "_AUTH"}
        if identities.get(alias):
            profile["expected_game_identity"] = identities[alias]
        accounts.append(profile)
    return {"version": 1, "accounts": accounts}


def action(alias, at, outcome="verified", error_class=None, attempted=True):
    return GameplayEvent.create(
        account_alias=alias, action_id="collect_all_resources", started_at=at, finished_at=at + timedelta(minutes=1),
        outcome=outcome if attempted else "skipped", reason="synthetic", attempted=attempted,
        error_class=error_class, dry_run=not attempted,
    )


class Trial:
    """Writes synthetic execute journal directories and run records under one temporary root."""

    def __init__(self, root):
        self.root = Path(root)

    def run(self, name, start, verified, mode="record", failed=None, uncertain=None, extra=None, record=None,
            identities=None):
        """One execute run of an hour: a dry-run event, then each account's actions three minutes apart."""
        directory = self.root / name
        (directory / "runs").mkdir(parents=True, exist_ok=True)
        failed, uncertain, extra = failed or {}, uncertain or {}, extra or {}
        identities = GID if identities is None else identities
        accounts = []
        for alias, count in zip((ALPHA, BETA), verified):
            journal = GameplayJournal(directory / f"{alias}.log")
            journal.append(action(alias, start - timedelta(hours=2), attempted=False))  # the day's dry run
            outcomes = (["verified"] * count + ["not-observed"] * failed.get(alias, 0)
                        + ["unknown"] * uncertain.get(alias, 0))
            for index, outcome in enumerate(outcomes):
                journal.append(action(alias, start + timedelta(minutes=1 + 3 * index), outcome,
                                      error_class="ServerError" if outcome == "not-observed" else None))
            for event in extra.get(alias, ()):
                journal.append(event)
            accounts.append({"alias": alias, "status": "completed", "reason": None,
                             "game_identity": identities.get(alias), "attempted": len(outcomes),
                             "verified": count, "results": []})
        payload = {
            "version": 1, "kind": "fleet-execute", "mode": "full",
            "started_at": start.isoformat(), "finished_at": (start + timedelta(hours=1)).isoformat(),
            "authorization_hash": AUTH_HASH, "registry_hash": REGISTRY_HASH,
            "policy_versions": {ALPHA: "baseline-v1", BETA: "baseline-v1"},
            "game_version": {"client": "0.999.1", "server": None}, "halt_share": 0, "stop_reason": None,
            "accounts": accounts, "authorization": {"owner": "owner"},
        }
        if mode == "record":
            payload["orchestrator"] = "concurrent" if start >= SWITCH else "sequential"
        elif mode is not None:
            payload["orchestrator"] = mode
        payload.update(record or {})
        path = directory / "runs" / (start.strftime("%Y%m%dT%H%M%SZ") + ".json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return directory

    def standard(self, concurrent_changes=None):
        """Three sequential and three concurrent daily runs; a beta failure burst and an alpha uncertain burst."""
        dirs = []
        for day, verified in enumerate(SEQUENTIAL):
            options = {"failed": {BETA: 2}} if day == 2 else {}
            dirs.append(self.run(f"seq-{day}", T0 + timedelta(days=day, hours=10), verified, **options))
        for day, verified in enumerate(CONCURRENT):
            options = {"uncertain": {ALPHA: 2}} if day == 2 else {}
            options.update((concurrent_changes or {}).get(day, {}))
            dirs.append(self.run(f"conc-{day}", SWITCH + timedelta(days=day, hours=10), verified, **options))
        return dirs


def build(dirs, plan=None, registry=None, minutes=None):
    plan = tr.fleet_plan_from_dict(plan or plan_payload())
    registry = registry_from_dict(registry or registry_payload())
    return tr.build_fleet_trial_report(plan, registry, tr.load_fleet_runs(dirs), minutes, now=NOW)


class FleetTrialTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("network used"))
        patcher.start()
        self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.trial = Trial(self.root / "journals")


class IsolationStopTests(FleetTrialTestCase):
    # Verifies: S10-R3
    def test_foreign_event_stops_the_trial_and_is_reported(self):
        foreign = action(BETA, SWITCH + timedelta(days=1, hours=10, minutes=40))
        dirs = self.trial.standard({1: {"extra": {ALPHA: [foreign]}}})
        report = build(dirs)
        self.assertEqual(report["status"], "stopped")
        stop_run = "20270413T100000Z"
        self.assertEqual(report["stop"]["run_id"], stop_run)
        rules = [incident["rule"] for incident in report["incidents"]]
        self.assertIn("foreign-event", rules)
        self.assertTrue(all(incident["run_id"] == stop_run for incident in report["incidents"]))
        self.assertEqual(report["incidents"][0]["attempt_ids"], [foreign.attempt_id])
        runs = {run["run_id"]: run for run in report["runs"]}
        self.assertFalse(runs[stop_run]["audit"]["clean"])
        self.assertTrue(runs[stop_run]["counted"])
        later = runs["20270414T100000Z"]
        self.assertFalse(later["counted"])
        self.assertIn("after the trial stopped", later["excluded"])
        self.assertEqual(report["modes"]["concurrent"]["runs"], 2)
        self.assertIsNone(report["recommendation"])
        self.assertIn("critical", {a["severity"] for a in runs[stop_run]["alerts"]["alerts"]})
        self.assertTrue(report["limitations"][0].startswith(f"The trial stopped at run {stop_run}"))
        markdown = tr.fleet_to_markdown(report)
        self.assertIn("## Trial stopped", markdown)
        self.assertIn(f"| {stop_run} | foreign-event | {ALPHA} |", markdown)

    # Verifies: S10-R3
    def test_events_between_runs_are_audited_with_the_next_run(self):
        sneaked = action(BETA, T0 + timedelta(days=1, hours=5))  # between the first and second sequential run
        dirs = [self.trial.run("all", T0 + timedelta(days=day, hours=10), verified)
                for day, verified in enumerate(SEQUENTIAL)]
        GameplayJournal(dirs[0] / f"{ALPHA}.log").append(sneaked)
        report = build(dirs[:1])
        self.assertEqual(report["stop"]["run_id"], "20270406T100000Z")
        self.assertIn("foreign-event", report["runs"][1]["audit"]["by_rule"])
        self.assertTrue(report["runs"][0]["audit"]["clean"])

    # Verifies: S10-R3
    def test_every_run_is_audited_and_a_clean_trial_does_not_stop(self):
        report = build(self.trial.standard())
        self.assertIsNone(report["stop"])
        self.assertEqual(report["incidents"], [])
        self.assertEqual(len(report["runs"]), 6)
        for run in report["runs"]:
            self.assertTrue(run["audit"]["clean"])
            self.assertEqual(run["audit"]["accounts_checked"], 2)
            self.assertGreater(run["events_audited"], 0)
        self.assertIn("_Every run's audit was clean._", tr.fleet_to_markdown(report))

    # Verifies: S10-R3
    def test_run_records_without_identity_enforcement_stop_the_trial(self):
        report = build(self.trial.standard(), registry=registry_payload({ALPHA: GID[ALPHA]}))
        self.assertEqual(report["stop"]["run_id"], "20270405T100000Z")
        self.assertEqual({(i["rule"], i["account_alias"]) for i in report["stop"]["incidents"]},
                         {(tr.IDENTITY_RULE, BETA)})

        other = Trial(self.root / "mismatch")
        dirs = [other.run("one", T0 + timedelta(hours=10), (1, 1), identities={ALPHA: GID[BETA], BETA: GID[BETA]})]
        report = build(dirs)
        self.assertEqual(report["status"], "stopped")
        self.assertIn("other than its expected_game_identity", report["incidents"][0]["detail"])


class ComparisonTests(FleetTrialTestCase):
    # Verifies: S10-R4, S10-R5, S10-R6
    def test_sequential_and_concurrent_journals_produce_the_comparison_and_alerts(self):
        report = build(self.trial.standard())
        self.assertEqual(report["status"], "complete")
        seq, conc = report["modes"]["sequential"], report["modes"]["concurrent"]
        self.assertEqual((seq["runs"], conc["runs"]), (3, 3))
        self.assertEqual(seq["verified_actions_per_hour"]["n"], 3)
        self.assertEqual(seq["verified_actions_per_hour"]["mean"], 5.0)
        self.assertEqual(conc["verified_actions_per_hour"]["mean"], 11.0)
        self.assertEqual((seq["attempted"], seq["failed"], seq["uncertain"]), (17, 2, 0))
        self.assertEqual((conc["attempted"], conc["failed"], conc["uncertain"]), (35, 0, 2))
        self.assertEqual(seq["failure_rate"], {"n": 17, "rate": round(2 / 17, 4),
                                               "interval_95": seq["failure_rate"]["interval_95"]})
        self.assertEqual(conc["uncertain_rate"]["rate"], round(2 / 35, 4))
        self.assertEqual((seq["incidents"], conc["incidents"]), (0, 0))
        decisions = report["decisions"]
        self.assertEqual(set(decisions), {"verified-actions-per-hour", "failure-rate", "uncertain-rate"})
        self.assertEqual(decisions["verified-actions-per-hour"]["decision"], ADOPT)
        self.assertEqual((decisions["verified-actions-per-hour"]["baseline"],
                          decisions["verified-actions-per-hour"]["candidate"]), ("sequential", "concurrent"))
        self.assertEqual((decisions["failure-rate"]["baseline_n"], decisions["failure-rate"]["candidate_n"]),
                         (17, 35))
        self.assertEqual(report["recommendation"], "concurrent")

        # R6: alerts are evaluated per run at the run's end.
        alerts = {(run["run_id"], alert["rule_id"], alert["account_alias"])
                  for run in report["runs"] for alert in run["alerts"]["alerts"]}
        self.assertEqual(alerts, {("20270407T100000Z", "failure-rate-high", BETA),
                                  ("20270414T100000Z", "uncertain-rate-high", ALPHA)})
        self.assertEqual(report["runs"][0]["alerts"]["evaluated_at"], report["runs"][0]["finished_at"])
        self.assertFalse(report["runs"][0]["alerts"]["rules_not_evaluated"])
        self.assertEqual((seq["alerts"], conc["alerts"]), (1, 1))

        # R4: per-account and fleet metrics per run, with the mode.
        first = report["runs"][0]
        self.assertEqual(first["mode"], "sequential")
        self.assertEqual(first["fleet_report"]["mode"], "sequential")
        self.assertEqual([row["account_alias"] for row in first["fleet_report"]["accounts"]], [ALPHA, BETA])
        self.assertEqual(first["fleet_report"]["fleet"]["verified"], 4)
        self.assertEqual(conc["accounts"][ALPHA]["uncertain"], 2)
        self.assertEqual(seq["accounts"][BETA]["failed"], 2)

        markdown = tr.fleet_to_markdown(report)
        self.assertIn("## Mode comparison", markdown)
        self.assertIn("| verified-actions-per-hour | **adopt** | 3 / 3 |", markdown)
        self.assertIn("uncertain-rate-high", markdown)
        self.assertIn("failure-rate-high", markdown)

    # Verifies: S10-R4
    def test_time_saved_only_when_both_minute_figures_are_given(self):
        dirs = self.trial.standard()
        plan = tr.fleet_plan_from_dict(plan_payload())
        minutes = tr.fleet_minutes_from_list([
            {"run_id": "20270405T100000Z", "account_alias": ALPHA, "operator_minutes": 5},
            {"run_id": "20270405T100000Z", "account_alias": BETA, "operator_minutes": 4},
            {"run_id": "20270406T100000Z", "account_alias": ALPHA, "operator_minutes": None},
        ], plan)
        report = build(dirs, minutes=minutes)
        first = {row["account_alias"]: row for row in report["runs"][0]["fleet_report"]["accounts"]}
        self.assertEqual(first[ALPHA]["time_saved_minutes"], 25)
        self.assertEqual(first[BETA]["operator_minutes"], 4)
        self.assertIsNone(first[BETA]["time_saved_minutes"])  # no manual routine minutes for beta
        second = {row["account_alias"]: row for row in report["runs"][1]["fleet_report"]["accounts"]}
        self.assertIsNone(second[ALPHA]["time_saved_minutes"])  # operator minutes not logged (null)
        self.assertEqual(report["time_saved_minutes"], 25)
        self.assertEqual(report["modes"]["sequential"]["accounts"][ALPHA]["time_saved_runs"], 1)

        report = build(dirs)
        self.assertIsNone(report["time_saved_minutes"])
        self.assertTrue(any(note.startswith("Time saved is not reported") for note in report["limitations"]))

    # Verifies: S10-R5
    def test_mode_comes_from_the_record_or_the_schedule_and_deviations_are_listed(self):
        dirs = [
            self.trial.run("a", T0 + timedelta(hours=10), (1, 1), mode=None),
            self.trial.run("b", T0 + timedelta(days=1, hours=10), (1, 1), mode="concurrent"),
            self.trial.run("c", END + timedelta(hours=10), (1, 1), mode=None),
        ]
        report = build(dirs)
        a, b, c = report["runs"]
        self.assertEqual((a["mode"], a["mode_source"], a["schedule_deviation"]), ("sequential", "schedule", None))
        self.assertEqual((b["mode"], b["scheduled_mode"]), ("concurrent", "sequential"))
        self.assertIn("schedule names sequential", b["schedule_deviation"])
        self.assertFalse(c["counted"])
        self.assertEqual(c["excluded"], "outside the trial window")
        self.assertEqual((report["modes"]["sequential"]["runs"], report["modes"]["concurrent"]["runs"]), (1, 1))
        self.assertEqual(report["decisions"]["verified-actions-per-hour"]["decision"], INSUFFICIENT)
        self.assertTrue(any("did not follow the schedule" in note for note in report["limitations"]))

    # Verifies: S10-R4
    def test_run_record_hashes_are_shortened_and_extra_fields_ignored(self):
        report = build(self.trial.standard())
        run = report["runs"][0]
        self.assertEqual(run["authorization_hash"], "sha256:" + "1" * 16)
        self.assertEqual(run["registry_hash"], "sha256:" + "2" * 16)
        self.assertEqual(run["execute_mode"], "full")
        self.assertEqual(run["game_version"], {"client": "0.999.1", "server": None})
        self.assertNotIn("authorization", run)
        self.assertNotIn("1" * 64, json.dumps(report))


class InputTests(FleetTrialTestCase):
    def test_plan_validation(self):
        for changes in ({"accounts": [ALPHA]}, {"accounts": [ALPHA, ALPHA]}, {"min_sample": 1},
                        {"schedule": [{"mode": "sequential", "start": T0.isoformat(), "end": END.isoformat()}]},
                        {"schedule": [{"mode": "parallel", "start": T0.isoformat(), "end": END.isoformat()}]},
                        {"manual_minutes": {"gamma-ship": 3}}, {"extra": 1},
                        {"hypothesis": f"mail {SYNTHETIC_EMAIL}"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                tr.fleet_plan_from_dict(plan_payload(**changes))

    def test_run_record_validation(self):
        for changes in ({"kind": "fleet-dry-run"}, {"version": 2}, {"orchestrator": "parallel"},
                        {"authorization_hash": "abc"}, {"finished_at": (T0 - timedelta(days=1)).isoformat()},
                        {"accounts": [{"alias": ALPHA, "status": "completed"}] * 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                trial = Trial(self.root / f"bad-{sorted(changes)[0]}")
                build([trial.run("x", T0 + timedelta(hours=10), (1, 1), record=changes)])

    def test_minutes_for_unknown_runs_or_accounts_are_refused(self):
        plan = tr.fleet_plan_from_dict(plan_payload())
        with self.assertRaises(ValueError):
            tr.fleet_minutes_from_list([{"run_id": "x", "account_alias": "gamma-ship", "operator_minutes": 3}], plan)
        minutes = tr.fleet_minutes_from_list([{"run_id": "nope", "account_alias": ALPHA, "operator_minutes": 3}],
                                             plan)
        with self.assertRaises(tr.TrialInputError):
            build(self.trial.standard(), minutes=minutes)


class CommandLineTests(FleetTrialTestCase):
    def write(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    # Verifies: S10-R3, S10-R4, S10-R5, S10-R6
    def test_fleet_report_end_to_end(self):
        dirs = self.trial.standard()
        plan, registry = self.write("plan.json", plan_payload()), self.write("registry.json", registry_payload())
        minutes = self.write("minutes.json", [{"run_id": "20270405T100000Z", "account_alias": ALPHA,
                                               "operator_minutes": 5}])
        out_json, out_md = self.root / "report.json", self.root / "report.md"
        record = next((dirs[-1] / "runs").glob("*.json"))
        args = ["--fleet", "--plan", str(plan), "--registry", str(registry), "--minutes", str(minutes),
                "--now", NOW.isoformat(), "--json", str(out_json), "--markdown", str(out_md)]
        args += [arg for directory in dirs[:-1] for arg in ("--run", str(directory))] + ["--run", str(record)]
        self.assertEqual(tr.main(args), 0)
        report = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(report["report"], "fleet-trial")
        self.assertEqual(len(report["runs"]), 6)
        self.assertEqual(report["time_saved_minutes"], 25)
        self.assertEqual(report["recommendation"], "concurrent")
        self.assertIn("# Fleet trial report: r5-fleet-trial", out_md.read_text(encoding="utf-8"))
        self.assertNotIn(str(self.root), out_json.read_text(encoding="utf-8"))

    def test_fleet_flag_combinations_are_refused(self):
        plan, registry = self.write("plan.json", plan_payload()), self.write("registry.json", registry_payload())
        run = str(self.trial.run("one", T0 + timedelta(hours=10), (1, 1)))
        cases = [
            ["--plan", str(plan), "--registry", str(registry), "--run", run],  # no --fleet
            ["--pvp", "--plan", str(plan), "--run", run],
            ["--fleet", "--plan", str(plan), "--run", run],  # no registry
            ["--fleet", "--plan", str(plan), "--registry", str(registry)],  # no run
            ["--fleet", "--plan", str(plan), "--registry", str(registry), "--run", run, "--journal", "a=b"],
            ["--fleet", "--plan", str(plan), "--registry", str(registry), "--run", str(self.root / "missing")],
            ["--fleet", "--plan", str(plan), "--registry", str(registry), "--run", run, "--run", run],
        ]
        for args in cases:
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                self.assertEqual(tr.main(args), 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            tr.main(["--fleet", "--pvp", "--plan", str(plan)])


class ExecuteRecordTests(FleetExecuteTestCase):
    """The report reads the journal directory and run records that ``run.py --fleet-execute`` writes."""

    # Verifies: S10-R3, S10-R4, S10-R5, S10-R6
    def test_report_reads_real_execute_run_records(self):
        self.dry_run()
        code, out, err = self.execute("--mode", "full")
        self.assertEqual(code, 0, err + out)
        code, out, err = self.execute("--mode", "full", "--concurrent", "2")
        self.assertEqual(code, 0, err + out)
        now = datetime.now(timezone.utc)
        plan = plan_payload(
            accounts=["alpha", "bravo"], manual_minutes={}, min_sample=2,
            window_start=(now - timedelta(days=1)).isoformat(), window_end=(now + timedelta(days=2)).isoformat(),
            schedule=[{"mode": "sequential", "start": (now - timedelta(days=1)).isoformat(),
                       "end": (now + timedelta(days=1)).isoformat()},
                      {"mode": "concurrent", "start": (now + timedelta(days=1)).isoformat(),
                       "end": (now + timedelta(days=2)).isoformat()}])
        report = tr.build_fleet_trial_report(
            tr.fleet_plan_from_dict(plan), registry_from_dict(json.loads(self.registry.read_text("utf-8"))),
            tr.load_fleet_runs([self.journal_dir]), now=now)
        self.assertIsNone(report["stop"], report["incidents"])
        first, second = report["runs"]
        self.assertEqual((first["mode"], second["mode"]), ("sequential", "concurrent"))
        self.assertEqual(second["mode_source"], "run record")
        self.assertIn("schedule names sequential", second["schedule_deviation"])
        self.assertEqual(first["execute_mode"], "full")
        self.assertRegex(first["registry_hash"], r"^sha256:[0-9a-f]{16}$")
        self.assertTrue(first["audit"]["clean"] and second["audit"]["clean"])
        for run_section in (first, second):
            attempted = {row["account_alias"]: row["throughput"]["attempted"]
                         for row in run_section["fleet_report"]["accounts"]}
            self.assertGreater(attempted["alpha"], 0)
            self.assertGreater(attempted["bravo"], 0)
            self.assertIn("isolation-incident", run_section["alerts"]["rules_evaluated"])
        self.assertEqual(report["modes"]["sequential"]["runs"], 1)
        self.assertEqual(report["modes"]["concurrent"]["runs"], 1)
        self.assertTrue(any("outside the plan: charlie" in note for note in report["limitations"]))


if __name__ == "__main__":
    unittest.main()
