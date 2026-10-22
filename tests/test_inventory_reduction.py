"""Adversarial Task G reduction tests: arithmetic, protection, evidence,
uncertainty monotonicity, unsupported demand, shared refs, determinism,
mutation isolation, network isolation, and apply refusal.

Every test builds synthetic catalogs/ledgers only — no Client, no
network, no live traffic. Over-reserved ledgers are hand-built (the
Task F planner structurally cannot emit one).
"""

from __future__ import annotations

import ast
import socket
import unittest
from pathlib import Path

import sdk.inventory as inv
from sdk.inventory import (
    BLOCKED_DESIGN_CONDITION,
    BLOCKED_MALFORMED_ROW,
    BLOCKED_OVER_RESERVED,
    BLOCKED_PROTECTED_OR_UNKNOWN,
    BLOCKED_STACK_CONDITION,
    BLOCKED_STRATEGIC_ROLE,
    BLOCKED_UNSUPPORTED_DEMAND,
    BLOCKED_ZERO_QUANTITY,
    BLOCKED_ZERO_UNRESERVED,
    BOTH_EQUIPPED_AND_ROOM_ATTACHED,
    DEMAND_EXCEEDS_SUPPLY,
    DUPLICATE_CONFIG_ENTRY,
    EQUIPMENT_SHARED_STACK,
    MALFORMED_QUANTITY,
    MODULE_MULTI_ATTACH,
    DemandPlanner,
    Evidence,
    InventoryCatalog,
    InventoryClassifier,
    Item,
    ItemDesign,
    PlanningContext,
    ReductionPlanner,
    ReservationEntry,
    ReservationLedger,
    ReservationSource,
    STATE_CANDIDATE,
    STATE_HOLD,
    UnresolvedDemand,
)

INVENTORY_DIR = Path(__file__).resolve().parent.parent / "sdk" / "inventory"


def make_design(
    design_id,
    name="Widget",
    sub_type="",
    item_type="",
    rarity="common",
    tags=(),
    ingredients=(),
    module_type=None,
):
    return ItemDesign(
        item_design_id=design_id,
        item_name=name,
        item_type=item_type,
        item_sub_type=sub_type,
        rarity=rarity,
        tags=tuple(tags),
        ingredients=tuple(ingredients),
        module_type=module_type,
        training_design_id=None,
    )


def make_item(item_id, design_id, quantity, ship_id=1):
    return Item(
        item_id=item_id,
        ship_id=ship_id,
        item_design_id=design_id,
        quantity=quantity,
        is_new=False,
        skin_key=None,
    )


def make_ledger(
    entries=(),
    conditions=(),
    observed=None,
):
    return ReservationLedger(
        entries=tuple(entries),
        conditions=tuple(conditions),
        observed_by_design=dict(observed or {}),
    )


def entry(design_id, source, requested, reserved, evidence=Evidence.CAPTURED_NOT_VERIFIED):
    return ReservationEntry(
        item_design_id=design_id,
        source=source,
        requested_quantity=requested,
        reserved_quantity=reserved,
        reason="test",
        evidence=evidence,
        provenance="test",
    )


def plan_for(items, designs, ledger, context=None):
    catalog = InventoryCatalog(items, inv.DesignCatalog(designs))
    classified = InventoryClassifier(
        context=(context or PlanningContext().protection_context())
    ).classify(catalog)
    return ReductionPlanner().plan(classified, ledger), classified


