import copy
from types import SimpleNamespace
from typing import Any

import pytest

from src.agent.model import DEFAULT_MODEL, AnthropicModelClient
from src.agent.pricing import UnknownModelError, cost_usd

TOOLS = [
    {"name": "a", "description": "first", "input_schema": {"type": "object"}},
    {"name": "b", "description": "second", "input_schema": {"type": "object"}},
]
EPHEMERAL = {"type": "ephemeral"}


class _Block:
    def __init__(self, data: dict[str, Any]):
        self._data = data

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        return dict(self._data)


class _StubMessages:
    def __init__(self, response: SimpleNamespace):
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.kwargs = kwargs
        return self.response


def _response(**usage: int | None) -> SimpleNamespace:
    base = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    return SimpleNamespace(
        content=[_Block({"type": "text", "text": "hi"})],
        stop_reason="end_turn",
        usage=SimpleNamespace(**{**base, **usage}),
    )


def _client(response: SimpleNamespace | None = None) -> tuple[AnthropicModelClient, _StubMessages]:
    stub = _StubMessages(response or _response())
    sdk = SimpleNamespace(messages=stub)
    return AnthropicModelClient(client=sdk), stub  # type: ignore[arg-type]


def test_defaults_and_fixed_request_options():
    client, stub = _client()
    client.complete(
        system="sys", tools=TOOLS, messages=[{"role": "user", "content": "go"}], timeout_s=30
    )
    kw = stub.kwargs
    assert kw["model"] == DEFAULT_MODEL == "claude-haiku-5-5"
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert "thinking" not in kw


def test_no_sampling_params_are_sent():
    client, stub = _client()
    client.complete(
        system="s", tools=TOOLS, messages=[{"role": "user", "content": "x"}], timeout_s=1
    )
    for key in ("temperature", "top_p", "top_k"):
        assert key not in stub.kwargs


def test_timeout_is_passed_to_the_request():
    client, stub = _client()
    client.complete(
        system="s", tools=TOOLS, messages=[{"role": "user", "content": "x"}], timeout_s=42.5
    )
    assert stub.kwargs["timeout"] == 42.5


def test_cache_breakpoints_on_last_tool_system_and_last_message_block():
    client, _ = _client()
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "one"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "two"},
            ],
        },
    ]
    req = client.build_request("sys", TOOLS, messages)

    assert "cache_control" not in req["tools"][0]
    assert req["tools"][-1]["cache_control"] == EPHEMERAL
    assert req["system"] == [{"type": "text", "text": "sys", "cache_control": EPHEMERAL}]
    # only the last block of the last message carries the moving breakpoint
    assert req["messages"][0] == messages[0]
    assert "cache_control" not in req["messages"][1]["content"][0]
    last = req["messages"][2]["content"]
    assert "cache_control" not in last[0]
    assert last[1]["cache_control"] == EPHEMERAL


def test_string_content_becomes_a_text_block_with_the_breakpoint():
    client, _ = _client()
    req = client.build_request("s", TOOLS, [{"role": "user", "content": "go"}])
    assert req["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "go", "cache_control": EPHEMERAL}]}
    ]


def test_build_request_does_not_mutate_its_inputs():
    client, _ = _client()
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "r"}]},
    ]
    before_messages, before_tools = copy.deepcopy(messages), copy.deepcopy(TOOLS)
    client.build_request("sys", TOOLS, messages)
    assert messages == before_messages
    assert TOOLS == before_tools


def test_breakpoint_moves_with_the_conversation():
    client, _ = _client()
    history = [{"role": "user", "content": [{"type": "text", "text": "a"}]}]
    first = client.build_request("s", TOOLS, history)
    history += [
        {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
        {"role": "user", "content": [{"type": "text", "text": "c"}]},
    ]
    second = client.build_request("s", TOOLS, history)
    assert "cache_control" in first["messages"][-1]["content"][-1]
    assert "cache_control" not in second["messages"][0]["content"][-1]
    assert "cache_control" in second["messages"][-1]["content"][-1]


def test_cost_uses_cache_read_and_write_tokens():
    response = _response(
        input_tokens=1000,
        output_tokens=200,
        cache_creation_input_tokens=5000,
        cache_read_input_tokens=20000,
    )
    client, _ = _client(response)
    out = client.complete(
        system="s", tools=TOOLS, messages=[{"role": "user", "content": "x"}], timeout_s=1
    )
    expected = (1000 * 0.10 + 5000 * 0.125 + 20000 * 0.01 + 200 * 0.50) / 1_000_000
    assert out.cost_usd == pytest.approx(expected)
    assert out.cost_usd == pytest.approx(cost_usd("claude-haiku-5-5", 1000, 200, 5000, 20000))
    assert out.usage.cache_write_tokens == 5000
    assert out.usage.cache_read_tokens == 20000


def test_none_cache_counts_are_treated_as_zero():
    response = _response(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
    )
    client, _ = _client(response)
    out = client.complete(
        system="s", tools=TOOLS, messages=[{"role": "user", "content": "x"}], timeout_s=1
    )
    assert out.usage.cache_write_tokens == out.usage.cache_read_tokens == 0
    assert out.content == [{"type": "text", "text": "hi"}]
    assert out.stop_reason == "end_turn"


def test_unknown_model_is_rejected_up_front():
    with pytest.raises(UnknownModelError):
        AnthropicModelClient("claude-not-priced", client=SimpleNamespace())  # type: ignore[arg-type]
