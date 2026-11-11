"""Tests for sdk.reports.fleet: fleet throughput, failure and isolation metrics. Synthetic, offline."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from sdk.fleet import registry_from_dict
from sdk.fleet.audit import audit_fleet
from sdk.fleet.orchestrator import AccountRun, FleetRun
from sdk.journal import GameplayEvent
from sdk.reports.fleet import compute_fleet_report, to_json, to_markdown

T0 = datetime(2027, 4, 3, 8, 0, tzinfo=timezone.utc)


def event(alias, minutes, outcome="verified", attempted=True, error_class=None):
    start = T0 + timedelta(minutes=minutes)
    return GameplayEvent.create(account_alias=alias, action_id="collect_all_resources", started_at=start,
                                finished_at=start + timedelta(minutes=1), outcome=outcome if attempted else "skipped",
                                reason="synthetic", attempted=attempted, error_class=error_class)


JOURNALS = {
    "alpha": [event("alpha", 0), event("alpha", 29), event("alpha", 59, "unknown", error_class="TimeoutError"),
              event("alpha", 70, attempted=False)],
    "bravo": [event("bravo", 0), event("bravo", 119, "contradicted")],
}


class FleetReportTests(unittest.TestCase):
    def test_per_account_and_fleet_throughput(self):
        report = compute_fleet_report(JOURNALS)
        alpha, bravo = report.accounts
        self.assertEqual((alpha.throughput.attempted, alpha.throughput.verified, alpha.throughput.uncertain,
                          alpha.throughput.failed, alpha.throughput.skipped), (3, 2, 1, 1, 1))
        self.assertEqual(alpha.throughput.success_rate, round(2 / 3, 4))
        self.assertEqual(alpha.throughput.elapsed_hours, 1.0)
        self.assertEqual(alpha.throughput.actions_per_hour, 3.0)
        self.assertEqual(bravo.throughput.contradicted, 1)
        self.assertEqual((report.fleet.attempted, report.fleet.verified), (5, 3))
        self.assertEqual(report.fleet.elapsed_hours, 2.0)
        self.assertEqual(report.mode, "unknown")
        self.assertIsNone(report.dry_run)

    def test_no_attempts_means_no_rates(self):
        report = compute_fleet_report({"alpha": [event("alpha", 0, attempted=False)]})
        t = report.accounts[0].throughput
        self.assertEqual(t.attempted, 0)
        self.assertIsNone(t.success_rate)
        self.assertIsNone(t.actions_per_hour)

    def test_time_saved_needs_both_operator_figures(self):
        report = compute_fleet_report(JOURNALS, manual_minutes={"alpha": 25, "bravo": 30},
                                      operator_minutes={"alpha": 5})
        alpha, bravo = report.accounts
        self.assertEqual(alpha.time_saved_minutes, 20)
        self.assertIsNone(bravo.time_saved_minutes)
        self.assertEqual(report.time_saved_minutes, 20)
        self.assertIsNone(compute_fleet_report(JOURNALS).time_saved_minutes)

    def test_fleet_run_status_mode_and_audit_incidents(self):
        registry = registry_from_dict({"version": 1, "accounts": [
            {"alias": a, "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": f"{a.upper()}_AUTH"}
            for a in ("alpha", "bravo", "charlie")]})
        journals = dict(JOURNALS, alpha=JOURNALS["alpha"] + [event("bravo", 200)])
        run = FleetRun(mode="sequential", dry_run=False, started_at=T0.isoformat(), finished_at=T0.isoformat(),
                       accounts=(AccountRun("alpha", "completed", None), AccountRun("bravo", "stopped", "x"),
                                 AccountRun("charlie", "disabled", "off")), stop_reason=None)
        report = compute_fleet_report(journals, fleet_run=run, audit=audit_fleet(registry, journals))
        self.assertEqual(report.mode, "sequential")
        self.assertFalse(report.dry_run)
        self.assertEqual([r.status for r in report.accounts], ["completed", "stopped", "disabled"])
        self.assertEqual(report.accounts[0].incidents, 1)
        self.assertEqual(report.accounts[0].throughput.attempted, 3)  # the foreign event is not alpha's work
        self.assertEqual(report.incidents_by_rule, {"foreign-event": 1})
        self.assertEqual(report.accounts[2].throughput.attempted, 0)

    def test_outputs(self):
        report = compute_fleet_report(JOURNALS, manual_minutes={"alpha": 25}, operator_minutes={"alpha": 5})
        payload = json.loads(to_json(report))
        self.assertEqual(payload["fleet"]["attempted"], 5)
        table = to_markdown(report)
        self.assertIn("**Mode:** unknown", table)
        self.assertIn("| alpha | n/a | 3 | 2 | 67% | 33% | 33% | 3.0 | 0 | 20 |", table)
        self.assertIn("| **fleet** |", table)


if __name__ == "__main__":
    unittest.main()
