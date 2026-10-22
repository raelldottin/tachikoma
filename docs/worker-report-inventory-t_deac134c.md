# Worker report: Task D repository integration & Slice 1 boundary verification

Task: `t_deac134c` (workstream A — repository integration).
Swarm root: `t_d799c340`. Verifier: `t_23ccb053`.

This lane inspected the current repository HEAD and dirty worktree, verified the
Slice 0 + Slice 1 boundary claims in the swarm brief, and identified the smallest
correct integration surface for Task D (deterministic inventory classification +
protected-item policy). No production code was changed by this lane — the
implementation lanes are `t_12a30041` (classifier), `t_b49b0534` (CLI/UX),
`t_e3256e21` (protection/adversarial tests).

---

## 1. Precondition checks (all pass)

| # | Check | Result | Evidence |
|---|-------|--------|----------|
| 1 | Repository HEAD & dirty state inspected | PASS | `git log -1` → `2cb5aa4` (mining drone slice). Dirty worktree contains exactly the committed Slice 0/1 swarm output plus unrelated `uv.lock` churn (see §7). |
| 2 | Planning docs read | PASS | `docs/security/protected-item-policy.md` §2 (10 protection rules), §5 (H2/H3 lists); `docs/security/inventory-safety-boundaries.md` §1 S1–S7, §5 phase-2 gate; `docs/architecture-addendum-inventory-t_71d8b652.md` §3 (public interfaces), §13 (handoff contract); `docs/swarm-synthesis-inventory-t_1a2fac28.md` §4 (bucket model), §13 (protected rules); `docs/inventory-implementation-plan.md` §16. |
| 3 | Slice 1 code inspected | PASS | `sdk/inventory/{__init__,_types,catalog,designs,checksums}.py`, `sdk/client.py`, `sdk/commands.py`, `tests/test_inventory.py`. |
| 4 | Unrelated modifications preserved | PASS | No files written by this lane. |
| 5 | Existing domain types suffice for reuse | PASS | `ItemDesign` already carries `item_type`, `item_sub_type`, `rarity`, `module_type`, `tags`, `ingredients`, `training_design_id`, `raw` (`sdk/inventory/_types.py:60-78`). `Item` carries the six captured instance attributes. No competing model is needed. |
| 6 | Repository truth conflicts with approved architecture? | NONE MATERIAL | Two naming-level divergences, both accommodated (§4). No BLOCK required. |

## 2. Slice 1 boundary verification (observed, not assumed)

All boundary claims in the swarm brief were re-verified against the working tree:

- **Baseline test suite**: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
  → `Ran 383 tests ... OK (skipped=1)`. Matches the brief's "383 tests passing
  with one pre-existing skip". (The pre-existing skip is in the suite; 143/143
  when restricted to loadable modules was an artifact of a broken venv — see §7.)
- **`make git-check`** → clean.
- **Ruff on the slice diff surface** (`sdk/inventory`, `sdk/commands.py`,
  `sdk/client.py`, `tests/test_inventory.py`) → `All checks passed!`. The 12
  pre-existing Ruff errors in `scripts/` and `sdk/crew_leveling.py` are
  unrelated to this swarm and predate the inventory work.
- **Architecture boundary**: `sdk/client.py` never imports `sdk.inventory`
  (grep + import-graph check; the one textual mention at `sdk/client.py:905`
  is a docstring). `sdk/inventory/{designs,catalog}.py` import
  `_extract_collection` from `sdk.client` — one-way dependency as specified.
- **No mutation reachability (S1/T10)**: the static scan in
  `tests/test_inventory.py::TestNoMutationPathReachable` passes. Outside tests,
  mutation URL fragments appear only in `sdk/inventory/checksums.py` (docstring
  references inside pure functions) and the pre-existing
  `sdk/client.py:1658 activateItem3` legacy call, which the Slice 1 T10
  carve-out explicitly allows. `run.py` contains no inventory mutation
  references.
- **`inventory apply` always-refusing (S2/AD-10)**:
  `tests/test_inventory.py::TestInventoryCommands` covers refusal for
  `[]`, `--force`, `--yes`, `now`, env-var bypass attempts
  (`INVENTORY_ENABLE_APPLY`, `PSS_ENABLE_INVENTORY_MUTATIONS`,
  `INVENTORY_APPLY`), and `verified=False` registration. All pass.
- **No PSS_* environment-variable bypasses exist** in the environment or code.
- **README.template** (`README.template:71-77`) already documents the
  inventory command surface and the apply stub; README.md is a copy updated
  per the repo rule.

