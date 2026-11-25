"""Deterministic offline gameplay scenarios (R6, AUTO-014).

``SimulatedGame`` stands in for ``sdk.client.Client`` with the method names
the action catalog uses. Its state (resources, trophies, rooms, research,
inventory) changes the way a successful server call would, with amounts
drawn from a seeded ``random.Random``. Nothing touches the network.

Crew training (``CrewTraining``) is modelled when the scenario has a
``training`` block: crew records, ``addTraining``/``finishTraining``,
consumables through ``activateItem3``/``useConsumable``, fatigue that
recovers with time, and the Client's read shapes. It reads the clock without
ticking it and draws from its own per-account generator, so scenarios
without crew give byte-identical reports.

``run_scenario`` drives the real stack end to end for a scenario: account
registry, fleet orchestrator, per-account journals, then the progression,
fleet and isolation-audit reports. Rooms that were upgrading and research
that was running finish at each day boundary. The same scenario and seed
give byte-identical reports: the clock is simulated, elapsed timers are
fixed, journal attempt ids (random UUIDs on disk) are renumbered in run
order before reporting, and a workdir that already holds one of the
scenario's journals is refused, so every run starts from empty journals.

Each account may also list ``faults`` in the scenario: seeded failures of the
simulated game (a snapshot read that fails, a call that raises or returns an
ambiguous response, the player spending between snapshots, a snapshot of the
wrong account). They exercise the runner, scheduler and orchestrator safety
rules end to end; see ``Fault`` and ``docs/baseline/simulation.md``.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from sdk.crew_leveling import parse_server_datetime
from sdk.fleet import registry_from_dict
from sdk.fleet.audit import audit_fleet
from sdk.fleet.orchestrator import AccountContext, FleetOrchestrator, FleetRun
from sdk.journal import GameplayEvent, GameplayJournal
from sdk.policy import DesignData
from sdk.reports import compute_progression_metrics
from sdk.reports import progression as progression_report
from sdk.reports import fleet as fleet_report
from sdk.state import AccountSnapshot, build_snapshot

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"


class SimulatedClock:
    """Advances one second per reading; jumps to day boundaries on request. The start must carry a UTC offset."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            # Snapshots refuse naive times, so a naive start would fail mid-run; refuse it before anything runs.
            raise ValueError(f"simulated clock start {start.isoformat()} has no UTC offset (give one, e.g. +00:00)")
        self.start = start
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    def start_day(self, day: int) -> None:
        self.now = max(self.now, self.start + timedelta(days=day))

    def advance(self, delta: timedelta) -> None:
        self.now += delta


# Client methods the simulated game provides, and the pseudo-method "snapshot"
# (a read of the snapshot provider), which faults can be attached to.
CLIENT_METHODS = ("collectAllResources", "collectDailyReward", "upgradeRoom", "addResearch", "addTraining",
                  "runBattleEndToEnd", "finishTraining", "activateItem3")
SNAPSHOT = "snapshot"
FAULT_KINDS = ("snapshot-failure", "raise", "ambiguous-response", "concurrent-spend", "foreign-snapshot", "reject",
               "contradicted-effect")
# Faults that change what a call returns; they only attach to client methods.
CALL_FAULTS = ("raise", "ambiguous-response")
# Faults that change what the server does with a training call: refuse it, or start another design.
OUTCOME_FAULTS = ("reject", "contradicted-effect")
TRAINING_CALLS = ("addTraining", "finishTraining", "activateItem3")
READ_FAULTS = ("snapshot-failure", "foreign-snapshot")
RESOURCES = ("minerals", "gas", "starbux")


class SimulatedFaultError(ConnectionError):
    """Raised by an injected fault, standing in for a dropped or timed-out request."""


