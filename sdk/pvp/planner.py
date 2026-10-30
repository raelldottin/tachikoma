from __future__ import annotations

"""Offline progression roadmap generation."""

from typing import List

from .models import (
    AccountProgressionProfile,
    ProgressionRoadmap,
    UNKNOWN,
)
from .scoring import (
    assess_account,
)


def _safe_list(items) -> List[str]:
    return [str(x) for x in items]


def build_progression_roadmap(profile: AccountProgressionProfile) -> ProgressionRoadmap:
    assessment = assess_account(profile)
    immediate: List[str] = []
    next7: List[str] = []
    next30: List[str] = []

    # Immediate fixes from low scores / blockers
    readiness = assessment.hull_upgrade_readiness
    if readiness is not None and not readiness.ready:
        for b in readiness.blockers:
            immediate.append(f"Fix blocker: {b}")
    for rec in assessment.ai_recommendations:
        immediate.append(f"AI: {rec}")

    # Hull-level metrics
    for metric in ("armor_coverage", "power_balance", "repair_proximity", "weapon_coverage"):
        val = getattr(profile, metric, None)
        if val is not None and val < 60:
            immediate.append(f"Improve {metric} (current {val}).")

    # 7 days: top crew training + room upgrades
    for item in list(assessment.crew_training_priorities)[:3]:
        next7.append(f"{item.title} — {item.reason}")
    for item in list(assessment.room_upgrade_priorities)[:3]:
        next7.append(f"{item.title} — {item.reason}")
    for item in list(assessment.research_priorities)[:2]:
        next7.append(f"{item.title} — {item.reason}")

    # 30 days: research + boarding + hull criteria
    for item in list(assessment.research_priorities):
        next30.append(f"Research: {item.title}")
    boarding_score = assessment.scores.get("boarding_anti_boarding")
    if boarding_score is None or (boarding_score.score or 0) < 70:
        next30.append("Configure boarding/anti-boarding (teleport, security, gas traps, mines).")
    next30.append("Log 5+ PvP battles for baseline performance.")

    hull_criteria = [
        "All combat rooms at max level for current hull.",
        "All crew at level cap.",
        "Core crew roles covered.",
        "Native AI targeting and repair rules configured.",
        "Resource availability confirmed for next hull.",
        "Trophy plateau or readiness per hull guidance.",
    ]
    if readiness is not None and readiness.unknowns:
        hull_criteria.append("Resolve unknowns: " + "; ".join(readiness.unknowns))

    highest_crew = [f"{i.title}: {i.reason}" for i in assessment.crew_training_priorities[:5]]
    highest_rooms = [f"{i.title}: {i.reason}" for i in assessment.room_upgrade_priorities[:5]]
    highest_rooms += [f"Research: {i.title}" for i in assessment.research_priorities[:3]]

    next_ai = list(assessment.ai_recommendations) + [
        "Test one AI change at a time; compare battle logs (A/B)."
    ]

    # Resource requirements
    resources = dict(profile.resources) if profile.resources else {}
    if not resources:
        resources = {"status": UNKNOWN}
    else:
        resources = dict(resources)
        resources.setdefault("note", "Costs not embedded; provided resources only.")

    unknowns = list(profile.unknowns) + list(assessment.unknowns if hasattr(assessment, "unknowns") else [])
    # dedup
    seen = set()
    uniq = []
    for u in unknowns:
        if u not in seen:
            seen.add(u)
            uniq.append(u)

    return ProgressionRoadmap(
        account_pseudonym=profile.pseudonym,
        immediate_fixes=tuple(immediate or ("No immediate fixes identified from available data.",)),
        next_7_days=tuple(next7 or ("Collect missing data; no actions computed.",)),
        next_30_days=tuple(next30 or ("Continue logging battles and upgrades.",)),
        hull_advance_criteria=tuple(hull_criteria),
        highest_value_crew=tuple(highest_crew or ("No crew gaps identified.",)),
        highest_value_rooms_research=tuple(highest_rooms or ("No room/research gaps identified.",)),
        next_ai_changes_to_test=tuple(next_ai),
        expected_resource_requirements=resources,
        unknowns=tuple(uniq),
        assumptions=profile.assumptions,
    )