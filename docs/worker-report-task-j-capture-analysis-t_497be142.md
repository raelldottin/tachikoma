# Task J worker report — capture analysis, reconciliation, and evidence-tier accounting (t_497be142)

Lane: PROTOCOL / CAPTURE ANALYSIS + DUAL-ACCOUNT RECONCILIATION +
SANITIZATION + EVIDENCE INTEGRATION (Task J).

Date: 2026-10-06 (analysis performed 2026-10-05, runs 131/136)
Repository: ~/Documents/Personal/tachikoma
HEAD: 2cb5aa4 (working tree carries uncommitted Task J lane artifacts from
prior runs and sibling lanes; historical corpus untouched)
Live mutations initiated by agents: 0. No agent issued any protocol
request against the live service; all analysis is read-only over the
operator's capture file.

## 1. Repository baseline

- HEAD 2cb5aa4; git diff --check clean.
- Full suite at close: 1097 passed / 8 skipped (superset of the 1090
  baseline; the 7 new tests are SellItem3/Purchase4 checksum
  regressions in tests/test_inventory.py).
- Ruff clean on sdk/inventory/, tests/test_task_j_reconcile.py,
  tests/test_task_j_sanitize.py.
- ty clean on sdk/inventory/sanitize.py, sdk/inventory/reconcile.py.

## 2. Task I prerequisite verification

- Task I synthesis docs/swarm-synthesis-task-i-t_b4414b94.md and verifier
  t_232a3d8d gate=pass confirmed at lane start.
- HOLD_UNVERIFIED_TRANSFERABILITY preserved: every TransferPlanner
  proposal still yields proposed_quantity=0, actionable=false (pinned by
  tests/test_inventory_transfer.py; unchanged this lane).
- No Task I test was weakened or removed.

## 3. Historical corpus baseline

Six historical capture files re-scanned by the reconcile lane
(t_903bb716): GiftItem=0, ListItemsOfAShip=0 across 50,755 records.
Historical corpus (incl. 692MB pss-mitm-capture.jsonl) untouched.

## 4. Human gate procedure used

- Run 128 blocked with the exact operator procedure
  (docs/task-j-operator-capture-procedure-t_1fce40eb.md): operator to
  perform exactly ONE official-client GiftItem exchange between two
  operator-owned accounts with `mitmdump -s scripts/mitm_addon.py`
  active, capture defaulting to ~/pss-task-j-capture.jsonl.
- The operator performed a capture session on 2026-10-05 (~07:54-08:17
  UTC, 357 records, 35,050,332 bytes). The swarm did NOT ask for or
  perform any protocol mutation during analysis; the capture was
  inspected read-only after the operator finished.

## 5. What the capture actually contained (critical finding)

The session does NOT contain a GiftItem exchange and does NOT contain
any ListItemsOfAShip call. Zero Task J target endpoints were captured.
Full analysis: docs/task-j-capture-analysis-2026-10-05-t_1fce40eb.md.

The session instead contains one complete human-operated cross-account
MARKETPLACE transfer:

- 07:56:11 — account 9228834 (seller) MarketService/SellItem3,
  itemId=187296241 (ItemDesignId=360 "New Repair Guide", stack qty 6
  pre-sale), quantity=1, price 250,000 Gas. Response
  `<SellItem success="True"/>`.
- 08:00:31 — account 9238149 (buyer) MarketService/Purchase4,
  saleId=44414085, success=True. Achievements progress in the response
  is attributed to the SELLER's UserId (9228834), confirming the
  buyer/seller linkage.
- Marketplace listing channel (ListActiveMarketplaceMessages5) confirms
  SaleId 44414085 present at 08:00:15 and gone by 08:08:01.
- All four accounts in the session (9228834, 9238149, 9231708, 9181430)
  logged in sequentially from one operator-controlled device; account
  attribution is unambiguous via the login chain.

The card's controlled-test-vector criteria explicitly EXCLUDE marketplace
transactions, so this exchange cannot substitute for the GiftItem
evidence requirement. It is recorded as evidence of official-client
wire behavior only.

## 6. Protocol evidence gained (OBSERVED tier, this session only)

