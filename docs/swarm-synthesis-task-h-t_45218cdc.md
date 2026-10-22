# Task H swarm synthesis — versioned AccountSnapshot persistence and read-only multi-account inventory portfolio

Swarm root / blackboard: `t_68ba2251`. Verifier: `t_ce82f4d7` (gate=PASS,
blackboard comment "verifier_gate", completed with metadata
`{"gate":"pass"}`). Synthesizer: `t_45218cdc` (this card).

This report consolidates the five lane handoffs (reconcile/requirements,
snapshot+portfolio domain, safety/policy verification, architect/CLI,
adversarial verification) plus the verifier gate into the final Task H
deliverable record. Every load-bearing claim below was re-verified by the
synthesizer against the working tree at HEAD `2cb5aa4` on 2026-09-30: the
full validation ladder re-run (829 tests, Ruff on the touched surface, ty
on the new modules, `git diff --check`), the verifier's preserved 57-probe
suite re-executed from its own script, a fresh 5-probe synthesizer suite
written from scratch (purity under socket block, CLI e2e, fail-closed
gates, portfolio provenance, evidence cap + unmet arithmetic), the
evidence-cap scan re-run over all 6 capture files, and the golden
renderer-parity sha256 re-checked in the shipped test. Where a claim rests
only on a worker report and not on code or a fresh run, the report says so.

Swarm narrative (for the record): the reconcile lane (`t_8c21999b`) landed
first and pinned the Task H requirements against repository truth
(docs/task-h-reconciliation-t_8c21999b.md: identity triple, secrets
blacklist, schema v1 field names, fail-closed batch rule, evidence floors,
the 24h staleness-annotation decision, and the renderer-extraction
precondition). The domain lane (`t_c2fee26d`) then built the core:
`sdk/inventory/snapshot.py` (712 lines at landing) and
`sdk/inventory/portfolio.py` (293 lines) plus 54 tests. The safety lane
(`t_f4b6027d`) probed the domain output, confirmed 4 version-gate holes
(version=True/1.0 accepted, user_id=0 accepted, unknown root keys accepted,
duplicate-JSON-key last-wins), deferred them to the adversarial lane to
avoid file collisions, and shipped 48 adversarial safety tests proving 7
clean axes. The architect lane (`t_6f3684d5`) performed the brief's
precondition first — mechanically extracting the inventory-plan renderer
from the `sdk/commands.py` hotspot into pure `sdk/inventory/plan_render.py`
with a byte-parity golden — then added the pure
`sdk/inventory/portfolio_render.py` (staleness surface, single owner) and
the read-only `inventory accounts` / `inventory deficits` CLI verbs, with
57 tests. The adversarial lane (`t_2c696fd5`) crashed twice on model
output-token limits, was re-dispatched, and closed the loop: 29 live
probes confirmed 28 fail-open holes (the safety lane's 4 plus 24 more:
captured_at format, VERIFIED evidence acceptance, vocabulary gates,
producer-invariant couplings, arithmetic cross-checks, list-nested secret
scan, cross-account condition bleed, hand-constructed-snapshot bypass);
all 28 were fixed with ~450 lines of loader gates and 22 regression
tests, with fixture-value-only repairs on the safety and architect lanes'
completed test files. The verifier (`t_ce82f4d7`) ran an independent gate:
57 from-scratch probes (all closed), full suite 829 OK, and reconciliation
of all five lane handoffs — zero material discrepancies, gate=PASS. The
synthesizer re-ran everything and found one additional defect the swarm
had missed: a time-bomb test fixture (see §15), which it fixed and
re-validated. Net: **28 production fail-open holes + 1 test-side time bomb
were found and fixed across the swarm**, every production fix pinned by
regression tests, and the loader's gate structure held through 57
independent probes plus the synthesizer's re-runs.

## 1. Repository baseline / HEAD

- HEAD `2cb5aa4` (mining drone slice), branch `main`. No commits were made
  by any lane or by the synthesizer; the entire Task H output lives in the
  dirty worktree alongside the earlier Slice 0/1, Task D, Task F, and Task
  G output, which remains uncommitted by design.
- Worktree state at synthesis: `sdk/inventory/` (12 modules, incl. new
  `snapshot.py`, `portfolio.py`, `plan_render.py`, `portfolio_render.py`),
  seven `tests/test_inventory*.py` files (incl. 5 new), inventory docs
  under `docs/`, plus modified `sdk/commands.py`, `sdk/client.py`,
  `sdk/security.py`, `sdk/tui.py`, `README.template`, `README.md`,
  `tests/test_battle_flow.py`, `tests/test_runtime_guards.py`,
  `tests/test_tui.py`, `uv.lock`. The client.py / security.py / tui.py
  modifications predate this swarm (earlier slices); no Task H lane
  disturbed them.
