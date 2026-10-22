# Task F swarm synthesis — inventory demand + reservation planning

Swarm root / blackboard: `t_9d96a7cc`. Verifier: `t_73ff6fd6` (gate=PASS, blackboard
comment "task-f-verifier-gate"). Synthesizer: `t_535bc48a` (this card).

This report consolidates the five lane handoffs (discovery, domain, safety, crafting
adversarial verification, architecture verification) plus the verifier gate into the
final Task F deliverable record. Every load-bearing claim below was re-verified by the
synthesizer against the working tree at HEAD `2cb5aa4` on 2026-09-26: the validation
ladder re-run, the CLI surface re-exercised with sanitized fixtures, adversarial
domain probes re-run, and the live 2026-08-08 MITM capture re-decoded and re-planned.
Where a claim rests only on a worker report and not on code or a fresh run, the report
says so.

Swarm narrative (for the record): the discovery lane (`t_b38f0330`) landed first and
staked the demand-source verdict table, four latent parse defects (D1-D4), and six
live-data edge cases (E1-E6) on the blackboard. The domain lane crashed once on
output-token limits, leaving a docstring-only `demand.py` stub; the safety lane
(`t_f2f61aba`) cleaned the stub's dead imports to keep lint green, applied the D1-D4
fixes to `sdk/inventory/designs.py` and locked them with regression tests, fixed a
pre-existing live-HTTPS leak in the battle suite (S-LIVE-1), and posted its handoff.
The domain lane's re-dispatch (`t_8eed4d88`) then landed the full Task F surface —
absorbing the crafting lane (`t_c7889e74`, twice stale-lock-reclaimed) and the
architecture lane (`t_62b699b8`, never claimed). The crafting lane's final run
adversarially verified the absorbed crafting implementation (30/30 probes, zero
defects, 9 regression tests added, no production change); the architecture lane's
final run independently verified the CLI integration and boundaries (0 files
touched). The verifier ran an independent gate against repository truth plus the real
capture and recorded PASS.

## 1. Repository baseline / HEAD

- HEAD `2cb5aa4` (mining drone slice), branch `main`. No commits were made by any
  lane; the entire Task F output lives in the dirty worktree alongside the earlier
  Slice 0/1 and Task D output, which remains uncommitted by design.
- Worktree state at synthesis: `sdk/inventory/` (8 modules, incl. new `demand.py`),
  six `tests/test_inventory*.py` files, inventory docs under `docs/`, plus modified
  `sdk/commands.py`, `sdk/client.py`, `sdk/security.py`, `sdk/tui.py`,
  `README.template`, `README.md`, `tests/test_battle_flow.py`,
  `tests/test_runtime_guards.py`, `tests/test_tui.py`, `uv.lock`. The client.py /
  security.py / tui.py modifications predate this swarm (earlier slices); no Task F
  lane disturbed them.
- Suite before Task F: 489 tests (post-Task D). Suite after Task F: 592 tests,
  1 pre-existing skip. 489 + 26 (safety lane) + 68 (domain lane) + 9 (crafting
  adversarial) = 592; no test was weakened or removed.

## 2. Files added

Production code:

- `sdk/inventory/demand.py` — the entire Task F domain (1,029 lines):
  `DemandPlanner`, `PlanningContext`, `ReservationEntry`, `ReservationLedger`,
  `UnresolvedDemand`, 14 condition-code constants, recursive recipe expansion.

Tests:

- `tests/test_inventory_demand.py` — 77 tests (68 from the domain lane covering all
  30 brief points; 9 adversarial graph tests from the crafting lane's verification
  run: `TestCraftAdversarialGraphs`).
- `tests/test_inventory_safety.py` — 26 tests (safety lane): classification/quantity
  never create reservations, D1-D4 regression locks, whole-package
  mutation/network/config-bypass auto-scan, apply-refusal config independence,
  zero-quantity placeholder inertness (E3).

Documentation:

- This report. (The discovery lane's full report lives in its task workspace,
  `t_b38f0330/demand-source-discovery-report.md`; the domain lane's report is
  `docs/worker-report-inventory-t_8eed4d88.md`.)

## 3. Files modified

