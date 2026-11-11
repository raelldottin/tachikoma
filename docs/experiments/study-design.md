# Manual-vs-automation study design (R0, AUTO-010)

This is the protocol for comparing manual play with Tachikoma's automation, as [roadmap §6](../PRODUCT_ROADMAP.md#6-experimental-design) asks. It fixes the arms, the metrics, how to compare ships of different hull levels, what to record, the windows and when to stop, before any trial starts. `sdk/experiments/study.py` turns a plan into the minimum sample each arm needs.

## Status: no baseline data exists

Nothing in this design has been run. No account has a measured baseline, so every planning value below (a control rate, a mean, a standard deviation, an effect worth detecting) is an assumption. Every module the study relies on has been tested only offline ([known limitations](../baseline/known-limitations.md)). Before a trial can start, the owner must supply the items in [What the owner must supply](#what-the-owner-must-supply). The first live window must be a measurement-only baseline window, and the plan must be re-sized from what it measures.

## Comparison modes (arms)

An arm is one way of operating an account for a window. `StudyPlan.arms[0]` is the control. Every other arm is compared with it, one `Experiment` per candidate (`experiments_for(plan)`).

| Arm | What runs | What measures it | State on `main` |
| --- | --- | --- | --- |
| `manual` (control) | The owner's documented daily routine, played by hand | Read-only snapshots at fixed times each day (`ClientSnapshotProvider`), and the owner's own minutes log. Manual actions are not journaled. | Snapshot reads exist; no routine has been written down |
| `baseline-v1` (rule-based) | `DailyScheduler` with the `baseline-v1` policy: research, room upgrades, daily reward | The gameplay journal, plus the same daily snapshots as the manual arm | Dry run only. There is no live execute path (`r2-live-progression-trial` is blocked) |
| `adaptive-vN` (adaptive) | A policy version revised from earlier decisions. Each revision is a new arm with a new identifier. A revision happens only between windows, never inside one. | As for `baseline-v1` | No adaptive policy exists. `POLICIES` holds only `baseline-v1` |
| Fleet variants | Two or more accounts run by the orchestrator: sequential or concurrent, independent or shared planning, differentiated or common strategies | `compute_fleet_report` over every account's journal, with the audit | Sequential only. Concurrent operation is not implemented, so it cannot be an arm yet |
| PvP strategies | Strategy windows in an `ExperimentSchedule` | `battles_from_events` and `compute_scorecard` | Live battle outcomes are always `unknown`, so no win rate can be measured yet |

The legacy `daily-run.yml` workflow acts on the accounts in repository secrets three times a day, outside the journal. A study account must be removed from it for the whole study, or every arm is contaminated by actions nobody recorded.

## Studies

Each study has one primary metric. Running one decision per study keeps the error rates the plan states. Everything else is secondary and reported with its interval, not used to decide.

| Study | Arms | Primary metric | Sample kind | Unit |
| --- | --- | --- | --- | --- |
| A. Manual vs rule-based | `manual`, `baseline-v1` | Growth velocity: room upgrades plus research completed, per account-day, from snapshot diffs | Mean | `account-day` |
| B. Rule-based vs adaptive | `baseline-v1`, `adaptive-v1`, ... | Growth velocity, as in A | Mean | `account-day` |
| C. PvP strategy | two strategy versions | Attack win rate among known-result battles | Proportion | `battle` |
| D. Fleet operation | fleet variants | Verified actions per hour across the fleet | Mean | `fleet-day` |

Growth velocity counts only what `baseline-v1` can do. Crew levels are left out of the primary metric because `baseline-v1` does not train crew, so including them would measure that gap rather than automation. Crew levels gained is a secondary metric that shows the gap. The owner may choose a different primary metric, but must choose it before the window opens.

The primary metric must be measured the same way in every arm. Journal fields exist only for automated arms, so a manual-vs-automation primary metric must come from snapshots. That is why growth velocity in A uses `diff_snapshots` (room design changes and research state changes) and not journal outcomes.

## Metrics

Each [roadmap §7](../PRODUCT_ROADMAP.md#7-product-scorecard) metric, the existing field that measures it, and whether it can be compared with the manual arm.

| Scorecard metric | Existing field | Sample for `decide` | Comparable with manual | Measurable today |
| --- | --- | --- | --- | --- |
| Account growth velocity | Not computed by any report. Derived from `diff_snapshots(before, after)`: `rooms` design changes, `research` state changes, `crew` level changes | `MeanSample` of per-day counts | Yes, from snapshots | Offline only |
| Attack win rate | `ScorecardRow.attack.wins / known` (`RoleStats.win_rate`) | `ProportionSample(wins, known)` | Only if manual battles are logged by hand with the same evidence rules | No: live outcomes are `unknown` |
| Defense win rate | `ScorecardRow.defense` | `ProportionSample(wins, known)` | Same as attack | No |
| Net trophies | `ScorecardRow.net_trophies`, `trophy_samples` | `MeanSample` of per-day net change | Yes, if trophies are read at day boundaries | No: live snapshots leave `profile` missing |
| Resources per hour | `DayMetrics.gained_per_hour`, `EconomyRow.gained_per_hour` | `MeanSample` of per-day values | Yes, from snapshot resource deltas over the same day boundaries | Offline only |
| Market return | `EconomyRow.market_return` | `MeanSample` per completed trade | No manual market arm is planned | No: `SellItem3` and `Purchase4` are missing |
| Automation coverage | `AccountRun.verified / AccountRun.planned` from the fleet run | `ProportionSample(verified, planned)` | No: automation arms only | Offline only |
| Verified action success | `DayMetrics.verified_success_rate`, `Throughput.success_rate`; `samples_from_outcomes` | `ProportionSample(verified, attempted)` | No: automation arms only | Offline only |
| Time saved | `AccountFleetRow.time_saved_minutes` (`manual_minutes - operator_minutes`) | `MeanSample` of operator minutes per account-day | Yes: it is the manual arm's own log | No minutes have been supplied |
| Cost per progression unit | `DayMetrics.resources_spent`, `EconomyRow.spend_by_action`, `ScorecardRow.resources_spent`, divided by growth units | Ratio of totals, reported, not decided | Yes, from snapshots | Offline only |
| Incident/failure frequency | `DayMetrics.failed` and `failures`, `Throughput.failure_rate`, `FleetReport.incidents` | `ProportionSample(failed, attempted)` | No: automation arms only | Offline only |

Rules that apply to every metric:

- A value that was not observed is missing, never 0. A day without a snapshot at both boundaries contributes no growth value.
- Report the sample size, game version and data source next to every headline figure.
- Include failed tasks, losses and unknown outcomes in the counts. Unknown battle outcomes are counted, never treated as wins or losses.
- Automation-only metrics (coverage, verified success, incidents) describe the automated arms. They are not evidence that automation beats manual play.

## Allocation and hull levels

Hull level changes what a day of play can achieve: upgrades at higher levels cost more and take longer. Ships at different levels are not comparable unadjusted.

**One or two accounts: within-account crossover.** Each account is its own control. Alternate the arms in blocks of equal length in the order `manual`, `automated`, `automated`, `manual` (ABBA), so a steady trend in growth over the study falls equally on both arms. Seven-day blocks cover a weekly cycle. A four-block study gives 14 account-days per arm per account.

**Two or more accounts at the same level: matched pairs.** Pair accounts in the same hull bracket (`hull_bracket` in `sdk/reports/battles.py`: 1–4, 5–7, 8–10, 11–13, 14–20) and, for PvP, the same trophy bracket (`trophy_bracket`, 500-trophy bands). Assign the arm within each pair by a coin flip recorded before the window opens, then swap the arms in a second period.

**Adjustment where matching is imperfect:**

- Analyse each hull bracket separately. Claim an overall result only if every bracket that reaches the minimum sample points the same way. This is the rule `compare_strategies` already applies to opponent cohorts.
- When an account's hull level changes inside a block, split the block at the snapshot that first shows the new level. The rest of the block belongs to the new bracket.
- Report cost per growth unit next to growth velocity, so a higher-level ship's slower count is read against its larger cost per upgrade.
- Do not fit a regression adjustment across a handful of accounts. With so few units it would be an assumption presented as a result.

**Carry-over between blocks.** An upgrade or research started in one block can finish in the next. Credit each growth event to the block in which it started, using the in-progress state in the boundary snapshot. Where the start cannot be told from snapshots, leave out the first day of the new block (a washout day) and record that in the intervention log.

**Unit of analysis.** A unit must be roughly independent of the others. Account-days are serially correlated: a day spent saving resources makes the next day's upgrades likelier. That makes `decide`'s intervals too narrow ([known limitations](../baseline/known-limitations.md#experiments-and-simulation)). As a sensitivity check, also run `decide` on block means (one value per block), and report both. Battles are closer to independent, but battles against the same opponent are not.

## Sample size

`per_arm_min_sample(plan)` returns the number of independent units each arm needs, and `experiments_for(plan)` passes it to `Experiment.min_sample`. `decide` refuses to adopt or reject before every arm has that many.

The helpers use the normal approximation, with 95% two-sided and 80% power as the defaults:

- Two proportions: `n = [z(1-a/2) sqrt(2 pbar qbar) + z(1-b) sqrt(p1 q1 + p2 q2)]^2 / (p1 - p2)^2` with `pbar = (p1 + p2) / 2` (Fleiss, Levin and Paik, *Statistical Methods for Rates and Proportions*, 3rd ed., 2003, eq. 4.14, without continuity correction). `pooled=False` gives `(z(1-a/2) + z(1-b))^2 (p1 q1 + p2 q2) / (p1 - p2)^2` (Chow, Shao and Wang, *Sample Size Calculations in Clinical Research*, 2nd ed., 2008, §4.2).
- Two means: `n = (z(1-a/2) + z(1-b))^2 (sd1^2 + sd2^2) / delta^2` (Chow, Shao and Wang, §3.2.1).

Checks against standard values: 0.50 vs 0.60 needs 388 per arm (385 unpooled), and a mean difference of half a standard deviation needs 63 per arm (Cohen's tables give 64 using the t distribution).

**These formulas size a conventional test, and `decide` is not that test.** `decide` adopts only when the two 95% intervals do not overlap. With equal standard errors, that rejects at about z = 2.77 instead of 1.96. A trial sized by the conventional formula gives `decide` roughly a coin flip's chance of adopting a real effect: 63 per arm at half a standard deviation adopts about 49% of the time. So plans default to `decision_rule="decide-non-overlap"`, which sizes for `decide` itself:

- Means: the smallest `n` with `delta sqrt(n) >= t(n-1) (sd1 + sd2) + z(power) sqrt(sd1^2 + sd2^2)`, where `t(n-1)` is `decide`'s own `t_quantile_975`. A seeded simulation of `decide` adopts at the planned rate (tested).
- Proportions: start from the same condition with `sd = sqrt(p q)`, then search on the exact binomial probability that `decide`'s Wilson intervals separate (`non_overlap_proportion_power`), because the normal approximation runs one to three points of power short.

`decision_rule="two-sample-test"` uses the conventional formulas and splits alpha across the candidates (Bonferroni). It suits an analysis that runs its own test rather than `decide`.

Worked examples (per arm, 80% power):

| Planning assumption | Conventional test | `decide` rule |
| --- | --- | --- |
| Mean, effect = 0.5 sd | 63 | 107 |
| Mean, effect = 1 sd | 16 | 29 |
| Mean, effect = 1.5 sd | 7 | 14 |
| Proportion 0.50 → 0.60 | 388 | 645 |
| Proportion 0.50 → 0.70 | 93 | 154 |
| Proportion 0.70 → 0.85 | 121 | 199 |
| Proportion 0.90 → 0.97 | 194 | 310 |

What that means for the studies:

- **Study A on one account** detects only large effects. A four-block ABBA crossover gives 14 account-days per arm, enough for an effect of about 1.5 standard deviations. An effect of 1 sd needs 29 account-days per arm: five seven-day blocks per arm, ten in all.
- **Study C** is long. Detecting a win-rate change from 50% to 60% needs 645 known-result battles per strategy, more than 200 days at three battles a day. Only an effect of about 20 points (154 battles per strategy, about 50 days each at three a day) is within reach of a few months on one account.

## Interventions to record

Keep an intervention log for the study, one entry per event, written when the event happens. Each entry records the UTC time, the account alias, the arm active at that time, the kind of event, a description, and the action taken (continue, exclude the day, split the block, abort the arm). Entries use account aliases only, never e-mail addresses, credentials or game user ids.

| Kind | Examples | Default action |
| --- | --- | --- |
| Game update | Client version or balance patch released mid-window | Split the window at the update and analyse each part separately |
| Premium spend | Starbux bought or spent, purchase of a pack | Record the amount. Exclude the day from growth if it bought progress |
| Rare acquisition | Legendary crew, a special item, an event reward | Record it, and name it in the report next to every figure it could have moved |
| Manual action in an automated arm | The owner upgrades, trains or battles by hand on an automated account | Counts against automation coverage. Exclude the day if it touched the primary metric |
| Automation failure | Run stopped, uncertain outcome reconciled by hand, scheduler stop | Count the failure. The repair time counts as operator minutes |
| Hull upgrade | Ship level changes | Split the block at the change (new bracket) |
| Configuration change | Policy, strategy, reserve or budget changed | Allowed only between windows. Inside a window it ends the window for that arm |
| Outage or access | Server maintenance, login failure, rotated credentials | Exclude the affected days. Record their number |
| Account context | Alliance change, tournament, event week | Record it. Compare like with like in the report |

## Windows

- Every window is set in UTC before it opens and recorded as `StudyPlan.window_start` and `window_end`, which become each `Experiment`'s window. Windows for one account never overlap. `ExperimentSchedule` already enforces this for PvP strategy windows.
- Record the game version, hull level, trophies and a full snapshot at the start of every window.
- **Baseline window first.** Run the manual routine for 7 to 14 days purely to measure the primary metric's mean and standard deviation. Do not decide anything from it. Re-size the plan from the measured values before the comparison starts.
- Block length is fixed for the whole study (seven days unless the owner sets otherwise), and every arm gets the same number of blocks.

## Stopping rules

`decide` makes the statistical stop. Safety rules can stop an arm first.

1. **No decision before the minimum sample.** `decide` returns `keep-running` while either arm has fewer than `per_arm_min_sample(plan)` units.
2. **Pre-registered looks only.** Evaluate at the end of each block with `decide_and_record`, so every look is in the `DecisionLog`. Do not evaluate on arbitrary days.
3. **`adopt` or `reject` ends that comparison.** For one look with equal standard errors, `decide`'s non-overlap rule has a false-adopt rate of about 0.3% (0.6% for adopt or reject). By a Bonferroni bound, eight looks keep the overall false-decision rate under 5%. The bound weakens when one arm has a much larger standard error than the other, so keep arms balanced.
4. **`keep-running`** continues to `window_end`.
5. **`insufficient-evidence`** at `window_end` ends the comparison as inconclusive. Extending a window is allowed only if the extension was written into the plan before the window opened.
6. **Acting on a decision is a human step.** `adopt` does not change any account. Rolling out the adopted version is a new configuration, recorded in the intervention log, and starts a new window.
7. **Safety stops override statistics.** Stop an automated arm at once, and report the comparison as aborted, on any cross-account incident from the fleet audit, any starbux spend by automation, or an uncertain non-idempotent outcome that is not reconciled within a day. Thresholds for the failure rate are the owner's to set before the window opens.

`decide` has no futility rule and no non-inferiority mode. It cannot conclude that automation is "no worse" than manual play, only that it is better, worse, or not separable.

## What the owner must supply

None of these exist yet, and the study cannot start without them:

- **Test accounts.** At least one owned test account by alias, ideally two in the same hull bracket. Its hull level and trophies at the start. Credentials provisioned through the existing secret mechanism, never in tracked files.
- **Authorization.** Live runs for the automated arm, and read-only snapshot reads for the manual arm. Removal of the study accounts from `daily-run.yml` for the study.
- **The manual routine.** A written list of what the owner does each day, in what order and at what times, so the manual arm is a defined treatment and not "whatever happened".
- **Time logging.** How operator minutes will be recorded for each account and day, in both arms. The fleet report takes them as `manual_minutes` and `operator_minutes`.
- **Windows.** Start dates, block length, number of blocks, and the snapshot times each day.
- **The primary metric and the smallest effect worth detecting**, with a planning standard deviation or a commitment to run the baseline window first.
- **Safety thresholds.** The failure rate or incident count at which an automated arm is stopped.

## Planning a study

```python
from datetime import datetime, timezone
from sdk.experiments import StudyPlan, experiments_for, per_arm_min_sample

plan = StudyPlan(
    study_id="study-a", hypothesis="baseline-v1 completes more upgrades and research per day than the manual routine",
    arms=("manual", "baseline-v1"), metric="growth-per-day", metric_kind="mean", higher_is_better=True,
    baseline_value=2.0, minimum_effect=1.0, sd=1.0,  # assumptions until the baseline window is measured
    unit="account-day", window_start=datetime(2027, 1, 4, tzinfo=timezone.utc),
    window_end=datetime(2027, 3, 1, tzinfo=timezone.utc),
)
per_arm_min_sample(plan)          # 29
(experiment,) = experiments_for(plan)  # Experiment(min_sample=29, baseline="manual", candidate="baseline-v1", ...)
```

`StudyPlan` refuses an invalid plan: arms that are not distinct lowercase identifiers, a rate outside (0, 1), a mean metric without a positive `sd`, `alpha` other than 0.05 under the decide rule (`decide` always compares 95% intervals), power not above alpha, a `min_sample` floor under 2, or a window that is not UTC or ends before it starts.

## Limitations of this design

- Every planning number is an assumption until a baseline window has been measured.
- The independence assumption behind every interval is violated by account-days. Block means are the check.
- With one or two accounts, results describe those accounts. They do not generalize to other hull levels or play styles.
- The manual arm is not journaled. Only snapshot-based metrics compare it with automation.
- PvP and market studies cannot produce a figure until a trophy read and the market calls exist.
- A fleet comparison of sequential with concurrent operation needs concurrent operation, which is not implemented.
