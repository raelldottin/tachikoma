# Task G evidence gate — worker report (t_f4dd7bad)

Swarm root / blackboard: `t_af901051`. Worker lane: evidence gate, ListItemsOfAShip
capture search, demand-coverage matrix, repository reconciliation. HEAD at lane start:
`2cb5aa4`, branch `main`, dirty worktree carrying the uncommitted Slice 0/1, Task D,
and Task F output (by design; no lane committed).

Deliverable type: read-only evidence verification. **This lane changed no production
code and no tests.** Its outputs are the machine-checkable facts below, posted to the
blackboard for the planner/verifier lanes, plus one documentation correction
(`INVENTORY_ENDPOINT_ANALYSIS.md` capture-coverage note, §5 below) and this report.

All numbers in this report were computed by the lane itself against the real capture
files and the repository working tree on 2026-09-26 — not transcribed from worker
reports.

## 1. ListItemsOfAShip capture search (primary evidence question)

Question: does a `ItemService/ListItemsOfAShip` capture exist anywhere in the capture
corpus, so Task G could treat the inventory feed as checksum-verified?

Method: binary-safe, case-insensitive substring scan for `listitemsofaship` and the
prefix-tolerant variant `itemsofaship` over every record of every capture file
(300 records in the four credential-capture files):

| Capture file | Records | ListItemsOfAShip hits | Variant hits |
|---|---|---|---|
| `~/pss-mitm-capture.jsonl` (2026-08-08/16 MITM, 692 MB) | 49,774 | **0** | 0 |
| `~/pss-login-capture.jsonl` (2026-08-01) | 681 | **0** | 0 |
| `~/.tachikoma-credential-capture/previous-capture-2.jsonl` | 66 | **0** | 0 |
| `~/.tachikoma-credential-capture/previous-capture-3.jsonl` | 84 | **0** | 0 |
| `~/.tachikoma-credential-capture/previous-capture-4.jsonl` | 74 | **0** | 0 |
| `~/.tachikoma-credential-capture/previous-capture-5.jsonl` | 76 | **0** | 0 |
| **Total** | **50,755** | **0** | **0** |

Result: **ListItemsOfAShip has NEVER been captured — confirmed against all 50,755
records of all 6 capture files.** The scan was binary-safe (two records in the MITM
corpus contain non-UTF-8 bytes and failed strict JSON decode; both were re-scanned
leniently; each carries 2 Item tags of primary-account items but zero
ListItemsOfAShip references).

The ItemService records that DO exist in the corpus (12 in MITM + 2 in login +
2 FixUnsynchronizedItems) are all mutation endpoints: MarkItemsNotNew (3+1),
CombineItem4 (1+1), ConsumeItem2 (6), ActivateItem4 (2), FixUnsynchronizedItems (2).
Zero inventory-feed (read) captures exist.

**Evidence consequence for Task G: the inventory-feed evidence cap must be preserved
(CAPTURED_NOT_VERIFIED, not VERIFIED).** Per the task brief: "If absent, preserve the
inventory-feed evidence cap. Compute unreserved=max(0,observed-total_reserved)". The
observed quantities Task G will consume are GetShipByUserId-derived, captured but not
checksum-verified, and evidence must never be promoted. No live traffic may be created
solely to obtain a capture; none was.

## 2. Inventory-feed field evidence (what "observed" can prove)

Item-node attributes observed across all capture files, both accounts (4,168 raw
`<Item ` byte occurrences in the primary MITM file, 4,164 of them in decodable
response bodies; 5,778 more in the login/credential captures; 9,942 total decoded):

| Attribute set | Tag count | Where |
|---|---|--- |
| `ItemId, ShipId, ItemDesignId, Quantity, IsNew, SkinKey` (the six) | 9,572 | all captures, both accounts |
| six + `BonusEnhancementType, BonusEnhancementValue` | 370 | login/credential captures ONLY; second account (userId 9231708, ships 2708491/6959709) |

- The primary account's items (ship 6957420) NEVER carry enhancement attributes in
  any capture. The six-attribute model in `CAPTURED_ITEM_ATTRIBUTES` remains correct
  for this account.
