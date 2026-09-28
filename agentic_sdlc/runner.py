"""Wiring: YAML -> WorkflowSpec -> RunContext -> Engine. The only place that knows every component."""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from .core.budget import BudgetTracker
from .core.cache import ResponseCache
from .core.config import PACKAGE_ROOT, Settings, WorkflowSpec, load_prompt, load_workflow
from .core.context import RunContext
from .core.errors import ConfigError
from .core.gates import ApprovalPolicy, AutoApproval
from .core.llm import ModelRouter
from .core.observability import configure
from .core.sandbox import Sandbox
from .core.state import WorkflowState
from .engines.base import get_engine
from .tools.registry import ToolEnv, ToolRegistry

DEFAULT_TARGET = PACKAGE_ROOT.parent / "targets" / "url_shortener"


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def build_context(spec: WorkflowSpec, state: WorkflowState, settings: Settings, approvals: ApprovalPolicy,
                  verbose: bool = False) -> RunContext:
    log = configure(settings.logs_dir / f"{state.run_id}.jsonl", verbose).bind(run_id=state.run_id)
    read_roots = [Path(state.inputs["target_dir"])] if state.inputs.get("target_dir") else []
    sandbox = Sandbox(settings.workspace_dir / state.run_id, read_roots=read_roots)
    limits = spec.budgets.model_copy(update={"max_llm_calls": settings.max_llm_calls}) if settings.max_llm_calls else spec.budgets
    budget = BudgetTracker(limits, used={k: v for k, v in state.usage.items() if k != "limits"})
    provider_options: dict[str, dict[str, Any]] = {k: dict(v) for k, v in spec.providers.items()}
    for provider, extra in settings.provider_options.items():
        provider_options.setdefault(provider, {}).update(extra)
    router = ModelRouter(spec.models, provider_options, budget,
                         ResponseCache(settings.cache_dir, spec.cache and settings.cache_enabled),
                         settings.provider_override, settings.model_override)
    allowlist = {a.id: set(a.context_tools) | ({a.tool} if a.tool else set()) for a in spec.agents}
    tools = ToolRegistry(ToolEnv(sandbox=sandbox, log=log), budget, allowlist)
    return RunContext(run_id=state.run_id, workflow=spec, settings=settings, sandbox=sandbox, llm=router, tools=tools,
                      budget=budget, approvals=approvals, log=log, system_prompt=load_prompt(spec.system_prompt))


def run_workflow(
    requirement: str | None,
    workflow: str = "sdlc",
    settings: Settings | None = None,
    approvals: ApprovalPolicy | None = None,
    inputs: dict[str, Any] | None = None,
    run_id: str | None = None,
    resume: bool = False,
    verbose: bool = False,
) -> WorkflowState:
    settings = settings or Settings()
    approvals = approvals or AutoApproval()
    spec = load_workflow(workflow)
    if resume:
        if not run_id:
            raise ConfigError("--resume needs --run-id")
        path = settings.workspace_dir / run_id / "state.json"
        if not path.exists():
            raise ConfigError(f"no checkpoint at {path}")
        state = WorkflowState.load(path)
        state.record("resumed")
    else:
        if not requirement or not requirement.strip():
            raise ConfigError("a requirement is required")
        merged = {"target_dir": str(DEFAULT_TARGET), **(inputs or {})}
        merged["target_dir"] = str(Path(merged["target_dir"]).resolve())
        state = WorkflowState(run_id=run_id or new_run_id(), workflow=spec.name, requirement=requirement.strip(), inputs=merged)
    ctx = build_context(spec, state, settings, approvals, verbose)
    ctx.log.info("run_started", workflow=spec.name, engine=spec.engine, resume=resume,
                 provider=settings.provider_override or "per-tier config")
    return get_engine(spec.engine).execute(spec, state, ctx)
