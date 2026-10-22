# Inventory Management — Implementation Plan

Planning task: `t_f8b6c248` (swarm root `t_b9080d7a`).
Status: PLAN ONLY. No production code changed by this task. No live mutations performed.

## 1. Research reconciliation and corrections

Sources reconciled (all read in full, verified against repo HEAD `2cb5aa4`):

- `docs/swarm-synthesis-inventory-t_1a2fac28.md` (synthesizer)
- `docs/worker-report-inventory-t_730c8a08.md` (repo/data-model lane)
- `docs/worker-report-inventory-t_74779a1f.md` (endpoint/capture lane)
- `docs/worker-report-inventory-t_fd97b1ee.md` (classification/demand/multi-account lane)
- `docs/security/protected-item-policy.md` (safety lane `t_a20d45b8`)
- `docs/worker-report-reconcile-t_b9d76783.md` (reconcile lane)
- `INVENTORY_ENDPOINT_ANALYSIS.md` (stale static catalog)
- `sdk/client.py`, `sdk/security.py`, `sdk/commands.py`, `run.py`, tests/

The reconcile lane (`t_b9d76783`) already confirmed the synthesis against repo truth.
This plan adopts its two judgment calls:

1. `_extract_collection` stays in `sdk/client.py`; `sdk/inventory/` imports it from
   there. No mass churn of existing call sites.
2. `ConsumeItem2` is reconciled as: checksum VERIFIED, plumbing may be implemented
   behind a hard-disabled executor, but it stays unwired from autonomous flows.

Confirmed correction list (drives Phase 0):

1. `ConsumeItem2` HAS a checksum: `MD5(clientDateTime+itemId+quantity+characterId+CK+SC)`,
   wire `?itemId=&quantity=&characterId=&clientDateTime=&checksum=&accessToken=`.
   The "no-checksum" row in the analysis doc is wrong.
2. `MarkItemsNotNew` template resolves to `?checksum=&clientDateTime=&accessToken=`,
   preimage `clientDateTime+accessToken`. No longer UNKNOWN.
3. `CombineItem4` has one capture (2026-08-01, empty `itemIds`) — the "No captures"
   row is stale; checksum remains ambiguous.
4. Item nodes carry 6 attributes (`ItemId, ShipId, ItemDesignId, Quantity, IsNew,
   SkinKey`); equipment linkage lives on `Character/@ItemIds` + `Character/Items`.
5. ItemDesigns come from `DesignService/ListAllDynamicDesigns` (2,557 rows), NOT the
   static branch read by `listAllDesigns4` (client.py:746-900). The checksum-endpoint
   count in the analysis doc must be recomputed accordingly.

Verified at plan time: `sdk/client.py` has NO `listAllDynamicDesigns` fetch — it must
be added. `sdk/security.py` has no helpers for ConsumeItem2/ActivateItem4/
MarkItemsNotNew.

## 2. Current Tachikoma architecture relevant to inventory

- All inventory behavior lives in `sdk/client.py` (~3,760 LOC) as `Client` methods.
  `itemsOfAShip` (client.py:690-697) is raw `xmltodict` output; normalization is
  ad-hoc via `_extract_collection(..., "Item")` (client.py:50) at point of use.
- Only `@ItemId` and `@ItemDesignId` are consumed anywhere.
  `findConsumableDesignId` (client.py:1677-1704) is name-substring matching; it is
  the only classifier that exists.
- `useConsumable` (client.py:1636) → `activateItem3`; training flow binds inventory
  at call time with no reservation concept.
- `sdk/security.py` holds checksum primitives (style reference:
  `checksum_collect_resources3` at sdk/security.py:579).
- `sdk/commands.py` is a `CommandRegistry` mapping typed commands to Client methods;
  no item-aware commands exist.
- CLI entry is `run.py` argparse flags (`--auth-file`, `--login-email`, ...);
  there is no subcommand structure today.
- Repo manages exactly one account per working directory (`.device` + `auth.json`).
- Tests: `tests/synthetic_fixtures.py` + `tests/fixtures/checksum_samples.json`
  provide the checksum harness pattern to reuse. No ListItemsOfAShip parsing tests
  exist.

## 3. Proposed inventory-domain architecture

