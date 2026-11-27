"""Offline end-to-end tests of the real daily run: ``run.main()`` and the real ``Client`` against FakePss.

Nothing here touches the network or real credentials. The fake answers at the requests adapter, the clock is frozen
at FAKE_EPOCH, and every random draw is seeded, so two runs send byte-identical requests.

The host clock
    The goldens run on a UTC host, like the ubuntu-latest runner of .github/workflows/daily-run.yml, so they pin what
    the scheduled run really sends. A UTC host cannot tell local time from UTC, so NonUtcHostTests reruns the default
    world on a host behind UTC (UTC-5) and one ahead of it (UTC+9): every server date is UTC, so each decision must match
    the UTC run, and the two that do not are pinned (TZ1, TZ2).

What the log reports
    The run's log is the emailed report. ``assert_reports_match_server`` checks that its per-record success lines
    ("Starting training", "Collected mining drone") name exactly what the fake applied, so a swallowed error that the
    request goldens cannot see still fails.

How the goldens are organised
    ``run_daily`` records which daily step (the Client method run.main() called) sent each request, so the goldens are
    dicts keyed by step and a change in one step fails only that step's subTest. manageTraining is owned by
    EXPECTED_TRAINING_PATHS, EXPECTED_TRAINING and EXPECTED_SECOND_RUN_TRAINING; every other golden leaves it out, so
    an intended manageTraining rewrite (Wave 2) edits only those three. Failure, variant and multi-run tests compare
    against an un-faulted run of the same world (``baseline()``) instead of re-pinning a golden. Every run must also
    be protocol-clean (``assert_protocol_clean``): FakePss records a violation for a bad token, a missing or
    undeclared parameter, a malformed date or an id that names nothing, so worlds without goldens are checked too.

One bug, few failures
    Only the goldens pin every request in order, and only test_every_daily_step_runs_in_run_py_order pins the step
    order. Everything else compares ``traffic_by_step()`` (reads as a multiset, mutations in order) and
    ``assert_every_step_ran`` (the baseline's steps, any order), so a refactor that swaps two reads or moves a
    log-only step fails at most the golden that owns it. Every class but the goldens derives from
    NeedsHealthyBaseline and is skipped, with the reason, while the baseline run itself is broken: test_exits_zero
    and the protocol test report that once.

Regenerating the goldens
    Only when a production change is INTENDED to change the daily requests. From any directory except the repository
    root (tests/__init__.py refuses it while ./.device exists)::

        cd "$(mktemp -d)" && PYTHONPATH=<repo> <repo>/.venv/bin/python -m tests.test_daily_run_offline_e2e \\
            --print-golden [--all]

    It prints, as paste-ready Python, every golden whose value differs from this file (``--all``: every golden): the
    step order, step paths, step mutations, checksums, training paths and calls, second-run training, and the
    economy, ship and crew snapshots. Review it line by line, carry the "why" comments over, and give the reason in
    the commit message.

Known defects
    KNOWN_DEFECTS lists every production defect these tests characterise. Each assertion that pins one carries a
    grep-able ``# PINNED(<id>)`` tag (also in tests/fakes and tests/test_fake_pss_server.py). When a fix lands, grep
    the id, flip each tagged assertion to the correct behaviour, and delete the entry. A self-test keeps the tags and
    the registry in sync. Training tiers are named P-/S-T1..T3 (primary or secondary room, tier 1-3 of the bands in
    sdk/training/strategy.py); they are not defect ids. The training defects D1-D15 were fixed when manageTraining
    became sdk/execution/training.py; each one's regression test names it.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import functools
import io
import itertools
import logging
import os
import pprint
import re
import sys
import tempfile
import time
import types
import unittest
from collections.abc import Mapping
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import patch

import requests

import run
from sdk.client import Client
from tests.fakes.pss_server import HOST_UTC, HOST_UTC_MINUS_5, HOST_UTC_PLUS_9, Exchange, FakePss
from tests.fakes.state import (
    FAKE_EPOCH,
    FAKE_PASSWORD,
    Marker,
    World,
    default_world,
    minimal_world,
    stats,
)
from tests.synthetic_fixtures import SYNTHETIC_EMAIL

HOUR = datetime.timedelta(hours=1)

# id: (what is wrong today, where). "(not pinned)" marks a known defect no assertion characterises yet.
KNOWN_DEFECTS = {
    "DR1": ("collectDailyReward reports success for any non-empty reply, including an errorMessage envelope, so a "
            "rejected collect logs 'You have collected the daily reward' and the run exits 0",
            "sdk/client.py collectDailyReward (~L1940)"),
    "A1": ("the re-auth retry resends the old URL, which still carries the revoked token, so the retried request "
           "fails too", "sdk/client.py Client.request (~L239-245)"),
    "H1": ("heartbeat measures the age of the last heartbeat with timedelta.seconds, which ignores days: a heartbeat "
           "1 day + 30 s old is treated as 30 s old and skipped", "sdk/client.py heartbeat (~L3377)"),
    "TASK1": ("collectTaskReward compares ProgressValue == ObjectiveAmount as strings, so an over-achieved task "
              "(4 of 2) is never collected", "sdk/client.py collectTaskReward (~L3355)"),
    "L1": ("authorize_email_password raises IndexError on a falsy reply, and run.py calls client.login bare, so the "
           "exception escapes main()", "sdk/client.py authorize_email_password (~L486); run.py main (~L1723)"),
    "RS1": ("upgradeResearches starts a research only when its gas cost is strictly below the gas total, so a research "
            "the account can exactly afford is never started", "sdk/client.py upgradeResearches (~L2339)"),
    "DC1": ("Client.dronesCollected is a class-level dict shared by every Client in the process, so a later Client "
            "(the fleet runner makes one per account) skips every drone id an earlier one collected",
            "sdk/client.py Client.dronesCollected (~L202) / collectMiningDronesWithTravel (~L3152)"),
    "TZ1": ("upgradeCharacters compares the server's UTC @AvailableDate with the host's local datetime.now(): a host "
            "behind UTC upgrades an available crew hours late, and a host ahead of UTC sends UpgradeCharacter before the "
            "crew is available (the server refuses it)", "sdk/client.py upgradeCharacters (~L1624-1631)"),
    "TZ2": ("waitForArrival compares the server's UTC arrival date with the host's local datetime.now(): a host behind "
            "UTC never speeds up a short trip and polls until max_wait, and a host ahead of UTC collects the drone "
            "before the ship arrives", "sdk/client.py waitForArrival (~L3049, ~L3081)"),
    "R1": ("(not pinned) upgradeRooms evaluates the cost `continue` per design row with sticky names, so an "
           "unaffordable upgrade can be requested if an affordable row follows it; the default world's row order "
           "avoids it", "sdk/client.py upgradeRooms (~L2618-2640)"),
}

# Client methods run.main() calls on the daily path, in run.py's order (run.py ~L1745-1876).
DAILY_STEPS = (
    "heartbeat", "purchaseScorchedPodIfAffordable", "grabFlyingStarbux", "collectTaskReward", "getCrewInfo",
    "upgradeResearches", "upgradeRooms", "speedUpBoostGauge", "collectDailyReward", "collectAvailableMarkers",
    "collectMiningDronesWithTravel", "listActiveMarketplaceMessages", "getMessages", "infoBux", "manageTraining",
    "analyzeShipLayout", "getResourceTotals", "upgradeCharacters",
)


class Traffic(NamedTuple):
    """What one step sent, as far as a behaviour-preserving refactor must keep it (see ``DailyRun.traffic_by_step``)."""

    reads: tuple[tuple[str, int], ...]  # ("METHOD /path", count), sorted: the order of two reads is not behaviour
    mutations: tuple[tuple[str, str, dict], ...]  # normalized mutating requests, in order: their order is behaviour


@dataclass
class DailyRun:
    code: object
    log: str
    fake: FakePss
    email_calls: int
    getpass_calls: int
    steps: list[tuple[str, int]] = field(default_factory=list)  # (daily step, index of its first exchange)
    first_exchange: int = 0  # runs on a reused fake start later in fake.exchanges
    end_exchange: int | None = None  # one past this run's last exchange (None: the end of fake.exchanges)
    _traffic: Mapping[str, Traffic] | None = field(default=None, init=False, repr=False, compare=False)

    def exchanges(self) -> list[Exchange]:
        """This run's exchanges only, also when later runs reused its fake."""
        return self.fake.exchanges[self.first_exchange:self.end_exchange]

    def step_order(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.steps)

    def by_step(self) -> dict[str, list[Exchange]]:
        """This run's exchanges per step, in first-entry order; "login" is everything before the first step.

        A step entered more than once gets all of its slices, concatenated in order.
        """
        end = len(self.fake.exchanges) if self.end_exchange is None else self.end_exchange
        bounds = [("login", self.first_exchange), *self.steps, ("", end)]
        result: dict[str, list[Exchange]] = {}
        for (name, start), (_, stop) in itertools.pairwise(bounds):
            result.setdefault(name, []).extend(self.fake.exchanges[start:stop])
        return result

    def paths_by_step(self) -> dict[str, tuple[str, ...]]:
        return {name: tuple(f"{e.method} {e.path}" for e in exchanges) for name, exchanges in self.by_step().items()}

    def mutations_by_step(self) -> dict[str, tuple]:
        """Normalized mutating requests per step, for the steps that sent any."""
        found = {name: tuple(self.fake.mutations(exchanges)) for name, exchanges in self.by_step().items()}
        return {name: mutations for name, mutations in found.items() if mutations}

    def traffic_by_step(self) -> Mapping[str, Traffic]:
        """Per step: its reads as a multiset and its mutating requests (with normalized params) in order.

        Everything but the goldens compares this instead of ``paths_by_step()``, so a refactor that swaps two
        independent reads (or moves a read before a guard) fails only the request goldens, which pin every request
        in order on purpose. A finished run (``end_exchange`` set) computes it once and returns the same read-only
        mapping every time.
        """
        if self._traffic is not None:
            return self._traffic
        result = {}
        for name, exchanges in self.by_step().items():
            mutations = tuple(self.fake.mutations(exchanges))
            reads = collections.Counter(f"{e.method} {e.path}" for e in exchanges)
            reads.subtract(f"{method} {path}" for method, path, _ in mutations)
            result[name] = Traffic(tuple(sorted((path, n) for path, n in reads.items() if n)), mutations)
        traffic = types.MappingProxyType(result)
        if self.end_exchange is not None:
            self._traffic = traffic
        return traffic

    def step_of(self, exchange_index: int) -> str:
        return next(name for name, start in reversed([("login", self.first_exchange), *self.steps])
                    if exchange_index >= start)


def _step_recorder(name, method, fake: FakePss, steps: list, depth: list):
    """Wrap one daily-step method: record (name, exchange index) on entry unless another step is running."""
    @functools.wraps(method)
    def recorded(self, *args, **kwargs):
        if depth[0] == 0:
            steps.append((name, len(fake.exchanges)))
        depth[0] += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            depth[0] -= 1
    return recorded


def run_daily(fake: FakePss, *, password: str = FAKE_PASSWORD, email: str = SYNTHETIC_EMAIL,
              write_password_file: bool = True) -> DailyRun:
    """Run ``run.main()`` on the production daily path (minus SMTP and battle) against ``fake``.

    Every patch is undone before it returns, so one FakePss can serve several runs (run, ``fake.advance``, run).
    Exceptions other than SystemExit propagate on purpose: login exceptions escape main (see the L1 test).
    """
    tmp = tempfile.TemporaryDirectory(prefix="tachikoma-e2e-")
    cwd = os.getcwd()
    os.chdir(tmp.name)
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)  # setLevel, not an attribute patch: it clears the logger caches
    steps: list[tuple[str, int]] = []
    first_exchange = len(fake.exchanges)
    try:
        pw = Path(tmp.name, "pw.txt")
        if write_password_file:
            pw.write_text(password + "\n", encoding="utf-8")
        argv = ["run.py", "--login-email", email, "--password-file", str(pw)]
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        with ExitStack() as stack:
            stack.enter_context(fake.install())
            depth = [0]
            for name in DAILY_STEPS:
                recorder = _step_recorder(name, getattr(Client, name), fake, steps, depth)
                stack.enter_context(patch.object(Client, name, recorder))
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            stack.enter_context(patch.object(root, "handlers", [handler]))
            email_mock = stack.enter_context(patch("run.email_logfile",
                                                   side_effect=AssertionError("SMTP must stay off")))
            stack.enter_context(patch("run.send_email", side_effect=AssertionError("SMTP must stay off")))
            prompt = stack.enter_context(patch("getpass.getpass", side_effect=AssertionError("no prompt")))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            try:
                run.main()
                code = "returned-without-exit"
            except SystemExit as exc:
                code = exc.code
        return DailyRun(code, buf.getvalue(), fake, email_mock.call_count, prompt.call_count, steps, first_exchange,
                        len(fake.exchanges))
    finally:
        root.setLevel(old_level)
        os.chdir(cwd)
        tmp.cleanup()


