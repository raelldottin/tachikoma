"""The world a FakePss serves: plain dataclasses, no I/O and no clock.

Every date is FAKE_EPOCH arithmetic. All identifiers, names and the password are synthetic.

Two ready-made worlds:

- ``default_world()``: the realistic account the golden lists in tests/test_daily_run_offline_e2e.py are built on.
  Changing it shifts every golden, so build a variant (``w = default_world(); w.account.... = ...``) instead.
- ``minimal_world()``: an account on which the daily run only collects resources (upgradeResearches always does).
  Add crew with ``world.add_crew(...)`` (and rooms with ``world.add_room(...)``) for a run whose only other activity
  is training. A single CharacterDesign is fine (defect D5, fixed).

``World.validate()`` (FakePss calls it) rejects dangling ids, so a typo in a hand-built world fails loudly instead of
letting production silently skip a crew member whose design is unknown.

Training reads everything it needs itself (sdk/execution/training.py): the crew, the ship's rooms and researches and
the character, training, room and research designs, so manageTraining works without run.main()'s earlier steps
(defect D15, fixed).

Defect IDs (DR1, H1, ...) are listed in KNOWN_DEFECTS in tests/test_daily_run_offline_e2e.py.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from tests.synthetic_fixtures import SYNTHETIC_EMAIL

# Production order (sdk/training/model.py ``CANONICAL_STATS``), without the "@".
STATS = ("HpImprovement", "PilotImprovement", "RepairImprovement", "WeaponImprovement", "ScienceImprovement",
         "EngineImprovement", "AttackImprovement", "AbilityImprovement", "StaminaImprovement")
FAKE_EPOCH = datetime.datetime(2026, 11, 20, 12, 0, 0)  # naive UTC; test code never calls utcnow()
FAKE_PASSWORD = "synthetic-daily-pass-0001"  # synthetic; never a real credential
GUEST_USER_ID = "700000999"


def fmt(dt: datetime.datetime | None) -> str:
    """The server's date format (no fractional seconds), or "" for no date."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else ""


PRICE_KINDS = ("mineral", "gas", "starbux")


def parse_price(price: str) -> tuple[str, int]:
    """A RoomDesign PriceString ("mineral:5000") as (kind, amount); ValueError for anything else.

    An empty or unknown price is a misconfigured world, never a free upgrade.
    """
    kind, _, amount = price.partition(":")
    if kind not in PRICE_KINDS or not amount.isdigit():
        raise ValueError(f"PriceString {price!r} is not <{'|'.join(PRICE_KINDS)}>:<amount>")
    return kind, int(amount)


def stats(**values: int) -> dict[str, int]:
    """All nine stats, zero unless given: ``stats(Hp=20, Weapon=40)``."""
    unknown = set(values) - {name[: -len("Improvement")] for name in STATS}
    if unknown:
        raise KeyError(f"unknown stats {sorted(unknown)}")
    return {name: values.get(name[: -len("Improvement")], 0) for name in STATS}


_zero_stats = stats  # for builders whose ``stats`` parameter shadows the function

@dataclass
class Account:
    user_id: str = "700000501"
    name: str = "SyntheticCaptain"
    email: str = SYNTHETIC_EMAIL
    password: str = FAKE_PASSWORD
    refresh_token: str = "RT-synthetic-daily-0001"
    credits: int = 1200
    free_starbux_today: int = 10
    daily_reward_status: str = "0"
    last_heartbeat: datetime.datetime = FAKE_EPOCH - datetime.timedelta(minutes=5)


@dataclass
class Ship:
    ship_id: str = "800000501"
    name: str = "SyntheticShip"
    level: int = 8
    design_id: str = "108"
    star_system_id: str = "42"
    upgrade_design_id: str = "109"
    arrival: datetime.datetime | None = None


@dataclass
class Resources:
    mineral: int = 60000
    gas: int = 40000


@dataclass
class RoomDesign:
    id: str
    name: str
    room_type: str
    upgrade_from: str = "0"
    price: str = ""
    column_width: int = 2
    row_height: int = 1
    room_hp: int = 10
    power_generated: int = 0
    max_population: int = 0


