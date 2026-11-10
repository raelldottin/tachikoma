from __future__ import annotations

"""Offline PvP battle performance metrics."""

from typing import Any, Dict, List, Optional, Sequence

from .models import (
    BattlePerformanceReport,
    BattleResult,
    BattleOutcome,
    BattleRole,
    SuspectedLossCause,
)


def _battles_for_account(battles: Sequence[BattleResult], account: str) -> List[BattleResult]:
    return [b for b in battles if b.account_pseudonym == account]


def attack_win_rate(battles: Sequence[BattleResult], account: str) -> Optional[float]:
    relevant = [b for b in _battles_for_account(battles, account) if b.role == BattleRole.ATTACKER.value]
    if not relevant:
        return None
    wins = sum(1 for b in relevant if b.outcome == BattleOutcome.WIN.value)
    return round(wins / len(relevant), 4)


def defense_win_rate(battles: Sequence[BattleResult], account: str) -> Optional[float]:
    relevant = [b for b in _battles_for_account(battles, account) if b.role == BattleRole.DEFENDER.value]
    if not relevant:
        return None
    wins = sum(1 for b in relevant if b.outcome == BattleOutcome.WIN.value)
    return round(wins / len(relevant), 4)


def trophy_progression(battles: Sequence[BattleResult], account: str) -> List[Dict[str, Any]]:
    out = []
    for b in sorted(_battles_for_account(battles, account), key=lambda x: x.created_at or x.battle_id):
        out.append(
            {
                "battle_id": b.battle_id,
                "role": b.role,
                "trophy_before": b.trophy_before,
                "trophy_after": b.trophy_after,
                "trophy_delta": b.trophy_delta,
                "created_at": b.created_at,
            }
        )
    return out


def losses_by_cause(battles: Sequence[BattleResult], account: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for b in _battles_for_account(battles, account):
        if b.outcome == BattleOutcome.LOSS.value:
            cause = b.suspected_loss_cause or SuspectedLossCause.UNKNOWN.value
            counts[cause] = counts.get(cause, 0) + 1
    return counts


def average_battle_duration(battles: Sequence[BattleResult], account: str,
                            role: Optional[str] = None) -> Optional[float]:
    relevant = _battles_for_account(battles, account)
    if role:
        relevant = [b for b in relevant if b.role == role]
    durs = [b.battle_duration_s for b in relevant if b.battle_duration_s is not None]
    if not durs:
        return None
    return round(sum(durs) / len(durs), 2)


def performance_by_opponent_hull(battles: Sequence[BattleResult], account: str,
                                 role: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    relevant = _battles_for_account(battles, account)
    if role:
        relevant = [b for b in relevant if b.role == role]
    groups: Dict[str, List[BattleResult]] = {}
    for b in relevant:
        key = "UNKNOWN" if b.opponent_hull_level is None else str(b.opponent_hull_level)
        groups.setdefault(key, []).append(b)
    out: Dict[str, Dict[str, Any]] = {}
    for key, grp in groups.items():
        wins = sum(1 for b in grp if b.outcome == BattleOutcome.WIN.value)
        out[key] = {
            "battles": len(grp),
            "wins": wins,
            "win_rate": round(wins / len(grp), 4) if grp else None,
        }
    return out


def config_comparisons(battles: Sequence[BattleResult], account: str) -> List[Dict[str, Any]]:
    relevant = _battles_for_account(battles, account)
    groups: Dict[str, List[BattleResult]] = {}
    for b in relevant:
        key = b.ship_config_version or "unknown"
        groups.setdefault(key, []).append(b)
    out = []
    for version, grp in groups.items():
        wins = sum(1 for b in grp if b.outcome == BattleOutcome.WIN.value)
        out.append(
            {
                "ship_config_version": version,
                "battles": len(grp),
                "wins": wins,
                "win_rate": round(wins / len(grp), 4) if grp else None,
            }
        )
    out.sort(key=lambda x: x["ship_config_version"])
    return out


def build_performance_report(battles: Sequence[BattleResult], account: str) -> BattlePerformanceReport:
    relevant = _battles_for_account(battles, account)
    unknowns = []
    if not relevant:
        unknowns.append("no battle results for account")
    if any(b.opponent_hull_level is None for b in relevant):
        unknowns.append("opponent hull level unknown for some battles")
    if any(b.battle_duration_s is None for b in relevant):
        unknowns.append("battle duration unknown for some battles")
    if any(b.suspected_loss_cause == SuspectedLossCause.UNKNOWN.value for b in relevant if b.outcome == BattleOutcome.LOSS.value):
        unknowns.append("suspected loss cause unknown for some losses")
    notes = (
        "Suspected loss causes are inferred, not measured facts.",
    )
    return BattlePerformanceReport(
        account_pseudonym=account,
        battles_considered=len(relevant),
        attack_win_rate=attack_win_rate(battles, account),
        defense_win_rate=defense_win_rate(battles, account),
        attack_battles=len([b for b in relevant if b.role == BattleRole.ATTACKER.value]),
        defense_battles=len([b for b in relevant if b.role == BattleRole.DEFENDER.value]),
        trophy_progression=tuple(trophy_progression(battles, account)),
        losses_by_cause=losses_by_cause(battles, account),
        average_battle_duration_s=average_battle_duration(battles, account),
        performance_by_opponent_hull=performance_by_opponent_hull(battles, account),
        config_comparisons=tuple(config_comparisons(battles, account)),
        unknowns=tuple(unknowns),
        assumptions=("Battle outcomes are human-reported.",),
        notes=notes,
    )


def attach_performance(assessment, performance: BattlePerformanceReport):
    """Attach a performance report to an assessment (returns new assessment)."""
    from .models import AccountAssessment

    return AccountAssessment(
        profile_pseudonym=assessment.profile_pseudonym,
        hull_level=assessment.hull_level,
        scores=assessment.scores,
        hull_upgrade_readiness=assessment.hull_upgrade_readiness,
        crew_training_priorities=assessment.crew_training_priorities,
        crew_equipment_priorities=assessment.crew_equipment_priorities,
        room_upgrade_priorities=assessment.room_upgrade_priorities,
        research_priorities=assessment.research_priorities,
        ai_recommendations=assessment.ai_recommendations,
        battle_performance=performance,
        unknowns=assessment.unknowns,
        assumptions=assessment.assumptions,
        recommendations=assessment.recommendations,
    )