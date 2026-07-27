"""Pluggable eval backends.

ProgramBench's authoritative test execution runs each submission inside an amd64
cleanroom Docker image, so it is the one step that needs containers (and a native
x86_64 host for reasonable speed). We hide it behind a small protocol so the
orchestrator and tests never depend on Docker directly:

* ``ProgramBenchEvalRunner`` shells out to the real ``programbench eval`` CLI.
* ``RecordedEvalRunner`` writes caller-supplied eval JSON, for local smoke runs and
  tests that must stay container-free.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, Protocol, Sequence, Union


class EvalRunner(Protocol):
    def evaluate(self, run_dir: Path) -> None:
        """Write ``<iid>/<iid>.eval.json`` for each submission under ``run_dir``."""


class ProgramBenchEvalRunner:
    """Authoritative eval via the ProgramBench CLI. Requires Docker (amd64)."""

    def __init__(
        self,
        programbench_cmd: Sequence[str] = ("uvx", "programbench"),
        workers: Union[int, None] = None,
        extra_args: Sequence[str] = (),
    ) -> None:
        self._cmd = list(programbench_cmd)
        self._workers = workers
        self._extra = list(extra_args)

    def evaluate(self, run_dir: Path) -> None:
        argv = [*self._cmd, "eval", str(run_dir)]
        if self._workers is not None:
            argv += ["--workers", str(self._workers)]
        argv += self._extra
        subprocess.run(argv, check=True)


class RecordedEvalRunner:
    """Container-free eval double that writes pre-supplied results per instance."""

    def __init__(self, provider: Callable[[str], dict]) -> None:
        self._provider = provider

    @classmethod
    def from_mapping(cls, results: Mapping[str, dict]) -> "RecordedEvalRunner":
        return cls(lambda instance_id: results[instance_id])

    def evaluate(self, run_dir: Path) -> None:
        run_dir = Path(run_dir)
        for instance_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            if not (instance_dir / "submission.tar.gz").is_file():
                continue
            data = self._provider(instance_dir.name)
            eval_file = instance_dir / f"{instance_dir.name}.eval.json"
            eval_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
