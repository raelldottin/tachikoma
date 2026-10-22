from __future__ import annotations

"""Pure renderer for `inventory plan` (Tasks D + F + G view).

Mechanically extracted from sdk/commands.py `_render_inventory_plan`
(Task H architect lane): the text assembly below is a verbatim move of
the hotspot method body, with the only I/O-touching parts (ship fetch,
strategic-reserve settings read) lifted into caller-supplied parameters.
Byte parity with the pre-extraction output is pinned by
tests/test_inventory_plan_render.py (golden sha256 + full-text golden).

Read-only Task D + Task F + Task G view (dry-run, nothing executable).

Renders the pure classification surface exported by sdk.inventory
(what each item IS and what protections apply), the Task F
demand/reservation ledger (what quantities known plans actually
claim, with provenance and fail-closed conditions), and the Task G
dry-run reduction section (bounded candidate arithmetic and
informational disposition hints — actionable=false on every row).
No surplus, deficit, disposable or sale quantity is executed or
recommended here, and nothing in this view authorizes any
mutation.

Purity: no network, no disk, no clock, no client. All inputs are
already-computed domain objects; the caller (sdk/commands.py) owns
transport and configuration reads.
"""

from typing import Mapping, Optional, Sequence

from .catalog import InventoryCatalog
from ._types import Item
from .classify import InventoryClassifier
from .demand import DemandPlanner, PlanningContext
from .reduction import ReductionPlanner


