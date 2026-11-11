"""Tests for the static HTML dashboard (sdk/reports/dashboard.py, run.py --report dashboard).

Synthetic journals only. Client, Device, getpass and socket are patched to
raise, proving the dashboard path never logs in or opens a connection.
"""

import io
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sdk.fleet.accounts import load_registry
from sdk.journal import GameplayEvent, GameplayJournal
from sdk.reports import dashboard
from tests.synthetic_fixtures import SYNTHETIC_EMAIL

T0 = datetime(2027, 5, 1, 9, 0, tzinfo=timezone.utc)
REGISTRY = {"version": 1, "accounts": [
    {"alias": "alpha", "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": "ALPHA_AUTH",
     "budgets": {"minerals": 100}},
    {"alias": "bravo", "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": "BRAVO_AUTH"},
]}
SCRIPT = "<script>alert(1)</script>"
ATTRIBUTE_BREAK = "\"><img src=x onerror=alert(1)>"


def event(alias, action_id="upgrade_room", start=0, outcome="verified", attempted=True, cost=None, evidence=None,
          reason="synthetic", error_class=None, index=0):
    begin = T0 + timedelta(minutes=start)
    return GameplayEvent.create(
        account_alias=alias, action_id=action_id, started_at=begin, finished_at=begin + timedelta(minutes=1),
        outcome=outcome, reason=reason, attempted=attempted, cost=cost or {}, evidence=evidence or {},
        error_class=error_class, attempt_id=f"{alias}-{start:04d}-{index}",
    )


def sample_events():
    return [
        event("alpha", cost={"minerals_delta": -150}),
        event("alpha", start=5, outcome="unknown", reason=SCRIPT, error_class="TimeoutError"),
        event("alpha", ATTRIBUTE_BREAK, start=8, outcome="contradicted", reason="effect not seen"),
        event("bravo", "run_pvp_battle", start=3, evidence={"evidence_tier": "outcome-observed", "trophies_delta": 12}),
        event("bravo", "run_pvp_battle", start=6, outcome="unknown"),
        event("alpha", start=20, outcome="skipped", attempted=False),
    ]


def forbidden(name):
    def _raise(*args, **kwargs):
        raise AssertionError(f"dashboard touched {name}")
    return _raise


