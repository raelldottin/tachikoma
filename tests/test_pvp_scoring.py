import unittest
from sdk.pvp.models import (
    AccountProgressionProfile, CrewMember, CombatRoom, ResearchItem, NativeAIRule,
)
from sdk.pvp.scoring import (
    assess_account, hull_upgrade_readiness, crew_training_priorities, combat_room_upgrade_priorities, armor_power_repair_coverage,
    native_weapon_targeting, boarding_anti_boarding, research_priorities,
)
from sdk.pvp.config import crew_level_cap


class TestPvpScoring(unittest.TestCase):
    def setUp(self):
        self.profile = AccountProgressionProfile(
            pseudonym="acctA", hull_level=10, trophies=5000,
            crew=(
                CrewMember(name="gunner", level=30, rarity=5, training_points=10, role="weapon_boost"),
                CrewMember(name="eng", level=40, rarity=6, training_points=80, role="repair"),
            ),
            rooms=(
                CombatRoom(design_id="r1", name="Reactor", category="reactor", level=2, max_level=5, power=10, hp=100),
                CombatRoom(design_id="w1", name="Laser", category="weapon", level=5, max_level=5),
            ),
            research=(ResearchItem(name="Python 1", level=0, max_level=1, completed=False),),
            armor_coverage=50, power_balance=80, repair_proximity=40, weapon_coverage=70,
            native_ai_rules=(NativeAIRule(group="weapon", conditions=("enemy present",), actions=("target lasers",)),),
            resources={"minerals": 1000},
        )

    def test_assess_account_keys(self):
        a = assess_account(self.profile)
        self.assertEqual(a.profile_pseudonym, "acctA")
        self.assertIn("crew_role_assignment", a.scores)
        self.assertIsNotNone(a.hull_upgrade_readiness)

    def test_hull_readiness_not_ready_due_to_rooms(self):
        r = hull_upgrade_readiness(self.profile)
        self.assertFalse(r.ready)
        self.assertTrue(len(r.blockers) >= 1)

    def test_crew_training_priorities(self):
        items = crew_training_priorities(self.profile)
        self.assertTrue(len(items) >= 1)
        # deterministic
        self.assertEqual(crew_training_priorities(self.profile), items)

    def test_room_upgrade_priorities(self):
        items = combat_room_upgrade_priorities(self.profile)
        self.assertTrue(any("Reactor" in i.title for i in items))

    def test_armor_score(self):
        s = armor_power_repair_coverage(self.profile)
        self.assertIsNotNone(s.score)

    def test_native_weapon_targeting(self):
        s = native_weapon_targeting(self.profile)
        self.assertIsNotNone(s.score)

    def test_boarding_score(self):
        s = boarding_anti_boarding(self.profile)
        # rooms lack security; score maybe present
        self.assertTrue(s.score is None or 0 <= s.score <= 100)

    def test_research_priorities(self):
        items = research_priorities(self.profile)
        self.assertTrue(any("Python 1" in i.title for i in items))

    def test_crew_level_cap(self):
        self.assertEqual(crew_level_cap(10), 40)

    def test_unknown_profile(self):
        p = AccountProgressionProfile(pseudonym="u", hull_level=5)
        a = assess_account(p)
        self.assertTrue(len(a.unknowns) >= 0)
        r = hull_upgrade_readiness(p)
        self.assertFalse(r.ready)


if __name__ == "__main__":
    unittest.main()