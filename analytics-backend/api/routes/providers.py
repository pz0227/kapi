"""
Provider configuration API — CRUD, connection test, browser login.

Patched by kapi_app_ver_1.3 (vendored on top of the npm-shipped backend).

Why this patch exists
---------------------
The dashboard reads `/providers/gateway-status` to decide whether to render
analytics or the "AI Provider — Re-authentication Required" banner. The
upstream version of `gateway_status()` ONLY probes the GatewayProxyProvider
(OpenAI Codex via the Kapi Gateway). When a user instead configured a
direct API-key provider (`openai`, `anthropic`, ...), Chat works fine —
because chat.py's `_get_provider_from_config` resolves the active
ProviderConfig directly, or falls back to env vars — but the dashboard
shows "Re-authenticate" because the gateway has no token.

Result: inconsistent UX. Chat says "you're connected", Dashboard says
"reconnect". Both surfaces should reflect the *same* underlying truth:
"is there ANY provider Kapi can use right now?"

The fix
-------
Centralize the auth check in the backend. After the gateway probe, if
the gateway is not usable, call `_has_alternative_provider(db)` — which
mirrors chat.py's resolution: active ProviderConfig with credential, OR
env-var fallback. If either exists, set `auth_ok=True` and report the
provider via the `via` / `provider_label` fields.

Both Chat and Dashboard then agree, and we don't fake a "connected"
state — the dashboard checks the real usable provider state, exactly
matching what chat.py would reach for on the next message.

OpenClaw-inspired additions (carried over from upstream):
- session_status computed from session_expires_at (valid/expiring_soon/expired/no_session)
- session_expires_at persisted from JWT exp after browser login
- POST /{id}/refresh-session — silent token refresh without full re-login
- Error state written back on failed completions (via update_provider_error)
"""
import uuid
import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from core.config import get_settings
from core.database import get_db, ProviderConfig
from models.schemas import ProviderConfigCreate, ProviderConfigOut, ProviderTestResult, BrowserAuthStatus, QuickConnectRequest, TokenSubmitRequest
from services.providers import get_provider, encrypt_key, invalidate, PROVIDER_CATALOGUE
from services.providers.gateway_proxy_provider import GatewayProxyProvider
from services.providers.openai_browser_provider import OpenAIBrowserProvider
from services.providers.discovery import auto_discover_providers
from core.config import write_api_key_to_env

import logging as _logging
_prov_log = _logging.getLogger(__name__)

router = APIRouter(prefix="/providers", tags=["providers"])
settings = get_settings()

# Track in-flight browser login tasks
_browser_tasks: dict[str, dict] = {}

# ── Expiry helpers (mirrors OpenClaw's resolveTokenExpiryState) ───────────────

_EXPIRY_REFRESH_SECS = 15 * 60   # matches REFRESH_THRESHOLD_SECS in provider


def _compute_session_status(pc: ProviderConfig) -> str:
    """
    Return a session_status string for the given ProviderConfig.
    Only meaningful for browser_session auth; api_key configs return None.
    """
    if pc.auth_method != "browser_session":
        return None

    if not pc.session_file or not Path(pc.session_file).exists():
        return "no_session"

    if pc.session_expires_at is None:
        return "unknown"

    now = datetime.utcnow()
    remaining = (pc.session_expires_at - now).total_seconds()
    if remaining <= 0:
        return "expired"
    if remaining <= _EXPIRY_REFRESH_SECS:
        return "expiring_soon"
    return "valid"


def _to_out(pc: ProviderConfig) -> ProviderConfigOut:
    return ProviderConfigOut(
        id=pc.id,
        provider=pc.provider,
        label=pc.label,
        model=pc.model,
        auth_method=pc.auth_method,
        is_active=pc.is_active,
        has_api_key=bool(pc.api_key_encrypted),
        has_session=bool(pc.session_file and Path(pc.session_file).exists()),
        created_at=pc.created_at,
        last_used_at=pc.last_used_at,
        session_expires_at=pc.session_expires_at,
        session_status=_compute_session_status(pc),
        last_error_at=pc.last_error_at,
        last_error_msg=pc.last_error_msg,
    )