- The documented "4,168 captured Item tags" figure counts raw byte occurrences
  (4,168) vs decodable response-body tags (4,164); the 4-tag delta is exactly the two
  binary-corrupted response lines (they carry no decodable Item content).
- Task G consequence: per-item enhancement data (e.g. `BonusEnhancementType`) is
  observed only for a DIFFERENT account's ships; the primary account's inventory feed
  cannot support any enhancement-based distinction. Any "malformed" handling must
  tolerate, not consume, extra attributes (catalog.py already preserves them in
  `raw` and logs).

## 3. Demand-coverage matrix (what each Task F demand source can and cannot claim)

Grounded in `sdk/inventory/demand.py` (condition codes), `classify.py` (roles),
`_types.py` (sources), and re-run live over the 2026-08-08 capture
(GetShipByUserId snapshot with 356 stacks + ListAllDynamicDesigns 2,557 designs).

### 3.1 Source-by-source matrix

| Demand source | Repository support | Ledger behavior | Evidence tier | Live-ship count |
|---|---|---|---|---|
| EQUIPMENT (current state) | `Character/@ItemIds` on GetShipByUserId | whole-stack claim per distinct ItemId; dedupe + `EQUIPMENT_SHARED_STACK` | CAPTURED_NOT_VERIFIED | 16 refs / 11 stacks -> 11 entries |
| MODULE (current state) | `Room/@ItemIds` | whole-stack claim; dedupe + `MODULE_MULTI_ATTACH`; overlap with equipped -> one EQUIPMENT claim + `BOTH_EQUIPPED_AND_ROOM_ATTACHED` | CAPTURED_NOT_VERIFIED | 11 refs / 4 stacks -> 4 entries |
| STRATEGIC | explicit config only (`settings["inventory"]["strategic_reserve"]`) | absent config -> 0 + `SOURCE_UNSUPPORTED`; malformed/duplicate dropped | INFERRED (never promoted) | 0 entries (no config) |
| CRAFT | `ItemDesign/@Ingredients` driven ONLY by explicit objectives | objectives empty by default -> 0 + `SOURCE_UNSUPPORTED`; recursive gross expansion; cycles/magnitude bounded | INFERRED | 0 entries |
| TRAINING | **NONE** — one-at-a-time runtime flow; no plan/quantity representation | ALWAYS 0 + `SOURCE_UNSUPPORTED` on every plan | n/a | 0 entries |
| AMMUNITION | **NONE** — RebuildAmmo3 sends `ammoCategory`; server-side restock | ALWAYS 0 + `SOURCE_UNSUPPORTED` on every plan | n/a | 0 entries |

### 3.2 Unsupported-but-applicable demand (the Task G blocking rule, live-grounded)

Per the brief: "Unsupported training/ammunition demand blocks only applicable designs."
On the live ship, the exactly-4 stacks blocked by this rule (unprotected, unreserved>0,
carrying the applicable role) are:

| ItemId | Design | Name | Qty | Applicable unsupported role |
|---|---|---|---|---|
| 181673752 | 11 | Ion Core | 15 | AMMUNITION_MATERIAL |
| 182315577 | 359 | Repair Guide | 2 | CREW_TRAINING_MATERIAL |
| 184469427 | 307 | Espresso Shot | 5 | CREW_TRAINING_MATERIAL + CRAFTING_INTERMEDIATE |
| 188673662 | 702 | Military Recruit Handbook | 1 | CREW_TRAINING_MATERIAL |

That is the complete population: 4 stacks on the live ship. All other
training/ammo-role stacks are blocked anyway (protected, or zero-unreserved).

### 3.3 Candidate-eligible population (dry-run reduction surface, live-grounded)

Applying the Task G blocking rules from the brief to the live ship snapshot:
0<=candidate<=unreserved<=observed, candidate=0 for protected/unknown/malformed/
over-reserved/applicable-unsupported-demand, actionable=false always:

- 356 stacks total; 251 are zero-quantity (never actionable), 0 negative.
- 259 blocked (protected or unknown-design).
- 76 blocked (unreserved == 0: fully reserved or qty 0).
- 4 blocked (applicable unsupported demand: the §3.2 stacks above).
- 0 blocked (design-scoped blocking conditions: `UNKNOWN_DESIGN`, `MALFORMED_QUANTITY`,
  `UNREASONABLE_QUANTITY`, `DEMAND_EXCEEDS_SUPPLY` — none fire on the live ship).
