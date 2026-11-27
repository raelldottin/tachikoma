"""Offline CI policy and source-release tooling; never imports the game client."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from contextlib import nullcontext
import gzip
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest


# abspath, not resolve(): under Bazel this file is a runfiles symlink, and following it would leave the
# runfiles tree for the source checkout.
ROOT = Path(os.path.abspath(__file__)).parent.parent
WORKFLOWS = (
    "blocking-ci.yml",
    "automation-check.yml",
    "python-tests.yml",
    "postmerge-ci.yml",
    "release.yml",
    "daily-run.yml",
    "live-trial.yml",
    "live-trial-schedule.yml",
)
MAX_BLOB_BYTES = 1024 * 1024
RELEASE_ROOT_FILES = ("run.py", "requirements.txt", "LICENSE", "README.md")
# Non-Python files under sdk/ that the code reads at run time. Anything else under sdk/ is left out.
RELEASE_SDK_DATA_SUFFIXES = (".json",)
RELEASE_EXCLUDED_DIRS = frozenset({"tests", "test", "__pycache__"})
# Package data whose path contains one of these words is refused, not silently shipped or dropped.
RELEASE_PRIVATE_WORDS = frozenset(
    """
    secret secrets password passwords passwd token tokens credential credentials cookie cookies session sessions
    auth capture captures device devices profile profiles dump dumps har pcap log logs env
    """.split()
)
# daily-run.yml (SPEC-06): one matrix job runs every account at the same time, one leg per account on its own runner.
# A leg reads only its own PSS_ACCOUNT_<N>_* secrets, looked up by matrix.account, and its account step is skippable
# through the LEGACY_RUN_SKIP repository variable, so a trial account can be fenced off without deleting its secrets.
DAILY_ACCOUNTS_JOB = "accounts"
DAILY_REPORT_JOB = "training-report"
ACCOUNT_SECRET_MENTION_RE = re.compile(r"PSS_ACCOUNT_")
MATRIX_ACCOUNT_SECRET_RE = re.compile(r"secrets\[format\('PSS_ACCOUNT_\{0\}_[A-Z]+', matrix\.account\)\]")
LEGACY_RUN_SKIP_CONDITION = "${{ !contains(format(',{0},', vars.LEGACY_RUN_SKIP), format(',{0},', matrix.account)) }}"
# SPEC-06 S06-R6: while an authorization file under LIVE_AUTHORIZATION_DIR declares legacy_run_serialized, these
# workflows share one fixed workflow-level concurrency group with cancel-in-progress false, so GitHub never runs two
# of them at once.
LIVE_AUTHORIZATION_DIR = "docs/experiments/live"
SERIALIZED_WORKFLOWS = ("daily-run.yml", "live-trial.yml", "live-trial-schedule.yml")
# Mutation testing: sdk/training/ targets block merges (python-tests.yml's mutation-gate job); every other target stays
# advisory. Every sdk/training/ target needs a min_score of at least 0.90 and some tests; the four named ones must be
# present and keep both training test modules.
MUTATION_CONFIG = "automation/mutation/config.json"
BLOCKING_MUTATION_PREFIX = "sdk/training/"
BLOCKING_MUTATION_TESTS = ("tests.test_training_planner", "tests.test_training_parse")
REQUIRED_BLOCKING_MUTATION_TARGETS = (
    "sdk/training/model.py",
    "sdk/training/parse.py",
    "sdk/training/planner.py",
    "sdk/training/prestige.py",
    "sdk/training/strategy.py",
)
MIN_BLOCKING_MUTATION_SCORE = 0.90
MUTATION_GATE_JOB = "mutation-gate"
MUTATION_GATE_COMMAND = "python scripts/ci.py mutation-config --blocking"
# The gate's scripts/mutation.py command may only pick its config, parallelism and outputs: --target, --only,
# --operators, --tests, --min-score, --timeout or --list would narrow or skip what the blocking targets measure.
MUTATION_GATE_RUNNER = ("python", "scripts/mutation.py")
# Besides the mutation step, the gate job may only check out, set up Python, install the CI requirements and upload
# its report: any other command could rewrite the config, the tests or sdk/training before the checked command runs.
MUTATION_GATE_SETUP_RUNS = ("python -m pip install -r .github/requirements-ci.txt",)
MUTATION_GATE_ACTIONS = ("actions/checkout@", "actions/setup-python@", "actions/upload-artifact@")
MUTATION_GATE_ALLOWED_FLAGS = frozenset({"--config", "--jobs", "--json", "--summary", "--quiet", "--scratch-dir"})
# The gate step's lines before its scripts/mutation.py command, exactly: nothing can rewrite the checked config.
MUTATION_GATE_PRELUDE = (
    "set -euo pipefail",
    'config="$RUNNER_TEMP/mutation-blocking.json"',
    f'{MUTATION_GATE_COMMAND} > "$config"',
)
# Target keys a blocking training target may not set: functions and operators narrow which mutants it generates, and
# an explicit timeout replaces the one derived from the baseline, so a tiny value turns every mutant into a timeout,
# which counts as killed.
WEAKENING_MUTATION_TARGET_KEYS = ("functions", "operators", "timeout")
# Shell that would let a failing command pass: an or-list, an explicit success exit, errexit turned off or a trap.
SWALLOWING_SHELL_RE = re.compile(r"\|\||\bexit\s+0\b|\bset\s+\+[a-z]*e|\bset\s+\+o\b|\btrap\b")
# daily-run.yml crew training: each leg journals training to $RUNNER_TEMP/tachikoma-training/journals under its own
# alias and uploads that directory as its own artifact; the credential-free training-report job downloads every leg's
# artifact into the same directory, reports on every journal and uploads only the training directory.
DAILY_TRAINING_DIR = "$RUNNER_TEMP/tachikoma-training"
DAILY_TRAINING_JOURNAL_ARG = f'--training-journal-dir "{DAILY_TRAINING_DIR}/journals"'
DAILY_TRAINING_REPORT_COMMAND = "python run.py --report training"
# The account step passes --account-alias account-$ACCOUNT_NUMBER, with ACCOUNT_NUMBER set from matrix.account in its env.
DAILY_ACCOUNT_ALIAS = "account-$ACCOUNT_NUMBER"
DAILY_ACCOUNT_NUMBER_ENV = ("ACCOUNT_NUMBER", "${{ matrix.account }}")
ACCOUNT_ALIAS_ARG_RE = re.compile(r"""--account-alias[ =](?:"([^"\s]*)"|'([^'\s]*)'|([^"'\s]+))""")
# A `${{ }}` expression naming the secrets context anywhere before its closing `}}`: a lone `}` inside it (a format()
# placeholder) or a line break does not end it.
SECRETS_EXPRESSION_RE = re.compile(r"\$\{\{(?:(?!\}\}).)*?\bsecrets\b", re.DOTALL)
DAILY_TRAINING_ARTIFACT_PATH = "${{ runner.temp }}/tachikoma-training"
DAILY_JOURNALS_ARTIFACT_PATH = "${{ runner.temp }}/tachikoma-training/journals"
DAILY_JOURNAL_ARTIFACT = "training-journal-account-${{ matrix.account }}"
DAILY_JOURNAL_ARTIFACT_PATTERN = "training-journal-account-*"
DAILY_TRAINING_UPLOAD_KEYS = frozenset({"name", "path", "retention-days", "if-no-files-found"})
DAILY_JOURNAL_UPLOAD_KEYS = DAILY_TRAINING_UPLOAD_KEYS | {"overwrite"}
DAILY_JOURNAL_DOWNLOAD_KEYS = frozenset({"pattern", "path", "merge-multiple"})
MAX_DAILY_TRAINING_RETENTION_DAYS = 30
VERSION_RE = re.compile(r"v?((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))\Z")
# Hollow tests (`hollow-tests`): unittest tests that assert nothing about project behaviour. The baseline records the
# legacy ones; a new hollow test, or a baseline entry whose test is no longer hollow, fails the check.
HOLLOW_BASELINE = ".github/hollow-tests-baseline.json"
HOLLOW_RULES = frozenset({"no-assertion", "mock-only", "tautology"})
MOCK_CALL_ASSERTIONS = frozenset(
    """
    assert_called assert_called_once assert_called_with assert_called_once_with assert_not_called assert_has_calls
    assert_any_call assert_awaited assert_awaited_once assert_awaited_with assert_awaited_once_with assert_not_awaited
    assert_has_awaits assert_any_await
    """.split()
)
# Mock attributes that record how a mock was called; an assertion operand that is, or is read out of, one of them
# checks a mock call, like assert_called_with.
MOCK_CALL_RECORDS = frozenset(
    """
    call_count called await_count awaited call_args call_args_list mock_calls method_calls await_args await_args_list
    """.split()
)
MOCK_CONSTRUCTORS = frozenset(
    {"Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock", "PropertyMock"}
)
SINGLE_VALUE_ASSERTIONS = frozenset({"assertTrue", "assertFalse", "assertIsNone", "assertIsNotNone"})
PAIRED_VALUE_ASSERTIONS = frozenset(
    """
    assertEqual assertNotEqual assertIs assertIsNot assertIn assertNotIn assertAlmostEqual assertNotAlmostEqual
    assertGreater assertGreaterEqual assertLess assertLessEqual assertCountEqual assertListEqual assertTupleEqual
    assertDictEqual assertSetEqual assertSequenceEqual assertMultiLineEqual
    """.split()
)
# Two identical operands always pass these comparisons (a tautology) and always fail the irreflexive ones.
REFLEXIVE_ASSERTIONS = frozenset(
    """
    assertEqual assertIs assertAlmostEqual assertGreaterEqual assertLessEqual assertCountEqual assertListEqual
    assertTupleEqual assertDictEqual assertSetEqual assertSequenceEqual assertMultiLineEqual
    """.split()
)
IRREFLEXIVE_ASSERTIONS = frozenset(
    {"assertNotEqual", "assertIsNot", "assertNotAlmostEqual", "assertGreater", "assertLess"}
)
REFLEXIVE_OPERATORS = (ast.Eq, ast.Is, ast.LtE, ast.GtE)
IRREFLEXIVE_OPERATORS = (ast.NotEq, ast.IsNot, ast.Lt, ast.Gt)
UNITTEST_MODULES = frozenset({"unittest", "unittest.case"})
UNITTEST_TEST_CASES = frozenset({"TestCase", "IsolatedAsyncioTestCase"})
# Builtins that hand a test-built value to no project code; reading a value through one (print(x), len(x)) keeps
# it test-controlled, unless the module or test rebinds the name.
PURE_BUILTINS = frozenset("print str repr len int float bool type isinstance sorted list tuple dict set format".split())
# Expression nodes whose value depends only on their parts (operators and contexts are child nodes too). An
# expression made of these, literals and test-controlled names is a value the test built itself.
CONTROLLED_NODES = (
    ast.Constant,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Tuple,
    ast.List,
    ast.Set,
    ast.Dict,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.Call,
    ast.keyword,
    ast.Starred,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.expr_context,
    ast.operator,
    ast.unaryop,
    ast.boolop,
    ast.cmpop,
)


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def require_success(results: object, required: list[str]) -> None:
    """Missing, skipped, cancelled, malformed and failed jobs all fail the gate."""
    if not required or len(set(required)) != len(required):
        raise ValueError("required jobs must be nonempty and unique")
    if not isinstance(results, dict) or set(results) != set(required):
        raise ValueError("upstream results do not match the required jobs")
    for name in required:
        result = results[name]
        if not isinstance(result, dict) or result.get("result") != "success":
            raise ValueError(f"required job did not succeed: {name}")


