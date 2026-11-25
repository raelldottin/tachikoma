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

Lines carrying a `# pragma: no mutate` comment produce no mutants.
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
STATUSES = ("killed", "survived", "timeout", "incompetent", "equivalent")
RUN_STATUSES = {"passed": "survived", "failed": "killed", "timeout": "timeout"}
BASELINE_TIMEOUT = 300.0
MIN_TIMEOUT, TIMEOUT_FACTOR = 10.0, 5.0
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
MUTANT_ID_RE = re.compile(rf"^(?P<path>.+):\d+:\d+:(?:{'|'.join(OPERATORS)}):\d+$")
GLOB_CHARACTERS = frozenset("*?[")

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
ATOMIC_TESTS = (ast.Name, ast.Attribute, ast.Call, ast.Subscript, ast.Constant)
DATA_TABLE_VALUES = (ast.Dict, ast.List, ast.Tuple, ast.Set)
DELETABLE_STATEMENTS = (ast.Assign, ast.AugAssign, ast.Raise)
FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef)
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

    Lines are 1-based and columns are 0-based character offsets.
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


def is_int_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        node = node.operand
    return isinstance(node, ast.Constant) and type(node.value) is int


def int_literal_value(node: ast.AST) -> int:
    """The value of a node accepted by is_int_literal."""
    sign, inner = 1, node
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        sign, inner = -1, node.operand
    if isinstance(inner, ast.Constant) and isinstance(inner.value, int):
        return sign * inner.value
    raise TypeError(f"not an int literal: {ast.dump(node)}")


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


