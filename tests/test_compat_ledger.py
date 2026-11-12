"""Tests for the patch-aware compatibility ledger (sdk/compat, AUTO-015)."""

import importlib
import json
import os
import re
import socket
import unittest
from pathlib import Path
from unittest import mock

from sdk.compat import (
    DECLARED_CLIENT_VERSION,
    EVIDENCE_CLASSES,
    LEDGER,
    Evidence,
    LedgerEntry,
    check_compatibility,
    compare_versions,
    entries_for_client_method,
    parse_version,
    validate_ledger,
)

REPO_ROOT = Path(os.path.abspath(__file__)).parents[1]
INVENTORY_PATH = REPO_ROOT / "docs" / "baseline" / "capability-inventory.json"
DOC_PATH = REPO_ROOT / "docs" / "baseline" / "compatibility-ledger.md"
# Documents whose text must already cite any public URL the ledger uses.
CITING_DOCS = ("README.template", "docs/PRODUCT_ROADMAP.md", "docs/pss-pvp-research-digest.md")


def _entry(entry_id, *evidence, kind="rule", code=()):
    return LedgerEntry(entry_id, kind, f"subject of {entry_id}", tuple(evidence), code=code)


class VersionTests(unittest.TestCase):
    def test_parse_accepts_repository_spellings(self):
        self.assertEqual(parse_version("0.999.59"), (0, 999, 59))
        self.assertEqual(parse_version("V0.999.55"), (0, 999, 55))
        self.assertEqual(parse_version("1.0"), (1, 0))

    def test_parse_rejects_garbage(self):
        for bad in ("", "latest", "0.999.x", "1..0", None, 1.0):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_version(bad)

    def test_compare_is_numeric_and_pads_with_zeros(self):
        self.assertEqual(compare_versions("0.999.60", "0.999.59"), 1)
        self.assertEqual(compare_versions("0.999.9", "0.999.59"), -1)
        self.assertEqual(compare_versions("1.0", "1.0.0"), 0)
        self.assertEqual(compare_versions("0.999.60", "1.0"), -1)


