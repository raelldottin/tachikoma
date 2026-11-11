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

With ``--fleet`` it builds study D's fleet trial report (SPEC-10 R3-R6) from:

- a fleet plan (JSON): ``trial_id``, ``hypothesis``, ``accounts`` (the two
  registry aliases), ``schedule`` (a list of ``mode`` - ``sequential`` or
  ``concurrent`` - ``start`` and ``end`` in UTC, fixed before the trial, R2),
  ``min_sample`` (runs per mode), ``window_start``, ``window_end`` and
  optionally ``min_attempts`` (attempts per mode for the rate comparisons;
  default ``min_sample``), ``concurrency`` (default 2), ``manual_minutes``
  (each account's manual routine minutes per run) and ``notes``;
- the account registry (``sdk/fleet/accounts.py`` JSON);
- one or more ``--run`` paths: an execute journal directory (its
  ``<alias>.log`` journals and every run record under ``runs/``), or one run
  record file ``<journal dir>/runs/<UTC time>.json``;
- optionally operator minutes (JSON list of ``run_id`` - the run record's
  file name without ``.json`` - ``account_alias`` and ``operator_minutes``).

The run record is the one ``run.py --fleet-execute`` writes. The report reads
only this subset and ignores every other field: ``started_at`` and
``finished_at`` (UTC, required); ``accounts`` (required: a list of objects
with ``alias`` and ``status``, and optionally ``reason``, ``game_identity``
and ``attempted``); ``version`` (1 when present); ``kind``
(``fleet-execute`` when present); ``orchestrator`` (``sequential`` or
``concurrent``: the mode that ran); ``mode`` (``canary`` or ``full``, or the
orchestrator mode when ``orchestrator`` is absent); ``authorization_hash``
and ``registry_hash`` (``sha256:`` hashes, reported by their first 16 hex
digits); ``policy_versions`` (alias to policy id) or ``policy_version``;
``game_version`` (text, or an object with ``client`` and ``server``); and
``stop_reason``. A run's mode is the record's orchestrator mode, else the
plan's schedule at the run's start; a record that contradicts the schedule
is listed as a deviation.

Each run is audited with ``audit_fleet`` over every journal event since the
previous run record in the same directory (so the run's dry run and anything
journaled between runs are audited too) up to the run's end (R3). A run
whose record shows an account with no declared fingerprint, or a game
identity other than the registry's, adds an ``identity-not-enforced``
incident (S10-R1 is enforced by the execute command; this only checks its
records). The first run with any incident stops the trial: it is listed with
its incidents, and later runs are left out of the comparison. Per run, the
events inside the run's start and end feed ``compute_fleet_report`` (R4;
time saved only when the plan's manual minutes and the run's operator
minutes are both given for an account) and ``evaluate_alerts`` at the run's
end (R6). The modes are compared (R5), ``sequential`` as the baseline and
``concurrent`` as the candidate, with ``decide()`` on verified actions per
run-hour (one value per run), failure rate and uncertain rate (per attempt),
next to incident counts and sample sizes.

With ``--study`` it aggregates the per-trial outputs into the figures of the
study report (SPEC-12 R1, R4, R6). Each ``--trial PATH`` is one JSON output,
recognised by its content: a progression report (study A, this script's
``--json`` output), a ``--pvp`` report (study C), a ``--fleet`` report (study
D) or a market trial result (the R4 market trial, format below); at most one
per study. For every study, in the fixed order A, C, D, R4-market, the report
states the question, arms, windows, game versions, sample sizes and minimum,
the primary result per arm with its 95% interval, the ``decide()`` outcome
(regenerated from each strategy's pooled wins and known attacks for study C,
whose report has none, and not applicable to a one-arm market trial), costs,
failures and the trial's limitations (R1). A study without an output, short
of its minimum sample, or whose ``decide()`` gave neither ``adopt`` nor
``reject``, is ``inconclusive`` with its reasons, and never left out (R4); a
trial that stopped early is ``aborted``. The figures come only from the given
outputs, keyed by file name and the first 16 hex digits of the file's
SHA-256, and no current time enters the report, so the same outputs give the
same JSON and Markdown byte for byte (R6). ``--study`` takes no other input.

Market trial result format (version 1, provisional until SPEC-09 adds
``--market``): a JSON object with ``report`` (``market-trial``),
``format_version`` (1), ``trial_id``, ``hypothesis``, ``account_alias``,
``arms`` (one or two variant ids, the baseline first), ``currency`` (every
amount is in it), ``min_sample`` (completed trades per arm), ``window_start``,
``window_end`` and ``evaluated_at`` (UTC), ``trades`` and optionally
``operator_minutes`` and ``notes``. Each trade has ``trade_id``, ``side``
(``sell`` or ``buy``), ``state`` (as ``reconcile_trade`` decided it: a sell is
``requested``, ``listed``, ``completed``, ``expired`` or ``unknown``; a buy
``requested``, ``completed`` or ``unknown``), ``at`` (UTC, inside the window)
and ``price``, and optionally ``arm`` (required with two arms),
``item_design_id``, ``quantity``, ``proceeds``, ``cost``, ``fees`` (integers
or ``null``) and ``game_version``. A completed sell needs its proceeds and a
completed buy its cost. Market return counts completed trades only: sell
proceeds minus buy cost; the primary metric is that signed amount per
completed trade.

