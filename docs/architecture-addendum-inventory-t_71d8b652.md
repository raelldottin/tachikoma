# Inventory Architecture Addendum — Module Boundaries, APIs, Seams, Config

Planning task: `t_71d8b652` (swarm root `t_b9080d7a`, architect lane).
Companion to: `docs/inventory-implementation-plan.md` (analyst lane `t_f8b6c248`).
Status: PLAN ONLY. No production code changed. No live mutations performed.

The analyst lane delivered the full 22-section plan: reconciliation, classification
model, phasing, task decomposition, capture backlog, definition of done. This
addendum covers what the architect lane is specifically responsible for and that
the analyst plan intentionally left at design-sketch level:

1. Concrete module boundaries and dependency direction.
2. Internal API signatures the implementation slices will code against.
3. Dependency-injection seams that keep tests fully offline.
4. Determinism and ordering rules, in test-assertable form.
5. Configuration schema and safe defaults.
6. Dry-run output format (the on-disk / on-screen shape of `inventory plan`).
7. Audit/logging contract for every gate refusal.
8. CLI vs TUI placement, matched to the existing `sdk/commands.py` +
   `sdk/tui.py` architecture.
9. Parallelization refined against real import-graph constraints.
10. Open architectural questions the verifier (`t_6391549a`) or follow-up lanes
    should resolve.

Sources reconciled (read in full by this lane):

- `docs/swarm-synthesis-inventory-t_1a2fac28.md`
- `docs/worker-report-inventory-t_730c8a08.md`
- `docs/worker-report-inventory-t_74779a1f.md`
- `docs/worker-report-inventory-t_fd97b1ee.md`
- `docs/security/protected-item-policy.md`
- `docs/worker-report-reconcile-t_b9d76783.md`
- `docs/inventory-implementation-plan.md` (analyst lane, adopted)
- `INVENTORY_ENDPOINT_ANALYSIS.md` (stale; corrections per analyst §1)
- `sdk/client.py` (3,759 LOC at HEAD `2cb5aa4`)
- `sdk/security.py`, `sdk/commands.py`, `sdk/tui.py`, `run.py`, `tests/`

Evidence tiers: VERIFIED / CAPTURED-NOT-VERIFIED / STATICALLY-DISCOVERED /
INFERRED / UNKNOWN (unchanged from research swarm).

---

## 1. Module boundaries — the contract

The architect decision that everything else hangs off:

```
sdk/inventory/                    new package; pure-Python; zero network imports
sdk/inventory/__init__.py         re-exports the public surface only
sdk/inventory/_types.py           frozen dataclasses + enums (no logic)
sdk/inventory/designs.py          design feed loading & normalization
sdk/inventory/catalog.py          ItemService/ListItemsOfAShip → typed Items
sdk/inventory/checksums.py        pure checksum functions (MD5)
sdk/inventory/protection.py       protected-item policy (10 rules)
sdk/inventory/classify.py         bucket classifier (deterministic)
sdk/inventory/demand.py           reservation math (training/equip/craft/module)
sdk/inventory/portfolio.py        multi-account surplus/deficit (pure)
sdk/inventory/reduction.py        dry-run planner (deterministic)
sdk/inventory/transfer.py         transfer proposal builder (PURE; never mutates)
sdk/inventory/executor.py         gated mutation dispatcher — HARD-DISABLED
sdk/inventory/reconcile.py        snapshot/verify wrapper (fakes only in tests)
```

Dependency direction is one-way and strictly enforced:

```
            ┌─────────────┐
            │  sdk.client │  (transport; owns network)
            └──────┬──────┘
                   │ imports for types only
                   ▼
        ┌──────────────────────┐
        │   sdk.inventory.*    │  (pure domain; no network)
        └──────────────────────┘
                   ▲
                   │ uses
            ┌──────┴──────┐
            │sdk.commands │  (CLI verbs; thin)
            │sdk.tui      │  (interactive UI; thin)
            └─────────────┘
```

`sdk/client.py` never imports `sdk.inventory.*`. `sdk/inventory/*` never imports
`requests` or performs I/O; creation-time inputs are already-parsed dicts or
domain objects supplied by the caller.

