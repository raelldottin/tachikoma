# Daily progression scheduler (R2 / AUTO-004)

`sdk/scheduler/DailyScheduler` connects a [progression policy](progression-policy.md) to the [action runner](action-runner.md) for one account.

1. It takes one snapshot through the runner's provider and refuses one that belongs to another alias.
2. It plans once with the policy.
3. It executes the plan's requests in order through `ActionRunner.run`. The runner takes fresh before and after snapshots for every action, so each one is checked against current state, not the state the plan was made from.

## Safety rules

| Rule | Behaviour |
| --- | --- |
| Dry run by default | `run()` journals dry-run events only. A mutating call needs `run(execute=True)`. |
| Operator stop | If `SchedulerConfig.stop_file` exists, the run stops before the next action. |
| Action budget | `max_actions` (default 20) caps the attempts per run. |
| Once per period | A `once-per-period` action (the daily reward) is skipped when the journal shows an attempt within `period` (default 24 h) that was `verified` or is still `unknown`. |
| Contradiction | The first `contradicted` outcome stops the run. |
| Uncertainty | An `unknown` outcome for a non-idempotent action stops the run. A raw room-upgrade response that leaves the room unchanged, for example at the server's concurrent-construction limit, ends up here. |
| Runner refusal | `UncertainPriorAttemptError` or `AccountMismatchError` from the runner stops the run, and the report says why. |

`ScheduleReport` returns the plan, every run result, skipped requests with reasons, and `stop_reason` (`None` when the whole plan ran).

## Scope

The scheduler is not wired into `run.py`. Slice `r2-progression-dry-run-command` adds a dry-run command. Live execution against a real account is left to the blocked slice `r2-live-progression-trial`, which needs owner authorization.
