# PvP assessment integration architecture (t_5c582dd6)

**Task:** `t_5c582dd6` (architect lane, GOAL-PVP-001)
**Swarm root / blackboard:** `t_a812900d`
**Status:** Integration artifact. Offline only. No network, no credentials, no mutations.

## 1. Purpose

Define the module boundaries, data flow, and integration points for the offline
PvP assessment / native-AI recommendation / progression-planning / reporting
system added under GOAL-PVP-001. This document integrates the outputs of:

- `t_e104fc3d` — current PSS PvP research digest + facts
  (`docs/pss-pvp-research-digest.md`, `docs/pss-pvp-research-facts.json`)
- `t_10365d08` — read-only ship/battle data model audit + sanitized inputs
  (`docs/pvp-read-only-ship-battle-model-audit-t_10365d08.md`,
  `tests/fixtures/pvp_sanitized/`)
- `t_0608cb40` — offline readiness/scoring/performance/planning implementation
  (`sdk/pvp/`)

## 2. Module boundaries

```
sdk/
  pvp/
    __init__.py          # public surface (re-exported via top-level shims)
    models.py            # pure dataclasses + validation + secret-key rejection
    config.py            # hull-specific thresholds (reuses sdk.crew_leveling)
    scoring.py           # 12 deterministic offline evaluations
    performance.py       # battle-results metrics
    planner.py           # 7/30-day progression roadmap
    report.py            # read-only text reports
  account_progression.py # shim -> sdk.pvp
  battle_results.py      # shim -> sdk.pvp
  progression_roadmap.py # shim -> sdk.pvp
  pvp_models.py          # shim -> sdk.pvp
  pvp_performance.py     # shim -> sdk.pvp
  pvp_planner.py         # shim -> sdk.pvp
  pvp_report.py          # shim -> sdk.pvp
  pvp_scoring.py         # shim -> sdk.pvp
  crew_leveling.py       # reused for level/TP caps (verified)
  ship_layout.py         # reused for read-only layout analysis (pure)
  game_formulas.py       # reused pure wiki formulas
```

Boundary rules:

- `sdk.pvp` never imports `sdk.client`, `sdk.commands`, or any network module.
- `sdk.client` never imports `sdk.pvp` (mirrors the inventory purity invariant).
- All inputs are pure dataclasses; validation is fail-closed (`PvpInputError`).
- No mutation, matchmaking, battle execution, farming, trading, transfers,
  premium purchases, unattended gameplay, credential/token extraction, or
  authenticated production API traffic anywhere in the package.

## 3. Data flow

```
sanitized manual export / screenshot
        |
        v
AccountProgressionProfile (models.py, validated, account-isolated)
        |
        +--> hull_upgrade_readiness, crew_role_assignment, crew_training_priorities,
        |    crew_equipment_priorities, combat_room_upgrade_priorities,
        |    armor_power_repair_coverage, native_weapon_targeting,
        |    native_repair_healing, boarding_anti_boarding, research_priorities,
        |    resource_investment_efficiency, ai_recommendations
        |         (scoring.py)  --> AccountAssessment
        |
        +--> BattleResult[] (account-isolated) --> build_performance_report
        |                                         (performance.py) --> BattlePerformanceReport
        |
        +--> attach_performance (performance.py)
        |
        +--> build_progression_roadmap (planner.py) --> ProgressionRoadmap
        |
        +--> render_account_report / render_performance_report (report.py)
```

Ship-layout analysis (`sdk/ship_layout.py`) feeds `armor_coverage`,
`power_balance`, `repair_proximity`, `weapon_coverage` into the profile;
crew caps come from `sdk/crew_leveling.py`.

## 4. Hull-specific analysis

Thresholds are hull-aware where supported: `crew_level_cap(hull_level)` derives
from `sdk.crew_leveling.get_crew_level_cap` (ship_level * 4, max 40).
`hull_config(hull_level)` exposes hull-specific caps, metric targets, core
roles, and high-value research. Generic metric targets in `METRIC_TARGETS` are
COMMUNITY-tier hypotheses; exact hull-by-hull values are UNKNOWN until verified
against current official PSS balance (2026 Hull 14 changes noted in the
research digest). Old community guides are treated as hypotheses.

## 5. Evidence and unknown handling

- `EvidenceTier`: VERIFIED / CAPTURED / STATIC / COMMUNITY / UNKNOWN.
- Every `ScoreResult`, `PrioritizedItem`, `HullUpgradeReadiness`,
  `AccountAssessment`, `ProgressionRoadmap`, `BattlePerformanceReport` carries
  `unknowns`, `assumptions`, `reasons`.
- Missing inputs are reported as `UNKNOWN` and never fabricated.
- Suspected loss causes are explicitly labeled inferred, never measured facts.
- `FORBIDDEN_SECRET_KEYS` + `reject_secret_keys` fail closed on any secret key
  anywhere in a payload.
- Account isolation: profile rejects battles for other accounts; crew/room
  uniqueness enforced.

## 6. Fair-play boundary

Offline strategy/decision-support only. The package contains no matchmaking,
battle execution, account control, farming, marketplace trading, scripted
transfers, premium purchases, unattended gameplay, credential/token extraction,
or authenticated production API traffic. Native combat AI is configured manually
by the human account holder. Known-exposed credentials are not used.

## 7. Integration points (reuse, not rebuild)

| Existing module | Reuse |
|---|---|
| `sdk/crew_leveling.py` | level/TP caps via `config.crew_level_cap`/`tp_cap` |
| `sdk/ship_layout.py` | read-only layout metrics feeding profile fields |
| `sdk/game_formulas.py` | pure wiki formulas (no reimplementation) |
| tests (`test_ship_layout`, `test_crew_leveling`, `test_game_formulas`) | regression baseline |

## 8. Tests

- `tests/test_pvp_models.py`, `test_pvp_scoring.py`, `test_pvp_performance.py`,
  `test_pvp_planner.py` (32 tests, analyst lane).
- `tests/test_pvp_sanitized_inputs.py` (8 tests, protocol lane) — sanitized
  fixtures, account isolation, secret rejection, determinism, trophy consistency,
  no network imports.
- `tests/test_pvp_integration.py` (this lane) — end-to-end pipeline over
  sanitized fixtures.

## 9. Open items / unknowns

- Exact hull-by-hull metric targets and trophy thresholds: UNKNOWN (community
  hypotheses pending official 2026 balance verification).
- Native AI rule semantics depend on manual export/screenshot; absent data
  yields UNKNOWN + recommendation to document rules.
- Battle results are human-reported; outcomes and suspected causes are not
  measured facts.
