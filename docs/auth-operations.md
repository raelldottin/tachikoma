# Pixel Starships Authentication — Operational Documentation

## Overview
This document describes the manual provisioning and recovery procedures for Tachikoma's Pixel Starships authentication. Both endpoints are **live-verified** (2026-08-02).

## Endpoints

| Endpoint | Status | Checksum Formula |
|----------|--------|------------------|
| `DeviceLogin17` | Live-verified | `MD5(deviceKey + clientDateTime + "DeviceTypeMac" + "5343" + "Savvy!s0d@")` |
| `UserEmailPasswordAuthorize4` | Live-verified | `MD5(deviceKey + email + clientDateTime + accessToken + "5343" + "Savvy!s0d@")` |

## Authentication Flow

### Production (Default) — Refresh Token Path
```
DeviceLogin17(refreshToken) → accessToken → authenticated session
```
- Used by `run.py` by default
- No email/password required
- Requires valid `refreshToken` stored in `.device` file or auth string

### Provisioning/Recovery — Email/Password Path (Feature-Gated)
```
DeviceLogin17(no refreshToken) → accessToken
→ UserEmailPasswordAuthorize4(email, password, accessToken) → new refreshToken
→ DeviceLogin17(new refreshToken) → authenticated session
```
- Requires `allow_email_password_login = True` in settings
- Generates a **new** refreshToken (rotates previous)
- Use for initial provisioning or recovery after manual login invalidation

## Manual Provisioning Procedure

### Prerequisites
- iPhone with Pixel Starships app installed
- macOS machine with mitmproxy installed
- GitHub CLI (`gh`) authenticated for secret management

### Steps

1. **Set up mitmproxy on macOS**
   ```bash
   pip install mitmproxy
   mitmdump -p 8082 -s /path/to/mitm_capture.py
   ```

2. **Configure iPhone proxy**
   - Settings → Wi-Fi → (i) → Configure Proxy → Manual
   - Server: `<Mac's LAN IP>`
   - Port: `8082`

3. **Install mitmproxy CA on iPhone**
   - Safari → `http://mitm.it` → Apple → Install
   - Settings → General → About → Certificate Trust Settings → Enable mitmproxy

3. **Force fresh login**
   - Force close Pixel Starships on iPhone
   - Launch app → perform email/password login (not Guest/Apple/Google)
   - Capture appears in `/tmp/pss_capture_multi.json`

4. **Extract refreshToken from capture**
   ```bash
   cat /tmp/pss_capture_multi.json | python3 -c "
   import json, re
   with open('/tmp/pss_capture_multi.json') as f: data = json.load(f)
   for cap in data:
       resp = cap[1]
       if 'refreshToken' in resp.get('text', ''):
           m = re.search(r'refreshToken=\"([^\"]+)\"', resp['text'])
           if m: print(m.group(1))
   "
   ```

5. **Build auth string** (6 fields, pipe-delimited):
   ```
   name|deviceKey|refreshToken|languageKey|accessToken|userId
   ```
   - `name`: arbitrary identifier (e.g., `acc1`)
   - `deviceKey`: from capture (e.g., `CC3C7642-E6FE-4737-88C1-130395760B52`)
   - `refreshToken`: from step 4
   - `languageKey`: `en`
   - `accessToken`: optional, can be empty
   - `userId`: optional, can be empty

6. **Store as GitHub secret**
   ```bash
   gh secret set PSS_ACCOUNT_1_AUTH_STRING -b "<auth_string_from_step_5>"
   ```

## Recovery After Manual Login Invalidation

When you log in manually on the iPhone app, the server rotates the refreshToken. Automation will fail with authentication errors.

**Recovery procedure:**
1. Repeat steps 1-6 above (fresh iPhone login + capture)
2. Update the GitHub secret with the new auth string
3. Next automation run will succeed

## Feature Gate Configuration

```python
settings = {
    "checksum_key": "5343",
    "savy_checksum": "Savvy!s0d@",
    "allow_email_password_login": True,  # Enable for provisioning only
}
client = Client(device=device, settings=settings)
client.login(email="user@example.com", password="...")
```

**Default:** `allow_email_password_login = False` (production safe)

## Expected Reprovisioning Behavior

| Scenario | Behavior |
|----------|----------|
| Valid refreshToken | `login()` succeeds, skips email/password |
| Expired/invalid refreshToken | `login()` returns `False`, no fallback |
| No refreshToken + no credentials | Guest session (returns `True`) |
| No refreshToken + email/password + flag disabled | Returns `False`, no request sent |
| No refreshToken + email/password + flag enabled | Full three-stage flow, new refreshToken stored |

## CI/CD Automation

The provisioning workflow (`.github/workflows/provision-pss-secrets.yml`) runs daily to rotate tokens:

```yaml
# Triggers: daily at 06:00 UTC + manual dispatch
# Reads: PSS_ACCOUNT_X_AUTH_STRING secrets
# Performs: DeviceLogin17 → UserEmailPasswordAuthorize4 → new refreshToken
# Output: New auth string (must be manually copied back to secret)
```

## Safety Notes

- **Never commit** auth strings, refreshTokens, accessTokens, deviceKeys, or credentials
- All logs redact sensitive fields via `sdk/redaction.py`
- Feature gate prevents accidental email/password usage in production
- Password is sent in URL query string but **excluded from checksum**

## Verification Commands

```bash
# Run all tests
make test           # 40 unit tests
make automation-check  # 37 automation tests

# Live auth test (manual, requires env vars)
PSS_RUN_LIVE_AUTH_TESTS=1 \
PSS_TEST_EMAIL='user@example.com' \
PSS_TEST_PASSWORD='***' \
python -m unittest tests.test_live_auth.TestLiveAuth.test_fresh_email_password_login_e2e
```