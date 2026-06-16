"""
Provider registry — resolves a ProviderConfig DB record to a live BaseProvider instance.

Key pattern mirrors OpenClaw's provider resolution priority:
  1. Environment variable API key (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)
  2. .env file (loaded via pydantic-settings in core/config.py)
  3. Encrypted key stored in DB (fallback)

This means: if the user sets ANTHROPIC_API_KEY in their environment or backend/.env,
it works immediately without any UI configuration — same as OpenClaw.
"""
import base64
import os
from pathlib import Path

from core.config import get_settings
from .base import BaseProvider
from .anthropic_provider import AnthropicProvider
from .openai_api_provider import OpenAIAPIProvider
from .openai_browser_provider import OpenAIBrowserProvider
from .gemini_provider import GeminiProvider
from .openai_compat_provider import MistralProvider, XAIProvider, DeepSeekProvider
from .ollama_provider import OllamaProvider
from .gateway_proxy_provider import GatewayProxyProvider

settings = get_settings()

# In-memory cache keyed by provider_config.id + key fingerprint
_CACHE: dict[str, BaseProvider] = {}


# ── Key resolution (OpenClaw-style: env first, then file, then DB) ────────────

def _resolve_api_key(provider: str, api_key_encrypted: str) -> str:
    """
    Resolve the effective API key for a provider.

    Priority (mirrors OpenClaw's resolveEnvApiKey / normalizeProviders):
      1. Environment variable from process environment
      2. Key from backend/.env (via pydantic-settings)
      3. Encrypted key stored in the DB row
    """
    env_map = {
        "anthropic":      ["ANTHROPIC_API_KEY", "ANTHROPIC_API_KEYS"],
        "openai":         ["OPENAI_API_KEY", "OPENAI_API_KEYS"],
        "openai_browser": [],
        "gemini":         ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_API_KEY"],
        "mistral":        ["MISTRAL_API_KEY"],
        "xai":            ["XAI_API_KEY", "GROK_API_KEY"],
        "deepseek":       ["DEEPSEEK_API_KEY"],
        "ollama":         [],  # no API key needed
    }
    for env_var in env_map.get(provider, []):
        val = os.environ.get(env_var, "").strip()
        if val:
            return val.split(",")[0].strip()

    settings_map = {
        "anthropic": getattr(settings, "anthropic_api_key", ""),
        "openai":    getattr(settings, "openai_api_key", ""),
        "gemini":    getattr(settings, "gemini_api_key", ""),
        "mistral":   getattr(settings, "mistral_api_key", ""),
        "xai":       getattr(settings, "xai_api_key", ""),
        "deepseek":  getattr(settings, "deepseek_api_key", ""),
    }
    settings_key = settings_map.get(provider, "").strip()
    if settings_key:
        return settings_key

    return _decrypt(api_key_encrypted)


def _decrypt(encrypted: str) -> str:
    """Base64 decode of DB-stored key. Not cryptographically strong — same as before."""
    if not encrypted:
        return ""
    try:
        return base64.b64decode(encrypted.encode()).decode()
    except Exception:
        return ""


def _encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return base64.b64encode(plaintext.encode()).decode()


def encrypt_key(plaintext: str) -> str:
    return _encrypt(plaintext)


# ── Provider factory ──────────────────────────────────────────────────────────

def get_provider(config_id: str, provider: str, model: str, auth_method: str,
                 api_key_encrypted: str, session_file: str) -> BaseProvider:
    """
    Return a cached or newly-constructed provider.

    For API-key providers, key resolution follows OpenClaw priority:
      env var > .env file > DB stored key
    """
    api_key = _resolve_api_key(provider, api_key_encrypted)
    cache_key = f"{config_id}:{api_key[:8]}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    inst: BaseProvider
    if provider == "anthropic":
        inst = AnthropicProvider(api_key=api_key, model=model)
    elif provider == "openai":
        inst = OpenAIAPIProvider(api_key=api_key, model=model)
    elif provider == "openai_browser":
        sf = Path(session_file) if session_file else settings.sessions_dir / f"{config_id}.json"
        inst = OpenAIBrowserProvider(session_file=sf, model=model)
    elif provider == "gemini":
        inst = GeminiProvider(api_key=api_key, model=model)
    elif provider == "mistral":
        inst = MistralProvider(api_key=api_key, model=model)
    elif provider == "xai":
        inst = XAIProvider(api_key=api_key, model=model)
    elif provider == "deepseek":
        inst = DeepSeekProvider(api_key=api_key, model=model)
    elif provider == "ollama":
        # For Ollama, the "api_key" field stores the base URL (no real key needed)
        base_url = _decrypt(api_key_encrypted) or "http://localhost:11434"
        inst = OllamaProvider(base_url=base_url, model=model)
    else:
        raise ValueError(f"Unknown provider: {provider}")

    _CACHE[cache_key] = inst
    return inst


