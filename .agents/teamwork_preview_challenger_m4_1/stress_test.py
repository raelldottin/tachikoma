#!/usr/bin/env python3
"""
Empirical Adversarial Stress Test Suite for provision_account_secrets.py
Targeting all edge cases specified in MANDATORY ASSIGNMENT and AGENTS.md / ORIGINAL_REQUEST.md.
"""
import os
import sys
import unittest
import io
from unittest.mock import patch, MagicMock

# Ensure repo root is on sys.path
REPO_ROOT = "/Users/raelldottin/Documents/Personal/tachikoma"
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import scripts.provision_account_secrets as pas


class StressTestProvisionAccountSecrets(unittest.TestCase):

    def setUp(self):
        self.env_cleaner = patch.dict(os.environ, {}, clear=True)
        self.env_cleaner.start()

    def tearDown(self):
        self.env_cleaner.stop()

    def test_01_missing_ratelimit_dependency(self):
        """Test missing ratelimit dependency causing SystemExit(1) on import."""
        with patch.dict("sys.modules", {"ratelimit": None}):
            sys.modules.pop("sdk.client", None)
            sys.modules.pop("scripts.provision_account_secrets", None)
            with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                with self.assertRaises(SystemExit) as cm:
                    pass
                self.assertEqual(cm.exception.code, 1)
                self.assertIn("Dependency error", mock_stderr.getvalue())

    def test_02_zero_accounts(self):
        """0 accounts configured -> exit 0, no network calls, sanitized message."""
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr, \
             patch("scripts.provision_account_secrets.provision_account") as mock_prov, \
             patch("sdk.client.Client") as mock_client:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("No accounts configured. Safe exit 0.", mock_stdout.getvalue())
            self.assertEqual(mock_stderr.getvalue(), "")
            mock_prov.assert_not_called()
            mock_client.assert_not_called()

    def test_03_one_account_success(self):
        """1 account fully configured -> exit 0, provision_account called once."""
        os.environ["PSS_ACCOUNT_1_EMAIL"] = "user1@example.com"
        os.environ["PSS_ACCOUNT_1_PASSWORD"] = "pass123"
        os.environ["PSS_ACCOUNT_1_REFRESH_TOKEN"] = "ref123"

        with patch("scripts.provision_account_secrets.provision_account") as mock_prov, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            mock_prov.return_value = "new_ref_123"
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 0)
            mock_prov.assert_called_once_with("account_1", "user1@example.com", "pass123", "ref123")
            stdout_val = mock_stdout.getvalue()
            self.assertIn("Account 1: SUCCESS", stdout_val)
            self.assertNotIn("user1@example.com", stdout_val)
            self.assertNotIn("pass123", stdout_val)
            self.assertNotIn("ref123", stdout_val)
            self.assertNotIn("new_ref_123", stdout_val)

    def test_04_five_accounts_all_success(self):
        """5 accounts fully configured -> exit 0, all 5 called independently."""
        for i in range(1, 6):
            os.environ[f"PSS_ACCOUNT_{i}_EMAIL"] = f"user{i}@example.com"
            os.environ[f"PSS_ACCOUNT_{i}_PASSWORD"] = f"pass{i}"
            os.environ[f"PSS_ACCOUNT_{i}_REFRESH_TOKEN"] = f"ref{i}"

        with patch("scripts.provision_account_secrets.provision_account") as mock_prov, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            mock_prov.side_effect = [f"new_ref_{i}" for i in range(1, 6)]
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertEqual(mock_prov.call_count, 5)
            stdout_val = mock_stdout.getvalue()
            for i in range(1, 6):
                self.assertIn(f"Account {i}: SUCCESS", stdout_val)

    def test_05_five_accounts_with_one_failing(self):
        """5 accounts, account 3 fails -> remaining accounts processed, exit code 1."""
        for i in range(1, 6):
            os.environ[f"PSS_ACCOUNT_{i}_EMAIL"] = f"user{i}@example.com"
            os.environ[f"PSS_ACCOUNT_{i}_PASSWORD"] = f"pass{i}"
            os.environ[f"PSS_ACCOUNT_{i}_REFRESH_TOKEN"] = f"ref{i}"

        def mock_side_effect(name, email, password, refresh):
            if name == "account_3":
                raise RuntimeError("DeviceLogin17 failed for account 3 token=ref3_secret")
            return f"new_ref_{name}"

        with patch("scripts.provision_account_secrets.provision_account", side_effect=mock_side_effect) as mock_prov, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 1)
            self.assertEqual(mock_prov.call_count, 5)
            stdout_val = mock_stdout.getvalue()
            stderr_val = mock_stderr.getvalue()
            self.assertIn("Account 1: SUCCESS", stdout_val)
            self.assertIn("Account 2: SUCCESS", stdout_val)
            self.assertIn("Account 3: FAILED", stdout_val)
            self.assertIn("Account 4: SUCCESS", stdout_val)
            self.assertIn("Account 5: SUCCESS", stdout_val)
            self.assertNotIn("ref3_secret", stderr_val)

    def test_06_partial_email_no_password(self):
        """Partial config: email + refresh_token, missing password -> exit 1, no provision_account calls."""
        os.environ["PSS_ACCOUNT_1_EMAIL"] = "user1@example.com"
        os.environ["PSS_ACCOUNT_1_REFRESH_TOKEN"] = "ref1"

        with patch("scripts.provision_account_secrets.provision_account") as mock_prov, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 1)
            mock_prov.assert_not_called()
            self.assertIn("Account 1: Partial configuration - missing password", mock_stderr.getvalue())
            self.assertIn("Account 1: PARTIAL_CONFIG_FAILED", mock_stdout.getvalue())

    def test_07_partial_password_no_email(self):
        """Partial config: password + refresh_token, missing email -> exit 1, no provision_account calls."""
        os.environ["PSS_ACCOUNT_1_PASSWORD"] = "pass1"
        os.environ["PSS_ACCOUNT_1_REFRESH_TOKEN"] = "ref1"

        with patch("scripts.provision_account_secrets.provision_account") as mock_prov, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 1)
            mock_prov.assert_not_called()
            self.assertIn("Account 1: Partial configuration - missing email", mock_stderr.getvalue())
            self.assertIn("Account 1: PARTIAL_CONFIG_FAILED", mock_stdout.getvalue())

    def test_08_partial_email_and_password_no_refresh(self):
        """Partial config: email + password, missing refresh_token -> exit 1, no provision_account calls."""
        os.environ["PSS_ACCOUNT_1_EMAIL"] = "user1@example.com"
        os.environ["PSS_ACCOUNT_1_PASSWORD"] = "pass1"

        with patch("scripts.provision_account_secrets.provision_account") as mock_prov, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 1)
            mock_prov.assert_not_called()
            self.assertIn("Account 1: Partial configuration - missing refresh_token", mock_stderr.getvalue())

    def test_09_whitespace_only_env_vars(self):
        """Whitespace-only env vars treated as unconfigured."""
        os.environ["PSS_ACCOUNT_1_EMAIL"] = "   "
        os.environ["PSS_ACCOUNT_1_PASSWORD"] = "\t\n"
        os.environ["PSS_ACCOUNT_1_REFRESH_TOKEN"] = " "

        with patch("scripts.provision_account_secrets.provision_account") as mock_prov, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with self.assertRaises(SystemExit) as cm:
                pas.main()
            self.assertEqual(cm.exception.code, 0)
            mock_prov.assert_not_called()
            self.assertIn("No accounts configured. Safe exit 0.", mock_stdout.getvalue())

    def test_10_adversarial_exception_with_tokens_and_passwords(self):
        """Test exception carrying raw credentials (refresh token, access token, password, email) is redacted."""
        os.environ["PSS_ACCOUNT_1_EMAIL"] = "victim@domain.org"
        os.environ["PSS_ACCOUNT_1_PASSWORD"] = "SuperSecretPassword123!"
        os.environ["PSS_ACCOUNT_1_REFRESH_TOKEN"] = "REFRESH_TOKEN_ABC123XYZ"

        with patch("scripts.provision_account_secrets.provision_account") as mock_prov, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            leak_msg = ("Failed to rotate: accessToken=ACCESS_TOKEN_99999 "
                        "refreshToken=REFRESH_TOKEN_ABC123XYZ "
                        "password=SuperSecretPassword123! "
                        "email=victim@domain.org")
            mock_prov.side_effect = RuntimeError(leak_msg)

            with self.assertRaises(SystemExit) as cm:
                pas.main()

            self.assertEqual(cm.exception.code, 1)
            output = mock_stdout.getvalue() + mock_stderr.getvalue()
            self.assertNotIn("REFRESH_TOKEN_ABC123XYZ", output)
            self.assertNotIn("ACCESS_TOKEN_99999", output)
            self.assertNotIn("SuperSecretPassword123!", output)
            self.assertNotIn("victim@domain.org", output)

    def test_11_underlying_provision_account_function_with_mocked_client(self):
        """Test provision_account function directly under mocked Client and Device."""
        with patch("scripts.provision_account_secrets.Client") as mock_client_cls, \
             patch("scripts.provision_account_secrets.Device") as mock_device_cls:
            mock_device = MagicMock()
            mock_device_cls.return_value = mock_device

            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.create_device_session.return_value = True
            mock_client.accessToken = "acc_token_123"
            mock_client.authorize_email_password.return_value = True
            mock_device.refreshToken = "rotated_ref_token_456"

            res = pas.provision_account("account_1", "email@test.com", "pass123", "orig_ref_token")
            self.assertEqual(res, "rotated_ref_token_456")
            mock_client.create_device_session.assert_called_once()
            mock_client.authorize_email_password.assert_called_once_with("email@test.com", "pass123")

    def test_12_underlying_provision_account_device_login_failure(self):
        """Test provision_account raising RuntimeError when DeviceLogin17 fails."""
        with patch("scripts.provision_account_secrets.Client") as mock_client_cls, \
             patch("scripts.provision_account_secrets.Device"):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.create_device_session.return_value = False

            with self.assertRaises(RuntimeError) as cm:
                pas.provision_account("account_1", "email@test.com", "pass123", "orig_ref")
            self.assertIn("account_1: DeviceLogin17 failed", str(cm.exception))

    def test_13_underlying_provision_account_no_access_token(self):
        """Test provision_account raising RuntimeError when accessToken is missing after DeviceLogin17."""
        with patch("scripts.provision_account_secrets.Client") as mock_client_cls, \
             patch("scripts.provision_account_secrets.Device"):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.create_device_session.return_value = True
            mock_client.accessToken = None

            with self.assertRaises(RuntimeError) as cm:
                pas.provision_account("account_1", "email@test.com", "pass123", "orig_ref")
            self.assertIn("account_1: No accessToken from DeviceLogin17", str(cm.exception))

    def test_14_underlying_provision_account_authorize_failed(self):
        """Test provision_account raising RuntimeError when authorize_email_password fails."""
        with patch("scripts.provision_account_secrets.Client") as mock_client_cls, \
             patch("scripts.provision_account_secrets.Device"):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.create_device_session.return_value = True
            mock_client.accessToken = "acc_token"
            mock_client.authorize_email_password.return_value = False

            with self.assertRaises(RuntimeError) as cm:
                pas.provision_account("account_1", "email@test.com", "pass123", "orig_ref")
            self.assertIn("account_1: Email/password authorize failed", str(cm.exception))

    def test_15_underlying_provision_account_no_new_refresh_token(self):
        """Test provision_account raising RuntimeError when refreshToken is missing after rotation."""
        with patch("scripts.provision_account_secrets.Client") as mock_client_cls, \
             patch("scripts.provision_account_secrets.Device") as mock_device_cls:
            mock_device = MagicMock()
            mock_device_cls.return_value = mock_device
            mock_device.refreshToken = None

            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.create_device_session.return_value = True
            mock_client.accessToken = "acc_token"
            mock_client.authorize_email_password.return_value = True

            with self.assertRaises(RuntimeError) as cm:
                pas.provision_account("account_1", "email@test.com", "pass123", "orig_ref")
            self.assertIn("account_1: No new refreshToken after rotation", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
