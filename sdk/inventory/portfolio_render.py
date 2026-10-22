from __future__ import annotations

"""Pure renderers for the read-only Task H multi-account views.

`inventory accounts` and `inventory deficits` are rendered here. The
caller (sdk/commands.py) owns disk reads, clock reads and the client;
this module receives already-loaded AccountSnapshot objects plus an
explicit ``now`` and produces deterministic text. No disk, no
network, no clock, no client.

Staleness (Task H freshness axis, pinned in
docs/task-h-reconciliation-t_8c21999b.md sections 4/4a):

- STALE_AFTER_SECONDS is the single v1 rendering-annotation threshold:
  24h, matching the game's daily-reward cadence. Finer precision would
  be invented; the constant is exported and overridable per call.
- Staleness is a RENDERING ANNOTATION, never a rejection: stale
  snapshots still render, with a visible STALE marker on every line
  derived from them. Portfolio-level "all fresh" means every loaded
  snapshot is fresh.
- Staleness NEVER changes an evidence tier (evidence is about protocol
  verification, not age).
- ``captured_at`` is pinned ISO-8601 UTC Z-suffix seconds. An
  unparseable timestamp is malformed HERE (the whole view refuses,
  fail closed), and so is a FUTURE timestamp — future-ness is
  clock-relative, so it is checked exactly where ``now`` is injected
  (this module), never at file load. The snapshot loader's weaker
  string gate is a known gap owned by the adversarial lane; this
  render surface enforces the pinned format independently.

Hard boundaries (Task H brief): no donor/recipient structures, no
account pairing or ranking, no transfer planning, no GiftItem,
marketplace, SellItem3 or Purchase4 semantics, no destination
selection, no executor. Deficits are per-account observations of that
account's own demand. Every rendered row is actionable=false by
construction, and the required wording "Candidate does not mean
transferable." appears in both views.
"""

import datetime
from dataclasses import dataclass
from typing import Dict, List, Sequence

from ._types import Evidence
from .portfolio import PortfolioError, aggregate_snapshots
from .snapshot import AccountSnapshot

# Single v1 staleness threshold (rendering annotation, seconds).
STALE_AFTER_SECONDS = 24 * 60 * 60

# Pinned v1 timestamp convention: ISO-8601, UTC, Z suffix, seconds.
_CAPTURED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class PortfolioViewError(PortfolioError):
    """Fail-closed multi-account view error.

    Raised instead of any partial rendering when a snapshot's
    captured_at is unparseable or future-dated, an evidence tier is
    unknown, or aggregation itself refuses (duplicate accounts,
    non-snapshot input). Never a silent drop of one account.
    """


def _parse_iso_utc(text: object, what: str) -> int:
    """Parse a pinned-format ISO-8601 UTC timestamp to epoch seconds."""
    if not isinstance(text, str):
        raise PortfolioViewError(
            f"{what} must be an ISO-8601 UTC string, got {type(text).__name__}"
        )
    try:
        parsed = datetime.datetime.strptime(text, _CAPTURED_AT_FORMAT)
    except ValueError as exc:
        raise PortfolioViewError(
            f"{what} {text!r} is not a valid ISO-8601 UTC Z-suffix "
            f"seconds timestamp: {exc}"
        ) from exc
    return int(parsed.replace(tzinfo=datetime.timezone.utc).timestamp())


@dataclass(frozen=True)
class SnapshotFreshness:
    """Per-snapshot freshness record (pure, deterministic).

    Computed only from (captured_at, now, stale_after); no clock is
    read here. ``age_seconds`` is now minus captured_at; ``stale`` is
    age strictly greater than the threshold.
    """

    captured_at: str
    age_seconds: int
    stale: bool

    @property
    def fresh(self) -> bool:
        return not self.stale


