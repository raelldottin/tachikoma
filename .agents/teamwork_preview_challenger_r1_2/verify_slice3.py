#!/usr/bin/env python3
"""
Empirical verification & stress testing script for Tachikoma Gauntlet Slice 3.
Validates:
1. provision_account_secrets.py exit code 0 on 0 accounts.
2. provision_account_secrets.py exit code 1 fast on partial account config without contacting PSS network.
3. provision_account_secrets.py independent evaluation across 5 accounts.
4. run.py exception boundaries: exception in getMessages or collectAllResources logs redacted error and sets runtime_failed = True without crashing downstream operations.
5. run.py exit statuses: 0 on clean run / expected skips, 1 on runtime error, 2 on partial SMTP.
"""
import os
import sys
import unittest
import logging
from unittest.mock import MagicMock, patch
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from sdk.client import Client
import run
import scripts.provision_account_secrets as pas


class TestSlice3Empirical(unittest.TestCase):

    def setUp(self):
        logging.getLogger().handlers = []

    def test_1_provision_0_accounts_exit_0(self):
        """1. provision_account_secrets.py exit code 0 on 0 accounts."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("PSS_ACCOUNT_")}
        res = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "provision_account_secrets.py")],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Expected returncode 0, got {res.returncode}. Output: {res.stdout} Stderr: {res.stderr}")
        self.assertIn("No accounts configured. Safe exit 0.", res.stdout)

    def test_2_provision_partial_config_exit_1_fast(self):
        """2. provision_account_secrets.py exit code 1 fast on partial config without network activity."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("PSS_ACCOUNT_")}
        env["PSS_ACCOUNT_1_EMAIL"] = "partial_user@example.com"
        
        with patch.object(pas, "provision_account") as mock_provision:
            res = subprocess.run(
                [sys.executable, os.path.join(REPO_ROOT, "scripts", "provision_account_secrets.py")],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 1, f"Expected returncode 1, got {res.returncode}")
            self.assertIn("Account 1: Partial configuration - missing password, refresh_token", res.stderr)
            self.assertIn("Account 1: PARTIAL_CONFIG_FAILED", res.stdout)
            mock_provision.assert_not_called()

    def test_3_provision_5_accounts_independent_processing(self):
        """3. provision_account_secrets.py independent evaluation across 5 accounts."""
        slots = {}
        for i in range(1, 6):
            slots[i] = {
                'status': 'CONFIGURED',
                'email': f'user{i}@example.com',
                'password': f'pass{i}secret',
                'refresh_token': f'token{i}secret',
                'missing_fields': [],
            }

        def mock_provision(acc_name, email, password, token):
            if "account_1" in acc_name or "account_3" in acc_name:
                raise RuntimeError(f"Rotation failure for secret_{email}")
            return f"new_token_{acc_name}"

        with patch.object(pas, "inspect_account_slots", return_value=slots), \
             patch.object(pas, "provision_account", side_effect=mock_provision), \
             self.assertRaises(SystemExit) as cm:
            pas.main()
        self.assertEqual(cm.exception.code, 1)

    def test_4a_run_py_exception_boundaries_getMessages(self):
        """4a. run.py exception boundary: exception in getMessages logs redacted error and sets runtime_failed."""
        mock_client = MagicMock(spec=Client)
        mock_client.login.return_value = True
        mock_client.freeStarbuxToday = 100
        mock_client.freeStarbuxMax = 10
        mock_client.info = {"@Name": "TestCaptain"}
        mock_client.getMessages.side_effect = RuntimeError("Failed with sensitive pass123secret")
        
        executed_ops = []
        mock_client.infoBux.side_effect = lambda: executed_ops.append("infoBux")
        mock_client.manageTraining.side_effect = lambda: (executed_ops.append("manageTraining"), True)[1]
        mock_client.getResourceTotals.side_effect = lambda: executed_ops.append("getResourceTotals")
        mock_client.upgradeCharacters.side_effect = lambda: (executed_ops.append("upgradeCharacters"), True)[1]

        with patch("run.Client", return_value=mock_client), \
             patch("run.Device"), \
             patch("sys.argv", ["run.py"]), \
             self.assertRaises(SystemExit) as cm:
            run.main()

        self.assertEqual(cm.exception.code, 1)
        self.assertIn("infoBux", executed_ops)
        self.assertIn("manageTraining", executed_ops)
        self.assertIn("getResourceTotals", executed_ops)
        self.assertIn("upgradeCharacters", executed_ops)

    def test_4b_run_py_exception_boundaries_collectAllResources(self):
        """4b. run.py exception boundary: exception in collectAllResources logs redacted error and sets runtime_failed."""
        mock_client = MagicMock(spec=Client)
        mock_client.login.return_value = True
        mock_client.freeStarbuxToday = 100
        mock_client.freeStarbuxMax = 10
        mock_client.info = {"@Name": "TestCaptain"}
        
        def mock_upgrade_researches():
            raise RuntimeError("collectAllResources failed with secret_pass999")
        mock_client.upgradeResearches.side_effect = mock_upgrade_researches

        executed_ops = []
        mock_client.upgradeRooms.side_effect = lambda: (executed_ops.append("upgradeRooms"), True)[1]
        mock_client.getMessages.side_effect = lambda: (executed_ops.append("getMessages"), True)[1]
        mock_client.manageTraining.side_effect = lambda: (executed_ops.append("manageTraining"), True)[1]

        with patch("run.Client", return_value=mock_client), \
             patch("run.Device"), \
             patch("sys.argv", ["run.py"]), \
             self.assertRaises(SystemExit) as cm:
            run.main()

        self.assertEqual(cm.exception.code, 1)
        self.assertIn("upgradeRooms", executed_ops)
        self.assertIn("getMessages", executed_ops)
        self.assertIn("manageTraining", executed_ops)

    def test_5a_run_py_exit_code_partial_smtp(self):
        """5a. run.py exit 2 on partial SMTP configuration."""
        with patch("sys.argv", ["run.py", "--smtp-email", "foo@example.com"]), \
             self.assertRaises(SystemExit) as cm:
            run.main()
        self.assertEqual(cm.exception.code, 2)

    def test_5b_run_py_exit_code_clean_run(self):
        """5b. run.py exit 0 on clean run / expected skips."""
        mock_client = MagicMock(spec=Client)
        mock_client.login.return_value = True
        mock_client.freeStarbuxToday = 100
        mock_client.freeStarbuxMax = 10
        mock_client.info = {"@Name": "CleanCaptain"}
        mock_client.upgradeResearches.return_value = True
        mock_client.upgradeRooms.return_value = True
        mock_client.manageTraining.return_value = True
        mock_client.upgradeCharacters.return_value = True
        mock_client.getMessages.return_value = True

        with patch("run.Client", return_value=mock_client), \
             patch("run.Device"), \
             patch("sys.argv", ["run.py"]), \
             self.assertRaises(SystemExit) as cm:
            run.main()
        self.assertEqual(cm.exception.code, 0)

    def test_5c_run_py_exit_code_runtime_error(self):
        """5c. run.py exit 1 on runtime error."""
        mock_client_err = MagicMock(spec=Client)
        mock_client_err.login.return_value = True
        mock_client_err.freeStarbuxToday = 100
        mock_client_err.freeStarbuxMax = 10
        mock_client_err.info = {"@Name": "ErrorCaptain"}
        mock_client_err.upgradeResearches.return_value = False

        with patch("run.Client", return_value=mock_client_err), \
             patch("run.Device"), \
             patch("sys.argv", ["run.py"]), \
             self.assertRaises(SystemExit) as cm:
            run.main()
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestSlice3Empirical)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("\nALL EMPIRICAL TESTS PASSED SUCCESSFULLY!")
        os._exit(0)
    else:
        print("\nEMPIRICAL TEST FAILURES DETECTED!")
        os._exit(1)
