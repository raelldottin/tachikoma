from __future__ import annotations

"""Offline PvP scoring framework (deterministic, read-only)."""

from typing import List, Optional, Sequence, Tuple

from .config import (
    ROOM_IMPACT_WEIGHTS,
    METRIC_TARGETS,
    CORE_CREW_ROLES,
    HIGH_VALUE_RESEARCH,
    crew_level_cap,
    tp_cap,
)
from .models import (
    AccountAssessment,
    AccountProgressionProfile,
    EvidenceTier,
    HullUpgradeReadiness,
    PrioritizedItem,
    ScoreComponent,
    ScoreResult,
    UNKNOWN,
)

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"


def _score_from_components(components: Sequence[ScoreComponent]) -> Optional[float]:
    available = [c for c in components if c.value is not None]
    if not available:
        return None
    total_weight = sum(c.weight for c in available)
    if total_weight <= 0:
        return None
    weighted = sum((c.value or 0.0) * c.weight for c in available)
    return round(weighted / total_weight, 2)


def _unknowns_for_metrics(profile: AccountProgressionProfile) -> List[str]:
    out = []
    if profile.armor_coverage is None:
        out.append("armor_coverage unknown")
    if profile.power_balance is None:
        out.append("power_balance unknown")
    if profile.repair_proximity is None:
        out.append("repair_proximity unknown")
    if profile.weapon_coverage is None:
        out.append("weapon_coverage unknown")
    return out


def hull_upgrade_readiness(profile: AccountProgressionProfile) -> HullUpgradeReadiness:
    blockers: List[str] = []
    unknowns: List[str] = []
    reasons: List[str] = []
    cap = crew_level_cap(profile.hull_level)

    # Crew level cap
    underleveled_crew = [c for c in profile.crew if c.level < cap]
    if underleveled_crew:
        blockers.append(f"crew below level cap ({len(underleveled_crew)} crew)")
        reasons.append("Crew must reach level cap before hull upgrade (conservative).")

    # Rooms below max level
    rooms_with_max = [r for r in profile.rooms if r.max_level is not None]
    rooms_unknown_max = [r for r in profile.rooms if r.max_level is None]
    underleveled_rooms = [r for r in rooms_with_max if r.level < (r.max_level or 0)]
    if underleveled_rooms:
        blockers.append(f"rooms below max level ({len(underleveled_rooms)} rooms)")
    if rooms_unknown_max:
        unknowns.append(f"max level unknown for {len(rooms_unknown_max)} rooms")
    if rooms_unknown_max:
        reasons.append("Room max levels unknown; cannot confirm readiness.")

    # Resources
    if not profile.resources:
        unknowns.append("resource availability unknown")
        reasons.append("Resource availability not provided; readiness unverified.")
    else:
        # Conservative: require non-negative availability; if any negative, blocker
        for key, value in profile.resources.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
                blockers.append(f"insufficient resource: {key}")

    # Critical AI gaps
    if not profile.native_ai_rules:
        blockers.append("native AI rules not documented")
        unknowns.append("native AI rules unknown")

    ready = len(blockers) == 0 and len(unknowns) == 0
    score = 100.0 if ready else max(0.0, 100.0 - 15.0 * len(blockers) - 5.0 * len(unknowns))
    if unknowns:
        score = min(score, 70.0)
    return HullUpgradeReadiness(
        ready=ready,
        blockers=tuple(blockers),
        score=round(score, 2),
        unknowns=tuple(unknowns),
        assumptions=("Hull upgrade requires all rooms/crew at cap (community consensus).",),
        reasons=tuple(reasons + [f"Evaluated {len(profile.crew)} crew, {len(profile.rooms)} rooms."]),
    )


