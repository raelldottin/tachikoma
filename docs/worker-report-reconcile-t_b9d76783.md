# Reconciliation: inventory research vs. current repository truth

Swarm root: `t_b9080d7a`. Worker: `t_b9d76783` (Reconcile completed inventory research
with current repository truth). Scope: this is a PLANNING-lane deliverable. No
production code was changed. No live mutations attempted.

Sources reconciled:

- `docs/swarm-synthesis-inventory-t_1a2fac28.md` (synthesizer)
- `docs/worker-report-inventory-t_730c8a08.md` (repo & data-model lane)
- `docs/worker-report-inventory-t_74779a1f.md` (endpoint/capture/protocol lane)
- `docs/worker-report-inventory-t_fd97b1ee.md` (classification / demand / multi-account lane)
- `docs/security/protected-item-policy.md` (safety lane `t_a20d45b8`)
- `INVENTORY_ENDPOINT_ANALYSIS.md` (stale static catalog, needs corrections)
- `sdk/client.py` (3759 LOC, current repo truth at HEAD 2cb5aa4)
- `sdk/security.py`, `run.py`, `sdk/commands.py`, `sdk/tui.py`, tests/

## 1. Corrections confirmed against the repository (reconciled)

The following synthesis claims were cross-checked against HEAD and hold:

1. **ConsumeItem2 checksum is VERIFIED** (`MD5(clientDateTime+itemId+quantity+characterId+CK+SC)`, 3/3 samples).
   `INVENTORY_ENDPOINT_ANALYSIS.md` line ~110 still lists it as a no-checksum `?itemId=` endpoint — **wrong**, must be corrected before any implementation.

2. **MarkItemsNotNew checksum is VERIFIED** (`clientDateTime+accessToken`, 1/1). The analysis doc's "truncated template" row is stale.

3. **CombineItem4 is CAPTURED-BUT-AMBIGUOUS**, not "no captures". One capture exists (2026-08-01) with empty `itemIds`; checksum matches `clientDateTime`-only for that request but a non-empty-itemIds capture is required before implementation.

4. **ItemDesigns come from `DesignService/ListAllDynamicDesigns`**, not the static branch. Current `client.listAllDesigns4` (client.py:746-898) pulls `ItemDesigns` from the deprecated `ListAllDesigns4`. Capture counts: static feed = 0 ItemDesign rows; dynamic feed = 2,557. The synthesis's data-model correction is correct and any `InventoryCatalog` must consume the dynamic feed (plus `ListAllStaticDesigns2` for TrainingDesigns/CraftDesigns).

5. **`ListItemsOfAShip` has never been captured.** It is implemented in code (`client.py:690-697`) and used by automation, but its full Item-node field list is UNKNOWN beyond the 6 attributes observed in adjacent responses. It remains the single highest-priority capture.

6. **Item nodes carry only 6 attributes** (`ItemId, ShipId, ItemDesignId, Quantity, IsNew, SkinKey`) in observed responses; equipment linkage lives on `Character/@ItemIds` + `Character/Items`, not on Item nodes. Any data model must not invent per-item rarity/enhancement attributes until `ListItemsOfAShip` capture proves them.

7. **Equipment linkage representation**：equipped items are identified via `Character/@ItemIds`; `sdk/client.py` does not currently consume this at all (no reference to `@ItemIds` on characters outside implicit training flows). The classifier plan correctly assigns equipment reservations via `Character/@ItemIds`.

8. **Current classification in code is name-substring matching only** (`findConsumableDesignId`, client.py:1677-1704). Rarity, ModuleType, Ingredients, Tags, MarketPrice, etc. are emitted by the server but discarded. The synthesis's bucket model (equipment 1,241 / cosmetic 378 / lootbox 227 / craft vehicles 154 / ammo 143 / modules 131 / training 113 / artifacts 82 / androids 37 / blueprints 16 / resource packs 12 / immunity 4 / true-consumable 3 / speedups 3 / misc 7 / currency proxies 2) stands on CAPTURED design data and is a valid foundation for a real classifier.

9. **`sdk/security.py` has no helpers yet for ConsumeItem2 / ActivateItem4 / MarkItemsNotNew / CombineItem4.** They must be added following the existing pattern (`checksum_collect_resources3` at sdk/security.py:579 is the style reference).

10. **All four room-module endpoints (Add/Move/Remove/UpdateItemIds…) are STATICALLY-DISCOVERED with zero captures.** `sdk/ship_layout.py` does not model item attachment. The plan's default of "all modules RESERVED-WHEN-PRESENT" is the only safe stance.

11. **Marketplace transfer chain (SellItem3 → ListSalesByItemDesignId → Purchase4) is STATIC only.** Zero marketplace traffic in 16,703 captured exchanges. TransferPlanner must emit proposals only; `inventory apply` stays disabled for transfers.

12. **Repo supports exactly one account alive at a time** (`.device` + `auth.json` at repo root). Multi-account portfolio work is greenfield; `AccountPortfolio` is a pure aggregation object over N previously-fetched single-account snapshots.

## 2. Disagreements / risks found during reconciliation (evidence-level)

- The synthesis recommends `_extract_collection` (client.py:50) as the normalization primitive that "should move with" the new package. That helper is used broadly across `client.py`; moving it would churn dozens of call sites. **Recommendation: keep it in place; the new `sdk/inventory/` package imports it from `sdk.client`.** Acceptable dependency direction — `client.py` remains the transport layer and gains nothing.

