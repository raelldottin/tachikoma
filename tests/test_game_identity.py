"""Tests for game identity fingerprints: snapshots, registry profiles and fleet refusals.

Synthetic user ids and salts only; no network. The raw ids below must never
appear in a snapshot dict, a journal, an AccountRun reason or error text.
"""

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sdk.fleet import AccountProfile, RegistryError, registry_from_dict
from sdk.fleet.accounts import game_identity_refusal, pin_game_identity
from sdk.fleet.concurrent import ConcurrentFleetOrchestrator
from sdk.fleet.orchestrator import AccountContext, FleetClaims, FleetOrchestrator
from sdk.journal import GameplayJournal
from sdk.state import AccountMismatchError, ClientSnapshotProvider, SnapshotInputError, build_snapshot
from sdk.state.snapshot import GAME_IDENTITY_PATTERN, game_identity_fingerprint
from tests.test_client_snapshot_provider import FakeClient
from tests.test_fleet_concurrent import LockedClock
from tests.test_fleet_orchestrator import DESIGN, FakeGame, profile

T0 = datetime(2027, 4, 1, 6, 0, tzinfo=timezone.utc)
SALT = b"synthetic-fleet-salt-0001"
OTHER_SALT = b"synthetic-fleet-salt-0002"
ALPHA_ID = "700000101"
BRAVO_ID = "700000202"
RAW_IDS = (ALPHA_ID, BRAVO_ID)


def fp(user_id, salt=SALT):
    return game_identity_fingerprint(user_id, salt)


class FingerprintTests(unittest.TestCase):
    def test_format_is_stable_and_keyed(self):
        value = fp(ALPHA_ID)
        self.assertRegex(value, GAME_IDENTITY_PATTERN)
        self.assertEqual(value, fp(ALPHA_ID))
        self.assertEqual(value, fp(int(ALPHA_ID)))
        self.assertEqual(value, fp(" 0" + ALPHA_ID))
        self.assertEqual(value, game_identity_fingerprint(ALPHA_ID, SALT.decode()))
        self.assertNotEqual(value, fp(BRAVO_ID))
        self.assertNotEqual(value, fp(ALPHA_ID, OTHER_SALT))
        self.assertNotIn(ALPHA_ID, value)

    def test_unknown_ids_are_refused_without_echo(self):
        for user_id in (None, "", "0", 0, "000", True, "abc", "12a", "-5", "1" * 21):
            with self.assertRaises(SnapshotInputError, msg=repr(user_id)) as caught:
                fp(user_id)
            self.assertNotIn("abc", str(caught.exception))

    def test_short_or_missing_salt_is_refused_without_echo(self):
        for salt in (None, b"", b"short-salt", "short", 12345):
            with self.assertRaises(SnapshotInputError) as caught:
                fp(ALPHA_ID, salt)
            self.assertNotIn(ALPHA_ID, str(caught.exception))
            self.assertNotIn("short", str(caught.exception))


class SnapshotFieldTests(unittest.TestCase):
    def test_snapshot_carries_the_fingerprint(self):
        snapshot = build_snapshot("alpha", T0, "test", game_identity=fp(ALPHA_ID))
        self.assertEqual(snapshot.game_identity, fp(ALPHA_ID))
        self.assertEqual(snapshot.to_dict()["game_identity"], fp(ALPHA_ID))

    def test_old_snapshots_without_identity_still_work(self):
        snapshot = build_snapshot("alpha", T0, "test")
        self.assertIsNone(snapshot.game_identity)
        self.assertIsNone(snapshot.to_dict()["game_identity"])

    def test_raw_id_is_refused_as_identity(self):
        for value in (ALPHA_ID, int(ALPHA_ID), "gid1-" + ALPHA_ID, fp(ALPHA_ID).upper()):
            with self.assertRaises(SnapshotInputError) as caught:
                build_snapshot("alpha", T0, "test", game_identity=value)
            self.assertNotIn(ALPHA_ID, str(caught.exception))


