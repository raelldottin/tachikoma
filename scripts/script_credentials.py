"""Shared credential loading for one-off scripts in scripts/.

Credentials are never hardcoded in tracked files. The test account email
comes from PSS_TEST_EMAIL; the password comes from PSS_TEST_PASSWORD or,
preferably, from the file named by PSS_TEST_PASSWORD_FILE (matching the
--password-file pattern run.py uses).

Values are returned in memory only. Callers must never print or log them.
"""
import os
import sys
from pathlib import Path
from typing import NoReturn

EMAIL_ENV = "PSS_TEST_EMAIL"
PASSWORD_ENV = "PSS_TEST_PASSWORD"
PASSWORD_FILE_ENV = "PSS_TEST_PASSWORD_FILE"


def _fail(missing_env) -> NoReturn:
    print(
        f"ERROR: set {missing_env} "
        f"(and optionally {PASSWORD_FILE_ENV} instead of {PASSWORD_ENV})",
        file=sys.stderr,
    )
    sys.exit(1)


def load_test_email():
    """Return the test account email from PSS_TEST_EMAIL."""
    email = os.environ.get(EMAIL_ENV, "").strip()
    if not email:
        _fail(EMAIL_ENV)
    return email


def load_test_password():
    """Return the test account password from env or password file."""
    password = os.environ.get(PASSWORD_ENV, "").strip()
    if password:
        return password
    password_file = os.environ.get(PASSWORD_FILE_ENV, "").strip()
    if password_file:
        path = Path(password_file)
        if not path.is_file():
            print(f"ERROR: password file not found: {password_file}", file=sys.stderr)
            sys.exit(1)
        password = path.read_text().strip()
        if password:
            return password
    _fail(f"{PASSWORD_ENV} or {PASSWORD_FILE_ENV}")


def load_email_password():
    """Return (email, password) for the test account."""
    return load_test_email(), load_test_password()
