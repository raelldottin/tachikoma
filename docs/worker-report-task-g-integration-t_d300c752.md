# Task G integration lane report (t_d300c752)

Lane: Task G integration with Tasks D/F and the inventory plan UX, without
multi-account, marketplace, or mutation leakage.

Context: the adversarial lane (t_ed2a5694) landed the Task G implementation
(sdk/inventory/reduction.py + the plan dry-run section) after absorbing the
crashed planner lane (t_14d15df9), and the safety lane (t_5f8b64c3) repaired
three defects in it. This lane's job was to reconcile the D/F/G integration
surface against repository truth and the Task F synthesis doc, and to verify
no prohibited planning leaked into it.

## Verification performed (independent of prior lanes)

1. README.template and README.md inventory sections are byte-identical and
   already describe the integrated D+F+G surface accurately (verified
   programmatically before any edit).
2. sdk/inventory AST scan (module + every function/class docstring excluded
   from code analysis): no network imports (socket/requests/urllib/http/
   aiohttp/httpx/ftplib/smtplib), no I/O calls (open/urlopen/connect/
   create_connection/post/request) anywhere in the package. checksums.py
   imports only __future__ and hashlib; its mutation-endpoint names exist
   only in docstring prose documenting the verified checksum formulas
   (Slice 1 scope, unchanged).
3. Disposition members are exactly KEEP/USE/CRAFT/ALLOCATE (deferred
   dispositions unconstructible).
4. AST proof re-run: every `actionable=` keyword in reduction.py is the
   literal False.
5. Apply refusal re-proven under fully-armed settings
   (inventory.enabled/apply/force/mutations all true) plus env vars set:
   byte-identical REFUSED; `help inventory apply` surfaces the unverified
   stub (SAF-G1 fix confirmed working).
6. Plan output rendered end-to-end from the sanitized fixture and checked
   against a 10-point integrated-output contract (see below) — all PASS.

## Defects found and fixed by this lane

### INT-G1: stale Task F-era plan copy contradicting the rendered Task G section

The adversarial lane appended the Task G dry-run section to
`inventory plan` but left the surrounding Task F-era copy in place, so the
same output contradicted itself:

- header said "READ-ONLY Tasks D+F ... no dispositions" while dry-run
  disposition hints rendered below;
- disclaimer said "reduction dispositions and surplus are not available in
  this slice (Task G)" — false about the output it introduces;
- ledger caption said "remaining quantity is NOT computed here — that is
  Task G" while the Task G section computes unreserved directly below;
- the plain `inventory` footer said reduction dispositions/surplus "remain
  unavailable" and the package docstring said reduction "remain[s] later
  tasks (G-L) and intentionally absent" — false since reduction.py exists.

Fix: header now reads "READ-ONLY Tasks D+F+G — classification + demand +
dry-run reduction, nothing executable"; disclaimer scopes the dry-run
("every row is informational (actionable=false), hints name no executor,
and no row is a recommendation"); ledger caption now says remaining
quantity "is NOT computed in this ledger — the Task G dry-run section below
computes unreserved = max(0, observed - reserved)"; plain-view footer and
package docstring updated to match repository truth (remaining tasks are
H-L). Three test pins of the stale claims updated to pin the corrected
contract instead (tests/test_inventory_plan_cli.py,
tests/test_inventory_demand.py, tests/test_inventory.py).

### INT-G2: BLOCKED_ALLOCATION_EXHAUSTED defined and emitted but never exported

The adversarial lane's handoff claims "10 BLOCKED_* reason codes" exported,
but `sdk.inventory` exported only 9: BLOCKED_ALLOCATION_EXHAUSTED (the
reason emitted when a per-design allocation pool is consumed by lower-
ItemId sibling stacks) was defined in reduction.py and emitted by the
planner, yet absent from both the package import and __all__, so
`from sdk.inventory import BLOCKED_ALLOCATION_EXHAUSTED` raised
ImportError. Fixed: exported in sdk/inventory/__init__.py imports and
__all__; regression test added
(test_every_reason_code_defined_is_exported) asserting every BLOCKED_*
constant defined in reduction.py is importable from the package, present
in __all__, and value-identical.

## Files changed by this lane

- sdk/commands.py (stale copy: header, disclaimer, ledger caption,
  plain-view footer, verb description, _render_inventory_plan docstring)
- sdk/inventory/__init__.py (missing BLOCKED_ALLOCATION_EXHAUSTED export;
  stale package docstring scoped to H-L)
- tests/test_inventory_reduction.py (+1 export-consistency regression test)
- tests/test_inventory_plan_cli.py, tests/test_inventory_demand.py,
  tests/test_inventory.py (stale pins -> corrected contract pins)
- docs/worker-report-task-g-integration-t_d300c752.md (this report)

No production behavior changed: all edits are copy/docstring/export-surface
only; the planner, ledger, classifier, refusal stub, and TUI dispatch are
untouched.

## Prohibition verification (no leakage)

- No multi-account planning: no account/userId dimension anywhere in
  sdk/inventory (AST + source scan).
- No marketplace transactions: no MarketService/SellItem/Purchase tokens
  outside docstring prose; DEFAULT_INVENTORY_CONFIG marketplace gate
  untouched.
- No mutation executor/reconciler: none exists; refusal stub unchanged.
- No endpoint enablement: no config/env surface added (the apply handler
  still reads nothing).
- No network I/O in sdk/inventory: AST-verified.
- inventory apply remains an unconditional refusal with no bypass.

## Validation

- Targeted suites (plan_cli, demand, inventory, reduction, safety, classify,
  policy, tui): 321 tests OK.
- Full suite: 648 OK, skipped=1 (647 baseline + 1 new regression test).
- ruff on the touched surface: All checks passed (repo-wide: 11 documented
  pre-existing F401s in crew_leveling/ship_layout/game_formulas/battle
  scripts, committed 632fba6/30c73f0/8a55c34, untouched).
- git diff --check: clean.
- Integrated-output contract (10 checks, end-to-end render): all PASS.

## Hotspot note

sdk/commands.py has now been touched by three lanes in this swarm
(adversarial t_ed2a5694, safety t_5f8b64c3, integration t_d300c752). If a
follow-up slice touches the inventory CLI again, consider splitting the
plan renderer out of CommandRegistry first.
