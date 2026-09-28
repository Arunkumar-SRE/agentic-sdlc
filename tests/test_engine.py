"""Orchestration behaviour of the default engine, driven by YAML workflows and a scripted provider."""

from __future__ import annotations

import pytest

from agentic_sdlc.core.errors import AgenticError, RetryableError
from agentic_sdlc.core.gates import AutoApproval
from agentic_sdlc.core.state import StepStatus

from . import helpers
from .helpers import ScriptedClient, llm, run, slow_note, write_workflow

OK = {"text": "ok"}
PLUGINS = {"plugins": ["tests.helpers"]}


@pytest.fixture(autouse=True)
def _reset() -> None:
    ScriptedClient.reset()
    helpers.CHECK_RESULTS[:] = [{"passed": True}]


def test_independent_branches_run_concurrently_and_dependencies_are_respected(tmp_path):
    log: list[tuple[str, float, float]] = []
    ScriptedClient.reset({"Note": [lambda r: slow_note(r.metadata["agent"], 0.4, log)(r)]})
    wf = write_workflow(tmp_path, [llm("a"), llm("b", depends_on=["a"]), llm("c", depends_on=["a"]), llm("d", depends_on=["b", "c"])])
    state = run(tmp_path, wf)
    assert state.status == "succeeded"
    t = {name: (start, end) for name, start, end in log}
    assert t["a"][1] <= t["b"][0] and t["a"][1] <= t["c"][0]  # dependency order
    assert max(t["b"][1], t["c"][1]) <= t["d"][0]
    assert t["b"][0] < t["c"][1] and t["c"][0] < t["b"][1]  # b and c overlapped


def test_transient_errors_are_retried(tmp_path):
    ScriptedClient.reset({"Note": [RetryableError("429"), RetryableError("timeout"), OK]})
    state = run(tmp_path, write_workflow(tmp_path, [llm("a")]))
    assert state.status == "succeeded" and state.steps["a"].attempts == 3
    assert [e.kind for e in state.events].count("retry") == 2


def test_invalid_model_output_is_rejected_and_the_error_fed_back(tmp_path):
    ScriptedClient.reset({"Note": [{"wrong": 1}, OK]})
    state = run(tmp_path, write_workflow(tmp_path, [llm("a")]))
    assert state.status == "succeeded" and state.steps["a"].attempts == 2
    second_prompt = ScriptedClient.calls[1].prompt
    assert "Previous output was rejected" in second_prompt and "text" in second_prompt


def test_non_retryable_error_fails_fast_and_halts(tmp_path):
    ScriptedClient.reset({"Note": [AgenticError("bad request")]})
    state = run(tmp_path, write_workflow(tmp_path, [llm("a"), llm("b", depends_on=["a"])]))
    assert state.status == "failed" and state.steps["a"].attempts == 1
    assert state.steps["b"].status == StepStatus.PENDING


def test_fallback_output_marks_step_degraded_and_run_continues(tmp_path):
    ScriptedClient.reset({"Note": [AgenticError("down"), OK]})
    wf = write_workflow(tmp_path, [llm("a", on_failure="fallback", fallback={"text": "static default"}), llm("b", depends_on=["a"])])
    state = run(tmp_path, wf)
    assert state.steps["a"].status == StepStatus.DEGRADED and state.outputs["a"]["text"] == "static default"
    assert state.steps["b"].status == StepStatus.SUCCEEDED and state.status == "needs_attention"


def test_continue_policy_blocks_only_dependents(tmp_path):
    def answer(r):
        if r.metadata["agent"] == "a":
            raise AgenticError("broken")
        return OK
    ScriptedClient.reset({"Note": [answer]})
    wf = write_workflow(tmp_path, [llm("a", on_failure="continue"), llm("b", depends_on=["a"]), llm("c")])
    state = run(tmp_path, wf)
    assert state.steps["a"].status == StepStatus.FAILED and state.steps["b"].status == StepStatus.BLOCKED
    assert state.steps["c"].status == StepStatus.SUCCEEDED and state.status == "needs_attention"


def test_rejected_gate_halts_and_step_is_rerun_on_resume(tmp_path):
    ScriptedClient.reset({"Note": [OK]})
    wf = write_workflow(tmp_path, [llm("a"), llm("b", depends_on=["a"], gate={"required": True}), llm("c", depends_on=["b"])])
    state = run(tmp_path, wf, approvals=AutoApproval(reject_at={"b"}), run_id="r1")
    assert state.status == "halted" and state.steps["b"].status == StepStatus.FAILED
    assert state.steps["c"].status == StepStatus.PENDING
    assert state.approvals[-1].approved is False

    before = len(ScriptedClient.calls)
    resumed = run(tmp_path, wf, approvals=AutoApproval(), run_id="r1", resume=True)
    assert resumed.status == "succeeded"
    assert [c.metadata["agent"] for c in ScriptedClient.calls[before:]] == ["b", "c"]  # 'a' reused from checkpoint
    assert "Reviewer rejected" in ScriptedClient.calls_for("b")[-1].prompt


