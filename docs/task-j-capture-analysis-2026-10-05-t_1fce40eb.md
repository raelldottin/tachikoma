# Task J capture analysis — 2026-10-05 session (t_1fce40eb)

Status: ANALYZED, COMPLETE FOR THE REVISED PROTOCOL WORKER GATE
(rechecked 2026-10-11). Transferability remains INSUFFICIENT; the separate
swarm verifier has not been passed by this worker. The operator ran a capture session on
2026-10-05 (~07:54–08:17 UTC, file `~/pss-task-j-capture.jsonl`, 35,050,332
bytes, 357 records), but the session does NOT contain the GiftItem
exchange or any ListItemsOfAShip call. The root blackboard revision of
2026-10-06 explicitly accepts MARKET-MEDIATED evidence and removes the
GiftItem capture requirement for this slice. This document records what the session does contain, the
protocol facts learned, and the resulting corrections to the operator
procedure (applied to
`docs/task-j-operator-capture-procedure-t_1fce40eb.md`).

No agent initiated any live mutation. All analysis below is read-only
over the capture file. No tokens, emails, device keys, or passwords
appear in this document; accounts are identified by numeric UserId only.

## 1. Session inventory (sanitized)

357 JSONL records, all hosts:

| Host | Records |
|---|---|
| api.pixelstarships.com | 291 |
| collect.analytics.unity3d.com | 50 |
| config.services.api.unity.com | 8 |
| other Unity endpoints | 6 |
| assets.pixelstarships.com | 2 |

Four sequential account logins from one device (one at a time, not
simultaneous). Numeric UserIds observed, in login order:

1. 9228834 (DeviceLogin17 + UserGameCenterAuthorize)
2. 9238149 (UserEmailPasswordAuthorize4 -> DeviceLogin17)
3. 9231708 (UserEmailPasswordAuthorize4 -> DeviceLogin17)
4. 9181430 (UserEmailPasswordAuthorize4 -> DeviceLogin17)

Endpoint census (individual request/response records, PSS API only;
these counts are NOT pair counts):

