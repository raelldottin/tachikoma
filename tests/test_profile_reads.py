"""Tests for SPEC-03 verified profile reads (offline, no network).

So far this holds the S03-R2 scan of ``tests/fixtures/live_sanitized/``: every
file there must be free of secrets and user ids before a parser is built on it.
Parser and provider tests arrive with the owner's captures (S03-R1).
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple

from sdk.redaction import REDACTION_PATTERNS
from tests.synthetic_fixtures import (
    SYNTHETIC_ACCESS_TOKEN,
    SYNTHETIC_DEVICE_KEY_IOS,
    SYNTHETIC_DEVICE_KEY_MAC,
    SYNTHETIC_EMAIL,
    SYNTHETIC_IOS_CHECKSUM_1,
)

LIVE_SANITIZED_DIR = Path(os.path.abspath(__file__)).parent / "fixtures" / "live_sanitized"

# Field names that hold a user id: ``Id`` exactly, or anything ending in
# ``UserId`` (``UserId``, ``AttackingUserId``...), optionally ``@``-prefixed as
# xmltodict writes attributes. Design ids such as ``ShipDesignId`` are game data.
_USER_ID_KEY = r"(?<![A-Za-z0-9_])@?(?:[A-Za-z_]*UserId|Id)"
_NUMERIC_USER_ID_PATTERNS = (
    # XML attribute, JSON/dict key or query parameter: UserId="5", "@Id": 5, userId=5
    re.compile(_USER_ID_KEY + r"""["']?\s*[:=]\s*["']?\s*[-+]?\d""", re.IGNORECASE),
    # XML element: <UserId>5</UserId>
    re.compile(r"<" + _USER_ID_KEY + r"""(?:\s[^>]*)?>\s*[-+]?\d""", re.IGNORECASE),
)
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_LONG_HEX_PATTERN = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_SECRET_FIELD_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])@?[A-Za-z_]*(?:token|secret|password|checksum|devicekey)[A-Za-z_]*"
    r"""["']?\s*[:=]\s*["']?(?P<value>[^\s"'&<>,;}\]]+)""",
    re.IGNORECASE,
)
_ALLOWED_SECRET_FIELD_VALUES = {"null", "none"}
_REDACTION_MARKER_PREFIX = "***REDACTED"


class Finding(NamedTuple):
    """One scan hit. Holds where and what kind, never the matched value."""

    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_text(text: str, path: str = "<text>") -> list[Finding]:
    """Return every secret or user-id finding in ``text``."""
    findings = []

    def add(match: re.Match, kind: str) -> None:
        findings.append(Finding(path, _line_of(text, match.start()), kind))

    for index, (pattern, replacement) in enumerate(REDACTION_PATTERNS):
        for match in pattern.finditer(text):
            # A value already reduced to the module's own marker redacts to
            # itself; anything else is something the module would hide.
            if match.expand(replacement) != match.group(0):
                add(match, f"redaction pattern {index}")
    for pattern in _NUMERIC_USER_ID_PATTERNS:
        for match in pattern.finditer(text):
            add(match, "numeric user id")
    for match in _EMAIL_PATTERN.finditer(text):
        add(match, "e-mail address")
    for pattern in (_UUID_PATTERN, _LONG_HEX_PATTERN):
        for match in pattern.finditer(text):
            add(match, "token-like string")
    for match in _SECRET_FIELD_PATTERN.finditer(text):
        value = match.group("value")
        if value.lower() in _ALLOWED_SECRET_FIELD_VALUES or value.startswith(_REDACTION_MARKER_PREFIX):
            continue
        add(match, "token-like string")
    return sorted(set(findings))


def scan_tree(root: Path) -> list[Finding]:
    """Scan every file under ``root`` recursively, hidden files included."""
    findings = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        text = path.read_bytes().decode("utf-8", errors="replace")
        findings.extend(scan_text(text, path.relative_to(root).as_posix()))
    return findings


def _format(findings: list[Finding]) -> str:
    return "\n".join(str(finding) for finding in findings)


_FAKE_JWT = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
_FAKE_BASE64 = "QUFB" * 11

# One planted secret per sdk/redaction.py pattern, in pattern order.
_REDACTION_SAMPLES = (
    f"token: {_FAKE_JWT}",
    f"blob: {_FAKE_BASE64}",
    f'<UserLogin accessToken="{SYNTHETIC_ACCESS_TOKEN}" />',
    f'<UserLogin refreshToken="{SYNTHETIC_ACCESS_TOKEN}" />',
    f'{{"DeviceKey":"{SYNTHETIC_DEVICE_KEY_IOS}"}}',
    f'{{"RefreshToken":"{SYNTHETIC_ACCESS_TOKEN}"}}',
    f'{{"Email":"{SYNTHETIC_EMAIL}"}}',
    '{"password":"synthetic-password"}',
    f'{{"email": "{SYNTHETIC_EMAIL}"}}',
    "GET /UserService/Ping?accessToken=synthetic",
    "GET /UserService/Ping?refreshToken=synthetic",
    "GET /UserService/Ping?deviceKey=synthetic",
    f"contact {SYNTHETIC_EMAIL}",
    "GET /UserService/Ping?password=synthetic",
    "Authorization: Bearer synthetic",
)

_NUMERIC_ID_SAMPLES = (
    '<User UserId="4242" />',
    '<User Id="7" Name="PLAYER-A" />',
    "<Battle AttackingUserId='31' DefendingUserId=\"USER-B\" />",
    "<UserId>4242</UserId>",
    '<Id kind="user"> 7 </Id>',
    '{"@Id": "7"}',
    '{"@UserId": "4242"}',
    '{"UserId": 4242}',
    '{"id": 7}',
    "GET /UserService/ListFriends?userId=4242",
    "USERID = 4242",
)

