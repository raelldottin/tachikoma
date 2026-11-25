"""Mutation testing for sdk modules: stdlib-ast mutants run against mapped tests in temporary repository copies.

Each target module in automation/mutation/config.json is parsed with ast, and small faults (mutants) are generated:
flipped comparisons, shifted integer boundaries, negated conditions, swapped and/or, changed constants, stripped
"@" from xmltodict attribute keys, replaced return values and deleted statements. Every mutant is applied by
replacing one exact source span, so the rest of the file stays byte for byte identical. The mapped tests then run
once per mutant through scripts/ci.py test inside a temporary copy of the repository; a mutant that the tests do not
detect "survives". The score is (killed + timeout) / (total - incompetent - equivalent), and a target fails when the
score is below its recorded min_score.

The working tree is never written: every copy lives under a mkdtemp scratch directory that is removed afterwards,
also on errors, Ctrl-C and SIGTERM. Known limitation: if this process is SIGKILLed, test processes started in their
own sessions are orphaned, and an infinite-loop mutant keeps running until it is killed by hand.

A `# pragma: no mutate` comment excludes every mutant whose source span covers its line. Docstrings, f-strings,
annotations, the `__main__` guard and the arguments of `Literal[...]` are never mutated.

A target can restrict mutation to some functions ("functions": qualnames such as Class.method, nested scopes
included) and choose operators ("operators", or --operators), including the opt-in arithmetic and minmax operators
that the default set leaves out. "aggregate": true on a glob target such as sdk/training/**/*.py gates the glob's
combined score instead of each file's; a qualname in "functions" of a glob target must match in at least one of its
files. A complete, passing run at least two points above min_score, with the configured tests, operators, minimum
and timeout, prints a `ratchet:` line with the floor to record, and --summary appends a Markdown report (for
$GITHUB_STEP_SUMMARY). --target names one module or a configured glob. An --only run, like a --target run of one
file of an aggregate glob, is partial: it is gated only against an explicit --min-score. A glob member, or a run
whose operators leave out a default one, with nothing to mutate is reported as skipped, not an error. Without a
timeout, each mutant run gets 5x the baseline's seconds times the number of parallel runs (at least 10 seconds).
Reports record the operators used and any command-line overrides.
"""

from __future__ import annotations

import argparse
import ast
import bisect
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
import difflib
import io
import json
import math
import os
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import warnings

ROOT = Path(os.path.abspath(__file__)).parent.parent
DEFAULT_CONFIG = "automation/mutation/config.json"
DEFAULT_COPY_PATHS = ("sdk", "tests", "scripts/ci.py")
OPERATORS = ("compare", "boundary", "negate", "boolop", "constant", "strip_at", "return", "delete")
# Opt-in operators (a target's "operators" key, --operators): not in the default set, so recorded floors stay valid.
EXTRA_OPERATORS = ("arithmetic", "minmax")
ALL_OPERATORS = OPERATORS + EXTRA_OPERATORS
STATUSES = ("killed", "survived", "timeout", "incompetent", "equivalent")
RUN_STATUSES = {"passed": "survived", "failed": "killed", "timeout": "timeout"}
BASELINE_TIMEOUT = 300.0
MIN_TIMEOUT, TIMEOUT_FACTOR = 10.0, 5.0
KILL_GRACE = 10.0
RATCHET_MARGIN = 0.02
CHILD_TMP = ".mutation-tmp"
OUTPUT_TAIL = 4000
CHILD_ENV_DROPPED = (
    "PYTHONPATH",
    "PYTHONSAFEPATH",
    "TEST_TMPDIR",
    "PYTHONPYCACHEPREFIX",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
)
PRAGMA_RE = re.compile(r"#\s*pragma:\s*no\s+mutate\b")
RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
AT_STRING_RE = re.compile(r"""^(["'])@(.*)\1$""", re.DOTALL)
TEST_MODULE_RE = re.compile(r"^[A-Za-z_][\w.]*$")
MUTANT_ID_RE = re.compile(rf"^(?P<path>.+):\d+:\d+:(?:{'|'.join(ALL_OPERATORS)}):\d+$")
QUALNAME_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
GLOB_CHARACTERS = frozenset("*?[")
# The first argument of these calls is a type expression or a type's name, not runtime logic.
TYPING_HELPERS = frozenset({"cast", "TypeVar", "NewType"})
TYPING_MODULES = frozenset({"typing", "typing_extensions", "t"})

COMPARE_FLIPS: dict[type[ast.cmpop], str] = {
    ast.Lt: ">=",
    ast.GtE: "<",
    ast.Gt: "<=",
    ast.LtE: ">",
    ast.Eq: "!=",
    ast.NotEq: "==",
    ast.Is: "is not",
    ast.IsNot: "is",
    ast.In: "not in",
    ast.NotIn: "in",
}
ARITHMETIC_SWAPS: dict[type[ast.operator], str] = {
    ast.Add: "-",
    ast.Sub: "+",
    ast.Mult: "/",
    ast.Div: "*",
    ast.FloorDiv: "/",
    ast.Mod: "*",
    ast.Pow: "*",
}
MINMAX_SWAPS = {"min": "max", "max": "min"}
ATOMIC_TESTS = (ast.Name, ast.Attribute, ast.Call, ast.Subscript, ast.Constant)
DATA_TABLE_VALUES = (ast.Dict, ast.List, ast.Tuple, ast.Set)
DELETABLE_STATEMENTS = (ast.Assign, ast.AugAssign, ast.Raise)
FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef)
NAMED_SCOPES = (*FUNCTION_SCOPES, ast.ClassDef)
Located = ast.expr | ast.stmt


class MutationError(Exception):
    """A problem that stops a mutation run before it can produce a score (exit 2)."""


class ConfigError(MutationError, ValueError):
    """Invalid configuration, target, test module or source file."""


class BaselineError(MutationError):
    """The mapped tests do not pass (or run nothing) on the unmutated source, so no score can be measured."""


@dataclass(frozen=True)
class Mutant:
    """One fault: replace source[(line, col):(end_line, end_col)], which must equal original, with replacement.

    Lines are 1-based and columns are 0-based character offsets. scope is the qualname of the innermost enclosing
    def or class, nested names joined by "." (`Class.method`, `outer.inner`); "" at module level.
    """

    path: str
    line: int
    col: int
    end_line: int
    end_col: int
    operator: str
    index: int
    original: str
    replacement: str
    scope: str = ""

    @property
    def id(self) -> str:
        return f"{self.path}:{self.line}:{self.col}:{self.operator}:{self.index}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "end_col": self.end_col,
            "operator": self.operator,
            "index": self.index,
            "original": self.original,
            "replacement": self.replacement,
            "scope": self.scope,
        }


def source_lines(source: str) -> list[str]:
    """Split like ast and tokenize do: on \\n, \\r\\n and \\r only, keeping the line endings."""
    return io.StringIO(source, newline="").readlines()


def line_offsets(lines: list[str]) -> list[int]:
    """offsets[i] is the character offset where line i + 1 starts; the last entry is the source length."""
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets


def read_source(path: Path) -> str:
    """Read a UTF-8 source file without newline translation; a BOM or invalid UTF-8 is a ConfigError."""
    data = Path(path).read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        raise ConfigError(f"{path}: a UTF-8 byte order mark is not supported")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigError(f"{path}: not valid UTF-8 ({error})") from None


def int_literal_value(node: ast.AST) -> int | None:
    """The value of an int literal such as 5 or -5 (not a bool); None for any other node."""
    sign, inner = 1, node
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        sign, inner = -1, node.operand
    if isinstance(inner, ast.Constant) and type(inner.value) is int:
        return sign * inner.value
    return None


def is_main_guard(node: ast.stmt) -> bool:
    """A top-level `if __name__ == "__main__":`, with the operands in either order."""
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    operands = (test.left, test.comparators[0])
    has_name = any(isinstance(item, ast.Name) and item.id == "__name__" for item in operands)
    has_main = any(isinstance(item, ast.Constant) and item.value == "__main__" for item in operands)
    return has_name and has_main


