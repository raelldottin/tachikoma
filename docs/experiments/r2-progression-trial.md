# R2 progression trial plan (study A, pss5)

**Status: DRAFT, not approved.** This is an agent-drafted proposal for [SPEC-07](../specs/SPEC-07-progression-trial.md) S07-R1. Its safe default (S07-Q1) is that the trial does not start. It starts only when the owner fills in the manual routine, changes or confirms every value below, sets this line to `Approved by the owner on <date>`, and writes the full-mode authorization in [Authorization](#authorization). Nothing here authorizes a live run.

It follows the [study design](study-design.md): one account, a within-account ABBA crossover, the primary metric from snapshot diffs.

## Account

| Field | Value |
| --- | --- |
| Alias | `pss5` (the only G2 account; [registry](live/registry-g2-pss5.json), `expected_game_identity` recorded) |
| Starting state | [G1 baseline, 2026-11-12](baselines/2026-11-12-g1.md): hull 1-4, game version `1.0+settings.3307714` |
| Legacy run | skipped (`LEGACY_RUN_SKIP=5`; scheduled run 43260995664 showed the step skipped) |

## Manual routine (owner)

*To be written by the owner.* A list of what is done in the game each day of a manual block, in order and at what times, so the manual arm is a defined treatment. For example: collect resources at about 08:00 and 20:00 UTC, start the cheapest affordable research, upgrade one room when affordable, collect the daily reward, no starbux spending, no PvP.

## Arms and schedule

| Block | Dates (UTC, inclusive) | Arm |
| --- | --- | --- |
| Baseline window | 2026-11-16 to 2026-11-22 | manual (measures the metric's mean and sd only; nothing is decided from it) |
| 1 | 2026-11-23 to 2026-11-29 | manual |
| 2 | 2026-11-30 to 2026-12-06 | automated (`baseline-v1`) |
| 3 | 2026-12-07 to 2026-12-13 | automated (`baseline-v1`) |
| 4 | 2026-12-14 to 2026-12-20 | manual |

Before block 1 opens, a canary run must succeed and reconcile (S07-R2). The plan is re-sized from the baseline window's measured sd.

## Snapshots and runs

- **Every day, both arms:** one read-only snapshot of `pss5` at 23:30 UTC (`live-trial.yml`, `baseline` step, or `run.py --baseline` with the G2 registry). The primary metric comes only from consecutive snapshot diffs (S07-R4).
- **Automated days:** `run.py --fleet-execute --mode full` once at 00:30 UTC under the trial authorization, then the 23:30 snapshot.
- **Manual days:** no execute run; the owner follows the manual routine.

## Primary metric and sample

- **Metric:** growth velocity, `growth-per-day`: completed research plus room upgrades per account-day, from snapshot diffs (study design §Metrics).
- **Smallest effect worth detecting:** 1.5 sd, the largest a four-block single-account study can detect.
- **Sample size:** `per_arm_min_sample` gives 14 account-days per arm for a 1.5 sd effect (29 for 1.0 sd), at alpha 0.05 and power 0.80. Four 7-day blocks give exactly 14 per arm.
- **Decisions:** only at block ends, with `decide_and_record`; no decision before the minimum sample.

## Operator time (S07-R5)

One row per account-day in both arms, in `docs/experiments/r2-progression-trial/operator-minutes.csv`:

```text
date,account_alias,arm,manual_minutes,operator_minutes
2026-11-23,pss5,manual,25,0
2026-11-30,pss5,baseline-v1,0,3
```

`manual_minutes` is time spent playing; `operator_minutes` is time spent dispatching, checking and fixing automation.

## Interventions (S07-R6)

Logged when they happen in `docs/experiments/r2-progression-trial/interventions.md`: UTC time, alias, active arm, kind (premium spending, legacy-run action, game update, manual play during an automated window, outage), description and action taken (continue, exclude the day, split the block, abort the arm).

## Stop thresholds (S07-R8, proposed)

The automated arm stops, and the report says so, at the first of:

1. any `critical` alert (`isolation-incident`, `budget-exceeded`);
2. any starbux spent by a fleet run;
3. an identity mismatch or an isolation refusal for `pss5`;
4. `failure-rate-high` or `uncertain-rate-high` on two consecutive automated days;
5. a non-idempotent action with an uncertain outcome that the owner cannot resolve by the next day's snapshot.

A game update during a block splits the block (study design §Interventions).

## Authorization

*To be written by the owner* as `docs/experiments/live/authorization-g2-pss5-trial.json`: account `pss5`; `modes` `["full"]`; the actions `baseline-v1` plans (`add_research`, `upgrade_room`, `collect_all_resources`, `collect_daily_reward`); mineral and gas caps per day; starbux cap 0; valid 2026-11-30 to 2026-12-14 (the automated blocks only); `legacy_run_skipped` `[5]`. The canary authorization ([authorization-g2-pss5-canary.json](live/authorization-g2-pss5-canary.json)) expires 2026-11-26 and allows canary mode only.

## Exit report

`docs/experiments/r2-progression-trial-report.md`, per S07-R7, generated with `scripts/trial_report.py` from the snapshots, journals and the two logs above. Only the report and summary tables are committed (S07-Q2).
