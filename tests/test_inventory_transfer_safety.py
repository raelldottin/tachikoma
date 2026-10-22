#!/usr/bin/env python3
"""Task I safety/policy verification lane (t_6a3ef050).

Independent, from-scratch safety proofs over the landed Task I transfer
surface (sdk/inventory/transfer.py + its exports), per the lane card:
"Prove stale/protected/unresolved/unverified cases fail closed and no
GiftItem, network or mutation path becomes reachable."

This lane wrote NO production code. Every probe below was first run
interactively against the live tree, then pinned here as a permanent
regression gate. Probes are derived from the Task I brief, not from the
planner/adversarial lanes' test files.

Axes proven here (each a permanent regression gate):

T1  STALE FAILS CLOSED: a stale source OR stale destination suppresses
    the pairing (matched 0, REASON_FRESH_SNAPSHOTS, per-side freshness
    flags correct); a gate-failing stale pair consumes NO capacity
    (fresh destinations still draw from the full remaining supply);
    the exact STALE_AFTER_SECONDS=86400 boundary holds (age==threshold
    fresh, threshold+1 stale); a malformed or future ``now`` refuses the
    whole plan via TransferPlannerError (never a partial plan).

T2  PROTECTED FAILS CLOSED: a protected or unknown stack of the paired
    design on EITHER side suppresses the pairing even when usable
    supply already excludes protected candidates (REASON_NO_PROTECTION_
    CONFLICT, matched 0) — the fail-closed reading, verified
    independently here.

T3  UNRESOLVED FAILS CLOSED: a design-scoped unresolved condition on
    either side blocks the pairing; an account-global condition does
    NOT block (Task H portfolio precedent); a condition scoped to a
    DIFFERENT design never bleeds into the paired design's proposal.

T4  UNVERIFIED FAILS CLOSED (constant): every proposal across a 16-
    shape portfolio matrix (stale/condition/protected on either side,
    all combinations) carries HOLD_UNVERIFIED_TRANSFERABILITY,
    proposed_quantity 0, actionable False; ELIGIBLE is never assigned;
    REASON_NO_EVIDENCE_PROMOTION is never stamped as an unmet reason
    (structurally satisfied); evidence is never above the
    GIFTITEM_EVIDENCE_TIER floor (weakest-input, never promoted).

T5  NO GIFTITEM PATH: zero GiftItem/SellItem3/Purchase4/MarketService/
    TransferItem captures in the full 6-file, 50,755-record corpus was
    independently re-verified by this lane (binary-safe scan, run
    2026-10-01, results recorded in the lane report); the pin here is
    the code-level invariant the scan grounds: transfer.py carries no
    GiftItem call site, no endpoint string, no checksum, no transport;
    the module's only GiftItem references are the evidence-floor
    constant and documentation strings.

T6  NO NETWORK PATH: transfer.py imports nothing but dataclasses/
    typing/local pure modules (AST-proven); plan_transfers and
    render_transfers_view complete under a blocked socket namespace
    (runtime proof); sdk/client.py never imports sdk.inventory
    (import-statement proof, not substring).

T7  NO MUTATION PATH: no mutation endpoint name appears as a call site
    in any sdk/inventory module; the exported registry registers no
    `transfer` verb (the UX lane wires `inventory transfers` as a
    read-only subcommand under the existing `inventory` verb, importing
    only the pure renderer); `inventory apply` still refuses
    unconditionally (AST: single return, no If, no settings/env
    access) from registry dispatch, TUI loop, and help; the transfer
    planner does not open any path around the refusal (planner emits
    rows, never commands).

T8  FORGED/HAND-BUILT INPUTS FAIL CLOSED: hand-built AccountPortfolio
    with duplicate accounts aborts; a dataclasses.replace-forged
    snapshot (tampered unmet) fails loader re-validation inside
    plan_transfers; non-portfolio inputs, bad stale_after types
    (negative/bool/float/str) all refuse; the actionable=True flag on a
    hand-built portfolio input is ignored.

T9  NON-ACTIONABILITY EVERYWHERE: serialized plans carry actionable
    false and proposed 0 on every row, no ItemId in any output, plan.
    actionable_proposals() structurally empty, replanning never
    mutates the portfolio, and the rendered view carries the pinned
    read-only footer.

All inputs are synthetic. No live Pixel Starships traffic; sockets are
blocked wherever the domain surface is exercised (repo rule: tests mock
all PSS network traffic).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import io
import json
import socket
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import sdk.inventory as inv
from sdk.inventory import (
    ELIGIBLE,
    GIFTITEM_EVIDENCE_TIER,
    HOLD_UNVERIFIED_TRANSFERABILITY,
    REASON_FRESH_SNAPSHOTS,
    REASON_NO_APPLICABLE_UNRESOLVED_CONDITION,
    REASON_NO_EVIDENCE_PROMOTION,
    REASON_NO_PROTECTION_CONFLICT,
    REASON_TRANSFER_ELIGIBILITY_UNPROVEN,
    STALE_AFTER_SECONDS,
    Evidence,
    TransferPlannerError,
    aggregate_snapshots,
    plan_transfers,
    render_transfers_view,
    snapshot_from_dict,
)

# Mutation endpoints no Task I code may reach (Task D-H safety
# inheritance; GiftItem/SellItem3/Purchase4 explicitly forbidden this
# slice per the Task I brief).
FORBIDDEN_ENDPOINTS = (
    "GiftItem",
    "SellItem3",
    "Purchase4",
    "TransferItem",
    "ConsumeItem2",
    "ActivateItem3",
    "ActivateItem4",
    "CombineItem4",
    "DeleteItem3",
    "WearItem",
    "UnequipItems",
    "AddItemIdToRoom",
    "MoveItemIdToRoom3",
    "RemoveItemIdFromRoom",
    "UpdateItemIdsForRoom2",
    "Refill2",
    "RepairBrokenModules",
    "DeleteAmmo",
)

# Strings that would indicate an executable/transport surface in the
# transfer module (call-site scan is allow-nothing: even the words
# must not appear outside documentation).
NETWORK_TOKENS = (
    "requests",
    "urllib",
    "urlopen",
    "socket",
    "http.client",
    "transport",
    "api.pixelstarships.com",
)

CAPTURED = "2026-09-28T00:00:00Z"
NOW = "2026-09-28T06:00:00Z"  # 6h after capture: fresh at 86400s
STALE_CAPTURED = "2026-09-01T00:00:00Z"  # 27 days: stale at 86400s

_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Synthetic snapshot builders (hand-built loader payloads — the same
# fail-closed gates the persisted-file path enforces apply to them)
# ---------------------------------------------------------------------------


def _stack(item_id, design_id, qty, candidate, **overrides):
    base: Dict[str, Any] = {
        "item_id": item_id,
        "item_design_id": design_id,
        "ship_id": 55,
        "quantity": qty,
        "classification": "RESERVED",
        "protected": False,
        "unknown": False,
        "roles": [],
        "protection_reasons": [],
        "classification_reasons": [],
        "evidence": "CAPTURED_NOT_VERIFIED",
        "requested_quantity": 0,
        "reserved_quantity": 0,
        "candidate_quantity": candidate,
        "candidate_state": "CANDIDATE",
        "candidate_reasons": [],
        "candidate_evidence": "CAPTURED_NOT_VERIFIED",
        "disposition_hint": "USE",
    }
    base.update(overrides)
    return base


def _design(design_id, observed, requested=0, stacks=1, **overrides):
    base: Dict[str, Any] = {
        "item_design_id": design_id,
        "item_name": "x",
        "observed_quantity": observed,
        "requested_quantity": requested,
        "reserved_quantity": 0,
        "unmet_quantity": max(0, requested - observed),
        "evidence": "CAPTURED_NOT_VERIFIED",
        "stacks": stacks,
    }
    base.update(overrides)
    return base


def _snap(uid, stacks, designs, conditions=(), captured=CAPTURED):
    return snapshot_from_dict(
        {
            "version": 1,
            "identity": {"user_id": uid, "ship_id": 55, "display_name": "n"},
            "stacks": list(stacks),
            "designs": list(designs),
            "conditions": list(conditions),
            "limitations": [],
            "captured_at": captured,
            "actionable": False,
        }
    )


def _hold(iid, did, qty, requested=0, **overrides):
    """Zero-candidate HOLD stack (loader-legal producer shape)."""
    return _stack(
        iid,
        did,
        qty,
        0,
        requested_quantity=requested,
        candidate_state="HOLD",
        candidate_reasons=["BLOCKED_ZERO_UNRESERVED"],
        disposition_hint="KEEP",
        **overrides,
    )


def _protected(iid, did, qty, requested=0, unknown=False):
    """Protected/unknown HOLD stack (Task D rule-10 coupling)."""
    return _stack(
        iid,
        did,
        qty,
        0,
        requested_quantity=requested,
        classification="PROTECTED",
        protected=True,
        unknown=unknown,
        protection_reasons=[
            "PROTECTED_UNKNOWN_DESIGN" if unknown else "PROTECTED_RARITY"
        ],
        candidate_state="HOLD",
        candidate_reasons=["BLOCKED_PROTECTED_OR_UNKNOWN"],
        disposition_hint="KEEP",
    )


def _free_source(uid=101, qty=5, candidate=5, design_id=501, captured=CAPTURED):
    """Clean supply account: one free candidate stack, no demand."""
    return _snap(
        uid,
        [_stack(1, design_id, qty, candidate)],
        [_design(design_id, qty)],
        captured=captured,
    )


def _deficit_dest(uid=202, observed=1, requested=6, design_id=501, captured=CAPTURED):
    """Clean demand account: holds `observed`, requests `requested`."""
    return _snap(
        uid,
        [_hold(9, design_id, observed, requested=requested)],
        [_design(design_id, observed, requested=requested)],
        captured=captured,
    )


def _clean_pair():
    return aggregate_snapshots([_free_source(), _deficit_dest()])


def _plan(portfolio, now=NOW, **kw):
    return plan_transfers(portfolio, now=now, **kw)


class _SocketBlocked:
    """Block socket construction + create_connection for the duration."""

    def __enter__(self):
        self._orig_socket = socket.socket
        self._orig_cc = socket.create_connection

        def _blocked(*_a, **_k):  # pragma: no cover - assertion path
            raise AssertionError("network attempt inside pure inventory surface")

        socket.socket = _blocked  # ty: ignore[invalid-assignment]
        socket.create_connection = _blocked  # ty: ignore[invalid-assignment]
        return self

    def __exit__(self, *exc):
        socket.socket = self._orig_socket
        socket.create_connection = self._orig_cc
        return False


# ---------------------------------------------------------------------------
# T1: stale fails closed
# ---------------------------------------------------------------------------


class TestStaleFailsClosed(unittest.TestCase):
    """Stale snapshots never yield a positive pairing, and a stale pair
    consumes no arithmetic capacity a fresh pairing could use."""

    def test_stale_destination_suppresses_pairing(self):
        pf = aggregate_snapshots(
            [_free_source(), _deficit_dest(captured=STALE_CAPTURED)]
        )
        plan = _plan(pf)
        self.assertEqual(len(plan.proposals), 1)
        row = plan.proposals[0]
        self.assertEqual(row.matched_quantity, 0)
        self.assertTrue(row.source_fresh)
        self.assertFalse(row.destination_fresh)
        self.assertIn(REASON_FRESH_SNAPSHOTS, row.reasons)
        self.assertIn(REASON_TRANSFER_ELIGIBILITY_UNPROVEN, row.reasons)

    def test_stale_source_suppresses_pairing(self):
        pf = aggregate_snapshots(
            [_free_source(captured=STALE_CAPTURED), _deficit_dest()]
        )
        plan = _plan(pf)
        row = plan.proposals[0]
        self.assertEqual(row.matched_quantity, 0)
        self.assertFalse(row.source_fresh)
        self.assertTrue(row.destination_fresh)
        self.assertIn(REASON_FRESH_SNAPSHOTS, row.reasons)

    def test_stale_pair_consumes_no_capacity(self):
        # One source (supply 10), three destinations (unmet 5 each):
        # the FIRST (lowest user_id) is stale. It must consume nothing,
        # so the two fresh destinations still see the full supply
        # (5 + 5 = 10; nothing is reserved by the stale pairing).
        src = _free_source(qty=10, candidate=10)
        stale_dst = _deficit_dest(uid=202, captured=STALE_CAPTURED)
        fresh_dst = _deficit_dest(uid=303)
        fresh_dst2 = _deficit_dest(uid=404)
        plan = _plan(aggregate_snapshots([src, stale_dst, fresh_dst, fresh_dst2]))
        by_dest = {p.destination_user_id: p for p in plan.proposals}
        self.assertEqual(by_dest[202].matched_quantity, 0)
        self.assertEqual(by_dest[202].reasons.count(REASON_FRESH_SNAPSHOTS), 1)
        self.assertEqual(by_dest[303].matched_quantity, 5)
        self.assertEqual(by_dest[404].matched_quantity, 5)
        # The stale pairing reserved no capacity: the fresh pairings
        # consumed the source's ENTIRE usable supply.
        self.assertEqual(
            by_dest[303].matched_quantity + by_dest[404].matched_quantity, 10
        )

    def test_boundary_age_equal_to_threshold_is_fresh(self):
        # captured 2026-09-27T06:00:00Z, now 2026-09-28T06:00:00Z -> age 86400
        boundary = "2026-09-27T06:00:00Z"
        pf = aggregate_snapshots(
            [_free_source(captured=boundary), _deficit_dest(captured=boundary)]
        )
        plan = _plan(pf)
        row = plan.proposals[0]
        self.assertTrue(row.source_fresh and row.destination_fresh)
        self.assertNotIn(REASON_FRESH_SNAPSHOTS, row.reasons)

    def test_boundary_one_second_past_threshold_is_stale(self):
        boundary = "2026-09-27T05:59:59Z"  # age 86401 > 86400
        pf = aggregate_snapshots(
            [_free_source(captured=boundary), _deficit_dest(captured=boundary)]
        )
        plan = _plan(pf)
        row = plan.proposals[0]
        self.assertFalse(row.source_fresh or row.destination_fresh)
        self.assertIn(REASON_FRESH_SNAPSHOTS, row.reasons)

    def test_default_threshold_is_86400(self):
        self.assertEqual(STALE_AFTER_SECONDS, 86400)

    def test_malformed_now_refuses_whole_plan(self):
        pf = _clean_pair()
        for bad in ("not-a-date", "2026-13-40T99:99:99Z", "", "2026-09-28 06:00:00"):
            with self.assertRaises(TransferPlannerError, msg=repr(bad)):
                _plan(pf, now=bad)

    def test_future_now_refuses_whole_plan(self):
        # now BEFORE capture is a contradiction; the plan must refuse.
        pf = _clean_pair()
        with self.assertRaises(TransferPlannerError):
            _plan(pf, now="2020-01-01T00:00:00Z")

    def test_renderer_refuses_on_malformed_now(self):
        a, b = _free_source(), _deficit_dest()
        with self.assertRaises(TransferPlannerError):
            render_transfers_view([a, b], now="garbage")


# ---------------------------------------------------------------------------
# T2: protected/unknown fail closed
# ---------------------------------------------------------------------------


class TestProtectedFailsClosed(unittest.TestCase):
    """Any protected/unknown stack of the paired design on EITHER side
    suppresses the pairing — the fail-closed reading — even though the
    planner's usable-supply arithmetic already excludes protected
    candidates. Verified here from first principles."""

    def test_protected_stack_on_source_blocks_despite_free_candidates(self):
        # Source holds a free candidate stack (candidates 4) AND a
        # protected stack of the same design. Supply arithmetic would
        # allow 4; protection must suppress the whole pairing.
        src = _snap(
            101,
            [_stack(1, 501, 5, 4), _protected(2, 501, 3)],
            [_design(501, 8, stacks=2)],
        )
        dst = _deficit_dest()
        plan = _plan(aggregate_snapshots([src, dst]))
        row = plan.proposals[0]
        self.assertEqual(row.matched_quantity, 0)
        self.assertIn(REASON_NO_PROTECTION_CONFLICT, row.reasons)

    def test_unknown_stack_on_source_blocks(self):
        src = _snap(
            101,
            [_stack(1, 501, 5, 4), _protected(2, 501, 3, unknown=True)],
            [_design(501, 8, stacks=2)],
        )
        dst = _deficit_dest()
        plan = _plan(aggregate_snapshots([src, dst]))
        self.assertEqual(plan.proposals[0].matched_quantity, 0)
        self.assertIn(REASON_NO_PROTECTION_CONFLICT, plan.proposals[0].reasons)

    def test_protected_stack_on_destination_blocks(self):
        # Destination holds a protected stack of the design it is short
        # of: receiving it is a protection conflict.
        dst = _snap(
            202,
            [_protected(9, 501, 1, requested=6)],
            [_design(501, 1, requested=6)],
        )
        plan = _plan(aggregate_snapshots([_free_source(), dst]))
        row = plan.proposals[0]
        self.assertEqual(row.matched_quantity, 0)
        self.assertIn(REASON_NO_PROTECTION_CONFLICT, row.reasons)

    def test_clean_pair_has_no_protection_reason(self):
        plan = _plan(_clean_pair())
        self.assertNotIn(
            REASON_NO_PROTECTION_CONFLICT, plan.proposals[0].reasons
        )


# ---------------------------------------------------------------------------
# T3: unresolved conditions fail closed (design-scoped only)
# ---------------------------------------------------------------------------


class TestUnresolvedFailsClosed(unittest.TestCase):
    def test_design_scoped_condition_on_source_blocks(self):
        src = _snap(
            101,
            [_stack(1, 501, 5, 5)],
            [_design(501, 5)],
            conditions=[{"code": "SOURCE_UNAVAILABLE", "detail": "y", "design_id": 501}],
        )
        plan = _plan(aggregate_snapshots([src, _deficit_dest()]))
        row = plan.proposals[0]
        self.assertEqual(row.matched_quantity, 0)
        self.assertIn(REASON_NO_APPLICABLE_UNRESOLVED_CONDITION, row.reasons)

    def test_design_scoped_condition_on_destination_blocks(self):
        dst = _snap(
            202,
            [_hold(9, 501, 1, requested=6)],
            [_design(501, 1, requested=6)],
            conditions=[{"code": "DEMAND_EXCEEDS_SUPPLY", "detail": "d", "design_id": 501}],
        )
        plan = _plan(aggregate_snapshots([_free_source(), dst]))
        row = plan.proposals[0]
        self.assertEqual(row.matched_quantity, 0)
        self.assertIn(REASON_NO_APPLICABLE_UNRESOLVED_CONDITION, row.reasons)

    def test_account_global_condition_does_not_block(self):
        # Account-global conditions are annotations (Task H portfolio
        # precedent): they must not gate the pairing.
        src = _snap(
            101,
            [_stack(1, 501, 5, 5)],
            [_design(501, 5)],
            conditions=[{"code": "SOURCE_UNSUPPORTED", "detail": "x", "design_id": None}],
        )
        plan = _plan(aggregate_snapshots([src, _deficit_dest()]))
        row = plan.proposals[0]
        self.assertNotIn(REASON_NO_APPLICABLE_UNRESOLVED_CONDITION, row.reasons)
        # and the pairing still happens arithmetically
        self.assertEqual(row.matched_quantity, 5)

    def test_condition_on_other_design_does_not_bleed(self):
        # Source has an unresolved condition on design 502; the 501
        # pairing must be unaffected (no cross-design provenance bleed).
        src = _snap(
            101,
            [_stack(1, 501, 5, 5), _hold(2, 502, 2)],
            [_design(501, 5), _design(502, 2)],
            conditions=[{"code": "SOURCE_UNAVAILABLE", "detail": "y", "design_id": 502}],
        )
        plan = _plan(aggregate_snapshots([src, _deficit_dest()]))
        rows_501 = [p for p in plan.proposals if p.item_design_id == 501]
        self.assertTrue(rows_501)
        self.assertNotIn(
            REASON_NO_APPLICABLE_UNRESOLVED_CONDITION, rows_501[0].reasons
        )
        self.assertEqual(rows_501[0].matched_quantity, 5)


# ---------------------------------------------------------------------------
# T4: unverified transferability fails closed (constant hold)
# ---------------------------------------------------------------------------


class TestUnverifiedFailsClosed(unittest.TestCase):
    """The constant-hold proof across an exhaustive shape matrix: even
    when every gate requirement passes, transferability is unproven
    (GiftItem uncaptured), so the hold is stamped unconditionally."""

    def _matrix_plans(self):
        import itertools

        plans = []
        for src_stale, dst_stale, cond, prot in itertools.product(
            (False, True), repeat=4
        ):
            captured = STALE_CAPTURED if src_stale else CAPTURED
            src_stacks = [_stack(1, 501, 5, 4)]
            if prot:
                src_stacks.append(_protected(2, 501, 3))
            src = _snap(
                101,
                src_stacks,
                [_design(501, 8 if prot else 5, stacks=2 if prot else 1)],
                conditions=(
                    [{"code": "SOURCE_UNAVAILABLE", "detail": "d", "design_id": 501}]
                    if cond
                    else ()
                ),
                captured=captured,
            )
            dst_stacks = [
                _protected(9, 501, 1, requested=6) if prot else _hold(9, 501, 1, requested=6)
            ]
            dst = _snap(
                202,
                dst_stacks,
                [_design(501, 1, requested=6)],
                captured=STALE_CAPTURED if dst_stale else CAPTURED,
            )
            plan = _plan(aggregate_snapshots([src, dst]))
            plans.append((plan, (src_stale, dst_stale, cond, prot)))
        return plans

    def test_every_matrix_shape_holds_unverified(self):
        for plan, shape in self._matrix_plans():
            self.assertTrue(plan.proposals, f"no proposals for shape {shape}")
            for row in plan.proposals:
                self.assertEqual(
                    row.eligibility_state, HOLD_UNVERIFIED_TRANSFERABILITY,
                    f"shape {shape}",
                )
                self.assertEqual(row.proposed_quantity, 0, f"shape {shape}")
                self.assertFalse(row.actionable, f"shape {shape}")
                self.assertIn(REASON_TRANSFER_ELIGIBILITY_UNPROVEN, row.reasons,
                              f"shape {shape}")

    def test_eligible_never_assigned_anywhere(self):
        for plan, _shape in self._matrix_plans():
            for row in plan.proposals:
                self.assertNotEqual(row.eligibility_state, ELIGIBLE)

    def test_no_evidence_promotion_reason_never_unmet(self):
        # Requirement 7 is structurally satisfied by weakest-input
        # computation: it must never appear as an unmet reason.
        for plan, _shape in self._matrix_plans():
            for row in plan.proposals:
                self.assertNotIn(REASON_NO_EVIDENCE_PROMOTION, row.reasons)

    def test_evidence_never_above_giftitem_floor(self):
        order = list(Evidence)
        floor_index = order.index(Evidence(GIFTITEM_EVIDENCE_TIER.value))
        for plan, _shape in self._matrix_plans():
            for row in plan.proposals:
                tier_index = order.index(Evidence(row.evidence))
                self.assertGreaterEqual(
                    tier_index, floor_index,
                    f"evidence {row.evidence} promoted above "
                    f"{GIFTITEM_EVIDENCE_TIER.value}",
                )

    def test_giftitem_floor_is_statically_discovered(self):
        self.assertEqual(GIFTITEM_EVIDENCE_TIER, Evidence.STATICALLY_DISCOVERED)

    def test_perfect_inputs_still_hold(self):
        # Everything passes every structural gate; the ONLY reason is
        # the unverified-transferability constant.
        plan = _plan(_clean_pair())
        row = plan.proposals[0]
        self.assertEqual(
            row.reasons, (REASON_TRANSFER_ELIGIBILITY_UNPROVEN,)
        )
        # matched is informational only
        self.assertEqual(row.matched_quantity, 5)
        self.assertEqual(row.proposed_quantity, 0)
        self.assertFalse(row.actionable)


# ---------------------------------------------------------------------------
# T5: no GiftItem path (code-level invariants grounded by the corpus scan)
# ---------------------------------------------------------------------------


class TestNoGiftItemPath(unittest.TestCase):
    """The corpus evidence (0 GiftItem/SellItem3/Purchase4/MarketService/
    TransferItem captures across all 6 files, 50,755 records; this lane's
    independent binary-safe re-scan, 2026-10-01) grounds the constant
    hold. These pins keep the code consistent with that evidence."""

    def test_transfer_module_has_no_giftitem_call_site(self):
        import sdk.inventory.transfer as transfer_mod

        tree = ast.parse(inspect.getsource(transfer_mod))
        for node in ast.walk(tree):
            # No call whose name mentions a forbidden endpoint
            if isinstance(node, ast.Call):
                name = None
                func = node.func
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name:
                    for endpoint in FORBIDDEN_ENDPOINTS:
                        self.assertNotIn(
                            endpoint.lower(),
                            name.lower(),
                            f"forbidden call site {name!r}",
                        )

    def test_transfer_module_giftitem_references_carry_no_protocol_payload(self):
        import sdk.inventory.transfer as transfer_mod

        tree = ast.parse(inspect.getsource(transfer_mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if "GiftItem" in value or "giftitem" in value.lower():
                    # Documentation mentions are expected (the capture
                    # gap is the named blocker, and the renderer quotes
                    # the truncated '?itemId=' fragment). What must
                    # NEVER exist is protocol payload: a full endpoint
                    # path, a host, a checksum formula, or a
                    # recipient-parameter template.
                    self.assertNotIn("/ItemService/GiftItem", value)
                    self.assertNotIn("pixelstarships", value.lower())
                    self.assertNotIn("md5", value.lower())
                    self.assertNotIn("sha", value.lower())
                    self.assertNotIn("touserid", value.lower())
                    self.assertNotIn("frienduserid", value.lower())

    def test_no_forbidden_endpoint_string_in_inventory_source(self):
        # Call-site scan across every sdk/inventory module: a forbidden
        # endpoint name may appear only in docstring/comment text, and
        # GiftItem may never appear inside a string that also carries a
        # URL scheme, path prefix, or checksum preimage.
        import pathlib

        for path in pathlib.Path(inv.__file__).parent.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    value = node.value
                    if "GiftItem" in value:
                        self.assertNotIn("/ItemService/", value)
                        self.assertNotIn("http", value.lower())

    def test_planner_emits_no_endpoint_no_route_no_command(self):
        plan = _plan(_clean_pair())
        blob = json.dumps(plan.to_dicts())
        for word in ("endpoint", "route", "command", "url", "GiftItem",
                     "SellItem3", "Purchase4", "checksum", "execute"):
            self.assertNotIn(word.lower(), json.dumps(blob).lower())


# ---------------------------------------------------------------------------
# T6: no network path
# ---------------------------------------------------------------------------


class TestNoNetworkPath(unittest.TestCase):
    def test_transfer_module_imports_are_pure_only(self):
        import sdk.inventory.transfer as transfer_mod

        tree = ast.parse(inspect.getsource(transfer_mod))
        allowed_roots = {"dataclasses", "typing", "__future__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed_roots)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import: must stay inside sdk.inventory
                    self.assertEqual(node.level, 1)
                else:
                    root = (node.module or "").split(".")[0]
                    self.assertIn(root, allowed_roots)

    def test_transfer_module_source_free_of_network_tokens(self):
        import sdk.inventory.transfer as transfer_mod

        source = inspect.getsource(transfer_mod)
        for token in NETWORK_TOKENS:
            self.assertNotIn(token, source)

    def test_planner_and_renderer_run_under_socket_block(self):
        a, b = _free_source(), _deficit_dest()
        with _SocketBlocked():
            plan = _plan(aggregate_snapshots([a, b]))
            render_transfers_view([a, b], now=NOW)
        self.assertTrue(plan.proposals)

    def test_client_never_imports_inventory(self):
        # Import statements only (substring scan would false-positive
        # on the docstring/log line that MENTIONS sdk.inventory).
        import sdk.client as client_mod

        tree = ast.parse(inspect.getsource(client_mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(
                        alias.name.startswith("sdk.inventory"),
                        f"client.py imports {alias.name}",
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertFalse(
                    module.startswith("sdk.inventory"),
                    f"client.py imports from {module}",
                )

    def test_commands_imports_inventory_lazily_and_never_transfer(self):
        # commands.py wires portfolio views only via deferred imports;
        # the transfer planner stays unreachable from the command
        # surface — the UX lane (t_7ed909c9) imports ONLY the pure
        # renderer render_transfers_view, never plan_transfers or any
        # transfer.* symbol.
        import sdk.commands as commands_mod

        tree = ast.parse(inspect.getsource(commands_mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "sdk.inventory":
                for alias in node.names:
                    self.assertFalse(
                        alias.name.startswith("transfer"),
                        f"commands.py imports {alias.name} from sdk.inventory",
                    )
                    self.assertFalse(
                        alias.name.startswith("plan_transfers"),
                        "commands.py imports plan_transfers",
                    )


# ---------------------------------------------------------------------------
# T7: no mutation path
# ---------------------------------------------------------------------------


class TestNoMutationPath(unittest.TestCase):
    def _registry(self):
        from sdk.commands import create_command_registry

        return create_command_registry(MagicMock())

    def test_no_transfer_verb_registered(self):
        registry = self._registry()
        for name in registry.list_commands():
            self.assertNotIn("transfer", name)

    def test_apply_refusal_single_unconditional_return(self):
        import sdk.commands as commands_mod

        tree = ast.parse(inspect.getsource(commands_mod.CommandRegistry))
        method = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_cmd_inventory_apply":
                method = node
                break
        self.assertIsNotNone(method)
        assert method is not None  # narrowing for the static checker
        returns = [n for n in ast.walk(method) if isinstance(n, ast.Return)]
        self.assertEqual(len(returns), 1)
        assert returns[0].value is not None
        self.assertIn("REFUSED", ast.literal_eval(returns[0].value))
        # No conditional, no settings/env access, body is docstring + return.
        self.assertEqual(len(method.body), 2)
        for node in ast.walk(method):
            self.assertNotIsInstance(node, ast.If)
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ("settings", "environ", "getenv"))

    def test_apply_refuses_from_registry_dispatch(self):
        registry = self._registry()
        handler = registry.get("inventory apply")["handler"]
        for args in ([], ["--force"], ["--yes"], ["501", "5"], ["transfer"]):
            out = handler(args)
            self.assertIn("REFUSED", out, args)

    def test_apply_refuses_from_tui_loop(self):
        from sdk.commands import create_command_registry
        from sdk.tui import TUI

        tui = TUI(MagicMock())
        tui.client = MagicMock()
        tui.client.accessToken = "t"
        tui.registry = create_command_registry(tui.client)
        tui.running = True
        buf = io.StringIO()
        with patch("sdk.tui.input", side_effect=["inventory apply", "exit"]), redirect_stdout(buf):
            tui._command_loop()
        out = buf.getvalue()
        self.assertIn("REFUSED", out)
        self.assertNotIn("Unknown inventory subcommand", out)

    def test_help_shows_apply_as_unverified_refusal(self):
        registry = self._registry()
        out = registry._cmd_help(["inventory", "apply"])
        self.assertIn("REFUSED", out)
        self.assertIn("(unverified)", out)

    def test_apply_refusal_survives_armed_config_and_env(self):
        from sdk.commands import create_command_registry

        client = MagicMock()
        client.settings = {
            "inventory": {
                "enabled": True,
                "snapshots_dir": "snapshots",
                "starbux_cap_per_run": 999,
            }
        }
        registry = create_command_registry(client)
        with patch.dict("os.environ", {"ENABLE_MUTATION": "1", "INVENTORY_ENABLED": "1"}):
            cmd = registry.get("inventory apply")
            self.assertIsNotNone(cmd)
            assert cmd is not None
            out = cmd["handler"]([])
        self.assertIn("REFUSED", out)

    def test_apply_not_verified_flag(self):
        registry = self._registry()
        self.assertFalse(registry.is_verified("inventory apply"))

    def test_no_mutation_endpoint_importable_from_inventory_exports(self):
        # The public export surface carries no mutation capability:
        # no callable exported from sdk.inventory may construct or
        # dispatch a request. (Names only; AST-level call-site checks
        # live in T5/T6.)
        exported = set(inv.__all__)
        for name in exported:
            self.assertFalse(name.endswith(("_executor", "_reconciler", "_retry")))

    def test_inventory_module_ast_has_no_client_construction(self):
        # Across ALL sdk/inventory modules: no `Client(...)` call and
        # no attribute named `execute` (the executor vocabulary).
        import pathlib

        for path in pathlib.Path(inv.__file__).parent.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.id if isinstance(func, ast.Name) else (
                        func.attr if isinstance(func, ast.Attribute) else None
                    )
                    if name:
                        self.assertNotEqual(name, "Client", f"{path}: Client() constructed")
                        self.assertNotIn("execute", name.lower(), f"{path}: execute call")


# ---------------------------------------------------------------------------
# T8: forged / hand-built inputs fail closed
# ---------------------------------------------------------------------------


class TestForgedInputsFailClosed(unittest.TestCase):
    def test_hand_built_portfolio_duplicate_accounts_abort(self):
        a = _free_source()
        hand = inv.AccountPortfolio(accounts=(a, a), rows=(), actionable=False)
        with self.assertRaises(TransferPlannerError):
            _plan(hand)

    def test_forged_snapshot_fails_revalidation_before_or_inside_planner(self):
        # Tamper the loaded snapshot's unmet arithmetic; the loader
        # re-validation gate catches it — inside aggregate_snapshots
        # (before the planner) or, if entered directly, inside
        # plan_transfers. Either refuse point proves no forged row can
        # reach the matching arithmetic.
        b = _deficit_dest()
        forged = dataclasses.replace(
            b, designs=(dataclasses.replace(b.designs[0], unmet_quantity=99),)
        )
        with self.assertRaises((TransferPlannerError, inv.PortfolioError)):
            _plan(aggregate_snapshots([_free_source(), forged]))

    def test_non_portfolio_inputs_rejected(self):
        a = _free_source()
        for bad in (None, [a], "x", 42, (a,), {"accounts": [a]}):
            with self.assertRaises(TransferPlannerError, msg=repr(type(bad))):
                _plan(bad)  # type: ignore[arg-type]

    def test_non_snapshot_account_rejected(self):
        a = _free_source()
        hand = inv.AccountPortfolio(
            accounts=(a, {"bogus": 1}),  # ty: ignore[invalid-argument-type]
            rows=(),
            actionable=False,
        )
        with self.assertRaises(TransferPlannerError):
            _plan(hand)

    def test_bad_stale_after_types_rejected(self):
        pf = _clean_pair()
        for bad in (-1, True, False, 1.5, "86400", None):
            with self.assertRaises(TransferPlannerError, msg=repr(bad)):
                _plan(pf, stale_after=bad)  # type: ignore[arg-type]

    def test_actionable_true_flag_on_hand_built_portfolio_ignored(self):
        a, b = _free_source(), _deficit_dest()
        hand = inv.AccountPortfolio(accounts=(a, b), rows=(), actionable=True)
        plan = _plan(hand)
        self.assertFalse(plan.actionable)
        for row in plan.proposals:
            self.assertFalse(row.actionable)
            self.assertEqual(row.proposed_quantity, 0)

    def test_planner_input_never_mutated_by_planning(self):
        pf = _clean_pair()
        before = pf.to_dicts()
        _plan(pf)
        _plan(pf, stale_after=100)
        self.assertEqual(pf.to_dicts(), before)


# ---------------------------------------------------------------------------
# T9: non-actionability everywhere (serialized + rendered)
# ---------------------------------------------------------------------------


class TestNonActionabilityEverywhere(unittest.TestCase):
    def test_serialized_plan_actionable_false_proposed_zero(self):
        plan = _plan(_clean_pair())
        for row in plan.to_dicts():
            self.assertIs(row["actionable"], False)
            self.assertEqual(row["proposed_quantity"], 0)
            self.assertEqual(
                row["eligibility_state"], HOLD_UNVERIFIED_TRANSFERABILITY
            )

    def test_no_item_id_in_serialized_plan(self):
        plan = _plan(_clean_pair())
        blob = json.dumps(plan.to_dicts())
        self.assertNotIn('"item_id"', blob)

    def test_actionable_proposals_structurally_empty(self):
        plan = _plan(_clean_pair())
        self.assertEqual(plan.actionable_proposals(), ())

    def test_rendered_view_carries_readonly_pins(self):
        a, b = _free_source(), _deficit_dest()
        out = render_transfers_view([a, b], now=NOW)
        self.assertIn("actionable=false", out)
        self.assertIn("READ-ONLY", out)
        self.assertIn("HOLD_UNVERIFIED_TRANSFERABILITY", out)
        self.assertIn("GiftItem", out)  # named blocker documented
        self.assertIn("'inventory apply' remains refused", out)

    def test_rendered_view_has_no_item_id(self):
        a, b = _free_source(), _deficit_dest()
        out = render_transfers_view([a, b], now=NOW)
        # item_design_id is the join key and MUST appear; bare item_id
        # (instance identity) must not.
        self.assertNotIn("item_id=", out)
        self.assertIn("design 501", out)

    def test_rendered_view_empty_state(self):
        out = render_transfers_view([], now=NOW)
        self.assertIn("No account snapshots found", out)

    def test_rendered_view_stale_marker_visible(self):
        pf_src = _free_source()
        dst = _deficit_dest(captured=STALE_CAPTURED)
        out = render_transfers_view([pf_src, dst], now=NOW)
        self.assertIn("STALE", out)

    def test_replanning_is_deterministic(self):
        pf = _clean_pair()
        first = _plan(pf)
        second = _plan(pf)
        self.assertEqual(first.to_dicts(), second.to_dicts())


if __name__ == "__main__":
    unittest.main()