# ── Public helper: called from chat route to record auth errors ───────────────

async def update_provider_error(db: AsyncSession, config_id: str, error_msg: str) -> None:
    """Record an auth/provider error on the ProviderConfig row.
    Called from chat.py when a completion fails with auth/session errors."""
    try:
        result = await db.execute(select(ProviderConfig).where(ProviderConfig.id == config_id))
        pc = result.scalar_one_or_none()
        if pc:
            pc.last_error_at = datetime.utcnow()
            pc.last_error_msg = error_msg[:255]
            await db.commit()
    except Exception:
        pass  # don't let error tracking break the request flow


# ── Centralized "is any provider usable?" check ──────────────────────────────
#
# This mirrors chat.py's `_get_provider_from_config` resolution path WITHOUT
# the gateway. If chat.py would happily complete a message right now, this
# returns (True, label). The dashboard reads `auth_ok` derived from this so
# it never claims "reconnect required" while Chat is happily streaming.

async def _has_alternative_provider(db: AsyncSession) -> tuple[bool, str]:
    """
    Return (has_provider, human_label).

    Order matches chat.py:
      1. Active ProviderConfig with a usable credential (api_key OR session
         file present on disk for browser_session).
      2. Env-var fallback (OPENAI_API_KEY / ANTHROPIC_API_KEY / ...).
    """
    # 1. Active ProviderConfig?
    try:
        result = await db.execute(
            select(ProviderConfig).where(ProviderConfig.is_active == True)
        )
        pc = result.scalar_one_or_none()
        if pc:
            has_key = bool(pc.api_key_encrypted)
            has_session = bool(pc.session_file and Path(pc.session_file).exists())
            if has_key or has_session:
                label = pc.label or pc.provider
                if pc.model:
                    label = f"{label} · {pc.model}"
                return True, label
    except Exception as exc:
        _prov_log.warning("[gateway-status] active-provider lookup failed: %s", exc)

    # 2. Env-var fallback (same path chat.py uses).
    try:
        from services.providers.registry import get_fallback_provider  # local import to avoid cycles
        fb = get_fallback_provider()
        if fb:
            label = getattr(fb, "provider_id", "env")
            model = getattr(fb, "model", None)
            return True, f"env: {label}" + (f" · {model}" if model else "")
    except Exception as exc:
        _prov_log.warning("[gateway-status] env fallback lookup failed: %s", exc)

    return False, ""


# ── Gateway status ────────────────────────────────────────────────────────────

