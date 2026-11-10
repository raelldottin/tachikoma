"""Regression guard: the R0 capability inventory must track the code it describes."""

import ast
import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = REPO_ROOT / "docs" / "baseline" / "capability-inventory.json"
PILLARS = {"platform", "A", "B", "C", "D", "E", "F"}
KINDS = {"read-only", "mutating", "session"}
EVIDENCE = {"mocked-test", "live-claimed", "untested"}


def public_client_methods() -> set[str]:
    tree = ast.parse((REPO_ROOT / "sdk" / "client.py").read_text(encoding="utf-8"))
    client = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Client")
    return {
        node.name
        for node in client.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }


def run_flags() -> set[str]:
    tree = ast.parse((REPO_ROOT / "run.py").read_text(encoding="utf-8"))
    flags = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and str(arg.value).startswith("--"):
                    flags.add(arg.value)
                    break
    return flags


class CapabilityInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))

    def test_every_public_client_method_is_inventoried(self):
        listed = {entry["name"] for entry in self.inventory["client_methods"]}
        actual = public_client_methods()
        self.assertEqual(sorted(actual - listed), [], "Client methods missing from the inventory")
        self.assertEqual(sorted(listed - actual), [], "Inventory lists methods that no longer exist")

    def test_every_run_flag_is_inventoried(self):
        listed = {entry["flag"] for entry in self.inventory["run_flags"]}
        actual = run_flags()
        self.assertEqual(sorted(actual - listed), [], "run.py flags missing from the inventory")
        self.assertEqual(sorted(listed - actual), [], "Inventory lists run.py flags that no longer exist")

    def test_entries_use_known_classifications(self):
        for entry in self.inventory["client_methods"] + self.inventory["run_flags"]:
            label = entry.get("name") or entry.get("flag")
            with self.subTest(label=label):
                self.assertIn(entry["pillar"], PILLARS)
                self.assertIn(entry["kind"], KINDS)
                self.assertIn(entry["evidence"], EVIDENCE)

    def test_cited_tests_exist_and_mention_the_capability(self):
        for entry in self.inventory["client_methods"] + self.inventory["run_flags"]:
            label = entry.get("name") or entry.get("flag")
            pattern = re.compile(rf"\b{re.escape(label)}\b") if "name" in entry else re.compile(re.escape(label))
            for cited in entry["tests"]:
                with self.subTest(label=label, cited=cited):
                    path = REPO_ROOT / cited
                    self.assertTrue(path.is_file(), f"{cited} does not exist")
                    self.assertRegex(path.read_text(encoding="utf-8"), pattern)

    def test_evidence_level_matches_citations(self):
        for entry in self.inventory["client_methods"] + self.inventory["run_flags"]:
            label = entry.get("name") or entry.get("flag")
            with self.subTest(label=label):
                if entry["evidence"] == "mocked-test":
                    self.assertTrue(entry["tests"], "mocked-test evidence needs at least one cited test")
                if entry["evidence"] == "untested":
                    self.assertEqual(entry["tests"], [], "untested entries must not cite tests")
                if entry["evidence"] == "live-claimed":
                    self.assertTrue(entry.get("source"), "live-claimed evidence needs a cited source")


if __name__ == "__main__":
    unittest.main()
