"""
OpenAI provider — standard API key auth.
Supports GPT-4o, GPT-4 Turbo, GPT-3.5 Turbo, o1, o3-mini, etc.
"""
import time
from typing import AsyncIterator
import openai

from .base import BaseProvider, Message, CompletionResult


class OpenAIAPIProvider(BaseProvider):
    provider_id = "openai"
    label = "OpenAI (API Key)"

    SUPPORTED_MODELS = [
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4o",
        "gpt-4o-mini",
        "o3",
        "o3-mini",
        "o4-mini",
    ]

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.api_key = api_key
        self.model = model
        self._client = openai.AsyncOpenAI(api_key=api_key)

    def _build_messages(self, messages: list[Message], system: str) -> list[dict]:
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "system":
                continue  # already prepended above
            out.append({"role": m.role, "content": m.content})
        return out

    async def complete(
        self,
        messages: list[Message],
        system: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> CompletionResult:
        system_prompt = system or next(
            (m.content for m in messages if m.role == "system"), ""
        )
        api_msgs = self._build_messages(messages, system_prompt)

        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=api_msgs,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text = resp.choices[0].message.content or ""
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
        system_prompt = system or next(
            (m.content for m in messages if m.role == "system"), ""
        )
        api_msgs = self._build_messages(messages, system_prompt)

        stream_obj = await self._client.chat.completions.create(
            model=self.model,
            messages=api_msgs,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )
        # openai >=2.x returns an AsyncStream directly (no context-manager needed)
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
