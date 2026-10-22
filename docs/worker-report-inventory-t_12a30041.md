# Worker Report — Task D Lane B: Classification Semantics (t_12a30041)

Lane: B-classification-semantics
Swarm root / blackboard: t_d799c340
Repo HEAD at work: 2cb5aa4 (main, dirty worktree preserved)

## 1. What happened on this lane

Lane B staked the frozen classification interface on the blackboard
(lane-stake:t_12a30041) and then crashed twice on the model's output-token
limit with ZERO files landed. Lane C (t_e3256e21) landed the pure-domain
layer (sdk/inventory/policy.py, sdk/inventory/classify.py, _types.py
additions) to that exact frozen contract as a contingency, and lane D
(t_b49b0534) landed the CLI rendering.

This re-dispatch followed lane C's blackboard instruction: VERIFY the landed
surface against the frozen contract, do not recreate it. That verification is
what this run performed.

## 2. Frozen-contract verification result: PASS

Every interface decision from lane-stake:t_12a30041 verified against the
landed code:

- Module names: classify.py + policy.py (as staked).
- ProtectedItemPolicy.evaluate(item, design, context) -> Tuple[str, ...] of
  ALL matched reason codes in fixed rule order 1,4,5,6,7,8,10; no
  short-circuit (policy.py EVALUATED_RULES).
- Rules 2/3 (Task F) and 9 (uncaptured marketplace) inert with limitations
  exposed via policy_limitations().
- ProtectionContext equipped_item_ids/room_item_ids: None = unavailable
  (rule cannot fire, limitation exposed), frozenset() = positively empty.
- InventoryClassifier(policy, context) constructor with defaults;
  classify(catalog) -> Tuple[ClassifiedItem, ...]; no ReservationPlan
  parameter; .limitations property.
- ClassifiedItem.reservation == () and available_surplus is None always.
- Buckets emitted: PROTECTED | RESERVED | CONSUMABLE (+ UNKNOWN state flag);
  SURPLUS_*/TRANSFERABLE/SELLABLE/DISCARDABLE never assigned.
- ROLES_FORBIDDEN_THIS_SLICE never assigned (hard assert in _roles()).
- Role enum declaration order == stable roles output order (11 roles).
- Evidence: minimum across required inputs via Evidence.weaker/weakest;
  item+design rows CAPTURED_NOT_VERIFIED; unknown design UNKNOWN; module
  role STATICALLY_DISCOVERED; marker roles INFERRED; training
  CAPTURED_NOT_VERIFIED; empty input UNKNOWN (fail closed).
- RESERVED-when-present buckets are classification-level conservatism, not
  Task F reservation math (no quantities computed).

## 3. Defect found during verification: fixed

The captured ItemSubType is `ResetTraining` (2 designs in the 2026-08-08
dynamic feed; fd97b1ee §3 / synthesis §4), but the landed
`_TRAINING_SUBTYPES` marker table contained the nonexistent string
`"reducetraining"`. Only InstantTraining designs carry @TrainingDesignId,
so a ResetTraining design could not reach the training role through either
derivation path: it fell to the default conservative-RESERVED bucket with
the reason "no protection rule fired and no role matched" instead of the
training-material classification. The bucket outcome stayed fail-closed
(RESERVED), but the classification reason was wrong for a crew-progression
design the synthesis explicitly reserves because it resets costly state.

Fix: sdk/inventory/classify.py marker corrected to "resettraining"
(lowercase compare). Regression test:
tests/test_inventory_classify.py::TestTrainingRolePaths::test_reset_training_subtype_matches_training_role,
plus test_all_captured_training_subtypes_classified asserting all seven
captured training subtypes (InstantTraining, InstantXP, ReduceFatigue,
ResetTraining, ReducePrestige, RerollBonusStats, SpecialCRISPR) land
RESERVED with the training role.

No other divergence between the captured feed vocabulary and the marker
tables was found: consumable subtypes (InstantPrize/Lootbox/Consumable/
ResourcePackage/GasPack/MineralPack/FillGasStorage/FillMineralStorage),
strategic (Immunity), ammunition (Missile) all match fd97b1ee §3 exactly.

