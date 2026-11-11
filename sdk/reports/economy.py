"""Market return and resource-allocation report (R4 exit metrics).

Rows are keyed by account alias and variant (a strategy or policy version
label chosen by the experiment). Inputs are reconciled trades
(``sdk.economy.reconcile_trade``) and ledgers (``sdk.economy.build_ledger``).

- **Market return** uses completed trades only: sell proceeds minus buy
  cost, per currency. Fees are reported separately (they are already inside
  proceeds and cost). With no completed trade the return is ``None``.
- **Unreconciled** trades are those still ``requested``, ``listed`` or
  ``unknown``; ``expired`` trades are counted on their own.
- **Resources** come from the ledger: gained and spent per currency, the
  elapsed window, per-hour gain, and spend by action. Unobserved changes are
  excluded, and every figure carries its sample count.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from sdk.economy import Ledger, TradeReconciliation

UNRECONCILED_STATES = frozenset({"requested", "listed", "unknown"})


@dataclass(frozen=True)
class TradeRecord:
    account_alias: str
    variant: str
    trade: TradeReconciliation


@dataclass(frozen=True)
class LedgerRecord:
    variant: str
    ledger: Ledger


@dataclass(frozen=True)
class EconomyRow:
    account_alias: str
    variant: str
    trades: int = 0
    completed: int = 0
    expired: int = 0
    unreconciled: int = 0
    market_return: Optional[dict] = None
    fees: dict = field(default_factory=dict)
    ledger_entries: int = 0
    window_hours: Optional[float] = None
    gained: dict = field(default_factory=dict)
    spent: dict = field(default_factory=dict)
    samples: dict = field(default_factory=dict)
    gained_per_hour: dict = field(default_factory=dict)
    spend_by_action: dict = field(default_factory=dict)
    unattributed_entries: int = 0
    ambiguous_entries: int = 0


def _add(target: dict, key: str, value: int) -> None:
    target[key] = target.get(key, 0) + value


def compute_economy_report(
    trades: Iterable[TradeRecord] = (), ledgers: Iterable[LedgerRecord] = ()
) -> list[EconomyRow]:
    rows: dict[tuple, dict] = {}

    def row(alias: str, variant: str) -> dict:
        return rows.setdefault((alias, variant), {
            "trades": 0, "completed": 0, "expired": 0, "unreconciled": 0, "return": {}, "fees": {},
            "has_return": False, "entries": [], "spend_by_action": {}, "unattributed": 0, "ambiguous": 0,
        })

    for record in trades:
        data = row(record.account_alias, record.variant)
        trade = record.trade
        data["trades"] += 1
        if trade.state == "completed":
            data["completed"] += 1
            data["has_return"] = True
            if trade.side == "sell" and trade.proceeds is not None:
                _add(data["return"], trade.currency, trade.proceeds)
            elif trade.side == "buy" and trade.cost is not None:
                _add(data["return"], trade.currency, -trade.cost)
            if trade.fees is not None:
                _add(data["fees"], trade.currency, trade.fees)
        elif trade.state == "expired":
            data["expired"] += 1
        elif trade.state in UNRECONCILED_STATES:
            data["unreconciled"] += 1

    for record in ledgers:
        ledger = record.ledger
        data = row(ledger.account_alias, record.variant)
        data["entries"].extend(ledger.entries)
        for action_id, totals in ledger.by_action().items():
            for name, value in totals.spent.items():
                _add(data["spend_by_action"].setdefault(action_id, {}), name, value)
        data["unattributed"] += ledger.unattributed().entries
        data["ambiguous"] += ledger.ambiguous().entries

    result = []
    for (alias, variant), data in sorted(rows.items()):
        gained: dict = {}
        spent: dict = {}
        samples: dict = {}
        for entry in data["entries"]:
            for name, value in entry.currency.items():
                if value is None:
                    continue
                _add(samples, name, 1)
                if value > 0:
                    _add(gained, name, value)
                elif value < 0:
                    _add(spent, name, -value)
        window = None
        if data["entries"]:
            start = min(datetime.fromisoformat(e.start) for e in data["entries"])
            end = max(datetime.fromisoformat(e.end) for e in data["entries"])
            hours = (end - start).total_seconds() / 3600
            window = round(hours, 4) if hours > 0 else None
        per_hour = {name: (round(gained.get(name, 0) / window, 2) if window and samples.get(name) else None)
                    for name in ("minerals", "gas", "starbux")}
        result.append(EconomyRow(
            account_alias=alias, variant=variant, trades=data["trades"], completed=data["completed"],
            expired=data["expired"], unreconciled=data["unreconciled"],
            market_return=dict(data["return"]) if data["has_return"] else None, fees=data["fees"],
            ledger_entries=len(data["entries"]), window_hours=window, gained=gained, spent=spent, samples=samples,
            gained_per_hour=per_hour, spend_by_action=data["spend_by_action"],
            unattributed_entries=data["unattributed"], ambiguous_entries=data["ambiguous"],
        ))
    return result


def to_json(rows: Iterable[EconomyRow]) -> str:
    return json.dumps([asdict(row) for row in rows], indent=2, sort_keys=True)


def _money(values: Optional[dict]) -> str:
    if values is None:
        return "n/a"
    if not values:
        return "0"
    return ", ".join(f"{name} {value:+d}" for name, value in sorted(values.items()))


def _cell(value) -> str:
    return "n/a" if value is None else str(value)


def to_markdown(rows: list[EconomyRow]) -> str:
    if not rows:
        return "_No trades or ledger entries._\n"
    lines = [
        "| Account | Variant | Trades | Completed | Expired | Unreconciled | Market return | Fees | "
        "Minerals/h | Gas/h | Window (h) | Unattributed entries |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row.account_alias} | {row.variant} | {row.trades} | {row.completed} | {row.expired} | "
            f"{row.unreconciled} | {_money(row.market_return)} | {_money(row.fees) if row.fees else 'n/a'} | "
            f"{_cell(row.gained_per_hour.get('minerals'))} | {_cell(row.gained_per_hour.get('gas'))} | "
            f"{_cell(row.window_hours)} | {row.unattributed_entries} |"
        )
    return "\n".join(lines) + "\n"
