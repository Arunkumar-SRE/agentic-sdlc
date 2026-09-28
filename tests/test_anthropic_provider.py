"""The Anthropic provider against a stand-in SDK: request shape (forced tool call with the schema) and error mapping."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from agentic_sdlc.core.errors import AgenticError, ConfigError, RetryableError
from agentic_sdlc.core.llm import LLMRequest


class _Err(Exception):
    def __init__(self, msg: str = "", status_code: int = 500) -> None:
        super().__init__(msg)
        self.status_code = status_code


def _fake_sdk(behaviour: Any) -> types.ModuleType:
    sdk = types.ModuleType("anthropic")
    for name in ("RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError", "APIStatusError"):
        setattr(sdk, name, type(name, (_Err,), {}))
    sdk.RateLimitError.__bases__ = (sdk.APIStatusError,)  # type: ignore[attr-defined]
    sent: dict[str, Any] = {}

    class Messages:
        def create(self, **kwargs: Any) -> Any:
            sent.update(kwargs)
            return behaviour(sdk, kwargs)

    class Anthropic:
        def __init__(self, **kwargs: Any) -> None:
            self.messages = Messages()

    sdk.Anthropic = Anthropic  # type: ignore[attr-defined]
    sdk.sent = sent  # type: ignore[attr-defined]
    return sdk


def _reply(sdk: types.ModuleType, kwargs: dict[str, Any], stop: str = "tool_use") -> Any:
    block = types.SimpleNamespace(type="tool_use", name=kwargs["tool_choice"]["name"], input={"text": "hi"})
    return types.SimpleNamespace(content=[block], stop_reason=stop, model=kwargs["model"],
                                 usage=types.SimpleNamespace(input_tokens=12, output_tokens=3))


REQ = LLMRequest(system="sys", prompt="p", schema_name="Note", json_schema={"type": "object"}, model="claude-x", max_tokens=100)


def _client(monkeypatch: pytest.MonkeyPatch, behaviour: Any) -> Any:
    sdk = _fake_sdk(behaviour)
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from agentic_sdlc.core.llm.anthropic_provider import AnthropicClient
    return AnthropicClient({}), sdk


def test_structured_output_is_a_forced_tool_call_with_the_schema(monkeypatch):
    client, sdk = _client(monkeypatch, _reply)
    resp = client.complete(REQ)
    assert resp.data == {"text": "hi"} and resp.input_tokens == 12
    assert sdk.sent["tool_choice"] == {"type": "tool", "name": "submit_Note"}
    assert sdk.sent["tools"][0]["input_schema"] == {"type": "object"} and sdk.sent["system"] == "sys"


def test_rate_limit_is_retryable_and_bad_request_is_not(monkeypatch):
    def limited(sdk: Any, kw: Any) -> Any:
        raise sdk.RateLimitError("slow down", 429)
    client, _ = _client(monkeypatch, limited)
    with pytest.raises(RetryableError):
        client.complete(REQ)

    def bad(sdk: Any, kw: Any) -> Any:
        raise sdk.APIStatusError("bad", 400)
    client, _ = _client(monkeypatch, bad)
    with pytest.raises(AgenticError) as info:
        client.complete(REQ)
    assert not isinstance(info.value, RetryableError)


def test_truncated_output_is_retryable(monkeypatch):
    client, _ = _client(monkeypatch, lambda sdk, kw: _reply(sdk, kw, stop="max_tokens"))
    with pytest.raises(RetryableError, match="max_tokens"):
        client.complete(REQ)


def test_missing_api_key_is_a_config_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", _fake_sdk(_reply))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from agentic_sdlc.core.llm.anthropic_provider import AnthropicClient
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        AnthropicClient({})


def test_provider_override_maps_tiers_to_configured_models():
    from agentic_sdlc.core.budget import BudgetLimits, BudgetTracker
    from agentic_sdlc.core.cache import ResponseCache
    from agentic_sdlc.core.config import load_workflow
    from agentic_sdlc.core.llm import ModelRouter
    from pathlib import Path

    spec = load_workflow("sdlc")
    router = ModelRouter(spec.models, spec.providers, BudgetTracker(BudgetLimits()), ResponseCache(Path("."), False),
                         provider_override="offline")
    assert router.resolve("strong")[1].model == "offline-reference-v1"
    anthropic_models = spec.providers["anthropic"]["models"]
    assert set(anthropic_models) == set(spec.models)  # every tier has a model for the real provider
