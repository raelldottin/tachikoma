from __future__ import annotations

"""Tests for the TUI module."""

import sys
import io
import unittest
from unittest.mock import patch, MagicMock, call

# Add parent directory to path for imports
sys.path.insert(0, "/Users/raelldottin/Documents/Personal/tachikoma")

from sdk.tui import TUI, run_tui
from sdk.commands import CommandRegistry, create_command_registry
from sdk.client import Client
from sdk.device import Device


class TestCommandRegistry(unittest.TestCase):
    """Tests for CommandRegistry."""

    def setUp(self):
        self.device = Device(language="en")
        self.client = Client(device=self.device, settings={"checksum_key": "5343", "savy_checksum": "Savvy!s0d@"})
        self.registry = create_command_registry(self.client)

    def test_register_default_commands(self):
        """Test that default commands are registered."""
        commands = self.registry.list_commands()
        expected = ["exit", "help", "logout", "quit", "refresh", "ship", "status"]
        self.assertEqual(sorted(commands), expected)

    def test_is_verified(self):
        """Test verified flag on commands."""
        self.assertTrue(self.registry.is_verified("help"))
        self.assertTrue(self.registry.is_verified("status"))
        self.assertTrue(self.registry.is_verified("ship"))
        self.assertTrue(self.registry.is_verified("refresh"))
        self.assertTrue(self.registry.is_verified("logout"))
        self.assertTrue(self.registry.is_verified("exit"))
        self.assertTrue(self.registry.is_verified("quit"))

    def test_unknown_command(self):
        """Test unknown command returns None."""
        self.assertIsNone(self.registry.get("unknown"))

    def test_cmd_help(self):
        """Test help command output."""
        result = self.registry._cmd_help([])
        self.assertIn("Available commands:", result)
        self.assertIn("help", result)
        self.assertIn("status", result)

    def test_cmd_help_specific(self):
        """Test help command for specific command."""
        result = self.registry._cmd_help(["status"])
        self.assertIn("Usage: status", result)
        self.assertIn("session state", result)

    def test_cmd_status_no_auth(self):
        """Test status command when not authenticated."""
        # Create a fresh client without auth
        device = Device(language="en")
        client = Client(device=device)
        registry = create_command_registry(client)

        result = registry._cmd_status([])
        self.assertIn("Authenticated: False", result)
        self.assertIn("Refresh token: Not present", result)

    def test_cmd_status_with_auth(self):
        """Test status command with authentication."""
        device = Device(language="en")
        client = Client(device=device, settings={"checksum_key": "5343", "savy_checksum": "Savvy!s0d@"})
        client.accessToken = "test-access-token"
        client.device.refreshToken = "test-refresh-token"
        client.device.key = "TEST-KEY"
        client.device.name = "iOS"
        client.info = {"@Name": "TestCaptain"}
        client.credits = 1000
        registry = create_command_registry(client)

        result = registry._cmd_status([])
        self.assertIn("Authenticated: True", result)
        self.assertIn("Access token: Present", result)
        self.assertIn("Refresh token: Present", result)
        self.assertIn("Device identity: Configured", result)
        self.assertIn("TestCaptain", result)
        self.assertIn("Credits: 1000", result)

    def test_cmd_ship_no_auth(self):
        """Test ship command when not authenticated."""
        device = Device(language="en")
        client = Client(device=device)
        registry = create_command_registry(client)

        result = registry._cmd_ship([])
        self.assertEqual(result, "Not authenticated. Run 'refresh' or login again.")

    def test_cmd_refresh_no_token(self):
        """Test refresh command with no token."""
        device = Device(language="en")
        client = Client(device=device)
        registry = create_command_registry(client)

        result = registry._cmd_refresh([])
        self.assertEqual(result, "No refresh token stored. Cannot refresh.")

    def test_cmd_logout(self):
        """Test logout command raises SystemExit."""
        self.client.accessToken = "test-token"
        self.client.device.refreshToken = "test-refresh"

        with self.assertRaises(SystemExit) as cm:
            self.registry._cmd_logout([])
        self.assertEqual(str(cm.exception), "Logged out. Stored session cleared.")
        self.assertIsNone(self.client.accessToken)
        self.assertIsNone(self.client.device.refreshToken)

    def test_cmd_exit(self):
        """Test exit command raises SystemExit."""
        with self.assertRaises(SystemExit) as cm:
            self.registry._cmd_exit([])
        self.assertEqual(str(cm.exception), "Goodbye!")


