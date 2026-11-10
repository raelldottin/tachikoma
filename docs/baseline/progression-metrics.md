# Progression metrics report (R2 exit metrics)

`sdk/reports/compute_progression_metrics(events)` turns [journal](gameplay-journal.md) events into per-account, per-UTC-day rows that feed the roadmap scorecard. `to_json` and `to_markdown` render them.

| Field | Definition | Scorecard metric |
| --- | --- | --- |
| `attempted` | Events where a mutating call was made. This is the sample size for every rate. | — |
| `verified`, `not_observed`, `contradicted`, `uncertain` | Attempted events by reconciliation outcome | — |
| `verified_success_rate` | `verified / attempted`; `None` when nothing was attempted | Verified action success |
| `failed` | Attempted events with an error class recorded | Incident/failure frequency |
| `skipped`, `dry_run` | Precondition skips and dry-run events. They are not attempts. | — |
| `window_start`, `window_end`, `elapsed_hours` | Span from the first attempt's start to the last attempt's finish | — |
| `resources_gained`, `resources_spent` | Sums of positive and negative `*_delta` costs. Empty when no attempt observed resources. | Resources per hour |
| `gained_per_hour` | Gain divided by `elapsed_hours`; `None` without a window or resource samples | Resources per hour |
| `resource_samples` | Attempts with at least one observed resource delta | — |
| `failures` | Failure ledger: every attempt that was `contradicted` or `unknown`, or recorded an error | Incident/failure frequency |

## Rules

- No sample means no number. Rates and per-hour figures are `None` (`n/a` in Markdown) rather than 0.
- Dry-run and skipped events never count as attempts, so a dry run cannot raise or lower the success rate.
- Days are bucketed by the event's `finished_at` in UTC.

## What this does not measure yet

The roadmap's R2 exit also needs account growth velocity and operator time saved, both measured against a manual baseline. Those come from the blocked slice `r2-live-progression-trial`, after the owner defines the manual routine and authorizes live runs.
