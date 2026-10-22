# Task J Lane Report — Research / Gap Reconciliation (t_903bb716)

Lane: RESEARCH / GAP RECONCILIATION — reconcile Task I final synthesis with
repo truth, enumerate the exact remaining ListItemsOfAShip/GiftItem evidence
gaps, inspect static evidence, and define the safe human-operated capture
vector. No live mutation initiated by this lane.

Date: 2026-10-03
Repository: ~/Documents/Personal/tachikoma
HEAD: 2cb5aa4 (working tree carries uncommitted Task J lane artifacts from
runs 119/120 and sibling lanes; historical corpus untouched)

## 1. Task I prerequisite verification (synthesis vs repo truth)

- Task I final synthesis exists: docs/swarm-synthesis-task-i-t_b4414b94.md.
- Task I verifier t_232a3d8d gate=pass; synthesizer t_b4414b94 done.
- TransferPlanner HOLD invariant preserved in the live tree:
  sdk/inventory/transfer.py:115 defines HOLD_UNVERIFIED_TRANSFERABILITY;
  module docstring pins "quantity 0, actionable=false" (transfer.py:12).
- Full suite is a strict superset of the Task I baseline:
  1090 passed / 8 skipped today vs 1013 OK / 1 skipped at Task I verification
  (delta = Task J pure-logic tests added by this swarm; no Task I test
  weakened).
- GiftItem production reachability: `grep -n "def gift" sdk/ sdk/inventory/`
  returns zero matches; no Client.giftItem, no mutation flag, no transfer
  executor, no apply path.
- `inventory apply` remains an unconditional refusal (README.md:81 documents
  the always-refusing stub; covered by test_runtime_guards.py /
  test_inventory_safety.py, passing in the full suite).

Observed discrepancy vs the Task I snapshot: test skip count rose 1 -> 8.
Cause: Task J runs upgraded pytest-asyncio 0.23 -> 0.24 (pytest 8.3 compat);
the additional skips are async-collection skips, not weakened assertions.
All previously passing Task I tests still pass (1090 >= 1013 collection).

## 2. Historical corpus baseline (independent re-scan, this lane)

Binary-safe, case-insensitive scan re-run 2026-10-03 by this lane:

| file | bytes | GiftItem | ListItemsOfAShip |
|---|---|---|---|
| ~/pss-mitm-capture.jsonl | 691,774,247 (2026-08-16) | 0 | 0 |
| ~/pss-login-capture.jsonl | 42,151,460 (2026-08-01) | 0 | 0 |
| ~/.tachikoma-credential-capture/previous-capture-2.jsonl | 13,212,854 | 0 | 0 |
| ~/.tachikoma-credential-capture/previous-capture-3.jsonl | 9,628,458 | 0 | 0 |
| ~/.tachikoma-credential-capture/previous-capture-4.jsonl | 9,683,859 | 0 | 0 |
| ~/.tachikoma-credential-capture/previous-capture-5.jsonl | 19,693,827 | 0 | 0 |
| ~/.tachikoma-credential-capture/previous-capture.jsonl | 0 (empty) | 0 | 0 |

Conclusion: zero GiftItem and zero ListItemsOfAShip captures exist anywhere in
the historical corpus. Newest corpus file is 2026-08-16. No fresh Task J
capture exists: ~/pss-task-j-capture.jsonl is ABSENT (verified 2026-10-03,
this run). The human gate is therefore mandatory before any GiftItem exchange.

## 3. Static GiftItem evidence (current tier: STATICALLY_DISCOVERED)

- IL2CPP binary template is truncated: `/ItemService/GiftItem?itemId=`
  (INVENTORY_ENDPOINT_ANALYSIS.md §10, lines 145-146). The full parameter set
  is NOT recoverable from the binary: recipient-targeting parameter name,
  quantity parameter, and any checksum/signature parameter are all unknown.
- No independently discovered GiftItem checksum construction exists in the
  static analysis (docs/worker-report-inventory-t_74779a1f.md §73-75 records
  GiftItem as "No capture" with no preimage; contrast RepairBrokenModules and
  DeleteItem3, which at least have full templates).