- `sdk/inventory/designs.py` — D1/D2/D4 fixes from the safety lane's run:
  `parse_item_design` reads `@ItemDesignName` with `@ItemName` as legacy fallback
  (D1); `TrainingDesignId="0"` parses to `None` (D2); `from_client` reads
  `client.dynamicDesigns`, the attribute `Client.listAllDynamicDesigns` actually
  stores (D3); `Ingredients="0"` parses as absent rather than raising so designs
  544/780/786 stay cataloged (D4). Malformed non-"0" ingredient strings still
  raise and de-catalog (fail-closed direction preserved). Verified by the
  synthesizer against the live capture: 2557/2557 designs cataloged, 2557 non-empty
  names, exactly 90 nonzero TrainingDesignIds, 902 Ingredient rows of which 3 are
  the documented null markers.
- `sdk/inventory/__init__.py` — export additions only (Task F surface:
  `DemandPlanner`, `PlanningContext`, `ReservationEntry`, `ReservationLedger`,
  `UnresolvedDemand`, all condition codes). No pre-existing export altered.
- `sdk/commands.py` — `inventory plan` now renders the Task F demand/reservation
  ledger after the unchanged Task D classification; read-only GetShipByUserId
  fetch-if-absent (same transport method the `ship` command uses);
  `_inventory_strategic_reserve` config reader (explicit values only, absent
  config yields `{}`). `inventory apply` handler byte-identical.
- `tests/test_inventory_plan_cli.py` — 3 assertions updated to the Task F contract
  (the plan now reports demand; Task G remains the prohibition).
- `tests/test_battle_flow.py` — pre-existing live-traffic defect S-LIVE-1 fixed by
  the safety lane: `test_run_battle_proceeds_when_hp_full` made a real HTTPS call to
  api.pixelstarships.com (unmocked `acceptBattle5` plus continue-on-failure with
  dummy battleId). Now mocked with a call assertion. The safety lane proved the leak
  with a socket-block probe; no other LIVE-NET test exists in the suite.
- `tests/test_inventory_policy.py` — two unclosed `open()` ResourceWarnings fixed
  (they masked the SSL-leak detection).
- `README.template` — updated first, then `README.md` — identical inventory sections
  (synthesizer re-verified the demand/reservation paragraphs match verbatim).

## 4. Demand sources supported

| Source | Basis | Quantity semantics |
|---|---|---|
| EQUIPMENT (current state) | `Character/@ItemIds` on GetShipByUserId / ListAllUserDataFirst2 (discovery verdict: AUTHORITATIVE; live: 16 references over 11 distinct stacks) | each distinct referenced ItemId claims its whole stack; repeated references dedupe to one claim + `EQUIPMENT_SHARED_STACK` condition (edge E1) |
| MODULE (current state) | `Room/@ItemIds` (AUTHORITATIVE; live: 8 rooms carrying items, all broken-module variants) | same whole-stack rule; duplicates within/across rooms dedupe + `MODULE_MULTI_ATTACH` (E2); both-equipped-and-attached claimed once under EQUIPMENT + `BOTH_EQUIPPED_AND_ROOM_ATTACHED` (E5) |
| STRATEGIC | explicit configuration only (`settings["inventory"]["strategic_reserve"]`) | validated design keys and non-negative integer quantities; duplicate records dropped entirely + `DUPLICATE_CONFIG_ENTRY`; absent config = zero + `SOURCE_UNSUPPORTED` |
| CRAFT (capability) | `ItemDesign/@Ingredients` graph (899 live recipes after the 3 documented null markers), driven only by explicit `crafting_objectives` (empty by default) | recursive gross-requirements expansion; see §9 |

The synthesizer re-ran the planner over the real capture payloads: the live ship
yields 15 entries (11 EQUIPMENT + 4 MODULE), 3 shared-stack and 3 multi-attach
conditions, every reserved quantity <= observed, all current-state evidence
CAPTURED_NOT_VERIFIED, and zero referenced stacks missing Task D protection.

## 5. Demand sources intentionally unsupported

Each is surfaced as a `SOURCE_UNSUPPORTED` ledger condition on every plan — never
silently omitted, never invented (discovery verdicts, confirmed by the verifier
against live data):

- TRAINING — the only training flow is `manageTraining`/`useConsumable`, a runtime
  one-at-a-time consumable selection; no plan, quantity, or per-character
  requirement representation exists anywhere. The 90 live training-linked designs
  are classification facts, not demand.
