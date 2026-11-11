"""Study plans and sample-size planning (R0, AUTO-010).

A ``StudyPlan`` fixes, before a trial starts, everything the decision loop
needs: the arms (``arms[0]`` is the control), one primary metric and its
direction, the smallest improvement worth detecting, the error rates, the
window and a floor on the sample. ``per_arm_min_sample(plan)`` turns that
into the number of independent measurements each arm needs, which feeds
``Experiment.min_sample``; ``experiments_for(plan)`` builds one
``Experiment`` per candidate arm.

Sample-size formulas (normal approximation, per arm, rounded up):

- Two proportions, pooled variance under the null (Fleiss, Levin and Paik,
  Statistical Methods for Rates and Proportions, 3rd ed., 2003, eq. 4.14,
  without continuity correction)::

      n = [z(1-a/2) * sqrt(2 * pbar * qbar) + z(1-b) * sqrt(p1*q1 + p2*q2)]^2 / (p1 - p2)^2

  with ``pbar = (p1 + p2) / 2``. ``pooled=False`` gives the unpooled form
  (Chow, Shao and Wang, Sample Size Calculations in Clinical Research, 2nd
  ed., 2008, section 4.2)::

      n = (z(1-a/2) + z(1-b))^2 * (p1*q1 + p2*q2) / (p1 - p2)^2

- Two means (Chow, Shao and Wang, section 3.2.1)::

      n = (z(1-a/2) + z(1-b))^2 * (sd1^2 + sd2^2) / delta^2

  which is ``2 * sd^2 * (z + z)^2 / delta^2`` for equal standard deviations.

A one-sided test replaces ``z(1-a/2)`` with ``z(1-a)``. ``z`` is the standard
normal quantile from ``statistics.NormalDist``.

Those formulas size a conventional two-sample test. ``decide`` does not run
that test: it adopts only when the two 95% intervals do not overlap, which
for equal standard errors is a test of the difference at about z = 2.77
rather than 1.96. Sizing for it (``RULE_DECIDE``, the default) solves, for
the smallest ``n``::

      delta * sqrt(n) >= c * (sd1 + sd2) + z(power) * sqrt(sd1^2 + sd2^2)

where ``c`` is ``decide``'s own critical value: ``t_quantile_975(n - 1)``
for means and 1.96 for proportions (with ``sd = sqrt(p * q)``). For means
that is the probability ``power`` that ``decide`` returns ``adopt`` when the
measurements are independent and normal with the planned spread. For
proportions it is only the starting point: the Wald standard error runs one
to three points of power short of the Wilson interval ``decide`` uses, so
``non_overlap_proportion_sample_size`` then searches on the exact binomial
probability of adoption (``non_overlap_proportion_power``). Either way the
decide rule needs roughly 1.6 to 1.7 times the conventional sample.

Planning values (the control rate or mean, the effect and the standard
deviation) are assumptions the owner supplies. No baseline data exists yet,
so a plan sized from guesses must be re-sized once a baseline window has
been measured.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from statistics import NormalDist
from typing import Optional

from sdk.experiments.loop import (
    ID_PATTERN,
    Z_95,
    Experiment,
    ExperimentError,
    ProportionSample,
    _require_utc,
    t_quantile_975,
)
from sdk.redaction import redact_secrets

KIND_PROPORTION, KIND_MEAN = "proportion", "mean"
RULE_DECIDE, RULE_TEST = "decide-non-overlap", "two-sample-test"
DECIDE_ALPHA = 0.05  # decide() always compares 95% intervals
_EPSILON = 1e-9  # absorbs float error before rounding up, so an exact 63.0 stays 63
_MAX_N = 10_000_000


def z_quantile(p: float) -> float:
    """Standard normal quantile: ``z_quantile(0.975)`` is 1.959964."""
    if not _is_number(p) or not 0 < p < 1:
        raise ExperimentError("quantile probability must be strictly between 0 and 1")
    return NormalDist().inv_cdf(p)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_rates(alpha: float, power: float) -> None:
    if not _is_number(alpha) or not 0 < alpha < 1:
        raise ExperimentError("alpha must be strictly between 0 and 1")
    if not _is_number(power) or not 0 < power < 1:
        raise ExperimentError("power must be strictly between 0 and 1")


def _check_proportions(p1: float, p2: float) -> None:
    for value in (p1, p2):
        if not _is_number(value) or not 0 < value < 1:
            raise ExperimentError("proportions must be strictly between 0 and 1")
    if p1 == p2:
        raise ExperimentError("the two proportions must differ")


def _check_means(delta: float, sd: float, sd2: Optional[float]) -> float:
    if not _is_number(delta) or delta == 0:
        raise ExperimentError("delta must be a non-zero number")
    for value in (sd, sd if sd2 is None else sd2):
        if not _is_number(value) or value <= 0:
            raise ExperimentError("standard deviations must be positive numbers")
    return sd if sd2 is None else sd2


def _too_large() -> ExperimentError:
    return ExperimentError(f"the required sample exceeds {_MAX_N} per arm; the effect is too small to plan for")


def _round_up(n: float) -> int:
    if not math.isfinite(n) or n > _MAX_N:
        raise _too_large()
    return max(2, math.ceil(n - _EPSILON))


def _z_alpha(alpha: float, two_sided: bool) -> float:
    return z_quantile(1 - alpha / 2 if two_sided else 1 - alpha)


def two_proportion_sample_size(p1: float, p2: float, alpha: float = 0.05, power: float = 0.80,
                               two_sided: bool = True, pooled: bool = True) -> int:
    """Per-arm sample to detect ``p1`` versus ``p2`` with a two-sample test of proportions."""
    _check_proportions(p1, p2)
    _check_rates(alpha, power)
    z_a, z_b = _z_alpha(alpha, two_sided), z_quantile(power)
    spread = math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))
    if pooled:
        pbar = (p1 + p2) / 2
        numerator = (z_a * math.sqrt(2 * pbar * (1 - pbar)) + z_b * spread) ** 2
    else:
        numerator = (z_a + z_b) ** 2 * spread ** 2
    return _round_up(numerator / (p1 - p2) ** 2)


def two_mean_sample_size(delta: float, sd: float, alpha: float = 0.05, power: float = 0.80,
                         two_sided: bool = True, sd2: Optional[float] = None) -> int:
    """Per-arm sample to detect a difference ``delta`` in means; ``sd2`` defaults to ``sd``."""
    sd2 = _check_means(delta, sd, sd2)
    _check_rates(alpha, power)
    z_a, z_b = _z_alpha(alpha, two_sided), z_quantile(power)
    return _round_up((z_a + z_b) ** 2 * (sd ** 2 + sd2 ** 2) / delta ** 2)


def _binomial_window(n: int, p: float) -> tuple[int, list[float]]:
    """Binomial(n, p) probabilities over mean +/- 8 standard deviations (the mass outside is negligible)."""
    spread = 8 * math.sqrt(n * p * (1 - p)) + 1
    low, high = max(0, math.floor(n * p - spread)), min(n, math.ceil(n * p + spread))
    log_p, log_q, log_n = math.log(p), math.log(1 - p), math.lgamma(n + 1)
    return low, [math.exp(log_n - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * log_p + (n - k) * log_q)
                 for k in range(low, high + 1)]


def non_overlap_proportion_power(n: int, p1: float, p2: float) -> float:
    """Probability that ``decide`` adopts a candidate with true rate ``p2`` over a control at ``p1``.

    Exact under independent binomial sampling with ``n`` trials per arm: it sums, over the control's
    outcomes, the chance that the candidate's 95% Wilson interval (as ``ProportionSample`` computes and
    rounds it) lies wholly beyond the control's on the side of ``p2``."""
    _check_proportions(p1, p2)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ExperimentError("n must be a positive integer")
    if p2 < p1:  # Wilson intervals are symmetric under p -> 1 - p, so count failures instead
        p1, p2 = 1 - p1, 1 - p2
    b_low, b_pmf = _binomial_window(n, p1)
    c_low, c_pmf = _binomial_window(n, p2)
    tail = [0.0] * (len(c_pmf) + 1)  # tail[i]: P(candidate successes >= c_low + i)
    for i in range(len(c_pmf) - 1, -1, -1):
        tail[i] = tail[i + 1] + c_pmf[i]
    c_lower = [ProportionSample(c_low + i, n).interval()[0] for i in range(len(c_pmf))]
    total, j = 0.0, 0
    for i, mass in enumerate(b_pmf):  # the threshold j only moves up as the control's count rises
        b_upper = ProportionSample(b_low + i, n).interval()[1]
        while j < len(c_pmf) and c_lower[j] <= b_upper:
            j += 1
        total += mass * tail[j]
    return min(1.0, total)


