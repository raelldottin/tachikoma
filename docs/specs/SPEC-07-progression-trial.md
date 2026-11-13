# SPEC-07: Live progression trial, manual vs `baseline-v1`

**Status:** Verified offline
**Approval:** owner, 2026-11-11, through the session goal "complete the plan in docs/specs/". This approves building and testing the spec offline; owner questions take their safe defaults. It does not open any live gate (G1–G5), so live acceptance criteria stay pending.
**Release:** R2 exit (study A)
**Owner gate:** G2
**Depends on:** SPEC-04, SPEC-05, SPEC-06
**Implements roadmap item:** queue slice `r2-live-progression-trial`; "R2 exit: measurable gains in progression tasks completed, operator time saved, and/or resource utilization, with transparent costs and a failure ledger"

## Problem

`baseline-v1`, the scheduler, the journal and the metrics report exist but have never run live. The R2 exit needs a measured comparison with a documented manual routine, which the [study design](../experiments/study-design.md) describes as study A.

## Scope

- **In:** the trial protocol, a canary, the trial run, the measurement, and the exit report.
- **Out:** adaptive policies (study B), PvP, market and fleet.

## Requirements

- **S07-R1 (MUST)** A written trial plan in `docs/experiments/r2-progression-trial.md` fixes, before the window opens: account alias, manual routine, arm schedule (ABBA crossover or matched pair from the study design), window dates, snapshot times, primary metric (growth velocity) and the smallest effect worth detecting, sample size from `sdk.experiments.study`, stop thresholds, and the authorization reference.
- **S07-R2 (MUST)** A canary run (S05-R4) succeeds and reconciles before the first automated window. It succeeds only when its action reconciles `verified`; `not-observed` does not count (owner decision, 2026-11-12).
- **S07-R3 (MUST)** Automated windows run `--fleet-execute` under the authorization; manual windows take read-only snapshots at the same times.
- **S07-R4 (MUST)** The primary metric is computed the same way in both arms, from snapshot diffs, never from journal outcomes alone.
- **S07-R5 (MUST)** Operator minutes are logged per account-day in both arms in the format the fleet report takes (`manual_minutes`, `operator_minutes`).
- **S07-R6 (MUST)** Interventions (premium spending, legacy-run actions, game updates, manual play during automated windows) are logged with time and kind.
- **S07-R7 (MUST)** The exit report gives, per arm: sample size, growth velocity with interval, resources gained and spent per hour, verified action success, the failure ledger, operator time, and the `decide()` result. It states limitations and the game version.
- **S07-R8 (MUST)** The trial stops early when a stop threshold is crossed, and the report says so.

## Interfaces

- `run.py --baseline` (SPEC-04) for manual-arm snapshots.
- `run.py --fleet-execute` (SPEC-05) for automated windows.
- `run.py --report progression|fleet|dashboard` for measurement.
- New `scripts/trial_report.py` builds the exit report from journals, snapshots, minutes and the intervention log.

## Safety invariants

- SPEC-05's gate applies.
- No starbux.
- One account.
- The legacy run is fenced (SPEC-06).
- Unobserved metrics are reported as `None` with their sample size.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S07-AC1 | R4, R7 | offline | Given synthetic snapshots, journals, minutes and interventions for both arms, `trial_report.py` produces the report with every field and the right `decide()` result |
| S07-AC2 | R8 | offline | A synthetic run crossing a stop threshold is reported as stopped early |
| S07-AC3 | R1 | owner | The trial plan is committed before the window opens |
| S07-AC4 | R2, R3, R5, R6 | live | Canary and windows run as planned; logs complete |
| S07-AC5 | R7 | live | The exit report is committed to `docs/experiments/`, and the roadmap status is updated |

## Test plan

`tests/test_trial_report.py` with synthetic data; the simulation harness supplies realistic journals.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec07-trial-report` | R4, R7, R8 | `scripts/trial_report.py`, tests | agent |
| `spec07-trial-plan` | R1 | `docs/experiments/r2-progression-trial.md` | **owner** (agent may draft) |
| `spec07-run-trial` (replaces `r2-live-progression-trial`) | R2, R3, R5, R6 | `docs/experiments/*` | **owner** |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S07-Q1 | Test account, manual routine, windows, time logging, stop thresholds | trial plan | None; the trial does not start |
| S07-Q2 | Commit raw journals, or only the report? | exit report | Only the report and summary tables |

## Evidence

**Offline verification:** every MUST with an offline acceptance criterion is covered by a test that cites it (checked by `scripts/spec_trace.py`). Implemented in #167.

**Trial plan (S07-R1, S07-AC3):** [r2-progression-trial.md](../experiments/r2-progression-trial.md) was approved by the owner on 2026-11-13, before the baseline window opens on 2026-11-16. It covers `pss5`, a baseline window of 2026-11-16 to 2026-11-22, then four 7-day ABBA blocks to 2026-12-20 (14 account-days per arm, enough for a 1.5 sd effect). The manual arm is the owner's regular play under four rules: no starbux, no play during automated blocks, minutes logged, unusual events noted. A scheduled workflow (#257) runs the snapshots and the automated days under the full-mode trial authorization. S07-R2 is met: canary 43394041693 reconciled `verified` (see below).

**Verified canary (S07-R2):** the 2026-11-12 canaries reconciled `not-observed` because pss5 had no collector ([G2 evidence](../experiments/live/g2-evidence.md#why-the-collections-had-nothing-to-collect)). SPEC-13 placed a Gas Collector Lv1 on pss5 (run 43393262041, 2026-11-13), and canary 43394041693 then reconciled `verified` with minerals +10 ([G2 evidence](../experiments/live/g2-evidence.md#the-verified-canary)).

**The trial itself (S07-AC4, S07-AC5):** pending. It runs from 2026-11-16 to 2026-12-20, and the exit report follows.

## Open questions and risks

Daily figures are serially correlated; the design's interval assumptions are optimistic. The report must say so.
