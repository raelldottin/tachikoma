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
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

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


class SimulatedGame:
    def __init__(self, alias: str, state: Mapping[str, Any], design: DesignData, rng: random.Random,
                 clock: SimulatedClock) -> None:
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

    # -- observation -----------------------------------------------------------------

    def snapshot(self) -> AccountSnapshot:
        return build_snapshot(
            self.alias, self.clock(), "simulation",
            profile={"@Trophy": str(self.trophies)},
            ship={"@ShipLevel": "1"},
            resources={"minerals": str(self.minerals), "gas": str(self.gas), "starbux": str(self.starbux)},
            rooms=[dict(room) for room in self.rooms],
            research=[dict(entry) for entry in self.research],
            inventory=[dict(item) for item in self.inventory],
            crew=[],
        )

    def finish_day(self) -> None:
        for room in self.rooms:
            if room.get("@RoomStatus") == "Upgrading":
                room["@RoomDesignId"] = self._upgrade_targets.pop(room["@RoomId"], room["@RoomDesignId"])
                room["@RoomStatus"] = "Normal"
        for entry in self.research:
            if entry.get("@ResearchState") == "Researching":
                entry["@ResearchState"] = "Completed"

    # -- Client surface used by the action catalog -----------------------------------

    def collectAllResources(self) -> bool:
        self.calls.append("collectAllResources")
        self.minerals += self.rng.randint(20, 120)
        self.gas += self.rng.randint(10, 60)
        return True

    def collectDailyReward(self) -> bool:
        self.calls.append("collectDailyReward")
        day = (self.clock.now - self.clock.start).days
        if self.reward_collected_on == day:
            return True  # already collected today: no observable change
        self.reward_collected_on = day
        self.starbux += self.rng.randint(1, 3)
        return True

    def upgradeRoom(self, room_id: str, upgrade_room_design_id: str) -> object:
        self.calls.append("upgradeRoom")
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

    def addResearch(self, research_design_id: str) -> bool:
        self.calls.append("addResearch")
        design = next((d for d in self.design.research if d.design_id == str(research_design_id)), None)
        cost = (design.gas_cost or 0) if design is not None else 0
        if self.gas < cost or any(e.get("@ResearchDesignId") == str(research_design_id) for e in self.research):
            return False
        self.gas -= cost
        self.research.append({"@ResearchDesignId": str(research_design_id), "@ResearchState": "Researching"})
        return True

    def addTraining(self, training_design_id: str, character_id: str) -> bool:
        self.calls.append("addTraining")
        return False

    def runBattleEndToEnd(self) -> bool:
        self.calls.append("runBattleEndToEnd")
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
    games = {alias: SimulatedGame(alias, state, design, rng, clock)
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
            game.finish_day()
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
    )