New package `sdk/inventory/`. `sdk/client.py` remains the transport layer and gains
only the `listAllDynamicDesigns` fetch. Nothing else moves into it.

```
sdk/inventory/__init__.py    package surface
sdk/inventory/designs.py     static+dynamic design feed loading/caching;
                             ItemDesigns resolved from the DYNAMIC feed
sdk/inventory/catalog.py     InventoryCatalog — normalize ListItemsOfAShip into
                             typed Item records (imports _extract_collection)
sdk/inventory/classify.py    InventoryClassifier — bucket rules + rarity/tag guards
sdk/inventory/checksums.py   checksum_consume_item2 / checksum_activate_item4 /
                             checksum_mark_items_not_new (pure functions)
sdk/inventory/demand.py      InventoryDemandPlanner (Phase 3)
sdk/inventory/protection.py  protected-item policy module (Phase 2)
sdk/inventory/portfolio.py   AccountPortfolio (Phase 5)
sdk/inventory/reduction.py   InventoryReductionPlanner — dry-run actions (Phase 4)
sdk/inventory/transfer.py    TransferPlanner — proposals only (Phase 5)
sdk/inventory/executor.py    gated mutation dispatcher — HARD-DISABLED (Phase 8)
sdk/inventory/reconcile.py   snapshot→mutate→refetch→verify wrapper (Phase 8)
```

Dependency direction: inventory → client (transport). Never the reverse.

## 4. Domain / data models

Immutable value objects (Python `@dataclass(frozen=True)`; no third-party deps —
matches existing project style):

- `ItemDesign`: item_design_id: int, item_name: str, item_type: str,
  item_sub_type: str, rarity: str, module_type: str|None, tags: tuple[str, ...],
  ingredients: tuple[(design_id, qty), ...], training_design_id: int|None,
  raw: mapping (escape hatch).
- `Item`: item_id: int, ship_id: int, item_design_id: int, quantity: int,
  is_new: bool, skin_key: str|None. Only the 6 capture-observed attributes; do NOT
  model rarity/enhancement on Item until ListItemsOfAShip is captured.
- `Character.equipped_item_ids: tuple[int, ...]` — parsed from `Character/@ItemIds`.
- Ingredients parse: `"81x5"` / `"78x3|104x6"` (pipe-separated `designId x qty`).
- `Reservation`: design_id, source (enum: TRAINING / EQUIPMENT / CRAFT / MODULE /
  STRATEGIC), quantity, reason: str.
- `Classification`: enum PROTECTED / RESERVED / SURPLUS_USABLE / SURPLUS_CRAFTABLE /
  TRANSFERABLE / SELLABLE / CONSUMABLE / DISCARDABLE / UNKNOWN, plus reason: str
  and evidence tier tag.
- `PlannedAction`: action kind, item_id / design_id, quantity, reason, evidence
  tier, endpoint gate status. Dry-run only until Phase 9.
- Evidence tier representation: `Evidence` enum (VERIFIED / CAPTURED_NOT_VERIFIED /
  CAPTURED_AMBIGUOUS / STATICALLY_DISCOVERED / INFERRED / UNKNOWN) carried on every
  gate decision.

ItemId vs ItemDesignId ownership: `ItemId` is an account-scoped instance identifier;
`ItemDesignId` is the global design key. All demand/reservation math is per
ItemDesignId; mutation proposals reference ItemId + ItemDesignId + expected quantity.

## 5. ItemDesign and inventory normalization strategy

- `designs.py`: fetch `ListAllDynamicDesigns` (new client method) for ItemDesigns;
  `ListAllStaticDesigns2` (via existing `listAllDesigns4` static branch) for
  TrainingDesigns/CraftDesigns. Normalize both single-dict and list forms through
  `_extract_collection`. Cache per run; no on-disk cache in slice 1.
- `catalog.py`: normalize `ListItemsOfAShip` via `_extract_collection(..., "Item")`;
  tolerant of missing attributes (default quantity=1, is_new=False); unknown
  attributes preserved in `raw` for forward compatibility.
- Keep `client.py` raw attributes untouched; the catalog reads
  `client.itemsOfAShip` / `client.itemDesigns` as today plus the new dynamic feed.

