# SPEC-10: Live fleet trial on two accounts

**Status:** Approved
**Approval:** owner, 2026-11-11, through the session goal "complete the plan in docs/specs/". This approves building and testing the spec offline; owner questions take their safe defaults. It does not open any live gate (G1–G5), so live acceptance criteria stay pending.
**Release:** R5 exit (study D)
**Owner gate:** G5
**Depends on:** SPEC-05, SPEC-06, SPEC-07
**Implements roadmap item:** queue slice `r5-live-fleet-trial`; "R5 exit: consistent multi-ship metrics, no unintended cross-account execution or data mix-ups, reproducible workload comparisons, and measurable operator time savings"

## Problem

The registry, sequential and concurrent orchestration, identity checks, the isolation audit and the fleet report all exist, and `--fleet-dry-run` wires them into `run.py`. None has run live, and the sequential-vs-concurrent comparison has no data.

## Scope

- **In:** two accounts under one authorization, sequential and concurrent windows, the isolation audit, and the fleet report.
- **Out:** more than two accounts, and differentiated per-account strategies beyond the registry fields.

## Requirements

- **S10-R1 (MUST)** Both profiles declare `expected_game_identity`, both are fenced from the legacy run (SPEC-06), and the authorization names both.
- **S10-R2 (MUST)** Windows alternate between sequential and `--concurrent 2` by a schedule fixed in the trial plan.
- **S10-R3 (MUST)** After every run, `audit_fleet` runs on both journals; any isolation incident stops the trial and is reported.
- **S10-R4 (MUST)** The fleet report records per-account and fleet metrics, the mode, incidents, and operator minutes (time saved only when both figures are supplied).
- **S10-R5 (MUST)** The exit report compares modes on verified actions per hour, failure and uncertain rates, and incidents, with sample sizes and `decide()` results.
- **S10-R6 (SHOULD)** Alerts from `sdk/reports/alerts.py` are evaluated after every run and included in the run record.

## Interfaces

- `run.py --fleet-execute --concurrent 2` (SPEC-05).
- `run.py --report fleet --registry`.
- `scripts/trial_report.py --fleet`.

## Safety invariants

- Identity checks are mandatory.
- The audit runs after every run.
- One account's failure never stops the other.
- No starbux.
- Caps apply per account.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S10-AC1 | R1 | offline | Execute refuses a two-account run when either profile lacks a fingerprint or legacy-run fencing |
| S10-AC2 | R3 | offline | A synthetic foreign event stops the trial and appears in the report |
| S10-AC3 | R4, R5, R6 | offline | Synthetic sequential and concurrent journals produce the comparison and alerts |
| S10-AC4 | R2–R5 | live | The owner runs the scheduled windows and commits the report |

## Test plan

Extend `tests/test_fleet_execute.py` and `tests/test_trial_report.py`; the two-account and fault-injection scenarios supply journals.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec10-fleet-report` | R3–R6 | `scripts/trial_report.py`, tests | agent |
| `spec10-run-trial` (replaces `r5-live-fleet-trial`) | AC4 | `docs/experiments/*` | **owner** |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S10-Q1 | The second test account, and both accounts' manual routine minutes | trial plan | None; R5 stays blocked |
| S10-Q2 | Window schedule for the two modes | trial plan | Alternate weekly for four weeks |

## Evidence

Pending.

## Open questions and risks

Both accounts share the client's process-wide rate limit, so concurrent mode may gain little; the report should say whether throughput was limited by rate.