def is_literal_type(node: ast.expr) -> bool:
    """`Literal`, `typing.Literal`, `t.Literal` and the like: the subscripted name of a typing.Literal type."""
    return (isinstance(node, ast.Name) and node.id == "Literal") or (
        isinstance(node, ast.Attribute) and node.attr == "Literal"
    )


def is_typing_helper(node: ast.expr) -> bool:
    """`cast`, `TypeVar` or `NewType`, bare or as an attribute of typing, typing_extensions or t."""
    if isinstance(node, ast.Name):
        return node.id in TYPING_HELPERS
    return (
        isinstance(node, ast.Attribute)
        and node.attr in TYPING_HELPERS
        and isinstance(node.value, ast.Name)
        and node.value.id in TYPING_MODULES
    )


def constant_replacement(value: object) -> str | None:
    if isinstance(value, bool):
        return str(not value)
    if type(value) is int:
        return "1" if value == 0 else str(value + 1)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        # From 2**53 on, value + 1.0 == value, which would be an equivalent mutant that always survives.
        return repr(value + 1.0 if value + 1.0 != value else value / 2)
    if isinstance(value, str) and value:
        return '""'
    if isinstance(value, bytes) and value:
        return 'b""'
    return None


def strip_at_replacement(text: str, value: str) -> str:
    """The literal without its leading "@", in the same quote style when that is safe, else repr(value[1:])."""
    match = AT_STRING_RE.match(text)
    if match and "\n" not in text and "\r" not in text:
        candidate = f"{match.group(1)}{match.group(2)}{match.group(1)}"
        try:
            if ast.literal_eval(candidate) == value[1:]:
                return candidate
        except (SyntaxError, ValueError):
            pass
    return repr(value[1:])


class _Collector:
    """Walks one parsed module and collects candidate mutants as (line, col, end_line, end_col, op, repl, scope)."""

    def __init__(self, source: str, tree: ast.Module, mutate_data_tables: bool):
        self.source = source
        self.lines = source_lines(source)
        self.offsets = line_offsets(self.lines)
        self.tree = tree
        self.mutate_data_tables = mutate_data_tables
        self.candidates: list[tuple[int, int, int, int, str, str, str]] = []
        self.scope = ""
        self.skipped: set[int] = set()
        self.tables: set[int] = set()
        self.owned: set[int] = set()
        tokens = list(tokenize.generate_tokens(io.StringIO(source, newline="").readline))
        self.tokens = [token for token in tokens if token.type in (tokenize.NAME, tokenize.OP)]
        self.token_starts = [token.start for token in self.tokens]
        self.pragma_lines = {
            token.start[0] for token in tokens if token.type == tokenize.COMMENT and PRAGMA_RE.search(token.string)
        }

    # Positions -----------------------------------------------------------------------------------------------
    def char_col(self, line: int, byte_col: int) -> int:
        return len(self.lines[line - 1].encode("utf-8")[:byte_col].decode("utf-8"))

    def start(self, node: Located) -> tuple[int, int]:
        return node.lineno, self.char_col(node.lineno, node.col_offset)

    def end(self, node: Located) -> tuple[int, int]:
        end_line = node.end_lineno if node.end_lineno is not None else node.lineno
        end_col = node.end_col_offset if node.end_col_offset is not None else node.col_offset
        return end_line, self.char_col(end_line, end_col)

    def text(self, start: tuple[int, int], end: tuple[int, int]) -> str:
        return self.source[self.offsets[start[0] - 1] + start[1] : self.offsets[end[0] - 1] + end[1]]

    def node_text(self, node: Located) -> str:
        return self.text(self.start(node), self.end(node))

    def add(self, operator: str, start: tuple[int, int], end: tuple[int, int], replacement: str) -> None:
        self.candidates.append((start[0], start[1], end[0], end[1], operator, replacement, self.scope))

    def add_node(self, operator: str, node: Located, replacement: str) -> None:
        self.add(operator, self.start(node), self.end(node), replacement)

    def operator_tokens(self, left: ast.expr, right: ast.expr) -> tuple[tuple[int, int], tuple[int, int]] | None:
        """Span of the operator token(s) between two operands, ignoring parentheses and comments."""
        gap_start, gap_end = self.end(left), self.start(right)
        found = []
        for position in range(bisect.bisect_left(self.token_starts, gap_start), len(self.tokens)):
            token = self.tokens[position]
            if token.start >= gap_end:
                break
            if token.string not in ("(", ")"):
                found.append(token)
        if not found:
            return None
        return found[0].start, found[-1].end

    # Skipped contexts --------------------------------------------------------------------------------------
    def mark(self, node: ast.AST | None) -> None:
        if node is not None:
            self.skipped.update(id(child) for child in ast.walk(node))

    def mark_skipped_contexts(self) -> None:
        for statement in self.tree.body:
            if is_main_guard(statement):
                self.mark(statement)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)) and isinstance(statement.value, DATA_TABLE_VALUES):
                self.tables.update(id(child) for child in ast.walk(statement.value))
        for node in ast.walk(self.tree):
            if isinstance(node, ast.JoinedStr):
                self.mark(node)
            elif isinstance(node, ast.arg):
                self.mark(node.annotation)
            elif isinstance(node, FUNCTION_SCOPES):
                self.mark(node.returns)
            elif isinstance(node, ast.AnnAssign):
                self.mark(node.annotation)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                self.mark(node.value)
            elif isinstance(node, ast.Subscript) and is_literal_type(node.value):
                # Literal["a", "b"] is typing (often a module-level alias), not runtime logic.
                self.mark(node.slice)
            elif isinstance(node, ast.Call) and node.args and is_typing_helper(node.func):
                # cast("Foo", x), TypeVar("T") and NewType("UserId", int): a type expression or a type's name.
                self.mark(node.args[0])

    # Traversal ---------------------------------------------------------------------------------------------
    def collect(self) -> list[tuple[int, int, int, int, str, str, str]]:
        self.mark_skipped_contexts()
        self.visit(self.tree, in_function=False)
        return [
            candidate
            for candidate in self.candidates
            if not any(line in self.pragma_lines for line in range(candidate[0], candidate[2] + 1))
        ]

    def visit(self, node: ast.AST, in_function: bool) -> None:
        if id(node) in self.skipped:
            return
        deletable = isinstance(node, DELETABLE_STATEMENTS) or (
            isinstance(node, ast.Expr)
            and isinstance(node.value.value if isinstance(node.value, ast.Await) else node.value, ast.Call)
        )
        if in_function and deletable:
            self.add_node("delete", node, "pass")
        handler = getattr(self, f"visit_{type(node).__name__}", None)
        if handler is not None:
            handler(node)
        if isinstance(node, FUNCTION_SCOPES):
            in_function = True
        elif isinstance(node, ast.ClassDef):
            in_function = False
        enclosing = self.scope
        if isinstance(node, NAMED_SCOPES):
            # Everything below a def or class, its decorators and default values included, is in its scope.
            self.scope = f"{enclosing}.{node.name}" if enclosing else node.name
        for child in ast.iter_child_nodes(node):
            self.visit(child, in_function)
        self.scope = enclosing

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        for position, operator in enumerate(node.ops):
            span = self.operator_tokens(operands[position], operands[position + 1])
            if span is not None:
                self.add("compare", span[0], span[1], COMPARE_FLIPS[type(operator)])
        for operand in operands:
            value = int_literal_value(operand)
            if value is not None:
                self.add_node("boundary", operand, str(value - 1))
                self.add_node("boundary", operand, str(value + 1))
                self.owned.add(id(operand.operand if isinstance(operand, ast.UnaryOp) else operand))

    def negate(self, test: ast.expr) -> None:
        if id(test) in self.skipped:
            return
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            inner = self.node_text(test.operand)
            replacement = inner if isinstance(test.operand, ATOMIC_TESTS) else f"({inner})"
        else:
            text = self.node_text(test)
            replacement = f"not {text}" if isinstance(test, ATOMIC_TESTS) else f"not ({text})"
        self.add_node("negate", test, replacement)

    def visit_If(self, node: ast.If) -> None:
        self.negate(node.test)

    def visit_While(self, node: ast.While) -> None:
        self.negate(node.test)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.negate(node.test)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        replacement = "or" if isinstance(node.op, ast.And) else "and"
        for left, right in zip(node.values, node.values[1:]):
            span = self.operator_tokens(left, right)
            if span is not None:
                self.add("boolop", span[0], span[1], replacement)

    def visit_Return(self, node: ast.Return) -> None:
        value = node.value
        if value is None or (isinstance(value, ast.Constant) and value.value is None):
            return
        if isinstance(value, ast.Constant):
            self.owned.add(id(value))
        if isinstance(value, ast.Constant) and isinstance(value.value, bool):
            self.add_node("return", value, str(not value.value))
        else:
            self.add_node("return", value, "None")

    def visit_BinOp(self, node: ast.BinOp) -> None:
        replacement = ARITHMETIC_SWAPS.get(type(node.op))
        span = None if replacement is None else self.operator_tokens(node.left, node.right)
        if replacement is not None and span is not None:
            self.add("arithmetic", span[0], span[1], replacement)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        replacement = ARITHMETIC_SWAPS.get(type(node.op))
        span = None if replacement is None else self.operator_tokens(node.target, node.value)
        if replacement is not None and span is not None:
            self.add("arithmetic", span[0], span[1], replacement + "=")

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in MINMAX_SWAPS:
            self.add_node("minmax", node.func, MINMAX_SWAPS[node.func.id])

    def visit_Constant(self, node: ast.Constant) -> None:
        exempt_table = id(node) in self.tables and not self.mutate_data_tables
        if id(node) not in self.owned and not exempt_table:
            replacement = constant_replacement(node.value)
            if replacement is not None:
                self.add_node("constant", node, replacement)
        if isinstance(node.value, str) and len(node.value) > 1 and node.value.startswith("@"):
            self.add_node("strip_at", node, strip_at_replacement(self.node_text(node), node.value))


