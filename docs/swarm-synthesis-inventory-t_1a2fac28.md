# Swarm synthesis: t_1a2fac28 — Inventory planner, multi-account transfers, marketplace, and implementation architecture

Swarm root: `t_bd458ad8`. Sibling lanes synthesized:

- `t_730c8a08` — repository & data model (docs/worker-report-inventory-t_730c8a08.md)
- `t_74779a1f` — endpoint/capture/protocol verification (docs/worker-report-inventory-t_74779a1f.md)
- `t_a20d45b8` — protected-item policy & mutation boundaries (docs/security/protected-item-policy.md)
- `t_fd97b1ee` — classification, demand, multi-account, reduction policy (docs/worker-report-inventory-t_fd97b1ee.md)

Investigation-only. No live mutations were performed by any lane. No production code changed by this synthesis.

Evidence tiers used throughout: VERIFIED (capture-recomputed), CAPTURED-NOT-VERIFIED, CAPTURED-BUT-AMBIGUOUS, STATICALLY-DISCOVERED (IL2CPP only), INFERRED, UNKNOWN.

---

## 1. Current inventory architecture (CODE evidence)

All inventory behavior lives in `sdk/client.py` (~3,760 LOC) as methods on `Client`. There is no inventory module. Relevant supporting modules:

- `sdk/security.py` — checksum primitives (auth, battle, marker, RebuildAmmo3, CollectResources3, BuyReward2, AddStarbux2, GoTo, SpeedUpTravelling, CharacterDraw). No ConsumeItem2/ActivateItem4/CombineItem4 helpers yet.
- `sdk/crew_leveling.py` — training math only; does not touch items.
- `sdk/ship_layout.py` — room-grid parsing from image bytes; no module/item attachment model.
- `sdk/commands.py`, `sdk/tui.py`, `run.py` — no item-aware command surface today.

Inventory read path: `Client.itemsOfAShip` (client.py:690-697) is a raw `xmltodict` parse of `ItemService/ListItemsOfAShip`; normalization happens ad-hoc at point of use via `_extract_collection(..., "Item")`. Designs come from `listAllDesigns4` (client.py:756-900), which pulls `ItemDesigns` from the static branch — **contested by capture evidence; see §2 correction.**

Conclusion: inventory logic should be extracted into a new `sdk/inventory/` package (see §16) instead of continuing to grow `client.py`. `_extract_collection` is the de-facto normalization primitive and should move with it.

## 2. Current inventory data model (CODE + CAPTURED)

What the code consumes today:

- Item attributes referenced anywhere: `@ItemId`, `@ItemDesignId` only.
- ItemDesign attributes referenced: `@ItemDesignId`, `@ItemName`, `@ItemSubType` (in `findConsumableDesignId`, client.py:1677-1704).
- No code consumes rarity, enhancement, equipped state, module linkage, stack limits, market price, tags, or recipe ingredients.

What the server actually emits (CAPTURED from `~/pss-mitm-capture.jsonl`):

- Item nodes carry `ItemId, ShipId, ItemDesignId, Quantity, IsNew, SkinKey`. No rarity/enhancement/equipment attributes on Item nodes in ConsumeItem2/CombineItem4 responses.
- Equipment linkage lives on `Character/@ItemIds` + `Character/Items` child elements, not on Item nodes.
- `ActivateItem4` response contains a `<Situation .../>` row — activation produces a situation buff, not an inventory-item transform.
- `MarkItemsNotNew` returns an empty element — pure side-effect call.

**Data-model correction (VERIFIED against captures):** sibling t_fd97b1ee established that static and dynamic design feeds split the data:

| Feed (captured 2026-08-08) | ItemDesign rows | TrainingDesign | CraftDesign |
|---|---|---|---|
| `DesignService/ListAllStaticDesigns2` | 0 | 142 | 143 |
| `DesignService/ListAllDynamicDesigns` | 2,557 | 0 | 0 |

Current `listAllDesigns4` reads `ItemDesigns` from the static branch, so the item catalog it builds is at best partially grounded. Any inventory catalog layer must consume the dynamic feed for ItemDesigns (and static for TrainingDesigns/CraftDesigns). Static-tag `ListItemDesigns2`/`ListItemsOfAShip` themselves remain UNCAPTURED.

