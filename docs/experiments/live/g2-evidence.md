# G2 evidence: first live execution on pss5, 2026-11-12

**Gate:** G2, approved by the owner on 2026-11-12 for execution on `pss5` only, under [authorization-g2-pss5-canary.json](authorization-g2-pss5-canary.json) (canary mode only; `collect_all_resources` and `collect_daily_reward`; starbux cap 0; valid to 2026-11-26). See the [runbook](RUNBOOK.md).
**Who ran it:** the owner, by dispatching [live-trial.yml](../../../.github/workflows/live-trial.yml) on `main`. Agents only downloaded the artifacts, re-scanned them and read them. Raw run records and journals are not committed: they hold room and attempt ids.

## Runs

| Run | Step | Executed | Outcome | Notes |
| --- | --- | --- | --- | --- |
| 43247933379 | enrol | nothing (dry run) | — | gave pss5's salted fingerprint, recorded in #204 |
| 43251373229 | enrol | nothing (dry run) | — | the same fingerprint: the identity check is stable |
| 43256792025 | canary | nothing | — | minerals and gas were never observed (live items carry only `ItemDesignId`), and with no safe-to-retry request the canary had nothing to run; fixed in #214 |
| 43261519416 | canary | one `collect_all_resources` | `not-observed`: minerals and gas unchanged | server version `1.0+settings.3307714` recorded; one `no-verified-action` warning; dashboard written |
| 43262412245 | canary | one `collect_all_resources` | `not-observed`: minerals and gas unchanged | as above |
| 43284132171 | canary | one `collect_all_resources` | `not-observed`: minerals and gas unchanged | about two hours after the previous canary, for SPEC-07's verified-canary precondition (S07-R2); as above |
| 43305786623 | canary | one `collect_all_resources` | `not-observed`: minerals and gas unchanged | first run with room-fill evidence: every room at `CapacityUsed` 0 before and after, `room_fill_note` set; pss5 has no collector to collect from |
| 43308552399 | canary | one `collect_all_resources` | `not-observed`: minerals and gas unchanged | the same nine rooms at `CapacityUsed` 0; no collector had been built yet |
| 43394041693 | canary | one `collect_all_resources` | **`verified`: minerals +10**, gas unchanged | 2026-11-13, about five minutes after SPEC-13's run 43393262041 placed a Gas Collector Lv1 at row 1, column 1; no spend, no alerts |
| 43401277661 | canary | one `collect_all_resources` | **`verified`: minerals +51**, gas unchanged | 2026-11-13, after the owner played pss5 by hand that morning (see below); no spend, no alerts |
| 43581425387 | canary | one `collect_all_resources` | **`verified`: gas +11,289, minerals +11,382** | 2026-11-14, main 033dd32, the first run after #269 removed `psycopg2-binary` (canary 43476718839 had failed installing it); the owner had kept playing pss5 (see below); no spend, no alerts |

In all eight executing runs:

- exactly one action ran, the first safe-to-retry one (S05-R4); `collect_daily_reward` was held back as not safe to retry;
- nothing was spent and starbux stayed disabled (S05-R6); every room upgrade and research was refused by the caps (0) or for its starbux cost;
- the dry-run stamp, the authorization and the identity check passed (S05-R3, R8); the run record holds the authorization and registry hashes, the per-account server version and the results (S05-R7);
- alerts were evaluated and the dashboard written after the run (S11-R1); the only alert was the `no-verified-action` warning, which is correct: an API success is never a verified effect.

## Reconciliation

The first five collections reconciled to `not-observed`: the before and after snapshots showed the same mineral and gas balances. The plans reported minerals above the reserve and no gas above it. The likely cause is that nothing was waiting to be collected, or that storage was full. pss5's ship capture from earlier that day showed every room with `CapacityUsed` 0 and `PowerGenerated` 0. The runs cannot tell which, because the journal did not record room fill. Since `collect-room-fill-evidence`, a collection's journal evidence lists, per room design, the `CapacityUsed` before and after the call, with the design's name, room type, manufacture type and capacity. When the total was 0 before a `not-observed` collection, it says so. The next canary should therefore help tell the cases apart, although what `CapacityUsed` counts for each room type is still to be confirmed against live data. Nothing is claimed as verified.

## Why the collections had nothing to collect

Canary 43305786623 was the first run with room-fill evidence. It journaled every pss5 room by design:

