# Account registry (R5, AUTO-009)

`sdk/fleet/accounts.py` describes the ships a fleet run may touch. The registry holds no credentials: each profile names the secret that holds them.

## File format

```json
{
  "version": 1,
  "accounts": [
    {
      "alias": "alpha",
      "objective": "grow hull 8 economy",
      "policy_id": "baseline-v1",
      "pvp_strategy": "weapon-focus-fire@v1",
      "budgets": {"minerals": 50000, "gas": 20000, "starbux": 0},
      "max_actions": 15,
      "enabled": true,
      "credentials_secret": "TACHIKOMA_ALPHA_AUTH"
    }
  ]
}
```

| Field | Rule |
| --- | --- |
| `alias` | The [snapshot alias rule](account-snapshot.md); unique within the file |
| `objective` | Non-empty label of at most 200 characters |
| `policy_id` | A registered [progression policy](progression-policy.md) |
| `pvp_strategy` | Optional [strategy key](pvp-strategies.md) such as `weapon-focus-fire@v1` |
| `budgets` | Spend limit per period for `minerals`, `gas` and `starbux`, as non-negative integers. An unset `minerals` or `gas` budget has no limit; an unset `starbux` budget is 0, so premium currency is never unlimited by omission. |
| `max_actions` | Positive integer, default 20 |
| `enabled` | `true` or `false`, default `true` |
| `credentials_secret` | The name of the secret that holds the account's credentials, in upper snake case. Never the value. |

## Validation

`load_registry(path)` and `registry_from_dict(payload)` fail closed with `RegistryError` on:

- a wrong version, unknown top-level or profile fields, or a missing required field;
- a duplicate alias;
- any secret-named key (for example `password`, `accessToken`, `email`) anywhere in the file;
- any value that `redact_secrets` would change, such as an e-mail address or a token.

Error messages name the location but never repeat the offending value. `AccountRegistry.enabled()` returns enabled profiles in alias order.

## Orchestration

`sdk/fleet/orchestrator.py` runs the [daily scheduler](daily-scheduler.md) for each enabled account, one at a time, in alias order (`mode` `sequential`). Dry run is the default; `run(execute=True)` makes mutating calls.

- **Isolation.** An injected factory returns each account's `AccountContext`: client, snapshot provider, journal and design data. The orchestrator builds that account's runner with the account's own alias. A first snapshot of another account, or a journal file already used by another account in the same run, ends that account with `error` before any call.
- **Failure containment.** An exception in one account (for example a failed login) is recorded as `error`, with secret-shaped text redacted, and the next account still runs. A safety stop inside the scheduler ends that account as `stopped`.
- **Fleet stop.** If the stop file exists, the remaining accounts are `not-run`. The same file is passed to each scheduler, so it also stops an account between actions.
- **Budgets.** Spend already journaled in the current period is subtracted from each budget. That spend is the observed spend (negative `<kind>_delta` cost values) plus the planned cost of attempts whose effect was not observed. The scheduler passes each request's `expected_cost` to the runner. When an attempted action's resource delta is unobserved (for example because the `after` snapshot failed), the runner journals the planned amount as `<kind>_uncertain` (`minerals_uncertain`, `gas_uncertain`, `starbux_uncertain`), and `spent_in_period` charges it whenever the matching `<kind>_delta` is missing or `null`. An attempt that may have spent its cost therefore never makes that cost spendable again. Dry runs and skipped events never carry `_uncertain` keys. Progression and economy reports keep summing observed deltas only. Journals written before `_uncertain` existed charge observed spend only. What remains is passed to the policy as its `max_mineral_spend` / `max_gas_spend` caps (the tighter of that and any cap in the base policy config), and the base reserves are left as they are. The caps are absolute amounts that the policy applies to the scheduler's own planning snapshot, so a balance that grows after the orchestrator's first snapshot adds no spendable budget. Starbux spending is never allowed in a fleet run, because the policy cannot cap a starbux amount.
- **Limits.** `max_actions` caps both planned requests and executed actions.

### Concurrent mode

`sdk/fleet/concurrent.py` provides `ConcurrentFleetOrchestrator`, which takes the same arguments plus `max_workers` (a positive integer, default 4) and runs accounts on a thread pool of at most that many workers (`mode` `concurrent`). It runs the sequential orchestrator's own per-account code, so isolation, failure containment, budgets, the starbux rule, `max_actions` and dry run by default are unchanged. With `max_workers=1` it runs in exactly the sequential order.

- **Steps.** Each account is three worker steps, never more than one in flight: *open* (the factory call, which may log in), *first snapshot*, and *schedule* (the budgeted scheduler run).
- **Claims.** Between steps the coordinating thread decides the account's claims in alias order, through the run's lock-guarded `FleetClaims` table. The journal claim comes after *open*, and `claim_snapshot` comes after *first snapshot*. The first account in alias order wins a contested claim, whatever order the steps finish in. A refused account ends with `error` before its next step, so its scheduler makes no call. `claim_snapshot` checks that the snapshot belongs to the account, and it is the hook for claims that depend on the first snapshot, such as game identity. A subclass returned by `_new_claims()` can extend it, and the sequential orchestrator calls the same hooks in the same order.
- **Scheduling.** When a worker is free, the coordinator starts the ready step of the earliest account in alias order. So an account that has opened is snapshotted and scheduled before later accounts open, and with fewer workers than accounts a logged-in session does not wait behind later logins.
- **Cost of deterministic claims.** An account's first snapshot waits until every earlier account's factory has returned. Its scheduler waits until every earlier account's first snapshot has been claimed. One slow login therefore delays the first snapshots of all later accounts, though not the schedulers of accounts already past their claims.
- **Failure containment.** An exception in one account's step, including one escaping a worker, is recorded as that account's `error` with secret-shaped text redacted. Other accounts, including those in flight, are not affected.
- **Fleet stop.** The stop file is checked before every step starts. Once seen, it stays in force for the run and `stop_reason` is set. An account not yet opened is `not-run` with the fleet stop reason. An account already opened but not yet scheduled is also `not-run`, with the reason `account opened but stopped before scheduling: ...`, because its factory ran and may have logged in. Schedulers already running stop between actions as in sequential mode.
- **Order and timing.** `accounts` is in alias order whatever order the accounts finish in. The injected `clock`, `timer` and factory are used as in sequential mode and must be safe to call from worker threads.

`FleetRun` records the mode, dry-run flag, start and finish times, the fleet stop reason, and one `AccountRun` per registered account: status (`completed`, `stopped`, `error`, `disabled` or `not-run`), reason, attempted, verified and planned counts, elapsed seconds, spend this period and remaining budget.
