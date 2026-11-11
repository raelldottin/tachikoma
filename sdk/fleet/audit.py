"""Cross-account isolation audit (R5).

``audit_fleet`` checks each account's journal against the registry and
reports incidents. Rules:

- ``foreign-event``: an event whose alias is not the journal's owner.
- ``unregistered-account``: a journal owner or event alias missing from the
  registry.
- ``attempt-id-reused``: one attempt id in more than one account's journal,
  or twice in one journal.
- ``overlapping-attempts``: two attempted mutating events on one account
  whose time windows overlap.
- ``budget-exceeded``: observed spend in one budget period above the
  account's budget for that currency. Periods are fixed windows of
  ``period`` length counted from the Unix epoch (UTC days by default).

An empty report still states how many accounts and events were checked.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Optional

from sdk.fleet.accounts import AccountRegistry
from sdk.journal import GameplayEvent

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
RESOURCE_KINDS = ("minerals", "gas", "starbux")


@dataclass(frozen=True)
class Incident:
    rule: str
    account_alias: str
    attempt_ids: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class AuditReport:
    accounts_checked: int
    events_checked: int
    incidents: tuple[Incident, ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        return not self.incidents

    def by_rule(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for incident in self.incidents:
            counts[incident.rule] = counts.get(incident.rule, 0) + 1
        return counts

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def _period_start(moment: datetime, period: timedelta) -> datetime:
    index = (moment - EPOCH) // period
    return EPOCH + index * period


def audit_fleet(
    registry: AccountRegistry,
    journals: Mapping[str, Iterable[GameplayEvent]],
    *,
    period: timedelta = timedelta(hours=24),
) -> AuditReport:
    incidents: list[Incident] = []
    events_checked = 0
    attempt_owners: dict[str, list[str]] = defaultdict(list)

    for owner in sorted(journals):
        events = list(journals[owner])
        events_checked += len(events)
        if owner not in registry:
            incidents.append(Incident("unregistered-account", owner, (), f"journal owner {owner!r} is not registered"))

        for event in events:
            attempt_owners[event.attempt_id].append(owner)
            if event.account_alias != owner:
                incidents.append(Incident(
                    "foreign-event", owner, (event.attempt_id,),
                    f"{event.action_id} event for {event.account_alias!r} in {owner!r}'s journal",
                ))
                if event.account_alias not in registry:
                    incidents.append(Incident(
                        "unregistered-account", event.account_alias, (event.attempt_id,),
                        f"event alias {event.account_alias!r} is not registered",
                    ))

        own = [e for e in events if e.account_alias == owner and e.attempted]
        windows = sorted(
            ((datetime.fromisoformat(e.started_at), datetime.fromisoformat(e.finished_at), e) for e in own),
            key=lambda item: item[0],
        )
        for index, (start, end, event) in enumerate(windows):
            for other_start, other_end, other in windows[index + 1:]:
                if other_start >= end:
                    break
                incidents.append(Incident(
                    "overlapping-attempts", owner, (event.attempt_id, other.attempt_id),
                    f"{event.action_id} and {other.action_id} were in flight at the same time",
                ))

        if owner in registry:
            profile = registry.get(owner)
            spent: dict[tuple, int] = defaultdict(int)
            contributors: dict[tuple, list[str]] = defaultdict(list)
            for event in own:
                bucket = _period_start(datetime.fromisoformat(event.finished_at), period)
                for kind in RESOURCE_KINDS:
                    value = (event.cost or {}).get(f"{kind}_delta")
                    if value is not None and value < 0:
                        spent[(bucket, kind)] += -value
                        contributors[(bucket, kind)].append(event.attempt_id)
            for (bucket, kind), amount in sorted(spent.items()):
                limit: Optional[int] = profile.budget(kind)
                if limit is not None and amount > limit:
                    incidents.append(Incident(
                        "budget-exceeded", owner, tuple(contributors[(bucket, kind)]),
                        f"spent {amount} {kind} in the period starting {bucket.isoformat()}, budget {limit}",
                    ))

    for attempt_id, owners in sorted(attempt_owners.items()):
        if len(owners) > 1:
            incidents.append(Incident(
                "attempt-id-reused", owners[0], (attempt_id,),
                f"attempt id appears {len(owners)} times across journals of {', '.join(sorted(set(owners)))}",
            ))

    return AuditReport(accounts_checked=len(journals), events_checked=events_checked, incidents=tuple(incidents))
