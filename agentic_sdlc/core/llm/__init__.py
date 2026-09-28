"""LLMClient interface, provider registry, and the tier router agents talk to.

Agents never name a model. They ask for a tier ("fast", "standard", "strong"); the workflow YAML maps tiers to
(provider, model). Swapping vendors or models is a config change.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

from ..budget import BudgetTracker
from ..cache import ResponseCache
from ..config import ModelTier
from ..errors import ConfigError, OutputValidationError

T = TypeVar("T", bound=BaseModel)


class LLMRequest(BaseModel):
    system: str
    prompt: str
    schema_name: str
    json_schema: dict[str, Any]
    model: str
    max_tokens: int = 4096
    temperature: float = 0.0
    # The structured values the prompt was rendered from. Real providers ignore it; the offline
    # reference provider uses it so demos and tests run without a network or API key.
    context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    data: dict[str, Any]
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


@runtime_checkable
class LLMClient(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...


ProviderFactory = Callable[[dict[str, Any]], LLMClient]
_PROVIDERS: dict[str, ProviderFactory] = {}


def register_provider(name: str) -> Callable[[ProviderFactory], ProviderFactory]:
    def decorator(factory: ProviderFactory) -> ProviderFactory:
        _PROVIDERS[name] = factory
        return factory

    return decorator


def available_providers() -> list[str]:
    _load_builtin()
    return sorted(_PROVIDERS)


def create_provider(name: str, options: dict[str, Any]) -> LLMClient:
    _load_builtin()
    if name not in _PROVIDERS:
        raise ConfigError(f"unknown LLM provider '{name}' (known: {', '.join(sorted(_PROVIDERS))})")
    return _PROVIDERS[name](options)


def _load_builtin() -> None:
    from . import anthropic_provider, offline_provider  # noqa: F401  (registration side effect)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class ModelRouter:
    """Resolves tier -> provider/model, enforces budget, consults the cache, validates output against the schema."""

    def __init__(
        self,
        tiers: dict[str, ModelTier],
        provider_options: dict[str, dict[str, Any]],
        budget: BudgetTracker,
        cache: ResponseCache,
        provider_override: str | None = None,
        model_override: str | None = None,
    ) -> None:
        self.tiers = tiers
        self.budget = budget
        self.cache = cache
        self.provider_override = provider_override
        self.model_override = model_override
        self._options = provider_options
        self._clients: dict[str, LLMClient] = {}
        self._lock = threading.Lock()

    def resolve(self, tier: str) -> tuple[LLMClient, ModelTier]:
        if tier not in self.tiers:
            raise ConfigError(f"unknown model tier '{tier}'")
        spec = self.tiers[tier]
        provider = self.provider_override or spec.provider
        if provider != spec.provider:
            # Overriding the provider: take the model from that provider's tier map if configured.
            mapped = self._options.get(provider, {}).get("models", {}).get(tier)
            spec = spec.model_copy(update={"provider": provider, "model": mapped or spec.model})
        if self.model_override:
            spec = spec.model_copy(update={"model": self.model_override})
        with self._lock:
            if provider not in self._clients:
                self._clients[provider] = create_provider(provider, dict(self._options.get(provider, {})))
        return self._clients[provider], spec

    def structured(
        self,
        tier: str,
        system: str,
        prompt: str,
        schema: type[T],
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[T, dict[str, Any]]:
        client, spec = self.resolve(tier)
        request = LLMRequest(
            system=system,
            prompt=prompt,
            schema_name=schema.__name__,
            json_schema=schema.model_json_schema(),
            model=spec.model,
            max_tokens=spec.max_tokens,
            temperature=spec.temperature,
            context=context or {},
            metadata=metadata or {},
        )
        key = ResponseCache.key(provider=client.name, fingerprint=getattr(client, "fingerprint", ""), model=spec.model,
                                system=system, prompt=prompt, schema=request.json_schema)
        cached = self.cache.get(key)
        if cached is not None:
            try:
                self.budget.cache_hit()
                return schema.model_validate(cached), {"cached": True, "provider": client.name, "model": spec.model}
            except ValidationError:
                pass  # schema changed since it was cached: treat as a miss

        self.budget.reserve_llm_call()
        response = client.complete(request)
        self.budget.charge_tokens(
            response.input_tokens or estimate_tokens(system + prompt),
            response.output_tokens or estimate_tokens(json.dumps(response.data)),
        )
        try:
            result = schema.model_validate(response.data)
        except ValidationError as exc:
            # Never trust model output unvalidated. The engine retries and feeds this message back.
            raise OutputValidationError(f"{schema.__name__} validation failed: {_short(exc)}") from exc
        self.cache.put(key, result.model_dump(mode="json"))
        info = {"cached": False, "provider": response.provider, "model": response.model,
                "input_tokens": response.input_tokens, "output_tokens": response.output_tokens}
        return result, info


def _short(exc: ValidationError, limit: int = 6) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()[:limit]]
    return "; ".join(parts)
