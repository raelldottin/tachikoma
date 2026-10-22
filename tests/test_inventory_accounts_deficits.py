#!/usr/bin/env python3
"""Task H architect lane: read-only `inventory accounts` / `inventory deficits`.

Covers the CLI verbs over persisted snapshots and the pure renderers:
byte determinism, fail-closed behavior on corrupt/unknown-version/
duplicate/secret-bearing snapshots (whole-view refusal, never a partial
portfolio), staleness annotation (never a rejection, never an evidence
change), weakest-input evidence floors, the ListItemsOfAShip evidence
cap, no-transfer-planning vocabulary/structure, mutation isolation and
unconditional `inventory apply` refusal.

All snapshots are synthetic dicts (the schema-v1 shape the domain
loader accepts). The Client is a MagicMock; disk reads happen against
temp dirs only. NO live Pixel Starships traffic; sockets are blocked
in the purity tests.
"""

import datetime
import json
import os
import shutil
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sdk.inventory import (
    STALE_AFTER_SECONDS,
    render_accounts_view,
    render_deficits_view,
    snapshot_freshness,
    snapshot_from_dict,
)

# ---------------------------------------------------------------------------
# Synthetic snapshot fixtures (schema v1 shape; all values synthetic)
# ---------------------------------------------------------------------------


def stack_row(item_id=1001, design_id=501, quantity=10, **overrides):
    row = {
        "item_id": item_id,
        "item_design_id": design_id,
        "ship_id": 55,
        "quantity": quantity,
        "classification": "CONSUMABLE",
        "protected": False,
        "unknown": False,
        "roles": [],
        "protection_reasons": [],
        "classification_reasons": [],
        "evidence": "CAPTURED_NOT_VERIFIED",
        "requested_quantity": 0,
        "reserved_quantity": 0,
        "candidate_quantity": 0,
        "candidate_state": "HOLD",
        "candidate_reasons": ["BLOCKED_ZERO_UNRESERVED"],
        "candidate_evidence": "UNKNOWN",
        "disposition_hint": "KEEP",
    }
    row.update(overrides)
    return row


def design_row(design_id=501, name="Missile Alpha", observed=10, **overrides):
    row = {
        "item_design_id": design_id,
        "item_name": name,
        "observed_quantity": observed,
        "requested_quantity": 0,
        "reserved_quantity": 0,
        "unmet_quantity": 0,
        "evidence": "CAPTURED_NOT_VERIFIED",
        "stacks": 1,
    }
    row.update(overrides)
    return row


def snap_payload(user_id=101, display_name="alpha", captured="2026-09-29T00:00:00Z", **overrides):
    payload = {
        "version": 1,
        "identity": {"user_id": user_id, "ship_id": 55, "display_name": display_name},
        "stacks": [stack_row()],
        "designs": [design_row()],
        "conditions": [],
        "limitations": [],
        "captured_at": captured,
        "actionable": False,
    }
    payload.update(overrides)
    return payload


def deficit_payload(user_id=202, display_name="bravo"):
    """One account holding 10, requesting 13 -> unmet 3 on design 501.

    Producer-possible shape (adversarial-lane gates): reserved 10 of the
    13 requested against 10 observed leaves unreserved 0, so every
    candidate must be 0/HOLD (the planner zeroes candidates when the
    pool is empty); stack demand totals mirror the design row.
    """
    return snap_payload(
        user_id=user_id,
        display_name=display_name,
        stacks=[stack_row(item_id=2001, requested_quantity=13, reserved_quantity=10,
                          candidate_quantity=0, candidate_state="HOLD",
                          candidate_reasons=["BLOCKED_ZERO_UNRESERVED"],
                          candidate_evidence="INFERRED")],
        designs=[design_row(observed=10, requested_quantity=13, reserved_quantity=10,
                            unmet_quantity=3)],
        conditions=[{"code": "DEMAND_EXCEEDS_SUPPLY",
                     "detail": "requested 13 > observed 10", "design_id": 501}],
    )


NOW = "2026-09-29T02:00:00Z"  # pinned clock: fixtures captured 2h earlier

