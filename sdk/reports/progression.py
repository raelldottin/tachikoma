"""Progression and reliability metrics from the gameplay journal (R2 exit).

Turns journal events into the roadmap scorecard inputs for each account and
UTC day: verified actions, verified action success rate, uncertain and failed
counts, resources gained and spent per elapsed hour, and a failure ledger.

Every metric carries its sample size and time window. A rate with no sample
is ``None``, never 0. Dry-run and skipped events count as neither attempts
nor outcomes.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from sdk.journal import GameplayEvent

RESOURCES = ("minerals", "gas", "starbux")


@dataclass(frozen=True)
class FailureEntry:
    at: str
    action_id: str
    attempt_id: str
    outcome: str
    error_class: Optional[str]
    reason: str


@dataclass(frozen=True)
class DayMetrics:
    account_alias: str
    day: str
    window_start: Optional[str]
    window_end: Optional[str]
    attempted: int
    verified: int
    not_observed: int
    contradicted: int
    uncertain: int
    failed: int
    skipped: int
    dry_run: int
    verified_success_rate: Optional[float]
    elapsed_hours: Optional[float]
    resources_gained: dict[str, int] = field(default_factory=dict)
    resources_spent: dict[str, int] = field(default_factory=dict)
    gained_per_hour: dict[str, Optional[float]] = field(default_factory=dict)
    resource_samples: int = 0
    failures: tuple[FailureEntry, ...] = ()


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def compute_progression_metrics(events: Iterable[GameplayEvent]) -> list[DayMetrics]:
    """Group events by (account alias, UTC day of finished_at) and compute metrics."""
    groups: dict[tuple[str, str], list[GameplayEvent]] = defaultdict(list)
    for event in events:
        day = datetime.fromisoformat(event.finished_at).date().isoformat()
        groups[(event.account_alias, day)].append(event)

    report = []
    for (alias, day), day_events in sorted(groups.items()):
        attempted = [event for event in day_events if event.attempted]
        counts = defaultdict(int)
        for event in attempted:
            counts[event.outcome] += 1
        failures = tuple(
            FailureEntry(event.finished_at, event.action_id, event.attempt_id, event.outcome,
                         event.error_class, event.reason)
            for event in attempted
            if event.error_class or event.outcome in ("contradicted", "unknown")
        )

        gained = {name: 0 for name in RESOURCES}
        spent = {name: 0 for name in RESOURCES}
        resource_samples = 0
        for event in attempted:
            deltas = [event.cost.get(f"{name}_delta") for name in RESOURCES]
            if all(value is None for value in deltas):
                continue
            resource_samples += 1
            for name, value in zip(RESOURCES, deltas):
                if value is None:
                    continue
                if value > 0:
                    gained[name] += int(value)
                elif value < 0:
                    spent[name] += int(-value)

        if attempted:
            starts = [datetime.fromisoformat(event.started_at) for event in attempted]
            ends = [datetime.fromisoformat(event.finished_at) for event in attempted]
            window_start, window_end = min(starts), max(ends)
            elapsed_hours = (window_end - window_start).total_seconds() / 3600
        else:
            window_start = window_end = None
            elapsed_hours = None
        per_hour = {
            name: (round(gained[name] / elapsed_hours, 2) if elapsed_hours and resource_samples else None)
            for name in RESOURCES
        }

        report.append(
            DayMetrics(
                account_alias=alias,
                day=day,
                window_start=window_start.isoformat() if window_start else None,
                window_end=window_end.isoformat() if window_end else None,
                attempted=len(attempted),
                verified=counts["verified"],
                not_observed=counts["not-observed"],
                contradicted=counts["contradicted"],
                uncertain=counts["unknown"],
                failed=sum(1 for event in attempted if event.error_class),
                skipped=sum(1 for event in day_events if event.outcome == "skipped" and not event.dry_run),
                dry_run=sum(1 for event in day_events if event.dry_run),
                verified_success_rate=_rate(counts["verified"], len(attempted)),
                elapsed_hours=round(elapsed_hours, 4) if elapsed_hours is not None else None,
                resources_gained=gained if resource_samples else {},
                resources_spent=spent if resource_samples else {},
                gained_per_hour=per_hour,
                resource_samples=resource_samples,
                failures=failures,
            )
        )
    return report


def to_json(metrics: list[DayMetrics]) -> str:
    return json.dumps([asdict(day) for day in metrics], indent=2, sort_keys=True)


def _cell(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def to_markdown(metrics: list[DayMetrics]) -> str:
    if not metrics:
        return "_No journal events._\n"
    lines = [
        "| Account | Day | Attempted (n) | Verified | Success rate | Uncertain | Failed | Contradicted | "
        "Minerals/h | Gas/h | Window (h) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for day in metrics:
        rate = "n/a" if day.verified_success_rate is None else f"{day.verified_success_rate:.0%}"
        lines.append(
            f"| {day.account_alias} | {day.day} | {day.attempted} | {day.verified} | {rate} | {day.uncertain} | "
            f"{day.failed} | {day.contradicted} | {_cell(day.gained_per_hour.get('minerals'))} | "
            f"{_cell(day.gained_per_hour.get('gas'))} | {_cell(day.elapsed_hours)} |"
        )
    ledger = [failure for day in metrics for failure in day.failures]
    if ledger:
        lines += ["", "**Failure ledger**", "", "| Time | Account | Action | Outcome | Error | Reason |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for day in metrics:
            for failure in day.failures:
                lines.append(
                    f"| {failure.at} | {day.account_alias} | {failure.action_id} | {failure.outcome} | "
                    f"{failure.error_class or ''} | {failure.reason.replace('|', '/')} |"
                )
    return "\n".join(lines) + "\n"
