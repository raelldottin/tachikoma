from __future__ import annotations

"""Offline PvP domain models for Pixel Starships account progression.

Pure dataclasses. No network, no credentials, no mutation semantics.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple
import hashlib
import json

UNKNOWN = "UNKNOWN"


class EvidenceTier(str, Enum):
    VERIFIED = "VERIFIED"
    CAPTURED = "CAPTURED"
    STATIC = "STATIC"
    COMMUNITY = "COMMUNITY"
    UNKNOWN = "UNKNOWN"


class BattleOutcome(str, Enum):
    WIN = "win"
    LOSS = "loss"
    DRAW = "draw"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class BattleRole(str, Enum):
    ATTACKER = "attacker"
    DEFENDER = "defender"


class BattleStrategy(str, Enum):
    FOCUS_FIRE = "focus_fire"
    REACTOR_SHIELD_DEVASTATION = "reactor_shield_devastation"
    WEAPON_DEVASTATION = "weapon_devastation"
    BOARDING = "boarding"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


class SuspectedLossCause(str, Enum):
    BOARDING = "boarding"
    FOCUS_FIRE = "focus_fire"
    POWER_LOSS = "power_loss"
    REPAIR_FAILURE = "repair_failure"
    CREW_DEATH = "crew_death"
    AMMO_DEPLETION = "ammo_depletion"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class PvpInputError(ValueError):
    """Raised when PvP input validation fails (fail closed)."""


FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "accesstoken",
        "refreshtoken",
        "password",
        "passwd",
        "secret",
        "token",
        "apikey",
        "api_key",
        "clientkey",
        "client_key",
        "devicekey",
        "device_key",
        "credentials",
        "checksum",
    }
)


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PvpInputError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_int(value: Any, field_name: str, *, min_value: Optional[int] = None,
                  max_value: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PvpInputError(f"{field_name} must be an int")
    if min_value is not None and value < min_value:
        raise PvpInputError(f"{field_name} must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise PvpInputError(f"{field_name} must be <= {max_value}")
    return value


def reject_secret_keys(payload: Any, path: str = "") -> None:
    """Fail closed if any forbidden secret key appears anywhere in payload."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            key_s = str(key)
            if key_s.lower() in FORBIDDEN_SECRET_KEYS:
                raise PvpInputError(f"forbidden secret key at {path or '<root>'}: {key_s}")
            reject_secret_keys(value, f"{path}.{key_s}" if path else key_s)
    elif isinstance(payload, (list, tuple)):
        for idx, item in enumerate(payload):
            reject_secret_keys(item, f"{path}[{idx}]")


@dataclass(frozen=True)
class CrewMember:
    name: str
    level: int
    rarity: int
    training_points: int = 0
    equipment: str = ""
    role: str = "general"
    assigned_room: str = ""
    abilities: Dict[str, Any] = field(default_factory=dict)
    evidence_tier: str = EvidenceTier.UNKNOWN.value

    def __post_init__(self) -> None:
        _require_str(self.name, "crew.name")
        _require_int(self.level, "crew.level", min_value=0, max_value=40)
        _require_int(self.rarity, "crew.rarity", min_value=1, max_value=7)
        _require_int(self.training_points, "crew.training_points", min_value=0)
        if self.evidence_tier not in {e.value for e in EvidenceTier}:
            raise PvpInputError("crew.evidence_tier invalid")
        reject_secret_keys(self.abilities, "crew.abilities")


@dataclass(frozen=True)
class CombatRoom:
    design_id: str
    name: str
    category: str = "other"
    level: int = 0
    max_level: Optional[int] = None
    position_row: int = 0
    position_col: int = 0
    power: int = 0
    hp: int = 0
    is_powered: bool = True
    ammo_config: str = ""
    evidence_tier: str = EvidenceTier.UNKNOWN.value

    def __post_init__(self) -> None:
        _require_str(self.design_id, "room.design_id")
        _require_str(self.name, "room.name")
        _require_int(self.level, "room.level", min_value=0)
        _require_int(self.power, "room.power", min_value=0)
        _require_int(self.hp, "room.hp", min_value=0)
        if self.max_level is not None:
            _require_int(self.max_level, "room.max_level", min_value=0)
            if self.max_level < self.level:
                raise PvpInputError("room.max_level must be >= room.level")
        if self.evidence_tier not in {e.value for e in EvidenceTier}:
            raise PvpInputError("room.evidence_tier invalid")


@dataclass(frozen=True)
class ResearchItem:
    name: str
    level: int = 0
    max_level: int = 0
    completed: bool = False
    evidence_tier: str = EvidenceTier.UNKNOWN.value

    def __post_init__(self) -> None:
        _require_str(self.name, "research.name")
        _require_int(self.level, "research.level", min_value=0)
        _require_int(self.max_level, "research.max_level", min_value=0)
        if self.max_level and self.level > self.max_level:
            raise PvpInputError("research.level must be <= research.max_level")
        if self.evidence_tier not in {e.value for e in EvidenceTier}:
            raise PvpInputError("research.evidence_tier invalid")


