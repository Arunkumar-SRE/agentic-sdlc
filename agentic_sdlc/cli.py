"""Command line: run a workflow, resume one, or inspect what is registered."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core.config import Settings, load_workflow
from .core.errors import AgenticError
from .core.gates import ApprovalPolicy, AutoApproval, InteractiveApproval
from .core.llm import available_providers
from .runner import run_workflow
from .tools.registry import registered_tools

EXIT = {"succeeded": 0, "needs_attention": 1, "failed": 1, "halted": 2}


def _faults(values: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        step, _, n = v.partition(":")
        out[step] = int(n or 1)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentic_sdlc", description="Agentic software-engineering workflow")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run a workflow on a requirement")
    r.add_argument("-r", "--requirement", help="the requirement text")
    r.add_argument("-f", "--requirement-file", type=Path, help="read the requirement from a file")
    r.add_argument("-w", "--workflow", default="sdlc", help="built-in workflow name or path to a YAML file")
    r.add_argument("--mode", choices=["greenfield", "brownfield"], help="override change-type detection")
    r.add_argument("--target", type=Path, help="existing codebase for brownfield work (default: targets/url_shortener)")
    r.add_argument("--provider", help="serve every tier from this provider (e.g. offline, anthropic)")
    r.add_argument("--model", help="force one model id for every tier")
    r.add_argument("--auto-approve", action="store_true", help="unattended: approve routine gates, escalate low confidence")
    r.add_argument("--accept-assumptions", action="store_true", help="with --auto-approve: proceed on stated assumptions")
    r.add_argument("--reject-at", action="append", default=[], metavar="STEP", help="simulate a reviewer rejecting a gate")
    r.add_argument("--inject-fault", action="append", default=[], metavar="STEP:N", help="fail a step's first N attempts")
    r.add_argument("--inject-defect", action="store_true", help="offline provider: first code round has a real bug")
    r.add_argument("--malformed-once", action="append", default=[], metavar="SCHEMA",
                   help="offline provider: first answer for this output schema is invalid (e.g. TaskPlan)")
    r.add_argument("--run-id")
    r.add_argument("--resume", action="store_true", help="continue a checkpointed run (needs --run-id)")
    r.add_argument("--no-cache", action="store_true")
    r.add_argument("--max-llm-calls", type=int)
    r.add_argument("--workspace", type=Path, default=Path("workspace"))
    r.add_argument("--logs", type=Path, default=Path("logs"))
    r.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("list", help="list registered tools, providers and built-in workflows")
    v = sub.add_parser("validate", help="validate a workflow YAML without running it")
    v.add_argument("workflow")
    return p


def _utf8_console() -> None:
    """Windows consoles default to a legacy code page; never crash on a character it cannot show."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            print("tools:")
            for name, tool_spec in sorted(registered_tools().items()):
                print(f"  {name:18} [{tool_spec.side_effects}] {tool_spec.description}")
            print("providers:", ", ".join(available_providers()))
            print("workflows:", ", ".join(p.stem for p in sorted((Path(__file__).parent / "workflows").glob("*.yaml"))))
            return 0
        if args.command == "validate":
            spec = load_workflow(args.workflow)
            levels = spec.graph().levels()
            print(f"OK: {spec.name} v{spec.version}, {len(spec.agents)} agents, engine={spec.engine}")
            print("levels:", " -> ".join("[" + ", ".join(level) + "]" for level in levels))
            return 0
        return _run(args)
    except AgenticError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace) -> int:
    requirement = args.requirement
    if args.requirement_file:
        requirement = args.requirement_file.read_text(encoding="utf-8")
    approvals: ApprovalPolicy = (AutoApproval(args.accept_assumptions, set(args.reject_at)) if args.auto_approve or args.reject_at
                                 else InteractiveApproval())
    offline: dict[str, object] = {}
    if args.inject_defect:
        offline["inject_defect"] = True
    if args.malformed_once:
        offline["malformed_once"] = args.malformed_once
    provider_options = {"offline": offline} if offline else {}
    settings = Settings(workspace_dir=args.workspace, logs_dir=args.logs, provider_override=args.provider,
                        model_override=args.model, cache_enabled=not args.no_cache, max_llm_calls=args.max_llm_calls,
                        provider_options=provider_options, faults=_faults(args.inject_fault))
    inputs = {}
    if args.mode:
        inputs["mode"] = args.mode
    if args.target:
        inputs["target_dir"] = str(args.target)
    state = run_workflow(requirement, args.workflow, settings, approvals, inputs, args.run_id, args.resume, args.verbose)

    root = settings.workspace_dir / state.run_id
    print(f"\nrun {state.run_id}: {state.status.upper()}" + (f" ({state.halted_reason})" if state.halted_reason else ""))
    for step, rec in state.steps.items():
        extra = f" rounds={rec.rounds}" if rec.rounds else ""
        extra += " cached" if rec.cached else ""
        print(f"  {step:12} {rec.status.value:10} attempts={rec.attempts}{extra}" + (f"  {rec.error[:90]}" if rec.error else ""))
    u = state.usage
    print(f"usage: {u.get('llm_calls')} model calls ({u.get('cache_hits')} cached), {u.get('tool_calls')} tool calls, {u.get('elapsed_s')} s")
    summary = root / "artifacts" / "engineering_summary.md"
    print(f"workspace: {root}")
    if summary.exists():
        print(f"summary:   {summary}")
    return EXIT.get(state.status, 1)


if __name__ == "__main__":
    sys.exit(main())
