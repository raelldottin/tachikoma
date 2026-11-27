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


# --- SPEC-14 observations (S14-R2) -----------------------------------------------------------------------------
# What fresh account and design reads say about a course, a room design and a crew member's training. Each
# observation keeps 0 (explicitly none, or no minimum) apart from None (not observed): a later eligibility check must
# treat None as unknown and fail closed.

PRICE_OK = "ok"
PRICE_UNAVAILABLE = "unavailable"
PRICE_MALFORMED = "malformed"
PRICE_PREMIUM = "premium"
PRICE_STATUSES: frozenset[str] = frozenset({PRICE_OK, PRICE_UNAVAILABLE, PRICE_MALFORMED, PRICE_PREMIUM})

CURRENCY_MINERAL = "mineral"
CURRENCY_GAS = "gas"
CURRENCY_STARBUX = "starbux"
SPENDABLE_CURRENCIES: frozenset[str] = frozenset({CURRENCY_MINERAL, CURRENCY_GAS})
PRICE_CURRENCIES: frozenset[str] = frozenset({CURRENCY_MINERAL, CURRENCY_GAS, CURRENCY_STARBUX})

SESSION_NONE = "none"
SESSION_RUNNING = "running"
SESSION_ENDED_UNCLAIMED = "ended_unclaimed"
SESSION_FINISHED = "finished"
SESSION_UNKNOWN = "unknown"
SESSION_PHASES: frozenset[str] = frozenset(
    {SESSION_NONE, SESSION_RUNNING, SESSION_ENDED_UNCLAIMED, SESSION_FINISHED, SESSION_UNKNOWN}
)


@dataclass(frozen=True)
class CoursePrice:
    """A course's advertised price, classified; only ``usable`` (status "ok") prices may ever be paid.

    | Status      | Meaning                                              | Step-3 skip code      |
    |-------------|------------------------------------------------------|-----------------------|
    | ok          | mineral or gas, a whole amount >= 0 (0 is observed)  | -                     |
    | unavailable | no price advertised                                  | ``price_unavailable`` |
    | malformed   | a price that cannot be read unambiguously            | ``price_malformed``   |
    | premium     | a Starbux price (never spent by the policy)          | ``premium_currency``  |

    ``amount`` is None or an int (never a bool). Unavailable and malformed prices carry neither currency nor amount.
    """

    status: str
    currency: str | None = None
    amount: int | None = None

    def __post_init__(self) -> None:
        if self.status not in PRICE_STATUSES:
            raise ValueError(f"unknown price status: {self.status!r}")
        if self.amount is not None and (isinstance(self.amount, bool) or not isinstance(self.amount, int)):
            raise ValueError(f"a price amount must be None or an int: {self!r}")
        if self.status in (PRICE_UNAVAILABLE, PRICE_MALFORMED) and (self.currency, self.amount) != (None, None):
            raise ValueError(f"an unreadable price carries no currency or amount: {self!r}")
        if self.status == PRICE_OK and (
            self.currency not in SPENDABLE_CURRENCIES or self.amount is None or self.amount < 0
        ):
            raise ValueError(f"an ok price needs mineral or gas and a whole amount >= 0: {self!r}")
        if self.status == PRICE_PREMIUM and (
            self.currency != CURRENCY_STARBUX or self.amount is None or self.amount < 0
        ):
            raise ValueError(f"a premium price needs starbux and a whole amount >= 0: {self!r}")

    @property
    def usable(self) -> bool:
        return self.status == PRICE_OK


@dataclass(frozen=True)
class CourseObservation:
    """One TrainingDesign as the design read reports it. 0 = explicitly none / no minimum; None = unknown.

    ``required_room_name`` (@RequiredRoomType) is None when the record does not report it (unknown: fail closed) and
    "" when it reports a blank requirement (explicitly no room requirement).
    """

    design_id: int
    name: str
    rank: int | None
    stat: str | None
    required_room_name: str | None
    required_room_level: int | None
    required_research_design_id: int | None
    price: CoursePrice


@dataclass(frozen=True)
class RoomDesignObservation:
    """One RoomDesign's name, level, training capacity and @RoomType; None = not reported.

    ``room_type`` is stripped; "" when the record reports a blank type.
    """

    design_id: int
    name: str
    level: int | None
    capacity: int | None
    room_type: str | None


_DATED_PHASES: frozenset[str] = frozenset({SESSION_RUNNING, SESSION_ENDED_UNCLAIMED, SESSION_FINISHED})


@dataclass(frozen=True)
class TrainingSession:
    """A crew member's training at one observation time (``phase`` is one of ``SESSION_PHASES``).

    ``course_id`` is None or a positive int. Running, ended_unclaimed and finished carry an end; none and unknown do
    not, and none carries no course either.

    What step 3 (the training-v2 eligibility check) must report for each phase, using only codes of the workflow
    benchmark vocabulary (tests/fixtures/training/workflow_benchmark.json):

    | Phase           | Step-3 outcome on the session axis                                                 |
    |-----------------|------------------------------------------------------------------------------------|
    | running         | skip ``training_in_progress``                                                      |
    | ended_unclaimed | skip ``unclaimed_course``                                                          |
    | none, finished  | eligible on this axis (other checks still apply)                                   |
    | unknown         | blocks the start; no dedicated code, so step 3 must skip, reporting the closest    |
    |                 | code ``training_in_progress`` (the member may still be training)                   |

    ``finished`` is not evidence that a course completed: an idle placeholder end in the past maps there too.
    Completion and stat gains are separate observations.
    """

    course_id: int | None
    end: datetime | None
    phase: str

    def __post_init__(self) -> None:
        if self.phase not in SESSION_PHASES:
            raise ValueError(f"unknown session phase: {self.phase!r}")
        if self.end is not None:
            _require_aware(self.end, "end")
        if self.course_id is not None and (
            isinstance(self.course_id, bool) or not isinstance(self.course_id, int) or self.course_id <= 0
        ):
            raise ValueError(f"course_id must be None or a positive int: {self.course_id!r}")
        if self.phase in _DATED_PHASES and self.end is None:
            raise ValueError(f"a {self.phase} session needs an end")
        if self.phase not in _DATED_PHASES and self.end is not None:
            raise ValueError(f"a {self.phase} session carries no end")
        if self.phase == SESSION_NONE and self.course_id is not None:
            raise ValueError("no training carries no course")


@dataclass(frozen=True)
class PrestigeRecipe:
    """Two crew designs (possibly the same design twice) that Prestige into ``to_design_id``."""

    first_design_id: int
    second_design_id: int
    to_design_id: int
