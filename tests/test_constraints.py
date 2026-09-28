"""The framework constraints from the brief, enforced as tests so they cannot silently regress."""

from __future__ import annotations

import ast
import re
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "agentic_sdlc"
SOURCES = sorted(PKG.rglob("*.py"))
BANNED = re.compile(r"^(langchain|langgraph|crewai|autogen|pyautogen|llama_index|semantic_kernel)", re.I)
ALLOWED_THIRD_PARTY = {"pydantic", "yaml", "structlog", "anthropic"}  # plus stdlib; FastAPI/SQLAlchemy/pytest are for generated code


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


def test_no_agent_framework_dependencies():
    offenders = {p.name: sorted(i for i in _imports(p) if BANNED.match(i)) for p in SOURCES}
    assert not {k: v for k, v in offenders.items() if v}
    reqs = (PKG.parent / "requirements.txt").read_text(encoding="utf-8").lower()
    assert not any(b in reqs for b in ("langchain", "langgraph", "crewai", "autogen"))


def test_only_allowed_third_party_imports():
    import sys
    stdlib = set(sys.stdlib_module_names)
    third = {i for p in SOURCES for i in _imports(p)} - stdlib - {"agentic_sdlc", "__future__"}
    assert third <= ALLOWED_THIRD_PARTY, third - ALLOWED_THIRD_PARTY


def test_every_function_is_fully_type_hinted():
    missing = []
    for path in SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs if a.arg not in ("self", "cls")]
                args += [a for a in (node.args.vararg, node.args.kwarg) if a]
                if node.returns is None or any(a.annotation is None for a in args):
                    missing.append(f"{path.relative_to(PKG)}:{node.lineno} {node.name}")
    assert not missing, missing


def test_agents_package_holds_no_orchestration_logic():
    """agents/ is data: prompt templates and output schemas. It may only use the graph (plan validation) and errors."""
    agents = PKG / "agents"
    assert {p.name for p in agents.glob("*.py")} == {"__init__.py", "schemas.py"}
    internal = set()
    for node in ast.walk(ast.parse((agents / "schemas.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            internal.add(node.module)
    assert internal <= {"core.errors", "core.graph"}, internal
    assert all(p.suffix == ".md" for p in (agents / "prompts").iterdir())


def test_model_ids_live_in_config_not_code():
    hits = [p.name for p in SOURCES if re.search(r"claude-(haiku|sonnet|opus)|gpt-\d", p.read_text(encoding="utf-8"))]
    assert not hits, hits


def test_layout_matches_the_brief():
    for rel in ["core", "agents", "engines/simple.py", "engines/base.py", "tools", "workflows", "cli.py"]:
        assert (PKG / rel).exists(), rel
    assert (PKG.parent / "workspace").is_dir() and (PKG.parent / "logs").is_dir()
