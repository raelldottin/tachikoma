#!/usr/bin/env python3
"""Progression trial exit report, manual vs automated (SPEC-07 R4, R7, R8).

Builds study A's exit report for one account and two arms (``arms[0]`` is the
manual control, ``arms[1]`` the automated candidate) from:

- a trial plan (JSON): trial id, hypothesis, account alias, arms, primary
  metric and direction, minimum sample per arm, window, stop thresholds;
- each arm's snapshot series (``AccountSnapshot.to_dict()`` records);
- the automated arm's gameplay journal (JSONL);
- operator minutes per account-day (JSON list of ``day``, ``arm``,
  ``operator_minutes``);
- the intervention log (JSON list of ``at``, ``kind``, ``note`` and
  optionally ``arm`` and ``action``).

The primary metric, growth velocity, is computed the same way in both arms
(R4): for each pair of consecutive snapshots about one day apart inside the
window, the number of rooms whose design changed plus the number of research
entries that became ``Completed``, from ``diff_snapshots``. Journal outcomes
never feed it. A day whose rooms or research were not observed on both sides
contributes no value rather than 0.

Resources per hour come from the snapshot ledger (``build_ledger``) for both
arms, and from journal costs (``compute_progression_metrics``) where a
journal exists. Verified success, failures and the failure ledger come from
the journal. Every value that was not observed is ``None`` next to its sample
size. ``decide()`` compares the arms' growth velocity under the plan's
``Experiment``.

The trial stops early (R8) when a stop threshold is crossed: cumulative
failure rate or counts set in the plan, any starbux spent by automation, any
journal event of another account, or an intervention whose action is
``abort-arm``. The report then says it stopped early, and its ``decide()``
result is for reference only.

With ``--pvp`` it builds study C's PvP strategy comparison instead (SPEC-08
R3-R6) from:

- a PvP plan (JSON): ``trial_id``, ``hypothesis``, ``account_alias``,
  ``strategies`` (two registered strategy keys such as
  ``weapon-focus-fire@v1``), ``windows`` (a list of ``strategy``, ``start``,
  ``end`` in UTC), ``min_sample`` and optionally ``effect``
  (``baseline_win_rate``, ``minimum_effect``, optional ``power``),
  ``game_version`` and ``notes``;
- the account's gameplay journals (JSONL; ``run_pvp_battle`` events);
- the strategy adoption log (``AdoptionLog`` JSON from
  ``sdk/pvp/strategies.py``, such as
  ``docs/experiments/r3-pvp-trial/applications.json``);
- optionally battle-history records (see below).

A journaled battle is credited to the strategy ``TrialRecord.attribution_at``
returns for its start time, and to none otherwise. Its outcome is known
(S03-R5) when the event reconciled ``verified`` at evidence tier
``outcome-observed`` with no error class and an observed trophy change, or
when a battle-history attacker record with its battle id is present (the
server's reported outcome then wins; a record that contradicts a known
journal outcome makes it unknown). Defender records never verify an attack.
Unknown outcomes are counted, never dropped (R3). Attack win rates are
compared with ``compare_strategies``; its minimum sample is
``per_arm_min_sample`` from ``sdk.experiments.study`` when the plan gives an
effect size and the plan's ``min_sample`` otherwise, and the comparison is
stratified by opponent cohort whenever a cohort was observed (R4). Defense
results come only from battle-history defender records and are reported
separately; they never drive the recommendation (R5).

Battle-history file format (version 1, provisional until SPEC-03 lands the
``listPvPBattles2`` parser): a JSON list of objects with

- ``battle_id`` (string, required): the game's battle id;
- ``account_alias`` (string, required): must be the plan's account;
- ``role`` (required): ``attacker`` or ``defender``, from this account's side;
- ``outcome`` (required): ``win``, ``loss``, ``draw``, ``timeout`` or
  ``unknown``, as the server reports it for this account;
- ``at`` (required): the battle's start time, ISO 8601 UTC;
- ``trophy_change``, ``opponent_hull_level`` (1-20), ``opponent_trophies``:
  integers, or ``null`` when the record does not carry them.

No other key is accepted, and a ``(battle_id, role)`` pair may appear once.

Offline only: it reads files and makes no network call.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdk.economy.ledger import CURRENCIES, build_ledger  # noqa: E402
from sdk.experiments import (  # noqa: E402
    KIND_PROPORTION,
    Experiment,
    MeanSample,
    ProportionSample,
    StudyPlan,
    decide,
    per_arm_min_sample,
)
from sdk.experiments.loop import ID_PATTERN  # noqa: E402
from sdk.journal import GameplayEvent, read_events  # noqa: E402
from sdk.pvp import BattleOutcome, BattleResult, BattleRole, BattleStrategy  # noqa: E402
from sdk.pvp.strategies import (  # noqa: E402
    AdoptionLog,
    ExperimentSchedule,
    ExperimentWindow,
    PvpStrategy,
    StrategyAttribution,
    StrategyRegistry,
    TrialRecord,
    candidate_registry,
)
from sdk.redaction import redact_secrets  # noqa: E402
from sdk.reports.battles import (  # noqa: E402
    BATTLE_ACTION_ID,
    UNKNOWN_COHORT,
    JournalBattle,
    battles_from_events,
    hull_bracket,
    trophy_bracket,
)
from sdk.reports.progression import compute_progression_metrics  # noqa: E402
from sdk.reports.pvp import RoleStats, cohort_label, compare_strategies, observed_spend  # noqa: E402
from sdk.state.snapshot import (  # noqa: E402
    ACCOUNT_ALIAS_PATTERN,
    SECTIONS,
    AccountMismatchError,
    AccountSnapshot,
    Completeness,
    CrewMember,
    InventoryItem,
    Research,
    Resources,
    Room,
    SnapshotDelta,
    check_game_identity,
    diff_snapshots,
)

GROWTH_SOURCE = "snapshot diffs: room design changes plus research completions per account-day"
RESEARCH_DONE = "Completed"
DEFAULT_DAY_PAIR_HOURS = (20.0, 28.0)
ACTIONS = ("continue", "exclude-day", "split-block", "abort-arm")
# Safety stops that always apply (study design, stopping rule 7); the plan may only make them stricter.
ALWAYS_ON_THRESHOLDS = {"max_starbux_spent": 0, "max_cross_account_events": 0}
COUNT_THRESHOLDS = ("max_failed", "max_uncertain", "max_contradicted", "max_starbux_spent",
                    "max_cross_account_events")
THRESHOLD_KEYS = frozenset(COUNT_THRESHOLDS + ("max_failure_rate", "failure_rate_min_attempts"))
PLAN_KEYS = frozenset({"trial_id", "hypothesis", "account_alias", "arms", "metric", "higher_is_better",
                       "min_sample", "window_start", "window_end", "stop_thresholds", "day_pair_hours", "notes"})
REQUIRED_PLAN_KEYS = ("trial_id", "hypothesis", "account_alias", "arms", "metric", "higher_is_better",
                      "min_sample", "window_start", "window_end")


class TrialInputError(ValueError):
    """Raised for a malformed trial input. The message never repeats an input value."""


def _utc(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise TrialInputError(f"{label} must be an ISO 8601 time") from None
    else:
        raise TrialInputError(f"{label} must be an ISO 8601 time")
    if moment.utcoffset() != timedelta(0):
        raise TrialInputError(f"{label} must be a timezone-aware UTC time")
    return moment


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise TrialInputError(f"{label} must be a lowercase identifier")
    return value


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _refuse_secrets(payload: Any, label: str) -> None:
    text = json.dumps(payload, sort_keys=True, default=str)
    if redact_secrets(text) != text:
        raise TrialInputError(f"{label} contains secret-shaped text; refusing to use it")


# -- inputs --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrialPlan:
    trial_id: str
    hypothesis: str
    account_alias: str
    arms: tuple[str, str]
    metric: str
    higher_is_better: bool
    min_sample: int
    window_start: datetime
    window_end: datetime
    stop_thresholds: Mapping[str, float] = field(default_factory=dict)
    day_pair_hours: tuple[float, float] = DEFAULT_DAY_PAIR_HOURS

    @property
    def control(self) -> str:
        return self.arms[0]

    @property
    def candidate(self) -> str:
        return self.arms[1]

    def thresholds(self) -> dict[str, float]:
        """The plan's thresholds, with the always-on safety stops at their strictest."""
        merged = dict(self.stop_thresholds)
        for key, limit in ALWAYS_ON_THRESHOLDS.items():
            merged[key] = min(limit, merged.get(key, limit))
        return merged

    def experiment(self) -> Experiment:
        return Experiment(
            experiment_id=self.trial_id, hypothesis=self.hypothesis, baseline=self.control,
            candidate=self.candidate, metric=self.metric, higher_is_better=self.higher_is_better,
            min_sample=self.min_sample, window_start=self.window_start, window_end=self.window_end,
        )


