# Crew-training benchmark

`scripts/training_benchmark.py` scores the crew-training planner (`sdk/training`) on a fixed suite of simulated worlds and fails CI when the score drops. Each scenario runs for 7 to 10 simulated days in the offline simulator (`sdk.simulation.game`), once for each of three seeds, so there is no network, no account and no credential anywhere in the run.

Every training call the executor makes goes through a recording client. The client journals the call together with the simulator's own crew record from just before it. Nine independent oracles judge that journal for hard policy violations, and every metric is computed from the journal and the simulator's training log. No executor can skip a check, because no check depends on the executor reporting anything.

A second mode, qa-eval, mutates the planner and the strategy and measures how many mutants the benchmark and the unit tests catch.

The benchmark measures the owner-approved strategy as it is. It never tunes the planner or the strategy to raise a score.

## Usage

```bash
make training-benchmark                                  # the CI gate: exit 0 pass, 1 regression, 2 input error
make training-benchmark ARGS="--update-baseline"         # rewrite baseline.json (only ever up)
make training-benchmark ARGS="--update-baseline --allow-lower 'reason'"
make training-benchmark ARGS="--quick"                   # 2-day runs, report only
make training-benchmark ARGS="--qa-eval --jobs 12"       # local only, about 5.5 minutes
```

