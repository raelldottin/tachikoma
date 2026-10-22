# Task J Operator Capture Procedure (pre-capture lane, t_1fce40eb)

Status: OPTIONAL FUTURE GIFTITEM PROCEDURE, NOT A CURRENT TASK J GATE
(status corrected 2026-10-11). The root blackboard revision on 2026-10-06
accepts the existing MARKET-MEDIATED capture and explicitly removes the
requirement to wait for or perform GiftItem. This document preserves the
earlier gift-only procedure for a separately authorized future evidence
slice; it does not ask the operator to perform another action now.
The first capture session
(2026-10-05, `~/pss-task-j-capture.jsonl`) was analyzed and did NOT
contain the gift exchange — see
`docs/task-j-capture-analysis-2026-10-05-t_1fce40eb.md`. This revision
incorporates what that session taught about how the client fetches
inventory. No agent may perform the live GiftItem action. Agents must
never synthesize or replay a GiftItem request.

Swarm root: t_4b4bdc63. Evidence slice: Task J.

## 1. Baseline (updated 2026-10-06)

| Source | ListItemsOfAShip hits | GiftItem hits |
|---|---|---|
| ~/pss-mitm-capture.jsonl (Aug 16, 692MB) | 0 | 0 |
| ~/pss-login-capture.jsonl (Aug 1) | 0 | 0 |
| ~/.tachikoma-credential-capture/previous-capture-2..5.jsonl (Jul 31) | 0 | 0 |
| ~/pss-task-j-capture.jsonl (Oct 5, 357 records) | 0 | 0 |

Verdict: still zero ListItemsOfAShip captures and zero GiftItem
captures. The Oct 5 session is fully analyzed (link above): it showed
the official client pulls inventory from `ListAllUserDataFirst2` at
login and embeds Item nodes in `GetShipByUserId` responses — it did NOT
call ListItemsOfAShip anywhere in four full account sessions.

## 2. Capture tooling (already in place, one change this lane)

`scripts/mitm_addon.py` is the existing mitmproxy addon. It captures all
api.pixelstarships.com requests and responses to a JSONL file. This lane
changed ONLY its output path: it now defaults to
`~/pss-task-j-capture.jsonl` (env-overridable via PSS_CAPTURE_FILE) so a
fresh session never appends into the 692MB historical corpus. Everything
else (headers, bodies, full URL including query strings = access tokens
and checksums) is captured as before. Syntax-checked; no other behavior
changed. The historical corpus is untouched.

## 3. Controlled test-vector criteria (ALL must hold before gifting)

- Source and destination are accounts owned/controlled by you, the operator.
- Source and destination are DISTINCT accounts.
- The item is known, common, and expendable: loss of the entire stack would
  be acceptable.
- Quantity gifted is exactly 1 unless the official UI forces whole-stack.
- No Starbux / premium currency is spent by the gifting action itself.
- No marketplace listing, sale, or deletion is involved.
- The item is NOT protected per docs/security/protected-item-policy.md:
  not equipped on any character, not a consumable reserved by crafting /
  training / module / strategic-reserve policy, not premium, not
  irreplaceable.
- The item can be unambiguously identified before and after: a common
  stackable consumable or material with a known ItemDesignId, present in
  quantity >= 2 on the source account (so a post-gift stack of >= 1
  remains identifiable) and either absent or stacked on the destination
  account (so the receiving delta is unambiguous).

If you cannot name an item that satisfies every criterion above, STOP and
report which criterion fails. Do not guess.

## 4. Exact operator procedure (REVISED 2026-10-06 per observed client behavior)

What the Oct 5 session established (see the analysis doc):

- Inventory snapshots come from `UserService/ListAllUserDataFirst2`,
  which fires ONCE PER LOGIN. Re-opening the ship view does NOT trigger
  a fresh inventory fetch. The reliable snapshot trigger is a full
  client restart (log out / kill and relaunch, then log back in).
- Sequential single-device logins work fine; both accounts do NOT need
  to be online at the same time.
