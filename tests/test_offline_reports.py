"""Tests for run.py --report: offline reports from journal files. No login, no network.

Exercises the flags --report, --journal, --registry and --report-format with
synthetic journals written to temporary directories. Client, Device, getpass
and socket are patched to raise, proving the report path never reaches them.
"""

import io
import json
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sdk.journal import GameplayEvent, GameplayJournal
from tests.synthetic_fixtures import SYNTHETIC_EMAIL

T0 = datetime(2027, 5, 1, 9, 0, tzinfo=timezone.utc)
REGISTRY = {"version": 1, "accounts": [
    {"alias": "alpha", "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": "ALPHA_AUTH",
     "budgets": {"minerals": 100}},
    {"alias": "bravo", "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": "BRAVO_AUTH"},
]}


def event(alias, action_id="upgrade_room", start=0, outcome="verified", attempted=True, cost=None, evidence=None):
    begin = T0 + timedelta(minutes=start)
    return GameplayEvent.create(
        account_alias=alias, action_id=action_id, started_at=begin, finished_at=begin + timedelta(minutes=1),
        outcome=outcome, reason="synthetic", attempted=attempted, cost=cost or {}, evidence=evidence or {},
    )


def forbidden(name):
    def _raise(*args, **kwargs):
        raise AssertionError(f"offline report touched {name}")
    return _raise


class OfflineReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def journal(self, name, events):
        path = self.dir / name
        journal = GameplayJournal(path)
        for item in events:
            journal.append(item)
        if not events:
            path.write_text("", encoding="utf-8")
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

    def test_progression_report_markdown_and_json(self):
        path = self.journal("alpha.jsonl", [
            event("alpha"), event("alpha", start=5, outcome="unknown"),
            event("alpha", start=10, outcome="skipped", attempted=False),
        ])
        code, out, _ = self.run_main("--report", "progression", "--journal", str(path))
        self.assertEqual(code, 0)
        self.assertIn("| alpha | 2027-05-01 | 2 | 1 | 50% |", out)
        self.assertIn("**Failure ledger**", out)

        code, out, _ = self.run_main("--report", "progression", "--journal", str(path), "--report-format", "json")
        self.assertEqual(code, 0)
        days = json.loads(out)
        self.assertEqual([(d["account_alias"], d["attempted"], d["verified"]) for d in days], [("alpha", 2, 1)])

    def test_pvp_report_has_no_strategy_without_a_schedule(self):
        path = self.journal("pvp.jsonl", [
            event("alpha", "run_pvp_battle", evidence={"evidence_tier": "outcome-observed", "trophies_delta": 12}),
            event("alpha", "run_pvp_battle", start=5,
                  evidence={"evidence_tier": "outcome-observed", "trophies_delta": -8}),
            event("alpha", "upgrade_room", start=10),
        ])
        code, out, _ = self.run_main("--report", "pvp", "--journal", str(path), "--report-format", "json")
        self.assertEqual(code, 0)
        rows = json.loads(out)["scorecard"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["strategy_key"], "none")
        self.assertEqual((rows[0]["attack"]["wins"], rows[0]["attack"]["losses"]), (1, 1))

        code, out, _ = self.run_main("--report", "pvp", "--journal", str(path))
        self.assertEqual(code, 0)
        self.assertIn("| alpha | none |", out)

    def test_fleet_report_groups_events_by_alias_across_journals(self):
        first = self.journal("one.jsonl", [event("alpha"), event("bravo", start=2)])
        second = self.journal("two.jsonl", [event("alpha", start=5), event("bravo", start=7, outcome="unknown")])
        code, out, _ = self.run_main("--report", "fleet", "--journal", str(first), "--journal", str(second),
                                     "--report-format", "json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        attempted = {row["account_alias"]: row["throughput"]["attempted"] for row in report["accounts"]}
        self.assertEqual(attempted, {"alpha": 2, "bravo": 2})
        self.assertEqual(report["fleet"]["attempted"], 4)
        self.assertEqual(report["incidents"], 0)

    def test_fleet_report_with_registry_runs_the_isolation_audit(self):
        path = self.journal("fleet.jsonl", [
            event("alpha", cost={"minerals_delta": -150}),
            event("charlie", start=3),
        ])
        registry = self.dir / "registry.json"
        registry.write_text(json.dumps(REGISTRY), encoding="utf-8")
        code, out, _ = self.run_main("--report", "fleet", "--journal", str(path), "--registry", str(registry))
        self.assertEqual(code, 0)
        self.assertIn("**Isolation incidents:** budget-exceeded 1, unregistered-account 1", out)
        self.assertIn("| **fleet** |", out)

    def test_empty_journal_prints_the_empty_report(self):
        path = self.journal("empty.jsonl", [])
        for kind, expected in (("progression", "_No journal events._\n"), ("pvp", "_No battles._\n")):
            with self.subTest(kind=kind):
                code, out, _ = self.run_main("--report", kind, "--journal", str(path))
                self.assertEqual((code, out), (0, expected))
        code, out, _ = self.run_main("--report", "fleet", "--journal", str(path), "--report-format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["accounts"], [])

    def test_missing_journal_is_a_clear_error(self):
        missing = self.dir / "absent.jsonl"
        code, out, err = self.run_main("--report", "progression", "--journal", str(missing))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn(f"journal not found: {missing}", err)

    def test_malformed_journal_and_bad_registry_fail_closed(self):
        bad = self.dir / "bad.jsonl"
        bad.write_text("not json\n", encoding="utf-8")
        code, _, err = self.run_main("--report", "progression", "--journal", str(bad))
        self.assertEqual(code, 2)
        self.assertIn("malformed journal line", err)

        good = self.journal("good.jsonl", [event("alpha")])
        registry = self.dir / "registry.json"
        registry.write_text(json.dumps({"version": 1, "accounts": [], "password": "x"}), encoding="utf-8")
        code, _, err = self.run_main("--report", "fleet", "--journal", str(good), "--registry", str(registry))
        self.assertEqual(code, 2)
        self.assertIn("secret-named key", err)

        code, _, err = self.run_main("--report", "fleet", "--journal", str(good), "--registry",
                                     str(self.dir / "absent.json"))
        self.assertEqual(code, 2)
        self.assertIn("registry not found", err)

    def test_report_needs_a_journal_and_journal_needs_report(self):
        code, _, err = self.run_main("--report", "fleet")
        self.assertEqual(code, 2)
        self.assertIn("--report needs at least one --journal PATH", err)

        path = self.journal("alpha.jsonl", [event("alpha")])
        code, _, err = self.run_main("--journal", str(path))
        self.assertEqual(code, 2)
        self.assertIn("only used with --report", err)

    def test_report_ignores_login_flags_and_never_prompts(self):
        path = self.journal("alpha.jsonl", [event("alpha")])
        code, out, _ = self.run_main("--report", "progression", "--journal", str(path),
                                     "--login-email", SYNTHETIC_EMAIL, "--progression-dry-run")
        self.assertEqual(code, 0)
        self.assertIn("| alpha |", out)


if __name__ == "__main__":
    unittest.main()