def _run_once(make_run):
    """Cache a shared run per argument tuple, its exception included (``functools.lru_cache`` caches only returns).

    A broken baseline then costs one run.main(), however many tests ask for it, and each caller gets the same
    exception.
    """
    cache: dict[tuple, tuple[bool, Any]] = {}

    @functools.wraps(make_run)
    def shared(*args):
        if args not in cache:
            try:
                cache[args] = (True, make_run(*args))
            except Exception as exc:  # noqa: BLE001 - re-raised below, now and on every later call
                cache[args] = (False, exc)
        ok, value = cache[args]
        if ok:
            return value
        raise value

    shared.cache = cache  # ty: ignore[missing-attribute]
    return shared


@_run_once
def baseline() -> DailyRun:
    """One un-faulted run of the default world, shared read-only by the tests (do not mutate its fake)."""
    return run_daily(FakePss(default_world()))


def baseline_problem() -> str | None:
    """Why the baseline run is unhealthy (it raised, exited non-zero, hit an unknown endpoint or broke the protocol),
    or None."""
    try:
        daily = baseline()
    except Exception as exc:  # noqa: BLE001 - DailyRunGoldenTests.setUpClass reports the traceback itself
        return f"run.main() raised {exc!r}"
    problems = [f"exit code {daily.code!r}"] if daily.code != 0 else []
    problems += [f"unknown endpoints {daily.fake.unknown}"] if daily.fake.unknown else []
    problems += [f"{len(daily.fake.violations)} protocol violation(s), first: {daily.fake.violations[0]}"
                 ] if daily.fake.violations else []
    return "; ".join(problems) or None


class NeedsHealthyBaseline(unittest.TestCase):
    """Base of every test class but the goldens: the tests here compare against ``baseline()`` or rely on the run
    being healthy, so while the baseline itself is broken they are skipped with the reason.

    A bug that breaks every run (a failed login, a missing accessToken, a step that always errors) is then reported
    once, by DailyRunGoldenTests.test_exits_zero and test_protocol_clean_no_unknown_endpoints_and_no_violations,
    which never skip, instead of by every test in the module. A bug the baseline does not show still runs all of these.
    """

    def setUp(self):
        problem = baseline_problem()
        if problem:
            self.skipTest(f"the default-world baseline is broken ({problem}); DailyRunGoldenTests reports it")


def assert_every_step_ran(test: unittest.TestCase, daily: DailyRun, *, skipped: tuple[str, ...] = ()) -> None:
    """Each step the baseline ran, but ``skipped``, ran here too, exactly as often.

    Neither the order nor the list itself is checked here: test_every_daily_step_runs_in_run_py_order owns both, so a
    step dropped or moved in run.py fails that one test, and a failure that stops later steps fails this one.
    """
    expected = sorted(step for step in baseline().step_order() if step not in skipped)
    test.assertEqual(sorted(daily.step_order()), expected, "a step the baseline runs did not run here (or ran twice)")


def steps_after(step: str) -> tuple[str, ...]:
    """The steps the baseline ran after ``step``, in its observed order (a reordering moves both sides)."""
    order = baseline().step_order()
    return order[order.index(step) + 1:]


def training_slice(fake: FakePss, start: int = 0) -> list[tuple]:
    """(endpoint, characterId, trainingDesignId, error) for every Finish/AddTraining request from ``start``, in order."""
    return [(e.path.rsplit("/", 1)[1], e.params.get("characterId"), e.params.get("trainingDesignId"), e.error)
            for e in fake.exchanges[start:] if e.path in ("/TrainingService/FinishTraining", "/TrainingService/AddTraining")]


# The log contract: the emailed report's per-record success lines, as assert_reports_match_server reads them. A wording
# change in production is a one-line edit here.
START_LINE = re.compile(r"\] Starting training (?P<course>.+?) for (?P<crew>.+?) in (?P<room>.*?) with ")
DRONE_LINE = re.compile(r"\] Collected mining drone (?P<marker>\S+)\n")
# Every CollectMarker2 success line: drones (collectMiningDronesWithTravel) and user markers (collectAvailableMarkers).
COLLECT_LINE = re.compile(r"\] Collected (?:mining drone|marker) (?P<marker>\S+)\n")


def applied(daily: DailyRun, path: str, *keys: str) -> list[tuple]:
    """``keys`` of every request this run sent to ``path`` that the server applied (no error, fault or violation)."""
    return [tuple(e.params.get(key) for key in keys) for e in daily.exchanges()
            if e.path == path and e.error is None and e.status == 200]


def reported_training_starts(daily: DailyRun) -> list[tuple]:
    """(characterId, trainingDesignId) for every "Starting training" line of this run's log.

    A name the roster holds twice (two copies of a character, like 1005 and 1016) is told apart by the room the line
    names; an unassigned crew's line names an inherited room (D6), but its name is unique.
    """
    state = daily.fake.state
    room_names = {room.id: state.room_design(room.design_id).name for room in state.rooms}
    course_ids = {d.name: d.id for d in state.training_designs}

    def crew_id(name: str, room: str) -> str:
        crews = [c for c in state.characters if c.name == name]
        if len(crews) > 1:
            crews = [c for c in crews if room_names.get(c.room_id) == room]
        if len(crews) != 1:
            raise AssertionError(f"log line names crew {name!r} in {room!r}: {len(crews)} roster matches")
        return crews[0].id

    return [(crew_id(m["crew"], m["room"]), course_ids[m["course"]]) for m in START_LINE.finditer(daily.log)]


def assert_reports_match_server(test: unittest.TestCase, daily: DailyRun) -> None:
    """The log's per-record success lines name exactly what the server applied: every "Starting training" is an
    AddTraining the fake accepted (and vice versa), and every "Collected mining drone" or "Collected marker" a
    CollectMarker2 it accepted, in request order."""
    test.assertEqual(reported_training_starts(daily),
                     applied(daily, "/TrainingService/AddTraining", "characterId", "trainingDesignId"),
                     "'Starting training' log lines vs the AddTraining requests the server applied")
    test.assertEqual([(m["marker"],) for m in COLLECT_LINE.finditer(daily.log)],
                     applied(daily, "/GalaxyService/CollectMarker2", "starSystemMarkerId"),
                     "'Collected mining drone'/'Collected marker' log lines vs the CollectMarker2 requests applied")


def economy_snapshot(state) -> dict:
    return {
        "credits": state.account.credits,
        "daily_reward_status": state.account.daily_reward_status,
        "tasks_collected": sorted(t.design_id for t in state.tasks if t.collected),
        "messages": {m.id: (m.collected, m.actioned) for m in state.messages},
        "markers_collected": sorted(m.id for m in state.markers if m.collected),
    }


def ship_snapshot(state) -> dict:
    return {
        "rooms": {r.id: (r.design_id, r.status, r.pending_design_id) for r in state.rooms},
        "researches": [(r.design_id, r.state) for r in state.researches],
        "mineral": state.resources.mineral,
        "gas": state.resources.gas,
    }


def crew_snapshot(state) -> dict:
    return {c.id: (c.level, c.stats["HpImprovement"], c.fatigue) for c in state.characters}


def checksums(fake: FakePss) -> list[tuple[str, str]]:
    return [(path, params.get("checksum", params.get("Checksum"))) for _, path, params in fake.requests
            if "checksum" in params or "Checksum" in params]


# --- goldens for the default world (regenerate with --print-golden; see the module docstring) --------------------

