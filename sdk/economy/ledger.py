"""Per-account currency and item ledger (R4).

``build_ledger`` walks an ordered series of snapshots of one account. Each
consecutive pair becomes one ``LedgerEntry`` with the observed minerals,
gas, starbux and per-item quantity changes. An entry is attributed to a
journaled action only when the snapshot interval lies inside that action's
window, or the action's window lies inside the interval, and exactly one
attempted action qualifies. Otherwise it is ``unattributed`` (no candidate)
or ``ambiguous`` (several candidates).

Unobserved values stay ``None``, never zero. Snapshots of another account
raise ``AccountMismatchError``; events of other accounts are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional, Sequence

from sdk.journal import GameplayEvent
from sdk.state import AccountMismatchError, AccountSnapshot, SnapshotInputError, diff_snapshots

CURRENCIES = ("minerals", "gas", "starbux")
ATTRIBUTED, UNATTRIBUTED, AMBIGUOUS = "attributed", "unattributed", "ambiguous"


@dataclass(frozen=True)
class LedgerEntry:
    start: str
    end: str
    attribution: str
    action_id: Optional[str]
    attempt_id: Optional[str]
    candidates: tuple[str, ...]
    currency: dict  # name -> observed change, or None when unobserved
    items: Optional[dict]  # item design id -> change; None when inventory was not observed on both sides

    @property
    def changed(self) -> bool:
        return any(value for value in self.currency.values()) or bool(self.items)


@dataclass(frozen=True)
class FlowTotals:
    """Sums of observed changes. ``samples`` counts entries that observed each currency."""

    gained: dict = field(default_factory=dict)
    spent: dict = field(default_factory=dict)
    samples: dict = field(default_factory=dict)
    items: dict = field(default_factory=dict)
    entries: int = 0


@dataclass(frozen=True)
class Ledger:
    account_alias: str
    entries: tuple[LedgerEntry, ...]

    def by_action(self) -> dict[str, FlowTotals]:
        keys = sorted({entry.action_id for entry in self.entries if entry.attribution == ATTRIBUTED})
        return {key: _totals(e for e in self.entries if e.action_id == key and e.attribution == ATTRIBUTED)
                for key in keys}

    def unattributed(self) -> FlowTotals:
        return _totals(e for e in self.entries if e.attribution == UNATTRIBUTED)

    def ambiguous(self) -> FlowTotals:
        return _totals(e for e in self.entries if e.attribution == AMBIGUOUS)


def _totals(entries: Iterable[LedgerEntry]) -> FlowTotals:
    gained: dict = {}
    spent: dict = {}
    samples: dict = {}
    items: dict = {}
    count = 0
    for entry in entries:
        count += 1
        for name, value in entry.currency.items():
            if value is None:
                continue
            samples[name] = samples.get(name, 0) + 1
            if value > 0:
                gained[name] = gained.get(name, 0) + value
            elif value < 0:
                spent[name] = spent.get(name, 0) - value
        for item, value in (entry.items or {}).items():
            if value is not None:
                items[item] = items.get(item, 0) + value
    return FlowTotals(gained=gained, spent=spent, samples=samples, items=items, entries=count)


def _contained(inner_start: datetime, inner_end: datetime, outer_start: datetime, outer_end: datetime) -> bool:
    return outer_start <= inner_start and inner_end <= outer_end


def build_ledger(
    account_alias: str, snapshots: Sequence[AccountSnapshot], events: Iterable[GameplayEvent] = ()
) -> Ledger:
    for snapshot in snapshots:
        if snapshot.account_alias != account_alias:
            raise AccountMismatchError(f"snapshot of {snapshot.account_alias!r} in the ledger for {account_alias!r}")
    for earlier, later in zip(snapshots, snapshots[1:]):
        if later.observed_at < earlier.observed_at:
            raise SnapshotInputError("snapshots must be in observation order")

    windows = [
        (datetime.fromisoformat(event.started_at), datetime.fromisoformat(event.finished_at), event)
        for event in events
        if event.account_alias == account_alias and event.attempted
    ]

    entries = []
    for before, after in zip(snapshots, snapshots[1:]):
        delta = diff_snapshots(before, after)
        start, end = before.observed_at, after.observed_at
        candidates = [
            event for (s, f, event) in windows
            if _contained(start, end, s, f) or _contained(s, f, start, end)
        ]
        if len(candidates) == 1:
            attribution, action_id, attempt_id = ATTRIBUTED, candidates[0].action_id, candidates[0].attempt_id
        else:
            attribution = AMBIGUOUS if candidates else UNATTRIBUTED
            action_id = attempt_id = None
        items = None if "inventory" in delta.unknown_sections else dict(delta.inventory_quantity)
        entries.append(LedgerEntry(
            start=start.isoformat(),
            end=end.isoformat(),
            attribution=attribution,
            action_id=action_id,
            attempt_id=attempt_id,
            candidates=tuple(event.attempt_id for event in candidates) if len(candidates) > 1 else (),
            currency={name: getattr(delta, name) for name in CURRENCIES},
            items=items,
        ))
    return Ledger(account_alias=account_alias, entries=tuple(entries))