| Endpoint | Records | Notes |
|---|---|---|
| UserService/HeartBeat4 | 52 | keepalive |
| MessageService/ListMessagesForChannelKey | 34 | chat |
| UserService/ListFriends | 28 | |
| MessageService/ListSystemMessagesForUser3 | 14 | |
| MessageService/ListActiveMarketplaceMessages5 | 14 | marketplace browse |
| MarketService/SellItem3 | 12 | listings, see §3 |
| ItemService/MarkItemsNotNew | 10 | |
| UserService/DeviceLogin17 | 8 | |
| DesignService/ListAll{Static,Dynamic}Designs2 | 16 | |
| UserService/UserGameCenterAuthorize | 8 | |
| UserService/UserEmailPasswordAuthorize4 | 6 | |
| ShipService/GetShipByUserId | 8 | carries Item nodes |
| UserService/ListAllUserDataFirst2 | 8 | carries Item nodes |
| UserService/SetTutorialStatus | 8 | |
| MessageService/ActionMessage | 6 | all errored, see §4 |
| MissionService/CreateMission3 / SelectEvent4 | 12 | |
| BattleService/UpdateManualCommands / FinaliseBattle15 | 9 | |
| MessageService/ListPrivateMessages | 6 | |
| MarketService/Purchase4 | 2 | one purchase, see §3 |
| MessageService/CollectReward2 | 2 | reward claim, see §4 |
| FileService/* , SettingService/*, LiveOpsService/* | misc | |

### Task J target endpoints in this session

| Endpoint | Count |
|---|---|
| ItemService/GiftItem | **0** |
| ItemService/ListItemsOfAShip | **0** |

## 2. Protocol facts learned (evidence tier: OBSERVED, single session)

These are new observations about official-client inventory-read behavior.
Each is supported by the capture only; no claim below is promoted beyond
one session's evidence.

1. **The official client does not call ListItemsOfAShip during normal
   login, ship view, or inventory browsing.** Across four full account
   sessions — including GetShipByUserId (ship view) and
   ListAllUserDataFirst2 (user-data load) — zero ListItemsOfAShip
   requests occurred. Inventory Item nodes arrive embedded in:
   - `ShipService/GetShipByUserId` responses;
   - `UserService/ListAllUserDataFirst2` responses;
   - `BattleService/FinaliseBattle15` responses (post-battle deltas).
   Item node shape (all three carriers, identical):
   `<Item ItemId="…" ShipId="…" ItemDesignId="…" Quantity="…" IsNew="…"
   SkinKey="0" />`
   Example observed (account 9228834, design 360, stack of 6):
   ItemId=187296241, ShipId=6957420, ItemDesignId=360, Quantity=6.

2. **Pre/post inventory snapshots for reconciliation are obtainable
   without ListItemsOfAShip.** ListAllUserDataFirst2 fires once per
   login and carries the full item list (359/177/174/363 Item nodes
   across the four logins). Full client restart (re-login) is the
   observed reliable trigger; merely re-opening the ship view did NOT
   fire a fresh ListAllUserDataFirst2 in this session.

3. **ListAllUserDataFirst2 only fires at login.** Within each account's
   session, exactly one ListAllUserDataFirst2 occurs, immediately after
   login. Inventory changes mid-session (marketplace sells, reward
   claims) were reflected in later responses (FinaliseBattle15,
   CollectReward2, GetShipByUserId on next login) but no mid-session
   full-inventory re-fetch was observed.

4. **One device, sequential logins is a workable capture topology.**
   Four accounts were captured sequentially from one device; token ->
   UserId attribution is unambiguous via the login chain (authorize
   response UserId + immediately following GetShipByUserId userId
   parameter).

## 3. What the operator actually did (marketplace path, not gifting)

The session contains a cross-account item movement via the MARKETPLACE,
not the gift flow:

- 07:56:11 — account 9228834 `MarketService/SellItem3` itemId=187296241
  (ItemDesignId=360, stack qty 6 pre-sale), quantity=1, price
  currencyType=Gas currencyValue=250000. Response:
  `<MarketService><SellItem success="True" /></MarketService>`
- 08:00:31 — account 9238149 `MarketService/Purchase4` saleId=44414085
  (the listing created above), allowConversion=False. Response:
  `<Purchase success="True" />` plus achievement progress for UserId
  9228834 (the seller).
- Additional SellItem3 listings by 9238149 (itemId=187865520,
  ItemDesignId=359, qty 34 stack; listed qty 1 at Mineral 50000) and by
  9181430 (itemId=184469427 twice; itemId=181676245 twice), all
  success=True.

Market observations (now Task J scope under the revised gate):

- SellItem3 request template (OBSERVED, 6 captures):
  `/MarketService/SellItem3?itemId=&quantity=&currencyType=&
  currencyValue=&clientDateTime=&checksum=&accessToken=`
  — concrete ItemId (account-scoped), not ItemDesignId.
- Purchase4 request template (OBSERVED, 1 capture):
  `/MarketService/Purchase4?saleId=&clientDateTime=&checksum=&
  accessToken=&allowConversion=False` — recipient is implied by
  accessToken; no explicit recipient parameter.
- The root revision accepts these observations for Task J. They do not
  establish GiftItem semantics and do not authorize any production mutation.

### Independently rechecked evidence chain (2026-10-11)

Baseline HEAD: `f90581bcc5b6f8c0c8d60fd2c5fc78cd1f553d93`.
The working tree contains pre-existing changes from other lanes; this
worker changes only this analysis, its procedure status, a derived fixture,
and evidence-contract tests. No applicable active supervisor slice prompt
exists in the local queue (all four entries are done); the supplied worker
card and its root revision define this bounded evidence lane.

Raw capture SHA-256:
`7edadfcdc55806a79836ebcd6453d7d2d8b5d18d3cc8949829af31ec2b3c9ed4`.
357 records / 35,050,332 bytes were parsed without printing raw credentials.
Line numbers below are one-based JSONL record numbers. Accounts A and B
refer to the seller and distinct buyer; the new derived fixture uses aliases.

| Evidence | Source lines | Observation |
|---|---|---|
| A inventory | 17-20 | GET GetShipByUserId and ListAllUserDataFirst2; design 360, source stack quantity 6 |
| SellItem3 | 45-46 | POST; concrete itemId; quantity 1; Gas 250000; HTTP 200 and SellItem success=True |
| B inventory | 73-76 | GET carriers; a distinct design-360 stack quantity 4; design-359 stack quantity 34 |
| Matching listing | 112, 121 | GET ListActiveMarketplaceMessages5; SaleId 44414085; seller A; Argument=item:360x1; ActivityArgument=gas:250000 |
| Purchase4 | 126-127 | POST; exact matching saleId; allowConversion=False; HTTP 200 and Purchase success=True |
| B's later listing | 132-133 | Different pre-existing design 359; one item at Mineral 50000; no second Purchase4 captured |
| Later comparable browse | 211, 220 | Same unrestricted filters as line 112; target SaleId absent at 08:07:19 UTC |
| Filtered browse | 223-224 | itemDesignId=359 excludes target design 360; absence here is NOT evidence of disappearance |

This corrects the sibling report's use of the 08:08:01 browse as lifecycle
proof: the valid comparable observation is 08:07:19. Absence from a browse
response alone does not prove global removal or inventory delivery.
SellItem3 returns no SaleId; the join is supported by the listing's seller,
design, quantity, price, session chronology, and the exact Purchase4 SaleId.
The purchase's achievement UserId names the seller, not the buyer. The buyer
is attributed through the distinct preceding login/ship session. The capture
contains six SellItem3 pairs, one Purchase4 pair, seven marketplace browse
pairs, and four pairs for each inventory carrier. The earlier census heading
incorrectly called doubled record counts pairs; it is corrected above.

The market requests use POST with form bodies; their field names/values
also appear in the captured URL. SellItem3 fields are itemId, quantity,
currencyType, currencyValue, clientDateTime, checksum, accessToken.
Purchase4 fields are saleId, clientDateTime, checksum, accessToken,
allowConversion. No explicit recipient identifier is sent by Purchase4.
The receiving stack's ItemId, merge/split behavior, fees, resulting currency
balances, and quantity conservation are UNKNOWN: neither A nor B has an
inventory-carrier response after the purchase. A's pre-sale total is 6;
B's pre-purchase total is 4. Do not substitute expected totals 5 and 5 for
observed post-state. Other accounts' later snapshots cannot close this gap.

The supplied operator provenance, existing mitmproxy tooling, UnityPlayer
6000.0.77f1 headers and login/ship sequence support official-client origin.
Headers are supporting evidence, not independent proof of origin or account
ownership. The capture does not independently prove expendability or absence
of protected-item reservations. The selected request is Gas-priced with
allowConversion=False; it does not establish a session-wide premium-spend
total or the buyer's final Gas debit.

### Evidence tiers and sanitization

Market endpoint request/response shapes advance from static discovery to
CAPTURED_NOT_VERIFIED, with this session as the exact source. Unreconciled
delivery/stack semantics remain CAPTURED_AMBIGUOUS. GiftItem stays
STATICALLY_DISCOVERED; no GiftItem or ListItemsOfAShip claim is promoted.
The earlier six-file / 50,755-record corpus census is a historical Task I
result, not a fresh corpus scan in this run.

The sibling's checksum report records SellItem3 agreement in 6/6 samples
and Purchase4 agreement in 1/1. This worker verifies helper behavior using
synthetic authentication stand-ins only; it does not use captured credentials
to recompute those hashes. The formula candidates are:

- SellItem3: MD5(accessToken + itemId + quantity + currencyType +
  currencyValue + clientDateTime + ChecksumKey + SavyChecksum).
- Purchase4: MD5(accessToken + saleId + clientDateTime + ChecksumKey +
  SavyChecksum); the helper excludes allowConversion.

The reported agreement is separate from independent formula verification.
In particular, one Purchase4 sample cannot prove the preimage or exclusion
of a flag whose observed value never varies. No independent complete
preimage derivation was established by this worker. Under the existing
checksum_tier_ceiling contract the candidate remains CAPTURED_AMBIGUOUS;
even corroboration of a single observation would stay below VERIFIED.
GiftItem's checksum construction remains unknown. No executable path is added.

Existing sanitized request/response fixtures remain intact. The new
`tests/fixtures/task_j_capture/sanitized_market_evidence.json` contains
only whitelisted derived facts, aliases, capture line references, and the
capture digest. It carries no raw headers, usernames, emails, credential
values, or signatures. It explicitly records missing post-state, the valid
browse filter comparison, and the fail-closed decision. Secret scans and
evidence-contract tests cover the new fixture alongside existing fixtures.

## 4. Other observations

- All three `MessageService/ActionMessage` calls (messageId=594319814,
  594320279, 594320753) returned `<ActionMessage errorMessage="An error
  occurred." />`. Same failure signature previously recorded for this
  endpoint. Not a Task J target; no conclusion drawn.
- `MessageService/CollectReward2` (messageId=590099126) succeeded for
  account 9181430: reward `item:1257x1`, response carried the granted
  Item node (ItemId=181678275, ItemDesignId=1257, Quantity=3 → the
  recipient stack became Quantity=3, i.e. merged into an existing
  stack). Single observation; recorded as OBSERVED only.

## 5. Verdict for Task J

- ListItemsOfAShip: 0 captures in this session. Remains
  STATICALLY_DISCOVERED / CAPTURED_NOT_VERIFIED per the evidence
  hierarchy. The prior full-corpus scan result (0 across all six
  historical files) is unchanged.
- GiftItem: 0 captures in this session. Remains
  STATICALLY_DISCOVERED; checksum preimage unknown (template truncated
  at `?itemId=`); recipient/quantity/ItemId semantics all still open.
- Market dual-account reconciliation: UNRECONCILED because both post-purchase
  inventory snapshots are missing. One successful A->B Gas-priced purchase
  is observed. A completed reciprocal swap is NOT proved.
- Task I HOLD_UNVERIFIED_TRANSFERABILITY: unchanged. Every proposal
  remains proposed_quantity=0, actionable=false.
- Live mutations initiated by agents: 0 (all mutations in the capture
  were performed by the human operator via the official client).

## 6. Historical procedure and next evidence slice

The 2026-10-02 procedure assumed "open the ship / inventory view" would
trigger ListItemsOfAShip and assumed both accounts needed to be online
simultaneously. Observation §2 refutes both assumptions for this client
version (Unity 6000.0.77f1, DeviceTypeMac):

- Inventory snapshots come from ListAllUserDataFirst2 at LOGIN, and the
  reliable re-capture trigger is a full client restart, not re-opening
  a view.
- Sequential single-device logins suffice.
- ListItemsOfAShip remains a bonus target: it may only fire from a
  specific UI path not yet identified. Its absence from a capture must
  not block GiftItem analysis.

The earlier GiftItem procedure is retained as an OPTIONAL FUTURE reference,
not a blocker or instruction to perform an exchange for this worker. The
root's revised human gate is satisfied by analyzing the existing marketplace
capture. Production GiftItem execution remains unreachable and inventory
apply remains an unconditional refusal, including config/env/force/yes paths.

Is evidence sufficient to define a safe transferability predicate? NO.
Missing evidence includes both inventory post-states, delivery/stack behavior,
failure/error cases, protected-item and currency constraints, and the unresolved
GiftItem protocol and ListItemsOfAShip gaps. The recommended next slice is
the existing independent verifier `t_369a929e` reviewing this revised evidence
handoff. Any later evidence collection must be separately operator-authorized;
do not replay requests, add execution, or claim an already completed swap.

## 7. Current validation (2026-10-11)

- Before this worker's changes: 77 Task J tests passed; full suite ran
  1097 tests, OK (skipped=1).
- After changes: 86 Task J tests passed (nine new evidence contracts),
  673 inventory tests passed, and `make test` ran 1106 tests,
  OK (skipped=1). This is the complete mocked end-to-end suite; no live
  game request or operator exchange was initiated for validation.
- `uv run ruff check tests/test_task_j_market_evidence.py`: passed.
- `uv run ty check sdk/inventory/reconcile.py sdk/inventory/sanitize.py
  sdk/inventory/checksums.py`: passed; these modules were not modified.
- `git diff --check`: passed. Existing secret-scan, Task I HOLD, GiftItem
  reachability, purity, and unconditional apply-refusal tests all passed.
- A separate read-only comparison against the raw capture confirmed the
  derived fixture's digest, endpoint request/response counts, methods,
  success responses, seller/buyer association, pre-inventory quantities,
  missing post-state, and browse filters. No captured authentication value
  was used for checksum computation or network access.

Machine-readable completion facts are recorded in the worker's Kanban
handoff. This worker does not assign the swarm verifier's
`gate=pass` or change the queue, tenant registry, or unrelated dirty files.
