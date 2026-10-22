# Task I reconcile lane — repository truth + GiftItem/transferability evidence (t_97ae279f)

Swarm root / blackboard: `t_7842f6df`. This lane: reconcile Task H portfolio
truth and research transferability/GiftItem evidence WITHOUT live mutation
traffic. Read-only research; no production code changes in this lane.

Re-verified against the live tree on 2026-10-01 at HEAD `2cb5aa4` (dirty
worktree, uncommitted by design, same state the Task H synthesis describes).

## 1. Baseline re-verified (live runs, this lane)

- `make test` (canonical unittest gate): **829 tests, OK, skipped=1** —
  matches the Task H synthesis §14 baseline exactly. No regression
  introduced by any Task I lane so far.
- `ruff check sdk/inventory/ sdk/commands.py`: clean.
- `git diff --check`: clean.
- Module inventory: `sdk/inventory/` = 13 modules (snapshot.py 1166,
  portfolio.py 336, portfolio_render.py 303, plan_render.py 211, plus
  _types/catalog/checksums/classify/demand/designs/policy/reduction +
  `__init__`); `sdk/commands.py` = 514 lines. All match the synthesis §19.
- `sdk/client.py` does not import `sdk.inventory` (one-way boundary holds).
- No network imports anywhere in `sdk/inventory/` (source scan, all 13 files).

## 2. Task H portfolio truth — confirmed properties the TransferPlanner MUST preserve

Verified by reading `sdk/inventory/portfolio.py`, `snapshot.py`,
`portfolio_render.py` in full and cross-checking the synthesis §4-§12:

- `aggregate_snapshots(snapshots)` is pure/deterministic, input-order
  independent (keyed ascending user_id), re-validates EVERY input through
  `snapshot_from_dict(snapshot.to_dict())` (hand-built instances cannot
  bypass gates), and aborts the WHOLE batch on any invalid input or
  duplicate user_id (`PortfolioError`).
- `DesignPortfolioRow.accounts` = per-account provenance
  (`AccountContribution`: user_id, display_name, stacks,
  observed/requested/reserved/unmet, candidate_quantity (informational
  Task G number), evidence, own-snapshot conditions). Conditions are
  per-(account, design) scoped — no provenance bleed.
- `deficit_accounts` = accounts with `unmet_quantity > 0` on that design.
  `unmet == max(0, requested − observed)`, pinned by loader arithmetic
  gates (§5 synthesis).
- `evidence` = weakest across every contributing stack/design/condition
  tier (`Evidence.weakest`, declared order VERIFIED > CAPTURED_NOT_VERIFIED
  > CAPTURED_AMBIGUOUS > STATICALLY_DISCOVERED > INFERRED > UNKNOWN).
  Never promoted. ListItemsOfAShip cap: VERIFIED is REJECTED on every
  feed-derived surface at the loader.
- `actionable=False` on portfolio, every row, every contribution;
  `actionable_rows()` structurally empty.
- Freshness: single owner `portfolio_render.snapshot_freshness`,
  `STALE_AFTER_SECONDS = 86400`, staleness is an ANNOTATION (never a
  rejection, never changes evidence); future captured_at fails closed at
  view time (where `now` is injected); `now` is caller-supplied — sdk/inventory
  reads no clock.
- `inventory apply` = unconditional refusal (verified=False stub),
  config/env/args-independent (pinned by tests).
- Loader gate battery (snapshot.py): strict int version==1 (rejects 1.0/
  True/2), user_id>=1, strict key sets at every level, duplicate-JSON-key
  rejection via object_pairs_hook, ISO-8601 UTC Z-suffix seconds
  captured_at, recursive secret scan (15 FORBIDDEN_SNAPSHOT_KEYS at any
  nesting incl. lists-of-lists), vocabulary gates (4 buckets; roles minus
  TRANSFERABLE_CANDIDATE; dispositions KEEP/USE/CRAFT/ALLOCATE only —
  TRANSFER unconstructible), producer-invariant couplings, arithmetic
  cross-checks.

## 3. TransferPlanner input contract (the reconciliation this lane owes Task I)