def crew_role_assignment(profile: AccountProgressionProfile) -> ScoreResult:
    present_roles = {c.role for c in profile.crew}
    covered = [r for r in CORE_CREW_ROLES if r in present_roles]
    missing = [r for r in CORE_CREW_ROLES if r not in present_roles]
    if not profile.crew:
        return ScoreResult(
            name="crew_role_assignment",
            score=None,
            unknowns=("crew roster unknown",),
            assumptions=("Core roles are community-derived hypotheses.",),
            reasons=("No crew provided.",),
            evidence_tier=EvidenceTier.UNKNOWN.value,
        )
    score = 100.0 * len(covered) / len(CORE_CREW_ROLES)
    reasons = [f"Covered roles: {', '.join(covered) or 'none'}."]
    if missing:
        reasons.append(f"Missing roles: {', '.join(missing)}.")
    unknowns = []
    if any(c.role == "general" for c in profile.crew):
        unknowns.append("some crew have unspecified roles")
    return ScoreResult(
        name="crew_role_assignment",
        score=round(score, 2),
        components=tuple(ScoreComponent(name=r, value=100.0 if r in present_roles else 0.0) for r in CORE_CREW_ROLES),
        unknowns=tuple(unknowns),
        assumptions=("Core role set is a community hypothesis.",),
        reasons=tuple(reasons),
        evidence_tier=EvidenceTier.COMMUNITY.value,
    )


def crew_training_priorities(profile: AccountProgressionProfile) -> Tuple[PrioritizedItem, ...]:
    cap = crew_level_cap(profile.hull_level)
    items: List[PrioritizedItem] = []
    for crew in profile.crew:
        tp_target = tp_cap(crew.rarity, crew.name)
        tp_deficit = max(0, tp_target - crew.training_points)
        level_gap = max(0, cap - crew.level)
        if tp_deficit <= 0 and level_gap <= 0:
            continue
        priority = PRIORITY_HIGH if (level_gap > 0 or tp_deficit >= tp_target * 0.5) else PRIORITY_MEDIUM
        items.append(
            PrioritizedItem(
                item_id=f"crew:{crew.name}",
                title=f"Train {crew.name}",
                priority=priority,
                score_impact=float(tp_deficit),
                reason=f"Level {crew.level}/{cap}; TP {crew.training_points}/{tp_target} (deficit {tp_deficit}).",
                evidence_tier=EvidenceTier.STATIC.value,
                unknowns=() if crew.level is not None else ("crew level unknown",),
                estimated_resources={"gas": UNKNOWN, "xp": UNKNOWN},
            )
        )
    items.sort(key=lambda x: (0 if x.priority == PRIORITY_HIGH else 1, -(x.score_impact or 0)))
    return tuple(items)


def crew_equipment_priorities(profile: AccountProgressionProfile) -> Tuple[PrioritizedItem, ...]:
    items: List[PrioritizedItem] = []
    for crew in profile.crew:
        if not crew.equipment or crew.equipment.strip().lower() in ("", "none", UNKNOWN.lower()):
            items.append(
                PrioritizedItem(
                    item_id=f"equip:{crew.name}",
                    title=f"Equip {crew.name}",
                    priority=PRIORITY_MEDIUM,
                    reason=f"No equipment recorded for {crew.name} (role {crew.role}).",
                    evidence_tier=EvidenceTier.UNKNOWN.value,
                    unknowns=("equipment availability unknown",),
                )
            )
    return tuple(items)


def combat_room_upgrade_priorities(profile: AccountProgressionProfile) -> Tuple[PrioritizedItem, ...]:
    items: List[PrioritizedItem] = []
    for room in profile.rooms:
        if room.max_level is None:
            items.append(
                PrioritizedItem(
                    item_id=f"room:{room.design_id}:{room.position_row},{room.position_col}",
                    title=f"Upgrade {room.name}",
                    priority=PRIORITY_LOW,
                    reason=f"Level {room.level}; max level unknown (cannot confirm upgrade path).",
                    evidence_tier=EvidenceTier.UNKNOWN.value,
                    unknowns=("room max level unknown",),
                )
            )
            continue
        if room.level >= room.max_level:
            continue
        weight = ROOM_IMPACT_WEIGHTS.get(room.category, 0.5)
        gap = room.max_level - room.level
        impact = round(weight * gap * 10, 2)
        priority = PRIORITY_HIGH if weight >= 0.8 and gap >= 2 else PRIORITY_MEDIUM if weight >= 0.5 else PRIORITY_LOW
        items.append(
            PrioritizedItem(
                item_id=f"room:{room.design_id}:{room.position_row},{room.position_col}",
                title=f"Upgrade {room.name} ({room.category})",
                priority=priority,
                score_impact=impact,
                reason=f"Level {room.level}/{room.max_level}; category weight {weight}.",
                evidence_tier=room.evidence_tier,
                estimated_resources={"minerals": UNKNOWN, "gas": UNKNOWN},
            )
        )
    items.sort(key=lambda x: ({"high": 0, "medium": 1, "low": 2}[x.priority], -(x.score_impact or 0)))
    return tuple(items)


