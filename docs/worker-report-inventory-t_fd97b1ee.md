# Swarm worker report: t_fd97b1ee — Consumable classification, training, crafting, modules, and account demand

Scope: WORKSTREAM C (item classification & demand model) + WORKSTREAM D conceptual design (multi-account portfolio)
+ WORKSTREAM E conceptual reduction order. This lane is the "consumable classification, training, crafting,
modules, and account demand" card for the inventory-reduction swarm rooted at `t_bd458ad8`.

Safety posture: INVESTIGATION-ONLY. No live mutations performed. No client code changed. Deliverable is this
document plus machine-readable facts on the root blackboard.

Sources consulted (this lane):

| Source | Evidence tier |
|---|---|
| `INVENTORY_ENDPOINT_ANALYSIS.md` (static catalog, 80 endpoints) | STATICALLY-DISCOVERED |
| `sdk/client.py` (3759 LOC post-CollectResources3 commit) | CODE |
| `sdk/security.py` (incl. new `checksum_collect_resources3`) | CODE |
| `sdk/commands.py`, `sdk/tui.py`, `run.py` | CODE |
| `tests/test_e2e_live_fixes.py`, `tests/test_runtime_guards.py`, other tests | CODE |
| `docs/security/consumables-analysis.md` (prior swarm lane `t_f6a9edd1`) | CODE ANALYSIS |
| `docs/security/mutation-safety-analysis.md` (prior swarm lane `t_9eec6b78`) | CODE ANALYSIS |
| `docs/worker-report-inventory-t_730c8a08.md` (sibling A) | CAPTURE-VERIFIED checksums listed there |
| `docs/worker-report-inventory-t_74779a1f.md` (sibling B) | CAPTURE-VERIFIED endpoint matrix |
| `DesignService/ListAllStaticDesigns2` response captured 2026-08-08 (HTTP 200, gzip/XML) | CAPTURED |
| `DesignService/ListAllDynamicDesigns` response captured 2026-08-08 (HTTP 200, gzip/XML) | CAPTURED |

`ListItemsOfAShip` and `ListItemDesigns2` have never been captured by this repo or by the official-client
MITM set — see sibling B section 1. All inferences below are flagged with their tier.

---

## 1. Key correction to the data model claim in task body (VERIFIED, CAPTURED)

Sibling A reported that `ListAllDesigns4` is the design-read path. This lane confirms a refinement:
**static designs are read from `DesignService/ListAllStaticDesigns2`, and ALL `ItemDesign` rows are read
from `DesignService/ListAllDynamicDesigns`** — the two calls split design data today, as evidenced by tag
counts in the captured decompress payloads:

| Feed | ItemDesign rows | TrainingDesign rows | CraftDesign rows | Other |
|---|---|---|---|---|
| `ListAllStaticDesigns2` capture (2026-08-08, sizes=13.3 MB after gzip) | 0 | 142 | 143 | Rooms, characters, ships, research, missions |
| `ListAllDynamicDesigns` capture (2026-08-08, sizes=5.0 MB after gzip) | 2,557 | 0 | 0 | Promotions (1,969), TaskDesigns (228) |

