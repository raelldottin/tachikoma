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
- **Training is sometimes unobservable.** A crew record without `@TrainingEndDate` leaves training unobserved, so `add_training` reconciles as `unknown`. [action-catalog.md](action-catalog.md#known-gaps)
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
- **baseline-v1 covers part of the daily routine.** It does not model training. Flying starbux, task rewards, mining drones, market, messages and crew upgrades are outside the plan. [progression-policy.md](progression-policy.md#differences-from-runpy)
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

- **Concurrent mode is library-only.** `ConcurrentFleetOrchestrator` runs accounts in parallel with the sequential guarantees, but `run.py` does not start it. A slow login delays every later account's first snapshot, because claims are decided in alias order. [fleet-accounts.md](fleet-accounts.md#orchestration)
- **No live account wiring.** The registry names a credentials secret, but only the simulation supplies an account context. Nothing in `run.py` starts a fleet run. [fleet-accounts.md](fleet-accounts.md#orchestration)
- **The game identity check is inactive by default.** Snapshots can carry a salted fingerprint of the logged-in user and profiles can declare the expected one, but nothing in `run.py` supplies a salt yet, so only alias checks apply. An account that declares no fingerprint and logs in as an undeclared game account is not caught. [fleet-accounts.md](fleet-accounts.md)
- **Uncertain attempts are charged at planned cost.** When an attempt's effect is not observed, its planned cost counts against the budget (`<kind>_uncertain`), which can over-count if nothing was spent. [fleet-accounts.md](fleet-accounts.md#orchestration)
- **No starbux spending.** A fleet run never allows starbux spend, because the policy cannot cap a starbux amount. [fleet-accounts.md](fleet-accounts.md#orchestration)
- **Time saved needs operator input.** `time_saved_minutes` is reported only when the operator supplies both manual and operator minutes for an account. None have been supplied. [fleet-report.md](fleet-report.md)
- **The audit sees only journals.** It checks journaled events and budgets. Actions taken outside the journal, such as by the legacy daily run, are invisible to it. [fleet-audit.md](fleet-audit.md)

## Experiments and simulation

- **Decisions are not applied.** The evaluation loop records `adopt` or `reject` but never changes an account. Rolling out a decision is a manual step. [experiment-loop.md](experiment-loop.md)
- **Simple statistics.** Proportions use a 95% Wilson interval and means a 95% Student-t interval, and a zero-width interval never decides. Both assume independent samples; serially correlated daily figures make intervals too narrow. [experiment-loop.md](experiment-loop.md#samples)
- **The simulated game is not the real game.** `SimulatedGame` models six catalog methods with seeded random amounts. Training always fails. Simulation shows that code paths work together, not how the game responds. [simulation.md](simulation.md#simulated-game)
- **Trophies exist only in simulation.** Battle outcomes are observable in scenarios but not against the live client. [simulation.md](simulation.md#simulated-game)
- **The hull-reserve variant rarely applies.** `hull-reserve-v1` needs hull design costs that nothing passes to the planner yet, and every owned crew member at the level cap, so it falls back to `baseline-v1`. Its `@MineralCost` field name is unverified. [progression-policy.md](progression-policy.md)
- **Faults are simulated, not observed.** The fault-injection scenarios show the safety rules hold against the simulated game; they do not show how often each fault happens live. [simulation.md](simulation.md)

## Operations

- **No live execute path.** The scheduler is reachable from `run.py` only through `--progression-dry-run`, which logs in, makes read-only calls and journals dry-run events. There is no live-execute flag. [daily-scheduler.md](daily-scheduler.md#scope)
- **Reports are offline only.** `run.py --report` builds progression, PvP and fleet reports from journal files without logging in. Nothing produces them from a live run yet. In that mode fleet events are grouped by alias, so the audit's `foreign-event` rule cannot fire. [README](../../README.md#offline-reports)
- **Dashboard and alerts are offline.** `run.py --report dashboard --output PATH` writes a static HTML page from journal files, and `sdk/reports/alerts.py` evaluates alert rules, but nothing sends alerts or runs them on a schedule. [alerts.md](alerts.md)
- **The compatibility ledger flags everything against 1.0.** No entry has strong evidence on the current game version; most have no recorded version at all. [compatibility-ledger.md](compatibility-ledger.md)
- **The study has no data.** The study design and sample-size planning exist, but no manual baseline, test account or window has been supplied. [study-design.md](../experiments/study-design.md)
- **The legacy daily run is separate.** `daily-run.yml` runs the legacy `run.py` sequence with `--run-battle` three times a day for the accounts in repository secrets. Its actions do not go through the runner, are not reconciled and are not journaled. [daily-run.yml](../../.github/workflows/daily-run.yml)
- **The only live claim is old and unversioned.** The battle chain (`CreateBattle9`, `AcceptBattle5`, `FinaliseBattle15`) is `live-claimed` from README notes, with no recorded game version. [capability-inventory.md](capability-inventory.md#r3-pvp-automation-engine)
- **Windows CI is partial.** Windows runs a portable smoke subset, not the full suite. [workflows README](../../.github/workflows/README.md#validation-coverage-and-dependencies)
