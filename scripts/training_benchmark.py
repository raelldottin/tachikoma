"""Crew-training simulator benchmark: a scored, deterministic, offline gate for the crew-training planner.

Every scenario under automation/benchmark/training/scenarios is a small world (rooms, research, inventory, crew,
optional injected faults) that runs for several simulated days in the offline simulator (sdk.simulation.game), once
per seed. The executor under test is a *tick*: ``tick(client, now) -> TickReport | None``. It drives a
RecordingClient, the simulator's Client surface, which journals every training call together with the simulator's
own crew record from just before the call, the outcome and the faults the call fired. Every metric and every
hard-violation oracle is a pure function of the world, that call journal and the simulator's training log, so no
executor can skip a check. The default tick is ``make_contract_executor``, which follows the documented executor
contract; Unit 9's executor (sdk/execution/training.py) plugs in as another tick. The composite score must never fall
below baseline.json.

No network, no credentials: the simulator is in-process. See docs/baseline/training-benchmark.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:  # run as a script: python scripts/training_benchmark.py
    sys.path.insert(0, ROOT)

from sdk.actions.catalog import get_spec  # noqa: E402
from sdk.execution.runner import call_arguments  # noqa: E402
from sdk.policy import DesignData  # noqa: E402
from sdk.simulation.game import (  # noqa: E402
    READ_FAULTS,
    SNAPSHOT,
    TIME_FORMAT,  # @TrainingEndDate and the simulator log's times, UTC without an offset
    Fault,
    SimulatedClock,
    SimulatedFaultError,
    build_games,
)
from sdk.training import build_context  # noqa: E402
from sdk.training.actions import action_for  # noqa: E402
from sdk.training.model import SKIP_NOT_IN_TRAINING_ROOM, CrewState, FinishTraining, Skip  # noqa: E402
from sdk.training.parse import parse_consumable_designs, parse_crew  # noqa: E402
from sdk.training.planner import plan_training, remaining_stock, replan_after_finish  # noqa: E402
from sdk.training.strategy import DEFAULT_STRATEGY, TrainingStrategy, with_consumables  # noqa: E402

ACCOUNT = "bench"
BENCHMARK_DIR = os.path.join(ROOT, "automation", "benchmark", "training")
SCENARIO_DIR = os.path.join(BENCHMARK_DIR, "scenarios")
BASELINE_PATH = os.path.join(BENCHMARK_DIR, "baseline.json")
EXIT_OK, EXIT_GATE, EXIT_ERROR = 0, 1, 2
# Set in the environment of a benchmark child (the determinism child, a qa-eval benchmark-channel run): such a run
# starts no determinism child of its own, so children never recurse.
CHILD_ENV = "TRAINING_BENCHMARK_CHILD"
CHILD_TIMEOUT = 300.0  # seconds the determinism child may take (the whole suite runs in about 8 s)

# -- world catalog: every record parses for both sdk.training.build_context and the simulator ---------------------

# room design name -> (design id, training room type or None, upgraded from design id)
CATALOG_ROOMS = {
    "GYM": (1000, "gym", None),
    "Galaxy Gym": (1001, "gym", 1000),
    "Academy": (1100, "academy", None),
    "Lunar College": (1101, "academy", 1100),
    "Bridge": (1200, None, None),
}
# research design name -> (design id, root design id): the live training lines, ids, names and roots included
# (ListAllResearchDesigns2, design version 717; tests/fixtures/training/live_research_designs.xml)
CATALOG_RESEARCH = {
    "Fitness 101": (74, 74),
    "Fitness 102": (75, 74),
    "Fitness 103": (76, 74),
    "Fitness 201": (77, 74),
    "Fitness 202": (78, 74),
    "Fitness 203": (79, 74),
    "Education 101": (101, 101),
    "Education 102": (102, 101),
    "Education 103": (103, 101),
    "Education 201": (104, 101),
    "Education 202": (105, 101),
    "Education 203": (106, 101),
}
# training name -> (design id, simulator stat, room design name, tier)
CATALOG_TRAININGS = {
    "Read Expert Weapon Theory": (3011, "Weapon", "Academy", 1),
    "Weapons Summit": (3012, "Weapon", "Academy", 2),
    "Weapons PHD": (3013, "Weapon", "Academy", 3),
    "Big Book of Science": (3021, "Science", "Academy", 1),
    "Scientific Summit": (3022, "Science", "Academy", 2),
    "Science PHD": (3023, "Science", "Academy", 3),
    "Study Expert Engineering Manual": (3031, "Engine", "Academy", 1),
    "Engineering Summit": (3032, "Engine", "Academy", 2),
    "Engineering PHD": (3033, "Engine", "Academy", 3),
    "Read Expert Pilot Handbook": (3041, "Pilot", "Academy", 1),
    "Pilot Summit": (3042, "Pilot", "Academy", 2),
    "Pilot Expert": (3043, "Pilot", "Academy", 3),
    "Bench Press": (3111, "Attack", "GYM", 1),
    "Muscle Beach": (3112, "Attack", "GYM", 2),
    "Olympic Weightlifting": (3113, "Attack", "GYM", 3),
    "Steam Yoga": (3121, "Stamina", "GYM", 1),
    "Crew vs Wild": (3122, "Stamina", "GYM", 2),
    "Space Marine": (3123, "Stamina", "GYM", 3),
    "Kickbox": (3131, "Hp", "GYM", 1),
    "BBJ": (3132, "Hp", "GYM", 2),
    "Shaolin Tradition": (3133, "Hp", "GYM", 3),
}
# tier -> (duration seconds, fatigue added, min gain, max gain)
TIER_PARAMS = {1: (3600, 10, 1, 3), 2: (10800, 20, 5, 9), 3: (21600, 30, 12, 18)}
# (room design name, tier) -> required research design id (Fitness/Education 201 and 203); tier 1 needs none
TIER_RESEARCH = {
    ("GYM", 2): CATALOG_RESEARCH["Fitness 201"][0],
    ("GYM", 3): CATALOG_RESEARCH["Fitness 203"][0],
    ("Academy", 2): CATALOG_RESEARCH["Education 201"][0],
    ("Academy", 3): CATALOG_RESEARCH["Education 203"][0],
}
# design key -> (design id, rarity, training capacity)
CATALOG_CHARACTERS = {
    "common": (4001, "Common", 60),
    "elite": (4002, "Elite", 100),
    "unique": (4003, "Unique", 100),
    "epic": (4004, "Epic", 100),
    "hero": (4005, "Hero", 120),
    "special": (4006, "Special", 120),
    "captain": (4007, "Special", 200),
    "legendary": (4008, "Legendary", 150),
    "standard": (4009, "Standard", 50),
}
# item name -> (design id, rarity, simulator stat, gain, fatigue added, fatigue recovered)
CATALOG_ITEMS = {
    "Common ATK Consumable": (5001, "Common", "Attack", 2, 0, 0),
    "Common WPN Consumable": (5002, "Common", "Weapon", 2, 0, 0),
    "Common SCI Consumable": (5003, "Common", "Science", 2, 0, 0),
    "Common ENG Consumable": (5004, "Common", "Engine", 2, 0, 0),
    "Common STA Consumable": (5005, "Common", "Stamina", 2, 0, 0),
    "Common HP Consumable": (5006, "Common", "Hp", 2, 0, 0),
    "Hero ATK Consumable": (5011, "Hero", "Attack", 5, 0, 50),
    "Hero WPN Consumable": (5012, "Hero", "Weapon", 5, 0, 50),
    "Hero SCI Consumable": (5013, "Hero", "Science", 5, 0, 50),
    "Hero ENG Consumable": (5014, "Hero", "Engine", 5, 0, 50),
    "Hero STA Consumable": (5015, "Hero", "Stamina", 5, 0, 50),
    "Hero HP Consumable": (5016, "Hero", "Hp", 5, 0, 50),
}
# (stat code, tier) -> item design id: the strategy's consumables, and what parse_consumable_designs must find
CATALOG_CONSUMABLES = {
    ("ATK", "Common"): 5001,
    ("WPN", "Common"): 5002,
    ("SCI", "Common"): 5003,
    ("ENG", "Common"): 5004,
    ("STA", "Common"): 5005,
    ("HP", "Common"): 5006,
    ("ATK", "Hero"): 5011,
    ("WPN", "Hero"): 5012,
    ("SCI", "Hero"): 5013,
    ("ENG", "Hero"): 5014,
    ("STA", "Hero"): 5015,
    ("HP", "Hero"): 5016,
}
# research design id -> (family, level = course number)
CATALOG_RESEARCH_LEVELS = {
    74: ("Fitness", 101),
    75: ("Fitness", 102),
    76: ("Fitness", 103),
    77: ("Fitness", 201),
    78: ("Fitness", 202),
    79: ("Fitness", 203),
    101: ("Education", 101),
    102: ("Education", 102),
    103: ("Education", 103),
    104: ("Education", 201),
    105: ("Education", 202),
    106: ("Education", 203),
}
SIM_STATS = ("Hp", "Pilot", "Repair", "Weapon", "Science", "Engine", "Attack", "Ability", "Stamina")
ROOM_TYPE_WORDS = {"gym": "Gym", "academy": "Academy"}
TRAININGS_BY_ID = {design_id: (name, room, tier) for name, (design_id, _, room, tier) in CATALOG_TRAININGS.items()}
CONSUMABLE_TIERS = {item_id: tier for (_, tier), item_id in CATALOG_CONSUMABLES.items()}

# -- scenario schema ---------------------------------------------------------------------------------------------

ALLOWED_FAULTS = ("reject", "contradicted-effect", "raise", "ambiguous-response", "snapshot-failure")
FAULT_METHODS = ("addTraining", "finishTraining", "activateItem3", "snapshot")
REQUIRED_TAGS = frozenset(
    {
        "fresh",
        "mid-level",
        "near-cap",
        "missing-research",
        "mixed-rooms",
        "no-training-room",
        "fatigued",
        "consumables",
        "in-training",
        "fault-reject",
        "fault-contradicted",
        "fault-raise",
        "fault-ambiguous",
        "fault-snapshot",
    }
)
MIN_SUITE_DAYS = 7  # owner requirement: every checked-in scenario runs at least a week
SEED_COUNT = 3  # each scenario runs at seed, seed + 1 and seed + 2; its score is the mean
SCENARIO_REQUIRED = (
    "schema_version",
    "name",
    "description",
    "tags",
    "seed",
    "start",
    "days",
    "rooms",
    "research",
    "inventory",
    "crew",
    "faults",
)
SCENARIO_OPTIONAL = ("tick_minutes", "fatigue_recovery_seconds")
CREW_KEYS = ("id", "name", "design", "room", "fatigue", "level", "stats", "training")
FAULT_KEYS = ("kind", "method", "day", "call", "probability", "effect", "training_design")
NAME_RE = re.compile(r"^[a-z0-9-]+$")

# -- oracle tables: independent literal copies of the owner-approved policy ---------------------------------------
# They are never imported from sdk/training, so a mutant of the planner or the strategy cannot move them.
# tests/test_training_benchmark_policy.py pins them to the policy sources; a deliberate policy change updates both.

ORACLE_ROLES = {
    "Galactic Succubus": "weapons",
    "Galactic Snow Maiden": "weapons",
    "Delish": "weapons",
    "Mistycball": "shields",
    "C.P.U.": "shields",
    "r2e": "shields",
    "The Conjoint Archon": "engines",
    "Galactic Sprite": "engines",
    "Huge Hellaluya": "rushers",
    "Cyber Duck": "rushers",
    "Admiral Serena": "defenders",
    "Ancestral Spirit": "defenders",
    "Green Ranger - Oliver": "defenders",
    "Huntress": "defenders",
    "Turkey Hero": "defenders",
    "1st engineer Tully": "defenders",
    "King Dong": "defenders",
}
ORACLE_GYM_STRENGTH = ("Bench Press", "Muscle Beach", "Olympic Weightlifting")
# role -> slot -> (room type, training names by tier)
ORACLE_ROLE_SLOTS = {
    "weapons": {
        "primary": ("academy", ("Read Expert Weapon Theory", "Weapons Summit", "Weapons PHD")),
        "secondary": ("gym", ORACLE_GYM_STRENGTH),
    },
    "shields": {
        "primary": ("academy", ("Big Book of Science", "Scientific Summit", "Science PHD")),
        "secondary": ("gym", ORACLE_GYM_STRENGTH),
    },
    "engines": {
        "primary": ("gym", ORACLE_GYM_STRENGTH),
        "secondary": ("academy", ("Study Expert Engineering Manual", "Engineering Summit", "Engineering PHD")),
    },
    "rushers": {
        "primary": ("gym", ("Steam Yoga", "Crew vs Wild", "Space Marine")),
        "secondary": ("gym", ORACLE_GYM_STRENGTH),
    },
    "defenders": {
        "primary": ("gym", ORACLE_GYM_STRENGTH),
        "secondary": ("gym", ("Kickbox", "BBJ", "Shaolin Tradition")),
    },
}
ORACLE_RARITY_STARS = {"Common": 1, "Elite": 2, "Unique": 3, "Epic": 4, "Hero": 5, "Special": 6, "Legendary": 7}
ORACLE_TP_CAPS = {3: 70, 4: 80, 5: 90, 6: 100, 7: 110}
ORACLE_EXCEPTIONAL_CAPS = {(4, "Mistycball"): 100, (4, "Huge Hellaluya"): 100}
ORACLE_CAPTAIN_CAPS = {6: 200}
ORACLE_BANDS = (
    (0, 50, "primary", 1),
    (51, 64, "primary", 2),
    (65, 71, "primary", 3),
    (72, 73, "secondary", 1),
    (74, 84, "secondary", 2),
    (85, 89, "secondary", 3),
)
ORACLE_COMPLETE_PERCENT = 90
ORACLE_MAX_TRAINING_FATIGUE = 100
ORACLE_GATES = {
    ("primary", 1): (("Fitness", 101), ("Education", 101)),
    ("primary", 2): (("Fitness", 201), ("Education", 201)),
    ("primary", 3): (("Fitness", 203), ("Education", 203)),
    ("secondary", 1): (),
    ("secondary", 2): (("Fitness", 201), ("Education", 201)),
    ("secondary", 3): (("Fitness", 203), ("Education", 203)),
}
# tier -> (min percent, max percent or None, min fatigue, max fatigue or None)
ORACLE_CONSUMABLE_RULES = {"Common": (0, 29, 0, 0), "Hero": (90, None, 100, None)}

# -- score -------------------------------------------------------------------------------------------------------

# score = 100 * R * (0.75 P + 0.25 U); U is left out (weights renormalised to P alone) when nothing had a window.
SCORE_WEIGHTS = {"progress": 0.75, "utilisation": 0.25}
SCORE_FORMULA = "100 * R * (0.75 * P + 0.25 * U); 100 * R * P without a utilisation window; 0 on a hard violation"
# Hard-violation oracles: any one zeroes the scenario and fails the gate. Every oracle is gated.
GATED_VIOLATIONS = (
    "cap",
    "complete",
    "fatigue",
    "research-gate",
    "room",
    "design",
    "design-research",
    "consumable",
    "consumable-precedence",
)
MAX_VIOLATION_SAMPLES = 20


# -- pure oracle helpers -----------------------------------------------------------------------------------------


def percent(total: int, capacity: int) -> int:
    """The exact integer ceiling of total * 100 / capacity (the benchmark's own copy)."""
    return -(-total * 100 // capacity)


def first_total(pct: int, capacity: int) -> int:
    """The smallest total whose percent of ``capacity`` reaches ``pct``."""
    return 0 if pct <= 0 else (pct - 1) * capacity // 100 + 1


def policy_target(role: str | None, room_type: str | None, total: int, capacity: int, cap: int) -> int:
    """The most the policy can train a crew to in its room: the TP cap, the complete percent, and the first band
    from ``total`` on whose slot needs another room type (nothing in the game or the simulator moves crew).

    Research gates are left out on purpose (owner decision, final review). Research never changes during a run, but
    a consumable can still move a research-blocked crew, and counting the blocked TP as headroom keeps the research
    gap visible: P is a fraction of this headroom, so a crew the gates hold back lowers P by its blocked TP, and U
    charges it idle time until it reaches this target (the whole horizon when it never does). A scenario that tests
    missing research scores low for that reason, not because the executor erred.
    """
    target = min(cap, first_total(ORACLE_COMPLETE_PERCENT, capacity))
    slots = ORACLE_ROLE_SLOTS.get(role or "")
    if slots is None or room_type is None:
        return target
    start = percent(total, capacity)
    for low, high, slot, _ in ORACLE_BANDS:
        if high >= start and slots[slot][0] != room_type:
            return min(target, first_total(low, capacity))
    return target


def oracle_band(pct: int) -> tuple[str, int] | None:
    """(slot, tier) for a training percent; None once training is complete."""
    if pct >= ORACLE_COMPLETE_PERCENT:
        return None
    for low, high, slot, tier in ORACLE_BANDS:
        if pct <= high:
            return slot, tier
    return None  # pragma: no cover - the bands tile 0..complete-1


def band_label(pct: int) -> str:
    band = oracle_band(pct)
    return "complete" if band is None else f"{band[0]}-{band[1]}"


def oracle_cap(rarity: str, name: str, capacity: int) -> int:
    """The TP cap the owner-approved policy allows: captain, exceptional, then tier cap, never above capacity."""
    stars = ORACLE_RARITY_STARS.get(rarity, 0)
    captain = ORACLE_CAPTAIN_CAPS.get(stars)
    if captain is not None and capacity >= captain:
        cap = captain
    else:
        cap = ORACLE_EXCEPTIONAL_CAPS.get((stars, name), ORACLE_TP_CAPS.get(stars, capacity))
    return min(capacity, cap)


def consumable_allowed(tier: str, pct: int, fatigue: int) -> bool:
    """Whether the consumable rule of ``tier`` allows an item at (pct, fatigue)."""
    rule = ORACLE_CONSUMABLE_RULES.get(tier)
    if rule is None:
        return False
    min_pct, max_pct, min_fatigue, max_fatigue = rule
    pct_ok = pct >= min_pct and (max_pct is None or pct <= max_pct)
    fatigue_ok = fatigue >= min_fatigue and (max_fatigue is None or fatigue <= max_fatigue)
    return pct_ok and fatigue_ok


def research_levels(research_ids: Sequence[int]) -> dict[str, int]:
    """Research family -> highest completed level, from catalog research design ids (independent of parse)."""
    levels: dict[str, int] = {}
    for research_id in research_ids:
        entry = CATALOG_RESEARCH_LEVELS.get(research_id)
        if entry is None:
            continue
        family, level = entry
        levels[family] = max(levels.get(family, 0), level)
    return levels


def gate_met(band: tuple[str, int], levels: Mapping[str, int]) -> bool:
    """Whether one research alternative of the band's gate is met; an empty gate is ungated."""
    alternatives = ORACLE_GATES[band]
    if not alternatives:
        return True
    return any(levels.get(name, 0) >= level for name, level in alternatives)


def design_research(design_id: int) -> int | None:
    """The research design id a catalog training needs (None: ungated or not a catalog training)."""
    entry = TRAININGS_BY_ID.get(design_id)
    return None if entry is None else TIER_RESEARCH.get((entry[1], entry[2]))


def role_training(role: str | None, band: tuple[str, int] | None) -> tuple[str | None, int | None]:
    """(room type, catalog design id) of the training ``role`` does in ``band``; (None, None) without one."""
    slots = ORACLE_ROLE_SLOTS.get(role or "")
    if slots is None or band is None:
        return None, None
    room_type, trainings = slots[band[0]]
    return room_type, CATALOG_TRAININGS[trainings[band[1] - 1]][0]


def _to_int(value: object) -> int:
    """An ASCII-decimal id as an int, or 0 for anything else (an executor under test may pass garbage, and
    str.isdigit accepts characters such as "²" that int() refuses)."""
    if _is_int(value):
        return value
    return int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else 0


# -- errors ------------------------------------------------------------------------------------------------------


class BenchmarkError(Exception):
    """A benchmark input or run problem (exit 2), never a planner regression."""


class ScenarioError(BenchmarkError):
    def __init__(self, problems: Sequence[str], source: str = "<scenario>") -> None:
        self.problems = list(problems)
        self.source = source
        super().__init__(f"{source}: " + "; ".join(self.problems))


class WorldError(BenchmarkError):
    """A scenario that validates but cannot make a meaningful world (no reachable headroom, catalog drift)."""


class BaselineError(BenchmarkError):
    """A missing-schema or malformed baseline.json."""


class BaselineRefused(BenchmarkError):
    """An --update-baseline that would lower the baseline, or record a nondeterministic or violating run."""


# -- scenarios ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoomSpec:
    id: int
    design: str


@dataclass(frozen=True)
class CrewSpec:
    id: int
    name: str
    design: str
    room: int
    fatigue: int = 0
    level: int = 1
    stats: Mapping[str, int] = field(default_factory=dict)
    training: tuple[str, int] | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    tags: tuple[str, ...]
    seed: int
    start: datetime
    days: int
    tick_minutes: int
    fatigue_recovery_seconds: int
    rooms: tuple[RoomSpec, ...]
    research: tuple[str, ...]
    inventory: tuple[tuple[str, int], ...]
    crew: tuple[CrewSpec, ...]
    faults: tuple[Mapping[str, object], ...]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_in(value: object, low: int, high: int | None = None) -> bool:
    return _is_int(value) and value >= low and (high is None or value <= high)


def _known(value: object, table: Mapping) -> bool:
    """A catalog name in ``table``; a JSON list or object (unhashable) is simply not one."""
    return isinstance(value, str) and value in table


def _parse_start(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _validate_rooms(value: object, problems: list[str]) -> tuple[RoomSpec, ...]:
    if not isinstance(value, list) or not value:
        problems.append("rooms: must be a non-empty list")
        return ()
    rooms: list[RoomSpec] = []
    for index, entry in enumerate(value):
        where = f"rooms[{index}]"
        if not isinstance(entry, Mapping) or set(entry) != {"id", "design"}:
            problems.append(f"{where}: must be an object with exactly id and design")
            continue
        if not _int_in(entry["id"], 1):
            problems.append(f"{where}.id: must be a positive integer")
        elif any(room.id == entry["id"] for room in rooms):
            problems.append(f"{where}.id: duplicate room id {entry['id']}")
        elif not _known(entry["design"], CATALOG_ROOMS):
            problems.append(f"{where}.design: unknown room design {entry['design']!r}")
        else:
            rooms.append(RoomSpec(entry["id"], entry["design"]))
    return tuple(rooms)


def _validate_stats(value: object, capacity: int | None, where: str, problems: list[str]) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        problems.append(f"{where}.stats: must be an object of stat -> points")
        return {}
    unknown = sorted(str(stat) for stat in value if stat not in SIM_STATS)
    if unknown:
        problems.append(f"{where}.stats: unknown stats {', '.join(unknown)}")
        return {}
    if not all(_int_in(points, 0) for points in value.values()):
        problems.append(f"{where}.stats: points must be non-negative integers")
        return {}
    total = sum(value.values())
    if capacity is not None and total > capacity:
        problems.append(f"{where}.stats: total {total} is over the training capacity {capacity}")
    return dict(value)


def _validate_session(value: object, where: str, problems: list[str]) -> tuple[str, int] | None:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"design", "ends_after_minutes"}
        or not _known(value["design"], CATALOG_TRAININGS)
        or not _int_in(value["ends_after_minutes"], 1)
    ):
        problems.append(f"{where}.training: needs a catalog training design and ends_after_minutes >= 1")
        return None
    return value["design"], value["ends_after_minutes"]


def _validate_crew_entry(entry: Mapping, where: str, room_ids: set[int], seen: set[int], problems: list[str]):
    unknown = sorted(str(key) for key in entry if key not in CREW_KEYS)
    if unknown:
        problems.append(f"{where}: unknown keys {', '.join(unknown)}")
    missing = [key for key in ("id", "name", "design", "room") if key not in entry]
    if missing:
        problems.append(f"{where}: missing {', '.join(missing)}")
        return None
    if not _int_in(entry["id"], 1):
        problems.append(f"{where}.id: must be a positive integer")
    elif entry["id"] in seen:
        problems.append(f"{where}.id: duplicate crew id {entry['id']}")
    else:
        seen.add(entry["id"])
    if not isinstance(entry["name"], str) or not entry["name"].strip():
        problems.append(f"{where}.name: must be a non-empty string")
    design = CATALOG_CHARACTERS.get(entry["design"]) if isinstance(entry["design"], str) else None
    if design is None:
        problems.append(f"{where}.design: unknown character design {entry['design']!r}")
    if not (_is_int(entry["room"]) and (entry["room"] == 0 or entry["room"] in room_ids)):
        problems.append(f"{where}.room: {entry['room']!r} is neither 0 nor a declared room id")
    fatigue, level = entry.get("fatigue", 0), entry.get("level", 1)
    if not _int_in(fatigue, 0, 100):
        problems.append(f"{where}.fatigue: must be an integer 0..100")
    if not _int_in(level, 1):
        problems.append(f"{where}.level: must be a positive integer")
    stats = _validate_stats(entry.get("stats", {}), design[2] if design else None, where, problems)
    session = _validate_session(entry["training"], where, problems) if "training" in entry else None
    return CrewSpec(entry["id"], entry["name"], entry["design"], entry["room"], fatigue, level, stats, session)


def _validate_crew(value: object, room_ids: set[int], problems: list[str]) -> tuple[CrewSpec, ...]:
    if not isinstance(value, list) or not value:
        problems.append("crew: must be a non-empty list")
        return ()
    crew: list[CrewSpec] = []
    seen: set[int] = set()
    for index, entry in enumerate(value):
        where = f"crew[{index}]"
        if not isinstance(entry, Mapping):
            problems.append(f"{where}: must be an object")
            continue
        before = len(problems)
        spec = _validate_crew_entry(entry, where, room_ids, seen, problems)
        if len(problems) == before:
            crew.append(spec)
    return tuple(crew)


def _validate_fault(entry: object, where: str, problems: list[str]) -> Mapping[str, object] | None:
    if not isinstance(entry, Mapping):
        problems.append(f"{where}: must be an object")
        return None
    before = len(problems)
    unknown = sorted(str(key) for key in entry if key not in FAULT_KEYS)
    if unknown:
        problems.append(f"{where}: unknown keys {', '.join(unknown)}")
    kind, method = entry.get("kind"), entry.get("method")
    if kind not in ALLOWED_FAULTS:
        problems.append(f"{where}.kind: {kind!r} is not one of {', '.join(ALLOWED_FAULTS)}")
    if method not in FAULT_METHODS:
        problems.append(f"{where}.method: {method!r} is not one of {', '.join(FAULT_METHODS)}")
    if "effect" in entry and kind not in ("raise", "ambiguous-response"):
        problems.append(f"{where}.effect: only raise and ambiguous-response faults take an effect")
    design = entry.get("training_design")
    if kind == "contradicted-effect" and not _known(design, CATALOG_TRAININGS):
        problems.append(f"{where}.training_design: contradicted-effect needs a catalog training design")
    elif kind != "contradicted-effect" and "training_design" in entry:
        problems.append(f"{where}.training_design: only contradicted-effect faults take a training design")
    if len(problems) > before:
        return None
    try:
        Fault.from_dict(_sim_fault(entry))
    except ValueError as error:
        problems.append(f"{where}: {error}")
        return None
    return dict(entry)


def _validate_header(data: Mapping, missing: list[str], problems: list[str]) -> None:
    get = data.get
    if "schema_version" not in missing and not (_is_int(get("schema_version")) and get("schema_version") == 1):
        problems.append(f"schema_version: must be 1, not {get('schema_version')!r}")
    if "name" not in missing and not (isinstance(get("name"), str) and NAME_RE.match(get("name"))):
        problems.append(f"name: {get('name')!r} must match {NAME_RE.pattern}")
    if "description" not in missing and not (isinstance(get("description"), str) and get("description").strip()):
        problems.append("description: must be a non-empty string")
    tags = get("tags", [])
    if not (isinstance(tags, list) and all(isinstance(tag, str) for tag in tags) and len(set(tags)) == len(tags)):
        problems.append("tags: must be a list of unique strings")
    if "seed" not in missing and not _int_in(get("seed"), 0):
        problems.append(f"seed: must be a non-negative integer, not {get('seed')!r}")
    if "start" not in missing and _parse_start(get("start")) is None:
        problems.append(f"start: {get('start')!r} must be an ISO 8601 time with a UTC offset")
    if "days" not in missing and not _int_in(get("days"), 1, 30):
        problems.append(f"days: must be an integer 1..30, not {get('days')!r}")
    tick = get("tick_minutes", 60)
    if not (_int_in(tick, 5, 240) and 1440 % tick == 0):
        problems.append(f"tick_minutes: must be an integer 5..240 dividing 1440, not {tick!r}")
    recovery = get("fatigue_recovery_seconds", 360)
    if not _int_in(recovery, 1):
        problems.append(f"fatigue_recovery_seconds: must be a positive integer, not {recovery!r}")


def _validate_faults(data: Mapping, problems: list[str]) -> list[Mapping[str, object]]:
    faults_raw = data.get("faults", [])
    faults: list[Mapping[str, object]] = []
    if not isinstance(faults_raw, list):
        problems.append("faults: must be a list")
        return faults
    days = data.get("days")
    for index, entry in enumerate(faults_raw):
        fault = _validate_fault(entry, f"faults[{index}]", problems)
        # A null day means any day (Fault.from_dict accepts it), so only a numbered day has a range.
        if fault is not None and fault.get("day") is not None and _int_in(days, 1, 30) and fault["day"] >= days:
            problems.append(f"faults[{index}].day: {fault['day']} is past the last day ({days - 1}); "
                            "it would never fire")
        elif fault is not None:
            faults.append(fault)
    return faults


def validate_scenario(data: Mapping, *, source: str = "<scenario>") -> Scenario:
    """A checked Scenario; every problem is collected and raised as one ScenarioError."""
    problems: list[str] = []
    if not isinstance(data, Mapping):
        raise ScenarioError(["scenario: must be a JSON object"], source)
    unknown = sorted(str(key) for key in data if key not in SCENARIO_REQUIRED + SCENARIO_OPTIONAL)
    if unknown:
        problems.append(f"unknown keys: {', '.join(unknown)}")
    missing = [key for key in SCENARIO_REQUIRED if key not in data]
    for key in missing:
        problems.append(f"{key}: missing")
    _validate_header(data, missing, problems)
    rooms = _validate_rooms(data.get("rooms"), problems) if "rooms" in data else ()
    research = data.get("research", [])
    if not (isinstance(research, list) and len(set(map(str, research))) == len(research)) or any(
        not _known(name, CATALOG_RESEARCH) for name in research
    ):
        problems.append(f"research: must be unique names among {', '.join(CATALOG_RESEARCH)}")
        research = []
    inventory = data.get("inventory", {})
    if not isinstance(inventory, Mapping) or any(
        name not in CATALOG_ITEMS or not _int_in(quantity, 0) for name, quantity in inventory.items()
    ):
        problems.append("inventory: must map catalog item names to non-negative quantities")
        inventory = {}
    crew = _validate_crew(data.get("crew"), {room.id for room in rooms}, problems) if "crew" in data else ()
    faults = _validate_faults(data, problems)
    if problems:
        raise ScenarioError(problems, source)
    return Scenario(
        name=data["name"],
        description=data["description"],
        tags=tuple(data["tags"]),
        seed=data["seed"],
        start=_parse_start(data["start"]),
        days=data["days"],
        tick_minutes=data.get("tick_minutes", 60),
        fatigue_recovery_seconds=data.get("fatigue_recovery_seconds", 360),
        rooms=rooms,
        research=tuple(research),
        inventory=tuple(sorted(inventory.items())),
        crew=crew,
        faults=tuple(faults),
    )


def load_scenario(path: str) -> Scenario:
    """A scenario file; its name must equal the file stem."""
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        raise ScenarioError([f"cannot read: {error}"], path) from None
    scenario = validate_scenario(data, source=path)
    if scenario.name != stem:
        raise ScenarioError([f"name: {scenario.name!r} must equal the file stem {stem!r}"], path)
    return scenario


def load_suite(directory: str | None = None, names: Sequence[str] | None = None, *, strict: bool | None = None):
    """Every scenario in ``directory`` sorted by name (or the listed ``names``).

    The checked-in suite (strict, the default for SCENARIO_DIR) also needs at least MIN_SUITE_DAYS days per
    scenario and, when complete, every REQUIRED_TAGS tag.
    """
    directory = SCENARIO_DIR if directory is None else directory
    if strict is None:
        strict = os.path.abspath(directory) == os.path.abspath(SCENARIO_DIR)
    files = sorted(name for name in os.listdir(directory) if name.endswith(".json"))
    if not files:
        raise ScenarioError(["no scenario files (*.json)"], directory)
    scenarios = [load_scenario(os.path.join(directory, name)) for name in files]
    problems = []
    if names is not None:
        unknown = sorted(set(names) - {scenario.name for scenario in scenarios})
        if unknown:
            problems.append(f"unknown scenario(s): {', '.join(unknown)}")
        scenarios = [scenario for scenario in scenarios if scenario.name in names]
    if strict:
        short = [scenario.name for scenario in scenarios if scenario.days < MIN_SUITE_DAYS]
        if short:
            problems.append(f"days < {MIN_SUITE_DAYS}: {', '.join(short)}")
        missing = sorted(REQUIRED_TAGS - {tag for scenario in scenarios for tag in scenario.tags})
        if names is None and missing:
            problems.append(f"missing required tags: {', '.join(missing)}")
    if problems:
        raise ScenarioError(problems, directory)
    return tuple(scenarios)


# -- world -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CrewMeta:
    character_id: int
    name: str
    role: str | None
    rarity: str
    capacity: int
    cap: int
    target: int  # policy_target: the most the policy can train this crew to in its room
    room_type: str | None
    eligible: bool  # a role and a training room
    initial_total: int
    headroom: int  # max(0, target - initial_total) for eligible crew, else 0 (research gates not counted)
    loaded_until: datetime | None = None  # the end of a session already running at the start
    loaded_can_gain: bool = False  # that session can gain something (see can_gain)


@dataclass(frozen=True)
class World:
    sim_scenario: Mapping[str, object]
    room_designs: tuple[Mapping[str, str], ...]
    research_designs: tuple[Mapping[str, str], ...]
    crew: Mapping[int, CrewMeta]
    rooms: Mapping[int, str]  # room id -> catalog room design name
    research: frozenset[int]  # completed research design ids (research never changes during a run)
    levels: Mapping[str, int]  # research family -> highest completed level
    start: datetime
    horizon_end: datetime
    last_tick: datetime  # horizon_end minus one tick: no tick runs after it, so nothing ending later is collected


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=timezone.utc)


