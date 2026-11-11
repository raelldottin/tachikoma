"""Tests for sdk.experiments.study: study plans and sample-size planning. Offline, standard library only."""

import math
import random
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sdk.experiments import (
    RULE_TEST,
    Experiment,
    ExperimentError,
    MeanSample,
    ProportionSample,
    StudyPlan,
    decide,
    experiments_for,
    non_overlap_mean_power,
    non_overlap_mean_sample_size,
    non_overlap_proportion_power,
    non_overlap_proportion_sample_size,
    per_arm_min_sample,
    two_mean_sample_size,
    two_proportion_sample_size,
    z_quantile,
)
from tests.synthetic_fixtures import SYNTHETIC_EMAIL

START = datetime(2027, 5, 1, tzinfo=timezone.utc)
END = START + timedelta(days=28)

SUCCESS_PLAN = StudyPlan(
    study_id="study-001", hypothesis="rule-based automation verifies more actions than the manual routine",
    arms=("manual", "baseline-v1"), metric="verified-action-success", metric_kind="proportion",
    higher_is_better=True, baseline_value=0.70, minimum_effect=0.15, unit="action",
    window_start=START, window_end=END,
)
GROWTH_PLAN = StudyPlan(
    study_id="study-002", hypothesis="baseline-v1 levels crew faster per day than the manual routine",
    arms=("manual", "baseline-v1"), metric="crew-levels-per-day", metric_kind="mean", higher_is_better=True,
    baseline_value=3.0, minimum_effect=1.0, sd=1.0, unit="account-day", window_start=START, window_end=END,
)


def _binomial(n, p):
    return [math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(n + 1)]


class QuantileTests(unittest.TestCase):
    def test_standard_normal_quantiles(self):
        self.assertAlmostEqual(z_quantile(0.975), 1.959964, places=6)
        self.assertAlmostEqual(z_quantile(0.95), 1.644854, places=6)
        self.assertAlmostEqual(z_quantile(0.80), 0.841621, places=6)

    def test_rejects_out_of_range(self):
        for bad in (0, 1, -0.1, 1.5, True, float("nan"), "0.5"):
            with self.assertRaises(ExperimentError):
                z_quantile(bad)


class TwoMeanTests(unittest.TestCase):
    def test_textbook_values(self):
        # n = 2 (z_.975 + z_.80)^2 / d^2 per group: 62.8 -> 63 for d = 0.5 (Cohen 1988 tabulates 64 with the
        # t distribution; the normal approximation gives 63), 251.2 -> 252 for d = 0.25, 15.7 -> 16 for d = 1.
        self.assertEqual(two_mean_sample_size(0.5, 1.0), 63)
        self.assertEqual(two_mean_sample_size(0.25, 1.0), 252)
        self.assertEqual(two_mean_sample_size(1.0, 1.0), 16)
        self.assertEqual(two_mean_sample_size(5.0, 10.0), 63)  # only delta / sd matters
        self.assertEqual(two_mean_sample_size(-0.5, 1.0), 63)  # direction does not change the size

    def test_one_sided_unequal_sd_and_other_rates(self):
        self.assertEqual(two_mean_sample_size(0.5, 1.0, two_sided=False), 50)  # 2 (1.645 + 0.842)^2 / 0.25 = 49.5
        self.assertEqual(two_mean_sample_size(5.0, 10.0, sd2=15.0), 103)  # 7.849 * 325 / 25 = 102.0
        self.assertEqual(two_mean_sample_size(0.5, 1.0, power=0.90), 85)  # 2 (1.960 + 1.282)^2 / 0.25 = 84.1
        self.assertEqual(two_mean_sample_size(0.5, 1.0, alpha=0.01), 94)  # 2 (2.576 + 0.842)^2 / 0.25 = 93.4

    def test_rejects_bad_inputs(self):
        for args, kwargs in (((0, 1), {}), ((0.5, 0), {}), ((0.5, -1), {}), ((0.5, 1), {"sd2": 0}),
                             ((0.5, 1), {"alpha": 0}), ((0.5, 1), {"power": 1}), ((True, 1), {}),
                             ((float("inf"), 1), {})):
            with self.assertRaises(ExperimentError, msg=(args, kwargs)):
                two_mean_sample_size(*args, **kwargs)

    def test_tiny_effect_is_refused_not_looped(self):
        with self.assertRaises(ExperimentError):
            two_mean_sample_size(1e-6, 1.0)


