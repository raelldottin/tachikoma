"""Tests for the post-run step of run.py --fleet-execute (SPEC-11) with fakes. No network, no real credentials.

Exercises --fleet-execute with --dashboard-out and the SMTP flags (--smtp-email, --smtp-password-file,
--recipient): every execute run evaluates the alerts over its journals and the isolation audit once all
accounts have finished, records them, writes the dashboard, fails on a critical alert, and with the SMTP
flags sends one scrubbed summary e-mail. ``smtplib.SMTP`` is a fake and sockets are patched to raise.
"""

import json
import os
import smtplib
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import run  # imported here so run.py's log file opens in the repository, not a test tmpdir
from sdk.journal import GameplayEvent, GameplayJournal
from sdk.reports.alerts import Alert, AlertEvidence, AlertReport
from tests.test_fleet_dry_run import REAL_NAMES
from tests.test_fleet_execute import (
    ALPHA_AUTH,
    BRAVO_PASSWORD,
    REAL_REGISTRY,
    REAL_SALT_ENV,
    SALT,
    SECRET_VALUES,
    ExecClient,
    FleetExecuteTestCase,
    RealClientExecuteLeakTests,
    authorization_payload,
)

SMTP_SENDER = "alerts-sender@example.invalid"
SMTP_RECIPIENT = "operator@example.invalid"
SMTP_PASSWORD = "synthetic-smtp-password-0003"
USER_ID_PATTERN = r"(?i)user_?id['\"]?\s*[=:]\s*['\"]?\d"


class FakeSMTP:
    """Stands in for ``smtplib.SMTP``: records each connection and message, never opens a socket."""

    instances = []
    login_error = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.logins, self.messages, self.closed = [], [], False
        FakeSMTP.instances.append(self)

    def ehlo(self):
        return 250, b"ok"

    def starttls(self):
        return 220, b"ok"

    def login(self, user, password):
        self.logins.append((user, password))
        if FakeSMTP.login_error is not None:
            raise FakeSMTP.login_error

    def send_message(self, message):
        self.messages.append(message)

    def quit(self):
        self.closed = True

    @classmethod
    def sent(cls):
        return [message for instance in cls.instances for message in instance.messages]


class PostRunTestCase(FleetExecuteTestCase):
    def setUp(self):
        super().setUp()
        FakeSMTP.instances = []
        FakeSMTP.login_error = None
        patcher = patch.object(smtplib, "SMTP", FakeSMTP)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.password_file = self.dir / "smtp-password"
        self.password_file.write_text(SMTP_PASSWORD + "\n", encoding="utf-8")

    def smtp_flags(self):
        return ("--smtp-email", SMTP_SENDER, "--smtp-password-file", str(self.password_file),
                "--recipient", SMTP_RECIPIENT)

    def plant_overlapping_attempts(self, alias="alpha"):
        """Two verified attempts in flight at once: the audit's overlapping-attempts, a critical alert."""
        now = datetime.now(timezone.utc)
        journal = GameplayJournal(self.journal_dir / f"{alias}.log")
        for start, end in ((now - timedelta(hours=3), now - timedelta(hours=2)),
                           (now - timedelta(hours=2, minutes=30), now - timedelta(hours=1, minutes=30))):
            journal.append(GameplayEvent.create(account_alias=alias, action_id="upgrade_room", started_at=start,
                                                finished_at=end, outcome="verified", reason="planted", attempted=True))

    def record(self):
        records = self.records()
        self.assertEqual(len(records), 1)
        return records[0]


