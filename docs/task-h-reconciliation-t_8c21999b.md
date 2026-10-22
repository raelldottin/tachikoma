# Task H reconciliation — Task G / multi-account / account-identity infrastructure, snapshot evidence and freshness requirements

Worker lane: `t_8c21999b` (reconcile + requirements). Swarm root: `t_68ba2251`.
Method: every claim below was verified against the live working tree (branch `main`,
HEAD `2cb5aa4`, dirty tree as designed) on 2026-09-28 by direct file inspection
(`rg`/`read`), not taken from worker reports. Where a claim rests on an earlier
synthesis doc, the doc was re-checked against the current code.

## 1. Task G reconciliation against the working tree

Verified current state of the D/F/G surface (all imports resolve, exports complete):

- `sdk/inventory/` modules: `_types.py`, `catalog.py`, `checksums.py`, `classify.py`,
  `demand.py`, `designs.py`, `policy.py`, `reduction.py`, `__init__.py` (152 lines,
  full export list including all 10 `BLOCKED_*` codes, `STATE_HOLD`,
  `STATE_CANDIDATE`, `ReductionPlanner/Plan/Result`).
- The Task G synthesis (`docs/swarm-synthesis-task-g-t_46223d9d.md`) §16-§19 claims
  reconcile exactly with the tree: no account/userId dimension anywhere in
  `sdk/inventory/` (verified by scan `rg -in "account" sdk/inventory/` and
  `rg -n "user_id|userId" sdk/inventory/`: 11 case-insensitive hits, ALL in
  docstrings/comments — `__init__.py:14` (package docstring), `reduction.py:86,219`
  (module/class docstrings), `_types.py:142,192,213` (ProtectionContext/ItemDesign/Item
  docstrings), `catalog.py:8` (ItemId/ItemDesignId disambiguation), `demand.py:93,95,442`
  (module + class docstrings); `user_id|userId` scan returns ZERO hits); no
  portfolio/transfer/executor module exists; `Disposition` members are
  KEEP/USE/CRAFT/ALLOCATE only.
- Live Task G surface (per synthesis §19, re-verified by 4 independent parties):
  356 stacks / 259 protected-unknown / 76 zero-qty / 4 unsupported-blocked /
  16 CANDIDATE + 1 strategic HOLD / evidence {INFERRED, STATICALLY_DISCOVERED,
  UNKNOWN} / 0 actionable. `inventory apply` handler is a docstring + constant
  REFUSED return reading no state (`sdk/commands.py:523`).
- `sdk/inventory/reduction.py` (455 lines): module docstring documents per-design
  unreserved-pool candidate allocation in ascending ItemId order, the blocking
  precedence (malformed > protected/unknown > over-reserved > design-condition >
  stack-condition > zero-qty > zero-unreserved > unsupported-role >
  strategic-role > allocation-exhausted), weakest-input evidence propagation, and
  the actionable=False-by-construction contract.
- Suite at Task G close: 648 tests, 1 pre-existing skip; canonical gate is
  `make test` (unittest) — bare `pytest` is broken by a pre-existing
  pytest-asyncio 0.23.0 vs pytest 8.3.0 incompatibility (unchanged by this lane).

One nuance found vs the synthesis doc: the Task G synthesis §16 says
"no multi-account planning ... no account identity" — confirmed. But the older
research doc `docs/worker-report-inventory-t_fd97b1ee.md` §8/§11 proposed the
AccountPortfolio/`inventory accounts`/`inventory deficits`/`inventory transfers`
surface that Task H now implements a SUBSET of. Task H's brief supersedes the
older proposal in three specific ways (these are Task H scope decisions, not
repo drift):

1. `inventory transfers` / GiftItem / SellItem3 / Purchase4 / donor-recipient
   matching remain PROHIBITED in Task H (older doc proposed them as Phase 6).
2. Portfolio reads accounts from persisted snapshots on disk, NOT live
   multi-login (older doc said the same; re-confirmed: `run.py` constructs ONE
   `Client(device)` per run, `Device.DB = "./.device"` single file, no
   per-account directory or registry exists).
