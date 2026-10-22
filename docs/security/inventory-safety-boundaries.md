# Inventory Safety Boundaries — Enforcement Design

Planning task: `t_d5626222` (swarm root `t_b9080d7a`, safety-boundaries lane).
Companion documents (already delivered by sibling lanes):

- `docs/inventory-implementation-plan.md` — analyst lane; full 22-section plan,
  §12 CLI plan, §13/§14 endpoint readiness, §17/§20 reconciliation gates backlog.
- `docs/architecture-addendum-inventory-t_71d8b652.md` — architect lane; module
  boundaries, internal APIs, determinism rules R1–R5, config schema, dry-run
  format, audit contract, open questions Q1–Q5.
- `docs/security/protected-item-policy.md` — policy lane `t_a20d45b8`; the 10
  protected rules, H2/H3 hazard lists, per-run cap defaults.
- `docs/security/mutation-safety-analysis.md` — sibling safety lane
  `t_9eec6b78`; hazard classes and the six cross-cutting mutation-safety rules.

Status: PLAN ONLY. No production code changed by this lane. No live mutations.

Per the architect lane's handoff contract (addendum §13), this document's job
is to operationalize the reconciliation contract sketched in the analyst plan
§17/§20 and the policy lane §4 — i.e. turn "snapshot → mutate → refetch →
verify → abort" and the safety rules above into enforceable, test-assertable
boundaries with concrete acceptance gates. It deliberately does NOT re-derive
the endpoint readiness matrix (analyst §14 stands; gates are cited, not
re-listed) or re-specify module layout (architect addendum stands).

---

## 1. Safety invariants (binding on every inventory implementation slice)

Each invariant is named because acceptance tests in §5 reference them by name.
They are ordered: S1 outranks everything beneath it.

**S1. No live mutation path exists until Phase 9.**
No code merged before a Phase-9, separately-authorized slice may contain a
reachable call chain from any entry point (`run.py`, TUI command registry,
any SDK public method) to an item/state mutation endpoint. "Reachable" is
proven by a static scan test (T10, §5) that greps for the mutation URL
templates outside the tests/ tree and asserts they occur only inside
hard-disabled executor scaffolding. The executor scaffolding itself raises on
construction when `enabled` is False (architect addendum §3), so even a
wiring mistake fails loud at startup, not at call time.

**S2. `inventory apply` always refuses until its slice lands.**
The verb registers in `CommandRegistry` as `verified=False` and its handler
raises/returns a refusal naming the blocking phase. No configuration value,
CLI flag, environment variable, or test helper may flip this. The ONLY way
the refusal disappears is deletion of the stub in the same commit that ships
the real, capture-backed executor path. This is deliberately the opposite of
a feature flag: a flag is a thing someone flips; a stub is a thing someone
must re-implement.

**S3. Unknown state resolves to PROTECTED, never to disposal.**
Unknown `ItemDesignId`, unreadable inventory, malformed recipe graph, cycle
in the craft DAG, ambiguous server response — all funnel to PROTECTED with a
logged reason. The classifier must not contain a code path in which
insufficient evidence produces SELLABLE/CONSUMABLE/DISCARDABLE. Corollary:
protection checks short-circuit BEFORE bucket classification, so a protection
rule cannot be bypassed by a bucket mis-mapping.

**S4. No silent state repair.**
Every guard refusal is logged (audit JSONL, architect §8) and surfaced to the
caller. No fallback path may "fix" input to make a guard pass — e.g. if a
recipe is ambiguous, the planner marks the design PROTECTED; it does not
substitute another recipe.

**S5. Budget caps are per-run and live next to the call site.**
`PSS_MAX_STARBUX_PER_RUN` (default 0) applies to every endpoint that can
spend premium currency — including the four pre-existing autonomous endpoints
flagged by the safety lane (SpeedUpTravelling, CharacterService/Draw,
AddStarbux2, RebuildAmmo3). Those four are out of inventory scope but are
listed in §6 boundary B5 so the first inventory slice cannot accidentally
make them worse (see §4 RT-6).

