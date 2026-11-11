# Currency and item ledger (R4)

`sdk/economy/ledger.py` records how one account's minerals, gas, starbux and item quantities changed over a series of snapshots, and which journaled action each change belongs to.

`build_ledger(account_alias, snapshots, events=())` turns each consecutive pair of snapshots into a `LedgerEntry`:

| Field | Meaning |
| --- | --- |
| `start`, `end` | Observation times of the two snapshots |
| `currency` | Observed change for `minerals`, `gas` and `starbux`; `None` when either side did not observe it |
| `items` | Item design id → quantity change; `None` when inventory was not observed on both sides |
| `attribution` | `attributed`, `unattributed` or `ambiguous` |
| `action_id`, `attempt_id` | The journaled action, when attributed |
| `candidates` | Attempt ids of the competing actions, when ambiguous |

## Attribution

An entry belongs to an attempted action when the snapshot interval lies inside the action's journal window (the runner's own before/after snapshots), or the action's window lies inside the interval. With exactly one such action the entry is `attributed`; with several it is `ambiguous`; with none it is `unattributed`. A partial overlap does not count: the runner takes its before snapshot after the event starts, so a change observed before that snapshot cannot be the action's effect.

Skipped events and events of other accounts are ignored. A snapshot of another account raises `AccountMismatchError`, and snapshots out of observation order raise `SnapshotInputError`.

## Totals

`Ledger.by_action()`, `unattributed()` and `ambiguous()` return `FlowTotals`: currency gained and spent, the number of entries that observed each currency (`samples`), net item changes, and the entry count. Unobserved changes are left out of the sums and the sample counts, so a missing value never reads as zero.
