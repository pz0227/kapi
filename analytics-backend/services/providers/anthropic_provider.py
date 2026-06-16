"""
Anthropic Claude provider — API key auth.
"""
import time
from typing import AsyncIterator
import anthropic

from .base import BaseProvider, Message, CompletionResult


class AnthropicProvider(BaseProvider):
    provider_id = "anthropic"
    label = "Anthropic Claude"

    SUPPORTED_MODELS = [
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ]

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.api_key = api_key
        self.model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    def _build_messages(self, messages: list[Message]) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]

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
        api_msgs = self._build_messages(messages)

        kwargs: dict = dict(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=api_msgs,
        )
        if system_prompt:
            kwargs["system"] = system_prompt

        resp = await self._client.messages.create(**kwargs)
        text = resp.content[0].text if resp.content else ""
        return CompletionResult(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
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
        api_msgs = self._build_messages(messages)

        kwargs: dict = dict(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=api_msgs,
        )
        if system_prompt:
            kwargs["system"] = system_prompt

        async with self._client.messages.stream(**kwargs) as stream_ctx:
            async for chunk in stream_ctx.text_stream:
                yield chunk

    async def health_check(self) -> tuple[bool, str]:
        try:
            t0 = time.monotonic()
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=16,
                messages=[{"role": "user", "content": "ping"}],
            )
            latency = round((time.monotonic() - t0) * 1000)
            return True, f"OK — {latency}ms — {resp.model}"
        except anthropic.AuthenticationError:
            return False, "Invalid API key"
        except Exception as exc:
            return False, str(exc)
