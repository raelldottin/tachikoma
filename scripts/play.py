#!/usr/bin/env python3
"""Tachikoma Interactive TUI Entry Point.

Usage:
    python -m sdk.tui
    python scripts/play.py
"""

from __future__ import annotations

import sys
import os
import argparse

# Add sdk to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sdk.device import Device
from sdk.tui import run_tui


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tachikoma Interactive TUI for Pixel Starships"
    )
    parser.add_argument(
        "--auth-file",
        dest="auth_file",
        help="Path to file containing authentication string",
    )
    parser.add_argument(
        "-d",
        "--device-name",
        default="iOS",
        help="Device name for device type resolution (iOS, macOS, Android)",
    )
    args = parser.parse_args()

    auth_string = None
    if args.auth_file:
        with open(args.auth_file, "r") as f:
            auth_string = f.read().strip()

    if auth_string:
        device = Device(
            name=args.device_name,
            authentication_string=auth_string,
        )
    else:
        device = Device(name=args.device_name)

    return run_tui(device)


if __name__ == "__main__":
    sys.exit(main())