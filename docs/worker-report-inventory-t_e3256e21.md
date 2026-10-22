# Worker report: Task D lane C — protected-item fail-closed policy and adversarial safety coverage

Task: `t_e3256e21` (workstream C — protected-item safety).
Swarm root: `t_d799c340`. Verifier: `t_23ccb053`.

## 1. Repository baseline (observed at lane start)

- HEAD `2cb5aa4` (mining drone slice), branch `main`.
- Dirty worktree = the committed-intent Slice 0/1 swarm output (untracked
  `sdk/inventory/`, `tests/test_inventory.py`, docs/) plus unrelated
  `uv.lock` churn and pre-existing modified `sdk/client.py`/`sdk/commands.py`/
  `README*` — all preserved, none written by this lane beyond the inventory
  package itself.
- Baseline suite before my changes: `Ran 383 tests ... OK (skipped=1)`,
  matching the brief.

### Lane B contingency (important for the verifier)

Lane B (`t_12a30041`, classification semantics) crashed twice on
model output-token limits and the dispatcher recorded `gave_up` with ZERO
files landed — no `policy.py`, no `classify.py`, no `_types.py` additions
existed when this lane started. Its blackboard stake
(`lane-stake:t_12a30041`, root comment thread) froze the interface contract
before crashing. This lane implemented the full pure-domain Task D surface
to that EXACT frozen contract (module names, signatures, reason-set
semantics, None-vs-frozenset context meaning, role order, evidence rules,
forbidden-role list). If lane B is ever re-dispatched, its work exists
already; its lane-stake note itself said to verify rather than recreate.

## 2. Files added

- `sdk/inventory/policy.py` — `ProtectedItemPolicy` (fail-closed rules),
  reason-code constants, marker sets, `policy_limitations`.
- `sdk/inventory/classify.py` — `InventoryClassifier`, `classify_catalog`.
- `tests/test_inventory_policy.py` — 52 lane tests (per-lane test file,
  avoiding `test_inventory.py` collisions per the lane-B convention).
- `docs/worker-report-inventory-t_e3256e21.md` — this report.

## 3. Files modified

- `sdk/inventory/_types.py` — additions ONLY: `Role`, `Reservation`,
  `ProtectionContext`, `ClassifiedItem`, `ROLES_FORBIDDEN_THIS_SLICE`,
  `Evidence.weaker`/`Evidence.weakest`; no existing type changed.
- `sdk/inventory/__init__.py` — export additions only.

NOT touched (staked by siblings / out of lane): `sdk/commands.py`,
`README.template`, `README.md`, `tests/test_inventory.py`, `sdk/client.py`,
`run.py`. Lane D (`t_b49b0534`) still owns CLI rendering.

## 4. Classification/domain model implemented

`ClassifiedItem` (frozen dataclass): `item`, `design: Optional[ItemDesign]`,
`classification`, `roles: Tuple[Role, ...]`, `protection_reasons`,
`classification_reasons`, `evidence`, `unknown`, `reservation=()` (ALWAYS,
Task F), `available_surplus=None` (ALWAYS, Task G), plus `protected` property.

`Role` enum (declaration order = stable output order): CREW_TRAINING_MATERIAL,
CREW_EQUIPMENT_CANDIDATE, CRAFTING_INGREDIENT, CRAFTING_INTERMEDIATE,
SHIP_MODULE_CANDIDATE, AMMUNITION_MATERIAL, STRATEGIC_RESERVE_CANDIDATE,
TRANSFERABLE_CANDIDATE, MARKETPLACE_SALE_CANDIDATE, CONSUMABLE_USE_CANDIDATE,
DISCARD_CANDIDATE. The last three are domain vocabulary ONLY — never
assigned this slice (`ROLES_FORBIDDEN_THIS_SLICE` asserted in the
classifier). Multi-role is first-class: a design can be simultaneously
crafting-ingredient + consumable + protected (roles and protection are
independent axes; protection wins the bucket).

Primary buckets emitted: PROTECTED | RESERVED | CONSUMABLE (+ `unknown`
flag). SURPLUS_*/TRANSFERABLE/SELLABLE/DISCARDABLE are never assigned.

## 5. Protection rules implemented

`ProtectedItemPolicy.evaluate(item, design, context) -> Tuple[str, ...]` —
ALL matched reason codes in FIXED rule order 1, 4, 5, 6, 7, 8, 10 (no
short-circuit; protection stays boolean = any reason fired):