def armor_power_repair_coverage(profile: AccountProgressionProfile) -> ScoreResult:
    components = [
        ScoreComponent(name="armor_coverage", value=profile.armor_coverage, weight=1.0),
        ScoreComponent(name="power_balance", value=profile.power_balance, weight=1.0),
        ScoreComponent(name="repair_proximity", value=profile.repair_proximity, weight=1.0),
        ScoreComponent(name="weapon_coverage", value=profile.weapon_coverage, weight=0.8),
    ]
    unknowns = _unknowns_for_metrics(profile)
    score = _score_from_components(components)
    reasons = []
    for metric, target in METRIC_TARGETS.items():
        val = getattr(profile, metric, None)
        if val is not None and val < target:
            reasons.append(f"{metric} {val} below target {target}.")
    if not reasons and score is not None:
        reasons.append("Layout metrics meet conservative targets.")
    if score is None:
        reasons.append("No layout metrics provided.")
    return ScoreResult(
        name="armor_power_repair_coverage",
        score=score,
        components=tuple(components),
        unknowns=tuple(unknowns),
        assumptions=("Targets are conservative community hypotheses.",),
        reasons=tuple(reasons),
        evidence_tier=EvidenceTier.COMMUNITY.value if score is not None else EvidenceTier.UNKNOWN.value,
    )


def _ai_groups(profile: AccountProgressionProfile) -> set:
    return {r.group.lower() for r in profile.native_ai_rules}


def _ai_has(profile: AccountProgressionProfile, *keywords: str) -> bool:
    for rule in profile.native_ai_rules:
        text = " ".join(rule.conditions + rule.actions + (rule.target_type,)).lower()
        if all(k in text for k in keywords):
            return True
    return False


def native_weapon_targeting(profile: AccountProgressionProfile) -> ScoreResult:
    if not profile.native_ai_rules:
        return ScoreResult(
            name="native_weapon_targeting",
            score=None,
            unknowns=("native AI rules unknown",),
            reasons=("No AI rules provided.",),
            evidence_tier=EvidenceTier.UNKNOWN.value,
        )
    components = [
        ScoreComponent(name="weapon_group_present", value=100.0 if _ai_has(profile, "weapon") or "weapon" in _ai_groups(profile) else 0.0),
        ScoreComponent(name="focus_fire", value=100.0 if _ai_has(profile, "focus") or _ai_has(profile, "target") else 0.0),
        ScoreComponent(name="two_phase", value=100.0 if _ai_has(profile, "hull") else 0.0),
    ]
    score = _score_from_components(components)
    reasons = []
    if not _ai_has(profile, "focus") and not _ai_has(profile, "target"):
        reasons.append("No focus-fire/targeting rule detected.")
    if not _ai_has(profile, "hull"):
        reasons.append("No two-phase (hull threshold) targeting detected.")
    if score is not None and score >= 80:
        reasons.append("Weapon targeting AI appears reasonably configured.")
    return ScoreResult(
        name="native_weapon_targeting",
        score=score,
        components=tuple(components),
        unknowns=(),
        assumptions=("AI availability depends on Python research level.",),
        reasons=tuple(reasons or ("Evaluated native AI rules.",)),
        evidence_tier=EvidenceTier.STATIC.value,
    )


