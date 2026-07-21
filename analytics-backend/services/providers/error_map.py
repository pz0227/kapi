"""
Provider error classification.

The most common thing a user gets wrong is their API key (invalid, restricted,
rate-limited, expired session). The raw SDK exception is unhelpful ("Error code:
401 - {...}"). This maps a provider exception to an (http_status, user_message)
pair that tells the user what to actually do about it.

Kept as a pure function so it's testable and consistent across every route that
calls a provider, rather than re-implemented inline in each handler.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MappedError:
    status: int
    message: str
    kind: str          # "auth" | "scopes" | "rate_limit" | "session" | "unknown"
    persist_error: bool  # should we mark the provider config as errored?


def classify_provider_error(exc: Exception, provider_name: str = "the provider") -> MappedError:
    """Map a provider exception to a user-actionable HTTP error. Never raises."""
    s = str(exc).lower()

    if any(k in s for k in ("invalid api key", "authentication", "unauthorized", " 401")):
        return MappedError(
            401,
            f"Provider authentication failed ({provider_name}). "
            "Go to Settings and update your API key, or switch to a different provider.",
            "auth", True,
        )
    if any(k in s for k in ("missing scopes", "insufficient permissions", "restricted")):
        return MappedError(
            403,
            f"API key lacks required permissions ({provider_name}). "
            "Your key may be restricted; create an unrestricted key or try a different model.",
            "scopes", False,
        )
    if any(k in s for k in ("rate limit", "rate_limit", "429", "too many requests")):
        return MappedError(
            429,
            f"Rate limit hit ({provider_name}). Wait a moment and try again.",
            "rate_limit", False,
        )
    if any(k in s for k in ("session", "browser", "expired")):
        return MappedError(
            503,
            f"Browser session error ({provider_name}). "
            "Go to Settings and re-authenticate.",
            "session", True,
        )
    return MappedError(
        502,
        f"AI provider error ({provider_name}): {exc}",
        "unknown", False,
    )
