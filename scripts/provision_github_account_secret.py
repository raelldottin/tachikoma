#!/usr/bin/env python3
"""Provision a GitHub Actions secret with a PSS auth string.

This script reads the CURRENTLY logged-in Pixel Starships account from the
game's local session files, constructs the six-field auth string, validates
its structure, and pipes it directly into `gh secret set`.

DESIGN PRINCIPLES (credential-safe):
  - Never prints the credential value to stdout, stderr, or logs.
  - Only prints validation status (field names + lengths, not values).
  - When piped into `gh secret set`, writes the auth string to stdout
    (which goes directly to `gh` — not to the terminal).
  - Does not write credentials to any file.
  - Does not include credentials in error messages or tracebacks.

USAGE:
  # Safe: pipe directly into gh secret set
  python scripts/provision_github_account_secret.py \\
      --secret-name auth_string_first \\
      --repo raelldottin/tachikoma |
      gh secret set auth_string_first --repo raelldottin/tachikoma

  # Dry-run: validate only, don't output the auth string
  python scripts/provision_github_account_secret.py --validate-only

  # Or use --pipe to explicitly print the auth string for piping
  python scripts/provision_github_account_secret.py --pipe | gh secret set ...

SOURCES (in priority order):
  1. Game's local UserLogin.txt (most reliable — written by the game on login)
  2. Game's preferences plist (has deviceKey, refreshToken, userId)
  3. Mitmproxy capture (fallback — requires proxy capture to be running)

AUTH STRING FORMAT:
  name|deviceKey|refreshToken|language|accessToken|userId
"""

import argparse
import os
import subprocess
import sys
import xmltodict
from pathlib import Path

# Paths to local game session data
GAME_CONTAINER = Path.home() / "Library/Containers/com.savysoda.pixelStarships"
USER_LOGIN_TXT = (
    GAME_CONTAINER
    / "Data/Library/Application Support/com.savysoda.pixelStarships/Data/Prod/UserLogin.txt"
)
GAME_PLIST = (
    GAME_CONTAINER
    / "Data/Library/Preferences/com.savysoda.pixelStarships.plist"
)

VALID_LANGUAGES = {"en", "fr", "de", "es", "pt", "ru", "ja", "ko", "zh", "zh-TW", "it", "tr"}


def read_user_login_txt():
    """Extract auth fields from the game's local UserLogin.txt file."""
    if not USER_LOGIN_TXT.exists():
        return None

    with open(USER_LOGIN_TXT, "r") as f:
        content = f.read()

    if not content.strip():
        return None

    try:
        d = xmltodict.parse(content)
    except Exception:
        return None

    try:
        user_login = d["UserService"]["UserLogin"]
        access_token = user_login.get("@accessToken", "")
        user_id = user_login.get("@UserId", "")
        user = user_login.get("User", {})
        name = user.get("@Name", "")
        language = user.get("@LanguageKey", "en")
    except (KeyError, TypeError):
        return None

    if not access_token or not user_id:
        return None

    return {
        "name": name,
        "accessToken": access_token,
        "userId": user_id,
        "language": language or "en",
    }


def read_game_plist():
    """Extract deviceKey and refreshToken from the game's preferences plist."""
    if not GAME_PLIST.exists():
        return None

    result = {}
    try:
        out = subprocess.check_output(
            ["plutil", "-p", str(GAME_PLIST)], stderr=subprocess.DEVNULL
        ).decode("utf-8")
    except subprocess.CalledProcessError:
        return None

    for line in out.splitlines():
        line = line.strip()
        if line.startswith('"persistedDeviceKey"'):
            # "persistedDeviceKey" => "UUID-VALUE"
            val = line.split("=>", 1)[1].strip().strip('"')
            if val:
                result["deviceKey"] = val
        elif line.startswith('"refreshToken"'):
            val = line.split("=>", 1)[1].strip().strip('"')
            if val:
                result["refreshToken"] = val

    return result if result else None