def plan_from_dict(payload: Mapping[str, Any]) -> TrialPlan:
    if not isinstance(payload, Mapping):
        raise TrialInputError("the trial plan must be a JSON object")
    _refuse_secrets(payload, "the trial plan")
    unknown = sorted(set(payload) - PLAN_KEYS)
    if unknown:
        raise TrialInputError(f"unknown trial plan keys: {', '.join(unknown)}")
    missing = [key for key in REQUIRED_PLAN_KEYS if key not in payload]
    if missing:
        raise TrialInputError(f"the trial plan is missing: {', '.join(missing)}")
    alias = payload["account_alias"]
    if not isinstance(alias, str) or not ACCOUNT_ALIAS_PATTERN.match(alias):
        raise TrialInputError("account_alias must be a lowercase alias, not an e-mail or display name")
    arms = payload["arms"]
    if not isinstance(arms, list) or len(arms) != 2:
        raise TrialInputError("arms must list exactly two arms, the manual control first")
    arms = tuple(_identifier(arm, "each arm") for arm in arms)
    if arms[0] == arms[1]:
        raise TrialInputError("the two arms must differ")
    thresholds = payload.get("stop_thresholds") or {}
    if not isinstance(thresholds, Mapping):
        raise TrialInputError("stop_thresholds must be a JSON object")
    unknown = sorted(set(thresholds) - THRESHOLD_KEYS)
    if unknown:
        raise TrialInputError(f"unknown stop thresholds: {', '.join(unknown)}")
    for key, limit in thresholds.items():
        if not _number(limit) or limit < 0:
            raise TrialInputError(f"stop threshold {key} must be a non-negative number")
    if "max_failure_rate" in thresholds and thresholds["max_failure_rate"] > 1:
        raise TrialInputError("max_failure_rate must be between 0 and 1")
    hours = payload.get("day_pair_hours", list(DEFAULT_DAY_PAIR_HOURS))
    if (not isinstance(hours, list) or len(hours) != 2 or not all(_number(h) for h in hours)
            or not 0 < hours[0] <= hours[1]):
        raise TrialInputError("day_pair_hours must be [minimum, maximum] hours with 0 < minimum <= maximum")
    min_sample = payload["min_sample"]
    if isinstance(min_sample, bool) or not isinstance(min_sample, int):
        raise TrialInputError("min_sample must be an integer")
    plan = TrialPlan(
        trial_id=_identifier(payload["trial_id"], "trial_id"),
        hypothesis=payload["hypothesis"],
        account_alias=alias,
        arms=arms,
        metric=_identifier(payload["metric"], "metric"),
        higher_is_better=payload["higher_is_better"],
        min_sample=min_sample,
        window_start=_utc(payload["window_start"], "window_start"),
        window_end=_utc(payload["window_end"], "window_end"),
        stop_thresholds=dict(thresholds),
        day_pair_hours=(float(hours[0]), float(hours[1])),
    )
    plan.experiment()  # the Experiment checks the hypothesis, direction, sample floor and window
    return plan


def _optional_text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TrialInputError("snapshot counts must be integers or null")
    return value


def snapshot_from_dict(payload: Mapping[str, Any]) -> AccountSnapshot:
    """Load an ``AccountSnapshot.to_dict()`` record back into a snapshot."""
    if not isinstance(payload, Mapping):
        raise TrialInputError("a snapshot must be a JSON object")
    _refuse_secrets(payload, "a snapshot")
    alias = payload.get("account_alias")
    if not isinstance(alias, str) or not ACCOUNT_ALIAS_PATTERN.match(alias):
        raise TrialInputError("snapshot account_alias must be a lowercase alias")
    try:
        completeness = {name: Completeness((payload.get("completeness") or {}).get(name, "missing"))
                        for name in SECTIONS}
        resources = payload.get("resources") or {}
        return AccountSnapshot(
            account_alias=alias,
            observed_at=_utc(payload.get("observed_at"), "snapshot observed_at"),
            source=str(payload.get("source") or "file"),
            game_version=_optional_text(payload.get("game_version")),
            hull_level=_optional_int(payload.get("hull_level")),
            trophies=_optional_int(payload.get("trophies")),
            resources=Resources(**{name: _optional_int(resources.get(name)) for name in CURRENCIES}),
            crew=tuple(
                CrewMember(
                    character_id=str(member["character_id"]), design_id=_optional_text(member.get("design_id")),
                    name=_optional_text(member.get("name")), level=_optional_int(member.get("level")),
                    fatigue=_optional_int(member.get("fatigue")), room_id=_optional_text(member.get("room_id")),
                    training_observed=bool(member.get("training_observed", False)),
                    training_design_id=_optional_text(member.get("training_design_id")),
                    training_end=_optional_text(member.get("training_end")),
                )
                for member in payload.get("crew") or ()
            ),
            rooms=tuple(Room(room_id=str(room["room_id"]), design_id=_optional_text(room.get("design_id")),
                             status=_optional_text(room.get("status")))
                        for room in payload.get("rooms") or ()),
            research=tuple(Research(design_id=str(entry["design_id"]), state=_optional_text(entry.get("state")))
                           for entry in payload.get("research") or ()),
            inventory=tuple(InventoryItem(item_design_id=str(item["item_design_id"]),
                                          quantity=_optional_int(item.get("quantity")))
                            for item in payload.get("inventory") or ()),
            completeness=completeness,
            notes=tuple(str(note) for note in payload.get("notes") or ()),
            game_identity=check_game_identity(payload.get("game_identity")),
        )
    except (KeyError, TypeError, AttributeError) as error:
        raise TrialInputError(f"malformed snapshot record ({type(error).__name__})") from None


@dataclass(frozen=True)
class Intervention:
    at: str
    kind: str
    note: str
    arm: Optional[str] = None
    action: str = "continue"


def interventions_from_list(payload: Any, plan: TrialPlan) -> list[Intervention]:
    if not isinstance(payload, list):
        raise TrialInputError("the intervention log must be a JSON list")
    _refuse_secrets(payload, "the intervention log")
    entries = []
    for number, item in enumerate(payload, start=1):
        if not isinstance(item, Mapping) or not {"at", "kind", "note"} <= set(item):
            raise TrialInputError(f"intervention {number} needs at, kind and note")
        unknown = sorted(set(item) - {"at", "kind", "note", "arm", "action"})
        if unknown:
            raise TrialInputError(f"intervention {number} has unknown keys: {', '.join(unknown)}")
        arm = item.get("arm")
        if arm is not None and arm not in plan.arms:
            raise TrialInputError(f"intervention {number} names an arm that is not in the plan")
        action = item.get("action", "continue")
        if action not in ACTIONS:
            raise TrialInputError(f"intervention {number} action must be one of {', '.join(ACTIONS)}")
        if not isinstance(item["note"], str):
            raise TrialInputError(f"intervention {number} note must be text")
        entries.append(Intervention(
            at=_utc(item["at"], f"intervention {number} at").isoformat(),
            kind=_identifier(item["kind"], f"intervention {number} kind"),
            note=item["note"], arm=arm, action=action,
        ))
    return sorted(entries, key=lambda entry: entry.at)


def minutes_from_list(payload: Any, plan: TrialPlan) -> dict[str, dict[str, Optional[float]]]:
    """Operator minutes per arm and UTC day. A ``null`` entry is a day without a log (unobserved)."""
    if not isinstance(payload, list):
        raise TrialInputError("operator minutes must be a JSON list")
    _refuse_secrets(payload, "operator minutes")
    minutes: dict[str, dict[str, Optional[float]]] = {arm: {} for arm in plan.arms}
    for number, item in enumerate(payload, start=1):
        if not isinstance(item, Mapping) or not {"day", "arm", "operator_minutes"} <= set(item):
            raise TrialInputError(f"minutes entry {number} needs day, arm and operator_minutes")
        if item.get("account_alias", plan.account_alias) != plan.account_alias:
            raise AccountMismatchError(f"minutes entry {number} belongs to another account")
        if item["arm"] not in plan.arms:
            raise TrialInputError(f"minutes entry {number} names an arm that is not in the plan")
        try:
            day = date.fromisoformat(str(item["day"])).isoformat()
        except ValueError:
            raise TrialInputError(f"minutes entry {number} day must be YYYY-MM-DD") from None
        value = item["operator_minutes"]
        if value is not None and (not _number(value) or value < 0):
            raise TrialInputError(f"minutes entry {number} operator_minutes must be a non-negative number or null")
        if day in minutes[item["arm"]]:
            raise TrialInputError(f"minutes entry {number} repeats a day for its arm")
        minutes[item["arm"]][day] = value
    return minutes


