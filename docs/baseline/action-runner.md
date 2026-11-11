# Reconciled action runner (R1 exit)

`sdk/execution/ActionRunner` closes the R1 evidence loop for one account. It ties together the [account snapshot](account-snapshot.md), [action catalog](action-catalog.md) and [event journal](gameplay-journal.md):

1. Observe a `before` snapshot through the injected snapshot provider. If it belongs to a different alias, journal `skipped` and raise `AccountMismatchError` before any call.
2. Check the action's preconditions. If any fail, journal `skipped` with the reasons.
3. If the action is not idempotent and its last attempt with the same parameters is still `unknown`, journal `skipped` and raise `UncertainPriorAttemptError`. `allow_after_uncertain=True` overrides this once an operator has reconciled the earlier attempt by hand.
4. With `dry_run=True`, journal what would have been called and stop. No mutating call is made.
5. Call the `Client` method exactly once. The runner never retries.
6. Observe an `after` snapshot, reconcile, and journal the outcome together with the signed resource deltas (`minerals_delta`, `gas_delta`, `starbux_delta`).

## Uncertainty rules

| Situation | Journaled outcome |
| --- | --- |
| Effect observed | `verified`, even if the call raised (the error class is still recorded) |
| Call raised, effect not observed | `unknown`. The request may have reached the server. |
| Non-boolean response (for example `"LAB_UPGRADE_REQUIRED"`) and no effect | `unknown` |
| `False` response and no effect | `not-observed` |
| `after` snapshot failed | `unknown`, with the read error's class |
| `after` snapshot for another account | `unknown`, then `AccountMismatchError` is raised |

## Scope

The runner takes the client and snapshot provider by injection, and tests use a fake client with no network traffic. The [daily scheduler](daily-scheduler.md) and the [fleet orchestrator](fleet-accounts.md#orchestration) drive it; `run.py --progression-dry-run` reaches it in dry-run mode only.

`run(action_id, params, expected_cost=...)` accepts the plan's expected cost. When an attempted action's resource delta is not observed (for example, the after-snapshot failed), the planned amount is recorded as `<kind>_uncertain` in the event's cost, next to the observed `<kind>_delta` values. Budgets and the [isolation audit](fleet-audit.md) charge it; reports of observed resources do not.
