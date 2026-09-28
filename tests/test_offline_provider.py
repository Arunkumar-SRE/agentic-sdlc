"""The offline reference provider: classification, plans, and code edits that really apply to the target."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentic_sdlc.agents.schemas import ApiContract, CodeBundle, DesignSpec, RequirementSpec, RiskRegister, TaskPlan, TestBundle
from agentic_sdlc.core.errors import UnsupportedByProvider
from agentic_sdlc.core.llm import LLMRequest
from agentic_sdlc.core.llm.offline_provider import OfflineReferenceClient

TARGET = Path(__file__).resolve().parent.parent / "targets" / "url_shortener"
MANDATORY = "Build a scalable URL shortener service with APIs, persistence, and analytics."
BROWNFIELD = ("Enhance the existing URL shortener: add a max_clicks limit per link; once reached, redirects return 410 "
              "and analytics report remaining clicks.")


def ask(schema: type, context: dict[str, Any], client: OfflineReferenceClient | None = None, round_: int = 0) -> Any:
    client = client or OfflineReferenceClient({})
    req = LLMRequest(system="", prompt="", schema_name=schema.__name__, json_schema={}, model="m", context=context,
                     metadata={"round": round_})
    return schema.model_validate(client.complete(req).data)


def requirement(text: str) -> RequirementSpec:
    return ask(RequirementSpec, {"requirement": text, "inputs": {}})


def test_mandatory_use_case_is_greenfield_with_explicit_assumptions():
    r = requirement(MANDATORY)
    assert (r.change_type, r.intent, r.clarity) == ("greenfield", "feature", "well_defined")
    assert {a.topic for a in r.ambiguities} == {"scale target", "analytics dimensions", "datastore"}
    assert r.confidence >= 0.6 and len(r.acceptance_criteria) >= 4


@pytest.mark.parametrize("text, change_type, intent", [
    (BROWNFIELD, "brownfield", "feature"),
    ("Fix the bug where expired links still redirect", "brownfield", "bugfix"),
    ("Refactor the service layer into smaller modules", "brownfield", "refactor"),
    ("Improve test coverage for the analytics endpoint", "brownfield", "tests"),
])
def test_change_type_and_intent_detection(text, change_type, intent):
    r = requirement(text)
    assert (r.change_type, r.intent) == (change_type, intent)


def test_ambiguous_requirement_has_low_confidence_questions_and_assumptions():
    r = requirement("Make links better and track stuff.")
    assert r.clarity == "ambiguous" and r.confidence < 0.6
    assert all(a.question and a.assumption for a in r.ambiguities)


@pytest.mark.parametrize("text", [MANDATORY, BROWNFIELD, "Fix the redirect bug", "Refactor services", "Add tests for analytics", "Update the docs"])
def test_every_plan_template_is_a_valid_dag(text):
    plan = ask(TaskPlan, {"outputs": {"requirements": requirement(text).model_dump()}})
    assert plan.execution_levels and plan.critical_path[-1] == plan.tasks[-1].id


def test_brownfield_edits_apply_exactly_once_to_the_real_target_and_parse():
    ctx = {"outputs": {"requirements": requirement(BROWNFIELD).model_dump()}}
    bundle = ask(CodeBundle, ctx)
    files = {p.relative_to(TARGET).as_posix(): p.read_text(encoding="utf-8") for p in TARGET.rglob("*.py")}
    for e in bundle.edits:
        assert files[e.path].count(e.find) == 1, e.find
        files[e.path] = files[e.path].replace(e.find, e.replace)
    for path, text in files.items():
        compile(text, path, "exec")
    assert "LinkExhausted" in files["app/services.py"] and "max_clicks" in files["app/models.py"]


def test_injected_defect_only_affects_the_first_round():
    ctx = {"outputs": {"requirements": requirement(MANDATORY).model_dump()}}
    client = OfflineReferenceClient({"inject_defect": True})
    first = ask(CodeBundle, ctx, client, 0)
    repaired = ask(CodeBundle, ctx, client, 1)
    svc = lambda b: next(f.content for f in b.files if f.path == "app/services.py")  # noqa: E731
    assert "raise LinkExpired()" not in svc(first) and "raise LinkExpired()" in svc(repaired)


def test_design_contract_tests_and_risks_validate_for_both_modes():
    for text in (MANDATORY, BROWNFIELD):
        ctx: dict[str, Any] = {"outputs": {"requirements": requirement(text).model_dump()}}
        ctx["outputs"]["design"] = ask(DesignSpec, ctx).model_dump()
        contract = ask(ApiContract, ctx)
        ctx["outputs"]["contract"] = contract.model_dump()
        tests = ask(TestBundle, ctx)
        ask(RiskRegister, ctx)
        assert "/api/v1/urls" in contract.openapi["paths"]
        assert any(f.path == "tests/test_contract.py" for f in tests.files)
    assert "max_clicks" in contract.openapi["components"]["schemas"]["URLCreate"]["properties"]


def test_malformed_first_answer_is_rejected_by_schema():
    client = OfflineReferenceClient({"malformed_once": ["RequirementSpec"]})
    with pytest.raises(Exception):
        ask(RequirementSpec, {"requirement": MANDATORY, "inputs": {}}, client)
    assert ask(RequirementSpec, {"requirement": MANDATORY, "inputs": {}}, client).change_type == "greenfield"


def test_out_of_domain_requirement_is_refused_not_faked():
    todo = "Build a todo list REST API where users can create tasks with due dates and list overdue tasks."
    r = requirement(todo)
    assert any(a.topic == "domain" for a in r.ambiguities)
    with pytest.raises(UnsupportedByProvider, match="--provider anthropic"):
        ask(DesignSpec, {"outputs": {"requirements": r.model_dump()}})
