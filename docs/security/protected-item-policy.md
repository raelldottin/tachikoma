# Protected-Item Policy, Mutation Boundaries, and Reconciliation Safety

Swarm lane: `t_a20d45b8` (safety / policy synthesis).
Swarm root: `t_bd458ad8`.
Sources read:
- `INVENTORY_ENDPOINT_ANALYSIS.md`
- `docs/security/mutation-safety-analysis.md` (sibling safety lane `t_9eec6b78`)
- `docs/security/consumables-analysis.md` (sibling consumables lane `t_f6a9edd1`)
- `docs/security/cleanup-architecture-analysis.md` (sibling cleanup lane `t_457d0913`)
- `sdk/client.py`, `sdk/security.py`, `run.py`

This document is the policy/verification half of the swarm synthesis. It
defines which items may never be touched autonomously, which endpoints may
never be wired into autonomous code, what reconciliation must look like
before any mutation is allowed, and what is verified vs. inferred today.

No code was changed by this lane.

---

## 1. Verification matrix

Status labels (from the swarm root brief):
VERIFIED = capture-verified on live traffic, checksum proven.
CAPTURED = seen in official-client capture, checksum not necessarily reproduced.
STATIC  = present in IL2CPP `global-metadata.dat` only.
INFERRED = deduced from pattern / surrounding code; no direct evidence.
UNKNOWN = named in code or templates but template truncated.

| Endpoint | Evidence | Status |
|----------|----------|--------|
| `RoomService/CollectAllResources` | sdk/client.py + 4 captures | VERIFIED (no checksum) |
| `RoomService/CollectResources3` | sdk/client.py + checksum fns in sdk/security.py | VERIFIED |
| `RoomService/RebuildAmmo3` | sdk/security.py `checksum_rebuild_ammo3` + 1 capture | VERIFIED |
| `RewardService/BuyReward2` | sdk/security.py + 8 captures, unique preimage `clientDateTime` only | VERIFIED |
| `UserService/AddStarbux2` | sdk/security.py | VERIFIED |
| `GalaxyService/GoTo` | sdk/security.py `checksum_go_to` + capture | VERIFIED |
| `GalaxyService/SpeedUpTravelling` | sdk/security.py `checksum_speedup_travelling` + capture | VERIFIED |
| `CharacterService/Draw` | sdk/client.py + `checksum_character_draw` | VERIFIED |
| `ShopService/PurchaseCatalog2` | sdk/security.py + capture | VERIFIED (old-style numeric) |
| `UserService/HeartBeat4` | sdk/security.py | VERIFIED (old-style numeric) |
| `ItemService/ActivateItem3` | sdk/client.py (`activateItem3`, `useConsumable`) | IMPLEMENTED, NOT capture-verified on this repo's captures |
| `ItemService/ActivateItem4` | IL2CPP only | STATIC |
| `ItemService/ActivateRenameItem` | IL2CPP only | STATIC |
| `ItemService/ConsumeItem2` | IL2CPP only | STATIC |
| `ItemService/DeleteItem3` | IL2CPP only | STATIC |
| `ItemService/DeleteAmmo` | IL2CPP only | STATIC |
| `ItemService/CombineItem4` | IL2CPP only | STATIC — checksum preimage inferred |
| `ItemService/GiftItem` | IL2CPP only | STATIC |
| `ItemService/RepairBrokenModules` | IL2CPP only | STATIC |
| `ItemService/MarkItemsNotNew` | IL2CPP template truncated | UNKNOWN |
| `ItemService/FixUnsynchronizedItems` | IL2CPP only | STATIC |
| `ItemService/ListItemDesigns2`, `ListItemDesignActions`, `ListItemsOfAShip` | read-only; ListItemsOfAShip implemented | VERIFIED (read) |
| `RoomService/AddItemIdToRoom` | IL2CPP only | STATIC |
| `RoomService/MoveItemIdToRoom3` | IL2CPP only | STATIC |
| `RoomService/RemoveItemIdFromRoom` | IL2CPP only | STATIC |
| `RoomService/UpdateItemIdsForRoom2` | IL2CPP only | STATIC |
| `RoomService/Refill2` | IL2CPP only | STATIC — inferred Starbux spend |
| `MarketService/SellItem3` | IL2CPP only | STATIC |
| `MarketService/Purchase4` | IL2CPP only | STATIC — checksum preimage inferred |
| `MarketService/ListSalesByItemDesignId` | IL2CPP only | STATIC (read) |
| `CharacterService/WearItem` | IL2CPP only | STATIC |
| `CharacterService/UnequipItems` | IL2CPP only | STATIC |
| `CharacterService/RemoveItem` | IL2CPP only | STATIC |
| `CharacterService/PrestigeCharacters` | IL2CPP only | STATIC — H3 |
| `CharacterService/DeleteCharacter` | IL2CPP only | STATIC — H3 |
| `CharacterService/ConsumeCharacter` | IL2CPP only | STATIC — H3 |