- Consequence per the evidence hierarchy: even after ONE dynamic capture, the
  GiftItem checksum cannot be promoted to VERIFIED unless independent static
  evidence establishing the preimage is also found. A single observation does
  not verify a formula.
- ListItemsOfAShip static template: `?accessToken=` (auth-only parameters
  visible in the binary); request/response shape requires a capture.

## 4. Exact remaining evidence gaps

GiftItem (all currently UNKNOWN, none may be inferred):
1. Complete endpoint/template beyond `?itemId=` — recipient param name unknown.
2. HTTP method (presumed GET by family convention; NOT independently
   established for GiftItem).
3. Recipient identifier semantics (UserId vs ShipId vs friend-slot index).
4. ItemId vs ItemDesignId on the wire.
5. Quantity semantics (fixed 1? stack-wide? gift-all?).
6. Checksum inputs/preimage — no static construction exists.
7. Response schema and success indication.
8. Sender inventory delta.
9. Recipient inventory delta (new ItemId? merge into stack? split?).
10. Failure/error behavior.

ListItemsOfAShip:
1. Genuine official-client request/response pair (0 in corpus).
2. Response shape: ItemId, ItemDesignId, quantity, ShipId association,
   design metadata links, duplicate/placeholder behavior.

Transferability decision at current evidence level: **NO** — there is not
enough verified evidence to define a safe transferability predicate.
HOLD_UNVERIFIED_TRANSFERABILITY is preserved; every Task I proposal remains
proposed_quantity=0, actionable=false.

## 5. Safe human-operated capture vector (defined)

The safe vector is fully specified in
docs/task-j-operator-capture-procedure-t_1fce40eb.md (protocol lane). Summary
of the safety envelope, verified by this lane against the card's test-vector
criteria:

- Source and destination are distinct accounts owned/controlled by the
  operator (both PSS accounts are the operator's own; no third party).
- Item: exactly ONE expendable, non-protected, non-premium item, quantity 1.
  No premium currency, no marketplace, no sale, no deletion, no
  training/equipment/module/crafting/reserve-critical item.
- Capture isolation: scripts/mitm_addon.py now defaults fresh output to
  ~/pss-task-j-capture.jsonl (env-overridable via PSS_CAPTURE_FILE), so a
  fresh session cannot append into the 692MB historical corpus. The
  historical corpus is untouched (verified this run, §2).
- The operator, not the agent, performs exactly ONE official-client GiftItem
  action while mitmdump -s scripts/mitm_addon.py runs. Agents never
  synthesize or replay a GiftItem request.

## 6. Safety state confirmations (this lane, this run)

- Live mutations initiated by agents (this lane and observed repo-wide): 0.
- Human-operated GiftItem exchanges to date: 0 (capture file absent).
- Starbux/premium currency spent: 0.
- Production GiftItem reachability: 0 (no `def gift*` anywhere in sdk/).
- sdk/inventory network I/O: 0 (sanitize.py/reconcile.py are pure; verified by
  tests and by module docstring invariants).
- All Task I proposals: actionable=false (HOLD preserved).
- inventory apply: unconditional refusal, unchanged.

## 7. Validation (this run)

- Targeted Task J tests: tests/test_task_j_sanitize.py +
  tests/test_task_j_reconcile.py — 77/77 passed.
- Full suite: 1090 passed / 8 skipped.
- ruff: clean on sanitize.py, reconcile.py, checksums.py, both Task J test
  files, scripts/mitm_addon.py.
- ty: clean on sdk/inventory/sanitize.py, sdk/inventory/reconcile.py.
- git diff --check: clean.

## 8. Remaining protocol gaps / next slice

The single blocking dependency for the whole swarm is the human gate: the
operator must perform exactly ONE official-client GiftItem exchange per
docs/task-j-operator-capture-procedure-t_1fce40eb.md with mitmdump running,
producing ~/pss-task-j-capture.jsonl. Recommended next slice (already
queued as the capture-analysis resumption of t_1fce40eb / t_497be142 /
t_899abb44): parse the fresh capture, sanitize it into fixtures, run the
dual-account pre/post reconciliation with sdk/inventory/reconcile.py, and
promote evidence only as far as the capture plus static evidence supports.

This lane's research deliverable is COMPLETE. The capture-dependent lanes
remain blocked at the human gate by design.