def _seconds(delta: timedelta) -> int:
    return delta // timedelta(seconds=1)


def _sim_fault(entry: Mapping[str, object]) -> dict:
    fault = {key: value for key, value in entry.items() if key != "training_design"}
    if "training_design" in entry:
        fault["training_design_id"] = str(CATALOG_TRAININGS[entry["training_design"]][0])
    return fault


def room_design_records() -> tuple[dict, ...]:
    records = []
    for name, (design_id, _, upgrade_from) in CATALOG_ROOMS.items():
        record = {"@RoomDesignId": str(design_id), "@RoomName": name}
        if upgrade_from is not None:
            record["@UpgradeFromRoomDesignId"] = str(upgrade_from)
        records.append(record)
    return tuple(records)


def research_design_records() -> tuple[dict, ...]:
    return tuple(
        {"@ResearchDesignId": str(design_id), "@ResearchName": name, "@RootResearchDesignId": str(root)}
        for name, (design_id, root) in CATALOG_RESEARCH.items()
    )


def training_design_records() -> list[dict]:
    records = []
    for name, (design_id, stat, room, tier) in CATALOG_TRAININGS.items():
        duration, fatigue, low, high = TIER_PARAMS[tier]
        room_id, room_type, _ = CATALOG_ROOMS[room]
        records.append(
            {
                "@TrainingDesignId": str(design_id),
                "@TrainingName": name,
                "@Rank": str(tier),
                "@TrainingStat": stat,
                f"@{stat}Chance": "100",
                "@Duration": str(duration),
                "@Fatigue": str(fatigue),
                "@MinGain": str(low),
                "@MaxGain": str(high),
                "@RequiredRoomDesignId": str(room_id),
                "@RequiredRoomType": ROOM_TYPE_WORDS[room_type],
                "@RequiredResearchDesignId": str(TIER_RESEARCH.get((room, tier), 0)),
            }
        )
    return records