@dataclass(frozen=True)
class Fault:
    """One injected fault, read from an account's ``faults`` list in a scenario.

    It is triggered by invocations of ``method`` (a client method, or
    ``"snapshot"`` for snapshot reads), counted per account: within ``day``
    (0-based) when given, else across the whole run. ``call`` is the 1-based
    invocation that triggers it; without it every matching invocation does.
    A triggered fault fires with ``probability``, drawn from the account's
    own fault generator, which is seeded from the scenario seed and alias.

    Kinds:

    - ``raise``: the call takes effect (unless ``effect`` is false), then raises
      ``SimulatedFaultError``;
    - ``ambiguous-response``: the call takes effect (unless ``effect`` is false)
      and returns ``None`` instead of a boolean;
    - ``snapshot-failure``: on ``"snapshot"``, that read raises; on a client
      method, the next snapshot read after the call (its after-snapshot) does;
    - ``foreign-snapshot``: like ``snapshot-failure``, but the read returns a
      snapshot labelled with another account's alias (``alias``);
    - ``concurrent-spend``: the player spends ``minerals``, ``gas`` and
      ``starbux`` (capped at the balance), right after the call took effect or
      just before the snapshot read, so it lands between two snapshots;
    - ``reject``: on ``addTraining``, ``finishTraining`` or ``activateItem3``,
      the server refuses the call: nothing changes and it returns the
      Client's failure value (``False``, or an ``@errorMessage`` item);
    - ``contradicted-effect``: on ``addTraining``, a request the server
      accepts starts ``training_design_id`` instead of the requested design.
    """

    kind: str
    method: str
    day: Optional[int] = None
    call: Optional[int] = None
    probability: float = 1.0
    effect: bool = True
    alias: Optional[str] = None
    spend: Mapping[str, int] = field(default_factory=dict)
    training_design_id: Optional[str] = None

    def __post_init__(self) -> None:
        # Fail closed: a mistyped fault must not silently fire on every call, or never.
        kind = self.kind
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind {kind!r}; expected one of {', '.join(FAULT_KINDS)}")
        if self.method not in CLIENT_METHODS + (SNAPSHOT,):
            raise ValueError(f"fault {kind!r} has unknown method {self.method!r}")
        if kind in CALL_FAULTS and self.method == SNAPSHOT:
            raise ValueError(f"fault {kind!r} applies to a client method, not to snapshot reads")
        if kind == "reject" and self.method not in TRAINING_CALLS:
            raise ValueError("fault 'reject' applies to addTraining, finishTraining or activateItem3")
        if kind == "contradicted-effect" and self.method != "addTraining":
            raise ValueError("fault 'contradicted-effect' applies to addTraining only")
        if _is_int(self.training_design_id):  # a JSON number, like ids elsewhere in a scenario
            object.__setattr__(self, "training_design_id", str(self.training_design_id))
        if kind == "contradicted-effect" and not (isinstance(self.training_design_id, str) and self.training_design_id):
            raise ValueError("fault 'contradicted-effect' needs the training_design_id it starts instead")
        if self.day is not None and not (_is_int(self.day) and self.day >= 0):
            raise ValueError(f"fault {kind!r}: day must be a non-negative integer, not {self.day!r}")
        if self.call is not None and not (_is_int(self.call) and self.call >= 1):
            raise ValueError(f"fault {kind!r}: call must be a positive integer, not {self.call!r}")
        if not (_is_number(self.probability) and 0 < self.probability <= 1):
            raise ValueError(f"fault {kind!r}: probability must be a number in (0, 1], not {self.probability!r}")
        if not isinstance(self.effect, bool):
            raise ValueError(f"fault {kind!r}: effect must be true or false, not {self.effect!r}")
        if kind == "foreign-snapshot" and not (isinstance(self.alias, str) and self.alias):
            raise ValueError("fault 'foreign-snapshot' needs the alias the snapshot claims")
        for name, amount in self.spend.items():
            if name not in RESOURCES or not (_is_int(amount) and amount >= 0):
                raise ValueError(f"fault {kind!r}: {name} must be a non-negative integer, not {amount!r}")
        if kind == "concurrent-spend" and not any(amount > 0 for amount in self.spend.values()):
            raise ValueError("fault 'concurrent-spend' needs a positive minerals, gas or starbux amount")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Fault":
        kind = data.get("kind")
        allowed = {"kind", "method", "day", "call", "probability"} | FAULT_OPTIONS.get(kind, set())
        unknown = sorted(set(data) - allowed)
        if kind in FAULT_KINDS and unknown:
            raise ValueError(f"fault {kind!r} has unknown or inapplicable keys: {', '.join(map(str, unknown))}")
        return cls(kind=kind, method=data.get("method"), day=data.get("day"), call=data.get("call"),
                   probability=data.get("probability", 1.0), effect=data.get("effect", True),
                   alias=data.get("alias"), spend={name: data[name] for name in RESOURCES if name in data},
                   training_design_id=data.get("training_design_id"))


