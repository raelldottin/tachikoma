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
- **Budgets.** Spend already journaled in the current period (negative cost deltas) is subtracted from each budget. What remains is passed to the policy as its `max_mineral_spend` / `max_gas_spend` caps (the tighter of that and any cap in the base policy config), and the base reserves are left as they are. The caps are absolute amounts that the policy applies to the scheduler's own planning snapshot, so a balance that grows after the orchestrator's first snapshot adds no spendable budget. Starbux spending is never allowed in a fleet run, because the policy cannot cap a starbux amount.
- **Limits.** `max_actions` caps both planned requests and executed actions.

`FleetRun` records the mode, dry-run flag, start and finish times, the fleet stop reason, and one `AccountRun` per registered account: status (`completed`, `stopped`, `error`, `disabled` or `not-run`), reason, attempted, verified and planned counts, elapsed seconds, spend this period and remaining budget.