def character_design_records() -> list[dict]:
    return [
        {
            "@CharacterDesignId": str(design_id),
            "@CharacterDesignName": key.title(),
            "@Rarity": rarity,
            "@TrainingCapacity": str(capacity),
        }
        for key, (design_id, rarity, capacity) in CATALOG_CHARACTERS.items()
    ]


def item_design_records() -> list[dict]:
    return [
        {
            "@ItemDesignId": str(design_id),
            "@ItemName": name,
            "@ItemSubType": "Consumable",
            "@Rarity": rarity,
            "@ConsumableStat": stat,
            "@ConsumableGain": str(gain),
            "@ConsumableFatigue": str(fatigue),
            "@FatigueRecovery": str(recovery),
        }
        for name, (design_id, rarity, stat, gain, fatigue, recovery) in CATALOG_ITEMS.items()
    ]


def _crew_record(spec: CrewSpec, start: datetime) -> dict:
    design_id = CATALOG_CHARACTERS[spec.design][0]
    record = {
        "@CharacterId": str(spec.id),
        "@CharacterDesignId": str(design_id),
        "@CharacterName": spec.name,
        "@RoomId": str(spec.room),
        "@Level": str(spec.level),
        "@Fatigue": str(spec.fatigue),
        "@TrainingDesignId": "0",
        "@TrainingEndDate": "",
    }
    if spec.training is not None:
        name, minutes = spec.training
        record["@TrainingDesignId"] = str(CATALOG_TRAININGS[name][0])
        record["@TrainingEndDate"] = format_time(start + timedelta(minutes=minutes))
    for stat in SIM_STATS:
        record[f"@{stat}Improvement"] = str(spec.stats.get(stat, 0))
    return record


def _crew_meta(spec: CrewSpec, rooms: Mapping[int, str], start: datetime) -> CrewMeta:
    _, rarity, capacity = CATALOG_CHARACTERS[spec.design]
    role = ORACLE_ROLES.get(spec.name)
    room_type = CATALOG_ROOMS[rooms[spec.room]][1] if spec.room in rooms else None
    cap = oracle_cap(rarity, spec.name, capacity)
    eligible = role is not None and room_type is not None
    total = sum(spec.stats.values())
    target = policy_target(role, room_type, total, capacity, cap)
    headroom = max(0, target - total) if eligible else 0
    loaded = None if spec.training is None else start + timedelta(minutes=spec.training[1])
    possible = spec.training is not None and can_gain(CATALOG_TRAININGS[spec.training[0]][0], spec.fatigue, total,
                                                      capacity)
    return CrewMeta(spec.id, spec.name, role, rarity, capacity, cap, target, room_type, eligible, total, headroom,
                    loaded, possible)


def _account(scenario: Scenario) -> dict:
    return {
        "rooms": [
            {"@RoomId": str(room.id), "@RoomDesignId": str(CATALOG_ROOMS[room.design][0]), "@RoomStatus": "Normal"}
            for room in scenario.rooms
        ],
        "research": [
            {"@ResearchDesignId": str(CATALOG_RESEARCH[name][0]), "@ResearchState": "Completed"}
            for name in scenario.research
        ],
        "inventory": [
            {"@ItemId": str(CATALOG_ITEMS[name][0] + 1000), "@ItemDesignId": str(CATALOG_ITEMS[name][0]),
             "@Quantity": str(quantity)}
            for name, quantity in sorted(scenario.inventory, key=lambda item: CATALOG_ITEMS[item[0]][0])
        ],
        "crew": [_crew_record(spec, scenario.start) for spec in scenario.crew],
        "faults": [_sim_fault(fault) for fault in scenario.faults],
    }


def build_world(scenario: Scenario) -> World:
    """The simulator scenario and the per-crew oracle facts. WorldError when no eligible crew has headroom under
    its policy target: the TP cap, the complete percent and the room. Research gates are not counted (see
    policy_target), so a world whose crew are all research-blocked is accepted and simply scores low."""
    item_designs = item_design_records()
    found = dict(parse_consumable_designs({"ItemDesign": item_designs}))
    if found != CATALOG_CONSUMABLES:
        raise WorldError(f"consumable item designs parse to {sorted(found.items())}, not CATALOG_CONSUMABLES")
    rooms = {room.id: room.design for room in scenario.rooms}
    crew = {spec.id: _crew_meta(spec, rooms, scenario.start) for spec in scenario.crew}
    if sum(meta.headroom for meta in crew.values()) <= 0:
        raise WorldError(f"{scenario.name}: no eligible crew has headroom under its policy target "
                         "(research gates are not counted)")
    room_designs, research_designs = room_design_records(), research_design_records()
    sim_scenario = {
        "design": {"room_designs": list(room_designs), "research_designs": list(research_designs)},
        "training": {
            "fatigue_max": 100,
            "fatigue_recovery_seconds": scenario.fatigue_recovery_seconds,
            "character_designs": character_design_records(),
            "training_designs": training_design_records(),
            "item_designs": item_designs,
        },
        "accounts": {ACCOUNT: _account(scenario)},
    }
    research = frozenset(CATALOG_RESEARCH[name][0] for name in scenario.research)
    horizon_end = scenario.start + timedelta(days=scenario.days)
    return World(
        sim_scenario=sim_scenario,
        room_designs=room_designs,
        research_designs=research_designs,
        crew=crew,
        rooms=rooms,
        research=research,
        levels=research_levels(sorted(research)),
        start=scenario.start,
        horizon_end=horizon_end,
        last_tick=horizon_end - timedelta(minutes=scenario.tick_minutes),
    )


# -- the simulator, its probe and the recording client ------------------------------------------------------------


class Simulation:
    """One seeded simulator account and its clock, and the probe the metrics read (training log, fired faults)."""

    def __init__(self, world: World, seed: int) -> None:
        self.world = world
        self.clock = SimulatedClock(world.start)
        design = DesignData.from_records(**world.sim_scenario["design"])
        self.game = build_games(world.sim_scenario, seed, design, self.clock)[ACCOUNT]
        # Whether a snapshot can do anything beyond counting itself: only a fault on the snapshot itself, or a read
        # fault a call leaves pending, changes or fails one (sdk.simulation.game.SimulatedGame.snapshot).
        self.snapshot_faults = any(fault.method == SNAPSHOT or fault.kind in READ_FAULTS for fault in self.game.faults)

    @property
    def now(self) -> datetime:
        return self.clock.now

    def start_day(self, day: int) -> None:
        self.clock.start_day(day)
        self.game.start_day(day)

    def advance(self, delta: timedelta) -> None:
        self.clock.advance(delta)

    def log(self) -> tuple[dict, ...]:
        return tuple(dict(entry) for entry in self.game.training.log)

    def fired(self) -> tuple[str, ...]:
        return tuple(self.game.fired)

    def crew_record(self, character_id: str) -> Mapping[str, str] | None:
        """The simulator's own record of a crew member now; None for an id it does not know."""
        if character_id not in self.game.training.members:
            return None
        return self.game.training.record(character_id, self.clock.now)

    def read_fails(self) -> bool:
        """Whether an account read now hits an injected snapshot-failure. The clock is put back (a snapshot ticks
        it), so a read-fault scenario keeps the tick times of a fault-free one. An account without snapshot faults
        builds no snapshot: it could only count itself, which no fault would ever read."""
        if not self.snapshot_faults:
            return False
        now = self.clock.now
        try:
            self.game.snapshot()
        except SimulatedFaultError:
            return True
        finally:
            self.clock.now = now
        return False


