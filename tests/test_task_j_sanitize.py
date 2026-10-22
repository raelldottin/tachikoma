"""Task J safety/sanitization tests (lane t_06be4e4c).

Covers the sanitizer surface added for Task J evidence capture:
- sanitize capture records/lines from the mitmproxy addon shape;
- fail-closed behavior on malformed input;
- determinism of fixture output;
- no secret survives in sanitized fixtures (deep key+value scan);
- no network I/O path exists in the sanitizer;
- no premium-currency path was added;
- GiftItem execution remains unreachable (no call site, no method);
- committed fixtures carry no secrets (regression scan);
- inventory apply still refuses unconditionally (bypass battery).

All fixtures here are SYNTHETIC and SANITIZED: no real token,
credential or secret value appears anywhere in this file.
"""

import ast
import inspect
import json
import os
import socket
import unittest
from pathlib import Path
from unittest import mock

import sdk.inventory.sanitize as sanitize_mod
from sdk.inventory.sanitize import (
    REDACTED,
    SANITIZE_SCHEMA_VERSION,
    SanitizationError,
    sanitize_capture_line,
    sanitize_capture_record,
    sanitize_body,
    sanitize_headers,
    sanitize_query_string,
    scan_for_secrets,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "tests" / "fixtures"

# Synthetic secret stand-ins (never real values).
TOKEN = "synthetic-token-abcdef0123456789"
PASSWORD = "synthetic-password-not-real"
DEVICE_KEY = "synthetic-device-key-000"

# ---------------------------------------------------------------------------
# Synthetic capture records in the mitmproxy addon shape
# ---------------------------------------------------------------------------


def _capture_request(url, body="", headers=None, ts="2026-10-02T00:00:00"):
    return {
        "ts": ts,
        "type": "request",
        "url": url,
        "method": "GET",
        "headers": headers if headers is not None else {"Host": "api.example.test"},
        "body": body,
    }


def _capture_response(url, body="", headers=None, status_code=200, ts="2026-10-02T00:00:01"):
    return {
        "ts": ts,
        "type": "response",
        "url": url,
        "status_code": status_code,
        "headers": headers if headers is not None else {"Host": "api.example.test"},
        "body": body,
    }


def _list_items_url(token=TOKEN):
    return (
        "/ItemService/ListItemsOfAShip"
        f"?accessToken={token}&userId=900001&shipId=55"
    )


class TestSanitizeQuery(unittest.TestCase):
    def test_secret_query_value_redacted(self):
        sanitized, names, secrets = sanitize_query_string(
            f"accessToken={TOKEN}&userId=900001&shipId=55"
        )
        self.assertNotIn(TOKEN, sanitized)
        self.assertIn(f"accessToken={REDACTED}", sanitized)
        self.assertEqual(names, ("accessToken", "userId", "shipId"))
        self.assertEqual(secrets, ("accessToken",))

    def test_case_and_underscore_variants_covered(self):
        for name in (
            "accessToken", "access_token", "refreshToken", "password",
            "deviceKey", "clientKey", "apiKey", "token", "sessionId",
        ):
            _, _, secrets = sanitize_query_string(f"{name}=value123")
            self.assertIn(name, secrets, name)

    def test_non_secret_values_preserved(self):
        sanitized, _, _ = sanitize_query_string("userId=900001&shipId=55")
        self.assertIn("userId=900001", sanitized)
        self.assertIn("shipId=55", sanitized)

    def test_empty_query(self):
        sanitized, names, secrets = sanitize_query_string("")
        self.assertEqual(sanitized, "")
        self.assertEqual(names, ())
        self.assertEqual(secrets, ())


class TestSanitizeHeaders(unittest.TestCase):
    def test_authorization_and_cookie_redacted(self):
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "Cookie": f"session={TOKEN}",
            "Host": "api.example.test",
        }
        out = sanitize_headers(headers)
        self.assertEqual(out["Authorization"], REDACTED)
        self.assertEqual(out["Cookie"], REDACTED)
        self.assertEqual(out["Host"], "api.example.test")

    def test_header_names_preserved(self):
        out = sanitize_headers({"X-Custom": "keepme"})
        self.assertEqual(out["X-Custom"], "keepme")


