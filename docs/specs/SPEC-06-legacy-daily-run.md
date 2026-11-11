# SPEC-06: Fence the legacy daily run away from trial accounts

**Status:** Approved
**Approval:** owner, 2026-11-11, through the session goal "complete the plan in docs/specs/". This approves building and testing the spec offline; owner questions take their safe defaults. It does not open any live gate (G1–G5), so live acceptance criteria stay pending.
**Release:** operations (prerequisite for SPEC-07 and SPEC-10)
**Owner gate:** G2 (the owner edits the workflow)
**Depends on:** SPEC-05
**Implements roadmap item:** "The legacy daily run is separate ... its actions do not go through the runner, are not reconciled and are not journaled" ([known-limitations.md](../baseline/known-limitations.md)); "Removal of the study accounts from `daily-run.yml`" ([study-design.md](../experiments/study-design.md))

## Problem

`.github/workflows/daily-run.yml` runs the legacy `run.py` sequence, including `--run-battle`, three times a day for the accounts in repository secrets (`PSS_ACCOUNT_1` to `PSS_ACCOUNT_4`). Those actions bypass the journal, so the audit cannot see them, ledger entries become unattributed, and a trial account touched by it would contaminate both arms of a study.

## Scope

- **In:** a documented way to exclude named accounts from the legacy run, a check that a trial account is excluded, and a decision on the legacy run's future.
- **Out:** agents editing any workflow that holds secrets. Only the owner edits `daily-run.yml`.

## Requirements

- **S06-R1 (MUST)** The legacy run can skip an account without deleting its secrets, through a repository variable that lists skipped account numbers (for example `LEGACY_RUN_SKIP=2,3`), checked before each account's step.
- **S06-R2 (MUST)** `--fleet-execute` (SPEC-05) refuses an account whose registry profile declares `legacy_run_account: N` unless the owner confirms in the authorization file that N is listed in `LEGACY_RUN_SKIP`.
- **S06-R3 (MUST)** The legacy run's actions during a trial window are recorded as interventions in the trial record (owner step), if any happen.
- **S06-R4 (SHOULD)** The owner decides, and records in this spec, whether the legacy run is retired, kept for non-trial accounts, or replaced by a scheduled `--fleet-execute` (SPEC-11).
- **S06-R5 (MUST)** Workflow policy hardening for `daily-run.yml` (pinned actions, read-only permissions, timeouts) is applied by the owner, and the repository's workflow policy check covers the file afterwards.

## Interfaces

- `daily-run.yml`: a step-level `if:` on `vars.LEGACY_RUN_SKIP` (owner edit).
- `AccountProfile.legacy_run_account` (optional int) and an authorization field `legacy_run_skipped: [N]`.

## Safety invariants

- Only the owner edits the secret-holding workflow.
- Agents change only the registry and authorization checks.
- No secret moves.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S06-AC1 | R2 | offline | An execute run is refused for a profile with `legacy_run_account` not confirmed as skipped; accepted when confirmed |
| S06-AC2 | R1 | live | The owner sets `LEGACY_RUN_SKIP`, and the next scheduled run's log shows the skipped account's step skipped |
| S06-AC3 | R5 | offline, after owner edit | `scripts/ci.py repository` covers `daily-run.yml` and passes |
| S06-AC4 | R3 | live | Each trial record has an intervention log, which lists any legacy-run actions on trial accounts during the window or states there were none |

## Test plan

Extend `tests/test_fleet_execute.py` (SPEC-05) and `tests/test_ci.py` once the owner has hardened the workflow.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec06-workflow-skip` | R1, R5 | `.github/workflows/daily-run.yml` | **owner** |
| `spec06-execute-check` | R2 | `sdk/fleet/accounts.py`, `sdk/fleet/authorization.py`, tests | agent |
| `spec06-policy-coverage` | R5 | `scripts/ci.py`, `tests/test_ci.py` | agent, after owner edit |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S06-Q1 | Retire, keep or replace the legacy run? | M5 | Keep for non-trial accounts |
| S06-Q2 | Which legacy account numbers map to which registry aliases? | SPEC-07 | Not mapped; execute refuses |

## Evidence

Pending.

## Open questions and risks

`daily-run-langgraph.yml` also runs on a schedule; the owner should confirm whether it touches trial accounts.
