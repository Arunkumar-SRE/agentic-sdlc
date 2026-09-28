"""The Agent protocol and the two generic agent kinds that YAML entries are built into.

The orchestrator knows exactly one thing about an agent: `run(state, ctx) -> BaseModel`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from .config import AgentSpec, load_prompt, resolve_schema
from .context import RunContext
from .errors import ConfigError, OutputValidationError
from .state import WorkflowState
from .templating import render, resolve


@runtime_checkable
class Agent(Protocol):
    id: str

    def run(self, state: WorkflowState, ctx: RunContext) -> BaseModel: ...


def template_context(spec: AgentSpec, state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    """What a prompt or tool-argument template may reference. Only upstream outputs are visible."""
    upstream = ctx.workflow.graph().ancestors(spec.id)
    return {
        "requirement": state.requirement,
        "inputs": state.inputs,
        "outputs": {k: v for k, v in state.outputs.items() if k in upstream},
        "feedback": "\n".join(f"- {f}" for f in state.feedback.get(spec.id, [])) or "none",
        "run": {
            "run_id": state.run_id,
            "workflow": state.workflow,
            "steps": {k: v.model_dump(mode="json") for k, v in state.steps.items()},
            "approvals": [a.model_dump(mode="json") for a in state.approvals],
            "feedback": state.feedback,
            "events": [e.model_dump(mode="json") for e in state.events
                       if e.kind in ("retry", "fallback", "feedback_loop", "feedback_exhausted", "feedback_skipped", "fault_injected", "escalated")],
            "usage": ctx.budget.snapshot(),
        },
        "workspace": str(ctx.sandbox.root),
    }


class LLMAgent:
    """Renders its prompt template, asks the configured model tier for output matching its schema."""

    def __init__(self, spec: AgentSpec) -> None:
        self.id = spec.id
        self.spec = spec
        self.schema = resolve_schema(spec.output_schema)
        self.template = load_prompt(spec.prompt or "")

    def run(self, state: WorkflowState, ctx: RunContext) -> BaseModel:
        values = template_context(self.spec, state, ctx)
        tool_results: dict[str, Any] = {}
        for name, args in self.spec.context_tools.items():
            tool_results[name] = ctx.tools.call(name, resolve(args, values), agent_id=self.id)
        values["tools"] = tool_results
        prompt = render(self.template, values)
        result, info = ctx.llm.structured(
            self.spec.tier, ctx.system_prompt, prompt, self.schema,
            context=values, metadata={"agent": self.id, "round": state.steps[self.id].rounds if self.id in state.steps else 0},
        )
        ctx.extras.setdefault("llm_info", {})[self.id] = info
        return result


class ToolAgent:
    """Calls one registered tool with templated arguments and validates the result against its schema."""

    def __init__(self, spec: AgentSpec) -> None:
        self.id = spec.id
        self.spec = spec
        self.schema = resolve_schema(spec.output_schema)

    def run(self, state: WorkflowState, ctx: RunContext) -> BaseModel:
        args = resolve(self.spec.args, template_context(self.spec, state, ctx))
        raw = ctx.tools.call(self.spec.tool or "", args, agent_id=self.id)
        try:
            return self.schema.model_validate(raw)
        except ValidationError as exc:
            raise OutputValidationError(f"tool '{self.spec.tool}' returned data not matching {self.schema.__name__}: {exc}") from exc


AgentFactory = Callable[[AgentSpec], Agent]
AGENT_KINDS: dict[str, AgentFactory] = {"llm": LLMAgent, "tool": ToolAgent}


def build_agent(spec: AgentSpec) -> Agent:
    if spec.kind not in AGENT_KINDS:
        raise ConfigError(f"unknown agent kind '{spec.kind}'")
    return AGENT_KINDS[spec.kind](spec)