class DashboardRenderTests(unittest.TestCase):
    def test_journal_text_is_escaped_and_never_becomes_markup(self):
        page = dashboard.render_html(dashboard.build_dashboard(sample_events()))
        self.assertNotIn("<script", page)
        self.assertNotIn("<img", page)
        self.assertNotIn(ATTRIBUTE_BREAK, page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("&quot;&gt;&lt;img src=x onerror=alert(1)&gt;", page)

    def test_secret_shaped_text_is_redacted(self):
        leaked = GameplayEvent(
            account_alias="alpha", action_id="upgrade_room", attempt_id="a-1", started_at=T0.isoformat(),
            finished_at=(T0 + timedelta(minutes=1)).isoformat(), outcome="unknown", attempted=True,
            reason=f"login failed for {SYNTHETIC_EMAIL}", error_class="AuthError",
        )
        page = dashboard.render_html(dashboard.build_dashboard([leaked]))
        self.assertNotIn(SYNTHETIC_EMAIL, page)
        self.assertIn("REDACTED_EMAIL", page)

    def test_page_is_self_contained_and_scriptless(self):
        page = dashboard.render_html(dashboard.build_dashboard(sample_events()))
        for marker in ("<script", "<link", "src=\"", "@import", "url(", "http://", "https://"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, page)
        self.assertIn("Content-Security-Policy", page)
        self.assertIn("default-src 'none'", page)
        self.assertIn("@media (prefers-color-scheme:dark)", page)
        self.assertTrue(page.startswith("<!doctype html>"))

    def test_sections_show_sample_sizes(self):
        page = dashboard.render_html(dashboard.build_dashboard(sample_events()))
        for anchor in ("summary", "progression", "pvp", "fleet", "audit", "failures"):
            self.assertIn(f"id=\"{anchor}\"", page)
        # alpha attempted 3 (one verified); bravo's two battles; fleet total 5.
        self.assertIn("33% <span class=\"n\">(n=3)</span>", page)
        self.assertIn("verified / attempted, n=5", page)
        self.assertIn("100% <span class=\"n\">(21%&ndash;100%, n=1)</span>", page)  # one known-result win
        self.assertIn("n/a <span class=\"n\">(n=0)</span>", page)  # no defenses
        self.assertIn("(n=3).", page)  # failure ledger: unknown, contradicted, unknown battle

    def test_missing_numbers_are_na_not_zero(self):
        board = dashboard.build_dashboard([])
        page = dashboard.render_html(board)
        self.assertIn("<div class=\"label\">Success rate</div><div class=\"value\">n/a</div>", page)
        self.assertIn("<div class=\"label\">Incidents</div><div class=\"value\">n/a</div>", page)
        self.assertIn("data window n/a", page)
        self.assertIn("No journal events.", page)
        self.assertIn("No battles.", page)
        self.assertIn("Audit not run", page)

    def test_without_registry_incidents_are_unknown(self):
        page = dashboard.render_html(dashboard.build_dashboard(sample_events()))
        self.assertIn("audit not run (no --registry)", page)
        self.assertNotIn("<div class=\"label\">Incidents</div><div class=\"value\">0</div>", page)

    def test_registry_adds_audit_incidents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            path.write_text(json.dumps(REGISTRY), encoding="utf-8")
            registry = load_registry(path)
        events = sample_events() + [event("charlie", start=30)]
        board = dashboard.build_dashboard(events, registry=registry)
        self.assertEqual(board.audit.by_rule(), {"budget-exceeded": 1, "unregistered-account": 1})
        page = dashboard.render_html(board)
        self.assertIn("<div class=\"tile alert\"><div class=\"label\">Incidents</div><div class=\"value\">2</div>",
                      page)
        self.assertIn("budget-exceeded 1, unregistered-account 1", page)
        self.assertIn("journal owner &#x27;charlie&#x27; is not registered", page)

    def test_same_events_render_the_same_bytes(self):
        first = dashboard.render_html(dashboard.build_dashboard(sample_events(), journal_names=["a.jsonl"]))
        second = dashboard.render_html(dashboard.build_dashboard(sample_events(), journal_names=["a.jsonl"]))
        self.assertEqual(first, second)


class DashboardWriteTests(unittest.TestCase):
    def test_write_is_atomic_and_replaces_the_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dashboard.html"
            target.write_text("old", encoding="utf-8")
            dashboard.write_dashboard("<p>new</p>", target)
            self.assertEqual(target.read_text(encoding="utf-8"), "<p>new</p>")
            self.assertEqual(sorted(os.listdir(tmp)), ["dashboard.html"])

    def test_failed_write_leaves_the_target_and_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dashboard.html"
            target.write_text("old", encoding="utf-8")
            with patch("sdk.reports.dashboard.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    dashboard.write_dashboard("<p>new</p>", target)
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(sorted(os.listdir(tmp)), ["dashboard.html"])

    def test_missing_directory_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                dashboard.write_dashboard("x", Path(tmp) / "absent" / "dashboard.html")


class DashboardCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def journal(self, path, events):
        path.parent.mkdir(parents=True, exist_ok=True)
        journal = GameplayJournal(path)
        for item in events:
            journal.append(item)
        return path

    def run_main(self, *args):
        argv = ["run.py", *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), \
                patch("sdk.client.Client", side_effect=forbidden("sdk.client.Client")), \
                patch("run.Client", side_effect=forbidden("run.Client")), \
                patch("run.Device", side_effect=forbidden("run.Device")), \
                patch("getpass.getpass", side_effect=forbidden("getpass")), \
                patch.object(socket, "socket", side_effect=forbidden("socket.socket")), \
                patch.object(socket, "create_connection", side_effect=forbidden("socket.create_connection")), \
                redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
            from run import main
            main()
        return exit_info.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_report_dashboard_writes_the_file_and_prints_its_path(self):
        path = self.journal(self.dir / "alpha.jsonl", sample_events())
        registry = self.dir / "registry.json"
        registry.write_text(json.dumps(REGISTRY), encoding="utf-8")
        output = self.dir / "out.html"
        code, out, err = self.run_main("--report", "dashboard", "--journal", str(path), "--registry", str(registry),
                                       "--output", str(output), "--login-email", SYNTHETIC_EMAIL)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"{output}\n")
        page = output.read_text(encoding="utf-8")
        self.assertIn("<title>Tachikoma dashboard</title>", page)
        self.assertIn("Journals: alpha.jsonl", page)
        self.assertIn("budget-exceeded 1", page)
        self.assertNotIn(SYNTHETIC_EMAIL, page)

    def test_same_journals_give_identical_files(self):
        pages = []
        for name in ("one", "two"):
            path = self.journal(self.dir / name / "alpha.jsonl", sample_events())
            output = self.dir / name / "dashboard.html"
            code, _, _ = self.run_main("--report", "dashboard", "--journal", str(path), "--output", str(output))
            self.assertEqual(code, 0)
            pages.append(output.read_bytes())
        self.assertEqual(pages[0], pages[1])

    def test_dashboard_requires_output(self):
        path = self.journal(self.dir / "alpha.jsonl", [event("alpha")])
        code, out, err = self.run_main("--report", "dashboard", "--journal", str(path))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--report dashboard needs --output PATH", err)

    def test_output_is_only_for_the_dashboard(self):
        path = self.journal(self.dir / "alpha.jsonl", [event("alpha")])
        code, _, err = self.run_main("--report", "progression", "--journal", str(path),
                                     "--output", str(self.dir / "x.html"))
        self.assertEqual(code, 2)
        self.assertIn("--output is only used with --report dashboard", err)

        code, _, err = self.run_main("--output", str(self.dir / "x.html"))
        self.assertEqual(code, 2)
        self.assertIn("only used with --report", err)
        self.assertFalse((self.dir / "x.html").exists())

    def test_output_never_overwrites_a_journal(self):
        path = self.journal(self.dir / "alpha.jsonl", [event("alpha")])
        before = path.read_bytes()
        code, _, err = self.run_main("--report", "dashboard", "--journal", str(path), "--output", str(path))
        self.assertEqual(code, 2)
        self.assertIn("would overwrite a journal", err)
        self.assertEqual(path.read_bytes(), before)

    def test_output_never_overwrites_the_registry(self):
        path = self.journal(self.dir / "alpha.jsonl", [event("alpha")])
        registry = self.dir / "fleet.json"
        registry.write_text(json.dumps(REGISTRY), encoding="utf-8")
        before = registry.read_bytes()
        # The same file reached through a different spelling still counts.
        alias = self.dir / "sub" / ".." / "fleet.json"
        (self.dir / "sub").mkdir()
        code, out, err = self.run_main("--report", "dashboard", "--journal", str(path), "--registry", str(registry),
                                       "--output", str(alias))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("would overwrite the registry", err)
        self.assertEqual(registry.read_bytes(), before)

    def test_unwritable_output_and_missing_journal_exit_2(self):
        path = self.journal(self.dir / "alpha.jsonl", [event("alpha")])
        code, out, err = self.run_main("--report", "dashboard", "--journal", str(path),
                                       "--output", str(self.dir / "absent" / "d.html"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("cannot write dashboard: output directory not found", err)

        code, _, err = self.run_main("--report", "dashboard", "--journal", str(self.dir / "absent.jsonl"),
                                     "--output", str(self.dir / "d.html"))
        self.assertEqual(code, 2)
        self.assertIn("journal not found", err)
        self.assertFalse((self.dir / "d.html").exists())


if __name__ == "__main__":
    unittest.main()
