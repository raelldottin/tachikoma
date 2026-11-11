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

import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional, Protocol

from sdk.actions import check_preconditions, get_spec
from sdk.pvp.config import crew_level_cap
from sdk.state import AccountSnapshot, Completeness


@dataclass(frozen=True)
class RoomDesign:
    design_id: str
    name: str = ""
    upgrade_from: Optional[str] = None
    # Both None when the price is unknown: no @PriceString, or one that doesn't parse.
    cost_kind: Optional[str] = None
    cost_amount: Optional[int] = None


@dataclass(frozen=True)
class ResearchDesign:
    design_id: str
    root_id: Optional[str] = None
    name: str = ""
    gas_cost: Optional[int] = None  # None when @GasCost is missing or unparseable
    starbux_cost: Optional[int] = 0  # a missing @StarbuxCost is 0; None when unparseable


@dataclass(frozen=True)
class HullDesign:
    design_id: str
    level: Optional[int] = None  # @ShipLevel; None when missing or unparseable
    mineral_cost: Optional[int] = None  # @MineralCost; None when missing or unparseable


_COST = re.compile(r"\d+")


def _cost(value: Any) -> Optional[int]:
    """A non-negative whole-number cost, or None when ``value`` is missing or unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    return int(text) if _COST.fullmatch(text) else None


def _price(price: Any) -> tuple[Optional[str], Optional[int]]:
    """Split a ``kind:amount`` @PriceString; ``(None, None)`` when it is missing or unparseable."""
    kind, separator, amount = str(price or "").partition(":")
    kind = kind.strip()
    parsed = _cost(amount)
    if not kind or not separator or parsed is None:
        return None, None
    return kind, parsed


@dataclass(frozen=True)
class DesignData:
    rooms: tuple[RoomDesign, ...] = ()
    research: tuple[ResearchDesign, ...] = ()
    # Hull (ShipDesign) levels and mineral prices. Only hull-reserve-v1 reads them; empty by default.
    hulls: tuple[HullDesign, ...] = ()

    @classmethod
    def from_records(
        cls, room_designs: Iterable[Mapping[str, Any]] = (), research_designs: Iterable[Mapping[str, Any]] = (),
        ship_designs: Iterable[Mapping[str, Any]] = (),
    ) -> "DesignData":
        """Parse ``RoomDesign``, ``ResearchDesign`` and ``ShipDesign`` records as the client receives them."""
        rooms = []
        for record in room_designs:
            kind, amount = _price(record.get("@PriceString"))
            rooms.append(
                RoomDesign(
                    design_id=str(record.get("@RoomDesignId")),
                    name=str(record.get("@RoomName") or ""),
                    upgrade_from=(str(record["@UpgradeFromRoomDesignId"])
                                  if record.get("@UpgradeFromRoomDesignId") not in (None, "", "0") else None),
                    cost_kind=kind,
                    cost_amount=amount,
                )
            )
        research = [
            ResearchDesign(
                design_id=str(record.get("@ResearchDesignId")),
                root_id=str(record["@RootResearchDesignId"]) if record.get("@RootResearchDesignId") else None,
                name=str(record.get("@ResearchName") or ""),
                gas_cost=_cost(record.get("@GasCost")),
                # Gas-priced research carries no starbux price, so a missing @StarbuxCost is 0,
                # as Client.upgradeResearches reads it. A present but unparseable one is unknown.
                starbux_cost=_cost(record.get("@StarbuxCost", 0)),
            )
            for record in research_designs
        ]
        hulls = [
            HullDesign(
                design_id=str(record.get("@ShipDesignId")),
                level=_cost(record.get("@ShipLevel")),
                mineral_cost=_cost(record.get("@MineralCost")),
            )
            for record in ship_designs
        ]
        return cls(rooms=tuple(rooms), research=tuple(research), hulls=tuple(hulls))


@dataclass(frozen=True)
class PolicyConfig:
    mineral_reserve: int = 0
    gas_reserve: int = 0
    allow_starbux_spend: bool = False
    max_requests: Optional[int] = None
    # Caps on total planned spend, applied to the snapshot the plan is made from. None is no cap.
    max_mineral_spend: Optional[int] = None
    max_gas_spend: Optional[int] = None


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
    """Tracks resources still available after planned spend, reserves and spend caps."""

    def __init__(self, snapshot: AccountSnapshot, config: PolicyConfig) -> None:
        resources = snapshot.resources
        self.available = {
            "mineral": None if resources.minerals is None else resources.minerals - config.mineral_reserve,
            "gas": None if resources.gas is None else resources.gas - config.gas_reserve,
        }
        self.cap_left = {"mineral": config.max_mineral_spend, "gas": config.max_gas_spend}

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
        cap_left = self.cap_left[kind]
        if cap_left is not None and amount > cap_left:
            return f"needs {amount} {kind}, {max(cap_left, 0)} left under the {kind} spend cap"
        return None

    def spend(self, kind: Optional[str], amount: int) -> None:
        if kind in self.available and self.available[kind] is not None and amount > 0:
            self.available[kind] -= amount
            if self.cap_left[kind] is not None:
                self.cap_left[kind] -= amount


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
                  cost_kind: Optional[str] = None, cost_amount: int = 0, unknown_cost: Optional[str] = None) -> bool:
            failures = check_preconditions(get_spec(action_id), snapshot, params)
            if failures:
                rejected.append(Rejection(label, "; ".join(failures)))
                return False
            if unknown_cost:
                rejected.append(Rejection(label, f"cost unknown: {unknown_cost}"))
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
                if item.starbux_cost is None:
                    rejected.append(Rejection(label, f"cost unknown: research {item.design_id} has an "
                                                     "unparseable @StarbuxCost"))
                    continue
                if item.starbux_cost > 0 and not config.allow_starbux_spend:
                    rejected.append(Rejection(label, f"costs {item.starbux_cost} starbux; starbux spend not allowed"))
                    continue
                admit("add_research", {"research_design_id": item.design_id},
                      f"next research in line {item.root_id}", label, "gas", item.gas_cost or 0,
                      unknown_cost=(f"research {item.design_id} has no parseable @GasCost"
                                    if item.gas_cost is None else None))

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
                unknown = target.cost_kind is None or target.cost_amount is None
                if admit("upgrade_room", params, f"upgrade to {target.name or target.design_id}", label,
                         target.cost_kind, target.cost_amount or 0,
                         unknown_cost=(f"room design {target.design_id} has no parseable @PriceString"
                                       if unknown else None)):
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


@dataclass(frozen=True)
class HullHold:
    """The mineral reserve hull-reserve-v1 holds for the next hull upgrade."""

    next_level: int
    mineral_cost: int


def hull_hold(snapshot: AccountSnapshot, design: DesignData) -> tuple[Optional[HullHold], str]:
    """Decide whether ``snapshot`` is near its next hull upgrade, and what that upgrade costs.

    Near means every observed crew member has reached the current hull's crew
    level cap (``sdk.pvp.config.crew_level_cap``: hull level x 4, at most 40)
    and the next hull raises that cap. That is the crew criterion of
    ``sdk.pvp.scoring.hull_upgrade_readiness``; its other inputs (room max
    levels, native AI rules) are not in an ``AccountSnapshot``. Returns the
    hold, or ``None`` and why it isn't applied. Nothing unobserved is guessed.
    """
    level = snapshot.hull_level
    if level is None:
        return None, "hull level not observed"
    if level < 1:
        return None, f"hull level {level} is not a valid hull level"
    crew_state = snapshot.section("crew")
    if crew_state is not Completeness.COMPLETE:
        return None, f"crew section is {crew_state.value}, so crew levels are not fully observed"
    if not snapshot.crew:
        return None, "no crew observed"
    if any(member.level is None for member in snapshot.crew):
        return None, "a crew member's level is not observed"
    nxt = level + 1
    cap, next_cap = crew_level_cap(level), crew_level_cap(nxt)
    if next_cap <= cap:
        return None, f"the hull {nxt} upgrade does not raise the crew level cap ({cap})"
    below = sum(1 for member in snapshot.crew if member.level < cap)
    if below:
        return None, (f"not near the hull {nxt} upgrade: {below} of {len(snapshot.crew)} crew below "
                      f"the hull {level} crew level cap ({cap})")
    candidates = [hull for hull in design.hulls if hull.level == nxt]
    if not candidates:
        return None, f"no hull design for level {nxt} in the design data, so its cost is unknown"
    costs = {hull.mineral_cost for hull in candidates}
    if None in costs:
        return None, f"hull {nxt} cost unknown: a level {nxt} hull design has no parseable @MineralCost"
    if len(costs) > 1:
        listed = ", ".join(str(cost) for cost in sorted(cost for cost in costs if cost is not None))
        return None, (f"hull {nxt} cost is ambiguous ({listed} mineral); "
                      "the snapshot does not say which hull design the ship upgrades to")
    cost = next(iter(costs))
    assert cost is not None
    return HullHold(nxt, cost), f"near the hull {nxt} upgrade: every crew member is at the crew level cap ({cap})"


class HullReserveV1:
    """baseline-v1 with a mineral reserve held toward the next hull upgrade.

    Hypothesis (unproven): once every crew member is at the current hull's
    level cap, crew progress waits on the hull, so minerals are better saved
    for it than spent on room upgrades that would delay it. When ``hull_hold``
    finds the ship near its next hull upgrade, the mineral reserve is raised to
    that hull's mineral cost, and room upgrades the hold displaces are rejected
    with that reason. Otherwise the plan is baseline-v1's, plus a
    ``hull_reserve`` rejection saying why the hold was not applied.
    """

    name = "hull-reserve-v1"

    def __init__(self) -> None:
        self._baseline = BaselineV1()

    def plan(self, snapshot: AccountSnapshot, design: DesignData, config: PolicyConfig) -> Plan:
        hold, why = hull_hold(snapshot, design)
        if hold is not None and hold.mineral_cost <= config.mineral_reserve:
            why = (f"the configured mineral reserve ({config.mineral_reserve}) already covers "
                   f"the hull {hold.next_level} cost ({hold.mineral_cost} mineral)")
            hold = None
        if hold is None:
            base = self._baseline.plan(snapshot, design, config)
            note = Rejection("hull_reserve", f"not applied, planned as baseline-v1: {why}")
            return replace(base, policy=self.name, rejected=base.rejected + (note,))

        held = self._baseline.plan(snapshot, design, replace(config, mineral_reserve=hold.mineral_cost))
        # Rooms baseline-v1 would upgrade (before max_requests) but the hold refuses were displaced by it.
        unheld = self._baseline.plan(snapshot, design, replace(config, max_requests=None))
        displaced = {f"upgrade_room {request.params['room_id']}"
                     for request in unheld.requests if request.action_id == "upgrade_room"}
        held_note = (f"held for the hull {hold.next_level} upgrade: mineral reserve raised from "
                     f"{config.mineral_reserve} to its {hold.mineral_cost} mineral cost")
        rejected = tuple(
            Rejection(item.candidate, f"{item.reason} ({held_note})") if item.candidate in displaced else item
            for item in held.rejected
        )
        return replace(held, policy=self.name, rejected=rejected)


POLICIES: dict[str, ProgressionPolicy] = {BaselineV1.name: BaselineV1(), HullReserveV1.name: HullReserveV1()}


def get_policy(name: str) -> ProgressionPolicy:
    try:
        return POLICIES[name]
    except KeyError:
        raise KeyError(f"unknown progression policy {name!r}") from None