def native_repair_healing(profile: AccountProgressionProfile) -> ScoreResult:
    if not profile.native_ai_rules and not profile.rooms:
        return ScoreResult(
            name="native_repair_healing",
            score=None,
            unknowns=("AI and room data unknown",),
            reasons=("No data provided.",),
            evidence_tier=EvidenceTier.UNKNOWN.value,
        )
    has_repair_room = any(r.category == "repair" for r in profile.rooms)
    has_repair_ai = _ai_has(profile, "repair") or "repair" in _ai_groups(profile)
    has_medical = any("med" in r.name.lower() or "medical" in r.name.lower() for r in profile.rooms)
    components = [
        ScoreComponent(name="repair_room", value=100.0 if has_repair_room else 0.0),
        ScoreComponent(name="repair_ai", value=100.0 if has_repair_ai else 0.0),
        ScoreComponent(name="medical_room", value=100.0 if has_medical else 50.0 if has_repair_room else 0.0),
    ]
    score = _score_from_components(components)
    reasons = []
    if not has_repair_room:
        reasons.append("No repair room detected.")
    if not has_repair_ai:
        reasons.append("No repair AI rule detected.")
    if not reasons:
        reasons.append("Repair/healing coverage present.")
    return ScoreResult(
        name="native_repair_healing",
        score=score,
        components=tuple(components),
        unknowns=(),
        assumptions=("Repair proximity from ship_layout not re-derived here.",),
        reasons=tuple(reasons),
        evidence_tier=EvidenceTier.STATIC.value,
    )


def boarding_anti_boarding(profile: AccountProgressionProfile) -> ScoreResult:
    has_teleport = any("teleport" in r.name.lower() for r in profile.rooms)
    security_rooms = [r for r in profile.rooms if r.category == "security"]
    has_gas_trap = any("gas" in r.name.lower() or "trap" in r.name.lower() for r in profile.rooms)
    has_mines = any("mine" in r.name.lower() for r in profile.rooms)
    components = [
        ScoreComponent(name="teleport", value=100.0 if has_teleport else 0.0, weight=0.8),
        ScoreComponent(name="security_rooms", value=min(100.0, len(security_rooms) * 33.3), weight=1.0),
        ScoreComponent(name="gas_traps", value=100.0 if has_gas_trap else 0.0, weight=0.9),
        ScoreComponent(name="mines", value=100.0 if has_mines else 0.0, weight=0.7),
    ]
    score = _score_from_components(components)
    reasons = []
    if not has_teleport:
        reasons.append("No teleport room (boarding disabled).")
    if not security_rooms:
        reasons.append("No security/anti-boarder rooms detected.")
    if not reasons:
        reasons.append("Boarding/anti-boarding facilities present.")
    unknowns = []
    if not profile.rooms:
        unknowns.append("room list unknown")
    return ScoreResult(
        name="boarding_anti_boarding",
        score=score if profile.rooms else None,
        components=tuple(components),
        unknowns=tuple(unknowns),
        assumptions=("Post-Hull-14 cloak nerfs treated as current meta.",),
        reasons=tuple(reasons),
        evidence_tier=EvidenceTier.COMMUNITY.value,
    )


def research_priorities(profile: AccountProgressionProfile) -> Tuple[PrioritizedItem, ...]:
    completed = {r.name.lower(): r for r in profile.research}
    items: List[PrioritizedItem] = []
    for name in HIGH_VALUE_RESEARCH:
        r = completed.get(name.lower())
        if r is None or not r.completed or (r.max_level and r.level < r.max_level):
            level_str = f"{r.level}/{r.max_level}" if r else "0/unknown"
            items.append(
                PrioritizedItem(
                    item_id=f"research:{name}",
                    title=f"Research/upgrade {name}",
                    priority=PRIORITY_HIGH if name.startswith("Python") else PRIORITY_MEDIUM,
                    reason=f"Current level {level_str}; high PvP value (AI/reinforcement).",
                    evidence_tier=EvidenceTier.COMMUNITY.value,
                    unknowns=() if r is not None else ("research state unknown",),
                )
            )
    # Any research items provided but incomplete
    for r in profile.research:
        if not r.completed and r.name.lower() not in {n.lower() for n in HIGH_VALUE_RESEARCH}:
            items.append(
                PrioritizedItem(
                    item_id=f"research:{r.name}",
                    title=f"Complete research {r.name}",
                    priority=PRIORITY_LOW,
                    reason=f"Research incomplete ({r.level}/{r.max_level}).",
                    evidence_tier=r.evidence_tier,
                )
            )
    return tuple(items)


