"""
Kapi Analytics Backend — sidecar service for product analytics, RAG, and reports.
Runs alongside the Kapi Gateway on port 18792 by default.
"""
import os
import sys
from pathlib import Path

# Ensure backend package root is on the path
sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager

from core.config import get_settings
from core.database import init_db
from core.logging_config import setup_logging
from api.routes import data, analytics, chat, providers, reports, eval as eval_router, insights, onboarding, export
from services.providers.discovery import auto_discover_providers

settings = get_settings()

# Initialize structured logging (JSON in production, colored in dev)
setup_logging(debug=settings.debug, json_output=(settings.auth_mode != "local"))

# Default analytics port — sits next to gateway (18789) and browser control (18791)
ANALYTICS_DEFAULT_PORT = 18792


def _warm_embedder_in_background():
    """Pre-load the sentence-transformers embedding model in a daemon thread.

    The first retrieval/eval/report query otherwise pays a ~45s one-time cost
    (importing transformers/torch + loading the model). Warming it in the
    background at startup moves that cost OFF the user's critical path, so the
    first AI Analyst / eval / report query returns warm. Best-effort: never
    blocks startup and never fails it.
    """
    import threading

    def _warm():
        try:
            from services.rag.embedder import embed_query
            embed_query("warmup")
        except Exception:
            pass  # warm-up is purely an optimization; ignore any failure

    threading.Thread(target=_warm, name="embedder-warmup", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await auto_discover_providers()
    _warm_embedder_in_background()
    yield


app = FastAPI(
    title="Kapi Analytics Backend",
    version=settings.app_version,
    description="Product analytics, RAG, and report generation service for Kapi.",
    lifespan=lifespan,
)

# CORS — restrictive in production, permissive in local mode
_cors_origins = (
    ["*"] if settings.auth_mode == "local"
    else settings.cors_origins + [settings.app_url]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-Id"],
    expose_headers=["X-RateLimit-Limit", "X-RateLimit-Remaining", "X-Request-Id"],
)

# Security headers (X-Frame-Options, CSP, etc.)
from core.security import SecurityHeadersMiddleware, RequestLoggingMiddleware
app.add_middleware(SecurityHeadersMiddleware)

# Request logging with timing and user info
app.add_middleware(RequestLoggingMiddleware)

# Rate limiting (in-memory; swap to Redis-backed in production)
from core.rate_limit import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware, general_rpm=120, ai_rpm=20)

# Mount API routes
app.include_router(data.router,      prefix="/api")
app.include_router(analytics.router, prefix="/api")
app.include_router(chat.router,      prefix="/api")
app.include_router(providers.router, prefix="/api")
app.include_router(reports.router,   prefix="/api")
app.include_router(eval_router.router, prefix="/api")
app.include_router(insights.router, prefix="/api")
app.include_router(onboarding.router, prefix="/api")
app.include_router(export.router, prefix="/api")


# ── Global error handler ──────────────────────────────────────────────────────

import logging
import traceback
from fastapi import Request
from fastapi.responses import JSONResponse

_log = logging.getLogger("kapi.analytics")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch unhandled exceptions and return a clean JSON error."""
    _log.error(
        "[%s %s] Unhandled error: %s\n%s",
        request.method, request.url.path,
        str(exc), traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Please try again or contact support."},
    )


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": settings.app_version,
        "app": "Kapi Analytics Backend",
        "auth_mode": settings.auth_mode,
    }


# ── Landing page & static assets ─────────────────────────────────────────────

_STATIC_DIR = Path(__file__).parent / "static"

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

@app.get("/landing")
async def landing_page():
    """Serve the marketing landing page."""
    landing = _STATIC_DIR / "landing.html"
    if landing.exists():
        return FileResponse(str(landing), media_type="text/html")
    return {"error": "Landing page not found"}


if __name__ == "__main__":
    import uvicorn
    import socket

    def _port_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) != 0

    port = int(os.environ.get("KAPI_ANALYTICS_PORT", ANALYTICS_DEFAULT_PORT))
    if not _port_free(port):
        # Do NOT silently fall back to alternate ports — the UI hardcodes
        # port 18792 and won't find us on a different port.  Instead, log
        # a clear error so the user knows to kill the stale process.
        print(f"[Kapi Analytics] ERROR: Port {port} is already in use!")
        print(f"[Kapi Analytics] Kill the process using port {port} and try again.")
        print(f"[Kapi Analytics] On Windows: for /f \"tokens=5\" %p in ('netstat -ano ^| findstr \":{port}\" ^| findstr \"LISTENING\"') do taskkill /PID %p /F")
        # Still try fallback ports as a last resort, but warn loudly
        for alt in [18793, 18794, 18795]:
            if _port_free(alt):
                print(f"[Kapi Analytics] WARNING: Falling back to port {alt} — UI may not connect!")
                port = alt
                break

    print(f"[Kapi Analytics] Starting on http://127.0.0.1:{port}")
    uvicorn.run("main:app", host="127.0.0.1", port=port, reload=False)