class AlertsAndDashboardTests(PostRunTestCase):
    # Verifies: S11-R1
    def test_an_execute_run_records_its_alerts_and_writes_the_dashboard_beside_the_journals(self):
        self.dry_run()
        code, out, err = self.execute()
        self.assertEqual(code, 0, err + out)
        record = self.record()
        alerts = record["alerts"]
        self.assertEqual(alerts["by_severity"], {"critical": 0, "warning": 0, "info": 0})
        self.assertEqual(alerts["alerts"], [])
        self.assertIn("isolation-incident", alerts["rules_evaluated"])
        self.assertEqual(alerts["rules_not_evaluated"], [], "the audit must be evaluated, not skipped")
        self.assertEqual(alerts["accounts_checked"], 2)
        self.assertGreater(alerts["events_checked"], 0)
        dashboard = self.journal_dir / "dashboard.html"
        self.assertEqual(record["dashboard"], str(dashboard))
        self.assertEqual(record["post_run_errors"], [])
        page = dashboard.read_text(encoding="utf-8")
        self.assertTrue(page.startswith("<!doctype html>"), page[:80])
        self.assertIn("alpha.log", page)
        self.assertIn("## Alerts", out)
        self.assertIn(f"- Dashboard: {dashboard}", out)
        self.assertIn("- Summary e-mail: disabled (no SMTP settings)", out)

    # Verifies: S11-R1
    def test_dashboard_out_chooses_the_path_and_is_checked_before_any_login(self):
        self.dry_run()
        target = self.dir / "reports" / "fleet.html"
        code, _, err = self.execute("--dashboard-out", str(target))
        self.assert_refused_before_login(code, err, f"dashboard directory not found: {target.parent}")
        for path in (self.journal_dir / "alpha.log", self.registry, self.authorization):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                code, _, err = self.execute("--dashboard-out", str(path))
                self.assert_refused_before_login(code, err, "dashboard path would overwrite")
                self.assertEqual(path.read_bytes(), before)
        target.parent.mkdir()
        code, out, err = self.execute("--dashboard-out", str(target))
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.record()["dashboard"], str(target))
        self.assertTrue(target.is_file())
        self.assertFalse((self.journal_dir / "dashboard.html").exists())

    # Verifies: S11-R1
    def test_only_an_execute_run_writes_a_dashboard(self):
        self.dry_run()
        self.assertFalse((self.journal_dir / "dashboard.html").exists())
        code, _, err = self.execute("--plan-only", *self.smtp_flags())
        self.assertEqual(code, 0, err)
        self.assertFalse((self.journal_dir / "dashboard.html").exists())
        self.assertEqual(FakeSMTP.instances, [])
        code, _, err = self.main("--fleet-dry-run", "--registry", str(self.registry), "--dashboard-out", "x.html")
        self.assertEqual(code, 2)
        self.assertIn("--dashboard-out is only used with --fleet-execute", err)

    # Verifies: S11-R1, S11-R2
    def test_a_critical_alert_fails_the_run_only_after_every_account_finished(self):
        for index, extra in enumerate(((), ("--concurrent", "2"))):
            with self.subTest(extra=extra):
                self.journal_dir = self.dir / f"journals-{index}"
                ExecClient.executed = []
                self.dry_run()
                self.plant_overlapping_attempts("alpha")
                seen_at_evaluation = []
                original = run._evaluate_fleet_alerts

                def evaluate(*args, **kwargs):
                    seen_at_evaluation.append(sorted(user for user, _ in ExecClient.executed))
                    return original(*args, **kwargs)
                with patch.object(run, "_evaluate_fleet_alerts", evaluate):
                    code, out, err = self.execute(*extra)
                self.assertEqual(code, 1, err + out)
                self.assertEqual(seen_at_evaluation, [["1001", "1002"]], "alerts are evaluated after all accounts ran")
                record = self.record()
                self.assertEqual({entry["alias"]: entry["status"] for entry in record["accounts"]},
                                 {"alpha": "completed", "bravo": "completed", "charlie": "disabled"})
                self.assertIsNone(record["stop_reason"])
                alerts = record["alerts"]
                self.assertEqual(alerts["by_severity"]["critical"], 1)
                (alert,) = alerts["alerts"]
                self.assertEqual((alert["severity"], alert["rule_id"], alert["account_alias"],
                                  alert["evidence"]["audit_rule"]),
                                 ("critical", "isolation-incident", "alpha", "overlapping-attempts"))
                self.assertIn("| critical | isolation-incident | alpha |", out)
                self.assertIn("- 1 critical alert: exit code 1", out)
                self.assertTrue((self.journal_dir / "dashboard.html").is_file())

    # Verifies: S11-R2
    def test_a_warning_alone_keeps_a_clean_exit(self):
        self.dry_run()
        warning = AlertReport("2026-11-11T00:00:00+00:00", 2, 4, ("contradicted-action",), (), (
            Alert("contradicted-action", "warning", "alpha", "1 of 1 attempt was contradicted", AlertEvidence()),))
        with patch.object(run, "_evaluate_fleet_alerts", lambda registry, journals, now: (None, warning)), \
                patch.object(run, "_fleet_report_sections", lambda *args: ["", "## Alerts"]):
            code, out, err = self.execute()
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.record()["alerts"]["by_severity"], {"critical": 0, "warning": 1, "info": 0})

    # Verifies: S11-R1
    def test_a_dashboard_that_cannot_be_written_is_recorded_and_fails_the_run(self):
        self.dry_run()
        with patch("sdk.reports.dashboard.write_dashboard", side_effect=OSError(28, "No space left on device")):
            code, out, err = self.execute()
        self.assertEqual(code, 1, err + out)
        record = self.record()  # the run record is still written
        self.assertIsNone(record["dashboard"])
        self.assertIsNotNone(record["alerts"])
        self.assertEqual(len(record["post_run_errors"]), 1)
        self.assertIn("dashboard not written: OSError", record["post_run_errors"][0])
        self.assertIn("- Dashboard: not written", out)


