"""CI gates and release provenance tests using synthetic, temporary Git repos."""

import ast
import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
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
        with patch.object(ci, "workflow_documents", return_value={}), patch.object(ci, "check_workflows"):
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
