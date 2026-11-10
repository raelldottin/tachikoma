# Audit: read-only ship and battle data models + sanitized inputs

**Task:** `t_10365d08` (protocol lane, GOAL-PVP-001)
**Author profile:** `tachikoma-protocol`
**Swarm root / blackboard:** `t_a812900d`
**Date:** 2026-10-28 (workspace local)
**Status:** Audit artifact + sanitized-input specification. No live network traffic. No mutations.

---

## 1. Purpose

This artifact (a) audits the existing read-only ship and battle data models in
Tachikoma, (b) records which are pure/offline-safe versus which touch the
network, and (c) defines the sanitized input contract that downstream PvP
analysis must use so that no account state, credential, or live traffic is ever
required.

Fair-play boundary (restated): Tachikoma is an offline strategy/decision-support
tool. No automatic matchmaking, battle execution, account control, farming,
marketplace trading, transfers, premium purchases, unattended gameplay, credential
or token extraction, or authenticated production API traffic. Native combat AI is
configured manually by the human account holder. Known-exposed credentials are
not used.

---

## 2. Audit method

- Static inspection only: source files, imports, tests. No network calls.
- Network purity check: grep for `requests`, `urllib`, `http`, `socket`, and
  `accessToken`/`deviceKey`/`password` usage in each audited module.
- Cross-checked against `tests/test_ship_layout.py`, `tests/test_pvp_models.py`,
  `tests/test_pvp_battles.py`, `tests/test_game_formulas.py`,
  `tests/test_crew_leveling.py`.
- Baseline: HEAD `cabdaba`, 1140 tests passing (skipped=1), ruff 25 pre-existing
  errors, ty 139 diagnostics (mostly pre-existing).

---

## 3. Audited modules

### 3.1 `sdk/ship_layout.py` — read-only layout analyzer

- **Network purity:** PURE. Imports only `math`, `dataclasses`, `typing`,
  `collections.Counter`. No `requests`/`http`/`socket`. No credentials.
- **Inputs:**
  - `ship_data: dict` — parsed `GetShipByUserId` payload, shape
    `{"Rooms": {"Room": [ {...@RoomId, @RoomDesignId, @Row, @Column,
    @RoomStatus, @UpgradeRoomDesignId...} ]}}`. May be a single dict room
    (handled: dict → list).
  - `room_designs: list[dict]` — from `ListRoomDesigns2`/`ListAllDesigns4`,
    each with `@RoomDesignId`, `@RoomName`, `@ColumnWidth`, `@RowHeight`,
    `@RoomHp`, `@PowerGenerated`, `@MaxPopulation`.
  - `ship_name`, `ship_level`, `ship_design_id` (optional reporting context).
- **Outputs:** `list[RoomInfo]`, `LayoutAnalysis` (scores 0–100, recommendations).
- **Mutations:** None. Pure functions.
- **Gaps / caveats:**
  - Room-design lookup is by `@RoomDesignId` string; missing designs degrade to
    `"other"` category (acceptable, fail-soft).
  - `power` parsing uses `@PowerGenerated`; non-reactor rooms with power
    consumption are not modeled (consumption estimated as `3` per non-armor room).
  - No validation that `ship_data` came from a sanitized source; callers must
    strip secrets before passing. This artifact defines that contract (§5).
  - `_get_attr` handles both `@Key` and `Key` forms (xmltodict quirk) — good.
- **Verdict:** REUSE AS-IS for offline analysis. Do not rewrite.

### 3.2 `sdk/game_formulas.py` — pure wiki formulas

- **Network purity:** PURE. Imports `math`, `typing`. No network.
- **Functions:** room reload boosted, escape, dodge, damage reduction, fire
  damage, crew stat by level, gas draw price, trophy gain (capped 1–40).
- **Verdict:** REUSE AS-IS. Hull-specific thresholds to be added downstream, not
  here.

### 3.3 `sdk/crew_leveling.py` — crew XP/gas/TP caps

- **Network purity:** PURE. Imports `math`, `dataclasses`, `enum`, `datetime`,
  `typing`. No network.
- **Data-quality caveat (known):** `STANDARD_GAS_REQUIRED` /
  `LEGENDARY_GAS_REQUIRED` contain duplicate dict keys (levels 9, 17); Python
  keeps last value. Any scoring must use a cleaned, deduplicated table or mark
  UNKNOWN. Flagged for data-quality review; do not silently trust.
- **Verdict:** REUSE, but deduplicate gas tables before scoring (or report UNKNOWN).

### 3.4 `sdk/client.py` — HTTP client (network-touching)

- **Network purity:** IMPURE by design. Uses `requests`, `urllib`, HTTP URLs,
  `accessToken`, `deviceKey`, `password` flows.
