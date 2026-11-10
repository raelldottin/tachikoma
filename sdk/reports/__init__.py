"""Reports over the gameplay journal (roadmap R2 and R3 metrics)."""

from sdk.reports.battles import JournalBattle, battles_from_events, hull_bracket, trophy_bracket
from sdk.reports.progression import DayMetrics, FailureEntry, compute_progression_metrics, to_json, to_markdown
from sdk.reports.pvp import (
    RoleStats,
    ScorecardRow,
    StrategyComparison,
    compare_strategies,
    compute_scorecard,
    wilson_interval,
)

__all__ = [
    "DayMetrics",
    "FailureEntry",
    "JournalBattle",
    "RoleStats",
    "ScorecardRow",
    "StrategyComparison",
    "compare_strategies",
    "compute_scorecard",
    "wilson_interval",
    "battles_from_events",
    "compute_progression_metrics",
    "hull_bracket",
    "to_json",
    "to_markdown",
    "trophy_bracket",
]
