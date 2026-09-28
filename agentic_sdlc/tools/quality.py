"""Quality tools: run_pytest, lint, and verify (the composite check whose result drives the repair loop)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .contract import check_contract
from .registry import ToolEnv, tool

# ruff rules that indicate broken code, not style: syntax errors, undefined names, invalid comparisons.
BLOCKING_RULES = "E9,F63,F7,F82"


def _parse_junit(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"total": 0, "failed": 0, "errors": 0, "skipped": 0, "failures": []}
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = sum(int(s.get("tests", 0)) for s in suites)
    failed = sum(int(s.get("failures", 0)) for s in suites)
    errors = sum(int(s.get("errors", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    failures: list[dict[str, str]] = []
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is not None:
                message = (node.get("message") or node.text or "").strip().splitlines()
                failures.append({"test": f"{case.get('classname')}::{case.get('name')}", "kind": kind,
                                 "message": (message[0] if message else "")[:300]})
    return {"total": total, "failed": failed, "errors": errors, "skipped": skipped, "failures": failures}


@tool(side_effects="exec")
def run_pytest(env: ToolEnv, project_dir: str = "project", target: str = "tests", timeout_s: int = 300) -> dict[str, Any]:
    """Run pytest inside the workspace project in a subprocess with a timeout; return structured results."""
    cwd = env.sandbox.resolve(project_dir)
    report = cwd / ".pytest-report.xml"
    report.unlink(missing_ok=True)
    path = os.pathsep.join(p for p in (str(cwd), os.environ.get("PYTHONPATH", "")) if p)
    proc_env = {**os.environ, "PYTHONPATH": path, "DATABASE_URL": "sqlite://", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    own_config = [c for c in ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg") if (cwd / c).exists()]
    isolation: list[str] = ["--rootdir", str(cwd)]
    if not own_config:  # never inherit a config from directories above the generated project
        ini = env.sandbox.write_text(".pytest-isolated.ini", "[pytest]\n")
        isolation += ["-c", str(ini)]
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider", f"--junitxml={report.name}", *isolation],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s, env=proc_env, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"passed": False, "summary": f"pytest timed out after {timeout_s}s", "details": {}}
    results = _parse_junit(report)
    results["exit_code"] = proc.returncode
    results["tail"] = proc.stdout.strip().splitlines()[-15:]
    passed = proc.returncode == 0 and results["total"] > 0
    summary = (f"{results['total']} tests, {results['failed']} failed, {results['errors']} errors"
               if results["total"] else "no tests collected: " + " ".join(results["tail"][-3:]))
    return {"passed": passed, "summary": summary, "details": results}


@tool(side_effects="exec")
def lint(env: ToolEnv, project_dir: str = "project") -> dict[str, Any]:
    """Compile every Python file; if ruff is installed, also fail on undefined names and invalid syntax."""
    root = env.sandbox.resolve(project_dir)
    issues: list[str] = []
    files = [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]
    for path in files:
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as exc:
            issues.append(f"{path.relative_to(root).as_posix()}:{exc.lineno}: {exc.msg}")
    ruff = shutil.which("ruff")
    if ruff and not issues:
        proc = subprocess.run([ruff, "check", "--select", BLOCKING_RULES, "--output-format", "concise", "."],
                              cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False)
        if proc.returncode not in (0, 1):
            issues.append(f"ruff failed: {proc.stderr.strip()[:200]}")
        issues += [line for line in proc.stdout.splitlines() if ":" in line and not line.startswith("Found")]
    checker = "compile + ruff" if ruff else "compile only (install ruff for undefined-name checks)"
    return {"passed": not issues, "summary": f"{len(files)} files, {len(issues)} blocking issue(s) [{checker}]",
            "details": {"issues": issues[:50]}}


@tool(side_effects="exec")
def verify(
    env: ToolEnv,
    contract: dict[str, Any],
    project_dir: str = "project",
    app_import: str = "app.main:app",
    materialize_problems: list[str] | None = None,
) -> dict[str, Any]:
    """Composite verification: edits applied, lint, contract drift, test suite. Produces repair feedback on failure."""
    checks: dict[str, dict[str, Any]] = {}
    problems = materialize_problems or []
    checks["edits_applied"] = {"passed": not problems, "summary": "all edits applied" if not problems else
                               f"{len(problems)} edit(s) failed", "details": {"problems": problems}}
    checks["lint"] = lint(env, project_dir)
    checks["contract"] = check_contract(env, contract, project_dir, app_import)
    checks["tests"] = run_pytest(env, project_dir)

    feedback: list[str] = []
    feedback += [f"Edit could not be applied: {p}" for p in problems]
    feedback += [f"Lint: {i}" for i in checks["lint"]["details"].get("issues", [])[:10]]
    if not checks["contract"]["passed"]:
        d = checks["contract"]["details"]
        feedback += [f"Contract: {x} is not served" for x in d.get("missing_operations", []) + d.get("missing_request_fields", [])]
        if not d:
            feedback.append(f"Contract: {checks['contract']['summary']}")
    for f in checks["tests"]["details"].get("failures", [])[:10]:
        feedback.append(f"Test {f['test']} {f['kind']}: {f['message']}")
    if not checks["tests"]["passed"] and not checks["tests"]["details"].get("failures"):
        feedback.append(f"Tests: {checks['tests']['summary']}")
    missing = _missing_third_party(env, project_dir, checks)
    if missing:
        # The code cannot fix a missing library in the runner's environment: do not spend repair rounds on it.
        return {"passed": False, "checks": checks, "environment_error": f"missing dependencies in the verification "
                f"environment: {', '.join(missing)} (pip install -r requirements.txt)", "feedback": ""}
    return {"passed": all(c["passed"] for c in checks.values()), "checks": checks, "feedback": "\n".join(feedback)}


def _missing_third_party(env: ToolEnv, project_dir: str, checks: dict[str, dict[str, Any]]) -> list[str]:
    text = json.dumps(checks)
    root = env.sandbox.resolve(project_dir)
    names = set(re.findall(r"No module named '([A-Za-z0-9_]+)", text))
    return sorted(n for n in names if not (root / n).exists() and not (root / f"{n}.py").exists())
