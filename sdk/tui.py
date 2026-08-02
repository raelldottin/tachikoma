from __future__ import annotations

"""Interactive TUI command loop for Tachikoma."""

import sys
import getpass
from typing import Optional

from sdk.client import Client
from sdk.device import Device
from sdk.commands import create_command_registry, CommandRegistry


class TUI:
    """Interactive TUI for Pixel Starships automation."""

    def __init__(self, device: Device):
        self.device = device
        self.client: Optional[Client] = None
        self.registry: Optional[CommandRegistry] = None
        self.running = False

    def run(self):
        """Main TUI entry point."""
        self.running = True
        self._authenticate()
        if not self.client or not self.client.accessToken:
            print("Authentication failed. Exiting.")
            return

        print(f"\nAuthenticated as {self.client.info.get('@Name', 'Unknown')}")
        print('Type "help" for commands.\n')

        self.registry = create_command_registry(self.client)
        self._command_loop()

    def _authenticate(self):
        """Interactive email/password authentication."""
        print("=== Tachikoma Interactive Login ===")
        email = input("Email: ").strip()
        if not email:
            print("Email required.")
            return

        password = getpass.getpass("Password: ").strip()
        if not password:
            print("Password required.")
            return

        # Create client with email/password login enabled
        self.client = Client(
            device=self.device,
            settings={
                "checksum_key": "5343",
                "savy_checksum": "Savvy!s0d@",
                "allow_email_password_login": True,
            },
        )

        print("\nAuthenticating...")
        success = self.client.login(email=email, password=password)

        # Clear password reference immediately
        password = None

        if success:
            print("Authentication successful!")
        else:
            print("Authentication failed.")
            self.client = None

    def _command_loop(self):
        """Main command REPL loop."""
        assert self.registry is not None

        while self.running:
            try:
                line = input("tachikoma> ").strip()
                if not line:
                    continue

                parts = line.split()
                cmd_name = parts[0].lower()
                args = parts[1:]

                if cmd_name in ("exit", "quit"):
                    self.running = False
                    print("Goodbye!")
                    break

                cmd = self.registry.get(cmd_name)
                if not cmd:
                    print(f"Unknown command: {cmd_name}")
                    print('Type "help" for available commands.')
                    continue

                try:
                    result = cmd["handler"](args)
                    if result:
                        print(result)
                except SystemExit as e:
                    print(e)
                    self.running = False
                    break
                except Exception as e:
                    print(f"Error: {e}")

            except (KeyboardInterrupt, EOFError):
                print("\nUse 'exit' to quit.")
                continue


def run_tui(device: Device) -> int:
    """Entry point for the TUI. Returns exit code."""
    tui = TUI(device)
    tui.run()
    return 0