@dataclass
class Room:
    id: str
    design_id: str
    status: str = "Normal"
    row: int = 0
    column: int = 0
    pending_design_id: str | None = None


@dataclass
class ResearchDesign:
    id: str
    root_id: str
    name: str
    gas_cost: int
    starbux_cost: int = 0


@dataclass
class Research:
    design_id: str
    state: str = "Completed"
    # Emitted only when set. Real ListAllResearches records carry no ResearchName; training joins the design id with
    # ListAllResearchDesigns2 instead (defect D1, fixed).
    name: str | None = None
    level: int | None = None


@dataclass
class CharacterDesign:
    id: str
    name: str
    training_capacity: int = 100
    rarity: str = "Common"
    special_ability_type: str = "None"


@dataclass
class TrainingSession:
    design_id: str
    start: datetime.datetime
    end: datetime.datetime
    applied: bool = False


@dataclass
class Character:
    id: str
    name: str
    design_id: str
    room_id: str
    level: int = 40
    xp: int = 0
    fatigue: int = 0
    stats: dict[str, int] = field(default_factory=stats)
    session: TrainingSession | None = None
    available_date: datetime.datetime | None = None

    @property
    def training_end(self) -> datetime.datetime | None:
        """The TrainingEndDate the server reports: the last session's end, still reported after FinishTraining."""
        return self.session.end if self.session else None

    @property
    def last_session(self) -> TrainingSession:
        """The last session, finished or not; AssertionError when the crew never trained (narrows the Optional)."""
        assert self.session is not None, f"character {self.id} has no training session"
        return self.session


@dataclass
class TrainingDesign:
    id: str
    name: str
    stat: str
    gain: int
    fatigue: int
    duration: datetime.timedelta
    price: str | None = None


@dataclass
class Item:
    id: str
    design_id: str
    quantity: int


@dataclass
class TaskDesign:
    id: str
    name: str
    objective: int


@dataclass
class Task:
    design_id: str
    progress: int
    collected: bool = False


@dataclass
class Marker:
    id: str
    star_system_id: str
    marker_type: str = "Mining"
    collected: bool = False
    reward: str = "mineral:500"
    mission_design_id: str = "0"
    mission_event_id: str = "0"
    user_marker: bool = False  # True: emitted under UserMarkers instead of StarSystemMarkers


@dataclass
class Message:
    id: str
    text: str
    activity_argument: str
    collected: bool = False
    actioned: bool = False


@dataclass
class MarketListing:
    id: str
    text: str
    activity_argument: str


def _default_design_versions() -> dict[str, str]:
    return {"Room": "11", "Research": "12", "Task": "13", "Training": "14", "Character": "15"}


def _find(rows, key: str, kind: str):
    for row in rows:
        if row.id == key:
            return row
    raise KeyError(f"no {kind} {key!r}")