- `protected-item-policy.md` classifies `ConsumeItem2` as H3 (manual-only). The synthesis (§20) and demand lane both allow it as H2 under an allow-list. Reconciled position: keep it **unwired from autonomous flows today** (request lane 8/9 never enables bulk use); the verification lane's VERIFIED checksum means the *plumbing* may be implemented behind a hard-disabled executor. This is compatible with both documents.

- Existing autonomous calls with no budget caps accumulate risk independently of inventory (`SpeedUpTravelling`, `purchaseDrawWithStarbux`, `AddStarbux2`, `RebuildAmmo3`). Not in scope for this plan but flagged for the safety lane's follow-up.

## 3. Reconciled first-slice boundary (smallest safe implementation slice)

The synthesis's "Slice 1" is achievable but touches two domains at once (docs corrections + catalog). Recommended split into two dependency-ordered slices, each independently reviewable:

- **Slice 0 (docs-only, READ-ONLY):** Apply the 5 correction items to `INVENTORY_ENDPOINT_ANALYSIS.md` (ConsumeItem2 checksum row, MarkItemsNotNew template resolution, CombineItem4 capture status, Item-node response shape, recomputed checksum-endpoint count). Depends on nothing. Files: `INVENTORY_ENDPOINT_ANALYSIS.md` only.

- **Slice 1 (READ-ONLY):**
  - new files: `sdk/inventory/__init__.py`, `sdk/inventory/designs.py` (load + cache static and dynamic design feeds; resolve ItemDesigns from the **dynamic** feed), `sdk/inventory/catalog.py` (normalize `listItemsOfAShip` via `_extract_collection`; typed Item/ItemDesign value objects preserving the 6 captured Item attributes and the full ItemDesign attribute set including `@Ingredients`, `@Rarity`, `@Tags`, `@ModuleType`), `sdk/inventory/classify.py` (bucket rules + rarity/tag guards + unknown→protected), `sdk/inventory/checksums.py` (`checksum_consume_item2`, `checksum_activate_item4`, `checksum_mark_items_not_new` — pure functions, fixture-tested).
  - modified: `sdk/commands.py` (register `inventory` and `inventory plan` read-only verbs; `inventory apply` registered as a stub that always refuses), `sdk/client.py` (add `listAllDynamicDesigns` fetch ONLY if not already present — verify before coding), README.template (+ regenerate README.md).
  - non-goals: no `executor.py`, no `transfer.py`, no mutation calls, no TUI changes beyond listing new commands.
  - tests: parsing fixtures (single-dict and list forms), classifier bucket tests + unknown/tag protection cases, checksum fixture tests reusing `tests/synthetic_fixtures.py` + `tests/fixtures/checksum_samples.json` patterns, CLI dry-run smoke test. All mocked; zero network.

## 4. Phasing — reconciled roadmap (unchanged from synthesis, validated as compatible with repo)

Phase 0 docs corrections → Phase 1 read-only catalog/designs/classifier →
Phase 2 protection policy module (`sdk/inventory/protection.py` encoding the 10 protected rules) →
Phase 3 demand planner (`demand.py`, training/equipment/craft/module reservations; conservative defaults where plans don't exist) →
Phase 4 dry-run reduction planner + `inventory plan` CLI →
Phase 5 portfolio/aggregation (`portfolio.py`; N account snapshots loaded from disk artifacts, no live multi-login) →
Phase 6 remaining read-only CLI verbs (`inventory accounts|deficits|transfers`) →
Phase 7 capture backlog (see §5) →
Phase 8 executor/reconciler interfaces with fakes only (`executor.py`, `reconcile.py`, both hard-disabled) →
Phase 9 per-endpoint enablement, each a separate authorized slice with captures.

## 5. Ranked capture backlog (gate list — unchanged, confirmed)

1. `ListItemsOfAShip` full response (blocks full field enumeration)
2. `CombineItem4` with non-empty itemIds (checksum ambiguity)
3. `DeleteItem3` (`remainingQuantity` semantics)
4. `SellItem3` + `ListSalesByItemDesignId` + `Purchase4` sequence
5. `GiftItem`
6. Second independent `ActivateItem4` sample
7. `WearItem` / `UnequipItems`
8. Any one room-module endpoint
9. `RepairBrokenModules`, `Refill2`, `DeleteAmmo` (lower)
10. `ListCraftDesigns` (only to prove redundancy vs `@Ingredients`)

## 6. Machine-readable facts

```json
{
  "worker": "t_b9d76783",
  "role": "reconcile",
  "mutations_attempted": 0,
  "files_changed": ["docs/worker-report-reconcile-t_b9d76783.md"],
  "corrections_confirmed": 5,
  "disagreements": [
    "_extract_collection should stay in sdk/client.py; sdk/inventory imports it",
    "ConsumeItem2: H3 (policy doc) vs H2 (synthesis) — reconciled as unwired today, executor plumbing allowed behind hard-disable"
  ],
  "first_slices": [
    {"id": "slice-0-docs", "kind": "READ-ONLY", "files": ["INVENTORY_ENDPOINT_ANALYSIS.md"]},
    {"id": "slice-1-catalog", "kind": "READ-ONLY", "files": ["sdk/inventory/{__init__,designs,catalog,classify,checksums}.py", "sdk/commands.py", "sdk/client.py", "README.template", "README.md", "tests/"]}
  ],
  "critical_blockers": ["ListItemsOfAShip never captured"],
  "phases": 9,
  "cli_verbs": ["inventory", "inventory plan", "inventory accounts", "inventory deficits", "inventory transfers", "inventory apply (disabled)"]
}
```