def snapshot_freshness(
    captured_at: str,
    now: str,
    stale_after: int = STALE_AFTER_SECONDS,
) -> SnapshotFreshness:
    """Deterministic freshness record for one snapshot (section 4a).

    Future-dated captured_at is malformed (fail closed) — validated
    here because ``now`` is available here.
    """
    if not isinstance(stale_after, int) or isinstance(stale_after, bool):
        raise PortfolioViewError(
            f"stale_after must be an integer number of seconds, got {stale_after!r}"
        )
    if stale_after < 0:
        raise PortfolioViewError(
            f"stale_after must be non-negative, got {stale_after}"
        )
    captured_epoch = _parse_iso_utc(captured_at, "captured_at")
    now_epoch = _parse_iso_utc(now, "now")
    age = now_epoch - captured_epoch
    if age < 0:
        raise PortfolioViewError(
            f"snapshot captured_at {captured_at!r} is in the future "
            f"relative to now {now!r} (malformed; fail closed)"
        )
    return SnapshotFreshness(
        captured_at=str(captured_at),
        age_seconds=age,
        stale=age > stale_after,
    )


def _validated_freshness(
    snapshots: Sequence[AccountSnapshot], now: str
) -> Dict[int, SnapshotFreshness]:
    """Freshness for every snapshot; ANY problem refuses the whole view."""
    fresh: Dict[int, SnapshotFreshness] = {}
    for snapshot in snapshots:
        fresh[snapshot.user_id] = snapshot_freshness(snapshot.captured_at, now)
    return fresh


def _safe_evidence(tier: object) -> Evidence:
    """Evidence member or fail-closed view error (never a silent guess)."""
    try:
        return Evidence(str(tier))
    except ValueError as exc:
        raise PortfolioViewError(f"unknown evidence tier {tier!r}") from exc


def _weakest_evidence(tiers: Sequence[object]) -> str:
    if not tiers:
        return Evidence.UNKNOWN.value
    return Evidence.weakest([_safe_evidence(t) for t in tiers]).value


_NO_SNAPSHOTS_MESSAGE = (
    "No account snapshots found. Nothing to report: this view reads "
    "persisted account-snapshot files (schema v1, written by the "
    "sdk.inventory build_snapshot API) from the configured snapshots "
    "directory, and none are present. This view is strictly read-only; "
    "no game state is read or modified here."
)

_READ_ONLY_FOOTER = (
    "All Task-H output is actionable=false and 'inventory apply' "
    "remains refused."
)


def render_accounts_view(
    snapshots: Sequence[AccountSnapshot], now: str
) -> str:
    """Render `inventory accounts`: one summary block per account.

    Per-account observed facts only: identity, capture time and
    freshness, stack/design counts, demand totals, dry-run candidate
    total, weakest-input evidence and unresolved condition codes.
    Candidates are informational Task G numbers; no quantity in this
    view can move between accounts.
    """
    portfolio = aggregate_snapshots(snapshots)
    if not snapshots:
        return _NO_SNAPSHOTS_MESSAGE
    fresh = _validated_freshness(snapshots, now)
    all_fresh = all(f.fresh for f in fresh.values())

    lines = [
        "=== Inventory accounts (READ-ONLY Task H — persisted "
        "multi-account snapshots; nothing executable) ===",
        f"Accounts: {len(snapshots)} ("
        + ("all fresh" if all_fresh else "some STALE — marked below")
        + f"); staleness threshold {STALE_AFTER_SECONDS}s. Every number "
        "below is an observed fact from one account's persisted "
        "snapshot; STALE markers annotate age and never change any "
        "evidence tier.",
        "Candidate does not mean transferable. No account is paired "
        "with another, nothing here is actionable, and no quantity in "
        "this view can move between accounts.",
        "",
    ]
    for snapshot in portfolio.accounts:
        record = fresh[snapshot.user_id]
        marker = "fresh" if record.fresh else "STALE"
        observed = sum(d.observed_quantity for d in snapshot.designs)
        requested = sum(d.requested_quantity for d in snapshot.designs)
        reserved = sum(d.reserved_quantity for d in snapshot.designs)
        unmet = sum(d.unmet_quantity for d in snapshot.designs)
        candidates = sum(s.candidate_quantity for s in snapshot.stacks)
        tiers: List[object] = [d.evidence for d in snapshot.designs]
        tiers.extend(s.evidence for s in snapshot.stacks)
        tiers.extend(s.candidate_evidence for s in snapshot.stacks)
        codes = sorted({c.code for c in snapshot.conditions})
        lines.append(
            f"  account {snapshot.user_id} | {snapshot.identity.display_name or '(no name)'} "
            f"| ship {snapshot.identity.ship_id} | captured {snapshot.captured_at} "
            f"| {marker} (age {record.age_seconds}s)"
        )
        lines.append(
            f"    stacks {len(snapshot.stacks)} | designs {len(snapshot.designs)} "
            f"| observed {observed} | requested {requested} | reserved {reserved} "
            f"| unmet (informational) {unmet}"
        )
        lines.append(
            f"    dry-run candidates {candidates} (informational only — a Task G "
            "number, never a quantity that can move between accounts)"
        )
        lines.append(f"    weakest evidence {_weakest_evidence(tiers)}")
        if codes:
            lines.append(
                f"    unresolved conditions {len(codes)}: {', '.join(codes)}"
            )
        else:
            lines.append("    unresolved conditions 0")
    lines.append("")
    lines.append(
        "'inventory deficits' reports unmet demand per account. " + _READ_ONLY_FOOTER
    )
    return "\n".join(lines)