**S6. Allow-listed autonomy.**
The autonomous loop (`run.py`) may only invoke endpoints on an explicit
allow-list. Inventory work adds NO entry to that list until Phase 9. The
allow-list is deny-by-default: anything not named is manual/CLI-only.

**S7. One mutation per reconciliation unit.**
When mutations are eventually enabled, each mutation is wrapped: snapshot →
exactly one mutation → refetch → verify → audit. Never batch. Never fire two
mutations against the same re-read.

## 2. Reconciliation Requirements Specification

This section is the operationalization of analyst §17/§20 and policy-lane §4.
It pins the contract that Phase 8 (executor/reconcile fakes-only slice) must
implement and that every Phase 9 enablement slice must pass.

### 2.1 The reconciliation unit

```
ReconUnit(endpoint, item_id, item_design_id, expected_delta, params_digest)
```

A reconciler executes exactly this sequence:

1. `before = snapshot(ship_id)` — refetch `ListItemsOfAShip`, capture the full
   typed catalog plus a digest of the raw response.
2. `dispatch(endpoint, params)` — one HTTP call, no retry.
3. `after = snapshot(ship_id)` — refetch.
4. `verify(before, after, expected_delta)` — raises `ReconciliationAbort` on
   any of the abort conditions below.
5. Write audit row (`reconcile_ok` or `reconcile_abort`); on abort, the run
   terminates — no further planned actions dispatch.

### 2.2 Abort conditions (each is a fakes-only test)

A1. Target item disappears unexpectedly (item_id absent in `after`).
A2. Target quantity changes by an amount different from `expected_delta`.
A3. Any UNRELATED `item_id` changes quantity or disappears between before and
    after (server-side race, another client, or a bug in our params).
A4. Response is ambiguous: HTTP 2xx with an error body, 5xx, schema drift
    (missing attributes the parser requires), or empty body.
A5. Auth state changes mid-flight (accessToken/device rejection on refetch).
A6. Inventory cannot be re-read after the mutation (network or parse failure).
A7. Server reports a quantity for the stack that contradicts the mutation's
    own response payload (planner/server disagreement).

On any abort: STOP the run, keep the audit row, and require human review
before the next mutation attempt. Reconciliation NEVER retries the mutation.

### 2.3 Test evidence required per abort condition

For each of A1–A7: a fake catalog pair (before/after fixtures) constructed to
trigger exactly that condition, asserting `ReconciliationAbort` with the
expected reason code, and asserting the audit row was written. Phase 8's task
(Task J in the analyst plan) is not complete until all seven pass.

## 3. Adversarial test specification

Beyond the analyst plan §15 lists, the following negative cases are mandatory
because they target the failure modes most likely to destroy user items:

| ID | Case | Assertion |
|----|------|-----------|
| AD-1 | Inventory contains an `ItemDesignId` absent from both design feeds | Classified PROTECTED("unknown design"); planner issues no action |
| AD-2 | Craft recipe graph contains a cycle (A needs B, B needs A) | Both designs PROTECTED; planner logs cycle; no crash, no partial demand |
| AD-3 | Malformed `@Ingredients` string (`"abc"`, `"81x"`, `""`) | Design treated as PROTECTED with reason naming the parse failure; run continues |
| AD-4 | Duplicate `ItemId` rows in ListItemsOfAShip response | Catalog raises/normalizes deterministically; never double-counts surplus |
| AD-5 | Item with quantity 0 retained in response | Counted as zero surplus; never proposed for any disposition |
| AD-6 | Equipped item whose design feed row is missing | PROTECTED via BOTH rule 1 (equipped) and rule 10 (unknown); short-circuit order proof |
| AD-7 | Planner run with identical inputs executed twice | Byte-identical ordered plan output (determinism R1) |
| AD-8 | Config with all `[inventory]` keys absent | Identical behavior to config-with-defaults (safe-by-default proof) |
| AD-9 | Attempt to construct `InventoryExecutor(enabled=True)` in a test that has not explicitly opted into executor tests | Test harness forbids; production default is `enabled=False` |
| AD-10 | `inventory apply` invoked with every arg shape (`--force`, `--yes`, env var set) | Always refuses; refusal message names blocking phase |
| AD-11 | Marketplace transfer proposal where the only listing for a design is ambiguous (multiple sellers) | TransferPlanner emits NO action; the "never buy first matching listing" rule is structural, not a filter |
| AD-12 | Account snapshot with version skew (old schema) | Portfolio refuses to load; logs schema version; does not guess fields |