# Keys each kind accepts besides kind, method, day, call and probability.
FAULT_OPTIONS = {
    "raise": {"effect"},
    "ambiguous-response": {"effect"},
    "snapshot-failure": set(),
    "foreign-snapshot": {"alias"},
    "concurrent-spend": set(RESOURCES),
    "reject": set(),
    "contradicted-effect": {"training_design_id"},
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# -- crew training --------------------------------------------------------------------

STATS = ("Hp", "Pilot", "Repair", "Weapon", "Science", "Engine", "Attack", "Ability", "Stamina")
STAT_KEYS = tuple(f"@{stat}Improvement" for stat in STATS)
# Every key of a simulated character record, with its default; a scenario crew record may give no other key.
IDLE = "0"  # @TrainingDesignId of a crew member with no session
UNASSIGNED = "0"  # @RoomId of a crew member in no room, as the live game sends it; never a room
CREW_DEFAULTS = {"@CharacterId": "", "@CharacterDesignId": "", "@CharacterName": "", "@RoomId": UNASSIGNED,
                 "@Level": "1", "@Xp": "0", "@Fatigue": "0", "@TrainingDesignId": IDLE, "@TrainingEndDate": "",
                 **{key: "0" for key in STAT_KEYS}}
TRAINING_BLOCK_KEYS = {"fatigue_max", "fatigue_recovery_seconds", "character_designs", "training_designs",
                       "item_designs"}
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"  # @TrainingEndDate, in UTC without an offset, as the server sends it


def _container(key: str, records: list) -> Optional[dict]:
    """What xmltodict parses a container to: ``None`` when empty, ``{key: record}`` for a lone record (not a
    one-item list), else ``{key: records}``."""
    if not records:
        return None
    return {key: records[0] if len(records) == 1 else records}


def _strings(record: Mapping[str, Any]) -> dict:
    """``record`` as xmltodict gives it: every value a string (a JSON number becomes its digits), a null left out."""
    return {key: str(value) for key, value in record.items() if value is not None}


def _same_id(value: Any, wanted: str) -> bool:
    """Whether an id from scenario state (a string, or a JSON number) is ``wanted``."""
    return value is not None and str(value) == wanted


@dataclass(frozen=True)
class TrainingDesign:
    record: Mapping[str, Any]
    stat: str
    room: str  # the root room design that trains it; its upgrades do too
    duration: int  # seconds
    fatigue: int  # added when the session starts
    low: int
    high: int
    research: Optional[str]  # a research design that must be Completed first, if any


@dataclass(frozen=True)
class ItemDesign:
    record: Mapping[str, Any]
    stat: Optional[str]
    gain: int
    fatigue: int
    recovery: int


@dataclass(frozen=True)
class CharacterDesign:
    record: Mapping[str, Any]
    capacity: int  # @TrainingCapacity: the most the nine training stats may add up to


@dataclass(frozen=True)
class TrainingRules:
    """The scenario's ``training`` block: fatigue rules and character, training and item designs."""

    fatigue_max: int = 100
    recovery_seconds: int = 360  # one fatigue point recovers per this many seconds
    characters: Mapping[str, CharacterDesign] = field(default_factory=dict)
    trainings: Mapping[str, TrainingDesign] = field(default_factory=dict)
    items: Mapping[str, ItemDesign] = field(default_factory=dict)

    @classmethod
    def from_block(cls, block: Optional[Mapping[str, Any]], design: DesignData) -> "TrainingRules":
        """Parse and check a ``training`` block (``None``: no crew training); a malformed one raises ValueError.

        Design records are kept as the reads return them: strings only, like xmltodict's."""
        if block is None:
            block = {}
        if not isinstance(block, Mapping):
            raise ValueError(f"training block must be a mapping of keys to values, not {type(block).__name__}")
        unknown = sorted(set(block) - TRAINING_BLOCK_KEYS)
        if unknown:
            raise ValueError(f"training block has unknown keys: {', '.join(unknown)}")

        def by_id(key: str, id_key: str) -> dict:
            listed = block.get(key, [])
            if not isinstance(listed, list) or not all(isinstance(record, Mapping) for record in listed):
                raise ValueError(f"training block: {key} must be a list of records")
            records = [_strings(record) for record in listed]
            if any(record.get(id_key) in (None, "") for record in records):
                raise ValueError(f"training block: a {key} record has no {id_key}")
            ids = [str(record.get(id_key)) for record in records]
            _unique(ids, f"training block: duplicate {id_key}")
            return dict(zip(ids, records))

        rooms = {room.design_id for room in design.rooms}
        research = {entry.design_id for entry in design.research}
        trainings = {}
        for design_id, record in by_id("training_designs", "@TrainingDesignId").items():
            label = f"training design {design_id}"
            stat, room = record.get("@TrainingStat"), str(record.get("@RequiredRoomDesignId"))
            required = record.get("@RequiredResearchDesignId")
            if required is not None and str(required) in ("", "0"):  # how live records say "no research"
                required = None
            low, high = _int_field(record, "@MinGain", label), _int_field(record, "@MaxGain", label)
            for problem in (stat not in STATS and f"unknown @TrainingStat {stat!r}",
                            room not in rooms and f"unknown @RequiredRoomDesignId {room!r}",
                            required is not None and str(required) not in research
                            and f"unknown @RequiredResearchDesignId {required!r}",
                            low > high and f"@MinGain {low} is above @MaxGain {high}"):
                if problem:
                    raise ValueError(f"{label}: {problem}")
            trainings[design_id] = TrainingDesign(
                record, stat, room, _int_field(record, "@Duration", label, minimum=1),
                _int_field(record, "@Fatigue", label), low, high, None if required is None else str(required))
        items = {}
        for design_id, record in by_id("item_designs", "@ItemDesignId").items():
            label = f"item design {design_id}"
            stat = record.get("@ConsumableStat")
            if stat is not None and stat not in STATS:
                raise ValueError(f"{label}: unknown @ConsumableStat {stat!r}")
            gain, fatigue, recovery = (_int_field(record, key, label, default=0)
                                       for key in ("@ConsumableGain", "@ConsumableFatigue", "@FatigueRecovery"))
            if not (stat and gain) and not recovery:
                raise ValueError(f"{label} has no effect: give @ConsumableStat and @ConsumableGain, or @FatigueRecovery")
            items[design_id] = ItemDesign(record, stat, gain, fatigue, recovery)
        characters = {design_id: CharacterDesign(record, _int_field(record, "@TrainingCapacity",
                                                                    f"character design {design_id}", minimum=1))
                      for design_id, record in by_id("character_designs", "@CharacterDesignId").items()}
        return cls(_int_field(block, "fatigue_max", "training block", minimum=1, default=100),
                   _int_field(block, "fatigue_recovery_seconds", "training block", minimum=1, default=360),
                   characters, trainings, items)


def _int_field(record: Mapping[str, Any], key: str, label: str, *, minimum: int = 0, default: Any = None) -> int:
    """``record[key]`` as an int >= ``minimum``: an int or a string of digits, never a bool, float or sign."""
    value = record.get(key, default)
    if not str(value).isdecimal() or int(value) < minimum:
        raise ValueError(f"{label}: {key} must be an integer >= {minimum}, not {value!r}")
    return int(value)


def _unique(ids: list, what: str) -> None:
    duplicates = sorted(value for value, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"{what} {', '.join(duplicates)}")


def _item_error(message: str) -> dict:
    return {"ItemService": {"ActivateItem3": {"@errorMessage": message}}}


def _format_time(moment: datetime) -> str:
    """``moment`` (aware: ``SimulatedClock`` refuses a naive start) in UTC, as the server writes times."""
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


@dataclass
class _Member:
    record: dict  # the character record; its @Fatigue is recomputed on every read
    capacity: int
    fatigue: int  # fatigue at `since`; it recovers lazily from there
    since: datetime
    # Fatigue just before the running session's own fatigue was added; for crew loaded mid-session, whose start
    # is unknown, the loaded @Fatigue.
    fatigue_at_start: int


def parse_crew(alias: str, rules: TrainingRules, crew: Iterable[Mapping[str, Any]], rooms: Iterable[Mapping[str, Any]],
               now: datetime) -> dict[str, _Member]:
    """An account's scenario ``crew`` as members by character id, each record completed with ``CREW_DEFAULTS``;
    a malformed record raises ValueError.

    Two live shapes are normalised: an empty ``@RoomId`` becomes ``UNASSIGNED``, and an idle record keeping its last
    session's ``@TrainingEndDate`` loses it. A loaded session is taken as the server reported it: its room and
    research are not re-checked."""
    crew = [{**CREW_DEFAULTS, **{key: str(value) for key, value in raw.items()}} for raw in crew]
    _unique([record["@CharacterId"] for record in crew], f"account {alias}: duplicate @CharacterId")
    room_ids = {str(room.get("@RoomId")) for room in rooms if room.get("@RoomId") is not None}
    members = {}
    for record in crew:
        label = f"account {alias}: crew {record['@CharacterId']}"
        character, assigned = rules.characters.get(record["@CharacterDesignId"]), record["@TrainingDesignId"]
        if not record["@CharacterId"]:
            raise ValueError(f"account {alias}: a crew record has no @CharacterId")
        if character is None:
            raise ValueError(f"{label}: unknown @CharacterDesignId {record['@CharacterDesignId']!r}")
        unknown = sorted(set(record) - set(CREW_DEFAULTS))
        total = sum(_int_field(record, key, label) for key in STAT_KEYS)
        fatigue = _int_field(record, "@Fatigue", label)
        record["@RoomId"] = record["@RoomId"] or UNASSIGNED
        end = parse_server_datetime(record["@TrainingEndDate"])
        if assigned == IDLE and end is not None:
            record["@TrainingEndDate"] = ""
        end_fits = end is not None if assigned != IDLE else not record["@TrainingEndDate"]
        for problem in (unknown and f"unknown keys: {', '.join(unknown)}",
                        total > character.capacity and f"stats total {total} is over the training capacity {character.capacity}",
                        record["@RoomId"] != UNASSIGNED and record["@RoomId"] not in room_ids
                        and f"unknown @RoomId {record['@RoomId']!r}",
                        fatigue > rules.fatigue_max and f"@Fatigue {fatigue} is above the maximum {rules.fatigue_max}",
                        assigned != IDLE and assigned not in rules.trainings
                        and f"unknown @TrainingDesignId {assigned!r}",
                        not end_fits and f"@TrainingEndDate {record['@TrainingEndDate']!r} does not fit "
                                         f"@TrainingDesignId {assigned!r} (a {TIME_FORMAT} time; idle crew may leave it empty)"):
            if problem:
                raise ValueError(f"{label}: {problem}")
        members[record["@CharacterId"]] = _Member(record, character.capacity, fatigue, now, fatigue)
    return members


class CrewTraining:
    """One account's crew and its training, as a pure state machine.

    It reads no clock (every call takes ``now``) and keeps none of the game's other state: a call that depends on
    rooms, research or an item is handed them. It draws only from its own generator, once per finished session.
    ``begin``, ``refuse``, ``finish`` and ``consume`` each append one entry to ``log``, stamped with the scenario
    ``day`` they are handed (the game's, the one its faults count by).
    """

    def __init__(self, rules: TrainingRules, members: dict[str, _Member], design: DesignData,
                 rng: random.Random) -> None:
        self.rules = rules
        self.members = members
        self.rng = rng
        self.upgrade_from = {room.design_id: room.upgrade_from for room in design.rooms}
        self.log: list[dict] = []

    # -- observation -----------------------------------------------------------------

    def fatigue(self, member: _Member, now: datetime) -> int:
        recovered = (now - member.since) // timedelta(seconds=self.rules.recovery_seconds)
        return max(0, member.fatigue - recovered)

    def record(self, character_id: str, now: datetime) -> dict:
        member = self.members[character_id]
        return dict(member.record, **{"@Fatigue": str(self.fatigue(member, now))})

    def records(self, now: datetime) -> list[dict]:
        return [self.record(character_id, now) for character_id in self.members]

    def trains(self, room: Mapping[str, Any]) -> list[str]:
        """The training designs ``room`` trains by its current design: its own, and those of the designs it was
        upgraded from."""
        lineage, current = [], str(room.get("@RoomDesignId"))
        while current is not None and current not in lineage:
            lineage.append(current)
            current = self.upgrade_from.get(current)
        return [design_id for design_id, design in self.rules.trainings.items() if design.room in lineage]

    def training_rooms(self, rooms: Iterable[Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
        """Room id -> the training designs it trains, for every room that trains one."""
        trained = {str(room.get("@RoomId")): tuple(self.trains(room)) for room in rooms}
        return {room_id: designs for room_id, designs in trained.items() if designs}

    # -- the state machine -----------------------------------------------------------

    def check_start(self, character_id: str, design_id: str, rooms: Iterable[Mapping[str, Any]],
                    research: Iterable[Mapping[str, Any]]) -> Optional[tuple[str, str]]:
        """Why the server refuses to start ``design_id`` for the member, as (code, detail); ``None`` if it starts."""
        member, design = self.members.get(character_id), self.rules.trainings.get(design_id)
        if member is None:
            return "unknown-character", "unknown character"
        if design is None:
            return "unknown-design", "unknown training design"
        if self._training(member):
            return "busy", f"already training {member.record['@TrainingDesignId']}"
        room_id = member.record["@RoomId"]
        room = {} if room_id == UNASSIGNED else next(
            (room for room in rooms if _same_id(room.get("@RoomId"), room_id)), {})
        if design_id not in self.trains(room):
            return "wrong-room", f"room {'(none)' if room_id == UNASSIGNED else room_id} does not train {design_id}"
        if room.get("@RoomStatus") == "Upgrading":
            return "room-upgrading", f"room {room_id} is being upgraded"
        if design.research is not None and not any(
                _same_id(entry.get("@ResearchDesignId"), design.research) and entry.get("@ResearchState") == "Completed"
                for entry in research):
            return "no-research", f"research {design.research} is not completed"
        return None

    def refuse(self, character_id: str, design_id: str, now: datetime, day: int, code: str, detail: str) -> dict:
        """Log a start refused with ``check_start``'s (code, detail)."""
        return self._entry("start", character_id, design_id, now, day, code, detail, requested=design_id)

    def begin(self, character_id: str, design_id: str, now: datetime, day: int, *, requested: str) -> dict:
        """Start ``design_id``, for a request of ``requested`` that ``check_start`` passed (they differ only when
        the server starts another design)."""
        member, design = self.members[character_id], self.rules.trainings[design_id]
        member.fatigue_at_start = self.fatigue(member, now)
        self._set_fatigue(member, member.fatigue_at_start + design.fatigue, now)
        end = _format_time(now + timedelta(seconds=design.duration))
        member.record.update({"@TrainingDesignId": design_id, "@TrainingEndDate": end})
        return self._entry("start", character_id, design_id, now, day, requested=requested, end=end)

    def finish(self, character_id: str, now: datetime, day: int) -> dict:
        member = self.members.get(character_id)
        session = member.record["@TrainingDesignId"] if member is not None and self._training(member) else None
        refusal = (("unknown-character", "unknown character") if member is None
                   else ("idle", "not training") if session is None
                   else ("early", f"still training until {member.record['@TrainingEndDate']}")
                   if now < parse_server_datetime(member.record["@TrainingEndDate"]) else None)
        if refusal:
            return self._entry("finish", character_id, session, now, day, *refusal)
        design, top = self.rules.trainings[session], self.rules.fatigue_max
        raw = self.rng.randint(design.low, design.high)
        gain = self._add(member, design.stat, raw * (top - member.fatigue_at_start) // top)
        member.record.update({"@TrainingDesignId": IDLE, "@TrainingEndDate": ""})
        return self._entry("finish", character_id, session, now, day, **gain)

    def consume(self, character_id: str, item: Optional[Mapping[str, Any]], now: datetime, day: int) -> dict:
        """Use one of ``item`` (an inventory record, or ``None`` for an unknown item id) on the member: its stat
        gain, capped like training, and its fatigue change. Allowed while training. The caller takes the item."""
        member = self.members.get(character_id)
        design_id = None if item is None else str(item.get("@ItemDesignId"))
        design = self.rules.items.get(design_id)
        refusal = (("unknown-item", "unknown item") if item is None
                   else ("not-consumable", "not a consumable") if design is None
                   else ("out-of-stock", "out of stock") if int(item["@Quantity"]) < 1
                   else ("unknown-character", "unknown character") if member is None else None)
        if refusal:
            return self._entry("consume", character_id, design_id, now, day, *refusal)
        gain = self._add(member, design.stat, design.gain) if design.stat else {}
        self._set_fatigue(member, self.fatigue(member, now) + design.fatigue - design.recovery, now)
        return self._entry("consume", character_id, design_id, now, day, **gain)

    def _training(self, member: _Member) -> bool:
        return member.record["@TrainingDesignId"] != IDLE

    def _set_fatigue(self, member: _Member, value: int, now: datetime) -> None:
        """Set the fatigue at ``now``, within 0 and the maximum, keeping the progress toward the next recovered point
        while the member is still recovering, so a change never delays recovery by itself."""
        period = timedelta(seconds=self.rules.recovery_seconds)
        carried = (now - member.since) % period if self.fatigue(member, now) > 0 else timedelta(0)
        member.fatigue, member.since = max(0, min(value, self.rules.fatigue_max)), now - carried

    def _total(self, member: _Member) -> int:
        return sum(int(member.record[key]) for key in STAT_KEYS)

    def _add(self, member: _Member, stat: str, amount: int) -> dict:
        """Add up to ``amount`` to ``stat`` without taking the total over the capacity."""
        key = f"@{stat}Improvement"
        added = min(amount, member.capacity - self._total(member))
        member.record[key] = str(int(member.record[key]) + added)
        return {"gain": {stat: added}, "capped": added < amount}

    def _entry(self, op: str, character_id: str, design_id: Optional[str], now: datetime, day: int,
               code: str = "ok", detail: str = "", *, requested: Optional[str] = None, end: Optional[str] = None,
               gain: Optional[dict] = None, capped: bool = False) -> dict:
        member = self.members.get(character_id)
        known = member is not None
        design = (self.rules.items if op == "consume" else self.rules.trainings).get(design_id)
        entry = {"seq": len(self.log) + 1, "at": _format_time(now), "day": day, "op": op,
                 "character_id": character_id, "room_id": member.record["@RoomId"] if known else None,
                 "design_id": design_id, "requested_design_id": requested,
                 "stat": None if design is None else design.stat, "accepted": code == "ok", "code": code,
                 "detail": detail, "end": end, "gain": gain or {}, "capped": capped,
                 "fatigue": self.fatigue(member, now) if known else None,
                 "total": self._total(member) if known else None,
                 "capacity": member.capacity if known else None}
        self.log.append(entry)
        return entry


class SimulatedGame:
    def __init__(self, alias: str, state: Mapping[str, Any], design: DesignData, rng: random.Random,
                 clock: SimulatedClock, *, faults: Iterable[Mapping[str, Any] | Fault] = (),
                 fault_rng: Optional[random.Random] = None,
                 training: Optional[Mapping[str, Any]] = None,
                 training_rng: Optional[random.Random] = None) -> None:
        self.alias = alias
        self.design = design
        self.rng = rng
        self.clock = clock
        self.minerals = int(state.get("minerals", 0))
        self.gas = int(state.get("gas", 0))
        self.starbux = int(state.get("starbux", 0))
        self.trophies = int(state.get("trophies", 0))
        self.rooms = [dict(room) for room in state.get("rooms", [])]
        self.research = [dict(entry) for entry in state.get("research", [])]
        self.inventory = [dict(item) for item in state.get("inventory", [])]
        self.reward_collected_on: Optional[int] = None
        self.calls: list[str] = []
        self._upgrade_targets: dict[str, str] = {}
        self.faults = tuple(f if isinstance(f, Fault) else Fault.from_dict(f) for f in faults)
        self.fault_rng = fault_rng or random.Random(f"{alias}:faults")
        self.fired: list[str] = []  # "day <n> <method> #<call>: <kind>", in firing order
        self.day = 0
        self._counts: dict[tuple, int] = {}
        self._pending_read_faults: list[Fault] = []
        self.days: list[dict] = []  # per start_day: the crew and the training rooms at that day's start
        # Fail closed, like faults: malformed training data is refused before anything runs.
        if training is None and state.get("crew"):
            raise ValueError(f"account {alias} has crew but the scenario has no training block")
        rules = self.rules = TrainingRules.from_block(training, design)
        # Items are checked only for crew training: without a block no item is a consumable, so none is used.
        listed = [item for item in self.inventory if "@ItemId" in item] if training is not None else []
        _unique([str(item["@ItemId"]) for item in listed], f"account {alias}: duplicate @ItemId")
        for item in listed:
            _int_field(item, "@Quantity", f"account {alias}: item {item['@ItemId']}")
        for fault in self.faults:
            if fault.training_design_id is not None and fault.training_design_id not in rules.trainings:
                raise ValueError(f"fault {fault.kind!r}: unknown training design {fault.training_design_id!r}")
        self.training = CrewTraining(rules, parse_crew(alias, rules, state.get("crew", ()), self.rooms, clock.now),
                                     design, training_rng or random.Random(f"{alias}:training"))

    # -- fault injection -------------------------------------------------------------

    def _triggered(self, method: str) -> list[Fault]:
        """Count one invocation of ``method`` and return the faults it fires."""
        for key in ((method,), (self.day, method)):
            self._counts[key] = self._counts.get(key, 0) + 1
        fired = []
        for fault in self.faults:
            if fault.method != method or (fault.day is not None and fault.day != self.day):
                continue
            count = self._counts[(self.day, method) if fault.day is not None else (method,)]
            if fault.call is not None and fault.call != count:
                continue
            if fault.probability < 1.0 and self.fault_rng.random() >= fault.probability:
                continue
            self.fired.append(f"day {self.day} {method} #{self._counts[(self.day, method)]}: {fault.kind}")
            fired.append(fault)
        return fired

    def _player_spends(self, fault: Fault) -> None:
        for kind, amount in fault.spend.items():
            setattr(self, kind, max(getattr(self, kind) - amount, 0))

    def _invoke(self, method: str, effect: Callable[[], Any], rejected: Callable[[], Any] = lambda: False,
                contradicted: Optional[Callable[[str], Any]] = None) -> Any:
        """Call ``effect`` the way the server would, unless a fault changes it.

        ``rejected`` gives the server's refusal (a ``reject`` fault; by default the Client's ``False``);
        ``contradicted`` is the effect with another design, which a ``contradicted-effect`` fault calls with its
        substitute (``Fault`` allows that kind on ``addTraining`` only, which passes it).
        """
        self.calls.append(method)
        fired = self._triggered(method)
        call_fault = next((f for f in fired if f.kind in CALL_FAULTS), None)
        outcome = next((f for f in fired if f.kind in OUTCOME_FAULTS), None)
        if outcome is not None:
            effect = rejected if outcome.kind == "reject" else partial(contradicted, outcome.training_design_id)
        result = effect() if call_fault is None or call_fault.effect else None
        for fault in fired:
            if fault.kind == "concurrent-spend":
                self._player_spends(fault)
            elif fault.kind in READ_FAULTS:
                self._pending_read_faults.append(fault)
        if call_fault is not None and call_fault.kind == "raise":
            raise SimulatedFaultError(f"injected fault: {method} raised after the request was sent")
        if call_fault is not None:
            return None  # neither True nor False: the runner treats it as ambiguous
        return result

    # -- observation -----------------------------------------------------------------

    def snapshot(self) -> AccountSnapshot:
        read_faults = self._pending_read_faults + self._triggered(SNAPSHOT)
        self._pending_read_faults = []
        alias = self.alias
        for fault in read_faults:
            if fault.kind == "concurrent-spend":
                self._player_spends(fault)
        for fault in read_faults:
            if fault.kind == "snapshot-failure":
                raise SimulatedFaultError("injected fault: snapshot read failed")
            if fault.kind == "foreign-snapshot":
                alias = fault.alias
        observed_at = self.clock()
        return build_snapshot(
            alias, observed_at, "simulation",
            profile={"@Trophy": str(self.trophies)},
            ship={"@ShipLevel": "1"},
            resources={"minerals": str(self.minerals), "gas": str(self.gas), "starbux": str(self.starbux)},
            rooms=[dict(room) for room in self.rooms],
            research=[dict(entry) for entry in self.research],
            inventory=[dict(item) for item in self.inventory],
            crew=self.training.records(observed_at),
        )

    # -- reads: plain methods with the Client's return value and response shape; not calls, no faults --

    def listAllCharactersOfUser(self) -> bool:
        records = self.training.records(self.clock.now)
        self.allCharactersOfUser = {"CharacterService": {"ListAllCharactersOfUser": {
            "Characters": _container("Character", records)}}}
        return True

    def listAllTrainingDesigns2(self) -> bool:
        records = [dict(design.record) for design in self.rules.trainings.values()]
        self.trainingDesigns = {"TrainingService": {"ListAllTrainingDesigns": {
            "TrainingDesigns": _container("TrainingDesign", records)}}}
        return True

    def listAllCharacterDesigns2(self) -> bool:
        records = [dict(design.record) for design in self.rules.characters.values()]
        self.allCharacterDesigns = {"CharacterService": {"ListAllCharacterDesigns": {
            "CharacterDesigns": _container("CharacterDesign", records)}}}
        return True

    def listItemsOfAShip(self) -> bool:
        records = [_strings(item) for item in self.inventory]
        self.itemsOfAShip = {"ItemService": {"ListItemsOfAShip": {"Items": _container("Item", records)}}}
        return True

    def start_day(self, day: int) -> None:
        """Begin scenario day ``day``: finish timers, and count fault triggers and log training within it."""
        self.day = day
        self.finish_day()
        now = self.clock.now  # read, not ticked, so economy timestamps stay as they were
        self.days.append({"day": day, "at": _format_time(now), "crew": tuple(self.training.records(now)),
                          "training_rooms": self.training.training_rooms(self.rooms)})

    def finish_day(self) -> None:
        for room in self.rooms:
            if room.get("@RoomStatus") == "Upgrading":
                room["@RoomDesignId"] = self._upgrade_targets.pop(room["@RoomId"], room["@RoomDesignId"])
                room["@RoomStatus"] = "Normal"
        for entry in self.research:
            if entry.get("@ResearchState") == "Researching":
                entry["@ResearchState"] = "Completed"

    # -- Client surface used by the action catalog -----------------------------------

    def collectAllResources(self) -> object:
        return self._invoke("collectAllResources", self._collect_all_resources)

    def collectDailyReward(self) -> object:
        return self._invoke("collectDailyReward", self._collect_daily_reward)

    def upgradeRoom(self, room_id: str, upgrade_room_design_id: str) -> object:
        return self._invoke("upgradeRoom", lambda: self._upgrade_room(room_id, upgrade_room_design_id))

    def addResearch(self, research_design_id: str) -> object:
        return self._invoke("addResearch", lambda: self._add_research(research_design_id))

    def addTraining(self, training_design_id: str, character_id: str) -> object:
        character_id, requested = str(character_id), str(training_design_id)

        def start(design_id: str) -> bool:
            # The server checks the request; one it accepts starts ``design_id`` (another design when contradicted).
            refusal = self.training.check_start(character_id, requested, self.rooms, self.research)
            if refusal:
                return self.training.refuse(character_id, requested, self.clock.now, self.day, *refusal)["accepted"]
            return self.training.begin(character_id, design_id, self.clock.now, self.day,
                                       requested=requested)["accepted"]

        return self._invoke("addTraining", partial(start, requested), contradicted=start)

    def finishTraining(self, character_id: str) -> object:
        def finish() -> bool:
            entry = self.training.finish(str(character_id), self.clock.now, self.day)
            if entry["accepted"]:
                self.trainingFinish = {"TrainingService": {"FinishTraining": {
                    "Character": self.training.record(str(character_id), self.clock.now)}}}
            return entry["accepted"]

        return self._invoke("finishTraining", finish)

    def activateItem3(self, itemId: Any = 0, targetId: Any = 0) -> None:
        """Use item ``itemId`` on crew member ``targetId``; the outcome is in ``self.item``, as with the Client."""
        self.item = None  # no reply (a call fault without effect) leaves no response, and no error
        self._invoke("activateItem3", lambda: self._activate_item(str(itemId), str(targetId)),
                     rejected=lambda: setattr(self, "item", _item_error(f"item {itemId}: refused by the server")))

    def useConsumable(self, consumable_item_design_id: Any, character_id: Any) -> bool:
        """``Client.useConsumable``: the first listed item of that design, then ``activateItem3``.

        The Client reuses its last item list; this one reads the inventory as it is. Simulated item ids never
        change, so the item chosen is the same."""
        item_id = next((item.get("@ItemId") for item in self.inventory
                        if _same_id(item.get("@ItemDesignId"), str(consumable_item_design_id))), None)
        if not item_id:
            return False
        self.activateItem3(itemId=item_id, targetId=character_id)
        return not (self.item and self.item["ItemService"]["ActivateItem3"].get("@errorMessage"))

    def runBattleEndToEnd(self) -> object:
        return self._invoke("runBattleEndToEnd", self._run_battle)

    # -- effects of a call that reaches the server -----------------------------------

    def _collect_all_resources(self) -> bool:
        self.minerals += self.rng.randint(20, 120)
        self.gas += self.rng.randint(10, 60)
        return True

    def _collect_daily_reward(self) -> bool:
        day = (self.clock.now - self.clock.start).days
        if self.reward_collected_on == day:
            return True  # already collected today: no observable change
        self.reward_collected_on = day
        self.starbux += self.rng.randint(1, 3)
        return True

    def _upgrade_room(self, room_id: str, upgrade_room_design_id: str) -> object:
        room = next((r for r in self.rooms if r.get("@RoomId") == room_id), None)
        target = next((d for d in self.design.rooms if d.design_id == str(upgrade_room_design_id)), None)
        if room is None or target is None or room.get("@RoomStatus") == "Upgrading":
            return object()  # raw response; the runner treats it as ambiguous
        # Charge the price in its own currency; refuse an unaffordable or unmodelled one.
        cost = target.cost_amount or 0
        if target.cost_kind and cost > 0:
            if target.cost_kind == "mineral" and self.minerals >= cost:
                self.minerals -= cost
            elif target.cost_kind == "gas" and self.gas >= cost:
                self.gas -= cost
            else:
                return object()
        room["@RoomStatus"] = "Upgrading"
        self._upgrade_targets[room_id] = str(upgrade_room_design_id)
        return object()

    def _add_research(self, research_design_id: str) -> bool:
        design = next((d for d in self.design.research if d.design_id == str(research_design_id)), None)
        cost = (design.gas_cost or 0) if design is not None else 0
        if self.gas < cost or any(e.get("@ResearchDesignId") == str(research_design_id) for e in self.research):
            return False
        self.gas -= cost
        self.research.append({"@ResearchDesignId": str(research_design_id), "@ResearchState": "Researching"})
        return True

    def _activate_item(self, item_id: str, character_id: str) -> None:
        item = next((entry for entry in self.inventory if _same_id(entry.get("@ItemId"), item_id)), None)
        entry = self.training.consume(character_id, item, self.clock.now, self.day)
        if not entry["accepted"]:
            self.item = _item_error(f"item {item_id}: {entry['detail']}")
            return
        item["@Quantity"] = str(int(item["@Quantity"]) - 1)
        self.item = {"ItemService": {"ActivateItem3": {
            "Item": _strings(item), "Character": self.training.record(character_id, self.clock.now)}}}

    def _run_battle(self) -> bool:
        self.trophies += self.rng.choice((-1, 1)) * self.rng.randint(5, 30)
        return True


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    seed: int
    fleet_runs: tuple[FleetRun, ...]
    progression_json: str
    fleet_json: str
    audit_json: str
    calls: Mapping[str, tuple[str, ...]]
    faults: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # faults fired, per account
    # Per account: the crew records at the end of the run; the training state machine's log (one entry per
    # addTraining, finishTraining or item use that reached it); and per day, the crew and the training rooms
    # (room id -> training design ids) at the day's start. With the run's span (UTC, like the log's times),
    # the training_json property holds them all, byte-stable per seed.
    crew: Mapping[str, tuple[Mapping[str, str], ...]] = field(default_factory=dict)
    training_log: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    days: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""

    @property
    def training_json(self) -> str:
        return json.dumps({"crew": self.crew, "log": self.training_log, "days": self.days,
                           "started_at": self.started_at, "ended_at": self.ended_at}, sort_keys=True, indent=2)

    @property
    def initial_crew(self) -> dict[str, tuple[Mapping[str, str], ...]]:
        """Each account's crew at the scenario start: day 0's start, before any call."""
        return {alias: days[0]["crew"] for alias, days in self.days.items()}


def load_scenario(name_or_path: str | Path) -> dict:
    path = Path(name_or_path)
    if not path.suffix:
        path = SCENARIO_DIR / f"{name_or_path}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _renumber(journals: Mapping[str, list[GameplayEvent]]) -> dict[str, list[GameplayEvent]]:
    ordered = sorted((event.started_at, alias, index) for alias, events in journals.items()
                     for index, event in enumerate(events))
    mapping: dict[str, str] = {}
    for _, alias, index in ordered:
        original = journals[alias][index].attempt_id
        mapping.setdefault(original, f"sim-{len(mapping) + 1:05d}")
    return {alias: [replace(event, attempt_id=mapping[event.attempt_id]) for event in events]
            for alias, events in journals.items()}


def build_games(scenario: Mapping[str, Any], seed: Any, design: DesignData,
                 clock: SimulatedClock) -> dict[str, SimulatedGame]:
    """One game per account, in alias order. They share the economy generator; faults and training each have
    their own per-account generator, so adding either never shifts the economy's random amounts."""
    rng = random.Random(seed)
    return {alias: SimulatedGame(alias, state, design, rng, clock, faults=state.get("faults", ()),
                                 fault_rng=random.Random(f"{seed}:{alias}:faults"),
                                 training=scenario.get("training"),
                                 training_rng=random.Random(f"{seed}:{alias}:training"))
            for alias, state in sorted(scenario["accounts"].items())}


def run_scenario(scenario: Mapping[str, Any], workdir: Path | str, *, seed: Optional[int] = None) -> ScenarioResult:
    """Run ``scenario`` with its journals in ``workdir``.

    Journals are append-only, so a workdir that already holds a journal for
    one of the scenario's accounts (``<alias>.jsonl``) is refused with
    ``FileExistsError`` rather than appended to; nothing is written in that
    case. Other files in the workdir are left alone."""
    workdir = Path(workdir)
    seed = scenario.get("seed", 0) if seed is None else seed
    clock = SimulatedClock(datetime.fromisoformat(scenario["start"]))
    design = DesignData.from_records(**scenario.get("design", {}))
    registry = registry_from_dict(scenario["registry"])
    games = build_games(scenario, seed, design, clock)
    existing = [str(workdir / f"{alias}.jsonl") for alias in games if (workdir / f"{alias}.jsonl").exists()]
    if existing:
        raise FileExistsError(f"scenario workdir already has journals ({', '.join(existing)}); "
                              "use an empty directory so the run starts from empty journals")

    def factory(profile) -> AccountContext:
        game = games[profile.alias]
        return AccountContext(game, game.snapshot, GameplayJournal(workdir / f"{profile.alias}.jsonl"), design)

    runs = []
    for day in range(int(scenario.get("days", 1))):
        clock.start_day(day)
        for game in games.values():
            game.start_day(day)
        orchestrator = FleetOrchestrator(registry, factory, clock=clock, timer=lambda: 0.0,
                                         stop_file=workdir / "STOP")
        runs.append(orchestrator.run(execute=bool(scenario.get("execute", False))))

    journals = _renumber({alias: GameplayJournal(workdir / f"{alias}.jsonl").read() for alias in sorted(games)})
    audit = audit_fleet(registry, journals)
    all_events = [event for alias in sorted(journals) for event in journals[alias]]
    return ScenarioResult(
        name=scenario.get("name", "scenario"),
        seed=seed,
        fleet_runs=tuple(runs),
        progression_json=progression_report.to_json(compute_progression_metrics(all_events)),
        fleet_json=fleet_report.to_json(fleet_report.compute_fleet_report(journals, fleet_run=runs[-1], audit=audit)),
        audit_json=audit.to_json(),
        calls={alias: tuple(game.calls) for alias, game in games.items()},
        faults={alias: tuple(game.fired) for alias, game in games.items()},
        crew={alias: tuple(game.training.records(clock.now)) for alias, game in games.items()},
        training_log={alias: tuple(game.training.log) for alias, game in games.items()},
        days={alias: tuple(game.days) for alias, game in games.items()},
        started_at=_format_time(clock.start),
        ended_at=_format_time(clock.now),
    )