3. Evidence/freshness requirements below are NEW requirements Task H adds on
   top of the older docs; the older docs had NO freshness/staleness concept.

## 2. Account-identity infrastructure reconciliation

What exists today (verified in code):

- One account per working directory. `sdk/device.py`: `Device.DB = "./.device"`
  single fixed path; fields are pipe-delimited
  `name|key|refreshToken|languageKey|accessToken|userId` (positions 5/6 are
  optional pre-provisioned token/userId added by the auth slices). There is no
  account registry, no per-account directory, no multi-account loader.
- `sdk/client.py`: `Client.user = User(userId, name, lastHeartBeat,
  refreshToken)` built at login from `UserLogin/@UserId` or `User/@Id`
  (`client.py:233`); `self.info` holds the raw User dict. `User.id` is the only
  stable per-account identifier the SDK surfaces.
- Auth tokens (`refreshToken`, `accessToken`, device `key`) are SECRETS:
  `.gitignore` covers `.device` and `config.secrets`. `sdk/redaction.py`
  provides `redact_secrets` / `redact_dict` for scrubbing.
- Name (`User.name`) is non-unique across the game (display name) — NOT a safe
   identity key; `User.id` (integer userId) IS the stable key.

Therefore, for AccountSnapshot account identity, the ONLY safe, non-secret,
stable identity triple is:

- `user_id: int` — canonical account key (from UserLogin/@UserId or User/@Id).
- `display_name: str` — informational only, never a join key, never a secret.
- Optional `ship_id: int` — items carry `@ShipId`; useful provenance but a ship
  is not the account. ItemId is account-scoped (catalog.py docstring); ItemIds
  from two different accounts MUST never be assumed comparable. ItemDesignId is
  global and IS the cross-account join key.

MUST NOT be persisted in snapshots (secrets / credential material):
`refreshToken`, `accessToken`, device `key`, `email`, `password`,
`authentication_string`, checksum-key material (`checksum_key`,
`savy_checksum` values are constants in the repo, not secrets, but snapshot
must not carry them — they are not account data).

## 3. Snapshot evidence requirements (what a v1 AccountSnapshot must carry)

Derived from the D/F/G domain contracts (re-verified in code):

Required blocks, per account, in schema v1:

1. `version: 1` — constant for this slice; any other version value MUST be
   rejected fail-closed (unknown version = refuse the whole snapshot).
2. `account` identity block: `user_id` (int, required), `display_name` (str,
   optional), `ship_id` (int, optional). Validation: negative/zero user_id
   invalid; unknown keys in the account block = malformed (fail closed).
3. `inventory` block (Task D/C inputs): per stack — ItemId, ShipId,
   ItemDesignId, Quantity, IsNew, SkinKey + evidence tier for the feed
   (CAPTURED_NOT_VERIFIED; the ListItemsOfAShip evidence cap is PRESERVED:
   0 captures in 50,755 records across 6 files, re-scanned by 4 parties —
   snapshot serialization must NOT promote these tiers to VERIFIED).
4. `classification` block (Task D outputs): per stack — bucket, protected
   flag, protection_reasons, roles, evidence. Or re-derivable? DECISION:
   PERSIST the Task D classification results, not just inputs, because Task H
   portfolio aggregation needs protection state and the brief requires
   "protection ... reason" in the snapshot. Re-deriving at portfolio time from
   persisted inputs would re-run the classifier over snapshot data, which is
   pure and deterministic — but the brief says the snapshot contains
   "inventory, protection, demand/reservation, reduction-candidate, evidence,
   reason and unresolved-condition state", i.e. persist the computed states.