## 3. Evidence hierarchy and domain types

`sdk/inventory/_types.py` already defines the approved five-tier `Evidence`
enum (`VERIFIED`, `CAPTURED_NOT_VERIFIED`, `CAPTURED_AMBIGUOUS`,
`STATICALLY_DISCOVERED`, `INFERRED`, `UNKNOWN` — `_types.py:14-26`), a
`Classification` enum (`_types.py:48-57`), `ReservationSource`, and
`Disposition`. `ClassifiedItem` is specified in the architecture addendum §3
but intentionally NOT yet implemented (Task D scope).

Two notes for the implementation lanes:

1. **Evidence tier nuance**: the brief's hierarchy lists
   `CAPTURED BUT NOT FULLY VERIFIED` as one tier; `_types.py` splits this into
   `CAPTURED_NOT_VERIFIED` and `CAPTURED_AMBIGUOUS`. The split is a
   refinement, not a conflict — minimum-confidence ordering still works
   (verified < captured_verified < captured_ambiguous < static < inferred <
   unknown runs weakest to strongest bottom-up). Classifier evidence
   propagation must use "weakest input wins" (architecture addendum §3,
   gate Q4); a helper like `Evidence.min(a, b)` ordered by the enum's declared
   order-of-strength should be added to `_types.py` rather than re-derivng
   ordering in the classifier.
2. **`Classification` enum is already multi-valued for roles** via the planned
   `reservation: Tuple[Reservation, ...]` field on `ClassifiedItem`
   (addendum §3), so a design can simultaneously be PROTECTED + a bucket
   classification. The brief's "do not force one mutually-exclusive category"
   is satisfied by keeping `classification` as the bucket and expressing
   additional roles (protected, training-material, equipment, ingredient)
   through policy reason codes and reservation sources — no competing model
   is needed.

## 4. Smallest correct integration surface (recommended module split)

Current package contents (`ls sdk/inventory/`):
`__init__.py`, `_types.py`, `designs.py`, `catalog.py`, `checksums.py` — plus
`pycache`. There is no `classify.py`, `classifier.py`, or `policy.py` yet.

Architecture addendum §3 names `protection.py` (`ProtectedItemPolicy
evaluate(...)`) and `classify.py` (`InventoryClassifier` taking policy +
ReservationPlan). The swarm brief's "prefer classifier.py / policy.py if the
repository structure warrants it" is the same content under different names;
the addendum's names are the ones every planning doc cites, so they are the
smallest-diff integration:

- `sdk/inventory/policy.py` — `ProtectedItemPolicy`, rules 1–10 from
  `protected-item-policy.md` §2, short-circuit ordering (safety S3:
  protection runs BEFORE bucket classification), fail-closed on unknown.
- `sdk/inventory/classify.py` — `InventoryClassifier` returning
  `Sequence[ClassifiedItem]`. Task D must NOT depend on a `ReservationPlan`
  from Task F; the correct Task-D shape is `classify(catalog) ->
  Sequence[ClassifiedItem]` with `reservation=()` (empty tuple) and
  `available_surplus` NOT computed (Task G). The addendum's
  `ClassifiedItem.available_surplus` field must either be omitted or fixed at
  `None` for this slice — computing `quantity - 0 = quantity` would leak
  Task G semantics, which the brief explicitly forbids.

Data already available to the classifier from `ItemDesign`/`Item`:
`item_type`, `item_sub_type`, `rarity`, `module_type`, `tags`,
`ingredients`, `training_design_id`. This covers brief classifications:

- crafting ingredient / crafting intermediate → `@Ingredients` (`Artifact`
  inputs in synthesis §6) — INFERRED evidence
- crew-training material → `training_design_id` (63 reachable InstantTraining
  designs, synthesis §5) — CAPTURED_NOT_VERIFIED (feed captured; per-design
  training semantic inferred from `@TrainingDesignId`)
- crew-equipment candidate → `item_type == "Equipment"`/subtypes — INFERRED
- ship-module candidate → `module_type` set — STATICALLY_DISCOVERED (module
  endpoints uncaptured per mutation-safety §6; reservation deferred to
  Task F but the classification role itself is valid metadata)
- ammunition/manufacturing → `item_type`/`item_sub_type` (Missile) — INFERRED
- consumable/use → `UseClippy`/`ItemSubType == "Consumable"`/lootbox etc. —
  INFERRED; note the true `ItemSubType == "Consumable"` set has only 3
  designs and synthesis §4 rules them "KEEP — unknown → protected", so the
  classifier should classify role=consumable but policy still protects them
