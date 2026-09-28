"""Filesystem tools. All writes go through the Sandbox; the brownfield target is copied in, never edited in place."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from ..core.errors import ToolError
from .registry import ToolEnv, tool


@tool(side_effects="read")
def read_file(env: ToolEnv, path: str, max_bytes: int = 200_000) -> str:
    """Read a UTF-8 file from the workspace or an allowed read-only root."""
    text = env.sandbox.read_text(path)
    return text[:max_bytes]


@tool(side_effects="write")
def write_file(env: ToolEnv, path: str, content: str) -> str:
    """Write a UTF-8 file inside the run workspace."""
    return str(env.sandbox.write_text(path, content).relative_to(env.sandbox.root).as_posix())


@tool(side_effects="read")
def list_files(env: ToolEnv, path: str = ".", pattern: str = "**/*") -> list[str]:
    """List files under a workspace directory."""
    root = env.sandbox.resolve(path, for_write=False)
    return sorted(p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file() and "__pycache__" not in p.parts)


def _apply_edit(target: Path, find: str, replace: str) -> str | None:
    if not target.exists():
        return f"edit target does not exist: {target.name}"
    text = target.read_text(encoding="utf-8")
    count = text.count(find)
    if count != 1:
        return f"edit for {target.name} matched {count} times (must be exactly 1): {find[:60]!r}"
    target.write_text(text.replace(find, replace, 1), encoding="utf-8")
    return None


def _diff(base: Path, project: Path, paths: list[str]) -> str:
    """Unified diff of edited files against the untouched original, for human review."""
    chunks: list[str] = []
    for rel in paths:
        before = (base / rel).read_text(encoding="utf-8").splitlines(keepends=True)
        after = (project / rel).read_text(encoding="utf-8").splitlines(keepends=True)
        chunks.extend(difflib.unified_diff(before, after, f"a/{rel}", f"b/{rel}"))
    return "".join(chunks)


@tool(side_effects="write")
def materialize(
    env: ToolEnv,
    project_dir: str = "project",
    mode: str = "greenfield",
    base_dir: str | None = None,
    files: list[dict[str, Any]] | None = None,
    edits: list[dict[str, Any]] | None = None,
    tests: list[dict[str, Any]] | None = None,
    docs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the project in the workspace: copy the brownfield base, apply edits, write code, tests, docs.

    Idempotent: the project directory is rebuilt from scratch each time, so a feedback round never inherits
    half-applied changes from the previous one.
    """
    base = base_dir if mode == "brownfield" and base_dir else None
    if base:
        env.sandbox.copy_tree_in(Path(base), project_dir)
    else:
        env.sandbox.reset_dir(project_dir)
    written: list[str] = []
    edited: list[str] = []
    problems: list[str] = []
    for f in [*(files or []), *(tests or []), *(docs or [])]:
        env.sandbox.write_text(f"{project_dir}/{f['path']}", f["content"])
        written.append(f["path"])
    for e in edits or []:
        err = _apply_edit(env.sandbox.resolve(f"{project_dir}/{e['path']}"), e["find"], e["replace"])
        if err:
            problems.append(err)
        else:
            edited.append(e["path"])
    if base and edited:
        env.sandbox.write_text("artifacts/changes.diff", _diff(Path(base), env.sandbox.resolve(project_dir), sorted(set(edited))))
    if not written and not edited and not base:
        raise ToolError("nothing to materialize: no files, edits or base project")
    return {"project_dir": project_dir, "written": sorted(set(written)), "edited": sorted(set(edited)),
            "base": base, "problems": problems}