- **Read-only snapshot methods (safe to call for data collection by the human
  account holder, never by automation):**
  - `getShipByUserId(userId)` → `self.shipByUserId`
  - `listRoomDesigns2()` → `self.roomDesigns`
  - `listAllDesigns4()` → `self.allDesigns`
  - `listPvPBattles2(take, skip)` → `self.pvpBattles`
  - `listMissionBattles(take, skip)` → `self.missionBattles`
  - `listAllTaskDesigns2`, `listAllTrainingDesigns2`, `getLatestVersion3`
- **Battle-flow methods (MUTATING / gameplay — must NOT be automated):**
  - `createStarBattle5`, `verifyBattle2`, `finaliseBattle15`, `createBattle9`,
    `acceptBattle5`. These execute/resolve battles. Fair-play boundary forbids
    automation. Allowed only as manual human play; results are then logged via
    the sanitized battle-results schema (§6).
- **Sanitization requirement:** Any data exported from client attributes for
  offline use must pass through `reject_secret_keys` and `redact_dict`/
  `redact_secrets` (see §5). `accessToken`, `refreshToken`, `deviceKey`,
  `password`, `email`, `checksum`, `clientKey` must never appear in sanitized
  inputs.
- **Verdict:** Keep as collection tool for the human holder. Offline analysis
  consumes only the sanitized exports, never the live client.

### 3.5 `sdk/pvp/models.py` — offline PvP domain models (new, t_0608cb40)

- **Network purity:** PURE. Imports `dataclasses`, `enum`, `typing`, `hashlib`,
  `json`. No network.
- **Key safety properties:**
  - `FORBIDDEN_SECRET_KEYS` + `reject_secret_keys()` fail-closed on any forbidden
    key anywhere in a payload (recursive).
  - `PvpInputError` raised on invalid input (fail closed, no silent coercion).
  - `BattleResult` enforces role/outcome/strategy/cause enums, trophy
    consistency (`trophy_after - trophy_before == trophy_delta`), hull range
    1–20, non-negative durations.
  - `AccountProgressionProfile` enforces account isolation: every
    `recent_battles` entry's `account_pseudonym` must equal profile pseudonym;
    duplicate crew names rejected; metrics 0–100; secret-key rejection on
    `ammo_craft_config` and `resources`.
  - `compute_ship_config_version()` is a deterministic, non-secret SHA-256
    (16 hex) over a canonical JSON of ship state. Stable across runs.
- **Gaps / caveats:**
  - `CrewMember.abilities` is `Dict[str, Any]` — callers must ensure no secret
    keys; `reject_secret_keys` is called in `__post_init__`.
  - `BattleResult.notes` is scanned via `reject_secret_keys({"notes": ...})` only
    at the top level; free text should also be redacted of emails/tokens by the
    human before entry (use `redact_secrets`).
  - `opponent_pseudonym` is optional and should be anonymized or omitted when
    sharing.
  - No `max_level` enforcement linkage between `CombatRoom.level` and hull; hull-
    specific thresholds live in scoring (COMMUNITY tier).
- **Verdict:** REUSE AS-IS. This is the canonical sanitized input model.

### 3.6 `sdk/pvp/config.py`, `scoring.py`, `performance.py`, `planner.py`, `report.py`

- **Network purity:** PURE (imports only stdlib + `sdk.crew_leveling`).
- **Thresholds:** labeled COMMUNITY/STATIC/UNKNOWN; hull-specific configs.
- **Verdict:** REUSE AS-IS.

### 3.7 `sdk/redaction.py` — secret redaction utilities

- **Network purity:** PURE.
- **Functions:** `redact_secrets`, `redact_dict`, `safe_log_message`,
  `redact_log`.
- **Verdict:** REUSE for sanitizing any exported snapshot before offline use.

### 3.8 `sdk/inventory/*` (sanitized fixtures precedent)

- Precedent: `tests/fixtures/task_j_capture/sanitized_records.json` and
  `sdk/inventory/sanitize.py` define the "raw capture → REDACTED fixture"
  pattern with fail-closed secret removal. The PvP sanitized inputs follow the
  same discipline: if secret removal cannot be proven, fail closed (do not emit).

---

## 4. Network-purity summary (grep results)

| Module | Network imports? | Credentials referenced? | Safe for offline? |
|---|---|---|---|
| `sdk/ship_layout.py` | No | No | Yes |
| `sdk/game_formulas.py` | No | No | Yes |
| `sdk/crew_leveling.py` | No | No | Yes |
| `sdk/pvp/*` | No | No (forbidden-key guards) | Yes |
| `sdk/redaction.py` | No | No (redacts only) | Yes |
| `sdk/client.py` | Yes (`requests`) | Yes | No — collection tool only |
| `sdk/inventory/*` | No (sanitize/reconcile pure) | Guards present | Yes |

Verification command (run in worktree):

