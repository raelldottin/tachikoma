from __future__ import annotations

"""Command registry mapping typed commands to Client methods."""

import datetime

from typing import Callable, Dict, List, Mapping, Optional
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
        self.register(
            "inventory",
            self._cmd_inventory,
            "Read-only inventory view (summary, or 'plan' for classification, "
            "demand/reservation ledger and dry-run reduction candidates; "
            "'accounts'/'deficits' for read-only multi-account snapshot views; "
            "'transfers' for read-only cross-account pairing proposals)",
            args=["subcommand"],
        )
        self.register(
            "inventory apply",
            self._cmd_inventory_apply,
            "REFUSED: inventory mutations are disabled until the Phase 8/9 "
            "executor gate is satisfied in a separately authorized slice",
            args=[],
            verified=False,
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
        """Register a command. `handler` receives args = all words after `name`."""
        self._commands[name] = {
            "handler": handler,
            "description": description,
            "args": args or [],
            "verified": verified,
        }

    def get(self, name: str) -> Optional[Dict]:
        """Get command info by name. `name` may be multi-word."""
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
        """Show available commands or detailed help for a command.

        Multi-word verbs resolve exactly like the TUI loop: longest
        registered prefix wins, so `help inventory apply` shows the
        unverified refusal stub — never the verified `inventory` verb.
        """
        if args:
            # Longest registered prefix first (mirrors TUI._command_loop).
            cmd = None
            name = ""
            for width in range(min(len(args), 1), -1, -1):
                name = " ".join(args[: width + 1]).lower()
                cmd = self._commands.get(name)
                if cmd:
                    break
            if not cmd:
                return f"Unknown command: {args[0].lower()}"
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
        lines.append("Device identity: Configured")
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

    def _cmd_inventory(self, args: List[str]) -> str:
        """Read-only inventory view. No mutation path exists in this slice.

        No [inventory] config section is read: absent/empty config behaves
        identically to defaults (safety AD-8).

        Task H: the plan renderer lives in sdk/inventory/plan_render.py
        (pure); this verb also dispatches the read-only `accounts` and
        `deficits` subcommands over persisted account snapshots.

        Task I: `transfers` dispatches the read-only cross-account pairing
        view (pure render_transfers_view in sdk/inventory/transfer.py).
        Every proposal there is a non-actionable HOLD (transferability
        unproven: GiftItem uncaptured). No subcommand writes, executes,
        or moves anything between accounts.
        """
        from sdk.inventory import (  # deferred: keeps client.py free of sdk.inventory
            InventoryCatalog,
        )

        subcommand = args[0].lower() if args else ""
        if subcommand in ("accounts", "deficits", "transfers"):
            return self._render_portfolio_view(subcommand)
        if subcommand and subcommand != "plan":
            return (
                f"Unknown inventory subcommand: {subcommand}. "
                "Available: inventory, inventory plan, inventory accounts, "
                "inventory deficits, inventory transfers"
            )

        catalog = InventoryCatalog.from_client(self.client)
        items = catalog.items()
        if not items:
            return (
                "Inventory data unavailable or ship inventory is empty. "
                "Nothing to report."
            )

        if subcommand == "plan":
            return self._render_inventory_plan(catalog, items)

        # Plain `inventory`: per-design stack/quantity digest (unchanged
        # slice-1 behavior; no classification columns here).
        known = {}
        unknown_qty = 0
        unknown_ids = set()
        zero_qty = 0
        for item in items:
            if item.quantity == 0:
                zero_qty += 1
            design = catalog.resolve(item)
            if design is None:
                unknown_qty += item.quantity
                unknown_ids.add(item.item_design_id)
                continue
            entry = known.setdefault(
                item.item_design_id,
                {"design": design, "stacks": 0, "quantity": 0},
            )
            entry["stacks"] += 1
            entry["quantity"] += item.quantity

        lines = ["=== Inventory (read-only) ==="]

        lines.append(f"Item stacks: {len(items)}")
        lines.append(f"Distinct designs: {len(known)}")
        lines.append(
            f"Zero-quantity stacks retained by server: {zero_qty} (never actionable)"
        )
        lines.append("")
        for design_id in sorted(known):
            entry = known[design_id]
            design = entry["design"]
            lines.append(
                f"  {design_id:>6} | {design.item_name[:40]:<40} "
                f"| stacks {entry['stacks']:>3} | qty {entry['quantity']:>5}"
            )
        if unknown_ids:
            lines.append("")
            lines.append(
                f"PROTECTED — unknown designs (absent from dynamic design feed): "
                f"{len(unknown_ids)} design ids, {unknown_qty} total quantity. "
                f"Fail-closed by policy; will not be proposed for any action."
            )

        lines.append("")
        lines.append(
            "Classification and demand/reservations are reported by "
            "'inventory plan', which also renders the Task G dry-run "
            "reduction section (informational only, actionable=false "
            "everywhere). Cross-account pairing proposals are reported "
            "by 'inventory transfers' (read-only Task I: every proposal "
            "is a non-actionable HOLD with proposed quantity 0). "
            "Marketplace valuation (gate=NEEDS_CAPTURE) remains "
            "unavailable. This view is strictly read-only."
        )
        return "\n".join(lines)

    def _render_inventory_plan(self, catalog, items) -> str:
        """Read-only Task D + Task F + Task G view (dry-run, nothing executable).

        Task H: the text assembly was mechanically extracted to the pure
        sdk/inventory/plan_render.py module (output-parity pinned by
        tests/test_inventory_plan_render.py). This wrapper supplies the
        only two caller-owned inputs: the ship payload (fetched only when
        absent, fetch failure = fail-closed None) and the explicit
        strategic-reserve configuration ({} when absent; no default is
        invented).
        """
        # Read-only ship payload (GetShipByUserId — the same transport
        # method the `ship` command uses), fetched only when absent.
        # Fetch failure leaves the reference sources unavailable (fail
        # closed, surfaced as conditions) — never a negative evaluation.
        ship_data = getattr(self.client, "shipByUserId", None)
        if not ship_data:
            try:
                if self.client.getShipByUserId():
                    ship_data = getattr(self.client, "shipByUserId", None)
            except Exception:
                ship_data = None
        from sdk.inventory import render_inventory_plan

        return render_inventory_plan(
            catalog,
            items,
            ship_data,
            strategic_reserve=self._inventory_strategic_reserve(),
        )

    def _inventory_strategic_reserve(self):
        """Explicit strategic-reserve configuration ONLY (fail closed).

        Reads the inventory.strategic_reserve mapping from the client
        settings section. Absent/empty/None yields {} — no approved
        default exists, and no default is ever invented here. The value
        is passed to the pure planner, which validates every entry;
        nothing here interprets, filters or authorizes anything.
        """
        settings = getattr(self.client, "settings", None)
        if not isinstance(settings, Mapping):
            return {}
        section = settings.get("inventory")
        if not isinstance(section, Mapping):
            return {}
        reserve = section.get("strategic_reserve")
        return dict(reserve) if isinstance(reserve, Mapping) else {}

    def _inventory_snapshot_dir(self) -> str:
        """Snapshot directory: inventory.snapshots_dir from settings ONLY.

        Absent config yields the conventional path 'snapshots'
        (documented in README.template) — a directory must be picked to
        read anything at all, and the setting exists so a caller can
        point elsewhere. Anything that is not a non-empty string fails
        closed to the conventional path, never to a widening rule.
        """
        settings = getattr(self.client, "settings", None)
        if not isinstance(settings, Mapping):
            return "snapshots"
        section = settings.get("inventory")
        if not isinstance(section, Mapping):
            return "snapshots"
        value = section.get("snapshots_dir")
        if not isinstance(value, str) or not value:
            return "snapshots"
        return value

    def _read_snapshot_texts(self):
        """Read every *.json snapshot text from the snapshots dir.

        Read-only disk access owned by this caller (never sdk/inventory,
        which stays pure). Returns a list of (file_name, text) pairs in
        ascending file-name order; [] when the directory does not exist
        (nothing captured yet is a legitimate empty state, not an error).
        An unreadable file is reported, not raised, so the CLI handler
        can print an explicit fail-closed message.
        """
        import os

        directory = self._inventory_snapshot_dir()
        if not os.path.isdir(directory):
            return []
        texts = []
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    texts.append((name, handle.read()))
            except OSError:
                return [(name, f"<unreadable: {name}>")]
        return texts

    def _render_portfolio_view(self, subcommand: str) -> str:
        """Render `inventory accounts` / `inventory deficits` /
        `inventory transfers` (read-only).

        Reads persisted snapshot JSON from the snapshots directory
        (caller-owned disk read), parses + validates each snapshot via
        the fail-closed domain loader, aggregates via the pure domain
        portfolio, and renders via the pure renderers (accounts and
        deficits in portfolio_render.py; the Task I cross-account
        pairing view in transfer.py). Any malformed, unreadable or
        contradictory input refuses the WHOLE view as explicit text —
        never a partial rendering, never a silent per-account drop.
        All three views are strictly observational: actionable=false
        everywhere, no destination selection, and no quantity that
        can move between accounts. The transfers view joins accounts
        only by ItemDesignId and every proposal is a non-actionable
        HOLD_UNVERIFIED_TRANSFERABILITY with proposed quantity 0.
        """
        from sdk.inventory import (
            render_accounts_view,
            render_deficits_view,
            render_transfers_view,
            snapshot_from_json,
        )

        renderers = {
            "accounts": render_accounts_view,
            "deficits": render_deficits_view,
            "transfers": render_transfers_view,
        }
        renderer = renderers[subcommand]
        prefix = f"inventory {subcommand}: REFUSED fail-closed."

        snapshots = []
        for name, text in self._read_snapshot_texts():
            if text.startswith("<unreadable:"):
                return (
                    f"{prefix} Snapshot file '{name}' could not be read "
                    "from disk. The whole view refuses (no partial "
                    "rendering). Fix or remove the file, then re-run."
                )
            try:
                snapshots.append(snapshot_from_json(text))
            except Exception as exc:
                return (
                    f"{prefix} Snapshot file '{name}' is malformed, "
                    f"corrupt or an unsupported version: {exc}. The whole "
                    "view refuses (no partial rendering, no silent "
                    "account drop). Fix or remove the file, then re-run."
                )
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        try:
            return renderer(snapshots, now=now)
        except Exception as exc:
            return (
                f"{prefix} The multi-account view rejected the input: "
                f"{exc}. The whole view refuses (no partial rendering)."
            )

    def _cmd_inventory_apply(self, args: List[str]) -> str:
        """ALWAYS-REFUSING safety stub (safety invariant S2).

        There is no mutation implementation behind this verb and it cannot be
        enabled by any configuration value, CLI flag, environment variable, or
        undocumented switch. The refusal disappears only by deleting this stub
        in the same commit that ships a capture-backed, reconciliation-tested
        executor under an explicitly authorized Phase 8/9 slice.
        """
        return (
            "inventory apply: REFUSED. Inventory mutations are disabled until "
            "Phase 8 (executor/reconciler with fakes-only validation) and "
            "Phase 9 (per-endpoint enablement with capture, checksum and "
            "semantic verification) are satisfied in a separately authorized "
            "slice. No configuration, flag, or environment variable enables "
            "this command."
        )

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