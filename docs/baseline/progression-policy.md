# Progression policy (R2 / AUTO-005)

`sdk/policy/` turns one [account snapshot](account-snapshot.md) plus static design data into a `Plan`: an ordered list of [catalog](action-catalog.md) `ActionRequest`s, and every candidate it rejected with the reason. Policies are pure and versioned by name. Design data (`DesignData.from_records` parses the client's `RoomDesign` and `ResearchDesign` records) is passed in, never fetched.

## baseline-v1

`baseline-v1` reproduces `run.py`'s current daily order as far as the catalog can express it. That gives automated runs today's behaviour as their comparison baseline, not an invented strategy.

1. **Research** (`Client.upgradeResearches`): for each research line (`@RootResearchDesignId`), the first design that is neither researching nor completed. A line with research already in progress is skipped. Starbux-priced research is rejected unless `allow_starbux_spend=True`.
2. **Room upgrades** (`Client.upgradeRooms`): each room whose design has an upgrade (`@UpgradeFromRoomDesignId`) and is not already upgrading. Every admitted upgrade is followed by `collect_all_resources`, as `upgradeRooms` collects after each request.
3. **Daily reward** (`collect_daily_reward`).

Rules applied to every candidate:

- The action's catalog preconditions must hold on the snapshot. A section that wasn't observed yields no action, only a rejection.
- Costs (`mineral:N` or `gas:N`) are checked against the observed balance minus `mineral_reserve` / `gas_reserve`, and deducted cumulatively across the plan. An unobserved balance blocks priced actions.
- `max_mineral_spend` / `max_gas_spend` (default `None`, no cap) cap the total planned spend of each resource. They're applied to the snapshot the plan is made from, so each candidate must fit both `balance - reserve` and `cap - already planned`. The [fleet orchestrator](fleet-accounts.md#orchestration) passes remaining budgets here.
- An unknown cost is never treated as free. A candidate is rejected with `cost unknown: ...` when a room upgrade has no `@PriceString` or one that isn't `kind:N` with a whole non-negative `N` (for example `mineral:1,500`), when research has no parseable `@GasCost`, or when research has an `@StarbuxCost` that doesn't parse. A *missing* `@StarbuxCost` counts as 0: gas-priced research carries no starbux price, and `Client.upgradeResearches` reads it the same way.
- `max_requests` truncates the plan, and the dropped requests are listed as rejections.

## Differences from run.py

- **Training is not modelled by baseline-v1** (nor by fleet-v1 and hull-reserve-v1, which build on it). Every such plan carries an `add_training` rejection saying so. [training-v1](#training-v1) models it.
- **Spend is deducted cumulatively.** `run.py` re-checks its last collected totals, while baseline-v1 subtracts planned spend, so it is slightly more conservative within one plan.
- **No concurrency cap.** As in `run.py`, the server's concurrent-construction limit is discovered at execution time; the R2 scheduler stops on it.
- Steps outside the catalog (flying starbux, task rewards, mining drones, market and messages, crew upgrades) are not part of the plan.

## hull-reserve-v1 (experimental)

`hull-reserve-v1` models hull-upgrade timing as a strategy to test, not a rule. `baseline-v1` stays the default; nothing selects this variant unless a [registry](fleet-accounts.md) profile names it as its `policy_id`.

**Hypothesis (unproven).** Once every crew member is at the current hull's crew level cap, crew progress waits on the hull, so minerals are better saved for the next hull upgrade than spent on room upgrades that delay it. This partly cuts against the community advice recorded in the [PvP research digest](../pss-pvp-research-digest.md) (section 4.8), which is to max rooms *and* crew before upgrading the hull. No trial has tested it, and the plan says nothing about whether it helps.

**Near the next hull upgrade** (`hull_hold`) means all of the following hold on the snapshot and the design data:

1. The hull level (`@ShipLevel`) is observed and at least 1.
2. The crew section is `complete`, has at least one member, and every member's level is observed.
3. The next hull raises the crew level cap. The cap is `sdk.pvp.config.crew_level_cap`: hull level × 4, at most 40 (`sdk/crew_leveling.py`). From hull 10 the cap is already 40, so the variant never holds there.
4. Every crew member is at or above the current cap. This is the crew criterion of `sdk.pvp.scoring.hull_upgrade_readiness`. Its other inputs (room max levels for the hull, native AI rules) are not in an `AccountSnapshot`, so they're not used.
5. `DesignData.hulls` has a design for the next level, and every such design has the same parseable mineral cost.

**Inputs.** `DesignData.from_records(ship_designs=...)` reads `ShipDesign` records into `HullDesign(design_id, level, mineral_cost)` from `@ShipDesignId`, `@ShipLevel` and `@MineralCost`. `@MineralCost` is not used anywhere else in the repository yet, so treat that field name as unverified. A missing or unparseable value is `None`, never 0. `hulls` is empty by default. When it's empty, the next hull's cost is unknown and the variant falls back.

**Behaviour.**

- When the ship is near, the plan is `baseline-v1` with `mineral_reserve` raised to the next hull's mineral cost. Minerals above that reserve are still spent. A room upgrade that `baseline-v1` would make and the hold refuses keeps baseline-v1's reason and adds the cause, for example `needs 300 mineral, 0 available above reserve (held for the hull 4 upgrade: mineral reserve raised from 0 to its 1100 mineral cost)`. Research, gas-priced rooms, the daily reward, spend caps, `max_requests` and the starbux rule are unchanged.
- In every other case the requests are exactly `baseline-v1`'s, and one extra rejection, `hull_reserve: not applied, planned as baseline-v1: <why>`, gives the reason. The reason can be: not near (crew below the cap), an unobserved hull level or crew level, an incomplete crew section, no crew, a next hull that doesn't raise the cap, no next-level hull design, an unknown or ambiguous next-hull cost (the snapshot doesn't say which hull design the ship upgrades to), or a configured `mineral_reserve` that already covers the cost.

**Limitations.**

- Nothing passes ship designs yet. `run.py --progression-dry-run`, the fleet contexts and the shipped simulation scenarios build `DesignData` from room and research records only. Until a caller supplies `ShipDesign` records, the variant always falls back. The simulated game also reports hull 1 and no crew.
- The live snapshot provider reads crew with `listAllCharactersOfUser`, so every character the account owns counts. One unlevelled spare keeps the ship "not near". A looser criterion, such as counting only crew assigned to rooms, would need a new versioned name, not a change to this one.
- A hull upgrade already in progress, and starbux-priced hulls, are not observed. Starbux is never spent either way.

**Comparing with baseline-v1.** Give comparable accounts `baseline-v1` and `hull-reserve-v1` as their `policy_id`, or run one account under each for matched periods. Then record an `sdk.experiments.Experiment` with `baseline="baseline-v1"`, `candidate="hull-reserve-v1"` and a stated metric. Good metrics are days from "crew capped" to an observed `hull_level` increase, and crew levels or trophies gained per day after it. Use [progression metrics](progression-metrics.md) such as `gained_per_hour` as a guard against starving the economy. Build `MeanSample`s from the per-period values and call `decide`. Only `adopt` with both intervals clear of each other supports switching. Until then, the variant is a hypothesis. See the [experiment loop](experiment-loop.md).

## training-v1

`training-v1` is `fleet-v1` followed by crew training. `sdk/policy/training.py` plans it with the pure planner in `sdk/training` (`plan_training` and the owner-approved `DEFAULT_STRATEGY`) from the same snapshot. Nothing selects it unless a [registry](fleet-accounts.md) profile names it as its `policy_id`. `--fleet-execute` needs a stamped dry run with the same policy id, so an operator must dry-run `training-v1` (and read its training block and rejections) before it can execute. fleet-v1 accounts, their dry-run stamps and the simulation golden digests are unchanged.

**Plan order.**

1. fleet-v1's requests, unchanged. `run.py` manages training last, and putting training last means the account's own economy requests run before any training request on the day. It does not contain a training fault: an uncertain start or consumable use stops the account and, under the default halt share, the fleet, every day until reconciled (see [known limitations](known-limitations.md)).
2. Every `finish_training`, in crew order. It is safe to retry, so stat gains are banked before a start (not idempotent) can stop the scheduler.
3. Every `add_training` and `use_consumable`, in crew order.

`max_requests` cuts the whole plan, so training is cut first. Under the orchestrator `max_requests` is the profile's `max_actions`: raise it on training-v1 accounts so the training block fits.

**Rules.**

- **Clock.** The plan is made at `snapshot.observed_at`, never the wall clock.
- **Deferral.** After a finish, the planner adds an advisory decision for the same crew, planned from the stats *before* the finish, so it may name the wrong tier. A once-per-run plan cannot see the state after the finish, so that decision is dropped and recorded as a rejection (`deferred to the next run: ...`). The next daily run plans it from fresh stats. Each crew member therefore trains once every two daily runs.
- **Plan-time checks.** Every training request goes through the catalog's preconditions on the snapshot, and a failure is a rejection. For example, a finish is planned only when the training ended at least 60 seconds before the observation, so it never pays to speed one up. A start needs an idle crew member. A consumable must be held in exactly one inventory record.
- **Skips.** Every crew member the planner skips gets a `training <id>` rejection with the reason code and detail, for example `training 105: unknown_role: 'Ada' has no training role`.
- **Fail closed.** A crew member whose training state, any of the nine stats, or fatigue was not observed, or whose id is not a positive whole number, is left out with a `training <id>` rejection. The parser would otherwise read each gap as a value (idle, 0 or dropped). No crew section, or no `TrainingDesign` or `CharacterDesign` records in the design data, plans no training and gives the reason as a `training` rejection. The snapshot is rebuilt into raw records and parsed again by `sdk.training.parse`, so a training end date that parser rejects (even one `sdk.state` accepts) skips that crew member.
- **Training errors.** A `ValueError` from `sdk.training` while planning (for example a custom strategy that rejects a consumable the item designs name) plans no training and is a `training` rejection (`training not planned: ValueError: ...`). fleet-v1's requests are kept.
- **Cost.** `add_training` and `use_consumable` cost no minerals or gas, so they carry an explicit zero (`{"minerals": 0, "gas": 0}`). The [execute gate](action-catalog.md) runs them under caps only with that declaration. `finish_training` plans no cost. No request ever names starbux.
- **Consumables.** `DEFAULT_STRATEGY` maps no consumables, so the item designs' consumables are attached (`parse_consumable_designs`, `with_consumables`). A live `use_consumable` still has to be listed in the authorization file, and the dry run shows it first.

**Design data.** `DesignData.from_records(training_designs=..., character_designs=..., item_designs=...)` keeps those records as raw `DesignRecord`s for the planner's parser, which is the single authority on their meaning. Character designs supply the training capacity and rarity, which the snapshot doesn't carry. They are empty by default, so other callers are unchanged. The live fleet path (`read_design_data`) fills them from the `listAllDesigns4` reply it already reads, but only for an account whose policy models training; for other accounts they stay empty. For such an account whose policy models training, a TrainingDesign or CharacterDesign list that reply did not give is read with `listAllTrainingDesigns2` or `listAllCharacterDesigns2` (read-only, the reads `run.py`'s training uses). A list that still gives none stays empty with an account note, and training-v1 plans no training and says why. Item designs come only from `listAllDesigns4`, so without it no consumable is planned. A research design reaches the parser as its id, root and name only: its level is the name's course number ("Fitness 203" is course 203 of the Fitness line, `sdk.training.parse.research_course`), as in the live `ListAllResearchDesigns2` reply, which carries no level attribute. The band research gates need Fitness or Education course 101 (primary T1), 201 (T2) or 203 (T3); secondary T1 is ungated. Simulation scenarios pass them in their `design` block.