@router.get("/gateway-status")
async def gateway_status(db: AsyncSession = Depends(get_db)):
    """
    Check whether ANY usable provider is configured — gateway OR direct
    provider OR env-var fallback. Returns a structured shape the UI uses
    to decide between "Connected ✓" and "Re-authenticate".

    `auth_ok` is the single source of truth for the dashboard's banner.
    It is True iff Chat would be able to send a message right now.

    Returned shape:
      { gateway_reachable: bool, auth_ok: bool, status: str, detail: str,
        model: str|null, latency_ms: int|null, reauth_command: str,
        via: "gateway"|"provider"|"env"|"",
        provider_label: str|null }
    """
    import httpx, time

    gw = GatewayProxyProvider()
    base = gw.base_url
    token = gw.token
    reauth_cmd = "kapi models auth login --provider openai-codex"

    # Gather gateway probe results into locals; we'll pick a final shape
    # below depending on whether an alternative provider exists.
    gateway_reachable = False
    gateway_auth_ok   = False
    gateway_status    = "no_token"
    gateway_detail    = "No gateway token found in ~/.kapi/kapi.json"
    gateway_model     = None
    gateway_latency   = None

    if token:
        # 1) Health probe
        try:
            r = httpx.get(f"{base}/health", timeout=5.0)
            if r.status_code == 200:
                gateway_reachable = True
                # 2) Chat completions probe (checks actual LLM auth)
                try:
                    t0 = time.monotonic()
                    probe = httpx.post(
                        f"{base}/v1/chat/completions",
                        json={
                            "model": "kapi",
                            "messages": [{"role": "user", "content": "ping"}],
                            "max_tokens": 4,
                        },
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        },
                        timeout=20.0,
                    )
                    gateway_latency = round((time.monotonic() - t0) * 1000)
                    if probe.status_code == 200:
                        data = probe.json()
                        gateway_model   = data.get("model", "unknown")
                        gateway_auth_ok = True
                        gateway_status  = "healthy"
                        gateway_detail  = f"Gateway proxy working — model={gateway_model}"
                    else:
                        body = probe.text[:200]
                        auth_keywords = ["oauth", "refresh", "token", "auth", "expired", "unauthorized"]
                        is_auth = any(kw in body.lower() for kw in auth_keywords)
                        gateway_status = "auth_expired" if is_auth else "gateway_error"
                        gateway_detail = (
                            "OAuth tokens expired — re-authentication required"
                            if is_auth
                            else f"Gateway returned HTTP {probe.status_code}: {body}"
                        )
                except Exception as exc:
                    _prov_log.warning("[gateway-status] probe failed: %s", exc)
                    gateway_status = "probe_error"
                    gateway_detail = str(exc)[:200]
            else:
                gateway_status = "gateway_down"
                gateway_detail = f"Gateway health returned HTTP {r.status_code}"
        except httpx.ConnectError:
            gateway_status = "gateway_unreachable"
            gateway_detail = f"Cannot connect to gateway at {base}"

    # Gateway healthy → that's the truth.
    if gateway_auth_ok:
        return {
            "gateway_reachable": True,
            "auth_ok":           True,
            "status":            "healthy",
            "detail":            gateway_detail,
            "model":             gateway_model,
            "latency_ms":        gateway_latency,
            "reauth_command":    reauth_cmd,
            "via":               "gateway",
            "provider_label":    "Kapi Gateway (OpenAI Codex)",
        }

    # Gateway is not the path — but Chat may still work via an alternative
    # provider. Mirror chat.py's resolution to decide auth_ok.
    has_alt, alt_label = await _has_alternative_provider(db)
    if has_alt:
        return {
            "gateway_reachable": gateway_reachable,
            "auth_ok":           True,
            "status":            "healthy_via_provider",
            "detail":            f"Connected via {alt_label}",
            "model":             None,
            "latency_ms":        gateway_latency,
            "reauth_command":    reauth_cmd,
            "via":               "env" if alt_label.startswith("env:") else "provider",
            "provider_label":    alt_label,
        }

    # No working path at all — surface gateway diagnostics for the UI.
    return {
        "gateway_reachable": gateway_reachable,
        "auth_ok":           False,
        "status":            gateway_status,
        "detail":            gateway_detail,
        "model":             gateway_model,
        "latency_ms":        gateway_latency,
        "reauth_command":    reauth_cmd,
        "via":               "",
        "provider_label":    None,
    }


# ── Catalogue ─────────────────────────────────────────────────────────────────

@router.get("/catalogue")
async def get_catalogue():
    """Return available providers and their supported models/auth methods."""
    return PROVIDER_CATALOGUE


@router.post("/discover")
async def discover_providers():
    """
    OpenClaw-style: scan environment variables for API keys and auto-create
    provider configs.  Call this after setting ANTHROPIC_API_KEY / OPENAI_API_KEY
    to register providers without using the Settings UI.
    """
    touched = await auto_discover_providers()
    return {
        "discovered": touched,
        "message": (
            f"Discovered and registered: {', '.join(touched)}"
            if touched else
            "No new providers found in environment variables. "
            "Set ANTHROPIC_API_KEY or OPENAI_API_KEY in backend/.env and restart."
        ),
    }


# ── Quick Connect ─────────────────────────────────────────────────────────────