@dataclass
class World:
    account: Account = field(default_factory=Account)
    ship: Ship = field(default_factory=Ship)
    resources: Resources = field(default_factory=Resources)
    room_designs: list[RoomDesign] = field(default_factory=list)
    rooms: list[Room] = field(default_factory=list)
    research_designs: list[ResearchDesign] = field(default_factory=list)
    researches: list[Research] = field(default_factory=list)
    character_designs: list[CharacterDesign] = field(default_factory=list)
    characters: list[Character] = field(default_factory=list)
    training_designs: list[TrainingDesign] = field(default_factory=list)
    report_resource_items: bool = False
    items: list[Item] = field(default_factory=list)
    task_designs: list[TaskDesign] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    market_listings: list[MarketListing] = field(default_factory=list)
    daily_reward_argument: str = "3"
    max_concurrent_upgrades: int = 2
    travel_seconds: int = 600
    # CollectAllResources lists only the resources the ship holds (a mineral-only reply when gas is 0), the one-item
    # shape tests/test_e2e_live_fixes.py also feeds the client. False: both items, always.
    collect_omits_empty_items: bool = False
    # CollectAllResources carries the account's User record (Credits), which overwrites the balance production read
    # at login. False: no User element, so a later starbux total can only come from the login reply (or a purchase).
    collect_reply_has_user: bool = True
    design_versions: dict[str, str] = field(default_factory=_default_design_versions)

    def character(self, cid: str) -> Character:
        return _find(self.characters, cid, "character")

    def room(self, rid: str) -> Room:
        return _find(self.rooms, rid, "room")

    def room_design(self, did: str) -> RoomDesign:
        return _find(self.room_designs, did, "room design")

    def research_design(self, did: str) -> ResearchDesign:
        return _find(self.research_designs, did, "research design")

    def training_design(self, tid: str) -> TrainingDesign:
        return _find(self.training_designs, tid, "training design")

    def item(self, iid: str) -> Item:
        return _find(self.items, iid, "item")

    def marker(self, mid: str) -> Marker:
        return _find(self.markers, mid, "marker")

    def message(self, mid: str) -> Message:
        return _find(self.messages, mid, "message")

    def task(self, design_id: str) -> Task:
        for task in self.tasks:
            if task.design_id == design_id:
                return task
        raise KeyError(f"no task {design_id!r}")

    def task_design(self, design_id: str) -> TaskDesign:
        return _find(self.task_designs, design_id, "task design")

    # --- consistency ---------------------------------------------------------------------------------------------

    def validate(self) -> None:
        """Raise ValueError naming every problem: an id that points at nothing (RoomId "0", unassigned crew, is valid),
        a duplicate id in any entity or design collection, or an upgrade target (upgrade_from not "0") whose
        PriceString ``parse_price`` rejects."""
        ids = {kind: {row.id for row in rows} for kind, rows in (
            ("room", self.rooms), ("room design", self.room_designs), ("character design", self.character_designs),
            ("training design", self.training_designs), ("research design", self.research_designs),
            ("task design", self.task_designs))}
        problems = []

        def check(owner: str, kind: str, key: str | None, label: str | None = None) -> None:
            if key is not None and key not in ids[kind]:
                problems.append(f"{owner}: {label or kind} {key} does not exist")

        for crew in self.characters:
            check(f"character {crew.id}", "character design", crew.design_id, "design")
            check(f"character {crew.id}", "room", None if crew.room_id == "0" else crew.room_id)
            check(f"character {crew.id}", "training design", crew.session.design_id if crew.session else None)
        for room in self.rooms:
            check(f"room {room.id}", "room design", room.design_id, "design")
            check(f"room {room.id}", "room design", room.pending_design_id, "pending design")
        for design in self.room_designs:
            check(f"room design {design.id}", "room design",
                  None if design.upgrade_from == "0" else design.upgrade_from, "upgrade_from")
            if design.upgrade_from != "0":  # bought through UpgradeRoom2, so it needs a price
                try:
                    parse_price(design.price)
                except ValueError as bad:
                    problems.append(f"room design {design.id}: {bad}")
        for research in self.researches:
            check(f"research {research.design_id}", "research design", research.design_id, "design")
        for task in self.tasks:
            check(f"task {task.design_id}", "task design", task.design_id, "design")
        for kind, rows in (("character", self.characters), ("room", self.rooms), ("item", self.items),
                           ("marker", self.markers), ("message", self.messages), ("room design", self.room_designs),
                           ("research design", self.research_designs), ("character design", self.character_designs),
                           ("training design", self.training_designs), ("task design", self.task_designs),
                           ("market listing", self.market_listings)):
            seen = [row.id for row in rows]
            problems += [f"duplicate {kind} id {key}" for key in sorted({k for k in seen if seen.count(k) > 1})]
        if problems:
            raise ValueError("World is inconsistent:\n  " + "\n  ".join(problems))

    # --- builders (Wave 2 rosters) -------------------------------------------------------------------------------

    def _next_id(self, rows, start: int) -> str:
        return str(max([start - 1, *(int(row.id) for row in rows if row.id.isdigit())]) + 1)

    def room_named(self, name: str) -> Room:
        """The first room whose design is called ``name`` (e.g. "Galaxy Gym")."""
        for room in self.rooms:
            if self.room_design(room.design_id).name == name:
                return room
        raise KeyError(f"no room named {name!r}")

    def training_design_named(self, name: str) -> TrainingDesign:
        for design in self.training_designs:
            if design.name == name:
                return design
        raise KeyError(f"no training design named {name!r}")

    def add_room(self, name: str, *, room_type: str | None = None, upgrade_from: str | None = None,
                 price: str | None = None) -> Room:
        """Add a room of the design called ``name``, creating that design when the world has none.

        A new design defaults to a "Training" room with upgrade_from "0" and no price. For an existing design the
        arguments may only repeat what it already has: ValueError for a contradiction, which would otherwise be
        silently ignored.
        """
        design = next((d for d in self.room_designs if d.name == name), None)
        if design is None:
            design = RoomDesign(self._next_id(self.room_designs, 900), name, room_type or "Training",
                                upgrade_from or "0", price or "")
            self.room_designs.append(design)
        else:
            conflicts = [f"{label}={wanted!r} (design {design.id} has {have!r})" for label, wanted, have in (
                ("room_type", room_type, design.room_type), ("upgrade_from", upgrade_from, design.upgrade_from),
                ("price", price, design.price)) if wanted is not None and wanted != have]
            if conflicts:
                raise ValueError(f"add_room({name!r}): " + ", ".join(conflicts))
        room = Room(self._next_id(self.rooms, 101), design.id, row=len(self.rooms))
        self.rooms.append(room)
        return room

    def add_crew(self, name: str, *, room: str | None = "Galaxy Gym", stats: dict[str, int] | None = None,
                 capacity: int = 100, rarity: str = "Common", level: int = 40, xp: int = 0, fatigue: int = 0,
                 training: tuple[str, datetime.timedelta] | None = None) -> Character:
        """Add one crew member with its own CharacterDesign and return it.

        ``room`` is a room design name resolved with ``room_named`` (None: unassigned, RoomId "0"). ``stats`` is a
        ``stats(...)`` dict (all zero by default). ``training=("Bench Press", -2 * H)`` gives the crew an unapplied
        session of that design that ends at FAKE_EPOCH + offset (negative: already ended).
        """
        design = CharacterDesign(self._next_id(self.character_designs, 501), f"{name} Design",
                                 training_capacity=capacity, rarity=rarity)
        crew = Character(self._next_id(self.characters, 1001), name, design.id,
                         self.room_named(room).id if room is not None else "0", level=level, xp=xp,
                         fatigue=fatigue, stats=dict(stats) if stats is not None else _zero_stats())
        if training is not None:
            course, end_offset = training
            course_design = self.training_design_named(course)
            end = FAKE_EPOCH + end_offset
            crew.session = TrainingSession(course_design.id, end - course_design.duration, end)
        self.character_designs.append(design)
        self.characters.append(crew)
        return crew


