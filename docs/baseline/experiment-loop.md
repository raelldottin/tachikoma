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
- `MeanSample(values)`: independent measurements such as resources per hour, with a 95% normal interval (needs two or more values).

## Decisions

`decide(experiment, baseline, candidate, now)` returns:

| Decision | When |
| --- | --- |
| `adopt` | Both variants have at least `min_sample` measurements, and the candidate's interval lies wholly on the better side of the baseline's |
| `reject` | The same, with the candidate wholly worse |
| `keep-running` | The window is open and the evidence is not yet conclusive |
| `insufficient-evidence` | The window has closed without a conclusive result |

Each `Decision` carries both sample sizes, estimates and intervals, the reason and the time.

`DecisionLog(path)` appends decisions as JSONL (fsynced, secret-checked) and reads them back. `latest(experiment_id)` returns the most recent decision. `decide_and_record` decides and appends in one step.