## 6. Crafting dependency-graph design

- Recipe source: `ItemDesign/@Ingredients` (VERIFIED against dynamic-feed capture;
  `ListCraftDesigns` treated as redundant unless a future capture proves otherwise).
- Graph: nodes = ItemDesignId; edges design → ingredient with qty weight. 902 /
  2,557 designs carry ingredients.
- Demand propagation: for each elected recipe goal `d × k`,
  `artifactDemand[i] += k * q_i` transitively up the DAG; cycle detection required
  (malformed-recipe handling) — on cycle, log and treat affected designs as
  PROTECTED, never crash.
- Until crafting goals exist in code, all Artifact ingredients default to
  RESERVED-WHEN-PRESENT (conservative hold).

## 7. Reservation model

```
available_surplus[design] = quantity
                          - training_reservation
                          - equipment_reservation
                          - crafting_reservation
                          - module_reservation
                          - strategic_reserve
```

Demand sources and conservative defaults (until real plans exist):

- Training: all InstantTraining items RESERVED-WHEN-PRESENT (142 TrainingDesigns;
  90 InstantTraining designs map to 63 distinct training design ids through
  `TrainingDesignId`). ReduceFatigue/ResetTraining/ReducePrestige/RerollBonusStats/
  SpecialCRISPR reserved — they reset costly-to-rebuild state.
- Equipment: currently worn (via `Character/@ItemIds`) + best-rarity-per-slot per
  owned character until an equipment plan type exists.
- Modules: ALL modules RESERVED-WHEN-PRESENT (room-module endpoints uncaptured).
- Strategic reserve: Immunity × all; crafting ingredients ≥ 1 full recipe per
  elected recipe (later); training tier ≥ 5 (later); ammo ≥ 1 room reload.

Thresholds configurable via a small config dict with safe defaults; defaults are the
conservative holds above.

## 8. Protected-item policy integration

`protection.py` encodes the 10 protected rules from `docs/security/protected-item-policy.md`
(equipped items, planned equipment, recipe inputs, room-attached modules,
Rare/Epic/Hero/Unique/Legendary rarities, event/trader-tagged designs — the 220-tag
set, quest/progression items incl. ship blueprints, premium items, marketplace-
valuable items once ListSales exists, unknown designs).

Binding rule: the classifier NEVER auto-demotes a protected or unknown design; a
protection hit short-circuits classification to PROTECTED. Guard refusals are logged
and surfaced (no silent state repair).

## 9. Classification algorithm

Deterministic pipeline per (Item, ItemDesign) pair:

1. Unknown design (not in dynamic feed) → PROTECTED("unknown design").
2. Protection policy hit → PROTECTED(rule id).
3. Bucket from `ItemType × ItemSubType × ModuleType × Ingredients` per the
   captured 13-bucket model (equipment 1,241 / cosmetic 378 / lootbox 227 /
   craft-vehicle 154 / ammo 143 / module 131 / training 113 / artifact 82 /
   android 37 / blueprint 16 / resource-pack 12 / immunity 4 / true-consumable 3 /
   speedup 3 / misc 7 / currency-proxy 2).
4. Apply reservations → PROTECTED/RESERVED when reservation > 0, else
   candidate surplus when `available_surplus > 0`.
5. Deterministic ordering: sort key (bucket rank, rarity rank desc, design_id asc).

## 10. Multi-account portfolio model

Greenfield. `AccountPortfolio` aggregates N per-account snapshots loaded from disk
artifacts (no live multi-login; the repo manages one account per working directory):

```
per account:
  inventory_vector: design_id -> qty
  demand_vector:    design_id -> reserved_qty
  surplus[design] = max(0, inventory - demand)
  deficit[design] = max(0, demand - inventory)
```

Transfer candidacy: `deficit > 0` on one account AND `surplus > 0` on another for
the same design → TransferPlanner emits a proposal (never a mutation).

Marketplace transfer stays proposal-only and must NEVER buy the first listing
matching an ItemDesignId; the future flow is SellItem3 → ListSalesByItemDesignId →
identify exact (seller, item, qty, price) saleId → Purchase4 → dual-account
re-verification. All three endpoints are STATICALLY-DISCOVERED with zero captures.