@router.post("/quick-connect", response_model=ProviderConfigOut)
async def quick_connect(
    body: QuickConnectRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    One-shot: write API key to .env, immediately activate it in the running
    process, then create (or update) a ProviderConfig and mark it active.

    Mirrors OpenClaw's "add key → instantly usable" flow — no restart required.
    """
    # Determine model from catalogue if not specified
    model = body.model
    if not model:
        for entry in PROVIDER_CATALOGUE:
            if entry["provider"] == body.provider:
                model = entry["default_model"]
                break
        if not model:
            raise HTTPException(400, f"Unknown provider: {body.provider}")

    # 1. Persist key to .env and apply to running process immediately
    write_api_key_to_env(body.provider, body.api_key)

    # 2. Deactivate all existing configs
    await db.execute(update(ProviderConfig).values(is_active=False))

    # 3. Check if a config for this provider already exists
    result = await db.execute(
        select(ProviderConfig).where(ProviderConfig.provider == body.provider)
    )
    existing = result.scalar_one_or_none()

    if existing:
        # Update the existing config
        existing.model        = model
        existing.auth_method  = "api_key"
        existing.api_key_encrypted = encrypt_key(body.api_key)
        existing.is_active    = True
        existing.last_used_at = datetime.utcnow()
        existing.last_error_at  = None
        existing.last_error_msg = None
        await db.commit()
        await db.refresh(existing)
        invalidate(existing.id)
        return _to_out(existing)

    # 4. Create new provider config
    config_id = str(uuid.uuid4())
    label_map = {
        "anthropic":      "Claude (Anthropic)",
        "openai":         "ChatGPT (OpenAI)",
        "gemini":         "Google Gemini",
        "mistral":        "Mistral AI",
        "xai":            "xAI Grok",
        "deepseek":       "DeepSeek",
        "ollama":         "Ollama (Local)",
        "openai_browser": "ChatGPT (Browser Session)",
    }
    pc = ProviderConfig(
        id=config_id,
        provider=body.provider,
        label=label_map.get(body.provider, body.provider.title()),
        model=model,
        auth_method="api_key",
        api_key_encrypted=encrypt_key(body.api_key),
        session_file="",
        is_active=True,
    )
    db.add(pc)
    await db.commit()
    await db.refresh(pc)
    return _to_out(pc)


# ── Token submit (browser session) ───────────────────────────────────────────

@router.post("/{config_id}/submit-token", response_model=ProviderTestResult)
async def submit_token(
    config_id: str,
    body: TokenSubmitRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Accept the access token the user extracted from their browser console
    (via CAPTURE_JS), save it to the session file, and persist expiry to DB.
    """
    result = await db.execute(select(ProviderConfig).where(ProviderConfig.id == config_id))
    pc = result.scalar_one_or_none()
    if not pc:
        raise HTTPException(404, "Provider config not found")
    if pc.auth_method != "browser_session":
        raise HTTPException(400, "This provider uses API key auth, not browser session")

    session_path = pc.session_file or str(settings.sessions_dir / f"{config_id}.json")
    provider = OpenAIBrowserProvider(session_file=Path(session_path), model=pc.model)
    outcome = provider.submit_session_token(body.token)

    if outcome["success"]:
        # Persist session file path + expiry back to DB
        pc.session_file = session_path
        pc.last_used_at = datetime.utcnow()
        pc.last_error_at  = None
        pc.last_error_msg = None
        expiry = provider.get_session_expiry()
        if expiry:
            pc.session_expires_at = expiry
        await db.commit()
        invalidate(config_id)

    return ProviderTestResult(
        success=outcome["success"],
        message=outcome["message"],
    )


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.post("/", response_model=ProviderConfigOut)
async def create_provider(
    body: ProviderConfigCreate,
    db: AsyncSession = Depends(get_db),
):
    config_id = str(uuid.uuid4())
    encrypted_key = encrypt_key(body.api_key or "") if body.api_key else ""
    session_file = str(settings.sessions_dir / f"{config_id}.json") if body.auth_method == "browser_session" else ""

    if body.make_active:
        await db.execute(update(ProviderConfig).values(is_active=False))

    pc = ProviderConfig(
        id=config_id,
        provider=body.provider,
        label=body.label,
        model=body.model,
        auth_method=body.auth_method,
        api_key_encrypted=encrypted_key,
        session_file=session_file,
        is_active=body.make_active,
    )
    db.add(pc)
    await db.commit()
    await db.refresh(pc)
    return _to_out(pc)


@router.get("/", response_model=list[ProviderConfigOut])
async def list_providers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ProviderConfig).order_by(ProviderConfig.created_at.desc()))
    return [_to_out(pc) for pc in result.scalars().all()]


