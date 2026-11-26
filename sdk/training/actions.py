"""Planner decisions as catalog action requests: ``(action_id, params)``, or None for a Skip.

Shared by the legacy executor (``sdk/execution/training.py``) and the fleet policy (``sdk/policy``), so both paths
send a decision through the same catalog spec, preconditions and postconditions. Pure: no I/O, no clock.

Params are decimal strings: the form the client sends and the catalog's numeric-id preconditions accept.
"""

from __future__ import annotations

from typing import Mapping, Optional

from sdk.training.model import Decision, FinishTraining, Skip, StartTraining, UseConsumable

TRAINING_ACTION_IDS = ("finish_training", "add_training", "use_consumable")


def action_for(decision: Decision) -> Optional[tuple[str, Mapping[str, str]]]:
    """The catalog action and params that carry out ``decision``; None for a Skip (nothing to send).

    Raises TypeError for anything that is not a training decision, so a new decision type cannot be dropped silently.
    """
    if isinstance(decision, FinishTraining):
        return "finish_training", {"character_id": str(decision.character_id)}
    if isinstance(decision, StartTraining):
        return "add_training", {
            "training_design_id": str(decision.training_design_id),
            "character_id": str(decision.character_id),
        }
    if isinstance(decision, UseConsumable):
        return "use_consumable", {
            "item_design_id": str(decision.item_design_id),
            "character_id": str(decision.character_id),
        }
    if isinstance(decision, Skip):
        return None
    raise TypeError(f"not a training decision: {decision!r}")
