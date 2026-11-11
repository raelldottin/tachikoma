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

`active_strategy` only says which strategy a window *planned*. To credit a battle to a strategy, use the adoption log below.

## Adoption log (SPEC-08 R1, R7)

A window does not put a strategy on the ship. `StrategyAdoption` records that one did, and when:

| Field | Meaning |
| --- | --- |
| `account_alias` | Lowercase alias, never an e-mail or display name |
| `strategy_id`, `strategy_version`, `config_version` | The strategy version adopted, and its 16-hex `config_version` |
| `adopted_at` | Timezone-aware UTC time; serialised as ISO-8601 with `Z` |
| `method` | `bot-call` (a game call identified from captures) or `by-hand` (the owner set it in the game and noted the time) |
| `note` | Optional plain text. A note that `redact_secrets` would change is refused. |

A `by-hand` adoption has `approximate_timing` true: its time is the owner's note, not an observed call, so reports must say the attribution boundary is approximate (R7). Until a client call that sets native AI is captured (SPEC-08 Q2 default), every adoption is `by-hand`.

`AdoptionLog` holds adoptions per alias in strict time order. It refuses an entry earlier than the alias's last one (out of order) or at the same instant (overlapping); a refused entry leaves the log unchanged. Aliases are independent. `AdoptionLog.load(path)` and `save(path)` read and write a local JSON file:

```json
{
  "schema_version": 1,
  "adoptions": [
    {"account_alias": "alpha", "strategy_id": "weapon-focus-fire", "strategy_version": 1,
     "config_version": "<16 hex>", "adopted_at": "2027-01-04T01:00:00Z", "method": "by-hand", "note": ""}
  ]
}
```

Loading fails closed on invalid JSON, another schema version, missing or unknown fields, secret-shaped keys or notes, non-UTC times, and out-of-order or overlapping entries. The trial's log lives at `docs/experiments/r3-pvp-trial/applications.json`.

`TrialRecord(schedule, log)` joins the two. Construction refuses an adoption of an unregistered strategy version or one whose `config_version` does not match the registry. `strategy_in_force(alias, at)` returns a strategy only when:

1. `at` falls inside a window for the alias, and
2. the alias's latest adoption at or before `at` is of that window's strategy version, and
3. that adoption was made inside the window.

Otherwise it returns `None`: battles before the first adoption in a window, after a different strategy was adopted, before every window, or after every window have no strategy. An adoption made before a window opens does not carry into it; record a new adoption inside the window. `attribution_at(alias, at)` returns the same decision as a `StrategyAttribution` (strategy, window, adoption, `method`, `approximate_timing`), and `has_approximate_timing()` says whether any adoption in the log was by hand.

## Candidate strategies

`CANDIDATE_STRATEGIES` holds two strategies for the first R3 comparison: `weapon-focus-fire@v1` and `reactor-shield-devastation@v1`. Both are hypotheses with no battle evidence (`evidence_tier` `UNKNOWN`). Neither is applied to a ship by this module; it is pure data with no network access.
