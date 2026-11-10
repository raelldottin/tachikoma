from __future__ import annotations

"""Read-only PvP reporting helpers."""


from .models import AccountProgressionProfile, ProgressionRoadmap, AccountAssessment
from .performance import BattlePerformanceReport


def render_account_report(profile: AccountProgressionProfile,
                          assessment: AccountAssessment,
                          roadmap: ProgressionRoadmap,
                          performance: BattlePerformanceReport | None = None) -> str:
    lines: list[str] = []
    lines.append(f"# PvP Account Report: {profile.pseudonym}")
    lines.append("")
    lines.append(f"- Hull level: {profile.hull_level}")
    lines.append(f"- Trophies: {profile.trophies if profile.trophies is not None else 'UNKNOWN'}")
    lines.append(f"- Ship config version: {profile.ship_config_version or 'UNKNOWN'}")
    lines.append("")
    lines.append("## Scores")
    for name, score in (assessment.scores or {}).items():
        lines.append(f"- {name}: {score.score if score.score is not None else 'UNKNOWN'}")
    if assessment.hull_upgrade_readiness:
        r = assessment.hull_upgrade_readiness
        lines.append(f"- hull_upgrade_readiness: ready={r.ready}, score={r.score}")
        for b in r.blockers:
            lines.append(f"  - blocker: {b}")
    lines.append("")
    lines.append("## Immediate fixes")
    for x in roadmap.immediate_fixes:
        lines.append(f"- {x}")
    lines.append("")
    lines.append("## Next 7 days")
    for x in roadmap.next_7_days:
        lines.append(f"- {x}")
    lines.append("")
    lines.append("## Next 30 days")
    for x in roadmap.next_30_days:
        lines.append(f"- {x}")
    lines.append("")
    lines.append("## Hull advance criteria")
    for x in roadmap.hull_advance_criteria:
        lines.append(f"- {x}")
    lines.append("")
    lines.append("## Unknowns")
    for x in set(list(profile.unknowns) + list(assessment.unknowns) + list(roadmap.unknowns)):
        lines.append(f"- {x}")
    lines.append("")
    lines.append("## Assumptions")
    for x in set(list(profile.assumptions) + list(roadmap.assumptions)):
        lines.append(f"- {x}")
    if performance is not None:
        lines.append("")
        lines.append("## PvP performance")
        lines.append(f"- Battles: {performance.battles_considered}")
        lines.append(f"- Attack win rate: {performance.attack_win_rate if performance.attack_win_rate is not None else 'UNKNOWN'}")
        lines.append(f"- Defense win rate: {performance.defense_win_rate if performance.defense_win_rate is not None else 'UNKNOWN'}")
        lines.append("- Loss causes (suspected/inferred): " + (", ".join(f"{k}={v}" for k, v in performance.losses_by_cause.items()) or "none"))
    lines.append("")
    lines.append("Note: suspected loss causes are inferred, not measured facts.")
    return "\n".join(lines)


def render_performance_report(performance: BattlePerformanceReport) -> str:
    lines = [f"# PvP Performance Report: {performance.account_pseudonym}"]
    lines.append(f"- Battles considered: {performance.battles_considered}")
    lines.append(f"- Attack win rate: {performance.attack_win_rate}")
    lines.append(f"- Defense win rate: {performance.defense_win_rate}")
    lines.append(f"- Average battle duration (s): {performance.average_battle_duration_s}")
    lines.append("- Losses by suspected cause (inferred):")
    for k, v in performance.losses_by_cause.items():
        lines.append(f"  - {k}: {v}")
    lines.append("- Performance by opponent hull:")
    for hull, data in performance.performance_by_opponent_hull.items():
        lines.append(f"  - hull {hull}: {data}")
    return "\n".join(lines)