def get_fallback_provider() -> BaseProvider | None:
    """
    Create a provider instance WITHOUT needing a DB-stored ProviderConfig record.

    Resolution order:
      1. Gateway proxy — piggy-back on Kapi Gateway's configured provider
         (works with OAuth, API keys, browser sessions — whatever the user set up)
      2. Environment variable API keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
      3. None — no provider available
    """
    import logging
    log = logging.getLogger(__name__)

    # 1) Try gateway proxy first — this uses whatever provider Kapi Chat uses
    try:
        gw = GatewayProxyProvider()
        if gw.token:
            import httpx
            # Quick check: is the gateway reachable AND can it actually complete LLM requests?
            try:
                r = httpx.get(f"{gw.base_url}/health", timeout=3.0)
                if r.status_code != 200:
                    log.debug("[fallback] Gateway not reachable (HTTP %s)", r.status_code)
                    raise Exception("gateway not reachable")
                # Verify chat completions endpoint actually works with a tiny probe
                probe = httpx.post(
                    f"{gw.base_url}/v1/chat/completions",
                    json={"model": "kapi", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4},
                    headers={"Authorization": f"Bearer {gw.token}", "Content-Type": "application/json"},
                    timeout=15.0,
                )
                if probe.status_code == 200:
                    log.info("[fallback] Using Kapi Gateway proxy provider (verified)")
                    return gw
                else:
                    body = probe.text[:200]
                    log.warning("[fallback] Gateway chat completions returned %s: %s — falling through", probe.status_code, body)
            except httpx.ConnectError:
                log.debug("[fallback] Gateway not reachable, trying env vars")
            except Exception as e:
                log.debug("[fallback] Gateway probe failed: %s — trying env vars", e)
    except Exception as e:
        log.debug("[fallback] Gateway proxy init failed: %s", e)

    # 2) Direct API key fallback
    # OpenAI
    for env_var in ("OPENAI_API_KEY", "OPENAI_API_KEYS"):
        val = os.environ.get(env_var, "").strip()
        if val:
            key = val.split(",")[0].strip()
            if len(key) > 10:
                return OpenAIAPIProvider(api_key=key, model="gpt-5.4")
    if getattr(settings, "openai_api_key", "").strip():
        return OpenAIAPIProvider(api_key=settings.openai_api_key.strip(), model="gpt-5.4")

    # Anthropic
    for env_var in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEYS"):
        val = os.environ.get(env_var, "").strip()
        if val:
            key = val.split(",")[0].strip()
            if len(key) > 10:
                return AnthropicProvider(api_key=key, model="claude-sonnet-4-6")
    if getattr(settings, "anthropic_api_key", "").strip():
        return AnthropicProvider(api_key=settings.anthropic_api_key.strip(), model="claude-sonnet-4-6")

    # Gemini
    for env_var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_API_KEY"):
        val = os.environ.get(env_var, "").strip()
        if val:
            key = val.split(",")[0].strip()
            if len(key) > 10:
                return GeminiProvider(api_key=key, model="gemini-2.5-flash")
    if getattr(settings, "gemini_api_key", "").strip():
        return GeminiProvider(api_key=settings.gemini_api_key.strip(), model="gemini-2.5-flash")

    return None


