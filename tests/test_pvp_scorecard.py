"""Tests for sdk.reports.pvp: PvP scorecards and strategy comparison. Synthetic battles, offline."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from sdk.journal import GameplayEvent
from sdk.pvp import BattleResult
from sdk.reports import battles_from_events, compare_strategies, compute_scorecard, wilson_interval
from sdk.reports.battles import JournalBattle
from sdk.reports.pvp import to_json, to_markdown

T0 = datetime(2027, 1, 4, 12, 0, tzinfo=timezone.utc)
A, B = "weapon-focus-fire@v1", "reactor-shield-devastation@v1"


def battle(outcome, strategy=A, alias="alpha", role="attacker", hull="unknown", trophies=None, cost=None, n=[0]):
    n[0] += 1
    delta = {"win": 10, "loss": -8}.get(outcome) if trophies is None else trophies
    return JournalBattle(
        result=BattleResult(battle_id=str(n[0]), account_pseudonym=alias, role=role, outcome=outcome,
                            trophy_delta=delta or 0),
        attempt_id=f"a{n[0]}", evidence_tier="outcome-observed" if outcome != "unknown" else "finalised",
        trophy_delta=delta if outcome != "unknown" else None, strategy_key=strategy, strategy_config_version=None,
        opponent_hull_bracket=hull, opponent_trophy_bracket="unknown", started_at=T0.isoformat(),
        finished_at=T0.isoformat(), cost=cost or {},
    )


def many(strategy, wins, losses, unknown=0, **kwargs):
    return ([battle("win", strategy, **kwargs) for _ in range(wins)]
            + [battle("loss", strategy, **kwargs) for _ in range(losses)]
            + [battle("unknown", strategy, **kwargs) for _ in range(unknown)])


class WilsonTests(unittest.TestCase):
    def test_known_values(self):
        self.assertIsNone(wilson_interval(0, 0))
        self.assertEqual(wilson_interval(18, 30), (0.4232, 0.7541))
        low, high = wilson_interval(0, 10)
        self.assertEqual(low, 0.0)
        self.assertGreater(high, 0.0)
        self.assertEqual(wilson_interval(10, 10)[1], 1.0)


class ScorecardTests(unittest.TestCase):
    def test_rates_use_known_results_and_count_unknowns(self):
        [row] = compute_scorecard(many(A, 3, 1, unknown=2))
        self.assertEqual((row.attack.battles, row.attack.known, row.attack.wins, row.attack.losses, row.attack.unknown),
                         (6, 4, 3, 1, 2))
        self.assertEqual(row.attack.win_rate, 0.75)
        self.assertEqual(row.net_trophies, 3 * 10 - 8)
        self.assertEqual(row.trophy_samples, 4)
        self.assertEqual(row.defense.battles, 0)
        self.assertIsNone(row.defense.win_rate)
        self.assertIsNone(row.mean_duration_s)

    def test_only_unknown_outcomes_give_no_numbers(self):
        [row] = compute_scorecard(many(A, 0, 0, unknown=3))
        self.assertIsNone(row.attack.win_rate)
        self.assertIsNone(row.attack.interval)
        self.assertIsNone(row.net_trophies)

    def test_groups_by_alias_strategy_and_cohort(self):
        rows = compute_scorecard(many(A, 1, 0) + many(B, 1, 0) + many(A, 1, 0, alias="bravo")
                                 + many(A, 1, 0, hull="hull 8-10") + [battle("win", None)])
        keys = [(r.account_alias, r.strategy_key, r.opponent_hull_bracket) for r in rows]
        self.assertEqual(keys, [("alpha", "none", "unknown"), ("alpha", B, "unknown"), ("alpha", A, "hull 8-10"),
                                ("alpha", A, "unknown"), ("bravo", A, "unknown")])

    def test_defenses_and_resource_spend(self):
        rows = compute_scorecard([battle("win", role="defender"), battle("loss", role="defender"),
                                  battle("win", cost={"minerals_delta": -40, "gas_delta": None, "starbux_delta": 0})])
        [row] = rows
        self.assertEqual((row.defense.known, row.defense.win_rate), (2, 0.5))
        self.assertEqual(row.resources_spent, {"minerals": 40})
        self.assertEqual(row.resource_samples, 1)

    def test_empty_input(self):
        self.assertEqual(compute_scorecard([]), [])
        self.assertEqual(to_markdown([]), "_No battles._\n")
        self.assertEqual(json.loads(to_json([])), {"scorecard": []})


class CompareStrategiesTests(unittest.TestCase):
    def test_recommends_only_with_enough_samples_and_separated_intervals(self):
        battles = many(A, 32, 8) + many(B, 10, 30)
        result = compare_strategies(battles, A, B, min_sample=30)
        self.assertEqual((result.verdict, result.recommended), ("recommend", A))
        self.assertGreater(result.a.interval[0], result.b.interval[1])

    def test_too_few_known_results_is_insufficient(self):
        battles = many(A, 20, 0, unknown=40) + many(B, 0, 20)
        result = compare_strategies(battles, A, B, min_sample=30)
        self.assertEqual((result.verdict, result.recommended), ("insufficient-evidence", None))
        self.assertIn("20", result.reason)

    def test_overlapping_intervals_are_no_clear_difference(self):
        result = compare_strategies(many(A, 18, 12) + many(B, 15, 15), A, B, min_sample=30)
        self.assertEqual((result.verdict, result.recommended), ("no-clear-difference", None))

    def test_disagreeing_cohorts_block_a_recommendation(self):
        battles = (many(A, 60, 5, hull="hull 5-7") + many(B, 5, 60, hull="hull 5-7")
                   + many(A, 5, 35, hull="hull 8-10") + many(B, 35, 5, hull="hull 8-10"))
        result = compare_strategies(battles, A, B, min_sample=30)
        self.assertEqual((result.verdict, result.recommended), ("insufficient-evidence", None))
        self.assertIn("cohorts disagree", result.reason)
        self.assertEqual(set(result.cohorts), {"hull 5-7 / unknown", "hull 8-10 / unknown"})

    def test_filters_by_alias_and_role(self):
        battles = many(A, 40, 0) + many(B, 0, 40) + many(A, 0, 40, alias="bravo") + many(A, 0, 40, role="defender")
        result = compare_strategies(battles, A, B, account_alias="alpha", min_sample=30)
        self.assertEqual((result.a.known, result.b.known, result.recommended), (40, 40, A))

    def test_invalid_min_sample(self):
        with self.assertRaises(ValueError):
            compare_strategies([], A, B, min_sample=0)

    def test_outputs_include_comparison(self):
        battles = many(A, 32, 8) + many(B, 10, 30)
        rows, comparison = compute_scorecard(battles), compare_strategies(battles, A, B, min_sample=30)
        payload = json.loads(to_json(rows, comparison))
        self.assertEqual(payload["comparison"]["recommended"], A)
        table = to_markdown(rows, comparison)
        self.assertIn("Verdict: **recommend**", table)
        self.assertIn(f"| alpha | {A} | unknown / unknown | 40/40 | 80%", table)


class JournalToScorecardTests(unittest.TestCase):
    def test_journal_events_flow_into_the_scorecard(self):
        def event(minutes, outcome, tier, trophies):
            start = T0 + timedelta(minutes=minutes)
            return GameplayEvent.create(
                account_alias="alpha", action_id="run_pvp_battle", started_at=start,
                finished_at=start + timedelta(seconds=30), outcome=outcome, reason="synthetic", attempted=True,
                evidence={"evidence_tier": tier, "trophies_delta": trophies},
                cost={"minerals_delta": 0, "gas_delta": -5, "starbux_delta": 0},
            )

        battles = battles_from_events([event(0, "verified", "outcome-observed", 12),
                                       event(5, "unknown", "finalised", None)])
        [row] = compute_scorecard(battles)
        self.assertEqual((row.strategy_key, row.attack.known, row.attack.unknown, row.net_trophies), ("none", 1, 1, 12))
        self.assertEqual(row.resources_spent, {"gas": 10})


if __name__ == "__main__":
    unittest.main()