Rationale: this is what lets us test every planner / classifier / reconciler
branch without mocking HTTP. The test seam is the function boundary, not a
network library.

## 2. What `sdk/client.py` gains (and only this)

Add exactly one thin fetch method:

```python
def listAllDynamicDesigns(self) -> bool:
    """Fetch DesignService/ListAllDynamicDesigns. ItemDesigns live here."""
    # Pattern matches listAllDesigns4: pulls @ItemDesignVersion and related
    # dynamic versions from self.latestVersion, calls the dynamic endpoint,
    # parses with xmltodict, sets self.dynamicItemDesigns etc.
```

No other behavior added to `Client`. In particular, we do NOT move
`_extract_collection` out of `client.py` (reconcile lane's disagreement
resolved: too many call sites; `sdk/inventory/*` imports it from `sdk.client`).

Existing inventory-touching methods in `client.py` remain untouched in slice 1:
`listItemsOfAShip`, `findConsumableDesignId`, `useConsumable`, `activateItem3`.
Their replacement / rewiring is a later phase once the read side is proven.

## 3. Public API of `sdk/inventory`

Everything outside this list is private (`_`-prefixed):

```python
# sdk/inventory/_types.py
class Evidence(Enum):        VERIFIED; CAPTURED_NOT_VERIFIED
                             CAPTURED_AMBIGUOUS; STATICALLY_DISCOVERED
                             INFERRED; UNKNOWN

class ReservationSource(Enum): TRAINING; EQUIPMENT; CRAFT; MODULE; STRATEGIC
class Disposition(Enum):     KEEP; USE; CRAFT; ALLOCATE; TRANSFER; SELL
                             CONSUME; DELETE
class Classification(Enum):  PROTECTED; RESERVED; SURPLUS_USABLE;
                             SURPLUS_CRAFTABLE; TRANSFERABLE; SELLABLE;
                             CONSUMABLE; DISCARDABLE; UNKNOWN

@dataclass(frozen=True)
class ItemDesign:
    item_design_id: int
    item_name: str
    item_type: str             # e.g. "Equipment", "Cosmetic", "Module"
    item_sub_type: str         # e.g. "InstantTraining", "Artifact", ...
    rarity: str                # "Common"|"Uncommon"|"Rare"|"Epic"|"Hero"|...
    module_type: Optional[str]
    tags: Tuple[str, ...]      # parsed from @Tags
    ingredients: Tuple[Tuple[int, int], ...]   # (design_id, qty)
    training_design_id: Optional[int]
    raw: Mapping[str, Any]     # unmodeled fields, escape hatch

@dataclass(frozen=True)
class Item:
    item_id: int
    ship_id: int
    item_design_id: int
    quantity: int
    is_new: bool
    skin_key: Optional[str]
    raw: Mapping[str, Any]

@dataclass(frozen=True)
class Reservation:
    item_design_id: int
    source: ReservationSource
    quantity: int
    reason: str

@dataclass(frozen=True)
class ClassifiedItem:
    item: Item
    design: Optional[ItemDesign]    # None if unknown
    classification: Classification
    reservation: Tuple[Reservation, ...]
    available_surplus: int          # quantity − Σreservations
    evidence: Evidence
    reason: str

@dataclass(frozen=True)
class PlannedAction:
    disposition: Disposition
    item_id: int
    item_design_id: int
    quantity: int
    reason: str
    evidence: Evidence
    gate: str                       # "SAFE_READ" | "DISABLED_INTERFACE" | ...
    endpoint: Optional[str]         # e.g. "ItemService/ConsumeItem2"

@dataclass(frozen=True)
class AccountSnapshot:
    account_label: str              # human-friendly
    captured_at: str                # ISO8601
    items: Tuple[Item, ...]
    characters: Tuple[Mapping[str, Any], ...]  # raw rows for @ItemIds

@dataclass(frozen=True)
class PortfolioRow:
    item_design_id: int
    by_account: Mapping[str, int]   # account_label -> quantity
    demand_by_account: Mapping[str, int]
    surplus_by_account: Mapping[str, int]
    deficit_by_account: Mapping[str, int]
    global_available_surplus: int
```