def lint_counts(diagnostics: list[dict], repo: Path) -> Counter:
    counts = Counter()
    for item in diagnostics:
        path = Path(item["filename"]).resolve().relative_to(repo.resolve()).as_posix()
        counts[(path, item["code"], item["message"])] += 1
    return counts


def lint_baseline(payload: dict) -> Counter:
    if payload.get("schema_version") != 1 or not isinstance(payload.get("violations"), list):
        raise ValueError("invalid lint baseline schema")
    counts = Counter()
    for item in payload["violations"]:
        key = (item["path"], item["code"], item["message"])
        if key in counts or type(item["count"]) is not int or item["count"] <= 0:
            raise ValueError("duplicate or invalid lint baseline entry")
        counts[key] = item["count"]
    return counts


def check_lint(repo: Path) -> None:
    run = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "run.py", "sdk", "tests", "scripts", "--output-format", "json"],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if run.returncode not in (0, 1):
        raise ValueError(f"Ruff could not run: {run.stderr.strip()}")
    actual = lint_counts(json.loads(run.stdout), repo)
    expected = lint_baseline(json.loads((repo / ".github/ruff-baseline.json").read_text(encoding="utf-8")))
    if actual != expected:
        for key, count in sorted((actual - expected).items()):
            print(f"NEW ({count}): {key[0]}: {key[1]} {key[2]}")
        for key, count in sorted((expected - actual).items()):
            print(f"REMOVE FIXED BASELINE ENTRY ({count}): {key[0]}: {key[1]} {key[2]}")
        raise ValueError("lint findings changed; fix new findings and remove resolved baseline entries")
    print(f"No new lint findings; {sum(expected.values())} explicitly recorded legacy findings remain.")


FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
# A base-class target: a unittest TestCase (from unittest, or a TestCase-named base from outside the tests tree).
TEST_CASE = "TestCase"


def _trailing_name(node: ast.AST) -> str | None:
    """`b` for the expressions `b` and `a.b`; None for anything else."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_super(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super"


def _receiver(node: ast.Attribute) -> str | None:
    """ "self" for self.x, cls.x and super().x; the name for <name>.x; None for anything else."""
    if _is_super(node.value):
        return "self"
    if isinstance(node.value, ast.Name):
        return "self" if node.value.id in {"self", "cls"} else node.value.id
    return None


def _module_scope(tree: ast.Module):
    """Every node a module runs at its top level: statements outside any def, lambda or class body, including
    those under if/try/with, and the defs and classes themselves."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, SCOPE_NODES):
            stack.extend(ast.iter_child_nodes(node))


def _walk_function(function: ast.AST, is_test_case):
    """Every node a call of function runs: its body, with lambdas and nested classes other than TestCases
    (is_test_case(node)), since code under test calls the methods of a fake defined in a test. A nested function's
    body is left to helper resolution, which walks it only when the function is referenced."""
    stack = [(node, False) for node in function.body]
    while stack:
        node, in_class = stack.pop()
        if isinstance(node, ast.ClassDef) and is_test_case(node):
            continue
        yield node
        if isinstance(node, FUNCTION_NODES) and not in_class:
            continue
        inside = in_class or isinstance(node, ast.ClassDef)
        stack.extend((child, inside) for child in ast.iter_child_nodes(node))


def _local_names(function: ast.AST, is_test_case) -> set[str]:
    """Parameters and assigned names of function: a reference to one is a local value, never a helper."""
    arguments = function.args
    names = {
        argument.arg
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            arguments.vararg,
            arguments.kwarg,
        )
        if argument is not None
    }
    names.update(
        node.id
        for node in _walk_function(function, is_test_case)
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load)
    )
    return names


def _controlled(expression: ast.AST, names: frozenset) -> bool:
    """True when expression is built only from literals, the test-controlled names in names, and mocks it creates
    from those (MagicMock(return_value=3)): a value the test made itself, so no regression can change it."""
    if isinstance(expression, ast.Call) and _trailing_name(expression.func) in MOCK_CONSTRUCTORS:
        parts = [*expression.args, *(keyword.value for keyword in expression.keywords)]
    elif isinstance(expression, ast.Name):
        return expression.id in names
    elif isinstance(expression, CONTROLLED_NODES):
        parts = list(ast.iter_child_nodes(expression))
    else:
        return False
    return all(_controlled(part, names) for part in parts)


def _mock_call_record(operand: ast.AST) -> bool:
    """m.call_count, m.call_args[0][0], m.call_args.kwargs["x"], ...: a value read out of a mock's call records."""
    while isinstance(operand, (ast.Attribute, ast.Subscript)):
        if isinstance(operand, ast.Attribute) and operand.attr in MOCK_CALL_RECORDS:
            return True
        operand = operand.value
    return False


def _always_truthy(operand: ast.AST) -> bool:
    """A non-empty tuple, list, set or dict display, or a mock assertion method referenced but never called."""
    if isinstance(operand, (ast.Tuple, ast.List, ast.Set)):
        return bool(operand.elts)
    if isinstance(operand, ast.Dict):
        return bool(operand.keys)
    return isinstance(operand, ast.Attribute) and operand.attr in MOCK_CALL_ASSERTIONS


def _assertion_operands(node: ast.AST) -> tuple[str, list, str | None]:
    """("mock", [], None) for a mock call assertion; ("tautology", [], None) for one that always passes whatever the
    values (`assert (x, "msg")`, assertTrue(m.assert_called)); ("real", [], None) for an assertion that checks
    project behaviour whatever its arguments (assertRaises, `assert False`, assertTrue(False), ...); otherwise
    ("value", operands, relation), relation "reflexive" when two identical operands always pass (assertEqual,
    `==`, `is`, `<=`, ...), "irreflexive" when they always fail (assertNotEqual, `!=`, `<`, ...), else None."""
    relation = None
    if isinstance(node, ast.Assert):
        test = node.test
        if isinstance(test, ast.Constant) and not test.value:
            return "real", [], None
        if _always_truthy(test):
            return "tautology", [], None
        if isinstance(test, ast.Compare):
            operands = [test.left, *test.comparators]
            if len(test.ops) == 1:
                if isinstance(test.ops[0], REFLEXIVE_OPERATORS):
                    relation = "reflexive"
                elif isinstance(test.ops[0], IRREFLEXIVE_OPERATORS):
                    relation = "irreflexive"
        else:
            operands = [test]
    else:
        name = _trailing_name(node.func)
        if name in MOCK_CALL_ASSERTIONS:
            return "mock", [], None
        if name in SINGLE_VALUE_ASSERTIONS and node.args:
            first = node.args[0]
            # assertTrue(False) / assertFalse(True) always fail: a reached-the-wrong-branch marker, like self.fail().
            if isinstance(first, ast.Constant) and (
                (name == "assertTrue" and not first.value) or (name == "assertFalse" and first.value)
            ):
                return "real", [], None
            if name in {"assertTrue", "assertIsNotNone"} and _always_truthy(first):
                return "tautology", [], None
            operands = [first]
        elif name in PAIRED_VALUE_ASSERTIONS and len(node.args) >= 2:
            operands = list(node.args[:2])
            if name in REFLEXIVE_ASSERTIONS:
                relation = "reflexive"
            elif name in IRREFLEXIVE_ASSERTIONS:
                relation = "irreflexive"
        else:
            return "real", [], None
    unwrapped = []
    for operand in operands:
        while isinstance(operand, ast.UnaryOp) and isinstance(operand.op, ast.Not):
            operand = operand.operand
        unwrapped.append(operand)
    return "value", unwrapped, relation