class ModelValidationTests(unittest.TestCase):
    def test_evidence_rejects_unknown_class_bad_version_bad_date_and_missing_source(self):
        with self.assertRaises(ValueError):
            Evidence("rumour", "README.template")
        with self.assertRaises(ValueError):
            Evidence("community", "README.template", game_version="next")
        with self.assertRaises(ValueError):
            Evidence("community", "README.template", observed="2026-13-01")
        with self.assertRaises(ValueError):
            Evidence("community", "  ")

    def test_entry_rejects_bad_kind_and_empty_evidence(self):
        with self.assertRaises(ValueError):
            _entry("x", Evidence("community", "a"), kind="mechanic")
        with self.assertRaises(ValueError):
            _entry("x")
        with self.assertRaises(ValueError):
            _entry("x", Evidence("community", "a"), code=("no_module_separator",))

    def test_duplicate_ids_are_refused(self):
        entry = _entry("dup", Evidence("community", "a"))
        with self.assertRaises(ValueError):
            validate_ledger([entry, entry])


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            _entry("old-strong", Evidence("official-release", "a", game_version="0.999.55")),
            _entry(
                "mixed",
                Evidence("community", "a", game_version="0.999.55"),
                Evidence("dynamically-demonstrated", "b", game_version="0.999.60"),
            ),
            _entry("unknown-weak", Evidence("static-analysis", "a")),
            _entry("unknown-strong", Evidence("dynamically-demonstrated", "a")),
            _entry("new-weak", Evidence("official-announcement", "a", game_version="1.0")),
        ]

    def test_classifies_against_target(self):
        report = check_compatibility("0.999.60", self.entries, declared_client_version="0.999.59")
        self.assertEqual(report.stale, ("old-strong",))
        self.assertEqual(report.current, ("mixed",))
        self.assertEqual(report.weak_at_target, ("new-weak",))
        self.assertEqual(report.unknown_version, ("unknown-weak", "unknown-strong"))
        self.assertEqual(report.weak_only, ("unknown-weak", "new-weak"))
        self.assertTrue(report.client_behind_target)

    def test_newest_evidence_decides_staleness(self):
        # "mixed" has an old community record and a newer strong record: current, not stale.
        report = check_compatibility("0.999.56", self.entries)
        self.assertIn("mixed", report.current)
        self.assertNotIn("mixed", report.stale)
        self.assertNotIn("mixed", report.weak_only)

    def test_summary_counts_and_flagged(self):
        report = check_compatibility("1.0", self.entries)
        summary = report.summary()
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["stale"], 2)
        self.assertEqual(summary["current"], 0)
        self.assertEqual(summary["weak_at_target"], 1)
        self.assertEqual(summary["unknown_version"], 2)
        self.assertEqual(summary["weak_only"], 2)
        self.assertEqual(summary["flagged"], 5)
        self.assertEqual(summary["clean"], 0)
        self.assertIsNone(summary["client_behind_target"])
        self.assertEqual(len(report.flagged), len(set(report.flagged)))

    def test_weak_evidence_at_target_does_not_make_an_entry_current(self):
        # Regression: the newest version of any class used to decide currency, so
        # strong evidence at 0.999.55 plus community evidence at 1.0 read as
        # current, not weak and not flagged against 1.0.
        entry = _entry(
            "rule.x",
            Evidence("official-release", "a", game_version="0.999.55"),
            Evidence("community", "b", game_version="1.0"),
        )
        report = check_compatibility("1.0", [entry])
        self.assertEqual(report.current, ())
        self.assertEqual(report.stale, ())
        self.assertEqual(report.weak_at_target, ("rule.x",))
        self.assertEqual(report.weak_only, ())
        self.assertEqual(report.flagged, ("rule.x",))
        self.assertEqual(report.summary()["clean"], 0)
        # Against a target the strong evidence covers, the entry is current.
        self.assertEqual(check_compatibility("0.999.55", [entry]).current, ("rule.x",))

    def test_partition_is_exact(self):
        for target in ("0.999.55", "0.999.60", "1.0"):
            report = check_compatibility(target, self.entries)
            buckets = report.current + report.weak_at_target + report.stale + report.unknown_version
            with self.subTest(target=target):
                self.assertEqual(sorted(buckets), sorted(e.entry_id for e in self.entries))
                self.assertEqual(set(report.flagged), set(buckets) - set(report.current))

    def test_report_serialises_and_renders(self):
        report = check_compatibility("0.999.60", self.entries)
        payload = json.loads(json.dumps(report.to_dict()))
        self.assertEqual(payload["summary"]["target_version"], "0.999.60")
        text = report.render_text()
        self.assertIn("Never observed on the target version or newer (1):", text)
        self.assertIn("  - old-strong", text)

    def test_invalid_target_is_refused(self):
        with self.assertRaises(ValueError):
            check_compatibility("soon", self.entries)

    def test_check_is_offline(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")):
            report = check_compatibility("1.0")
        self.assertEqual(report.total, len(LEDGER))


class ShippedLedgerTests(unittest.TestCase):
    def test_every_entry_is_valid_and_ids_are_unique(self):
        self.assertEqual(len({entry.entry_id for entry in LEDGER}), len(LEDGER))
        for entry in LEDGER:
            for item in entry.evidence:
                self.assertIn(item.evidence_class, EVIDENCE_CLASSES)

    def test_sources_are_repository_paths_or_urls_the_repository_cites(self):
        cited = "".join((REPO_ROOT / path).read_text(encoding="utf-8") for path in CITING_DOCS)
        for entry in LEDGER:
            for item in entry.evidence:
                with self.subTest(entry=entry.entry_id, source=item.source):
                    if item.source.startswith("https://"):
                        self.assertIn(item.source, cited)
                    else:
                        self.assertTrue((REPO_ROOT / item.source).is_file())

    def test_code_references_resolve(self):
        for entry in LEDGER:
            for ref in entry.code:
                with self.subTest(entry=entry.entry_id, ref=ref):
                    module_name, attr_path = ref.split(":")
                    target = importlib.import_module(module_name)
                    for part in attr_path.split("."):
                        target = getattr(target, part)
                    self.assertTrue(callable(target))

    def test_every_live_claimed_inventory_method_has_a_ledger_entry(self):
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        live = [m["name"] for m in inventory["client_methods"] if m["evidence"] == "live-claimed"]
        self.assertTrue(live, "inventory lists no live-claimed methods")
        for name in live:
            with self.subTest(method=name):
                entries = entries_for_client_method(name, LEDGER)
                self.assertTrue(entries, f"no ledger entry covers live-claimed {name}")
                # The live claim is documentation only and records no game version.
                classes = {item.evidence_class for entry in entries for item in entry.evidence}
                self.assertIn("dynamically-demonstrated", classes)

    def test_declared_client_version_matches_client_source(self):
        source = (REPO_ROOT / "sdk" / "client.py").read_text(encoding="utf-8")
        found = re.findall(r'[Cc]lientVersion"?\s*(?::\s*str\s*)?[:=]\s*"([^"]+)"', source)
        self.assertGreaterEqual(len(found), 3)
        self.assertEqual(set(found), {DECLARED_CLIENT_VERSION})

    def test_unrecorded_versions_stay_unknown(self):
        # The README live claim, the captures and the formulas record no game version.
        by_id = {entry.entry_id: entry for entry in LEDGER}
        for entry_id in (
            "endpoint.battle.create-battle9",
            "endpoint.battle.accept-battle5",
            "endpoint.battle.finalise-battle15",
            "endpoint.market.sell-item3",
            "endpoint.market.purchase4",
            "formula.pvp.trophy-gain",
        ):
            with self.subTest(entry=entry_id):
                self.assertIsNone(by_id[entry_id].latest_version)

    def test_recorded_official_versions(self):
        by_id = {entry.entry_id: entry for entry in LEDGER}
        self.assertEqual(by_id["rule.hull.max-level"].latest_version, "0.999.55")
        self.assertEqual(by_id["rule.hull.max-level"].latest_strong_version, "0.999.55")
        # The declared client version is unverified, so it never makes the entry current.
        self.assertIsNone(by_id["rule.client.declared-version"].latest_strong_version)
        report = check_compatibility("0.999.59")
        self.assertIn("rule.client.declared-version", report.weak_at_target)
        self.assertEqual(by_id["rule.research.room-reinforcement"].latest_version, "0.999.60")
        self.assertEqual(
            by_id["rule.release.pixel-starships-1-0"].evidence[0].evidence_class, "official-announcement"
        )

    def test_marketplace_path_is_weak_only(self):
        report = check_compatibility("0.999.59")
        for entry_id in (
            "endpoint.market.list-active-marketplace-messages5",
            "endpoint.market.sell-item3",
            "endpoint.market.purchase4",
            "endpoint.market.gift-item",
        ):
            self.assertIn(entry_id, report.weak_only)

    def test_target_1_0_flags_every_entry(self):
        report = check_compatibility("1.0")
        self.assertEqual(report.current, ())
        self.assertEqual(report.summary()["clean"], 0)
        self.assertTrue(report.client_behind_target)

    def test_doc_lists_every_entry_id(self):
        text = DOC_PATH.read_text(encoding="utf-8")
        for entry in LEDGER:
            with self.subTest(entry=entry.entry_id):
                self.assertIn(f"`{entry.entry_id}`", text)


if __name__ == "__main__":
    unittest.main()
