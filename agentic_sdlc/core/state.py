"""WorkflowState: the single, serialisable record of a run. Checkpointed after every change, so runs can resume."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)

RunStatus = Literal["running", "succeeded", "needs_attention", "halted", "failed"]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"  # failed, fallback output used
    SKIPPED = "skipped"  # `when` condition false
    FAILED = "failed"
    BLOCKED = "blocked"  # an upstream step failed

    @property
    def satisfies_dependents(self) -> bool:
        return self in (StepStatus.SUCCEEDED, StepStatus.DEGRADED, StepStatus.SKIPPED)

    @property
    def terminal(self) -> bool:
        return self not in (StepStatus.PENDING, StepStatus.RUNNING)


class StepRecord(BaseModel):
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    rounds: int = 0  # how many times a feedback loop re-opened this step
    cached: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float = 0.0
    error: str | None = None


class Event(BaseModel):
    ts: str = Field(default_factory=utcnow)
    step: str | None = None
    kind: str
    detail: dict[str, Any] = Field(default_factory=dict)


class Approval(BaseModel):
    step: str
    approved: bool
    reviewer: str
    reason: str
    note: str = ""
    ts: str = Field(default_factory=utcnow)


class WorkflowState(BaseModel):
    run_id: str
    workflow: str
    requirement: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    steps: dict[str, StepRecord] = Field(default_factory=dict)
    feedback: dict[str, list[str]] = Field(default_factory=dict)
    approvals: list[Approval] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    status: RunStatus = "running"
    halted_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow)

    def output(self, step_id: str, schema: type[T]) -> T:
        """Typed access to an upstream output."""
        return schema.model_validate(self.outputs[step_id])

    def has_output(self, step_id: str) -> bool:
        return step_id in self.outputs

    def record(self, kind: str, step: str | None = None, **detail: Any) -> Event:
        event = Event(step=step, kind=kind, detail=detail)
        self.events.append(event)
        return event

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic: a crash never leaves a half-written checkpoint

    @classmethod
    def load(cls, path: Path) -> WorkflowState:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