@dataclass(frozen=True)
class Before:
    """The simulator's crew record just before a training call: the ground truth every oracle judges."""

    total: int
    fatigue: int
    room_id: int
    training_design_id: int  # 0 when idle
    training_end: str  # "" when idle


def before_from_record(record: Mapping[str, str]) -> Before:
    return Before(
        total=sum(int(record[f"@{stat}Improvement"]) for stat in SIM_STATS),
        fatigue=int(record["@Fatigue"]),
        room_id=int(record["@RoomId"]),
        training_design_id=int(record["@TrainingDesignId"]),
        training_end=record["@TrainingEndDate"],
    )


@dataclass(frozen=True)
class Call:
    """One journaled training call."""

    seq: int
    tick: int
    at: str
    method: str  # addTraining | finishTraining | useConsumable
    args: tuple[str, ...]
    character_id: str
    before: Before | None  # None: the simulator does not know the character
    outcome: str  # ok | refused | ambiguous | raised
    faults: tuple[str, ...]  # the faults the call fired
    log_seqs: tuple[int, ...]  # the simulator log entries the call appended

    def to_dict(self) -> dict:
        data = asdict(self)
        data["args"], data["faults"], data["log_seqs"] = list(self.args), list(self.faults), list(self.log_seqs)
        return data


def call_outcome(result: object, faults: Sequence[str]) -> str:
    """The ground truth of a call: no reply (or a fired ambiguous-response fault behind useConsumable, which still
    answers True) is ambiguous; True is ok; anything else is a refusal."""
    if result is None or any(fault.endswith(": ambiguous-response") for fault in faults):
        return "ambiguous"
    return "ok" if result is True else "refused"


CLIENT_RESPONSES = frozenset(
    {"allCharactersOfUser", "allCharacterDesigns", "trainingDesigns", "itemsOfAShip", "trainingFinish"}
)


class RecordingClient:
    """The Client surface a tick drives: the simulator's reads and training calls, every training call journaled.

    Reads are delegated unchanged. The simulator has no room or research reads, so the four the real Client uses
    (listRoomsViaAccessToken, listRoomDesigns2, listAllResearches, listAllResearchDesigns2) are built here from the
    simulator's rooms and research and the catalog designs; Unit 9's executor makes the same calls on the real
    Client. listAllCharactersOfUser fails (returns False) when an injected snapshot-failure hits the account read.
    Any other attribute is an AttributeError, and the simulator is held in name-mangled attributes that no
    underscore name reaches, so a tick using the client surface cannot bypass the journal. (Python cannot hide
    them completely: code that spells out _RecordingClient__game is deliberately breaking the contract.)
    """

    def __init__(self, simulation: Simulation, record: Callable[[Call], None]) -> None:
        self.__simulation = simulation
        self.__game = simulation.game
        self.__record = record
        self.__seq = 0
        self.tick = 0

    def __getattr__(self, name: str):
        if name in CLIENT_RESPONSES:
            return getattr(self.__game, name)
        raise AttributeError(f"RecordingClient has no {name!r}: not a training call or read")

    # -- reads -------------------------------------------------------------------------------------------------

    def listAllCharactersOfUser(self) -> bool:
        if self.__simulation.read_fails():
            return False
        return self.__game.listAllCharactersOfUser()

    def listAllCharacterDesigns2(self) -> bool:
        return self.__game.listAllCharacterDesigns2()

    def listAllTrainingDesigns2(self) -> bool:
        return self.__game.listAllTrainingDesigns2()

    def listItemsOfAShip(self) -> bool:
        return self.__game.listItemsOfAShip()

    def listRoomsViaAccessToken(self) -> bool:
        rooms = [dict(room) for room in self.__game.rooms]
        self.roomsViaAccessToken = {"RoomService": {"ListRoomsViaAccessToken": {"Rooms": {"Room": rooms}}}}
        return True

    def listRoomDesigns2(self) -> bool:
        designs = [dict(record) for record in self.__simulation.world.room_designs]
        self.roomDesigns = {"RoomService": {"ListRoomDesigns": {"RoomDesigns": {"RoomDesign": designs}}}}
        return True

    def listAllResearches(self) -> bool:
        research = [dict(entry) for entry in self.__game.research]
        self.allResearches = {"ResearchService": {"ListAllResearches": {"Researches": {"Research": research}}}}
        return True

    def listAllResearchDesigns2(self) -> bool:
        designs = [dict(record) for record in self.__simulation.world.research_designs]
        self.allResearchDesigns = {
            "ResearchService": {"ListAllResearchDesigns": {"ResearchDesigns": {"ResearchDesign": designs}}}
        }
        return True

    # -- training calls ------------------------------------------------------------------------------------------

    def addTraining(self, training_design_id, character_id) -> object:
        return self._call("addTraining", (training_design_id, character_id), character_id)

    def finishTraining(self, character_id) -> object:
        return self._call("finishTraining", (character_id,), character_id)

    def useConsumable(self, consumable_item_design_id, character_id) -> object:
        return self._call("useConsumable", (consumable_item_design_id, character_id), character_id)

    def _call(self, method: str, args: tuple, character_id: object) -> object:
        game, simulation = self.__game, self.__simulation
        record = simulation.crew_record(str(character_id))
        before = None if record is None else before_from_record(record)
        fired, logged = len(game.fired), len(game.training.log)
        outcome = "raised"  # unless the call returns: an exception (an injected raise) propagates to the tick
        try:
            result = getattr(game, method)(*args)
            outcome = call_outcome(result, game.fired[fired:])
            return result
        finally:
            self.__seq += 1
            log_seqs = tuple(entry["seq"] for entry in game.training.log[logged:])
            self.__record(Call(self.__seq, self.tick, format_time(simulation.now), method,
                              tuple(str(arg) for arg in args), str(character_id), before, outcome,
                              tuple(game.fired[fired:]), log_seqs))


# -- the contract executor (the default tick) ---------------------------------------------------------------------


@dataclass(frozen=True)
class TickReport:
    """What a tick says about itself: diagnostics only, never scored and never trusted for an oracle."""

    skips: tuple[tuple[str, str], ...] = ()  # (reason code, detail) per skipped crew
    dropped: int = 0  # advisory decisions dropped after a finish that did not succeed
    replans: int = 0
    observe_failed: bool = False

    def to_dict(self) -> dict:
        return {"skips": [list(skip) for skip in self.skips], "dropped": self.dropped, "replans": self.replans,
                "observe_failed": self.observe_failed}


Tick = Callable[[object, datetime], "TickReport | None"]
# (client read method, response attribute, build_context keyword)
CLIENT_READS = (
    ("listAllCharactersOfUser", "allCharactersOfUser", "characters_raw"),
    ("listAllCharacterDesigns2", "allCharacterDesigns", "character_designs_raw"),
    ("listAllTrainingDesigns2", "trainingDesigns", "training_designs_raw"),
    ("listItemsOfAShip", "itemsOfAShip", "items_raw"),
    ("listRoomsViaAccessToken", "roomsViaAccessToken", "rooms_raw"),
    ("listRoomDesigns2", "roomDesigns", "room_designs_raw"),
    ("listAllResearches", "allResearches", "researches_raw"),
    ("listAllResearchDesigns2", "allResearchDesigns", "research_designs_raw"),
)


def read_payloads(client) -> dict | None:
    """build_context's payload keywords, read through the client; None when a read fails."""
    payloads = {}
    try:
        for method, attribute, keyword in CLIENT_READS:
            if getattr(client, method)() is not True:
                return None
            payloads[keyword] = getattr(client, attribute)
    except ConnectionError:
        return None
    return payloads


def execute(client, decision) -> bool:
    """Carry a decision out through its catalog action; True when the client answered True (the executor's view
    only: the call journal's outcome, never this, is what the oracles and metrics judge)."""
    request = action_for(decision)
    if request is None:
        raise BenchmarkError(f"a Skip is never executed: {decision!r}")
    action_id, params = request
    spec = get_spec(action_id)
    try:
        return getattr(client, spec.client_method)(*call_arguments(spec, params)) is True
    except ConnectionError:
        return False


def rebuilt_crew(client, character_id: int, payloads: Mapping[str, object]) -> CrewState | None:
    """The crew rebuilt from the last FinishTraining response; None unless it is exactly that crew."""
    rebuilt = parse_crew(client.trainingFinish, payloads["character_designs_raw"], payloads["rooms_raw"],
                         payloads["room_designs_raw"])
    return rebuilt[0] if len(rebuilt) == 1 and rebuilt[0].character_id == character_id else None


def carry_out(client, ctx, plan, replanner, strategy: TrainingStrategy, payloads: Mapping) -> TickReport:
    """Execute a plan: finish, replan each collected crew with the remaining stock, run every other decision."""
    finished = {decision.character_id for decision in plan if isinstance(decision, FinishTraining)}
    stock = remaining_stock(ctx.items, plan, exclude_character_ids=finished)
    collected: set[int] = set()
    skips: list[tuple[str, str]] = []
    dropped = replans = 0
    for decision in plan:
        cid = decision.character_id
        if isinstance(decision, FinishTraining):
            if execute(client, decision):
                collected.add(cid)
            continue
        if cid in finished:  # the advisory primary after a FinishTraining is never run as-is
            crew = rebuilt_crew(client, cid, payloads) if cid in collected else None
            if crew is None:
                dropped += 1
                continue
            (decision,) = replanner(ctx, crew, strategy, stock)
            stock = remaining_stock(stock, (decision,))
            replans += 1
        if isinstance(decision, Skip):
            skips.append((decision.reason_code, decision.detail))
            continue
        execute(client, decision)
    return TickReport(tuple(skips), dropped, replans)


def default_strategy() -> TrainingStrategy:
    """DEFAULT_STRATEGY with the catalog's consumable ids attached (the only strategy change the benchmark makes)."""
    return with_consumables(DEFAULT_STRATEGY, CATALOG_CONSUMABLES)


def make_contract_executor(planner=None, replanner=None, strategy: TrainingStrategy | None = None) -> Tick:
    """The documented executor contract as a tick: read through the client, build_context, plan_training, carry
    every decision out through the action catalog, and replan each finished crew with replan_after_finish and the
    remaining stock. Unit 9's executor (sdk/execution/training.py) replaces this tick, nothing else."""
    planner = plan_training if planner is None else planner
    replanner = replan_after_finish if replanner is None else replanner
    strategy = default_strategy() if strategy is None else strategy

    def tick(client, now: datetime) -> TickReport:
        payloads = read_payloads(client)
        if payloads is None:
            return TickReport(observe_failed=True)
        ctx = build_context(now=now, **payloads)
        return carry_out(client, ctx, planner(ctx, strategy), replanner, strategy, payloads)

    return tick


# -- episodes ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Episode:
    scenario: Scenario
    world: World
    seed: int
    journal: tuple[Call, ...]
    log: tuple[Mapping[str, object], ...]
    fired: tuple[str, ...]
    reports: tuple[TickReport, ...]