Only endpoints marked VERIFIED may be referenced by autonomous code today.
Anything else, regardless of how simple the URL template looks, is not
callable until captures or reproduction against a test account prove the
preimage and semantics.

## 2. Protected-item rules (Workstream C / required output 13)

The following item classes are protected: no autonomous code may delete,
consume, sell, gift, combine, or otherwise irreversibly transform them.

1. Equipped items (`CharacterService` equipment references on
   `ListAllCharactersOfUser`).
2. Desired future equipment (any item reserved by an equipment plan).
3. Inputs to any planned `ListCraftDesigns` recipe the player has elected.
4. Ship modules currently attached to rooms (`ListRoomsViaAccessToken`
   yields rooms with `ItemIds`).
5. Rare / Epic / Hero / Unique / Legendary rarities
   (`ItemDesign.Rarity`).
6. Event-limited items (any `ItemDesign` whose `ItemDesignId` appears in
   a seasonal/reward catalog not present year-round).
7. Quest or progression items (`ItemType == "Quest"` or design flagged
   "non-tradeable / non-discardable" server-side).
8. Premium items (anything with a Starbux purchase path or marked
   premium currency equivalent).
9. Marketplace-valuable items (current sales median above a configured
   threshold per `ListSalesByItemDesignId`).
10. Unknown designs — any `ItemDesignId` not present in the cached
    `ListItemDesigns2` snapshot is protected by default.

Anything not on this list is still not disposable until Workstream C's
demand math shows zero reservation and positive available surplus.

## 3. Retention thresholds and per-run limits (required output 14)

Conceptual per-design retention check:

  available surplus = quantity
                    - training reservation
                    - crew-equipment reservation
                    - crafting reservation
                    - ship-module reservation
                    - strategic reserve

Only positive surplus may be considered for reduction. Suggested defaults
(configurable; none of these are wired into code yet):

- strategic reserve per craftable ingredient: >= 1 full recipe
- strategic reserve per training consumable tier: >= 5
- strategic reserve per ammo: >= 1 room reload
- per-run mutation caps (defence in depth):
  - item deletions per run: 0 (autonomous)
  - item consumptions per run: bounded by training plan only
  - items gifted/sold per run: 0 (autonomous)
  - Starbux spend per run: environment-bounded (suggested
    `PSS_MAX_STARBUX_PER_RUN`, default 0)

## 4. Mutation-safety architecture (required output 17)

Layers, top to bottom. Only the read paths exist today.

1. InventoryCatalog — read/normalize `ListItemsOfAShip`,
   `ListItemDesigns2`, `ListItemDesignActions`. Pure reads.
2. InventoryDemandPlanner — derives reservations from training plans,
   equipment plans, crafting designs, ship-module plans.
3. InventoryClassifier — assigns each `ItemId` to
   protected / reserved / surplus / unknown.
4. AccountPortfolio — per-account surplus/deficit rollup across the
   user's own accounts.
5. InventoryReductionPlanner — emits a deterministic, ordered list of
   candidate actions. Dry-run output only.
6. TransferPlanner — pairs deficits with surpluses; emits GiftItem or
   marketplace pairing proposals. Never executes.
