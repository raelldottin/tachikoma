# Offline scenario harness (R6, AUTO-014)

`sdk/simulation/` runs complete gameplay scenarios with no network, through the real registry, fleet orchestrator, action runner, journals and reports.

## Simulated game

`SimulatedGame` provides the `Client` method names the [action catalog](action-catalog.md) uses: `collectAllResources`, `collectDailyReward`, `upgradeRoom`, `addResearch`, `addTraining` and `runBattleEndToEnd`, plus `finishTraining`, `activateItem3` and `useConsumable` for crew training. Each changes state the way a successful server call would:

- collections add seeded random amounts of minerals and gas, and the daily reward adds starbux once per day;
- a room upgrade deducts the design's price in its own currency (minerals for `mineral:`, gas for `gas:`) and marks the room `Upgrading`, returning a raw response object the way `Client.upgradeRoom` does. An upgrade the account cannot afford, or one priced in any other currency, changes nothing; an unpriced one is free;
- research deducts its gas cost and starts `Researching`;
- training starts, finishes and uses consumables as described in [Crew training](#crew-training);
- a battle moves trophies up or down by a seeded amount.

At each day boundary, upgrading rooms move to their target design and running research completes. Snapshots include trophies, so battle outcomes are observable in simulation, unlike against the live client today.

## Crew training

A scenario with a top-level `training` block models crew training; without one, crew is empty (a complete, empty `crew` section) `addTraining` and `finishTraining` are refused as an unknown character, and no item is a consumable. The block is separate from `design`, because `design` takes only room, research and ship designs.

| Key | Meaning |
| --- | --- |
| `fatigue_max` | Highest fatigue (default 100) |
| `fatigue_recovery_seconds` | Seconds per fatigue point recovered (default 360) |
| `character_designs` | `CharacterDesign` records; `@TrainingCapacity` (a positive integer) caps the sum of a member's nine training stats |
| `training_designs` | `TrainingDesign` records: `@TrainingStat` (one of `Hp`, `Pilot`, `Repair`, `Weapon`, `Science`, `Engine`, `Attack`, `Ability`, `Stamina`), `@Duration` in seconds, `@Fatigue` added at the start, `@MinGain`/`@MaxGain`, `@RequiredRoomDesignId` and an optional `@RequiredResearchDesignId` (`0` or empty means none) |
| `item_designs` | Consumables: `@ConsumableStat` with `@ConsumableGain` (and optional `@ConsumableFatigue`), or `@FatigueRecovery` |

Each account lists its `crew` as character records. The keys are `@CharacterId`, `@CharacterDesignId`, `@CharacterName`, `@RoomId`, `@Level`, `@Xp`, `@Fatigue`, `@TrainingDesignId`, `@TrainingEndDate` and the nine `@<Stat>Improvement` stats. Any other key is refused. Missing ones default to `0` (stats, `@Fatigue`, `@Xp`, `@TrainingDesignId`, meaning idle, and `@RoomId`, meaning no room, as the live game sends it), `1` (`@Level`) or empty. An empty `@RoomId` is read as `0`, and `0` is never a room. An idle record may keep its last session's `@TrainingEndDate`, as live records do; it is cleared on load. Consumables are `inventory` items with an `@ItemId`. Every record the game returns has every key, as strings. `@Fatigue` is computed when the record is read, and `@TrainingEndDate` is `YYYY-MM-DDTHH:MM:SS` in UTC, or empty when idle.

The rules, simplified from the real game:

- `addTraining(training_design_id, character_id)` is refused for an unknown character or design, a member who already has a session (finished or not), a member in no room (`@RoomId` `0`) or whose room's current design is not the design's room or an upgrade of it, a room being upgraded (`@RoomStatus` `Upgrading`, until the day boundary completes it), or missing completed research. Otherwise it sets the design and end, and adds the design's fatigue (capped at `fatigue_max`). Room capacity is not modelled: any number of crew can train in one room at once.
- `finishTraining(character_id)` is refused before the end (to the second) or with no session. Otherwise it adds `randint(min, max) * (fatigue_max - fatigue at the start) // fatigue_max` to the design's stat. The result is clamped so the nine stats never exceed the capacity. It also clears the session and sets `trainingFinish` to the updated record. Nothing finishes a session on its own: the real game needs `FinishTraining`, and idle time between the end and the finish is what a benchmark measures.
- `useConsumable(item_design_id, character_id)` picks the first listed item of that design, as `Client.useConsumable` does, then calls `activateItem3`. That uses one item (never below 0) and applies the stat gain (clamped like training, and still used up at capacity) and the fatigue change. It works during a session. `activateItem3` puts the outcome in `item`, with `@errorMessage` for an unknown item, a non-consumable, an empty stack or an unknown character.
- Fatigue recovers one point per `fatigue_recovery_seconds`, idle or training, and never goes below 0. Reads never change it. A start or a consumable keeps the progress toward the next recovered point, so a change that adds no fatigue never delays recovery; once fatigue is back at 0, the next point added starts a full period. A crew member loaded in the middle of a session has an unknown start, so its stored `@Fatigue` counts as the fatigue at the start.
- The defaults (`fatigue_max` 100, 360 seconds per point) are assumptions, not measured from the game. With one run a day, fatigue is back near 0 at every run, so it only matters within a run or with slower recovery.
- Reads (`listAllCharactersOfUser`, `listAllTrainingDesigns2`, `listAllCharacterDesigns2`, `listItemsOfAShip`) return `True` and set the same attributes and nesting as the `Client`, with `None` for an empty container, a lone record as one mapping rather than a one-item list, and every value a string (a JSON number in the scenario becomes its digits, and a `null` is left out), as xmltodict parses it, so `ClientSnapshotProvider` parses them. They are not calls and take no faults.

Training reads the simulated clock without advancing it, and draws only from its own generator, seeded with the scenario seed and the alias (`"<seed>:<alias>:training"`), once per finished session. Adding crew therefore never changes the economy's random amounts or timestamps. Malformed training data (any of the checks above, a stat total over capacity, an unknown room, research or design, fatigue out of range, a session without a valid end, duplicate ids, crew with no `training` block, a `training` block that is not a mapping, a designs entry that is not a list of records or a record without its id, and, when there is a `training` block, an inventory item with a duplicate `@ItemId` or a negative `@Quantity`) is refused with `ValueError` before anything runs. A crew member loaded in a session is taken as the server reported it: its room and research are not re-checked against the session's design. A scenario `start` must carry a UTC offset (the shipped ones give `+00:00`); one without is refused with `ValueError` before anything runs. Ids may be strings or JSON numbers.

### Training log and result

Every `addTraining`, `finishTraining` and `activateItem3` that reaches the server appends one entry to the account's training log, accepted or refused. A call a fault blocks (`reject`, or `raise` with `effect` false) never reaches it and leaves no entry; count those from the journal or the fired faults. Each entry has:

| Key | Meaning |
| --- | --- |
| `seq`, `at`, `day` | Position in the log (from 1), simulated time (UTC, `YYYY-MM-DDTHH:MM:SS`) and scenario day |
| `op` | `start`, `finish` or `consume` |
| `character_id`, `room_id` | The crew member and their room (`room_id` is `null` for an unknown character) |
| `design_id`, `requested_design_id`, `stat` | The design started, finished or used (a training design, or an item design for `consume`), the design requested (`start` only) and its stat |
| `accepted`, `code`, `detail` | Whether it took effect; `code` is `ok` or why not; `detail` says the same for people |
| `end` | The session's end, for an accepted start |
| `gain`, `capped` | The stat gain (`{stat: points}`), and whether the capacity cut it |
| `fatigue`, `total`, `capacity` | The member's fatigue, stats total and capacity right after the call |

The codes: for `start`, `unknown-character`, `unknown-design`, `busy` (a session exists), `wrong-room`, `room-upgrading` and `no-research`; for `finish`, `unknown-character`, `idle` and `early`; for `consume`, `unknown-item`, `not-consumable`, `out-of-stock` and `unknown-character`. Gains are clamped, so the capacity is never exceeded; a benchmark should score `capped` entries, including consumables used at capacity, rather than look for cap violations.

No shipped policy trains yet, so in a scenario run crew only recovers fatigue. Besides the reports, `run_scenario` returns, per account, the final crew records (`crew`), the training log (`training_log`), and one entry per day (`days`) with that day's start time (`at`), the crew records then (`crew`) and the rooms that train something (`training_rooms`, room id to training design ids). The days are recorded at each day boundary without advancing the clock, so they never change the reports. Day 0 is recorded at the scenario start before any call, so `initial_crew` is the crew in it. `started_at` and `ended_at` give the run's span. `training_json` holds all of them as sorted JSON.

## Scenarios

A scenario is JSON with `name`, `seed`, `start`, `days`, `execute`, `design` (room and research design records), `registry` (an [account registry](fleet-accounts.md)), an optional `training` block (see [Crew training](#crew-training)) and `accounts` (each alias's starting state, with optional `crew` and `faults` lists; see [Injected faults](#injected-faults)). Six ship with the package:

| Scenario | What it covers |
| --- | --- |
| `single-account` | Three days for one account with an 800-mineral budget: research, two room upgrades, collections and the daily reward |
| `two-accounts` | Two days for two accounts; `bravo` has a 100-mineral budget, so it never upgrades a room |
| `faults-uncertain-outcomes` | Two days, two accounts. `alpha`'s second research gets an ambiguous response and does not start, so it is `unknown` and the next day's retry is refused. `bravo`'s first research raises after taking effect (still `verified`, from state), then its room upgrade raises and the after-snapshot fails: `unknown`, with 300 minerals charged as uncertain, so the next day's 600-mineral upgrade no longer fits the 800 budget |
| `faults-contradicted-and-foreign` | Two days, two accounts. `alpha`'s first snapshot on day 0 claims to be `bravo`, so `alpha` ends that day with an error and no calls while `bravo` runs; `alpha` runs normally on day 1. The player spends minerals and gas while `bravo` collects resources, so the collection is `contradicted` and `bravo` stops for the day |
| `crew-training-single-account` | Three days, one account with a Gym, an Academy and three crew members: one idle (fatigue 40 recovers to 16 at 2 hours per point), one in a session that ended at 08:00 on day 0 and stays unfinished, one on the Bridge. Its reports equal the same scenario without crew |
| `crew-training-faults` | Two days, the same crew with training faults: day 0's first `addTraining` starts `t-acad-1` instead (`contradicted`), the second is rejected (`not-observed`), day 1's first `finishTraining` raises, and the first `activateItem3` is rejected. Under `run_scenario` this scenario is inert: no shipped policy trains yet, so none of its faults fires and its crew only recovers fatigue. The tests drive them through the action runner, and through a run with a stand-in policy |

The shipped scenarios price every room in minerals, so charging gas-priced upgrades does not change their results. The two baseline scenarios have no faults, and their reports did not change when faults were added.

`run_scenario(scenario, workdir, seed=None)` writes one journal per account (`<alias>.jsonl`) in `workdir` and returns the fleet runs, the progression, fleet and audit reports as JSON, the calls each simulated game received, and the faults that fired for each account, plus the crew training results in [Training log and result](#training-log-and-result). Journals are append-only, so a `workdir` that already holds a journal for one of the scenario's accounts is refused with `FileExistsError` before anything is written; use a fresh directory per run. Other files in `workdir` are left alone, and a `STOP` file there is honoured as the fleet stop file.

## Injected faults

Each entry of an account's `faults` list is one fault (`sdk.simulation.Fault`):

| Field | Meaning |
| --- | --- |
| `kind` | `raise`, `ambiguous-response`, `snapshot-failure`, `foreign-snapshot`, `concurrent-spend`, `reject` or `contradicted-effect` |
| `method` | A client method above (including `finishTraining` and `activateItem3`), or `snapshot` for a read of the snapshot provider |
| `day` | 0-based scenario day it applies to; all days when omitted |
| `call` | 1-based invocation of `method` that triggers it, counted per account within `day` (or across the run without `day`); every invocation when omitted |
| `probability` | Chance that a triggered fault fires (default 1), drawn from a generator seeded with the scenario seed and the alias, so it does not change the game's own random amounts |
| `effect` | For `raise` and `ambiguous-response`: whether the call takes effect first (default true) |
| `alias` | For `foreign-snapshot`: the alias the snapshot claims |
| `minerals`, `gas`, `starbux` | For `concurrent-spend`: what the player spends, capped at the balance |
| `training_design_id` | For `contradicted-effect`: the training design the server starts instead, as a string or a JSON integer (it must be in the `training` block) |

What each kind does:

- `raise`: the call takes effect (unless `effect` is false), then raises `SimulatedFaultError`, a `ConnectionError`;
- `ambiguous-response`: the call takes effect (unless `effect` is false) and returns `None` instead of `True` or `False`;
- `snapshot-failure`: on `snapshot`, that read raises `SimulatedFaultError`; on a client method, the next snapshot read after the call does, which is the runner's after-snapshot;
- `foreign-snapshot`: like `snapshot-failure`, but the read returns this account's state labelled with another alias;
- `concurrent-spend`: the player spends right after the call takes effect, or just before the snapshot read, so the spend lands between two snapshots;
- `reject`: on `addTraining`, `finishTraining` or `activateItem3`, the server refuses the call. Nothing changes, and it returns the `Client`'s failure value (`False`, or an `item` with `@errorMessage`, so `useConsumable` returns `False`);
- `contradicted-effect`: on `addTraining`, a request the server accepts starts `training_design_id` instead, with that design's duration and fatigue, and returns `True`. A request it would refuse is still refused. When the substitute is the requested design, the server did what was asked: it is not a contradiction, and the runner verifies it.

When a `reject` and a `contradicted-effect` both fire on one call, the one listed first decides.

Faults fail closed: a malformed one is refused with `ValueError` when the game is built, before anything runs or is journaled, so a typo can never make a fault fire on every call or never. Refused are an unknown kind or method; a `raise` or `ambiguous-response` on `snapshot`; a key the kind does not take (a misspelt `calls`, or `alias` on a `raise`); a `day` that is not a non-negative integer; a `call` that is not a positive integer; a `probability` that is not a number in (0, 1]; an `effect` that is not `true` or `false`; a `foreign-snapshot` without a string `alias`; a `concurrent-spend` amount that is not a non-negative integer, or with no positive amount; a `reject` on anything but `addTraining`, `finishTraining` or `activateItem3`; and a `contradicted-effect` on anything but `addTraining`, without a `training_design_id` that is a non-empty string or an integer, or naming a design the `training` block does not have. Booleans are not accepted as numbers.

The safety outcomes these faults produce, each checked end to end in `tests/test_simulation.py`:

| Fault | Outcome |
| --- | --- |
| Call raises after taking effect, effect observed | `verified` from state, with `error_class` journaled and `response_ok` `None` |
| After-snapshot fails after the effect | `unknown`; resource deltas are not journaled (never 0), and the planned cost is journaled as `<kind>_uncertain` |
| Ambiguous response, no effect | `unknown`; the scheduler stops the account, and a later retry of the same non-idempotent action with the same parameters is refused (`UncertainPriorAttemptError`) without calling the game |
| Uncertain spend | Charged by `spent_in_period` (lowering the next run's remaining budget) and by the audit's `budget-exceeded` rule, which says how much of it was uncertain |
| Foreign first snapshot | That account's run ends with status `error` and no calls or journal events; other accounts run |
| Foreign after-snapshot | `unknown` with `AccountMismatchError`; the account stops |
| Player spends during a collection | The collection is `contradicted` and the account stops for that run. The player's spend shows in the observed delta, so budgets charge it |
| Training started a different design (`contradicted-effect`) | `add_training` is `contradicted` |
| Training refused (`reject`) | `add_training` is `not-observed`, with `response_ok` `False` |

## Determinism

The same scenario and seed give byte-identical reports, with or without faults, and byte-identical `training_json` (final crew and training log). The clock is simulated (one second per reading, jumping to each day's start), orchestrator timers are fixed at zero, journal attempt ids, which are random UUIDs on disk, are renumbered in run order (`sim-00001`, ...) before reporting, faults trigger on counted invocations or on their own seeded generator, training gains come from their own seeded generator, and every run starts from empty journals because a reused workdir is refused. `tests/test_simulation.py` checks determinism, budgets, isolation, gas and mineral pricing, the reused-workdir refusal, the injected faults' safety outcomes, that an empty `faults` list changes nothing, that no socket is opened, that the four scenarios without crew match digests recorded before crew training existed, and that the two crew-training scenarios match digests that include `training_json`. `tests/test_simulation_training.py` checks the training state machine, its log entries and codes, consumables, read shapes against `ClientSnapshotProvider`, the training faults through the action runner and through a scenario run with a stand-in training policy, the per-day records, malformed training data, seeding, and a 300-seed property loop. The loop mixes starts, finishes, item uses, room upgrades, day boundaries, clock advances and snapshots with randomly firing `raise`, `reject` and `contradicted-effect` faults. After every step it checks the outcome and refusal code against its own model of the rules, that stats never exceed capacity, that a session never starts while one exists or in a room being upgraded, that none finishes early, and that the log grew by exactly one entry for a call that reached the server, agreeing with the crew record read straight after.