_EMAIL_SAMPLES = (
    '<User Email="someone@mail.example" />',
    "reach the owner at first.last+pss@sub.mail.example",
)

_TOKEN_SAMPLES = (
    f"session {SYNTHETIC_ACCESS_TOKEN}",
    f"<Device Key='{SYNTHETIC_DEVICE_KEY_MAC}' />",
    f"md5 {SYNTHETIC_IOS_CHECKSUM_1}",
    '<UserLogin DeviceKey="synthetic" />',
    '<Request Checksum="synthetic" />',
    '{"@accessToken": "synthetic"}',
    "client_secret: synthetic",
)

# A capture sanitized the way the README describes: placeholders, design ids,
# redaction markers and emptied token fields.
_CLEAN_XML = """<?xml version="1.0" encoding="utf-8"?>
<UserService>
  <UserLogin UserId="USER-A" accessToken="" refreshToken="***REDACTED***">
    <User Id="USER-A" Name="PLAYER-A" Trophy="1534" Credits="250" ShipDesignId="17"
          AllianceName="ALLIANCE-A" LastLoginDate="2026-11-11T00:00:00" />
  </UserLogin>
  <PVPBattles>
    <PVPBattle BattleId="BATTLE-1" AttackingUserId="USER-A" DefendingUserId="USER-B"
               ItemDesignId="3" RoomDesignId="12" TrophyChange="12" HullLevel="10" />
  </PVPBattles>
</UserService>
"""

_CLEAN_JSON = """{
  "@Id": "USER-B",
  "@UserId": "USER-B",
  "@ShipDesignId": "17",
  "@characterId": "5",
  "Email": "***REDACTED_EMAIL***",
  "DeviceKey": null,
  "Checksum": "",
  "note": "accessToken=***REDACTED*** game version 0.999.0"
}
"""


class TestLiveSanitizedFixtureScan(unittest.TestCase):
    def _scan_planted(self, files: dict[str, str]) -> list[Finding]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            return scan_tree(root)

    def _assert_detected(self, samples: tuple[str, ...], kind: str) -> None:
        for sample in samples:
            with self.subTest(sample=sample):
                findings = self._scan_planted({"nested/capture.xml": sample})
                kinds = {finding.kind for finding in findings}
                self.assertTrue(
                    any(k.startswith(kind) for k in kinds),
                    f"expected a {kind!r} finding, got {kinds}",
                )
                self.assertEqual({"nested/capture.xml"}, {f.path for f in findings})

    # Verifies: S03-R2
    def test_real_live_sanitized_directory_passes(self):
        self.assertTrue((LIVE_SANITIZED_DIR / "README.md").is_file())
        findings = scan_tree(LIVE_SANITIZED_DIR)
        self.assertEqual([], findings, "\n" + _format(findings))

    # Verifies: S03-R2
    def test_clean_sanitized_tree_passes(self):
        findings = self._scan_planted(
            {
                "README.md": (LIVE_SANITIZED_DIR / "README.md").read_text(encoding="utf-8"),
                "profile.xml": _CLEAN_XML,
                "battles/list_pvp_battles2.json": _CLEAN_JSON,
            }
        )
        self.assertEqual([], findings, "\n" + _format(findings))

    # Verifies: S03-R2
    def test_every_redaction_pattern_is_detected(self):
        self.assertEqual(len(REDACTION_PATTERNS), len(_REDACTION_SAMPLES))
        hit = set()
        for index, sample in enumerate(_REDACTION_SAMPLES):
            with self.subTest(pattern=index):
                kinds = {f.kind for f in self._scan_planted({"capture.txt": sample})}
                self.assertIn(f"redaction pattern {index}", kinds)
                hit.update(k for k in kinds if k.startswith("redaction pattern"))
        self.assertEqual({f"redaction pattern {i}" for i in range(len(REDACTION_PATTERNS))}, hit)

    # Verifies: S03-R2
    def test_numeric_user_ids_are_detected(self):
        self._assert_detected(_NUMERIC_ID_SAMPLES, "numeric user id")

    # Verifies: S03-R2
    def test_email_addresses_are_detected(self):
        self._assert_detected(_EMAIL_SAMPLES, "e-mail address")

    # Verifies: S03-R2
    def test_token_like_strings_are_detected(self):
        self._assert_detected(_TOKEN_SAMPLES, "token-like string")

    # Verifies: S03-R2
    def test_scan_reads_every_file_recursively(self):
        findings = self._scan_planted(
            {
                "profile.xml": _CLEAN_XML,
                "a/b/c/.hidden": '<User UserId="4242" />',
                "a/no_extension": f"session {SYNTHETIC_ACCESS_TOKEN}",
            }
        )
        self.assertEqual(
            [("a/b/c/.hidden", 1, "numeric user id"), ("a/no_extension", 1, "token-like string")],
            [tuple(f) for f in findings],
        )

    # Verifies: S03-R2
    def test_findings_report_location_but_never_the_value(self):
        findings = self._scan_planted(
            {"capture.xml": f'<x/>\n<UserLogin UserId="4242" accessToken="{SYNTHETIC_ACCESS_TOKEN}" />\n'}
        )
        self.assertTrue(findings)
        self.assertEqual({2}, {f.line for f in findings})
        message = _format(findings)
        self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, message)
        self.assertNotIn("4242", message)


if __name__ == "__main__":
    unittest.main()