def _controlled_names(function: ast.AST, operands: list, pure: frozenset) -> frozenset:
    """Names in function that hold a value the test built itself, from literals or from mocks no project code sees,
    together with pure, the side-effect-free builtins (len, str, print, ...) the test has not rebound.

    A name qualifies when each of its bindings is a plain `name = value` or `name: T = value` statement with a
    controlled value (see _controlled), run unconditionally or only under `if`/`while` tests that are themselves
    controlled. Each read of it must be in an assertion operand, in the value of another qualifying name's
    binding, or in a controlled `if`/`while` test, expression statement (`m.reset_mock()`, `print(x)`) or
    attribute or item assignment (`m.fetch.return_value = 5`) that runs unconditionally or under controlled tests.
    So `m = MagicMock(return_value=3)` qualifies until m is passed to a function other than a pure builtin, stored
    where project code can reach it, or configured from a project value. A name bound or read any other way (loop,
    with, try, match, augmented assignment, unpacking, walrus, except, import, nonlocal, global, a nested function,
    lambda or class) never qualifies; nor does a mock from patch(), which the patched project code sees.
    """
    operand_names = {id(node) for operand in operands for node in ast.walk(operand) if isinstance(node, ast.Name)}
    # name -> [(value, guarding tests)] for each binding of it.
    bindings: dict[str, list] = {}
    binding_targets: set[int] = set()
    # id of a Name read -> (names that must qualify, expressions that must be controlled) for the read to be safe.
    safe_if: dict[int, tuple] = {}

    def note(node, targets, controlled):
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                safe_if[id(inner)] = (targets, controlled)

    def visit(body, guards):
        for statement in body:
            if isinstance(statement, ast.Assign) and all(isinstance(target, ast.Name) for target in statement.targets):
                targets = statement.targets
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value:
                targets = [statement.target]
            elif isinstance(statement, (ast.If, ast.While)):
                note(statement.test, frozenset(), [statement.test, *guards])
                visit(statement.body, [*guards, statement.test])
                visit(statement.orelse, [*guards, statement.test])
                continue
            elif isinstance(statement, ast.Expr):
                note(statement.value, frozenset(), [statement.value, *guards])
                continue
            elif isinstance(statement, ast.Assign) and all(
                isinstance(target, (ast.Attribute, ast.Subscript)) for target in statement.targets
            ):
                note(statement, frozenset(), [*statement.targets, statement.value, *guards])
                continue
            else:
                continue
            note(statement.value, frozenset(target.id for target in targets), [])
            for target in targets:
                bindings.setdefault(target.id, []).append((statement.value, guards))
                binding_targets.add(id(target))

    visit(function.body, [])
    # Match patterns bind names without an ast.Name node; Python 3.9's ast has no match classes at all.
    match_captures = tuple(getattr(ast, name) for name in ("MatchAs", "MatchStar") if hasattr(ast, name))
    match_mapping = getattr(ast, "MatchMapping", ())
    disqualified: set[str] = set()
    reads: dict[str, list] = {}
    stack = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, SCOPE_NODES):
            if not isinstance(node, ast.Lambda):
                disqualified.add(node.name)
            disqualified.update(inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name))
            continue
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            disqualified.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            disqualified.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            disqualified.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, match_captures) and node.name:
            disqualified.add(node.name)
        elif isinstance(node, match_mapping) and node.rest:
            disqualified.add(node.rest)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                if id(node) in safe_if and id(node) not in operand_names:
                    reads.setdefault(node.id, []).append(safe_if[id(node)])
                elif id(node) not in operand_names:
                    disqualified.add(node.id)
            elif id(node) not in binding_targets:
                disqualified.add(node.id)
        stack.extend(ast.iter_child_nodes(node))
    names = frozenset(bindings) - disqualified
    while True:
        known = names | pure
        kept = frozenset(
            name
            for name in names
            if all(_controlled(part, known) for value, guards in bindings[name] for part in (value, *guards))
            and all(
                targets <= names and all(_controlled(part, known) for part in controlled)
                for targets, controlled in reads.get(name, ())
            )
        )
        if kept == names:
            return names | pure
        names = kept


def _value_assertion_kind(operands: list, relation: str | None, controlled_names: frozenset) -> str:
    if any(_mock_call_record(operand) for operand in operands):
        return "mock"
    if (
        relation is not None
        and len(operands) == 2
        and ast.dump(operands[0]) == ast.dump(operands[1])
        and not any(isinstance(node, ast.Call) for operand in operands for node in ast.walk(operand))
    ):
        # x == x always passes; x != x always fails, a reached-the-wrong-branch marker like self.fail().
        return "tautology" if relation == "reflexive" else "real"
    if all(_controlled(operand, controlled_names) for operand in operands):
        return "tautology"
    return "real"


def _skipped(node: ast.AST) -> bool:
    """A class or function decorated with @unittest.skip(...)."""
    return any(
        _trailing_name(decorator.func if isinstance(decorator, ast.Call) else decorator) == "skip"
        for decorator in node.decorator_list
    )


def _unconditionally_skipped(function: ast.AST) -> bool:
    """@unittest.skip(...), or a self.skipTest(...) / raise SkipTest(...) statement directly in the test body."""
    if _skipped(function):
        return True
    for statement in function.body:
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and _trailing_name(statement.value.func) == "skipTest"
        ):
            return True
        if isinstance(statement, ast.Raise) and statement.exc is not None:
            exception = statement.exc.func if isinstance(statement.exc, ast.Call) else statement.exc
            if _trailing_name(exception) == "SkipTest":
                return True
    return False


