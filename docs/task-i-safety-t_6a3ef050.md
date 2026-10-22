# Task I safety/policy lane — fail-closed + unreachability proofs (t_6a3ef050)

Swarm root / blackboard: `t_7842f6df`. Lane card: "Prove stale/protected/
unresolved/unverified cases fail closed and no GiftItem, network or mutation
path becomes reachable."

Deliverable: `tests/test_inventory_transfer_safety.py` (NEW, 56 tests,
landed, discoverable via `make test`). This lane wrote NO production code.

## 1. Method

Probe-first: every property below was first verified interactively against
the live tree (Python REPL over the real `sdk.inventory` exports, real
AST/`inspect` source of `sdk/inventory/transfer.py`, `sdk/commands.py`,
`sdk/client.py`, and the real TUI dispatch loop) and only then pinned as a
regression test. Probes were derived from the Task I brief text, not from
the planner (t_681185bf) or adversarial (t_628172e7) lanes' test files. No
sibling test file was read for assertion-shape copying; their test NAMES
were scanned only to avoid landing literal duplicates.

## 2. Independent corpus re-scan (GiftItem grounding) — run 2026-10-01

Binary-safe, case-insensitive, substring scan over every record of all 6
capture files (~757 MB):

| file | records |
|---|---|
| `~/pss-mitm-capture.jsonl` | 49,774 |
| `~/pss-login-capture.jsonl` | 681 |
| `~/.tachikoma-credential-capture/previous-capture-2.jsonl` | 66 |
| `~/.tachikoma-credential-capture/previous-capture-3.jsonl` | 84 |
| `~/.tachikoma-credential-capture/previous-capture-4.jsonl` | 74 |
| `~/.tachikoma-credential-capture/previous-capture-5.jsonl` | 76 |
| **total** | **50,755** |

Needle totals across the whole corpus:

- `giftitem` / `gift_item` / `gift-item`: **0 / 0 / 0**
- `sellitem3`: **0**; `purchase4`: **0**; `marketservice`: **0**
- `transferitem` / `transfer_item`: **0 / 0**
- `touserid`: **60**; `frienduserid`: **14** (see §2.1)
- `to_user_id`, `targetuserid`, `recipientid`, `recipient_id`: **0**

Conclusion: the constant `HOLD_UNVERIFIED_TRANSFERABILITY` grounding holds.
No ItemService/MarketService transfer, gift, sale or purchase traffic exists
anywhere in the corpus. GiftItem remains STATICALLY_DISCOVERED with a
truncated URL template; recipient parameter, quantity semantics and checksum
preimage are all unknown.

### 2.1 Correction to the reconcile lane's wording (safety conclusion unchanged)

The Task I reconcile lane (t_97ae279f) reported "zero hits for recipient-
style params (`toUserId`, ..., `recipientId`, ...)". A literal substring
scan disagrees on two needles: `ToUserId` appears in 60 records and
`FriendUserId` in 14. This lane classified every one:

- 38 records: `MessageService/ListMessagesForChannelKey` responses
  (alliance chat messages carry `ToUserId` as chat routing metadata).
- 14 records: `MessageService/ListSystemMessagesForUser3` responses.
- 12 records: `UserService/ListFriends` responses (`FriendUserId`).
- 10 records: response records in `pss-login-capture.jsonl` paired with
  the same two MessageService/UserService requests.

All 74 hits are read-only chat/friend-list response bodies. **Zero occur in
any ItemService or MarketService record**, and none describe a transfer
request parameter. The reconcile lane's safety conclusion is correct; its
zero-hit claim for those two needles was imprecise. (Their scan may have
matched on exact-case request-param names only; hits here are XML attribute
names inside response bodies.)

## 3. Axes proven (56 tests, all independently derived)

- **T1 stale (8 tests)**: stale source or destination suppresses the
  pairing (`matched 0`, `REASON_FRESH_SNAPSHOTS`, per-side freshness flags
  correct); the stale pair consumes NO arithmetic capacity (a source with
  supply 10 serving a stale destination + two fresh unmet-5 destinations
  yields 5+5=10 to the fresh ones, 0 to the stale one); boundary
  age==86400 is fresh, 86401 is stale; malformed or future `now` refuses
  the whole plan (`TransferPlannerError`), renderer included.
- **T2 protected (4 tests)**: a protected or unknown stack of the paired
  design on EITHER side suppresses the pairing (`REASON_NO_PROTECTION_
  CONFLICT`, matched 0) even when the usable-supply arithmetic already
  excludes protected candidates — the fail-closed reading, confirmed
  from first principles.
- **T3 unresolved (4 tests)**: a design-scoped unresolved condition on
  either side blocks; account-global conditions do NOT block (Task H
  precedent); a condition scoped to a different design never bleeds
  into the paired design's proposal.