def read_mitmproxy_capture(capture_path):
    """Fallback: extract auth fields from a mitmproxy JSONL capture.

    Looks for DeviceLogin17 responses that contain accessToken and UserId.
    """
    capture_path = Path(capture_path)
    if not capture_path.exists():
        return None

    import json

    with open(capture_path, "r") as f:
        lines = f.readlines()

    # Find the LAST DeviceLogin17 response with accessToken
    for line in reversed(lines):
        try:
            data = json.loads(line.strip())
        except json.JSONDecodeError:
            continue

        if data.get("type") != "response":
            continue

        url = data.get("url", "")
        if "DeviceLogin17" not in url:
            continue

        body = data.get("body", "")
        if not body or "accessToken" not in str(body):
            continue

        # Parse the response — it's XML-like
        body_str = str(body)
        access_token = ""
        user_id = ""
        try:
            # Response may be JSON or XML
            if body_str.startswith("{"):
                body_data = json.loads(body_str)
                access_token = body_data.get("AccessToken", "")
                user_id = str(body_data.get("UserId", ""))
            else:
                d = xmltodict.parse(body_str)
                login = d.get("UserService", {}).get("UserLogin", {})
                access_token = login.get("@accessToken", "")
                user_id = login.get("@UserId", "")
        except Exception:
            # Try regex extraction as last resort
            import re
            at_match = re.search(r'accessToken="([^"]+)"', body_str)
            uid_match = re.search(r'UserId="([^"]+)"', body_str)
            if at_match:
                access_token = at_match.group(1)
            if uid_match:
                user_id = uid_match.group(1)

        if access_token and user_id:
            # Also try to get deviceKey from the matching request
            device_key = ""
            for req_line in reversed(lines):
                try:
                    req_data = json.loads(req_line.strip())
                except json.JSONDecodeError:
                    continue
                if req_data.get("type") != "request":
                    continue
                if "DeviceLogin17" not in req_data.get("url", ""):
                    continue
                req_body = req_data.get("body", "")
                if req_body:
                    try:
                        req_json = json.loads(str(req_body))
                        device_key = req_json.get("DeviceKey", "")
                    except (json.JSONDecodeError, TypeError):
                        pass
                break

            return {
                "name": "",  # Not available from DeviceLogin response
                "deviceKey": device_key,
                "accessToken": access_token,
                "userId": user_id,
                "language": "en",
            }

    return None


def build_auth_string(fields):
    """Construct the six-field auth string: name|deviceKey|refreshToken|lang|accessToken|userId"""
    name = fields.get("name", "")
    device_key = fields.get("deviceKey", "")
    refresh_token = fields.get("refreshToken", "")
    language = fields.get("language", "en")
    access_token = fields.get("accessToken", "")
    user_id = str(fields.get("userId", ""))

    return f"{name}|{device_key}|{refresh_token}|{language}|{access_token}|{user_id}"


def validate_auth_string(auth_string):
    """Validate the auth string structure without printing values.

    Returns (is_valid, report_dict).
    """
    fields = auth_string.split("|")
    report = {
        "fields": len(fields),
        "device_name_present": "yes" if len(fields) > 0 and fields[0] else "no",
        "device_key_length": len(fields[1]) if len(fields) > 1 and fields[1] else 0,
        "refresh_token_present": "yes" if len(fields) > 2 and fields[2] else "no",
        "language": fields[3] if len(fields) > 3 else "",
        "access_token_present": "yes" if len(fields) > 4 and fields[4] else "no",
        "access_token_length": len(fields[4]) if len(fields) > 4 and fields[4] else 0,
        "user_id_present": "yes" if len(fields) > 5 and fields[5] else "no",
        "user_id_numeric": "yes" if len(fields) > 5 and fields[5].isdigit() else "no",
    }

    is_valid = (
        len(fields) == 6
        and bool(fields[0])  # name
        and bool(fields[1])  # deviceKey
        # refreshToken may be empty for some accounts — not a hard requirement
        and bool(fields[3])  # language
        and len(fields[4]) >= 8  # accessToken (length varies)
        and bool(fields[5])  # userId
        and fields[5].isdigit()
    )

    report["validation"] = "passed" if is_valid else "failed"
    return is_valid, report


