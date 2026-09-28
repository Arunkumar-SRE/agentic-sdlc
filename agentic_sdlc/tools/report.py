"""Deterministic report rendering. The final summary is assembled from validated outputs, never free-written."""

from __future__ import annotations

import json
from typing import Any

from .registry import ToolEnv, tool


def _cell(value: Any) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def _bullets(items: list[Any]) -> list[str]:
    return [f"- {i}" for i in items] or ["- none"]


def _design_md(d: dict[str, Any]) -> str:
    lines = ["# Design", "", d.get("summary", ""), "", "## Components", *_bullets(d.get("components", [])), "",
             "## Endpoints", "| Method | Path | Purpose |", "|---|---|---|",
             *[f"| {e['method']} | `{e['path']}` | {e['purpose']} |" for e in d.get("endpoints", [])], "",
             "## Data model", *[f"- **{t}**: {', '.join(cols)}" for t, cols in d.get("data_model", {}).items()], "",
             "## Key decisions", "| Decision | Rationale | Alternative considered |", "|---|---|---|",
             *[f"| {x['decision']} | {x['rationale']} | {x['alternative']} |" for x in d.get("decisions", [])], "",
             "## Trade-offs", *_bullets(d.get("tradeoffs", [])), "", "## SLOs", *_bullets(d.get("slos", [])), "",
             "## Scaling path", *[f"{i}. {s}" for i, s in enumerate(d.get("scaling_path", []), 1)]]
    if d.get("change_plan"):
        lines += ["", "## Change plan (brownfield)", *_bullets(d["change_plan"])]
    return "\n".join(lines) + "\n"


def _plan_md(p: dict[str, Any]) -> list[str]:
    return ["| Task | Title | Depends on | Produced by | Deliverable | Acceptance |", "|---|---|---|---|---|---|",
            *[f"| {t['id']} | {t['title']} | {', '.join(t['depends_on']) or '-'} | `{t['owner_step']}` | {t['deliverable']} | {t['acceptance']} |"
              for t in p.get("tasks", [])], "",
            "Execution levels (tasks in one level can proceed in parallel): " +
            " -> ".join("[" + ", ".join(level) + "]" for level in p.get("execution_levels", [])),
            "", "Critical path: " + " -> ".join(p.get("critical_path", []))]


def _risk_md(r: dict[str, Any]) -> str:
    lines = ["# Risk register", "", "| Risk | Likelihood | Impact | Mitigation | Status |", "|---|---|---|---|---|",
             *[f"| {x['name']} | {x['likelihood']} | {x['impact']} | {x['mitigation']} | {x['status']} |" for x in r.get("risks", [])],
             "", "## Failure scenarios", "| Scenario | Detection | Behaviour |", "|---|---|---|",
             *[f"| {x['scenario']} | {x['detection']} | {x['behaviour']} |" for x in r.get("failure_scenarios", [])],
             "", "## Validation strategy", *_bullets(r.get("validation_strategy", []))]
    return "\n".join(lines) + "\n"


def _verification_md(v: dict[str, Any]) -> str:
    lines = [f"# Verification: {'PASS' if v.get('passed') else 'FAIL'}", "", "| Check | Result | Summary |", "|---|---|---|"]
    for name, c in v.get("checks", {}).items():
        lines.append(f"| {name} | {'pass' if c['passed'] else 'FAIL'} | {_cell(c['summary'])} |")
    if v.get("feedback"):
        lines += ["", "## Repair feedback", "```", v["feedback"], "```"]
    return "\n".join(lines) + "\n"


