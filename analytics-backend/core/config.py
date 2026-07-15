from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from functools import lru_cache
import os


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_name: str = "Kapi — AI Product Analyst"
    app_version: str = "1.0.1"
    debug: bool = False
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # Storage
    storage_dir: Path = BASE_DIR / "storage"
    db_path: Path = BASE_DIR / "storage" / "kapi.db"
    sessions_dir: Path = BASE_DIR / "storage" / "sessions"
    faiss_index_dir: Path = BASE_DIR / "storage" / "faiss"

    # Embedding model (local, no API required)
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dim: int = 384
    chunk_size: int = 512
    chunk_overlap: int = 64
    retrieval_top_k: int = 6
    # Max dataset rows converted into retrieval chunks. Rows beyond this are
    # not searchable via RAG. Surfaced to the UI as `indexed_rows` so
    # truncation is a disclosed limitation instead of a silent one.
    index_max_rows: int = 200

    # Provider defaults
    default_provider: str = "anthropic"
    default_model: str = "claude-sonnet-4-6"

    # API keys (populated from env or UI)
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    mistral_api_key: str = ""
    xai_api_key: str = ""
    deepseek_api_key: str = ""

    # Auth — "local" (no auth, self-hosted) or "clerk" (SaaS with Clerk JWT)
    auth_mode: str = "local"
    clerk_domain: str = ""          # e.g. "your-app.clerk.accounts.dev"
    clerk_secret_key: str = ""      # sk_live_... or sk_test_...
    clerk_publishable_key: str = "" # pk_live_... or pk_test_...

    # Stripe billing
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_starter_price_id: str = ""
    stripe_pro_price_id: str = ""
    stripe_team_price_id: str = ""

    # SaaS settings
    app_url: str = "http://localhost:18789"  # public URL for redirects

    def ensure_dirs(self) -> None:
        for d in [self.storage_dir, self.sessions_dir, self.faiss_index_dir]:
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


_ENV_VAR_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "gemini":    "GEMINI_API_KEY",
    "mistral":   "MISTRAL_API_KEY",
    "xai":       "XAI_API_KEY",
    "deepseek":  "DEEPSEEK_API_KEY",
}


def write_api_key_to_env(provider: str, api_key: str) -> None:
    """
    Write an API key to backend/.env AND set it in the current process env.
    This mirrors OpenClaw's behaviour: once a key is written to .env it is
    immediately active — no restart required.
    """
    var = _ENV_VAR_MAP.get(provider)
    if not var:
        return

    env_file = BASE_DIR / ".env"
    lines: list[str] = []
    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()

    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{var}=") or line.startswith(f"# {var}="):
            lines[i] = f"{var}={api_key}"
            found = True
            break
    if not found:
        lines.append(f"{var}={api_key}")

    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Apply to running process immediately (no restart needed)
    os.environ[var] = api_key

    # Bust the settings cache so re-reads pick up the new key
    get_settings.cache_clear()
