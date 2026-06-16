"""
Input validation and sanitization utilities.
"""
import re
from fastapi import HTTPException


# UUID v4 pattern
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Safe filename pattern (alphanumeric, dashes, underscores, dots)
_SAFE_FILENAME_RE = re.compile(r"^[\w\-. ]+$", re.UNICODE)

# Max lengths
MAX_TITLE_LENGTH = 200
MAX_CONTEXT_LENGTH = 5000
MAX_MESSAGE_LENGTH = 10000


def validate_uuid(value: str, field_name: str = "id") -> str:
    """Validate that a string is a valid UUID v4."""
    if not _UUID_RE.match(value):
        raise HTTPException(400, f"Invalid {field_name}: must be a valid UUID")
    return value


def validate_filename(value: str) -> str:
    """Validate that a filename is safe (no path traversal)."""
    if not value or not _SAFE_FILENAME_RE.match(value):
        raise HTTPException(400, f"Invalid filename: {value!r}")
    if ".." in value or "/" in value or "\\" in value:
        raise HTTPException(400, "Invalid filename: path traversal detected")
    return value


def sanitize_text(value: str, max_length: int = MAX_MESSAGE_LENGTH) -> str:
    """Trim and truncate a text input."""
    if not value:
        return ""
    cleaned = value.strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


def validate_plan_id(value: str) -> str:
    """Validate plan identifier."""
    valid = {"free", "starter", "pro", "team"}
    if value not in valid:
        raise HTTPException(400, f"Invalid plan: {value}. Must be one of {valid}")
    return value
