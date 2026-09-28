"""Contract tools: read the OpenAPI the generated code actually serves and diff it against the agreed contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from ..core.errors import ToolError
from .registry import ToolEnv, tool

METHODS = ("get", "post", "put", "patch", "delete")


def served_openapi(env: ToolEnv, project_dir: str, app_import: str, timeout_s: int = 60) -> dict[str, Any]:
    module, _, attr = app_import.partition(":")
    code = (f"import json, sys; sys.path.insert(0, '.'); from {module} import {attr or 'app'} as a; "
            "print(json.dumps(a.openapi()))")
    cwd = env.sandbox.resolve(project_dir)
    proc_env = {**os.environ, "DATABASE_URL": "sqlite://", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    proc = subprocess.run([sys.executable, "-c", code], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout_s, env=proc_env, check=False)
    if proc.returncode != 0:
        raise ToolError(f"could not import {app_import}: {proc.stderr.strip()[-800:]}")
    data: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    return data


def _request_props(doc: dict[str, Any], op: dict[str, Any]) -> set[str]:
    schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    ref = schema.get("$ref")
    if ref:
        schema = doc.get("components", {}).get("schemas", {}).get(ref.rsplit("/", 1)[-1], {})
    return set(schema.get("properties", {}))


def diff_contract(contract: dict[str, Any], served: dict[str, Any]) -> dict[str, Any]:
    missing_ops: list[str] = []
    missing_fields: list[str] = []
    for path, item in contract.get("paths", {}).items():
        for method in METHODS:
            if method not in item:
                continue
            served_op = served.get("paths", {}).get(path, {}).get(method)
            if served_op is None:
                missing_ops.append(f"{method.upper()} {path}")
                continue
            gap = _request_props(contract, item[method]) - _request_props(served, served_op)
            missing_fields += [f"{method.upper()} {path}: request field '{f}'" for f in sorted(gap)]
    extra = [f"{m.upper()} {p}" for p, item in served.get("paths", {}).items() for m in METHODS
             if m in item and m not in contract.get("paths", {}).get(p, {})]
    return {"missing_operations": missing_ops, "missing_request_fields": missing_fields, "undocumented_operations": extra}


@tool(side_effects="exec")
def check_contract(env: ToolEnv, contract: dict[str, Any], project_dir: str = "project", app_import: str = "app.main:app") -> dict[str, Any]:
    """Fail if the generated service does not serve every operation and request field in the agreed contract."""
    try:
        served = served_openapi(env, project_dir, app_import)
    except (ToolError, subprocess.TimeoutExpired) as exc:
        last = str(exc).strip().splitlines()[-1] if str(exc).strip() else repr(exc)
        return {"passed": False, "summary": f"service could not be loaded: {last[:160]}", "details": {"error": str(exc)[-1500:]}}
    diff = diff_contract(contract, served)
    env.sandbox.write_text("artifacts/served_openapi.json", json.dumps(served, indent=2))
    passed = not diff["missing_operations"] and not diff["missing_request_fields"]
    summary = ("served API matches the contract" if passed else
               f"{len(diff['missing_operations'])} missing operation(s), {len(diff['missing_request_fields'])} missing field(s)")
    return {"passed": passed, "summary": summary, "details": diff}
