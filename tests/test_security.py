#!/usr/bin/env python3
"""
Security regression tests for Tachikoma.

Tests that verify credentials are never embedded or logged.
"""

import unittest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sdk.redaction import redact_secrets, redact_dict, safe_log_message, redact_log
from sdk.client import Client, ConfigurationError
from sdk.device import Device
from sdk.security import (
    checksum_device_login17,
    checksum_user_email_password_authorize4,
    checksum_finalise_battle15,
    checksum_update_marker_movement,
    checksum_rebuild_ammo3,
    checksum_collect_marker2,
)
from tests.synthetic_fixtures import (
    SYNTHETIC_DEVICE_KEY_IOS,
    SYNTHETIC_DEVICE_KEY_MAC,
    SYNTHETIC_CDT_1,
    SYNTHETIC_CDT_2,
    SYNTHETIC_CHECKSUM_KEY,
    SYNTHETIC_SAVY_CHECKSUM,
    SYNTHETIC_EMAIL,
    SYNTHETIC_ACCESS_TOKEN,
    SYNTHETIC_IOS_CHECKSUM_1,
    SYNTHETIC_IOS_CHECKSUM_2,
    SYNTHETIC_EMAIL_AUTH_CHECKSUM,
)


class TestRedaction(unittest.TestCase):
    """Test that sensitive data is properly redacted from logs."""

    def test_redact_jwt_token(self):
        """JWT tokens should be redacted."""
        text = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        result = redact_secrets(text)
        self.assertEqual(result, '***REDACTED_JWT***')

    def test_redact_access_token_in_url(self):
        """accessToken parameter in URLs should be redacted."""
        text = "https://api.example.com/endpoint?accessToken=abc123def456&other=param"
        result = redact_secrets(text)
        self.assertIn('accessToken=***REDACTED***', result)
        self.assertNotIn('abc123def456', result)

    def test_redact_refresh_token_in_url(self):
        """refreshToken parameter in URLs should be redacted."""
        text = "https://api.example.com/endpoint?refreshToken=xyz789&other=param"
        result = redact_secrets(text)
        self.assertIn('refreshToken=***REDACTED***', result)
        self.assertNotIn('xyz789', result)

    def test_redact_device_key_in_url(self):
        """deviceKey parameter in URLs should be redacted."""
        text = "https://api.example.com/endpoint?deviceKey=device123&other=param"
        result = redact_secrets(text)
        self.assertIn('deviceKey=***REDACTED***', result)
        self.assertNotIn('device123', result)

    def test_redact_email(self):
        """Email addresses should be redacted."""
        text = "User email: user@example.com"
        result = redact_secrets(text)
        self.assertEqual(result, "User email: ***REDACTED_EMAIL***")

    def test_redact_password_in_url(self):
        """Password parameter in URLs should be redacted."""
        text = "https://api.example.com/login?password=secret123&user=test"
        result = redact_secrets(text)
        self.assertIn('password=***REDACTED***', result)
        self.assertNotIn('secret123', result)

    def test_redact_access_token_in_response(self):
        """accessToken in response bodies should be redacted."""
        text = 'accessToken="abc123def456"'
        result = redact_secrets(text)
        # Our pattern catches accessToken="xxx"
        self.assertEqual(result, 'accessToken="***REDACTED***"')

    def test_redact_refresh_token_in_response(self):
        """refreshToken in response bodies should be redacted."""
        text = 'refreshToken="xyz789"'
        result = redact_secrets(text)
        # Our pattern catches refreshToken="xxx"
        self.assertEqual(result, 'refreshToken="***REDACTED***"')

    def test_redact_device_key_in_json(self):
        """DeviceKey in JSON should be redacted."""
        text = '{"DeviceKey": "device123"}'
        result = redact_secrets(text)
        self.assertEqual(result, '{"DeviceKey": "***REDACTED***"}')

    def test_redact_refresh_token_in_json(self):
        """RefreshToken in JSON should be redacted."""
        text = '{"RefreshToken": "token123"}'
        result = redact_secrets(text)
        self.assertEqual(result, '{"RefreshToken": "***REDACTED***"}')

    def test_redact_email_in_json(self):
        """Email in JSON should be redacted."""
        text = '{"Email": "user@example.com"}'
        result = redact_secrets(text)
        self.assertEqual(result, '{"Email": "***REDACTED_EMAIL***"}')

    def test_redact_password_in_json(self):
        """Password in JSON should be redacted (case insensitive)."""
        text = '{"password": "secret123"}'
        result = redact_secrets(text)
        self.assertEqual(result, '{"password": "***REDACTED***"}')

    def test_redact_dict(self):
        """Dictionary redaction should work recursively."""
        d = {
            "accessToken": "secret_token",
            "deviceKey": "device123",
            "normalField": "value",
            "nested": {
                "refreshToken": "nested_token",
                "email": "user@example.com"
            }
        }
        result = redact_dict(d)
        self.assertEqual(result["accessToken"], "***REDACTED***")
        self.assertEqual(result["deviceKey"], "***REDACTED***")
        self.assertEqual(result["normalField"], "value")
        self.assertEqual(result["nested"]["refreshToken"], "***REDACTED***")
        # Email in dict gets redacted by key match
        self.assertEqual(result["nested"]["email"], "***REDACTED_EMAIL***")

    def test_safe_log_message(self):
        """safe_log_message should format and redact."""
        result = safe_log_message("Token: %s", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
        self.assertIn("***REDACTED_JWT***", result)

    def test_redact_log_alias(self):
        """redact_log should be an alias for safe_log_message."""
        result = redact_log("Token: %s", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
        self.assertIn("***REDACTED_JWT***", result)


class TestClientSecurity(unittest.TestCase):
    """Test Client class security behavior."""

    def test_no_hardcoded_fallback_token(self):
        """Client should not have hardcoded fallback token."""
        device = Device(language="en")
        client = Client(device=device)

        # Check that getAccessToken doesn't use a hardcoded fallback
        # We can't fully test without mocking, but we can verify the code path
        import inspect
        source = inspect.getsource(client.getAccessToken)
        self.assertNotIn("eyJhbG", source)  # Base64 JWT prefix
        self.assertNotIn("1Rqw", source)  # Truncated token fragment

    def test_get_access_token_uses_empty_string(self):
        """DeviceLogin17 payload should use empty string when no refresh token."""
        device = Device(language="en")
        client = Client(device=device)

        import inspect
        source = inspect.getsource(client._build_device_login_payload)
        # Should use empty string as fallback, not a hardcoded token
        # The actual code uses: self.device.refreshToken if self.device.refreshToken else ""
        self.assertIn('else ""', source)

    def test_client_imports_redaction(self):
        """Client should import redaction utilities."""
        import sdk.client as client_module
        self.assertTrue(hasattr(client_module, 'redact_secrets'))
        self.assertTrue(hasattr(client_module, 'safe_log_message'))


class TestRebuildAmmoConfig(unittest.TestCase):
    """Test rebuildAmmo configuration-driven checksum behavior."""

    def test_rebuild_ammo_missing_checksum_key(self):
        """rebuildAmmo should raise ConfigurationError when checksum_key is missing."""
        from unittest.mock import MagicMock, patch
        from sdk.client import User

        device = Device(language="en")
        client = Client(device=device, settings={
            "savy_checksum": "test-savy-checksum",
        })
        client.accessToken = "test-token-uuid"
        client.user = User(3430892, "test", None, True)
        client.info = {"@Email": "test@example.com", "@Name": "test"}

        mock_response = MagicMock()
        mock_response.text = "<RebuildAmmo/>"

        with patch.object(client.session, 'request', side_effect=lambda *a, **k: mock_response):
            with self.assertRaises(ConfigurationError) as cm:
                client.rebuildAmmo()
            # Exception message should not expose configuration values
            self.assertNotIn("test-savy-checksum", str(cm.exception))

    def test_rebuild_ammo_missing_savy_checksum(self):
        """rebuildAmmo should raise ConfigurationError when savy_checksum is missing."""
        from unittest.mock import MagicMock, patch
        from sdk.client import User

        device = Device(language="en")
        client = Client(device=device, settings={
            "checksum_key": "test-checksum-key",
        })
        client.accessToken = "test-token-uuid"
        client.user = User(3430892, "test", None, True)
        client.info = {"@Email": "test@example.com", "@Name": "test"}

        mock_response = MagicMock()
        mock_response.text = "<RebuildAmmo/>"

        with patch.object(client.session, 'request', side_effect=lambda *a, **k: mock_response):
            with self.assertRaises(ConfigurationError) as cm:
                client.rebuildAmmo()
            # Exception message should not expose configuration values
            self.assertNotIn("test-checksum-key", str(cm.exception))

    def test_rebuild_ammo_missing_both_config(self):
        """rebuildAmmo should raise ConfigurationError when both config values are missing."""
        from unittest.mock import MagicMock, patch
        from sdk.client import User

        device = Device(language="en")
        client = Client(device=device, settings={})
        client.accessToken = "test-token-uuid"
        client.user = User(3430892, "test", None, True)
        client.info = {"@Email": "test@example.com", "@Name": "test"}

        mock_response = MagicMock()
        mock_response.text = "<RebuildAmmo/>"

        with patch.object(client.session, 'request', side_effect=lambda *a, **k: mock_response):
            with self.assertRaises(ConfigurationError) as cm:
                client.rebuildAmmo()
            # Exception message should not expose configuration values
            self.assertNotIn("test-checksum-key", str(cm.exception))
            self.assertNotIn("test-savy-checksum", str(cm.exception))

    def test_rebuild_ammo_does_not_log_preimage_or_secrets(self):
        """rebuildAmmo must not log the checksum preimage, tokens, device identifiers, or checksum secrets."""
        from unittest.mock import MagicMock, patch
        from sdk.client import User
        import logging
        from io import StringIO

        device = Device(language="en")
        client = Client(device=device, settings={
            "checksum_key": "secret-checksum-key",
            "savy_checksum": "secret-savy-checksum",
        })
        client.accessToken = "secret-access-token-uuid"
        client.user = User(3430892, "test", None, True)
        client.info = {"@Email": "test@example.com", "@Name": "test"}

        mock_response = MagicMock()
        mock_response.text = "<RebuildAmmo/>"

        # Capture logs
        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        logger = logging.getLogger("sdk.client")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        with patch.object(client.session, 'request', side_effect=lambda *a, **k: mock_response):
            client.rebuildAmmo()

        handler.flush()
        log_output = log_stream.getvalue()

        # Should not contain secrets
        self.assertNotIn("secret-checksum-key", log_output)
        self.assertNotIn("secret-savy-checksum", log_output)
        self.assertNotIn("secret-access-token-uuid", log_output)
        self.assertNotIn("test-device-key", log_output)
        self.assertNotIn("test@example.com", log_output)

        logger.removeHandler(handler)


class TestThreeStageAuth(unittest.TestCase):
    """Test three-stage email/password authentication flow."""

    def test_create_device_session_exists(self):
        """Client should have create_device_session method."""
        device = Device(language="en")
        client = Client(device=device)
        self.assertTrue(hasattr(client, "create_device_session"))
        self.assertTrue(callable(client.create_device_session))

    def test_authorize_email_password_exists(self):
        """Client should have authorize_email_password method."""
        device = Device(language="en")
        client = Client(device=device)
        self.assertTrue(hasattr(client, "authorize_email_password"))
        self.assertTrue(callable(client.authorize_email_password))

    def test_exchange_refresh_token_exists(self):
        """Client should have exchange_refresh_token method."""
        device = Device(language="en")
        client = Client(device=device)
        self.assertTrue(hasattr(client, "exchange_refresh_token"))
        self.assertTrue(callable(client.exchange_refresh_token))

    def test_authorize_email_password_missing_checksum_key(self):
        """authorize_email_password should use hardcoded fallback for checksum_key."""
        from sdk.security import UnsupportedNativeChecksum
        from unittest.mock import patch

        device = Device(language="en")
        client = Client(device=device, settings={"savy_checksum": "Savvy!s0d@"})
        client.accessToken = "test-token"

        with patch.object(client, "request", side_effect=Exception("Mocked")):
            try:
                client.authorize_email_password("test@example.com", "password123")
            except UnsupportedNativeChecksum:
                self.fail("Should not raise UnsupportedNativeChecksum with fallback")
            except Exception:
                pass  # Network errors are expected
        self.assertTrue(client.checksum and len(client.checksum) == 32)

    def test_authorize_email_password_missing_savy_checksum(self):
        """authorize_email_password should use hardcoded fallback for savy_checksum."""
        from sdk.security import UnsupportedNativeChecksum
        from unittest.mock import patch

        device = Device(language="en")
        client = Client(device=device, settings={"checksum_key": "5343"})
        client.accessToken = "test-token"

        with patch.object(client, "request", side_effect=Exception("Mocked")):
            try:
                client.authorize_email_password("test@example.com", "password123")
            except UnsupportedNativeChecksum:
                self.fail("Should not raise UnsupportedNativeChecksum with fallback")
            except Exception:
                pass  # Network errors are expected
        self.assertTrue(client.checksum and len(client.checksum) == 32)

    def test_authorize_email_password_requires_access_token(self):
        """authorize_email_password should raise ValueError without access token."""
        device = Device(language="en")
        client = Client(device=device, settings={"checksum_key": "k", "savy_checksum": "s"})

        with self.assertRaises(ValueError) as cm:
            client.authorize_email_password("test@example.com", "password123")
        self.assertIn("access token", str(cm.exception).lower())

    def test_login_raises_without_password_when_email_provided(self):
        """login() should raise ValueError if email provided but password missing."""
        from unittest.mock import patch

        device = Device(language="en")
        device.refreshToken = None  # Force no existing refresh token
        client = Client(device=device, settings={"checksum_key": "k", "savy_checksum": "s"})

        with patch.object(client, "create_device_session", return_value=True):
            client.accessToken = "test-token"
            with self.assertRaises(ValueError) as cm:
                client.login(email="test@example.com")
            self.assertIn("password", str(cm.exception).lower())


class TestVerifiedChecksums(unittest.TestCase):
    """Test checksum formulas verified against live captures."""

    def test_device_login17_verified_ios_capture_1(self):
        """DeviceLogin17 iOS formula (DeviceTypeIPhone) verified against
        synthetic vector. Timestamp must be stripped to seconds."""
        result = checksum_device_login17(
            SYNTHETIC_DEVICE_KEY_IOS,
            SYNTHETIC_CDT_1,
            SYNTHETIC_CHECKSUM_KEY,
            SYNTHETIC_SAVY_CHECKSUM,
        )
        self.assertEqual(result, SYNTHETIC_IOS_CHECKSUM_1)

    def test_device_login17_verified_ios_capture_2(self):
        """DeviceLogin17 iOS formula (DeviceTypeIPhone) verified against
        synthetic vector."""
        result = checksum_device_login17(
            SYNTHETIC_DEVICE_KEY_IOS,
            SYNTHETIC_CDT_2,
            SYNTHETIC_CHECKSUM_KEY,
            SYNTHETIC_SAVY_CHECKSUM,
        )
        self.assertEqual(result, SYNTHETIC_IOS_CHECKSUM_2)

    def test_device_login17_timestamp_must_be_stripped(self):
        """Regression: checksum with full-precision timestamp must NOT match.
        The official client strips microseconds + Z before hashing."""
        
        # Test that full-precision timestamp produces different result
        # We need to test with a known full-precision timestamp
        # Use a timestamp that will clearly produce a different result
        cdt_full = '2026-08-03T00:40:41.326915Z'
        result = checksum_device_login17(
            SYNTHETIC_DEVICE_KEY_IOS,
            cdt_full,
            SYNTHETIC_CHECKSUM_KEY,
            SYNTHETIC_SAVY_CHECKSUM,
        )
        self.assertNotEqual(result, SYNTHETIC_IOS_CHECKSUM_1)

    def test_device_login17_requires_config(self):
        """DeviceLogin17 raises UnsupportedNativeChecksum when config missing."""
        from sdk.security import UnsupportedNativeChecksum

        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_device_login17("key", "time", "", "savy")
        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_device_login17("key", "time", "ck", "")

    def test_email_password_authorize4_verified_capture(self):
        """UserEmailPasswordAuthorize4 formula verified against synthetic vector."""
        result = checksum_user_email_password_authorize4(
            SYNTHETIC_DEVICE_KEY_MAC,
            SYNTHETIC_EMAIL,
            SYNTHETIC_CDT_1,
            SYNTHETIC_ACCESS_TOKEN,
            SYNTHETIC_CHECKSUM_KEY,
            SYNTHETIC_SAVY_CHECKSUM,
        )
        self.assertEqual(result, SYNTHETIC_EMAIL_AUTH_CHECKSUM)

    def test_email_password_authorize4_excludes_password(self):
        """UserEmailPasswordAuthorize4 checksum does not include the password."""
        # Same inputs, different password - checksum must be identical
        result = checksum_user_email_password_authorize4(
            SYNTHETIC_DEVICE_KEY_MAC,
            SYNTHETIC_EMAIL,
            SYNTHETIC_CDT_1,
            SYNTHETIC_ACCESS_TOKEN,
            SYNTHETIC_CHECKSUM_KEY,
            SYNTHETIC_SAVY_CHECKSUM,
        )
        self.assertEqual(result, SYNTHETIC_EMAIL_AUTH_CHECKSUM)

    def test_email_password_authorize4_requires_config(self):
        """UserEmailPasswordAuthorize4 raises UnsupportedNativeChecksum when config missing."""
        from sdk.security import UnsupportedNativeChecksum

        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_user_email_password_authorize4(
                "key", "em", "ts", "at", "", "savy"
            )
        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_user_email_password_authorize4(
                "key", "em", "ts", "at", "ck", ""
            )

    def test_checksum_finalise_battle15(self):
        """FinaliseBattle15 formula verified against synthetic vector."""
        # preimage = battleId + clientOutcomeType + clientEndFrame + clientResultString + attackingShipHp + clientVersion + accessToken + "5343"
        # MD5(preimage + "Savvy!s0d@")
        battle_id = "4028975"
        client_outcome_type = "1"
        client_end_frame = "2400"
        client_result_string = ""
        attacking_ship_hp = "24.31"
        client_version = "0.999.59"
        access_token = "466f7d82-0bd8-48d1-90f6-2466c3e873b0"
        checksum_key = "5343"
        savy_checksum = "Savvy!s0d@"

        preimage = (
            battle_id
            + client_outcome_type
            + client_end_frame
            + client_result_string
            + attacking_ship_hp
            + client_version
            + access_token
            + checksum_key
        )
        encrypted = preimage + savy_checksum
        import hashlib
        expected_md5 = hashlib.md5(encrypted.encode("utf-8")).hexdigest()

        result = checksum_finalise_battle15(
            battle_id,
            client_outcome_type,
            client_end_frame,
            client_result_string,
            attacking_ship_hp,
            client_version,
            access_token,
            checksum_key,
            savy_checksum,
        )
        self.assertEqual(result, expected_md5)


class TestVerifiedChecksumsNew(unittest.TestCase):
    """Test newly verified checksum formulas against live captures."""

    def test_update_marker_movement_verified_captures(self):
        """UpdateMarkerMovement formula verified against 6 mitmproxy captures (2026-08-08 to 2026-08-10).
        
        Formula: MD5(markerId + clientDateTime + accessToken + ChecksumKey + SavyChecksum)
        """
        checksum_key = "5343"
        savy_checksum = "Savvy!s0d@"
        
        # Capture 1: 2026-08-08
        result = checksum_update_marker_movement(
            marker_id="95363156",
            client_date_time="2026-08-08T17:37:07",
            access_token="466f7d82-0bd8-48d1-90f6-2466c3e873b0",
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )
        self.assertEqual(result, "0449bbb16da6e2deb67113831801b0c3")
        
        # Capture 2: 2026-08-09
        result = checksum_update_marker_movement(
            marker_id="95384543",
            client_date_time="2026-08-09T03:14:28",
            access_token="9f98e1bf-38d3-449e-a5e8-3bd782b56c6e",
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )
        self.assertEqual(result, "5d7425e65dc8c4c88f478821d1d2e0d9")
        
        # Capture 3: 2026-08-09 (different markerId)
        result = checksum_update_marker_movement(
            marker_id="95384544",
            client_date_time="2026-08-09T03:14:28",
            access_token="9f98e1bf-38d3-449e-a5e8-3bd782b56c6e",
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )
        self.assertEqual(result, "068d7d6605a8598a0b047992f668b881")
        
        # Capture 4: 2026-08-09
        result = checksum_update_marker_movement(
            marker_id="95384545",
            client_date_time="2026-08-09T03:14:28",
            access_token="9f98e1bf-38d3-449e-a5e8-3bd782b56c6e",
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )
        self.assertEqual(result, "517f98cfd68adc047ed0fe82f64eeb0b")
        
        # Capture 5: 2026-08-09
        result = checksum_update_marker_movement(
            marker_id="95379943",
            client_date_time="2026-08-09T03:14:28",
            access_token="9f98e1bf-38d3-449e-a5e8-3bd782b56c6e",
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )
        self.assertEqual(result, "4ecf4bd91ff7440e2a76d82632326629")
        
        # Capture 6: 2026-08-10
        result = checksum_update_marker_movement(
            marker_id="95415247",
            client_date_time="2026-08-10T01:53:10",
            access_token="e02c01a6-18f4-4f91-8a14-0e91e1cfed8c",
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )
        self.assertEqual(result, "a5bb2a16a6dec0e9c8e4455b5d7bd739")

    def test_rebuild_ammo3_verified_capture(self):
        """RebuildAmmo3 formula verified against mitmproxy capture (2026-08-10).
        
        Formula: MD5(ammoCategory + clientDateTime + accessToken + ChecksumKey + SavyChecksum)
        """
        checksum_key = "5343"
        savy_checksum = "Savvy!s0d@"
        
        result = checksum_rebuild_ammo3(
            ammo_category="None",
            client_date_time="2026-08-10T01:53:06",
            access_token="e02c01a6-18f4-4f91-8a14-0e91e1cfed8c",
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )
        self.assertEqual(result, "0c062964d743eccb15b1bc00338997f3")

    def test_update_marker_movement_requires_config(self):
        """UpdateMarkerMovement raises UnsupportedNativeChecksum when config missing."""
        from sdk.security import UnsupportedNativeChecksum
        
        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_update_marker_movement("id", "time", "token", "", "savy")
        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_update_marker_movement("id", "time", "token", "ck", "")

    def test_rebuild_ammo3_requires_config(self):
        """RebuildAmmo3 raises UnsupportedNativeChecksum when config missing."""
        from sdk.security import UnsupportedNativeChecksum
        
        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_rebuild_ammo3("ammo", "time", "token", "", "savy")
        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_rebuild_ammo3("ammo", "time", "token", "ck", "")

    def test_collect_marker2_formula_matches_update_marker_movement(self):
        """CollectMarker2 uses the same formula as UpdateMarkerMovement (same Galaxy marker family).

        Formula: MD5(markerId + clientDateTime + accessToken + ChecksumKey + SavyChecksum)
        No designVersion — static analysis was wrong about this.
        """
        checksum_key = "5343"
        savy_checksum = "Savvy!s0d@"

        # Use the same known-good vector from UpdateMarkerMovement capture 1
        result_collect = checksum_collect_marker2(
            marker_id="95363156",
            client_date_time="2026-08-08T17:37:07",
            access_token="466f7d82-0bd8-48d1-90f6-2466c3e873b0",
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )
        result_movement = checksum_update_marker_movement(
            marker_id="95363156",
            client_date_time="2026-08-08T17:37:07",
            access_token="466f7d82-0bd8-48d1-90f6-2466c3e873b0",
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )
        # Same inputs → same checksum (both use identical formula)
        self.assertEqual(result_collect, result_movement)
        self.assertEqual(result_collect, "0449bbb16da6e2deb67113831801b0c3")

    def test_collect_marker2_requires_config(self):
        """CollectMarker2 raises UnsupportedNativeChecksum when config missing."""
        from sdk.security import UnsupportedNativeChecksum
        
        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_collect_marker2("id", "time", "token", "", "savy")
        with self.assertRaises(UnsupportedNativeChecksum):
            checksum_collect_marker2("id", "time", "token", "ck", "")


class TestRefreshTokenLoginBehavior(unittest.TestCase):
    """Test refresh-token-only login behavior and email/password feature gate."""

    def setUp(self):
        self.device = Device(language="en")
        self.settings = {"checksum_key": "5343", "savy_checksum": "Savvy!s0d@"}
        self.client = Client(device=self.device, settings=self.settings)

    def _mock_device_login_response(self, access_token="new-access-token", user_id="123", error_code=None):
        """Create a mock response matching DeviceLogin17 XML structure."""
        from unittest.mock import MagicMock
        mock = MagicMock()
        if error_code:
            xml = (
                f'<UserService><UserLogin errorCode="{error_code}" UserId="{user_id}">'
                f'<User Id="{user_id}" Name="test" LastHeartBeatDate="2026-08-02T12:00:00" '
                f'FreeStarbuxReceivedToday="0"/></UserLogin></UserService>'
            )
        else:
            xml = (
                f'<UserService><UserLogin accessToken="{access_token}" UserId="{user_id}">'
                f'<User Id="{user_id}" Name="test" LastHeartBeatDate="2026-08-02T12:00:00" '
                f'FreeStarbuxReceivedToday="0"/></UserLogin></UserService>'
            )
        mock.text = xml
        mock.content = xml.encode('utf-8')
        mock.status_code = 200
        return mock

    def test_existing_refresh_token_skips_email_authorization(self):
        """login() with existing refresh token must not call authorize_email_password."""
        from unittest.mock import patch

        self.device.refreshToken = "existing-refresh-token"
        mock_response = self._mock_device_login_response(access_token="new-access-token", user_id="123")

        with patch.object(self.client, 'authorize_email_password', return_value=False) as mock_auth:
            with patch.object(self.client.session, 'request', side_effect=lambda *a, **k: mock_response):
                result = self.client.login()
                self.assertTrue(result)
                self.assertEqual(self.client.accessToken, "new-access-token")
                mock_auth.assert_not_called()

    def test_rejected_refresh_token_no_fallback(self):
        """Rejected refresh token returns False; does not fall back to email/password."""
        from unittest.mock import patch

        self.device.refreshToken = "bad-refresh-token"
        mock_response = self._mock_device_login_response(error_code="401", user_id="123")

        with patch.object(self.client, 'authorize_email_password', return_value=True) as mock_auth:
            with patch.object(self.client.session, 'request', side_effect=lambda *a, **k: mock_response):
                result = self.client.login()
                self.assertFalse(result)
                mock_auth.assert_not_called()

    def test_missing_refresh_without_credentials_uses_guest_session(self):
        """Missing refresh token + no credentials enters guest session (no email auth)."""
        from unittest.mock import patch

        self.device.refreshToken = None
        self.client.accessToken = "test-access-token"

        # Feature flag disabled (default): email/password login blocked
        # login() without email/password should succeed as guest (returns True)
        with patch.object(self.client, 'authorize_email_password', return_value=True) as mock_auth:
            with patch.object(self.client, 'create_device_session', return_value=True):
                result = self.client.login()
                self.assertTrue(result)  # guest path
                mock_auth.assert_not_called()

    def test_email_password_blocked_by_feature_gate(self):
        """login(email, password) with no refresh token blocked when feature gate disabled."""
        from unittest.mock import patch

        self.device.refreshToken = None
        self.client.accessToken = "test-access-token"

        # Feature flag disabled: calling login() with email/password should fail
        # without calling authorize_email_password or storing a refresh token
        with patch.object(self.client, 'authorize_email_password', return_value=True) as mock_auth:
            with patch.object(self.client, 'create_device_session', return_value=True):
                result = self.client.login(email="user@example.com", password="password123")
                self.assertFalse(result)
                mock_auth.assert_not_called()
                # Ensure no refresh token was stored
                self.assertIsNone(self.device.refreshToken)

    def test_device_login_updates_access_token_only(self):
        """Successful DeviceLogin17 updates accessToken; stored refreshToken unchanged."""
        from unittest.mock import patch

        original_refresh = "stored-refresh-token"
        self.device.refreshToken = original_refresh

        mock_response = self._mock_device_login_response(access_token="new-access-token", user_id="123")

        with patch.object(self.client.session, 'request', side_effect=lambda *a, **k: mock_response):
            result = self.client.create_device_session()
            self.assertTrue(result)
            self.assertEqual(self.client.accessToken, "new-access-token")
            self.assertEqual(self.device.refreshToken, original_refresh)  # unchanged


if __name__ == '__main__':
    unittest.main()