def scope_names(tree: ast.Module) -> set[str]:
    """The qualname of every def and class in the module, nested names joined by "."."""
    names: set[str] = set()

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, NAMED_SCOPES):
                name = f"{prefix}.{child.name}" if prefix else child.name
                names.add(name)
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, "")
    return names


def parse_module(source: str, display: str) -> ast.Module:
    try:
        return ast.parse(source, filename=display)
    except SyntaxError as error:
        raise ConfigError(f"{display}: cannot parse: {error}") from None


def check_operators(operators: Iterable[str] | None) -> tuple[str, ...]:
    """The selected operator names (default: OPERATORS); a name outside ALL_OPERATORS is a ConfigError."""
    if operators is None:
        return OPERATORS
    selected = tuple(operators)
    unknown = sorted(set(selected) - set(ALL_OPERATORS))
    if unknown:
        raise ConfigError(f"unknown operator(s) {unknown}; choose from {list(ALL_OPERATORS)}")
    return selected


def in_scopes(scope: str, functions: tuple[str, ...]) -> bool:
    return any(scope == name or scope.startswith(name + ".") for name in functions)


def generate_mutants(
    path,
    source: str | None = None,
    *,
    mutate_data_tables: bool = False,
    functions: Iterable[str] | None = None,
    operators: Iterable[str] | None = None,
    require_functions: bool = True,
) -> list[Mutant]:
    """Every mutant of one module, in deterministic (line, col, operator) order.

    path names the module in mutant ids (as POSIX); source defaults to the file's UTF-8 text. operators selects from
    ALL_OPERATORS (default: OPERATORS). functions keeps only mutants whose scope equals or is nested inside one of the
    listed qualnames; a name that matches no def or class in the module is a ConfigError unless require_functions is
    false (a glob member: expand_targets checks the names across the whole glob). Neither filter changes the id of a
    mutant it keeps.
    """
    selected = check_operators(operators)
    if source is None:
        source = read_source(Path(path))
    display = PurePath(path).as_posix()
    tree = parse_module(source, display)
    scopes = None if functions is None else tuple(functions)
    if scopes is not None and require_functions:
        known = scope_names(tree)
        missing = [name for name in scopes if name not in known]
        if missing:
            raise ConfigError(f"{display}: function(s) {missing} match no def or class (give qualnames: Class.method)")
    collector = _Collector(source, tree, mutate_data_tables)
    candidates = sorted(collector.collect(), key=lambda item: (item[0], item[1], ALL_OPERATORS.index(item[4])))
    mutants = []
    seen: dict[tuple[int, int, str], int] = {}
    for line, col, end_line, end_col, operator, replacement, scope in candidates:
        key = (line, col, operator)
        index = seen.get(key, 0)
        seen[key] = index + 1
        if operator not in selected or (scopes is not None and not in_scopes(scope, scopes)):
            continue
        original = collector.text((line, col), (end_line, end_col))
        mutants.append(Mutant(display, line, col, end_line, end_col, operator, index, original, replacement, scope))
    return mutants


def apply_mutant(source: str, mutant: Mutant, offsets: list[int] | None = None) -> str:
    """source with the mutant's span replaced; ValueError when the span does not hold mutant.original.

    offsets, if given, must be line_offsets(source_lines(source)): a caller applying many mutants to one source
    computes them once.
    """
    if offsets is None:
        offsets = line_offsets(source_lines(source))
    if not (1 <= mutant.line <= mutant.end_line < len(offsets)):
        raise ValueError(f"{mutant.id}: span is outside the source")
    start = offsets[mutant.line - 1] + mutant.col
    end = offsets[mutant.end_line - 1] + mutant.end_col
    if source[start:end] != mutant.original or end > offsets[mutant.end_line]:
        raise ValueError(f"{mutant.id}: source does not match {mutant.original!r}")
    return source[:start] + mutant.replacement + source[end:]


# Configuration ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class TargetConfig:
    """One configured target: a repo-relative module path (or glob) and the dotted test modules that cover it.

    functions and operators restrict generation (see generate_mutants). aggregate (glob targets only) gates the
    glob's combined score instead of each file's; expand_targets records the configured glob as glob on each of its
    files, and also as group when aggregate is set. subset_of is the aggregate glob of a file that --target ran
    alone, without the rest of its group. overrides names what the command line changed ("operators", "tests",
    "min_score", "timeout"); see Report.
    """

    path: str
    tests: tuple[str, ...]
    min_score: float = 0.0
    equivalent: tuple[str, ...] = ()
    timeout: float | None = None
    mutate_data_tables: bool = False
    functions: tuple[str, ...] | None = None
    operators: tuple[str, ...] | None = None
    aggregate: bool = False
    group: str | None = None
    overrides: tuple[str, ...] = ()
    glob: str | None = None
    subset_of: str | None = None


@dataclass(frozen=True)
class Config:
    copy: tuple[str, ...]
    targets: tuple[TargetConfig, ...]


