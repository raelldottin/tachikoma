"""Mutation-testing harness tests: pure mutant generation, config validation, fake and real runner behaviour.

The toy suites under tests/mutation_toys are never imported here at module level, so test discovery cannot collect
them; the harness runs them by module name inside temporary copies of a toy repository.
"""

from argparse import Namespace
import ast
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from scripts import mutation
from tests.test_strict_mode import skip_unless_dependency

try:
    import yaml
except ImportError:
    yaml = None

REPO = Path(os.path.abspath(__file__)).parent.parent
TOYS = REPO / "tests" / "mutation_toys"
TOY_PATH = "tests/mutation_toys/toy_target.py"
LOOP_PATH = "tests/mutation_toys/loop_target.py"
TOY_COPY = ("scripts/ci.py", "tests/__init__.py", "tests/mutation_toys")
LOOP_DELETE_ID = f"{LOOP_PATH}:7:8:delete:0"

EXPECTED_TOY = [
    (f"{TOY_PATH}:{suffix}", original, replacement)
    for suffix, original, replacement in (
        ("5:7:negate:0", "score >= 50", "not (score >= 50)"),
        ("5:13:compare:0", ">=", "<"),
        ("5:16:boundary:0", "50", "49"),
        ("5:16:boundary:1", "50", "51"),
        ("6:15:return:0", '"pass"', "None"),
        ("7:11:return:0", '"fail"', "None"),
        ("11:11:return:0", "name in members and name is not None", "None"),
        ("11:16:compare:0", "in", "not in"),
        ("11:27:boolop:0", "and", "or"),
        ("11:36:compare:0", "is not", "is"),
        ("16:4:delete:0", 'tag = record.get("@Tag", "none")', "pass"),
        ("16:21:constant:0", '"@Tag"', '""'),
        ("16:21:strip_at:0", '"@Tag"', '"Tag"'),
        ("16:29:constant:0", '"none"', '""'),
        ("17:11:return:0", 'f"<{tag}>"', "None"),
        ("21:4:delete:0", "total = 0", "pass"),
        ("21:12:constant:0", "0", "1"),
        ("23:8:delete:0", "total += value", "pass"),
        ("24:4:delete:0", "log.append(total)", "pass"),
        ("25:11:return:0", "total", "None"),
        ("29:7:negate:0", "not flag", "flag"),
        ("30:8:delete:0", 'raise ValueError("flag required")', "pass"),
        ("30:25:constant:0", '"flag required"', '""'),
        ("31:11:return:0", "True", "False"),
    )
]
EXPECTED_WEAK_SURVIVORS = [
    f"{TOY_PATH}:{suffix}"
    for suffix in (
        "5:16:boundary:0",
        "5:16:boundary:1",
        "7:11:return:0",
        "11:27:boolop:0",
        "16:21:constant:0",
        "16:21:strip_at:0",
        "16:29:constant:0",
        "24:4:delete:0",
        "30:8:delete:0",
        "30:25:constant:0",
    )
]


def toy_source(name="toy_target.py"):
    return (TOYS / name).read_bytes().decode("utf-8")


def make_toy_repo(parent):
    """Copy the toy tree (ci.py, the tests package marker and tests/mutation_toys) into parent/"repo"."""
    repo = Path(parent) / "repo"
    for entry in TOY_COPY:
        source, destination = REPO / entry, repo / entry
        if source.is_dir():
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return repo


def tree_snapshot(root):
    """Every directory (None) and file (sha256) under root, keyed by POSIX relative path."""
    snapshot = {}
    for directory, directories, files in os.walk(root):
        relative = Path(directory).relative_to(root)
        for name in directories:
            snapshot[(relative / name).as_posix()] = None
        for name in files:
            snapshot[(relative / name).as_posix()] = hashlib.sha256((Path(directory) / name).read_bytes()).hexdigest()
    return snapshot


def gen(src, **keywords):
    return mutation.generate_mutants("m.py", src, **keywords)


def mutated(src, **keywords):
    return [(m.id, mutation.apply_mutant(src, m)) for m in gen(src, **keywords)]