```
grep -RE "import requests|urllib|socket|http" sdk/ship_layout.py sdk/game_formulas.py sdk/crew_leveling.py sdk/pvp/ sdk/redaction.py
# expected: no matches
```

---

## 5. Sanitized input contract (no live network traffic)

### 5.1 General rules

1. **No secrets ever.** Sanitized inputs must not contain `accessToken`,
   `refreshToken`, `deviceKey`, `password`, `email`, `checksum`, `clientKey`,
   `apiKey`, or any token-like value. Enforced by `reject_secret_keys()`.
2. **Anonymize accounts.** Use pseudonyms (`acctA`, `acctB`) or anonymized
   opponent labels (`anon-opp-N`). Never real player names/emails/IDs if sharing.
3. **No live traffic.** Inputs are produced by the human account holder from
   in-game screens or a one-time manual export, then redacted. Agents never call
   the API.
4. **Fail closed.** If secret removal cannot be proven, do not emit the input.
   Mirror `sdk/inventory/sanitize.py` discipline.
5. **Evidence tiers.** Every field carries (or inherits) an evidence tier:
   VERIFIED / CAPTURED / STATIC / COMMUNITY / UNKNOWN. Missing data → UNKNOWN,
   never fabricated.
6. **Account isolation.** A sanitized input bundle belongs to exactly one
   account pseudonym. Cross-account data must not be mixed (enforced by
   `AccountProgressionProfile`).
7. **Measured vs inferred.** Battle `suspected_loss_cause` is INFERRED and must
   be labeled "suspected" in any report. `trophy_delta`, `battle_duration_s`,
   `major_rooms_lost` are MEASURED.

### 5.2 Sanitized ship snapshot (input to `ship_layout.parse_rooms`)

Required fields (all non-secret):

```json
{
  "ship_name": "string (pseudonym-safe, no player name)",
  "ship_level": "int (hull level)",
  "ship_design_id": "string (e.g. HULL-10)",
  "ship_data": {
    "Rooms": {
      "Room": [
        {
          "@RoomId": "string (local id, may be original or renumbered)",
          "@RoomDesignId": "string",
          "@Row": "string-int",
          "@Column": "string-int",
          "@RoomStatus": "string (normal|constructing|...)",
          "@UpgradeRoomDesignId": "string (0 if none)"
        }
      ]
    }
  },
  "room_designs": [
    {
      "@RoomDesignId": "string",
      "@RoomName": "string",
      "@ColumnWidth": "string-int",
      "@RowHeight": "string-int",
      "@RoomHp": "string-int",
      "@PowerGenerated": "string-int",
      "@MaxPopulation": "string-int"
    }
  ]
}
```

Sanitization steps:
- Remove `accessToken`, `userId` (real), `@Name` (player), email, device keys from
  the raw `GetShipByUserId` payload. Keep only `Rooms` and design attributes.
- Room positions (`@Row`, `@Column`) are safe (no secrets) and needed for layout
  analysis; keep them.
- `room_designs` may be sourced from the static design list (no secrets) or
  captured; either way strip credentials.

### 5.3 Sanitized account progression profile (input to `sdk.pvp`)

Maps to `AccountProgressionProfile` (see `sdk/pvp/models.py`). All fields
optional except `pseudonym`, `hull_level`. Use UNKNOWN for missing data.

- `pseudonym`: non-empty string, no real identity.
- `hull_level`: int 1–20.
- `trophies`, `peak_trophies`: int or null.
- `crew`: list of `CrewMember` (name, level 0–40, rarity 1–7, training_points
  ≥0, equipment, role, assigned_room, abilities dict, evidence_tier).
- `rooms`: list of `CombatRoom` (design_id, name, category, level, max_level,
  position_row/col, power, hp, is_powered, ammo_config, evidence_tier).
- `research`: list of `ResearchItem`.
- `native_ai_rules`: list of `NativeAIRule` (group, conditions, actions,
  target_type, evidence_tier).
- `armor_coverage`, `power_balance`, `repair_proximity`, `weapon_coverage`:
  0–100 floats from ship_layout analyzer, or null.
- `ammo_craft_config`, `resources`: dicts, secret-key-scrubbed.
- `recent_battles`: list of `BattleResult` (§6).
- `ship_config_version`: use `compute_ship_config_version()` or a manual version
  string; never include secrets.
- `assumptions`, `unknowns`: tuples of strings.
- `evidence_tier`: one of VERIFIED/CAPTURED/STATIC/COMMUNITY/UNKNOWN.

### 5.4 Sanitized crew/equipment/training inputs

- Crew names may be real in-game names (not secrets) but should be pseudonymized
  if the bundle is shared.
- Training points (TP) and levels are numeric; no credentials.
- Equipment strings are design names/IDs, safe.
- Abilities dict values must be numeric/string design data only; reject secret
  keys recursively.