class TestArithmetic(unittest.TestCase):
    """unreserved=max(0,observed-reserved); 0<=candidate<=unreserved<=observed."""

    def test_exact_reservation_leaves_zero_unreserved(self):
        d = make_design(1)
        i = make_item(10, 1, 5)
        ledger = make_ledger(
            entries=[entry(1, ReservationSource.EQUIPMENT, 5, 5)],
            observed={1: 5},
        )
        plan, _ = plan_for([i], [d], ledger)
        self.assertEqual(len(plan.rows), 1)
        row = plan.rows[0]
        self.assertEqual(row.observed_quantity, 5)
        self.assertEqual(row.total_reserved, 5)
        self.assertEqual(row.unreserved, 0)
        self.assertEqual(row.candidate_quantity, 0)
        self.assertEqual(row.state, STATE_HOLD)
        self.assertIn(BLOCKED_ZERO_UNRESERVED, row.reasons)

    def test_partial_reservation_candidate_is_unreserved(self):
        d = make_design(1)
        i = make_item(10, 1, 5)
        ledger = make_ledger(
            entries=[entry(1, ReservationSource.EQUIPMENT, 2, 2)],
            observed={1: 5},
        )
        plan, _ = plan_for([i], [d], ledger)
        row = plan.rows[0]
        self.assertEqual(row.unreserved, 3)
        self.assertEqual(row.candidate_quantity, 3)
        self.assertEqual(row.state, STATE_CANDIDATE)
        self.assertEqual(row.reasons, ())

    def test_invariant_chain_holds_on_every_row(self):
        designs = [make_design(1), make_design(2)]
        items = [
            make_item(10, 1, 7),
            make_item(11, 1, 3),
            make_item(12, 2, 9),
        ]
        ledger = make_ledger(
            entries=[entry(1, ReservationSource.EQUIPMENT, 4, 4)],
            observed={1: 10, 2: 9},
        )
        plan, _ = plan_for(items, designs, ledger)
        for row in plan.rows:
            self.assertGreaterEqual(row.candidate_quantity, 0)
            self.assertLessEqual(row.candidate_quantity, row.unreserved)
            self.assertLessEqual(row.unreserved, row.observed_quantity)
            self.assertFalse(row.actionable)

    def test_over_reserved_ledger_fails_closed_every_stack(self):
        # Hand-built: the Task F planner can never emit reserved > observed.
        d = make_design(1)
        items = [make_item(10, 1, 5), make_item(11, 1, 2)]
        ledger = make_ledger(
            entries=[entry(1, ReservationSource.EQUIPMENT, 99, 99)],
            observed={1: 7},
        )
        plan, _ = plan_for(items, [d], ledger)
        for row in plan.rows:
            self.assertEqual(row.candidate_quantity, 0)
            self.assertEqual(row.state, STATE_HOLD)
            self.assertIn(BLOCKED_OVER_RESERVED, row.reasons)
            # reported arithmetic stays truthful, never fabricated
            self.assertEqual(row.total_reserved, 99)
            self.assertEqual(row.unreserved, 0)

    def test_zero_quantity_stack_gets_candidate_zero_with_reason(self):
        d = make_design(1)
        i = make_item(10, 1, 0)
        ledger = make_ledger(observed={1: 0})
        plan, _ = plan_for([i], [d], ledger)
        row = plan.rows[0]
        self.assertEqual(row.candidate_quantity, 0)
        self.assertIn(BLOCKED_ZERO_QUANTITY, row.reasons)
        self.assertEqual(row.state, STATE_HOLD)

    def test_negative_quantity_row_is_malformed_not_supply(self):
        d = make_design(1)
        i = make_item(10, 1, -3)
        ledger = make_ledger(observed={1: 0})  # planner counts negative as 0
        plan, _ = plan_for([i], [d], ledger)
        row = plan.rows[0]
        self.assertEqual(row.candidate_quantity, 0)
        self.assertIn(BLOCKED_MALFORMED_ROW, row.reasons)

    def test_multi_stack_design_candidates_sum_within_unreserved(self):
        # OVER-COUNT DEFECT PROBE: observed 10 = stacks 6+4, reserved 8
        # -> unreserved 2. Per-row min(qty, unreserved)=2 each would
        # total 4 > 2. The pool allocation must keep the sum <= 2.
        d = make_design(1)
        items = [make_item(10, 1, 6), make_item(11, 1, 4)]
        ledger = make_ledger(
            entries=[entry(1, ReservationSource.EQUIPMENT, 8, 8)],
            observed={1: 10},
        )
        plan, _ = plan_for(items, [d], ledger)
        by_item = {r.item_id: r for r in plan.rows}
        self.assertEqual(by_item[10].candidate_quantity, 2)
        self.assertEqual(by_item[11].candidate_quantity, 0)
        self.assertIn("BLOCKED_ALLOCATION_EXHAUSTED", by_item[11].reasons)
        self.assertEqual(
            sum(r.candidate_quantity for r in plan.rows),
            2,
            "per-design candidate sum must not exceed unreserved",
        )

    def test_per_design_candidate_sum_never_exceeds_unreserved(self):
        # Property probe across many stack splits and reservation levels.
        for stacks in ([1, 1, 1], [5, 5], [9, 1], [10], [2, 3, 5]):
            for reserved in range(0, sum(stacks) + 1):
                d = make_design(1)
                items = [
                    make_item(100 + index, 1, qty)
                    for index, qty in enumerate(stacks)
                ]
                total = sum(stacks)
                ledger = make_ledger(
                    entries=[entry(1, ReservationSource.EQUIPMENT, reserved, reserved)]
                    if reserved
                    else [],
                    observed={1: total},
                )
                plan, _ = plan_for(items, [d], ledger)
                expected_unreserved = max(0, total - reserved)
                got = sum(r.candidate_quantity for r in plan.rows)
                self.assertLessEqual(
                    got,
                    expected_unreserved,
                    f"stacks={stacks} reserved={reserved}: sum {got} > unreserved",
                )
                for r in plan.rows:
                    self.assertLessEqual(r.candidate_quantity, r.unreserved)
                    self.assertLessEqual(r.unreserved, r.observed_quantity)


