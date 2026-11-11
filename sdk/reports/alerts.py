"""Declarative alert rules over gameplay journals and the isolation audit (AUTO-012).

``evaluate_alerts(journals, now=..., audit=None, rules=DEFAULT_RULES)`` checks
each account's journal events (``sdk.journal.GameplayEvent``) and, optionally,
an ``sdk.fleet.audit.AuditReport`` against a set of ``AlertRule`` definitions
and returns an ``AlertReport`` of ``Alert`` records.

Rule kinds:

- ``failure-rate``: attempts with an error class / attempts in the window is
  above ``threshold``.
- ``uncertain-rate``: attempts with an ``unknown`` outcome / attempts in the
  window is above ``threshold``.
- ``contradicted``: more than ``threshold`` attempts in the window were
  contradicted by observation (default: any).
- ``no-verified``: the account attempted at least ``min_attempts`` actions in
  the window and none of them was verified.
- ``audit-incident``: more than ``threshold`` audit incidents for the account
  under one audit rule (default: any). One alert per account and audit rule.

Rate rules never fire below ``min_attempts`` attempts in the window (the
sample-size guard); ``min_attempts`` must be at least 1 for them. Windows are
``(now - window_hours, now]`` on each event's ``finished_at``; events that
finished after ``now`` are ignored. Only an account's own events count (an
event whose alias differs from the journal owner is the audit's
``foreign-event``). Audit rules are reported as not evaluated, never as clean,
when no audit is supplied.

This module only decides what to alert on. Sending alerts (e-mail, chat,
pager, webhooks) is deliberately out of scope: callers render the report with
``to_json`` or ``to_markdown`` and deliver it however they choose.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Mapping, Optional

from sdk.journal import GameplayEvent

SEVERITIES = ("critical", "warning", "info")
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITIES)}
RATE_KINDS = frozenset({"failure-rate", "uncertain-rate"})
JOURNAL_KINDS = RATE_KINDS | {"contradicted", "no-verified"}
KINDS = JOURNAL_KINDS | {"audit-incident"}

DEFAULT_WINDOW_HOURS = 24.0
DEFAULT_MIN_ATTEMPTS = 5
DEFAULT_RATE_THRESHOLD = 0.2


class AlertRuleError(ValueError):
    """Raised for an invalid rule definition or rule set."""


@dataclass(frozen=True)
class AlertRule:
    """One declarative alert threshold.

    ``threshold`` is a fraction in ``[0, 1)`` for rate kinds and a count for
    ``contradicted`` and ``audit-incident``; the rule fires when the observed
    value is strictly above it. ``window_hours`` bounds the journal events a
    journal rule looks at. ``audit_rules`` limits an ``audit-incident`` rule to
    those audit rules (empty means every audit rule), minus
    ``exclude_audit_rules``.
    """

    rule_id: str
    kind: str
    severity: str
    threshold: float = 0
    min_attempts: int = 1
    window_hours: float = DEFAULT_WINDOW_HOURS
    audit_rules: tuple[str, ...] = ()
    exclude_audit_rules: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not self.rule_id:
            raise AlertRuleError("rule_id is required")
        if self.kind not in KINDS:
            raise AlertRuleError(f"{self.rule_id}: unknown kind {self.kind!r}")
        if self.severity not in SEVERITIES:
            raise AlertRuleError(f"{self.rule_id}: unknown severity {self.severity!r}")
        if self.kind in RATE_KINDS:
            if not 0 <= self.threshold < 1:
                raise AlertRuleError(f"{self.rule_id}: a rate threshold must be in [0, 1)")
            if self.min_attempts < 1:
                raise AlertRuleError(f"{self.rule_id}: rate rules need min_attempts >= 1 (sample-size guard)")
        elif self.threshold < 0:
            raise AlertRuleError(f"{self.rule_id}: threshold must be non-negative")
        if self.min_attempts < 1:
            raise AlertRuleError(f"{self.rule_id}: min_attempts must be >= 1")
        if self.window_hours <= 0:
            raise AlertRuleError(f"{self.rule_id}: window_hours must be positive")


DEFAULT_RULES: tuple[AlertRule, ...] = (
    AlertRule("failure-rate-high", "failure-rate", "warning", threshold=DEFAULT_RATE_THRESHOLD,
              min_attempts=DEFAULT_MIN_ATTEMPTS,
              description="More than 20% of attempts in the last 24h raised an error (at least 5 attempts)."),
    AlertRule("uncertain-rate-high", "uncertain-rate", "warning", threshold=DEFAULT_RATE_THRESHOLD,
              min_attempts=DEFAULT_MIN_ATTEMPTS,
              description="More than 20% of attempts in the last 24h have an unknown outcome (at least 5 attempts)."),
    AlertRule("contradicted-action", "contradicted", "warning",
              description="Any attempt in the last 24h was contradicted by observation."),
    AlertRule("no-verified-action", "no-verified", "warning",
              description="The account attempted actions in the last 24h and none was verified."),
    AlertRule("isolation-incident", "audit-incident", "critical", exclude_audit_rules=("budget-exceeded",),
              description="Any isolation audit incident, one alert per account and audit rule."),
    AlertRule("budget-exceeded", "audit-incident", "critical", audit_rules=("budget-exceeded",),
              description="The audit found charged spend above an account's budget."),
)


@dataclass(frozen=True)
class AlertEvidence:
    counts: Mapping[str, int] = field(default_factory=dict)
    attempt_ids: tuple[str, ...] = ()
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    rate: Optional[float] = None
    threshold: Optional[float] = None
    audit_rule: Optional[str] = None
    last_verified_at: Optional[str] = None
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class Alert:
    rule_id: str
    severity: str
    account_alias: str
    message: str
    evidence: AlertEvidence

    def sort_key(self) -> tuple:
        return (_SEVERITY_RANK[self.severity], self.account_alias, self.rule_id, self.evidence.audit_rule or "")


@dataclass(frozen=True)
class AlertReport:
    evaluated_at: str
    accounts_checked: int
    events_checked: int
    rules_evaluated: tuple[str, ...]
    rules_not_evaluated: tuple[str, ...] = ()
    alerts: tuple[Alert, ...] = ()

    def by_severity(self) -> dict[str, int]:
        counts = {severity: 0 for severity in SEVERITIES}
        for alert in self.alerts:
            counts[alert.severity] += 1
        return counts


def _ordered(events: Iterable[GameplayEvent]) -> list[GameplayEvent]:
    return sorted(events, key=lambda e: (datetime.fromisoformat(e.finished_at), e.attempt_id))


def _attempts(count: int) -> str:
    return f"{count} attempt" if count == 1 else f"{count} attempts"


def _rate(count: int, total: int) -> Optional[float]:
    return round(count / total, 4) if total else None


def _journal_alerts(rule: AlertRule, alias: str, events: list[GameplayEvent], now: datetime) -> list[Alert]:
    start = now - timedelta(hours=rule.window_hours)
    own = [e for e in events if e.account_alias == alias]
    in_window = [e for e in own if start < datetime.fromisoformat(e.finished_at) <= now]
    attempted = _ordered(e for e in in_window if e.attempted)
    window = {"window_start": start.isoformat(), "window_end": now.isoformat()}
    total = len(attempted)

    if rule.kind in RATE_KINDS:
        if total < rule.min_attempts:
            return []
        if rule.kind == "failure-rate":
            matched, label, noun = [e for e in attempted if e.error_class], "failed", "failure"
        else:
            matched, label, noun = [e for e in attempted if e.outcome == "unknown"], "uncertain", "uncertain"
        rate = _rate(len(matched), total)
        if rate is None or rate <= rule.threshold:
            return []
        message = (f"{noun} rate {rate:.0%} ({len(matched)}/{total} attempts) in the last "
                   f"{rule.window_hours:g}h is above {rule.threshold:.0%}")
        evidence = AlertEvidence(counts={"attempted": total, label: len(matched)},
                                 attempt_ids=tuple(e.attempt_id for e in matched), rate=rate,
                                 threshold=rule.threshold, **window)
        return [Alert(rule.rule_id, rule.severity, alias, message, evidence)]

    if rule.kind == "contradicted":
        matched = [e for e in attempted if e.outcome == "contradicted"]
        if len(matched) <= rule.threshold:
            return []
        message = (f"{len(matched)} of {_attempts(total)} in the last {rule.window_hours:g}h were contradicted "
                   f"by observation ({', '.join(sorted({e.action_id for e in matched}))})")
        evidence = AlertEvidence(counts={"attempted": total, "contradicted": len(matched)},
                                 attempt_ids=tuple(e.attempt_id for e in matched), threshold=rule.threshold,
                                 **window)
        return [Alert(rule.rule_id, rule.severity, alias, message, evidence)]

    # no-verified
    if total < rule.min_attempts or any(e.outcome == "verified" for e in attempted):
        return []
    verified_before = [e.finished_at for e in own
                       if e.attempted and e.outcome == "verified" and datetime.fromisoformat(e.finished_at) <= start]
    last_verified = max(verified_before, key=datetime.fromisoformat) if verified_before else None
    since = f"last verified {last_verified}" if last_verified else "no verified action on record"
    message = f"{_attempts(total)} in the last {rule.window_hours:g}h and none verified ({since})"
    evidence = AlertEvidence(counts={"attempted": total, "verified": 0},
                             attempt_ids=tuple(e.attempt_id for e in attempted), last_verified_at=last_verified,
                             **window)
    return [Alert(rule.rule_id, rule.severity, alias, message, evidence)]


def _audit_alerts(rule: AlertRule, audit) -> list[Alert]:
    groups: dict[tuple[str, str], list] = {}
    for incident in audit.incidents:
        if rule.audit_rules and incident.rule not in rule.audit_rules:
            continue
        if incident.rule in rule.exclude_audit_rules:
            continue
        groups.setdefault((incident.account_alias, incident.rule), []).append(incident)
    alerts = []
    for (alias, audit_rule), incidents in sorted(groups.items()):
        if len(incidents) <= rule.threshold:
            continue
        attempt_ids = tuple(sorted({a for incident in incidents for a in incident.attempt_ids}))
        noun = "incident" if len(incidents) == 1 else "incidents"
        message = f"{len(incidents)} {audit_rule} audit {noun}"
        evidence = AlertEvidence(counts={"incidents": len(incidents)}, attempt_ids=attempt_ids,
                                 threshold=rule.threshold, audit_rule=audit_rule,
                                 details=tuple(sorted(incident.detail for incident in incidents)))
        alerts.append(Alert(rule.rule_id, rule.severity, alias, message, evidence))
    return alerts


def evaluate_alerts(
    journals: Mapping[str, Iterable[GameplayEvent]],
    *,
    now: datetime,
    audit=None,
    rules: Iterable[AlertRule] = DEFAULT_RULES,
) -> AlertReport:
    """Evaluate ``rules`` over ``journals`` (owner alias -> events) and an
    optional ``AuditReport`` at ``now`` (timezone-aware)."""
    if now.tzinfo is None:
        raise AlertRuleError("now must be timezone-aware")
    rules = tuple(rules)
    seen: set[str] = set()
    for rule in rules:
        if rule.rule_id in seen:
            raise AlertRuleError(f"duplicate rule_id {rule.rule_id!r}")
        seen.add(rule.rule_id)

    materialized = {alias: list(events) for alias, events in journals.items()}
    alerts: list[Alert] = []
    evaluated, not_evaluated = [], []
    for rule in rules:
        if rule.kind == "audit-incident":
            if audit is None:
                not_evaluated.append(rule.rule_id)
                continue
            alerts.extend(_audit_alerts(rule, audit))
        else:
            for alias in sorted(materialized):
                alerts.extend(_journal_alerts(rule, alias, materialized[alias], now))
        evaluated.append(rule.rule_id)

    return AlertReport(
        evaluated_at=now.isoformat(),
        accounts_checked=len(materialized),
        events_checked=sum(len(events) for events in materialized.values()),
        rules_evaluated=tuple(sorted(evaluated)),
        rules_not_evaluated=tuple(sorted(not_evaluated)),
        alerts=tuple(sorted(alerts, key=Alert.sort_key)),
    )


def to_json(report: AlertReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True)


MARKDOWN_ATTEMPT_IDS = 5


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _evidence_cell(evidence: AlertEvidence) -> str:
    parts = [", ".join(f"{key} {value}" for key, value in sorted(evidence.counts.items()))]
    if evidence.window_start and evidence.window_end:
        parts.append(f"window {evidence.window_start} to {evidence.window_end}")
    ids = evidence.attempt_ids
    if ids:
        shown = ", ".join(f"`{attempt_id}`" for attempt_id in ids[:MARKDOWN_ATTEMPT_IDS])
        more = len(ids) - MARKDOWN_ATTEMPT_IDS
        parts.append("attempts " + shown + (f" (+{more} more)" if more > 0 else ""))
    return _md("; ".join(part for part in parts if part))


def to_markdown(report: AlertReport) -> str:
    severity = report.by_severity()
    lines = [
        f"**Alerts at {report.evaluated_at}:** " + ", ".join(f"{severity[s]} {s}" for s in SEVERITIES)
        + f" ({report.accounts_checked} accounts, {report.events_checked} events checked)",
        "",
    ]
    if report.alerts:
        lines += ["| Severity | Rule | Account | Message | Evidence |", "| --- | --- | --- | --- | --- |"]
        for alert in report.alerts:
            lines.append(f"| {alert.severity} | {alert.rule_id} | {alert.account_alias} | {_md(alert.message)} | "
                         f"{_evidence_cell(alert.evidence)} |")
    else:
        lines.append("No alerts.")
    if report.rules_not_evaluated:
        lines += ["", "**Not evaluated (no audit supplied):** " + ", ".join(report.rules_not_evaluated)]
    return "\n".join(lines) + "\n"
