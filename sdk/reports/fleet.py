"""Fleet throughput, failure and isolation report (R5 exit metrics).

``compute_fleet_report`` summarises each account's journal and the fleet as
a whole on the same fields, so a sequential run and a future concurrent run
can be compared directly. Inputs:

- ``journals``: account alias -> that account's journal events;
- ``fleet_run`` (optional): the orchestrator's ``FleetRun``, for the mode and
  each account's status;
- ``audit`` (optional): an ``AuditReport``, for isolation incidents;
- ``manual_minutes`` and ``operator_minutes`` (optional): the operator's own
  figures for the manual routine and for time spent supervising the run.
  Time saved is reported only when both are supplied for an account.

Rates use attempted events as the denominator; with nothing attempted they
are ``None``. Events whose alias differs from the journal owner are left out
of that account's figures (the audit reports them).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Iterable, Mapping, Optional

from sdk.journal import GameplayEvent


@dataclass(frozen=True)
class Throughput:
    attempted: int = 0
    verified: int = 0
    uncertain: int = 0
    contradicted: int = 0
    failed: int = 0
    skipped: int = 0
    success_rate: Optional[float] = None
    uncertain_rate: Optional[float] = None
    failure_rate: Optional[float] = None
    elapsed_hours: Optional[float] = None
    actions_per_hour: Optional[float] = None


@dataclass(frozen=True)
class AccountFleetRow:
    account_alias: str
    status: Optional[str]
    throughput: Throughput
    incidents: int
    manual_minutes: Optional[float]
    operator_minutes: Optional[float]
    time_saved_minutes: Optional[float]


@dataclass(frozen=True)
class FleetReport:
    mode: str
    dry_run: Optional[bool]
    accounts: tuple[AccountFleetRow, ...]
    fleet: Throughput
    incidents: int
    incidents_by_rule: dict = field(default_factory=dict)
    time_saved_minutes: Optional[float] = None


def _rate(count: int, total: int) -> Optional[float]:
    return round(count / total, 4) if total else None


def _throughput(events: Iterable[GameplayEvent]) -> Throughput:
    events = list(events)
    attempted = [e for e in events if e.attempted]
    verified = sum(1 for e in attempted if e.outcome == "verified")
    uncertain = sum(1 for e in attempted if e.outcome == "unknown")
    contradicted = sum(1 for e in attempted if e.outcome == "contradicted")
    failed = sum(1 for e in attempted if e.error_class)
    elapsed = None
    if attempted:
        start = min(datetime.fromisoformat(e.started_at) for e in attempted)
        end = max(datetime.fromisoformat(e.finished_at) for e in attempted)
        hours = (end - start).total_seconds() / 3600
        elapsed = round(hours, 4) if hours > 0 else None
    return Throughput(
        attempted=len(attempted),
        verified=verified,
        uncertain=uncertain,
        contradicted=contradicted,
        failed=failed,
        skipped=sum(1 for e in events if e.outcome == "skipped"),
        success_rate=_rate(verified, len(attempted)),
        uncertain_rate=_rate(uncertain, len(attempted)),
        failure_rate=_rate(failed, len(attempted)),
        elapsed_hours=elapsed,
        actions_per_hour=round(len(attempted) / elapsed, 2) if elapsed else None,
    )


def compute_fleet_report(
    journals: Mapping[str, Iterable[GameplayEvent]],
    *,
    fleet_run=None,
    audit=None,
    manual_minutes: Optional[Mapping[str, float]] = None,
    operator_minutes: Optional[Mapping[str, float]] = None,
) -> FleetReport:
    manual_minutes = manual_minutes or {}
    operator_minutes = operator_minutes or {}
    statuses = {run.alias: run.status for run in fleet_run.accounts} if fleet_run is not None else {}
    incidents_by_account: dict[str, int] = {}
    if audit is not None:
        for incident in audit.incidents:
            incidents_by_account[incident.account_alias] = incidents_by_account.get(incident.account_alias, 0) + 1

    aliases = sorted(set(journals) | set(statuses))
    rows = []
    owned_events: list[GameplayEvent] = []
    saved_total: Optional[float] = None
    for alias in aliases:
        own = [e for e in journals.get(alias, ()) if e.account_alias == alias]
        owned_events.extend(own)
        manual, operator = manual_minutes.get(alias), operator_minutes.get(alias)
        saved = round(manual - operator, 2) if manual is not None and operator is not None else None
        if saved is not None:
            saved_total = round((saved_total or 0) + saved, 2)
        rows.append(AccountFleetRow(
            account_alias=alias,
            status=statuses.get(alias),
            throughput=_throughput(own),
            incidents=incidents_by_account.get(alias, 0),
            manual_minutes=manual,
            operator_minutes=operator,
            time_saved_minutes=saved,
        ))

    return FleetReport(
        mode=fleet_run.mode if fleet_run is not None else "unknown",
        dry_run=fleet_run.dry_run if fleet_run is not None else None,
        accounts=tuple(rows),
        fleet=_throughput(owned_events),
        incidents=len(audit.incidents) if audit is not None else 0,
        incidents_by_rule=audit.by_rule() if audit is not None else {},
        time_saved_minutes=saved_total,
    )


def to_json(report: FleetReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True)


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _cell(value) -> str:
    return "n/a" if value is None else str(value)


def to_markdown(report: FleetReport) -> str:
    lines = [
        f"**Mode:** {report.mode}" + ("" if report.dry_run is None else f" ({'dry run' if report.dry_run else 'execute'})"),
        "",
        "| Account | Status | Attempted | Verified | Success | Uncertain | Failed | Actions/h | Incidents | "
        "Time saved (min) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report.accounts + (AccountFleetRow("**fleet**", None, report.fleet, report.incidents, None, None,
                                                  report.time_saved_minutes),):
        t = row.throughput
        lines.append(
            f"| {row.account_alias} | {_cell(row.status)} | {t.attempted} | {t.verified} | {_pct(t.success_rate)} | "
            f"{_pct(t.uncertain_rate)} | {_pct(t.failure_rate)} | {_cell(t.actions_per_hour)} | {row.incidents} | "
            f"{_cell(row.time_saved_minutes)} |"
        )
    if report.incidents_by_rule:
        lines += ["", "**Isolation incidents:** " + ", ".join(
            f"{rule} {count}" for rule, count in sorted(report.incidents_by_rule.items()))]
    return "\n".join(lines) + "\n"