@router.get("/active", response_model=ProviderConfigOut | None)
async def get_active_provider(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ProviderConfig).where(ProviderConfig.is_active == True))
    pc = result.scalar_one_or_none()
    return _to_out(pc) if pc else None


@router.post("/{config_id}/activate", response_model=ProviderConfigOut)
async def activate_provider(config_id: str, db: AsyncSession = Depends(get_db)):
    await db.execute(update(ProviderConfig).values(is_active=False))
    result = await db.execute(select(ProviderConfig).where(ProviderConfig.id == config_id))
    pc = result.scalar_one_or_none()
    if not pc:
        raise HTTPException(404, "Provider config not found")
    pc.is_active = True
    await db.commit()
    await db.refresh(pc)
    return _to_out(pc)


@router.post("/{config_id}/test", response_model=ProviderTestResult)
async def test_provider(config_id: str, db: AsyncSession = Depends(get_db)):
    import time
    result = await db.execute(select(ProviderConfig).where(ProviderConfig.id == config_id))
    pc = result.scalar_one_or_none()
    if not pc:
        raise HTTPException(404, "Provider config not found")

    try:
        t0 = time.monotonic()
        provider = get_provider(
            config_id=pc.id,
            provider=pc.provider,
            model=pc.model,
            auth_method=pc.auth_method,
            api_key_encrypted=pc.api_key_encrypted,
            session_file=pc.session_file,
        )
        ok, msg = await provider.health_check()
        latency = round((time.monotonic() - t0) * 1000, 1)

        if ok:
            pc.last_used_at = datetime.utcnow()
            # Clear any previous error on successful test
            pc.last_error_at = None
            pc.last_error_msg = None
            # For browser sessions, sync expiry from provider into DB
            if pc.auth_method == "browser_session" and hasattr(provider, "get_session_expiry"):
                expiry = provider.get_session_expiry()
                if expiry:
                    pc.session_expires_at = expiry
            await db.commit()

        return ProviderTestResult(success=ok, message=msg, latency_ms=latency)
    except Exception as exc:
        return ProviderTestResult(success=False, message=str(exc))


# ── Browser login ─────────────────────────────────────────────────────────────

