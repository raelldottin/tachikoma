# PvP strategies and experiment windows (R3, AUTO-006)

`sdk/pvp/strategies.py` makes a PvP strategy a versioned piece of data, so battle results can be attributed to the exact configuration that produced them.

## Strategy

| Field | Meaning |
| --- | --- |
| `strategy_id`, `version` | Lowercase slug and integer version. `key` is `<id>@v<version>`. |
| `battle_strategy` | One of the `sdk.pvp.BattleStrategy` values |
| `native_ai_rules` | `NativeAIRule` entries describing what the native room/crew AI should do |
| `crew_roles` | `(role, room_category)` pairs, each role at most once |
| `power_priorities`, `target_priorities` | Room categories in priority order, no repeats |
| `config_version` | First 16 hex characters of a SHA-256 over the behaviour-defining fields. The description and evidence tier are not part of the hash, so rewording a description does not change it. |

Validation fails closed (`PvpInputError`). A description that `redact_secrets` would change is refused.

`StrategyRegistry` keys strategies by `(strategy_id, version)`. Once a version is registered, re-registering it with different behaviour is refused; publish a new version instead. `strategy_index` maps `config_version` back to a strategy.

## Experiment windows

`ExperimentWindow(account_alias, strategy_id, strategy_version, start, end)` assigns one registered strategy to one account alias for a half-open UTC interval `[start, end)`. Times must be timezone-aware UTC. `ExperimentSchedule` refuses:

- a window for a strategy version that is not registered;
- a window that overlaps another window for the same alias.

Windows for different aliases are independent. `active_strategy(alias, at)` returns the strategy in force at `at`, or `None` when no window covers it, so a battle outside every window is never attributed to a strategy.

## Candidate strategies

`CANDIDATE_STRATEGIES` holds two strategies for the first R3 comparison: `weapon-focus-fire@v1` and `reactor-shield-devastation@v1`. Both are hypotheses with no battle evidence (`evidence_tier` `UNKNOWN`). Neither is applied to a ship by this module; it is pure data with no network access.