EXPECTED_STEP_ORDER = DAILY_STEPS  # every step runs: free starbux are at the cap, so collectTaskReward runs too
EXPECTED_STEP_PATHS = {  # manageTraining is owned by EXPECTED_TRAINING_PATHS
    "login": ("POST /UserService/DeviceLogin17",  # stage 1: guest session
              "POST /UserService/UserEmailPasswordAuthorize4",  # stage 2: refresh token
              "POST /UserService/DeviceLogin17",  # stage 3: account session
              "GET /SettingService/GetLatestVersion3",
              "GET /LiveOpsService/GetTodayLiveOps2"),
    "heartbeat": ("POST /UserService/HeartBeat4",),  # last heartbeat 5 min ago
    "purchaseScorchedPodIfAffordable": ("POST /ShopService/PurchaseCatalog2",),  # 1200 >= 1000 starbux
    "grabFlyingStarbux": (),  # free starbux 10 = cap
    "collectTaskReward": ("GET /TaskService/ListTasksOfAUser",
                          "GET /TaskService/ListAllTaskDesigns2",
                          "POST /TaskService/CollectTaskCompletion"),  # task 7001 (7004 never: TASK1)
    "getCrewInfo": ("GET /CharacterService/ListAllCharactersOfUser",),
    "upgradeResearches": ("GET /ResearchService/ListAllResearches",
                          "GET /ResearchService/ListAllResearchDesigns2",
                          "POST /RoomService/CollectAllResources",
                          # design 10 only: design 2 costs 50000 > 40000 gas, and design 20 (3000) is affordable but
                          # upgradeResearches stops after its first start
                          "POST /ResearchService/AddResearch"),
    "upgradeRooms": ("GET /RoomService/ListRoomDesigns2",
                     "GET /ShipService/GetShipByUserId",  # pass 1
                     "POST /RoomService/UpgradeRoom2",  # room 104 -> design 51
                     "POST /RoomService/CollectAllResources",
                     "GET /ShipService/GetShipByUserId"),  # pass 2 requests nothing -> break
    # One boost per run: the research upgradeResearches started wins over the upgrading rooms and the ship upgrade.
    "speedUpBoostGauge": ("POST /ResearchService/SpeedUpResearchUsingBoostGauge",),
    "collectDailyReward": ("POST /UserService/CollectDailyReward2",),
    "collectAvailableMarkers": ("GET /GalaxyService/ListStarSystemMarkersAndUserMarkers",
                                "POST /GalaxyService/CollectMarker2"),  # user marker 9004 (M1 fixed in #276)
    "collectMiningDronesWithTravel": ("GET /GalaxyService/ListStarSystemMarkersAndUserMarkers",
                                      "GET /ShipService/GetShipByUserId",  # current star system
                                      "POST /GalaxyService/CollectMarker2"),  # drone 9001, same system: no GoTo
    "listActiveMarketplaceMessages": ("GET /MessageService/ListActiveMarketplaceMessages5",),
    "getMessages": ("GET /MessageService/ListSystemMessagesForUser3",
                    "POST /MessageService/CollectReward2",  # 8001 starbux:5 (8002 gas: nothing)
                    "GET /MessageService/ActionMessage"),  # 8003 "None"
    "infoBux": (),  # log only; see test_steps_without_requests_still_run_and_log
    "analyzeShipLayout": (),  # ship and room designs already loaded
    "getResourceTotals": (),
    "upgradeCharacters": ("GET /ItemService/ListItemsOfAShip",
                          "POST /RoomService/CollectAllResources",  # 1009
                          "POST /CharacterService/UpgradeCharacter",  # 1009
                          "POST /RoomService/CollectAllResources",  # 1010
                          "POST /CharacterService/UpgradeCharacter",  # 1010
                          "POST /RoomService/CollectAllResources",  # 1012: AvailableDate in the future, no upgrade
                          # 1013 (89 of 90 xp): nothing
                          "POST /RoomService/CollectAllResources",  # 1014: AvailableDate 2 h ago
                          "POST /CharacterService/UpgradeCharacter"),  # 1014
}
N, T, C = "2026-11-20T12:00:00", "<token>", "<checksum>"  # frozen clock, current account token, any checksum
COLLECT = ("POST", "/RoomService/CollectAllResources", {"itemType": "None", "collectDate": N, "accessToken": T})
EXPECTED_STEP_MUTATIONS = {  # manageTraining is owned by EXPECTED_TRAINING
    "heartbeat": (("POST", "/UserService/HeartBeat4", {"clientDateTime": N, "checksum": C, "accessToken": T}),),
    "purchaseScorchedPodIfAffordable": (
        ("POST", "/ShopService/PurchaseCatalog2", {"argument": "1291", "clientDateTime": N, "checksum": C,
                                                   "accessToken": T}),),
    "collectTaskReward": (("POST", "/TaskService/CollectTaskCompletion", {"taskDesignId": "7001", "accessToken": T}),),
    "upgradeResearches": (
        COLLECT,
        ("POST", "/ResearchService/AddResearch", {"researchDesignId": "10", "researchStartDate": N, "accessToken": T})),
    "upgradeRooms": (
        ("POST", "/RoomService/UpgradeRoom2", {"roomId": "104", "upgradeRoomDesignId": "51", "accessToken": T}),
        COLLECT),
    "speedUpBoostGauge": (
        # @ResearchId of the research AddResearch started, never its design id "10"
        ("POST", "/ResearchService/SpeedUpResearchUsingBoostGauge", {"researchId": "700004", "accessToken": T,
                                                                     "clientDateTime": N}),),
    "collectDailyReward": (
        ("POST", "/UserService/CollectDailyReward2", {"dailyRewardStatus": "Box", "argument": "3", "accessToken": T}),),
    "collectAvailableMarkers": (
        ("POST", "/GalaxyService/CollectMarker2", {"starSystemMarkerId": "9004", "clientDateTime": N, "checksum": C,
                                                   "accessToken": T}),),
    "collectMiningDronesWithTravel": (
        ("POST", "/GalaxyService/CollectMarker2", {"starSystemMarkerId": "9001", "clientDateTime": N, "checksum": C,
                                                   "accessToken": T}),),
    "getMessages": (
        ("POST", "/MessageService/CollectReward2", {"messageId": "8001", "clientDateTime": N, "checksum": C,
                                                    "accessToken": T}),
        ("GET", "/MessageService/ActionMessage", {"messageId": "8003", "accessToken": T})),
    "upgradeCharacters": (
        COLLECT,
        ("POST", "/CharacterService/UpgradeCharacter", {"characterId": "1009", "accessToken": T}),
        COLLECT,
        ("POST", "/CharacterService/UpgradeCharacter", {"characterId": "1010", "accessToken": T}),
        COLLECT,
        COLLECT,
        ("POST", "/CharacterService/UpgradeCharacter", {"characterId": "1014", "accessToken": T})),
}
# Literal checksums of the default run, in request order (FAKE_EPOCH clock, seeded device key, deterministic tokens).
# mutations() normalizes them and the fake does not verify them, so a broken checksum formula is caught only here.
EXPECTED_CHECKSUMS = [
    ("/UserService/DeviceLogin17", "854c945565736ec6869ea7361e4cc5ac"),
    ("/UserService/UserEmailPasswordAuthorize4", "f3b25abb0f7168bb894d5f6ebe03a986"),
    ("/UserService/DeviceLogin17", "854c945565736ec6869ea7361e4cc5ac"),
    ("/UserService/HeartBeat4", "144"),
    ("/ShopService/PurchaseCatalog2", "e50ffedf21403100334f3077d51e7a35"),
    ("/GalaxyService/CollectMarker2", "a5facf18fc820b8bc296f9dd0fc9b3f3"),  # user marker 9004
    ("/GalaxyService/CollectMarker2", "14c5560d328fe2a56702b41eb4addd2e"),  # drone 9001
    ("/MessageService/CollectReward2", "144"),
]
# State after the default run. Trace: credits 1200 - 1000 pod + 10 task 7001 + 5 message 8001 = 215; mineral 60000
# - 20000 room 104 + 1000 daily reward + 500 user marker 9004 + 500 drone 9001 = 42000; gas 40000 - 2000 research 10 = 38000.
EXPECTED_ECONOMY = {
    "credits": 215,
    "daily_reward_status": "1",
    "tasks_collected": ["7001", "7003"],  # 7003 already collected; 7004 (4 of 2) never is: PINNED(TASK1)
    "messages": {"8001": (True, False), "8002": (False, False), "8003": (False, True)},  # (collected, actioned)
    "markers_collected": ["9001", "9002", "9004"],  # 9002 already collected; 9004 is a user marker (M1 fixed)
}
EXPECTED_SHIP = {
    "rooms": {"101": ("1", "Normal", None), "102": ("30", "Normal", None), "103": ("31", "Normal", None),
              "104": ("50", "Upgrading", "51"), "105": ("60", "Upgrading", "61"), "106": ("40", "Normal", None),
              "107": ("41", "Normal", None)},
    "researches": [("1", "Completed"), ("2", "Completed"), ("3", "Completed"), ("4", "Completed"),
                   ("10", "Researching")],
    "mineral": 42000,
    "gas": 38000,
}
EXPECTED_CREW = {  # id: (level, HpImprovement, fatigue)
    "1001": (40, 18, 10),  # pending Bench Press finished: Hp +2, fatigue +10
    "1002": (40, 20, 0),
    "1003": (40, 22, 10),  # Bench Press ended 30 min ago, finished (D2: no extra wait after the end)
    "1004": (40, 10, 0), "1005": (40, 30, 0), "1006": (40, 34, 0),
    "1007": (40, 40, 0), "1008": (40, 40, 0),
    "1009": (2, 10, 0),  # level 1 -> 2 (production's cost: 90 xp, 0 gas)
    "1010": (6, 10, 0),  # level 5 -> 6 (production's cost: 2250 xp, 130 gas; the fake charges no gas)
    "1011": (1, 20, 0),  # Legendary: needs 270 xp, has 120
    "1012": (1, 20, 0),  # AvailableDate 3 h in the future
    "1013": (1, 10, 0),  # 89 xp: one short of production's 90-xp cost for level 1
    "1014": (4, 12, 0),  # level 3 -> 4 (810 xp, 33 gas); AvailableDate 2 h ago
    "1015": (40, 30, 0),  # exactly 71 %: no training
    "1016": (40, 22, 10),  # pending Bench Press finished: Hp +2, fatigue +10; then S-T2 starts (not yet applied)
    "1017": (1, 10, 0),  # unassigned: never upgraded, whatever its xp
}

# --- training goldens (the only ones a manageTraining change should touch) ---------------------------------------

EXPECTED_TRAINING_PATHS = (
    # One read of each payload the plan needs (D11: never refetched per crew member), in this order:
    "GET /CharacterService/ListAllCharactersOfUser",
    "GET /ShipService/GetShipByUserId",  # rooms and researches
    "GET /CharacterService/ListAllCharacterDesigns2",
    "GET /TrainingService/ListAllTrainingDesigns2",
    "GET /RoomService/ListRoomDesigns2",  # read here, not left over from upgradeRooms (D15)
    "GET /ResearchService/ListAllResearchDesigns2",
    "GET /CharacterService/ListAllCharactersOfUser",  # the runner's before-snapshot of the first action
    "POST /TrainingService/FinishTraining",  # 1001
    "GET /CharacterService/ListAllCharactersOfUser",  # after-snapshot, reused as the next before-snapshot
    "POST /TrainingService/AddTraining",  # 1001, replanned from the finish reply
    "GET /CharacterService/ListAllCharactersOfUser",
    "POST /TrainingService/FinishTraining",  # 1003
    "GET /CharacterService/ListAllCharactersOfUser",
    "POST /TrainingService/AddTraining",  # 1003
    "GET /CharacterService/ListAllCharactersOfUser",
    "POST /TrainingService/AddTraining",  # 1004
    "GET /CharacterService/ListAllCharactersOfUser",
    "POST /TrainingService/AddTraining",  # 1005
    "GET /CharacterService/ListAllCharactersOfUser",
    "POST /TrainingService/AddTraining",  # 1008
    "GET /CharacterService/ListAllCharactersOfUser",
    "POST /TrainingService/FinishTraining",  # 1016
    "GET /CharacterService/ListAllCharactersOfUser",
    "POST /TrainingService/AddTraining",  # 1016
    "GET /CharacterService/ListAllCharactersOfUser",
)
# manageTraining against the default roster: (endpoint, characterId, trainingDesignId, fake's error). Each ended
# Bench Press is finished (D14), and no FinishTraining precedes an AddTraining of an idle crew member (D3). The world
# has completed Fitness 101-201 (live course names), so the 101 and 201 research gates are open: every research-gated
# crew member starts its tier. After each finish the crew is replanned from the reply: 1001 (59 / 80 = 74 %), 1003
# (111 / 150 = 74 %) and 1016 (88 / 120 = 74 %) reach S-T2. Regression: with the old parser no gate ever opened on
# live names, and these two runs started nothing.
EXPECTED_TRAINING = [
    ("FinishTraining", "1001", None, None),  # ended 2 h ago
    ("AddTraining", "1001", "4", None),  # S-T2 Muscle Beach (Galaxy Gym), course 201 gate
    ("FinishTraining", "1003", None, None),  # ended 30 min ago: finished now (D2)
    ("AddTraining", "1003", "4", None),  # S-T2 Muscle Beach (GYM), course 201 gate
    ("AddTraining", "1004", "21", None),  # P-T1 Big Book of Science (30 %), course 101 gate
    ("AddTraining", "1005", "4", None),  # P-T2 Muscle Beach (60 %), course 201 gate
    ("AddTraining", "1008", "42", None),  # S-T2 BBJ (80 %), course 201 gate
    ("FinishTraining", "1016", None, None),  # ended 2 h ago
    ("AddTraining", "1016", "32", None),  # S-T2 Engineering Summit (Lunar College), course 201 gate
]
# Second run of the default world, 2 h 1 min later: only 1004's one-hour T1 session has ended; it is finished and,
# still in P-T1 (32 %), started again. The three-hour T2 sessions are still in progress.
EXPECTED_SECOND_RUN_TRAINING: list[tuple] = [
    ("FinishTraining", "1004", None, None),
    ("AddTraining", "1004", "21", None),
]


def assert_protocol_clean(test: unittest.TestCase, fake: FakePss, expected_violations: int = 0) -> None:
    """No unknown endpoint and exactly ``expected_violations`` protocol violations (token, params, ids, clock)."""
    test.assertEqual(fake.unknown, [])
    test.assertEqual(len(fake.violations), expected_violations, fake.violations)


class DailyRunGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daily = baseline()
        cls.fake = cls.daily.fake

    def test_exits_zero(self):
        self.assertEqual(self.daily.code, 0, self.daily.log[-3000:])

    def test_every_daily_step_runs_in_run_py_order(self):
        self.assertEqual(self.daily.step_order(), EXPECTED_STEP_ORDER)

    def test_requests_of_each_step_match_golden(self):
        actual = self.daily.paths_by_step()
        actual.pop("manageTraining")
        self.assertEqual(sorted(actual), sorted(EXPECTED_STEP_PATHS), "steps differ")  # the order test owns order
        for step, expected in EXPECTED_STEP_PATHS.items():
            with self.subTest(step=step):
                self.assertEqual(actual[step], expected)

    def test_mutating_requests_of_each_step_match_golden(self):
        actual = self.daily.mutations_by_step()
        actual.pop("manageTraining", None)
        for step in dict.fromkeys([*EXPECTED_STEP_MUTATIONS, *actual]):
            with self.subTest(step=step):
                self.assertEqual(actual.get(step, ()), EXPECTED_STEP_MUTATIONS.get(step, ()))

    def test_training_step_matches_training_goldens(self):
        """What manageTraining sends for the default roster, and why every other crew member sends nothing."""
        self.assertEqual(self.daily.paths_by_step()["manageTraining"], EXPECTED_TRAINING_PATHS)
        self.assertEqual(training_slice(self.fake), EXPECTED_TRAINING)
        trained = {cid for _, cid, _, _ in training_slice(self.fake)}
        self.assertEqual(trained, {"1001", "1003", "1004", "1005", "1008", "1016"})
        # Every silent crew member logs why (D6: no room is inherited):
        prefix = "INFO [SyntheticCaptain] training: "
        for line in (
            "The Conjoint Archon (1006) skip tp_cap_reached: 135 TP >= cap 80 TP (68% of capacity 200)",
            "Cyber Duck (1007) skip not_in_training_room: move to gym",  # wrong room for its role: no request
            "Rookie Pilot (1010) skip unknown_role: 'Rookie Pilot' has no training role",
            "Ancestral Spirit (1015) skip tp_cap_reached: 106 TP >= cap 70 TP (71% of capacity 150)",
            "Galactic Snow Maiden (1002) skip not_in_training_room: room None is not a training room",  # D6
        ):
            with self.subTest(line=line):
                self.assertIn(f"{prefix}{line}\n", self.daily.log)
        # Regression: on live research names every research-gated tier was skipped; Fitness 201 now opens them all.
        self.assertNotIn("research_missing", self.daily.log)
        self.assertIn(f"{prefix}started=6 finished=3 consumables=0 skipped=11 failed=0\n", self.daily.log)
        # Consumables need item designs, which the daily path never loads (known limitation); with them, D9 and D12
        # are proven fixed in tests/test_training_endpoints.py.
        self.assertNotIn("POST /ItemService/ActivateItem3", self.fake.paths())
        self.assertNotIn("manageTraining failed", self.daily.log)

    def test_log_reports_only_what_the_server_applied(self):
        # Each applied start and finish is reported, a finish with its real stat change (D4).
        self.assertEqual(reported_training_starts(self.daily), [("1001", "4"), ("1003", "4"), ("1004", "21"),
                                                                ("1005", "4"), ("1008", "42"), ("1016", "32")])
        self.assertIn("] Starting training Big Book of Science for Mistycball in Academy with 30% training complete\n",
                      self.daily.log)
        for name, room in (("Galactic Succubus", "Galaxy Gym"), ("Delish", "GYM"), ("Galactic Sprite", "Lunar College")):
            with self.subTest(name=name):
                self.assertIn(f"] Completed training for {name} in {room} with Hp +2\n", self.daily.log)
        self.assertIn("INFO [SyntheticCaptain] Collected mining drone 9001\n", self.daily.log)
        assert_reports_match_server(self, self.daily)

    def test_steps_without_requests_still_run_and_log(self):
        # infoBux, analyzeShipLayout and getResourceTotals send nothing (their data is already loaded), so only the
        # step recorder and their log lines show that they ran. The totals are the client's view after its last
        # CollectAllResources (in upgradeRooms), before the daily reward and drone were granted.
        for line in ("[SyntheticCaptain] A total of 10 free starbux was collected today.",
                     "[SyntheticCaptain] You have a total of 210 starbux.",
                     "[SyntheticShip] === Ship Layout Analysis: SyntheticShip ===",
                     "[SyntheticShip] Ship Level: 8 | Design: 108",
                     "[SyntheticCaptain] There is a total of 40000 minerals on your ship.",
                     "[SyntheticCaptain] There is a total of 38000 gas on your ship.",
                     "[SyntheticCaptain] Finished..."):
            with self.subTest(line=line):
                self.assertIn(f"INFO {line}\n", self.daily.log)
        for step in ("infoBux", "analyzeShipLayout", "getResourceTotals"):
            self.assertNotIn(f"{step} failed", self.daily.log)

    def test_economy_state_after_run(self):
        self.assertEqual(economy_snapshot(self.fake.state), EXPECTED_ECONOMY)

    def test_ship_state_after_run(self):
        self.assertEqual(ship_snapshot(self.fake.state), EXPECTED_SHIP)

    def test_crew_levels_after_run(self):
        self.assertEqual(crew_snapshot(self.fake.state), EXPECTED_CREW)

    def test_protocol_clean_no_unknown_endpoints_and_no_violations(self):
        assert_protocol_clean(self, self.fake)

    def test_pending_training_finished_with_stat_gain_then_next_tier_started(self):
        # D14: the ended Bench Press is finished (Hp +2); the replan at 74 % starts S-T2 Muscle Beach (3 h), whose
        # course-201 gate the completed Fitness 201 opens.
        crew = self.fake.state.character("1001")
        self.assertEqual((crew.stats["HpImprovement"], crew.fatigue), (18, 10))
        self.assertEqual((crew.last_session.design_id, crew.last_session.applied), ("4", False))
        self.assertEqual(crew.training_end, FAKE_EPOCH + 3 * HOUR)

    def test_no_real_sleep_no_smtp_no_prompt(self):
        self.assertEqual(self.fake.clock.sleeps, [])
        self.assertEqual(self.fake.now, FAKE_EPOCH)
        self.assertEqual((self.daily.email_calls, self.daily.getpass_calls), (0, 0))
        self.assertIn("Email log delivery is disabled.", self.daily.log)

    def test_password_never_appears_in_log_or_fake_log(self):
        self.assertNotIn(FAKE_PASSWORD, self.daily.log)
        self.assertNotIn(FAKE_PASSWORD, repr(self.fake.requests))
        authorize = next(params for _, path, params in self.fake.requests if path.endswith("Authorize4"))
        self.assertEqual(authorize["password"], "<redacted>")

    def test_design_reads_use_server_design_versions(self):
        versions = {path: params.get("designVersion") for _, path, params in self.fake.requests
                    if path.endswith("Designs2")}
        self.assertEqual(versions, {
            "/RoomService/ListRoomDesigns2": "11", "/ResearchService/ListAllResearchDesigns2": "12",
            "/TaskService/ListAllTaskDesigns2": "13", "/TrainingService/ListAllTrainingDesigns2": "14",
            "/CharacterService/ListAllCharacterDesigns2": "15"})

    def test_checksums_match_golden(self):
        self.assertEqual(checksums(self.fake), EXPECTED_CHECKSUMS)

    def test_run_is_deterministic(self):
        again = run_daily(FakePss(default_world()))
        self.assertEqual(again.code, self.daily.code)
        self.assertEqual(again.steps, self.daily.steps)
        self.assertEqual(again.fake.requests, self.fake.requests)  # raw params: tokens, checksums, device key
        self.assertEqual(dataclasses.asdict(again.fake.state), dataclasses.asdict(self.fake.state))


# One fatal step failure per row (ORIGINAL_REQUEST.md 781-807: one unexpected failure exits 1, and the remaining
# independent actions still run first). (failing step, path, fault kwargs, why production treats it as a failure).
# A malformed body must not contain "errorMessage": Client.request would parse it and raise inside request(). A bare
# status must be a 4xx: production's urllib3 retry re-sends a 5xx (and a network failure), so a one-off one never
# reaches it (see test_one_off_server_error_is_retried_transparently); a persistent one needs times=11.
FATAL_STEP_FAULTS: tuple[tuple[str, str, dict[str, Any], str], ...] = (
    ("purchaseScorchedPodIfAffordable", "/ShopService/PurchaseCatalog2", {"body": "not xml"},
     "parse raises in purchaseCatalogItem"),
    ("getCrewInfo", "/CharacterService/ListAllCharactersOfUser", {"status": 404}, "empty body fails to parse"),
    ("upgradeResearches", "/ResearchService/AddResearch", {"status": 404}, "falsy reply returns False"),
    ("upgradeRooms", "/ShipService/GetShipByUserId", {"body": "<<not xml"}, "parse raises inside upgradeRooms"),
    ("collectDailyReward", "/UserService/CollectDailyReward2", {"status": 503, "times": 11},
     "retries exhausted: RetryError"),
    ("listActiveMarketplaceMessages", "/MessageService/ListActiveMarketplaceMessages5",
     {"error": "Marketplace unavailable"}, "errorMessage= returns False"),
    ("listActiveMarketplaceMessages", "/MessageService/ListActiveMarketplaceMessages5",
     {"raises": requests.ConnectionError, "times": 11}, "network failure after retries raises ConnectionError"),
    ("getMessages", "/MessageService/ListSystemMessagesForUser3", {"status": 404}, "attribute never set"),
    ("manageTraining", "/TrainingService/ListAllTrainingDesigns2", {"error": "Design service down"},
     "errorMessage in the training designs returns False"),
    ("upgradeCharacters", "/ItemService/ListItemsOfAShip", {"body": "not xml"},
     "parse raises; upgradeCharacters catches it and returns False"),
    ("upgradeCharacters", "/CharacterService/UpgradeCharacter", {"raises": requests.ConnectionError, "times": 11},
     "network failure on the first crew upgrade, mid-roster: caught, returns False"),
)
# Later steps whose requests legitimately differ from the baseline after a fault (state or cache the fault changed).
DIFFERS_AFTER_FAULT = {
    "getCrewInfo": {"manageTraining"},  # refetches the characters getCrewInfo failed to load
    "upgradeResearches": {"speedUpBoostGauge"},  # no research started, so the boost goes to a room
    "upgradeRooms": {"speedUpBoostGauge"},  # the unparsed ship is read again before boosting
}


