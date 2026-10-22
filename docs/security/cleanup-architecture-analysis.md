# Cleanup Architecture and Test Plan (Inventory)

Swarm lane: `t_457d0913` (cleanup).
Swarm root / shared blackboard: `t_39a1fbda`.
Sibling lanes: researcher (`t_315b6cb2`), protocol (`t_9f78e590`),
safety (`t_9eec6b78`), consumables (`t_f6a9edd1`).

> Note on card body: as with the other lane cards, the Kanban body for
> this card carries the unrelated Opnory Gate 3 / SEC-02 template text
> in error. The operative statement of work is the card title
> ("Inventory cleanup architecture and test plan") plus the swarm root
> and the researcher lane's handoff, which defines the cleanup lane's
> scope around the inventory collection architecture that lane shipped.

## 1. What this lane actually changed

Two defects were found in the tree left by the researcher lane and the
pre-existing test suite. Both are fixed in-tree (not committed — commit
is outside this lane's mandate; leave that to the orchestrator/human).

### Fix 1 — duplicate `collectAllResources` definition (F811)

The researcher lane (t_315b6cb2) added a hardened
`collectAllResources` implementation but left the legacy definition in
place. Python keeps the *second* definition, so the old one was dead
code and lint failed with `F811 Redefinition of unused collectAllResources`.

- Removed: `sdk/client.py` legacy `collectAllResources` (was ~line 1759).
- Retained: researcher-lane `collectAllResources` (near line 3003),
  which preserves the legacy mineralTotal/gasTotal/credits parsing
  pinned by `tests/test_e2e_live_fixes.py` and adds `errorMessage=`
  detection plus an accessToken guard.

Regression coverage: the 3 `test_e2e_live_fixes.test_collect_all_resources_*`
tests pin the legacy parsing semantics against the retained
implementation — they fail if the parsing regresses.

### Fix 2 — `TestExitCodeAggregation` silently tested nothing

The 3 "pre-existing failures on clean HEAD" reported by the researcher
lane were not environment flakiness. Root cause:

`run.py`'s automation block is gated by
`if not getattr(client, "_automation_done", False):` (run.py:279). The
tests mock the client with a `MagicMock`; `getattr(mock, "_automation_done",
False)` returns a truthy auto-created child mock, so the gate evaluated
False and the entire block under test (`upgradeResearches` /
`upgradeRooms` / `manageTraining` …) never executed. `runtime_failed`
stayed False and `main()` exited 0, while the tests expected exit 1.

The tests were vacuous: they passed the "exit 0" case and "failed" the
exit-1 cases for the wrong reason, and they could not have caught a
sequencing regression.

Fix: set `mock_cli_inst._automation_done = False` in all four
`TestExitCodeAggregation` tests (including the previously-passing
success test, which now genuinely exercises the loop).

Regression coverage: the 4 tests now verifiably enter the automation
block — the exit-1 tests additionally assert that
`manageTraining`/`upgradeRooms` were still called after the injected
failure. Full suite: 347 tests, OK (skipped=1), matching the prior
count with 0 failures.

## 2. Cleanup architecture decisions (answering sibling lanes)

### Researcher handoff — adopted as-is

- Daily bulk collection uses `CollectAllResources` (no checksum, single
  call) via `collectRoomResources`; per-room `CollectResources3` is the
  fallback only when the bulk call fails. No new cleanup path was added
  and none should be — this layering is correct per the researcher
  lane's capture data (CollectAllResources needs no checksum; the
  per-room endpoint costs one checksum computation per room).
- No code path was added that bypasses the two-tier structure.

### Safety lane open question — resolved

> Should `collectRoomResources` remain H0 when called on ammunition
> rooms?

**Yes, H0.** `collectRoomResources` only ever calls
`CollectAllResources` (H0: no checksum, collects already-manufactured
goods) and falls back to `CollectResources3` (also collection-only).
It never invokes a manufacturing, purchase, or destruction endpoint.
Confirmed against `sdk/client.py` implementation: no mutation of room
production state, ammo counts, or currency. The H0 classification in
`docs/security/mutation-safety-analysis.md` stands.

Additional confirmation for the safety lane: **no H3 endpoint is
implemented in `sdk/client.py`** — `DeleteItem3`, `ConsumeItem2`,
`SellItem3` have no client methods. The cleanup lane deliberately added
none (consumables-lane/safety-lane guidance: H3 purge paths stay
manual-only, and an autonomous inventory-pressure relief valve must not
be created).

