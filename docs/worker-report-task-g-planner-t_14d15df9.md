# Task G Planner Lane Report — t_14d15df9

Lane: planner (deterministic dry-run surplus/reduction planner).
Status: lane scope absorbed by the adversarial lane after two crashes; this
re-spawned run independently verified the landed Task G surface against this
lane's contract. No production code was changed by this run.

## What happened

This lane crashed twice on model output-token limits before producing any
work (runs 86 and 87; both left the scratch workspace empty). The
adversarial lane (t_ed2a5694) absorbed the planner scope and shipped
`sdk/inventory/reduction.py`; the safety lane (t_5f8b64c3) and integration
lane (t_d300c752) subsequently hardened and reconciled it (5 combined
defects found and fixed across those lanes). This re-spawned run did not
re-implement or modify any of that work; it independently re-verified it.

## Independent verification performed (this run, fresh evidence)

Full-suite and static checks, run from a clean shell:

- `make test` — 648 tests, OK (skipped=1). Matches the integration lane's
  claimed count exactly.
- `ruff check` on `sdk/inventory/`, all 7 inventory test files,
  `sdk/commands.py`, `sdk/tui.py` — all checks passed.
- `git diff --check` — clean.
- README parity — the only diff between README.template and README.md is the
  GitHub-hook template substitution header (`{{ repository.name }} {{ current.version }}`);
  the inventory sections are byte-identical.

Fresh behavioral probe (no fixture reuse; synthetic inputs through the real
public API chain InventoryCatalog -> InventoryClassifier -> DemandPlanner ->
ReductionPlanner). All 15 probes passed:

- invariant fuzz, 200 randomized trials across mixed blocking classes
  (negative/zero/positive quantities, multiple designs and stacks):
  0 violations of 0 <= candidate <= unreserved <= observed, per-design
  candidate sums within unreserved, HOLD rows always candidate=0,
  CANDIDATE rows always candidate>0, actionable always False.