class DailyRunFailureContinuationTests(NeedsHealthyBaseline):
    def test_each_fatal_step_failure_exits_1_and_all_later_steps_still_run_unchanged(self):
        expected = baseline().traffic_by_step()
        for step, path, fault_kwargs, why in FATAL_STEP_FAULTS:
            with self.subTest(step=step, why=why):
                fake = FakePss(default_world())
                fault = fake.fail(path, **fault_kwargs)
                daily = run_daily(fake)
                self.assertEqual(daily.code, 1, daily.log[-2000:])
                self.assertEqual(fault.hits, fault.times, "the fault was not used up, so this row proves nothing")
                failed_at = next(i for i, e in enumerate(fake.exchanges) if e.error == "fault")
                self.assertEqual(daily.step_of(failed_at), step, "the fault hit another step")
                assert_every_step_ran(self, daily)
                actual = daily.traffic_by_step()
                for later_step in steps_after(step):
                    if later_step not in DIFFERS_AFTER_FAULT.get(step, set()):
                        self.assertEqual(actual[later_step], expected[later_step], later_step)
                self.assertTrue(daily.log.endswith("INFO [SyntheticCaptain] Finished...\n"), daily.log[-500:])
                assert_protocol_clean(self, fake)

    def test_failed_boosts_move_on_to_the_next_candidate_without_failing_the_run(self):
        research = ("POST", "/ResearchService/SpeedUpResearchUsingBoostGauge",
                    {"researchId": "700004", "clientDateTime": N, "accessToken": T})
        room_104 = ("POST", "/RoomService/SpeedUpRoomConstructionUsingBoostGauge",
                    {"roomId": "104", "clientDateTime": N, "accessToken": T})
        room_105 = ("POST", "/RoomService/SpeedUpRoomConstructionUsingBoostGauge",
                    {"roomId": "105", "clientDateTime": N, "accessToken": T})
        ship = ("POST", "/ShipService/SpeedUpShipConstructionUsingBoostGauge",
                {"shipId": "800000501", "clientDateTime": N, "accessToken": T})
        rows = (
            ("an errorMessage moves on to the first upgrading room",
             (("/ResearchService/SpeedUpResearchUsingBoostGauge", {"error": "Boost gauge is not full"}),),
             (research, room_104)),
            ("a network failure moves on as well (urllib3 sends the research boost 11 times first)",
             (("/ResearchService/SpeedUpResearchUsingBoostGauge", {"raises": requests.ConnectionError, "times": 11}),),
             (research,) * 11 + (room_104,)),
            ("every call failing (an empty gauge) tries each candidate once, in order",
             (("/ResearchService/SpeedUpResearchUsingBoostGauge", {"error": "Boost gauge is not full"}),
              ("/RoomService/SpeedUpRoomConstructionUsingBoostGauge", {"error": "Boost gauge is not full", "times": 2}),
              ("/ShipService/SpeedUpShipConstructionUsingBoostGauge", {"error": "Boost gauge is not full"})),
             (research, room_104, room_105, ship)),
        )
        expected = baseline().traffic_by_step()
        for why, faults, boosts in rows:
            with self.subTest(why=why):
                fake = FakePss(default_world())
                for path, kwargs in faults:
                    fake.fail(path, **kwargs)
                daily = run_daily(fake)
                self.assertEqual(daily.code, 0, daily.log[-2000:])
                self.assertEqual(daily.mutations_by_step()["speedUpBoostGauge"], boosts)
                assert_every_step_ran(self, daily)
                actual = daily.traffic_by_step()
                for later_step in steps_after("speedUpBoostGauge"):
                    self.assertEqual(actual[later_step], expected[later_step], later_step)
                assert_protocol_clean(self, fake)

    def test_manage_training_exception_exits_1_and_later_steps_still_run(self):
        # Keeps run.py's manageTraining `except` branch covered: a read that raises (an unparseable
        # ListAllCharacterDesigns2 reply) propagates out of manageTraining into run.py; only actions are per crew.
        fake = FakePss(default_world())
        fault = fake.fail("/CharacterService/ListAllCharacterDesigns2", body="not xml")
        daily = run_daily(fake)
        expected = baseline()
        self.assertEqual((daily.code, fault.hits), (1, 1), daily.log[-2000:])
        failed_at = next(i for i, e in enumerate(fake.exchanges) if e.error == "fault")
        self.assertEqual(daily.step_of(failed_at), "manageTraining")
        self.assertRegex(daily.log, r"\nERROR manageTraining failed: \S")
        self.assertEqual(training_slice(fake), [])
        assert_every_step_ran(self, daily)
        traffic, expected_traffic = daily.traffic_by_step(), expected.traffic_by_step()
        for step in ("analyzeShipLayout", "getResourceTotals"):
            self.assertEqual(traffic[step], expected_traffic[step], step)
        # The designs were never stored, so upgradeCharacters reads them once more, then upgrades the same crew in the
        # same order (where in the step it reads them is not behaviour).
        reads = collections.Counter(dict(expected_traffic["upgradeCharacters"].reads))
        reads["GET /CharacterService/ListAllCharacterDesigns2"] += 1
        self.assertEqual(traffic["upgradeCharacters"],
                         Traffic(tuple(sorted(reads.items())), expected_traffic["upgradeCharacters"].mutations))
        self.assertTrue(daily.log.endswith("INFO [SyntheticCaptain] Finished...\n"), daily.log[-500:])
        assert_protocol_clean(self, fake)

    def test_get_crew_info_failure_is_refetched_by_manage_training(self):
        fake = FakePss(default_world())
        fault = fake.fail("/CharacterService/ListAllCharactersOfUser", status=404)
        daily = run_daily(fake)
        self.assertEqual(fault.hits, 1)
        self.assertEqual(daily.paths_by_step()["manageTraining"][0], "GET /CharacterService/ListAllCharactersOfUser")
        self.assertEqual(training_slice(fake), training_slice(baseline().fake))

    def test_one_off_server_error_and_network_failure_are_retried_transparently(self):
        # Production's HTTPAdapter retries 5xx and connection errors, so a single one is re-sent and the run carries
        # on as if nothing failed.
        faults: tuple[dict[str, Any], ...] = ({"status": 500}, {"raises": requests.ConnectionError})
        for kwargs in faults:
            with self.subTest(fault=kwargs):
                fake = FakePss(default_world())
                fault = fake.fail("/CharacterService/ListAllCharactersOfUser", **kwargs)
                daily = run_daily(fake)
                self.assertEqual((daily.code, fault.hits), (0, 1), daily.log[-2000:])
                self.assertEqual(daily.paths_by_step()["getCrewInfo"], ("GET /CharacterService/ListAllCharactersOfUser",) * 2)
                self.assertEqual(training_slice(fake), training_slice(baseline().fake))
                assert_protocol_clean(self, fake)

    def test_manage_training_failure_sends_no_training_but_upgrades_crew(self):
        fake = FakePss(default_world())
        fake.fail("/TrainingService/ListAllTrainingDesigns2", error="Design service down")
        daily = run_daily(fake)
        self.assertEqual(training_slice(fake), [])
        self.assertIn("ERROR [SyntheticCaptain] training: cannot read training designs; no training this run\n",
                      daily.log)
        self.assertEqual(daily.traffic_by_step()["upgradeCharacters"], baseline().traffic_by_step()["upgradeCharacters"])

    def test_room_designs_failure_in_upgrade_rooms_does_not_stop_training(self):
        fake = FakePss(default_world())
        fault = fake.fail("/RoomService/ListRoomDesigns2", status=404)
        daily = run_daily(fake)
        self.assertEqual((daily.code, fault.hits), (1, 1))  # upgradeRooms reports the failure
        self.assertIn("Room design data unavailable; skipping room upgrades.", daily.log)
        # D15: manageTraining reads the room designs itself, so the crew are trained as in the baseline.
        self.assertEqual(training_slice(fake), training_slice(baseline().fake))
        self.assertNotIn("manageTraining failed", daily.log)
        self.assertNotIn("GET /DesignService/ListAllDesigns4", fake.paths())
        self.assertEqual(daily.paths_by_step()["analyzeShipLayout"], ())  # reuses the room designs training read
        assert_protocol_clean(self, fake)

    def test_unreadable_room_designs_fail_manage_training_explicitly(self):
        # D15: when manageTraining's own read fails too, it sends nothing and says why (and run.py exits 1).
        fake = FakePss(default_world())
        fault = fake.fail("/RoomService/ListRoomDesigns2", status=404, times=2)
        daily = run_daily(fake)
        self.assertEqual((daily.code, fault.hits), (1, 2))
        self.assertIn("ERROR [SyntheticCaptain] training: cannot read room designs; no training this run\n", daily.log)
        self.assertEqual(training_slice(fake), [])
        assert_every_step_ran(self, daily)
        assert_protocol_clean(self, fake)

    def test_non_fatal_step_failures_keep_exit_zero_and_later_steps(self):
        fake = FakePss(default_world())
        heartbeat = fake.fail("/UserService/HeartBeat4", body="not xml")
        markers = fake.fail("/GalaxyService/ListStarSystemMarkersAndUserMarkers", status=404, times=2)
        daily = run_daily(fake)
        self.assertEqual(daily.code, 0, daily.log[-2000:])
        self.assertEqual((heartbeat.hits, markers.hits), (1, 2))
        self.assertNotIn("POST /GalaxyService/CollectMarker2", fake.paths())
        expected = baseline().traffic_by_step()
        for step in steps_after("collectMiningDronesWithTravel"):
            self.assertEqual(daily.traffic_by_step()[step], expected[step], step)

    def test_rejected_drone_collect_is_reported_as_a_failure_and_keeps_exit_zero(self):
        fake = FakePss(default_world())
        # Aimed at the drone: since the M1 fix, collectAvailableMarkers sends CollectMarker2 for user marker 9004 first.
        fault = fake.fail("/GalaxyService/CollectMarker2", error="Marker already collected",
                          when=lambda p: p.get("starSystemMarkerId") == "9001")
        daily = run_daily(fake)
        self.assertEqual((daily.code, fault.hits), (0, 1), daily.log[-2000:])  # a drone is not a fatal step
        self.assertTrue(fake.state.marker("9004").collected)  # the user marker before it is unaffected
        self.assertIn("CollectMiningDrone(9001) returned False\n", daily.log)
        self.assertNotIn("Collected mining drone 9001", daily.log)
        self.assertFalse(fake.state.marker("9001").collected)
        assert_reports_match_server(self, daily)
        expected = baseline().traffic_by_step()
        for step in steps_after("collectMiningDronesWithTravel"):
            self.assertEqual(daily.traffic_by_step()[step], expected[step], step)
        assert_protocol_clean(self, fake)

    def test_add_training_http_error_is_a_counted_failure_not_a_started_training(self):
        world = minimal_world()
        crew = world.add_crew("Galactic Succubus", room="Galaxy Gym", stats=stats(Hp=20, Weapon=40, Attack=12))
        fake = FakePss(world)
        fault = fake.fail("/TrainingService/AddTraining", status=404)
        daily = run_daily(fake)
        self.assertEqual((daily.code, fault.hits), (0, 1), daily.log[-2000:])  # failures are logged and counted
        self.assertIsNone(crew.session)
        # Regression (AT1, fixed in #279): only a training the server applied is reported as started.
        self.assertEqual(reported_training_starts(daily), [])
        self.assertIn("WARNING [SyntheticCaptain] training: Galactic Succubus (1001) add_training no-change: 1001 is "
                      "not training after the call\n", daily.log)
        self.assertIn("training: started=0 finished=0 consumables=0 skipped=0 failed=1\n", daily.log)
        assert_protocol_clean(self, fake)

    def test_training_failure_mid_roster_still_processes_later_crew(self):
        fake = FakePss(default_world())
        fault = fake.fail("/TrainingService/FinishTraining", when=lambda p: p.get("characterId") == "1001",
                          body='<TrainingService><FinishTraining><Character Fatigue="0" HpImprovement="n/a"/>'
                               "</FinishTraining></TrainingService>")
        daily = run_daily(fake)
        self.assertEqual((daily.code, fault.hits), (0, 1), daily.log[-2000:])
        # D13: 1001's broken finish fails that crew member only; every later crew member is still trained.
        later = [entry for entry in EXPECTED_TRAINING if entry[1] != "1001"]
        self.assertEqual(training_slice(fake), [("FinishTraining", "1001", None, "fault"), *later])
        self.assertIn("WARNING [SyntheticCaptain] training: Galactic Succubus (1001) finish_training no-change: the "
                      "ended training of 1001 is still in place and stat improvements are unchanged\n", daily.log)
        self.assertIn("training: started=5 finished=2 consumables=0 skipped=12 failed=1\n", daily.log)
        self.assertNotIn("manageTraining failed", daily.log)
        self.assertEqual(daily.traffic_by_step()["upgradeCharacters"], baseline().traffic_by_step()["upgradeCharacters"])
        assert_protocol_clean(self, fake)

    def test_a_journal_that_refuses_every_event_changes_neither_training_nor_the_exit_code(self):
        # A real journal can refuse an event (GameplayJournal refuses a secret-shaped value). The refusal is logged,
        # redacted, and the run trains and exits exactly as without a journal.
        class RefusingJournal:
            def append(self, event):
                raise OSError("cannot write /journal?password=hunter2")

            def read(self):
                return []

        self.assertEqual((Client.training_journal, Client.training_account_alias), (None, "legacy"))
        fake = FakePss(default_world())
        with patch.object(Client, "training_journal", RefusingJournal()):
            daily = run_daily(fake)
        self.assertEqual(daily.code, 0, daily.log[-2000:])
        self.assertEqual(daily.traffic_by_step()["manageTraining"], baseline().traffic_by_step()["manageTraining"])
        self.assertIn("ERROR [SyntheticCaptain] training: journal refused the finish_training event: OSError: cannot "
                      "write /journal?password=***REDACTED***\n", daily.log)
        self.assertIn("training: started=6 finished=3 consumables=0 skipped=11 failed=0\n", daily.log)
        self.assertNotIn("hunter2", daily.log)
        self.assertNotIn("manageTraining failed", daily.log)
        assert_protocol_clean(self, fake)

    def test_a_plan_that_does_not_pair_with_the_roster_stops_training_loudly(self):
        # A planner whose output shape changed is a defect, not a crew failure: no training request is sent, the
        # step fails by name and the run exits 1.
        fake = FakePss(default_world())
        with patch("sdk.execution.training.plan_training", return_value=()):
            daily = run_daily(fake)
        self.assertEqual(daily.code, 1, daily.log[-2000:])
        self.assertEqual(training_slice(fake), [])
        self.assertIn("manageTraining failed: the plan has no decision in place for crew member 1001\n", daily.log)
        assert_protocol_clean(self, fake)

    def test_reauth_after_token_expiry_on_a_read_relogs_and_continues(self):
        fake = FakePss(default_world())
        marketplace = "/MessageService/ListActiveMarketplaceMessages5"
        fake.expire_token_once(at=marketplace)
        daily = run_daily(fake)
        self.assertEqual(daily.paths_by_step()["listActiveMarketplaceMessages"], (
            f"GET {marketplace}", "POST /UserService/DeviceLogin17", f"GET {marketplace}"))
        start = fake.paths().index(f"GET {marketplace}")
        self.assertEqual([e.token_status for e in fake.exchanges[start:start + 3]], ["revoked", "n/a", "revoked"])
        self.assertEqual({e.token_status for e in fake.exchanges[start + 3:]} - {"n/a"}, {"current"})
        # Client.request retries the SAME url, which still carries the revoked token, so the retried read fails and
        # the run exits 1. A fix flips this to code 0 and a single violation.
        assert_protocol_clean(self, fake, expected_violations=2)  # PINNED(A1)
        self.assertEqual(daily.code, 1)  # PINNED(A1)
        self.assertEqual(daily.traffic_by_step()["upgradeCharacters"], baseline().traffic_by_step()["upgradeCharacters"])

    def test_reauth_on_a_mutating_request_neither_applies_it_nor_reports_the_failure(self):
        fake = FakePss(default_world())
        fake.expire_token_once(at="/UserService/CollectDailyReward2")
        daily = run_daily(fake)
        self.assertEqual(daily.paths_by_step()["collectDailyReward"], (
            "POST /UserService/CollectDailyReward2", "POST /UserService/DeviceLogin17",
            "POST /UserService/CollectDailyReward2"))
        self.assertEqual([e.error for e in daily.by_step()["collectDailyReward"]][::2],
                         ["revoked accessToken (wanted current)"] * 2)
        assert_protocol_clean(self, fake, expected_violations=2)  # PINNED(A1): the retry reuses the revoked token
        self.assertEqual(fake.state.account.daily_reward_status, "0")  # never applied, so never applied twice
        # The rejected collect is reported as a success and the run exits 0. A fix flips this to code 1 (or, with A1
        # fixed too, to a collected reward).
        self.assertIn("You have collected the daily reward from the dropship.", daily.log)  # PINNED(DR1)
        self.assertEqual(daily.code, 0)  # PINNED(DR1)

    def test_daily_reward_already_collected_sends_no_collect_request(self):
        world = default_world()
        world.account.daily_reward_status = "1"
        daily = run_daily(FakePss(world))
        self.assertEqual(daily.code, 0, daily.log[-2000:])
        self.assertEqual(daily.paths_by_step()["collectDailyReward"], ())
        self.assertIn("Daily reward already collected today.", daily.log)

    def test_wrong_password_exits_1_without_gameplay(self):
        wrong = "wrong-synthetic-pass-7"
        daily = run_daily(FakePss(default_world()), password=wrong)
        self.assertEqual(daily.code, 1)
        self.assertEqual(daily.fake.paths(), ["POST /UserService/DeviceLogin17",
                                              "POST /UserService/UserEmailPasswordAuthorize4"])
        self.assertEqual(daily.steps, [])
        self.assertNotIn(wrong, daily.log)
        self.assertNotIn(wrong, repr(daily.fake.requests))

    def test_device_login_failure_exits_1_after_one_request(self):
        fake = FakePss(default_world())
        fake.fail("/UserService/DeviceLogin17", status=403)
        daily = run_daily(fake)
        self.assertEqual(daily.code, 1)
        self.assertEqual(fake.paths(), ["POST /UserService/DeviceLogin17"])

    def test_missing_password_file_exits_2_without_requests(self):
        daily = run_daily(FakePss(default_world()), write_password_file=False)
        self.assertEqual(daily.code, 2)
        self.assertEqual(daily.fake.requests, [])
        self.assertIn("Password file not found", daily.log)

    def test_authorize_http_error_escapes_main(self):
        # authorize_email_password skips both `if r and ...` guards when the reply is falsy and then raises
        # IndexError on r.text.split('refreshToken="')[1]; run.py calls client.login bare, so the exception escapes
        # main. A fix flips this to `daily.code == 1`.
        fake = FakePss(default_world())
        fake.fail("/UserService/UserEmailPasswordAuthorize4", status=403)
        with self.assertRaises(IndexError):  # PINNED(L1)
            run_daily(fake)
        self.assertEqual(fake.paths(), ["POST /UserService/DeviceLogin17",
                                        "POST /UserService/UserEmailPasswordAuthorize4"])