The only safe, repository-supported inputs for a pure TransferPlanner are
validated `AccountSnapshot` objects aggregated through
`aggregate_snapshots` (or the same re-validation discipline). Concretely:

- **Join key: ItemDesignId ONLY.** ItemId is account-scoped (each account's
  items get their own server-side ids); the Task H identity triple is
  `{user_id, ship_id, display_name}` and ItemIds never appear in it. The
  synthesis §4 states: "ItemIds are account-scoped and never comparable
  across accounts; ItemDesignId is the only cross-account join key."
- **Distinct source/destination**: keyed by `identity.user_id` (>= 1,
  loader-pinned). Two snapshots with the same user_id abort the portfolio;
  so source != destination is expressible as user_id inequality.
- **Source candidate quantity**: per-stack `candidate_quantity` summed per
  account+design (portfolio does exactly this in
  `aggregate_snapshots`, line 279). NEVER transferable by itself —
  "Candidate does not mean transferable" is a pinned Task G/H invariant.
- **Destination unmet demand**: per-account `unmet_quantity > 0` on the
  design (deficit_accounts). Loader pins unmet == max(0, requested −
  observed). Unmet is "informational" — Task I is the first slice allowed
  to pair it with source supply, and only as a NON-ACTIONABLE proposal.
- **Freshness**: `snapshot_freshness(captured_at, now)` per snapshot;
  `STALE_AFTER_SECONDS = 86400`; planner should refuse positive proposals
  when EITHER side is stale (per the brief: "fresh source and destination
  snapshots" required for positive proposals).
- **Evidence floor**: weakest-input propagation. A transfer proposal's
  evidence tier must be `Evidence.weakest` over source stack/design
  evidence + destination design evidence + the GiftItem protocol-evidence
  tier itself.
- **Conditions**: per-account, per-design condition codes from the
  account's OWN snapshot (`conditions_for(design_id)`); an applicable
  unresolved condition (e.g. DEMAND_EXCEEDS_SUPPLY on the destination, or
  a BLOCKED_* code) must suppress a positive proposal.
- **Protection**: `protected` flag + protection reason codes on stacks;
  protected items can never be a source candidate (Task D policy).

## 4. GiftItem protocol evidence — corpus research result (NO live traffic)

Full-corpus scans run by this lane on 2026-10-01 (binary-safe,
case-insensitive, prefix-variant-tolerant; all 6 capture files;
49,774 + 681 + 66 + 84 + 74 + 76 records):

1. **`GiftItem`: 0 captures.** Case-insensitive scan for `giftitem`,
   `gift_item`, `gift-item` across every record of all 6 files: zero hits.
   The endpoint has never appeared on the wire in any captured session.
2. **Every ItemService/MarketService endpoint actually seen** (complete
   enumeration, case-insensitive, including nested request bodies and the
   two binary-corrupted MITM records):
   - ItemService/ActivateItem4 (2), CombineItem4 (2), ConsumeItem2 (6),
     FixUnsynchronizedItems (2), MarkItemsNotNew (4).
   - MarketService: ZERO records of ANY kind. No SellItem3, no Purchase4,
     no ListSalesByItemDesignId, no marketplace traffic at all.
3. **Related transfer-semantics sweep**: zero hits for `TransferItem`,
   any `Transfer*` identifier, recipient-style params (`toUserId`,
   `to_user_id`, `targetUserId`, `friendUserId`, `recipientId`,
   `recipient_id`). Nothing in the corpus describes a cross-account item
   movement protocol.
4. **The 67 bare `gift` substring hits decompose entirely into
   non-protocol content** (each classified by this lane):
   - 21 records: random-token noise — 'gift' as a coincidental substring
     inside base64-ish design-hash strings in ListAllStaticDesigns2
     responses and login-capture device keys (e.g.
     `...nnennkbkq57giftgibq2xqcc...`).
   - 13 records: `saletype="fleetgift"` LiveOps sale configuration in
     LiveOpsService/GetTodayLiveOps2 responses (a Starbux sale category,
     not item transfer).
   - 4 records: Unity analytics config `enableautocollectexplorationgift`
     (a remote-config flag, not protocol).
   - 13 records: localization strings ("thankful for gift") in
     DesignService text feeds.
   - 7 records: sprite atlases (spritekey="giftbutton", "normalgiftbox",
     "chatgift", "fltgiftlist_*", "xmasgiftbox", etc.) in
     FileService/ListSprites2 responses.
   - 5 records: "your gifts"/"for gift" in unrelated response text.
   - 1 record (line 47800): 'gift' bytes inside a PNG asset payload —
     binary coincidence.
   **Conclusion: ZERO of the 67 are ItemService/GiftItem protocol
   traffic.** The corpus contains no gift/transfer request or response of
   any kind.
5. **Static knowledge (IL2CPP-derived, pre-existing docs)**: GiftItem is
   STATICALLY_DISCOVERED only —
   - URL template (INVENTORY_ENDPOINT_ANALYSIS.md §10):
     `/ItemService/GiftItem?itemId=` (template truncated in the binary;
     full param set unknown — recipient-targeting param name, quantity
     semantics, and checksum preimage are ALL unknown).
   - Evidence tier in the implementation plan's endpoint matrix:
     `STATICALLY_DISCOVERED | NEEDS_CAPTURE` — unblock condition:
     "capture + dual-account reconcile" (plan §20).
   - Hazard class H2 (docs/security/protected-item-policy.md): disabled
     until capture-verified AND guarded. Safety doc adds: "Recipient-
     targeting correctness must be provable from the capture (account
     identifier semantics); a gift to the wrong account is unrecoverable."
   - Reduction priority order (swarm-synthesis-inventory §12): "5 TRANSFER
     to owned account — NOT wired (GiftItem uncaptured)"; only positive
     `available_surplus` may enter steps 5-8.
   - Marketplace path is equally uncaptured (0 MarketService records):
     the AD-11 exact-listing rule and the whole sell→identify→buy→verify
     chain remain hypothetical.

## 5. Transferability decision (what Task I must do with this evidence)

Because GiftItem is uncaptured and its recipient/eligibility/checksum
semantics are unknown, **no repository-supported policy or verified
metadata can establish that ANY item is transferable**. Therefore, per the
Task I brief's fail-closed rule:

- Every cross-account match must carry an explicit transfer-eligibility
  decision, and the ONLY defensible decision today is
  **`HOLD_UNVERIFIED_TRANSFERABILITY`** (or equivalent) with quantity 0
  and actionable=false — for every proposal, unconditionally.
- No proposal may cite GiftItem, SellItem3, or Purchase4 as an executable
  route. The planner may still pair source supply with destination unmet
  demand (that is the Task I deliverable) but the eligibility state of
  every positive-quantity path must be a HOLD, and the evidence tier of
  every proposal must include the STATICALLY_DISCOVERED (at best) GiftItem
  tier via weakest-input propagation — in practice pinning every proposal
  at STATICALLY_DISCOVERED or weaker.
- Proposals must remain non-actionable even when source != destination,
  quantities are positive, snapshots are fresh, no conditions are
  unresolved, and no protection conflict exists: transferability cannot
  be established, so the gate fails closed. (Positive-quantity *proposal*
  with HOLD_UNVERIFIED_TRANSFERABILITY is acceptable per the brief only if
  `actionable=false` holds and no route is asserted; the brief's text
  "If transferability cannot be established, fail closed with
  HOLD_UNVERIFIED_TRANSFERABILITY or equivalent and quantity 0" governs:
  quantity 0 whenever transferability is unestablished.)
- The GiftItem capture gap must be documented in the transfers view and
  kept as the named blocker for any future actionable transfer planning
  (unblock condition: capture + dual-account reconcile, plan §20).

## 6. Gap register (documented, not fixable without captures)

| Gap | Evidence | Consequence for Task I |
|---|---|---|
| GiftItem: zero captures; template truncated (`?itemId=` only); recipient param, quantity semantics, checksum preimage unknown | corpus scan §4.1-4.4; INVENTORY_ENDPOINT_ANALYSIS §10 | transferability unprovable -> HOLD_UNVERIFIED_TRANSFERABILITY, qty 0, actionable=false on every proposal |
| ListItemsOfAShip: zero captures (re-confirmed 2026-09-30 by Task H synthesizer) | Task H synthesis §12; INVENTORY_ENDPOINT_ANALYSIS §ListItemsOfAShip | feed evidence capped at CAPTURED_NOT_VERIFIED; observed quantities never VERIFIED |
| Marketplace chain (SellItem3/ListSalesByItemDesignId/Purchase4): zero captures | corpus scan §4.2 (0 MarketService records) | no alternative transfer route can be cited |
| No recipient-identifier semantics anywhere in captures | corpus scan §4.3 | recipient targeting unverifiable; wrong-account gift unrecoverable (safety doc) |

## 7. Non-negotiables for sibling lanes (from this reconciliation)

1. Match accounts ONLY by ItemDesignId. ItemId must never appear in any
   cross-account join, key, or equality test. (Portfolio already models
   this correctly; the planner must not regress it.)
2. Quantity bounds: 0 <= proposal <= source candidate quantity AND <=
   destination unmet demand; decrement source supply and destination
   deficit across multiple matches (no oversubscription, no double-fill).
3. Deterministic, input-order-independent matching and reason ordering
   (sort keys: user_id, item_design_id, then reason codes lexicographic).
4. Positive proposals additionally require: distinct source/destination,
   positive source candidate quantity, destination unmet > 0, BOTH
   snapshots fresh (STALE_AFTER_SECONDS=86400, snapshot_freshness),
   no applicable unresolved condition on either side's design,
   no protection conflict on the source, no evidence promotion
   (weakest-input only), explicit eligibility decision.
5. When ANY requirement is unmet: HOLD_* state, quantity 0,
   actionable=false. HOLD_UNVERIFIED_TRANSFERABILITY is the default
   state for every proposal until a GiftItem capture exists.
6. No network I/O in sdk/inventory; no clock reads inside sdk/inventory
   (caller passes `now`); no GiftItem/SellItem3/Purchase4 execution, no
   marketplace self-transfer, no executor/reconciler/retry/sequencing, no
   endpoint enablement, no credential persistence, no premium spending.
7. `inventory apply` refusal stays unconditional. commands.py stays a
   thin wrapper; new view logic goes in portfolio_render (or a new pure
   transfer module, e.g. the planned `sdk/inventory/transfer.py` per the
   implementation plan §16), NOT in commands.py.
8. Every transfers-view row must show: source account, destination
   account, ItemDesignId, proposed quantity, evidence, freshness,
   eligibility/hold state, reasons, actionable=false.

## 8. Lane validation

- No production or test files were modified by this lane (research +
  reconciliation only). The baseline `make test` 829 OK / ruff clean /
  git diff --check clean runs in §1 double as the lane's validation and
  proof the working tree was not disturbed.

## 9. Machine-readable summary

- task: Task I reconcile/evidence lane
- baseline_tests: 829 OK, skipped=1 (make test, 2026-10-01, this lane)
- giftitem_captures: 0 (6 files, 50,755 records; case-insensitive,
  binary-safe, prefix-variant scan; bare-'gift' hits=67, all classified
  non-protocol: noise/liveops-config/unity-config/localization/sprites/png)
- market_service_records: 0
- transfer_semantics_records: 0 (TransferItem/recipient params: none)
- itemservice_endpoints_seen: ActivateItem4, CombineItem4, ConsumeItem2,
  FixUnsynchronizedItems, MarkItemsNotNew (5 distinct; none transfer)
- giftitem_evidence_tier: STATICALLY_DISCOVERED (IL2CPP template
  `/ItemService/GiftItem?itemId=`, truncated; checksum unknown)
- giftitem_unblock_condition: capture + dual-account reconcile
- transferability_decision: HOLD_UNVERIFIED_TRANSFERABILITY, quantity 0,
  actionable=false — every proposal, until GiftItem is captured
- portfolio_invariants_preserved: loader gate battery, weakest-input
  evidence, ListItemsOfAShip VERIFIED cap, ItemDesignId-only joins,
  actionable=false everywhere, apply refusal, no network/clock in
  sdk/inventory
