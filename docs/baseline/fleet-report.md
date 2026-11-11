# Fleet report (R5 exit metrics)

`sdk/reports/fleet.py` summarises a fleet on fields that stay the same whether accounts ran sequentially or, later, concurrently. `compute_fleet_report(journals, fleet_run=None, audit=None, manual_minutes=None, operator_minutes=None)` takes each account's journal events, and optionally the [orchestrator's](fleet-accounts.md#orchestration) `FleetRun`, the [isolation audit](fleet-audit.md), and the operator's own time figures.

| Field | Definition | Scorecard metric |
| --- | --- | --- |
| `attempted`, `verified`, `uncertain`, `contradicted`, `failed`, `skipped` | Counts from the account's own events | — |
| `success_rate`, `uncertain_rate`, `failure_rate` | Divided by attempted events; `None` when nothing was attempted | Verified action success, incident/failure frequency |
| `elapsed_hours`, `actions_per_hour` | From the first attempt's start to the last attempt's finish | Fleet throughput |
| `incidents` | Audit incidents for the account; `incidents_by_rule` for the fleet | Isolation |
| `status`, `mode`, `dry_run` | From the `FleetRun` when supplied; mode is `unknown` otherwise | — |
| `time_saved_minutes` | `manual_minutes - operator_minutes`, only when the operator supplied both for the account. The fleet total sums the accounts that have it, and is `None` when none do. | Time saved |

Events whose alias differs from the journal owner are left out of that account's figures; the audit reports them as `foreign-event`. The fleet row uses the same rules over all accounts' own events. `to_json(report)` and `to_markdown(report)` render the report.
