# Task G swarm synthesis — dry-run inventory surplus/reduction planning

Swarm root / blackboard: `t_af901051`. Verifier: `t_3e8645cd` (gate=PASS, blackboard
comment "task-g-verifier-gate", completed with metadata `{"gate":"pass"}`).
Synthesizer: `t_46223d9d` (this card).

This report consolidates the five lane handoffs (evidence gate, adversarial
implementation, safety/policy verification, integration reconciliation, planner
re-verification) plus the verifier gate into the final Task G deliverable record.
Every load-bearing claim below was re-verified by the synthesizer against the
working tree at HEAD `2cb5aa4` on 2026-09-27: the full validation ladder re-run
(648 tests, Ruff on the touched surface, `git diff --check`), the 46-check
adversarial battery and the 16-check live-capture probe re-run from the
verifier's preserved scripts, the evidence-cap scan re-executed over all 6
capture files, and the live 2026-08-08 MITM capture re-decoded and re-planned
through the real public chain. Where a claim rests only on a worker report and
not on code or a fresh run, the report says so.

Swarm narrative (for the record): the evidence lane (`t_f4dd7bad`) landed first
and confirmed the inventory-feed evidence cap (0 ListItemsOfAShip captures in
50,755 records) before any Task G code existed. The planner lane (`t_14d15df9`)
crashed twice on model output-token limits before producing any work, leaving
its scope to be absorbed. The adversarial lane (`t_ed2a5694`) implemented the
entire Task G surface in `sdk/inventory/reduction.py` (441 lines at landing)
plus 40 adversarial tests, and found and fixed 2 defects in its own
implementation during review (per-design over-counting across sibling stacks;
imprecise reasons on zero-quantity rows carrying roles). The safety lane
(`t_5f8b64c3`) then proved the four mandated properties and, in doing so, found
and fixed 3 more real defects (unreachable `inventory apply` refusal from the
TUI; input-order-dependent pool allocation; structural proof gaps). The
integration lane (`t_d300c752`) reconciled the D/F/G CLI surface and found and
fixed 2 integration defects (stale Task F-era copy contradicting the rendered
Task G section; a defined-but-unexported reason code). The re-spawned planner
lane run added no code and instead independently re-verified the landed surface
with 15 fresh probes — all passing, no new defects. The verifier
(`t_3e8645cd`) ran an independent gate: a fresh 46-check adversarial battery,
a fresh 16-check live-capture reproduction, its own evidence-cap rescan, and
full reconciliation of all five lane handoffs against repository truth —
zero material discrepancies, gate=PASS. Net: **7 real defects were found and
fixed across the swarm** (2 adversarial, 3 safety, 2 integration), every one
with regression coverage.

## 1. Repository baseline / HEAD

- HEAD `2cb5aa4` (mining drone slice), branch `main`. No commits were made by
  any lane; the entire Task G output lives in the dirty worktree alongside the
  earlier Slice 0/1, Task D, and Task F output, which remains uncommitted by
  design.
- Worktree state at synthesis: `sdk/inventory/` (9 modules, incl. new
  `reduction.py`), seven `tests/test_inventory*.py` files, inventory docs under
  `docs/`, plus modified `sdk/commands.py`, `sdk/client.py`, `sdk/security.py`,
  `sdk/tui.py`, `README.template`, `README.md`, `tests/test_battle_flow.py`,
  `tests/test_runtime_guards.py`, `tests/test_tui.py`, `uv.lock`. The
  client.py / security.py modifications predate this swarm (earlier slices); no
  Task G lane disturbed them. The tui.py change is the swarm's SAF-G1 dispatch
  fix.
- Suite before Task G: 592 tests (post-Task F). Suite after Task G: 648 tests,
  1 pre-existing skip. 592 + 40 (adversarial lane) + 15 (safety lane) + 1
  (integration lane) = 648; no test was weakened or removed.

## 2. Files added

Production code:

