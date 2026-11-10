"""Tests for sdk.journal. Journals are written to temporary directories only."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.journal import GameplayEvent, GameplayJournal, JournalError, read_events, summarize
from tests.synthetic_fixtures import SYNTHETIC_ACCESS_TOKEN, SYNTHETIC_EMAIL

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def event(outcome="verified", alias="alpha", attempted=True, reason="synthetic", **extra):
    return GameplayEvent.create(
        account_alias=alias,
        action_id="collect_all_resources",
        started_at=T0,
        finished_at=T0 + timedelta(seconds=2),
        outcome=outcome,
        reason=reason,
        attempted=attempted,
        **extra,
    )


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "journal" / "events.jsonl"
        self.journal = GameplayJournal(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_then_read_round_trips(self):
        first = event(cost={"minerals": -50.0}, params={"room_id": "r1"}, expected_effect="minerals increase")
        second = event(outcome="unknown", error_class="TimeoutError")
        self.journal.append(first)
        self.journal.append(second)
        self.assertEqual(self.journal.read(), [first, second])
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 2)

    def test_append_never_rewrites_existing_lines(self):
        self.journal.append(event())
        before = self.path.read_bytes()
        self.journal.append(event(outcome="not-observed"))
        self.assertTrue(self.path.read_bytes().startswith(before))

    def test_sensitive_param_keys_are_redacted(self):
        self.journal.append(event(params={"accessToken": SYNTHETIC_ACCESS_TOKEN, "room_id": "r1"}))
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, text)
        self.assertEqual(json.loads(text)["params"]["room_id"], "r1")

    def test_secret_shaped_param_values_are_redacted_before_writing(self):
        self.journal.append(event(params={"note": f"retry with accessToken={SYNTHETIC_ACCESS_TOKEN}"}))
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, text)
        self.assertIn("***REDACTED***", text)

    def test_secret_shaped_values_outside_params_are_refused(self):
        for bad in (
            event(reason=f"login failed for {SYNTHETIC_EMAIL}"),
            event(error_class=f"accessToken={SYNTHETIC_ACCESS_TOKEN}"),
        ):
            with self.subTest(reason=bad.reason), self.assertRaises(JournalError):
                self.journal.append(bad)
        self.assertFalse(self.path.exists())

    def test_invalid_events_are_refused(self):
        with self.assertRaises(JournalError):
            self.journal.append(event(alias=SYNTHETIC_EMAIL))
        with self.assertRaises(JournalError):
            self.journal.append(event(outcome="succeeded"))
        with self.assertRaises(JournalError):
            self.journal.append(event(outcome="skipped", attempted=True))
        with self.assertRaises(JournalError):
            self.journal.append(event(dry_run=True, attempted=True))
        with self.assertRaises(JournalError):
            GameplayEvent.create(
                account_alias="alpha", action_id="x", started_at=T0, finished_at=T0 - timedelta(seconds=1),
                outcome="verified", reason="", attempted=True,
            )
        with self.assertRaises(JournalError):
            GameplayEvent.create(
                account_alias="alpha", action_id="x", started_at=datetime(2026, 1, 1), finished_at=T0,
                outcome="verified", reason="", attempted=True,
            )

    def test_interrupted_final_line_is_ignored(self):
        self.journal.append(event())
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"account_alias": "alpha", "action')
        self.assertEqual(len(read_events(self.path)), 1)

    def test_corrupt_complete_line_is_an_error(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json\n", encoding="utf-8")
        with self.assertRaisesRegex(JournalError, ":1: malformed"):
            read_events(self.path)

    def test_missing_journal_reads_as_empty(self):
        self.assertEqual(read_events(self.path), [])


class SummaryTests(unittest.TestCase):
    def test_counts_are_per_account(self):
        events = [
            event(),
            event(outcome="not-observed"),
            event(outcome="contradicted"),
            event(outcome="unknown"),
            event(outcome="unknown", error_class="ConnectionError"),
            event(outcome="skipped", attempted=False),
            event(outcome="unknown", attempted=False, dry_run=True),
            event(alias="bravo"),
        ]
        summary = summarize(events)
        self.assertEqual(
            summary["alpha"],
            {"attempted": 5, "verified": 1, "not_observed": 1, "contradicted": 1, "uncertain": 2, "failed": 1,
             "skipped": 1},
        )
        self.assertEqual(summary["bravo"]["verified"], 1)
        self.assertEqual(list(summary), ["alpha", "bravo"])


if __name__ == "__main__":
    unittest.main()
