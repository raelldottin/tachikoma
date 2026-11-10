"""End-to-end integration test for the offline PvP pipeline (no network)."""

from __future__ import annotations

import json
import os
import unittest

from sdk.pvp import (
    AccountProgressionProfile,
    CrewMember,
    CombatRoom,
    ResearchItem,
    NativeAIRule,
    BattleResult,
    assess_account,
    build_performance_report,
    attach_performance,
    build_progression_roadmap,
    render_account_report,
    render_performance_report,
    compute_ship_config_version,
)
from sdk.ship_layout import parse_rooms, analyze_layout

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "pvp_sanitized")


def _load(name):
    with open(os.path.join(FIXTURE_DIR, name)) as f:
        return json.load(f)


class TestPvpIntegrationPipeline(unittest.TestCase):
    def _build_profile(self):
        ship = _load("ship_layout_sanitized.json")
        rooms = parse_rooms(ship["ship_data"], ship["room_designs"])
        layout = analyze_layout(rooms, ship_name=ship["ship_name"],
                                ship_level=ship["ship_level"],
                                ship_design_id=ship["ship_design_id"])
        battles_raw = _load("battle_results_sanitized.json")
        battles = []
        for d in battles_raw:
            battles.append(BattleResult(
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
            ))
        profile = AccountProgressionProfile(
            pseudonym="acctA",
            hull_level=10,
            trophies=1200,
            crew=(
                CrewMember(name="gunner", level=30, rarity=5, training_points=40,
                           role="weapon_boost", assigned_room="laser"),
                CrewMember(name="engineer", level=28, rarity=4, training_points=20,
                           role="repair", assigned_room="repair"),
            ),
            rooms=tuple(
                CombatRoom(design_id=r.design_id, name=r.name, category=r.category,
                           level=0, power=r.power, hp=r.hp, max_level=None)
                for r in rooms
            ),
            research=(ResearchItem(name="Room Reinforcement", level=1, max_level=3),),
            armor_coverage=layout.armor_coverage,
            power_balance=layout.power_balance,
            repair_proximity=layout.repair_proximity,
            weapon_coverage=layout.weapon_coverage,
            native_ai_rules=(NativeAIRule(group="weapon", conditions=("enemy shield low",),
                                         actions=("focus fire",), target_type="shield"),),
            resources={"minerals": 1000},
            recent_battles=tuple(battles),
        )
        return profile, battles, layout

    def test_pipeline_deterministic(self):
        profile, battles, layout = self._build_profile()
        a1 = assess_account(profile)
        a2 = assess_account(profile)
        self.assertEqual(a1.profile_pseudonym, a2.profile_pseudonym)
        self.assertEqual(len(a1.scores), len(a2.scores))
        v1 = compute_ship_config_version(profile)
        v2 = compute_ship_config_version(profile)
        self.assertEqual(v1, v2)

    def test_performance_and_attach(self):
        profile, battles, layout = self._build_profile()
        perf = build_performance_report(battles, "acctA")
        self.assertEqual(perf.battles_considered, 2)
        self.assertIsNotNone(perf.attack_win_rate)
        assessment = assess_account(profile)
        attached = attach_performance(assessment, perf)
        self.assertIs(attached.battle_performance, perf)

    def test_roadmap_and_report(self):
        profile, battles, layout = self._build_profile()
        assessment = assess_account(profile)
        perf = build_performance_report(battles, "acctA")
        attached = attach_performance(assessment, perf)
        roadmap = build_progression_roadmap(profile)
        report = render_account_report(profile, attached, roadmap, perf)
        self.assertIn("PvP Account Report: acctA", report)
        self.assertIn("PvP performance", report)
        perf_report = render_performance_report(perf)
        self.assertIn("PvP Performance Report", perf_report)

    def test_unknowns_exposed(self):
        profile, battles, layout = self._build_profile()
        # profile with no resources/native ai still surfaces unknowns via a bare profile
        bare = AccountProgressionProfile(pseudonym="bare", hull_level=5)
        assessment = assess_account(bare)
        self.assertTrue(len(assessment.unknowns) > 0 or
                        any(s.score is None for s in assessment.scores.values()))
        roadmap = build_progression_roadmap(bare)
        self.assertTrue(len(roadmap.unknowns) > 0)


if __name__ == "__main__":
    unittest.main()
