# Task D swarm synthesis — deterministic inventory classification + protected-item policy

Swarm root / blackboard: `t_d799c340`. Verifier: `t_23ccb053` (gate=PASS, comment 25 on
the root). Synthesizer: `t_45832ade`.

This report consolidates the five worker handoffs (lanes A-D plus the verifier) into the
final Task D deliverable record. Every claim below was re-verified by the synthesizer
against the working tree at HEAD `2cb5aa4` on 2026-09-25: source files re-read, the full
validation ladder re-run, and the CLI surface re-exercised with sanitized fixtures.
Where a claim rests only on a worker report and not on code or a fresh run, the report
says so.

---

## 1. Current repository baseline

- HEAD `2cb5aa4` (mining drone slice), branch `main`.
- Dirty worktree at synthesis time: the committed-intent Slice 0/1 + Task D swarm output
  (untracked `sdk/inventory/`, `tests/test_inventory*.py`, `docs/` swarm documents,
  `INVENTORY_ENDPOINT_ANALYSIS.md`; modified `sdk/commands.py`, `sdk/client.py`,
  `sdk/security.py`, `sdk/tui.py`, `README.template`, `README.md`,
  `tests/test_runtime_guards.py`, `tests/test_tui.py`) plus unrelated `uv.lock` churn.
  No worker disturbed unrelated modifications.
- Baseline suite before Task D: 383 tests, 1 pre-existing skip (verified by lane A and
  re-confirmed in lane baselines).
- Suite after Task D: 489 tests, 1 pre-existing skip. 383 + 53 (lane C) + 30 (lane D) +
  23 (lane B) = 489; no test was weakened or removed.

Swarm narrative (for the record): lane B (`t_12a30041`, classification semantics)
staked the frozen interface contract on the blackboard, then crashed twice on
output-token limits with zero files landed. Lane C (`t_e3256e21`, protected-item
safety) landed the entire pure-domain layer to that exact frozen contract as a
contingency, so lanes D and E were not stranded. Lane B's re-dispatch then performed
the contracted VERIFY-then-test pass against lane C's landing — verifying every frozen
interface decision, finding and fixing one marker defect (see §10), and adding the
classification-semantics test file it originally staked. Lane D
(`t_b49b0534`, CLI) rendered the exported surface. The verifier ran an independent gate
from the repo at HEAD and recorded PASS.

## 2. Exact files added

Production code:

- `sdk/inventory/policy.py` — `ProtectedItemPolicy`, reason-code constants, marker
  sets, `EVALUATED_RULES` / `NOT_EVALUATED_RULES`, `policy_limitations`.
- `sdk/inventory/classify.py` — `InventoryClassifier`, `classify_catalog`, role marker
  tables, evidence tier constants.

Tests:

- `tests/test_inventory_policy.py` — 53 tests (lane C; adversarial/fail-closed file).
- `tests/test_inventory_classify.py` — 23 tests (lane B; classification semantics).
- `tests/test_inventory_plan_cli.py` — 30 tests (lane D; CLI rendering).

Documentation:

- `docs/worker-report-inventory-t_deac134c.md` (lane A integration report)
- `docs/worker-report-inventory-t_e3256e21.md` (lane C)
- `docs/worker-report-inventory-t_b49b0534.md` (lane D)
- `docs/worker-report-inventory-t_12a30041.md` (lane B)
- `docs/swarm-synthesis-task-d-t_45832ade.md` — this report.

No new files under `sdk/` outside `sdk/inventory/`. `run.py` untouched. `sdk/client.py`
untouched by Task D lanes (its dirty-worktree modification predates this swarm).

## 3. Exact files modified

- `sdk/inventory/_types.py` — additions only: `Role` (11 members, declaration order =
  stable output order), `ROLES_FORBIDDEN_THIS_SLICE`, `Reservation`,
  `ProtectionContext`, `ClassifiedItem`, and `Evidence.weaker` / `Evidence.weakest`.
  No pre-existing type (Evidence, Classification, ReservationSource, Disposition,
  ItemDesign, Item) was altered.
- `sdk/inventory/__init__.py` — export additions only (public surface listed in §4).
- `sdk/commands.py` — `inventory plan` rendering (new `_render_inventory_plan` path),
  plain-`inventory` footer correction; `inventory apply` handler byte-identical.