class GenerationTests(unittest.TestCase):
    def test_compare_flips_every_operator_in_place(self):
        pairs = {
            "<": ">=",
            ">=": "<",
            ">": "<=",
            "<=": ">",
            "==": "!=",
            "!=": "==",
            "is": "is not",
            "is not": "is",
            "in": "not in",
            "not in": "in",
        }
        for operator, flipped in pairs.items():
            with self.subTest(operator=operator):
                self.assertEqual(mutated(f"x = a {operator} b\n"), [("m.py:1:6:compare:0", f"x = a {flipped} b\n")])

    def test_compare_chain_and_parentheses_replace_only_operator_tokens(self):
        self.assertEqual(
            mutated("x = a < b > c\n"),
            [("m.py:1:6:compare:0", "x = a >= b > c\n"), ("m.py:1:10:compare:0", "x = a < b <= c\n")],
        )
        self.assertEqual(mutated("x = (a) < (b)\n"), [("m.py:1:8:compare:0", "x = (a) >= (b)\n")])
        self.assertEqual(
            mutated("x = (a or b) < c\n"),
            [("m.py:1:7:boolop:0", "x = (a and b) < c\n"), ("m.py:1:13:compare:0", "x = (a or b) >= c\n")],
        )
        self.assertEqual(
            mutated("x = (a <  # note\n     b)\n"), [("m.py:1:7:compare:0", "x = (a >=  # note\n     b)\n")]
        )
        self.assertEqual(mutated("x = a not  in b\n"), [("m.py:1:6:compare:0", "x = a in b\n")])

    def test_boundary_shifts_int_operands_down_then_up(self):
        self.assertEqual(
            mutated("x = a < 5\n"),
            [
                ("m.py:1:6:compare:0", "x = a >= 5\n"),
                ("m.py:1:8:boundary:0", "x = a < 4\n"),
                ("m.py:1:8:boundary:1", "x = a < 6\n"),
            ],
        )
        self.assertEqual(
            mutated("x = 5 <= a\n"),
            [
                ("m.py:1:4:boundary:0", "x = 4 <= a\n"),
                ("m.py:1:4:boundary:1", "x = 6 <= a\n"),
                ("m.py:1:6:compare:0", "x = 5 > a\n"),
            ],
        )
        self.assertEqual(
            mutated("x = a == True\n"),
            [("m.py:1:6:compare:0", "x = a != True\n"), ("m.py:1:9:constant:0", "x = a == False\n")],
        )
        self.assertEqual(
            mutated("x = a < -1\n"),
            [
                ("m.py:1:6:compare:0", "x = a >= -1\n"),
                ("m.py:1:8:boundary:0", "x = a < -2\n"),
                ("m.py:1:8:boundary:1", "x = a < 0\n"),
            ],
        )

    def test_negate_wraps_compound_tests_and_unwraps_not(self):
        body = "\n    pass\n"
        self.assertEqual(mutated("if a:" + body), [("m.py:1:3:negate:0", "if not a:" + body)])
        self.assertEqual(
            mutated("if a < b:" + body),
            [("m.py:1:3:negate:0", "if not (a < b):" + body), ("m.py:1:5:compare:0", "if a >= b:" + body)],
        )
        self.assertEqual(mutated("if not a:" + body), [("m.py:1:3:negate:0", "if a:" + body)])
        self.assertEqual(
            mutated("if not (a or b):" + body),
            [("m.py:1:3:negate:0", "if (a or b):" + body), ("m.py:1:10:boolop:0", "if not (a and b):" + body)],
        )
        self.assertEqual(mutated("while ok():" + body), [("m.py:1:6:negate:0", "while not ok():" + body)])
        self.assertEqual(mutated("y = b if a else c\n"), [("m.py:1:9:negate:0", "y = b if not a else c\n")])
        self.assertEqual(
            mutated("if a:\n    pass\nelif b:\n    pass\n"),
            [
                ("m.py:1:3:negate:0", "if not a:\n    pass\nelif b:\n    pass\n"),
                ("m.py:3:5:negate:0", "if a:\n    pass\nelif not b:\n    pass\n"),
            ],
        )

    def test_boolop_swaps_each_token(self):
        self.assertEqual(
            mutated("x = a and b or c\n"),
            [("m.py:1:6:boolop:0", "x = a or b or c\n"), ("m.py:1:12:boolop:0", "x = a and b and c\n")],
        )
        self.assertEqual(
            mutated("x = a and b and c\n"),
            [("m.py:1:6:boolop:0", "x = a or b and c\n"), ("m.py:1:12:boolop:0", "x = a and b or c\n")],
        )

    def test_constant_replacement_table(self):
        cases = {
            "x = True\n": "x = False\n",
            "x = False\n": "x = True\n",
            "x = 0\n": "x = 1\n",
            "x = 7\n": "x = 8\n",
            "x = 2.5\n": "x = 3.5\n",
            "x = 'hi'\n": 'x = ""\n',
            'x = b"z"\n': 'x = b""\n',
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(mutated(source), [("m.py:1:4:constant:0", expected)])
        for source in ('x = ""\n', "x = None\n", "x = ...\n", "x = 1j\n", 'x = b""\n'):
            with self.subTest(source=source):
                self.assertEqual(mutated(source), [])
        self.assertEqual(mutated("def f():\n    return 0\n"), [("m.py:2:11:return:0", "def f():\n    return None\n")])

    def test_return_replacement(self):
        self.assertEqual(mutated("def f(x):\n    return x\n"), [("m.py:2:11:return:0", "def f(x):\n    return None\n")])
        self.assertEqual(
            mutated("def f():\n    return True\n"), [("m.py:2:11:return:0", "def f():\n    return False\n")]
        )
        self.assertEqual(
            mutated("def f():\n    return False\n"), [("m.py:2:11:return:0", "def f():\n    return True\n")]
        )
        self.assertEqual(mutated("def f():\n    return None\n"), [])
        self.assertEqual(mutated("def f():\n    return\n"), [])

    def test_statement_deletion_only_in_function_bodies(self):
        source = "def f(log):\n    y = 1\n    y += 2\n    log.append(y)\n    raise E\n    y\n"
        lines = source.splitlines(keepends=True)

        def deleted(number):
            return "".join(lines[: number - 1] + ["    pass\n"] + lines[number:])

        self.assertEqual(
            mutated(source),
            [
                ("m.py:2:4:delete:0", deleted(2)),
                ("m.py:2:8:constant:0", source.replace("y = 1", "y = 2")),
                ("m.py:3:4:delete:0", deleted(3)),
                ("m.py:3:9:constant:0", source.replace("y += 2", "y += 3")),
                ("m.py:4:4:delete:0", deleted(4)),
                ("m.py:5:4:delete:0", deleted(5)),
            ],
        )
        self.assertEqual(
            mutated("def f():\n    y = (\n        1\n    )\n"),
            [
                ("m.py:2:4:delete:0", "def f():\n    pass\n"),
                ("m.py:3:8:constant:0", "def f():\n    y = (\n        2\n    )\n"),
            ],
        )
        self.assertEqual(mutated("y = 1\nlog.append(y)\n"), [("m.py:1:4:constant:0", "y = 2\nlog.append(y)\n")])
        klass = "class C:\n    x = 1\n    def m(self):\n        self.y = 2\n"
        self.assertEqual(
            mutated(klass),
            [
                ("m.py:2:8:constant:0", klass.replace("x = 1", "x = 2")),
                ("m.py:4:8:delete:0", klass.replace("self.y = 2", "pass")),
                ("m.py:4:17:constant:0", klass.replace("self.y = 2", "self.y = 3")),
            ],
        )
        # A class body is not a function body, also when the class is defined inside a function.
        nested = "def f():\n    class C:\n        x = 1\n    return C\n"
        self.assertEqual([m.id for m in gen(nested)], ["m.py:3:12:constant:0", "m.py:4:11:return:0"])

    def test_strip_at_keeps_quote_style_and_applies_in_data_tables(self):
        self.assertEqual(
            mutated('v = d["@Id"]\n'),
            [("m.py:1:6:constant:0", 'v = d[""]\n'), ("m.py:1:6:strip_at:0", 'v = d["Id"]\n')],
        )
        self.assertEqual(mutated("v = d['@Id']\n")[1], ("m.py:1:6:strip_at:0", "v = d['Id']\n"))
        self.assertEqual(mutated('T = {"@Id": 1}\n'), [("m.py:1:5:strip_at:0", 'T = {"Id": 1}\n')])
        self.assertEqual(mutated('v = "@"\n'), [("m.py:1:4:constant:0", 'v = ""\n')])
        self.assertEqual(mutated('v = r"@Id"\n')[1], ("m.py:1:4:strip_at:0", "v = 'Id'\n"))

    def test_docstrings_fstrings_annotations_and_main_guard_are_skipped(self):
        self.assertEqual(mutated('"""Doc."""\nx = f"{a < 1}"\n'), [])
        self.assertEqual(mutated('def f():\n    """Doc."""\n    "stray"\n'), [])
        self.assertEqual(
            mutated('def f(a: "x" = 1) -> "y":\n    pass\n'),
            [("m.py:1:15:constant:0", 'def f(a: "x" = 2) -> "y":\n    pass\n')],
        )
        self.assertEqual(mutated('x: "T" = 0\n'), [("m.py:1:9:constant:0", 'x: "T" = 1\n')])
        self.assertEqual(mutated('if __name__ == "__main__":\n    main(1 < 2)\n'), [])
        self.assertEqual(mutated('if "__main__" == __name__:\n    main(1 < 2)\n'), [])
        # Only an equality between __name__ and "__main__" is the guard.
        for test in ('__name__ != "__main__"', '__name__ == "other"', 'name == "__main__"'):
            with self.subTest(test=test):
                self.assertIn("m.py:1:3:negate:0", [m.id for m in gen(f"if {test}:\n    main()\n")])

    def test_strings_inside_literal_subscripts_are_skipped(self):
        # Regression (benchmark N1): `EaseType = Literal["ease_out", ...]` in sdk/game_formulas.py is a typing alias,
        # so its constant mutants could only be killed by a typing.get_args() test.
        source = (
            "import typing\n"
            "import typing as t\n"
            "from typing import Literal\n"
            'A = Literal["x", "@y"]\n'
            'B = t.Literal["z", 0]\n'
            'C = typing.Literal[Literal["w"], "v"]\n'
            'D = Optional["kept"]\n'
        )
        self.assertEqual(
            [(m.id, m.original, m.replacement) for m in gen(source)],
            [("m.py:7:13:constant:0", '"kept"', '""')],
        )

    def test_pragma_no_mutate_excludes_its_line(self):
        self.assertEqual(
            mutated("x = 1  # pragma: no mutate\ny = 2\n"),
            [("m.py:2:4:constant:0", "x = 1  # pragma: no mutate\ny = 3\n")],
        )
        source = toy_source()
        self.assertFalse([m for m in mutation.generate_mutants(TOY_PATH, source) if m.line == 35])
        unpragmatic = source.replace("  # pragma: no mutate", "")
        line35 = [
            (m.id.removeprefix(f"{TOY_PATH}:"), m.original, m.replacement)
            for m in mutation.generate_mutants(TOY_PATH, unpragmatic)
            if m.line == 35
        ]
        self.assertEqual(
            line35,
            [
                ("35:11:constant:0", '"neg"', '""'),
                ("35:11:return:0", '"neg" if number < 0 else "pos"', "None"),
                ("35:20:negate:0", "number < 0", "not (number < 0)"),
                ("35:27:compare:0", "<", ">="),
                ("35:29:boundary:0", "0", "-1"),
                ("35:29:boundary:1", "0", "1"),
                ("35:36:constant:0", '"pos"', '""'),
            ],
        )

    def test_data_tables_are_skipped_unless_enabled(self):
        for source in (
            "T = {1: 2}\n",
            "T = [1]\n",
            "T = (1,)\n",
            "T = {1}\n",
            "T: dict = {1: 2}\n",
            'T = {"a": [1, 2]}\n',
        ):
            with self.subTest(source=source):
                self.assertEqual(mutated(source), [])
        self.assertEqual(
            [m.id for m in gen("T = {1: 2}\n", mutate_data_tables=True)], ["m.py:1:5:constant:0", "m.py:1:8:constant:0"]
        )
        self.assertEqual(
            [m.id for m in gen("def f():\n    return {1: 2}\n")],
            ["m.py:2:11:return:0", "m.py:2:12:constant:0", "m.py:2:15:constant:0"],
        )

    def test_columns_are_character_offsets_on_non_ascii_lines(self):
        source = 'x = "é" if a < 1 else 2\n'
        self.assertEqual(
            mutated(source),
            [
                ("m.py:1:4:constant:0", 'x = "" if a < 1 else 2\n'),
                ("m.py:1:11:negate:0", 'x = "é" if not (a < 1) else 2\n'),
                ("m.py:1:13:compare:0", 'x = "é" if a >= 1 else 2\n'),
                ("m.py:1:15:boundary:0", 'x = "é" if a < 0 else 2\n'),
                ("m.py:1:15:boundary:1", 'x = "é" if a < 2 else 2\n'),
                ("m.py:1:22:constant:0", 'x = "é" if a < 1 else 3\n'),
            ],
        )

    def test_crlf_line_endings_are_preserved(self):
        source = "def f(a):\r\n    return a < 1\r\n"
        results = mutated(source)
        self.assertEqual(results[0], ("m.py:2:11:return:0", "def f(a):\r\n    return None\r\n"))
        self.assertEqual(len(results), 4)
        for mutant_id, text in results:
            with self.subTest(mutant_id=mutant_id):
                self.assertEqual(text.count("\r\n"), 2)
                self.assertEqual(text.count("\n"), 2)

    def test_lines_split_only_where_ast_splits_them(self):
        # A form feed and U+2028 are line breaks to str.splitlines, but not to ast and tokenize.
        source = 'x = 1\n\x0cdef f(a):\n    return a < " "\ny = 2\n'
        self.assertEqual(
            [(m.id, m.original) for m in gen(source)],
            [
                ("m.py:1:4:constant:0", "1"),
                ("m.py:3:11:return:0", 'a < " "'),
                ("m.py:3:13:compare:0", "<"),
                ("m.py:3:15:constant:0", '" "'),
                ("m.py:4:4:constant:0", "2"),
            ],
        )
        self.assertEqual(
            dict(mutated(source))["m.py:4:4:constant:0"], 'x = 1\n\x0cdef f(a):\n    return a < " "\ny = 3\n'
        )

    def test_apply_mutant_rejects_a_mutant_that_does_not_match_the_source(self):
        (mutant,) = gen("x = a < b\n")
        with self.assertRaises(ValueError):
            mutation.apply_mutant("x = a > b\n", mutant)
        with self.assertRaises(ValueError):
            mutation.apply_mutant("", mutant)

    def test_apply_mutant_rejects_a_span_whose_columns_run_past_its_lines(self):
        source = "x = 1\ny = 2\n"
        well_formed = mutation.Mutant("m.py", 1, 0, 2, 1, "constant", 0, "x = 1\ny", "z")
        self.assertEqual(mutation.apply_mutant(source, well_formed), "z = 2\n")
        # The same characters addressed as line 1, column 7: the text matches, but the end column overruns line 1.
        with self.assertRaises(ValueError):
            mutation.apply_mutant(source, replace(well_formed, end_line=1, end_col=7))
        # Lines outside the source, or a start after the end: each original equals the characters the span would
        # slice, so only the range check can reject it.
        for line, end_line, original in ((0, 1, ""), (2, 1, ""), (2, 3, "y = 2\n")):
            outside = replace(well_formed, line=line, col=0, end_line=end_line, end_col=0, original=original)
            with self.subTest(line=line, end_line=end_line), self.assertRaisesRegex(ValueError, "outside"):
                mutation.apply_mutant(source, outside)

    def test_toy_target_mutants_match_the_expected_list(self):
        mutants = mutation.generate_mutants(TOY_PATH, toy_source())
        self.assertEqual([(m.id, m.original, m.replacement) for m in mutants], EXPECTED_TOY)

    def test_generation_is_deterministic_and_ids_survive_appended_code(self):
        source = toy_source()
        first = mutation.generate_mutants(TOY_PATH, source)
        self.assertEqual(first, mutation.generate_mutants(TOY_PATH, source))
        extended = mutation.generate_mutants(TOY_PATH, source + "\n\ndef extra():\n    return 1\n")
        self.assertEqual([m.id for m in extended][: len(first)], [m.id for m in first])
        self.assertGreater(len(extended), len(first))

    def test_generate_mutants_reads_the_file_when_no_source_is_given(self):
        self.assertEqual(
            mutation.generate_mutants(TOYS / "toy_target.py"),
            mutation.generate_mutants(TOYS / "toy_target.py", toy_source()),
        )

    def test_every_toy_mutant_compiles(self):
        source = toy_source()
        mutants = mutation.generate_mutants(TOY_PATH, source)
        self.assertEqual(len(mutants), 24)
        for mutant in mutants:
            with self.subTest(mutant=mutant.id):
                compile(mutation.apply_mutant(source, mutant), TOY_PATH, "exec", dont_inherit=True)


SCOPED_SOURCE = (
    "x = 1\n"
    "def outer(a):\n"
    "    y = 2\n"
    "    def inner():\n"
    "        return 3\n"
    "    return inner\n"
    "class C:\n"
    "    z = 4\n"
    "    def m(self):\n"
    "        return 5\n"
    "    async def n(self):\n"
    "        return 6\n"
)
ALL_OPERATORS = mutation.OPERATORS + mutation.EXTRA_OPERATORS


class ScopeAndOperatorTests(unittest.TestCase):
    """Function scoping (Mutant.scope, functions=) and operator selection (operators=, the opt-in operators)."""

    def test_every_mutant_records_its_enclosing_qualname(self):
        self.assertEqual(
            [(m.id, m.scope) for m in gen(SCOPED_SOURCE)],
            [
                ("m.py:1:4:constant:0", ""),
                ("m.py:3:4:delete:0", "outer"),
                ("m.py:3:8:constant:0", "outer"),
                ("m.py:5:15:return:0", "outer.inner"),
                ("m.py:6:11:return:0", "outer"),
                ("m.py:8:8:constant:0", "C"),
                ("m.py:10:15:return:0", "C.m"),
                ("m.py:12:15:return:0", "C.n"),
            ],
        )
        self.assertEqual(gen(SCOPED_SOURCE)[3].to_dict()["scope"], "outer.inner")

    def test_functions_keeps_mutants_in_or_nested_inside_a_listed_qualname(self):
        everything = {m.id: m for m in gen(SCOPED_SOURCE)}
        cases = {
            ("outer",): ["m.py:3:4:delete:0", "m.py:3:8:constant:0", "m.py:5:15:return:0", "m.py:6:11:return:0"],
            ("outer.inner",): ["m.py:5:15:return:0"],
            ("C",): ["m.py:8:8:constant:0", "m.py:10:15:return:0", "m.py:12:15:return:0"],
            ("C.n", "outer.inner"): ["m.py:5:15:return:0", "m.py:12:15:return:0"],
        }
        for functions, expected in cases.items():
            with self.subTest(functions=functions):
                selected = gen(SCOPED_SOURCE, functions=functions)
                self.assertEqual([m.id for m in selected], expected)
                self.assertEqual(selected, [everything[mutant_id] for mutant_id in expected])
        self.assertEqual([m.id for m in gen("def ab():\n    return 1\n", functions=["ab"])], ["m.py:2:11:return:0"])
        self.assertEqual(gen("def ab():\n    return 1\ndef a():\n    pass\n", functions=["a"]), [])

    def test_a_function_that_matches_no_def_is_a_config_error(self):
        for name in ("x", "inner", "C.missing", "outer.inner.deeper", "Outer"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(mutation.ConfigError, "match no def or class"):
                    gen(SCOPED_SOURCE, functions=[name])
        main_guard = 'def f():\n    return 1\nif __name__ == "__main__":\n    def g():\n        return 2\n'
        self.assertEqual(gen(main_guard, functions=["g"]), [])

    def test_operators_selects_and_unknown_names_are_config_errors(self):
        self.assertEqual(
            [m.id for m in gen(SCOPED_SOURCE, operators=["return"])],
            ["m.py:5:15:return:0", "m.py:6:11:return:0", "m.py:10:15:return:0", "m.py:12:15:return:0"],
        )
        with self.assertRaisesRegex(mutation.ConfigError, "unknown operator"):
            gen(SCOPED_SOURCE, operators=["nope"])
        # Reports record the selection as configured, in the configured order.
        self.assertEqual(mutation.check_operators(["return", "compare"]), ("return", "compare"))
        self.assertEqual(mutation.check_operators(None), mutation.OPERATORS)
        self.assertEqual(set(mutation.OPERATORS) & set(mutation.EXTRA_OPERATORS), set())
        self.assertEqual(mutation.EXTRA_OPERATORS, ("arithmetic", "minmax"))

    def test_arithmetic_is_opt_in_and_swaps_only_the_operator_token(self):
        self.assertEqual(gen("x = a + b\n"), [])
        pairs = {"+": "-", "-": "+", "*": "/", "/": "*", "//": "/", "%": "*", "**": "*"}
        for operator, swapped in pairs.items():
            with self.subTest(operator=operator):
                self.assertEqual(
                    mutated(f"x = a {operator} b\n", operators=["arithmetic"]),
                    [("m.py:1:6:arithmetic:0", f"x = a {swapped} b\n")],
                )
        self.assertEqual(
            mutated("def f(t, v):\n    t += v\n    t //= v\n    t **= v\n", operators=["arithmetic"]),
            [
                ("m.py:2:6:arithmetic:0", "def f(t, v):\n    t -= v\n    t //= v\n    t **= v\n"),
                ("m.py:3:6:arithmetic:0", "def f(t, v):\n    t += v\n    t /= v\n    t **= v\n"),
                ("m.py:4:6:arithmetic:0", "def f(t, v):\n    t += v\n    t //= v\n    t *= v\n"),
            ],
        )
        self.assertEqual(
            mutated("x = (a) * (b)\n", operators=["arithmetic"]), [("m.py:1:8:arithmetic:0", "x = (a) / (b)\n")]
        )
        self.assertEqual(mutated("x = a - -b\n", operators=["arithmetic"]), [("m.py:1:6:arithmetic:0", "x = a + -b\n")])
        self.assertEqual(
            mutated("x = a * b + c\n", operators=["arithmetic"]),
            [("m.py:1:6:arithmetic:0", "x = a / b + c\n"), ("m.py:1:10:arithmetic:0", "x = a * b - c\n")],
        )
        for source in ("x = a << b\n", "x = a | b\n", "x = a @ b\n", "x = a & b\n", 'x = f"{a + b}"\n'):
            with self.subTest(source=source):
                self.assertEqual(gen(source, operators=["arithmetic"]), [])

    def test_minmax_swaps_calls_to_the_bare_builtins(self):
        self.assertEqual(gen("x = min(a, b)\n"), [])
        self.assertEqual(
            mutated("x = min(a, b) + max(c)\n", operators=["minmax"]),
            [("m.py:1:4:minmax:0", "x = max(a, b) + max(c)\n"), ("m.py:1:16:minmax:0", "x = min(a, b) + min(c)\n")],
        )
        for source in ("x = o.min(a)\n", "x = min\n", "x = sorted(a)\n"):
            with self.subTest(source=source):
                self.assertEqual(gen(source, operators=["minmax"]), [])

    def test_extra_operators_sort_after_the_defaults_and_leave_default_ids_unchanged(self):
        self.assertEqual(
            [m.id for m in gen("def f(a):\n    return min(a)\n", operators=ALL_OPERATORS)],
            ["m.py:2:11:return:0", "m.py:2:11:minmax:0"],
        )
        source = toy_source()
        everything = mutation.generate_mutants(TOY_PATH, source, operators=ALL_OPERATORS)
        self.assertEqual([m for m in everything if m.operator in mutation.OPERATORS], self.default_toy())
        self.assertEqual(
            [(m.id, m.original, m.replacement) for m in everything if m.operator not in mutation.OPERATORS],
            [(f"{TOY_PATH}:23:14:arithmetic:0", "+=", "-=")],
        )
        self.assertTrue(mutation.MUTANT_ID_RE.match(f"{TOY_PATH}:23:14:arithmetic:0"))

    def default_toy(self):
        return mutation.generate_mutants(TOY_PATH, toy_source())


VALID_TARGET = {"path": TOY_PATH, "tests": ["tests.mutation_toys.strong_suite"], "min_score": 0.5}


def write_config(directory, payload, name="config.json"):
    path = Path(directory) / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


class ConfigTests(unittest.TestCase):
    def test_shipped_config_loads_and_every_target_and_test_module_exists(self):
        config = mutation.load_config(REPO / "automation" / "mutation" / "config.json")
        self.assertTrue(config.targets)
        for entry in config.copy:
            self.assertTrue((REPO / entry).exists(), entry)
        targets = mutation.expand_targets(config, REPO)
        self.assertEqual(len(targets), len({target.path for target in targets}))
        for target in targets:
            with self.subTest(target=target.path):
                self.assertTrue((REPO / target.path).is_file())
                self.assertTrue(target.path.startswith("sdk/"))
                self.assertGreaterEqual(target.min_score, 0.0)
                self.assertLessEqual(target.min_score, 1.0)
                self.assertTrue(target.tests)
                for name in target.tests:
                    self.assertTrue((REPO / (name.replace(".", "/") + ".py")).is_file(), name)

    def test_load_config_parses_a_valid_file(self):
        payload = {
            "schema_version": 1,
            "description": "toy",
            "copy": list(TOY_COPY),
            "targets": [
                dict(VALID_TARGET, equivalent=[f"{TOY_PATH}:24:4:delete:0"], timeout=30, mutate_data_tables=True),
                {"path": LOOP_PATH, "tests": ["tests.mutation_toys.loop_suite"], "min_score": 1},
            ],
        }
        with TemporaryDirectory() as tmp:
            config = mutation.load_config(write_config(tmp, payload))
        self.assertEqual(config.copy, TOY_COPY)
        self.assertEqual(
            config.targets,
            (
                mutation.TargetConfig(
                    TOY_PATH,
                    ("tests.mutation_toys.strong_suite",),
                    0.5,
                    (f"{TOY_PATH}:24:4:delete:0",),
                    30.0,
                    True,
                ),
                mutation.TargetConfig(LOOP_PATH, ("tests.mutation_toys.loop_suite",), 1.0),
            ),
        )
        with TemporaryDirectory() as tmp:
            default = mutation.load_config(write_config(tmp, {"schema_version": 1, "targets": [VALID_TARGET]}))
        self.assertEqual(default.copy, mutation.DEFAULT_COPY_PATHS)

    def test_load_config_rejects_malformed_entries(self):
        def config(**target):
            return {"schema_version": 1, "targets": [dict(VALID_TARGET, **target)]}

        missing_tests = {"schema_version": 1, "targets": [{"path": TOY_PATH, "min_score": 0.5}]}
        cases = {
            "schema_version 2": {"schema_version": 2, "targets": [VALID_TARGET]},
            "schema_version true": {"schema_version": True, "targets": [VALID_TARGET]},
            "schema_version string": {"schema_version": "1", "targets": [VALID_TARGET]},
            "no schema_version": {"targets": [VALID_TARGET]},
            "description not a string": {"schema_version": 1, "description": 1, "targets": [VALID_TARGET]},
            "duplicate copy": {"schema_version": 1, "copy": ["sdk", "sdk"], "targets": [VALID_TARGET]},
            "parent copy": {"schema_version": 1, "copy": ["../sdk"], "targets": [VALID_TARGET]},
            "target not an object": {"schema_version": 1, "targets": [TOY_PATH]},
            "tests not a list": config(tests="tests.a"),
            "windows drive-relative path": config(path="C:sdk\\x.py"),
            "timeout negative": config(timeout=-1),
            "timeout true": config(timeout=True),
            "equivalent not a list": config(equivalent=f"{TOY_PATH}:5:13:compare:0"),
            "empty targets": {"schema_version": 1, "targets": []},
            "targets not a list": {"schema_version": 1, "targets": {}},
            "top level not an object": [1],
            "unknown top-level key": {"schema_version": 1, "targets": [VALID_TARGET], "extra": 1},
            "empty copy": {"schema_version": 1, "copy": [], "targets": [VALID_TARGET]},
            "absolute copy": {"schema_version": 1, "copy": ["/etc"], "targets": [VALID_TARGET]},
            "missing tests": missing_tests,
            "empty tests": config(tests=[]),
            "duplicate tests": config(tests=["tests.a", "tests.a"]),
            "bad test module name": config(tests=["tests/test_x.py"]),
            "missing min_score": {"schema_version": 1, "targets": [{"path": TOY_PATH, "tests": ["tests.a"]}]},
            "min_score 1.5": config(min_score=1.5),
            "min_score negative": config(min_score=-0.1),
            "min_score true": config(min_score=True),
            "min_score string": config(min_score="0.5"),
            "unknown target key": config(extra=1),
            "absolute path": config(path="/etc/passwd.py"),
            "windows absolute path": config(path="C:\\sdk\\x.py"),
            "parent path": config(path="sdk/../../x.py"),
            "empty path": config(path=""),
            "duplicate equivalent id": config(equivalent=[f"{TOY_PATH}:5:13:compare:0"] * 2),
            "malformed equivalent id": config(equivalent=["toy:five"]),
            "timeout zero": config(timeout=0),
            "mutate_data_tables string": config(mutate_data_tables="yes"),
            "non-JSON file": "{not json",
        }
        with TemporaryDirectory() as tmp:
            for name, payload in cases.items():
                with self.subTest(case=name):
                    with self.assertRaises(mutation.ConfigError):
                        mutation.load_config(write_config(tmp, payload))
            with self.subTest(case="missing file"):
                with self.assertRaisesRegex(mutation.ConfigError, "missing.json"):
                    mutation.load_config(Path(tmp) / "missing.json")

    def test_functions_operators_and_aggregate_keys(self):
        glob = "tests/mutation_toys/*_target.py"
        payload = {
            "schema_version": 1,
            "targets": [
                dict(VALID_TARGET, functions=["grade", "C.m"], operators=["compare", "arithmetic"]),
                {"path": glob, "tests": ["tests.a"], "min_score": 0.5, "aggregate": True},
            ],
        }
        with TemporaryDirectory() as tmp:
            config = mutation.load_config(write_config(tmp, payload))
        first, second = config.targets
        self.assertEqual(
            (first.functions, first.operators, first.aggregate), (("grade", "C.m"), ("compare", "arithmetic"), False)
        )
        self.assertEqual((second.functions, second.operators, second.aggregate), (None, None, True))
        self.assertTrue(mutation.MUTANT_ID_RE.match(f"{TOY_PATH}:23:14:arithmetic:0"))

        def config(**target):
            return {"schema_version": 1, "targets": [dict(VALID_TARGET, **target)]}

        cases = {
            "functions not a list": config(functions="grade"),
            "functions empty": config(functions=[]),
            "functions duplicate": config(functions=["grade", "grade"]),
            "functions not a qualname": config(functions=["a b"]),
            "functions trailing dot": config(functions=["C."]),
            "operators unknown": config(operators=["nope"]),
            "operators empty": config(operators=[]),
            "aggregate string": {
                "schema_version": 1,
                "targets": [{"path": glob, "tests": ["tests.a"], "min_score": 0, "aggregate": "yes"}],
            },
            "aggregate without a glob": config(aggregate=True),
        }
        with TemporaryDirectory() as tmp:
            for name, bad in cases.items():
                with self.subTest(case=name):
                    with self.assertRaises(mutation.ConfigError):
                        mutation.load_config(write_config(tmp, bad))

    def test_glob_targets_expand_to_sorted_files_and_empty_globs_fail(self):
        loop_id, toy_id = f"{LOOP_PATH}:7:8:delete:0", f"{TOY_PATH}:5:13:compare:0"
        target = mutation.TargetConfig("tests/mutation_toys/*_target.py", ("tests.a",), 0.5, (toy_id, loop_id))
        expanded = mutation.expand_targets(mutation.Config(TOY_COPY, (target,)), REPO)
        self.assertEqual(
            expanded,
            [
                mutation.TargetConfig(LOOP_PATH, ("tests.a",), 0.5, (loop_id,), glob=target.path),
                mutation.TargetConfig(TOY_PATH, ("tests.a",), 0.5, (toy_id,), glob=target.path),
            ],
        )
        failures = {
            "equivalent outside the glob": mutation.TargetConfig(
                "tests/mutation_toys/*_target.py", ("tests.a",), 0.5, ("sdk/other.py:1:0:delete:0",)
            ),
            "empty glob": mutation.TargetConfig("tests/mutation_toys/*_nothing.py", ("tests.a",), 0.5),
            "missing file": mutation.TargetConfig("tests/mutation_toys/nope.py", ("tests.a",), 0.5),
            "equivalent for another file": mutation.TargetConfig(TOY_PATH, ("tests.a",), 0.5, (loop_id,)),
        }
        for name, bad in failures.items():
            with self.subTest(case=name):
                with self.assertRaises(mutation.ConfigError):
                    mutation.expand_targets(mutation.Config(TOY_COPY, (bad,)), REPO)
        with self.subTest(case="file matched twice"):
            twice = (target, mutation.TargetConfig(TOY_PATH, ("tests.a",), 0.5))
            with self.assertRaisesRegex(mutation.ConfigError, "more than one"):
                mutation.expand_targets(mutation.Config(TOY_COPY, twice), REPO)
        with self.subTest(case="only .py files"), TemporaryDirectory() as tmp:
            for name in ("pkg/b.py", "pkg/a.py", "pkg/notes.txt", "pkg/data.pyc", "pkg/sub/c.py"):
                (Path(tmp) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / name).write_text("x = 1\n", encoding="utf-8")
            star = mutation.TargetConfig("pkg/*", ("tests.a",), 0.5)
            self.assertEqual(
                [item.path for item in mutation.expand_targets(mutation.Config(("pkg",), (star,)), tmp)],
                ["pkg/a.py", "pkg/b.py"],
            )


def make_results(path="m.py", operator="constant", **counts):
    """MutantResults with distinct ids: counts maps a status to how many results have it, in STATUSES order."""
    results = []
    for status in mutation.STATUSES:
        for _ in range(counts.get(status, 0)):
            mutant = mutation.Mutant(path, 1, 0, 1, 1, operator, len(results), "1", "2")
            diff = f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-x = 1\n+x = 2\n" if status == "survived" else ""
            results.append(mutation.MutantResult(mutant, status, 0.01, diff))
    return tuple(results)


class FakeRunner:
    """A test_runner that records what each run saw instead of starting a subprocess.

    The first call is the baseline. status decides every later call: a string, or a callable taking the target text.
    raise_on=(call_number, exception) raises from that call instead.
    """

    def __init__(self, target=TOY_PATH, status="failed", baseline=None, raise_on=None):
        self.target = target
        self.status = status
        self.baseline = baseline or mutation.RunResult("passed", 1.0, "OK", tests_run=5)
        self.raise_on = raise_on
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, workspace, tests, timeout):
        text = (Path(workspace) / self.target).read_bytes().decode("utf-8")
        with self.lock:
            self.calls.append((text, timeout, tuple(tests), Path(workspace)))
            number = len(self.calls)
        if number == 1:
            return self.baseline
        if self.raise_on and number == self.raise_on[0]:
            raise self.raise_on[1]
        status = self.status(text) if callable(self.status) else self.status
        return mutation.RunResult(status, 0.01, "", tests_run=5)


class FakeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.repo = make_toy_repo(self.tmp)
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        self.before = tree_snapshot(self.repo)
        self.source = toy_source()
        self.expected = mutation.generate_mutants(TOY_PATH, self.source)

    def tearDown(self):
        self.assertEqual(tree_snapshot(self.repo), self.before)
        self.assertEqual(os.listdir(self.scratch), [])

    def run_toy(self, runner, target=TOY_PATH, tests=("tests.mutation_toys.weak_suite",), **keywords):
        return mutation.run_mutants(
            target,
            tests,
            repo_root=self.repo,
            copy_paths=TOY_COPY,
            scratch_dir=self.scratch,
            test_runner=runner,
            **keywords,
        )

    def test_statuses_and_score_from_runner_outcomes(self):
        killed = self.run_toy(FakeRunner(status="failed"), min_score=1.0)
        self.assertEqual(killed.counts["killed"], 24)
        self.assertEqual(killed.counts["total"], 24)
        self.assertEqual(killed.score, 1.0)
        self.assertTrue(killed.passed)
        self.assertEqual([result.diff for result in killed.results], [""] * 24)

        survived = self.run_toy(FakeRunner(status="passed"), min_score=0.5)
        self.assertEqual(survived.counts["survived"], 24)
        self.assertEqual(survived.score, 0.0)
        self.assertFalse(survived.passed)
        self.assertEqual(len(survived.survivors), 24)
        for result in survived.results:
            self.assertTrue(result.diff.startswith(f"--- a/{TOY_PATH}\n+++ b/{TOY_PATH}\n"), result.diff)
            self.assertIn(
                f"+{mutation.apply_mutant(self.source, result.mutant).splitlines()[result.mutant.line - 1]}",
                result.diff,
            )

        timeout = self.run_toy(FakeRunner(status="timeout"))
        self.assertEqual(timeout.counts["timeout"], 24)
        self.assertEqual(timeout.score, 1.0)
        self.assertEqual([result.mutant for result in timeout.results], self.expected)

    def test_each_run_sees_exactly_one_mutation_and_the_expected_timeouts(self):
        runner = FakeRunner(status="failed", baseline=mutation.RunResult("passed", 4.0, tests_run=5))
        seen = []
        report = self.run_toy(runner, jobs=3, progress=seen.append)
        baseline_text, baseline_timeout, tests, _ = runner.calls[0]
        self.assertEqual(baseline_text, self.source)
        self.assertGreaterEqual(baseline_timeout, 300)
        self.assertEqual(
            Counter(text for text, _, _, _ in runner.calls[1:]),
            Counter(mutation.apply_mutant(self.source, mutant) for mutant in self.expected),
        )
        self.assertEqual({call[2] for call in runner.calls}, {("tests.mutation_toys.weak_suite",)})
        # Regression (final review 5): the timeout scales with the parallel runs (3 here) that contend with each
        # other, not only with the solo baseline: 5 x 4.0 s x 3.
        self.assertEqual(report.timeout, 60.0)
        self.assertEqual({call[1] for call in runner.calls[1:]}, {60.0})
        self.assertEqual(report.baseline_seconds, 4.0)
        self.assertLessEqual(len({call[3] for call in runner.calls}), 3)
        self.assertEqual([result.mutant for result in report.results], self.expected)
        self.assertEqual(sorted(result.mutant.id for result in seen), sorted(m.id for m in self.expected))

        two = FakeRunner(baseline=mutation.RunResult("passed", 4.0, tests_run=5))
        self.assertEqual(self.run_toy(two, jobs=3, mutants=self.expected[:2]).timeout, 40.0, "two mutants: two runs")
        self.assertEqual(self.run_toy(FakeRunner(baseline=two.baseline), jobs=1).timeout, 20.0)
        quick = FakeRunner(baseline=mutation.RunResult("passed", 0.5, tests_run=5))
        self.assertEqual(self.run_toy(quick).timeout, 10.0)
        fixed = FakeRunner()
        self.assertEqual(self.run_toy(fixed, timeout=400.0).timeout, 400.0)
        self.assertEqual(fixed.calls[0][1], 400.0)
        self.assertEqual({call[1] for call in fixed.calls[1:]}, {400.0})

    def test_a_workspace_holds_the_original_source_again_after_each_run(self):
        runner = FakeRunner(status="passed")
        restored = []

        def check(result):
            # progress runs after the mutant's run has finished; with one mutant no other run can reuse the copy.
            restored.append((runner.calls[-1][3] / TOY_PATH).read_bytes() == self.source.encode("utf-8"))

        self.run_toy(runner, mutants=[self.expected[0]], progress=check)
        self.assertEqual(runner.calls[-1][0], mutation.apply_mutant(self.source, self.expected[0]))
        self.assertEqual(restored, [True])

    def test_equivalent_ids_are_not_run_and_leave_the_denominator(self):
        equivalent_id, stale_id = f"{TOY_PATH}:24:4:delete:0", f"{TOY_PATH}:999:0:delete:0"
        (deleted,) = [mutant for mutant in self.expected if mutant.id == equivalent_id]
        runner = FakeRunner(status="passed")
        seen = []
        report = self.run_toy(runner, equivalent=[equivalent_id, stale_id], progress=seen.append)
        self.assertEqual(Counter(result.status for result in seen), Counter(equivalent=1, survived=23))
        self.assertEqual(seen[0].mutant.id, equivalent_id, "results known before any run are reported first")
        self.assertNotIn(mutation.apply_mutant(self.source, deleted), [call[0] for call in runner.calls])
        self.assertEqual(len(runner.calls), 24)
        self.assertEqual(report.counts["equivalent"], 1)
        self.assertEqual(report.counts["survived"], 23)
        self.assertEqual(report.score, 0.0)
        self.assertEqual(report.stale_equivalent, (stale_id,))
        statuses = {result.mutant.id: result.status for result in report.results}
        self.assertEqual(statuses[equivalent_id], "equivalent")

        all_killed = self.run_toy(FakeRunner(status="failed"), equivalent=[equivalent_id])
        self.assertEqual(all_killed.score, 1.0)
        self.assertEqual(all_killed.counts["killed"], 23)

    def test_only_restricts_the_run_and_unknown_ids_fail(self):
        chosen = [self.expected[5].id, self.expected[1].id]
        runner = FakeRunner(status="passed")
        report = self.run_toy(runner, only=chosen)
        self.assertEqual([result.mutant.id for result in report.results], [self.expected[1].id, self.expected[5].id])
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(report.counts["total"], 2)

        untouched = FakeRunner()
        with self.assertRaisesRegex(mutation.ConfigError, "unknown mutant id"):
            self.run_toy(untouched, only=[f"{TOY_PATH}:1:0:delete:0"])
        self.assertEqual(untouched.calls, [])

    def test_uncompilable_mutant_is_incompetent_and_never_run(self):
        broken = mutation.Mutant(TOY_PATH, 5, 13, 5, 15, "compare", 0, ">=", "(")
        runner = FakeRunner(status="passed")
        report = self.run_toy(runner, mutants=[broken])
        self.assertEqual([result.status for result in report.results], ["incompetent"])
        self.assertEqual(report.counts["incompetent"], 1)
        self.assertIsNone(report.score)
        self.assertTrue(report.passed)
        self.assertEqual(len(runner.calls), 1)

    def test_baseline_failure_and_empty_baseline_raise_baseline_error(self):
        baselines = {
            "failed": (mutation.RunResult("failed", 1.0, "AssertionError: boom", tests_run=5), "boom"),
            "timeout": (mutation.RunResult("timeout", 300.0, "", tests_run=None), "timeout"),
            "no tests": (mutation.RunResult("passed", 1.0, "Ran 0 tests", tests_run=0), "ran no tests"),
            "long output": (
                mutation.RunResult("failed", 1.0, "x" * 2 * mutation.OUTPUT_TAIL + "the real error", tests_run=5),
                "the real error$",
            ),
        }
        for name, (baseline, message) in baselines.items():
            with self.subTest(case=name):
                runner = FakeRunner(baseline=baseline)
                with self.assertRaisesRegex(mutation.BaselineError, message):
                    self.run_toy(runner)
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(os.listdir(self.scratch), [])

    def test_runner_crash_leaves_tree_and_scratch_untouched(self):
        runner = FakeRunner(raise_on=(3, RuntimeError("injected crash")))
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.run_toy(runner, jobs=2)
        self.assertLess(len(runner.calls), 25)

    def test_keyboard_interrupt_leaves_tree_and_scratch_untouched(self):
        runner = FakeRunner(raise_on=(3, KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            self.run_toy(runner, jobs=2)
        self.assertLess(len(runner.calls), 25)

    def test_an_interrupted_run_asks_the_runner_to_kill_its_live_children(self):
        class KillableRunner(FakeRunner):
            def __init__(self, **keywords):
                super().__init__(**keywords)
                self.kill_all_calls = 0

            def kill_all(self):
                self.kill_all_calls += 1

        interrupted = KillableRunner(raise_on=(3, KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            self.run_toy(interrupted, jobs=2)
        self.assertEqual(interrupted.kill_all_calls, 1)
        finished = KillableRunner()
        self.run_toy(finished, jobs=2)
        self.assertEqual(finished.kill_all_calls, 0, "a run that finishes normally kills nothing")

    def test_an_unknown_runner_status_stops_the_run_with_a_mutation_error(self):
        # A MutationError exits 2 from main; any other exception would escape as a traceback.
        with self.assertRaisesRegex(mutation.MutationError, "unknown status 'error'"):
            self.run_toy(FakeRunner(status="error"))

    def test_a_baseline_without_a_test_count_still_runs(self):
        # A custom runner need not parse "Ran N tests": only an explicit count of zero means nothing ran.
        runner = FakeRunner(baseline=mutation.RunResult("passed", 1.0, "", tests_run=None))
        self.assertEqual(self.run_toy(runner).counts["killed"], 24)

    def test_paths_and_test_modules_are_validated_before_running(self):
        cases = {
            "outside the root": ("../escape.py", ("tests.mutation_toys.weak_suite",), {}, "outside"),
            "parent segment": ("tests/../tests/mutation_toys/toy_target.py", ("tests.a",), {}, r"\.\."),
            "missing target": ("tests/mutation_toys/nope.py", ("tests.mutation_toys.weak_suite",), {}, "not found"),
            "no tests": (TOY_PATH, (), {}, "no tests mapped"),
            "missing copy path": (
                TOY_PATH,
                ("tests.mutation_toys.weak_suite",),
                {"copy_paths": TOY_COPY + ("nope",)},
                "copy path",
            ),
            "target not copied": (
                TOY_PATH,
                ("tests.mutation_toys.weak_suite",),
                {"copy_paths": ("scripts/ci.py", "tests/__init__.py")},
                "not covered",
            ),
        }
        for name, (target, tests, extra, message) in cases.items():
            with self.subTest(case=name):
                runner = FakeRunner()
                with self.assertRaisesRegex(mutation.ConfigError, message):
                    mutation.run_mutants(
                        target,
                        tests,
                        repo_root=self.repo,
                        scratch_dir=self.scratch,
                        test_runner=runner,
                        copy_paths=extra.get("copy_paths", TOY_COPY),
                    )
                self.assertEqual(runner.calls, [])
        # A copy path covers itself and what is below it, not a sibling that merely shares its prefix.
        self.assertTrue(mutation.is_covered("tests/mutation_toys/a.py", ("tests/mutation_toys",)))
        self.assertTrue(mutation.is_covered("tests/mutation_toys/a.py", ("tests/mutation_toys/",)))
        self.assertTrue(mutation.is_covered("scripts/ci.py", ("scripts/ci.py",)))
        self.assertFalse(mutation.is_covered("tests/mutation_toys/a.py", ("tests/mutation_toy",)))
        self.assertFalse(mutation.is_covered("scripts/ci.py.orig", ("scripts/ci.py",)))
        absolute = self.run_toy(FakeRunner(), target=str(self.repo / TOY_PATH), only=[self.expected[0].id])
        self.assertEqual(absolute.target, TOY_PATH)
        dotted = self.run_toy(
            FakeRunner(), tests=("tests.mutation_toys.weak_suite.WeakToyTests",), only=[self.expected[0].id]
        )
        self.assertEqual(dotted.tests, ("tests.mutation_toys.weak_suite.WeakToyTests",))

    def test_default_runner_requires_ci_py_and_test_modules_but_a_custom_runner_does_not(self):
        default_cases = {
            "missing test module": (("tests.mutation_toys.no_such_suite",), TOY_COPY, "test module not found"),
            "ci.py not copied": (("tests.mutation_toys.weak_suite",), ("tests",), "scripts/ci.py is not covered"),
        }
        for name, (tests, copy_paths, message) in default_cases.items():
            with self.subTest(case=name), patch.object(mutation.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(mutation.ConfigError, message):
                    mutation.run_mutants(
                        TOY_PATH, tests, repo_root=self.repo, copy_paths=copy_paths, scratch_dir=self.scratch
                    )
                popen.assert_not_called()
        runner = FakeRunner()
        report = mutation.run_mutants(
            TOY_PATH,
            ("benchmark:crew-training",),
            repo_root=self.repo,
            copy_paths=("tests/mutation_toys",),
            scratch_dir=self.scratch,
            test_runner=runner,
            only=[self.expected[0].id],
        )
        self.assertEqual(report.counts["killed"], 1)
        self.assertEqual({call[2] for call in runner.calls}, {("benchmark:crew-training",)})
        with self.assertRaisesRegex(mutation.ConfigError, "not covered"):
            mutation.run_mutants(
                TOY_PATH, ("x",), repo_root=self.repo, copy_paths=("scripts/ci.py",), test_runner=FakeRunner()
            )

    def test_functions_and_operators_reach_run_mutants(self):
        runner = FakeRunner(status="failed")
        report = self.run_toy(runner, functions=["tally"], operators=["delete", "arithmetic"])
        self.assertEqual(
            [result.mutant.id for result in report.results],
            [
                f"{TOY_PATH}:21:4:delete:0",
                f"{TOY_PATH}:23:8:delete:0",
                f"{TOY_PATH}:23:14:arithmetic:0",
                f"{TOY_PATH}:24:4:delete:0",
            ],
        )
        self.assertEqual({result.mutant.scope for result in report.results}, {"tally"})
        self.assertEqual(len(runner.calls), 5)
        self.assertIn(
            "total -= value", runner.calls[1][0] + runner.calls[2][0] + runner.calls[3][0] + runner.calls[4][0]
        )
        for keywords, message in (
            ({"functions": ["nope"]}, "match no def or class"),
            ({"operators": ["nope"]}, "unknown operator"),
        ):
            with self.subTest(keywords=keywords):
                untouched = FakeRunner()
                with self.assertRaisesRegex(mutation.ConfigError, message):
                    self.run_toy(untouched, **keywords)
                self.assertEqual(untouched.calls, [])

    def test_zero_generated_mutants_is_a_config_error(self):
        for keywords in ({"functions": ["sign"]}, {"functions": ["grade"], "operators": ["minmax"]}):
            with self.subTest(keywords=keywords):
                runner = FakeRunner()
                with self.assertRaisesRegex(mutation.ConfigError, "no mutants"):
                    self.run_toy(runner, **keywords)
                self.assertEqual(runner.calls, [])

    def test_caller_supplied_mutants_check_only_and_stale_against_the_whole_file(self):
        subset = self.expected[:2]
        stale = f"{TOY_PATH}:999:0:delete:0"
        report = self.run_toy(
            FakeRunner(),
            mutants=subset,
            equivalent=[self.expected[10].id, stale],
            only=[self.expected[1].id, self.expected[12].id],
        )
        self.assertEqual([result.mutant.id for result in report.results], [self.expected[1].id])
        self.assertEqual(report.stale_equivalent, (stale,))
        with self.assertRaisesRegex(mutation.ConfigError, "unknown mutant id"):
            self.run_toy(FakeRunner(), mutants=subset, only=[f"{TOY_PATH}:1:0:delete:0"])

    def test_mixed_statuses_give_the_exact_score_and_denominator(self):
        broken = mutation.Mutant(TOY_PATH, 5, 13, 5, 15, "compare", 1, ">=", "(")
        verdicts = {
            mutation.apply_mutant(self.source, self.expected[1]): "failed",
            mutation.apply_mutant(self.source, self.expected[2]): "timeout",
        }
        runner = FakeRunner(status=lambda text: verdicts.get(text, "passed"))
        report = self.run_toy(
            runner, mutants=[*self.expected[:6], broken], equivalent=[self.expected[0].id], min_score=0.3
        )
        self.assertEqual(
            report.counts,
            {"total": 7, "killed": 1, "survived": 3, "timeout": 1, "incompetent": 1, "equivalent": 1},
        )
        # (killed 1 + timeout 1) / (total 7 - incompetent 1 - equivalent 1)
        self.assertEqual(report.score, 2 / 5)
        self.assertAlmostEqual(report.headroom, 0.1)
        self.assertTrue(report.passed)
        zero = dict.fromkeys(mutation.STATUSES, 0)
        self.assertEqual(
            list(report.counts_by_operator.items()),
            [
                ("compare", dict(zero, killed=1, incompetent=1)),
                ("boundary", dict(zero, timeout=1, survived=1)),
                ("negate", dict(zero, equivalent=1)),
                ("return", dict(zero, survived=2)),
            ],
        )
        payload = report.to_dict()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["counts_by_operator"], report.counts_by_operator)
        self.assertAlmostEqual(payload["headroom"], 0.1)
        self.assertEqual(report.ratchet_hint, f"ratchet: {TOY_PATH} scored 0.400; raise min_score from 0.3 to 0.40")

    def test_main_prints_the_ratchet_hint_and_appends_a_markdown_summary(self):
        config = write_config(self.tmp, {"schema_version": 1, "copy": list(TOY_COPY), "targets": [VALID_TARGET]})
        summary = self.tmp / "summary.md"
        summary.write_text("# Earlier step\n", encoding="utf-8")
        results = make_results(TOY_PATH, killed=9, survived=1)
        crafted = mutation.Report(TOY_PATH, ("tests.mutation_toys.strong_suite",), results, 1.0, 10.0, min_score=0.5)
        with patch.object(mutation, "run_mutants", return_value=crafted) as run:
            code, stdout, stderr = run_main(
                ["--repo-root", self.repo, "--config", config, "--quiet", "--summary", summary]
            )
        self.assertEqual(code, 0, stderr)
        self.assertIn(f"ratchet: {TOY_PATH} scored 0.900; raise min_score from 0.5 to 0.90\n", stderr)
        self.assertNotIn("ratchet", stdout)
        text = summary.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Earlier step\n\n## Mutation testing\n\n"), text)
        self.assertIn(crafted.format_markdown(), text)
        self.assertEqual(run.call_args.kwargs["operators"], None)
        self.assertEqual(run.call_args.kwargs["functions"], None)
        with patch.object(mutation, "run_mutants", return_value=crafted):
            unwritable = run_main(["--repo-root", self.repo, "--config", config, "--quiet", "--summary", self.tmp])
        self.assertEqual(unwritable[0], 2)
        self.assertIn("cannot write", unwritable[2])

    def test_operators_and_functions_reach_the_command_line_and_list(self):
        def listed(*arguments, **target):
            config = write_config(
                self.tmp, {"schema_version": 1, "copy": list(TOY_COPY), "targets": [dict(VALID_TARGET, **target)]}
            )
            code, stdout, stderr = run_main(["--repo-root", self.repo, "--config", config, "--list", *arguments])
            self.assertEqual(code, 0, stderr)
            return stdout.splitlines()

        arithmetic = [f"{TOY_PATH}:23:14:arithmetic:0  '+=' -> '-='"]
        self.assertEqual(listed("--operators", "arithmetic"), arithmetic)
        self.assertEqual(listed(operators=["arithmetic"]), arithmetic)
        self.assertEqual(listed("--operators", "arithmetic", operators=["delete"]), arithmetic)
        self.assertEqual(
            listed(functions=["is_member"], operators=["compare"]),
            [f"{TOY_PATH}:11:16:compare:0  'in' -> 'not in'", f"{TOY_PATH}:11:36:compare:0  'is not' -> 'is'"],
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            mutation.main(["--repo-root", str(self.repo), "--operators", "nope", "--list"])
        self.assertEqual(raised.exception.code, 2)
        config = write_config(
            self.tmp, {"schema_version": 1, "copy": list(TOY_COPY), "targets": [dict(VALID_TARGET, functions=["nope"])]}
        )
        code, _, stderr = run_main(["--repo-root", self.repo, "--config", config, "--quiet"])
        self.assertEqual(code, 2)
        self.assertIn("match no def or class", stderr)

    def test_aggregate_glob_gates_the_combined_score(self):
        glob = "tests/mutation_toys/*_target.py"
        originals = {name: (self.repo / name).read_bytes() for name in (TOY_PATH, LOOP_PATH)}

        class SplitRunner:
            """Kills every toy_target mutant and lets every loop_target mutant survive."""

            def __call__(self, workspace, tests, timeout):
                changed = [name for name, data in originals.items() if (Path(workspace) / name).read_bytes() != data]
                status = "failed" if changed == [TOY_PATH] else "passed"
                return mutation.RunResult(status, 0.5 if not changed else 0.01, "", tests_run=5)

        def run(min_score, aggregate=True, targets=None):
            target = {"path": glob, "tests": ["tests.mutation_toys.weak_suite"], "min_score": min_score}
            if aggregate:
                target["aggregate"] = True
            targets = [target] if targets is None else targets
            config = write_config(self.tmp, {"schema_version": 1, "copy": list(TOY_COPY), "targets": targets})
            report_path = self.tmp / "aggregate.json"
            arguments = ["--repo-root", self.repo, "--config", config, "--quiet", "--scratch-dir", self.scratch]
            with patch.object(mutation, "SubprocessTestRunner", SplitRunner):
                code, stdout, stderr = run_main([*arguments, "--json", report_path])
            return code, stdout, stderr, json.loads(report_path.read_text(encoding="utf-8"))

        code, stdout, stderr, payload = run(0.6)
        self.assertEqual(code, 0, stderr)
        self.assertTrue(payload["passed"])
        self.assertEqual([report["target"] for report in payload["reports"]], [LOOP_PATH, TOY_PATH, glob])
        loop, toy, combined = payload["reports"]
        self.assertEqual((loop["score"], loop["passed"], loop["part_of"]), (0.0, True, glob))
        self.assertEqual((toy["score"], toy["part_of"]), (1.0, glob))
        self.assertEqual(combined["counts"]["total"], 35)
        self.assertAlmostEqual(combined["score"], 24 / 35)
        self.assertEqual((combined["passed"], combined["part_of"], combined["min_score"]), (True, None, 0.6))
        self.assertIn(f"score 0.0% (gated as part of {glob})", stdout)
        self.assertIn(f"{glob}: 35 mutants, 24 killed, 11 survived", stdout)
        self.assertIn("score 68.6% (minimum 60.0%) PASS", stdout)
        self.assertIn(f"ratchet: {glob} scored 0.686; raise min_score from 0.6 to 0.68\n", stderr)

        failing = run(0.7)
        self.assertEqual(failing[0], 1)
        self.assertFalse(failing[3]["reports"][-1]["passed"])
        separate = run(0.6, aggregate=False)
        self.assertEqual(separate[0], 1)
        self.assertEqual([report["target"] for report in separate[3]["reports"]], [LOOP_PATH, TOY_PATH])
        self.assertEqual([report["passed"] for report in separate[3]["reports"]], [False, True])

        # Two aggregate globs in a row: each combined report holds only its own glob's results.
        groups = [
            {"path": path, "tests": ["tests.mutation_toys.weak_suite"], "min_score": minimum, "aggregate": True}
            for path, minimum in (("tests/mutation_toys/loop_t*.py", 0.0), ("tests/mutation_toys/toy_t*.py", 0.9))
        ]
        code, _, stderr, payload = run(0.0, targets=groups)
        self.assertEqual(code, 0, stderr)
        summary = [(report["target"], report["counts"]["total"], report["score"]) for report in payload["reports"]]
        self.assertEqual(
            summary,
            [
                (LOOP_PATH, 11, 0.0),
                ("tests/mutation_toys/loop_t*.py", 11, 0.0),
                (TOY_PATH, 24, 1.0),
                ("tests/mutation_toys/toy_t*.py", 24, 1.0),
            ],
        )

    def test_main_returns_130_on_interrupt(self):
        config = write_config(self.tmp, {"schema_version": 1, "copy": list(TOY_COPY), "targets": [VALID_TARGET]})
        stderr = io.StringIO()
        with patch.object(mutation, "run_mutants", side_effect=KeyboardInterrupt), redirect_stderr(stderr):
            code = mutation.main(["--repo-root", str(self.repo), "--config", str(config), "--quiet"])
        self.assertEqual(code, 130)
        self.assertIn("mutation: interrupted; temporary copies removed", stderr.getvalue())

    @unittest.skipUnless(hasattr(signal, "SIGTERM"), "SIGTERM is not available here")
    def test_sigterm_becomes_an_interrupt_and_the_old_handler_is_restored(self):
        config = write_config(self.tmp, {"schema_version": 1, "copy": list(TOY_COPY), "targets": [VALID_TARGET]})

        def previous(signum, frame):
            """A handler other than SIG_DFL, so restoring "the default" is not mistaken for restoring this one."""

        original = signal.signal(signal.SIGTERM, previous)
        self.addCleanup(signal.signal, signal.SIGTERM, original)

        def deliver_sigterm(*arguments, **keywords):
            handler = signal.getsignal(signal.SIGTERM)
            if not callable(handler):
                self.fail(f"main did not install a SIGTERM handler: {handler!r}")
            handler(signal.SIGTERM, None)

        with patch.object(mutation, "run_mutants", side_effect=deliver_sigterm), redirect_stderr(io.StringIO()):
            code = mutation.main(["--repo-root", str(self.repo), "--config", str(config), "--quiet"])
        self.assertEqual(code, 130)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


def run_main(arguments):
    """mutation.main(arguments) with stdout and stderr captured: (exit code, stdout, stderr)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = mutation.main([str(argument) for argument in arguments])
    return code, stdout.getvalue(), stderr.getvalue()


def toy_config(directory, name, suite, min_score, **target):
    entry = {"path": TOY_PATH, "tests": [f"tests.mutation_toys.{suite}"], "min_score": min_score, "timeout": 60}
    entry.update(target)
    return write_config(directory, {"schema_version": 1, "copy": list(TOY_COPY), "targets": [entry]}, name)


class EndToEndToyRuns(unittest.TestCase):
    """Real `scripts/ci.py test` subprocesses against copies of the toy repository."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(cls.enterClassContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        cls.repo = make_toy_repo(cls.tmp)
        cls.scratch = cls.tmp / "scratch"
        cls.scratch.mkdir()
        cls.real_toys = {path.name: path.read_bytes() for path in sorted(TOYS.glob("*.py"))}
        cls.before = tree_snapshot(cls.repo)
        cls.weak_config = toy_config(cls.tmp, "weak.config.json", "weak_suite", 0.9)
        cls.strong_config = toy_config(cls.tmp, "strong.config.json", "strong_suite", 1.0)
        common = ["--repo-root", cls.repo, "--quiet", "--jobs", "4", "--scratch-dir", cls.scratch]
        cls.weak = run_main([*common, "--config", cls.weak_config, "--json", cls.tmp / "weak.json"])
        cls.strong = run_main([*common, "--config", cls.strong_config, "--json", cls.tmp / "strong.json"])
        cls.weak_json = json.loads((cls.tmp / "weak.json").read_text(encoding="utf-8"))
        cls.strong_json = json.loads((cls.tmp / "strong.json").read_text(encoding="utf-8"))
        cls.after = tree_snapshot(cls.repo)

    def test_weak_suite_survivors_are_exact(self):
        (report,) = self.weak_json["reports"]
        survivors = [mutant["id"] for mutant in report["mutants"] if mutant["status"] == "survived"]
        self.assertEqual(survivors, EXPECTED_WEAK_SURVIVORS)
        self.assertEqual(
            report["counts"],
            {"total": 24, "killed": 14, "survived": 10, "timeout": 0, "incompetent": 0, "equivalent": 0},
        )
        self.assertAlmostEqual(report["score"], 14 / 24)
        self.assertFalse(report["passed"])

    def test_strong_suite_kills_every_mutant(self):
        (report,) = self.strong_json["reports"]
        self.assertEqual(report["score"], 1.0)
        self.assertTrue(report["passed"])
        self.assertTrue(self.strong_json["passed"])
        self.assertEqual(report["counts"]["killed"], 24)

    def test_gate_exit_codes(self):
        self.assertEqual(self.weak[0], 1, self.weak)
        self.assertIn("FAIL", self.weak[1])
        self.assertEqual(self.strong[0], 0, self.strong)
        self.assertIn("PASS", self.strong[1])
        missing = run_main(["--repo-root", self.repo, "--config", self.tmp / "nope.json", "--quiet"])
        self.assertEqual(missing[0], 2)
        self.assertIn("config file not found", missing[2])
        unknown = run_main(["--repo-root", self.repo, "--config", self.weak_config, "--target", "sdk/nope.py"])
        self.assertEqual(unknown[0], 2)
        self.assertIn("sdk/nope.py", unknown[2])
        no_module = run_main(
            [
                "--repo-root",
                self.repo,
                "--config",
                self.weak_config,
                "--target",
                TOY_PATH,
                "--tests",
                "tests.mutation_toys.no_such_suite",
                "--json",
                self.tmp / "not-written.json",
            ]
        )
        self.assertEqual(no_module[0], 2)
        self.assertIn("test module not found", no_module[2])
        self.assertFalse((self.tmp / "not-written.json").exists())

    def test_command_line_ranges_are_checked_by_argparse(self):
        for arguments in (
            ["--jobs", "0"],
            ["--min-score", "1.5"],
            ["--min-score", "-1"],
            ["--timeout", "0"],
            ["--timeout", "inf"],
            ["--timeout", "nan"],
        ):
            with self.subTest(arguments=arguments):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    mutation.main(["--repo-root", str(self.repo), *arguments])
                self.assertEqual(raised.exception.code, 2)

    def test_json_report_schema(self):
        self.assertEqual(set(self.weak_json), {"schema_version", "passed", "reports"})
        self.assertEqual(self.weak_json["schema_version"], 1)
        self.assertIs(self.weak_json["passed"], False)
        (report,) = self.weak_json["reports"]
        self.assertEqual(
            set(report),
            {
                "target",
                "tests",
                "min_score",
                "score",
                "passed",
                "counts",
                "baseline_seconds",
                "timeout_seconds",
                "stale_equivalent",
                "mutants",
                "schema_version",
                "counts_by_operator",
                "headroom",
                "part_of",
                "subset_of",
                "operators",
                "overrides",
                "partial",
                "no_mutants",
                "gated",
            },
        )
        self.assertEqual(report["operators"], list(mutation.OPERATORS))
        self.assertEqual((report["overrides"], report["partial"], report["gated"]), ([], False, True))
        self.assertEqual(report["schema_version"], 1)
        self.assertAlmostEqual(report["headroom"], 14 / 24 - 0.9)
        by_operator = report["counts_by_operator"]
        self.assertEqual(list(by_operator), [op for op in mutation.OPERATORS if op in by_operator])
        self.assertEqual(
            by_operator["boundary"], {"killed": 0, "survived": 2, "timeout": 0, "incompetent": 0, "equivalent": 0}
        )
        self.assertEqual(sum(sum(counts.values()) for counts in by_operator.values()), 24)
        self.assertEqual(report["target"], TOY_PATH)
        self.assertEqual(report["tests"], ["tests.mutation_toys.weak_suite"])
        self.assertEqual(report["min_score"], 0.9)
        self.assertEqual(report["timeout_seconds"], 60.0)
        self.assertEqual(report["stale_equivalent"], [])
        self.assertEqual(set(report["counts"]), {"total", "killed", "survived", "timeout", "incompetent", "equivalent"})
        keys = {"id", "path", "line", "col", "end_line", "end_col", "operator", "index", "original", "replacement"}
        keys.add("scope")
        self.assertEqual(
            sorted({mutant["scope"] for mutant in report["mutants"]}),
            ["describe", "grade", "is_member", "require", "tally"],
        )
        for mutant in report["mutants"]:
            self.assertEqual(set(mutant), keys | {"status", "seconds", "diff"})
            self.assertEqual(bool(mutant["diff"]), mutant["status"] == "survived", mutant["id"])
        self.assertEqual([(m["id"], m["original"], m["replacement"]) for m in report["mutants"]], EXPECTED_TOY)

    def test_text_report_lists_survivor_diffs(self):
        stdout = self.weak[1]
        self.assertIn(f"{TOY_PATH}: 24 mutants, 14 killed, 10 survived, 0 timeout, 0 incompetent, 0 equivalent", stdout)
        self.assertIn("score 58.3% (minimum 90.0%) FAIL", stdout)
        self.assertIn(f"survived {TOY_PATH}:24:4:delete:0", stdout)
        self.assertIn(f"--- a/{TOY_PATH}", stdout)
        self.assertIn("-    log.append(total)\n+    pass\n", stdout)
        # One line of context around the change, no more.
        self.assertIn(
            f"survived {TOY_PATH}:24:4:delete:0\n--- a/{TOY_PATH}\n+++ b/{TOY_PATH}\n@@ -23,3 +23,3 @@\n"
            "         total += value\n-    log.append(total)\n+    pass\n     return total\n",
            stdout,
        )
        self.assertEqual(stdout.count("\nsurvived "), 10)
        self.assertEqual(self.weak[2], "")
        self.assertIn("score 100.0% (minimum 100.0%) PASS", self.strong[1])

    def test_working_tree_byte_identical_after_real_runs(self):
        self.assertEqual(self.after, self.before)
        self.assertEqual(os.listdir(self.scratch), [])
        self.assertEqual({path.name: path.read_bytes() for path in sorted(TOYS.glob("*.py"))}, self.real_toys)

    def test_list_prints_mutants_without_running(self):
        code, stdout, stderr = run_main(
            ["--repo-root", self.repo, "--config", self.weak_config, "--target", TOY_PATH, "--list"]
        )
        self.assertEqual(code, 0, stderr)
        expected = [
            f"{mutant_id}  {original!r} -> {replacement!r}" for mutant_id, original, replacement in EXPECTED_TOY
        ]
        self.assertEqual(stdout.splitlines(), expected)
        self.assertEqual(os.listdir(self.scratch), [])
        # --list runs nothing, so it writes no report: a {"passed": true} file would claim a run that never happened.
        report_path, summary_path = self.tmp / "list.json", self.tmp / "list.md"
        code, _, stderr = run_main(
            [
                *("--repo-root", self.repo, "--config", self.weak_config, "--list"),
                *("--json", report_path, "--summary", summary_path),
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertFalse(report_path.exists())
        self.assertFalse(summary_path.exists())

    def test_progress_lines_only_and_stale_equivalent_warning(self):
        stale = f"{TOY_PATH}:999:0:delete:0"
        config = toy_config(self.tmp, "stale.config.json", "weak_suite", 0.0, equivalent=[stale])
        chosen = f"{TOY_PATH}:5:13:compare:0"
        code, stdout, stderr = run_main(
            ["--repo-root", self.repo, "--config", config, "--only", chosen, "--scratch-dir", self.scratch]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn(f"[1/1] killed {chosen}\n", stderr)
        self.assertIn(f"warning: {TOY_PATH}: stale equivalent id {stale}", stderr)
        self.assertIn("1 mutants, 1 killed", stdout)
        unknown = run_main(["--repo-root", self.repo, "--config", config, "--only", "sdk/x.py:1:0:delete:0"])
        self.assertEqual(unknown[0], 2)
        self.assertIn("sdk/x.py:1:0:delete:0", unknown[2])

    def test_baseline_failure_exits_2(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo = make_toy_repo(tmp)
            failing = "import unittest\n\n\nclass Broken(unittest.TestCase):\n    def test_fails(self):\n"
            (repo / "tests" / "mutation_toys" / "weak_suite.py").write_text(
                failing + "        self.fail('baseline broken on purpose')\n", encoding="utf-8"
            )
            config = toy_config(tmp, "weak.config.json", "weak_suite", 0.9)
            code, _, stderr = run_main(
                ["--repo-root", repo, "--config", config, "--quiet", "--json", Path(tmp) / "out.json"]
            )
            self.assertEqual(code, 2)
            self.assertIn("baseline", stderr)
            self.assertIn("baseline broken on purpose", stderr)
            self.assertFalse((Path(tmp) / "out.json").exists())

    def test_infinite_loop_mutant_is_reported_as_timeout(self):
        started = time.monotonic()
        report = mutation.run_mutants(
            LOOP_PATH,
            ["tests.mutation_toys.loop_suite"],
            only=[LOOP_DELETE_ID],
            timeout=2.0,
            jobs=1,
            repo_root=self.repo,
            copy_paths=TOY_COPY,
            scratch_dir=self.scratch,
        )
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual([result.status for result in report.results], ["timeout"])
        self.assertEqual(report.score, 1.0)
        self.assertEqual(os.listdir(self.scratch), [])


SLOW_SUITE = "import time\nimport unittest\n\n\nclass Slow(unittest.TestCase):\n    def test_sleeps(self):\n"


class QuickPopen:
    """A stand-in for subprocess.Popen that records its arguments and reports one passing test."""

    instances = []

    def __init__(self, command, **options):
        self.command, self.options, self.pid, self.returncode = command, options, 0, None
        QuickPopen.instances.append(self)

    def communicate(self, timeout=None):
        self.returncode = 0
        return "Ran 1 test in 0.001s\n\nOK\n", None


class HangingPipePopen(QuickPopen):
    """Times out, and after the kill a grandchild outside the process group still holds stdout open."""

    def __init__(self, command, **options):
        super().__init__(command, **options)
        self.stdout = io.StringIO()
        self.timeouts = []

    def communicate(self, timeout=None):
        self.timeouts.append(timeout)
        if timeout is None:
            raise AssertionError("communicate() without a timeout blocks forever while a grandchild holds the pipe")
        raise subprocess.TimeoutExpired("ci.py", timeout)

    def wait(self, timeout=None):
        self.returncode = -9
        return self.returncode


class ReviewRegressionTests(unittest.TestCase):
    """Regression coverage for defects found in review of the first harness version."""

    def setUp(self):
        self.tmp = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.repo = make_toy_repo(self.tmp)
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        QuickPopen.instances = []

    def test_large_float_constants_get_a_different_value(self):
        for literal in ("1e16", "9007199254740992.0", "1.7976931348623157e308"):
            with self.subTest(literal=literal):
                (mutant,) = gen(f"x = {literal}\n")
                self.assertNotEqual(ast.literal_eval(mutant.replacement), ast.literal_eval(literal))
                self.assertTrue(mutation.compiles(mutation.apply_mutant(f"x = {literal}\n", mutant), "m.py"))

    def test_pragma_excludes_every_mutant_whose_span_covers_the_line(self):
        source = "def f():\n    foo(1,\n        2)  # pragma: no mutate\n"
        self.assertEqual([m.id for m in gen(source)], ["m.py:2:8:constant:0"])
        self.assertEqual(gen("def f():\n    return (\n        a < b  # pragma: no mutate\n    )\n"), [])

    def test_awaited_call_statements_are_deleted(self):
        self.assertEqual(
            mutated("async def f():\n    await h()\n"), [("m.py:2:4:delete:0", "async def f():\n    pass\n")]
        )

    def test_a_target_with_no_scored_mutants_has_no_score_and_fails_a_positive_minimum(self):
        result = mutation.MutantResult(
            mutation.Mutant("m.py", 1, 0, 1, 1, "constant", 0, "1", "("), "incompetent", 0, ""
        )
        failing = mutation.Report("m.py", ("tests.a",), (result,), 1.0, 10.0, min_score=0.5)
        self.assertIsNone(failing.score)
        self.assertFalse(failing.passed)
        self.assertIsNone(failing.to_dict()["score"])
        self.assertIn("score n/a (no scored mutants) (minimum 50.0%) FAIL", failing.format_text())
        empty = mutation.Report("m.py", ("tests.a",), (), 1.0, 10.0, min_score=0.0)
        self.assertIsNone(empty.score)
        self.assertTrue(empty.passed)

    def test_interrupt_during_a_test_run_kills_the_child(self):
        suite = self.repo / "tests" / "mutation_toys" / "slow_suite.py"
        suite.write_text(SLOW_SUITE + "        time.sleep(60)\n", encoding="utf-8")
        started = []

        class InterruptedPopen(subprocess.Popen):
            def communicate(self, *arguments, **keywords):
                started.append(self)
                raise KeyboardInterrupt

        with patch.object(mutation.subprocess, "Popen", InterruptedPopen):
            with self.assertRaises(KeyboardInterrupt):
                mutation.SubprocessTestRunner()(self.repo, ("tests.mutation_toys.slow_suite",), 120.0)
        (process,) = started
        self.addCleanup(process.stdout.close)
        self.addCleanup(mutation.kill_process_tree, process)
        self.assertIsNotNone(process.wait(timeout=20), "the interrupted test process is still running")

    def test_timeout_does_not_wait_forever_for_a_pipe_held_by_an_escaped_grandchild(self):
        killed = []
        with (
            patch.object(mutation.subprocess, "Popen", HangingPipePopen),
            patch.object(mutation, "kill_process_tree", killed.append),
        ):
            result = mutation.SubprocessTestRunner()(self.repo, ("tests.a",), 1.0)
        self.assertEqual(result.status, "timeout")
        (process,) = QuickPopen.instances
        self.assertEqual(killed, [process])
        self.assertNotIn(None, process.timeouts)

    def test_test_children_keep_temporary_files_inside_the_workspace(self):
        workspace = self.tmp / "workspace"
        workspace.mkdir()
        with patch.object(mutation.subprocess, "Popen", QuickPopen):
            result = mutation.SubprocessTestRunner()(workspace, ("tests.a",), 5.0)
        self.assertEqual((result.status, result.tests_run), ("passed", 1))
        (process,) = QuickPopen.instances
        env = process.options["env"]
        child_tmp = Path(env["TMPDIR"])
        self.assertEqual({env["TMPDIR"], env["TEMP"], env["TMP"]}, {str(child_tmp)})
        self.assertTrue(child_tmp.is_dir())
        self.assertTrue(child_tmp.is_relative_to(workspace))

    def test_the_last_ran_line_counts_and_a_nonzero_exit_is_a_failure(self):
        # A suite whose tests print a nested "Ran N tests" line: the runner's own summary comes last.
        class NestedOutputPopen(QuickPopen):
            def communicate(self, timeout=None):
                self.returncode = 1
                return (
                    "Ran 0 tests in 0.000s\n" + "." * 2 * mutation.OUTPUT_TAIL + "\nRan 7 tests in 1.0s\n\nFAILED\n",
                    None,
                )

        workspace = self.tmp / "workspace"
        workspace.mkdir()
        with patch.object(mutation.subprocess, "Popen", NestedOutputPopen):
            result = mutation.SubprocessTestRunner()(workspace, ("tests.a",), 5.0)
        self.assertEqual((result.status, result.tests_run), ("failed", 7))
        self.assertEqual(len(result.output), mutation.OUTPUT_TAIL)
        self.assertTrue(result.output.endswith("Ran 7 tests in 1.0s\n\nFAILED\n"))

    def test_a_failed_restore_stops_the_run_instead_of_hanging(self):
        calls = []

        def runner(workspace, tests, timeout):
            calls.append(workspace)
            if len(calls) == 2:
                shutil.rmtree(Path(workspace) / "tests" / "mutation_toys")
            return mutation.RunResult("passed" if len(calls) == 1 else "failed", 0.01, tests_run=1)

        outcome = []

        def run():
            try:
                mutation.run_mutants(
                    TOY_PATH,
                    ("tests.mutation_toys.weak_suite",),
                    repo_root=self.repo,
                    copy_paths=TOY_COPY,
                    scratch_dir=self.scratch,
                    test_runner=runner,
                    jobs=1,
                )
            except BaseException as error:  # noqa: BLE001 - the test inspects whatever escaped
                outcome.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(20)
        self.assertFalse(thread.is_alive(), "run_mutants hung after the target file could not be restored")
        (error,) = outcome
        self.assertIsInstance(error, OSError)
        self.assertEqual(os.listdir(self.scratch), [])

    def config_arguments(self):
        config = write_config(self.tmp, {"schema_version": 1, "copy": list(TOY_COPY), "targets": [VALID_TARGET]})
        return ["--repo-root", self.repo, "--config", config]

    def test_an_explicit_config_path_that_does_not_exist_is_an_error(self):
        missing = self.tmp / "mutaton" / "config.json"
        code, _, stderr = run_main(
            ["--repo-root", self.repo, "--config", missing, "--target", TOY_PATH, "--tests", "tests.mutation_toys.a"]
        )
        self.assertEqual(code, 2)
        self.assertIn("config file not found", stderr)

    def test_filesystem_errors_exit_2_without_a_traceback(self):
        code, _, stderr = run_main([*self.config_arguments(), "--quiet", "--scratch-dir", self.tmp / "missing"])
        self.assertEqual(code, 2)
        self.assertIn("mutation:", stderr)

    def test_duplicate_only_ids_are_counted_once(self):
        chosen = f"{TOY_PATH}:5:13:compare:0"
        arguments = [*self.config_arguments(), "--scratch-dir", self.scratch, "--only", chosen, chosen]
        code, stdout, stderr = run_main(arguments)
        self.assertEqual(code, 0, stderr)
        self.assertIn(f"[1/1] killed {chosen}\n", stderr)
        self.assertIn("1 mutants, 1 killed", stdout)

    def test_tests_and_min_score_need_a_target(self):
        # Without --target they would be silently ignored, gating against the configured floors instead.
        for extra in (["--tests", "tests.mutation_toys.weak_suite"], ["--min-score", "0.5"]):
            with self.subTest(extra=extra):
                code, _, stderr = run_main([*self.config_arguments(), "--list", *extra])
                self.assertEqual(code, 2)
                self.assertIn("--tests and --min-score need --target", stderr)

    def test_an_aggregate_glob_with_no_member_run_adds_no_combined_report(self):
        # --only names a plain target only: the aggregate group ran nothing, so it must not add an empty report that
        # fails its positive minimum.
        targets = [
            {"path": "tests/mutation_toys/loop_t*.py", "tests": ["tests.a"], "min_score": 0.5, "aggregate": True},
            {"path": TOY_PATH, "tests": ["tests.mutation_toys.weak_suite"], "min_score": 0.0},
        ]
        config = write_config(self.tmp, {"schema_version": 1, "copy": list(TOY_COPY), "targets": targets})
        report_path = self.tmp / "only.json"

        original = (self.repo / TOY_PATH).read_bytes()

        class KillingRunner:
            def __call__(self, workspace, tests, timeout):
                mutated = (Path(workspace) / TOY_PATH).read_bytes() != original
                return mutation.RunResult("failed" if mutated else "passed", 0.01, "", tests_run=5)

        chosen = f"{TOY_PATH}:5:13:compare:0"
        arguments = ["--repo-root", self.repo, "--config", config, "--quiet", "--scratch-dir", self.scratch]
        with patch.object(mutation, "SubprocessTestRunner", KillingRunner):
            code, stdout, stderr = run_main([*arguments, "--only", chosen, "--json", report_path])
        self.assertEqual(code, 0, stdout + stderr)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual([report["target"] for report in payload["reports"]], [TOY_PATH])

    def test_list_honours_only(self):
        chosen = f"{TOY_PATH}:5:13:compare:0"
        code, stdout, stderr = run_main([*self.config_arguments(), "--list", "--only", chosen])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout.splitlines(), [f"{chosen}  '>=' -> '<'"])

    def run_surviving(self, *arguments):
        """main() over the toy config with every mutant surviving: (exit code, stdout, stderr, JSON reports)."""

        class SurvivingRunner:
            def __call__(self, workspace, tests, timeout):
                return mutation.RunResult("passed", 0.01, "", tests_run=5)

        report_path = self.tmp / "surviving.json"
        report_path.unlink(missing_ok=True)
        base = [*self.config_arguments(), "--quiet", "--scratch-dir", self.scratch, "--json", report_path]
        with patch.object(mutation, "SubprocessTestRunner", SurvivingRunner):
            code, stdout, stderr = run_main([*base, *arguments])
        reports = json.loads(report_path.read_text(encoding="utf-8"))["reports"] if report_path.exists() else None
        return code, stdout, stderr, reports

    def test_command_line_overrides_are_recorded_and_suppress_the_ratchet_hint(self):
        # Regression (benchmark B1): the hint suggested raising the configured floor after an --operators run.
        results = make_results(TOY_PATH, killed=9, survived=1)
        crafted = mutation.Report(TOY_PATH, ("tests.mutation_toys.strong_suite",), results, 1.0, 10.0, min_score=0.5)
        target = ["--target", TOY_PATH]
        cases = {
            "configured run": ([], []),
            "configured operators in another order": (["--operators", *reversed(mutation.OPERATORS)], []),
            "configured tests": ([*target, "--tests", "tests.mutation_toys.strong_suite"], []),
            "operators": (["--operators", "compare", "arithmetic"], ["operators"]),
            "tests": ([*target, "--tests", "tests.mutation_toys.weak_suite"], ["tests"]),
            "min_score": ([*target, "--min-score", "0.5"], ["min_score"]),
        }
        report_path = self.tmp / "overrides.json"
        for name, (arguments, overrides) in cases.items():
            with self.subTest(case=name):
                with patch.object(mutation, "run_mutants", return_value=crafted):
                    code, _, stderr = run_main([*self.config_arguments(), "--quiet", "--json", report_path, *arguments])
                self.assertEqual(code, 0, stderr)
                (payload,) = json.loads(report_path.read_text(encoding="utf-8"))["reports"]
                self.assertEqual(payload["overrides"], overrides)
                self.assertEqual("ratchet:" in stderr, not overrides, stderr)

    def test_an_operator_override_is_recorded_and_still_gated(self):
        # Regression (benchmark B2): the JSON report did not say which operators produced the score.
        code, stdout, stderr, reports = self.run_surviving("--operators", "arithmetic")
        self.assertEqual(code, 1, stderr)
        (report,) = reports
        self.assertEqual((report["operators"], report["overrides"]), (["arithmetic"], ["operators"]))
        self.assertEqual((report["passed"], report["gated"], report["partial"]), (False, True, False))
        self.assertIn("score 0.0% (minimum 50.0%) FAIL\noperators: arithmetic (operators differ from config)\n", stdout)
        _, stdout, _, (report,) = self.run_surviving("--only", f"{TOY_PATH}:5:13:compare:0")
        self.assertEqual(report["operators"], list(mutation.OPERATORS))
        self.assertIn(f"operators: {', '.join(mutation.OPERATORS)}\n", stdout)

    def test_operators_matching_a_targets_configured_set_are_not_an_override(self):
        configured = mutation.TargetConfig("a.py", ("tests.a",), operators=("compare", "boundary"))
        default = mutation.TargetConfig("b.py", ("tests.a",))
        targets = [configured, default]
        self.assertIs(mutation.apply_operators(Namespace(operators=None), targets), targets)
        replaced = mutation.apply_operators(Namespace(operators=["boundary", "compare", "compare"]), targets)
        self.assertEqual([target.operators for target in replaced], [("boundary", "compare")] * 2)
        self.assertEqual([target.overrides for target in replaced], [(), ("operators",)])
        same_as_default = mutation.apply_operators(Namespace(operators=list(mutation.OPERATORS)), targets)
        self.assertEqual([target.overrides for target in same_as_default], [("operators",), ()])

    def test_partial_runs_are_not_gated_unless_a_minimum_is_given(self):
        # Regression (benchmark O1): `--only <survivor id>` exited 1 against the full-run floor.
        survivor = f"{TOY_PATH}:5:13:compare:0"
        code, stdout, stderr, (report,) = self.run_surviving("--only", survivor)
        self.assertEqual(code, 0, stderr)
        self.assertIn("score 0.0% (partial run: not gated)\n", stdout)
        self.assertEqual((report["partial"], report["gated"], report["passed"]), (True, False, True))
        self.assertNotIn("ratchet:", stderr)
        code, stdout, stderr, (report,) = self.run_surviving(
            "--target", TOY_PATH, "--min-score", "0.5", "--only", survivor
        )
        self.assertEqual(code, 1, stderr)
        self.assertIn("score 0.0% (minimum 50.0%) FAIL\n", stdout)
        self.assertEqual((report["partial"], report["gated"], report["passed"]), (True, True, False))


TOY_GLOB = "tests/mutation_toys/*_target.py"
EMPTY_PATH = "tests/mutation_toys/empty_target.py"


class FinalReviewRegressionTests(unittest.TestCase):
    """Regression coverage for defects found in the final code review of the harness."""

    def setUp(self):
        self.tmp = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.repo = make_toy_repo(self.tmp)
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()

    def originals(self):
        return {
            path.relative_to(self.repo).as_posix(): path.read_bytes()
            for path in (self.repo / "tests" / "mutation_toys").glob("*_target.py")
        }

    def split_runner(self):
        """A runner class that kills every toy_target mutant and lets every other mutant survive."""
        originals = self.originals()

        class SplitRunner:
            def __call__(self, workspace, tests, timeout):
                changed = [name for name, data in originals.items() if (Path(workspace) / name).read_bytes() != data]
                return mutation.RunResult("failed" if changed == [TOY_PATH] else "passed", 0.01, "", tests_run=5)

        return SplitRunner

    def killing_runner(self):
        originals = self.originals()

        class KillingRunner:
            def __call__(self, workspace, tests, timeout):
                changed = any((Path(workspace) / name).read_bytes() != data for name, data in originals.items())
                return mutation.RunResult("failed" if changed else "passed", 0.01, "", tests_run=5)

        return KillingRunner

    def cli(self, targets, *arguments, runner=None):
        """main() over a config of targets: (exit code, stdout, stderr, JSON reports or None)."""
        config = write_config(self.tmp, {"schema_version": 1, "copy": list(TOY_COPY), "targets": targets})
        report_path = self.tmp / "report.json"
        report_path.unlink(missing_ok=True)
        base = ["--repo-root", self.repo, "--config", config, "--quiet", "--scratch-dir", self.scratch]
        with patch.object(mutation, "SubprocessTestRunner", runner or self.split_runner()):
            code, stdout, stderr = run_main([*base, "--json", report_path, *arguments])
        reports = json.loads(report_path.read_text(encoding="utf-8"))["reports"] if report_path.exists() else None
        return code, stdout, stderr, reports

    @staticmethod
    def glob_target(min_score, aggregate=True, **extra):
        target = {"path": TOY_GLOB, "tests": ["tests.mutation_toys.weak_suite"], "min_score": min_score, **extra}
        if aggregate:
            target["aggregate"] = True
        return target

    def add_empty_target(self):
        (self.repo / EMPTY_PATH).write_text('"""Nothing here to mutate."""\n', encoding="utf-8")

    def test_target_naming_one_member_of_an_aggregate_is_not_gated_against_the_glob(self):
        # Regression (final review 1): --target <member> built a one-file "combined" report gated against the glob's
        # min_score, with a ratchet hint for the whole glob.
        code, stdout, stderr, reports = self.cli([self.glob_target(0.9)], "--target", TOY_PATH)
        self.assertEqual(code, 0, stderr)
        self.assertEqual([report["target"] for report in reports], [TOY_PATH])
        (report,) = reports
        self.assertEqual((report["gated"], report["passed"], report["subset_of"]), (False, True, TOY_GLOB))
        self.assertIn(f"score 100.0% (part of {TOY_GLOB}; run the glob to gate)\n", stdout)
        self.assertNotIn("ratchet:", stderr)
        code, stdout, stderr, (report,) = self.cli([self.glob_target(0.9)], "--target", LOOP_PATH)
        self.assertEqual(code, 0, stderr)
        self.assertEqual((report["score"], report["gated"], report["passed"]), (0.0, False, True))
        code, stdout, stderr, (report,) = self.cli([self.glob_target(0.9)], "--target", LOOP_PATH, "--min-score", "0.5")
        self.assertEqual(code, 1, stderr)
        self.assertEqual((report["gated"], report["passed"], report["min_score"]), (True, False, 0.5))
        self.assertIn("score 0.0% (minimum 50.0%) FAIL\n", stdout)
        markdown = mutation.Report(
            TOY_PATH, ("tests.a",), make_results(TOY_PATH, killed=1), 1.0, 10.0, 0.9, subset_of=TOY_GLOB
        ).format_markdown()
        self.assertTrue(markdown.startswith(f"### `{TOY_PATH}`: not gated (part of `{TOY_GLOB}`)\n"), markdown)
        self.assertIn(f"part of `{TOY_GLOB}`; run the glob to gate", markdown)

    def test_target_naming_a_configured_glob_runs_the_whole_group(self):
        # Regression (final review 6): --target <glob> was rejected as "not a target".
        code, stdout, stderr, reports = self.cli([self.glob_target(0.6)], "--target", TOY_GLOB)
        self.assertEqual(code, 0, stderr)
        self.assertEqual([report["target"] for report in reports], [LOOP_PATH, TOY_PATH, TOY_GLOB])
        self.assertAlmostEqual(reports[-1]["score"], 24 / 35)
        self.assertIn(f"ratchet: {TOY_GLOB} scored 0.686; raise min_score from 0.6 to 0.68\n", stderr)
        code, _, stderr, reports = self.cli([self.glob_target(0.6)], "--target", TOY_GLOB, "--min-score", "0.7")
        self.assertEqual(code, 1, stderr)
        self.assertEqual((reports[-1]["min_score"], reports[-1]["overrides"]), (0.7, ["min_score"]))
        code, _, stderr, reports = self.cli([self.glob_target(0.0, aggregate=False)], "--target", TOY_GLOB)
        self.assertEqual(code, 0, stderr)
        self.assertEqual([report["target"] for report in reports], [LOOP_PATH, TOY_PATH])

    def test_target_selection_ignores_unrelated_broken_targets(self):
        # Regression (final review 7): every configured target was expanded and validated, so one broken entry
        # blocked --target runs of the others.
        good = dict(VALID_TARGET, tests=["tests.mutation_toys.weak_suite"])
        broken = [
            {"path": "tests/mutation_toys/missing.py", "tests": ["tests.a"], "min_score": 0.5},
            {"path": "tests/mutation_toys/*_nothing.py", "tests": ["tests.a"], "min_score": 0.5},
            {
                "path": "tests/mutation_toys/loop_t*.py",
                "tests": ["tests.a"],
                "min_score": 0.5,
                "equivalent": ["sdk/elsewhere.py:1:0:delete:0"],
            },
        ]
        code, stdout, stderr, _ = self.cli([*broken, good], "--target", TOY_PATH, "--list")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(stdout.splitlines()), 24)
        code, _, stderr, _ = self.cli([*broken, good], "--list")
        self.assertEqual(code, 2)
        self.assertIn("target not found", stderr)
        code, _, stderr, _ = self.cli([dict(good, typo=1), good], "--target", TOY_PATH, "--list")
        self.assertEqual(code, 2, "the schema of every entry is still validated")
        self.assertIn("unknown keys", stderr)

    def test_files_without_mutants_in_a_glob_or_narrowed_run_are_skipped(self):
        # Regression (final review 2): an empty __init__.py in a glob, or a narrow --operators run, exited 2.
        self.add_empty_target()
        code, stdout, stderr, reports = self.cli([self.glob_target(0.6)])
        self.assertEqual(code, 0, stderr)
        self.assertEqual([report["target"] for report in reports], [EMPTY_PATH, LOOP_PATH, TOY_PATH, TOY_GLOB])
        empty = reports[0]
        self.assertEqual(
            (empty["score"], empty["no_mutants"], empty["passed"], empty["gated"], empty["counts"]["total"]),
            (None, True, True, False, 0),
        )
        self.assertIn(f"{EMPTY_PATH}: 0 mutants", stdout)
        self.assertIn("score n/a (no mutants: skipped)\n", stdout)
        self.assertEqual(reports[-1]["counts"]["total"], 35)
        self.assertAlmostEqual(reports[-1]["score"], 24 / 35)
        self.assertFalse(reports[-1]["no_mutants"])

        code, _, stderr, reports = self.cli([self.glob_target(0.5, aggregate=False)], runner=self.killing_runner())
        self.assertEqual(code, 0, stderr)
        self.assertEqual([(report["target"], report["passed"]) for report in reports][0], (EMPTY_PATH, True))

        toy = dict(VALID_TARGET, tests=["tests.mutation_toys.weak_suite"])
        code, stdout, stderr, (report,) = self.cli([toy], "--operators", "minmax")
        self.assertEqual(code, 0, stderr)
        self.assertEqual((report["no_mutants"], report["passed"], report["score"]), (True, True, None))

        code, _, stderr, _ = self.cli([dict(toy, path=EMPTY_PATH)])
        self.assertEqual(code, 2, "an explicit target with nothing to mutate is still a configuration error")
        self.assertIn("no mutants", stderr)
        code, _, stderr, _ = self.cli([dict(toy, functions=["sign"])])
        self.assertEqual(code, 2)
        self.assertIn("no mutants", stderr)

        markdown = mutation.Report(EMPTY_PATH, ("tests.a",), (), 0.0, 0.0, 0.5, no_mutants=True).format_markdown()
        self.assertTrue(markdown.startswith(f"### `{EMPTY_PATH}`: skipped (no mutants)\n"), markdown)
        combined = mutation.Report.combine(
            TOY_GLOB, [mutation.Report(EMPTY_PATH, ("tests.a",), (), 0.0, 0.0, 0.5, no_mutants=True)], 0.5
        )
        self.assertEqual((combined.no_mutants, combined.passed), (True, True))

    def test_glob_functions_need_to_match_in_one_expanded_file_only(self):
        # Regression (final review 3): every file of a glob had to define every listed qualname.
        code, stdout, stderr, _ = self.cli([self.glob_target(0.0, functions=["grade"])], "--list")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(stdout.splitlines()), 6)
        self.assertTrue(all(line.startswith(f"{TOY_PATH}:") for line in stdout.splitlines()), stdout)
        code, _, stderr, reports = self.cli([self.glob_target(0.0, functions=["grade"])])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            [(report["target"], report["no_mutants"]) for report in reports][:2], [(LOOP_PATH, True), (TOY_PATH, False)]
        )
        code, _, stderr, _ = self.cli([self.glob_target(0.0, functions=["grade", "nope"])], "--list")
        self.assertEqual(code, 2)
        self.assertIn("['nope'] match no def or class", stderr)

    def test_a_command_line_timeout_is_an_override(self):
        # Regression (final review 4): --timeout changed which mutants time out but still printed a ratchet hint.
        results = make_results(TOY_PATH, killed=9, survived=1)
        crafted = mutation.Report(TOY_PATH, ("tests.mutation_toys.strong_suite",), results, 1.0, 10.0, min_score=0.5)
        cases = {
            "no --timeout": ({}, [], [], None),
            "--timeout": ({}, ["--timeout", "60"], ["timeout"], 60.0),
            "configured timeout": ({"timeout": 60}, ["--timeout", "60"], [], 60.0),
            "different timeout": ({"timeout": 60}, ["--timeout", "30"], ["timeout"], 30.0),
        }
        for name, (target, arguments, overrides, timeout) in cases.items():
            with self.subTest(case=name):
                with patch.object(mutation, "run_mutants", return_value=crafted) as run:
                    code, _, stderr, (report,) = self.cli([dict(VALID_TARGET, **target)], *arguments)
                self.assertEqual(code, 0, stderr)
                self.assertEqual(report["overrides"], overrides)
                self.assertEqual("ratchet:" in stderr, not overrides, stderr)
                self.assertEqual(run.call_args.kwargs["timeout"], timeout)

    def test_mutants_are_generated_once_per_target_and_line_offsets_once_per_run(self):
        # Regression (final review 10): progress re-parsed the target and apply_mutant re-split the source per mutant.
        toy = dict(VALID_TARGET, tests=["tests.mutation_toys.weak_suite"])
        config = write_config(self.tmp, {"schema_version": 1, "copy": list(TOY_COPY), "targets": [toy]})
        arguments = ["--repo-root", self.repo, "--config", config, "--scratch-dir", self.scratch]
        with (
            patch.object(mutation, "SubprocessTestRunner", self.killing_runner()),
            patch.object(mutation, "generate_mutants", wraps=mutation.generate_mutants) as generate,
            patch.object(mutation, "line_offsets", wraps=mutation.line_offsets) as offsets,
        ):
            code, _, stderr = run_main(arguments)
        self.assertEqual(code, 0, stderr)
        self.assertIn("[24/24]", stderr)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(offsets.call_count, 2, "once while generating and once for applying every mutant")

    def test_extra_workspaces_are_copied_only_after_the_baseline_passes(self):
        # Regression (final review 10): --jobs copies were made before a baseline that could fail.
        runner = FakeRunner(baseline=mutation.RunResult("failed", 1.0, "boom", tests_run=5))
        with patch.object(mutation, "copy_workspace", wraps=mutation.copy_workspace) as copy:
            with self.assertRaises(mutation.BaselineError):
                mutation.run_mutants(
                    TOY_PATH,
                    ("tests.mutation_toys.weak_suite",),
                    repo_root=self.repo,
                    copy_paths=TOY_COPY,
                    scratch_dir=self.scratch,
                    test_runner=runner,
                    jobs=4,
                )
        self.assertEqual(copy.call_count, 1)
        with patch.object(mutation, "copy_workspace", wraps=mutation.copy_workspace) as copy:
            mutation.run_mutants(
                TOY_PATH,
                ("tests.mutation_toys.weak_suite",),
                repo_root=self.repo,
                copy_paths=TOY_COPY,
                scratch_dir=self.scratch,
                test_runner=FakeRunner(),
                jobs=4,
            )
        self.assertEqual(copy.call_count, 4)

    def test_typing_helper_name_arguments_are_not_mutated(self):
        # Regression (final review 11): cast("T", x), TypeVar("T") and NewType("T", int) produced noise mutants.
        source = (
            "from typing import TypeVar, NewType, cast\n"
            "import typing\n"
            'T = TypeVar("T")\n'
            'UserId = NewType("UserId", int)\n'
            'K = typing.TypeVar("K", bound=int)\n'
            'kept = registry.cast("keep")\n'
            "def f(x):\n"
            '    y = cast("int", x)\n'
            '    z = typing.cast("list[str]", x)\n'
            "    return y\n"
        )
        self.assertEqual(
            [(m.id, m.original, m.replacement) for m in gen(source)],
            [
                ("m.py:6:21:constant:0", '"keep"', '""'),
                ("m.py:8:4:delete:0", 'y = cast("int", x)', "pass"),
                ("m.py:9:4:delete:0", 'z = typing.cast("list[str]", x)', "pass"),
                ("m.py:10:11:return:0", "y", "None"),
            ],
        )


class ReportTests(unittest.TestCase):
    """Report fields for gating and ratcheting: headroom, ratchet_hint, aggregation and the Markdown summary."""

    def report(self, min_score, **counts):
        partial = counts.pop("partial", False)
        part_of = counts.pop("part_of", None)
        results = make_results(**counts)
        return mutation.Report("m.py", ("tests.a",), results, 1.0, 10.0, min_score, partial=partial, part_of=part_of)

    def test_ratchet_hint_needs_a_passing_complete_run_two_points_above_the_floor(self):
        self.assertEqual(
            self.report(0.85, killed=9, survived=1).ratchet_hint,
            "ratchet: m.py scored 0.900; raise min_score from 0.85 to 0.90",
        )
        self.assertEqual(
            self.report(0.88, killed=9, survived=1).ratchet_hint,
            "ratchet: m.py scored 0.900; raise min_score from 0.88 to 0.90",
        )
        self.assertEqual(
            self.report(0.2, killed=29, survived=71).ratchet_hint,
            "ratchet: m.py scored 0.290; raise min_score from 0.2 to 0.29",
        )
        self.assertEqual(
            self.report(0, killed=1).ratchet_hint, "ratchet: m.py scored 1.000; raise min_score from 0 to 1.00"
        )
        for name, report in {
            "headroom below 0.02": self.report(0.89, killed=9, survived=1),
            "failing": self.report(0.95, killed=9, survived=1),
            "no score": self.report(0.0, incompetent=2),
            "partial run": self.report(0.5, killed=9, survived=1, partial=True),
            "aggregate member": self.report(0.5, killed=9, survived=1, part_of="sdk/x/*.py"),
        }.items():
            with self.subTest(case=name):
                self.assertIsNone(report.ratchet_hint)
        self.assertAlmostEqual(self.report(0.85, killed=9, survived=1).headroom, 0.05)
        self.assertAlmostEqual(self.report(0.95, killed=9, survived=1).headroom, -0.05)
        self.assertIsNone(self.report(0.0, incompetent=2).headroom)

    def test_aggregate_members_are_not_gated_and_combine_sums_every_result(self):
        member = self.report(0.95, killed=1, survived=1, part_of="sdk/x/*.py")
        self.assertTrue(member.passed)
        self.assertIsNone(member.headroom)
        self.assertIn("score 50.0% (gated as part of sdk/x/*.py)", member.format_text())
        self.assertEqual(member.to_dict()["part_of"], "sdk/x/*.py")
        stale = "m2.py:9:0:delete:0"
        other = mutation.Report(
            "m2.py", ("tests.a",), make_results("m2.py", killed=2, timeout=1), 2.0, 12.0, 0.95, (stale,)
        )
        combined = mutation.Report.combine("sdk/x/*.py", [member, other], 0.7)
        self.assertEqual(combined.target, "sdk/x/*.py")
        self.assertEqual(combined.counts["total"], 5)
        self.assertEqual(combined.score, 4 / 5)
        self.assertTrue(combined.passed)
        self.assertEqual((combined.baseline_seconds, combined.timeout), (3.0, 12.0))
        self.assertEqual(combined.stale_equivalent, (stale,))
        self.assertIsNone(combined.part_of)
        self.assertFalse(combined.partial)
        self.assertFalse(mutation.Report.combine("sdk/x/*.py", [member, other], 0.85).passed)
        # One partial member makes the whole aggregate partial; overrides are merged without duplicates.
        mixed = mutation.Report.combine(
            "sdk/x/*.py",
            [replace(member, partial=True, overrides=("tests",)), replace(other, overrides=("tests", "min_score"))],
            0.7,
        )
        self.assertEqual((mixed.partial, mixed.overrides), (True, ("tests", "min_score")))

    def test_ratchet_hint_is_suppressed_when_the_run_is_not_comparable_with_the_floor(self):
        # Regression (benchmark B1): `--operators arithmetic minmax` suggested raising the default-operator floor.
        for override in ("operators", "tests", "min_score"):
            with self.subTest(override=override):
                report = replace(self.report(0.5, killed=9, survived=1), overrides=(override,))
                self.assertTrue(report.passed)
                self.assertIsNone(report.ratchet_hint)
                self.assertNotIn("ratchet:", report.format_markdown())

    def test_reports_record_the_operators_and_note_an_override(self):
        # Regression (benchmark B2): a reader could not tell which operator set produced a score.
        report = replace(self.report(0.5, killed=9, survived=1), operators=("compare", "constant"))
        self.assertEqual(report.to_dict()["operators"], ["compare", "constant"])
        self.assertEqual(report.to_dict()["overrides"], [])
        self.assertEqual((report.to_dict()["partial"], report.to_dict()["gated"]), (False, True))
        self.assertIn("\noperators: compare, constant\n", report.format_text())
        self.assertIn("Operators: `compare`, `constant`.\n", report.format_markdown())
        overridden = replace(report, overrides=("operators",))
        self.assertEqual(overridden.to_dict()["overrides"], ["operators"])
        self.assertIn("\noperators: compare, constant (operators differ from config)\n", overridden.format_text())
        self.assertIn(
            "Operators: `compare`, `constant` (operators differ from config).\n", overridden.format_markdown()
        )
        self.assertFalse(replace(overridden, min_score=0.95).passed, "an operator override is still gated")

    def test_a_failing_score_is_never_displayed_at_or_above_the_minimum(self):
        # Regression (benchmark B3): 17/21 = 0.8095 printed "score 81.0% (minimum 81.0%) FAIL".
        failing = self.report(0.81, killed=17, survived=4)
        self.assertFalse(failing.passed)
        self.assertIn("score 80.9% (minimum 81.0%) FAIL\n", failing.format_text())
        self.assertIn("Score **80.9%** (minimum 81.0%, headroom -0.1 points)", failing.format_markdown())
        finer = self.report(0.8096, killed=17, survived=4)
        self.assertIn("score 80.95% (minimum 80.96%) FAIL\n", finer.format_text())
        passing = self.report(0.80, killed=17, survived=4)
        self.assertIn("score 81.0% (minimum 80.0%) PASS\n", passing.format_text())
        exact = self.report(0.81, killed=81, survived=19)
        self.assertIn("score 81.0% (minimum 81.0%) PASS\n", exact.format_text())

    def test_markdown_headroom_never_contradicts_the_verdict_or_the_ratchet_hint(self):
        # Regression (final review 8): headroom was rounded to .1f, so 1.96 points showed "+2.0" with no ratchet hint
        # and -0.05 points showed "-0.0" next to FAIL.
        cases = [
            # (min_score, killed, survived, shown headroom, verdict, has hint)
            (0.80, 2049, 451, "+1.9", "PASS", False),
            (0.81, 83, 17, "+2.0", "PASS", True),
            (0.81, 81, 19, "+0.0", "PASS", False),
            (0.8096, 17, 4, "-0.01", "FAIL", False),
            (0.81, 17, 4, "-0.1", "FAIL", False),
            (0.95, 1, 2, "-61.7", "FAIL", False),
        ]
        for min_score, killed, survived, headroom, verdict, hint in cases:
            with self.subTest(min_score=min_score, killed=killed, survived=survived):
                report = self.report(min_score, killed=killed, survived=survived)
                markdown = report.format_markdown()
                self.assertIn(f"headroom {headroom} points)", markdown)
                self.assertTrue(markdown.startswith(f"### `m.py`: {verdict}\n"), markdown)
                self.assertEqual("ratchet:" in markdown, hint, markdown)
        overridden = replace(self.report(0.5, killed=9, survived=1), overrides=("timeout",))
        self.assertIn("headroom +40.0 points", overridden.format_markdown())
        self.assertIn("No ratchet hint: the run used command-line overrides (timeout).\n", overridden.format_markdown())

    def test_a_partial_run_is_gated_only_against_an_explicit_minimum(self):
        # Regression (benchmark O1): `--only <survivor id>` was gated against the full-run floor and exited 1.
        partial = self.report(0.9, survived=1, partial=True)
        self.assertEqual((partial.gated, partial.passed), (False, True))
        self.assertIsNone(partial.headroom)
        self.assertIn("score 0.0% (partial run: not gated)\n", partial.format_text())
        self.assertTrue(partial.format_markdown().startswith("### `m.py`: not gated (partial run)\n"))
        self.assertEqual((partial.to_dict()["partial"], partial.to_dict()["gated"]), (True, False))
        self.assertIsNone(partial.to_dict()["headroom"], "an ungated score has no headroom in the JSON report")
        explicit = replace(partial, overrides=("min_score",))
        self.assertEqual((explicit.gated, explicit.passed), (True, False))
        self.assertAlmostEqual(explicit.to_dict()["headroom"], -0.9)
        self.assertIn("score 0.0% (minimum 90.0%) FAIL\n", explicit.format_text())
        unmeasured = self.report(0.9, incompetent=1, partial=True)
        self.assertFalse(unmeasured.passed, "a partial run that measured nothing still fails")

    def test_markdown_summary(self):
        report = self.report(0.85, killed=9, survived=1)
        markdown = report.format_markdown()
        self.assertTrue(markdown.startswith("### `m.py`: PASS\n\n"), markdown)
        self.assertIn(
            "Score **90.0%** (minimum 85.0%, headroom +5.0 points); 10 mutants, 9 killed, 1 survived, 0 timeout, "
            "0 incompetent, 0 equivalent.\n",
            markdown,
        )
        self.assertIn(
            "| operator | killed | survived | timeout | incompetent | equivalent |\n"
            "| --- | ---: | ---: | ---: | ---: | ---: |\n"
            "| constant | 9 | 1 | 0 | 0 | 0 |\n",
            markdown,
        )
        self.assertIn("ratchet: m.py scored 0.900; raise min_score from 0.85 to 0.90\n", markdown)
        self.assertIn("<details><summary>1 surviving mutant</summary>\n", markdown)
        self.assertIn("`m.py:1:0:constant:9`\n\n```diff\n--- a/m.py\n", markdown)
        self.assertTrue(markdown.endswith("</details>\n"), markdown)
        failing = self.report(0.95, killed=1, survived=2).format_markdown()
        self.assertTrue(failing.startswith("### `m.py`: FAIL\n"), failing)
        self.assertIn("headroom -61.7 points", failing)
        self.assertIn("2 surviving mutants", failing)
        empty = self.report(0.5, incompetent=1).format_markdown()
        self.assertIn("Score **n/a** (no scored mutants) (minimum 50.0%)", empty)
        self.assertNotIn("<details>", empty)

    def test_markdown_fence_is_longer_than_any_backtick_run_in_a_survivor_diff(self):
        (result,) = make_results(survived=1)
        diff = '--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-x = "```py"\n+x = ""\n'
        report = mutation.Report("m.py", ("tests.a",), (replace(result, diff=diff),), 1.0, 10.0, 0.5)
        self.assertIn(f"````diff\n{diff}````\n", report.format_markdown())


class RepositoryWiringTests(unittest.TestCase):
    def test_toy_suites_are_not_collected_by_discovery(self):
        self.assertEqual(sorted(path.name for path in TOYS.glob("test*.py")), [])
        suite = unittest.TestLoader().discover(str(TOYS), pattern="test_*.py", top_level_dir=str(REPO))
        self.assertEqual(suite.countTestCases(), 0)
        tree = ast.parse(Path(os.path.abspath(__file__)).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(alias.name for alias in node.names)]
            else:
                continue
            self.assertFalse([name for name in names if "mutation_toys" in name], ast.dump(node))

    @skip_unless_dependency(
        shutil.which("make"),
        "requires make, a POSIX shell and the repository Makefile",
        applicable=os.name == "posix" and (REPO / "Makefile").is_file(),
    )
    def test_make_mutation_recipe(self):
        dropped = {"PYTHONPATH", "PYTHONSAFEPATH", "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "PYTHON"}
        env = {key: value for key, value in os.environ.items() if key not in dropped}

        def dry_run(*arguments):
            with TemporaryDirectory(ignore_cleanup_errors=True) as cwd:
                result = subprocess.run(
                    [
                        "make",
                        "--no-print-directory",
                        "-n",
                        "-C",
                        str(REPO),
                        "mutation",
                        f"PYTHON={sys.executable}",
                        *arguments,
                    ],
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

        recipe = dry_run("TARGET=sdk/crew_leveling.py", "JOBS=2")
        for fragment in (
            "this target needs Python >= 3.11",
            "PYTHONDONTWRITEBYTECODE=1",
            'scripts/mutation.py"',
            '--target "sdk/crew_leveling.py"',
            "--jobs 2",
        ):
            self.assertIn(fragment, recipe)
        self.assertNotIn("--tests", recipe)
        everything = dry_run()
        self.assertIn('scripts/mutation.py"', everything)
        self.assertNotIn("--target", everything)
        self.assertNotIn("--jobs", everything)
        mapped = dry_run("TARGET=sdk/foo.py", "TESTS=tests.test_a tests.test_b")
        self.assertIn("--tests tests.test_a tests.test_b", mapped)

    @skip_unless_dependency(yaml, "PyYAML is not installed")
    def test_workflow_mutation_job_runs_only_for_blocking_ci_and_is_non_blocking(self):
        def load(name):
            path = REPO / ".github" / "workflows" / name
            return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

        workflow = load("python-tests.yml")
        run_input = workflow["on"]["workflow_call"]["inputs"]["run-mutation"]
        self.assertEqual((run_input["type"], run_input["default"]), ("boolean", "false"))
        callers = {name: load(name)["jobs"]["tests"] for name in ("blocking-ci.yml", "postmerge-ci.yml", "release.yml")}
        for name, caller in callers.items():
            with self.subTest(caller=name):
                self.assertEqual(caller["uses"], "./.github/workflows/python-tests.yml")
        self.assertEqual(callers["blocking-ci.yml"]["with"]["run-mutation"], "true")
        self.assertNotIn("run-mutation", callers["postmerge-ci.yml"].get("with", {}))
        self.assertNotIn("run-mutation", callers["release.yml"].get("with", {}))

        job = workflow["jobs"]["mutation"]
        self.assertEqual(job["if"], "${{ inputs.run-mutation }}")
        # Job-level continue-on-error is not relied on: its effect on a reusable workflow's needs.<job>.result is
        # uncertain. The step carries it instead, and times out before the job does.
        self.assertNotIn("continue-on-error", job)
        self.assertNotIn("strategy", job)
        steps = [step for step in job["steps"] if "scripts/mutation.py" in step.get("run", "")]
        self.assertEqual(len(steps), 1)
        (step,) = steps
        self.assertEqual(step.get("continue-on-error"), "true")
        self.assertLess(int(step["timeout-minutes"]), int(job["timeout-minutes"]))
        for fragment in ("--jobs 4", "--json mutation.json", '--summary "$GITHUB_STEP_SUMMARY"'):
            self.assertIn(fragment, step["run"])
        # Exit 1 (score below min_score) is a warning; exit 2 (config, baseline or harness error) stays visible.
        self.assertIn("::warning", step["run"])
        self.assertIn("::error", step["run"])
        # Regression (final review 9): mutation.json was written and then discarded with the runner. It is uploaded
        # even when the mutation step failed, and a failed upload cannot fail the job either.
        (upload,) = [other for other in job["steps"] if other.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertGreater(job["steps"].index(upload), job["steps"].index(step))
        self.assertRegex(upload["uses"], r"^actions/upload-artifact@[0-9a-f]{40}$")
        self.assertEqual((upload.get("if"), upload.get("continue-on-error")), ("always()", "true"))
        self.assertEqual(upload["with"]["path"], "mutation.json")
        self.assertEqual(upload["with"]["retention-days"], "14")
        self.assertEqual(upload["with"]["if-no-files-found"], "ignore")
        others = [other for other in job["steps"] if other is not step and other is not upload]
        self.assertFalse([other for other in others if "continue-on-error" in other])

        # The same cached dependency install as the tests job, so client.py targets can import requests.
        tests_steps = workflow["jobs"]["tests"]["steps"]
        install = "python -m pip install -r .github/requirements-ci.txt"
        self.assertEqual([other.get("run") for other in job["steps"]].count(install), 1)
        self.assertIn(install, [other.get("run") for other in tests_steps])

        def setup(steps):
            (found,) = [other for other in steps if other.get("uses", "").startswith("actions/setup-python@")]
            return found

        self.assertEqual(setup(job["steps"])["uses"], setup(tests_steps)["uses"])
        for key in ("cache", "cache-dependency-path"):
            self.assertEqual(setup(job["steps"])["with"][key], setup(tests_steps)["with"][key])


if __name__ == "__main__":
    unittest.main()
