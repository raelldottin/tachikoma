"""Regression tests for scripts/script_credentials.py credential loading.

Proves the script credential loader reads email/password ONLY from
environment variables or a password file, never from hardcoded values,
and fails closed when nothing is configured.
"""
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.script_credentials import (  # noqa: E402
    EMAIL_ENV,
    PASSWORD_ENV,
    PASSWORD_FILE_ENV,
    load_email_password,
    load_test_email,
    load_test_password,
)


class ScriptCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.env_clean = mock.patch.dict(os.environ, {}, clear=True)
        self.env_clean.start()
        self.addCleanup(self.env_clean.stop)

    def test_email_from_env(self):
        os.environ[EMAIL_ENV] = "user@example.com"
        self.assertEqual(load_test_email(), "user@example.com")

    def test_password_from_env(self):
        os.environ[PASSWORD_ENV] = "secret123"
        self.assertEqual(load_test_password(), "secret123")

    def test_password_from_file(self):
        with TemporaryDirectory() as tmp:
            pw_file = Path(tmp) / "pw.txt"
            pw_file.write_text("file-secret-456\n")
            os.environ[PASSWORD_FILE_ENV] = str(pw_file)
            self.assertEqual(load_test_password(), "file-secret-456")

    def test_env_password_preferred_over_file(self):
        with TemporaryDirectory() as tmp:
            pw_file = Path(tmp) / "pw.txt"
            pw_file.write_text("file-secret\n")
            os.environ[PASSWORD_FILE_ENV] = str(pw_file)
            os.environ[PASSWORD_ENV] = "env-secret"
            self.assertEqual(load_test_password(), "env-secret")

    def test_pair_loader(self):
        os.environ[EMAIL_ENV] = "user@example.com"
        os.environ[PASSWORD_ENV] = "secret123"
        self.assertEqual(load_email_password(), ("user@example.com", "secret123"))

    def test_missing_email_fails_closed(self):
        with self.assertRaises(SystemExit) as ctx:
            load_test_email()
        self.assertEqual(ctx.exception.code, 1)

    def test_missing_password_fails_closed(self):
        with self.assertRaises(SystemExit) as ctx:
            load_test_password()
        self.assertEqual(ctx.exception.code, 1)

    def test_missing_password_file_fails_closed(self):
        os.environ[PASSWORD_FILE_ENV] = "/nonexistent/path/pw.txt"
        with self.assertRaises(SystemExit) as ctx:
            load_test_password()
        self.assertEqual(ctx.exception.code, 1)

    def test_empty_password_file_fails_closed(self):
        with TemporaryDirectory() as tmp:
            pw_file = Path(tmp) / "pw.txt"
            pw_file.write_text("   \n")
            os.environ[PASSWORD_FILE_ENV] = str(pw_file)
            with self.assertRaises(SystemExit) as ctx:
                load_test_password()
            self.assertEqual(ctx.exception.code, 1)

    def test_no_hardcoded_credentials_in_scripts(self):
        """No live credential literal survives in tracked scripts/ files."""
        forbidden = ("cymfe0", "Zydvux", "jecqew", "jabpa1", "sugwuj")
        scripts_dir = REPO_ROOT / "scripts"
        for py_file in scripts_dir.glob("*.py"):
            text = py_file.read_text()
            for token in forbidden:
                self.assertNotIn(token, text, f"{py_file.name} contains {token}")
        sh = scripts_dir / "init_github_secrets.sh"
        sh_text = sh.read_text()
        for token in forbidden + ("syncpool.com",):
            self.assertNotIn(token, sh_text, f"{sh.name} contains {token}")


if __name__ == "__main__":
    unittest.main()
