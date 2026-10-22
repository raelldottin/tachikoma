from __future__ import annotations

"""Task H: versioned AccountSnapshot persistence (READ-ONLY).

A snapshot is the deterministic, non-secret, serializable projection of ONE
account's Tasks D/F/G state: account identity, per-stack inventory,
protection/classification, demand/reservation, reduction-candidate,
evidence and unresolved-condition state.

SAFETY CONTRACT (Task H brief):

- NO credentials, tokens, secrets or authentication material are ever
  stored. The schema has no field for them; the serializer actively
  rejects payload keys that look like secrets (fail closed).
- The snapshot is inert data. Loading it authorizes nothing. It contains
  no transfer, marketplace, sale or mutation semantics.
- Unknown snapshot versions are rejected. Malformed, corrupt or
  contradictory payloads fail closed with SnapshotError - never silently
  coerced, repaired or partially trusted.
- Pure: no network I/O, no filesystem access, no clock reads, no
  randomness. to_json/from_json operate on text the caller owns.
  (sdk/inventory as a whole stays network-free; the loader's caller in
  the CLI owns any disk access.)

DETERMINISM: build_snapshot produces byte-identical JSON for identical
inputs. Every list is sorted (stacks ascending ItemId; design rows
ascending ItemDesignId; conditions sorted by (code, detail, design_id);
accounts irrelevant here - one snapshot, one account). Field order is
fixed by construction.

EVIDENCE CAP (ListItemsOfAShip): feed-derived rows never rise above
CAPTURED_NOT_VERIFIED while the endpoint has zero captures. The builder
takes evidence tiers from the Task D/F/G outputs and never promotes
them; the serialized tier strings round-trip to the same Evidence
members.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ._types import Classification, ClassifiedItem, Evidence, ItemDesign, Role
from .demand import (
    CRAFT_OBJECTIVE_NOT_CRAFTABLE,
    DEMAND_EXCEEDS_SUPPLY,
    DUPLICATE_CONFIG_ENTRY,
    DUPLICATE_INGREDIENT,
    EQUIPMENT_SHARED_STACK,
    MALFORMED_QUANTITY,
    MODULE_MULTI_ATTACH,
    RECIPE_CYCLE,
    SOURCE_UNAVAILABLE,
    SOURCE_UNSUPPORTED,
    UNKNOWN_DESIGN,
    UNKNOWN_ITEM_REFERENCE,
    UNREASONABLE_QUANTITY,
    BOTH_EQUIPPED_AND_ROOM_ATTACHED,
    ReservationLedger,
)
from .policy import (
    PROTECTED_EQUIPPED,
    PROTECTED_EVENT_LIMITED,
    PROTECTED_INSUFFICIENT_EVIDENCE,
    PROTECTED_PREMIUM,
    PROTECTED_QUEST_PROGRESSION,
    PROTECTED_RARITY,
    PROTECTED_ROOM_MODULE,
    PROTECTED_UNKNOWN_DESIGN,
)
from .reduction import (
    BLOCKED_ALLOCATION_EXHAUSTED,
    BLOCKED_DESIGN_CONDITION,
    BLOCKED_MALFORMED_ROW,
    BLOCKED_OVER_RESERVED,
    BLOCKED_PROTECTED_OR_UNKNOWN,
    BLOCKED_STACK_CONDITION,
    BLOCKED_STRATEGIC_ROLE,
    BLOCKED_UNSUPPORTED_DEMAND,
    BLOCKED_ZERO_QUANTITY,
    BLOCKED_ZERO_UNRESERVED,
    STATE_CANDIDATE,
    STATE_HOLD,
    ReductionPlan,
)

# Schema version. v1 is the only version this slice reads or writes;
# anything else is rejected fail-closed (forward AND backward).
SNAPSHOT_VERSION = 1

# Keys that must never appear in a snapshot payload. Persisting any of
# these would leak authentication material into an on-disk artifact.
FORBIDDEN_SNAPSHOT_KEYS = (
    "accessToken",
    "accesstoken",
    "refreshToken",
    "refreshtoken",
    "password",
    "passwd",
    "secret",
    "token",
    "apiKey",
    "api_key",
    "clientKey",
    "client_key",
    "deviceKey",
    "device_key",
    "credentials",
)

# Evidence tier names accepted on load (exact Evidence members).
_VALID_EVIDENCE_VALUES = frozenset(tier.value for tier in Evidence)

# Evidence tiers a FEED-derived surface may never carry while the
# ListItemsOfAShip endpoint has zero captures (the Task D/F/G cap:
# reduction.py/classify.py/demand.py all pin feed rows at
# CAPTURED_NOT_VERIFIED; nothing this slice produces may reach VERIFIED).
_EVIDENCE_CAP = frozenset({Evidence.VERIFIED.value})

# Vocabulary gates (pinned §3b/§7a: unknown enum values are malformed).
# Each set is exactly the vocabulary the Task D/F/G producer can emit.
_CLASSIFICATION_VALUES = frozenset(
    {
        Classification.PROTECTED.value,
        Classification.RESERVED.value,
        Classification.CONSUMABLE.value,
        Classification.UNKNOWN.value,
    }
)
_STATE_VALUES = frozenset({STATE_HOLD, STATE_CANDIDATE})
_DISPOSITION_VALUES = frozenset({"KEEP", "USE", "CRAFT", "ALLOCATE"})
_ROLE_VALUES = frozenset({role.value for role in Role})
# Roles whose presence in a persisted snapshot would smuggle Task G
# surplus semantics this slice must never produce (_types.py pins them
# as ROLES_FORBIDDEN_THIS_SLICE; the loader mirrors the ban).
_FORBIDDEN_ROLE_VALUES = frozenset(
    {
        Role.TRANSFERABLE_CANDIDATE.value,
        Role.MARKETPLACE_SALE_CANDIDATE.value,
        Role.DISCARD_CANDIDATE.value,
    }
)
_PROTECTION_REASON_VALUES = frozenset(
    {
        PROTECTED_EQUIPPED,
        PROTECTED_ROOM_MODULE,
        PROTECTED_RARITY,
        PROTECTED_EVENT_LIMITED,
        PROTECTED_QUEST_PROGRESSION,
        PROTECTED_PREMIUM,
        PROTECTED_UNKNOWN_DESIGN,
        PROTECTED_INSUFFICIENT_EVIDENCE,
    }
)
_CANDIDATE_REASON_VALUES = frozenset(
    {
        BLOCKED_ALLOCATION_EXHAUSTED,
        BLOCKED_DESIGN_CONDITION,
        BLOCKED_MALFORMED_ROW,
        BLOCKED_OVER_RESERVED,
        BLOCKED_PROTECTED_OR_UNKNOWN,
        BLOCKED_STACK_CONDITION,
        BLOCKED_STRATEGIC_ROLE,
        BLOCKED_UNSUPPORTED_DEMAND,
        BLOCKED_ZERO_QUANTITY,
        BLOCKED_ZERO_UNRESERVED,
    }
)
_CONDITION_CODE_VALUES = frozenset(
    {
        BOTH_EQUIPPED_AND_ROOM_ATTACHED,
        CRAFT_OBJECTIVE_NOT_CRAFTABLE,
        DEMAND_EXCEEDS_SUPPLY,
        DUPLICATE_CONFIG_ENTRY,
        DUPLICATE_INGREDIENT,
        EQUIPMENT_SHARED_STACK,
        MALFORMED_QUANTITY,
        MODULE_MULTI_ATTACH,
        RECIPE_CYCLE,
        SOURCE_UNAVAILABLE,
        SOURCE_UNSUPPORTED,
        UNKNOWN_DESIGN,
        UNKNOWN_ITEM_REFERENCE,
        UNREASONABLE_QUANTITY,
    }
)

# Pinned v1 captured_at convention (§4): ISO-8601, UTC, Z suffix,
# seconds precision. Unparseable or off-convention = malformed at
# DESERIALIZATION time (future-ness stays a render-time check where
# `now` is injected; parseability is structural and checked here).
_CAPTURED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class SnapshotError(ValueError):
    """Malformed, corrupt, contradictory or unsupported-version snapshot.

    Fail closed: raised instead of any silent repair, partial trust or
    version guessing.
    """


@dataclass(frozen=True)
class SnapshotAccountIdentity:
    """Non-secret account identity (Task H: only these fields exist).

    user_id is the PSS UserId; ship_id is the ship the inventory was read
    from; display_name is informational. None of these are credentials;
    none of them can authenticate anything.
    """

    user_id: int
    ship_id: int = 0
    display_name: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "user_id": int(self.user_id),
            "ship_id": int(self.ship_id),
            "display_name": str(self.display_name),
        }


@dataclass(frozen=True)
class SnapshotStackRow:
    """One inventory stack: Task D identity/protection + Task F demand +
    Task G candidate state for a single ItemId.

    All numbers are non-negative observed facts or bounded arithmetic
    already computed upstream; this row stores, never computes policy.
    """

    item_id: int
    item_design_id: int
    ship_id: int
    quantity: int
    classification: str
    protected: bool
    unknown: bool
    roles: Tuple[str, ...]
    protection_reasons: Tuple[str, ...]
    classification_reasons: Tuple[str, ...]
    evidence: str
    # Task F: demand/reservation totals for this stack's design
    requested_quantity: int
    reserved_quantity: int
    # Task G: bounded dry-run candidate state for this stack
    candidate_quantity: int
    candidate_state: str
    candidate_reasons: Tuple[str, ...]
    candidate_evidence: str
    disposition_hint: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "item_id": int(self.item_id),
            "item_design_id": int(self.item_design_id),
            "ship_id": int(self.ship_id),
            "quantity": int(self.quantity),
            "classification": str(self.classification),
            "protected": bool(self.protected),
            "unknown": bool(self.unknown),
            "roles": list(self.roles),
            "protection_reasons": list(self.protection_reasons),
            "classification_reasons": list(self.classification_reasons),
            "evidence": str(self.evidence),
            "requested_quantity": int(self.requested_quantity),
            "reserved_quantity": int(self.reserved_quantity),
            "candidate_quantity": int(self.candidate_quantity),
            "candidate_state": str(self.candidate_state),
            "candidate_reasons": list(self.candidate_reasons),
            "candidate_evidence": str(self.candidate_evidence),
            "disposition_hint": str(self.disposition_hint),
        }


@dataclass(frozen=True)
class SnapshotDesignRow:
    """Design-level aggregate inside one account snapshot.

    observed/reserved/unmet are the Task F design totals; unmet is the
    requested-minus-reserved shortfall (demand the account has expressed
    but could not hold against observed supply) - exposed, never an
    acquisition or transfer instruction.
    """

    item_design_id: int
    item_name: str
    observed_quantity: int
    requested_quantity: int
    reserved_quantity: int
    unmet_quantity: int
    evidence: str
    stacks: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "item_design_id": int(self.item_design_id),
            "item_name": str(self.item_name),
            "observed_quantity": int(self.observed_quantity),
            "requested_quantity": int(self.requested_quantity),
            "reserved_quantity": int(self.reserved_quantity),
            "unmet_quantity": int(self.unmet_quantity),
            "evidence": str(self.evidence),
            "stacks": int(self.stacks),
        }


@dataclass(frozen=True)
class SnapshotCondition:
    """One unresolved-condition entry, carried with its scope.

    design_id is None for account-global conditions (Task F emits those
    with design_id=None); otherwise the condition is scoped to a design.
    """

    code: str
    detail: str
    design_id: Optional[int] = None

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "code": str(self.code),
            "detail": str(self.detail),
        }
        if self.design_id is not None:
            out["design_id"] = int(self.design_id)
        return out


@dataclass(frozen=True)
class AccountSnapshot:
    """Versioned, deterministic, non-secret projection of one account.

    Loading is inert: nothing in this object authorizes any action, and
    no field is a mutation instruction. ``actionable`` is False for the
    whole snapshot by construction - candidates are informational Task G
    dry-run state only.
    """

    version: int
    identity: SnapshotAccountIdentity
    stacks: Tuple[SnapshotStackRow, ...] = ()
    designs: Tuple[SnapshotDesignRow, ...] = ()
    conditions: Tuple[SnapshotCondition, ...] = ()
    limitations: Tuple[str, ...] = ()
    captured_at: str = ""
    actionable: bool = False

    @property
    def user_id(self) -> int:
        return self.identity.user_id

    def stacks_for_design(self, design_id: int) -> Tuple[SnapshotStackRow, ...]:
        key = int(design_id)
        return tuple(s for s in self.stacks if s.item_design_id == key)

    def design_row(self, design_id: int) -> Optional[SnapshotDesignRow]:
        for row in self.designs:
            if row.item_design_id == int(design_id):
                return row
        return None

    def conditions_for(self, design_id: int) -> Tuple[SnapshotCondition, ...]:
        key = int(design_id)
        return tuple(c for c in self.conditions if c.design_id == key)

    @property
    def global_conditions(self) -> Tuple[SnapshotCondition, ...]:
        return tuple(c for c in self.conditions if c.design_id is None)

    def to_dict(self) -> Dict[str, object]:
        """Deterministic ordered mapping (the serialization source)."""
        return {
            "version": int(self.version),
            "identity": self.identity.to_dict(),
            "stacks": [s.to_dict() for s in self.stacks],
            "designs": [d.to_dict() for d in self.designs],
            "conditions": [c.to_dict() for c in self.conditions],
            "limitations": [str(x) for x in self.limitations],
            "captured_at": str(self.captured_at),
            "actionable": bool(self.actionable),
        }

    def to_json(self) -> str:
        """Canonical JSON text. Keys ordered by insertion (deterministic
        construction above), separators tight, sort_keys for total
        byte-stability across Python versions."""
        import json

        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def actionable_rows(self) -> Tuple[SnapshotStackRow, ...]:
        """Always empty - proof surface for tests (Task H: all output
        actionable=false)."""
        return ()


def _require_mapping(value: Any, path: str) -> Mapping[Any, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotError(f"{path} must be an object, got {type(value).__name__}")
    return value


def _reject_unknown_keys(
    payload: Mapping[Any, Any], allowed: Tuple[str, ...], path: str
) -> None:
    """Strict schema (pinned §3b/§7a): unknown keys are malformed.

    A snapshot is a fixed contract between writer and reader; a key the
    v1 schema does not define cannot be validated and must never be
    silently carried (fail closed, no forward-compatibility guessing).
    """
    extra = sorted(str(key) for key in payload if key not in allowed)
    if extra:
        raise SnapshotError(
            f"{path} has unknown key(s) {', '.join(extra)}: the v1 snapshot "
            "schema is strict; unknown keys are malformed (fail closed)"
        )


def _require_int(value: Any, path: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotError(f"{path} must be an integer, got {type(value).__name__}")
    if minimum is not None and value < minimum:
        raise SnapshotError(f"{path} must be >= {minimum}, got {value}")
    return value


def _require_str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise SnapshotError(f"{path} must be a string, got {type(value).__name__}")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise SnapshotError(f"{path} must be a boolean, got {type(value).__name__}")
    return value


def _require_str_tuple(value: Any, path: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise SnapshotError(f"{path} must be an array of strings, got {type(value).__name__}")
    return tuple(_require_str(entry, f"{path}[{i}]") for i, entry in enumerate(value))


def _check_no_secrets(payload: Mapping[Any, Any]) -> None:
    """Reject secret-looking keys anywhere in the payload (fail closed).

    Fully recursive over dict AND list values at any depth, so a
    smuggled nested ``accessToken`` (including inside list-of-list
    nesting) cannot survive; exact names only - the goal is to catch
    the capture vocabulary (accessToken/refreshToken/...), not to
    guess schemas.
    """
    for key in payload:
        if isinstance(key, str) and key in FORBIDDEN_SNAPSHOT_KEYS:
            raise SnapshotError(
                f"snapshot payload key {key!r} is forbidden: snapshots never "
                "persist credentials, tokens or secrets"
            )
    for value in payload.values():
        if isinstance(value, Mapping):
            _check_no_secrets(value)
        elif isinstance(value, (list, tuple)):
            _check_no_secrets_in_list(value)


def _check_no_secrets_in_list(values: Sequence[Any]) -> None:
    for entry in values:
        if isinstance(entry, Mapping):
            _check_no_secrets(entry)
        elif isinstance(entry, (list, tuple)):
            _check_no_secrets_in_list(entry)


_ROOT_KEYS = (
    "version",
    "identity",
    "stacks",
    "designs",
    "conditions",
    "limitations",
    "captured_at",
    "actionable",
)
_IDENTITY_KEYS = ("user_id", "ship_id", "display_name")
_STACK_KEYS = (
    "item_id",
    "item_design_id",
    "ship_id",
    "quantity",
    "classification",
    "protected",
    "unknown",
    "roles",
    "protection_reasons",
    "classification_reasons",
    "evidence",
    "requested_quantity",
    "reserved_quantity",
    "candidate_quantity",
    "candidate_state",
    "candidate_reasons",
    "candidate_evidence",
    "disposition_hint",
)
_DESIGN_KEYS = (
    "item_design_id",
    "item_name",
    "observed_quantity",
    "requested_quantity",
    "reserved_quantity",
    "unmet_quantity",
    "evidence",
    "stacks",
)
_CONDITION_KEYS = ("code", "detail", "design_id")


def _parse_identity(payload: Mapping[Any, Any]) -> SnapshotAccountIdentity:
    _require_mapping(payload, "identity")
    _reject_unknown_keys(payload, _IDENTITY_KEYS, "identity")
    # user_id >= 1 (pinned §2/§3b: negative/zero user_id invalid - UserId
    # is a positive server identifier; 0 is not an account).
    user_id = _require_int(payload.get("user_id"), "identity.user_id", minimum=1)
    ship_id = _require_int(payload.get("ship_id"), "identity.ship_id", minimum=0)
    display_name = _require_str(payload.get("display_name"), "identity.display_name")
    return SnapshotAccountIdentity(
        user_id=user_id, ship_id=ship_id, display_name=display_name
    )


def _parse_stack(payload: Mapping[Any, Any], index: int) -> SnapshotStackRow:
    path = f"stacks[{index}]"
    row = _require_mapping(payload, path)
    _reject_unknown_keys(row, _STACK_KEYS, path)
    item_id = _require_int(row.get("item_id"), f"{path}.item_id", minimum=0)
    design_id = _require_int(
        row.get("item_design_id"), f"{path}.item_design_id", minimum=0
    )
    classification = _require_str(row.get("classification"), f"{path}.classification")
    if classification not in _CLASSIFICATION_VALUES:
        raise SnapshotError(
            f"{path}.classification {classification!r} is not one of the four "
            "buckets the Task D classifier emits (PROTECTED/RESERVED/"
            "CONSUMABLE/UNKNOWN)"
        )
    evidence = _require_str(row.get("evidence"), f"{path}.evidence")
    if evidence not in _VALID_EVIDENCE_VALUES:
        raise SnapshotError(f"{path}.evidence {evidence!r} is not a known evidence tier")
    if evidence in _EVIDENCE_CAP:
        # Feed-derived stack rows can never be VERIFIED while
        # ListItemsOfAShip has zero captures (the preserved cap).
        raise SnapshotError(
            f"{path}.evidence {evidence!r} is above the ListItemsOfAShip "
            "evidence cap: feed-derived rows stay CAPTURED_NOT_VERIFIED "
            "or below (fail closed, never promoted)"
        )
    candidate_evidence = _require_str(
        row.get("candidate_evidence"), f"{path}.candidate_evidence"
    )
    if candidate_evidence not in _VALID_EVIDENCE_VALUES:
        raise SnapshotError(
            f"{path}.candidate_evidence {candidate_evidence!r} is not a known evidence tier"
        )
    if candidate_evidence in _EVIDENCE_CAP:
        raise SnapshotError(
            f"{path}.candidate_evidence {candidate_evidence!r} is above the "
            "ListItemsOfAShip evidence cap (fail closed, never promoted)"
        )
    roles = _require_str_tuple(row.get("roles"), f"{path}.roles")
    for role in roles:
        if role not in _ROLE_VALUES:
            raise SnapshotError(f"{path}.roles entry {role!r} is not a known role")
        if role in _FORBIDDEN_ROLE_VALUES:
            raise SnapshotError(
                f"{path}.roles entry {role!r} is a forbidden this-slice role "
                "(transfer/marketplace/discard semantics are not producible "
                "by the Task D classifier; fail closed)"
            )
    protection_reasons = _require_str_tuple(
        row.get("protection_reasons"), f"{path}.protection_reasons"
    )
    for reason in protection_reasons:
        if reason not in _PROTECTION_REASON_VALUES:
            raise SnapshotError(
                f"{path}.protection_reasons entry {reason!r} is not a known "
                "protection reason code"
            )
    candidate_state = _require_str(
        row.get("candidate_state"), f"{path}.candidate_state"
    )
    if candidate_state not in _STATE_VALUES:
        raise SnapshotError(
            f"{path}.candidate_state {candidate_state!r} must be HOLD or CANDIDATE"
        )
    candidate_reasons = _require_str_tuple(
        row.get("candidate_reasons"), f"{path}.candidate_reasons"
    )
    for reason in candidate_reasons:
        if reason not in _CANDIDATE_REASON_VALUES:
            raise SnapshotError(
                f"{path}.candidate_reasons entry {reason!r} is not a known "
                "BLOCKED_* reason code"
            )
    disposition_hint = _require_str(
        row.get("disposition_hint"), f"{path}.disposition_hint"
    )
    if disposition_hint not in _DISPOSITION_VALUES:
        raise SnapshotError(
            f"{path}.disposition_hint {disposition_hint!r} is not a known "
            "disposition (KEEP/USE/CRAFT/ALLOCATE only this slice; "
            "TRANSFER/SELL/CONSUME/DELETE are deferred and cannot be "
            "persisted)"
        )
    return SnapshotStackRow(
        item_id=item_id,
        item_design_id=design_id,
        ship_id=_require_int(row.get("ship_id"), f"{path}.ship_id", minimum=0),
        quantity=_require_int(row.get("quantity"), f"{path}.quantity", minimum=0),
        classification=_require_str(row.get("classification"), f"{path}.classification"),
        protected=_require_bool(row.get("protected"), f"{path}.protected"),
        unknown=_require_bool(row.get("unknown"), f"{path}.unknown"),
        roles=_require_str_tuple(row.get("roles"), f"{path}.roles"),
        protection_reasons=_require_str_tuple(
            row.get("protection_reasons"), f"{path}.protection_reasons"
        ),
        classification_reasons=_require_str_tuple(
            row.get("classification_reasons"), f"{path}.classification_reasons"
        ),
        evidence=evidence,
        requested_quantity=_require_int(
            row.get("requested_quantity"), f"{path}.requested_quantity", minimum=0
        ),
        reserved_quantity=_require_int(
            row.get("reserved_quantity"), f"{path}.reserved_quantity", minimum=0
        ),
        candidate_quantity=_require_int(
            row.get("candidate_quantity"), f"{path}.candidate_quantity", minimum=0
        ),
        candidate_state=_require_str(row.get("candidate_state"), f"{path}.candidate_state"),
        candidate_reasons=_require_str_tuple(
            row.get("candidate_reasons"), f"{path}.candidate_reasons"
        ),
        candidate_evidence=candidate_evidence,
        disposition_hint=_require_str(
            row.get("disposition_hint"), f"{path}.disposition_hint"
        ),
    )


def _parse_design(payload: Mapping[Any, Any], index: int) -> SnapshotDesignRow:
    path = f"designs[{index}]"
    row = _require_mapping(payload, path)
    _reject_unknown_keys(row, _DESIGN_KEYS, path)
    evidence = _require_str(row.get("evidence"), f"{path}.evidence")
    if evidence not in _VALID_EVIDENCE_VALUES:
        raise SnapshotError(f"{path}.evidence {evidence!r} is not a known evidence tier")
    if evidence in _EVIDENCE_CAP:
        # Design-row evidence is weakest(feed CAPTURED_NOT_VERIFIED,
        # entry tiers); it can never be VERIFIED under the cap.
        raise SnapshotError(
            f"{path}.evidence {evidence!r} is above the ListItemsOfAShip "
            "evidence cap (fail closed, never promoted)"
        )
    return SnapshotDesignRow(
        item_design_id=_require_int(
            row.get("item_design_id"), f"{path}.item_design_id", minimum=0
        ),
        item_name=_require_str(row.get("item_name"), f"{path}.item_name"),
        observed_quantity=_require_int(
            row.get("observed_quantity"), f"{path}.observed_quantity", minimum=0
        ),
        requested_quantity=_require_int(
            row.get("requested_quantity"), f"{path}.requested_quantity", minimum=0
        ),
        reserved_quantity=_require_int(
            row.get("reserved_quantity"), f"{path}.reserved_quantity", minimum=0
        ),
        unmet_quantity=_require_int(
            row.get("unmet_quantity"), f"{path}.unmet_quantity", minimum=0
        ),
        evidence=evidence,
        stacks=_require_int(row.get("stacks"), f"{path}.stacks", minimum=0),
    )


def _parse_condition(payload: Mapping[Any, Any], index: int) -> SnapshotCondition:
    path = f"conditions[{index}]"
    row = _require_mapping(payload, path)
    _reject_unknown_keys(row, _CONDITION_KEYS, path)
    code = _require_str(row.get("code"), f"{path}.code")
    if code not in _CONDITION_CODE_VALUES:
        raise SnapshotError(
            f"{path}.code {code!r} is not a known unresolved-condition code"
        )
    design_id = row.get("design_id")
    if design_id is not None:
        design_id = _require_int(design_id, f"{path}.design_id", minimum=0)
    return SnapshotCondition(
        code=code,
        detail=_require_str(row.get("detail"), f"{path}.detail"),
        design_id=design_id,
    )


def snapshot_from_dict(payload: Any) -> AccountSnapshot:
    """Parse and validate a snapshot mapping (fail closed).

    Rejects: unknown versions (strict int 1 - 1.0/True are rejected,
    not equal to 1), non-object roots, unknown keys at any level,
    wrong field types, negative quantities, user_id < 1, unknown
    evidence tiers, VERIFIED tiers on feed-derived rows
    (ListItemsOfAShip cap), unknown vocabulary values, off-convention
    captured_at, duplicate ItemIds, duplicate ItemDesignIds,
    stack/design contradictions (a stack referencing a design with no
    design row; stack quantities disagreeing with the design
    aggregate; candidate quantities exceeding the stack quantity;
    reserved exceeding observed), producer-impossible row couplings
    (candidate>0 on a HOLD row, CANDIDATE with zero candidate, KEEP
    hint on a CANDIDATE row, unknown designs classified anything but
    PROTECTED, protected without reasons), arithmetic contradictions
    (design reserved > requested, stack demand totals disagreeing
    with the design row, unmet != max(0, requested-observed),
    per-design candidate sum exceeding the unreserved pool,
    design-scoped conditions without a design row), and any
    secret-looking key.
    """
    root = _require_mapping(payload, "snapshot")
    _check_no_secrets(root)

    version = root.get("version")
    if type(version) is not int or version != SNAPSHOT_VERSION:
        raise SnapshotError(
            f"unsupported snapshot version {version!r}: this build reads "
            f"version {SNAPSHOT_VERSION} only (fail closed, no guessing)"
        )

    _reject_unknown_keys(root, _ROOT_KEYS, "snapshot")

    identity_payload = root.get("identity")
    if identity_payload is None:
        raise SnapshotError("identity is required")
    identity = _parse_identity(identity_payload)

    stacks_payload = root.get("stacks")
    if not isinstance(stacks_payload, list):
        raise SnapshotError("stacks must be an array")
    stacks: List[SnapshotStackRow] = []
    seen_item_ids: Dict[int, int] = {}
    for i, entry in enumerate(stacks_payload):
        if not isinstance(entry, Mapping):
            raise SnapshotError(f"stacks[{i}] must be an object")
        stack = _parse_stack(entry, i)
        if stack.item_id in seen_item_ids:
            raise SnapshotError(
                f"contradictory snapshot: duplicate ItemId {stack.item_id} "
                f"(stacks[{i}] and stacks[{seen_item_ids[stack.item_id]}])"
            )
        seen_item_ids[stack.item_id] = i
        stacks.append(stack)

    designs_payload = root.get("designs")
    if not isinstance(designs_payload, list):
        raise SnapshotError("designs must be an array")
    designs: List[SnapshotDesignRow] = []
    design_index: Dict[int, int] = {}
    for i, entry in enumerate(designs_payload):
        if not isinstance(entry, Mapping):
            raise SnapshotError(f"designs[{i}] must be an object")
        design = _parse_design(entry, i)
        if design.item_design_id in design_index:
            raise SnapshotError(
                f"contradictory snapshot: duplicate ItemDesignId "
                f"{design.item_design_id} (designs[{i}] and "
                f"designs[{design_index[design.item_design_id]}])"
            )
        design_index[design.item_design_id] = i
        designs.append(design)

    conditions_payload = root.get("conditions")
    if not isinstance(conditions_payload, list):
        raise SnapshotError("conditions must be an array")
    conditions: List[SnapshotCondition] = []
    for i, entry in enumerate(conditions_payload):
        if not isinstance(entry, Mapping):
            raise SnapshotError(f"conditions[{i}] must be an object")
        conditions.append(_parse_condition(entry, i))

    limitations_payload = root.get("limitations")
    if not isinstance(limitations_payload, list):
        raise SnapshotError("limitations must be an array of strings")
    limitations = _require_str_tuple(limitations_payload, "limitations")

    captured_at = _require_str(root.get("captured_at"), "captured_at")
    _validate_captured_at(captured_at)

    actionable = root.get("actionable")
    if actionable is None:
        actionable = False
    else:
        actionable = _require_bool(actionable, "actionable")
    if actionable is not False:
        raise SnapshotError(
            "contradictory snapshot: actionable must be false (Task H output "
            "is never actionable)"
        )

    # Cross-row contradictions: every stack's design must have a design
    # row, and the aggregate must agree with the stack rows.
    for stack in stacks:
        design = next((d for d in designs if d.item_design_id == stack.item_design_id), None)
        if design is None:
            raise SnapshotError(
                f"contradictory snapshot: stack ItemId {stack.item_id} references "
                f"design {stack.item_design_id} with no design row"
            )
        if stack.quantity > design.observed_quantity:
            raise SnapshotError(
                f"contradictory snapshot: stack ItemId {stack.item_id} quantity "
                f"{stack.quantity} exceeds design {stack.item_design_id} "
                f"observed {design.observed_quantity}"
            )
        if stack.candidate_quantity > stack.quantity:
            raise SnapshotError(
                f"contradictory snapshot: stack ItemId {stack.item_id} candidate "
                f"{stack.candidate_quantity} exceeds its quantity "
                f"{stack.quantity}"
            )
        if stack.reserved_quantity > design.observed_quantity:
            raise SnapshotError(
                f"contradictory snapshot: stack ItemId {stack.item_id} reserved "
                f"{stack.reserved_quantity} exceeds design "
                f"{stack.item_design_id} observed {design.observed_quantity}"
            )
        # Stack rows carry the design's ledger totals verbatim (the
        # builder copies ledger.total_requested/total_reserved); a
        # disagreement is a hand-edit contradiction.
        if stack.requested_quantity != design.requested_quantity:
            raise SnapshotError(
                f"contradictory snapshot: stack ItemId {stack.item_id} "
                f"requested {stack.requested_quantity} disagrees with design "
                f"{stack.item_design_id} requested {design.requested_quantity}"
            )
        if stack.reserved_quantity != design.reserved_quantity:
            raise SnapshotError(
                f"contradictory snapshot: stack ItemId {stack.item_id} "
                f"reserved {stack.reserved_quantity} disagrees with design "
                f"{stack.item_design_id} reserved {design.reserved_quantity}"
            )
        # Producer-invariant row couplings (the Task G planner guarantees
        # these for every row it emits; a persisted row that breaks one
        # was never produced by the pipeline).
        if (stack.candidate_quantity > 0) != (stack.candidate_state == STATE_CANDIDATE):
            raise SnapshotError(
                f"contradictory snapshot: stack ItemId {stack.item_id} has "
                f"candidate_quantity {stack.candidate_quantity} with state "
                f"{stack.candidate_state!r} (a CANDIDATE row always carries a "
                "positive candidate; a HOLD row always carries zero)"
            )
        if stack.candidate_state == STATE_CANDIDATE and (
            stack.candidate_reasons or stack.disposition_hint == "KEEP"
        ):
            raise SnapshotError(
                f"contradictory snapshot: stack ItemId {stack.item_id} is "
                "CANDIDATE but carries blocked reasons or a KEEP hint "
                "(producer-impossible: only HOLD rows carry those)"
            )
        if (
            stack.candidate_state == STATE_HOLD
            and not stack.candidate_reasons
        ):
            raise SnapshotError(
                f"contradictory snapshot: stack ItemId {stack.item_id} is "
                "HOLD with no blocked reason (producer-impossible: the "
                "planner always emits at least one BLOCKED_* code)"
            )
        # Task D coupling: an unknown design is always PROTECTED (rule
        # 10) and protection always sets a reason; the reverse direction
        # (reasons present) is enforced by construction.
        if stack.unknown and (
            stack.classification != Classification.PROTECTED.value
            or not stack.protected
        ):
            raise SnapshotError(
                f"contradictory snapshot: stack ItemId {stack.item_id} is "
                "unknown but not PROTECTED/protected (rule 10: an unknown "
                "design is always protected)"
            )
        if stack.protected and not stack.protection_reasons:
            raise SnapshotError(
                f"contradictory snapshot: stack ItemId {stack.item_id} is "
                "protected with no protection reason (the classifier always "
                "names the rule that fired)"
            )
        if stack.protection_reasons and not stack.protected:
            raise SnapshotError(
                f"contradictory snapshot: stack ItemId {stack.item_id} "
                "carries protection reasons but is not protected (a reason "
                "firing always protects)"
            )

    # Stack/design aggregate agreement: sum of stack quantities per design
    # must equal the design row's observed quantity, and stack counts must
    # match. (A snapshot written by build_snapshot always agrees; a hand
    # edited or corrupt one may not, and is rejected.)
    per_design_quantities: Dict[int, int] = {}
    per_design_stacks: Dict[int, int] = {}
    for stack in stacks:
        per_design_quantities[stack.item_design_id] = (
            per_design_quantities.get(stack.item_design_id, 0) + stack.quantity
        )
        per_design_stacks[stack.item_design_id] = (
            per_design_stacks.get(stack.item_design_id, 0) + 1
        )
    for design in designs:
        total = per_design_quantities.get(design.item_design_id, 0)
        if total != design.observed_quantity:
            raise SnapshotError(
                f"contradictory snapshot: design {design.item_design_id} "
                f"observed {design.observed_quantity} but stack rows sum to "
                f"{total}"
            )
        count = per_design_stacks.get(design.item_design_id, 0)
        if count != design.stacks:
            raise SnapshotError(
                f"contradictory snapshot: design {design.item_design_id} "
                f"claims {design.stacks} stacks but {count} exist"
            )
        # Producer invariants on the design row itself: the Task F ledger
        # never reserves more than requested (reserve = min(requested,
        # remaining)); unmet is the DEMAND_EXCEEDS_SUPPLY shortfall
        # (max(0, requested - observed)); a design row with stacks but
        # no stack rows is already rejected above.
        if design.reserved_quantity > design.requested_quantity:
            raise SnapshotError(
                f"contradictory snapshot: design {design.item_design_id} "
                f"reserved {design.reserved_quantity} exceeds requested "
                f"{design.requested_quantity} (the ledger never over-reserves "
                "a source's own request)"
            )
        if design.unmet_quantity != max(0, design.requested_quantity - design.observed_quantity):
            raise SnapshotError(
                f"contradictory snapshot: design {design.item_design_id} "
                f"unmet {design.unmet_quantity} is not the demand shortfall "
                f"max(0, requested {design.requested_quantity} - observed "
                f"{design.observed_quantity}) (Task F DEMAND_EXCEEDS_SUPPLY "
                "arithmetic)"
            )
        # Per-design candidate pool bound: the planner allocates from a
        # per-design unreserved pool, so the sum of stack candidates for
        # a design can never exceed unreserved (over-reservation drives
        # the pool to zero and all candidates to zero).
        candidate_total = sum(
            s.candidate_quantity
            for s in stacks
            if s.item_design_id == design.item_design_id
        )
        unreserved = max(
            0, design.observed_quantity - design.reserved_quantity
        )
        if candidate_total > unreserved:
            raise SnapshotError(
                f"contradictory snapshot: design {design.item_design_id} "
                f"stack candidates sum to {candidate_total} but unreserved "
                f"is only {unreserved} (the planner never exceeds the "
                "per-design pool)"
            )

    # Cross-block consistency: a design-scoped condition must reference a
    # design row that exists in THIS snapshot (a condition about a design
    # the snapshot does not carry cannot be reconciled with anything).
    for condition in conditions:
        if (
            condition.design_id is not None
            and condition.design_id not in design_index
        ):
            raise SnapshotError(
                f"contradictory snapshot: condition {condition.code!r} "
                f"references design {condition.design_id} with no design row"
            )

    return AccountSnapshot(
        version=SNAPSHOT_VERSION,
        identity=identity,
        stacks=tuple(sorted(stacks, key=lambda s: s.item_id)),
        designs=tuple(sorted(designs, key=lambda d: d.item_design_id)),
        conditions=tuple(
            sorted(
                conditions,
                key=lambda c: (c.code, c.detail, c.design_id if c.design_id is not None else -1),
            )
        ),
        limitations=limitations,
        captured_at=captured_at,
        actionable=False,
    )


def _validate_captured_at(captured_at: str) -> None:
    """Structural captured_at gate (pinned §3b/§4).

    ISO-8601 UTC Z-suffix, seconds precision, exactly. Unparseable or
    off-convention timestamps are malformed at DESERIALIZATION time.
    Future-ness is deliberately NOT checked here (it is clock-relative;
    the purity rule forbids clock reads in sdk/inventory, so it runs
    where ``now`` is injected: portfolio_render / view time).
    """
    import datetime

    try:
        datetime.datetime.strptime(captured_at, _CAPTURED_AT_FORMAT)
    except ValueError as exc:
        raise SnapshotError(
            f"captured_at {captured_at!r} is malformed: v1 requires "
            "ISO-8601 UTC with Z suffix and seconds precision "
            "(YYYY-MM-DDTHH:MM:SSZ)"
        ) from exc


def snapshot_from_json(text: str) -> AccountSnapshot:
    """Parse snapshot JSON text (fail closed on any malformed input).

    Duplicate JSON keys make a payload ambiguous (last-wins rewrites
    any field), so they are malformed too - not silently resolved.
    """
    import json

    if not isinstance(text, str):
        raise SnapshotError(f"snapshot text must be a string, got {type(text).__name__}")

    def _reject_duplicate_keys(pairs):
        seen = set()
        for key, _value in pairs:
            if key in seen:
                raise ValueError(f"duplicate JSON key {key!r}")
            seen.add(key)
        return dict(pairs)

    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        raise SnapshotError(f"snapshot JSON is malformed: {exc}") from exc
    return snapshot_from_dict(payload)


def build_snapshot(
    identity: SnapshotAccountIdentity,
    classified: Sequence[ClassifiedItem],
    ledger: ReservationLedger,
    reduction: ReductionPlan,
    limitations: Sequence[str] = (),
    captured_at: str = "1970-01-01T00:00:00Z",
) -> AccountSnapshot:
    """Deterministically project Tasks D/F/G outputs into a snapshot.

    Pure: reads its inputs, writes a frozen value object. The caller
    supplies already-computed Task D classification, Task F ledger and
    Task G reduction plan; nothing is recomputed or reinterpreted here,
    and no secret can enter because the parameter types carry none.
    ``captured_at`` is caller-supplied (deterministic given the caller;
    the builder never reads a clock) and must already be pinned to the
    v1 convention (ISO-8601 UTC Z-suffix seconds); the builder
    validates it and refuses fail-closed on any off-convention value.
    """
    if identity is None:
        raise SnapshotError("identity is required")
    _validate_captured_at(str(captured_at))
    stacks: List[SnapshotStackRow] = []
    rows_by_item: Dict[int, Any] = {row.item_id: row for row in reduction.rows}
    seen_classified: Dict[int, int] = {}
    for ci in classified:
        item = ci.item
        if item.item_id in seen_classified:
            # AD-4 (never silently double-count): the catalog already
            # rejects duplicate ItemIds; a classified sequence with a
            # duplicate is a caller bug and refuses fail-closed here.
            raise SnapshotError(
                f"contradictory snapshot input: duplicate ItemId "
                f"{item.item_id} in classified sequence (position "
                f"{seen_classified[item.item_id]} and later); refusing to "
                "build a double-counted snapshot"
            )
        seen_classified[item.item_id] = len(stacks)
        row = rows_by_item.get(item.item_id)
        if row is None:
            # A classified item with no reduction row cannot happen with
            # the real planner (it emits one row per stack); refusing is
            # the fail-closed direction for a persistence layer.
            raise SnapshotError(
                f"reduction plan has no row for ItemId {item.item_id}; refusing "
                "to build a partial snapshot"
            )
        design_id = item.item_design_id
        stacks.append(
            SnapshotStackRow(
                item_id=item.item_id,
                item_design_id=design_id,
                ship_id=item.ship_id,
                quantity=item.quantity,
                classification=ci.classification.value,
                protected=ci.protected,
                unknown=ci.unknown,
                roles=tuple(role.value for role in ci.roles),
                protection_reasons=tuple(ci.protection_reasons),
                classification_reasons=tuple(ci.classification_reasons),
                evidence=ci.evidence.value,
                requested_quantity=ledger.total_requested(design_id),
                reserved_quantity=ledger.total_reserved(design_id),
                candidate_quantity=row.candidate_quantity,
                candidate_state=row.state,
                candidate_reasons=tuple(row.reasons),
                candidate_evidence=row.evidence.value,
                disposition_hint=row.disposition_hint.value,
            )
        )

    # Design rows: one per design appearing in stacks OR in the ledger.
    design_ids = {s.item_design_id for s in stacks}
    design_ids.update(ledger.design_ids())
    designs: List[SnapshotDesignRow] = []
    for design_id in sorted(design_ids):
        observed = ledger.observed_quantity(design_id)
        requested = ledger.total_requested(design_id)
        reserved = ledger.total_reserved(design_id)
        # Unmet demand: requested quantity the ledger could not hold
        # against observed supply (Task F exposes the same shortfall as a
        # DEMAND_EXCEEDS_SUPPLY condition; the snapshot surfaces it as an
        # explicit number for read-only reporting).
        unmet = max(0, requested - observed)
        design: Optional[ItemDesign] = None
        for ci in classified:
            if ci.item.item_design_id == design_id and ci.design is not None:
                design = ci.design
                break
        designs.append(
            SnapshotDesignRow(
                item_design_id=design_id,
                item_name=(design.item_name if design is not None else ""),
                observed_quantity=observed,
                requested_quantity=requested,
                reserved_quantity=reserved,
                unmet_quantity=unmet,
                evidence=ledger.evidence_for(design_id).value,
                stacks=len([s for s in stacks if s.item_design_id == design_id]),
            )
        )

    conditions = [
        SnapshotCondition(code=c.code, detail=c.detail, design_id=c.design_id)
        for c in ledger.conditions
    ]
    conditions.sort(
        key=lambda c: (c.code, c.detail, c.design_id if c.design_id is not None else -1)
    )

    return AccountSnapshot(
        version=SNAPSHOT_VERSION,
        identity=identity,
        stacks=tuple(sorted(stacks, key=lambda s: s.item_id)),
        designs=tuple(designs),
        conditions=tuple(conditions),
        limitations=tuple(str(x) for x in limitations),
        captured_at=str(captured_at),
        actionable=False,
    )