class TestSanitizeBody(unittest.TestCase):
    def test_json_secret_keys_redacted(self):
        body = json.dumps({"accessToken": TOKEN, "userId": 900001})
        out = sanitize_body(body)
        parsed = json.loads(out)
        self.assertEqual(parsed["accessToken"], REDACTED)
        self.assertEqual(parsed["userId"], 900001)

    def test_nested_json_secret_keys_redacted(self):
        body = json.dumps({"auth": {"password": PASSWORD, "ok": True}})
        out = sanitize_body(body)
        parsed = json.loads(out)
        self.assertEqual(parsed["auth"]["password"], REDACTED)
        self.assertTrue(parsed["auth"]["ok"])

    def test_form_text_body_redacted(self):
        body = f"email=a%40b.test&password={PASSWORD}&checksum=abc"
        out = sanitize_body(body)
        self.assertNotIn(PASSWORD, out)
        self.assertIn("email=a%40b.test", out)

    def test_xml_attribute_body_redacted(self):
        body = f'<Item accessToken="{TOKEN}" itemId="7"/>'
        out = sanitize_body(body)
        self.assertNotIn(TOKEN, out)
        self.assertIn(f'accessToken="{REDACTED}"', out)

    def test_structural_payload_preserved(self):
        xml = (
            "<ItemService><ListItemsOfAShip><Items>"
            '<Item ItemId="101" ItemDesignId="4210" Quantity="3"/>'
            "</Items></ListItemsOfAShip></ItemService>"
        )
        out = sanitize_body(xml)
        self.assertIn('ItemId="101"', out)
        self.assertIn('ItemDesignId="4210"', out)
        self.assertIn('Quantity="3"', out)


class TestSanitizeCaptureRecord(unittest.TestCase):
    def test_request_record_shape(self):
        out = sanitize_capture_record(_capture_request(_list_items_url()))
        self.assertEqual(out["schema_version"], SANITIZE_SCHEMA_VERSION)
        self.assertEqual(out["type"], "request")
        self.assertEqual(out["endpoint"], "/ItemService/ListItemsOfAShip")
        self.assertEqual(
            out["param_names"], ["accessToken", "userId", "shipId"]
        )
        self.assertEqual(out["secret_param_names"], ["accessToken"])
        self.assertNotIn(TOKEN, out["url"])
        self.assertIn(f"accessToken={REDACTED}", out["url"])
        self.assertIn("userId=900001", out["url"])
        self.assertEqual(out["method"], "GET")

    def test_response_record_shape(self):
        out = sanitize_capture_record(
            _capture_response(_list_items_url(), status_code=200)
        )
        self.assertEqual(out["type"], "response")
        self.assertEqual(out["status_code"], 200)
        self.assertNotIn("method", out)

    def test_known_secret_value_scrubbed_from_everywhere(self):
        record = _capture_request(
            "/ItemService/GetItem?itemId=7", body=f"note={TOKEN}"
        )
        out = sanitize_capture_record(record, known_secret_values=[TOKEN])
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_no_secret_survives_deep_scan(self):
        record = _capture_request(
            _list_items_url(),
            headers={"Authorization": f"Bearer {TOKEN}"},
            body=json.dumps({"accessToken": TOKEN, "password": PASSWORD}),
        )
        out = sanitize_capture_record(record, known_secret_values=[TOKEN, PASSWORD])
        self.assertEqual(scan_for_secrets(out, [TOKEN, PASSWORD]), [])
        self.assertNotIn(TOKEN, json.dumps(out))
        self.assertNotIn(PASSWORD, json.dumps(out))

    def test_malformed_type_fails_closed(self):
        with self.assertRaises(SanitizationError):
            sanitize_capture_record({"type": "bogus", "ts": "x", "url": "y"})

    def test_missing_ts_fails_closed(self):
        with self.assertRaises(SanitizationError):
            sanitize_capture_record({"type": "request", "url": "/x", "method": "GET"})

    def test_missing_url_fails_closed(self):
        with self.assertRaises(SanitizationError):
            sanitize_capture_record({"type": "request", "ts": "x", "method": "GET"})

    def test_missing_method_fails_closed(self):
        with self.assertRaises(SanitizationError):
            sanitize_capture_record(
                {"type": "request", "ts": "x", "url": "/x"}
            )

    def test_missing_status_fails_closed(self):
        with self.assertRaises(SanitizationError):
            sanitize_capture_record(
                {"type": "response", "ts": "x", "url": "/x"}
            )

    def test_non_mapping_fails_closed(self):
        with self.assertRaises(SanitizationError):
            # deliberately a bare string, not a mapping
            sanitize_capture_record("not-a-record")  # type: ignore[arg-type]

    def test_non_string_body_fails_closed(self):
        record = _capture_request("/x")
        record["body"] = 123
        with self.assertRaises(SanitizationError):
            sanitize_capture_record(record)

    def test_non_mapping_headers_fails_closed(self):
        record = _capture_request("/x")
        record["headers"] = ["nope"]
        with self.assertRaises(SanitizationError):
            sanitize_capture_record(record)


