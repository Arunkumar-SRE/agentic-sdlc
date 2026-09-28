"""RunContext: the capabilities an agent may use. Agents get this and the state; nothing else."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .budget import BudgetTracker
from .config import Settings, WorkflowSpec
from .gates import ApprovalPolicy
from .llm import ModelRouter
from .sandbox import Sandbox

if TYPE_CHECKING:
    from ..tools.registry import ToolRegistry


@dataclass
class RunContext:
    run_id: str
    workflow: WorkflowSpec
    settings: Settings
    sandbox: Sandbox
    llm: ModelRouter
    tools: ToolRegistry
    budget: BudgetTracker
    approvals: ApprovalPolicy
    log: Any  # structlog BoundLogger
    system_prompt: str = ""
    extras: dict[str, Any] = field(default_factory=dict)
