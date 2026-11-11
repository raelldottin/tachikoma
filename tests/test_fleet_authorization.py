"""Tests for sdk.fleet.authorization: the live execution authorization file. Synthetic files only."""

import copy
import dataclasses
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.fleet import registry_from_dict
from sdk.fleet.authorization import (
    Authorization,
    AuthorizationError,
    TradeRecord,
    authorization_from_dict,
    authorization_hash,
    battle_cap_refusal,
    battle_fits_cap,
    battles_in_window,
    check_authorization,
    check_trade,
    load_authorization,
)
from sdk.journal import GameplayEvent
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


def legacy_registry():
    def entry(alias, legacy=None):
        base = {"alias": alias, "objective": "grow", "policy_id": "baseline-v1",
                "credentials_secret": f"TACHIKOMA_{alias.upper()}_AUTH"}
        if legacy is not None:
            base["legacy_run_account"] = legacy
        return base
    return registry_from_dict({"version": 1, "accounts": [entry("alpha", 2), entry("bravo")]})


class LegacyRunTests(unittest.TestCase):
    # Verifies: S06-R2
    def test_legacy_run_skipped_is_optional_and_validated(self):
        self.assertEqual(authorization_from_dict(payload()).legacy_run_skipped, ())
        self.assertEqual(authorization_from_dict(payload(legacy_run_skipped=[2, 3])).legacy_run_skipped, (2, 3))
        for value in ([0], [-1], ["2"], [True], [2, 2], 2, "2", None, [1.5]):
            with self.subTest(value=value), self.assertRaises(AuthorizationError):
                authorization_from_dict(payload(legacy_run_skipped=value))

    # Verifies: S06-R2
    def test_old_files_load_and_hash_as_before(self):
        auth = authorization_from_dict(payload())
        self.assertNotIn("legacy_run_skipped", auth.to_dict())
        self.assertNotIn("battles", auth.to_dict())
        self.assertNotIn("market", auth.to_dict())
        self.assertEqual(authorization_hash(auth), authorization_hash(authorization_from_dict(payload(
            legacy_run_skipped=[]))))
        self.assertNotEqual(authorization_hash(auth), authorization_hash(authorization_from_dict(payload(
            legacy_run_skipped=[2]))))
        self.assertEqual(authorization_from_dict(auth.to_dict()), auth)

    # Verifies: S06-R2
    def test_execute_is_refused_unless_the_legacy_account_is_confirmed_skipped(self):
        # S06-AC1: a profile with legacy_run_account not confirmed as skipped is refused; accepted when confirmed.
        reg = legacy_registry()
        for skipped in (None, [], [1, 3]):
            extra = {} if skipped is None else {"legacy_run_skipped": skipped}
            auth = authorization_from_dict(payload(**extra))
            with self.subTest(skipped=skipped):
                reasons = check_authorization(auth, reg, INSIDE, ["alpha"], ["collect_all_resources"], "canary")
                self.assertEqual(len(reasons), 1)
                self.assertIn("legacy daily run account 2", reasons[0])
                # An account with no legacy number is unaffected.
                self.assertEqual(
                    check_authorization(auth, reg, INSIDE, ["bravo"], ["collect_all_resources"], "canary"), [])
        confirmed = authorization_from_dict(payload(legacy_run_skipped=[2]))
        self.assertEqual(check_authorization(confirmed, reg, INSIDE, ["alpha", "bravo"],
                                             ["collect_all_resources"], "canary"), [])


def battle_payload(**overrides):
    base = payload(actions=["collect_all_resources", "run_pvp_battle"],
                   battles={"max_per_window": 2, "window_hours": 24})
    base.update(overrides)
    return base


def battle_event(alias="alpha", started=INSIDE, attempted=True, dry_run=False, action_id="run_pvp_battle",
                 outcome="unknown"):
    if dry_run:
        attempted = False
    return GameplayEvent.create(account_alias=alias, action_id=action_id, started_at=started,
                                finished_at=started, outcome=outcome, reason="test", attempted=attempted,
                                dry_run=dry_run)


