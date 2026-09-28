"""Config validation, graph algorithms, sandbox, templating and the tool registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_sdlc.core.budget import BudgetLimits, BudgetTracker
from agentic_sdlc.core.config import load_workflow
from agentic_sdlc.core.errors import BudgetExceeded, ConfigError, SandboxViolation, ToolError
from agentic_sdlc.core.graph import TaskGraph
from agentic_sdlc.core.sandbox import Sandbox
from agentic_sdlc.core.templating import render, resolve
from agentic_sdlc.tools.registry import ToolEnv, ToolRegistry, registered_tools

from .helpers import ScriptedClient, llm, run, write_workflow


class _Log:
    def info(self, *a: object, **k: object) -> None: ...


# ---------------------------------------------------------------- workflow config
def test_builtin_workflow_is_a_valid_dag_with_a_parallel_generation_level():
    spec = load_workflow("sdlc")
    levels = spec.graph().levels()
    assert levels[0] == ["requirements"] and levels[-1] == ["report"]
    assert {"codegen", "testgen", "docs"} in [set(level) for level in levels]
    assert spec.agent("verify").feedback and spec.agent("verify").feedback.target == "codegen"


@pytest.mark.parametrize("agents, message", [
    ([llm("a", depends_on=["b"]), llm("b", depends_on=["a"])], "cycle"),
    ([llm("a", depends_on=["ghost"])], "unknown node"),
    ([llm("a", tier="huge")], "unknown model tier"),
    ([llm("a"), llm("a")], "duplicate"),
    ([llm("a", output_schema="tests.helpers.Nope")], "not a Pydantic model"),
    ([{"id": "a", "output_schema": "tests.helpers.Note"}], "needs a prompt"),
    ([llm("a"), llm("b", feedback={"target": "c"}), llm("c", depends_on=["b"])], "must be an upstream step"),
    ([llm("a", on_failure="fallback")], "no fallback"),
])
def test_invalid_workflows_are_rejected_at_load_time(tmp_path, agents, message):
    with pytest.raises(ConfigError, match=message):
        load_workflow(str(write_workflow(tmp_path, agents)))


def test_a_team_adds_an_agent_with_yaml_prompt_and_schema_only(tmp_path):
    """Pluggability: new agent = YAML entry + prompt file next to it + a schema in the team's own module."""
    (tmp_path / "review.md").write_text("Review: {{ requirement }} after {{ outputs.draft.text }}", encoding="utf-8")
    ScriptedClient.reset({"Note": [{"text": "draft v1"}], "Check": [{"passed": True, "feedback": "lgtm"}]})
    wf = write_workflow(tmp_path, [llm("draft"), llm("review", schema="tests.helpers.Check", depends_on=["draft"], prompt="review.md")])
    state = run(tmp_path, wf)
    assert state.status == "succeeded" and state.outputs["review"]["passed"] is True
    assert ScriptedClient.calls_for("review")[0].prompt == "Review: do the thing after draft v1"


