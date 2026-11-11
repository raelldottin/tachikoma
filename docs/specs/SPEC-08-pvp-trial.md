# SPEC-08: Live PvP strategy comparison

**Status:** Draft
**Release:** R3 exit (study C)
**Owner gate:** G3
**Depends on:** SPEC-03, SPEC-05, SPEC-07
**Implements roadmap item:** queue slice `r3-live-pvp-trial`; "R3 exit: reproducible battle trials, offense/defense scorecards, opponent-normalized comparisons and an evidence-based recommendation"

## Problem

Strategies are versioned data, battles are a catalog action, and the scorecard compares strategies, but nothing applies a strategy to a ship. Every live outcome is `unknown` until SPEC-03 verifies the trophy and battle-history reads.

## Scope

- **In:** applying (or recording the application of) a strategy, scheduled battle windows, outcome evidence, and the comparison report.
- **Out:** new strategies, and changing `Client.runBattleEndToEnd`.

## Requirements

- **S08-R1 (MUST)** Each experiment window records which strategy version was in force, and how it was applied: by a client call identified from captures, or by the owner by hand with a timestamp. Battles outside a recorded application have no strategy.
- **S08-R2 (MUST)** `run_pvp_battle` is included in an authorization only for G3 windows, with a per-window battle cap.
- **S08-R3 (MUST)** A battle counts toward the comparison only when its outcome is known under S03-R5. Unknown outcomes are reported in their own count.
- **S08-R4 (MUST)** The comparison uses `compare_strategies` with the minimum sample from `sdk.experiments.study`, stratified by opponent cohort when cohorts are observed, and reports `insufficient-evidence` rather than a recommendation when the sample is short.
- **S08-R5 (MUST)** Defense results come from battle history, not from attack runs, and are reported separately.
- **S08-R6 (MUST)** The exit report records the game version, battle counts per strategy, win rates with intervals, net trophies, resource cost, the recommendation or its absence, and limitations.
- **S08-R7 (SHOULD)** If no client call can apply native AI settings, the spec records that strategies were applied by hand, and the report treats application time as approximate.

## Interfaces

- Experiment windows from `sdk/pvp/strategies.py`, with an application log in `docs/experiments/r3-pvp-trial/applications.json`.
- `scripts/trial_report.py --pvp` extends SPEC-07's report.

## Safety invariants

- Battles only inside authorized windows, up to the cap.
- A finalise response is never a win.
- Unknown outcomes are reported, not dropped.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S08-AC1 | R1 | offline | Battles are attributed to the strategy whose recorded application covers their start time, and to none otherwise |
| S08-AC2 | R3, R4, R5 | offline | Synthetic journals and battle histories produce the right known/unknown split, stratified comparison and `insufficient-evidence` on short samples |
| S08-AC3 | R2 | offline | An authorization without `run_pvp_battle` refuses battle actions; the cap stops further battles |
| S08-AC4 | R6 | live | The owner runs both windows and commits the report |

## Test plan

Extend `tests/test_pvp_scorecard.py`, `tests/test_battle_results_from_journal.py` and `tests/test_fleet_execute.py`.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec08-application-log` | R1, R7 | `sdk/pvp/strategies.py`, tests, `docs/baseline/pvp-strategies.md` | agent |
| `spec08-pvp-report` | R3–R6 | `scripts/trial_report.py`, `sdk/reports/pvp.py`, tests | agent |
| `spec08-authorization-cap` | R2 | `sdk/fleet/authorization.py`, tests | agent |
| `spec08-run-trial` (replaces `r3-live-pvp-trial`) | AC4 | `docs/experiments/*` | **owner** |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S08-Q1 | Which two strategies, and the window lengths? | trial plan | The two candidate strategies, two-week windows |
| S08-Q2 | Applied by the bot (needs a captured call) or by hand? | `spec08-application-log` | By hand |
| S08-Q3 | Battle cap per window | authorization | 10 per day |

## Evidence

Pending.

## Open questions and risks

Thirty known-result battles per strategy may take weeks. Matchmaking may change between windows.
