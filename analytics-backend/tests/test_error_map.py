"""Tests for provider error classification — the most common user-facing error
(bad/restricted/rate-limited API key) mapped to an actionable message."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.providers.error_map import classify_provider_error


def test_invalid_key_maps_to_401_and_persists():
    m = classify_provider_error(Exception("Error code: 401 - Invalid API key"), "OpenAI")
    assert m.status == 401 and m.kind == "auth" and m.persist_error is True
    assert "API key" in m.message and "OpenAI" in m.message


def test_restricted_key_maps_to_403_no_persist():
    m = classify_provider_error(Exception("missing scopes: api.responses.write"))
    assert m.status == 403 and m.kind == "scopes" and m.persist_error is False


def test_rate_limit_maps_to_429():
    m = classify_provider_error(Exception("429 Too Many Requests: rate limit exceeded"))
    assert m.status == 429 and m.kind == "rate_limit"


def test_session_error_maps_to_503_and_persists():
    m = classify_provider_error(Exception("browser session expired"))
    assert m.status == 503 and m.kind == "session" and m.persist_error is True


def test_unknown_error_falls_through_to_502():
    m = classify_provider_error(Exception("some novel upstream failure"), "Gemini")
    assert m.status == 502 and m.kind == "unknown"
    assert "Gemini" in m.message


def test_never_raises_on_weird_exception():
    class Weird(Exception):
        def __str__(self): return "boom 401 unauthorized"
    assert classify_provider_error(Weird()).status == 401