- determinism: all 120 input permutations of a 5-stack catalog produce
  byte-identical plans (extends the safety lane's SAF-G2 fix verification).
- per-design pool allocation: hand-checked case allocates ascending-ItemId
  exactly (7+3+1 = 11 = unreserved).
- protection: equipped stack HOLD/candidate=0 with BLOCKED_PROTECTED_OR_UNKNOWN;
  clean sibling CANDIDATE with full quantity.
- zero-quantity: HOLD with BLOCKED_ZERO_QUANTITY on a clean design;
  negative row: HOLD with BLOCKED_MALFORMED_ROW and excluded from observed
  supply; the malformed row's design-scoped condition also blocks the clean
  sibling stack of the same design (BLOCKED_DESIGN_CONDITION), while a clean
  design's sibling stack is untouched — condition scoping confirmed.
- unknown design: HOLD/candidate=0 with BLOCKED_PROTECTED_OR_UNKNOWN.
- evidence: no row reaches VERIFIED (feed rows capped at
  CAPTURED_NOT_VERIFIED; weakest-input propagation).
- over-reserved: a hand-forged ReservationEntry with reserved 99 > observed 5
  (the public build path cannot over-reserve) fails closed — HOLD,
  candidate=0, BLOCKED_OVER_RESERVED on every stack of the design.
- unsupported-demand applicability: a training-material design is blocked
  with BLOCKED_UNSUPPORTED_DEMAND while plain and strategic-role designs in
  the same plan are not; 4 global SOURCE_UNSUPPORTED conditions are present
  on the plan without blocking role-less designs.
- strategic-role (design-90 shape): STRATEGIC_RESERVE_CANDIDATE without
  config -> HOLD, candidate=0, KEEP hint; both directions are also
  unit-tested in the shipped suite.

Apply refusal, tested directly in this run: the registered `inventory apply`
handler returns the byte-identical REFUSED string under armed settings
(`inventory.enabled/apply/mutations` all True) and armed env vars
(PSS_INVENTORY_ENABLED / INVENTORY_ENABLED / PSS_INVENTORY_APPLY /
PSS_INVENTORY_MUTATIONS all "1"), and is registered `verified=False`.

Package surface, tested directly: `Disposition` has exactly
[KEEP, USE, CRAFT, ALLOCATE]; `Disposition("TRANSFER"|"SELL"|"CONSUME"|"DELETE")`
raises ValueError — deferred dispositions are unconstructible.

Network isolation, tested directly: no socket/ssl/urllib/http/requests/httpx
import tokens anywhere in `sdk/inventory/` (source scan), and a
fresh-interpreter import of `sdk.inventory` under a blocked module finder
attempts no network module import.

Evidence cap justification, independently re-scanned: 0 occurrences of
ListItemsOfAShip (case-insensitive, binary-safe) across the 50,455 JSONL
records of ~/pss-mitm-capture.jsonl + ~/pss-login-capture.jsonl. No live
traffic was created by this run; the scan was read-only from disk.

## Contract reconciliation (task body vs. repo truth)

Every mandated property was verified present and passing:

- unreserved = max(0, observed - total_reserved): reduction.py:339.
- 0 <= candidate <= unreserved <= observed enforced, never trusted:
  enforced via per-design pools (reduction.py:388-407) and verified by fuzz.
- Protected/unknown/malformed/over-reserved/applicable-unresolved-demand
  rows get candidate=0: blocking precedence chain (reduction.py:361-378),
  each probed.
- Unsupported training/ammunition demand blocks only applicable designs:
  role-match applicability (reduction.py:375-376), probed alongside
  unblocked role-less designs.
- Weakest-input evidence preserved: Evidence.weakest across feed rows,
  ledger, classification (reduction.py:351-356); never VERIFIED.
- actionable=False on every result: field default False, frozen dataclass,
  literal False at every construction site; AST-proven by the safety lane,
  confirmed by fuzz here.
- HOLD/CANDIDATE states, reasons, evidence, disposition hints with fixed
  future order KEEP/RESERVE->USE->CRAFT->ALLOCATE->TRANSFER->SELL->CONSUME
  ->DELETE; TRANSFER/SELL/CONSUME/DELETE deferred and unconstructible:
  Disposition enum member set verified.
- Read-only inventory plan extended with the dry-run section (no new verb).
- No multi-account planning, no marketplace transactions, no mutation
  executor/reconciler, no endpoint enablement, no network I/O in
  sdk/inventory: source-scanned + import-probed.
- inventory apply unconditional refusal, no config/env bypass: probed under
  fully-armed settings and env; byte-identical refusal.
- Adversarial test coverage: tests/test_inventory_reduction.py (40 tests,
  9 classes) + tests/test_inventory_safety.py TestTaskGSafetyLan (15) cover
  arithmetic, protection, uncertainty monotonicity, unsupported demand,
  shared refs/conflicts, evidence, determinism, mutation isolation,
  network isolation, and apply refusal.

## Defects found by this run

None. Every discrepancy encountered during probing was a defect in the
probe fixtures, not the planner:

- Probe fixtures with empty rarity correctly tripped
  PROTECTED_INSUFFICIENT_EVIDENCE (fail-closed policy by design).
- A probe that mixed a negative-quantity row into a zero-quantity stack's
  design correctly tripped the design-scoped MALFORMED_QUANTITY condition
  (BLOCKED_DESIGN_CONDITION precedence over BLOCKED_ZERO_QUANTITY).

## Hotspot note

tests/test_inventory_reduction.py and tests/test_inventory_plan_cli.py were
each touched by 2-3 lanes; sdk/commands.py by 3 lanes (already flagged by
the integration lane). This run deliberately added no code to those files;
the verification probe ships as a task artifact instead.

## Artifacts

- This report: docs/worker-report-task-g-planner-t_14d15df9.md
- Verification probe (runnable): task_g_lane_probe.py, attached to the task
