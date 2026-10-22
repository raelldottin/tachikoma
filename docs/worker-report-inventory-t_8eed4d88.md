# Swarm worker report: t_8eed4d88 — Task F demand/reservation domain

Lane: DOMAIN / RESERVATION IMPLEMENTATION (absorbed CRAFTING / DEPENDENCY and
ARCHITECTURE / UX INTEGRATION scope — both sibling lanes t_c7889e74 / t_62b699b8
gave up or were never claimed; this card carries the full Task F brief).
Swarm root / blackboard: `t_9d96a7cc`.

## 1. Repository baseline / HEAD

- HEAD: `2cb5aa4` (unchanged by this lane; no commits made).
- Dirty worktree at start preserved; unrelated modifications untouched
  (client.py, security.py, tui.py, battle/TUI/runtime-guard tests, uv.lock
  belong to earlier slices/lanes).

## 2. Files added

- `sdk/inventory/demand.py` — full Task F domain (replaces the dormant
  docstring-only stub left by the crashed run; the safety lane's dead-import
  cleanup is superseded by real code using real imports).
- `tests/test_inventory_demand.py` — 68 tests (all 30 brief points).

## 3. Files modified

- `sdk/inventory/__init__.py` — exports the Task F surface (DemandPlanner,
  PlanningContext, ReservationEntry, ReservationLedger, UnresolvedDemand,
  all condition-code constants).
- `sdk/commands.py` — `inventory plan` renders the demand/reservation
  ledger after the Task D classification; read-only GetShipByUserId
  fetch-if-absent (same transport method the `ship` command uses);
  `_inventory_strategic_reserve` config reader (explicit values only).
- `tests/test_inventory_plan_cli.py` — 3 assertions updated to the new
  contract (Task F now reported; Task G remains the prohibition).
- `README.template`, then `README.md` — inventory section updated
  (template first, per repo rule).

## 4. Demand sources supported

| Source | Basis | Quantity semantics |
|---|---|---|
| EQUIPMENT (current) | `Character/@ItemIds` on GetShipByUserId / ListAllUserDataFirst2 (discovery verdict: AUTHORITATIVE) | each distinct referenced ItemId claims its whole stack; repeated references dedupe to one claim + EQUIPMENT_SHARED_STACK condition (edge E1) |
| MODULE (current) | `Room/@ItemIds` (AUTHORITATIVE) | same whole-stack rule; duplicates within/across rooms dedupe + MODULE_MULTI_ATTACH (E2); both-equipped-and-attached claimed once under EQUIPMENT + conflict condition (E5) |
| STRATEGIC | explicit configuration ONLY (`settings["inventory"]["strategic_reserve"]`) | validated design keys, non-negative integer quantities, duplicates dropped entirely + condition; absent config = zero + SOURCE_UNSUPPORTED |
| CRAFT (capability) | `ItemDesign/@Ingredients` graph (902 live recipes), driven ONLY by explicit `crafting_objectives` (empty by default) | recursive expansion, gross-requirements semantics |

## 5. Demand sources intentionally unsupported

- TRAINING — runtime one-at-a-time flow; no plan/quantity representation
  exists (discovery verdict). Zero reservations + SOURCE_UNSUPPORTED.
- AMMUNITION — RebuildAmmo3 restocks server-side; no client-side quantity
  source. Zero reservations + SOURCE_UNSUPPORTED.
- STRATEGIC without config — no approved default exists anywhere; the
  policy doc's thresholds are explicitly "not wired into code yet".
- CRAFT without objectives — recipe graph is capability, not demand.
- EQUIPMENT/MODULE *planned* variants — no plan representation exists.

## 6. Reservation domain / API

- `ReservationEntry` (frozen): item_design_id, source, requested_quantity,
  reserved_quantity, reason, evidence, provenance.
- `ReservationLedger` (frozen): entries, conditions, observed_by_design;
  accessors entries_for / total_requested / total_reserved /
  observed_quantity / conditions_for / evidence_for / design_ids.
- `UnresolvedDemand` (frozen): code, detail, design_id (fail-closed
  condition; never a demotion).
- `PlanningContext`: equipped_references / room_references (ItemId ->
  reference COUNT, or None = unavailable), strategic_reserve,
  crafting_objectives; `from_ship_dict` parses any GetShipByUserId /
  ListAllUserDataFirst2-shaped dict (nested Character/Items echoes ignored,
  E4; error-shaped payloads are UNAVAILABLE, never "positively empty");
  `protection_context()` projects membership into Task D rule 1/4 sets.
