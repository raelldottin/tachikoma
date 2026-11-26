"""CI gates and release provenance tests using synthetic, temporary Git repos."""

import ast
import copy
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import textwrap
import unittest
from unittest.mock import patch

from scripts import ci
from sdk.fleet.accounts import load_registry
from tests.synthetic_fixtures import SYNTHETIC_ACCESS_TOKEN, SYNTHETIC_DEVICE_KEY_IOS
from tests.test_strict_mode import skip_unless_dependency

try:
    import yaml
except ImportError:
    yaml = None


def child_env(**extra):
    """Environment for a child interpreter that imports scripts.ci without relying on its working directory.

    PYTHONSAFEPATH=1 (and Bazel) keep the CWD off sys.path, so the checkout root is put on PYTHONPATH instead.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([str(ci.ROOT), existing]) if existing else str(ci.ROOT)
    env.update(extra)
    return env


# Path(__file__).resolve() and os.path.realpath(__file__) follow symlinks, so under Bazel's runfiles tree they
# escape to the source checkout. Every test module and each file listed here must use os.path.abspath instead.
FILE_RESOLUTION_GUARDED_FILES = ("scripts/ci.py", "scripts/mutation.py")
# The one known runtime exception: sdk/simulation/game.py sets SCENARIO_DIR with Path(__file__).resolve() so the
# bundled scenarios are found next to the installed module. It is outside the guard and recorded by a test below.
FILE_RESOLUTION_RUNTIME_EXCEPTION = "sdk/simulation/game.py"


def file_resolution_calls(source):
    """Line numbers of Path(__file__).resolve() and os.path.realpath(__file__) calls; string literals do not count."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "resolve"
            and isinstance(function.value, ast.Call)
            and ast.unparse(function.value.func) in {"Path", "pathlib.Path"}
            and [ast.unparse(argument) for argument in function.value.args] == ["__file__"]
        ) or (
            ast.unparse(function) == "os.path.realpath"
            and [ast.unparse(argument) for argument in node.args] == ["__file__"]
        ):
            lines.append(node.lineno)
    return sorted(lines)


def file_resolution_violations(root, listed=FILE_RESOLUTION_GUARDED_FILES):
    """Guard findings for tests/**/*.py and the listed files under root; a listed file that is missing is one."""
    violations = []
    paths = sorted((root / "tests").rglob("*.py"))
    for name in listed:
        if (root / name).is_file():
            paths.append(root / name)
        else:
            violations.append(f"{name}: listed for the __file__ guard but missing")
    for path in paths:
        for line in file_resolution_calls(path.read_text(encoding="utf-8")):
            violations.append(f"{path.relative_to(root).as_posix()}:{line}: resolves __file__ through symlinks")
    return violations


class RequiredGateTests(unittest.TestCase):
    def test_all_success_is_required(self):
        self.assertIsNone(
            ci.require_success({"checks": {"result": "success"}, "tests": {"result": "success"}}, ["checks", "tests"])
        )

    def test_every_other_upstream_state_fails(self):
        for result in ("failure", "cancelled", "skipped", "neutral", "", None, True):
            for failing_job in ("checks", "tests"):
                with self.subTest(result=result, job=failing_job):
                    results = {"checks": {"result": "success"}, "tests": {"result": "success"}}
                    results[failing_job]["result"] = result
                    with self.assertRaises(ValueError):
                        ci.require_success(results, ["checks", "tests"])

    def test_missing_extra_and_malformed_results_fail(self):
        for results in (
            None,
            [],
            {},
            {"checks": {"result": "success"}},
            {"checks": "success"},
            {"checks": {"result": "success"}, "extra": {"result": "success"}},
        ):
            with self.subTest(results=results), self.assertRaises(ValueError):
                ci.require_success(results, ["checks", "tests"])

    def test_empty_or_duplicate_required_names_fail(self):
        for required in ([], ["checks", "checks"]):
            with self.assertRaises(ValueError):
                ci.require_success({}, required)

    def test_cli_fails_closed_on_missing_or_invalid_json(self):
        for value in (None, "not-json", "null", '{"checks":{"result":"skipped"}}'):
            env = {} if value is None else {"NEEDS_JSON": value}
            with patch.dict(os.environ, env, clear=True), redirect_stderr(io.StringIO()):
                self.assertEqual(ci.main(["gate", "--required", "checks"]), 1)


class TestIsolationTests(unittest.TestCase):
    def test_test_modules_do_not_override_checkout_with_absolute_literals(self):
        for path in (ci.ROOT / "tests").glob("test_*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and ast.unparse(node.func) == "sys.path.insert":
                    for argument in node.args:
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                            self.assertFalse(PurePosixPath(argument.value).is_absolute(), path.name)
                            self.assertFalse(PureWindowsPath(argument.value).is_absolute(), path.name)

    def test_no_guarded_file_resolves_file_through_symlinks(self):
        self.assertEqual(file_resolution_violations(ci.ROOT), [])

    def test_file_resolution_guard_catches_planted_calls_and_missing_listed_files(self):
        listed = ("scripts/ci.py",)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests" / "nested").mkdir(parents=True)
            (root / "scripts").mkdir()
            (root / "scripts" / "ci.py").write_text("ROOT = Path(os.path.abspath(__file__)).parent\n", encoding="utf-8")
            (root / "tests" / "test_clean.py").write_text(
                "ROOT = Path(os.path.abspath(__file__)).parents[1]\nTEXT = 'Path(__file__).resolve()'\n",
                encoding="utf-8",
            )
            self.assertEqual(file_resolution_violations(root, listed), [])

            (root / "tests" / "test_planted.py").write_text(
                "import os\nREPO = Path(__file__).resolve().parents[1]\n", encoding="utf-8"
            )
            (root / "tests" / "nested" / "helper.py").write_text(
                "HERE = os.path.realpath(__file__)\n", encoding="utf-8"
            )
            (root / "scripts" / "ci.py").write_text(
                "ROOT = pathlib.Path(__file__).resolve().parent\n", encoding="utf-8"
            )
            self.assertEqual(
                file_resolution_violations(root, listed),
                [
                    "tests/nested/helper.py:1: resolves __file__ through symlinks",
                    "tests/test_planted.py:2: resolves __file__ through symlinks",
                    "scripts/ci.py:1: resolves __file__ through symlinks",
                ],
            )
            self.assertIn(
                "scripts/missing.py: listed for the __file__ guard but missing",
                file_resolution_violations(root, ("scripts/ci.py", "scripts/missing.py")),
            )

    def test_game_scenario_dir_is_the_one_known_runtime_resolve(self):
        source = (ci.ROOT / FILE_RESOLUTION_RUNTIME_EXCEPTION).read_text(encoding="utf-8")
        self.assertEqual(len(file_resolution_calls(source)), 1)
        self.assertIn('SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"', source)

    def test_swallowed_network_attempt_still_fails_the_test_command(self):
        program = """
import socket
import sys
import unittest
from unittest.mock import patch
from scripts import ci

class CaughtNetworkAttempt(unittest.TestCase):
    def runTest(self):
        with socket.socket() as connection:
            try:
                connection.connect(('127.0.0.1', 9))
            except RuntimeError:
                pass

with patch.object(unittest.defaultTestLoader, 'discover', return_value=unittest.TestSuite([CaughtNetworkAttempt()])):
    sys.exit(ci.run_tests([]))
"""
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=ci.ROOT,
            env=child_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("CI rejected 1 unmocked network", result.stderr)
        self.assertIn("OK", result.stderr)


class LintRatchetTests(unittest.TestCase):
    def test_signature_includes_file_rule_and_message_but_not_line_number(self):
        root = Path(os.path.abspath(__file__)).parent.parent
        findings = [
            {"filename": str(root / "run.py"), "code": "F401", "message": "unused import", "location": {"row": 1}}
        ]
        first = ci.lint_counts(findings, root)
        findings[0]["location"]["row"] = 200
        self.assertEqual(first, ci.lint_counts(findings, root))
        findings.append(dict(findings[0]))
        self.assertEqual(ci.lint_counts(findings, root)[("run.py", "F401", "unused import")], 2)

    def test_invalid_baseline_counts_and_duplicates_fail(self):
        entry = {"path": "run.py", "code": "F401", "message": "unused", "count": 1}
        for count in (0, -1, True, "1"):
            with self.assertRaises(ValueError):
                ci.lint_baseline({"schema_version": 1, "violations": [{**entry, "count": count}]})
        with self.assertRaises(ValueError):
            ci.lint_baseline({"schema_version": 1, "violations": [entry, entry]})

    def test_new_lint_and_stale_allowances_both_fail(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".github").mkdir()
            baseline = root / ".github/ruff-baseline.json"
            finding = {"filename": str(root / "run.py"), "code": "F401", "message": "unused"}
            baseline.write_text(json.dumps({"schema_version": 1, "violations": []}), encoding="utf-8")
            result = subprocess.CompletedProcess([], 1, stdout=json.dumps([finding]), stderr="")
            with patch.object(ci.subprocess, "run", return_value=result), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "lint findings changed"):
                    ci.check_lint(root)
            baseline.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "violations": [
                            {"path": "run.py", "code": "F401", "message": "unused", "count": 1},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
            with patch.object(ci.subprocess, "run", return_value=result), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "lint findings changed"):
                    ci.check_lint(root)

    def test_tool_failure_cannot_be_treated_as_no_findings(self):
        result = subprocess.CompletedProcess([], 2, stdout="", stderr="configuration error")
        with patch.object(ci.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "Ruff could not run"):
                ci.check_lint(ci.ROOT)


HOLLOW_SAMPLE_PATH = "tests/test_sample.py"
HOLLOW_SAMPLE_HEADER = "import unittest\nfrom unittest.mock import MagicMock\n\n\nclass Sample(unittest.TestCase):\n"


def hollow(methods):
    """Hollow findings for methods (dedented source) placed in a unittest.TestCase subclass named Sample."""
    source = HOLLOW_SAMPLE_HEADER + textwrap.indent(textwrap.dedent(methods), "    ")
    return ci.hollow_tests_in_source(source, HOLLOW_SAMPLE_PATH)


def hollow_body(body):
    """Hollow findings for one Sample.test_x method whose body is the dedented source body."""
    return hollow("def test_x(self):\n" + textwrap.indent(textwrap.dedent(body), "    "))


def hollow_module(source):
    return ci.hollow_tests_in_source(textwrap.dedent(source), HOLLOW_SAMPLE_PATH)


def sample_id(qualname):
    return f"{HOLLOW_SAMPLE_PATH}::{qualname}"


# The unittest.mock assertion methods, pinned here rather than read from ci.MOCK_CALL_ASSERTIONS.
MOCK_ASSERTION_METHODS = (
    "assert_any_await",
    "assert_any_call",
    "assert_awaited",
    "assert_awaited_once",
    "assert_awaited_once_with",
    "assert_awaited_with",
    "assert_called",
    "assert_called_once",
    "assert_called_once_with",
    "assert_called_with",
    "assert_has_awaits",
    "assert_has_calls",
    "assert_not_awaited",
    "assert_not_called",
)


