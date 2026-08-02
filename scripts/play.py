#!/usr/bin/env python3
"""Tachikoma Interactive TUI Entry Point.

Usage:
    python -m tachikoma.tui
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


def main():
    parser = argparse.ArgumentParser(
        description="Tachikoma Interactive TUI for Pixel Starships"
    )
    parser.add_argument(
        "-a",
        "--auth",
        help="Authentication string (name|deviceKey|refreshToken|languageKey|accessToken|userId)",
    )
    parser.add_argument(
        "-d",
        "--device-name",
        default="iOS",
        help="Device name for device type resolution (iOS, macOS, Android)",
    )
    args = parser.parse_args()

    if args.auth:
        device = Device(
            name=args.device_name,
            authentication_string=args.auth,
        )
    else:
        device = Device(name=args.device_name)

    return run_tui(device)


if __name__ == "__main__":
    sys.exit(main())