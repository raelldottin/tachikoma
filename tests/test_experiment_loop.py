"""Tests for sdk.experiments: evidence-gated policy decisions and the decision log. Offline."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.experiments import (
    DecisionLog,
    Experiment,
    ExperimentError,
    MeanSample,
    ProportionSample,
    decide,
    decide_and_record,
    samples_from_outcomes,
)
from tests.synthetic_fixtures import SYNTHETIC_EMAIL

START = datetime(2027, 5, 1, tzinfo=timezone.utc)
END = START + timedelta(days=14)
OPEN, CLOSED = START + timedelta(days=3), END + timedelta(hours=1)

EXP = Experiment(
    experiment_id="exp-001", hypothesis="baseline-v2 verifies more actions than baseline-v1",
    baseline="baseline-v1", candidate="baseline-v2", metric="verified-action-success",
    higher_is_better=True, min_sample=30, window_start=START, window_end=END,
)


class DecideTests(unittest.TestCase):
    def test_adopt_reject_and_overlap(self):
        self.assertEqual(decide(EXP, ProportionSample(15, 40), ProportionSample(36, 40), OPEN).decision, "adopt")
        self.assertEqual(decide(EXP, ProportionSample(36, 40), ProportionSample(15, 40), OPEN).decision, "reject")
        overlap = decide(EXP, ProportionSample(20, 40), ProportionSample(24, 40), OPEN)
        self.assertEqual((overlap.decision, overlap.reason), ("keep-running", "the 95% intervals overlap"))
        self.assertEqual(decide(EXP, ProportionSample(20, 40), ProportionSample(24, 40), CLOSED).decision,
                         "insufficient-evidence")

    def test_never_adopts_below_minimum_sample(self):
        early = decide(EXP, ProportionSample(0, 29), ProportionSample(29, 29), OPEN)
        self.assertEqual(early.decision, "keep-running")
        self.assertIn("needs 30", early.reason)
        self.assertEqual(decide(EXP, ProportionSample(0, 29), ProportionSample(29, 29), CLOSED).decision,
                         "insufficient-evidence")

    def test_lower_is_better_metrics(self):
        cost = replace(EXP, metric="minerals-per-upgrade", higher_is_better=False, min_sample=5)
        cheaper = MeanSample((100, 102, 98, 101, 99))
        dearer = MeanSample((150, 149, 151, 152, 148))
        self.assertEqual(decide(cost, dearer, cheaper, OPEN).decision, "adopt")
        self.assertEqual(decide(cost, cheaper, dearer, OPEN).decision, "reject")

    def test_decision_records_evidence(self):
        decision = decide(EXP, ProportionSample(15, 40), ProportionSample(36, 40), OPEN)
        self.assertEqual((decision.baseline_n, decision.candidate_n), (40, 40))
        self.assertEqual(decision.candidate_estimate, 0.9)
        self.assertLess(decision.baseline_interval[1], decision.candidate_interval[0])

    def test_mixed_sample_kinds_and_naive_time_are_refused(self):
        with self.assertRaises(ExperimentError):
            decide(EXP, ProportionSample(1, 2), MeanSample((1.0, 2.0)), OPEN)
        with self.assertRaises(ExperimentError):
            decide(EXP, ProportionSample(1, 2), ProportionSample(1, 2), datetime(2027, 5, 2))


class ValidationTests(unittest.TestCase):
    def test_experiment_validation(self):
        bad = [dict(experiment_id="Bad Id"), dict(candidate="baseline-v1"), dict(hypothesis=" "),
               dict(hypothesis=f"ask {SYNTHETIC_EMAIL}"), dict(min_sample=1), dict(min_sample=True),
               dict(higher_is_better="yes"), dict(window_end=START), dict(window_start=datetime(2027, 5, 1))]
        for overrides in bad:
            with self.subTest(overrides=list(overrides)), self.assertRaises(ExperimentError):
                replace(EXP, **overrides)

    def test_sample_validation(self):
        for args in ((5, 4), (-1, 3), (1.5, 3)):
            with self.subTest(args=args), self.assertRaises(ExperimentError):
                ProportionSample(*args)
        with self.assertRaises(ExperimentError):
            MeanSample((1.0, float("nan")))
        self.assertIsNone(ProportionSample(0, 0).interval())
        self.assertIsNone(MeanSample((3.0,)).interval())

    def test_samples_from_outcomes(self):
        sample = samples_from_outcomes(["verified", "unknown", "skipped", "verified", "not-observed"])
        self.assertEqual((sample.successes, sample.trials), (2, 4))


class DecisionLogTests(unittest.TestCase):
    def test_append_read_and_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = DecisionLog(Path(tmp) / "decisions.jsonl")
            self.assertEqual(log.read(), [])
            first = decide_and_record(EXP, ProportionSample(10, 20), ProportionSample(12, 20), OPEN, log)
            second = decide_and_record(EXP, ProportionSample(15, 40), ProportionSample(36, 40), OPEN, log)
            self.assertEqual(log.read(), [first, second])
            self.assertEqual(log.latest("exp-001").decision, "adopt")
            self.assertIsNone(log.latest("exp-404"))
            lines = log.path.read_text().splitlines()
            self.assertEqual(json.loads(lines[0])["decision"], "keep-running")

    def test_malformed_line_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text("{not json}\n")
            with self.assertRaises(ExperimentError):
                DecisionLog(path).read()


if __name__ == "__main__":
    unittest.main()
