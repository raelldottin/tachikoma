# Gameplay action catalog (R1 / AUTO-003)

`sdk/actions/` declares, for each representative gameplay action, the `Client` method that performs it, its retry safety, what must hold before it runs, and how its effect appears in an [account snapshot](account-snapshot.md) diff. It is pure code: nothing here calls the game.

## Reconciliation outcomes

`reconcile(spec, before, after, params, response_ok)` compares two snapshots of the same account and returns one of:

| Status | Meaning |
| --- | --- |
| `verified` | The expected effect is visible in observed state. |
| `not-observed` | Every relevant field was observed on both sides and nothing changed. |
| `contradicted` | Observed state moved against the expected effect, for example resources fell after a collection or a room vanished. |
| `unknown` | The relevant state was not observed on both sides, or the snapshot cannot observe this effect at all. |

`response_ok` is recorded but never decides the outcome. An HTTP success with no observed effect is `not-observed` or `unknown`, never `verified`. Snapshots from different accounts raise `AccountMismatchError`.

## Catalog

| Action | Client method | Retry | Preconditions | Verified when |
| --- | --- | --- | --- | --- |
| `collect_all_resources` | `collectAllResources` | safe-to-retry | resources observed | minerals or gas increased |
| `collect_daily_reward` | `collectDailyReward` | once-per-period | resources observed | minerals, gas, starbux or an inventory item increased |
| `add_training` | `addTraining` | not-idempotent | crew observed, crew member present | the crew member is now training the requested design, or starts training whose design is not reported. Different training is `contradicted`; training fields missing on either side give `unknown` |
| `upgrade_room` | `upgradeRoom` (params `room_id`, `upgrade_room_design_id`) | not-idempotent | rooms observed, room present and not upgrading | room status became `Upgrading` or its design advanced |
| `add_research` | `addResearch` | not-idempotent | research observed, not already researching or completed | research state became `Researching` or `Completed` |
| `run_pvp_battle` | `runBattleEndToEnd` | not-idempotent | ship observed | trophies changed between the before and after snapshots. Unchanged trophies are `not-observed`; trophies missing on either side give `unknown` |

## Battle evidence tiers

`run_pvp_battle` journals an `evidence` mapping alongside the outcome. `evidence_tier` is the furthest step this call provably reached:

| Tier | Meaning |
| --- | --- |
| `request-sent` | The runner called `runBattleEndToEnd`; no step response was received in this call |
| `battle-created` | `CreateBattle9` returned a parsed response during this call |
| `battle-accepted` | `AcceptBattle5` returned a parsed response during this call |
| `finalised` | `FinaliseBattle15` returned a parsed response during this call |
| `outcome-observed` | The call reached `finalised` and the reconciled outcome is `verified`: a trophy change was observed |

A step counts only if its parsed response object was replaced during the call, so a response left over from an earlier battle is not evidence. `battle_id` is recorded only when a battle was created in this call and is not the flow's dummy `0`. `client_reported_outcome_trusted` is always `false`: `FinaliseBattle15` sends an outcome type chosen by the client, so a finalise response is never evidence of a win.

A trophy change counts as this call's battle outcome only if the call reached `finalised`. Without `FinaliseBattle15` no attack result was reported, so the change came from elsewhere, for example a defense while the flow failed before `CreateBattle9`. The evidence then records `outcome_supported: false` with an `outcome_support_reason`, the tier stays at the furthest battle step reached (`request-sent` if none), and the runner journals the outcome as `unknown` rather than `verified`, explaining why in the reason. Like any `unknown` attempt, it blocks a retry until reconciled. The runner applies this downgrade to any action whose evidence reports `outcome_supported: false`; only `run_pvp_battle` produces evidence today. `trophies_delta` still records the observed change.

`runBattleEndToEnd` itself, and the `run.py` battle step, are unchanged.

`tests/test_action_catalog.py` checks that every catalog method is listed as `mutating` in the [capability inventory](capability-inventory.json), and covers each outcome for each action.

## Known gaps

- `Client.upgradeRoom` sends one `UpgradeRoom2` request and does no eligibility or cost checks. The caller (the R2 policy) decides which room to upgrade and to which design. `upgradeRooms` still selects rooms exactly as before and delegates each request to it. It returns the raw HTTP response, so the runner treats the response as ambiguous and only observed state verifies the upgrade.
- `add_training` is observed from `@TrainingEndDate` and `@TrainingDesignId`. A crew record without `@TrainingEndDate` leaves training unobserved, and the outcome is `unknown`.
- No read-only `Client` method is verified to return current trophies or battle history (`findUserRanking` and `listPvPBattles2` are untested and their response schemas are unknown). Until one is, snapshots from `ClientSnapshotProvider` carry no trophies, so `run_pvp_battle` outcomes stay `unknown` and the evidence tier stops at `finalised` at best. A trophy change can also come from a defense in the same interval.
- Resource decreases from concurrent spending (another action, or a player) would show as `contradicted` for collections. The runner should reconcile with no other actions in flight on the same account.