## 4. Regression tests for known defects (analyst §1 corrections)

Each corrected defect gets one named regression test so a re-version of the
docs or code cannot silently resurrect the wrong claim:

- RT-1 `test_consume_item2_checksum_formula` — MD5(clientDateTime+itemId+
  quantity+characterId+CK+SC) against the 3 captured samples.
- RT-2 `test_mark_items_not_new_checksum` — preimage `clientDateTime+
  accessToken` against the captured sample.
- RT-3 `test_combine_item4_capture_recorded` — fixture/docs assertion that
  the 2026-08-01 empty-itemIds capture is reflected in the endpoint doc.
- RT-4 `test_item_node_six_attributes` — parser fixture asserting exactly the
  6 observed attributes are consumed and extras land in `raw`.
- RT-5 `test_dynamic_feed_is_itemdesign_source` — DesignCatalog resolve order
  prefers `ListAllDynamicDesigns`; static branch never consulted for
  ItemDesign rows.
- RT-6 `test_starbux_cap_unchanged_surface` — the inventory slice diff
  contains no edits to `SpeedUpTravelling`, `purchaseDrawWithStarbux`,
  `AddStarbux2`, `RebuildAmmo3` call sites (prevents a planning slice from
  quietly widening premium-currency exposure).

## 5. Acceptance gates per implementation slice

Derived from the analyst task list; this section is what a reviewer uses to
say "this slice may merge".

### Gate for Slice 0 (Task A — docs corrections)
- All 5 corrections present in `INVENTORY_ENDPOINT_ANALYSIS.md`.
- Checksum-endpoint count recomputed and internally consistent.
- Zero code files touched; `git diff --name-only` whitelist check.

### Gate for Slice 1 (Tasks B+C+E-minimal — domain + first CLI verbs)
- `make test` passes; no live network (asserted by the synthetic-fixture
  harness pattern — tests never import `requests`).
- T10 static scan clean: no mutation URL template appears in production code
  outside the disabled executor scaffold.
- AD-1, AD-4, AD-5, AD-7, AD-8, AD-10 pass.
- RT-1, RT-2, RT-4, RT-5 pass.
- `inventory` and `inventory plan` produce output; `inventory apply` refuses
  with a message naming Phase 8/9 (S2).
- `sdk/client.py` diff contains ONLY the `listAllDynamicDesigns` method
  (architect §2); `run.py` untouched (no new autonomous path, S6).
- README.template updated before README.md (repo rule).

### Gate for Phase 2 slice (Task D — classifier + protection)
- S3 structural proof: protection short-circuit precedes bucket assignment
  (asserted by a test feeding an item that is simultaneously rare AND in a
  surplus-eligible bucket → PROTECTED).
- AD-1, AD-6 pass; policy rules 1–10 each have one positive and one negative
  test.
- Determinism R1–R5 tests pass.

### Gate for Phase 4 slice (Task G — reduction planner)
- Only positive `available_surplus` rows receive dispositions 5–8
  (property test over randomized fixture catalogs).
- AD-2, AD-3, AD-7 pass.
- Dry-run output includes `config_digest` + input digests (replayability).