# --- default world ------------------------------------------------------------------------------------------

# (id, name, type, upgrade_from, price). Base rows first, then affordable upgrades, then unaffordable ones: the order
# keeps upgradeRooms' per-row cost check (defect R1) from requesting an unaffordable upgrade here. R1 is not pinned
# yet (see KNOWN_DEFECTS).
DEFAULT_ROOM_DESIGNS = (
    ("1", "Bridge", "Bridge", "0", "mineral:1000"),
    ("30", "GYM", "Training", "0", "mineral:5000"),
    ("40", "Academy", "Training", "0", "gas:5000"),
    ("50", "Mineral Storage", "Storage", "0", "mineral:1000"),
    ("60", "Gas Storage", "Storage", "0", "gas:1000"),
    ("51", "Mineral Storage Lv2", "Storage", "50", "mineral:20000"),
    ("61", "Gas Storage Lv2", "Storage", "60", "gas:2000"),
    ("2", "Bridge Lv2", "Bridge", "1", "mineral:100000"),
    ("31", "Galaxy Gym", "Training", "30", "mineral:900000"),
    ("41", "Lunar College", "Training", "40", "gas:900000"),
)
# (id, design_id, status, row, column)
DEFAULT_ROOMS = (
    ("101", "1", "Normal", 0, 0),
    ("102", "30", "Normal", 1, 0),
    ("103", "31", "Normal", 1, 2),
    ("106", "40", "Normal", 2, 0),
    ("104", "50", "Normal", 2, 2),
    ("105", "60", "Upgrading", 3, 0),
    ("107", "41", "Normal", 3, 2),  # Lunar College: the engines secondary room (1016)
)
# (id, root_id, name, gas_cost). Two roots are affordable (10 and 20), and 20 comes after 10: upgradeResearches must
# stop after its first successful AddResearch, so a second start would be visible (and rejected by the fake).
DEFAULT_RESEARCH_DESIGNS = (
    ("1", "1", "Fitness Lab Lv1", 1000),
    ("2", "1", "Fitness Lab Lv2", 50000),
    ("3", "1", "Fitness Lab Lv3", 90000),
    ("10", "10", "Education Lv1", 2000),
    ("11", "10", "Education Lv2", 8000),
    ("20", "20", "Combat Lv1", 3000),
)
# Training tiers T1-T3 (the tier bands of sdk/training/strategy.py): (gain, fatigue, duration).
TIERS = {
    "T1": (2, 10, datetime.timedelta(hours=1)),
    "T2": (4, 20, datetime.timedelta(hours=3)),
    "T3": (8, 40, datetime.timedelta(hours=12)),
}
# (id, name, stat, tier); every name is unique, so the planner's exact-name lookup (pick_design) is unambiguous.
DEFAULT_TRAINING_DESIGNS = (
    ("1", "Steam Yoga", "StaminaImprovement", "T1"),
    ("3", "Bench Press", "HpImprovement", "T1"),
    ("4", "Muscle Beach", "HpImprovement", "T2"),
    ("5", "Olympic Weightlifting", "HpImprovement", "T3"),
    ("11", "Read Expert Weapon Theory", "WeaponImprovement", "T1"),
    ("12", "Weapons Summit", "WeaponImprovement", "T2"),
    ("13", "Weapons PHD", "WeaponImprovement", "T3"),
    ("21", "Big Book of Science", "ScienceImprovement", "T1"),
    ("22", "Scientific Summit", "ScienceImprovement", "T2"),
    ("23", "Science PHD", "ScienceImprovement", "T3"),
    ("31", "Study Expert Engineering Manual", "EngineImprovement", "T1"),
    ("32", "Engineering Summit", "EngineImprovement", "T2"),
    ("33", "Engineering PHD", "EngineImprovement", "T3"),
    ("41", "Kickbox", "AttackImprovement", "T1"),
    ("42", "BBJ", "AttackImprovement", "T2"),
    ("43", "Shaolin Tradition", "AttackImprovement", "T3"),
    ("44", "Crew vs Wild", "AttackImprovement", "T2"),
    ("45", "Space Marine", "AttackImprovement", "T3"),
)