@dataclass(frozen=True)
class NativeAIRule:
    group: str
    conditions: Tuple[str, ...] = field(default_factory=tuple)
    actions: Tuple[str, ...] = field(default_factory=tuple)
    target_type: str = ""
    evidence_tier: str = EvidenceTier.UNKNOWN.value

    def __post_init__(self) -> None:
        _require_str(self.group, "ai.group")
        if self.evidence_tier not in {e.value for e in EvidenceTier}:
            raise PvpInputError("ai.evidence_tier invalid")


@dataclass(frozen=True)
class BattleResult:
    battle_id: str
    account_pseudonym: str
    role: str
    opponent_pseudonym: str = ""
    opponent_hull_level: Optional[int] = None
    outcome: str = BattleOutcome.UNKNOWN.value
    trophy_delta: int = 0
    battle_duration_s: Optional[int] = None
    major_rooms_lost: Tuple[str, ...] = field(default_factory=tuple)
    battle_strategy: str = BattleStrategy.UNKNOWN.value
    suspected_loss_cause: str = SuspectedLossCause.UNKNOWN.value
    ship_config_version: str = ""
    account_hull_level: Optional[int] = None
    trophy_before: Optional[int] = None
    trophy_after: Optional[int] = None
    notes: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        _require_str(self.battle_id, "battle.battle_id")
        _require_str(self.account_pseudonym, "battle.account_pseudonym")
        if self.role not in {r.value for r in BattleRole}:
            raise PvpInputError("battle.role invalid")
        if self.outcome not in {o.value for o in BattleOutcome}:
            raise PvpInputError("battle.outcome invalid")
        if self.battle_strategy not in {s.value for s in BattleStrategy}:
            raise PvpInputError("battle.battle_strategy invalid")
        if self.suspected_loss_cause not in {c.value for c in SuspectedLossCause}:
            raise PvpInputError("battle.suspected_loss_cause invalid")
        if self.opponent_hull_level is not None:
            _require_int(self.opponent_hull_level, "battle.opponent_hull_level", min_value=1, max_value=20)
        if self.account_hull_level is not None:
            _require_int(self.account_hull_level, "battle.account_hull_level", min_value=1, max_value=20)
        if self.battle_duration_s is not None:
            _require_int(self.battle_duration_s, "battle.battle_duration_s", min_value=0)
        if self.trophy_before is not None:
            _require_int(self.trophy_before, "battle.trophy_before")
        if self.trophy_after is not None:
            _require_int(self.trophy_after, "battle.trophy_after")
        if self.trophy_delta != 0 and self.trophy_before is not None and self.trophy_after is not None:
            if self.trophy_after - self.trophy_before != self.trophy_delta:
                raise PvpInputError("battle.trophy_delta must equal trophy_after - trophy_before")
        reject_secret_keys({"notes": self.notes}, "battle")