### Gate for Phase 8 slice (Task J — executor/reconciler, fakes only)
- A1–A7 fakes tests all pass.
- `InventoryExecutor()` with defaults raises on first dispatch (S1).
- No test in this slice performs real HTTP; harness-level proof (test run
  with network disabled — the repo pattern is mocked fixtures).
- AD-9, AD-10 pass.

### Gate for ANY Phase 9 slice (Task L — per-endpoint enablement)
ALL of the following, or the slice does not merge:
- Endpoint gate in the readiness matrix is SAFE-after-capture: capture
  exists, checksum reproduced from capture, response schema documented.
- Reconciliation unit for that endpoint passes A1–A7 fakes.
- Budget cap applies if the endpoint spends premium currency (S5).
- Audit rows written for both success and abort paths.
- Slice is a single endpoint per PR, authorized explicitly, and the dry-run
  planner has been able to emit that endpoint's PlannedAction for at least
  one full dry-run cycle before enablement.

## 6. Mutation-disabled backlog — safety annotations

Analyst §20's backlog stands. Safety additions per blocked item:

| Endpoint | Safety-specific requirement beyond §20 |
|----------|------------------------------------------|
| DeleteItem3 | DISABLED_H3. Additionally: response `remainingQuantity` semantics unknown — reconciliation cannot yet distinguish "deleted one of a stack" from "deleted the stack". Blocked on capture, semantic verify, AND a reconciler delta-spec that can express both. |
| CombineItem4 | Ambiguous parameter behavior in the one capture. Safety rule: until a capture with non-empty `itemIds` proves which items dissolve, the planner may not even PROPOSE combine actions (AD-3 conservatism) — combine rows are emitted as CRAFT proposals without endpoints, gate=NEEDS_CAPTURE. |
| SellItem3/Purchase4 | Dual-account reconciliation required: after marketplace transfer, BOTH accounts' inventories re-verified. Never buy the first listing matching an ItemDesignId — the exact-sale identification rule (AD-11) is a hard boundary, not a heuristic. |
| GiftItem | Recipient-targeting correctness must be provable from the capture (account identifier semantics); a gift to the wrong account is unrecoverable. |
| WearItem/UnequipItems | Equip changes affect crew stats and therefore training demand math; reconciliation delta-spec must include `Character/@ItemIds` before/after, not just item rows. |
| Refill2 / room-module endpoints | S5 budget cap mandatory before ANY enablement discussion (premium spend). |

## 7. What this lane did NOT do

- No code, doc edits outside this file, or test changes.
- No re-derivation of the readiness matrix, capture rankings, task sizing, or
  module layout — those live in the analyst plan and architect addendum and
  are cited, not copied.
- No conflict with sibling lane `t_23f9c5c6` (protocol gates): that lane fills
  readiness-matrix detail; this lane binds the enforcement/abort/adversarial
  contract on top of the gates both inherit.

## 8. Handoff

Machine-readable handoff for the swarm root blackboard `t_b9080d7a`:

```json
{
  "worker": "t_d5626222",
  "role": "safety-boundaries",
  "mutations_attempted": 0,
  "report": "docs/security/inventory-safety-boundaries.md",
  "deliverables": {
    "invariants": ["S1..S7"],
    "recon_abort_conditions": ["A1..A7"],
    "adversarial_tests": ["AD-1..AD-12"],
    "regression_tests": ["RT-1..RT-6"],
    "slice_gates": ["slice0", "slice1", "phase2", "phase4", "phase8", "phase9"]
  },
  "depends_on_blackboard": ["planner-t_23f9c5c6", "architect-t_71d8b652", "reconcile-t_b9d76783"],
  "binding_on": ["Task B..Task L"],
  "key_boundaries": [
    "no live mutation path reachable before Phase 9 (static-scan test T10)",
    "inventory apply is a delete-to-enable stub, not a flag (S2)",
    "abort conditions A1-A7 each require a fakes-only test in Phase 8",
    "protection short-circuits before bucket classification (S3)",
    "exact-sale matching for marketplace transfers is structural (AD-11)"
  ]
}
```
