# Fleet isolation audit (R5)

`sdk/fleet/audit.py` checks that no account's journal holds another account's work and that no account overspent. `audit_fleet(registry, journals, period=24h)` takes the [account registry](fleet-accounts.md) and a mapping of journal owner alias → that journal's events.

| Rule | Incident when |
| --- | --- |
| `foreign-event` | An event's alias is not the journal's owner |
| `unregistered-account` | A journal owner, or a foreign event's alias, is not in the registry |
| `attempt-id-reused` | One attempt id appears more than once, in one journal or across journals |
| `overlapping-attempts` | Two attempted events on one account were in flight at the same time. Windows that only touch do not overlap. |
| `budget-exceeded` | An account's observed spend (negative cost deltas of attempted events) in one budget period exceeds its budget for that currency. Periods are fixed windows counted from the Unix epoch, so UTC days by default. An omitted starbux budget is 0, so any starbux spend is an incident. |

Each `Incident` names the rule, the account, the attempt ids involved and a one-line detail. `AuditReport` states `accounts_checked` and `events_checked` even when it is clean, and offers `by_rule()` and `to_json()`.