class DailyRunVariantTests(NeedsHealthyBaseline):
    """Variants of the default world for the conditions it covers on one side only."""

    def assert_other_steps_match_baseline(self, daily: DailyRun, *changed: str) -> None:
        expected = baseline().traffic_by_step()
        for step, traffic in daily.traffic_by_step().items():
            if step not in changed:
                self.assertEqual(traffic, expected[step], step)

    def test_free_starbux_under_the_cap_grabs_a_seeded_quantity_and_skips_task_rewards(self):
        world = default_world()
        world.account.free_starbux_today = 4
        daily = run_daily(FakePss(world))
        self.assertEqual(daily.code, 0, daily.log[-2000:])
        self.assertEqual(daily.mutations_by_step()["grabFlyingStarbux"], (
            ("POST", "/UserService/AddStarbux2", {"quantity": "5", "clientDateTime": N, "checksum": C,
                                                  "accessToken": T}),))  # random.randint(1, 5) under seed 5343
        self.assertIn(("/UserService/AddStarbux2", "727f074bf71e9d7d35d7bbc567de90e1"), checksums(daily.fake))
        self.assertEqual((world.account.free_starbux_today, world.account.credits), (9, 210))  # 215 - 10 task + 5
        self.assertIn("You have collected a total of 9 starbux today.", daily.log)
        # 9 < 10, so run.py skips collectTaskReward altogether.
        assert_every_step_ran(self, daily, skipped=("collectTaskReward",))
        self.assertEqual(world.task("7001").collected, False)
        self.assert_other_steps_match_baseline(daily, "grabFlyingStarbux")
        assert_protocol_clean(self, daily.fake)

    def test_starbux_total_is_the_login_balance_when_no_reply_carries_the_user(self):
        # The CollectAllResources replies (and the pod purchase's) carry User@Credits, which overwrites the balance
        # read at login before infoBux logs it. Here no collect reply carries a User and 900 starbux buy no pod, so
        # the logged total can only be the login reply's Credits: a misread login balance shows.
        world = default_world()
        world.collect_reply_has_user = False
        world.account.credits = 900
        daily = run_daily(FakePss(world))
        self.assertEqual(daily.code, 0, daily.log[-2000:])
        self.assertIn("INFO [SyntheticCaptain] You have a total of 900 starbux.\n", daily.log)
        self.assert_other_steps_match_baseline(daily, "purchaseScorchedPodIfAffordable")
        assert_protocol_clean(self, daily.fake)

    def test_credentials_with_url_reserved_characters_reach_the_server_exactly(self):
        # Synthetic credentials (never real ones) holding every character a form body must percent-encode. The fake
        # decodes the body and accepts only the exact email and password: an unencoded '&' splits the password, '%'
        # starts an escape and '+' decodes to a space.
        world = default_world()
        world.account.email = "synthetic.daily+e2e@example.com"
        world.account.password = "synthetic-p&ss+w%rd #1="
        daily = run_daily(FakePss(world), password=world.account.password, email=world.account.email)
        self.assertEqual(daily.code, 0, daily.log[-2000:])
        def authorize(run: DailyRun) -> Exchange:
            return next(e for e in run.exchanges() if e.path == "/UserService/UserEmailPasswordAuthorize4")

        self.assertEqual((authorize(daily).error, authorize(daily).params["email"]), (None, world.account.email))
        self.assertEqual(sorted(authorize(daily).params), sorted(authorize(baseline()).params))  # nothing split off
        self.assertNotIn(world.account.password, daily.log)
        self.assertNotIn(world.account.password, repr(daily.fake.requests))
        assert_protocol_clean(self, daily.fake)

    def test_drone_in_another_star_system_travels_and_speeds_up_without_sleeping(self):
        world = default_world()
        world.markers.append(Marker("9005", "77"))
        world.travel_seconds = 120  # under 5 minutes: SpeedUpTravelling
        daily = run_daily(FakePss(world))
        self.assertEqual(daily.code, 0, daily.log[-2000:])
        self.assertEqual(daily.paths_by_step()["collectMiningDronesWithTravel"], (
            "GET /GalaxyService/ListStarSystemMarkersAndUserMarkers", "GET /ShipService/GetShipByUserId",
            "POST /GalaxyService/CollectMarker2", "POST /GalaxyService/GoTo", "POST /GalaxyService/SpeedUpTravelling",
            "POST /GalaxyService/CollectMarker2"))
        goto = next(params for _, path, params in daily.fake.requests if path.endswith("/GoTo"))
        self.assertEqual((goto["starSystemId"], goto["clientDateTime"]), ("77", N))
        self.assertEqual((daily.fake.clock.sleeps, daily.fake.now), ([], FAKE_EPOCH))
        self.assertEqual((world.ship.star_system_id, world.marker("9005").collected), ("77", True))
        assert_protocol_clean(self, daily.fake)

    def test_distant_drone_waits_on_the_fake_clock_and_collects_anyway(self):
        world = default_world()
        world.markers.append(Marker("9005", "77"))
        world.travel_seconds = 600  # 10 minutes: no speed-up, poll every 15 s for up to 300 s
        started = time.monotonic()
        daily = run_daily(FakePss(world))
        self.assertLess(time.monotonic() - started, 5.0)  # 300 s of sleeps, none real
        self.assertEqual(daily.code, 0, daily.log[-2000:])
        self.assertEqual(daily.fake.clock.sleeps, [15] * 20)
        self.assertEqual(daily.fake.now, FAKE_EPOCH + datetime.timedelta(seconds=300))
        self.assertNotIn("POST /GalaxyService/SpeedUpTravelling", daily.fake.paths())
        self.assertIn("waitForArrival timed out after 300s", daily.log)
        self.assertTrue(world.marker("9005").collected)  # "attempting collection anyway"
        assert_protocol_clean(self, daily.fake)

    def test_heartbeat_a_day_and_30_seconds_old_is_also_skipped(self):
        world = default_world()
        world.account.last_heartbeat = FAKE_EPOCH - datetime.timedelta(days=1, seconds=30)
        daily = run_daily(FakePss(world))
        # timedelta.seconds ignores days, so this day-old heartbeat counts as 30 s old. A fix flips this to one
        # HeartBeat4 (as for 1 day + 5 min, which is sent).
        self.assertEqual(daily.paths_by_step()["heartbeat"], ())  # PINNED(H1)
        world = default_world()
        world.account.last_heartbeat = FAKE_EPOCH - datetime.timedelta(days=1, minutes=5)
        self.assertEqual(run_daily(FakePss(world)).paths_by_step()["heartbeat"], ("POST /UserService/HeartBeat4",))


def world_without_affordable_research(gas: int):
    """The default world with ``gas`` and only the Fitness line (its next course, 202, costs 50000), so no research is
    started and the gas total every later step sees is ``gas`` whatever upgradeResearches' own threshold is (RS1)."""
    world = default_world()
    world.research_designs = [d for d in world.research_designs if d.root_id == "1"]
    world.resources.gas = gas
    return world


class ResourceReplyShapeTests(NeedsHealthyBaseline):
    def test_a_mineral_only_collect_reply_leaves_gas_at_zero(self):
        # With no gas on board the reply carries only the Mineral item (the shape tests/test_e2e_live_fixes.py feeds
        # the client), so a parser that files the mineral quantity under gas is no longer rescued by the Gas item.
        world = world_without_affordable_research(0)
        world.collect_omits_empty_items = True
        daily = run_daily(FakePss(world))
        self.assertEqual(daily.code, 0, daily.log[-2000:])
        self.assertIn("INFO [SyntheticCaptain] There is a total of 40000 minerals on your ship.\n", daily.log)
        self.assertIn("INFO [SyntheticCaptain] There is a total of 0 gas on your ship.\n", daily.log)
        upgraded = [params["characterId"] for _, path, params in daily.mutations_by_step()["upgradeCharacters"]
                    if path == "/CharacterService/UpgradeCharacter"]
        self.assertEqual(upgraded, ["1009"])  # 0 gas; 1010 (130 gas) and 1014 (33 gas) must wait
        assert_protocol_clean(self, daily.fake)


