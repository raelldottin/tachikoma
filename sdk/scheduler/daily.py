"""Daily progression scheduler (roadmap R2 / AUTO-004).

Plans once from a fresh snapshot with a progression policy, then executes the
plan's requests one at a time through ``sdk.execution.ActionRunner``. The
runner takes a fresh snapshot before and after every action, so each action
is checked against current state, not the state the plan was made from.

Safety rules:

- Dry run is the default. A mutating call happens only with ``execute=True``.
- An operator stop file is checked before every action.
- A per-run action budget caps how many requests are attempted.
- Once-per-period actions already verified, or still uncertain, within the
  period are skipped, judged from journal history.
- The run stops on the first ``contradicted`` outcome, on an ``unknown``
  outcome for a non-idempotent action, and on any refusal from the runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from sdk.actions import Idempotency, get_spec
from sdk.execution import ActionRunner, RunResult, UncertainPriorAttemptError
from sdk.policy import ActionRequest, DesignData, Plan, PolicyConfig, ProgressionPolicy
from sdk.state import AccountMismatchError


@dataclass(frozen=True)
class SchedulerConfig:
    max_actions: int = 20
    period: timedelta = timedelta(hours=24)
    stop_file: Optional[Path] = None


@dataclass(frozen=True)
class SkippedRequest:
    request: ActionRequest
    reason: str


@dataclass(frozen=True)
class ScheduleReport:
    account_alias: str
    plan: Plan
    dry_run: bool
    results: tuple[RunResult, ...]
    skipped: tuple[SkippedRequest, ...]
    stop_reason: Optional[str]

    @property
    def completed(self) -> bool:
        return self.stop_reason is None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DailyScheduler:
    def __init__(
        self,
        runner: ActionRunner,
        policy: ProgressionPolicy,
        design: DesignData,
        policy_config: PolicyConfig = PolicyConfig(),
        config: SchedulerConfig = SchedulerConfig(),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.runner = runner
        self.policy = policy
        self.design = design
        self.policy_config = policy_config
        self.config = config
        self.clock = clock

    def _done_this_period(self, action_id: str) -> bool:
        cutoff = self.clock() - self.config.period
        for event in self.runner.journal.read():
            if (
                event.account_alias == self.runner.account_alias
                and event.action_id == action_id
                and event.attempted
                and event.outcome in ("verified", "unknown")
                and datetime.fromisoformat(event.finished_at) >= cutoff
            ):
                return True
        return False

    def _stop_requested(self) -> bool:
        return self.config.stop_file is not None and self.config.stop_file.exists()

    def run(self, *, execute: bool = False) -> ScheduleReport:
        snapshot = self.runner.snapshot_provider()
        if snapshot.account_alias != self.runner.account_alias:
            raise AccountMismatchError(
                f"snapshot belongs to {snapshot.account_alias!r}, scheduler is for {self.runner.account_alias!r}"
            )
        plan = self.policy.plan(snapshot, self.design, self.policy_config)

        results: list[RunResult] = []
        skipped: list[SkippedRequest] = []
        stop_reason: Optional[str] = None

        for request in plan.requests:
            if self._stop_requested():
                stop_reason = f"operator stop file present: {self.config.stop_file}"
                break
            if len(results) >= self.config.max_actions:
                stop_reason = f"action budget reached ({self.config.max_actions})"
                break
            spec = get_spec(request.action_id)
            if spec.idempotency is Idempotency.ONCE_PER_PERIOD and self._done_this_period(spec.action_id):
                skipped.append(SkippedRequest(request, "already verified or uncertain within this period"))
                continue
            try:
                # The planned cost lets the runner journal it as possibly spent when
                # the effect cannot be observed, so budgets still charge it.
                result = self.runner.run(request.action_id, request.params, dry_run=not execute,
                                         expected_cost=request.expected_cost)
            except (UncertainPriorAttemptError, AccountMismatchError) as error:
                stop_reason = f"runner refused {request.action_id}: {error}"
                break
            results.append(result)
            if result.outcome == "contradicted":
                stop_reason = f"{request.action_id} contradicted: {result.reason}"
                break
            if result.outcome == "unknown" and spec.idempotency is Idempotency.NOT_IDEMPOTENT:
                stop_reason = f"{request.action_id} outcome uncertain: {result.reason}"
                break

        return ScheduleReport(
            account_alias=self.runner.account_alias,
            plan=plan,
            dry_run=not execute,
            results=tuple(results),
            skipped=tuple(skipped),
            stop_reason=stop_reason,
        )