## 4. Files added

- tests/test_inventory_classify.py — the lane-B classification-semantics
  test file staked on the blackboard (per-lane file avoids test_inventory.py
  edit collisions). 23 tests.
- docs/worker-report-inventory-t_12a30041.md — this report.

## 5. Files modified

- sdk/inventory/classify.py — the one-line marker correction above plus a
  comment noting the exact captured subtype strings. NOTE: this file is a
  lane-C hotspot (single author, flagged on the blackboard); lane B is the
  second author with this minimal corrective change, per the brief's
  "small corrective change" allowance. Lane C's 53 tests in
  tests/test_inventory_policy.py still pass unchanged.

## 6. New lane-B test coverage (beyond lane C's adversarial file)

- Both training-role derivation paths (@TrainingDesignId link; exact
  captured subtype), including the ResetTraining regression.
- STRATEGIC_RESERVE_CANDIDATE role (Immunity) — untested by lanes C/D —
  including role-survives-protection orthogonality.
- The brief's explicit multi-role example realized as a triple:
  crafting intermediate + consumable role + protected (policy) on one
  design (Golden Chest: InstantPrize + Rare + Ingredients).
- Role ordering under multi-role overlap (Role declaration order).
- Default conservative RESERVED bucket for a role-less, unprotected design
  (Decor) with its explicit reason.
- classify_catalog helper equivalence; constructor defaults; custom-policy
  injection proving composition can only ADD protection; .limitations
  lifecycle (empty before first run; deterministic; context-sensitive).
- ItemId-vs-ItemDesignId channel separation through the classifier
  (equipped set matches ItemId only, never a design id equal by value).
- Whole-surface determinism: 10-item fixture exercising every bucket,
  byte-identical across runs; ascending-ItemId output order.
- No-network-I/O test with socket blocked (brief point 24).
- Static source purity of classify.py (no requests/Client/os.environ).
- EVALUATED_RULES == (1,4,5,6,7,8,10) and NOT_EVALUATED_RULES == (2,3,9)
  as a stable output contract.

## 7. Validation

- Targeted: tests.test_inventory_classify — 23/23 OK.
- Full suite: 489 tests, OK (skipped=1) — baseline after lane D was
  466; +23 from this lane. No existing test weakened or removed.
- compileall run.py sdk — clean.
- ruff on changed files (sdk/inventory/classify.py,
  tests/test_inventory_classify.py) — All checks passed (2 unused-import
  errors in the new test file found and fixed during the run).
- git diff --check — clean.
- Note: `python -m pytest` cannot collect this suite (pytest-asyncio
  plugin Package-obj incompatibility, pre-existing); the repo-mandated
  runner is `make test` (unittest discover), which is what passed above.

## 8. Safety confirmations

- Task F (demand/reservation planning): NOT implemented. No ReservationPlan
  type exists; ClassifiedItem.reservation is always ().
- Task G (surplus/reduction): NOT implemented. available_surplus is always
  None; SURPLUS_*/TRANSFERABLE/SELLABLE/DISCARDABLE never assigned;
  forbidden roles hard-asserted.
- No mutation endpoint reachable: classify.py/policy.py contain no mutation
  endpoint names, no requests import, no Client construction (retested in
  this lane's file); `inventory apply` refusal retested by lanes C/D and
  unchanged by this lane (no commands.py edit).
- No live PSS traffic: all fixtures sanitized/synthetic; full suite mocks
  all network boundaries.
- sdk/client.py never imports sdk.inventory (unchanged, retested).

## 9. Limitations / deferred

- Marketplace value (rule 9) stays inert until ListSalesByItemDesignId is
  captured — limitation exposed, never demotes.
- Equipment/room-module context requires account-state endpoints
  (ListAllCharactersOfUser / ListRoomsViaAccessToken) not yet normalized;
  None-means-unavailable semantics already in place for them.
- Training demand (how many InstantTraining a crew plan needs) is Task F;
  this slice only classifies the material role.

## 10. Recommended next slice

Task F (demand/reservation planning) once the swarm verifier gates Task D —
it is the next approved slice in the implementation plan, and the
ClassifiedItem shape already carries the fields it will populate.