5. `demand`/`reservation` block (Task F outputs): per design — reservation
   entries (source, requested_quantity, reserved_quantity, evidence) plus
   requested/reserved/unmet where supported. Persist `UnresolvedDemand`
   conditions (the ledger's fail-closed conditions list).
6. `reduction` block (Task G outputs): per stack — state (HOLD/CANDIDATE),
   candidate quantity, blocked reason codes, weakest-input evidence,
   disposition hint.
7. `evidence_summary` block: weakest-input evidence per persisted surface, plus
   the per-surface source evidence tiers, so the portfolio's "weakest-input
   evidence" is computable as min across inputs (gate Q4).
8. `unresolved_conditions` block: consolidated per-account fail-closed
   condition list (dedup design-scoped conditions; preserve reason text).
9. `captured_at` timestamp (freshness convention in §4); schema `version` is
   item 1 above.
10. `reason` state: the brief's "reason" = the per-row reason codes (Task G
    BLOCKED_* codes, Task D protection_reasons, Task F condition codes) —
    persisted alongside their rows; a snapshot must never persist a bare
    "reason: ok" with no code.

Determinism requirement: serialization MUST be deterministic — canonical key
order, sorted rows (ascending ItemId for stacks; ascending design id, then
source precedence, then request order for reservations), so identical state
serializes byte-identically and the determinism tests can pin exact bytes.

### 3a. What "evidence" means per surface (concrete floors)

- Inventory feed rows: CAPTURED_NOT_VERIFIED (hard cap — ListItemsOfAShip
  never captured; INVENTORY_ENDPOINT_ANALYSIS.md remains authoritative).
- Task D classification of a stack: min(feed row evidence, design evidence).
- Task F reservation entry: min across its required inputs; strategic/craft
  config-driven entries are INFERRED at best (never VERIFIED).
- Task G candidate rows: min(feed rows, ledger evidence, classification
  evidence); no row reaches VERIFIED under the cap.
- Snapshot-level `evidence`: weakest across all persisted surfaces (a
  snapshot is exactly as strong as its weakest persisted input). Since every
  snapshot by construction contains CAPTURED_NOT_VERIFIED feed rows, a v1
  snapshot's ceiling is CAPTURED_NOT_VERIFIED; that ceiling is a property to
  assert, not something to encode as a scalar.
- PORTFOLIO-level evidence for an aggregated design row: min across the
  per-account snapshot evidence values that contributed to that row (weakest
  input across accounts), never max, never promoted.

### 3b. Contradiction validation (fail-closed malformed rules)

A snapshot is malformed (reject the WHOLE snapshot) when:

- `version` missing or != 1.
- Any required key missing (`version`, `account`, `captured_at`, `inventory`,
  `classification`, `demand`, `reduction`, `evidence_summary`,
  `unresolved_conditions`).
- Account block has unknown keys.
- Duplicate ItemId rows within one account's inventory block, or duplicate
  ItemId rows in the classification/reduction blocks (AD-4: never silently
  double-counted — catalog raises DuplicateItemIdError; snapshot
  deserialization must apply the SAME rule, not normalize).
- A classification or reduction row referencing an ItemId not present in
  the inventory block (cross-block consistency — ItemId-scoped rows must
  match a persisted stack).
- Demand entries referencing an item_design_id NOT present in the
  inventory block are LEGITIMATE (Task F supports demand for a design
  with zero held stock — that is exactly the deficit case the portfolio
  exists to expose, via DEMAND_EXCEEDS_SUPPLY); they are NOT
  contradictions. But a demand entry with a NEGATIVE or non-int quantity,
  or an unknown `source` enum value, IS malformed.
- `captured_at` missing or unparseable as ISO-8601 UTC Z-suffix seconds
  (§4 convention) = malformed at DESERIALIZATION time. "Future" timestamps
  are also malformed, but future-ness is clock-relative — it is checked
  where `now` is injected (portfolio build / render), not at file load
  (see §4 implementation note; the purity rule forbids clock reads in
  sdk/inventory).
- Any quantity negative (Task F/G quantity rules: non-negative ints only).
- Evidence tier strings not in the Evidence enum.
- Type violations: non-int ids, non-bool flags, non-list blocks.

