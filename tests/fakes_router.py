"""Copie de tests/fakes.py d'Atelier Nexus (fournisseurs LLM scriptés, aucun appel réseau)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.core.llm.providers.base import LLMProvider
from app.core.llm.router import LLMRouter
from app.core.llm.types import (
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    ProviderName,
    ResponseComplete,
    TextBlock,
    TextDelta,
    ToolUseBlock,
    Usage,
)


def text_response(text: str, provider: ProviderName = "anthropic") -> LLMResponse:
    return LLMResponse(
        provider=provider,
        model=f"{provider}-model",
        content=[TextBlock(text=text)],
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def tool_response(
    name: str,
    tool_input: dict[str, Any],
    *,
    call_id: str = "call_1",
    text: str = "",
    provider: ProviderName = "anthropic",
) -> LLMResponse:
    content: list[TextBlock | ToolUseBlock] = [TextBlock(text=text)] if text else []
    content.append(ToolUseBlock(id=call_id, name=name, input=tool_input))
    return LLMResponse(
        provider=provider,
        model=f"{provider}-model",
        content=list(content),
        stop_reason="tool_use",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


class ScriptedProvider(LLMProvider):
    """Rejoue une liste de réponses (ou d'exceptions) dans l'ordre."""

    def __init__(
        self,
        name: ProviderName,
        script: list[LLMResponse | Exception] | None = None,
        *,
        configured: bool = True,
        models: list[str] | None = None,
    ) -> None:
        super().__init__(f"{name}-model")
        self.name = name  # type: ignore[misc]
        self.script = list(script or [])
        self.configured = configured
        self.models = models if models is not None else [f"{name}-model"]
        self.requests: list[LLMRequest] = []

    @property
    def is_configured(self) -> bool:
        return self.configured

    def _next(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.script:
            raise AssertionError(f"{self.name} : script épuisé")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return self._next(request)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        response = self._next(request)
        for word in response.text.split(" ") if response.text else []:
            yield TextDelta(text=word + " ")
        yield ResponseComplete(response=response)

    async def list_models(self) -> list[str]:
        return self.models


def make_router(
    anthropic: ScriptedProvider | None = None,
    ollama: ScriptedProvider | None = None,
    *,
    default: ProviderName = "anthropic",
    fallback: ProviderName | None = None,
) -> LLMRouter:
    return LLMRouter(
        {
            "anthropic": anthropic or ScriptedProvider("anthropic"),
            "ollama": ollama or ScriptedProvider("ollama"),
        },
        default=default,
        fallback=fallback,
    )
