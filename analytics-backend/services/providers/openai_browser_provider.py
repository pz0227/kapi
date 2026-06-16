"""
OpenAI Browser-Session Provider.

How it works (auto-capture mode, mirroring OpenClaw's localhost callback):
  1. launch_browser_login() starts a local HTTP callback server on port 1455,
     then opens https://chatgpt.com in the user's DEFAULT browser.
  2. The CAPTURE_JS_AUTO snippet (shown in the UI) fetches the session from
     chatgpt.com and POSTs it to localhost:1455/token.
  3. The callback server receives the token, saves the session, and signals
     completion — no manual paste required.
  4. The frontend polls /providers/{id}/browser-login-status until it sees
     status="complete", then refreshes.

Manual fallback:
  If the user prefers, they can still paste the token via submit_session_token().

Session refresh:
  - Access token has a JWT exp field; we parse it to know when it expires.
  - _ensure_token() checks expiry and tries a cookie-based silent refresh
    before each completion call.
"""

import asyncio
import base64
import json
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx

from .base import BaseProvider, Message, CompletionResult


CHATGPT_SESSION_API = "https://chatgpt.com/api/auth/session"
LOGIN_URL           = "https://chatgpt.com"
CALLBACK_PORT       = 1455

# Proactively refresh if token expires within 15 min
REFRESH_THRESHOLD_SECS = 15 * 60

# JS that POSTs the token to the local callback server (auto-capture)
CAPTURE_JS_AUTO = (
    "fetch('/api/auth/session')"
    ".then(r=>r.json())"
    ".then(d=>{"
    "if(!d.accessToken){alert('Not logged in — please sign in to ChatGPT first.');return;}"
    "fetch('http://localhost:1455/token',{"
    "method:'POST',"
    "headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({token:d.accessToken})"
    "}).then(()=>alert('Kapi connected! You can close this tab.'));"
    "})"
)

# Manual fallback — shows token in a prompt for copy-paste
CAPTURE_JS_MANUAL = (
    "fetch('/api/auth/session')"
    ".then(r=>r.json())"
    ".then(d=>{if(d.accessToken){"
    "prompt('Copy your Kapi session token (Ctrl+A, Ctrl+C):', d.accessToken)"
    "}else{alert('Not logged in — please sign in to ChatGPT first.');}})"
)


class ProviderUnavailableError(RuntimeError):
    pass


