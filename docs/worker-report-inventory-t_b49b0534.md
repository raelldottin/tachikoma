# Worker report: Task D lane D — CLI/read-only UX classification rendering

Task: `t_b49b0534` (workstream D — CLI/read-only UX).
Swarm root: `t_d799c340`. Verifier: `t_23ccb053`.

## 1. Repository baseline (observed at lane start)

- HEAD `2cb5aa4` (mining drone slice), branch `main`.
- Lane C (`t_e3256e21`) had just landed the pure-domain Task D layer:
  `sdk/inventory/policy.py`, `sdk/inventory/classify.py`,
  `_types.py` additions (Role/Reservation/ProtectionContext/ClassifiedItem/
  Evidence.weaker+weakest), `__init__.py` exports, 53 lane tests.
- Baseline suite before my changes: `Ran 436 tests ... OK (skipped=1)`.
- Lane B (`t_12a30041`) crashed twice with zero files landed; lane C
  implemented its frozen interface contract. Nothing for lane D to redo.

## 2. Files added

- `tests/test_inventory_plan_cli.py` — 30 lane tests (per-lane test file,
  avoiding `test_inventory.py` edit collisions per the swarm convention).
- `docs/worker-report-inventory-t_b49b0534.md` — this report.

## 3. Files modified

- `sdk/commands.py` — `inventory plan` now renders the Task D
  classification surface; `inventory` keeps the plain per-design digest;
  `inventory apply` untouched (still always-refusing).
- `README.template` — updated FIRST (repo rule), then `README.md` rendered
  from it (identical inventory section).

NOT touched by this lane: `sdk/inventory/*` (lane C's frozen surface,
consumed only), `sdk/client.py`, `run.py`, `tests/test_inventory.py`,
`tests/test_inventory_policy.py`, `tests/test_tui.py`.

## 4. CLI changes implemented

`inventory plan` (workstream D integration of the lane-C API):

- Header: `=== Inventory plan (READ-ONLY Task D — classification, no
  dispositions) ===` plus an explicit statement that classification does
  not decide what to keep/use/remove and that demand reservations and
  reduction dispositions are Tasks F/G.
- Summary: stack count, bucket counts in fixed order
  (PROTECTED, RESERVED, CONSUMABLE), zero-quantity stack count.
- Per item (ascending ItemId, the classifier's deterministic order):
  `item <ItemId> | design <ItemDesignId> | qty <n> | <bucket> |
  protected: yes/no | evidence <tier>` plus `| unresolved` for unknowns.
  Indented sublines: design name (or the explicit unavailable note for
  unknown designs), protection reason codes (fixed rule order, ALL
  matched codes joined), roles (Role declaration order), classification
  reasons (one per line).
- Limitations block: the classifier's `limitations` tuple (rules 2/3/9
  inert, rule 1/4 set-unavailable), labeled "never a demotion".
- Footer: no surplus/deficit/reservation/transfer-target/sale quantity is
  computed; no role or bucket is a mutation recommendation; apply remains
  refused.

`inventory` (plain view): unchanged digest format; the stale footer claim
"classification buckets ... unavailable" was replaced with a pointer to
`inventory plan` (classification now exists; demand/disposition terms
remain listed as unavailable).

`inventory apply`: byte-identical stub, still `verified=False`.

## 5. Task F/G non-leakage (how it is guaranteed at the CLI layer)

- The renderer only reads `ClassifiedItem` fields; it never computes
  anything. `reservation`/`available_surplus` are not rendered at all.
- Buckets rendered are exactly the three the classifier can emit; the
  count line enumerates a fixed tuple ("PROTECTED", "RESERVED",
  "CONSUMABLE") and `counts.get(name, 0)` means an unemittable bucket
  (SURPLUS_*/TRANSFERABLE/SELLABLE/DISCARDABLE) can never appear even if
  a future classifier regressed — it would render as 0 or be absent.
- The only "candidate" usage is roles (domain vocabulary), and the
  per-item preamble states it is a classification label only, never a
  recommendation.
- Tests assert: no SURPLUS/TRANSFERABLE/SELLABLE/DISCARDABLE token on
  any item line; forbidden roles absent from the entire output; a
  40-quantity missile stack classifies RESERVED with protected:no and no
  discard/sell/transfer anywhere; consumed ClassifiedItems carry
  `reservation=()` and `available_surplus=None`.

## 6. Tests added (30, all passing; sanitized fixtures, mocked Client)

TestPlanClassificationOutput (11): header/disclaimer; ItemId +
ItemDesignId + quantity; design names; bucket counts; protected flag +
multi-reason sets (PROTECTED_RARITY + PROTECTED_EVENT_LIMITED in rule
order) + PROTECTED_INSUFFICIENT_EVIDENCE fail-closed; roles rendering
(ingredient/training/ammunition/module/consumable); module
STATICALLY_DISCOVERED vs marker INFERRED evidence tiers; unknown design
unresolved + PROTECTED_UNKNOWN_DESIGN + explicit name-unavailable note;
zero-quantity stack classified and marked never-actionable; limitations
block (rules 1/2/3/4/9 statements); ItemId 563 vs design 563 conflation
probe (no mixup in either direction).

TestPlanDeterminism (3): byte-identical double run; ascending-ItemId
order; plain `inventory` view still deterministic.