- AMMUNITION — `RebuildAmmo3` sends only `ammoCategory`; the server decides what to
  restock. No client-side ammo quantity is ever computed or observed.
  `TargetManufactureString` is a gas/mineral production objective, not inventory
  demand.
- STRATEGIC without explicit config — `DEFAULT_INVENTORY_CONFIG` has zero code
  consumers; the policy doc's suggested thresholds are explicitly "not wired into
  code yet" and are not approved defaults.
- CRAFT without objectives — the recipe graph is cataloged capability, not demand.
- EQUIPMENT/MODULE planned variants — no equipment-plan or module-plan
  representation exists (WearItem/UnequipItems uncaptured; ship_layout.py is
  read-only analysis; all four room-module endpoints uncaptured).

## 6. Reservation domain / API

- `ReservationEntry` (frozen): item_design_id, source (ReservationSource),
  requested_quantity, reserved_quantity, reason, evidence, provenance.
- `ReservationLedger` (frozen): entries, conditions, observed_by_design; accessors
  entries_for / total_requested / total_reserved / observed_quantity /
  conditions_for / evidence_for / design_ids / total_reserved_all. Deliberately
  carries no surplus/disposable/reduction field.
- `UnresolvedDemand` (frozen): code, detail, design_id — a fail-closed condition,
  never a demotion and never a reduction authorization.
- `PlanningContext`: equipped_references / room_references (ItemId -> reference
  count, or None = unavailable), strategic_reserve, crafting_objectives,
  parse_conditions; `from_ship_dict` parses any GetShipByUserId /
  ListAllUserDataFirst2-shaped dict (nested Character/Items echoes ignored, E4;
  error-shaped payloads are UNAVAILABLE, never "positively empty");
  `protection_context()` projects membership into Task D rule 1/4 sets.
- `DemandPlanner().plan(catalog, context) -> ReservationLedger` — pure, no I/O.
  The only transport-layer symbol imported is `_extract_collection` (same one-way
  boundary as catalog.py / designs.py); `sdk/client.py` does not import
  `sdk.inventory` (re-verified), and `_extract_collection` remains at
  `sdk/client.py:50` (re-verified).

## 7. Aggregation rules

- Demand aggregates by ItemDesignId; distinct ItemIds of one design are
  independent claims and sum.
- Entries sort by (design id, source precedence, provenance); conditions sort by
  (code, detail, design) and dedupe.
- Allocation against observed supply uses fixed precedence EQUIPMENT, MODULE,
  CRAFT, TRAINING, AMMUNITION, STRATEGIC (current-state facts before planning
  intent before policy configuration). Unsupported sources keep a fixed position so
  the order is total.

## 8. Overlapping-demand rules

- Same stack, repeated references: ONE whole-stack claim (representation
  duplicate) + `EQUIPMENT_SHARED_STACK` / `MODULE_MULTI_ATTACH`. Never
  double-counted. The synthesizer re-verified live: the qty-5 stack referenced by
  3 wearers produces requested 5, not 15.
- Same stack equipped and room-attached: claimed once under EQUIPMENT
  (documented precedence) + `BOTH_EQUIPPED_AND_ROOM_ATTACHED`. Live sets are
  disjoint; the rule exists anyway.
- Independent sources for one design (equipment + strategic, or two craft
  objectives): separate entries that sum.
- Requests exceeding supply: per-entry reservations capped at supply in precedence
  order + `DEMAND_EXCEEDS_SUPPLY`. The shortfall is exposed as a condition and is
  never an acquisition, purchase, transfer or reduction recommendation (re-verified
  live: strategic request 10 on observed 7 reserves 7 under EQUIPMENT first).
- Whole-stack claiming over-claims shared stacks by design: the per-unit split
  between wearers is unobservable, and over-claiming is the fail-closed direction
  for a ledger whose only downstream consumer is protection.

## 9. Crafting dependency behavior

- Driven only by explicit objectives; none exist in code or config, so default
  runs produce zero craft reservations while the graph capability is retained.
  `PlanningContext.from_ship_dict` accepts objectives as a parameter for future
  slices; no settings key feeds them (architecture-lane note: deliberate,
  documented asymmetry, since no objective representation exists anywhere).
- Recursive expansion multiplies quantities through depth (synthesizer probe:
  objective A x3 with A=10xB, B=2xC yields B:30, C:60).
