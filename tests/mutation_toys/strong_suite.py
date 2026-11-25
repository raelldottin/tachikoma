"""Strong toy suite: pins boundaries, exact values and side effects, so it kills every toy mutant."""

import unittest

from tests.mutation_toys import toy_target


class StrongToyTests(unittest.TestCase):
    def test_grade_boundaries(self):
        self.assertEqual(toy_target.grade(50), "pass")
        self.assertEqual(toy_target.grade(49), "fail")

    def test_is_member(self):
        self.assertIs(toy_target.is_member("a", ["a"]), True)
        self.assertIs(toy_target.is_member("b", ["a"]), False)
        self.assertIs(toy_target.is_member(None, [None]), False)

    def test_describe(self):
        self.assertEqual(toy_target.describe({"@Tag": "x"}), "<x>")
        self.assertEqual(toy_target.describe({}), "<none>")

    def test_tally(self):
        log = []
        self.assertEqual(toy_target.tally([1, 2], log), 3)
        self.assertEqual(log, [3])

    def test_require(self):
        self.assertIs(toy_target.require(True), True)
        with self.assertRaisesRegex(ValueError, "^flag required$"):
            toy_target.require(False)