- **17 candidate-eligible stacks** (all with candidate <= unreserved). Identified:
  designs 2 Gas (25000), 3 Mineral (30310), 64 Interceptor Lv8 (17), 81 Small Mineral
  Crate (2, CONSUMABLE), 139 Proximity Mine (1), 167 Sandbag (2), 189 Mechanics Apron
  (2), 206 Medical Droid III (5), 210 Defender Droid II (14), 313 Flintlock Phaser (1),
  419 E-Clamp (1), 457 Med Spoon (1), 639 Small Axe (2), 733 Sandbug (5), 78 Combat
  Helmet (1), 122 Personal Phaser (1), 90 Half Day Immunity (1, STRATEGIC_RESERVE_CANDIDATE).

Arithmetic invariant re-verified live: total observed 56,249; total reserved 33;
total unreserved 56,216. Every design satisfies reserved <= observed. The planner's
allocation decrements a per-design pool starting at observed, so `total_reserved <=
observed` holds structurally in Task F output; **over-reserved ledgers can only be
hand-built for adversarial tests** (the planner can never emit one) — Task G defensive
tests must construct them manually.

### 3.4 Overlap/uncertainty conditions on the live ship (Task G must not let them reduce blocking)

- 3 `EQUIPMENT_SHARED_STACK` conditions (designs 79, 447, 641): per-stack reference
  counts [3, 3, 2]; whole-stack claimed once.
- 3 `MODULE_MULTI_ATTACH` conditions (designs 394, 400, 401): per-stack [6, 2, 2].
- 0 equipped∩room overlap (`BOTH_EQUIPPED_AND_ROOM_ATTACHED` live sets disjoint —
  rule exists but fires zero times).
- 4 global `SOURCE_UNSUPPORTED` conditions (TRAINING/AMMUNITION always; STRATEGIC and
  CRAFT in this default-config run).
- **Uncertainty-monotonicity evidence**: every one of the 6 condition-carrying designs
  (79, 394, 400, 401, 447, 641) is either protected or zero-unreserved on the live
  ship; the "eligible stack whose design carries an unresolved-demand condition"
  population is EMPTY. Task G's strict reading (condition-carrying designs blocked)
  changes nothing live but must be tested synthetically.

### 3.5 Evidence-tier propagation facts (for the planner lane)

- `ReservationLedger.evidence_for(design)` = weakest(feed rows CAPTURED_NOT_VERIFIED,
  entry evidences); design with no entries -> UNKNOWN (fail closed).
- Entry evidence live: all 15 entries CAPTURED_NOT_VERIFIED; entry set exactly
  {CAPTURED_NOT_VERIFIED} re-verified.
- Evidence is minimum-across-inputs and never promoted; planning intent (strategic
  config, craft objectives) is INFERRED, so mixed designs stay INFERRED.
- Task G must preserve: candidate results carry the weakest-input evidence of their
  inputs (observed feed CAPTURED_NOT_VERIFIED + reservation ledger + classification);
  no result may claim VERIFIED while ListItemsOfAShip is uncaptured (§1).

## 4. Repository reconciliation (repo truth vs Task F synthesis §23/§24)

Every Task F synthesis claim this lane re-verified against the working tree and real
captures on 2026-09-26:

| Synthesis claim | This lane's re-verification |
|---|---|
| 592 tests, OK (skipped=1) | `make test`: **Ran 592 tests, OK (skipped=1)** — reproduced |
| Targeted demand + plan_cli: 107 OK | 77 + 30 = 107 — reproduced |
| All six inventory suites: 245 OK | 36+23+53+26+77+30 = 245 — reproduced |
| Ruff clean on changed/new files | `uv run ruff check sdk/inventory/ tests/test_inventory*.py sdk/commands.py sdk/client.py sdk/security.py sdk/tui.py`: **All checks passed!** (repo-wide lint carries 11 pre-existing errors in non-inventory files: crew_leveling, ship_layout, game_formulas tests, battle scripts — committed in 632fba6/30c73f0/8a55c34, outside the swarm's files and outside this lane's scope) |
| `git diff --check` clean | reproduced — clean |
| Task G not implemented anywhere | reproduced: no `available_surplus` computation, no reduction/disposition logic in production; `available_surplus` appears only as the always-None Task D field, docstrings and prohibition tests |
| `sdk.client` never imports `sdk.inventory` | reproduced (source-scan; only comment refs) |
| Only transport import in sdk/inventory: `_extract_collection` (sdk/client.py:50) | reproduced (checksums.py additionally imports stdlib `hashlib` — expected, not a transport import) |
| `inventory apply` byte-identical refusal | handler at commands.py:460-476 is docstring + constant return; no config/env reads — confirmed |
| Live ship plan: 15 entries (11 EQUIPMENT + 4 MODULE), 3 shared-stack, 3 multi-attach | **reproduced exactly** by re-decoding the 2026-08-08 GetShipByUserId + running the real planner |
| Reserved <= observed on every design; all referenced stacks protected; entry evidence = {CAPTURED_NOT_VERIFIED} | **reproduced exactly** |
| 2,557/2,557 designs cataloged from live feed; 90 nonzero TrainingDesignIds; 899 recipes | reproduced: 2,557 designs, 90 training links, 899 CRAFTING_INTERMEDIATE, 3 documented `Ingredients="0"` null markers |
| 16 references over 11 distinct stacks (E1); 8 rooms carrying items (E2) | reproduced exactly |
| Design feed evidence: dynamic feed base64-gzip encoded in capture | noted: ListAllDynamicDesigns bodies are base64(gzip(xml)) in the MITM record format — decoders must handle this |
| ListItemsOfAShip: 0 captures in 49,774 records | **superseded by this lane's full-corpus result: 0 in 50,755 across all 6 capture files** (same verdict, broader scan) |
| "Item nodes carry only 6 attributes ... in observed responses" (reconcile report) | **needs scoping**: true for the primary account (all its tags, all captures); false corpus-wide (370 tags on the second account carry BonusEnhancementType/Value). CAPTURED_ITEM_ATTRIBUTES stays correct for the account the planner runs on; the docstring's "anything else unverified and must not be consumed" direction still holds |
| DEFAULT_INVENTORY_CONFIG zero consumers | reproduced (only docstring/config surface) |
| S-LIVE-1 live-HTTPS leak fixed in test_battle_flow | out of this lane's verification path; the suite passing with sockets available is consistent |
| `findConsumableDesignId` rests on uncaptured `listAllDesigns4` | out of scope for Task G; noted |

Reconciliation verdict: **repo truth and the Task F synthesis agree on every
load-bearing claim this lane could re-verify.** Two doc-level corrections are needed
(§5).

## 5. Evidence corrections (documentation only, applied by this lane)

1. `INVENTORY_ENDPOINT_ANALYSIS.md` §Capture Coverage: the ListItemsOfAShip row says
   "0 — NEVER captured" (correct) but the note scope was MITM-only; updated to state
   the full-corpus scan result (6 capture files, 50,755 records, 0 hits) and that the
   corpus's only ItemService records are the 12 mutation-endpoint captures, so the
   inventory-feed evidence cap (CAPTURED_NOT_VERIFIED) must be preserved until a
   ListItemsOfAShip capture exists. Applied 2026-09-26 by this lane.
2. `_types.py` docstring "six capture-observed attributes ... anything else is
   unverified and must not be consumed" — direction unchanged, but the "4,168 Item
   tags" reference should be understood as raw byte occurrences in the primary file
   (4,164 decodable response tags; delta = the 2 binary-corrupted records). No code
   change: extra attributes are already preserved in `raw` and never consumed. Not
   edited (production file; the planner/verifier lanes own Task G code changes); this
   report records the correction for the synthesizer.

## 6. Machine-readable facts (for the verifier gate)