@router.post("/{config_id}/browser-login")
async def start_browser_login(
    config_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Open chatgpt.com in the user's DEFAULT system browser (webbrowser.open).
    Returns immediately with the JS snippet the user runs in the browser console
    to extract their access token.  No Playwright, no Chromium, no blocking.
    """
    result = await db.execute(select(ProviderConfig).where(ProviderConfig.id == config_id))
    pc = result.scalar_one_or_none()
    if not pc:
        raise HTTPException(404, "Provider config not found")
    if pc.auth_method != "browser_session":
        raise HTTPException(400, "This provider config uses API key auth, not browser session")

    session_file = pc.session_file or str(settings.sessions_dir / f"{config_id}.json")
    provider = OpenAIBrowserProvider(session_file=Path(session_file), model=pc.model)

    # Opens browser + starts local callback server — returns immediately
    login_info = provider.launch_browser_login(config_id=config_id)

    auto_capture = login_info.get("auto_capture", False)
    _browser_tasks[config_id] = {
        "status":       "waiting_token",
        "message":      login_info["message"],
        "js_snippet":   login_info.get("js_snippet", ""),
        "steps":        login_info.get("steps", []),
        "browser_opened": login_info.get("browser_opened", False),
        "auto_capture": auto_capture,
    }

    return {
        "status":            "waiting_token",
        "auto_capture":      auto_capture,
        "message":           login_info["message"],
        "js_snippet":        login_info.get("js_snippet", ""),
        "js_snippet_manual": login_info.get("js_snippet_manual"),
        "steps":             login_info.get("steps", []),
        "browser_opened":    login_info.get("browser_opened", False),
        "callback_port":     login_info.get("callback_port"),
    }


@router.get("/{config_id}/browser-login/status")
async def get_browser_login_status(config_id: str, db: AsyncSession = Depends(get_db)):
    """
    Poll this endpoint to check if the auto-capture callback received the token.
    When status=="complete", the session is already saved — just refresh the UI.
    """
    from services.providers.openai_browser_provider import _CALLBACK_STATE

    # Check auto-capture state first (set by the callback server thread)
    callback = _CALLBACK_STATE.get(config_id)
    if callback and callback.get("status") == "complete":
        # Persist expiry to DB if we have it
        expiry_iso = callback.get("expires_at")
        if expiry_iso:
            try:
                result = await db.execute(select(ProviderConfig).where(ProviderConfig.id == config_id))
                pc = result.scalar_one_or_none()
                if pc:
                    pc.session_expires_at = datetime.fromisoformat(expiry_iso)
                    pc.last_used_at = datetime.utcnow()
                    pc.last_error_at = None
                    pc.last_error_msg = None
                    await db.commit()
                    invalidate(config_id)
            except Exception:
                pass
        return {
            "provider_config_id": config_id,
            "status":  "complete",
            "message": callback.get("message", "Session connected!"),
        }

    task = _browser_tasks.get(config_id)
    if not task:
        return {
            "provider_config_id": config_id,
            "status":  "not_started",
            "message": "No browser login in progress",
            "js_snippet": None,
            "steps": [],
        }
    return {
        "provider_config_id": config_id,
        "status":         task["status"],
        "message":        task["message"],
        "js_snippet":     task.get("js_snippet"),
        "steps":          task.get("steps", []),
        "browser_opened": task.get("browser_opened", False),
        "auto_capture":   task.get("auto_capture", False),
    }


# ── Silent session refresh ────────────────────────────────────────────────────

@router.post("/{config_id}/refresh-session", response_model=ProviderTestResult)
async def refresh_session(config_id: str, db: AsyncSession = Depends(get_db)):
    """
    Attempt a silent token refresh using persisted cookies — no browser window.
    Mirrors OpenClaw's refreshOAuthTokenWithLock(): try to renew the access token
    by replaying the stored cookies against the session endpoint.

    Returns success=True if a fresh token was obtained, with updated expiry in DB.
    Returns success=False if cookies have also expired (full re-login required).
    """
    result = await db.execute(select(ProviderConfig).where(ProviderConfig.id == config_id))
    pc = result.scalar_one_or_none()
    if not pc:
        raise HTTPException(404, "Provider config not found")
    if pc.auth_method != "browser_session":
        raise HTTPException(400, "Only browser_session providers support session refresh")

    if not pc.session_file or not Path(pc.session_file).exists():
        return ProviderTestResult(
            success=False,
            message="No session file found. Please do a full Browser Login first.",
        )

    provider = OpenAIBrowserProvider(
        session_file=Path(pc.session_file),
        model=pc.model,
    )
    provider._load_session()
    refreshed = await provider._refresh_access_token()

    if refreshed:
        expiry = provider.get_session_expiry()
        pc.last_used_at = datetime.utcnow()
        pc.last_error_at = None
        pc.last_error_msg = None
        if expiry:
            pc.session_expires_at = expiry
        await db.commit()
        invalidate(config_id)
        expiry_str = expiry.strftime("%Y-%m-%d %H:%M UTC") if expiry else "unknown"
        return ProviderTestResult(
            success=True,
            message=f"Session refreshed successfully. Expires: {expiry_str}",
        )
    else:
        return ProviderTestResult(
            success=False,
            message="Silent refresh failed — cookies may have expired. Please do a full Browser Login.",
        )


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{config_id}")
async def delete_provider(config_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ProviderConfig).where(ProviderConfig.id == config_id))
    pc = result.scalar_one_or_none()
    if not pc:
        raise HTTPException(404, "Provider config not found")
    if pc.session_file:
        Path(pc.session_file).unlink(missing_ok=True)
    invalidate(config_id)
    await db.delete(pc)
    await db.commit()
    return {"ok": True}
