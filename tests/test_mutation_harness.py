"""Mutation-testing harness tests: pure mutant generation, config validation, fake and real runner behaviour.

The toy suites under tests/mutation_toys are never imported here at module level, so test discovery cannot collect
them; the harness runs them by module name inside temporary copies of a toy repository.
"""

import ast
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
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

    def test_apply_mutant_rejects_a_mutant_that_does_not_match_the_source(self):
        (mutant,) = gen("x = a < b\n")
        with self.assertRaises(ValueError):
            mutation.apply_mutant("x = a > b\n", mutant)
        with self.assertRaises(ValueError):
            mutation.apply_mutant("", mutant)

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
            "no schema_version": {"targets": [VALID_TARGET]},
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
                mutation.TargetConfig(LOOP_PATH, ("tests.a",), 0.5, (loop_id,)),
                mutation.TargetConfig(TOY_PATH, ("tests.a",), 0.5, (toy_id,)),
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
        self.assertEqual(report.timeout, 20.0)
        self.assertEqual({call[1] for call in runner.calls[1:]}, {20.0})
        self.assertEqual(report.baseline_seconds, 4.0)
        self.assertLessEqual(len({call[3] for call in runner.calls}), 3)
        self.assertEqual([result.mutant for result in report.results], self.expected)
        self.assertEqual(sorted(result.mutant.id for result in seen), sorted(m.id for m in self.expected))

        quick = FakeRunner(baseline=mutation.RunResult("passed", 0.5, tests_run=5))
        self.assertEqual(self.run_toy(quick).timeout, 10.0)
        fixed = FakeRunner()
        self.assertEqual(self.run_toy(fixed, timeout=400.0).timeout, 400.0)
        self.assertEqual(fixed.calls[0][1], 400.0)
        self.assertEqual({call[1] for call in fixed.calls[1:]}, {400.0})

    def test_equivalent_ids_are_not_run_and_leave_the_denominator(self):
        equivalent_id, stale_id = f"{TOY_PATH}:24:4:delete:0", f"{TOY_PATH}:999:0:delete:0"
        (deleted,) = [mutant for mutant in self.expected if mutant.id == equivalent_id]
        runner = FakeRunner(status="passed")
        report = self.run_toy(runner, equivalent=[equivalent_id, stale_id])
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

    def test_paths_and_test_modules_are_validated_before_running(self):
        cases = {
            "outside the root": ("../escape.py", ("tests.mutation_toys.weak_suite",), {}, "outside"),
            "parent segment": ("tests/../tests/mutation_toys/toy_target.py", ("tests.a",), {}, r"\.\."),
            "missing target": ("tests/mutation_toys/nope.py", ("tests.mutation_toys.weak_suite",), {}, "not found"),
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

        def run(min_score, aggregate=True):
            target = {"path": glob, "tests": ["tests.mutation_toys.weak_suite"], "min_score": min_score}
            if aggregate:
                target["aggregate"] = True
            config = write_config(self.tmp, {"schema_version": 1, "copy": list(TOY_COPY), "targets": [target]})
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
        previous = signal.getsignal(signal.SIGTERM)

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
        for arguments in (["--jobs", "0"], ["--min-score", "1.5"], ["--min-score", "-1"], ["--timeout", "0"]):
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
            },
        )
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

    def test_list_honours_only(self):
        chosen = f"{TOY_PATH}:5:13:compare:0"
        code, stdout, stderr = run_main([*self.config_arguments(), "--list", "--only", chosen])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout.splitlines(), [f"{chosen}  '>=' -> '<'"])


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
        self.assertFalse(mutation.Report.combine("sdk/x/*.py", [member, other], 0.85).passed)

    def test_markdown_summary(self):
        report = self.report(0.85, killed=9, survived=1)
        markdown = report.format_markdown()
        self.assertTrue(markdown.startswith("### `m.py`: PASS\n\n"), markdown)
        self.assertIn(
            "Score **90.0%** (minimum 85.0%, headroom +5.0 points); 10 mutants: 9 killed, 1 survived, 0 timeout, "
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
        others = [other for other in job["steps"] if other is not step]
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