## 3. Endpoint verification matrix (final swarm state)

VERIFIED = checksum recomputed from capture bytes (Ck="5343", SC="Savvy!s0d@", new-style MD5 unless noted).

| Endpoint | Status | Notes |
|---|---|---|
| `ItemService/ListItemsOfAShip` | STATICALLY-DISCOVERED | Never captured. Blocks full item-field enumeration. |
| `ItemService/ListItemDesigns2` | STATICALLY-DISCOVERED | Superseded in practice by the static/dynamic design feeds (10 captures of `ListAllStaticDesigns2`). |
| `ItemService/ConsumeItem2` | **VERIFIED** | 3/3 unique captures. Preimage `clientDateTime+itemId+quantity+characterId`. **Corrects INVENTORY_ENDPOINT_ANALYSIS.md**, which listed it as checksum-free. Response retains Item node with Quantity=0. |
| `ItemService/ActivateItem4` | VERIFIED (single-sample) | 1/1. Preimage `clientDateTime` only; itemId/quantity/targetId are on the wire but excluded. Needs a 2nd capture before production. |
| `ItemService/ActivateItem3` | STATICALLY-DISCOVERED / legacy | Zero captures; official client now uses v4. Do not extend. |
| `ItemService/MarkItemsNotNew` | **VERIFIED** | 1/1. Preimage `clientDateTime+accessToken`. Resolves the truncated template. |
| `ItemService/CombineItem4` | **CAPTURED-BUT-AMBIGUOUS** | Sole capture has `itemIds=` empty. Checksum matches `clientDateTime`-only for that request but cannot exclude param inclusion. Needs a real multi-item combine capture. |
| `ItemService/DeleteItem3` | STATICALLY-DISCOVERED | No checksum in template; `remainingQuantity` semantics UNKNOWN (possibly server-side reconciliation hint). |
| `ItemService/GiftItem` | STATICALLY-DISCOVERED | Zero captures. |
| `ItemService/RepairBrokenModules` | STATICALLY-DISCOVERED (checksum per template) | No captures. |
| `ItemService/DeleteAmmo` | STATICALLY-DISCOVERED | No captures. |
| `ItemService/FixUnsynchronizedItems` | STATICALLY-DISCOVERED | No captures. |
| `ItemService/ActivateRenameItem` | STATICALLY-DISCOVERED | No captures. |
| `MarketService/SellItem3` | STATICALLY-DISCOVERED | Zero captures; the 13 "SellItem" grep hits are `ObjectiveType="SellItemCount"` task strings, not API calls. |
| `MarketService/Purchase4` | STATICALLY-DISCOVERED (checksum per template, `saleId+clientDateTime`) | No captures. |
| `MarketService/ListSalesByItemDesignId` | STATICALLY-DISCOVERED (read) | No captures. Required for any verify-then-buy flow. |
| `RoomService/AddItemIdToRoom` / `MoveItemIdToRoom3` / `RemoveItemIdFromRoom` / `UpdateItemIdsForRoom2` | STATICALLY-DISCOVERED (checksum per template) | Zero captures of all four. |
| `RoomService/ListCraftDesigns` | STATICALLY-DISCOVERED | Zero captures. **Obsoleted as a recipe source** — recipes live on `ItemDesign/@Ingredients` (see §6). |
| `RoomService/Refill2` | STATICALLY-DISCOVERED (checksum, Starbux spend) | No captures. |
| `RoomService/CollectResources3` | VERIFIED | 7/7 unique captures; implemented in `sdk/security.py`. |
| `RoomService/RebuildAmmo3` | VERIFIED | Implemented in `sdk/security.py`. |
| `CharacterService/WearItem` / `UnequipItems` | STATICALLY-DISCOVERED | Zero captures; WearItem template truncated. |
| `RewardService/BuyReward2`, `UserService/AddStarbux2`, `GalaxyService/GoTo`, `GalaxyService/SpeedUpTravelling`, `CharacterService/Draw`, `ShopService/PurchaseCatalog2`, `UserService/HeartBeat4`, `RoomService/CollectAllResources` | VERIFIED (pre-existing) | Unchanged by this swarm. |

**Corrections that must be applied to `INVENTORY_ENDPOINT_ANALYSIS.md` by the implementation slice:**

