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

### 2. Extract Static Constants

From binary at known RVAs:
- `0x4F7AAB0` → `SavyChecksum` (salt)
- `0x4F64F20` → `ChecksumKey`

Or read from `Configuration` at runtime via Frida/LLDB (anti-attach blocks Frida on non-jailbroken).

### 3. Offline Reproduction Test

```python
# Once constants + designVersion known:
preimage = f"{markerId}{clientDateTime}{designVersion}{checksumKey}"
encrypted = preimage + savyChecksum  # SavysodaEncryptString
checksum = md5(encrypted.encode()).hexdigest()
```

### 4. Verify Against Captured Samples

- `authorize_1`: UserEmailPasswordAuthorize4 checksum `8418e1e0a07c1ed794789df7d8edc6ea`
- `collect_1`, `collect_2`, `collect_3`: CollectMarker2 samples
- `rebuild_1`, `rebuild_2`: RebuildAmmo3 samples

---

## Runtime Verification Gates

### CollectMarker2 — Enable `ENABLE_COLLECT_MARKER` only after:

1. Every historical `CollectMarker2` fixture matches offline.
2. A newly captured official-client request matches offline.
3. Tachikoma receives a successful response.
4. A follow-up read confirms the marker disappeared or the expected resources increased.
5. A second call produces the expected idempotent/no-op outcome.

### RebuildAmmo3 — Enable `ENABLE_REBUILD_AMMO` only after:

1. Every historical `RebuildAmmo3` fixture matches offline.
2. A newly captured official-client request matches offline.
3. Tachikoma receives a successful response.
4. A follow-up ship-state read confirms ammunition was restored.
5. A second call produces the expected already-rebuilt/no-op outcome.

### UserEmailPasswordAuthorize4 — Remains blocked

Uses `BuildKeyChecksum → FinaliseChecksumWithDesigns` native pipeline; no offline formula yet.

---

## Fixture Runner Separation

```python
SUPPORTED_NATIVE_SAMPLES = {"CollectMarker2", "RebuildAmmo3"}

blocked_samples = [
    s for s in samples if s["endpoint"] == "UserEmailPasswordAuthorize4"
]
```

---

## Implementation Status

> `CollectMarker2` and `RebuildAmmo3` have recovered checksum formulas and are pending verification, not further reverse engineering. `UserEmailPasswordAuthorize4` remains a separate native-analysis problem, so pre-provisioned `-a` authentication remains the supported login path.