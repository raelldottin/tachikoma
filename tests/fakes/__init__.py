"""In-memory fakes of the Pixel Starships game server for offline tests.

Import the modules by their full name (``from tests.fakes.pss_server import FakePss``). This file stays free of
imports on purpose: unittest discovery, started at tests/, imports this package as the top-level ``fakes``, and any
import here would load a second copy of the modules under that name.
"""
