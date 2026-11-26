# SPEC-14: Automated crew training

**Status:** Approved
**Approval:** The owner's request to continue after the offline approval request authorizes test-first offline implementation with a maximum of one training start per account per run. The owner separately approved live testing. The first live test is planned for Echo Rune, with the one-start limit. The exact authorization and recurring schedule remain to be recorded under G8.
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
- **S14-R4 (MUST)** Run the new policy only for an account explicitly configured with `policy_id: training-v2`, built on `training-v1`. Live execution uses `--fleet-execute --mode full`, a matching recent dry run, and authorization for `add_training` on that account. Submit at most one training start per account per run. Immediately before submitting, recheck eligibility and the selected course's authoritative game price and currency against fresh account balances, reserves, and remaining spend and action caps after earlier attempts. Accept zero only when explicitly observed for the selected course. Refuse unavailable, malformed, stale, or Starbux prices.
- **S14-R5 (MUST)** Verify a start only when a fresh post-action roster read shows that the intended character changed from observed idle to the requested course ID with a running timer. Store the sanitized outcome, course, end time and observed spend in the account journal. An already active or ended-but-unclaimed course cannot trigger a new start. A verified finish can make the crew eligible on the next scheduled run. An ambiguous, unchanged or missing post-read pauses all training starts on that account across runs. An operator can resume only after recording a durable reconciliation of the prior attempt using authoritative evidence. Treat completion and stat gains as separate observations.
- **S14-R6 (MUST)** Schedule the fleet dry-run and authorized execution entry points for the opted-in account under an owner-managed workflow. Preserve journals between runs and honor the existing stop control. Keep the account outside the legacy daily run where SPEC-06 applies. Serialized mode (S06-R6) is not enough: the legacy run also starts training through `Client.manageTraining()`, outside this policy's one-start limit, attempt store and lock, so the account must be listed in `LEGACY_RUN_SKIP` before its first `training-v2` start. After verifying account identity, take its nonblocking dispatch lock before any account mutation and hold it through settlement. An overlapping dispatch for that identity skips the account even if it would select a different crew member. Separate accounts may run independently. Missing or unreadable established state, unavailable serialization or invalid authorization stops execution.

- **S14-R7 (MUST)** Run the offline workflow benchmark and semantic mutation checks in S14-AC6 and S14-AC7 on the same candidate revision. Preserve the existing benchmark floors and QA gate. Publish the exact revision, commands, seeds, scenario results, detection rates and remaining survivors.

## Interfaces

- `uv run run.py --crew-plan` previews roles, training choices and Prestige routes.
- `uv run run.py --fleet-dry-run` and `uv run run.py --fleet-execute --mode full` use the same registry and journal directory. The account registry opts in with `policy_id: training-v2`. The execution command also takes the existing authorization and identity arguments from [SPEC-05](SPEC-05-live-execution-gate.md).
- Reuse `sdk.training`, the snapshot provider, `DailyScheduler`, `ActionRunner`, `add_training`, `Client.addTraining` and the [training report](../baseline/training-report.md). Pass freshly observed course prices through the existing caps and journal. Keep training after the existing progression block and account for earlier requests before spending. `baseline-v1`, `training-v1` and the legacy daily workflow keep their current behavior.

## Design for testability

- Keep observed-state normalization, role assignment, course eligibility and pricing, Prestige matching, and reconciliation classification pure. Feed them explicit observations and time. Assign as many distinct requested specialist slots as possible before optimizing role scores. Break equal scores by stable character instance ID. Both previews and the executor use the same decisions.
- Keep I/O in the fleet path. Inject the clock, observation provider, action client, attempt store and account lock. Key the lock to the verified account identity across processes and runner instances. Take it before any budget-affecting write, including earlier progression actions, and hold it for the account's whole execute dispatch. Recheck the authoritative price, currency and balances under the lock before sending the request. If the authoritative price cannot be observed for a course, treat that course as ineligible for execution.
- Exercise each pure decision with fixed inputs and the production entry point offline with a stateful fake and a persistent journal. Drive the real fleet CLI, authorization, scheduler and action runner. Read the fake's applied state and received requests independently of the planner, report and journal.

## Safety invariants

