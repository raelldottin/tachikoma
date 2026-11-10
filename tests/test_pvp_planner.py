import unittest
from sdk.pvp.models import AccountProgressionProfile
from sdk.pvp.planner import build_progression_roadmap


class TestPvpPlanner(unittest.TestCase):
    def test_roadmap(self):
        p = AccountProgressionProfile(pseudonym="acctA", hull_level=10, armor_coverage=30)
        rm = build_progression_roadmap(p)
        self.assertEqual(rm.account_pseudonym, "acctA")
        self.assertTrue(len(rm.immediate_fixes) >= 1)
        self.assertTrue(len(rm.next_7_days) >= 1)
        self.assertTrue(len(rm.next_30_days) >= 1)
        self.assertTrue(len(rm.hull_advance_criteria) >= 1)

    def test_deterministic(self):
        p = AccountProgressionProfile(pseudonym="acctA", hull_level=10)
        self.assertEqual(build_progression_roadmap(p), build_progression_roadmap(p))


if __name__ == "__main__":
    unittest.main()