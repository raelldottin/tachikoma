#!/usr/bin/env python3
"""Task H safety/policy verification lane (tests/test_inventory_snapshot_safety.py).

Safety-lane proof file for the Task H snapshot + portfolio surface, per the
swarm brief's SAFETY / POLICY VERIFICATION axis and the lane card
t_f4b6027d ("Prove no secrets, transfer planning, network behavior or
mutation reachability; verify apply refusal and fail-closed cross-account
behavior").

Axes proven here (each a permanent regression gate):

S1  SECRETS: a snapshot built from the REAL Task D/F/G pipeline contains
    no secret keys at any depth and no token-shaped values; the loader
    rejects every FORBIDDEN_SNAPSHOT_KEYS name wherever it appears
    (root, nested dict, list-of-dicts, nested-in-conditions) fail-closed.

S2  NO TRANSFER PLANNING: no donor/recipient/matching structure exists;
    serialized snapshots and portfolios contain none of the transfer
    vocabulary; deficit rows are per-account observations only; candidate
    totals never cross account boundaries into a "shippable" quantity.

S3  NO NETWORK / NO CLOCK / NO FILESYSTEM (purity): building, loading and
    aggregating snapshots over a socket-blocked namespace performs zero
    network I/O; static scan keeps snapshot.py/portfolio.py free of
    network/config-bypass tokens; no import-time I/O either (subprocess
    probe under a socket guard).

S4  NO MUTATION REACHABILITY: static scan — snapshot.py/portfolio.py
    never construct a Client, never call transport, never reach any
    forbidden endpoint; runtime probe — the full build/aggregate surface
    under a blocked-Client namespace never touches one.

S5  APPLY REFUSAL: `inventory apply` refuses unconditionally from every
    dispatch surface (registry lookup, TUI loop, help), for any args,
    with any settings object shape; the refusal text names the gate.

S6  FAIL-CLOSED CROSS-ACCOUNT: a duplicate account (same user_id, two
    different snapshots OR the same object twice) or a non-snapshot
    input aborts the ENTIRE aggregation via PortfolioError — no partial
    portfolio is ever returned; ItemIds are never compared across
    accounts (same ItemId in two accounts is legal and stays separate);
    actionable=true payloads are rejected at load; every loaded/derived
    surface reports actionable=false and actionable_rows()==().

S7  DETERMINISM + ISOLATION: byte-identical JSON for identical inputs
    (any input order); to_dict() returns fresh containers (mutating the
    returned dict cannot corrupt the snapshot); frozen dataclasses.

All inputs are synthetic. No live Pixel Starships traffic; sockets are
blocked wherever the domain surface is exercised (repo rule: tests mock
all PSS network traffic).
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import socket
import subprocess
import sys
import textwrap
import unittest
from contextlib import redirect_stdout
from typing import Any, Dict, List, cast
from unittest.mock import MagicMock, patch

import sdk.inventory as inv
from sdk.inventory import (
    FORBIDDEN_SNAPSHOT_KEYS,
    InventoryCatalog,
    Item,
    ItemDesign,
    PlanningContext,
    PortfolioError,
    SnapshotAccountIdentity,
    SnapshotError,
    aggregate_snapshots,
    build_snapshot,
    snapshot_from_dict,
    snapshot_from_json,
)

# The mutation-capable endpoints no Task H code may reach (Task D/E/F/G/H
# safety inheritance; extended with the exact names from the swarm brief).
FORBIDDEN_ENDPOINTS = (
    "ConsumeItem2",
    "ActivateItem3",
    "ActivateItem4",
    "CombineItem4",
    "DeleteItem3",
    "GiftItem",
    "SellItem3",
    "Purchase4",
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

# Transfer-planning vocabulary that must NEVER appear in serialized
# snapshot/portfolio output (lowercased comparison).
TRANSFER_VOCABULARY = (
    "donor",
    "recipient",
    "transfer",
    "giftitem",
    "sellitem3",
    "purchase4",
    "marketplace_transaction",
    "destination",
    "proposal",
    "ship_to",
    "send_to",
    "move_to_account",
)

# Extra secret-shape names beyond the loader's list (defense-in-depth:
# loader covers the capture vocabulary; this scan adds common aliases).
EXTRA_SECRET_KEYS = (
    "authentication",
    "authorization",
    "session_key",
    "sessionkey",
    "jwt",
    "bearer",
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_design(
    design_id,
    name="Widget",
    sub_type="",
    item_type="",
    rarity="common",
    tags=(),
    ingredients=(),
    module_type=None,
    training_design_id=None,
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
        training_design_id=training_design_id,
    )


def make_catalog(rows, designs):
    """rows: (item_id, design_id, qty)."""
    items = [
        Item(
            item_id=iid,
            ship_id=55,
            item_design_id=did,
            quantity=qty,
            is_new=False,
            skin_key=None,
        )
        for iid, did, qty in rows
    ]
    return InventoryCatalog(items, inv.DesignCatalog(designs))


def build_dfg(rows, designs, equipped=None, rooms=None):
    """Run the real Task D + F + G pipeline over synthetic inputs."""
    catalog = make_catalog(rows, designs)
    ctx = PlanningContext(
        equipped_references=equipped if equipped is not None else {},
        room_references=rooms if rooms is not None else {},
    )
    classified = inv.InventoryClassifier(context=ctx.protection_context()).classify(catalog)
    ledger = inv.DemandPlanner().plan(catalog, ctx)
    reduction = inv.ReductionPlanner().plan(classified, ledger)
    return catalog, classified, ledger, reduction


def make_identity(user_id=101, ship_id=55, name="alpha"):
    return SnapshotAccountIdentity(user_id=user_id, ship_id=ship_id, display_name=name)


def real_pipeline_snapshot(
    user_id=101,
    name="alpha",
    rows=None,
    designs=None,
    equipped=None,
    captured_at="2026-09-28T00:00:00Z",
):
    designs = designs if designs is not None else [make_design(501, "Steel Plate")]
    rows = rows if rows is not None else [(1, 501, 5)]
    _, classified, ledger, reduction = build_dfg(rows, designs, equipped=equipped)
    return build_snapshot(
        make_identity(user_id=user_id, name=name),
        classified,
        ledger,
        reduction,
        captured_at=captured_at,
    )


def stack_row(
    item_id=1,
    design_id=501,
    qty=5,
    candidate=2,
    evidence="CAPTURED_NOT_VERIFIED",
    **overrides,
):
    """Hand-built stack row for direct loader-input tests."""
    base = {
        "item_id": item_id,
        "item_design_id": design_id,
        "ship_id": 55,
        "quantity": qty,
        # Producer-possible bucket only: the classifier emits
        # PROTECTED/RESERVED/CONSUMABLE/UNKNOWN (adversarial-lane
        # vocabulary gate, pinned §7a).
        "classification": "RESERVED",
        "protected": False,
        "unknown": False,
        "roles": [],
        "protection_reasons": [],
        "classification_reasons": [],
        "evidence": evidence,
        "requested_quantity": 0,
        "reserved_quantity": 0,
        "candidate_quantity": candidate,
        "candidate_state": "CANDIDATE",
        "candidate_reasons": [],
        "candidate_evidence": evidence,
        # CANDIDATE rows hint USE/CRAFT/ALLOCATE (KEEP is HOLD-only:
        # producer-invariant coupling gate).
        "disposition_hint": "USE",
    }
    base.update(overrides)
    return base


def design_row(design_id=501, name="Steel Plate", observed=5, **overrides):
    base = {
        "item_design_id": design_id,
        "item_name": name,
        "observed_quantity": observed,
        "requested_quantity": 0,
        "reserved_quantity": 0,
        "unmet_quantity": 0,
        "evidence": "CAPTURED_NOT_VERIFIED",
        "stacks": 1,
    }
    base.update(overrides)
    return base


def valid_payload(user_id=101, with_stack=True):
    payload: Dict[str, Any] = {
        "version": 1,
        "identity": {"user_id": user_id, "ship_id": 55, "display_name": "alpha"},
        "stacks": [stack_row()] if with_stack else [],
        "designs": [design_row()] if with_stack else [],
        "conditions": [],
        "limitations": [],
        "captured_at": "2026-09-28T00:00:00Z",
        "actionable": False,
    }
    return payload


def dispatch(registry, line):
    """Mirror the TUI's longest-registered-prefix dispatch."""
    parts = line.split()
    name, args = parts[0].lower(), parts[1:]
    for width in range(min(len(args), 1), -1, -1):
        cmd = registry.get(" ".join([name] + args[:width]))
        if cmd:
            return cmd["handler"](args[width:])
    return None