class ProviderTests(unittest.TestCase):
    def client(self, user_id=ALPHA_ID):
        client = FakeClient()
        client.user = SimpleNamespace(id=user_id, name="Synthetic", isAuthorized=True)
        return client

    def test_provider_fingerprints_the_logged_in_user(self):
        client = self.client()
        snapshot = ClientSnapshotProvider(client, "alpha", clock=lambda: T0, identity_salt=SALT)()
        self.assertEqual(snapshot.game_identity, fp(ALPHA_ID))
        self.assertEqual(client.calls, ["getShipByUserId", "listAllCharactersOfUser", "listItemsOfAShip"])
        self.assertNotIn(ALPHA_ID, json.dumps(snapshot.to_dict()))
        self.assertNotIn(ALPHA_ID, repr(snapshot))

    def test_no_salt_or_unknown_user_leaves_identity_unknown(self):
        self.assertIsNone(ClientSnapshotProvider(self.client(), "alpha", clock=lambda: T0)().game_identity)
        for user_id in ("0", None, "not-a-number"):
            provider = ClientSnapshotProvider(self.client(user_id), "alpha", clock=lambda: T0, identity_salt=SALT)
            self.assertIsNone(provider().game_identity)
        no_user = ClientSnapshotProvider(FakeClient(), "alpha", clock=lambda: T0, identity_salt=SALT)
        self.assertIsNone(no_user().game_identity)

    def test_short_salt_fails_at_construction(self):
        with self.assertRaises(SnapshotInputError):
            ClientSnapshotProvider(self.client(), "alpha", identity_salt=b"short")

    def test_provider_keeps_no_raw_id(self):
        provider = ClientSnapshotProvider(self.client(), "alpha", clock=lambda: T0, identity_salt=SALT)
        provider()
        self.assertNotIn(ALPHA_ID, repr(vars(provider)))


