"""Immutable data model for the crew-training planner (frozen dataclasses, no I/O).

- Models normalise themselves: sequences become tuples, mappings become read-only ``MappingProxyType`` copies.
  Because of those mapping fields the objects compare by value but are not hashable.
- Datetimes (``CrewState.training_end``, ``TrainingContext.now``) must be timezone-aware; naive values raise
  ValueError. ``sdk.training.parse`` always produces aware UTC datetimes.
- ``CrewState.stats`` keys must be among the nine bare ``*Improvement`` names (``CANONICAL_STATS``); the XML
  ``@``-prefixed attributes and any other key raise ValueError, so a mis-keyed caller fails loudly instead of
  planning every crew at 0%.
- ``Skip.reason_code`` must be one of ``SKIP_CODES`` (a closed set).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType

CANONICAL_STATS: tuple[str, ...] = (
    "HpImprovement",
    "PilotImprovement",
    "RepairImprovement",
    "WeaponImprovement",
    "ScienceImprovement",
    "EngineImprovement",
    "AttackImprovement",
    "AbilityImprovement",
    "StaminaImprovement",
)

SKIP_NOT_IN_TRAINING_ROOM = "not_in_training_room"
SKIP_TRAINING_IN_PROGRESS = "training_in_progress"
SKIP_RESEARCH_MISSING = "research_missing"
SKIP_TP_CAP_REACHED = "tp_cap_reached"
SKIP_DESIGN_MISSING = "design_missing"
SKIP_UNKNOWN_ROLE = "unknown_role"
SKIP_FATIGUED = "fatigued"
SKIP_COMPLETE = "complete"

SKIP_CODES: frozenset[str] = frozenset(
    {
        SKIP_NOT_IN_TRAINING_ROOM,
        SKIP_TRAINING_IN_PROGRESS,
        SKIP_RESEARCH_MISSING,
        SKIP_TP_CAP_REACHED,
        SKIP_DESIGN_MISSING,
        SKIP_UNKNOWN_ROLE,
        SKIP_FATIGUED,
        SKIP_COMPLETE,
    }
)


def frozen_mapping(value: Mapping) -> Mapping:
    """A read-only copy of ``value`` (the package's one mapping normaliser)."""
    return MappingProxyType(dict(value))


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


@dataclass(frozen=True)
class Skip:
    character_id: int
    reason_code: str
    detail: str

    def __post_init__(self) -> None:
        if self.reason_code not in SKIP_CODES:
            raise ValueError(f"unknown skip reason code: {self.reason_code!r}")


@dataclass(frozen=True)
class CrewState:
    character_id: int
    name: str
    design_id: int
    rarity: str
    level: int
    room_id: int | None
    room_name: str | None
    stats: Mapping[str, int]
    training_capacity: int
    fatigue: int
    training_design_id: int | None
    training_end: datetime | None

    def __post_init__(self) -> None:
        prefixed = sorted(key for key in self.stats if key.startswith("@"))
        if prefixed:
            raise ValueError(f"stat keys must not carry the XML '@' prefix: {prefixed}")
        unknown = sorted(set(self.stats) - set(CANONICAL_STATS))
        if unknown:
            raise ValueError(f"stat keys must be among CANONICAL_STATS, got {unknown}")
        object.__setattr__(self, "stats", frozen_mapping(self.stats))
        if self.training_end is not None:
            _require_aware(self.training_end, "training_end")


@dataclass(frozen=True)
class TrainingDesign:
    id: int
    name: str
    tier: int
    stat: str | None
    duration: timedelta
    required_research: str | None
    required_room_type: str | None


@dataclass(frozen=True)
class TrainingContext:
    now: datetime
    crew: tuple[CrewState, ...]
    designs: tuple[TrainingDesign, ...]
    rooms: Mapping[int, str]
    research_levels: Mapping[str, int]
    items: Mapping[int, int]

    def __post_init__(self) -> None:
        _require_aware(self.now, "now")
        object.__setattr__(self, "crew", tuple(self.crew))
        object.__setattr__(self, "designs", tuple(self.designs))
        object.__setattr__(self, "rooms", frozen_mapping(self.rooms))
        object.__setattr__(self, "research_levels", frozen_mapping(self.research_levels))
        object.__setattr__(self, "items", frozen_mapping(self.items))


@dataclass(frozen=True)
class FinishTraining:
    character_id: int
    reason: str


@dataclass(frozen=True)
class StartTraining:
    character_id: int
    training_design_id: int
    reason: str


@dataclass(frozen=True)
class UseConsumable:
    character_id: int
    item_design_id: int
    reason: str


Decision = FinishTraining | StartTraining | UseConsumable | Skip