Fail-closed behavior for a batch: if ANY snapshot in the portfolio input is
unknown-version, malformed, corrupt (e.g. truncated JSON / wrong types), or
self-contradictory, the ENTIRE portfolio aggregation MUST fail closed (raise),
never silently drop the bad snapshot and aggregate the rest. Rationale: the
portfolio's job is to give a cross-account view for safety-relevant decisions
(even if read-only); silently dropping an account is the under-claim direction
for a protection-oriented portfolio. Distinguish a dedicated exception type so
callers can report which snapshot failed and why.

### 3c. Evidence floors (per the brief: "evidence floors" adversarial tests)

- Floor for accepting a snapshot at all: schema-valid (§3b rules). Evidence
  tiers themselves are free to be as weak as UNKNOWN/INFERRED — weak evidence
  is NOT a reason to reject a snapshot; it must propagate to the portfolio
  row's weakest-input evidence. E.g. an account whose design feed failed and
  whose classifications are all UNKNOWN still participates, with its rows
  carrying UNKNOWN evidence (fail-closed direction: include-with-weak-evidence
  beats drop).
- Floor for a portfolio aggregated row: min across contributing accounts.
- Floor for `inventory accounts` / `inventory deficits` rendering: report the
  weakest-input evidence next to every number, so no rendered number is
  stronger than its weakest input.
- Floor for candidate: candidate quantities are Task-G semantics only — a
  candidate does NOT mean transferable (brief). Render must keep that wording.

## 4. Freshness requirements (staleness axis)

Problem: portfolio aggregates snapshots captured at different times; the
brief mandates "freshness requirements". Where staleness enters Task H:

- Each snapshot carries `captured_at` (server-time-derived wall clock of when
  the inventory feed was fetched, ISO-8601, seconds precision, UTC or with
  explicit offset — pin ONE convention in v1: UTC Z-suffix, seconds).
- The CLI/UX read path (renderer/`inventory accounts` / `inventory deficits`)
  reads the LOCAL clock at render time to compute staleness. No network I/O
  needed for staleness computation (requirement: no network I/O in
  sdk/inventory — staleness must be computable from snapshot + explicit
  `now` parameter; the CLI passes the real clock, tests pass pinned clocks).
- STALENESS IS NOT A MALFORMATION. A stale snapshot is still schema-valid.
  The two axes are independent: version/malformation govern ACCEPT/reject;
  staleness governs TRUST annotation and rendering.
- Staleness rule (deterministic, no magic thresholds invented silently): v1
  defines a single constant STALE_AFTER (rendering annotation threshold).
  DECISION for v1: stale is a RENDERING annotation, not a rejection — a stale
  snapshot still aggregates (fail-closed by inclusion, see §3c) but every
  rendered row/account summary that depends on a stale snapshot must carry a
  visible STALE marker (and the account summary line for that account).
  Portfolio-level "fresh" requires ALL contributing snapshots fresh.
  DEFAULT STALE_AFTER: 24h. Justification: Task F demand sources are
  current-state claims (equipment/module references), training is
  one-at-a-time runtime flow, and RebuildAmmo3 restocks server-side — the
  game's inventory-turnover granularity in this repo's captures is at least
  day-scale; anything finer is invented precision, and 24h matches the
  game's daily-reward cadence (client.py DailyReward fields). NOTE: because
  stale is an annotation, threshold choice never affects whether a snapshot
  aggregates — the 24h default only governs how the marker reads; it is a
  named, exported module constant with a docstring, overridable per-call in
  the pure API (parameter, not config), never silently changed.
- Freshness + evidence interplay: staleness annotation NEVER changes a row's
  evidence tier (evidence is about protocol verification, not age). A stale
  snapshot contributes the same evidence as a fresh one; staleness is a
  separate rendered column/marker.
- `inventory accounts`/`inventory deficits` must never block on staleness:
  they render with markers. Fail-closed applies to structure (§3b), not age.
- A snapshot with `captured_at` in the future is malformed (§3b), which
  catches clock-skew nonsense separately from staleness. IMPLEMENTATION
  NOTE: "future" is clock-relative, and the purity rule says no clock reads
  inside sdk/inventory — so future-timestamp validation is NOT a
  deserialization-time check; it runs where a `now` value is already
  injected (portfolio build / render call), using the same `now` parameter
  as staleness. A snapshot file loaded without a `now` is validated for
  structure only; the future check fires the moment `now` is supplied.

