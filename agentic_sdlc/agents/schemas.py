"""Pydantic output schemas: the contract every agent's output must satisfy before anyone downstream sees it.

Validators here are guardrails: an LLM plan with a dependency cycle, generated code that does not parse, or a
file path that escapes the workspace is rejected and the model is asked again with the error.
"""

from __future__ import annotations

import ast
from pathlib import PurePosixPath
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..core.errors import ConfigError
from ..core.graph import TaskGraph

# ------------------------------------------------------------------ requirement understanding


class Ambiguity(BaseModel):
    topic: str
    question: str
    assumption: str


class RequirementSpec(BaseModel):
    original: str
    change_type: Literal["greenfield", "brownfield"]
    intent: Literal["feature", "bugfix", "refactor", "tests", "docs"]
    clarity: Literal["well_defined", "ambiguous"]
    normalized: str = Field(description="One-paragraph engineering problem statement")
    goals: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1, description="Testable statements")
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _ambiguous_needs_questions(self) -> RequirementSpec:
        if self.clarity == "ambiguous" and not self.ambiguities:
            raise ValueError("clarity is 'ambiguous' but no ambiguities (questions + assumptions) were listed")
        return self


# ------------------------------------------------------------------ codebase reasoning


class ImpactedModule(BaseModel):
    path: str
    role: str
    reason: str
    symbols: list[str] = Field(default_factory=list)


class RequestFlow(BaseModel):
    entrypoint: str
    chain: list[str]
    models: list[str] = Field(default_factory=list)


class CodebaseImpact(BaseModel):
    applicable: bool = True
    summary: str
    impacted_modules: list[ImpactedModule] = Field(default_factory=list)
    flows: list[RequestFlow] = Field(default_factory=list)
    data_flow_notes: list[str] = Field(default_factory=list)
    tests_to_update: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ decomposition


class PlanTask(BaseModel):
    id: str = Field(pattern=r"^[A-Z]\d+$")
    title: str
    owner_step: str = Field(description="Workflow step that produces the deliverable")
    depends_on: list[str] = Field(default_factory=list)
    deliverable: str
    acceptance: str


class TaskPlan(BaseModel):
    template: str
    rationale: str
    tasks: list[PlanTask] = Field(min_length=2)
    execution_levels: list[list[str]] = Field(default_factory=list, description="Computed; may be left empty")
    critical_path: list[str] = Field(default_factory=list, description="Computed; may be left empty")

    @model_validator(mode="after")
    def _valid_dag(self) -> TaskPlan:
        try:
            graph = TaskGraph({t.id: t.depends_on for t in self.tasks})
        except ConfigError as exc:
            raise ValueError(f"task plan is not a valid DAG: {exc}") from exc
        self.execution_levels = graph.levels()
        self.critical_path = graph.critical_path()
        return self


# ------------------------------------------------------------------ design


class Decision(BaseModel):
    decision: str
    rationale: str
    alternative: str


class Endpoint(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    purpose: str


class DesignSpec(BaseModel):
    summary: str
    components: list[str] = Field(min_length=1)
    endpoints: list[Endpoint] = Field(min_length=1)
    data_model: dict[str, list[str]]
    decisions: list[Decision] = Field(min_length=1)
    tradeoffs: list[str] = Field(default_factory=list)
    scaling_path: list[str] = Field(default_factory=list)
    slos: list[str] = Field(default_factory=list)
    change_plan: list[str] = Field(default_factory=list, description="Brownfield: concrete per-module changes")


# ------------------------------------------------------------------ contracts


class ApiContract(BaseModel):
    openapi: dict[str, Any] = Field(description="OpenAPI 3.x document")
    ddl: dict[str, str] = Field(description="Dialect name -> CREATE TABLE statements")
    compatibility_notes: list[str] = Field(default_factory=list)

    @field_validator("openapi")
    @classmethod
    def _looks_like_openapi(cls, doc: dict[str, Any]) -> dict[str, Any]:
        if not str(doc.get("openapi", "")).startswith("3") or not doc.get("paths"):
            raise ValueError("openapi must be an OpenAPI 3.x document with at least one path")
        return doc


# ------------------------------------------------------------------ code and tests


def _safe_relative(path: str) -> str:
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"file path must be relative and inside the project: {path}")
    return p.as_posix()


class SourceFile(BaseModel):
    path: str
    content: str

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return _safe_relative(v)

    @model_validator(mode="after")
    def _python_parses(self) -> SourceFile:
        if self.path.endswith(".py"):
            try:
                ast.parse(self.content)
            except SyntaxError as exc:
                raise ValueError(f"{self.path} does not parse: line {exc.lineno}: {exc.msg}") from exc
        return self


class Edit(BaseModel):
    """Exact search/replace against an existing file. `find` must match exactly once."""

    path: str
    find: str = Field(min_length=1)
    replace: str

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return _safe_relative(v)


class CodeBundle(BaseModel):
    summary: str
    files: list[SourceFile] = Field(default_factory=list, description="New or fully rewritten files")
    edits: list[Edit] = Field(default_factory=list, description="Brownfield: targeted edits to existing files")
    notes: list[str] = Field(default_factory=list)


class TestBundle(BaseModel):
    __test__: ClassVar[bool] = False  # not a pytest class

    strategy: dict[str, list[str]] = Field(description="Layer (unit/integration/contract/...) -> what it covers")
    files: list[SourceFile] = Field(min_length=1)
    covers_acceptance: list[str] = Field(default_factory=list)

    @field_validator("files")
    @classmethod
    def _test_names(cls, files: list[SourceFile]) -> list[SourceFile]:
        for f in files:
            name = PurePosixPath(f.path).name
            if f.path.endswith(".py") and not (name.startswith("test_") or name in ("conftest.py", "__init__.py")):
                raise ValueError(f"test file must be named test_*.py, conftest.py or __init__.py: {f.path}")
        return files


class DocBundle(BaseModel):
    files: list[SourceFile] = Field(min_length=1)


# ------------------------------------------------------------------ tool-agent outputs


class MaterializeResult(BaseModel):
    project_dir: str
    written: list[str]
    edited: list[str] = Field(default_factory=list)
    base: str | None = None
    problems: list[str] = Field(default_factory=list, description="Edits that could not be applied")


class CheckResult(BaseModel):
    passed: bool
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationReport(BaseModel):
    passed: bool
    checks: dict[str, CheckResult]
    feedback: str = ""
    environment_error: str | None = Field(None, description="Failure caused by the runner's environment, not the code")


class Risk(BaseModel):
    name: str
    likelihood: Literal["low", "medium", "high"]
    impact: Literal["low", "medium", "high"]
    mitigation: str
    status: Literal["mitigated_in_code", "verified_by_test", "documented", "accepted", "open"]


class FailureScenario(BaseModel):
    scenario: str
    detection: str
    behaviour: str


class RiskRegister(BaseModel):
    risks: list[Risk] = Field(min_length=1)
    failure_scenarios: list[FailureScenario] = Field(default_factory=list)
    validation_strategy: list[str] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _every_risk_mitigated_or_accepted(self) -> RiskRegister:
        for r in self.risks:
            if r.status != "accepted" and len(r.mitigation.strip()) < 10:
                raise ValueError(f"risk '{r.name}' needs a concrete mitigation")
        return self


class SummaryReport(BaseModel):
    path: str
    artifacts: list[str]
    verdict: Literal["ready_for_review", "needs_attention"]