## 11. Dry-run reduction planner

Deterministic disposition order:

1. KEEP / RESERVE (everything not proven surplus)
2. USE for intended crew benefit (training consumables)
3. CRAFT / COMBINE — NOT wired (CombineItem4 ambiguous)
4. ALLOCATE as module — NOT wired
5. TRANSFER — proposals only
6. SELL — NOT wired
7. CONSUME — VERIFIED checksum, allow-list only, behind disabled executor
8. DELETE — last resort, semantics UNKNOWN

Only positive `available_surplus` enters steps 5–8. Output: ordered list of
`PlannedAction` with reason + evidence tier + gate status; stable across runs for
identical inputs (determinism test required).

## 12. CLI / TUI integration plan

Fit the existing `CommandRegistry` pattern in `sdk/commands.py` (register verbs; no
argparse subcommand rework in run.py for slice 1):

- `inventory` — per-bucket summary counts of current ship inventory.
- `inventory plan` — dry-run reduction plan, rendered as a table with reason codes.
- `inventory accounts` — per-account surplus/deficit summary (Phase 6).
- `inventory deficits` — cross-account deficit report (Phase 6).
- `inventory transfers` — transfer proposals (Phase 6).
- `inventory apply` — registered as a stub that ALWAYS refuses with a message
  naming the endpoint gate; removed from the refusal stub only by a later
  explicitly-authorized slice.

TUI: list the new read-only commands in help output; no interactive flows yet.
README.template updated before README.md (Makefile rule), per repo convention.

## 13. Evidence / protocol gates

Every endpoint carries one gate value, derived from evidence tier:

- SAFE_READ: read-only, usable now (no mutation surface).
- DISABLED_INTERFACE: implement behind a hard-disabled interface only.
- NEEDS_CAPTURE: requires an official-client capture.
- NEEDS_CHECKSUM_VERIFY: capture exists, checksum ambiguous.
- NEEDS_SEMANTIC_VERIFY: semantics unknown even if callable.
- DISABLED_H3: never autonomous; manual one-shot CLI with explicit confirmation.

Promotion rule: never promote based on endpoint name, IL2CPP metadata, or inference.

## 14. Endpoint readiness matrix

| Endpoint | Evidence | Gate |
|---|---|---|
| ItemService/ListItemsOfAShip | STATICALLY-DISCOVERED, implemented, never captured | SAFE_READ (code path in use); NEEDS_CAPTURE for full field enumeration |
| ItemService/ListItemDesigns2 | STATICALLY-DISCOVERED (superseded by design feeds) | SAFE_READ (unneeded; design feeds cover it) |
| ItemService/ListItemDesignActions | STATICALLY-DISCOVERED | SAFE_READ |
| DesignService/ListAllDynamicDesigns | CAPTURED (2,557 ItemDesigns) | SAFE_READ |
| DesignService/ListAllStaticDesigns2 | CAPTURED (10 samples) | SAFE_READ |
| ItemService/ConsumeItem2 | VERIFIED (3/3) | DISABLED_INTERFACE (allow-list only, behind disabled executor) |
| ItemService/ActivateItem4 | VERIFIED (1/1, single sample) | DISABLED_INTERFACE; needs 2nd capture |
| ItemService/ActivateItem3 | legacy in code, 0 captures | DISABLED_INTERFACE; do not extend |
| ItemService/MarkItemsNotNew | VERIFIED (1/1) | DISABLED_INTERFACE (harmless UI-state; low priority) |
| ItemService/CombineItem4 | CAPTURED-BUT-AMBIGUOUS | NEEDS_CAPTURE (non-empty itemIds) |
| ItemService/DeleteItem3 | STATICALLY-DISCOVERED | DISABLED_H3; NEEDS_SEMANTIC_VERIFY (remainingQuantity) |
| ItemService/GiftItem | STATICALLY-DISCOVERED | NEEDS_CAPTURE |
| MarketService/SellItem3 | STATICALLY-DISCOVERED (0 real captures) | NEEDS_CAPTURE |
| MarketService/Purchase4 | STATICALLY-DISCOVERED (checksum per template) | NEEDS_CAPTURE |
| MarketService/ListSalesByItemDesignId | STATICALLY-DISCOVERED read | NEEDS_CAPTURE |
| CharacterService/WearItem | STATICALLY-DISCOVERED (truncated template) | NEEDS_CAPTURE |
| CharacterService/UnequipItems | STATICALLY-DISCOVERED | NEEDS_CAPTURE |
| RoomService/ListCraftDesigns | STATICALLY-DISCOVERED | SAFE_READ candidate; treat as redundant vs @Ingredients until captured |
| RoomService/AddItemIdToRoom, MoveItemIdToRoom3, RemoveItemIdFromRoom, UpdateItemIdsForRoom2 | STATICALLY-DISCOVERED (checksum per template) | NEEDS_CAPTURE (all four, zero captures) |
| ItemService/RepairBrokenModules, DeleteAmmo, ActivateRenameItem, FixUnsynchronizedItems | STATICALLY-DISCOVERED | NEEDS_CAPTURE / low priority |
| RoomService/Refill2 | STATICALLY-DISCOVERED (Starbux spend) | NEEDS_CAPTURE; needs budget cap |