class BoundaryTests(NeedsHealthyBaseline):
    """Each daily-path threshold, run just below and exactly at its value (the default world is far from all of them).

    The prices are the world's own (room design PriceString, research GasCost); the server accepts a request the
    account can exactly afford, so "exactly at the price" must be requested. One pair per threshold.
    """

    def run_ok(self, world, local_offset: datetime.timedelta = HOST_UTC) -> DailyRun:
        daily = run_daily(FakePss(world, local_offset=local_offset))
        self.assertEqual(daily.code, 0, daily.log[-2000:])
        assert_protocol_clean(self, daily.fake)
        return daily

    def test_heartbeat_59_seconds_old_is_skipped_and_60_seconds_old_is_sent(self):
        # heartbeat skips while (utcnow - lastHeartBeat).seconds // 60 == 0, i.e. under 60 s. LastHeartBeatDate is UTC,
        # so the edge must not move on a host whose local clock is not UTC: a gate reading local time sends the 59 s
        # heartbeat on both (ahead: 9 h old; behind: a negative age, whose .seconds is almost a day).
        hosts = {"UTC": HOST_UTC, "UTC-5": HOST_UTC_MINUS_5, "UTC+9": HOST_UTC_PLUS_9}
        for (host, offset), (age, expected) in itertools.product(
                hosts.items(), ((59, ()), (60, ("POST /UserService/HeartBeat4",)))):
            with self.subTest(host=host, age_seconds=age):
                world = default_world()
                world.account.last_heartbeat = FAKE_EPOCH - datetime.timedelta(seconds=age)
                daily = self.run_ok(world, offset)
                self.assertEqual(daily.paths_by_step()["heartbeat"], expected)
                if offset == HOST_UTC:  # (non-UTC hosts differ in crew upgrades: TZ1)
                    actual, utc_baseline = daily.traffic_by_step(), baseline().traffic_by_step()
                    self.assertEqual({step: t for step, t in actual.items() if step != "heartbeat"},
                                     {step: t for step, t in utc_baseline.items() if step != "heartbeat"})

    def test_room_upgrade_priced_in_minerals_is_requested_at_exactly_its_price(self):
        # Mineral Storage (room 104) -> Lv2 costs mineral:20000; the client's total at the check is the world's mineral.
        upgrade = ("POST", "/RoomService/UpgradeRoom2", {"roomId": "104", "upgradeRoomDesignId": "51", "accessToken": T})
        for mineral, requested in ((19999, False), (20000, True)):
            with self.subTest(mineral=mineral):
                world = default_world()
                world.resources.mineral = mineral
                daily = self.run_ok(world)
                self.assertEqual(upgrade in daily.mutations_by_step().get("upgradeRooms", ()), requested)
                self.assertEqual(world.room("104").status, "Upgrading" if requested else "Normal")

    def test_room_upgrade_priced_in_gas_is_requested_at_exactly_its_price(self):
        # Gas Storage (room 105, made Normal here) -> Lv2 costs gas:2000.
        upgrade = ("POST", "/RoomService/UpgradeRoom2", {"roomId": "105", "upgradeRoomDesignId": "61", "accessToken": T})
        for gas, requested in ((1999, False), (2000, True)):
            with self.subTest(gas=gas):
                world = world_without_affordable_research(gas)
                world.room("105").status, world.room("105").pending_design_id = "Normal", None
                daily = self.run_ok(world)
                self.assertEqual(upgrade in daily.mutations_by_step()["upgradeRooms"], requested)
                self.assertEqual((world.room("105").status, world.resources.gas),
                                 ("Upgrading", 0) if requested else ("Normal", gas))

    def test_research_at_exactly_its_price_is_never_started(self):
        # Education 101 (design 10) costs 2000 gas; Advanced Training (design 20) costs 3000.
        add_10 = ("POST", "/ResearchService/AddResearch", {"researchDesignId": "10", "researchStartDate": N,
                                                           "accessToken": T})
        for gas, expected in ((2000, (COLLECT,)),  # PINNED(RS1): a fix flips this to (COLLECT, add_10)
                              (2001, (COLLECT, add_10))):
            with self.subTest(gas=gas):
                world = default_world()
                world.resources.gas = gas
                daily = self.run_ok(world)
                self.assertEqual(daily.mutations_by_step()["upgradeResearches"], expected)

    def test_scorched_pod_is_bought_at_exactly_its_price(self):
        # The pod costs 1000 starbux; the client's balance is the login reply's Credits.
        pod = ("POST", "/ShopService/PurchaseCatalog2", {"argument": "1291", "clientDateTime": N, "checksum": C,
                                                         "accessToken": T})
        for credits, bought in ((999, False), (1000, True)):
            with self.subTest(credits=credits):
                world = default_world()
                world.account.credits = credits
                daily = self.run_ok(world)
                self.assertEqual(daily.mutations_by_step().get("purchaseScorchedPodIfAffordable", ()),
                                 (pod,) if bought else ())
                self.assertEqual(world.account.credits, (0 if bought else 999) + 15)  # + task 7001 (10), message (5)
                self.assertEqual("Not enough Starbux for Scorched Pod: have 999, need 1000" in daily.log, not bought)

    def test_crew_upgrade_is_sent_when_xp_equals_its_xp_cost(self):
        # Cadet Ensign (1013), Common, level 1: production's cost is 90 xp and 0 gas; xp >= cost upgrades.
        upgrade = ("POST", "/CharacterService/UpgradeCharacter", {"characterId": "1013", "accessToken": T})
        for xp, upgraded in ((89, False), (90, True)):
            with self.subTest(xp=xp):
                world = default_world()
                world.character("1013").xp = xp
                daily = self.run_ok(world)
                self.assertEqual(upgrade in daily.mutations_by_step()["upgradeCharacters"], upgraded)
                self.assertEqual(world.character("1013").level, 2 if upgraded else 1)

    def test_crew_upgrade_is_sent_when_gas_equals_its_gas_cost(self):
        # Rookie Pilot (1010) level 5 -> 6 costs 130 gas in production's table; the check is gas_cost <= gas total.
        # (The fake charges no gas for UpgradeCharacter, so the gas total stays put after the upgrade.)
        upgrade = ("POST", "/CharacterService/UpgradeCharacter", {"characterId": "1010", "accessToken": T})
        for gas, upgraded in ((129, False), (130, True)):
            with self.subTest(gas=gas):
                world = world_without_affordable_research(gas)
                daily = self.run_ok(world)
                self.assertEqual(upgrade in daily.mutations_by_step()["upgradeCharacters"], upgraded)
                self.assertEqual(world.character("1010").level, 6 if upgraded else 5)


class MinimalWorldTests(NeedsHealthyBaseline):
    """minimal_world(): the base for Wave 2 rosters. Without crew the daily run mutates nothing."""

    def test_minimal_world_run_exits_zero_and_only_collects_resources(self):
        daily = run_daily(FakePss(minimal_world()))
        self.assertEqual(daily.code, 0, daily.log[-2000:])
        self.assertEqual(daily.mutations_by_step(), {
            "upgradeResearches": (COLLECT,),
            "speedUpBoostGauge": (("POST", "/ShipService/SpeedUpShipConstructionUsingBoostGauge",
                                   {"shipId": "800000501", "clientDateTime": N, "accessToken": T}),)})
        assert_every_step_ran(self, daily)
        assert_protocol_clean(self, daily.fake)

    def test_a_roster_with_a_single_character_design_trains(self):
        # D5: one CharacterDesign record (a dict, not a list) is read like any other.
        world = minimal_world()
        crew = world.add_crew("Galactic Succubus", room="Galaxy Gym", stats=stats(Hp=20, Weapon=40, Attack=12))
        daily = run_daily(FakePss(world))
        self.assertEqual((daily.code, training_slice(daily.fake)), (0, [("AddTraining", crew.id, "3", None)]))


class MultiRunTests(NeedsHealthyBaseline):
    """The harness Wave 2 builds on: one FakePss, run, let time pass, run again (state and clock carry over)."""

    def test_second_run_after_time_passes_finishes_the_training_the_first_run_started(self):
        fake = FakePss(default_world())
        first = run_daily(fake)
        fake.advance(hours=2, minutes=1)
        second = run_daily(fake)
        self.assertEqual((first.code, second.code), (0, 0), second.log[-2000:])
        self.assertEqual(training_slice(fake, second.first_exchange), EXPECTED_SECOND_RUN_TRAINING)
        assert_protocol_clean(self, fake)
        for daily in (first, second):
            assert_reports_match_server(self, daily)
        succubus, delish = fake.state.character("1001"), fake.state.character("1003")
        self.assertEqual((delish.stats["HpImprovement"], delish.fatigue), (22, 10))  # finished in the first run
        # D14: the first run finished 1001's Bench Press and started S-T2 Muscle Beach (3 h), still running 2 h later.
        self.assertEqual((succubus.stats["HpImprovement"], succubus.last_session.design_id), (18, "4"))
        self.assertIs(succubus.last_session.applied, False)

    def test_one_crew_training_cycle_on_a_minimal_world(self):
        """Start, time passes, finish and stat gain, through run.main() and the real Client on a custom roster."""
        world = minimal_world()
        crew = world.add_crew("Galactic Succubus", room="Galaxy Gym", stats=stats(Hp=20, Weapon=40, Attack=12))
        fake = FakePss(world)

        def training_of(daily: DailyRun) -> list[tuple]:
            return training_slice(fake, daily.first_exchange)

        first = run_daily(fake)  # 72 / 100 = 72 %: S-T1, Bench Press (ungated), with no FinishTraining first (D3)
        self.assertEqual(training_of(first), [("AddTraining", crew.id, "3", None)])
        self.assertEqual(first.mutations_by_step(), {
            "upgradeResearches": (COLLECT,),
            "speedUpBoostGauge": (("POST", "/ShipService/SpeedUpShipConstructionUsingBoostGauge",
                                   {"shipId": "800000501", "clientDateTime": N, "accessToken": T}),),
            "manageTraining": (("POST", "/TrainingService/AddTraining",
                                {"trainingDesignId": "3", "characterId": crew.id,
                                 "trainingStartDate": N, "accessToken": T}),)})
        self.assertIn("] Starting training Bench Press for Galactic Succubus in Galaxy Gym with 72% training complete\n",
                      first.log)
        self.assertIn("training: started=1 finished=0 consumables=0 skipped=0 failed=0\n", first.log)
        self.assertEqual(crew.training_end, FAKE_EPOCH + HOUR)

        fake.advance(hours=1, seconds=30)  # ended 30 s ago: inside the 60 s finish grace, so nothing is sent
        second = run_daily(fake)
        self.assertEqual(training_of(second), [])
        self.assertIn("training: started=0 finished=0 consumables=0 skipped=1 failed=0\n", second.log)
        fake.advance(seconds=31)  # 61 s after the end (D2: no extra wait after the end)
        third = run_daily(fake)
        self.assertEqual(training_of(third), [("FinishTraining", crew.id, None, None)])
        self.assertEqual((crew.stats["HpImprovement"], crew.fatigue), (22, 10))
        self.assertIs(crew.last_session.applied, True)
        self.assertIn("] Completed training for Galactic Succubus in Galaxy Gym with Hp +2\n", third.log)
        # 74 %: S-T2 is research-gated, so the replan from the reply starts nothing.
        self.assertIn("training: started=0 finished=1 consumables=0 skipped=1 failed=0\n", third.log)
        self.assertEqual({d.code for d in (first, second, third)}, {0})
        assert_protocol_clean(self, fake)
        for daily in (first, second, third):
            assert_reports_match_server(self, daily)

    def test_research_gates_open_exactly_at_their_course(self):
        # D1 and the live-name regression: the gates read the highest completed course of a line by design id. With
        # only Fitness 101 done (the default world minus 102-201), P-T1 opens (1004 starts) and every T2 tier, which
        # needs course 201, stays shut.
        world = default_world()
        world.researches = [research for research in world.researches if research.design_id == "1"]
        daily = run_daily(FakePss(world))
        self.assertEqual(daily.code, 0, daily.log[-2000:])
        self.assertEqual(training_slice(daily.fake), [
            ("FinishTraining", "1001", None, None),
            ("FinishTraining", "1003", None, None),
            ("AddTraining", "1004", "21", None),  # P-T1 Big Book of Science: course 101 is enough
            ("FinishTraining", "1016", None, None),
        ])
        prefix = "INFO [SyntheticCaptain] training: "
        for line in ("Galactic Succubus (1001) skip research_missing: secondary T2 research gate not met",
                     "Delish (1003) skip research_missing: secondary T2 research gate not met",
                     "Galactic Sprite (1005) skip research_missing: primary T2 research gate not met",
                     "Admiral Serena (1008) skip research_missing: secondary T2 research gate not met",
                     "Galactic Sprite (1016) skip research_missing: secondary T2 research gate not met"):
            with self.subTest(line=line):
                self.assertIn(f"{prefix}{line}\n", daily.log)
        self.assertIn(f"{prefix}started=1 finished=3 consumables=0 skipped=16 failed=0\n", daily.log)
        assert_reports_match_server(self, daily)
        assert_protocol_clean(self, daily.fake)


# A host behind UTC and one ahead of it: a local/UTC mix-up moves a decision one way on the first and the other way on
# the second, so between them every such mix-up in a decision the default world exercises shows.
NON_UTC_HOSTS = {"UTC-5": HOST_UTC_MINUS_5, "UTC+9": HOST_UTC_PLUS_9}


@_run_once
def non_utc_run(host: str) -> DailyRun:
    """One un-faulted run of the default world on a non-UTC host, shared read-only like ``baseline()``."""
    return run_daily(FakePss(default_world(), local_offset=NON_UTC_HOSTS[host]))