New wire facts, each supported solely by the 2026-10-05 capture; no
claim is promoted beyond a single session's evidence:

1. The official client (Unity 6000.0.77f1, DeviceTypeMac) does not call
   ListItemsOfAShip during login, ship view, or inventory browsing.
   Across four full account sessions, inventory Item nodes arrive
   embedded only in ShipService/GetShipByUserId,
   UserService/ListAllUserDataFirst2, and
   BattleService/FinaliseBattle15 responses. Item node shape:
   `<Item ItemId="…" ShipId="…" ItemDesignId="…" Quantity="…" IsNew="…"
   SkinKey="0"/>`.
2. ListAllUserDataFirst2 fires exactly once per login (full inventory,
   359/177/174/363 Item nodes across the four logins). Mid-session
   inventory changes surface via FinaliseBattle15 / CollectReward2 /
   next-login GetShipByUserId; no mid-session full re-fetch was
   observed. Reliable re-snapshot trigger = full client restart.
3. Checksums cracked against every available sample (formula matches
   all samples):
   - SellItem3: MD5(accessToken + itemId + quantity + currencyType +
     currencyValue + clientDateTime + 5343 + Savvy!s0d@) — 6/6 samples.
   - Purchase4: MD5(accessToken + saleId + clientDateTime + 5343 +
     Savvy!s0d@) — 1/1 sample.
   - MarkItemsNotNew helper re-verified 5/5 on new samples.
   Implemented in sdk/inventory/checksums.py (pure, non-network) with
   regression tests in tests/test_inventory.py. The GiftItem checksum
   is NOT affected: no static preimage evidence exists and no GiftItem
   request was captured, so GiftItem remains below VERIFIED.
4. SellItem3 sends a concrete account-scoped ItemId (not
   ItemDesignId); Purchase4 carries no explicit recipient parameter
   (recipient implied by accessToken). These are marketplace facts,
   recorded as OBSERVED; they do NOT transfer to GiftItem semantics.
5. CollectReward2 (messageId=590099126, account 9181430) granted
   item:1257x1 and the response Item node shows the destination stack
   merged to Quantity=3 — one OBSERVED instance of stack-merge
   behavior on grant, explicitly NOT extrapolated to GiftItem.

## 7. Dual-account reconciliation verdict

GiftItem dual-account pre/post reconciliation: NOT POSSIBLE from this
capture — no GiftItem exchange occurred.

For the marketplace transfer that did occur, reconciliation is
incomplete by the card's fail-closed standard: only ONE inventory
snapshot per account exists (login-time ListAllUserDataFirst2):
- A=9228834 PRE at 07:54 (before its sell; has the stack containing
  itemId=187296241 at design 360, stack qty 6);
- B=9238149 PRE at 07:57 (before purchase; has design-360 stack
  187363052 qty=4; does NOT hold the sold ItemId).
NO post-transaction snapshots exist for either account — the client
never re-fetched full inventory mid-session. HeartBeat4 responses
carry no inventory. B's 08:00:56 SellItem3 was a different pre-existing
item (187865520, design 359), not a re-sale of the purchased item.
Per the card, ambiguous deltas fail closed: quantity conservation for
the marketplace path is recorded as UNRECONCILED (listing-channel
disappearance of saleId 44414085 is consistent with, but does not
prove, delivery).

## 8. ItemId / ItemDesignId and quantity semantics

- Marketplace: SellItem3 references a concrete account-scoped ItemId;
  quantity parameter present and observed = 1 (partial-stack sale from
  a qty-6 stack; listing remains possible afterwards).
- GiftItem: ALL identity/quantity semantics remain UNKNOWN (no
  capture). Task I's ItemDesignId planning join remains separate from
  wire semantics and is unchanged.

## 9. Evidence-tier accounting

- ListItemsOfAShip: 0 captures. Tier unchanged:
  STATICALLY_DISCOVERED. New OBSERVED finding: the client never calls
  it in normal login/ship/inventory flows (may only fire from an
  unidentified UI path).
