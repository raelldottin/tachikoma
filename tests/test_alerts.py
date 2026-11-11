"""Tests for sdk.reports.alerts: declarative alert rules over journals and the audit. Synthetic, offline."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from sdk.fleet import registry_from_dict
from sdk.fleet.audit import AuditReport, Incident, audit_fleet
from sdk.journal import GameplayEvent
from sdk.reports.alerts import (
    DEFAULT_RULES,
    AlertRule,
    AlertRuleError,
    evaluate_alerts,
    to_json,
    to_markdown,
)

NOW = datetime(2027, 4, 3, 12, 0, tzinfo=timezone.utc)
_counter = iter(range(10_000))


def event(alias, hours_ago, outcome="verified", attempted=True, error_class=None, cost=None, attempt_id=None):
    finish = NOW - timedelta(hours=hours_ago)
    return GameplayEvent.create(
        account_alias=alias, action_id="collect_all_resources", started_at=finish - timedelta(minutes=1),
        finished_at=finish, outcome=outcome if attempted else "skipped", reason="synthetic", attempted=attempted,
        error_class=error_class, cost=cost or {}, attempt_id=attempt_id or f"{alias}-{next(_counter):04d}",
    )


def only(rule_id):
    return [rule for rule in DEFAULT_RULES if rule.rule_id == rule_id]


def rules_fired(report):
    return [(alert.rule_id, alert.account_alias) for alert in report.alerts]


class RateRuleTests(unittest.TestCase):
    def test_failure_rate_above_threshold_fires_with_evidence(self):
        failed = [event("alpha", 1, "unknown", error_class="TimeoutError", attempt_id=f"f{i}") for i in range(2)]
        journal = [event("alpha", 2 + i) for i in range(3)] + failed
        report = evaluate_alerts({"alpha": journal}, now=NOW, rules=only("failure-rate-high"))
        (alert,) = report.alerts
        self.assertEqual((alert.rule_id, alert.severity, alert.account_alias),
                         ("failure-rate-high", "warning", "alpha"))
        self.assertEqual(alert.evidence.counts, {"attempted": 5, "failed": 2})
        self.assertEqual(alert.evidence.rate, 0.4)
        self.assertEqual(alert.evidence.threshold, 0.2)
        self.assertEqual(set(alert.evidence.attempt_ids), {"f0", "f1"})
        self.assertEqual(alert.evidence.window_end, NOW.isoformat())
        self.assertEqual(alert.evidence.window_start, (NOW - timedelta(hours=24)).isoformat())

    def test_rate_at_threshold_does_not_fire(self):
        journal = [event("alpha", 1, error_class="HTTPError")] + [event("alpha", 2 + i) for i in range(4)]
        self.assertEqual(evaluate_alerts({"alpha": journal}, now=NOW, rules=only("failure-rate-high")).alerts, ())

    def test_uncertain_rate_fires(self):
        journal = [event("alpha", 1 + i, "unknown") for i in range(3)] + [event("alpha", 5), event("alpha", 6)]
        report = evaluate_alerts({"alpha": journal}, now=NOW, rules=only("uncertain-rate-high"))
        (alert,) = report.alerts
        self.assertEqual(alert.evidence.counts, {"attempted": 5, "uncertain": 3})
        self.assertEqual(alert.evidence.rate, 0.6)

    def test_sample_size_guard_suppresses_rate_rules(self):
        # Four attempts, all failed and uncertain: 100% rates, but below the default minimum of five.
        journal = [event("alpha", 1 + i, "unknown", error_class="TimeoutError") for i in range(4)]
        report = evaluate_alerts({"alpha": journal}, now=NOW,
                                 rules=only("failure-rate-high") + only("uncertain-rate-high"))
        self.assertEqual(report.alerts, ())
        # Skipped and out-of-window events do not count towards the minimum either.
        journal += [event("alpha", 2, attempted=False), event("alpha", 30, "unknown", error_class="TimeoutError")]
        self.assertEqual(evaluate_alerts({"alpha": journal}, now=NOW, rules=only("failure-rate-high")).alerts, ())
        journal.append(event("alpha", 3, "unknown", error_class="TimeoutError"))
        self.assertEqual(len(evaluate_alerts({"alpha": journal}, now=NOW, rules=only("failure-rate-high")).alerts), 1)

    def test_rate_rules_cannot_disable_the_guard(self):
        with self.assertRaises(AlertRuleError):
            AlertRule("r", "failure-rate", "warning", threshold=0.1, min_attempts=0)
        with self.assertRaises(AlertRuleError):
            AlertRule("r", "uncertain-rate", "warning", threshold=1.0)
        with self.assertRaises(AlertRuleError):
            AlertRule("r", "failure-rate", "loud")
        with self.assertRaises(AlertRuleError):
            AlertRule("r", "page-everyone", "info")

    def test_window_excludes_old_and_future_events(self):
        journal = [event("alpha", 25 + i, error_class="HTTPError") for i in range(5)]
        journal += [event("alpha", -1 - i, error_class="HTTPError") for i in range(5)]
        self.assertEqual(evaluate_alerts({"alpha": journal}, now=NOW, rules=only("failure-rate-high")).alerts, ())
        custom = AlertRule("failures-48h", "failure-rate", "critical", threshold=0.5, min_attempts=5, window_hours=48)
        (alert,) = evaluate_alerts({"alpha": journal}, now=NOW, rules=[custom]).alerts
        self.assertEqual((alert.severity, alert.evidence.counts["attempted"]), ("critical", 5))

    def test_foreign_events_do_not_count_for_the_owner(self):
        journal = [event("bravo", 1 + i, error_class="HTTPError") for i in range(5)]
        report = evaluate_alerts({"alpha": journal}, now=NOW, rules=only("failure-rate-high"))
        self.assertEqual(report.alerts, ())


class CountRuleTests(unittest.TestCase):
    def test_any_contradicted_action_fires(self):
        journal = [event("alpha", 1), event("alpha", 2, "contradicted", attempt_id="c1")]
        (alert,) = evaluate_alerts({"alpha": journal}, now=NOW, rules=only("contradicted-action")).alerts
        self.assertEqual(alert.evidence.attempt_ids, ("c1",))
        self.assertEqual(alert.evidence.counts, {"attempted": 2, "contradicted": 1})
        self.assertIn("collect_all_resources", alert.message)
        self.assertEqual(evaluate_alerts({"alpha": journal[:1]}, now=NOW, rules=only("contradicted-action")).alerts,
                         ())

    def test_no_verified_action_in_window(self):
        journal = [event("alpha", 30, attempt_id="old-ok"), event("alpha", 2, "not-observed", attempt_id="n1"),
                   event("alpha", 1, "unknown", attempt_id="u1")]
        (alert,) = evaluate_alerts({"alpha": journal}, now=NOW, rules=only("no-verified-action")).alerts
        self.assertEqual(alert.evidence.attempt_ids, ("n1", "u1"))
        self.assertEqual(alert.evidence.last_verified_at, (NOW - timedelta(hours=30)).isoformat())
        self.assertEqual(alert.evidence.counts, {"attempted": 2, "verified": 0})

    def test_no_verified_needs_attempts_in_window(self):
        idle = [event("alpha", 30, "unknown"), event("alpha", 1, attempted=False)]
        self.assertEqual(evaluate_alerts({"alpha": idle}, now=NOW, rules=only("no-verified-action")).alerts, ())
        healthy = [event("alpha", 3, "unknown"), event("alpha", 2)]
        self.assertEqual(evaluate_alerts({"alpha": healthy}, now=NOW, rules=only("no-verified-action")).alerts, ())
        (alert,) = evaluate_alerts({"alpha": [event("alpha", 1, "unknown")]}, now=NOW,
                                   rules=only("no-verified-action")).alerts
        self.assertIsNone(alert.evidence.last_verified_at)
        self.assertIn("no verified action on record", alert.message)


class AuditRuleTests(unittest.TestCase):
    def registry(self, starbux=0):
        return registry_from_dict({"version": 1, "accounts": [
            {"alias": a, "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": f"{a.upper()}_AUTH",
             "budgets": {"starbux": starbux}} for a in ("alpha", "bravo")]})

    def test_isolation_incidents_one_alert_per_account_and_audit_rule(self):
        journals = {"alpha": [event("alpha", 1, attempt_id="a1"), event("bravo", 2, attempt_id="b9"),
                              event("bravo", 3, attempt_id="b8")],
                    "bravo": [event("bravo", 1, attempt_id="a1")]}
        audit = audit_fleet(self.registry(), journals)
        report = evaluate_alerts(journals, now=NOW, audit=audit, rules=only("isolation-incident"))
        found = [(a.account_alias, a.evidence.audit_rule, a.evidence.counts["incidents"]) for a in report.alerts]
        self.assertEqual(found, [("alpha", "attempt-id-reused", 1), ("alpha", "foreign-event", 2)])
        self.assertEqual(report.alerts[1].evidence.attempt_ids, ("b8", "b9"))
        self.assertTrue(all(a.severity == "critical" for a in report.alerts))

    def test_budget_exceeded_is_its_own_rule(self):
        journals = {"alpha": [event("alpha", 1, cost={"starbux_delta": -5}, attempt_id="spend")]}
        audit = audit_fleet(self.registry(), journals)
        report = evaluate_alerts(journals, now=NOW, audit=audit,
                                 rules=only("budget-exceeded") + only("isolation-incident"))
        self.assertEqual(rules_fired(report), [("budget-exceeded", "alpha")])
        self.assertEqual(report.alerts[0].evidence.attempt_ids, ("spend",))
        self.assertIn("budget 0", report.alerts[0].evidence.details[0])

    def test_future_audit_rules_still_count_as_isolation(self):
        audit = AuditReport(1, 1, (Incident("new-rule", "alpha", (), "something new"),))
        (alert,) = evaluate_alerts({}, now=NOW, audit=audit, rules=only("isolation-incident")).alerts
        self.assertEqual(alert.evidence.audit_rule, "new-rule")

    def test_audit_rules_are_not_evaluated_without_an_audit(self):
        report = evaluate_alerts({"alpha": [event("alpha", 1)]}, now=NOW)
        self.assertEqual(report.rules_not_evaluated, ("budget-exceeded", "isolation-incident"))
        self.assertNotIn("budget-exceeded", report.rules_evaluated)
        self.assertIn("Not evaluated (no audit supplied)", to_markdown(report))
        clean = evaluate_alerts({"alpha": [event("alpha", 1)]}, now=NOW, audit=AuditReport(1, 1))
        self.assertEqual((clean.rules_not_evaluated, clean.alerts), ((), ()))


class ReportTests(unittest.TestCase):
    def mixed(self):
        journals = {
            "charlie": [event("charlie", 1, "contradicted")],
            "alpha": [event("alpha", 1, "unknown")],
            "bravo": [event("bravo", 1 + i, "unknown", error_class="TimeoutError") for i in range(5)],
        }
        audit = AuditReport(3, 7, (Incident("budget-exceeded", "charlie", ("x",), "over budget"),
                                   Incident("foreign-event", "alpha", ("y",), "foreign")))
        return journals, audit

    def test_deterministic_ordering_severity_then_account_then_rule(self):
        journals, audit = self.mixed()
        report = evaluate_alerts(journals, now=NOW, audit=audit)
        self.assertEqual(rules_fired(report), [
            ("isolation-incident", "alpha"), ("budget-exceeded", "charlie"),
            ("no-verified-action", "alpha"),
            ("failure-rate-high", "bravo"), ("no-verified-action", "bravo"), ("uncertain-rate-high", "bravo"),
            ("contradicted-action", "charlie"), ("no-verified-action", "charlie"),
        ])
        shuffled = {alias: list(reversed(events)) for alias, events in reversed(list(journals.items()))}
        again = evaluate_alerts(shuffled, now=NOW, audit=audit, rules=tuple(reversed(DEFAULT_RULES)))
        self.assertEqual(to_json(again), to_json(report))
        self.assertEqual(report.by_severity(), {"critical": 2, "warning": 6, "info": 0})
        self.assertEqual((report.accounts_checked, report.events_checked), (3, 7))

    def test_json_and_markdown_rendering(self):
        journals, audit = self.mixed()
        report = evaluate_alerts(journals, now=NOW, audit=audit)
        payload = json.loads(to_json(report))
        self.assertEqual(payload["evaluated_at"], NOW.isoformat())
        first = payload["alerts"][0]
        self.assertEqual(set(first), {"rule_id", "severity", "account_alias", "message", "evidence"})
        self.assertEqual(first["evidence"]["audit_rule"], "foreign-event")
        markdown = to_markdown(report)
        self.assertIn("2 critical, 6 warning, 0 info (3 accounts, 7 events checked)", markdown)
        self.assertIn("| critical | isolation-incident | alpha |", markdown)
        self.assertIn("attempts `y`", markdown)

    def test_markdown_truncates_long_attempt_lists_and_reports_no_alerts(self):
        journal = [event("alpha", 1 + i * 0.1, "unknown", error_class="TimeoutError") for i in range(8)]
        markdown = to_markdown(evaluate_alerts({"alpha": journal}, now=NOW, rules=only("failure-rate-high")))
        self.assertIn("(+3 more)", markdown)
        empty = evaluate_alerts({"alpha": [event("alpha", 1)]}, now=NOW, audit=AuditReport(1, 1))
        self.assertIn("No alerts.", to_markdown(empty))
        self.assertIn("0 critical, 0 warning, 0 info", to_markdown(empty))

    def test_invalid_inputs(self):
        with self.assertRaises(AlertRuleError):
            evaluate_alerts({}, now=NOW.replace(tzinfo=None))
        with self.assertRaises(AlertRuleError):
            evaluate_alerts({}, now=NOW, rules=list(DEFAULT_RULES) + only("budget-exceeded"))


if __name__ == "__main__":
    unittest.main()