### 5.5 Sanitized research inputs

- Research name + level + max_level + completed. No account identifiers.

### 5.6 Sanitized native AI inputs

- AI command groups as text (conditions/actions). No credentials. These are
  manually transcribed from the Command Center screen.

### 5.7 Sanitized resource inputs

- Resource amounts (mineral, gas, starbux, dilithium, etc.) as numbers.
- Upgrade costs as numbers when known, else UNKNOWN.
- Never include payment info, billing, or account IDs.

---

## 6. Sanitized battle-results schema (input to `sdk.pvp.performance`)

Maps to `BattleResult`. Fields (measured vs inferred labeled):

| Field | Type | Source | Measured/Inferred |
|---|---|---|---|
| `battle_id` | string (local) | human log | measured (local id) |
| `account_pseudonym` | string | human | n/a |
| `role` | attacker/defender | human | measured |
| `opponent_pseudonym` | string (optional, anonymized) | human | n/a |
| `opponent_hull_level` | int or null | human estimate | inferred/observed |
| `outcome` | win/loss/draw/timeout/unknown | human | measured |
| `trophy_delta` | int | game UI | measured |
| `battle_duration_s` | int or null | game UI/timer | measured |
| `major_rooms_lost` | list of room-type strings | human observation | measured/observed |
| `battle_strategy` | enum | human-labeled | human-labeled (not measured) |
| `suspected_loss_cause` | enum | human inference | **INFERRED — label "suspected"** |
| `ship_config_version` | string | `compute_ship_config_version()` | measured (derived) |
| `account_hull_level` | int or null | human | measured |
| `trophy_before`, `trophy_after` | int or null | game UI | measured |
| `notes` | string (sanitized free text) | human | n/a |
| `created_at` | ISO timestamp | human | measured |

Invariant: `suspected_loss_cause` is never reported as a measured fact.
`trophy_delta == trophy_after - trophy_before` when both present.

---

## 7. Audit findings summary

1. **Pure offline models exist and are reusable:** `sdk/ship_layout.py`,
   `sdk/game_formulas.py`, `sdk/crew_leveling.py`, `sdk/pvp/*`,
   `sdk/redaction.py`. None touch the network.
2. **Network-touching collection is isolated to `sdk/client.py`.** Offline
   analysis must consume only sanitized exports, never the live client. Battle
   execution methods must never be automated.
3. **Input validation is fail-closed** in `sdk/pvp/models.py` (secret keys,
   enums, ranges, account isolation, trophy consistency).
4. **Data-quality caveat:** `crew_leveling.py` gas tables have duplicate keys
   (levels 9, 17). Scoring must deduplicate or report UNKNOWN.
5. **Secret redaction precedent** exists in `sdk/inventory/sanitize.py` and
   `sdk/redaction.py`; PvP sanitized inputs follow the same discipline.
6. **No sanitized ship-input schema existed** before this artifact; §5.2 fills
   that gap. No battle-results dataset exists yet; §6 + fixtures provide the
   seed.

---

## 8. Fixtures provided

- `tests/fixtures/pvp_sanitized/ship_layout_sanitized.json` — sanitized ship
  snapshot consumable by `ship_layout.parse_rooms`.
- `tests/fixtures/pvp_sanitized/battle_results_sanitized.json` — two sanitized
  battle results consumable by `BattleResult` / performance metrics.

Both contain no secrets, no credentials, no real account identifiers.

---

## 9. Regression tests

- `tests/test_pvp_sanitized_inputs.py` — verifies:
  - fixtures load and parse without network,
  - `parse_rooms` produces the expected room count and categories,
  - `BattleResult` construction from fixture succeeds,
  - secret-key rejection on a poisoned payload,
  - account isolation rejection,
  - deterministic `compute_ship_config_version`,
  - no forbidden network imports in pure modules.

---

## 10. Cross-worker notes

- Sibling `t_0608cb40` (analyst) owns `sdk/pvp/*` models/scoring/performance.
  This audit reuses them; no changes made to that package.
- Sibling `t_e104fc3d` (researcher) proposed the battle-results schema; this
  artifact formalizes it as the sanitized input contract and adds fixtures.
- Sibling `t_5c582dd6` (architect) integrates; should wire sanitized-input
  loading into the progression/report pipeline.
- Sibling `t_e39758ee` (safety) enforces no credentials/automation; this
  artifact's §5 contract is the input-side counterpart.

---

## 11. Unknowns / not fabricated

- Real account state: UNKNOWN (no snapshots provided). Fixtures are synthetic and
  labeled as such.
- Hull-specific room unlock levels by hull: COMMUNITY tier, not verified.
- Gas/XP tables: data-quality caveat (duplicate keys) unresolved.
- Native AI condition/action availability by Python level: not fully enumerated.
