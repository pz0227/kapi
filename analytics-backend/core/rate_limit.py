"""
Simple in-memory rate limiter for API endpoints.
In production, replace with Redis-backed limiter.
"""
import time
import logging
from collections import defaultdict
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Token-bucket rate limiter per IP/user.
    - 60 requests per minute for general API calls
    - 10 requests per minute for AI-heavy endpoints (chat, reports)
    """

    def __init__(self, app, general_rpm: int = 60, ai_rpm: int = 10):
        super().__init__(app)
        self.general_rpm = general_rpm
        self.ai_rpm = ai_rpm
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def _get_key(self, request: Request) -> str:
        """Get rate limit key from auth header or IP."""
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            # Use a hash of the token to avoid storing raw tokens
            return f"tok:{hash(auth)}"
        forwarded = request.headers.get("x-forwarded-for", "")
        ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
        return f"ip:{ip}"

    def _is_ai_endpoint(self, path: str) -> bool:
        """Check if this is an AI-heavy endpoint with lower rate limit."""
        ai_paths = ["/api/chat/", "/api/chat/stream", "/api/reports/", "/api/insights/analyze"]
        return any(path.startswith(p) or path == p for p in ai_paths)

    def _check_rate(self, key: str, limit: int) -> bool:
        """Return True if request is allowed, False if rate limited."""
        now = time.time()
        window = 60.0  # 1 minute window

        # Clean old entries
        self._buckets[key] = [t for t in self._buckets[key] if now - t < window]

        if len(self._buckets[key]) >= limit:
            return False

        self._buckets[key].append(now)
        return True

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health checks and static files
        path = request.url.path
        if path in ("/api/health", "/landing") or path.startswith("/static"):
            return await call_next(request)

        # Skip webhooks (Stripe calls directly)
        if path == "/api/billing/webhook":
            return await call_next(request)

        key = self._get_key(request)
        limit = self.ai_rpm if self._is_ai_endpoint(path) else self.general_rpm

        if not self._check_rate(key, limit):
            log.warning("[rate_limit] Rate limited: %s on %s", key, path)
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded. Max {limit} requests per minute for this endpoint.",
            )

        response = await call_next(request)

        # Add rate limit headers
        remaining = max(0, limit - len(self._buckets.get(key, [])))
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return response
