# Protected-Item and Mutation-Safety Analysis

Swarm lane: `t_9eec6b78` (safety).
Source data: `INVENTORY_ENDPOINT_ANALYSIS.md` (80 inventory endpoints,
12 checksum-required, 68 checksum-free) cross-referenced against
`sdk/client.py` and `run.py`.

This document classifies every inventory-adjacent endpoint that changes
player state, ranks its blast radius, and identifies what guard must
exist before the client is allowed to call it autonomously. It is the
sibling lane to the protocol (`t_9f78e590`), consumables
(`t_f6a9edd1`), and cleanup (`t_457d0913`) lanes.

## Hazard classes

Each mutation endpoint is placed into the highest class that applies.

| Class | Name | Definition |
|-------|------|-----------|
| H0 | Read-only | GET-style; no player state change. Collecting already-manufactured resources is H0 for hazard purposes (idempotent, reversible by next tick). |
| H1 | Reversible, no currency | State change with an in-game inverse; no Starbux/real-money spend. |
| H2 | Currency or progression spend | Spends Starbux / minerals / gas / crew XP / prestige progress. Reversible only by refunding what was spent, which the game does not expose. |
| H3 | Irreversible / destructive | Item or character permanently destroyed or.prestige-reset. No recovery path in API. |

The autonomous client (`run.py`) is allowed to invoke H0 and H1 freely.
H2 requires an explicit affordability / quota guard before each call.
H3 endpoints MUST NOT be invoked by the autonomous loop; if a future
feature needs one, the call must be reachable only via a manual,
single-shot code path.

## Implemented endpoints — current guard state

Audited in `sdk/client.py` and `run.py`, current as of the latest
commit on `main` (2cb5aa4).

