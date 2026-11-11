"""Tests for sdk.fleet.authorization: the live execution authorization file. Synthetic files only."""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.fleet import registry_from_dict
from sdk.fleet.authorization import (
    Authorization,
    AuthorizationError,
    authorization_from_dict,
    authorization_hash,
    check_authorization,
    load_authorization,
)
from tests.synthetic_fixtures import SYNTHETIC_ACCESS_TOKEN, SYNTHETIC_EMAIL

NOT_BEFORE = datetime(2026, 11, 12, tzinfo=timezone.utc)
INSIDE = NOT_BEFORE + timedelta(days=1)


def payload(**overrides):
    base = {
        "version": 1,
        "owner": "owner",
        "purpose": "canary on the test account",
        "accounts": ["alpha", "bravo"],
        "actions": ["collect_all_resources", "collect_daily_reward"],
        "not_before": "2026-11-12T00:00:00Z",
        "expires": "2026-11-19T00:00:00Z",
        "caps": {"alpha": {"minerals": 1000, "gas": 500}, "bravo": {"minerals": 0, "gas": 0}},
        "modes": ["canary"],
    }
    base.update(overrides)
    return base


def registry():
    def entry(alias, enabled=True):
        return {"alias": alias, "objective": "grow", "policy_id": "baseline-v1",
                "credentials_secret": f"TACHIKOMA_{alias.upper()}_AUTH", "enabled": enabled}
    return registry_from_dict({"version": 1, "accounts": [entry("alpha"), entry("bravo"), entry("charlie", False)]})


