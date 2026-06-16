"""
Google Gemini provider.

Uses the native google-generativeai SDK with a graceful fallback to the
OpenAI-compatible endpoint (https://generativelanguage.googleapis.com/v1beta/openai/)
so it works with the same openai client if google-generativeai isn't installed.
"""
import time
from typing import AsyncIterator

from .base import BaseProvider, Message, CompletionResult


class GeminiProvider(BaseProvider):
    provider_id = "gemini"
    label       = "Google Gemini"

    SUPPORTED_MODELS = [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ]

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.api_key = api_key
        self.model   = model
        # Use OpenAI-compat endpoint — works without google-generativeai SDK
        import openai
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=self.BASE_URL,
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
        except Exception as exc:
            err = str(exc)
            if "api_key" in err.lower() or "401" in err or "invalid" in err.lower():
                return False, "Invalid API key"
            return False, err
