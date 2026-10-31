"""Redaction helpers for anything persisted or streamed by the remediation module."""

import re
from typing import Any

from opspilot_api.investigations.safety import redact_text, sanitize_error
from opspilot_api.repositories.policy import redact_code

__all__ = ["excerpt", "redact_code", "redact_text", "sanitize_error", "scrub_details"]

_SENSITIVE = re.compile(
    r"(?i)(secret|password|api[_-]?key|authorization|private[_-]?key|(^|_)token($|_))"
)
MAX_EXCERPT = 4000
_IDENTIFIER = re.compile(
    r"[0-9a-f]{7,64}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


_INLINE_SECRET = re.compile(
    r"(?i)\b(?:token|secret|password|passwd|api[_-]?key|authorization)\b\s*[=:]\s*\S+"
    r"|\bospk_[A-Za-z0-9_-]{8,}|\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{4,}|\bBearer\s+\S{12,}"
)


def _mask(value: str) -> str:
    return _INLINE_SECRET.sub("[REDACTED]", value)


def excerpt(value: str, limit: int = MAX_EXCERPT) -> str:
    """Bounded, redacted text safe to store (command output, logs)."""
    return redact_text(value, max_length=limit)


def scrub_details(value: Any, depth: int = 0) -> Any:
    """Drop sensitive keys and bound size for audit/event payloads."""
    if depth > 4:
        return None
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]" if _SENSITIVE.search(str(key)) else scrub_details(item, depth + 1)
            )
            for key, item in list(value.items())[:50]
        }
    if isinstance(value, list):
        return [scrub_details(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        if _IDENTIFIER.fullmatch(value):
            return value  # hashes, SHAs, and UUIDs must survive byte-for-byte
        return _mask(redact_text(value, max_length=500))
    return value
