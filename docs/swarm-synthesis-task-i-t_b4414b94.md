# Task I swarm synthesis — deterministic, fail-closed, read-only cross-account inventory transfer planning

Swarm root / blackboard: `t_7842f6df`. Verifier: `t_232a3d8d`
(gate=PASS, blackboard comment "verifier-gate: PASS", completed with
metadata `{"gate":"pass"}`). Synthesizer: `t_b4414b94` (this card).

This report consolidates the five lane handoffs (reconcile/evidence,
planner, adversarial, safety, UX/CLI) plus the verifier gate into the
final Task I deliverable record. Every load-bearing claim below was
re-verified by the synthesizer against the working tree at HEAD
`2cb5aa4` on 2026-10-02: the full validation ladder re-run (1013
tests, Ruff on the touched surface, ty on the new module,
`git diff --check`), the 184 targeted transfer tests re-run, a fresh
10-probe synthesizer suite written from scratch (purity under socket
block, matching arithmetic with independent capacity accounting,
ItemId-collision join isolation, stale-capacity semantics, the constant
hold on structurally perfect inputs, loader rejection of tampered
unmet, duplicate-account abort, 24-permutation byte-identity, CLI e2e
through the real CommandRegistry including the apply refusal, and a
binary-safe corpus scan of all 6 capture files), plus README parity.
Where a claim rests only on a worker report and not on code or a
fresh run, the report says so.

Swarm narrative (for the record): the reconcile lane (`t_97ae279f`)
landed first and pinned Task I against repository truth and the Task
H synthesis (docs/task-i-reconciliation-t_97ae279f.md: ItemDesignId-
only joins, the TransferPlanner input contract, the GiftItem corpus
verdict — 0 captures — and the resulting transferability decision).
The planner lane (`t_681185bf`) built the core: pure
`sdk/inventory/transfer.py` (`plan_transfers` + `render_transfers_view`)
plus 54 tests and the export surface, catching 4 defects during
self-review (GiftItem call-token safety-scan trip, cross-design
evidence bleed, gate-failing pairs consuming capacity, a mis-stamped
promotion reason). The adversarial lane (`t_628172e7`) differential-
fuzzed the planner against a from-scratch reference allocator (60
randomized portfolios, exact match on quantities, reason codes, and
row ordering) plus a 300-trial stress fuzz with independent capacity
accounting, and shipped 52 tests; zero production defects found. The
safety lane (`t_6a3ef050`) probed nine fail-closed axes (stale,
protected, unresolved, unverified, no-GiftItem-path, no-network,
no-mutation, forged inputs, non-actionability) and shipped 56 tests;
it also corrected the reconcile lane's corpus wording — 74
ToUserId/FriendUserId hits are MessageService/UserService chat and
friend-list response bodies, zero in any ItemService/MarketService
record — a correction this synthesis adopts. The UX lane
(`t_7ed909c9`) wired `inventory transfers` as a thin dispatch inside
the existing `inventory` verb (commands.py adds only a dict lookup
plus the caller-owned clock read), updated the TUI's multi-word verb
resolution so the `inventory apply` refusal stub is no longer
shadowed, and shipped 22 CLI tests plus README.template/README.md.
The verifier (`t_232a3d8d`) ran 20 from-scratch probes (200-trial
fuzz, boundary pins, provenance, 24-permutation determinism,
isolation, apply refusal) with zero planner defects and re-ran the
full ladder: 1013 OK, 184/184 targeted, clean ruff/ty/diff-check. The
synthesizer re-ran everything; all numbers matched. Net: **zero
production defects were found by any lane, the verifier, or the
synthesizer after the planner lane's own self-review** — the first
swarm in this series to land with no cross-lane defect discovery,
which is consistent with Task I building on an already-hardened
Task H loader surface.

## 1. Repository baseline / HEAD

- HEAD `2cb5aa4` (mining drone slice), branch `main`. No commits were
  made by any lane, the verifier, or the synthesizer; the entire Task
  I output lives in the dirty worktree alongside the Slice 0/1,
  Task D, Task F, Task G, and Task H output, uncommitted by design.