def render_inventory_plan(
    catalog: InventoryCatalog,
    items: Sequence[Item],
    ship_data: Optional[Mapping],
    strategic_reserve: Optional[Mapping],
) -> str:
    """Render the read-only plan text (deterministic, byte-stable).

    ``ship_data`` is the parsed GetShipByUserId payload (or None when
    unavailable — fail closed: reference sources surface as conditions,
    never a negative evaluation). ``strategic_reserve`` is the explicit
    configuration mapping ({} when absent — no default is invented).
    """
    plan_ctx = PlanningContext.from_ship_dict(
        ship_data,
        strategic_reserve=strategic_reserve,
    )
    classifier = InventoryClassifier(context=plan_ctx.protection_context())
    classified = classifier.classify(catalog)
    ledger = DemandPlanner().plan(catalog, plan_ctx)

    counts: dict = {}
    for ci in classified:
        counts[ci.classification.name] = counts.get(ci.classification.name, 0) + 1

    lines = [
        "=== Inventory plan (READ-ONLY Tasks D+F+G — classification + "
        "demand + dry-run reduction, nothing executable) ===",
        "Classification reports what each item IS and what protections "
        "apply. Demand reports what quantity known plans actually claim, "
        "with provenance. The dry-run section reports what COULD be "
        "considered if reduction were ever authorized: every row is "
        "informational (actionable=false), hints name no executor, "
        "and no row is a recommendation. Nothing below is actionable.",
        f"Item stacks: {len(items)}",
        "Classified stacks: "
        + ", ".join(
            f"{name} {counts.get(name, 0)}"
            for name in ("PROTECTED", "RESERVED", "CONSUMABLE")
        ),
        "Zero-quantity stacks retained by server: "
        f"{sum(1 for i in items if i.quantity == 0)} (never actionable)",
        "",
        "Per-item classification (ascending ItemId; 'candidate' marks a "
        "classification label only, never a recommendation):",
    ]
    for ci in classified:
        item = ci.item
        line = (
            f"  item {item.item_id} | design {item.item_design_id} "
            f"| qty {item.quantity} | {ci.classification.name} "
            f"| protected: {'yes' if ci.protected else 'no'} "
            f"| evidence {ci.evidence.value}"
        )
        if ci.unknown:
            line += " | unresolved"
        lines.append(line)
        if ci.design is not None:
            name = ci.design.item_name.strip() or "(no name in design feed)"
            lines.append(f"    name: {name}")
        else:
            lines.append(
                "    name: unavailable (design absent from captured dynamic design feed)"
            )
        if ci.protection_reasons:
            lines.append(
                f"    protection reasons: {', '.join(ci.protection_reasons)}"
            )
        if ci.roles:
            lines.append(f"    roles: {', '.join(role.name for role in ci.roles)}")
        if ci.classification_reasons:
            lines.append("    classification reasons:")
            for reason in ci.classification_reasons:
                lines.append(f"      - {reason}")
    if classifier.limitations:
        lines.append("")
        lines.append(
            "Limitations (rules not evaluated or data unavailable; never a demotion):"
        )
        for limitation in classifier.limitations:
            lines.append(f"  - {limitation}")

    # -----------------------------------------------------------
    # Task F demand/reservation ledger (ascending ItemDesignId;
    # entries sorted by source precedence then provenance).
    # -----------------------------------------------------------
    lines.append("")
    lines.append(
        "Demand/reservation ledger (Task F; ascending design id; "
        "requested/reserved per source; remaining quantity is NOT "
        "computed in this ledger — the Task G dry-run section below "
        "computes unreserved = max(0, observed - reserved)):"
    )
    for design_id in ledger.design_ids():
        observed_qty = ledger.observed_quantity(design_id)
        total_reserved = ledger.total_reserved(design_id)
        total_requested = ledger.total_requested(design_id)
        lines.append(
            f"  design {design_id} | observed {observed_qty} "
            f"| requested {total_requested} | reserved {total_reserved} "
            f"| evidence {ledger.evidence_for(design_id).value}"
        )
        for entry in ledger.entries_for(design_id):
            lines.append(
                f"    {entry.source.value} requested {entry.requested_quantity} "
                f"reserved {entry.reserved_quantity} | evidence "
                f"{entry.evidence.value} | provenance: {entry.provenance}"
            )
            lines.append(f"      reason: {entry.reason}")
        for condition in ledger.conditions_for(design_id):
            lines.append(
                f"    unresolved [{condition.code}]: {condition.detail}"
            )
    global_conditions = tuple(
        c for c in ledger.conditions if c.design_id is None
    )
    if global_conditions:
        lines.append("")
        lines.append("Unresolved demand conditions (fail-closed; never a demotion):")
        for condition in global_conditions:
            lines.append(f"  [{condition.code}] {condition.detail}")

    # -----------------------------------------------------------
    # Task G dry-run reduction candidates (HOLD/CANDIDATE per
    # stack; actionable=false everywhere; hints informational).
    # -----------------------------------------------------------
    reduction = ReductionPlanner().plan(classified, ledger)
    lines.append("")
    lines.append(
        "Dry-run reduction candidates (Task G; per stack; DRY-RUN "
        "ONLY — every row actionable=false; no executor exists):"
    )
    lines.append(
        "unreserved = max(0, observed - reserved); unreserved does "
        "not mean reducible. candidate respects "
        "0<=candidate<=unreserved<=observed and is 0 for protected, "
        "unknown, malformed, over-reserved, shared/conflicted, or "
        "applicable-unsupported-demand stacks."
    )
    hold_count = sum(1 for r in reduction.rows if r.state == "HOLD")
    candidate_count = sum(1 for r in reduction.rows if r.state == "CANDIDATE")
    lines.append(
        f"Stacks: HOLD {hold_count}, CANDIDATE {candidate_count}, "
        f"total candidate quantity {reduction.total_candidates()} "
        "(informational only)"
    )
    for row in reduction.rows:
        line = (
            f"    item {row.item_id} | design {row.item_design_id} "
            f"| qty observed {row.observed_quantity} | reserved "
            f"{row.total_reserved} | unreserved {row.unreserved} "
            f"| candidate {row.candidate_quantity} | {row.state} "
            f"| hint {row.disposition_hint.value} | evidence "
            f"{row.evidence.value} | actionable {row.actionable}"
        )
        lines.append(line)
        if row.reasons:
            lines.append(
                f"      reasons: {', '.join(row.reasons)}"
            )
    lines.append("")
    lines.append(
        "Disposition hints (KEEP/USE/CRAFT/ALLOCATE) are future-order "
        "information only; TRANSFER/SELL/CONSUME/DELETE remain "
        "deferred and unassigned."
    )
    lines.append("")
    lines.append(
        "No surplus, deficit, disposable quantity, sale quantity or "
        "reduction action is executed or recommended in this slice; "
        "no role, reservation, condition or candidate shown here is "
        "a mutation recommendation. 'inventory apply' remains refused."
    )
    return "\n".join(lines)