@dataclass(frozen=True)
class RosterRow:
    id: str
    name: str
    design_id: str
    rarity: str
    room_id: str
    stats: dict
    expected_percent: int  # math.ceil(sum / capacity * 100), checked by a self-test
    capacity: int = 100  # the CharacterDesign's TrainingCapacity
    level: int = 40
    xp: int = 0
    available_offset: datetime.timedelta | None = None  # AvailableDate = FAKE_EPOCH + offset
    session_end_offset: datetime.timedelta | None = None  # unapplied Bench Press session ending FAKE_EPOCH + offset
    note: str = ""


H = datetime.timedelta(hours=1)
# Roster, in this order: unassigned crew #2 follows a trained crew #1, which is how defect D6 (fixed) inherited a room.
#
# The capacities differ per design (80/120/150/200/100) and several stat totals are not a multiple of the capacity, so
# the percent manageTraining computes is fractional: picking the wrong CharacterDesign, or rounding down instead of up,
# changes a decision. These records sit on an edge on purpose:
# - 1001: 57 / 80 = 71.25 %, which production rounds UP to 72, just inside the S-T1 window (71 < p < 74); rounded down
#   it would be 71, outside. After its Bench Press (+2 Hp) it is 59 / 80 = 73.75 -> 74: S-T2, research-gated, yet
#   the ended session is still finished (D14, fixed).
# - 1013: 89 xp at level 1, one short of production's 90-xp cost; 1009 (120 xp) is on the other side.
# - 1014: AvailableDate 2 h ago. A UTC host upgrades it; a host behind UTC compares the UTC date with its local clock and
#   waits (TZ1). 1012 (AvailableDate in 3 h) is upgraded early by a host ahead of UTC (TZ1). See NonUtcHostTests.
# - 1015: 106 / 150 = 70.67 -> exactly 71 %, in its secondary room: the S-T1 window's lower edge excludes it.
# - 1016: 86 / 120 = 71.67 -> 72 % in Lunar College, the only room of that design: the training-room allowlist and
#   the engines secondary rooms must both still name it.
# - 1017: unassigned (RoomId "0") at level 1 with 500 xp and a past AvailableDate: only the RoomId check keeps
#   upgradeCharacters from upgrading it (compare the string "0", not the int).
DEFAULT_ROSTER = (
    RosterRow("1001", "Galactic Succubus", "501", "Legendary", "103", stats(Hp=16, Weapon=32, Attack=9), 72,
              capacity=80, session_end_offset=-2 * H,
              note="weapons, secondary room, 71.25 %: the ended Bench Press is finished; S-T2 is research-gated"),
    RosterRow("1002", "Galactic Snow Maiden", "502", "Epic", "0", stats(Hp=20, Weapon=55, Attack=12), 73,
              capacity=120, note="weapons, unassigned, 72.5 %: no room, no request (D6, fixed)"),
    RosterRow("1003", "Delish", "503", "Common", "102", stats(Hp=20, Weapon=77, Attack=12), 73, capacity=150,
              session_end_offset=datetime.timedelta(minutes=-30), note="weapons, 72.67 %, ended 30 min ago: finished now (D2, fixed)"),
    RosterRow("1004", "Mistycball", "504", "Common", "106", stats(Hp=10, Science=20), 30,
              note="shields, primary room: P-T1 research-gated (no Fitness 1)"),
    RosterRow("1005", "Galactic Sprite", "505", "Common", "102", stats(Hp=30, Engine=30), 60,
              note="engines, primary room: P-T2 research-gated"),
    RosterRow("1006", "The Conjoint Archon", "506", "Epic", "103", stats(Hp=34, Engine=101), 68, capacity=200,
              note="engines, primary room, 67.5 %: Epic TP cap 80 reached"),
    RosterRow("1007", "Cyber Duck", "507", "Common", "106", stats(Hp=40, Stamina=40), 80,
              note="rushers, wrong room: not_in_training_room skip ('move to gym'), no request"),
    RosterRow("1008", "Admiral Serena", "508", "Common", "103", stats(Hp=40, Attack=40), 80,
              note="defenders, secondary room: S-T2 research-gated"),
    RosterRow("1009", "Engineer Grunt", "509", "Common", "104", stats(Hp=10, Repair=10), 20, level=1, xp=120,
              note="not a training room; upgraded to level 2"),
    RosterRow("1010", "Rookie Pilot", "510", "Common", "102", stats(Hp=10), 10, level=5, xp=3000,
              available_offset=-24 * H, note="no role: unknown_role skip (consumables need item designs: D12 fixed); upgraded to level 6"),
    RosterRow("1011", "Huntress", "511", "Legendary", "101", stats(Hp=20, Attack=20), 40, level=1, xp=120,
              note="Legendary needs 270 xp: not upgraded"),
    RosterRow("1012", "Turkey Hero", "512", "Common", "101", stats(Hp=20, Attack=20), 40, level=1, xp=500,
              available_offset=3 * H, note="AvailableDate in 3 h: collect only (a UTC+9 host upgrades it: TZ1)"),
    RosterRow("1013", "Cadet Ensign", "513", "Common", "104", stats(Hp=10), 10, level=1, xp=89,
              note="89 xp, level 1 costs 90: not upgraded"),
    RosterRow("1014", "Night Shift Medic", "514", "Common", "104", stats(Hp=12), 12, level=3, xp=900,
              available_offset=-2 * H, note="AvailableDate 2 h ago: upgraded to level 4 (a UTC-5 host waits: TZ1)"),
    RosterRow("1015", "Ancestral Spirit", "515", "Unique", "103", stats(Hp=30, Attack=76), 71, capacity=150,
              note="defenders, secondary room, exactly 71 %: Unique TP cap 70 reached, no request"),
    # A second copy of a character, as real rosters have: the log names both "Galactic Sprite", the room tells them
    # apart. 86 / 120 = 71.67 rounds UP to 72 (rounded down, 71 is outside the S-T1 window).
    RosterRow("1016", "Galactic Sprite", "516", "Common", "107", stats(Hp=20, Engine=66), 72, capacity=120,
              session_end_offset=-2 * H,
              note="engines, secondary room Lunar College, 71.67 %: the ended Bench Press is finished; S-T2 gated"),
    RosterRow("1017", "Deck Hand", "517", "Common", "0", stats(Hp=10), 10, level=1, xp=500,
              available_offset=-24 * H,
              note="unassigned, level 1 with 500 xp and available: upgradeCharacters skips RoomId \"0\" crew"),
)


