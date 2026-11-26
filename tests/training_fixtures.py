"""Synthetic training-v1 fixtures: planner-compatible design records, raw crew and snapshot builders.

Constants and builder functions only, never a TestCase (a module that imports one reruns it). Shared by
tests/test_training_policy.py, tests/test_training_policy_simulation.py, tests/test_fleet_execute.py and
tests/test_fleet_v1_actions.py.
"""

from datetime import datetime, timedelta, timezone

from sdk.policy import DesignData, PolicyConfig, get_policy
from sdk.state import build_snapshot

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
STATS = ("Hp", "Pilot", "Repair", "Weapon", "Science", "Engine", "Attack", "Ability", "Stamina")


def td(design_id, name, chance, stat, room, kind, low, high):
    """A TrainingDesign record that both the planner's parser and the simulator read."""
    return {"@TrainingDesignId": design_id, "@TrainingName": name, "@Rank": "1", f"@{chance}Chance": "100",
            "@RequiredRoomType": kind, "@TrainingStat": stat, "@RequiredRoomDesignId": room,
            "@Duration": "3600", "@Fatigue": "10", "@MinGain": low, "@MaxGain": high}


TRAINING_DESIGNS = [td("5010", "Bench Press", "Attack", "Attack", "300", "Gym", "3", "3"),
                    td("5030", "Read Expert Weapon Theory", "Weapon", "Weapon", "400", "Academy", "1", "3"),
                    td("5040", "Steam Yoga", "Hp", "Hp", "300", "Gym", "5", "5"),
                    td("5090", "Shaolin Tradition", "Ability", "Ability", "300", "Gym", "2", "2")]
CHARACTER_DESIGNS = [
    {"@CharacterDesignId": "7001", "@CharacterDesignName": "Recruit", "@Rarity": "Common", "@TrainingCapacity": "20"},
    {"@CharacterDesignId": "7002", "@Rarity": "Common", "@TrainingCapacity": "10"},
    {"@CharacterDesignId": "7003", "@Rarity": "Epic", "@TrainingCapacity": "100"},
]
ITEM_DESIGNS = [{"@ItemDesignId": "902", "@ItemName": "Common SCI Consumable", "@ItemSubType": "Consumable",
                 "@Rarity": "Common", "@ConsumableStat": "Science", "@ConsumableGain": "1"}]
ROOM_DESIGNS = [{"@RoomDesignId": "300", "@RoomName": "GYM"}, {"@RoomDesignId": "400", "@RoomName": "Academy"},
                {"@RoomDesignId": "500", "@RoomName": "Bridge"}]
RESEARCH_DESIGNS = [{"@ResearchDesignId": "610", "@RootResearchDesignId": "610", "@ResearchName": "Fitness 101",
                     "@GasCost": "10"}]
ROOMS = [{"@RoomId": "11", "@RoomDesignId": "300", "@RoomStatus": "Normal"},
         {"@RoomId": "12", "@RoomDesignId": "400", "@RoomStatus": "Normal"},
         {"@RoomId": "13", "@RoomDesignId": "500", "@RoomStatus": "Normal"}]
RESEARCH = [{"@ResearchDesignId": "610", "@ResearchState": "Completed"}]
DESIGN_RECORDS = {"room_designs": ROOM_DESIGNS, "research_designs": RESEARCH_DESIGNS,
                  "training_designs": TRAINING_DESIGNS, "character_designs": CHARACTER_DESIGNS,
                  "item_designs": ITEM_DESIGNS}
ZERO = {"minerals": 0, "gas": 0}


def design(**overrides):
    return DesignData.from_records(**{**DESIGN_RECORDS, **overrides})


def crew(cid="101", name="Huntress", design_id="7001", room="11", fatigue="0", training="0", end="", **stats):
    """A raw character record with all nine @*Improvement fields (default "0")."""
    record = {"@CharacterId": cid, "@CharacterName": name, "@CharacterDesignId": design_id, "@Level": "1",
              "@RoomId": room, "@Fatigue": fatigue, "@TrainingDesignId": training, "@TrainingEndDate": end}
    record.update({f"@{stat}Improvement": str(stats.get(stat, 0)) for stat in STATS})
    return record


def snap(crew_records=(crew(),), inventory=(), **overrides):
    """A snapshot at T0; ``crew_records`` or ``inventory`` None leaves that section unobserved."""
    kwargs = dict(resources={"minerals": "1000", "gas": "2000", "starbux": "5"}, ship={"@ShipLevel": "3"},
                  rooms=ROOMS, research=RESEARCH, crew=None if crew_records is None else list(crew_records),
                  inventory=None if inventory is None else list(inventory))
    kwargs.update(overrides)
    return build_snapshot("alpha", kwargs.pop("observed_at", T0), "synthetic-fixture", **kwargs)


def server_time(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def plan(snapshot=None, design_data=None, policy="training-v1", **config):
    return get_policy(policy).plan(snapshot or snap(), design_data or design(), PolicyConfig(**config))


def ids(result):
    return [(request.action_id, dict(request.params)) for request in result.requests]


TRAINING_ACTIONS = ("add_training", "finish_training", "use_consumable")


def training_requests(result):
    return [request for request in result.requests if request.action_id in TRAINING_ACTIONS]


def training_ids(result):
    return [(request.action_id, dict(request.params)) for request in training_requests(result)]


def reasons(result):
    return {rejection.candidate: rejection.reason for rejection in result.rejected}


def start(cid="101", design_id="5010"):
    return ("add_training", {"training_design_id": design_id, "character_id": cid})


def ended(cid="101", name="Huntress", room="11", ago=timedelta(hours=2), design_id="5010", **kwargs):
    """A crew record whose training ``design_id`` ended ``ago`` before T0 (negative: still running)."""
    return crew(cid, name, room=room, training=design_id, end=server_time(T0 - ago), **kwargs)


def item(quantity="2", item_id="9001", design_id="902"):
    return {"@ItemId": item_id, "@ItemDesignId": design_id, "@Quantity": quantity}


CPU = crew("104", "C.P.U.", room="12", Science=1)
USE_CPU = ("use_consumable", {"item_design_id": "902", "character_id": "104"})
MIXED = [crew("102", "Delish", room="12"), ended(), crew("103", "Cyber Duck", design_id="7002")]