class LoadTests(unittest.TestCase):
    # Verifies: S05-R2
    def test_valid_authorization_loads(self):
        auth = authorization_from_dict(payload())
        self.assertIsInstance(auth, Authorization)
        self.assertEqual(auth.accounts, ("alpha", "bravo"))
        self.assertEqual(auth.not_before, NOT_BEFORE)
        self.assertEqual(auth.cap("alpha", "minerals"), 1000)
        self.assertEqual(auth.cap("bravo", "gas"), 0)
        self.assertEqual(auth.modes, ("canary",))
        with self.assertRaises(Exception):
            auth.owner = "someone"  # frozen

    # Verifies: S05-R2
    def test_load_from_file_and_fail_closed_on_bad_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "auth.json"
            good.write_text(json.dumps(payload()), encoding="utf-8")
            self.assertEqual(load_authorization(good).owner, "owner")
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            for path in (broken, Path(tmp) / "missing.json"):
                with self.subTest(path=path.name), self.assertRaises(AuthorizationError):
                    load_authorization(path)
            for bad in ([], "x", None):
                with self.subTest(bad=bad), self.assertRaises(AuthorizationError):
                    authorization_from_dict(bad)

    # Verifies: S05-R2
    def test_unknown_and_missing_fields_are_refused(self):
        with self.assertRaises(AuthorizationError):
            authorization_from_dict(payload(extra="x"))
        for name in payload():
            broken = payload()
            del broken[name]
            with self.subTest(missing=name), self.assertRaises(AuthorizationError):
                authorization_from_dict(broken)
        for version in (2, "1", True):
            with self.subTest(version=version), self.assertRaises(AuthorizationError):
                authorization_from_dict(payload(version=version))

    # Verifies: S05-R2
    def test_owner_must_be_a_short_label(self):
        authorization_from_dict(payload(owner="Fleet Owner-1"))
        for owner in (SYNTHETIC_EMAIL, "owner@host", "", " lead", "x" * 65, 7, None, SYNTHETIC_ACCESS_TOKEN,
                      "A" * 48):
            with self.subTest(owner=owner):
                with self.assertRaises(AuthorizationError) as caught:
                    authorization_from_dict(payload(owner=owner))
                if isinstance(owner, str) and owner:
                    self.assertNotIn(owner, str(caught.exception))

    # Verifies: S05-R2
    def test_aliases_must_match_the_alias_rule(self):
        for accounts in ([], ["Alpha"], [SYNTHETIC_EMAIL], ["alpha", "alpha"], ["has space"], [1], "alpha"):
            with self.subTest(accounts=accounts), self.assertRaises(AuthorizationError):
                authorization_from_dict(payload(accounts=accounts, caps={}))

    # Verifies: S05-R2
    def test_actions_must_be_catalog_ids(self):
        for actions in ([], ["buy_starbux"], ["collect_all_resources", "collect_all_resources"],
                        ["Collect_All_Resources"], [None]):
            with self.subTest(actions=actions), self.assertRaises(AuthorizationError):
                authorization_from_dict(payload(actions=actions))
        auth = authorization_from_dict(payload(actions=["upgrade_room"]))
        self.assertEqual(auth.actions, ("upgrade_room",))

    # Verifies: S05-R2
    def test_window_must_be_utc_and_at_most_fourteen_days(self):
        ok = authorization_from_dict(payload(not_before="2026-11-12T00:00:00+00:00",
                                             expires="2026-11-26T00:00:00Z"))
        self.assertEqual(ok.expires - ok.not_before, timedelta(days=14))
        bad = [
            {"not_before": "2026-11-12T00:00:00"},  # naive
            {"expires": "2026-11-19T00:00:00"},
            {"not_before": "2026-11-12T00:00:00+01:00"},  # aware, not UTC
            {"expires": "2026-11-26T00:00:01Z"},  # 14 days and a second
            {"expires": "2026-11-12T00:00:00Z"},  # equal
            {"expires": "2026-11-11T00:00:00Z"},  # before
            {"not_before": "yesterday"},
            {"not_before": 1700000000},
            {"expires": ""},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(AuthorizationError):
                authorization_from_dict(payload(**overrides))

    # Verifies: S05-R2
    def test_caps_are_per_account_non_negative_integers(self):
        caps = payload()["caps"]
        bad = []
        for alias, currency, value in (("alpha", "minerals", -1), ("alpha", "gas", 1.5),
                                       ("alpha", "gas", True), ("bravo", "minerals", "10"),
                                       ("bravo", "gas", None)):
            broken = copy.deepcopy(caps)
            broken[alias][currency] = value
            bad.append(broken)
        missing_currency = copy.deepcopy(caps)
        del missing_currency["alpha"]["gas"]
        missing_account = copy.deepcopy(caps)
        del missing_account["bravo"]
        extra_account = copy.deepcopy(caps)
        extra_account["charlie"] = {"minerals": 0, "gas": 0}
        unknown_currency = copy.deepcopy(caps)
        unknown_currency["alpha"]["diamonds"] = 1
        bad += [missing_currency, missing_account, extra_account, unknown_currency, [], {"alpha": 5, "bravo": {}}]
        for value in bad:
            with self.subTest(caps=value), self.assertRaises(AuthorizationError):
                authorization_from_dict(payload(caps=value))

    # Verifies: S05-R2
    def test_starbux_is_never_authorizable(self):
        caps = copy.deepcopy(payload()["caps"])
        for value in (0, 100):
            caps["alpha"]["starbux"] = value
            with self.subTest(value=value), self.assertRaises(AuthorizationError) as caught:
                authorization_from_dict(payload(caps=caps))
            self.assertIn("starbux", str(caught.exception))
        with self.assertRaises(AuthorizationError):
            authorization_from_dict(payload(starbux=0))
        self.assertEqual(authorization_from_dict(payload()).cap("alpha", "starbux"), 0)

    # Verifies: S05-R2
    def test_modes_must_be_a_subset_of_canary_and_full(self):
        self.assertEqual(authorization_from_dict(payload(modes=["canary", "full"])).modes, ("canary", "full"))
        for modes in ([], ["live"], ["canary", "canary"], ["CANARY"], "canary"):
            with self.subTest(modes=modes), self.assertRaises(AuthorizationError):
                authorization_from_dict(payload(modes=modes))

    # Verifies: S05-R2
    def test_purpose_is_free_text_without_secrets(self):
        for purpose in ("", "   ", "x" * 501, None, f"contact {SYNTHETIC_EMAIL}", f"use {'Q' * 48}"):
            with self.subTest(purpose=purpose), self.assertRaises(AuthorizationError) as caught:
                authorization_from_dict(payload(purpose=purpose))
            self.assertNotIn(SYNTHETIC_EMAIL, str(caught.exception))

    # Verifies: S05-R2
    def test_secret_named_keys_anywhere_are_refused(self):
        nested = payload()
        nested["caps"]["alpha"]["accessToken"] = SYNTHETIC_ACCESS_TOKEN
        cases = [payload(password="x"), payload(email=SYNTHETIC_EMAIL), payload(credentials="x"), nested,
                 payload(accounts=["alpha", "bravo"], caps={"alpha": {"minerals": 0, "gas": 0},
                                                            "bravo": {"minerals": 0, "gas": 0, "Token": "x"}})]
        for case in cases:
            with self.subTest(keys=sorted(case)), self.assertRaises(AuthorizationError) as caught:
                authorization_from_dict(case)
            self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, str(caught.exception))
            self.assertNotIn(SYNTHETIC_EMAIL, str(caught.exception))