def non_overlap_proportion_sample_size(p1: float, p2: float, power: float = 0.80) -> int:
    """Per-arm sample for ``decide`` to adopt (non-overlapping 95% Wilson intervals) with probability ``power``.

    Starts from the normal approximation above, which runs one to three points of power short for Wilson
    intervals, then searches on ``non_overlap_proportion_power`` for the boundary ``n`` where the exact power
    first reaches ``power`` (``n - 1`` falls short). The binomial is discrete, so power is not perfectly
    monotone in ``n``; the search returns a boundary, not a guarantee that no smaller ``n`` ever reaches it."""
    _check_proportions(p1, p2)
    _check_rates(DECIDE_ALPHA, power)
    s1, s2 = math.sqrt(p1 * (1 - p1)), math.sqrt(p2 * (1 - p2))
    root_n = (Z_95 * (s1 + s2) + z_quantile(power) * math.hypot(s1, s2)) / abs(p1 - p2)
    start = _round_up(root_n ** 2)

    def enough(n: int) -> bool:
        return non_overlap_proportion_power(n, p1, p2) >= power

    step = max(1, start // 50)
    if enough(start):
        high, low = start, start - step
        while low > 1 and enough(low):
            high, low, step = low, max(1, low - step), step * 2
    else:
        low, high = start, start + step
        while not enough(high):
            low, high, step = high, high + step, step * 2
            if high > _MAX_N:
                raise _too_large()
    while high - low > 1:  # invariant: enough(high), and low is 1 or not enough(low)
        middle = (low + high) // 2
        if enough(middle):
            high = middle
        else:
            low = middle
    return max(2, high)


def non_overlap_mean_power(n: int, delta: float, sd: float, sd2: Optional[float] = None) -> float:
    """Normal-approximation probability that ``decide`` adopts a candidate better by ``delta``, with ``n`` per arm.

    ``Phi((|delta| * sqrt(n) - t_quantile_975(n - 1) * (sd1 + sd2)) / sqrt(sd1^2 + sd2^2))``, treating the
    sample standard deviations as the planned ones."""
    sd2 = _check_means(delta, sd, sd2)
    if isinstance(n, bool) or not isinstance(n, int) or n < 2:
        raise ExperimentError("n must be an integer of at least 2")
    gap = abs(delta) * math.sqrt(n) - t_quantile_975(n - 1) * (sd + sd2)
    return NormalDist().cdf(gap / math.hypot(sd, sd2))


def non_overlap_mean_sample_size(delta: float, sd: float, power: float = 0.80, sd2: Optional[float] = None) -> int:
    """Per-arm sample for ``decide`` to adopt (non-overlapping 95% t intervals) with probability ``power``.

    ``decide``'s critical value ``t_quantile_975(n - 1)`` shrinks as ``n`` grows, so this starts from the
    normal approximation and steps up to the smallest ``n`` that meets the condition."""
    sd2 = _check_means(delta, sd, sd2)
    _check_rates(DECIDE_ALPHA, power)
    z_b, spread = z_quantile(power), math.hypot(sd, sd2)
    n = _round_up(((Z_95 * (sd + sd2) + z_b * spread) / abs(delta)) ** 2)
    while abs(delta) * math.sqrt(n) < t_quantile_975(n - 1) * (sd + sd2) + z_b * spread - _EPSILON:
        n += 1
    return n


@dataclass(frozen=True)
class StudyPlan:
    """A pre-registered comparison of one control arm with one or more candidate arms.

    ``baseline_value`` is the expected control value (a rate for proportions, a mean otherwise) and
    ``minimum_effect`` the smallest absolute improvement worth detecting, in the better direction.
    ``sd`` is the expected standard deviation of one measurement and is required for mean metrics only.
    ``unit`` names one independent measurement, such as ``account-day`` or ``battle``.
    """

    study_id: str
    hypothesis: str
    arms: tuple[str, ...]
    metric: str
    metric_kind: str
    higher_is_better: bool
    baseline_value: float
    minimum_effect: float
    unit: str
    window_start: datetime
    window_end: datetime
    sd: Optional[float] = None
    alpha: float = 0.05
    power: float = 0.80
    min_sample: int = 2
    decision_rule: str = RULE_DECIDE

    def __post_init__(self) -> None:
        for name in ("study_id", "metric", "unit"):
            value = getattr(self, name)
            if not isinstance(value, str) or not ID_PATTERN.match(value):
                raise ExperimentError(f"{name} must be a lowercase identifier")
        if not isinstance(self.hypothesis, str) or not self.hypothesis.strip():
            raise ExperimentError("hypothesis is required")
        if redact_secrets(self.hypothesis) != self.hypothesis:
            raise ExperimentError("hypothesis must not contain secret-shaped text")
        if not isinstance(self.arms, tuple) or len(self.arms) < 2:
            raise ExperimentError("arms must be a tuple of at least two arm identifiers, control first")
        for arm in self.arms:
            if not isinstance(arm, str) or not ID_PATTERN.match(arm):
                raise ExperimentError("each arm must be a lowercase identifier")
            if not ID_PATTERN.match(f"{self.study_id}.{arm}"):
                raise ExperimentError("study_id and arm together must fit an experiment identifier (100 characters)")
        if len(set(self.arms)) != len(self.arms):
            raise ExperimentError("arms must be distinct")
        if self.metric_kind not in (KIND_PROPORTION, KIND_MEAN):
            raise ExperimentError(f"metric_kind must be {KIND_PROPORTION!r} or {KIND_MEAN!r}")
        if not isinstance(self.higher_is_better, bool):
            raise ExperimentError("higher_is_better must be true or false")
        if not _is_number(self.baseline_value):
            raise ExperimentError("baseline_value must be a finite number")
        if not _is_number(self.minimum_effect) or self.minimum_effect <= 0:
            raise ExperimentError("minimum_effect must be a positive number (the direction comes from higher_is_better)")
        if self.metric_kind == KIND_PROPORTION:
            if self.sd is not None:
                raise ExperimentError("sd applies to mean metrics only; a proportion's spread follows from its rate")
            _check_proportions(self.baseline_value, self.candidate_value)
        elif not _is_number(self.sd) or self.sd <= 0:
            raise ExperimentError("mean metrics need a positive sd")
        _check_rates(self.alpha, self.power)
        if self.power <= self.alpha:
            raise ExperimentError("power must exceed alpha")
        if self.decision_rule not in (RULE_DECIDE, RULE_TEST):
            raise ExperimentError(f"decision_rule must be {RULE_DECIDE!r} or {RULE_TEST!r}")
        if self.decision_rule == RULE_DECIDE and self.alpha != DECIDE_ALPHA:
            raise ExperimentError("decide() compares 95% intervals, so the decide rule needs alpha 0.05")
        if isinstance(self.min_sample, bool) or not isinstance(self.min_sample, int) or self.min_sample < 2:
            raise ExperimentError("min_sample must be an integer of at least 2")
        _require_utc(self.window_start, "window_start")
        _require_utc(self.window_end, "window_end")
        if self.window_end <= self.window_start:
            raise ExperimentError("window_end must be after window_start")

    @property
    def control(self) -> str:
        return self.arms[0]

    @property
    def candidates(self) -> tuple[str, ...]:
        return self.arms[1:]

    @property
    def comparisons(self) -> int:
        return len(self.arms) - 1

    @property
    def candidate_value(self) -> float:
        """The value a candidate must reach for the effect the plan is sized to detect."""
        sign = 1 if self.higher_is_better else -1
        return self.baseline_value + sign * self.minimum_effect

    @property
    def comparison_alpha(self) -> float:
        """Alpha per comparison: Bonferroni-split for the test rule, fixed at 0.05 for the decide rule."""
        if self.decision_rule == RULE_TEST:
            return self.alpha / self.comparisons
        return DECIDE_ALPHA


def per_arm_min_sample(plan: StudyPlan) -> int:
    """Independent measurements each arm needs before a decision; use it as ``Experiment.min_sample``.

    The larger of the plan's ``min_sample`` floor and the size its decision rule requires."""
    p0, p1 = plan.baseline_value, plan.candidate_value
    if plan.decision_rule == RULE_DECIDE:
        if plan.metric_kind == KIND_PROPORTION:
            n = non_overlap_proportion_sample_size(p0, p1, plan.power)
        else:
            n = non_overlap_mean_sample_size(plan.minimum_effect, plan.sd, plan.power)
    elif plan.metric_kind == KIND_PROPORTION:
        n = two_proportion_sample_size(p0, p1, plan.comparison_alpha, plan.power)
    else:
        n = two_mean_sample_size(plan.minimum_effect, plan.sd, plan.comparison_alpha, plan.power)
    return max(plan.min_sample, n)


def experiments_for(plan: StudyPlan) -> tuple[Experiment, ...]:
    """One ``Experiment`` per candidate arm against the control, each with ``per_arm_min_sample(plan)``."""
    n = per_arm_min_sample(plan)
    return tuple(
        Experiment(
            experiment_id=f"{plan.study_id}.{arm}", hypothesis=plan.hypothesis, baseline=plan.control,
            candidate=arm, metric=plan.metric, higher_is_better=plan.higher_is_better, min_sample=n,
            window_start=plan.window_start, window_end=plan.window_end,
        )
        for arm in plan.candidates
    )
