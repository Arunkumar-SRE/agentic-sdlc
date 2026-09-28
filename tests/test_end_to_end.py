"""Full `sdlc` workflow with the offline provider. Verification really builds and tests the generated service,
so these need FastAPI + SQLAlchemy installed (they are skipped otherwise; the escalation test is not)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agentic_sdlc.core.config import Settings
from agentic_sdlc.core.gates import AutoApproval
from agentic_sdlc.core.state import StepStatus
from agentic_sdlc.runner import DEFAULT_TARGET, run_workflow

HAVE_SERVICE_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "sqlalchemy", "httpx"))
needs_service = pytest.mark.skipif(not HAVE_SERVICE_DEPS, reason="fastapi/sqlalchemy/httpx not installed")

MANDATORY = "Build a scalable URL shortener service with APIs, persistence, and analytics."
BROWNFIELD = ("Enhance the existing URL shortener: add a max_clicks limit per link; once reached, redirects return 410 "
              "and analytics report remaining clicks.")


def go(tmp: Path, requirement: str, approvals: AutoApproval | None = None, **settings: object):
    s = Settings(workspace_dir=tmp / "ws", logs_dir=tmp / "logs", cache_dir=tmp / "cache", **settings)  # type: ignore[arg-type]
    return run_workflow(requirement, "sdlc", s, approvals or AutoApproval(), run_id="e2e")


@needs_service
def test_mandatory_use_case_greenfield(tmp_path):
    state = go(tmp_path, MANDATORY)
    assert state.status == "succeeded", state.halted_reason
    checks = state.outputs["verify"]["checks"]
    assert all(c["passed"] for c in checks.values())
    assert checks["tests"]["details"]["total"] >= 14  # reference suite + generated contract tests
    assert state.steps["impact"].status == StepStatus.SKIPPED
    root = tmp_path / "ws" / "e2e"
    for name in ("engineering_summary.md", "openapi.json", "schema.sql", "design.md", "risk_register.md", "test_plan.md"):
        assert (root / "artifacts" / name).exists(), name
    assert (root / "project" / "app" / "main.py").exists() and (root / "project" / "README.md").exists()
    assert [a.step for a in state.approvals] == ["requirements", "plan", "report"]


@needs_service
def test_brownfield_enhancement_edits_a_copy_and_keeps_old_tests_green(tmp_path):
    before = {p: p.read_text(encoding="utf-8") for p in DEFAULT_TARGET.rglob("*.py")}
    state = go(tmp_path, BROWNFIELD)
    assert state.status == "succeeded", state.halted_reason
    assert state.outputs["impact"]["impacted_modules"]
    tests = state.outputs["verify"]["checks"]["tests"]["details"]
    assert tests["failed"] == 0 and tests["total"] >= 18  # 12 pre-existing + contract + max_clicks
    assert {p: p.read_text(encoding="utf-8") for p in DEFAULT_TARGET.rglob("*.py")} == before  # original repository untouched
    project = tmp_path / "ws" / "e2e" / "project"
    assert "LinkExhausted" in (project / "app" / "services.py").read_text(encoding="utf-8")


@needs_service
def test_verification_failure_triggers_a_repair_round(tmp_path):
    state = go(tmp_path, MANDATORY, provider_options={"offline": {"inject_defect": True}})
    assert state.status == "succeeded"
    assert state.steps["codegen"].rounds == 1
    loop = next(e for e in state.events if e.kind == "feedback_loop")
    assert "test_expired_link_returns_410" in loop.detail["feedback"]


@needs_service
def test_transient_faults_and_malformed_output_are_recovered(tmp_path):
    state = go(tmp_path, MANDATORY, faults={"design": 2}, provider_options={"offline": {"malformed_once": ["TaskPlan"]}})
    assert state.status == "succeeded"
    assert state.steps["design"].attempts == 3 and state.steps["plan"].attempts == 2


def test_ambiguous_requirement_escalates_in_unattended_mode(tmp_path):
    state = go(tmp_path, "Make links better and track stuff.")
    assert state.status == "halted" and "low_confidence" in (state.halted_reason or "")
    assert state.steps["plan"].status == StepStatus.PENDING  # nothing built on an unconfirmed interpretation


def test_reviewer_rejection_of_the_plan_stops_before_any_code(tmp_path):
    state = go(tmp_path, MANDATORY, approvals=AutoApproval(reject_at={"plan"}))
    assert state.status == "halted" and "codegen" not in state.outputs


def test_out_of_domain_requirement_stops_honestly_instead_of_building_a_url_shortener(tmp_path):
    state = go(tmp_path, "Build a todo list REST API where users can create tasks with due dates and list overdue tasks.")
    assert state.status == "failed" and "UnsupportedByProvider" in (state.halted_reason or "")
    assert state.steps["requirements"].status == StepStatus.SUCCEEDED and state.steps["plan"].status == StepStatus.SUCCEEDED
    assert "codegen" not in state.outputs  # nothing was built
