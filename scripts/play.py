#!/usr/bin/env python3
"""Tachikoma Interactive TUI Entry Point.

Usage:
    python -m sdk.tui
    python scripts/play.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sdk.tui import main


if __name__ == "__main__":
    sys.exit(main())