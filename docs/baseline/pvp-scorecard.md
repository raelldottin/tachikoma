# PvP scorecard and strategy comparison (R3 exit metrics)

`sdk/reports/pvp.py` turns [battle results](pvp-battle-results.md) into the roadmap's offense/defense scorecard and compares two [strategies](pvp-strategies.md).

## Scorecard

`compute_scorecard(battles)` returns one row per account alias, strategy key (`none` when no window applied) and opponent cohort (hull bracket / trophy bracket).

| Field | Definition | Scorecard metric |
| --- | --- | --- |
| `attack`, `defense` | Battles, known-result battles, wins, losses and unknown outcomes for each role | — |
| `win_rate` | Wins / known-result battles; `None` with no known results | Attack / defense win rate |
| `interval` | 95% Wilson score interval for `win_rate` | — |
| `net_trophies`, `trophy_samples` | Sum of observed trophy changes and how many battles observed one; `None` when none did | Net trophies |
| `mean_duration_s` | Mean recorded battle duration; `None` when none was recorded (the journal does not record it yet) | — |
| `resources_spent`, `resource_samples` | Observed resource spend during battle actions | Cost per progression unit |

Unknown outcomes are counted in `unknown` and never treated as wins or losses.

## Comparison

`compare_strategies(battles, strategy_a, strategy_b, account_alias=None, role="attacker", min_sample=30)` returns a verdict:

| Verdict | When |
| --- | --- |
| `recommend` | Both strategies have at least `min_sample` known-result battles, their 95% intervals do not overlap, and no opponent cohort where both reach `min_sample` points the other way |
| `no-clear-difference` | Enough samples, but the intervals overlap |
| `insufficient-evidence` | Too few known-result battles for either strategy, or the cohorts disagree |

`recommended` is set only for `recommend`. Per-cohort statistics are included so a reader can see where the evidence comes from.

`to_json(rows, comparison=None)` and `to_markdown(rows, comparison=None)` render the report.

## What this cannot show yet

Until a read-only trophy or battle-history call is verified, every journaled battle outcome is `unknown`, so live scorecards will report counts with `n/a` rates. The blocked slice `r3-live-pvp-trial` produces real comparisons once the owner authorizes live runs.