def constant_replacement(value: object) -> str | None:
    if isinstance(value, bool):
        return str(not value)
    if type(value) is int:
        return "1" if value == 0 else str(value + 1)
    if isinstance(value, float):
        return repr(value + 1.0) if math.isfinite(value) else None
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
    """Walks one parsed module and collects candidate mutants as (line, col, end_line, end_col, op, repl)."""

    def __init__(self, source: str, tree: ast.Module, mutate_data_tables: bool):
        self.source = source
        self.lines = source_lines(source)
        self.offsets = line_offsets(self.lines)
        self.tree = tree
        self.mutate_data_tables = mutate_data_tables
        self.candidates: list[tuple[int, int, int, int, str, str]] = []
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
        self.candidates.append((start[0], start[1], end[0], end[1], operator, replacement))

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

    # Traversal ---------------------------------------------------------------------------------------------
    def collect(self) -> list[tuple[int, int, int, int, str, str]]:
        self.mark_skipped_contexts()
        self.visit(self.tree, in_function=False)
        return [candidate for candidate in self.candidates if candidate[0] not in self.pragma_lines]

    def visit(self, node: ast.AST, in_function: bool) -> None:
        if id(node) in self.skipped:
            return
        deletable = isinstance(node, DELETABLE_STATEMENTS) or (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
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
        for child in ast.iter_child_nodes(node):
            self.visit(child, in_function)

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        for position, operator in enumerate(node.ops):
            span = self.operator_tokens(operands[position], operands[position + 1])
            if span is not None:
                self.add("compare", span[0], span[1], COMPARE_FLIPS[type(operator)])
        for operand in operands:
            if is_int_literal(operand):
                value = int_literal_value(operand)
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

    def visit_Constant(self, node: ast.Constant) -> None:
        exempt_table = id(node) in self.tables and not self.mutate_data_tables
        if id(node) not in self.owned and not exempt_table:
            replacement = constant_replacement(node.value)
            if replacement is not None:
                self.add_node("constant", node, replacement)
        if isinstance(node.value, str) and len(node.value) > 1 and node.value.startswith("@"):
            self.add_node("strip_at", node, strip_at_replacement(self.node_text(node), node.value))


def generate_mutants(path, source: str | None = None, *, mutate_data_tables: bool = False) -> list[Mutant]:
    """Every mutant of one module, in deterministic (line, col, operator) order.

    path names the module in mutant ids (as POSIX); source defaults to the file's UTF-8 text.
    """
    if source is None:
        source = read_source(Path(path))
    display = PurePath(path).as_posix()
    try:
        tree = ast.parse(source, filename=display)
    except SyntaxError as error:
        raise ConfigError(f"{display}: cannot parse: {error}") from None
    collector = _Collector(source, tree, mutate_data_tables)
    candidates = sorted(collector.collect(), key=lambda item: (item[0], item[1], OPERATORS.index(item[4])))
    mutants = []
    seen: dict[tuple[int, int, str], int] = {}
    for line, col, end_line, end_col, operator, replacement in candidates:
        key = (line, col, operator)
        index = seen.get(key, 0)
        seen[key] = index + 1
        original = collector.text((line, col), (end_line, end_col))
        mutants.append(Mutant(display, line, col, end_line, end_col, operator, index, original, replacement))
    return mutants


def apply_mutant(source: str, mutant: Mutant) -> str:
    """source with the mutant's span replaced; ValueError when the span does not hold mutant.original."""
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
    """One configured target: a repo-relative module path (or glob) and the dotted test modules that cover it."""

    path: str
    tests: tuple[str, ...]
    min_score: float = 0.0
    equivalent: tuple[str, ...] = ()
    timeout: float | None = None
    mutate_data_tables: bool = False


@dataclass(frozen=True)
class Config:
    copy: tuple[str, ...]
    targets: tuple[TargetConfig, ...]


TOP_LEVEL_KEYS = {"schema_version", "description", "copy", "targets"}
TARGET_KEYS = {"path", "tests", "min_score", "equivalent", "timeout", "mutate_data_tables"}


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
    return TargetConfig(
        path,
        tests,
        float(min_score),
        equivalent,
        None if timeout is None else float(timeout),
        mutate_data_tables,
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


def expand_targets(config: Config, repo_root) -> list[TargetConfig]:
    """One TargetConfig per concrete file: globs expand to sorted .py files and equivalent ids split per file."""
    root = Path(repo_root)
    expanded: list[TargetConfig] = []
    owners: dict[str, str] = {}
    for target in config.targets:
        if GLOB_CHARACTERS & set(target.path):
            matches = {match.relative_to(root).as_posix() for match in root.glob(target.path) if match.is_file()}
            files = sorted(name for name in matches if name.endswith(".py"))
            if not files:
                raise ConfigError(f"target glob {target.path!r} matches no .py files")
        else:
            if not (root / target.path).is_file():
                raise ConfigError(f"target not found: {target.path}")
            files = [target.path]
        for mutant_id in target.equivalent:
            if mutant_id_path(mutant_id) not in files:
                raise ConfigError(f"equivalent id {mutant_id!r} names a file outside target {target.path!r}")
        for name in files:
            if name in owners:
                raise ConfigError(f"{name} is matched by more than one target ({owners[name]!r}, {target.path!r})")
            owners[name] = target.path
            equivalent = tuple(item for item in target.equivalent if mutant_id_path(item) == name)
            expanded.append(replace(target, path=name, equivalent=equivalent))
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
    target: str
    tests: tuple[str, ...]
    results: tuple[MutantResult, ...]
    baseline_seconds: float
    timeout: float
    min_score: float = 0.0
    stale_equivalent: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        counts = {"total": len(self.results)}
        counts.update({status: sum(result.status == status for result in self.results) for status in STATUSES})
        return counts

    @property
    def score(self) -> float:
        counts = self.counts
        scored = counts["total"] - counts["incompetent"] - counts["equivalent"]
        return (counts["killed"] + counts["timeout"]) / scored if scored else 1.0

    @property
    def passed(self) -> bool:
        return self.score >= self.min_score

    @property
    def survivors(self) -> tuple[MutantResult, ...]:
        return tuple(result for result in self.results if result.status == "survived")

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "tests": list(self.tests),
            "min_score": self.min_score,
            "score": self.score,
            "passed": self.passed,
            "counts": self.counts,
            "baseline_seconds": round(self.baseline_seconds, 3),
            "timeout_seconds": self.timeout,
            "stale_equivalent": list(self.stale_equivalent),
            "mutants": [result.to_dict() for result in self.results],
        }

    def format_text(self) -> str:
        counts = self.counts
        summary = ", ".join(f"{counts[status]} {status}" for status in STATUSES)
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            f"{self.target}: {counts['total']} mutants, {summary}",
            f"score {self.score * 100:.1f}% (minimum {self.min_score * 100:.1f}%) {verdict}",
        ]
        for result in self.survivors:
            lines.append(f"survived {result.mutant.id}")
            lines.append(result.diff.rstrip("\n"))
        return "\n".join(lines) + "\n"


def unified_diff(source: str, mutated: str, path: str) -> str:
    """A one-line-context unified diff of the mutation, every line ending in a newline."""
    lines = difflib.unified_diff(source_lines(source), source_lines(mutated), f"a/{path}", f"b/{path}", n=1)
    return "".join(line if line.endswith(("\n", "\r")) else line + "\n" for line in lines)


