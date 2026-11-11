"""PvP scorecards and strategy comparison (R3 exit metrics).

Input is ``JournalBattle`` records from ``sdk.reports.battles``. Rows are
grouped by account alias, strategy key and opponent cohort (hull bracket and
trophy bracket). Win rates use battles with a known result only, and each
carries a 95% Wilson score interval. Unknown outcomes are counted, never
folded into wins or losses.

``compare_strategies`` recommends a strategy only when both strategies have
at least ``min_sample`` known-result battles and their intervals do not
overlap, both overall and in every cohort where both strategies reach the
sample size. Otherwise the verdict is ``insufficient-evidence`` or
``no-clear-difference``. No sample means no number: rates are ``None``.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional, Sequence

from sdk.pvp import BattleOutcome, BattleRole
from sdk.reports.battles import JournalBattle

Z_95 = 1.959964
NO_STRATEGY = "none"
DEFAULT_MIN_SAMPLE = 30


def wilson_interval(wins: int, n: int, z: float = Z_95) -> Optional[tuple[float, float]]:
    if n <= 0:
        return None
    p = wins / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)


@dataclass(frozen=True)
class RoleStats:
    battles: int = 0
    known: int = 0
    wins: int = 0
    losses: int = 0
    unknown: int = 0
    win_rate: Optional[float] = None
    interval: Optional[tuple[float, float]] = None


def _role_stats(battles: Sequence[JournalBattle]) -> RoleStats:
    wins = sum(1 for b in battles if b.result.outcome == BattleOutcome.WIN.value)
    losses = sum(1 for b in battles if b.result.outcome == BattleOutcome.LOSS.value)
    known = sum(1 for b in battles if b.outcome_known)
    return RoleStats(
        battles=len(battles),
        known=known,
        wins=wins,
        losses=losses,
        unknown=len(battles) - known,
        win_rate=round(wins / known, 4) if known else None,
        interval=wilson_interval(wins, known),
    )


@dataclass(frozen=True)
class ScorecardRow:
    account_alias: str
    strategy_key: str
    opponent_hull_bracket: str
    opponent_trophy_bracket: str
    attack: RoleStats
    defense: RoleStats
    net_trophies: Optional[int]
    trophy_samples: int
    mean_duration_s: Optional[float]
    resources_spent: dict = field(default_factory=dict)
    resource_samples: int = 0


def observed_spend(battles: Iterable[JournalBattle]) -> tuple[dict, int]:
    """Observed spend per currency (from negative cost deltas) and how many battles observed any cost."""
    spent: dict = {}
    resource_samples = 0
    for battle in battles:
        deltas = {name: battle.cost.get(f"{name}_delta") for name in ("minerals", "gas", "starbux")}
        if any(value is not None for value in deltas.values()):
            resource_samples += 1
        for name, value in deltas.items():
            if value is not None and value < 0:
                spent[name] = spent.get(name, 0) - value
    return spent, resource_samples


def cohort_label(battle: JournalBattle) -> str:
    """The opponent cohort ``compare_strategies`` stratifies by: hull bracket / trophy bracket."""
    return f"{battle.opponent_hull_bracket} / {battle.opponent_trophy_bracket}"


def _row(key: tuple, battles: Sequence[JournalBattle]) -> ScorecardRow:
    alias, strategy_key, hull, trophies = key
    observed = [b.trophy_delta for b in battles if b.trophy_delta is not None]
    durations = [b.result.battle_duration_s for b in battles if b.result.battle_duration_s is not None]
    spent, resource_samples = observed_spend(battles)
    return ScorecardRow(
        account_alias=alias,
        strategy_key=strategy_key,
        opponent_hull_bracket=hull,
        opponent_trophy_bracket=trophies,
        attack=_role_stats([b for b in battles if b.result.role == BattleRole.ATTACKER.value]),
        defense=_role_stats([b for b in battles if b.result.role == BattleRole.DEFENDER.value]),
        net_trophies=sum(observed) if observed else None,
        trophy_samples=len(observed),
        mean_duration_s=round(sum(durations) / len(durations), 1) if durations else None,
        resources_spent=spent,
        resource_samples=resource_samples,
    )


def _key(battle: JournalBattle) -> tuple:
    return (battle.result.account_pseudonym, battle.strategy_key or NO_STRATEGY,
            battle.opponent_hull_bracket, battle.opponent_trophy_bracket)


def compute_scorecard(battles: Iterable[JournalBattle]) -> list[ScorecardRow]:
    groups: dict[tuple, list[JournalBattle]] = {}
    for battle in battles:
        groups.setdefault(_key(battle), []).append(battle)
    return [_row(key, groups[key]) for key in sorted(groups)]


@dataclass(frozen=True)
class StrategyComparison:
    account_alias: Optional[str]
    strategy_a: str
    strategy_b: str
    role: str
    min_sample: int
    a: RoleStats
    b: RoleStats
    verdict: str
    recommended: Optional[str]
    reason: str
    cohorts: dict = field(default_factory=dict)


def _better(a: RoleStats, b: RoleStats, min_sample: int) -> Optional[str]:
    """'a' or 'b' when one interval lies wholly above the other with enough samples, else None."""
    if a.known < min_sample or b.known < min_sample or a.interval is None or b.interval is None:
        return None
    if a.interval[0] > b.interval[1]:
        return "a"
    if b.interval[0] > a.interval[1]:
        return "b"
    return None


def compare_strategies(
    battles: Iterable[JournalBattle],
    strategy_a: str,
    strategy_b: str,
    *,
    account_alias: Optional[str] = None,
    role: str = BattleRole.ATTACKER.value,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> StrategyComparison:
    if min_sample < 1:
        raise ValueError("min_sample must be at least 1")
    pool = [b for b in battles if b.result.role == role
            and (account_alias is None or b.result.account_pseudonym == account_alias)]
    side = {strategy_a: "a", strategy_b: "b"}
    by_side = {"a": [], "b": []}
    cohorts: dict[str, dict[str, list[JournalBattle]]] = {}
    for battle in pool:
        name = side.get(battle.strategy_key or NO_STRATEGY)
        if name is None:
            continue
        by_side[name].append(battle)
        cohorts.setdefault(cohort_label(battle), {"a": [], "b": []})[name].append(battle)

    a, b = _role_stats(by_side["a"]), _role_stats(by_side["b"])
    cohort_stats = {
        cohort: {"a": _role_stats(sides["a"]), "b": _role_stats(sides["b"])} for cohort, sides in sorted(cohorts.items())
    }
    cohort_winners = {cohort: _better(s["a"], s["b"], min_sample) for cohort, s in cohort_stats.items()}
    overall = _better(a, b, min_sample)

    if a.known < min_sample or b.known < min_sample:
        verdict, recommended = "insufficient-evidence", None
        reason = (f"needs at least {min_sample} known-result battles per strategy; "
                  f"have {a.known} for {strategy_a} and {b.known} for {strategy_b}")
    elif overall is None:
        verdict, recommended = "no-clear-difference", None
        reason = "the 95% intervals overlap"
    elif any(winner not in (None, overall) for winner in cohort_winners.values()):
        verdict, recommended = "insufficient-evidence", None
        reason = "opponent cohorts disagree about which strategy is better"
    else:
        recommended = strategy_a if overall == "a" else strategy_b
        verdict = "recommend"
        reason = f"{recommended} has a higher win rate with non-overlapping 95% intervals"

    return StrategyComparison(
        account_alias=account_alias, strategy_a=strategy_a, strategy_b=strategy_b, role=role,
        min_sample=min_sample, a=a, b=b, verdict=verdict, recommended=recommended, reason=reason,
        cohorts={cohort: {"a": asdict(s["a"]), "b": asdict(s["b"])} for cohort, s in cohort_stats.items()},
    )


# --- output -----------------------------------------------------------------------


def to_json(rows: Sequence[ScorecardRow], comparison: Optional[StrategyComparison] = None) -> str:
    payload = {"scorecard": [asdict(row) for row in rows]}
    if comparison is not None:
        payload["comparison"] = asdict(comparison)
    return json.dumps(payload, indent=2, sort_keys=True)


def _rate(stats: RoleStats) -> str:
    if stats.win_rate is None:
        return "n/a"
    low, high = stats.interval
    return f"{stats.win_rate:.0%} ({low:.0%}–{high:.0%})"


def _cell(value) -> str:
    return "n/a" if value is None else str(value)


def to_markdown(rows: Sequence[ScorecardRow], comparison: Optional[StrategyComparison] = None) -> str:
    if not rows:
        return "_No battles._\n"
    lines = [
        "| Account | Strategy | Opponent cohort | Attacks (known/total) | Attack win rate (95% CI) | "
        "Defenses (known/total) | Defense win rate (95% CI) | Unknown | Net trophies (n) | Mean duration (s) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        net = "n/a" if row.net_trophies is None else f"{row.net_trophies:+d} ({row.trophy_samples})"
        lines.append(
            f"| {row.account_alias} | {row.strategy_key} | {row.opponent_hull_bracket} / {row.opponent_trophy_bracket} | "
            f"{row.attack.known}/{row.attack.battles} | {_rate(row.attack)} | {row.defense.known}/{row.defense.battles} | "
            f"{_rate(row.defense)} | {row.attack.unknown + row.defense.unknown} | {net} | {_cell(row.mean_duration_s)} |"
        )
    if comparison is not None:
        lines += [
            "",
            f"**Comparison ({comparison.role}):** `{comparison.strategy_a}` {_rate(comparison.a)} "
            f"(n={comparison.a.known}) vs `{comparison.strategy_b}` {_rate(comparison.b)} (n={comparison.b.known}). "
            f"Verdict: **{comparison.verdict}**. {comparison.reason}.",
        ]
    return "\n".join(lines) + "\n"