class TestProtectionGating(unittest.TestCase):
    """Protected/unknown stacks get candidate=0 regardless of arithmetic."""

    def test_protected_stack_zero_candidate(self):
        d = make_design(1, rarity="legendary")
        i = make_item(10, 1, 50)
        ledger = make_ledger(observed={1: 50})
        plan, _ = plan_for([i], [d], ledger)
        row = plan.rows[0]
        self.assertEqual(row.candidate_quantity, 0)
        self.assertIn(BLOCKED_PROTECTED_OR_UNKNOWN, row.reasons)

    def test_unknown_design_zero_candidate(self):
        i = make_item(10, 999, 40)  # design absent from feed
        ledger = make_ledger(observed={999: 40})
        plan, _ = plan_for([i], [], ledger)
        row = plan.rows[0]
        self.assertEqual(row.candidate_quantity, 0)
        self.assertIn(BLOCKED_PROTECTED_OR_UNKNOWN, row.reasons)

    def test_equipped_stack_zero_candidate(self):
        d = make_design(1, sub_type="equipment")
        i = make_item(10, 1, 6)
        ledger = make_ledger(observed={1: 6})
        ctx = PlanningContext(equipped_references={10: 1}, room_references={})
        catalog = InventoryCatalog([i], inv.DesignCatalog([d]))
        classified = InventoryClassifier(context=ctx.protection_context()).classify(catalog)
        plan = ReductionPlanner().plan(classified, ledger)
        row = plan.rows[0]
        self.assertEqual(row.candidate_quantity, 0)
        self.assertIn(BLOCKED_PROTECTED_OR_UNKNOWN, row.reasons)


class TestEvidence(unittest.TestCase):
    """Weakest-input evidence; never promoted; cap at CAPTURED_NOT_VERIFIED."""

    def test_evidence_is_minimum_across_inputs(self):
        d = make_design(1)
        i = make_item(10, 1, 5)
        ledger = make_ledger(
            entries=[entry(1, ReservationSource.EQUIPMENT, 1, 1, evidence=Evidence.INFERRED)],
            observed={1: 5},
        )
        plan, _ = plan_for([i], [d], ledger)
        self.assertEqual(plan.rows[0].evidence, Evidence.INFERRED)

    def test_no_result_reaches_verified(self):
        d = make_design(1)
        i = make_item(10, 1, 5)
        ledger = make_ledger(
            entries=[entry(1, ReservationSource.EQUIPMENT, 1, 1, evidence=Evidence.VERIFIED)],
            observed={1: 5},
        )
        plan, _ = plan_for([i], [d], ledger)
        # feed rows are CAPTURED_NOT_VERIFIED -> the row can never be VERIFIED
        self.assertNotEqual(plan.rows[0].evidence, Evidence.VERIFIED)

    def test_design_without_entries_gets_unknown_evidence(self):
        d = make_design(1)
        i = make_item(10, 1, 5)
        ledger = make_ledger(observed={1: 5})
        plan, _ = plan_for([i], [d], ledger)
        self.assertEqual(plan.rows[0].evidence, Evidence.UNKNOWN)


