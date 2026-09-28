"""Engine interface. The runner picks an engine by the workflow's `engine:` key, so another executor
(a distributed queue, a durable-execution service) can be swapped in without touching agents or YAML."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..core.config import WorkflowSpec
from ..core.context import RunContext
from ..core.errors import ConfigError
from ..core.state import WorkflowState


class Engine(Protocol):
    name: str

    def execute(self, workflow: WorkflowSpec, state: WorkflowState, ctx: RunContext) -> WorkflowState: ...


EngineFactory = Callable[[], Engine]
_ENGINES: dict[str, EngineFactory] = {}


def register_engine(name: str) -> Callable[[EngineFactory], EngineFactory]:
    def decorator(factory: EngineFactory) -> EngineFactory:
        _ENGINES[name] = factory
        return factory

    return decorator


def get_engine(name: str) -> Engine:
    from . import simple  # noqa: F401  (registers the default engine)

    if name not in _ENGINES:
        raise ConfigError(f"unknown engine '{name}' (known: {', '.join(sorted(_ENGINES))})")
    return _ENGINES[name]()