@tool(side_effects="write")
def render_report(env: ToolEnv, requirement: str, outputs: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """Render reviewable artifacts and the engineering summary from validated step outputs."""
    written: list[str] = []

    def put(name: str, content: str) -> None:
        env.sandbox.write_text(f"artifacts/{name}", content)
        written.append(f"artifacts/{name}")

    req = outputs.get("requirements", {})
    plan = outputs.get("plan", {})
    design = outputs.get("design", {})
    contract = outputs.get("contract", {})
    tests = outputs.get("testgen", {})
    verify = outputs.get("verify", {})
    risks = outputs.get("risks", {})
    impact = outputs.get("impact")
    code = outputs.get("codegen", {})
    mat = outputs.get("materialize", {})

    if design:
        put("design.md", _design_md(design))
    if contract:
        put("openapi.json", json.dumps(contract["openapi"], indent=2))
        put("schema.sql", "\n\n".join(f"-- dialect: {k}\n{v.strip()}" for k, v in contract.get("ddl", {}).items()) + "\n")
    if tests:
        put("test_plan.md", "\n".join(["# Test plan", "", *[f"## {layer}\n" + "\n".join(_bullets(items)) for layer, items in tests["strategy"].items()],
                                       "", "## Acceptance criteria covered", *_bullets(tests.get("covers_acceptance", []))]) + "\n")
    if risks:
        put("risk_register.md", _risk_md(risks))
    if verify:
        put("verification.md", _verification_md(verify))

    steps = run.get("steps", {})
    passed = bool(verify.get("passed"))
    verdict = "ready_for_review" if passed else "needs_attention"
    usage = run.get("usage", {})
    md: list[str] = [
        "# Engineering summary", "",
        f"**Run** `{run.get('run_id')}` - workflow `{run.get('workflow')}` - verdict **{verdict.replace('_', ' ')}**", "",
        "## 1. Requirement", "", f"> {requirement}", "",
        f"- Classified as **{req.get('change_type')} / {req.get('intent')}**, clarity **{req.get('clarity')}**, "
        f"confidence **{req.get('confidence')}**",
        f"- Normalised: {req.get('normalized', '')}", "", "### Goals", *_bullets(req.get("goals", [])), "",
        "### Acceptance criteria", *_bullets(req.get("acceptance_criteria", [])),
    ]
    if req.get("ambiguities"):
        md += ["", "### Ambiguities (question -> working assumption)",
               *[f"- **{a['topic']}**: {a['question']} -> _{a['assumption']}_" for a in req["ambiguities"]]]
    if impact:
        md += ["", "## 2. Codebase impact (brownfield)", "", impact.get("summary", ""), "",
               "| Module | Role | Why | Symbols |", "|---|---|---|---|",
               *[f"| `{m['path']}` | {m['role']} | {m['reason']} | {', '.join(m['symbols'])} |" for m in impact.get("impacted_modules", [])],
               "", "Request flows:", *[f"- `{f['entrypoint']}`: {' -> '.join(f['chain'])} (models: {', '.join(f['models']) or '-'})" for f in impact.get("flows", [])],
               "", "Tests to update:", *_bullets(impact.get("tests_to_update", []))]
    md += ["", "## 3. Implementation plan and rationale", "", plan.get("rationale", ""), "", *_plan_md(plan), ""]
    if design:
        md += ["### Key design decisions", *[f"- **{d['decision']}**: {d['rationale']} (instead of: {d['alternative']})" for d in design.get("decisions", [])], ""]
        if design.get("change_plan"):
            md += ["### Change plan", *_bullets(design["change_plan"]), ""]
    md += ["## 4. Generated artifacts", "",
           f"- Code: {code.get('summary', '-')} ({len(code.get('files', []))} files, {len(code.get('edits', []))} edits)",
           f"- Project built in `{mat.get('project_dir', 'project')}/`" + (f" from base `{mat['base']}`" if mat.get("base") else "")
           + f": {len(mat.get('written', []))} files written, {len(mat.get('edited', []))} edited",
           f"- Tests: {len(tests.get('files', []))} files ({', '.join(tests.get('strategy', {}))})",
           *[f"- `{w}`" for w in written], "- `artifacts/<step>.json`: raw validated output of every step", "- `state.json`: checkpoint (resumable)", ""]
    md += ["## 5. Validation", "", f"Result: **{'PASS' if passed else 'FAIL'}**", "", "| Check | Result | Summary |", "|---|---|---|",
           *[f"| {n} | {'pass' if c['passed'] else 'FAIL'} | {_cell(c['summary'])} |" for n, c in verify.get("checks", {}).items()], ""]
    if verify.get("environment_error"):
        md += [f"**Environment problem (not a code defect, repair loop not used):** {verify['environment_error']}", ""]
    md += ["## 6. Risks, trade-offs and validation approach", "",
           "| Risk | L | I | Mitigation | Status |", "|---|---|---|---|---|",
           *[f"| {r['name']} | {r['likelihood']} | {r['impact']} | {r['mitigation']} | {r['status']} |" for r in risks.get("risks", [])],
           "", "Trade-offs:", *_bullets(design.get("tradeoffs", [])), "", "Validation strategy:", *_bullets(risks.get("validation_strategy", [])), ""]
    md += ["## 7. Assumptions and limitations", "", *_bullets(req.get("assumptions", []) + risks.get("limitations", []) + code.get("notes", [])), ""]
    md += ["## 8. Execution trace (controlled autonomy)", "", "| Step | Status | Attempts | Repair rounds | Cached | Seconds |", "|---|---|---|---|---|---|",
           *[f"| {k} | {'in progress (this report)' if v['status'] == 'running' else v['status']} | {v['attempts']} | {v['rounds']} | "
             f"{'yes' if v['cached'] else ''} | {v['duration_s']} |" for k, v in steps.items()],
           "", "Human gates:", *_bullets([f"{a['step']} ({a['reason']}): {'approved' if a['approved'] else 'REJECTED'} by {a['reviewer']} {a['note']}".strip()
                                          for a in run.get("approvals", [])]),
           "", "Recovery events:", *_bullets([f"{e['step']}: {e['kind']} " + _cell(json.dumps({k: v for k, v in e['detail'].items() if k != 'feedback'}))
                                         + (f" - feedback: {_cell(e['detail']['feedback'])[:300]}" if e['detail'].get('feedback') else "")
                                         for e in run.get("events", [])]),
           "", f"Budget used: {usage.get('llm_calls', 0)} model calls ({usage.get('cache_hits', 0)} cache hits), "
               f"{usage.get('input_tokens', 0) + usage.get('output_tokens', 0)} tokens, {usage.get('tool_calls', 0)} tool calls, "
               f"{usage.get('elapsed_s', 0)} s"]
    put("engineering_summary.md", "\n".join(md) + "\n")
    return {"path": "artifacts/engineering_summary.md", "artifacts": written, "verdict": verdict}
