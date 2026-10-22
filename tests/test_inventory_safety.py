#!/usr/bin/env python3
"""Task F safety/policy verification lane (tests/test_inventory_safety.py).

Adversarial safety coverage for the Task F slice, per the swarm brief's
SAFETY / POLICY VERIFICATION lane and the demand-source-discovery handoff
(t_b38f0330, blackboard comment 28):

- regression-assert that classification and quantity alone NEVER create
  reservations (defect D2 makes this a live risk: TrainingDesignId="0" on
  2467/2557 live rows previously parsed as a training link, which would have
  collapsed every live design into training-material reservations once the
  demand layer lands);
- verify the four latent parse defects D1-D4 stay fixed against live-shape
  feeds;
- verify no sdk/inventory module (present or future) performs network I/O,
  reads environment/config bypasses, constructs a Client, or reaches any
  mutation endpoint;
- verify `inventory apply` refusal survives attacker-shaped configuration,
  not just environment variables;
- verify zero-quantity server placeholders (edge case E3) are inert.

All inputs are sanitized/synthetic. No live Pixel Starships traffic; these
tests never import requests and block sockets wherever they exercise the
domain surface (repo rule: tests mock all PSS network traffic).
"""

import os
import socket
import unittest
from unittest.mock import MagicMock, patch

import xmltodict

from sdk.inventory import (
    Classification,
    DesignCatalog,
    InventoryCatalog,
    InventoryClassifier,
    ProtectionContext,
    Role,
    parse_ingredients,
    parse_item_design,
)

# ---------------------------------------------------------------------------
# Sanitized fixtures — LIVE SHAPE of the 2026-08-08 dynamic feed (synthetic
# values, capture-observed attribute names). D2 shape: every mundane design
# carries TrainingDesignId="0" (2467/2557 live rows do exactly this).
# ---------------------------------------------------------------------------

LIVE_SHAPE_FEED_XML = """<DesignService xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
  <ListAllDynamicDesigns>
    <ItemDesigns>
      <ItemDesign ItemDesignId="501" ItemDesignName="Steel Plate" ItemType="Equipment" ItemSubType="EquipmentBody" Rarity="Common" Ingredients="" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="502" ItemDesignName="Plasma Bolt" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Common" Ingredients="" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="503" ItemDesignName="Brain Stim" ItemType="Consumable" ItemSubType="InstantTraining" Rarity="Common" Ingredients="" TrainingDesignId="77" Tags=""/>
      <ItemDesign ItemDesignId="504" ItemDesignName="Mystery Shard" ItemType="Artifact" ItemSubType="Artifact" Rarity="Common" Ingredients="0" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="505" ItemDesignName="Fused Core" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Common" Ingredients="501x2|504x3" TrainingDesignId="0" Tags=""/>
      <ItemDesign ItemDesignId="506" ItemDesignName="Empty Links" ItemType="Equipment" ItemSubType="EquipmentHead" Rarity="Common" Ingredients="" Tags=""/>
    </ItemDesigns>
  </ListAllDynamicDesigns>
</DesignService>"""

# The 17 endpoints no Task F code may reach (swarm brief MUTATION SAFETY).
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


def _items_response(rows):
    return {
        "ItemService": {
            "ListItemsOfAShip": {
                "Items": {
                    "Item": [
                        {
                            "@ItemId": str(item_id),
                            "@ShipId": "55",
                            "@ItemDesignId": str(design_id),
                            "@Quantity": str(qty),
                            "@IsNew": "false",
                            "@SkinKey": "",
                        }
                        for item_id, design_id, qty in rows
                    ]
                }
            }
        }
    }


def _catalog(rows, feed_xml=LIVE_SHAPE_FEED_XML):
    designs = DesignCatalog.from_dynamic_dict(xmltodict.parse(feed_xml))
    return InventoryCatalog.from_dicts(_items_response(rows), designs)


def _classified(rows, feed_xml=LIVE_SHAPE_FEED_XML):
    clf = InventoryClassifier(context=ProtectionContext())
    return clf.classify(_catalog(rows, feed_xml)), clf


