"""sdk.training.actions: each decision maps to exactly the catalog action and params its spec requires."""

import unittest

from sdk.actions.catalog import get_spec
from sdk.training.actions import TRAINING_ACTION_IDS, action_for
from sdk.training.model import FinishTraining, Skip, StartTraining, UseConsumable


class ActionForTests(unittest.TestCase):
    def test_each_decision_maps_to_its_action_and_string_params(self):
        cases = (
            (FinishTraining(101, "ended"), ("finish_training", {"character_id": "101"})),
            (StartTraining(101, 5030, "band"), ("add_training", {"training_design_id": "5030", "character_id": "101"})),
            (UseConsumable(102, 901, "rule"), ("use_consumable", {"item_design_id": "901", "character_id": "102"})),
        )
        for decision, expected in cases:
            with self.subTest(decision=decision):
                self.assertEqual(action_for(decision), expected)

    def test_a_skip_sends_nothing(self):
        self.assertIsNone(action_for(Skip(101, "complete", "at cap")))

    def test_anything_else_is_refused(self):
        for value in (None, "add_training", (101, 5030)):
            with self.subTest(value=value), self.assertRaises(TypeError):
                action_for(value)

    def test_params_are_exactly_each_catalog_specs_required_params(self):
        decisions = (FinishTraining(7, ""), StartTraining(7, 8, ""), UseConsumable(7, 9, ""))
        seen = []
        for decision in decisions:
            action_id, params = action_for(decision)
            seen.append(action_id)
            self.assertEqual(sorted(params), sorted(get_spec(action_id).required_params), action_id)
        self.assertEqual(tuple(seen), TRAINING_ACTION_IDS)
