"""
Ollama provider — local or remote inference, no API key required.

Auto-discovers available models from the Ollama REST API.
Communicates via Ollama's OpenAI-compatible endpoint at /v1.
"""
import time
from typing import AsyncIterator

import httpx
import openai

from .base import BaseProvider, Message, CompletionResult


DEFAULT_BASE = "http://localhost:11434"


async def discover_ollama_models(base_url: str = DEFAULT_BASE) -> list[str]:
    """Return list of locally available model names from Ollama."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                return [m["name"] for m in data.get("models", [])]
    except Exception:
        pass
    return []


class OllamaProvider(BaseProvider):
    provider_id = "ollama"
    label       = "Ollama (Local)"

    # Static fallback list; actual list comes from discover_ollama_models()
    SUPPORTED_MODELS = [
        "llama3.3",
        "llama3.2",
        "llama3.1",
        "mistral",
        "gemma3",
        "qwen2.5",
        "phi4",
        "deepseek-r1",
    ]

    def __init__(self, base_url: str = DEFAULT_BASE, model: str = "llama3.3", api_key: str = "ollama"):
        self.base_url = base_url.rstrip("/")
        self.model    = model
        self._client  = openai.AsyncOpenAI(
            api_key=api_key or "ollama",
            base_url=f"{self.base_url}/v1",
        )

    def _build_messages(self, messages: list[Message], system: str) -> list[dict]:
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "system":
                continue
            out.append({"role": m.role, "content": m.content})
        return out

    async def complete(
        self,
        messages: list[Message],
        system: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> CompletionResult:
        system_prompt = system or next((m.content for m in messages if m.role == "system"), "")
        api_msgs = self._build_messages(messages, system_prompt)
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=api_msgs,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text  = resp.choices[0].message.content or ""
        usage = resp.usage
        return CompletionResult(
            text=text,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=self.model,
            provider=self.provider_id,
        )

    async def stream(
        self,
        messages: list[Message],
        system: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        system_prompt = system or next((m.content for m in messages if m.role == "system"), "")
        api_msgs = self._build_messages(messages, system_prompt)
        stream_obj = await self._client.chat.completions.create(
            model=self.model,
            messages=api_msgs,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )
        async for chunk in stream_obj:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def health_check(self) -> tuple[bool, str]:
        try:
            models = await discover_ollama_models(self.base_url)
            if not models:
                return False, f"Ollama reachable at {self.base_url} but no models found. Run: ollama pull llama3"
            if self.model not in models:
                return False, f"Model '{self.model}' not found. Available: {', '.join(models[:5])}"
            t0 = time.monotonic()
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=8,
            )
            latency = round((time.monotonic() - t0) * 1000)
            return True, f"OK — {latency}ms — {self.model} ({len(models)} models available)"
        except Exception as exc:
            err = str(exc)
            if "connection" in err.lower() or "connect" in err.lower():
                return False, f"Cannot reach Ollama at {self.base_url} — is it running?"
            return False, err
