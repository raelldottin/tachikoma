from __future__ import annotations

"""Hull-specific configuration and conservative thresholds for PvP.

All thresholds are hypotheses (COMMUNITY/STATIC tiers) until verified.
"""

from typing import Any, Dict, Optional

from sdk.crew_leveling import get_tp_cap, get_crew_level_cap


def crew_level_cap(hull_level: int) -> int:
    return get_crew_level_cap(hull_level)


def tp_cap(rarity: int, name: str = "", is_captain: bool = False) -> int:
    return get_tp_cap(rarity, name, is_captain)


# Room categories with upgrade impact weights (higher = more valuable).
ROOM_IMPACT_WEIGHTS: Dict[str, float] = {
    "reactor": 1.0,
    "weapon": 0.95,
    "shield": 0.9,
    "repair": 0.85,
    "teleport": 0.85,
    "security": 0.8,
    "lab": 0.75,
    "training": 0.7,
    "bedroom": 0.6,
    "storage": 0.4,
    "corridor": 0.2,
    "armor": 0.3,
    "other": 0.5,
}

# Conservative hull-upgrade readiness thresholds by metric (COMMUNITY).
METRIC_TARGETS: Dict[str, float] = {
    "armor_coverage": 60.0,
    "power_balance": 70.0,
    "repair_proximity": 60.0,
    "weapon_coverage": 60.0,
}

# Expected core crew roles for a competitive account.
CORE_CREW_ROLES = ("weapon_boost", "shield_boost", "repair", "boarding", "boarding_defense")

# Research items of high PvP value (names are hypotheses).
HIGH_VALUE_RESEARCH = (
    "Python 1",
    "Python 2",
    "Python 3",
    "Room Reinforcement",
    "Lab",
)

# Hull-specific minimum trophy guidance (COMMUNITY, UNKNOWN exact).
def min_trophies_for_hull(hull_level: int) -> Optional[int]:
    # Conservative: unknown exact; return None to mark UNKNOWN.
    return None


def hull_config(hull_level: int) -> Dict[str, Any]:
    return {
        "hull_level": hull_level,
        "crew_level_cap": crew_level_cap(hull_level),
        "metric_targets": dict(METRIC_TARGETS),
        "core_roles": list(CORE_CREW_ROLES),
        "high_value_research": list(HIGH_VALUE_RESEARCH),
        "evidence_tier": "COMMUNITY",
    }