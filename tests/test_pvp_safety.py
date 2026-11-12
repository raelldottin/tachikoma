#!/usr/bin/env python3
"""Safety/policy verification lane for the offline PvP package (tests/test_pvp_safety.py)."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO = Path(os.path.abspath(__file__)).parents[1]
PVP_DIR = REPO / "sdk" / "pvp"
SHIMS = [
    "account_progression.py",
    "battle_results.py",
    "progression_roadmap.py",
    "pvp_models.py",
    "pvp_performance.py",
    "pvp_planner.py",
    "pvp_report.py",
    "pvp_scoring.py",
]

FORBIDDEN_NETWORK = ("requests", "urllib", "socket", "http", "httpx", "aiohttp", "ssl", "mitm")


def _module_sources():
    sources = {}
    for p in sorted(PVP_DIR.glob("*.py")):
        sources[str(p.relative_to(REPO))] = p.read_text()
    for name in SHIMS:
        p = REPO / "sdk" / name
        if p.exists():
            sources[str(p.relative_to(REPO))] = p.read_text()
    return sources


def _identifiers(src: str):
    """Collect all identifier-like names from AST (names, attrs, defs, args)."""
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.append(node.id)
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(node.name)
        elif isinstance(node, ast.arg):
            out.append(node.arg)
        elif isinstance(node, ast.alias):
            if node.asname:
                out.append(node.asname)
            if node.name:
                out.append(node.name.split(".")[-1])
    return out


class TestPvpSafety(unittest.TestCase):
    # ---------- P1 secrets ----------

    def test_forbidden_secret_keys_present(self):
        from sdk.pvp.models import FORBIDDEN_SECRET_KEYS

        expected = {"accesstoken", "refreshtoken", "password", "token",
                    "apikey", "api_key", "clientkey", "client_key",
                    "devicekey", "device_key", "credentials", "checksum"}
        self.assertTrue(expected.issubset(FORBIDDEN_SECRET_KEYS))

    def test_reject_secret_keys_fail_closed(self):
        from sdk.pvp.models import reject_secret_keys, PvpInputError

        cases = [
            {"accessToken": "x"},
            {"nested": {"password": "x"}},
            {"lst": [{"token": "y"}]},
            {"t": ("a", {"secret": "z"})},
        ]
        for c in cases:
            with self.assertRaises(PvpInputError):
                reject_secret_keys(c)

    def test_profile_rejects_secret_keys(self):
        from sdk.pvp.models import AccountProgressionProfile, PvpInputError

        for key in ("accessToken", "password", "refreshToken", "deviceKey", "checksum"):
            with self.assertRaises(PvpInputError):
                AccountProgressionProfile(pseudonym="a", hull_level=10,
                                         resources={key: "v"})
            with self.assertRaises(PvpInputError):
                AccountProgressionProfile(pseudonym="a", hull_level=10,
                                         ammo_craft_config={key: "v"})

    def test_battle_notes_are_plain_strings(self):
        from sdk.pvp.models import (BattleResult, BattleRole, BattleOutcome)

        br = BattleResult(battle_id="1", account_pseudonym="a",
                          role=BattleRole.ATTACKER.value,
                          outcome=BattleOutcome.WIN.value,
                          notes="no secrets here")
        self.assertEqual(br.notes, "no secrets here")

    def test_config_version_no_secret_leak(self):
        from sdk.pvp.models import (AccountProgressionProfile, CrewMember,
                                     compute_ship_config_version)

        p = AccountProgressionProfile(pseudonym="secretAcct", hull_level=10,
            crew=(CrewMember(name="c1", level=5, rarity=3),),
            resources={})
        v = compute_ship_config_version(p)
        self.assertEqual(len(v), 16)
        self.assertNotIn("secretAcct", v)

    # ---------- P2 no network ----------

    def test_no_network_imports(self):
        for rel, src in _module_sources().items():
            tree = ast.parse(src)
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for n in node.names:
                        imports.append(n.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.append(node.module)
                    for n in node.names:
                        imports.append(n.name)
            joined = " ".join(imports).lower()
            for bad in FORBIDDEN_NETWORK:
                self.assertNotIn(bad, joined, f"{rel} imports {bad}")
            self.assertNotIn("sdk.client", joined, f"{rel} imports sdk.client")
            self.assertNotIn("sdk.commands", joined, f"{rel} imports sdk.commands")

    def test_runtime_purity_probe(self):
        code = textwrap.dedent("""
            import socket
            def boom(*a, **k):
                raise RuntimeError("NETWORK BLOCKED")
            socket.socket = boom
            socket.create_connection = boom
            import sdk.pvp as p
            from sdk.pvp.models import AccountProgressionProfile, BattleResult, CrewMember
            from sdk.pvp.scoring import assess_account
            from sdk.pvp.performance import build_performance_report
            from sdk.pvp.planner import build_progression_roadmap
            from sdk.pvp.report import render_account_report
            prof = AccountProgressionProfile(pseudonym="a", hull_level=10,
                crew=(CrewMember(name="c", level=5, rarity=3),),
                armor_coverage=70, power_balance=70, repair_proximity=70, weapon_coverage=70)
            a = assess_account(prof)
            rm = build_progression_roadmap(prof)
            rr = render_account_report(prof, a, rm)
            print("OK", len(rr))
        """)
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join([str(REPO), existing]) if existing else str(REPO)
        r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, f"probe failed: {r.stderr}")
        self.assertIn("OK", r.stdout)

    # ---------- P3 no mutation paths ----------

    def test_no_mutation_identifiers(self):
        forbidden = ("apply", "mutate", "execute", "transfer", "purchase",
                     "request", "client")
        for rel, src in _module_sources().items():
            ids = [i.lower() for i in _identifiers(src)]
            for ident in ids:
                for tok in forbidden:
                    self.assertNotIn(tok, ident,
                                     f"{rel} has forbidden identifier {ident!r}")

    def test_no_forbidden_calls_ast(self):
        for rel, src in _module_sources().items():
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        name = node.func.attr
                        if name in ("request", "apply", "mutate", "execute", "transfer"):
                            self.fail(f"{rel} calls .{name}()")
                    if isinstance(node.func, ast.Name):
                        if node.func.id in ("eval", "exec"):
                            self.fail(f"{rel} calls {node.func.id}()")

    def test_public_surface_has_no_mutation_functions(self):
        import sdk.pvp as pvp

        names = [n for n in dir(pvp) if not n.startswith("_")]
        forbidden_verbs = ("apply", "mutate", "execute", "transfer", "purchase",
                           "market", "gift", "matchmak", "farm", "premium",
                           "request", "client")
        for name in names:
            low = name.lower()
            for v in forbidden_verbs:
                self.assertNotIn(v, low, f"sdk.pvp exposes {name}")

    # ---------- P4 no automated gameplay ----------

    def test_no_gameplay_identifiers(self):
        forbidden = ("matchmaking", "battle_execute", "auto_play", "autoplay",
                     "unattended", "farm", "premium_purchase", "automated",
                     "matchmak")
        for rel, src in _module_sources().items():
            ids = [i.lower() for i in _identifiers(src)]
            for ident in ids:
                for tok in forbidden:
                    self.assertNotIn(tok, ident,
                                     f"{rel} has gameplay identifier {ident!r}")

    # ---------- P5 no live market activity ----------

    def test_no_market_identifiers(self):
        forbidden = ("market", "trade", "gift", "transfer", "sellitem",
                     "purchase", "listitemsofaship")
        for rel, src in _module_sources().items():
            ids = [i.lower() for i in _identifiers(src)]
            for ident in ids:
                for tok in forbidden:
                    self.assertNotIn(tok, ident,
                                     f"{rel} has market identifier {ident!r}")

    # ---------- P6 no account-control automation ----------

    def test_no_account_control_identifiers(self):
        forbidden = ("login", "logout", "authenticate", "refresh", "device",
                     "credential", "password", "access", "token", "client")
        for rel, src in _module_sources().items():
            ids = [i.lower() for i in _identifiers(src)]
            for ident in ids:
                for tok in forbidden:
                    self.assertNotIn(tok, ident,
                                     f"{rel} has account-control identifier {ident!r}")

    # ---------- P7 determinism + isolation ----------

    def test_frozen_dataclasses(self):
        from sdk.pvp.models import (AccountProgressionProfile, BattleResult,
                                     CrewMember, CombatRoom)
        for cls in (AccountProgressionProfile, BattleResult, CrewMember, CombatRoom):
            self.assertTrue(cls.__dataclass_params__.frozen)

    def test_cross_account_isolation(self):
        from sdk.pvp.models import (AccountProgressionProfile, BattleResult,
                                     PvpInputError, BattleRole, BattleOutcome)
        b = BattleResult(battle_id="1", account_pseudonym="other",
                         role=BattleRole.ATTACKER.value,
                         outcome=BattleOutcome.WIN.value)
        with self.assertRaises(PvpInputError):
            AccountProgressionProfile(pseudonym="acctA", hull_level=10,
                                     recent_battles=(b,))

    def test_duplicate_crew_rejected(self):
        from sdk.pvp.models import (AccountProgressionProfile, CrewMember, PvpInputError)
        with self.assertRaises(PvpInputError):
            AccountProgressionProfile(pseudonym="a", hull_level=10,
                crew=(CrewMember(name="c", level=1, rarity=3),
                      CrewMember(name="c", level=2, rarity=3)))

    def test_config_version_deterministic(self):
        from sdk.pvp.models import (AccountProgressionProfile, CrewMember,
                                     compute_ship_config_version)
        p = AccountProgressionProfile(pseudonym="a", hull_level=10,
            crew=(CrewMember(name="c1", level=5, rarity=3, training_points=10),))
        self.assertEqual(compute_ship_config_version(p), compute_ship_config_version(p))

    # ---------- P8 input validation ----------

    def test_input_validation_fail_closed(self):
        from sdk.pvp.models import (AccountProgressionProfile, CrewMember,
                                     CombatRoom, BattleResult, PvpInputError,
                                     BattleOutcome)
        with self.assertRaises(PvpInputError):
            AccountProgressionProfile(pseudonym="", hull_level=10)
        with self.assertRaises(PvpInputError):
            AccountProgressionProfile(pseudonym="a", hull_level=0)
        with self.assertRaises(PvpInputError):
            CrewMember(name="x", level=41, rarity=3)
        with self.assertRaises(PvpInputError):
            CrewMember(name="x", level=1, rarity=0)
        with self.assertRaises(PvpInputError):
            CombatRoom(design_id="1", name="r", level=5, max_level=3)
        with self.assertRaises(PvpInputError):
            BattleResult(battle_id="1", account_pseudonym="a", role="bogus",
                         outcome=BattleOutcome.WIN.value)

    def test_metrics_range(self):
        from sdk.pvp.models import AccountProgressionProfile, PvpInputError
        with self.assertRaises(PvpInputError):
            AccountProgressionProfile(pseudonym="a", hull_level=10, armor_coverage=101)
        with self.assertRaises(PvpInputError):
            AccountProgressionProfile(pseudonym="a", hull_level=10, armor_coverage=-1)

    # ---------- P9 reports no secret leakage ----------

    def test_reports_no_secret_values(self):
        from sdk.pvp.models import (AccountProgressionProfile, CrewMember)
        from sdk.pvp.scoring import assess_account
        from sdk.pvp.planner import build_progression_roadmap
        from sdk.pvp.report import render_account_report

        prof = AccountProgressionProfile(pseudonym="acctA", hull_level=10,
            crew=(CrewMember(name="c1", level=5, rarity=3),),
            armor_coverage=70, power_balance=70, repair_proximity=70, weapon_coverage=70)
        assessment = assess_account(prof)
        roadmap = build_progression_roadmap(prof)
        report = render_account_report(prof, assessment, roadmap)
        for bad in ("accessToken", "refreshToken", "password", "secret"):
            self.assertNotIn(bad, report)


if __name__ == "__main__":
    unittest.main()