def resource_investment_efficiency(profile: AccountProgressionProfile) -> ScoreResult:
    if not profile.resources:
        return ScoreResult(
            name="resource_investment_efficiency",
            score=None,
            unknowns=("resources unknown",),
            reasons=("No resource data provided; efficiency cannot be computed.",),
            evidence_tier=EvidenceTier.UNKNOWN.value,
        )
    # Simple efficiency: count of known positive resources; no invented costs.
    known = {k: v for k, v in profile.resources.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if not known:
        return ScoreResult(
            name="resource_investment_efficiency",
            score=None,
            unknowns=("no numeric resources",),
            reasons=("Resources present but non-numeric.",),
            evidence_tier=EvidenceTier.UNKNOWN.value,
        )
    score = 50.0 + min(50.0, len(known) * 10.0)
    return ScoreResult(
        name="resource_investment_efficiency",
        score=round(score, 2),
        components=tuple(ScoreComponent(name=k, value=float(v)) for k, v in known.items()),
        unknowns=("upgrade costs not provided; efficiency is input-coverage only",),
        assumptions=("Cost tables not embedded; efficiency cannot be computed precisely.",),
        reasons=("Resource inputs recorded; cost-effectiveness requires cost data.",),
        evidence_tier=EvidenceTier.UNKNOWN.value,
    )


def ai_recommendations(profile: AccountProgressionProfile) -> Tuple[str, ...]:
    recs: List[str] = []
    if not profile.native_ai_rules:
        recs.append("Document native AI rules; baseline unknown.")
        return tuple(recs)
    if not _ai_has(profile, "focus") and not _ai_has(profile, "target"):
        recs.append("Add focus-fire targeting by room type (e.g., lasers/shields).")
    if not _ai_has(profile, "hull"):
        recs.append("Add two-phase targeting: switch to hull-damage target below 100% hull.")
    if not _ai_has(profile, "repair"):
        recs.append("Add repair crew AI targeting damaged critical rooms.")
    groups = _ai_groups(profile)
    if "weapon" not in groups:
        recs.append("Add a weapon targeting group.")
    if "repair" not in groups:
        recs.append("Add a repair/healing group.")
    return tuple(recs)


def assess_account(profile: AccountProgressionProfile) -> "AccountAssessment":
    from .models import AccountAssessment

    scores = {
        "crew_role_assignment": crew_role_assignment(profile),
        "armor_power_repair_coverage": armor_power_repair_coverage(profile),
        "native_weapon_targeting": native_weapon_targeting(profile),
        "native_repair_healing": native_repair_healing(profile),
        "boarding_anti_boarding": boarding_anti_boarding(profile),
        "resource_investment_efficiency": resource_investment_efficiency(profile),
    }
    readiness = hull_upgrade_readiness(profile)
    training = crew_training_priorities(profile)
    equipment = crew_equipment_priorities(profile)
    rooms = combat_room_upgrade_priorities(profile)
    research = research_priorities(profile)
    ai = ai_recommendations(profile)

    unknowns: list[str] = list(profile.unknowns)
    for src in (readiness, training, equipment, rooms, research, ai):
        for u in getattr(src, "unknowns", ()) or ():
            if u not in unknowns:
                unknowns.append(u)
    for sc in scores.values():
        for u in getattr(sc, "unknowns", ()) or ():
            if u not in unknowns:
                unknowns.append(u)

    assumptions: list[str] = list(profile.assumptions)
    for src in (readiness, training, equipment, rooms, research, ai):
        for a in getattr(src, "assumptions", ()) or ():
            if a not in assumptions:
                assumptions.append(a)

    assessment = AccountAssessment(
        profile_pseudonym=profile.pseudonym,
        hull_level=profile.hull_level,
        scores=scores,
        hull_upgrade_readiness=readiness,
        crew_training_priorities=training,
        crew_equipment_priorities=equipment,
        room_upgrade_priorities=rooms,
        research_priorities=research,
        ai_recommendations=ai,
        unknowns=tuple(unknowns),
        assumptions=tuple(assumptions),
    )
    return assessment