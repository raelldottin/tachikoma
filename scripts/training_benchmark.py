"""Crew-training simulator benchmark: a scored, deterministic, offline gate for the crew-training planner.

Every scenario under automation/benchmark/training/scenarios is a small world (rooms, research, inventory, crew,
optional injected faults) that runs for several simulated days in the offline simulator (sdk.simulation.game). The
executor loop below follows the documented executor contract only: observe, plan_training, carry every decision out
through the action catalog, replan a finished crew with replan_after_finish and the remaining stock. Metrics come
from the simulator's training log; independent oracles (literal policy tables, cross-checked against the
owner-approved strategy by the tests) flag hard violations. The composite score must never fall below baseline.json.

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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sdk.actions.catalog import get_spec  # noqa: E402
from sdk.execution.runner import call_arguments  # noqa: E402
from sdk.policy import DesignData  # noqa: E402
from sdk.simulation.game import Fault, SimulatedClock, SimulatedFaultError, build_games  # noqa: E402
from sdk.training import build_context  # noqa: E402
from sdk.training.actions import action_for  # noqa: E402
from sdk.training.model import (  # noqa: E402
    SKIP_NOT_IN_TRAINING_ROOM,
    CrewState,
    Decision,
    FinishTraining,
    Skip,
    StartTraining,
    TrainingContext,
    UseConsumable,
)
from sdk.training.parse import parse_consumable_designs, parse_crew  # noqa: E402
from sdk.training.planner import plan_training, remaining_stock, replan_after_finish  # noqa: E402
from sdk.training.strategy import DEFAULT_STRATEGY, TrainingStrategy, with_consumables  # noqa: E402

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"  # @TrainingEndDate and the simulator log's times, UTC without an offset
ACCOUNT = "bench"
BENCHMARK_DIR = os.path.join(ROOT, "automation", "benchmark", "training")
SCENARIO_DIR = os.path.join(BENCHMARK_DIR, "scenarios")
BASELINE_PATH = os.path.join(BENCHMARK_DIR, "baseline.json")
EXIT_OK, EXIT_GATE, EXIT_ERROR = 0, 1, 2

# -- world catalog: every record parses for both sdk.training.build_context and the simulator ---------------------

# room design name -> (design id, training room type or None, upgraded from design id)
CATALOG_ROOMS = {
    "GYM": (1000, "gym", None),
    "Galaxy Gym": (1001, "gym", 1000),
    "Academy": (1100, "academy", None),
    "Lunar College": (1101, "academy", 1100),
    "Bridge": (1200, None, None),
}
# research design name -> (design id, root design id)
CATALOG_RESEARCH = {
    "Fitness": (2010, 2010),
    "Fitness Lv2": (2011, 2010),
    "Fitness Lv3": (2012, 2010),
    "Education": (2020, 2020),
    "Education Lv2": (2021, 2020),
    "Education Lv3": (2022, 2020),
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
# (room design name, tier) -> required research design id; tier 1 needs none
TIER_RESEARCH = {("GYM", 2): 2011, ("GYM", 3): 2012, ("Academy", 2): 2021, ("Academy", 3): 2022}
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
SIM_STATS = ("Hp", "Pilot", "Repair", "Weapon", "Science", "Engine", "Attack", "Ability", "Stamina")
ROOM_TYPE_WORDS = {"gym": "Gym", "academy": "Academy"}

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
        "fault-reject",
        "fault-contradicted",
        "fault-raise",
        "fault-ambiguous",
        "fault-snapshot",
    }
)
MIN_SUITE_DAYS = 7
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

# -- oracle tables: independent literal copies of the owner-approved policy (tests cross-check them) -------------

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
ORACLE_GATES = {
    ("primary", 1): (("Fitness", 1), ("Education", 1)),
    ("primary", 2): (("Fitness", 2), ("Education", 2)),
    ("primary", 3): (("Fitness", 3), ("Education", 3)),
    ("secondary", 1): (),
    ("secondary", 2): (("Fitness", 2), ("Education", 2)),
    ("secondary", 3): (("Fitness", 3), ("Education", 3)),
}
# tier -> (min percent, max percent or None, min fatigue, max fatigue or None)
ORACLE_CONSUMABLE_RULES = {"Common": (0, 29, 0, 0), "Hero": (90, None, 100, None)}
ORACLE_FOCUS = {
    "weapons": ("Weapon", "Attack"),
    "shields": ("Science", "Attack"),
    "engines": ("Attack", "Engine"),
    "rushers": ("Stamina", "Attack"),
    "defenders": ("Attack", "Hp"),
}
SCORE_WEIGHTS = {"progress": 0.65, "utilisation": 0.25, "focus": 0.10}

# research design id -> (family, level)
CATALOG_RESEARCH_LEVELS = {
    2010: ("Fitness", 1),
    2011: ("Fitness", 2),
    2012: ("Fitness", 3),
    2020: ("Education", 1),
    2021: ("Education", 2),
    2022: ("Education", 3),
}


# -- pure oracle helpers -----------------------------------------------------------------------------------------


def percent(total: int, capacity: int) -> int:
    """The exact integer ceiling of total * 100 / capacity (the benchmark's own copy)."""
    return -(-total * 100 // capacity)


def oracle_band(pct: int) -> tuple[str, int] | None:
    """(slot, tier) for a training percent; None once training is complete."""
    if pct >= ORACLE_COMPLETE_PERCENT:
        return None
    for low, high, slot, tier in ORACLE_BANDS:
        if pct <= high:
            return slot, tier
    return None  # pragma: no cover - the bands tile 0..complete-1


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


def research_levels(researches: Sequence[Mapping]) -> dict[str, int]:
    """Research family -> highest completed level, from the catalog's research design ids (independent of parse)."""
    levels: dict[str, int] = {}
    for record in researches:
        if record.get("@ResearchState") != "Completed":
            continue
        entry = CATALOG_RESEARCH_LEVELS.get(int(record.get("@ResearchDesignId") or 0))
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


# -- errors ------------------------------------------------------------------------------------------------------


class BenchmarkError(Exception):
    """A benchmark input or run problem (exit 2), never a planner regression."""


class ScenarioError(BenchmarkError):
    def __init__(self, problems: Sequence[str], source: str = "<scenario>") -> None:
        self.problems = list(problems)
        self.source = source
        super().__init__(f"{source}: " + "; ".join(self.problems))


class WorldError(BenchmarkError):
    """A scenario that validates but cannot make a meaningful world (no headroom, catalog drift)."""


class ObservationError(BenchmarkError):
    """A read failed during one tick; the tick is skipped."""


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
        unknown = sorted(str(key) for key in entry if key not in CREW_KEYS)
        if unknown:
            problems.append(f"{where}: unknown keys {', '.join(unknown)}")
        missing = [key for key in ("id", "name", "design", "room") if key not in entry]
        if missing:
            problems.append(f"{where}: missing {', '.join(missing)}")
            continue
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
        if len(problems) == before:
            spec = CrewSpec(entry["id"], entry["name"], entry["design"], entry["room"], fatigue, level, stats, session)
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
    start = _parse_start(get("start"))
    if "start" not in missing and start is None:
        problems.append(f"start: {get('start')!r} must be an ISO 8601 time with a UTC offset")
    if "days" not in missing and not _int_in(get("days"), 1, 30):
        problems.append(f"days: must be an integer 1..30, not {get('days')!r}")
    tick = get("tick_minutes", 60)
    if not (_int_in(tick, 5, 240) and 1440 % tick == 0):
        problems.append(f"tick_minutes: must be an integer 5..240 dividing 1440, not {tick!r}")
    recovery = get("fatigue_recovery_seconds", 360)
    if not _int_in(recovery, 1):
        problems.append(f"fatigue_recovery_seconds: must be a positive integer, not {recovery!r}")
    rooms = _validate_rooms(get("rooms"), problems) if "rooms" in data else ()
    research = get("research", [])
    if not (isinstance(research, list) and len(set(map(str, research))) == len(research)) or any(
        not _known(name, CATALOG_RESEARCH) for name in research
    ):
        problems.append(f"research: must be unique names among {', '.join(CATALOG_RESEARCH)}")
        research = []
    inventory = get("inventory", {})
    if not isinstance(inventory, Mapping) or any(
        name not in CATALOG_ITEMS or not _int_in(quantity, 0) for name, quantity in inventory.items()
    ):
        problems.append("inventory: must map catalog item names to non-negative quantities")
        inventory = {}
    crew = _validate_crew(get("crew"), {room.id for room in rooms}, problems) if "crew" in data else ()
    faults_raw = get("faults", [])
    faults: list[Mapping[str, object]] = []
    if not isinstance(faults_raw, list):
        problems.append("faults: must be a list")
        faults_raw = []
    for index, entry in enumerate(faults_raw):
        fault = _validate_fault(entry, f"faults[{index}]", problems)
        if fault is not None and "day" in fault and _int_in(get("days"), 1, 30) and fault["day"] >= get("days"):
            problems.append(f"faults[{index}].day: {fault['day']} is past the last day ({get('days') - 1}); "
                            "it would never fire")
        elif fault is not None:
            faults.append(fault)
    if problems:
        raise ScenarioError(problems, source)
    return Scenario(
        name=data["name"],
        description=data["description"],
        tags=tuple(tags),
        seed=data["seed"],
        start=start,
        days=data["days"],
        tick_minutes=tick,
        fatigue_recovery_seconds=recovery,
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
    room_type: str | None
    eligible: bool
    initial_total: int
    headroom: int


@dataclass(frozen=True)
class World:
    sim_scenario: Mapping[str, object]
    room_designs: tuple[Mapping[str, str], ...]
    research_designs: tuple[Mapping[str, str], ...]
    crew: Mapping[int, CrewMeta]
    horizon_end: datetime
    read_faults: bool


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=timezone.utc)


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


def _crew_meta(spec: CrewSpec, rooms: Mapping[int, str]) -> CrewMeta:
    _, rarity, capacity = CATALOG_CHARACTERS[spec.design]
    role = ORACLE_ROLES.get(spec.name)
    room_type = CATALOG_ROOMS[rooms[spec.room]][1] if spec.room in rooms else None
    cap = oracle_cap(rarity, spec.name, capacity)
    eligible = role is not None and room_type is not None
    total = sum(spec.stats.values())
    headroom = max(0, cap - total) if eligible else 0
    return CrewMeta(spec.id, spec.name, role, rarity, capacity, cap, room_type, eligible, total, headroom)


def build_world(scenario: Scenario) -> World:
    """The simulator scenario and the per-crew oracle facts; WorldError when nothing can be gained."""
    item_designs = item_design_records()
    found = dict(parse_consumable_designs({"ItemDesign": item_designs}))
    if found != CATALOG_CONSUMABLES:
        raise WorldError(f"consumable item designs parse to {sorted(found.items())}, not CATALOG_CONSUMABLES")
    rooms = {room.id: room.design for room in scenario.rooms}
    crew = {spec.id: _crew_meta(spec, rooms) for spec in scenario.crew}
    if sum(meta.headroom for meta in crew.values()) <= 0:
        raise WorldError(f"{scenario.name}: no eligible crew has headroom below its TP cap")
    room_designs, research_designs = room_design_records(), research_design_records()
    account = {
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
    sim_scenario = {
        "design": {"room_designs": list(room_designs), "research_designs": list(research_designs)},
        "training": {
            "fatigue_max": 100,
            "fatigue_recovery_seconds": scenario.fatigue_recovery_seconds,
            "character_designs": character_design_records(),
            "training_designs": training_design_records(),
            "item_designs": item_designs,
        },
        "accounts": {ACCOUNT: account},
    }
    return World(
        sim_scenario=sim_scenario,
        room_designs=room_designs,
        research_designs=research_designs,
        crew=crew,
        horizon_end=scenario.start + timedelta(days=scenario.days),
        read_faults=any(fault["kind"] == "snapshot-failure" for fault in scenario.faults),
    )


# -- environment -------------------------------------------------------------------------------------------------

TRAINING_METHODS = ("addTraining", "finishTraining", "useConsumable")


@dataclass(frozen=True)
class CallOutcome:
    status: str  # "ok" | "refused" | "ambiguous" | "raised"
    detail: str = ""
    faults: tuple[str, ...] = ()


class SimEnvironment:
    """The offline simulator behind the Environment protocol: one account, a simulated clock, catalog designs."""

    def __init__(self, world: World, scenario: Scenario, seed: int) -> None:
        self.world = world
        self.clock = SimulatedClock(scenario.start)
        design = DesignData.from_records(**world.sim_scenario["design"])
        self.game = build_games(world.sim_scenario, seed, design, self.clock)[ACCOUNT]

    def now(self) -> datetime:
        return self.clock.now

    def start_day(self, day: int) -> None:
        self.clock.start_day(day)
        self.game.start_day(day)

    def advance(self, delta: timedelta) -> None:
        self.clock.advance(delta)

    def observe(self) -> dict:
        """build_context's payload keyword arguments (without now); ObservationError when a read fails."""
        game = self.game
        if self.world.read_faults:
            # snapshot() reads the simulated clock, which ticks one second per reading; put it back so a read-fault
            # scenario keeps the same tick times as a fault-free one.
            now = self.clock.now
            try:
                game.snapshot()
            except SimulatedFaultError as error:
                raise ObservationError(str(error)) from None
            finally:
                self.clock.now = now
        game.listAllCharactersOfUser()
        game.listAllTrainingDesigns2()
        game.listAllCharacterDesigns2()
        game.listItemsOfAShip()
        world = self.world
        return {
            "characters_raw": game.allCharactersOfUser,
            "character_designs_raw": game.allCharacterDesigns,
            "training_designs_raw": game.trainingDesigns,
            "items_raw": game.itemsOfAShip,
            "rooms_raw": {"Rooms": {"Room": [dict(room) for room in game.rooms]}},
            "room_designs_raw": {"RoomDesigns": {"RoomDesign": [dict(r) for r in world.room_designs]}},
            "researches_raw": {"Researches": {"Research": [dict(entry) for entry in game.research]}},
            "research_designs_raw": {"ResearchDesigns": {"ResearchDesign": [dict(r) for r in world.research_designs]}},
        }

    def call(self, method: str, args: tuple) -> CallOutcome:
        if method not in TRAINING_METHODS:
            raise BenchmarkError(f"not a training call: {method}")
        fired = len(self.game.fired)
        try:
            result = getattr(self.game, method)(*args)
        except SimulatedFaultError as error:
            return CallOutcome("raised", str(error), tuple(self.game.fired[fired:]))
        faults = tuple(self.game.fired[fired:])
        # useConsumable reports True for an activateItem3 with no reply, so a fired call fault decides "ambiguous".
        if result is None or any(fault.endswith(": ambiguous-response") for fault in faults):
            return CallOutcome("ambiguous", "", faults)
        return CallOutcome("ok" if result is True else "refused", "", faults)

    def finish_response(self) -> object:
        return getattr(self.game, "trainingFinish", None)

    def training_log(self) -> Sequence[Mapping]:
        return self.game.training.log

    def crew_records(self) -> list[Mapping]:
        return self.game.training.records(self.clock.now)

    def day_records(self) -> Sequence[Mapping]:
        return self.game.days

    def fired(self) -> Sequence[str]:
        return self.game.fired


# -- recorder and oracles ----------------------------------------------------------------------------------------

ROOM_TYPES = {name: kind for name, (_, kind, _) in CATALOG_ROOMS.items()}
CONSUMABLE_TIERS = {item_id: tier for (_, tier), item_id in CATALOG_CONSUMABLES.items()}
MAX_VIOLATIONS = 20
DECISION_KINDS = ((FinishTraining, "finish"), (StartTraining, "start"), (UseConsumable, "consume"), (Skip, "skip"))


def decision_kind(decision: Decision) -> str:
    for kind, label in DECISION_KINDS:
        if isinstance(decision, kind):
            return label
    raise BenchmarkError(f"not a training decision: {decision!r}")


def crew_total(crew: CrewState) -> int:
    return sum(crew.stats.values())


class Recorder:
    """Counts what the executor loop did and runs the hard-violation oracles on every executed decision.

    The oracles judge the crew state the decision was made from (the rebuilt crew for a replan) and the research
    observed in the same tick, with the benchmark's own policy tables, never the planner's helpers.
    """

    def __init__(self) -> None:
        self.now: datetime | None = None
        self.ticks = 0
        self.observe_failures = 0
        self.levels: dict[str, int] = {}
        self.skip_reasons: dict[str, int] = {}
        self.skip_moves = 0
        self.dropped = 0
        self.replans = 0
        self.actions = {method: 0 for method in TRAINING_METHODS}
        self.outcomes = {"ok": 0, "refused": 0, "ambiguous": 0, "raised": 0}
        self.accepted = 0
        self.refused = 0
        self.consumables_used = 0
        self.cap_violations = 0
        self.consumable_violations = 0
        self.research_gate_violations = 0
        self.violations: list[dict] = []
        self.trace: list[dict] = []

    def begin_tick(self, now: datetime) -> None:
        self.now = now
        self.ticks += 1

    def observe_failure(self) -> None:
        self.observe_failures += 1
        self.trace.append({"at": format_time(self.now), "decision": "observe-failure"})

    def planned(self, ctx: TrainingContext, plan: Sequence[Decision], payloads: Mapping[str, object]) -> None:
        researches = payloads.get("researches_raw") or {}
        self.levels = research_levels(researches.get("Researches", {}).get("Research", []))

    def _trace(self, decision: Decision, **extra: object) -> None:
        entry = {
            "at": format_time(self.now),
            "decision": decision_kind(decision),
            "character_id": decision.character_id,
        }
        if isinstance(decision, StartTraining):
            entry["design"] = decision.training_design_id
        elif isinstance(decision, UseConsumable):
            entry["item"] = decision.item_design_id
        elif isinstance(decision, Skip):
            entry["reason"] = decision.reason_code
            entry["detail"] = decision.detail
        entry.update(extra)
        self.trace.append(entry)

    def skipped(self, decision: Skip) -> None:
        self.skip_reasons[decision.reason_code] = self.skip_reasons.get(decision.reason_code, 0) + 1
        if decision.reason_code == SKIP_NOT_IN_TRAINING_ROOM and decision.detail.startswith("move to"):
            self.skip_moves += 1
        self._trace(decision)

    def drop(self, decision: Decision) -> None:
        self.dropped += 1
        self._trace(decision, status="dropped")

    def replanned(self, decision: Decision) -> None:
        self.replans += 1

    def executed(self, decision: Decision, crew: CrewState, outcome: CallOutcome) -> None:
        method = {"finish": "finishTraining", "start": "addTraining", "consume": "useConsumable"}[
            decision_kind(decision)
        ]
        self.actions[method] += 1
        self.outcomes[outcome.status] += 1
        if outcome.status == "ok":
            self.accepted += 1
            if isinstance(decision, UseConsumable):
                self.consumables_used += 1
        elif outcome.status == "refused" and not any(fault.endswith(": reject") for fault in outcome.faults):
            self.refused += 1
        self._check(decision, crew)
        self._trace(decision, status=outcome.status)

    def _violation(self, kind: str, crew: CrewState, detail: str) -> None:
        if kind == "cap":
            self.cap_violations += 1
        elif kind == "consumable":
            self.consumable_violations += 1
        else:
            self.research_gate_violations += 1
        if len(self.violations) < MAX_VIOLATIONS:
            entry = {"kind": kind, "at": format_time(self.now), "character_id": crew.character_id, "detail": detail}
            self.violations.append(entry)

    def _check(self, decision: Decision, crew: CrewState) -> None:
        if isinstance(decision, FinishTraining):
            return
        total, capacity = crew_total(crew), crew.training_capacity
        cap = oracle_cap(crew.rarity, crew.name, capacity)
        if total >= cap:
            self._violation("cap", crew, f"{decision_kind(decision)} at {total} TP >= cap {cap} TP")
        pct = percent(total, capacity) if capacity > 0 else ORACLE_COMPLETE_PERCENT
        if isinstance(decision, UseConsumable):
            tier = CONSUMABLE_TIERS.get(decision.item_design_id)
            problems = []
            if tier is None:
                problems.append(f"item {decision.item_design_id} is not a catalog consumable")
            elif not consumable_allowed(tier, pct, crew.fatigue):
                problems.append(f"{tier} rule does not allow {pct}% with fatigue {crew.fatigue}")
            if crew.name not in ORACLE_ROLES:
                problems.append(f"{crew.name!r} has no role")
            if ROOM_TYPES.get(crew.room_name or "") is None:
                problems.append(f"room {crew.room_name!r} is not a training room")
            if problems:
                self._violation("consumable", crew, "; ".join(problems))
        if isinstance(decision, StartTraining):
            band = oracle_band(pct)
            if band is not None and not gate_met(band, self.levels):
                self._violation("research-gate", crew, f"{band[0]} T{band[1]} gate unmet at {pct}% ({self.levels})")


# -- the executor loop -------------------------------------------------------------------------------------------


def execute(env, decision: Decision) -> CallOutcome:
    """Carry a decision out through its catalog action: action_for, the spec's client method, its positional args."""
    request = action_for(decision)
    if request is None:
        raise BenchmarkError(f"a Skip is never executed: {decision!r}")
    action_id, params = request
    spec = get_spec(action_id)
    return env.call(spec.client_method, call_arguments(spec, params))


def run_tick(env, *, planner, replanner, strategy: TrainingStrategy, recorder: Recorder) -> None:
    """One executor pass: observe, plan, finish, replan each collected crew with the remaining stock, execute."""
    recorder.begin_tick(env.now())
    try:
        payloads = env.observe()
    except ObservationError:
        recorder.observe_failure()
        return
    ctx = build_context(now=env.now(), **payloads)
    plan = planner(ctx, strategy)
    recorder.planned(ctx, plan, payloads)
    finished = {decision.character_id for decision in plan if isinstance(decision, FinishTraining)}
    stock = remaining_stock(ctx.items, plan, exclude_character_ids=finished)
    by_id = {crew.character_id: crew for crew in ctx.crew}
    collected: set[int] = set()
    for decision in plan:
        cid = decision.character_id
        if isinstance(decision, FinishTraining):
            outcome = execute(env, decision)
            recorder.executed(decision, by_id[cid], outcome)
            if outcome.status == "ok":
                collected.add(cid)
            continue
        crew = by_id[cid]
        if cid in finished:  # the advisory primary after a FinishTraining is never run as-is
            if cid not in collected:
                recorder.drop(decision)
                continue
            rebuilt = parse_crew(
                env.finish_response(),
                payloads["character_designs_raw"],
                payloads["rooms_raw"],
                payloads["room_designs_raw"],
            )
            if len(rebuilt) != 1:
                recorder.drop(decision)
                continue
            crew = rebuilt[0]
            (decision,) = replanner(ctx, crew, strategy, stock)
            stock = remaining_stock(stock, (decision,))
            recorder.replanned(decision)
        if isinstance(decision, Skip):
            recorder.skipped(decision)
            continue
        recorder.executed(decision, crew, execute(env, decision))


# -- metrics -----------------------------------------------------------------------------------------------------


def _seconds(delta: timedelta) -> int:
    return delta // timedelta(seconds=1)


def idle_seconds(window: tuple[datetime, datetime], sessions: Sequence[tuple[datetime, datetime]]) -> int:
    """Seconds of ``window`` that no session covers (sessions are clipped to the window and never double-counted)."""
    start, end = window
    if end <= start:
        return 0
    covered = 0
    cursor = start
    for low, high in sorted(sessions):
        low, high = max(low, cursor), min(high, end)
        if high > low:
            covered += _seconds(high - low)
            cursor = high
    return _seconds(end - start) - covered


def progress_auc(
    headroom: Mapping[int, int], initial: Mapping[int, int], totals_by_day: Sequence[Mapping[int, int]]
) -> float:
    """Mean over days of the headroom gained by each day's end, as a fraction of the total headroom."""
    total_headroom = sum(headroom.values())
    if total_headroom <= 0 or not totals_by_day:
        return 0.0
    gained = 0
    for totals in totals_by_day:
        for crew, room in sorted(headroom.items()):
            gained += min(room, max(0, totals.get(crew, initial[crew]) - initial[crew]))
    return gained / (len(totals_by_day) * total_headroom)


def focus_ratio(role_by_crew: Mapping[int, str | None], gains_by_crew: Mapping[int, Mapping[str, int]]) -> float:
    """TP the role-known crew gained in their role's focus stats, as a fraction of all TP they gained."""
    focused = gained = 0
    for crew, role in sorted(role_by_crew.items()):
        if role is None:
            continue
        gains = gains_by_crew.get(crew, {})
        focus = ORACLE_FOCUS.get(role, ())
        for stat, amount in sorted(gains.items()):
            gained += amount
            if stat in focus:
                focused += amount
    return focused / gained if gained else 0.0


@dataclass(frozen=True)
class Metrics:
    progress: float = 0.0  # P: progress AUC over days, a fraction of the eligible headroom
    utilisation: float = 0.0  # U: 1 - idle / eligible training-room crew time
    focus: float = 0.0  # F: share of gained TP in the role's focus stats
    accepted_actions: int = 0
    refused_actions: int = 0  # server refusals not caused by an injected reject fault
    cap_violations: int = 0
    consumable_violations: int = 0
    research_gate_violations: int = 0
    eligible_crew: int = 0
    headroom: int = 0
    tp_gained: int = 0
    tp_gained_eligible: int = 0
    tp_per_crew_day: float = 0.0
    eligible_crew_hours: float = 0.0
    idle_crew_hours: float = 0.0
    actions: Mapping[str, int] = field(default_factory=dict)
    refusal_codes: Mapping[str, int] = field(default_factory=dict)
    zero_gain_sessions: int = 0
    wasted_actions: int = 0
    fault_rejected: int = 0
    raised: int = 0
    ambiguous: int = 0
    contradicted: int = 0
    observe_failures: int = 0
    dropped: int = 0
    replans: int = 0
    violations: tuple[Mapping[str, object], ...] = ()
    cap_overshoot_tp: int = 0
    skip_reasons: Mapping[str, int] = field(default_factory=dict)
    skip_moves: int = 0
    consumables_used: int = 0

    @property
    def reliability(self) -> float:
        attempted = self.accepted_actions + self.refused_actions
        return self.accepted_actions / attempted if attempted else 1.0

    def to_dict(self) -> dict:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        for name in ("actions", "refusal_codes", "skip_reasons"):
            data[name] = dict(sorted(data[name].items()))
        data["violations"] = [dict(entry) for entry in self.violations]
        data["reliability"] = round(self.reliability, 6)
        for name in ("progress", "utilisation", "focus"):
            data[name] = round(data[name], 6)
        return data


def score_scenario(metrics: Metrics | None) -> float:
    """100 x R x (0.65 P + 0.25 U + 0.10 F), rounded to 6 places; 0.0 for an error or any hard violation."""
    if metrics is None:
        return 0.0
    if metrics.cap_violations or metrics.consumable_violations or metrics.research_gate_violations:
        return 0.0
    weighted = (
        SCORE_WEIGHTS["progress"] * metrics.progress
        + SCORE_WEIGHTS["utilisation"] * metrics.utilisation
        + SCORE_WEIGHTS["focus"] * metrics.focus
    )
    return round(100 * metrics.reliability * weighted, 6)


def composite(scores: Sequence[float]) -> float:
    """The mean scenario score, rounded to 6 places (0.0 for no scenarios)."""
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores), 6)


# -- episodes ----------------------------------------------------------------------------------------------------


def default_strategy() -> TrainingStrategy:
    """DEFAULT_STRATEGY with the catalog's consumable ids attached (the only strategy change the benchmark makes)."""
    return with_consumables(DEFAULT_STRATEGY, CATALOG_CONSUMABLES)


@dataclass(frozen=True)
class Episode:
    world: World
    scenario: Scenario
    seed: int
    recorder: Recorder
    log: tuple[Mapping[str, object], ...]
    day_totals: tuple[Mapping[int, int], ...]  # crew totals at the start of days 1..D-1
    final: Mapping[int, int]  # crew totals at the horizon
    fired: tuple[str, ...]


def record_totals(records: Sequence[Mapping[str, str]]) -> dict[int, int]:
    return {
        int(record["@CharacterId"]): sum(int(record[f"@{stat}Improvement"]) for stat in SIM_STATS)
        for record in records
    }


def run_episode(
    scenario: Scenario,
    *,
    seed: int | None = None,
    planner=None,
    replanner=None,
    strategy: TrainingStrategy | None = None,
    environment_factory=None,
    tick=None,
    days: int | None = None,
) -> Episode:
    """Run one scenario for its days (or ``days``) at its tick cadence and collect what the metrics need.

    Everything is injectable: the planner and replanner, the strategy, the environment factory and the tick
    function. The swap point for Unit 9's executor (sdk/execution/training.py) is ``tick``: any callable taking
    ``run_tick``'s keywords, driving ``env.game`` (which implements the Client surface) and reporting to the
    recorder.
    """
    if days is not None:
        scenario = replace(scenario, days=days)
    seed = scenario.seed if seed is None else seed
    planner = plan_training if planner is None else planner
    replanner = replan_after_finish if replanner is None else replanner
    strategy = default_strategy() if strategy is None else strategy
    tick = run_tick if tick is None else tick
    world = build_world(scenario)
    env = (environment_factory or SimEnvironment)(world, scenario, seed)
    recorder = Recorder()
    step = timedelta(minutes=scenario.tick_minutes)
    for day in range(scenario.days):
        env.start_day(day)
        for _ in range(1440 // scenario.tick_minutes):
            tick(env, planner=planner, replanner=replanner, strategy=strategy, recorder=recorder)
            env.advance(step)
    return Episode(
        world=world,
        scenario=scenario,
        seed=seed,
        recorder=recorder,
        log=tuple(dict(entry) for entry in env.training_log()),
        day_totals=tuple(record_totals(day["crew"]) for day in env.day_records()[1:]),
        final=record_totals(env.crew_records()),
        fired=tuple(env.fired()),
    )


def _done_at(meta: CrewMeta, log: Sequence[Mapping[str, object]], horizon: datetime) -> datetime:
    """When the crew stopped needing training: its first accepted finish or item use that reached its cap or the
    complete percent; the horizon when it never did."""
    for entry in log:
        if entry["character_id"] != str(meta.character_id) or not entry["accepted"] or entry["op"] == "start":
            continue
        total = entry["total"]
        if total >= meta.cap or percent(total, meta.capacity) >= ORACLE_COMPLETE_PERCENT:
            return parse_time(entry["at"])
    return horizon


def _sessions(spec: CrewSpec, log: Sequence[Mapping[str, object]], start: datetime) -> list[tuple[datetime, datetime]]:
    sessions = []
    if spec.training is not None:
        sessions.append((start, start + timedelta(minutes=spec.training[1])))
    for entry in log:
        if entry["op"] == "start" and entry["accepted"] and entry["character_id"] == str(spec.id):
            sessions.append((parse_time(entry["at"]), parse_time(entry["end"])))
    return sessions


def compute_metrics(episode: Episode) -> Metrics:
    """Every metric of one episode (integer arithmetic until the final ratios)."""
    world, scenario, recorder, log = episode.world, episode.scenario, episode.recorder, episode.log
    eligible = {cid: meta for cid, meta in sorted(world.crew.items()) if meta.eligible}
    initial = {cid: meta.initial_total for cid, meta in world.crew.items()}
    headroom = {cid: meta.headroom for cid, meta in eligible.items()}
    window_s = idle_s = 0
    for spec in scenario.crew:
        meta = eligible.get(spec.id)
        if meta is None:
            continue
        if meta.initial_total >= meta.cap or percent(meta.initial_total, meta.capacity) >= ORACLE_COMPLETE_PERCENT:
            continue  # nothing left to train: no window
        window = (scenario.start, _done_at(meta, log, world.horizon_end))
        window_s += _seconds(window[1] - window[0])
        idle_s += idle_seconds(window, _sessions(spec, log, scenario.start))
    gains: dict[int, dict[str, int]] = {}
    zero_gain = contradicted = 0
    refusal_codes: dict[str, int] = {}
    for entry in log:
        if not entry["accepted"]:
            refusal_codes[entry["code"]] = refusal_codes.get(entry["code"], 0) + 1
            continue
        if entry["op"] == "start" and entry["design_id"] != entry["requested_design_id"]:
            contradicted += 1
        per_stat = gains.setdefault(int(entry["character_id"]), {})
        for stat, amount in entry["gain"].items():
            per_stat[stat] = per_stat.get(stat, 0) + amount
        if entry["op"] == "finish" and sum(entry["gain"].values()) == 0 and not entry["capped"]:
            zero_gain += 1
    final = episode.final
    tp_eligible = sum(final[cid] - initial[cid] for cid in eligible)
    crew_days = len(eligible) * scenario.days
    return Metrics(
        progress=progress_auc(headroom, initial, [*episode.day_totals, final]),
        utilisation=1 - idle_s / window_s if window_s else 1.0,
        focus=focus_ratio({cid: meta.role for cid, meta in eligible.items()}, gains),
        accepted_actions=recorder.accepted,
        refused_actions=recorder.refused,
        cap_violations=recorder.cap_violations,
        consumable_violations=recorder.consumable_violations,
        research_gate_violations=recorder.research_gate_violations,
        eligible_crew=len(eligible),
        headroom=sum(headroom.values()),
        tp_gained=sum(final[cid] - initial[cid] for cid in initial),
        tp_gained_eligible=tp_eligible,
        tp_per_crew_day=round(tp_eligible / crew_days, 6) if crew_days else 0.0,
        eligible_crew_hours=round(window_s / 3600, 6),
        idle_crew_hours=round(idle_s / 3600, 6),
        actions=dict(recorder.actions),
        refusal_codes=refusal_codes,
        zero_gain_sessions=zero_gain,
        wasted_actions=recorder.refused + zero_gain,
        fault_rejected=sum(1 for fired in episode.fired if fired.endswith(": reject")),
        raised=recorder.outcomes["raised"],
        ambiguous=recorder.outcomes["ambiguous"],
        contradicted=contradicted,
        observe_failures=recorder.observe_failures,
        dropped=recorder.dropped,
        replans=recorder.replans,
        violations=tuple(recorder.violations),
        # TP pushed past the cap during the run; a crew that started above its cap only counts what it gained.
        cap_overshoot_tp=sum(max(0, final[cid] - max(meta.cap, meta.initial_total)) for cid, meta in world.crew.items()),
        skip_reasons=dict(recorder.skip_reasons),
        skip_moves=recorder.skip_moves,
        consumables_used=recorder.consumables_used,
    )


def trace_digest(episode: Episode) -> str:
    """sha256 of the decision trace and the simulator log: any behaviour change moves it."""
    payload = json.dumps({"trace": episode.recorder.trace, "log": list(episode.log)}, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# -- suite, report, determinism, gate ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    seed: int
    metrics: Metrics | None
    score: float
    trace_digest: str
    error: str | None = None


@dataclass(frozen=True)
class SuiteResult:
    scenarios: tuple[ScenarioResult, ...]
    composite: float


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: tuple[str, ...]
    drift: tuple[str, ...] = ()


def run_suite(scenarios: Sequence[Scenario], **episode_kwargs) -> SuiteResult:
    """Run and score every scenario; a crash inside an episode is that scenario's error (score 0)."""
    results = []
    for scenario in scenarios:
        try:
            episode = run_episode(scenario, **episode_kwargs)
        except BenchmarkError:
            raise
        except Exception as error:  # a planner crash is a result, not a benchmark failure
            seed = episode_kwargs.get("seed")
            seed = scenario.seed if seed is None else seed
            results.append(ScenarioResult(scenario.name, seed, None, 0.0, "", f"{type(error).__name__}: {error}"))
            continue
        metrics = compute_metrics(episode)
        digest = trace_digest(episode)
        results.append(ScenarioResult(scenario.name, episode.seed, metrics, score_scenario(metrics), digest))
    return SuiteResult(tuple(results), composite([result.score for result in results]))


def report_dict(suite: SuiteResult) -> dict:
    return {
        "schema_version": 1,
        "benchmark": "crew-training",
        "composite": suite.composite,
        "scenarios": {
            result.name: {
                "score": result.score,
                "seed": result.seed,
                "trace_digest": result.trace_digest,
                "error": result.error,
                "metrics": None if result.metrics is None else result.metrics.to_dict(),
            }
            for result in suite.scenarios
        },
    }


def to_json(report: Mapping) -> str:
    return json.dumps(report, sort_keys=True, indent=2) + "\n"


def check_determinism(scenarios: Sequence[Scenario], runs: int = 2, **episode_kwargs) -> tuple[bool, SuiteResult]:
    """Run the suite ``runs`` times in this process; deterministic when every report is byte-identical."""
    first = run_suite(scenarios, **episode_kwargs)
    expected = to_json(report_dict(first))
    same = True
    for _ in range(runs - 1):
        if to_json(report_dict(run_suite(scenarios, **episode_kwargs))) != expected:
            same = False
    return same, first


VIOLATION_REASONS = (
    ("cap_violations", "cap_violation"),
    ("consumable_violations", "consumable_violation"),
    ("research_gate_violations", "research_gate_violation"),
)


def gate(suite: SuiteResult, baseline: Mapping | None, *, deterministic: bool, subset: bool = False) -> GateResult:
    """Pass only when deterministic, violation- and error-free, and at or above the baseline scores."""
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
        if result.metrics is not None:
            for name, label in VIOLATION_REASONS:
                if getattr(result.metrics, name):
                    reasons.append(f"{label}:{result.name}")
    return GateResult(not reasons, tuple(reasons), tuple(drift))


# -- baseline ----------------------------------------------------------------------------------------------------

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


def validate_baseline(data: object) -> dict:
    """The baseline itself when its schema is right; BaselineError listing every problem otherwise."""
    if not isinstance(data, Mapping):
        raise BaselineError("baseline must be a JSON object")
    problems: list[str] = []
    if set(data) != set(BASELINE_KEYS):
        problems.append(f"keys must be exactly {', '.join(BASELINE_KEYS)}")
    if data.get("schema_version") != 1 or isinstance(data.get("schema_version"), bool):
        problems.append("schema_version: must be 1")
    if data.get("benchmark") != "crew-training":
        problems.append("benchmark: must be 'crew-training'")
    if not _score_ok(data.get("composite")):
        problems.append("composite: must be a number in [0, 100]")
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, Mapping):
        problems.append("scenarios: must be an object")
        scenarios = {}
    for name, entry in scenarios.items():
        if not isinstance(entry, Mapping) or set(entry) != {"score", "trace_digest"}:
            problems.append(f"scenarios.{name}: needs exactly score and trace_digest")
            continue
        if not _score_ok(entry["score"]):
            problems.append(f"scenarios.{name}.score: must be a number in [0, 100]")
        if not (isinstance(entry["trace_digest"], str) and DIGEST_RE.match(entry["trace_digest"])):
            problems.append(f"scenarios.{name}.trace_digest: must match {DIGEST_RE.pattern}")
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
        raise BaselineRefused("nondeterministic: two in-process runs gave different reports")
    errors = [result.name for result in suite.scenarios if result.error is not None]
    if errors:
        raise BaselineRefused(f"scenario error in {', '.join(errors)}")
    violating = [
        result.name
        for result in suite.scenarios
        if any(getattr(result.metrics, name) for name, _ in VIOLATION_REASONS)
    ]
    if violating:
        raise BaselineRefused(f"hard violation in {', '.join(violating)}")
    new = {
        "schema_version": 1,
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


def render_markdown(report: Mapping, verdict: GateResult | None) -> str:
    scenarios = report["scenarios"]
    lines = [
        "# Crew-training benchmark",
        "",
        f"Composite: **{report['composite']:.2f}** over {len(scenarios)} scenarios",
    ]
    if verdict is None:
        lines.append("Gate: not run (report only)")
    elif verdict.passed:
        lines.append("Gate: **PASS**")
    else:
        lines.append(f"Gate: **FAIL** ({', '.join(verdict.reasons)})")
    if verdict is not None and verdict.drift:
        lines += ["", f"> Warning: Trace drift (behaviour changed, scores held): {', '.join(verdict.drift)}"]
    lines += [
        "",
        "| Scenario | Score | P | U | F | R | TP gained | Idle h / eligible h | Overshoot TP | Consumables "
        "| Violations |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    skips = []
    for name, entry in sorted(scenarios.items()):
        metrics = entry["metrics"]
        if metrics is None:
            lines.append(f"| {name} | {entry['score']:.2f} | error: {entry['error']} |")
            continue
        violations = (
            metrics["cap_violations"] + metrics["consumable_violations"] + metrics["research_gate_violations"]
        )
        lines.append(
            f"| {name} | {entry['score']:.2f} | {metrics['progress']:.3f} | {metrics['utilisation']:.3f} | "
            f"{metrics['focus']:.3f} | {metrics['reliability']:.3f} | {metrics['tp_gained']} | "
            f"{metrics['idle_crew_hours']:g} / {metrics['eligible_crew_hours']:g} | {metrics['cap_overshoot_tp']} | "
            f"{metrics['consumables_used']} | {violations} |"
        )
        reasons = ", ".join(f"{code} {count}" for code, count in sorted(metrics["skip_reasons"].items())) or "none"
        skips.append(f"| {name} | {reasons} | {metrics['skip_moves']} | {metrics['zero_gain_sessions']} |")
    lines += ["", "| Scenario | Skips (reason count) | Move skips | Zero-gain sessions |", "|---|---|---:|---:|"]
    return "\n".join(lines + skips) + "\n"


# -- qa-eval: does the benchmark catch mutants of the planner and strategy? --------------------------------------

QA_TARGETS = ("sdk/training/planner.py", "sdk/training/strategy.py")
QA_TEST_MODULES = ("tests.test_training_planner", "tests.test_training_parse", "tests.test_training_benchmark")
QA_COPY_PATHS = (
    "sdk",
    "tests",
    "scripts/ci.py",
    "scripts/mutation.py",
    "scripts/training_benchmark.py",
    "automation/benchmark/training",
)
QA_CHANNELS = ("tests", "benchmark")
BENCH_DETECTED = ("detected", "crash", "timeout")
TESTS_KILLED = ("failed", "timeout")
QA_CLASSES = ("both", "tests-only", "benchmark-only", "drift-only", "neither", "equivalent", "incompetent")
UNEXPLAINED = "unexplained - investigate"


@dataclass(frozen=True)
class BenchOutcome:
    status: str  # "pass" | "detected" | "crash" | "timeout"
    reasons: tuple[str, ...] = ()
    drift: tuple[str, ...] = ()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
        process = self.popen(command, cwd=workspace, env=mutation.child_environment(workspace),
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
    """mutation.run_mutants' test runner: the tests channel, then the benchmark channel, per mutant workspace.

    Each mutant's outcome per channel is kept under the sha256 of the mutated target, so the report can say which
    channel caught it; the run_mutants status is "failed" when either channel did.
    """

    def __init__(self, rel: str, channels: Sequence[str], tests_runner, bench_runner) -> None:
        self.rel = rel
        self.channels = tuple(channels)
        self.tests_runner = tests_runner
        self.bench_runner = bench_runner
        self.outcomes: dict[str, tuple[str | None, BenchOutcome | None]] = {}
        self._lock = threading.Lock()

    def __call__(self, workspace, tests: tuple[str, ...], timeout: float):
        from scripts import mutation

        workspace = str(workspace)
        with open(os.path.join(workspace, self.rel), "rb") as handle:
            digest = _sha256(handle.read())
        share = timeout / len(self.channels)
        started = time.monotonic()
        result = self.tests_runner(workspace, tests, share) if "tests" in self.channels else None
        outcome = self.bench_runner(workspace, share) if "benchmark" in self.channels else None
        with self._lock:
            self.outcomes[digest] = (None if result is None else result.status, outcome)
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


def classify_mutant(status: str, tests_status: str | None, outcome: BenchOutcome | None) -> str:
    """Which channel caught a mutant: both, tests-only, benchmark-only, drift-only, neither (or its skip class)."""
    if status in ("equivalent", "incompetent"):
        return status
    tests = tests_status in TESTS_KILLED
    detected = outcome is not None and outcome.status in BENCH_DETECTED
    if tests and detected:
        return "both"
    if tests:
        return "tests-only"
    if detected:
        return "benchmark-only"
    if outcome is not None and outcome.drift:
        return "drift-only"
    return "neither"


def _rate(count: int, eligible: int) -> float:
    return round(count / eligible, 6) if eligible else 1.0


def summarize(rows: Sequence[Mapping], equivalent_reasons: Mapping[str, str]) -> dict:
    """Class counts and rates; the detection rate counts tests or benchmark, never drift, over non-skipped mutants."""
    counts = {klass: 0 for klass in QA_CLASSES}
    for row in rows:
        counts[row["class"]] += 1
    eligible = len(rows) - counts["equivalent"] - counts["incompetent"]
    detected = counts["both"] + counts["tests-only"] + counts["benchmark-only"]

    def change(row: Mapping) -> str:
        return f"{row['original']} -> {row['replacement']}"

    return {
        "counts": counts,
        "total": len(rows),
        "eligible": eligible,
        "detection_rate": _rate(detected, eligible),
        "observable_rate": _rate(detected + counts["drift-only"], eligible),
        "tests_rate": _rate(counts["both"] + counts["tests-only"], eligible),
        "benchmark_rate": _rate(counts["both"] + counts["benchmark-only"], eligible),
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
    progress=None,
) -> dict:
    """Mutate each target, run both channels against every mutant and report who caught what."""
    from scripts import mutation

    baseline = load_baseline(baseline_path)
    qa = baseline["qa_eval"] if baseline else {"min_detection_rate": DEFAULT_MIN_DETECTION_RATE, "equivalent": []}
    reasons = {entry["id"]: entry["reason"] for entry in qa["equivalent"]}
    per_target: dict[str, dict] = {}
    all_rows: list[dict] = []
    stale_all: list[str] = []
    for rel in targets:
        target_only = None if only is None else [mutant_id for mutant_id in only if mutant_id.startswith(rel + ":")]
        if target_only == []:
            continue
        # The bytes run_mutants writes (no newline translation), so the digests QaRunner records match.
        source = mutation.read_source(Path(repo_root) / rel)
        generated = mutation.generate_mutants(rel, source)
        entries = [entry for entry in qa["equivalent"] if entry["id"].startswith(rel + ":")]
        stale = stale_equivalents(entries, generated)
        stale_all += stale
        runner = QaRunner(
            rel,
            channels,
            tests_runner if tests_runner is not None else mutation.SubprocessTestRunner(),
            bench_runner if bench_runner is not None else BenchSubprocessRunner(),
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
        rows = []
        for result in report.results:
            mutant = result.mutant
            tests_status, outcome = None, None
            if result.status not in ("equivalent", "incompetent"):
                key = _sha256(mutation.apply_mutant(source, mutant).encode("utf-8"))
                tests_status, outcome = runner.outcomes.get(key, (None, None))
            rows.append(
                {
                    "id": mutant.id,
                    "original": mutant.original,
                    "replacement": mutant.replacement,
                    "status": result.status,
                    "tests": tests_status,
                    "benchmark": None if outcome is None else outcome.status,
                    "reasons": [] if outcome is None else list(outcome.reasons),
                    "drift": [] if outcome is None else list(outcome.drift),
                    "class": classify_mutant(result.status, tests_status, outcome),
                }
            )
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
    lines = [
        "# Crew-training qa-eval",
        "",
        f"Detection rate: **{qa['overall']['detection_rate']:.3f}** (minimum {qa['min_detection_rate']}); "
        f"observable (with drift): {qa['overall']['observable_rate']:.3f}; channels: {', '.join(qa['channels'])}",
        "",
        "| Target | Mutants | Incompetent | Equivalent | Tests | Benchmark | Benchmark-only | Drift-only | Neither "
        "| Detection | Observable |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    sections = {"Caught only by the benchmark": [], "Caught by neither": [], "Drift only": []}
    for rel, summary in sorted(qa["targets"].items()):
        counts = summary["counts"]
        lines.append(
            f"| {rel} | {summary['total']} | {counts['incompetent']} | {counts['equivalent']} | "
            f"{counts['both'] + counts['tests-only']} | {counts['both'] + counts['benchmark-only']} | "
            f"{counts['benchmark-only']} | {counts['drift-only']} | {counts['neither']} | "
            f"{summary['detection_rate']:.3f} | {summary['observable_rate']:.3f} |"
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
    parser.add_argument("--seed", type=int, help="override every scenario's seed, report only (no gate)")
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
    if args.qa_eval and (args.gate or args.update_baseline or args.quick or args.seed is not None or args.scenario):
        return "--qa-eval takes no --gate, --update-baseline, --quick, --seed or --scenario (it would ignore them)"
    if args.gate and (args.update_baseline or args.quick or args.seed is not None):
        return "--gate cannot run with --update-baseline, --quick or --seed (those runs are never gated)"
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    problem = _usage_error(args)
    if problem:
        print(f"training_benchmark: {problem}", file=sys.stderr)
        return EXIT_ERROR
    if args.qa_eval:
        return qa_main(args)
    try:
        scenarios = load_suite(args.scenarios_dir, args.scenario)
        kwargs: dict = {}
        if args.quick:
            kwargs["days"] = 2
        if args.seed is not None:
            kwargs["seed"] = args.seed
        deterministic, suite = check_determinism(scenarios, **kwargs)
        report = report_dict(suite)
        verdict = None
        code = EXIT_OK
        if args.update_baseline:
            new = update_baseline(suite, load_baseline(args.baseline), deterministic=deterministic,
                                  allow_lower=args.allow_lower)
            _write(args.baseline, to_json(new))
        elif not (args.quick or args.seed is not None):
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