- Shared intermediates aggregate gross: diamond fan-in reaches one design via two
  branches and sums to ONE entry (probe: BASE reached from two paths at depth 2
  aggregates to 2, single entry, no double-count, no under-count).
- Cross-objective sharing produces distinct provenance rows that sum.
- GROSS requirements: intermediates AND raw inputs are both claimed. Netting held
  stock against upstream demand would under-claim and is Task-G-adjacent semantics
  this slice must not invent.
- Deterministic traversal: sorted objective ids, sorted ingredient ids, fixed
  depth-first order.
- Objective for a non-recipe design: `CRAFT_OBJECTIVE_NOT_CRAFTABLE` (a defect the
  domain lane found in its own first pass — an objective on a recipe-less design
  previously did nothing silently — now surfaced, regression-covered).
- Objective on an unknown design: `UNKNOWN_DESIGN`, zero reservations.

## 10. Cycle / malformed-recipe behavior

- Cycles (synthetic; the live graph is acyclic — 0 cycles confirmed by discovery,
  crafting lane, verifier, and the synthesizer's own DFS reproduction): the edge
  is cut + `RECIPE_CYCLE`; expansion terminates; pre-cycle demand is retained; no
  partial demand from the cut edge. Self-loops behave the same.
- Unknown ingredient design: `UNKNOWN_DESIGN`, no demand recorded.
- Non-positive ingredient quantity (x0): edge dropped + `MALFORMED_QUANTITY`.
  Negative segments never survive `parse_ingredients` (isdigit) — the design
  de-catalogs at parse, so the objective hits `UNKNOWN_DESIGN` (fail closed either
  way).
- Pathological magnitudes: `MAX_REASONABLE_QUANTITY` (1e9) and `MAX_RECIPE_DEPTH`
  (64) with `UNREASONABLE_QUANTITY`; an over-bound edge and its whole subtree are
  dropped, never partially demanded. The 100-node chain vs depth bound terminates
  and records demand exactly to the bound.

## 11. Evidence propagation

- Entry evidence = minimum across required inputs (`Evidence.weakest`).
- Feed rows + current-state references: CAPTURED_NOT_VERIFIED.
- Planning intent (strategic config, crafting objectives): INFERRED — never
  promoted (synthesizer probe: a design with EQUIPMENT CAPTURED_NOT_VERIFIED +
  STRATEGIC INFERRED entries has design-level evidence INFERRED).
- Design with no entries: evidence UNKNOWN (fail closed). A reservation requiring
  UNKNOWN metadata reserves zero.

## 12. Fail-closed behavior

- Unknown design referenced by an equipped/room set: zero reservation +
  `UNKNOWN_DESIGN`; Task D protection of that stack untouched (live-verified: 0
  referenced stacks missing protection; the unknown-design stack stays
  PROTECTED_UNKNOWN_DESIGN in the rendered plan).
- Reference to an ItemId absent from the catalog: `UNKNOWN_ITEM_REFERENCE`.
- Malformed @ItemIds tokens: parse condition; parseable tokens still count
  (dropping the list would under-claim).
- Negative inventory rows: counted as zero supply + `MALFORMED_QUANTITY`; can
  never reduce reservations.
- Malformed config keys/quantities and duplicate config records: dropped +
  conditions; duplicates dropped ENTIRELY (never first-wins, never merged) —
  synthesizer probe with `{"1": 5, 1: 7}` reserves zero and records
  `DUPLICATE_CONFIG_ENTRY` with both raw values.
- Non-dict ship payload: reference sources UNAVAILABLE, never "positively empty";
  a payload with no Character/Room nodes is likewise UNAVAILABLE.
- Classification or quantity alone never create reservations — synthesizer
  re-ran the D2-style adversarial catalog (roles on every design, quantities up
  to 1e18, explicitly empty context): zero entries, zero reserved.

## 13. CLI / inventory-plan changes

`inventory plan` renders, after the unchanged Task D classification: per design
(ascending ItemDesignId) the observed quantity, total requested/reserved, and
evidence; per entry the source, requested/reserved, evidence, provenance and
reason; per-design conditions inline; global conditions in a footer section; and a
fixed footer stating that remaining quantity is NOT computed (Task G) and that
nothing shown is a mutation recommendation. `inventory` plain-view footer updated
to point at plan. `inventory apply` unchanged: unconditional refusal,
config-independent (synthesizer re-ran it under attacker-shaped settings —
byte-identical refusal). No new verbs. Output is deterministic — the synthesizer
re-ran the full CLI twice with identical bytes.

## 14. Tests added

- `tests/test_inventory_demand.py` — 77 tests across 11 classes: determinism,
  equipment demand, module demand, crafting points 4-10 of the brief (single-level,
  recursion, multiplication, shared aggregation, duplicates, cycles, missing
  designs), quantity rules, unknown-design behavior, overlapping demand 13-14,
  evidence 15-16, classification-vs-demand 17-19, ordering 20-21, unsupported
  sources, Task G / multi-account prohibition 22-24, apply refusal 25-26, purity
  28-29, CLI integration, protection independence; plus
  `TestCraftAdversarialGraphs` (9 tests: diamond fan-in, self-loop, back-edge with
  pre-cycle retention, depth bound, magnitude subtree cut, nested duplicate,
  objective-on-intermediate, cross-objective provenance, multi-objective
  determinism).
- `tests/test_inventory_safety.py` — 26 tests, including a whole-package
  auto-discovering scan (`_inventory_sources` iterates `sdk/inventory/*.py`, so new
  modules are covered without registration) asserting no network imports, no
  env/config bypass reads, no mutation call patterns, no Client construction;
  D1-D4 regression locks with adversarial re-bug verification; classification and
  quantity never create reservations; E3 zero-quantity placeholders inert; apply
  refusal byte-identical under attacker-shaped config.

## 15. Adversarial tests performed

Across the swarm (each re-verified by the verifier against repository truth, and
the domain-level ones re-run by the synthesizer):

- Roles-present + quantity-adversarial catalogs (0/1/1e9/1e18) with empty planning
  context: zero reservations (the D2 live-risk regression at the demand layer).
- 3-character shared stack: requested == stack quantity, not 3x.
- Equipped+room conflict: exactly one EQUIPMENT entry.
- Cycle 507<->508 and self-loops: terminate, `RECIPE_CYCLE` recorded.
- Duplicate config keys (int + str form): dropped entirely.
- Attacker-shaped settings (enabled/apply/force): apply refusal byte-identical;
  strategic junk shapes (None/int/str/list): no reservations, no behavior change.
- Socket-blocked planner runs over all four source types: no network I/O.
- Crafting lane's 30-probe adversarial pass on the absorbed implementation
  (diamond fan-in, remote back-edge, 100-node chain vs depth bound, magnitude
  edge-drop, nested duplicates, objective-on-intermediate, cross-objective
  provenance, determinism, live-feed stress on all 899 craftable designs).
- Safety lane's injected-defect scans: an injected `activateItem3` call in
  demand.py is caught by the auto-scan; D1-D4 adversarial re-bugs fail the suite.
- Verifier's 15 adversarial disproof probes (classification/quantity create demand,
  shared-stack double-count, equipped+room double-claim, recipe over/under-count,
  shared-ingredient double-count, infinite cycles, unknown metadata lowering
  protection, weak evidence promoted, reservations creating surplus semantics,
  shortfall becoming acquisition semantics, duplicate config merge, negative rows
  reducing reservations, oversized objective partial demand, planner mutating
  inputs, cross-design contamination): all could not reproduce; zero defects.