class HollowTestDetectorTests(unittest.TestCase):
    """ci.hollow_tests_in_source finds tests that assert nothing, only mock calls, or only literals."""

    def test_assertion_free_test_is_no_assertion(self):
        self.assertEqual(hollow_body("run()\n"), [(sample_id("Sample.test_x"), "no-assertion")])

    def test_real_assertions_are_recognised(self):
        bodies = (
            "self.assertEqual(run(), 3)\n",
            "assert run() == 3\n",
            "if run():\n    self.fail('x')\n",
            "with self.assertRaises(ValueError):\n    run()\n",
            "with self.assertLogs() as logs:\n    run()\n",
            "self.assertRaisesRegex(ValueError, 'x', run)\n",
            "with self.subTest(i=1):\n    self.assertTrue(run())\n",
            "self.assertIsInstance(run(), dict)\n",
            "assert_shape(run())\n",
            "self._assert_ok(run())\n",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(hollow_body(body), [])

    def test_helpers_are_followed(self):
        self.assertEqual(
            hollow(
                """
                def _check(self, value):
                    self.assertEqual(value, 1)

                def test_x(self):
                    self._check(run())
                """
            ),
            [],
        )
        self.assertEqual(
            hollow_module(
                """
                import unittest


                def verify(value):
                    assert value == run()


                class Sample(unittest.TestCase):
                    def test_x(self):
                        verify(2)
                """
            ),
            [],
        )
        self.assertEqual(
            hollow(
                """
                def _prepare(self):
                    run()

                def test_x(self):
                    self._prepare()
                """
            ),
            [(sample_id("Sample.test_x"), "no-assertion")],
        )
        self.assertEqual(
            hollow(
                """
                def _loop(self):
                    self._loop()

                def test_x(self):
                    self._loop()
                """
            ),
            [(sample_id("Sample.test_x"), "no-assertion")],
        )

    def test_helpers_passed_by_reference_are_followed(self):
        for methods in (
            "def _check(self):\n    self.assertEqual(run(), 1)\n\ndef test_x(self):\n    self.addCleanup(self._check)\n",
            "def _check(self, v):\n    self.assertEqual(v, 1)\n\ndef test_x(self):\n    Sample._check(self, run())\n",
        ):
            with self.subTest(methods=methods):
                self.assertEqual(hollow(methods), [])

    def test_self_helpers_resolve_through_the_test_class_and_its_bases(self):
        source = """
        import unittest


        class Base(unittest.TestCase):
            def _check(self):
                self.assertTrue(run())


        class ViaSuper(Base):
            def _check(self):
                run()

            def test_super(self):
                super()._check()


        class Quiet(unittest.TestCase):
            def _check(self):
                run()

            def test_own_helper(self):
                self._check()


        class Inherits(Base):
            def test_inherited(self):
                self._check()
        """
        self.assertEqual(hollow_module(source), [(sample_id("Quiet.test_own_helper"), "no-assertion")])

    def test_fail_counts_only_on_the_test_case(self):
        self.assertEqual(hollow_body("job = run()\njob.fail()\n"), [(sample_id("Sample.test_x"), "no-assertion")])
        self.assertEqual(hollow_body("if run():\n    cls = self\n    cls.fail('x')\n"), [])

    def test_nested_function_assertions_count_only_when_the_function_is_referenced(self):
        check = "def check(*args):\n    self.assertEqual(run(), 1)\n\n"
        self.assertEqual(hollow_body(check + "run()\n"), [(sample_id("Sample.test_x"), "no-assertion")])
        for use in ("check()\n", "run(side_effect=check)\n"):
            with self.subTest(use=use):
                self.assertEqual(hollow_body(check + use), [])

    def test_assertions_in_a_nested_fake_class_count(self):
        body = (
            "test = self\n\nclass Fake:\n    def send(self, value):\n        test.assertEqual(value, 1)\n\n"
            "run(Fake())\n"
        )
        self.assertEqual(hollow_body(body), [])

    def test_helpers_imported_from_other_test_modules_are_followed(self):
        sources = {
            "tests/helpers.py": (
                "import unittest\n\n\ndef verify(value):\n    assert value == run()\n\n\ndef prepare():\n    run()\n\n\n"
                "class Checks:\n    def _check(self):\n        self.assertEqual(run(), 1)\n"
            ),
            "tests/test_a.py": (
                "import unittest\nfrom tests.helpers import Checks, prepare, verify as check\n"
                "from tests import helpers as h\nimport tests.helpers as hh\nfrom . import helpers as rel\n\n\n"
                "class A(Checks, unittest.TestCase):\n"
                "    def test_imported(self):\n        check(run())\n\n"
                "    def test_module_alias(self):\n        h.verify(run())\n\n"
                "    def test_import_as(self):\n        hh.verify(run())\n\n"
                "    def test_relative(self):\n        rel.verify(run())\n\n"
                "    def test_inherited_helper(self):\n        self._check()\n\n"
                "    def test_imported_non_asserting(self):\n        prepare()\n"
            ),
        }
        self.assertEqual(
            ci.hollow_tests_in_sources(sources),
            [("tests/test_a.py::A.test_imported_non_asserting", "no-assertion")],
        )

    def test_imported_bases_count_only_when_they_are_test_cases(self):
        sources = {
            "tests/fakes.py": "class FakeClient:\n    pass\n",
            "tests/test_base.py": "import unittest\n\n\nclass BaseTests(unittest.TestCase):\n    pass\n",
            "tests/test_b.py": (
                "from tests.fakes import FakeClient\nfrom tests.test_base import BaseTests\n\n\n"
                "class Stub(FakeClient):\n    def test_mode(self):\n        run()\n\n\n"
                "class Child(BaseTests):\n    def test_c(self):\n        run()\n"
            ),
        }
        self.assertEqual(ci.hollow_tests_in_sources(sources), [("tests/test_b.py::Child.test_c", "no-assertion")])

    def test_mock_call_assertions_are_the_unittest_mock_assertion_methods(self):
        self.assertEqual(sorted(ci.MOCK_CALL_ASSERTIONS), sorted(MOCK_ASSERTION_METHODS))

    def test_mock_call_assertions_alone_are_mock_only(self):
        # Iterates the pinned literal, not ci.MOCK_CALL_ASSERTIONS, so a name dropped from the detector fails here.
        bodies = [f"m = MagicMock()\nrun(m)\nm.{name}()\n" for name in MOCK_ASSERTION_METHODS]
        bodies += [
            "m = MagicMock()\nrun(m)\nself.assertEqual(m.call_count, 2)\n",
            "m = MagicMock()\nrun(m)\nassert m.call_count == 2\n",
            "m = MagicMock()\nrun(m)\nself.assertTrue(m.called)\n",
            "m = MagicMock()\nrun(m)\nself.assertFalse(m.called)\n",
            "m = MagicMock()\nrun(m)\nassert not m.called\n",
            "m = MagicMock()\nrun(m)\nexpected = 2\nself.assertEqual(m.await_count, expected)\n",
            # A recorded call compared with anything is a mock call assertion, like assert_called_with.
            "m = MagicMock()\nrun(m)\nself.assertEqual(m.call_count, len(run()))\n",
            "m = MagicMock()\nrun(m)\nself.assertEqual(m.call_args_list, [call(1)])\n",
            "m = MagicMock()\nrun(m)\nself.assertEqual(m.call_args, call(run()))\n",
            "m = MagicMock()\nrun(m)\nself.assertIn('x', m.call_args[0][0])\n",
            "m = MagicMock()\nrun(m)\nself.assertEqual(m.call_args.kwargs['body'], 'x')\n",
            "m = MagicMock()\nrun(m)\nassert m.mock_calls == []\n",
            "m = MagicMock()\nrun(m)\nself.assertEqual(m.method_calls[0], call.send(1))\n",
            "m = AsyncMock()\nrun(m)\nself.assertEqual(m.await_args, call(1))\n",
            "m = AsyncMock()\nrun(m)\nself.assertEqual(m.await_args_list, [])\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(hollow_body(body), [(sample_id("Sample.test_x"), "mock-only")])

    def test_mock_assertion_beside_tautology_is_mock_only(self):
        # Neither checks project behaviour; the mock call assertion names the more specific rule.
        for body in (
            "m = MagicMock()\nrun(m)\nm.assert_called_once()\nself.assertTrue(True)\n",
            "m = MagicMock()\nrun(m)\nself.assertEqual(m.call_count, 1)\nx = 4\nself.assertEqual(x, 4)\n",
        ):
            with self.subTest(body=body):
                self.assertEqual(hollow_body(body), [(sample_id("Sample.test_x"), "mock-only")])

    def test_behaviour_assertion_beside_mock_assertion_is_not_hollow(self):
        for body in (
            "m = MagicMock()\nm.assert_called_once()\nself.assertEqual(run(m), 3)\n",
            "m = MagicMock()\nrun(m)\nself.assertIn('x', m.call_args[0][0])\nself.assertTrue(run())\n",
        ):
            with self.subTest(body=body):
                self.assertEqual(hollow_body(body), [])

    def test_tautologies(self):
        bodies = (
            "self.assertTrue(True)\n",
            "assert True\n",
            "self.assertFalse(False)\n",
            "self.assertIsNone(None)\n",
            "self.assertIsNotNone(1)\n",
            "x = 4\nself.assertEqual(x, 4)\n",
            "trophies = 1000\nxp = trophies / 10\nself.assertEqual(xp, 100)\n",
            "value = run()\nself.assertEqual(value, value)\n",
            # Always truthy: a non-empty display, or a mock assertion method that is never called.
            "assert (run(), 'msg')\n",
            "self.assertTrue([run()])\n",
            "m = MagicMock()\nrun(m)\nself.assertTrue(m.assert_called_once)\n",
            "self.assertEqual(2 + 2, 4)\n",
            "self.assertIn('a', 'abc')\n",
            "self.assertIs(None, None)\n",
            "self.assertAlmostEqual(1.0, 1.0)\n",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(hollow_body(body), [(sample_id("Sample.test_x"), "tautology")])

    def test_project_values_and_mutated_names_are_not_tautologies(self):
        bodies = (
            "callers = []\nfor p in run():\n    callers.append(p)\nself.assertEqual(callers, [])\n",
            "out = {}\nfill(out)\nself.assertEqual(out, {})\n",
            "seen = False\n\ndef mark():\n    nonlocal seen\n    seen = True\n\nrun(mark)\nself.assertTrue(seen)\n",
            "self.assertEqual(run(), run())\n",
            "for x in [1]:\n    self.assertEqual(x, 1)\n",
            "try:\n    run()\nexcept ValueError:\n    return\nassert False, 'expected'\n",
            "if run():\n    self.assertTrue(False, 'unexpected')\n",
            # Rebound under control flow: the assertion depends on what project code returned.
            "flag = False\nif run() is False:\n    flag = True\nself.assertTrue(flag)\n",
            "count = 0\nfor _ in run():\n    count += 1\nself.assertEqual(count, 3)\n",
            "found = False\nfor m in run():\n    if 'x' in m:\n        found = True\nself.assertTrue(found)\n",
            "raised = False\ntry:\n    run()\nexcept ValueError:\n    raised = True\nself.assertTrue(raised)\n",
            "n = 0\nwhile poll():\n    n += 1\nassert n == 2\n",
            "if run():\n    x = 1\nself.assertEqual(x, 1)\n",
            # Handed to project code inside another binding's value.
            "out = {}\nresult = fill(out)\nself.assertEqual(out, {'a': 1})\n",
            # The list is filled through an alias handed to project code.
            "expected = []\ncaptured = expected\nfill(captured)\nself.assertEqual(expected, [1])\n",
            # Match patterns rebind names without an ast.Name node.
            "x = 1\nmatch run():\n    case [x]:\n        pass\nself.assertEqual(x, 1)\n",
            "x = 1\nmatch run():\n    case [*x]:\n        pass\nself.assertEqual(x, 1)\n",
            "x = 1\nmatch run():\n    case {**x}:\n        pass\nself.assertEqual(x, 1)\n",
            # A test-built value compared with a project value: every operand of the comparison counts.
            "value = 3\nassert value == run()\n",
            "value = 3\nassert 0 < value <= run()\n",
            # Rebound by an except clause: the name holds whatever project code raised.
            "err = None\ntry:\n    run()\nexcept ValueError as err:\n    pass\nself.assertIsNone(err)\n",
            # Declared global: project code can rebind it.
            "global FLAG\nFLAG = False\nrun()\nself.assertFalse(FLAG)\n",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(hollow_body(body), [])

    def test_original_status_aggregation_test_is_a_tautology(self):
        # Verbatim from tests/test_e2e_live_fixes.py before this check existed: it never runs run.py, it only
        # reads back return values it configured on its own MagicMock.
        original = '''
        def test_run_py_status_aggregation_failure(self):
            """Proves that a failure in secondary operations propagates to runtime_failed."""
            mock_client = MagicMock()
            mock_client.grabFlyingStarbux = MagicMock()
            mock_client.freeStarbuxToday = 10
            mock_client.freeStarbuxMax = 5
            mock_client.collectTaskReward = MagicMock(return_value=False)  # Secondary action fails!
            mock_client.getCrewInfo = MagicMock(return_value=True)
            mock_client.upgradeResearches = MagicMock(return_value=True)
            mock_client.upgradeRooms = MagicMock(return_value=True)
            mock_client.collectDailyReward = MagicMock(return_value=True)
            mock_client.listActiveMarketplaceMessages = MagicMock(return_value=True)
            mock_client.getMessages = MagicMock(return_value=True)
            mock_client.manageTraining = MagicMock(return_value=True)
            mock_client.upgradeCharacters = MagicMock(return_value=True)
            mock_client.info = {"@Name": "TestBot"}

            runtime_failed = False
            if mock_client.collectTaskReward() is False:
                runtime_failed = True
            if mock_client.getCrewInfo() is False:
                runtime_failed = True

            self.assertTrue(runtime_failed)
        '''
        self.assertEqual(hollow(original), [(sample_id("Sample.test_run_py_status_aggregation_failure"), "tautology")])

    def test_values_from_mocks_no_project_code_sees_are_tautologies(self):
        bodies = (
            "m = MagicMock(return_value=3)\nself.assertEqual(m(), 3)\n",
            "m = mock.MagicMock()\nm.fetch.return_value = 5\nassert m.fetch() == 5\n",
            "m = Mock(ok=True)\nself.assertTrue(m.ok)\n",
            "m = AsyncMock()\nm.reset_mock()\nself.assertIsNotNone(m.x)\n",
            "m = MagicMock(return_value=False)\nflag = False\nif m() is False:\n    flag = True\nself.assertTrue(flag)\n",
            "m = MagicMock()\nm.ok.return_value = True\nx = 1 if m.ok() else 2\nself.assertEqual(x, 1)\n",
            "seen = []\nseen.append(1)\nself.assertEqual(seen, [1])\n",
            "flag = False\nif True:\n    flag = True\nelse:\n    flag = None\nself.assertTrue(flag)\n",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(hollow_body(body), [(sample_id("Sample.test_x"), "tautology")])

    def test_mocks_project_code_can_see_are_not_test_controlled(self):
        bodies = (
            # Handed to project code, directly, through a container, an attribute or a nested function.
            "m = MagicMock(return_value=3)\nrun(m)\nself.assertEqual(m(), 3)\n",
            "m = MagicMock()\nholder = [m]\nrun(holder)\nself.assertEqual(m.x(), 1)\n",
            "m = MagicMock()\nself.client.request = m\nrun(self.client)\nself.assertEqual(m(), 1)\n",
            "m = MagicMock()\n\ndef get():\n    return m\n\nrun(get)\nself.assertEqual(m.x(), 1)\n",
            # Created by patch(), so the patched project code sees it.
            "with patch('run.Client') as m:\n    m.return_value = 3\n    self.assertEqual(m(), 3)\n",
            "m = patch('run.Client').start()\nm.return_value = 3\nself.assertEqual(m(), 3)\n",
            # Configured with project code or a project value.
            "m = MagicMock(side_effect=run)\nself.assertEqual(m(), 3)\n",
            "m = MagicMock()\nm.x.return_value = run()\nself.assertEqual(m.x(), 3)\n",
            # Configured or called under control flow that depends on project code.
            "m = MagicMock()\nif run():\n    m.x.return_value = 1\nself.assertEqual(m.x(), 1)\n",
            "m = MagicMock(return_value=False)\nflag = False\nfor _ in run():\n    if m() is False:\n        flag = True\n"
            "self.assertTrue(flag)\n",
            "seen = []\nfor item in run():\n    seen.append(1)\nself.assertEqual(seen, [])\n",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(hollow_body(body), [])

    def test_unconditional_skips_are_exempt_conditional_are_not(self):
        for methods in (
            "def test_x(self):\n    self.skipTest('later')\n    run()\n",
            "@unittest.skip('x')\ndef test_x(self):\n    run()\n",
            "def test_x(self):\n    raise unittest.SkipTest('x')\n",
        ):
            with self.subTest(methods=methods):
                self.assertEqual(hollow(methods), [])
        for methods in (
            "def test_x(self):\n    if os.environ.get('X') is None:\n        self.skipTest('x')\n    run()\n",
            "@unittest.skipIf(True, 'x')\ndef test_x(self):\n    run()\n",
        ):
            with self.subTest(methods=methods):
                self.assertEqual(hollow(methods), [(sample_id("Sample.test_x"), "no-assertion")])
        skipped_class = "import unittest\n\n\n@unittest.skip('x')\nclass Sample(unittest.TestCase):\n    def test_x(self):\n        run()\n"
        self.assertEqual(hollow_module(skipped_class), [])

    def test_only_unittest_test_methods_are_examined(self):
        cases = (
            (
                """
                class Tree:
                    def tests(self):
                        run()

                    def test_build(self):
                        run()
                """,
                [],
            ),
            (
                """
                import unittest


                class Sample(unittest.TestCase):
                    def helper(self):
                        run()

                    def test_ok(self):
                        self.assertEqual(run(), 1)
                """,
                [],
            ),
            (
                """
                import unittest


                class Sample(unittest.TestCase):
                    def test_outer(self):
                        class Probe(unittest.TestCase):
                            def test_inner(self):
                                self.assertTrue(run())

                        run(Probe)
                """,
                [(sample_id("Sample.test_outer"), "no-assertion")],
            ),
            (
                """
                import unittest


                class Mixin:
                    def test_shared(self):
                        run()


                class Concrete(Mixin, unittest.TestCase):
                    pass
                """,
                [(sample_id("Mixin.test_shared"), "no-assertion")],
            ),
            (
                """
                import unittest


                class Base(unittest.TestCase):
                    pass


                class Child(Base):
                    def test_c(self):
                        run()
                """,
                [(sample_id("Child.test_c"), "no-assertion")],
            ),
            (
                """
                from tests.test_other import OtherTests


                class Child(OtherTests):
                    def test_c(self):
                        run()
                """,
                [(sample_id("Child.test_c"), "no-assertion")],
            ),
            (
                """
                from elsewhere import Thing


                class X(Thing):
                    def test_x(self):
                        run()
                """,
                [],
            ),
            (
                """
                import unittest


                class Outer(unittest.TestCase):
                    class Inner(unittest.TestCase):
                        def test_i(self):
                            run()
                """,
                # unittest's loader collects only module-level TestCase classes.
                [],
            ),
            (
                # unittest never collects module-level functions.
                """
                def test_function():
                    run()
                """,
                [],
            ),
            (
                """
                import unittest


                class Sample(unittest.IsolatedAsyncioTestCase):
                    async def test_async(self):
                        await run()
                """,
                [(sample_id("Sample.test_async"), "no-assertion")],
            ),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(hollow_module(source), expected)

    def test_results_sorted_with_path_prefix(self):
        source = """
        import unittest
        from unittest.mock import MagicMock


        class Zeta(unittest.TestCase):
            def test_b(self):
                run()

            def test_a(self):
                self.assertTrue(True)


        class Alpha(unittest.TestCase):
            def test_c(self):
                m = MagicMock()
                m.assert_called()
        """
        self.assertEqual(
            hollow_module(source),
            [
                (sample_id("Alpha.test_c"), "mock-only"),
                (sample_id("Zeta.test_a"), "tautology"),
                (sample_id("Zeta.test_b"), "no-assertion"),
            ],
        )

    def test_unparsable_source_names_the_file(self):
        with self.assertRaisesRegex(ValueError, "tests/test_bad.py"):
            ci.hollow_tests_in_source("def broken(:\n", "tests/test_bad.py")

    def test_test_cases_are_resolved_through_unittest_imports(self):
        hollow_test = "    def test_a(self):\n        run()\n"
        cases = (
            ("from unittest import TestCase as TC\n\n\nclass T(TC):\n" + hollow_test, ["T.test_a"]),
            ("import unittest as ut\n\n\nclass T(ut.TestCase):\n" + hollow_test, ["T.test_a"]),
            ("import unittest.mock\n\n\nclass T(unittest.TestCase):\n" + hollow_test, ["T.test_a"]),
            ("from unittest.case import TestCase\n\n\nclass T(TestCase):\n" + hollow_test, ["T.test_a"]),
            (
                "from unittest import IsolatedAsyncioTestCase\n\n\nclass T(IsolatedAsyncioTestCase):\n"
                "    async def test_a(self):\n        await run()\n",
                ["T.test_a"],
            ),
            # A class defined in the tree is a test base only when it subclasses a TestCase, whatever its name.
            ("class FakeTestCase:\n    pass\n\n\nclass T(FakeTestCase):\n" + hollow_test, []),
            ("import unittest\n\n\nclass T(unittest.FakeTestCase):\n" + hollow_test, []),
            # A base from outside the tree is trusted by name: a third-party unittest subclass.
            ("from django.test import TestCase\n\n\nclass T(TestCase):\n" + hollow_test, ["T.test_a"]),
            ("from absl.testing import absltest\n\n\nclass T(absltest.TestCase):\n" + hollow_test, ["T.test_a"]),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(hollow_module(source), [(sample_id(name), "no-assertion") for name in expected])
        sources = {
            "tests/fakes.py": "class FakeTestCase:\n    pass\n",
            "tests/base.py": "from unittest import TestCase\n",
            "tests/test_b.py": (
                "from tests.fakes import FakeTestCase\nfrom tests.base import TestCase\nfrom tests import fakes\n\n\n"
                "class Fake(FakeTestCase):\n"
                + hollow_test
                + "\n\nclass Dotted(fakes.FakeTestCase):\n"
                + hollow_test
                + "\n\nclass Real(TestCase):\n"
                + hollow_test
            ),
        }
        self.assertEqual(ci.hollow_tests_in_sources(sources), [("tests/test_b.py::Real.test_a", "no-assertion")])

    def test_assert_named_helpers_defined_in_the_tree_are_followed(self):
        self.assertEqual(
            hollow("def assert_ok(self, value):\n    pass\n\ndef test_x(self):\n    self.assert_ok(run())\n"),
            [(sample_id("Sample.test_x"), "no-assertion")],
        )
        self.assertEqual(
            hollow(
                "def _assert_ok(self, value):\n    self.assertEqual(value, run())\n\ndef test_x(self):\n    self._assert_ok(1)\n"
            ),
            [],
        )
        source = """
        import unittest


        def assert_shape(value):
            print(value)


        class Sample(unittest.TestCase):
            def test_x(self):
                assert_shape(run())
        """
        self.assertEqual(hollow_module(source), [(sample_id("Sample.test_x"), "no-assertion")])
        sources = {
            "tests/helpers.py": "def assert_valid(value):\n    return value\n",
            "tests/test_a.py": (
                "import unittest\nfrom tests.helpers import assert_valid\n\n\nclass A(unittest.TestCase):\n"
                "    def test_a(self):\n        assert_valid(run())\n"
            ),
        }
        self.assertEqual(ci.hollow_tests_in_sources(sources), [("tests/test_a.py::A.test_a", "no-assertion")])

    def test_helpers_resolve_only_through_their_own_scope(self):
        source = """
        import unittest


        class Fake:
            def _check(self):
                assert run() == 1

            def verify(self):
                assert run() == 1


        class Sample(unittest.TestCase):
            def test_self_attribute_missing_from_the_class(self):
                self._check()

            def test_bare_name_of_a_method(self):
                verify()

            def test_defines_check(self):
                def check():
                    self.assertEqual(run(), 1)

                check()

            def test_uses_another_tests_nested_function(self):
                check()
        """
        self.assertEqual(
            hollow_module(source),
            [
                (sample_id("Sample.test_bare_name_of_a_method"), "no-assertion"),
                (sample_id("Sample.test_self_attribute_missing_from_the_class"), "no-assertion"),
                (sample_id("Sample.test_uses_another_tests_nested_function"), "no-assertion"),
            ],
        )

    def test_detector_runs_without_match_statement_nodes(self):
        # Python 3.9 has no ast.Match* classes; the detector must not reference them unconditionally.
        removed = {name: getattr(ast, name) for name in ("MatchAs", "MatchStar", "MatchMapping") if hasattr(ast, name)}
        try:
            for name in removed:
                delattr(ast, name)
            self.assertEqual(
                hollow_body("x = 4\nself.assertEqual(x, 4)\n"), [(sample_id("Sample.test_x"), "tautology")]
            )
        finally:
            for name, value in removed.items():
                setattr(ast, name, value)

    def test_identical_operands_are_a_tautology_only_for_reflexive_comparisons(self):
        for body in (
            "value = run()\nassert value == value\n",
            "value = run()\nassert value is value\n",
            "value = run()\nassert value >= value\n",
            "value = run()\nself.assertLessEqual(value, value)\n",
            "value = run()\nself.assertCountEqual(value, value)\n",
        ):
            with self.subTest(body=body):
                self.assertEqual(hollow_body(body), [(sample_id("Sample.test_x"), "tautology")])
        # Always false: a reached-the-wrong-branch marker, like self.fail().
        for body in (
            "value = run()\nself.assertNotEqual(value, value)\n",
            "value = run()\nself.assertIsNot(value, value)\n",
            "value = run()\nself.assertLess(value, value)\n",
            "value = run()\nself.assertGreater(value, value)\n",
            "value = run()\nassert value != value\n",
            "value = run()\nassert value < value\n",
        ):
            with self.subTest(body=body):
                self.assertEqual(hollow_body(body), [])

    def test_test_classes_across_an_import_cycle_do_not_depend_on_module_order(self):
        modules = (
            (
                "tests/test_a.py",
                "import unittest\nfrom tests.test_b import BBase\n\n\nclass ABase(unittest.TestCase):\n    pass\n\n\n"
                "class A(BBase):\n    def test_a(self):\n        run()\n",
            ),
            (
                "tests/test_b.py",
                "from tests.test_a import ABase\n\n\nclass BBase(ABase):\n    pass\n\n\n"
                "class B(BBase):\n    def test_b(self):\n        run()\n",
            ),
        )
        expected = [("tests/test_a.py::A.test_a", "no-assertion"), ("tests/test_b.py::B.test_b", "no-assertion")]
        for ordered in (modules, modules[::-1]):
            with self.subTest(first=ordered[0][0]):
                self.assertEqual(ci.hollow_tests_in_sources(dict(ordered)), expected)

    def test_mixins_imported_from_other_test_modules_are_examined(self):
        sources = {
            "tests/mixins.py": (
                "class SharedChecks:\n    def test_shared(self):\n        run()\n\n\n"
                "class Unused:\n    def test_unused(self):\n        run()\n"
            ),
            "tests/test_c.py": (
                "import unittest\nfrom tests.mixins import SharedChecks\n\n\n"
                "class C(SharedChecks, unittest.TestCase):\n    pass\n"
            ),
        }
        self.assertEqual(
            ci.hollow_tests_in_sources(sources), [("tests/mixins.py::SharedChecks.test_shared", "no-assertion")]
        )

    def test_side_effect_free_builtins_do_not_hand_a_value_to_project_code(self):
        for body in (
            "x = 4\nprint(x)\nself.assertEqual(x, 4)\n",
            "x = [1]\nself.assertEqual(len(x), 1)\n",
            "x = 4\ny = str(x)\nself.assertEqual(y, '4')\n",
            "x = (2, 1)\nself.assertEqual(sorted(x), [1, 2])\n",
        ):
            with self.subTest(body=body):
                self.assertEqual(hollow_body(body), [(sample_id("Sample.test_x"), "tautology")])
        # Rebound names are not the builtins: project code may run.
        self.assertEqual(hollow_body("len = run\nx = [1]\nself.assertEqual(len(x), 1)\n"), [])
        shadowed = """
        import unittest
        from project import print


        class Sample(unittest.TestCase):
            def test_x(self):
                x = 4
                print(x)
                self.assertEqual(x, 4)
        """
        self.assertEqual(hollow_module(shadowed), [])


HOLLOW_FIXTURE_FILES = {
    "tests/test_a.py": (
        "import unittest\n\n\nclass A(unittest.TestCase):\n"
        "    def test_hollow(self):\n        run()\n\n"
        "    def test_fine(self):\n        self.assertEqual(run(), 1)\n"
    ),
    "tests/sub/test_b.py": "import unittest\n\n\nclass B(unittest.TestCase):\n    def test_hollow(self):\n        run()\n",
    # Not a test module: never examined, however hollow it looks.
    "tests/helper.py": "import unittest\n\n\nclass H(unittest.TestCase):\n    def test_hollow(self):\n        run()\n",
}
HOLLOW_FIXTURE_ENTRIES = [
    {"test": "tests/sub/test_b.py::B.test_hollow", "rule": "no-assertion"},
    {"test": "tests/test_a.py::A.test_hollow", "rule": "no-assertion"},
]


def write_hollow_fixture(root, entries):
    for name, text in HOLLOW_FIXTURE_FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    write_hollow_baseline(root, entries)


def write_hollow_baseline(root, entries):
    path = root / ci.HOLLOW_BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "hollow_tests": entries}), encoding="utf-8")


class HollowTestRatchetTests(unittest.TestCase):
    """The committed baseline records every legacy hollow test; new ones and stale entries both fail."""

    def test_hollow_baseline_parses_entries(self):
        payload = {
            "schema_version": 1,
            "hollow_tests": [
                {"test": "tests/test_a.py::A.test_x", "rule": "no-assertion"},
                {"test": "tests/test_a.py::A.test_y", "rule": "mock-only"},
                {"test": "tests/test_b.py::test_z", "rule": "tautology"},
            ],
        }
        self.assertEqual(
            ci.hollow_baseline(payload),
            {
                ("tests/test_a.py::A.test_x", "no-assertion"),
                ("tests/test_a.py::A.test_y", "mock-only"),
                ("tests/test_b.py::test_z", "tautology"),
            },
        )

    def test_invalid_hollow_baselines_fail(self):
        entry = {"test": "tests/test_a.py::A.test_x", "rule": "no-assertion"}
        invalid_schemas = (
            [],
            {"schema_version": 2, "hollow_tests": []},
            {"schema_version": "1", "hollow_tests": []},
            {"schema_version": True, "hollow_tests": []},
            {"schema_version": 1},
            {"schema_version": 1, "hollow_tests": {}},
        )
        for payload in invalid_schemas:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, r"^invalid hollow-test baseline schema$"):
                    ci.hollow_baseline(payload)
        invalid_entries = (
            ["tests/test_a.py::A.test_x"],
            [{"test": "tests/test_a.py::A.test_x"}],
            [{**entry, "rule": "weak"}],
            [{**entry, "rule": ["no-assertion"]}],
            [{**entry, "test": 3}],
            [{**entry, "test": "tests/test_a.py"}],
            [{**entry, "count": 1}],
            [entry, entry],
            [entry, {**entry, "rule": "mock-only"}],
        )
        for entries in invalid_entries:
            with self.subTest(entries=entries):
                with self.assertRaisesRegex(ValueError, r"^duplicate or invalid hollow-test baseline entry$"):
                    ci.hollow_baseline({"schema_version": 1, "hollow_tests": entries})

    def test_unsorted_hollow_baseline_fails(self):
        entries = [
            {"test": "tests/test_b.py::B.test_x", "rule": "no-assertion"},
            {"test": "tests/test_a.py::A.test_x", "rule": "no-assertion"},
        ]
        with self.assertRaisesRegex(ValueError, r"^hollow-test baseline entries must be sorted$"):
            ci.hollow_baseline({"schema_version": 1, "hollow_tests": entries})

    def test_hollow_scan_reads_only_tracked_regular_test_modules(self):
        hollow_module_text = HOLLOW_FIXTURE_FILES["tests/test_a.py"]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ci.git(root, "init", "-b", "main")
            write_hollow_fixture(root, HOLLOW_FIXTURE_ENTRIES)
            (root / "tests/test_link.py").symlink_to("test_a.py")
            ci.git(root, "add", "tests")
            (root / "tests/test_untracked.py").write_text(hollow_module_text, encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertIsNone(ci.check_hollow_tests(root))
            self.assertEqual(
                output.getvalue(), "No new hollow tests; 2 explicitly recorded legacy hollow tests remain.\n"
            )

    def test_repository_check_scans_the_files_git_tracks(self):
        tracked = b"".join(name.encode() + b"\0" for name in HOLLOW_FIXTURE_FILES)

        def fake_git(repo, *args):
            return tracked if args[:1] == ("ls-files",) else b""

        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_hollow_fixture(root, HOLLOW_FIXTURE_ENTRIES)
            (root / "tests/test_untracked.py").write_text(HOLLOW_FIXTURE_FILES["tests/test_a.py"], encoding="utf-8")
            with (
                patch.object(ci, "workflow_documents", return_value={}),
                patch.object(ci, "check_workflows"),
                patch.object(ci, "check_mutation_policy"),
                patch.object(ci, "git", side_effect=fake_git),
                redirect_stdout(io.StringIO()),
            ):
                self.assertIsNone(ci.check_repository(root))

    def test_hollow_test_changes_lists_new_then_resolved(self):
        a = ("tests/test_a.py::A.test_a", "no-assertion")
        b = ("tests/test_a.py::A.test_b", "mock-only")
        c = ("tests/test_a.py::A.test_c", "tautology")
        self.assertEqual(
            ci.hollow_test_changes({a, b}, {b, c}),
            [
                "NEW: tests/test_a.py::A.test_a: no-assertion",
                "REMOVE FIXED BASELINE ENTRY: tests/test_a.py::A.test_c: tautology",
            ],
        )
        self.assertEqual(ci.hollow_test_changes({a, b}, {a, b}), [])
        self.assertEqual(
            ci.hollow_test_changes({(a[0], "mock-only")}, {a}),
            [
                "NEW: tests/test_a.py::A.test_a: mock-only",
                "REMOVE FIXED BASELINE ENTRY: tests/test_a.py::A.test_a: no-assertion",
            ],
        )

    def test_check_hollow_tests_ratchets_on_synthetic_repo(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_hollow_fixture(root, [])
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaisesRegex(ValueError, r"^hollow tests changed"):
                ci.check_hollow_tests(root)
            self.assertEqual(
                output.getvalue(),
                "NEW: tests/sub/test_b.py::B.test_hollow: no-assertion\n"
                "NEW: tests/test_a.py::A.test_hollow: no-assertion\n",
            )

            write_hollow_baseline(root, HOLLOW_FIXTURE_ENTRIES)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertIsNone(ci.check_hollow_tests(root))
            self.assertEqual(
                output.getvalue(), "No new hollow tests; 2 explicitly recorded legacy hollow tests remain.\n"
            )

            (root / "tests/test_a.py").write_text(
                "import unittest\n\n\nclass A(unittest.TestCase):\n"
                "    def test_hollow(self):\n        self.assertEqual(run(), 2)\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaisesRegex(ValueError, r"^hollow tests changed"):
                ci.check_hollow_tests(root)
            self.assertEqual(
                output.getvalue(), "REMOVE FIXED BASELINE ENTRY: tests/test_a.py::A.test_hollow: no-assertion\n"
            )

    def test_cli_and_repository_check_enforce_ratchet(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_hollow_fixture(root, [])
            stderr = io.StringIO()
            with patch.object(ci, "ROOT", root), redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                self.assertEqual(ci.main(["hollow-tests"]), 1)
            self.assertIn("CI check failed: hollow tests changed", stderr.getvalue())

            tracked = b"".join(name.encode() + b"\0" for name in HOLLOW_FIXTURE_FILES)
            with (
                patch.object(ci, "workflow_documents", return_value={}),
                patch.object(ci, "check_workflows"),
                patch.object(ci, "check_mutation_policy"),
                patch.object(ci, "git", side_effect=lambda repo, *args: tracked if args[:1] == ("ls-files",) else b""),
            ):
                with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, r"^hollow tests changed"):
                    ci.check_repository(root)
                write_hollow_baseline(root, HOLLOW_FIXTURE_ENTRIES)
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertIsNone(ci.check_repository(root))
            self.assertEqual(
                output.getvalue().splitlines()[-1],
                "Workflow policies, tracked-file syntax, blob sizes, whitespace and hollow tests passed.",
            )

            with patch.object(ci, "ROOT", root), redirect_stdout(io.StringIO()):
                self.assertEqual(ci.main(["hollow-tests"]), 0)

            (root / ci.HOLLOW_BASELINE).unlink()
            stderr = io.StringIO()
            with patch.object(ci, "ROOT", root), redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                self.assertEqual(ci.main(["hollow-tests"]), 1)
            self.assertIn("CI check failed:", stderr.getvalue())

    def test_committed_tree_matches_committed_hollow_baseline(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertIsNone(ci.check_hollow_tests(ci.ROOT))
        self.assertRegex(
            output.getvalue(), r"^No new hollow tests; \d+ explicitly recorded legacy hollow tests remain\.\n$"
        )
        payload = json.loads((ci.ROOT / ci.HOLLOW_BASELINE).read_text(encoding="utf-8"))
        hardened = (
            "tests/test_e2e_live_fixes.py::",
            "tests/test_crew_leveling.py::",
            "tests/test_runtime_guards.py::",
        )
        self.assertEqual([entry for entry in payload["hollow_tests"] if entry["test"].startswith(hardened)], [])

    def test_hollow_baseline_is_canonical(self):
        text = (ci.ROOT / ci.HOLLOW_BASELINE).read_text(encoding="utf-8")
        payload = json.loads(text)
        self.assertEqual(text, json.dumps(payload, indent=2) + "\n")
        keys = [(entry["test"], entry["rule"]) for entry in payload["hollow_tests"]]
        self.assertEqual(keys, sorted(keys))


class MutationConfigPolicyTests(unittest.TestCase):
    """The mutation config split: sdk/training/ targets block merges (mutation-gate), the rest stay advisory."""

    def setUp(self):
        self.config = json.loads((ci.ROOT / ci.MUTATION_CONFIG).read_text(encoding="utf-8"))
        # Every committed target outside sdk/training/, in config order, derived independently of the code under
        # test, so adding an advisory target (a new policy or executor module) does not break these tests.
        self.advisory_paths = [t["path"] for t in self.config["targets"] if not t["path"].startswith("sdk/training/")]
        self.assertIn("sdk/crew_leveling.py", self.advisory_paths)

    def test_subset_splits_on_the_training_prefix(self):
        blocking = ci.mutation_config_subset(self.config, blocking=True)
        advisory = ci.mutation_config_subset(self.config, blocking=False)
        self.assertEqual(
            [target["path"] for target in blocking["targets"]], list(ci.REQUIRED_BLOCKING_MUTATION_TARGETS)
        )
        self.assertEqual([target["path"] for target in advisory["targets"]], self.advisory_paths)
        for subset in (blocking, advisory):
            self.assertEqual(
                {key: value for key, value in subset.items() if key != "targets"},
                {key: value for key, value in self.config.items() if key != "targets"},
            )
            self.assertEqual(set(subset), set(self.config))
        # Disjoint, together every target, each entry unchanged and in config order.
        merged = sorted(blocking["targets"] + advisory["targets"], key=self.config["targets"].index)
        self.assertEqual(merged, self.config["targets"])
        extra = {"path": "sdk/training_extra.py", "tests": ["tests.test_x"], "min_score": 0.5}
        widened = {**self.config, "targets": [*self.config["targets"], extra]}
        self.assertIn(extra, ci.mutation_config_subset(widened, blocking=False)["targets"])
        self.assertNotIn(extra, ci.mutation_config_subset(widened, blocking=True)["targets"])

    def test_empty_subset_is_refused(self):
        # mutation.py refuses an empty target list too, but only after the job has spent its setup.
        training_only = ci.mutation_config_subset(self.config, blocking=True)
        with self.assertRaisesRegex(ValueError, "no advisory targets"):
            ci.mutation_config_subset(training_only, blocking=False)
        self.assertEqual(ci.mutation_config_subset(training_only, blocking=True), training_only)

    def test_shipped_config_holds_the_training_gate(self):
        self.assertIsNone(ci.check_mutation_config(self.config))

    def weakened(self, path, change):
        config = copy.deepcopy(self.config)
        index = next(i for i, target in enumerate(config["targets"]) if target["path"] == path)
        change(config["targets"], index)
        return config

    def test_weakened_training_gate_is_refused(self):
        planner, parse = "sdk/training/planner.py", "sdk/training/parse.py"

        def set_score(score):
            return lambda targets, index: targets[index].update(min_score=score)

        missing = "missing the blocking target sdk/training/planner.py"
        floor = "sdk/training/planner.py needs a numeric min_score of at least 0.9"
        mutations = {
            "planner dropped": (planner, lambda targets, index: targets.pop(index), missing),
            "planner below 0.90": (planner, set_score(0.8999), floor),
            "bool min_score": (planner, set_score(True), floor),
            "string min_score": (planner, set_score("0.95"), floor),
            "parse loses a test module": (
                parse,
                lambda targets, index: targets[index]["tests"].remove("tests.test_training_parse"),
                "sdk/training/parse.py must run tests.test_training_planner and tests.test_training_parse",
            ),
            "path renamed": (
                planner,
                lambda targets, index: targets[index].update(path="sdk/training/plan.py"),
                missing,
            ),
        }
        for name, (path, change, fragment) in mutations.items():
            with self.subTest(mutation=name), self.assertRaisesRegex(ValueError, fragment):
                ci.check_mutation_config(self.weakened(path, change))
        self.assertIsNone(ci.check_mutation_config(self.weakened(planner, set_score(0.90))))

    def run_command(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = ci.main(["mutation-config", *argv])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_mutation_config_command_prints_a_config_mutation_loads(self):
        from scripts import mutation

        for selector, expected in (
            ("--blocking", list(ci.REQUIRED_BLOCKING_MUTATION_TARGETS)),
            ("--advisory", self.advisory_paths),
        ):
            with self.subTest(selector=selector), TemporaryDirectory() as temporary:
                code, stdout, stderr = self.run_command(selector)
                self.assertEqual(code, 0, stderr)
                path = Path(temporary) / "config.json"
                path.write_text(stdout, encoding="utf-8")
                loaded = mutation.load_config(path)
                self.assertEqual([target.path for target in loaded.targets], expected)

    def test_narrowed_shadowed_or_malformed_training_gate_is_refused(self):
        # Regression: functions/operators narrowed a blocking target to a few mutants and a tiny timeout turned every
        # mutant into a timeout (counted as killed, so a score of 1.0); a weak entry was hidden by a
        # later strong one for the same path (only the last was checked); NaN compared false with the floor; and a
        # config that is not an object of target objects raised AttributeError instead of a ValueError.
        planner = "sdk/training/planner.py"

        def add(key, value):
            return lambda targets, index: targets[index].update({key: value})

        def shadowed(targets, index):
            targets.insert(index, {**targets[index], "min_score": 0.1})

        floor = f"{planner} needs a numeric min_score of at least 0.9"
        whole = f"{planner} must mutate the whole file with the baseline's timeout, without"
        for name, (change, fragment) in {
            "functions": (add("functions", ["plan"]), f"{whole} functions"),
            "operators": (add("operators", ["compare"]), f"{whole} operators"),
            "tiny timeout": (add("timeout", 0.001), f"{whole} timeout"),
            "weak entry before a strong one": (shadowed, floor),
            "NaN floor": (add("min_score", float("nan")), floor),
        }.items():
            with self.subTest(mutation=name), self.assertRaisesRegex(ValueError, fragment):
                ci.check_mutation_config(self.weakened(planner, change))
        malformed = "needs a targets list of objects"
        for name, config in {
            "a list": [],
            "targets an object": {**self.config, "targets": {}},
            "a target that is text": {**self.config, "targets": [*self.config["targets"], "sdk/x.py"]},
        }.items():
            with self.subTest(config=name):
                with self.assertRaisesRegex(ValueError, malformed):
                    ci.check_mutation_config(config)
                with self.assertRaisesRegex(ValueError, malformed):
                    ci.mutation_config_subset(config, blocking=True)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text("[]", encoding="utf-8")
            code, stdout, stderr = self.run_command("--advisory", "--config", str(path))
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn(malformed, stderr)

    def test_every_training_target_is_held_to_the_blocking_floor(self):
        # Regression: is_blocking_mutation_target makes every sdk/training/ target merge-blocking, but only the four
        # named files were checked, so a new training target blocked merges at a min_score of 0.1, with no tests or
        # with its mutants narrowed.
        executor = "sdk/training/executor.py"
        strong = {"path": executor, "tests": ["tests.test_x"], "min_score": 0.9, "equivalent": []}

        def config_with(change):
            target = copy.deepcopy(strong)
            change(target)
            return {**self.config, "targets": [*copy.deepcopy(self.config["targets"]), target]}

        def set_key(key, value):
            return lambda target: target.update({key: value})

        def drop_key(key):
            return lambda target: target.pop(key)

        floor = f"mutation target {executor} needs a numeric min_score of at least 0.9"
        tests = f"mutation target {executor} needs a non-empty tests list of module names"
        whole = f"mutation target {executor} must mutate the whole file with the baseline's timeout, without"
        for name, (change, fragment) in {
            "min_score 0.1": (set_key("min_score", 0.1), floor),
            "min_score just below": (set_key("min_score", 0.8999), floor),
            "min_score missing": (drop_key("min_score"), floor),
            "bool min_score": (set_key("min_score", True), floor),
            "string min_score": (set_key("min_score", "0.95"), floor),
            "NaN min_score": (set_key("min_score", float("nan")), floor),
            "infinite min_score": (set_key("min_score", float("inf")), floor),
            "no tests": (set_key("tests", []), tests),
            "tests missing": (drop_key("tests"), tests),
            "tests as text": (set_key("tests", "tests.test_x"), tests),
            "a blank test module": (set_key("tests", ["tests.test_x", ""]), tests),
            "a test module that is not text": (set_key("tests", [1]), tests),
            "functions": (set_key("functions", ["run"]), f"{whole} functions"),
            "operators": (set_key("operators", ["compare"]), f"{whole} operators"),
            "tiny timeout": (set_key("timeout", 0.001), f"{whole} timeout"),
        }.items():
            with self.subTest(mutation=name), self.assertRaisesRegex(ValueError, re.escape(fragment)):
                ci.check_mutation_config(config_with(change))
        # At the floor it passes and blocks; its tests need not be the four named files' test modules.
        accepted = config_with(lambda target: None)
        self.assertIsNone(ci.check_mutation_config(accepted))
        self.assertIn(strong, ci.mutation_config_subset(accepted, blocking=True)["targets"])
        # Targets outside sdk/training/ stay advisory and keep their own floor.
        advisory = {"path": "sdk/training_extra.py", "tests": ["tests.test_x"], "min_score": 0.1}
        widened = {**self.config, "targets": [*self.config["targets"], advisory]}
        self.assertIsNone(ci.check_mutation_config(widened))
        self.assertIn(advisory, ci.mutation_config_subset(widened, blocking=False)["targets"])

    def test_non_canonical_or_glob_paths_cannot_dodge_the_blocking_floor(self):
        # Regression: scripts/mutation.py normalizes "./sdk/training/x.py" to sdk/training/x.py (and expands globs),
        # but ci.py classified the raw text, so such a target ran as advisory below the 0.90 floor.
        weak = {"tests": ["tests.test_x"], "min_score": 0.1, "equivalent": []}
        canonical = "must be written as"
        glob = "a glob mutation target must not reach sdk/training/"
        for path, fragment in {
            "./sdk/training/x.py": canonical,
            "sdk//training/x.py": canonical,
            "sdk/./training/x.py": canonical,
            "sdk\\training\\x.py": canonical,
            "sdk/training/x.py/": canonical,
            "sdk/*/x.py": glob,
            "sdk/**/*.py": glob,
            "sdk/tr?ining/x.py": glob,
            "sdk/[t]raining/x.py": glob,
            "*/training/x.py": glob,
            "sdk/training/*.py": glob,
            "sdk/training/sub/*.py": glob,
            "sdk/training/**/x.py": glob,
        }.items():
            config = {**self.config, "targets": [*copy.deepcopy(self.config["targets"]), {"path": path, **weak}]}
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, re.escape(fragment)):
                ci.check_mutation_config(config)
            with self.subTest(path=path, command="mutation-config"), self.assertRaisesRegex(ValueError, fragment):
                ci.mutation_config_subset(config, blocking=False)
        # A glob that cannot reach sdk/training/ stays an allowed advisory target.
        fleet = {"path": "sdk/fleet/*.py", **weak}
        widened = {**self.config, "targets": [*self.config["targets"], fleet]}
        self.assertIsNone(ci.check_mutation_config(widened))
        self.assertIn(fleet, ci.mutation_config_subset(widened, blocking=False)["targets"])
        self.assertTrue(ci.is_blocking_mutation_target("sdk/training/x.py"))
        self.assertFalse(ci.is_blocking_mutation_target("sdk/training_extra.py"))

    def test_mutation_config_command_fails_on_weakened_config(self):
        weakened = self.weakened("sdk/training/planner.py", lambda targets, index: targets[index].update(min_score=0.5))
        for name, text in (("weakened", json.dumps(weakened)), ("not json", "{")):
            with self.subTest(config=name), TemporaryDirectory() as temporary:
                path = Path(temporary) / "config.json"
                path.write_text(text, encoding="utf-8")
                code, stdout, stderr = self.run_command("--blocking", "--config", str(path))
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("CI check failed", stderr)

    def test_advisory_subset_is_not_held_to_the_training_policy(self):
        # Regression: --advisory ran the training floor check, so a weakened training target also broke the advisory
        # job (a misleading harness error) and its crew_leveling/game_formulas scores were lost for that PR.
        weakened = self.weakened("sdk/training/planner.py", lambda targets, index: targets[index].update(min_score=0.5))
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(weakened), encoding="utf-8")
            code, stdout, stderr = self.run_command("--advisory", "--config", str(path))
            self.assertEqual((code, stderr), (0, ""))
            paths = [target["path"] for target in json.loads(stdout)["targets"]]
            self.assertEqual(paths, self.advisory_paths)
            code, stdout, _ = self.run_command("--blocking", "--config", str(path))
            self.assertEqual((code, stdout), (1, ""))

    def test_mutation_config_needs_exactly_one_selector(self):
        for argv in ((), ("--blocking", "--advisory")):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                ci.main(["mutation-config", *argv])
            self.assertEqual(raised.exception.code, 2)

    @skip_unless_dependency(yaml, "install .github/requirements-ci.txt for workflow checks")
    def test_repository_check_runs_mutation_config_policy(self):
        with (
            patch.object(ci, "check_mutation_config", side_effect=ValueError("sentinel")) as check,
            self.assertRaisesRegex(ValueError, "sentinel"),
        ):
            ci.check_repository(ci.ROOT)
        self.assertEqual(check.call_args.args, (self.config,))


@skip_unless_dependency(yaml, "install .github/requirements-ci.txt for workflow checks")
class WorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.documents = ci.workflow_documents(ci.ROOT)

    def test_actual_workflows_pass_policy(self):
        self.assertIsNone(ci.check_workflows(self.documents))

    def test_pull_requests_keep_github_default_merge_checkout(self):
        for filename in ("blocking-ci.yml", "automation-check.yml", "python-tests.yml"):
            for job in self.documents[filename]["jobs"].values():
                for step in job.get("steps", []):
                    if step.get("uses", "").startswith("actions/checkout@"):
                        self.assertNotIn("ref", step.get("with", {}))

    def test_merge_gate_cannot_be_skipped_or_filter_out_changes(self):
        for mutation in ("condition", "dependency", "paths", "merge_queue"):
            docs = copy.deepcopy(self.documents)
            if mutation == "condition":
                docs["blocking-ci.yml"]["jobs"]["required"]["if"] = "success()"
            elif mutation == "dependency":
                docs["blocking-ci.yml"]["jobs"]["required"]["needs"] = ["tests"]
            elif mutation == "paths":
                docs["blocking-ci.yml"]["on"]["pull_request"]["paths-ignore"] = ["docs/**"]
            else:
                del docs["blocking-ci.yml"]["on"]["merge_group"]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                ci.check_workflows(docs)

    def test_mutable_action_or_persisted_token_fails(self):
        for field, value in (("uses", "actions/checkout@v6"), ("with", {"persist-credentials": "true"})):
            docs = copy.deepcopy(self.documents)
            docs["blocking-ci.yml"]["jobs"]["required"]["steps"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                ci.check_workflows(docs)

    # Verifies: S06-R5
    def test_policy_covers_the_secret_holding_workflows(self):
        self.assertIn("daily-run.yml", ci.WORKFLOWS)
        self.assertIn("live-trial.yml", ci.WORKFLOWS)
        self.assertIn("live-trial-schedule.yml", ci.WORKFLOWS)
        for filename in ("daily-run.yml", "live-trial.yml", "live-trial-schedule.yml"):
            job_name, job = next(iter(self.documents[filename]["jobs"].items()))
            checkout = next(i for i, step in enumerate(job["steps"]) if "checkout@" in step.get("uses", ""))
            for mutation in ("unpinned", "timeout", "token", "permissions", "job_permissions"):
                docs = copy.deepcopy(self.documents)
                target = docs[filename]["jobs"][job_name]
                if mutation == "unpinned":
                    target["steps"][checkout]["uses"] = "actions/checkout@v6"
                elif mutation == "timeout":
                    del target["timeout-minutes"]
                elif mutation == "token":
                    target["steps"][checkout]["with"]["persist-credentials"] = "true"
                elif mutation == "permissions":
                    docs[filename]["permissions"] = {"contents": "write"}
                else:
                    target["permissions"] = {"contents": "write", "actions": "write"}
                with self.subTest(filename=filename, mutation=mutation), self.assertRaises(ValueError):
                    ci.check_workflows(docs)

    # Verifies: S06-R1, S06-R5
    def test_every_legacy_account_step_stays_skippable(self):
        job_name, job = next(iter(self.documents["daily-run.yml"]["jobs"].items()))
        steps = [i for i, step in enumerate(job["steps"]) if "PSS_ACCOUNT_" in json.dumps(step)]
        self.assertEqual(len(steps), 5)
        for mutation in ("dropped", "wrong_account", "two_accounts", "no_steps"):
            docs = copy.deepcopy(self.documents)
            target = docs["daily-run.yml"]["jobs"][job_name]["steps"]
            if mutation == "dropped":
                del target[steps[-1]]["if"]
            elif mutation == "wrong_account":
                target[steps[-1]]["if"] = ci.LEGACY_RUN_SKIP_CONDITION.format(account=4)
            elif mutation == "two_accounts":
                target[steps[0]]["env"]["EXTRA"] = "${{ secrets.PSS_ACCOUNT_5_EMAIL }}"
            else:
                for index in reversed(steps):
                    del target[index]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                ci.check_workflows(docs)

    def daily_run_steps(self, docs):
        job_name, _ = next(iter(self.documents["daily-run.yml"]["jobs"].items()))
        return docs["daily-run.yml"]["jobs"][job_name]["steps"]

    def assert_each_refused(self, mutations):
        """Each mutation alone, on a fresh copy, must fail check_workflows with its own rule's message."""
        for name, (mutate, fragment) in mutations.items():
            docs = copy.deepcopy(self.documents)
            mutate(docs)
            with self.subTest(mutation=name), self.assertRaisesRegex(ValueError, fragment):
                ci.check_workflows(docs)

    # Verifies: S06-R5
    def test_every_account_step_journals_training_under_its_own_alias(self):
        steps = self.daily_run_steps(self.documents)
        accounts = [i for i, step in enumerate(steps) if ci.LEGACY_ACCOUNT_SECRET_RE.search(json.dumps(step))]
        self.assertEqual(len(accounts), 5)
        for number, index in enumerate(accounts, start=1):
            with self.subTest(account=number):
                self.assertIn(f"--account-alias account-{number} ", steps[index]["run"])
                self.assertEqual(steps[index]["run"].count(ci.DAILY_TRAINING_JOURNAL_ARG), 1)
        last = accounts[-1]
        arg = ci.DAILY_TRAINING_JOURNAL_ARG

        def edit_run(old, new):
            def mutate(docs):
                step = self.daily_run_steps(docs)[last]
                self.assertIn(old, step["run"])
                step["run"] = step["run"].replace(old, new)

            return mutate

        def edit_step(change):
            return lambda docs: change(self.daily_run_steps(docs)[last])

        alias, journal, keep_going = (
            "must pass --account-alias account-5 exactly once",
            "must journal training",
            ("must keep continue-on-error: true"),
        )
        self.assert_each_refused(
            {
                "journal arg dropped": (edit_run(arg, ""), journal),
                "journal arg twice": (edit_run(arg, f"{arg} {arg}"), journal),
                "journal in the checkout": (edit_run("$RUNNER_TEMP", "$GITHUB_WORKSPACE"), journal),
                "alias dropped": (edit_run("--account-alias account-5", ""), alias),
                "another account's alias": (edit_run("--account-alias account-5", "--account-alias account-4"), alias),
                "default alias": (edit_run("--account-alias account-5", "--account-alias primary"), alias),
                "two alias flags": (
                    edit_run("--account-alias account-5", "--account-alias account-5 --account-alias=account-5"),
                    alias,
                ),
                "quoted wrong alias": (
                    edit_run("--account-alias account-5", '--account-alias "account-4"'),
                    alias,
                ),
                "continue-on-error deleted": (edit_step(lambda step: step.pop("continue-on-error")), keep_going),
                "continue-on-error false": (
                    edit_step(lambda step: step.update({"continue-on-error": "false"})),
                    keep_going,
                ),
            }
        )

    def test_quoted_account_alias_is_accepted(self):
        # Regression: the alias pattern kept shell quotes, so a correctly quoted --account-alias "account-5" (the
        # same style as the quoted journal directory) was refused.
        steps = self.daily_run_steps(self.documents)
        last = [i for i, step in enumerate(steps) if ci.LEGACY_ACCOUNT_SECRET_RE.search(json.dumps(step))][-1]
        for quoted in ('--account-alias "account-5"', "--account-alias 'account-5'", '--account-alias="account-5"'):
            docs = copy.deepcopy(self.documents)
            step = self.daily_run_steps(docs)[last]
            step["run"] = step["run"].replace("--account-alias account-5", quoted)
            with self.subTest(alias=quoted):
                self.assertIn(quoted, step["run"])
                self.assertIsNone(ci.check_workflows(docs))

    def training_step_index(self, docs, marker):
        return next(i for i, step in enumerate(self.daily_run_steps(docs)) if marker(step))

    @staticmethod
    def is_report(step):
        return ci.DAILY_TRAINING_REPORT_COMMAND in step.get("run", "")

    @staticmethod
    def is_upload(step):
        return step.get("uses", "").startswith("actions/upload-artifact@")

    def move_step(self, marker, before):
        """Move the step ``marker`` finds to just before the step ``before`` finds."""

        def mutate(docs):
            steps = self.daily_run_steps(docs)
            step = steps.pop(self.training_step_index(docs, marker))
            steps.insert(self.training_step_index(docs, before), step)

        return mutate

    def edit_training_step(self, marker, change):
        return lambda docs: change(self.daily_run_steps(docs)[self.training_step_index(docs, marker)])

    def test_training_report_step_always_runs_after_every_account(self):
        steps = self.daily_run_steps(self.documents)
        (report,) = [step for step in steps if self.is_report(step)]
        self.assertEqual(report["if"], "always()")
        self.assertNotIn("env", report)
        last_account = max(i for i, step in enumerate(steps) if ci.LEGACY_ACCOUNT_SECRET_RE.search(json.dumps(step)))
        self.assertGreater(steps.index(report), last_account)

        def edit(change):
            return self.edit_training_step(self.is_report, change)

        def edit_run(old, new):
            return edit(lambda step: step.update(run=step["run"].replace(old, new)))

        def duplicate(docs):
            steps = self.daily_run_steps(docs)
            steps.append(copy.deepcopy(steps[self.training_step_index(docs, self.is_report)]))

        def last_account_step(step):
            return step.get("name") == "Process account 5"

        self.assert_each_refused(
            {
                "report deleted": (
                    lambda docs: self.daily_run_steps(docs).pop(self.training_step_index(docs, self.is_report)),
                    "exactly one training report step",
                ),
                "runs only on success": (edit(lambda step: step.update({"if": "success()"})), "if: always()"),
                "condition deleted": (edit(lambda step: step.pop("if")), "if: always()"),
                "before account 5": (self.move_step(self.is_report, last_account_step), "after every account step"),
                "second report": (duplicate, "exactly one training report step"),
                "no job summary": (edit_run('>> "$GITHUB_STEP_SUMMARY"', "> /dev/null"), "job summary"),
                "no JSON report": (edit_run(" --report-format json", ""), "JSON report"),
                "journals read from the checkout": (
                    edit_run('root="$RUNNER_TEMP/tachikoma-training"', 'root="$GITHUB_WORKSPACE/tachikoma-training"'),
                    "must read the journals under \\$RUNNER_TEMP/tachikoma-training",
                ),
                "continue-on-error": (
                    edit(lambda step: step.update({"continue-on-error": "true"})),
                    "report step must not carry continue-on-error",
                ),
                "given a password file": (
                    edit_run("--report-format json", '--report-format json --password-file "$pw"'),
                    "must not be given credentials",
                ),
                "reads a secret": (
                    edit(lambda step: step.update(env={"X": "${{ secrets.email_password }}"})),
                    "only the account steps may read secrets",
                ),
            }
        )

    def test_training_artifact_uploads_only_the_training_directory(self):
        steps = self.daily_run_steps(self.documents)
        (upload,) = [step for step in steps if self.is_upload(step)]
        self.assertEqual(upload["with"]["path"], "${{ runner.temp }}/tachikoma-training")
        self.assertEqual(upload["with"]["retention-days"], "14")
        self.assertGreater(steps.index(upload), self.training_step_index(self.documents, self.is_report))

        def edit_with(change):
            return self.edit_training_step(self.is_upload, lambda step: change(step["with"]))

        def set_with(key, value):
            return edit_with(lambda inputs: inputs.update({key: value}))

        def duplicate(docs):
            steps = self.daily_run_steps(docs)
            steps.append(copy.deepcopy(steps[self.training_step_index(docs, self.is_upload)]))

        def own_job(docs):
            # Another job runs on another runner, where $RUNNER_TEMP holds no journals or report.
            upload = self.daily_run_steps(docs).pop(self.training_step_index(docs, self.is_upload))
            docs["daily-run.yml"]["jobs"]["training-upload"] = {
                "runs-on": "ubuntu-latest",
                "timeout-minutes": "10",
                "steps": [upload],
            }

        path, retention = "must upload only", "retention-days between 1 and 30"
        self.assert_each_refused(
            {
                "upload deleted": (
                    lambda docs: self.daily_run_steps(docs).pop(self.training_step_index(docs, self.is_upload)),
                    "exactly one artifact upload step",
                ),
                "condition deleted": (
                    self.edit_training_step(self.is_upload, lambda step: step.pop("if")),
                    "upload step must run with if: always()",
                ),
                "whole runner temp": (set_with("path", "${{ runner.temp }}"), path),
                "checkout": (set_with("path", "."), path),
                "checkout as a second line": (
                    set_with("path", "${{ runner.temp }}/tachikoma-training\n${{ github.workspace }}\n"),
                    path,
                ),
                "before the report": (
                    self.move_step(self.is_upload, self.is_report),
                    "upload step must come after the training report step",
                ),
                "upload in another job": (own_job, "upload step must run in the training report step's job"),
                "retention deleted": (edit_with(lambda inputs: inputs.pop("retention-days")), retention),
                "retention 90": (set_with("retention-days", "90"), retention),
                "retention 0": (set_with("retention-days", "0"), retention),
                "retention not a number": (set_with("retention-days", "x"), retention),
                "hidden files": (set_with("include-hidden-files", "true"), "unexpected upload inputs"),
                "second upload": (duplicate, "exactly one artifact upload step"),
                "reads a secret": (
                    self.edit_training_step(
                        self.is_upload, lambda step: step.update(env={"X": "${{ secrets.email_password }}"})
                    ),
                    "only the account steps may read secrets",
                ),
            }
        )

    def test_training_steps_accept_either_spelling_of_always(self):
        # Regression: the report and upload checks compared `if` with the text "always()", so the equivalent
        # `${{ always() }}` was refused. Any other condition, inside or outside an expression, is still refused.
        for marker, kind in ((self.is_report, "report"), (self.is_upload, "upload")):
            fragment = re.escape(f"the daily-run training {kind} step must run with if: always()")
            for spelling in ("${{ always() }}", "${{always()}}", "  ${{\n  always()\n}}  ", " always() "):
                docs = copy.deepcopy(self.documents)
                self.daily_run_steps(docs)[self.training_step_index(docs, marker)]["if"] = spelling
                with self.subTest(step=kind, accepted=spelling):
                    self.assertIsNone(ci.check_workflows(docs))
            for spelling in (
                "${{ success() }}",
                "${{ always() && false }}",
                "${{ always() }} && false",
                "${{ always() }} || ${{ false }}",
                "${{ }}",
                "",
            ):
                docs = copy.deepcopy(self.documents)
                self.daily_run_steps(docs)[self.training_step_index(docs, marker)]["if"] = spelling
                with self.subTest(step=kind, refused=spelling), self.assertRaisesRegex(ValueError, fragment):
                    ci.check_workflows(docs)

    # Verifies: S06-R5
    def test_only_account_steps_read_secrets(self):
        job_name, _ = next(iter(self.documents["daily-run.yml"]["jobs"].items()))

        def install_step(docs):
            return next(step for step in self.daily_run_steps(docs) if step.get("name") == "Install dependencies")

        secret = "${{ secrets.email_password }}"
        fragment = "only the account steps may read secrets"
        self.assert_each_refused(
            {
                "install step": (lambda docs: install_step(docs).update(env={"X": secret}), fragment),
                "job env": (lambda docs: docs["daily-run.yml"]["jobs"][job_name].update(env={"X": secret}), fragment),
                "workflow env": (lambda docs: docs["daily-run.yml"].update(env={"X": secret}), fragment),
                "all secrets in the report": (
                    self.edit_training_step(
                        self.is_report, lambda step: step.update(run=step["run"] + 'echo "${{ toJSON(secrets) }}"\n')
                    ),
                    fragment,
                ),
                # Regression: the expression match stopped at the first `}`, so a `}` inside the expression (here a
                # format() placeholder) hid the secrets reference after it.
                "secret behind a format() placeholder": (
                    self.edit_training_step(
                        self.is_report,
                        lambda step: step.update(env={"X": "${{ format('{0}', secrets.email_password) }}"}),
                    ),
                    fragment,
                ),
                "account secret behind a format() placeholder": (
                    self.edit_training_step(
                        self.is_report,
                        lambda step: step.update(env={"X": "${{ format('{0}', secrets.PSS_ACCOUNT_1_EMAIL) }}"}),
                    ),
                    fragment,
                ),
                "multiline expression": (
                    self.edit_training_step(
                        self.is_report,
                        lambda step: step.update(
                            run=step["run"] + "echo \"${{ format(\n  '{0}-{1}',\n  github.run_id,\n"
                            '  secrets.email_password\n) }}"\n'
                        ),
                    ),
                    fragment,
                ),
            }
        )
        # The match ends at the expression's closing `}}`, on raw (unserialised) text as on a step's JSON.
        self.assertIsNotNone(ci.SECRETS_EXPRESSION_RE.search("${{\n  format('{0}',\n  secrets.x)\n}}"))
        self.assertIsNone(ci.SECRETS_EXPRESSION_RE.search("${{ runner.temp }}/secrets ${{ github.run_id }}"))

    def test_mutation_gate_stays_blocking(self):
        gate = self.documents["python-tests.yml"]["jobs"][ci.MUTATION_GATE_JOB]
        self.assertEqual(gate["if"], "${{ inputs.run-mutation }}")
        (step,) = [step for step in gate["steps"] if "scripts/mutation.py" in step.get("run", "")]
        self.assertIn(ci.MUTATION_GATE_COMMAND, step["run"])
        self.assertLess(int(step["timeout-minutes"]), int(gate["timeout-minutes"]))

        def edit_gate(change):
            return lambda docs: change(docs["python-tests.yml"]["jobs"][ci.MUTATION_GATE_JOB])

        def edit_step(change):
            def mutate(docs):
                steps = docs["python-tests.yml"]["jobs"][ci.MUTATION_GATE_JOB]["steps"]
                change(next(step for step in steps if "scripts/mutation.py" in step.get("run", "")))

            return mutate

        def edit_run(change):
            return edit_step(lambda step: step.update(run=change(step["run"])))

        def install_continues(docs):
            steps = docs["python-tests.yml"]["jobs"][ci.MUTATION_GATE_JOB]["steps"]
            next(step for step in steps if "pip install" in step.get("run", ""))["continue-on-error"] = "true"

        def mutation_step_index(steps):
            return next(i for i, step in enumerate(steps) if "scripts/mutation.py" in step.get("run", ""))

        def no_mutation_step(job):
            job["steps"].pop(mutation_step_index(job["steps"]))

        def two_mutation_steps(job):
            steps = job["steps"]
            index = mutation_step_index(steps)
            steps.insert(index + 1, copy.deepcopy(steps[index]))

        blocking_ci = "Blocking CI must run the mutation-gate job"
        swallow = "must not swallow"
        one_step = "mutation-gate job must run scripts/mutation.py in exactly one step"
        self.assert_each_refused(
            {
                "no mutation step": (edit_gate(no_mutation_step), one_step),
                "two mutation steps": (edit_gate(two_mutation_steps), one_step),
                "job deleted": (
                    lambda docs: docs["python-tests.yml"]["jobs"].pop(ci.MUTATION_GATE_JOB),
                    "needs the mutation-gate job",
                ),
                "job continue-on-error": (
                    edit_gate(lambda job: job.update({"continue-on-error": "true"})),
                    "mutation-gate job must not carry continue-on-error",
                ),
                "step continue-on-error": (
                    edit_step(lambda step: step.update({"continue-on-error": "true"})),
                    "mutation-gate step must not carry continue-on-error",
                ),
                "|| true": (edit_run(lambda run: run.rstrip("\n") + " || true\n"), swallow),
                "exit 0": (edit_run(lambda run: run + "exit 0\n"), swallow),
                # These three name the required-text rule, not the prelude rule that check_mutation_gate_command
                # would raise for the same edit.
                "no pipefail": (
                    edit_run(lambda run: run.replace("set -euo pipefail\n", "")),
                    "mutation-gate step must run set -euo pipefail",
                ),
                "advisory targets": (
                    edit_run(lambda run: run.replace("mutation-config --blocking", "mutation-config --advisory")),
                    re.escape(f"mutation-gate step must run {ci.MUTATION_GATE_COMMAND}"),
                ),
                "default config": (
                    edit_run(lambda run: run.replace(' --config "$config"', "")),
                    re.escape('mutation-gate step must run --config "$config"'),
                ),
                "never runs": (edit_gate(lambda job: job.update({"if": "${{ false }}"})), "inputs.run-mutation"),
                "install continue-on-error": (install_continues, "only its upload step may carry continue-on-error"),
                "blocking CI skips it": (
                    lambda docs: docs["blocking-ci.yml"]["jobs"]["tests"]["with"].update({"run-mutation": "false"}),
                    blocking_ci,
                ),
                "blocking CI passes no inputs": (
                    lambda docs: docs["blocking-ci.yml"]["jobs"]["tests"].pop("with"),
                    blocking_ci,
                ),
            }
        )

    def test_mutation_gate_job_runs_no_other_commands(self):
        # Regression: only the mutation step was pinned, so an earlier step in the same job could rewrite the config,
        # delete the training tests or replace sdk/training before the checked command ran.
        def insert_step(step, *, before_mutation=True):
            def mutate(docs):
                steps = docs["python-tests.yml"]["jobs"][ci.MUTATION_GATE_JOB]["steps"]
                index = next(i for i, other in enumerate(steps) if "scripts/mutation.py" in other.get("run", ""))
                steps.insert(index if before_mutation else index + 1, step)

            return mutate

        def edit_install(change):
            def mutate(docs):
                steps = docs["python-tests.yml"]["jobs"][ci.MUTATION_GATE_JOB]["steps"]
                change(next(step for step in steps if "pip install" in step.get("run", "")))

            return mutate

        other_command = "mutation-gate job may run only"
        other_action = "mutation-gate job may use only"
        sed = "sed -i 's/1.0/0.0/' automation/mutation/config.json"
        self.assert_each_refused(
            {
                "config rewritten first": (insert_step({"run": sed}), other_command),
                "tests deleted first": (insert_step({"run": "rm tests/test_training_planner.py"}), other_command),
                "command after the gate": (insert_step({"run": "true"}, before_mutation=False), other_command),
                "install widened": (
                    edit_install(lambda step: step.update(run=step["run"] + " && rm -r tests")),
                    other_command,
                ),
                "third-party action": (
                    insert_step({"uses": "someone/rewrite@" + "a" * 40}),
                    other_action,
                ),
                "pinned first-party action": (insert_step({"uses": "actions/github-script@" + "b" * 40}), other_action),
            }
        )

    def test_mutation_gate_command_cannot_be_narrowed_or_skipped(self):
        # Regression: the gate only looked for required substrings, so a skipped step or a narrowed or inverted
        # scripts/mutation.py command passed the policy while measuring nothing that could fail.
        def edit_step(change):
            def mutate(docs):
                steps = docs["python-tests.yml"]["jobs"][ci.MUTATION_GATE_JOB]["steps"]
                change(next(step for step in steps if "scripts/mutation.py" in step.get("run", "")))

            return mutate

        def edit_command(old, new):
            def change(step):
                self.assertIn(old, step["run"])
                step["run"] = step["run"].replace(old, new)

            return edit_step(change)

        command = "python scripts/mutation.py --config"
        plain, swallow, narrowing = "must end in one plain python scripts/mutation.py", "must not swallow", "takes only"
        self.assert_each_refused(
            {
                "step skipped": (edit_step(lambda step: step.update({"if": "${{ false }}"})), "its own if: condition"),
                "min score zero": (
                    edit_command(command, "python scripts/mutation.py --min-score 0 --config"),
                    narrowing,
                ),
                "one target": (
                    edit_command(command, "python scripts/mutation.py --target sdk/training/model.py --config"),
                    narrowing,
                ),
                "list only": (edit_command(command, "python scripts/mutation.py --list --config"), narrowing),
                "some mutants": (
                    edit_command(command, "python scripts/mutation.py --only x:1:1:op:0 --config"),
                    narrowing,
                ),
                "one operator": (
                    edit_command(command, "python scripts/mutation.py --operators=compare --config"),
                    narrowing,
                ),
                "inverted": (edit_command(command, "! " + command), plain),
                "inside if": (
                    edit_step(
                        lambda step: step.update(run=step["run"].replace(command, "if " + command) + "then :; fi\n")
                    ),
                    "must run exactly set -euo pipefail",
                ),
                "errexit off": (
                    edit_step(
                        lambda step: step.update(
                            run=step["run"].replace("set -euo pipefail", "set -euo pipefail\nset +e") + "true\n"
                        )
                    ),
                    swallow,
                ),
                "exit trap": (
                    edit_step(lambda step: step.update(run="trap 'exit $((0))' EXIT\n" + step["run"])),
                    swallow,
                ),
                "errexit off by name": (
                    edit_step(
                        lambda step: step.update(
                            run=step["run"].replace("set -euo pipefail", "set -euo pipefail\nset +o errexit") + "true\n"
                        )
                    ),
                    swallow,
                ),
                "checked config overwritten": (
                    edit_command(command, "printf '{}' > \"$config\"\n" + command),
                    "must run exactly set -euo pipefail",
                ),
                "another config file": (
                    edit_command('--config "$config"', '--config "$config" --config other.json'),
                    'must read --config "\\$config" once',
                ),
                "second config given with =": (
                    edit_command('--config "$config"', '--config "$config" --config=other.json'),
                    'must read --config "\\$config" once',
                ),
                "config value suffixed": (
                    edit_command('--config "$config"', '--config "$config"x'),
                    'must read --config "\\$config" once',
                ),
                "chained command": (edit_command("--jobs 4", "--jobs 4 ; true"), plain),
                "piped command": (edit_command("--jobs 4", "--jobs 4 | cat"), plain),
                "backgrounded command": (edit_command("--jobs 4", "--jobs 4 &"), plain),
                "redirected status": (edit_command("--jobs 4", "--jobs 4 >/dev/null"), plain),
            }
        )

    def test_training_report_must_share_the_account_job_and_keep_its_status(self):
        # Regression: a report moved to its own job (another runner, no journals) passed because that job's
        # last account index was -1, and `|| true` in the report run was not refused.
        job_name, _ = next(iter(self.documents["daily-run.yml"]["jobs"].items()))

        def own_job(docs):
            jobs = docs["daily-run.yml"]["jobs"]
            steps = jobs[job_name]["steps"]
            report = steps.pop(self.training_step_index(docs, self.is_report))
            upload = steps.pop(self.training_step_index(docs, self.is_upload))
            jobs["training-report"] = {"runs-on": "ubuntu-latest", "timeout-minutes": "10", "steps": [report, upload]}

        def edit_run(change):
            return self.edit_training_step(self.is_report, lambda step: step.update(run=change(step["run"])))

        swallow = "report step must not swallow"
        self.assert_each_refused(
            {
                "report in its own job": (own_job, "after every account step, in their job"),
                "|| true": (
                    edit_run(
                        lambda run: run.replace(
                            '> "$root/report/training-report.md"', '> "$root/report/training-report.md" || true'
                        )
                    ),
                    swallow,
                ),
                "exit 0": (edit_run(lambda run: run + "exit 0\n"), swallow),
                "errexit off": (
                    edit_run(lambda run: run.replace("set -euo pipefail", "set -uo pipefail")),
                    "set -euo pipefail",
                ),
            }
        )

    def test_malformed_training_steps_raise_value_error(self):
        # Regression: a mapping where a scalar belongs, or `with:` given as text, raised AttributeError, which
        # scripts/ci.py's main does not catch, instead of a ValueError naming the rule.
        def account_run_mapping(docs):
            step = next(s for s in self.daily_run_steps(docs) if s.get("name") == "Process account 5")
            step["run"] = {"script": step["run"]}

        self.assert_each_refused(
            {
                "account run is a mapping": (account_run_mapping, "must pass --account-alias account-5"),
                "upload with is text": (
                    self.edit_training_step(self.is_upload, lambda step: step.update({"with": "path"})),
                    "must upload only",
                ),
                "upload path is a list": (
                    self.edit_training_step(self.is_upload, lambda step: step["with"].update(path=["a"])),
                    "must upload only",
                ),
            }
        )

    def test_training_upload_cannot_fail_the_daily_run(self):
        # Regression: the upload had no continue-on-error, so an artifact service failure turned the daily run red
        # although every account step and the report had passed.
        (upload,) = [step for step in self.daily_run_steps(self.documents) if self.is_upload(step)]
        self.assertEqual(upload["continue-on-error"], "true")
        fragment = "upload step must keep continue-on-error: true"
        self.assert_each_refused(
            {
                "deleted": (
                    self.edit_training_step(self.is_upload, lambda step: step.pop("continue-on-error")),
                    fragment,
                ),
                "false": (
                    self.edit_training_step(self.is_upload, lambda step: step.update({"continue-on-error": "false"})),
                    fragment,
                ),
            }
        )

    def test_daily_training_refuses_a_step_with_several_accounts_on_its_own(self):
        # Regression: check_daily_training popped an arbitrary account from a step naming several and relied on
        # check_legacy_run having refused that step first.
        docs = copy.deepcopy(self.documents)
        step = next(s for s in self.daily_run_steps(docs) if s.get("name") == "Process account 5")
        step["env"]["OTHER"] = "${{ secrets.PSS_ACCOUNT_4_EMAIL }}"
        with self.assertRaisesRegex(ValueError, "reads several accounts' secrets"):
            ci.check_daily_training(docs["daily-run.yml"])

    def test_trial_schedule_runs_only_on_its_schedule(self):
        self.assertEqual(set(self.documents["live-trial-schedule.yml"]["on"]), {"schedule"})
        for trigger in ("push", "pull_request", "workflow_dispatch", "workflow_run"):
            docs = copy.deepcopy(self.documents)
            docs["live-trial-schedule.yml"]["on"][trigger] = {}
            with self.subTest(trigger=trigger), self.assertRaisesRegex(ValueError, "only on its schedule"):
                ci.check_workflows(docs)

    def test_live_trial_runs_only_on_manual_dispatch(self):
        for trigger in ("schedule", "push", "pull_request"):
            docs = copy.deepcopy(self.documents)
            docs["live-trial.yml"]["on"][trigger] = {}
            with self.subTest(trigger=trigger), self.assertRaisesRegex(ValueError, "manual dispatch"):
                ci.check_workflows(docs)

    def test_fleet_v1_dry_run_step_is_read_only_and_covers_every_account(self):
        workflow = self.documents["live-trial.yml"]
        job = next(iter(workflow["jobs"].values()))
        self.assertIn("fleet-dry-run", workflow["on"]["workflow_dispatch"]["inputs"]["step"]["options"])
        step = next(step for step in job["steps"] if step.get("if") == "inputs.step == 'fleet-dry-run'")
        self.assertIn("--fleet-dry-run", step["run"])
        self.assertNotIn("--fleet-execute", step["run"])
        self.assertIn("--identity-salt-env TACHIKOMA_FLEET_SALT", step["run"])
        self.assertEqual(step["env"]["TACHIKOMA_FLEET_SALT"], "${{ secrets.TACHIKOMA_FLEET_SALT }}")
        # The runner's own loader: a registry it would refuse (exit 2) fails here.
        registry = load_registry(ci.ROOT / job["env"]["FLEET_V1_REGISTRY"])
        self.assertEqual(registry.aliases(), tuple(f"pss{n}" for n in range(1, 6)))
        self.assertEqual(len(registry.enabled()), 5)
        for profile in registry.enabled():
            with self.subTest(alias=profile.alias):
                self.assertEqual(profile.policy_id, "fleet-v1")
                # Every kind is listed: a missing mineral or gas budget is uncapped, not zero.
                self.assertEqual(dict(profile.budgets), {"minerals": 0, "gas": 0, "starbux": 0})
                secret = profile.credentials_secret
                for suffix in ("_EMAIL", "_PASSWORD"):
                    self.assertEqual(step["env"][secret + suffix], f"${{{{ secrets.{secret}{suffix} }}}}")

    def test_upgrade_ship_step_runs_one_authorized_hull_upgrade_on_pss5_only(self):
        from sdk.fleet.authorization import load_authorization

        workflow = self.documents["live-trial.yml"]
        job = next(iter(workflow["jobs"].values()))
        self.assertIn("upgrade-ship", workflow["on"]["workflow_dispatch"]["inputs"]["step"]["options"])
        step = next(step for step in job["steps"] if step.get("if") == "inputs.step == 'upgrade-ship'")
        self.assertIn("--mode upgrade-ship --upgrade-ship", step["run"])
        self.assertNotIn("--fleet-dry-run", step["run"])
        self.assertEqual(set(step["env"]), {"PSS_ACCOUNT_5_EMAIL", "PSS_ACCOUNT_5_PASSWORD", "TACHIKOMA_FLEET_SALT"})
        # The runner's own loaders: files it would refuse (exit 2) fail here.
        registry = load_registry(ci.ROOT / job["env"]["G7_REGISTRY"])
        authorization = load_authorization(ci.ROOT / job["env"]["G7_AUTHORIZATION"])
        self.assertEqual([profile.alias for profile in registry.enabled()], ["pss5"])
        self.assertEqual((authorization.accounts, authorization.actions), (("pss5",), ("upgrade_ship",)))
        self.assertEqual(authorization.modes, ("upgrade-ship",))
        self.assertEqual(len(authorization.hull_upgrades["pss5"]), 1)  # the step reads the one entry
        self.assertEqual(authorization.cap("pss5", "gas"), 0)
        self.assertEqual(registry.get("pss5").budget("minerals"), authorization.cap("pss5", "minerals"))

    def test_only_release_publisher_has_write_permissions(self):
        for filename, document in self.documents.items():
            for name, job in document["jobs"].items():
                if job.get("permissions", {}).get("contents") == "write":
                    self.assertEqual((filename, name), ("release.yml", "publish"))
        publish = self.documents["release.yml"]["jobs"]["publish"]
        self.assertEqual(publish["needs"], "verify")
        self.assertFalse(any("checkout@" in step.get("uses", "") for step in publish["steps"]))
        command = publish["steps"][-1]["run"]
        self.assertIn("--draft", command)
        self.assertIn("--verify-tag", command)

    def test_manual_release_cannot_publish_or_bypass_artifact_verification(self):
        for mutation in (
            "publish_condition",
            "publish_dependency",
            "build_condition",
            "verify_dependency",
            "permissions",
        ):
            docs = copy.deepcopy(self.documents)
            jobs = docs["release.yml"]["jobs"]
            if mutation == "publish_condition":
                jobs["publish"].pop("if")
            elif mutation == "publish_dependency":
                jobs["publish"]["needs"] = "build"
            elif mutation == "build_condition":
                jobs["build"].pop("if")
            elif mutation == "verify_dependency":
                jobs["verify"]["needs"] = "checks"
            else:
                jobs["verify"]["permissions"] = {"contents": "write"}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                ci.check_workflows(docs)

    def test_manual_release_uses_only_a_local_synthetic_tag(self):
        release = self.documents["release.yml"]
        self.assertIn("workflow_dispatch", release["on"])
        steps = release["jobs"]["build"]["steps"]
        tag_step = next(step for step in steps if step.get("name") == "Create local verification tag")
        self.assertEqual(tag_step["if"], "${{ github.event_name == 'workflow_dispatch' }}")
        self.assertEqual(tag_step["run"], 'git tag 0.0.0 "$GITHUB_SHA"')
        build_step = next(
            step for step in steps if step.get("name") == "Validate tag and build reproducible source archive"
        )
        self.assertEqual(
            build_step["env"]["RELEASE_TAG"],
            "${{ github.event_name == 'workflow_dispatch' && '0.0.0' || github.ref_name }}",
        )

    def test_downloaded_manifest_must_match_workflow_identity(self):
        steps = self.documents["release.yml"]["jobs"]["verify"]["steps"]
        script = next(step["run"] for step in steps if step.get("name") == "Verify release identity")
        code = "\n".join(script.splitlines()[1:-1])
        for tag in ("0.0.0", "v1.2.3"):
            expected = {"tag": tag, "version": tag.removeprefix("v"), "commit": "abc123"}
            for field in (None, "tag", "version", "commit", "missing"):
                with self.subTest(tag=tag, field=field), TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / "dist").mkdir()
                    manifest = expected.copy()
                    if field == "missing":
                        manifest.pop("commit")
                    elif field:
                        manifest[field] = "wrong"
                    (root / "dist/release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                    result = subprocess.run(
                        [sys.executable, "-c", code],
                        cwd=root,
                        env={**os.environ, "GITHUB_SHA": "abc123", "RELEASE_TAG": tag},
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0 if field is None else 1, result.stderr)

    def test_validation_does_not_call_live_game_or_request_secrets(self):
        for filename in ("blocking-ci.yml", "automation-check.yml", "python-tests.yml", "postmerge-ci.yml"):
            text = json.dumps(self.documents[filename])
            for forbidden in ("secrets.", "secrets: inherit", "--run-battle", "pss_langgraph_automation"):
                self.assertNotIn(forbidden, text)


PSS5_WORKFLOWS = ("daily-run.yml", "live-trial.yml", "live-trial-schedule.yml")


class SerializedLegacyRunTests(unittest.TestCase):
    """S06-R6: an authorization that claims legacy_run_serialized is checked against the three pss5 workflows."""

    def setUp(self):
        self.documents = ci.workflow_documents(ci.ROOT)
        self.serialized = ci.serialized_authorizations(ci.ROOT)

    # Verifies: S06-R6
    def test_the_committed_files_claim_serialized_and_the_workflows_honour_it(self):
        self.assertEqual(
            self.serialized,
            [
                f"docs/experiments/live/authorization-{name}.json"
                for name in ("g2-pss5-canary", "g2-pss5-trial", "g6-pss5-place", "g7-pss5-ship")
            ],
        )
        self.assertIsNone(ci.check_serialized_legacy_run(self.documents, self.serialized))
        for filename in PSS5_WORKFLOWS:
            with self.subTest(filename=filename):
                self.assertEqual(
                    self.documents[filename]["concurrency"], {"group": "live-trial", "cancel-in-progress": "false"}
                )

    # Verifies: S06-R6
    def test_each_way_of_breaking_the_shared_group_fails_its_own_check(self):
        mutations = {
            "missing": ("must set a workflow-level concurrency group", lambda doc: doc.pop("concurrency")),
            "shorthand": (
                "must set a workflow-level concurrency group",
                lambda doc: doc.__setitem__("concurrency", "live-trial"),
            ),
            "renamed": (
                "must share one concurrency group",
                lambda doc: doc["concurrency"].__setitem__("group", "pss5-other"),
            ),
            "expression": (
                "fixed name, not an expression",
                lambda doc: doc["concurrency"].__setitem__("group", "${{ github.workflow }}"),
            ),
            "cancel_true": (
                "cancel-in-progress: false",
                lambda doc: doc["concurrency"].__setitem__("cancel-in-progress", "true"),
            ),
            "cancel_missing": ("cancel-in-progress: false", lambda doc: doc["concurrency"].pop("cancel-in-progress")),
            "job_level": ("must set a workflow-level concurrency group", self.move_to_job),
            "job_level_too": ("only at the workflow level", self.copy_to_job),
        }
        for filename in PSS5_WORKFLOWS:
            for mutation, (message, apply) in mutations.items():
                docs = copy.deepcopy(self.documents)
                apply(docs[filename])
                with self.subTest(filename=filename, mutation=mutation):
                    with self.assertRaisesRegex(ValueError, re.escape(message)):
                        ci.check_serialized_legacy_run(docs, self.serialized)
                    # The general workflow policy alone would not have caught it.
                    if mutation not in ("job_level", "job_level_too"):
                        self.assertIsNone(ci.check_workflows(docs))

    @staticmethod
    def move_to_job(document):
        next(iter(document["jobs"].values()))["concurrency"] = document.pop("concurrency")

    @staticmethod
    def copy_to_job(document):
        next(iter(document["jobs"].values()))["concurrency"] = dict(document["concurrency"])

    # Verifies: S06-R6
    def test_the_rule_applies_only_while_a_file_claims_serialized(self):
        docs = copy.deepcopy(self.documents)
        for filename in PSS5_WORKFLOWS:
            docs[filename].pop("concurrency")
        self.assertIsNone(ci.check_serialized_legacy_run(docs, []))
        with self.assertRaisesRegex(ValueError, "authorization-x.json"):
            ci.check_serialized_legacy_run(docs, ["docs/experiments/live/authorization-x.json"])

    # Verifies: S06-R6
    def test_serialized_authorizations_finds_every_claim_under_the_live_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(ci.serialized_authorizations(root), [])
            live = root / "docs/experiments/live"
            (live / "nested").mkdir(parents=True)
            files = {
                "authorization-a.json": {"legacy_run_serialized": [5]},
                "authorization-b.json": {"legacy_run_skipped": [5]},
                "nested/authorization-c.json": {"legacy_run_serialized": []},
                "registry-d.json": {"version": 1, "accounts": []},
                "list.json": [{"legacy_run_serialized": [5]}],
            }
            for name, payload in files.items():
                (live / name).write_text(json.dumps(payload), encoding="utf-8")
            (root / "docs/experiments/authorization-outside.json").write_text(
                json.dumps({"legacy_run_serialized": [5]}), encoding="utf-8"
            )
            self.assertEqual(
                ci.serialized_authorizations(root),
                [
                    "docs/experiments/live/authorization-a.json",
                    "docs/experiments/live/nested/authorization-c.json",
                ],
            )
            (live / "broken.json").write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "broken.json"):
                ci.serialized_authorizations(root)

    # Verifies: S06-R6
    def test_the_repository_check_runs_the_serialized_rule(self):
        docs = copy.deepcopy(self.documents)
        docs["daily-run.yml"].pop("concurrency")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs/experiments/live").mkdir(parents=True)
            (root / "docs/experiments/live/authorization-a.json").write_text(
                json.dumps({"legacy_run_serialized": [5]}), encoding="utf-8"
            )
            with (
                patch.object(ci, "workflow_documents", return_value=docs),
                patch.object(ci, "check_workflows"),
                patch.object(ci, "check_mutation_policy", side_effect=AssertionError("ran past the rule")),
            ):
                with self.assertRaisesRegex(ValueError, "daily-run.yml must set a workflow-level concurrency group"):
                    ci.check_repository(root)


TRIAL_STEP_NAME = "Decide today's step"


def trial_days(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


@skip_unless_dependency(shutil.which("bash"), "the trial's day decision is a bash step")
class TrialScheduleDecisionTests(unittest.TestCase):
    """live-trial-schedule.yml's "Decide today's step", run as written with a stubbed `date`."""

    def setUp(self):
        self.workflow = ci.workflow_documents(ci.ROOT)["live-trial-schedule.yml"]
        job = self.workflow["jobs"]["trial"]
        self.script = next(step for step in job["steps"] if step.get("name") == TRIAL_STEP_NAME)["run"]
        self.stop_file = job["env"]["STOP_FILE"]
        self.crons = [entry["cron"] for entry in self.workflow["on"]["schedule"]]
        self.schedule = json.loads(
            (ci.ROOT / "docs/experiments/r2-progression-trial/schedule.json").read_text(encoding="utf-8")
        )

    def decide(self, today, *, schedule=None, stop=False):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "bin/date"
            fake.parent.mkdir()
            fake.write_text('#!/bin/sh\n[ "$*" = "-u +%F" ] || exit 9\necho "$FAKE_TODAY"\n', encoding="utf-8")
            fake.chmod(0o755)
            if stop:
                (root / self.stop_file).parent.mkdir(parents=True)
                (root / self.stop_file).write_text("", encoding="utf-8")
            output = root / "github-output"
            result = subprocess.run(
                ["bash", "-c", self.script],
                cwd=root,
                capture_output=True,
                text=True,
                env={
                    "PATH": f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}",
                    "FAKE_TODAY": str(today),
                    "SCHEDULE": self.crons[0] if schedule is None else schedule,
                    "STOP_FILE": self.stop_file,
                    "GITHUB_OUTPUT": str(output),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            pairs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
            return pairs["snapshot"], pairs["execute"]

    def test_one_cron_in_a_gap_of_the_daily_run(self):
        self.assertEqual(self.crons, ["30 4 * * *"])
        daily = ci.workflow_documents(ci.ROOT)["daily-run.yml"]["on"]["schedule"]
        self.assertEqual([entry["cron"] for entry in daily], ["0 0 * * *", "0 8 * * *", "0 16 * * *"])

    def test_every_date_boundary(self):
        cases = {
            "2026-11-16": ("no", "no"),  # the day before the first run that closes a trial day
            "2026-11-17": ("yes", "no"),  # closes trial day 2026-11-16, the first
            "2026-11-29": ("yes", "no"),  # closes 2026-11-28; 2026-11-29 is still manual
            "2026-11-30": ("yes", "yes"),  # closes manual 2026-11-29, opens automated 2026-11-30
            "2026-12-06": ("yes", "yes"),
            "2026-12-13": ("yes", "yes"),  # the last automated day
            "2026-12-14": ("yes", "no"),  # closes automated 2026-12-13; 2026-12-14 is manual
            "2026-12-21": ("yes", "no"),  # closes trial day 2026-12-20, the last
            "2026-12-22": ("no", "no"),  # the day after
        }
        for today, expected in cases.items():
            with self.subTest(today=today):
                self.assertEqual(self.decide(today), expected)

    def test_a_stop_file_stops_only_the_execute_run(self):
        self.assertEqual(self.decide("2026-12-01", stop=True), ("yes", "stopped"))
        self.assertEqual(self.decide("2026-11-25", stop=True), ("yes", "no"))
        self.assertEqual(self.decide("2026-12-21", stop=True), ("yes", "no"))

    def test_any_other_trigger_does_nothing(self):
        for schedule in ("", "30 23 * * *", "30 0 * * *", "0 4 * * *"):
            with self.subTest(schedule=schedule):
                self.assertEqual(self.decide("2026-12-01", schedule=schedule), ("no", "no"))

    def test_the_runs_cover_exactly_the_approved_trial_days(self):
        blocks = self.schedule["blocks"]
        days = trial_days(blocks[0]["start"], blocks[-1]["end"])
        automated = [
            day for block in blocks if block["arm"] == "baseline-v1" for day in trial_days(block["start"], block["end"])
        ]
        closed, opened = [], []
        for today in trial_days("2026-11-10", "2026-12-31"):
            snapshot, execute = self.decide(today)
            if snapshot == "yes":
                closed.append(today - timedelta(days=1))  # a run's snapshot closes the previous trial day
            if execute == "yes":
                opened.append(today)
        self.assertEqual(closed, days)
        self.assertEqual(opened, automated)

    def test_the_snapshot_comes_first_and_each_kind_keeps_its_artifact(self):
        steps = self.workflow["jobs"]["trial"]["steps"]
        names = [step.get("name", "") for step in steps]
        snapshot = names.index("Read-only snapshot of pss5")
        execute = names.index("Automated day (baseline-v1, full mode)")
        self.assertLess(names.index(TRIAL_STEP_NAME), snapshot)
        self.assertLess(snapshot, execute)
        self.assertEqual(steps[snapshot]["if"], "steps.day.outputs.snapshot == 'yes'")
        # The execute run does not depend on the snapshot succeeding, as when they were separate runs.
        self.assertEqual(steps[execute]["if"], "${{ !cancelled() && steps.day.outputs.execute == 'yes' }}")
        self.assertIn("--baseline", steps[snapshot]["run"])
        self.assertNotIn("--fleet-execute", steps[snapshot]["run"])
        run = steps[execute]["run"]
        self.assertLess(run.index("--fleet-dry-run"), run.index("--fleet-execute"))
        uploads = {
            step["with"]["name"]: step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")
        }
        self.assertEqual(set(uploads), {"trial-snapshot-${{ github.run_id }}", "trial-execute-${{ github.run_id }}"})
        for kind, root in (("snapshot", "OUT"), ("execute", "EXECUTE_OUT")):
            upload = uploads[f"trial-{kind}-${{{{ github.run_id }}}}"]
            with self.subTest(kind=kind):
                self.assertEqual(upload["if"], f"always() && steps.day.outputs.{kind} == 'yes'")
                self.assertEqual(upload["with"]["path"], f"${{{{ env.{root} }}}}")
                self.assertEqual(upload["with"]["retention-days"], "90")
                self.assertEqual(upload["with"]["if-no-files-found"], "ignore")
        self.assertIn('"$OUT/baselines"', steps[snapshot]["run"])
        self.assertIn('"$EXECUTE_OUT/journals"', run)
        self.assertNotIn("$OUT/", run)

    def test_every_execute_run_falls_inside_the_trial_authorization(self):
        from sdk.fleet.authorization import load_authorization

        authorization = load_authorization(ci.ROOT / self.workflow["jobs"]["trial"]["env"]["TRIAL_AUTHORIZATION"])
        minute, hour = (int(field) for field in self.crons[0].split()[:2])
        for day in trial_days("2026-11-30", "2026-12-13"):
            start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)
            with self.subTest(day=day):
                self.assertEqual(self.decide(day)[1], "yes")
                self.assertLessEqual(authorization.not_before, start)
                # GitHub starts scheduled runs late, so leave room for a long delay.
                self.assertLess(start + timedelta(hours=12), authorization.expires)


@skip_unless_dependency(shutil.which("git"), "release provenance tests require Git")
class SourceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.output = Path(self.temporary.name) / "dist"
        ci.git(self.root, "init", "-b", "main")
        ci.git(self.root, "config", "user.name", "CI Test")
        ci.git(self.root, "config", "user.email", "ci@example.invalid")
        ci.git(self.root, "config", "commit.gpgsign", "false")
        ci.git(self.root, "config", "core.autocrlf", "false")
        ci.git(self.root, "config", "core.hooksPath", str(self.root / "no-hooks"))
        files = {
            "run.py": "print('synthetic application')\n",
            "requirements.txt": "requests==2.28.2\n",
            "LICENSE": "Synthetic fixture license\n",
            "README.md": "Synthetic fixture readme\n",
            "sdk/client.py": "class Client: pass\n",
            "sdk/inventory/example.py": "VALUE = 1\n",
            "sdk/.device": "synthetic private device fixture\n",
            "run_log.txt": "synthetic private log fixture\n",
        }
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        self.commit("initial fixture")
        ci.git(self.root, "tag", "1.2.3")

    def commit(self, message):
        ci.git(self.root, "add", ".")
        ci.git(self.root, "commit", "-m", message)

    def build(self, tag="1.2.3"):
        return ci.build_release(self.root, tag, self.output, "main")

    def test_release_is_reproducible_and_checksums_cover_every_artifact(self):
        manifest = self.build()
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertEqual(manifest["commit"], ci.git(self.root, "rev-parse", "HEAD").decode().strip())
        original = {path.name: path.read_bytes() for path in self.output.iterdir()}
        self.build()
        self.assertEqual(original, {path.name: path.read_bytes() for path in self.output.iterdir()})
        for line in (self.output / "SHA256SUMS").read_text().splitlines():
            digest, filename = line.split("  ")
            self.assertEqual(hashlib.sha256((self.output / filename).read_bytes()).hexdigest(), digest)

    def test_archive_excludes_logs_devices_untracked_and_dirty_contents(self):
        (self.root / "run.py").write_text("DIRTY_SYNTHETIC_SENTINEL\n", encoding="utf-8")
        (self.root / "passwords.txt").write_text("UNTRACKED_SYNTHETIC_SENTINEL\n", encoding="utf-8")
        self.build()
        with tarfile.open(self.output / "tachikoma-1.2.3.tar.gz") as archive:
            names = archive.getnames()
            self.assertEqual(names, sorted(names))
            self.assertIn("tachikoma-1.2.3/sdk/inventory/example.py", names)
            self.assertEqual(len(names), 6)
            contents = b"".join(archive.extractfile(name).read() for name in names)
            self.assertNotIn(b"SYNTHETIC_SENTINEL", contents)
            self.assertFalse(any(".device" in name or "log" in name or "password" in name for name in names))

    def test_prefixed_and_annotated_tags_are_supported(self):
        ci.git(self.root, "tag", "-a", "v1.2.3", "-m", "synthetic annotated release")
        self.assertEqual(self.build("v1.2.3")["version"], "1.2.3")

    def test_malformed_or_option_like_tags_are_rejected_before_git(self):
        for tag in ("1.2", "v1.2.3-rc1", "01.2.3", "v1.2.3;echo injected", "--help", "1.2.3\n"):
            with self.subTest(tag=tag), patch.object(ci, "git") as mocked:
                with self.assertRaises(ValueError):
                    self.build(tag)
                mocked.assert_not_called()

    def test_tag_must_match_checked_out_commit(self):
        (self.root / "run.py").write_text("print('new commit')\n", encoding="utf-8")
        self.commit("next fixture")
        with self.assertRaisesRegex(ValueError, "checked-out commit"):
            self.build()

    def test_release_must_already_be_on_main(self):
        ci.git(self.root, "switch", "-c", "candidate")
        (self.root / "run.py").write_text("print('unmerged')\n", encoding="utf-8")
        self.commit("unmerged fixture")
        ci.git(self.root, "tag", "1.2.4")
        with self.assertRaises(subprocess.CalledProcessError):
            self.build("1.2.4")

    def test_source_symlink_is_rejected_without_dereferencing_it(self):
        object_id = ci.git(self.root, "hash-object", "run.py").decode().strip()
        ci.git(self.root, "update-index", "--add", "--cacheinfo", f"120000,{object_id},sdk/linked.py")
        ci.git(self.root, "commit", "-m", "synthetic symlink fixture")
        ci.git(self.root, "tag", "1.2.4")
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.build("1.2.4")

    def test_missing_required_application_source_refuses_release(self):
        ci.git(self.root, "rm", "sdk/client.py")
        ci.git(self.root, "commit", "-m", "remove fixture client")
        ci.git(self.root, "tag", "1.2.4")
        with self.assertRaisesRegex(ValueError, "missing required"):
            self.build("1.2.4")

    def test_blob_policy_rejects_oversized_tracked_file(self):
        (self.root / "large.bin").write_bytes(b"0" * (ci.MAX_BLOB_BYTES + 1))
        ci.git(self.root, "add", "large.bin")
        with (
            patch.object(ci, "workflow_documents", return_value={}),
            patch.object(ci, "check_workflows"),
            patch.object(ci, "check_mutation_policy"),
        ):
            with self.assertRaisesRegex(ValueError, "blob policy"):
                ci.check_repository(self.root)


@skip_unless_dependency(shutil.which("git"), "release provenance tests require Git")
class ReleasePackageDataTests(unittest.TestCase):
    """Package data under sdk/ ships in the archive; tests, captures and secrets do not."""

    LOADER = (
        "import json\n"
        "from pathlib import Path\n"
        "def load(name):\n"
        "    path = Path(__file__).resolve().parent / 'scenarios' / f'{name}.json'\n"
        "    return json.loads(path.read_text(encoding='utf-8'))\n"
    )

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        ci.git(self.root, "init", "-b", "main")
        ci.git(self.root, "config", "user.name", "CI Test")
        ci.git(self.root, "config", "user.email", "ci@example.invalid")
        ci.git(self.root, "config", "commit.gpgsign", "false")
        ci.git(self.root, "config", "core.autocrlf", "false")
        ci.git(self.root, "config", "core.hooksPath", str(self.root / "no-hooks"))
        self.write(
            {
                "run.py": "print('synthetic application')\n",
                "requirements.txt": "requests==2.28.2\n",
                "LICENSE": "Synthetic fixture license\n",
                "README.md": "Synthetic fixture readme\n",
                "sdk/__init__.py": "",
                "sdk/client.py": "class Client: pass\n",
                "sdk/simulation/__init__.py": "",
                "sdk/simulation/loader.py": self.LOADER,
                "sdk/simulation/scenarios/alpha.json": '{"name": "alpha", "seed": 7}\n',
                "sdk/simulation/scenarios/beta.json": '{"name": "beta", "seed": 11}\n',
                "sdk/notes.txt": "PRIVATE_SYNTHETIC_SENTINEL notes\n",
                "sdk/data.bin": "PRIVATE_SYNTHETIC_SENTINEL binary\n",
                "sdk/tests/fixture.json": '{"sentinel": "PRIVATE_SYNTHETIC_SENTINEL"}\n',
                "sdk/tests/test_client.py": "PRIVATE_SYNTHETIC_SENTINEL = 1\n",
                "tests/test_app.py": "PRIVATE_SYNTHETIC_SENTINEL = 1\n",
                "tests/fixture.json": '{"sentinel": "PRIVATE_SYNTHETIC_SENTINEL"}\n',
                "docs/scenario.json": '{"sentinel": "PRIVATE_SYNTHETIC_SENTINEL"}\n',
                "scripts/tool.py": "PRIVATE_SYNTHETIC_SENTINEL = 1\n",
                "sdk/.device": "PRIVATE_SYNTHETIC_SENTINEL device\n",
                "captures/session.json": '{"sentinel": "PRIVATE_SYNTHETIC_SENTINEL"}\n',
            }
        )
        self.release("1.2.3")

    def write(self, files):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")

    def release(self, tag):
        ci.git(self.root, "add", ".")
        ci.git(self.root, "commit", "-m", f"fixture {tag}")
        ci.git(self.root, "tag", tag)

    def build(self, tag="1.2.3", output="dist"):
        return ci.build_release(self.root, tag, Path(self.temporary.name) / output, "main")

    def archive_path(self, tag="1.2.3", output="dist"):
        return Path(self.temporary.name) / output / f"tachikoma-{tag}.tar.gz"

    def test_sdk_json_ships_and_excluded_kinds_stay_out(self):
        manifest = self.build()
        with tarfile.open(self.archive_path()) as archive:
            names = archive.getnames()
            contents = b"".join(archive.extractfile(name).read() for name in names)
        prefix = "tachikoma-1.2.3/"
        self.assertEqual(
            names,
            [
                prefix + "LICENSE",
                prefix + "README.md",
                prefix + "requirements.txt",
                prefix + "run.py",
                prefix + "sdk/__init__.py",
                prefix + "sdk/client.py",
                prefix + "sdk/simulation/__init__.py",
                prefix + "sdk/simulation/loader.py",
                prefix + "sdk/simulation/scenarios/alpha.json",
                prefix + "sdk/simulation/scenarios/beta.json",
            ],
        )
        self.assertEqual(manifest["files"], len(names))
        self.assertNotIn(b"PRIVATE_SYNTHETIC_SENTINEL", contents)

    def test_archive_with_package_data_is_byte_reproducible(self):
        self.build(output="first")
        self.build(output="second")
        first, second = Path(self.temporary.name) / "first", Path(self.temporary.name) / "second"
        for name in ("tachikoma-1.2.3.tar.gz", "release-manifest.json", "SHA256SUMS"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)
        raw = self.archive_path(output="first").read_bytes()
        self.assertEqual(raw[4:8], b"\0\0\0\0", "gzip header must not carry a timestamp")
        epoch = int(ci.git(self.root, "show", "-s", "--format=%ct", "HEAD"))
        with tarfile.open(self.archive_path(output="first")) as archive:
            for info in archive.getmembers():
                with self.subTest(member=info.name):
                    self.assertTrue(info.isfile())
                    self.assertEqual((info.mtime, info.uid, info.gid, info.uname, info.gname), (epoch, 0, 0, "", ""))
                    self.assertEqual(info.mode, 0o644)

    def test_scenario_loads_from_the_extracted_archive(self):
        self.build()
        extracted = Path(self.temporary.name) / "extracted"
        with tarfile.open(self.archive_path()) as archive:
            if hasattr(tarfile, "data_filter"):
                archive.extractall(extracted, filter="data")
            else:  # Python builds without extraction filters; members are regular files only.
                archive.extractall(extracted)
        program = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from sdk.simulation.loader import load\n"
            "print(load('alpha')['seed'], load('beta')['seed'])\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", program, str(extracted / "tachikoma-1.2.3")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "7 11")

    def test_private_looking_package_data_refuses_the_release(self):
        names = (
            "sdk/secrets.json",
            "sdk/config/auth.json",
            "sdk/captures/alpha.json",
            "sdk/simulation/run_log.json",
            "sdk/device-token.json",
            "sdk/.profiles/alpha.json",
            "sdk/Session.json",
        )
        for index, name in enumerate(names, start=4):
            with self.subTest(name=name):
                tag = f"1.2.{index}"
                self.write({name: '{"value": "synthetic"}\n'})
                self.release(tag)
                with self.assertRaisesRegex(ValueError, "looks like a secret, log or capture"):
                    self.build(tag, output=f"refused-{index}")
                self.assertFalse((Path(self.temporary.name) / f"refused-{index}").exists())
                ci.git(self.root, "rm", "-q", name)
                ci.git(self.root, "commit", "-q", "-m", f"remove {name}")

    def test_invalid_json_package_data_refuses_the_release(self):
        self.write({"sdk/simulation/scenarios/broken.json": "{not json\n"})
        self.release("1.2.4")
        with self.assertRaisesRegex(ValueError, "not valid UTF-8 JSON"):
            self.build("1.2.4", output="broken")
        self.assertFalse((Path(self.temporary.name) / "broken").exists())

    def test_member_selection_allow_list(self):
        for path, expected in (
            ("run.py", True),
            ("sdk/client.py", True),
            ("sdk/device.py", True),
            ("sdk/simulation/scenarios/two-accounts.json", True),
            ("sdk/actions/catalog.json", True),
            ("sdk/notes.md", False),
            ("sdk/image.png", False),
            ("sdk/.device", False),
            ("sdk/tests/fixture.json", False),
            ("sdk/tests/test_client.py", False),
            ("sdk/__pycache__/client.cpython-313.pyc", False),
            ("tests/test_ci.py", False),
            ("docs/RELEASE.md", False),
            ("captures/session.json", False),
            ("scripts/ci.py", False),
        ):
            with self.subTest(path=path):
                self.assertIs(ci.release_includes(path), expected)


# Without a .git checkout this test does not apply, so it skips even under TACHIKOMA_STRICT_DEPS=1.
@skip_unless_dependency(
    shutil.which("git"), "requires the tachikoma Git checkout", applicable=(ci.ROOT / ".git").exists()
)
class RepositoryReleaseSelectionTests(unittest.TestCase):
    def test_tracked_scenarios_are_selected_and_no_tracked_sdk_file_is_refused(self):
        tracked = [os.fsdecode(name) for name in ci.git(ci.ROOT, "ls-files", "-z").split(b"\0") if name]
        selected = {path for path in tracked if ci.release_includes(path)}
        scenarios = {path for path in tracked if PurePosixPath(path).match("sdk/simulation/scenarios/*.json")}
        self.assertTrue(scenarios, "the bundled simulation scenarios must be tracked")
        self.assertLessEqual(scenarios, selected)
        self.assertIn("sdk/simulation/game.py", selected)
        self.assertFalse({path for path in selected if path.startswith(("tests/", "docs/", "automation/"))})
        for path in sorted(path for path in selected if path.startswith("sdk/") and path.endswith(".json")):
            with self.subTest(path=path):
                json.loads((ci.ROOT / path).read_text(encoding="utf-8"))


SENTINEL_PROBE = """
import os
import unittest

import run
from sdk.device import Device


class SentinelProbe(unittest.TestCase):
    def test_writes_device_and_log_in_the_working_directory(self):
        Device(language="en")
        run.logging.getLogger().info("sentinel probe")
        self.assertTrue(os.path.isfile(".device"))
        self.assertTrue(os.path.isfile("tachikoma.log"))
"""
SENTINEL_MTIME_NS = 1_600_000_000_000_000_000
# The Makefile recipes use mktemp, trap and a POSIX shell; Windows runs only scripts/ci.py test.
REQUIRES_POSIX_MAKE = unittest.skipUnless(
    os.name == "posix" and shutil.which("make"), "requires make and a POSIX shell"
)


class CredentialFileSafetyTests(unittest.TestCase):
    """Runs the test entry points in a scratch copy of the repository whose root holds sentinel credential files."""

    def setUp(self):
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve() / "repo"
        self.temp = Path(directory.name).resolve() / "tmp"
        self.temp.mkdir()
        shutil.copytree(ci.ROOT / "sdk", self.root / "sdk", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "scripts").mkdir()
        (self.root / "tests").mkdir()
        for name in ("run.py", "Makefile", "scripts/ci.py", "tests/__init__.py"):
            shutil.copy2(ci.ROOT / name, self.root / name)
        # The probe writes ./.device and ./tachikoma.log in its working directory, as the real device and
        # run.py tests do. It stands in for tests/test_security.py, so both `make test` (every test_*.py) and
        # `make test-security` (--names tests.test_security) run it.
        (self.root / "tests" / "test_security.py").write_text(SENTINEL_PROBE, encoding="utf-8")
        self.sentinels = {
            ".device": f"Android|{SYNTHETIC_DEVICE_KEY_IOS}|sentinel-refresh|en|{SYNTHETIC_ACCESS_TOKEN}|1".encode(),
            "tachikoma.log": b"sentinel log line\n",
        }
        for name, content in self.sentinels.items():
            path = self.root / name
            path.write_bytes(content)
            os.utime(path, ns=(SENTINEL_MTIME_NS, SENTINEL_MTIME_NS))
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONSAFEPATH", "MAKEFLAGS", "MFLAGS", "MAKELEVEL"}
        }
        # Every temporary directory the commands create lands here, so the test can see that it was removed.
        self.env.update(PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(self.temp), TEMP=str(self.temp), TMP=str(self.temp))

    def run_in_root(self, command):
        return subprocess.run(command, cwd=self.root, env=self.env, capture_output=True, text=True, timeout=120)

    def assert_sentinels_untouched(self):
        for name, content in self.sentinels.items():
            path = self.root / name
            self.assertEqual(path.read_bytes(), content, name)
            self.assertEqual(path.stat().st_mtime_ns, SENTINEL_MTIME_NS, name)

    def assert_probe_ran_and_temp_dir_removed(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 1 test", result.stderr)
        self.assertEqual(list(self.temp.iterdir()), [], "the temporary working directory must be removed")

    # Verifies: S02-R1, S02-R2
    def test_ci_test_command_leaves_root_credential_files_untouched(self):
        for names in ([], ["--names", "tests.test_security"]):
            with self.subTest(names=names):
                result = self.run_in_root([sys.executable, "scripts/ci.py", "test", *names])
                self.assert_probe_ran_and_temp_dir_removed(result)
                self.assert_sentinels_untouched()

    def test_verbose_prints_every_test_id_and_the_default_does_not(self):
        probe_id = "(tests.test_security.SentinelProbe"
        for verbose, expected in ((["--verbose"], True), ([], False)):
            with self.subTest(verbose=verbose):
                names = ["--names", "tests.test_security"]
                result = self.run_in_root([sys.executable, "scripts/ci.py", "test", *verbose, *names])
                self.assert_probe_ran_and_temp_dir_removed(result)
                self.assertEqual(probe_id in result.stderr, expected, result.stderr)
                self.assertEqual("test_writes_device_and_log_in_the_working_directory" in result.stderr, expected)
                self.assert_sentinels_untouched()

    # Verifies: S02-R1
    def test_run_tests_uses_a_fresh_temporary_directory_and_restores_the_working_directory(self):
        program = """
import os
import unittest
from unittest.mock import patch
from scripts import ci

seen = []

class RecordWorkingDirectory(unittest.TestCase):
    def runTest(self):
        seen.append(os.getcwd())
        self.assertEqual(os.listdir("."), [])

with patch.object(unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite([RecordWorkingDirectory()])):
    code = ci.run_tests([])
print(repr((code, seen[0], os.getcwd(), os.path.exists(seen[0]))))
"""
        result = self.run_in_root([sys.executable, "-c", program])
        self.assertEqual(result.returncode, 0, result.stderr)
        code, seen, after, still_exists = ast.literal_eval(result.stdout.strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertEqual(Path(after).resolve(), self.root)
        self.assertEqual(Path(seen).resolve().parent, self.temp)
        self.assertFalse(still_exists)

    # Verifies: S02-R1
    def test_log_files_opened_in_the_temporary_directory_are_closed_so_it_can_be_removed(self):
        inside = logging.FileHandler(self.temp / "tachikoma.log", delay=False)
        outside = logging.FileHandler(self.root / "outside.log", delay=False)
        for handler in (inside, outside):
            logging.root.addHandler(handler)
        self.addCleanup(outside.close)
        self.addCleanup(logging.root.removeHandler, outside)
        self.addCleanup(logging.root.removeHandler, inside)
        ci.close_root_log_files_under(self.temp)
        self.assertNotIn(inside, logging.root.handlers)
        self.assertIsNone(inside.stream)
        self.assertIn(outside, logging.root.handlers)
        self.assertIsNotNone(outside.stream)

    # Verifies: S02-R1, S02-R2
    @REQUIRES_POSIX_MAKE
    def test_make_test_targets_leave_root_credential_files_untouched(self):
        for target in ("test", "test-security"):
            with self.subTest(target=target):
                result = self.run_in_root(["make", target, f"PYTHON={sys.executable}"])
                self.assert_probe_ran_and_temp_dir_removed(result)
                self.assert_sentinels_untouched()

    # Verifies: S02-R1
    @REQUIRES_POSIX_MAKE
    def test_make_test_recipes_use_an_absolute_interpreter_and_a_temporary_directory(self):
        venv_python = self.root / ".venv" / "bin" / "python"
        cases = [((f"PYTHON={sys.executable}",), sys.executable), (("PYTHON=.venv/bin/python",), str(venv_python))]
        if shutil.which("python3", path=self.env.get("PATH")):
            cases.append(((), None))
        for arguments, expected in cases:
            for target in ("test", "test-security"):
                with self.subTest(arguments=arguments, target=target):
                    result = self.run_in_root(["make", "-n", target, *arguments])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    recipe = result.stdout
                    self.assertIn("mktemp -d", recipe)
                    self.assertIn("trap ", recipe)
                    self.assertIn(f'"{self.root / "scripts" / "ci.py"}" test', recipe)
                    interpreter = recipe.split('cd "$tmp" && PYTHONDONTWRITEBYTECODE=1 "', 1)[1].split('"', 1)[0]
                    self.assertTrue(os.path.isabs(interpreter), interpreter)
                    if expected is not None:
                        self.assertEqual(interpreter, expected)
        # With no PYTHON given, an executable .venv interpreter is preferred, by its absolute path.
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        venv_python.chmod(0o755)
        result = self.run_in_root(["make", "-n", "test"])
        self.assertIn(
            f'cd "$tmp" && PYTHONDONTWRITEBYTECODE=1 "{venv_python}" "{self.root / "scripts" / "ci.py"}" test',
            result.stdout,
        )

    # Verifies: S02-R3
    def test_repository_root_unittest_run_is_refused_while_device_exists(self):
        result = self.run_in_root([sys.executable, "-m", "unittest", "tests.test_security"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("python scripts/ci.py test --names tests.<module>", result.stderr)
        self.assertNotIn("Ran ", result.stderr)
        self.assert_sentinels_untouched()

    # Verifies: S02-R3
    def test_guard_allows_a_repository_root_run_without_device(self):
        (self.root / ".device").unlink()
        result = self.run_in_root([sys.executable, "-m", "unittest", "tests.test_security"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)


RECORD_WORKING_DIRECTORY = """
import os
import unittest
from unittest.mock import patch
from scripts import ci

seen = []

class RecordWorkingDirectory(unittest.TestCase):
    def runTest(self):
        seen.append(os.getcwd())

with patch.object(unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite([RecordWorkingDirectory()])):
    code = ci.run_tests([])
print(repr((code, seen[0], os.getcwd(), os.path.isdir(seen[0]))))
"""


class TestRunnerPortabilityTests(unittest.TestCase):
    """scripts/ci.py must work from a Bazel runfiles tree and per-test $TEST_TMPDIR as well as from a checkout."""

    def setUp(self):
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.work = Path(os.path.abspath(directory.name)) / "work"
        self.temp = Path(os.path.abspath(directory.name)) / "tmp"
        self.work.mkdir()
        self.temp.mkdir()

    def run_child(self, command, **extra):
        env = child_env(PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(self.temp), TEMP=str(self.temp), TMP=str(self.temp))
        env.pop("TEST_TMPDIR", None)
        env.update(extra)
        return subprocess.run(command, cwd=self.work, env=env, capture_output=True, text=True, timeout=120)

    def test_root_is_derived_with_abspath_without_resolving_symlinks(self):
        self.assertEqual(ci.ROOT, Path(os.path.abspath(ci.__file__)).parent.parent)
        # Evaluate the module under a fictitious path with resolve() disabled: a runfiles symlink must not be followed.
        fake = str(self.work / "runfiles" / "_main" / "scripts" / "ci.py")
        source = (ci.ROOT / "scripts" / "ci.py").read_text(encoding="utf-8")
        namespace = {"__file__": fake, "__name__": "ci_root_probe"}
        with patch.object(Path, "resolve", side_effect=AssertionError("ROOT must not call resolve()")):
            exec(compile(source, fake, "exec"), namespace)
        self.assertEqual(namespace["ROOT"], self.work / "runfiles" / "_main")

    def test_require_tests_fails_on_an_empty_suite_and_the_default_does_not(self):
        program = """
import unittest
from unittest.mock import patch
from scripts import ci

with patch.object(unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite()):
    print(repr((ci.run_tests([]), ci.run_tests([], require_tests=True))))
"""
        result = self.run_child([sys.executable, "-c", program])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ast.literal_eval(result.stdout.strip().splitlines()[-1]), (0, 1))
        self.assertIn("CI ran no tests, but at least one was required.", result.stderr)

    def test_child_with_pythonsafepath_still_imports_scripts_ci(self):
        program = "from scripts import ci; print(ci.ROOT)"
        result = self.run_child([sys.executable, "-c", program], PYTHONSAFEPATH="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(ci.ROOT))
        if sys.version_info >= (3, 11):
            # Without PYTHONPATH the checkout root is not importable even from the checkout itself.
            env = child_env(PYTHONSAFEPATH="1")
            del env["PYTHONPATH"]
            bare = subprocess.run(
                [sys.executable, "-c", program], cwd=ci.ROOT, env=env, capture_output=True, text=True, timeout=30
            )
            self.assertNotEqual(bare.returncode, 0)
            self.assertIn("ModuleNotFoundError", bare.stderr)

    def test_run_tests_stays_in_test_tmpdir_and_otherwise_uses_a_fresh_directory(self):
        result = self.run_child([sys.executable, "-c", RECORD_WORKING_DIRECTORY], TEST_TMPDIR=str(self.work))
        self.assertEqual(result.returncode, 0, result.stderr)
        code, seen, after, still_exists = ast.literal_eval(result.stdout.strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.samefile(seen, self.work))
        self.assertTrue(os.path.samefile(after, self.work))
        self.assertTrue(still_exists)
        # A $TEST_TMPDIR that is not the working directory changes nothing: a fresh directory is still used.
        elsewhere = self.temp / "elsewhere"
        elsewhere.mkdir()
        result = self.run_child([sys.executable, "-c", RECORD_WORKING_DIRECTORY], TEST_TMPDIR=str(elsewhere))
        self.assertEqual(result.returncode, 0, result.stderr)
        code, seen, after, still_exists = ast.literal_eval(result.stdout.strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertEqual(Path(seen).resolve().parent, self.temp.resolve())
        self.assertTrue(os.path.samefile(after, self.work))
        self.assertFalse(still_exists)

    def test_device_test_module_leaves_a_sentinel_device_in_the_starting_directory_untouched(self):
        sentinel = self.work / ".device"
        content = f"Android|{SYNTHETIC_DEVICE_KEY_IOS}|sentinel-refresh|en|{SYNTHETIC_ACCESS_TOKEN}|1".encode()
        sentinel.write_bytes(content)
        os.utime(sentinel, ns=(SENTINEL_MTIME_NS, SENTINEL_MTIME_NS))
        # tests.test_battle_flow constructs Device(), which reads and rewrites ./.device.
        command = [sys.executable, str(ci.ROOT / "scripts" / "ci.py"), "test", "--names", "tests.test_battle_flow"]
        result = self.run_child(command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"Ran [1-9][0-9]* tests?")
        self.assertEqual(sentinel.read_bytes(), content)
        self.assertEqual(sentinel.stat().st_mtime_ns, SENTINEL_MTIME_NS)
        self.assertEqual(sorted(os.listdir(self.work)), [".device"])
        self.assertEqual(list(self.temp.iterdir()), [], "the temporary working directory must be removed")
