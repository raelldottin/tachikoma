"""Weak toy suite: every toy function is called once without edge cases, so ten of the 24 toy mutants survive."""

import unittest

from tests.mutation_toys import toy_target


class WeakToyTests(unittest.TestCase):
    def test_grade_passes_a_high_score(self):
        self.assertEqual(toy_target.grade(80), "pass")

    def test_is_member_finds_a_member(self):
        self.assertTrue(toy_target.is_member("a", ["a"]))

    def test_describe_returns_text(self):
        self.assertIsInstance(toy_target.describe({"@Tag": "x"}), str)

    def test_tally_adds(self):
        self.assertEqual(toy_target.tally([1, 2], []), 3)

    def test_require_accepts_true(self):
        self.assertTrue(toy_target.require(True))