class TestUncertaintyMonotonicity(unittest.TestCase):
    """Adding a blocking condition can only lower candidates, never raise."""

    def test_design_condition_blocks_candidate(self):
        d = make_design(1)
        i = make_item(10, 1, 5)
        clean = make_ledger(observed={1: 5})
        blocked = make_ledger(
            conditions=[
                UnresolvedDemand(DEMAND_EXCEEDS_SUPPLY, "test shortfall", 1),
            ],
            observed={1: 5},
        )
        plan_clean, _ = plan_for([i], [d], clean)
        plan_blocked, _ = plan_for([i], [d], blocked)
        self.assertEqual(plan_clean.rows[0].candidate_quantity, 5)
        self.assertEqual(plan_blocked.rows[0].candidate_quantity, 0)
        self.assertIn(BLOCKED_DESIGN_CONDITION, plan_blocked.rows[0].reasons)

    def test_more_uncertainty_never_increases_candidate(self):
        # monotonicity probe: every added condition keeps candidate <= baseline
        d = make_design(1)
        i = make_item(10, 1, 9)
        baseline = make_ledger(observed={1: 9})
        conditions = [
            UnresolvedDemand(MALFORMED_QUANTITY, "bad qty", 1),
            UnresolvedDemand(DUPLICATE_CONFIG_ENTRY, "dup", 1),
            UnresolvedDemand("UNKNOWN_DESIGN", "unknown", 1),
        ]
        for condition in conditions:
            ledger = make_ledger(conditions=[condition], observed={1: 9})
            plan_c, _ = plan_for([i], [d], ledger)
            plan_b, _ = plan_for([i], [d], baseline)
            self.assertLessEqual(
                plan_c.rows[0].candidate_quantity,
                plan_b.rows[0].candidate_quantity,
                f"condition {condition.code} raised the candidate",
            )

    def test_stack_condition_blocks_only_that_stack(self):
        d1 = make_design(1)
        d2 = make_design(2)
        affected = make_item(10, 1, 5)
        sibling = make_item(11, 2, 5)
        ledger = make_ledger(
            conditions=[
                UnresolvedDemand(
                    EQUIPMENT_SHARED_STACK,
                    "ItemId 10 is referenced by 3 characters; shared",
                    1,
                ),
            ],
            observed={1: 5, 2: 5},
        )
        plan, _ = plan_for([affected, sibling], [d1, d2], ledger)
        by_item = {r.item_id: r for r in plan.rows}
        self.assertEqual(by_item[10].candidate_quantity, 0)
        self.assertIn(BLOCKED_STACK_CONDITION, by_item[10].reasons)
        self.assertEqual(by_item[11].candidate_quantity, 5)
        self.assertEqual(by_item[11].state, STATE_CANDIDATE)

    def test_shared_and_multi_attach_conditions_block(self):
        d1 = make_design(1)
        d2 = make_design(2)
        i1 = make_item(10, 1, 5)
        i2 = make_item(11, 2, 5)
        ledger = make_ledger(
            conditions=[
                UnresolvedDemand(EQUIPMENT_SHARED_STACK, "ItemId 10 shared", 1),
                UnresolvedDemand(MODULE_MULTI_ATTACH, "ItemId 11 multi-attached", 2),
                UnresolvedDemand(BOTH_EQUIPPED_AND_ROOM_ATTACHED, "ItemId 12 both", None),
            ],
            observed={1: 5, 2: 5},
        )
        plan, _ = plan_for([i1, i2], [d1, d2], ledger)
        self.assertTrue(all(r.candidate_quantity == 0 for r in plan.rows))
        self.assertTrue(all(BLOCKED_STACK_CONDITION in r.reasons for r in plan.rows))