class ProfileTests(unittest.TestCase):
    def test_profile_declares_expected_identity(self):
        registry = registry_from_dict({"version": 1, "accounts": [
            profile("alpha", expected_game_identity=fp(ALPHA_ID)), profile("bravo")]})
        self.assertEqual(registry.get("alpha").expected_game_identity, fp(ALPHA_ID))
        self.assertIsNone(registry.get("bravo").expected_game_identity)
        self.assertEqual(registry.get("alpha").to_dict()["expected_game_identity"], fp(ALPHA_ID))
        again = registry_from_dict({"version": 1, "accounts": [registry.get("alpha").to_dict()]})
        self.assertEqual(again.get("alpha"), registry.get("alpha"))

    def test_raw_or_malformed_identity_is_refused_without_echo(self):
        for value in (ALPHA_ID, int(ALPHA_ID), "gid1-xyz", ""):
            with self.assertRaises(RegistryError) as caught:
                AccountProfile("alpha", "grow", "baseline-v1", "ALPHA_AUTH", expected_game_identity=value)
            self.assertNotIn(ALPHA_ID, str(caught.exception))

    def test_two_profiles_may_not_declare_one_identity(self):
        with self.assertRaises(RegistryError) as caught:
            registry_from_dict({"version": 1, "accounts": [
                profile("alpha", expected_game_identity=fp(ALPHA_ID)),
                profile("bravo", expected_game_identity=fp(ALPHA_ID), enabled=False)]})
        self.assertIn("already declared by 'alpha'", str(caught.exception))
        registry = registry_from_dict({"version": 1, "accounts": [
            profile("alpha", expected_game_identity=fp(ALPHA_ID)), profile("bravo"),
            profile("charlie", expected_game_identity=fp(BRAVO_ID), enabled=False)]})
        self.assertEqual(registry.declared_identities(), {fp(ALPHA_ID): "alpha", fp(BRAVO_ID): "charlie"})

    def test_refusal_rules(self):
        declared = {fp(ALPHA_ID): "alpha"}
        self.assertIsNone(game_identity_refusal("alpha", fp(ALPHA_ID), fp(ALPHA_ID), {}, declared))
        self.assertIn("unknown", game_identity_refusal("alpha", fp(ALPHA_ID), None, {}))
        self.assertIn("does not match", game_identity_refusal("alpha", fp(ALPHA_ID), fp(BRAVO_ID), {}))
        self.assertIsNone(game_identity_refusal("bravo", None, None, {}, declared))
        self.assertIsNone(game_identity_refusal("bravo", None, fp(BRAVO_ID), {fp(BRAVO_ID): "bravo"}, declared))
        self.assertIn("declared for 'alpha' in the registry",
                      game_identity_refusal("bravo", None, fp(ALPHA_ID), {}, declared))
        self.assertIn("already observed for 'alpha'",
                      game_identity_refusal("bravo", None, fp(ALPHA_ID), {fp(ALPHA_ID): "alpha"}))
        self.assertIn("malformed", game_identity_refusal("bravo", None, ALPHA_ID, {}))
        for reason in (game_identity_refusal("alpha", fp(ALPHA_ID), ALPHA_ID, {}),
                       game_identity_refusal("alpha", fp(ALPHA_ID), None, {})):
            self.assertNotIn(ALPHA_ID, reason)

    def test_bare_claims_still_refuse_in_run_duplicates(self):
        claims = FleetClaims()
        first = build_snapshot("alpha", T0, "t", game_identity=fp(ALPHA_ID))
        self.assertIsNone(claims.claim_snapshot("alpha", first))
        self.assertEqual(claims.identity("alpha"), fp(ALPHA_ID))
        other = build_snapshot("bravo", T0, "t", game_identity=fp(ALPHA_ID))
        self.assertIn("already observed for 'alpha'", claims.claim_snapshot("bravo", other))
        self.assertIsNone(claims.identity("bravo"))

    def test_pin_refuses_a_changed_identity(self):
        snapshots = [build_snapshot("alpha", T0, "t", game_identity=fp(ALPHA_ID)),
                     build_snapshot("alpha", T0, "t", game_identity=fp(BRAVO_ID)),
                     build_snapshot("alpha", T0, "t")]
        provider = pin_game_identity(lambda: snapshots.pop(0), fp(ALPHA_ID))
        self.assertEqual(provider().game_identity, fp(ALPHA_ID))
        for _ in range(2):
            with self.assertRaises(AccountMismatchError):
                provider()

    def test_pin_keeps_an_unknown_identity_unknown(self):
        snapshots = [build_snapshot("alpha", T0, "t"), build_snapshot("alpha", T0, "t", game_identity=fp(BRAVO_ID))]
        provider = pin_game_identity(lambda: snapshots.pop(0), None)
        self.assertIsNone(provider().game_identity)
        with self.assertRaises(AccountMismatchError):
            provider()


class IdentityGame(FakeGame):
    """A fake game logged in as ``user_id``; its snapshots carry that id's fingerprint."""

    def __init__(self, alias, clock, user_id, salt=SALT):
        super().__init__(alias, clock)
        self.user_id = user_id
        self.salt = salt

    def snapshot(self):
        identity = fp(self.user_id, self.salt) if self.salt and self.user_id else None
        return build_snapshot(self.snapshot_alias, self.clock(), "fake-game", game_identity=identity, **self.state)