def test_low_confidence_escalates_unless_assumptions_accepted(tmp_path):
    ScriptedClient.reset({"Note": [{"text": "vague", "confidence": 0.3}]})
    wf = write_workflow(tmp_path, [llm("a", gate={"required": False, "min_confidence": 0.6}), llm("b", depends_on=["a"])])
    state = run(tmp_path, wf)
    assert state.status == "halted" and any(e.kind == "escalated" for e in state.events)
    assert state.steps["b"].status == StepStatus.PENDING
    accepted = run(tmp_path, wf, approvals=AutoApproval(accept_low_confidence=True))
    assert accepted.status == "succeeded"


def test_feedback_loop_reopens_upstream_step_with_findings(tmp_path):
    helpers.CHECK_RESULTS[:] = [{"passed": False, "feedback": "test_x failed: expected 410"}, {"passed": True}]
    ScriptedClient.reset({"Note": [OK]})
    wf = write_workflow(tmp_path, [
        llm("gen"),
        {"id": "check", "kind": "tool", "tool": "scripted_check", "output_schema": "tests.helpers.Check", "depends_on": ["gen"],
         "feedback": {"target": "gen", "max_rounds": 2}},
        llm("after", depends_on=["check"]),
    ], **PLUGINS)
    state = run(tmp_path, wf)
    assert state.status == "succeeded"
    assert state.steps["gen"].rounds == 1 and len(ScriptedClient.calls_for("gen")) == 2
    assert "expected 410" in ScriptedClient.calls_for("gen")[1].prompt
    assert len(ScriptedClient.calls_for("after")) == 1  # downstream ran once, after the loop settled
    assert any(e.kind == "feedback_loop" for e in state.events)


def test_feedback_loop_is_bounded(tmp_path):
    helpers.CHECK_RESULTS[:] = [{"passed": False, "feedback": "still broken"}]
    ScriptedClient.reset({"Note": [OK]})
    wf = write_workflow(tmp_path, [
        llm("gen"),
        {"id": "check", "kind": "tool", "tool": "scripted_check", "output_schema": "tests.helpers.Check", "depends_on": ["gen"],
         "feedback": {"target": "gen", "max_rounds": 2}},
    ], **PLUGINS)
    state = run(tmp_path, wf)
    assert len(ScriptedClient.calls_for("gen")) == 3 and state.steps["gen"].rounds == 2
    assert state.status == "needs_attention" and any(e.kind == "feedback_exhausted" for e in state.events)


def test_budget_exhaustion_halts_the_run(tmp_path):
    ScriptedClient.reset({"Note": [OK]})
    wf = write_workflow(tmp_path, [llm("a"), llm("b", depends_on=["a"])], budgets={"max_llm_calls": 1})
    state = run(tmp_path, wf)
    assert state.status == "halted" and "budget" in (state.halted_reason or "")
    assert state.steps["b"].status == StepStatus.FAILED and len(ScriptedClient.calls) == 1


def test_condition_skips_step_and_dependents_still_run(tmp_path):
    ScriptedClient.reset({"Note": [{"text": "t", "kind": "greenfield"}]})
    wf = write_workflow(tmp_path, [llm("a"), llm("only_brownfield", depends_on=["a"], when={"path": "a.kind", "in": ["brownfield"]}),
                                   llm("c", depends_on=["only_brownfield"])])
    state = run(tmp_path, wf)
    assert state.steps["only_brownfield"].status == StepStatus.SKIPPED
    assert state.steps["c"].status == StepStatus.SUCCEEDED and state.status == "succeeded"


def test_identical_rerun_is_served_from_cache(tmp_path):
    ScriptedClient.reset({"Note": [OK]})
    wf = write_workflow(tmp_path, [llm("a"), llm("b", depends_on=["a"])], cache=True)
    first = run(tmp_path, wf)
    second = run(tmp_path, wf)
    # Content-addressed: a and b send byte-identical requests, so even the first run pays for only one call.
    assert first.usage["llm_calls"] == 1 and first.usage["cache_hits"] == 1
    assert second.usage["llm_calls"] == 0 and second.usage["cache_hits"] == 2
    assert all(r.cached for r in second.steps.values())


def test_injected_faults_exercise_recovery(tmp_path):
    ScriptedClient.reset({"Note": [OK]})
    state = run(tmp_path, write_workflow(tmp_path, [llm("a")]), faults={"a": 2})
    assert state.status == "succeeded" and state.steps["a"].attempts == 3
    assert [e.kind for e in state.events].count("fault_injected") == 2


def test_checkpoint_and_artifacts_written(tmp_path):
    ScriptedClient.reset({"Note": [OK]})
    state = run(tmp_path, write_workflow(tmp_path, [llm("a")]), run_id="cp")
    root = tmp_path / "ws" / "cp"
    assert (root / "state.json").exists() and (root / "artifacts" / "a.json").exists()
    assert state.status == "succeeded"
