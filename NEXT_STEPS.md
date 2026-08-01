# Pixel Starships Checksum Research — Static Analysis Complete

## Summary

### Static Analysis Findings (Cpp2IL + ISIL)

| Target | RVA | Status |
|--------|-----|--------|
| `SavyChecksum` (salt) | `0x4F7AAB0` | Points to static field in `__DATA` segment; value not directly extractable from binary (likely initialized at runtime) |
| `ChecksumKey` | `0x4F64F20` | Same as above |
| `SavysodaEncryptString` | `~0xB33CA0` | Confirmed: `String.Concat(input, SavyChecksum)` |
| `Md5Sum` | `~0xB33D50` | Confirmed: Managed `MD5CryptoServiceProvider` |
| `CollectMarker` checksum gen | `~0x84CC00` | Traced: builds preimage with `designVersion` from `GetLatestVersion4` |
| `DownloadUserLogin` (login) | `~0xA1B600` | Traced: same pattern without `designVersion` |

### Confirmed Checksum Pipeline

```csharp
// CollectMarker2 / RebuildAmmo3 / etc.
preimage = markerIdStr + clientDateTime + designVersion + ChecksumKey
encrypted = SavysodaEncryptString(preimage)  // = preimage + SavyChecksum (concatenation)
checksum = Md5Sum(encrypted)                  // = MD5(preimage + SavyChecksum)

// UserEmailPasswordAuthorize4
preimage = clientDateTime + LoginTypeStr + ChecksumKey
encrypted = SavysodaEncryptString(preimage)  // = preimage + SavyChecksum
checksum = Md5Sum(encrypted)
```

### Key Finding

The **`designVersion`** from `GetLatestVersion4` (server-synced design data) is the **only dynamic input** not captured in fixtures. This explains why 36+ offline formulas failed — they lacked this runtime value.

---

## Next Steps

### 1. Capture Design Versions + Checksums (mitmproxy)

Run the prepared capture script:

```bash
# On macOS (where Pixel Starships.app runs):
./scripts/run_capture.sh

# This starts mitmproxy on port 8080
# Configure iOS device / macOS to proxy through this Mac
# Install mitmproxy CA: http://mitm.it
# Play game → triggers CollectMarker2, RebuildAmmo3, UserEmailPasswordAuthorize4
# Ctrl+C to save capture
```

The script saves:
- Full JSON capture with all requests/responses
- CSV summary with `timestamp,endpoint,checksum,design_versions_at_time`
- `GetLatestVersion4` responses paired with each checksum endpoint call

### 2. Offline Reproduction

Once you have captures with `designVersion`:
```python
# Extract constants from capture (or dump at runtime via LLDB)
SavyChecksum = "..."  # 32-char hex salt
ChecksumKey = "..."   # static key

# For each captured sample:
preimage = f"{markerId}{clientDateTime}{designVersion}{ChecksumKey}"
encrypted = preimage + SavyChecksum
checksum = md5(encrypted.encode()).hexdigest()
# Must match captured checksum exactly
```

### 3. Verify All 6 Fixtures

| Sample | Endpoint | Dynamic Input Needed |
|--------|----------|---------------------|
| `collect_1` | CollectMarker2 | designVersion at 2026-08-01T00:17:05 |
| `collect_2` | CollectMarker2 | designVersion at 2026-08-01T00:17:08 |
| `collect_3` | CollectMarker2 | designVersion at 2026-08-01T00:46:10 |
| `rebuild_1` | RebuildAmmo3 | designVersion at 2026-07-31T23:49:19 |
| `rebuild_2` | RebuildAmmo3 | designVersion at 2026-08-01T00:43:59 |
| `authorize_1` | UserEmailPasswordAuthorize4 | (no designVersion needed) |

### 4. Implement & Live Test

Once reproduction works:
1. Add shared `NativeChecksum` module to Tachikoma SDK
2. Live test one request per endpoint type
3. Verify state changes (marker collected, ammo rebuilt, login successful)

---

## Files Created

| File | Purpose |
|------|---------|
| `STATIC_ANALYSIS_FINDINGS.md` | Full technical documentation |
| `scripts/capture_checksum_inputs.py` | mitmproxy capture script |
| `scripts/run_capture.sh` | Runner script |
| `scripts/extract_constants.py` | Binary extraction (partial — constants at runtime) |

---

## Blocked Paths

| Approach | Status |
|----------|--------|
| Extract constants from binary directly | Values not present as plain strings; initialized at runtime via IL2CPP metadata |
| Frida attach | Blocked by anti-tampering |
| IL2CppDumper | Incompatible with IL2CPP v31 / Mach-O |
| Cpp2IL IL body recovery | Partial — gives disassembly + pseudo-IL |

---

## Ready to Proceed

Run `./scripts/run_capture.sh` and play the game to capture `GetLatestVersion4` + checksum endpoints in one session. The CSV output will give you the exact `designVersion` for each sample.