Public services (constructed by callers, not by `sdk/client.py`):

```python
# designs.py
class DesignCatalog:
    @classmethod
    def from_client(cls, client) -> "DesignCatalog": ...
    @classmethod
    def from_dicts(cls, static_branch: dict, dynamic_branch: dict) -> "DesignCatalog": ...
    def get(self, design_id: int) -> Optional[ItemDesign]: ...
    def all(self) -> Sequence[ItemDesign]: ...

# catalog.py
class InventoryCatalog:
    @classmethod
    def from_client(cls, client) -> "InventoryCatalog":
        """Pull client.itemsOfAShip + client.dynamicItemDesigns, normalize."""
    @classmethod
    def from_dicts(cls, items_response: dict, design_catalog: DesignCatalog) -> "InventoryCatalog": ...
    def items(self) -> Sequence[Item]: ...
    def by_design(self, design_id: int) -> Sequence[Item]: ...

# protection.py
class ProtectedItemPolicy:
    def evaluate(self, item: Item, design: Optional[ItemDesign],
                 ctx: "ProtectionContext") -> Optional[str]:
        """Return reason string if protected, else None. Runs the 10 rules
        in fixed order and short-circuits."""

# classify.py
class InventoryClassifier:
    def __init__(self, policy: ProtectedItemPolicy, reservation_plan: "ReservationPlan"): ...
    def classify(self, catalog: InventoryCatalog) -> Sequence[ClassifiedItem]: ...

# demand.py
class ReservationPlan:
    reservations: Tuple[Reservation, ...]
    def total_for(self, design_id: int) -> int: ...

class InventoryDemandPlanner:
    def plan(self, catalog: InventoryCatalog,
             characters: Sequence[Mapping],
             design_catalog: DesignCatalog,
             config: "InventoryConfig") -> ReservationPlan: ...

# portfolio.py
class AccountPortfolio:
    @classmethod
    def from_snapshots(cls, snaps: Sequence[AccountSnapshot],
                       reservation_plans: Mapping[str, ReservationPlan]
                       ) -> "AccountPortfolio": ...
    def rows(self) -> Sequence[PortfolioRow]: ...

# reduction.py
class InventoryReductionPlanner:
    def plan(self, classified: Sequence[ClassifiedItem],
             config: "InventoryConfig") -> Sequence[PlannedAction]: ...

# transfer.py
class TransferPlanner:
    def propose(self, portfolio: AccountPortfolio) -> Sequence[PlannedAction]:
        """PURE: emits TRANSFER-disposition PlannedActions. Never mutates."""

# executor.py (Phase 8+, disabled)
class InventoryExecutor:
    def __init__(self, *, enabled: bool = False, allow_list: FrozenSet[str] = frozenset()):
        if not enabled:
            self._disabled = True
    def dispatch(self, action: PlannedAction) -> None:
        if self._disabled or action.endpoint not in self._allow_list:
            raise ExecutorDisabled(...)

# reconcile.py (Phase 8+, fakes only)
class InventoryReconciler:
    def __init__(self, snapshot_provider, refetch_provider): ...
    def verify(self, before: InventoryCatalog,
               after: InventoryCatalog,
               expected_delta: DeltaSpec) -> None:
        """Raise ReconciliationMismatch on any deviation."""
```

Two `from_dicts` / `from_client` classmethod pairs are the deliberate test
seam: tests exercise pure `from_dicts` paths with fixture XML; production
paths use `from_client`. Neither path performs I/O itself.

## 4. Design-catalog resolution rule (VERIFIED)

`DesignCatalog.get(design_id)` looks up `design_id` in this priority order:

1. The DYNAMIC feed (2,557 rows; captured). This is the only authoritative
   source for `ItemDesign` rows.
2. The STATIC feed for `TrainingDesign` and `CraftDesign` rows (captured via
   `ListAllStaticDesigns2`).