class _HollowModule:
    """The symbol table of one parsed module of the tests tree, and the assertions each of its functions reaches.

    Names resolve the way Python binds them, never by a search for any definition with the same name:
    - functions and classes at module level (outside any def or class body, including under if/try);
    - unittest's TestCase and IsolatedAsyncioTestCase, through `import unittest [as u]` (u.TestCase) or
      `from unittest import TestCase [as TC]` (unittest.case too);
    - names imported from the `tests` package, looked up in that module's own table;
    - a function nested in a function, from that function and the functions nested in it.
    tree is the _HollowTree holding every module, so imports and base classes are followed across modules.
    """

    def __init__(self, syntax: ast.Module, path: str, package: str, tree: "_HollowTree"):
        self.path = path
        self.tree = tree
        self.functions: dict[str, list] = {}
        self.classes: dict[str, list] = {}
        # Every name the module binds at its top level or imports anywhere: a builtin under one of these names is
        # not the builtin.
        self.bound: set[str] = set()
        for node in _module_scope(syntax):
            if isinstance(node, FUNCTION_NODES):
                self.functions.setdefault(node.name, []).append(node)
                self.bound.add(node.name)
            elif isinstance(node, ast.ClassDef):
                self.classes.setdefault(node.name, []).append(node)
                self.bound.add(node.name)
            elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                self.bound.add(node.id)
        # Imports anywhere in the module: one inside a function binds the name it would bind at module level.
        self.unittest_modules: set[str] = set()  # names bound to unittest or unittest.case
        self.test_case_names: set[str] = set()  # names bound to a unittest TestCase class
        # alias -> (module, attribute) for each name imported from the tests package; attribute None for a module.
        self.imports: dict[str, tuple[str, str | None]] = {}
        for node in ast.walk(syntax):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = package.split(".")
                    parts = parts[: len(parts) - node.level + 1]
                    module = ".".join(parts + ([node.module] if node.module else []))
                else:
                    module = node.module or ""
                for alias in node.names:
                    name = alias.asname or alias.name
                    self.bound.add(name)
                    if module in UNITTEST_MODULES and alias.name in UNITTEST_TEST_CASES:
                        self.test_case_names.add(name)
                    elif module == "tests" or module.startswith("tests."):
                        self.imports[name] = (module, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    # `import a.b` binds a; `import a.b as c` binds c to a.b.
                    name = alias.asname or alias.name.split(".")[0]
                    module = alias.name if alias.asname else name
                    self.bound.add(name)
                    if module in UNITTEST_MODULES:
                        self.unittest_modules.add(name)
                    elif alias.asname and module.startswith("tests."):
                        self.imports[name] = (module, None)
        # For each function: the class whose instance `self` is in it (methods, and functions nested in methods),
        # the function it is nested in, and the functions defined directly in its body.
        self.owner: dict[int, ast.ClassDef | None] = {}
        self.parent: dict[int, ast.AST | None] = {}
        self.nested: dict[int, dict[str, list]] = {}

        def index(node, owner, function, class_body):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, FUNCTION_NODES):
                    self.owner[id(child)] = owner
                    self.parent[id(child)] = function
                    if function is not None and not class_body:
                        self.nested.setdefault(id(function), {}).setdefault(child.name, []).append(child)
                    index(child, owner, child, False)
                elif isinstance(child, ast.ClassDef):
                    index(child, child, function, True)
                else:
                    index(child, owner, function, class_body)

        index(syntax, None, None, False)
        self._direct: dict[int, tuple[frozenset, tuple]] = {}

    def imported(self, alias: str) -> tuple["_HollowModule | None", str | None]:
        """The module an imported alias comes from, and the attribute it names there (None for the module)."""
        module, attribute = self.imports[alias]
        if attribute is not None and f"{module}.{attribute}" in self.tree.modules:
            module, attribute = f"{module}.{attribute}", None
        return self.tree.modules.get(module), attribute

    def functions_named(self, name: str, seen: frozenset = frozenset()) -> list:
        """(module, function) for the module-level function name refers to here, following tests imports."""
        if name in self.functions:
            return [(self, node) for node in self.functions[name]]
        if name in self.imports and (id(self), name) not in seen:
            module, attribute = self.imported(name)
            if module is not None and attribute is not None:
                return module.functions_named(attribute, seen | {(id(self), name)})
        return []

    def classes_named(self, name: str, seen: frozenset = frozenset()) -> list | None:
        """TEST_CASE and (module, class) for what name, bound here, names as a class; None when it is not bound
        to anything in the tests tree or unittest (a third-party name, or no binding at all)."""
        if name in self.classes:
            return [(self, node) for node in self.classes[name]]
        if name in self.test_case_names:
            return [TEST_CASE]
        if name in self.imports:
            module, attribute = self.imported(name)
            if module is None:
                return [TEST_CASE]  # From a tests module outside the parsed tree: assume a test base.
            if attribute is None or (id(self), name) in seen:
                return []
            return module.classes_named(attribute, seen | {(id(self), name)}) or []
        return None

    def base_targets(self, base: ast.AST) -> list:
        """TEST_CASE and (module, class) for the classes a base-class expression names. A base bound outside the
        tests tree and unittest counts as a TestCase when its name ends with TestCase (django.test.TestCase,
        absltest.TestCase); a class defined in the tree counts only when it subclasses one."""
        targets = None
        if isinstance(base, ast.Name):
            targets = self.classes_named(base.id)
        elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            receiver = base.value.id
            if receiver in self.unittest_modules:
                targets = [TEST_CASE] if base.attr in UNITTEST_TEST_CASES else []
            elif receiver in self.imports:
                module, attribute = self.imported(receiver)
                if module is None:
                    targets = [TEST_CASE]
                elif attribute is None:
                    targets = module.classes_named(base.attr) or []
                else:
                    targets = []
        if targets is None:
            targets = [TEST_CASE] if (_trailing_name(base) or "").endswith("TestCase") else []
        return targets

    def bases(self, node: ast.ClassDef) -> list[tuple["_HollowModule", ast.ClassDef]]:
        """(module, class) for each base of node defined in the tests tree."""
        return [target for base in node.bases for target in self.base_targets(base) if target is not TEST_CASE]

    def is_test_case(self, node: ast.ClassDef) -> bool:
        """node subclasses a unittest TestCase, directly or through a base in the tree."""
        return any(
            target is TEST_CASE or id(target[1]) in self.tree.test_cases
            for base in node.bases
            for target in self.base_targets(base)
        )

    @staticmethod
    def lookup(start: list, attribute: str) -> list:
        """(module, method) for the methods named attribute in the first class that defines one, searching start
        and then their bases breadth first."""
        queue = list(start)
        seen = set()
        while queue:
            module, node = queue.pop(0)
            if id(node) in seen:
                continue
            seen.add(id(node))
            methods = [item for item in node.body if isinstance(item, FUNCTION_NODES) and item.name == attribute]
            if methods:
                return [(module, method) for method in methods]
            queue.extend(module.bases(node))
        return []

    def resolve_name(self, name: str, function: ast.AST) -> list:
        """(module, function) for a bare name read in function: a function nested in it or in a function enclosing
        it, then a module-level function, defined here or imported from the tests package."""
        scope = function
        while scope is not None:
            found = self.nested.get(id(scope), {}).get(name)
            if found:
                return [(self, node) for node in found]
            scope = self.parent.get(id(scope))
        return self.functions_named(name)

    def resolve_attribute(self, node: ast.Attribute, function: ast.AST) -> list:
        """(module, function) for self.x, cls.x and super().x (methods of function's class and its bases only),
        <class>.x and <imported tests module>.x."""
        receiver = _receiver(node)
        if receiver == "self":
            owner = self.owner.get(id(function))
            if owner is None:
                return []
            return self.lookup(self.bases(owner) if _is_super(node.value) else [(self, owner)], node.attr)
        if receiver in self.imports:
            module, attribute = self.imported(receiver)
            if module is not None and attribute is None:
                return module.functions_named(node.attr)
        if receiver is None:
            return []
        classes = self.classes_named(receiver) or []
        return self.lookup([target for target in classes if target is not TEST_CASE], node.attr)

    def resolve(self, expression: ast.AST, function: ast.AST, local: set) -> list:
        if isinstance(expression, ast.Name):
            return [] if expression.id in local else self.resolve_name(expression.id, function)
        if isinstance(expression, ast.Attribute):
            return self.resolve_attribute(expression, function)
        return []

    def direct(self, function: ast.AST) -> tuple[frozenset, tuple]:
        """The kinds of assertion in function's own body, and the (module, function) helpers it references. A call
        named assert* that resolves to a helper in the tree is followed like any helper; only one that resolves to
        nothing there (self.assertEqual, a library's assert_frame_equal) counts as an assertion by its name."""
        key = id(function)
        if key not in self._direct:
            local = _local_names(function, self.is_test_case)
            assertions = []
            helpers = []
            for node in _walk_function(function, self.is_test_case):
                if isinstance(node, ast.Assert):
                    assertions.append(_assertion_operands(node))
                elif isinstance(node, ast.Call):
                    name = _trailing_name(node.func)
                    if (
                        name is not None
                        and name.lstrip("_").startswith("assert")
                        and not self.resolve(node.func, function, local)
                    ):
                        assertions.append(_assertion_operands(node))
                    elif name == "fail" and isinstance(node.func, ast.Attribute) and _receiver(node.func) == "self":
                        assertions.append(("real", [], None))
                elif isinstance(node, ast.Attribute) or (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
                    helpers.extend(self.resolve(node, function, local))
            controlled = _controlled_names(
                function,
                [operand for kind, operands, _ in assertions if kind == "value" for operand in operands],
                PURE_BUILTINS - self.bound - local,
            )
            kinds = frozenset(
                _value_assertion_kind(operands, relation, controlled) if kind == "value" else kind
                for kind, operands, relation in assertions
            )
            self._direct[key] = (kinds, tuple(helpers))
        return self._direct[key]

    def verdict(self, function: ast.AST) -> str | None:
        """The hollow rule function breaks, following the helpers it references, or None when it checks behaviour."""
        kinds: set[str] = set()
        seen: set[int] = set()
        pending = [(self, function)]
        while pending:
            module, current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            direct_kinds, helpers = module.direct(current)
            kinds |= direct_kinds
            pending.extend(helpers)
        if not kinds:
            return "no-assertion"
        if "real" in kinds:
            return None
        return "mock-only" if "mock" in kinds else "tautology"


class _HollowTree:
    """Every parsed module of a tests tree, keyed by dotted module name, and the unittest test classes in it."""

    def __init__(self, sources: dict[str, str]):
        self.modules: dict[str, _HollowModule] = {}
        for path, source in sources.items():
            try:
                syntax = ast.parse(source, filename=path)
            except SyntaxError as error:
                raise ValueError(f"cannot parse {path}: {error.msg}") from None
            parts = PurePosixPath(path).with_suffix("").parts
            package = ".".join(parts[:-1])
            name = package if parts[-1] == "__init__" else ".".join(parts)
            self.modules[name] = _HollowModule(syntax, path, package, self)
        # ids of the module-level classes that subclass a unittest TestCase, directly or through bases anywhere in
        # the tree: a fixed point over all modules at once, so an import cycle cannot hide one.
        self.test_cases: set[int] = set()
        changed = True
        while changed:
            changed = False
            for module in self.modules.values():
                for node in (node for nodes in module.classes.values() for node in nodes):
                    if id(node) not in self.test_cases and module.is_test_case(node):
                        self.test_cases.add(id(node))
                        changed = True

    def tests(self, paths) -> list[tuple[str, "_HollowModule", ast.AST]]:
        """("<path>::<class>.<test>", module, function) for each test* method that runs in a module-level TestCase
        of a module in paths: methods of the class itself and of every base it inherits from in the tree (mixins,
        reported under the module that defines them), unless the defining class is skipped as a whole. unittest
        loads neither module-level functions nor classes nested in a class or function, so those are not tests."""
        pending = [
            (module, node)
            for module in self.modules.values()
            if module.path in paths
            for nodes in module.classes.values()
            for node in nodes
            if id(node) in self.test_cases
        ]
        classes: dict[int, tuple[_HollowModule, ast.ClassDef]] = {}
        while pending:
            module, node = pending.pop()
            if id(node) not in classes:
                classes[id(node)] = (module, node)
                pending.extend(module.bases(node))
        return [
            (f"{module.path}::{node.name}.{item.name}", module, item)
            for module, node in classes.values()
            if not _skipped(node)
            for item in node.body
            if isinstance(item, FUNCTION_NODES) and item.name.startswith("test")
        ]


def hollow_tests_in_sources(sources: dict[str, str], examine=None) -> list[tuple[str, str]]:
    """Sorted ("<path>::<class>.<test>", rule) pairs for the hollow tests that run in the TestCases of the modules
    of sources, a {posix path: source} map of a tests tree, whose paths are in examine (default: the test_*.py
    modules). Helpers, base classes and mixins one module imports from another module in sources are followed
    into it.

    no-assertion: the test, and the helpers it references, assert nothing.
    mock-only: its only assertions check how mocks were called (assert_called_once_with, call_count, call_args, ...).
    tautology: its only assertions check values the test built itself, from literals or from mocks no project code
    ever sees, or compare a name with itself, or are always true, so they cannot detect a regression.
    """
    tree = _HollowTree(sources)
    if examine is None:
        examine = {path for path in sources if PurePosixPath(path).name.startswith("test_")}
    findings = []
    for test, module, function in tree.tests(examine):
        rule = None if _unconditionally_skipped(function) else module.verdict(function)
        if rule:
            findings.append((test, rule))
    return sorted(findings)


def hollow_tests_in_source(source: str, path: str) -> list[tuple[str, str]]:
    """hollow_tests_in_sources for one module on its own, examined whatever its file name."""
    return hollow_tests_in_sources({path: source}, examine={path})


def tracked_files(repo: Path) -> list[Path]:
    """Repository-relative paths of the files git tracks in repo."""
    return [Path(os.fsdecode(name)) for name in git(repo, "ls-files", "-z").split(b"\0") if name]


def hollow_tests(repo: Path, tracked: list[Path] | None = None) -> set[tuple[str, str]]:
    """Hollow tests in the test_*.py modules under tests/, following helpers imported from any module there.

    Only tracked regular files are read: tracked (repository-relative paths) defaults to what git tracks when repo
    is a git checkout, and symlinks are skipped, as check_repository does. Outside a checkout (a Bazel runfiles
    tree, made of symlinks) every module under tests/ is read.
    """
    if tracked is None and (repo / ".git").exists():
        tracked = tracked_files(repo)
    if tracked is None:
        files = sorted((repo / "tests").rglob("*.py"))
    else:
        files = sorted(
            repo / path
            for path in tracked
            if path.parts[:1] == ("tests",) and path.suffix == ".py" and not (repo / path).is_symlink()
        )
    sources = {path.relative_to(repo).as_posix(): path.read_text(encoding="utf-8") for path in files}
    return set(hollow_tests_in_sources(sources))


def hollow_baseline(payload: object) -> set[tuple[str, str]]:
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload.get("hollow_tests"), list)
    ):
        raise ValueError("invalid hollow-test baseline schema")
    entries = set()
    tests = set()
    for item in payload["hollow_tests"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"test", "rule"}
            or not isinstance(item["test"], str)
            or "::" not in item["test"]
            or not isinstance(item["rule"], str)
            or item["rule"] not in HOLLOW_RULES
            or item["test"] in tests
        ):
            raise ValueError("duplicate or invalid hollow-test baseline entry")
        tests.add(item["test"])
        entries.add((item["test"], item["rule"]))
    keys = [(item["test"], item["rule"]) for item in payload["hollow_tests"]]
    if keys != sorted(keys):
        raise ValueError("hollow-test baseline entries must be sorted")
    return entries


