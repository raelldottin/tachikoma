"""Regression tests for sanitized PvP ship/battle inputs (no network)."""

from __future__ import annotations

import json
import os
import unittest

from sdk.pvp.models import (
    AccountProgressionProfile,
    BattleResult,
    CrewMember,
    PvpInputError,
    reject_secret_keys,
    compute_ship_config_version,
    BattleRole,
    BattleOutcome,
    SuspectedLossCause,
)
from sdk.ship_layout import parse_rooms, analyze_layout

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "pvp_sanitized")


def _load(name):
    with open(os.path.join(FIXTURE_DIR, name)) as f:
        return json.load(f)


class TestPvpSanitizedInputs(unittest.TestCase):
    def test_ship_fixture_loads_and_parses(self):
        data = _load("ship_layout_sanitized.json")
        rooms = parse_rooms(data["ship_data"], data["room_designs"])
        self.assertEqual(len(rooms), 4)
        cats = {r.category for r in rooms}
        self.assertIn("armor", cats)
        self.assertIn("weapon", cats)
        self.assertIn("reactor", cats)
        self.assertIn("repair", cats)

    def test_ship_fixture_analyzes_deterministically(self):
        data = _load("ship_layout_sanitized.json")
        rooms = parse_rooms(data["ship_data"], data["room_designs"])
        a1 = analyze_layout(rooms, ship_name=data["ship_name"],
                            ship_level=data["ship_level"],
                            ship_design_id=data["ship_design_id"])
        a2 = analyze_layout(rooms, ship_name=data["ship_name"],
                            ship_level=data["ship_level"],
                            ship_design_id=data["ship_design_id"])
        self.assertEqual(a1.defense_score, a2.defense_score)
        self.assertEqual(a1.total_rooms, 4)

    def test_battle_fixture_constructs(self):
        data = _load("battle_results_sanitized.json")
        self.assertEqual(len(data), 2)
        results = []
        for d in data:
            br = BattleResult(
                battle_id=d["battle_id"],
                account_pseudonym=d["account_pseudonym"],
                role=d["role"],
                opponent_pseudonym=d.get("opponent_pseudonym", ""),
                opponent_hull_level=d.get("opponent_hull_level"),
                outcome=d["outcome"],
                trophy_delta=d["trophy_delta"],
                battle_duration_s=d.get("battle_duration_s"),
                major_rooms_lost=tuple(d.get("major_rooms_lost", [])),
                battle_strategy=d["battle_strategy"],
                suspected_loss_cause=d["suspected_loss_cause"],
                ship_config_version=d["ship_config_version"],
                account_hull_level=d.get("account_hull_level"),
                trophy_before=d.get("trophy_before"),
                trophy_after=d.get("trophy_after"),
                notes=d.get("notes", ""),
                created_at=d.get("created_at", ""),
            )
            results.append(br)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].outcome, BattleOutcome.WIN.value)
        self.assertEqual(results[1].suspected_loss_cause, SuspectedLossCause.BOARDING.value)

    def test_profile_account_isolation(self):
        b = BattleResult(battle_id="x", account_pseudonym="other",
                         role=BattleRole.ATTACKER.value,
                         outcome=BattleOutcome.WIN.value)
        with self.assertRaises(PvpInputError):
            AccountProgressionProfile(pseudonym="acctA", hull_level=10,
                                     recent_battles=(b,))

    def test_secret_key_rejection(self):
        with self.assertRaises(PvpInputError):
            reject_secret_keys({"nested": {"accessToken": "secret"}})
        with self.assertRaises(PvpInputError):
            AccountProgressionProfile(pseudonym="a", hull_level=10,
                                     resources={"password": "x"})

    def test_ship_config_version_deterministic(self):
        p = AccountProgressionProfile(pseudonym="a", hull_level=10,
            crew=(CrewMember(name="c1", level=5, rarity=3, training_points=10),))
        v1 = compute_ship_config_version(p)
        v2 = compute_ship_config_version(p)
        self.assertEqual(v1, v2)
        self.assertEqual(len(v1), 16)

    def test_trophy_consistency(self):
        with self.assertRaises(PvpInputError):
            BattleResult(battle_id="1", account_pseudonym="a",
                         role=BattleRole.ATTACKER.value,
                         trophy_before=10, trophy_after=12, trophy_delta=5)

    def test_no_network_imports_in_pure_modules(self):
        import ast
        import pathlib
        pure = ["sdk/ship_layout.py", "sdk/game_formulas.py",
                "sdk/crew_leveling.py", "sdk/redaction.py"]
        for rel in pure:
            src = pathlib.Path(rel).read_text()
            tree = ast.parse(src)
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    name = getattr(node, "names", [])
                    for n in name:
                        imports.append(n.name)
                    if isinstance(node, ast.ImportFrom) and node.module:
                        imports.append(node.module)
            joined = " ".join(imports)
            for bad in ("requests", "urllib", "socket", "http"):
                self.assertNotIn(bad, joined, f"{rel} imports {bad}")


if __name__ == "__main__":
    unittest.main()