def print_validation_report(report):
    """Print validation status — NO credential values."""
    for key, value in report.items():
        print(f"{key}: {value}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Provision a GitHub Actions secret with a PSS auth string. "
        "NEVER prints credential values. Safe to pipe into gh secret set."
    )
    parser.add_argument(
        "--secret-name",
        help="GitHub secret name (e.g., auth_string_first). Not used for validation.",
    )
    parser.add_argument(
        "--repo",
        default="raelldottin/tachikoma",
        help="GitHub repo for gh secret set (not used directly by this script).",
    )
    parser.add_argument(
        "--capture",
        default=os.path.expanduser("~/pss-mitm-capture.jsonl"),
        help="Path to mitmproxy capture file (fallback source).",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate only — do not output the auth string.",
    )
    parser.add_argument(
        "--pipe",
        action="store_true",
        help="Output the auth string to stdout for piping into gh secret set. "
        "Do NOT use without a pipe.",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "local", "capture"],
        default="auto",
        help="Source for credentials: auto (local first), local (game files), "
        "capture (mitmproxy).",
    )
    args = parser.parse_args()

    # --- Extract credentials from local sources ---
    fields = None
    source_used = ""

    if args.source in ("auto", "local"):
        login_data = read_user_login_txt()
        plist_data = read_game_plist()
        if login_data:
            fields = {}
            fields.update(login_data)
            if plist_data:
                if "deviceKey" not in fields or not fields.get("deviceKey"):
                    fields["deviceKey"] = plist_data.get("deviceKey", "")
                if "refreshToken" not in fields or not fields.get("refreshToken"):
                    fields["refreshToken"] = plist_data.get("refreshToken", "")
            source_used = "local (UserLogin.txt + plist)"
        elif args.source == "local":
            print("ERROR: Could not read credentials from local game files.", file=sys.stderr)
            print(f"  UserLogin.txt: {USER_LOGIN_TXT}", file=sys.stderr)
            print(f"  Plist: {GAME_PLIST}", file=sys.stderr)

    if not fields and args.source in ("auto", "capture"):
        capture_data = read_mitmproxy_capture(args.capture)
        if capture_data:
            fields = capture_data
            source_used = "mitmproxy capture"
        elif args.source == "capture":
            print(f"ERROR: Could not read credentials from capture: {args.capture}", file=sys.stderr)

    if not fields:
        print("ERROR: No credentials found from any source.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Ensure the game is logged in and has been opened recently,", file=sys.stderr)
        print("or provide a valid mitmproxy capture with --capture.", file=sys.stderr)
        sys.exit(1)

    # --- Build and validate ---
    auth_string = build_auth_string(fields)
    is_valid, report = validate_auth_string(auth_string)

    print(f"source: {source_used}", file=sys.stderr)
    print_validation_report(report)

    if not is_valid:
        print("", file=sys.stderr)
        print("VALIDATION FAILED — auth string structure is invalid.", file=sys.stderr)
        print("Do NOT proceed with setting the GitHub secret.", file=sys.stderr)
        sys.exit(1)

    # --- Output ---
    if args.validate_only:
        print("", file=sys.stderr)
        print("Validation passed. Use --pipe to output for gh secret set.", file=sys.stderr)
        sys.exit(0)

    if args.pipe:
        # Write ONLY the auth string to stdout — for piping into gh secret set.
        # stdout goes to the pipe, not to the terminal.
        sys.stdout.write(auth_string)
        sys.exit(0)

    # Default: also pipe (safe default — stdout should go to gh secret set)
    # But warn if it looks like a terminal
    if sys.stdout.isatty():
        print(
            "WARNING: stdout is a terminal. Use --validate-only to check structure,",
            file=sys.stderr,
        )
        print(
            "or pipe this script directly into: gh secret set <name> --repo <repo>",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        print("Aborting to prevent credential display.", file=sys.stderr)
        sys.exit(1)

    sys.stdout.write(auth_string)
    sys.exit(0)


if __name__ == "__main__":
    main()