### Consumables lane notes for cleanup — disposition

1. *"Do not add an autonomous SellItem3/DeleteItem3 cleanup path."*
   Honored. Nothing added. `collectTaskReward` returning False on full
   storage is logged ("expected if storage full") and the loop
   continues — matching the consumables lane's recommendation to log
   and defer.
2. *"`useConsumable` calls must remain send-once per decision."*
   Confirmed in current code: `useConsumable` → `activateItem3` is
   invoked once per matched item inside `manageTraining`'s decision
   pass; there is no retry wrapper around it. No change made. Any
   future retry logic added around H2 endpoints must preserve
   send-once semantics (safety-lane rule 2).

### Consumables lane's answer on BuyReward2 — acknowledged

The consumables lane confirmed `BuyReward2`'s minimal checksum
(`MD5(clientDateTime + ChecksumKey + SavyChecksum)`) is its own family
and must not be generalized. The cleanup lane touches no checksum code;
`checksum_collect_resources3` (researcher lane, 14/14 captures) is
retained verbatim.

## 3. Known under-guarded H2 endpoints (NOT fixed here — out of scope)

These are architectural gaps the safety/consumables lanes identified
that remain open. Fixing them changes autonomous spending policy and
needs its own slice with explicit acceptance criteria — a cleanup lane
fixing them silently would be a scope violation:

1. `speedUpTravelling` — gated only by a "<5 min remaining" heuristic,
   no `PSS_MAX_STARBUX_PER_RUN` budget cap (safety rule 4).
2. `purchaseDrawWithStarbux` — no affordability check inside the
   method; relies on caller discipline.
3. `useConsumable` — no allow-list of itemDesignIds; name/substring
   matching is fragile (consumables lane guard gap 1 & 2); no
   `PSS_MAX_CONSUMABLES_PER_RUN` budget.

Recommendation: file one follow-up slice titled
"Implement PSS_MAX_STARBUX_PER_RUN / PSS_MAX_CONSUMABLES_PER_RUN budget
caps + useConsumable allow-list" with safety-lane rule 4 as the
acceptance criteria.

## 4. Test plan for the collection architecture

Already in place and passing (347 tests total):

- `tests/test_inventory_collection.py` (researcher lane):
  `_findResourceRooms` parsing, `collectResources3` success/failure/
  checksum-missing paths, `collectAllResources` parsing variants,
  `collectRoomResources` bulk-vs-fallback strategy.
- `tests/test_e2e_live_fixes.py::test_collect_all_resources_*` (3
  tests): pin legacy mineralTotal/gasTotal/credits parsing semantics.
- `tests/test_checksum_security.py`: `checksum_collect_resources3`
  against capture-verified vectors.
- `tests/test_runtime_guards.py::TestExitCodeAggregation` (fixed by
  this lane): genuinely exercise the run.py automation block, verify
  exit-code aggregation and that later steps still run after a failure.

Remaining test gaps worth a future slice:

- No test exercises `collectRoomResources` when `getShipByUserId`
  raises (currently returns 0 — behavior exists, unpinned).
- No negative test that `collectResources3` refuses to fire without
  checksum settings (the early-return branch) — partially covered;
  explicit assertion of *zero HTTP calls* would strengthen it.
- Integration-level test that a full-storage signal from
  `collectTaskReward` does not trigger any retry of H2 endpoints
  (currently guaranteed structurally, not by test).

## 5. Validation performed by this lane

- `python3 -m unittest discover -s tests -q` → Ran 347, OK (skipped=1).
  Suite now matches main parity **and** the 3 previously-failing
  TestExitCodeAggregation tests pass for genuine reasons.
- `uv run ruff check sdk/client.py sdk/security.py tests/test_runtime_guards.py`
  → All checks passed.
- `git diff --check` → clean.
- Note: repo-wide `make lint` still reports 12 pre-existing ruff errors
  in files untouched by any lane in this swarm (`sdk/crew_leveling.py`,
  `sdk/ship_layout.py`, several test files, `scripts/`). Left intact —
  out of this lane's path scope.

## 6. Capture availability note

No packet-capture data exists on disk in this workspace, so no
capture-replay verification was possible from this lane. Checksum
confidence for `CollectResources3` rests on the researcher lane's
reported 14/14 capture verification, recorded in
`INVENTORY_ENDPOINT_ANALYSIS.md`. If a fresh capture of
`CollectAllResources`/`CollectResources3` becomes available, the
protocol lane (`t_9f78e590`) owns re-verification.