def render_deficits_view(
    snapshots: Sequence[AccountSnapshot], now: str
) -> str:
    """Render `inventory deficits`: designs with per-account unmet demand.

    Unmet = requested quantity an account's own plans claim beyond what
    that account holds (requested - observed, floored at 0) — a demand-
    side observation about ONE account, never an instruction to
    acquire or move anything. Candidate quantities are deliberately
    not rendered here: pairing them with deficits would imply matching
    accounts against each other, which this slice forbids.
    """
    portfolio = aggregate_snapshots(snapshots)
    if not snapshots:
        return _NO_SNAPSHOTS_MESSAGE
    fresh = _validated_freshness(snapshots, now)
    rows = portfolio.rows_with_deficits()

    stale_count = sum(1 for f in fresh.values() if not f.fresh)
    lines = [
        "=== Inventory deficits (READ-ONLY Task H — unmet demand per "
        "account; nothing executable) ===",
        f"Accounts: {len(snapshots)}; designs with unmet demand: {len(rows)}. "
        "Unmet = requested quantity an account's own plans claim beyond what "
        "that account holds (requested - observed, floored at 0); reported "
        "per account only. STALE markers annotate snapshot age and never "
        "change any evidence tier.",
        "Candidate does not mean transferable. No account is paired with "
        "another, no deficit here is an instruction to acquire or move "
        "anything, and nothing in this view is actionable.",
        "",
    ]
    for row in rows:
        name = row.item_name or "(no name in snapshots)"
        unresolved = ", ".join(row.unresolved_codes) if row.unresolved_codes else "-"
        lines.append(
            f"  design {row.item_design_id} | {name} | unmet total "
            f"{row.total_unmet} | evidence {row.evidence} | unresolved: {unresolved}"
        )
        for entry in row.accounts:
            if entry.unmet_quantity <= 0:
                # Supply-holders are deliberately NOT listed beside
                # deficit-holders: a donor/recipient-style table is the
                # exact structure Task H forbids. Only the accounts whose
                # own demand is unmet appear in this view.
                continue
            record = fresh[entry.user_id]
            marker = "fresh" if record.fresh else "STALE"
            lines.append(
                f"    account {entry.user_id} ({entry.display_name or '(no name)'}) "
                f"| observed {entry.observed_quantity} | requested "
                f"{entry.requested_quantity} | reserved {entry.reserved_quantity} "
                f"| unmet (informational) {entry.unmet_quantity} | captured "
                f"{record.captured_at} | {marker}"
            )
    if not rows:
        lines.append(
            f"No design has unmet demand across the {len(snapshots)} loaded "
            "snapshots."
        )
    lines.append("")
    lines.append(_READ_ONLY_FOOTER)
    return "\n".join(lines)
