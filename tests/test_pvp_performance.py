import unittest
from sdk.pvp.models import (
    BattleResult, BattleOutcome, BattleRole, SuspectedLossCause,
)
from sdk.pvp.performance import build_performance_report, attack_win_rate, defense_win_rate, losses_by_cause


class TestPvpPerformance(unittest.TestCase):
    def setUp(self):
        self.battles = (
            BattleResult(battle_id="1", account_pseudonym="acctA", opponent_hull_level=10, role=BattleRole.ATTACKER.value,
                         outcome=BattleOutcome.WIN.value, trophy_before=5000, trophy_after=5020, trophy_delta=20,
                         battle_duration_s=120, major_rooms_lost=(), battle_strategy="focus_fire",                         suspected_loss_cause=SuspectedLossCause.UNKNOWN.value,
                         ship_config_version="v1", created_at="2026-01-01"),
            BattleResult(battle_id="2", account_pseudonym="acctA", opponent_hull_level=12, role=BattleRole.ATTACKER.value,
                         outcome=BattleOutcome.LOSS.value, trophy_before=5020, trophy_after=5000, trophy_delta=-20,
                         battle_duration_s=90, major_rooms_lost=("reactor",), battle_strategy="focus_fire",
                         suspected_loss_cause=SuspectedLossCause.POWER_LOSS.value, ship_config_version="v1",
                         created_at="2026-01-02"),
            BattleResult(battle_id="3", account_pseudonym="acctA", role=BattleRole.DEFENDER.value,
                         outcome=BattleOutcome.WIN.value, trophy_before=5000, trophy_after=5010, trophy_delta=10,
                         battle_duration_s=60, ship_config_version="v2", created_at="2026-01-03"),
        )

    def test_attack_win_rate(self):
        self.assertEqual(attack_win_rate(self.battles, "acctA"), 0.5)

    def test_defense_win_rate(self):
        self.assertEqual(defense_win_rate(self.battles, "acctA"), 1.0)

    def test_losses_by_cause(self):
        self.assertEqual(losses_by_cause(self.battles, "acctA"), {SuspectedLossCause.POWER_LOSS.value: 1})

    def test_report(self):
        rep = build_performance_report(self.battles, "acctA")
        self.assertEqual(rep.battles_considered, 3)
        self.assertEqual(rep.attack_battles, 2)
        self.assertEqual(rep.defense_battles, 1)
        self.assertIn("suspected", " ".join(rep.notes).lower() or "suspected")

    def test_isolation(self):
        self.assertIsNone(attack_win_rate(self.battles, "other"))

    def test_unknowns_recorded(self):
        # opponent hull unknown on defense battle
        rep = build_performance_report(self.battles, "acctA")
        self.assertTrue(any("opponent hull" in u for u in rep.unknowns))

    def test_deterministic(self):
        r1 = build_performance_report(self.battles, "acctA")
        r2 = build_performance_report(self.battles, "acctA")
        self.assertEqual(r1, r2)


if __name__ == "__main__":
    unittest.main()