TOP_LEVEL_KEYS = {"schema_version", "description", "copy", "targets"}
TARGET_KEYS = {
    "path",
    "tests",
    "min_score",
    "equivalent",
    "timeout",
    "mutate_data_tables",
    "functions",
    "operators",
    "aggregate",
}


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_relative_path(value: object, where: str) -> str:
    """A non-empty, relative path without `..`, returned in POSIX form; anything else is a ConfigError."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: expected a non-empty relative path, got {value!r}")
    posix = value.replace("\\", "/")
    if PurePosixPath(posix).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
        raise ConfigError(f"{where}: {value!r} must be relative to the repository root")
    if ".." in PurePosixPath(posix).parts:
        raise ConfigError(f"{where}: {value!r} must not contain '..'")
    return PurePosixPath(posix).as_posix()


def check_unique_strings(value: object, where: str, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}: expected a list of strings")
    if not value and not allow_empty:
        raise ConfigError(f"{where}: must not be empty")
    if len(set(value)) != len(value):
        raise ConfigError(f"{where}: duplicate entries")
    return tuple(value)


def parse_target(entry: object, where: str) -> TargetConfig:
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: expected an object")
    unknown = set(entry) - TARGET_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    for key in ("path", "tests", "min_score"):
        if key not in entry:
            raise ConfigError(f"{where}: missing {key!r}")
    path = check_relative_path(entry["path"], f"{where}.path")
    tests = check_unique_strings(entry["tests"], f"{where}.tests", allow_empty=False)
    for name in tests:
        if not TEST_MODULE_RE.match(name):
            raise ConfigError(f"{where}.tests: {name!r} is not a dotted module name")
    min_score = entry["min_score"]
    if not is_number(min_score) or not 0.0 <= min_score <= 1.0:
        raise ConfigError(f"{where}.min_score: expected a number from 0 to 1, got {min_score!r}")
    equivalent = check_unique_strings(entry.get("equivalent", []), f"{where}.equivalent", allow_empty=True)
    for mutant_id in equivalent:
        if not MUTANT_ID_RE.match(mutant_id):
            raise ConfigError(f"{where}.equivalent: {mutant_id!r} is not a mutant id (path:line:col:operator:index)")
    timeout = entry.get("timeout")
    if timeout is not None and (not is_number(timeout) or timeout <= 0):
        raise ConfigError(f"{where}.timeout: expected a positive number of seconds, got {timeout!r}")
    mutate_data_tables = entry.get("mutate_data_tables", False)
    if not isinstance(mutate_data_tables, bool):
        raise ConfigError(f"{where}.mutate_data_tables: expected true or false")
    functions = None
    if "functions" in entry:
        functions = check_unique_strings(entry["functions"], f"{where}.functions", allow_empty=False)
        for name in functions:
            if not QUALNAME_RE.match(name):
                raise ConfigError(f"{where}.functions: {name!r} is not a qualname (name, Class.method, outer.inner)")
    operators = None
    if "operators" in entry:
        operators = check_unique_strings(entry["operators"], f"{where}.operators", allow_empty=False)
        try:
            check_operators(operators)
        except ConfigError as error:
            raise ConfigError(f"{where}.operators: {error}") from None
    aggregate = entry.get("aggregate", False)
    if not isinstance(aggregate, bool):
        raise ConfigError(f"{where}.aggregate: expected true or false")
    if aggregate and not GLOB_CHARACTERS & set(path):
        raise ConfigError(f"{where}.aggregate: needs a glob path such as 'sdk/training/**/*.py'")
    return TargetConfig(
        path,
        tests,
        float(min_score),
        equivalent,
        None if timeout is None else float(timeout),
        mutate_data_tables,
        functions,
        operators,
        aggregate,
    )


def load_config(path) -> Config:
    """Parse and validate a mutation config file; every problem is a ConfigError naming the file."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path}: config file not found") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"{path}: cannot read config: {error}") from None
    if not isinstance(payload, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    unknown = set(payload) - TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    version = payload.get("schema_version")
    if version != 1 or isinstance(version, bool):
        raise ConfigError(f"{path}: schema_version must be 1")
    if not isinstance(payload.get("description", ""), str):
        raise ConfigError(f"{path}: description must be a string")
    copy = DEFAULT_COPY_PATHS
    if "copy" in payload:
        entries = check_unique_strings(payload["copy"], f"{path}: copy", allow_empty=False)
        copy = tuple(check_relative_path(entry, f"{path}: copy") for entry in entries)
    targets = payload.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ConfigError(f"{path}: targets must be a non-empty list")
    return Config(copy, tuple(parse_target(entry, f"{path}: targets[{n}]") for n, entry in enumerate(targets)))


def mutant_id_path(mutant_id: str) -> str:
    return mutant_id.rsplit(":", 4)[0]


def is_glob(path: str) -> bool:
    return bool(GLOB_CHARACTERS & set(path))


def glob_files(pattern: str, root: Path) -> list[str]:
    """The .py files under root that a glob pattern matches, as sorted repo-relative POSIX paths (maybe none)."""
    matches = {match.relative_to(root).as_posix() for match in root.glob(pattern) if match.is_file()}
    return sorted(name for name in matches if name.endswith(".py"))


def expand_target(target: TargetConfig, repo_root) -> list[TargetConfig]:
    """One TargetConfig per concrete file of one configured target (see expand_targets)."""
    root = Path(repo_root)
    glob = target.path if is_glob(target.path) else None
    if glob is not None:
        files = glob_files(glob, root)
        if not files:
            raise ConfigError(f"target glob {glob!r} matches no .py files")
    else:
        if not (root / target.path).is_file():
            raise ConfigError(f"target not found: {target.path}")
        files = [target.path]
    for mutant_id in target.equivalent:
        if mutant_id_path(mutant_id) not in files:
            raise ConfigError(f"equivalent id {mutant_id!r} names a file outside target {target.path!r}")
    if glob is not None and target.functions is not None:
        # A glob member generates with require_functions=False, so each qualname is checked here, once for the glob.
        known = set().union(*(scope_names(parse_module(read_source(root / name), name)) for name in files))
        missing = [name for name in target.functions if name not in known]
        if missing:
            raise ConfigError(
                f"{glob}: function(s) {missing} match no def or class in any matched file (give qualnames: Class.method)"
            )
    group = glob if target.aggregate else None
    return [
        replace(
            target,
            path=name,
            equivalent=tuple(item for item in target.equivalent if mutant_id_path(item) == name),
            group=group,
            glob=glob,
        )
        for name in files
    ]


def expand_targets(config: Config, repo_root) -> list[TargetConfig]:
    """One TargetConfig per concrete file: globs expand to sorted .py files and equivalent ids split per file."""
    expanded: list[TargetConfig] = []
    owners: dict[str, str] = {}
    for target in config.targets:
        for item in expand_target(target, repo_root):
            if item.path in owners:
                raise ConfigError(
                    f"{item.path} is matched by more than one target ({owners[item.path]!r}, {target.path!r})"
                )
            owners[item.path] = target.path
            expanded.append(item)
    return expanded


# Results ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RunResult:
    """What a test runner reports for one run: "passed", "failed" or "timeout"."""

    status: str
    seconds: float
    output: str = ""
    tests_run: int | None = None


TestRunner = Callable[[Path, tuple[str, ...], float], RunResult]


@dataclass(frozen=True)
class MutantResult:
    mutant: Mutant
    status: str
    seconds: float
    diff: str

    def to_dict(self) -> dict:
        return {**self.mutant.to_dict(), "status": self.status, "seconds": round(self.seconds, 3), "diff": self.diff}


@dataclass(frozen=True)
class Report:
    """The outcome of one target.

    partial: only some mutants ran (`only`); such a score is not comparable with min_score, so it is not gated
    unless "min_score" is in overrides, and gives no ratchet hint. part_of: the glob of an aggregate target this file
    belongs to; such a report is informational and the combined report is gated. subset_of: the glob of an
    aggregate target when --target ran this one file of it alone; like a partial run it is not gated unless
    "min_score" is in overrides, and gives no ratchet hint. no_mutants: a glob member or an operator-narrowed run
    with nothing to mutate; it is skipped (never gated, always passes). operators: the operator set used.
    overrides: what the command line changed from the config ("operators", "tests", "min_score", "timeout"); the
    score is still gated, but it is not measured like the recorded floor, so no ratchet hint.
    """

    target: str
    tests: tuple[str, ...]
    results: tuple[MutantResult, ...]
    baseline_seconds: float
    timeout: float
    min_score: float = 0.0
    stale_equivalent: tuple[str, ...] = ()
    partial: bool = False
    part_of: str | None = None
    operators: tuple[str, ...] = ()
    overrides: tuple[str, ...] = ()
    subset_of: str | None = None
    no_mutants: bool = False

    @classmethod
    def combine(cls, target: str, reports: Iterable[Report], min_score: float) -> Report:
        """One gated report over every result of an aggregate target's per-file reports.

        Members without mutants add nothing; when no member had any, the combined report is skipped too.
        """
        reports = list(reports)
        return cls(
            target,
            reports[0].tests if reports else (),
            tuple(result for report in reports for result in report.results),
            sum(report.baseline_seconds for report in reports),
            max((report.timeout for report in reports), default=0.0),
            min_score,
            tuple(mutant_id for report in reports for mutant_id in report.stale_equivalent),
            any(report.partial for report in reports),
            operators=reports[0].operators if reports else (),
            overrides=tuple(dict.fromkeys(name for report in reports for name in report.overrides)),
            no_mutants=bool(reports) and all(report.no_mutants for report in reports),
        )

    @property
    def counts(self) -> dict[str, int]:
        counts = {"total": len(self.results)}
        counts.update({status: sum(result.status == status for result in self.results) for status in STATUSES})
        return counts

    @property
    def counts_by_operator(self) -> dict[str, dict[str, int]]:
        """Status counts per operator that has results, in ALL_OPERATORS order."""
        counts: dict[str, dict[str, int]] = {}
        for operator in ALL_OPERATORS:
            statuses = [result.status for result in self.results if result.mutant.operator == operator]
            if statuses:
                counts[operator] = {status: statuses.count(status) for status in STATUSES}
        return counts

    @property
    def score(self) -> float | None:
        """(killed + timeout) / scored mutants; None when no mutant was scored, so nothing was measured."""
        counts = self.counts
        scored = counts["total"] - counts["incompetent"] - counts["equivalent"]
        return (counts["killed"] + counts["timeout"]) / scored if scored else None

    @property
    def gated(self) -> bool:
        """Whether the score is compared with min_score: not for a skipped report or an aggregate member, nor for a
        partial run or a lone member of an aggregate unless its minimum was given explicitly."""
        if self.no_mutants or self.part_of is not None:
            return False
        return (not self.partial and self.subset_of is None) or "min_score" in self.overrides

    @property
    def passed(self) -> bool:
        """Without a score only a zero minimum passes: an unmeasured target cannot meet a positive one. An ungated
        partial run passes when it measured anything; a skipped report or an aggregate member always passes."""
        if self.no_mutants or self.part_of is not None:
            return True
        score = self.score
        if not self.gated:
            return score is not None
        return self.min_score <= 0.0 if score is None else score >= self.min_score

    @property
    def headroom(self) -> float | None:
        """score - min_score; None without a score or when the score is not gated."""
        score = self.score
        return None if score is None or not self.gated else score - self.min_score

    @property
    def ratchet_hint(self) -> str | None:
        """After a complete, passing run at least RATCHET_MARGIN above min_score: the floor to record instead.

        None when the run is not comparable with the recorded floor: partial, one file of an aggregate, or measured
        with operators, tests, a minimum or a timeout given on the command line.
        """
        headroom = self.headroom
        if headroom is None or self.partial or self.subset_of or self.overrides or not self.passed:
            return None
        if headroom < RATCHET_MARGIN - 1e-9:
            return None
        score = self.score or 0.0
        floor = math.floor(score * 100 + 1e-9) / 100
        if floor <= self.min_score:
            return None
        return f"ratchet: {self.target} scored {score:.3f}; raise min_score from {self.min_score:g} to {floor:.2f}"

    @property
    def survivors(self) -> tuple[MutantResult, ...]:
        return tuple(result for result in self.results if result.status == "survived")

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "target": self.target,
            "tests": list(self.tests),
            "min_score": self.min_score,
            "score": self.score,
            "passed": self.passed,
            "headroom": self.headroom,
            "part_of": self.part_of,
            "subset_of": self.subset_of,
            "partial": self.partial,
            "no_mutants": self.no_mutants,
            "gated": self.gated,
            "operators": list(self.operators),
            "overrides": list(self.overrides),
            "counts": self.counts,
            "counts_by_operator": self.counts_by_operator,
            "baseline_seconds": round(self.baseline_seconds, 3),
            "timeout_seconds": self.timeout,
            "stale_equivalent": list(self.stale_equivalent),
            "mutants": [result.to_dict() for result in self.results],
        }

    def summary_line(self) -> str:
        counts = self.counts
        return f"{counts['total']} mutants, " + ", ".join(f"{counts[status]} {status}" for status in STATUSES)

    def percent_digits(self) -> int:
        """Decimals that show min_score as a percentage exactly (1 to 4)."""
        minimum = self.min_score * 100
        return next((digits for digits in (1, 2, 3) if abs(round(minimum, digits) - minimum) < 1e-9), 4)

    def shown_minimum(self) -> str:
        return f"{self.min_score * 100:.{self.percent_digits()}f}%"

    def shown_score(self) -> str | None:
        """The score as a percentage, rounded, but never shown at or above the shown minimum when it fails."""
        score = self.score
        if score is None:
            return None
        digits = self.percent_digits()
        shown = round(score * 100, digits)
        if self.gated and score < self.min_score and shown >= round(self.min_score * 100, digits):
            shown = math.floor(score * 100 * 10**digits) / 10**digits
        return f"{shown:.{digits}f}%"

    def shown_headroom(self) -> str | None:
        """headroom in points at the score's precision, rounded down: a pass never shows two points that no ratchet
        hint backs up, and a failure never shows a headroom of zero or more."""
        headroom = self.headroom
        if headroom is None:
            return None
        digits = self.percent_digits()
        units = headroom * 100 * 10**digits
        units = math.floor(units + 1e-6) if self.passed else min(math.floor(units), -1)
        return f"{units / 10**digits:+.{digits}f}"

    def operators_note(self) -> str:
        return " (operators differ from config)" if "operators" in self.overrides else ""

    def ungated_reason(self, quote: str = "") -> str:
        if self.subset_of is not None:
            return f"part of {quote}{self.subset_of}{quote}; run the glob to gate"
        return "partial run: not gated"

    def format_text(self) -> str:
        score = self.shown_score() or "n/a (no scored mutants)"
        if self.no_mutants:
            gate = "score n/a (no mutants: skipped)"
        elif self.part_of is not None:
            gate = f"score {score} (gated as part of {self.part_of})"
        elif not self.gated:
            gate = f"score {score} ({self.ungated_reason()})"
        else:
            gate = f"score {score} (minimum {self.shown_minimum()}) {'PASS' if self.passed else 'FAIL'}"
        lines = [f"{self.target}: {self.summary_line()}", gate]
        if self.operators:
            lines.append(f"operators: {', '.join(self.operators)}{self.operators_note()}")
        for result in self.survivors:
            lines.append(f"survived {result.mutant.id}")
            lines.append(result.diff.rstrip("\n"))
        return "\n".join(lines) + "\n"

    def format_markdown(self) -> str:
        """A GitHub-flavoured Markdown section: verdict, score, per-operator table, ratchet hint, survivor diffs."""
        if self.no_mutants:
            verdict, detail = "skipped (no mutants)", "no mutants: skipped"
        elif self.part_of is not None:
            verdict, detail = f"gated as part of `{self.part_of}`", "not gated alone"
        elif not self.gated:
            reason = f"part of `{self.subset_of}`" if self.subset_of is not None else "partial run"
            verdict, detail = f"not gated ({reason})", self.ungated_reason("`")
        else:
            verdict, detail = ("PASS" if self.passed else "FAIL"), f"minimum {self.shown_minimum()}"
        score, headroom = self.shown_score(), self.shown_headroom()
        if headroom is not None:
            detail += f", headroom {headroom} points"
        if score is not None:
            shown = f"**{score}**"
        else:
            shown = "**n/a**" if self.no_mutants else "**n/a** (no scored mutants)"
        lines = [f"### `{self.target}`: {verdict}", "", f"Score {shown} ({detail}); {self.summary_line()}.", ""]
        if self.operators:
            operators = ", ".join(f"`{operator}`" for operator in self.operators)
            lines += [f"Operators: {operators}{self.operators_note()}.", ""]
        lines.append("| operator | " + " | ".join(STATUSES) + " |")
        lines.append("| --- |" + " ---: |" * len(STATUSES))
        for operator, counts in self.counts_by_operator.items():
            lines.append(f"| {operator} | " + " | ".join(str(counts[status]) for status in STATUSES) + " |")
        if self.ratchet_hint:
            lines += ["", self.ratchet_hint]
        elif self.overrides and self.passed and (self.headroom or 0.0) >= RATCHET_MARGIN - 1e-9:
            lines += ["", f"No ratchet hint: the run used command-line overrides ({', '.join(self.overrides)})."]
        survivors = self.survivors
        if survivors:
            noun = "surviving mutant" if len(survivors) == 1 else "surviving mutants"
            lines += ["", f"<details><summary>{len(survivors)} {noun}</summary>", ""]
            for result in survivors:
                fence = "`" * max([3, *(len(run) + 1 for run in re.findall(r"`+", result.diff))])
                lines += [f"`{result.mutant.id}`", "", f"{fence}diff", result.diff.rstrip("\n"), fence, ""]
            lines.append("</details>")
        return "\n".join(lines) + "\n"


