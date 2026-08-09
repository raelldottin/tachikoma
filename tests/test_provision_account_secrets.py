import io
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import scripts.provision_account_secrets as pas


class TestProvisionAccountSecrets(unittest.TestCase):

    def setUp(self):
        # Clear PSS_ACCOUNT_* env vars before each test
        self.env_cleaner = patch.dict(os.environ, {}, clear=True)
        self.env_cleaner.start()

    def tearDown(self):
        self.env_cleaner.stop()

    def test_missing_ratelimit_dependency(self):
        """Verify regression behavior when ratelimit module is absent."""
        with patch.dict("sys.modules", {"ratelimit": None}):
            # Clear cached imports
            sys.modules.pop("sdk.client", None)
            sys.modules.pop("scripts.provision_account_secrets", None)
            with self.assertRaises(SystemExit) as cm:
                __import__("scripts.provision_account_secrets")
            self.assertEqual(cm.exception.code, 1)

    def test_zero_accounts_configured(self):
        """Verify exit status 0, stdout summary message, and zero network calls when no env vars are set."""
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO), \
             patch("scripts.provision_account_secrets.provision_account") as mock_prov, \
             patch("sdk.client.Client") as mock_client:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 0)
            stdout_val = mock_stdout.getvalue()
            self.assertIn("No accounts configured. Safe exit 0.", stdout_val)
            mock_prov.assert_not_called()
            mock_client.assert_not_called()

    @patch("scripts.provision_account_secrets.provision_account")
    def test_one_account_success(self, mock_prov):
        """Verify 1 configured account succeeds with exit 0 and produces sanitized stdout."""
        mock_prov.return_value = True
        os.environ["PSS_ACCOUNT_1_EMAIL"] = "user1@example.com"
        os.environ["PSS_ACCOUNT_1_PASSWORD"] = "pass123"

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 0)
            stdout_val = mock_stdout.getvalue()
            self.assertIn("Account 1: SUCCESS", stdout_val)
            self.assertNotIn("user1@example.com", stdout_val)
            self.assertNotIn("pass123", stdout_val)

    @patch("scripts.provision_account_secrets.provision_account")
    def test_one_account_failure(self, mock_prov):
        """Verify 1 configured account failure yields exit 1 and sanitized stderr without token leak."""
        mock_prov.side_effect = RuntimeError("DeviceLogin17 failed")
        os.environ["PSS_ACCOUNT_1_EMAIL"] = "user1@example.com"
        os.environ["PSS_ACCOUNT_1_PASSWORD"] = "pass123"

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 1)
            stdout_val = mock_stdout.getvalue()
            stderr_val = mock_stderr.getvalue()
            self.assertIn("Account 1: FAILED", stdout_val)
            self.assertIn("Account 1: FAILED", stderr_val)
            self.assertNotIn("pass123", stderr_val)

    @patch("scripts.provision_account_secrets.provision_account")
    def test_five_accounts_all_success(self, mock_prov):
        """Verify 5 configured accounts are all processed independently and return exit 0."""
        mock_prov.return_value = True
        for i in range(1, 6):
            os.environ[f"PSS_ACCOUNT_{i}_EMAIL"] = f"user{i}@example.com"
            os.environ[f"PSS_ACCOUNT_{i}_PASSWORD"] = f"pass{i}"

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 0)
            stdout_val = mock_stdout.getvalue()
            for i in range(1, 6):
                self.assertIn(f"Account {i}: SUCCESS", stdout_val)
            self.assertEqual(mock_prov.call_count, 5)

    @patch("scripts.provision_account_secrets.provision_account")
    def test_five_accounts_partial_failure(self, mock_prov):
        """Verify account 1 failure does not abort remaining 4 accounts; all 5 evaluated, exit 1."""
        def side_effect(name, email, password):
            if name == "account_1":
                raise RuntimeError("Auth failure for account 1")
            return True

        mock_prov.side_effect = side_effect
        for i in range(1, 6):
            os.environ[f"PSS_ACCOUNT_{i}_EMAIL"] = f"user{i}@example.com"
            os.environ[f"PSS_ACCOUNT_{i}_PASSWORD"] = f"pass{i}"

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 1)
            stdout_val = mock_stdout.getvalue()
            self.assertIn("Account 1: FAILED", stdout_val)
            for i in range(2, 6):
                self.assertIn(f"Account {i}: SUCCESS", stdout_val)
            self.assertEqual(mock_prov.call_count, 5)

    @patch("scripts.provision_account_secrets.provision_account")
    def test_partial_account_email_no_password(self, mock_prov):
        """Verify fast fail before network activity, slot error message, and exit status 1."""
        os.environ["PSS_ACCOUNT_1_EMAIL"] = "user1@example.com"
        # Password is missing

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 1)
            stderr_val = mock_stderr.getvalue()
            stdout_val = mock_stdout.getvalue()
            self.assertIn("Account 1: Partial configuration - missing password", stderr_val)
            self.assertIn("Account 1: PARTIAL_CONFIG_FAILED", stdout_val)
            mock_prov.assert_not_called()

    @patch("scripts.provision_account_secrets.provision_account")
    def test_partial_account_password_no_email(self, mock_prov):
        """Verify fast fail before network activity, slot error message, and exit status 1."""
        os.environ["PSS_ACCOUNT_1_PASSWORD"] = "pass123"
        # Email is missing

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 1)
            stderr_val = mock_stderr.getvalue()
            stdout_val = mock_stdout.getvalue()
            self.assertIn("Account 1: Partial configuration - missing email", stderr_val)
            self.assertIn("Account 1: PARTIAL_CONFIG_FAILED", stdout_val)
            mock_prov.assert_not_called()

    @patch("scripts.provision_account_secrets.provision_account")
    def test_token_safety_stdout_stderr(self, mock_prov):
        """Assert credentials NEVER appear in captured stdout or stderr."""
        secret_password = "PASSWORD_SECRET_8888"
        secret_email = "secret_user@example.com"

        os.environ["PSS_ACCOUNT_1_EMAIL"] = secret_email
        os.environ["PSS_ACCOUNT_1_PASSWORD"] = secret_password

        # Success run check
        mock_prov.return_value = True
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 0)
            combined_output = mock_stdout.getvalue() + mock_stderr.getvalue()
            self.assertNotIn(secret_password, combined_output)
            self.assertNotIn(secret_email, combined_output)

        # Failure run check
        mock_prov.side_effect = RuntimeError(f"Rotation error with password {secret_password}")
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 1)
            combined_output = mock_stdout.getvalue() + mock_stderr.getvalue()
            self.assertNotIn(secret_password, combined_output)
            self.assertNotIn(secret_email, combined_output)

    @patch("scripts.provision_account_secrets.Client")
    @patch("scripts.provision_account_secrets.Device")
    def test_mocked_failed_token_rotation_sanitized(self, mock_device_cls, mock_client_cls):
        """Verify useful redacted error message when token rotation raises an exception."""
        mock_device = MagicMock()
        mock_device_cls.return_value = mock_device

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.create_device_session.return_value = True
        mock_client.accessToken = "mock_access_token_123"
        mock_client.authorize_email_password.side_effect = Exception(
            "Authorization error for password=secret_password_123"
        )

        os.environ["PSS_ACCOUNT_1_EMAIL"] = "user1@example.com"
        os.environ["PSS_ACCOUNT_1_PASSWORD"] = "secret_password_123"

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 1)
            stderr_val = mock_stderr.getvalue()
            stdout_val = mock_stdout.getvalue()
            self.assertIn("Account 1: FAILED", stdout_val)
            self.assertIn("Account 1: FAILED", stderr_val)
            self.assertNotIn("secret_password_123", stderr_val)

    @patch("scripts.provision_account_secrets.provision_account")
    def test_idempotency_repeated_execution(self, mock_prov):
        """Execute provisioning twice sequentially with identical account configurations,
        asserting that both runs succeed with exit code 0, produce consistent safe output,
        and perform zero unneeded actions."""
        mock_prov.return_value = True
        os.environ["PSS_ACCOUNT_1_EMAIL"] = "user1@example.com"
        os.environ["PSS_ACCOUNT_1_PASSWORD"] = "pass123"

        # First run
        with patch("sys.stdout", new_callable=io.StringIO) as stdout_1, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr_1:
            with self.assertRaises(SystemExit) as cm1:
                pas.main()
            self.assertEqual(cm1.exception.code, 0)
            out1 = stdout_1.getvalue()
            err1 = stderr_1.getvalue()

        self.assertEqual(mock_prov.call_count, 1)

        # Second run with identical configuration
        with patch("sys.stdout", new_callable=io.StringIO) as stdout_2, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr_2:
            with self.assertRaises(SystemExit) as cm2:
                pas.main()
            self.assertEqual(cm2.exception.code, 0)
            out2 = stdout_2.getvalue()
            err2 = stderr_2.getvalue()

        # Both runs produce consistent safe output
        self.assertEqual(out1, out2)
        self.assertEqual(err1, err2)
        self.assertIn("Account 1: SUCCESS", out2)
        self.assertNotIn("user1@example.com", out2)
        self.assertNotIn("pass123", out2)

        # Both runs completed with exactly expected calls and zero unneeded actions
        self.assertEqual(mock_prov.call_count, 2)

    @patch("scripts.provision_account_secrets.provision_account")
    def test_redaction_unprefixed_secrets_in_exceptions(self, mock_prov):
        """Mock an exception containing raw un-prefixed password and email strings,
        asserting that captured stderr has zero unredacted secret values."""
        raw_password = "RawUnprefixedPassword999!"
        raw_email = "unprefixed_user@example.com"

        mock_prov.side_effect = RuntimeError(
            f"Authentication failure: {raw_password} for {raw_email}"
        )

        os.environ["PSS_ACCOUNT_1_EMAIL"] = raw_email
        os.environ["PSS_ACCOUNT_1_PASSWORD"] = raw_password

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 1)
            stdout_val = mock_stdout.getvalue()
            stderr_val = mock_stderr.getvalue()
            self.assertIn("Account 1: FAILED", stdout_val)
            self.assertIn("Account 1: FAILED", stderr_val)
            self.assertNotIn(raw_password, stderr_val)
            self.assertNotIn(raw_email, stderr_val)
            self.assertNotIn(raw_password, stdout_val)
            self.assertNotIn(raw_email, stdout_val)
            self.assertIn("***REDACTED***", stderr_val)


if __name__ == "__main__":
    unittest.main()