# -- primary metric (R4) -------------------------------------------------------------------------


def growth_units(delta: SnapshotDelta) -> Optional[dict[str, int]]:
    """Room design advances and research completions in one interval; ``None`` when either was unobserved."""
    if "rooms" in delta.unknown_sections or "research" in delta.unknown_sections:
        return None
    rooms = sum(1 for old, new in delta.room_design.values() if old is not None and new is not None)
    research = sum(1 for old, new in delta.research_state.values() if new == RESEARCH_DONE and old != RESEARCH_DONE)
    return {"room_design_advances": rooms, "research_completions": research}


@dataclass(frozen=True)
class GrowthDay:
    day: str
    start: str
    end: str
    units: Optional[int]
    room_design_advances: Optional[int]
    research_completions: Optional[int]
    excluded: Optional[str] = None


def _within(moment: datetime, plan: TrialPlan) -> bool:
    return plan.window_start <= moment <= plan.window_end


def _day_in_window(day: str, plan: TrialPlan) -> bool:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return start < plan.window_end and start + timedelta(days=1) > plan.window_start


def growth_days(plan: TrialPlan, arm: str, snapshots: Sequence[AccountSnapshot],
                interventions: Sequence[Intervention] = ()) -> list[GrowthDay]:
    """One entry per consecutive snapshot pair inside the window; excluded pairs say why."""
    for snapshot in snapshots:
        if snapshot.account_alias != plan.account_alias:
            raise AccountMismatchError("a snapshot belongs to another account; accounts never mix")
    series = sorted((s for s in snapshots if _within(s.observed_at, plan)), key=lambda s: s.observed_at)
    excluding = [_utc(entry.at, "intervention at") for entry in interventions
                 if entry.action == "exclude-day" and entry.arm in (None, arm)]
    low, high = plan.day_pair_hours
    days = []
    for before, after in zip(series, series[1:]):
        delta = diff_snapshots(before, after)
        hours = delta.elapsed_seconds / 3600
        units = growth_units(delta)
        excluded = None
        if not low <= hours <= high:
            excluded = f"interval of {hours:.1f} h is not one account-day"
        elif units is None:
            excluded = "rooms or research not observed on both sides"
        elif any(before.observed_at <= moment < after.observed_at for moment in excluding):
            excluded = "excluded by the intervention log"
        counted = units if excluded is None else None
        days.append(GrowthDay(
            day=before.observed_at.date().isoformat(), start=before.observed_at.isoformat(),
            end=after.observed_at.isoformat(),
            units=None if counted is None else counted["room_design_advances"] + counted["research_completions"],
            room_design_advances=None if counted is None else counted["room_design_advances"],
            research_completions=None if counted is None else counted["research_completions"],
            excluded=excluded,
        ))
    return days


def growth_sample(days: Sequence[GrowthDay]) -> MeanSample:
    return MeanSample(tuple(float(day.units) for day in days if day.units is not None))


# -- secondary metrics (R7) ----------------------------------------------------------------------


def _mean_summary(sample: MeanSample) -> dict[str, Any]:
    return {"n": sample.n, "mean": sample.estimate(), "interval_95": sample.interval()}


def _per_hour(amount: Optional[float], hours: float) -> Optional[float]:
    return round(amount / hours, 2) if amount is not None and hours > 0 else None


def snapshot_resources(plan: TrialPlan, snapshots: Sequence[AccountSnapshot],
                       events: Sequence[GameplayEvent]) -> dict[str, Any]:
    """Gained and spent per hour from the snapshot ledger: net change between consecutive snapshots."""
    series = sorted((s for s in snapshots if _within(s.observed_at, plan)), key=lambda s: s.observed_at)
    ledger = build_ledger(plan.account_alias, series, events)
    result: dict[str, Any] = {"source": "snapshot ledger (net change between consecutive snapshots)",
                              "intervals": len(ledger.entries)}
    for name in CURRENCIES:
        observed = [(entry, entry.currency.get(name)) for entry in ledger.entries
                    if entry.currency.get(name) is not None]
        hours = sum((datetime.fromisoformat(e.end) - datetime.fromisoformat(e.start)).total_seconds()
                    for e, _ in observed) / 3600
        gained = sum(v for _, v in observed if v > 0) if observed else None
        spent = sum(-v for _, v in observed if v < 0) if observed else None
        result[name] = {"samples": len(observed), "hours": round(hours, 4) if observed else None,
                        "gained": gained, "spent": spent,
                        "gained_per_hour": _per_hour(gained, hours), "spent_per_hour": _per_hour(spent, hours)}
    return result


def journal_metrics(events: Sequence[GameplayEvent]) -> dict[str, Any]:
    """Verified success, failures and journal resource flows; every rate is ``None`` without a sample."""
    days = compute_progression_metrics(events)
    attempted = sum(day.attempted for day in days)
    verified = sum(day.verified for day in days)
    success = ProportionSample(verified, attempted)
    with_resources = [day for day in days if day.resource_samples]
    hours = sum(day.elapsed_hours or 0.0 for day in with_resources)
    resources: dict[str, Any] = {"source": "journal costs of attempted actions",
                                 "samples": sum(day.resource_samples for day in days),
                                 "hours": round(hours, 4) if with_resources else None}
    for name in CURRENCIES:
        gained = sum(day.resources_gained.get(name, 0) for day in with_resources) if with_resources else None
        spent = sum(day.resources_spent.get(name, 0) for day in with_resources) if with_resources else None
        resources[name] = {"gained": gained, "spent": spent, "gained_per_hour": _per_hour(gained, hours),
                           "spent_per_hour": _per_hour(spent, hours)}
    return {
        "verified_success": {"attempted": attempted, "verified": verified, "rate": success.estimate(),
                             "interval_95": success.interval()},
        "failures": {
            "attempted": attempted,
            "failed": sum(day.failed for day in days),
            "uncertain": sum(day.uncertain for day in days),
            "contradicted": sum(day.contradicted for day in days),
            "not_observed": sum(day.not_observed for day in days),
            "failure_rate": round(sum(day.failed for day in days) / attempted, 4) if attempted else None,
            "ledger": [asdict(failure) for day in days for failure in day.failures],
        },
        "journal_resources": resources,
    }


# -- early stop (R8) -----------------------------------------------------------------------------


def _event_day(event: GameplayEvent) -> str:
    return datetime.fromisoformat(event.finished_at).date().isoformat()


def check_stop(plan: TrialPlan, journals: Mapping[str, Sequence[GameplayEvent]],
               foreign: Mapping[str, Sequence[GameplayEvent]],
               interventions: Sequence[Intervention] = ()) -> Optional[dict[str, Any]]:
    """The first UTC day on which any arm crossed a stop threshold, with every crossing that day."""
    thresholds = plan.thresholds()
    min_attempts = thresholds.get("failure_rate_min_attempts", 1)
    crossings_by_day: dict[str, list[dict[str, Any]]] = {}
    for arm in plan.arms:
        metrics = {day.day: day for day in compute_progression_metrics(journals.get(arm, ()))}
        foreign_by_day: dict[str, int] = {}
        for event in foreign.get(arm, ()):
            foreign_by_day[_event_day(event)] = foreign_by_day.get(_event_day(event), 0) + 1
        aborts: dict[str, list[Intervention]] = {}
        for entry in interventions:
            if entry.action == "abort-arm" and entry.arm in (None, arm) and _within(_utc(entry.at, "at"), plan):
                aborts.setdefault(_utc(entry.at, "at").date().isoformat(), []).append(entry)
        totals = dict.fromkeys(("attempted",) + COUNT_THRESHOLDS, 0)
        for day in sorted(set(metrics) | set(foreign_by_day) | set(aborts)):
            if day in metrics:
                m = metrics[day]
                totals["attempted"] += m.attempted
                totals["max_failed"] += m.failed
                totals["max_uncertain"] += m.uncertain
                totals["max_contradicted"] += m.contradicted
                totals["max_starbux_spent"] += m.resources_spent.get("starbux", 0)
            totals["max_cross_account_events"] += foreign_by_day.get(day, 0)
            crossed = [{"arm": arm, "threshold": key, "value": totals[key], "limit": thresholds[key]}
                       for key in COUNT_THRESHOLDS if key in thresholds and totals[key] > thresholds[key]]
            if "max_failure_rate" in thresholds and totals["attempted"] >= max(1, min_attempts):
                rate = round(totals["max_failed"] / totals["attempted"], 4)
                if rate > thresholds["max_failure_rate"]:
                    crossed.append({"arm": arm, "threshold": "max_failure_rate", "value": rate,
                                    "limit": thresholds["max_failure_rate"]})
            crossed += [{"arm": arm, "threshold": "intervention", "value": entry.kind, "limit": "abort-arm"}
                        for entry in aborts.get(day, ())]
            if crossed:
                crossings_by_day.setdefault(day, []).extend(crossed)
                break
    if not crossings_by_day:
        return None
    first = min(crossings_by_day)
    return {"day": first, "crossings": crossings_by_day[first]}