class TwoProportionTests(unittest.TestCase):
    def test_textbook_values(self):
        # Fleiss, Levin and Paik eq. 4.14 without continuity correction: 0.50 vs 0.60 needs 387.3 -> 388 per
        # group (the figure Stata's `power twoproportions 0.5 0.6` reports); the unpooled form gives 384.6 -> 385.
        self.assertEqual(two_proportion_sample_size(0.5, 0.6), 388)
        self.assertEqual(two_proportion_sample_size(0.5, 0.6, pooled=False), 385)
        self.assertEqual(two_proportion_sample_size(0.6, 0.5), 388)  # symmetric in the two rates
        self.assertEqual(two_proportion_sample_size(0.65, 0.85), 73)
        self.assertEqual(two_proportion_sample_size(0.65, 0.85, pooled=False), 70)

    def test_matches_the_formula(self):
        z_a, z_b = z_quantile(0.975), z_quantile(0.80)
        p1, p2 = 0.1, 0.2
        pbar = (p1 + p2) / 2
        expected = (z_a * math.sqrt(2 * pbar * (1 - pbar)) + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
        self.assertEqual(two_proportion_sample_size(p1, p2), math.ceil(expected / (p1 - p2) ** 2))

    def test_rejects_bad_inputs(self):
        for p1, p2 in ((0.5, 0.5), (0.0, 0.5), (0.5, 1.0), (-0.1, 0.5), (False, 0.5)):
            with self.assertRaises(ExperimentError, msg=(p1, p2)):
                two_proportion_sample_size(p1, p2)


class DecideRuleTests(unittest.TestCase):
    def test_proportion_power_matches_full_enumeration(self):
        for n, p1, p2 in ((30, 0.5, 0.8), (40, 0.7, 0.9), (25, 0.6, 0.3)):
            ints = [ProportionSample(k, n).interval() for k in range(n + 1)]
            f1, f2 = _binomial(n, p1), _binomial(n, p2)
            better = (lambda c, b: ints[c][0] > ints[b][1]) if p2 > p1 else (lambda c, b: ints[c][1] < ints[b][0])
            expected = sum(f1[b] * f2[c] for b in range(n + 1) for c in range(n + 1) if better(c, b))
            self.assertAlmostEqual(non_overlap_proportion_power(n, p1, p2), expected, places=10)

    def test_proportion_size_is_the_power_boundary(self):
        for p1, p2 in ((0.7, 0.85), (0.5, 0.6), (0.05, 0.15), (0.6, 0.45)):
            n = non_overlap_proportion_sample_size(p1, p2)
            self.assertGreaterEqual(non_overlap_proportion_power(n, p1, p2), 0.80)
            self.assertLess(non_overlap_proportion_power(n - 1, p1, p2), 0.80)
            self.assertGreater(n, two_proportion_sample_size(p1, p2))
        self.assertEqual(non_overlap_proportion_sample_size(0.7, 0.85), 199)

    def test_mean_size_is_the_power_boundary(self):
        for delta in (0.5, 1.0, 2.0):
            n = non_overlap_mean_sample_size(delta, 1.0)
            self.assertGreaterEqual(non_overlap_mean_power(n, delta, 1.0), 0.80)
            self.assertLess(non_overlap_mean_power(n - 1, delta, 1.0), 0.80)
        self.assertEqual(non_overlap_mean_sample_size(0.5, 1.0), 107)
        self.assertEqual(non_overlap_mean_sample_size(1.0, 1.0), 29)

    def test_conventionally_sized_trial_is_underpowered_for_decide(self):
        # Regression guard for the sizing choice: 63 per arm gives a z-test 80% power for d = 0.5, but decide()
        # needs non-overlapping intervals and would adopt only about half the time.
        self.assertLess(non_overlap_mean_power(two_mean_sample_size(0.5, 1.0), 0.5, 1.0), 0.55)

    def test_mean_power_agrees_with_decide_by_simulation(self):
        rng = random.Random(20271101)
        n = non_overlap_mean_sample_size(1.0, 1.0)
        experiment = Experiment("sim", "h", "a", "b", "m", True, n, START, END)
        adopted = sum(
            decide(experiment, MeanSample(tuple(rng.gauss(0, 1) for _ in range(n))),
                   MeanSample(tuple(rng.gauss(1, 1) for _ in range(n))), START).decision == "adopt"
            for _ in range(400)
        )
        self.assertAlmostEqual(adopted / 400, 0.80, delta=0.07)

    def test_power_rejects_bad_n(self):
        for bad in (0, -1, 1.5, True):
            with self.assertRaises(ExperimentError):
                non_overlap_proportion_power(bad, 0.5, 0.6)
        with self.assertRaises(ExperimentError):
            non_overlap_mean_power(1, 0.5, 1.0)


class StudyPlanTests(unittest.TestCase):
    def test_per_arm_min_sample_uses_the_decide_rule_by_default(self):
        self.assertEqual(per_arm_min_sample(SUCCESS_PLAN), non_overlap_proportion_sample_size(0.70, 0.85))
        self.assertEqual(per_arm_min_sample(GROWTH_PLAN), non_overlap_mean_sample_size(1.0, 1.0))
        self.assertEqual(per_arm_min_sample(replace(GROWTH_PLAN, min_sample=60)), 60)  # the floor wins when larger

    def test_test_rule_and_bonferroni_split(self):
        plan = replace(GROWTH_PLAN, decision_rule=RULE_TEST, minimum_effect=0.5)
        self.assertEqual(per_arm_min_sample(plan), 63)
        three = replace(plan, arms=("manual", "baseline-v1", "adaptive-v1"))
        self.assertEqual(three.comparisons, 2)
        self.assertAlmostEqual(three.comparison_alpha, 0.025)
        self.assertEqual(per_arm_min_sample(three), two_mean_sample_size(0.5, 1.0, alpha=0.025))
        self.assertEqual(per_arm_min_sample(replace(SUCCESS_PLAN, decision_rule=RULE_TEST, minimum_effect=0.1,
                                                    baseline_value=0.5)), 388)

    def test_direction_sets_the_candidate_value(self):
        lower = replace(SUCCESS_PLAN, metric="failure-rate", higher_is_better=False, baseline_value=0.30)
        self.assertAlmostEqual(lower.candidate_value, 0.15)
        self.assertEqual(per_arm_min_sample(lower), per_arm_min_sample(SUCCESS_PLAN))  # mirror of 0.70 -> 0.85

    def test_experiments_for_feeds_decide(self):
        plan = replace(GROWTH_PLAN, arms=("manual", "baseline-v1", "adaptive-v1"))
        experiments = experiments_for(plan)
        self.assertEqual([e.experiment_id for e in experiments], ["study-002.baseline-v1", "study-002.adaptive-v1"])
        self.assertTrue(all(e.baseline == "manual" and e.min_sample == per_arm_min_sample(plan) for e in experiments))
        short = MeanSample(tuple(float(v % 5) for v in range(plan.min_sample + 10)))
        self.assertEqual(decide(experiments[0], short, short, START + timedelta(days=1)).decision, "keep-running")

    def test_validation(self):
        bad = (
            {"study_id": "Study 1"}, {"hypothesis": " "}, {"hypothesis": f"compare {SYNTHETIC_EMAIL}"},
            {"arms": ("manual",)}, {"arms": ["manual", "baseline-v1"]}, {"arms": ("manual", "manual")},
            {"arms": ("manual", "Baseline")}, {"metric_kind": "count"}, {"higher_is_better": 1},
            {"baseline_value": float("nan")}, {"minimum_effect": 0}, {"minimum_effect": -0.1},
            {"minimum_effect": 0.3},  # 0.70 + 0.30 reaches 1.0
            {"sd": 0.2},  # sd is for mean metrics only
            {"alpha": 0.01},  # decide() is fixed at 95% intervals
            {"power": 1.0}, {"min_sample": 1}, {"min_sample": True}, {"decision_rule": "bayes"},
            {"unit": ""}, {"window_end": START}, {"window_start": START.replace(tzinfo=None)},
            {"study_id": "s" * 95},  # study_id.arm would exceed an experiment id
        )
        for change in bad:
            with self.assertRaises(ExperimentError, msg=change):
                replace(SUCCESS_PLAN, **change)
        for change in ({"sd": None}, {"sd": 0.0}, {"sd": -1.0}):
            with self.assertRaises(ExperimentError, msg=change):
                replace(GROWTH_PLAN, **change)
        with self.assertRaises(ExperimentError):
            replace(GROWTH_PLAN, decision_rule=RULE_TEST, alpha=0.05, power=0.04)
        self.assertEqual(replace(GROWTH_PLAN, decision_rule=RULE_TEST, alpha=0.01).comparison_alpha, 0.01)


if __name__ == "__main__":
    unittest.main()