def run_episode(scenario: Scenario, *, seed: int | None = None, tick: Tick | None = None,
                days: int | None = None) -> Episode:
    """Run one scenario for its days (or ``days``) at its tick cadence: ``tick(client, now)`` once per tick."""
    if days is not None:
        scenario = replace(scenario, days=days)
    seed = scenario.seed if seed is None else seed
    tick = make_contract_executor() if tick is None else tick
    world = build_world(scenario)
    simulation = Simulation(world, seed)
    journal: list[Call] = []
    client = RecordingClient(simulation, journal.append)
    reports: list[TickReport] = []
    step = timedelta(minutes=scenario.tick_minutes)
    for day in range(scenario.days):
        simulation.start_day(day)
        for _ in range(1440 // scenario.tick_minutes):
            client.tick += 1
            report = tick(client, simulation.now)
            reports.append(TickReport() if report is None else report)
            simulation.advance(step)
    return Episode(scenario, world, seed, tuple(journal), simulation.log(), simulation.fired(), tuple(reports))


# -- oracles: pure functions of the world and the call journal ----------------------------------------------------


@dataclass(frozen=True)
class PolicyView:
    """What the owner-approved policy sees for one crew just before a call (the benchmark's own reading)."""

    role: str | None
    room_type: str | None
    total: int
    pct: int
    cap: int
    fatigue: int
    in_progress: bool
    band: tuple[str, int] | None
    slot_room: str | None  # the room type of the role's slot for the band
    expected_design: int | None  # the role's training for the band's slot and tier
    gate_ok: bool
    design_research_ok: bool  # the expected design's own research is completed

    @property
    def placed(self) -> bool:
        """A role, a training room and room under the cap: the policy considers the crew at all."""
        return self.role is not None and self.room_type is not None and self.total < self.cap

    @property
    def startable(self) -> bool:
        return (
            self.placed
            and not self.in_progress
            and self.band is not None
            and self.room_type == self.slot_room
            and self.gate_ok
            and self.fatigue < ORACLE_MAX_TRAINING_FATIGUE
            and self.design_research_ok
        )

    @property
    def consumable_branch(self) -> bool:
        """The precedence reaches a consumable fallback: in progress, complete, or nothing startable in the right
        room type (gate, fatigue, design or design research). Never while a start is possible or on a move."""
        if not self.placed:
            return False
        if self.in_progress or self.band is None:
            return True
        return self.room_type == self.slot_room and not self.startable


def policy_view(world: World, meta: CrewMeta, before: Before, at: str) -> PolicyView:
    room_name = world.rooms.get(before.room_id)
    room_type = CATALOG_ROOMS[room_name][1] if room_name is not None else None
    pct = percent(before.total, meta.capacity)
    band = oracle_band(pct)
    slot_room, expected = role_training(meta.role, band)
    required = None if expected is None else design_research(expected)
    in_progress = before.training_design_id != 0 and parse_time(before.training_end) > parse_time(at)
    return PolicyView(
        role=meta.role,
        room_type=room_type,
        total=before.total,
        pct=pct,
        cap=meta.cap,
        fatigue=before.fatigue,
        in_progress=in_progress,
        band=band,
        slot_room=slot_room,
        expected_design=expected,
        gate_ok=band is None or gate_met(band, world.levels),
        design_research_ok=required is None or required in world.research,
    )


def start_violations(world: World, view: PolicyView, design_id: int) -> list[tuple[str, str]]:
    """Each policy rule a StartTraining of ``design_id`` breaks, judged on the crew before the call."""
    found = []
    if view.total >= view.cap:
        found.append(("cap", f"start at {view.total} TP >= cap {view.cap} TP"))
    if view.band is None:
        found.append(("complete", f"start at {view.pct}% >= {ORACLE_COMPLETE_PERCENT}%"))
    if view.fatigue >= ORACLE_MAX_TRAINING_FATIGUE:
        found.append(("fatigue", f"start at fatigue {view.fatigue} >= {ORACLE_MAX_TRAINING_FATIGUE}"))
    if view.band is not None and not view.gate_ok:
        found.append(("research-gate", f"{view.band[0]} T{view.band[1]} gate unmet at {view.pct}%"))
    if view.room_type is None or view.slot_room not in (None, view.room_type):
        found.append(("room", f"start in room type {view.room_type} for a {view.slot_room} slot"))
    if view.role is None or (view.band is not None and design_id != view.expected_design):
        found.append(("design", f"design {design_id} is not the role's training {view.expected_design}"))
    required = design_research(design_id)
    if required is not None and required not in world.research:
        found.append(("design-research", f"design {design_id} needs research {required}"))
    return found


def consumable_violations(view: PolicyView, item_id: int) -> list[tuple[str, str]]:
    """Each policy rule a UseConsumable of ``item_id`` breaks, judged on the crew before the call."""
    found = []
    if view.total >= view.cap:
        found.append(("cap", f"consumable at {view.total} TP >= cap {view.cap} TP"))
    tier = CONSUMABLE_TIERS.get(item_id)
    problems = []
    if tier is None:
        problems.append(f"item {item_id} is not a catalog consumable")
    elif not consumable_allowed(tier, view.pct, view.fatigue):
        problems.append(f"{tier} rule does not allow {view.pct}% with fatigue {view.fatigue}")
    if view.role is None:
        problems.append("no role")
    if view.room_type is None:
        problems.append("not in a training room")
    if problems:
        found.append(("consumable", "; ".join(problems)))
    if view.placed and not view.consumable_branch:
        reason = "a start was possible" if view.startable else f"the crew should move to {view.slot_room}"
        found.append(("consumable-precedence", reason))
    return found


@dataclass(frozen=True)
class Violation:
    kind: str
    seq: int
    at: str
    character_id: int
    detail: str


def journal_violations(world: World, journal: Sequence[Call]) -> tuple[Violation, ...]:
    """Every hard-violation finding over the journal; a finish, or a call for an unknown crew, is never judged."""
    found = []
    for call in journal:
        meta = world.crew.get(_to_int(call.character_id))
        if call.method == "finishTraining" or call.before is None or meta is None:
            continue
        view = policy_view(world, meta, call.before, call.at)
        target = _to_int(call.args[0])
        if call.method == "addTraining":
            rules = start_violations(world, view, target)
        else:
            rules = consumable_violations(view, target)
        found += [Violation(kind, call.seq, call.at, meta.character_id, detail) for kind, detail in rules]
    return tuple(found)


# -- metrics: pure functions of the world, the journal and the simulator log ---------------------------------------


@dataclass(frozen=True)
class Session:
    character_id: int
    start: datetime
    end: datetime
    gain: int | None  # None: not collected by the horizon
    can_gain: bool  # the best draw at its starting fatigue, under the capacity, gains something (and, when
    # uncollected, it ended after the last tick, so no tick could collect it)
    loaded: bool = False  # already running at the start


def can_gain(design_id: int, fatigue: int, total: int, capacity: int) -> bool:
    """Whether a session can gain anything: the simulator scales the draw by (100 - fatigue at start) / 100."""
    entry = TRAININGS_BY_ID.get(design_id)
    best = 0 if entry is None else TIER_PARAMS[entry[2]][3] * (100 - fatigue) // 100
    return best > 0 and total < capacity


def episode_sessions(world: World, log: Sequence[Mapping[str, object]],
                     journal: Sequence[Call]) -> tuple[Session, ...]:
    """Every training session, from accepted starts (and the sessions loaded at the start) to their finishes. The
    fatigue a session started with is the journaled record from just before its start."""
    before = {seq: call.before for call in journal for seq in call.log_seqs}
    open_sessions = {
        cid: (world.start, meta.loaded_until, True, meta.loaded_can_gain)
        for cid, meta in world.crew.items() if meta.loaded_until
    }
    sessions = []
    for entry in log:
        if not entry["accepted"] or entry["op"] == "consume":
            continue
        cid = int(entry["character_id"])
        if entry["op"] == "start":
            record = before.get(entry["seq"])
            possible = record is not None and can_gain(_to_int(entry["design_id"]), record.fatigue, record.total,
                                                       world.crew[cid].capacity)
            open_sessions[cid] = (parse_time(entry["at"]), parse_time(entry["end"]), False, possible)
        elif cid in open_sessions:
            start, end, loaded, possible = open_sessions.pop(cid)
            sessions.append(Session(cid, start, end, sum(entry["gain"].values()), possible, loaded))
    # An uncollected session that ended by the last tick gained nothing: a tick could have collected it and none
    # did. One that ended later (still running at the horizon, or ending between the last tick and the horizon)
    # could not be collected, so it counts when it can gain something.
    sessions += [Session(cid, start, end, None, possible and end > world.last_tick, loaded)
                 for cid, (start, end, loaded, possible) in open_sessions.items()]
    return tuple(sorted(sessions, key=lambda session: (session.character_id, session.start)))


def productive(session: Session) -> bool:
    """A session that gained something; one still running at the horizon counts when it can gain something."""
    return session.can_gain if session.gain is None else session.gain > 0


def covered_seconds(window: tuple[datetime, datetime], intervals: Sequence[tuple[datetime, datetime]]) -> int:
    """Seconds of ``window`` the intervals cover (clipped to the window, never double-counted)."""
    start, end = window
    covered = 0
    cursor = start
    for low, high in sorted(intervals):
        low, high = max(low, cursor), min(high, end)
        if high > low:
            covered += _seconds(high - low)
            cursor = high
    return covered


def done_times(world: World, log: Sequence[Mapping[str, object]]) -> dict[int, datetime]:
    """When each crew with headroom first reached its policy target (an accepted finish or item use); the horizon
    for one that never did. One pass over the log, shared by U and crew_completed."""
    done = {cid: world.horizon_end for cid, meta in world.crew.items() if meta.headroom > 0}
    reached: set[int] = set()
    for entry in log:
        cid = _to_int(entry["character_id"])  # a refusal logs whatever id the executor sent
        if cid in done and cid not in reached and entry["accepted"] and entry["op"] != "start" \
                and entry["total"] >= world.crew[cid].target:
            done[cid] = parse_time(entry["at"])
            reached.add(cid)
    return done


def utilisation_seconds(world: World, done: Mapping[int, datetime],
                        sessions: Sequence[Session]) -> tuple[int, int]:
    """(window, productive) crew-seconds over the eligible crew with headroom: from the start until each reaches its
    target (``done``, from done_times), the time covered by sessions that gained something. Zero-gain sessions are
    wasted, not busy."""
    window_s = busy_s = 0
    for cid, meta in sorted(world.crew.items()):
        if meta.headroom <= 0:
            continue
        window = (world.start, done[cid])
        window_s += _seconds(window[1] - window[0])
        busy = [(s.start, s.end) for s in sessions if s.character_id == cid and productive(s)]
        busy_s += covered_seconds(window, busy)
    return window_s, busy_s


def progress_area(world: World, log: Sequence[Mapping[str, object]]) -> float:
    """P: the area under the reachable-progress curve, per log event, as a fraction of headroom x horizon.

    Each crew's progress is min(headroom, total - initial total); it changes only at accepted finishes and item
    uses, so the area is exact: every gain counts for the time left until the horizon.
    """
    headroom = sum(meta.headroom for meta in world.crew.values())
    span = _seconds(world.horizon_end - world.start)
    if headroom <= 0:
        return 0.0
    reached: dict[int, int] = {}
    area = 0
    for entry in log:
        if not entry["accepted"] or entry["op"] == "start":
            continue
        meta = world.crew[int(entry["character_id"])]
        now = min(meta.headroom, max(0, entry["total"] - meta.initial_total))
        area += (now - reached.get(meta.character_id, 0)) * _seconds(world.horizon_end - parse_time(entry["at"]))
        reached[meta.character_id] = now
    return area / (headroom * span)


def gains_by_crew(log: Sequence[Mapping[str, object]]) -> dict[int, int]:
    gains: dict[int, int] = {}
    for entry in log:
        if entry["accepted"]:
            cid = int(entry["character_id"])
            gains[cid] = gains.get(cid, 0) + sum(entry["gain"].values())
    return gains


def _count(keys) -> dict[str, int]:
    """Key -> occurrences, sorted by key (a plain dict, so the report is stable)."""
    return dict(sorted(Counter(keys).items()))


def tick_diagnostics(reports: Sequence[TickReport]) -> dict:
    skips = [skip for report in reports for skip in report.skips]
    return {
        "skip_reasons": _count(reason for reason, _ in skips),
        "skip_moves": sum(1 for reason, detail in skips
                          if reason == SKIP_NOT_IN_TRAINING_ROOM and detail.startswith("move to")),
        "dropped": sum(report.dropped for report in reports),
        "replans": sum(report.replans for report in reports),
        "observe_failures": sum(1 for report in reports if report.observe_failed),
    }


def journal_counts(world: World, journal: Sequence[Call]) -> dict:
    ok = [call for call in journal if call.outcome == "ok"]
    refused = [call for call in journal
               if call.outcome == "refused" and not any(fault.endswith(": reject") for fault in call.faults)]
    starts = [call for call in ok if call.method == "addTraining" and call.before is not None]
    items = [_to_int(call.args[0]) for call in ok if call.method == "useConsumable"]
    return {
        "accepted_actions": len(ok),
        "refused_actions": len(refused),
        "actions": {method: sum(1 for call in journal if call.method == method)
                    for method in ("addTraining", "finishTraining", "useConsumable")},
        "outcomes": _count(call.outcome for call in journal),
        "raised": sum(1 for call in journal if call.outcome == "raised"),
        "ambiguous": sum(1 for call in journal if call.outcome == "ambiguous"),
        "starts_by_band": _count(band_label(percent(call.before.total, world.crew[_to_int(call.character_id)].capacity))
                                 for call in starts),
        "consumables_by_tier": _count(CONSUMABLE_TIERS.get(item, "other") for item in items),
        "consumables_used": len(items),
    }


def log_counts(world: World, log: Sequence[Mapping[str, object]], fired: Sequence[str]) -> dict:
    accepted = [entry for entry in log if entry["accepted"]]
    return {
        "refusal_codes": _count(entry["code"] for entry in log if not entry["accepted"]),
        "contradicted": sum(1 for entry in accepted
                            if entry["op"] == "start" and entry["design_id"] != entry["requested_design_id"]),
        "sessions_by_room": _count(world.rooms.get(int(entry["room_id"]), "none") for entry in accepted
                                   if entry["op"] == "start"),
        "fault_rejected": sum(1 for fault in fired if fault.endswith(": reject")),
        "read_faults": sum(1 for fault in fired if fault.endswith(": snapshot-failure")),
    }


@dataclass(frozen=True)
class Metrics:
    progress: float = 0.0  # P: reachable-progress area, per log event
    utilisation: float | None = None  # U: productive / window crew time; None when no crew had a window
    accepted_actions: int = 0
    refused_actions: int = 0  # refusals not caused by an injected reject fault
    violations: Mapping[str, int] = field(default_factory=dict)  # oracle kind -> findings
    violation_samples: tuple[Mapping[str, object], ...] = ()
    eligible_crew: int = 0
    headroom: int = 0
    tp_gained: int = 0
    tp_gained_eligible: int = 0
    tp_per_crew_day: float = 0.0
    crew_completed: int = 0
    eligible_crew_hours: float = 0.0
    productive_crew_hours: float = 0.0
    idle_crew_hours: float = 0.0
    actions: Mapping[str, int] = field(default_factory=dict)
    outcomes: Mapping[str, int] = field(default_factory=dict)
    refusal_codes: Mapping[str, int] = field(default_factory=dict)
    zero_gain_sessions: int = 0
    wasted_actions: int = 0
    fault_rejected: int = 0
    contradicted: int = 0
    raised: int = 0
    ambiguous: int = 0
    read_faults: int = 0
    observe_failures: int = 0
    dropped: int = 0
    replans: int = 0
    cap_overshoot_tp: int = 0
    skip_reasons: Mapping[str, int] = field(default_factory=dict)
    skip_moves: int = 0
    starts_by_band: Mapping[str, int] = field(default_factory=dict)
    sessions_by_room: Mapping[str, int] = field(default_factory=dict)
    consumables_by_tier: Mapping[str, int] = field(default_factory=dict)
    consumables_used: int = 0
    loaded_sessions_collected: int = 0

    @property
    def reliability(self) -> float:
        attempted = self.accepted_actions + self.refused_actions
        return self.accepted_actions / attempted if attempted else 1.0

    @property
    def hard_violations(self) -> int:
        return sum(self.violations.get(kind, 0) for kind in GATED_VIOLATIONS)

    def to_dict(self) -> dict:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        for name, value in data.items():
            if isinstance(value, Mapping):
                data[name] = dict(sorted(value.items()))
        data["violation_samples"] = [dict(entry) for entry in self.violation_samples]
        data["reliability"] = round(self.reliability, 6)
        data["progress"] = round(self.progress, 6)
        data["utilisation"] = None if self.utilisation is None else round(self.utilisation, 6)
        return data


def compute_metrics(episode: Episode) -> Metrics:
    """Every metric of one episode, from the journal, the simulator log and the fired faults (the tick reports add
    diagnostics only)."""
    world, log = episode.world, episode.log
    sessions = episode_sessions(world, log, episode.journal)
    done = done_times(world, log)
    window_s, busy_s = utilisation_seconds(world, done, sessions)
    gains = gains_by_crew(log)
    final = {cid: meta.initial_total + gains.get(cid, 0) for cid, meta in world.crew.items()}
    eligible = [meta for meta in world.crew.values() if meta.eligible]
    tp_eligible = sum(final[meta.character_id] - meta.initial_total for meta in eligible)
    crew_days = len(eligible) * (_seconds(world.horizon_end - world.start) // 86400)
    violations = journal_violations(world, episode.journal)
    zero_gain = sum(1 for session in sessions if session.gain == 0)
    counts = journal_counts(world, episode.journal)
    return Metrics(
        progress=progress_area(world, log),
        utilisation=busy_s / window_s if window_s else None,
        violations={kind: sum(1 for found in violations if found.kind == kind) for kind in GATED_VIOLATIONS},
        violation_samples=tuple(asdict(found) for found in violations[:MAX_VIOLATION_SAMPLES]),
        eligible_crew=len(eligible),
        headroom=sum(meta.headroom for meta in world.crew.values()),
        tp_gained=sum(gains.values()),
        tp_gained_eligible=tp_eligible,
        tp_per_crew_day=round(tp_eligible / crew_days, 6) if crew_days else 0.0,
        crew_completed=sum(1 for time in done.values() if time < world.horizon_end),
        eligible_crew_hours=round(window_s / 3600, 6),
        productive_crew_hours=round(busy_s / 3600, 6),
        idle_crew_hours=round((window_s - busy_s) / 3600, 6),
        zero_gain_sessions=zero_gain,
        wasted_actions=counts["refused_actions"] + zero_gain,
        cap_overshoot_tp=sum(max(0, final[cid] - max(meta.cap, meta.initial_total)) for cid, meta in world.crew.items()),
        loaded_sessions_collected=sum(1 for session in sessions if session.loaded and session.gain is not None),
        **counts,
        **log_counts(world, log, episode.fired),
        **tick_diagnostics(episode.reports),
    )


def score_scenario(metrics: Metrics | None) -> float:
    """SCORE_FORMULA, rounded to 6 places: 0.0 for an error or any gated violation."""
    if metrics is None or metrics.hard_violations:
        return 0.0
    if metrics.utilisation is None:
        weighted = metrics.progress
    else:
        weighted = SCORE_WEIGHTS["progress"] * metrics.progress + SCORE_WEIGHTS["utilisation"] * metrics.utilisation
    return round(100 * metrics.reliability * weighted, 6)


def composite(scores: Sequence[float]) -> float:
    """The mean, rounded to 6 places (0.0 for nothing)."""
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores), 6)