@dataclass(frozen=True)
class AccountProgressionProfile:
    pseudonym: str
    hull_level: int
    trophies: Optional[int] = None
    peak_trophies: Optional[int] = None
    crew: Tuple[CrewMember, ...] = field(default_factory=tuple)
    rooms: Tuple[CombatRoom, ...] = field(default_factory=tuple)
    research: Tuple[ResearchItem, ...] = field(default_factory=tuple)
    armor_coverage: Optional[float] = None
    power_balance: Optional[float] = None
    repair_proximity: Optional[float] = None
    weapon_coverage: Optional[float] = None
    ammo_craft_config: Dict[str, Any] = field(default_factory=dict)
    native_ai_rules: Tuple[NativeAIRule, ...] = field(default_factory=tuple)
    resources: Dict[str, Any] = field(default_factory=dict)
    recent_battles: Tuple[BattleResult, ...] = field(default_factory=tuple)
    ship_config_version: str = ""
    assumptions: Tuple[str, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    evidence_tier: str = EvidenceTier.UNKNOWN.value

    def __post_init__(self) -> None:
        _require_str(self.pseudonym, "profile.pseudonym")
        _require_int(self.hull_level, "profile.hull_level", min_value=1, max_value=20)
        if self.trophies is not None:
            _require_int(self.trophies, "profile.trophies")
        if self.peak_trophies is not None:
            _require_int(self.peak_trophies, "profile.peak_trophies")
        for metric in (self.armor_coverage, self.power_balance, self.repair_proximity, self.weapon_coverage):
            if metric is not None:
                if not isinstance(metric, (int, float)) or isinstance(metric, bool):
                    raise PvpInputError("profile metric must be numeric")
                if metric < 0 or metric > 100:
                    raise PvpInputError("profile metric must be 0-100")
        if self.evidence_tier not in {e.value for e in EvidenceTier}:
            raise PvpInputError("profile.evidence_tier invalid")
        reject_secret_keys(self.ammo_craft_config, "profile.ammo_craft_config")
        reject_secret_keys(self.resources, "profile.resources")
        # account isolation: battles must belong to this account
        for battle in self.recent_battles:
            if battle.account_pseudonym != self.pseudonym:
                raise PvpInputError("profile contains battle for a different account (isolation violation)")
        # crew/room uniqueness
        crew_names = [c.name for c in self.crew]
        if len(crew_names) != len(set(crew_names)):
            raise PvpInputError("duplicate crew names in profile")
        room_ids = [r.design_id for r in self.rooms]
        # design ids may repeat (multiple same room type); do not enforce uniqueness


def compute_ship_config_version(profile: AccountProgressionProfile) -> str:
    """Deterministic, non-secret hash of ship configuration state."""
    payload = {
        "hull_level": profile.hull_level,
        "crew": [
            {
                "name": c.name,
                "level": c.level,
                "rarity": c.rarity,
                "tp": c.training_points,
                "equipment": c.equipment,
                "role": c.role,
                "assigned_room": c.assigned_room,
                "abilities": sorted(c.abilities.items()),
            }
            for c in sorted(profile.crew, key=lambda x: x.name)
        ],
        "rooms": [
            {
                "design_id": r.design_id,
                "name": r.name,
                "category": r.category,
                "level": r.level,
                "max_level": r.max_level,
                "power": r.power,
                "hp": r.hp,
                "ammo_config": r.ammo_config,
            }
            for r in sorted(profile.rooms, key=lambda x: (x.design_id, x.position_row, x.position_col))
        ],
        "research": [
            {"name": r.name, "level": r.level, "completed": r.completed}
            for r in sorted(profile.research, key=lambda x: x.name)
        ],
        "ai": [
            {"group": a.group, "conditions": list(a.conditions), "actions": list(a.actions), "target": a.target_type}
            for a in sorted(profile.native_ai_rules, key=lambda x: x.group)
        ],
        "layout": {
            "armor": profile.armor_coverage,
            "power": profile.power_balance,
            "repair": profile.repair_proximity,
            "weapon": profile.weapon_coverage,
        },
        "ammo_craft_config": profile.ammo_craft_config,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    value: Optional[float] = None
    weight: float = 1.0
    evidence_tier: str = EvidenceTier.UNKNOWN.value
    note: str = ""


@dataclass(frozen=True)
class ScoreResult:
    name: str
    score: Optional[float] = None
    components: Tuple[ScoreComponent, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    assumptions: Tuple[str, ...] = field(default_factory=tuple)
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    evidence_tier: str = EvidenceTier.UNKNOWN.value

    def __post_init__(self) -> None:
        if self.score is not None:
            if not isinstance(self.score, (int, float)) or isinstance(self.score, bool):
                raise PvpInputError("score must be numeric or None")
            if self.score < 0 or self.score > 100:
                raise PvpInputError("score must be 0-100 or None")


@dataclass(frozen=True)
class PrioritizedItem:
    item_id: str
    title: str
    priority: str
    score_impact: Optional[float] = None
    reason: str = ""
    evidence_tier: str = EvidenceTier.UNKNOWN.value
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    estimated_resources: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HullUpgradeReadiness:
    ready: bool
    blockers: Tuple[str, ...] = field(default_factory=tuple)
    score: Optional[float] = None
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    assumptions: Tuple[str, ...] = field(default_factory=tuple)
    reasons: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AccountAssessment:
    profile_pseudonym: str
    hull_level: int
    scores: Dict[str, ScoreResult] = field(default_factory=dict)
    hull_upgrade_readiness: Optional[HullUpgradeReadiness] = None
    crew_training_priorities: Tuple[PrioritizedItem, ...] = field(default_factory=tuple)
    crew_equipment_priorities: Tuple[PrioritizedItem, ...] = field(default_factory=tuple)
    room_upgrade_priorities: Tuple[PrioritizedItem, ...] = field(default_factory=tuple)
    research_priorities: Tuple[PrioritizedItem, ...] = field(default_factory=tuple)
    ai_recommendations: Tuple[str, ...] = field(default_factory=tuple)
    battle_performance: Optional["BattlePerformanceReport"] = None
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    assumptions: Tuple[str, ...] = field(default_factory=tuple)
    recommendations: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ProgressionRoadmap:
    account_pseudonym: str
    immediate_fixes: Tuple[str, ...] = field(default_factory=tuple)
    next_7_days: Tuple[str, ...] = field(default_factory=tuple)
    next_30_days: Tuple[str, ...] = field(default_factory=tuple)
    hull_advance_criteria: Tuple[str, ...] = field(default_factory=tuple)
    highest_value_crew: Tuple[str, ...] = field(default_factory=tuple)
    highest_value_rooms_research: Tuple[str, ...] = field(default_factory=tuple)
    next_ai_changes_to_test: Tuple[str, ...] = field(default_factory=tuple)
    expected_resource_requirements: Dict[str, Any] = field(default_factory=dict)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    assumptions: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BattlePerformanceReport:
    account_pseudonym: str
    battles_considered: int = 0
    attack_win_rate: Optional[float] = None
    defense_win_rate: Optional[float] = None
    attack_battles: int = 0
    defense_battles: int = 0
    trophy_progression: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    losses_by_cause: Dict[str, int] = field(default_factory=dict)
    average_battle_duration_s: Optional[float] = None
    performance_by_opponent_hull: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    config_comparisons: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    assumptions: Tuple[str, ...] = field(default_factory=tuple)
    notes: Tuple[str, ...] = field(default_factory=tuple)