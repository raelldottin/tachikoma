# Static Analysis Findings — Pixel Starships IL2CPP (v31.1, Unity 6000.0.77f1)

## Target Binary
- **GameAssembly.dylib**: `/Applications/Pixel Starships.app/Contents/Frameworks/GameAssembly.dylib` (367 MB, Mach-O arm64)
- **global-metadata.dat**: `/Applications/Pixel Starships.app/Contents/Resources/Data/il2cpp_data/Metadata/global-metadata.dat` (IL2CPP metadata v31.1)
- **Cpp2IL**: v2022.1.0-pre-release.21 (runs under Rosetta x86_64 on macOS arm64)

---

## Address Map (RVAs from Cpp2IL ISIL output)

| Method / Symbol | RVA | Location |
|-----------------|-----|----------|
| `BuildKeyChecksum` (enum) | N/A | `SavySoda.PixelStarships.Model.SharedModel.Enums.ChecksumType` |
| `HardcodedChecksum` (enum) | N/A | `SavySoda.PixelStarships.Model.SharedModel.Enums.ChecksumType` |
| `SavyChecksum` (static salt) | 0x4F7AAB0 | `Configuration.get_SavyChecksum()` |
| `ChecksumKey` (static key) | 0x4F64F20 | `Configuration.get_ChecksumKey()` |
| `SavysodaEncryptString` | ~0xB33CA0 | `StringExtensions.SavysodaEncryptString(String)` |
| `Md5Sum` | ~0xB33D50 | `StringExtensions.Md5Sum(String)` |
| `ChecksumPasswordWithString` | ~0x9BF100 | `SharedManager.ChecksumPasswordWithString(String)` |
| `TimeCheckSum` | ~0x9BF000 | `SharedManager.TimeCheckSum(DateTime)` |
| `TimeCheckSumForDate` | ~0xA1A780 | `UserManager.TimeCheckSumForDate(DateTime)` |
| `FinaliseChecksumWithDesigns` | ~0x787300 | `BattleManager.FinaliseChecksumWithDesigns(PSBattle, Int32, Int32)` |
| `CollectMarker` (checksum generation) | ~0x84CC00 | `GalaxyManager.CollectMarker(Int32, ...)` |
| `DownloadUserLogin` (login checksum) | ~0xA1B600 | `UserManager.DownloadUserLogin(...)` |

---

## Checksum Pipeline Architecture

### 1. Configuration Constants (Static, embedded in binary)
```csharp
// Configuration.get_SavyChecksum() → returns constant at 0x4F7AAB0
// Configuration.get_ChecksumKey() → returns constant at 0x4F64F20
```
- **SavyChecksum**: 32-char hex string (the "salt" from plist: `91cde416c93fb401585d963a556381ca`)
- **ChecksumKey**: static string used in concatenation

### 2. Core Transformation: `SavysodaEncryptString`
```csharp
// StringExtensions.SavysodaEncryptString(dataString):
//   1. Get Configuration.SavyChecksum (salt)
//   2. String.Concat(dataString, SavyChecksum)  ← PREIMAGE
//   3. Return concatenated string
```
**Key finding**: The "encryption" is just **string concatenation** of the input with the static salt.

### 3. Hashing: `Md5Sum`
```csharp
// StringExtensions.Md5Sum(strToEncrypt):
//   1. UTF8Encoding.GetBytes(strToEncrypt)
//   2. MD5CryptoServiceProvider.ComputeHash(bytes)
//   3. Convert each byte to hex (lowercase, 2 chars each)
//   4. Return 32-char hex string
```
**Uses managed `System.Security.Cryptography.MD5`** — not native.

### 4. API Request Checksum Generation (e.g., CollectMarker)

In `GalaxyManager.CollectMarker(Int32 starSystemMarkerId, ...)`:

```
1. clientDateTime = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture)
   → e.g., "2026-08-01T11:11:32.1234567Z"

2. markerIdStr = starSystemMarkerId.ToString()

3. designVersion = Configuration.GetLatestVersion4()  // reads design versions

4. preimage = String.Concat(
       markerIdStr,
       clientDateTime,
       designVersion,           // from GetLatestVersion4
       Configuration.ChecksumKey  // static key
   )

5. encrypted = StringExtensions.SavysodaEncryptString(preimage)
   // = preimage + SavyChecksum (salt)

6. checksum = StringExtensions.Md5Sum(encrypted)
   // MD5(preimage + salt)

7. Pass checksum as query param to CollectMarker2 endpoint
```

**Critical**: The `designVersion` comes from `GetLatestVersion4` (server-synced design data). This changes when game data updates.

### 5. Login Checksum (UserEmailPasswordAuthorize4)

In `UserManager.DownloadUserLogin()`:

```
1. clientDateTime = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture)

2. loginTypeStr = LoginType enum.ToString()

3. preimage = String.Concat(
       clientDateTime,
       LoginTypeStr,
       Configuration.ChecksumKey
   )

4. encrypted = StringExtensions.SavysodaEncryptString(preimage)
   // = preimage + SavyChecksum

5. checksum = StringExtensions.Md5Sum(encrypted)
   // MD5(preimage + salt)

6. StartCoroutine(DownloadUserLoginCoroutine(clientDateTime, checksum, ...))
```

---

## Why Offline Formulas Failed

The research lab tested **36+ formulas** against captured samples but missed:

| Missing Input | Source | Notes |
|---------------|--------|-------|
| `designVersion` | `GetLatestVersion4` (server response) | Runtime-only, changes per app version |
| Exact concatenation order | Verified in `GalaxyManager.CollectMarker` | markerId + datetime + designVersion + ChecksumKey |
| Salt (`SavyChecksum`) | `Configuration.get_SavyChecksum()` | Static, but must be concatenated *after* preimage |

**The actual preimage for CollectMarker2**:
```
markerIdStr + clientDateTime + designVersion + ChecksumKey + SavyChecksum
```

Then MD5 of the full string.

---

## Next Steps

### 1. Capture Design Versions (mitmproxy)
Record `GetLatestVersion4` response alongside each checksum sample. The design versions are the **only dynamic input** not in fixtures.

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

## Blocked Paths

| Method | Status |
|--------|--------|
| Frida attach (iOS/macOS) | **Blocked** — anti-tampering |
| IL2CppDumper | **Incompatible** — IL2CPP v31 / Mach-O |
| Cpp2IL IL body recovery | **Partial** — ISIL gives disassembly + pseudo-IL, not full C# |

---

## Recommended Path Forward

1. **mitmproxy capture session** with `GetLatestVersion4` + all checksum endpoints in one uninterrupted run
2. **Extract SavyChecksum & ChecksumKey** from binary at RVAs (or dump Configuration at runtime via LLDB)
3. **Offline reproduce** all 6 fixture samples
4. **Implement shared native-checksum module** in Tachikoma SDK
5. **Live test** one request per endpoint type