def _parse_jwt_expiry(token: str) -> Optional[datetime]:
    """Decode JWT payload (no signature check) and return exp as UTC datetime."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        )
        exp = payload.get("exp")
        return datetime.fromtimestamp(int(exp), tz=timezone.utc).replace(tzinfo=None) if exp else None
    except Exception:
        return None


def _expiry_state(expires_at: Optional[datetime]) -> str:
    if expires_at is None:
        return "unknown"
    remaining = (expires_at - datetime.utcnow()).total_seconds()
    if remaining <= 0:
        return "expired"
    if remaining <= REFRESH_THRESHOLD_SECS:
        return "expiring_soon"
    return "valid"


# ── Callback server (one per process, shared across all browser-session providers) ──

# Keyed by config_id → {"status": "waiting"|"complete"|"error", "token": str, "message": str}
_CALLBACK_STATE: dict[str, dict] = {}
_SERVER_THREAD: Optional[threading.Thread] = None
_SERVER_INSTANCE: Optional[HTTPServer] = None


def _make_handler(on_token):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # suppress console spam

        def do_OPTIONS(self):
            self.send_response(200)
            self._cors()
            self.end_headers()

        def do_POST(self):
            if self.path != "/token":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                token = data.get("token", "").strip()
                if token:
                    on_token(token)
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                else:
                    self.send_response(400)
                    self._cors()
                    self.end_headers()
            except Exception:
                self.send_response(400)
                self._cors()
                self.end_headers()

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    return _Handler


def _start_callback_server(config_id: str, on_token) -> bool:
    """Start the shared callback HTTP server on localhost:1455 (if not already running)."""
    global _SERVER_THREAD, _SERVER_INSTANCE

    if _SERVER_THREAD and _SERVER_THREAD.is_alive():
        return True  # already running

    try:
        handler = _make_handler(on_token)
        server = HTTPServer(("localhost", CALLBACK_PORT), handler)
        _SERVER_INSTANCE = server

        def run():
            server.serve_forever()

        t = threading.Thread(target=run, daemon=True, name="kapi-oauth-callback")
        t.start()
        _SERVER_THREAD = t
        return True
    except OSError:
        # Port in use — still attempt (another Kapi process may be serving it)
        return False


def _stop_callback_server():
    global _SERVER_INSTANCE, _SERVER_THREAD
    if _SERVER_INSTANCE:
        try:
            _SERVER_INSTANCE.shutdown()
        except Exception:
            pass
        _SERVER_INSTANCE = None
    _SERVER_THREAD = None


class OpenAIBrowserProvider(BaseProvider):
    provider_id = "openai_browser"
    label       = "ChatGPT (Browser Session)"

    SUPPORTED_MODELS = ["gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-4.1", "gpt-4o", "o3", "o3-mini", "o4-mini"]

    def __init__(self, session_file: Path, model: str = "gpt-5.4"):
        self.session_file  = Path(session_file)
        self.model         = model
        self._access_token: Optional[str]      = None
        self._cookies:      dict               = {}
        self._expires_at:   Optional[datetime] = None

    # ── Session file ─────────────────────────────────────────────────────────

    def _load_session(self) -> bool:
        if not self.session_file.exists():
            return False
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
            self._cookies      = data.get("cookies", {})
            self._access_token = data.get("access_token")
            raw_exp = data.get("expires_at")
            self._expires_at = datetime.fromisoformat(raw_exp) if raw_exp else None
            if self._expires_at is None and self._access_token:
                self._expires_at = _parse_jwt_expiry(self._access_token)
            return bool(self._access_token)
        except Exception:
            return False

    def _save_session(self, token: str, cookies: Optional[dict] = None) -> None:
        if cookies is not None:
            self._cookies = cookies
        self._access_token = token
        self._expires_at   = _parse_jwt_expiry(token)
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(
            json.dumps({
                "cookies":      self._cookies,
                "access_token": token,
                "expires_at":   self._expires_at.isoformat() if self._expires_at else None,
            }, indent=2),
            encoding="utf-8",
        )

    def get_session_expiry(self) -> Optional[datetime]:
        if not self._expires_at and not self._access_token:
            self._load_session()
        return self._expires_at

    # ── Browser login with auto-capture callback ──────────────────────────────

    def launch_browser_login(self, config_id: str = "") -> dict:
        """
        Open chatgpt.com in the user's DEFAULT browser and start a local callback
        server on localhost:1455. The CAPTURE_JS_AUTO snippet POSTs the token back
        automatically — no manual paste required (mirrors OpenClaw's OAuth flow).

        Falls back to manual mode (CAPTURE_JS_MANUAL) if the server can't start.
        """
        _CALLBACK_STATE[config_id] = {"status": "waiting", "token": None, "message": "Waiting for login…"}

        def on_token(token: str):
            self._save_session(token)
            exp = self._expires_at
            msg = "Session connected!"
            if exp:
                msg += f" Expires: {exp.strftime('%Y-%m-%d %H:%M UTC')}"
            _CALLBACK_STATE[config_id] = {
                "status": "complete",
                "token": token[:16] + "…",
                "message": msg,
                "expires_at": exp.isoformat() if exp else None,
            }

        server_started = _start_callback_server(config_id, on_token)

        try:
            webbrowser.open(LOGIN_URL)
            browser_opened = True
        except Exception:
            browser_opened = False

        if server_started:
            return {
                "success":        False,
                "pending":        True,
                "auto_capture":   True,
                "browser_opened": browser_opened,
                "message": (
                    "ChatGPT opened in your browser. "
                    "Log in if needed, then paste the snippet below in the console — "
                    "Kapi will auto-capture the session."
                ),
                "steps": [
                    "1. Make sure you are logged into ChatGPT in the browser that just opened.",
                    "2. Press F12 to open DevTools → Console tab.",
                    "3. Paste the snippet below and press Enter.",
                    "4. You'll see \"Kapi connected!\" — come back here, you're done.",
                ],
                "js_snippet":        CAPTURE_JS_AUTO,
                "js_snippet_manual": CAPTURE_JS_MANUAL,
                "callback_port":     CALLBACK_PORT,
            }
        else:
            # Server couldn't start (port in use, etc.) — fall back to manual paste
            return {
                "success":        False,
                "pending":        True,
                "auto_capture":   False,
                "browser_opened": browser_opened,
                "message": (
                    "ChatGPT opened in your browser. "
                    "Use the manual snippet below to copy and paste your token."
                ),
                "steps": [
                    "1. Make sure you are logged into ChatGPT in the browser that just opened.",
                    "2. Press F12 to open DevTools → Console tab.",
                    "3. Paste the snippet below and press Enter.",
                    "4. A popup appears with your token — Ctrl+A, Ctrl+C to copy it.",
                    "5. Paste the token into the field below and click Activate.",
                ],
                "js_snippet":      CAPTURE_JS_MANUAL,
                "callback_port":   None,
            }

    def get_callback_status(self, config_id: str) -> dict:
        """Return the current auto-capture status for this config."""
        return _CALLBACK_STATE.get(config_id, {"status": "unknown", "message": "No login in progress."})

    def submit_session_token(self, token: str) -> dict:
        """
        Accept a manually pasted access token.
        Also used as the fallback when auto-capture isn't available.
        """
        token = token.strip()
        if not token:
            return {"success": False, "message": "Token is empty."}
        try:
            self._save_session(token)
            exp = self._expires_at
            msg = "Session saved."
            if exp:
                msg += f" Expires: {exp.strftime('%Y-%m-%d %H:%M UTC')}"
            return {"success": True, "message": msg, "expires_at": exp.isoformat() if exp else None}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    # ── Token refresh ─────────────────────────────────────────────────────────

    async def _refresh_access_token(self) -> bool:
        """Try to silently refresh using stored cookies."""
        try:
            async with httpx.AsyncClient(cookies=self._cookies, follow_redirects=True) as client:
                resp = await client.get(CHATGPT_SESSION_API, timeout=15)
                if resp.status_code == 200:
                    token = resp.json().get("accessToken", "")
                    if token:
                        self._save_session(token)
                        return True
        except Exception:
            pass
        return False

    async def _ensure_token(self) -> None:
        if not self._access_token:
            if not self._load_session():
                raise ProviderUnavailableError(
                    "No ChatGPT session. Go to Settings → ChatGPT Browser → Activate Session."
                )
        state = _expiry_state(self._expires_at)
        if state == "expired":
            if not await self._refresh_access_token():
                raise ProviderUnavailableError(
                    "ChatGPT session expired. Go to Settings → re-activate browser session."
                )
        elif state == "expiring_soon":
            try:
                await self._refresh_access_token()
            except Exception:
                pass

    # ── Completion ────────────────────────────────────────────────────────────

    async def complete(
        self,
        messages: list[Message],
        system: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> CompletionResult:
        await self._ensure_token()
        system_prompt = system or next((m.content for m in messages if m.role == "system"), "")
        try:
            import openai as _openai
            client = _openai.AsyncOpenAI(
                api_key="BROWSER_SESSION",
                base_url="https://chatgpt.com/backend-api",
                default_headers={"Authorization": f"Bearer {self._access_token}"},
            )
            msgs = []
            if system_prompt:
                msgs.append({"role": "system", "content": system_prompt})
            for m in messages:
                if m.role != "system":
                    msgs.append({"role": m.role, "content": m.content})
            resp = await client.chat.completions.create(model=self.model, messages=msgs, max_tokens=max_tokens)
            return CompletionResult(
                text=resp.choices[0].message.content or "",
                model=self.model,
                provider=self.provider_id,
            )
        except Exception as exc:
            raise ProviderUnavailableError(f"Browser session completion failed: {exc}.")

    async def stream(self, messages, system="", max_tokens=2048, temperature=0.3) -> AsyncIterator[str]:
        result = await self.complete(messages, system, max_tokens, temperature)
        for i, word in enumerate(result.text.split(" ")):
            yield word + ("" if i == len(result.text.split(" ")) - 1 else " ")
            await asyncio.sleep(0)

    async def health_check(self) -> tuple[bool, str]:
        try:
            await self._ensure_token()
            state = _expiry_state(self._expires_at)
            if state == "valid" and self._expires_at:
                h = round((self._expires_at - datetime.utcnow()).total_seconds() / 3600, 1)
                return True, f"Session active — expires in {h}h"
            if state == "expiring_soon" and self._expires_at:
                m = round((self._expires_at - datetime.utcnow()).total_seconds() / 60)
                return True, f"Session active — expiring in {m}min"
            return True, "Browser session active"
        except ProviderUnavailableError as exc:
            return False, str(exc)