class TestSanitizeCaptureLine(unittest.TestCase):
    def test_line_round_trip(self):
        line = json.dumps(_capture_request(_list_items_url()))
        out = sanitize_capture_line(line)
        parsed = json.loads(out)
        self.assertEqual(parsed["schema_version"], SANITIZE_SCHEMA_VERSION)
        self.assertNotIn(TOKEN, out)

    def test_invalid_json_line_fails_closed(self):
        with self.assertRaises(SanitizationError):
            sanitize_capture_line("{not json")

    def test_deterministic_output(self):
        line = json.dumps(_capture_request(_list_items_url()))
        self.assertEqual(sanitize_capture_line(line), sanitize_capture_line(line))


class TestFixtureSecretScan(unittest.TestCase):
    def test_scan_detects_leaked_secret_key(self):
        violations = scan_for_secrets({"accessToken": TOKEN})
        self.assertTrue(violations)

    def test_scan_detects_known_value_in_string(self):
        violations = scan_for_secrets({"note": f"x{TOKEN}y"}, [TOKEN])
        self.assertTrue(violations)

    def test_scan_accepts_redacted_fixture(self):
        fixture = sanitize_capture_record(
            _capture_request(_list_items_url()), known_secret_values=[TOKEN]
        )
        self.assertEqual(scan_for_secrets(fixture, [TOKEN]), [])

    def test_committed_fixtures_carry_no_secrets(self):
        # Regression: every committed fixture file must stay secret-free.
        self.assertTrue(FIXTURES_DIR.exists())
        files = sorted(FIXTURES_DIR.glob("*.json"))
        files += sorted(
            path for path in FIXTURES_DIR.rglob("*.json")
            if path not in files
        )
        self.assertTrue(files)
        for path in files:
            with path.open(encoding="utf-8") as fh:
                payload = json.load(fh)
            violations = scan_for_secrets(payload)
            self.assertEqual(
                violations, [], f"{path.name}: {violations}"
            )


