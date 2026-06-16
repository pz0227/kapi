"""
Security middleware — headers, CORS enforcement, audit logging.
"""
import logging
import time
import uuid
from datetime import datetime

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("kapi.security")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add production security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # Block MIME-type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # XSS protection (legacy browsers)
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # Referrer policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Permissions policy — restrict sensitive browser APIs
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(self)"
        )
        # Content-Security-Policy for the landing page
        if request.url.path in ("/landing",) or request.url.path.startswith("/static"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "font-src 'self'; "
                "script-src 'self'; "
                "frame-ancestors 'none'"
            )

        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every request with timing, method, path, status, and user info."""

    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        start = time.perf_counter()

        # Extract user hint from auth header
        auth = request.headers.get("authorization", "")
        user_hint = "anon"
        if auth.startswith("Bearer "):
            user_hint = f"jwt:{auth[7:15]}..."

        response = await call_next(request)

        elapsed_ms = (time.perf_counter() - start) * 1000
        status = response.status_code

        # Color-code log level by status
        if status >= 500:
            log.error(
                "[%s] %s %s -> %d (%.0fms) user=%s",
                request_id, request.method, request.url.path, status, elapsed_ms, user_hint,
            )
        elif status >= 400:
            log.warning(
                "[%s] %s %s -> %d (%.0fms) user=%s",
                request_id, request.method, request.url.path, status, elapsed_ms, user_hint,
            )
        else:
            log.info(
                "[%s] %s %s -> %d (%.0fms) user=%s",
                request_id, request.method, request.url.path, status, elapsed_ms, user_hint,
            )

        response.headers["X-Request-Id"] = request_id
        return response


# ── Audit log helpers ────────────────────────────────────────────────────────

async def record_audit(
    db: AsyncSession,
    org_id: str,
    user_id: str,
    action: str,
    resource_type: str,
    resource_id: str = "",
    details: str = "",
) -> None:
    """Record an audit event. Non-blocking — errors are swallowed."""
    try:
        from core.database import AuditLog
        entry = AuditLog(
            id=str(uuid.uuid4()),
            org_id=org_id,
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            created_at=datetime.utcnow(),
        )
        db.add(entry)
        # Don't commit here — let the route's own commit handle it
    except Exception as exc:
        log.debug("[audit] Failed to record: %s", exc)
