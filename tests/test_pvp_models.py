import unittest
from sdk.pvp.models import (
    AccountProgressionProfile,
    BattleResult,
    CrewMember,
    CombatRoom,
    PvpInputError,
    reject_secret_keys,
    compute_ship_config_version,
    BattleOutcome,
    BattleRole,
)


class TestPvpModels(unittest.TestCase):
    def test_profile_minimal(self):
        p = AccountProgressionProfile(pseudonym="acctA", hull_level=10)
        self.assertEqual(p.pseudonym, "acctA")
        self.assertEqual(p.hull_level, 10)

    def test_profile_rejects_empty_pseudonym(self):
        with self.assertRaises(PvpInputError):
            AccountProgressionProfile(pseudonym="", hull_level=10)

    def test_profile_rejects_secret_keys(self):
        with self.assertRaises(PvpInputError):
            AccountProgressionProfile(pseudonym="a", hull_level=10, resources={"accessToken": "x"})

    def test_profile_isolates_accounts(self):
        b = BattleResult(battle_id="1", account_pseudonym="other", role=BattleRole.ATTACKER.value, outcome=BattleOutcome.WIN.value)
        with self.assertRaises(PvpInputError):
            AccountProgressionProfile(pseudonym="acctA", hull_level=10, recent_battles=(b,))

    def test_crew_validation(self):
        with self.assertRaises(PvpInputError):
            CrewMember(name="", level=1, rarity=3)
        with self.assertRaises(PvpInputError):
            CrewMember(name="x", level=41, rarity=3)
        with self.assertRaises(PvpInputError):
            CrewMember(name="x", level=1, rarity=0)

    def test_battle_trophy_consistency(self):
        with self.assertRaises(PvpInputError):
            BattleResult(battle_id="1", account_pseudonym="a", role=BattleRole.ATTACKER.value,
                        trophy_before=10, trophy_after=12, trophy_delta=5)

    def test_reject_secret_keys_nested(self):
        with self.assertRaises(PvpInputError):
            reject_secret_keys({"nested": {"password": "x"}})

    def test_ship_config_version_deterministic(self):
        p = AccountProgressionProfile(pseudonym="a", hull_level=10,
            crew=(CrewMember(name="c1", level=5, rarity=3, training_points=10),))
        v1 = compute_ship_config_version(p)
        v2 = compute_ship_config_version(p)
        self.assertEqual(v1, v2)
        self.assertEqual(len(v1), 16)

    def test_ship_config_version_changes_with_state(self):
        p1 = AccountProgressionProfile(pseudonym="a", hull_level=10)
        p2 = AccountProgressionProfile(pseudonym="a", hull_level=11)
        self.assertNotEqual(compute_ship_config_version(p1), compute_ship_config_version(p2))

    def test_room_validation(self):
        with self.assertRaises(PvpInputError):
            CombatRoom(design_id="1", name="r", level=5, max_level=3)

    def test_evidence_tier_validation(self):
        with self.assertRaises(PvpInputError):
            CrewMember(name="x", level=1, rarity=3, evidence_tier="BOGUS")

    def test_unknown_enum_validation(self):
        with self.assertRaises(PvpInputError):
            BattleResult(battle_id="1", account_pseudonym="a", role="bogus", outcome=BattleOutcome.WIN.value)


if __name__ == "__main__":
    unittest.main()