class BattleCapTests(unittest.TestCase):
    # Verifies: S08-R2
    def test_battles_section_is_required_exactly_with_run_pvp_battle(self):
        auth = authorization_from_dict(battle_payload())
        self.assertEqual((auth.battles.max_per_window, auth.battles.window_hours), (2, 24))
        self.assertEqual(authorization_from_dict(auth.to_dict()), auth)
        with self.assertRaises(AuthorizationError) as caught:
            authorization_from_dict(payload(actions=["run_pvp_battle"]))
        self.assertIn("battles", str(caught.exception))
        with self.assertRaises(AuthorizationError):
            authorization_from_dict(payload(battles={"max_per_window": 2, "window_hours": 24}))

    # Verifies: S08-R2
    def test_battles_section_is_validated(self):
        bad = [
            {"max_per_window": 0, "window_hours": 24}, {"max_per_window": -1, "window_hours": 24},
            {"max_per_window": True, "window_hours": 24}, {"max_per_window": "10", "window_hours": 24},
            {"max_per_window": 10, "window_hours": 0}, {"max_per_window": 10, "window_hours": 337},
            {"max_per_window": 10}, {"max_per_window": 10, "window_hours": 24, "extra": 1},
            {"max_per_window": 10, "window_hours": 24, "starbux": 0}, [], None,
        ]
        for battles in bad:
            with self.subTest(battles=battles), self.assertRaises(AuthorizationError):
                authorization_from_dict(battle_payload(battles=battles))
        self.assertEqual(authorization_from_dict(battle_payload(
            battles={"max_per_window": 10, "window_hours": 336})).battles.window_hours, 336)

    # Verifies: S08-R2
    def test_an_authorization_without_run_pvp_battle_refuses_battles(self):
        # S08-AC3, first half.
        auth = authorization_from_dict(payload())
        reasons = check_authorization(auth, registry(), INSIDE, ["alpha"], ["run_pvp_battle"], "canary",
                                      battle_events=[])
        self.assertEqual(len(reasons), 1)
        self.assertIn("run_pvp_battle", reasons[0])
        self.assertFalse(battle_fits_cap(auth, "alpha", [], INSIDE))
        self.assertIn("not allowed", battle_cap_refusal(auth, "alpha", [], INSIDE))

    # Verifies: S08-R2
    def test_the_cap_stops_further_battles_in_the_window(self):
        # S08-AC3, second half.
        auth = authorization_from_dict(battle_payload())
        now = INSIDE + timedelta(hours=12)
        self.assertTrue(battle_fits_cap(auth, "alpha", [], now))
        one = [battle_event(started=now - timedelta(hours=1))]
        self.assertTrue(battle_fits_cap(auth, "alpha", one, now))
        self.assertEqual(battles_in_window(auth, "alpha", one, now), 1)
        two = one + [battle_event(started=now - timedelta(hours=23), outcome="verified")]
        self.assertFalse(battle_fits_cap(auth, "alpha", two, now))
        self.assertIn("2 of 2", battle_cap_refusal(auth, "alpha", two, now))
        # A battle that started exactly window_hours ago has left the rolling window.
        aged = one + [battle_event(started=now - timedelta(hours=24))]
        self.assertTrue(battle_fits_cap(auth, "alpha", aged, now))

    # Verifies: S08-R2
    def test_only_attempted_battles_of_the_same_account_count(self):
        auth = authorization_from_dict(battle_payload())
        now = INSIDE
        noise = [
            battle_event(alias="bravo"), battle_event(alias="bravo"),
            battle_event(dry_run=True), battle_event(attempted=False, outcome="skipped"),
            battle_event(action_id="collect_all_resources", outcome="verified"),
            battle_event(),
        ]
        self.assertEqual(battles_in_window(auth, "alpha", noise, now), 1)
        self.assertTrue(battle_fits_cap(auth, "alpha", noise, now))
        self.assertFalse(battle_fits_cap(auth, "bravo", noise, now))
        self.assertFalse(battle_fits_cap(auth, "zulu", [], now))
        self.assertFalse(battle_fits_cap(auth, "alpha", [], datetime(2026, 11, 13)))

    # Verifies: S08-R2
    def test_unreadable_or_future_battle_times_fill_the_cap(self):
        auth = authorization_from_dict(battle_payload())
        future = battle_event(started=INSIDE + timedelta(days=3))
        unreadable = dataclasses.replace(battle_event(), started_at="not a time")
        naive = dataclasses.replace(battle_event(), started_at="2026-11-01T00:00:00")
        self.assertEqual(battles_in_window(auth, "alpha", [future, unreadable, naive], INSIDE), 3)
        self.assertFalse(battle_fits_cap(auth, "alpha", [future, unreadable], INSIDE))

    # Verifies: S08-R2
    def test_check_authorization_refuses_an_account_whose_cap_is_full(self):
        auth = authorization_from_dict(battle_payload())
        full = [battle_event(), battle_event()]
        reg = registry()
        actions = ["run_pvp_battle"]
        self.assertEqual(check_authorization(auth, reg, INSIDE, ["alpha", "bravo"], actions, "canary",
                                             battle_events=full), [check_authorization(
                                                 auth, reg, INSIDE, ["alpha"], actions, "canary",
                                                 battle_events=full)[0]])
        self.assertIn("alpha", check_authorization(auth, reg, INSIDE, ["alpha"], actions, "canary",
                                                   battle_events=full)[0])
        # Without events (the SPEC-05 call) or without a battle request, the cap is not consulted.
        self.assertEqual(check_authorization(auth, reg, INSIDE, ["alpha"], actions, "canary"), [])
        self.assertEqual(check_authorization(auth, reg, INSIDE, ["alpha"], ["collect_all_resources"], "canary",
                                             battle_events=full), [])


