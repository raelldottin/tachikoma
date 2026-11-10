"""Tests for sdk.pvp.strategies: versioned strategies and experiment windows. Offline."""

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

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


if __name__ == "__main__":
    unittest.main()