1. `ConsumeItem2` HAS a checksum: `MD5(clientDateTime+itemId+quantity+characterId+CK+SC)`, wire URL `?itemId=&quantity=&characterId=&clientDateTime=&checksum=&accessToken=`. The "no-checksum" row is wrong.
2. `MarkItemsNotNew` template resolves to `?checksum=&clientDateTime=&accessToken=`, preimage `clientDateTime+accessToken`. No longer UNKNOWN.
3. `CombineItem4` has one capture (2026-08-01) — the "No captures" row is stale; checksum still ambiguous.
4. Item-node response shape (6 attributes) and `Character/@ItemIds` equipment linkage are capture-observed.
5. The checksum-endpoint count in the static catalog must be recomputed after applying (1).

## 4. Consumable classification model (CAPTURED-DESIGNS)

2,557 ItemDesigns bucketized by ItemType × ItemSubType × Rarity × ModuleType × `@Ingredients`:

| Bucket | Count | Default disposition |
|---|---|---|
| Crew equipment (6 `Equipment*` subtypes) | 1,241 | KEEP — equip or reserve |
| Cosmetic (Skin/ShipSkin/Sticker) | 378 | KEEP |
| Lootbox / InstantPrize | 227 | USE (open) then reclassify |
| Craft-vehicle designs (Interceptor, Marauder, …) | 154 | KEEP — PVE/PVP craft bodies |
| Ammunition (Missile) | 143 | KEEP — `RebuildAmmo3` loop handles stock |
| Ship modules (`Module`) | 131 | KEEP — allocation endpoints unverified |
| Crew training (InstantTraining 90, InstantXP 8, ReduceFatigue 1, ResetTraining 2, ReducePrestige 10, RerollBonusStats 1, SpecialCRISPR 1) | 113 | RESERVED-WHEN-PRESENT (USE per plan) |
| Crafting ingredients (`Artifact`) | 82 | KEEP — recipe inputs |
| Android bodies | 37 | KEEP |
| Ship blueprints | 16 | KEEP — NEVER discard (progression) |
| Resource packs (GasPack/MineralPack/Fill*Storage) | 12 | USE when storage allows |
| Strategic reserve (Immunity) | 4 | KEEP |
| True `ItemSubType=Consumable` | 3 | KEEP — unknown → protected |
| Speedups (Construction/Research) | 3 | KEEP — situational |
| Misc rare single-instance subtypes | 7 | KEEP |
| Currency proxies (Gas/Mineral items) | 2 | KEEP |

220 designs carry trader/event tags (`robotrader`, `harvestor01..04`, `stardust`, `vip`, `uber`, `heroic`, `unique`, …) — protected regardless of bucket, since they may not be re-obtainable.

Unknown items default to PROTECTED. No item is disposable because of quantity alone.

## 5. Training-material reservations

142 TrainingDesigns (captured). 90 InstantTraining ItemDesigns each point at exactly one TrainingDesignId (63 distinct design IDs reachable). The current `useConsumable` flow (client.py:1297-1350) binds inventory to the training decision at call time with no holdback.

Reservation model for the planner:

    trainingReservation[design_id] = Σ over characters c: planned uses of design_id for c

Until training plans are representable in code, ALL InstantTraining items are RESERVED-WHEN-PRESENT regardless of quantity (deliberate conservatism). `ReduceFatigue`, `ReducePrestige`, `ResetTraining`, `RerollBonusStats`, `SpecialCRISPR` are RESERVED — they reset costly-to-rebuild state.

## 6. Crafting dependency model (CAPTURED-DESIGNS)

- 902 / 2,557 ItemDesigns carry `@Ingredients`; format `81x5` or `78x3|104x6`.
- Inputs are overwhelmingly `Artifact` (elemental ores) or lower-rarity Equipment of the same slot chain.
- The same Artifact appears in many recipes — reservations must sum across the transitive recipe DAG.
- **Correction:** the dynamic-design feed, not `ListCraftDesigns`, is the recipe source. `ListCraftDesigns` may be treated as unneeded for the demand graph unless a future capture proves otherwise.

    artifactDemand[i] += k * q_i   per recipe (design d, k copies, ingredients q_i)
    equipmentDemand[d] += k        only if recipe queued as a goal

## 7. Crew-equipment reservations