- strategic-reserve / transferable / marketplace-sale candidates → tags +
  policy rules 5–9; marketplace value is `STATICALLY_DISCOVERED` (endpoint
  uncaptured) and must stay at that tier, never promoted
- unknown/unresolvable design → PROTECTED with evidence UNKNOWN
  (policy rule 10; AD-1)

`Item.raw` extra attributes must remain inert (RT-4): do not read
`@Rarity` from an Item node to enrich the design role — tests
(`test_extra_attributes_land_in_raw_not_fields`) already lock this in.

## 5. What Task D must NOT implement (boundary restatement)

- No `ReservationPlan`, no demand/reservation quantities (Task F).
- No `available_surplus` computation or surplus/discard classification from
  quantity alone (Task G). The existing `InventoryCatalog.quantity_of()`
  is fine; the classifier must not call it to derive disposability.
- No executor, no new mutation endpoint references, no env-var/flag bypass,
  no `inventory apply` change beyond keeping its refusal.
- `inventory` summary stays a read-only digest; `inventory plan` gains
  classification/protection columns but must print a note that values are
  classification-only, not recommendations.

## 6. CLI integration notes for workstream D

`sdk/commands.py::_cmd_inventory` currently rejects any subcommand other than
`plan`. Adding classification output should:

- keep the per-design table deterministic (sort by `ItemDesignId`, then
  reason-code sort per addendum R1);
- report per-design `classifications`, `protected yes/no`, `reasons`,
  `evidence` (min tier), and `unknown` flag;
- NOT print "surplus", "deficit", "reserved", "sell", "consume", "delete",
  "transfer" as outcomes — "candidate" terms need an explicit
  "(classification only; no action implied)" marker in the header.

## 7. Environment hazard found during verification (fixed locally only)

The repo venv (`.venv`, Homebrew Python 3.13) was missing `xmltodict`,
`requests`, etc. after an environment rebuild (`uv.lock` had drifted to
`requires-python = ">=3.13"` while `pyproject.toml` declares
`target-version = "py311"` and pins nothing); `uv pip install -r
requirements.txt` failed on `psycopg2-binary` compilation. Installing the
non-psycopg deps (psycopg2 is not imported by the test suite) restored the
documented test baseline (383 tests, 1 skip). No repository files were changed
to fix this; it is session-environment repair only. Flag for future sessions:
if `.venv` was rebuilt after `uv.lock` churn, re-run
`grep -v psycopg2 requirements.txt | uv pip install --python .venv/bin/python
-r /dev/stdin`.

## 8. Machine-readable handoff (for root blackboard)

```json
{
  "worker": "t_deac134c",
  "lane": "repository-integration",
  "mutations_attempted": 0,
  "files_changed": ["docs/worker-report-inventory-t_deac134c.md"],
  "baseline": {"tests_run": 383, "tests_passed": 382, "tests_skipped": 1, "suite": "tests/test_*.py"},
  "boundary_verification": {
    "client_imports_inventory": false,
    "slice_ruff_clean": true,
    "slice_git_diff_check_clean": true,
    "t10_mutation_scan_clean": true,
    "inventory_apply_always_refuses": true,
    "apply_env_bypass_attempts": ["INVENTORY_ENABLE_APPLY", "PSS_ENABLE_INVENTORY_MUTATIONS", "INVENTORY_APPLY"],
    "apply_refusal_under_bypass": true
  },
  "integration_surface": {
    "new_files": ["sdk/inventory/policy.py", "sdk/inventory/classify.py"],
    "modified_files": ["sdk/inventory/__init__.py", "sdk/inventory/_types.py", "sdk/commands.py", "tests/test_inventory.py", "README.template", "README.md"],
    "client_py_expected_change": "none",
    "run_py_expected_change": "none"
  },
  "deferred_semantics": ["ReservationPlan", "available_surplus", "surplus/discard dispositions"],
  "planning_assumptions_invalidated": [],
  "known_limitations": [
    "Marketplace value classification is STATIC at best until ListSalesByItemDesignId is captured",
    "Equipment role classification is INFERRED from ItemType/ItemSubType; Character/@ItemIds linkage is unused pending Task F",
    "psycopg2-binary does not compile under Python 3.13; local venv fix required (session-only)"
  ]
}
```
