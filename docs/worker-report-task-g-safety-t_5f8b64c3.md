# Task G Safety/Policy Verification Lane — t_5f8b64c3

Verdict: ALL FOUR MANDATED PROPERTIES PROVEN, and the verification itself found
and fixed three real defects in the landed Task G surface (2 dispatch, 1 determinism).

## Independently proven (consolidated battery, synthetic inputs, no live traffic)

- P1 non-actionability: `actionable=False` on every row across 120 input
  permutations; `plan.actionable_rows == ()` always; invariant chain
  `0 <= candidate <= unreserved <= observed` holds on every row; evidence never
  promoted above CAPTURED_NOT_VERIFIED; every disposition hint drawn only from
  KEEP/USE/CRAFT/ALLOCATE (deferred members unconstructible).
- P2 mutation isolation: planner leaves catalog items, classified sequence and
  ledger entries byte-identical; output is input-order independent.
- P3 no network/live traffic: full plan path runs with `socket.socket` and
  `socket.create_connection` blocked; a fresh-interpreter subprocess imports
  `sdk.inventory` with socket construction blocked (stdlib loaded first so the
  guard only catches genuine connection attempts); package-wide source scan
  finds no requests/socket/urllib/http.client import, no `Client(` construction,
  no `.request(` call, no embedded URL.
- P4 unconditional apply refusal: byte-identical REFUSED output from the
  registry API, under fully-armed settings (`enabled/apply/force/mutations`
  all True), under env-var attack shapes, from the TUI REPL, and via
  `help inventory apply` (which now shows the unverified stub, not the
  verified `inventory` verb).

## Defects found and fixed by this lane (each with regression coverage)

- SAF-G1 (refusal reachability, sdk/tui.py + sdk/commands.py `_cmd_help`):
  the REPL tried the single-word verb first, so `inventory` always shadowed
  `inventory apply`; the registered refusal stub was UNREACHABLE from the TUI
  and `help inventory apply` displayed the verified `inventory` verb. Fixed by
  longest-registered-prefix resolution in both dispatch sites. Regressions:
  `test_tui_loop_dispatches_inventory_apply_to_refusal_handler`,
  `test_help_inventory_apply_shows_unverified_refusal_stub`,
  `test_tui_loop_inventory_plan_still_reaches_plan`,
  `test_help_resolution_covers_all_registered_verbs`.
- SAF-G2 (determinism, sdk/inventory/reduction.py): the planner allocated
  per-design pool shares in caller-input order and only sorted output rows
  afterward; identical inputs in different order produced a different
  allocation, violating the documented "ascending ItemId allocation"
  contract. Every prior test missed it because InventoryCatalog normalizes to
  ascending order before classification. Fixed by enforcing ascending-ItemId
  iteration inside `ReductionPlanner.plan`. Regressions:
  `test_planner_allocation_is_input_order_independent` (2-stack reverse),
  `test_planner_allocation_order_enforced_for_many_stacks` (all 24
  permutations of a 4-stack design).
- SAF-G3 (proof gap, tests only): no prior AST proof pinned `actionable` at
  the construction site, the `actionable_rows` property body, or refusal
  reachability per dispatch surface. Added structural proofs:
  `test_actionable_is_a_compile_time_constant_false`,
  `test_actionable_field_has_no_default_true_anywhere`,
  `test_reduction_result_is_frozen`,
  `test_plan_actionable_rows_property_is_structurally_empty`,
  `test_planner_output_rows_are_value_types_only`,
  `test_plan_dry_run_section_no_mutation_semantics_leak`,
  `test_import_sdk_inventory_performs_no_network_io`,
  `test_apply_refusal_independent_of_client_type`,
  `test_apply_refusal_survives_fully_armed_settings_object`.

## Files changed by this lane

- sdk/tui.py — longest-registered-prefix dispatch (SAF-G1)
- sdk/commands.py — `_cmd_help` multi-word resolution (SAF-G1)
- sdk/inventory/reduction.py — enforced ascending-ItemId iteration (SAF-G2)
- tests/test_inventory_safety.py — new class TestTaskGSafetyLan (15 tests)
- README.template, README.md — `inventory apply` bullet documents the
  dispatch contract (template updated first; sections byte-identical)

## Validation

- Full suite: 647 tests OK, skipped=1 (was 632 before this lane).
- Targeted: inventory suites + test_tui = 320 OK.
- New lane class: 15/15 OK.
- Ruff: clean on all touched files (sdk/tui.py, sdk/commands.py,
  sdk/inventory/reduction.py, tests/test_inventory_safety.py, test_tui.py);
  repo-wide remains 11 pre-existing F401s in crew_leveling/ship_layout/
  game_formulas/battle scripts (committed 632fba6/30c73f0/8a55c34, untouched).
- `git diff --check`: clean.
- Consolidated four-property battery: PASS (120 input permutations on P1).

## Prohibitions held

- No multi-account planning, no marketplace transactions, no mutation
  executor/reconciler, no endpoint enablement, no network I/O added anywhere.
- `inventory apply` refusal unchanged in content; only its REACHABILITY was
  repaired. No config/env bypass introduced (the dispatch change reads only
  the registry's own name map).
- Evidence cap preserved: feed-derived facts stay CAPTURED_NOT_VERIFIED
  (ListItemsOfAShip: 0 captures in 50,755 records — evidence lane t_f4dd7bad).

## Notes for the verifier (t_3e8645cd)

- The dispatch fix changes observable TUI/help behavior: `inventory apply`
  now prints the refusal (previously "Unknown inventory subcommand"), and
  `help inventory apply` shows the unverified stub (previously the verified
  inventory verb's usage). Both prior behaviors were fail-closed but
  misrepresenting; the new behavior matches the registered contract.
- The determinism fix changes planner output ONLY for callers that hand the
  planner a non-ascending classified sequence (none in the repo; all
  in-repo callers go through InventoryCatalog which sorts). The live-shape
  356-stack probe result is unchanged.
- The safety lane adds no production imports and no new verbs.
