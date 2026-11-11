"""Tests for sdk.fleet.accounts: the account registry. Synthetic files in temporary directories."""

import json
import tempfile
import unittest
from pathlib import Path

from sdk.fleet import AccountProfile, AccountRegistry, RegistryError, load_registry, registry_from_dict
from tests.synthetic_fixtures import SYNTHETIC_ACCESS_TOKEN, SYNTHETIC_EMAIL


def entry(**overrides):
    base = {
        "alias": "alpha",
        "objective": "grow hull 8 economy",
        "policy_id": "baseline-v1",
        "pvp_strategy": "weapon-focus-fire@v1",
        "budgets": {"minerals": 50000, "gas": 20000, "starbux": 0},
        "max_actions": 15,
        "enabled": True,
        "credentials_secret": "TACHIKOMA_ALPHA_AUTH",
    }
    base.update(overrides)
    return base


def registry(*entries):
    return registry_from_dict({"version": 1, "accounts": list(entries)})


class ProfileTests(unittest.TestCase):
    def test_valid_profile(self):
        profile = AccountProfile(**entry())
        self.assertEqual(profile.budget("starbux"), 0)
        self.assertEqual(profile.budget("minerals"), 50000)
        self.assertEqual(profile.to_dict()["credentials_secret"], "TACHIKOMA_ALPHA_AUTH")

    def test_defaults_forbid_starbux_spend(self):
        profile = AccountProfile(alias="alpha", objective="x", policy_id="baseline-v1", credentials_secret="A_AUTH")
        self.assertEqual(profile.budget("starbux"), 0)
        self.assertIsNone(profile.budget("minerals"))

    def test_missing_starbux_budget_means_zero(self):
        profile = AccountProfile(**entry(budgets={"minerals": 10}))
        self.assertEqual(profile.budget("starbux"), 0)
        self.assertEqual(profile.budget("minerals"), 10)

    def test_invalid_fields_fail_closed(self):
        bad = [
            dict(alias=SYNTHETIC_EMAIL), dict(alias="Has Caps"),
            dict(objective=""), dict(objective="x" * 201),
            dict(policy_id="yolo-v9"),
            dict(credentials_secret="lowercase"), dict(credentials_secret=SYNTHETIC_ACCESS_TOKEN),
            dict(pvp_strategy="weapon-focus-fire"), dict(pvp_strategy="x@v0"),
            dict(budgets={"diamonds": 5}), dict(budgets={"gas": -1}), dict(budgets={"gas": True}), dict(budgets=[]),
            dict(max_actions=0), dict(max_actions=1.5), dict(enabled="yes"),
            dict(objective=f"reach {SYNTHETIC_EMAIL}"),
        ]
        for overrides in bad:
            with self.subTest(overrides=list(overrides)), self.assertRaises(RegistryError):
                AccountProfile(**entry(**overrides))


class LegacyRunAccountTests(unittest.TestCase):
    # Verifies: S06-R2
    def test_legacy_run_account_is_an_optional_positive_integer(self):
        self.assertIsNone(AccountProfile(**entry()).legacy_run_account)
        profile = AccountProfile(**entry(legacy_run_account=2))
        self.assertEqual(profile.legacy_run_account, 2)
        self.assertEqual(profile.to_dict()["legacy_run_account"], 2)
        again = registry_from_dict({"version": 1, "accounts": [profile.to_dict()]})
        self.assertEqual(again.get("alpha").legacy_run_account, 2)
        for value in (0, -1, 1.0, "2", True, [2]):
            with self.subTest(value=value), self.assertRaises(RegistryError):
                AccountProfile(**entry(legacy_run_account=value))

    # Verifies: S06-R2
    def test_two_aliases_cannot_claim_the_same_legacy_account(self):
        with self.assertRaises(RegistryError) as caught:
            registry(entry(legacy_run_account=3), entry(alias="bravo", legacy_run_account=3))
        self.assertIn("legacy_run_account", str(caught.exception))
        reg = registry(entry(legacy_run_account=3), entry(alias="bravo", legacy_run_account=4), entry(alias="charlie"))
        self.assertEqual([reg.get(a).legacy_run_account for a in reg.aliases()], [3, 4, None])


class RegistryTests(unittest.TestCase):
    def test_lookup_and_enabled(self):
        reg = registry(entry(), entry(alias="bravo", enabled=False, pvp_strategy=None))
        self.assertEqual(reg.aliases(), ("alpha", "bravo"))
        self.assertEqual([p.alias for p in reg.enabled()], ["alpha"])
        self.assertIn("bravo", reg)
        self.assertEqual(len(reg), 2)
        with self.assertRaises(RegistryError):
            reg.get("charlie")

    def test_duplicate_alias_is_refused(self):
        with self.assertRaises(RegistryError):
            registry(entry(), entry())
        with self.assertRaises(RegistryError):
            AccountRegistry([AccountProfile(**entry()), AccountProfile(**entry())])

    def test_file_level_validation(self):
        bad = [
            {"version": 2, "accounts": []},
            {"version": 1},
            {"version": 1, "accounts": {}},
            {"version": 1, "accounts": [], "extra": 1},
            {"version": 1, "accounts": ["alpha"]},
            {"version": 1, "accounts": [entry(nickname="Al")]},
            {"version": 1, "accounts": [{"alias": "alpha"}]},
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(RegistryError):
                registry_from_dict(payload)
        with self.assertRaises(RegistryError):
            registry_from_dict(["not", "an", "object"])

    def test_secrets_anywhere_in_the_file_are_refused(self):
        payloads = [
            {"version": 1, "accounts": [entry(password="hunter2")]},
            {"version": 1, "accounts": [entry()], "notes": {"accessToken": "x"}},
            {"version": 1, "accounts": [entry(objective=f"accessToken={SYNTHETIC_ACCESS_TOKEN}")]},
            {"version": 1, "accounts": [entry(email=SYNTHETIC_EMAIL)]},
        ]
        for payload in payloads:
            with self.subTest(payload=list(payload)), self.assertRaises(RegistryError) as caught:
                registry_from_dict(payload)
            self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, str(caught.exception))
            self.assertNotIn(SYNTHETIC_EMAIL, str(caught.exception))

    def test_load_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fleet.json"
            path.write_text(json.dumps({"version": 1, "accounts": [entry(), entry(alias="bravo")]}))
            self.assertEqual(load_registry(path).aliases(), ("alpha", "bravo"))
            path.write_text("{not json")
            with self.assertRaises(RegistryError):
                load_registry(path)


if __name__ == "__main__":
    unittest.main()