class TestUnsupportedDemand(unittest.TestCase):
    """Training/ammunition unsupported demand blocks only applicable designs."""

    def test_training_role_stack_blocked(self):
        d = make_design(1, sub_type="instanttraining")
        i = make_item(10, 1, 5)
        ledger = make_ledger(observed={1: 5})
        plan, _ = plan_for([i], [d], ledger)
        row = plan.rows[0]
        self.assertEqual(row.candidate_quantity, 0)
        self.assertIn(BLOCKED_UNSUPPORTED_DEMAND, row.reasons)

    def test_ammunition_role_stack_blocked(self):
        d = make_design(1, item_type="Missile")
        i = make_item(10, 1, 15)
        ledger = make_ledger(observed={1: 15})
        plan, _ = plan_for([i], [d], ledger)
        self.assertEqual(plan.rows[0].candidate_quantity, 0)
        self.assertIn(BLOCKED_UNSUPPORTED_DEMAND, plan.rows[0].reasons)

    def test_unrelated_design_not_blocked_by_global_source_unsupported(self):
        # The TRAINING/AMMUNITION SOURCE_UNSUPPORTED conditions are
        # global on every plan; only role-carrying designs block.
        d = make_design(1, sub_type="consumable")
        i = make_item(10, 1, 5)
        ledger = make_ledger(
            conditions=[
                UnresolvedDemand("SOURCE_UNSUPPORTED", "TRAINING unsupported"),
                UnresolvedDemand("SOURCE_UNSUPPORTED", "AMMUNITION unsupported"),
            ],
            observed={1: 5},
        )
        plan, _ = plan_for([i], [d], ledger)
        row = plan.rows[0]
        self.assertEqual(row.candidate_quantity, 5)
        self.assertEqual(row.state, STATE_CANDIDATE)
        self.assertEqual(row.reasons, ())

    def test_strategic_role_without_config_blocks_with_keep_hint(self):
        # Design 90 Half Day Immunity: STRATEGIC_RESERVE_CANDIDATE and
        # eligible by every other rule. Decision: candidate=0, hint KEEP.
        # (Lane t_f4dd7bad note 5: planner must decide and test BOTH
        # directions — this is direction A: no config -> HOLD.)
        d = make_design(90, name="Half Day Immunity", sub_type="immunity")
        i = make_item(10, 90, 1)
        ledger = make_ledger(observed={90: 1})
        plan, _ = plan_for([i], [d], ledger)
        row = plan.rows[0]
        self.assertEqual(row.candidate_quantity, 0)
        self.assertIn(BLOCKED_STRATEGIC_ROLE, row.reasons)
        self.assertEqual(row.disposition_hint.value, "KEEP")

    def test_strategic_role_with_config_reservation_stays_reserved_zero(self):
        # Direction B: WITH an explicit config-backed reservation the
        # strategic intent is resolved — but a fully-reserved stack has
        # unreserved 0, so the candidate is 0 anyway (zero-unreserved
        # gate). The two directions differ in REASON, not in candidate:
        # never actionable either way.
        d = make_design(90, name="Half Day Immunity", sub_type="immunity")
        i = make_item(10, 90, 1)
        catalog = InventoryCatalog([i], inv.DesignCatalog([d]))
        ctx = PlanningContext(
            equipped_references={},
            room_references={},
            strategic_reserve={90: 1},
        )
        classified = InventoryClassifier(context=ctx.protection_context()).classify(catalog)
        ledger = DemandPlanner().plan(catalog, ctx)
        plan = ReductionPlanner().plan(classified, ledger)
        row = plan.rows[0]
        self.assertEqual(row.candidate_quantity, 0)
        self.assertEqual(row.state, STATE_HOLD)
        # reserved by the explicit strategic config -> zero-unreserved
        self.assertIn(BLOCKED_ZERO_UNRESERVED, row.reasons)
        self.assertNotIn(BLOCKED_STRATEGIC_ROLE, row.reasons)


class TestDeterminism(unittest.TestCase):
    """Identical inputs -> identical output, regardless of input order."""

    def _build(self, seed_order):
        designs = [make_design(1), make_design(2), make_design(3)]
        items = [
            make_item(10, 1, 5),
            make_item(11, 2, 5),
            make_item(12, 3, 5),
        ]
        if seed_order:
            items = list(reversed(items))
        ledger = make_ledger(
            entries=[entry(2, ReservationSource.EQUIPMENT, 1, 1)],
            observed={1: 5, 2: 5, 3: 5},
        )
        return plan_for(items, designs, ledger)[0]

    def test_identical_inputs_identical_output(self):
        plan_a = self._build(False)
        plan_b = self._build(False)
        self.assertEqual(plan_a.to_dicts(), plan_b.to_dicts())

    def test_input_order_does_not_change_output(self):
        plan_a = self._build(False)
        plan_b = self._build(True)
        self.assertEqual(plan_a.to_dicts(), plan_b.to_dicts())

    def test_rows_sorted_ascending_item_id(self):
        plan = self._build(False)
        ids = [r.item_id for r in plan.rows]
        self.assertEqual(ids, sorted(ids))


