#!/usr/bin/env python3
"""
Log redaction utilities for Tachikoma.

Provides functions to redact sensitive information from log messages
before they are written to disk or stdout.
"""

import re
from typing import Optional


# Patterns for sensitive data that should be redacted
# Order matters: more specific patterns (quoted JSON values) before generic URL patterns
REDACTION_PATTERNS = [
    # JWT tokens (base64 encoded, typically start with eyJ)
    (re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b'), '***REDACTED_JWT***'),
    # Generic long base64 strings that could be tokens
    (re.compile(r'\b[A-Za-z0-9+/]{40,}={0,2}\b'), '***REDACTED_BASE64***'),
    # accessToken="xxx" in response bodies (with quotes) - MUST come before URL pattern
    (re.compile(r'(accessToken=")[^"]+(")'), r'\1***REDACTED***\2'),
    # refreshToken="xxx" in response bodies (with quotes) - MUST come before URL pattern
    (re.compile(r'(refreshToken=")[^"]+(")'), r'\1***REDACTED***\2'),
    # Device keys in JSON: "DeviceKey":"xxx"
    (re.compile(r'("DeviceKey"\s*:\s*")[^"]+(")'), r'\1***REDACTED***\2'),
    # Refresh tokens in JSON: "RefreshToken":"xxx"
    (re.compile(r'("RefreshToken"\s*:\s*")[^"]+(")'), r'\1***REDACTED***\2'),
    # Email in JSON: "Email":"xxx" or "email":"xxx"
    (re.compile(r'("Email"\s*:\s*")[^"]+(")', re.IGNORECASE), r'\1***REDACTED_EMAIL***\2'),
    # Password in JSON: "password":"xxx"
    (re.compile(r'("password"\s*:\s*")[^"]+(")', re.IGNORECASE), r'\1***REDACTED***\2'),
    # Generic email in any quoted string
    (re.compile(r'"(?:Email|email)"\s*:\s*"([^"]+@[^"]+)"'), r'"email": "***REDACTED_EMAIL***"'),
    # Access tokens in URLs: accessToken=xxx (exclude quotes from URL values)
    (re.compile(r'(accessToken=)[^&\s"]+'), r'\1***REDACTED***'),
    # UUID-format access tokens (bare, not in URL — e.g. in error messages or tracebacks)
    (re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.IGNORECASE), '***REDACTED_UUID***'),
    # Refresh tokens in URLs: refreshToken=xxx (exclude quotes from URL values)
    (re.compile(r'(refreshToken=)[^&\s"]+'), r'\1***REDACTED***'),
    # Device keys in URLs: deviceKey=xxx (exclude quotes from URL values)
    (re.compile(r'(deviceKey=)[^&\s"]+'), r'\1***REDACTED***'),
    # Email addresses
    (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'), '***REDACTED_EMAIL***'),
    # Passwords in URLs: password=xxx
    (re.compile(r'(password=)[^&\s]+', re.IGNORECASE), r'\1***REDACTED***'),
    # Authorization headers: Authorization: Bearer xxx
    (re.compile(r'(Authorization:\s*Bearer\s+)[^\s]+', re.IGNORECASE), r'\1***REDACTED***'),
]


def redact_secrets(text: str) -> str:
    """
    Redact sensitive information from a string.
    
    Args:
        text: Input string potentially containing secrets
        
    Returns:
        String with sensitive data replaced by redaction markers
    """
    if not text:
        return text
        
    result = text
    for pattern, replacement in REDACTION_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_dict(d: dict, sensitive_keys: Optional[set] = None) -> dict:
    """
    Redact sensitive values from a dictionary.
    
    Args:
        d: Dictionary to redact
        sensitive_keys: Additional keys to redact (case-insensitive)
        
    Returns:
        New dictionary with sensitive values redacted
    """
    if sensitive_keys is None:
        sensitive_keys = {
            'accesstoken', 'refreshtoken', 'devicekey', 'advertisingkey',
            'password', 'email', 'authorization', 'secret', 'key', 'token',
            'credits', 'checksum'
        }
    
    result = {}
    for key, value in d.items():
        key_lower = key.lower()
        if key_lower in sensitive_keys:
            if key_lower == 'email':
                result[key] = '***REDACTED_EMAIL***'
            else:
                result[key] = '***REDACTED***'
        elif isinstance(value, dict):
            result[key] = redact_dict(value, sensitive_keys)
        elif isinstance(value, list):
            result[key] = [
                redact_dict(item, sensitive_keys) if isinstance(item, dict)
                else redact_secrets(str(item)) if isinstance(item, str)
                else item
                for item in value
            ]
        elif isinstance(value, str):
            result[key] = redact_secrets(value)
        else:
            result[key] = value
    return result


def safe_log_message(message: str, *args) -> str:
    """
    Format a log message with redaction applied.
    
    Args:
        message: Format string
        *args: Arguments to format into message
        
    Returns:
        Formatted and redacted message
    """
    try:
        formatted = message % args if args else message
    except (TypeError, ValueError):
        formatted = str(message)
        if args:
            formatted += ' ' + ' '.join(str(a) for a in args)
    return redact_secrets(formatted)


def redact_log(message: str, *args) -> str:
    """Alias for safe_log_message for backward compatibility."""
    return safe_log_message(message, *args)