"""Tests for sdk.reports.alerts: declarative alert rules over journals and the audit. Synthetic, offline."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from sdk.fleet import registry_from_dict
from sdk.fleet.audit import AuditReport, Incident, audit_fleet
from sdk.journal import GameplayEvent
from sdk.reports.alerts import (
    DEFAULT_MIN_ATTEMPTS,
    DEFAULT_RATE_THRESHOLD,
    DEFAULT_RULES,
    Alert,
    AlertEvidence,
    AlertReport,
    AlertRule,
    AlertRuleError,
    evaluate_alerts,
    to_json,
    to_markdown,
)
from sdk.reports.training import (
    DECISION_KIND,
    TRAINING_ACTIONS,
    TRAINING_DECISION_ACTION,
    TrainingObservation,
    build_training_report,
    crew_label,
)
from tests.synthetic_fixtures import SYNTHETIC_EMAIL

NOW = datetime(2027, 4, 3, 12, 0, tzinfo=timezone.utc)
_counter = iter(range(10_000))


def event(alias, hours_ago, outcome="verified", attempted=True, error_class=None, cost=None, attempt_id=None):
    finish = NOW - timedelta(hours=hours_ago)
    return GameplayEvent.create(
        account_alias=alias, action_id="collect_all_resources", started_at=finish - timedelta(minutes=1),
        finished_at=finish, outcome=outcome if attempted else "skipped", reason="synthetic", attempted=attempted,
        error_class=error_class, cost=cost or {}, attempt_id=attempt_id or f"{alias}-{next(_counter):04d}",
    )


def tevent(alias, hours_ago, outcome="verified", action_id="add_training", error_class=None, attempt_id=None):
    """An attempted training action."""
    finish = NOW - timedelta(hours=hours_ago)
    return GameplayEvent.create(
        account_alias=alias, action_id=action_id, started_at=finish - timedelta(minutes=1), finished_at=finish,
        outcome=outcome, reason="synthetic", attempted=True, error_class=error_class,
        attempt_id=attempt_id or f"{alias}-{next(_counter):04d}",
    )


def tdecision(alias, hours_ago, cid, in_room=True, active=False, reason_code="fatigued"):
    """A training_decision record observing ``cid``; idle by default."""
    finish = NOW - timedelta(hours=hours_ago)
    return GameplayEvent.create(
        account_alias=alias, action_id=TRAINING_DECISION_ACTION, started_at=finish - timedelta(minutes=1),
        finished_at=finish, outcome="skipped", reason="synthetic", attempted=False,
        attempt_id=f"{alias}-{next(_counter):04d}",
        evidence={"kind": DECISION_KIND, "decision": "skip", "reason_code": reason_code, "character_id": cid,
                  "in_training_room": in_room, "training_active": active},
    )


def idle_at(alias, cid, *hours_ago, reason_code="fatigued"):
    return [tdecision(alias, hours, cid, reason_code=reason_code) for hours in hours_ago]


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
        self.assertNotIn("over budget", markdown)  # audit details stay out of the markdown cell

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


class TrainingContradictedTests(unittest.TestCase):
    def test_contradicted_training_attempt_fires(self):
        journal = [tevent("alpha", 1), tevent("alpha", 2, "contradicted", attempt_id="tc1"), tevent("alpha", 3)]
        (alert,) = evaluate_alerts({"alpha": journal}, now=NOW, rules=only("training-contradicted")).alerts
        self.assertEqual((alert.rule_id, alert.severity, alert.account_alias),
                         ("training-contradicted", "warning", "alpha"))
        self.assertEqual(alert.message,
                         "1 of 3 attempts in the last 24h were contradicted by observation (add_training)")
        self.assertEqual(alert.evidence.counts, {"attempted": 3, "contradicted": 1})
        self.assertEqual(alert.evidence.attempt_ids, ("tc1",))

    def test_other_actions_are_left_to_the_generic_rule(self):
        journal = [event("alpha", 1, "contradicted"), event("alpha", 2)]
        report = evaluate_alerts({"alpha": journal}, now=NOW,
                                 rules=only("contradicted-action") + only("training-contradicted"))
        self.assertEqual(rules_fired(report), [("contradicted-action", "alpha")])

    def test_only_training_attempts_are_counted(self):
        journal = [tevent("alpha", 1, "contradicted", action_id="use_consumable")]
        journal += [event("alpha", 2 + i) for i in range(5)]
        (alert,) = evaluate_alerts({"alpha": journal}, now=NOW, rules=only("training-contradicted")).alerts
        self.assertEqual(alert.evidence.counts, {"attempted": 1, "contradicted": 1})
        self.assertEqual(alert.message,
                         "1 of 1 attempt in the last 24h were contradicted by observation (use_consumable)")

    def test_no_contradiction_does_not_fire(self):
        journal = [tevent("alpha", 1), tevent("alpha", 2, "unknown"), tevent("alpha", 3, "not-observed")]
        self.assertEqual(evaluate_alerts({"alpha": journal}, now=NOW, rules=only("training-contradicted")).alerts,
                         ())

    def test_window_boundary(self):
        edge = [tevent("alpha", 24, "contradicted", action_id="finish_training")]
        self.assertEqual(evaluate_alerts({"alpha": edge}, now=NOW, rules=only("training-contradicted")).alerts, ())
        inside = [tevent("alpha", 24 - 1 / 3600, "contradicted", action_id="finish_training")]
        report = evaluate_alerts({"alpha": inside}, now=NOW, rules=only("training-contradicted"))
        self.assertEqual(rules_fired(report), [("training-contradicted", "alpha")])

    def test_attempt_finishing_exactly_at_now_counts(self):
        # The window is (now - w, now]: an attempt finished at ``now`` is inside it, as in the training report.
        journal = [tevent("alpha", 0, "contradicted", attempt_id="tc-now")]
        (alert,) = evaluate_alerts({"alpha": journal}, now=NOW, rules=only("training-contradicted")).alerts
        self.assertEqual(alert.evidence.counts, {"attempted": 1, "contradicted": 1})
        self.assertEqual(alert.evidence.attempt_ids, ("tc-now",))
        self.assertEqual(build_training_report(journal, now=NOW).fleet.outcomes.contradicted, 1)
        late = [tevent("alpha", -1 / 3600, "contradicted")]
        self.assertEqual(evaluate_alerts({"alpha": late}, now=NOW, rules=only("training-contradicted")).alerts, ())

    def test_duplicate_first_seen_on_a_non_training_action_drops_the_training_copy(self):
        # Dedupe runs over every action before the action filter, as the training report does: the first copy of
        # "dup" is not a training action, so its later training copy is dropped rather than counted.
        journal = [event("alpha", 2, attempt_id="dup"), tevent("alpha", 1, "contradicted", attempt_id="dup"),
                   tevent("alpha", 3, "contradicted", attempt_id="tc1")]
        (alert,) = evaluate_alerts({"alpha": journal}, now=NOW, rules=only("training-contradicted")).alerts
        self.assertEqual(alert.evidence.counts, {"attempted": 1, "contradicted": 1})
        self.assertEqual(alert.evidence.attempt_ids, ("tc1",))
        report = build_training_report(journal, now=NOW)
        self.assertEqual(report.duplicates_dropped, 1)
        self.assertEqual((report.fleet.outcomes.attempted, report.fleet.outcomes.contradicted), (1, 1))

    def test_duplicate_attempt_ids_count_once_like_the_training_report(self):
        # Regression: a training attempt written twice (the same journal read twice) was counted twice.
        journal = [tevent("alpha", 1, "contradicted", attempt_id="tc1"), tevent("alpha", 2)]
        journal.append(journal[0])
        (alert,) = evaluate_alerts({"alpha": journal}, now=NOW, rules=only("training-contradicted")).alerts
        self.assertEqual(alert.evidence.counts, {"attempted": 2, "contradicted": 1})
        self.assertEqual(alert.evidence.attempt_ids, ("tc1",))
        self.assertEqual(alert.message,
                         "1 of 2 attempts in the last 24h were contradicted by observation (add_training)")
        totals = build_training_report(journal, now=NOW).fleet
        self.assertEqual((totals.outcomes.contradicted, totals.outcomes.attempted), (1, 2))

    def test_generic_rules_keep_counting_duplicates(self):
        journal = [event("alpha", 1, "contradicted", attempt_id="gc1"), event("alpha", 2)]
        journal.append(journal[0])
        (alert,) = evaluate_alerts({"alpha": journal}, now=NOW, rules=only("contradicted-action")).alerts
        self.assertEqual(alert.evidence.counts, {"attempted": 3, "contradicted": 2})
        self.assertEqual(alert.evidence.attempt_ids, ("gc1", "gc1"))

    def test_generic_rule_still_fires_on_training(self):
        journal = [tevent("alpha", 1, "contradicted")]
        report = evaluate_alerts({"alpha": journal}, now=NOW,
                                 rules=only("contradicted-action") + only("training-contradicted"))
        self.assertEqual(rules_fired(report), [("contradicted-action", "alpha"), ("training-contradicted", "alpha")])


def failing(count, total, alias="alpha"):
    """``count`` unknown-outcome training attempts and ``total - count`` verified ones."""
    return ([tevent(alias, 1 + i * 0.1, "unknown", attempt_id=f"u{i}") for i in range(count)]
            + [tevent(alias, 5 + i * 0.1) for i in range(total - count)])


class TrainingFailedRateTests(unittest.TestCase):
    def fired(self, journal):
        return evaluate_alerts({"alpha": journal}, now=NOW, rules=only("training-failed-rate-high")).alerts

    def test_rate_at_threshold_does_not_fire(self):
        self.assertEqual(self.fired(failing(1, 5)), ())

    def test_rate_above_threshold_fires(self):
        (alert,) = self.fired(failing(2, 9))
        self.assertEqual((alert.rule_id, alert.severity), ("training-failed-rate-high", "warning"))
        self.assertEqual(alert.evidence.rate, 0.2222)
        self.assertEqual(alert.evidence.threshold, 0.2)
        self.assertEqual(alert.evidence.counts, {"attempted": 9, "failed_or_unknown": 2})
        self.assertEqual(alert.evidence.attempt_ids, ("u1", "u0"))
        self.assertEqual(alert.message, "failed-or-unknown rate 22% (2/9 attempts) in the last 24h is above 20%")

    def test_rate_below_threshold_does_not_fire(self):
        self.assertEqual(self.fired(failing(1, 6)), ())

    def test_sample_size_guard(self):
        self.assertEqual(self.fired(failing(4, 4)), ())
        self.assertEqual(len(self.fired(failing(2, 5))), 1)

    def test_error_and_unknown_on_one_attempt_count_once(self):
        journal = [tevent("alpha", 1, "unknown", error_class="TimeoutError")]
        journal += [tevent("alpha", 2 + i) for i in range(4)]
        self.assertEqual(self.fired(journal), ())

    def test_other_actions_are_not_in_the_denominator(self):
        journal = failing(2, 5) + [event("alpha", 3 + i * 0.1) for i in range(10)]
        (alert,) = self.fired(journal)
        self.assertEqual(alert.evidence.counts, {"attempted": 5, "failed_or_unknown": 2})

    def test_error_class_counts_and_not_observed_does_not(self):
        errors = [tevent("alpha", 1 + i, error_class="HTTPError", action_id="use_consumable") for i in range(2)]
        (alert,) = self.fired(errors + [tevent("alpha", 4 + i) for i in range(3)])
        self.assertEqual(alert.evidence.counts, {"attempted": 5, "failed_or_unknown": 2})
        not_observed = [tevent("alpha", 1 + i, "not-observed") for i in range(4)]
        self.assertEqual(self.fired(not_observed + [tevent("alpha", 6, error_class="HTTPError")]), ())

    def test_duplicate_attempt_ids_count_once_like_the_training_report(self):
        # Regression: 1 failed attempt written twice + 4 verified read as 2/6 (33%) and fired; the report says 1/5.
        failed = tevent("alpha", 1, error_class="HTTPError", attempt_id="dup-f")
        journal = [failed] + [tevent("alpha", 2 + i) for i in range(4)] + [failed]
        self.assertEqual(self.fired(journal), ())
        report = build_training_report(journal, now=NOW)
        self.assertEqual((report.fleet.outcomes.failed_or_unknown, report.fleet.outcomes.attempted,
                          report.duplicates_dropped), (1, 5, 1))
        (alert,) = self.fired(journal + [tevent("alpha", 3.5, "unknown", attempt_id="u9")])
        self.assertEqual(alert.evidence.counts, {"attempted": 6, "failed_or_unknown": 2})
        self.assertEqual(alert.evidence.attempt_ids, ("u9", "dup-f"))

    def test_first_copy_of_a_duplicate_wins(self):
        first = tevent("alpha", 2, attempt_id="dup-1")
        second = tevent("alpha", 1, "unknown", attempt_id="dup-1")
        journal = [first, second] + [tevent("alpha", 3 + i, "unknown", attempt_id=f"u{i}") for i in range(4)]
        (alert,) = self.fired(journal)
        self.assertEqual(alert.evidence.counts, {"attempted": 5, "failed_or_unknown": 4})
        self.assertNotIn("dup-1", alert.evidence.attempt_ids)
        late = tevent("alpha", -1, "unknown", attempt_id="dup-2")
        kept = tevent("alpha", 1, attempt_id="dup-2")
        journal = [late, kept] + [tevent("alpha", 3 + i, "unknown", attempt_id=f"v{i}") for i in range(4)]
        (alert,) = self.fired(journal)
        self.assertEqual(alert.evidence.counts, {"attempted": 5, "failed_or_unknown": 4})
        totals = build_training_report(journal, now=NOW).fleet.outcomes
        self.assertEqual((totals.failed_or_unknown, totals.attempted), (4, 5))

    def test_any_other_outcome_counts_as_unknown_like_the_training_report(self):
        odd = [tevent("alpha", 1 + i * 0.1, "mystery", attempt_id=f"m{i}") for i in range(2)]
        journal = odd + [tevent("alpha", 4 + i, "contradicted" if i == 0 else "verified") for i in range(3)]
        (alert,) = self.fired(journal)
        self.assertEqual(alert.evidence.counts, {"attempted": 5, "failed_or_unknown": 2})
        self.assertEqual(alert.evidence.attempt_ids, ("m1", "m0"))
        totals = build_training_report(journal, now=NOW).fleet
        self.assertEqual((totals.outcomes.failed_or_unknown, totals.outcomes.attempted), (2, 5))


class TrainingStalledTests(unittest.TestCase):
    def stalled(self, journal, alias="alpha", rules=None, **kwargs):
        return evaluate_alerts({alias: journal}, now=NOW, rules=rules or only("training-stalled"), **kwargs).alerts

    def test_streak_at_threshold_does_not_fire(self):
        self.assertEqual(self.stalled(idle_at("alpha", "c1", 13, 1)), ())

    def test_streak_above_threshold_fires(self):
        start = 13 + 1 / 3600
        (alert,) = self.stalled(idle_at("alpha", "c1", start, 1))
        self.assertEqual((alert.rule_id, alert.severity, alert.account_alias), ("training-stalled", "info", "alpha"))
        self.assertEqual(alert.message, "1 crew idle in a training room for more than 12h (longest 12.0h)")
        self.assertEqual(alert.evidence.counts, {"idle_crew": 1, "capped_in_room": 0})
        self.assertEqual(alert.evidence.threshold, 12)
        self.assertEqual(alert.evidence.details,
                         (f"crew-dd4eecb7 idle 12.00h since {(NOW - timedelta(hours=start)).isoformat()} (fatigued)",))
        self.assertEqual((alert.evidence.window_start, alert.evidence.window_end),
                         ((NOW - timedelta(hours=24)).isoformat(), NOW.isoformat()))
        self.assertEqual(alert.evidence.attempt_ids, ())

    def test_streak_below_threshold_does_not_fire(self):
        self.assertEqual(self.stalled(idle_at("alpha", "c1", 12.5, 1)), ())

    def test_last_observation_must_be_inside_the_window(self):
        self.assertEqual(self.stalled(idle_at("alpha", "c1", 40, 24)), ())
        self.assertEqual(len(self.stalled(idle_at("alpha", "c1", 40, 24 - 1 / 3600))), 1)

    def test_gap_cap_breaks_the_streak(self):
        self.assertEqual(self.stalled(idle_at("alpha", "c1", 28, 1.5)), ())
        (alert,) = self.stalled(idle_at("alpha", "c1", 27.5, 1.5))
        self.assertEqual(alert.message, "1 crew idle in a training room for more than 12h (longest 26.0h)")

    def test_training_or_unknown_state_ends_the_streak(self):
        trained = idle_at("alpha", "c1", 20, 14) + [tdecision("alpha", 1, "c1", active=True)]
        self.assertEqual(self.stalled(trained), ())
        unknown = idle_at("alpha", "c1", 20, 1) + [tdecision("alpha", 10, "c1", active=None)]
        self.assertEqual(self.stalled(unknown), ())
        left = idle_at("alpha", "c1", 20, 1) + [tdecision("alpha", 10, "c1", in_room=False)]
        self.assertEqual(self.stalled(left), ())

    def test_future_observations_are_ignored(self):
        journal = idle_at("alpha", "c1", 20, 1) + [tdecision("alpha", -1, "c1", active=True)]
        (alert,) = self.stalled(journal)
        self.assertEqual(alert.message, "1 crew idle in a training room for more than 12h (longest 19.0h)")

    def test_one_alert_per_account_with_sorted_details(self):
        journal = idle_at("alpha", "c2", 15, 1) + idle_at("alpha", "c1", 20.25, 1) + idle_at("alpha", "c0", 5, 1)
        (alert,) = self.stalled(journal)
        self.assertEqual(alert.message, "2 crew idle in a training room for more than 12h (longest 19.2h)")
        self.assertEqual(alert.evidence.counts, {"idle_crew": 2, "capped_in_room": 0})
        self.assertEqual(alert.evidence.details, (
            f"crew-dd4eecb7 idle 19.25h since {(NOW - timedelta(hours=20.25)).isoformat()} (fatigued)",
            f"crew-f62a8364 idle 14.00h since {(NOW - timedelta(hours=15)).isoformat()} (fatigued)",
        ))

    def test_details_use_the_same_crew_label_as_the_training_report(self):
        journal = idle_at("alpha", "c1", 20, 1)
        (alert,) = self.stalled(journal)
        (crew,) = build_training_report(journal, now=NOW).accounts[0].crew
        self.assertEqual(alert.evidence.details[0].split(" ")[0], crew.crew)
        self.assertEqual(crew.crew, crew_label("alpha", "c1"))
        self.assertNotIn("c1 ", alert.evidence.details[0])

    def test_capped_crew_over_the_threshold_do_not_alert(self):
        for code in ("tp_cap_reached", "complete"):
            with self.subTest(code=code):
                self.assertEqual(self.stalled(idle_at("alpha", "c1", 20, 1, reason_code=code)), ())

    def test_capped_crew_are_counted_beside_stalled_crew(self):
        journal = (idle_at("alpha", "c1", 20, 1, reason_code="tp_cap_reached")
                   + idle_at("alpha", "c2", 15, 1)
                   + idle_at("alpha", "c3", 25, 2, reason_code="complete")
                   + idle_at("alpha", "c4", 5, 1, reason_code="tp_cap_reached"))
        (alert,) = self.stalled(journal)
        self.assertEqual(alert.message, "1 crew idle in a training room for more than 12h (longest 14.0h)")
        self.assertEqual(alert.evidence.counts, {"idle_crew": 1, "capped_in_room": 2})
        self.assertEqual(alert.evidence.details,
                         (f"crew-f62a8364 idle 14.00h since {(NOW - timedelta(hours=15)).isoformat()} (fatigued)",))

    def test_only_the_last_reason_code_decides_whether_crew_are_capped(self):
        no_longer = idle_at("alpha", "c1", 20, reason_code="tp_cap_reached") + idle_at("alpha", "c1", 1)
        (alert,) = self.stalled(no_longer)
        self.assertEqual(alert.evidence.counts, {"idle_crew": 1, "capped_in_room": 0})
        now_capped = idle_at("alpha", "c1", 20) + idle_at("alpha", "c1", 1, reason_code="complete")
        self.assertEqual(self.stalled(now_capped), ())

    def test_details_without_a_reason_code_have_no_suffix(self):
        extra = [TrainingObservation("delta", "k1", NOW - timedelta(hours=20), True, False),
                 TrainingObservation("delta", "k1", NOW - timedelta(hours=1), True, False)]
        (alert,) = evaluate_alerts({}, now=NOW, rules=only("training-stalled"), training_observations=extra).alerts
        self.assertEqual(alert.evidence.details,
                         (f"crew-88ccbd9b idle 19.00h since {(NOW - timedelta(hours=20)).isoformat()}",))
        unknown_code = idle_at("alpha", "c1", 20, 1, reason_code="bogus")
        (alert,) = self.stalled(unknown_code)
        self.assertEqual(alert.evidence.details,
                         (f"crew-dd4eecb7 idle 19.00h since {(NOW - timedelta(hours=20)).isoformat()}",))

    def test_foreign_events_are_ignored(self):
        self.assertEqual(self.stalled(idle_at("bravo", "c1", 20, 1)), ())

    def test_observations_passed_in_fire_without_a_journal(self):
        extra = [TrainingObservation("delta", "k1", NOW - timedelta(hours=20), True, False),
                 TrainingObservation("delta", "k1", NOW - timedelta(hours=1), True, False)]
        report = evaluate_alerts({}, now=NOW, rules=only("training-stalled"), training_observations=extra)
        self.assertEqual(rules_fired(report), [("training-stalled", "delta")])
        self.assertEqual(report.rules_evaluated, ("training-stalled",))
        self.assertEqual((report.accounts_checked, report.events_checked), (1, 0))

    def test_accounts_checked_counts_journal_and_observation_aliases_once(self):
        # Regression: an account seen only through training_observations was not counted as checked.
        extra = [TrainingObservation("alpha", "c1", NOW - timedelta(hours=1), True, False),
                 TrainingObservation("delta", "k1", NOW - timedelta(hours=1), True, False),
                 TrainingObservation("delta", "k2", NOW - timedelta(hours=2), True, False)]
        journals = {"alpha": [event("alpha", 1)], "bravo": [event("bravo", 1), event("bravo", 2)]}
        report = evaluate_alerts(journals, now=NOW, training_observations=extra)
        self.assertEqual((report.accounts_checked, report.events_checked), (3, 3))
        self.assertIn("(3 accounts, 3 events checked)", to_markdown(report))
        self.assertEqual(evaluate_alerts(journals, now=NOW).accounts_checked, 2)

    def test_observations_passed_in_join_the_owner_journal(self):
        extra = [TrainingObservation("alpha", "c1", NOW - timedelta(hours=1), True, False),
                 TrainingObservation("bravo", "c1", NOW - timedelta(hours=2), True, False)]
        (alert,) = self.stalled(idle_at("alpha", "c1", 20), training_observations=extra)
        self.assertEqual(alert.account_alias, "alpha")
        self.assertEqual(alert.message, "1 crew idle in a training room for more than 12h (longest 19.0h)")
        self.assertEqual(self.stalled(idle_at("alpha", "c1", 20), training_observations=extra[1:]), ())

    def test_markdown_lists_stalled_crew(self):
        # Regression: the markdown evidence cell dropped evidence.details, so fleet run output named no crew.
        report = evaluate_alerts({"alpha": idle_at("alpha", "c1", 20, 1)}, now=NOW, rules=only("training-stalled"))
        markdown = to_markdown(report)
        detail = f"crew-dd4eecb7 idle 19.00h since {(NOW - timedelta(hours=20)).isoformat()} (fatigued)"
        self.assertIn(f"idle_crew 1; window {(NOW - timedelta(hours=24)).isoformat()} to {NOW.isoformat()}; "
                      f"stalled: {detail} |\n", markdown)

    def test_markdown_stalled_details_are_escaped_redacted_and_truncated(self):
        details = ("crew-1 a|b", f"crew-2 {SYNTHETIC_EMAIL}") + tuple(f"crew-{i}" for i in range(3, 9))
        evidence = AlertEvidence(counts={"idle_crew": 8, "capped_in_room": 0}, threshold=12, details=details)
        report = AlertReport(NOW.isoformat(), 1, 0, ("training-stalled",),
                             alerts=(Alert("training-stalled", "info", "alpha", "8 crew idle", evidence),))
        markdown = to_markdown(report)
        self.assertIn("stalled: crew-1 a\\|b, crew-2 ***REDACTED_EMAIL***, crew-3, crew-4, crew-5 (+3 more) |", markdown)
        self.assertNotIn(SYNTHETIC_EMAIL, markdown)
        self.assertNotIn("crew-6", markdown)

    def test_markdown_shows_exactly_the_limit_without_a_more_suffix(self):
        def cell(details, attempt_ids):
            evidence = AlertEvidence(counts={"idle_crew": len(details), "capped_in_room": 0}, threshold=12,
                                     attempt_ids=attempt_ids, details=details)
            report = AlertReport(NOW.isoformat(), 1, 0, ("training-stalled",),
                                 alerts=(Alert("training-stalled", "info", "alpha", "crew idle", evidence),))
            return to_markdown(report)

        five, six = tuple(f"crew-{i}" for i in range(1, 6)), tuple(f"crew-{i}" for i in range(1, 7))
        ids5, ids6 = tuple(f"a{i}" for i in range(1, 6)), tuple(f"a{i}" for i in range(1, 7))
        markdown = cell(five, ids5)
        self.assertIn("attempts `a1`, `a2`, `a3`, `a4`, `a5`; stalled: crew-1, crew-2, crew-3, crew-4, crew-5 |",
                      markdown)
        self.assertNotIn("more)", markdown)
        markdown = cell(six, ids6)
        self.assertIn("attempts `a1`, `a2`, `a3`, `a4`, `a5` (+1 more); "
                      "stalled: crew-1, crew-2, crew-3, crew-4, crew-5 (+1 more) |", markdown)
        self.assertNotIn("crew-6", markdown)
        self.assertNotIn("`a6`", markdown)

    def test_custom_threshold(self):
        rule = AlertRule("stall-6h", "training-stalled", "warning", threshold=6)
        (alert,) = self.stalled(idle_at("alpha", "c1", 8, 1), rules=[rule])
        self.assertEqual((alert.rule_id, alert.severity), ("stall-6h", "warning"))
        self.assertEqual(alert.message, "1 crew idle in a training room for more than 6h (longest 7.0h)")
        self.assertEqual(self.stalled(idle_at("alpha", "c1", 8, 1)), ())

    def test_custom_window(self):
        rule = AlertRule("stall-48h", "training-stalled", "info", threshold=12, window_hours=48)
        self.assertEqual(len(self.stalled(idle_at("alpha", "c1", 50, 30), rules=[rule])), 1)
        self.assertEqual(self.stalled(idle_at("alpha", "c1", 50, 30)), ())


class TrainingRuleDefinitionTests(unittest.TestCase):
    def test_rule_validation(self):
        with self.assertRaises(AlertRuleError):
            AlertRule("r", "audit-incident", "critical", actions=("add_training",))
        with self.assertRaises(AlertRuleError):
            AlertRule("r", "training-stalled", "info", actions=("add_training",))
        with self.assertRaises(AlertRuleError):
            AlertRule("r", "failed-or-unknown-rate", "warning", threshold=1.0)
        with self.assertRaises(AlertRuleError):
            AlertRule("r", "failed-or-unknown-rate", "warning", threshold=0.2, min_attempts=0)
        with self.assertRaises(AlertRuleError):
            AlertRule("r", "training-stalled", "info", threshold=-1)
        for kind in ("failure-rate", "uncertain-rate", "failed-or-unknown-rate", "contradicted", "no-verified"):
            with self.subTest(kind=kind):
                self.assertEqual(AlertRule("r", kind, "info", actions=("x",)).actions, ("x",))
        self.assertEqual(AlertRule("r", "contradicted", "info").actions, ())

    def test_actions_must_be_a_collection_of_action_ids(self):
        # Regression: a bare string made `action_id in actions` a substring match ("training" matched add_training).
        for actions in ("add_training", ("add_training", ""), ("add_training", 3), (None,), ("",)):
            with self.subTest(actions=actions):
                with self.assertRaises(AlertRuleError):
                    AlertRule("r", "contradicted", "warning", actions=actions)
        self.assertEqual(AlertRule("r", "contradicted", "warning", actions=["add_training"]).actions,
                         ["add_training"])

    def test_default_training_rules(self):
        found = {rule.rule_id: (rule.kind, rule.severity, rule.threshold, rule.min_attempts, rule.window_hours,
                                rule.actions) for rule in DEFAULT_RULES if rule.rule_id.startswith("training-")}
        self.assertEqual(found, {
            "training-contradicted": ("contradicted", "warning", 0, 1, 24.0, TRAINING_ACTIONS),
            "training-failed-rate-high": ("failed-or-unknown-rate", "warning", DEFAULT_RATE_THRESHOLD,
                                          DEFAULT_MIN_ATTEMPTS, 24.0, TRAINING_ACTIONS),
            "training-stalled": ("training-stalled", "info", 12, 1, 24.0, ()),
        })
        self.assertEqual(TRAINING_ACTIONS, ("add_training", "finish_training", "use_consumable"))
        self.assertTrue(all(rule.description for rule in DEFAULT_RULES))
        self.assertEqual([rule.actions for rule in DEFAULT_RULES if not rule.rule_id.startswith("training-")],
                         [()] * 6)

    def test_training_rules_are_evaluated_from_journals_alone(self):
        report = evaluate_alerts({"alpha": [event("alpha", 1)]}, now=NOW)
        self.assertEqual(report.rules_not_evaluated, ("budget-exceeded", "isolation-incident"))
        self.assertEqual(report.rules_evaluated, (
            "contradicted-action", "failure-rate-high", "no-verified-action", "training-contradicted",
            "training-failed-rate-high", "training-stalled", "uncertain-rate-high"))
        self.assertEqual(report.alerts, ())

    def test_default_rules_on_a_training_journal(self):
        journal = [tevent("alpha", 2, "contradicted"), tevent("alpha", 3)] + idle_at("alpha", "c1", 20, 1)
        report = evaluate_alerts({"alpha": journal}, now=NOW)
        self.assertEqual([(a.rule_id, a.severity, a.account_alias) for a in report.alerts], [
            ("contradicted-action", "warning", "alpha"), ("training-contradicted", "warning", "alpha"),
            ("training-stalled", "info", "alpha"),
        ])
        self.assertEqual(report.by_severity(), {"critical": 0, "warning": 2, "info": 1})


if __name__ == "__main__":
    unittest.main()