def hollow_test_changes(actual: set, expected: set) -> list[str]:
    return [f"NEW: {test}: {rule}" for test, rule in sorted(actual - expected)] + [
        f"REMOVE FIXED BASELINE ENTRY: {test}: {rule}" for test, rule in sorted(expected - actual)
    ]


def check_hollow_tests(repo: Path, tracked: list[Path] | None = None) -> None:
    expected = hollow_baseline(json.loads((repo / HOLLOW_BASELINE).read_text(encoding="utf-8")))
    changes = hollow_test_changes(hollow_tests(repo, tracked), expected)
    if changes:
        for line in changes:
            print(line)
        raise ValueError("hollow tests changed; fix new hollow tests and remove resolved baseline entries")
    print(f"No new hollow tests; {len(expected)} explicitly recorded legacy hollow tests remain.")


def workflow_documents(repo: Path) -> dict:
    import yaml

    # BaseLoader preserves GitHub's YAML 1.2 `on` key (SafeLoader treats it as True).
    return {
        name: yaml.load((repo / ".github/workflows" / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for name in WORKFLOWS
    }


def check_workflows(documents: dict) -> None:
    for name, document in documents.items():
        if document.get("permissions") != {"contents": "read"}:
            raise ValueError(f"workflow needs read-only default permissions: {name}")
        if "pull_request_target" in document.get("on", {}):
            raise ValueError("privileged pull-request execution is forbidden")
        for job_name, job in document["jobs"].items():
            allowed_permissions = (
                {"contents": "write"} if (name, job_name) == ("release.yml", "publish") else {"contents": "read"}
            )
            if job.get("permissions", {"contents": "read"}) != allowed_permissions:
                raise ValueError(f"unexpected job permissions: {name}/{job_name}")
            if "runs-on" in job and "timeout-minutes" not in job:
                raise ValueError(f"job lacks a timeout: {name}")
            for step in job.get("steps", []):
                action = step.get("uses", "")
                if action and not re.fullmatch(r"[\w.-]+/[\w.-]+@[a-f0-9]{40}", action):
                    raise ValueError(f"external action must be pinned to a commit: {action}")
                if (
                    action.startswith("actions/checkout@")
                    and step.get("with", {}).get("persist-credentials") != "false"
                ):
                    raise ValueError("checkout must not persist its token")
    blocking = documents["blocking-ci.yml"]
    if not {"pull_request", "merge_group"} <= set(blocking["on"]):
        raise ValueError("blocking CI must cover pull requests and merge queues")
    if any("paths" in event or "paths-ignore" in event for event in blocking["on"].values() if isinstance(event, dict)):
        raise ValueError("required workflow must not skip changes via path filters")
    for filename in ("blocking-ci.yml", "postmerge-ci.yml"):
        gate = documents[filename]["jobs"]["required"]
        if gate.get("if") != "${{ always() }}" or gate.get("needs") != ["checks", "tests"]:
            raise ValueError("aggregate gate must always evaluate all required workflows")
    check_daily_run(documents["daily-run.yml"])
    check_mutation_gate(documents)
    # live-trial.yml touches real accounts, so only an owner's manual dispatch may start it.
    if set(documents["live-trial.yml"]["on"]) != {"workflow_dispatch"}:
        raise ValueError("the live trial workflow must run only on a manual dispatch")
    # live-trial-schedule.yml runs the SPEC-07 trial with secrets: only its schedule may start it.
    if set(documents["live-trial-schedule.yml"]["on"]) != {"schedule"}:
        raise ValueError("the trial schedule workflow must run only on its schedule")
    release = documents["release.yml"]
    jobs = release["jobs"]
    if "workflow_dispatch" not in release["on"]:
        raise ValueError("source release must support manual verification")
    if jobs["build"].get("if") != "${{ github.event_name == 'push' || github.ref == 'refs/heads/main' }}":
        raise ValueError("manual release verification must build only main")
    if jobs["verify"].get("needs") != "build" or jobs["publish"].get("needs") != "verify":
        raise ValueError("release publication must depend on downloaded artifact verification")
    if jobs["publish"].get("if") != "${{ github.event_name == 'push' && github.ref_type == 'tag' }}":
        raise ValueError("release publication must require a real tag push")


def serialized_authorizations(repo: Path) -> list[str]:
    """The JSON files under LIVE_AUTHORIZATION_DIR (any depth) that declare legacy_run_serialized, repo-relative."""
    found = []
    for path in sorted((repo / LIVE_AUTHORIZATION_DIR).rglob("*.json")):
        relative = path.relative_to(repo).as_posix()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValueError(f"{relative}: not valid JSON ({error})") from None
        if isinstance(payload, dict) and "legacy_run_serialized" in payload:
            found.append(relative)
    return found


def check_serialized_legacy_run(documents: dict, serialized: list[str]) -> None:
    """S06-R6: a legacy_run_serialized claim holds only while the pss5 workflows can never run at the same time.

    With any claim in ``serialized``, each of SERIALIZED_WORKFLOWS sets the same fixed concurrency group at the
    workflow level (none at the job level) with ``cancel-in-progress: false``.
    """
    if not serialized:
        return
    claim = f"{serialized[0]} declares legacy_run_serialized"
    groups = set()
    for name in SERIALIZED_WORKFLOWS:
        document = documents[name]
        concurrency = document.get("concurrency")
        if not isinstance(concurrency, dict) or not _text(concurrency.get("group")):
            raise ValueError(
                f"{name} must set a workflow-level concurrency group, written as a mapping with a group name and "
                f"cancel-in-progress: false, because {claim}"
            )
        if "${{" in concurrency["group"]:
            raise ValueError(f"{name}: the shared concurrency group must be a fixed name, not an expression")
        if concurrency.get("cancel-in-progress") != "false":
            raise ValueError(f"{name}: the shared concurrency group must keep cancel-in-progress: false")
        if any("concurrency" in job for job in document.get("jobs", {}).values()):
            raise ValueError(f"{name}: set the shared concurrency group only at the workflow level, not on a job")
        groups.add(concurrency["group"])
    if len(groups) != 1:
        raise ValueError(f"{', '.join(SERIALIZED_WORKFLOWS)} must share one concurrency group, because {claim}")


def _text(value: object) -> str:
    """A workflow scalar as text; a missing value or a mapping or list where a scalar belongs is the empty string."""
    return value if isinstance(value, str) else ""


def _condition(value: object) -> str:
    """A step's `if:` without its optional `${{ }}` wrapper and without whitespace, so `${{ always() }}` and
    `always()` compare equal; a condition only partly inside an expression keeps its `${{` and `}}` and matches
    neither."""
    text = _text(value).strip()
    wrapped = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.DOTALL)
    return "".join((wrapped.group(1) if wrapped else text).split())


def check_daily_run(document: dict) -> None:
    """daily-run.yml runs every account at once and reports on every account's crew training (SPEC-06).

    The ``accounts`` job is a matrix with one leg per account number. Its single account step reads that leg's own
    PSS_ACCOUNT_<N>_* secrets, is skippable through LEGACY_RUN_SKIP, journals training under the leg's alias and keeps
    continue-on-error; the leg then uploads its journal. The ``training-report`` job runs after every leg, also when one
    failed or was skipped, is given no secrets, downloads every journal, reports on them and uploads the result.
    """
    if SECRETS_EXPRESSION_RE.search(json.dumps(document.get("env", {}))):
        raise ValueError("daily-run: only the account step may read secrets, not the workflow env")
    jobs = document.get("jobs", {})
    if set(jobs) != {DAILY_ACCOUNTS_JOB, DAILY_REPORT_JOB}:
        raise ValueError(f"daily-run must have exactly the jobs {DAILY_ACCOUNTS_JOB} and {DAILY_REPORT_JOB}")
    check_daily_accounts_job(jobs[DAILY_ACCOUNTS_JOB])
    check_daily_report_job(jobs[DAILY_REPORT_JOB])


def _check_matrix(job: dict) -> None:
    """The accounts job's matrix is one `account` list of distinct positive integers and never fails fast; an include
    or exclude could add, drop or rewrite a leg."""
    strategy = job.get("strategy")
    if not isinstance(strategy, dict) or strategy.get("fail-fast") != "false":
        raise ValueError(
            "daily-run accounts job must set strategy fail-fast: false, so one leg cannot cancel the others"
        )
    matrix = strategy.get("matrix")
    accounts = matrix.get("account") if isinstance(matrix, dict) and set(matrix) == {"account"} else None
    if (
        not isinstance(accounts, list)
        or not accounts
        or not all(isinstance(number, str) and re.fullmatch(r"[1-9][0-9]*", number) for number in accounts)
        or len(set(accounts)) != len(accounts)
    ):
        raise ValueError("daily-run accounts matrix must be only `account:` listing distinct positive account numbers")


def check_daily_accounts_job(job: dict) -> None:
    _check_matrix(job)
    if SECRETS_EXPRESSION_RE.search(json.dumps(job.get("env", {}))):
        raise ValueError("daily-run: only the account step may read secrets, not the env of the accounts job")
    steps = job.get("steps", [])
    account_steps = []
    for index, step in enumerate(steps):
        text = json.dumps(step)
        mentions = len(ACCOUNT_SECRET_MENTION_RE.findall(text))
        if mentions:
            # A literal secrets.PSS_ACCOUNT_<N>_ would hand every leg the same account's credentials.
            if len(MATRIX_ACCOUNT_SECRET_RE.findall(text)) != mentions:
                raise ValueError(
                    "daily-run reads account secrets only as secrets[format('PSS_ACCOUNT_{0}_<NAME>', matrix.account)] "
                    f"({step.get('name', index)})"
                )
            account_steps.append(index)
        elif SECRETS_EXPRESSION_RE.search(text):
            raise ValueError(f"daily-run: only the account step may read secrets ({step.get('name', index)})")
    if len(account_steps) != 1:
        raise ValueError("daily-run accounts job needs exactly one account step reading the leg's account secrets")
    (account_index,) = account_steps
    account = steps[account_index]
    if _condition(account.get("if")) != _condition(LEGACY_RUN_SKIP_CONDITION):
        raise ValueError("daily-run account step must be skippable through LEGACY_RUN_SKIP for its matrix.account")
    run = _text(account.get("run"))
    if ["".join(groups) for groups in ACCOUNT_ALIAS_ARG_RE.findall(run)] != [DAILY_ACCOUNT_ALIAS]:
        raise ValueError(f"daily-run account step must pass --account-alias {DAILY_ACCOUNT_ALIAS} exactly once")
    env = account.get("env")
    name, value = DAILY_ACCOUNT_NUMBER_ENV
    if not isinstance(env, dict) or _text(env.get(name)).strip() != value:
        raise ValueError(f"daily-run account step must set {name}: {value} in its env, for its alias")
    if run.count(DAILY_TRAINING_JOURNAL_ARG) != 1:
        raise ValueError(f"daily-run account step must journal training with {DAILY_TRAINING_JOURNAL_ARG} exactly once")
    if account.get("continue-on-error") != "true":
        raise ValueError(
            "daily-run account step must keep continue-on-error: true, so one account's failure cannot fail the run"
        )
    uploads = [
        index for index, step in enumerate(steps) if _text(step.get("uses")).startswith("actions/upload-artifact@")
    ]
    if len(uploads) != 1:
        raise ValueError("daily-run accounts job needs exactly one artifact upload step, for the leg's journal")
    (upload_index,) = uploads
    if upload_index < account_index:
        raise ValueError("daily-run journal upload step must come after the account step")
    _check_upload(
        steps[upload_index],
        "journal upload",
        path=DAILY_JOURNALS_ARTIFACT_PATH,
        keys=DAILY_JOURNAL_UPLOAD_KEYS,
    )
    inputs = steps[upload_index]["with"]
    # One artifact name per leg, so the legs do not collide, and overwrite: true, so a re-run leg replaces its journal
    # instead of failing to upload it.
    if _text(inputs.get("name")).strip() != DAILY_JOURNAL_ARTIFACT:
        raise ValueError(f"daily-run journal upload step must be named {DAILY_JOURNAL_ARTIFACT}")
    if inputs.get("overwrite") != "true":
        raise ValueError("daily-run journal upload step must set overwrite: true, so a re-run leg replaces its journal")


def _check_upload(step: dict, kind: str, *, path: str, keys: frozenset) -> None:
    if _condition(step.get("if")) != "always()":
        raise ValueError(f"the daily-run {kind} step must run with if: always()")
    if step.get("continue-on-error") != "true":
        raise ValueError(
            f"the daily-run {kind} step must keep continue-on-error: true, so an artifact service failure "
            "cannot fail the daily run"
        )
    inputs = step.get("with")
    if not isinstance(inputs, dict) or _text(inputs.get("path")).strip() != path:
        raise ValueError(f"the daily-run {kind} step must upload only {path}")
    if not set(inputs) <= keys:
        raise ValueError(f"unexpected {kind} inputs in daily-run: {sorted(set(inputs) - keys)}")
    retention = _text(inputs.get("retention-days"))
    if not (re.fullmatch(r"[0-9]+", retention) and 1 <= int(retention) <= MAX_DAILY_TRAINING_RETENTION_DAYS):
        raise ValueError(f"the daily-run {kind} needs retention-days between 1 and {MAX_DAILY_TRAINING_RETENTION_DAYS}")


def check_daily_report_job(job: dict) -> None:
    """The training-report job waits for every leg, runs even when one failed, and never sees a secret."""
    if SECRETS_EXPRESSION_RE.search(json.dumps(job)):
        raise ValueError("daily-run: only the account step may read secrets, not the training-report job")
    needs = job.get("needs")
    if needs not in (DAILY_ACCOUNTS_JOB, [DAILY_ACCOUNTS_JOB]):
        raise ValueError(
            f"the daily-run training-report job must need {DAILY_ACCOUNTS_JOB}, so it runs after every leg"
        )
    if _condition(job.get("if")) != "always()":
        raise ValueError("the daily-run training-report job must run with if: always(), also when a leg failed")
    steps = job.get("steps", [])

    def find(marker) -> list[int]:
        return [index for index, step in enumerate(steps) if marker(step)]

    downloads = find(lambda step: _text(step.get("uses")).startswith("actions/download-artifact@"))
    reports = find(lambda step: DAILY_TRAINING_REPORT_COMMAND in _text(step.get("run")))
    uploads = find(lambda step: _text(step.get("uses")).startswith("actions/upload-artifact@"))
    if len(downloads) != 1:
        raise ValueError("the daily-run training-report job needs exactly one download step, for the legs' journals")
    if len(reports) != 1:
        raise ValueError("daily-run needs exactly one training report step")
    if len(uploads) != 1:
        raise ValueError("the daily-run training-report job needs exactly one artifact upload step")
    ((download_index,), (report_index,), (upload_index,)) = (downloads, reports, uploads)
    download = steps[download_index].get("with")
    # No name, run-id or token: only this run's legs' journals, all merged into the directory the report reads.
    if (
        not isinstance(download, dict)
        or set(download) != DAILY_JOURNAL_DOWNLOAD_KEYS
        or _text(download.get("pattern")).strip() != DAILY_JOURNAL_ARTIFACT_PATTERN
        or _text(download.get("path")).strip() != DAILY_JOURNALS_ARTIFACT_PATH
        or download.get("merge-multiple") != "true"
    ):
        raise ValueError(
            f"the daily-run download step must merge only {DAILY_JOURNAL_ARTIFACT_PATTERN} into "
            f"{DAILY_JOURNALS_ARTIFACT_PATH}"
        )
    report = steps[report_index]
    if _condition(report.get("if")) != "always()":
        raise ValueError("the daily-run training report step must run with if: always()")
    if report_index < download_index:
        raise ValueError("the daily-run training report step must come after the download step")
    if "continue-on-error" in report:
        raise ValueError("the daily-run training report step must not carry continue-on-error")
    run = _text(report.get("run"))
    if "set -euo pipefail" not in run:
        raise ValueError("the daily-run training report step must run under set -euo pipefail")
    if SWALLOWING_SHELL_RE.search(run):
        raise ValueError("the daily-run training report step must not swallow a failing exit status")
    if '>> "$GITHUB_STEP_SUMMARY"' not in run:
        raise ValueError("the daily-run training report step must append the report to the job summary")
    if "--report-format json" not in run:
        raise ValueError("the daily-run training report step must also write the JSON report")
    if DAILY_TRAINING_DIR not in run:
        raise ValueError(f"the daily-run training report step must read the journals under {DAILY_TRAINING_DIR}")
    if any(flag in run for flag in ("--login-email", "--password-file", "--auth-file", "--smtp-")):
        raise ValueError("the daily-run training report step must not be given credentials")
    if upload_index < report_index:
        raise ValueError("the daily-run training upload step must come after the training report step")
    _check_upload(
        steps[upload_index], "training upload", path=DAILY_TRAINING_ARTIFACT_PATH, keys=DAILY_TRAINING_UPLOAD_KEYS
    )


def check_mutation_gate(documents: dict) -> None:
    """python-tests.yml's mutation-gate job runs the sdk/training/ targets and can fail `CI required`."""
    gate = documents["python-tests.yml"].get("jobs", {}).get(MUTATION_GATE_JOB)
    if not isinstance(gate, dict):
        raise ValueError(f"python-tests.yml needs the {MUTATION_GATE_JOB} job")
    if gate.get("if") != "${{ inputs.run-mutation }}":
        raise ValueError(f"the {MUTATION_GATE_JOB} job must run when inputs.run-mutation is set")
    if "continue-on-error" in gate:
        raise ValueError(f"the {MUTATION_GATE_JOB} job must not carry continue-on-error")
    steps = gate.get("steps", [])
    mutation_steps = [step for step in steps if "scripts/mutation.py" in _text(step.get("run"))]
    if len(mutation_steps) != 1:
        raise ValueError(f"the {MUTATION_GATE_JOB} job must run scripts/mutation.py in exactly one step")
    (step,) = mutation_steps
    if "continue-on-error" in step:
        raise ValueError(f"the {MUTATION_GATE_JOB} step must not carry continue-on-error")
    if "if" in step:
        raise ValueError(f"the {MUTATION_GATE_JOB} step must not carry its own if: condition")
    run = _text(step.get("run"))
    for required in ("set -euo pipefail", MUTATION_GATE_COMMAND, '--config "$config"'):
        if required not in run:
            raise ValueError(f"the {MUTATION_GATE_JOB} step must run {required}")
    if SWALLOWING_SHELL_RE.search(run):
        raise ValueError(f"the {MUTATION_GATE_JOB} step must not swallow a failing exit status")
    check_mutation_gate_command(run)
    for other in steps:
        if "continue-on-error" in other and not _text(other.get("uses")).startswith("actions/upload-artifact@"):
            raise ValueError(f"in the {MUTATION_GATE_JOB} job only its upload step may carry continue-on-error")
        if other is not step and "run" in other and _text(other.get("run")).strip() not in MUTATION_GATE_SETUP_RUNS:
            raise ValueError(
                f"the {MUTATION_GATE_JOB} job may run only {'; '.join(MUTATION_GATE_SETUP_RUNS)} besides scripts/mutation.py"
            )
        if "uses" in other and not _text(other.get("uses")).startswith(MUTATION_GATE_ACTIONS):
            raise ValueError(f"the {MUTATION_GATE_JOB} job may use only {', '.join(MUTATION_GATE_ACTIONS)}")
    if documents["blocking-ci.yml"].get("jobs", {}).get("tests", {}).get("with", {}).get("run-mutation") != "true":
        raise ValueError(f"Blocking CI must run the {MUTATION_GATE_JOB} job (run-mutation: true)")


def check_mutation_gate_command(run: str) -> None:
    """The gate step is MUTATION_GATE_PRELUDE then one plain scripts/mutation.py command on the checked config, so
    that command's exit status is the step's, and it takes no flag that narrows or skips the blocking targets
    (--target, --only, --operators, --min-score, --list...)."""
    lines = [line.strip() for line in run.splitlines() if line.strip()]
    if tuple(lines[:-1]) != MUTATION_GATE_PRELUDE:
        raise ValueError(
            f"the {MUTATION_GATE_JOB} step must run exactly {'; '.join(MUTATION_GATE_PRELUDE)} before scripts/mutation.py"
        )
    try:
        words = shlex.split(lines[-1]) if lines else []
    except ValueError:
        words = []
    if tuple(words[:2]) != MUTATION_GATE_RUNNER or any(set(word) & set(";&|<>`") for word in words):
        raise ValueError(f"the {MUTATION_GATE_JOB} step must end in one plain {' '.join(MUTATION_GATE_RUNNER)} command")
    pairs = list(zip(words, words[1:]))
    if pairs.count(("--config", "$config")) != 1 or sum(word.startswith("--config") for word in words) != 1:
        raise ValueError(f'the {MUTATION_GATE_JOB} mutation command must read --config "$config" once')
    flags = {word.split("=", 1)[0] for word in words[2:] if word.startswith("-")}
    if not flags <= MUTATION_GATE_ALLOWED_FLAGS:
        raise ValueError(
            f"the {MUTATION_GATE_JOB} mutation command takes only {', '.join(sorted(MUTATION_GATE_ALLOWED_FLAGS))}, "
            f"not {', '.join(sorted(flags - MUTATION_GATE_ALLOWED_FLAGS))}"
        )


def is_blocking_mutation_target(path: str) -> bool:
    return path.startswith(BLOCKING_MUTATION_PREFIX)


def mutation_config_subset(config: dict, *, blocking: bool) -> dict:
    """A copy of the mutation config holding only its blocking (sdk/training/) or only its advisory targets."""
    targets = [
        target
        for target in mutation_targets(config)
        if is_blocking_mutation_target(_text(target.get("path"))) == blocking
    ]
    if not targets:
        raise ValueError(f"the mutation config has no {'blocking' if blocking else 'advisory'} targets")
    return {**config, "targets": targets}


def mutation_targets(config: object) -> list[dict]:
    """The config's target objects; a config that is not an object holding a list of objects is a ValueError."""
    targets = config.get("targets") if isinstance(config, dict) else None
    if not isinstance(targets, list) or not all(isinstance(target, dict) for target in targets):
        raise ValueError("the mutation config needs a targets list of objects")
    for target in targets:
        check_mutation_target_path(target.get("path"))
    return targets


def check_mutation_target_path(path: object) -> None:
    """A target path must be written the way scripts/mutation.py normalizes it, and a glob must not be able to reach
    sdk/training/: otherwise "./sdk/training/x.py" or "sdk/*/x.py" would be classed advisory here yet mutate a
    training file, dodging the blocking floor. Non-text paths are left to scripts/mutation.py to refuse."""
    if not isinstance(path, str):
        return
    canonical = PurePosixPath(path.replace("\\", "/")).as_posix()
    if canonical != path:
        raise ValueError(f"mutation target {path!r} must be written as {canonical!r}")
    if not any(character in path for character in "*?["):
        return
    literal = []
    for part in PurePosixPath(path).parts:
        if any(character in part for character in "*?["):
            break
        literal.append(part)
    prefix = "/".join(literal)
    blocking_dir = BLOCKING_MUTATION_PREFIX.rstrip("/")
    if (
        not prefix
        or blocking_dir == prefix
        or blocking_dir.startswith(f"{prefix}/")
        or prefix.startswith(f"{blocking_dir}/")
    ):
        raise ValueError(
            f"a glob mutation target must not reach {BLOCKING_MUTATION_PREFIX}: list each training file on its own "
            f"line instead of {path!r}"
        )


def check_mutation_config(config: dict) -> None:
    """Every blocking (sdk/training/) target, the four required ones and any other, keeps the floor, runs some tests
    and mutates its whole file. The four required targets cannot be dropped, renamed or unmapped from the training
    test modules. Every entry is checked, not only the last for a path."""
    targets = mutation_targets(config)
    for target in targets:
        path = _text(target.get("path"))
        if is_blocking_mutation_target(path):
            check_blocking_mutation_target(path, target)
    for path in REQUIRED_BLOCKING_MUTATION_TARGETS:
        entries = [target for target in targets if target.get("path") == path]
        if not entries:
            raise ValueError(f"the mutation config is missing the blocking target {path}")
        for target in entries:
            if not set(BLOCKING_MUTATION_TESTS) <= set(target["tests"]):  # a list of text: checked above
                raise ValueError(f"mutation target {path} must run {' and '.join(BLOCKING_MUTATION_TESTS)}")


def check_blocking_mutation_target(path: str, target: dict) -> None:
    weakening = [key for key in WEAKENING_MUTATION_TARGET_KEYS if key in target]
    if weakening:
        raise ValueError(
            f"mutation target {path} must mutate the whole file with the baseline's timeout, without "
            f"{' or '.join(weakening)}"
        )
    tests = target.get("tests")
    if not isinstance(tests, list) or not tests or not all(isinstance(test, str) and test.strip() for test in tests):
        raise ValueError(f"mutation target {path} needs a non-empty tests list of module names")
    score = target.get("min_score")
    # isfinite also refuses NaN (json.loads accepts it and it compares false with everything) and infinity.
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or score < MIN_BLOCKING_MUTATION_SCORE
    ):
        raise ValueError(f"mutation target {path} needs a numeric min_score of at least {MIN_BLOCKING_MUTATION_SCORE}")