### 4a. Freshness: what the pure core must expose

- `SnapshotFreshness(captured_at, now, stale_after) -> {fresh|stale, age_seconds}`
  — pure function/record; deterministic for identical inputs.
- Portfolio record carries per-account `freshness` and a derived
  `all_fresh` boolean; renderer consumes. No clock reads inside
  sdk/inventory (purity/no-network rule; clock injection via parameter).

## 5. Prohibitions that carry into Task H (verified unchanged)

- No donor/recipient matching, no transfers, no GiftItem, no marketplace, no
  SellItem3/Purchase4, no destination selection, no mutation executor or
  reconciler, no endpoint enablement (`docs/swarm-synthesis-task-g-t_46223d9d.md`
  §16; brief).
- `inventory apply` unconditionally refuses; all Task-H output actionable=false.
- No network I/O in `sdk/inventory` (one-way boundary: inventory imports
  transport helpers from client; client never imports inventory).
- ListItemsOfAShip evidence cap preserved: no surface may label feed-derived
  claims VERIFIED (catalog.py from_dicts docstring already pins this).
- Rendering must not imply transferability: "candidate does not mean
  transferable" wording must appear in accounts/deficits output.
- Renderer extraction precondition: per the brief, BEFORE any CLI expansion,
  mechanically extract the inventory-plan renderer from `sdk/commands.py`
  (hotspot flag from Task G synthesis §17) into a pure module with
  output-parity tests pinning the exact current bytes of `inventory plan`
  output on fixture catalogs. This lane verified the hotspot is real:
  `sdk/commands.py` is 565 lines and the `_render_inventory_plan` +
  `_render` paths + `inventory` verb handling are the bulk of the inventory
  surface; three Task G lanes collided in this file. The extraction is the
  architect lane's (t_6f3684d5) job — this lane's requirement is only that
  parity tests exist BEFORE any accounts/deficits rendering lands.
- Prohibition on scope creep from the older research docs: where
  `docs/worker-report-inventory-t_fd97b1ee.md` §11 proposes verbs or modules
  beyond the Task H brief (transfer.py, executor.py, `inventory transfers`),
  the Task H brief wins; those remain unimplemented.

## 6. Requirements traceability for the implementation lane

Mapping the brief's phrases to concrete requirements above:

| Brief phrase | Requirement |
|---|---|
| "non-secret account identity" | §2 identity triple; §2 secrets blacklist |
| "deterministic schema-v1" | §3 item 1, §3 determinism paragraph |
| "reject unknown versions, malformed/corrupt/contradictory fail-closed" | §3b |
| "evidence, reason and unresolved-condition state" | §3 items 4-10 |
| "weakest-input evidence" | §3a |
| "duplicates" (adversarial) | §3b duplicate ItemId rows |
| "staleness" (adversarial) | §4 |
| "evidence floors" (adversarial) | §3c |
| "secret leakage" (adversarial) | §2 blacklist + leakage scan of serialized bytes |
| "fail-closed aggregation" | §3b batch rule |
| "no transfer planning" | §5 |
| "mutation isolation / apply refusal" | §5 |
| "determinism" (adversarial) | §3 determinism, §4a |
| "Preserve the ListItemsOfAShip evidence cap" | §3a first bullet |
| "extract renderer before CLI expansion" | §5 renderer precondition |

## 7. Open question for the synthesizer (non-blocking)

The brief says "requested/reserved/unmet demand where supported". Task F
persisted state exposes `requested_quantity` and `reserved_quantity` per
entry, and DEMAND_EXCEEDS_SUPPLY conditions. Unmet demand is derivable as
`requested - reserved` per (design, source) but Task F deliberately never
materializes "unmet" as a first-class quantity (shortfalls are EXPOSED as
conditions, never converted into acquisition semantics). DECISION needed by
implementation: represent unmet as a derived value at PORTFOLIO time only
(computed, not persisted), so the snapshot stays a faithful record of Task F
outputs and "unmet" never enters the persisted state as an invented semantic.
This lane's recommendation: derived-at-portfolio, and it must not be labeled
an acquisition need — render as "unmet (informational)".

