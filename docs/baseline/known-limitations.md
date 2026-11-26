# Known limitations


**As of:** 2026-11-11, `main` after #149.

This page collects the gaps recorded in the baseline docs in one place. Every R0–R6 module on `main` has been tested only offline, with mocked game traffic or the simulated game. No roadmap module has been run against a live account, and no live result has been recorded. Each item links to the doc that describes it in full.

## Live trials are blocked

Every release's exit trial needs live runs on owned test accounts. The owner has not authorized them, so all five slices are `blocked` in [the queue](../../automation/queue/slices.json):

| Slice | Release | Waiting on |
| --- | --- | --- |
| `r2-live-progression-trial` | R2 | Authorization for live runs on a named test account, a defined manual routine and a comparison window |
| `r3-live-pvp-trial` | R3 | Authorization for live PvP on a named test account, the two strategies and their windows |
| `r4-live-market-trial` | R4 | Sanitized current captures of `SellItem3` and `Purchase4`, authorization for live trades, and a defined experiment |
| `r5-live-fleet-trial` | R5 | At least two named test accounts with provisioned credentials, each account's manual routine minutes, and authorization |
| `r6-study-report` | R6 | The four trials above, run and recorded |

Until these run, no exit criterion that needs live evidence is met. Account growth velocity, operator time saved, win rates, market return and fleet throughput have no real figures.

## State and reads