def unified_diff(source: str, mutated: str, path: str) -> str:
    """A one-line-context unified diff of the mutation, every line ending in a newline."""
    lines = difflib.unified_diff(source_lines(source), source_lines(mutated), f"a/{path}", f"b/{path}", n=1)
    return "".join(line if line.endswith(("\n", "\r")) else line + "\n" for line in lines)


# Running ------------------------------------------------------------------------------------------------------
def child_environment(workspace: Path) -> dict[str, str]:
    """os.environ for a test child: only the workspace on PYTHONPATH, no bytecode, UTF-8 output.

    Temporary files go to a folder inside the workspace, so directories a killed child cannot clean up (such as
    scripts/ci.py's tachikoma-test-* working directory) are removed with the scratch directory.
    """
    env = {key: value for key, value in os.environ.items() if key not in CHILD_ENV_DROPPED}
    child_tmp = str(Path(workspace) / CHILD_TMP)
    env.update({"PYTHONPATH": str(workspace), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    env.update({"TMPDIR": child_tmp, "TEMP": child_tmp, "TMP": child_tmp})
    return env


def kill_process_tree(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        process.kill()


def drain_killed(process: subprocess.Popen) -> str:
    """The output of a process that was just killed, waiting at most KILL_GRACE seconds for its pipe to close.

    A grandchild outside the killed process group (setsid, or any child on Windows) can keep stdout open forever.
    """
    try:
        output, _ = process.communicate(timeout=KILL_GRACE)
        return output or ""
    except subprocess.TimeoutExpired:
        if process.stdout is not None:
            process.stdout.close()
        try:
            process.wait(timeout=KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass
        return ""


class SubprocessTestRunner:
    """Runs `scripts/ci.py test --names ...` from the workspace copy in its own process group."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: set[subprocess.Popen] = set()
        self._closed = False

    def __call__(self, workspace: Path, tests: tuple[str, ...], timeout: float) -> RunResult:
        workspace = Path(workspace)
        command = [sys.executable, str(workspace / "scripts" / "ci.py"), "test", "--names", *tests]
        if os.name == "posix":
            options: dict = {"start_new_session": True}
        else:
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        (workspace / CHILD_TMP).mkdir(exist_ok=True)
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=child_environment(workspace),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **options,
        )
        with self._lock:
            self._live.add(process)
            closed = self._closed
        try:
            if closed:
                kill_process_tree(process)
            try:
                output, _ = process.communicate(timeout=timeout)
                status = "passed" if process.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                kill_process_tree(process)
                output, status = drain_killed(process), "timeout"
        except BaseException:
            # Ctrl-C or SIGTERM in this thread (the baseline runs in the main thread): the child is in its own
            # session and never saw the signal, so it is killed here.
            kill_process_tree(process)
            raise
        finally:
            with self._lock:
                self._live.discard(process)
        output = output or ""
        ran = RAN_RE.findall(output)
        return RunResult(status, time.monotonic() - started, output[-OUTPUT_TAIL:], int(ran[-1]) if ran else None)

    def kill_all(self) -> None:
        with self._lock:
            self._closed = True
            live = list(self._live)
        for process in live:
            kill_process_tree(process)


def is_covered(path: str, copy_paths: Iterable[str]) -> bool:
    return any(path == entry or path.startswith(entry.rstrip("/") + "/") for entry in copy_paths)


def resolve_target(target, root: Path) -> str:
    """The target as a repo-relative POSIX path; outside the root or containing `..` is a ConfigError."""
    text = os.fspath(target)
    if os.path.isabs(text):
        try:
            text = Path(os.path.abspath(text)).relative_to(Path(os.path.abspath(root))).as_posix()
        except ValueError:
            raise ConfigError(f"target {target} is outside the repository root {root}") from None
    text = text.replace("\\", "/")
    if ".." in PurePosixPath(text).parts:
        raise ConfigError(f"target {target!r} is outside the repository root (it contains '..')")
    return PurePosixPath(text).as_posix()


def find_test_module(name: str, root: Path) -> str | None:
    """Repo-relative file of a dotted test name: a module file followed by attributes, or a package itself."""
    parts = name.split(".")
    for length in range(len(parts), 0, -1):
        base = "/".join(parts[:length])
        if (root / f"{base}.py").is_file():
            return f"{base}.py"
        if (root / base / "__init__.py").is_file():
            return f"{base}/__init__.py" if length == len(parts) else None
    return None


def validate_run(
    rel: str, tests: tuple[str, ...], root: Path, copy_paths: tuple[str, ...], *, default_runner: bool = True
) -> None:
    """Check paths before anything runs. scripts/ci.py and the test modules are required on disk and in the copy
    only for the default SubprocessTestRunner: a custom test_runner may run its suites from elsewhere."""
    for entry in copy_paths:
        if not (root / entry).exists():
            raise ConfigError(f"copy path not found: {entry}")
    if not (root / rel).is_file():
        raise ConfigError(f"target not found: {rel}")
    if not tests:
        raise ConfigError(f"no tests mapped to {rel}")
    required = {rel: f"target {rel}"}
    if default_runner:
        required["scripts/ci.py"] = "scripts/ci.py"
    for name in tests if default_runner else ():
        module = find_test_module(name, root)
        if module is None:
            raise ConfigError(f"test module not found: {name}")
        required[module] = f"test module {name} ({module})"
    for path, label in required.items():
        if not is_covered(path, copy_paths):
            raise ConfigError(f"{label} is not covered by the copy paths {list(copy_paths)}")


def copy_workspace(root: Path, workspace: Path, copy_paths: tuple[str, ...]) -> None:
    for entry in copy_paths:
        source, destination = root / entry, workspace / entry
        if source.is_dir():
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def remove_tree(path: Path) -> None:
    """rmtree with a few retries: Windows briefly keeps files locked after a process is killed."""
    for attempt in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.lexists(path):
            return
        if attempt < 4:
            time.sleep(0.2)


def compiles(text: str, path: str) -> bool:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compile(text, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return False
    return True


def run_mutants(
    target,
    tests,
    *,
    repo_root=ROOT,
    jobs: int = 1,
    timeout: float | None = None,
    equivalent: Iterable[str] = (),
    min_score: float = 0.0,
    copy_paths: Iterable[str] = DEFAULT_COPY_PATHS,
    mutants: list[Mutant] | None = None,
    only: Iterable[str] | None = None,
    mutate_data_tables: bool = False,
    functions: Iterable[str] | None = None,
    operators: Iterable[str] | None = None,
    test_runner: TestRunner | None = None,
    scratch_dir=None,
    progress: Callable[[MutantResult], None] | None = None,
    generated: list[Mutant] | None = None,
    allow_empty: bool = False,
) -> Report:
    """Mutate one target and run its mapped tests against every mutant in temporary repository copies.

    generated is every mutant of the file (with functions and operators applied) when the caller already has it;
    otherwise it is generated here. Caller-supplied mutants replace it as the list to run, but `only` and the
    stale-equivalent check still use every generated mutant. No mutants to run is a ConfigError, unless allow_empty
    (a glob member or an operator-narrowed run): then nothing runs and the report is skipped (no_mutants).

    The timeout per mutant run, unless given, is TIMEOUT_FACTOR x the baseline's seconds x the number of parallel
    runs (the baseline runs alone; parallel runs contend), and at least MIN_TIMEOUT.
    """
    root = Path(os.path.abspath(repo_root))
    rel = resolve_target(target, root)
    tests = tuple(tests)
    copy_paths = tuple(copy_paths)
    validate_run(rel, tests, root, copy_paths, default_runner=test_runner is None)
    source = read_source(root / rel)
    if generated is None:
        generated = generate_mutants(
            rel, source, mutate_data_tables=mutate_data_tables, functions=functions, operators=operators
        )
    if mutants is None:
        mutants = generated
    if not mutants and not allow_empty:
        raise ConfigError(f"{rel}: no mutants to run (check the target's functions and operators)")
    known_ids = {mutant.id for mutant in generated} | {mutant.id for mutant in mutants}
    if only is not None:
        wanted = set(only)
        unknown = sorted(wanted - known_ids)
        if unknown:
            raise ConfigError(f"unknown mutant id(s) for {rel}: {', '.join(unknown)}")
        mutants = [mutant for mutant in mutants if mutant.id in wanted]
    equivalent = tuple(equivalent)
    stale = tuple(mutant_id for mutant_id in equivalent if mutant_id not in known_ids)
    selected_operators = check_operators(operators)
    if not mutants:
        return Report(
            rel, tests, (), 0.0, 0.0, min_score, stale, only is not None, operators=selected_operators, no_mutants=True
        )
    runner = test_runner if test_runner is not None else SubprocessTestRunner()

    results: list[MutantResult | None] = [None] * len(mutants)
    to_run: list[tuple[int, Mutant, str]] = []
    offsets = line_offsets(source_lines(source))
    for index, mutant in enumerate(mutants):
        if mutant.id in equivalent:
            results[index] = MutantResult(mutant, "equivalent", 0.0, "")
            continue
        try:
            text = apply_mutant(source, mutant, offsets)
        except ValueError as error:
            raise ConfigError(str(error)) from None
        if compiles(text, rel):
            to_run.append((index, mutant, text))
        else:
            results[index] = MutantResult(mutant, "incompetent", 0.0, "")
    if progress is not None:
        for result in results:
            if result is not None:
                progress(result)

    scratch = Path(tempfile.mkdtemp(prefix="tachikoma-mutation-", dir=scratch_dir))
    stop = threading.Event()
    try:
        workspaces = [scratch / f"w{number}" for number in range(max(1, min(jobs, len(to_run))))]
        copy_workspace(root, workspaces[0], copy_paths)
        baseline = runner(workspaces[0], tests, max(BASELINE_TIMEOUT, timeout or 0.0))
        if baseline.status != "passed":
            raise BaselineError(
                f"baseline run of {' '.join(tests)} on unmutated {rel} did not pass ({baseline.status}):\n"
                f"{baseline.output[-OUTPUT_TAIL:]}"
            )
        if baseline.tests_run == 0:
            raise BaselineError(f"baseline: mapped tests ran no tests for {rel}: {' '.join(tests)}")
        for workspace in workspaces[1:]:
            copy_workspace(root, workspace, copy_paths)
        if timeout:
            effective_timeout = float(timeout)
        else:
            effective_timeout = max(MIN_TIMEOUT, TIMEOUT_FACTOR * baseline.seconds * len(workspaces))

        free: queue.Queue[Path] = queue.Queue()
        for workspace in workspaces:
            free.put(workspace)
        original_bytes = source.encode("utf-8")

        def take_workspace() -> Path:
            # Polls so that a worker can never block forever once the run is stopping.
            while True:
                if stop.is_set():
                    raise MutationError("mutation run stopped")
                try:
                    return free.get(timeout=0.2)
                except queue.Empty:
                    continue

        def run_one(index: int, mutant: Mutant, text: str) -> tuple[int, MutantResult]:
            workspace = take_workspace()
            path = workspace / rel
            try:
                path.write_bytes(text.encode("utf-8"))
                outcome = runner(workspace, tests, effective_timeout)
            finally:
                try:
                    path.write_bytes(original_bytes)
                finally:
                    free.put(workspace)
            if outcome.status not in RUN_STATUSES:
                raise MutationError(f"test runner returned unknown status {outcome.status!r}")
            status = RUN_STATUSES[outcome.status]
            diff = unified_diff(source, text, rel) if status == "survived" else ""
            return index, MutantResult(mutant, status, outcome.seconds, diff)

        executor = ThreadPoolExecutor(max_workers=len(workspaces))
        try:
            pending = {executor.submit(run_one, *item) for item in to_run}
            while pending:
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    index, result = future.result()
                    results[index] = result
                    if progress is not None:
                        progress(result)
        except BaseException:
            stop.set()
            kill_all = getattr(runner, "kill_all", None)
            if callable(kill_all):
                kill_all()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        executor.shutdown(wait=True)
    finally:
        remove_tree(scratch)
    finished = tuple(result for result in results if result is not None)
    if len(finished) != len(results):
        raise MutationError(f"{rel}: {len(results) - len(finished)} mutant(s) produced no result")
    return Report(
        rel,
        tests,
        finished,
        baseline.seconds,
        effective_timeout,
        min_score,
        stale,
        only is not None,
        operators=selected_operators,
    )


# Command line -------------------------------------------------------------------------------------------------
def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected an integer of at least 1, got {text}")
    return value


def positive_float(text: str) -> float:
    value = float(text)
    if not value > 0 or not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"expected a positive number of seconds, got {text}")
    return value


def unit_score(text: str) -> float:
    value = float(text)
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"expected a score from 0 to 1, got {text}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", help=f"config file (default: <repo-root>/{DEFAULT_CONFIG})")
    parser.add_argument("--repo-root", default=str(ROOT), help="repository root (default: this checkout)")
    parser.add_argument("--target", help="run one module or a configured glob, a path relative to the repository root")
    parser.add_argument("--tests", nargs="+", help="dotted test modules for --target (overrides the config)")
    parser.add_argument("--min-score", type=unit_score, help="minimum score for --target, from 0 to 1")
    parser.add_argument("--jobs", type=positive_int, default=min(4, os.cpu_count() or 1), help="parallel test runs")
    parser.add_argument(
        "--timeout",
        type=positive_float,
        help="seconds per mutant run (default: config, else 5x baseline x parallel runs, min 10)",
    )
    parser.add_argument("--only", nargs="+", help="run only these mutant ids")
    parser.add_argument("--operators", nargs="+", choices=ALL_OPERATORS, help="operators to apply (default: config)")
    parser.add_argument("--json", help="write a JSON report to this file (exit 0 or 1 only)")
    parser.add_argument("--summary", help="append a Markdown summary to this file, such as $GITHUB_STEP_SUMMARY")
    parser.add_argument("--scratch-dir", help="directory for temporary repository copies (default: system temp)")
    parser.add_argument("--list", action="store_true", help="print the mutants and run nothing")
    parser.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    return parser


def select_targets(args: argparse.Namespace, root: Path) -> tuple[tuple[str, ...], list[TargetConfig]]:
    """The copy paths and the concrete targets this invocation runs, after --target/--tests/--min-score."""
    if (args.tests or args.min_score is not None) and not args.target:
        raise ConfigError("--tests and --min-score need --target")
    config_path = Path(args.config) if args.config else root / DEFAULT_CONFIG
    if args.target and args.tests and not args.config and not config_path.exists():
        config = Config(DEFAULT_COPY_PATHS, ())
    else:
        config = load_config(config_path)
    if not args.target:
        return config.copy, expand_targets(config, root)
    rel = resolve_target(args.target, root)
    selected = select_entry(config, rel, root)
    if not selected:
        if not args.tests:
            raise ConfigError(f"{rel} is not a target in {config_path}; give --tests to map its tests")
        if not (root / rel).is_file():
            raise ConfigError(f"target not found: {rel}")
        selected = [TargetConfig(rel, ())]
    chosen = []
    for target in selected:
        overrides = []
        if args.tests and tuple(args.tests) != target.tests:
            target = replace(target, tests=tuple(args.tests))
            overrides.append("tests")
        if args.min_score is not None:
            target = replace(target, min_score=args.min_score)
            overrides.append("min_score")
        chosen.append(replace(target, overrides=tuple(overrides)))
    return config.copy, chosen


def select_entry(config: Config, rel: str, root: Path) -> list[TargetConfig]:
    """The concrete targets that --target rel names, expanding and validating only the config entry it belongs to.

    rel can be a configured path or glob (the whole glob), or one file of a configured glob; a lone file of an
    aggregate glob records the glob as subset_of instead of group, so it is not gated against the glob's floor.
    """
    for target in config.targets:
        if target.path == rel:
            return expand_target(target, root)
    for target in config.targets:
        if is_glob(target.path) and rel in glob_files(target.path, root):
            (member,) = [item for item in expand_target(target, root) if item.path == rel]
            return [replace(member, group=None, subset_of=member.group)]
    return []


def apply_operators(args: argparse.Namespace, targets: list[TargetConfig]) -> list[TargetConfig]:
    """--operators replaces every selected target's operators; a different set is recorded as an override."""
    if not args.operators:
        return targets
    operators = tuple(dict.fromkeys(args.operators))
    replaced = []
    for target in targets:
        overrides = target.overrides
        if set(operators) != set(check_operators(target.operators)):
            overrides = (*overrides, "operators")
        replaced.append(replace(target, operators=operators, overrides=overrides))
    return replaced


def apply_timeout(args: argparse.Namespace, targets: list[TargetConfig]) -> list[TargetConfig]:
    """--timeout replaces every selected target's timeout; a value other than the configured one is an override,
    since it changes which mutants time out (and timeouts count as killed)."""
    if args.timeout is None:
        return targets
    return [
        replace(
            target,
            timeout=args.timeout,
            overrides=target.overrides if target.timeout == args.timeout else (*target.overrides, "timeout"),
        )
        for target in targets
    ]


def only_by_target(only: list[str] | None, targets: list[TargetConfig]) -> dict[str, list[str]] | None:
    if only is None:
        return None
    paths = {target.path for target in targets}
    stray = [mutant_id for mutant_id in only if mutant_id_path(mutant_id) not in paths]
    if stray:
        raise ConfigError(f"--only ids name no selected target: {', '.join(stray)}")
    grouped: dict[str, list[str]] = {}
    for mutant_id in dict.fromkeys(only):
        grouped.setdefault(mutant_id_path(mutant_id), []).append(mutant_id)
    return grouped


def list_mutants(root: Path, targets: list[TargetConfig], only: dict[str, list[str]] | None) -> None:
    for target in targets:
        if only is not None and target.path not in only:
            continue
        mutants = target_mutants(root, target)
        if only is not None:
            wanted = set(only[target.path])
            unknown = sorted(wanted - {mutant.id for mutant in mutants})
            if unknown:
                raise ConfigError(f"unknown mutant id(s) for {target.path}: {', '.join(unknown)}")
            mutants = [mutant for mutant in mutants if mutant.id in wanted]
        for mutant in mutants:
            print(f"{mutant.id}  {mutant.original!r} -> {mutant.replacement!r}")


def progress_printer(total: int) -> Callable[[MutantResult], None]:
    done = 0

    def report(result: MutantResult) -> None:
        nonlocal done
        done += 1
        print(f"[{done}/{total}] {result.status} {result.mutant.id}", file=sys.stderr, flush=True)

    return report


def target_mutants(root: Path, target: TargetConfig) -> list[Mutant]:
    """Every mutant of a concrete target; a glob member's functions were checked across its glob already."""
    return generate_mutants(
        target.path,
        read_source(root / target.path),
        mutate_data_tables=target.mutate_data_tables,
        functions=target.functions,
        operators=target.operators,
        require_functions=target.glob is None,
    )


def may_be_empty(target: TargetConfig) -> bool:
    """A glob member, or a run without some default operator, may have nothing to mutate; it is then skipped."""
    return target.glob is not None or not set(OPERATORS) <= set(check_operators(target.operators))


def print_report(report: Report) -> None:
    for mutant_id in report.stale_equivalent:
        print(f"warning: {report.target}: stale equivalent id {mutant_id} matches no mutant", file=sys.stderr)
    print(report.format_text(), end="", flush=True)
    if report.ratchet_hint:
        print(report.ratchet_hint, file=sys.stderr, flush=True)


def run_targets(args: argparse.Namespace, root: Path) -> list[Report]:
    """Run every selected target; after the last file of an aggregate glob, add and print its combined report."""
    copy_paths, targets = select_targets(args, root)
    targets = apply_timeout(args, apply_operators(args, targets))
    only = only_by_target(args.only, targets)
    if args.list:
        list_mutants(root, targets, only)
        return []
    reports = []
    members: list[Report] = []
    for position, target in enumerate(targets):
        target_only = None if only is None else only.get(target.path)
        if only is None or target_only is not None:
            report = run_target(args, root, copy_paths, target, target_only)
            if target.group is not None:
                report = replace(report, part_of=target.group)
                members.append(report)
            print_report(report)
            reports.append(report)
        following = targets[position + 1] if position + 1 < len(targets) else None
        if target.group is not None and members and (following is None or following.group != target.group):
            combined = Report.combine(target.group, members, target.min_score)
            print_report(combined)
            reports.append(combined)
            members = []
    return reports


def run_target(
    args: argparse.Namespace,
    root: Path,
    copy_paths: tuple[str, ...],
    target: TargetConfig,
    target_only: list[str] | None,
) -> Report:
    generated = target_mutants(root, target)
    progress = None if args.quiet else progress_printer(len(generated if target_only is None else target_only))
    report = run_mutants(
        target.path,
        target.tests,
        repo_root=root,
        jobs=args.jobs,
        timeout=target.timeout,
        equivalent=target.equivalent,
        min_score=target.min_score,
        copy_paths=copy_paths,
        only=target_only,
        mutate_data_tables=target.mutate_data_tables,
        functions=target.functions,
        operators=target.operators,
        scratch_dir=args.scratch_dir,
        progress=progress,
        generated=generated,
        allow_empty=may_be_empty(target),
    )
    return replace(report, overrides=target.overrides, subset_of=target.subset_of)


def raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(os.path.abspath(args.repo_root))
    handle_sigterm = hasattr(signal, "SIGTERM") and threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, raise_keyboard_interrupt) if handle_sigterm else None
    try:
        reports = run_targets(args, root)
    except KeyboardInterrupt:
        print("mutation: interrupted; temporary copies removed", file=sys.stderr)
        return 130
    except (MutationError, OSError) as error:
        # OSError: a missing --scratch-dir, a failed copy and the like still exit 2, never 1 ("score too low").
        print(f"mutation: {error}", file=sys.stderr)
        return 2
    finally:
        if handle_sigterm:
            signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)
    passed = all(report.passed for report in reports)
    if args.json and not args.list:
        payload = {"schema_version": 1, "passed": passed, "reports": [report.to_dict() for report in reports]}
        try:
            Path(os.path.abspath(args.json)).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            print(f"mutation: cannot write {args.json}: {error}", file=sys.stderr)
            return 2
    if args.summary and not args.list:
        sections = "\n".join(report.format_markdown() for report in reports)
        try:
            with open(os.path.abspath(args.summary), "a", encoding="utf-8") as summary:
                summary.write(f"\n## Mutation testing\n\n{sections}")
        except OSError as error:
            print(f"mutation: cannot write {args.summary}: {error}", file=sys.stderr)
            return 2
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