# Pinned wall clock for the CLI path. sdk/commands.py reads the real
# clock (correct per the pinned purity decision: the CLI caller owns
# clock reads, sdk/inventory stays pure). Tests must pin it, because
# fixtures carry fixed captured_at dates: an unpinned run makes
# freshness assertions decay with the calendar (a fixture captured
# 2026-09-29T01:00:00Z flips from fresh to STALE on 2026-09-30, and a
# "future" fixture stops being future on its date). Pinning also makes
# the byte-identity tests safe across second boundaries.
_CLI_NOW = datetime.datetime(2026, 9, 29, 2, 0, 0, tzinfo=datetime.timezone.utc)


class _TempSnapshotsMixin:
    """Registry over a MagicMock client pointing at a temp snapshot dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="inv_accounts_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.registry = self._make_registry({"snapshots_dir": self.tmp})
        # Pin the CLI wall clock for every test in this module: the
        # registry handler path reads datetime.now() at render time.
        patcher = patch("sdk.commands.datetime")
        mocked = patcher.start()
        mocked.datetime.now.return_value = _CLI_NOW
        mocked.datetime.timezone = datetime.timezone
        self.addCleanup(patcher.stop)

    def _make_registry(self, inventory_settings=None):
        from sdk.client import Client
        from sdk.commands import CommandRegistry

        client = MagicMock(spec=Client)
        if inventory_settings is not None:
            client.settings = {"inventory": inventory_settings}
        return CommandRegistry(client)

    def _write_snapshot(self, name, payload):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def _run(self, subcommand, registry=None):
        handler = (registry or self.registry).get("inventory")["handler"]
        return handler([subcommand])


class TestAccountsViewBasics(_TempSnapshotsMixin, unittest.TestCase):
    """`inventory accounts` renders per-account observed facts only."""

    def test_accounts_header_and_wording(self):
        self._write_snapshot("a.json", snap_payload())
        self._write_snapshot("b.json", deficit_payload())
        out = self._run("accounts")
        self.assertIn("=== Inventory accounts (READ-ONLY Task H", out)
        self.assertIn("Candidate does not mean transferable.", out)
        self.assertIn("actionable=false", out)

    def test_accounts_renders_identity_and_totals_per_account(self):
        self._write_snapshot("a.json", snap_payload())
        self._write_snapshot("b.json", deficit_payload())
        out = self._run("accounts")
        self.assertIn("account 101 | alpha", out)
        self.assertIn("account 202 | bravo", out)
        # bravo: observed 10, requested 13, reserved 10, unmet 3
        self.assertIn("observed 10 | requested 13 | reserved 10 | unmet (informational) 3", out)

    def test_accounts_ascending_user_id_order(self):
        for uid in (303, 101, 202):
            self._write_snapshot(f"{uid}.json", snap_payload(user_id=uid, display_name=f"u{uid}"))
        out = self._run("accounts")
        positions = [out.index(f"account {uid} |") for uid in (101, 202, 303)]
        self.assertEqual(positions, sorted(positions))

    def test_accounts_reports_unresolved_condition_codes(self):
        self._write_snapshot("a.json", deficit_payload())
        out = self._run("accounts")
        self.assertIn("unresolved conditions 1: DEMAND_EXCEEDS_SUPPLY", out)

    def test_accounts_candidate_is_informational_wording(self):
        # A candidate-bearing account in a producer-possible shape:
        # observed 10, no demand -> unreserved 10, candidate 2.
        self._write_snapshot(
            "b.json",
            snap_payload(
                stacks=[stack_row(candidate_quantity=2, candidate_state="CANDIDATE",
                                  candidate_reasons=[], disposition_hint="USE")],
            ),
        )
        out = self._run("accounts")
        self.assertIn("dry-run candidates 2 (informational only", out)
        self.assertIn("never a quantity that can move between accounts", out)


class TestDeficitsViewBasics(_TempSnapshotsMixin, unittest.TestCase):
    """`inventory deficits` renders per-account unmet demand only."""

    def test_deficits_header_and_wording(self):
        self._write_snapshot("b.json", deficit_payload())
        out = self._run("deficits")
        self.assertIn("=== Inventory deficits (READ-ONLY Task H", out)
        self.assertIn("Candidate does not mean transferable.", out)
        self.assertIn("no deficit here is an instruction to acquire or move anything", out)

    def test_deficits_renders_unmet_per_account(self):
        self._write_snapshot("b.json", deficit_payload())
        out = self._run("deficits")
        self.assertIn("design 501 | Missile Alpha | unmet total 3", out)
        self.assertIn("account 202 (bravo) | observed 10 | requested 13 | reserved 10 | unmet (informational) 3", out)

    def test_deficits_never_lists_supply_holders_beside_deficit_holders(self):
        """A donor/recipient-style table is the forbidden structure:
        only deficit accounts appear under a design row, even when
        other accounts hold supply of the same design."""
        self._write_snapshot("supply_only.json", snap_payload())  # 101: holds 10, unmet 0
        self._write_snapshot("deficit.json", deficit_payload())   # 202: unmet 3
        out = self._run("deficits")
        self.assertIn("account 202 (bravo)", out)
        self.assertNotIn("account 101", out)

    def test_deficits_no_unmet_reports_none(self):
        self._write_snapshot("a.json", snap_payload())
        out = self._run("deficits")
        self.assertIn("No design has unmet demand", out)

    def test_deficits_candidate_quantity_absent_from_output(self):
        """No candidate QUANTITY is rendered in the deficits view:
        pairing candidates with deficits would imply account matching."""
        self._write_snapshot("b.json", deficit_payload())
        out = self._run("deficits")
        lowered = out.lower().replace("candidate does not mean transferable.", "")
        for line in lowered.splitlines():
            self.assertNotIn("candidate", line, line)


class TestViewDeterminism(_TempSnapshotsMixin, unittest.TestCase):
    """Identical inputs -> byte-identical views; input order never matters."""

    def test_accounts_byte_identical_across_runs(self):
        self._write_snapshot("a.json", snap_payload())
        self._write_snapshot("b.json", deficit_payload())
        self.assertEqual(self._run("accounts"), self._run("accounts"))

    def test_deficits_byte_identical_across_runs(self):
        self._write_snapshot("a.json", snap_payload())
        self._write_snapshot("b.json", deficit_payload())
        self.assertEqual(self._run("deficits"), self._run("deficits"))

    def test_file_write_order_does_not_change_output(self):
        """Same content under different file names must reorder by the
        snapshot's own keys (user_id), not by file name... but files
        are read in name order and aggregation is input-order
        independent, so output must be identical either way."""
        a = snap_payload(user_id=101)
        b = deficit_payload()
        self._write_snapshot("z_first.json", b)
        self._write_snapshot("a_second.json", a)
        first = self._run("accounts")
        os.remove(os.path.join(self.tmp, "z_first.json"))
        os.remove(os.path.join(self.tmp, "a_second.json"))
        self._write_snapshot("a_first.json", a)
        self._write_snapshot("z_second.json", b)
        second = self._run("accounts")
        self.assertEqual(first, second)

    def test_pure_renderers_deterministic(self):
        a = snapshot_from_dict(snap_payload())
        b = snapshot_from_dict(deficit_payload())
        self.assertEqual(
            render_accounts_view([a, b], now=NOW),
            render_accounts_view([b, a], now=NOW),
        )
        self.assertEqual(
            render_deficits_view([a, b], now=NOW),
            render_deficits_view([b, a], now=NOW),
        )


class TestFailClosedCorruption(_TempSnapshotsMixin, unittest.TestCase):
    """ANY bad snapshot refuses the WHOLE view; never a partial render."""

    def _assert_refused(self, out):
        self.assertIn("REFUSED fail-closed", out)
        self.assertIn("The whole view refuses (no partial rendering", out)

    def test_malformed_json_file_refuses_whole_view(self):
        self._write_snapshot("good.json", snap_payload())
        with open(os.path.join(self.tmp, "bad.json"), "w", encoding="utf-8") as f:
            f.write('{"version": 1, "identity": {')
        self._assert_refused(self._run("accounts"))
        self._assert_refused(self._run("deficits"))

    def test_unknown_version_refuses_whole_view(self):
        self._write_snapshot("good.json", snap_payload())
        self._write_snapshot("v2.json", snap_payload(version=2))
        self._assert_refused(self._run("accounts"))

    def test_missing_version_refuses_whole_view(self):
        payload = snap_payload()
        del payload["version"]
        self._write_snapshot("nov.json", payload)
        self._assert_refused(self._run("accounts"))

    def test_truncated_file_refuses_whole_view(self):
        text = json.dumps(snap_payload())
        with open(os.path.join(self.tmp, "trunc.json"), "w", encoding="utf-8") as f:
            f.write(text[: len(text) // 2])
        self._assert_refused(self._run("accounts"))

    def test_duplicate_item_id_refuses_whole_view(self):
        payload = snap_payload(
            stacks=[stack_row(item_id=1001), stack_row(item_id=1001, quantity=5)]
        )
        self._write_snapshot("dup.json", payload)
        self._assert_refused(self._run("accounts"))

    def test_secret_bearing_snapshot_refuses_whole_view(self):
        payload = snap_payload()
        payload["accessToken"] = "jwt-secret-value"
        self._write_snapshot("secret.json", payload)
        self._assert_refused(self._run("accounts"))

    def test_contradictory_duplicate_user_id_refuses_whole_view(self):
        """Two on-disk snapshots claiming the same account abort the
        whole aggregation (fail-closed batch rule)."""
        self._write_snapshot("a1.json", snap_payload(user_id=101))
        self._write_snapshot("a2.json", snap_payload(user_id=101, display_name="imposter"))
        out = self._run("accounts")
        self.assertIn("REFUSED fail-closed", out)
        self.assertIn("no partial rendering", out)
        # Never a partial listing of one account
        self.assertNotIn("account 101 |", out)

    def test_corrupt_file_refusal_names_the_file(self):
        self._write_snapshot("a_good.json", snap_payload())
        self._write_snapshot("z_bad.json", snap_payload(version=7))
        out = self._run("accounts")
        self.assertIn("REFUSED fail-closed", out)
        self.assertIn("z_bad.json", out)

    def test_non_json_extension_files_ignored(self):
        self._write_snapshot("notes.txt", {"junk": True})
        self._write_snapshot("a.json", snap_payload())
        out = self._run("accounts")
        self.assertIn("account 101", out)
        self.assertNotIn("REFUSED", out)

    def test_empty_directory_is_empty_state_not_error(self):
        out = self._run("accounts")
        self.assertIn("No account snapshots found", out)
        # Regression: the empty-state message must not reference a
        # capture verb that does not exist anywhere in this slice.
        self.assertNotIn("inventory capture", out)
        self.assertNotIn("inventory write", out)
        out = self._run("deficits")
        self.assertIn("No account snapshots found", out)

    def test_missing_directory_is_empty_state_not_error(self):
        registry = self._make_registry({"snapshots_dir": os.path.join(self.tmp, "nope")})
        out = self._run("accounts", registry)
        self.assertIn("No account snapshots found", out)


class TestStalenessAnnotation(_TempSnapshotsMixin, unittest.TestCase):
    """Staleness is a rendering annotation, never a rejection."""

    def test_stale_snapshot_renders_with_marker_not_refusal(self):
        self._write_snapshot("stale.json", snap_payload(captured="2026-09-01T00:00:00Z"))
        out = self._run("accounts")
        self.assertNotIn("REFUSED", out)
        self.assertIn("STALE", out)

    def test_fresh_snapshot_marked_fresh(self):
        self._write_snapshot("fresh.json", snap_payload(captured="2026-09-29T01:00:00Z"))
        out = self._run("accounts")
        self.assertIn("| fresh (age", out)
        self.assertIn("(all fresh)", out)
        # No per-account line carries a STALE marker
        for line in out.splitlines():
            if line.startswith("  account "):
                self.assertIn("| fresh", line, line)

    def test_threshold_constant_is_24_hours(self):
        self.assertEqual(STALE_AFTER_SECONDS, 24 * 60 * 60)

    def test_snapshot_freshness_boundary_semantics(self):
        # exactly at the threshold is still fresh; one second past is stale
        fresh = snapshot_freshness("2026-09-28T02:00:01Z", "2026-09-29T02:00:00Z")
        self.assertTrue(fresh.fresh)
        stale = snapshot_freshness("2026-09-28T02:00:00Z", "2026-09-29T02:00:01Z")
        self.assertTrue(stale.stale)

    def test_snapshot_freshness_deterministic(self):
        self.assertEqual(
            snapshot_freshness("2026-09-29T00:00:00Z", NOW),
            snapshot_freshness("2026-09-29T00:00:00Z", NOW),
        )

    def test_staleness_never_changes_evidence_tier(self):
        # Identical content, different capture times: the evidence tier
        # rendered for the account must be identical (staleness is a
        # separate annotation axis and never touches evidence).
        def payload(captured):
            return snap_payload(
                captured=captured,
                stacks=[stack_row(candidate_quantity=2, candidate_state="CANDIDATE",
                                  candidate_evidence="CAPTURED_NOT_VERIFIED",
                                  candidate_reasons=[], disposition_hint="USE")],
                designs=[design_row(evidence="CAPTURED_NOT_VERIFIED")],
            )

        fresh_snap = snapshot_from_dict(payload("2026-09-29T01:00:00Z"))
        stale_snap = snapshot_from_dict(payload("2026-09-01T00:00:00Z"))
        fresh_out = render_accounts_view([fresh_snap], now=NOW)
        stale_out = render_accounts_view([stale_snap], now=NOW)
        self.assertIn("weakest evidence CAPTURED_NOT_VERIFIED", fresh_out)
        self.assertIn("weakest evidence CAPTURED_NOT_VERIFIED", stale_out)
        self.assertIn("| fresh", fresh_out)
        self.assertIn("| STALE", stale_out)

    def test_future_captured_at_fails_closed(self):
        self._write_snapshot("future.json", snap_payload(captured="2027-01-01T00:00:00Z"))
        out = self._run("accounts")
        self.assertIn("REFUSED fail-closed", out)

    def test_unparseable_captured_at_fails_closed(self):
        self._write_snapshot("bad_ts.json", snap_payload(captured="not-a-timestamp"))
        out = self._run("accounts")
        self.assertIn("REFUSED fail-closed", out)

    def test_non_zulu_timestamp_fails_closed(self):
        self._write_snapshot("offset.json", snap_payload(captured="2026-09-29T00:00:00+02:00"))
        out = self._run("accounts")
        self.assertIn("REFUSED fail-closed", out)


class TestEvidenceFloors(_TempSnapshotsMixin, unittest.TestCase):
    """Weak evidence includes-with-weak-tier; weakest-input never promoted."""

    def test_weak_unknown_evidence_still_renders_not_dropped(self):
        payload = snap_payload(
            stacks=[stack_row(evidence="UNKNOWN", candidate_evidence="UNKNOWN")],
            designs=[design_row(evidence="UNKNOWN")],
        )
        self._write_snapshot("weak.json", payload)
        out = self._run("accounts")
        self.assertIn("account 101", out)
        self.assertIn("weakest evidence UNKNOWN", out)

    def test_weakest_input_evidence_is_min_not_max(self):
        # account 101: strong tier; account 202: UNKNOWN tier, same design,
        # both with unmet demand -> the aggregated design row carries the
        # weaker tier, never the stronger per-account feed tier.
        # Producer-possible shape: reserved 10 of 12 against 10 observed
        # leaves unreserved 0, so candidates are HOLD/0 with the planner's
        # BLOCKED_ZERO_UNRESERVED reason.
        strong = snap_payload(
            user_id=101,
            display_name="strong",
            stacks=[stack_row(item_id=1101, requested_quantity=12, reserved_quantity=10,
                              candidate_quantity=0, candidate_state="HOLD",
                              candidate_reasons=["BLOCKED_ZERO_UNRESERVED"],
                              candidate_evidence="CAPTURED_NOT_VERIFIED",
                              disposition_hint="KEEP")],
            designs=[design_row(observed=10, requested_quantity=12,
                                reserved_quantity=10, unmet_quantity=2)],
        )
        weak = snap_payload(
            user_id=202,
            display_name="weak",
            stacks=[stack_row(item_id=2001, evidence="UNKNOWN",
                              candidate_evidence="UNKNOWN",
                              requested_quantity=12, reserved_quantity=10,
                              candidate_quantity=0, candidate_state="HOLD",
                              candidate_reasons=["BLOCKED_ZERO_UNRESERVED"],
                              disposition_hint="KEEP")],
            designs=[design_row(observed=10, requested_quantity=12,
                                reserved_quantity=10, unmet_quantity=2,
                                evidence="UNKNOWN")],
        )
        self._write_snapshot("strong.json", strong)
        self._write_snapshot("weak.json", weak)
        out = self._run("deficits")
        # The aggregated design row carries the weaker tier (UNKNOWN),
        # never the stronger per-account feed tier.
        self.assertIn("evidence UNKNOWN", out)

    def test_feed_evidence_cap_never_verified(self):
        self._write_snapshot("a.json", snap_payload())
        out = self._run("accounts") + "\n" + self._run("deficits")
        self.assertNotIn("VERIFIED", out)

    def test_unknown_tier_in_payload_fails_closed_at_view(self):
        payload = snap_payload(designs=[design_row(evidence="MADE_UP_TIER")])
        self._write_snapshot("badtier.json", payload)
        out = self._run("accounts")
        self.assertIn("REFUSED fail-closed", out)


class TestNoTransferPlanning(_TempSnapshotsMixin, unittest.TestCase):
    """No donor/recipient/matching vocabulary or structure in output."""

    VOCAB = (
        "donor", "recipient", "transferable quantity", "ship to",
        "send to", "move to account", "proposal", "route", "match account",
        "gift", "sell", "purchase", "marketplace",
    )

    def test_views_contain_no_transfer_vocabulary(self):
        self._write_snapshot("a.json", snap_payload())
        self._write_snapshot("b.json", deficit_payload())
        accounts = self._run("accounts")
        deficits = self._run("deficits")
        for out in (accounts, deficits):
            lowered = out.lower()
            for word in self.VOCAB:
                self.assertNotIn(word, lowered, f"view contains {word!r}")

    def test_views_have_no_donor_recipient_structure(self):
        """No line pairs two accounts (never "101 -> 202", never a
        from/to pair on one line)."""
        self._write_snapshot("a.json", snap_payload())
        self._write_snapshot("b.json", deficit_payload())
        for out in (self._run("accounts"), self._run("deficits")):
            for line in out.splitlines():
                if "account" in line and "->" in line:
                    self.fail(f"account-pairing line: {line}")
                self.assertNotIn("from account", line)
                self.assertNotIn("to account", line)

    def test_supply_holder_absent_from_deficit_lines(self):
        self._write_snapshot("supply.json", snap_payload(user_id=101))
        self._write_snapshot("deficit.json", deficit_payload())
        out = self._run("deficits")
        self.assertNotIn("account 101", out)


class TestMutationIsolation(_TempSnapshotsMixin, unittest.TestCase):
    """The views touch no mutation surface; apply keeps refusing."""

    MUTATION_FRAGMENTS = (
        "/ItemService/ConsumeItem2", "/ItemService/GiftItem",
        "/MarketService/SellItem3", "/MarketService/Purchase4",
        "/ItemService/DeleteItem3", "/ItemService/ActivateItem4",
    )

    def test_views_render_with_sockets_blocked(self):
        self._write_snapshot("a.json", snap_payload())
        self._write_snapshot("b.json", deficit_payload())

        def _blocked(*args, **kwargs):
            raise AssertionError("network I/O attempted during portfolio view")

        with patch.object(socket, "socket", _blocked), patch.object(
            socket, "create_connection", _blocked
        ):
            accounts = self._run("accounts")
            deficits = self._run("deficits")
        self.assertIn("=== Inventory accounts", accounts)
        self.assertIn("=== Inventory deficits", deficits)

    def test_no_mutation_urls_in_touched_sources(self):
        base = os.path.join(os.path.dirname(__file__), "..")
        for rel in ("sdk/commands.py", "sdk/inventory/portfolio_render.py"):
            with open(os.path.join(base, rel), encoding="utf-8") as handle:
                src = handle.read()
            for frag in self.MUTATION_FRAGMENTS:
                self.assertNotIn(frag, src, rel)

    def test_views_read_no_env_or_config_for_behavior(self):
        base = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(base, "sdk/inventory/portfolio_render.py"), encoding="utf-8") as handle:
            src = handle.read()
        self.assertNotIn("os.environ", src)
        self.assertNotIn("getenv", src)

    def test_apply_still_refuses_alongside_new_verbs(self):
        handler = self.registry.get("inventory apply")["handler"]
        for args in ([], ["--force"], ["accounts"], ["deficits"]):
            out = handler(args)
            self.assertIn("REFUSED", out)

    def test_registry_inventory_apply_still_unverified(self):
        self.assertFalse(self.registry.is_verified("inventory apply"))
        self.assertTrue(self.registry.is_verified("inventory"))

    def test_pure_renderers_accept_frozen_snapshots_without_mutation(self):
        a = snapshot_from_dict(snap_payload())
        before = a.to_json()
        render_accounts_view([a], now=NOW)
        render_deficits_view([a], now=NOW)
        self.assertEqual(before, a.to_json())


class TestSubcommandDispatch(_TempSnapshotsMixin, unittest.TestCase):
    """Verb wiring: dispatch, help, unknown subcommands."""

    def test_unknown_subcommand_lists_new_verbs(self):
        out = self._run("frobnicate")
        self.assertIn("Unknown inventory subcommand: frobnicate", out)
        self.assertIn("inventory accounts", out)
        self.assertIn("inventory deficits", out)

    def test_plain_inventory_still_works(self):
        from sdk.inventory import DesignCatalog, InventoryCatalog

        client = self.registry.client
        client.dynamicDesigns = xmltodict.parse(_MINIMAL_FEED)
        client.itemsOfAShip = _MINIMAL_ITEMS
        designs = DesignCatalog.from_dynamic_dict(client.dynamicDesigns)
        catalog = InventoryCatalog.from_dicts(client.itemsOfAShip, designs)
        with patch("sdk.inventory.InventoryCatalog.from_client", return_value=catalog):
            out = self.registry.get("inventory")["handler"]([])
        self.assertIn("=== Inventory (read-only) ===", out)

    def test_plan_still_reaches_renderer(self):
        from sdk.inventory import DesignCatalog, InventoryCatalog

        client = self.registry.client
        client.dynamicDesigns = xmltodict.parse(_MINIMAL_FEED)
        client.itemsOfAShip = _MINIMAL_ITEMS
        designs = DesignCatalog.from_dynamic_dict(client.dynamicDesigns)
        catalog = InventoryCatalog.from_dicts(client.itemsOfAShip, designs)
        with patch("sdk.inventory.InventoryCatalog.from_client", return_value=catalog):
            out = self.registry.get("inventory")["handler"](["plan"])
        self.assertIn("=== Inventory plan", out)

    def test_tui_loop_reaches_accounts_view(self):
        """`inventory accounts` resolves from the REPL via the
        single-word verb + args path."""
        import io
        from contextlib import redirect_stdout

        from sdk.tui import TUI

        self._write_snapshot("a.json", snap_payload())
        client = MagicMock()
        client.accessToken = "t"
        client.settings = {"inventory": {"snapshots_dir": self.tmp}}
        tui = TUI(MagicMock())
        tui.client = client
        tui.registry = self.registry
        tui.running = True
        buffer = io.StringIO()
        with patch("sdk.tui.input", side_effect=["inventory accounts", "exit"]), redirect_stdout(buffer):
            tui._command_loop()
        out = buffer.getvalue()
        self.assertIn("=== Inventory accounts", out)
        self.assertIn("account 101", out)


_MINIMAL_FEED = (
    '<DesignService><ListAllDynamicDesigns><ItemDesigns>'
    '<ItemDesign ItemDesignId="501" ItemDesignName="Missile Alpha" '
    'ItemType="Missile" ItemSubType="Missile" Rarity="Common" '
    'Ingredients="" Tags=""/>'
    '</ItemDesigns></ListAllDynamicDesigns></DesignService>'
)

_MINIMAL_ITEMS = {
    "ItemService": {"ListItemsOfAShip": {"Items": {"Item": {
        "@ItemId": "10", "@ShipId": "55", "@ItemDesignId": "501",
        "@Quantity": "5", "@IsNew": "false", "@SkinKey": "",
    }}}}
}


import xmltodict  # noqa: E402  (used by dispatch/plan tests above)

if __name__ == "__main__":
    unittest.main()