- capture_corpus_files: 6
- capture_corpus_records: 50755
- listitemsofaship_captures: 0
- inventory_read_endpoint_captures: 0
- itemservice_records_in_corpus: 12 (MITM) + 2 (login) + 2 FixUnsynchronizedItems — all mutation endpoints
- item_tags_total_decodable: 9942
- item_tags_six_attrs: 9572
- item_tags_with_bonus_enhancement: 370 (second account only; primary account: 0)
- live_ship_stacks: 356; zero_qty: 251; negative: 0
- live_ledger_entries: 15 (EQUIPMENT 11, MODULE 4)
- live_shared_stack_conditions: 3; multi_attach: 3; equipped_room_overlap: 0
- live_candidate_eligible: 17 (designs 2,3,64,81,90,122,139,167,189,206,210,313,419,457,639,733,78)
- live_blocked_training_ammo: 4 (items 181673752, 182315577, 184469427, 188673662)
- live_total_observed: 56249; reserved: 33; unreserved: 56216
- evidence_cap: inventory feed stays CAPTURED_NOT_VERIFIED (ListItemsOfAShip uncaptured)
- tests: make test -> 592 OK (skipped=1); inventory suites 245; targeted demand+plan_cli 107
- ruff: clean on inventory/changed surface; 11 pre-existing errors elsewhere (files committed in 632fba6/30c73f0/8a55c34)
- git diff --check: clean
- task_g_code_before_lane: absent (this lane added none)
- files_changed_by_this_lane: INVENTORY_ENDPOINT_ANALYSIS.md (evidence note), this report

## 7. Notes for the planner lane (t_14d15df9) and verifier (t_3e8645cd)

1. **Evidence cap is binding**: no Task G result may carry evidence above
   CAPTURED_NOT_VERIFIED for feed-derived facts; VERIFIED requires a ListItemsOfAShip
   (or equivalent read-path) capture that does not exist (§1). Do not create live
   traffic to obtain one.
2. **Over-reserved ledgers are synthetic-only**: the Task F planner structurally
   cannot emit reserved > observed (allocation decrements from observed). Build
   hand-crafted adversarial ledgers for the candidate=0 over-reserved tests.
3. **Uncertainty monotonicity**: live data provides no natural case of an
   eligible-but-condition-carrying design; test it synthetically (e.g. a strategic
   reservation on a design with unreserved stock plus a MALFORMED_QUANTITY condition).
4. **The 17-stack eligible population** is the real dry-run surface for live-shaped
   tests; its largest members are Gas 25000 and Mineral 30310 — quantity magnitudes
   vary wildly (0 to 30,310), so any candidate cap logic must not assume smallness.
5. **Design 90 (Half Day Immunity, STRATEGIC_RESERVE_CANDIDATE) is eligible with no
   strategic config**: Task G must give it candidate=0 or state a KEEP/RESERVE hint —
   the role means the owner may want it held; role presence without config-backed
   reservation is exactly the "applicable unresolved demand" family the brief says
   blocks (candidate=0) — flag for the planner lane to decide, then test both ways.
6. **Zero-quantity stacks (251/356 live)**: never actionable; Task G should emit
   candidate=0 + a reason, not skip them.
7. **BonusEnhancement***: only on the second account's tags; primary-account runs never
   see them. Catalog preserves extras in `raw` — Task G must not consume them (would
   be consuming an attribute with 370 total observations, none on this account).
8. **Malformed-input tests should use the documented live null markers**
   (`Ingredients="0"`, `TrainingDesignId="0"` parse to absent, not malformed) —
   Task G's malformed handling must not regress the D1-D4 fixes locked in
   test_inventory_safety.py.
9. **capture-decoding note**: ListAllDynamicDesigns bodies in the MITM file are
   base64(gzip(xml)); the catalog/fixtures in tests use plain dicts, so no change
   needed, but any verifier replaying capture data must gunzip-decode.

## 8. Handoff

This lane's lane-definition work is complete:

- ListItemsOfAShip capture search: done, verdict NEGATIVE (0 in 50,755).
- Inventory-feed evidence cap: confirmed preserved and binding.
- Demand-coverage matrix: complete (§3), grounded in code + live capture.
- Repository reconciliation vs Task F synthesis: complete (§4), all load-bearing
  claims reproduced; two doc-level corrections identified (§5), one applied.
- Task G prohibition surface: unchanged; this lane added no Task G code, no network
  I/O, no mutations, no live traffic.
- Validation: full suite 592 OK (skipped=1), targeted 107 OK, ruff clean on the
  lane's touched surface, git diff --check clean.

The planner/protocol/safety/architecture lanes own all Task G code changes. The
verifier can independently reproduce every number in §6 from the capture files and
the working tree.