Equipment linkage is `Character/@ItemIds` (CAPTURED). Reservation:

    equipmentReservation[design] = Σ equipped characters: 1
                                 + Σ planned-slot assignments: 1

Today there is no "desired equipment" plan in the repo; conservative default = currently-worn + best-rarity-per-slot per owned character.

## 8. Ship-module reservations

131 module designs (Trap, Sandbag, WaterSprinkler, Turret, Recall, legacy None). The four RoomService module-item endpoints are STATICALLY-DISCOVERED with zero captures, and `ship_layout.py` does not model attachment. Therefore: all modules RESERVED-WHEN-PRESENT; no surplus is ever computed for modules until room-module binds are captured.

## 9. Multi-account surplus/deficit model (design only)

The repo manages exactly one account per working directory (`.device` + `auth.json` at repo root). Portfolio design contract:

    AccountPortfolio
      inventory_vector : design -> qty           (read)
      demand_vector    : design -> reserved_qty  (from InventoryDemandPlanner)
      surplus[d] = max(0, inventory - demand)
      deficit[d] = max(0, demand - inventory)

Per-design per-account: quantity by account, required by account, surplus by account, deficit by account, global available surplus.

## 10. GiftItem feasibility

STATICALLY-DISCOVERED; zero captures. Suitability for owned-account transfer is UNKNOWN. It is the natural direct path if its semantics (recipient targeting, item eligibility, level/rarity restrictions, mail/claim flow) can be captured. Until captured it must remain disabled and all transfer planning must emit proposals only.

## 11. Marketplace-transfer feasibility

STATICALLY-DISCOVERED end to end. No sale/purchase traffic exists in 16,703 captured exchanges, so buyer targeting is UNKNOWN — design must assume listings are PUBLIC. Correct guarded flow, when eventually implemented:

    source: SellItem3 (itemId, qty, price)
    source: ListSalesByItemDesignId → identify exact saleId (seller+item+qty+price)
    destination: confirm exact listing tuple, then Purchase4(saleId)
    both: re-fetch inventories; verify exact deltas

Never buy "the first listing matching ItemDesignId". This whole path stays disabled until all three endpoints are capture-verified.

## 12. Safe reduction priority (deterministic order)

    1 KEEP / RESERVE                    everything not proven surplus
    2 USE for intended crew benefit     InstantTraining/InstantXP/ReduceFatigue (wired via useConsumable)
    3 CRAFT / COMBINE into useful items CombineItem4 — NOT wired (checksum ambiguous)
    4 ALLOCATE as ship module           NOT wired (room-module endpoints uncaptured)
    5 TRANSFER to owned account         NOT wired (GiftItem uncaptured)
    6 SELL excess externally            NOT wired (market flow uncaptured)
    7 CONSUME                           ConsumeItem2 VERIFIED; allow-list gated only
    8 DELETE                            DeleteItem3 last resort; semantics UNKNOWN

Only positive `available_surplus` (quantity − all reservations) may enter steps 5–8.

## 13. Protected-item rules

No autonomous code may delete, consume, sell, gift, combine, or otherwise irreversibly transform:

1. Equipped items (Character/@ItemIds).
2. Items reserved by an equipment plan.
3. Inputs to any elected crafting recipe.
4. Modules attached to rooms (or plannable modules, until capture says otherwise).
5. Rare/Epic/Hero/Unique/Legendary rarities.
6. Event-limited / trader-tagged designs (the 220-tag set).
7. Quest/progression items (ship blueprints and any non-discardable flag).
8. Premium items (Starbux purchase path).
9. Marketplace-valuable items (median sale price above configured threshold, once `ListSalesByItemDesignId` exists).
10. Unknown designs — default protected.

## 14. Recommended retention thresholds

- Strategic reserve per crafting ingredient: ≥ 1 full recipe of each elected recipe using it.
- Strategic reserve per training consumable tier: ≥ 5 (once plans exist; until then: hold all).
- Strategic reserve per ammo: ≥ 1 room reload.
- Immunity items: keep all.
- Per-run mutation caps (defence in depth): deletions 0, gifts/sales 0, consumptions bounded by training plan only, Starbux spend bounded by env var (suggested `PSS_MAX_STARBUX_PER_RUN`, default 0).