- `sdk/inventory/reduction.py` — the entire Task G domain (455 lines after the
  safety lane's determinism fix; 441 at adversarial landing):
  `ReductionPlanner`, `ReductionPlan`, `ReductionResult`, 10 `BLOCKED_*` reason
  codes, `STATE_HOLD`/`STATE_CANDIDATE`, per-design unreserved-pool candidate
  allocation, weakest-input evidence propagation, and the disposition-hint
  mapping. Module docstring documents the full arithmetic, blocking rules,
  evidence, hints, and determinism contract.

Tests:

- `tests/test_inventory_reduction.py` — 41 tests across 10 classes:
  `TestArithmetic` (invariant property probe), `TestProtectionGating`,
  `TestEvidence`, `TestUncertaintyMonotonicity`, `TestUnsupportedDemand`,
  `TestDeterminism`, `TestNonActionability`, `TestMutationAndNetworkIsolation`,
  `TestSharedRefsAndConflicts`, `TestLiveShape`. (40 from the adversarial lane;
  1 export-consistency regression added by the integration lane.)

Documentation:

- This report. (The four lane reports are
  `docs/worker-report-task-g-evidence-t_f4dd7bad.md`,
  `docs/worker-report-task-g-planner-t_14d15df9.md`,
  `docs/worker-report-task-g-safety-t_5f8b64c3.md`,
  `docs/worker-report-task-g-integration-t_d300c752.md`; the adversarial
  lane's full handoff lives in its blackboard comment "task-g-adversarial-verify",
  which this synthesis consolidates. The verifier's probes are preserved in its
  scratch workspace: `task_g_verifier_probe.py` + `task_g_live_probe.py`.)

## 3. Files modified

- `sdk/inventory/__init__.py` — Task G export surface (all 10 `BLOCKED_*`
  reason codes, `ReductionPlanner/Plan/Result`, `STATE_HOLD/STATE_CANDIDATE`;
  `BLOCKED_ALLOCATION_EXHAUSTED` added by the integration lane after being
  defined-but-unexported) plus the package docstring scoped to remaining tasks
  H-L. No pre-existing export altered.
- `sdk/commands.py` — `inventory plan` renders the Task G dry-run section
  after the unchanged Task D classification and Task F ledger; the stale
  Task F-era copy corrected by the integration lane (header now "READ-ONLY
  Tasks D+F+G", disclaimer scopes the dry-run as informational, ledger caption
  points to the Task G section, plain-view footer scoped to H-L). `inventory
  apply` handler content byte-identical (docstring + constant REFUSED return;
  reads nothing).
- `sdk/tui.py` — SAF-G1: longest-registered-prefix dispatch in
  `TUI._command_loop`, so `inventory apply` reaches the registered refusal stub
  instead of being shadowed by the single-word `inventory` verb. Read-only
  dispatch resolution; no config/env surface added.
- `sdk/inventory/reduction.py` — SAF-G2: ascending-ItemId iteration enforced
  inside `ReductionPlanner.plan` (sorted at entry, not just at output), making
  pool allocation input-order independent and byte-identical across input
  permutations.
- `tests/test_inventory_reduction.py`, `tests/test_inventory_plan_cli.py`,
  `tests/test_inventory_demand.py`, `tests/test_inventory.py` — stale Task
  F-era pins updated to pin the corrected D+F+G contract (integration lane);
  +1 export-consistency regression (`test_every_reason_code_defined_is_exported`).
- `tests/test_inventory_safety.py` — new class `TestTaskGSafetyLan` (15 tests):
  structural proofs (actionable literal-False at construction site, frozen
  rows, no default-True anywhere, empty `actionable_rows` property body,
  value-type-only output, dry-run-section no-mutation-semantics scan,
  import-time network purity, per-surface refusal reachability, and the four
  SAF-G1 dispatch regressions: TUI `inventory apply` reaches the refusal
  handler, `help inventory apply` shows the unverified stub, `inventory plan`
  still reaches the plan, help resolution covers all registered verbs) plus
  the consolidated four-property battery (120 input permutations on P1).
- `tests/test_tui.py` — 2 existing registry tests updated (not added):
  `test_register_default_commands` now expects the registered `inventory` +
  `inventory apply` verbs; `test_is_verified` pins `inventory` verified=True
  and `inventory apply` verified=False.
- `README.template` — updated first, then `README.md` — inventory sections
  byte-identical (only the GitHub-hook header differs; synthesizer re-verified
  via plain diff). Documents the plan's Task G dry-run section and the
  longest-prefix dispatch contract for `inventory apply`.
- `INVENTORY_ENDPOINT_ANALYSIS.md` — evidence lane's full-corpus
  capture-coverage note: 0 ListItemsOfAShip hits in 50,755 records across all
  6 capture files (binary-safe, case-insensitive, prefix-variant-tolerant), so
  the inventory-feed evidence cap stands until a read-path capture exists.

## 4. The dry-run planning surface (what Task G computes)

`ReductionPlanner.plan(classified, ledger) -> ReductionPlan` — pure,
deterministic, fail-closed. One row per stack, ascending ItemId. Every row:

| Field | Meaning |
|---|---|
| `item_id`, `item_design_id` | stack identity |
| `observed_quantity` | ledger's observed total for the design (negative rows excluded) |
| `total_reserved` | ledger's reserved total for the design |
| `unreserved` | `max(0, observed - total_reserved)` — an arithmetic remainder ONLY; never a claim that units are reducible, sellable, or disposable |
| `candidate_quantity` | `0 <= candidate <= unreserved <= observed`, enforced (never trusted from inputs) |
| `state` | `HOLD` or `CANDIDATE` |
| `reasons` | sorted blocking reason codes (empty on CANDIDATE rows) |
| `evidence` | weakest input across feed rows (CAPTURED_NOT_VERIFIED), ledger evidence, classification evidence |
| `disposition_hint` | informational only, from the fixed future ladder |
| `actionable` | ALWAYS False — literal at the construction site, AST-proven |

### 4.1 Blocking precedence (candidate=0, first match wins)

`BLOCKED_MALFORMED_ROW` (negative quantity; the row is excluded from observed
supply) > `BLOCKED_PROTECTED_OR_UNKNOWN` (Task D fail-closed surface) >
`BLOCKED_OVER_RESERVED` (hand-built ledgers only; the Task F planner
structurally cannot emit reserved > observed) > `BLOCKED_DESIGN_CONDITION`
(design-scoped unresolved-demand conditions: UNKNOWN_DESIGN,
MALFORMED_QUANTITY, UNREASONABLE_QUANTITY, DEMAND_EXCEEDS_SUPPLY,
DUPLICATE_CONFIG_ENTRY, DUPLICATE_INGREDIENT, RECIPE_CYCLE,
CRAFT_OBJECTIVE_NOT_CRAFTABLE, SOURCE_UNAVAILABLE) > `BLOCKED_STACK_CONDITION`
(EQUIPMENT_SHARED_STACK / MODULE_MULTI_ATTACH /
BOTH_EQUIPPED_AND_ROOM_ATTACHED, scoped to the referenced ItemId) >
`BLOCKED_ZERO_QUANTITY` > `BLOCKED_ZERO_UNRESERVED` >
`BLOCKED_UNSUPPORTED_DEMAND` (applicable roles only) >
`BLOCKED_STRATEGIC_ROLE` — and for eligible rows whose design pool was
consumed by lower-ItemId siblings: `BLOCKED_ALLOCATION_EXHAUSTED`.

The precedence order is itself load-bearing and adversarially tested: the
adversarial lane initially emitted `BLOCKED_UNSUPPORTED_DEMAND` on
zero-quantity training-role rows, hiding the precise reason behind the role
reason; the reorder puts zero-quantity and zero-unreserved gates ahead of role
gates so the reason names the actual cause.

### 4.2 Unsupported demand applicability

TRAINING and AMMUNITION demand sources are unsupported this slice (Task F
verdict: no plan/quantity representation exists anywhere). Task F emits their
`SOURCE_UNSUPPORTED` conditions globally on every plan. Task G blocks ONLY the
designs where the unresolved demand APPLIES — i.e. designs carrying the
`CREW_TRAINING_MATERIAL` or `AMMUNITION_MATERIAL` role. Role-less designs are
never blocked by the bare global condition; strategic-role designs are decided
under STRATEGIC_RESERVE_CANDIDATE, not this rule. On the live ship this rule
blocks exactly 4 stacks (§8).

### 4.3 The strategic-role decision (design 90)

A stack carrying `STRATEGIC_RESERVE_CANDIDATE` with no config-backed
reservation is unresolved strategic intent: HOLD, candidate=0, hint KEEP, with
the strategic reason carrying the RESERVE intent. Both directions are
unit-tested: with a config-backed reservation the row is decided by the
zero-unreserved gate instead (`BLOCKED_ZERO_UNRESERVED`); without config it is
`BLOCKED_STRATEGIC_ROLE`. On the live ship design 90 (Half Day Immunity, qty 1,
unreserved 1, no config) is exactly this case — a HOLD, not a candidate.

### 4.4 Disposition hints

Hints name the fixed future ladder KEEP/RESERVE -> USE -> CRAFT -> ALLOCATE ->
TRANSFER -> SELL -> CONSUME -> DELETE but only the assignable subset exists:
the `Disposition` enum has exactly [KEEP, USE, CRAFT, ALLOCATE]. TRANSFER,
SELL, CONSUME, and DELETE have no member (RESERVE likewise carries no member;
its intent travels inside the strategic reason code), so constructing a
deferred hint is impossible — verified by direct ValueError probes. HOLD rows
hint KEEP; eligible equipment/module-role rows hint ALLOCATE; crafting-role
rows hint CRAFT; other eligible rows hint USE. An assertion at the
construction site guards the assignable set. Hints are informational only and
name no executor.

## 5. Arithmetic invariants (enforced, never trusted)

- `unreserved = max(0, observed - total_reserved)` per design.
- `0 <= candidate_quantity <= unreserved <= observed_quantity` on every row —
  enforced twice: by the gate chain above, and by the allocation arithmetic
  clamping candidate to `min(quantity, unreserved, pool)` with negative and
  over-cap results forced to 0.
- Candidates allocate against a per-design unreserved POOL in ascending
  ItemId order, so the per-design SUM of candidates never exceeds the
  design's unreserved. This closed the adversarial lane's first defect: a
  naive per-row `min(qty, unreserved)` summed to 2x unreserved across
  sibling stacks of one design. A stack whose pool share was consumed by
  lower-ItemId siblings is HOLD with `BLOCKED_ALLOCATION_EXHAUSTED`.
- Over-reserved ledgers (reserved > observed) fail closed: every stack of the
  design gets candidate=0 and `BLOCKED_OVER_RESERVED` — never a negative
  unreserved, never partial trust of an impossible ledger. The public Task F
  build path cannot produce such a ledger (allocation decrements from
  observed), so these tests hand-forge `ReservationEntry` objects.
- 300-trial (adversarial) + 200-trial (planner re-verification) + 300-trial
  (verifier) randomized fuzz across mixed blocking classes: zero violations.

## 6. Evidence propagation

Weakest-input, never promoted: a row's evidence is the minimum across the feed
rows (CAPTURED_NOT_VERIFIED), the design's ledger evidence, and the Task D
classification evidence. On the live plan, the row evidence set is exactly
{INFERRED, STATICALLY_DISCOVERED, UNKNOWN} — all weaker than
CAPTURED_NOT_VERIFIED, consistent with the cap. No row can reach VERIFIED
while `ListItemsOfAShip` remains uncaptured: 0 hits in 50,755 records across
all 6 capture files (re-scanned independently by the evidence lane, the
planner re-verification, the verifier, and this synthesizer). A hand-injected
VERIFIED entry does not promote a mixed design — verified by probe.

## 7. `inventory plan` integration

`inventory plan` (no new verb) renders three sections in order: Task D
classification, Task F demand/reservation ledger, Task G dry-run reduction
candidates. The integrated-output contract (integration lane, 10 checks,
synthesizer re-ran the verifier's end-to-end render): header names D+F+G;
disclaimer scopes the dry-run (every row informational, actionable=false,
hints name no executor, no row is a recommendation); ledger caption points to
the G section; dry-run section present; every row actionable False; deferred
disposition disclaimer present; refusal tail present; zero stale Task F-era
claims. `inventory apply` remains an unconditional refusal: a docstring plus a
constant REFUSED return that reads nothing — byte-identical under fully-armed
settings (inventory.enabled/apply/force/mutations all True) and armed env vars
(PSS_INVENTORY_* / INVENTORY_*), proven at the registry API, from the TUI
REPL (reachable after SAF-G1), and via `help inventory apply` (which shows the
unverified stub).

## 8. Live capture reproduction (read-only, no traffic)

Synthesizer re-ran the verifier's preserved 16-check probe and re-derived the
breakdown independently. Full public chain — raw ship XML ->
PlanningContext.from_ship_dict, raw designs XML (base64-gzip) ->
DesignCatalog.from_dynamic_dict, ship items -> InventoryCatalog ->
InventoryClassifier(context=protection_ctx).classify ->
DemandPlanner().plan -> ReductionPlanner().plan — on the real 2026-08-08
GetShipByUserId snapshot (ship 6917397) + ListAllDynamicDesigns (2,557
designs), read-only from disk:

- 356 stacks -> 356 rows, ascending ItemId.
- 259 rows `BLOCKED_PROTECTED_OR_UNKNOWN` (protected or unknown-design
  stacks).
- 76 rows carrying `BLOCKED_ZERO_QUANTITY` — the zero-quantity stacks that
  reach the arithmetic gates (the verifier's precision note: describe them as
  zero-quantity rows, never as "reserved-to-zero"). 251 of 356 live stacks are
  zero-quantity; the other 175 zero-quantity stacks are protected and counted
  within the 259 above. Arithmetically 266 rows have unreserved == 0: the 76
  zero-quantity rows above plus 190 protected rows (175 zero-quantity
  protected + 15 fully-reserved positive-quantity rows — exactly the 15
  ledger-referenced stacks, reserved == observed on all, blocked at the
  protection gate before the arithmetic gates).
- 4 rows `BLOCKED_UNSUPPORTED_DEMAND`, item ids exactly
  [181673752, 182315577, 184469427, 188673662] (designs 11 Ion Core,
  359 Repair Guide, 307 Espresso Shot, 702 Military Recruit Handbook — the
  complete applicable-role population on the live ship).
- 16 CANDIDATE rows on exactly the lane's eligible designs minus 90, plus
  design 90 (Half Day Immunity) as the 1 strategic-role HOLD = 17 eligible
  designs total (lane list membership confirmed; its ordering was cosmetic).
- Totals: observed 56,249; reserved 33; unreserved 56,216 — exact.
- Candidate sum 55,365 across the 16 rows; per-design candidate sums within
  per-design unreserved on all 17 designs; 0 invariant violations; 0
  actionable rows; disposition hints observed: 340 KEEP / 16 rows split USE 6
  / ALLOCATE 10; evidence tiers {INFERRED, STATICALLY_DISCOVERED, UNKNOWN}.

The 16 CANDIDATE rows include the two large resource stacks (Gas 25,000,
Mineral 30,310) — candidate magnitudes on the live surface span 1 to 30,310,
so nothing in the output assumes smallness.

## 9. Adversarial verification (independent re-checks)

The verifier's 46-check fresh battery (no lane-test reuse), re-run by the
synthesizer from the preserved script — all passing: invariant fuzz
(300-trial), over-reserved fail-closed, malformed/zero-quantity precedence,
unsupported-demand scoping (plain/training/strategic in one plan), protection
and unknown gating via the real classifier chain, evidence cap including
VERIFIED-entry non-promotion, disposition unconstructibility, hints,
ascending-ItemId pool allocation + `BLOCKED_ALLOCATION_EXHAUSTED`,
24-permutation + 5-run byte-identity determinism, design/stack condition
scoping (all 3 stack codes), mutation isolation, source-token network scan,
apply refusal under armed settings AND armed env, TUI + help dispatch
reachability, AST actionable=False proof, export consistency, and uncertainty
monotonicity. The verifier's investigation of its own 4 initial probe failures
found them all to be probe-fixture errors (wrong constructor, hand-built
impossible ClassifiedItem, wrong expected arithmetic, over-rigid string
match) — each investigated, each confirmed production correct, documented in
blackboard comment 42.

## 10. Determinism

Identical inputs produce byte-identical output. Rows sort ascending ItemId;
pool allocation iterates ascending ItemId (enforced at entry since SAF-G2, so
input order is irrelevant); every list is sorted before construction; no
wall-clock, randomness, dict-order, or set-order dependence. Proven: 120 input
permutations byte-identical (planner lane), 24 permutations + 5 fresh planner
instances with one distinct sha256 of pickled `to_dicts` output (verifier),
2-stack reverse-order case (shipped regression).

## 11. Safety properties (the four mandated properties)

- P1 non-actionability: `actionable=False` on every row across 120 input
  permutations; `plan.actionable_rows == ()` always; the invariant chain holds
  on every row; evidence never promoted; hints drawn only from the assignable
  set.
- P2 mutation isolation: planner leaves catalog items, classified sequence,
  and ledger entries byte-identical; output input-order independent.
- P3 no network/live traffic: full plan path under blocked
  `socket.socket`/`create_connection`; fresh-interpreter import with socket
  construction blocked; package-wide source scan (no socket/ssl/urllib/http/
  requests/httpx imports, no `Client(` construction, no `.request(` call, no
  embedded URL). AST scan of `sdk/inventory`: zero banned imports/calls.
- P4 unconditional apply refusal: byte-identical REFUSED from every dispatch
  surface under fully-armed settings and env.

## 12. Prohibition verification (no leakage)

- No multi-account planning: no account/userId dimension anywhere in
  `sdk/inventory` (AST + source scan).
- No marketplace transactions: no MarketService/SellItem/Purchase tokens
  outside docstring prose; the DEFAULT_INVENTORY_CONFIG marketplace gate is
  untouched.
- No mutation executor/reconciler: none exists; the refusal stub is unchanged
  in content.
- No endpoint enablement: no config/env surface added; the apply handler reads
  nothing. (`activateItem3`/`rebuildAmmo` exist on the legacy Client from
  pre-inventory commits a0c7b0c/4b71fc8, are not forbidden-endpoint additions,
  and nothing in `sdk/inventory` references them — verifier note.)
- No network I/O in `sdk/inventory`: AST-verified.
- Evidence cap preserved: feed-derived facts stay CAPTURED_NOT_VERIFIED.

## 13. Tests added / modified in Task G

- `tests/test_inventory_reduction.py`: 41 tests, 10 classes (listed in §2).
- `tests/test_inventory_safety.py`: +15 (`TestTaskGSafetyLan`, including the
  four SAF-G1 dispatch regressions).
- `tests/test_tui.py`: 2 existing registry tests updated to pin the
  `inventory` + `inventory apply` registration and verified flags (no test
  added or removed; suite total 20).
- `tests/test_inventory_plan_cli.py` / `test_inventory_demand.py` /
  `test_inventory.py`: stale pins updated to the corrected D+F+G contract.
- Adversarial coverage spans every mandated area: arithmetic (invariant
  property probe, pool splits across reservation levels), protection,
  uncertainty monotonicity (incl. per-condition probes), unsupported demand
  (applicability + strategic both directions), shared refs/conflicts,
  evidence (weakest-input, never-VERIFIED cap), determinism (incl. input-order
  independence), mutation isolation, network isolation (socket-block +
  source-token scan + import scan), and apply refusal (config/env independence
  + per-surface reachability + AST constant proof).

## 14. Full validation results

Re-run by the synthesizer on 2026-09-27 (matching the verifier's independent
re-run):

- `make test` (canonical gate): Ran 648 tests, OK (skipped=1).
- Per-suite counts (synthesizer's own run): test_inventory 36 + classify 23 +
  policy 53 + safety 41 (26 baseline + 15 Task G) + demand 77 + reduction 41 +
  plan_cli 30 = 301 inventory tests; +20 TUI = 321 — matching the verifier's
  and integration lane's targeted totals exactly.
- `uv run ruff check` on all changed/new files: All checks passed. Repo-wide
  lint: 11 pre-existing F401s in crew_leveling/ship_layout/game_formulas/battle
  scripts (committed 632fba6/30c73f0/8a553c34) inside `make lint` scope, plus
  23 in `pss_langgraph_automation.py` (committed 233aa61, outside `make lint`
  scope) — none touched by the swarm; no swarm-introduced lint debt.
- `git diff --check`: clean.
- Live-capture probe: 16/16 PASS (verifier's preserved script, synthesizer
  re-run).
- Adversarial battery: 46/46 PASS (verifier's preserved script, synthesizer
  re-run).
- Evidence-cap scan: 0 ListItemsOfAShip hits in 50,755 records across all 6
  capture files (synthesizer's own scan; reconciles exactly with the
  evidence lane and verifier).
- README parity: README.template vs README.md differ only by the GitHub-hook
  template header; inventory sections byte-identical.

Note: bare `pytest` collection is broken by a pre-existing pytest-asyncio
0.23.0 vs pytest 8.3.0 environment incompatibility; `make test` (unittest) is
the canonical gate, as established in earlier slices.

## 15. Defects found and fixed during the swarm

All seven defects were found by lanes reviewing landed code (their own or
each other's), each with regression coverage, each independently confirmed by
the verifier and reproduced in §7/§8/§9 by the synthesizer:

1. ADV-1 (adversarial, fixed in-lane): per-row `min(qty, unreserved)` could
   sum to 2x unreserved across sibling stacks of one design. Fix: per-design
   pool allocation in ascending ItemId order + `BLOCKED_ALLOCATION_EXHAUSTED`
   + property test over 5 stack splits x all reservation levels.
2. ADV-2 (adversarial, fixed in-lane): zero-quantity rows carrying
   training/ammo roles were tagged `BLOCKED_UNSUPPORTED_DEMAND` instead of
   the precise zero-quantity reason. Fix: reason-precedence reorder —
   zero-qty and zero-unreserved gates fire before role gates.
3. SAF-G1 (safety): the TUI tried the single-word verb first, so `inventory`
   always shadowed `inventory apply`; the refusal stub was unreachable from
   the REPL and `help inventory apply` displayed the verified `inventory`
   verb. Fix: longest-registered-prefix resolution in both dispatch sites.
   Both old behaviors were fail-closed but misrepresenting.
4. SAF-G2 (safety): pool allocation iterated in caller-input order; identical
   inputs in different order produced different allocation, violating the
   documented ascending-ItemId contract. Invisible to prior tests because
   InventoryCatalog pre-sorts. Fix: planner enforces ascending-ItemId
   iteration itself.
5. SAF-G3 (safety): proof gaps — no AST proof pinned actionable=False at the
   construction site, the `actionable_rows` property body, import-time
   network purity, or per-surface refusal reachability. Fix: structural
   proofs added (all shipped in TestTaskGSafetyLan).
6. INT-G1 (integration): stale Task F-era plan copy contradicting the
   rendered Task G section (header "no dispositions", "not available in this
   slice (Task G)", "remaining quantity NOT computed here", package
   docstring "reduction remains absent"). Fix: all copy corrected; 3 stale
   test pins updated to pin the corrected contract.
7. INT-G2 (integration): `BLOCKED_ALLOCATION_EXHAUSTED` defined and emitted
   but absent from package exports (9 of 10 codes exported). Fix: exported;
   `test_every_reason_code_defined_is_exported` regression added.

## 15a. Discrepancies investigated and resolved (not defects)

- The adversarial lane's live probe initially reported "35 blocked" vs the
  evidence lane's 4: cause was the ADV-2 reason-precedence defect, fixed
  in-lane (see #2).
- The verifier's 4 initial probe failures: all probe-fixture errors (wrong
  constructor name, hand-built impossible ClassifiedItem, wrong expected
  arithmetic, over-rigid string match), each investigated and confirmed
  production-correct, documented in blackboard comment 42.
- The planner re-verification's 2 probe discrepancies: both probe-fixture
  errors (empty rarity correctly tripped PROTECTED_INSUFFICIENT_EVIDENCE;
  negative row mixed into a clean design correctly tripped design-scoped
  MALFORMED_QUANTITY), documented in its lane report.
- Lane attachment id 6 (22-byte placeholder) on the planner card: self-
  disclosed by that lane; the real probe is attachment id 7 (11,027 bytes);
  the verifier confirmed no action needed.
- The evidence lane's eligible-design list `[2, 3, 64, ..., 733, 78]` was
  unsorted; membership (not order) was the claim, and membership reconciles
  exactly with the live reproduction.

## 16. Tasks H-L remain NOT implemented

Per the brief, Task G stops after G: no transfer/marketplace planning, no
sell/consume/delete quantities, no reduction executor or reconciler, no
end-state target representation, no report/export formatting beyond the
rendered plan, no multi-account planning, no endpoint enablement. The
`Disposition` members that would name those semantics do not exist, so no
code path can assign them. `available_surplus` remains the always-None Task D
`Classification` field, and the plan footer scopes remaining work to H-L.

## 17. Hotspot note for the next slice

`sdk/commands.py` was touched by three lanes in this swarm (adversarial,
safety, integration) and `tests/test_inventory_reduction.py` +
`tests/test_inventory_plan_cli.py` by two to three. Before the next
inventory-CLI slice, split the plan renderer out of `CommandRegistry` so
lanes stop colliding in one file.

## 18. Recommended next step

Capture `ListItemsOfAShip` (or an equivalent inventory read path) during
normal play, and re-run the full Task G validation ladder. That single
capture promotes inventory-feed evidence beyond CAPTURED_NOT_VERIFIED and
makes observed quantities checksum-verified inputs. Until then, every dry-run
row correctly stays below the cap. Task H (per the implementation plan's
lettering, the next lettered task after G) should start from this synthesis
plus `docs/swarm-synthesis-task-f-t_535bc48a.md`.

## 19. Machine-readable facts

- reduction_module: sdk/inventory/reduction.py, 455 lines
- exports: ReductionPlanner/ReductionPlan/ReductionResult, 10 BLOCKED_* codes,
  STATE_HOLD/STATE_CANDIDATE
- disposition_members: [KEEP, USE, CRAFT, ALLOCATE]; TRANSFER/SELL/CONSUME/
  DELETE (and RESERVE) unconstructible
- blocking_precedence: malformed > protected/unknown > over-reserved >
  design-condition > stack-condition > zero-qty > zero-unreserved >
  unsupported-role > strategic-role > allocation-exhausted
- live_surface: 356 stacks / 259 protected-unknown / 76 zero-qty (all with
  BLOCKED_ZERO_QUANTITY) / 4 unsupported-blocked [181673752, 182315577,
  184469427, 188673662] / 16 CANDIDATE + 1 strategic HOLD (design 90) /
  observed 56,249 / reserved 33 / unreserved 56,216 / candidate sum 55,365 /
  hints 340 KEEP + 16 (6 USE + 10 ALLOCATE) / evidence
  {INFERRED, STATICALLY_DISCOVERED, UNKNOWN} / 0 actionable
- evidence_cap: 0 ListItemsOfAShip in 50,755 records across 6 capture files
  (independently re-scanned by 4 parties)
- validation: make test 648 OK skipped=1; ruff clean on touched surface;
  git diff --check clean; live probe 16/16; adversarial battery 46/46
- test_ladder: 592 (post-F) + 40 (adversarial) + 15 (safety) + 1
  (integration) = 648
- defects_fixed_in_swarm: 7 (2 adversarial, 3 safety, 2 integration), all
  with regression coverage
- prohibitions_held: no multi-account, no marketplace, no mutation executor,
  no endpoint enablement, no network I/O in sdk/inventory, apply refusal
  unconditional

---

## Swarm protocol record

- Machine-readable facts: in the completion metadata of this card.
- Cross-worker notes: posted to the swarm root `t_af901051` as a structured
  comment.
- Files changed by this synthesizer: this report only (no production code, no
  tests). The full suite (648 OK, skipped=1), ruff on the touched surface,
  and `git diff --check` were re-run after writing it; see §14.