| Rule | Reason code | Fires when |
|---|---|---|
| 1 | PROTECTED_EQUIPPED | ItemId ∈ context.equipped_item_ids (set available) |
| 4 | PROTECTED_ROOM_MODULE | ItemId ∈ context.room_item_ids (set available) |
| 5 | PROTECTED_RARITY | rarity ∈ {Rare, Epic, Hero, Unique, Legendary} |
| 5b | PROTECTED_INSUFFICIENT_EVIDENCE | rarity empty/unrecognized (fail closed) |
| 6 | PROTECTED_EVENT_LIMITED | tag ∈ event/trader set (event, robotrader, graytrader, harvestor01-04, stardust, voidparticle, vip, uber, heroic, unique, threewins) |
| 7 | PROTECTED_QUEST_PROGRESSION | ItemType/ItemSubType contains quest/blueprint |
| 8 | PROTECTED_PREMIUM | premium tags (premium, vip, uber, battlepass) or BattlePass subtype |
| 10 | PROTECTED_UNKNOWN_DESIGN | design is None (unknown/unresolvable) |

Marker sets are grounded in the captured-feed analysis
(docs/worker-report-inventory-t_fd97b1ee.md §3, swarm synthesis §4/§13:
the 220-design tagged set, rarity vocabulary, blueprint/progression class).
Tag matching is exact-token, case-insensitive — adversarial substrings
("eventful", "vipguest") do NOT match (regression-tested).

Rules 2 (equipment-plan), 3 (elected-craft-recipe) = Task F semantics, not
evaluated. Rule 9 (marketplace-valuable) = ListSalesByItemDesignId
uncaptured; structurally present but inert, never demotes, never invents a
value. All three are exposed deterministically via
`policy_limitations(context)` and `InventoryClassifier.limitations`.

## 6. Evidence propagation rules

- `Evidence.weaker(a, b)` / `Evidence.weakest(tiers)` on the declared enum
  order (strongest → weakest); empty input → UNKNOWN (fail closed).
- Per-role input tiers: item+design rows CAPTURED_NOT_VERIFIED; training
  link (@TrainingDesignId) CAPTURED_NOT_VERIFIED; module role
  STATICALLY_DISCOVERED (module endpoints uncaptured); marker-derived roles
  INFERRED.
- `ClassifiedItem.evidence` = `Evidence.weakest([row tier] + role tiers)`;
  unknown design → UNKNOWN. Never promoted above the weakest input (Q4).

## 7. Unknown / fail-closed behavior

- Unknown design (not in dynamic feed) → PROTECTED + `unknown=True` +
  evidence UNKNOWN + reason PROTECTED_UNKNOWN_DESIGN.
- Missing design catalog entirely (`design_catalog=None`) → every item
  resolves None → same fail-closed path.
- Partial metadata never reduces protection: empty/whitespace/unrecognized
  rarity → PROTECTED_INSUFFICIENT_EVIDENCE (a malformed record is never
  less protected than a complete one — regression-tested both directions).
- `ProtectionContext` None-vs-empty semantics: None = data unavailable →
  rule cannot fire + limitation exposed; frozenset() = positively observed
  empty → rule evaluates negative.
- Quantity NEVER influences protection or classification (tested at -5, 0,
  1, 9, 10^9, 10^12).

## 8. CLI changes

None by this lane (lane D `t_b49b0534` owns `sdk/commands.py` + README
rendering). The public API lane D consumes is exported from
`sdk.inventory`: `InventoryClassifier`, `classify_catalog`,
`ProtectedItemPolicy`, `ProtectionContext`, `ClassifiedItem`, `Role`,
`Evidence`, `policy_limitations`. `inventory apply` remains an
always-refusing stub — this lane's tests re-verify the refusal under
argument-shape and environment-variable attacks (5 env vars attempted;
source inspected to confirm the stub reads no environment/config).

## 9. Tests added

`tests/test_inventory_policy.py` — 52 tests, 6 suites:
TestPolicyRules (13), TestClassifierClassification (10),
TestAdversarialMarkers (6), TestEvidencePropagation (6),
TestTaskFGBoundaries (4), TestPurityAndSafety (7), TestDeterminism (3),
plus helper-shape test. Sanitized synthetic fixtures only; no network; no
live PSS calls; mocked client via `MagicMock(spec=Client)`.

Brief coverage points 1-24 mapping: 1,2 (classifier/classification tests),
3,4 (unknown/missing design), 5 (partial-metadata + adversarial markers),
6 (rarity), 7 (event tags, quest/blueprint), 8,9 (ingredient/intermediate),
10 (consumable incl. literal ItemSubType=Consumable dual role+protection),
11 (equipment/module incl. STATICALLY_DISCOVERED evidence), 12
(ammunition), 13,14 (evidence min + no-promotion + strictness), 15
(reason-code and role ordering), 16 (determinism suite; CLI-output
determinism handed to lane D), 17 (ItemId vs ItemDesignId collision probe:
equipped ItemId=201 vs distinct item with ItemDesignId=201), 18 (quantity
invariance at policy AND classifier level), 19 (no ReservationPlan/DemandPlanner
exports; reservation always ()), 20 (no surplus/executor/transfer exports;
available_surplus always None), 21 (apply refuses all arg shapes), 22
(no mutation endpoint name or Client reference in classifier/policy source),
23 (5 env-var bypass attempts + stub-source inspection), 24 (socket blocked
during full classification run).