def check_mutation_policy(repo: Path) -> None:
    check_mutation_config(json.loads((repo / MUTATION_CONFIG).read_text(encoding="utf-8")))


def check_repository(repo: Path) -> None:
    documents = workflow_documents(repo)
    check_workflows(documents)
    check_serialized_legacy_run(documents, serialized_authorizations(repo))
    check_mutation_policy(repo)
    paths = tracked_files(repo)
    for relative in paths:
        path = repo / relative
        if path.lstat().st_size > MAX_BLOB_BYTES:
            raise ValueError(f"tracked file exceeds the 1 MiB blob policy: {relative}")
        if path.suffix == ".py" and not path.is_symlink():
            compile(path.read_bytes(), str(relative), "exec")
    git(repo, "diff", "--check")
    if git(repo, "rev-list", "--max-parents=0", "HEAD").strip() != git(repo, "rev-parse", "HEAD").strip():
        git(repo, "diff", "--check", "HEAD^", "HEAD")
    else:
        git(repo, "show", "--check", "--format=", "HEAD")
    check_hollow_tests(repo, paths)
    print("Workflow policies, tracked-file syntax, blob sizes, whitespace and hollow tests passed.")


def release_identity(repo: Path, tag: str, main_ref: str) -> tuple[str, str, int]:
    match = VERSION_RE.fullmatch(tag)
    if not match:
        raise ValueError("release tag must be X.Y.Z or vX.Y.Z without leading zeroes")
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    if git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}").decode().strip() != commit:
        raise ValueError("release tag does not identify the checked-out commit")
    git(repo, "merge-base", "--is-ancestor", commit, main_ref)
    epoch = int(git(repo, "show", "-s", "--format=%ct", commit))
    return match[1], commit, epoch