- All decisions, journal entries and spend are scoped to the same verified account. Preserve account identity checks and redact credentials, raw payloads and player identifiers from published logs and evidence.
- Training starts are non-idempotent. Write the intent atomically and wait for durable acknowledgment before sending a request. If intent persistence fails, send nothing. If settlement fails after sending, retain the unresolved intent and block later starts, including a different crew member or course. Persist an operator's resolution under the same account lock before lifting the block.
- Provision a virgin attempt store explicitly for the verified account before first execution. Refuse to recreate missing established state or treat unreadable state as a fresh account. Use observed costs and state. Fleet execution prohibits Starbux spend. The training runner never relocates crew or spends a Prestige ingredient.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S14-AC1 | S14-R1, S14-R3 | offline | Given a roster without any original target names, both previews assign distinct specialists when possible and agree on courses. Changing roster stats changes assignments deterministically. A recipe needing two identical designs reports one missing when the roster has one eligible copy. Neither preview calls a mutation. |
| S14-AC2 | S14-R2, S14-R4 | offline | Given an idle repair specialist in a ready level 2 Academy and an observed eligible Engineering Manual course costing 500 minerals, execute sends one start with exactly 500 spendable. With 499 spendable, an earlier reservation that leaves the account short, stale room or research, an unavailable authoritative price or a premium currency, execute sends none. Explicit zero is accepted and an unobserved price is refused. |
| S14-AC3 | S14-R4, S14-R5 | offline | Given the same planned start, a fresh read with the requested course and timer verifies it. An already-active course, disabled policy, missing authorization or stale dry run yields zero starts. An unresolved timeout, different course, missing ID or interrupted attempt blocks further training across reruns until reconciled. |
| S14-AC4 | S14-R5, S14-R6 | offline | With two eligible idle crew, use an interprocess barrier to overlap dispatches. The lock holder sends at most one start and the other sends no account writes, even with a different candidate. A separate account can still start. A stop, missing established state or failed intent persistence yields zero starts. A settlement failure after sending leaves at most the original request and a durable pending record. Restart pauses the account until a durable operator resolution. |
| S14-AC5 | S14-R2, S14-R4, S14-R5, S14-R6 | live | On one owner-authorized ship, the first scheduled run starts one eligible course and records its ID, timer and spend from fresh reads. A later scheduled run while it remains active skips it. The published evidence includes the policy and game version and the reason for the skip. |
| S14-AC6 | S14-R1, S14-R2, S14-R3, S14-R4, S14-R5, S14-R6, S14-R7 | offline | The candidate passes every required workflow trace with zero hard violations and preserves the existing benchmark gate. Repeat runs, including a fresh process with a different hash seed, produce identical normalized behavioral traces. These fixtures simulate dispatch, persistence and stop controls. S14-AC5 verifies the owner-managed schedule. |
| S14-AC7 | S14-R7 | offline | Given the passing unmodified revision and independently seeded behavioral defects, every critical defect triggers its named assertion against observed behavior. The existing QA gate also passes. |

## Test plan

Freeze the oracle, required cases and independent holdout before production changes. Run the first CLI acceptance tests against unchanged code and record the failing behavioral assertion and its cause. Pair each failing case with a passing legacy control. Keep baseline and existing QA gates green during implementation. Include an independent design review before benchmarking.

### Workflow benchmark

- Keep the existing training benchmark at or above its checked-in composite floor of 67.261087 and every existing scenario floor. Its training-point score, determinism checks and hard-violation gates continue to apply. [Training benchmark](../baseline/training-benchmark.md)
- Score the new fleet workflow separately. Freeze the required manifest in `tests/fixtures/training/workflow_benchmark.json` and the independent holdout in `tests/fixtures/training/workflow_holdout.json`. Require these case IDs across three fixed seeds and require every held-out case declared before implementation. Compare an independently authored trace against actual roles, skip reasons, received requests, account identity, course and timer, signed resource changes and journal outcomes. Compare paired `training-v1` controls against frozen traces.
- Required cases are `role-coverage`, `preview-prestige`, `paid-boundary`, `stale-preflight`, `reconciliation`, `persisted-intent-restart`, `overlapping-dispatch`, `distinct-accounts` and `store-stop-controls`. Use barriers to witness contention, and require a paid start on one identity even when a separate identity is locked.
- Score each required scenario-and-seed trace as pass or fail. Require 100 times passing traces divided by required traces to equal 100.000000. Exit nonzero on an empty suite, missing or duplicate case, missing required witness, fake or oracle error, or semantic mismatch. Account crossover, budget or premium spend violations, duplicate writes and false verification fail the gate regardless of score. Normalize only environmental values. Sort concurrent outcomes canonically while preserving account and candidate identity, request counts, and overlap evidence.

During the red stage, run `python scripts/training_workflow_benchmark.py --gate` explicitly and record the unmet behavioral assertions. The final evaluation slice registers the scenario and critical-defect checks in `tests.test_training_workflow_benchmark`. Reproduce the blocking mocked CI checks with `python scripts/ci.py test --names tests.test_training_workflow_benchmark`. The full mocked suite discovers those tests without changing the scheduled workflow.

