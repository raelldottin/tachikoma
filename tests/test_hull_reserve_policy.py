"""Tests for sdk.policy hull-reserve-v1. Synthetic snapshots and design data only."""

import unittest
from dataclasses import replace

from sdk.policy import POLICIES, DesignData, HullDesign, HullHold, HullReserveV1, PolicyConfig, get_policy, hull_hold
from sdk.state import build_snapshot

from tests.test_progression_policy import DESIGN, RESEARCH, ROOMS, T0, ids, reasons

SHIPS = [
    {"@ShipDesignId": "3", "@ShipLevel": "3", "@MineralCost": "4000"},
    {"@ShipDesignId": "4", "@ShipLevel": "4", "@MineralCost": "1100"},
]
HULL_DESIGN = DesignData.from_records(
    room_designs=[
        {"@RoomDesignId": "100", "@RoomName": "Engine"},
        {"@RoomDesignId": "101", "@RoomName": "Engine II", "@UpgradeFromRoomDesignId": "100",
         "@PriceString": "mineral:300"},
        {"@RoomDesignId": "200", "@RoomName": "Shield"},
        {"@RoomDesignId": "201", "@RoomName": "Shield II", "@UpgradeFromRoomDesignId": "200",
         "@PriceString": "gas:900"},
        {"@RoomDesignId": "300", "@RoomName": "Bridge", "@UpgradeFromRoomDesignId": "0"},
    ],
    research_designs=[
        {"@ResearchDesignId": "500", "@RootResearchDesignId": "500", "@GasCost": "100"},
        {"@ResearchDesignId": "501", "@RootResearchDesignId": "500", "@GasCost": "200"},
        {"@ResearchDesignId": "510", "@RootResearchDesignId": "510", "@GasCost": "50"},
        {"@ResearchDesignId": "520", "@RootResearchDesignId": "520", "@GasCost": "0", "@StarbuxCost": "10"},
    ],
    ship_designs=SHIPS,
)
# Hull 3 caps crew at level 12; hull 4 raises the cap to 16.
CAPPED = [{"@CharacterId": "c1", "@Level": "12"}, {"@CharacterId": "c2", "@Level": "12"}]


def snap(**overrides):
    kwargs = dict(resources={"minerals": "1000", "gas": "2000", "starbux": "5"}, rooms=ROOMS, research=RESEARCH,
                  ship={"@ShipLevel": "3"}, crew=CAPPED)
    kwargs.update(overrides)
    return build_snapshot("alpha", T0, "synthetic-fixture", **kwargs)


def both(snapshot, design=HULL_DESIGN, **config):
    policy_config = PolicyConfig(**config)
    return (get_policy("baseline-v1").plan(snapshot, design, policy_config),
            get_policy("hull-reserve-v1").plan(snapshot, design, policy_config))


class HullReserveRegistryTests(unittest.TestCase):
    def test_variant_is_registered_and_baseline_stays_first(self):
        self.assertIsInstance(get_policy("hull-reserve-v1"), HullReserveV1)
        self.assertEqual(get_policy("hull-reserve-v1").name, "hull-reserve-v1")
        self.assertEqual(list(POLICIES)[0], "baseline-v1")

    def test_ship_designs_parse_and_default_to_none(self):
        self.assertEqual(DesignData().hulls, ())
        self.assertEqual(DESIGN.hulls, ())
        parsed = DesignData.from_records(ship_designs=[
            {"@ShipDesignId": "9", "@ShipLevel": "4", "@MineralCost": "1100"},
            {"@ShipDesignId": "10", "@ShipLevel": "x", "@MineralCost": "1,100"},
            {"@ShipDesignId": "11"},
        ])
        self.assertEqual(parsed.hulls, (HullDesign("9", 4, 1100), HullDesign("10", None, None),
                                        HullDesign("11", None, None)))