class TestSanitizerSafetyProperties(unittest.TestCase):
    """No network, no mutation path, no premium-currency path in the
    Task J sanitizer surface (and the package as a whole keeps its
    existing pins)."""

    def _source(self):
        return inspect.getsource(sanitize_mod)

    def test_module_imports_are_pure_only(self):
        tree = ast.parse(self._source())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        forbidden = {"requests", "socket", "urllib", "http.client", "os", "sys"}
        for name in imported:
            self.assertNotIn(name, forbidden, f"import {name!r} is forbidden")

    def test_no_network_call_sites(self):
        src = self._source()
        for pattern in ("requests.", "urlopen", "socket.", "urllib."):
            self.assertNotIn(pattern, src, pattern)

    def test_no_environment_or_config_bypass_reads(self):
        src = self._source()
        for token in ("os.environ", "getenv", "read_env", "config"):
            self.assertNotIn(token, src, token)

    def test_no_file_io(self):
        src = self._source()
        self.assertNotIn("open(", src)
        self.assertNotIn("Path(", src)

    def test_no_endpoint_execution_vocabulary(self):
        # The sanitizer redacts; it never reconstructs or invokes any
        # endpoint. No forbidden call pattern may appear at all.
        src = self._source()
        for name in (
            "GiftItem", "SellItem3", "Purchase4", "TransferItem",
            "ConsumeItem2", "DeleteItem3",
        ):
            self.assertNotIn(f"{name}(", src, name)

    def test_no_premium_currency_path_added(self):
        # The sanitizer surface has no Starbux/premium spend capability
        # and the inventory config cap stays at zero.
        from sdk.inventory import DEFAULT_INVENTORY_CONFIG

        self.assertEqual(DEFAULT_INVENTORY_CONFIG["starbux_cap_per_run"], 0)
        src = self._source()
        self.assertNotIn("starbux", src.lower())
        self.assertNotIn("purchase", src.lower())

    def test_full_surface_under_socket_block(self):
        # Runtime proof: the whole sanitize surface completes with the
        # socket namespace poisoned — any network attempt fails loud.
        def blocked(*args, **kwargs):
            raise AssertionError("network attempt from sanitizer")

        line = json.dumps(_capture_request(_list_items_url()))
        with mock.patch.object(socket, "socket", blocked), mock.patch.object(
            socket, "create_connection", blocked
        ):
            out = sanitize_capture_line(line, [TOKEN])
        self.assertNotIn(TOKEN, out)

    def test_no_mutation_of_input_record(self):
        record = _capture_request(_list_items_url())
        snapshot = json.dumps(record, sort_keys=True)
        sanitize_capture_record(record, [TOKEN])
        self.assertEqual(json.dumps(record, sort_keys=True), snapshot)


class TestGiftItemUnreachable(unittest.TestCase):
    """GiftItem execution stays unreachable in production code."""

    def test_no_giftitem_client_method(self):
        import sdk.client as client_mod

        src = inspect.getsource(client_mod)
        self.assertNotIn("def giftItem", src)
        self.assertNotIn("def gift_item", src)

    def test_sanitizer_module_has_no_giftitem_protocol_payload(self):
        tree = ast.parse(inspect.getsource(sanitize_mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if "GiftItem" in value:
                    self.assertNotIn("/ItemService/", value)
                    self.assertNotIn("http", value.lower())

    def test_no_new_transfer_executor_symbols(self):
        import sdk.inventory as inv

        for name in ("transfer_execute", "execute_transfer", "apply_transfer"):
            self.assertFalse(hasattr(inv, name), name)


class TestInventoryApplyStillRefuses(unittest.TestCase):
    """`inventory apply` remains an unconditional refusal; nothing in
    the Task J sanitizer lane can bypass it (Task I safety inheritance,
    re-proven on the current tree)."""

    def _registry(self):
        from unittest.mock import MagicMock

        from sdk.commands import create_command_registry

        client = MagicMock()
        client.settings = {
            "inventory": {
                "enabled": True,
                "snapshots_dir": "snapshots",
                "starbux_cap_per_run": 999,
            }
        }
        return create_command_registry(client)

    def _apply_output(self, extra=None):
        registry = self._registry()
        cmd = registry.get("inventory apply")
        self.assertIsNotNone(cmd)
        assert cmd is not None
        return cmd["handler"](list(extra or []))

    def test_apply_refuses_with_no_flags(self):
        self.assertIn("REFUSED", self._apply_output())

    def test_apply_refuses_with_force_and_yes(self):
        self.assertIn("REFUSED", self._apply_output(["--force", "--yes"]))

    def test_apply_refuses_with_env_and_config_armed(self):
        env = {
            "TACHIKOMA_INVENTORY_APPLY": "1",
            "TACHIKOMA_ALLOW_MUTATION": "1",
            "ENABLE_MUTATION": "1",
            "INVENTORY_ENABLED": "1",
        }
        with mock.patch.dict(os.environ, env):
            self.assertIn("REFUSED", self._apply_output(["--force", "--yes"]))

    def test_apply_is_single_unconditional_return(self):
        import sdk.commands as commands_mod

        tree = ast.parse(inspect.getsource(commands_mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_cmd_inventory_apply":
                returns = [
                    n for n in ast.walk(node) if isinstance(n, ast.Return)
                ]
                self.assertEqual(len(returns), 1)
                self.assertEqual(
                    len([n for n in ast.walk(node) if isinstance(n, ast.If)]), 0
                )


if __name__ == "__main__":
    unittest.main()
