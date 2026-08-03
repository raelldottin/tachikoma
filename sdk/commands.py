from __future__ import annotations

"""Command registry mapping typed commands to Client methods."""

from typing import Callable, Dict, List, Optional
from sdk.client import Client


class CommandRegistry:
    """Registry of available commands mapped to Client methods."""

    def __init__(self, client: Client):
        self.client = client
        self._commands: Dict[str, Dict] = {}
        self._register_default_commands()

    def _register_default_commands(self):
        """Register the default conservative command set."""
        self.register(
            "help",
            self._cmd_help,
            "Show available commands or detailed help for a command",
            args=["command"],
        )
        self.register(
            "status",
            self._cmd_status,
            "Show session state without displaying secrets",
            args=[],
        )
        self.register(
            "ship",
            self._cmd_ship,
            "Show ship summary, rooms, and research",
            args=[],
        )
        self.register(
            "refresh",
            self._cmd_refresh,
            "Refresh session (re-authenticate using stored refresh token)",
            args=[],
        )
        self.register(
            "logout",
            self._cmd_logout,
            "Delete stored authentication and end the session",
            args=[],
        )
        self.register(
            "exit",
            self._cmd_exit,
            "Exit while preserving stored authentication",
            args=[],
        )
        self.register(
            "quit",
            self._cmd_exit,
            "Alias for exit",
            args=[],
        )
        self.register(
            "device",
            self._cmd_device,
            "Manage device key (generate, show, set)",
            args=["action", "key"],
        )

    def _cmd_device(self, args: List[str]) -> str:
        """Manage device key: generate, show, or set a permanent device key."""
        if not args:
            return "Usage: device <generate|show|set> [key]"
        
        action = args[0].lower()
        
        if action == "generate":
            new_key = self.client.device.generate_device_key()
            return f"Generated new device key: {new_key}\nSaved to .device file"
        
        elif action == "show":
            return f"Current device key: {self.client.device.key}"
        
        elif action == "set":
            if len(args) < 2:
                return "Usage: device set <key>"
            new_key = args[1].upper()
            self.client.device.set_device_key(new_key)
            return f"Set device key to: {new_key}\nSaved to .device file"
        
        else:
            return f"Unknown device action: {action}. Use generate, show, or set."

    def register(
        self,
        name: str,
        handler: Callable,
        description: str,
        args: Optional[List[str]] = None,
        verified: bool = True,
    ):
        """Register a command."""
        self._commands[name] = {
            "handler": handler,
            "description": description,
            "args": args or [],
            "verified": verified,
        }

    def get(self, name: str) -> Optional[Dict]:
        """Get command info by name."""
        return self._commands.get(name.lower())

    def list_commands(self) -> List[str]:
        """Return sorted list of command names."""
        return sorted(self._commands.keys())

    def is_verified(self, name: str) -> bool:
        """Check if a command is verified."""
        cmd = self._commands.get(name.lower())
        return cmd["verified"] if cmd else False

    # Command handlers
    def _cmd_help(self, args: List[str]) -> str:
        """Show available commands or detailed help for a command."""
        if args:
            name = args[0].lower()
            cmd = self._commands.get(name)
            if not cmd:
                return f"Unknown command: {name}"
            usage = " ".join([name] + [f"<{a}>" for a in cmd["args"]])
            status = "✓" if cmd["verified"] else "⚠ (unverified)"
            return f"Usage: {usage}\n{status} - {cmd['description']}"

        lines = ["Available commands:"]
        for name in self.list_commands():
            cmd = self._commands[name]
            status = "✓" if cmd["verified"] else "⚠ (unverified)"
            args_str = " ".join(f"<{a}>" for a in cmd["args"])
            lines.append(f"  {name} {args_str}  {status} - {cmd['description']}")
        return "\n".join(lines)

    def _cmd_status(self, args: List[str]) -> str:
        """Show authentication and session status without displaying secrets."""
        lines = ["=== Session Status ==="]
        lines.append(f"Authenticated: {self.client.accessToken is not None}")
        lines.append(f"Access token: {'Present' if self.client.accessToken else 'Not present'}")
        lines.append(f"Refresh token: {'Present' if self.client.device.refreshToken else 'Not present'}")
        lines.append(f"Device identity: Configured")
        if self.client.info.get("@Name"):
            lines.append(f"Captain: {self.client.info['@Name']}")
        if self.client.credits is not None:
            lines.append(f"Credits: {self.client.credits}")
        return "\n".join(lines)

    def _cmd_ship(self, args: List[str]) -> str:
        """Show ship information."""
        if not self.client.accessToken:
            return "Not authenticated. Run 'refresh' or login again."

        lines = ["=== Ship Information ==="]

        # Get ship data if not already loaded
        if not hasattr(self.client, 'shipByUserId') or not self.client.shipByUserId:
            if not self.client.getShipByUserId():
                return "Failed to fetch ship data."

        ship_data = self.client.shipByUserId.get("ShipService", {}).get("GetShipByUserId", {}).get("Ship", {})
        if not ship_data:
            return "No ship data available."

        lines.append(f"Ship Design ID: {ship_data.get('@ShipDesignId', 'N/A')}")
        lines.append(f"Captain: {self.client.info.get('@Name', 'N/A')}")
        lines.append(f"Credits: {self.client.credits}")

        # Rooms
        rooms_data = ship_data.get("Rooms")
        rooms = rooms_data.get("Room", []) if rooms_data else []
        if isinstance(rooms, dict):
            rooms = [rooms]
        lines.append(f"\nRooms ({len(rooms)}):")
        for room in rooms[:10]:  # Limit to first 10
            room_id = room.get("@RoomId", "?")
            design_id = room.get("@RoomDesignId", "?")
            power = room.get("@Power", "0")
            lines.append(f"  Room {room_id}: Design={design_id}, Power={power}")
        if len(rooms) > 10:
            lines.append(f"  ... and {len(rooms) - 10} more")

        # Researches
        researches_data = ship_data.get("Researches")
        researches = researches_data.get("Research", []) if researches_data else []
        if isinstance(researches, dict):
            researches = [researches]
        lines.append(f"\nResearches ({len(researches)}):")
        for research in researches[:10]:
            design_id = research.get("@ResearchDesignId", "?")
            level = research.get("@ResearchLevel", "0")
            lines.append(f"  Design {design_id}: Level {level}")
        if len(researches) > 10:
            lines.append(f"  ... and {len(researches) - 10} more")

        return "\n".join(lines)

    def _cmd_refresh(self, args: List[str]) -> str:
        """Refresh session using stored refresh token."""
        if not self.client.device.refreshToken:
            return "No refresh token stored. Cannot refresh."

        self.client.accessToken = None
        if self.client.create_device_session():
            return "Session refreshed successfully."
        else:
            return "Failed to refresh session. Refresh token may be invalid."

    def _cmd_logout(self, args: List[str]) -> str:
        """Clear stored session and exit."""
        self.client.accessToken = None
        self.client.device.refreshToken = None
        self.client.device.save()
        raise SystemExit("Logged out. Stored session cleared.")

    def _cmd_exit(self, args: List[str]) -> str:
        """Exit the TUI."""
        raise SystemExit("Goodbye!")


def create_command_registry(client: Client) -> CommandRegistry:
    """Factory function to create a CommandRegistry for a client."""
    return CommandRegistry(client)