- Suite before Task H: 648 tests (post-Task G), 1 pre-existing skip.
  Suite after Task H: 829 tests. 648 + 54 (domain) + 48 (safety) + 57
  (architect, incl. 6 renderer-parity) + 22 (adversarial) = 829; no test
  was weakened or removed. (Three fixture-value-only repairs were made on
  existing lane tests by the adversarial lane so fixtures match what the
  D/F/G producer can actually emit; the intent of every repaired test was
  preserved and the verifier re-ran each file green.)

## 2. Files added

Production code:

- `sdk/inventory/snapshot.py` — versioned AccountSnapshot persistence
  (1166 lines after the adversarial lane's gate battery): `build_snapshot`
  (pure projection of one account's D/F/G state), `snapshot_from_dict` /
  `snapshot_from_json` (fail-closed loader), `to_json` (deterministic
  canonical serialization), `SNAPSHOT_VERSION = 1`,
  `FORBIDDEN_SNAPSHOT_KEYS` (15 secret-name variants, deep-scanned at every
  nesting level incl. list-of-list), and the full gate battery (§5).
- `sdk/inventory/portfolio.py` — pure `AccountPortfolio` aggregation (336
  lines): `aggregate_snapshots` (by ItemDesignId, per-account provenance,
  weakest-input evidence, fail-closed batch), `AccountContribution`,
  `DesignPortfolioRow` (totals, deficit_accounts, unresolved_codes,
  actionable=False), `rows_with_deficits()` / `rows_with_candidates()`,
  `actionable_rows()` (structurally empty), `PortfolioError`.
- `sdk/inventory/plan_render.py` — pure `render_inventory_plan` (211
  lines), mechanically extracted from the `sdk/commands.py` hotspot
  (§7). Byte-identical to the pre-extraction renderer on the pinned
  fixtures.
- `sdk/inventory/portfolio_render.py` — pure Task H view renderers (303
  lines): `render_accounts_view`, `render_deficits_view`,
  `snapshot_freshness` + `SnapshotFreshness` (now-injected),
  `STALE_AFTER_SECONDS = 86400`, `PortfolioViewError`. Single owner of the
  staleness surface.

Tests:

- `tests/test_inventory_snapshot.py` — 51 tests (33 domain + 18
  adversarial gates).
- `tests/test_inventory_portfolio.py` — 25 tests (21 domain + 4
  adversarial: hand-built bypass rejection, forged-vocab rejection,
  no-condition-bleed provenance, batch fail-closed).
- `tests/test_inventory_snapshot_safety.py` — 48 tests (safety lane's 7
  axes: secrets, no-transfer-planning, purity, no-mutation-reachability,
  apply refusal, fail-closed cross-account, determinism/isolation).
- `tests/test_inventory_accounts_deficits.py` — 51 tests (architect lane:
  view basics, fail-closed corruption, staleness annotation, evidence
  floors, no-transfer planning, mutation isolation, dispatch, determinism).
- `tests/test_inventory_plan_render.py` — 6 tests (byte-parity golden
  sha256 + full golden text + purity + hotspot-body-removed proof).

Documentation:

- This report. (The reconcile lane's requirements doc is
  `docs/task-h-reconciliation-t_8c21999b.md`; each lane's full handoff
  lives in its blackboard comment on the root, which this synthesis
  consolidates. The verifier's probe suite is preserved at
  `workspaces/t_ce82f4d7/probes/verify_task_h.py`; the synthesizer's
  independent probe suite at
  `workspaces/t_45218cdc/probes/synthesizer_probe.py`.)

## 3. Files modified

- `sdk/inventory/__init__.py` — Task H export surface: snapshot module
  (SNAPSHOT_VERSION, FORBIDDEN_SNAPSHOT_KEYS, AccountSnapshot,
  SnapshotAccountIdentity/Condition/DesignRow/StackRow/Error, build_snapshot,
  snapshot_from_dict/json), portfolio module (AccountContribution,
  AccountPortfolio, DesignPortfolioRow, PortfolioError,
  aggregate_snapshots), portfolio_render module (STALE_AFTER_SECONDS,
  SnapshotFreshness, PortfolioViewError, render_accounts_view,
  render_deficits_view, snapshot_freshness), and render_inventory_plan.
  No pre-existing export altered. Synthesizer verified: every Task H
  symbol named by the lanes appears in `__all__`.
- `sdk/commands.py` — hotspot reduced 565 -> 514 lines: the plan-render
  body moved to `plan_render.py` (`_render_inventory_plan` is now a thin
  wrapper supplying ship payload + strategic reserve); `inventory`
  dispatches the read-only `accounts`/`deficits` subcommands to
  `_render_portfolio_view`; new `_inventory_snapshot_dir` /
  `_read_snapshot_texts` / `_render_portfolio_view` own ALL disk + clock
  I/O in the Task H path (sdk/inventory stays pure). Any malformed,
  corrupt, unknown-version, secret-bearing or contradictory snapshot
  refuses the WHOLE view as explicit fail-closed text naming the file.
  `inventory apply` handler content unchanged (constant REFUSED return).
  Synthesizer change (see §15): `import datetime` hoisted from
  function-local to module level — no behavior change; it is the single
  clock read (line 461) and the seam tests use to pin the CLI clock.
- `README.template` — updated first, then `README.md` (repo rule
  respected): Task H verbs documented in both. Synthesizer re-verified
  the two files differ only by the GitHub-hook header.
- `tests/test_inventory_accounts_deficits.py` — synthesizer: pinned the
  CLI wall clock in the shared test mixin (§15); no assertion changed.
- `tests/test_inventory_snapshot_safety.py` /
  `tests/test_inventory_accounts_deficits.py` — adversarial-lane
  fixture-value-only repairs (documented in §15a), verifier re-ran both
  files green.

## 4. The snapshot surface (schema v1)

`build_snapshot(identity, classified, ledger, reduction, captured_at)` is
the only writer. It projects ONE account's D/F/G outputs into inert data;
it adds no semantics. The serialized v1 shape (field names mirror the
domain records 1:1, pinned in the reconcile doc §7a; synthesizer confirmed
against a live build):

- `version: 1` — the only readable/writable version; anything else is
  rejected (forward AND backward), with a strict int gate so `1.0` and
  `True` are rejected, not accepted via `==` comparison.
- `identity` — exactly `{user_id, ship_id, display_name}`: the only
  safe, non-secret, stable identity triple (reconcile doc §2). user_id is
  the canonical account key (>= 1); display_name is informational;
  ItemIds are account-scoped and never comparable across accounts;
  ItemDesignId is the only cross-account join key.
- `captured_at` — pinned ISO-8601 UTC Z-suffix seconds
  (`2026-09-29T00:00:00Z`); off-convention forms (offset, fractional,
  naive) are malformed at deserialization AND at build.
- `stacks` — per-stack D/F/G state: ids, quantity, classification
  bucket, protected/unknown flags, roles, protection/classification
  reasons, feed evidence, requested/reserved, Task G candidate
  quantity/state/reasons/evidence, disposition hint.
- `designs` — per-design rollups: observed/requested/reserved/unmet,
  evidence, stack count.
- `conditions` — the ledger's UnresolvedDemand conditions (code, detail,
  design_id nullable).
- `limitations` — the policy's limitation list.
- `actionable` — false, at the root; a payload carrying true is
  malformed.

No credential, token, secret or authentication material has a field, and
the loader deep-scans every dict for the 15 `FORBIDDEN_SNAPSHOT_KEYS`
name variants (case-sensitive pairs cover the camelCase/lowercase forms;
the scan recurses through lists of lists). Determinism: identical inputs
serialize byte-identically; stacks ascending ItemId, designs ascending
ItemDesignId, conditions sorted by (code, detail, design_id).

## 5. The fail-closed gate battery

A snapshot is rejected whole (SnapshotError) when any of the following
hold — each line below is pinned by at least one regression test and was
independently re-probed by the verifier and the synthesizer:

- Version: missing, non-int, != 1, `1.0`, `True`, `2`; duplicate JSON key
  overriding `version` (rejected via object_pairs_hook — no last-wins).
- Identity: user_id 0 or negative, missing identity, unknown identity
  keys, non-int ids.
- Schema: unknown keys at root/identity/stack/design/condition level
  (strict key sets); non-object roots/entries; wrong field types;
  truncated payloads; non-object JSON.
- Secrets: any FORBIDDEN_SNAPSHOT_KEYS name at any nesting depth,
  including inside lists of dicts and lists of lists.
- Evidence: unknown tier strings; VERIFIED on any feed-derived surface
  (stack evidence, candidate evidence, design evidence) — the
  ListItemsOfAShip cap is enforced at the loader, not just the producer.
- Vocabulary: classification outside the 4 emittable buckets
  (PROTECTED/RESERVED/CONSUMABLE/UNKNOWN); roles outside the producer set
  minus ROLES_FORBIDDEN_THIS_SLICE (TRANSFERABLE_CANDIDATE rejected);
  disposition outside KEEP/USE/CRAFT/ALLOCATE (TRANSFER rejected);
  candidate_state outside HOLD/CANDIDATE; unknown BLOCKED_* /
  PROTECTED_* / condition codes.
- Producer-invariant couplings: candidate>0 <-> CANDIDATE; HOLD always
  has a reason; CANDIDATE never has reasons or a KEEP hint;
  unknown-design -> PROTECTED + protected flag; protected <-> reasons in
  both directions.
- Arithmetic: stack demand totals == design totals; reserved <=
  requested; unmet == max(0, requested − observed); per-design candidate
  sum <= unreserved pool; design observed == sum of its stack
  quantities; design stack count == actual; candidate <= stack quantity;
  design-scoped conditions must reference an existing design row.
- captured_at: unparseable, offset, fractional, naive — rejected at load
  and at build.

Batch rule: ANY invalid snapshot in the portfolio input aborts the
ENTIRE aggregation with PortfolioError — never a silent per-account drop
(the under-claim direction). Duplicate user_id (two different snapshots,
or the same object twice) aborts. Non-AccountSnapshot input aborts. And
the aggregate re-validates every input through
`snapshot_from_dict(snapshot.to_dict())`, so a hand-constructed
AccountSnapshot that never passed the loader cannot bypass the gates by
entering aggregation directly (synthesizer re-proved: a forged
actionable=True instance is rejected at aggregate time).

## 6. Portfolio aggregation

`aggregate_snapshots(snapshots) -> AccountPortfolio` is pure,
deterministic, fail-closed. One row per ItemDesignId across all
accounts:

- `accounts` — per-account provenance (ascending user_id): stacks,
  observed/requested/reserved/unmet, the account's Task G candidate
  total (informational only), evidence, and the condition codes from
  the account's OWN snapshot for that design. The adversarial lane's
  condition-bleed fix scopes conditions per (account, design): bob's
  contribution row never carries alice's DEMAND_EXCEEDS_SUPPLY.
- Totals: observed/requested/reserved/unmet/candidates summed across
  contributing accounts.
- `evidence` — weakest across every contributing stack row, design row,
  and condition tier. Never promoted.
- `deficit_accounts` — accounts whose own demand exceeds their own
  observed supply (unmet > 0). An observed fact about ONE account,
  never a transfer plan; supply holders are never paired with deficit
  holders.
- `unresolved_codes` — pooled design-scoped condition codes.
- `actionable` — False on the portfolio and every row and every
  contribution.

Unmet semantics were the swarm's one substantive reconciliation: the
reconcile doc recommended derived-at-portfolio `requested − reserved`;
the domain lane persisted `max(0, requested − observed)`. The verifier
reconciled them: under the Task F producer invariant
`reserved = min(requested, observed)` (demand.py `_finalize`: `reserve
= min(requested, remaining)`), the two formulas are equivalent for
producer-emitted ledgers, and the persisted form matches Task F's own
DEMAND_EXCEEDS_SUPPLY shortfall arithmetic. The loader pins
`unmet == max(0, requested − observed)` so the persisted value is
bounded either way. Synthesizer re-verified the producer line and the
equivalence. Unmet renders as "(informational)" everywhere and is never
acquisition semantics.

## 7. Renderer extraction (the brief's precondition)

Before any CLI expansion, the inventory-plan renderer was mechanically
extracted from `sdk/commands.py` (the 565-line, three-lane collision
hotspot flagged in the Task G synthesis §17) into pure
`sdk/inventory/plan_render.py`. Parity evidence, all shipped in
`tests/test_inventory_plan_render.py` and re-verified by the verifier and
synthesizer:

- Golden sha256 pinned:
  `0c4c2095c41dce0a3a273a43bf8aa24177ab1c5ef39652f8c0b540b162abb87d`,
  captured from the PRE-extraction renderer on the same fixtures as the
  Task G-era CLI tests, byte-identical post-extraction.
- The 30 Task-G-era line-level `test_inventory_plan_cli.py` tests
  (written against the pre-extraction hotspot) still pass unchanged.
- Pure-path vs CLI-path output equality; purity (no disk/network/clock)
  proven; hotspot-body-removed AST proof.
- commands.py is now 514 lines and the remaining inventory CLI is a thin
  I/O wrapper. Any future CLI verb extends the pure render modules, not
  commands.py.

## 8. The CLI views (`inventory accounts` / `inventory deficits`)

Both are strictly read-only, disk-fed (caller owns I/O), and fail-closed
whole-view: a corrupt/malformed/unknown-version/secret-bearing/
contradictory snapshot file refuses the entire view with the offending
file named; a duplicate user_id across files refuses the whole view; an
unreadable file refuses the whole view; an empty or missing directory is
an explicit empty state, not an error.

- `inventory accounts` — one summary block per account (ascending
  user_id): identity, capture time, freshness marker, stack/design
  counts, observed/requested/reserved/unmet totals, the account's dry-run
  candidate total (informational only), weakest-input evidence,
  unresolved condition codes.
- `inventory deficits` — designs (ascending ItemDesignId) with unmet
  demand, reporting each DEFICIT account's numbers with capture time and
  freshness. Supply holders are deliberately not listed beside deficit
  holders (a donor/recipient table is the forbidden structure), and
  candidate quantities are deliberately absent from this view.
- Both views carry the required wording "Candidate does not mean
  transferable." and "actionable=false"; neither view contains any
  donor/recipient/matching/route/proposal vocabulary (synthesizer
  re-proved by scan of rendered output).
- Dispatch: `inventory <subcommand>` from the registry, the TUI REPL,
  and help; unknown subcommand lists all four verbs. `inventory apply`
  remains an unconditional refusal, reachable everywhere, independent of
  args (--force/--yes included), settings, and env (synthesizer re-proved
  via registry dispatch with --force --yes: REFUSED).

## 9. Staleness / freshness (the age axis)

Pinned in the reconcile doc §4/§4a and implemented once, in
`sdk/inventory/portfolio_render.py` (single owner; portfolio.py and
snapshot.py stay free of freshness — the adversarial lane reconciled
rather than duplicated):

- `STALE_AFTER_SECONDS = 86400` — the single v1 threshold, exported,
  documented (24h matches the game's daily-reward cadence), overridable
  per call as a parameter.
- Staleness is a RENDERING ANNOTATION, never a rejection: a stale
  snapshot still aggregates and renders, with a visible STALE marker on
  its account line; portfolio-level "all fresh" requires every snapshot
  fresh. Exactly at the threshold is still fresh; one second past is
  stale (boundary pinned by test).
- Staleness never changes an evidence tier (pinned by test: identical
  content at different capture times renders identical evidence).
- `captured_at` is parsed strictly at view time (pinned ISO-8601 UTC
  Z-seconds): unparseable, offset, and FUTURE timestamps refuse the
  whole view. Future-ness is clock-relative, so it is checked exactly
  where `now` is injected — never at file load, never inside
  sdk/inventory (purity).
- No clock reads inside sdk/inventory: the CLI caller reads the wall
  clock once and passes `now`; tests pass pinned clocks. The
  synthesizer's §15 fix pins the CLI clock in tests so this contract is
  actually exercised deterministically.

## 10. Determinism

- Snapshot builds are byte-identical across runs and input permutations;
  round-trip (build -> to_json -> from_json -> to_json) is lossless
  (synthesizer re-proved under a hard socket block).
- Portfolio aggregation is input-order independent (accounts keyed and
  processed ascending user_id; design rows ascending ItemDesignId).
- Both views are byte-identical across runs and file-name permutations.
- No wall-clock, randomness, dict-order or set-order dependence inside
  sdk/inventory. The only clock read in the whole Task H surface is the
  CLI's single `now` (commands.py:461), injected into pure renderers.
- Frozen dataclasses throughout; `to_dict`/`to_json` return fresh
  containers; aggregation never mutates its inputs (pinned by tests).

## 11. Safety properties (the mandated axes)

- Secrets: real D/F/G pipeline snapshots carry no secret keys at any
  depth and no token-shaped values (deep key scan + JWT/bearer/hex value
  regex); the loader rejects every forbidden key name at root, identity,
  stack, conditions, and list nesting (synthesizer re-probed live).
- No transfer planning: no donor/recipient/match/route/proposal
  structure exists anywhere in snapshot/portfolio state; serialized
  output contains none of the forbidden vocabulary; deficits are
  per-account observations; candidate totals are never netted against
  deficits (synthesizer re-proved by scanning rendered views).
- Purity: build + load + aggregate + render complete under a hard socket
  block (synthesizer re-proved in a fresh interpreter); source scans
  show no network imports, no env reads, no disk tokens, no clock reads
  in sdk/inventory; the one-way boundary holds (sdk.client imports no
  sdk.inventory).
- No mutation reachability: no Client construction, no `.request(`
  calls, no forbidden endpoint patterns; the full surface completes with
  every mutation-capable Client method poisoned.
- Apply refusal: unconditional, config/env/args-independent, reachable
  from registry, TUI, and help; the stub is registered verified=False.
- Fail-closed cross-account: duplicate user_id aborts the entire
  aggregation even after valid accounts were processed (no partial
  portfolio); ItemIds equal across accounts are legal (never treated as
  duplicates — ItemId is not a cross-account key).
- Non-actionability: actionable=False on every snapshot, portfolio, row,
  and contribution; actionable_rows() is structurally empty; a payload
  carrying actionable=true is malformed; a forged instance is rejected at
  aggregate re-validation.

## 12. Prohibition verification (no leakage)

- No donor/recipient matching, no transfers, no GiftItem, no
  marketplace transactions, no SellItem3, no Purchase4, no destination
  selection, no mutation executor/reconciler, no endpoint enablement:
  verified by source scans (no mutation URLs in the touched sources),
  vocabulary scans of serialized output, and symbol scans of the
  modules. The `Disposition` enum still has exactly KEEP/USE/CRAFT/
  ALLOCATE — TRANSFER remains unconstructible.
- No network I/O in sdk/inventory: static + runtime proven (§11).
- ListItemsOfAShip evidence cap preserved: VERIFIED is rejected on every
  feed-derived surface at the loader; nothing this slice produces
  reaches VERIFIED. The cap's factual basis re-verified by the
  synthesizer on 2026-09-30 with its own binary-safe,
  prefix-variant-tolerant scan: 0 ListItemsOfAShip hits across all 6
  capture files (49,774 + 681 + 66 + 84 + 74 + 76 records under the
  lanes' newline accounting; 50,547 non-empty lines by the
  synthesizer's stricter count — the 208-record difference is
  newline-vs-nonempty-line accounting on the MITM file, not a coverage
  difference; both scans see the same corpus and both find zero hits).
- No multi-login: portfolio reads persisted snapshot files from
  `inventory.snapshots_dir` (conventional path `snapshots/`); nothing in
  Task H authenticates, switches, or selects accounts live.

## 13. Tests added in Task H

181 new tests across 5 files (all counts re-derived by the synthesizer
from a verbose full-suite run):

- `tests/test_inventory_snapshot.py` — 51 (33 domain: build projection,
  determinism, round-trip, ledger totals, no-secret-fields; 18
  adversarial gates pinning every confirmed hole with error-fragment
  assertions).
- `tests/test_inventory_portfolio.py` — 25 (21 domain: aggregation,
  provenance, evidence, fail-closed batch, mutation isolation,
  non-actionability; 4 adversarial).
- `tests/test_inventory_snapshot_safety.py` — 48 (7 axes; includes
  real-pipeline fixture scans, socket-block purity, poisoned-client
  reachability, apply refusal under armed settings/env).
- `tests/test_inventory_accounts_deficits.py` — 51 (view contracts,
  whole-view fail-closed, staleness annotation + boundary semantics,
  evidence floors, no-transfer vocabulary/structure, mutation isolation,
  dispatch/TUI reachability, determinism).
- `tests/test_inventory_plan_render.py` — 6 (golden sha256, full golden
  text, purity, hotspot-body-removed).

Adversarial coverage spans every area the brief named: determinism,
corruption, version rejection, duplicates (ItemId, design, JSON key,
user_id), staleness (annotation-not-rejection, boundary, never-touches-
evidence, future fails closed), evidence floors (weakest-input,
never-promoted, weak-evidence-included-not-dropped), secret leakage
(every key variant at every nesting, incl. list-of-list), fail-closed
aggregation (batch abort, no partial portfolio), no transfer planning,
mutation isolation, and apply refusal.

## 14. Full validation results

Re-run by the synthesizer on 2026-09-30 after its §15 fix (matching the
verifier's independent gate run):

- `make test` (canonical gate): Ran 829 tests, OK (skipped=1). Baseline
  648 + 181 new; no test weakened or removed.
- Per-file counts (synthesizer's verbose run): snapshot 51, portfolio
  25, snapshot_safety 48, accounts_deficits 51, plan_render 6, plan_cli
  30 (Task G era, unchanged), reduction 41, safety 41, demand 77,
  classify 23, policy 53, inventory 36 — 482 inventory-surface tests
  total.
- `ruff check` on the Task H surface (all touched files): All checks
  passed. Repo-wide `make lint` scope: 11 errors, ALL pre-existing at
  HEAD in 5 files untouched by Task H (crew_leveling, ship_layout x2,
  test_game_formulas x5, test_ship_layout, verify_battle_against_capture)
  — identical to the verifier's disclosure; no Task H-introduced lint
  debt.
- `ty check` on the new modules (snapshot, portfolio, plan_render,
  portfolio_render): clean. commands.py: 4 diagnostics, all pre-existing
  device-key calls (lines 94/104/505), untouched by Task H. (Repo-wide
  ty debt is pre-existing.)
- `git diff --check`: clean.
- Verifier probe suite re-run by the synthesizer from the preserved
  script: 57/57 CLOSED, 0 open, 0 crash.
- Synthesizer's own 5-probe suite (fresh interpreter, no worker
  helpers): 5/5 PASS — purity under socket block, CLI e2e (both views,
  corrupt-file whole-view refusal, duplicate-user refusal, apply
  refusal with --force --yes, unknown-subcommand listing), the fail-
  closed gate battery (version bool/float/missing/2, user_id 0/-5,
  unknown keys, negative quantity, VERIFIED evidence, TRANSFER
  disposition, TRANSFERABLE_CANDIDATE role, fake classification,
  duplicate JSON key, nested secret, off-convention timestamps),
  portfolio provenance (ascending order, hand-built actionable=True
  bypass rejected, same-snapshot-twice rejected), evidence cap (no
  standalone VERIFIED value in serialized snapshot) + unmet arithmetic.
- Evidence-cap scan: 0 ListItemsOfAShip hits across all 6 capture files
  (synthesizer's own binary-safe scan; §12).
- README parity: README.template vs README.md differ only by the
  GitHub-hook header; Task H sections identical in both.

Note: bare `pytest` collection remains broken by the pre-existing
pytest-asyncio 0.23.0 vs pytest 8.3.0 incompatibility; `make test`
(unittest) is the canonical gate, as established in earlier slices.

## 15. Defects found and fixed during the swarm

All production defects were found by lanes probing landed code; every
fix has regression coverage; the verifier independently confirmed every
fix closed, and the synthesizer re-probed the battery:

1. AD-H (adversarial lane, 28 fail-open holes, fixed in-lane): the
   domain lane's first snapshot.py/portfolio.py accepted version=1.0/
   True, user_id=0, unknown keys at every level, duplicate JSON keys
   (last-wins), off-convention captured_at strings, VERIFIED evidence on
   feed surfaces, TRANSFER dispositions, TRANSFERABLE_CANDIDATE roles,
   fake classification/condition/protection/candidate codes,
   producer-impossible state couplings, arithmetic contradictions
   (stack-vs-design demand mismatch, reserved>requested, candidate sum
   above the unreserved pool, wrong unmet, observed != stack sum),
   list-nested secrets, attributed one account's design conditions to
   every contributing account (provenance bleed), and admitted
   hand-constructed AccountSnapshots that bypassed all loader gates.
   Fix: the full gate battery of §5 — strict int version gate, strict
   key sets, user_id >= 1, ISO-8601 parse, vocabulary gates, evidence
   cap at the loader, producer-invariant couplings, arithmetic
   cross-checks, duplicate-JSON-key rejection via object_pairs_hook,
   fully recursive secret scan, per-account condition scoping, and
   aggregate re-validation of every input. 22 regression tests pin the
   holes; 29 live probes re-verified post-fix.
2. SYN-H1 (synthesizer, test-side time bomb, fixed in-lane): the
   architect lane's staleness tests drove the real-clock CLI path with
   fixtures pinned to fixed calendar dates. `test_fresh_snapshot_marked_
   fresh` (fixture captured 2026-09-29T01:00:00Z) was green when every
   lane and the verifier ran on 2026-09-29 and turned permanently red
   at 2026-09-30T01:00:00Z when the fixture crossed the 24h threshold —
   found by the synthesizer's mandatory full-suite re-run, which is the
   first run of this suite on a later calendar day.
   `test_future_captured_at_fails_closed` carried the same class of fuse
   (its 2027-01-01 "future" fixture stops being future on that date),
   and the three byte-identity tests could flake across a second
   boundary. Production code was correct throughout (staleness is an
   annotation; the CLI owns the clock read by design). Fix: hoist
   `import datetime` to module level in sdk/commands.py (no behavior
   change; the single clock read stays at commands.py:461) and pin the
   CLI clock in the test mixin via `patch("sdk.commands.datetime")`,
   so every CLI-path test runs against a fixed `now` exactly like the
   pure-path tests already did. All 829 tests green post-fix; the
   previously failing assertion now passes deterministically and the
   2027 fuse is disarmed.

## 15a. Fixture repairs and reconciliations (not defects)

- Adversarial-lane fixture-value-only repairs on the safety and
  architect lanes' completed test files: three fixtures used values the
  D/F/G producer can never emit (SURPLUS_USABLE bucket, CANDIDATE+KEEP
  coupling, a deficit stack whose demand totals disagreed with its
  design row). Each repaired fixture is producer-possible and preserves
  the intent of its test; both lanes' files re-ran green (safety 48/48,
  accounts/deficits 51/51), confirmed by the verifier and re-run by the
  synthesizer.
- Unmet semantics: reconciled as equivalent under the producer
  invariant (§6) — the verifier's reconciliation, accepted by this
  synthesis, with the loader pinning the persisted value.
- Staleness surface ownership: the adversarial lane's candidate list
  wanted STALE_AFTER/all_fresh in portfolio.py; the architect lane had
  already landed them in portfolio_render.py. The adversarial lane
  reconciled rather than duplicated (single owner); the verifier and
  synthesizer confirmed no duplication exists.
- The synthesizer's evidence-cap count (50,547 non-empty lines) vs the
  lanes' documented 50,755 records: newline-vs-nonempty-line accounting
  on the MITM corpus; zero hits under both counts (§12).
- The synthesizer's first probe draft had three fixture errors of its
  own (wrong DemandPlanner signature, bare-list item payload instead of
  the ListItemsOfAShip envelope, substring-vs-exact-value VERIFIED
  assertion); each was corrected after reading the real signatures, and
  the corrected suite passes 5/5. The exact-value VERIFIED check is the
  correct form of the cap probe: CAPTURED_NOT_VERIFIED legitimately
  contains the substring.

## 16. Tasks I-L remain NOT implemented

Per the brief, Task H stops after H: no transfer planning or execution,
no GiftItem, no marketplace, no SellItem3/Purchase4, no destination
selection, no mutation executor or reconciler, no endpoint enablement,
no live multi-login. `inventory apply` remains an unconditional refusal;
all Task H output is actionable=false; the `Disposition` members that
would name deferred semantics do not exist. The plan footer scopes
remaining work to I-L. Nothing in this synthesis expands that scope.

## 17. Hotspot notes for the next slice

- `sdk/inventory/snapshot.py` is now 1166 lines, the large majority of
  them loader gates. The structure held through 57 verifier probes plus
  the synthesizer's battery, but any future gate MUST go in a `_parse_*`
  helper — never inlined into `snapshot_from_dict` (adversarial lane's
  pinned note, verifier-confirmed).
- `sdk/commands.py` shrank 565 -> 514 via the renderer extraction. Any
  further CLI verbs extend `plan_render.py` / `portfolio_render.py`, not
  commands.py. The commands.py surface that remains is thin I/O wrappers
  (settings lookup, directory read, one clock read, fail-closed text).
- The suite now pins the CLI wall clock in tests
  (test_inventory_accounts_deficits.py `_TempSnapshotsMixin`). Any new
  CLI test that drives a clock-dependent path against fixed-date
  fixtures must do the same, or it will decay with the calendar exactly
  like SYN-H1 did.

## 18. Recommended next step

Unchanged from the Task G synthesis and now doubly load-bearing:
capture `ListItemsOfAShip` (or an equivalent inventory read path) during
normal play. A single capture promotes inventory-feed evidence beyond
CAPTURED_NOT_VERIFIED, makes observed quantities checksum-verified
inputs, and — new in Task H — would let `build_snapshot` carry a
captured feed under real captured_at provenance for the account views.
Until then every feed-derived row correctly stays below the cap, and
snapshots built from synthetic or stale feeds render with the weak tiers
they actually have. Task I (per the implementation plan's lettering, the
next lettered task after H) should start from this synthesis plus
`docs/swarm-synthesis-task-g-t_46223d9d.md`.

## 19. Machine-readable facts

- snapshot_module: sdk/inventory/snapshot.py, 1166 lines, SNAPSHOT_VERSION=1,
  FORBIDDEN_SNAPSHOT_KEYS=15, loader gates as §5
- portfolio_module: sdk/inventory/portfolio.py, 336 lines, aggregate
  re-validates every input; per-account condition scoping; PortfolioError
  subclasses SnapshotError
- renderer_modules: sdk/inventory/plan_render.py (211), portfolio_render.py
  (303); commands.py 565 -> 514; golden parity sha256
  0c4c2095c41dce0a3a273a43bf8aa24177ab1c5ef39652f8c0b540b162abb87d
- staleness: STALE_AFTER_SECONDS=86400; annotation not rejection;
  never changes evidence; future captured_at fails closed at view time
- cli_verbs: inventory accounts, inventory deficits (read-only,
  whole-view fail-closed, file named); inventory apply still REFUSED
- tests: 829 total (648 baseline + 181 Task H: snapshot 51, portfolio
  25, snapshot_safety 48, accounts_deficits 51, plan_render 6)
- validation: make test 829 OK skipped=1; ruff Task-H surface clean (11
  repo errors pre-existing in 5 untouched files); ty new modules clean
  (4 commands.py diagnostics pre-existing); git diff --check clean
- verifier_probes: 57/57 CLOSED (re-run by synthesizer from
  workspaces/t_ce82f4d7/probes/verify_task_h.py)
- synthesizer_probes: 5/5 PASS (workspaces/t_45218cdc/probes/
  synthesizer_probe.py, fresh interpreter, no worker helpers)
- evidence_cap: 0 ListItemsOfAShip hits across all 6 capture files
  (synthesizer's own binary-safe scan, 2026-09-30)
- defects_fixed: 28 production fail-open holes (adversarial lane) + 1
  test-side time bomb SYN-H1 (synthesizer), all with regression coverage
- prohibitions_held: no donor/recipient matching, no transfers, no
  GiftItem/SellItem3/Purchase4/marketplace, no destination selection,
  no mutation executor/reconciler, no endpoint enablement, no network
  I/O in sdk/inventory, apply refusal unconditional, actionable=false
  everywhere, ListItemsOfAShip cap preserved
- synthesizer_files_changed: docs/swarm-synthesis-task-h-t_45218cdc.md
  (this report); sdk/commands.py (datetime import hoisted to module
  level, no behavior change); tests/test_inventory_accounts_deficits.py
  (CLI clock pinned in the mixin; no assertion changed)

---

## Swarm protocol record

- Machine-readable facts: in the completion metadata of this card.
- Cross-worker notes: posted to the swarm root `t_68ba2251` as a
  structured comment.
- Files changed by this synthesizer: this report, plus the SYN-H1 fix
  (commands.py import hoist + test clock pinning) described in §15. No
  production behavior was changed by the synthesizer. The full suite
  (829 OK, skipped=1), ruff on the touched surface, ty on the new
  modules, and `git diff --check` were re-run after writing the fix and
  again after writing this report; see §14.