def has_env_key(provider: str) -> bool:
    """Return True if a working API key is available for this provider via env/settings."""
    key = _resolve_api_key(provider, "")
    return bool(key and len(key) > 10)


def invalidate(config_id: str) -> None:
    """Evict all cache entries for a given config (call after key update)."""
    for k in list(_CACHE.keys()):
        if k.startswith(f"{config_id}:"):
            del _CACHE[k]


def invalidate_all() -> None:
    """Clear entire cache — call after env vars change."""
    _CACHE.clear()


# ── Catalogue of available providers shown in the UI ─────────────────────────

PROVIDER_CATALOGUE = [
    {
        "provider":      "anthropic",
        "label":         "Anthropic Claude",
        "auth_methods":  ["api_key"],
        "models":        AnthropicProvider.SUPPORTED_MODELS,
        "default_model": "claude-sonnet-4-6",
        "docs_url":      "https://console.anthropic.com/",
        "env_var":       "ANTHROPIC_API_KEY",
        "note":          "Best reasoning & analysis. Get your key at console.anthropic.com",
    },
    {
        "provider":      "openai",
        "label":         "OpenAI",
        "auth_methods":  ["api_key"],
        "models":        OpenAIAPIProvider.SUPPORTED_MODELS,
        "default_model": "gpt-5.4",
        "docs_url":      "https://platform.openai.com/api-keys",
        "env_var":       "OPENAI_API_KEY",
        "note":          "GPT-5.4, o3, o4-mini. Get your key at platform.openai.com",
    },
    {
        "provider":      "openai_browser",
        "label":         "ChatGPT (Browser Session)",
        "auth_methods":  ["browser_session"],
        "models":        OpenAIBrowserProvider.SUPPORTED_MODELS,
        "default_model": "gpt-5.4",
        "docs_url":      None,
        "env_var":       None,
        "note": (
            "No API key needed — uses your logged-in ChatGPT session. "
            "Opens chatgpt.com, run the JS snippet, Kapi auto-captures the token."
        ),
    },
    {
        "provider":      "gemini",
        "label":         "Google Gemini",
        "auth_methods":  ["api_key"],
        "models":        GeminiProvider.SUPPORTED_MODELS,
        "default_model": "gemini-2.5-flash",
        "docs_url":      "https://aistudio.google.com/app/apikey",
        "env_var":       "GEMINI_API_KEY",
        "note":          "Gemini 2.5 Pro & Flash. Get your key at aistudio.google.com",
    },
    {
        "provider":      "mistral",
        "label":         "Mistral AI",
        "auth_methods":  ["api_key"],
        "models":        MistralProvider.SUPPORTED_MODELS,
        "default_model": "mistral-large-latest",
        "docs_url":      "https://console.mistral.ai/",
        "env_var":       "MISTRAL_API_KEY",
        "note":          "Mistral Large, Small, Codestral. Get your key at console.mistral.ai",
    },
    {
        "provider":      "xai",
        "label":         "xAI Grok",
        "auth_methods":  ["api_key"],
        "models":        XAIProvider.SUPPORTED_MODELS,
        "default_model": "grok-3",
        "docs_url":      "https://console.x.ai/",
        "env_var":       "XAI_API_KEY",
        "note":          "Grok-4, Grok-3, Grok-3-mini. Get your key at console.x.ai",
    },
    {
        "provider":      "deepseek",
        "label":         "DeepSeek",
        "auth_methods":  ["api_key"],
        "models":        DeepSeekProvider.SUPPORTED_MODELS,
        "default_model": "deepseek-chat",
        "docs_url":      "https://platform.deepseek.com/",
        "env_var":       "DEEPSEEK_API_KEY",
        "note":          "DeepSeek-V3 & R1 Reasoner. Get your key at platform.deepseek.com",
    },
    {
        "provider":      "ollama",
        "label":         "Ollama (Local)",
        "auth_methods":  ["none"],
        "models":        OllamaProvider.SUPPORTED_MODELS,
        "default_model": "llama3.3",
        "docs_url":      "https://ollama.com/",
        "env_var":       None,
        "note":          "Run models locally — no API key needed. Install Ollama and pull a model.",
    },
]