## 15. Required official-client captures (ranked)

1. `ListItemsOfAShip` — full item field enumeration (blocks the data model).
2. `CombineItem4` with non-empty `itemIds` — resolves checksum ambiguity.
3. `DeleteItem3` — `remainingQuantity` semantics.
4. `SellItem3` + `ListSalesByItemDesignId` + `Purchase4` sequence — marketplace transfer.
5. `GiftItem` — owned-account transfer.
6. Second independent `ActivateItem4` sample.
7. `WearItem` / `UnequipItems` — equipment mutation side.
8. Any one of `AddItemIdToRoom` / `MoveItemIdToRoom3` / `RemoveItemIdFromRoom` / `UpdateItemIdsForRoom2`.
9. `RepairBrokenModules`, `Refill2`, `DeleteAmmo` (lower priority).
10. `ListCraftDesigns` — only to confirm redundancy vs `@Ingredients`.

## 16. Dry-run planner design (Workstream F architecture)

New package; `client.py` remains the transport layer and gains no behavior:

    sdk/inventory/__init__.py
    sdk/inventory/catalog.py      InventoryCatalog — fetch/normalize ListItemsOfAShip + ListAllDynamicDesigns
    sdk/inventory/designs.py      static/dynamic design feed caching (must use dynamic feed for ItemDesigns)
    sdk/inventory/demand.py       InventoryDemandPlanner — training/equipment/craft/module reservations
    sdk/inventory/classify.py     InventoryClassifier — bucket rules of §4 + rarity/tag guards
    sdk/inventory/portfolio.py    AccountPortfolio — multi-account surplus/deficit
    sdk/inventory/reduction.py    InventoryReductionPlanner — deterministic dry-run action list
    sdk/inventory/transfer.py     TransferPlanner — GiftItem vs Sell/Purchase proposals
    sdk/inventory/executor.py     mutation dispatcher — hard-disabled until per-endpoint verification + budget caps
    sdk/inventory/reconcile.py    snapshot→mutate→refetch→verify wrapper

CLI/TUI surface: `inventory`, `inventory plan`, `inventory accounts`, `inventory deficits`, `inventory transfers`, `inventory apply` (`apply` disabled by default until the carried endpoints are capture-verified).

## 17. Mutation safety architecture

Reconciliation contract on every mutation:

    snapshot (item_id, quantity, location)
    apply exactly one mutation
    re-fetch the same inventory slice
    verify the intended delta and NO other delta
    write audit row (endpoint, preimage hash, before, after)
    abort run on any mismatch

Abort conditions: ambiguous response, target vanished, unexpected delta amount, unrelated ItemId changed, auth/device state changed mid-flight, inventory unreadable.

Binding rules (from safety lane, adopted by this synthesis):

1. A captured checksum does not make a mutation safe — it only proves the server accepts the call.
2. No retries on H2/H3 endpoints; reconcile by re-reading, never refire.
3. The autonomous loop is allow-listed, not deny-listed.
4. Budget caps live next to the call site, in addition to affordability checks.
5. H3 endpoints stay out of `client.py` until deliberately wired for one-shot use.
6. No silent state repair; guard refusals are logged and surfaced.
7. Unknown items default to protected; the classifier never auto-demotes.

## 18. Tests required

- Catalog parsing fixtures for both design feeds (static + dynamic) and for `ListItemsOfAShip` (once captured); single-dict and list forms via `_extract_collection`.
- Checksum harness entries for ConsumeItem2 (3 samples), ActivateItem4 (1), MarkItemsNotNew (1) reusing `tests/synthetic_fixtures.py` + `tests/fixtures/checksum_samples.json` patterns.
- Classifier unit tests per §4 bucket, plus unknown→protected and tag→protected cases.
- Reservation math unit tests (training/equipment/craft-DAG transitivity/module hold).
- Reduction planner determinism tests (same inputs → identical ordered dry-run plan).
- Reconciler tests: delta mismatch, unrelated change, re-read failure → abort.
- No autonomous-mutation tests beyond existing static/guard assertions (executor disabled).

## 19. Smallest safe implementation slice

**Slice 1 (read-only, no mutations):**

