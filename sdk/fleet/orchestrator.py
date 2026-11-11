"""Sequential fleet orchestration (R5).

``FleetOrchestrator`` runs the daily scheduler for each enabled account in
the registry, one account at a time. Isolation rules:

- Each account gets its own client, snapshot provider, journal and design
  data from an injected factory. The orchestrator builds that account's
  runner with the account's own alias, so a snapshot of another account is
  refused by the runner and scheduler.
- Two accounts may not share a journal file.
- Game identity (``FleetClaims.claim_snapshot``): the first snapshot's
  ``game_identity`` fingerprint must match the profile's
  ``expected_game_identity`` when one is declared, must not be the identity
  the registry declares for another account, and must not be one another
  account already used in this run. Later snapshots must keep it.
- A failure or safety stop in one account is recorded and the next account
  still runs. Only the fleet stop file stops the whole fleet.

Budgets are enforced by planning within them: the spend already journaled
in the current period (observed spend, plus the planned cost of attempts
whose effect was not observed) is subtracted from the account's budget, and what
remains is passed to the policy as its mineral and gas spend caps. The
policy applies those caps to whichever snapshot it plans from, so balance
growth between the orchestrator's first snapshot and the scheduler's planning
snapshot never adds spendable budget. Starbux spending is never allowed in a
fleet run, because the policy cannot cap a starbux amount to a budget.

Dry run is the default.

Per account the steps are: factory, journal claim, first snapshot, snapshot
claim (``FleetClaims``), then the budgeted scheduler run (``_run_scheduler``).
``sdk.fleet.concurrent.ConcurrentFleetOrchestrator`` runs the same steps on a
bounded worker pool.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from sdk.execution import ActionRunner
from sdk.fleet.accounts import AccountProfile, AccountRegistry, game_identity_refusal, pin_game_identity
from sdk.fleet.audit import charged_spend
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
    game_identity: Optional[str] = None  # accepted fingerprint, never the raw user id


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
    """Spend charged to ``alias`` since ``since``.

    Observed spend (negative cost deltas), plus the planned amount
    (``<kind>_uncertain``) of attempts whose delta was not observed, so an
    uncertain attempt never leaves its cost spendable again.
    """
    spent: dict = {}
    for event in journal.read():
        if event.account_alias != alias or not event.attempted:
            continue
        if datetime.fromisoformat(event.finished_at) < since:
            continue
        for kind, (amount, _uncertain) in charged_spend(event.cost).items():
            spent[kind] = spent.get(kind, 0) + amount
    return spent


def budgeted_policy_config(profile: AccountProfile, spent: dict,
                           base: PolicyConfig = PolicyConfig()) -> tuple[PolicyConfig, dict]:
    """Cap planned mineral and gas spend at what remains of each budget.

    The caps are absolute amounts, not reserves derived from a balance, so they
    hold whichever snapshot the scheduler plans from.
    """
    remaining = {}
    for kind in RESOURCE_KINDS:
        limit = profile.budget(kind)
        if limit is not None:
            remaining[kind] = max(limit - spent.get(kind, 0), 0)

    def cap(kind: str, current: Optional[int]) -> Optional[int]:
        if kind not in remaining:
            return current
        return remaining[kind] if current is None else min(current, remaining[kind])

    config = PolicyConfig(
        mineral_reserve=base.mineral_reserve,
        gas_reserve=base.gas_reserve,
        allow_starbux_spend=False,  # the policy cannot cap starbux spend to a budget
        max_requests=min(profile.max_actions, base.max_requests or profile.max_actions),
        max_mineral_spend=cap("minerals", base.max_mineral_spend),
        max_gas_spend=cap("gas", base.max_gas_spend),
    )
    return config, remaining


def error_reason(error: BaseException) -> str:
    """An account error as recorded in its ``AccountRun``, with secret-shaped text redacted."""
    return f"{type(error).__name__}: {redact_secrets(str(error))}"


class FleetClaims:
    """What each account has claimed in one fleet run, decided before it makes any game call.

    ``claim_journal``: a journal path is owned by the first account to present
    it; any other account presenting the same resolved path is refused.

    ``claim_snapshot``: the account's first snapshot must belong to the
    account, and its ``game_identity`` must pass ``game_identity_refusal``:
    it matches the profile's declared identity, it is not the identity the
    registry declares for another account, and no other account was accepted
    with it in this run. An accepted identity is recorded, and
    ``pinned_provider`` keeps the account's later snapshots on it; an
    identity that was unknown (``None``) stays unknown. Without a
    ``registry`` only the in-run duplicate check applies. A subclass returned
    by ``FleetOrchestrator._new_claims`` can extend these claims.

    Both orchestrators call these in alias order, so the first alias wins a
    contested claim. Claims are lock-guarded so concurrent callers always see
    one consistent owner.
    """

    def __init__(self, registry: Optional[AccountRegistry] = None) -> None:
        self._journal_owners: dict[Path, str] = {}
        self._registry = registry
        self._declared = registry.declared_identities() if registry is not None else {}
        self._identity_owners: dict[str, str] = {}
        self._identities: dict[str, Optional[str]] = {}
        self._lock = threading.Lock()

    def claim_journal(self, alias: str, journal: GameplayJournal) -> Optional[str]:
        """Claim ``journal`` for ``alias``; return a refusal reason if another account owns it."""
        path = Path(journal.path).resolve()
        with self._lock:
            owner = self._journal_owners.setdefault(path, alias)
        if owner != alias:
            return f"journal {path.name} is already used by {owner}"
        return None

    def claim_snapshot(self, alias: str, snapshot: AccountSnapshot) -> Optional[str]:
        """Check ``alias``'s first snapshot; return a refusal reason if the account may not proceed."""
        if snapshot.account_alias != alias:
            return f"snapshot belongs to {snapshot.account_alias!r}"
        expected = None
        if self._registry is not None and alias in self._registry:
            expected = self._registry.get(alias).expected_game_identity
        observed = getattr(snapshot, "game_identity", None)
        with self._lock:
            refusal = game_identity_refusal(alias, expected, observed, self._identity_owners, self._declared)
            if refusal is None:
                self._identities[alias] = observed
                if observed is not None:
                    self._identity_owners[observed] = alias
        return refusal

    def identity(self, alias: str) -> Optional[str]:
        """The game identity accepted for ``alias`` in this run, or ``None``."""
        with self._lock:
            return self._identities.get(alias)

    def pinned_provider(self, alias: str, provider: Callable[[], AccountSnapshot]) -> Callable[[], AccountSnapshot]:
        """``provider``, refusing any later snapshot whose identity differs from the accepted one."""
        return pin_game_identity(provider, self.identity(alias))


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

    def _finish(self, profile: AccountProfile, started: float, status: str, reason: Optional[str],
                **fields) -> AccountRun:
        return AccountRun(profile.alias, status, reason, elapsed_seconds=round(self.timer() - started, 3), **fields)

    def _new_claims(self) -> FleetClaims:
        """The claim table for one run; override to add claims."""
        return FleetClaims(self.registry)

    def _run_scheduler(self, profile: AccountProfile, context: AccountContext, execute: bool,
                       started: float, claims: Optional[FleetClaims] = None) -> AccountRun:
        """Plan within the budget and run the scheduler for an account whose claims all succeeded.

        With ``claims``, every snapshot the runner and scheduler take is pinned
        to the game identity accepted for the account.
        """
        identity = claims.identity(profile.alias) if claims is not None else None
        try:
            spent = spent_in_period(context.journal, profile.alias, self.clock() - self.period)
            policy_config, remaining = budgeted_policy_config(profile, spent, self.base_policy_config)

            provider = (claims.pinned_provider(profile.alias, context.snapshot_provider) if claims is not None
                        else context.snapshot_provider)
            runner = ActionRunner(profile.alias, context.client, provider, context.journal, clock=self.clock)
            scheduler = DailyScheduler(
                runner, get_policy(profile.policy_id), context.design, policy_config,
                SchedulerConfig(max_actions=profile.max_actions, period=self.period, stop_file=self.stop_file),
                clock=self.clock,
            )
            report = scheduler.run(execute=execute)
        except Exception as error:  # one account's failure must not stop the others
            return self._finish(profile, started, "error", error_reason(error))

        attempted = sum(1 for result in report.results if result.attempted)
        verified = sum(1 for result in report.results if result.outcome == "verified")
        return self._finish(
            profile, started, "completed" if report.completed else "stopped", report.stop_reason,
            attempted=attempted, verified=verified, planned=len(report.plan.requests),
            spent_this_period=spent, budget_remaining=remaining, game_identity=identity,
        )

    def _run_account(self, profile: AccountProfile, execute: bool, claims: FleetClaims) -> AccountRun:
        started = self.timer()
        try:
            context = self.factory(profile)
            refusal = claims.claim_journal(profile.alias, context.journal)
            if refusal is None:
                refusal = claims.claim_snapshot(profile.alias, context.snapshot_provider())
        except Exception as error:  # one account's failure must not stop the others
            return self._finish(profile, started, "error", error_reason(error))
        if refusal is not None:
            return self._finish(profile, started, "error", refusal)
        return self._run_scheduler(profile, context, execute, started, claims)

    def run(self, *, execute: bool = False) -> FleetRun:
        started_at = self.clock()
        claims = self._new_claims()
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
            runs.append(self._run_account(profile, execute, claims))
        return FleetRun(
            mode=MODE_SEQUENTIAL,
            dry_run=not execute,
            started_at=started_at.isoformat(),
            finished_at=self.clock().isoformat(),
            accounts=tuple(runs),
            stop_reason=stop_reason,
        )