Pre-existing autonomous endpoints needing budget caps (separate follow-up, flagged
by safety lane): SpeedUpTravelling, CharacterService/Draw, AddStarbux2,
RebuildAmmo3.

## 15. Testing strategy

All tests mock network traffic; zero live Pixel Starships calls (repo rule).

- Parser/normalizer: fixtures for both design feeds and ListItemsOfAShip;
  single-dict AND list forms via `_extract_collection`; missing-attribute
  tolerance.
- Checksum harness: ConsumeItem2 (3 samples), ActivateItem4 (1), MarkItemsNotNew (1)
  reusing `tests/synthetic_fixtures.py` + `tests/fixtures/checksum_samples.json`.
- Classifier: one test per bucket; unknown→protected; tag→protected; rarity→
  protected; equipped→protected (Character/@ItemIds).
- Reservations: training/equipment/craft-DAG transitivity (multi-level recipe),
  module hold-all; cycle and malformed-ingredient handling.
- Portfolio: multi-account surplus/deficit vectors; transfer candidacy pairing.
- Planner: determinism (same inputs → identical ordered plan); only surplus enters
  dispositions 5–8.
- Reconciler (Phase 8, fakes only): delta mismatch aborts; unrelated ItemId change
  aborts; re-read failure aborts; ambiguous response aborts.
- CLI: dry-run smoke tests for each read-only verb; `inventory apply` refusal test.
- Negative/adversarial: empty inventory, unknown design ids in inventory, item with
  quantity 0 retained, malformed XML, account snapshot version skew.
- Regression: every corrected defect from the research gets a test naming the
  correction.

## 16. Dependency-ordered implementation phases

- Phase 0 — docs corrections (INVENTORY_ENDPOINT_ANALYSIS.md; 5 items in §1)
- Phase 1 — read-only domain: designs.py (dynamic feed), catalog.py, checksums.py
- Phase 2 — classify.py + protection.py (protected/unknown rules, deterministic)
- Phase 3 — demand.py (training/equipment/craft-DAG/module/strategic reservations)
- Phase 4 — reduction.py + `inventory plan` (dry-run, reason codes, deterministic)
- Phase 5 — portfolio.py + transfer.py (proposals only)
- Phase 6 — read-only UX: `inventory accounts|deficits|transfers`
- Phase 7 — protocol-evidence backlog (capture list §20)
- Phase 8 — executor.py + reconcile.py interfaces, fakes-only tests, hard-disabled
- Phase 9 — per-endpoint enablement; each a separately authorized slice with
  captures and reconciliation tests

Do not collapse mutation work into read-only phases.

## 17. Parallelization opportunities

- Phase 0 is independent of all code phases (docs-only).
- checksums.py (part of Phase 1) can proceed in parallel with designs.py/catalog.py.
- protection.py and classify.py (Phase 2) can develop in parallel after Phase 1.
- Phase 5 portfolio work can start once Phase 3 demand output shape is frozen;
  it does not depend on Phase 4 CLI.
- Phase 7 capture work is entirely parallel (external evidence gathering).
- Sequential chains: Phase 1 → 2 → 3 → 4; Phase 8 → 9 strictly ordered.