class NonUtcHostTests(NeedsHealthyBaseline):
    """The default world on a developer laptop whose clock is not UTC.

    The server speaks UTC (every date it sends, every date production must send), so a correct client decides exactly
    as on the UTC host the goldens use. Decisions that read local time instead are pinned: TZ1 (crew upgrades) and TZ2
    (drone travel, in its own world). Everything else, training included, must equal the UTC baseline.
    """

    def test_the_host_clock_is_local_but_every_date_sent_is_utc(self):
        for host, offset in NON_UTC_HOSTS.items():
            with self.subTest(host=host):
                daily = non_utc_run(host)
                self.assertEqual(daily.fake.clock.local_now - daily.fake.now, offset)
                self.assertEqual(daily.code, 0, daily.log[-2000:])
                self.assertEqual(daily.step_order(), baseline().step_order())
                self.assertEqual(checksums(daily.fake), checksums(baseline().fake))  # built from UTC clientDateTimes
                assert_protocol_clean(self, daily.fake)

    def test_training_decisions_do_not_depend_on_the_host_timezone(self):
        # The S-T1 rest gate compares the UTC TrainingEndDate with utcnow(): 1001's session ended 2 h ago, so a gate
        # reading local time would hold it back on UTC-5 (and open 1003's on UTC+9).
        for host in NON_UTC_HOSTS:
            with self.subTest(host=host):
                daily = non_utc_run(host)
                self.assertEqual(daily.traffic_by_step()["manageTraining"],
                                 baseline().traffic_by_step()["manageTraining"])
                self.assertEqual(training_slice(daily.fake), training_slice(baseline().fake))
                self.assertEqual(crew_snapshot(daily.fake.state)["1001"], crew_snapshot(baseline().fake.state)["1001"])
                assert_reports_match_server(self, daily)

    def test_every_step_but_crew_upgrades_sends_what_the_utc_host_sends(self):
        expected, state = baseline().traffic_by_step(), baseline().fake.state
        for host in NON_UTC_HOSTS:
            daily = non_utc_run(host)
            traffic = daily.traffic_by_step()
            for step in ("login", *DAILY_STEPS):
                if step != "upgradeCharacters":
                    with self.subTest(host=host, step=step):
                        self.assertEqual(traffic[step], expected[step])
            with self.subTest(host=host, state="economy and ship"):
                self.assertEqual(economy_snapshot(daily.fake.state), economy_snapshot(state))
                self.assertEqual(ship_snapshot(daily.fake.state), ship_snapshot(state))

    def test_crew_upgrades_compare_the_utc_available_date_with_local_time(self):
        def upgrades(daily: DailyRun) -> list[tuple]:
            return [(e.params["characterId"], e.error) for e in daily.by_step()["upgradeCharacters"]
                    if e.path == "/CharacterService/UpgradeCharacter"]

        utc = upgrades(baseline())  # the golden pins it: 1009, 1010 and 1014, all applied
        self.assertIn(("1014", None), utc)
        self.assertNotIn("1012", [cid for cid, _ in utc])
        # UTC-5: 1014 became available 2 h ago (UTC), but local time is 5 h behind, so it is skipped for 3 more hours.
        # A fix flips this to the UTC list.
        west = non_utc_run("UTC-5")
        self.assertEqual(upgrades(west), [row for row in utc if row[0] != "1014"])  # PINNED(TZ1)
        self.assertEqual(west.fake.state.character("1014").level, 3)  # PINNED(TZ1)
        # UTC+9: 1012 becomes available in 3 h (UTC), but local time is 9 h ahead, so UpgradeCharacter is sent now and
        # the server refuses it (in roster order, between 1010 and 1014); upgradeCharacters ignores the refusal and
        # the run still exits 0. A fix flips this to the UTC list.
        east = non_utc_run("UTC+9")
        roster = [crew.id for crew in east.fake.state.characters]  # the order ListAllCharactersOfUser sends
        expected = sorted([*utc, ("1012", "Crew is not available yet")], key=lambda row: roster.index(row[0]))
        self.assertEqual(upgrades(east), expected)  # PINNED(TZ1)
        self.assertEqual((east.code, east.fake.state.character("1012").level), (0, 1))  # PINNED(TZ1)

    def test_drone_travel_compares_the_utc_arrival_with_local_time(self):
        def drone_run(offset) -> DailyRun:
            world = default_world()
            world.markers.append(Marker("9005", "77"))
            world.travel_seconds = 120  # under 5 minutes: a UTC host speeds the trip up
            return run_daily(FakePss(world, local_offset=offset))

        utc, west, east = drone_run(HOST_UTC), drone_run(HOST_UTC_MINUS_5), drone_run(HOST_UTC_PLUS_9)
        travel = ("POST /GalaxyService/GoTo", "POST /GalaxyService/SpeedUpTravelling",
                  "POST /GalaxyService/CollectMarker2")
        self.assertEqual(utc.paths_by_step()["collectMiningDronesWithTravel"][-3:], travel)
        self.assertEqual(utc.fake.clock.sleeps, [])
        for daily in (utc, west, east):
            self.assertEqual((daily.code, daily.fake.state.marker("9005").collected), (0, True))
            assert_protocol_clean(self, daily.fake)
        # UTC-5: the 120 s trip looks 5 h long, so it is never sped up; the client polls 20 x 15 s, times out and
        # collects anyway. A fix flips this to the UTC run (SpeedUpTravelling, no sleeps).
        self.assertNotIn("POST /GalaxyService/SpeedUpTravelling", west.fake.paths())  # PINNED(TZ2)
        self.assertEqual(west.fake.clock.sleeps, [15] * 20)  # PINNED(TZ2)
        self.assertIn("waitForArrival timed out after 300s", west.log)  # PINNED(TZ2)
        # UTC+9: the arrival looks 9 h past, so the drone is collected at once, 120 s before the ship arrives. A fix
        # flips this to the UTC run.
        self.assertNotIn("POST /GalaxyService/SpeedUpTravelling", east.fake.paths())  # PINNED(TZ2)
        self.assertEqual(east.fake.clock.sleeps, [])  # PINNED(TZ2)
        self.assertEqual(east.fake.state.ship.arrival, east.fake.now + datetime.timedelta(seconds=120))  # PINNED(TZ2)
        self.assertIn("Ship already arrived", east.log)  # PINNED(TZ2)


class HarnessTests(unittest.TestCase):
    """The DailyRun bookkeeping and the shared-run cache, on synthetic exchanges (no run.main())."""

    @staticmethod
    def synthetic_run(steps: list[tuple[str, int]], count: int = 7, end: int = 6) -> tuple[DailyRun, list[Exchange]]:
        fake = FakePss(World())
        fake.exchanges = [Exchange("GET", f"/Read/{i}", {}, "n/a") for i in range(count)]
        return DailyRun(0, "", fake, 0, 0, steps, 0, end), fake.exchanges

    def test_by_step_concatenates_every_slice_of_a_repeated_step(self):
        daily, ex = self.synthetic_run([("a", 1), ("b", 3), ("a", 4)])
        by_step = daily.by_step()
        self.assertEqual(list(by_step), ["login", "a", "b"])
        self.assertEqual(by_step, {"login": ex[0:1], "a": ex[1:3] + ex[4:6], "b": ex[3:4]})
        self.assertEqual(daily.paths_by_step()["a"], ("GET /Read/1", "GET /Read/2", "GET /Read/4", "GET /Read/5"))
        self.assertEqual(daily.traffic_by_step()["a"].reads,
                         (("GET /Read/1", 1), ("GET /Read/2", 1), ("GET /Read/4", 1), ("GET /Read/5", 1)))
        self.assertEqual([daily.step_of(i) for i in range(6)], ["login", "a", "a", "b", "a", "a"])

    def test_traffic_by_step_is_computed_once_per_finished_run(self):
        daily, ex = self.synthetic_run([("a", 2)])
        first = daily.traffic_by_step()
        self.assertIs(daily.traffic_by_step(), first)
        with self.assertRaises(TypeError):
            first["a"] = first["login"]  # ty: ignore[invalid-assignment]  # shared: read-only
        ex.append(Exchange("GET", "/Read/later", {}, "n/a"))  # a later run on the same fake does not change it
        self.assertEqual(daily.traffic_by_step(), first)

    def test_a_shared_run_that_raises_is_run_once_and_re_raised_every_time(self):
        calls = []

        @_run_once
        def broken(host: str) -> DailyRun:
            calls.append(host)
            raise RuntimeError(f"login failed on {host}")

        for _ in range(3):
            with self.assertRaisesRegex(RuntimeError, "login failed on UTC-5"):
                broken("UTC-5")
        self.assertEqual(calls, ["UTC-5"])
        with self.assertRaisesRegex(RuntimeError, "login failed on UTC\\+9"):
            broken("UTC+9")
        self.assertEqual(calls, ["UTC-5", "UTC+9"])
        self.assertTrue(hasattr(baseline, "cache") and hasattr(non_utc_run, "cache"))  # both shared runs use it


class GoldenRegenerationTests(unittest.TestCase):
    """Tests the --print-golden tool only. The golden VALUES are checked by DailyRunGoldenTests and friends, so this
    compares the printed text with the freshly computed values, never with the constants in this file: a production
    change fails the golden tests, not this one."""

    def test_print_golden_covers_every_constant_and_its_output_pastes_back_exactly(self):
        module = sys.modules[__name__]
        fresh = current_goldens()
        self.assertEqual(set(fresh), {name for name in vars(module) if name.startswith("EXPECTED_")})
        out = io.StringIO()
        with redirect_stdout(out):
            print_golden(show_all=True, goldens=fresh)
        pasted: dict[str, Any] = {}
        exec(out.getvalue(), {}, pasted)  # noqa: S102 - our own pprint output
        self.assertEqual(pasted, fresh)
        for name, value in fresh.items():  # same types (tuple vs list) and dict key order as computed
            self.assertEqual(repr(pasted[name]), repr(value), name)

    def test_print_golden_prints_only_the_goldens_that_differ_from_this_file(self):
        # No daily run: the file's own constants, with one value changed.
        goldens = {name: value for name, value in vars(sys.modules[__name__]).items() if name.startswith("EXPECTED_")}
        goldens["EXPECTED_CREW"] = {"1001": (40, 99, 0)}
        out = io.StringIO()
        with redirect_stdout(out):
            print_golden(goldens=goldens)
        lines = out.getvalue().splitlines()
        self.assertIn("EXPECTED_CREW = {'1001': (40, 99, 0)}", lines)
        self.assertEqual(len(lines), len(goldens))
        self.assertEqual(sum(not line.startswith("# ") for line in lines), 1, lines)


class DefectRegistryTests(unittest.TestCase):
    FILES = ("tests/test_daily_run_offline_e2e.py", "tests/test_fake_pss_server.py", "tests/fakes/state.py",
             "tests/fakes/pss_server.py")

    def test_every_pinned_tag_names_a_known_defect_and_every_defect_is_pinned_or_marked(self):
        repo = Path(__file__).absolute().parents[1]
        tagged = set()
        for name in self.FILES:
            tagged |= set(re.findall(r"PINNED\(([A-Za-z0-9]+)\)", (repo / name).read_text(encoding="utf-8")))
        self.assertEqual(tagged - set(KNOWN_DEFECTS), set(), "PINNED tags without a KNOWN_DEFECTS entry")
        unpinned = {key for key, (what, _) in KNOWN_DEFECTS.items() if what.startswith("(not pinned)")}
        self.assertEqual(set(KNOWN_DEFECTS) - tagged - unpinned, set(), "defects neither pinned nor marked")
        self.assertEqual(tagged & unpinned, set(), "marked (not pinned) but pinned")
        for key, (what, where) in KNOWN_DEFECTS.items():
            self.assertRegex(where, r"^(sdk/client\.py|run\.py)", key)


# --- golden regeneration -----------------------------------------------------------------------------------------

def current_goldens() -> dict[str, object]:
    """Every EXPECTED_* golden recomputed from a fresh default run (and a second run 2 h 1 min later)."""
    fake = FakePss(default_world())
    daily = run_daily(fake)
    paths, mutations = daily.paths_by_step(), daily.mutations_by_step()
    training_paths = paths.pop("manageTraining")
    mutations.pop("manageTraining", None)
    goldens = {
        "EXPECTED_STEP_ORDER": daily.step_order(),
        "EXPECTED_STEP_PATHS": paths,
        "EXPECTED_STEP_MUTATIONS": mutations,
        "EXPECTED_CHECKSUMS": checksums(fake),
        "EXPECTED_ECONOMY": economy_snapshot(fake.state),
        "EXPECTED_SHIP": ship_snapshot(fake.state),
        "EXPECTED_CREW": crew_snapshot(fake.state),
        "EXPECTED_TRAINING_PATHS": training_paths,
        "EXPECTED_TRAINING": training_slice(fake),
    }
    fake.advance(hours=2, minutes=1)
    second = run_daily(fake)
    goldens["EXPECTED_SECOND_RUN_TRAINING"] = training_slice(fake, second.first_exchange)
    return goldens


def print_golden(show_all: bool = False, goldens: dict[str, object] | None = None) -> None:
    """Print, as paste-ready Python, every golden that differs from this module (all of them with ``show_all``).

    ``goldens`` (default: ``current_goldens()``) lets the self-test pass values it already computed.
    """
    module = sys.modules[__name__]
    for name, value in (current_goldens() if goldens is None else goldens).items():
        if show_all or getattr(module, name) != value:
            print(f"{name} = {pprint.pformat(value, width=116, sort_dicts=False)}")
        else:
            print(f"# {name}: unchanged")


if __name__ == "__main__":
    if "--print-golden" in sys.argv:
        print_golden(show_all="--all" in sys.argv)
    else:
        unittest.main()