| Flag | Meaning |
| --- | --- |
| `--gate` | The default mode: run the suite twice in-process, compare the reports byte for byte, gate against `baseline.json`. |
| `--scenario NAME` | Run only this scenario (repeatable). Only the listed scenarios are gated (each must be in the baseline), and the composite is not compared. |
| `--quick` | Override every scenario to 2 days. Report only, never gated (faults after day 1 do not fire). |
| `--seed N` | Run every scenario at seeds N, N+1 and N+2 instead of its own. Report only, never gated. |
| `--json PATH` | Write the JSON report, with a `gate` object in gate mode. `-` prints it instead of the markdown. |
| `--markdown PATH`, `--summary PATH` | Write, or append (for `$GITHUB_STEP_SUMMARY`), the markdown report. |
| `--quiet` | Print nothing. |
| `--scenarios-dir`, `--baseline` | Other inputs (the tests use temporary ones). |
| `--update-baseline`, `--allow-lower REASON` | See [Gate and baseline](#gate-and-baseline). |
| `--qa-eval`, `--jobs`, `--timeout`, `--only ID...`, `--qa-target PATH...`, `--channels tests,benchmark` | See [qa-eval](#qa-eval). |

Flags a mode would ignore are a usage error (exit 2):

- `--gate` with `--update-baseline`, `--quick` or `--seed`.
- `--qa-eval` with `--gate`, `--update-baseline`, `--quick`, `--seed` or `--scenario`.
- `--update-baseline` with `--quick`, `--seed` or `--scenario`.

The CI job `training-benchmark` in `.github/workflows/python-tests.yml` runs `make training-benchmark ARGS="--gate --json training-benchmark.json --summary $GITHUB_STEP_SUMMARY"` and uploads the JSON report. It is blocking: `blocking-ci.yml` calls that workflow as its `tests` job, which `CI required` needs. Locally the gate takes about 5 seconds: 15 scenarios × 3 seeds, run twice.

## How a run works

### The tick contract

The executor under test is a *tick*: `tick(client, now) -> TickReport | None`. `run_episode` calls `start_day(day)` on the simulator and then calls the tick once per tick, `1440 / tick_minutes` times a day (every 60 minutes by default). Every catalog duration is a whole number of hours, so a planner that restarts at once loses no time to polling.

A `TickReport` (skips with their reason and detail, dropped advisories, replans, a failed read) is diagnostics only. It feeds the skip histogram and the drift digest, never a score and never an oracle.

### The recording client

`RecordingClient` is the Client surface a tick drives. It delegates the simulator's reads unchanged: `listAllCharactersOfUser`, `listAllCharacterDesigns2`, `listAllTrainingDesigns2` and `listItemsOfAShip`, with their response attributes.

The simulator has no room or research reads. The client therefore builds the four that the real Client uses from the simulator's rooms and research and the catalog designs: `listRoomsViaAccessToken` (`roomsViaAccessToken`), `listRoomDesigns2` (`roomDesigns`), `listAllResearches` (`allResearches`) and `listAllResearchDesigns2` (`allResearchDesigns`). Unit 9's executor makes the same calls on the real Client.

`listAllCharactersOfUser` returns False when an injected `snapshot-failure` hits the account read. The simulator's snapshot ticks the clock, so the client puts the clock back and a read-fault scenario keeps the tick times of a fault-free one.

Each training call (`addTraining`, `finishTraining`, `useConsumable`) becomes one `Call` in the journal, with these fields:

- The method and its arguments, kept as text.
- `before`: the simulator's own record of the crew just before the call (total, fatigue, room, training design and end). It is `None` for a character the simulator does not know.
- The outcome, decided by the benchmark from the simulator's reply: `ok` (True), `refused` (anything else), `ambiguous` (no reply, or a fired `ambiguous-response` fault behind `useConsumable`, which still answers True) or `raised` (the exception propagates to the tick).
- The faults the call fired, and the simulator log entries it appended.

Any other attribute is an `AttributeError`, so a tick cannot reach the simulator behind the journal's back.

`Simulation` holds the seeded simulator account and its clock, and is the probe the metrics read: the training log, the fired faults and the crew records the client journals.

### The contract executor and the Unit 9 swap point

`make_contract_executor(planner, replanner, strategy)` is the default tick. It follows the documented executor contract, nothing more:

1. Read the eight payloads through the client (`read_payloads`). A failed read skips the tick.
2. `ctx = build_context(...)`, then `plan = planner(ctx, strategy)`.
3. Carry out each decision through the action catalog: `action_for`, then `get_spec(...).client_method` with `call_arguments`.
4. A primary decision that follows a FinishTraining is advisory and never runs as is.
   - When the finish returned True, the crew is rebuilt from the finish response with `parse_crew`. Its decision is replaced by `replan_after_finish(ctx, crew, strategy, stock)`, where `stock` starts as `remaining_stock(ctx.items, plan, exclude_character_ids=<finished>)` and shrinks with each replan.
   - When the finish was refused, raised or ambiguous, or the response is not that crew, the advisory decision is dropped and the crew is picked up on a later tick.

Unit 9's executor lives at `sdk/execution/training.py`, which is not in this tree yet. It replaces this tick and nothing else: `run_suite(scenarios, tick=lambda client, now: <the Unit 9 executor>(client, now, ...))`. It reads the payloads through the client exactly as above. `SwapPointTests` in `tests/test_training_benchmark.py` prove two things:

- A hand-written executor that calls the client directly, with no catalog and no report, produces the identical journal, log, metrics and score.
- A tick that never reports anything still has every violation caught.

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

A training design record carries two sets of keys:

- Simulator keys: `@TrainingStat`, `@Duration`, `@Fatigue`, `@MinGain`, `@MaxGain`, `@RequiredRoomDesignId`, `@RequiredResearchDesignId`.
- Planner keys: `@TrainingName`, `@Rank`, `@<Stat>Chance`, `@RequiredRoomType`.

`build_world` checks that `parse_consumable_designs` finds exactly `CATALOG_CONSUMABLES`, and the default tick attaches those ids with `with_consumables`. That is the only strategy change the benchmark makes.

## Scenarios

The files live in `automation/benchmark/training/scenarios/`. Every file's stem equals its `name`. The checked-in suite must meet four conditions:

- At least 7 days per scenario (an owner requirement).
- Every required tag covered.
- Every allowed fault kind among its faults.
- At least one crew member already in a session at the start.

A test runs each fault scenario and checks that every fault fires.

| Scenario | Tags | Days | What it stresses |
| --- | --- | --- | --- |
| fresh-crew | fresh | 7 | Untrained crew: primary T1 to T3, then the 72% "move to gym" boundary. |
| mid-level-crew | mid-level | 7 | Band edges 50/51, 64/65 and 71/72; the exceptional cap of Huge Hellaluya. |
| near-cap-crew | near-cap | 7 | Every rarity a few points below its cap, or at it; tp_cap_reached; cap overshoot. |
| missing-research | missing-research | 7 | No research: the primary T1 gate, the ungated secondary T1 band, the research_missing consumable fallback. |
| mixed-rooms | mixed-rooms | 7 | All four room names and their upgrade lineage; crew in the wrong room type; band gates met through Education only. |
| no-training-room | no-training-room | 7 | Crew on the Bridge, in no room, or without a role; the consumable role and room rules. |
| fatigued-crew | fatigued | 7 | Fatigue recovers one point per two hours, so saturated crew sit at 100 between sessions. The fatigue gate fires again and again, sessions started at 99 gain nothing, a rested captain on primary T3 gains less with each session, and the Hero rule fires. |
| consumable-windows | consumables, in-training | 7 | Four crew start inside a five-hour session, where a consumable is the only allowed action. 20% and 29% with fatigue 0 take Common items; 30% and 29% with fatigue 1 do not. Idle crew at 90% with fatigue 100 take a Hero item; 90% with fatigue 99 and 89% (a move) do not. |
| long-run | multi-day | 10 | Twelve crew, all four rooms, partial research: saturation and runtime. |
| fault-reject | fault-reject | 7 | Refused start, finish and item use: retried, never counted as planner refusals. |
| fault-contradicted | fault-contradicted | 7 | Two accepted starts that run another design (Kickbox, Big Book of Science). |
| fault-raise | fault-raise | 7 | A start with effect and a finish without effect both raise. |
| fault-ambiguous | fault-ambiguous | 7 | A finish with effect and a start without effect both return no answer. |
| fault-snapshot | fault-snapshot | 7 | A failed account read on day 1 and one after a start on day 0. |
| fitness-only | research-split | 7 | Only Fitness and Fitness Lv2: the band gate accepts either family, but each design needs its own. |

The schema is strict: an unknown key is an error, and `validate_scenario` reports every problem at once.

- Top level: `schema_version` 1, `name` (`^[a-z0-9-]+$`), `description`, `tags`, `seed` (>= 0; the run uses seed, seed + 1 and seed + 2), `start` (ISO 8601 with an offset), `days` (1-30), optional `tick_minutes` (5-240, divides 1440, default 60) and `fatigue_recovery_seconds` (default 360), `rooms`, `research`, `inventory`, `crew` and `faults`.
- `rooms`: `[{"id", "design"}]`, where `design` is a `CATALOG_ROOMS` name.
- `research`: unique `CATALOG_RESEARCH` names.
- `inventory`: `{item name: quantity}`.
- `crew`: `{"id", "name", "design", "room"}` (room 0 means none) plus optional `fatigue` (0-100), `level`, `stats` (simulator stat names; the total must stay within capacity) and `training` (`{"design", "ends_after_minutes"}` for a session already running).
- `faults`: `{"kind", "method", "day"?, "call"?, "probability"?}`, where the kind is one of `reject`, `contradicted-effect`, `raise`, `ambiguous-response` and `snapshot-failure`. `raise` and `ambiguous-response` take an optional `effect`; `contradicted-effect` needs a `training_design` (a catalog name). A fault `day` must be before `days` (a later one would never fire). The simulator's `Fault` re-validates every fault.

### Tags must show their evidence

A tag is a claim about what a scenario exercises, and `test_every_tag_shows_its_evidence` checks every claim against the unmodified planner's metrics, at each scenario's own seed. Every tag used in the suite must have a rule. `tag_evidence(scenario, metrics)` lists the claims a run does not show.

| Tag | Evidence (each at least) |
| --- | --- |
| fresh | a start in primary T1, T2 and T3; a move skip |
| mid-level | a start in primary T2, primary T3, secondary T1 and secondary T2 |
| near-cap | 24 `tp_cap_reached` skips; 1 TP of cap overshoot; a crew reaching its target |
| missing-research | 24 `research_missing` skips; a Common item used; a start in the ungated secondary T1 band |
| mixed-rooms | a move skip; a session in Galaxy Gym and one in Lunar College |
| no-training-room | 24 `not_in_training_room` skips; 24 `unknown_role` skips |
| fatigued | 24 `fatigued` skips; a zero-gain session; a Hero item used |
| consumables | a Common item and a Hero item used |
| in-training | a session that was running at the start collected |
| research-split | 24 `research_missing` skips; a start in primary T2 |
| multi-day | 12 eligible crew; a crew reaching its target; a session in each of the four training rooms |
| fault-reject, fault-contradicted, fault-raise, fault-ambiguous, fault-snapshot | exactly as many `fault_rejected`, `contradicted`, `raised`, `ambiguous` or `read_faults` as the scenario has faults of that kind |

The review found two scenarios that failed their own claims, and both are fixed:

- **fatigued-crew** fired the fatigue gate once in 7 days. With recovery at 720 s per point (5 points an hour), the crew never sat at 100 at a tick. Recovery is now 7200 s, and the gate fires 169 times per seed.
- **consumable-windows** had U = 0 because it had no research. It now has Fitness and Education. Its Common-rule edges are crew loaded inside a running session, the in-progress branch where a consumable is the planner's only action. That also covers the crew `training` schema field, which no scenario used before.

## Metrics and score

### Definitions

E is the set of eligible crew: those with a role in the oracle table and a room whose catalog type is a training room. For each crew member i, with starting total `T0_i`:

- `cap_i = oracle_cap(...)`.
- `target_i = policy_target(...)` is the most the policy can train the crew to in its room. It is the smallest of three values: the cap, `first_total(90, capacity)` (the first total at which the policy calls training complete), and the first total of the first band from `T0_i` on whose slot needs another room type.
- `headroom_i = max(0, target_i - T0_i)` for eligible crew, otherwise 0. H is the sum over E. `build_world` refuses a scenario with H = 0.

The room limit is there because nothing in the game API or the simulator moves crew between rooms, so no planner can train past it. The limit is an extension of the review's M1. Before it, the 72% "move to" boundary alone gave mid-level-crew a seed spread of 18.8 points: whether a session happened to jump a crew past the boundary decided whether that crew had any idle time. With the limit, the spread is 0.31.

Missing research is deliberately not clamped. Consumables can still move a crew whose research is missing, and the gap between the band gate and the design gate is a planner finding that the score should keep visible.

| Metric | Definition |
| --- | --- |
| P, progress | The area under the reachable-progress curve, per simulator log event, as a fraction of `H × horizon`. Each crew's progress is `min(headroom_i, total_i - T0_i)`. It changes only at accepted finishes and item uses, so the area is exact: every gained point counts for the time left until the horizon. P rewards getting there sooner, at the resolution of single events instead of day samples. |
| U, utilisation | Productive crew time as a fraction of the window crew time. A crew with headroom has a window from the start until its first accepted finish or item use that reaches `target_i`, or until the horizon. Busy time is the union of its productive sessions (sessions loaded at the start count too). A session is productive when it gained something. A session still running at the horizon is productive when its best draw, at the fatigue the journal recorded just before its start and under the capacity, gains something. A session that ended uncollected before the horizon gained nothing. Zero-gain sessions are wasted time, not busy time. |
| R, reliability | `accepted / (accepted + genuine refusals)` journaled calls. A refusal that fired an injected `reject` fault is not genuine. Ambiguous and raised calls are neither. 1.0 when nothing ran. |

### Score

```text
score     = 0                                               if the run errored, or any gated oracle fired
score     = round(100 * R * (0.75 * P + 0.25 * U), 6)       when some crew had a utilisation window
score     = round(100 * R * P, 6)                           when none did (U left out, weights renormalised)
scenario  = round(mean(score over seeds seed, seed + 1, seed + 2), 6)
composite = round(mean(scenario scores), 6)
```

The weights are 0.75 for P and 0.25 for U, for these reasons:

- P is the outcome the owner wants: training points, sooner. With per-event resolution it already carries speed, so it takes most of the weight.
- U is a process signal. It separates planners with similar TP: one that idles, or keeps crew busy with sessions that gain nothing, from one that does not. It keeps the 0.25 weight it had in v1, so it detects a lazy planner as before but cannot outweigh the outcome.
- v1's focus term (0.10) is dropped. It was 1.0 in 14 of 15 scenarios, and it was circular: its stat table was taken from the role tables the planner follows. Its weight moved to P.
- v1 scored U as 1.0 when nothing had a window, which gave 25 free points. Now U is left out and P carries the whole weight.

R stays a multiplier. A planner that starts a design in the wrong room, or an unknown design, is refused by the simulator, so R falls; `test_a_wrong_design_is_refused_and_scores_lower` checks this. Such a start also trips the `design` oracle, so the scenario scores 0.

What the score does:

- It is bounded in [0, 100].
- It is monotone in waste. Dropping a zero-gain session never lowers U (`test_utilisation_counts_productive_time_in_each_window`).
- A planner that rests crew at fatigue 90 or more never scores below the default planner on fatigued-crew: 9.33 against 8.94 on the three-seed mean, with 23 wasted sessions per seed instead of 216 (`test_resting_fatigued_crew_never_scores_lower`). In a one-crew world, where the shared simulator random stream cannot interfere, it scores at least as high on every seed.
- A no-op planner scores 0 on every scenario.
- A lazy planner, a narrower `room_types` and a strategy without consumables each score lower.

Reported only, never scored:

- `tp_gained`, `tp_gained_eligible`, `tp_per_crew_day` and `crew_completed`.
- Eligible, productive and idle crew-hours.
- Actions per method, outcomes, `refusal_codes`, `zero_gain_sessions` and `wasted_actions` (genuine refusals plus zero-gain sessions).
- The fault counters: `fault_rejected`, `contradicted`, `raised`, `ambiguous`, `read_faults` and `observe_failures`.
- `dropped`, `replans`, `cap_overshoot_tp`, the `skip_reasons` histogram and `skip_moves`.
- `starts_by_band`, `sessions_by_room`, `consumables_by_tier`, `consumables_used` and `loaded_sessions_collected`.

Every metric comes from the journal, the simulator log and the fired faults, except the skip histogram, `skip_moves`, `dropped`, `replans` and `observe_failures`, which come from the tick reports.

## Oracles

The oracle tables are independent literal copies of the owner-approved policy in `sdk/training/strategy.py`, `sdk/training/planner.py` and `sdk/crew_leveling.py`:

- `ORACLE_ROLES` and `ORACLE_ROLE_SLOTS` (each role's room type and trainings per slot).
- `ORACLE_RARITY_STARS`, `ORACLE_TP_CAPS`, `ORACLE_EXCEPTIONAL_CAPS` and `ORACLE_CAPTAIN_CAPS`.
- `ORACLE_BANDS`, `ORACLE_COMPLETE_PERCENT`, `ORACLE_GATES`, `ORACLE_MAX_TRAINING_FATIGUE` and `ORACLE_CONSUMABLE_RULES`.

They are never imported from `sdk/training`, so a mutant cannot move them. `tests/test_training_benchmark_policy.py` pins them to the sources, and a deliberate policy change must update both sides in the same change.

Each oracle judges one journaled `addTraining` or `useConsumable` call, of any outcome, against the simulator's crew record from just before it. Research comes from the scenario, since it never changes during a run. A crew whose finished session was not collected yet is judged as idle, as the planner does. Finishes, and calls for a character the simulator does not know, are never judged.

| Oracle | Fires on |
| --- | --- |
| `cap` | A start or an item use at or above the crew's oracle cap. |
| `complete` | A start at or above 90%. |
| `fatigue` | A start at fatigue 100 or more (`max_training_fatigue`). |
| `research-gate` | A start whose band gate (`ORACLE_GATES`) is not met. |
| `room` | A start for a crew outside a training room, or in the wrong room type for its band's slot (the planner skips with "move to"). |
| `design` | A start by a crew with no role, or of a design that is not the role's training for the band's slot and tier. |
| `design-research` | A start whose design's own required research (by catalog research design id) is not completed. |
| `consumable` | An item use where the item is not a catalog consumable, its tier rule does not allow the crew's percent and fatigue, the crew has no role, or the crew is not in a training room. |
| `consumable-precedence` | An item use while a start was possible, or while the crew should move rooms. The planner reaches its consumable fallback only when training is in progress, training is complete, or nothing is startable in the right room type (gate, fatigue, design or design research). |

The unmodified planner trips none of the nine on any scenario at any seed (`test_the_unmodified_planner_trips_no_oracle`, and the gate itself). All nine are therefore gated, and `REPORT_ONLY_VIOLATIONS` is empty. If a later planner change trips an oracle, first check the oracle against the policy code. If the oracle is faithful and the planner is the problem, record the planner defect as a finding, move that oracle to `REPORT_ONLY_VIOLATIONS`, and note it here.

`test_each_policy_breach_is_caught_by_its_oracle` drives a policy breach for five of the oracles (fatigue, complete, design research, consumable precedence and room) through the default tick on a checked-in scenario, and checks that the scenario scores 0.

**Cap overshoot is reported, not gated.** The planner cannot know a session's gain in advance, so a session started below the cap can end above it. Gating "total above cap" would fail the unmodified planner. The gate therefore counts acting at or above the cap, and `cap_overshoot_tp` reports the overshoot: the TP above `max(cap, starting total)`, so a crew that started above its cap only counts what it gained.

## Seeds, determinism and the gate

Each scenario runs at seed, seed + 1 and seed + 2, and its score is the mean. The JSON report lists every seed's score, trace digest and metrics, and the scenario's spread (max - min). Spreads with the unmodified planner range from 0.28 (fault-contradicted) to 1.68 (fault-ambiguous).

The gate fails with these reasons:

- `nondeterministic`: two in-process reports differ by a byte.
- `baseline_missing`: no baseline file (exit 2).
- `baseline_mismatch`: the scenario set differs from the baseline (with `--scenario`: a listed scenario is not in it).
- `composite_below_baseline`.
- `scenario_below_baseline:<name>`.
- `<oracle>_violation:<name>` for each gated oracle that fired, for example `cap_violation`, `consumable_precedence_violation` or `design_research_violation`.
- `scenario_error:<name>`: an exception inside any seed's episode (the planner's or the tick's).

**The comparison is exact, with no tolerance.** The benchmark is deterministic: the reports are byte-identical across reruns and across `PYTHONHASHSEED` values. Any score change is therefore caused by a code change, never by noise between runs. A tolerance would let small regressions and weak mutants through. The cost is that a beneficial change can lower one scenario through the shared simulator random stream (see the [known limitations](#known-limitations)). That change then needs `--update-baseline --allow-lower REASON`, which records the reason. Averaging three seeds makes that rarer.

The trace digest of a seed is a sha256 of its call journal, simulator log and tick reports. A scenario's digest combines its seeds' digests.

**Trace drift is a warning, never a failure.** A digest that differs from the baseline is reported in the markdown as "trace drift (behaviour changed, scores held; never a failure)", with the instruction to refresh the digests with `--update-baseline` when the change is intended. An errored scenario is a `scenario_error`, never drift. qa-eval never counts drift as detected.

## Gate and baseline

`automation/benchmark/training/baseline.json` (schema version 2) holds four things:

- The composite.
- Each scenario's mean score and combined trace digest.
- The `lowered` history.
- The `qa_eval` settings: `min_detection_rate` 0.9 and the equivalent-mutant list.

Schema 2 is the score formula of the design review. A schema 1 baseline is refused as not comparable. The v1 composite was 65.184847; the v2 baseline was written fresh.

`--update-baseline` always runs the determinism check first. It refuses (exit 1) a nondeterministic run, a scenario error or any gated violation. It writes the file when every score is at or above the old one; equal scores refresh the digests, and new scenarios are added. It refuses each of these unless `--allow-lower REASON` gives a non-empty reason, which is then appended to `lowered`:

- any lower score;
- a removed scenario;
- a lower composite over the same scenario set (a new scenario that scores below the mean is not a regression).

The `qa_eval` block and the `lowered` history are always kept.

### Measured baseline

These are the scores of the unmodified planner as checked in. The reports are byte-identical across runs and across `PYTHONHASHSEED` values. P, U and R are means over the three seeds; TP gained is summed over them.

| Scenario | Score | Spread | P | U | R | TP gained | Zero-gain sessions | Consumables |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| consumable-windows | 41.153274 | 1.39 | 0.519 | 0.088 | 1.000 | 312 | 1 | 21 |
| fatigued-crew | 8.944731 | 0.73 | 0.107 | 0.036 | 1.000 | 90 | 648 | 3 |
| fault-ambiguous | 84.704006 | 1.68 | 0.800 | 0.988 | 1.000 | 1094 | 0 | 0 |
| fault-contradicted | 91.252056 | 0.28 | 0.883 | 1.000 | 1.000 | 1022 | 0 | 0 |
| fault-raise | 84.626497 | 1.03 | 0.797 | 0.994 | 1.000 | 1060 | 0 | 0 |
| fault-reject | 88.592830 | 0.32 | 0.856 | 0.974 | 1.000 | 502 | 0 | 0 |
| fault-snapshot | 89.099699 | 0.87 | 0.870 | 0.954 | 1.000 | 754 | 0 | 0 |
| fitness-only | 40.203946 | 1.13 | 0.520 | 0.048 | 1.000 | 172 | 0 | 0 |
| fresh-crew | 90.093743 | 0.62 | 0.868 | 1.000 | 1.000 | 1364 | 0 | 0 |
| long-run | 70.962044 | 1.18 | 0.870 | 0.228 | 1.000 | 2289 | 0 | 0 |
| mid-level-crew | 97.030142 | 0.31 | 0.960 | 1.000 | 1.000 | 406 | 0 | 0 |
| missing-research | 4.296580 | 0.30 | 0.057 | 0.002 | 1.000 | 43 | 0 | 18 |
| mixed-rooms | 65.743764 | 1.35 | 0.765 | 0.336 | 1.000 | 450 | 0 | 0 |
| near-cap-crew | 96.965681 | 0.46 | 0.960 | 1.000 | 1.000 | 385 | 0 | 0 |
| no-training-room | 55.247307 | 0.34 | 0.682 | 0.163 | 1.000 | 156 | 0 | 0 |
| **composite** | **67.261087** | | | | | | | |

## qa-eval

qa-eval runs `scripts/mutation.py`'s `run_mutants` over `sdk/training/planner.py` and `sdk/training/strategy.py`. A custom runner does three things in every mutant workspace:

1. **Import probe**: `python -c "import sdk.training"`. A mutant that breaks the import is classified `invalid-strategy`, and its channels are skipped. In practice these are the strategy mutants that `TrainingStrategy` validation rejects at import, for example a band edge that no longer tiles.
2. **tests channel**: `scripts/ci.py test --names tests.test_training_planner tests.test_training_parse tests.test_training_benchmark`.
3. **benchmark channel**: the gate (`training_benchmark.py --gate --json - --quiet`) in a subprocess inside the workspace, because the mutated planner can only be imported there. The outcomes:
   - Exit 0 is a pass.
   - Exit 1 is "detected", with the gate's reasons.
   - Any other exit is a "crash", and running out of time is a "timeout". Both count as detected.

The policy pins (`tests/test_training_benchmark_policy.py`) are deliberately not a channel. They compare tables literally, so every change to strategy data would count as "caught by the tests" whatever its behaviour.

The per-mutant timeout (`--timeout`, default 300 s) is split evenly between the channels. The workspaces copy `sdk`, `tests`, `scripts/ci.py`, `scripts/mutation.py`, `scripts/training_benchmark.py` and `automation/benchmark/training`. The unmutated run must pass both channels.

Each mutant is classified as one of:

- `both`, `tests-only` or `benchmark-only`: caught by those channels.
- `invalid-strategy`: the import probe failed.
- `drift-only`: neither channel caught it, but the trace changed.
- `neither`, `equivalent` or `incompetent`.

```text
eligible                   = mutants - incompetent - equivalent
importable                 = eligible - invalid-strategy
benchmark_rate             = |both or benchmark-only| / importable        the benchmark's own claim, reported first
tests_rate                 = |both or tests-only| / importable
detection_rate             = |caught or invalid-strategy| / eligible      gated: >= qa_eval.min_detection_rate (0.9)
detection_rate_importable  = |caught| / importable                       reported
observable_rate            = |caught or invalid-strategy or drift| / eligible   reported only, never gated
```

The gated rate counts `invalid-strategy` as detected, because an import crash breaks the bot at once and CI catches it. The importable rate leaves those mutants out so that they cannot inflate the claim.

Drift is never counted in a headline rate. It flags every behaviour change, including neutral ones. If the rate falls below 0.9, the fix is better scenarios or oracles, never counting drift.

Equivalent mutants are listed in `baseline.json` under `qa_eval.equivalent`, each as `{id, original, replacement, reason}`. An entry whose id no longer exists, or whose text changed (ids are line:column based), is reported as stale and ignored.

The first full run left 25 mutants caught by neither channel. They sit in code the scenarios cannot reach: a training capacity of 1, the `training_state` return values, the frozen and normalised strategy fields, and validation messages. Each one now fails a test in `PlannerContractTests` (`tests/test_training_benchmark.py`). Those tests check public return values, decisions, error messages and immutability. They do not pin strategy literals. None of the 25 was listed as equivalent, so `qa_eval.equivalent` is still empty. A capacity-1 crew is unrealistic, so these mutants have no benchmark scenario. The tests are candidates to move into `tests/test_training_planner.py`.

A smoke run of six mutants (`--only` the ids below, `--jobs 4`) took 55 s. It found a benchmark rate of 0.800 and a tests rate of 1.000 over the 5 importable mutants, and a gated detection rate of 1.000.

| Mutant | Change | Class | Benchmark reasons |
| --- | --- | --- | --- |
| `sdk/training/planner.py:390:20:compare:0` | fatigue gate `>=` to `<` | both | `fatigue_violation:fatigued-crew`, scenarios below baseline |
| `sdk/training/planner.py:397:7:negate:0` | design research check inverted | both | scenarios below baseline |
| `sdk/training/planner.py:205:63:boundary:1` | design research needs level >= 2 | tests-only | (equivalent in this world: every gated catalog design needs a Lv2 or Lv3 research) |
| `sdk/training/strategy.py:313:25:constant:0` | `max_training_fatigue` 100 to 101 | both | `fatigue_violation:fatigued-crew` (v1: tests only) |
| `sdk/training/strategy.py:316:61:constant:0` | Common rule max percent 29 to 30 | both | `consumable_violation:consumable-windows` |
| `sdk/training/strategy.py:297:37:constant:0` | primary T1 band high 50 to 51 | invalid-strategy | (the bands no longer tile: import fails) |

### qa-eval results

Measured on 2026-11-25 at commit `dc872ad`, with both channels:

```bash
.venv/bin/python scripts/training_benchmark.py --qa-eval --jobs 12 --json qa.json --markdown qa.md
```

The run took 324 s of wall time (3362 s of CPU time) and exited 0, with the qa-eval passing.

| Rate | Value | Over |
| --- | ---: | --- |
| Benchmark channel | 0.588 | 197 of 335 importable |
| Tests channel | 1.000 | 335 of 335 importable |
| Detection, gated (minimum 0.9) | 1.000 | 415 of 415 eligible |
| Detection, importable only | 1.000 | 335 of 335 importable |

| Target | Mutants | Invalid strategy | Both | Tests-only | Benchmark-only | Drift-only | Neither | Benchmark rate | Detection |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `sdk/training/planner.py` | 207 | 0 | 159 | 48 | 0 | 0 | 0 | 0.768 | 1.000 |
| `sdk/training/strategy.py` | 208 | 80 | 38 | 90 | 0 | 0 | 0 | 0.297 | 1.000 |
| **total** | **415** | **80** | **197** | **138** | **0** | **0** | **0** | **0.588** | **1.000** |

No mutant was incompetent, equivalent, benchmark-only, drift-only or caught by neither channel. `qa_eval.equivalent` stays empty, and no entry was reported stale.

The benchmark-channel rate on `strategy.py` is low because most of its importable mutants change validation messages, frozen or normalised fields, or table rows that no scenario reaches. Only the tests catch those.

Round history:

| Round | Gated detection | Importable only | Tests channel | Benchmark channel | Neither |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.940 | 0.925 | 0.925 | 0.588 | 25 (6 planner, 19 strategy) |
| 2 | 1.000 | 1.000 | 1.000 | 0.588 | 0 |

Between the rounds, the QA loop killed all 25 neither mutants with `PlannerContractTests`. The benchmark channel did not change, because no planner or strategy code changed.

## Known limitations

- **One shared random stream per account.** The simulator draws every session's gain from one per-account `training_rng`, in finish order (`sdk/simulation/game.py`). A change for one crew reshuffles every later gain of every crew, so two planners never see the same draws. The simulator is frozen for this unit. The benchmark averages three seeds and reports the spread; the gate stays exact (see [Seeds, determinism and the gate](#seeds-determinism-and-the-gate)).
- **No room moves.** Neither the simulator nor the client can move crew between rooms. Weapons, shields and engines crew wait on `not_in_training_room` "move to ..." from 72%, or from the start when they sit in the other room type. The reachable headroom stops at that boundary, so the wait costs no score. `skip_moves` reports it.
- **Research never arrives mid-run.** The simulator completes "Researching" entries at a day boundary, so scenarios list only completed research.
- **Snapshot faults** are modelled only as a failed account read: the tick is skipped. The planner never sees a stale or foreign snapshot.
- **Cap overshoot** is reported, not gated (see [Oracles](#oracles)).
- **A design-research boundary is invisible to the benchmark.** Every gated catalog design needs a research named "Lv2" or "Lv3", so a planner that demanded level 2 instead of 1 for a design's own research behaves identically here. The planner tests catch that mutant.
- **The script's own mutation target** is not in `automation/mutation/config.json`, because `tests/test_mutation_harness.py` requires every target path to start with `sdk/`. Measure it with `make mutation TARGET=scripts/training_benchmark.py TESTS="tests.test_training_benchmark" JOBS=6`. The `copy` list already covers the script and the scenarios. The full run measured 90.4% (2426 mutants: 2188 killed, 4 timeouts, 234 survived). Re-running the 234 survivors against the final tests killed 49 more, which puts the score at 92.4%. The rest are mostly message strings, dataclass defaults and response envelope keys that `build_context` ignores.
- **The reusable-manifest sync.** `automation/reusable-manifest.json` syncs `automation/benchmark` as a whole directory (with `delete_stale`) into the reusable repo-automation template, so the crew-training scenarios and baseline are exported with it. Excluding the path is an owner decision.

## Planner findings (report only)

These are what the metrics reveal about the owner-approved planner. None of them is fixed here, and none of them is gated. The counts are per scenario at its own seed.

1. **Training at high fatigue for no gain.** The fatigue gate is at 100 only. In fatigued-crew the planner runs 216 sessions per seed that gain nothing, because sessions started at fatigue 99 draw 0. A planner that rests at 90 or more runs about 23, and scores higher (9.33 against 8.94). The planner never rests.
2. **The band gate and the design gate disagree.** The band gate accepts either research family, but each design needs its own family's research.
   - fitness-only: 484 `research_missing` skips. Weapons and shields crew pass the T2 band gate through Fitness Lv2, then fail on Education Lv2.
   - long-run: 1124 (no Education Lv3 for the academy T3 designs).
   - mixed-rooms: 148 (gym designs need Fitness).
   - consumable-windows: 608 (Education Lv1 only, so no T2 for the academy crew).
3. **The design-research consumable fallback can never fire with the default rules.** The planner allows a consumable when a design's own research is missing. However, the Common rule needs 29% or less, where every band trains a T1 design that needs no research, and the Hero rule needs 90% or more, where no band applies. `test_each_consumable_rule` documents it.
4. **Waiting to move is the largest idle share.** "move to" skips: mixed-rooms 464, consumable-windows 168, mid-level-crew 155, fault-contradicted 131, fresh-crew 127. This is a planner/executor gap: nothing can carry out a move.
5. **Cap overshoot.** The last session can end above the cap: near-cap-crew 37 TP, fresh-crew 32, long-run 25, fault-contradicted 24. The planner has no gain-aware stop near the cap.
6. **Consumables rarely fire.** The Common rule needs fatigue exactly 0 at 29% or less, so it fires only while a session is running or when research blocks every start. The Hero rule needs 90% or more and fatigue 100. Items were used only in consumable-windows (7), missing-research (6) and fatigued-crew (1) per seed. Stock sits unused in near-cap-crew (4 items), long-run (5) and no-training-room (9).
7. **The last 10% is unreachable for some crew.** Crew whose cap is above 90% of capacity (captains at 200/200, Common, Elite, Standard) stop at 90% (`complete`); only a Hero item at fatigue 100 can add more. For example, complete skips: fault-reject 167, fault-raise 82, fault-ambiguous 81.
8. **A failed finish costs one tick.** After a refused, raised or ambiguous finish, the advisory decision is dropped (`dropped` = 1 in fault-reject, fault-raise and fault-ambiguous), and the crew restarts an hour later.

No oracle disagreement was found: the unmodified planner trips none of the nine oracles on any scenario at any seed.

## Not the ProgramBench package

`automation/benchmark/` also holds the ProgramBench Python package (`adapter.py`, `run.py`, `scoring.py`, ...). It is unrelated, and it never reads the `training/` subdirectory. See the reusable-manifest note under [Known limitations](#known-limitations).