def release_includes(path: str) -> bool:
    """Select a tracked path for the source archive, refusing sdk/ data that looks private."""
    pure = PurePosixPath(path)
    if path in RELEASE_ROOT_FILES:
        return True
    if pure.parts[0] != "sdk" or RELEASE_EXCLUDED_DIRS.intersection(pure.parts[:-1]):
        return False
    if pure.suffix == ".py":
        return True
    if pure.suffix not in RELEASE_SDK_DATA_SUFFIXES:
        return False
    words = set(re.split(r"[^a-z0-9]+", path.lower()))
    if words & RELEASE_PRIVATE_WORDS or any(part.startswith(".") for part in pure.parts):
        raise ValueError(f"release refuses package data that looks like a secret, log or capture: {path}")
    return True


def build_release(repo: Path, tag: str, output: Path, main_ref: str = "origin/main") -> dict:
    """Build from Git objects, never from dirty files, logs or local credentials."""
    version, commit, epoch = release_identity(repo, tag, main_ref)
    tree = git(repo, "ls-tree", "-rz", commit)
    members = []
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        mode, kind, object_id = meta.split()
        path = os.fsdecode(name)
        pure = PurePosixPath(path)
        if not release_includes(path):
            continue
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError(f"release source must be a regular file: {path}")
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError("unsafe archive member")
        contents = git(repo, "cat-file", "blob", object_id.decode())
        if pure.parts[0] == "sdk" and pure.suffix == ".json":
            try:
                json.loads(contents.decode("utf-8"))
            except ValueError:
                raise ValueError(f"release package data is not valid UTF-8 JSON: {path}") from None
        members.append((path, mode, contents))
    names = {path for path, _, _ in members}
    if not set(RELEASE_ROOT_FILES) <= names or "sdk/client.py" not in names:
        raise ValueError("release is missing required application source files")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"tachikoma-{version}.tar.gz"
    with archive.open("wb") as stream:
        with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as package:
                for path, mode, contents in sorted(members):
                    info = tarfile.TarInfo(f"tachikoma-{version}/{path}")
                    info.size, info.mtime = len(contents), epoch
                    info.mode = 0o755 if mode == b"100755" else 0o644
                    package.addfile(info, io.BytesIO(contents))
    manifest = {"version": version, "tag": tag, "commit": commit, "source_date_epoch": epoch, "files": len(members)}
    manifest_path = output / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksums = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in (archive, manifest_path)
    )
    (output / "SHA256SUMS").write_text(checksums, encoding="utf-8", newline="\n")
    return manifest


