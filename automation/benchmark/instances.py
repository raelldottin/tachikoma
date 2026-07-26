"""Resolve which ProgramBench instances to run and their task metadata.

The instance catalogue and per-task metadata ship inside the ``programbench`` package
(``programbench/data/tasks/<instance>/task.yaml``), so this module reads them locally
with no network or container. When ``programbench`` is not importable we fall back to a
small built-in smoke set so the CLI and tests still function.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

# Cheap, fast-building instances used when neither --instances nor --all is given.
DEFAULT_SMOKE_INSTANCES: tuple[str, ...] = ("abishekvashok__cmatrix.5c082c6",)

# ProgramBench reserves this prefix for its own test fixtures; never benchmark them.
_FIXTURE_PREFIX = "testorg__"


@dataclass(frozen=True)
class TaskSpec:
    """What the harness needs to attempt one ProgramBench rebuild."""

    instance_id: str
    repository: str
    commit: str
    language: str
    difficulty: str

    @property
    def objective(self) -> str:
        return (
            f"Rebuild the program `{self.repository}` (language: {self.language}) from scratch so "
            "that its black-box test suite passes. Work only from the program's observable behaviour "
            "and interface. Produce a self-contained codebase plus an executable `compile.sh` at the "
            "workspace root that builds the program's `./executable`."
        )


def _tasks_dir() -> Optional[Path]:
    try:
        from programbench.constants import TASKS_DIR  # ty: ignore[unresolved-import]
    except Exception:  # pragma: no cover - programbench optional.
        return None
    tasks_dir = Path(TASKS_DIR)
    return tasks_dir if tasks_dir.is_dir() else None


def _read_task_yaml(path: Path) -> dict[str, str]:
    """Parse the flat scalar keys of a ProgramBench task.yaml without a YAML dependency.

    task.yaml is a flat ``key: value`` mapping plus one list (``eval_clean_hashes``); we
    only need the scalar fields, so a tiny line parser keeps this dependency-free.
    """
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith(("#", "-")) or ":" not in line:
            continue
        if line[0].isspace():  # nested list/mapping value, not a top-level scalar.
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip("'\"")
        if value:
            values[key.strip()] = value
    return values


def task_spec(instance_id: str) -> TaskSpec:
    data: dict[str, str] = {}
    tasks_dir = _tasks_dir()
    if tasks_dir is not None:
        task_yaml = tasks_dir / instance_id / "task.yaml"
        if task_yaml.is_file():
            data = _read_task_yaml(task_yaml)
    repository = data.get("repository") or instance_id.rsplit(".", 1)[0].replace("__", "/")
    return TaskSpec(
        instance_id=instance_id,
        repository=repository,
        commit=data.get("commit", ""),
        language=data.get("language", "unknown"),
        difficulty=data.get("difficulty", "unknown"),
    )


def all_instances() -> list[str]:
    tasks_dir = _tasks_dir()
    if tasks_dir is None:
        return list(DEFAULT_SMOKE_INSTANCES)
    return sorted(
        entry.name
        for entry in tasks_dir.iterdir()
        if entry.is_dir() and (entry / "task.yaml").is_file() and not entry.name.startswith(_FIXTURE_PREFIX)
    )


def resolve_instances(explicit: Optional[Sequence[str]], use_all: bool) -> list[str]:
    if explicit:
        return list(explicit)
    if use_all:
        return all_instances()
    return list(DEFAULT_SMOKE_INSTANCES)