### Test effectiveness

- Seed executable defects for greedy role allocation that leaves repair uncovered, unstable tie and input-order behavior, observed zero treated as unknown, and a two-copy recipe counted as one. Seed wrong-stat and wrong-price courses, 499 versus 500 spendable minerals, an earlier reservation consuming the remainder, and stale crew, room or research observations. Seed false verification without the requested course and timer, a retry for a different crew member after an ambiguous paid call, and two processes racing or restarting after a persisted intent.
- Require the unmodified candidate to pass both channels. Every critical defect must fail its named behavioral assertion in the test or benchmark channel. Import errors, crashes, timeouts and trace drift alone do not count toward this requirement. Record the test or oracle that caught each defect. Keep legacy QA detection at or above 90% and report tests-only, benchmark-only, both, and remaining survivors over the importable set.
- Use a fresh agent for Research → Plan → Write → Review → Benchmark → QA Eval. Hand off the exact revision, evidence and next gate. A review finding, lower score or surviving critical defect returns to the earliest affected stage with a fresh agent. Keep the failed evidence and rerun the gates on the revised candidate. Stop when all offline gates pass on one revision or when a specific unresolved blocker prevents another iteration.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec14-workflow-red` | S14-R1–R7 | `scripts/training_workflow_benchmark.py`, `tests/test_training_workflow_benchmark.py`, `tests/fixtures/training/workflow_benchmark.json`, `tests/fixtures/training/workflow_holdout.json`, `tests/fakes/pss_server.py`, `tests/fakes/state.py` | agent, freeze fixtures and capture red CLI assertions before implementation |
| `spec14-training-observations` | S14-R2, S14-R5 | `sdk/state/snapshot.py`, `sdk/state/client_provider.py`, `sdk/state/baseline.py`, `sdk/training/model.py`, `sdk/training/parse.py`, `sdk/actions/catalog.py`, `tests/test_account_snapshot.py`, `tests/test_client_snapshot_provider.py`, `tests/test_training_parse.py`, `tests/test_training_actions.py` | agent, after workflow red |
| `spec14-crew-policy` | S14-R1, S14-R2, S14-R3 | `sdk/training/strategy.py`, `sdk/training/planner.py`, `sdk/training/prestige.py`, `sdk/policy/training.py`, `sdk/policy/progression.py`, `sdk/policy/__init__.py`, `sdk/fleet/factory.py`, `run.py`, `README.template`, `README.md`, `tests/test_training_planner.py`, `tests/test_training_policy.py`, `tests/test_crew_plan.py` | agent, after training observations |
| `spec14-training-execution` | S14-R4, S14-R5, S14-R6 | `sdk/execution/runner.py`, `sdk/scheduler/daily.py`, `sdk/fleet/factory.py`, `tests/test_action_runner.py`, `tests/test_daily_scheduler.py`, `tests/test_fleet_execute.py` | agent, after crew policy |
| `spec14-workflow-eval` | S14-R7 | `scripts/training_workflow_benchmark.py`, `tests/test_training_workflow_benchmark.py`, `docs/baseline/training-benchmark.md` | agent, after training execution |
| `spec14-scheduled-training` | S14-R6 | Owner-managed workflow, authorization and evidence in `docs/experiments/live/` | owner |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S14-Q1 | Approve this policy and the proposed one-start limit? | Implementation | Approved for offline implementation, with one start per account per run. |
| S14-Q2 | Which account, schedule, mineral and gas caps and reserves are authorized? | Scheduled live execution | Live testing approved in principle for Echo Rune. Record the verified account identity, test window, caps and reserves in G8 before a live course start. A recurring schedule needs its own authorization. |

## Evidence

[Daily training and reporting](https://github.com/raelldottin/tachikoma/pull/287), [fleet training policy](https://github.com/raelldottin/tachikoma/pull/288), [reconciled daily execution](https://github.com/raelldottin/tachikoma/pull/289) and [simulator benchmarks](https://github.com/raelldottin/tachikoma/pull/290) are merged. The individual Academy start that prompted this spec ran separately. Scheduled acceptance for `training-v2` is pending.

## Open questions and risks

- Confirm how current responses identify an expired or unclaimed course, room capacity and training-based stat gains. Until a field required for eligibility can be observed reliably, skip the affected candidate.
- Confirm the authoritative price and currency source before authorizing a paid start. Confirm where the scheduler shares and durably stores account locks, initialization state, pending attempts and operator settlements across dispatches. Live scheduling stays disabled while those prerequisites are unverified.
