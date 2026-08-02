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
        from sdk.client import User, ConfigurationError

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
        from sdk.client import User, ConfigurationError

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
        from sdk.client import User, ConfigurationError

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
        """authorize_email_password should raise UnsupportedNativeChecksum when checksum_key missing."""
        from sdk.client import UnsupportedNativeChecksum

        device = Device(language="en")
        client = Client(device=device, settings={"savy_checksum": "test-savy"})
        client.accessToken = "test-token"

        with self.assertRaises(UnsupportedNativeChecksum) as cm:
            client.authorize_email_password("test@example.com", "password123")
        self.assertNotIn("test-savy", str(cm.exception))

    def test_authorize_email_password_missing_savy_checksum(self):
        """authorize_email_password should raise UnsupportedNativeChecksum when savy_checksum missing."""
        from sdk.client import UnsupportedNativeChecksum

        device = Device(language="en")
        client = Client(device=device, settings={"checksum_key": "test-key"})
        client.accessToken = "test-token"

        with self.assertRaises(UnsupportedNativeChecksum) as cm:
            client.authorize_email_password("test@example.com", "password123")
        self.assertNotIn("test-key", str(cm.exception))

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


if __name__ == '__main__':
    unittest.main()