- `README.template` — updated first (repo rule), then `README.md` — identical
  inventory-plan sections (verified: the paragraphs match verbatim at
  `README.template:74` / `README.md:74` style positions).

## 4. Classification / domain model implemented

`ClassifiedItem` (frozen dataclass, `sdk/inventory/_types.py:148`): `item`,
`design: Optional[ItemDesign]`, `classification`, `roles: Tuple[Role, ...]`,
`protection_reasons`, `classification_reasons`, `evidence`, `unknown`,
`reservation=()` (ALWAYS, Task F), `available_surplus=None` (ALWAYS, Task G), plus a
`protected` property that is true when any protection reason fired OR the bucket is
PROTECTED/UNKNOWN (belt and braces — protection never depends on reasons being
populated).

Multi-role is first-class: `roles` is a tuple, not a category. Roles and protection are
independent axes; protection always wins the bucket. The brief's example triple is
realized and tested: a design can be simultaneously CRAFTING_INTERMEDIATE +
CONSUMABLE_USE_CANDIDATE + PROTECTED (Golden Chest fixture: InstantPrize + Rare +
Ingredients + Event tag), rendered by the plan output as
`roles: CRAFTING_INTERMEDIATE, CONSUMABLE_USE_CANDIDATE` with
`PROTECTED_RARITY, PROTECTED_EVENT_LIMITED` reasons.

Role derivation (`classify.py:_roles`, in Role declaration order):
- CREW_TRAINING_MATERIAL — `@TrainingDesignId` present OR subtype in the seven captured
  training subtypes (InstantTraining, InstantXP, ReduceFatigue, ResetTraining,
  ReducePrestige, RerollBonusStats, SpecialCRISPR).
- CREW_EQUIPMENT_CANDIDATE — Equipment subtype markers.
- CRAFTING_INGREDIENT — subtype Artifact.
- CRAFTING_INTERMEDIATE — non-empty `@Ingredients`.
- SHIP_MODULE_CANDIDATE — `@ModuleType` set or subtype Module.
- AMMUNITION_MATERIAL — ItemType Missile or subtype Missile.
- STRATEGIC_RESERVE_CANDIDATE — subtype Immunity.
- CONSUMABLE_USE_CANDIDATE — InstantPrize/Lootbox/Consumable/ResourcePackage/GasPack/
  MineralPack/FillGasStorage/FillMineralStorage.
- TRANSFERABLE_CANDIDATE / MARKETPLACE_SALE_CANDIDATE / DISCARD_CANDIDATE — domain
  vocabulary only, NEVER assigned this slice (`ROLES_FORBIDDEN_THIS_SLICE`,
  hard-asserted inside `_roles`).

Primary buckets emitted: PROTECTED | RESERVED | CONSUMABLE (+ `unknown` flag for
design-absent items). SURPLUS_USABLE / SURPLUS_CRAFTABLE / TRANSFERABLE / SELLABLE /
DISCARDABLE exist in the `Classification` enum (pre-existing vocabulary) but are never
assigned in this slice. RESERVED-WHEN-PRESENT (training, module, ammunition, strategic
reserve, and the conservative default for role-less unprotected designs) is
classification-level conservatism — no quantities are computed anywhere.

Public surface exported from `sdk.inventory`: `InventoryClassifier`, `classify_catalog`,
`ProtectedItemPolicy`, `policy_limitations`, `ProtectionContext`, `ClassifiedItem`,
`Role`, `ROLES_FORBIDDEN_THIS_SLICE`, `Reservation`, `ReservationSource`,
`Classification`, `Disposition`, `Evidence`, alongside the pre-existing Slice 1 exports.

## 5. Protection rules implemented

`ProtectedItemPolicy.evaluate(item, design, context) -> Tuple[str, ...]` returns ALL
matched reason codes in FIXED rule order (1, 4, 5, 6, 7, 8, 10) — no short-circuit;
protection stays boolean (any reason => protected) while the full reason set is
preserved for explanation. This reason-SET contract supersedes the addendum's
`Optional[str]` sketch (see §16).

From `docs/security/protected-item-policy.md` §2:

| Rule | Reason code | Fires when |
|---|---|---|
| 1 | PROTECTED_EQUIPPED | ItemId ∈ context.equipped_item_ids (set available) |
| 4 | PROTECTED_ROOM_MODULE | ItemId ∈ context.room_item_ids (set available) |
| 5 | PROTECTED_RARITY | rarity ∈ {Rare, Epic, Hero, Unique, Legendary} |
| 5b | PROTECTED_INSUFFICIENT_EVIDENCE | rarity empty or unrecognized (fail closed) |
| 6 | PROTECTED_EVENT_LIMITED | @Tags marker ∈ event/trader/harvest/stardust/vip/uber set (exact-token, case-insensitive) |
| 7 | PROTECTED_QUEST_PROGRESSION | quest/blueprint in ItemType or ItemSubType |
| 8 | PROTECTED_PREMIUM | premium/vip/uber/battlepass tags or BattlePass subtype |
| 10 | PROTECTED_UNKNOWN_DESIGN | design is None (unknown/unresolvable) |

Rules 2 (equipment-plan reservation) and 3 (elected-craft-recipe) are Task F semantics
and are NOT evaluated. Rule 9 (marketplace-valuable) requires the uncaptured
`ListSalesByItemDesignId` endpoint; it is structurally present but inert — it can never
demote an item and no marketplace value is ever invented. All three are exposed
deterministically through `policy_limitations(context)` and
`InventoryClassifier.limitations`.

`ProtectionContext` None-vs-empty semantics: `equipped_item_ids=None` means the data
is UNAVAILABLE (the rule cannot fire; limitation exposed); `frozenset()` means
positively observed empty (the rule evaluates and does not fire). ItemId and
ItemDesignId are never conflated: the sets hold instance ItemIds only, and the
collision probe (equipped ItemId 201 vs a different item whose ItemDesignId is 201)
proves the channels stay separate in both directions.

Marker sets are grounded in the captured-feed analysis (worker report `t_fd97b1ee` §3
and synthesis §4/§13): the 220-design tagged set, the rarity vocabulary, and the
blueprint/progression class. Tag matching is exact-token, case-insensitive —
adversarial substrings ("eventful", "vipguest", "ubercool") do NOT match
(regression-tested).

## 6. Evidence propagation rules

- `Evidence.weaker(a, b)` / `Evidence.weakest(tiers)` added to the declared enum order
  (strongest to weakest: VERIFIED, CAPTURED_NOT_VERIFIED, CAPTURED_AMBIGUOUS,
  STATICALLY_DISCOVERED, INFERRED, UNKNOWN). Empty input -> UNKNOWN (fail closed).
  Both helpers are strict: a non-member input is a programming error and raises rather
  than being silently coerced (coercion could silently promote a classification).
- Per-role input tiers (`classify.py` constants): item+design rows
  CAPTURED_NOT_VERIFIED (feed captured, not checksum-verified); training link
  (@TrainingDesignId) CAPTURED_NOT_VERIFIED; module role STATICALLY_DISCOVERED (module
  endpoints uncaptured); all marker-derived roles INFERRED.
- `ClassifiedItem.evidence = Evidence.weakest([row tier] + role tiers)`; a design=None
  row returns UNKNOWN outright. Output evidence is therefore the minimum confidence
  across required inputs (gate Q4) and can never be promoted above its weakest input.
  Verified by the verifier's floor probe: a module role drags the tier to
  STATICALLY_DISCOVERED; an INFERRED role drags CAPTURED rows down to INFERRED.

## 7. Unknown / fail-closed behavior

- Unknown design (ItemDesignId absent from the captured dynamic feed) -> PROTECTED +
  `unknown=True` + evidence UNKNOWN + reason PROTECTED_UNKNOWN_DESIGN. The plan renders
  it as `unresolved` with the explicit name note "design absent from captured dynamic
  design feed".
- Missing design catalog entirely -> every item resolves None -> same fail-closed path.
- Partial metadata never reduces protection: empty, whitespace, or unrecognized rarity
  ("Zealous") -> PROTECTED_INSUFFICIENT_EVIDENCE. A malformed record is never less
  protected than a complete one (tested in both directions).