# -- the report ----------------------------------------------------------------------------------


def build_trial_report(
    plan: TrialPlan,
    snapshots: Mapping[str, Sequence[AccountSnapshot]],
    journals: Optional[Mapping[str, Sequence[GameplayEvent]]] = None,
    minutes: Optional[Mapping[str, Mapping[str, Optional[float]]]] = None,
    interventions: Sequence[Intervention] = (),
    *,
    now: datetime,
) -> dict[str, Any]:
    now = _utc(now, "now")
    journals, minutes = journals or {}, minutes or {}
    for name, mapping in (("snapshots", snapshots), ("journals", journals), ("minutes", minutes)):
        unknown = set(mapping) - set(plan.arms)
        if unknown:
            raise TrialInputError(f"{name} name an arm that is not in the plan")

    own: dict[str, list[GameplayEvent]] = {}
    foreign: dict[str, list[GameplayEvent]] = {}
    for arm in plan.arms:
        in_window = [e for e in journals.get(arm, ()) if _within(datetime.fromisoformat(e.finished_at), plan)]
        own[arm] = [e for e in in_window if e.account_alias == plan.account_alias]
        foreign[arm] = [e for e in in_window if e.account_alias != plan.account_alias]

    arms, samples, versions = [], {}, set()
    for arm in plan.arms:
        series = list(snapshots.get(arm, ()))
        versions |= {s.game_version for s in series if s.game_version is not None and _within(s.observed_at, plan)}
        days = growth_days(plan, arm, series, interventions)
        sample = samples[arm] = growth_sample(days)
        counted = [day for day in days if day.units is not None]
        logged = [value for day, value in sorted((minutes.get(arm) or {}).items())
                  if value is not None and _day_in_window(day, plan)]
        operator = MeanSample(tuple(float(value) for value in logged))
        arms.append({
            "arm": arm,
            "role": "control" if arm == plan.control else "candidate",
            "sample_size": sample.n,
            "snapshots": sum(1 for s in series if _within(s.observed_at, plan)),
            "growth_velocity": {
                **_mean_summary(sample),
                "unit": "account-day",
                "source": GROWTH_SOURCE,
                "room_design_advances": sum(d.room_design_advances for d in counted) if counted else None,
                "research_completions": sum(d.research_completions for d in counted) if counted else None,
            },
            "excluded_days": [{"day": d.day, "start": d.start, "end": d.end, "reason": d.excluded}
                              for d in days if d.excluded],
            "resources_per_hour": snapshot_resources(plan, series, own[arm]),
            "journal_events": len(own[arm]),
            "cross_account_events": len(foreign[arm]),
            **journal_metrics(own[arm]),
            "operator_minutes": {**_mean_summary(operator), "unit": "account-day",
                                 "total": round(sum(logged), 2) if logged else None},
        })

    decision = decide(plan.experiment(), samples[plan.control], samples[plan.candidate], now)
    early_stop = check_stop(plan, own, foreign, interventions)
    control_minutes, candidate_minutes = (arm["operator_minutes"]["mean"] for arm in arms)
    saved = (round(control_minutes - candidate_minutes, 2)
             if control_minutes is not None and candidate_minutes is not None else None)

    report = {
        "trial_id": plan.trial_id,
        "account_alias": plan.account_alias,
        "hypothesis": plan.hypothesis,
        "metric": plan.metric,
        "higher_is_better": plan.higher_is_better,
        "min_sample": plan.min_sample,
        "window": {"start": plan.window_start.isoformat(), "end": plan.window_end.isoformat()},
        "evaluated_at": now.isoformat(),
        "game_version": sorted(versions)[0] if len(versions) == 1 else None,
        "game_versions": sorted(versions),
        "status": "stopped-early" if early_stop else decision.decision,
        "stop_thresholds": plan.thresholds(),
        "early_stop": early_stop,
        "decision": asdict(decision),
        "operator_minutes_saved_per_account_day": saved,
        "arms": arms,
        "interventions": [asdict(entry) for entry in interventions],
    }
    report["limitations"] = _limitations(report)
    _refuse_secrets(report, "the trial report")
    return report


def _limitations(report: Mapping[str, Any]) -> list[str]:
    notes = [
        "Account-days are serially correlated (a day spent saving makes the next day's upgrades likelier), "
        "so the 95% intervals and the decide() result are optimistic. Block means are the sensitivity check "
        "and are not computed by this report.",
        "The results describe one account. They do not generalize to other hull levels or play styles.",
        "The manual arm is not journaled. Verified success, failures and journal resources describe the "
        "automated arm only and are not evidence that automation beats manual play.",
        "Growth counts room design changes and research completions between consecutive snapshots. Work "
        "started in one block and finished in the next is credited to the day it finished.",
        "Snapshot resource flows are net changes between snapshots: gains and spending inside one interval "
        "cancel.",
        "decide() has no futility rule and no non-inferiority mode: it cannot show the arms are equivalent.",
    ]
    if report["early_stop"]:
        notes.insert(0, f"The trial stopped early on {report['early_stop']['day']}. The comparison is reported "
                        "as aborted; the decide() result is for reference only and must not be acted on.")
    if not report["game_versions"]:
        notes.append("No snapshot recorded a game version.")
    elif len(report["game_versions"]) > 1:
        notes.append("The game version changed inside the window; analyse each part separately.")
    for arm in report["arms"]:
        if arm["sample_size"] < report["min_sample"]:
            notes.append(f"{arm['arm']} has {arm['sample_size']} account-days, under the planned minimum of "
                         f"{report['min_sample']}.")
        if arm["excluded_days"]:
            notes.append(f"{arm['arm']}: {len(arm['excluded_days'])} snapshot interval(s) excluded from growth.")
    return notes


# -- output --------------------------------------------------------------------------------------