def market_payload(**overrides):
    base = payload(actions=["sell_item", "purchase_item"],
                   market={"item_design_ids": [101, 202], "max_value_per_trade": 500,
                           "max_value_per_window": 800, "window_hours": 24})
    base.update(overrides)
    return base


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.auth = authorization_from_dict(market_payload())

    # Verifies: S09-R4
    def test_market_actions_need_a_market_section(self):
        self.assertEqual(self.auth.market.item_design_ids, (101, 202))
        self.assertEqual(authorization_from_dict(self.auth.to_dict()), self.auth)
        for actions in (["sell_item"], ["purchase_item"], ["collect_all_resources", "sell_item"]):
            with self.subTest(actions=actions), self.assertRaises(AuthorizationError):
                authorization_from_dict(payload(actions=actions))
        # A market section with no market action is refused too.
        with self.assertRaises(AuthorizationError):
            authorization_from_dict(market_payload(actions=["collect_all_resources"]))
        self.assertEqual(authorization_from_dict(market_payload(actions=["sell_item"])).actions, ("sell_item",))
        # Other unknown actions stay refused with a market section.
        with self.assertRaises(AuthorizationError):
            authorization_from_dict(market_payload(actions=["sell_item", "buy_starbux"]))

    # Verifies: S09-R4
    def test_market_section_is_validated(self):
        good = market_payload()["market"]
        bad = []
        for name, value in (("item_design_ids", []), ("item_design_ids", [101, 101]),
                            ("item_design_ids", ["101"]), ("item_design_ids", [0]), ("item_design_ids", [True]),
                            ("item_design_ids", 101), ("max_value_per_trade", 0), ("max_value_per_trade", 1.5),
                            ("max_value_per_trade", 900), ("max_value_per_window", -1),
                            ("window_hours", 0), ("window_hours", 400)):
            broken = dict(good)
            broken[name] = value
            bad.append(broken)
        missing = dict(good)
        del missing["max_value_per_window"]
        bad += [missing, {**good, "extra": 1}, {**good, "starbux": 1}, [], None]
        for market in bad:
            with self.subTest(market=market), self.assertRaises(AuthorizationError):
                authorization_from_dict(market_payload(market=market))

    # Verifies: S09-R4
    def test_a_trade_inside_the_item_list_and_caps_is_allowed(self):
        self.assertEqual(check_trade(self.auth, 101, 500, []), [])
        self.assertEqual(check_trade(self.auth, 202, 300, [TradeRecord(INSIDE, 101, 500)], now=INSIDE), [])

    # Verifies: S09-R4
    def test_trades_outside_the_item_list_or_value_caps_are_refused(self):
        # S09-AC3: refused before any call; check_trade makes none.
        self.assertIn("item 303", check_trade(self.auth, 303, 10, [])[0])
        self.assertIn("malformed", check_trade(self.auth, "101", 10, [])[0])
        self.assertIn("max_value_per_trade", check_trade(self.auth, 101, 501, [])[0])
        for value in (0, -5, 1.5, True, "10"):
            with self.subTest(value=value):
                self.assertIn("positive integer", check_trade(self.auth, 101, value, [])[0])
        self.assertIn("unknown", check_trade(self.auth, 101, None, [])[0])
        prior = [TradeRecord(INSIDE - timedelta(hours=1), 101, 400)]
        self.assertIn("max_value_per_window", check_trade(self.auth, 202, 401, prior, now=INSIDE)[0])
        self.assertEqual(check_trade(self.auth, 202, 400, prior, now=INSIDE), [])
        self.assertEqual(len(check_trade(self.auth, 303, 501, [])), 2)

    # Verifies: S09-R4
    def test_the_window_rolls_and_unobserved_prior_values_fail_closed(self):
        old = [TradeRecord(INSIDE - timedelta(hours=24), 101, 500)]
        self.assertEqual(check_trade(self.auth, 101, 500, old, now=INSIDE), [])
        # Without now, every prior trade counts.
        self.assertIn("max_value_per_window", check_trade(self.auth, 101, 500, old)[0])
        unknown = [TradeRecord(INSIDE, 101, None)]
        self.assertIn("unknown value", check_trade(self.auth, 101, 1, unknown, now=INSIDE)[0])
        # An unknown value outside the window is irrelevant; an unreadable time counts as inside.
        aged_unknown = [TradeRecord(INSIDE - timedelta(days=2), 101, None)]
        self.assertEqual(check_trade(self.auth, 101, 1, aged_unknown, now=INSIDE), [])
        unreadable = [TradeRecord("not a time", 101, 500)]
        self.assertIn("max_value_per_window", check_trade(self.auth, 101, 500, unreadable, now=INSIDE)[0])

    # Verifies: S09-R4
    def test_trades_need_market_actions_a_valid_time_and_never_starbux(self):
        plain = authorization_from_dict(payload())
        self.assertEqual(check_trade(plain, 101, 10, []), ["market trades are not allowed by the authorization"])
        self.assertIn("starbux", check_trade(self.auth, 101, 10, [], currency="starbux")[0])
        self.assertEqual(check_trade(self.auth, 101, 10, [], currency="minerals"), [])
        self.assertIn("expired", check_trade(self.auth, 101, 10, [], now=INSIDE + timedelta(days=30))[0])
        self.assertIn("not valid yet", check_trade(self.auth, 101, 10, [], now=NOT_BEFORE - timedelta(seconds=1))[0])
        self.assertIn("timezone", check_trade(self.auth, 101, 10, [], now=datetime(2026, 11, 13))[0])
        self.assertEqual(self.auth.cap("alpha", "starbux"), 0)


if __name__ == "__main__":
    unittest.main()
