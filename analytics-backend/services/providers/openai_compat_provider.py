"""
Generic OpenAI-compatible provider.

Used for Mistral, XAI/Grok, DeepSeek, and any other service that speaks the
OpenAI Chat Completions API at a custom base URL.  Each is just an instance of
this class with a different base_url and model list.
"""
import time
from typing import AsyncIterator

import openai

from .base import BaseProvider, Message, CompletionResult


class OpenAICompatProvider(BaseProvider):
    """
    Configurable provider for any OpenAI-compat endpoint.

    Subclasses set class-level defaults; callers can also instantiate directly.
    """
    provider_id = "openai_compat"
    label       = "OpenAI-Compatible"
    SUPPORTED_MODELS: list[str] = []
    DEFAULT_BASE_URL: str = ""

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        self.api_key  = api_key
        self.model    = model
        self._client  = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or self.DEFAULT_BASE_URL,
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
            t0 = time.monotonic()
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=8,
            )
            latency = round((time.monotonic() - t0) * 1000)
            return True, f"OK — {latency}ms — {resp.model}"
        except openai.AuthenticationError:
            return False, "Invalid API key"
        except openai.RateLimitError:
            return False, "Rate limit reached"
        except Exception as exc:
            return False, str(exc)


# ── Concrete subclasses with fixed endpoints ──────────────────────────────────

class MistralProvider(OpenAICompatProvider):
    provider_id      = "mistral"
    label            = "Mistral AI"
    DEFAULT_BASE_URL = "https://api.mistral.ai/v1"
    SUPPORTED_MODELS = [
        "mistral-large-latest",
        "mistral-small-latest",
        "codestral-latest",
        "devstral-medium-latest",
        "mistral-medium-latest",
    ]

    def __init__(self, api_key: str, model: str = "mistral-large-latest"):
        super().__init__(api_key=api_key, model=model, base_url=self.DEFAULT_BASE_URL)


class XAIProvider(OpenAICompatProvider):
    provider_id      = "xai"
    label            = "xAI Grok"
    DEFAULT_BASE_URL = "https://api.x.ai/v1"
    SUPPORTED_MODELS = [
        "grok-4",
        "grok-3",
        "grok-3-mini",
        "grok-3-fast",
        "grok-2",
    ]

    def __init__(self, api_key: str, model: str = "grok-3"):
        super().__init__(api_key=api_key, model=model, base_url=self.DEFAULT_BASE_URL)


class DeepSeekProvider(OpenAICompatProvider):
    provider_id      = "deepseek"
    label            = "DeepSeek"
    DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
    SUPPORTED_MODELS = [
        "deepseek-chat",
        "deepseek-reasoner",
    ]

    def __init__(self, api_key: str, model: str = "deepseek-chat"):
        super().__init__(api_key=api_key, model=model, base_url=self.DEFAULT_BASE_URL)
