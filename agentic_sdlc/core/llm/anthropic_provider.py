"""Anthropic provider. Structured output via a forced tool call whose input_schema is the agent's Pydantic schema."""

from __future__ import annotations

import os
from typing import Any

from ..errors import AgenticError, ConfigError, RetryableError
from . import LLMRequest, LLMResponse, register_provider


class AnthropicClient:
    name = "anthropic"

    def __init__(self, options: dict[str, Any]) -> None:
        try:
            import anthropic
        except ImportError as exc:  # optional dependency
            raise ConfigError("provider 'anthropic' needs `pip install anthropic`") from exc
        api_key = os.getenv(str(options.get("api_key_env", "ANTHROPIC_API_KEY")))
        if not api_key:
            raise ConfigError("provider 'anthropic' needs ANTHROPIC_API_KEY (or options.api_key_env)")
        self._sdk = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=float(options.get("timeout_s", 300)))
        self.fingerprint = "anthropic-v1"

    def complete(self, request: LLMRequest) -> LLMResponse:
        tool_name = f"submit_{request.schema_name}"
        try:
            message = self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                system=request.system,
                messages=[{"role": "user", "content": request.prompt}],
                tools=[{"name": tool_name, "description": f"Submit the {request.schema_name} result.",
                        "input_schema": request.json_schema}],
                tool_choice={"type": "tool", "name": tool_name},
            )
        except (self._sdk.RateLimitError, self._sdk.APIConnectionError, self._sdk.APITimeoutError,
                self._sdk.InternalServerError) as exc:
            raise RetryableError(f"anthropic transient error: {exc}") from exc
        except self._sdk.APIStatusError as exc:
            raise AgenticError(f"anthropic API error {exc.status_code}: {exc}") from exc

        if message.stop_reason == "max_tokens":
            raise RetryableError(f"output truncated at max_tokens={request.max_tokens}; raise the tier's max_tokens")
        for block in message.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                return LLMResponse(data=dict(block.input), provider=self.name, model=message.model,
                                   input_tokens=message.usage.input_tokens, output_tokens=message.usage.output_tokens)
        raise RetryableError("model did not return the structured tool call")


@register_provider("anthropic")
def _factory(options: dict[str, Any]) -> AnthropicClient:
    return AnthropicClient(options)