## 18. Issue / task breakdown

Task A (READ-ONLY) — Docs corrections.
Objective: apply the 5 corrections in §1 to `INVENTORY_ENDPOINT_ANALYSIS.md`.
Files: `INVENTORY_ENDPOINT_ANALYSIS.md`. Deps: none. Tests: none (doc-only).
Acceptance: all 5 corrections applied; checksum-endpoint count recomputed.

Task B (READ-ONLY) — Design feeds + checksums.
Objective: `sdk/inventory/designs.py` (dynamic-feed ItemDesigns + static
Training/Craft), new `listAllDynamicDesigns` fetch in client.py,
`sdk/inventory/checksums.py` with the three VERIFIED formulas.
Files: new `sdk/inventory/__init__.py`, `designs.py`, `checksums.py`;
modify `sdk/client.py`. Deps: none. Tests: feed parsing fixtures (both forms),
3 checksum fixture tests. Acceptance: dynamic feed yields ItemDesign count > 0 in
mocked test; checksum fixtures pass.
Excluded: no classification, no CLI.

Task C (READ-ONLY) — Catalog.
Objective: `sdk/inventory/catalog.py` normalizing ListItemsOfAShip into typed
Item/ItemDesign records. Deps: B. Files: new `catalog.py`; tests fixtures.
Acceptance: 6-attribute Item parse, tolerant defaults, both collection forms.

Task D (READ-ONLY) — Classifier + protection.
Objective: `classify.py` bucket rules + `protection.py` 10 rules; unknown→
protected; determinism. Deps: B, C. Tests: per-bucket, protection, unknown cases.
Acceptance: deterministic ordering; protection short-circuit verified.

Task E (READ-ONLY) — First CLI verbs.
Objective: register `inventory` and `inventory plan` (summary + classification
view only, no demand yet); `inventory apply` refusal stub. Deps: D.
Files: `sdk/commands.py`, README.template, README.md. Tests: CLI smoke + refusal.
Acceptance: `inventory apply` always refuses with gate message; verbs read-only.

Task F (READ-ONLY) — Demand planner.
Objective: `demand.py` training/equipment/craft-DAG/module/strategic reservations
with conservative defaults. Deps: D. Tests: reservation math, DAG transitivity,
cycle handling. Acceptance: surplus formula matches §7.

Task G (DRY-RUN) — Reduction planner.
Objective: `reduction.py` emitting ordered `PlannedAction`s; `inventory plan`
renders them. Deps: E, F. Tests: determinism, surplus gating. Acceptance: only
positive surplus enters dispositions 5–8; plan is byte-stable across runs.

Task H (READ-ONLY) — Portfolio.
Objective: `portfolio.py` over disk-loaded account snapshots. Deps: F. Tests:
multi-account vectors, deficit/surplus pairing. Acceptance: per-design per-account
surplus/deficit math per §10.

Task I (READ-ONLY) — Remaining UX.
Objective: `inventory accounts|deficits|transfers`. Deps: G, H. Tests: CLI smoke.
Acceptance: verbs read-only; transfer output is proposals only.

Task J (READ-ONLY) — Executor/reconciler interfaces.
Objective: `executor.py` hard-disabled dispatcher + `reconcile.py`
snapshot/verify contract, fakes-only tests. Deps: G. Tests: all abort conditions.
Acceptance: no path to a live mutation exists; every abort condition tested.

Task K (evidence) — Capture backlog execution. Deps: none. See §20.

Task L (MUTATION-CAPABLE, gated) — Per-endpoint enablement. Deps: J, K evidence.
One slice per endpoint; requires explicit authorization and capture verification.

## 19. Smallest safe first implementation slice

Slice 1 = tasks A + B + C + E-minimal (the reconcile lane's "Slice 0 + 1" split,
kept as two reviewable units):

- Slice 0 (docs): `INVENTORY_ENDPOINT_ANALYSIS.md` corrections only.
- Slice 1 (code): `sdk/inventory/{__init__,designs,catalog,checksums}.py` +
  `listAllDynamicDesigns` in `sdk/client.py` + `inventory` / `inventory plan`
  read-only verbs + `inventory apply` refusal stub in `sdk/commands.py` +
  README.template/README.md + tests (parsing fixtures, checksum fixtures, CLI
  smoke/refusal).