def deep_key_scan(obj, forbidden, path="$") -> List[str]:
    """Collect any forbidden key name at any dict depth / list position."""
    hits: List[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in forbidden:
                hits.append(f"{path}.{key}")
            hits.extend(deep_key_scan(value, forbidden, f"{path}.{key}"))
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            hits.extend(deep_key_scan(value, forbidden, f"{path}[{i}]"))
    return hits


class TestSnapshotSecrets(unittest.TestCase):
    """S1: no secret ever persists; loader rejects them at any depth."""

    def test_real_pipeline_snapshot_has_no_secret_keys_at_any_depth(self):
        snap = real_pipeline_snapshot()
        payload = json.loads(snap.to_json())
        forbidden = {k.lower() for k in FORBIDDEN_SNAPSHOT_KEYS} | {
            k.lower() for k in EXTRA_SECRET_KEYS
        }
        self.assertEqual(deep_key_scan(payload, forbidden), [])

    def test_real_pipeline_snapshot_identity_block_is_non_secret_only(self):
        snap = real_pipeline_snapshot()
        identity = json.loads(snap.to_json())["identity"]
        self.assertEqual(set(identity), {"user_id", "ship_id", "display_name"})

    def test_no_token_shaped_values_in_real_pipeline_snapshot(self):
        snap = real_pipeline_snapshot()
        text = snap.to_json()
        # JWT-ish / bearer-ish / long-hex secrets must not appear as values.
        import re

        self.assertEqual(re.findall(r"eyJ[A-Za-z0-9_-]{10,}", text), [])
        self.assertEqual(re.findall(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}", text), [])
        self.assertEqual(re.findall(r"\b[0-9a-f]{32,}\b", text), [])

    def _secret_must_reject(self, mutate):
        payload = valid_payload()
        mutate(payload)
        with self.assertRaises(
            SnapshotError,
            msg=f"secret-looking payload must fail closed: {json.dumps(payload)[:120]}",
        ):
            snapshot_from_dict(payload)

    def test_loader_rejects_forbidden_key_at_root(self):
        self._secret_must_reject(lambda p: p.update(accessToken="x"))

    def test_loader_rejects_forbidden_key_nested_in_identity(self):
        self._secret_must_reject(
            lambda p: p["identity"].update(refreshToken="x")
        )

    def test_loader_rejects_forbidden_key_nested_in_stack(self):
        self._secret_must_reject(
            lambda p: p["stacks"][0].update(password="x")
        )

    def test_loader_rejects_forbidden_key_nested_in_list_of_dicts(self):
        # list-of-dicts nesting (adversarial lane candidate: the deep scan
        # must not stop at dict values)
        self._secret_must_reject(
            lambda p: p["stacks"][0].update(protection_reasons=["ok"])
            or p["stacks"].append({"secret": "x"})
        )

    def test_loader_rejects_forbidden_key_nested_in_conditions(self):
        self._secret_must_reject(
            lambda p: p["conditions"].append({"code": "X", "detail": "d", "token": "y"})
        )

    def test_loader_rejects_every_forbidden_snapshot_key(self):
        # Every name in the loader's blacklist is enforced somewhere in the
        # payload, not just the few used above.
        for key in FORBIDDEN_SNAPSHOT_KEYS:
            payload = valid_payload()
            payload[key] = "x"
            with self.assertRaises(SnapshotError):
                snapshot_from_dict(payload)

    def test_secret_rejection_covers_case_variants_of_capture_vocabulary(self):
        # The capture vocabulary is exact-case in the wire protocol, but a
        # smuggler should not get through with a case-variant key either:
        # lowercase and no-separator variants are already blacklisted.
        for key in ("accessToken", "accesstoken", "refreshToken", "refreshtoken"):
            payload = valid_payload()
            payload["identity"][key] = "x"
            with self.assertRaises(SnapshotError):
                snapshot_from_dict(payload)

    def test_snapshot_never_persists_authentication_state_from_pipeline(self):
        # The builder takes only (identity, classified, ledger, reduction):
        # none of these types can carry credentials. Prove the bound
        # surface: the snapshot module has no attribute that could hold
        # authentication material.
        import sdk.inventory.snapshot as snapshot_mod

        for attr in ("accessToken", "refreshToken", "device_key", "email", "password"):
            self.assertFalse(hasattr(snapshot_mod, attr))


class TestNoTransferPlanning(unittest.TestCase):
    """S2: candidate is informational; no donor/recipient semantics exist."""

    def test_serialized_snapshot_has_no_transfer_vocabulary(self):
        snap = real_pipeline_snapshot()
        text = snap.to_json().lower()
        for word in TRANSFER_VOCABULARY:
            self.assertNotIn(word, text, f"snapshot JSON contains {word!r}")

    def test_serialized_portfolio_has_no_transfer_vocabulary(self):
        a = real_pipeline_snapshot(user_id=101, name="alpha")
        b = real_pipeline_snapshot(user_id=202, name="bravo")
        portfolio = aggregate_snapshots([b, a])
        text = json.dumps(portfolio.to_dicts()).lower()
        for word in TRANSFER_VOCABULARY:
            self.assertNotIn(word, text, f"portfolio JSON contains {word!r}")

    def test_no_donor_recipient_structure_exists_in_portfolio_rows(self):
        a = real_pipeline_snapshot(user_id=101)
        b = real_pipeline_snapshot(user_id=202)
        portfolio = aggregate_snapshots([a, b])
        for row in portfolio.rows:
            d = row.to_dict()
            for banned in ("donor", "recipient", "match", "route", "proposal"):
                self.assertNotIn(banned, d)
            # candidate quantities stay strictly per-account contributions
            accounts = cast(List[Dict[str, Any]], d["accounts"])
            for account in accounts:
                self.assertIsInstance(account["candidate_quantity"], int)
                self.assertGreaterEqual(account["candidate_quantity"], 0)

    def test_deficit_is_per_account_observation_never_transfer_plan(self):
        # Account A unmet 3 on design 501, account B surplus candidates 5:
        # the portfolio must report both facts but never pair them.
        a = snapshot_from_dict(valid_payload(user_id=101))
        deficit = valid_payload(user_id=202)
        deficit["designs"][0]["unmet_quantity"] = 3
        deficit["designs"][0]["requested_quantity"] = 8
        # Stack rows must carry the design's demand totals verbatim
        # (adversarial-lane cross-check: stack/design demand equality).
        for stack in deficit["stacks"]:
            stack["requested_quantity"] = 8
        b = snapshot_from_dict(deficit)
        portfolio = aggregate_snapshots([a, b])
        row = portfolio.row_for(501)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.deficit_accounts, (202,))
        self.assertEqual(row.total_unmet, 3)
        # Both accounts' stacks carry candidate_quantity=2 (informational
        # dry-run numbers); the total is a sum of observations, never a
        # matched/movable quantity.
        self.assertEqual(row.total_candidates, 4)
        # No field pairs 202's deficit with 101's candidates: candidates
        # appear ONLY inside each account's own provenance row.
        self.assertEqual(
            [c.user_id for c in row.accounts if c.unmet_quantity > 0], [202]
        )
        for contribution in row.accounts:
            if contribution.user_id == 202:
                # the deficit account's OWN candidate quantity is separate
                # from its unmet demand; nothing nets one against the other
                self.assertEqual(contribution.candidate_quantity, 2)
                self.assertEqual(contribution.unmet_quantity, 3)

    def test_candidate_quantity_is_never_a_transferable_quantity(self):
        # The portfolio exposes total_candidates as an aggregate of
        # informational dry-run numbers; no output field converts it
        # into a movable/transferable quantity (no such field exists).
        a = real_pipeline_snapshot(user_id=101)
        portfolio = aggregate_snapshots([a])
        row = portfolio.row_for(501)
        assert row is not None
        d = row.to_dict()
        self.assertEqual(
            sorted(d),
            sorted(
                [
                    "item_design_id",
                    "item_name",
                    "accounts",
                    "total_observed",
                    "total_requested",
                    "total_reserved",
                    "total_unmet",
                    "total_candidates",
                    "evidence",
                    "deficit_accounts",
                    "unresolved_codes",
                    "actionable",
                ]
            ),
        )
        self.assertFalse(d["actionable"])

    def test_inventory_module_has_no_transfer_executor_symbols(self):
        import sdk.inventory.snapshot as snapshot_mod
        import sdk.inventory.portfolio as portfolio_mod

        for symbol in (
            "donor",
            "recipient",
            "transfer",
            "match_accounts",
            "plan_transfer",
            "GiftItem",
            "SellItem3",
            "Purchase4",
            "executor",
            "reconciler",
        ):
            self.assertFalse(hasattr(snapshot_mod, symbol), symbol)
            self.assertFalse(hasattr(portfolio_mod, symbol), symbol)


class TestPurityNoNetworkNoClockNoDisk(unittest.TestCase):
    """S3: snapshot/portfolio surface is pure — no network, clock, disk."""

    @staticmethod
    def _blocked(*args, **kwargs):
        raise AssertionError("network I/O attempted inside sdk.inventory")

    def test_build_load_aggregate_under_socket_block(self):
        # Full Task-H surface under a socket block: any network attempt
        # anywhere fails the test loudly.
        with patch.object(socket, "socket", self._blocked), patch.object(
            socket, "create_connection", self._blocked
        ):
            snap = real_pipeline_snapshot()
            loaded = snapshot_from_json(snap.to_json())
            other = real_pipeline_snapshot(user_id=202, name="bravo")
            portfolio = aggregate_snapshots([loaded, other])
        self.assertEqual(len(portfolio.rows), 1)
        row = portfolio.row_for(501)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.total_observed, 10)

    def test_snapshot_and_portfolio_sources_free_of_network_tokens(self):
        forbidden_imports = (
            "import requests",
            "from requests",
            "import socket",
            "import urllib",
            "from urllib",
            "import http.client",
        )
        bypass_reads = ("os.environ", "getenv", "read_env")
        for rel in ("snapshot.py", "portfolio.py"):
            path = os.path.join(ROOT, "sdk", "inventory", rel)
            with open(path) as fh:
                src = fh.read()
            for token in forbidden_imports:
                self.assertNotIn(token, src, f"{rel} contains {token}")
            for token in bypass_reads:
                self.assertNotIn(token, src, f"{rel} contains {token}")

    def test_no_filesystem_access_tokens_in_snapshot_portfolio(self):
        for rel in ("snapshot.py", "portfolio.py"):
            path = os.path.join(ROOT, "sdk", "inventory", rel)
            with open(path) as fh:
                src = fh.read()
            for token in ("open(", "Path(", "os.remove", "os.makedirs", "shutil"):
                self.assertNotIn(token, src, f"{rel} touches the filesystem: {token}")
            # no clock reads (purity: captured_at is caller-supplied)
            for token in ("datetime.now", "time.time", "utcnow"):
                self.assertNotIn(token, src, f"{rel} reads a clock: {token}")

    def test_import_of_snapshot_and_portfolio_performs_no_network_io(self):
        # Fresh-interpreter import probe (same guard technique as the Task
        # F/G safety lanes): socket CONSTRUCTION fails loudly, so both the
        # import and a full build+aggregate cycle are network-proof.
        prog = textwrap.dedent(
            """
            import socket
            import ssl, http.client, urllib.request

            class _Blocked(socket.socket):
                def __init__(self, *args, **kwargs):
                    raise AssertionError("network I/O inside sdk.inventory")

            socket.socket = _Blocked
            socket.create_connection = _Blocked
            import sdk.inventory as inv
            designs = [inv.ItemDesign(
                item_design_id=501, item_name="Steel Plate", item_type="",
                item_sub_type="", rarity="common", tags=(), ingredients=(),
                module_type=None, training_design_id=None,
            )]
            items = [inv.Item(
                item_id=1, ship_id=55, item_design_id=501, quantity=5,
                is_new=False, skin_key=None,
            )]
            cat = inv.InventoryCatalog(items, inv.DesignCatalog(designs))
            classified = inv.InventoryClassifier(
                context=inv.ProtectionContext()
            ).classify(cat)
            ledger = inv.DemandPlanner().plan(cat, inv.PlanningContext())
            reduction = inv.ReductionPlanner().plan(classified, ledger)
            snap = inv.build_snapshot(
                inv.SnapshotAccountIdentity(101, 55, "a"),
                classified, ledger, reduction,
                captured_at="2026-09-28T00:00:00Z",
            )
            loaded = inv.snapshot_from_json(snap.to_json())
            inv.aggregate_snapshots([loaded])
            print("pure-ok")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", prog],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=120,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"network I/O or crash under socket guard: {result.stderr[-800:]}",
        )
        self.assertIn("pure-ok", result.stdout)


class TestNoMutationReachability(unittest.TestCase):
    """S4: no Client construction, transport call or forbidden endpoint."""

    def _inventory_sources(self):
        for rel in ("snapshot.py", "portfolio.py"):
            path = os.path.join(ROOT, "sdk", "inventory", rel)
            with open(path) as fh:
                yield rel, fh.read()

    def test_no_client_construction_or_transport_calls(self):
        for name, src in self._inventory_sources():
            self.assertNotIn("Client(", src, f"{name} constructs a Client")
            self.assertNotIn("from sdk.client import", src, f"{name} imports transport")
            self.assertNotIn(".request(", src, f"{name} calls transport")
            for scheme in ("http://", "https://"):
                self.assertNotIn(scheme, src, f"{name} embeds a URL")

    def test_no_mutation_endpoint_call_patterns(self):
        call_patterns = [f"{name}(" for name in FORBIDDEN_ENDPOINTS] + [
            "activateItem3(",
            "useConsumable(",
            "consumeItem(",
            "combineItem(",
            "deleteItem(",
            "giftItem(",
            "sellItem(",
            "purchase4(",
            "wearItem(",
            "unequipItems(",
            "refill2(",
            "repairBrokenModules(",
            "deleteAmmo(",
        ]
        for name, src in self._inventory_sources():
            for pattern in call_patterns:
                self.assertNotIn(pattern, src, f"{name} reaches {pattern}")

    def test_full_surface_under_blocked_client_namespace(self):
        # Runtime probe: poison every mutation-capable client method, then
        # run build + load + aggregate. If any code path reached a mutation
        # method, the poisoned namespace would fail the test.
        from sdk import client as client_mod

        def _poisoned(*args, **kwargs):
            raise AssertionError("mutation endpoint reached from inventory surface")

        originals: Dict[str, Any] = {}
        for name in (
            "consumeItem2",
            "activateItem3",
            "activateItem4",
            "combineItem4",
            "deleteItem3",
            "giftItem",
            "sellItem3",
            "purchase4",
            "wearItem",
            "unequipItems",
            "addItemIdToRoom",
            "moveItemIdToRoom3",
            "removeItemIdFromRoom",
            "updateItemIdsForRoom2",
            "refill2",
            "repairBrokenModules",
            "deleteAmmo",
        ):
            if hasattr(client_mod.Client, name):
                originals[name] = getattr(client_mod.Client, name)
                setattr(client_mod.Client, name, _poisoned)
        try:
            snap = real_pipeline_snapshot()
            loaded = snapshot_from_json(snap.to_json())
            portfolio = aggregate_snapshots([loaded])
            self.assertFalse(portfolio.actionable)
        finally:
            for name, original in originals.items():
                setattr(client_mod.Client, name, original)

    def test_client_never_imports_inventory(self):
        # One-way dependency boundary: transport must not import the
        # read-only domain (re-anchored here because Task H adds modules).
        path = os.path.join(ROOT, "sdk", "client.py")
        with open(path) as fh:
            src = fh.read()
        self.assertNotIn("import sdk.inventory", src)
        self.assertNotIn("from sdk.inventory", src)


class TestApplyRefusal(unittest.TestCase):
    """S5: `inventory apply` refuses unconditionally from every surface."""

    def _registry(self):
        from sdk.commands import create_command_registry

        return create_command_registry(MagicMock())

    def test_registry_dispatch_refuses(self):
        out = dispatch(self._registry(), "inventory apply")
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("REFUSED", out)
        self.assertIn("No configuration", out)

    def test_refusal_independent_of_args(self):
        registry = self._registry()
        for line in (
            "inventory apply",
            "inventory apply --force",
            "inventory apply --yes --confirm-all",
            "inventory apply 501 5",
        ):
            out = dispatch(registry, line)
            self.assertIsNotNone(out, line)
            assert out is not None
            self.assertIn("REFUSED", out, line)

    def test_refusal_survives_fully_armed_settings(self):
        from sdk.commands import create_command_registry

        client = MagicMock()
        client.accessToken = "t"
        armed = {
            "enabled": True,
            "strategic_reserve": {"501": 10},
            "training_reserve_tiers": 9,
            "starbux_cap_per_run": 999,
        }
        registry = create_command_registry(client)
        # The refusal handler reads no state; feed the armed shape through
        # the same dispatch path the TUI uses.
        with patch.dict(os.environ, {"ENABLE_MUTATION": "1", "INVENTORY_ENABLED": "1"}):
            out = dispatch(registry, "inventory apply")
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("REFUSED", out)

        # registry configured through the documented settings surface must
        # still refuse (attacker-shaped config, Task F regression shape).
        registry2 = create_command_registry(client)
        if hasattr(registry2, "_settings"):
            setattr(registry2, "_settings", armed)
        out2 = dispatch(registry2, "inventory apply")
        self.assertIsNotNone(out2)
        assert out2 is not None
        self.assertIn("REFUSED", out2)

    def test_tui_loop_dispatches_apply_to_refusal(self):
        from sdk.commands import create_command_registry
        from sdk.tui import TUI

        tui = TUI(MagicMock())
        tui.client = MagicMock()
        tui.client.accessToken = "t"
        tui.registry = create_command_registry(tui.client)
        tui.running = True
        buffer = io.StringIO()
        with patch("sdk.tui.input", side_effect=["inventory apply", "exit"]), redirect_stdout(
            buffer
        ):
            tui._command_loop()
        out = buffer.getvalue()
        self.assertIn("REFUSED", out)
        self.assertNotIn("Unknown inventory subcommand", out)

    def test_help_shows_unverified_refusal_stub(self):
        registry = self._registry()
        out = registry._cmd_help(["inventory", "apply"])
        self.assertIn("Usage: inventory apply", out)
        self.assertIn("(unverified)", out)

    def test_apply_stub_is_registered_and_not_verified(self):
        registry = self._registry()
        cmd = registry.get("inventory apply")
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertFalse(cmd["verified"])


class TestFailClosedCrossAccount(unittest.TestCase):
    """S6: cross-account inputs fail closed; no partial aggregation."""

    def test_duplicate_user_id_two_different_snapshots_aborts_whole_portfolio(self):
        a = snapshot_from_dict(valid_payload(user_id=101))
        b = snapshot_from_dict(valid_payload(user_id=101))
        with self.assertRaises(PortfolioError):
            aggregate_snapshots([a, b])

    def test_same_snapshot_twice_aborts_whole_portfolio(self):
        a = snapshot_from_dict(valid_payload(user_id=101))
        with self.assertRaises(PortfolioError):
            aggregate_snapshots([a, a])

    def test_non_snapshot_input_rejected(self):
        a = snapshot_from_dict(valid_payload(user_id=101))
        for bad in ({"user_id": 1}, "snapshot", 42, None, [1, 2]):
            with self.assertRaises(PortfolioError):
                bad_inputs: List[Any] = [a, bad]
                aggregate_snapshots(bad_inputs)  # type: ignore[arg-type]

    def test_no_partial_portfolio_on_later_duplicate(self):
        # The duplicate is discovered AFTER a valid account was processed:
        # the fail-closed rule must still hold (no partial result leaks).
        a = snapshot_from_dict(valid_payload(user_id=101))
        c = snapshot_from_dict(valid_payload(user_id=303))
        dup = snapshot_from_dict(valid_payload(user_id=101))
        with self.assertRaises(PortfolioError):
            aggregate_snapshots([a, c, dup])

    def test_item_ids_are_never_compared_across_accounts(self):
        # The same ItemId in two accounts is two independent stacks on two
        # different ships/accounts: ItemId is NOT a cross-account key, and
        # identical ItemIds must never be treated as a duplicate.
        a = snapshot_from_dict(valid_payload(user_id=101))
        b = snapshot_from_dict(valid_payload(user_id=202))
        portfolio = aggregate_snapshots([a, b])
        row = portfolio.row_for(501)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(len(row.accounts), 2)
        self.assertEqual(row.total_observed, 10)
        self.assertEqual(row.accounts[0].user_id, 101)
        self.assertEqual(row.accounts[1].user_id, 202)

    def test_actionable_true_payload_rejected_at_load(self):
        payload = valid_payload()
        payload["actionable"] = True
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_actionable_false_everywhere_after_load_and_aggregate(self):
        a = snapshot_from_dict(valid_payload(user_id=101))
        b = snapshot_from_dict(valid_payload(user_id=202))
        portfolio = aggregate_snapshots([a, b])
        self.assertFalse(a.actionable)
        self.assertEqual(a.actionable_rows(), ())
        self.assertFalse(portfolio.actionable)
        self.assertEqual(portfolio.actionable_rows(), ())
        for row in portfolio.rows:
            self.assertFalse(row.actionable)
        for row in portfolio.to_dicts():
            self.assertFalse(row["actionable"])

    def test_corrupt_snapshot_in_batch_aborts_whole_portfolio(self):
        # ANY malformed snapshot in the portfolio input rejects the ENTIRE
        # aggregation — the batch rule pinned in the reconciliation doc §3b.
        good = snapshot_from_dict(valid_payload(user_id=101))
        corrupt_text = real_pipeline_snapshot(user_id=202).to_json()[:-8]  # truncated
        with self.assertRaises((SnapshotError, PortfolioError)):
            loaded_bad = snapshot_from_json(corrupt_text)  # noqa: F841
            aggregate_snapshots([good, loaded_bad])

    def test_unknown_version_in_batch_aborts_whole_portfolio(self):
        good = snapshot_from_dict(valid_payload(user_id=101))
        payload = valid_payload(user_id=202)
        payload["version"] = 99
        with self.assertRaises(SnapshotError):
            loaded_bad = snapshot_from_dict(payload)  # noqa: F841
            aggregate_snapshots([good, loaded_bad])

    def test_secret_in_batch_snapshot_aborts(self):
        good = snapshot_from_dict(valid_payload(user_id=101))
        payload = valid_payload(user_id=202)
        payload["accessToken"] = "x"
        with self.assertRaises(SnapshotError):
            loaded_bad = snapshot_from_dict(payload)  # noqa: F841
            aggregate_snapshots([good, loaded_bad])


class TestDeterminismAndIsolation(unittest.TestCase):
    """S7: byte-identical output, order independence, mutation isolation."""

    def test_real_pipeline_snapshot_is_byte_identical_across_builds(self):
        a = real_pipeline_snapshot()
        b = real_pipeline_snapshot()
        self.assertEqual(a.to_json(), b.to_json())

    def test_round_trip_is_lossless(self):
        snap = real_pipeline_snapshot()
        loaded = snapshot_from_json(snap.to_json())
        self.assertEqual(loaded.to_json(), snap.to_json())
        self.assertEqual(loaded, snap)

    def test_portfolio_is_input_order_independent(self):
        a = real_pipeline_snapshot(user_id=101, name="alpha")
        b = real_pipeline_snapshot(user_id=202, name="bravo")
        forward = aggregate_snapshots([a, b])
        backward = aggregate_snapshots([b, a])
        self.assertEqual(forward.to_dicts(), backward.to_dicts())

    def test_to_dict_returns_fresh_containers(self):
        snap = snapshot_from_dict(valid_payload())
        d = snap.to_dict()
        identity = cast(Dict[str, Any], d["identity"])
        identity["user_id"] = 999
        d["actionable"] = True
        self.assertEqual(snap.identity.user_id, 101)
        self.assertFalse(snap.actionable)

    def test_snapshot_is_frozen(self):
        snap = snapshot_from_dict(valid_payload())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(snap, "actionable", True)

    def test_portfolio_is_frozen(self):
        a = snapshot_from_dict(valid_payload())
        portfolio = aggregate_snapshots([a])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(portfolio, "actionable", True)

    def test_aggregation_never_mutates_inputs(self):
        a = snapshot_from_dict(valid_payload(user_id=101))
        before = a.to_json()
        aggregate_snapshots([a])
        self.assertEqual(a.to_json(), before)


if __name__ == "__main__":
    unittest.main()