- Suite before Task I: 829 tests (post-Task H). Suite after Task I:
  1013 tests. 829 + 54 (planner) + 52 (adversarial) + 56 (safety) +
  22 (CLI) = 1013; no test was weakened or removed.
- Task I touched: `sdk/inventory/transfer.py` (new, 624 lines),
  `sdk/inventory/__init__.py` (Task I export surface),
  `sdk/commands.py` (514 -> 530 lines: transfers dict entry,
  description text, and the same fail-closed view wrapper as
  accounts/deficits), `sdk/tui.py` (multi-word verb resolution),
  `README.template` + `README.md` (header-only delta between them,
  re-verified by the synthesizer), and four new test files
  (184 tests total).
- Pre-existing (NOT this swarm's): 10 ruff F401/F811 errors in
  `sdk/crew_leveling.py`, `sdk/ship_layout.py`,
  `tests/test_game_formulas.py`, `tests/test_ship_layout.py` —
  committed files untouched by Task I. The 4 ty diagnostics in
  `sdk/commands.py` `_cmd_device` pre-exist at HEAD (UX lane verified
  against `git show HEAD`). Neither blocks the Task I gate: `make
  lint` scope is repo-wide while the brief's ruff requirement is the
  touched surface, which is clean.

## 2. Files added

Production code:

- `sdk/inventory/transfer.py` — 624 lines: the pure TransferPlanner
  (`plan_transfers`), the proposal/plan records, the eight-
  requirement eligibility gate, the constant hold, and the read-only
  `render_transfers_view`. No network, no filesystem, no clock; `now`
  is caller-supplied. Single import of Task H surfaces
  (`snapshot_freshness`, `STALE_AFTER_SECONDS`, `PortfolioViewError`)
  — no freshness logic duplicated.

Tests (184 across 4 files, counts re-derived by the synthesizer via
`grep -c "def test_"` and confirmed by a targeted run):

- `tests/test_inventory_transfer.py` — 54 (planner lane).
- `tests/test_inventory_transfer_adversarial.py` — 52 (adversarial
  lane: reference-allocator differential fuzz, capacity invariants,
  gate bypasses, determinism, provenance, isolation).
- `tests/test_inventory_transfer_safety.py` — 56 (safety lane: nine
  axes T1-T9).
- `tests/test_inventory_transfers_cli.py` — 22 (UX lane: dispatch,
  byte-parity with the pure renderer, help paths, whole-view refusal,
  thin-wrapper AST pins, apply refusal untouched).

Documentation:

- This report. (The reconcile lane's requirements doc is
  `docs/task-i-reconciliation-t_97ae279f.md`; the safety lane's
  report is `docs/task-i-safety-t_6a3ef050.md`; each lane's full
  handoff lives in its blackboard comment on the root. The
  synthesizer's probe suite is preserved at
  `workspaces/t_b4414b94/probes/synthesizer_probes.py`; the verifier's
  at `workspaces/t_232a3d8d/probes/verify_task_i.py` under the
  verifier profile's scratch.)

## 3. Files modified

- `sdk/inventory/__init__.py` (249 lines) — Task I export surface:
  `TransferProposal`, `TransferPlan`, `TransferPlannerError`,
  `plan_transfers`, `render_transfers_view`, the reason-code
  constants, `HOLD_UNVERIFIED_TRANSFERABILITY`, `ELIGIBLE` (vocabulary
  only — never assigned), and `GIFTITEM_EVIDENCE_TIER`. No
  pre-existing export altered. Synthesizer confirmed every Task I
  symbol the lanes name appears in `__all__`.
- `sdk/commands.py` (514 -> 530) — `inventory transfers` dispatches
  through the SAME `_render_portfolio_view` wrapper as accounts and
  deficits: a dict lookup adds `render_transfers_view` as the third
  renderer, the wall-clock read stays the caller-owned single seam,
  and the whole-view fail-closed refusal (malformed / duplicate /
  secret-bearing / unreadable snapshot file names the file and
  refuses everything) is shared unchanged. The verb description and
  unknown-subcommand help list `transfers`. The deferred import at
  line 445 imports ONLY `render_transfers_view` from the transfer
  family — `plan_transfers` is not reachable from commands.py
  (verifier AST pin, re-verified by the synthesizer's read of the
  import block). `inventory apply` handler content unchanged
  (constant REFUSED return).
- `sdk/tui.py` — the REPL loop now resolves multi-word verbs
  longest-registered-prefix first, so `inventory apply` (the
  registered refusal stub) is reachable instead of being shadowed by
  the `inventory` verb; unregistered second words fall back to the
  single-word verb with arguments preserved. `_cmd_help` resolves the
  same way.
- `README.template` — updated first per the repo rule, then
  `README.md`: documents the `inventory transfers` verb, the
  ItemDesignId-only join, the informational matched arithmetic, the
  GiftItem gap, and the refusal semantics. Synthesizer re-verified
  the two files differ only by the GitHub-hook header.
- `tests/test_inventory_transfer_safety.py` — UX-lane
  prose reconciliation of the T6/T7 pins (the safety lane's §6
  handoff): the docstring/comment wording now states the renderer-
  only import contract; the assertions themselves were already
  compatible and pass unchanged.

## 4. The TransferPlanner surface

`plan_transfers(portfolio, now, stale_after=STALE_AFTER_SECONDS)` is
pure, deterministic, and input-order independent. It accepts only a
validated `AccountPortfolio` (anything else refuses), re-validates
every snapshot through `snapshot_from_dict(snapshot.to_dict())` — the
same discipline as `aggregate_snapshots`, so a hand-built or forged
instance cannot bypass the Task H loader gates — and aborts the
whole plan on duplicate user_id or any re-validation failure. No
partial plans exist.

Join key: **ItemDesignId only.** The planner consumes per-(account,
design) facts re-derived from each snapshot (design rows, stack
rows, conditions); ItemId never enters a join, key, equality test,
or the output — pinned by tests and re-proved by the synthesizer
with an ItemId-collision fixture (two accounts holding the same
ItemId on the same design keep separate supply ledgers; the
serialized plan contains no `item_id` key).

Matching arithmetic (all pinned by planner + adversarial tests and
re-proved by the synthesizer's P2/P3/P4):

- Usable source supply per (account, design) = the account's Task G
  candidate total for that design MINUS candidates on
  protected/unknown stacks. A candidate is an informational dry-run
  number, never automatically transferable; a negative result from
  the subtraction aborts the whole plan (fail closed, never a silent
  clamp).
- Destination deficit = `design.unmet_quantity > 0` (loader-pinned
  as max(0, requested − observed)).
- Partition rule: an account with unmet demand on a design is never
  a source for it, and a source must hold stacks of the design.
- Per design (ascending), destinations in ascending user_id order,
  each offered from every eligible source in ascending user_id
  order; `matched = min(remaining supply, remaining deficit)`, both
  sides decrementing across matches. Gate-failing pairs consume NO
  capacity — a stale pairing must not eat the supply a fresh
  pairing could use (synthesizer probe P4). Nothing is
  oversubscribed and nothing is double-filled; bounds
  0 <= matched <= supply and <= deficit hold by construction.
- Every attempted (destination, source) pair is emitted as a row
  even at matched 0 — the planner reports attempts, never silently
  drops them.

Determinism: proposals sort by (item_design_id, destination_user_id,
source_user_id); reasons sort lexicographically; all 24 permutations
of a 4-account portfolio produce byte-identical serialized plans
(synthesizer probe P8; verifier and adversarial lane the same).

## 5. The eligibility gate (fail closed)

Eight requirements per (source, destination, design) match; ANY
unmet requirement forces a HOLD row with the specific reason codes:

1. DISTINCT_ACCOUNTS — user_id inequality; self-transfer is
   structurally impossible (duplicate user_id aborts) but the gate
   stays explicit.
2. POSITIVE_SOURCE_SUPPLY — usable supply > 0.
3. POSITIVE_DESTINATION_DEMAND — unmet > 0.
4. FRESH_SNAPSHOTS — BOTH sides fresh via `snapshot_freshness`
   (STALE_AFTER_SECONDS=86400; age == threshold is fresh, one
   second past is stale). A stale side suppresses the pairing.
5. NO_APPLICABLE_UNRESOLVED_CONDITION — no unresolved condition for
   that design in EITHER side's OWN snapshot
   (`conditions_for(design_id)`, design-scoped only; account-global
   SOURCE_UNSUPPORTED emissions are account-level annotations that do
   not block, matching the Task H portfolio precedent). ALL
   design-scoped codes are applicable — the union, not an
   enumerated allowlist, so a future condition code fails closed too.
   Consequence pinned by both the adversarial lane and the verifier:
   the real pipeline stamps DEMAND_EXCEEDS_SUPPLY design-scoped on
   every deficit destination, so real-data pairings always fail
   closed to matched 0. Intended conservative behavior, not a bug.
6. NO_PROTECTION_CONFLICT — no protected/unknown stack of the
   design on EITHER side (fail-closed direction: even though usable
   supply already excludes protected candidates on the source, a
   protected stack anywhere in the pairing suppresses it).
7. NO_EVIDENCE_PROMOTION — the proposal's evidence is
   `Evidence.weakest` over both sides' design/stack/candidate tiers
   for that design plus the GIFTITEM_EVIDENCE_TIER floor
   (STATICALLY_DISCOVERED). Structurally satisfied; never stamped
   as an unmet reason; never promoted above the weakest input;
   never bleeds across designs or accounts.
8. TRANSFER_ELIGIBILITY_UNPROVEN — no repository-supported policy or
   verified metadata establishes transferability (GiftItem
   uncaptured). Constant this slice; stamped on EVERY proposal.

## 6. The fail-closed outcome (the point of Task I)

Every proposal, unconditionally: eligibility_state
`HOLD_UNVERIFIED_TRANSFERABILITY`, proposed_quantity 0,
actionable=False. `ELIGIBLE` exists in the vocabulary for the future
capture-verified slice and is never assigned — the verifier proved
it unreachable, and the synthesizer's perfect-inputs probe (P5)
confirms the constant hold with `[REASON_TRANSFER_ELIGIBILITY_-
UNPROVEN]` as the ONLY reason on a structurally perfect pair.
`matched_quantity` is informational dry-run arithmetic, NOT a
proposed move; it is 0 whenever any structural gate requirement is
unmet. `actionable_proposals()` is structurally empty. The plan
carries no route, no endpoint, no executor, no retry, no sequencing.
A GiftItem capture is the named unblock condition (§10).

## 7. The GiftItem corpus verdict (the grounding for the hold)

Four independent scans agree — reconcile lane, adversarial lane,
safety lane, verifier — and the synthesizer ran a fifth: a
binary-safe, case-insensitive scan of all 6 capture files
(50,755 newline records, 786,144,705 bytes — byte-identical count to
the verifier's):

- `giftitem` / `gift_item` / `gift-item`: 0 / 0 / 0
- `sellitem3`, `purchase4`, `marketservice`: 0 / 0 / 0
- `transferitem` / `transfer_item`: 0 / 0

The ItemService endpoints actually seen are ActivateItem4,
CombineItem4, ConsumeItem2, FixUnsynchronizedItems, MarkItemsNotNew
— none transfer. The 67 bare `gift` substring hits decompose
entirely into non-protocol content (random-token noise, LiveOps
fleetgift sale config, Unity analytics flags, localization strings,
sprite atlases, one PNG payload). GiftItem remains
STATICALLY_DISCOVERED with a truncated URL template
(`/ItemService/GiftItem?itemId=`): recipient parameter, quantity
semantics, and checksum preimage are ALL unknown.

Wording correction adopted from the safety lane (their §2.1): the
reconcile report's "zero recipient-param hits" is imprecise —
`ToUserId` appears in 60 records and `FriendUserId` in 14, all 74
classified as MessageService/UserService chat and friend-list
response bodies. Zero occur in any ItemService or MarketService
record; none describe a transfer request parameter. The safety
conclusion is unchanged: transferability is unprovable from the
corpus, so HOLD_UNVERIFIED_TRANSFERABILITY, quantity 0,
actionable=false on every proposal is the only defensible state,
and the GiftItem gap is documented in the view footer as the named
blocker.

## 8. The `inventory transfers` view (read-only CLI)

`render_transfers_view(snapshots, now)` (pure, in transfer.py) is
the single render surface. `inventory transfers` dispatches through
the shared `_render_portfolio_view` wrapper in commands.py:

- Per proposal row (ascending design, destination, source): source
  account, destination account, ItemDesignId, proposed quantity
  (always 0), matched (informational), weakest-input evidence,
  per-side freshness (fresh/STALE), HOLD_UNVERIFIED_TRANSFERABILITY,
  sorted reasons, actionable=false. The footer documents the GiftItem
  zero-capture gap, the unblock condition, and that `inventory apply`
  remains refused.
- Whole-view fail-closed: malformed JSON / duplicate user_id /
  secret-bearing / unreadable snapshot each refuses the ENTIRE view
  naming the offending file. Empty or missing snapshots dir renders
  an explicit empty state, never an error.
- Header wording pins the Task G/H invariants: "Candidate does not
  mean transferable.", accounts matched only by ItemDesignId, ItemId
  never cross-account identity, STALE markers never change evidence.
- CLI byte-parity with the pure renderer is pinned by test
  (test_registry_pairing_output_matches_pure_renderer).
- `help inventory` reaches the verb; the unknown-subcommand path
  lists it. The synthesizer's e2e probe drove the real
  CommandRegistry over a temp snapshots dir: full pairing row,
  footer, and refusal text all render.

`inventory apply` remains an unconditional refusal — now MORE
reachable, not less: the TUI fix means the registered refusal stub
is no longer shadowed by the `inventory` verb, and the refusal is
args/config/env-independent (synthesizer probe re-proved with
--force --yes via the multi-word verb).

## 9. Safety properties (the mandated axes)

- No network I/O in sdk/inventory: transfer.py imports only
  dataclasses/typing/`__future__` and local pure modules (AST
  proven); planner + renderer complete under a blocked-socket
  namespace at runtime (synthesizer probe P1); `sdk/client.py`
  imports nothing from `sdk.inventory`.
- No clock reads inside sdk/inventory: `now` is caller-supplied;
  the CLI's single wall-clock read is the same caller-owned seam Task
  H established, and the CLI test mixin pins it so freshness
  assertions never decay with the calendar (the SYN-H1 lesson).
- No mutation reachability: `plan_transfers` is not imported by
  commands.py; no registered verb contains "transfer" (transfers is
  a subcommand under the existing `inventory` verb); no Client
  construction, no `.request(`, no GiftItem/SellItem3/Purchase4
  call site, no URL/checksum/recipient payload anywhere in
  transfer.py (the renderer's quoted `'?itemId='` truncation
  fragment is the documented gap, explicitly allowed); the apply
  stub is a single unconditional return — no If, no settings/env
  access — surviving armed config (`inventory.enabled=true`) and
  armed env (`ENABLE_MUTATION=1`).
- Forged inputs fail closed: duplicate user_id aborts the whole
  plan; a tampered snapshot is caught by loader re-validation at
  aggregate time (synthesizer probe P6: unmet
  != max(0, requested − observed) rejected); non-portfolio inputs,
  bad `stale_after` types, and actionable=True flags on hand-built
  inputs are all refused or ignored.
- Non-actionability everywhere: serialized plans carry proposed=0 /
  actionable=false on every row and no `item_id` key; the renderer
  never emits a route or executable step.
- Prohibited work NOT done (verified absent by source/AST scans and
  symbol searches): no GiftItem execution, no SellItem3, no
  Purchase4, no marketplace self-transfer, no mutation
  executor/reconciler, no retry logic, no transaction sequencing, no
  endpoint enablement, no credential persistence, no
  premium-currency spending. No live traffic was generated by any
  lane, the verifier, or the synthesizer (the corpus scan ran over
  existing files only).

## 10. GiftItem gap register (documented, unblock conditions)

| Gap | Evidence | Consequence |
|---|---|---|
| GiftItem: zero captures; template truncated at `?itemId=`; recipient param, quantity semantics, checksum preimage unknown | 5 independent corpus scans (this synthesis §7) | HOLD_UNVERIFIED_TRANSFERABILITY, qty 0, actionable=false on every proposal; unblock = capture + dual-account reconcile |
| ListItemsOfAShip: zero captures (Task H synthesis §12) | Task H synthesizer scan, re-confirmed | feed evidence capped at CAPTURED_NOT_VERIFIED; observed quantities never VERIFIED |
| Marketplace chain (SellItem3/ListSalesByItemDesignId/Purchase4): zero captures | corpus scans: 0 MarketService records | no alternative transfer route can be cited |
| No recipient-identifier semantics in any ItemService capture | safety lane §2.1 classification | recipient targeting unverifiable; a wrong-account gift is unrecoverable |

## 11. Tests added in Task I

184 new tests across 4 files:

- `tests/test_inventory_transfer.py` — 54: matching arithmetic,
  gate requirements, evidence floors, freshness, protection,
  conditions, self-transfer rejection, no-ItemId joins,
  isolation, determinism, renderer output.
- `tests/test_inventory_transfer_adversarial.py` — 52: a
  from-scratch reference allocator differential-fuzzed against
  plan_transfers (60 randomized portfolios: exact match on
  quantities, reason codes, ordering) plus a 300-trial stress fuzz
  with independent capacity accounting, all-24-permutation
  determinism, ItemId-collision joins, hand-built-portfolio attacks,
  apply-refusal integration.
- `tests/test_inventory_transfer_safety.py` — 56: nine axes (T1
  stale, T2 protected, T3 unresolved, T4 unverified, T5 no-GiftItem-
  path, T6 no-network, T7 no-mutation, T8 forged inputs, T9
  non-actionability), derived probe-first from the brief, not from
  sibling test files.
- `tests/test_inventory_transfers_cli.py` — 22: dispatch,
  registry/pure-renderer byte parity, accounts/deficits unchanged,
  help paths, every-row hold/qty0/actionable=false, stale-side
  blocking, no ItemId in the view, GiftItem footer, empty/missing
  dir states, whole-view refusals, thin-wrapper AST pins,
  socket-blocked runtime, apply refusal untouched.

Adversarial coverage spans every axis the brief named: self-transfer
rejection, stale accounts, malformed/duplicate snapshots, unknown
designs, protected items, unresolved applicable demand, unverified
transferability, evidence floors, deterministic matching, multiple
donors, multiple recipients, no oversubscription, no double-fill,
no ItemId cross-account matching, no network I/O, no mutation
reachability, all actionable=false, and inventory apply refusal.

## 12. Full validation results

Re-run by the synthesizer on 2026-10-02 (matching the verifier's
independent gate run):

- `make test` (canonical gate): **Ran 1013 tests, OK (skipped=1)**.
  Baseline 829 + 184 new; no test weakened or removed.
- Targeted: `python -m unittest tests.test_inventory_transfer
  tests.test_inventory_transfer_adversarial
  tests.test_inventory_transfer_safety
  tests.test_inventory_transfers_cli` — **Ran 184 tests, OK**.
- `ruff check` on the Task I surface (transfer.py,
  `sdk/inventory/__init__.py`, commands.py, tui.py, all 4 transfer
  test files): **All checks passed**.
- `ty check sdk/inventory/transfer.py`: **All checks passed**.
- `git diff --check`: **clean**.
- Synthesizer probe suite (fresh interpreter, project venv, no
  worker helpers): **10/10 PASS** — purity under socket block;
  no-oversubscription/no-double-fill with independent capacity
  accounting; ItemId collision isolation; stale-pair-consumes-no-
  capacity; perfect-inputs-constant-hold; tampered-unmet loader
  rejection; duplicate-user abort; 24-permutation byte-identity;
  real-CommandRegistry e2e (transfers row + footer + apply
  refusal with --force --yes); corpus scan (50,755 records,
  786,144,705 bytes, all 8 needles zero — identical to the
  verifier's count).
- README parity: README.template vs README.md differ only by the
  GitHub-hook header.
- Verifier probe suite: 20 from-scratch probes, ALL PASS, 0
  planner defects (verifier's own run, run 116; the synthesizer
  did not re-run the verifier's script and rests on the verifier's
  preserved probe file plus its gate metadata).

Note: bare `pytest` collection remains broken by the pre-existing
pytest-asyncio 0.23.0 vs pytest 8.3.0 incompatibility; `make test`
(unittest) is the canonical gate, as established in earlier slices.

## 13. Defects found during the swarm

**Zero production defects** were found by the adversarial lane, the
safety lane, the verifier, or the synthesizer. The planner lane
caught 4 defects in its OWN code during self-review before landing
(documented in its handoff): a literal `GiftItem(` token that would
have tripped the package-wide safety scan; evidence inputs that
weighed ALL of an account's designs (provenance bleed); matching
that decremented supply for gate-failing pairs (stale pairings
would have eaten fresh capacity); and a promotion reason stamped as
unmet though structurally satisfied. All four are pinned by landed
tests.

Probe-harness bugs fixed by the probers themselves (test-side only,
never production): the verifier fixed 3 (deficit fixtures violating
the reserved<=observed loader gate, comment-only token matches,
frozen-dataclass mutation); the safety lane fixed 3; the
adversarial lane fixed 3; and the synthesizer's own first probe
draft hit the same reserved<=observed loader gate plus two more
fixture errors (a NOW misaligned with the 86400s freshness boundary
and an apply probe dispatching through the wrong surface — plain
`inventory` with an `apply` argument instead of the registered
multi-word verb). The corrected suite passes 10/10. This fixture
error class is now a four-time repeater across probers; the §15
hotspot note records the convention that prevents it.

## 14. Tasks J-L remain NOT implemented

Per the brief, Task I stops after I: no transfer execution, no
GiftItem, no SellItem3/Purchase4, no marketplace self-transfer, no
mutation executor or reconciler, no retry logic, no transaction
sequencing, no endpoint enablement, no credential persistence, no
premium-currency spending. `inventory apply` remains an
unconditional refusal; all Task I output is actionable=false;
`ELIGIBLE` is never assigned. Nothing in this synthesis expands that
scope.

## 15. Hotspot notes for the next slice

- `sdk/inventory/transfer.py` (624 lines) is one cohesive pure
  module. If a future capture-verified slice extends the gate, keep
  the constant-hold logic in one place and keep `plan_transfers`
  unreachable from commands.py — the thin-wrapper AST pins in the
  CLI tests enforce this and will land red on any violation.
- `sdk/commands.py` grew 514 -> 530 via the transfers dict entry and
  description text. The remaining inventory CLI is still a thin I/O
  wrapper; any new verb extends the pure render modules.
- Multi-word verbs in the TUI now resolve longest-prefix-first. Any
  future multi-word verb registration should keep that loop in mind
  (the `inventory apply` shadowing was the bug this fixed).
- Probe-fixture convention (four probers hit it): a deficit fixture
  must satisfy the loader's reserved <= observed and stack-sum ==
  design-observed gates — the safe pattern is a quantity-5 stack
  with requested=8 / reserved=5 (unmet 3). And a fixture captured at
  2026-09-30T00:00:00Z is FRESH only for now <= 2026-10-01T00:00:00Z
  (age == threshold is fresh; one second past is stale).
- The CLI test mixin pins `sdk.commands.datetime`. Any new
  CLI-path test driving clock-dependent paths must do the same or it
  will decay with the calendar (the Task H SYN-H1 lesson).

## 16. Recommended next step

Unchanged and now triply load-bearing: capture `ListItemsOfAShip`
(or an equivalent inventory read path) during normal play — a
single capture promotes inventory-feed evidence beyond
CAPTURED_NOT_VERIFIED and makes observed quantities checksum-
verified inputs. The Task I-specific addition: capture one
`ItemService/GiftItem` exchange (send a gift between two accounts
you own) plus a dual-account reconcile. That single capture fills
the recipient-parameter, quantity-semantics, and checksum-preimage
gaps named in §10, and is the ONLY path by which any future slice
could move a proposal off HOLD_UNVERIFIED_TRANSFERABILITY. Until
then every proposal correctly holds.

## 17. Machine-readable facts

- planner_module: sdk/inventory/transfer.py, 624 lines, pure (no
  network/filesystem/clock), now caller-supplied
- join_key: ItemDesignId ONLY; no item_id field, key, or output
  anywhere in the planner surface
- matching: per design ascending, destinations ascending user_id,
  sources ascending user_id; matched=min(remaining supply, remaining
  deficit); both sides decrement; gate-failing pairs consume no
  capacity; partition rule (deficit account never sources same
  design)
- gate: 8 requirements; ALL design-scoped condition codes
  applicable (union, not allowlist); protected/unknown on EITHER
  side suppresses; stale on either side suppresses; evidence never
  above GIFTITEM_EVIDENCE_TIER (STATICALLY_DISCOVERED)
- fail_closed_constant: every proposal HOLD_UNVERIFIED_TRANSFERABILITY,
  proposed_quantity=0, actionable=false; ELIGIBLE never assigned;
  matched_quantity informational only
- cli: `inventory transfers` read-only subcommand via shared
  _render_portfolio_view wrapper; commands.py imports ONLY
  render_transfers_view from the transfer family; no registered verb
  contains "transfer"; inventory apply unconditional REFUSED
  (args/config/env-independent), now reachable from the TUI via
  longest-prefix multi-word resolution
- tui: multi-word verb longest-prefix resolution added (fixes
  inventory-apply shadowing); _cmd_help matches
- tests: 1013 total (829 baseline + 184 Task I: planner 54,
  adversarial 52, safety 56, CLI 22)
- validation: make test 1013 OK skipped=1; targeted 184/184 OK; ruff
  clean on the Task I surface (10 repo-wide F401/F811 pre-existing
  in 4 untouched files); ty transfer.py clean (4 commands.py
  diagnostics pre-existing at HEAD); git diff --check clean
- verifier_probes: 20/20 PASS, 0 planner defects (run 116,
  gate=pass)
- synthesizer_probes: 10/10 PASS (fresh interpreter, project venv;
  workspaces/t_b4414b94/probes/synthesizer_probes.py)
- corpus_scans: 5 independent binary-safe scans agree — all 6
  capture files, 50,755 newline records, 786,144,705 bytes:
  giftitem/gift_item/gift-item=0, sellitem3=0, purchase4=0,
  marketservice=0, transferitem/transfer_item=0; ToUserId=60 +
  FriendUserId=14 all classified MessageService/UserService chat +
  friend-list bodies (safety-lane correction, adopted)
- defects: 0 production defects post-planner-self-review; 4
  planner-lane self-review catches pinned by tests; 12 probe-harness
  bugs fixed by probers (verifier 3, safety 3, adversarial 3,
  synthesizer 3), all test-side
- prohibitions_held: no GiftItem execution, no SellItem3, no
  Purchase4, no marketplace, no mutation executor/reconciler, no
  retry, no sequencing, no endpoint enablement, no credential
  persistence, no premium spending, no network I/O in sdk/inventory,
  no clock reads in sdk/inventory, apply refusal unconditional,
  actionable=false everywhere, ListItemsOfAShip cap preserved, Task H
  loader gates preserved
- synthesizer_files_changed: docs/swarm-synthesis-task-i-t_b4414b94.md
  (this report) ONLY. No production code, test, or README file was
  changed by the synthesizer.

---

## Swarm protocol record

- Machine-readable facts: in the completion metadata of this card.
- Cross-worker notes: posted to the swarm root `t_7842f6df` as a
  structured comment.
- Files changed by this synthesizer: this report plus the probe
  suite preserved under the task workspace. No production behavior
  was changed by the synthesizer. The full validation ladder
  (§12) was re-run AFTER the report was written and the numbers in
  §12 reflect those final runs.
