"""Tests for sdk.reports.battles: BattleResult records from journaled battles. Offline."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.execution import ActionRunner, UncertainPriorAttemptError
from sdk.journal import GameplayEvent, GameplayJournal
from sdk.pvp import ExperimentSchedule, ExperimentWindow, build_performance_report, candidate_registry
from sdk.reports import battles_from_events, hull_bracket, trophy_bracket
from sdk.state import build_snapshot
from tests import test_pvp_battle_action as battle_action  # module import, so its tests are not collected twice

T0 =datetime(2027, 1, 4, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def event(minutes=0, outcome="verified", tier="outcome-observed", trophies=12, alias="alpha", attempted=True,
          dry_run=False, battle_id="4028975", action_id="run_pvp_battle", error_class=None, tz=timezone.utc,
          **evidence):
    start = (T0 + timedelta(minutes=minutes)).astimezone(tz)
    payload = {"evidence_tier": tier, "battle_id": battle_id, "trophies_delta": trophies, **evidence}
    return GameplayEvent.create(
        account_alias=alias, action_id=action_id, started_at=start, finished_at=start + timedelta(seconds=40),
        outcome=outcome if attempted else "skipped", reason="synthetic", attempted=attempted, dry_run=dry_run,
        evidence=payload if attempted else {}, error_class=error_class,
    )


def schedule():
    return ExperimentSchedule(candidate_registry(), [
        ExperimentWindow("alpha", "weapon-focus-fire", 1, T0 - DAY, T0 + DAY),
        ExperimentWindow("alpha", "reactor-shield-devastation", 1, T0 + DAY, T0 + 2 * DAY),
    ])


class BattlesFromEventsTests(unittest.TestCase):
    def test_observed_trophy_change_gives_win_or_loss(self):
        win, loss = battles_from_events([event(trophies=12), event(minutes=5, trophies=-9, battle_id="2")])
        self.assertEqual((win.result.outcome, win.result.trophy_delta, win.trophy_delta), ("win", 12, 12))
        self.assertEqual((loss.result.outcome, loss.result.trophy_delta), ("loss", -9))
        self.assertEqual(win.result.role, "attacker")
        self.assertEqual(win.result.account_pseudonym, "alpha")

    def test_anything_short_of_observed_outcome_is_unknown(self):
        cases = [
            event(outcome="unknown", tier="finalised", trophies=None),
            event(outcome="not-observed", tier="finalised", trophies=0),
            event(outcome="verified", tier="finalised", trophies=12),  # inconsistent evidence stays unknown
            event(outcome="verified", tier="outcome-observed", trophies=None),
            event(outcome="verified", tier="outcome-observed", trophies=12, error_class="ConnectionError"),
        ]
        for battle in battles_from_events(cases):
            with self.subTest(tier=battle.evidence_tier, delta=battle.trophy_delta):
                self.assertEqual(battle.result.outcome, "unknown")
                self.assertFalse(battle.outcome_known)
                self.assertEqual(battle.result.trophy_delta, battle.trophy_delta or 0)

    def test_skipped_dry_run_and_other_actions_are_not_battles(self):
        events = [event(attempted=False), event(attempted=False, dry_run=True), event(action_id="collect_all_resources")]
        self.assertEqual(battles_from_events(events), [])

    def test_strategy_comes_from_the_window_at_battle_start(self):
        sched = schedule()
        first, second, outside = battles_from_events(
            [event(), event(minutes=24 * 60 + 1), event(minutes=-3 * 24 * 60)], sched)
        self.assertEqual(first.strategy_key, "weapon-focus-fire@v1")
        self.assertEqual(first.result.battle_strategy, "focus_fire")
        self.assertEqual(first.result.ship_config_version, first.strategy_config_version)
        self.assertEqual(second.strategy_key, "reactor-shield-devastation@v1")
        self.assertIsNone(outside.strategy_key)
        self.assertEqual(outside.result.battle_strategy, "unknown")

    def test_non_utc_journal_times_are_normalised_for_the_schedule(self):
        eastern = timezone(timedelta(hours=-5))
        # 08:00-05:00 on day 2 is 13:00Z, an hour into the second window (wall-clock 08:00 would be the first).
        first, second = battles_from_events(
            [event(tz=eastern), event(minutes=24 * 60 + 60, tz=eastern)], schedule())
        self.assertTrue(first.started_at.endswith("-05:00"))
        self.assertEqual(first.strategy_key, "weapon-focus-fire@v1")
        self.assertEqual(second.strategy_key, "reactor-shield-devastation@v1")

    def test_missing_battle_id_falls_back_to_attempt(self):
        [battle] = battles_from_events([event(battle_id=None)])
        self.assertEqual(battle.result.battle_id, f"attempt-{battle.attempt_id}")

    def test_cohorts_are_unknown_unless_recorded(self):
        plain, known = battles_from_events([event(), event(opponent_hull_level=9, opponent_trophies=1720)])
        self.assertEqual((plain.opponent_hull_bracket, plain.opponent_trophy_bracket), ("unknown", "unknown"))
        self.assertEqual((known.opponent_hull_bracket, known.opponent_trophy_bracket), ("hull 8-10", "trophies 1500-1999"))
        self.assertEqual(known.result.opponent_hull_level, 9)

    def test_bracket_helpers(self):
        self.assertEqual(hull_bracket(None), "unknown")
        self.assertEqual(hull_bracket(1), "hull 1-4")
        self.assertEqual(hull_bracket(14), "hull 14-20")
        self.assertEqual(hull_bracket(25), "unknown")
        self.assertEqual(trophy_bracket(0), "trophies 0-499")
        self.assertEqual(trophy_bracket(-5), "unknown")

    def test_results_feed_the_existing_performance_report(self):
        battles = battles_from_events([event(trophies=12), event(minutes=5, trophies=-9, battle_id="2"),
                                       event(minutes=9, outcome="unknown", tier="finalised", trophies=None,
                                             battle_id="3")])
        report = build_performance_report([b.result for b in battles], "alpha")
        self.assertEqual((report.battles_considered, report.attack_battles), (3, 3))


class RunnerToResultsTests(unittest.TestCase):
    """End to end: the runner journals a battle, and the journal yields a battle result."""

    def test_runner_journal_round_trip(self):
        class Client:
            def runBattleEndToEnd(self):
                self.createBattle9Result = {"ok": 1}
                self.acceptBattle5Result = {"ok": 1}
                self.finaliseBattle15Result = {"ok": 1}
                self.lastBattleId = "777"
                return True

        snaps = iter([
            build_snapshot("alpha", T0, "synthetic", profile={"@Trophy": "1500"}, ship={"@ShipLevel": "8"}),
            build_snapshot("alpha", T0 + timedelta(minutes=2), "synthetic", profile={"@Trophy": "1514"},
                           ship={"@ShipLevel": "8"}),
        ])
        ticks = iter(T0 + timedelta(seconds=n) for n in range(100))
        with tempfile.TemporaryDirectory() as tmp:
            journal = GameplayJournal(Path(tmp) / "events.jsonl")
            ActionRunner("alpha", Client(), lambda: next(snaps), journal, clock=lambda: next(ticks)).run("run_pvp_battle")
            [battle] = battles_from_events(journal.read(), schedule())
        self.assertEqual(battle.result.battle_id, "777")
        self.assertEqual((battle.result.outcome, battle.trophy_delta), ("win", 14))
        self.assertEqual(battle.evidence_tier, "outcome-observed")
        self.assertEqual(battle.strategy_key, "weapon-focus-fire@v1")

    def test_trophy_change_without_a_battle_step_is_not_a_win(self):
        class Client:
            def runBattleEndToEnd(self):
                raise ConnectionError("synthetic: down before CreateBattle9")  # a defense adds trophies meanwhile

        snaps = iter([
            build_snapshot("alpha", T0, "synthetic", profile={"@Trophy": "1000"}, ship={"@ShipLevel": "5"}),
            build_snapshot("alpha", T0 + timedelta(minutes=2), "synthetic", profile={"@Trophy": "1012"},
                           ship={"@ShipLevel": "5"}),
        ])
        ticks = iter(T0 + timedelta(seconds=n) for n in range(100))
        with tempfile.TemporaryDirectory() as tmp:
            journal = GameplayJournal(Path(tmp) / "events.jsonl")
            ActionRunner("alpha", Client(), lambda: next(snaps), journal, clock=lambda: next(ticks)).run("run_pvp_battle")
            [event_] = journal.read()
            [battle] = battles_from_events([event_])
        self.assertEqual((event_.outcome, event_.error_class), ("unknown", "ConnectionError"))
        self.assertEqual((battle.result.outcome, battle.evidence_tier, battle.trophy_delta),
                         ("unknown", "request-sent", 12))


class UnsupportedOutcomeRunnerTests(unittest.TestCase):
    """A trophy change is this call's battle outcome only if the call reached FinaliseBattle15."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = GameplayJournal(Path(self.tmp.name) / "events.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def run_battle(self, client, snapshots, **kwargs):
        feed = iter(snapshots)
        runner = ActionRunner("alpha", client, lambda: next(feed), self.journal, clock=battle_action.FakeClock())
        return runner.run("run_pvp_battle", **kwargs)

    def test_trophy_change_before_battle_created_is_unknown_not_verified(self):
        # The call fails before CreateBattle9 while a defense adds trophies in the same interval.
        snap = battle_action.snap
        client = battle_action.FakeBattleClient(steps=(), raise_error=ConnectionError("synthetic: down"))
        result = self.run_battle(client, [snap(1000), snap(1012)])
        evidence = result.event.evidence
        self.assertEqual((result.outcome, result.event.error_class), ("unknown", "ConnectionError"))
        self.assertEqual(evidence["evidence_tier"], "request-sent")
        self.assertEqual(evidence["steps"], {"battle-created": False, "battle-accepted": False,
                                             "finalised": False, "outcome-observed": False})
        self.assertFalse(evidence["outcome_supported"])
        self.assertEqual(evidence["trophies_delta"], 12)
        self.assertIn("call raised ConnectionError", result.reason)
        self.assertIn("did not reach finalised", result.reason)
        with self.assertRaises(UncertainPriorAttemptError):
            self.run_battle(battle_action.FakeBattleClient(), [snap(1012), snap(1012)])

    def test_trophy_change_without_finalise_is_unknown(self):
        snap = battle_action.snap
        for steps in (("create",), ("create", "accept")):
            with self.subTest(steps=steps):
                result = self.run_battle(battle_action.FakeBattleClient(steps=steps), [snap(1500), snap(1512)],
                                         allow_after_uncertain=True)
                self.assertEqual((result.outcome, result.event.error_class), ("unknown", None))
                self.assertNotEqual(result.event.evidence["evidence_tier"], "outcome-observed")
                self.assertFalse(result.event.evidence["steps"]["outcome-observed"])
                self.assertFalse(result.event.evidence["outcome_supported"])
                [battle] = battles_from_events([result.event])
                self.assertEqual(battle.result.outcome, "unknown")

    def test_finalised_trophy_change_is_supported_and_verified(self):
        snap = battle_action.snap
        result = self.run_battle(battle_action.FakeBattleClient(), [snap(1500), snap(1512)])
        self.assertEqual(result.outcome, "verified")
        self.assertTrue(result.event.evidence["outcome_supported"])
        self.assertNotIn("outcome_support_reason", result.event.evidence)

    def test_other_actions_are_not_downgraded(self):
        class Client:
            def collectAllResources(self):
                return True

        resources = ({"minerals": "100", "gas": "100", "starbux": "0"}, {"minerals": "150", "gas": "100", "starbux": "0"})
        feed = iter([build_snapshot("alpha", T0, "synthetic", resources=r) for r in resources])
        runner = ActionRunner("alpha", Client(), lambda: next(feed), self.journal, clock=battle_action.FakeClock())
        result = runner.run("collect_all_resources")
        self.assertEqual((result.outcome, result.event.evidence), ("verified", {}))


if __name__ == "__main__":
    unittest.main()
