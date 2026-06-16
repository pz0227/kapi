"""
Gateway Proxy Provider — routes LLM calls through the Kapi Gateway.

Instead of the analytics backend managing its own API keys / OAuth sessions,
this provider calls the Gateway's OpenAI-compatible /v1/chat/completions
endpoint, piggy-backing on whatever provider the Gateway already has configured
(API key, OAuth, browser session, etc.).

The gateway token is read from ~/.kapi/kapi.json  →  gateway.auth.token.
"""
import json
import logging
import time
from pathlib import Path
from typing import AsyncIterator

import httpx

from .base import BaseProvider, Message, CompletionResult

log = logging.getLogger(__name__)

_GATEWAY_URL = "http://127.0.0.1:18789"


def _read_gateway_token() -> str:
    """Read gateway auth token from kapi.json."""
    paths = [
        Path.home() / ".kapi" / "kapi.json",
        Path.home() / ".config" / "kapi" / "kapi.json",
    ]
    for p in paths:
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
                token = cfg.get("gateway", {}).get("auth", {}).get("token", "")
                if token:
                    return token
            except Exception:
                pass
    return ""


def _read_gateway_port() -> int:
    """Read gateway port from kapi.json (default 18789)."""
    paths = [
        Path.home() / ".kapi" / "kapi.json",
        Path.home() / ".config" / "kapi" / "kapi.json",
    ]
    for p in paths:
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
                port = cfg.get("gateway", {}).get("port")
                if port and isinstance(port, int):
                    return port
            except Exception:
                pass
    return 18789


class GatewayProxyProvider(BaseProvider):
    """
    Proxy LLM calls through the Kapi Gateway's /v1/chat/completions endpoint.
    Uses the same provider & model that the user configured in Kapi Chat.
    """

    provider_id = "gateway_proxy"
    label = "Kapi Gateway (shared provider)"

    def __init__(self, model: str = ""):
        self.token = _read_gateway_token()
        port = _read_gateway_port()
        self.base_url = f"http://127.0.0.1:{port}"
        self.model = model  # empty = let gateway pick its default

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _build_payload(
        self,
        messages: list[Message],
        system: str,
        max_tokens: int,
        temperature: float,
        stream: bool = False,
    ) -> dict:
        api_msgs: list[dict] = []
        if system:
            api_msgs.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "system":
                continue
            api_msgs.append({"role": m.role, "content": m.content})

        payload: dict = {
            "messages": api_msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if self.model:
            payload["model"] = self.model
        return payload

    @staticmethod
    def _is_scopes_error(body: str) -> bool:
        """Check if error response is a missing scopes / Responses API permission issue."""
        lower = body.lower()
        return ("missing scopes" in lower or "insufficient permissions" in lower
                or "api.responses" in lower)

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
        payload = self._build_payload(messages, system_prompt, max_tokens, temperature)
        url = f"{self.base_url}/v1/chat/completions"

        async with httpx.AsyncClient(timeout=120.0) as client:
            t0 = time.monotonic()
            resp = await client.post(url, json=payload, headers=self._headers())

            # Scopes error → retry with gpt-4.1 (Chat Completions API, no special scopes)
            if resp.status_code != 200 and self._is_scopes_error(resp.text):
                log.warning("[gateway_proxy] Scopes error — retrying with model=gpt-4.1")
                payload["model"] = "gpt-4.1"
                resp = await client.post(url, json=payload, headers=self._headers())

            latency = round((time.monotonic() - t0) * 1000)

            if resp.status_code != 200:
                body = resp.text[:300]
                log.error("[gateway_proxy] %s %s: %s", resp.status_code, url, body)
                raise RuntimeError(
                    f"Gateway returned {resp.status_code}: {body}"
                )

            data = resp.json()
            choice = data.get("choices", [{}])[0]
            text = choice.get("message", {}).get("content", "")
            usage = data.get("usage", {})
            model_used = data.get("model", self.model or "gateway-default")

            log.info(
                "[gateway_proxy] OK — %dms — model=%s tokens=%d+%d",
                latency,
                model_used,
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )

            return CompletionResult(
                text=text,
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                model=model_used,
                provider="gateway_proxy",
            )

    async def _iter_sse(self, resp) -> AsyncIterator[str]:
        """Parse SSE stream and yield content deltas."""
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                delta = (
                    chunk.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content", "")
                )
                if delta:
                    yield delta
            except json.JSONDecodeError:
                continue

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
        payload = self._build_payload(
            messages, system_prompt, max_tokens, temperature, stream=True
        )
        url = f"{self.base_url}/v1/chat/completions"

        async with httpx.AsyncClient(timeout=120.0) as client:
            # First attempt — non-streaming probe to detect scopes error
            # (SSE streams don't return clean error bodies)
            probe_payload = dict(payload, stream=False, max_tokens=1)
            probe = await client.post(url, json=probe_payload, headers=self._headers())
            if probe.status_code != 200 and self._is_scopes_error(probe.text):
                log.warning("[gateway_proxy] Stream scopes error — retrying with gpt-4.1")
                payload["model"] = "gpt-4.1"

            async with client.stream(
                "POST", url, json=payload, headers=self._headers()
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise RuntimeError(
                        f"Gateway returned {resp.status_code}: {body.decode()[:300]}"
                    )

                async for delta in self._iter_sse(resp):
                    yield delta

    async def health_check(self) -> tuple[bool, str]:
        """Check gateway is reachable and chat completions endpoint is active."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # First check gateway health
                r = await client.get(
                    f"{self.base_url}/health", headers=self._headers()
                )
                if r.status_code != 200:
                    return False, f"Gateway not reachable (HTTP {r.status_code})"

                # Then test chat completions
                t0 = time.monotonic()
                payload = self._build_payload(
                    [Message(role="user", content="ping")], "", 8, 0.0
                )
                r2 = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    headers=self._headers(),
                )
                latency = round((time.monotonic() - t0) * 1000)
                if r2.status_code == 200:
                    model = r2.json().get("model", "unknown")
                    return True, f"OK — {latency}ms — {model}"
                else:
                    return False, f"Chat completions returned {r2.status_code}: {r2.text[:100]}"
        except httpx.ConnectError:
            return False, "Gateway not running on port 18789"
        except Exception as exc:
            return False, str(exc)
