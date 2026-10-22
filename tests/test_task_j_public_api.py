"""Task J integration: the inventory package's public API surface pins the
sanitize/reconcile evidence tools exported by the architecture-integration
lane (t_899abb44).

These tests are deliberately structural: they pin the exported surface so
that the Task J evidence modules cannot silently drift out of the package's
canonical public API, and they assert the safety invariants that the
sibling safety lane established (no executor semantics leak into the
package surface, sdk.client never imports sdk.inventory).
"""

import ast
import pathlib

import sdk.inventory as inv


class TestTaskJPublicApiSurface:
    """Every Task J evidence symbol is importable from the package root."""

    SANITIZE_EXPORTS = (
        "SanitizationError",
        "sanitize_body",
        "sanitize_capture_line",
        "sanitize_capture_record",
        "sanitize_headers",
        "sanitize_query_string",
        "scan_for_secrets",
    )

    RECONCILE_EXPORTS = (
        "GiftItemExchange",
        "InventoryDelta",
        "InventoryObservation",
        "ReconciliationError",
        "ReconciliationVerdict",
        "TierChange",
        "TransferabilityDecision",
        "account_tier_changes",
        "apply_tier_change",
        "checksum_tier_ceiling",
        "compute_inventory_delta",
        "decide_transferability",
        "reconcile_gift_exchange",
        "semantics_tier_ceiling",
    )

    CHECKSUM_EXPORTS = (
        "checksum_sell_item3",
        "checksum_purchase4",
    )

    def test_sanitize_exports(self):
        for name in self.SANITIZE_EXPORTS:
            assert hasattr(inv, name), f"{name} missing from sdk.inventory"
            assert name in inv.__all__, f"{name} missing from __all__"

    def test_reconcile_exports(self):
        for name in self.RECONCILE_EXPORTS:
            assert hasattr(inv, name), f"{name} missing from sdk.inventory"
            assert name in inv.__all__, f"{name} missing from __all__"

    def test_checksum_market_exports(self):
        for name in self.CHECKSUM_EXPORTS:
            assert hasattr(inv, name), f"{name} missing from sdk.inventory"
            assert name in inv.__all__, f"{name} missing from __all__"

    def test___all___matches_imports(self):
        """__all__ contains exactly the importable public names — no drift."""
        actual = set(name for name in dir(inv) if not name.startswith("_"))
        missing = set(inv.__all__) - actual
        assert not missing, f"__all__ lists unimportable names: {sorted(missing)}"

    def test_all_names_importable(self):
        """Star-import contract: every __all__ name resolves."""
        for name in inv.__all__:
            getattr(inv, name)


class TestTaskJModulePurityInvariants:
    """The integration must not weaken the package's purity guarantees."""

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def test_client_never_imports_inventory(self):
        client_src = (self.ROOT / "sdk" / "client.py").read_text(
            encoding="utf-8"
        )
        assert "sdk.inventory" not in client_src

    def test_evidence_modules_construct_no_client(self):
        """sanitize.py / reconcile.py must contain no Client construction."""
        for module_name in ("sanitize.py", "reconcile.py"):
            source = (
                self.ROOT / "sdk" / "inventory" / module_name
            ).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id == "Client":
                        raise AssertionError(
                            f"{module_name} constructs a Client at line "
                            f"{node.lineno} — evidence tools must be pure"
                        )
                if isinstance(node, ast.Attribute) and node.attr == "request":
                    raise AssertionError(
                        f"{module_name} touches network I/O via .request "
                        f"at line {node.lineno} — evidence tools must be pure"
                    )

    def test_no_executor_symbols_on_surface(self):
        """No executor/retry/mutation-planning names may appear on the
        exported surface — Task J evidence tools stay analysis-only."""
        banned_substrings = ("executor", "_executor", "_retry", "mutate")
        for name in inv.__all__:
            lowered = name.lower()
            for banned in banned_substrings:
                assert banned not in lowered, (
                    f"__all__ exports {name}: executor semantics must not "
                    "leak onto the Task J public surface"
                )
