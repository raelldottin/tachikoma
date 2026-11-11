# Offline scenario harness (R6, AUTO-014)

`sdk/simulation/` runs complete gameplay scenarios with no network, through the real registry, fleet orchestrator, action runner, journals and reports.

## Simulated game

`SimulatedGame` provides the `Client` method names the [action catalog](action-catalog.md) uses: `collectAllResources`, `collectDailyReward`, `upgradeRoom`, `addResearch`, `addTraining` and `runBattleEndToEnd`. Each changes state the way a successful server call would:

- collections add seeded random amounts of minerals and gas, and the daily reward adds starbux once per day;
- a room upgrade deducts the design's mineral price and marks the room `Upgrading`, returning a raw response object the way `Client.upgradeRoom` does;
- research deducts its gas cost and starts `Researching`;
- training is not modelled and always fails;
- a battle moves trophies up or down by a seeded amount.

At each day boundary, upgrading rooms move to their target design and running research completes. Snapshots include trophies, so battle outcomes are observable in simulation, unlike against the live client today.

## Scenarios

A scenario is JSON with `name`, `seed`, `start`, `days`, `execute`, `design` (room and research design records), `registry` (an [account registry](fleet-accounts.md)) and `accounts` (each alias's starting state). Two ship with the package:

| Scenario | What it covers |
| --- | --- |
| `single-account` | Three days for one account with an 800-mineral budget: research, two room upgrades, collections and the daily reward |
| `two-accounts` | Two days for two accounts; `bravo` has a 100-mineral budget, so it never upgrades a room |

`run_scenario(scenario, workdir, seed=None)` returns the fleet runs, the progression, fleet and audit reports as JSON, and the calls each simulated game received.

## Determinism

The same scenario and seed give byte-identical reports. The clock is simulated (one second per reading, jumping to each day's start), orchestrator timers are fixed at zero, and journal attempt ids, which are random UUIDs on disk, are renumbered in run order (`sim-00001`, ...) before reporting. `tests/test_simulation.py` checks determinism, budgets, isolation, and that no socket is opened.
