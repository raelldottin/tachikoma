# PvP battle results from the journal (R3)

`sdk/reports/battles.py` turns journaled `run_pvp_battle` events into battle records. `battles_from_events(events, schedule=None)` returns one `JournalBattle` per attempted battle. It holds an `sdk.pvp.BattleResult`, plus the facts `BattleResult` cannot carry:

| Field | Meaning |
| --- | --- |
| `result` | `BattleResult` with role `attacker`, the account alias as pseudonym, and `created_at` set to the event's finish time |
| `attempt_id` | The journal attempt id |
| `evidence_tier` | From the event's [battle evidence](action-catalog.md#battle-evidence-tiers); `request-sent` when absent |
| `trophy_delta` | The observed trophy change, or `None` when trophies were not observed on both sides. `result.trophy_delta` is `0` in that case because `BattleResult` cannot hold `None`. |
| `strategy_key`, `strategy_config_version` | The [strategy](pvp-strategies.md) active for the alias when the battle started, or `None` |
| `opponent_hull_bracket`, `opponent_trophy_bracket` | Cohort labels, `unknown` unless the event recorded opponent facts |

## Rules

- **Outcome.** A battle is `win` or `loss` only if the event reconciled as `verified`, with evidence tier `outcome-observed`, a non-zero observed trophy change, and no `error_class`. Every other battle is `unknown`, including one where the evidence is inconsistent or the call raised. A trophy change without a `finalised` step in the same call (see [battle evidence](action-catalog.md#battle-evidence-tiers)) is never journaled as `verified`, so it stays `unknown`.
- **Strategy.** It comes from the experiment window covering the battle's start time, converted to UTC first, so journal times with any offset work. A battle outside every window gets `battle_strategy` `unknown` and no strategy key.
- **Not battles.** Skipped events, dry-run events and other actions are ignored.
- **Battle id.** The evidence `battle_id` when one was created, otherwise `attempt-<attempt id>`.
- **Cohorts.** Hull brackets are 1-4, 5-7, 8-10, 11-13 and 14-20; trophy brackets are 500 wide. No journaled event records opponent facts yet, so every cohort is `unknown` today.

The runner now records `trophies_delta` in battle evidence (`None` when unobserved), which is where the observed change comes from.