Explicit non-goals for slice 1: no executor, no transfer planner, no demand
planner, no TUI flows, no mutation of any kind, no `_extract_collection` move.
Exact acceptance: `make test` passes with all-mocked fixtures; `inventory apply`
refuses; no production code path can issue an item mutation.

## 20. Mutation-disabled backlog

| Endpoint | Desired capability | Evidence | Missing | Unblock condition |
|---|---|---|---|---|
| CombineItem4 | craft/combine surplus | CAPTURED-AMBIGUOUS | non-empty itemIds capture; checksum disambiguation | capture + checksum verify + reconciler test |
| DeleteItem3 | discard last resort | STATIC | remainingQuantity semantics; no checksum in template | capture + semantics + manual-CLI-only gate |
| GiftItem | owned-account transfer | STATIC | entire flow (recipient targeting, eligibility) | capture + dual-account reconcile |
| SellItem3 / ListSalesByItemDesignId / Purchase4 | marketplace transfer | STATIC (0 marketplace traffic in 16,703 exchanges) | whole sell→identify→buy→verify chain | capture all three + exact-listing matching + dual reconcile |
| WearItem / UnequipItems | equipment allocation | STATIC | WearItem param template truncated | capture + equip-delta reconcile |
| AddItemIdToRoom / MoveItemIdToRoom3 / RemoveItemIdFromRoom / UpdateItemIdsForRoom2 | module allocation | STATIC (0 captures, all four) | checksum verify + room-bind semantics | any single capture + checksum verify |
| ActivateItem4 | consumable activation | VERIFIED (1 sample) | 2nd independent sample | 2nd capture reconcile |
| Refill2 | room refill (Starbux) | STATIC | checksum verify + Starbux budget cap | capture + budget guard |
| RepairBrokenModules / DeleteAmmo / ActivateRenameItem | misc | STATIC | everything | capture |

Every blocked mutation requires reconciliation tests (fakes) before any live
enablement, per the Phase 8 contract.

## 21. Required additional captures (ranked)

1. `ListItemsOfAShip` — full item field enumeration (critical gap; blocks Item
   data model beyond 6 attributes).
2. `CombineItem4` with non-empty `itemIds` — resolves checksum ambiguity.
3. `DeleteItem3` — `remainingQuantity` semantics.
4. `SellItem3` + `ListSalesByItemDesignId` + `Purchase4` sequence.
5. `GiftItem`.
6. Second independent `ActivateItem4` sample.
7. `WearItem` / `UnequipItems`.
8. Any one room-module endpoint.
9. `RepairBrokenModules`, `Refill2`, `DeleteAmmo`.
10. `ListCraftDesigns` — only to prove redundancy vs `@Ingredients`.

## 22. Definition of done — inventory management feature

- Read-only catalog, classification, demand, reduction planning, and multi-account
  portfolio shipped with full mocked test coverage.
- All six CLI verbs present; `inventory apply` gated per endpoint with evidence
  gates enforced in code.
- `INVENTORY_ENDPOINT_ANALYSIS.md` consistent with capture truth.
- No mutation endpoint wired without: capture verification, checksum verification,
  semantic verification, reconciler coverage, per-run budget caps, and an explicit
  authorizing slice.
- Unknown/ambiguous state always resolves to PROTECTED and never to disposal.
- Deterministic planner output; audit logging on every gate refusal.

## 23. Recommended execution sequence

```
Task A (docs corrections, READ-ONLY)
  -> Task B (designs + checksums)
       -> {Task C (catalog)} 
            -> Task D (classifier + protection)
                 -> {Task E (CLI verbs), Task F (demand)}          [parallel]
                      -> Task G (reduction planner, DRY-RUN)
                           -> Task H (portfolio)
                                -> Task I (accounts/deficits/transfers UX)
                                     -> Task J (executor/reconciler, fakes only)
                                          -> Task Ln (per-endpoint enablement, gated)
Task K (capture backlog) — parallel throughout; gates Task L.
```

What can be implemented now: everything through Phase 6 (read-only) plus Phase 8
fakes-only interfaces. Blocked on protocol evidence: every mutation in §20.