- `DemandPlanner().plan(catalog, context) -> ReservationLedger` — pure.

## 7. Aggregation rules

- Demand aggregates by ItemDesignId; distinct ItemIds of one design sum
  (independent claims).
- Entries sort by (design id, source precedence, provenance); conditions
  sort by (code, detail, design) and dedupe.
- Allocation against observed supply in fixed precedence: EQUIPMENT,
  MODULE, CRAFT, TRAINING, AMMUNITION, STRATEGIC (current-state facts
  before planning intent before policy configuration).

## 8. Overlapping-demand rules

- Same physical stack, repeated references: ONE whole-stack claim
  (representation duplicate), ambiguity surfaced (EQUIPMENT_SHARED_STACK /
  MODULE_MULTI_ATTACH) — never double-counted.
- Same stack equipped AND room-attached: claimed once under EQUIPMENT
  (documented precedence) + BOTH_EQUIPPED_AND_ROOM_ATTACHED condition.
- Independent sources for one design (e.g. equipment + strategic): sum.
- Requests exceeding supply: capped per precedence + DEMAND_EXCEEDS_SUPPLY;
  the shortfall is exposed and is NEVER an acquisition, purchase,
  or reduction recommendation.
- Whole-stack claim rule: per-unit splits between multiple wearers of one
  stack are unobservable; over-claiming is the fail-closed direction for a
  protection ledger.

## 9. Crafting dependency behavior

- Driven only by explicit objectives (none exist in code/config today ->
  default runs produce zero craft reservations; capability retained).
- Single-level expansion, recursive multiplication through depth
  (3x 506 = 505:6, 501:12, 502:18, 504:3), shared-intermediate aggregation
  across objectives/depths (separate provenance rows per objective+depth),
  duplicate ingredient segments summed + condition (conservative
  over-claim), GROSS requirements (intermediates AND raw inputs claimed;
  netting held stock against upstream demand would under-claim and is
  Task-G-adjacent semantics this slice must not invent), objective for a
  non-recipe design surfaced (CRAFT_OBJECTIVE_NOT_CRAFTABLE).
- Deterministic traversal: sorted objective ids, sorted ingredient ids,
  fixed depth-first order.

## 10. Cycle / malformed-recipe behavior

- Cycles (synthetic; live graph is acyclic): edge cut + RECIPE_CYCLE
  condition; terminates, no partial demand from the cut edge.
- Unknown ingredient design: UNKNOWN_DESIGN, no demand recorded.
- Non-positive ingredient quantity (x0): edge dropped + MALFORMED_QUANTITY.
  Negative segments (x-1) never survive `parse_ingredients` (isdigit) —
  the design de-catalogs at parse (pre-existing Task B fail-closed), so the
  objective hits UNKNOWN_DESIGN.
- Pathological magnitudes: bound MAX_REASONABLE_QUANTITY (1e9) and
  MAX_RECIPE_DEPTH (64) with UNREASONABLE_QUANTITY conditions.

## 11. Evidence propagation

- Entry evidence = minimum across required inputs (Evidence.weakest).
- Feed rows + current-state references: CAPTURED_NOT_VERIFIED.
- Planning intent (strategic config, crafting objectives): INFERRED —
  strategic/craft entries never rise above INFERRED (tested).
- Design with no entries: evidence_for = UNKNOWN (fail closed).
- A reservation requiring UNKNOWN metadata reserves zero (fail closed).

## 12. Fail-closed behavior

- Unknown design referenced by equipped/room set: zero reservation +
  UNKNOWN_DESIGN; Task D protection of that stack is untouched.
- Reference to an ItemId absent from the catalog: UNKNOWN_ITEM_REFERENCE.
- Malformed @ItemIds tokens: parse condition; parseable tokens still count
  (dropping the list would under-claim).
- Negative inventory rows: counted as zero supply + MALFORMED_QUANTITY
  (can never reduce reservations).
- Malformed config keys/quantities, duplicate config records: dropped +
  conditions; duplicates dropped ENTIRELY (never first-wins/merged).
- Non-dict ship payload: sources UNAVAILABLE, never "positively empty".
- No context at all: both reference sources SOURCE_UNAVAILABLE.

