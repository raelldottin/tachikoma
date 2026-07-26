"""Turn ProgramBench eval outputs into a harness-effectiveness report.

ProgramBench writes one ``<instance_id>.eval.json`` per evaluated submission. This
module reads those files and reduces them to the numbers we care about when judging
how effective the supervisor harness is at producing working code: per-instance test
pass fraction plus aggregate resolve / near-resolve rates.

No containers or network are required here; scoring is pure file reading and counting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:  # Reuse ProgramBench's thresholds when the package is installed.
    from programbench.submission import NEAR_RESOLVED_THRESHOLD, RESOLVED_THRESHOLD  # ty: ignore[unresolved-import]
except Exception:  # pragma: no cover - programbench is optional for scoring.
    RESOLVED_THRESHOLD = 1.0
    NEAR_RESOLVED_THRESHOLD = 0.95

PASSED_STATUS = "passed"
EVAL_SUFFIX = ".eval.json"


@dataclass(frozen=True)
class InstanceScore:
    """Effectiveness of a single rebuilt instance."""

    instance_id: str
    total_tests: int
    passed_tests: int
    error_code: Optional[str] = None

    @property
    def pass_fraction(self) -> float:
        return self.passed_tests / self.total_tests if self.total_tests else 0.0

    @property
    def resolved(self) -> bool:
        return self.total_tests > 0 and self.pass_fraction >= RESOLVED_THRESHOLD

    @property
    def near_resolved(self) -> bool:
        return self.total_tests > 0 and self.pass_fraction >= NEAR_RESOLVED_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "total_tests": self.total_tests,
            "passed_tests": self.passed_tests,
            "pass_fraction": round(self.pass_fraction, 4),
            "resolved": self.resolved,
            "near_resolved": self.near_resolved,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class EffectivenessReport:
    """Aggregate harness effectiveness across every scored instance."""

    instances: tuple[InstanceScore, ...]

    @property
    def instance_count(self) -> int:
        return len(self.instances)

    @property
    def resolved_count(self) -> int:
        return sum(1 for score in self.instances if score.resolved)

    @property
    def near_resolved_count(self) -> int:
        return sum(1 for score in self.instances if score.near_resolved)

    @property
    def resolve_rate(self) -> float:
        return self.resolved_count / self.instance_count if self.instance_count else 0.0

    @property
    def near_resolve_rate(self) -> float:
        return self.near_resolved_count / self.instance_count if self.instance_count else 0.0

    @property
    def mean_pass_fraction(self) -> float:
        if not self.instances:
            return 0.0
        return sum(score.pass_fraction for score in self.instances) / self.instance_count

    @property
    def error_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for score in self.instances:
            if score.error_code:
                counts[score.error_code] = counts.get(score.error_code, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_count": self.instance_count,
            "resolved_count": self.resolved_count,
            "near_resolved_count": self.near_resolved_count,
            "resolve_rate": round(self.resolve_rate, 4),
            "near_resolve_rate": round(self.near_resolve_rate, 4),
            "mean_pass_fraction": round(self.mean_pass_fraction, 4),
            "error_counts": self.error_counts,
            "instances": [score.to_dict() for score in self.instances],
        }

    def summary_table(self) -> str:
        lines = [
            "Harness Effectiveness (ProgramBench)",
            f"  instances        : {self.instance_count}",
            f"  resolved         : {self.resolved_count} ({self.resolve_rate:.1%})",
            f"  near-resolved    : {self.near_resolved_count} ({self.near_resolve_rate:.1%})",
            f"  mean pass frac.  : {self.mean_pass_fraction:.1%}",
        ]
        if self.error_counts:
            errors = ", ".join(f"{code}={count}" for code, count in sorted(self.error_counts.items()))
            lines.append(f"  errors           : {errors}")
        lines.append("")
        lines.append(f"  {'instance':<48} {'pass':>7} {'resolved':>9}  error")
        for score in self.instances:
            mark = "yes" if score.resolved else ("near" if score.near_resolved else "no")
            lines.append(f"  {score.instance_id:<48} {score.pass_fraction:>6.1%} {mark:>9}  {score.error_code or ''}")
        return "\n".join(lines)


def score_eval_data(instance_id: str, data: dict[str, Any]) -> InstanceScore:
    results = data.get("test_results") or []
    total = len(results)
    passed = sum(1 for result in results if result.get("status") == PASSED_STATUS)
    return InstanceScore(
        instance_id=instance_id,
        total_tests=total,
        passed_tests=passed,
        error_code=data.get("error_code"),
    )


def score_eval_file(path: Path) -> InstanceScore:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    instance_id = path.name[: -len(EVAL_SUFFIX)] if path.name.endswith(EVAL_SUFFIX) else path.stem
    return score_eval_data(instance_id, data)


def score_run_dir(run_dir: Path) -> EffectivenessReport:
    """Score every ``<iid>/<iid>.eval.json`` present under ``run_dir``."""
    run_dir = Path(run_dir)
    scores: list[InstanceScore] = []
    for instance_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        eval_file = instance_dir / f"{instance_dir.name}{EVAL_SUFFIX}"
        if eval_file.is_file():
            scores.append(score_eval_file(eval_file))
    return EffectivenessReport(tuple(scores))


def write_report(report: EffectivenessReport, path: Path) -> None:
    Path(path).write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
