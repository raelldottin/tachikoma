# Tachikoma Security Cleanup Plan

## Issue Summary

The repository contains exposed authentication credentials:

1. **Hardcoded JWT token in git history** (commit `e4225e7`): `eyJhbG...1Rqw` (truncated Pixel Starships refresh token with identifying account info)
2. **Real refresh token in `.device` file** (gitignored but present locally): `eyJhbG...xl6c`
3. **Secrets logged in URLs** - Access tokens and refresh tokens appear in query parameters in logged URLs
4. **Secrets in response bodies logged** - Error logging outputs full response XML containing tokens
5. **No secret scanning in CI** - No automated detection of credential leaks
6. **Email credentials in GitHub Actions secrets** - Sent to every workflow run unnecessarily

## Affected Commits

| Commit | Date | Description |
|--------|------|-------------|
| `e4225e7` | 2024-01-19 | "change devicelogin endpoint" - **ADDED hardcoded token** |

Only one commit introduced the token, but it's been in `dev` branch since.

## Immediate Actions Required

### 1. Remove Token from Git History (CRITICAL)
```bash
# Install git-filter-repo (recommended over BFG)
pip install git-filter-repo

# Remove the token from all history
git filter-repo --replace-text <(echo "eyJhbG...1Rqw==>***REDACTED_PIXEL_STARSHIPS_TOKEN***") --force

# Or use BFG Repo-Cleaner
# java -jar bfg.jar --replace-text passwords.txt
```

### 2. Rotate Exposed Token
- The exposed token is a **Pixel Starships refresh token** containing account identity
- Must be invalidated via Pixel Starships account settings or by logging in from a new device
- Generate new device key/refresh token pair after rotation

### 3. Redact Secrets in Logs
- URLs with `accessToken=` and `refreshToken=` query parameters
- Response bodies containing `accessToken="..."` or `refreshToken="..."`
- Email/password in authentication URLs
- Device keys in URLs

### 4. Add Secret Scanning to CI
- Use `gitleaks` or `trufflehog` in PR workflow
- Block merges on secret detection

### 5. Use Environment Variables for Config
- Device authentication string via `TACHIKOMA_AUTH` env var
- Email credentials via env vars (not GitHub secrets passed to every job)

## Files to Modify

### `sdk/client.py`
- Redact tokens in `logging.error()` calls (lines 118, 224, 268, 285, 291, etc.)
- Use shared HTTP transport with request/response filtering
- Remove fallback token behavior (already done in working tree)

### `sdk/device.py`
- Don't log device key or refresh token
- Ensure `.device` file permissions are restrictive (0600)

### `run.py`
- Don't log email credentials
- Redact auth string in logs

### `.github/workflows/hourly-run.yml`
- Convert to matrix strategy
- Only pass email credentials to jobs that need them
- Add secret scanning step

### New: `.github/workflows/ci.yml`
- Lint, type-check, test, secret-scan on PRs

### New: `.github/workflows/scheduled.yml`
- Matrix of accounts
- Structured reporting

## Token Rotation Instructions

1. Log into Pixel Starships on a new device
2. Go to Settings → Account → Devices
3. Revoke all existing device authorizations
4. Run Tachikoma with new device to generate fresh token
5. Update GitHub secrets: `auth_string_first`, `auth_string_second`, etc.
6. Delete old `.device` file locally

## Validation Checklist

- [ ] No tokens in git history (`git log -p --all | grep -i "eyJhbG"`)
- [ ] No tokens in current codebase
- [ ] Logs redact tokens (test with `python -m tachikoma doctor --auth "$TACHIKOMA_AUTH"`)
- [ ] Secret scanning runs on every PR
- [ ] CI fails on secret detection
- [ ] Email credentials not leaked in CI logs
- [ ] Token rotation documented and tested