7. InventoryExecutor — gated mutation entrypoint. Hard-disabled by
   default; refused unless explicitly enabled per endpoint per call.
8. InventoryReconciler — wraps every mutation. Snapshot -> mutate ->
   refetch -> verify delta -> continue. Any mismatch aborts the run.

Reconciliation contract for any future mutation:

  snapshot items of interest (item_id, quantity, location)
  apply exactly one mutation
  re-fetch the same inventory slice
  verify the intended delta and no other delta
  write an audit row (endpoint, preimage hash, before, after)
  abort the run if any of these checks fail

Stop conditions (any one aborts the run):
- server response is ambiguous (2xx with error body, 5xx, schema drift)
- the target item disappears unexpectedly
- quantity changes by an amount different from the request
- an unrelated ItemId changes
- accessToken / device state changes mid-flight
- inventory cannot be re-read

## 5. Endpoints that must remain mutation-disabled (required output 20)

These stay unwired from `client.py` and never callable from `run.py`
without a human-initiated, single-shot CLI invocation in front of them.
This list reflects the swarm safety brief plus the hazard classification
in `mutation-safety-analysis.md`:

H3 — never autonomous, manual CLI only with explicit confirmation flag:
- `ItemService/DeleteItem3`
- `ItemService/ConsumeItem2`
- `ItemService/DeleteAmmo` (until budget caps land)
- `CharacterService/DeleteCharacter`
- `CharacterService/ConsumeCharacter`
- `CharacterService/PrestigeCharacters`

H2 — disabled until capture-verified AND guarded (affordability +
budget cap + idempotency plan):
- `ItemService/CombineItem4`
- `ItemService/GiftItem`
- `ItemService/ActivateItem4`
- `ItemService/ActivateRenameItem`
- `ItemService/RepairBrokenModules`
- `MarketService/SellItem3`
- `MarketService/Purchase4`
- `RoomService/AddItemIdToRoom`
- `RoomService/MoveItemIdToRoom3`
- `RoomService/RemoveItemIdFromRoom`
- `RoomService/UpdateItemIdsForRoom2`
- `RoomService/Refill2` (Starbux)
- `CharacterService/WearItem`, `CharacterService/UnequipItems`,
  `CharacterService/RemoveItem` (until reconciliation is in place)

Already-autonomous H2 endpoints that today lack an explicit budget cap
and need one before any further work:
- `GalaxyService/SpeedUpTravelling` (sdk/client.py:2615)
- `CharacterService/Draw` via `purchaseDrawWithStarbux`
  (sdk/client.py:1913)
- `UserService/AddStarbux2` (ad-reward path; server rate-limited)
- `RoomService/RebuildAmmo3` (cli-only at present, no cap)

`RewardService/BuyReward2` (via `purchaseScorchedPodIfAffordable`,
sdk/client.py:2024) is the template for an acceptable affordability
guard: read balance, only call when `balance >= price`, single-shot,
no retries on timeout.

## 6. Rules that bind all of the above

1. A captured checksum does not make a mutation safe. Capture only
   proves the server will accept the call.
2. No retries on H2/H3. A timeout may already have executed; reconcile
   by re-reading state, not by refiring.
3. The autonomous loop is allow-listed, not deny-listed. Anything not
   on the allow-list is a manual action.
4. Budget caps live next to the call site, in addition to
   affordability checks. Affordability alone cannot stop a runaway
   loop.
5. H3 endpoints stay out of `client.py` until a human wires them
   deliberately for a one-shot operation.
6. No silent state repair. A guard refusal is logged and surfaced, not
   routed around.
7. Unknown items default to protected. The classifier must not
   auto-demote an unknown design to surplus.

## 7. Lane scope statement

- Lane: safety / policy synthesis.
- Code changed: none.
- Files added: `docs/security/protected-item-policy.md` (this file).
- Files intentionally not touched: `sdk/client.py`, `run.py`,
  `sdk/security.py`. The remediation items in section 5 are
  recommendations for the next implementation slice, not work this lane
  performed.
