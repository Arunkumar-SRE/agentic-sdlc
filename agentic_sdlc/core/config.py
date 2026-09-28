"""Workflow configuration. Everything another team would change lives in YAML and is validated here."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped,unused-ignore]  # stubs: pip install types-PyYAML
from pydantic import BaseModel, Field, ValidationError, model_validator

from .budget import BudgetLimits
from .errors import ConfigError
from .graph import TaskGraph

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = PACKAGE_ROOT / "workflows"
PROMPTS_DIR = PACKAGE_ROOT / "agents" / "prompts"
DEFAULT_SCHEMA_MODULE = "agentic_sdlc.agents.schemas"


class RetryPolicy(BaseModel):
    max_attempts: int = Field(3, ge=1, le=10)
    backoff_s: float = Field(0.5, ge=0)
    multiplier: float = Field(2.0, ge=1)
    max_backoff_s: float = Field(8.0, ge=0)

    def delay(self, attempt: int) -> float:
        return min(self.backoff_s * self.multiplier ** max(attempt - 1, 0), self.max_backoff_s)


class GateSpec(BaseModel):
    """Human approval after a step. `min_confidence` escalates low-confidence output even in unattended runs."""

    required: bool = True
    min_confidence: float | None = None
    confidence_field: str = "confidence"
    summary_field: str | None = None


class Condition(BaseModel):
    """Run the step only if `<step>.<field>` is (or is not) one of `in_`. Absent output counts as no match."""

    path: str
    in_: list[Any] = Field(alias="in")
    negate: bool = False


class FeedbackLoop(BaseModel):
    """Cross-step coordination: if `<this step>.<pass_field>` is falsy, re-open `target` with this step's feedback."""

    target: str
    pass_field: str = "passed"
    feedback_field: str = "feedback"
    skip_field: str | None = None  # if truthy in the output, the failure is not repairable upstream (e.g. environment)
    max_rounds: int = Field(2, ge=1, le=5)


class AgentSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["llm", "tool"] = "llm"
    description: str = ""
    prompt: str | None = None  # path under agents/prompts
    output_schema: str
    tier: str = "standard"
    depends_on: list[str] = Field(default_factory=list)
    context_tools: dict[str, dict[str, Any]] = Field(default_factory=dict)  # llm: tool results rendered into the prompt
    tool: str | None = None  # tool agents: the single tool they call
    args: dict[str, Any] = Field(default_factory=dict)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    on_failure: Literal["halt", "continue", "fallback"] = "halt"
    fallback: dict[str, Any] | None = None
    when: Condition | None = None
    gate: GateSpec | None = None
    feedback: FeedbackLoop | None = None

    @model_validator(mode="after")
    def _check_kind(self) -> AgentSpec:
        if self.kind == "llm" and not self.prompt:
            raise ValueError(f"llm agent '{self.id}' needs a prompt")
        if self.kind == "tool" and not self.tool:
            raise ValueError(f"tool agent '{self.id}' needs a tool")
        if self.on_failure == "fallback" and self.fallback is None:
            raise ValueError(f"agent '{self.id}' uses on_failure=fallback but defines no fallback output")
        return self


class ModelTier(BaseModel):
    provider: str
    model: str
    max_tokens: int = 4096
    temperature: float = 0.0


class WorkflowSpec(BaseModel):
    name: str
    version: str = "1"
    description: str = ""
    engine: str = "simple"
    max_parallel: int = Field(4, ge=1, le=32)
    plugins: list[str] = Field(default_factory=list)  # extra modules to import (tools, providers, engines)
    models: dict[str, ModelTier]
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    budgets: BudgetLimits = Field(default_factory=BudgetLimits)
    cache: bool = True
    system_prompt: str = "system.md"
    agents: list[AgentSpec]

    @model_validator(mode="after")
    def _check_graph(self) -> WorkflowSpec:
        ids = [a.id for a in self.agents]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate agent ids: {dupes}")
        graph = TaskGraph({a.id: a.depends_on for a in self.agents})
        for a in self.agents:
            if a.kind == "llm" and a.tier not in self.models:
                raise ValueError(f"agent '{a.id}' uses unknown model tier '{a.tier}'")
            if a.feedback and a.feedback.target not in graph.ancestors(a.id):
                raise ValueError(f"agent '{a.id}': feedback target '{a.feedback.target}' must be an upstream step")
            if a.when and a.when.path.split(".")[0] not in graph.ancestors(a.id) | {"inputs"}:
                raise ValueError(f"agent '{a.id}': condition must reference an upstream step or inputs")
        return self

    def agent(self, agent_id: str) -> AgentSpec:
        for a in self.agents:
            if a.id == agent_id:
                return a
        raise ConfigError(f"unknown agent '{agent_id}'")

    def graph(self) -> TaskGraph:
        return TaskGraph({a.id: a.depends_on for a in self.agents})


def resolve_workflow_path(name_or_path: str) -> Path:
    candidate = Path(name_or_path)
    if candidate.suffix in (".yaml", ".yml") and candidate.exists():
        return candidate
    builtin = WORKFLOWS_DIR / f"{name_or_path}.yaml"
    if builtin.exists():
        return builtin
    raise ConfigError(f"workflow not found: {name_or_path}")


def load_workflow(name_or_path: str) -> WorkflowSpec:
    path = resolve_workflow_path(name_or_path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        spec = WorkflowSpec.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid workflow {path}: {exc}") from exc
    for module in spec.plugins:
        importlib.import_module(module)
    search = [path.parent, path.parent / "prompts", PROMPTS_DIR]
    for agent in spec.agents:  # fail at load time, not mid-run
        resolve_schema(agent.output_schema)
        if agent.prompt:
            agent.prompt = str(find_prompt(agent.prompt, search))
    spec.system_prompt = str(find_prompt(spec.system_prompt, search))
    return spec


def find_prompt(relative: str, search: list[Path]) -> Path:
    """A team's own workflow can ship its prompts next to its YAML; built-in prompts are the fallback."""
    candidate = Path(relative)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    for directory in search:
        path = (directory / relative).resolve()
        if path.exists() and path.suffix in (".md", ".txt"):
            return path
    raise ConfigError(f"prompt template not found: {relative} (searched {', '.join(str(d) for d in search)})")


def resolve_schema(name: str) -> type[BaseModel]:
    module_name, _, attr = name.rpartition(".")
    module = importlib.import_module(module_name or DEFAULT_SCHEMA_MODULE)
    schema = getattr(module, attr, None)
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        raise ConfigError(f"output schema '{name}' is not a Pydantic model")
    return schema


def load_prompt(path_or_name: str) -> str:
    return find_prompt(path_or_name, [PROMPTS_DIR]).read_text(encoding="utf-8")


class Settings(BaseModel):
    """Per-invocation settings (CLI / env). Paths are relative to the current directory."""

    workspace_dir: Path = Path("workspace")
    logs_dir: Path = Path("logs")
    cache_dir: Path = Path(".cache/llm")
    provider_override: str | None = None
    model_override: str | None = None
    cache_enabled: bool = True
    max_llm_calls: int | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)
    faults: dict[str, int] = Field(default_factory=dict)  # step -> number of injected transient failures
