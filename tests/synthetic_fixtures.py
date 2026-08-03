#!/usr/bin/env python3
"""Synthetic test fixtures for security tests.

These are NOT derived from live captures. They are constructed to test
the checksum formula logic without exposing any real device identifiers,
tokens, or capture-derived values.

For exact live-capture verification vectors, see .private-fixtures/checksum_vectors.py
(ignored by git and never committed).
"""

# Synthetic device keys (valid UUID format, not from any capture)
SYNTHETIC_DEVICE_KEY_IOS = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
SYNTHETIC_DEVICE_KEY_MAC = "11111111-2222-3333-4444-555555555555"

# Synthetic timestamps (stripped to seconds as required by checksum formula)
SYNTHETIC_CDT_1 = "2026-08-03T00:40:41"
SYNTHETIC_CDT_2 = "2026-08-03T00:41:42"

# Synthetic checksum keys
SYNTHETIC_CHECKSUM_KEY = "5343"
SYNTHETIC_SAVY_CHECKSUM = "Savvy!s0d@"

# Synthetic email
SYNTHETIC_EMAIL = "test@example.com"

# Synthetic access token (UUID format)
SYNTHETIC_ACCESS_TOKEN = "00000000-1111-2222-3333-444444444444"

# Expected checksums computed with synthetic inputs
# These are deterministically derived from the synthetic vectors above
# using the verified formula: MD5(deviceKey + cdt + DeviceTypeIPhone + checksumKey + savyChecksum)

SYNTHETIC_IOS_CHECKSUM_1 = "e6ba2e6b47f60a372e272353107273cd"
SYNTHETIC_IOS_CHECKSUM_2 = "9e50a7538a8ebe66874001b9aaa88e41"
SYNTHETIC_EMAIL_AUTH_CHECKSUM = "634a9fe7cacbce1ee42b799ac3727b26"