- The synthesizer independently re-ran: diamond aggregation, deep multiplication,
  cycle/self-loop cuts, magnitude bounds, missing/malformed ingredients, duplicate
  and negative config, unavailable-vs-empty payloads, evidence minimums, the
  classification+quantity zero-demand probe, the 899-objective live-feed stress
  (3264 entries, deterministic, all quantities within bound), and the full live
  ship plan (15 entries, protection complete).

## 16. Full validation results

Re-run by the synthesizer on 2026-09-26 (matching the verifier's independent
re-run):

- `make test` (canonical gate): Ran 592 tests, OK (skipped=1).
- Targeted `tests.test_inventory_demand` + `tests.test_inventory_plan_cli`: 107 OK.
- All six inventory suites (test_inventory, classify, policy, safety, demand,
  plan_cli): 245 OK.
- `uv run ruff check` on all changed/new files: All checks passed.
- `git diff --check`: clean.

Note: bare `pytest` collection is broken by a pre-existing pytest-asyncio 0.23.0 vs
pytest 8.3.0 environment incompatibility; `make test` (unittest) is the canonical
gate, as established in earlier slices.

## 17. Task G was NOT implemented

- No `available = quantity - reservations` computation anywhere; the ledger has no
  surplus/disposable attribute (safety suite asserts via hasattr reflection).
