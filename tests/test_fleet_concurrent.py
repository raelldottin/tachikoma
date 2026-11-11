"""Tests for sdk.fleet.concurrent: bounded concurrent fleet runs with sequential guarantees. Fake games, no network.

Overlap is proven with barriers and events, never with sleeps; every wait has a timeout so a
regression fails instead of hanging.
"""

import itertools
import tempfile
import threading
import unittest
from pathlib import Path

from sdk.fleet import registry_from_dict
from sdk.fleet.concurrent import MODE_CONCURRENT, ConcurrentFleetOrchestrator
from sdk.fleet.orchestrator import AccountContext, FleetClaims, FleetOrchestrator
from sdk.journal import GameplayJournal
from tests.synthetic_fixtures import SYNTHETIC_EMAIL
from tests.test_fleet_orchestrator import DESIGN, Clock, FakeGame, profile

WAIT = 5.0  # generous timeout for events and barriers; reached only when a test is failing
FIRST_SNAPSHOT = 1  # the orchestrator's first-snapshot step
PLANNING_SNAPSHOT = 2  # the scheduler's planning snapshot, inside the scheduler run


class LockedClock(Clock):
    """The shared test clock, safe to call from worker threads."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return super().__call__()


class ConcurrentFleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.clock = LockedClock()
        self.games = {}
        self.games_lock = threading.Lock()

    def tearDown(self):
        self.tmp.cleanup()

    def game(self, alias):
        with self.games_lock:
            return self.games.setdefault(alias, FakeGame(alias, self.clock))

    def factory(self, profile, snapshot=None, journal=None):
        game = self.game(profile.alias)
        return AccountContext(game, snapshot or game.snapshot,
                              GameplayJournal(journal or self.dir / f"{profile.alias}.jsonl"), DESIGN)

    def registry(self, *entries):
        return registry_from_dict({"version": 1, "accounts": list(entries)})

    def orchestrator(self, *entries, factory=None, cls=ConcurrentFleetOrchestrator, **kwargs):
        kwargs.setdefault("timer", lambda: 0.0)
        return cls(self.registry(*entries), factory or self.factory, clock=self.clock, **kwargs)

    def snapshot_hook(self, hooks, call):
        """A factory whose account calls ``hooks[alias]()`` once, just before its ``call``-th snapshot."""
        def factory(profile):
            game = self.game(profile.alias)
            hook, calls = hooks.get(profile.alias), []

            def snapshot():
                calls.append(True)
                if hook is not None and len(calls) == call:
                    hook()
                return game.snapshot()

            return self.factory(profile, snapshot=snapshot)
        return factory

    def test_dry_run_is_the_default_and_mode_is_concurrent(self):
        run = self.orchestrator(profile("alpha"), profile("bravo")).run()
        self.assertTrue(run.dry_run)
        self.assertEqual(run.mode, MODE_CONCURRENT)
        self.assertEqual([a.status for a in run.accounts], ["completed", "completed"])
        self.assertEqual([a.attempted for a in run.accounts], [0, 0])
        self.assertTrue(all(not game.calls for game in self.games.values()))

    def test_two_schedulers_really_run_at_the_same_time(self):
        # Each scheduler's planning snapshot waits at a two-party barrier: it only passes when
        # both accounts are inside their scheduler runs at once. Run one at a time, it would break.
        barrier = threading.Barrier(2, timeout=WAIT)
        factory = self.snapshot_hook({"alpha": barrier.wait, "bravo": barrier.wait}, PLANNING_SNAPSHOT)
        run = self.orchestrator(profile("alpha"), profile("bravo"), factory=factory,
                                max_workers=2).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["completed", "completed"], run.accounts)
        self.assertFalse(barrier.broken)
        for alias in ("alpha", "bravo"):
            self.assertIn("addResearch", self.games[alias].calls)
            events = GameplayJournal(self.dir / f"{alias}.jsonl").read()
            self.assertTrue(events)
            self.assertEqual({event.account_alias for event in events}, {alias})

    def test_results_are_in_alias_order_whatever_the_completion_order(self):
        bravo_done = threading.Event()
        finished = []

        class Recording(ConcurrentFleetOrchestrator):
            def _run_scheduler(self, profile, *args):
                result = super()._run_scheduler(profile, *args)
                finished.append(profile.alias)
                if profile.alias == "bravo":
                    bravo_done.set()
                return result

        # alpha's scheduler cannot plan until bravo has finished its whole run.
        factory = self.snapshot_hook({"alpha": lambda: self.assertTrue(bravo_done.wait(WAIT))}, PLANNING_SNAPSHOT)
        run = self.orchestrator(profile("alpha"), profile("bravo"), factory=factory, cls=Recording,
                                max_workers=2).run(execute=True)
        self.assertEqual(finished, ["bravo", "alpha"])
        self.assertEqual([a.alias for a in run.accounts], ["alpha", "bravo"])
        self.assertEqual([a.status for a in run.accounts], ["completed", "completed"])

    def test_an_opened_account_moves_on_before_later_accounts_open(self):
        # With fewer workers than accounts, the earliest account's next step goes first, so a
        # logged-in session is scheduled before later accounts log in. One worker is sequential order.
        steps = []

        class Recording(ConcurrentFleetOrchestrator):
            def _run_scheduler(self, profile, *args):
                steps.append(f"schedule {profile.alias}")
                return super()._run_scheduler(profile, *args)

        def factory(profile):
            steps.append(f"open {profile.alias}")
            return self.factory(profile)

        run = self.orchestrator(profile("alpha"), profile("bravo"), profile("charlie"), factory=factory,
                                cls=Recording, max_workers=1).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["completed"] * 3)
        self.assertEqual(steps, ["open alpha", "schedule alpha", "open bravo", "schedule bravo",
                                 "open charlie", "schedule charlie"])

    def test_the_pool_is_bounded(self):
        threads, active, peak = set(), [0], [0]
        lock = threading.Lock()

        def factory(profile):
            game = self.game(profile.alias)

            def snapshot():
                with lock:
                    threads.add(threading.get_ident())
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                try:
                    return game.snapshot()
                finally:
                    with lock:
                        active[0] -= 1

            return self.factory(profile, snapshot=snapshot)

        run = self.orchestrator(profile("alpha"), profile("bravo"), profile("charlie"), factory=factory,
                                max_workers=1).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["completed"] * 3)
        self.assertEqual(peak[0], 1)
        self.assertEqual(len(threads), 1)
        self.assertNotIn(threading.get_ident(), threads)

    def test_max_workers_must_be_a_positive_integer(self):
        for bad in (0, -1, True, 1.5, "2"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.orchestrator(profile("alpha"), max_workers=bad)

    def test_matches_the_sequential_orchestrator(self):
        entries = (profile("alpha"), profile("bravo", budgets={"minerals": 200, "starbux": 0}),
                   profile("charlie", enabled=False))
        concurrent = self.orchestrator(*entries).run(execute=True)
        concurrent_events = {a: len(GameplayJournal(self.dir / f"{a}.jsonl").read()) for a in ("alpha", "bravo")}

        self.dir = Path(self.tmp.name) / "sequential"
        self.dir.mkdir()
        self.games = {}
        sequential = self.orchestrator(*entries, cls=FleetOrchestrator).run(execute=True)
        sequential_events = {a: len(GameplayJournal(self.dir / f"{a}.jsonl").read()) for a in ("alpha", "bravo")}

        self.assertEqual(sequential.mode, "sequential")
        self.assertEqual(concurrent.accounts, sequential.accounts)
        self.assertEqual(concurrent_events, sequential_events)
        self.assertEqual(concurrent.stop_reason, sequential.stop_reason)

    def test_disabled_accounts_are_not_touched(self):
        run = self.orchestrator(profile("alpha"), profile("bravo", enabled=False)).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["completed", "disabled"])
        self.assertNotIn("bravo", self.games)

    def test_one_accounts_failure_does_not_reach_another_in_flight(self):
        # alpha fails while bravo's scheduler is mid-run (both are past the barrier); bravo completes.
        barrier = threading.Barrier(2, timeout=WAIT)

        def alpha_fails():
            barrier.wait()
            raise ConnectionError(f"session lost for {SYNTHETIC_EMAIL}")

        factory = self.snapshot_hook({"alpha": alpha_fails, "bravo": barrier.wait}, PLANNING_SNAPSHOT)
        run = self.orchestrator(profile("alpha"), profile("bravo"), factory=factory,
                                max_workers=2).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["error", "completed"])
        self.assertIn("ConnectionError", run.accounts[0].reason)
        self.assertNotIn(SYNTHETIC_EMAIL, run.accounts[0].reason)
        self.assertIn("addResearch", self.games["bravo"].calls)

    def test_factory_and_first_snapshot_failures_are_contained_and_redacted(self):
        def factory(profile):
            if profile.alias == "alpha":
                raise ConnectionError(f"login failed for {SYNTHETIC_EMAIL}")
            return self.factory(profile)

        run = self.orchestrator(profile("alpha"), profile("bravo"), factory=factory).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["error", "completed"])
        self.assertIn("ConnectionError", run.accounts[0].reason)
        self.assertNotIn(SYNTHETIC_EMAIL, run.accounts[0].reason)

        def snapshot_fails():
            raise TimeoutError(f"snapshot timed out for {SYNTHETIC_EMAIL}")

        self.dir = Path(self.tmp.name) / "second"
        self.games = {}
        run = self.orchestrator(profile("alpha"), profile("bravo"),
                                factory=self.snapshot_hook({"alpha": snapshot_fails}, FIRST_SNAPSHOT)).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["error", "completed"])
        self.assertIn("TimeoutError", run.accounts[0].reason)
        self.assertNotIn(SYNTHETIC_EMAIL, run.accounts[0].reason)
        self.assertEqual(self.games["alpha"].calls, [])

    def test_an_unexpected_worker_exception_is_contained(self):
        class Broken(ConcurrentFleetOrchestrator):
            def _run_scheduler(self, profile, *args):
                if profile.alias == "alpha":
                    raise RuntimeError(f"worker crashed for {SYNTHETIC_EMAIL}")
                return super()._run_scheduler(profile, *args)

        run = self.orchestrator(profile("alpha"), profile("bravo"), cls=Broken).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["error", "completed"])
        self.assertIn("RuntimeError", run.accounts[0].reason)
        self.assertNotIn(SYNTHETIC_EMAIL, run.accounts[0].reason)

    def test_foreign_snapshot_is_refused_without_calls(self):
        self.games["alpha"] = FakeGame("alpha", self.clock)
        self.games["alpha"].snapshot_alias = "bravo"
        run = self.orchestrator(profile("alpha"), profile("bravo")).run(execute=True)
        self.assertEqual(run.accounts[0].status, "error")
        self.assertIn("snapshot belongs to 'bravo'", run.accounts[0].reason)
        self.assertEqual(self.games["alpha"].calls, [])
        self.assertEqual(run.accounts[1].status, "completed")

    def test_shared_journal_goes_to_the_first_alias_whatever_the_factory_order(self):
        # bravo's factory returns first, yet alpha (first in alias order) owns the journal,
        # and bravo is refused before its first snapshot.
        bravo_opened = threading.Event()
        snapshots = []

        def factory(profile):
            if profile.alias == "alpha":
                self.assertTrue(bravo_opened.wait(WAIT))
            game = self.game(profile.alias)

            def snapshot():
                snapshots.append(profile.alias)
                return game.snapshot()

            context = self.factory(profile, snapshot=snapshot, journal=self.dir / "shared.jsonl")
            if profile.alias == "bravo":
                bravo_opened.set()
            return context

        run = self.orchestrator(profile("alpha"), profile("bravo"), factory=factory,
                                max_workers=2).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["completed", "error"])
        self.assertIn("already used by alpha", run.accounts[1].reason)
        self.assertEqual(self.games["bravo"].calls, [])
        self.assertNotIn("bravo", snapshots)
        events = GameplayJournal(self.dir / "shared.jsonl").read()
        self.assertEqual({event.account_alias for event in events}, {"alpha"})

    def test_snapshot_claims_are_decided_on_the_coordinator_in_alias_order(self):
        # bravo's first snapshot completes before alpha's, yet the snapshot claim hook sees
        # alpha first, on the coordinating thread; a refused claim stops bravo before any call.
        bravo_snapped = threading.Event()
        decided = []
        coordinator = threading.get_ident()

        class Claims(FleetClaims):
            def claim_snapshot(self, alias, snapshot):
                decided.append((alias, threading.get_ident() == coordinator))
                return "identity already claimed" if alias == "bravo" else super().claim_snapshot(alias, snapshot)

        class Orchestrator(ConcurrentFleetOrchestrator):
            def _new_claims(self):
                return Claims()

        factory = self.snapshot_hook({"alpha": lambda: self.assertTrue(bravo_snapped.wait(WAIT)),
                                      "bravo": bravo_snapped.set}, FIRST_SNAPSHOT)
        run = self.orchestrator(profile("alpha"), profile("bravo"), factory=factory, cls=Orchestrator,
                                max_workers=2).run(execute=True)
        self.assertEqual(decided, [("alpha", True), ("bravo", True)])
        self.assertEqual([a.status for a in run.accounts], ["completed", "error"])
        self.assertEqual(run.accounts[1].reason, "identity already claimed")
        self.assertEqual(self.games["bravo"].calls, [])

    def test_fleet_stop_file_present_at_start_runs_nothing(self):
        stop = self.dir / "STOP"
        stop.write_text("stop")
        run = self.orchestrator(profile("alpha"), profile("bravo"), stop_file=stop).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["not-run", "not-run"])
        self.assertIn("stop file", run.stop_reason)
        self.assertEqual(self.games, {})

    def test_fleet_stop_file_stops_accounts_not_yet_started(self):
        # One worker: the stop file appears during alpha's scheduler run. alpha's scheduler
        # stops between actions; bravo and charlie are never opened.
        stop = self.dir / "STOP"
        factory = self.snapshot_hook({"alpha": lambda: stop.write_text("stop")}, PLANNING_SNAPSHOT)
        run = self.orchestrator(profile("alpha"), profile("bravo"), profile("charlie"), factory=factory,
                                stop_file=stop, max_workers=1).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["stopped", "not-run", "not-run"])
        self.assertIn("stop file", run.accounts[0].reason)
        self.assertEqual(run.accounts[1].reason, run.stop_reason)
        self.assertIn("stop file", run.stop_reason)
        self.assertEqual(self.games["alpha"].calls, [])
        self.assertEqual(set(self.games), {"alpha"})

    def test_an_opened_account_stopped_before_scheduling_says_so(self):
        # The stop file appears during alpha's first snapshot: alpha was opened (its factory
        # may have logged in) but never scheduled; bravo was never opened.
        stop = self.dir / "STOP"
        factory = self.snapshot_hook({"alpha": lambda: stop.write_text("stop")}, FIRST_SNAPSHOT)
        run = self.orchestrator(profile("alpha"), profile("bravo"), factory=factory, stop_file=stop,
                                max_workers=1).run(execute=True)
        self.assertEqual([a.status for a in run.accounts], ["not-run", "not-run"])
        self.assertIn("opened but stopped before scheduling", run.accounts[0].reason)
        self.assertNotIn("opened", run.accounts[1].reason)
        self.assertEqual(self.games["alpha"].calls, [])
        self.assertNotIn("bravo", self.games)

    def test_budget_keeps_the_plan_within_remaining_spend(self):
        run = self.orchestrator(profile("alpha", budgets={"minerals": 200, "starbux": 0}),
                                profile("bravo")).run(execute=True)
        self.assertNotIn("upgradeRoom", self.games["alpha"].calls)
        self.assertIn("upgradeRoom", self.games["bravo"].calls)
        self.assertEqual(run.accounts[0].budget_remaining, {"minerals": 200, "starbux": 0})

    def test_elapsed_timing_is_injectable(self):
        ticks = itertools.count()
        lock = threading.Lock()

        def timer():
            with lock:
                return float(next(ticks))

        # One worker makes the timer call order fixed: alpha opens (0) and finishes (1),
        # then bravo opens (2) and finishes (3).
        run = self.orchestrator(profile("alpha"), profile("bravo"), timer=timer, max_workers=1).run()
        self.assertEqual([a.elapsed_seconds for a in run.accounts], [1.0, 1.0])


class FleetClaimTests(unittest.TestCase):
    def test_concurrent_claims_of_one_journal_have_one_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            claims = FleetClaims()
            aliases = [f"acct{i}" for i in range(8)]
            barrier = threading.Barrier(len(aliases), timeout=WAIT)
            refusals = {}

            def claim(alias):
                journal = GameplayJournal(Path(tmp) / "same.jsonl")
                barrier.wait()
                refusals[alias] = claims.claim_journal(alias, journal)

            threads = [threading.Thread(target=claim, args=(alias,)) for alias in aliases]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(WAIT)
            owners = [alias for alias, refusal in refusals.items() if refusal is None]
            self.assertEqual(len(refusals), len(aliases))
            self.assertEqual(len(owners), 1)
            for alias, refusal in refusals.items():
                if refusal is not None:
                    self.assertIn(f"already used by {owners[0]}", refusal)
            # The owner may claim its own journal again.
            self.assertIsNone(claims.claim_journal(owners[0], GameplayJournal(Path(tmp) / "same.jsonl")))


if __name__ == "__main__":
    unittest.main()