- **T4 unverified (6 tests)**: across an exhaustive 16-shape portfolio
  matrix (stale/condition/protected toggled on each side), every proposal
  carries `HOLD_UNVERIFIED_TRANSFERABILITY`, proposed 0, actionable False;
  ELIGIBLE never assigned; `REASON_NO_EVIDENCE_PROMOTION` never stamped
  (structurally satisfied); evidence never above the GiftItem floor
  (weakest-input order check); perfect inputs hold with the constant as
  the ONLY reason.
- **T5 no GiftItem path (4 tests)**: no forbidden-endpoint call site in
  transfer.py (AST); GiftItem string constants carry no protocol payload
  (no `/ItemService/GiftItem`, no host, no md5/sha, no recipient-param
  template — the renderer's quoted `'?itemId='` truncation fragment is
  explicitly allowed as documentation of the gap); same payload scan
  across all sdk/inventory modules; serialized plans contain no
  endpoint/route/command/url/checksum vocabulary at all.
- **T6 no network path (5 tests)**: transfer.py imports only
  dataclasses/typing/`__future__`/local pure modules (AST); source free
  of network tokens; planner + renderer complete under a blocked-socket
  namespace at runtime; `sdk/client.py` imports nothing from
  `sdk.inventory` (import statements, not substrings); `sdk/commands.py`
  never imports `plan_transfers`/`transfer` from sdk.inventory.
- **T7 no mutation path (8 tests)**: no transfer verb in the command
  registry; `inventory apply` = single unconditional return (AST: no If,
  no settings/env access), refuses from registry dispatch with any args,
  from the TUI loop, from help; refusal survives armed config
  (`inventory.enabled=true`, `starbux_cap_per_run=999`) and armed env
  (`ENABLE_MUTATION=1`, `INVENTORY_ENABLED=1`); apply stays unverified;
  no `Client()` construction or `execute` call in any sdk/inventory
  module (AST).
- **T8 forged inputs (7 tests)**: hand-built `AccountPortfolio` with
  duplicate accounts aborts; a `dataclasses.replace`-forged snapshot
  (tampered unmet) is caught by loader re-validation — inside
  `aggregate_snapshots` (PortfolioError) BEFORE the planner even runs,
  an even earlier refuse point than the planner's own gate; non-portfolio
  inputs (None/list/str/int/tuple/dict) all refuse; non-snapshot account
  entries refuse; bad `stale_after` types (negative/bool/float/str/None)
  refuse; `actionable=True` on a hand-built portfolio input is ignored;
  planning never mutates the portfolio.
- **T9 non-actionability (8 tests)**: serialized plans actionable=false
  and proposed 0 on every row; no `item_id` key in serialized output;
  `actionable_proposals()` structurally empty; rendered view pins
  READ-ONLY, HOLD_UNVERIFIED_TRANSFERABILITY, the GiftItem named blocker,
  the apply-refusal footer; no ItemId in rendered text; empty-state and
  stale-marker rendering; replan determinism.

## 4. Defects found in the landed surface

**None.** Zero production defects. Three of my own probe-test bugs were
fixed during pinning (arithmetic expectation error in the stale-capacity
test, over-strict docstring scan that rejected the renderer's legitimate
`'?itemId='` gap documentation, exception-family mismatch: the forged
snapshot is caught by `aggregate_snapshots` with `PortfolioError`, not the
planner — a STRONGER fail-closed position than the card required).

## 5. Validation (live runs, this lane)

- `python -m unittest discover -s tests`: **991 OK, skipped=1**
  (baseline before this lane: 935 OK; +56 exactly).
- `python -m unittest tests.test_inventory_transfer_safety`: **56/56 OK**.
- `ruff check tests/test_inventory_transfer_safety.py`: clean.
- `ty check tests/test_inventory_transfer_safety.py`: **All checks
  passed** (0 diagnostics — stricter than the repo's `--exit-zero` gate).
- `git diff --check`: clean.
- No live Pixel Starships traffic. Sockets blocked in runtime isolation
  tests. All fixtures synthetic.

## 6. Notes for the verifier and the UX lane

- The UX lane (t_7ed909c9) has NOT landed `inventory transfers` command
  wiring yet; my T7 pins prove the registry TODAY has no transfer verb and
  `plan_transfers` is unreachable from commands.py — when the UX lane adds
  the thin dispatch, the T6 `commands.py` import pin may need its allowlist
  updated (it currently forbids any `plan_transfers`/`transfer*` import
  from sdk.inventory in commands.py). Flagging this explicitly so the
  UX lane updates that single test in the same change rather than
  discovering it red.
- The corpus `ToUserId`/`FriendUserId` finding (§2.1) should be folded
  into the final synthesis: it does not change any gate, tier, or decision,
  but the "zero recipient-param hits" phrasing in the reconcile report and
  Task H-era docs should be corrected to "zero hits in any ItemService or
  MarketService record; 74 hits in MessageService/UserService chat and
  friend-list response bodies, all classified non-transfer".
- Pre-existing (NOT this swarm's): 10 ruff F401/F811 errors in
  sdk/crew_leveling.py, sdk/ship_layout.py, tests/test_game_formulas.py,
  tests/test_ship_layout.py — committed files untouched by Task I.
