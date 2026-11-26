"""Policy pins for the crew-training benchmark: its oracle and catalog tables equal the owner-approved sources.

The benchmark's oracles are independent literal copies of the policy in sdk/training/strategy.py, sdk/training/planner.py
and sdk/crew_leveling.py, so a mutant of the planner or the strategy cannot move them. These tests keep the copies in
step with the sources: a deliberate policy change updates both sides in the same change.

This module is deliberately not one of qa-eval's test channels (scripts/training_benchmark.py QA_TEST_MODULES): it
compares tables literally, so every change to strategy data would count as "caught by the tests" whatever it does.
"""

from __future__ import annotations

import unittest

import sdk.crew_leveling as crew_leveling
from scripts import training_benchmark as bench
from sdk.training.strategy import DEFAULT_ROLES, DEFAULT_STRATEGY, RARITY_STARS, STAT_CODES


class OraclePolicyPinTests(unittest.TestCase):
    def test_cap_tables(self):
        self.assertEqual(bench.ORACLE_TP_CAPS, dict(crew_leveling.TP_CAPS_BY_RARITY))
        self.assertEqual(bench.ORACLE_TP_CAPS, dict(DEFAULT_STRATEGY.tp_caps))
        exceptional = {(4, name): crew_leveling.EXCEPTIONAL_4STAR_TP for name in crew_leveling.EXCEPTIONAL_4STAR_NAMES}
        self.assertEqual(bench.ORACLE_EXCEPTIONAL_CAPS, exceptional)
        self.assertEqual(bench.ORACLE_EXCEPTIONAL_CAPS, dict(DEFAULT_STRATEGY.exceptional_tp_caps))
        self.assertEqual(bench.ORACLE_CAPTAIN_CAPS, {6: crew_leveling.CAPTAIN_TP})
        self.assertEqual(bench.ORACLE_CAPTAIN_CAPS, dict(DEFAULT_STRATEGY.captain_tp_caps))
        self.assertEqual(bench.ORACLE_RARITY_STARS, dict(RARITY_STARS))
        self.assertEqual(bench.ORACLE_RARITY_STARS, dict(DEFAULT_STRATEGY.rarity_stars))

    def test_bands_gates_fatigue_and_consumable_rules(self):
        bands = tuple((band.low, band.high, band.slot, band.tier) for band in DEFAULT_STRATEGY.bands)
        self.assertEqual(bench.ORACLE_BANDS, bands)
        self.assertEqual(bench.ORACLE_COMPLETE_PERCENT, DEFAULT_STRATEGY.complete_percent)
        self.assertEqual(bench.ORACLE_GATES, dict(DEFAULT_STRATEGY.research_gates))
        self.assertEqual(bench.ORACLE_MAX_TRAINING_FATIGUE, DEFAULT_STRATEGY.max_training_fatigue)
        rules = {
            rule.tier: (rule.min_percent, rule.max_percent, rule.min_fatigue, rule.max_fatigue)
            for rule in DEFAULT_STRATEGY.consumable_rules
        }
        self.assertEqual(bench.ORACLE_CONSUMABLE_RULES, rules)

    def test_roles_and_their_trainings(self):
        roles = {name: role.name for role in DEFAULT_ROLES for name in role.characters}
        self.assertEqual(bench.ORACLE_ROLES, roles)
        slots = {
            role.name: {"primary": (role.primary.room_type, role.primary.trainings),
                        "secondary": (role.secondary.room_type, role.secondary.trainings)}
            for role in DEFAULT_ROLES
            if role.characters
        }
        self.assertEqual(bench.ORACLE_ROLE_SLOTS, slots)
        training_rooms = {name: kind for name, (_, kind, _) in bench.CATALOG_ROOMS.items() if kind is not None}
        self.assertEqual(dict(DEFAULT_STRATEGY.room_types), training_rooms)

    def test_catalog_fits_the_policy(self):
        for role in DEFAULT_ROLES:
            for slot in (role.primary, role.secondary):
                stats = set()
                for tier, name in enumerate(slot.trainings, start=1):
                    _, stat, room, catalog_tier = bench.CATALOG_TRAININGS[name]
                    stats.add(stat)
                    self.assertEqual(bench.CATALOG_ROOMS[room][1], slot.room_type, name)
                    self.assertEqual(catalog_tier, tier, name)
                self.assertEqual(len(stats), 1, (role.name, slot))
        codes = set(STAT_CODES.values())
        item_ids = {entry[0] for entry in bench.CATALOG_ITEMS.values()}
        for (code, tier), item_id in bench.CATALOG_CONSUMABLES.items():
            self.assertIn(code, codes)
            self.assertIn(tier, bench.ORACLE_CONSUMABLE_RULES)
            self.assertIn(item_id, item_ids)
        levels = {(family, level) for family in ("Fitness", "Education") for level in (1, 2, 3)}
        self.assertEqual(set(bench.CATALOG_RESEARCH_LEVELS.values()), levels)

    def test_policy_pins_are_not_a_qa_channel(self):
        self.assertNotIn("tests.test_training_benchmark_policy", bench.QA_TEST_MODULES)


if __name__ == "__main__":
    unittest.main()
