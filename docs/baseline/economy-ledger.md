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

An attempted action is a candidate for an entry when:

- the snapshot interval lies inside the action's journal window (the runner's own before/after snapshots), or
- the action's window lies inside the interval, or
- the action started inside the interval (at or after its start snapshot, before its end snapshot) and ran past its end, and no later snapshot falls inside the action's window.

With exactly one candidate the entry is `attributed`; with several it is `ambiguous`; with none it is `unattributed`.

An action that started before the interval's start snapshot and ends inside the interval is not a candidate: the runner takes its before snapshot after the event starts, so a change observed before that snapshot cannot be the action's effect. For the same reason, an action that started inside the interval is dropped when the series also holds a later snapshot inside its window. That is the runner's own before/after pair, so the interval's end snapshot is the action's before snapshot, and the gap between two runs stays `unattributed`. Without such a pair the end snapshot may have been taken after the call, so the action stays a candidate, and a change during it is never attributed to a different action alone.

Skipped events and events of other accounts are ignored. A snapshot of another account raises `AccountMismatchError`, and snapshots out of observation order raise `SnapshotInputError`.

## Totals

`Ledger.by_action()`, `unattributed()` and `ambiguous()` return `FlowTotals`: currency gained and spent, the number of entries that observed each currency (`samples`), net item changes, and the entry count. Unobserved changes are left out of the sums and the sample counts, so a missing value never reads as zero.