- **Field names are unverified.** The snapshot reads attribute names taken from existing client code and mocked tests. Whether they match the current live server has not been checked. [account-snapshot.md](account-snapshot.md#field-sources)
- **No trophies or starbux in live snapshots.** `ClientSnapshotProvider` leaves the `profile` section `missing`, because the client only holds those values from login and they would be stale. [account-snapshot.md](account-snapshot.md#live-provider-r2)
- **No verified trophy or battle-history read.** `findUserRanking` and `listPvPBattles2` are untested and their response schemas are unknown. [action-catalog.md](action-catalog.md#known-gaps)
- **Training is sometimes unobservable.** A crew record without `@TrainingEndDate` leaves training unobserved, so `add_training` refuses to start (it needs an idle member: nothing running and no ended training left uncollected) and both `add_training` and `finish_training` reconcile as `unknown`. Training state is judged by its end time against the snapshot's observation time, not by whether an end is set. Training capacity is a per-design value the snapshot reads do not fetch, so a `finish_training` that shows no stat gain is `unknown` (a training at capacity gains nothing). The training end is compared with the local clock: `finish_training` waits until the end is at least 60 seconds past, which covers a local clock up to a minute ahead of the server's, and a starbux fall after a finish reconciles as `contradicted`. A clock further ahead could still let a finish run early. [action-catalog.md](action-catalog.md#known-gaps)
- **One consumable use, one item.** `use_consumable` is `verified` only when the item's quantity fell by exactly 1. A larger fall or a rise (an item arriving at the same time) is `unknown`, not `contradicted`. [action-catalog.md](action-catalog.md#known-gaps)
- **Training and consumable actions run under execute caps only with an explicit zero.** The execute gate refuses a not-idempotent action with no planned cost whenever any cap is set. `add_training` and `use_consumable` are the only exceptions, and only when the request declares `0` for every capped kind (training-v1 does). Without that declaration they are refused like every other uncosted not-idempotent action. [action-catalog.md](action-catalog.md#known-gaps)
- **An uncertain training request stops the account, and the fleet, every day until reconciled.** An `add_training` or `use_consumable` (both not idempotent) whose outcome is `unknown` (for example a call that raised before the effect was observed) stops the account. training-v1 plans the same request again on every later run, and the runner refuses it until an operator reconciles the uncertain attempt, the same as any other not-idempotent action. The scheduler stops at that refusal, so the account counts as stopped each day. With the default halt share of 0.0, one stopped account halts the fleet for that day: accounts not yet started do not run, and running accounts stop before their next action. Plan order does not contain this to the training block; it only means the stopped account's own economy requests, which come first, ran before the refusal. Reconciliation is the operator step that clears it. [progression-policy.md](progression-policy.md#training-v1), [fleet-accounts.md](fleet-accounts.md)
- **Training-v1 re-parses the snapshot.** training-v1 rebuilds raw records from the snapshot and parses them again through `sdk.training.parse`. A training end date in a format that `sdk.state` accepts but the training parser rejects makes the planner skip that crew member (fail closed), so it is not trained until the formats agree. [progression-policy.md](progression-policy.md#training-v1)
- **Live training design attributes are unverified.** training-v1 reads training designs through `sdk.training`'s parser, which assumes `@RequiredRoomType`, the `@<Stat>Chance` attributes, `@Rarity` and `@TrainingCapacity`. These have not been checked against a live reply. The required stamped dry run shows what would be planned before anything executes. [progression-policy.md](progression-policy.md#training-v1)
- **No consumables without `listAllDesigns4`.** The live fleet path takes training, character and item designs from the `listAllDesigns4` reply it already reads (deprecated, and often empty). For a training-v1 account it reads missing training and character designs with `listAllTrainingDesigns2` and `listAllCharacterDesigns2`, but item designs have no such fallback, so without that reply training-v1 plans no consumable use. When the training or character reads also fail, training-v1 plans no training and says why. [progression-policy.md](progression-policy.md#training-v1)
- **Many reads are untested.** The R0 inventory found 17 untested reads in pillar A, such as achievements, star systems and research level lookups. [capability-inventory.md](capability-inventory.md#r1-reliable-game-state-and-execution)
- **Inventory summary counts are dated.** The capability inventory's JSON is kept current by its drift test, but the per-pillar summary counts were last computed for `main` after #91. [capability-inventory.md](capability-inventory.md)

## Actions and battles

- **Room upgrades are unchecked.** `Client.upgradeRoom` sends one `UpgradeRoom2` request with no eligibility or cost checks and returns the raw response. Only observed state can verify it. [action-catalog.md](action-catalog.md#known-gaps)
- **Concurrent activity confuses reconciliation.** Spending by another action or by a player during a collection shows as `contradicted`. Run with nothing else in flight on the account. [action-catalog.md](action-catalog.md#known-gaps)
- **Live battle outcomes are always `unknown`.** Without a trophy read, the evidence tier stops at `finalised` at best. A finalise response is never treated as a win, because the client chooses the outcome it sends. Even with trophies observed, an outcome counts only if the call reached `FinaliseBattle15`. [action-catalog.md](action-catalog.md#battle-evidence-tiers)
- **Trophy changes are not exclusive to attacks.** A defense during a finalised attack can also move trophies, and the change is credited to the attack. [action-catalog.md](action-catalog.md#known-gaps)
- **No opponent cohorts.** No journaled event records opponent facts, so every hull and trophy cohort is `unknown`. [pvp-battle-results.md](pvp-battle-results.md#rules)
- **No battle duration.** The journal does not record it, so `mean_duration_s` is always `None`. [pvp-scorecard.md](pvp-scorecard.md#scorecard)
- **Strategies are untested hypotheses.** The two candidate strategies have no battle evidence, and nothing applies a strategy to a ship. [pvp-strategies.md](pvp-strategies.md#candidate-strategies)
- **Strategy comparison needs data that does not exist.** `compare_strategies` needs 30 known-result battles per strategy by default. Live scorecards will show counts with `n/a` rates. [pvp-scorecard.md](pvp-scorecard.md#what-this-cannot-show-yet)
- **baseline-v1 covers part of the daily routine.** It does not model training, and neither does fleet-v1; training-v1 does. Market, messages and crew upgrades are outside every plan. [progression-policy.md](progression-policy.md#differences-from-runpy)
- **training-v1 trains each crew member once every two daily runs.** The plan is made once per run, so after a finish the next start waits for the next run (deferral). Its training requests also count toward `max_actions`, which cuts training first. [progression-policy.md](progression-policy.md#training-v1)
- **No construction cap in planning.** The server's concurrent-construction limit is found at execution time, and the scheduler stops on the resulting `unknown` outcome. [daily-scheduler.md](daily-scheduler.md#safety-rules)
- **No automatic retry.** The runner calls each action once. A non-idempotent action left `unknown` blocks the same action until an operator reconciles it by hand. [action-runner.md](action-runner.md)

## Economy

- **No market trades.** `SellItem3` and `Purchase4` are not implemented as `Client` methods, so no trade can be made or reconciled live. [economy-market.md](economy-market.md#limits)
- **Listing fields are mostly unverified.** Only `@Message` and `@ActivityArgument` appear in fixtures. `@SaleId`, `@ItemDesignId` and `@Quantity` are named from prior capture analysis. [economy-market.md](economy-market.md#limits)
- **A listing is not a trade.** A listing shows an offer, never a sale, purchase or transfer. A listing without a sale id leaves a trade at `requested`. [economy-trades.md](economy-trades.md#sell-states)
- **The fee bound is an assumption.** Trade reconciliation accepts fees up to 25% of the price by default (`max_fee`). The game's real market fee is unverified, and sale proceeds must arrive after the listing is seen gone, so a sale paid before that poll reconciles as `unknown`. [economy-trades.md](economy-trades.md)
- **Market return has no real data.** The economy report computes market return only from completed trades, and none can exist yet. [economy-report.md](economy-report.md)
- **Attribution can be ambiguous.** A ledger entry with several candidate actions, including one that started inside the interval and ran past its end, is `ambiguous`. A change in the gap between two runner runs stays `unattributed`. [economy-ledger.md](economy-ledger.md#attribution)
- **Starbux is unobserved live.** Live snapshots do not report starbux, so ledger starbux changes from them are `None`. [account-snapshot.md](account-snapshot.md#live-provider-r2), [economy-ledger.md](economy-ledger.md)

## Fleet

- **Concurrent mode has not run live.** `--fleet-dry-run --concurrent N` and `--fleet-execute --concurrent N` start `ConcurrentFleetOrchestrator`; execution needs an owner authorization (see [SPEC-05](../specs/SPEC-05-live-execution-gate.md)) and has only been tested with fakes. A slow login delays every later account's first snapshot, because claims are decided in alias order. [fleet-accounts.md](fleet-accounts.md#orchestration)
- **Live execution is gated and untried.** `run.py --fleet-execute` executes only under a dated, scoped owner authorization file, after a fresh dry run, with mandatory identity checks; canary mode runs one safe action per account. It has never been run against a real account. See [SPEC-05](../specs/SPEC-05-live-execution-gate.md). [fleet-accounts.md](fleet-accounts.md#orchestration)
- **The game identity check is inactive by default.** Snapshots can carry a salted fingerprint of the logged-in user and profiles can declare the expected one, and `run.py --fleet-dry-run` supplies a salt only when `--identity-salt-env` is given; without it only alias checks apply. An account that declares no fingerprint and logs in as an undeclared game account is not caught. [fleet-accounts.md](fleet-accounts.md)
- **Uncertain attempts are charged at planned cost.** When an attempt's effect is not observed, its planned cost counts against the budget (`<kind>_uncertain`), which can over-count if nothing was spent. [fleet-accounts.md](fleet-accounts.md#orchestration)
- **No starbux spending.** A fleet run never allows starbux spend, because the policy cannot cap a starbux amount. [fleet-accounts.md](fleet-accounts.md#orchestration)
- **Time saved needs operator input.** `time_saved_minutes` is reported only when the operator supplies both manual and operator minutes for an account. None have been supplied. [fleet-report.md](fleet-report.md)
- **The audit sees only journals.** It checks journaled events and budgets. Actions taken outside the journal, such as by the legacy daily run, are invisible to it. [fleet-audit.md](fleet-audit.md)

## Experiments and simulation

- **Decisions are not applied.** The evaluation loop records `adopt` or `reject` but never changes an account. Rolling out a decision is a manual step. [experiment-loop.md](experiment-loop.md)
- **Simple statistics.** Proportions use a 95% Wilson interval and means a 95% Student-t interval, and a zero-width interval never decides. Both assume independent samples; serially correlated daily figures make intervals too narrow. [experiment-loop.md](experiment-loop.md#samples)
- **The simulated game is not the real game.** `SimulatedGame` models six catalog methods with seeded random amounts, plus `finishTraining` and item use. Crew training uses simplified rules: one stat per training design, fatigue that recovers linearly at an assumed rate, and rooms that qualify by design lineage, with room level and room capacity ignored. training-v1 trains and finishes in the simulator, but the shipped scenarios do not fit `DEFAULT_STRATEGY` (non-numeric ids, crew names outside its roles), so they only show fatigue recovering. `SimulatedGame` lacks the fleet-v1 collection methods (`grabFlyingStarbux`, `collectTaskReward`, `collectAvailableMarkers`, `collectMiningDronesWithTravel`), so a fleet-v1 or training-v1 `run_scenario` needs them stubbed. A session loaded from a scenario is not re-checked against its room or research. The `add_training` postcondition reports `verified` when a refused request names the design the crew member is already training, so a training policy must only plan for idle crew. Simulation shows that code paths work together, not how the game responds. [simulation.md](simulation.md#crew-training)
- **Trophies exist only in simulation.** Battle outcomes are observable in scenarios but not against the live client. [simulation.md](simulation.md#simulated-game)
- **The hull-reserve variant rarely applies.** `hull-reserve-v1` needs hull design costs, which only `--fleet-dry-run` passes to the planner (from the deprecated `listAllDesigns4`, so usually none), and every owned crew member at the level cap, so it falls back to `baseline-v1`. Its `@MineralCost` field name is unverified. [progression-policy.md](progression-policy.md)
- **Faults are simulated, not observed.** The fault-injection scenarios show the safety rules hold against the simulated game; they do not show how often each fault happens live. [simulation.md](simulation.md)

## Operations

- **Live execution exists only behind an authorization.** `--fleet-execute` is the only path that executes actions (see above); `--progression-dry-run`, `--fleet-dry-run` and `--baseline` never do. [daily-scheduler.md](daily-scheduler.md#scope)
- **Reports are offline only.** `run.py --report` builds progression, PvP and fleet reports from journal files without logging in. Nothing produces them from a live run yet. In that mode fleet events are grouped by alias, so the audit's `foreign-event` rule cannot fire. [README](../../README.md#offline-reports)
- **Alerts run only after an execute run.** `--fleet-execute` evaluates alerts, writes a dashboard and can e-mail one scrubbed summary through the existing SMTP flags; nothing runs on a schedule (SPEC-11 R5 is the owner's). [alerts.md](alerts.md)
- **The compatibility ledger flags everything against 1.0.** No entry has strong evidence on the current game version; most have no recorded version at all. [compatibility-ledger.md](compatibility-ledger.md)
- **The study has no data.** The study design and sample-size planning exist, but no manual baseline, test account or window has been supplied. [study-design.md](../experiments/study-design.md)
- **The legacy daily run is separate.** `daily-run.yml` runs the legacy `run.py` sequence with `--run-battle` three times a day for the accounts in repository secrets. Its actions do not go through the runner, are not reconciled and are not journaled. [daily-run.yml](../../.github/workflows/daily-run.yml)
- **The only live claim is old and unversioned.** The battle chain (`CreateBattle9`, `AcceptBattle5`, `FinaliseBattle15`) is `live-claimed` from README notes, with no recorded game version. [capability-inventory.md](capability-inventory.md#r3-pvp-automation-engine)
- **Windows CI is partial.** Windows runs a portable smoke subset, not the full suite. [workflows README](../../.github/workflows/README.md#validation-coverage-and-dependencies)