- GiftItem: 0 captures. Tier unchanged: STATICALLY_DISCOVERED
  (template truncated at `?itemId=`; checksum preimage has no static
  evidence). Recipient, quantity, ItemId, response, and delta
  semantics all remain UNKNOWN.
- SellItem3 / Purchase4: NEW tier CAPTURED / formula VERIFIED against
  all available samples (6/6 and 1/1) + implemented as pure helpers.
- ListAllUserDataFirst2 / GetShipByUserId as inventory carriers: NEW
  tier OBSERVED (single session).
- No claim was promoted beyond its supporting data; no historical
  evidence was re-tiered.

## 10. Sanitized fixtures added

tests/fixtures/task_j_capture/ (all scanned secret-free by
tests/test_task_j_sanitize.py::test_committed_fixtures_carry_no_secrets):

- sanitized_records.json — 16 sanitized records spanning the session
  (logins, ship views, the SellItem3 + Purchase4 exchange, heartbeat,
  inventory-carrier responses). accessToken and all secret-like
  values replaced with [REDACTED].
- sanitized_market_transfer.json — the SellItem3 -> Purchase4
  exchange pair with param name/value separation, checksums, and
  seller/buyer attribution fields.
- sanitized_inventory_observations.json — derived facts (Item-node
  carrier census, Item node shape, ListAllUserDataFirst2 login-only
  behavior, stack examples) for evidence-contract tests.

## 11. Safety verification (all pinned by tests)

- No GiftItem production path: no Client.giftItem method, no transfer
  executor, no apply path beyond the existing unconditional refusal
  (test_task_j_sanitize.py safety-property tests +
  test_task_j_reconcile.py).
- `inventory apply` refuses unconditionally: no flags, --force/--yes,
  env, or config can bypass (tests at tests/test_task_j_sanitize.py
  425-441 and test_inventory_plan_cli.py).
- No network I/O in sdk/inventory (module-import purity + socket-block
  test).
- No premium-currency path added; no Starbux was spent by anything in
  this swarm (0 by agents; the operator's marketplace sale priced in
  Gas, not Starbux).
- Fixtures contain [REDACTED] placeholders only; verified by grep and
  by the committed-fixture secret scan.

## 12. Transferability decision

NO — there is still NOT enough verified evidence to define a safe
transferability predicate.

- GiftItem evidence requirements (one captured exchange + dual-account
  pre/post reconciliation) are still unmet: 0 GiftItem captures exist.
- HOLD_UNVERIFIED_TRANSFERABILITY is preserved; every Task I proposal
  remains proposed_quantity=0, actionable=false.
- Remaining gaps (unchanged from the reconcile lane's enumeration,
  docs/worker-report-task-j-reconcile-t_903bb716.md §4): 10 GiftItem
  gaps (endpoint template, HTTP method, recipient semantics, ItemId
  semantics, quantity semantics, checksum preimage, response schema,
  sender delta, recipient delta, failure behavior) and 2
  ListItemsOfAShip gaps (0 captures; UI path unknown).

## 13. Required final synthesis (card's 28 points)

1. Baseline/HEAD: 2cb5aa4, git diff --check clean.
2. Task I prerequisite: verified (synthesis + verifier gate=pass;
   HOLD preserved).
3. Historical corpus: 6 files, 50,755 records, GiftItem=0,
   ListItemsOfAShip=0; untouched.
4. Human gate: run 128 blocked with operator procedure; operator ran
   capture session 2026-10-05; agents performed 0 live mutations.
5. Controlled test item: no gifting occurred. The marketplace transfer
   used ItemDesignId=360 (stack qty 6, 1 unit listed at 250k Gas — a
   normal in-game currency sale by the operator, not swarm-initiated).
6. ListItemsOfAShip capture evidence: NONE (0 captures; client never
   calls it in normal flows — new OBSERVED finding).
7. GiftItem request evidence: NONE (0 captures).
8. GiftItem response evidence: NONE (0 captures).
9. Recipient-identifier semantics: UNKNOWN for GiftItem (marketplace
   Purchase4 has no explicit recipient param — OBSERVED, not
   transferable).
10. ItemId/ItemDesignId semantics: UNKNOWN for GiftItem; marketplace
    SellItem3 uses concrete ItemId (OBSERVED).
