"""Static HTML operator dashboard over local journals (AUTO-012).

``build_dashboard`` runs the existing offline reports over one set of journal
events: progression metrics, the PvP scorecard, the fleet report and, when a
registry is given, the isolation audit. ``render_html`` turns the result into
one self-contained HTML page: inline CSS only, no scripts, no external assets,
and a Content-Security-Policy that forbids loading anything. ``write_dashboard``
writes it atomically.

Rules:

- Every value placed in the page is HTML-escaped, and free text is passed
  through ``sdk.redaction`` first. Journal text is data, never markup.
- A number that was not observed renders as ``n/a``, never 0. Without a
  registry the audit did not run, so incident counts are ``n/a``.
- Rates show their sample size next to them.
- The page holds no generation time, so the same journals always render the
  same bytes. The data window shown is the span of the journal events.
"""

from __future__ import annotations

import html
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

from sdk.fleet.audit import AuditReport, audit_fleet
from sdk.journal import GameplayEvent
from sdk.redaction import redact_secrets
from sdk.reports.battles import battles_from_events
from sdk.reports.fleet import FleetReport, Throughput, compute_fleet_report
from sdk.reports.progression import DayMetrics, compute_progression_metrics
from sdk.reports.pvp import RoleStats, ScorecardRow, compute_scorecard

NA = "n/a"


@dataclass(frozen=True)
class Dashboard:
    journal_names: tuple[str, ...]
    event_count: int
    window_start: Optional[str]
    window_end: Optional[str]
    progression: tuple[DayMetrics, ...]
    scorecard: tuple[ScorecardRow, ...]
    fleet: FleetReport
    audit: Optional[AuditReport]


def build_dashboard(events: Iterable[GameplayEvent], *, registry=None,
                    journal_names: Sequence[str] = ()) -> Dashboard:
    """Compute every section from ``events``. Events are grouped by their own
    ``account_alias``, as in ``run.py --report fleet``."""
    events = list(events)
    journals: dict[str, list[GameplayEvent]] = {}
    for event in events:
        journals.setdefault(event.account_alias, []).append(event)
    audit = audit_fleet(registry, journals) if registry is not None else None
    starts = [datetime.fromisoformat(event.started_at) for event in events]
    ends = [datetime.fromisoformat(event.finished_at) for event in events]
    return Dashboard(
        journal_names=tuple(journal_names),
        event_count=len(events),
        window_start=min(starts).isoformat() if starts else None,
        window_end=max(ends).isoformat() if ends else None,
        progression=tuple(compute_progression_metrics(events)),
        scorecard=tuple(compute_scorecard(battles_from_events(events))),
        fleet=compute_fleet_report(journals, audit=audit),
        audit=audit,
    )


# --- formatting ---------------------------------------------------------------------


def _e(value) -> str:
    """Escape one value for HTML text or a quoted attribute. ``None`` is ``n/a``."""
    if value is None:
        return NA
    return html.escape(redact_secrets(str(value)), quote=True)


def _num(value, digits: int = 2) -> str:
    if value is None:
        return NA
    if isinstance(value, float):
        return _e(f"{value:.{digits}f}")
    return _e(value)


def _pct(value: Optional[float]) -> str:
    return NA if value is None else _e(f"{value:.0%}")


def _rate_n(value: Optional[float], n: int) -> str:
    return f"{_pct(value)} <span class=\"n\">(n={_e(n)})</span>"


def _win_rate(stats: RoleStats) -> str:
    if stats.win_rate is None or stats.interval is None:
        return f"{NA} <span class=\"n\">(n={_e(stats.known)})</span>"
    low, high = stats.interval
    return (f"{_pct(stats.win_rate)} <span class=\"n\">({_pct(low)}&ndash;{_pct(high)}, "
            f"n={_e(stats.known)})</span>")


def _table(caption: str, headers: Sequence[str], rows: Sequence[Sequence[str]], empty: str,
           numeric_from: int = 1) -> str:
    """Rows hold already-escaped cell HTML. Columns from ``numeric_from`` are right-aligned."""
    if not rows:
        return f"<p class=\"empty\">{_e(empty)}</p>"
    def align(index: int) -> str:
        return ' class="num"' if index >= numeric_from else ""

    head = "".join(
        f"<th scope=\"col\"{align(index)}>{_e(name)}</th>" for index, name in enumerate(headers)
    )
    body = []
    for row in rows:
        cells = "".join(f"<td{align(index)}>{cell}</td>" for index, cell in enumerate(row))
        body.append(f"<tr>{cells}</tr>")
    return (f"<div class=\"scroll\"><table><caption>{_e(caption)}</caption><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>")


def _section(anchor: str, title: str, intro: str, content: str) -> str:
    return (f"<section id=\"{_e(anchor)}\"><h2>{_e(title)}</h2><p class=\"intro\">{_e(intro)}</p>"
            f"{content}</section>")


# --- sections -----------------------------------------------------------------------


