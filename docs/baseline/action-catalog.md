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
| `add_training` | `addTraining` (params `training_design_id`, `character_id`) | not-idempotent | crew observed, crew member present, numeric `training_design_id` (a positive int or ASCII digit text matching `[1-9][0-9]*`: never whitespace-padded, never zero-padded like `007`, never `0`), and the member idle: training observed, no training running (with or without a reported design), and no ended training with a reported design left uncollected; a placeholder end in the past with no design counts as idle | the crew member shows a new training (design or end changed) of the requested design that ends after the before-snapshot's `observed_at`. A training unchanged from before the call (even of the requested design), or no training after, is `not-observed`; a new training of another design is `contradicted`; a "new" training whose end is unreadable or already past, a new training whose design is not reported, or training fields missing on either side, give `unknown` |
| `finish_training` | `finishTraining` (param `character_id`) | safe-to-retry | crew observed, crew member present, training observed, a reported training design (so an idle member's placeholder end is refused), and its end a readable server time at least `FINISH_TRAINING_GRACE` (60 seconds) before the before-snapshot's `observed_at`, so it never pays to speed a training up (the grace covers a local clock running a little ahead of the server's; see below) | judged by time, not by whether an end is set: the training had ended before the call, no training is running after, and the crew member's `stat_total` rose. It does not matter whether the server clears the end, rewrites it to the finish time, sets a placeholder or leaves the ended training in place. No gain is `verified` only when the record before the call shows the member at `@TrainingCapacity`. The same running training, the ended training left in place with the same stats, or no training before the call, is `not-observed`; stats falling, starbux falling (a due finish spends none), a running training after that was not there before, or the member gone are `contradicted`; stats unobserved on either side, an unreadable end, a training still running before the call, or no gain with no capacity shown, is `unknown` |
| `use_consumable` | `useConsumable` (params `item_design_id`, `character_id`) | not-idempotent | inventory and crew observed, crew member present, numeric `item_design_id` (as for `add_training`), the item in exactly one inventory record with at least one held | the item's inventory quantity fell by exactly 1 (to fewer, to 0, or out of the inventory). Unchanged is `not-observed`, even if fatigue or stats changed (both are named in the reason when observed, but never decide it); the member gone is `contradicted`; a fall of more than 1, a rise (an item may arrive at the same time, which must not halt the account), the item in several inventory records on either side, the item absent from a `partial` inventory, an unreadable quantity, or inventory missing on either side give `unknown` |
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
- `add_training` and `finish_training` are observed from `@TrainingEndDate` and `@TrainingDesignId`. A crew record without `@TrainingEndDate` leaves training unobserved, so `add_training` refuses to start and both reconcile as `unknown`.
- Training state is judged by time, against each snapshot's `observed_at`: a training is running while its end is later, and ended once its end is at or before it. An ended training stays on the record until it is collected, and a past end with no reported design (for example `0001-01-01T00:00:00`) is an idle member's placeholder, not a training.
- `finish_training` reads the nine stat improvement attributes from the character record. Training capacity is a per-design value (`listAllCharacterDesigns2`) that the snapshot reads do not fetch, so `@TrainingCapacity` is normally absent and a finish that shows no stat gain is `unknown`. If the stat attributes are missing from live records, every finish is `unknown`.
- Whether the server clears a training on its own when it ends is unverified. If it does, `@TrainingEndDate` is empty (or a placeholder) by the time the before-snapshot is read and the `finish_training` precondition never passes. What `FinishTraining` leaves on the record (an empty end, the finish time, a placeholder, or the ended training) is also unverified; the postcondition accepts any of them. If the server leaves the ended training with its design in place, the precondition keeps passing on the finished member, so each run sends a `finish_training` that reconciles as `not-observed` once the stats stop changing (safe to retry; the training has ended, so no speed-up is paid).
- The training end is compared with the before-snapshot's `observed_at`, which comes from the local clock. The precondition asks for the end to be at least `FINISH_TRAINING_GRACE` (60 seconds) before it, which covers a local clock up to a minute ahead of the server's. A clock further ahead could let a finish run before the training ends; whether `FinishTraining` then fails or charges starbux is unverified, but a starbux fall after the call reconciles as `contradicted`.
- `use_consumable` is `verified` only when the item's quantity fell by exactly 1. A larger fall (a concurrent use, or a reward claimed with the item) and a rise (an item arriving at the same time) are `unknown`, so neither halts the account as `contradicted` would.
- `useConsumable` finds the item instance in the client's cached `itemsOfAShip`, which the runner's before-read refreshes. Snapshots key the inventory by item design, the last record of a design winning, so several stacks of one design would make the observed quantity unreliable; the precondition refuses such a design and the postcondition reports it `unknown`.
- When an execute run sets any cap, its gate (`sdk/fleet/factory.py`, the not-idempotent cost check near line 1133) refuses a not-idempotent, non-battle action with no planned cost, so it refuses `add_training` and `use_consumable` whenever caps exist. Their only cost is the training slot or the item, not minerals, gas or starbux, so the gate cannot plan one. Wave 2 must solve this (for example an explicit, journaled allowance for actions whose only cost is an inventory item or a crew slot) before either can run under caps. `finish_training` is safe to retry and is not refused for that reason. Neither new action is a canary.
- No read-only `Client` method is verified to return current trophies or battle history (`findUserRanking` and `listPvPBattles2` are untested and their response schemas are unknown). Until one is, snapshots from `ClientSnapshotProvider` carry no trophies, so `run_pvp_battle` outcomes stay `unknown` and the evidence tier stops at `finalised` at best. A trophy change can also come from a defense in the same interval.
- Resource decreases from concurrent spending (another action, or a player) would show as `contradicted` for collections. The runner should reconcile with no other actions in flight on the same account.
