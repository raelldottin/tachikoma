# Market return and resource-allocation report (R4 exit metrics)

`sdk/reports/economy.py` combines [reconciled trades](economy-trades.md) and [ledgers](economy-ledger.md) into one row per account alias and variant. A variant is whatever strategy or policy version label the experiment assigns.

`compute_economy_report(trades=(), ledgers=())` takes `TradeRecord(account_alias, variant, trade)` and `LedgerRecord(variant, ledger)` values.

| Field | Definition | Scorecard metric |
| --- | --- | --- |
| `trades`, `completed`, `expired`, `unreconciled` | Trade counts. Unreconciled trades are `requested`, `listed` or `unknown`. | Incident/failure frequency |
| `market_return` | Per currency, completed sell proceeds minus completed buy cost. `None` when no trade completed. | Market return |
| `fees` | Fees from completed trades. They are already inside proceeds and cost, so they are shown, not subtracted again. | Market return |
| `gained`, `spent`, `samples` | Observed currency gained and spent across ledger entries, and how many entries observed each currency | Resources per hour |
| `window_hours`, `gained_per_hour` | Span of the ledger entries and gain per hour; `None` without a window or samples | Resources per hour |
| `spend_by_action` | Currency spent in entries attributed to each action | Cost per progression unit |
| `unattributed_entries`, `ambiguous_entries` | Ledger entries not tied to exactly one action | — |

No sample means no number: a missing return or rate is `None` (`n/a` in Markdown), never 0. `to_json(rows)` and `to_markdown(rows)` render the report.

Real market figures need `SellItem3` and `Purchase4`, which are not implemented yet; see the blocked slice `r4-live-market-trial`.
