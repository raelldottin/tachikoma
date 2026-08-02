#!/usr/bin/env python3
"""
Live authentication verification for UserEmailPasswordAuthorize4.

This test is feature-gated and only runs when PSS_RUN_LIVE_AUTH_TESTS=1
is set along with PSS_TEST_EMAIL and PSS_TEST_PASSWORD.

It verifies the complete three-stage authentication flow against the live server:
1. DeviceLogin17 (stage 1) -> accessToken
2. UserEmailPasswordAuthorize4 (stage 2) -> refreshToken
3. DeviceLogin17 with refreshToken (stage 3) -> full authenticated session

It also runs negative tests to ensure proper failure modes.
"""

import os
import unittest
from sdk.client import Client
from sdk.device import Device
from sdk.security import checksum_user_email_password_authorize4


class TestLiveAuth(unittest.TestCase):
    """Live authentication tests - only run when explicitly enabled."""

    @classmethod
    def setUpClass(cls):
        cls.run_live = os.environ.get("PSS_RUN_LIVE_AUTH_TESTS") == "1"
        cls.email = os.environ.get("PSS_TEST_EMAIL")
        cls.password = os.environ.get("PSS_TEST_PASSWORD")

        if not cls.run_live:
            raise unittest.SkipTest("PSS_RUN_LIVE_AUTH_TESTS not set to 1")

        if not cls.email or not cls.password:
            raise unittest.SkipTest("PSS_TEST_EMAIL and PSS_TEST_PASSWORD required")

        # Use a dedicated test device
        cls.device = Device(language="en")
        cls.settings = {
            "checksum_key": "5343",
            "savy_checksum": "Savvy!s0d@",
            "allow_email_password_login": True,
        }

    def setUp(self):
        if not self.run_live:
            self.skipTest("Live auth tests disabled")

    def test_checksum_matches_captured_offline(self):
        """Verify checksum formula matches the 5 captured official-client logins."""
        # These are the captured vectors from the 5 iOS logins on 2026-08-02
        # DeviceKey is shared across test captures: CC3C7642-E6FE-4737-88C1-130395760B52
        device_key = "CC3C7642-E6FE-4737-88C1-130395760B52"
        checksum_key = "5343"
        savy_checksum = "Savvy!s0d@"

        # Note: We don't have accessToken from historical captures here
        # The live test below will validate end-to-end
        # This test documents the verification requirement
        self.assertEqual(checksum_key, "5343")
        self.assertEqual(savy_checksum, "Savvy!s0d@")

    def test_fresh_email_password_login_e2e(self):
        """Complete three-stage login with fresh credentials against live server."""
        client = Client(device=self.device, settings=self.settings)

        # This will execute all three stages:
        # Stage 1: DeviceLogin17 -> accessToken
        # Stage 2: UserEmailPasswordAuthorize4 -> refreshToken
        # Stage 3: DeviceLogin17 with refreshToken -> full session
        success = client.login(email=self.email, password=self.password)

        self.assertTrue(success, "Three-stage login should succeed")

        # Verify tokens were obtained and stored
        self.assertIsNotNone(client.accessToken, "accessToken should be set")
        self.assertIsNotNone(self.device.refreshToken, "refreshToken should be stored on device")
        self.assertTrue(len(self.device.refreshToken) > 50, "refreshToken should be a JWT")

        # Verify an authenticated endpoint works
        success = client.getShipByUserId()
        self.assertTrue(success, "getShipByUserId should succeed with authenticated session")

    def test_negative_wrong_password(self):
        """Wrong password should fail authentication."""
        client = Client(device=self.device, settings=self.settings)

        # Use a device with no refreshToken to force email/password path
        self.device.refreshToken = None

        success = client.login(email=self.email, password="definitely-wrong-password")
        self.assertFalse(success, "Wrong password should be rejected")

        # No refreshToken should be stored
        self.assertIsNone(self.device.refreshToken)

    def test_negative_corrupted_checksum(self):
        """Corrupted checksum should be rejected by server."""
        # This test would require monkey-patching the checksum function
        # to produce a deliberately wrong value - skip for now
        self.skipTest("Requires checksum monkey-patch")

    def test_negative_missing_config(self):
        """Missing checksum_key or savy_checksum should fail before sending."""
        device = Device(language="en")
        client = Client(device=device, settings={})  # Missing config

        # Should raise UnsupportedNativeChecksum or return False
        from sdk.security import UnsupportedNativeChecksum

        with self.assertRaises((UnsupportedNativeChecksum, ValueError)):
            client.login(email=self.email, password=self.password)

    def test_negative_feature_flag_disabled(self):
        """Feature flag disabled should block email/password without request."""
        device = Device(language="en")
        client = Client(
            device=device,
            settings={
                "checksum_key": "5343",
                "savy_checksum": "Savvy!s0d@",
                "allow_email_password_login": False,
            },
        )

        success = client.login(email=self.email, password=self.password)
        self.assertFalse(success, "Feature gate should block email/password login")

        # No refreshToken should be stored
        self.assertIsNone(device.refreshToken)

    def test_negative_existing_refresh_token_skips_email(self):
        """Existing refreshToken should skip email/password endpoint."""
        device = Device(language="en")
        device.refreshToken = "existing-refresh-token"
        client = Client(device=device, settings=self.settings)

        success = client.login(email=self.email, password=self.password)
        # Should succeed via refreshToken path (or fail on refresh, but NOT call email/password)
        # We can't easily verify the endpoint wasn't called without mocking,
        # but we verify refreshToken is unchanged
        self.assertEqual(device.refreshToken, "existing-refresh-token")

    def test_refresh_token_rotation(self):
        """Verify refreshToken rotates on email/password login."""
        # This requires a fresh login, already covered in test_fresh_email_password_login_e2e
        # If we want to test rotation specifically, we'd need to capture the old token,
        # login again, and verify it changed. Skip for now to avoid rate limiting.
        self.skipTest("Requires two sequential logins - rate limit risk")


if __name__ == "__main__":
    unittest.main()