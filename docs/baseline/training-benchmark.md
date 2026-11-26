# Crew-training benchmark

`scripts/training_benchmark.py` scores the crew-training planner (`sdk/training`) on a fixed suite of simulated worlds and fails CI when the score drops. Each scenario runs for 7 to 10 simulated days in the offline simulator (`sdk.simulation.game`), so there is no network, no account and no credential anywhere in the run. Independent oracles flag hard policy violations (acting at or above the TP cap, consumables outside their rules, training past an unmet research gate). A second mode, qa-eval, mutates the planner and the strategy and measures how many mutants the benchmark and the unit tests catch.

The benchmark measures the owner-approved strategy as it is. It never tunes the planner or the strategy to raise a score.

## Usage

```bash
make training-benchmark                                  # the CI gate: exit 0 pass, 1 regression, 2 input error
make training-benchmark ARGS="--update-baseline"         # rewrite baseline.json (only ever up)
make training-benchmark ARGS="--update-baseline --allow-lower 'reason'"
make training-benchmark ARGS="--quick"                   # 2-day runs, report only
make training-benchmark ARGS="--qa-eval --jobs 8"        # local only, about 15 minutes
```

| Flag | Meaning |
| --- | --- |
| `--gate` | The default mode: run the suite twice in-process, compare the reports byte for byte, gate against `baseline.json`. |
| `--scenario NAME` | Run only this scenario (repeatable). Only the listed scenarios are gated (each must be in the baseline), and the composite is not compared. |
| `--quick` | Override every scenario to 2 days. Report only, never gated (faults after day 1 do not fire). |
| `--seed N` | Override every scenario's seed. Report only, never gated. |
| `--json PATH` | Write the JSON report, with a `gate` object in gate mode. `-` prints it instead of the markdown. |
| `--markdown PATH`, `--summary PATH` | Write, or append (for `$GITHUB_STEP_SUMMARY`), the markdown report. |
| `--quiet` | Print nothing. |
| `--scenarios-dir`, `--baseline` | Other inputs (the tests use temporary ones). |
| `--update-baseline`, `--allow-lower REASON` | See [Gate and baseline](#gate-and-baseline). |
| `--qa-eval`, `--jobs`, `--timeout`, `--only ID...`, `--qa-target PATH...`, `--channels tests,benchmark` | See [qa-eval](#qa-eval). |

Flags a mode would ignore are a usage error (exit 2): `--gate` with `--update-baseline`, `--quick` or `--seed`, and `--qa-eval` with `--gate`, `--update-baseline`, `--quick`, `--seed` or `--scenario`.

The CI job `training-benchmark` in `.github/workflows/python-tests.yml` runs `make training-benchmark ARGS="--gate --json training-benchmark.json --summary $GITHUB_STEP_SUMMARY"` and uploads the JSON report. It is blocking: `blocking-ci.yml` calls that workflow as its `tests` job, which `CI required` needs. Locally the gate takes about 3 seconds (the 15 scenarios, run twice).

## World catalog

Every scenario is built from one fixed catalog of literal tables in the script, so each record parses for both `sdk.training.build_context` and the simulator. All ids are positive numeric strings, because `build_context` drops any other id.

| Table | Contents |
| --- | --- |
| `CATALOG_ROOMS` | GYM 1000, Galaxy Gym 1001 (upgraded from 1000), Academy 1100, Lunar College 1101 (from 1100), Bridge 1200 (not a training room). Records carry `@RoomDesignId`, `@RoomName` and `@UpgradeFromRoomDesignId`. |
| `CATALOG_RESEARCH` | Fitness 2010, Fitness Lv2 2011, Fitness Lv3 2012, Education 2020, Education Lv2 2021, Education Lv3 2022. Records carry `@ResearchName` and `@RootResearchDesignId`, so `parse.py` derives the family and the level from the "Lv N" suffix. |
| `CATALOG_TRAININGS` | The 21 training names of `DEFAULT_ROLES`: 12 in the Academy (3011-3043: Weapon, Science, Engine, Pilot) and 9 in the GYM (3111-3133: Attack, Stamina, Hp), tiers 1 to 3. |
| `TIER_PARAMS` | T1: 1 h, +10 fatigue, gain 1-3. T2: 3 h, +20, gain 5-9, needs the Lv2 research of the room's family. T3: 6 h, +30, gain 12-18, needs Lv3. |
| `CATALOG_CHARACTERS` | common (Common 60), elite (Elite 100), unique (Unique 100), epic (Epic 100), hero (Hero 120), special (Special 120), captain (Special 200), legendary (Legendary 150), standard (Standard 50): rarity and `@TrainingCapacity`. |
| `CATALOG_ITEMS` | Common and Hero consumables for ATK, WPN, SCI, ENG, STA and HP (5001-5006, 5011-5016). Common items add 2 points; Hero items add 5 and recover 50 fatigue. |

A training design record carries the simulator keys (`@TrainingStat`, `@Duration`, `@Fatigue`, `@MinGain`, `@MaxGain`, `@RequiredRoomDesignId`, `@RequiredResearchDesignId`) and the planner keys (`@TrainingName`, `@Rank`, `@<Stat>Chance`, `@RequiredRoomType`). `build_world` checks that `parse_consumable_designs` finds exactly `CATALOG_CONSUMABLES`, and attaches those ids with `with_consumables`. That is the only strategy change the benchmark makes.

## Executor loop

`run_tick` follows the documented executor contract, nothing more:

1. Observe: the four `list*` reads, plus a snapshot read when the scenario injects read faults. A failed read skips the tick.
2. `ctx = build_context(...)`, then `plan = plan_training(ctx, strategy)`.
3. Carry out each decision through the action catalog: `action_for`, then `get_spec(...).client_method` with `call_arguments`.
4. A primary decision that follows a FinishTraining is advisory and never runs as is. When the finish succeeded, the crew is rebuilt from the finish response with `parse_crew`. Its decision is replaced by `replan_after_finish(ctx, crew, strategy, stock)`, where `stock` starts as `remaining_stock(ctx.items, plan, exclude_character_ids=<finished>)` and shrinks with each replan. When the finish was refused, raised or ambiguous, the advisory decision is dropped and the crew is picked up on a later tick.

`run_episode` calls `env.start_day(day)` and then runs `1440 / tick_minutes` ticks per day (60 minutes by default). Every catalog duration is a whole number of hours, so a planner that restarts at once loses no time to polling.

The loop is injectable: `planner`, `replanner`, `strategy`, `environment_factory` and `tick`. The swap point for Unit 9's executor (`sdk/execution/training.py`) is `tick`: any callable taking `run_tick`'s keywords, driving `env.game` (which implements the Client surface) and reporting to the `Recorder`.

## Scenarios

The files live in `automation/benchmark/training/scenarios/`. Every file's stem equals its `name`. The checked-in suite must have at least 7 days per scenario and cover every required tag, and its faults must cover every allowed fault kind. A test runs each fault scenario and checks that every fault fires.

| Scenario | Tag | Days | What it stresses |
| --- | --- | --- | --- |
| fresh-crew | fresh | 7 | Untrained crew: primary T1 to T3, then the 72% "move to gym" boundary. |
| mid-level-crew | mid-level | 7 | Band edges 50/51, 64/65 and 71/72; the exceptional cap of Huge Hellaluya. |
| near-cap-crew | near-cap | 7 | Every rarity a few points below its cap, or at it; tp_cap_reached; cap overshoot. |
| missing-research | missing-research | 7 | No research: the primary T1 gate, the ungated secondary T1 band, the research_missing consumable fallback. |
| mixed-rooms | mixed-rooms | 7 | All four room names and their upgrade lineage; crew in the wrong room type; band gates met through Education only. |
| no-training-room | no-training-room | 7 | Crew on the Bridge, in no room, or without a role; the consumable role and room rules. |
| fatigued-crew | fatigued | 7 | Fatigue 99/100 with slow recovery (720 s per point): the fatigue gate and the Hero rule. |
| consumable-windows | consumables | 7 | Exact consumable rule edges: 29/30%, fatigue 0/1, 89/90%, fatigue 99/100. |
| long-run | multi-day | 10 | Twelve crew, all four rooms, partial research: saturation and runtime. |
| fault-reject | fault-reject | 7 | Refused start, finish and item use: retried, never counted as planner refusals. |
| fault-contradicted | fault-contradicted | 7 | Two accepted starts that run another design (Kickbox, Big Book of Science). |
| fault-raise | fault-raise | 7 | A start with effect and a finish without effect both raise. |
| fault-ambiguous | fault-ambiguous | 7 | A finish with effect and a start without effect both return no answer. |
| fault-snapshot | fault-snapshot | 7 | A failed snapshot read and a failed read after a start. |
| fitness-only | research-split | 7 | Only Fitness and Fitness Lv2: the band gate accepts either family, but each design needs its own. |

The schema is strict: an unknown key is an error, and `validate_scenario` reports every problem at once.

- Top level: `schema_version` 1, `name` (`^[a-z0-9-]+$`), `description`, `tags`, `seed` (>= 0), `start` (ISO 8601 with an offset), `days` (1-30), optional `tick_minutes` (5-240, divides 1440, default 60) and `fatigue_recovery_seconds` (default 360), `rooms`, `research`, `inventory`, `crew` and `faults`.
- `rooms`: `[{"id", "design"}]`, where `design` is a `CATALOG_ROOMS` name.
- `research`: unique `CATALOG_RESEARCH` names.
- `inventory`: `{item name: quantity}`.
- `crew`: `{"id", "name", "design", "room"}` (room 0 means none) plus optional `fatigue` (0-100), `level`, `stats` (simulator stat names; the total must stay within capacity) and `training` (`{"design", "ends_after_minutes"}` for a session already running).
- `faults`: `{"kind", "method", "day"?, "call"?, "probability"?}`, where the kind is one of `reject`, `contradicted-effect`, `raise`, `ambiguous-response` and `snapshot-failure`. `raise` and `ambiguous-response` take an optional `effect`; `contradicted-effect` needs a `training_design` (a catalog name). A fault `day` must be before `days` (a later one would never fire). The simulator's `Fault` re-validates every fault.

## Metrics and score

E is the set of eligible crew: those with a role in the oracle table and a room whose catalog type is a training room. For each crew member i, `L_i = oracle_cap(...)`, `T0_i` is the starting total and `headroom_i = max(0, L_i - T0_i)`. H is the sum of the headroom over E; `build_world` refuses a scenario with H = 0.

| Metric | Definition |
| --- | --- |
| P, progress AUC | `sum over days d of G_d / (D * H)`, where `G_d = sum over E of min(headroom_i, max(0, T_i(d) - T0_i))` and `T_i(d)` is the total at the end of day d. P rewards getting there sooner. |
| U, utilisation | `1 - idle / eligible` crew-seconds. A crew's window runs from the start until its first finish or item use that reaches its cap or 90% of capacity, or until the horizon. Busy time is the union of its sessions; collection delay and "move to" waits count as idle. A crew that starts at its cap or at 90% has no window. U is 1.0 when there is no window at all. |
| F, focus | TP gained in the role's focus stats (weapons: Weapon/Attack, shields: Science/Attack, engines: Attack/Engine, rushers: Stamina/Attack, defenders: Attack/Hp), as a share of all TP gained by E. 0 when nothing was gained. |
| R, reliability | `accepted / (accepted + refused)` executed calls. A refusal caused by an injected `reject` fault does not count. A call with no reply (an `ambiguous-response` fault, including on `activateItem3` behind `useConsumable`) is ambiguous, neither accepted nor refused. 1.0 when nothing ran. |

```text
score     = 0                                              if the run errored, or any hard violation occurred
score     = round(100 * R * (0.65 * P + 0.25 * U + 0.10 * F), 6)   otherwise
composite = round(mean(scenario scores), 6)
```

- The score is bounded in [0, 100]. A score of 100 would need all headroom gained on day 1, no idle time and no refusal.
- A no-op planner scores 0: it gains nothing (P = F = 0) and is idle all the time (U = 0). A test checks this on every suite scenario.
- A weakened planner scores lower:
  - Lazy starts raise idle time.
  - Wrong rooms or designs cause refusals, so R drops.
  - Unused consumables lower P.
  - Breaking a cap, consumable or research rule zeroes the scenario.
  - Tests check that a lazy planner, a narrower `room_types` and a strategy without consumables each score strictly lower.

Reported only, never scored: `tp_gained`, `tp_per_crew_day`, idle and eligible crew-hours, actions per method, `refusal_codes`, `zero_gain_sessions`, `wasted_actions`, the fault counters (`fault_rejected`, `contradicted`, `raised`, `ambiguous`, `observe_failures`), `dropped`, `replans`, `cap_overshoot_tp`, the `skip_reasons` histogram, `skip_moves` and `consumables_used`.

## Oracles

The oracle tables (`ORACLE_ROLES`, `ORACLE_FOCUS`, `ORACLE_RARITY_STARS`, `ORACLE_TP_CAPS`, `ORACLE_EXCEPTIONAL_CAPS`, `ORACLE_CAPTAIN_CAPS`, `ORACLE_BANDS`, `ORACLE_COMPLETE_PERCENT`, `ORACLE_GATES`, `ORACLE_CONSUMABLE_RULES`) are independent literal copies of the owner-approved policy. They are never imported from `sdk/training/strategy.py`, so a mutant of the strategy cannot move the oracle with it. `test_oracle_tables_agree_with_policy_sources` checks them against `DEFAULT_STRATEGY` and `sdk.crew_leveling`. A deliberate policy change must update both sides in the same change.

Each oracle judges the crew state its decision was made from (the rebuilt crew for a replan) and the research observed in the same tick:

| Violation | Rule |
| --- | --- |
| `cap_violations` | An executed StartTraining or UseConsumable while the crew's total is at or above its oracle cap. |
| `consumable_violations` | An executed UseConsumable whose item is not a catalog consumable, whose tier rule does not allow the crew's percent and fatigue, whose crew has no role, or whose crew is not in a training room. |
| `research_gate_violations` | An executed StartTraining below 90% whose band gate is not met by the observed research. |

**Cap overshoot is reported, not gated.** The planner cannot know a session's gain in advance, so a session started below the cap can end above it. Gating "total above cap" would fail the unmutated planner, so the gate counts acting at or above the cap, and `cap_overshoot_tp` reports the overshoot: the TP above `max(cap, starting total)`, so a crew that started above its cap only counts what it gained.

The unmodified planner triggers no oracle in any scenario.

## Gate and baseline

`automation/benchmark/training/baseline.json` holds the composite, each scenario's score and trace digest, the `lowered` history and the `qa_eval` settings. The gate fails with these reasons:

- `nondeterministic`: the two in-process reports differ.
- `baseline_missing`: no baseline file (exit 2).
- `baseline_mismatch`: the scenario set differs from the baseline (with `--scenario`: a listed scenario is not in it).
- `composite_below_baseline`.
- `scenario_below_baseline:<name>`.
- `cap_violation:<name>`, `consumable_violation:<name>`, `research_gate_violation:<name>`.
- `scenario_error:<name>`.

The trace digest is a sha256 of the decision trace and the simulator log. A digest that differs from the baseline is reported as drift (behaviour changed while the scores held). Drift is a warning, never a failure. An errored scenario is a `scenario_error`, never drift.

`--update-baseline` always runs the determinism check first. It refuses (exit 1) a nondeterministic run, a scenario error or any hard violation. It writes the file when every score is at or above the old one; equal scores refresh the digests, and new scenarios are added. Any lower score, a removed scenario, or a lower composite over the same scenario set is refused (a new scenario that scores below the mean is not a regression) unless `--allow-lower REASON` gives a non-empty reason, which is then appended to `lowered`. The `qa_eval` block is always kept as it is.

### Measured baseline

These are the scores of the unmodified planner as checked in (seeded, byte-identical across runs and across `PYTHONHASHSEED` values):

| Scenario | Score | P | U | F | R | TP gained | Idle h / eligible h | Overshoot TP | Consumables |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| consumable-windows | 12.963526 | 0.046 | 0.000 | 1.000 | 1.000 | 11 | 840 / 840 | 0 | 4 |
| fatigued-crew | 35.950397 | 0.015 | 0.998 | 1.000 | 1.000 | 6 | 1 / 504 | 0 | 1 |
| fault-ambiguous | 88.535491 | 0.828 | 0.988 | 1.000 | 1.000 | 362 | 2 / 169 | 15 | 0 |
| fault-contradicted | 81.124174 | 0.887 | 0.544 | 0.985 | 1.000 | 344 | 131 / 287 | 24 | 0 |
| fault-raise | 88.334199 | 0.823 | 0.994 | 1.000 | 1.000 | 358 | 1 / 165 | 18 | 0 |
| fault-reject | 90.537546 | 0.864 | 0.974 | 1.000 | 1.000 | 163 | 2 / 78 | 13 | 0 |
| fault-snapshot | 95.216306 | 0.944 | 0.954 | 1.000 | 1.000 | 248 | 5 / 109 | 8 | 0 |
| fitness-only | 37.738796 | 0.407 | 0.051 | 1.000 | 1.000 | 57 | 484 / 510 | 0 | 0 |
| fresh-crew | 83.666527 | 0.892 | 0.626 | 1.000 | 1.000 | 465 | 127 / 340 | 32 | 0 |
| long-run | 70.071850 | 0.832 | 0.240 | 1.000 | 1.000 | 767 | 1124 / 1478 | 25 | 0 |
| mid-level-crew | 81.582512 | 0.991 | 0.286 | 1.000 | 1.000 | 133 | 155 / 217 | 18 | 0 |
| missing-research | 13.347786 | 0.050 | 0.003 | 1.000 | 1.000 | 14 | 670 / 672 | 0 | 6 |
| mixed-rooms | 42.932331 | 0.466 | 0.105 | 1.000 | 1.000 | 138 | 612 / 684 | 0 | 0 |
| near-cap-crew | 94.428571 | 0.914 | 1.000 | 1.000 | 1.000 | 133 | 0 / 52 | 37 | 0 |
| no-training-room | 61.342687 | 0.733 | 0.149 | 1.000 | 1.000 | 52 | 143 / 168 | 0 | 0 |
| **composite** | **65.184847** | | | | | | | | |

## qa-eval

qa-eval runs `scripts/mutation.py`'s `run_mutants` over `sdk/training/planner.py` and `sdk/training/strategy.py`, with a custom test runner that runs two channels in every mutant workspace:

- **tests**: `scripts/ci.py test --names tests.test_training_planner tests.test_training_parse tests.test_training_benchmark`.
- **benchmark**: the gate (`training_benchmark.py --gate --json - --quiet`) in a subprocess inside the workspace, because the mutated planner can only be imported there. Exit 0 is a pass. Exit 1 is "detected", with the gate's reasons. Any other exit, including an import-time crash from a broken `DEFAULT_STRATEGY`, is a "crash", and running out of time is a "timeout". Both of these count as detected.

The per-mutant timeout (`--timeout`, default 300 s) is split evenly between the channels. The workspaces copy `sdk`, `tests`, `scripts/ci.py`, `scripts/mutation.py`, `scripts/training_benchmark.py` and `automation/benchmark/training`. The unmutated run must pass both channels.

Each mutant is classified as `both`, `tests-only`, `benchmark-only`, `drift-only` (neither channel caught it, but the trace changed), `neither`, `equivalent` or `incompetent`.

```text
eligible        = mutants - incompetent - equivalent
detection_rate  = |tests or benchmark| / eligible        gated: >= qa_eval.min_detection_rate (0.9), exit 1 below
observable_rate = |tests or benchmark or drift| / eligible   reported only
```

Drift is never counted in the headline rate. It flags every behaviour change, including neutral ones, and it does not fail the CI gate. If the rate falls below 0.9, the fix is better scenarios or oracles, never counting drift.

Equivalent mutants are listed in `baseline.json` under `qa_eval.equivalent`, each as `{id, original, replacement, reason}`. An entry whose id no longer exists, or whose text changed (ids are line:column based), is reported as stale and ignored.

A smoke run of three mutants (`--only sdk/training/planner.py:374:13:compare:0 sdk/training/planner.py:390:20:compare:0 sdk/training/strategy.py:313:25:constant:0 --jobs 2`) took 24 s. The two planner mutants (the cap check and the fatigue gate flipped) were caught by both channels. The `max_training_fatigue` 100 to 101 mutant was caught by the tests only; the benchmark saw drift in fatigued-crew but no score change, because training at fatigue 100 gains nothing either way.

### qa-eval results

> **Placeholder: to be filled by the benchmark step.** Run `make training-benchmark ARGS="--qa-eval --jobs 8 --markdown qa.md"` and paste the summary table, the "caught only by the benchmark", "caught by neither" and "drift only" tables, and the equivalent-mutant list with a reason for each entry.

## Known gaps

- **No room moves.** Neither the simulator nor the client can move crew between rooms. Weapons, shields and engines crew wait on `not_in_training_room` "move to ..." from 72%. Every planner pays the same cost and the baseline absorbs it. It shows in `skip_moves` and in the idle hours.
- **Snapshot faults** are modelled only as failed observations: the tick is skipped. The planner never sees a stale or foreign snapshot. The snapshot read does not move the simulated clock, so a read-fault scenario keeps the same tick times as a fault-free one.
- **Cap overshoot** is reported, not gated (see [Oracles](#oracles)).
- **Research never arrives mid-run.** The simulator completes "Researching" entries at a day boundary, so scenarios list only completed research.
- **The script's own mutation target** is not in `automation/mutation/config.json`, because `tests/test_mutation_harness.py` requires every target path to start with `sdk/`. Measure it with `python scripts/mutation.py --target scripts/training_benchmark.py --tests tests.test_training_benchmark`.

## Planner findings (report only)

These are what the metrics reveal about the owner-approved planner. None of them is fixed here, and none of them is gated.

1. **Waiting to move is the largest idle share.** "move to" skips: mixed-rooms 464, consumable-windows 168, mid-level-crew 155, fault-contradicted 131, fresh-crew 127. In fresh-crew all 127 idle hours are weapons and shields crew waiting at 72% for a move to the gym. This is a planner/executor gap: nothing can carry out a move.
2. **Cap overshoot.** The last session can end above the cap: near-cap-crew 37 TP, fresh-crew 32, long-run 25, fault-contradicted 24. The planner has no gain-aware stop near the cap.
3. **Consumables almost never fire.** The Common rule needs fatigue exactly 0 at 29% or less; the Hero rule needs 90% or more and fatigue 100. Items were used only in missing-research (6), consumable-windows (4) and fatigued-crew (1). Stock sits unused in near-cap-crew (4 items), long-run (5) and no-training-room (9).
4. **Training at high fatigue for no gain.** The fatigue gate is at 100 only. In fatigued-crew the crew train at fatigue 99 for 499 sessions that gain nothing (6 TP in 7 days), yet U = 0.998 because they are "busy". The planner never rests crew.
5. **The band gate and the design gate disagree.** The band gate accepts either research family, but each design needs its own family's research.
   - fitness-only: 484 research_missing skips. Weapons and shields crew pass the T2 band gate through Fitness Lv2, then fail on Education Lv2.
   - long-run: 1124 research_missing skips (no Education Lv3 for the academy T3 designs).
   - mixed-rooms: 148 (gym designs need Fitness).
6. **The last 10% is unreachable for some crew.** Crew whose cap is above 90% of capacity (captains at 200/200, Common, Elite, Standard) stop at 90% (`complete`); only a Hero item at fatigue 100 can add more. For example: complete skips in fault-ambiguous 81, fault-raise 82, fault-reject 167.
7. **A failed finish costs one tick.** After a refused, raised or ambiguous finish the advisory decision is dropped (`dropped` = 1 in fault-reject, fault-raise and fault-ambiguous), and the crew restarts an hour later.

No oracle disagreement was found: the unmodified planner never acts at or above an oracle cap, never uses a consumable outside the owner-approved rules and never starts training past an unmet band gate.

## Not the ProgramBench package

`automation/benchmark/` also holds the ProgramBench Python package (`adapter.py`, `run.py`, `scoring.py`, ...). It is unrelated, and it never reads the `training/` subdirectory. Note that `automation/reusable-manifest.json` syncs `automation/benchmark` as a whole directory (with `delete_stale`) into the reusable repo-automation template. The crew-training scenarios and baseline will be exported with it unless the owner excludes the path.