- Quantity NEVER influences protection or classification (tested at -5, 0, 1, 9, 10^9,
  10^12; the plan's 999999-quantity missile stays RESERVED with protected: no).
- Uncertainty always increases protection: no path exists from missing data to
  discardable/sellable/transferable/mutation-eligible.

## 8. CLI changes

`inventory plan` (`sdk/commands.py`):

- Header: "=== Inventory plan (READ-ONLY Task D — classification, no dispositions) ==="
  plus an explicit disclaimer that classification reports what an item IS, never what to
  do with it, and that demand reservations / reduction dispositions are Tasks F/G.
- Summary: stack count, bucket counts in fixed order (PROTECTED, RESERVED, CONSUMABLE),
  zero-quantity stack count marked "never actionable".
- Per item (ascending ItemId): `item <ItemId> | design <ItemDesignId> | qty <n> |
  <bucket> | protected: yes/no | evidence <tier>` plus `| unresolved` for unknowns;
  indented sublines for design name (or the explicit unavailable note), protection
  reason codes in fixed rule order, roles in Role declaration order, classification
  reasons.
- Limitations block: the classifier's `limitations` tuple, labeled "never a demotion".
- Footer: no surplus/deficit/reservation/transfer-target/sale quantity is computed; no
  role or bucket is a mutation recommendation; `inventory apply` remains refused.
- "'candidate' marks a classification label only, never a recommendation" appears in
  the per-item preamble, satisfying the brief's candidate-terminology requirement.

`inventory` (plain view): unchanged digest format; the stale footer claim that
classification was unavailable was corrected to point at `inventory plan` (the old text
falsely said classification was a later task — it is now implemented).

`inventory apply`: byte-identical always-refusing stub (see §15).

An end-to-end CLI smoke was re-run by the synthesizer with a sanitized synthetic
fixture (unknown design, training item, 999999-quantity missile, multi-role chest,
design-absent item): all five rows render with the expected buckets, reason codes,
roles, evidence tiers, and the unresolved marker; the limitations block and
no-disposition footer render; no surplus/discard/sell/transfer token appears outside
the negation footer.

## 9. Tests added

106 new tests across three per-lane files (convention adopted to avoid
`test_inventory.py` edit collisions):

- `tests/test_inventory_policy.py` (53, lane C): policy rules (13), classifier
  classification (10), adversarial markers (6), evidence propagation (6), Task F/G
  boundaries (4), purity and safety (7), determinism (3), plus helper-shape tests.
- `tests/test_inventory_classify.py` (23, lane B): both training-role derivation paths
  including the ResetTraining regression, all seven captured training subtypes,
  STRATEGIC_RESERVE_CANDIDATE (Immunity), the brief's multi-role triple, role ordering
  under overlap, default conservative RESERVED, `classify_catalog` equivalence,
  custom-policy injection (composition only adds protection), `.limitations`
  lifecycle, ItemId-vs-ItemDesignId channel separation, whole-surface byte determinism,
  socket-blocked no-network run, static source purity, and the
  EVALUATED_RULES/NOT_EVALUATED_RULES contract.
- `tests/test_inventory_plan_cli.py` (30, lane D): full output assertions per field,
  plan determinism (byte-identical double run), no Task F/G semantics on item lines,
  plain-view immutability, apply refusal under 5 arg shapes and 5 env bypasses, and
  safety/purity (no mutation URL in commands.py source, no client->inventory import,
  sockets blocked, no env/config consultation).

Brief test-requirement points 1-24 are each covered; the mapping per point is recorded
in the lane reports (lane C §9 covers 1-24 with point 16's CLI half delegated to lane
D's TestPlanDeterminism; lane B and D files close the remainder). Two regression tests
exist for the two defects found during the swarm (see §10). All fixtures are sanitized
and synthetic; the Client is a `MagicMock(spec=Client)`; no live PSS call occurs
anywhere in the suite.

## 10. Adversarial cases covered (and defects found)

Adversarial coverage across the three test files and the verifier's independent
probes:

- unknown design -> protected; missing design catalog entirely -> everything protected
- all-fields-empty design row; whitespace rarity; unrecognized rarity
- substring-adversarial tags (eventful/vipguest/ubercool) -> no false match
- case-adversarial markers (VIP/RARE/COMMON) -> correct match
- ItemId == ItemDesignId value collision; equipped-set vs design-id conflation
  (design-id membership in the equipped set protects nothing)
- negative / zero / huge quantity (quantity independence at both policy and
  classifier level)
- None-vs-frozenset context semantics; frozen-dataclass mutation attempt
- byte-level determinism across repeated classify() and repeated plan renders
- forbidden-role hostile sweep (8 designs) -> no TRANSFERABLE/MARKETPLACE_SALE/DISCARD
- socket-blocked classification and plan runs (no network I/O attempted)
- env-var bypass attempts against `inventory apply` (7 vars tested across lanes) and
  source inspection confirming the stub reads no environment/config
- partial record never less protected than complete record
- evidence floor probes (no promotion past weakest input)

Defects found and fixed during the swarm, each with a regression test:

1. Lane C: `classification_reasons` claimed "protected by policy" on items with no
   protection reason — reasons now reflect the actual protection state. Regression:
   `tests/test_inventory_policy.py::test_reasons_reflect_actual_protection_state`.
2. Lane B (verify pass): the `_TRAINING_SUBTYPES` marker table held the nonexistent
   string `"reducetraining"` where the captured ItemSubType is `ResetTraining` (2
   designs, `t_fd97b1ee` §3). ResetTraining designs carry no `@TrainingDesignId`, so
   they fell to the default RESERVED bucket with the wrong reason (fail-closed outcome,
   wrong explanation). Fix: marker corrected to `"resettraining"` in
   `sdk/inventory/classify.py:53`. Regressions:
   `test_reset_training_subtype_matches_training_role` and
   `test_all_captured_training_subtypes_classified`. The verifier re-verified the fix in
   source and re-ran the regression.

## 11. Full validation results

Re-run by the synthesizer on 2026-09-25 from the working tree (not taken from worker
reports):

- Targeted lane tests: `tests.test_inventory_policy` (53/53 OK),
  `tests.test_inventory_classify` (23/23 OK), `tests.test_inventory_plan_cli` (30/30
  OK) — 106/106 total, matching the verifier's gate run.
- Full suite: `python -m unittest discover -s tests -p 'test_*.py'` ->
  `Ran 489 tests in 0.195s` / `OK (skipped=1)`, exit 0. The single skip is
  pre-existing (checksum-key-dependent captain test). Note: the repository-mandated
  runner is unittest discover / `make test`; pytest cannot collect this suite
  (pre-existing pytest-asyncio Package incompatibility).
- Ruff on Task D files (`sdk/inventory/`, `sdk/commands.py`, the three new test
  files): "All checks passed!". Repo-wide ruff still reports exactly 12 errors, all
  pre-existing and all in files untouched by this swarm
  (`scripts/verify_battle_against_capture.py`, `sdk/crew_leveling.py`,
  `sdk/ship_layout.py`, `tests/test_battle_flow.py`, `tests/test_game_formulas.py`,
  `tests/test_ship_layout.py`) — matching lanes A, C, D and the verifier.
- `python -m compileall -q run.py sdk`: clean.
- `git diff --check`: clean.
- `make lint`'s ty check is advisory (--exit-zero); it reports 3 diagnostics inside the
  new test files — two are the deliberate strictness tests passing bad types to
  `Evidence.weaker` under `# type: ignore[arg-type]`, one is fixture plumbing. None are
  in production code; none block.
- CLI smoke of `inventory plan` and `inventory apply` with sanitized fixtures: correct
  output (see §8), refusal intact under all attempted shapes.

Verifier gate (t_23ccb053): PASS, recorded on the blackboard. The verifier
independently re-ran the full suite (489 OK, skipped=1), the targeted lane tests
(106/106), ruff and git-diff-check on the Task D surface, and 12 adversarial probes;
it reconciled worker claims against repo truth and re-verified the ResetTraining fix in
source. Its one evidence-chain caveat: the dynamic design feed itself is not inside
`~/pss-mitm-capture.jsonl` (only ItemService traffic is), so feed-derived marker counts
rest on the t_fd97b1ee report — acceptable because any marker mismatch fails toward
RESERVED/PROTECTED (over-conservative), never under-protection.

## 12. Task F NOT implemented — confirmed

- No `ReservationPlan` or demand-planner type exists; no export carries reservation
  math.
- `ClassifiedItem.reservation` is ALWAYS the empty tuple (asserted in tests; the field
  exists only so the shape matches the addendum §3 contract for later slices).
- No training/equipment/craft/module/ammunition/strategic reservation quantities, no
  per-account or global deficits, are computed anywhere.
- RESERVED-WHEN-PRESENT buckets are classification-level conservatism with explicit
  reasons; they are not reservations.

## 13. Task G NOT implemented — confirmed

- `ClassifiedItem.available_surplus` is ALWAYS None (asserted in tests).
- No surplus/deficit/transfer-target/sale quantity is computed; the plan renders none.
- SURPLUS_*/TRANSFERABLE/SELLABLE/DISCARDABLE classifications are never assigned;
  TRANSFERABLE_CANDIDATE / MARKETPLACE_SALE_CANDIDATE / DISCARD_CANDIDATE roles are
  never assigned (hard assert in the classifier; hostile-sweep tested).
- High quantity alone never creates any surplus/discard classification (the
  999999-quantity missile probe).
- No SELL/CONSUME/DELETE/TRANSFER/CRAFT/EQUIP recommendation or action exists; nothing
  in the output is presented as an authorized action.

## 14. No mutation endpoint became reachable — confirmed

- No runtime path reaches an inventory mutation endpoint: the classifier and policy
  contain no mutation endpoint names, no requests/socket/urllib usage, no Client
  construction (source-scanned by tests; re-grepped by the synthesizer — only
  docstring mentions exist).
- No mutation enable flags, environment-variable bypasses, hidden debug modes, force
  switches, or undocumented aliases were added; the new surface consults no
  `os.environ` / `getenv` at all (re-grepped).
- `sdk/client.py` still never imports `sdk.inventory` (the only textual mention is a
  docstring at line 905); `_extract_collection` untouched; the one-way dependency
  direction (sdk.inventory -> sdk.client transport helpers) is preserved.
- `sdk/commands.py` contains no mutation URL fragments (re-grepped).
- The pre-existing `sdk/client.py:1658 activateItem3` legacy call remains covered by
  the Slice 1 T10 carve-out and is unreachable from the inventory surface.

## 15. `inventory apply` still always refuses — confirmed

- Handler byte-identical to Slice 1; registered `verified=False`.
- Re-tested by the synthesizer: refuses under `[]`, `--force`, `--yes sell 1001`,
  `now`, and a compound `--force --yes delete everything 42` shape; refuses unchanged
  with seven bypass environment variables set (`INVENTORY_ENABLE_APPLY`,
  `PSS_ENABLE_INVENTORY_MUTATIONS`, `INVENTORY_APPLY`, `PSS_INVENTORY_APPLY`,
  `TACHIKOMA_INVENTORY_MUTATIONS`, `INVENTORY_FORCE`, `PSS_FORCE_APPLY`); registry
  still reports `verified=False`.
- The stub reads no configuration, flag, or environment variable by construction;
  the refusal disappears only by deleting the stub in a future explicitly authorized
  Phase 8/9 slice.

## 16. Planning assumptions invalidated by repository truth

None rose to a BLOCK; all divergences were naming/shape-level and were settled on the
blackboard before or during implementation:

1. Module naming: the brief suggested `classifier.py`; the architecture addendum and
   every planning doc cite `classify.py` — the addendum name landed (same for
   `policy.py` vs the addendum's sketched `protection.py`). No abstraction was created
   merely to match a suggested name.
2. `evaluate` return shape: the addendum sketched `Optional[str]` (short-circuit);
   the brief's deterministic reason-code-SETS requirement won — the landed contract
   returns ALL matched codes in fixed rule order.
3. `ClassifiedItem.available_surplus: int` (addendum §3) would have leaked Task G
   semantics; landed as `Optional[int] = None`, never computed (lane A's
   recommendation, followed by lanes C/B).
4. `InventoryClassifier.__init__(reservation_plan=...)` (addendum) was correctly
   omitted — Task D must not depend on a Task F plan; constructor takes policy +
   context only.
5. The Slice 1 plan header/footer claimed classification was "not available in this
   slice (Tasks D/F/G)" — stale once Task D landed; corrected to scope unavailability
   to Tasks F/G only.
6. Lane B's plan of authoring `classifier.py` itself was invalidated by its own
   crashes; the contingency (lane C landing the frozen contract) is recorded here so
   the audit trail explains the authorship pattern (lane C authored, lane B verified
   and made one corrective line).

## 17. Known limitations intentionally deferred

- Rules 2/3 (plan-derived protection: equipment plan, elected craft recipes) — Task F
  semantics; inert, exposed as limitations.
- Rule 9 (marketplace value) — inert until `ListSalesByItemDesignId` is captured;
  never demotes, never invents a value.
- Rules 1/4 (equipped / room-module ItemId sets) — no captured source wired yet; the
  CLI constructs the default `ProtectionContext()` (None sets), so the rules cannot
  fire and the limitation is exposed in every plan render. Wiring them needs
  `ListAllCharactersOfUser` / `ListRoomsViaAccessToken` normalization.
- Evidence promotion: marker-derived roles are INFERRED by design; promotion requires
  capturing `ListItemsOfAShip` with full field enumeration (ranked #1 unblock in the
  prior synthesis §15).
- Dynamic design feed is not in the mitm capture file; feed-derived marker counts
  rest on the t_fd97b1ee report (fails over-conservative on mismatch — see §11).
- Minor, non-blocking, recorded for the next slice: the `sdk/inventory/__init__.py`
  module docstring still carries Slice 1 wording ("Classification ... intentionally
  absent") while the classification exports sit directly below it; and the repo's 12
  pre-existing ruff errors plus 3 advisory ty diagnostics in the new test files
  remain (the two ty ones on `Evidence.weaker` strictness tests are intentional).
- Environment hazard for future sessions (no repo change needed): after `uv.lock`
  churn the venv may miss runtime deps; `psycopg2-binary` does not compile under
  Python 3.13 — install requirements.txt minus psycopg2-binary (it is not imported by
  the test suite).

## 18. Recommended next implementation slice

1. Task F — demand/reservation planning (ReservationPlan, rules 2/3 activation,
  deficit math). The `ClassifiedItem`/`Reservation`/`ReservationSource` shapes already
  exist and are exported, so Task F extends rather than migrates. The verifier's gate
  note and lane reports agree on this ordering.
2. In parallel or first, capture `ListItemsOfAShip` (and later
  `ListSalesByItemDesignId`, `ListAllCharactersOfUser`, `ListRoomsViaAccessToken`) —
  capturing unblocks evidence promotion for the whole classification surface and
  wires the equipped/room sets for rules 1/4.
3. Task G (true available surplus and reduction dispositions) only after Task F,
  per the implementation plan's phase gate.

Do not fold Task F or G work into any Task D follow-up; the slice boundaries held and
should continue to hold.

---

## Machine-readable summary

```json
{
  "swarm": "t_d799c340",
  "slice": "Task D — deterministic inventory classification + protected-item policy",
  "synthesizer": "t_45832ade",
  "verifier_gate": "PASS (t_23ccb053, blackboard comment 25)",
  "repo_head": "2cb5aa4",
  "files_added": [
    "sdk/inventory/policy.py",
    "sdk/inventory/classify.py",
    "tests/test_inventory_policy.py",
    "tests/test_inventory_classify.py",
    "tests/test_inventory_plan_cli.py",
    "docs/worker-report-inventory-t_deac134c.md",
    "docs/worker-report-inventory-t_e3256e21.md",
    "docs/worker-report-inventory-t_b49b0534.md",
    "docs/worker-report-inventory-t_12a30041.md",
    "docs/swarm-synthesis-task-d-t_45832ade.md"
  ],
  "files_modified": [
    "sdk/inventory/_types.py",
    "sdk/inventory/__init__.py",
    "sdk/commands.py",
    "README.template",
    "README.md"
  ],
  "tests": {"baseline": 383, "after": 489, "new": 106, "skipped": 1, "weakened": 0},
  "validation_rerun_by_synthesizer": {
    "targeted_lane_tests": "106/106",
    "full_suite": "Ran 489 tests, OK (skipped=1)",
    "ruff_task_d_files": "All checks passed",
    "ruff_repo_preexisting": 12,
    "compileall": "clean",
    "git_diff_check": "clean",
    "cli_smoke": "plan + apply refusal verified with sanitized fixtures"
  },
  "task_f_implemented": false,
  "task_g_implemented": false,
  "mutation_endpoints_reachable": false,
  "inventory_apply_always_refuses": true,
  "defects_found_and_fixed": 2,
  "hotspots": [
    "sdk/inventory/classify.py (lane C authored, lane B one corrective line)",
    "sdk/inventory/policy.py (single author lane C)",
    "sdk/commands.py (lane D sole author of plan rendering)"
  ],
  "next_slice": "Task F (demand/reservation planning); capture ListItemsOfAShip to promote evidence"
}
```