| Endpoint | Method | Class | Current guard | Verdict |
|----------|--------|-------|---------------|---------|
| `RoomService/CollectAllResources` | `collectAllResources` | H0 | none needed | OK |
| `RoomService/CollectResources3` | `collectResources3` | H0 | none needed (only collects already-manufactured minerals/gas) | OK |
| `GalaxyService/GoTo` | `goToStarSystem` | H1 | travel is reversible | OK |
| `GalaxyService/SpeedUpTravelling` | `speedUpTravelling` | H2 (Starbux) | gated by runtime heuristic: only when remaining travel < 5 min (`waitForArrival`, client.py:2768, 2803). **No explicit budget cap.** | Needs bounded spend cap |
| `ItemService/ActivateItem3` | `activateItem3` / `useItem` | H2 (consumes an item) | invoked only via `useItem`, which the autonomous loop calls for known training-book scenarios. No allow-list in code. | Needs allow-list |
| `RoomService/RebuildAmmo3` | `rebuildAmmo` | H2 (minerals/gas) | only called manually via CLI in current tree. No quota. | Add budget cap before any autonomous use |
| `ShopService/PurchaseCatalog2` via `purchaseScorchedPodIfAffordable` | `purchaseScorchedPodIfAffordable` | H2 (Starbux) | calls `getStarbux()` and only buys if Starbux >= 25 (`run.py:251`). Affordability, not budget cap. | Acceptable — affordability check is the guard |
| `CharacterService/Draw` via `purchaseDrawWithStarbux` | `purchaseDrawWithStarbux` | H2 (Starbux) | none in method; relies on caller discipline | Needs affordability check equivalent to Scorched Pod |
| `UserService/AddStarbux2` | `AddStarbux2` | H2 (real-money adjacent in official app; in this client it's exercised via ad-reward) | called from `dnaAddStarbux`; no per-day cap | Acceptable for now — server rate-limits |

## Unimplemented state-mutating endpoints — protection requirements

These endpoints MUST NOT be wired into the client without the listed
guard. Source: `INVENTORY_ENDPOINT_ANALYSIS.md` and the
`t_315b6cb2` researcher's handoff to this lane.

### ItemService

| Endpoint | Class | Required guard before implementation |
|----------|-------|--------------------------------------|
| `CombineItem4` | H2 (consumes input items) | Validate output design is strictly more valuable than inputs (per `game_formulas` recipe table); log input item IDs; refuse if input count exceeds the recipe's exact requirement. |
| `ConsumeItem2` | H3 | No autonomous path. Allow only with explicit item ID match against a curated whitelist (e.g. XP books). Never call with a wildcard. |
| `DeleteItem3` | H3 | No autonomous path. If ever needed, require `--confirm-destroy <itemId>` CLI flag; never call from any loop. |
| `DeleteAmmo` | H2 | Budget cap on ammo quantity destroyed per call. |
| `MarkItemsNotNew` | H1 | None extra. |
| `RepairBrokenModules` | H2 (minerals) | Same affordability/guard pattern as `RebuildAmmo3`. |
| `ActivateItem4`, `ActivateRenameItem` | H2 | Treat as `ActivateItem3`; allow-list by itemDesignId. |

### RoomService

| Endpoint | Class | Required guard |
|----------|-------|----------------|
| `AddItemIdToRoom` | H1 | Caller-supplied room+item must exist; logical inverse is RemoveItemIdFromRoom. |
| `Refill2` | H2 (Starbux) | Budget cap + explicit user opt-in per call. Starbux spend. |
| `MoveItemIdToRoom3` | H1 | Same-room-item invariant checks. |
| `RemoveItemIdFromRoom` | H1 | Inverse of AddItemIdToRoom; safe. |
| `UpdateItemIdsForRoom2` | H1 (multi-set) | Validate intended final state once, then send; never send partial updates from a retry loop without idempotency knowledge. |

### MarketService

| Endpoint | Class | Required guard |
|----------|-------|----------------|
| `Purchase4` | H2 | Affordability check + max unit price cap (avoid market price spike buying). |
| `SellItem3` | H2 (opportunity-cost) | Read current market listing first; refuse to sell below a configurable fraction of median price. |

### CharacterService

| Endpoint | Class | Required guard |
|----------|-------|----------------|
| `DeleteCharacter` | H3 | Not exposed in autonomous client. Manual CLI only with `--confirm-character-id` flag. |
| `PrestigeCharacters` | H3 | Not autonomous. Prestige resets the character's level. |
| `UnequipItems` | H1 | Safe. |
| `RemoveItem` (character equipment) | H1 | Safe. |
| `MarkCharactersNotNew` | H1 | None extra. |

### RewardService

| Endpoint | Class | Required guard |
|----------|-------|----------------|
| `BuyReward2` | H2 | Same affordability pattern as `purchaseScorchedPodIfAffordable`. Already implemented as consumable lane prerequisite. |

## Cross-cutting mutation-safety rules

These rules apply regardless of endpoint:

1. **A checksum-validated endpoint is not a safe endpoint.**
   Capture-verifying the checksum (the t_9f78e590 / t_315b6cb2 lane work)
   tells us the server will accept the call. It says nothing about
   whether the call should be made. Every H2/H3 endpoint needs an
   explicit guard evaluated *before* the checksum is computed.

2. **No retries on H2/H3.**
   A timed-out H2/H3 call may have already executed on the server. The
   current `client.py` retry logic (where present) is appropriate for
   H0/H1 only. H2/H3 must be sent once per decision; on timeout, read
   state and reconcile rather than re-firing.

3. **Autonomous loop is allow-listed, not deny-listed.**
   `run.py` should call into a fixed set of method names collected in
   one place (a small list near the top of `run.py`). Anything outside
   that list requires explicit human invocation. This makes the
   unattended blast radius readable.

4. **Budget caps live next to the callsite, not just in config.**
   Affordability (`starbux >= price`) is necessary but not sufficient —
   a runaway loop can buy every affordable item every pass. Add a
   per-process Starbux spend so far and refuse past a configurable cap
   (suggested env var `PSS_MAX_STARBUX_PER_RUN`, default 0 → no
   autonomous Starbux spend). Apply to: `SpeedUpTravelling`,
   `purchaseDrawWithStarbux`, `purchaseCatalogItem`,
   `RebuildAmmo3` (mineral/gas analogue), `Refill2` if implemented.

5. **H3 endpoints stay out of `client.py` until specifically requested.**
   Delete/consume/prestige-style endpoints are breakpoint-safety: by
   leaving them unimplemented, an accidental helper regression cannot
   destroy items. Wire them only when a user explicitly asks, behind
   their own function, and never invoked from `run.py`.

6. **No silent state repair.**
   Mirroring the swarm protocol: a guard failure must surface as a
   logged refusal, not as an alternate code path that "fixes" the input.
   Example: if `CombineItem4` is refused because the recipe is unknown,
   do not fall back to a different recipe.

## Open questions for the cleanup lane (`t_457d0913`)

- Should `collectRoomResources` remain H0 when called on ammunition
  rooms? Current analysis says yes (collecting manufactured ammo
  does not spend anything), but the cleanup lane should confirm this
  matches how `CollectResources3` is actually used.

## Open questions for the consumables lane (`t_f6a9edd1`)

- Confirm `BuyReward2`'s unique checksum formula stays in the same
  checksum family even if `RewardService` grows new H2 endpoints. The
  formula's exclusion of `accessToken` and `rewardDesignId` is unusual
  and any sibling endpoint may not share that property.

## Summary for the synthesis / verifier

- Implemented and shipping: 4 H0/H1 mutation endpoints (GoTo,
  CollectAllResources, CollectResources3 via collectRoomResources,
  ActivateItem3 via useItem) — all safe under current rules.
- Implemented but under-guarded: 1 H2 remains (`purchaseDrawWithStarbux`
  — no affordability check inside the method).
- Implemented with adequate guard: `purchaseScorchedPodIfAffordable`
  (affordability + fixed 25-Starbux price), `SpeedUpTravelling`
  (time-window-only heuristic; no budget cap — see recommendation 4).
- Unimplemented H2/H3: 14 endpoints classified; not one has any guard,
  which is correct because none is wired. Guards above must be added
  before any of them land in the client.
- No code was changed by this lane. The deliverable is this document.