1. `sdk/inventory/catalog.py` + `designs.py` wired to both design feeds (fixing the static-branch issue for ItemDesigns).
2. `sdk/inventory/classify.py` with the §4 bucket rules and §13 protection.
3. `inventory` and `inventory plan` CLI verbs (dry-run only).
4. Apply the five corrections to `INVENTORY_ENDPOINT_ANALYSIS.md` (§3).
5. Checksum helpers for the three VERIFIED formulas added to `sdk/security.py` (or `sdk/inventory/checksums.py`) with fixture tests.

Nothing in slice 1 permits a mutation. Executor, transfer planner, and `inventory apply` each require their own later slices gated on the capture list in §15.

## 20. Endpoints that must remain mutation-disabled

H3 — never autonomous; manual one-shot CLI with explicit confirmation only, and remain unwired today:

- `ItemService/DeleteItem3`
- `ItemService/DeleteAmmo`
- `CharacterService/DeleteCharacter`, `ConsumeCharacter`, `PrestigeCharacters`

H2 — disabled until capture-verified AND guarded (budget cap + idempotency):

- `ItemService/ConsumeItem2` (checksum VERIFIED; allow-listed for training only, never autonomous bulk)
- `ItemService/CombineItem4` (checksum ambiguous — hard blocked)
- `ItemService/ActivateItem4` (single-sample), `ActivateItem3` (legacy, do not extend), `ActivateRenameItem`
- `ItemService/GiftItem`, `RepairBrokenModules`
- `MarketService/SellItem3`, `Purchase4`
- `RoomService/AddItemIdToRoom`, `MoveItemIdToRoom3`, `RemoveItemIdFromRoom`, `UpdateItemIdsForRoom2`, `Refill2`
- `CharacterService/WearItem`, `UnequipItems`, `RemoveItem`

Pre-existing autonomous endpoints needing budget caps before further work: `GalaxyService/SpeedUpTravelling`, `CharacterService/Draw` (purchaseDrawWithStarbux), `UserService/AddStarbux2`, `RoomService/RebuildAmmo3`. `RewardService/BuyReward2` (via `purchaseScorchedPodIfAffordable`) is the reference pattern for an affordability guard.

---

## Verifier guidance

Conclusions in this synthesis are based on recomputed captures and repo code, not endpoint names. Remarks by tier: VERIFIED items have sample counts in §3; CAPTURED-BUT-AMBIGUOUS applies only to CombineItem4; STATICALLY-DISCOVERED items have zero captures and must not be promoted. Nothing in this synthesis marks destructive inventory automation production-ready.

Machine-readable facts:

```json
{
  "worker": "t_1a2fac28",
  "role": "synthesizer",
  "mutations_attempted": 0,
  "files_changed": ["docs/swarm-synthesis-inventory-t_1a2fac28.md"],
  "verified_checksums": {
    "ItemService/ConsumeItem2": "MD5(clientDateTime+itemId+quantity+characterId+CK+SC) 3/3",
    "ItemService/ActivateItem4": "MD5(clientDateTime+CK+SC) 1/1",
    "ItemService/MarkItemsNotNew": "MD5(clientDateTime+accessToken+CK+SC) 1/1"
  },
  "ambiguous": ["ItemService/CombineItem4"],
  "zero_capture_must_stay_disabled": [
    "DeleteItem3", "GiftItem", "SellItem3", "Purchase4", "ListSalesByItemDesignId",
    "WearItem", "UnequipItems", "AddItemIdToRoom", "MoveItemIdToRoom3",
    "RemoveItemIdFromRoom", "UpdateItemIdsForRoom2", "Refill2",
    "RepairBrokenModules", "DeleteAmmo", "ActivateItem3"
  ],
  "critical_gap": "ListItemsOfAShip never captured",
  "design_feed_correction": "ItemDesigns come from ListAllDynamicDesigns (2557 rows), not the static branch",
  "item_design_counts": {"total": 2557, "equipment": 1241, "ingredient_bearing": 902, "module": 131, "artifact": 82, "true_consumable": 3, "tagged_protected": 220, "training_designs": 142, "craft_designs": 143},
  "smallest_safe_slice": "sdk/inventory catalog+designs+classifier + inventory/plan CLI (dry-run) + INVENTORY_ENDPOINT_ANALYSIS.md corrections + 3 checksum helpers",
  "doc_corrections_required": 5
}
```
