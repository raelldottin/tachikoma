"""Sequential fleet orchestration (R5).

``FleetOrchestrator`` runs the daily scheduler for each enabled account in
the registry, one account at a time. Isolation rules:

- Each account gets its own client, snapshot provider, journal and design
  data from an injected factory. The orchestrator builds that account's
  runner with the account's own alias, so a snapshot of another account is
  refused by the runner and scheduler.
- Two accounts may not share a journal file.
- A failure or safety stop in one account is recorded and the next account
  still runs. Only the fleet stop file stops the whole fleet.

Budgets are enforced by planning within them: the spend already journaled
in the current period is subtracted from the account's budget, and the
policy's mineral and gas reserves are raised so the plan cannot spend more
than what remains. Starbux spending is never allowed in a fleet run, because
the policy cannot cap a starbux amount to a budget.

Dry run is the default.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from sdk.execution import ActionRunner
from sdk.fleet.accounts import AccountProfile, AccountRegistry
from sdk.journal import GameplayJournal
from sdk.policy import DesignData, PolicyConfig, get_policy
from sdk.redaction import redact_secrets
from sdk.scheduler import DailyScheduler, SchedulerConfig
from sdk.state import AccountSnapshot

MODE_SEQUENTIAL = "sequential"
RESOURCE_KINDS = ("minerals", "gas", "starbux")


@dataclass(frozen=True)
class AccountContext:
    client: Any
    snapshot_provider: Callable[[], AccountSnapshot]
    journal: GameplayJournal
    design: DesignData


AccountFactory = Callable[[AccountProfile], AccountContext]


@dataclass(frozen=True)
class AccountRun:
    alias: str
    status: str  # completed | stopped | error | disabled | not-run
    reason: Optional[str]
    attempted: int = 0
    verified: int = 0
    planned: int = 0
    elapsed_seconds: float = 0.0
    spent_this_period: dict = field(default_factory=dict)
    budget_remaining: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FleetRun:
    mode: str
    dry_run: bool
    started_at: str
    finished_at: str
    accounts: tuple[AccountRun, ...]
    stop_reason: Optional[str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def spent_in_period(journal: GameplayJournal, alias: str, since: datetime) -> dict:
    """Observed spend (negative cost deltas) by ``alias`` since ``since``."""
    spent: dict = {}
    for event in journal.read():
        if event.account_alias != alias or not event.attempted:
            continue
        if datetime.fromisoformat(event.finished_at) < since:
            continue
        for kind in RESOURCE_KINDS:
            value = (event.cost or {}).get(f"{kind}_delta")
            if value is not None and value < 0:
                spent[kind] = spent.get(kind, 0) - value
    return spent


def budgeted_policy_config(profile: AccountProfile, snapshot: AccountSnapshot, spent: dict,
                           base: PolicyConfig = PolicyConfig()) -> tuple[PolicyConfig, dict]:
    """Raise reserves so planned spend stays within what remains of each budget."""
    remaining = {}
    for kind in RESOURCE_KINDS:
        limit = profile.budget(kind)
        if limit is not None:
            remaining[kind] = max(limit - spent.get(kind, 0), 0)

    def reserve(kind: str, balance: Optional[int], current: int) -> int:
        if kind not in remaining or balance is None:
            return current  # an unobserved balance is already rejected by the policy
        return max(current, balance - remaining[kind])

    config = PolicyConfig(
        mineral_reserve=reserve("minerals", snapshot.resources.minerals, base.mineral_reserve),
        gas_reserve=reserve("gas", snapshot.resources.gas, base.gas_reserve),
        allow_starbux_spend=False,  # the policy cannot cap starbux spend to a budget
        max_requests=min(profile.max_actions, base.max_requests or profile.max_actions),
    )
    return config, remaining


class FleetOrchestrator:
    def __init__(
        self,
        registry: AccountRegistry,
        factory: AccountFactory,
        *,
        stop_file: Optional[Path] = None,
        period: timedelta = timedelta(hours=24),
        base_policy_config: PolicyConfig = PolicyConfig(),
        clock: Callable[[], datetime] = _utc_now,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self.factory = factory
        self.stop_file = stop_file
        self.period = period
        self.base_policy_config = base_policy_config
        self.clock = clock
        self.timer = timer

    def _stop_requested(self) -> bool:
        return self.stop_file is not None and self.stop_file.exists()

    def _run_account(self, profile: AccountProfile, execute: bool, journals: dict) -> AccountRun:
        started = self.timer()

        def finish(status: str, reason: Optional[str], **fields) -> AccountRun:
            return AccountRun(profile.alias, status, reason, elapsed_seconds=round(self.timer() - started, 3), **fields)

        try:
            context = self.factory(profile)
            journal_path = Path(context.journal.path).resolve()
            owner = journals.get(journal_path)
            if owner is not None and owner != profile.alias:
                return finish("error", f"journal {journal_path.name} is already used by {owner}")
            journals[journal_path] = profile.alias

            snapshot = context.snapshot_provider()
            if snapshot.account_alias != profile.alias:
                return finish("error", f"snapshot belongs to {snapshot.account_alias!r}")
            spent = spent_in_period(context.journal, profile.alias, self.clock() - self.period)
            policy_config, remaining = budgeted_policy_config(profile, snapshot, spent, self.base_policy_config)

            runner = ActionRunner(profile.alias, context.client, context.snapshot_provider, context.journal,
                                  clock=self.clock)
            scheduler = DailyScheduler(
                runner, get_policy(profile.policy_id), context.design, policy_config,
                SchedulerConfig(max_actions=profile.max_actions, period=self.period, stop_file=self.stop_file),
                clock=self.clock,
            )
            report = scheduler.run(execute=execute)
        except Exception as error:  # one account's failure must not stop the others
            return finish("error", f"{type(error).__name__}: {redact_secrets(str(error))}")

        attempted = sum(1 for result in report.results if result.attempted)
        verified = sum(1 for result in report.results if result.outcome == "verified")
        return finish(
            "completed" if report.completed else "stopped", report.stop_reason,
            attempted=attempted, verified=verified, planned=len(report.plan.requests),
            spent_this_period=spent, budget_remaining=remaining,
        )

    def run(self, *, execute: bool = False) -> FleetRun:
        started_at = self.clock()
        journals: dict = {}
        runs: list[AccountRun] = []
        stop_reason: Optional[str] = None
        for alias in self.registry.aliases():
            profile = self.registry.get(alias)
            if not profile.enabled:
                runs.append(AccountRun(alias, "disabled", "account disabled in the registry"))
                continue
            if stop_reason or self._stop_requested():
                stop_reason = stop_reason or f"fleet stop file present: {self.stop_file}"
                runs.append(AccountRun(alias, "not-run", stop_reason))
                continue
            runs.append(self._run_account(profile, execute, journals))
        return FleetRun(
            mode=MODE_SEQUENTIAL,
            dry_run=not execute,
            started_at=started_at.isoformat(),
            finished_at=self.clock().isoformat(),
            accounts=tuple(runs),
            stop_reason=stop_reason,
        )