def trace_digest(episode: Episode) -> str:
    """sha256 of the call journal, the simulator log and the tick reports: any behaviour change moves it."""
    payload = json.dumps({"journal": [call.to_dict() for call in episode.journal], "log": list(episode.log),
                          "reports": [report.to_dict() for report in episode.reports]}, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# -- scenario tags against evidence -------------------------------------------------------------------------------

# tag -> (metric path, minimum) claims the default planner's run must show (tests/test_training_benchmark.py)
TAG_EVIDENCE: Mapping[str, tuple[tuple[str, int], ...]] = {
    "fresh": (("starts_by_band.primary-1", 1), ("starts_by_band.primary-2", 1), ("starts_by_band.primary-3", 1),
              ("skip_moves", 1)),
    "mid-level": (("starts_by_band.primary-2", 1), ("starts_by_band.primary-3", 1),
                  ("starts_by_band.secondary-1", 1), ("starts_by_band.secondary-2", 1)),
    "near-cap": (("skip_reasons.tp_cap_reached", 24), ("cap_overshoot_tp", 1), ("crew_completed", 1)),
    "missing-research": (("skip_reasons.research_missing", 24), ("consumables_by_tier.Common", 1),
                         ("starts_by_band.secondary-1", 1)),
    "mixed-rooms": (("skip_moves", 1), ("sessions_by_room.Galaxy Gym", 1), ("sessions_by_room.Lunar College", 1)),
    "no-training-room": (("skip_reasons.not_in_training_room", 24), ("skip_reasons.unknown_role", 24)),
    "fatigued": (("skip_reasons.fatigued", 24), ("zero_gain_sessions", 1), ("consumables_by_tier.Hero", 1)),
    "consumables": (("consumables_by_tier.Common", 1), ("consumables_by_tier.Hero", 1)),
    "in-training": (("loaded_sessions_collected", 1),),
    "research-split": (("skip_reasons.research_missing", 24), ("starts_by_band.primary-2", 1)),
    "multi-day": (("eligible_crew", 12), ("crew_completed", 1), ("sessions_by_room.GYM", 1),
                  ("sessions_by_room.Academy", 1), ("sessions_by_room.Galaxy Gym", 1),
                  ("sessions_by_room.Lunar College", 1)),
}
# fault tag -> (fault kind, metric that must equal the number of scenario faults of that kind)
FAULT_TAG_COUNTERS = {
    "fault-reject": ("reject", "fault_rejected"),
    "fault-contradicted": ("contradicted-effect", "contradicted"),
    "fault-raise": ("raise", "raised"),
    "fault-ambiguous": ("ambiguous-response", "ambiguous"),
    "fault-snapshot": ("snapshot-failure", "read_faults"),
}


def metric_value(metrics: Metrics, path: str) -> int:
    """A metric by dotted path ("skip_reasons.fatigued"); a missing mapping key is 0."""
    name, _, key = path.partition(".")
    value = getattr(metrics, name)
    return value.get(key, 0) if key else value


def tag_evidence(scenario: Scenario, metrics: Metrics) -> list[str]:
    """Every claim of the scenario's tags that the metrics do not show (an unknown tag is itself a problem)."""
    problems = []
    for tag in scenario.tags:
        if tag in FAULT_TAG_COUNTERS:
            kind, counter = FAULT_TAG_COUNTERS[tag]
            expected = sum(1 for fault in scenario.faults if fault["kind"] == kind)
            if metric_value(metrics, counter) != expected:
                problems.append(f"{tag}: {counter} is {metric_value(metrics, counter)}, not {expected}")
        elif tag not in TAG_EVIDENCE:
            problems.append(f"{tag}: no evidence rule")
        else:
            problems += [f"{tag}: {path} is {metric_value(metrics, path)} < {minimum}"
                         for path, minimum in TAG_EVIDENCE[tag] if metric_value(metrics, path) < minimum]
    return problems


# -- suite, report, determinism, gate ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedRun:
    seed: int
    metrics: Metrics | None
    score: float
    trace_digest: str
    error: str | None = None


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    runs: tuple[SeedRun, ...]

    @property
    def score(self) -> float:
        return composite([run.score for run in self.runs])

    @property
    def spread(self) -> float:
        scores = [run.score for run in self.runs]
        return round(max(scores) - min(scores), 6) if scores else 0.0

    @property
    def error(self) -> str | None:
        return next((f"seed {run.seed}: {run.error}" for run in self.runs if run.error is not None), None)

    @property
    def trace_digest(self) -> str:
        joined = ",".join(run.trace_digest for run in self.runs)
        return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def violations(self) -> dict[str, int]:
        return {kind: sum(run.metrics.violations.get(kind, 0) for run in self.runs if run.metrics is not None)
                for kind in GATED_VIOLATIONS}


@dataclass(frozen=True)
class SuiteResult:
    scenarios: tuple[ScenarioResult, ...]

    @property
    def composite(self) -> float:
        return composite([result.score for result in self.scenarios])


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: tuple[str, ...]
    drift: tuple[str, ...] = ()


def seeds_for(scenario: Scenario, base_seed: int | None = None) -> tuple[int, ...]:
    base = scenario.seed if base_seed is None else base_seed
    return tuple(base + offset for offset in range(SEED_COUNT))


def run_seed(scenario: Scenario, seed: int, *, tick: Tick | None, days: int | None) -> SeedRun:
    """One seeded episode, scored; a crash inside it (the planner's, the tick's, or one the tick's garbage causes
    while scoring) is that run's error. A BenchmarkError is an input problem and propagates (exit 2)."""
    try:
        episode = run_episode(scenario, seed=seed, tick=tick, days=days)
        metrics = compute_metrics(episode)
        return SeedRun(seed, metrics, score_scenario(metrics), trace_digest(episode))
    except BenchmarkError:
        raise
    except Exception as error:  # a planner crash is a result, not a benchmark failure
        return SeedRun(seed, None, 0.0, "", f"{type(error).__name__}: {error}")


def run_suite(scenarios: Sequence[Scenario], *, tick: Tick | None = None, days: int | None = None,
              base_seed: int | None = None, seeds: int | None = None) -> SuiteResult:
    """Run every scenario over its seeds (``seeds`` limits how many); the scenario score is the mean."""
    results = []
    for scenario in scenarios:
        chosen = seeds_for(scenario, base_seed)[: seeds or SEED_COUNT]
        results.append(ScenarioResult(scenario.name, tuple(run_seed(scenario, seed, tick=tick, days=days)
                                                           for seed in chosen)))
    return SuiteResult(tuple(results))


def report_dict(suite: SuiteResult) -> dict:
    return {
        "schema_version": 2,
        "benchmark": "crew-training",
        "formula": SCORE_FORMULA,
        "composite": suite.composite,
        "scenarios": {
            result.name: {
                "score": result.score,
                "spread": result.spread,
                "trace_digest": result.trace_digest,
                "error": result.error,
                "seeds": [
                    {"seed": run.seed, "score": run.score, "trace_digest": run.trace_digest, "error": run.error,
                     "metrics": None if run.metrics is None else run.metrics.to_dict()}
                    for run in result.runs
                ],
            }
            for result in suite.scenarios
        },
    }


def to_json(report: Mapping) -> str:
    return json.dumps(report, sort_keys=True, indent=2) + "\n"


def child_hash_seed() -> str:
    """A fixed PYTHONHASHSEED for the determinism child that differs from this process's: 1, or 2 under 1."""
    return "2" if os.environ.get("PYTHONHASHSEED") == "1" else "1"


def child_command(args: argparse.Namespace) -> list[str]:
    """This script as a report-only run of the same scenarios, printing its JSON report."""
    command = [sys.executable, os.path.abspath(__file__), "--report-only", "--json", "-", "--quiet",
               "--scenarios-dir", os.path.abspath(args.scenarios_dir)]
    for name in args.scenario or ():
        command += ["--scenario", name]
    return command


class ChildRun:
    """The determinism child: ``command`` in its own process under another fixed PYTHONHASHSEED, started at once
    so it runs while this process runs the suite. A benchmark child (CHILD_ENV) starts no child of its own."""

    def __init__(self, command: Sequence[str], popen=subprocess.Popen, timeout: float = CHILD_TIMEOUT) -> None:
        self.timeout = timeout
        env = dict(os.environ, PYTHONHASHSEED=child_hash_seed(), PYTHONDONTWRITEBYTECODE="1", **{CHILD_ENV: "1"})
        self.process = popen(list(command), cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")

    def report(self) -> str:
        """The child's JSON report; BenchmarkError when it fails or takes longer than the timeout."""
        try:
            output, errors = self.process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()
            raise BenchmarkError(f"determinism child: no report after {self.timeout:g} s") from None
        if self.process.returncode != 0:
            tail = (errors or "").strip().splitlines()[-1:] or [""]
            raise BenchmarkError(f"determinism child: exit {self.process.returncode}: {tail[0]}")
        return output

    def close(self) -> None:
        """Never leave the child running (this run failed or was interrupted before reading its report)."""
        if self.process.poll() is None:
            self.process.kill()
            self.process.communicate()


def check_determinism(scenarios: Sequence[Scenario], runs: int = 2, start_child: Callable[[], ChildRun] | None = None,
                      **suite_kwargs) -> tuple[bool, SuiteResult]:
    """Run the suite ``runs`` times in this process, and once in the child ``start_child`` starts (another process
    under another PYTHONHASHSEED, so set-order and hash-seed nondeterminism shows); deterministic when every report
    is byte-identical. The child runs while this process does, so it adds little wall time."""
    child = None if start_child is None else start_child()
    try:
        first = run_suite(scenarios, **suite_kwargs)
        expected = to_json(report_dict(first))
        same = True
        for _ in range(runs - 1):
            if to_json(report_dict(run_suite(scenarios, **suite_kwargs))) != expected:
                same = False
        if child is not None and child.report() != expected:
            same = False
    finally:
        if child is not None:
            child.close()
    return same, first


def violation_reason(kind: str) -> str:
    return kind.replace("-", "_") + "_violation"


def gate(suite: SuiteResult, baseline: Mapping | None, *, deterministic: bool, subset: bool = False) -> GateResult:
    """Pass only when deterministic, violation- and error-free, and at or above the baseline scores (exactly)."""
    reasons: list[str] = []
    if not deterministic:
        reasons.append("nondeterministic")
    if baseline is None:
        return GateResult(False, (*reasons, "baseline_missing"))
    recorded = baseline["scenarios"]
    names = {result.name for result in suite.scenarios}
    # A full run must match the baseline's scenarios exactly; a --scenario subset must at least all be in it.
    if (names - set(recorded)) if subset else (names != set(recorded)):
        reasons.append("baseline_mismatch")
    if not subset and suite.composite < baseline["composite"]:
        reasons.append("composite_below_baseline")
    drift = []
    for result in suite.scenarios:
        entry = recorded.get(result.name)
        if result.error is not None:
            reasons.append(f"scenario_error:{result.name}")
        if entry is not None and result.score < entry["score"]:
            reasons.append(f"scenario_below_baseline:{result.name}")
        if entry is not None and result.error is None and result.trace_digest != entry["trace_digest"]:
            drift.append(result.name)  # an errored scenario has no trace: it is a scenario_error, not drift
        reasons += [f"{violation_reason(kind)}:{result.name}" for kind, count in result.violations().items() if count]
    return GateResult(not reasons, tuple(reasons), tuple(drift))


# -- baseline ----------------------------------------------------------------------------------------------------

BASELINE_SCHEMA = 2  # 2: score formula v2 (design review); a v1 baseline is not comparable
BASELINE_KEYS = ("schema_version", "benchmark", "composite", "scenarios", "lowered", "qa_eval")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
EQUIVALENT_ID_RE = re.compile(r"^[\w./-]+\.py:\d+:\d+:[a-z]+:\d+$")
DEFAULT_MIN_DETECTION_RATE = 0.9


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _score_ok(value: object) -> bool:
    return _is_number(value) and 0 <= value <= 100


def _validate_qa_eval(qa: object, problems: list[str]) -> None:
    if not isinstance(qa, Mapping) or set(qa) != {"min_detection_rate", "equivalent"}:
        problems.append("qa_eval: needs exactly min_detection_rate and equivalent")
        return
    rate = qa["min_detection_rate"]
    if not (_is_number(rate) and 0 < rate <= 1):
        problems.append(f"qa_eval.min_detection_rate: must be in (0, 1], not {rate!r}")
    if not isinstance(qa["equivalent"], list):
        problems.append("qa_eval.equivalent: must be a list")
        return
    for index, entry in enumerate(qa["equivalent"]):
        where = f"qa_eval.equivalent[{index}]"
        if not isinstance(entry, Mapping) or set(entry) != {"id", "original", "replacement", "reason"}:
            problems.append(f"{where}: needs exactly id, original, replacement and reason")
            continue
        if not (isinstance(entry["id"], str) and EQUIVALENT_ID_RE.match(entry["id"])):
            problems.append(f"{where}.id: {entry['id']!r} is not a mutant id")
        if not (isinstance(entry["original"], str) and isinstance(entry["replacement"], str)):
            problems.append(f"{where}: original and replacement must be strings")
        if not (isinstance(entry["reason"], str) and entry["reason"].strip()):
            problems.append(f"{where}.reason: must be a non-empty string")


def _validate_baseline_scenarios(scenarios: object, problems: list[str]) -> None:
    if not isinstance(scenarios, Mapping):
        problems.append("scenarios: must be an object")
        return
    for name, entry in scenarios.items():
        if not isinstance(entry, Mapping) or set(entry) != {"score", "trace_digest"}:
            problems.append(f"scenarios.{name}: needs exactly score and trace_digest")
            continue
        if not _score_ok(entry["score"]):
            problems.append(f"scenarios.{name}.score: must be a number in [0, 100]")
        if not (isinstance(entry["trace_digest"], str) and DIGEST_RE.match(entry["trace_digest"])):
            problems.append(f"scenarios.{name}.trace_digest: must match {DIGEST_RE.pattern}")


def validate_baseline(data: object) -> dict:
    """The baseline itself when its schema is right; BaselineError listing every problem otherwise."""
    if not isinstance(data, Mapping):
        raise BaselineError("baseline must be a JSON object")
    problems: list[str] = []
    if set(data) != set(BASELINE_KEYS):
        problems.append(f"keys must be exactly {', '.join(BASELINE_KEYS)}")
    if data.get("schema_version") != BASELINE_SCHEMA or isinstance(data.get("schema_version"), bool):
        problems.append(f"schema_version: must be {BASELINE_SCHEMA}")
    if data.get("benchmark") != "crew-training":
        problems.append("benchmark: must be 'crew-training'")
    if not _score_ok(data.get("composite")):
        problems.append("composite: must be a number in [0, 100]")
    _validate_baseline_scenarios(data.get("scenarios"), problems)
    lowered = data.get("lowered")
    if not isinstance(lowered, list):
        problems.append("lowered: must be a list")
        lowered = []
    for index, entry in enumerate(lowered):
        if (
            not isinstance(entry, Mapping)
            or set(entry) != {"from", "to", "reason"}
            or not (_is_number(entry["from"]) and _is_number(entry["to"]))
            or not (isinstance(entry["reason"], str) and entry["reason"].strip())
        ):
            problems.append(f"lowered[{index}]: needs numbers from and to and a non-empty reason")
    _validate_qa_eval(data.get("qa_eval"), problems)
    if problems:
        raise BaselineError("; ".join(problems))
    return data


def load_baseline(path: str) -> dict | None:
    """The validated baseline, or None when the file does not exist."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return validate_baseline(data)
    except (OSError, ValueError) as error:
        raise BaselineError(f"{path}: cannot read: {error}") from None
    except BaselineError as error:
        raise BaselineError(f"{path}: {error}") from None


def update_baseline(suite: SuiteResult, baseline: Mapping | None, *, deterministic: bool, allow_lower: str | None):
    """The new baseline: only ever up, unless allow_lower gives a reason; qa_eval is always kept."""
    if not deterministic:
        raise BaselineRefused("nondeterministic: the in-process rerun or the child process gave another report")
    errors = [result.name for result in suite.scenarios if result.error is not None]
    if errors:
        raise BaselineRefused(f"scenario error in {', '.join(errors)}")
    violating = [result.name for result in suite.scenarios if any(result.violations().values())]
    if violating:
        raise BaselineRefused(f"hard violation in {', '.join(violating)}")
    new = {
        "schema_version": BASELINE_SCHEMA,
        "benchmark": "crew-training",
        "composite": suite.composite,
        "scenarios": {
            result.name: {"score": result.score, "trace_digest": result.trace_digest} for result in suite.scenarios
        },
        "lowered": [],
        "qa_eval": {"min_detection_rate": DEFAULT_MIN_DETECTION_RATE, "equivalent": []},
    }
    if baseline is None:
        return new
    new["lowered"] = [dict(entry) for entry in baseline["lowered"]]
    new["qa_eval"] = baseline["qa_eval"]
    drops = []
    # A changed scenario set makes the composites incomparable (a new scenario below the mean is not a regression);
    # the per-scenario checks below still catch every lowered or removed scenario.
    if set(new["scenarios"]) == set(baseline["scenarios"]) and suite.composite < baseline["composite"]:
        drops.append(f"composite {baseline['composite']} -> {suite.composite}")
    for name, entry in sorted(baseline["scenarios"].items()):
        if name not in new["scenarios"]:
            drops.append(f"{name} removed")
        elif new["scenarios"][name]["score"] < entry["score"]:
            drops.append(f"{name} {entry['score']} -> {new['scenarios'][name]['score']}")
    if drops:
        if allow_lower is None:
            raise BaselineRefused(f"would lower the baseline ({'; '.join(drops)}); pass --allow-lower REASON")
        if not allow_lower.strip():
            raise BaselineRefused("--allow-lower needs a non-empty reason")
        new["lowered"].append({"from": baseline["composite"], "to": suite.composite, "reason": allow_lower.strip()})
    return new


# -- report ------------------------------------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _scenario_rows(name: str, entry: Mapping) -> tuple[str, str]:
    metrics = [run["metrics"] for run in entry["seeds"] if run["metrics"] is not None]
    if entry["error"] is not None or not metrics:
        return f"| {name} | {entry['score']:.2f} | error: {entry['error']} |", f"| {name} | error |"
    utilisation = [m["utilisation"] for m in metrics if m["utilisation"] is not None]
    seeds = " / ".join(f"{run['score']:.2f}" for run in entry["seeds"])
    gated = sum(m["violations"].get(kind, 0) for m in metrics for kind in GATED_VIOLATIONS)
    progress = _mean([m["progress"] for m in metrics])
    main = (
        f"| {name} | {entry['score']:.2f} | {entry['spread']:.2f} | {seeds} | {_fmt(progress)} "
        f"| {_fmt(_mean(utilisation))} | {_fmt(_mean([m['reliability'] for m in metrics]))} "
        f"| {sum(m['tp_gained'] for m in metrics)} | {sum(m['zero_gain_sessions'] for m in metrics)} "
        f"| {sum(m['consumables_used'] for m in metrics)} | {gated} |"
    )
    skips: dict[str, int] = {}
    for m in metrics:
        for code, count in m["skip_reasons"].items():
            skips[code] = skips.get(code, 0) + count
    reasons = ", ".join(f"{code} {count}" for code, count in sorted(skips.items())) or "none"
    detail = f"| {name} | {reasons} | {sum(m['skip_moves'] for m in metrics)} | {sum(m['dropped'] for m in metrics)} |"
    return main, detail


def render_markdown(report: Mapping, verdict: GateResult | None) -> str:
    scenarios = report["scenarios"]
    lines = [
        "# Crew-training benchmark",
        "",
        f"Composite: **{report['composite']:.2f}** over {len(scenarios)} scenarios x {SEED_COUNT} seeds "
        f"(score = {report['formula']})",
    ]
    if verdict is None:
        lines.append("Gate: not run (report only)")
    elif verdict.passed:
        lines.append("Gate: **PASS**")
    else:
        lines.append(f"Gate: **FAIL** ({', '.join(verdict.reasons)})")
    if verdict is not None and verdict.drift:
        lines += ["", "> Warning: trace drift (behaviour changed, scores held; never a failure). Refresh the digests "
                      f"with --update-baseline when the change is intended: {', '.join(verdict.drift)}"]
    lines += [
        "",
        "| Scenario | Score | Spread | Per seed | P | U | R | TP gained | Zero-gain sessions | Consumables "
        "| Violations |",
        "|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    details = ["", "| Scenario | Skips (reason count, all seeds) | Move skips | Dropped advisories |",
               "|---|---|---:|---:|"]
    for name, entry in sorted(scenarios.items()):
        main, detail = _scenario_rows(name, entry)
        lines.append(main)
        details.append(detail)
    return "\n".join(lines + details) + "\n"


# -- qa-eval: does the benchmark catch mutants of the planner and strategy? --------------------------------------

QA_TARGETS = ("sdk/training/planner.py", "sdk/training/strategy.py")
QA_TEST_MODULES = ("tests.test_training_planner", "tests.test_training_parse", "tests.test_training_benchmark")
QA_COPY_PATHS = (
    "sdk",
    "tests",
    "scripts/ci.py",
    "run.py",  # the mutation config copies it for the offline daily-run suite (#289); a workspace needs a superset
    "scripts/mutation.py",
    "scripts/training_benchmark.py",
    "automation/benchmark/training",
)
QA_CHANNELS = ("tests", "benchmark")
BENCH_DETECTED = ("detected", "crash", "timeout")
TESTS_KILLED = ("failed", "timeout")
QA_CLASSES = (
    "both", "tests-only", "benchmark-only", "invalid-strategy", "drift-only", "neither", "equivalent", "incompetent"
)
UNEXPLAINED = "unexplained - investigate"
IMPORT_PROBE = "import sdk.training"


@dataclass(frozen=True)
class BenchOutcome:
    status: str  # "pass" | "detected" | "crash" | "timeout"
    reasons: tuple[str, ...] = ()
    drift: tuple[str, ...] = ()


@dataclass(frozen=True)
class QaOutcome:
    tests: str | None = None
    bench: BenchOutcome | None = None
    invalid: bool = False  # import sdk.training failed in the mutant workspace


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def import_probe(workspace, timeout: float) -> bool:
    """Whether ``import sdk.training`` works in the workspace (a strategy that fails validation crashes it)."""
    from scripts import mutation

    os.makedirs(os.path.join(str(workspace), mutation.CHILD_TMP), exist_ok=True)
    try:
        result = subprocess.run([sys.executable, "-c", IMPORT_PROBE], cwd=str(workspace),
                                env=mutation.child_environment(workspace), stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover - an import that hangs is left to the channels
        return True
    return result.returncode == 0


class BenchSubprocessRunner:
    """Runs the gate from a mutant workspace in its own process (the mutated planner is only importable there)."""

    def __init__(self, popen=subprocess.Popen, kill=None) -> None:
        self.popen = popen
        self.kill = kill
        self._lock = threading.Lock()
        self._live: set = set()
        self._closed = False

    def _kill(self, process) -> None:
        if self.kill is not None:
            self.kill(process)
        else:  # pragma: no cover - OS process-group glue
            from scripts import mutation

            mutation.kill_process_tree(process)

    def __call__(self, workspace, timeout: float) -> BenchOutcome:
        from scripts import mutation

        workspace = str(workspace)
        command = [sys.executable, os.path.join(workspace, "scripts", "training_benchmark.py"), "--gate", "--json",
                   "-", "--quiet"]
        os.makedirs(os.path.join(workspace, mutation.CHILD_TMP), exist_ok=True)
        if os.name == "posix":
            options: dict = {"start_new_session": True}
        else:  # pragma: no cover - Windows process groups
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        env = dict(mutation.child_environment(workspace), **{CHILD_ENV: "1"})  # its gate starts no child
        process = self.popen(command, cwd=workspace, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace", **options)
        with self._lock:
            self._live.add(process)
            closed = self._closed
        try:
            if closed:  # started after kill_all (run_mutants is stopping): never left running
                self._kill(process)
            try:
                output, errors = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill(process)
                mutation.drain_killed(process)
                return BenchOutcome("timeout")
        except BaseException:
            # Ctrl-C or SIGTERM in this thread: the child is in its own session and never saw the signal.
            self._kill(process)
            raise
        finally:
            with self._lock:
                self._live.discard(process)
        try:
            verdict = json.loads(output)["gate"]
        except (ValueError, KeyError, TypeError):
            verdict = None
        if process.returncode in (0, 1) and verdict is not None:
            status = "pass" if process.returncode == 0 else "detected"
            return BenchOutcome(status, tuple(verdict["reasons"]), tuple(verdict["drift"]))
        if process.returncode == 0:
            return BenchOutcome("pass")
        tail = (errors or "").strip().splitlines()[-1:] or [""]
        return BenchOutcome("crash", (f"exit {process.returncode}: {tail[0]}",))

    def kill_all(self) -> None:
        with self._lock:
            self._closed = True
            live = list(self._live)
        for process in live:
            self._kill(process)


class QaRunner:
    """mutation.run_mutants' test runner: the import probe, then the tests channel and the benchmark channel.

    Each mutant's outcome is kept under the sha256 of the mutated target, so the report can say which channel caught
    it; the run_mutants status is "failed" when the import probe or either channel did.
    """

    def __init__(self, rel: str, channels: Sequence[str], tests_runner, bench_runner, probe=import_probe) -> None:
        self.rel = rel
        self.channels = tuple(channels)
        self.tests_runner = tests_runner
        self.bench_runner = bench_runner
        self.probe = probe
        self.outcomes: dict[str, QaOutcome] = {}
        self._lock = threading.Lock()

    def __call__(self, workspace, tests: tuple[str, ...], timeout: float):
        from scripts import mutation

        workspace = str(workspace)
        with open(os.path.join(workspace, self.rel), "rb") as handle:
            digest = _sha256(handle.read())
        started = time.monotonic()
        if not self.probe(workspace, timeout):
            with self._lock:
                self.outcomes[digest] = QaOutcome(invalid=True)
            return mutation.RunResult("failed", time.monotonic() - started, f"{IMPORT_PROBE} failed", 1)
        share = timeout / len(self.channels)
        result = self.tests_runner(workspace, tests, share) if "tests" in self.channels else None
        outcome = self.bench_runner(workspace, share) if "benchmark" in self.channels else None
        with self._lock:
            self.outcomes[digest] = QaOutcome(None if result is None else result.status, outcome)
        tests_failed = result is not None and result.status != "passed"
        failed = tests_failed or (outcome is not None and outcome.status != "pass")
        if result is not None:
            tests_run = result.tests_run
        else:
            scenarios = os.path.join(workspace, os.path.relpath(SCENARIO_DIR, ROOT))
            tests_run = len([name for name in os.listdir(scenarios) if name.endswith(".json")])
        output = "" if result is None else result.output
        if outcome is not None:
            output += f"\nbenchmark: {outcome.status} {' '.join(outcome.reasons)}"
        return mutation.RunResult("failed" if failed else "passed", time.monotonic() - started, output, tests_run)

    def kill_all(self) -> None:
        for runner in (self.tests_runner, self.bench_runner):
            kill_all = getattr(runner, "kill_all", None)
            if callable(kill_all):
                kill_all()


def classify_mutant(status: str, outcome: QaOutcome) -> str:
    """Which channel caught a mutant: both, tests-only, benchmark-only, invalid-strategy, drift-only or neither."""
    if status in ("equivalent", "incompetent"):
        return status
    if outcome.invalid:
        return "invalid-strategy"
    tests = outcome.tests in TESTS_KILLED
    detected = outcome.bench is not None and outcome.bench.status in BENCH_DETECTED
    if tests and detected:
        return "both"
    if tests:
        return "tests-only"
    if detected:
        return "benchmark-only"
    if outcome.bench is not None and outcome.bench.drift:
        return "drift-only"
    return "neither"


def _rate(count: int, eligible: int) -> float:
    return round(count / eligible, 6) if eligible else 1.0


def summarize(rows: Sequence[Mapping], equivalent_reasons: Mapping[str, str]) -> dict:
    """Class counts and rates. Drift never counts as detected. Rates over importable mutants leave the
    invalid-strategy class out; the gated detection_rate counts it as detected (an import crash breaks the bot
    at once, so CI catches it)."""
    counts = {klass: 0 for klass in QA_CLASSES}
    for row in rows:
        counts[row["class"]] += 1
    eligible = len(rows) - counts["equivalent"] - counts["incompetent"]
    importable = eligible - counts["invalid-strategy"]
    caught = counts["both"] + counts["tests-only"] + counts["benchmark-only"]

    def change(row: Mapping) -> str:
        return f"{row['original']} -> {row['replacement']}"

    return {
        "counts": counts,
        "total": len(rows),
        "eligible": eligible,
        "importable": importable,
        "benchmark_rate": _rate(counts["both"] + counts["benchmark-only"], importable),
        "tests_rate": _rate(counts["both"] + counts["tests-only"], importable),
        "detection_rate": _rate(caught + counts["invalid-strategy"], eligible),
        "detection_rate_importable": _rate(caught, importable),
        "observable_rate": _rate(caught + counts["invalid-strategy"] + counts["drift-only"], eligible),
        "benchmark_only": [
            {"id": row["id"], "change": change(row), "reasons": list(row["reasons"])}
            for row in rows
            if row["class"] == "benchmark-only"
        ],
        "neither": [
            {"id": row["id"], "change": change(row), "note": equivalent_reasons.get(row["id"], UNEXPLAINED)}
            for row in rows
            if row["class"] == "neither"
        ],
        "drift_only": [
            {"id": row["id"], "change": change(row), "drift": list(row["drift"])}
            for row in rows
            if row["class"] == "drift-only"
        ],
        "invalid_strategy": [row["id"] for row in rows if row["class"] == "invalid-strategy"],
    }


def stale_equivalents(entries: Sequence[Mapping], generated: Sequence) -> list[str]:
    """Equivalent entries whose mutant id no longer exists or whose original/replacement text changed."""
    by_id = {mutant.id: mutant for mutant in generated}
    stale = []
    for entry in entries:
        mutant = by_id.get(entry["id"])
        if mutant is None or (mutant.original, mutant.replacement) != (entry["original"], entry["replacement"]):
            stale.append(entry["id"])
    return stale


def _qa_rows(report, runner: QaRunner, source: str) -> list[dict]:
    from scripts import mutation

    rows = []
    for result in report.results:
        mutant = result.mutant
        outcome = QaOutcome()
        if result.status not in ("equivalent", "incompetent"):
            key = _sha256(mutation.apply_mutant(source, mutant).encode("utf-8"))
            outcome = runner.outcomes.get(key, QaOutcome())
        rows.append(
            {
                "id": mutant.id,
                "original": mutant.original,
                "replacement": mutant.replacement,
                "status": result.status,
                "tests": outcome.tests,
                "benchmark": None if outcome.bench is None else outcome.bench.status,
                "reasons": [] if outcome.bench is None else list(outcome.bench.reasons),
                "drift": [] if outcome.bench is None else list(outcome.bench.drift),
                "class": classify_mutant(result.status, outcome),
            }
        )
    return rows


def qa_eval(
    *,
    targets: Sequence[str] = QA_TARGETS,
    jobs: int = 1,
    timeout: float = 300.0,
    only: Sequence[str] | None = None,
    channels: Sequence[str] = QA_CHANNELS,
    repo_root: str = ROOT,
    baseline_path: str = BASELINE_PATH,
    tests_runner=None,
    bench_runner=None,
    probe=import_probe,
    progress=None,
) -> dict:
    """Mutate each target, run the import probe and both channels against every mutant, report who caught what."""
    from scripts import mutation

    baseline = load_baseline(baseline_path)
    qa = baseline["qa_eval"] if baseline else {"min_detection_rate": DEFAULT_MIN_DETECTION_RATE, "equivalent": []}
    reasons = {entry["id"]: entry["reason"] for entry in qa["equivalent"]}
    per_target: dict[str, dict] = {}
    all_rows: list[dict] = []
    stale_all: list[str] = []
    # The bytes run_mutants writes (no newline translation), so the digests QaRunner records match.
    sources = {rel: mutation.read_source(Path(repo_root) / rel) for rel in targets}
    generated_by = {rel: mutation.generate_mutants(rel, source) for rel, source in sources.items()}
    if only is not None:  # an id no target generates (a typo, a moved line) is an input error, never a pass
        unknown = sorted(set(only) - {mutant.id for generated in generated_by.values() for mutant in generated})
        if unknown:
            raise BenchmarkError(f"--only: no mutant has id {', '.join(unknown)}")
    for rel in targets:
        target_only = None if only is None else [mutant_id for mutant_id in only if mutant_id.startswith(rel + ":")]
        if target_only == []:
            continue
        source, generated = sources[rel], generated_by[rel]
        entries = [entry for entry in qa["equivalent"] if entry["id"].startswith(rel + ":")]
        stale = stale_equivalents(entries, generated)
        stale_all += stale
        runner = QaRunner(
            rel,
            channels,
            tests_runner if tests_runner is not None else mutation.SubprocessTestRunner(),
            bench_runner if bench_runner is not None else BenchSubprocessRunner(),
            probe,
        )
        report = mutation.run_mutants(
            rel,
            QA_TEST_MODULES,
            repo_root=repo_root,
            jobs=jobs,
            timeout=timeout,
            equivalent=[entry["id"] for entry in entries if entry["id"] not in stale],
            copy_paths=QA_COPY_PATHS,
            test_runner=runner,
            generated=generated,
            only=target_only,
            progress=progress,
        )
        rows = _qa_rows(report, runner, source)
        per_target[rel] = summarize(rows, reasons)
        all_rows += rows
    overall = summarize(all_rows, reasons)
    return {
        "targets": per_target,
        "overall": overall,
        "channels": list(channels),
        "min_detection_rate": qa["min_detection_rate"],
        "stale_equivalents": stale_all,
        "mutants": all_rows,
        "passed": overall["detection_rate"] >= qa["min_detection_rate"],
    }


def render_qa_markdown(qa: Mapping) -> str:
    overall = qa["overall"]
    lines = [
        "# Crew-training qa-eval",
        "",
        f"Benchmark channel: **{overall['benchmark_rate']:.3f}**; tests channel: **{overall['tests_rate']:.3f}** "
        f"(of {overall['importable']} importable mutants; channels run: {', '.join(qa['channels'])})",
        f"Detection: **{overall['detection_rate']:.3f}** with invalid-strategy counted as detected (gated, minimum "
        f"{qa['min_detection_rate']}); {overall['detection_rate_importable']:.3f} over importable mutants only; "
        f"observable (with drift, never gated): {overall['observable_rate']:.3f}",
        "",
        "| Target | Mutants | Incompetent | Equivalent | Invalid strategy | Tests | Benchmark | Benchmark-only "
        "| Drift-only | Neither | Detection | Importable only |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    sections = {"Caught only by the benchmark": [], "Caught by neither": [], "Drift only": []}
    for rel, summary in sorted(qa["targets"].items()):
        counts = summary["counts"]
        lines.append(
            f"| {rel} | {summary['total']} | {counts['incompetent']} | {counts['equivalent']} | "
            f"{counts['invalid-strategy']} | {counts['both'] + counts['tests-only']} | "
            f"{counts['both'] + counts['benchmark-only']} | {counts['benchmark-only']} | {counts['drift-only']} | "
            f"{counts['neither']} | {summary['detection_rate']:.3f} | {summary['detection_rate_importable']:.3f} |"
        )
        sections["Caught only by the benchmark"] += [
            f"| `{entry['id']}` | `{entry['change']}` | {', '.join(entry['reasons'])} |"
            for entry in summary["benchmark_only"]
        ]
        sections["Caught by neither"] += [
            f"| `{entry['id']}` | `{entry['change']}` | {entry['note']} |" for entry in summary["neither"]
        ]
        sections["Drift only"] += [
            f"| `{entry['id']}` | `{entry['change']}` | {', '.join(entry['drift'])} |"
            for entry in summary["drift_only"]
        ]
    for title, rows in sections.items():
        lines += ["", f"## {title}", "", "| Mutant | Change | Note |", "|---|---|---|", *rows]
    if qa["stale_equivalents"]:
        stale = [f"- `{mutant_id}`" for mutant_id in qa["stale_equivalents"]]
        lines += ["", "## Stale equivalents (ignored)", "", *stale]
    return "\n".join(lines) + "\n"


def qa_main(args: argparse.Namespace) -> int:
    from scripts import mutation

    channels = tuple(channel.strip() for channel in args.channels.split(",") if channel.strip())
    if not channels or any(channel not in QA_CHANNELS for channel in channels):
        print(f"training_benchmark: --channels must name {' and/or '.join(QA_CHANNELS)}", file=sys.stderr)
        return EXIT_ERROR
    try:
        qa = qa_eval(
            targets=tuple(args.qa_target or QA_TARGETS),
            jobs=args.jobs,
            timeout=args.timeout,
            only=args.only,
            channels=channels,
            baseline_path=args.baseline,
            progress=None if args.quiet else _qa_progress,
        )
    except (BenchmarkError, mutation.MutationError) as error:
        print(f"training_benchmark: qa-eval: {error}", file=sys.stderr)
        return EXIT_ERROR
    _emit(args, qa, render_qa_markdown(qa))
    return EXIT_OK if qa["passed"] else EXIT_GATE


def _qa_progress(result) -> None:  # pragma: no cover - interactive output
    print(f"  {result.status:11} {result.mutant.id}", file=sys.stderr, flush=True)


# -- command line ------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crew-training simulator benchmark (offline).")
    parser.add_argument("--gate", action="store_true", help="gate against the baseline (the default mode)")
    parser.add_argument("--update-baseline", action="store_true", help="write the baseline (only ever up)")
    parser.add_argument("--allow-lower", metavar="REASON", help="with --update-baseline: allow a lower baseline")
    parser.add_argument("--scenario", action="append", metavar="NAME", help="run only this scenario (repeatable)")
    parser.add_argument("--quick", action="store_true", help="2-day runs, report only (no gate)")
    parser.add_argument("--seed", type=int, help="seeds N, N+1, N+2 for every scenario, report only (no gate)")
    parser.add_argument("--report-only", action="store_true",
                        help="run the suite once and report (no gate, no baseline; the determinism child's mode)")
    parser.add_argument("--scenarios-dir", default=SCENARIO_DIR)
    parser.add_argument("--baseline", default=BASELINE_PATH)
    parser.add_argument("--json", metavar="PATH", help="write the JSON report ('-' prints it instead of markdown)")
    parser.add_argument("--markdown", metavar="PATH", help="write the markdown report")
    parser.add_argument("--summary", metavar="PATH", help="append the markdown report (e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--quiet", action="store_true", help="print nothing")
    parser.add_argument("--qa-eval", action="store_true", help="mutate the planner and strategy; measure detection")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--timeout", type=float, default=300.0, help="qa-eval seconds per mutant (split by channel)")
    parser.add_argument("--only", nargs="+", metavar="ID", help="qa-eval: only these mutant ids")
    parser.add_argument("--qa-target", nargs="+", metavar="PATH", help="qa-eval: mutate only these targets")
    parser.add_argument("--channels", default="tests,benchmark", help="qa-eval channels: tests,benchmark")
    return parser


def _write(path: str, text: str, mode: str = "w") -> None:
    with open(path, mode, encoding="utf-8") as handle:
        handle.write(text)


def _emit(args: argparse.Namespace, output: Mapping, markdown: str) -> None:
    if args.json == "-":
        sys.stdout.write(to_json(output))
    elif args.json:
        _write(args.json, to_json(output))
    if args.markdown:
        _write(args.markdown, markdown)
    if args.summary:
        _write(args.summary, markdown, "a")
    if not args.quiet and args.json != "-":
        sys.stdout.write(markdown)


def _usage_error(args: argparse.Namespace) -> str | None:
    if args.allow_lower is not None and not args.update_baseline:
        return "--allow-lower needs --update-baseline"
    if args.update_baseline and (args.quick or args.seed is not None or args.scenario):
        return "--update-baseline runs the whole suite as checked in (no --quick, --seed or --scenario)"
    if args.update_baseline and _same_path(args.baseline, BASELINE_PATH) \
            and not _same_path(args.scenarios_dir, SCENARIO_DIR):
        return ("--update-baseline with another --scenarios-dir needs its own --baseline (it would overwrite the "
                "checked-in baseline from that suite)")
    if args.qa_eval and (args.gate or args.update_baseline or args.quick or args.seed is not None or args.scenario
                         or args.report_only):
        return ("--qa-eval takes no --gate, --update-baseline, --quick, --seed, --scenario or --report-only (it would "
                "ignore them)")
    if (args.gate or args.update_baseline) and (args.quick or args.seed is not None or args.report_only):
        return "--gate and --update-baseline cannot run with --quick, --seed or --report-only (never gated)"
    if args.gate and args.update_baseline:
        return "--gate cannot run with --update-baseline"
    return None


def _same_path(first: str, second: str) -> bool:
    return os.path.abspath(first) == os.path.abspath(second)


def main(argv: list[str] | None = None, *, child: Callable[[list[str]], ChildRun] = ChildRun) -> int:
    """The command line. ``child`` starts the determinism child from its command (tests pass a fake)."""
    args = build_parser().parse_args(argv)
    problem = _usage_error(args)
    if problem:
        print(f"training_benchmark: {problem}", file=sys.stderr)
        return EXIT_ERROR
    if args.qa_eval:
        return qa_main(args)
    try:
        # The checked-in baseline is only ever written from the checked-in suite, under its strict rules.
        strict = True if args.update_baseline and _same_path(args.baseline, BASELINE_PATH) else None
        scenarios = load_suite(args.scenarios_dir, args.scenario, strict=strict)
        verdict = None
        code = EXIT_OK
        if args.report_only or args.quick or args.seed is not None:  # never gated: one run, no determinism check
            report = report_dict(run_suite(scenarios, days=2 if args.quick else None, base_seed=args.seed))
            _emit(args, report, render_markdown(report, verdict))
            return code
        start_child = None if os.environ.get(CHILD_ENV) else lambda: child(child_command(args))
        deterministic, suite = check_determinism(scenarios, start_child=start_child)
        report = report_dict(suite)
        if args.update_baseline:
            new = update_baseline(suite, load_baseline(args.baseline), deterministic=deterministic,
                                  allow_lower=args.allow_lower)
            _write(args.baseline, to_json(new))
        else:
            verdict = gate(suite, load_baseline(args.baseline), deterministic=deterministic, subset=bool(args.scenario))
            report["gate"] = {"passed": verdict.passed, "reasons": list(verdict.reasons), "drift": list(verdict.drift)}
            code = EXIT_OK if verdict.passed else EXIT_ERROR if "baseline_missing" in verdict.reasons else EXIT_GATE
    except BaselineRefused as error:
        print(f"training_benchmark: refused: {error}", file=sys.stderr)
        return EXIT_GATE
    except BenchmarkError as error:
        print(f"training_benchmark: {error}", file=sys.stderr)
        return EXIT_ERROR
    _emit(args, report, render_markdown(report, verdict))
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