class SummaryEmailTests(PostRunTestCase):
    # Verifies: S11-R3
    def test_with_the_smtp_flags_one_summary_by_severity_is_sent(self):
        self.dry_run()
        self.plant_overlapping_attempts("alpha")
        code, out, err = self.execute(*self.smtp_flags())
        self.assertEqual(code, 1, err + out)  # the critical alert still fails the run
        self.assertEqual(len(FakeSMTP.instances), 1)
        (session,) = FakeSMTP.instances
        self.assertEqual(session.logins, [(SMTP_SENDER, SMTP_PASSWORD)])
        self.assertTrue(session.closed)
        (message,) = FakeSMTP.sent()
        self.assertEqual(message["to"], SMTP_RECIPIENT)
        self.assertEqual(message["subject"], "Tachikoma fleet execute (canary): 1 critical, 0 warning, 0 info")
        body = message.get_content()
        for expected in ("Accounts: alpha completed, bravo completed, charlie disabled.", "Critical (1)",
                         "- alpha / isolation-incident: 1 overlapping-attempts audit incident", "Warning (0)",
                         "Info (0)", "Dashboard: dashboard.html", f"Run record: {next((self.journal_dir / 'runs').glob('*.json')).name}"):
            self.assertIn(expected, body)
        self.assertLess(body.index("Critical (1)"), body.index("Warning (0)"))
        self.assertIn("- Summary e-mail: sent", out)

    # Verifies: S11-R3
    def test_without_the_smtp_flags_nothing_is_sent(self):
        self.dry_run()
        self.plant_overlapping_attempts("alpha")
        code, out, _ = self.execute()
        self.assertEqual(code, 1)
        self.assertEqual(FakeSMTP.instances, [])
        self.assertIn("- Summary e-mail: disabled (no SMTP settings)", out)

    # Verifies: S11-R3
    def test_incomplete_or_unreadable_smtp_settings_are_refused_before_any_login(self):
        self.dry_run()
        partial = [("--smtp-email", SMTP_SENDER), ("--recipient", SMTP_RECIPIENT),
                   ("--smtp-email", SMTP_SENDER, "--smtp-password-file", str(self.password_file))]
        for flags in partial:
            with self.subTest(flags=flags):
                code, _, err = self.execute(*flags)
                self.assert_refused_before_login(code, err, "incomplete SMTP configuration")
        self.password_file.write_text("  \n", encoding="utf-8")
        code, _, err = self.execute(*self.smtp_flags())
        self.assert_refused_before_login(code, err, "the SMTP password file is empty")
        self.password_file.unlink()
        code, _, err = self.execute(*self.smtp_flags())
        self.assert_refused_before_login(code, err, "cannot read the SMTP password file")
        self.assertEqual(FakeSMTP.instances, [])
        code, _, err = self.main("--fleet-dry-run", "--registry", str(self.registry), *self.smtp_flags())
        self.assertEqual(code, 2)
        self.assertIn("--fleet-dry-run cannot be combined with --smtp-email", err)

    # Verifies: S11-R3, S11-R4
    def test_a_failed_delivery_is_reported_without_the_password_and_keeps_the_exit_code(self):
        self.dry_run()
        FakeSMTP.login_error = smtplib.SMTPAuthenticationError(535, f"rejected {SMTP_SENDER} {SMTP_PASSWORD}".encode())
        code, out, err = self.execute(*self.smtp_flags())
        self.assertEqual(code, 0, err + out)
        self.assertEqual(len(FakeSMTP.instances), 1)
        self.assertEqual(FakeSMTP.sent(), [])
        self.assertIn("- Summary e-mail: failed (see the log)", out)
        self.assertIn("SMTP authentication failed", self.log.getvalue())
        for text in (out, err, self.log.getvalue()):
            self.assertNotIn(SMTP_PASSWORD, text)
            self.assertNotIn(SMTP_SENDER, text)