If neither feed contains `design_id`, the catalog returns `None` and the
classifier treats the item as UNKNOWN → PROTECTED. There is no third branch
and no fall-through to "worse" feeds.

## 5. Determinism rules (test-assertable)

Every ordered output must satisfy these invariants; each is a unit test:

R1. Classifier output is a pure function of `(catalog, snapshot_characters,
    reservation_plan, design_catalog, config)`. Same inputs → byte-identical
    ordered output.

R2. Stable sort keys, applied in order:
    1. `classification` rank (PROTECTED < RESERVED < UNKNOWN < SURPLUS_* <
       TRANSFERABLE < SELLABLE < CONSUMABLE < DISCARDABLE — protection first)
    2. `disposition` rank per §12 of the analyst plan (KEEP earliest)
    3. `rarity` rank descending (Legendary > Hero > Unique > Epic > Rare >
       Uncommon > Common; unknown rarities sort last)
    4. `item_design_id` ascending
    5. `item_id` ascending

R3. `PlannedAction` lists are deduplicated on `(item_id, disposition,
    quantity)`; duplicates collapse to a single row.

R4. ReservationPlan is commutative: the order in which reservations are added
    does not affect `total_for(design_id)` or downstream planner output.
    Implementation stores reservations as a sorted tuple keyed on
    `(item_design_id, source.value, quantity)`.

R5. The planner never returns an empty `reason` field. Every action carries a
    human-readable reason and an `Evidence` tier.

## 6. Configuration schema

New optional section, parsed once at startup by `run.py` / TUI `main()` and
handed as plain dict to the inventory services. Defaults chosen to be
conservative (everything protected unless proven surplus):

```ini
[inventory]
strategic_reserve_per_craft_recipe = 1
strategic_reserve_per_training_tier = 5
strategic_reserve_per_ammo_reload = 1
retention_minimum_common_equipment = 0
unknown_is_protected = true
max_deletions_per_run = 0
max_consumptions_per_run = 0      # bounded by training plan only
max_transfers_per_run = 0
max_sales_per_run = 0
max_starbux_per_run = 0           # mirrors PSS_MAX_STARBUX_PER_RUN
transfer_marketplace_enabled = false
transfer_gift_enabled = false
inventory_apply_enabled = false   # overrides everything; hard off
audit_log_path = logs/inventory-audit.jsonl
taxonomy_version = 1
```

Every knob has a safe default if the `[inventory]` section is absent; the
planner must run identically with zero configuration. None of the four
`max_*_per_run` caps can be raised by a CLI flag — they only come from the
config file or from environment variables (with the environment winning for
CI). `inventory_apply_enabled` is the single source of truth for whether the
executor may fire at all.

## 7. Dry-run output format

`inventory plan` (and the eventual `inventory apply --dry-run`) emits an
ordered plan with both human and machine renderers.

Human (default):

```
Inventory reduction plan (DRY-RUN) — 2026-09-21T14:03:11Z
Account: default  Ship: 1234567  Items considered: 311
Bucket summary:
  protected: 241    reserved: 47    surplus: 23
Disposition order:
  KEEP/RESERVE -> USE -> CRAFT -> ALLOCATE -> TRANSFER -> SELL -> CONSUME -> DELETE

Proposed actions (in deterministic order):
  [ 1] KEEP     design=563  qty=1   reason=equipped on character 55159886
                                       evidence=CAPTURED_NOT_VERIFIED
  [ 2] USE      item=987654 qty=1   design=Protein Shake (InstantTraining)
                                       reason=planned training for character 55159886
                                       gate=DISABLED_INTERFACE (consume via verified checksum)
  [ 3] CRAFT    design=987  qty=2   reason=artifact reagent for recipe 104
                                       gate=NEEDS_CAPTURE (CombineItem4 ambiguous)
  ...
No action will be executed. To enable a mutation, see
docs/security/protected-item-policy.md and the relevant slice.
```

Machine (`--format json` or writing to `audit_log_path`):