class TestLiveShapeParseRegressions(unittest.TestCase):
    """D1-D4 latent defects (demand-source-discovery handoff) stay fixed.

    These were found against the 2026-08-08 capture but were latent — the
    old fixtures used the wrong attribute shapes, so the suite passed while
    every live row would have mis-parsed.
    """

    def _feed_designs(self):
        return DesignCatalog.from_dynamic_dict(xmltodict.parse(LIVE_SHAPE_FEED_XML))

    def test_d1_live_feed_emits_item_design_name(self):
        # D1: the live dynamic feed names designs @ItemDesignName; the parse
        # must read that attribute, not @ItemName.
        designs = self._feed_designs()
        steel = designs.get(501)
        assert steel is not None
        self.assertEqual(steel.item_name, "Steel Plate")
        self.assertTrue(all(d.item_name for d in designs.all()))

    def test_d1_item_name_is_legacy_fallback_only(self):
        # A legacy/synthetic row carrying only @ItemName still parses (the
        # fallback), but the live attribute wins when both are present.
        node = {
            "@ItemDesignId": "910",
            "@ItemName": "Legacy Name",
            "@ItemType": "Equipment",
            "@ItemSubType": "EquipmentBody",
            "@Rarity": "Common",
        }
        d = parse_item_design(node)
        self.assertEqual(d.item_name, "Legacy Name")
        both = dict(node, **{"@ItemDesignName": "Live Name"})
        self.assertEqual(parse_item_design(both).item_name, "Live Name")

    def test_d2_training_design_id_zero_is_absent(self):
        # D2: TrainingDesignId="0" is the live-feed null marker (2467/2557
        # rows). It must parse to None — 0 is NOT a training link.
        d = parse_item_design(
            {
                "@ItemDesignId": "501",
                "@ItemDesignName": "Steel Plate",
                "@ItemType": "Equipment",
                "@ItemSubType": "EquipmentBody",
                "@Rarity": "Common",
                "@TrainingDesignId": "0",
            }
        )
        self.assertIsNone(d.training_design_id)

    def test_d2_zero_marker_never_creates_training_role(self):
        # The D2 live risk: a feed where every mundane design carries
        # TrainingDesignId="0" must NOT collapse into training material.
        designs = self._feed_designs()
        for d in designs.all():
            if d.item_design_id != 503:  # 503 carries a real link (77)
                self.assertIsNone(d.training_design_id, d.item_design_id)

    def test_d2_real_training_link_still_parses(self):
        # The fix must not overreach: a REAL TrainingDesignId still marks
        # the design as training material.
        d = self._feed_designs().get(503)
        assert d is not None
        self.assertEqual(d.training_design_id, 77)

    def test_d2_live_shape_feed_yields_zero_training_classifications(self):
        # End-to-end consequence of D2 on the classification surface: with
        # the live shape, only the genuinely-linked design classifies as
        # training material. Before the fix, all six would have.
        classified, _ = _classified(
            [(11, 501, 1), (12, 502, 1), (13, 503, 1), (14, 504, 1), (15, 505, 1), (16, 506, 1)]
        )
        training = [c for c in classified if Role.CREW_TRAINING_MATERIAL in c.roles]
        self.assertEqual(len(training), 1)
        self.assertEqual(training[0].item.item_design_id, 503)

    def test_d3_from_client_reads_the_attribute_client_stores(self):
        # D3: Client.listAllDynamicDesigns stores the parsed feed as
        # `dynamicDesigns` (sdk/client.py); from_client must read exactly
        # that attribute — `dynamicItemDesigns` yields an empty catalog.
        client = MagicMock()
        client.dynamicDesigns = xmltodict.parse(LIVE_SHAPE_FEED_XML)
        catalog = DesignCatalog.from_client(client)
        self.assertEqual(len(catalog), 6)
        client.listAllDynamicDesigns.assert_not_called()  # no fetch, no I/O

    def test_d3_client_source_still_stores_dynamic_designs(self):
        # Source-level anchor for D lane observers: if the client attribute
        # is ever renamed, this fails until from_client is updated with it.
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "sdk", "client.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("self.dynamicDesigns", src)

    def test_d4_ingredients_zero_is_absent_not_dropped(self):
        # D4: Ingredients="0" is the live null marker (designs 544/780/786).
        # It must parse as "no recipe" while KEEPING the design cataloged —
        # previously it raised and silently de-cataloged real designs.
        d = parse_item_design(
            {
                "@ItemDesignId": "504",
                "@ItemDesignName": "Mystery Shard",
                "@ItemType": "Artifact",
                "@ItemSubType": "Artifact",
                "@Rarity": "Common",
                "@Ingredients": "0",
            }
        )
        self.assertEqual(d.ingredients, ())
        designs = self._feed_designs()
        self.assertIsNotNone(designs.get(504))  # cataloged, not dropped

    def test_d4_real_ingredients_still_parse_sorted(self):
        d = self._feed_designs().get(505)
        assert d is not None
        self.assertEqual(d.ingredients, ((501, 2), (504, 3)))

    def test_d4_malformed_ingredients_still_fail_closed(self):
        # The null-marker fix must not weaken the fail-closed direction:
        # genuinely malformed recipes still raise for the caller to route
        # to PROTECTED.
        for bad in ("81x", "x5", "81", "81x|", "ax5", "81x-2"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_ingredients(bad)

    def test_parse_deterministic(self):
        a = self._feed_designs()
        b = self._feed_designs()
        self.assertEqual(a.all(), b.all())


class TestClassificationAndQuantityNeverCreateReservations(unittest.TestCase):
    """The hard classification-vs-demand separation (swarm brief).

    Task D answers "what roles can this item have". Roles — however many —
    never claim quantity. This is the lane's core regression: when the Task
    F demand layer lands, these properties must still hold, because D2
    showed how easily a marker bug turns into mass false demand.
    """

    def test_roles_never_yield_reservations(self):
        # Every role-bearing design in the fixture at once: classification
        # may say RESERVED, but no quantity is ever claimed.
        classified, _ = _classified(
            [(11, 501, 1), (12, 503, 1), (13, 504, 1), (14, 505, 1), (15, 999999, 1)]
        )
        self.assertTrue(classified)
        for c in classified:
            self.assertEqual(c.reservation, ())
            self.assertIsNone(c.available_surplus)

    def test_training_role_present_still_no_reservation(self):
        # A REAL training design: the role fires, classification says
        # RESERVED — and the reservation tuple is still empty. Being a
        # training item is not a training plan.
        (c,) = _classified([(11, 503, 1)])[0]
        self.assertIn(Role.CREW_TRAINING_MATERIAL, c.roles)
        self.assertEqual(c.classification, Classification.RESERVED)
        self.assertEqual(c.reservation, ())

    def test_quantity_alone_never_creates_demand(self):
        # Adversarial quantities, including a live-shape zero-quantity
        # placeholder (E3) and absurd magnitudes: nothing changes.
        for qty in (0, 1, 10**9, 10**18):
            (c,) = _classified([(11, 501, qty)])[0]
            self.assertEqual(c.reservation, ())
            self.assertIsNone(c.available_surplus)

    def test_zero_quantity_placeholder_is_inert(self):
        # E3: a Quantity=0 server placeholder counts as zero supply and is
        # never special-cased into anything actionable.
        (zero,) = _classified([(11, 501, 0)])[0]
        (one,) = _classified([(11, 501, 1)])[0]
        self.assertEqual(zero.classification, one.classification)
        self.assertEqual(zero.protection_reasons, one.protection_reasons)
        self.assertEqual(zero.roles, one.roles)
        self.assertEqual(zero.reservation, ())

    def test_unknown_design_demand_fails_closed_via_protection(self):
        # Unknown design: no role, PROTECTED bucket, protection NEVER
        # depends on any future demand computation.
        (c,) = _classified([(11, 999999, 5)])[0]
        self.assertEqual(c.classification, Classification.PROTECTED)
        self.assertTrue(c.protected)
        self.assertEqual(c.reservation, ())


class TestWholePackageMutationIsolation(unittest.TestCase):
    """No sdk/inventory module — present or future — may perform network
    I/O, read config/environment bypasses, construct a Client, or reach any
    mutation endpoint.

    The scan auto-discovers every .py file in sdk/inventory so that the
    Task F demand modules (demand.py and successors) fall under it the
    moment they land. Endpoint names are checked as CALL patterns so that
    capture-verified checksum helpers may document their endpoint in
    docstrings without tripping the scan.
    """

    def _inventory_sources(self):
        root = os.path.join(os.path.dirname(__file__), "..", "sdk", "inventory")
        for name in sorted(os.listdir(root)):
            if name.endswith(".py"):
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as f:
                    yield name, f.read()

    def test_no_network_or_config_bypass_anywhere_in_package(self):
        forbidden_imports = (
            "import requests",
            "from requests",
            "import socket",
            "import urllib",
            "from urllib",
            "import http.client",
        )
        bypass_reads = ("os.environ", "getenv", "read_env", "ENABLE_MUTATION")
        for name, src in self._inventory_sources():
            for token in forbidden_imports:
                self.assertNotIn(token, src, f"{name} contains {token}")
            for token in bypass_reads:
                self.assertNotIn(token, src, f"{name} contains {token}")

    def test_no_client_construction_or_transport_calls(self):
        for name, src in self._inventory_sources():
            self.assertNotIn("Client(", src, f"{name} constructs a Client")
            self.assertNotIn("from sdk.client import Client", src, name)
            self.assertNotIn("self.request(", src, f"{name} calls transport")
            self.assertNotIn(".request(", src, f"{name} calls transport")
            for scheme in ("http://", "https://"):
                self.assertNotIn(scheme, src, f"{name} embeds a URL")

    def test_no_mutation_endpoint_call_patterns(self):
        # Endpoint-name call patterns, lower-camel Client-method form, and
        # the two mutation-capable methods that exist on Client today.
        call_patterns = [
            f"{name}(" for name in FORBIDDEN_ENDPOINTS
        ] + [
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
            # Pure checksum helper DEFINITIONS are not call sites (same
            # doc-only precedent as the URL allowlist below). Strip their
            # def lines before matching; any actual call still trips.
            body = "\n".join(
                line
                for line in src.splitlines()
                if not line.lstrip().startswith("def checksum_")
            )
            for pattern in call_patterns:
                self.assertNotIn(pattern, body, f"{name} reaches {pattern}")

    def test_sdk_client_import_allowlist(self):
        # sdk.client is transport; the ONLY symbol the inventory domain may
        # take from it is the pure _extract_collection helper (same one-way
        # boundary as catalog.py / designs.py).
        for name, src in self._inventory_sources():
            for line in src.splitlines():
                stripped = line.strip()
                if stripped.startswith("from sdk.client import"):
                    imported = stripped[len("from sdk.client import"):]
                    symbols = [s.strip() for s in imported.split(",")]
                    for symbol in symbols:
                        self.assertEqual(symbol, "_extract_collection", f"{name}: {stripped}")

    def test_package_purity_under_socket_block(self):
        # Functional probe: block all socket creation, then run the full
        # parse + classify surface over a live-shape feed. Any network
        # attempt anywhere in the package fails the test.
        def _blocked(*args, **kwargs):
            raise AssertionError("network I/O attempted inside sdk.inventory")

        with patch.object(socket, "socket", _blocked), patch.object(
            socket, "create_connection", _blocked
        ):
            classified, clf = _classified(
                [(11, 501, 2), (12, 503, 1), (13, 504, 0), (14, 999999, 1)]
            )
        self.assertEqual(len(classified), 4)
        self.assertTrue(clf.limitations)  # unavailability stays exposed

    def test_client_never_imports_inventory(self):
        # Architecture boundary (one-way dependency), re-anchored here so
        # the safety lane fails loudly on transport/domain inversion.
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "sdk", "client.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("import sdk.inventory", src)
        self.assertNotIn("from sdk.inventory", src)


class TestApplyRefusalConfigIndependence(unittest.TestCase):
    """`inventory apply` refusal must survive attacker-shaped configuration.

    The existing suites prove env-var immunity; this class proves CONFIG
    immunity: a registry built on a client whose settings/config sections
    scream "enabled" refuses identically.
    """

    def _registry(self):
        from sdk.client import Client
        from sdk.commands import CommandRegistry

        client = MagicMock(spec=Client)
        client.settings = {
            "inventory": {
                "enabled": True,
                "apply": True,
                "mutations": True,
                "force": True,
                "yes": True,
            }
        }
        client.config = dict(client.settings)
        return CommandRegistry(client)

    def test_apply_refuses_with_enabled_shaped_config(self):
        handler = self._registry().get("inventory apply")["handler"]
        for args in ([], ["--force"], ["--yes", "--config", "inventory.enabled=true"]):
            out = handler(args)
            self.assertIn("REFUSED", out, args)
            self.assertIn("No configuration", out)

    def test_apply_refusal_is_byte_identical_regardless_of_config(self):
        # The refusal does not even READ the config: identical bytes with
        # and without an enabling-looking config section.
        from sdk.client import Client
        from sdk.commands import CommandRegistry

        plain = CommandRegistry(MagicMock(spec=Client)).get("inventory apply")["handler"]([])
        armed = self._registry().get("inventory apply")["handler"]([])
        self.assertEqual(plain, armed)

    def test_apply_still_not_verified(self):
        self.assertFalse(self._registry().is_verified("inventory apply"))


class TestTaskGSafetyLan(unittest.TestCase):
    """Task G safety/policy verification lane (t_5f8b64c3).

    Independent proofs over the landed Task G surface (sdk/inventory/
    reduction.py + the plan dry-run CLI section), plus regressions for
    the three defects this lane found and fixed while verifying:

    - SAF-G1 (dispatch): the REPL tried the single-word verb first, so
      `inventory apply` was routed to _cmd_inventory's unknown-subcommand
      branch and the registered refusal stub was UNREACHABLE from the
      TUI; `help inventory apply` showed the verified `inventory` verb.
      Longest-registered-prefix resolution fixes both.
    - SAF-G2 (determinism): the reduction planner allocated per-design
      pool shares in caller-input order (only the output rows were
      sorted), so identical inputs in a different order produced a
      DIFFERENT allocation. The planner now enforces ascending-ItemId
      iteration itself.
    - SAF-G3 (AST coverage): the only AST proof of the apply refusal
      scanned handler code, not whether the refusal is REACHABLE from
      every dispatch surface (registry API, TUI loop, help verb).

    All inputs are synthetic. No live Pixel Starships traffic; sockets
    are blocked wherever a network attempt would matter.
    """

    # ------------------------------------------------------------------
    # Fixtures (shared builder for planner-level probes)
    # ------------------------------------------------------------------

    @staticmethod
    def _design(design_id, name="Widget", sub_type="ResourcePackage", rarity="common"):
        from sdk.inventory import ItemDesign

        return ItemDesign(
            item_design_id=design_id,
            item_name=name,
            item_type="Resource",
            item_sub_type=sub_type,
            rarity=rarity,
        )

    @staticmethod
    def _item(item_id, design_id, quantity):
        from sdk.inventory import Item

        return Item(
            item_id=item_id,
            ship_id=1,
            item_design_id=design_id,
            quantity=quantity,
            is_new=False,
            skin_key=None,
        )

    @classmethod
    def _plan_direct(cls, items, ledger, designs=None):
        """ReductionPlanner over ClassifiedItem rows built via the real
        classifier (protection context with no references), with the
        caller controlling the ORDER of the classified sequence."""
        from sdk.inventory import (
            DesignCatalog,
            InventoryCatalog,
            InventoryClassifier,
            PlanningContext,
            ReductionPlanner,
        )

        catalog = InventoryCatalog(list(items), DesignCatalog(list(designs or ())))
        classified = InventoryClassifier(
            context=PlanningContext().protection_context()
        ).classify(catalog)
        return ReductionPlanner().plan(classified, ledger), classified

    # ------------------------------------------------------------------
    # SAF-G2 regression: planner-level input-order independence
    # ------------------------------------------------------------------

    def test_planner_allocation_is_input_order_independent(self):
        """SAF-G2 regression: identical rows in ANY order -> identical plan.

        The planner's own contract allocates per-design pool shares in
        ascending ItemId order; before the fix the allocation followed
        the caller's sequence order (catalog normalization made the
        bug invisible to every test that built the classified sequence
        through InventoryCatalog).
        """
        from sdk.inventory import (
            Evidence,
            ReservationEntry,
            ReservationLedger,
            ReservationSource,
        )

        design = self._design(1)
        i10 = self._item(10, 1, 6)
        i11 = self._item(11, 1, 4)
        ledger = ReservationLedger(
            entries=(
                ReservationEntry(
                    item_design_id=1,
                    source=ReservationSource.EQUIPMENT,
                    requested_quantity=8,
                    reserved_quantity=8,
                    reason="test",
                    evidence=Evidence.CAPTURED_NOT_VERIFIED,
                    provenance="test",
                ),
            ),
            observed_by_design={1: 10},
        )
        from sdk.inventory import ReductionPlanner

        plan_asc, classified = self._plan_direct([i10, i11], ledger, [design])
        plan_rev = ReductionPlanner().plan(tuple(reversed(classified)), ledger)
        self.assertEqual(plan_asc.to_dicts(), plan_rev.to_dicts())
        # and the fixed allocation contract holds: low ItemId first
        by_item = {r.item_id: r.candidate_quantity for r in plan_asc.rows}
        self.assertEqual(by_item[10], 2)
        self.assertEqual(by_item[11], 0)
        self.assertEqual(
            sum(r.candidate_quantity for r in plan_asc.rows), 2
        )

    def test_planner_allocation_order_enforced_for_many_stacks(self):
        """Property probe: every permutation of a 4-stack design yields the
        same allocation (ascending-ItemId priority), not just the reverse."""
        import itertools

        from sdk.inventory import (
            Evidence,
            ReservationEntry,
            ReservationLedger,
            ReservationSource,
        )

        design = self._design(1)
        items = [
            self._item(101, 1, 3),
            self._item(102, 1, 3),
            self._item(103, 1, 3),
            self._item(104, 1, 3),
        ]
        ledger = ReservationLedger(
            entries=(
                ReservationEntry(
                    item_design_id=1,
                    source=ReservationSource.EQUIPMENT,
                    requested_quantity=9,
                    reserved_quantity=9,
                    reason="test",
                    evidence=Evidence.CAPTURED_NOT_VERIFIED,
                    provenance="test",
                ),
            ),
            observed_by_design={1: 12},
        )
        plans = set()
        for perm in itertools.permutations(items):
            plan, classified = self._plan_direct(list(perm), ledger, [design])
            plans.add(repr(tuple(plan.to_dicts())))  # repr: fully hashable
        self.assertEqual(len(plans), 1, "allocation varies with input order")
        plan0, _ = self._plan_direct(items, ledger, [design])
        # pool = unreserved = 3: the LOWEST ItemId takes it, siblings get 0
        by_item = {r.item_id: r.candidate_quantity for r in plan0.rows}
        self.assertEqual(by_item[101], 3)
        self.assertEqual(by_item[102], 0)
        self.assertEqual(by_item[103], 0)
        self.assertEqual(by_item[104], 0)
        self.assertEqual(sum(by_item.values()), 3)

    # ------------------------------------------------------------------
    # Structural non-actionability proofs (independent of the reduction
    # suite: these pin the STRUCTURE, not sample outputs)
    # ------------------------------------------------------------------

    def test_actionable_is_a_compile_time_constant_false(self):
        """AST proof: every ReductionResult constructed in reduction.py
        passes the literal False for `actionable` — the planner cannot
        emit an actionable row without editing this source file."""
        import ast
        from pathlib import Path

        import sdk.inventory

        source = Path(sdk.inventory.__file__).parent.joinpath("reduction.py").read_text()
        tree = ast.parse(source)
        constructions = [
            call
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ReductionResult"
            for call in [node]
        ]
        self.assertTrue(constructions, "no ReductionResult construction found")
        for call in constructions:
            actionable_args = [
                kw.value
                for kw in call.keywords
                if kw.arg == "actionable"
            ]
            self.assertTrue(
                actionable_args, "ReductionResult built without actionable=..."
            )
            for value in actionable_args:
                self.assertIsInstance(value, ast.Constant)
                self.assertIs(value.value, False)

    def test_actionable_field_has_no_default_true_anywhere(self):
        """The `actionable` dataclass field defaults to False (never
        True, never a mutable/computed default_factory)."""
        import dataclasses

        from sdk.inventory import ReductionResult

        field = {f.name: f for f in dataclasses.fields(ReductionResult)}["actionable"]
        self.assertIs(field.default, False)
        self.assertIs(field.default_factory, dataclasses.MISSING)

    def test_reduction_result_is_frozen(self):
        """Frozen rows: no downstream code can flip actionable to True on
        a row that left the planner."""
        from sdk.inventory import Disposition, Evidence, ReductionResult

        with self.assertRaises(Exception):
            ReductionResult(
                item_id=1,
                item_design_id=1,
                observed_quantity=1,
                total_reserved=0,
                unreserved=1,
                candidate_quantity=1,
                state="CANDIDATE",
                reasons=(),
                evidence=Evidence.CAPTURED_NOT_VERIFIED,
                disposition_hint=Disposition.KEEP,
                actionable=False,
            ).actionable = True

    def test_plan_actionable_rows_property_is_structurally_empty(self):
        """AST proof: `actionable_rows` has no implementation that could
        ever return a non-empty tuple (the property body is a constant
        empty-tuple return)."""
        import ast
        from pathlib import Path

        import sdk.inventory

        source = Path(sdk.inventory.__file__).parent.joinpath("reduction.py").read_text()
        tree = ast.parse(source)
        props = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "actionable_rows"
        ]
        self.assertEqual(len(props), 1)
        returns = [
            n.value
            for n in ast.walk(props[0])
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)
        ]
        self.assertEqual(len(returns), 1)
        self.assertEqual(len(returns[0].elts), 0)

    # ------------------------------------------------------------------
    # Mutation isolation: planner output cannot smuggle a mutation
    # ------------------------------------------------------------------

    def test_planner_output_rows_are_value_types_only(self):
        """to_dict() and every public field of ReductionResult carry only
        value types — no Client, no callable, no request object. A row
        cannot EXECUTE anything; this pins the output shape structurally.
        """
        from sdk.inventory import (
            DesignCatalog,
            Disposition,
            Evidence,
            InventoryCatalog,
            InventoryClassifier,
            PlanningContext,
            ReductionPlanner,
            ReservationLedger,
        )

        design = self._design(1)
        item = self._item(10, 1, 5)
        catalog = InventoryCatalog([item], DesignCatalog([design]))
        classified = InventoryClassifier(
            context=PlanningContext().protection_context()
        ).classify(catalog)
        plan = ReductionPlanner().plan(
            classified, ReservationLedger(observed_by_design={1: 5})
        )
        self.assertTrue(plan.rows)
        allowed = (bool, int, str, tuple, list, dict, type(None), Evidence, Disposition)
        for row in plan.rows:
            for value in row.to_dict().values():
                self.assertIsInstance(
                    value,
                    allowed,
                    f"non-value type {type(value).__name__} in row output",
                )
        # reasons/evidence/hint fields themselves
        for row in plan.rows:
            self.assertIsInstance(row.reasons, tuple)
            self.assertIsInstance(row.evidence, Evidence)
            self.assertIsInstance(row.disposition_hint, Disposition)

    def test_plan_dry_run_section_no_mutation_semantics_leak(self):
        """The rendered CLI dry-run output carries no executable surface:
        every disposition name appears only inside the fixed deferred
        disclaimer, hints are drawn from the assignable set, and the
        refusal tail is pinned."""
        import re

        out = self._render_plan_with_synthetic_catalog()
        self.assertIn("actionable False", out)
        self.assertIn("DRY-RUN", out)
        self.assertIn("'inventory apply' remains refused", out)
        # The deferred dispositions appear ONLY in the fixed disclaimer
        # sentence, never as an assigned hint.
        for row_line in (ln for ln in out.splitlines() if "hint " in ln):
            hint = re.search(r"hint (\w+)", row_line)
            self.assertIsNotNone(hint, row_line)
            assert hint is not None  # narrowing for the type checker
            self.assertIn(hint.group(1), {"KEEP", "USE", "CRAFT", "ALLOCATE"}, row_line)
        for deferred in ("TRANSFER", "SELL", "CONSUME", "DELETE"):
            hits = [
                ln
                for ln in out.splitlines()
                if re.search(rf"\b{deferred}\b", ln)
            ]
            self.assertTrue(hits, f"deferred disclaimer for {deferred} missing")
            for ln in hits:
                self.assertIn(
                    "remain deferred and unassigned",
                    ln,
                    f"{deferred} used outside the deferred disclaimer: {ln}",
                )
        # no mutation-endpoint vocabulary anywhere (the disclaimer's
        # "is executed or recommended" phrasing is the negation, so the
        # endpoint/verb scan targets endpoint names only)
        for banned in (
            "SellItem",
            "ConsumeItem",
            "DeleteItem",
            "MoveItemIdToRoom",
            "GiftItem",
            "Refill2",
            "Purchase4",
        ):
            self.assertNotIn(banned, out)

    # ------------------------------------------------------------------
    # Network isolation at import time (existing socket-block tests all
    # run AFTER import; sdk.inventory transitively imports sdk.client,
    # which imports requests — prove no socket fires at import time)
    # ------------------------------------------------------------------

    def test_import_sdk_inventory_performs_no_network_io(self):
        """Fresh-interpreter import probe: import sdk.inventory in a
        subprocess where socket CONSTRUCTION fails loudly. The stdlib
        (ssl subclasses socket.socket at import) is imported first so
        only a genuine connection attempt raises; the package import
        itself runs under the guard, catching import-time network
        attempts the in-process tests cannot see."""
        import subprocess
        import sys
        import textwrap

        prog = textwrap.dedent(
            """
            import socket  # stdlib loads BEFORE the guard: ssl subclasses
            import ssl, http.client, urllib.request  # socket at import time

            class _Blocked(socket.socket):
                def __init__(self, *args, **kwargs):
                    raise AssertionError(
                        "network I/O at import time in sdk.inventory"
                    )

            socket.socket = _Blocked
            socket.create_connection = _Blocked
            import sdk.inventory  # noqa: F401
            print("import-ok")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", prog],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"import-time network I/O or crash: {result.stderr[-800:]}",
        )
        self.assertIn("import-ok", result.stdout)

    # ------------------------------------------------------------------
    # SAF-G1 regression: refusal reachability from EVERY dispatch surface
    # ------------------------------------------------------------------

    def test_tui_loop_dispatches_inventory_apply_to_refusal_handler(self):
        """SAF-G1 regression: `inventory apply` typed into the REPL must
        reach the registered refusal stub. Before the fix the single-word
        `inventory` verb shadowed it and users saw "Unknown inventory
        subcommand" instead of the refusal."""
        import io
        from contextlib import redirect_stdout
        from unittest.mock import MagicMock, patch

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

    def test_tui_loop_inventory_plan_still_reaches_plan(self):
        """The dispatch fix must not break `inventory plan` or `inventory`
        from the REPL (the two-word probe only fires on a REGISTERED
        second word)."""
        import io
        from contextlib import redirect_stdout
        from unittest.mock import MagicMock, patch

        import xmltodict

        from sdk.commands import create_command_registry
        from sdk.inventory import DesignCatalog, InventoryCatalog
        from sdk.tui import TUI

        feed = (
            '<DesignService><ListAllDynamicDesigns><ItemDesigns>'
            '<ItemDesign ItemDesignId="1" ItemDesignName="W" '
            'ItemType="Resource" ItemSubType="ResourcePackage" '
            'Rarity="Common" Ingredients="" TrainingDesignId="0" Tags=""/>'
            '</ItemDesigns></ListAllDynamicDesigns></DesignService>'
        )
        items = {
            "ItemService": {"ListItemsOfAShip": {"Items": {"Item": {
                "@ItemId": "10", "@ShipId": "1", "@ItemDesignId": "1",
                "@Quantity": "5", "@IsNew": "false", "@SkinKey": "",
            }}}}
        }
        designs = DesignCatalog.from_dynamic_dict(xmltodict.parse(feed))
        catalog = InventoryCatalog.from_dicts(items, designs)

        client = MagicMock()
        client.accessToken = "t"
        registry = create_command_registry(client)
        with patch(
            "sdk.inventory.InventoryCatalog.from_client", return_value=catalog
        ):
            tui = TUI(MagicMock())
            tui.client = client
            tui.registry = registry
            tui.running = True
            buffer = io.StringIO()
            with patch(
                "sdk.tui.input", side_effect=["inventory plan", "exit"]
            ), redirect_stdout(buffer):
                tui._command_loop()
        out = buffer.getvalue()
        self.assertIn("=== Inventory plan", out)

    def test_help_inventory_apply_shows_unverified_refusal_stub(self):
        """SAF-G1 regression: `help inventory apply` must show the
        unverified refusal stub — before the fix it showed the verified
        `inventory` verb's usage, misrepresenting an unverified stub as
        a verified command."""
        from sdk.commands import create_command_registry

        registry = create_command_registry(MagicMock())
        out = registry._cmd_help(["inventory", "apply"])
        self.assertIn("Usage: inventory apply", out)
        self.assertIn("unverified", out)
        self.assertIn("REFUSED", out)
        # and it is NOT the inventory verb's entry
        self.assertNotIn("Usage: inventory <subcommand>", out)

    def test_help_resolution_covers_all_registered_verbs(self):
        """Every registered command resolves from `help <verb>` without
        falling through to Unknown."""
        from sdk.commands import create_command_registry

        registry = create_command_registry(MagicMock())
        for name in registry.list_commands():
            words = name.split()
            out = registry._cmd_help(words)
            self.assertIn(f"Usage: {name}", out, name)

    # ------------------------------------------------------------------
    # Refusal: system-level attack surface beyond config/env
    # ------------------------------------------------------------------

    def test_apply_refusal_independent_of_client_type(self):
        """The refusal must not depend on the client being a real Client:
        ANY object (duck-typed, attacker-substituted) yields the same
        refusal — the handler reads nothing from the client."""
        from sdk.commands import CommandRegistry

        class NotAClient:
            settings = {"inventory": {"enabled": True, "apply": True}}
            config = settings

        registry = CommandRegistry(NotAClient())
        refusal = registry.get("inventory apply")["handler"]([])
        self.assertIn("REFUSED", refusal)
        self.assertIn("No configuration", refusal)

    def test_apply_refusal_survives_fully_armed_settings_object(self):
        """Attacker-shaped settings nesting deeper than the plan path
        reads: every enable-shaped key the CLI might consult is True,
        and the refusal is byte-identical to the plain one."""
        from sdk.commands import CommandRegistry

        armed = {
            "inventory": {
                "enabled": True,
                "apply": True,
                "allow_mutation": True,
                "force": True,
                "yes": True,
                "executor": True,
                "phase8": True,
                "phase9": True,
                "dry_run": False,
                "verified_only": False,
            },
            "mutations": {"enabled": True, "inventory": True},
            "apply": True,
            "force": True,
        }
        plain = CommandRegistry(MagicMock()).get("inventory apply")["handler"]([])
        for client in (
            MagicMock(settings=armed),
            MagicMock(settings=armed, config=armed),
        ):
            out = CommandRegistry(client).get("inventory apply")["handler"]([])
            self.assertEqual(plain, out)
            self.assertIn("REFUSED", out)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _render_plan_with_synthetic_catalog(self):
        import xmltodict

        from sdk.commands import CommandRegistry
        from sdk.inventory import DesignCatalog, InventoryCatalog

        feed = (
            '<DesignService><ListAllDynamicDesigns><ItemDesigns>'
            '<ItemDesign ItemDesignId="1" ItemDesignName="W" '
            'ItemType="Resource" ItemSubType="ResourcePackage" '
            'Rarity="Common" Ingredients="" TrainingDesignId="0" Tags=""/>'
            '</ItemDesigns></ListAllDynamicDesigns></DesignService>'
        )
        items = {
            "ItemService": {"ListItemsOfAShip": {"Items": {"Item": {
                "@ItemId": "10", "@ShipId": "1", "@ItemDesignId": "1",
                "@Quantity": "5", "@IsNew": "false", "@SkinKey": "",
            }}}}
        }
        designs = DesignCatalog.from_dynamic_dict(xmltodict.parse(feed))
        catalog = InventoryCatalog.from_dicts(items, designs)
        client = MagicMock()
        client.shipByUserId = None
        client.getShipByUserId = MagicMock(return_value=None)
        registry = CommandRegistry(client)
        with patch(
            "sdk.inventory.InventoryCatalog.from_client", return_value=catalog
        ):
            return registry.get("inventory")["handler"](["plan"])


if __name__ == "__main__":
    unittest.main()
