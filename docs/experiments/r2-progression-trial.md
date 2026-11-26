# R2 progression trial plan (study A, pss5)

**Status: Approved by the owner on 2026-11-13.** This is the plan for [SPEC-07](../specs/SPEC-07-progression-trial.md) S07-R1, drafted by an agent. The owner answered its questions and chose the manual routine (regular play under the rules below) on 2026-11-13. The full-mode authorization is [authorization-g2-pss5-trial.json](live/authorization-g2-pss5-trial.json), merged by the owner in #257, and the trial runs from [live-trial-schedule.yml](../../.github/workflows/live-trial-schedule.yml).

It follows the [study design](study-design.md): one account, a within-account ABBA crossover, the primary metric from snapshot diffs.

## Owner decisions (2026-11-13)

| Question | Decision |
| --- | --- |
| Manual arm | The owner finds pss5 in the game and plays it by hand on manual days, following the routine below. pss5 is the ship with a Gas Collector Lv1 in the top-left corner, about 931 trophies, a level-2 ship and a Bedroom Lv2 |
| Who runs each day | A scheduled workflow, `live-trial-schedule.yml`, added by the owner (it holds secrets). It ran the 23:30 UTC snapshot every day and was to run the 00:30 UTC automated run on automated days. Once the 2026-11-26 schedule change is merged, it runs once a day at 04:30 UTC: the snapshot, then on automated days the automated run in the same job (see [Snapshots and runs](#snapshots-and-runs)). It keeps each day's artifacts for 90 days and does nothing outside the trial dates |
| Daily caps | No practical limit on minerals or gas. The authorization needs a number for each, so both are 1,000,000,000; pss5's balance above its reserve is the real limit. Starbux is always 0 |
| Windows and stop thresholds | Kept as drafted below |

The manual routine below completed the plan on 2026-11-13.

## Account

| Field | Value |
| --- | --- |
| Alias | `pss5` (the only G2 account; [registry](live/registry-g2-pss5.json), `expected_game_identity` recorded) |
| Starting state | [G1 baseline, 2026-11-12](baselines/2026-11-12-g1.md): hull 1-4, game version `1.0+settings.3307714` |
| Legacy run | skipped until 2026-11-26 (`LEGACY_RUN_SKIP=5`; scheduled run 43260995664 showed the step skipped). On 2026-11-26 the owner deleted `LEGACY_RUN_SKIP`, so the legacy daily run acts on pss5 again, serialized with the trial (see [The legacy daily run on pss5](#the-legacy-daily-run-on-pss5)) |
| Ship rooms | On 2026-11-12 pss5 had a Bridge, a Bedroom, a Bolter, three Lifts, an Engine, a Small Reactor and a Mineral Mining Laser, with no collector, no storage and no gas above the reserve. On 2026-11-13 SPEC-13 placed a Gas Collector Lv1 (run 43393262041). Then the owner found pss5 and played it by hand, before the trial (not an intervention). After that play (canary 43401277661) the ship has a Bridge Lv1, a Bedroom Lv2, five Armor rooms, a Bolter Lv2, a Small Reactor Lv2, a Gym, six Lifts, a Mineral Mining Laser, a Mineral Storage, a Gas Storage and a Shield Generator. The Engine and the Gas Collector are gone, and gas is 1,517 above the reserve. The owner kept playing before the trial: by 2026-11-14 (canary 43581425387) the ship also had a Bridge Lv3, a Command Center, a Laboratory, Small Reactors Lv5 and Lv6, Gas Collectors Lv1 and Lv6, Mineral Extractors Lv1 and Lv5, Mineral and Gas Storage Lv5, a Shield Generator Lv6 and missile rooms; see [the G2 evidence](live/g2-evidence.md#the-third-verified-canary). The first evening snapshot (2026-11-16, 23:30 UTC) records the exact starting state. |

## Manual routine (owner)

**The owner plays pss5 the way they normally play** (owner decision, 2026-11-13). Collecting, research, room upgrades, new rooms and PvP are all as they usually would be. The study then asks whether `baseline-v1` does better than the owner's real play, not a scripted routine. Four rules hold on every manual day (the baseline window and blocks 1 and 4):

1. **No starbux on pss5.** The automated arm can never spend starbux. Any starbux spent is logged as an intervention (S07-R6), and that day is excluded or flagged in the report.
2. **No play on pss5 during the automated blocks** (2026-11-30 to 2026-12-13; from the 04:30 UTC run on 2026-11-30 to the 04:30 UTC run on 2026-12-14). Manual play there mixes the arms; if it happens, it is logged as an intervention.
3. **Minutes are logged each manual day** in `operator-minutes.csv` (`manual_minutes`), to compare with the operator time of automated days (S07-R5).
4. **Anything out of the ordinary is noted** in `interventions.md`: a long break, a game event, a big purchase.

The primary metric (completed research plus room upgrades per account-day, from the daily snapshots) is the same in both arms (S07-R4). Anything else the owner does on pss5 shows up in the resource figures.

## Arms and schedule

| Block | Dates (UTC, inclusive) | Arm |
| --- | --- | --- |
| Baseline window | 2026-11-16 to 2026-11-22 | manual (measures the metric's mean and sd only; nothing is decided from it) |
| 1 | 2026-11-23 to 2026-11-29 | manual |
| 2 | 2026-11-30 to 2026-12-06 | automated (`baseline-v1`) |
| 3 | 2026-12-07 to 2026-12-13 | automated (`baseline-v1`) |
| 4 | 2026-12-14 to 2026-12-20 | manual |

Before block 1 opens (2026-11-23), a canary run must succeed (S07-R2). Success means its collection reconciles `verified`: a later observation shows minerals or gas increased. A `not-observed` result does not count, because it shows only that the call went through, not that the trial can measure an effect. The 2026-11-12 canaries were `not-observed`, because pss5 had no collector. After SPEC-13 placed a Gas Collector Lv1, canary 43394041693 (2026-11-13) reconciled `verified` (minerals +10), so **this precondition is met**. If no canary is verified by 2026-11-23, block 1 does not open and the schedule moves back. The plan is re-sized from the baseline window's measured sd.

## Snapshots and runs

`live-trial-schedule.yml` runs once a day at **04:30 UTC**. Until the 2026-11-26 change is merged it ran a 23:30 UTC snapshot and was to run a separate 00:30 UTC automated run; it moved so that it no longer runs near the legacy daily run's 00:00 UTC start (see [The legacy daily run on pss5](#the-legacy-daily-run-on-pss5)).

- **Every day, both arms:** one read-only snapshot of `pss5` (`run.py --baseline` with the G2 registry). The 04:30 UTC snapshot on date D closes trial day D-1, so the runs from 2026-11-17 to 2026-12-21 close trial days 2026-11-16 to 2026-12-20. The primary metric comes only from consecutive snapshot diffs (S07-R4).
- **Automated days:** in the same job, after the snapshot, a dry run, then `run.py --fleet-execute --mode full` under the trial authorization with the trial registry ([registry-g2-pss5-trial.json](live/registry-g2-pss5-trial.json), which leaves mineral and gas budgets to the authorization). The run on date D opens trial day D, so the runs from 2026-11-30 to 2026-12-13 open the automated days. Running both in one job means a day's snapshot always comes before that day's execute run, even when GitHub starts the run late. The execute run does not depend on the snapshot succeeding, as when they were separate runs. A trial day therefore runs from about 04:30 UTC to about 04:30 UTC the next day, inside the trial authorization's window (2026-11-30T00:00Z to 2026-12-14T00:00Z).
- **Artifacts:** a `trial-snapshot-<run id>` artifact every day and a `trial-execute-<run id>` artifact on automated days, each kept 90 days, as before.
- **Stopping the automated arm:** commit `docs/experiments/r2-progression-trial/STOP`, or disable the workflow. The stop file stops the execute run only; the snapshot still runs. Agents report a crossed threshold; the owner stops the arm.
- **Manual days:** no execute run; the owner follows the manual routine.
- **The switch:** the pair from the last 23:30 snapshot to the first 04:30 snapshot is not 24 hours long. If the change merges on date D before the 23:30 UTC run, that pair is about 29 hours, outside the exit report's 20 to 28 hours, so manual day D is not counted. If it merges after D's 23:30 run has finished and before 04:30 UTC on D+1, no day is lost; the extra five-hour pair is left out. Merged before 2026-11-30, the day at stake is a block 1 (manual) day.

## The legacy daily run on pss5

**From 2026-11-26, the legacy daily run (`daily-run.yml`) also acts on pss5, three times a day (00:00, 08:00 and 16:00 UTC), on both arms: automated days and manual days.** It collects resources and starts research, room upgrades and crew training, and it runs PvP battles. Research completions and room upgrades are the trial's growth-per-day metric, so its actions feed the metric on every day of both arms. Its actions are not journaled. **Owner consideration:** decide how this affects the interpretation of the results, for example whether it is logged under [Interventions](#interventions-s07-r6) as a legacy-run action (S06-R3) and how the exit report treats it. This plan does not decide that.

The legacy daily run and the trial never run on pss5 at the same time:

- `daily-run.yml`, `live-trial-schedule.yml` and `live-trial.yml` share one workflow-level concurrency group, `live-trial`, with `cancel-in-progress: false`. GitHub runs at most one of them at a time; the others wait. **This group is the guarantee.** The 04:30 UTC time only makes waiting less likely: GitHub often starts the daily run's scheduled runs 0 to 7 hours late, so it can still be running at 04:30.
- The pss5 authorization files claim this with `legacy_run_serialized: [5]` ([SPEC-06](../specs/SPEC-06-legacy-daily-run.md) S06-R6), and `scripts/ci.py repository` refuses a change that drops or splits the shared group.
- **Limitation:** a GitHub concurrency group holds at most one running and one pending run. A third run that queues cancels the pending one (it does not wait). Scheduled runs are short (the daily run takes about 2 to 5 minutes), so this needs three runs within a few minutes. That is unlikely but realistic in one case: a run is in progress, another is waiting, and the owner dispatches `live-trial.yml` or `daily-run.yml` by hand. A dispatch whose job is then skipped (for example `live-trial.yml` from a branch other than `main`) still takes the pending place. If the cancelled run is the trial's, that day has no snapshot, so two days lose their one-day pair. Before a manual dispatch, check that no pss5 workflow is running or waiting.
- **Scope of the guarantee:** it covers only those three workflows, as they are on `main`. Dispatch `daily-run.yml` only from `main`: a run from an older branch uses that branch's file, which may have no concurrency group. `daily-run-langgraph.yml` also logs in to pss5 (00:00, 08:00 and 16:00 UTC) and is not in the group; the [runbook](live/RUNBOOK.md) has the owner disable it while the trial lasts, and the serialized claim holds only while it stays disabled. A `--fleet-execute` run started anywhere else (a local run, another workflow) is not serialized either.

## Primary metric and sample

- **Metric:** growth velocity, `growth-per-day`: completed research plus room upgrades per account-day, from snapshot diffs (study design §Metrics).
- **Smallest effect worth detecting:** 1.5 sd, the largest a four-block single-account study can detect.
- **Sample size:** `per_arm_min_sample` gives 14 account-days per arm for a 1.5 sd effect (29 for 1.0 sd), at alpha 0.05 and power 0.80. Four 7-day blocks give exactly 14 per arm.
- **Decisions:** only at block ends, with `decide_and_record`; no decision before the minimum sample.

## What changed each day

`scripts/daily_changes.py` summarises each day from the daily snapshots. A snapshot taken before 12:00 UTC counts for the day before, so a 04:30 UTC snapshot is labelled with the trial day it closes, as the 23:30 snapshots were. It shows:

- the day's block and arm, from [schedule.json](r2-progression-trial/schedule.json);
- the trial's growth measure, computed as the exit report computes it;
- rooms upgraded, added and removed (by design, never by room id), and room upgrades started;
- research completed and started;
- the change in resources, trophies and ship level, and crew level-ups;
- anything a snapshot did not observe.

It reads files only. Point it at downloaded `trial-snapshot-*` artifacts:

```bash
gh run download <run id> -D trial-artifacts/<run id>
python scripts/daily_changes.py trial-artifacts --alias pss5 --schedule docs/experiments/r2-progression-trial/schedule.json
```

It helps check the minutes and interventions logs, and it replaces reading screenshots. The output describes the account, so it is not committed.

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

`docs/experiments/live/authorization-g2-pss5-trial.json`, drafted for the owner to review in the same patch as the workflow: account `pss5`; `modes` `["full"]`; the actions `baseline-v1` plans (`add_research`, `upgrade_room`, `collect_all_resources`, `collect_daily_reward`); mineral and gas caps of 1,000,000,000 each (no practical limit); starbux cap 0; valid 2026-11-30T00:00Z to 2026-12-14T00:00Z (the automated blocks only); `legacy_run_skipped` `[5]`, changed to `legacy_run_serialized` `[5]` on 2026-11-26 when the legacy daily run started acting on pss5 again. The canary authorization ([authorization-g2-pss5-canary.json](live/authorization-g2-pss5-canary.json)) expires 2026-11-26 and allows canary mode only.

## Exit report

`docs/experiments/r2-progression-trial-report.md`, per S07-R7, generated with `scripts/trial_report.py` from the snapshots, journals and the two logs above. The snapshot that closes the last trial day (2026-12-20) runs at 04:30 UTC on 2026-12-21, so the report plan's `window_end` must be after that run (for example 2026-12-22T00:00:00Z), or the last day has no closing snapshot. Only the report and summary tables are committed (S07-Q2).
