"""The model seam: a small Protocol the loop depends on, and the Anthropic implementation."""

import time
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

from src.agent.pricing import PRICES, UnknownModelError, cost_usd

DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_MAX_TOKENS = 16_000
EFFORT = "medium"
_EPHEMERAL = {"type": "ephemeral"}


class ModelError(RuntimeError):
    """The model call failed (the SDK already retried). The message is safe to log."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass(frozen=True)
class ModelResponse:
    """`content` is the assistant message as plain dicts, ready to append to the history."""

    content: list[dict[str, Any]]
    stop_reason: str | None
    usage: Usage = Usage()
    cost_usd: float = 0.0
    latency_ms: int = 0


class ModelClient(Protocol):
    model: str

    def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        timeout_s: float,
    ) -> ModelResponse:
        """One model turn, abandoned after `timeout_s`. Raises `ModelError` on failure."""
        ...


def _with_message_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of `messages` with a cache breakpoint on the last block of the last message.

    The caller's list and dicts are left alone; only the path to the marked block is copied.
    """
    if not messages:
        return []
    *head, last = messages
    content = last["content"]
    if isinstance(content, str):
        blocks: list[dict[str, Any]] = [{"type": "text", "text": content}]
    else:
        blocks = [dict(b) for b in content]
    if not blocks:
        return [*head, last]
    blocks[-1]["cache_control"] = dict(_EPHEMERAL)
    return [*head, {**last, "content": blocks}]


class AnthropicModelClient:
    """Haiku 5.5 by default. `temperature` is never sent: the API rejects non-default sampling."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        client: anthropic.Anthropic | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        if model not in PRICES:
            raise UnknownModelError(
                f"no price for model {model!r}; supported: {', '.join(sorted(PRICES))}"
            )
        self.model = model
        self.max_tokens = max_tokens
        self._client = client if client is not None else anthropic.Anthropic()

    def build_request(
        self,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """The exact kwargs for `messages.create`. Pure, so the request shape is testable offline.

        Three of the four allowed cache breakpoints: last tool, system, last conversation block.
        """
        cached_tools = [dict(t) for t in tools]
        if cached_tools:
            cached_tools[-1]["cache_control"] = dict(_EPHEMERAL)
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": dict(_EPHEMERAL)}],
            "tools": cached_tools,
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            "output_config": {"effort": EFFORT},
            "messages": _with_message_breakpoint(messages),
        }

    def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        timeout_s: float,
    ) -> ModelResponse:
        request = self.build_request(system, tools, messages)
        started = time.perf_counter()
        try:
            response = self._client.messages.create(**request, timeout=timeout_s)
        except anthropic.APIStatusError as e:
            raise ModelError(f"{type(e).__name__} (status {e.status_code})") from None
        except anthropic.APIError as e:
            raise ModelError(type(e).__name__) from None
        latency_ms = int((time.perf_counter() - started) * 1000)

        u = response.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
            cache_read_tokens=u.cache_read_input_tokens or 0,
        )
        return ModelResponse(
            content=[b.model_dump(mode="json", exclude_none=True) for b in response.content],
            stop_reason=response.stop_reason,
            usage=usage,
            cost_usd=cost_usd(
                self.model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_write_tokens,
                usage.cache_read_tokens,
            ),
            latency_ms=latency_ms,
        )