# ---------------------------------------------------------------- graph
def test_graph_levels_critical_path_and_relations():
    g = TaskGraph({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"], "e": []})
    assert g.levels() == [["a", "e"], ["b", "c"], ["d"]]
    assert g.critical_path() == ["a", "b", "d"]
    assert g.critical_path({"c": 5}) == ["a", "c", "d"]
    assert g.descendants("a") == {"b", "c", "d"} and g.ancestors("d") == {"a", "b", "c"}


def test_graph_reports_the_cycle():
    with pytest.raises(ConfigError, match="a -> c -> b -> a"):
        TaskGraph({"a": ["c"], "b": ["a"], "c": ["b"]})


# ---------------------------------------------------------------- sandbox
@pytest.mark.parametrize("bad", ["../escape.txt", "/etc/passwd", "a/../../x", "C:/Windows/x", ".env", "sub/.git/config"])
def test_sandbox_blocks_writes_outside_the_workspace(tmp_path, bad):
    box = Sandbox(tmp_path / "ws")
    with pytest.raises(SandboxViolation):
        box.write_text(bad, "x")


def test_sandbox_reads_only_from_allowed_roots_and_copies_them_in(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("x = 1\n")
    box = Sandbox(tmp_path / "ws", read_roots=[repo])
    assert box.read_text(str(repo / "pkg" / "m.py")) == "x = 1\n"
    with pytest.raises(SandboxViolation):
        box.read_text(str(tmp_path / "elsewhere.txt"))
    box.copy_tree_in(repo, "project")
    box.write_text("project/pkg/m.py", "x = 2\n")
    assert (repo / "pkg" / "m.py").read_text(encoding="utf-8") == "x = 1\n"  # the original is never modified


def test_sandbox_refuses_oversized_writes(tmp_path):
    with pytest.raises(SandboxViolation):
        Sandbox(tmp_path).write_text("big.txt", "x" * (600 * 1024))


# ---------------------------------------------------------------- templating
def test_templates_are_strict_and_typed():
    ctx = {"outputs": {"a": {"files": [1, 2]}}, "requirement": "r"}
    assert render("{{ requirement }}!", ctx) == "r!"
    assert resolve("{{ outputs.a.files }}", ctx) == [1, 2]  # whole-placeholder keeps the type
    with pytest.raises(ConfigError, match="not found"):
        render("{{ outputs.missing }}", ctx)


# ---------------------------------------------------------------- budget
def test_budget_blocks_before_spending():
    b = BudgetTracker(BudgetLimits(max_llm_calls=2))
    b.reserve_llm_call()
    b.reserve_llm_call()
    with pytest.raises(BudgetExceeded):
        b.reserve_llm_call()
    assert b.llm_calls == 2


# ---------------------------------------------------------------- tools
def test_builtin_tools_are_registered_by_decorator():
    assert {"read_file", "write_file", "list_files", "materialize", "run_pytest", "lint", "verify",
            "check_contract", "analyze_codebase", "render_report"} <= set(registered_tools())


def test_tool_arguments_validated_and_allowlist_enforced(tmp_path):
    reg = ToolRegistry(ToolEnv(Sandbox(tmp_path), _Log()), BudgetTracker(BudgetLimits()), {"writer": {"write_file"}})
    assert reg.call("write_file", {"path": "a.txt", "content": "hi"}, agent_id="writer") == "a.txt"
    with pytest.raises(ToolError, match="bad arguments"):
        reg.call("write_file", {"path": "a.txt"}, agent_id="writer")
    with pytest.raises(ConfigError, match="not allowed"):
        reg.call("read_file", {"path": "a.txt"}, agent_id="writer")
    with pytest.raises(ConfigError, match="unknown tool"):
        reg.call("rm_rf", {})


def test_materialize_applies_exact_edits_and_reports_mismatches(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "m.py").write_text("a = 1\nb = 2\n")
    reg = ToolRegistry(ToolEnv(Sandbox(tmp_path / "ws", [base]), _Log()), BudgetTracker(BudgetLimits()))
    out = reg.call("materialize", {"mode": "brownfield", "base_dir": str(base), "edits": [
        {"path": "m.py", "find": "a = 1\n", "replace": "a = 10\n"},
        {"path": "m.py", "find": "zzz", "replace": "y"}], "tests": [{"path": "tests/test_m.py", "content": "def test_x():\n    pass\n"}]})
    assert out["edited"] == ["m.py"] and len(out["problems"]) == 1 and "matched 0 times" in out["problems"][0]
    assert (tmp_path / "ws" / "project" / "m.py").read_text(encoding="utf-8") == "a = 10\nb = 2\n"
    assert (base / "m.py").read_text(encoding="utf-8") == "a = 1\nb = 2\n"


def test_codebase_analysis_maps_routes_flows_and_impact(tmp_path):
    target = Path(__file__).resolve().parent.parent / "targets" / "url_shortener"
    reg = ToolRegistry(ToolEnv(Sandbox(tmp_path, [target]), _Log()), BudgetTracker(BudgetLimits()))
    report = reg.call("analyze_codebase", {"root": str(target), "requirement": "add a max_clicks limit; redirects return 410 when reached"})
    routes = {f"{r['method']} {r['path']}" for r in report["inventory"]["routes"]}
    assert {"POST /api/v1/urls", "GET /{short_code}", "GET /api/v1/urls/{short_code}/analytics"} <= routes
    redirect = next(f for f in report["flows"] if f["entrypoint"] == "GET /{short_code}")
    assert "services.resolve_url" in redirect["reachable"] and any("clicks" in m for m in redirect["models"])
    assert report["impacted"] and report["tests_to_update"]