class TestNonActionability(unittest.TestCase):
    """Every row actionable=false; no executor surface exists."""

    def test_every_row_actionable_false(self):
        designs = [make_design(1), make_design(2, rarity="epic")]
        items = [make_item(10, 1, 5), make_item(11, 2, 5)]
        ledger = make_ledger(observed={1: 5, 2: 5})
        plan, _ = plan_for(items, designs, ledger)
        self.assertTrue(all(r.actionable is False for r in plan.rows))
        self.assertEqual(plan.actionable_rows, ())

    def test_apply_refusal_is_unconditional_constant(self):
        import inspect
        import textwrap

        from sdk.commands import CommandRegistry

        source = textwrap.dedent(
            inspect.getsource(CommandRegistry._cmd_inventory_apply)
        )
        tree = ast.parse(source)
        returns = [
            n.value.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        ]
        self.assertEqual(len(returns), 1)
        self.assertIn("REFUSED", returns[0])
        # no config/env reads in the handler CODE. The handler body is
        # [docstring Expr, Return]; every statement besides the leading
        # docstring is dumped and scanned, so prose mentioning
        # "environment variable" cannot trip the check.
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        body = func.body  # statements of the handler
        code_stmts = (
            body[1:]
            if body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            else body
        )
        code_text = "\n".join(ast.dump(n) for n in code_stmts)
        # Real config/env reads would appear as Attribute/Call nodes:
        # os.environ, getenv(...), self.client.settings, open(...).
        # Scan only those node types; the refusal MESSAGE prose says
        # "environment variable" and must not trip this check.
        access_nodes = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.Attribute, ast.Call, ast.Import, ast.ImportFrom))
        ]
        access_text = "\n".join(ast.dump(n) for n in access_nodes)
        for banned in ("environ", "getenv", "settings", "open"):
            self.assertNotIn(
                banned, access_text, f"apply handler code reads {banned}"
            )
        # and the only statement after the docstring is the constant return
        self.assertEqual(len(code_stmts), 1)
        self.assertIsInstance(code_stmts[0], ast.Return)

    def test_no_deferred_disposition_members_exist(self):
        from sdk.inventory._types import Disposition

        names = {d.name for d in Disposition}
        for banned in ("TRANSFER", "SELL", "CONSUME", "DELETE"):
            self.assertNotIn(banned, names)

    def test_every_reason_code_defined_is_exported(self):
        # REGRESSION: BLOCKED_ALLOCATION_EXHAUSTED was defined in
        # reduction.py and emitted by the planner but never re-exported
        # from the package, so `from sdk.inventory import ...` of the
        # 10th reason code failed. Every BLOCKED_* constant defined in
        # reduction.py must be importable from sdk.inventory.
        from sdk.inventory import reduction as reduction_mod

        defined = {
            n
            for n in dir(reduction_mod)
            if n.startswith("BLOCKED_")
            and isinstance(getattr(reduction_mod, n), str)
        }
        self.assertEqual(len(defined), 10, sorted(defined))
        for name in defined:
            self.assertTrue(
                hasattr(inv, name), f"{name} defined but not exported"
            )
            self.assertIn(name, inv.__all__, f"{name} missing from __all__")
            exported = getattr(inv, name)
            self.assertEqual(exported, getattr(reduction_mod, name))

    def test_all_rows_use_assignable_hints_only(self):
        d = make_design(1)
        i = make_item(10, 1, 5)
        ledger = make_ledger(observed={1: 5})
        plan, _ = plan_for([i], [d], ledger)
        self.assertIn(plan.rows[0].disposition_hint.value, {"KEEP", "USE", "CRAFT", "ALLOCATE"})


