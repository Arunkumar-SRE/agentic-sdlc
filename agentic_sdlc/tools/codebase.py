"""Static, read-only codebase reasoning for brownfield work (AST, no execution of the target code).

Builds an inventory (modules, routes, ORM models), entry-point -> service -> model request flows, and a ranked
impact analysis for a requirement (TF-IDF over identifiers). The result is given to the impact agent as context.
"""

from __future__ import annotations

import ast
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .registry import ToolEnv, tool

STOP = set("""a an and the to of in on for with by from at as is are be it its this that these those or not no
add adding build make making new existing enhance improve support allow should must can will when once per each
so than then into over under also get set use using via any all more most less least very max min limit
service system api apis url urls link links shortener code codes remaining return returns report reports
current feature fix bug change update better stuff thing things track tracking reached reach plus out one two stated assumption assumptions interpret under scope clarified anything else until value total last access recent top""".split())
SYNONYMS = {"expiry": "expire", "ttl": "expire", "expiration": "expire", "expired": "expire", "expires": "expire",
            "analytics": "analytic", "alias": "alias",
            "aliases": "alias", "redirect": "redirect", "redirects": "redirect", "redirected": "redirect"}
HTTP_VERBS = {"get", "post", "put", "delete", "patch"}


def stem(word: str) -> str:
    word = SYNONYMS.get(word, word)
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return [stem(w) for w in re.findall(r"[a-z]+", text.lower()) if w not in STOP and len(w) > 2]


def keywords_from(text: str) -> list[str]:
    seen, out = set(), []
    for tok in tokens(text):
        if tok not in seen and tok not in STOP:
            seen.add(tok)
            out.append(tok)
    return out