def to_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _cell(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return f"{_cell(value[0])} to {_cell(value[1])}"
    return str(value).replace("|", "/").replace("\n", " ")


def to_markdown(report: Mapping[str, Any]) -> str:
    decision = report["decision"]
    lines = [
        f"# Progression trial report: {report['trial_id']}",
        "",
        f"- Account: {report['account_alias']}",
        f"- Window: {report['window']['start']} to {report['window']['end']}",
        f"- Evaluated at: {report['evaluated_at']}",
        f"- Game version: {_cell(report['game_version'])}"
        + (f" (observed: {', '.join(report['game_versions'])})" if len(report["game_versions"]) > 1 else ""),
        f"- Primary metric: {report['metric']} ({GROWTH_SOURCE}), "
        f"{'higher' if report['higher_is_better'] else 'lower'} is better, minimum {report['min_sample']} per arm",
        f"- Status: **{report['status']}**",
        "",
    ]
    if report["early_stop"]:
        stop = report["early_stop"]
        lines += ["## Early stop", "", f"The trial stopped early on {stop['day']}. The comparison is aborted, and "
                  "the decision below is for reference only.", "", "| Arm | Threshold | Value | Limit |",
                  "| --- | --- | --- | --- |"]
        lines += [f"| {c['arm']} | {c['threshold']} | {_cell(c['value'])} | {_cell(c['limit'])} |"
                  for c in stop["crossings"]]
        lines.append("")
    lines += [
        "## Arms", "",
        "| Arm | Role | Account-days (n) | Growth/day | 95% interval | Verified success | Attempted (n) | "
        "Failed | Uncertain | Minerals/h gained | Minerals/h spent | Gas/h gained | Gas/h spent | "
        "Operator min/day | Minutes logged (n) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in report["arms"]:
        growth, success, res = arm["growth_velocity"], arm["verified_success"], arm["resources_per_hour"]
        rate = "n/a" if success["rate"] is None else f"{success['rate']:.0%}"
        lines.append(
            f"| {arm['arm']} | {arm['role']} | {arm['sample_size']} | {_cell(growth['mean'])} | "
            f"{_cell(growth['interval_95'])} | {rate} | {success['attempted']} | {arm['failures']['failed']} | "
            f"{arm['failures']['uncertain']} | {_cell(res['minerals']['gained_per_hour'])} | "
            f"{_cell(res['minerals']['spent_per_hour'])} | {_cell(res['gas']['gained_per_hour'])} | "
            f"{_cell(res['gas']['spent_per_hour'])} | {_cell(arm['operator_minutes']['mean'])} | "
            f"{arm['operator_minutes']['n']} |"
        )
    lines += ["", "Resources per hour are snapshot-ledger figures, measured the same way in both arms.",
              "", "## Journal resources (automated arms)", "",
              "| Arm | Samples | Hours | Minerals/h gained | Minerals/h spent | Gas/h gained | Gas/h spent | "
              "Starbux spent |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for arm in report["arms"]:
        res = arm["journal_resources"]
        lines.append(
            f"| {arm['arm']} | {res['samples']} | {_cell(res['hours'])} | "
            f"{_cell(res['minerals']['gained_per_hour'])} | {_cell(res['minerals']['spent_per_hour'])} | "
            f"{_cell(res['gas']['gained_per_hour'])} | {_cell(res['gas']['spent_per_hour'])} | "
            f"{_cell(res['starbux']['spent'])} |"
        )
    lines += [
        "", "## Decision", "",
        f"`decide()` returned **{decision['decision']}**: {decision['reason']}.",
        "",
        f"- {decision['baseline']} (control): n={decision['baseline_n']}, estimate "
        f"{_cell(decision['baseline_estimate'])}, 95% interval {_cell(decision['baseline_interval'])}",
        f"- {decision['candidate']} (candidate): n={decision['candidate_n']}, estimate "
        f"{_cell(decision['candidate_estimate'])}, 95% interval {_cell(decision['candidate_interval'])}",
        f"- Operator minutes saved per account-day: {_cell(report['operator_minutes_saved_per_account_day'])}",
        "",
    ]
    ledger = [(arm["arm"], failure) for arm in report["arms"] for failure in arm["failures"]["ledger"]]
    lines += ["## Failure ledger", ""]
    if ledger:
        lines += ["| Time | Arm | Action | Outcome | Error | Reason |", "| --- | --- | --- | --- | --- | --- |"]
        lines += [f"| {f['at']} | {arm} | {f['action_id']} | {f['outcome']} | {_cell(f['error_class'] or '')} | "
                  f"{_cell(f['reason'])} |" for arm, f in ledger]
    else:
        lines.append("_No failures recorded._")
    lines += ["", "## Interventions", ""]
    if report["interventions"]:
        lines += ["| Time | Arm | Kind | Action | Note |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {e['at']} | {_cell(e['arm'] or 'all')} | {e['kind']} | {e['action']} | {_cell(e['note'])} |"
                  for e in report["interventions"]]
    else:
        lines.append("_No interventions recorded._")
    lines += ["", "## Limitations", ""] + [f"- {note}" for note in report["limitations"]]
    return "\n".join(lines) + "\n"


# -- PvP strategy comparison (SPEC-08 R3-R6) -----------------------------------------------------

PVP_PLAN_KEYS = frozenset({"trial_id", "hypothesis", "account_alias", "strategies", "windows", "min_sample",
                           "effect", "game_version", "notes"})
REQUIRED_PVP_PLAN_KEYS = ("trial_id", "hypothesis", "account_alias", "strategies", "windows", "min_sample")
EFFECT_KEYS = frozenset({"baseline_win_rate", "minimum_effect", "power"})
HISTORY_KEYS = frozenset({"battle_id", "account_alias", "role", "outcome", "at", "trophy_change",
                          "opponent_hull_level", "opponent_trophies"})
REQUIRED_HISTORY_KEYS = ("battle_id", "account_alias", "role", "outcome", "at")
PVP_METRIC = "attack-win-rate"
UNKNOWN_COHORT_LABEL = f"{UNKNOWN_COHORT} / {UNKNOWN_COHORT}"
KNOWN_FROM_JOURNAL, KNOWN_FROM_HISTORY = "journal", "battle-history"
ATTACK_SOURCE = ("journaled run_pvp_battle events; an outcome is known when the event verified at tier "
                 "outcome-observed with no error, or a battle-history attacker record with its battle id exists")
DEFENSE_SOURCE = "battle-history defender records only"


def _strategy_for_key(registry: StrategyRegistry, key: Any, label: str) -> PvpStrategy:
    if not isinstance(key, str) or "@v" not in key:
        raise TrialInputError(f"{label} must be a strategy key such as weapon-focus-fire@v1")
    strategy_id, _, version = key.rpartition("@v")
    strategy = registry.get(strategy_id, int(version)) if version.isdigit() else None
    if strategy is None:
        raise TrialInputError(f"{label} names a strategy that is not registered")
    return strategy


@dataclass(frozen=True)
class PvpPlan:
    trial_id: str
    hypothesis: str
    account_alias: str
    strategies: tuple[PvpStrategy, PvpStrategy]
    schedule: ExperimentSchedule
    min_sample: int
    effect: Optional[Mapping[str, float]] = None
    game_version: Optional[str] = None

    @property
    def keys(self) -> tuple[str, str]:
        return self.strategies[0].key, self.strategies[1].key

    def study_plan(self) -> Optional[StudyPlan]:
        """The study plan that sizes the comparison, when the plan gives an effect size."""
        if self.effect is None:
            return None
        windows = self.schedule.windows_for(self.account_alias)
        return StudyPlan(
            study_id=self.trial_id, hypothesis=self.hypothesis, arms=self.keys, metric=PVP_METRIC,
            metric_kind=KIND_PROPORTION, higher_is_better=True, baseline_value=self.effect["baseline_win_rate"],
            minimum_effect=self.effect["minimum_effect"], unit="battle",
            window_start=min(w.start for w in windows), window_end=max(w.end for w in windows),
            power=self.effect.get("power", 0.80), min_sample=max(2, self.min_sample),
        )

    def required_sample(self) -> tuple[int, str]:
        """Known-result battles each strategy needs, and where the number came from (S08-R4)."""
        study = self.study_plan()
        if study is None:
            return self.min_sample, "plan min_sample (no effect size given)"
        return per_arm_min_sample(study), "sdk.experiments.study.per_arm_min_sample"


def pvp_plan_from_dict(payload: Mapping[str, Any], registry: Optional[StrategyRegistry] = None) -> PvpPlan:
    if not isinstance(payload, Mapping):
        raise TrialInputError("the PvP plan must be a JSON object")
    _refuse_secrets(payload, "the PvP plan")
    registry = registry if registry is not None else candidate_registry()
    unknown = sorted(set(payload) - PVP_PLAN_KEYS)
    if unknown:
        raise TrialInputError(f"unknown PvP plan keys: {', '.join(unknown)}")
    missing = [key for key in REQUIRED_PVP_PLAN_KEYS if key not in payload]
    if missing:
        raise TrialInputError(f"the PvP plan is missing: {', '.join(missing)}")
    alias = payload["account_alias"]
    if not isinstance(alias, str) or not ACCOUNT_ALIAS_PATTERN.match(alias):
        raise TrialInputError("account_alias must be a lowercase alias, not an e-mail or display name")
    if not isinstance(payload["hypothesis"], str) or not payload["hypothesis"].strip():
        raise TrialInputError("hypothesis is required")
    keys = payload["strategies"]
    if not isinstance(keys, list) or len(keys) != 2:
        raise TrialInputError("strategies must list exactly two strategy keys")
    strategies = tuple(_strategy_for_key(registry, key, "each strategy") for key in keys)
    if strategies[0].key == strategies[1].key:
        raise TrialInputError("the two strategies must differ")
    windows = payload["windows"]
    if not isinstance(windows, list) or not windows:
        raise TrialInputError("windows must be a non-empty list")
    schedule = ExperimentSchedule(registry)
    for number, item in enumerate(windows, start=1):
        if not isinstance(item, Mapping) or set(item) != {"strategy", "start", "end"}:
            raise TrialInputError(f"window {number} needs exactly strategy, start and end")
        strategy = _strategy_for_key(registry, item["strategy"], f"window {number} strategy")
        if strategy.key not in (s.key for s in strategies):
            raise TrialInputError(f"window {number} names a strategy that the plan does not compare")
        schedule.add(ExperimentWindow(alias, strategy.strategy_id, strategy.version,
                                      _utc(item["start"], f"window {number} start"),
                                      _utc(item["end"], f"window {number} end")))
    min_sample = payload["min_sample"]
    if isinstance(min_sample, bool) or not isinstance(min_sample, int) or min_sample < 1:
        raise TrialInputError("min_sample must be an integer of at least 1")
    effect = payload.get("effect")
    if effect is not None:
        if not isinstance(effect, Mapping) or not {"baseline_win_rate", "minimum_effect"} <= set(effect):
            raise TrialInputError("effect needs baseline_win_rate and minimum_effect")
        unknown = sorted(set(effect) - EFFECT_KEYS)
        if unknown:
            raise TrialInputError(f"unknown effect keys: {', '.join(unknown)}")
        if not all(_number(value) for value in effect.values()):
            raise TrialInputError("effect values must be numbers")
        effect = dict(effect)
    game_version = payload.get("game_version")
    if game_version is not None and (not isinstance(game_version, str) or not game_version.strip()):
        raise TrialInputError("game_version must be text or null")
    plan = PvpPlan(
        trial_id=_identifier(payload["trial_id"], "trial_id"), hypothesis=payload["hypothesis"],
        account_alias=alias, strategies=strategies, schedule=schedule, min_sample=min_sample,
        effect=effect, game_version=game_version,
    )
    plan.required_sample()  # the StudyPlan checks the effect size, power and window
    return plan


@dataclass(frozen=True)
class HistoryRecord:
    """One parsed battle-history record (format documented in the module docstring)."""

    battle_id: str
    account_alias: str
    role: str
    outcome: str
    at: datetime
    trophy_change: Optional[int] = None
    opponent_hull_level: Optional[int] = None
    opponent_trophies: Optional[int] = None


def _history_int(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TrialInputError(f"{label} must be an integer or null")
    return value


def history_from_list(payload: Any, account_alias: str) -> list[HistoryRecord]:
    if not isinstance(payload, list):
        raise TrialInputError("battle history must be a JSON list")
    _refuse_secrets(payload, "battle history")
    records, seen = [], set()
    for number, item in enumerate(payload, start=1):
        if not isinstance(item, Mapping):
            raise TrialInputError(f"battle-history record {number} must be a JSON object")
        missing = [key for key in REQUIRED_HISTORY_KEYS if key not in item]
        if missing:
            raise TrialInputError(f"battle-history record {number} is missing: {', '.join(missing)}")
        unknown = sorted(set(item) - HISTORY_KEYS)
        if unknown:
            raise TrialInputError(f"battle-history record {number} has unknown keys: {', '.join(unknown)}")
        if item["account_alias"] != account_alias:
            raise AccountMismatchError(f"battle-history record {number} belongs to another account")
        battle_id = item["battle_id"]
        if not isinstance(battle_id, str) or not battle_id.strip() or len(battle_id) > 64:
            raise TrialInputError(f"battle-history record {number} battle_id must be a short non-empty string")
        if item["role"] not in {role.value for role in BattleRole}:
            raise TrialInputError(f"battle-history record {number} role must be attacker or defender")
        if item["outcome"] not in {outcome.value for outcome in BattleOutcome}:
            raise TrialInputError(f"battle-history record {number} outcome is not a battle outcome")
        hull = _history_int(item.get("opponent_hull_level"), f"battle-history record {number} opponent_hull_level")
        if hull is not None and not 1 <= hull <= 20:
            raise TrialInputError(f"battle-history record {number} opponent_hull_level must be 1-20")
        if (battle_id, item["role"]) in seen:
            raise TrialInputError(f"battle-history record {number} repeats a battle id for its role")
        seen.add((battle_id, item["role"]))
        records.append(HistoryRecord(
            battle_id=battle_id, account_alias=account_alias, role=item["role"], outcome=item["outcome"],
            at=_utc(item["at"], f"battle-history record {number} at"),
            trophy_change=_history_int(item.get("trophy_change"), f"battle-history record {number} trophy_change"),
            opponent_hull_level=hull,
            opponent_trophies=_history_int(item.get("opponent_trophies"),
                                           f"battle-history record {number} opponent_trophies"),
        ))
    return sorted(records, key=lambda record: (record.at, record.battle_id, record.role))


@dataclass(frozen=True)
class AttributedBattle:
    battle: JournalBattle
    attribution: Optional[StrategyAttribution]
    known_from: Optional[str]  # journal, battle-history, or None when the outcome is unknown


def _attribute(battle: JournalBattle, attribution: Optional[StrategyAttribution]) -> JournalBattle:
    strategy = attribution.strategy if attribution else None
    result = replace(battle.result,
                     battle_strategy=strategy.battle_strategy if strategy else BattleStrategy.UNKNOWN.value,
                     ship_config_version=strategy.config_version if strategy else "")
    return replace(battle, result=result, strategy_key=strategy.key if strategy else None,
                   strategy_config_version=strategy.config_version if strategy else None)


def _as_utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def attack_battles(plan: PvpPlan, trial: TrialRecord, events: Iterable[GameplayEvent],
                   history: Sequence[HistoryRecord] = ()) -> tuple[list[AttributedBattle], dict[str, int]]:
    """The account's journaled attacks with their strategy and outcome evidence (S08-R3, S03-R5)."""
    by_id = {record.battle_id: record for record in history if record.role == BattleRole.ATTACKER.value}
    matched, contradicted, seen = set(), 0, set()
    battles = []
    for battle in battles_from_events(e for e in events if e.account_alias == plan.account_alias):
        battle_id = battle.result.battle_id
        if battle_id in seen:
            raise TrialInputError("the journals repeat a battle id; pass each journal once")
        seen.add(battle_id)
        known_from = KNOWN_FROM_JOURNAL if battle.outcome_known else None
        record = by_id.get(battle_id)
        if record is not None:
            matched.add(battle_id)
            outcome = battle.result.outcome
            if record.outcome != BattleOutcome.UNKNOWN.value:
                if not battle.outcome_known:
                    outcome, known_from = record.outcome, KNOWN_FROM_HISTORY
                elif record.outcome != outcome:  # the two sources disagree: neither is trusted
                    outcome, known_from, contradicted = BattleOutcome.UNKNOWN.value, None, contradicted + 1
            trophies = record.trophy_change if record.trophy_change is not None else battle.trophy_delta
            hull = (record.opponent_hull_level if record.opponent_hull_level is not None
                    else battle.result.opponent_hull_level)
            battle = replace(
                battle,
                result=replace(battle.result, outcome=outcome, trophy_delta=trophies or 0, opponent_hull_level=hull),
                trophy_delta=trophies,
                opponent_hull_bracket=hull_bracket(hull),
                opponent_trophy_bracket=(trophy_bracket(record.opponent_trophies)
                                         if record.opponent_trophies is not None else battle.opponent_trophy_bracket),
            )
        attribution = trial.attribution_at(plan.account_alias, _as_utc(battle.started_at))
        battles.append(AttributedBattle(_attribute(battle, attribution), attribution, known_from))
    counts = {"contradicted": contradicted, "history_attacks_without_journal": len(set(by_id) - matched)}
    return battles, counts


def defense_battles(plan: PvpPlan, trial: TrialRecord, history: Sequence[HistoryRecord]) -> list[AttributedBattle]:
    """Defenses from battle-history defender records only, credited to the strategy in force (S08-R5)."""
    battles = []
    for record in history:
        if record.role != BattleRole.DEFENDER.value:
            continue
        at = record.at.isoformat()
        battle = JournalBattle(
            result=BattleResult(battle_id=record.battle_id, account_pseudonym=plan.account_alias,
                                role=BattleRole.DEFENDER.value, opponent_hull_level=record.opponent_hull_level,
                                outcome=record.outcome, trophy_delta=record.trophy_change or 0, created_at=at),
            attempt_id="", evidence_tier=KNOWN_FROM_HISTORY, trophy_delta=record.trophy_change,
            strategy_key=None, strategy_config_version=None,
            opponent_hull_bracket=hull_bracket(record.opponent_hull_level),
            opponent_trophy_bracket=trophy_bracket(record.opponent_trophies), started_at=at, finished_at=at,
        )
        attribution = trial.attribution_at(plan.account_alias, record.at)
        known_from = None if record.outcome == BattleOutcome.UNKNOWN.value else KNOWN_FROM_HISTORY
        battles.append(AttributedBattle(_attribute(battle, attribution), attribution, known_from))
    return battles


def _strategy_summary(strategy: PvpStrategy, battles: Sequence[AttributedBattle], stats: RoleStats,
                      with_cost: bool) -> dict[str, Any]:
    known = [b for b in battles if b.battle.outcome_known]
    trophies = [b.battle.trophy_delta for b in known if b.battle.trophy_delta is not None]
    summary = {
        "strategy": strategy.key,
        "config_version": strategy.config_version,
        "battles": stats.battles,
        "known": stats.known,
        "unknown": stats.unknown,
        "wins": stats.wins,
        "losses": stats.losses,
        "other_known": stats.known - stats.wins - stats.losses,
        "win_rate": stats.win_rate,
        "interval_95": stats.interval,
        "known_from_journal": sum(1 for b in known if b.known_from == KNOWN_FROM_JOURNAL),
        "known_from_battle_history": sum(1 for b in known if b.known_from == KNOWN_FROM_HISTORY),
        "net_trophies": sum(trophies) if trophies else None,
        "trophy_samples": len(trophies),
        "adoption_methods": sorted({b.attribution.method for b in battles if b.attribution}),
        "approximate_timing": any(b.attribution.approximate_timing for b in battles if b.attribution),
    }
    if with_cost:
        spent, samples = observed_spend(b.battle for b in battles)
        summary["resource_cost"] = {"source": "journal costs of attempted battles (negative deltas)",
                                    "samples": samples, "spent": spent if samples else None}
    return summary


def _role_section(plan: PvpPlan, battles: Sequence[AttributedBattle], role: str, min_sample: int,
                  source: str, with_cost: bool) -> dict[str, Any]:
    comparison = compare_strategies([b.battle for b in battles], *plan.keys, account_alias=plan.account_alias,
                                    role=role, min_sample=min_sample)
    stats = {plan.keys[0]: comparison.a, plan.keys[1]: comparison.b}
    attributed = [b for b in battles if b.battle.strategy_key in stats]
    unattributed = [b for b in battles if b.battle.strategy_key not in stats]
    cohorts = sorted({cohort_label(b.battle) for b in attributed if b.battle.outcome_known} - {UNKNOWN_COHORT_LABEL})
    return {
        "source": source,
        "battles": len(battles),
        "known": sum(1 for b in battles if b.battle.outcome_known),
        "unknown": sum(1 for b in battles if not b.battle.outcome_known),
        "unattributed": {"battles": len(unattributed),
                         "known": sum(1 for b in unattributed if b.battle.outcome_known),
                         "unknown": sum(1 for b in unattributed if not b.battle.outcome_known)},
        "per_strategy": [_strategy_summary(strategy, [b for b in attributed if b.battle.strategy_key == strategy.key],
                                           stats[strategy.key], with_cost) for strategy in plan.strategies],
        "stratified": bool(cohorts),
        "cohorts_observed": cohorts,
        "comparison": asdict(comparison),
    }


def build_pvp_report(
    plan: PvpPlan,
    events: Sequence[GameplayEvent],
    adoptions: AdoptionLog,
    history: Optional[Sequence[HistoryRecord]] = None,
    *,
    now: datetime,
) -> dict[str, Any]:
    now = _utc(now, "now")
    for record in history or ():
        if record.account_alias != plan.account_alias:
            raise AccountMismatchError("a battle-history record belongs to another account; accounts never mix")
    trial = TrialRecord(plan.schedule, adoptions)
    min_sample, sample_source = plan.required_sample()
    attacks, counts = attack_battles(plan, trial, events, history or ())
    attack = _role_section(plan, attacks, BattleRole.ATTACKER.value, min_sample, ATTACK_SOURCE, with_cost=True)
    defense = None
    if history is not None:
        defenses = defense_battles(plan, trial, history)
        defense = _role_section(plan, defenses, BattleRole.DEFENDER.value, min_sample, DEFENSE_SOURCE,
                                with_cost=False)
    own_adoptions = adoptions.adoptions_for(plan.account_alias)
    comparison = attack["comparison"]
    study = plan.study_plan()
    report = {
        "report": "pvp-strategy-comparison",
        "trial_id": plan.trial_id,
        "account_alias": plan.account_alias,
        "hypothesis": plan.hypothesis,
        "evaluated_at": now.isoformat(),
        "game_version": plan.game_version,
        "metric": PVP_METRIC,
        "strategies": [{"key": s.key, "config_version": s.config_version, "battle_strategy": s.battle_strategy}
                       for s in plan.strategies],
        "windows": [{"strategy": f"{w.strategy_id}@v{w.strategy_version}", "start": w.start.isoformat(),
                     "end": w.end.isoformat()} for w in plan.schedule.windows_for(plan.account_alias)],
        "min_sample": min_sample,
        "min_sample_source": sample_source,
        "study_plan": None if study is None else {
            "baseline_win_rate": study.baseline_value, "minimum_effect": study.minimum_effect,
            "power": study.power, "floor": study.min_sample, "decision_rule": study.decision_rule,
        },
        "verdict": comparison["verdict"],
        "recommendation": comparison["recommended"],
        "attack": {**attack, "contradicted": counts["contradicted"]},
        "defense": defense,
        "battle_history": {
            "provided": history is not None,
            "records": len(history or ()),
            "attacker_records": sum(1 for r in history or () if r.role == BattleRole.ATTACKER.value),
            "defender_records": sum(1 for r in history or () if r.role == BattleRole.DEFENDER.value),
            "attacks_without_journal": counts["history_attacks_without_journal"],
        },
        "cross_account_events": sum(1 for e in events if e.account_alias != plan.account_alias
                                    and e.action_id == BATTLE_ACTION_ID),
        "adoptions": [a.to_dict() for a in own_adoptions],
        "approximate_timing": any(a.approximate_timing for a in own_adoptions),
    }
    report["limitations"] = _pvp_limitations(report)
    _refuse_secrets(report, "the PvP report")
    return report


def _pvp_limitations(report: Mapping[str, Any]) -> list[str]:
    attack = report["attack"]
    notes = [
        "Strategy windows run one after another, so matchmaking or game changes between windows are "
        "confounded with the strategy.",
        "Battles against the same opponent are not independent, so the 95% intervals are optimistic.",
        f"{attack['unknown']} attack(s) had an unknown outcome; they are counted but never treated as wins "
        "or losses.",
        "Net trophies sum the observed trophy change of known-outcome battles only.",
        "The battle-history format is provisional until the SPEC-03 listPvPBattles2 parser exists.",
    ]
    if report["verdict"] != "recommend":
        notes.insert(0, f"No recommendation: the attack comparison is {report['verdict']} "
                        f"({attack['comparison']['reason']}).")
    if report["approximate_timing"]:
        notes.append("Strategies were applied by hand: adoption times are the owner's notes, so battles near an "
                     "adoption may be credited to the wrong strategy (approximate timing).")
    if not report["adoptions"]:
        notes.append("No strategy adoption is recorded for this account, so no battle has a strategy.")
    if not attack["stratified"]:
        notes.append("No opponent cohort was observed, so the comparison is not stratified by opponent.")
    if attack["unattributed"]["battles"]:
        notes.append(f"{attack['unattributed']['battles']} attack(s) fell outside every recorded strategy "
                     "application and have no strategy.")
    if attack["contradicted"]:
        notes.append(f"{attack['contradicted']} attack(s) had a journal outcome that battle history contradicts; "
                     "they are counted as unknown.")
    if report["game_version"] is None:
        notes.append("The plan records no game version.")
    if report["defense"] is None:
        notes.append("No battle history was given: defense results are not reported, and attack outcomes rest "
                     "on journal evidence alone.")
    elif report["battle_history"]["attacks_without_journal"]:
        notes.append(f"{report['battle_history']['attacks_without_journal']} battle-history attack(s) have no "
                     "journaled battle and are left out of the comparison.")
    if report["cross_account_events"]:
        notes.append(f"{report['cross_account_events']} battle event(s) of another account were left out; "
                     "accounts never mix.")
    for row in attack["per_strategy"]:
        if row["known"] < report["min_sample"]:
            notes.append(f"{row['strategy']} has {row['known']} known-result attack(s), under the required "
                         f"{report['min_sample']}.")
        if row["resource_cost"]["spent"] is None:
            notes.append(f"{row['strategy']}: no battle recorded a resource cost.")
    return notes


def _rate_cell(row: Mapping[str, Any]) -> str:
    if row["win_rate"] is None:
        return "n/a"
    low, high = row["interval_95"]
    return f"{row['win_rate']:.0%} ({low:.0%} to {high:.0%})"


def _pvp_rows(section: Mapping[str, Any], with_cost: bool) -> list[str]:
    header = "| Strategy | Battles | Known | Unknown | Wins | Losses | Win rate (95% CI) | Net trophies (n) | Applied |"
    rule = "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    if with_cost:
        header, rule = header + " Resources spent (n) |", rule + " --- |"
    lines = [header, rule]
    for row in section["per_strategy"]:
        net = "n/a" if row["net_trophies"] is None else f"{row['net_trophies']:+d} ({row['trophy_samples']})"
        applied = ", ".join(row["adoption_methods"]) or "n/a"
        if row["approximate_timing"]:
            applied += " (approximate timing)"
        line = (f"| {row['strategy']} | {row['battles']} | {row['known']} | {row['unknown']} | {row['wins']} | "
                f"{row['losses']} | {_rate_cell(row)} | {net} | {applied} |")
        if with_cost:
            cost = row["resource_cost"]
            spent = "n/a" if cost["spent"] is None else (
                ", ".join(f"{name} {_cell(value)}" for name, value in sorted(cost["spent"].items())) or "none")
            line += f" {spent} ({cost['samples']}) |"
        lines.append(line)
    unattributed = section["unattributed"]
    comparison = section["comparison"]
    lines += [
        "",
        f"Unattributed: {unattributed['battles']} battle(s), {unattributed['unknown']} with an unknown outcome. "
        f"Stratified by opponent cohort: {'yes' if section['stratified'] else 'no'}"
        + (f" ({', '.join(section['cohorts_observed'])})." if section["cohorts_observed"] else "."),
        "",
        f"`compare_strategies` ({comparison['role']}, min_sample {comparison['min_sample']}): "
        f"**{comparison['verdict']}**. {comparison['reason']}.",
    ]
    return lines


def pvp_to_markdown(report: Mapping[str, Any]) -> str:
    recommendation = report["recommendation"]
    lines = [
        f"# PvP strategy comparison: {report['trial_id']}",
        "",
        f"- Account: {report['account_alias']}",
        f"- Evaluated at: {report['evaluated_at']}",
        f"- Game version: {_cell(report['game_version'])}",
        f"- Strategies: {', '.join(s['key'] for s in report['strategies'])}",
        f"- Minimum known-result battles per strategy: {report['min_sample']} ({report['min_sample_source']})",
        f"- Verdict: **{report['verdict']}**",
        f"- Recommendation: {recommendation if recommendation else 'none (' + report['verdict'] + ')'}",
        "",
        "## Windows", "", "| Strategy | Start | End |", "| --- | --- | --- |",
    ]
    lines += [f"| {w['strategy']} | {w['start']} | {w['end']} |" for w in report["windows"]]
    attack = report["attack"]
    lines += ["", "## Attacks", "", f"Source: {attack['source']}.", "",
              f"{attack['battles']} attack(s): {attack['known']} known, {attack['unknown']} unknown, "
              f"{attack['contradicted']} contradicted by battle history.", ""]
    lines += _pvp_rows(attack, with_cost=True)
    lines += ["", "## Defenses", ""]
    if report["defense"] is None:
        lines.append("_No battle history given; defenses are not reported._")
    else:
        lines += [f"Source: {report['defense']['source']}. Defenses never drive the recommendation.", ""]
        lines += _pvp_rows(report["defense"], with_cost=False)
    lines += ["", "## Strategy applications", ""]
    if report["adoptions"]:
        lines += ["| Time | Strategy | Method | Note |", "| --- | --- | --- | --- |"]
        lines += [f"| {a['adopted_at']} | {a['strategy_id']}@v{a['strategy_version']} | {a['method']} | "
                  f"{_cell(a['note'])} |" for a in report["adoptions"]]
    else:
        lines.append("_No strategy application recorded._")
    lines += ["", "## Limitations", ""] + [f"- {note}" for note in report["limitations"]]
    return "\n".join(lines) + "\n"


def load_journals(paths: Sequence[Path]) -> list[GameplayEvent]:
    """Every event in the given journal files; a missing file is an error, not an empty journal."""
    events = []
    for path in paths:
        if not path.is_file():
            raise TrialInputError("a --journal path is not a file")
        events.extend(read_events(path))
    return events


# -- command line --------------------------------------------------------------------------------


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TrialInputError(f"cannot read {label} ({type(error).__name__})") from None


def load_snapshots(path: Path) -> list[AccountSnapshot]:
    """A JSON file holding one snapshot or a list of them, or a directory of such files."""
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    snapshots = []
    for file in files:
        payload = _read_json(file, "a snapshot file")
        for record in payload if isinstance(payload, list) else [payload]:
            snapshots.append(snapshot_from_dict(record))
    return snapshots


def _arm_paths(values: Sequence[str], flag: str) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for value in values:
        arm, sep, path = value.partition("=")
        if not sep or not path:
            raise TrialInputError(f"{flag} takes ARM=PATH")
        if arm in paths:
            raise TrialInputError(f"{flag} repeats an arm")
        paths[arm] = Path(path)
    return paths


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the progression trial exit report (SPEC-07), or with --pvp the PvP strategy "
                    "comparison (SPEC-08).")
    parser.add_argument("--plan", required=True, type=Path, help="trial plan JSON (a PvP plan with --pvp)")
    parser.add_argument("--pvp", action="store_true", help="build the PvP strategy comparison (SPEC-08)")
    parser.add_argument("--snapshots", action="append", default=[], metavar="ARM=PATH",
                        help="an arm's snapshot JSON file or directory (repeat per arm)")
    parser.add_argument("--journal", action="append", default=[], metavar="ARM=PATH",
                        help="an arm's gameplay journal JSONL (repeat per arm); with --pvp, a journal PATH "
                             "of the trial account (repeatable)")
    parser.add_argument("--minutes", type=Path, help="operator minutes per account-day JSON")
    parser.add_argument("--interventions", type=Path, help="intervention log JSON")
    parser.add_argument("--adoptions", type=Path, help="with --pvp: the strategy adoption log JSON")
    parser.add_argument("--battle-history", type=Path, dest="battle_history",
                        help="with --pvp: parsed battle-history records JSON")
    parser.add_argument("--now", help="evaluation time, ISO 8601 UTC (default: the current time)")
    parser.add_argument("--json", type=Path, dest="json_out", help="write the JSON report here")
    parser.add_argument("--markdown", type=Path, help="write the Markdown report here")
    args = parser.parse_args(argv)
    render = to_markdown
    try:
        now = _utc(args.now, "--now") if args.now else datetime.now(timezone.utc)
        if args.pvp:
            report, render = _pvp_from_args(args, now), pvp_to_markdown
        else:
            if args.adoptions or args.battle_history:
                raise TrialInputError("--adoptions and --battle-history need --pvp")
            plan = plan_from_dict(_read_json(args.plan, "the trial plan"))
            snapshots = {arm: load_snapshots(path)
                         for arm, path in _arm_paths(args.snapshots, "--snapshots").items()}
            journals = {arm: read_events(path) for arm, path in _arm_paths(args.journal, "--journal").items()}
            minutes = minutes_from_list(_read_json(args.minutes, "operator minutes"), plan) if args.minutes else {}
            interventions = (interventions_from_list(_read_json(args.interventions, "the intervention log"), plan)
                             if args.interventions else [])
            report = build_trial_report(plan, snapshots, journals, minutes, interventions, now=now)
    except ValueError as error:
        print(f"trial_report: error: {error}", file=sys.stderr)
        return 2
    if args.json_out:
        args.json_out.write_text(to_json(report), encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(render(report), encoding="utf-8")
    if not args.json_out and not args.markdown:
        sys.stdout.write(render(report))
    return 0


def _pvp_from_args(args: argparse.Namespace, now: datetime) -> dict[str, Any]:
    if args.snapshots or args.minutes or args.interventions:
        raise TrialInputError("--snapshots, --minutes and --interventions do not apply to --pvp")
    if not args.adoptions:
        raise TrialInputError("--pvp needs --adoptions (the strategy adoption log)")
    plan = pvp_plan_from_dict(_read_json(args.plan, "the PvP plan"))
    adoptions = AdoptionLog.from_dict(_read_json(args.adoptions, "the adoption log"))
    history = (history_from_list(_read_json(args.battle_history, "battle history"), plan.account_alias)
               if args.battle_history else None)
    return build_pvp_report(plan, load_journals([Path(value) for value in args.journal]), adoptions, history,
                            now=now)


if __name__ == "__main__":
    raise SystemExit(main())
