"""Tests for sdk.pvp.strategies: versioned strategies, experiment windows and adoption log. Offline."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.pvp import (
    CANDIDATE_STRATEGIES,
    BattleStrategy,
    ExperimentSchedule,
    ExperimentWindow,
    NativeAIRule,
    PvpInputError,
    PvpStrategy,
    StrategyRegistry,
    candidate_registry,
    strategy_index,
)
from sdk.pvp.strategies import AdoptionLog, AdoptionMethod, StrategyAdoption, TrialRecord
from tests.synthetic_fixtures import SYNTHETIC_ACCESS_TOKEN, SYNTHETIC_EMAIL

T0 = datetime(2027, 1, 4, 0, 0, tzinfo=timezone.utc)
WEEK = timedelta(days=7)


def strategy(**overrides):
    base = dict(
        strategy_id="test-strategy",
        version=1,
        description="synthetic",
        battle_strategy=BattleStrategy.FOCUS_FIRE.value,
        native_ai_rules=(NativeAIRule(group="weapons", conditions=("always",), actions=("target_room",),
                                      target_type="weapon"),),
        crew_roles=(("gunner", "weapon"),),
        power_priorities=("shield", "weapon"),
        target_priorities=("weapon",),
    )
    base.update(overrides)
    return PvpStrategy(**base)


class PvpStrategyTests(unittest.TestCase):
    def test_config_version_is_deterministic_and_content_based(self):
        a, b = strategy(), strategy()
        self.assertEqual(a.config_version, b.config_version)
        self.assertEqual(len(a.config_version), 16)
        self.assertNotEqual(a.config_version, strategy(target_priorities=("shield",)).config_version)

    def test_description_and_version_do_not_change_config_version(self):
        a = strategy()
        self.assertEqual(a.config_version, strategy(description="reworded", version=2).config_version)

    def test_validation_fails_closed(self):
        bad = [
            dict(strategy_id="Has Spaces"),
            dict(strategy_id=SYNTHETIC_EMAIL),
            dict(version=0),
            dict(version=True),
            dict(battle_strategy="nuke"),
            dict(crew_roles=(("gunner",),)),
            dict(crew_roles=(("gunner", "weapon"), ("gunner", "shield"))),
            dict(power_priorities=("shield", "shield")),
            dict(native_ai_rules=("not-a-rule",)),
            dict(evidence_tier="maybe"),
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(PvpInputError):
                strategy(**overrides)

    def test_secret_in_description_is_refused(self):
        with self.assertRaises(PvpInputError):
            strategy(description=f"accessToken={SYNTHETIC_ACCESS_TOKEN}")

    def test_to_dict_is_json_and_carries_key(self):
        payload = json.loads(json.dumps(strategy().to_dict()))
        self.assertEqual(payload["key"], "test-strategy@v1")
        self.assertEqual(payload["config_version"], strategy().config_version)
        self.assertEqual(payload["targets"], ["weapon"])


class StrategyRegistryTests(unittest.TestCase):
    def test_published_version_cannot_change(self):
        registry = StrategyRegistry([strategy()])
        registry.register(strategy(description="same behaviour"))  # identical content is accepted
        with self.assertRaises(PvpInputError):
            registry.register(strategy(target_priorities=("reactor",)))
        registry.register(strategy(version=2, target_priorities=("reactor",)))
        self.assertEqual(registry.latest("test-strategy").version, 2)
        self.assertEqual(registry.get("test-strategy", 1).target_priorities, ("weapon",))
        self.assertIsNone(registry.get("test-strategy", 3))
        self.assertIsNone(registry.latest("missing"))

    def test_candidates_are_two_distinct_strategies(self):
        registry = candidate_registry()
        self.assertEqual(len(registry.all()), 2)
        versions = {s.config_version for s in CANDIDATE_STRATEGIES}
        self.assertEqual(len(versions), 2)
        self.assertEqual(set(strategy_index(CANDIDATE_STRATEGIES)), versions)
        for candidate in CANDIDATE_STRATEGIES:
            self.assertTrue(candidate.description.startswith("Hypothesis"))


class ExperimentScheduleTests(unittest.TestCase):
    def setUp(self):
        self.registry = candidate_registry()
        self.schedule = ExperimentSchedule(self.registry, [
            ExperimentWindow("alpha", "weapon-focus-fire", 1, T0, T0 + WEEK),
            ExperimentWindow("alpha", "reactor-shield-devastation", 1, T0 + WEEK, T0 + 2 * WEEK),
            ExperimentWindow("bravo", "reactor-shield-devastation", 1, T0, T0 + WEEK),
        ])

    def test_active_strategy_uses_half_open_windows(self):
        self.assertEqual(self.schedule.active_strategy("alpha", T0).strategy_id, "weapon-focus-fire")
        self.assertEqual(self.schedule.active_strategy("alpha", T0 + WEEK).strategy_id, "reactor-shield-devastation")
        self.assertIsNone(self.schedule.active_strategy("alpha", T0 + 2 * WEEK))
        self.assertIsNone(self.schedule.active_strategy("alpha", T0 - timedelta(seconds=1)))
        self.assertIsNone(self.schedule.active_strategy("charlie", T0))

    def test_accounts_are_isolated(self):
        self.assertEqual(self.schedule.active_strategy("bravo", T0).strategy_id, "reactor-shield-devastation")
        self.assertEqual(len(self.schedule.windows_for("alpha")), 2)
        self.assertEqual(len(self.schedule.windows_for("bravo")), 1)

    def test_overlapping_window_for_same_alias_is_refused(self):
        with self.assertRaises(PvpInputError):
            self.schedule.add(ExperimentWindow("alpha", "weapon-focus-fire", 1, T0 + timedelta(days=6),
                                               T0 + timedelta(days=8)))
        self.schedule.add(ExperimentWindow("charlie", "weapon-focus-fire", 1, T0, T0 + WEEK))

    def test_unregistered_strategy_is_refused(self):
        with self.assertRaises(PvpInputError):
            self.schedule.add(ExperimentWindow("charlie", "weapon-focus-fire", 9, T0, T0 + WEEK))

    def test_window_validation(self):
        naive = datetime(2027, 1, 4)
        bad = [
            dict(account_alias=SYNTHETIC_EMAIL),
            dict(strategy_id="Bad Id"),
            dict(start=naive),
            dict(end=T0),
            dict(start=T0.astimezone(timezone(timedelta(hours=2)))),
        ]
        good = ExperimentWindow("alpha", "weapon-focus-fire", 1, T0, T0 + WEEK)
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(PvpInputError):
                replace(good, **overrides)

    def test_lookup_requires_utc(self):
        with self.assertRaises(PvpInputError):
            self.schedule.active_strategy("alpha", datetime(2027, 1, 5))


FOCUS = CANDIDATE_STRATEGIES[0]
REACTOR = CANDIDATE_STRATEGIES[1]
HOUR = timedelta(hours=1)


def adoption(alias="alpha", chosen=FOCUS, at=T0 + HOUR, method=AdoptionMethod.BY_HAND.value, **overrides):
    base = dict(
        account_alias=alias,
        strategy_id=chosen.strategy_id,
        strategy_version=chosen.version,
        config_version=chosen.config_version,
        adopted_at=at,
        method=method,
    )
    base.update(overrides)
    return StrategyAdoption(**base)


class StrategyAdoptionTests(unittest.TestCase):
    # Verifies: S08-R1
    def test_validation_fails_closed(self):
        bad = [
            dict(alias=SYNTHETIC_EMAIL),
            dict(strategy_id="Bad Id"),
            dict(strategy_version=0),
            dict(strategy_version=True),
            dict(config_version="not-a-hash"),
            dict(at=datetime(2027, 1, 4, 1)),
            dict(at=(T0 + HOUR).astimezone(timezone(timedelta(hours=-5)))),
            dict(method="bot"),
            dict(method="automatic"),
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(PvpInputError):
                adoption(**overrides)

    # Verifies: S08-R1
    def test_secret_shaped_note_is_refused(self):
        self.assertEqual(adoption(note="set in the ship AI screen").note, "set in the ship AI screen")
        for note in (f"accessToken={SYNTHETIC_ACCESS_TOKEN}", f"owner {SYNTHETIC_EMAIL}"):
            with self.subTest(note=note), self.assertRaises(PvpInputError):
                adoption(note=note)

    # Verifies: S08-R1, S08-R7
    def test_by_hand_timing_is_approximate_and_bot_call_is_not(self):
        self.assertTrue(adoption(method=AdoptionMethod.BY_HAND.value).approximate_timing)
        self.assertFalse(adoption(method=AdoptionMethod.BOT_CALL.value).approximate_timing)
        self.assertEqual(adoption().key, "weapon-focus-fire@v1")

    # Verifies: S08-R1
    def test_dict_round_trip_uses_utc_z(self):
        original = adoption(note="by hand after maintenance")
        payload = json.loads(json.dumps(original.to_dict()))
        self.assertEqual(payload["adopted_at"], "2027-01-04T01:00:00Z")
        self.assertEqual(StrategyAdoption.from_dict(payload), original)

    # Verifies: S08-R1
    def test_from_dict_refuses_secret_keys_missing_and_unknown_fields(self):
        good = adoption().to_dict()
        cases = [
            {**good, "accessToken": SYNTHETIC_ACCESS_TOKEN},
            {k: v for k, v in good.items() if k != "method"},
            {**good, "extra": 1},
            {**good, "adopted_at": "yesterday"},
            {**good, "adopted_at": "2027-01-04T01:00:00"},
            {**good, "adopted_at": 1767488400},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(PvpInputError):
                StrategyAdoption.from_dict(payload)
        with self.assertRaises(PvpInputError):
            StrategyAdoption.from_dict(["not", "a", "mapping"])


class AdoptionLogTests(unittest.TestCase):
    # Verifies: S08-R1
    def test_out_of_order_and_overlapping_entries_are_refused_per_alias(self):
        log = AdoptionLog([adoption(at=T0 + HOUR)])
        with self.assertRaises(PvpInputError):
            log.add(adoption(at=T0))
        with self.assertRaises(PvpInputError):
            log.add(adoption(chosen=REACTOR, at=T0 + HOUR))
        log.add(adoption(alias="bravo", at=T0))  # other aliases are independent
        log.add(adoption(chosen=REACTOR, at=T0 + WEEK + HOUR))
        self.assertEqual(len(log.adoptions_for("alpha")), 2)
        self.assertEqual(log.aliases(), ("alpha", "bravo"))
        with self.assertRaises(PvpInputError):
            log.add("not-an-adoption")

    # Verifies: S08-R1
    def test_refused_entry_leaves_log_unchanged(self):
        log = AdoptionLog([adoption(at=T0 + HOUR)])
        before = log.to_dict()
        with self.assertRaises(PvpInputError):
            log.add(adoption(at=T0))
        self.assertEqual(log.to_dict(), before)

    # Verifies: S08-R1
    def test_latest_at_includes_the_adoption_instant(self):
        log = AdoptionLog([adoption(at=T0 + HOUR), adoption(chosen=REACTOR, at=T0 + 2 * HOUR)])
        self.assertIsNone(log.latest_at("alpha", T0 + HOUR - timedelta(seconds=1)))
        self.assertEqual(log.latest_at("alpha", T0 + HOUR).strategy_id, FOCUS.strategy_id)
        self.assertEqual(log.latest_at("alpha", T0 + 2 * HOUR).strategy_id, REACTOR.strategy_id)
        self.assertIsNone(log.latest_at("charlie", T0 + WEEK))
        with self.assertRaises(PvpInputError):
            log.latest_at("alpha", datetime(2027, 1, 5))

    # Verifies: S08-R1
    def test_adoptions_in_window(self):
        log = AdoptionLog([adoption(at=T0 - HOUR), adoption(at=T0 + HOUR), adoption(at=T0 + WEEK)])
        window = ExperimentWindow("alpha", FOCUS.strategy_id, FOCUS.version, T0, T0 + WEEK)
        self.assertEqual([a.adopted_at for a in log.adoptions_in(window)], [T0 + HOUR])

    # Verifies: S08-R1
    def test_json_file_round_trip(self):
        log = AdoptionLog([
            adoption(at=T0 + HOUR, method=AdoptionMethod.BOT_CALL.value),
            adoption(alias="bravo", chosen=REACTOR, at=T0 + HOUR, note="set by hand"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "applications.json"
            log.save(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            loaded = AdoptionLog.load(path)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["adoptions"]), 2)
        self.assertEqual(loaded.all(), log.all())

    # Verifies: S08-R1
    def test_loading_refuses_bad_files(self):
        good = adoption().to_dict()
        later = adoption(at=T0 + 2 * HOUR).to_dict()
        cases = [
            "not json",
            json.dumps([good]),
            json.dumps({"schema_version": 2, "adoptions": [good]}),
            json.dumps({"schema_version": 1, "adoptions": {"a": good}}),
            json.dumps({"schema_version": 1, "adoptions": [later, good]}),
            json.dumps({"schema_version": 1, "adoptions": [good, good]}),
        ]
        for text in cases:
            with self.subTest(text=text[:40]), self.assertRaises(PvpInputError):
                AdoptionLog.from_json(text)


class TrialRecordTests(unittest.TestCase):
    def setUp(self):
        self.schedule = ExperimentSchedule(candidate_registry(), [
            ExperimentWindow("alpha", FOCUS.strategy_id, 1, T0, T0 + WEEK),
            ExperimentWindow("alpha", REACTOR.strategy_id, 1, T0 + WEEK, T0 + 2 * WEEK),
            ExperimentWindow("bravo", REACTOR.strategy_id, 1, T0, T0 + WEEK),
        ])

    def record(self, *adoptions):
        return TrialRecord(self.schedule, AdoptionLog(adoptions))

    # Verifies: S08-R1
    def test_battles_are_attributed_only_after_an_adoption_inside_the_window(self):
        trial = self.record(
            adoption(at=T0 + HOUR, method=AdoptionMethod.BOT_CALL.value),
            adoption(chosen=REACTOR, at=T0 + WEEK + 3 * HOUR),
        )
        cases = [
            (T0 - HOUR, None),  # before any window
            (T0, None),  # window open, nothing adopted yet
            (T0 + HOUR, FOCUS),  # at the adoption instant
            (T0 + 3 * HOUR, FOCUS),
            (T0 + WEEK, None),  # next window, its strategy not yet adopted
            (T0 + WEEK + 2 * HOUR, None),
            (T0 + WEEK + 3 * HOUR, REACTOR),
            (T0 + 2 * WEEK, None),  # after every window
        ]
        for at, expected in cases:
            with self.subTest(at=at):
                self.assertEqual(trial.strategy_in_force("alpha", at), expected)

    # Verifies: S08-R1
    def test_adoption_before_the_window_does_not_carry_in(self):
        trial = self.record(adoption(at=T0 - HOUR))
        self.assertIsNone(trial.strategy_in_force("alpha", T0 + HOUR))

    # Verifies: S08-R1
    def test_adopting_a_different_strategy_inside_the_window_ends_attribution(self):
        trial = self.record(adoption(at=T0 + HOUR), adoption(chosen=REACTOR, at=T0 + 2 * HOUR))
        self.assertEqual(trial.strategy_in_force("alpha", T0 + HOUR), FOCUS)
        self.assertIsNone(trial.strategy_in_force("alpha", T0 + 2 * HOUR))

    # Verifies: S08-R1
    def test_accounts_never_mix(self):
        trial = self.record(adoption(alias="bravo", chosen=REACTOR, at=T0 + HOUR))
        self.assertEqual(trial.strategy_in_force("bravo", T0 + 2 * HOUR), REACTOR)
        self.assertIsNone(trial.strategy_in_force("alpha", T0 + 2 * HOUR))
        self.assertIsNone(trial.strategy_in_force("charlie", T0 + 2 * HOUR))

    # Verifies: S08-R1
    def test_unregistered_or_mismatched_adoptions_are_refused(self):
        with self.assertRaises(PvpInputError):
            self.record(adoption(strategy_version=9))
        with self.assertRaises(PvpInputError):
            self.record(adoption(config_version=REACTOR.config_version))

    # Verifies: S08-R1, S08-R7
    def test_attribution_reports_method_and_approximate_timing(self):
        trial = self.record(
            adoption(at=T0 + HOUR, method=AdoptionMethod.BY_HAND.value, note="set in game"),
            adoption(alias="bravo", chosen=REACTOR, at=T0 + HOUR, method=AdoptionMethod.BOT_CALL.value),
        )
        by_hand = trial.attribution_at("alpha", T0 + 2 * HOUR)
        self.assertEqual(by_hand.method, "by-hand")
        self.assertTrue(by_hand.approximate_timing)
        self.assertEqual(by_hand.window.strategy_id, FOCUS.strategy_id)
        self.assertEqual(by_hand.adoption.note, "set in game")
        bot = trial.attribution_at("bravo", T0 + 2 * HOUR)
        self.assertEqual(bot.method, "bot-call")
        self.assertFalse(bot.approximate_timing)
        self.assertIsNone(trial.attribution_at("alpha", T0))
        self.assertTrue(trial.has_approximate_timing())

    # Verifies: S08-R7
    def test_bot_call_only_trial_is_not_approximate(self):
        trial = self.record(adoption(at=T0 + HOUR, method=AdoptionMethod.BOT_CALL.value))
        self.assertFalse(trial.has_approximate_timing())

    # Verifies: S08-R1
    def test_lookup_requires_utc(self):
        trial = self.record(adoption())
        with self.assertRaises(PvpInputError):
            trial.strategy_in_force("alpha", datetime(2027, 1, 4, 2))


if __name__ == "__main__":
    unittest.main()