class HashTests(unittest.TestCase):
    # Verifies: S05-R2
    def test_hash_is_canonical_and_changes_with_content(self):
        first = authorization_hash(authorization_from_dict(payload()))
        self.assertRegex(first, r"^sha256:[0-9a-f]{64}$")
        reordered = dict(reversed(list(payload().items())))
        reordered["not_before"] = "2026-11-12T00:00:00+00:00"
        self.assertEqual(authorization_hash(authorization_from_dict(reordered)), first)
        self.assertNotEqual(authorization_hash(authorization_from_dict(payload(purpose="other"))), first)
        self.assertNotEqual(authorization_hash(authorization_from_dict(payload(modes=["canary", "full"]))), first)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.auth = authorization_from_dict(payload(
            accounts=["alpha", "bravo", "charlie"],
            caps={alias: {"minerals": 0, "gas": 0} for alias in ("alpha", "bravo", "charlie")},
        ))
        self.registry = registry()

    def check(self, now=INSIDE, aliases=("alpha",), actions=("collect_all_resources",), mode="canary"):
        return check_authorization(self.auth, self.registry, now, aliases, actions, mode)

    # Verifies: S05-R2
    def test_inside_the_authorization_has_no_refusals(self):
        self.assertEqual(self.check(), [])
        self.assertEqual(self.check(now=NOT_BEFORE, aliases=("alpha", "bravo"),
                                    actions=("collect_all_resources", "collect_daily_reward")), [])

    # Verifies: S05-R2
    def test_time_outside_the_window_is_refused(self):
        expires = NOT_BEFORE + timedelta(days=7)
        for now in (NOT_BEFORE - timedelta(seconds=1), expires, expires + timedelta(days=1),
                    datetime(2026, 11, 13), "2026-11-13T00:00:00Z"):
            with self.subTest(now=now):
                self.assertEqual(len(self.check(now=now)), 1)
        self.assertIn("expired", self.check(now=expires)[0])
        self.assertIn("not valid yet", self.check(now=NOT_BEFORE - timedelta(seconds=1))[0])
        # Another time zone at the same instant is inside the window.
        self.assertEqual(self.check(now=INSIDE.astimezone(timezone(timedelta(hours=-5)))), [])

    # Verifies: S05-R2
    def test_accounts_outside_the_authorization_or_registry_are_refused(self):
        other = authorization_from_dict(payload(accounts=["alpha", "delta"],
                                                caps={"alpha": {"minerals": 0, "gas": 0},
                                                      "delta": {"minerals": 0, "gas": 0}}))
        self.assertIn("not in the registry",
                      check_authorization(other, self.registry, INSIDE, ["delta"], [], "canary")[0])
        self.assertIn("not authorized",
                      check_authorization(other, self.registry, INSIDE, ["bravo"], [], "canary")[0])
        self.assertIn("disabled", self.check(aliases=["charlie"])[0])
        self.assertIn("no account", self.check(aliases=[])[0])
        reasons = self.check(aliases=[SYNTHETIC_EMAIL, 5])
        self.assertEqual(len(reasons), 2)
        self.assertNotIn(SYNTHETIC_EMAIL, " ".join(reasons))

    # Verifies: S05-R2
    def test_actions_and_modes_outside_the_authorization_are_refused(self):
        self.assertIn("upgrade_room", self.check(actions=["upgrade_room"])[0])
        self.assertIn("not allowed", self.check(actions=["buy_starbux"])[0])
        self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, " ".join(self.check(actions=[SYNTHETIC_ACCESS_TOKEN])))
        self.assertIn("mode", self.check(mode="full")[0])
        self.assertIn("mode", self.check(mode="live")[0])

    # Verifies: S05-R2
    def test_every_refusal_is_reported(self):
        reasons = self.check(now=NOT_BEFORE - timedelta(days=1), aliases=["charlie", "zulu"],
                             actions=["upgrade_room"], mode="full")
        self.assertEqual(len(reasons), 5)


if __name__ == "__main__":
    unittest.main()