## 7a. Deterministic serialization format decision (for implementation lane)

DECISION: JSON (stdlib json module), UTF-8, one snapshot per file, canonical
ordering: dict keys sorted, arrays in the deterministic orders from §3. V1
JSON schema (concrete field names pinned — implementation should treat any
field-name deviation as a malformed snapshot):

```json
{
  "version": 1,
  "account": {
    "user_id": 123456,
    "display_name": "SomeCaptain",
    "ship_id": 478542
  },
  "captured_at": "2026-08-08T14:30:00Z",
  "inventory": [
    {"item_id": 181673752, "ship_id": 478542, "item_design_id": 90,
     "quantity": 56, "is_new": false, "skin_key": null,
     "evidence": "CAPTURED_NOT_VERIFIED"}
  ],
  "classification": [
    {"item_id": 181673752, "bucket": "CONSUMABLE", "protected": true,
     "protection_reasons": ["PROTECTED_INSUFFICIENT_EVIDENCE"],
     "roles": ["AMMUNITION_MATERIAL"], "evidence": "UNKNOWN"}
  ],
  "demand": {
    "entries": [
      {"item_design_id": 90, "source": "EQUIPMENT",
       "requested_quantity": 2, "reserved_quantity": 2,
       "reason": "equipped on Character 12345",
       "provenance": "Character/@ItemIds",
       "evidence": "CAPTURED_NOT_VERIFIED"}
    ],
    "conditions": [
      {"code": "SOURCE_UNSUPPORTED", "detail": "TRAINING", "design_id": null}
    ]
  },
  "reduction": [
    {"item_id": 181673752, "item_design_id": 90,
     "observed_quantity": 56, "total_reserved": 2, "unreserved": 54,
     "candidate_quantity": 0, "state": "HOLD",
     "reasons": ["BLOCKED_UNSUPPORTED_DEMAND"],
     "evidence": "UNKNOWN", "disposition_hint": "KEEP", "actionable": false}
  ],
  "evidence_summary": {
    "weakest": "UNKNOWN",
    "feed": "CAPTURED_NOT_VERIFIED"
  },
  "unresolved_conditions": [
    {"code": "SOURCE_UNSUPPORTED", "detail": "no plan representation", "design_id": null}
  ]
}
```

Field names deliberately mirror the domain records so serialization is a
1:1 mapping with no renaming layer: `Item` (item_id, ship_id,
item_design_id, quantity, is_new, skin_key), `ClassifiedItem`
(classification enum value as "bucket", protected, protection_reasons,
roles, evidence), `ReservationEntry` (item_design_id — NOT design_id —,
source, requested_quantity, reserved_quantity, reason, provenance,
evidence), `UnresolvedDemand` (code, detail, design_id — note the
INCONSISTENCY: UnresolvedDemand uses `design_id`, ReservationEntry uses
`item_design_id`; the snapshot format keeps each record's own name to
avoid a translation bug, i.e. conditions carry `design_id`, entries
carry `item_design_id`), `ReductionResult` (item_id, item_design_id,
observed_quantity, total_reserved, unreserved, candidate_quantity,
state, reasons — PLURAL, a list —, evidence, disposition_hint,
actionable). The `evidence` key on every row is the row-level
weakest-input tier. Bucket values are constrained to the four the
classifier can emit: PROTECTED, RESERVED, CONSUMABLE, UNKNOWN.

Adversarial-test hooks this format deliberately creates: unknown keys at
snapshot root or account block (malformed), `version: 2` (rejected),
duplicate item_id rows (rejected), classification row for a missing item
(cross-block contradiction), negative quantity (rejected), bad evidence
string (rejected), future captured_at (rejected), stale-but-valid (accepted
+ marked), truncated file (corrupt -> rejected).