- No sale/consume/delete/transfer quantity, no reduction action object, no
  reduction priority, no mutation planning, no transfer planning.
- `available_surplus` appears in production only as the always-None field on Task
  D's `Classification` (pre-existing contract, test-asserted) and in
  docstrings/tests asserting its absence. The `inventory plan` footer states
  remaining quantity is NOT computed; the "surplus" label appears nowhere.
- Shortfalls surface only as `DEMAND_EXCEEDS_SUPPLY` conditions with explicit
  "exposed only; never an acquisition or reduction recommendation" wording.

## 18. Multi-account planning was NOT implemented

No account identity, AccountPortfolio, account-deficit matching, GiftItem planning,
or marketplace transfer anywhere in the domain. The planner operates on one
catalog + one planning context. (Forbidden-term scan: production hits = 0.)

## 19. Mutation endpoints reachable = 0

- No Client method exists for any of the 17 forbidden endpoints (source-scanned).
- The safety suite's whole-package auto-scan fails on any mutation-call pattern in
  `sdk/inventory/`; an injected `activateItem3` call is caught.
- Functional socket-block probes over all four source types: no network I/O
  attempted from the domain.

## 20. Live destructive mutations attempted = 0

The slice is read-only; all tests use sanitized fixtures, MagicMock clients, and
blocked sockets. The one live-traffic defect found anywhere in the suite (S-LIVE-1,
battle flow) was fixed by the safety lane and is unrelated to inventory.

## 21. Starbux / premium currency spent = 0

No premium-currency endpoint is called by any Task F code or test.

## 22. `inventory apply` always refuses

The handler is a docstring plus a constant return: it reads no state and calls
nothing. The verifier proved via AST that the refusal is a compile-time constant —
byte-identical under attacker-shaped config and immune to environment variables.
No bypass exists; none can exist without editing the stub.

## 23. Known limitations / deferred demand sources

- EQUIPMENT/MODULE demand is current state only (what is worn/attached today); no
  planned-equipment or planned-module source exists to consume.
- Whole-stack claiming over-claims multi-wearer stacks by design (per-unit split
  unobservable); surfaced per stack via conditions.
- Craft expansion is gross requirements; no netting of held intermediates (netting
  is Task-G-adjacent and intentionally absent).
- `ListItemsOfAShip` — the endpoint InventoryCatalog is nominally built for — has
  zero captures in 49,774 records; inventory data still arrives via the client's
  existing fetch path (GetShipByUserId-shaped payloads), so inventory-feed
  evidence stays at CAPTURED_NOT_VERIFIED.
- `ListAllUserDataFirst2` is captured (12x) but not implemented in client.py; the
  plan uses GetShipByUserId (implemented). The domain parses both shapes
  identically, so wiring the second source is additive later.
- Crafting objectives are accepted by the domain API but deliberately not wired to
  any settings key (no objective representation exists in code or config; wiring
  one is future work).
- TrainingDesign and static-feed data (ListAllStaticDesigns2: 142 TrainingDesign
  rows) remain uncaptured in client usage; `findConsumableDesignId` still rests on
  the legacy uncaptured `listAllDesigns4` shape (pre-existing, out of scope).

## 24. Recommended next slice

Task G — dry-run surplus/reduction planning — under its own goal and verifier gate,
per the brief. It can consume `ReservationLedger` directly (observed, requested,
reserved, conditions, evidence, and provenance are all exposed for audit).
Recommended pre-work, carried from discovery, verifier, and this synthesis:
capture `ListItemsOfAShip` to promote inventory-feed evidence beyond
CAPTURED_NOT_VERIFIED before Task G treats observed quantities as authoritative.

---

## Swarm protocol record

- Machine-readable facts: in the completion metadata of this card.
- Cross-worker notes: posted to the swarm root `t_9d96a7cc` as a structured comment.
- Files changed by this synthesizer: this report only (no production code, no
  tests). All 592 tests re-run after writing it; see §16.
