"""
Provider abstraction layer.
Every provider implements BaseProvider so the chat/report services are model-agnostic.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Optional


@dataclass
class Message:
    role: str   # "user" | "assistant" | "system"
    content: str


@dataclass
class CompletionResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""


class BaseProvider(ABC):
    """Abstract product-analyst LLM provider."""

    provider_id: str = ""
    label: str = ""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        system: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> CompletionResult:
        """Non-streaming completion."""

    @abstractmethod
    async def stream(
        self,
        messages: list[Message],
        system: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        """Streaming completion — yields text chunks."""

    @abstractmethod
    async def health_check(self) -> tuple[bool, str]:
        """Return (ok, message)."""
