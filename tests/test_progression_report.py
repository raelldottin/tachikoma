"""Tests for sdk.reports progression metrics. Synthetic journals in temporary directories."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.journal import GameplayEvent, GameplayJournal
from sdk.reports import compute_progression_metrics, to_json, to_markdown

T0 = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)


def event(minutes, outcome, alias="alpha", attempted=True, error_class=None, cost=None, dry_run=False,
          action_id="collect_all_resources"):
    start = T0 + timedelta(minutes=minutes)
    return GameplayEvent.create(
        account_alias=alias, action_id=action_id, started_at=start, finished_at=start + timedelta(seconds=30),
        outcome=outcome, reason=f"synthetic {outcome}", attempted=attempted, error_class=error_class,
        cost=cost or {}, dry_run=dry_run,
    )


class ProgressionReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = GameplayJournal(Path(self.tmp.name) / "events.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *events):
        for item in events:
            self.journal.append(item)
        return compute_progression_metrics(self.journal.read())

    def test_counts_rates_and_resources_per_day(self):
        metrics = self.write(
            event(0, "verified", cost={"minerals_delta": 300, "gas_delta": 0, "starbux_delta": 0}),
            event(30, "verified", action_id="upgrade_room",
                  cost={"minerals_delta": -200, "gas_delta": None, "starbux_delta": 0}),
            event(60, "not-observed", cost={"minerals_delta": 0, "gas_delta": 0, "starbux_delta": 0}),
            event(90, "unknown", error_class="TimeoutError", action_id="add_research"),
            event(100, "skipped", attempted=False, action_id="add_research"),
            event(110, "skipped", attempted=False, dry_run=True),
        )
        self.assertEqual(len(metrics), 1)
        day = metrics[0]
        self.assertEqual((day.account_alias, day.day), ("alpha", "2026-11-10"))
        self.assertEqual((day.attempted, day.verified, day.not_observed, day.uncertain, day.failed),
                         (4, 2, 1, 1, 1))
        self.assertEqual((day.skipped, day.dry_run), (1, 1))
        self.assertEqual(day.verified_success_rate, 0.5)
        self.assertEqual(day.resource_samples, 3)
        self.assertEqual(day.resources_gained["minerals"], 300)
        self.assertEqual(day.resources_spent["minerals"], 200)
        self.assertEqual(day.elapsed_hours, 1.5083)
        self.assertEqual(day.gained_per_hour["minerals"], round(300 / 1.5083333, 2))
        self.assertEqual([failure.action_id for failure in day.failures], ["add_research"])

    def test_dry_run_only_day_has_no_rates(self):
        day = self.write(event(0, "skipped", attempted=False, dry_run=True))[0]
        self.assertEqual(day.attempted, 0)
        self.assertIsNone(day.verified_success_rate)
        self.assertIsNone(day.elapsed_hours)
        self.assertEqual(day.gained_per_hour["minerals"], None)
        self.assertEqual(day.resources_gained, {})

    def test_no_resource_observations_means_unknown_not_zero(self):
        day = self.write(event(0, "verified"), event(5, "verified"))[0]
        self.assertEqual(day.resource_samples, 0)
        self.assertEqual(day.resources_gained, {})
        self.assertIsNone(day.gained_per_hour["minerals"])

    def test_accounts_and_days_are_separated(self):
        metrics = self.write(
            event(0, "verified"),
            event(0, "verified", alias="bravo"),
            event(24 * 60, "contradicted"),
        )
        self.assertEqual([(day.account_alias, day.day) for day in metrics],
                         [("alpha", "2026-11-10"), ("alpha", "2026-11-11"), ("bravo", "2026-11-10")])
        self.assertEqual(metrics[1].contradicted, 1)
        self.assertEqual(metrics[1].failures[0].outcome, "contradicted")

    def test_empty_journal_reports_empty(self):
        self.assertEqual(compute_progression_metrics([]), [])
        self.assertEqual(to_markdown([]), "_No journal events._\n")
        self.assertEqual(json.loads(to_json([])), [])

    def test_json_and_markdown_outputs(self):
        metrics = self.write(
            event(0, "verified", cost={"minerals_delta": 50, "gas_delta": 10, "starbux_delta": 0}),
            event(60, "unknown", error_class="ConnectionError"),
        )
        payload = json.loads(to_json(metrics))
        self.assertEqual(payload[0]["attempted"], 2)
        self.assertEqual(payload[0]["failures"][0]["error_class"], "ConnectionError")
        table = to_markdown(metrics)
        self.assertIn("| alpha | 2026-11-10 | 2 | 1 | 50% | 1 | 1 | 0 |", table)
        self.assertIn("**Failure ledger**", table)
        self.assertIn("ConnectionError", table)


if __name__ == "__main__":
    unittest.main()
