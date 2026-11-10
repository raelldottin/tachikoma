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
| `add_training` | `addTraining` | not-idempotent | crew observed, crew member present | never yet: snapshots do not observe training state, so the result is always `unknown` |
| `upgrade_room` | `upgradeRooms` | not-idempotent | rooms observed, room present and not upgrading | room status became `Upgrading` or its design advanced |
| `add_research` | `addResearch` | not-idempotent | research observed, not already researching or completed | research state became `Researching` or `Completed` |

`tests/test_action_catalog.py` checks that every catalog method is listed as `mutating` in the [capability inventory](capability-inventory.json), and covers each outcome for each action.

## Known gaps

- `Client.upgradeRooms` upgrades every eligible room and takes no room id. The spec reconciles one `room_id`, so an executor must either add a targeted client call or reconcile every room it touched.
- `add_training` stays `unknown` until training state (for example `@TrainingEndDate`) is added to `AccountSnapshot`.
- Resource decreases from concurrent spending (another action, or a player) would show as `contradicted` for collections. The runner should reconcile with no other actions in flight on the same account.
