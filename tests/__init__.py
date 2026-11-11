"""Test package for tachikoma.

Several test modules construct ``Device()`` (which reads and rewrites ``./.device``, the file that holds a
device key and refresh token) or import ``run.py`` (which opens ``./tachikoma.log`` on import). Both paths are
relative to the working directory, so a test run started from the repository root can read and overwrite the
owner's real credential file.

Safe entry points, which run every module from a fresh temporary working directory:

* ``python scripts/ci.py test`` (optionally ``--names tests.<module>``)
* ``make test`` and ``make test-security``

Unsafe forms when run from the repository root:

* ``python -m unittest tests.<module>``: refused below while ``./.device`` exists.
* ``python -m unittest discover -s tests`` and running a test file as a script (``python tests/test_x.py``):
  covered only partly. Discovery with ``-s tests`` imports test modules as top-level modules, so this guard
  fires only for the modules that import from the ``tests`` package; a test file run as a script never
  imports this package at all. Modules the guard does not reach still touch ``./.device``.
"""

import os
from pathlib import Path

SAFE_COMMAND = "python scripts/ci.py test --names tests.<module>"


def _refuse_repository_root_run() -> None:
    root = Path(os.path.abspath(__file__)).parent.parent
    try:
        in_root = os.path.samefile(os.getcwd(), root)
    except OSError:
        return
    if in_root and os.path.lexists(root / ".device"):
        raise SystemExit(
            "Refusing to run tests from the repository root while ./.device exists: tests that construct "
            "Device() or import run.py would read and overwrite it. Run them from a temporary working "
            f"directory instead, with `{SAFE_COMMAND}` or `make test`."
        )


_refuse_repository_root_run()
