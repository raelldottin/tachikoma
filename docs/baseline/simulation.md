# Offline scenario harness (R6, AUTO-014)

`sdk/simulation/` runs complete gameplay scenarios with no network, through the real registry, fleet orchestrator, action runner, journals and reports.

## Simulated game

`SimulatedGame` provides the `Client` method names the [action catalog](action-catalog.md) uses: `collectAllResources`, `collectDailyReward`, `upgradeRoom`, `addResearch`, `addTraining` and `runBattleEndToEnd`. Each changes state the way a successful server call would:

- collections add seeded random amounts of minerals and gas, and the daily reward adds starbux once per day;
- a room upgrade deducts the design's price in its own currency (minerals for `mineral:`, gas for `gas:`) and marks the room `Upgrading`, returning a raw response object the way `Client.upgradeRoom` does. An upgrade the account cannot afford, or one priced in any other currency, changes nothing; an unpriced one is free;
- research deducts its gas cost and starts `Researching`;
- training is not modelled and always fails;
- a battle moves trophies up or down by a seeded amount.

At each day boundary, upgrading rooms move to their target design and running research completes. Snapshots include trophies, so battle outcomes are observable in simulation, unlike against the live client today.

## Scenarios

A scenario is JSON with `name`, `seed`, `start`, `days`, `execute`, `design` (room and research design records), `registry` (an [account registry](fleet-accounts.md)) and `accounts` (each alias's starting state, with an optional `faults` list; see [Injected faults](#injected-faults)). Four ship with the package:

| Scenario | What it covers |
| --- | --- |
| `single-account` | Three days for one account with an 800-mineral budget: research, two room upgrades, collections and the daily reward |
| `two-accounts` | Two days for two accounts; `bravo` has a 100-mineral budget, so it never upgrades a room |
| `faults-uncertain-outcomes` | Two days, two accounts. `alpha`'s second research gets an ambiguous response and does not start, so it is `unknown` and the next day's retry is refused. `bravo`'s first research raises after taking effect (still `verified`, from state), then its room upgrade raises and the after-snapshot fails: `unknown`, with 300 minerals charged as uncertain, so the next day's 600-mineral upgrade no longer fits the 800 budget |
| `faults-contradicted-and-foreign` | Two days, two accounts. `alpha`'s first snapshot on day 0 claims to be `bravo`, so `alpha` ends that day with an error and no calls while `bravo` runs; `alpha` runs normally on day 1. The player spends minerals and gas while `bravo` collects resources, so the collection is `contradicted` and `bravo` stops for the day |

The shipped scenarios price every room in minerals, so charging gas-priced upgrades does not change their results. The two baseline scenarios have no faults, and their reports did not change when faults were added.

`run_scenario(scenario, workdir, seed=None)` writes one journal per account (`<alias>.jsonl`) in `workdir` and returns the fleet runs, the progression, fleet and audit reports as JSON, the calls each simulated game received, and the faults that fired for each account. Journals are append-only, so a `workdir` that already holds a journal for one of the scenario's accounts is refused with `FileExistsError` before anything is written; use a fresh directory per run. Other files in `workdir` are left alone, and a `STOP` file there is honoured as the fleet stop file.

## Injected faults

Each entry of an account's `faults` list is one fault (`sdk.simulation.Fault`):

| Field | Meaning |
| --- | --- |
| `kind` | `raise`, `ambiguous-response`, `snapshot-failure`, `foreign-snapshot` or `concurrent-spend` |
| `method` | A client method above, or `snapshot` for a read of the snapshot provider |
| `day` | 0-based scenario day it applies to; all days when omitted |
| `call` | 1-based invocation of `method` that triggers it, counted per account within `day` (or across the run without `day`); every invocation when omitted |
| `probability` | Chance that a triggered fault fires (default 1), drawn from a generator seeded with the scenario seed and the alias, so it does not change the game's own random amounts |
| `effect` | For `raise` and `ambiguous-response`: whether the call takes effect first (default true) |
| `alias` | For `foreign-snapshot`: the alias the snapshot claims |
| `minerals`, `gas`, `starbux` | For `concurrent-spend`: what the player spends, capped at the balance |

What each kind does:

- `raise`: the call takes effect (unless `effect` is false), then raises `SimulatedFaultError`, a `ConnectionError`;
- `ambiguous-response`: the call takes effect (unless `effect` is false) and returns `None` instead of `True` or `False`;
- `snapshot-failure`: on `snapshot`, that read raises `SimulatedFaultError`; on a client method, the next snapshot read after the call does, which is the runner's after-snapshot;
- `foreign-snapshot`: like `snapshot-failure`, but the read returns this account's state labelled with another alias;
- `concurrent-spend`: the player spends right after the call takes effect, or just before the snapshot read, so the spend lands between two snapshots.

Faults fail closed: a malformed one is refused with `ValueError` when the game is built, before anything runs or is journaled, so a typo can never make a fault fire on every call or never. Refused are an unknown kind or method; a `raise` or `ambiguous-response` on `snapshot`; a key the kind does not take (a misspelt `calls`, or `alias` on a `raise`); a `day` that is not a non-negative integer; a `call` that is not a positive integer; a `probability` that is not a number in (0, 1]; an `effect` that is not `true` or `false`; a `foreign-snapshot` without a string `alias`; and a `concurrent-spend` amount that is not a non-negative integer, or with no positive amount. Booleans are not accepted as numbers.

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

## Determinism

The same scenario and seed give byte-identical reports, with or without faults. The clock is simulated (one second per reading, jumping to each day's start), orchestrator timers are fixed at zero, journal attempt ids, which are random UUIDs on disk, are renumbered in run order (`sim-00001`, ...) before reporting, faults trigger on counted invocations or on their own seeded generator, and every run starts from empty journals because a reused workdir is refused. `tests/test_simulation.py` checks determinism, budgets, isolation, gas and mineral pricing, the reused-workdir refusal, the injected faults' safety outcomes, that an empty `faults` list changes nothing, and that no socket is opened.