## 10. Adversarial cases covered

Unknown design; missing design catalog; all-fields-empty design row;
whitespace rarity; unrecognized rarity ("Zealous"); substring-adversarial
tags (eventful/vipguest/ubercool — no false match); case-adversarial
markers (VIP/RARE/COMMON — match); ItemId==ItemDesignId collision;
equipped-set vs design-id conflation; negative/zero/huge quantity; frozenset
vs None context; frozen-dataclass mutation attempt; byte-level double-run
determinism; forbidden-role assertion inside the classifier (hard assert);
env-var bypass attempts; mutation-fragment source scan of new modules.

## 11. Full validation results

- Full suite: `Ran 435 tests in ... OK (skipped=1)` (383 baseline + 52 new;
  the 1 skip is pre-existing).
- Targeted: `tests.test_inventory_policy` — 52/52 OK.
- Ruff on all changed/new files: `All checks passed!`.
- `python -m compileall -q run.py sdk`: clean.
- `git diff --check`: clean.

## 12-15. Forbidden-work confirmations

- Task F NOT implemented: no ReservationPlan, no InventoryDemandPlanner, no
  reservation quantities anywhere; `ClassifiedItem.reservation` is always ()
  (tested).
- Task G NOT implemented: no available_surplus computation, no
  surplus/transfer/sell/discard classification or role, no reduction
  actions, no executor/reconciler (tested via export absence + value
  assertions).
- No mutation endpoint became reachable: classifier/policy contain no
  endpoint names, no Client references, no requests import (source-scanned
  by test); `sdk/client.py` still never imports `sdk.inventory` (tested).
- `inventory apply` still always refuses (re-verified under new arg shapes
  and env-var attacks).

## 16. Planning assumptions invalidated by repository truth

- Lane B's planned `classifier.py` filename: implemented as `classify.py`
  per its own frozen lane-stake note (addendum §3 name) — no conflict.
- The addendum's `ClassifiedItem.available_surplus: int` field shape would
  leak Task G semantics in this slice; implemented as
  `Optional[int] = None` and never computed (lane A's recommendation,
  followed).
- The addendum's `evaluate -> Optional[str]` short-circuit sketch was
  superseded by the frozen reason-SET contract (all matched codes) —
  followed.

## 17. Known limitations intentionally deferred

- Rule 9 (marketplace value): inert until ListSalesByItemDesignId captured.
- Rules 2/3 (plan-derived protection): Task F.
- Equipped/room item sets: no captured source wired yet; None default means
  rules 1/4 cannot fire and limitations are exposed.
- CLI rendering of classification output: lane D (t_b49b0534) — the
  verifier should gate on it if `inventory plan` must display this surface.
- Evidence for marker-derived roles is INFERRED by design; promotion
  requires captures (ListItemsOfAShip full field enumeration first).

## 18. Recommended next implementation slice

1. Land lane D (CLI rendering) so `inventory plan` reports the
   classification surface — the API is exported and frozen.
2. Then the Task F demand-planning slice (ReservationPlan + rules 2/3
   protection activation) — the ClassifiedItem/Reservation shapes are
   already in place for it.
3. Capture `ListItemsOfAShip` (ranked #1 in synthesis §15) — it unblocks
   evidence promotion for the entire classification surface.

## Machine-readable handoff

```json
{
  "worker": "t_e3256e21",
  "lane": "C-protected-item-safety",
  "mutations_attempted": 0,
  "files_added": [
    "sdk/inventory/policy.py",
    "sdk/inventory/classify.py",
    "tests/test_inventory_policy.py",
    "docs/worker-report-inventory-t_e3256e21.md"
  ],
  "files_modified": ["sdk/inventory/_types.py", "sdk/inventory/__init__.py"],
  "files_untouched_by_this_lane": ["sdk/commands.py", "README.template", "README.md", "tests/test_inventory.py", "sdk/client.py", "run.py"],
  "baseline": {"tests_run": 383, "tests_passed": 382, "tests_skipped": 1},
  "after": {"tests_run": 435, "tests_passed": 434, "tests_skipped": 1, "new_lane_tests": 52},
  "boundary": {
    "client_imports_inventory": false,
    "classifier_policy_no_client_refs": true,
    "apply_always_refuses": true,
    "apply_env_bypass_tested": true,
    "ruff_clean_new_files": true,
    "git_diff_check_clean": true
  },
  "task_f_implemented": false,
  "task_g_implemented": false,
  "forbidden_roles_never_assigned": true,
  "lane_b_contingency": "lane B crashed with zero files landed; this lane implemented the frozen interface contract it published (policy.py + classify.py + _types additions)",
  "handoff_to_lane_D": "sdk.inventory exports InventoryClassifier/classify_catalog/ProtectedItemPolicy/ProtectionContext/ClassifiedItem/Role/policy_limitations; lane D renders them in inventory/inventory plan without Task F/G semantics"
}
```