```json
{
  "schema": "tachikoma.inventory.plan.v1",
  "generated_at": "2026-09-21T14:03:11Z",
  "account": "default",
  "ship_id": 1234567,
  "config_digest": "<sha256 of effective config>",
  "input_digests": {
    "itemsOfAShip": "<sha256 of raw xmltodict dump>",
    "dynamicDesigns": "<sha256>",
    "staticDesigns": "<sha256>"
  },
  "actions": [
    {"idx": 1, "disposition": "KEEP", "item_id": null, "item_design_id": 563,
     "quantity": 1, "reason": "equipped on character 55159886",
     "evidence": "CAPTURED_NOT_VERIFIED", "gate": "SAFE_READ"},
    {"idx": 2, "disposition": "USE", "item_id": 987654, "item_design_id": 42,
     "quantity": 1, "reason": "planned training for character 55159886",
     "evidence": "VERIFIED", "gate": "DISABLED_INTERFACE"}
  ],
  "bucket_summary": {"protected": 241, "reserved": 47, "surplus": 23},
  "gate_summary": {"SAFE_READ": 240, "DISABLED_INTERFACE": 5,
                   "NEEDS_CAPTURE": 58, "DISABLED_H3": 8}
}
```

The digests make the plan replayable and auditable. Two plans with identical
config + input digests MUST have identical `actions` arrays.

## 8. Audit logging contract

Append-only JSONL at `audit_log_path`. One record per gate decision and per
dispatch attempt. Every record:

```json
{"at": "ISO8601", "hostname": "...", "account": "...",
 "event": "gate_refusal" | "plan_emitted" | "dispatch_blocked" |
          "dispatch_succeeded" | "reconcile_abort" | "reconcile_ok",
 "endpoint": "...", "item_id": 0, "item_design_id": 0, "quantity": 0,
 "reason": "...", "evidence": "...",
 "before_digest": "...", "after_digest": "..." (when applicable)}
```

Rules:

- Secrets never appear in the audit log: all user-supplied strings pass
  through `sdk/redaction.redact_secrets` at write time.
- Never mutate an existing audit line. The reconciler pastes new lines on
  recovery.
- The audit log is the source of truth for "why was X never deleted/sold":
  any autonomous code path that touches an item must write a `gate_refusal`
  OR `dispatch_*` row.

## 9. CLI vs TUI placement

The existing `sdk/commands.py` `CommandRegistry` is the only command surface;
`sdk/tui.py` is a thin REPL that consumes it. `run.py` is a non-interactive
argparse pipeline. We do NOT add a subcommand tree to `run.py` in this slice.

Read-only verbs (register in `CommandRegistry._register_default_commands`):

- `inventory`               — bucket summary (per §4 buckets)
- `inventory plan`          — human-readable dry-run plan
- `inventory plan --json`   — machine format
- `inventory accounts`      — Phase 6
- `inventory deficits`      — Phase 6
- `inventory transfers`     — Phase 6 (proposals only)
- `inventory apply`         — registered as a `verified=False` stub that
                              ALWAYS refuses with a gate message naming the
                              phase that must first land. Not removable by a
                              future CLI flag; only removable by deleting the
                              stub and shipping a real executor in Phase 9.

`run.py` does not gain a non-interactive `inventory` flag in slice 1; the TUI
is the only entry point to inventory verbs. This keeps the autonomous daily
loop (`run.py`) physically incapable of touching items until Phase 9
explicitly authorizes a wired path.

## 10. Phasing (refined against import graph)

|-----------|-----------------------------------------------------|
| Phase     | Can merge after | Merger dependency reason             |
|-----------|-----------------|--------------------------------------|
| 0 docs    | —               | touches only `INVENTORY_ENDPOINT_ANALYSIS.md` |
| 1.a checksums | —           | pure functions in `sdk/security.py` or `sdk/inventory/checksums.py`; no other deps |
| 1.b designs   | —           | new client method + `designs.py`; no other deps |
| 1.c catalog   | 1.b         | consumes `DesignCatalog`                |
| 2 protection + classify | 1.c | needs the typed Item/ItemDesign surface |
| 3 demand    | 2             | consumes classifier output            |
| 4 reduction + plan CLI | 3 + 1 (CLI dependency on `commands.py` only) | needs reservations |
| 5 portfolio + transfer | 3 | depends only on ReservationPlan shape |
| 6 accounts/deficits/transfers UX | 4 + 5 | CLI composition |
| 7 capture backlog | —           | external; blocks 8                     |
| 8 executor/reconcile (fakes only) | 4 + 7 | needs planned-action shape + captured checksums |
| 9 per-endpoint enablement | 8 | separately authorized per endpoint     |

