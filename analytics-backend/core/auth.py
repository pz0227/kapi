"""
Authentication middleware — JWT-based auth for multi-tenant SaaS.

Supports two modes:
  1. Clerk JWT verification (production SaaS)
  2. Local mode with no auth (self-hosted / development)

Set KAPI_AUTH_MODE=clerk and CLERK_SECRET_KEY=sk_... for production.
Default is "local" which skips auth entirely.
"""
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, Request

from core.config import get_settings

log = logging.getLogger(__name__)

settings = get_settings()


@dataclass
class CurrentUser:
    """Represents the authenticated user for the current request."""
    user_id: str
    org_id: str
    email: str = ""
    plan: str = "free"


# ── JWKS cache for Clerk token verification ──────────────────────────────────

_jwks_cache: dict = {}
_jwks_fetched_at: float = 0.0
_JWKS_TTL = 3600  # 1 hour


async def _get_clerk_jwks() -> dict:
    """Fetch and cache Clerk's JWKS (JSON Web Key Set)."""
    global _jwks_cache, _jwks_fetched_at

    if _jwks_cache and (time.time() - _jwks_fetched_at) < _JWKS_TTL:
        return _jwks_cache

    clerk_domain = settings.clerk_domain
    if not clerk_domain:
        raise HTTPException(500, "CLERK_DOMAIN not configured")

    url = f"https://{clerk_domain}/.well-known/jwks.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise HTTPException(502, f"Failed to fetch Clerk JWKS: {resp.status_code}")
        _jwks_cache = resp.json()
        _jwks_fetched_at = time.time()
        return _jwks_cache


async def _verify_clerk_token(token: str) -> dict:
    """Verify a Clerk session JWT and return its claims."""
    try:
        import jwt as pyjwt
        from jwt import PyJWKClient
    except ImportError:
        raise HTTPException(500, "PyJWT not installed. Run: pip install PyJWT[crypto]")

    clerk_domain = settings.clerk_domain
    if not clerk_domain:
        raise HTTPException(500, "CLERK_DOMAIN not configured")

    jwks_url = f"https://{clerk_domain}/.well-known/jwks.json"
    jwk_client = PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=3600)

    try:
        signing_key = jwk_client.get_signing_key_from_jwt(token)
        claims = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        return claims
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired. Please sign in again.")
    except pyjwt.InvalidTokenError as exc:
        log.warning("[auth] Invalid JWT: %s", exc)
        raise HTTPException(401, "Invalid authentication token.")


# ── Local mode user (no auth) ────────────────────────────────────────────────

_LOCAL_USER = CurrentUser(
    user_id="local",
    org_id="local",
    email="local@kapi.dev",
    plan="team",  # full access in local mode
)


# ── Main dependency ──────────────────────────────────────────────────────────

async def get_current_user(request: Request) -> CurrentUser:
    """
    FastAPI dependency — extracts the authenticated user from the request.

    In local mode: returns a default local user with full access.
    In clerk mode: verifies the Bearer token and extracts user info.
    """
    auth_mode = settings.auth_mode

    if auth_mode == "local":
        return _LOCAL_USER

    # Clerk mode
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Missing authentication. Please sign in.")

    token = auth_header[7:]
    claims = await _verify_clerk_token(token)

    user_id = claims.get("sub", "")
    org_id = claims.get("org_id", user_id)  # fall back to user_id if no org
    email = claims.get("email", claims.get("email_address", ""))

    if not user_id:
        raise HTTPException(401, "Invalid token: no user ID.")

    return CurrentUser(
        user_id=user_id,
        org_id=org_id,
        email=email,
        plan="free",  # will be enriched by billing middleware
    )


async def get_optional_user(request: Request) -> Optional[CurrentUser]:
    """Same as get_current_user but returns None instead of 401 for unauthenticated requests."""
    try:
        return await get_current_user(request)
    except HTTPException:
        return None
