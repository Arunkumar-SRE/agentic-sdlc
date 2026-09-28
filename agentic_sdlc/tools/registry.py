"""Decorator-registered tool registry. Capabilities are additive: import a module that uses @tool and it exists.

Every call is argument-validated (Pydantic, from the function signature), budgeted, audited and sandboxed.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError, create_model

from ..core.budget import BudgetTracker
from ..core.errors import AgenticError, ConfigError, ToolError
from ..core.sandbox import Sandbox

SideEffect = Literal["read", "write", "exec"]


@dataclass
class ToolEnv:
    """Injected as the first argument of every tool."""

    sandbox: Sandbox
    log: Any


@dataclass
class ToolSpec:
    name: str
    func: Callable[..., Any]
    description: str
    side_effects: SideEffect
    args_model: type[Any]


_TOOLS: dict[str, ToolSpec] = {}


def tool(name: str | None = None, *, side_effects: SideEffect = "read") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or func.__name__
        params = list(inspect.signature(func).parameters.values())[1:]  # skip env
        hints = inspect.get_annotations(func, eval_str=True)
        fields: dict[str, Any] = {
            p.name: (hints.get(p.name, Any), ... if p.default is inspect.Parameter.empty else p.default) for p in params
        }
        args_model = create_model(f"{tool_name}_args", **fields)
        if tool_name in _TOOLS and _TOOLS[tool_name].func is not func:
            raise ConfigError(f"tool '{tool_name}' registered twice")
        _TOOLS[tool_name] = ToolSpec(tool_name, func, (func.__doc__ or "").strip().split("\n")[0], side_effects, args_model)
        return func

    return decorator


def registered_tools() -> dict[str, ToolSpec]:
    load_builtin_tools()
    return dict(_TOOLS)


def load_builtin_tools() -> None:
    from . import codebase, contract, fs, quality, report  # noqa: F401  (registration side effect)


class ToolRegistry:
    def __init__(self, env: ToolEnv, budget: BudgetTracker, allowlist: dict[str, set[str]] | None = None) -> None:
        load_builtin_tools()
        self.env = env
        self.budget = budget
        self.allowlist = allowlist or {}  # agent id -> tools it may call (least privilege)

    def call(self, name: str, args: dict[str, Any], agent_id: str | None = None) -> Any:
        if name not in _TOOLS:
            raise ConfigError(f"unknown tool '{name}' (registered: {', '.join(sorted(_TOOLS))})")
        if agent_id is not None and agent_id in self.allowlist and name not in self.allowlist[agent_id]:
            raise ConfigError(f"agent '{agent_id}' is not allowed to call tool '{name}'")
        spec = _TOOLS[name]
        try:
            parsed = spec.args_model.model_validate(args)
        except ValidationError as exc:
            raise ToolError(f"bad arguments for tool '{name}': {exc}") from exc
        self.budget.reserve_tool_call()
        started = time.perf_counter()
        try:
            result = spec.func(self.env, **{k: getattr(parsed, k) for k in type(parsed).model_fields})
        except AgenticError:
            raise
        except Exception as exc:  # noqa: BLE001 - tools are a trust boundary
            raise ToolError(f"tool '{name}' failed: {exc!r}") from exc
        finally:
            self.env.log.info("tool_call", tool=name, agent=agent_id, side_effects=spec.side_effects,
                              ms=round((time.perf_counter() - started) * 1000, 1))
        return result