def default_world() -> World:
    """The realistic account the e2e goldens are built on: a record on each side of most daily-path conditions.

    The conditions it covers only on one side (free starbux at the cap, drones in the ship's own star system, a
    heartbeat 5 minutes old) are covered by variant worlds in tests/test_daily_run_offline_e2e.py.
    """
    bench_press = "3"
    characters = []
    for row in DEFAULT_ROSTER:
        session = None
        if row.session_end_offset is not None:
            end = FAKE_EPOCH + row.session_end_offset
            session = TrainingSession(bench_press, end - H, end)
        characters.append(Character(
            id=row.id, name=row.name, design_id=row.design_id, room_id=row.room_id, level=row.level, xp=row.xp,
            stats=dict(row.stats), session=session,
            available_date=FAKE_EPOCH + row.available_offset if row.available_offset is not None else None))
    return World(
        room_designs=[RoomDesign(i, n, t, u, p) for i, n, t, u, p in DEFAULT_ROOM_DESIGNS],
        rooms=[Room(i, d, s, r, c, pending_design_id="61" if s == "Upgrading" else None)
               for i, d, s, r, c in DEFAULT_ROOMS],
        research_designs=[ResearchDesign(i, r, n, g) for i, r, n, g in DEFAULT_RESEARCH_DESIGNS],
        researches=[Research("1", "Completed")],
        character_designs=[CharacterDesign(row.design_id, f"{row.name} Design", training_capacity=row.capacity,
                                           rarity=row.rarity, special_ability_type="DamageToCurrentEnemy")
                           for row in DEFAULT_ROSTER],
        characters=characters,
        training_designs=[TrainingDesign(i, n, s, *TIERS[t]) for i, n, s, t in DEFAULT_TRAINING_DESIGNS],
        items=[Item("9101", "101", 5)],
        task_designs=[TaskDesign("7001", "Collect drones", 5), TaskDesign("7002", "Win battles", 10),
                      TaskDesign("7003", "Train crew", 3), TaskDesign("7004", "Upgrade rooms", 2)],
        tasks=[Task("7001", 5), Task("7002", 3), Task("7003", 3, collected=True), Task("7004", 4)],
        markers=[Marker("9001", "42"), Marker("9002", "42", collected=True), Marker("9003", "42", "Event"),
                 Marker("9004", "42", user_marker=True)],
        messages=[Message("8001", "Daily gift", "starbux:5"), Message("8002", "Gas refund", "gas:100"),
                  Message("8003", "Welcome back", "None")],
        market_listings=[MarketListing("8501", "Scrap Metal x10", "starbux:120")],
    )


def minimal_world() -> World:
    """An account on which the daily run sends only upgradeResearches' CollectAllResources: a Wave 2 roster base.

    Nothing to collect or buy (no credits, tasks, markers or messages; daily reward taken; free starbux at the cap; a
    heartbeat sent this minute), no affordable research or room upgrade, and no crew. It has the GYM, Academy and
    Galaxy Gym training rooms (``add_crew(room=...)`` resolves those names) and the default training designs.
    """
    world = World(
        account=Account(credits=0, daily_reward_status="1", last_heartbeat=FAKE_EPOCH),
        resources=Resources(mineral=0, gas=0),
        training_designs=[TrainingDesign(i, n, s, *TIERS[t]) for i, n, s, t in DEFAULT_TRAINING_DESIGNS],
    )
    for name in ("Bridge", "GYM", "Academy", "Galaxy Gym"):
        world.add_room(name, room_type="Bridge" if name == "Bridge" else "Training")
    return world