def close_root_log_files_under(directory: Path) -> None:
    """Close and detach root-logger file handlers whose file lies inside ``directory``."""
    directory = Path(os.path.realpath(directory))
    for handler in list(logging.root.handlers):
        if isinstance(handler, logging.FileHandler):
            if Path(os.path.realpath(handler.baseFilename)).is_relative_to(directory):
                logging.root.removeHandler(handler)
                handler.close()


def in_bazel_test_tmpdir() -> bool:
    """True when the working directory is already the scratch directory Bazel made for this test."""
    test_tmpdir = os.environ.get("TEST_TMPDIR")
    if not test_tmpdir:
        return False
    try:
        return os.path.samefile(os.getcwd(), test_tmpdir)
    except OSError:
        return False


def run_tests(names: list[str], require_tests: bool = False, verbosity: int = 1) -> int:
    """Run mocked tests and fail even when a forbidden network error is swallowed."""
    attempts = []

    def forbid_network(event, arguments):
        if event in {"socket.connect", "http.client.connect"}:
            attempts.append(event)
            raise RuntimeError("CI tests must mock network traffic")

    # Audit hooks cannot be removed; this command deliberately owns its interpreter.
    sys.addaudithook(forbid_network)
    sys.path.insert(0, str(ROOT))
    # Tests that construct Device() or import run.py write ./.device and ./tachikoma.log. Running them in a
    # fresh, empty working directory keeps them away from the real credential files in the repository root.
    # Under Bazel the working directory is already the per-test $TEST_TMPDIR, so it is used as it is.
    previous_cwd = os.getcwd()
    workdir = (
        nullcontext(None)
        if in_bazel_test_tmpdir()
        else TemporaryDirectory(prefix="tachikoma-test-", ignore_cleanup_errors=True)
    )
    with workdir as scratch:
        try:
            if scratch is not None:
                os.chdir(scratch)
            suite = (
                unittest.defaultTestLoader.loadTestsFromNames(names)
                if names
                else unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
            )
            result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
        finally:
            # Leave the directory and close log files opened in it (run.py logs to ./tachikoma.log) before it is
            # removed: Windows cannot delete the working directory or an open file.
            os.chdir(previous_cwd)
            if scratch is not None:
                close_root_log_files_under(Path(scratch))
    if attempts:
        print(f"CI rejected {len(attempts)} unmocked network connection attempt(s).", file=sys.stderr)
    if require_tests and result.testsRun == 0:
        print("CI ran no tests, but at least one was required.", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() and not attempts else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("gate")
    gate.add_argument("--required", nargs="+", required=True)
    commands.add_parser("lint")
    commands.add_parser("hollow-tests")
    commands.add_parser("repository")
    tests = commands.add_parser("test")
    tests.add_argument("--names", nargs="+", default=[])
    tests.add_argument("--verbose", action="store_true", help="print every test id (unittest verbosity 2)")
    release = commands.add_parser("release")
    release.add_argument("--tag", required=True)
    release.add_argument("--main-ref", default="origin/main")
    release.add_argument("--output", type=Path, default=Path("dist"))
    mutation_config = commands.add_parser(
        "mutation-config", help="print the blocking (sdk/training/) or the advisory part of the mutation config"
    )
    selector = mutation_config.add_mutually_exclusive_group(required=True)
    selector.add_argument("--blocking", action="store_true")
    selector.add_argument("--advisory", action="store_true")
    mutation_config.add_argument("--config", type=Path, default=ROOT / MUTATION_CONFIG)
    args = parser.parse_args(argv)
    try:
        if args.command == "gate":
            require_success(json.loads(os.environ.get("NEEDS_JSON", "null")), args.required)
            print("Every required workflow succeeded.")
        elif args.command == "lint":
            check_lint(ROOT)
        elif args.command == "hollow-tests":
            check_hollow_tests(ROOT)
        elif args.command == "repository":
            check_repository(ROOT)
        elif args.command == "test":
            return run_tests(args.names, verbosity=2 if args.verbose else 1)
        elif args.command == "mutation-config":
            # A JSONDecodeError is a ValueError, so an unreadable config fails here like a weakened one.
            config = json.loads(args.config.read_text(encoding="utf-8"))
            if args.blocking:  # the training policy belongs to the gate; the advisory job measures the rest regardless
                check_mutation_config(config)
            print(json.dumps(mutation_config_subset(config, blocking=args.blocking), indent=2))
        else:
            print(json.dumps(build_release(ROOT, args.tag, args.output, args.main_ref), sort_keys=True))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"CI check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