11. Quantity semantics: UNKNOWN for GiftItem; SellItem3 partial-stack
    quantity=1 OBSERVED.
12. Checksum evidence: GiftItem preimage still UNKNOWN (no capture, no
    static construction). SellItem3 6/6 and Purchase4 1/1 formulas
    cracked and implemented as pure helpers.
13. Source-account before/after reconciliation: NOT POSSIBLE (no
    post-transaction snapshot; fail closed).
14. Destination-account before/after reconciliation: NOT POSSIBLE
    (fail closed).
15. Stack split/merge/new-ID behavior: not observed for GiftItem; one
    OBSERVED stack-merge on CollectReward2 grant (1257 -> qty 3).
16. Evidence-tier changes: SellItem3/Purchase4 -> CAPTURED + checksum
    formula verified vs samples; inventory-carrier behavior -> OBSERVED.
    GiftItem/ListItemsOfAShip unchanged.
17. Sanitized fixtures: 3 files in tests/fixtures/task_j_capture/.
18. Secret-leak checks: committed-fixture scan test + grep; all
    accessTokens [REDACTED].
19. Tests/validation: 77/77 targeted Task J tests; 1097 passed /
    8 skipped full suite; ruff clean; ty clean; git diff --check clean.
20. Live mutation initiated by agents: 0.
21. Human-operated GiftItem exchanges captured: 0 (the human gate for
    the one allowed exchange has not been passed yet).
22. Starbux/premium currency spent by the swarm: 0.
23. Production GiftItem reachability: 0 (no code path added).
24. Task I proposals actionable=false: all (pinned by tests).
25. Inventory apply unconditional refusal: unchanged (pinned by tests).
26. Remaining gaps: 10 GiftItem + 2 ListItemsOfAShip (unchanged).
27. Transferability evidence sufficient? NO.
28. Recommended next slice: re-run the REVISED operator procedure
    (docs/task-j-operator-capture-procedure-t_1fce40eb.md, updated
    2026-10-06: full-client-restart pre/post snapshots, sequential
    single-device logins, ListItemsOfAShip downgraded to bonus target)
    — one GiftItem exchange of quantity 1 of a safe expendable item
    between two operator-owned accounts, with login-time
    ListAllUserDataFirst2 snapshots on both sides before and after.
    Then a small analysis slice parses the new capture into the
    GiftItemExchange reconciliation model.

## Verdict

UPDATE 2026-10-06 (root blackboard gate revision, t_4b4bdc63): the
human gate was REVISED by the swarm root — Task J no longer waits for
or requires the GiftItem exchange. The 2026-10-05 operator capture
(~/pss-task-j-capture.jsonl) is accepted as official-client
MARKET-MEDIATED transfer evidence. Accordingly this lane CLOSES as
complete under the revised gate, on the following accounting:

- Analyzed as official-client evidence: SellItem3 (checksum formula
  verified 6/6 samples), Purchase4 (1/1), ListActiveMarketplaceMessages5
  SaleId 44414085 lifecycle (present 08:00:15, gone 08:08:01), login-time
  inventory snapshots on both accounts, sanitization, and evidence-tier
  accounting — all documented above and pinned by tests.
- One successful Gas-priced A->B purchase is captured. No completed
  reciprocal swap is claimed: post-transaction inventory deltas are
  UNRECONCILED (no post-transaction snapshots; fail closed, §7).
- GiftItem remains UNVERIFIED and UNREACHABLE: tier
  STATICALLY_DISCOVERED, zero captures, zero production code paths
  (§9, §11, §12).
- Transferability decision: NO (§12). Task I HOLD preserved; all
  proposals actionable=false.
- Live mutations initiated by agents: 0. Human-operated exchanges in
  the capture: exactly one market-mediated transfer (SellItem3 +
  Purchase4), performed by the operator via the official client.

The original GiftItem capture procedure
(docs/task-j-operator-capture-procedure-t_1fce40eb.md) is retained for
any future slice that wants to close the remaining GiftItem gaps;
agents must still never initiate that live mutation.
