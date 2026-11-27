# Capability inventory (R0 / AUTO-001)

**As of:** 2026-11-10, `main` after the offline PvP library (#91). The JSON is kept current by the drift test; the summary counts below were not recomputed for #92–#136.
**Machine-readable source:** [capability-inventory.json](capability-inventory.json)
**Drift guard:** `tests/test_capability_inventory.py` fails when a public `sdk.client.Client` method or `run.py` flag is missing from the JSON, or when a cited test no longer mentions the capability.

This inventory maps what the code contains today to the pillars in the [product roadmap](../PRODUCT_ROADMAP.md). It does not establish current live-server compatibility.

## Evidence levels

| Level | Meaning |
| --- | --- |
| `mocked-test` | A unit test exercises the capability with mocked game traffic. This proves code paths, not current server behaviour. |
| `live-claimed` | Repository documentation claims live success. This inventory did not re-verify that claim. |
| `untested` | No test references the capability. |

## Summary by pillar

98 public `Client` methods and 8 `run.py` flags.

| Pillar | Methods | mocked-test | live-claimed | untested |
| --- | --- | --- | --- | --- |
| platform: transport, auth, session | 12 | 9 | 0 | 3 |
| A: account intelligence (reads) | 34 | 21 | 0 | 13 |
| B: autonomous progression | 32 | 25 | 0 | 7 |
| C: competitive PvP | 13 | 7 | 3 | 3 |
| D: game economy | 9 | 5 | 0 | 4 |
| E: multi-account orchestration | 0 | 0 | 0 | 0 |
| F: adaptive learning | 0 | 0 | 0 | 0 |

The offline PvP assessment library in `sdk/pvp/` (pillar C) is pure analysis code with its own tests. It is not a `Client` method, so it is listed here rather than in the JSON.

## Gaps by release

### R1: reliable game state and execution

- No typed, account-scoped state snapshot exists. Reads return raw parsed XML dictionaries stored on the `Client` instance, so one `Client` object holds one account's state with no alias or observation time. Addressed by slice `r1-account-snapshot-model`.
- No action has a declared postcondition. Mutating methods (39 of 98) report success from the HTTP response or not at all. Addressed by `r1-gameplay-action-catalog`.
- There is no event journal. Outcomes go to the log file only. Addressed by `r1-gameplay-event-journal`.
- Reads with no test coverage include achievements, star systems, situations, room actions and consumable design lookups (13 in pillar A).

### R2: automated growth engine

- Boost-gauge speed-ups (research, room and ship upgrades, `speedUpBoostGauge`) have mocked tests in `tests/test_boost.py`. Mining drones and marker collection are untested. The training, crew upgrade and consumable wrappers (`addTraining`, `finishTraining`, `upgradeCharacter`, `useConsumable`, `activateItem3`) have mocked tests in `tests/test_training_endpoints.py`, but no captured live success reply yet: the reply element names they accept are assumed.
- `run.py` executes a fixed daily sequence. There is no policy that ranks actions against resources, cooldowns or PvP readiness.

### R3: PvP automation engine

- The battle chain (`createBattle9`, `acceptBattle5`, `finaliseBattle15`) is the only live-claimed flow. The claim comes from README Battle Flow notes, and nothing records which game version it held for.
- Battle history reads (`listPvPBattles2`, `listMissionBattles`) and `findUserRanking` are untested, so win/loss and trophy evidence cannot be collected yet.

### R4 to R6

- Pillar D has market reads (`listActiveMarketplaceMessages`) under mocked tests, but no listing, purchase or sale reconciliation (`SellItem3` and `Purchase4` are not implemented as `Client` methods).
- Pillars E and F now have offline code (`sdk/fleet/`, `sdk/experiments/`) but no `Client` methods. Every live `run.py` invocation still handles a single account.

## Updating this inventory

When a `Client` method or `run.py` flag is added, removed or gains tests, update the JSON entry: `pillar`, `kind` (`read-only`, `mutating` or `session`), `evidence`, and the `tests` that cite it. The drift test enforces method and flag parity and cited-test accuracy. It does not decide pillar or evidence level for you.