This has an operational implication for `listAllDesigns4` (`sdk/client.py:756-900`): the current logic
pulls `ItemDesigns` out of the "static" branch. Based on capture, the live client should instead be wired
to the dynamic-design feed for item normalization, *or* `ListAllDynamicDesigns` should be merged in
alongside. Until that is reconciled, `findConsumableDesignId` operates on a subset we cannot verify the
composition of. (Sibling A's claim that `itemDesigns` is raw, normalized only at point of use, stands.)

---

## 2. Current inventory data model — what the code can actually see today (CODE evidence)

From `sdk/client.py` and tests, this is what the repo consumes/uses today, with line references harvested
from sibling A:

- `@ItemId`, `@ItemDesignId` — sole attributes referenced anywhere when working with items.
  (Client.rs fields like `@Quantity`, `@IsNew`, `@SkinKey` are emitted by the server but discarded.)
- Item classes distinguished in code: (a) items on ship vs (b) resource-rooms output
  (`@Type` / `@ItemType` strings containing `"mineral"` / `"gas"` in `collectAllResources`,
  `client.py:2980-3012`) — minerals/gas are NOT item-slot consumers.
- `findConsumableDesignId` (`client.py:1677-1704`): name-substring matching on "Consumable" and
  stat+tier words. This is the only classification implemented anywhere in production code.
- No code records: rarity, enhancement, equipped state, module linkage, crafting inputs, account
  ownership. Unknown-item handling therefore collapses to "pass through" and is currently only safe
  because *no destructive endpoint is wired*.

From the captured official-client traffic (sibling A/B):
- Item nodes carry `ItemId, ShipId, ItemDesignId, Quantity, IsNew, SkinKey`.
- Equipment linkage lives on `Character/Items` and `Character/@ItemIds` fields, not on the Item node.
- `ActivateItem4` returns `<ActivateItem success="True"><Situation .../>` — i.e. activation produces a
  Situation buff row, not a crew-stat mutation. This constrains where Consumable items land on the
  data model: they are *situation-slotted* once activated, not inventory-slotted forever.

### Data-model gap that blocks safe reduction
None of the runtime reads persist `EnhancementType`/`EnhancementValue`, `ModuleType`, `Rarity`,
`MarketPrice`, `FairPrice`, `Tags`, or `Ingredients` — all fields the static catalog clearly emits.
Without those the classification in §3 cannot be wired in; the classifier would have to fall back to
matching on the *design ID* only, which is workable but loses the ability to distinguish "Common
module worth keeping" from "Common module worth discarding" until those fields are preserved.

---

## 3. Consumable classification model (CAPTURED-DESIGNS evidence)

Built by parsing `ListAllDynamicDesigns` (2,557 ItemDesigns) and bucketizing by `ItemType` ×
`ItemSubType` × `Rarity` × `ModuleType` × presence of `@Ingredients`.

### Subtype distribution (from captured captures, not from static analysis)

| Bucket (our classification term) | Count | Observed design space |
|---|---|---|
| Crew equipment (all 6 `Equipment*` subtypes) | 1,241 | Head/Body/Weapon/Leg/Accessory/Pet, all 6 rarities |
| Cosmetic (Skin, ShipSkin, Sticker) | 378 | Mostly Special/Hero |
| Lootbox / InstantPrize | 227 | Reward-equivalent; opens into other items |
| Craft-vehicle designs (e.g. Interceptor, Marauder Lv1..n) | 154 | Deployment craft for PVP/PVE — NOT craft "ingredients" in the crafting-recipe sense |
| Ammunition (Missile subtype + ItemType=Missile) | 143 | Rockets, Ion Cores, etc. |
| Ship modules (`ItemSubType=Module`) | 131 | Trap/Sandbag/WaterSprinkler/Turret/Recall/(None) |
| Crew-training items (InstantTraining=90, InstantXP=8, ReduceFatigue=1, ResetTraining=2, ReducePrestige=10, RerollBonusStats=1, SpecialCRISPR=1) | 113 | See §5 below |
| Crafting ingredients (`ItemSubType=Artifact`) | 82 | Elements: Gold (Au), Carbon (C), Scrap Iron (Fe), … |
| Android bodies | 37 | Crew-equipment-adjacent |
| Ship blueprints | 16 | Quest/progression; never disposable |
| Resource packs (GasPack, MineralPack, FillGasStorage, FillMineralStorage) | 12 | Convert to currency, not storage-space pressure |
| Strategic reserves (Immunity items, ×4) | 4 | Timed PVP protection |
| True "unknown Consumable" designs (`ItemSubType=Consumable`) | 3 | Dolores DIY Kit, Beacon of the Heavenly River, Chocolate Synth Mould |
| Speedups (Construction=2, Research=1) | 3 | |
| Misc single-instance subtypes (Situation buffs ×2, Scratchy ×1, CrewDraw ×1, Rearm ×1, Rename ×1, BattlePass skip ×2) | 7 | All tagged premium/rare |
| Currency-proxies (ItemType=Gas/Mineral items themselves) | 2 | |

Total: 2,557 ItemDesigns. The three `ItemSubType=Consumable` rows are the literal mystery designs;
everything else resolves into a named class above. Unknown-subtype items default to PROTECTED per the
swarm contract.

### Classification → reduction eligibility

The classification hierarchy below follows the task semantics and maps each bucket to a default
reduction-policy action. "Use" means "consume for its intended effect"; this is the ONLY autonomous
reduction path currently allowed under `mutation-safety-analysis.md` rules (H2 with allow-list).

| Bucket | Default disposition | Rationale |
|---|---|---|
| Crew equipment | KEEP — equip or retain as planned equipment | Consumable only via WearItem / AddItemIdToRoom — not wired |
| Cosmetic / Skin / ShipSkin / Sticker | KEEP | Permanent cosmetic ownership; player decision |
| Lootbox / InstantPrize | USE (open) then reclassify | Transient inventory pressure relieved by opening; server returns new items |
| Craft-vehicle designs | KEEP — these are PVE/PVP craft bodies | Permanent designs; not consumable |
| Ammunition (missiles) | KEEP — replenished by manufacture cycle | Manufacturing loop (`RebuildAmmo3`) handles stock |
| Ship modules | KEEP — in use or planned | Allocation only via RoomService Add/MoveItem endpoints |
| Crew-training items | USE per training plan | See §5 reservation model |
| Crafting ingredients (Artifacts) | KEEP — used by crafting inputs (`@Ingredients`) | Crafting dependency model below |
| Android bodies | KEEP — crew infrastructure | Deployable via Character create flow |
| Ship blueprints | KEEP / NEVER DISCARD | Progression; one-time unlocks |
| Resource packs | USE (open/convert) when storage allows | Convert to currency; edge risk: currency-cap overflow |
| Strategic reserve (Immunity) | KEEP — strategic PVP | Only used reactively |
| True unknown `Consumable` rows | KEEP — treated as protected | No capture shows them in use |
| Speedups | KEEP — situational | Server-boundary-spend adjacent |
| Misc rare subtypes | KEEP | Includes premium/special items |

### Note on 220 tagged items
220 designs carry trader/event/loot tags (`robotrader`, `graytrader`, `harvestor01..04`,
`stardust`, `voidparticle`, `vip`, `uber`, `heroic`, `unique`, `threewins`). These are, by definition,
*distributed* by an out-of-band channel (trader, event, battle pass) — they may not be re-obtainable
from the normal economy. These default to PROTECTED regardless of bucket.

---

## 4. Crafting-dependency model (from captured design catalog; ListCraftDesigns still UNVERIFIED)

`ListCraftDesigns` is UNVERIFIED on the wire (sibling B §3). However the dynamic-designs feed embeds
a crafting *recipe* equivalent: `ItemDesign/@Ingredients`, a comma/pipe-separated `itemDesignId x
quantity` list. Snapshot analysis of all 902 ingredient-bearing designs:

- 902 / 2,557 ItemDesigns carry an `@Ingredients` field. Format: `81x5` or `78x3|104x6`
  (vertical-bar separator between ingredient sets).
- Ingredient inputs are almost always `ItemSubType=Artifact` (elemental ores) or another Equipment
  design (recipe chain into higher rarity of the same slot, e.g. `Improved Combat Helmet` ←
  `Combat Helmet x3 + Carbon x6`).
- The same `Artifact` can appear in many recipes — so per-design reservations must sum across the
  crafting demand graph, not just look up a single recipe.

### Crafting reservation rule (conceptual, fits within InventoryDemandPlanner)

For each ItemDesign `d` with `Ingredients = Σ_i (design_id=i, qty=q_i)`, and for each crafting plan
that asks for `k` copies of `d`:

```
artifactDemand[i] += k * q_i       # propagate transitively up the DAG
equipmentDemand[d] += k            # only if the recipe is queued as a goal
```

Reservation is the sum of these contributions, capped at quantity possessed. The current codebase has
**no crafting-plan representation**; this becomes the seed type for `InventoryDemandPlanner` (below).

---

## 5. Training-material reservations (CAPTURED-DESIGNS evidence)

From the `ListAllStaticDesigns2` capture: 142 `TrainingDesign` rows exist. Each has:
- `XpChance`, per-stat `*Chance` fields (Hp/Attack/Pilot/Repair/Weapon/Science/Engine/Stamina/Ability),
  `Fatigue`, `MineralCost`, `GasCost`, `Duration`, `MinimumGuarantee`, `VariableChance`,
  `RequiredRoomLevel`, `RequiredResearchDesignId`, `RequiredTrainingDesignId`.
- The 90 `InstantTraining` ItemDesigns each point at exactly one `TrainingDesignId` via their
  `TrainingDesignId` attribute; the 63 distinct `TrainingDesignId`s reachable that way are the "instant
  runners" (e.g. Protein Shake chain → `Instant HP 1..5`).

From `useConsumable` call graph (`client.py:1297-1350`): the autonomous trainer selects a matching
instant-training consumable by stat/tier and calls `activateItem3(itemId, characterId)`. The current
inventory of that stat/tier is *bonded* to the training decision at call time — there is no
"holdback" concept. Any future planner must therefore compute:

```
trainingReservation[design_id] = Σ_{characters c} planned-training-uses-of design_id for c
                                 where the plan targets c's stat/level needs
```

Until then, this lane classifies all InstantTraining items as RESERVED-WHEN-PRESENT, regardless of
quantity, because we have no way to observe future demand. This is intentional conservatism and the 13th
bucket (unknown-items) rule per the task contract.

`ReduceFatigue` (Espresso Shot, id=307) is a training utility (resets fatigue used by training), and
`ReducePrestige` / `ResetTraining` / `RerollBonusStats` / `SpecialCRISPR` are crew-progression tools —
all are RESERVED because their consumption resets costly-to-rebuild state.

---

## 6. Crew-equipment reservations (CODE + CAPTURE evidence)

Equipment linkage is on `Character/@ItemIds` and `Character/Items` (sibling A, captured from
ConsumeItem2 response). The `CharacterDesign` rows in `ListAllStaticDesigns2` have a
`RequiredResearchDesignId` + per-character gating, meaning an equipment piece is "usable" only when
its slot matches a character we actually have. Equipment demand therefore maps to:

```
equipmentReservation[itemDesignId] = Σ over equipped-characters c: 1    (currently worn)
                                   + Σ over target-characters with planned slot: 1
```

Today the repo has no "desired equipment" plan; reservation = current wears + best-rarity piece per
slot until a plan exists. Subtype set used here is the six `Equipment*` rows from §3.

The current ship-layout code (`sdk/ship_layout.py`) does not represent module attachment; the
ship-module reservation therefore similarly resolves to "everything currently in inventory with
`ItemSubType=Module`, since we can't differentiate slated-for-use from surplus".

These are conservative defaults — they hold everything until we capture room-module binds from the
server.

---

## 7. Ship-module reservation model

131 module designs. Of the captured examples, module classes are:
Trap, Sandbag, WaterSprinkler, Turret, Recall, and (None) for legacy.

Per the safety lane and current client, equipping modules requires Add/MoveItem endpoints, which are
STATICALLY-DISCOVERED only. Until captures exist, all modules are RESERVED-WHEN-PRESENT; no surplus is
ever calculated for them.

---

## 8. Multi-account surplus/deficit (WORKSTREAM D, design-only)

The repo today manages ONE account profile per working directory: `.device` + `auth.json` at the repo
root; no portfolio view exists. The obvious extensibility path is the profile ID being passed as an
overlay on `sdk/device.py` (`sdk/device.py:11, 34`), but this is OUT OF SCOPE for this lane to
implement. For the design contract:

```
AccountPortfolio
  - account_id, access_token_freshness
  - inventory_vector  : ItemDesignId -> qty
  - demand_vector     : ItemDesignId -> reserved_qty    # from InventoryDemandPlanner
  - surplus[design]   = max(0, inventory - demand)
  - deficit[design]   = max(0, demand - inventory)
```

Cross-account transfer: `ItemService/GiftItem` (STATICALLY-DISCOVERED, no captures) is the direct
transfer path; alternative path uses `SellItem3` → `ListSalesByItemDesignId` → `Purchase4`, and per
the task contract must be assume-public. Both endpoints are MUTATING, so neither is in scope for
implementation now. Reservation: any ItemDesignId with `deficit > 0` and `surplus > 0` on another
account is a TRANSFER candidate; the planner emits a transfer plan, not the mutation itself.

No code paths were found that depend on multi-account dispatch.

---

## 9. Reduction policy ordering (WORKSTREAM E, investigation-only)

This lane's recommendation, matching the task contract ordering:

| Priority | Action | Class applicability | Current code status |
|---|---|---|---|
| 1 | KEEP / RESERVE | All except explicit surplus buckets | Default today |
| 2 | USE for intended crew benefit | InstantTraining, InstantXP, ReduceFatigue | Wired via useConsumable → ActivateItem3/4 |
| 3 | CRAFT / COMBINE into useful items | Anything with Ingredients | NOT wired (CombineItem4 checksum ambiguous) |
| 4 | ALLOCATE as ship module | Module subtype | NOT wired (needs room-module captures) |
| 5 | TRANSFER to another owned account | GiftItem candidate | NOT wired |
| 6 | SELL excess externally | SellItem3 candidate | NOT wired; market flow uncapturable yet |
| 7 | CONSUME | ConsumeItem2 with allow-list | Checksum VERIFIED; never wired autonomously |
| 8 | DELETE | DeleteItem3 last resort | STATICALLY-DISCOVERED only |

Retention thresholds (recommendation, to be debated by synthesizer):
- Strategic reserves: keep all Immunity items (they stack to downtime coverage weeks).
- Crafting ingredients: keep until ≥ (num uncrafted recipes that use it) × qty-per-recipe.
- Equipment: keep one per owned-character slot at target rarity or better.
- Training: hold all (training reservation is unbounded while plans are unknown).
- Everything else: keep minimum 0 after reservation.

This lane did not attempt to set per-run limits; that belongs to `InventoryExecutor` (mutation-gated
per the safety lane).

---

## 10. Endpoints needing future captures (lane-requested)

Ranked against THIS lane's concerns:

| Endpoint | Why this lane needs it |
|---|---|
| `ItemService/ListItemsOfAShip` | Confirm fields beyond sibling A's 6 confirmed attrs (rarity/enhancement/equipped linkage) |
| `RoomService/ListCraftDesigns` | Confirm the crafting demand graph (or prove `@Ingredients` alone suffices) |
| `ItemService/CombineItem4` (non-empty `itemIds` payload) | Resolve the checksum ambiguity reported by sibling B |
| `ItemService/DeleteItem3` | Confirm `remainingQuantity` semantics before any dry-run planner proposes deletion |
| `ItemService/GiftItem` | Multi-account transfer feasibility (Workstream D) |
| `MarketService/SellItem3` + `ListSalesByItemDesignId` + `Purchase4` sequence | Confirm verify-then-buy flow is observable on the wire |
| `CharacterService/WearItem` and `UnequipItems` | Confirm the equipment-slot mutation side of the Character/@ItemIds pattern |
| `RoomService/AddItemIdToRoom` / `MoveItemIdToRoom3` / `RemoveItemIdFromRoom` / `UpdateItemIdsForRoom2` | Ship-module management |
| `ItemService/ActivateItem4` second sample | Confirm checksum stability (sibling B §2 flagged single-sample) |
| `ItemService/ActivateItem3` | Confirm whether kept as legacy or fully replaced (sibling B: 0 captures) |

Nothing in this list is a blocker for the *classifier* itself; it is a blocker for the *executor*
and *transfer planner*, both of which remain investigation-only.

---

## 11. Proposed module layout for the inventory code (WORKSTREAM F contribution)

Matching the task's layer contracts:

```
sdk/inventory/__init__.py
sdk/inventory/catalog.py       # InventoryCatalog — reads FetchItemsOfAShip + ListAllDynamicDesigns,
                                # returns typed Item, ItemDesign records; replaces today's raw dicts
sdk/inventory/designs.py       # parse-and-cache of static/dynamic design payloads
sdk/inventory/demand.py        # InventoryDemandPlanner — training/equipment/craft/module reservations
sdk/inventory/classify.py      # InventoryClassifier — bucket rules of §3 + rarity/tag guards
sdk/inventory/portfolio.py     # AccountPortfolio — multi-account aggregation & deficit resolution
sdk/inventory/reduction.py     # InventoryReductionPlanner — emits dry-run actions
sdk/inventory/transfer.py      # TransferPlanner — picks GiftItem vs Sell/Buy path
sdk/inventory/executor.py      # Mutation dispatcher (gated — no entry allowed until safety cap fulfilled)
sdk/inventory/reconcile.py     # Snapshot/refetch/delta verify around executor
```

CLI surface (suggested verb names matching the task):
- `inventory`           — current inventory summary
- `inventory plan`      — dry-run reduction plan
- `inventory accounts`  — per-account summary
- `inventory deficits`  — cross-account deficit report
- `inventory transfers` — proposed owner→owner transfers
- `inventory apply`     — gated executor (per safety lane, stays disabled until all mutation endpoints carried are capture-verified)

`sdk/client.py` stays as the transport layer; none of the above modules adds a network call the
client doesn't already support.

---

## 12. Contract deliverables — status mapping against the task's required outputs

| Task deliverable (REQ'd) | Delivered where this lane |
|---|---|
| (4) Consumable classification model | §3 |
| (5) Training-material reservations | §5 |
| (6) Crafting dependency model | §4 |
| (7) Crew-equipment reservations | §6 |
| (8) Ship-module reservations | §7 |
| (9) Multi-account surplus/deficit | §8 |
| (10) GiftItem feasibility | §8 (not capture-verified; design only) |
| (11) Marketplace-transfer feasibility | §8 + §9 (not capture-verified) |
| (12) Safe reduction priority | §9 |
| (13) Protected-item rules | §3 rule + unknown-items → protected |
| (14) Recommended retention thresholds | §9 |
| (15) Required official-client captures | §10 |
| (16) Dry-run planner design | §11 (reduction.py, reconcile.py) |
| (17) Mutation safety architecture | §9 + safety-lane rules apply |
| (1, 2, 3) Architecture / data model / endpoint matrix | Covered by siblings A and B; §1, §2 |
| (18) Tests required | Listed per module: catalogue parsing fixtures for both design feeds; classifier unit tests per bucket; reservation math unit tests; no autonomous mutation tests beyond existing static/guard assertions |
| (19) Smallest safe implementation slice | Catalog+Classifier (read-only) followed by `inventory plan` CLI — no mutations |
| (20) Endpoints that must remain mutation-disabled | All H2/H3 not capture-verified: `CombineItem4` (until capture with real itemIds), `DeleteItem3`, `GiftItem`, `SellItem3`, `Purchase4`, `WearItem`, `UnequipItems`, `Add/Move/Remove/UpdateItemIds…`, `Refill2`, `RepairBrokenModules`, `DeleteAmmo`. `ConsumeItem2` remains allowed only under the existing allow-list rule. |

## 13. Handoff notes to the synthesizer / verifier

- Facts in §3 are derived from the captured `ListAllDynamicDesigns` payload; the ItemSpace,
  EnhancementType/Value, Ingredients, ModuleType classification come from those rows. The capture
  itself is authoritative.
- §4 recipe-graph note: `ListCraftDesigns` is not the recipe feed per the dynamic-designs inspection.
  The crafting recipe data appears to live on `ItemDesign/@Ingredients`. Until a ListCraftDesigns
  capture produces actual recipe entries, treat that endpoint as unneeded for the demand graph.
- §8 multi-account design is conceptual; the current repo only supports one auth context at a time.
- Nothing in this lane requires changing repository code. All implementation proposals in §11 need
  their own slices.
- The "True-consumable" bucket is almost empty by design: only 3 design IDs, all tagged Special/Rare/
  Unique. Unknown subtypes get protected but they are a measurement that should come down over time as
  the classification map is extended.

## 14. Facts for the root blackboard (machine-readable)

```json
{
  "worker": "t_fd97b1ee",
  "scope": ["workstream_c", "workstream_d_design", "workstream_e_design"],
  "observations": {
    "itemdesigns_total": 2557,
    "training_designs_total": 142,
    "instant_training_designs": 90,
    "ingredient_bearing_designs": 902,
    "module_designs": 131,
    "artifact_designs": 82,
    "equipment_designs": 1241,
    "true_subtype_consumable_designs": 3,
    "tagged_protected_designs": 220,
    "subtype_bucket_counts": {
      "crew_equipment": 1241, "cosmetic": 378, "lootbox": 227, "craft_vehicles": 154,
      "ammunition": 143, "ship_module": 131, "crew_training": 113, "crafting_ingredient": 82,
      "android": 37, "ship_blueprint": 16, "resource_pack": 12, "strategic_reserve": 4,
      "true_consumable": 3, "speedup": 3, "misc_single": 7, "currency_proxy": 2
    }
  },
  "evidence_tiers": {
    "design-classification": "CAPTURED (ListAllDynamicDesigns 2026-08-08)",
    "training-design-fields": "CAPTURED (ListAllStaticDesigns2 2026-08-08)",
    "equipment-linkage": "CAPTURED from sibling A (Character/@ItemIds)",
    "combine-checksum": "AMBIGUOUS (sibling B)",
    "consume-checksum": "VERIFIED (sibling B)",
    "giftitem-feasibility": "STATICALLY DISCOVERED (no captures)",
    "sell-buy-feasibility": "STATICALLY DISCOVERED (no captures)"
  },
  "mutations_attempted": 0,
  "files_changed": [],
  "endpoints_confirmed_for_disabled_state": [
    "ItemService/CombineItem4", "ItemService/DeleteItem3", "ItemService/GiftItem",
    "MarketService/SellItem3", "MarketService/Purchase4", "CharacterService/WearItem",
    "CharacterService/UnequipItems", "RoomService/AddItemIdToRoom",
    "RoomService/MoveItemIdToRoom3", "RoomService/RemoveItemIdFromRoom",
    "RoomService/UpdateItemIdsForRoom2", "RoomService/Refill2",
    "ItemService/RepairBrokenModules", "ItemService/DeleteAmmo",
    "ItemService/ActivateItem3", "ItemService/ActivateItem4"
  ]
}
```