# Verifies: S11-R4
class AlertTextLeakTests(PostRunTestCase):
    """Alert text that carries secrets, ids or names is scrubbed in the record, the output and the e-mail."""

    def test_alert_and_summary_text_pass_the_dry_run_leak_scan(self):
        self.dry_run()
        self.plant_overlapping_attempts("alpha")
        original = run._evaluate_fleet_alerts
        leaky_text = f"failed for {ALPHA_AUTH} {BRAVO_PASSWORD} with {SALT} userId=1001 {SMTP_SENDER}"

        def leaky(registry, journals, now):
            audit, report = original(registry, journals, now)
            alert = Alert("contradicted-action", "warning", "bravo", leaky_text,
                          AlertEvidence(details=(leaky_text,)))
            return audit, AlertReport(report.evaluated_at, report.accounts_checked, report.events_checked,
                                      report.rules_evaluated, report.rules_not_evaluated, (*report.alerts, alert))
        with patch.object(run, "_evaluate_fleet_alerts", leaky):
            code, out, err = self.execute("--mode", "full", *self.smtp_flags())
        self.assertEqual(code, 1, err + out)
        (message,) = FakeSMTP.sent()
        summary = message["subject"] + "\n" + message.get_content()
        self.assertIn("Warning (1)", summary)
        self.assertIn("- bravo / contradicted-action: failed for", summary)
        record = json.dumps(self.record()["alerts"])
        self.assertIn("failed for", record)
        combined = "\n".join([summary, record, out, err, self.log.getvalue(),
                              (self.journal_dir / "dashboard.html").read_text(encoding="utf-8")])
        for value in (*SECRET_VALUES, SMTP_PASSWORD, SMTP_SENDER):
            self.assertNotIn(value, combined)
        self.assertNotRegex(combined, USER_ID_PATTERN)


# Verifies: S11-R4
class RealClientSummaryLeakTests(RealClientExecuteLeakTests):
    """With the real ``Client`` (player names, tokens and ids in its traffic), the summary and dashboard leak nothing."""

    test_no_secret_reaches_output_logs_journals_or_the_run_record = None  # the base class's test runs in its own module

    def test_no_secret_reaches_the_summary_the_dashboard_or_the_recorded_alerts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        cwd = os.getcwd()
        os.chdir(directory)
        self.addCleanup(os.chdir, cwd)
        registry, journals, authorization = directory / "fleet.json", directory / "journals", directory / "auth.json"
        password_file = directory / "smtp-password"
        password_file.write_text(SMTP_PASSWORD, encoding="utf-8")
        registry.write_text(json.dumps(REAL_REGISTRY), encoding="utf-8")
        authorization.write_text(json.dumps(authorization_payload()), encoding="utf-8")
        common = ("--registry", str(registry), "--fleet-journal-dir", str(journals), "--identity-salt-env",
                  REAL_SALT_ENV)
        FakeSMTP.instances = []
        with patch.object(smtplib, "SMTP", FakeSMTP):
            code, *texts, _ = self.run_main(directory, "--fleet-dry-run", *common)
            self.assertEqual(code, 0, texts)
            _, out, err, log, _ = self.run_main(
                directory, "--fleet-execute", "--authorization", str(authorization), "--mode", "full", *common,
                "--smtp-email", SMTP_SENDER, "--smtp-password-file", str(password_file), "--recipient",
                SMTP_RECIPIENT, execute=True)
        (message,) = FakeSMTP.sent()
        (record,) = [json.loads(path.read_text(encoding="utf-8")) for path in (journals / "runs").glob("*.json")]
        self.assertIsNotNone(record["alerts"])
        summary = message["subject"] + "\n" + message.get_content()
        combined = "\n".join([summary, json.dumps(record), out, err, log,
                              (journals / "dashboard.html").read_text(encoding="utf-8")])
        leaked = [value for value in (*self.FORBIDDEN, SMTP_PASSWORD, *REAL_NAMES.values()) if value in combined]
        self.assertEqual(leaked, [], combined)
        self.assertNotRegex(combined, USER_ID_PATTERN)


del RealClientExecuteLeakTests  # imported as a base class; do not run its tests twice
del FleetExecuteTestCase


if __name__ == "__main__":
    import unittest

    unittest.main()