class TestTUI(unittest.TestCase):
    """Tests for TUI class."""

    def setUp(self):
        self.device = Device(language="en")

    @patch("sdk.tui.input", side_effect=["test@example.com"])
    @patch("sdk.tui.getpass.getpass", side_effect=["testpass"])
    @patch("sdk.tui.Client")
    def test_authenticate_success(self, mock_client_class, mock_getpass, mock_input):
        """Test successful authentication."""
        mock_client = MagicMock()
        mock_client.login.return_value = True
        mock_client.accessToken = "test-token"
        mock_client.info = {"@Name": "TestCaptain"}
        mock_client_class.return_value = mock_client

        tui = TUI(self.device)
        tui._authenticate()

        self.assertIsNotNone(tui.client)
        mock_client.login.assert_called_once_with(email="test@example.com", password="testpass")

    @patch("sdk.tui.input", side_effect=[""])
    def test_authenticate_empty_email(self, mock_input):
        """Test authentication with empty email."""
        tui = TUI(self.device)
        tui._authenticate()
        self.assertIsNone(tui.client)

    @patch("sdk.tui.input", side_effect=["test@example.com"])
    @patch("sdk.tui.getpass.getpass", side_effect=[""])
    def test_authenticate_empty_password(self, mock_getpass, mock_input):
        """Test authentication with empty password."""
        tui = TUI(self.device)
        tui._authenticate()
        self.assertIsNone(tui.client)

    @patch("sdk.tui.input", side_effect=["test@example.com"])
    @patch("sdk.tui.getpass.getpass", side_effect=["testpass"])
    @patch("sdk.tui.Client")
    def test_authenticate_failure(self, mock_client_class, mock_getpass, mock_input):
        """Test failed authentication."""
        mock_client = MagicMock()
        mock_client.login.return_value = False
        mock_client_class.return_value = mock_client

        tui = TUI(self.device)
        tui._authenticate()

        self.assertIsNone(tui.client)

    @patch("sdk.tui.input", side_effect=["help", "exit"])
    @patch("sdk.tui.TUI._authenticate")
    def test_command_loop_help(self, mock_auth, mock_input):
        """Test command loop with help command."""
        tui = TUI(self.device)
        tui.client = MagicMock()
        tui.client.accessToken = "test-token"
        tui.client.info = {"@Name": "TestCaptain"}
        tui.registry = create_command_registry(tui.client)
        tui.running = True

        tui._command_loop()

        # Should have called help and then exit
        mock_input.assert_has_calls([call("tachikoma> "), call("tachikoma> ")])

    @patch("sdk.tui.input", side_effect=["unknown", "exit"])
    @patch("sdk.tui.TUI._authenticate")
    def test_command_loop_unknown(self, mock_auth, mock_input):
        """Test command loop with unknown command."""
        tui = TUI(self.device)
        tui.client = MagicMock()
        tui.client.accessToken = "test-token"
        tui.registry = create_command_registry(tui.client)
        tui.running = True

        tui._command_loop()

    @patch("sdk.tui.input")
    @patch("sdk.tui.TUI._authenticate")
    def test_command_loop_keyboard_interrupt(self, mock_auth, mock_input):
        """Test command loop handles KeyboardInterrupt."""
        tui = TUI(self.device)
        tui.client = MagicMock()
        tui.client.accessToken = "test-token"
        tui.registry = create_command_registry(tui.client)
        tui.running = True

        # Make input raise KeyboardInterrupt on first call, then return 'exit' to break
        mock_input.side_effect = [KeyboardInterrupt(), "exit"]

        tui._command_loop()
        # After KeyboardInterrupt, loop continues and processes 'exit', so running becomes False
        self.assertFalse(tui.running)

    @patch("sdk.tui.input")
    @patch("sdk.tui.TUI._authenticate")
    def test_command_loop_eof(self, mock_auth, mock_input):
        """Test command loop handles EOFError (Ctrl-D)."""
        tui = TUI(self.device)
        tui.client = MagicMock()
        tui.client.accessToken = "test-token"
        tui.registry = create_command_registry(tui.client)
        tui.running = True

        mock_input.side_effect = EOFError()

        tui._command_loop()
        self.assertFalse(tui.running)


class TestRunTUI(unittest.TestCase):
    """Tests for run_tui entry point."""

    @patch("sdk.tui.TUI.run")
    def test_run_tui(self, mock_run):
        """Test run_tui entry point."""
        device = Device(language="en")
        result = run_tui(device)
        self.assertEqual(result, 0)
        mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()