## 13. CLI / inventory-plan changes

- `inventory plan` renders, after the unchanged Task D classification:
  per design (ascending): observed, total requested/reserved, evidence;
  per entry: source, requested/reserved, evidence, provenance, reason;
  per-design conditions inline; global conditions listed; fixed footer
  stating remaining quantity is NOT computed (Task G).
- `inventory` plain view footer updated (classification + demand via plan).
- `inventory apply` unchanged: unconditional refusal, config-independent
  (byte-identical under attacker-shaped settings; re-verified).
- Command description updated; no new verbs.

## 14. Tests added

- `tests/test_inventory_demand.py` — 68 tests across 10 classes covering
  all 30 brief points (determinism, equipment, module, crafting 4-10,
  quantity rules 11-12, overlapping demand 13-14, evidence 15-16,
  classification-vs-demand 17-19, ordering 20-21, unsupported sources,
  Task G / multi-account prohibition 22-24, apply refusal 25-26, purity
  28-29, CLI integration 21, protection independence).

## 15. Adversarial tests performed

- Roles-present + quantity-adversarial (0/1/1e9/1e18) catalogs with an
  empty planning context: zero reservations (D2-style mass-false-demand
  regression at the demand layer).
- 3-character shared stack: requested == stack quantity, not 3x.
- Equipped+room conflict: exactly one EQUIPMENT entry.
- Cycle 507<->508: terminates, RECIPE_CYCLE recorded.
- Duplicate config keys (int + str form): entries dropped entirely.
- Attacker-shaped settings (enabled/apply/force): apply refusal
  byte-identical; strategic junk shapes (None/5/str/list): no reservations,
  no behavior change.
- Socket-blocked planner run over all four source types: no network I/O.
- The safety lane's auto-discovering whole-package scan (mutation call
  patterns, network/config bypass, Client construction, import allowlist)
  now covers demand.py with zero changes to that file — passes.

## 16. Full validation results

- `make test` (canonical gate): 583 tests, OK (skipped=1). Pre-lane
  baseline was 515; +68 from this lane; no existing test weakened.
- `uv run ruff check` on all changed/new files: clean. (10 pre-existing
  F401s in untouched files — crew_leveling/ship_layout/test_game_formulas/
  test_ship_layout — belong to earlier dirty-worktree state and are left
  alone.)
- `git diff --check`: clean.
- Targeted: tests.test_inventory_demand 68 OK; inventory suite 236 OK;
  safety suite (auto-scan incl. demand.py) OK.

## 17-22. Prohibition confirmations

- Task G NOT implemented: no surplus/disposable/sale/consume/delete/
  transfer quantity, no reduction action, no `available = quantity -
  reservations` anywhere; ledger carries no such attribute (tested via
  hasattr reflection; conditions vocabulary banned from recommending).
- Multi-account NOT implemented: no account identity in the domain
  (source-scanned for account_id/AccountPortfolio/deficit_match/GiftItem).
- Mutation endpoints reachable from Task F code = 0 (safety-lane
  functional socket probe + source scan, now covering demand.py).
- Live destructive mutations attempted = 0 (read-only slice; sockets
  blocked in tests; MagicMock clients only).
- Starbux/premium currency spent = 0.
- `inventory apply` always refuses: unchanged stub; verified flag False;
  env-var and attacker-config independence re-tested.

## 23. Known limitations / deferred demand sources

- EQUIPMENT/MODULE demand is CURRENT STATE only (what is worn/attached
  today); no planned-equipment or planned-module source exists.
- Whole-stack claiming over-claims shared stacks by design (per-unit
  split unobservable) — surfaced per stack via conditions.
- Craft expansion is gross requirements (no netting of held intermediates).
- ListItemsOfAShip remains uncaptured (0 captures); inventory data still
  arrives via the client's existing fetch path.
- ListAllUserDataFirst2 is captured but not implemented in client.py; the
  plan uses GetShipByUserId (implemented) as the ship payload source.

## 24. Recommended next slice

Task G — dry-run surplus/reduction planning — as its own goal + verifier
gate, per the brief. It can consume ReservationLedger (observed, requested,
reserved, conditions, evidence, provenance are all exposed for audit).
Suggested pre-work: capture ListItemsOfAShip to promote feed evidence.
