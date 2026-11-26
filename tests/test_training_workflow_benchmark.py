"""Passing controls and fixture integrity tests for the explicit SPEC-14 red gate."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import training_workflow_benchmark as gate


@dataclass
class FixtureFiles:
    directory: Path
    benchmark: dict
    holdout: dict

    def write(self):
        for label, package in (("benchmark", self.benchmark), ("holdout", self.holdout)):
            (self.directory / f"workflow_{label}.json").write_text(json.dumps(package), encoding="utf-8")


class WorkflowFixtureTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixtures = FixtureFiles(Path(directory.name), *gate.load_fixtures())

    # Verifies: S14-R7
    def test_missing_and_duplicate_cases_are_refused(self):
        cases = self.fixtures.benchmark["cases"]
        removed = cases.pop()
        self.fixtures.write()
        with self.assertRaisesRegex(ValueError, "every required case"):
            gate.load_fixtures(self.fixtures.directory)
        cases.extend((removed, removed))
        self.fixtures.write()
        with self.assertRaisesRegex(ValueError, "fixture IDs must be unique"):
            gate.load_fixtures(self.fixtures.directory)

    # Verifies: S14-R7
    def test_holdout_must_have_an_independent_paid_boundary_witness(self):
        paid = next(case for case in self.fixtures.holdout["cases"] if case["kind"] == "paid-boundary")
        self.fixtures.holdout["cases"].remove(paid)
        self.fixtures.write()
        with self.assertRaisesRegex(ValueError, "independently authored paid-boundary witness"):
            gate.load_fixtures(self.fixtures.directory)
        self.fixtures.holdout["cases"].append(paid)
        paid["seeds"] = self.fixtures.benchmark["seeds"][:1]
        self.fixtures.write()
        with self.assertRaisesRegex(ValueError, "independent seeds"):
            gate.load_fixtures(self.fixtures.directory)


class LegacyControlTests(unittest.TestCase):
    # Verifies: S14-R7
    def test_zero_cost_course_starts_through_the_real_fleet_cli(self):
        manifest, _ = gate.load_fixtures()
        control = manifest["legacy_control"]
        observed = gate.observe_legacy_training(control["input"], manifest["seeds"][0])
        self.assertEqual(observed, control["expected"])

    def test_fake_debits_exact_paid_course_price_and_observation_verifies_start(self):
        manifest, _ = gate.load_fixtures()
        paid = next(case for case in manifest["cases"] if case["id"] == "paid-boundary")
        observed = gate.observe_legacy_training({**paid["input"], "minerals": 500}, manifest["seeds"][0])
        self.assertEqual(observed, {
            "add_training_requests": 1,
            "mineral_delta": -500,
            "gas_delta": 0,
            "verified_starts": 1,
        })


class RedGateTests(unittest.TestCase):
    # Verifies: S14-R7
    def test_named_behavioral_mismatch_has_distinct_exit_status(self):
        mismatch = ("paid-boundary", 13, {"add_training_requests": 0}, {"add_training_requests": 1})
        output = io.StringIO()
        with patch.object(gate, "evaluate", return_value=[mismatch]), redirect_stdout(output):
            self.assertEqual(gate.main(["--gate"]), 1)
        self.assertIn("paid-boundary seed=13: expected {'add_training_requests': 0}", output.getvalue())
        self.assertIn("observed {'add_training_requests': 1}", output.getvalue())

    # Verifies: S14-R7
    def test_missing_witness_and_evaluation_error_are_gate_errors(self):
        for outcome, message in [
            ([], "found no behavioral red witness"),
            (ValueError("invalid fixture"), "could not evaluate its required witness: invalid fixture"),
        ]:
            with self.subTest(message=message):
                output = io.StringIO()
                with patch.object(gate, "evaluate") as evaluate, redirect_stderr(output):
                    if isinstance(outcome, ValueError):
                        evaluate.side_effect = outcome
                    else:
                        evaluate.return_value = outcome
                    self.assertEqual(gate.main(["--gate"]), 2)
                self.assertIn(message, output.getvalue())
