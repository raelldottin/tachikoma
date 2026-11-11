"""Policy evaluation loop (R6, AUTO-011).

An ``Experiment`` states a hypothesis: a candidate variant (a policy or
strategy version) beats a baseline variant on one primary metric. ``decide``
turns measured samples into one of four decisions:

- ``adopt``: both variants reached the minimum sample and the candidate's
  95% interval lies wholly on the better side of the baseline's;
- ``reject``: same, but the candidate is wholly worse;
- ``keep-running``: the window is still open and the evidence is not yet
  conclusive (too few samples, or overlapping intervals);
- ``insufficient-evidence``: the window has closed without a conclusive
  result.

A decision never changes any account. ``DecisionLog`` appends decisions to a
JSONL file so every rollout choice is recorded with its evidence; acting on a
decision is a separate, human step.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence, Union

from sdk.redaction import redact_secrets

Z_95 = 1.959964
ADOPT, REJECT, KEEP_RUNNING, INSUFFICIENT = "adopt", "reject", "keep-running", "insufficient-evidence"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.@-]{0,99}$")


class ExperimentError(ValueError):
    """Raised for an invalid experiment, sample or log line."""


def _require_utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ExperimentError(f"{name} must be a timezone-aware UTC datetime")


@dataclass(frozen=True)
class Experiment:
    experiment_id: str
    hypothesis: str
    baseline: str
    candidate: str
    metric: str
    higher_is_better: bool
    min_sample: int
    window_start: datetime
    window_end: datetime

    def __post_init__(self) -> None:
        for name in ("experiment_id", "baseline", "candidate", "metric"):
            value = getattr(self, name)
            if not isinstance(value, str) or not ID_PATTERN.match(value):
                raise ExperimentError(f"{name} must be a lowercase identifier")
        if self.baseline == self.candidate:
            raise ExperimentError("baseline and candidate must differ")
        if not isinstance(self.hypothesis, str) or not self.hypothesis.strip():
            raise ExperimentError("hypothesis is required")
        if redact_secrets(self.hypothesis) != self.hypothesis:
            raise ExperimentError("hypothesis must not contain secret-shaped text")
        if not isinstance(self.higher_is_better, bool):
            raise ExperimentError("higher_is_better must be true or false")
        if isinstance(self.min_sample, bool) or not isinstance(self.min_sample, int) or self.min_sample < 2:
            raise ExperimentError("min_sample must be an integer of at least 2")
        _require_utc(self.window_start, "window_start")
        _require_utc(self.window_end, "window_end")
        if self.window_end <= self.window_start:
            raise ExperimentError("window_end must be after window_start")


@dataclass(frozen=True)
class ProportionSample:
    """Successes out of trials, for rates such as verified action success or win rate."""

    successes: int
    trials: int

    def __post_init__(self) -> None:
        for value in (self.successes, self.trials):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExperimentError("successes and trials must be non-negative integers")
        if self.successes > self.trials:
            raise ExperimentError("successes cannot exceed trials")

    @property
    def n(self) -> int:
        return self.trials

    def estimate(self) -> Optional[float]:
        return round(self.successes / self.trials, 4) if self.trials else None

    def interval(self) -> Optional[tuple[float, float]]:
        if not self.trials:
            return None
        n, p, z = self.trials, self.successes / self.trials, Z_95
        denominator = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / denominator
        margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
        return round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)


@dataclass(frozen=True)
class MeanSample:
    """Independent measurements, for quantities such as resources per hour."""

    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in self.values):
            raise ExperimentError("values must be finite numbers")

    @property
    def n(self) -> int:
        return len(self.values)

    def estimate(self) -> Optional[float]:
        return round(sum(self.values) / self.n, 4) if self.n else None

    def interval(self) -> Optional[tuple[float, float]]:
        if self.n < 2:
            return None
        mean = sum(self.values) / self.n
        variance = sum((v - mean) ** 2 for v in self.values) / (self.n - 1)
        margin = Z_95 * math.sqrt(variance / self.n)
        return round(mean - margin, 4), round(mean + margin, 4)


Sample = Union[ProportionSample, MeanSample]


@dataclass(frozen=True)
class Decision:
    experiment_id: str
    decision: str
    reason: str
    decided_at: str
    baseline: str
    candidate: str
    metric: str
    baseline_n: int
    candidate_n: int
    baseline_estimate: Optional[float]
    candidate_estimate: Optional[float]
    baseline_interval: Optional[tuple[float, float]]
    candidate_interval: Optional[tuple[float, float]]


def decide(experiment: Experiment, baseline: Sample, candidate: Sample, now: datetime) -> Decision:
    _require_utc(now, "now")
    if type(baseline) is not type(candidate):
        raise ExperimentError("baseline and candidate samples must be the same kind")
    open_window = now < experiment.window_end
    b_int, c_int = baseline.interval(), candidate.interval()

    if baseline.n < experiment.min_sample or candidate.n < experiment.min_sample or b_int is None or c_int is None:
        verdict = KEEP_RUNNING if open_window else INSUFFICIENT
        reason = (f"needs {experiment.min_sample} samples per variant; "
                  f"have {baseline.n} for {experiment.baseline} and {candidate.n} for {experiment.candidate}")
    else:
        better = c_int[0] > b_int[1] if experiment.higher_is_better else c_int[1] < b_int[0]
        worse = c_int[1] < b_int[0] if experiment.higher_is_better else c_int[0] > b_int[1]
        if better:
            verdict, reason = ADOPT, f"{experiment.candidate} is better with non-overlapping 95% intervals"
        elif worse:
            verdict, reason = REJECT, f"{experiment.candidate} is worse with non-overlapping 95% intervals"
        else:
            verdict = KEEP_RUNNING if open_window else INSUFFICIENT
            reason = "the 95% intervals overlap"

    return Decision(
        experiment_id=experiment.experiment_id, decision=verdict, reason=reason, decided_at=now.isoformat(),
        baseline=experiment.baseline, candidate=experiment.candidate, metric=experiment.metric,
        baseline_n=baseline.n, candidate_n=candidate.n,
        baseline_estimate=baseline.estimate(), candidate_estimate=candidate.estimate(),
        baseline_interval=b_int, candidate_interval=c_int,
    )


class DecisionLog:
    """Append-only JSONL record of decisions."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, decision: Decision) -> None:
        line = json.dumps(asdict(decision), sort_keys=True, separators=(",", ":"))
        if redact_secrets(line) != line:
            raise ExperimentError("decision contains secret-shaped text; refusing to write")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> list[Decision]:
        if not self.path.exists():
            return []
        decisions = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").split("\n")[:-1], start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                for key in ("baseline_interval", "candidate_interval"):
                    if payload.get(key) is not None:
                        payload[key] = tuple(payload[key])
                decisions.append(Decision(**payload))
            except (json.JSONDecodeError, TypeError) as error:
                raise ExperimentError(f"{self.path}:{number}: malformed decision line ({error})") from None
        return decisions

    def latest(self, experiment_id: str) -> Optional[Decision]:
        matching = [d for d in self.read() if d.experiment_id == experiment_id]
        return matching[-1] if matching else None


def decide_and_record(experiment: Experiment, baseline: Sample, candidate: Sample, now: datetime,
                      log: DecisionLog) -> Decision:
    decision = decide(experiment, baseline, candidate, now)
    log.append(decision)
    return decision


def samples_from_outcomes(outcomes: Sequence[str]) -> ProportionSample:
    """Verified-action success as a proportion: verified out of attempted outcomes (skipped excluded)."""
    attempted = [o for o in outcomes if o != "skipped"]
    return ProportionSample(sum(1 for o in attempted if o == "verified"), len(attempted))