Parallel-friendly pairs (independent PR-shaped slices):

- {1.a, 1.b} — no overlapping files
- {3, 5} — both consume the classifier's output shape, once frozen after Phase 2
- {4, 6} — only merge sequentially because both touch `sdk/commands.py`
- {7} — fully parallel

Sequential constraints: 2 must land before 3 and 4 (because classifier output
shape is the contract); 4 must land before 6 (because read-only CLI plumbing
shares a renderer with `inventory apply` refusal); 8 cannot start until at
least one target endpoint has a captured checksum.

## 11. Open questions for the verifier / follow-up

These are consciously left open by this lane and should be resolved by
`t_6391549a` (verifier) or follow-up lanes before their blocking phase
starts:

Q1. Where do `checksum_consume_item2` / `checksum_activate_item4` /
    `checksum_mark_items_not_new` live: `sdk/security.py` (existing style,
    matches `checksum_collect_resources3`) or `sdk/inventory/checksums.py`
    (keeps native checksums together)? My recommendation: `sdk/security.py`,
    because it already holds all MD5 primitives and existing harness
    (`tests/synthetic_fixtures.py`) does not import from `sdk/inventory/*`.

Q2. Does `listAllDynamicDesigns` belong on `Client` as a sibling of
    `listAllDesigns4`, or should `Client` grow a generic `fetch_design_feed`
    taking an enum? Sibling method (matches existing style) is my
    recommendation; refactor into a generic helper can happen later without
    breaking the inventory package.

Q3. `AccountSnapshot` source of truth: hand-maintained JSON, or
    `listItemsOfAShip` + `listAllCharactersOfUser` XML cached per account?
    This decision blocks Phase 5. Recommend: define a versioned on-disk
    snapshot format first (this plan's §7 schema), with a tool that
    materializes it from fetched XML. Captures then become snapshots without
    extra parsing code in the planner.

Q4. `Evidence` tier on `ClassifiedItem`: should the tier be the *minimum*
    across (item, design, reservation) reads, or per-source (allowing the
    classifier to flag an item RESERVED on VERIFIED design but
    CAPTURED-ambiguous reservation)? Per-source is more informative but
    complicates the determinism contract; recommend the simpler "minimum
    across inputs" rule for slice 1, revisit after Phase 3.

Q5. Whether `protected-item-policy.md` rule 9 (marketplace-valuable) is
    evaluated in Phase 2 at all. `ListSalesByItemDesignId` is
    STATICALLY-DISCOVERED; there is no safe read path today. Recommend:
    evaluating as "no data → no demotion", logged as
    `gate=NEEDS_CAPTURE marketplace evaluation skipped`, so the policy is
    structurally present but cannot change classification until the endpoint
    is captured.

## 12. What this lane is NOT changing

- No edits to `sdk/client.py`, `run.py`, `sdk/commands.py`, `sdk/tui.py`.
- No edits to any test file.
- No mutation of any kind.
- No implementation of `sdk/inventory/*`; that belongs to follow-up slices
  driven by the analyst plan's task list (A–L).

## 13. Handoff

This document plus the analyst's `docs/inventory-implementation-plan.md` are
sufficient for the next implementation slice (`Slice 0` docs corrections or
`Slice 1` catalog/checksums) to start without further planning. Sibling lanes
`t_23f9c5c6` (protocol gates) and `t_d5626222` (safety boundaries) build on
this without conflicting scope: `t_23f9c5c6` fills in the readiness matrix
tables already outlined in the analyst plan's §13/§14, and `t_d5626222`
operationalizes the reconciliation contract sketched in §17 / §20.

End of architect addendum.