class TestMutationAndNetworkIsolation(unittest.TestCase):
    """Planner mutates nothing and performs no I/O."""

    def test_planner_does_not_mutate_inputs(self):
        d = make_design(1)
        i = make_item(10, 1, 5)
        catalog = InventoryCatalog([i], inv.DesignCatalog([d]))
        ctx = PlanningContext(equipped_references={}, room_references={})
        classified = InventoryClassifier(context=ctx.protection_context()).classify(catalog)
        ledger = DemandPlanner().plan(catalog, ctx)
        before_items = catalog.items()
        before_entries = list(ledger.entries)
        before_conditions = list(ledger.conditions)
        before_classified = list(classified)
        ReductionPlanner().plan(classified, ledger)
        self.assertEqual(catalog.items(), before_items)
        self.assertEqual(list(ledger.entries), before_entries)
        self.assertEqual(list(ledger.conditions), before_conditions)
        self.assertEqual(list(classified), before_classified)

    def test_no_network_io_under_socket_block(self):
        def guard(*args, **kwargs):
            raise AssertionError("network I/O attempted from reduction planner")

        original = socket.socket
        socket.socket = guard
        try:
            d = make_design(1)
            i = make_item(10, 1, 5)
            ledger = make_ledger(observed={1: 5})
            plan_for([i], [d], ledger)
        finally:
            socket.socket = original

    def test_package_has_no_mutation_calls_or_client_construction(self):
        banned = (
            "activateItem",
            "consumeItem",
            "sellItem",
            "moveItem",
            "transferItem",
            "deleteItem",
            "markItems",
            "combineItem",
            "rebuildAmmo",
            "Client(",
        )
        for path in sorted(INVENTORY_DIR.glob("*.py")):
            source = path.read_text()
            for token in banned:
                self.assertNotIn(
                    token,
                    source,
                    f"{path.name} references mutation token {token}",
                )

    def test_reduction_module_imports_no_transport_or_io(self):
        source = (INVENTORY_DIR / "reduction.py").read_text()
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        for banned in ("socket", "requests", "urllib", "http", "os", "sdk.client"):
            self.assertNotIn(banned, imports)

    def test_apply_refusal_independent_of_config(self):
        from unittest.mock import MagicMock

        from sdk.commands import CommandRegistry

        client = MagicMock()
        client.settings = {
            "inventory": {
                "enabled": True,
                "apply": True,
                "force": True,
                "allow_mutation": True,
            }
        }
        registry = CommandRegistry(client)
        spec = registry.get("inventory apply")
        self.assertIsNotNone(spec)
        assert spec is not None  # for the type checker
        handler = spec["handler"]
        result_a = handler([])
        client.settings = None
        result_b = handler([])
        self.assertEqual(result_a, result_b)
        self.assertIn("REFUSED", result_a)


class TestSharedRefsAndConflicts(unittest.TestCase):
    """Shared/multi-attach/conflict conditions block exactly the named stacks."""

    def test_condition_parsing_recovers_item_ids(self):
        d = make_design(1)
        i = make_item(10, 1, 5)
        ledger = make_ledger(
            conditions=[
                UnresolvedDemand(
                    EQUIPMENT_SHARED_STACK,
                    "ItemId 10 is referenced by 3 characters; whole stack claimed once",
                    1,
                ),
            ],
            observed={1: 5},
        )
        plan, _ = plan_for([i], [d], ledger)
        self.assertEqual(plan.rows[0].candidate_quantity, 0)

    def test_condition_without_item_id_token_does_not_crash(self):
        d = make_design(1)
        i = make_item(10, 1, 5)
        ledger = make_ledger(
            conditions=[
                UnresolvedDemand(EQUIPMENT_SHARED_STACK, "no id token here", 1),
            ],
            observed={1: 5},
        )
        plan, _ = plan_for([i], [d], ledger)
        # no ItemId recovered -> nothing stack-blocked; row stays candidate
        self.assertEqual(plan.rows[0].candidate_quantity, 5)


class TestLiveShape(unittest.TestCase):
    """Synthetic reproduction of the live 17-eligible / 4-blocked surface."""

    def test_live_shape_reproduction(self):
        # Eligible design with unreserved stock -> candidate == unreserved.
        d = make_design(2, name="Gas", sub_type="")
        i = make_item(10, 2, 25000)
        ledger = make_ledger(observed={2: 25000})
        plan, _ = plan_for([i], [d], ledger)
        row = plan.rows[0]
        self.assertEqual(row.candidate_quantity, 25000)
        self.assertEqual(row.state, STATE_CANDIDATE)

    def test_live_shape_training_blocked(self):
        # Repair Guide: training role, unprotected, unreserved -> blocked.
        d = make_design(359, name="Repair Guide", sub_type="resettraining")
        i = make_item(11, 359, 2)
        ledger = make_ledger(observed={359: 2})
        plan, _ = plan_for([i], [d], ledger)
        row = plan.rows[0]
        self.assertEqual(row.candidate_quantity, 0)
        self.assertIn(BLOCKED_UNSUPPORTED_DEMAND, row.reasons)

    def test_live_shape_ammunition_blocked(self):
        # Ion Core: ammunition material, unprotected, unreserved -> blocked.
        d = make_design(11, name="Ion Core", item_type="missile")
        i = make_item(12, 11, 15)
        ledger = make_ledger(observed={11: 15})
        plan, _ = plan_for([i], [d], ledger)
        self.assertEqual(plan.rows[0].candidate_quantity, 0)
        self.assertIn(BLOCKED_UNSUPPORTED_DEMAND, plan.rows[0].reasons)


if __name__ == "__main__":
    unittest.main()
