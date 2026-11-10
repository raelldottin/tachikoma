"""Versioned progression policies (roadmap R2 / AUTO-005).

A policy turns one ``AccountSnapshot`` plus static design data into an ordered
list of catalog ``ActionRequest``s, and records every candidate it rejected
with the reason. Policies are pure: they perform no I/O, and design data
(room upgrade costs, research costs) is passed in rather than fetched.

``baseline-v1`` reproduces ``run.py``'s current daily order, as far as the
action catalog can express it, so automated runs can be compared against
today's behaviour rather than against an invented strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Protocol

from sdk.actions import check_preconditions, get_spec
from sdk.state import AccountSnapshot, Completeness


@dataclass(frozen=True)
class RoomDesign:
    design_id: str
    name: str = ""
    upgrade_from: Optional[str] = None
    cost_kind: Optional[str] = None  # "mineral", "gas" or None when unpriced
    cost_amount: int = 0


@dataclass(frozen=True)
class ResearchDesign:
    design_id: str
    root_id: Optional[str] = None
    name: str = ""
    gas_cost: int = 0
    starbux_cost: int = 0


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class DesignData:
    rooms: tuple[RoomDesign, ...] = ()
    research: tuple[ResearchDesign, ...] = ()

    @classmethod
    def from_records(
        cls, room_designs: Iterable[Mapping[str, Any]] = (), research_designs: Iterable[Mapping[str, Any]] = ()
    ) -> "DesignData":
        """Parse ``RoomDesign`` and ``ResearchDesign`` records as the client receives them."""
        rooms = []
        for record in room_designs:
            price = str(record.get("@PriceString") or "")
            kind, _, amount = price.partition(":")
            rooms.append(
                RoomDesign(
                    design_id=str(record.get("@RoomDesignId")),
                    name=str(record.get("@RoomName") or ""),
                    upgrade_from=(str(record["@UpgradeFromRoomDesignId"])
                                  if record.get("@UpgradeFromRoomDesignId") not in (None, "", "0") else None),
                    cost_kind=kind or None,
                    cost_amount=_int(amount),
                )
            )
        research = [
            ResearchDesign(
                design_id=str(record.get("@ResearchDesignId")),
                root_id=str(record["@RootResearchDesignId"]) if record.get("@RootResearchDesignId") else None,
                name=str(record.get("@ResearchName") or ""),
                gas_cost=_int(record.get("@GasCost", 0)),
                starbux_cost=_int(record.get("@StarbuxCost", 0)),
            )
            for record in research_designs
        ]
        return cls(rooms=tuple(rooms), research=tuple(research))


@dataclass(frozen=True)
class PolicyConfig:
    mineral_reserve: int = 0
    gas_reserve: int = 0
    allow_starbux_spend: bool = False
    max_requests: Optional[int] = None


@dataclass(frozen=True)
class ActionRequest:
    action_id: str
    params: Mapping[str, Any]
    reason: str
    expected_cost: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Rejection:
    candidate: str
    reason: str


@dataclass(frozen=True)
class Plan:
    policy: str
    account_alias: str
    observed_at: str
    requests: tuple[ActionRequest, ...]
    rejected: tuple[Rejection, ...]


class ProgressionPolicy(Protocol):
    name: str

    def plan(self, snapshot: AccountSnapshot, design: DesignData, config: PolicyConfig) -> Plan: ...


class _Budget:
    """Tracks resources still available after planned spend and reserves."""

    def __init__(self, snapshot: AccountSnapshot, config: PolicyConfig) -> None:
        resources = snapshot.resources
        self.available = {
            "mineral": None if resources.minerals is None else resources.minerals - config.mineral_reserve,
            "gas": None if resources.gas is None else resources.gas - config.gas_reserve,
        }

    def check(self, kind: Optional[str], amount: int) -> Optional[str]:
        if not kind or amount <= 0:
            return None
        if kind not in self.available:
            return f"unsupported cost type {kind!r}"
        remaining = self.available[kind]
        if remaining is None:
            return f"{kind} balance not observed"
        if amount > remaining:
            return f"needs {amount} {kind}, {max(remaining, 0)} available above reserve"
        return None

    def spend(self, kind: Optional[str], amount: int) -> None:
        if kind in self.available and self.available[kind] is not None and amount > 0:
            self.available[kind] -= amount


class BaselineV1:
    """run.py's daily order: research, room upgrades (each followed by a
    resource collection, as ``Client.upgradeRooms`` does), then the daily
    reward. Crew training is not modelled; see the policy doc."""

    name = "baseline-v1"

    def plan(self, snapshot: AccountSnapshot, design: DesignData, config: PolicyConfig) -> Plan:
        requests: list[ActionRequest] = []
        rejected: list[Rejection] = []
        budget = _Budget(snapshot, config)

        def admit(action_id: str, params: Mapping[str, Any], reason: str, label: str,
                  cost_kind: Optional[str] = None, cost_amount: int = 0) -> bool:
            failures = check_preconditions(get_spec(action_id), snapshot, params)
            if failures:
                rejected.append(Rejection(label, "; ".join(failures)))
                return False
            problem = budget.check(cost_kind, cost_amount)
            if problem:
                rejected.append(Rejection(label, problem))
                return False
            budget.spend(cost_kind, cost_amount)
            cost = {cost_kind: cost_amount} if cost_kind and cost_amount else {}
            requests.append(ActionRequest(action_id, dict(params), reason, cost))
            return True

        # 1. Research: one design per research line, mirroring Client.upgradeResearches.
        if snapshot.section("research") is Completeness.MISSING:
            rejected.append(Rejection("research", "research section not observed"))
        else:
            busy_roots = {
                item.root_id for item in design.research
                if snapshot.research_state(item.design_id) == "Researching"
            }
            seen_roots: set[Optional[str]] = set()
            for item in design.research:
                label = f"add_research {item.design_id}"
                if snapshot.research_state(item.design_id) in ("Researching", "Completed"):
                    continue
                if item.root_id in busy_roots:
                    rejected.append(Rejection(label, f"research line {item.root_id} is already researching"))
                    continue
                if item.root_id in seen_roots:
                    continue
                seen_roots.add(item.root_id)
                if item.starbux_cost > 0 and not config.allow_starbux_spend:
                    rejected.append(Rejection(label, f"costs {item.starbux_cost} starbux; starbux spend not allowed"))
                    continue
                admit("add_research", {"research_design_id": item.design_id},
                      f"next research in line {item.root_id}", label, "gas", item.gas_cost)

        # 2. Room upgrades, each followed by a collection.
        if snapshot.section("rooms") is Completeness.MISSING:
            rejected.append(Rejection("rooms", "rooms section not observed"))
        else:
            upgrades = {room.upgrade_from: room for room in design.rooms if room.upgrade_from}
            for room in snapshot.rooms:
                target = upgrades.get(room.design_id)
                if target is None:
                    continue
                label = f"upgrade_room {room.room_id}"
                params = {"room_id": room.room_id, "upgrade_room_design_id": target.design_id}
                if admit("upgrade_room", params, f"upgrade to {target.name or target.design_id}", label,
                         target.cost_kind, target.cost_amount):
                    admit("collect_all_resources", {}, "collect after a room upgrade, as upgradeRooms does",
                          "collect_all_resources")

        # 3. Daily reward.
        admit("collect_daily_reward", {}, "daily reward", "collect_daily_reward")

        rejected.append(Rejection("add_training", "crew training is not modelled by baseline-v1"))

        if config.max_requests is not None and len(requests) > config.max_requests:
            for request in requests[config.max_requests:]:
                rejected.append(Rejection(request.action_id, f"over max_requests ({config.max_requests})"))
            requests = requests[: config.max_requests]

        return Plan(
            policy=self.name,
            account_alias=snapshot.account_alias,
            observed_at=snapshot.observed_at.isoformat(),
            requests=tuple(requests),
            rejected=tuple(rejected),
        )


POLICIES: dict[str, ProgressionPolicy] = {BaselineV1.name: BaselineV1()}


def get_policy(name: str) -> ProgressionPolicy:
    try:
        return POLICIES[name]
    except KeyError:
        raise KeyError(f"unknown progression policy {name!r}") from None