class FleetIdentityTests(unittest.TestCase):
    """Identity claims in sequential mode; ``ConcurrentFleetIdentityTests`` repeats every test concurrently."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.clock = LockedClock()
        self.logins = {"alpha": ALPHA_ID, "bravo": BRAVO_ID}
        self.salt = SALT
        self.games = {}
        self.games_lock = threading.Lock()

    def tearDown(self):
        self.tmp.cleanup()

    def orchestrator(self, registry, factory):
        return FleetOrchestrator(registry, factory, clock=self.clock, timer=lambda: 0.0)

    def factory(self, profile):
        with self.games_lock:
            game = self.games.setdefault(
                profile.alias, IdentityGame(profile.alias, self.clock, self.logins[profile.alias], self.salt))
        return AccountContext(game, game.snapshot, GameplayJournal(self.dir / f"{profile.alias}.jsonl"), DESIGN)

    def run_fleet(self, *entries, factory=None):
        registry = registry_from_dict({"version": 1, "accounts": list(entries)})
        return self.orchestrator(registry, factory or self.factory).run(execute=True)

    def assert_no_raw_ids(self, run):
        text = json.dumps([account.__dict__ for account in run.accounts], default=str)
        for path in self.dir.glob("*.jsonl"):
            text += path.read_text(encoding="utf-8")
        for raw in RAW_IDS:
            self.assertNotIn(raw, text)

    def test_matching_identities_run_and_are_reported(self):
        run = self.run_fleet(profile("alpha", expected_game_identity=fp(ALPHA_ID)),
                             profile("bravo", expected_game_identity=fp(BRAVO_ID)))
        self.assertEqual([a.status for a in run.accounts], ["completed", "completed"])
        self.assertEqual([a.game_identity for a in run.accounts], [fp(ALPHA_ID), fp(BRAVO_ID)])
        self.assertIn("addResearch", self.games["alpha"].calls)
        self.assert_no_raw_ids(run)

    def test_factory_that_loads_the_wrong_credentials_is_refused_before_any_call(self):
        self.logins["alpha"] = BRAVO_ID  # alias "alpha", but logged in as bravo's game account
        run = self.run_fleet(profile("alpha", expected_game_identity=fp(ALPHA_ID)),
                             profile("bravo", expected_game_identity=fp(BRAVO_ID)))
        self.assertEqual([a.status for a in run.accounts], ["error", "completed"])
        self.assertIn("does not match expected_game_identity", run.accounts[0].reason)
        self.assertEqual(self.games["alpha"].calls, [])
        self.assertEqual(GameplayJournal(self.dir / "alpha.jsonl").read(), [])
        self.assert_no_raw_ids(run)

    def test_undeclared_account_on_another_accounts_declared_identity_is_refused(self):
        # Regression: alpha declared nothing but its factory loaded bravo's credentials. alpha
        # was accepted first and made real calls on bravo's game account; then bravo, the
        # rightful owner, was refused as the duplicate.
        self.logins["alpha"] = BRAVO_ID
        run = self.run_fleet(profile("alpha"), profile("bravo", expected_game_identity=fp(BRAVO_ID)))
        self.assertEqual([a.status for a in run.accounts], ["error", "completed"])
        self.assertIn("declared for 'bravo' in the registry", run.accounts[0].reason)
        self.assertIsNone(run.accounts[0].game_identity)
        self.assertEqual(self.games["alpha"].calls, [])
        self.assertEqual(GameplayJournal(self.dir / "alpha.jsonl").read(), [])
        self.assertIn("addResearch", self.games["bravo"].calls)
        self.assertEqual(run.accounts[1].game_identity, fp(BRAVO_ID))
        self.assert_no_raw_ids(run)

    def test_identity_declared_by_a_disabled_account_is_refused(self):
        self.logins["alpha"] = BRAVO_ID
        run = self.run_fleet(profile("alpha"), profile("bravo", expected_game_identity=fp(BRAVO_ID), enabled=False))
        self.assertEqual([a.status for a in run.accounts], ["error", "disabled"])
        self.assertIn("declared for 'bravo' in the registry", run.accounts[0].reason)
        self.assertEqual(self.games["alpha"].calls, [])

    def test_unknown_identity_is_refused_when_the_profile_declares_one(self):
        self.salt = None  # the provider cannot fingerprint, so the identity is unknown
        run = self.run_fleet(profile("alpha", expected_game_identity=fp(ALPHA_ID)), profile("bravo"))
        self.assertEqual([a.status for a in run.accounts], ["error", "completed"])
        self.assertIn("unknown", run.accounts[0].reason)
        self.assertEqual(self.games["alpha"].calls, [])
        self.assertIn("addResearch", self.games["bravo"].calls)

    def test_two_accounts_on_one_game_identity_are_refused(self):
        self.logins["bravo"] = ALPHA_ID
        run = self.run_fleet(profile("alpha"), profile("bravo"))
        self.assertEqual([a.status for a in run.accounts], ["completed", "error"])
        self.assertIn("already observed for 'alpha'", run.accounts[1].reason)
        self.assertEqual(self.games["bravo"].calls, [])
        self.assert_no_raw_ids(run)

    def test_disabled_duplicate_is_not_observed(self):
        self.logins["bravo"] = ALPHA_ID
        run = self.run_fleet(profile("alpha"), profile("bravo", enabled=False))
        self.assertEqual([a.status for a in run.accounts], ["completed", "disabled"])

    def test_unknown_identities_without_declarations_still_run(self):
        self.salt = None
        run = self.run_fleet(profile("alpha"), profile("bravo"))
        self.assertEqual([a.status for a in run.accounts], ["completed", "completed"])
        self.assertEqual([a.game_identity for a in run.accounts], [None, None])

    def test_identity_change_mid_run_stops_before_any_call(self):
        game = IdentityGame("alpha", self.clock, ALPHA_ID)
        calls = []

        def snapshot():
            calls.append(1)
            if len(calls) == 3:  # the runner's before-snapshot of the first action
                game.user_id = BRAVO_ID
            return game.snapshot()

        def factory(profile):
            return AccountContext(game, snapshot, GameplayJournal(self.dir / "alpha.jsonl"), DESIGN)

        run = self.run_fleet(profile("alpha", expected_game_identity=fp(ALPHA_ID)), factory=factory)
        self.assertEqual(run.accounts[0].status, "stopped")
        self.assertIn("game identity changed", run.accounts[0].reason)
        self.assertEqual(game.calls, [])
        events = GameplayJournal(self.dir / "alpha.jsonl").read()
        self.assertTrue(events)
        self.assertFalse(any(event.attempted for event in events))
        self.assert_no_raw_ids(run)

    def test_identity_unknown_at_first_snapshot_may_not_become_known(self):
        # Regression: alpha declared nothing and its user id was unknown at the first snapshot,
        # so nothing was pinned; it then became bravo's id and alpha made real calls on
        # bravo's game account although the registry declares that identity for bravo.
        alpha = IdentityGame("alpha", self.clock, None)
        alpha_snapshots = []

        def alpha_snapshot():
            alpha_snapshots.append(1)
            if len(alpha_snapshots) == 2:  # the scheduler's planning snapshot
                alpha.user_id = BRAVO_ID
            return alpha.snapshot()

        def factory(profile):
            if profile.alias != "alpha":
                return self.factory(profile)
            return AccountContext(alpha, alpha_snapshot, GameplayJournal(self.dir / "alpha.jsonl"), DESIGN)

        run = self.run_fleet(profile("alpha"), profile("bravo", expected_game_identity=fp(BRAVO_ID)),
                             factory=factory)
        self.assertIn(run.accounts[0].status, ("error", "stopped"))
        self.assertIn("game identity changed", run.accounts[0].reason)
        self.assertEqual(alpha.calls, [])
        self.assertFalse(any(event.attempted for event in GameplayJournal(self.dir / "alpha.jsonl").read()))
        self.assertEqual(run.accounts[1].status, "completed")
        self.assert_no_raw_ids(run)

    def test_identity_unknown_throughout_still_runs(self):
        self.salt = None
        run = self.run_fleet(profile("alpha"))
        self.assertEqual(run.accounts[0].status, "completed")
        self.assertIn("addResearch", self.games["alpha"].calls)


class ConcurrentFleetIdentityTests(FleetIdentityTests):
    """Every identity test again, with the concurrent orchestrator and two workers."""

    def orchestrator(self, registry, factory):
        return ConcurrentFleetOrchestrator(registry, factory, clock=self.clock, timer=lambda: 0.0, max_workers=2)

    def test_mode_is_concurrent(self):
        self.assertEqual(self.run_fleet(profile("alpha"), profile("bravo")).mode, "concurrent")


if __name__ == "__main__":
    unittest.main()