def _tiles(board: Dashboard) -> str:
    fleet = board.fleet.fleet
    audit = board.audit
    tiles = [
        ("Attempted", _e(fleet.attempted), "mutating attempts across all accounts"),
        ("Verified", _e(fleet.verified), "effect confirmed by a later observation"),
        ("Success rate", _pct(fleet.success_rate), f"verified / attempted, n={fleet.attempted}"),
        ("Uncertain", _e(fleet.uncertain), f"outcome unknown, n={fleet.attempted}"),
        ("Incidents", NA if audit is None else _e(len(audit.incidents)),
         "audit not run (no --registry)" if audit is None
         else f"{audit.accounts_checked} accounts, {audit.events_checked} events checked"),
    ]
    items = []
    for label, value, note in tiles:
        css = "tile alert" if label == "Incidents" and audit is not None and audit.incidents else "tile"
        items.append(
            f"<div class=\"{css}\"><div class=\"label\">{_e(label)}</div>"
            f"<div class=\"value\">{value}</div><div class=\"note\">{_e(note)}</div></div>"
        )
    return f"<section id=\"summary\" aria-label=\"Summary\"><div class=\"tiles\">{''.join(items)}</div></section>"


def _progression(board: Dashboard) -> str:
    rows = [
        [
            _e(day.account_alias), _e(day.day), _e(day.attempted), _e(day.verified),
            _rate_n(day.verified_success_rate, day.attempted), _e(day.uncertain), _e(day.not_observed),
            _e(day.contradicted), _e(day.failed), _e(day.skipped), _e(day.dry_run),
            _num(day.gained_per_hour.get("minerals")), _num(day.gained_per_hour.get("gas")),
            _e(day.resource_samples), _num(day.elapsed_hours),
        ]
        for day in board.progression
    ]
    table = _table(
        "Progression per account and UTC day",
        ["Account", "Day", "Attempted", "Verified", "Success rate", "Uncertain", "Not observed", "Contradicted",
         "Failed", "Skipped", "Dry run", "Minerals/h", "Gas/h", "Resource samples", "Window (h)"],
        rows, "No journal events.", numeric_from=2,
    )
    return _section("progression", "Progression",
                    "Success rate is verified over attempted. Dry-run and skipped events are not attempts. "
                    "Per-hour rates need at least one observed resource change.", table)


def _pvp(board: Dashboard) -> str:
    rows = []
    for row in board.scorecard:
        net = NA
        if row.net_trophies is not None:
            net = f"{_e(f'{row.net_trophies:+d}')} <span class=\"n\">(n={_e(row.trophy_samples)})</span>"
        rows.append([
            _e(row.account_alias), _e(row.strategy_key),
            _e(f"{row.opponent_hull_bracket} / {row.opponent_trophy_bracket}"),
            f"{_e(row.attack.known)}/{_e(row.attack.battles)}", _win_rate(row.attack),
            f"{_e(row.defense.known)}/{_e(row.defense.battles)}", _win_rate(row.defense),
            _e(row.attack.unknown + row.defense.unknown), net, _num(row.mean_duration_s, 1),
        ])
    table = _table(
        "PvP scorecard",
        ["Account", "Strategy", "Opponent cohort", "Attacks (known/total)", "Attack win rate (95% CI)",
         "Defenses (known/total)", "Defense win rate (95% CI)", "Unknown", "Net trophies", "Mean duration (s)"],
        rows, "No battles.", numeric_from=3,
    )
    return _section("pvp", "PvP scorecard",
                    "Win rates use known-result battles only, with 95% Wilson intervals. No experiment schedule "
                    "is applied offline, so every strategy shows as none.", table)


def _fleet_row(alias: str, status: Optional[str], t: Throughput, incidents: str) -> list[str]:
    return [
        alias, _e(status), _e(t.attempted), _e(t.verified), _rate_n(t.success_rate, t.attempted),
        _pct(t.uncertain_rate), _pct(t.failure_rate), _e(t.contradicted), _e(t.skipped),
        _num(t.actions_per_hour), _num(t.elapsed_hours), incidents,
    ]


def _fleet(board: Dashboard) -> str:
    report, audited = board.fleet, board.audit is not None
    rows = [
        _fleet_row(_e(row.account_alias), row.status, row.throughput, _e(row.incidents) if audited else NA)
        for row in report.accounts
    ]
    if rows:
        rows.append(_fleet_row("<strong>fleet</strong>", None, report.fleet,
                               _e(report.incidents) if audited else NA))
    table = _table(
        "Fleet throughput",
        ["Account", "Status", "Attempted", "Verified", "Success rate", "Uncertain rate", "Failure rate",
         "Contradicted", "Skipped", "Actions/h", "Elapsed (h)", "Incidents"],
        rows, "No accounts.", numeric_from=2,
    )
    return _section("fleet", "Fleet",
                    "Events are grouped by their own account alias across all journals. Status comes from a "
                    "fleet run, which offline reports do not have.", table)