- Do NOT use the marketplace. The Oct 5 session used SellItem3 +
  Purchase4 to move an item between accounts; that path is explicitly
  excluded from the Task J test vector and cannot substitute for the
  gift flow.

Preconditions: two of your own PSS accounts (A = sender, B = recipient),
both known to you; mitmproxy running on your Mac with its CA trusted by
the device running the client.

1. Start the capture with a FRESH file so this session is not mixed
   with the analyzed Oct 5 one:

       cd ~/Documents/Personal/tachikoma
       PSS_CAPTURE_FILE=~/pss-task-j-capture-2.jsonl mitmdump -s scripts/mitm_addon.py

   Confirm `~/pss-task-j-capture-2.jsonl` is being created.
   (Leave `~/pss-task-j-capture.jsonl` untouched — it is analyzed
   evidence.)
2. RECIPIENT PRE-SNAPSHOT: fully log in on account B and wait until the
   ship view is loaded (this fires ListAllUserDataFirst2 for B). Note
   the time. Then fully log out / kill the client.
3. SENDER PRE-SNAPSHOT: fully log in on account A the same way. Note
   the time.
4. Perform the gift from account A's session: open the chosen item's
   in-game gift/send flow, select account B as recipient (or B's user
   ID if the UI asks), send quantity 1. EXACTLY ONE gift. Do NOT retry,
   do NOT gift a second item, do NOT use the marketplace.
5. Confirm in-game that the gift went through (the client shows a
   success state and A's stack decreases).
6. SENDER POST-SNAPSHOT: fully log out of A, relaunch, log back in.
   Wait for the ship view to load (fresh ListAllUserDataFirst2 for A).
7. RECIPIENT POST-SNAPSHOT: log out, log in on account B, wait for the
   ship view to load (fresh ListAllUserDataFirst2 for B).
8. Stop mitmdump (Ctrl+C).
9. Tell the swarm the capture is done and the file path. The agents will
   parse, sanitize, and reconcile; nothing further is needed from you
   unless a step is missing from the capture.

Note on ListItemsOfAShip: the Oct 5 session showed the client never
calls it during normal play. If the gift flow's UI happens to trigger
it, it will be captured automatically; its absence is now EXPECTED and
will not block GiftItem analysis. Inventory snapshots for
reconciliation come from ListAllUserDataFirst2 and GetShipByUserId.

## 5. What agents will extract after the operator acts

From the new capture file only (never replayed):

- Item inventory snapshots for both accounts pre/post gift, from
  `ListAllUserDataFirst2` and/or `GetShipByUserId` Item nodes:
  ItemId, ItemDesignId, Quantity, ShipId, response shape.
  (If the client happens to call ItemService/ListItemsOfAShip, that is
  captured too and analyzed the same way — but the Oct 5 session
  showed it does not fire during normal play, so its absence is
  expected and not blocking.)
- The single ItemService/GiftItem request: full parameter set,
  recipient parameter name/value, ItemId vs ItemDesignId, quantity
  semantics, clientDateTime, checksum value (value used in preimage
  analysis only, never committed), access-token placement (redacted).
- The GiftItem response: schema, success indication.
- Dual-account reconciliation: source before/after, destination
  before/after, expected vs observed deltas, stack merge/split/new
  ItemId behavior. Fail closed on any ambiguity.
- Checksum: static-evidence comparison only. The static template is
  truncated at `?itemId=` (STATICALLY_DISCOVERED, no independent
  preimage evidence), so a single capture CANNOT promote GiftItem's
  checksum to VERIFIED. Single-observation ceiling applies.

## 6. Safety state (unchanged by this lane)

- GiftItem remains STATICALLY_DISCOVERED, zero captures, checksum
  preimage unknown (template truncated at `?itemId=`).
- HOLD_UNVERIFIED_TRANSFERABILITY; every Task I proposal remains
  proposed_quantity=0, actionable=false.
- No GiftItem call site exists anywhere in sdk/; `inventory apply`
  remains an unconditional refusal; GiftItem execution unreachable.
- Live mutations initiated by agents during this lane: 0.