# Running ------------------------------------------------------------------------------------------------------
def child_environment(workspace: Path) -> dict[str, str]:
    """os.environ for a test child: only the workspace on PYTHONPATH, no bytecode, UTF-8 output."""
    env = {key: value for key, value in os.environ.items() if key not in CHILD_ENV_DROPPED}
    env.update({"PYTHONPATH": str(workspace), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def kill_process_tree(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        process.kill()


class SubprocessTestRunner:
    """Runs `scripts/ci.py test --names ...` from the workspace copy in its own process group."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: set[subprocess.Popen] = set()
        self._closed = False

    def __call__(self, workspace: Path, tests: tuple[str, ...], timeout: float) -> RunResult:
        workspace = Path(workspace)
        command = [sys.executable, str(workspace / "scripts" / "ci.py"), "test", "--names", *tests]
        options: dict = {"start_new_session": True} if os.name == "posix" else {}
        if os.name != "posix":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
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
                output, _ = process.communicate()
                status = "timeout"
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


def validate_run(rel: str, tests: tuple[str, ...], root: Path, copy_paths: tuple[str, ...]) -> None:
    for entry in copy_paths:
        if not (root / entry).exists():
            raise ConfigError(f"copy path not found: {entry}")
    if not (root / rel).is_file():
        raise ConfigError(f"target not found: {rel}")
    if not tests:
        raise ConfigError(f"no tests mapped to {rel}")
    required = {"scripts/ci.py": "scripts/ci.py", rel: f"target {rel}"}
    for name in tests:
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
    test_runner: TestRunner | None = None,
    scratch_dir=None,
    progress: Callable[[MutantResult], None] | None = None,
) -> Report:
    """Mutate one target and run its mapped tests against every mutant in temporary repository copies."""
    root = Path(os.path.abspath(repo_root))
    rel = resolve_target(target, root)
    tests = tuple(tests)
    copy_paths = tuple(copy_paths)
    validate_run(rel, tests, root, copy_paths)
    source = read_source(root / rel)
    if mutants is None:
        mutants = generate_mutants(rel, source, mutate_data_tables=mutate_data_tables)
    generated_ids = {mutant.id for mutant in mutants}
    if only is not None:
        wanted = set(only)
        unknown = sorted(wanted - generated_ids)
        if unknown:
            raise ConfigError(f"unknown mutant id(s) for {rel}: {', '.join(unknown)}")
        mutants = [mutant for mutant in mutants if mutant.id in wanted]
    equivalent = tuple(equivalent)
    stale = tuple(mutant_id for mutant_id in equivalent if mutant_id not in generated_ids)
    runner = test_runner if test_runner is not None else SubprocessTestRunner()

    results: list[MutantResult | None] = [None] * len(mutants)
    to_run: list[tuple[int, Mutant, str]] = []
    for index, mutant in enumerate(mutants):
        if mutant.id in equivalent:
            results[index] = MutantResult(mutant, "equivalent", 0.0, "")
            continue
        try:
            text = apply_mutant(source, mutant)
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
        for workspace in workspaces:
            copy_workspace(root, workspace, copy_paths)
        baseline = runner(workspaces[0], tests, max(BASELINE_TIMEOUT, timeout or 0.0))
        if baseline.status != "passed":
            raise BaselineError(
                f"baseline run of {' '.join(tests)} on unmutated {rel} did not pass ({baseline.status}):\n"
                f"{baseline.output[-OUTPUT_TAIL:]}"
            )
        if baseline.tests_run == 0:
            raise BaselineError(f"baseline: mapped tests ran no tests for {rel}: {' '.join(tests)}")
        effective_timeout = float(timeout) if timeout else max(MIN_TIMEOUT, TIMEOUT_FACTOR * baseline.seconds)

        free: queue.Queue[Path] = queue.Queue()
        for workspace in workspaces:
            free.put(workspace)
        original_bytes = source.encode("utf-8")

        def run_one(index: int, mutant: Mutant, text: str) -> tuple[int, MutantResult]:
            if stop.is_set():
                raise MutationError("mutation run stopped")
            workspace = free.get()
            path = workspace / rel
            try:
                path.write_bytes(text.encode("utf-8"))
                outcome = runner(workspace, tests, effective_timeout)
            finally:
                path.write_bytes(original_bytes)
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
    return Report(rel, tests, finished, baseline.seconds, effective_timeout, min_score, stale)


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
    parser.add_argument("--target", help="run one module, a path relative to the repository root")
    parser.add_argument("--tests", nargs="+", help="dotted test modules for --target (overrides the config)")
    parser.add_argument("--min-score", type=unit_score, help="minimum score for --target, from 0 to 1")
    parser.add_argument("--jobs", type=positive_int, default=min(4, os.cpu_count() or 1), help="parallel test runs")
    parser.add_argument("--timeout", type=positive_float, help="seconds per mutant run (default: 5x baseline, min 10)")
    parser.add_argument("--only", nargs="+", help="run only these mutant ids")
    parser.add_argument("--json", help="write a JSON report to this file (exit 0 or 1 only)")
    parser.add_argument("--scratch-dir", help="directory for temporary repository copies (default: system temp)")
    parser.add_argument("--list", action="store_true", help="print the mutants and run nothing")
    parser.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    return parser


def select_targets(args: argparse.Namespace, root: Path) -> tuple[tuple[str, ...], list[TargetConfig]]:
    """The copy paths and the concrete targets this invocation runs, after --target/--tests/--min-score."""
    if (args.tests or args.min_score is not None) and not args.target:
        raise ConfigError("--tests and --min-score need --target")
    config_path = Path(args.config) if args.config else root / DEFAULT_CONFIG
    if args.target and args.tests and not config_path.exists():
        config = Config(DEFAULT_COPY_PATHS, ())
    else:
        config = load_config(config_path)
    targets = expand_targets(config, root)
    if not args.target:
        return config.copy, targets
    rel = resolve_target(args.target, root)
    matches = [target for target in targets if target.path == rel]
    if matches:
        selected = matches[0]
    elif args.tests:
        selected = TargetConfig(rel, ())
    else:
        raise ConfigError(f"{rel} is not a target in {config_path}; give --tests to map its tests")
    if args.tests:
        selected = replace(selected, tests=tuple(args.tests))
    if args.min_score is not None:
        selected = replace(selected, min_score=args.min_score)
    return config.copy, [selected]


def only_by_target(only: list[str] | None, targets: list[TargetConfig]) -> dict[str, list[str]] | None:
    if only is None:
        return None
    paths = {target.path for target in targets}
    stray = [mutant_id for mutant_id in only if mutant_id_path(mutant_id) not in paths]
    if stray:
        raise ConfigError(f"--only ids name no selected target: {', '.join(stray)}")
    grouped: dict[str, list[str]] = {}
    for mutant_id in only:
        grouped.setdefault(mutant_id_path(mutant_id), []).append(mutant_id)
    return grouped


def list_mutants(root: Path, targets: list[TargetConfig]) -> None:
    for target in targets:
        source = read_source(root / target.path)
        for mutant in generate_mutants(target.path, source, mutate_data_tables=target.mutate_data_tables):
            print(f"{mutant.id}  {mutant.original!r} -> {mutant.replacement!r}")


def progress_printer(total: int) -> Callable[[MutantResult], None]:
    done = 0

    def report(result: MutantResult) -> None:
        nonlocal done
        done += 1
        print(f"[{done}/{total}] {result.status} {result.mutant.id}", file=sys.stderr, flush=True)

    return report


def mutant_total(root: Path, target: TargetConfig, only: list[str] | None) -> int:
    if only is not None:
        return len(only)
    source = read_source(root / target.path)
    return len(generate_mutants(target.path, source, mutate_data_tables=target.mutate_data_tables))


def run_targets(args: argparse.Namespace, root: Path) -> list[Report]:
    copy_paths, targets = select_targets(args, root)
    if args.list:
        list_mutants(root, targets)
        return []
    only = only_by_target(args.only, targets)
    reports = []
    for target in targets:
        target_only = None if only is None else only.get(target.path)
        if only is not None and target_only is None:
            continue
        progress = None if args.quiet else progress_printer(mutant_total(root, target, target_only))
        report = run_mutants(
            target.path,
            target.tests,
            repo_root=root,
            jobs=args.jobs,
            timeout=args.timeout or target.timeout,
            equivalent=target.equivalent,
            min_score=target.min_score,
            copy_paths=copy_paths,
            only=target_only,
            mutate_data_tables=target.mutate_data_tables,
            scratch_dir=args.scratch_dir,
            progress=progress,
        )
        for mutant_id in report.stale_equivalent:
            print(f"warning: {report.target}: stale equivalent id {mutant_id} matches no mutant", file=sys.stderr)
        print(report.format_text(), end="", flush=True)
        reports.append(report)
    return reports


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
    except MutationError as error:
        print(f"mutation: {error}", file=sys.stderr)
        return 2
    finally:
        if handle_sigterm:
            signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)
    passed = all(report.passed for report in reports)
    if args.json and not args.list:
        payload = {"schema_version": 1, "passed": passed, "reports": [report.to_dict() for report in reports]}
        Path(os.path.abspath(args.json)).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