def _audit(board: Dashboard) -> str:
    audit = board.audit
    if audit is None:
        return _section("audit", "Audit incidents", "The isolation audit needs an account registry.",
                        f"<p class=\"empty\">{_e('Audit not run: pass --registry PATH to check isolation.')}</p>")
    rows = [
        [_e(incident.rule), _e(incident.account_alias), _e(", ".join(incident.attempt_ids) or NA), _e(incident.detail)]
        for incident in audit.incidents
    ]
    by_rule = ", ".join(f"{rule} {count}" for rule, count in sorted(audit.by_rule().items()))
    intro = (f"Checked {audit.accounts_checked} accounts and {audit.events_checked} events. "
             + (f"By rule: {by_rule}." if by_rule else "No incidents.")
             + " Events are grouped by alias, so the foreign-event rule cannot fire here.")
    table = _table("Isolation incidents", ["Rule", "Account", "Attempt ids", "Detail"], rows,
                   "No incidents.", numeric_from=4)
    return _section("audit", "Audit incidents", intro, table)


def _ledger(board: Dashboard) -> str:
    rows = [
        [_e(failure.at), _e(day.account_alias), _e(failure.action_id), _e(failure.outcome),
         _e(failure.error_class or "none"), _e(failure.reason or NA), _e(failure.attempt_id)]
        for day in board.progression for failure in day.failures
    ]
    table = _table("Failure ledger", ["Time", "Account", "Action", "Outcome", "Error", "Reason", "Attempt id"],
                   rows, "No failures, contradictions or unknown outcomes.", numeric_from=7)
    return _section("failures", "Failure ledger",
                    f"Attempts that raised an error, were contradicted, or have an unknown outcome "
                    f"(n={len(rows)}).", table)


CSS = """
:root{color-scheme:light dark;--bg:#f7f7f5;--surface:#fff;--text:#1d1d1b;--muted:#5f5f5a;--line:#deded8;
--accent:#2457c5;--alert:#a8361f;--alert-bg:#fbeae6}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--surface:#1f1f1d;--text:#ececea;--muted:#a3a39d;
--line:#3a3a36;--accent:#8fb1ff;--alert:#ff9b85;--alert-bg:#3a201a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 48px}
header h1{font-size:1.5rem;margin:0 0 4px}
header p,.intro,.note,.n,.empty,footer{color:var(--muted)}
header p{margin:0}
nav{margin:12px 0 0;display:flex;flex-wrap:wrap;gap:4px 16px}
nav a{color:var(--accent)}
h2{font-size:1.15rem;margin:32px 0 4px}
.intro{margin:0 0 12px;font-size:.9rem}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(170px,100%),1fr));gap:12px;margin-top:20px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.tile .label{font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.tile .value{font-size:1.8rem;font-weight:600;font-variant-numeric:tabular-nums}
.tile .note{font-size:.8rem}
.tile.alert{border-color:var(--alert);background:var(--alert-bg)}
.tile.alert .value{color:var(--alert)}
.scroll{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:.875rem}
caption{position:absolute;left:-9999px}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
td:last-child{white-space:normal}
th{font-weight:600;background:var(--bg)}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.n{font-size:.8em}
.empty{margin:0;padding:12px 14px;background:var(--surface);border:1px dashed var(--line);border-radius:8px}
footer{margin-top:40px;font-size:.8rem}
""".strip()


def render_html(board: Dashboard) -> str:
    """The whole page as one string. Deterministic for the same ``Dashboard``."""
    sources = ", ".join(board.journal_names) or NA
    window = (f"{_e(board.window_start)} to {_e(board.window_end)}"
              if board.window_start is not None else NA)
    nav = "".join(f"<a href=\"#{anchor}\">{_e(label)}</a>" for anchor, label in (
        ("progression", "Progression"), ("pvp", "PvP"), ("fleet", "Fleet"),
        ("audit", "Audit"), ("failures", "Failure ledger")))
    body = "".join([
        _tiles(board), _progression(board), _pvp(board), _fleet(board), _audit(board), _ledger(board),
    ])
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">\n"
        "<meta name=\"color-scheme\" content=\"light dark\">\n"
        "<title>Tachikoma dashboard</title>\n"
        f"<style>\n{CSS}\n</style>\n</head>\n<body>\n<main>\n"
        "<header><h1>Tachikoma dashboard</h1>"
        f"<p>Journals: {_e(sources)} &middot; {_e(board.event_count)} events &middot; data window {window}</p>"
        f"<nav aria-label=\"Sections\">{nav}</nav></header>\n"
        f"{body}\n"
        "<footer>Built offline from local journal files. An API success is not a verified game effect; "
        "values that were not observed show as n/a.</footer>\n"
        "</main>\n</body>\n</html>\n"
    )


def write_dashboard(text: str, path: Path | str) -> Path:
    """Write ``text`` to ``path`` atomically: a temporary file in the same
    directory, fsynced, then renamed over the target. The directory must exist."""
    path = Path(path)
    directory = path.parent if str(path.parent) else Path(".")
    if not directory.is_dir():
        raise FileNotFoundError(f"output directory not found: {directory}")
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path