| Room design | Rooms | Room type | Manufacture type | `CapacityUsed` before → after |
| --- | --- | --- | --- | --- |
| Bridge Lv1 | 1 | Bridge | None | 0 → 0 |
| Bedroom Lv2 | 1 | Bedroom | None | 0 → 0 |
| Bolter Lv1 | 1 | Laser | None | 0 → 0 |
| Lift Lv1 | 3 | Lift | None | 0 → 0 |
| Engine Lv1 | 1 | Engine | None | 0 → 0 |
| Small Reactor Lv1 | 1 | Reactor | None | 0 → 0 |
| Mineral Mining Laser Lv1 | 1 | Laser | Mineral | 0 → 0 |

pss5 has no mineral or gas collector and no storage room, so `collect_all_resources` has nothing to collect, and no canary on pss5 can reconcile a collection as `verified` while the ship stays like this. That is why the four collections were `not-observed`. It is not a defect in the before-and-after check. Gas is also 0 above the reserve, so `baseline-v1` refused every research and room upgrade (108 rejected candidates in this run).

## The verified canary

Canary 43394041693 (2026-11-13, main 7d68240) reconciled its collection as `verified`: minerals rose by 10, and gas was unchanged. It ran about five minutes after SPEC-13's placement run 43393262041 added a Gas Collector Lv1 at row 1, column 1. Its room-fill evidence still listed that collector, but every room, the new collector included, read `CapacityUsed` 0 before and after the call. The journal therefore cannot say which room the minerals came from; the Mineral Mining Laser is the likely source. It is not a refund of the placement, because the collector is still on the ship. Nothing else touches pss5: the legacy daily run skips it. This is the first live verified effect, and it meets SPEC-07's precondition (S07-R2).

## The owner's pre-trial play

The owner found pss5 in the game and played it by hand on the morning of 2026-11-13, before the trial (confirmed by the owner). Canary 43401277661 reconciled `verified` again (minerals +51). Its room-fill evidence shows the ship after that play:

| Room design | Rooms |
| --- | --- |
| Bridge Lv1 | 1 |
| Bedroom Lv2 | 1 |
| Armor Lv1 | 2 |
| Armor Lv2 | 3 |
| Bolter Lv2 | 1 |
| Small Reactor Lv2 | 1 |
| Gym Lv1 | 1 |
| Lift Lv1 | 6 |
| Mineral Mining Laser Lv1 | 1 |
| Mineral Storage Lv1 | 1 |
| Gas Storage Lv1 | 1 |
| Shield Generator Lv1 | 1 |

The Engine Lv1 and the Gas Collector Lv1 that SPEC-13 placed are no longer on the ship, and gas is 1,517 above the reserve. This play is before the trial, so it is not an intervention.

## The third verified canary

Canary 43581425387 (2026-11-14, main 033dd32) is the first live run since #269 removed the unused `psycopg2-binary`, which no longer built on the runner and had failed the install step of canary 43476718839. Its dependencies installed cleanly. Its collection reconciled `verified`: gas rose by 11,289 and minerals by 11,382. This is pss5's first verified gas collection. Nothing was spent, and no alert fired.

The owner had kept playing pss5 by hand since 2026-11-13, still before the trial. The room-fill evidence shows the ship on 2026-11-14:

| Room design | Rooms |
| --- | --- |
| Bridge Lv3 | 1 |
| Command Center Lv1 | 1 |
| Bedroom Lv3 | 2 |
| Laboratory Lv1 | 1 |
| Armor Lv1, Lv2, Lv3 | 2, 7, 6 |
| Bolter Lv3 | 1 |
| Missile Launcher Lv2 | 1 |
| Missile Pod Lv1 | 1 |
| Shield Generator Lv6 | 1 |
| Small Reactor Lv5, Lv6 | 1, 1 |
| Gas Collector Lv1, Lv6 | 1, 1 |
| Mineral Extractor Lv1, Lv5 | 1, 1 |
| Mineral Mining Laser Lv4 | 2 |
| Mineral Storage Lv5 | 1 |
| Gas Storage Lv5 | 1 |
| Lift Lv1 | 7 |

Every room's `CapacityUsed` was the same before and after the collection: 70 in the Bolter Lv3 (full) and 0 elsewhere.

## What this does not show

- Gas from SPEC-13's collector: the owner removed it before it produced anything. Gas collection itself was verified by canary 43581425387, from collectors the owner built.
- That the owner has looked at the alerts and dashboard (S11-AC3); they are in each canary artifact (`journals/dashboard.html`), which expires after seven days.
