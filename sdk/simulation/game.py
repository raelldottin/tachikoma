"""Deterministic offline gameplay scenarios (R6, AUTO-014).

``SimulatedGame`` stands in for ``sdk.client.Client`` with the method names
the action catalog uses. Its state (resources, trophies, rooms, research,
inventory) changes the way a successful server call would, with amounts
drawn from a seeded ``random.Random``. Nothing touches the network.

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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

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
    """Advances one second per reading; jumps to day boundaries on request."""

    def __init__(self, start: datetime) -> None:
        self.start = start
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    def start_day(self, day: int) -> None:
        self.now = max(self.now, self.start + timedelta(days=day))


# Client methods the simulated game provides, and the pseudo-method "snapshot"
# (a read of the snapshot provider), which faults can be attached to.
CLIENT_METHODS = ("collectAllResources", "collectDailyReward", "upgradeRoom", "addResearch", "addTraining",
                  "runBattleEndToEnd")
SNAPSHOT = "snapshot"
FAULT_KINDS = ("snapshot-failure", "raise", "ambiguous-response", "concurrent-spend", "foreign-snapshot")
# Faults that change what a call returns; they only attach to client methods.
CALL_FAULTS = ("raise", "ambiguous-response")
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
      just before the snapshot read, so it lands between two snapshots.
    """

    kind: str
    method: str
    day: Optional[int] = None
    call: Optional[int] = None
    probability: float = 1.0
    effect: bool = True
    alias: Optional[str] = None
    spend: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Fail closed: a mistyped fault must not silently fire on every call, or never.
        kind = self.kind
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind {kind!r}; expected one of {', '.join(FAULT_KINDS)}")
        if self.method not in CLIENT_METHODS + (SNAPSHOT,):
            raise ValueError(f"fault {kind!r} has unknown method {self.method!r}")
        if kind in CALL_FAULTS and self.method == SNAPSHOT:
            raise ValueError(f"fault {kind!r} applies to a client method, not to snapshot reads")
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
                   alias=data.get("alias"), spend={name: data[name] for name in RESOURCES if name in data})


# Keys each kind accepts besides kind, method, day, call and probability.
FAULT_OPTIONS = {
    "raise": {"effect"},
    "ambiguous-response": {"effect"},
    "snapshot-failure": set(),
    "foreign-snapshot": {"alias"},
    "concurrent-spend": set(RESOURCES),
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class SimulatedGame:
    def __init__(self, alias: str, state: Mapping[str, Any], design: DesignData, rng: random.Random,
                 clock: SimulatedClock, *, faults: Iterable[Mapping[str, Any] | Fault] = (),
                 fault_rng: Optional[random.Random] = None) -> None:
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

    def _invoke(self, method: str, effect: Callable[[], Any]) -> Any:
        self.calls.append(method)
        fired = self._triggered(method)
        call_fault = next((f for f in fired if f.kind in CALL_FAULTS), None)
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
        return build_snapshot(
            alias, self.clock(), "simulation",
            profile={"@Trophy": str(self.trophies)},
            ship={"@ShipLevel": "1"},
            resources={"minerals": str(self.minerals), "gas": str(self.gas), "starbux": str(self.starbux)},
            rooms=[dict(room) for room in self.rooms],
            research=[dict(entry) for entry in self.research],
            inventory=[dict(item) for item in self.inventory],
            crew=[],
        )

    def start_day(self, day: int) -> None:
        """Begin scenario day ``day``: finish timers, and count fault triggers within it."""
        self.day = day
        self.finish_day()

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
        return self._invoke("addTraining", lambda: False)

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


def run_scenario(scenario: Mapping[str, Any], workdir: Path | str, *, seed: Optional[int] = None) -> ScenarioResult:
    """Run ``scenario`` with its journals in ``workdir``.

    Journals are append-only, so a workdir that already holds a journal for
    one of the scenario's accounts (``<alias>.jsonl``) is refused with
    ``FileExistsError`` rather than appended to; nothing is written in that
    case. Other files in the workdir are left alone."""
    workdir = Path(workdir)
    seed = scenario.get("seed", 0) if seed is None else seed
    rng = random.Random(seed)
    clock = SimulatedClock(datetime.fromisoformat(scenario["start"]))
    design = DesignData.from_records(**scenario.get("design", {}))
    registry = registry_from_dict(scenario["registry"])
    games = {alias: SimulatedGame(alias, state, design, rng, clock, faults=state.get("faults", ()),
                                  fault_rng=random.Random(f"{seed}:{alias}:faults"))
             for alias, state in sorted(scenario["accounts"].items())}
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
    )