class HullReserveFallbackTests(unittest.TestCase):
    """Whenever the hold isn't applied the requests equal baseline-v1's and a note says why."""

    def assert_falls_back(self, snapshot, expected_reason, design=HULL_DESIGN, **config):
        baseline, variant = both(snapshot, design, **config)
        self.assertEqual(variant.policy, "hull-reserve-v1")
        self.assertEqual(variant.requests, baseline.requests)
        self.assertEqual(variant.rejected[:-1], baseline.rejected)
        self.assertEqual(variant.rejected[-1].candidate, "hull_reserve")
        self.assertEqual(variant.rejected[-1].reason, f"not applied, planned as baseline-v1: {expected_reason}")
        self.assertEqual(replace(variant, policy="baseline-v1", rejected=baseline.rejected), baseline)

    def test_not_near_when_any_crew_is_below_the_cap(self):
        crew = [{"@CharacterId": "c1", "@Level": "12"}, {"@CharacterId": "c2", "@Level": "11"}]
        self.assert_falls_back(snap(crew=crew),
                               "not near the hull 4 upgrade: 1 of 2 crew below the hull 3 crew level cap (12)")

    def test_unobserved_hull_level(self):
        self.assert_falls_back(snap(ship=None), "hull level not observed")
        self.assert_falls_back(snap(ship={"@ShipLevel": "?"}), "hull level not observed")
        self.assert_falls_back(snap(ship={"@ShipLevel": "0"}), "hull level 0 is not a valid hull level")

    def test_unobserved_crew(self):
        self.assert_falls_back(snap(crew=None), "crew section is missing, so crew levels are not fully observed")
        partial = CAPPED + [{"@Level": "12"}]  # a record without @CharacterId is skipped
        self.assert_falls_back(snap(crew=partial), "crew section is partial, so crew levels are not fully observed")
        self.assert_falls_back(snap(crew=[]), "no crew observed")
        self.assert_falls_back(snap(crew=CAPPED + [{"@CharacterId": "c3"}]), "a crew member's level is not observed")

    def test_next_hull_cost_unknown(self):
        no_ships = replace(HULL_DESIGN, hulls=())
        self.assert_falls_back(snap(), "no hull design for level 4 in the design data, so its cost is unknown",
                               design=no_ships)
        unpriced = replace(HULL_DESIGN, hulls=(HullDesign("4", 4, None),))
        self.assert_falls_back(snap(), "hull 4 cost unknown: a level 4 hull design has no parseable @MineralCost",
                               design=unpriced)

    def test_ambiguous_next_hull_cost_is_not_guessed(self):
        two = replace(HULL_DESIGN, hulls=(HullDesign("4", 4, 1100), HullDesign("44", 4, 2500)))
        self.assert_falls_back(snap(), "hull 4 cost is ambiguous (1100, 2500 mineral); "
                                       "the snapshot does not say which hull design the ship upgrades to", design=two)

    def test_next_hull_that_does_not_raise_the_crew_cap(self):
        crew = [{"@CharacterId": "c1", "@Level": "40"}]
        self.assert_falls_back(snap(ship={"@ShipLevel": "10"}, crew=crew),
                               "the hull 11 upgrade does not raise the crew level cap (40)")

    def test_configured_reserve_already_covers_the_hull(self):
        self.assert_falls_back(snap(), "the configured mineral reserve (1100) already covers the hull 4 cost "
                                       "(1100 mineral)", mineral_reserve=1100)

    def test_identical_to_baseline_on_the_baseline_fixture(self):
        # baseline-v1's own fixture has no ship or crew, so the hold never applies.
        self.assert_falls_back(build_snapshot("alpha", T0, "synthetic-fixture", rooms=ROOMS, research=RESEARCH,
                                              resources={"minerals": "1000", "gas": "2000", "starbux": "5"}),
                               "hull level not observed", design=DESIGN)


class HullReserveAppliedTests(unittest.TestCase):
    def test_hold_reports_the_next_hull_and_cost(self):
        hold, why = hull_hold(snap(), HULL_DESIGN)
        self.assertEqual(hold, HullHold(next_level=4, mineral_cost=1100))
        self.assertEqual(why, "near the hull 4 upgrade: every crew member is at the crew level cap (12)")

    def test_crew_above_the_cap_still_counts_as_capped(self):
        crew = [{"@CharacterId": "c1", "@Level": "12"}, {"@CharacterId": "c2", "@Level": "15"}]
        self.assertIsNotNone(hull_hold(snap(crew=crew), HULL_DESIGN)[0])

    def test_mineral_room_upgrade_that_would_delay_the_hull_is_rejected(self):
        # 1000 minerals < the 1100 hull cost: baseline-v1 upgrades the engine, hull-reserve-v1 holds.
        baseline, variant = both(snap())
        engine = ("upgrade_room", {"room_id": "r1", "upgrade_room_design_id": "101"})
        self.assertIn(engine, ids(baseline))
        self.assertNotIn(engine, ids(variant))
        self.assertEqual(reasons(variant)["upgrade_room r1"],
                         "needs 300 mineral, 0 available above reserve (held for the hull 4 upgrade: "
                         "mineral reserve raised from 0 to its 1100 mineral cost)")
        # Everything else is baseline-v1's plan: research, the gas-priced shield, the daily reward.
        self.assertEqual(ids(variant), [
            ("add_research", {"research_design_id": "501"}),
            ("add_research", {"research_design_id": "510"}),
            ("upgrade_room", {"room_id": "r2", "upgrade_room_design_id": "201"}),
            ("collect_all_resources", {}),
            ("collect_daily_reward", {}),
        ])
        expected = ids(baseline)
        del expected[expected.index(engine):expected.index(engine) + 2]  # the engine and its collection
        self.assertEqual(ids(variant), expected)
        self.assertNotIn("hull_reserve", reasons(variant))

    def test_surplus_above_the_hull_cost_is_still_spent(self):
        # 1500 minerals - 1100 held = 400 available, enough for the 300-mineral engine.
        baseline, variant = both(snap(resources={"minerals": "1500", "gas": "2000", "starbux": "5"}))
        self.assertEqual(variant.requests, baseline.requests)

    def test_rejections_not_caused_by_the_hold_keep_their_reason(self):
        # With 0 minerals baseline-v1 can't afford the engine either, so the reason isn't attributed to the hold.
        baseline, variant = both(snap(resources={"minerals": "0", "gas": "2000", "starbux": "5"}))
        self.assertEqual(reasons(variant)["upgrade_room r1"], reasons(baseline)["upgrade_room r1"])
        self.assertEqual(variant.requests, baseline.requests)

    def test_other_config_still_applies(self):
        _, variant = both(snap(), gas_reserve=1500, max_requests=2)
        self.assertEqual(len(variant.requests), 2)
        self.assertIn("starbux spend not allowed", reasons(variant)["add_research 520"])
        self.assertEqual(reasons(variant)["upgrade_room r2"], "needs 900 gas, 250 available above reserve")

    def test_plan_is_deterministic_and_does_not_change_baseline(self):
        before = get_policy("baseline-v1").plan(snap(), HULL_DESIGN, PolicyConfig())
        self.assertEqual(get_policy("hull-reserve-v1").plan(snap(), HULL_DESIGN, PolicyConfig()),
                         get_policy("hull-reserve-v1").plan(snap(), HULL_DESIGN, PolicyConfig()))
        self.assertEqual(get_policy("baseline-v1").plan(snap(), HULL_DESIGN, PolicyConfig()), before)


if __name__ == "__main__":
    unittest.main()