Offline only: it reads files and makes no network call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdk.economy.ledger import CURRENCIES, build_ledger  # noqa: E402
from sdk.economy.trades import (  # noqa: E402
    BUY as TRADE_BUY,
    COMPLETED as TRADE_COMPLETED,
    EXPIRED as TRADE_EXPIRED,
    LISTED as TRADE_LISTED,
    REQUESTED as TRADE_REQUESTED,
    SELL as TRADE_SELL,
    UNKNOWN as TRADE_UNKNOWN,
)
from sdk.experiments import (  # noqa: E402
    KIND_PROPORTION,
    Experiment,
    MeanSample,
    ProportionSample,
    StudyPlan,
    decide,
    per_arm_min_sample,
)
from sdk.experiments.loop import ADOPT, ID_PATTERN, REJECT, Decision  # noqa: E402
from sdk.fleet.accounts import AccountRegistry, load_registry  # noqa: E402
from sdk.fleet.audit import audit_fleet  # noqa: E402
from sdk.fleet.concurrent import MODE_CONCURRENT  # noqa: E402
from sdk.fleet.orchestrator import MODE_SEQUENTIAL, AccountRun, FleetRun  # noqa: E402
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
from sdk.reports.alerts import evaluate_alerts  # noqa: E402
from sdk.reports.battles import (  # noqa: E402
    BATTLE_ACTION_ID,
    UNKNOWN_COHORT,
    JournalBattle,
    battles_from_events,
    hull_bracket,
    trophy_bracket,
)
from sdk.reports.fleet import compute_fleet_report  # noqa: E402
from sdk.reports.progression import compute_progression_metrics  # noqa: E402
from sdk.reports.pvp import RoleStats, cohort_label, compare_strategies, observed_spend  # noqa: E402
from sdk.state.snapshot import (  # noqa: E402
    ACCOUNT_ALIAS_PATTERN,
    SECTIONS,
    AccountMismatchError,
    GAME_IDENTITY_PATTERN,
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


# -- fleet trial (SPEC-10 R3-R6) -----------------------------------------------------------------

FLEET_MODES = (MODE_SEQUENTIAL, MODE_CONCURRENT)
FLEET_BASELINE, FLEET_CANDIDATE = MODE_SEQUENTIAL, MODE_CONCURRENT
EXECUTE_MODES = ("canary", "full")
FLEET_PLAN_KEYS = frozenset({"trial_id", "hypothesis", "accounts", "schedule", "min_sample", "min_attempts",
                             "window_start", "window_end", "concurrency", "manual_minutes", "notes"})
REQUIRED_FLEET_PLAN_KEYS = ("trial_id", "hypothesis", "accounts", "schedule", "min_sample", "window_start",
                            "window_end")
FLEET_METRICS = (("verified-actions-per-hour", True), ("failure-rate", False), ("uncertain-rate", False))
RUN_RECORD_DIR = "runs"  # <journal dir>/runs/<UTC time>.json, as run.py --fleet-execute writes it
RUN_RECORD_KIND = "fleet-execute"
RUN_RECORD_VERSION = 1
FLEET_JOURNAL_SUFFIX = ".log"  # <journal dir>/<alias>.log, as sdk.fleet.factory names a journal
HASH_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
SHORT_HASH_DIGITS = 16  # a full sha256 is secret-shaped to the redactor; 16 hex digits identify a file
IDENTITY_RULE = "identity-not-enforced"
UNOBSERVED_STATUSES = frozenset({"disabled", "not-run"})


@dataclass(frozen=True)
class ScheduledWindow:
    mode: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class FleetPlan:
    trial_id: str
    hypothesis: str
    accounts: tuple[str, str]
    schedule: tuple[ScheduledWindow, ...]
    min_sample: int
    min_attempts: int
    window_start: datetime
    window_end: datetime
    concurrency: int = 2
    manual_minutes: Mapping[str, float] = field(default_factory=dict)

    def scheduled_mode(self, moment: datetime) -> Optional[str]:
        for window in self.schedule:
            if window.start <= moment < window.end:
                return window.mode
        return None

    def experiment(self, metric: str, higher_is_better: bool) -> Experiment:
        return Experiment(
            experiment_id=self.trial_id, hypothesis=self.hypothesis, baseline=FLEET_BASELINE,
            candidate=FLEET_CANDIDATE, metric=metric, higher_is_better=higher_is_better,
            min_sample=self.min_sample if metric == FLEET_METRICS[0][0] else self.min_attempts,
            window_start=self.window_start, window_end=self.window_end,
        )


def _positive_int(value: Any, label: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise TrialInputError(f"{label} must be an integer of at least {minimum}")
    return value


def fleet_plan_from_dict(payload: Mapping[str, Any]) -> FleetPlan:
    if not isinstance(payload, Mapping):
        raise TrialInputError("the fleet plan must be a JSON object")
    _refuse_secrets(payload, "the fleet plan")
    unknown = sorted(set(payload) - FLEET_PLAN_KEYS)
    if unknown:
        raise TrialInputError(f"unknown fleet plan keys: {', '.join(unknown)}")
    missing = [key for key in REQUIRED_FLEET_PLAN_KEYS if key not in payload]
    if missing:
        raise TrialInputError(f"the fleet plan is missing: {', '.join(missing)}")
    accounts = payload["accounts"]
    if (not isinstance(accounts, list) or len(accounts) != 2
            or not all(isinstance(a, str) and ACCOUNT_ALIAS_PATTERN.match(a) for a in accounts)):
        raise TrialInputError("accounts must list exactly two lowercase registry aliases")
    if accounts[0] == accounts[1]:
        raise TrialInputError("the two accounts must differ")
    window_start = _utc(payload["window_start"], "window_start")
    window_end = _utc(payload["window_end"], "window_end")
    schedule = payload["schedule"]
    if not isinstance(schedule, list) or not schedule:
        raise TrialInputError("schedule must be a non-empty list")
    windows = []
    for number, item in enumerate(schedule, start=1):
        if not isinstance(item, Mapping) or set(item) != {"mode", "start", "end"}:
            raise TrialInputError(f"schedule window {number} needs exactly mode, start and end")
        if item["mode"] not in FLEET_MODES:
            raise TrialInputError(f"schedule window {number} mode must be sequential or concurrent")
        window = ScheduledWindow(item["mode"], _utc(item["start"], f"schedule window {number} start"),
                                 _utc(item["end"], f"schedule window {number} end"))
        if not window_start <= window.start < window.end <= window_end:
            raise TrialInputError(f"schedule window {number} must be a non-empty span inside the trial window")
        windows.append(window)
    windows.sort(key=lambda w: w.start)
    if any(later.start < earlier.end for earlier, later in zip(windows, windows[1:])):
        raise TrialInputError("schedule windows must not overlap")
    if {w.mode for w in windows} != set(FLEET_MODES):
        raise TrialInputError("the schedule must give both sequential and concurrent windows")
    min_sample = _positive_int(payload["min_sample"], "min_sample", 2)
    manual = payload.get("manual_minutes") or {}
    if not isinstance(manual, Mapping):
        raise TrialInputError("manual_minutes must be a JSON object of account alias to minutes")
    for alias, value in manual.items():
        if alias not in accounts:
            raise TrialInputError("manual_minutes names an account that is not in the plan")
        if value is not None and (not _number(value) or value < 0):
            raise TrialInputError("manual_minutes values must be non-negative numbers or null")
    plan = FleetPlan(
        trial_id=_identifier(payload["trial_id"], "trial_id"), hypothesis=payload["hypothesis"],
        accounts=(accounts[0], accounts[1]), schedule=tuple(windows), min_sample=min_sample,
        min_attempts=_positive_int(payload.get("min_attempts", min_sample), "min_attempts", 2),
        window_start=window_start, window_end=window_end,
        concurrency=_positive_int(payload.get("concurrency", 2), "concurrency", 2),
        manual_minutes={alias: float(value) for alias, value in manual.items() if value is not None},
    )
    for metric, higher in FLEET_METRICS:
        plan.experiment(metric, higher)  # the Experiment checks the hypothesis, sample floor and window
    return plan


@dataclass(frozen=True)
class RunRecord:
    """The subset of a ``--fleet-execute`` run record this report reads (see the module docstring)."""

    run_id: str
    journal_dir: Path
    started_at: datetime
    finished_at: datetime
    accounts: tuple[Mapping[str, Any], ...]
    orchestrator: Optional[str] = None
    execute_mode: Optional[str] = None
    authorization_hash: Optional[str] = None
    registry_hash: Optional[str] = None
    policy_versions: Mapping[str, str] = field(default_factory=dict)
    game_version: Any = None
    stop_reason: Optional[str] = None


def _short_hash(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    match = HASH_PATTERN.match(value) if isinstance(value, str) else None
    if match is None:
        raise TrialInputError(f"{label} must be a sha256: hash")
    return f"sha256:{match.group(1)[:SHORT_HASH_DIGITS]}"


def _record_text(value: Any, label: str) -> Optional[str]:
    if value is not None and not isinstance(value, str):
        raise TrialInputError(f"{label} must be text or null")
    return value


def run_record_from_dict(payload: Mapping[str, Any], run_id: str, journal_dir: Path) -> RunRecord:
    label = f"run record {run_id}"
    if not isinstance(payload, Mapping):
        raise TrialInputError(f"{label} must be a JSON object")
    if payload.get("version", RUN_RECORD_VERSION) != RUN_RECORD_VERSION:
        raise TrialInputError(f"{label} has an unsupported version")
    if payload.get("kind", RUN_RECORD_KIND) != RUN_RECORD_KIND:
        raise TrialInputError(f"{label} is not a --fleet-execute run record")
    started = _utc(payload.get("started_at"), f"{label} started_at")
    finished = _utc(payload.get("finished_at"), f"{label} finished_at")
    if finished < started:
        raise TrialInputError(f"{label} finished before it started")
    orchestrator, execute_mode = payload.get("orchestrator"), payload.get("mode")
    if orchestrator is not None and orchestrator not in FLEET_MODES:
        raise TrialInputError(f"{label} orchestrator must be sequential or concurrent")
    if execute_mode in FLEET_MODES:
        orchestrator, execute_mode = orchestrator or execute_mode, None
    elif execute_mode is not None and execute_mode not in EXECUTE_MODES:
        raise TrialInputError(f"{label} mode must be canary, full, sequential or concurrent")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise TrialInputError(f"{label} accounts must be a list")
    entries, seen = [], set()
    for number, entry in enumerate(accounts, start=1):
        if not isinstance(entry, Mapping):
            raise TrialInputError(f"{label} account {number} must be a JSON object")
        alias = entry.get("alias")
        if not isinstance(alias, str) or not ACCOUNT_ALIAS_PATTERN.match(alias):
            raise TrialInputError(f"{label} account {number} alias must be a lowercase alias")
        if alias in seen:
            raise TrialInputError(f"{label} repeats an account")
        seen.add(alias)
        status = entry.get("status")
        if not isinstance(status, str) or not ID_PATTERN.match(status):
            raise TrialInputError(f"{label} account {number} status must be a lowercase word")
        identity = entry.get("game_identity")
        if identity is not None and (not isinstance(identity, str) or not GAME_IDENTITY_PATTERN.match(identity)):
            raise TrialInputError(f"{label} account {number} game_identity must be a gid1- fingerprint or null")
        attempted = entry.get("attempted")
        if attempted is not None and (isinstance(attempted, bool) or not isinstance(attempted, int)):
            raise TrialInputError(f"{label} account {number} attempted must be an integer or null")
        entries.append({"alias": alias, "status": status,
                        "reason": _record_text(entry.get("reason"), f"{label} account {number} reason"),
                        "game_identity": identity, "attempted": attempted})
    policies = payload.get("policy_versions")
    if policies is None and payload.get("policy_version") is not None:
        policies = {entry["alias"]: payload["policy_version"] for entry in entries}
    policies = policies or {}
    if not isinstance(policies, Mapping) or not all(isinstance(v, str) for v in policies.values()):
        raise TrialInputError(f"{label} policy_versions must map aliases to policy ids")
    game_version = payload.get("game_version")
    if isinstance(game_version, Mapping):
        game_version = {key: _record_text(game_version.get(key), f"{label} game_version {key}")
                        for key in ("client", "server")}
    else:
        game_version = _record_text(game_version, f"{label} game_version")
    return RunRecord(
        run_id=run_id, journal_dir=journal_dir, started_at=started, finished_at=finished, accounts=tuple(entries),
        orchestrator=orchestrator, execute_mode=execute_mode,
        authorization_hash=_short_hash(payload.get("authorization_hash"), f"{label} authorization_hash"),
        registry_hash=_short_hash(payload.get("registry_hash"), f"{label} registry_hash"),
        policy_versions=dict(policies), game_version=game_version,
        stop_reason=_record_text(payload.get("stop_reason"), f"{label} stop_reason"),
    )


@dataclass(frozen=True)
class FleetRunInput:
    """One run record with every journal of its directory (owner alias -> events)."""

    record: RunRecord
    journals: Mapping[str, Sequence[GameplayEvent]]


def load_fleet_runs(paths: Sequence[Path]) -> list[FleetRunInput]:
    """Run records and their directories' journals from ``--run`` directories or run record files."""
    records: dict[Path, Path] = {}  # record file -> journal dir
    for path in paths:
        if path.is_dir():
            found = sorted((path / RUN_RECORD_DIR).glob("*.json"))
            if not found:
                raise TrialInputError(f"a --run directory has no run record under {RUN_RECORD_DIR}/")
            pairs = [(file, path) for file in found]
        elif path.is_file() and path.suffix == ".json" and path.parent.name == RUN_RECORD_DIR:
            pairs = [(path, path.parent.parent)]
        else:
            raise TrialInputError(f"a --run path must be a journal directory or a {RUN_RECORD_DIR}/*.json record")
        for file, journal_dir in pairs:
            if file.resolve() in records:
                raise TrialInputError("a run record is given twice")
            records[file.resolve()] = journal_dir.resolve()
    journals: dict[Path, dict[str, list[GameplayEvent]]] = {}
    runs, run_ids = [], set()
    for file, journal_dir in records.items():
        if file.stem in run_ids:
            raise TrialInputError("two run records share a file name; run ids must be unique")
        run_ids.add(file.stem)
        if journal_dir not in journals:
            journals[journal_dir] = {log.name[:-len(FLEET_JOURNAL_SUFFIX)]: read_events(log)
                                     for log in sorted(journal_dir.glob(f"*{FLEET_JOURNAL_SUFFIX}")) if log.is_file()}
        record = run_record_from_dict(_read_json(file, "a run record"), file.stem, journal_dir)
        runs.append(FleetRunInput(record, journals[journal_dir]))
    return sorted(runs, key=lambda run: (run.record.started_at, run.record.run_id))


def fleet_minutes_from_list(payload: Any, plan: FleetPlan) -> dict[str, dict[str, float]]:
    """Operator minutes per run id and account. A ``null`` entry is a run without a log (unobserved)."""
    if not isinstance(payload, list):
        raise TrialInputError("operator minutes must be a JSON list")
    _refuse_secrets(payload, "operator minutes")
    minutes: dict[str, dict[str, float]] = {}
    seen = set()
    for number, item in enumerate(payload, start=1):
        if not isinstance(item, Mapping) or set(item) != {"run_id", "account_alias", "operator_minutes"}:
            raise TrialInputError(f"minutes entry {number} needs exactly run_id, account_alias and operator_minutes")
        if item["account_alias"] not in plan.accounts:
            raise AccountMismatchError(f"minutes entry {number} names an account that is not in the plan")
        if not isinstance(item["run_id"], str) or not item["run_id"]:
            raise TrialInputError(f"minutes entry {number} run_id must be a run record name")
        key = (item["run_id"], item["account_alias"])
        if key in seen:
            raise TrialInputError(f"minutes entry {number} repeats a run and account")
        seen.add(key)
        value = item["operator_minutes"]
        if value is not None and (not _number(value) or value < 0):
            raise TrialInputError(f"minutes entry {number} operator_minutes must be a non-negative number or null")
        if value is not None:
            minutes.setdefault(item["run_id"], {})[item["account_alias"]] = float(value)
    return minutes


def _identity_incidents(plan: FleetPlan, registry: AccountRegistry, record: RunRecord,
                        window: Mapping[str, Sequence[GameplayEvent]]) -> list[dict[str, Any]]:
    """Accounts whose run record does not show the S10-R1 identity check (fingerprint declared and matched)."""
    incidents = []
    for entry in record.accounts:
        alias = entry["alias"]
        expected = registry.get(alias).expected_game_identity if alias in registry else None
        attempted = entry["attempted"] if entry["attempted"] is not None else sum(
            1 for e in window.get(alias, ()) if e.account_alias == alias and e.attempted)
        detail = None
        if alias not in registry:
            detail = f"run record account {alias!r} is not in the registry"
        elif expected is None and entry["status"] not in UNOBSERVED_STATUSES:
            detail = f"{alias!r} declares no expected_game_identity in the registry"
        elif entry["game_identity"] is not None and entry["game_identity"] != expected:
            detail = f"{alias!r} ran as a game identity other than its expected_game_identity"
        elif entry["game_identity"] is None and attempted:
            detail = f"{alias!r} attempted actions but the run record shows no accepted game identity"
        if detail:
            incidents.append({"rule": IDENTITY_RULE, "account_alias": alias, "attempt_ids": [], "detail": detail})
    return incidents


def _run_mode(plan: FleetPlan, record: RunRecord) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """The mode the run counts under, where it came from, and a schedule deviation if any."""
    scheduled = plan.scheduled_mode(record.started_at)
    if record.orchestrator is not None:
        deviation = None
        if scheduled is None:
            deviation = "the run started outside every scheduled window"
        elif scheduled != record.orchestrator:
            deviation = f"the schedule names {scheduled} but the run record shows {record.orchestrator}"
        return record.orchestrator, "run record", deviation
    if scheduled is not None:
        return scheduled, "schedule", None
    return None, None, "the run record names no mode and the run started outside every scheduled window"


def _run_section(plan: FleetPlan, registry: AccountRegistry, run: FleetRunInput, previous_end: Optional[datetime],
                 minutes: Mapping[str, Mapping[str, float]]) -> dict[str, Any]:
    record = run.record
    start, end = record.started_at, record.finished_at
    scope = {alias: [e for e in events if (previous_end is None or _as_utc(e.finished_at) > previous_end)
                     and _as_utc(e.finished_at) <= end] for alias, events in run.journals.items()}
    window = {alias: [e for e in events if _as_utc(e.started_at) >= start] for alias, events in scope.items()}
    audit = audit_fleet(registry, scope)
    incidents = [{**asdict(incident), "attempt_ids": list(incident.attempt_ids)} for incident in audit.incidents]
    incidents += _identity_incidents(plan, registry, record, window)
    fleet_run = FleetRun(
        mode=record.orchestrator or "unknown", dry_run=False, started_at=start.isoformat(),
        finished_at=end.isoformat(), stop_reason=record.stop_reason,
        accounts=tuple(AccountRun(entry["alias"], entry["status"], entry["reason"]) for entry in record.accounts),
    )
    fleet = compute_fleet_report(window, fleet_run=fleet_run, audit=audit, manual_minutes=plan.manual_minutes,
                                 operator_minutes=minutes.get(record.run_id, {}))
    alerts = evaluate_alerts(window, now=end, audit=audit)
    mode, mode_source, deviation = _run_mode(plan, record)
    hours = (end - start).total_seconds() / 3600
    verified = fleet.fleet.verified
    return {
        "run_id": record.run_id,
        "journal_dir": record.journal_dir.name,
        "started_at": start.isoformat(),
        "finished_at": end.isoformat(),
        "hours": round(hours, 4),
        "mode": mode,
        "mode_source": mode_source,
        "scheduled_mode": plan.scheduled_mode(start),
        "schedule_deviation": deviation,
        "execute_mode": record.execute_mode,
        "authorization_hash": record.authorization_hash,
        "registry_hash": record.registry_hash,
        "policy_versions": dict(record.policy_versions),
        "game_version": record.game_version,
        "stop_reason": record.stop_reason,
        "accounts": [dict(entry) for entry in record.accounts],
        "events_audited": audit.events_checked,
        "attempted_outside_run": sum(1 for alias, events in scope.items() for e in events
                                     if e.attempted and e.account_alias == alias and _as_utc(e.started_at) < start),
        "audit": {"clean": not incidents, "accounts_checked": audit.accounts_checked,
                  "events_checked": audit.events_checked, "by_rule": _count_rules(incidents)},
        "incidents": incidents,
        "fleet_report": asdict(fleet),
        "verified_per_hour": round(verified / hours, 4) if hours > 0 else None,
        "alerts": asdict(alerts),
    }


def _count_rules(incidents: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for incident in incidents:
        counts[incident["rule"]] = counts.get(incident["rule"], 0) + 1
    return counts


def _mode_summary(plan: FleetPlan, mode: str, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fleet = [run["fleet_report"]["fleet"] for run in runs]
    totals = {key: sum(row[key] for row in fleet) for key in ("attempted", "verified", "failed", "uncertain",
                                                              "contradicted")}
    per_hour = MeanSample(tuple(run["verified_per_hour"] for run in runs if run["verified_per_hour"] is not None))
    failure = ProportionSample(totals["failed"], totals["attempted"])
    uncertain = ProportionSample(totals["uncertain"], totals["attempted"])
    accounts = {}
    for alias in plan.accounts:
        rows = [row for run in runs for row in run["fleet_report"]["accounts"] if row["account_alias"] == alias]
        sums = {key: sum(row["throughput"][key] for row in rows) for key in ("attempted", "verified", "failed",
                                                                             "uncertain", "contradicted")}
        saved = [row["time_saved_minutes"] for row in rows if row["time_saved_minutes"] is not None]
        operator = [row["operator_minutes"] for row in rows if row["operator_minutes"] is not None]
        accounts[alias] = {
            "runs": len(rows), **sums,
            "success_rate": ProportionSample(sums["verified"], sums["attempted"]).estimate(),
            "failure_rate": ProportionSample(sums["failed"], sums["attempted"]).estimate(),
            "uncertain_rate": ProportionSample(sums["uncertain"], sums["attempted"]).estimate(),
            "incidents": sum(row["incidents"] for row in rows),
            "operator_minutes": round(sum(operator), 2) if operator else None,
            "time_saved_minutes": round(sum(saved), 2) if saved else None,
            "time_saved_runs": len(saved),
        }
    saved_runs = [run["fleet_report"]["time_saved_minutes"] for run in runs
                  if run["fleet_report"]["time_saved_minutes"] is not None]
    return {
        "mode": mode,
        "runs": len(runs),
        "run_hours": round(sum(run["hours"] for run in runs), 4),
        **totals,
        "verified_actions_per_hour": {**_mean_summary(per_hour), "unit": "run",
                                      "source": "fleet verified actions / run record duration, one value per run"},
        "failure_rate": {"n": failure.n, "rate": failure.estimate(), "interval_95": failure.interval()},
        "uncertain_rate": {"n": uncertain.n, "rate": uncertain.estimate(), "interval_95": uncertain.interval()},
        "incidents": sum(len(run["incidents"]) for run in runs),
        "alerts": sum(len(run["alerts"]["alerts"]) for run in runs),
        "time_saved_minutes": round(sum(saved_runs), 2) if saved_runs else None,
        "accounts": accounts,
        "_samples": (per_hour, failure, uncertain),
    }


def _recommendation(decisions: Mapping[str, Decision]) -> Optional[str]:
    throughput = decisions[FLEET_METRICS[0][0]].decision
    if throughput == REJECT:
        return FLEET_BASELINE
    if throughput == ADOPT and all(decisions[m].decision != REJECT for m, _ in FLEET_METRICS[1:]):
        return FLEET_CANDIDATE
    return None


def build_fleet_trial_report(
    plan: FleetPlan,
    registry: AccountRegistry,
    runs: Sequence[FleetRunInput],
    minutes: Optional[Mapping[str, Mapping[str, float]]] = None,
    *,
    now: datetime,
) -> dict[str, Any]:
    now = _utc(now, "now")
    minutes = minutes or {}
    missing = [alias for alias in plan.accounts if alias not in registry]
    if missing:
        raise TrialInputError("a plan account is not in the registry")
    runs = sorted(runs, key=lambda run: (run.record.started_at, run.record.run_id))
    unknown_runs = set(minutes) - {run.record.run_id for run in runs}
    if unknown_runs:
        raise TrialInputError("operator minutes name a run that is not given with --run")

    previous_end: dict[Path, datetime] = {}
    sections, stop = [], None
    for run in sorted(runs, key=lambda run: (run.record.finished_at, run.record.run_id)):
        section = _run_section(plan, registry, run, previous_end.get(run.record.journal_dir), minutes)
        previous_end[run.record.journal_dir] = run.record.finished_at
        sections.append(section)
    sections.sort(key=lambda section: (section["started_at"], section["run_id"]))
    for section in sections:
        excluded = None
        if stop is not None:
            excluded = f"after the trial stopped at run {stop['run_id']}"
        elif not (plan.window_start <= _utc(section["started_at"], "started_at")
                  and _utc(section["finished_at"], "finished_at") <= plan.window_end):
            excluded = "outside the trial window"
        elif section["mode"] is None:
            excluded = "mode unknown"
        section["counted"], section["excluded"] = excluded is None, excluded
        if stop is None and section["incidents"]:  # S10-R3: any incident stops the trial from this run
            stop = {"run_id": section["run_id"], "finished_at": section["finished_at"],
                    "incidents": section["incidents"]}

    modes, decisions = {}, {}
    for mode in FLEET_MODES:
        modes[mode] = _mode_summary(plan, mode, [s for s in sections if s["counted"] and s["mode"] == mode])
    for index, (metric, higher) in enumerate(FLEET_METRICS):
        decisions[metric] = decide(plan.experiment(metric, higher), modes[FLEET_BASELINE]["_samples"][index],
                                   modes[FLEET_CANDIDATE]["_samples"][index], now)
    for summary in modes.values():
        del summary["_samples"]
    after_last = 0
    for journal_dir, end in previous_end.items():
        journals = next(run.journals for run in runs if run.record.journal_dir == journal_dir)
        after_last += sum(1 for events in journals.values() for e in events if _as_utc(e.finished_at) > end)
    saved = [s["fleet_report"]["time_saved_minutes"] for s in sections
             if s["counted"] and s["fleet_report"]["time_saved_minutes"] is not None]
    report = {
        "report": "fleet-trial",
        "trial_id": plan.trial_id,
        "hypothesis": plan.hypothesis,
        "accounts": list(plan.accounts),
        "concurrency": plan.concurrency,
        "window": {"start": plan.window_start.isoformat(), "end": plan.window_end.isoformat()},
        "schedule": [{"mode": w.mode, "start": w.start.isoformat(), "end": w.end.isoformat()} for w in plan.schedule],
        "evaluated_at": now.isoformat(),
        "min_sample": plan.min_sample,
        "min_attempts": plan.min_attempts,
        "manual_minutes": dict(plan.manual_minutes),
        "status": "stopped" if stop else ("in-progress" if now < plan.window_end else "complete"),
        "stop": stop,
        "incidents": [{"run_id": s["run_id"], **incident} for s in sections for incident in s["incidents"]],
        "runs": sections,
        "modes": modes,
        "decisions": {metric: asdict(decision) for metric, decision in decisions.items()},
        "recommendation": None if stop else _recommendation(decisions),
        "time_saved_minutes": round(sum(saved), 2) if saved else None,
        "events_after_last_run": after_last,
        "registry_hashes": sorted({s["registry_hash"] for s in sections if s["registry_hash"]}),
    }
    report["limitations"] = _fleet_limitations(report)
    _refuse_secrets(report, "the fleet trial report")
    return report


def _fleet_limitations(report: Mapping[str, Any]) -> list[str]:
    notes = [
        "Both accounts share the client's process-wide rate limit, so concurrent runs may gain little. The "
        "journals do not record rate-limit waits, so this report cannot say whether throughput was rate-limited.",
        "Modes run in different windows, so changes in the game or the accounts between windows are confounded "
        "with the mode.",
        "Runs of one account are serially correlated, so the 95% intervals and the decide() results are "
        "optimistic.",
        "Verified actions per hour divide by the run record's duration, which includes logins and snapshots.",
    ]
    if report["stop"]:
        notes.insert(0, f"The trial stopped at run {report['stop']['run_id']} on {len(report['stop']['incidents'])} "
                        "isolation incident(s). Later runs are left out; the comparison and decide() results are "
                        "for reference only and must not be acted on.")
    for mode, summary in report["modes"].items():
        if summary["runs"] < report["min_sample"]:
            notes.append(f"{mode} has {summary['runs']} counted run(s), under the planned minimum of "
                         f"{report['min_sample']}.")
        if summary["attempted"] < report["min_attempts"]:
            notes.append(f"{mode} has {summary['attempted']} attempt(s), under the planned minimum of "
                         f"{report['min_attempts']} for the rate comparisons.")
    deviations = [s["run_id"] for s in report["runs"] if s["schedule_deviation"]]
    if deviations:
        notes.append(f"{len(deviations)} run(s) did not follow the schedule ({', '.join(deviations)}); each counts "
                     "under the mode its run record shows.")
    excluded = [s for s in report["runs"] if not s["counted"]]
    if excluded:
        notes.append(f"{len(excluded)} run(s) are left out of the comparison: "
                     + "; ".join(f"{s['run_id']} ({s['excluded']})" for s in excluded) + ".")
    outside = sum(s["attempted_outside_run"] for s in report["runs"])
    if outside:
        notes.append(f"{outside} attempted action(s) were journaled between runs, outside every run record; they "
                     "were audited but are not in any run's metrics.")
    if report["events_after_last_run"]:
        notes.append(f"{report['events_after_last_run']} journal event(s) finished after the last run record of "
                     "their directory and were not audited.")
    if len(report["registry_hashes"]) > 1:
        notes.append("The registry changed between runs (several registry hashes).")
    elif not report["registry_hashes"]:
        notes.append("No run record carries a registry hash.")
    servers = {s["game_version"].get("server") if isinstance(s["game_version"], Mapping) else s["game_version"]
               for s in report["runs"]}
    if servers <= {None}:
        notes.append("No run record observed the server's game version.")
    elif len(servers - {None}) > 1:
        notes.append("The game version changed during the trial; analyse each part separately.")
    if report["time_saved_minutes"] is None:
        notes.append("Time saved is not reported: it needs an account's manual routine minutes in the plan and "
                     "its operator minutes for a run.")
    others = sorted({entry["alias"] for s in report["runs"] for entry in s["accounts"]} - set(report["accounts"]))
    if others:
        notes.append(f"Run records include account(s) outside the plan: {', '.join(others)}.")
    notes.append("decide() has no futility rule and no non-inferiority mode: it cannot show the modes are "
                 "equivalent.")
    return notes


def _pct_cell(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _interval_cell(interval: Optional[Sequence[float]], percent: bool = False) -> str:
    if interval is None:
        return "n/a"
    render = _pct_cell if percent else _cell
    return f"{render(interval[0])} to {render(interval[1])}"


def fleet_to_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        f"# Fleet trial report: {report['trial_id']}",
        "",
        f"- Accounts: {', '.join(report['accounts'])}",
        f"- Window: {report['window']['start']} to {report['window']['end']}",
        f"- Evaluated at: {report['evaluated_at']}",
        f"- Modes: {FLEET_BASELINE} (baseline) vs {FLEET_CANDIDATE} (candidate, {report['concurrency']} workers); "
        f"minimum {report['min_sample']} runs and {report['min_attempts']} attempts per mode",
        f"- Status: **{report['status']}**",
        f"- Recommendation: {report['recommendation'] or 'none'}",
        f"- Time saved (min): {_cell(report['time_saved_minutes'])}",
        "",
    ]
    if report["stop"]:
        lines += ["## Trial stopped", "", f"Run {report['stop']['run_id']} (finished {report['stop']['finished_at']}) "
                  "had isolation incidents, so the trial stopped there. The comparison below is for reference only.",
                  ""]
    lines += ["## Isolation incidents", ""]
    if report["incidents"]:
        lines += ["| Run | Rule | Account | Attempts | Detail |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {i['run_id']} | {i['rule']} | {i['account_alias']} | {_cell(', '.join(i['attempt_ids']) or '')} "
                  f"| {_cell(i['detail'])} |" for i in report["incidents"]]
    else:
        lines.append("_Every run's audit was clean._")
    lines += ["", "## Runs", "",
              "| Run | Mode (source) | Scheduled | Start | Hours | Attempted | Verified | Failed | Uncertain | "
              "Verified/h | Incidents | Alerts | Counted |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in report["runs"]:
        t = s["fleet_report"]["fleet"]
        mode = f"{s['mode']} ({s['mode_source']})" if s["mode"] else "n/a"
        counted = "yes" if s["counted"] else f"no: {s['excluded']}"
        lines.append(f"| {s['run_id']} | {mode} | {_cell(s['scheduled_mode'])} | {s['started_at']} | "
                     f"{_cell(s['hours'])} | {t['attempted']} | {t['verified']} | {t['failed']} | {t['uncertain']} | "
                     f"{_cell(s['verified_per_hour'])} | {len(s['incidents'])} | {len(s['alerts']['alerts'])} | "
                     f"{_cell(counted)} |")
    lines += ["", "## Accounts per run", "",
              "| Run | Account | Status | Attempted | Verified | Success | Uncertain | Failed | Actions/h | "
              "Incidents | Operator min | Time saved (min) |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in report["runs"]:
        for row in s["fleet_report"]["accounts"]:
            t = row["throughput"]
            lines.append(f"| {s['run_id']} | {row['account_alias']} | {_cell(row['status'])} | {t['attempted']} | "
                         f"{t['verified']} | {_pct_cell(t['success_rate'])} | {_pct_cell(t['uncertain_rate'])} | "
                         f"{_pct_cell(t['failure_rate'])} | {_cell(t['actions_per_hour'])} | {row['incidents']} | "
                         f"{_cell(row['operator_minutes'])} | {_cell(row['time_saved_minutes'])} |")
    lines += ["", "## Mode comparison", "",
              "| Mode | Runs (n) | Attempted (n) | Verified/h (mean) | 95% interval | Failure rate | 95% interval | "
              "Uncertain rate | 95% interval | Incidents | Alerts | Time saved (min) |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for mode, m in report["modes"].items():
        lines.append(f"| {mode} | {m['runs']} | {m['attempted']} | {_cell(m['verified_actions_per_hour']['mean'])} | "
                     f"{_interval_cell(m['verified_actions_per_hour']['interval_95'])} | "
                     f"{_pct_cell(m['failure_rate']['rate'])} | {_interval_cell(m['failure_rate']['interval_95'], True)} | "
                     f"{_pct_cell(m['uncertain_rate']['rate'])} | "
                     f"{_interval_cell(m['uncertain_rate']['interval_95'], True)} | {m['incidents']} | {m['alerts']} | "
                     f"{_cell(m['time_saved_minutes'])} |")
    lines += ["", "| Metric | decide() | n (sequential / concurrent) | Reason |", "| --- | --- | --- | --- |"]
    for metric, d in report["decisions"].items():
        lines.append(f"| {metric} | **{d['decision']}** | {d['baseline_n']} / {d['candidate_n']} | {_cell(d['reason'])} |")
    lines += ["", "## Per account and mode", "",
              "| Mode | Account | Runs | Attempted | Verified | Success | Failure | Uncertain | Incidents | "
              "Operator min | Time saved (min) |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for mode, m in report["modes"].items():
        for alias, a in m["accounts"].items():
            lines.append(f"| {mode} | {alias} | {a['runs']} | {a['attempted']} | {a['verified']} | "
                         f"{_pct_cell(a['success_rate'])} | {_pct_cell(a['failure_rate'])} | "
                         f"{_pct_cell(a['uncertain_rate'])} | {a['incidents']} | {_cell(a['operator_minutes'])} | "
                         f"{_cell(a['time_saved_minutes'])} |")
    alerts = [(s["run_id"], alert) for s in report["runs"] for alert in s["alerts"]["alerts"]]
    lines += ["", "## Alerts", ""]
    if alerts:
        lines += ["| Run | Severity | Rule | Account | Message |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {run_id} | {a['severity']} | {a['rule_id']} | {a['account_alias']} | {_cell(a['message'])} |"
                  for run_id, a in alerts]
    else:
        lines.append("_No alerts in any run._")
    lines += ["", "## Run records", "",
              "| Run | Journal dir | Execute mode | Authorization | Registry | Policies | Game version | Stop reason |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in report["runs"]:
        version = s["game_version"]
        if isinstance(version, Mapping):
            version = f"client {_cell(version.get('client'))}, server {_cell(version.get('server'))}"
        policies = ", ".join(f"{alias} {policy}" for alias, policy in sorted(s["policy_versions"].items()))
        lines.append(f"| {s['run_id']} | {_cell(s['journal_dir'])} | {_cell(s['execute_mode'])} | "
                     f"{_cell(s['authorization_hash'])} | {_cell(s['registry_hash'])} | {_cell(policies or None)} | "
                     f"{_cell(version)} | {_cell(s['stop_reason'])} |")
    lines += ["", "## Limitations", ""] + [f"- {note}" for note in report["limitations"]]
    return "\n".join(lines) + "\n"


# -- study report aggregate (SPEC-12 R1, R4, R6) -------------------------------------------------

STUDY_REPORT_VERSION = 1
MARKET_TRIAL_VERSION = 1  # provisional: see the module docstring
# (study id, input kind, spec, the question the study answers). The order is the report's order.
STUDIES = (
    ("A", "progression", "SPEC-07",
     "Does rule-based automation (baseline-v1) grow an account faster per account-day than the owner's manual "
     "routine?"),
    ("C", "pvp", "SPEC-08",
     "Which of two PvP strategies wins a larger share of known-result attacks?"),
    ("D", "fleet", "SPEC-10",
     "Does concurrent fleet operation verify more actions per run-hour than sequential operation?"),
    ("R4-market", "market", "SPEC-09",
     "Do market trades reconcile as completed, and what do completed trades return net of fees?"),
)
STUDY_KINDS = tuple(kind for _, kind, _, _ in STUDIES)
REPORT_KINDS = {"pvp-strategy-comparison": "pvp", "fleet-trial": "fleet", "market-trial": "market"}
PROGRESSION_REPORT_KEYS = frozenset({"trial_id", "arms", "decision", "early_stop", "min_sample", "window"})
MARKET_METRIC = "market-return-per-completed-trade"
MARKET_TRIAL_KEYS = frozenset({"report", "format_version", "trial_id", "hypothesis", "account_alias", "arms",
                               "currency", "min_sample", "window_start", "window_end", "evaluated_at", "trades",
                               "operator_minutes", "notes"})
REQUIRED_MARKET_TRIAL_KEYS = ("report", "format_version", "trial_id", "hypothesis", "account_alias", "arms",
                              "currency", "min_sample", "window_start", "window_end", "evaluated_at", "trades")
MARKET_TRADE_KEYS = frozenset({"trade_id", "arm", "side", "state", "at", "item_design_id", "quantity", "price",
                               "proceeds", "cost", "fees", "game_version"})
REQUIRED_MARKET_TRADE_KEYS = ("trade_id", "side", "state", "at", "price")
MARKET_STATES = {TRADE_SELL: (TRADE_REQUESTED, TRADE_LISTED, TRADE_COMPLETED, TRADE_EXPIRED, TRADE_UNKNOWN),
                 TRADE_BUY: (TRADE_REQUESTED, TRADE_COMPLETED, TRADE_UNKNOWN)}
CONCLUSIVE, INCONCLUSIVE, ABORTED = "conclusive", "inconclusive", "aborted"


def study_kind(payload: Any) -> str:
    """Which per-trial output a JSON payload is: ``progression``, ``pvp``, ``fleet`` or ``market``."""
    if isinstance(payload, Mapping):
        if payload.get("report") in REPORT_KINDS:
            return REPORT_KINDS[payload["report"]]
        if "report" not in payload and PROGRESSION_REPORT_KEYS <= set(payload):
            return "progression"
    raise TrialInputError("a --trial file is not a progression, PvP, fleet or market trial output")


def _optional_amount(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TrialInputError(f"{label} must be a non-negative integer or null")
    return value


def market_trial_from_dict(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Check a provisional market-trial result (see the module docstring) and return it normalized."""
    if not isinstance(payload, Mapping):
        raise TrialInputError("the market trial must be a JSON object")
    _refuse_secrets(payload, "the market trial")
    unknown = sorted(set(payload) - MARKET_TRIAL_KEYS)
    if unknown:
        raise TrialInputError(f"unknown market trial keys: {', '.join(unknown)}")
    missing = [key for key in REQUIRED_MARKET_TRIAL_KEYS if key not in payload]
    if missing:
        raise TrialInputError(f"the market trial is missing: {', '.join(missing)}")
    if payload["report"] != "market-trial" or payload["format_version"] != MARKET_TRIAL_VERSION:
        raise TrialInputError(f"the market trial must be report market-trial, format_version {MARKET_TRIAL_VERSION}")
    alias = payload["account_alias"]
    if not isinstance(alias, str) or not ACCOUNT_ALIAS_PATTERN.match(alias):
        raise TrialInputError("account_alias must be a lowercase alias, not an e-mail or display name")
    if not isinstance(payload["hypothesis"], str) or not payload["hypothesis"].strip():
        raise TrialInputError("hypothesis is required")
    arms = payload["arms"]
    if not isinstance(arms, list) or len(arms) not in (1, 2):
        raise TrialInputError("arms must list one or two arms, the baseline first")
    arms = [_identifier(arm, "each arm") for arm in arms]
    if len(set(arms)) != len(arms):
        raise TrialInputError("the two arms must differ")
    min_sample = _positive_int(payload["min_sample"], "min_sample", 1)
    start, end = _utc(payload["window_start"], "window_start"), _utc(payload["window_end"], "window_end")
    if end <= start:
        raise TrialInputError("window_end must be after window_start")
    evaluated = _utc(payload["evaluated_at"], "evaluated_at")
    minutes = payload.get("operator_minutes")
    if minutes is not None and (not _number(minutes) or minutes < 0):
        raise TrialInputError("operator_minutes must be a non-negative number or null")
    if not isinstance(payload["trades"], list):
        raise TrialInputError("trades must be a list")
    trades, seen = [], set()
    for number, item in enumerate(payload["trades"], start=1):
        label = f"trade {number}"
        if not isinstance(item, Mapping):
            raise TrialInputError(f"{label} must be a JSON object")
        unknown = sorted(set(item) - MARKET_TRADE_KEYS)
        if unknown:
            raise TrialInputError(f"{label} has unknown keys: {', '.join(unknown)}")
        missing = [key for key in REQUIRED_MARKET_TRADE_KEYS if key not in item]
        if missing:
            raise TrialInputError(f"{label} is missing: {', '.join(missing)}")
        trade_id = _identifier(item["trade_id"], f"{label} trade_id")
        if trade_id in seen:
            raise TrialInputError(f"{label} repeats a trade_id")
        seen.add(trade_id)
        arm = item.get("arm", arms[0] if len(arms) == 1 else None)
        if arm not in arms:
            raise TrialInputError(f"{label} must name one of the trial's arms")
        side, state = item["side"], item["state"]
        if side not in MARKET_STATES or state not in MARKET_STATES[side]:
            raise TrialInputError(f"{label} has an unknown side or a state that side cannot reach")
        at = _utc(item["at"], f"{label} at")
        if not start <= at < end:
            raise TrialInputError(f"{label} is outside the trial window")
        quantity = item.get("quantity")
        if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1):
            raise TrialInputError(f"{label} quantity must be a positive integer or null")
        version = item.get("game_version")
        if version is not None and (not isinstance(version, str) or not version.strip()):
            raise TrialInputError(f"{label} game_version must be text or null")
        trade = {
            "trade_id": trade_id, "arm": arm, "side": side, "state": state, "at": at.isoformat(),
            "item_design_id": _optional_text(item.get("item_design_id")), "quantity": quantity,
            "price": _optional_amount(item["price"], f"{label} price"),
            "proceeds": _optional_amount(item.get("proceeds"), f"{label} proceeds"),
            "cost": _optional_amount(item.get("cost"), f"{label} cost"),
            "fees": _optional_amount(item.get("fees"), f"{label} fees"),
            "game_version": version,
        }
        if state == TRADE_COMPLETED and trade["proceeds" if side == TRADE_SELL else "cost"] is None:
            raise TrialInputError(f"{label} is completed but has no {'proceeds' if side == TRADE_SELL else 'cost'}")
        trades.append(trade)
    return {
        "report": "market-trial", "format_version": MARKET_TRIAL_VERSION,
        "trial_id": _identifier(payload["trial_id"], "trial_id"), "hypothesis": payload["hypothesis"],
        "account_alias": alias, "arms": arms, "currency": _identifier(payload["currency"], "currency"),
        "min_sample": min_sample, "window_start": start.isoformat(), "window_end": end.isoformat(),
        "evaluated_at": evaluated.isoformat(), "operator_minutes": minutes,
        "notes": _optional_text(payload.get("notes")),
        "trades": sorted(trades, key=lambda trade: (trade["at"], trade["trade_id"])),
    }


def _primary_row(arm: str, n: int, estimate: Optional[float], interval: Optional[Sequence[float]]) -> dict[str, Any]:
    return {"arm": arm, "n": n, "estimate": estimate, "interval_95": None if interval is None else list(interval)}


def _window_span(windows: Sequence[Mapping[str, Any]]) -> tuple[datetime, datetime]:
    return (min(_utc(w["start"], "window start") for w in windows),
            max(_utc(w["end"], "window end") for w in windows))


def _progression_study(report: Mapping[str, Any]) -> dict[str, Any]:
    arms = report["arms"]
    return {
        "trial_id": report["trial_id"], "hypothesis": report["hypothesis"], "accounts": [report["account_alias"]],
        "evaluated_at": report["evaluated_at"], "trial_status": report["status"], "recommendation": None,
        "arms": [{"arm": arm["arm"], "role": arm["role"]} for arm in arms],
        "windows": [{"label": "trial", "start": report["window"]["start"], "end": report["window"]["end"]}],
        "game_versions": list(report["game_versions"]),
        "min_sample": report["min_sample"], "sample_unit": "account-day",
        "sample_sizes": {arm["arm"]: arm["sample_size"] for arm in arms},
        "primary_result": {
            "metric": report["metric"], "higher_is_better": report["higher_is_better"], "unit": "account-day",
            "source": GROWTH_SOURCE,
            "arms": [_primary_row(arm["arm"], arm["growth_velocity"]["n"], arm["growth_velocity"]["mean"],
                                  arm["growth_velocity"]["interval_95"]) for arm in arms],
        },
        "decide": dict(report["decision"]),
        "decide_note": None,
        "secondary_decisions": {},
        "costs": {
            "operator_minutes_per_account_day": {arm["arm"]: arm["operator_minutes"]["mean"] for arm in arms},
            "operator_minutes_logged_days": {arm["arm"]: arm["operator_minutes"]["n"] for arm in arms},
            "operator_minutes_total": {arm["arm"]: arm["operator_minutes"]["total"] for arm in arms},
            "operator_minutes_saved_per_account_day": report["operator_minutes_saved_per_account_day"],
            "snapshot_spent_per_hour": {arm["arm"]: {name: arm["resources_per_hour"][name]["spent_per_hour"]
                                                     for name in CURRENCIES} for arm in arms},
            "journal_starbux_spent": {arm["arm"]: arm["journal_resources"]["starbux"]["spent"] for arm in arms},
        },
        "failures": {arm["arm"]: {key: arm["failures"][key] for key in ("attempted", "failed", "uncertain",
                                                                         "contradicted", "failure_rate")}
                     for arm in arms},
        "stopped": report["early_stop"] is not None,
        "extra_reasons": [],
        "limitations": list(report["limitations"]),
    }


def _pvp_study(report: Mapping[str, Any]) -> dict[str, Any]:
    keys = [strategy["key"] for strategy in report["strategies"]]
    rows = {row["strategy"]: row for row in report["attack"]["per_strategy"]}
    start, end = _window_span(report["windows"])
    min_sample = report["min_sample"]
    experiment = Experiment(
        experiment_id=report["trial_id"], hypothesis=report["hypothesis"], baseline=keys[0], candidate=keys[1],
        metric=PVP_METRIC, higher_is_better=True, min_sample=max(2, min_sample), window_start=start, window_end=end,
    )
    decision = decide(experiment, ProportionSample(rows[keys[0]]["wins"], rows[keys[0]]["known"]),
                      ProportionSample(rows[keys[1]]["wins"], rows[keys[1]]["known"]),
                      _utc(report["evaluated_at"], "evaluated_at"))
    note = ("decide() is regenerated here from each strategy's pooled wins and known-result attacks; the trial's own "
            "verdict comes from compare_strategies, stratified by opponent cohort when one was observed")
    if min_sample < 2:
        note += "; decide() needs at least 2 per arm, so its minimum is 2"
    extra = [] if report["verdict"] == "recommend" else [
        f"the trial's stratified comparison returned {report['verdict']}"]
    return {
        "trial_id": report["trial_id"], "hypothesis": report["hypothesis"], "accounts": [report["account_alias"]],
        "evaluated_at": report["evaluated_at"], "trial_status": report["verdict"],
        "recommendation": report["recommendation"],
        "arms": [{"arm": key, "role": "baseline" if index == 0 else "candidate"} for index, key in enumerate(keys)],
        "windows": [{"label": w["strategy"], "start": w["start"], "end": w["end"]} for w in report["windows"]],
        "game_versions": [] if report["game_version"] is None else [report["game_version"]],
        "min_sample": min_sample, "sample_unit": "known-result attack",
        "sample_sizes": {key: rows[key]["known"] for key in keys},
        "primary_result": {
            "metric": PVP_METRIC, "higher_is_better": True, "unit": "known-result attack",
            "source": report["attack"]["source"],
            "arms": [_primary_row(key, rows[key]["known"], rows[key]["win_rate"], rows[key]["interval_95"])
                     for key in keys],
        },
        "decide": asdict(decision),
        "decide_note": note,
        "secondary_decisions": {},
        "costs": {
            "resources_spent": {key: rows[key]["resource_cost"]["spent"] for key in keys},
            "resource_samples": {key: rows[key]["resource_cost"]["samples"] for key in keys},
        },
        "failures": {key: {"attacks": rows[key]["battles"], "unknown_outcomes": rows[key]["unknown"]}
                     for key in keys},
        "stopped": False,
        "extra_reasons": extra,
        "limitations": list(report["limitations"]),
    }


def _fleet_game_versions(runs: Sequence[Mapping[str, Any]]) -> list[str]:
    versions = set()
    for run in runs:
        version = run.get("game_version")
        if isinstance(version, Mapping):
            versions |= {f"{side} {version[side]}" for side in ("client", "server") if version.get(side)}
        elif version:
            versions.add(str(version))
    return sorted(versions)


def _fleet_study(report: Mapping[str, Any]) -> dict[str, Any]:
    modes = report["modes"]
    names = [FLEET_BASELINE, FLEET_CANDIDATE]
    primary = FLEET_METRICS[0][0]
    operator = {}
    for name in names:
        logged = [a["operator_minutes"] for a in modes[name]["accounts"].values() if a["operator_minutes"] is not None]
        operator[name] = round(sum(logged), 2) if logged else None
    extra = []
    if report["recommendation"] is None and not report["stop"]:
        extra.append("the trial makes no recommendation")
    return {
        "trial_id": report["trial_id"], "hypothesis": report["hypothesis"], "accounts": list(report["accounts"]),
        "evaluated_at": report["evaluated_at"], "trial_status": report["status"],
        "recommendation": report["recommendation"],
        "arms": [{"arm": name, "role": "baseline" if name == FLEET_BASELINE else "candidate"} for name in names],
        "windows": [{"label": w["mode"], "start": w["start"], "end": w["end"]} for w in report["schedule"]],
        "game_versions": _fleet_game_versions(report["runs"]),
        "min_sample": report["min_sample"], "sample_unit": "run",
        "sample_sizes": {name: modes[name]["runs"] for name in names},
        "primary_result": {
            "metric": primary, "higher_is_better": True, "unit": "run",
            "source": modes[FLEET_BASELINE]["verified_actions_per_hour"]["source"],
            "arms": [_primary_row(name, modes[name]["verified_actions_per_hour"]["n"],
                                  modes[name]["verified_actions_per_hour"]["mean"],
                                  modes[name]["verified_actions_per_hour"]["interval_95"]) for name in names],
        },
        "decide": dict(report["decisions"][primary]),
        "decide_note": None,
        "secondary_decisions": {metric: {"decision": d["decision"], "reason": d["reason"]}
                                for metric, d in report["decisions"].items() if metric != primary},
        "costs": {
            "run_hours": {name: modes[name]["run_hours"] for name in names},
            "operator_minutes": operator,
            "time_saved_minutes": {name: modes[name]["time_saved_minutes"] for name in names},
        },
        "failures": {name: {"attempted": modes[name]["attempted"], "failed": modes[name]["failed"],
                            "uncertain": modes[name]["uncertain"], "failure_rate": modes[name]["failure_rate"]["rate"],
                            "uncertain_rate": modes[name]["uncertain_rate"]["rate"],
                            "incidents": modes[name]["incidents"], "alerts": modes[name]["alerts"]} for name in names},
        "stopped": report["stop"] is not None,
        "extra_reasons": extra,
        "limitations": list(report["limitations"]),
    }


def _market_study(trial: Mapping[str, Any]) -> dict[str, Any]:
    arms, trades = trial["arms"], trial["trades"]
    samples, returns, fees, buy_cost, states = {}, {}, {}, {}, {}
    for arm in arms:
        own = [t for t in trades if t["arm"] == arm]
        done = [t for t in own if t["state"] == TRADE_COMPLETED]
        signed = [t["proceeds"] if t["side"] == TRADE_SELL else -t["cost"] for t in done]
        samples[arm] = MeanSample(tuple(float(value) for value in signed))
        returns[arm] = sum(signed) if done else None
        charged = [t["fees"] for t in done if t["fees"] is not None]
        fees[arm] = {"total": sum(charged) if charged else None, "samples": len(charged)}
        buys = [t["cost"] for t in done if t["side"] == TRADE_BUY]
        buy_cost[arm] = sum(buys) if buys else None
        states[arm] = {"trades": len(own), **{state: sum(1 for t in own if t["state"] == state)
                                              for state in MARKET_STATES[TRADE_SELL]}}
        states[arm]["unreconciled"] = sum(states[arm][s] for s in (TRADE_REQUESTED, TRADE_LISTED, TRADE_UNKNOWN))
    decision, note = None, None
    if len(arms) == 2:
        experiment = Experiment(
            experiment_id=trial["trial_id"], hypothesis=trial["hypothesis"], baseline=arms[0], candidate=arms[1],
            metric=MARKET_METRIC, higher_is_better=True, min_sample=max(2, trial["min_sample"]),
            window_start=_utc(trial["window_start"], "window_start"),
            window_end=_utc(trial["window_end"], "window_end"))
        decision = asdict(decide(experiment, samples[arms[0]], samples[arms[1]],
                                 _utc(trial["evaluated_at"], "evaluated_at")))
    else:
        note = "a single-arm trial has no comparison, so decide() does not apply"
    versions = sorted({t["game_version"] for t in trades if t["game_version"] is not None})
    limitations = [
        f"The market-trial format is provisional (version {MARKET_TRIAL_VERSION}) until SPEC-09 adds "
        "trial_report.py --market; its figures come from the trade states the trial recorded.",
        "Market return counts completed trades only: sell proceeds minus buy cost, in the trial currency. Fees are "
        "already inside proceeds and cost and are reported separately.",
    ]
    if not versions:
        limitations.append("No trade recorded a game version.")
    elif len(versions) > 1:
        limitations.append("The game version changed during the trial; analyse each part separately.")
    unreconciled = sum(row["unreconciled"] for row in states.values())
    if unreconciled:
        limitations.append(f"{unreconciled} trade(s) are still requested, listed or unknown; they are counted but "
                           "never treated as completed.")
    if trial["notes"]:
        limitations.append(f"Trial notes: {trial['notes']}")
    now = _utc(trial["evaluated_at"], "evaluated_at")
    return {
        "trial_id": trial["trial_id"], "hypothesis": trial["hypothesis"], "accounts": [trial["account_alias"]],
        "evaluated_at": trial["evaluated_at"],
        "trial_status": "in-progress" if now < _utc(trial["window_end"], "window_end") else "complete",
        "recommendation": None,
        "arms": [{"arm": arm, "role": "baseline" if index == 0 else "candidate"} for index, arm in enumerate(arms)],
        "windows": [{"label": "trial", "start": trial["window_start"], "end": trial["window_end"]}],
        "game_versions": versions,
        "min_sample": trial["min_sample"], "sample_unit": "completed trade",
        "sample_sizes": {arm: samples[arm].n for arm in arms},
        "primary_result": {
            "metric": MARKET_METRIC, "higher_is_better": True, "unit": "completed trade",
            "source": f"completed trades only: sell proceeds or minus buy cost, in {trial['currency']}",
            "currency": trial["currency"], "market_return": returns,
            "arms": [_primary_row(arm, samples[arm].n, samples[arm].estimate(), samples[arm].interval())
                     for arm in arms],
        },
        "decide": decision,
        "decide_note": note,
        "secondary_decisions": {},
        "costs": {"fees": fees, "buy_cost": buy_cost, "operator_minutes": trial["operator_minutes"]},
        "failures": states,
        "stopped": False,
        "extra_reasons": [],
        "limitations": limitations,
    }


STUDY_BUILDERS = {"progression": _progression_study, "pvp": _pvp_study, "fleet": _fleet_study,
                  "market": _market_study}
STUDY_FIELDS = ("trial_id", "hypothesis", "accounts", "evaluated_at", "trial_status", "recommendation", "arms",
                "windows", "game_versions", "min_sample", "sample_unit", "sample_sizes", "primary_result", "decide",
                "decide_note", "secondary_decisions", "costs", "failures", "limitations")


def _conclude(study: Mapping[str, Any], stopped: bool, extra: Sequence[str]) -> tuple[str, Optional[bool], list[str]]:
    """The study's conclusion, whether every arm reached its minimum sample, and why it is not conclusive (R4)."""
    if study["status"] == "missing":
        return INCONCLUSIVE, None, ["no trial output was given for this study"]
    short = [f"{arm} has {n} {study['sample_unit']}(s), under the minimum of {study['min_sample']}"
             for arm, n in study["sample_sizes"].items() if n < study["min_sample"]]
    reasons = list(short)
    if stopped:
        reasons.append("the trial stopped early; its results are for reference only")
    decision = study["decide"]
    if decision is not None and decision["decision"] not in (ADOPT, REJECT):
        reasons.append(f"decide() returned {decision['decision']}: {decision['reason']}")
    reasons += extra
    if short:
        return INCONCLUSIVE, False, reasons
    if stopped:
        return ABORTED, True, reasons
    return (INCONCLUSIVE if reasons else CONCLUSIVE), True, reasons


def _missing_study() -> dict[str, Any]:
    empty: dict[str, Any] = dict.fromkeys(STUDY_FIELDS)
    empty.update(accounts=[], arms=[], windows=[], game_versions=[], sample_sizes={}, secondary_decisions={},
                 costs={}, failures={}, limitations=[])
    return empty


def build_study_report(trials: Mapping[str, Mapping[str, Any]],
                       sources: Optional[Mapping[str, Mapping[str, Any]]] = None) -> dict[str, Any]:
    """Aggregate per-trial outputs, keyed by kind (``progression``, ``pvp``, ``fleet``, ``market``), into the study
    report. Every study appears; one without an output, or short of its minimum sample, is inconclusive (R4). The
    result depends only on the inputs, so the same outputs give the same report byte for byte (R6)."""
    unknown = set(trials) - set(STUDY_KINDS)
    if unknown:
        raise TrialInputError("trials are keyed by progression, pvp, fleet or market")
    sources = sources or {}
    studies = []
    for study_id, kind, spec, question in STUDIES:
        payload = trials.get(kind)
        if payload is None:
            fields, stopped, extra = _missing_study(), False, []
        else:
            payload = json.loads(json.dumps(payload, sort_keys=True))  # a JSON file and a dict give one result
            _refuse_secrets(payload, f"the {kind} trial output")
            if study_kind(payload) != kind:
                raise TrialInputError(f"the {kind} input is not a {kind} trial output")
            if kind == "market":
                payload = market_trial_from_dict(payload)
            try:
                built = STUDY_BUILDERS[kind](payload)
            except (KeyError, TypeError, IndexError, AttributeError):
                raise TrialInputError(f"the {kind} trial output is missing a field or has a malformed one") from None
            stopped, extra = built.pop("stopped"), built.pop("extra_reasons")
            fields = built
        study = {"study": study_id, "kind": kind, "spec": spec, "question": question,
                 "status": "missing" if payload is None else "reported", "source": sources.get(kind), **fields}
        study["conclusion"], study["reached_min_sample"], study["inconclusive_reasons"] = _conclude(
            study, stopped, extra)
        studies.append(study)
    report = {
        "report": "study",
        "format_version": STUDY_REPORT_VERSION,
        "generated_by": "scripts/trial_report.py --study",
        "studies": studies,
        "summary": {
            "reported": [s["study"] for s in studies if s["status"] == "reported"],
            "missing": [s["study"] for s in studies if s["status"] == "missing"],
            CONCLUSIVE: [s["study"] for s in studies if s["conclusion"] == CONCLUSIVE],
            INCONCLUSIVE: [s["study"] for s in studies if s["conclusion"] == INCONCLUSIVE],
            ABORTED: [s["study"] for s in studies if s["conclusion"] == ABORTED],
        },
    }
    _refuse_secrets(report, "the study report")
    return json.loads(to_json(report))  # the in-memory report is exactly what its JSON file holds, key order too


def load_study_inputs(paths: Sequence[Path]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Read per-trial output files, each identified by its content; at most one per study."""
    trials: dict[str, Any] = {}
    sources: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.is_file():
            raise TrialInputError("a --trial path is not a file")
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TrialInputError(f"cannot read a --trial file ({type(error).__name__})") from None
        kind = study_kind(payload)
        if kind in trials:
            raise TrialInputError(f"two --trial files are {kind} trial outputs; give one per study")
        trials[kind] = payload
        sources[kind] = {"file": path.name,
                         "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()[:SHORT_HASH_DIGITS]}
    return trials, sources


def _value_cell(value: Any) -> str:
    if isinstance(value, Mapping):
        return ", ".join(f"{key} {_value_cell(item)}" for key, item in sorted(value.items())) or "none"
    return _cell(value)


def study_to_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Automation effectiveness study: figures",
        "",
        "Regenerated by `scripts/trial_report.py --study` from the per-trial outputs listed under Sources. The same "
        "outputs always give this file byte for byte. A study without an output, or short of its minimum sample, is "
        "reported as inconclusive, never left out.",
        "",
        "## Summary",
        "",
        "| Study | Spec | Trial | Conclusion | decide() | Sample per arm (minimum) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for s in report["studies"]:
        decision = "n/a" if s["decide"] is None else s["decide"]["decision"]
        sizes = ", ".join(f"{a['arm']} {s['sample_sizes'][a['arm']]}" for a in s["arms"]) or "none"
        minimum = "" if s["min_sample"] is None else f" ({s['min_sample']})"
        lines.append(f"| {s['study']} | {s['spec']} | {_cell(s['trial_id'])} | **{s['conclusion']}** | {decision} | "
                     f"{_cell(sizes)}{minimum} |")
    for s in report["studies"]:
        lines += ["", f"## Study {s['study']} ({s['kind']}, {s['spec']})", "", f"- Question: {s['question']}"]
        if s["status"] == "missing":
            lines += [f"- Conclusion: **{s['conclusion']}**: {'; '.join(s['inconclusive_reasons'])}."]
            continue
        reasons = f": {_cell('; '.join(s['inconclusive_reasons']))}" if s["inconclusive_reasons"] else ""
        lines += [
            f"- Trial: {s['trial_id']} ({_cell(s['hypothesis'])})",
            f"- Accounts: {', '.join(s['accounts'])}",
            f"- Evaluated at: {s['evaluated_at']}; trial status: {s['trial_status']}; recommendation: "
            f"{_cell(s['recommendation'])}",
            "- Arms: " + ", ".join(f"{a['arm']} ({a['role']})" for a in s["arms"]),
            "- Windows: " + "; ".join(f"{w['label']} {w['start']} to {w['end']}" for w in s["windows"]),
            f"- Game versions: {', '.join(s['game_versions']) or 'none recorded'}",
            f"- Minimum sample: {s['min_sample']} {s['sample_unit']}(s) per arm; reached: "
            f"{'yes' if s['reached_min_sample'] else 'no'}",
            f"- Conclusion: **{s['conclusion']}**{reasons}",
            "",
        ]
        primary = s["primary_result"]
        lines += [f"Primary result: {primary['metric']} per {primary['unit']} "
                  f"({'higher' if primary['higher_is_better'] else 'lower'} is better). Source: "
                  f"{_cell(primary['source'])}.", "",
                  "| Arm | n | Estimate | 95% interval |", "| --- | --- | --- | --- |"]
        lines += [f"| {row['arm']} | {row['n']} | {_cell(row['estimate'])} | {_interval_cell(row['interval_95'])} |"
                  for row in primary["arms"]]
        if "market_return" in primary:
            lines += ["", f"Market return ({primary['currency']}, completed trades): "
                          f"{_value_cell(primary['market_return'])}."]
        lines.append("")
        if s["decide"] is None:
            lines.append(f"`decide()`: not applicable ({_cell(s['decide_note'])}).")
        else:
            d = s["decide"]
            lines.append(f"`decide()` returned **{d['decision']}** (n {d['baseline_n']} / {d['candidate_n']}): "
                         f"{_cell(d['reason'])}.")
            if s["decide_note"]:
                lines.append(f"Note: {_cell(s['decide_note'])}.")
        for metric, d in s["secondary_decisions"].items():
            lines.append(f"- Secondary {metric}: {d['decision']} ({_cell(d['reason'])})")
        lines += ["", "| Cost | Value |", "| --- | --- |"]
        lines += [f"| {name} | {_cell(_value_cell(value))} |" for name, value in s["costs"].items()]
        lines += ["", "| Failures | Value |", "| --- | --- |"]
        lines += [f"| {a['arm']} | {_cell(_value_cell(s['failures'][a['arm']]))} |" for a in s["arms"]]
        lines += ["", "Limitations carried from the trial output:", ""]
        lines += [f"- {note}" for note in s["limitations"]] or ["- none recorded"]
    lines += ["", "## Sources", "", "| Study | File | SHA-256 (first 16 hex digits) |", "| --- | --- | --- |"]
    lines += [f"| {s['study']} | {_cell(s['source']['file'])} | {s['source']['sha256']} |"
              for s in report["studies"] if s["source"]]
    if not any(s["source"] for s in report["studies"]):
        lines.append("| none | n/a | n/a |")
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
    parser = argparse.ArgumentParser(
        description="Build the progression trial exit report (SPEC-07), with --pvp the PvP strategy "
                    "comparison (SPEC-08), with --fleet the fleet trial report (SPEC-10), or with --study the "
                    "study report aggregate of the per-trial outputs (SPEC-12).")
    parser.add_argument("--plan", type=Path,
                        help="trial plan JSON (a PvP plan with --pvp, a fleet plan with --fleet); required "
                             "except with --study")
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument("--pvp", action="store_true", help="build the PvP strategy comparison (SPEC-08)")
    kind.add_argument("--fleet", action="store_true", help="build the fleet trial report (SPEC-10)")
    kind.add_argument("--study", action="store_true",
                      help="aggregate per-trial outputs given with --trial into the study report (SPEC-12)")
    parser.add_argument("--snapshots", action="append", default=[], metavar="ARM=PATH",
                        help="an arm's snapshot JSON file or directory (repeat per arm)")
    parser.add_argument("--journal", action="append", default=[], metavar="ARM=PATH",
                        help="an arm's gameplay journal JSONL (repeat per arm); with --pvp, a journal PATH "
                             "of the trial account (repeatable)")
    parser.add_argument("--minutes", type=Path,
                        help="operator minutes JSON: per account-day, or with --fleet per run and account")
    parser.add_argument("--interventions", type=Path, help="intervention log JSON")
    parser.add_argument("--adoptions", type=Path, help="with --pvp: the strategy adoption log JSON")
    parser.add_argument("--battle-history", type=Path, dest="battle_history",
                        help="with --pvp: parsed battle-history records JSON")
    parser.add_argument("--registry", type=Path, help="with --fleet: the account registry JSON")
    parser.add_argument("--run", action="append", default=[], type=Path, dest="runs", metavar="PATH",
                        help="with --fleet: an execute journal directory or one runs/*.json run record (repeatable)")
    parser.add_argument("--trial", action="append", default=[], type=Path, dest="trials", metavar="PATH",
                        help="with --study: a per-trial output JSON (the progression, --pvp or --fleet --json "
                             "report, or a market trial result); one per study, repeatable")
    parser.add_argument("--now", help="evaluation time, ISO 8601 UTC (default: the current time)")
    parser.add_argument("--json", type=Path, dest="json_out", help="write the JSON report here")
    parser.add_argument("--markdown", type=Path, help="write the Markdown report here")
    args = parser.parse_args(argv)
    if not args.study and args.plan is None:
        parser.error("the following arguments are required: --plan")
    try:
        if args.study:
            report, render = _study_from_args(args), study_to_markdown
        elif args.trials:
            raise TrialInputError("--trial needs --study")
        else:
            report, render = _trial_from_args(args)
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


def _study_from_args(args: argparse.Namespace) -> dict[str, Any]:
    others = (args.plan, args.snapshots, args.journal, args.minutes, args.interventions, args.adoptions,
              args.battle_history, args.registry, args.runs, args.now)
    if any(others):
        raise TrialInputError("--study reads only --trial outputs; the per-trial inputs and --now do not apply")
    trials, sources = load_study_inputs(args.trials)
    return build_study_report(trials, sources)


def _trial_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], Any]:
    """One per-trial report and its Markdown renderer."""
    now = _utc(args.now, "--now") if args.now else datetime.now(timezone.utc)
    if args.fleet:
        return _fleet_from_args(args, now), fleet_to_markdown
    if args.registry or args.runs:
        raise TrialInputError("--registry and --run need --fleet")
    if args.pvp:
        return _pvp_from_args(args, now), pvp_to_markdown
    if args.adoptions or args.battle_history:
        raise TrialInputError("--adoptions and --battle-history need --pvp")
    plan = plan_from_dict(_read_json(args.plan, "the trial plan"))
    snapshots = {arm: load_snapshots(path) for arm, path in _arm_paths(args.snapshots, "--snapshots").items()}
    journals = {arm: read_events(path) for arm, path in _arm_paths(args.journal, "--journal").items()}
    minutes = minutes_from_list(_read_json(args.minutes, "operator minutes"), plan) if args.minutes else {}
    interventions = (interventions_from_list(_read_json(args.interventions, "the intervention log"), plan)
                     if args.interventions else [])
    return build_trial_report(plan, snapshots, journals, minutes, interventions, now=now), to_markdown


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


def _fleet_from_args(args: argparse.Namespace, now: datetime) -> dict[str, Any]:
    if args.snapshots or args.journal or args.interventions or args.adoptions or args.battle_history:
        raise TrialInputError("--snapshots, --journal, --interventions, --adoptions and --battle-history do not "
                              "apply to --fleet; journals are read from each --run directory")
    if not args.registry or not args.runs:
        raise TrialInputError("--fleet needs --registry and at least one --run")
    plan = fleet_plan_from_dict(_read_json(args.plan, "the fleet plan"))
    if not args.registry.is_file():
        raise TrialInputError("the --registry path is not a file")
    registry = load_registry(args.registry)
    minutes = fleet_minutes_from_list(_read_json(args.minutes, "operator minutes"), plan) if args.minutes else {}
    return build_fleet_trial_report(plan, registry, load_fleet_runs(args.runs), minutes, now=now)


if __name__ == "__main__":
    raise SystemExit(main())
