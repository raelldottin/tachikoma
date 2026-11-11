# Policy evaluation loop (R6, AUTO-011)

`sdk/experiments/` records experiments and decides from evidence whether a candidate variant should replace a baseline. It never changes an account; acting on a decision is a separate, human step.

## Experiment

| Field | Rule |
| --- | --- |
| `experiment_id`, `baseline`, `candidate`, `metric` | Lowercase identifiers; baseline and candidate must differ |
| `hypothesis` | Required; refused if `redact_secrets` would change it |
| `higher_is_better` | Direction of the metric |
| `min_sample` | At least 2; the minimum measurements per variant before any adopt or reject |
| `window_start`, `window_end` | UTC; the end must follow the start |

## Samples

- `ProportionSample(successes, trials)`: a rate such as verified-action success or win rate, with a 95% Wilson interval. `samples_from_outcomes` builds one from journal outcomes, leaving out `skipped`.
- `MeanSample(values)`: independent measurements such as resources per hour, with a 95% Student-t interval for the mean (needs two or more values). The critical value is `t_quantile_975(n - 1)`: a table of the standard two-sided 95% values for 1 to 30 degrees of freedom (checked to within 5e-7 by numerically integrating the t density), and beyond 30 the Cornish-Fisher expansion of the t quantile (Abramowitz and Stegun, 26.7.5), within 1e-7 of the exact value there. With two values the critical value is 12.71 rather than 1.96, so small samples get honestly wide intervals.

A zero-width interval carries no information about spread, so it never supports `adopt` or `reject`:

- a `MeanSample` whose values are all identical has zero variance and a point interval;
- a Wilson interval is never zero width for one or more trials, but intervals are rounded to 4 places, so with no (or only) successes in about 77,000 or more trials it collapses to `(0.0, 0.0)` or `(1.0, 1.0)`. That is treated the same way.

## Decisions

`decide(experiment, baseline, candidate, now)` returns:

| Decision | When |
| --- | --- |
| `adopt` | Both variants have at least `min_sample` measurements, neither interval has zero width, and the candidate's interval lies wholly on the better side of the baseline's |
| `reject` | The same, with the candidate wholly worse |
| `keep-running` | The window is open and the evidence is not yet conclusive: too few measurements, a zero-width interval, or overlapping intervals |
| `insufficient-evidence` | The window has closed without a conclusive result |

Each `Decision` carries both sample sizes, estimates and intervals, the reason and the time.

`DecisionLog(path)` appends decisions as JSONL (fsynced, secret-checked) and reads them back. `latest(experiment_id)` returns the most recent decision. `decide_and_record` decides and appends in one step.