TestPlanNoTaskFGSemantics (5): no F/G terms on item lines; forbidden
roles absent; high quantity never surplus/discard; F/G explicitly stated
unavailable; consumed ClassifiedItems keep inert F/G fields.

TestInventoryRemainsPlainDigest (4): plain view unchanged (stacks 8,
qty totals); unknown designs still PROTECTED footer; pointer to plan;
no reasons/roles/evidence lines leak into the plain view.

TestApplyStillRefuses (3): all arg shapes ([], --force, --yes, now,
sell 1001) refuse with Phase 8/9 text; 5 env-var bypass attempts
(INVENTORY_ENABLE_APPLY, PSS_ENABLE_INVENTORY_MUTATIONS, INVENTORY_APPLY,
PSS_INVENTORY_APPLY, TACHIKOMA_INVENTORY_MUTATIONS) all still refused;
verified flags unchanged.

TestPlanSafetyAndPurity (4): no mutation URL fragment in commands.py
source; sdk/client.py still never imports sdk.inventory; plan runs with
socket.socket and create_connection patched to raise; commands.py source
contains no os.environ/getenv consultation.

## 7. Validation results (full ladder)

- Targeted: `tests.test_inventory_plan_cli` 30/30 OK;
  `tests.test_inventory_plan_cli + tests.test_inventory +
  tests.test_inventory_policy + tests.test_tui` = 139/139 OK.
- Full suite: `Ran 466 tests in 0.261s — OK (skipped=1)`
  (436 baseline + 30 lane tests; the skip is pre-existing).
- Ruff on changed/new files (`sdk/commands.py`,
  `tests/test_inventory_plan_cli.py`): `All checks passed!`.
  Repo-wide ruff count remains 12 pre-existing errors in untouched files
  (matches lanes A and C).
- `python -m compileall -q run.py sdk`: clean.
- `git diff --check`: clean.

## 8. Confirmations (brief sections 12-15)

- Task F NOT implemented by this lane: no reservation quantities, no
  demand planning; plan output renders none.
- Task G NOT implemented by this lane: no surplus computation; plan
  output renders none.
- No mutation endpoint became reachable: no mutation URL in
  `sdk/commands.py`; classifier/policy untouched (lane C's
  no-endpoint/no-Client source guarantees hold); client still never
  imports sdk.inventory (tested).
- `inventory apply` still always refuses: re-verified under 5 arg shapes
  and 5 env-var bypass attempts.

## 9. Planning assumptions invalidated by repository truth

None material. One stale claim corrected: the slice-1 plan header/footer
said classification was "not available in this slice (Tasks D/F/G)" —
classification (Task D) is now implemented, so the note now correctly
scopes unavailability to Tasks F/G only.

## 10. Known limitations intentionally deferred

- Equipped/room ItemId sets have no captured source; the CLI constructs
  the default `ProtectionContext()` (None sets), so rules 1/4 cannot
  fire and are exposed in the limitations block — never a demotion.
- Rule 9 marketplace value stays inert until ListSalesByItemDesignId is
  captured (lane C limitation, surfaced in plan output).
- Evidence promotion requires ListItemsOfAShip capture (synthesis §15 #1).

## 11. Recommended next implementation slice

1. Task F demand-planning (ReservationPlan + rules 2/3 activation) —
   ClassifiedItem/Reservation shapes already exist.
2. Capture `ListItemsOfAShip` to promote evidence tiers and wire the
   equipped/room sets.
3. Then Task G surplus math on top of reservations.

## Machine-readable handoff

```json
{
  "worker": "t_b49b0534",
  "lane": "D-cli-readonly-ux",
  "mutations_attempted": 0,
  "files_added": [
    "tests/test_inventory_plan_cli.py",
    "docs/worker-report-inventory-t_b49b0534.md"
  ],
  "files_modified": ["sdk/commands.py", "README.template", "README.md"],
  "files_untouched_by_this_lane": [
    "sdk/inventory/policy.py",
    "sdk/inventory/classify.py",
    "sdk/inventory/_types.py",
    "sdk/inventory/__init__.py",
    "sdk/client.py",
    "run.py",
    "tests/test_inventory.py",
    "tests/test_inventory_policy.py"
  ],
  "baseline": {"tests_run": 436, "tests_passed": 435, "tests_skipped": 1},
  "after": {"tests_run": 466, "tests_passed": 465, "tests_skipped": 1, "new_lane_tests": 30},
  "boundary": {
    "client_imports_inventory": false,
    "commands_source_has_no_mutation_urls": true,
    "apply_always_refuses": true,
    "apply_env_bypass_attempts": 5,
    "plan_runs_with_sockets_blocked": true,
    "ruff_clean_changed_files": true,
    "git_diff_check_clean": true
  },
  "task_f_implemented": false,
  "task_g_implemented": false,
  "fg_leakage_checks": {
    "no_surplus_transferable_sellable_discardable_on_item_lines": true,
    "forbidden_roles_absent_from_output": true,
    "high_quantity_stays_reserved": true,
    "consumed_classified_items_reservation_empty_and_surplus_none": true
  },
  "cli_surface": {
    "inventory": "plain per-design digest unchanged (footer now points at plan)",
    "inventory_plan": "per-item Task D classification: ids, name, qty, bucket, protected, reasons, roles, evidence, unresolved, limitations",
    "inventory_apply": "untouched always-refusing stub, verified=false"
  },
  "report": "docs/worker-report-inventory-t_b49b0534.md"
}
```
