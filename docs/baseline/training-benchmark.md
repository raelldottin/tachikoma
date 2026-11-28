# Crew-training benchmark

`scripts/training_benchmark.py` scores the crew-training planner (`sdk/training`) on a fixed suite of simulated worlds and fails CI when the score drops. Each scenario runs for 7 to 10 simulated days in the offline simulator (`sdk.simulation.game`), once for each of three seeds, so there is no network, no account and no credential anywhere in the run.

Every training call the executor makes goes through a recording client. The client journals the call together with the simulator's own crew record from just before it. Nine independent oracles judge that journal for hard policy violations, and every metric is computed from the journal and the simulator's training log. No executor can skip a check, because no check depends on the executor reporting anything.

A second mode, qa-eval, mutates the planner and the strategy and measures how many mutants the benchmark and the unit tests catch.

The SPEC-14 workflow benchmark and its seeded-defect evaluation are a separate harness, described in [SPEC-14 workflow evaluation](#spec-14-workflow-evaluation).

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
| `--gate` | The default mode: run the suite twice in-process and once in a child process under another `PYTHONHASHSEED`, compare the three reports byte for byte, gate against `baseline.json`. |
| `--scenario NAME` | Run only this scenario (repeatable). Only the listed scenarios are gated (each must be in the baseline), and the composite is not compared. |
| `--quick` | Override every scenario to 2 days. Report only, never gated (faults after day 1 do not fire). |
| `--seed N` | Run every scenario at seeds N, N+1 and N+2 instead of its own. Report only, never gated. |
| `--report-only` | Run the suite once and report: no gate, no baseline, no determinism check. `--quick` and `--seed` runs also run the suite once. It is the determinism child's mode. |
| `--json PATH` | Write the JSON report, with a `gate` object in gate mode. `-` prints it instead of the markdown. |
| `--markdown PATH`, `--summary PATH` | Write, or append (for `$GITHUB_STEP_SUMMARY`), the markdown report. |
| `--quiet` | Print nothing. |
| `--scenarios-dir`, `--baseline` | Other inputs (the tests use temporary ones). `--update-baseline` with another `--scenarios-dir` needs its own `--baseline`. |
| `--update-baseline`, `--allow-lower REASON` | See [Gate and baseline](#gate-and-baseline). |
| `--qa-eval`, `--jobs`, `--timeout`, `--only ID...`, `--qa-target PATH...`, `--channels tests,benchmark` | See [qa-eval](#qa-eval). |

Flags a mode would ignore are a usage error (exit 2):

- `--gate` with `--update-baseline`, `--quick`, `--seed` or `--report-only`.
- `--qa-eval` with `--gate`, `--update-baseline`, `--quick`, `--seed`, `--scenario` or `--report-only`.
- `--update-baseline` with `--quick`, `--seed`, `--scenario` or `--report-only`.
- `--update-baseline` with a `--scenarios-dir` other than the checked-in one, unless `--baseline` names a file other than the checked-in `baseline.json`. The checked-in baseline is only ever written from the checked-in suite, under its strict rules (7 days per scenario, every required tag).

The CI job `training-benchmark` in `.github/workflows/python-tests.yml` runs `make training-benchmark ARGS="--gate --json training-benchmark.json --summary $GITHUB_STEP_SUMMARY"` and uploads the JSON report. It is blocking: `blocking-ci.yml` calls that workflow as its `tests` job, which `CI required` needs. Locally the gate takes about 4 seconds of wall time: 15 scenarios × 3 seeds, run twice in-process while the child process runs them once in parallel (a single run takes about 2 seconds).

## How a run works

### The tick contract

The executor under test is a *tick*: `tick(client, now) -> TickReport | None`. `run_episode` calls `start_day(day)` on the simulator and then calls the tick once per tick, `1440 / tick_minutes` times a day (every 60 minutes by default). Every catalog duration is a whole number of hours, so a planner that restarts at once loses no time to polling.

A `TickReport` (skips with their reason and detail, dropped advisories, replans, a failed read) is diagnostics only. It feeds the skip histogram and the drift digest, never a score and never an oracle.

### The recording client

`RecordingClient` is the Client surface a tick drives. It delegates the simulator's reads unchanged: `listAllCharactersOfUser`, `listAllCharacterDesigns2`, `listAllTrainingDesigns2` and `listItemsOfAShip`, with their response attributes.

The simulator has no room or research reads. The client therefore builds the four that the real Client uses from the simulator's rooms and research and the catalog designs: `listRoomsViaAccessToken` (`roomsViaAccessToken`), `listRoomDesigns2` (`roomDesigns`), `listAllResearches` (`allResearches`) and `listAllResearchDesigns2` (`allResearchDesigns`). Unit 9's executor makes the same calls on the real Client.

`listAllCharactersOfUser` returns False when an injected `snapshot-failure` hits the account read. The client finds out by taking a simulator snapshot. The snapshot ticks the clock, so the client puts the clock back, and a read-fault scenario keeps the tick times of a fault-free one. An account with no snapshot fault and no read fault takes no snapshot at all: there it could only count itself, and no fault would ever read the count. That saves about half of each run's time and changes no report.

Each training call (`addTraining`, `finishTraining`, `useConsumable`) becomes one `Call` in the journal, with these fields:

- The method and its arguments, kept as text.
- `before`: the simulator's own record of the crew just before the call (total, fatigue, room, training design and end). It is `None` for a character the simulator does not know.
- The outcome, decided by the benchmark from the simulator's reply: `ok` (True), `refused` (anything else), `ambiguous` (no reply, or a fired `ambiguous-response` fault behind `useConsumable`, which still answers True) or `raised` (the exception propagates to the tick).
- The faults the call fired, and the simulator log entries it appended.

Any other attribute is an `AttributeError`, including every underscore name such as `client._game` or `client._simulation`. The client holds the simulator in name-mangled attributes, so a tick using the client surface cannot bypass the journal. Python cannot hide them completely: a tick that spells out `_RecordingClient__game` is deliberately breaking the contract.

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
| `CATALOG_RESEARCH` | The live training lines (ListAllResearchDesigns2, design version 717): Fitness 101-203 (ids 74-79, root 74) and Education 101-203 (ids 101-106, root 101). Records carry `@ResearchName` and `@RootResearchDesignId`, so `parse.py` derives the family from the root and the level from the course number. |
| `CATALOG_TRAININGS` | The 21 training names of `DEFAULT_ROLES`: 12 in the Academy (3011-3043: Weapon, Science, Engine, Pilot) and 9 in the GYM (3111-3133: Attack, Stamina, Hp), tiers 1 to 3. |
| `TIER_PARAMS` | T1: 1 h, +10 fatigue, gain 1-3. T2: 3 h, +20, gain 5-9, needs course 201 of the room's line (Fitness for the GYM, Education for the Academy). T3: 6 h, +30, gain 12-18, needs course 203. |
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
| fitness-only | research-split | 7 | Only Fitness 101 to 201: the band gate accepts either family, but each design needs its own. |

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
- **consumable-windows** had U = 0 because it had no research. It now has Fitness 101 and Education 101. Its Common-rule edges are crew loaded inside a running session, the in-progress branch where a consumable is the planner's only action. That also covers the crew `training` schema field, which no scenario used before.

## Metrics and score

### Definitions

E is the set of eligible crew: those with a role in the oracle table and a room whose catalog type is a training room. For each crew member i, with starting total `T0_i`:

- `cap_i = oracle_cap(...)`.
- `target_i = policy_target(...)` is the most the policy can train the crew to in its room. It is the smallest of three values: the cap, `first_total(90, capacity)` (the first total at which the policy calls training complete), and the first total of the first band from `T0_i` on whose slot needs another room type.
- `headroom_i = max(0, target_i - T0_i)` for eligible crew, otherwise 0. H is the sum over E. `build_world` refuses a scenario with H = 0: no eligible crew has headroom under its cap, the complete percent and its room. That check does not look at research (see below), so a world whose crew are all research-blocked is accepted and simply scores low.

The room limit is there because nothing in the game API or the simulator moves crew between rooms, so no planner can train past it. The limit is an extension of the review's M1. Before it, the 72% "move to" boundary alone gave mid-level-crew a seed spread of 18.8 points: whether a session happened to jump a crew past the boundary decided whether that crew had any idle time. With the limit, the spread is 0.31.

Missing research is deliberately not clamped (owner decision, confirmed in the final review). Research never changes during a run, but consumables can still move a crew whose research is missing, and the gap between the band gate and the design gate is a planner finding that the score should keep visible. The effect on the score is direct:

- P is a fraction of H, so the research-blocked TP stays in the denominator. In missing-research the headroom is 252 TP. The planner gains about 14 TP a seed (Common items and the ungated secondary T1 band), so P is 0.057.
- U charges a research-blocked crew idle time from the start until it reaches its target, which is the whole horizon when it never does. Missing-research has U = 0.002.
- `crew_completed` counts only crew that reach the unclamped target.

A low score on a research scenario is therefore the research gap, not an executor fault. `test_research_blocked_crew_keep_their_headroom` pins the decision.

| Metric | Definition |
| --- | --- |
| P, progress | The area under the reachable-progress curve, per simulator log event, as a fraction of `H × horizon`. Each crew's progress is `min(headroom_i, total_i - T0_i)`. It changes only at accepted finishes and item uses, so the area is exact: every gained point counts for the time left until the horizon. P rewards getting there sooner, at the resolution of single events instead of day samples. |
| U, utilisation | Productive crew time as a fraction of the window crew time. A crew with headroom has a window from the start until its first accepted finish or item use that reaches `target_i`, or until the horizon. Busy time is the union of its productive sessions (sessions loaded at the start count too). A session is productive when it gained something. A session that ends after the last tick (still running at the horizon, or ending between the last tick and the horizon, where no tick is left to collect it) is productive when its best draw, at the fatigue the journal recorded just before its start and under the capacity, gains something. A session that ended uncollected by the last tick gained nothing, because a tick could have collected it. The last tick is the horizon minus `tick_minutes`. Zero-gain sessions are wasted time, not busy time. |
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

The unmodified planner trips none of the nine on any scenario at any seed (`test_the_unmodified_planner_trips_no_oracle`, and the gate itself). All nine are therefore gated (`GATED_VIOLATIONS`), and there is no report-only oracle. If a later planner change trips an oracle, first check the oracle against the policy code. If the oracle is faithful and the planner is the problem, record the planner defect as a finding and decide with the owner whether to fix the planner or report that oracle without gating it.

`test_each_policy_breach_is_caught_by_its_oracle` drives a policy breach for five of the oracles (fatigue, complete, design research, consumable precedence and room) through the default tick on a checked-in scenario, and checks that the scenario scores 0.

**Cap overshoot is reported, not gated.** The planner cannot know a session's gain in advance, so a session started below the cap can end above it. Gating "total above cap" would fail the unmodified planner. The gate therefore counts acting at or above the cap, and `cap_overshoot_tp` reports the overshoot: the TP above `max(cap, starting total)`, so a crew that started above its cap only counts what it gained.

## Seeds, determinism and the gate

Each scenario runs at seed, seed + 1 and seed + 2, and its score is the mean. The JSON report lists every seed's score, trace digest and metrics, and the scenario's spread (max - min). Spreads with the unmodified planner range from 0.28 (fault-contradicted) to 1.68 (fault-ambiguous).

The gate fails with these reasons:

- `nondeterministic`: the in-process rerun, or the child process run, gave a report that differs by a byte.
- `baseline_missing`: no baseline file (exit 2).
- `baseline_mismatch`: the scenario set differs from the baseline (with `--scenario`: a listed scenario is not in it).
- `composite_below_baseline`.
- `scenario_below_baseline:<name>`.
- `<oracle>_violation:<name>` for each gated oracle that fired, for example `cap_violation`, `consumable_precedence_violation` or `design_research_violation`.
- `scenario_error:<name>`: an exception inside any seed's episode or its scoring (the planner's, the tick's, or one that garbage from the tick causes while the metrics are computed).

**The determinism check runs in two processes.** The gate and `--update-baseline` run the suite twice in this process, and once in a child process: this script with `--report-only --json -`, under a fixed `PYTHONHASHSEED` that differs from the parent's (1, or 2 when the parent runs under 1). The child starts first and runs in parallel. Two in-process runs share one hash seed, so they cannot see set-order or hash-seed nondeterminism; the child can. A benchmark child never starts a child of its own: the determinism child and every qa-eval benchmark-channel run have `TRAINING_BENCHMARK_CHILD` set in their environment, so a qa-eval mutant's gate runs the two in-process runs only. A child that fails, or gives no report within 300 s, is an input error (exit 2).

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

`--update-baseline` always runs the determinism check first, with the child process. It refuses (exit 1) a nondeterministic run, a scenario error or any gated violation. It writes the file when every score is at or above the old one; equal scores refresh the digests, and new scenarios are added. It refuses each of these unless `--allow-lower REASON` gives a non-empty reason, which is then appended to `lowered`:

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
3. **benchmark channel**: the gate (`training_benchmark.py --gate --json - --quiet`) in a subprocess inside the workspace, because the mutated planner can only be imported there. It runs with `TRAINING_BENCHMARK_CHILD` set, so it compares two in-process runs and starts no determinism child. The outcomes:
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

`--only ID...` must name generated mutants: an id that no target generates (a typo, or a line that moved) is an input error (exit 2) that names the unknown ids, and nothing runs. Before this, such an id ran zero mutants and reported a detection rate of 1.0.

Equivalent mutants are listed in `baseline.json` under `qa_eval.equivalent`, each as `{id, original, replacement, reason}`. An entry whose id no longer exists, or whose text changed (ids are line:column based), is reported as stale and ignored.

The first full run left 25 mutants caught by neither channel. They sit in code the scenarios cannot reach: a training capacity of 1, the `training_state` return values, the frozen and normalised strategy fields, and validation messages. Each one now fails a test in `PlannerContractTests` (`tests/test_training_benchmark.py`). Those tests check public return values, decisions, error messages and immutability. They do not pin strategy literals. None of the 25 was listed as equivalent, so `qa_eval.equivalent` is still empty. A capacity-1 crew is unrealistic, so these mutants have no benchmark scenario. The tests are candidates to move into `tests/test_training_planner.py`.

A smoke run of six mutants (`--only` the ids below, `--jobs 4`) took 55 s. It found a benchmark rate of 0.800 and a tests rate of 1.000 over the 5 importable mutants, and a gated detection rate of 1.000.

| Mutant | Change | Class | Benchmark reasons |
| --- | --- | --- | --- |
| `sdk/training/planner.py:391:20:compare:0` | fatigue gate `>=` to `<` | both | `fatigue_violation:fatigued-crew`, scenarios below baseline |
| `sdk/training/planner.py:398:7:negate:0` | design research check inverted | both | scenarios below baseline |
| `sdk/training/planner.py:206:63:boundary:1` | design research needs level >= 2 | tests-only | (equivalent in this world: every gated catalog design needs course 201 or 203) |
| `sdk/training/strategy.py:313:25:constant:0` | `max_training_fatigue` 100 to 101 | both | `fatigue_violation:fatigued-crew` (v1: tests only) |
| `sdk/training/strategy.py:316:61:constant:0` | Common rule max percent 29 to 30 | both | `consumable_violation:consumable-windows` |
| `sdk/training/strategy.py:297:37:constant:0` | primary T1 band high 50 to 51 | invalid-strategy | (the bands no longer tile: import fails) |

### qa-eval results

Measured on 2026-11-26 on the live-research-names fix (the commit after `5671451` that changes the gates to 101 / 201 / 203), with both channels:

```bash
.venv/bin/python scripts/training_benchmark.py --qa-eval --jobs 12 --json qa.json --markdown qa.md
```

The run exited 0, with the qa-eval passing. (The 2026-11-25 run at `dc872ad` took 324 s of wall time and 3362 s of CPU time.)

| Rate | Value | Over |
| --- | ---: | --- |
| Benchmark channel | 0.585 | 196 of 335 importable |
| Tests channel | 1.000 | 335 of 335 importable |
| Detection, gated (minimum 0.9) | 1.000 | 415 of 415 eligible |
| Detection, importable only | 1.000 | 335 of 335 importable |

| Target | Mutants | Invalid strategy | Both | Tests-only | Benchmark-only | Drift-only | Neither | Benchmark rate | Detection |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `sdk/training/planner.py` | 207 | 0 | 158 | 49 | 0 | 0 | 0 | 0.763 | 1.000 |
| `sdk/training/strategy.py` | 208 | 80 | 38 | 90 | 0 | 0 | 0 | 0.297 | 1.000 |
| **total** | **415** | **80** | **196** | **139** | **0** | **0** | **0** | **0.585** | **1.000** |

No mutant was incompetent, equivalent, benchmark-only, drift-only or caught by neither channel. `qa_eval.equivalent` stays empty, and no entry was reported stale.

The benchmark-channel rate on `strategy.py` is low because most of its importable mutants change validation messages, frozen or normalised fields, or table rows that no scenario reaches. Only the tests catch those.

Round history:

| Round | Gated detection | Importable only | Tests channel | Benchmark channel | Neither |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.940 | 0.925 | 0.925 | 0.588 | 25 (6 planner, 19 strategy) |
| 2 | 1.000 | 1.000 | 1.000 | 0.588 | 0 |

Between the rounds, the QA loop killed all 25 neither mutants with `PlannerContractTests`. The benchmark channel did not change, because no planner or strategy code changed.

With live research names (2026-11-26), the gates moved to the original courses 101 / 201 / 203 and the catalog to the live lines (`Fitness 101` ... `Education 203`). Every scenario score stayed the same: each old level maps onto the course that opens the same gates (1 to 101, 2 to 201, 3 to 203). One planner mutant moved from both channels to tests only: `planner.py:199:41:constant:0` (a missing research counted as level 1 instead of 0) no longer opens a gate in any scenario, because no default gate is below 101. `test_missing_research_counts_as_level_zero` kills it.

## SPEC-14 workflow evaluation

The SPEC-14 workflow benchmark, `scripts/training_workflow_benchmark.py`, is a separate harness from the simulator benchmark above and is scored separately. It drives the real `run.main()` against a stateful FakePss through frozen, independently written traces: 18 cases and 53 scenarios. The required suite has 9 cases, 43 scenarios and 129 traces at seeds 1401-1403. The holdout has 9 cases, 10 scenarios and 30 traces at seeds 2401-2403. Every case also carries a paired training-v1 control.

The seeded-defect evaluation (`--defects`) checks that the benchmark catches the defects SPEC-14 names (S14-AC7). Each catalogued defect is patched into production at call time, never as a source edit, and replayed against named targets in two channels:

1. **benchmark:** the frozen required, holdout and training-v1 control traces.
2. **derived:** variants built in code from a frozen scenario. `trigger_after(n)` moves each step trigger to a later roster read; `expecting(step, ...)` freezes more of a step's clean trace (an exit, a skip code, a store state); `inserted(...)` adds a step (a second provision, say); `step_pre(...)` adds pre-state. Ten step options exist for derived variants only, and `validate_suite` refuses each in a fixture:

- `remove_marker`: delete the account's establishment marker, keep its store.
- `corrupt_attempt`: add an unknown field to the first stored attempt.
- `hold_lock`: the harness holds `<declared game identity>.lock` of each of the step's accounts through the step (created if production has not created it yet), and no other file.
- the trigger operation `timer_over`: the session just started ends before the post-read.
- `policy_id` (QA round 2): the step's registry names this policy, over a training-v1 control's override.
- `mode` (QA round 2): an execute step in another mode (canary), under an authorization for that mode that also allows the canary's fallback account write.
- `day_start` and `dry_run_age_s` (QA round 2): the fake clock moves forward to the next UTC midnight, or to the latest stamped dry run's finish plus N seconds. For that step, run.py's dry-run and execute entries read the fake clock (they default to the real one).
- `fail_replace` (QA round 2): the attempt store's `os.replace` raises for the step. Only `sdk.fleet.attempts` sees the failing `os`; the process-wide module is untouched.
- `store_as_directory` (QA round 2): a directory replaces the account's store file.

Each option that reads production's layout or state records a witness, so an option with nothing to act on fails its variant's clean run by name. Variants are never written to a fixture, and each must pass unpatched first.

The catalogue holds 51 defects, 48 of them critical. The test module `tests.test_training_workflow_benchmark` runs the same checks in CI: `WorkflowGateTests` (both suites, every control, and a determinism child), `DeterminismTests` (the real `check_determinism` with a real child interpreter), `SeededDefectRunTests` (one test per critical defect, and every derived variant clean), `SeamLivenessTests` (every patched attribute is called), `DerivedOptionTests` (each QA round 2 step option does what its variants rely on), `CapLayerTests` (each spend-cap layer alone prevents the overspend) and `DefectsCliTests` (`--defects` itself).

### Detection rule

- A defect is detected on a trace only when every expected named assertion (`step:assertion`) and every expected hard code appears, the run records no harness error, and no step's exit is an unexpected crash or a production exception. Every step's raw exit is read, including steps that freeze no exit and each dispatch of an overlap step.
- Each target must pass on the unpatched revision first. A target that fails clean fails the run.
- Critical defects must be detected on every trace. A critical defect with no trace, or a run with no defect, fails. Non-critical detection rates are published, not gated.
- A harness error in any run, clean or patched, a failed fake self-check (the same check `--gate` makes) or a seeding that cannot be applied exits 2: that run measured nothing, so it is never only a miss.
- Every seeding declares the production attributes it patches (its seams). Each must be called on a clean run of at least one of the defect's in-process targets, or `--defects` fails with `defect <id>: seam <attribute> is never called on a clean run of its targets`: a refactor that bypasses a seam would otherwise turn its defect into a silent no-op. `SeamLivenessTests` checks every catalogue defect at the first seed. Two kinds of seam are exempt, each with its reason in the harness. Values that are read, not called (`SEAM_EXEMPT`): `run.FLEET_STOP_FILE` and `sdk.fleet.factory.STRICT_ADD_TRAINING`. Seams that are uncalled by design (`SEAMS_NOT_CALLED`): `intent-after-send`'s `settle_training_intent` (both of its targets crash at the send, before any settle) and `recheck-off-frozen`'s `start_refusal` (the frozen stale triggers make the plan refuse before the recheck). The test fails if an exemption goes stale.
- `--gate` never runs with a defect active: it exits 2. The determinism child's argv and environment carry no defect. A defect reaches a processes-mode dispatch child only through that child's spec JSON, while one is active in the parent.

### SPEC-14 workflow evaluation results

Revision: `9d8b623` (the slice after QA fix round 2, merged with `origin/main` at `b833a16`). Every number in this section was measured on that revision on 2026-11-28. The commit that adds this line changes only this document.

Commands:

```bash
.venv/bin/python scripts/training_workflow_benchmark.py --gate --json gate.json
PYTHONHASHSEED=1   .venv/bin/python scripts/training_workflow_benchmark.py --report-only --json h1.json --quiet
PYTHONHASHSEED=999 .venv/bin/python scripts/training_workflow_benchmark.py --report-only --json h999.json --quiet
cmp h1.json h999.json && cmp h1.json gate.json
.venv/bin/python scripts/training_workflow_benchmark.py --defects --json defects.json
.venv/bin/python scripts/ci.py test --names tests.test_training_workflow_benchmark
```

| Command | Exit | Wall time |
| --- | ---: | ---: |
| `--gate --json gate.json` | 0 | 70 s |
| `--report-only` under `PYTHONHASHSEED=1` | 0 | 35 s |
| `--report-only` under `PYTHONHASHSEED=999` | 0 | 53 s |
| `cmp` of the three reports | 0 | - |
| `--defects --json defects.json` | 0 | 193 s |
| `scripts/ci.py test --names tests.test_training_workflow_benchmark` (253 tests) | 0 | 105 s |
| `make automation-check syntax-check test test-security lint` (3480 tests, 1 skipped) | 0 | 434 s |
| `git diff --check origin/main...HEAD` and `scripts/ci.py repository` | 0 | 7 s |
| `make training-benchmark` (composite 67.261087, floor 67.261087, 0 violations) | 0 | 24 s |
| `make mutation` (blocking targets 100%; `crew_leveling` 81.9%, `game_formulas` 73.7%, both above their minimums) | 0 | 2784 s |

The gate passed: required scored 100.000000 (129 of 129 traces) and holdout 100.000000 (30 of 30), with no hard violation, missing witness or fake error, and every control passed. Its JSON report is byte-identical to the report `--gate` wrote on `1f924e0` before this slice, and to the ones written before QA fix rounds 1 and 2: no derived-only option, trigger operation or seeding changes a frozen trace.

**Scenario results:**

| Case | Suite | Scenarios | Traces passed | Control |
| --- | --- | ---: | ---: | --- |
| `role-coverage` | required | 1 | 3/3 | pass (3/3) |
| `preview-prestige` | required | 2 | 6/6 | pass (3/3) |
| `paid-boundary` | required | 9 | 27/27 | pass (3/3) |
| `stale-preflight` | required | 7 | 21/21 | pass (3/3) |
| `reconciliation` | required | 10 | 30/30 | pass (3/3) |
| `persisted-intent-restart` | required | 5 | 15/15 | pass (3/3) |
| `overlapping-dispatch` | required | 2 | 6/6 | pass (3/3) |
| `distinct-accounts` | required | 1 | 3/3 | pass (3/3) |
| `store-stop-controls` | required | 6 | 18/18 | pass (6/6) |
| `tie-break-instance-id` | holdout | 1 | 3/3 | pass (3/3) |
| `observed-zero-stat` | holdout | 1 | 3/3 | pass (3/3) |
| `gas-priced-course` | holdout | 1 | 3/3 | pass (3/3) |
| `malformed-price` | holdout | 1 | 3/3 | pass (3/3) |
| `unclaimed-course` | holdout | 1 | 3/3 | pass (3/3) |
| `room-level-gate` | holdout | 1 | 3/3 | pass (3/3) |
| `two-copy-recipe` | holdout | 2 | 6/6 | pass (3/3) |
| `retry-different-crew` | holdout | 1 | 3/3 | pass (3/3) |
| `concurrent-two-accounts` | holdout | 1 | 3/3 | pass (3/3) |
| **total** | | **53** | **159/159** | **all pass** |

**Determinism evidence.** The `--gate` report and the two `--report-only` reports under `PYTHONHASHSEED` 1 and 999 are byte-identical (sha256 `adcd9418d68b9310...`), as is the gate's own determinism child, which runs under another fixed hash seed. In the test module, `WorkflowGateTests` compares a real child interpreter's run of `overlapping-dispatch` and `persisted-intent-restart` with the shared in-process run, and `DeterminismTests` runs the real `check_determinism` with a real child on `distinct-accounts`. Both assert that the child's `PYTHONHASHSEED` differs from the parent's. `--defects` does not change the gate report.

**Detection table.** `AddTraining` stands for `POST /TrainingService/AddTraining`. Crew labels print as `<crew>` and the prestige target as `<target>`. Each defect ran at all three seeds of its suite, and every target passed unpatched at every seed first.

| Defect | Critical | Channel | Caught by (named assertion / hard code) | Detected / traces |
| --- | --- | --- | --- | ---: |
| `two-starts` | yes | benchmark | `paid-boundary/exact-500`: `exec:mutations.AddTraining`; hard `exec:duplicate_write`<br>`gas-priced-course/gas-300`: `exec:mutations.AddTraining`; hard `exec:duplicate_write` | 6 / 6 |
| `guard-latch-off` | no | benchmark | `paid-boundary/exact-500`: `exec:mutations.AddTraining`; hard `exec:duplicate_write`<br>`overlapping-dispatch/threads`: `overlap:dispatches` | 0 / 6 |
| `lock-skipped` | yes | benchmark | `overlapping-dispatch/threads`: `overlap:dispatches`<br>`distinct-accounts/alpha-held`: `overlap:dispatches`<br>`overlapping-dispatch/processes`: `overlap:dispatches` | 9 / 9 |
| `intent-not-persisted` | yes | benchmark | `persisted-intent-restart/crash-after-send`: `exec2:mutations.AddTraining`, `exec2:resource_delta.alpha`; hard `exec2:duplicate_write`, `exec2:budget_spend` | 3 / 3 |
| `intent-after-send` | yes | benchmark | `persisted-intent-restart/crash-after-send`: `exec2:mutations.AddTraining`, `exec2:skips.alpha.unresolved_attempt`; hard `exec2:duplicate_write`, `exec2:budget_spend`<br>`persisted-intent-restart/crash-before-apply`: `exec2:mutations.AddTraining`, `exec2:skips.alpha.unresolved_attempt`; hard `exec2:duplicate_write`, `exec2:budget_spend` | 6 / 6 |
| `stop-ignored` | yes | benchmark | `store-stop-controls/stop`: `exec:mutations.AddTraining`, `exec:requests.alpha`<br>`store-stop-controls/stop-mid-run`: `exec:mutations.AddTraining`, `exec:skips.alpha.stopped`; hard `exec:budget_spend` | 6 / 6 |
| `scheduler-stop-ignored` | yes | benchmark, derived | `store-stop-controls/stop-mid-run`: `exec:mutations.AddTraining`, `exec:skips.alpha.stopped`; hard `exec:budget_spend`<br>`paid-boundary/earlier-reservation~stop-after-upgrade`: `exec:mutations.AddTraining`, `exec:skips.alpha.stopped`, `exec:exit`; hard `exec:budget_spend` | 6 / 6 |
| `missing-store-fresh` | yes | benchmark | `store-stop-controls/missing-state`: `exec2:mutations.AddTraining`, `exec2:skips.alpha.state_missing`, `exec2:store.alpha`; hard `exec2:budget_spend` | 3 / 3 |
| `unreadable-store-fresh` | yes | benchmark | `store-stop-controls/unreadable-state`: `exec2:mutations.AddTraining`, `exec2:skips.alpha.state_unreadable`; hard `exec2:budget_spend` | 3 / 3 |
| `settle-loses-pending` | yes | benchmark | `persisted-intent-restart/settle-write-fails`: `exec2:mutations.AddTraining`, `exec2:skips.alpha.unresolved_attempt`; hard `exec2:duplicate_write`, `exec2:budget_spend` | 3 / 3 |
| `v1-under-v2-path` | yes | benchmark | `paid-boundary/v1-unpriced-start (training-v1 control)`: `exec:mutations.AddTraining`, `exec:applied.AddTraining`, `exec:journal` | 3 / 3 |
| `v2-down-v1-path` | yes | benchmark | `overlapping-dispatch/threads`: `overlap:dispatches`, `provision:store.alpha`<br>`persisted-intent-restart/crash-after-send`: `exec2:mutations.AddTraining`, `provision:store.alpha`; hard `exec2:duplicate_write` | 6 / 6 |
| `unresolved-guard-only` | yes | benchmark | `reconciliation/missing-post-read`: `exec2:skips.alpha.unresolved_attempt`<br>`retry-different-crew/lost-reply`: `exec2:skips.alpha.unresolved_attempt` | 6 / 6 |
| `unresolved-ignored` | yes | benchmark | `retry-different-crew/lost-reply`: `exec2:mutations.AddTraining`, `exec2:skips.alpha.unresolved_attempt`; hard `exec2:duplicate_write`, `exec2:budget_spend`<br>`reconciliation/missing-post-read`: `exec2:mutations.AddTraining`, `exec2:skips.alpha.unresolved_attempt`; hard `exec2:duplicate_write`, `exec2:budget_spend`<br>`persisted-intent-restart/crash-after-send`: `exec2:mutations.AddTraining`, `exec2:skips.alpha.unresolved_attempt`; hard `exec2:duplicate_write`, `exec2:budget_spend` | 9 / 9 |
| `premium-spend` | yes | benchmark | `paid-boundary/premium`: `exec:mutations.AddTraining`, `exec:skips.alpha.premium_currency.<crew>`; hard `exec:premium_spend` | 3 / 3 |
| `false-verification` | yes | benchmark | `reconciliation/different-course`: `exec1:journal`; hard `exec1:false_verification`<br>`reconciliation/missing-id`: `exec1:journal`; hard `exec1:false_verification` | 6 / 6 |
| `funds-plus-one` | yes | benchmark | `paid-boundary/short-499`: `exec:mutations.AddTraining`, `exec:skips.alpha.insufficient_funds.<crew>` | 3 / 3 |
| `funds-minus-one` | yes | benchmark | `paid-boundary/exact-500`: `exec:mutations.AddTraining`, `exec:applied.AddTraining`, `exec:resource_delta.alpha` | 3 / 3 |
| `reservation-ignored` | yes | benchmark | `paid-boundary/earlier-reservation`: `dry:journal_planned`, `exec:skips.alpha.insufficient_funds.<crew>`<br>`paid-boundary/spend-cap-after-earlier`: `exec:skips.alpha.cap_exceeded.<crew>` | 6 / 6 |
| `recheck-off` | yes | derived | `stale-preflight/room-upgrading~nth2`: `exec:mutations.AddTraining`, `exec:skips.alpha.room_not_ready.<crew>`<br>`stale-preflight/crew-moved~nth2`: `exec:mutations.AddTraining`, `exec:skips.alpha.not_in_training_room.<crew>`<br>`stale-preflight/research-removed~nth2`: `exec:mutations.AddTraining`, `exec:skips.alpha.research_missing.<crew>`<br>`stale-preflight/crew-fatigued~nth2`: `exec:mutations.AddTraining`, `exec:skips.alpha.fatigued.<crew>`<br>`stale-preflight/price-raised~nth3`: `exec:mutations.AddTraining`, `exec:skips.alpha.insufficient_funds.<crew>` | 15 / 15 |
| `recheck-off-frozen` | no | benchmark | `stale-preflight/room-upgrading`: `exec:mutations.AddTraining`<br>`stale-preflight/crew-moved`: `exec:mutations.AddTraining`<br>`stale-preflight/research-removed`: `exec:mutations.AddTraining`<br>`stale-preflight/crew-fatigued`: `exec:mutations.AddTraining`<br>`stale-preflight/price-raised`: `exec:mutations.AddTraining` | 0 / 15 |
| `price-under` | yes | benchmark | `paid-boundary/short-499`: `exec:mutations.AddTraining`, `exec:skips.alpha.insufficient_funds.<crew>` | 3 / 3 |
| `price-over` | yes | benchmark | `paid-boundary/exact-500`: `exec:mutations.AddTraining`, `exec:applied.AddTraining` | 3 / 3 |
| `currency-swapped` | yes | benchmark | `gas-priced-course/gas-300`: `exec:mutations.AddTraining`, `exec:applied.AddTraining`, `exec:resource_delta.alpha` | 3 / 3 |
| `zero-price-unknown` | yes | benchmark | `paid-boundary/explicit-zero`: `exec:mutations.AddTraining`, `exec:applied.AddTraining` | 3 / 3 |
| `zero-stat-unknown` | yes | benchmark | `observed-zero-stat/missing-versus-zero`: `plan:crew_plan.<crew>`, `dry:journal_planned` | 3 / 3 |
| `greedy-roles` | yes | benchmark | `role-coverage/swap`: `plan1:crew_plan.<crew>`, `plan1:crew_plan.<crew>`, `plan2:crew_plan.<crew>`, `plan2:crew_plan.<crew>` | 3 / 3 |
| `tie-high-id` | yes | benchmark | `tie-break-instance-id/equal-repair`: `plan:crew_plan.<crew>`, `dry:journal_planned` | 3 / 3 |
| `tie-input-order` | no | benchmark | `tie-break-instance-id/equal-repair`: `plan:crew_plan.<crew>` | 2 / 3 |
| `two-copy-as-one` | yes | benchmark | `two-copy-recipe/one-training`: `plan:prestige.<target>`<br>`preview-prestige/one-copy`: `plan:prestige.<target>` | 6 / 6 |
| `wrong-stat-course` | yes | benchmark | `role-coverage/swap`: `plan1:crew_plan.<crew>`, `dry1:journal_planned`<br>`preview-prestige/one-copy`: `plan:crew_plan.<crew>`, `dry:journal_planned` | 6 / 6 |
| `settle-failure-swallowed` (QA1 D3) | yes | derived | `persisted-intent-restart/settle-write-fails~exit1`: `exec1:exit`, `exec1:skips.alpha.unresolved_attempt` | 3 / 3 |
| `training-block-dropped` (QA1 D4) | yes | derived | `reconciliation/missing-post-read~exit2`: `exec2:exit`<br>`persisted-intent-restart/crash-after-send~exit2`: `exec2:exit` | 6 / 6 |
| `stop-checked-once` (QA1 D13) | yes | derived | `paid-boundary/earlier-reservation~stop-after-upgrade`: `exec:mutations.AddTraining`, `exec:skips.alpha.stopped`, `exec:exit`; hard `exec:budget_spend` | 3 / 3 |
| `runner-cap-window` (QA1 D5) | yes | benchmark | `paid-boundary/spend-cap-after-earlier`: `exec:mutations.AddTraining`, `exec:skips.alpha.cap_exceeded.<crew>`; hard `exec:budget_spend` | 3 / 3 |
| `recheck-spend-frozen` (QA1 D9) | yes | benchmark | `paid-boundary/spend-cap-after-earlier`: `exec:mutations.AddTraining`, `exec:skips.alpha.cap_exceeded.<crew>`; hard `exec:budget_spend` | 3 / 3 |
| `provision-overwrites-unmarked` (QA1 D17) | yes | derived | `persisted-intent-restart/settle-write-fails~marker-lost`: `exec2:mutations.AddTraining`, `exec2:skips.alpha.unresolved_attempt`; hard `exec2:duplicate_write`, `exec2:budget_spend`<br>`persisted-intent-restart/crash-after-send~marker-lost`: the same | 6 / 6 |
| `journal-witness-wrong-key` (QA1 D16) | yes | derived | `persisted-intent-restart/settle-write-fails~store-lost`: `reprovision:exit`, `reprovision:store.alpha`, `exec2:mutations.AddTraining`, `exec2:skips.alpha.state_missing`; hard `exec2:duplicate_write`, `exec2:budget_spend` | 3 / 3 |
| `resolve-ignores-lock` (QA1 D7) | yes | derived | `reconciliation/resolved-after-reconciliation~held`: `resolve:exit`, `resolve:skips.alpha.locked`, `exec3:mutations.AddTraining`, `exec3:skips.alpha.unresolved_attempt` | 3 / 3 |
| `malformed-attempt-dropped` (QA1 D8) | yes | derived | `persisted-intent-restart/settle-write-fails~extra-field`: `exec2:mutations.AddTraining`, `exec2:skips.alpha.state_unreadable`; hard `exec2:duplicate_write`, `exec2:budget_spend` | 3 / 3 |
| `strict-spec-dropped` (QA1 D12) | yes | derived | `reconciliation/verified~timer-over`: `exec:exit`, `exec:journal`; hard `exec:false_verification` | 3 / 3 |
| `currency-blind-recheck` (QA1 D15) | yes | derived | `stale-preflight/price-raised~gas-after-plan`: `exec:mutations.AddTraining`, `exec:skips.alpha.price_unavailable.<crew>` | 3 / 3 |
| `v1-uncertainty-ignored-under-guard` (QA2 D15) | yes | derived | `retry-different-crew/v1-lost-reply~v2-after-v1 (training-v1 control, then training-v2)`: `exec2:mutations.AddTraining`, `exec2:exit`, `exec2:skips.alpha.stopped`, `exec2:journal`; hard `exec2:duplicate_write`, `exec2:budget_spend` | 3 / 3 |
| `lock-keyed-by-alias` (QA2 D1) | yes | derived | `paid-boundary/exact-500~held`: `exec:mutations.AddTraining`, `exec:exit`, `exec:skips.alpha.locked`; hard `exec:budget_spend` | 3 / 3 |
| `lock-full-mode-only` (QA2 D5) | yes | derived | `paid-boundary/exact-500~canary-held`: `exec:exit`, `exec:skips.alpha.locked`, `exec:other_mutations` | 3 / 3 |
| `dry-run-age-by-day` (QA2 D13) | yes | derived | `reconciliation/verified~dry-run-24h-1s`: `exec:mutations.AddTraining`, `exec:exit`, `exec:skips.alpha.dry_run_stale`; hard `exec:budget_spend` | 3 / 3 |
| `unobserved-funds-enough` (QA2 D14) | yes | derived | `paid-boundary/exact-500~funds-unobserved`: `exec:mutations.AddTraining`, `exec:skips.alpha.insufficient_funds.<crew>`; hard `exec:budget_spend` | 3 / 3 |
| `store-write-not-atomic` (QA2 D7) | yes | derived | `persisted-intent-restart/intent-write-fails~replace-fails`: `exec2:mutations.AddTraining`, `exec2:skips.alpha.intent_not_persisted.<crew>`; hard `exec2:budget_spend` | 3 / 3 |
| `store-unreadable-as-missing` (QA2 D11) | yes | derived | `store-stop-controls/unreadable-state~directory`: `exec2:skips.alpha.state_unreadable` | 3 / 3 |
| `resolution-links-intent` (QA2 D2) | yes | derived | `reconciliation/resolved-after-reconciliation~idle-retry`: `exec3:mutations.AddTraining`, `exec3:exit`, `exec3:journal` | 3 / 3 |
| `verified-still-blocks` (QA2 D3) | yes | derived | `persisted-intent-restart/intent-write-fails~second-start`: `exec2:mutations.AddTraining`, `exec2:applied.AddTraining`, `exec2:journal` | 3 / 3 |

| Rate | Value | Over |
| --- | ---: | --- |
| Critical defects detected on every trace | 48 of 48 | 48 critical defects |
| Critical traces detected | 204 of 204 | benchmark 126, derived 78 |
| Benchmark-channel traces detected | 128 of 150 | all defects, critical and non-critical |
| Derived-channel traces detected | 78 of 78 | 20 defects (`recheck-off`, `scheduler-stop-ignored`, the nine derived QA1 kills and the nine QA2 kills) |
| Targets failing unpatched | 0 | every target at every seed |

**Survivors (non-critical, published only):**

- `guard-latch-off` (0 of 6): masked by the planner's one-start rule. A crew plan never holds two starts, so the guard's latch cannot be reached without also seeding the planner.
- `recheck-off-frozen` (0 of 15): the frozen stale triggers fire at the identity read, so the plan read already sees the change and refuses. The derived channel (`recheck-off`, 15 of 15) moves each trigger to a later roster read, where only the under-lock recheck can see it.
- `tie-input-order` (2 of 3): caught only at the seeds whose reply order puts the higher instance id first, because the seed permutes reply order. `tie-high-id` (3 of 3) covers the instance-id tie-break itself.

**Cap layers (QA1 D5, D9).** The spend cap is enforced three times: the crew plan reserves earlier requests of the same plan, the under-lock recheck charges the spend journaled so far, and the gate runner checks the period's spend before each send. A defect in any one layer is masked by the other two, which is how D5 survived every test module. `CapLayerTests` disables two layers at a time on `paid-boundary/spend-cap-after-earlier` and shows each remaining layer alone prevents the overspend. The plan and the recheck each reproduce the frozen trace. The runner alone refuses later, so its refusal stops the run (exit 1), with no start sent and no hard code. With all three disabled, the overspend is a `budget_spend` hard code. `runner-cap-window` and `recheck-spend-frozen` each seed their layer's QA defect with the other two layers disabled.

**Targets left out on purpose** (each named in its defect's `note`):

- `scheduler-stop-ignored` on `store-stop-controls/stop`: run.py refuses before login, so the scheduler is never reached.
- `missing-store-fresh` on `store-stop-controls/unprovisioned`: the dispatch refuses on a missing store before any load.
- `price-under` on `paid-boundary/exact-500`: an underestimate still sends, and the fake charges the real price.
- `intent-not-persisted` on `paid-boundary/exact-500`: only the exit changes, which is not specific enough.
- `intent-after-send` on `persisted-intent-restart/intent-write-fails`: its armed fault replaces the intent write itself.
- `two-copy-as-one` on `two-copy-recipe/two-idle`: both copies are present there.
- `stop-checked-once` on `store-stop-controls/stop-mid-run`: the stop file appears before the first action, so a single read still sees it.
- `settle-failure-swallowed` on the frozen `persisted-intent-restart/settle-write-fails`: that trace leaves exec1's exit open, and the pending intent still blocks exec2. Only the derived exit and skip of exec1 show that the account was not stopped.
- `training-block-dropped` on the frozen traces: the runner's guard still refuses the start with the same skip code, so only the exec2 exit the derived variants freeze (0 on the clean revision) changes.
- Timing-dependent hard codes: without the lock, whether the second dispatch's start lands (`duplicate_write`, `budget_spend`) or its store write is refused depends on timing, in threads and processes mode alike. `lock-skipped` and the threads target of `v2-down-v1-path` therefore expect only the deterministic named assertions (`overlap:dispatches`, plus `provision:store.alpha` for `v2-down-v1-path`). Those were present in 15 of 15 repeated runs, 5 runs at each seed.

**QA-eval rounds:**

| Round | Revision | Workflow gate | Critical detected | Non-critical survivors | Planted production defects | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `08b14b9` (QA), fix round 1 after | pass, report unchanged | 28 of 28 at QA; 39 of 39 after the fix | 3 (published only) | 17 planted: 5 caught by the workflow module, 11 caught only in other test modules, 1 survived everything (D5) | The fix kills 11 of the 12 workflow-channel survivors in the workflow module (11 new critical defects, the seam-liveness check and `CapLayerTests`). D14 is caught only in the test channel. |
| 2 | `c171b10` (QA), fix round 2 after | pass, report unchanged | 39 of 39 at QA; 48 of 48 after the fix | 3 (published only) | 15 planted: 5 caught by the workflow module, 9 caught only in other test modules (`test_fleet_execute`, `test_training_attempts`, `test_uncertain_spend`, `test_simulation`, `test_crew_plan`), 1 survived everything (D15). Step 0: the five re-planted round 1 defects (D3, D5, D8, D13, D17) were all caught by the workflow module | The fix kills D15 and seven of the nine elsewhere-only defects (D1, D2, D5, D7, D11, D13, D14), and replaces D3's missing-witness catch with behavioral assertions: nine new critical defects on derived variants and six new derived-only step options. D6 and D9 stay caught only in the test channel. |

**QA round 1, planted production defects.** Each was planted in a scratch copy only (never in the worktree). "Before" is where QA found it caught; "after" is the workflow-module test that fails when the defect is planted again on the fixed revision.

| QA id | Planted change | Before fix round 1 | After fix round 1 |
| --- | --- | --- | --- |
| D1 | settled record treats unknown as verified | workflow module (gate score) | workflow module (unchanged) |
| D2 | intent-write failure swallowed, start sent | workflow module (`intent-write-fails`) | workflow module (unchanged) |
| D3 | settle-write failure swallowed in `_settle` | only `test_fleet_execute` (`test_a_settlement_write_failure_keeps_a_pending_record_and_blocks_the_next_run`) | workflow: `settle-write-fails~exit1` clean run (`exec1:exit`, `exec1:skips.alpha.unresolved_attempt`) |
| D4 | `training_block` condition inverted | only `test_fleet_execute` (`test_a_crash_leaves_a_pending_intent_that_pauses_the_restarted_account`) | workflow: `missing-post-read~exit2` and `crash-after-send~exit2` clean runs (`exec2:exit`) |
| D5 | gate-runner spend window starts now | survived every module | workflow: `CapLayerTests` (runner layer alone: `budget_spend`) |
| D6 | dispatch releases the lock before yielding | workflow module (`overlap:dispatches`) | workflow module (unchanged) |
| D7 | resolve ignores a held lock | only `test_fleet_execute` (`test_resolve_refuses_while_locked_and_on_missing_or_unreadable_state`) | workflow: `resolved-after-reconciliation~held` clean run (`resolve:exit`, `exec3:mutations.AddTraining`) |
| D8 | store parse drops a malformed attempt | only `test_training_attempts` (`test_an_attempt_with_an_extra_key_is_unreadable_and_never_overwritten`) | workflow: `settle-write-fails~extra-field` clean run (`exec2:skips.alpha.state_unreadable`, `exec2:mutations.AddTraining`) |
| D9 | recheck spend read when the recheck is built | only `test_training_policy` (`test_the_pre_send_recheck_charges_the_spend_of_earlier_actions_in_this_run`) | workflow: `CapLayerTests` (recheck layer alone: `budget_spend`) |
| D10 | premium course passes the planner | workflow module (`paid-boundary/premium`) | workflow module (unchanged) |
| D11 | guarded path chosen by policy-name prefix | workflow module (controls) | workflow module (unchanged) |
| D12 | strict add_training spec dropped | only `test_fleet_execute` (`test_a_timer_over_by_the_post_read_is_unknown_and_blocks_the_next_start`) | workflow: `verified~timer-over` clean run (`exec:journal`, hard `false_verification`) |
| D13 | stop file read once before the loop | only `test_fleet_execute` (`test_fleet_stop_file_stops_between_actions_and_accounts`) | workflow: `earlier-reservation~stop-after-upgrade` clean run (`exec:skips.alpha.stopped`, `exec:mutations.AddTraining`) |
| D14 | resolution freshness `<=` became `<` | only `test_training_attempts` (`test_classify_resolution_needs_a_read_newer_than_the_attempt`) | test channel only (same test). The resolve run's own paced requests move the fake clock before its roster read, so a read at exactly the settlement instant cannot be built without patching production's clock in the middle of a run. |
| D15 | price recheck ignores the currency | only `test_training_attempts` (`test_a_changed_currency_at_the_same_amount_is_refused`) | workflow: `price-raised~gas-after-plan` clean run (`exec:skips.alpha.price_unavailable.<crew>`, `exec:mutations.AddTraining`) |
| D16 | journal witness reads the wrong evidence key | only `test_fleet_execute` and `test_training_attempts` (`test_a_journal_with_a_training_v2_start_is_never_provisioned_fresh`) | workflow: `settle-write-fails~store-lost` clean run (`exec2:mutations.AddTraining`, `exec2:skips.alpha.state_missing`) |
| D17 | provision overwrites an unmarked store | only `test_fleet_execute` and `test_training_attempts` (`test_provision_rewrites_a_missing_marker_only_beside_a_readable_store`) | workflow: `settle-write-fails~marker-lost` and `crash-after-send~marker-lost` clean runs (`exec2:mutations.AddTraining`) |

**QA round 2, planted production defects.** QA planted 15 defects on `c171b10`, each in a scratch copy only. Step 0 first re-planted five QA round 1 defects (D3, D5, D8, D13, D17): the workflow module caught all five with behavioral assertions. "Before" is where QA found each round 2 defect caught; "after" is the workflow-module check that fails when the defect is planted again on the fixed revision.

| QA id | Planted change | Before fix round 2 | After fix round 2 |
| --- | --- | --- | --- |
| D1 | the dispatch locks `<alias>.lock`, not the game identity's lock | only `test_fleet_execute` (second-account and lock-held tests) and `test_training_attempts` (`DispatchTests`, `FileLockTests`) | workflow: `exact-500~held` clean run (`exec:exit`, `exec:skips.alpha.locked`, `exec:mutations.AddTraining`; hard `budget_spend`) |
| D2 | a resolution records the intent id as the journal attempt id | only `test_fleet_execute` (not-running resolution tests) and `test_training_attempts` (resolution-link test) | workflow: `resolved-after-reconciliation~idle-retry` clean run (`exec3:exit`, `exec3:mutations.AddTraining`, `exec3:journal`) |
| D3 | `settled()` reads `settlement["state"]`, so a verified start keeps blocking | workflow module, by a missing witness only | workflow: `intent-write-fails~second-start` clean run (`exec2:mutations.AddTraining`, `exec2:applied.AddTraining`, `exec2:journal`) |
| D4 | the plan's `max_requests` applied before the authorization filter | workflow module (`paid-boundary`, action cap after an earlier action) | workflow module (unchanged) |
| D5 | canary, place and upgrade-ship execute take no lock | only `test_fleet_execute` (every execute mode locked) | workflow: `exact-500~canary-held` clean run (`exec:exit`, `exec:skips.alpha.locked`, `exec:other_mutations`) |
| D6 | audit `charged_spend` never charges an uncertain spend | only `test_uncertain_spend` (`ChargedSpendTests`, `SchedulerAndFleetTests`) and `test_simulation` (`test_uncertain_spend_is_charged_against_budgets`) | test channel only (same tests); see the remaining gaps |
| D7 | the store's `_replace` writes in place (not atomic) | only `test_training_attempts` (atomic write) | workflow: `intent-write-fails~replace-fails` clean run (`exec2:mutations.AddTraining`, `exec2:skips.alpha.intent_not_persisted.<crew>`; hard `budget_spend`) |
| D8 | the price recheck refuses only a summed rise | workflow module (`price-raised~gas-after-plan`) | workflow module (unchanged) |
| D9 | `classify_resolution` drops the complete-roster check | only `test_training_attempts` (`PureCoreTests.test_classify_resolution_table`, roster unread) | test channel only (same test); see the remaining gaps |
| D10 | the orchestrator's stop file is relative to the working directory | workflow module (`stop-mid-run`) | workflow module (unchanged) |
| D11 | the store read treats any `OSError` as a missing store | only `test_training_attempts` | workflow: `unreadable-state~directory` clean run (`exec2:skips.alpha.state_unreadable`) |
| D12 | `training_block` not passed to the plan | workflow module (the `~exit2` variants) | workflow module (unchanged) |
| D13 | a dry run's age counted in calendar days | only `test_fleet_execute` (dry-run age tests) | workflow: `verified~dry-run-24h-1s` clean run (`exec:exit`, `exec:skips.alpha.dry_run_stale`, `exec:mutations.AddTraining`; hard `budget_spend`) |
| D14 | unobserved funds count as enough | only `test_crew_plan` | workflow: `exact-500~funds-unobserved` clean run (`exec:mutations.AddTraining`, `exec:skips.alpha.insufficient_funds.<crew>`; hard `budget_spend`) |
| D15 | with an attempt guard, `_last_attempt_uncertain` returns False | survived every test module | workflow: `v1-lost-reply~v2-after-v1` clean run (`exec2:exit`, `exec2:skips.alpha.stopped`, `exec2:mutations.AddTraining`, `exec2:journal`; hard `duplicate_write`, `budget_spend`) |

**Remaining gaps after fix round 2.** Each is caught only in the test channel, never in the workflow channel:

- QA1 D14 (resolution freshness `<=` became `<`): caught only by `test_training_attempts` (`test_classify_resolution_needs_a_read_newer_than_the_attempt`). The resolve run's own paced requests move the fake clock before its roster read, so a read at exactly the settlement instant cannot be built without patching production's clock in the middle of a run.
- QA2 D6 (`charged_spend` never charges an uncertain spend): caught only by `test_uncertain_spend` (`ChargedSpendTests.test_audit_counts_uncertain_attempts_toward_budget_exceeded`, `ChargedSpendTests.test_spent_in_period_charges_uncertain_only_when_the_delta_is_unobserved`, `SchedulerAndFleetTests.test_uncertain_attempt_is_charged_against_the_next_runs_budget`) and `test_simulation` (`FaultScenarioTests.test_uncertain_spend_is_charged_against_budgets`). No FakePss workflow tried here journals an uncertain spend. In the `idle-retry` variant the lost start's mineral delta is journaled as observed (0), not uncertain, and a probe with a 900-mineral cap started the retry on both revisions. An unobserved delta would need a fake extension, and `tests/fakes/` is outside this slice's files.
- QA2 D9 (`classify_resolution` drops the complete-roster check): caught only by `test_training_attempts` (`PureCoreTests.test_classify_resolution_table`, the roster-unread row). FakePss cannot serve a roster that is incomplete while the attempted crew member is still present (one malformed crew record, say). That would also need a fake extension.
- Non-critical survivors, published only: `guard-latch-off`, `recheck-off-frozen` and `tie-input-order` (see the survivors above).

**Legacy qa-eval (simulator mutants), reported, not fixed here.** `.venv/bin/python scripts/training_benchmark.py --qa-eval --jobs 12` on `9d8b623` exits 1: gated detection 0.536 (minimum 0.9), 0.465 over the 703 importable mutants. `sdk/training/planner.py` scores 0.385 (537 mutants: 207 caught by tests, 158 by the benchmark, 330 drift-only) and `sdk/training/strategy.py` 0.832. The same command on a clean export of `origin/main` at `b833a16` gives byte-for-byte the same overall figures and per-target table, so this slice did not cause it: it predates the slice, and the 2026-11-25 run above (415 eligible mutants) predates the SPEC-14 planner changes. This slice changes neither `scripts/training_benchmark.py`, its qa-eval test list nor `sdk/training/*`, so it cannot repair it within its allowed files. The blocking gates, `make training-benchmark` (composite 67.261087, PASS) and `make mutation` (every blocking target at 100%), pass on `9d8b623`.

**Fix round 2 notes.** D15 needed an account that moves from training-v1 to training-v2. The training-v1 control's lost reply is not applied, so its crew member stays idle. That journals an unknown start but writes no attempt store. The variant then provisions the store and runs a training-v2 dry run while the crew member is out of the training room, so the dry run completes and plans no start. The crew member then moves back, and execute plans the same crew member and course. Only the runner's journal check refuses them. A dry run that planned the start would itself refuse on the journal's uncertainty and leave no stamped plan, and execute would then stop at `dry_run_stale` on both revisions. The `hold_lock` option now holds only the declared identity's lock: holding every lock file present also held a lock keyed by alias, which is how D1 hid from the existing `~held` variant (`resolve-ignores-lock` is unchanged on the clean revision). D13's variant runs the dry run just after a UTC midnight on the fake clock, so 24 hours and one second later is always the next calendar day. `DerivedOptionTests.test_a_dry_run_exactly_24_hours_old_still_qualifies` pins the boundary: exactly 24 hours still starts.

## Known limitations

- **One shared random stream per account.** The simulator draws every session's gain from one per-account `training_rng`, in finish order (`sdk/simulation/game.py`). A change for one crew reshuffles every later gain of every crew, so two planners never see the same draws. The simulator is frozen for this unit. The benchmark averages three seeds and reports the spread; the gate stays exact (see [Seeds, determinism and the gate](#seeds-determinism-and-the-gate)).
- **No room moves.** Neither the simulator nor the client can move crew between rooms. Weapons, shields and engines crew wait on `not_in_training_room` "move to ..." from 72%, or from the start when they sit in the other room type. The reachable headroom stops at that boundary, so the wait costs no score. `skip_moves` reports it.
- **Research never arrives mid-run.** The simulator completes "Researching" entries at a day boundary, so scenarios list only completed research.
- **Snapshot faults** are modelled only as a failed account read: the tick is skipped. The planner never sees a stale or foreign snapshot.
- **The simulator has no room or research reads, and no read faults on its reads** (follow-up for the simulator owner). `RecordingClient` builds `listRoomsViaAccessToken`, `listRoomDesigns2`, `listAllResearches` and `listAllResearchDesigns2` itself, and emulates a failed `listAllCharactersOfUser` with `snapshot()` and a clock rewind. Other simulator users (Unit 9's executor tests, the `run.py` simulation) do not get these reads, and the emulation depends on `snapshot()` ticking the clock exactly once. Moving the reads and the read-fault handling into `SimulatedGame` (`sdk/simulation/game.py`) would fix it once for everyone. The simulator is outside this unit's files, so the benchmark keeps the emulation for now.
- **`BenchSubprocessRunner` duplicates `mutation.SubprocessTestRunner`** (follow-up for the owner of `scripts/mutation.py`). Both carry the same process lifecycle: the lock, the live set, the closed flag, a new session, kill on timeout or interrupt, `drain_killed` and `kill_all`. `SubprocessTestRunner` builds its own command and parses test output inside `__call__`, so it cannot be reused by composition without editing it. The benchmark reuses the public helpers it can (`child_environment`, `kill_process_tree`, `drain_killed`, `CHILD_TMP`). A shared runner in `scripts/mutation.py` that takes the command and the result parser would remove about 50 duplicated lines.
- **Cap overshoot** is reported, not gated (see [Oracles](#oracles)).
- **A design-research boundary is invisible to the benchmark.** Every gated catalog design needs course 201 or 203 of its line (Fitness for gym designs, Education for academy designs), so a planner that demanded a higher course for a design's own research behaves identically here. The planner tests catch that mutant.
- **The script's own mutation target** is not in `automation/mutation/config.json`, because `tests/test_mutation_harness.py` requires every target path to start with `sdk/`. Measure it with `make mutation TARGET=scripts/training_benchmark.py TESTS="tests.test_training_benchmark" JOBS=6`. The `copy` list already covers the script and the scenarios. The full run measured 90.4% (2426 mutants: 2188 killed, 4 timeouts, 234 survived). Re-running the 234 survivors against the final tests killed 49 more, which puts the score at 92.4%. The rest are mostly message strings, dataclass defaults and response envelope keys that `build_context` ignores.
- **The reusable-manifest sync.** `automation/reusable-manifest.json` syncs `automation/benchmark` as a whole directory (with `delete_stale`) into the reusable repo-automation template, so the crew-training scenarios and baseline are exported with it. Excluding the path is an owner decision.

## Planner findings (report only)

These are what the metrics reveal about the owner-approved planner. None of them is fixed here, and none of them is gated. The counts are per scenario at its own seed.

1. **Training at high fatigue for no gain.** The fatigue gate is at 100 only. In fatigued-crew the planner runs 216 sessions per seed that gain nothing, because sessions started at fatigue 99 draw 0. A planner that rests at 90 or more runs about 23, and scores higher (9.33 against 8.94). The planner never rests.
2. **The band gate and the design gate disagree.** The band gate accepts either research family, but each design needs its own family's research.
   - fitness-only: 484 `research_missing` skips. Weapons and shields crew pass the T2 band gate through Fitness 201, then fail on Education 201.
   - long-run: 1124 (no Education 203 for the academy T3 designs).
   - mixed-rooms: 148 (gym designs need Fitness).
   - consumable-windows: 608 (Education 101 only, so no T2 for the academy crew).
3. **The design-research consumable fallback can never fire with the default rules.** The planner allows a consumable when a design's own research is missing. However, the Common rule needs 29% or less, where every band trains a T1 design that needs no research, and the Hero rule needs 90% or more, where no band applies. `test_each_consumable_rule` documents it.
4. **Waiting to move is the largest idle share.** "move to" skips: mixed-rooms 464, consumable-windows 168, mid-level-crew 155, fault-contradicted 131, fresh-crew 127. This is a planner/executor gap: nothing can carry out a move.
5. **Cap overshoot.** The last session can end above the cap: near-cap-crew 37 TP, fresh-crew 32, long-run 25, fault-contradicted 24. The planner has no gain-aware stop near the cap.
6. **Consumables rarely fire.** The Common rule needs fatigue exactly 0 at 29% or less, so it fires only while a session is running or when research blocks every start. The Hero rule needs 90% or more and fatigue 100. Items were used only in consumable-windows (7), missing-research (6) and fatigued-crew (1) per seed. Stock sits unused in near-cap-crew (4 items), long-run (5) and no-training-room (9).
7. **The last 10% is unreachable for some crew.** Crew whose cap is above 90% of capacity (captains at 200/200, Common, Elite, Standard) stop at 90% (`complete`); only a Hero item at fatigue 100 can add more. For example, complete skips: fault-reject 167, fault-raise 82, fault-ambiguous 81.
8. **A failed finish costs one tick.** After a refused, raised or ambiguous finish, the advisory decision is dropped (`dropped` = 1 in fault-reject, fault-raise and fault-ambiguous), and the crew restarts an hour later.

No oracle disagreement was found: the unmodified planner trips none of the nine oracles on any scenario at any seed.

## Not the ProgramBench package

`automation/benchmark/` also holds the ProgramBench Python package (`adapter.py`, `run.py`, `scoring.py`, ...). It is unrelated, and it never reads the `training/` subdirectory. See the reusable-manifest note under [Known limitations](#known-limitations).
