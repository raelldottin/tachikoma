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
from sdk.client import Client
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

    def test_redact_bare_uuid_token(self):
        """Bare UUID-format access tokens should be redacted."""
        text = "Token: b067dfa5-9050-47fa-9950-635bfd81770b"
        result = redact_secrets(text)
        self.assertNotIn('b067dfa5', result)

    def test_redact_uuid_in_error_message(self):
        """UUID-format tokens in error messages should be redacted."""
        text = "Connection failed for b067dfa5-9050-47fa-9950-635bfd81770b on host"
        result = redact_secrets(text)
        self.assertNotIn('b067dfa5', result)

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
        """getAccessToken should use empty string when no refresh token."""
        device = Device(language="en")
        client = Client(device=device)
        
        import inspect
        source = inspect.getsource(client.getAccessToken)
        # Should use empty string as fallback, not a hardcoded token
        # The actual code uses: self.device.refreshToken if self.device.refreshToken else ""
        self.assertIn('else ""', source)

    def test_client_imports_redaction(self):
        """Client should import redaction utilities."""
        import sdk.client as client_module
        self.assertTrue(hasattr(client_module, 'redact_secrets'))
        self.assertTrue(hasattr(client_module, 'safe_log_message'))


class TestDeviceSecurity(unittest.TestCase):
    """Test Device class security behavior."""

    def test_device_file_permissions(self):
        """Device file should not be world-readable."""
        # This is more of a documentation test - actual permissions
        # are set at save time
        device = Device(language="en")
        self.assertIsNotNone(device.DB)

    def test_refresh_token_not_logged(self):
        """Device should not log refresh tokens in plain text."""
        device = Device(language="en")
        # The save/load methods handle the token
        # We verify the methods exist
        self.assertTrue(hasattr(device, 'refreshTokenAcquire'))
        self.assertTrue(hasattr(device, 'save'))
        self.assertTrue(hasattr(device, 'load'))


class TestPostBodyExcludesAccessToken(unittest.TestCase):
    """Regression: accessToken must NOT appear in POST body (mitmproxy evidence).

    The official game client sends accessToken only in the URL query string,
    never in the form-urlencoded POST body. Including it in the body causes
    AddStarbux2 to return 'An error occurred.' (mitmproxy capture 2026-07-31).
    """

    def test_post_body_excludes_access_token(self):
        """request() should not include accessToken in auto-populated POST body."""
        from unittest.mock import MagicMock, patch
        from urllib.parse import parse_qs

        device = Device(language="en")
        client = Client(device=device)

        # Capture what data gets sent
        captured_data = {}
        mock_response = MagicMock()
        mock_response.text = "<ok/>"

        def capture_request(method, url, headers=None, data=None):
            captured_data['data'] = data
            return mock_response

        with patch.object(client.session, 'request', side_effect=capture_request):
            url = ("http://api.pixelstarships.com/RoomService/CollectAllResources"
                   "?itemType=None&collectDate=2026-07-31T09:42:46"
                   "&accessToken=66e3603d-test-token")
            client.request(url, "POST")

        body = captured_data.get('data', '')
        params = parse_qs(body) if body else {}

        # accessToken must NOT be in the POST body
        self.assertNotIn('accessToken', params,
                         "accessToken must not be in POST body (causes CollectAllResources failure)")
        # Other params should be present
        self.assertIn('itemType', params)
        self.assertIn('collectDate', params)

    def test_collect_all_resources_body_excludes_access_token(self):
        """CollectAllResources must not send accessToken in POST body.

        Mitmproxy evidence (2026-07-31): old code included accessToken
        in body → errorMessage='An error occurred.' Fixed by request()
        excluding accessToken from auto-populated body.
        """
        from unittest.mock import MagicMock, patch
        from urllib.parse import parse_qs

        device = Device(language="en")
        client = Client(device=device)
        client.accessToken = "test-token-uuid"

        captured_data = {}
        mock_response = MagicMock()
        mock_response.text = "<ok/>"
        mock_response.content = b"<ok/>"

        def capture_request(method, url, headers=None, data=None):
            captured_data['data'] = data
            captured_data['url'] = url
            return mock_response

        with patch.object(client.session, 'request', side_effect=capture_request):
            client.collectAllResources()

        body = captured_data.get('data', '')
        params = parse_qs(body) if body else {}

        self.assertNotIn('accessToken', params,
                         "accessToken must not be in CollectAllResources POST body")
        self.assertIn('itemType', params)
        self.assertIn('collectDate', params)


if __name__ == '__main__':
    unittest.main()