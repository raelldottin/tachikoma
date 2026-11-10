# Pixel Starships PvP Research Digest — GOAL-PVP-001

**Artifact:** `pss-pvp-research-digest.md`
**Task:** `t_e104fc3d` (research lane)
**Swarm root / blackboard:** `t_a812900d`
**Author profile:** `tachikoma-researcher`
**Date authored:** 2026-10-28 (workspace local)
**Status:** Research digest — hypotheses and sourced facts; not implementation.

---

## 1. Purpose and scope

This digest supports the GOAL-PVP-001 swarm ("Transform Tachikoma into an offline,
evidence-driven Pixel Starships account progression and PvP optimization system"). It
consolidates:

1. A baseline inventory of the existing Tachikoma repository systems that PvP work must
   reuse rather than rebuild.
2. Researched, sourced facts about current (2026) Pixel Starships PvP mechanics, hull
   progression, native combat AI, boarding, crew training/equipment, room upgrades,
   armor/power/repair, and research prioritization.
3. The 2026 Hull 14 (V0.999.55) and subsequent Galaxy (V0.999.60) balance changes.
4. A proposed offline battle-results schema and metric set.
5. A per-account progression roadmap template and scoring framework skeleton.
6. Explicit unknowns, evidence tiers, and fair-play boundaries.

**Fair-play boundary (restated):** Tachikoma is an offline strategy/decision-support tool.
No automatic matchmaking, battle execution, account control, farming, marketplace trading,
transfers, premium purchases, unattended gameplay, credential/token extraction, or
authenticated production API traffic. Native combat AI is configured manually by the human
account holder. Do not use known-exposed credentials.

---

## 2. Evidence tiers used

| Tier | Meaning |
|---|---|
| **VERIFIED** | Confirmed against live server capture or official source. |
| **CAPTURED** | Present in a real capture; not yet cross-validated. |
| **STATIC** | Derived from binary/static analysis; unverified at runtime. |
| **COMMUNITY** | Community guide / wiki / forum; treat as hypothesis, especially post-Hull-14. |
| **UNKNOWN** | Not available; must be reported as UNKNOWN, never fabricated. |

Every downstream recommendation must carry its tier. Inferred causes are never labeled as
measured facts.

---

## 3. Repository baseline (reuse, do not rebuild)

Inspected at `/Users/raelldottin/Documents/Personal/tachikoma` (HEAD `cabdaba`, clean tree).

### 3.1 Existing modules relevant to PvP

| Module | Role | Reuse notes |
|---|---|---|
| `sdk/crew_leveling.py` | Crew XP/gas upgrade costs, TP caps by rarity, stat formulas | Reuse `compute_final_stat`, `compute_final_ability`, `compute_final_stamina`, `get_tp_cap`, `get_crew_level_cap`. Note: duplicate dict keys in `STANDARD_GAS_REQUIRED` / `LEGENDARY_GAS_REQUIRED` (last wins) — flag for data-quality review before scoring. |
| `sdk/game_formulas.py` | Wiki formulas: room reload, escape, dodge, damage reduction, fire damage, crew stat by level, gas draw price, trophy gain | Reuse all 9 formulas; trophy gain capped 1–40. |
| `sdk/ship_layout.py` | Read-only layout analyzer (armor coverage, repair proximity, weapon balance, power balance, defense score 0–100) | Reuse `analyzeShipLayout()`; it never mutates. Hull-specific thresholds should be added, not a rewrite. |
| `sdk/inventory/*` | Read-only inventory protection/reservation/reduction/transfer planning, snapshots, portfolio | Reuse for resource/ammunition/craft availability. Transfer planning is HOLD (unverified). |
| `sdk/client.py` | HTTP client; battle endpoints (CreateStarBattle5, VerifyBattle2, FinaliseBattle15) | Read-only snapshot + battle flow only; no auto-battle. |
| `sdk/commands.py`, `sdk/tui.py` | CLI/TUI | Add read-only PvP report commands, not mutations. |
| `tests/` | 1140 tests passing (skipped=1) | Baseline for regression; ruff reports 25 pre-existing errors, ty 139 diagnostics (mostly pre-existing). |

### 3.2 Existing tests touching PvP/battles

- `tests/test_pvp_battles.py` — end-to-end battle flow (Create/Verify/Finalise) with mocked traffic.
- `tests/test_ship_layout.py` — layout scoring.
- `tests/test_crew_leveling.py`, `tests/test_game_formulas.py` — formulas.
- `tests/test_inventory*.py` — inventory protection/reservation.

### 3.3 Known data-quality caveats

- `STANDARD_GAS_REQUIRED` has duplicate keys (levels 9, 17) — Python keeps last; documented in source. Any scoring must use a cleaned, deduplicated table or mark UNKNOWN.
- Crew level cap formula: `ship_level * 4`, max 40 (verified in `get_crew_level_cap`).
- TP caps: 3★=70, 4★=80 (exceptions Mistycball/Huge Hellaluya =100), 5★=90, 6★=100 (captains 200), 7★=110.

---

## 4. Pixel Starships PvP mechanics (sourced)

Sources: official blog patch notes (V0.999.55, V0.999.60), Fandom wiki (Battles, How To Play), Herzbube wiki (PSSCombat, PSSMyStrategies), Pixel Perfect Guide, Battleship Cobra FAQ, community Reddit. Community content is treated as **COMMUNITY** tier and as hypotheses post-Hull-14.

### 4.1 Core battle loop

- PvP matches a ship against an offline opponent of roughly ±2 ship levels (trophy-based matchmaking).
- Battle duration: 3 minutes to reduce opponent hull HP to 0.
- Attacker has a distinct advantage (defender is offline, cannot actively defend; defender AI only).
- All room consumables used while defending are refunded (wooden box).
- Immunity: 8 hours after a failed defense; 20 minutes after a successful defense. AI events count as real battles (no trophy loss, but repair time + small immunity on loss).
- Non-powered rooms have a 300% base armor (not worth targeting, but cannot be repaired).
- Surplus system damage to a destroyed room converts to hull damage (weapons); crew surplus system damage has no effect. Hull damage lowers ship HP. Armor reduces system and hull damage, even direct hull damage.
- Room HP is tied to max power consumption/production; damaged rooms can only consume/produce up to truncated HP.

### 4.2 Trophy system

- Trophies gained/lost by PvP; `trophy_gain(loser, winner)` capped 1–40 (in `game_formulas.py`).
- Matchmaking by trophy count; mineral/gas bonus for winning scales with ranking.
- Peak trophy count per ship level — climbing requires upgrades.

### 4.3 Boarding

- Teleport room enables boarding enemy ships; boarding crew fight, extinguish fires, repair targeted room type.
- Crew AI groups: Special Power and Targeting; first true+valid command per group per frame (40 fps).
- No targeting specific rooms, only room types; crew go to closest room of that type with space.
- Boarding crew should target reactors (commonly boarded, should not be crewed), TLP, hangars, etc.
- Anti-boarding: security gates, gas traps, proximity mines in reactors, crew placement.
- Hull 14: cloaked crew movement speed halved; cloaked crews decloak faster near enemy crew; turrets can target cloaked crew; mine modules no longer damage/freeze friendly crew; security gate HP/power 1→2; teleport repulsor HP/power 3→4.

### 4.4 Native combat AI

- Command Center room required only while editing AI; AI persists after removal.
- Python 1/2/3 research (lab) unlocks more commands; conditions/actions expand with research and updates.
- Conditions evaluated top-to-bottom; first true+valid command per group executed.
- Power-related AI conditions not selectable for non-power rooms; queue conditions only for queued rooms; target-room conditions only for targeting rooms.
- Hull 14 adds: AI support for checking new status effects; Set Target Mode for Anti-Craft rooms; "Ammo Selected" conditions; expanded defensive craft count / missile count conditions; priority accounted when ship takes power from rooms when reactors damaged.
- Focus-fire (FF) strategies: target a room type and keep firing; "Enemy Ship has Full Shield → Target Enemy Lasers" trick keeps targeting first laser.
- Two-phase targeting common: attack TLP/boarders first, then switch to softer hull-damage target when enemy hull < 100%.

### 4.5 Crew roles and stats

- Crew stat formula: `final = base × (1 + TP/100) + equipment` (stat); ability `base × (1 + TP/100) × (1 + equipment%)`; stamina `TP + equipment` (additive).
- TP caps by rarity (above). Crew level cap = ship level × 4, max 40.
- Crew boost rooms by appropriate stat (WPN for weapons, SCI for shields, etc.).
- Hull 14: max crew cap +3; new legendary PLT crew (July 1); revive chamber room; crew can reinforce a room (temporary max HP based on Repair stat); Urgent Repair reinforces up to ABL/4 HP; AP damage ignores reinforced HP.
- Crew movement speed halved while cloaked; medical rooms/healing cure status effects.
- RST stat now reduces poison duration (V0.999.58).

### 4.6 Rooms, armor, power, repair

- Armor blocks adjacent to critical rooms add protection (each adjacent side adds).
- Lifts on deck ends to avoid traffic jams; repair proximity matters.
- Power balance: reactor output vs consumption; non-powered rooms have 300% armor.
- Repair rooms (medbay/medical) heal crew and cure status; repair crew target condition room.
- Hull 14: room construction/ammo build/research times reduced; mineral/gas collector capacity/rate increased; gray hulls overhauled; new room types (Warp Hangar, Corvette Hangar, Tactical Hangar, Revivification Chamber, Gamma Beam Emitter, Coilgun, Auxiliary Boosters, Revivification Chamber).
- New hangar craft types (Warp/Corvette) with distinct roles; anti-craft rooms count craft squads as single target; tractor beam multi-target; bombshell AP damage reduced.
- Reinforcement research requirement (V0.999.60: "New Research Requirement: Room Reinforcement").
- Defensive craft reload no longer paused during target cloak; crafts stop attacking if origin hangar has no target.

### 4.7 Research

- Lab upgrades unlock Python 1/2/3 AI.
- Research times reduced across all levels (Hull 14 + Galaxy).
- New research: Room Reinforcement (V0.999.60).
- Starbase researches, ship researches, dropship, merchant, etc.
- Gas draw price formula (capped 2M) in `game_formulas.py`.

### 4.8 Hull progression

- Community consensus: max out rooms and crew to level cap before upgrading hull; rushing hull weakens PvP competitiveness.
- Crew level cap = hull level × 4.
- Room max level tied to ship level.
- Ship upgrades increase size and room options but raise opponent level range.
- Hull 14 raises max hull to 14; new rooms unlock at higher levels.
- Premium/gray hulls overhauled.

### 4.9 Boarding and anti-boarding readiness (post-Hull-14)

- Security gate HP/power 1→2; teleport repulsor 3→4; teleport interdictor defense 50→100.
- Mine modules no longer harm friendly crew.
- Ardent collection perk applies 20–30s EMP to all modules in current room (1 use).
- Anti-craft: rooms count craft squads as single target; manual target mode for anti-craft.
- Cloaked crew: movement speed halved (Hull 14), decloak faster near enemy crew (Galaxy), turrets can target cloaked crew, security gates detect/damage cloaked crew (V0.999.58).

---

## 5. 2026 meta changes summary (Hull 14 + Galaxy)

Consolidated from official patch notes (authoritative for balance numbers). Old community guides must be treated as hypotheses where mechanics changed.

Key deltas affecting PvP:
- Crew cap +3; new legendary PLT crew.
- Room construction/ammo build/research times reduced (faster progression).
- Mineral/gas collector capacity/rate increased.
- New room types and hangar craft categories (Warp/Corvette) expand strategies.
- Reinforcement mechanic (crew adds temporary max HP; AP ignores reinforced HP) changes targeting priority.
- Cloak nerfs (movement halved, turrets target cloaked, security gates detect cloaked) reduce boarding stealth.
- Bolter damage 0.5→0.3 but reload 3.25→1s (faster, lower per-shot).
- Railgun gains shield piercing.
- Plasma discharger max 2→3; missile pod max 2→3; seeker defense min range 500→600.
- Craft behavior changes (anti-craft counts squads as one target; crafts stop if origin hangar no target; defensive craft reload not paused by cloak).
- New missile types (Cryo, Dampener, Breaker, Suppressor, Chimera torpedo) expand debuff strategies.
- Auxiliary boosters max boost 15%→12%.
- Irradiator: single shot, 10s poison; crew damage 3→2 (V0.999.58: 2→3).
- Teleport cooldown increased at lower levels; room cooldowns now progress separately from reload times (Galaxy).
- Engine evasion regeneration reduced (5%→4%, thrusters 3.5%→3%); fusion drive no longer increases max evasion but grants 1% regen every 0.5s.
- Shields reload 8→7s; bolter reload 1→2s (V0.999.58).
- Anti-craft rooms prioritize decoy crafts; anti-craft with random unique/random once follow target priority for all volleys.

---

## 6. Proposed offline battle-results schema (sanitized/manual)

No live capture required; human-played battles logged manually. Must separate measured from inferred.

Fields:
- `battle_id` (local, non-secret)
- `account_pseudonym` (attacker or defender perspective)
- `role` (attacker | defender)
- `opponent_pseudonym_or_anon` (optional, non-secret)
- `opponent_hull_level` (int or UNKNOWN)
- `outcome` (win | loss | draw | timeout)
- `trophy_delta` (int; measured)
- `battle_duration_s` (int; measured)
- `major_rooms_lost` (list of room-type strings; measured/observed)
- `battle_strategy` (focus_fire | reactor_shield_devastation | weapon_devastation | boarding | hybrid | unknown) — human-labeled
- `suspected_loss_cause` (enum: boarding | focus_fire | power_loss | repair_failure | crew_death | ammo_depletion | timeout | unknown) — INFERRED, labeled as suspected
- `ship_config_version` (string hash/version of layout+AI+crew snapshot)
- `account_hull_level` (int)
- `trophy_before`, `trophy_after`
- `notes` (free text, sanitized)
- `created_at` (ISO timestamp)

Metrics to track (per account, per hull level, per config version):
- attack win rate, defense win rate
- trophy progression (net, per battle, rolling)
- losses by cause (counts; inferred causes flagged)
- average battle duration
- performance by opponent hull level
- before/after configuration comparisons (paired by config version)

**Invariant:** suspected causes are never reported as measured facts. UI/report must label "suspected".

---

## 7. Account progression profile (per account, non-secret)

Fields (UNKNOWN when missing; never fabricate):
- `account_pseudonym`
- `hull_level`, `trophies`, `peak_trophies`
- `crew[]`: name, level, rarity, abilities (training points), equipment, assigned room, role
- `rooms[]`: design id, level, position, power, HP, ammo config
- `research[]`: completed research list + levels
- `armor_power_repair`: armor coverage %, power balance, repair proximity (from ship_layout analyzer)
- `ammo_craft_config`: ammo types, craft squads, hangars
- `native_ai_rules`: room/crew AI command lists (text)
- `upgrade_costs_resources`: known costs + available resources
- `recent_battles[]`: battle-results schema above
- `ship_config_version`

---

## 8. Scoring framework skeleton (hull-specific)

All scores 0–100 with explicit components and unknown handling. Thresholds must be hull-specific, not universal.

1. **Hull-upgrade readiness**: rooms at max for current hull? crew at level cap? resources available? → ready / not-ready + blockers.
2. **Crew role assignment & leveling priorities**: role coverage (repair, boarding, weapon boost, shield boost, boarding defense); level gaps vs cap; TP gaps vs cap.
3. **Crew training & equipment priorities**: TP deficit per crew; equipment slots.
4. **Combat-room upgrade priorities**: room level vs hull max; high-impact rooms first (reactors, weapons, shields, teleport, repair).
5. **Ship armor/power/repair coverage**: reuse ship_layout analyzer outputs.
6. **Native weapon targeting & focus-fire rules**: AI command completeness, FF strategy presence, two-phase targeting.
7. **Native repair/healing/defensive AI**: repair crew AI, medical rooms, status-effect curing.
8. **Boarding & anti-boarding readiness**: teleport, security gates, gas traps, mines, crew placement.
9. **Research prioritization**: Python levels, Room Reinforcement, lab level, collection perks.
10. **Resource investment efficiency**: mineral/gas/starbux per upgrade; time.
11. **Attack vs defense performance**: win rates from battle log.
12. **Battle failure patterns & corrective recommendations**: suspected-cause aggregation.

Each recommendation must expose: source data, assumptions, unknown inputs, reason, evidence tier.

---

## 9. Progression roadmap template (per account)

- **Immediate fixes** (today): e.g., exposed reactors, missing lift, uncrewed weapons, AI gaps.
- **Next 7 days**: upgrade high-impact rooms, train key crew to TP cap, configure AI, log 5+ battles.
- **Next 30 days**: hull upgrade criteria check, research Python levels, boarding/anti-boarding setup, craft/ammo config.
- **Criteria for advancing hull level**: all rooms maxed at current hull; crew at level cap; trophy plateau; resources for next hull; no critical AI gaps.
- **Highest-value crew improvements**: list.
- **Highest-value room/research improvements**: list.
- **Next native AI changes to test**: one change at a time, A/B via battle log.
- **Expected resource requirements**: when supported by known costs; otherwise UNKNOWN.

---

## 10. Unknowns and data gaps (report, do not fabricate)

- No account snapshots provided in this task; all account state = UNKNOWN until sanitized manual export/screenshot.
- Hull 14 exact room unlock levels by hull (1–14) not captured here; use pixel-prestige/room list or in-game data; community tables are COMMUNITY tier.
- Gas/XP upgrade cost tables in `crew_leveling.py` have duplicate-key data-quality issues (levels 9, 17) — must be reconciled before scoring.
- Native AI condition/action availability by Python level and hull not fully enumerated; treat community guides as hypotheses.
- Battle-results dataset does not yet exist; must be created by downstream lane.
- Transferability of items between accounts remains unverified (separate inventory goal; do not touch).

---

## 11. Recommendations for downstream implementers

- Reuse `crew_leveling`, `game_formulas`, `ship_layout`, inventory snapshot/portfolio modules; do not rebuild.
- Implement hull-specific thresholds as configuration data, not hardcoded universal constants.
- Keep all PvP evaluation read-only and deterministic; no network calls in scoring.
- Add regression tests for: deterministic recommendations, input validation, account isolation, outcome calculation, no secret leakage, zero mutations.
- Run `make test`, `uv run ruff check`, `uv run ty check`, `git diff --check`.
- Verifier (`t_b454cc29`) completes only with metadata `{"gate":"pass"}`.

---

## 12. Sources

Official:
- Pixel Starships blog, "Hull 14 Major Update! (Patch Notes V0.999.55)", 2026-06-17.
- Pixel Starships blog, "Galaxy Patch Notes V0.999.60", 2026-08-17.
- Pixel Starships wiki Formulas (referenced via repo integration).

Community (treat as hypotheses post-Hull-14):
- pixelstarships.fandom.com/wiki/Battles, How_To_Play.
- wiki.herzbube.ch (PSSCombat, PSSMyStrategies).
- pixelperfectguide.com (ship strategy, crew AI, focus fire).
- battleshipcobra.com (new player FAQ).
- levelwinner.com (upgrade advice).
- Reddit r/PixelStarships threads on leveling/AI.

Repository:
- `/Users/raelldottin/Documents/Personal/tachikoma` HEAD `cabdaba`, 1140 tests OK (skipped=1), ruff 25 pre-existing errors, ty 139 diagnostics (pre-existing).
- `sdk/crew_leveling.py`, `sdk/game_formulas.py`, `sdk/ship_layout.py`, `sdk/inventory/*`, `tests/test_pvp_battles.py`.

---

## 13. Machine-readable facts (summary)

See completion metadata for structured facts. Key facts:
- `official_patch_latest_reviewed`: V0.999.60 (2026-08-17)
- `hull14_patch`: V0.999.55 (2026-06-17), max hull 14
- `repo_tests_baseline`: 1140 OK, skipped=1
- `repo_ruff_errors`: 25 (pre-existing)
- `repo_ty_diagnostics`: 139 (pre-existing)
- `crew_level_cap_formula`: ship_level * 4, max 40
- `tp_caps`: {3:70, 4:80 (exceptions Mistycball/Huge Hellaluya=100), 5:90, 6:100 (captains 200), 7:110}
- `trophy_gain_cap`: 1-40
- `battle_duration_s`: 180
- `immunity_failed_defense_h`: 8
- `immunity_successful_defense_min`: 20
- `non_powered_room_armor_pct`: 300
- `fair_play`: offline decision-support only; no auto gameplay/mutations
