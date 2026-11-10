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

`tests/test_action_catalog.py` checks that every catalog method is listed as `mutating` in the [capability inventory](capability-inventory.json), and covers each outcome for each action.

## Known gaps

- `Client.upgradeRoom` sends one `UpgradeRoom2` request and does no eligibility or cost checks. The caller (the R2 policy) decides which room to upgrade and to which design. `upgradeRooms` still selects rooms exactly as before and delegates each request to it. It returns the raw HTTP response, so the runner treats the response as ambiguous and only observed state verifies the upgrade.
- `add_training` is observed from `@TrainingEndDate` and `@TrainingDesignId`. A crew record without `@TrainingEndDate` leaves training unobserved, and the outcome is `unknown`.
- Resource decreases from concurrent spending (another action, or a player) would show as `contradicted` for collections. The runner should reconcile with no other actions in flight on the same account.