class _ModuleScan(ast.NodeVisitor):
    def __init__(self, name: str) -> None:
        self.name = name
        self.imports: set[str] = set()
        self.classes: list[dict[str, Any]] = []
        self.functions: dict[str, dict[str, Any]] = {}
        self.routes: list[dict[str, Any]] = []
        self.idents: list[str] = []
        self.prefixes: dict[str, str] = {}  # router variable -> APIRouter(prefix=...)

    def visit_Assign(self, node: ast.Assign) -> None:
        call = node.value
        if isinstance(call, ast.Call) and getattr(call.func, "id", getattr(call.func, "attr", "")) == "APIRouter":
            for kw in call.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self.prefixes[target.id] = kw.value.value
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.imports.add(alias.name if node.module is None else node.module.split(".")[-1])
            self.imports.add(alias.name)

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.update(a.name.split(".")[-1] for a in node.names)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        table = None
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(getattr(t, "id", "") == "__tablename__" for t in stmt.targets):
                table = getattr(stmt.value, "value", None)
        bases = [getattr(b, "id", getattr(b, "attr", "")) for b in node.bases]
        self.classes.append({"name": node.name, "table": table, "bases": bases})
        self.idents.append(node.name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        calls, refs = set(), set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                    calls.add(f"{fn.value.id}.{fn.attr}")
                elif isinstance(fn, ast.Name):
                    calls.add(fn.id)
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id in {"models", "schemas"}:
                refs.add(f"{sub.value.id}.{sub.attr}")
        self.functions[node.name] = {"calls": sorted(calls), "refs": sorted(refs), "args": [a.arg for a in node.args.args]}
        self.idents.append(node.name)
        self.idents.extend(a.arg for a in node.args.args)
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in HTTP_VERBS and dec.args:
                if isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                    owner = dec.func.value.id if isinstance(dec.func.value, ast.Name) else ""
                    path = self.prefixes.get(owner, "") + dec.args[0].value
                    self.routes.append({"method": dec.func.attr.upper(), "path": path, "function": node.name})
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self.idents.append(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.idents.append(node.attr)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and len(node.value) < 60:
            self.idents.append(node.value)


def _scan(path: Path, root: Path) -> dict[str, Any]:
    rel = path.relative_to(root).as_posix()
    scan = _ModuleScan(path.stem)
    scan.visit(ast.parse(path.read_text(encoding="utf-8")))
    kind = "test" if path.name.startswith("test_") or "tests" in path.parts else "source"
    toks = Counter(t for ident in scan.idents for t in tokens(ident))
    return {"path": rel, "module": path.stem, "kind": kind, "imports": sorted(scan.imports), "classes": scan.classes,
            "functions": scan.functions, "routes": scan.routes, "tokens": toks, "loc": len(path.read_text(encoding="utf-8").splitlines())}


def analyze(project_root: Path, target_dir: Path, requirement_keywords: list[str], analyse_impact: bool) -> dict[str, Any]:
    files = [p for p in sorted(target_dir.rglob("*.py"))
             if p.stat().st_size > 0 and "tests" not in p.relative_to(target_dir).parts and "__pycache__" not in p.parts]
    tests_dir = project_root / "tests"
    if tests_dir.exists():
        files += [p for p in sorted(tests_dir.rglob("test_*.py"))]
    modules = [_scan(p, project_root) for p in files]
    src = [m for m in modules if m["kind"] == "source"]
    by_name = {m["module"]: m for m in src}

    routes = [{**r, "module": m["module"]} for m in src for r in m["routes"]]
    models = [{"class": c["name"], "table": c["table"], "module": m["module"]} for m in src for c in m["classes"] if c["table"]]
    model_names = {m["class"]: m for m in models}

    def resolve(module: str, call: str) -> tuple[str, str] | None:
        if "." in call:
            mod, fn = call.split(".", 1)
            if mod in by_name and fn in by_name[mod]["functions"]:
                return mod, fn
        elif call in by_name[module]["functions"]:
            return module, call
        return None

    flows: list[dict[str, Any]] = []
    for r in routes:
        chain: list[str] = [f"{r['module']}.{r['function']}"]
        models_touched: set[str] = set()
        seen: set[tuple[str, str]] = set()
        frontier = [(r["module"], r["function"])]
        while frontier and len(seen) < 12:
            mod, fn = frontier.pop(0)
            if (mod, fn) in seen:
                continue
            seen.add((mod, fn))
            info = by_name[mod]["functions"][fn]
            for ref in info["refs"]:
                cls = ref.split(".", 1)[1]
                if ref.startswith("models.") and cls in model_names:
                    models_touched.add(f"models.{cls}({model_names[cls]['table']})")
            for call in info["calls"]:
                tgt = resolve(mod, call)
                if tgt and tgt not in seen:
                    frontier.append(tgt)
                    chain.append(f"{tgt[0]}.{tgt[1]}")
        flows.append({"entrypoint": f"{r['method']} {r['path']}", "handler": chain[0], "reachable": chain[1:], "models": sorted(models_touched)})

    report: dict[str, Any] = {"applicable": True,
              "inventory": {"modules": [{k: m[k] for k in ("path", "kind", "loc", "imports")} for m in modules],
                            "routes": routes, "models": models},
              "flows": flows, "impacted": [], "tests_to_update": [], "keywords": requirement_keywords}
    if not analyse_impact:
        return report

    df: dict[str, int] = defaultdict(int)
    for m in src:
        for kw in set(requirement_keywords):
            if kw in m["tokens"]:
                df[kw] += 1
    n = max(len(src), 1)

    def score(m: dict[str, Any]) -> tuple[float, list[str]]:
        hits = {kw: m["tokens"][kw] for kw in requirement_keywords if kw in m["tokens"]}
        s = sum((math.log((n + 1) / (df[kw] + 1)) + 1) * (1 + math.log(c)) for kw, c in hits.items())
        return round(s, 2), sorted(hits)

    for m in src:
        s, hits = score(m)
        if s > 0:
            symbols = sorted({name for name in list(m["functions"]) + [c["name"] for c in m["classes"]] if any(h in tokens(name) for h in hits)})
            role = ("route layer" if m["routes"] else "data model" if any(c["table"] for c in m["classes"])
                    else "schema / DTO" if any("BaseModel" in c["bases"] for c in m["classes"]) else "business logic")
            report["impacted"].append({"module": m["path"], "role": role, "score": s, "matched_keywords": hits, "symbols": symbols})
    report["impacted"].sort(key=lambda i: -i["score"])
    for m in modules:
        if m["kind"] == "test":
            s, hits = score(m)
            if s > 0:
                report["tests_to_update"].append({"test_file": m["path"], "matched_keywords": hits})
    return report


@tool(side_effects="read")
def analyze_codebase(env: ToolEnv, root: str, requirement: str = "", keywords: list[str] | None = None) -> dict[str, Any]:
    """Inventory, request flows and ranked impact for an existing Python service (read-only)."""
    target = env.sandbox.resolve(root, for_write=False)
    if not target.is_dir():
        return {"applicable": False, "note": f"no codebase at {root}", "inventory": {}, "flows": [], "impacted": [],
                "tests_to_update": [], "keywords": []}
    kws = keywords or keywords_from(requirement)
    report = analyze(target, target, kws, analyse_impact=True)
    return report
