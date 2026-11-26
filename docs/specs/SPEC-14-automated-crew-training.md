# SPEC-14: Automated crew training

**Status:** Draft
**Release:** R2 (automated growth engine)
**Owner gate:** G8 (proposed, per-account authorization for a scheduled training start)
**Depends on:** SPEC-05, SPEC-06
**Implements roadmap item:** Crew training in the [R2 automated growth engine](../PRODUCT_ROADMAP.md#r2--automated-growth-engine)

## Problem

[The scheduled daily run](../../.github/workflows/daily-run.yml) executes and reports training through `Client.manageTraining()`, and [`training-v1`](../../sdk/policy/training.py) plans training on the fleet path. Both use a [shared planner](../../sdk/training/planner.py) whose default strategy assigns roles by exact crew name. The fleet policy currently declares training starts as zero-cost, and the daily workflow creates fresh journals for each run. This spec extends the shared planner for current-roster specialization and defines the paid-course and cross-run safeguards needed to schedule an Academy start.

## Scope

- Assign roles from the current roster, select training for crew already in compatible onboard facilities, and expose the same decisions in a read-only crew plan and the scheduled progression workflow.
- Keep the original named crew goals in the plan and show Prestige recipes and missing ingredients. Crew relocation, room placement or upgrades, rushing, changes to existing course-finish or consumable behavior, and Prestige execution are outside this spec.

## Requirements

- **S14-R1 (MUST)** Assign roles deterministically from current crew stats and ship role demand, prioritizing distinct shield, repair and weapon specialists when the roster supports them. Use character instance IDs for assignments and prefer observed stats to estimates based on design and level. Report any specialist role the roster cannot cover. Use the same assignments to choose courses and to render the plan.
- **S14-R2 (MUST)** Select an available course that trains the assigned stat and matches the crew member's current onboard room, room level and required completed research. A ready Academy with an eligible repair specialist can offer Engineering Manual when the current design data confirms the match and availability. Check room status and capacity, active or unclaimed training, fatigue, training points and course cost from fresh account reads. Skip incomplete or ineligible candidates with a reason. Preserve a legitimate observed zero and treat missing data as unknown.
- **S14-R3 (MUST)** Offer read-only `--crew-plan` and `--fleet-dry-run` previews for the same training policy. The crew plan shows roles, facility readiness, course or skip reason, and direct Prestige routes toward the original named targets. Resolve recipes by crew design ID, count duplicate ingredients accurately, and label research and eligibility as unknown when unobserved. Preview calls make no gameplay writes.
- **S14-R4 (MUST)** Run the new policy only for an account explicitly configured with `policy_id: training-v2`, built on `training-v1`. Live execution uses `--fleet-execute --mode full`, a matching recent dry run, and authorization for `add_training` on that account. Submit at most one training start per account per run, within the account's action cap, mineral and gas reserves, and remaining spend limits. Recheck eligibility, observed price and balances immediately before submitting. Refuse any unknown cost or Starbux-priced course.
- **S14-R5 (MUST)** Verify a start only when a fresh post-action roster read shows that the intended character changed from observed idle to the requested course ID with a running timer. Store the sanitized outcome, course, end time and observed spend in the account journal. An already active or ended-but-unclaimed course cannot trigger a new start. A verified finish can make the crew eligible on the next scheduled run. An ambiguous, unchanged or missing post-read cannot trigger an automatic retry, including on a later scheduled run. Keep the account's training paused across runs until the owner reconciles the prior attempt. Treat completion and stat gains as separate observations.
- **S14-R6 (MUST)** Schedule the fleet dry-run and authorized execution entry points for the opted-in account under an owner-managed workflow. Preserve journals between runs, serialize scheduled and manually dispatched Tachikoma training for that account, and honor the existing stop control. Keep the account outside the legacy daily run where SPEC-06 applies. A missing journal when prior attempts may exist, unavailable serialization or an invalid authorization stops training execution.

## Interfaces

- `uv run run.py --crew-plan` previews roles, training choices and Prestige routes.
- `uv run run.py --fleet-dry-run` and `uv run run.py --fleet-execute --mode full` use the same registry and journal directory. The account registry opts in with `policy_id: training-v2`. The execution command also takes the existing authorization and identity arguments from [SPEC-05](SPEC-05-live-execution-gate.md).
- Reuse `sdk.training`, the snapshot provider, `DailyScheduler`, `ActionRunner`, `add_training`, `Client.addTraining` and the [training report](../baseline/training-report.md). Extend the policy with observed course prices and pass them through the existing caps and journal. Keep training after the existing progression block and account for earlier requests before spending. `baseline-v1`, `training-v1` and the legacy daily workflow keep their current behavior.

## Safety invariants

- All decisions, journal entries and spend are scoped to the same verified account. Preserve account identity checks and redact credentials, raw payloads and player identifiers from published logs and evidence.
- Training starts are non-idempotent. Persist the attempt before sending it, and treat an interrupted attempt without a reconciled outcome as ambiguous when the next run loads the journal.
- Use observed costs and state. Fleet execution prohibits Starbux spend. The training runner never relocates crew or spends a Prestige ingredient.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S14-AC1 | S14-R1, S14-R3 | offline | Given a roster without any original target names, both previews assign distinct specialists when possible and agree on courses. Changing roster stats changes assignments deterministically. A recipe needing two identical designs reports one missing when the roster has one eligible copy. Neither preview calls a mutation. |
| S14-AC2 | S14-R2, S14-R4 | offline | Given an idle repair specialist in a ready level 2 Academy and an observed eligible Engineering Manual course costing 500 minerals, the authorized action submits one start. Missing state, exhausted capacity, fatigue, a moved crew member, a room under upgrade, missing research, premium pricing or insufficient budget causes a skip or refusal with zero starts. |
| S14-AC3 | S14-R4, S14-R5 | offline | Given the same planned start, a fresh read with the requested course and timer verifies it. An already-active course, disabled policy, missing authorization or stale dry run yields zero starts. An unresolved timeout, different course, missing ID or interrupted attempt blocks further training across reruns until reconciled. |
| S14-AC4 | S14-R5, S14-R6 | offline | Given overlapping dispatches and a shared account journal, one dispatch can submit the start. After restart, a pending attempt still pauses training. A stop request, journal persistence failure or inability to take the account lock yields zero writes. |
| S14-AC5 | S14-R2, S14-R4, S14-R5, S14-R6 | live | On one owner-authorized ship, the first scheduled run starts one eligible course and records its ID, timer and spend from fresh reads. A later scheduled run while it remains active skips it. The published evidence includes the policy and game version and the reason for the skip. |

## Test plan

Extend the existing training fixtures, planner and fleet-policy tests. Use the stateful fake game server and a controlled clock to exercise the CLI and cross-run reconciliation. Include a run with a priced course beside existing zero-cost training cases. Run the offline suite and repository checks before the owner validates one scheduled execution.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec14-training-observations` | S14-R2, S14-R5 | `sdk/state/snapshot.py`, `sdk/state/client_provider.py`, `sdk/state/baseline.py`, `sdk/training/model.py`, `sdk/training/parse.py`, `sdk/actions/catalog.py`, `tests/test_account_snapshot.py`, `tests/test_client_snapshot_provider.py`, `tests/test_training_parse.py`, `tests/test_training_actions.py` | agent |
| `spec14-crew-policy` | S14-R1, S14-R2, S14-R3 | `sdk/training/strategy.py`, `sdk/training/planner.py`, `sdk/training/prestige.py`, `sdk/policy/training.py`, `sdk/policy/progression.py`, `sdk/policy/__init__.py`, `sdk/fleet/factory.py`, `run.py`, `README.template`, `README.md`, `tests/test_training_planner.py`, `tests/test_training_policy.py`, `tests/test_crew_plan.py` | agent, after training observations |
| `spec14-training-execution` | S14-R4, S14-R5, S14-R6 | `sdk/execution/runner.py`, `sdk/scheduler/daily.py`, `sdk/fleet/factory.py`, `tests/test_action_runner.py`, `tests/test_daily_scheduler.py`, `tests/test_fleet_execute.py` | agent, after crew policy |
| `spec14-scheduled-training` | S14-R6 | Owner-managed workflow, authorization and evidence in `docs/experiments/live/` | owner |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S14-Q1 | Approve this policy and the proposed one-start limit? | Implementation | Keep draft. |
| S14-Q2 | Which account, schedule, mineral and gas caps and reserves are authorized? | Scheduled live execution | No scheduled starts. |

## Evidence

[Daily training and reporting](https://github.com/raelldottin/tachikoma/pull/287), [fleet training policy](https://github.com/raelldottin/tachikoma/pull/288), [reconciled daily execution](https://github.com/raelldottin/tachikoma/pull/289) and [simulator benchmarks](https://github.com/raelldottin/tachikoma/pull/290) are merged. The individual Academy start that prompted this spec ran separately. Scheduled acceptance for `training-v2` is pending.

## Open questions and risks

- Confirm how current responses identify an expired or unclaimed course, room capacity and training-based stat gains. Until a field required for eligibility can be observed reliably, skip the affected candidate.
- Confirm where the scheduled runner can persist and lock the same account journal across dispatches. If it cannot, live scheduling stays disabled.
