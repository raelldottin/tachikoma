"""Suite for loop_target: deleting `n -= 1` turns count_down into an infinite loop."""

import unittest

from tests.mutation_toys import loop_target


class LoopToyTests(unittest.TestCase):
    def test_count_down(self):
        self.assertEqual(loop_target.count_down(3), 3)
