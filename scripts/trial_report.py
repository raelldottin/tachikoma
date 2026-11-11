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

Offline only: it reads files and makes no network call.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdk.economy.ledger import CURRENCIES, build_ledger  # noqa: E402
from sdk.experiments import Experiment, MeanSample, ProportionSample, decide  # noqa: E402
from sdk.experiments.loop import ID_PATTERN  # noqa: E402
from sdk.journal import GameplayEvent, read_events  # noqa: E402
from sdk.redaction import redact_secrets  # noqa: E402
from sdk.reports.progression import compute_progression_metrics  # noqa: E402
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
    parser = argparse.ArgumentParser(description="Build the progression trial exit report (SPEC-07).")
    parser.add_argument("--plan", required=True, type=Path, help="trial plan JSON")
    parser.add_argument("--snapshots", action="append", default=[], metavar="ARM=PATH",
                        help="an arm's snapshot JSON file or directory (repeat per arm)")
    parser.add_argument("--journal", action="append", default=[], metavar="ARM=PATH",
                        help="an arm's gameplay journal JSONL (repeat per arm)")
    parser.add_argument("--minutes", type=Path, help="operator minutes per account-day JSON")
    parser.add_argument("--interventions", type=Path, help="intervention log JSON")
    parser.add_argument("--now", help="evaluation time, ISO 8601 UTC (default: the current time)")
    parser.add_argument("--json", type=Path, dest="json_out", help="write the JSON report here")
    parser.add_argument("--markdown", type=Path, help="write the Markdown report here")
    args = parser.parse_args(argv)
    try:
        plan = plan_from_dict(_read_json(args.plan, "the trial plan"))
        snapshots = {arm: load_snapshots(path) for arm, path in _arm_paths(args.snapshots, "--snapshots").items()}
        journals = {arm: read_events(path) for arm, path in _arm_paths(args.journal, "--journal").items()}
        minutes = minutes_from_list(_read_json(args.minutes, "operator minutes"), plan) if args.minutes else {}
        interventions = (interventions_from_list(_read_json(args.interventions, "the intervention log"), plan)
                         if args.interventions else [])
        now = _utc(args.now, "--now") if args.now else datetime.now(timezone.utc)
        report = build_trial_report(plan, snapshots, journals, minutes, interventions, now=now)
    except ValueError as error:
        print(f"trial_report: error: {error}", file=sys.stderr)
        return 2
    if args.json_out:
        args.json_out.write_text(to_json(report), encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(to_markdown(report), encoding="utf-8")
    if not args.json_out and not args.markdown:
        sys.stdout.write(to_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
