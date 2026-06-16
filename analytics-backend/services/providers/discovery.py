"""
Provider auto-discovery — mirrors OpenClaw's resolveImplicitProviders().

At startup (and on demand via POST /providers/discover), Kapi checks environment
variables and settings for API keys, then auto-creates or updates provider configs
in the DB so that:

    ANTHROPIC_API_KEY=sk-ant-...   →  Anthropic Claude provider, active
    OPENAI_API_KEY=sk-...          →  OpenAI provider, active if no Anthropic

This is the exact pattern OpenClaw uses: env vars take priority, and you never
need to touch the Settings UI to get a working LLM connection.
"""
import logging
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from core.config import get_settings
from core.database import AsyncSessionLocal, ProviderConfig
from services.providers.registry import has_env_key, encrypt_key, PROVIDER_CATALOGUE

log = logging.getLogger(__name__)
settings = get_settings()


async def auto_discover_providers() -> list[str]:
    """
    Inspect env vars and settings for API keys.  For each key found:
      - If no provider config exists for that provider → create one and mark active
      - If a provider config exists with a fake/empty key → update it to env-discovered
      - If a valid config already exists → leave it alone

    Returns list of provider names that were created or updated.
    """
    touched: list[str] = []

    async with AsyncSessionLocal() as db:
        for entry in PROVIDER_CATALOGUE:
            provider = entry["provider"]
            if not has_env_key(provider):
                continue  # no env key for this provider

            # Check existing configs for this provider
            result = await db.execute(
                select(ProviderConfig).where(ProviderConfig.provider == provider)
            )
            existing = result.scalars().all()

            # Determine if any existing config has a valid (non-fake) key
            has_good_config = any(
                pc.api_key_encrypted and len(pc.api_key_encrypted) > 30
                for pc in existing
            )

            if has_good_config:
                log.debug("[discovery] %s already has a stored key — skipping", provider)
                continue

            # No good config — create one from the env key, or update the bad one
            env_key = _read_env_key(provider)
            if not env_key:
                continue

            # Deactivate all other providers before making this one active
            # (only if there's no currently active provider at all)
            active_result = await db.execute(
                select(ProviderConfig).where(ProviderConfig.is_active == True)
            )
            any_active = active_result.scalar_one_or_none() is not None

            if existing:
                # Update the first existing record
                pc = existing[0]
                pc.api_key_encrypted = encrypt_key(env_key)
                if not any_active:
                    pc.is_active = True
                pc.last_error_at = None
                pc.last_error_msg = None
                log.info("[discovery] Updated %s provider config from env var", provider)
            else:
                # Create fresh config
                if not any_active:
                    # deactivate everything
                    await db.execute(update(ProviderConfig).values(is_active=False))

                pc = ProviderConfig(
                    id=str(uuid.uuid4()),
                    provider=provider,
                    label=entry["label"] + " (env)",
                    model=entry["default_model"],
                    auth_method="api_key",
                    api_key_encrypted=encrypt_key(env_key),
                    session_file="",
                    is_active=not any_active,
                    created_at=datetime.utcnow(),
                )
                db.add(pc)
                log.info("[discovery] Created %s provider config from env var (active=%s)",
                         provider, pc.is_active)

            touched.append(provider)

        if touched:
            await db.commit()
            log.info("[discovery] Auto-discovered providers: %s", touched)
        else:
            log.debug("[discovery] No new providers discovered from env vars")

    return touched


def _read_env_key(provider: str) -> str:
    """Read the first valid API key for provider from env or settings."""
    import os
    env_map = {
        "anthropic": ["ANTHROPIC_API_KEY", "ANTHROPIC_API_KEYS"],
        "openai":    ["OPENAI_API_KEY", "OPENAI_API_KEYS"],
        "gemini":    ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "mistral":   ["MISTRAL_API_KEY"],
        "xai":       ["XAI_API_KEY", "GROK_API_KEY"],
        "deepseek":  ["DEEPSEEK_API_KEY"],
    }
    for var in env_map.get(provider, []):
        val = os.environ.get(var, "").strip()
        if val:
            return val.split(",")[0].strip()
    # Fall back to pydantic-settings
    settings_map = {
        "anthropic": settings.anthropic_api_key,
        "openai":    settings.openai_api_key,
        "gemini":    getattr(settings, "gemini_api_key", ""),
        "mistral":   getattr(settings, "mistral_api_key", ""),
        "xai":       getattr(settings, "xai_api_key", ""),
        "deepseek":  getattr(settings, "deepseek_api_key", ""),
    }
    return settings_map.get(provider, "").strip()
