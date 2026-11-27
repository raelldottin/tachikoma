"""Prestige routes toward the original named targets (SPEC-14, S14-R3): pure, no I/O and no clock.

- Targets are character designs whose exact name is one of the policy's original named targets
  (``CrewPolicy.prestige_targets``), resolved to design ids and ordered by (name, id).
- Recipes (``model.PrestigeRecipe``, from ``CharacterService/PrestigeCharacterTo``) are matched by crew design id.
  A recipe that needs the same design twice needs two crew of that design: duplicates are counted, never folded.
- A crew member is an eligible ingredient only when its training is observed idle or finished. One that is training
  or holds an unclaimed course is not; one whose training is unknown is neither (``unknown`` counts the missing copies
  such crew could fill, and crew of unknown design that are not known to be busy, each filling at most one copy).
  With no observed roster the missing and unknown counts are None (unknown), never 0.
- The routes are read-only previews: nothing here (or anywhere in training-v2) spends a Prestige ingredient.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from sdk.training.model import (
    SESSION_ENDED_UNCLAIMED,
    SESSION_FINISHED,
    SESSION_NONE,
    SESSION_RUNNING,
    PrestigeRecipe,
    TrainingSession,
)

__all__ = (
    "PrestigeCandidate",
    "PrestigeRoute",
    "ingredient_eligible",
    "match_recipe",
    "prestige_routes",
    "prestige_targets",
)


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class PrestigeCandidate:
    """A crew member as a possible ingredient: its positive id, its design id (positive; None = not observed) and
    whether it is eligible now (``ingredient_eligible``: True, False, or None when unknown). Anything else raises
    ValueError."""

    character_id: int
    design_id: int | None
    eligible: bool | None

    def __post_init__(self) -> None:
        if not _positive(self.character_id):
            raise ValueError(f"character_id must be a positive int: {self.character_id!r}")
        if self.design_id is not None and not _positive(self.design_id):
            raise ValueError(f"design_id must be a positive int or None: {self.design_id!r}")
        if self.eligible is not None and not isinstance(self.eligible, bool):
            raise ValueError(f"eligible must be True, False or None: {self.eligible!r}")


@dataclass(frozen=True)
class PrestigeRoute:
    """One recipe toward a target: the two ingredient design ids (ascending, a design twice for a two-copy recipe),
    the copies the roster lacks (0 to 2) and how many of those crew of unknown eligibility or design could fill (0 to
    ``missing``); both None when the roster was not observed. Anything else raises ValueError."""

    target_design_id: int
    ingredient_design_ids: tuple[int, int]
    missing: int | None
    unknown: int | None

    def __post_init__(self) -> None:
        if not _positive(self.target_design_id):
            raise ValueError(f"target_design_id must be a positive int: {self.target_design_id!r}")
        ids = self.ingredient_design_ids
        if not (isinstance(ids, tuple) and len(ids) == 2 and all(_positive(entry) for entry in ids)):
            raise ValueError(f"ingredient_design_ids must be a tuple of two positive ints: {ids!r}")
        if self.missing is not None and not (_count(self.missing) and self.missing <= 2):
            raise ValueError(f"missing must be 0, 1, 2 or None: {self.missing!r}")
        if (self.missing is None) != (self.unknown is None):
            raise ValueError(f"unknown is None exactly when missing is: {self.missing!r}, {self.unknown!r}")
        if self.unknown is not None and not (_count(self.unknown) and self.unknown <= self.missing):
            raise ValueError(f"unknown must be between 0 and missing ({self.missing}): {self.unknown!r}")


def ingredient_eligible(session: TrainingSession | None) -> bool | None:
    """True for an idle or finished session, False while training or holding an unclaimed course, else None."""
    if session is None:
        return None
    if session.phase in (SESSION_NONE, SESSION_FINISHED):
        return True
    if session.phase in (SESSION_RUNNING, SESSION_ENDED_UNCLAIMED):
        return False
    return None


def prestige_targets(design_names: Mapping[int, str], target_names: Collection[str]) -> tuple[int, ...]:
    """The design ids whose exact name is in ``target_names``, ordered by (name, id)."""
    wanted = frozenset(target_names)
    named = sorted((name, design_id) for design_id, name in design_names.items() if name in wanted)
    return tuple(design_id for _, design_id in named)


def match_recipe(recipe: PrestigeRecipe, roster: Sequence[PrestigeCandidate] | None) -> PrestigeRoute:
    """How many ingredient copies ``roster`` lacks for ``recipe`` (duplicates counted; see the module docstring)."""
    ingredients = tuple(sorted((recipe.first_design_id, recipe.second_design_id)))
    if roster is None:
        return PrestigeRoute(recipe.to_design_id, ingredients, None, None)
    missing = unknown = 0
    for design_id in sorted(set(ingredients)):
        need = ingredients.count(design_id)
        ready = sum(1 for entry in roster if entry.design_id == design_id and entry.eligible is True)
        maybe = sum(1 for entry in roster if entry.design_id == design_id and entry.eligible is None)
        short = max(0, need - ready)
        missing += short
        unknown += min(short, maybe)
    # A crew of unknown design that is not known to be busy could be any ingredient: it may fill one more copy each.
    undesigned = sum(1 for entry in roster if entry.design_id is None and entry.eligible is not False)
    unknown += min(missing - unknown, undesigned)
    return PrestigeRoute(recipe.to_design_id, ingredients, missing, unknown)


def prestige_routes(
    targets: Sequence[int],
    recipes: Mapping[int, Sequence[PrestigeRecipe]],
    roster: Sequence[PrestigeCandidate] | None,
) -> tuple[PrestigeRoute, ...]:
    """Each target's routes, in target order: only recipes into that target, one per ingredient pair (the first)."""
    routes: list[PrestigeRoute] = []
    for target in targets:
        seen: set[tuple[int, int]] = set()
        for recipe in recipes.get(target, ()):
            if recipe.to_design_id != target:
                continue
            route = match_recipe(recipe, roster)
            if route.ingredient_design_ids in seen:
                continue
            seen.add(route.ingredient_design_ids)
            routes.append(route)
    return tuple(routes)
