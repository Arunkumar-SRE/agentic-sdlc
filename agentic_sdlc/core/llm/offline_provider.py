"""Offline reference provider: a deterministic stand-in for a model, keyed by output schema.

Why it exists: the demo and the test suite must run with no network and no API key, and give the same answer
every time. It implements the same LLMClient interface as the Anthropic provider and its answers pass through
the same schema validation, gates, verification and repair loop. It reasons with rules, not a model: it is
strongest on URL-shortener requirements and says so in its notes when it cannot produce code for something else.

Demo switches (provider options, or CLI flags):
  inject_defect:   first code-generation round contains a real bug; verification catches it; the repair round fixes it
  malformed_once:  schema names whose first answer is deliberately invalid (shows schema rejection + self-correction)
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import PACKAGE_ROOT
from ..errors import ConfigError, UnsupportedByProvider
from . import LLMRequest, LLMResponse, estimate_tokens, register_provider

Handler = Callable[["OfflineReferenceClient", LLMRequest], dict[str, Any]]
_HANDLERS: dict[str, Handler] = {}


def handles(schema_name: str) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        _HANDLERS[schema_name] = fn
        return fn

    return decorator


class OfflineReferenceClient:
    name = "offline"

    def __init__(self, options: dict[str, Any]) -> None:
        ref = Path(str(options.get("reference_dir", "targets/url_shortener")))
        self.reference_dir = ref if ref.is_absolute() else (PACKAGE_ROOT.parent / ref)
        self.inject_defect = bool(options.get("inject_defect", False))
        self._malformed = set(options.get("malformed_once", []))
        self._lock = threading.Lock()
        # Options, this module's source and the reference files: changing any of them invalidates cached answers.
        self.fingerprint = hashlib.sha256(json.dumps(
            {k: v for k, v in options.items() if k != "models"}, sort_keys=True, default=str).encode()
            + Path(__file__).read_bytes()
            + b"".join(f.read_bytes() for f in sorted(self.reference_dir.rglob("*.py")))).hexdigest()[:16]

    def complete(self, request: LLMRequest) -> LLMResponse:
        handler = _HANDLERS.get(request.schema_name)
        if handler is None:
            raise ConfigError(f"offline provider has no handler for schema '{request.schema_name}'")
        with self._lock:
            malformed = request.schema_name in self._malformed
            self._malformed.discard(request.schema_name)
        data = {"note": "deliberately malformed first answer"} if malformed else handler(self, request)
        return LLMResponse(data=data, provider=self.name, model=request.model,
                           input_tokens=estimate_tokens(request.system + request.prompt),
                           output_tokens=estimate_tokens(json.dumps(data)))

    def read_reference(self, relative: str) -> str:
        path = self.reference_dir / relative
        if not path.exists():
            raise ConfigError(f"offline provider reference file missing: {path}")
        return path.read_text(encoding="utf-8")


@register_provider("offline")
def _factory(options: dict[str, Any]) -> OfflineReferenceClient:
    return OfflineReferenceClient(options)


# ====================================================================== helpers
def _req(request: LLMRequest) -> dict[str, Any]:
    out: dict[str, Any] = request.context.get("outputs", {}).get("requirements", {})
    return out


def _is_max_clicks(text: str) -> bool:
    return bool(re.search(r"max[_ ]?clicks|click limit|limit (the )?(number of )?clicks|limit per link", text, re.I))


def _round(request: LLMRequest) -> int:
    return int(request.metadata.get("round", 0))


DOMAIN = re.compile(r"\b(url|urls|links?|short(en(er|ing)?)?|shortener|redirects?|alias(es)?|clicks?)\b", re.I)


def _in_domain(client: OfflineReferenceClient, request: LLMRequest, text: str | None = None) -> bool:
    """The offline provider only has domain knowledge for URL shorteners (greenfield, or changes to its reference repo)."""
    req = _req(request)
    text = text if text is not None else str(req.get("original") or request.context.get("requirement", ""))
    if DOMAIN.search(text):
        return True
    target = request.context.get("inputs", {}).get("target_dir")
    brownfield_on_reference = req.get("change_type") == "brownfield" and target and \
        Path(str(target)).resolve() == client.reference_dir.resolve()
    return bool(brownfield_on_reference)


def _guard(client: OfflineReferenceClient, request: LLMRequest) -> None:
    """Refuse honestly instead of answering an unrelated requirement with the URL-shortener reference."""
    if not _in_domain(client, request):
        raise UnsupportedByProvider(
            "the offline reference provider only has domain knowledge for URL shorteners, so it cannot design or build "
            "this requirement. Re-run with a real model: --provider anthropic (needs ANTHROPIC_API_KEY). "
            "Requirement analysis and planning above are still valid.")


# ====================================================================== requirement understanding
BROWNFIELD = re.compile(r"\b(existing|enhance|refactor|fix(es|ed)?|bugs?|patch|extend|migrate|update|modify|improve|better|"
                        r"optimi[sz]e|change)\b|\badd(ing)?\b.{0,60}\bto\b", re.I)
VAGUE = re.compile(r"\b(better|improve[d]?|nice|good|modern|stuff|things?|etc|whatever|somehow|smart|awesome)\b", re.I)
BUILD = re.compile(r"\b(build|create|implement|develop|design)\b", re.I)
STOP = set("""a an and the to of in on for with by from at as is are be it its this that these those or not no add build make
new existing enhance improve support allow should must can will when once per each so than then into also get set use via any all
more less very service system api apis url urls link links shortener code codes return returns report reports better stuff
thing things track reached plus under""".split())

GOALS: list[tuple[str, str, str]] = [
    (r"\bapis?\b|endpoint|\brest\b", "Expose REST APIs to create and query short links",
     "POST /api/v1/urls returns 201 with a short_code; malformed or non-http(s) URLs return 422"),
    (r"redirect|short(en)?", "Redirect short codes to long URLs with correct 404/410 semantics",
     "GET /{code} returns 302 to the long URL; unknown code returns 404"),
    (r"persist|database|\bstore\b|storage", "Persist links and click events durably",
     "Links and clicks are stored in a relational database with a UNIQUE short_code"),
    (r"analytic|click|track|stats?\b", "Record and expose click analytics",
     "GET /api/v1/urls/{code}/analytics reports total clicks, last access, clicks by day and top referrers"),
    (r"expir|ttl", "Support link expiry", "A link past expires_at returns 410"),
    (r"alias", "Support custom aliases with conflict detection", "A taken alias returns 409; reserved aliases return 422"),
    (r"max[_ ]?clicks|click limit|limit per link", "Enforce a per-link click limit",
     "A link with max_clicks=N redirects N times, then returns 410; analytics shows remaining clicks"),
    (r"scal", "Design for horizontal scalability",
     "API is stateless; lookups use an indexed unique key; the path to Postgres, caching and async analytics is documented"),
]


def _ambiguities(text: str, change_type: str) -> list[dict[str, str]]:
    t = text.lower()
    out: list[dict[str, str]] = []

    def has(pattern: str) -> bool:
        return re.search(pattern, t) is not None

    if has(r"scal|high[- ]traffic|\bfast(er)?\b|performance") and not has(r"\d+\s*(k|m|rps|qps|req|requests|users|links)\b"):
        out.append({"topic": "scale target", "question": "What load must it sustain (peak redirects/sec, total links, retention)?",
                    "assumption": "~1K redirects/sec peak and ~10M links; stateless API behind a load balancer; PostgreSQL in production"})
    if has(r"analytic|track|stats?\b|metrics") and not has(r"referr|geo|countr|device|unique|daily|hourly|per day|click|remaining|count"):
        out.append({"topic": "analytics dimensions", "question": "Which analytics: totals, referrers, geo, devices, time series?",
                    "assumption": "Total clicks, last access, recent clicks, clicks per day and top referrers; no geo or device data"})
    if has(r"persist|database|\bstore\b") and not has(r"sqlite|postgres|mysql|dynamo|redis|mongo|cassandra"):
        out.append({"topic": "datastore", "question": "Which datastore and durability guarantees?",
                    "assumption": "SQLite behind SQLAlchemy for the prototype; PostgreSQL is the production target"})
    if change_type == "brownfield" and has(r"\b(limit|max|ttl|expir\w*)\b") and not has(
            r"\bdefault\b|\b\d+\s*(clicks?|days?|hours?|minutes?|seconds?|times?)\b"):
        out.append({"topic": "limit defaults", "question": "What is the default, and what happens when the limit is unset?",
                    "assumption": "Unset means unlimited (backward compatible); an exhausted link returns 410"})
    if VAGUE.search(text):
        out.append({"topic": "scope", "question": 'Which outcomes define "better": expiry, analytics, QR codes, custom domains, performance?',
                    "assumption": "Interpret as link expiry plus click analytics; anything else is out of scope until clarified"})
    return out


@handles("RequirementSpec")
def requirement(_: OfflineReferenceClient, request: LLMRequest) -> dict[str, Any]:
    text = str(request.context.get("requirement", "")).strip()
    forced = request.context.get("inputs", {}).get("mode")
    change_type = forced if forced in ("greenfield", "brownfield") else ("brownfield" if BROWNFIELD.search(text) else "greenfield")
    if re.search(r"\b(fix(es|ed)?|bugs?|crash|broken|regression|defect)\b", text, re.I):
        intent = "bugfix"
    elif re.search(r"\b(refactor|restructure|clean ?up|modulari[sz]e)\b", text, re.I):
        intent = "refactor"
    elif re.search(r"\b(docs?|documentation|readme)\b", text, re.I) and not BUILD.search(text):
        intent = "docs"
    elif re.search(r"\b(tests?|coverage|testing)\b", text, re.I) and not BUILD.search(text):
        intent = "tests"
    else:
        intent = "feature"
    if intent != "feature" and change_type == "greenfield":
        change_type = "brownfield"  # fixes, refactors, tests and docs act on existing code

    ambs = _ambiguities(text, change_type)
    confidence = 1 - 0.12 * len(ambs) - (0.3 if VAGUE.search(text) else 0) - (0.2 if len(text.split()) < 8 else 0)
    confidence = round(min(max(confidence, 0.05), 0.98), 2)
    clarity = "ambiguous" if confidence < 0.6 else "well_defined"
    goal_src = text + (" " + " ".join(a["assumption"] for a in ambs) if clarity == "ambiguous" else "")
    if not DOMAIN.search(text) and change_type == "greenfield":
        ambs.append({"topic": "domain", "question": "The offline reference provider has no domain knowledge for this requirement.",
                     "assumption": "Design and build need a real model provider (--provider anthropic)"})
        return {
            "original": text, "change_type": change_type, "intent": intent, "clarity": clarity, "normalized": f"{text}",
            "goals": [f"Deliver: {text}"],
            "acceptance_criteria": ["Acceptance criteria to be derived by a model provider for this domain"],
            "ambiguities": ambs, "assumptions": [a["assumption"] for a in ambs],
            "out_of_scope": [], "keywords": [], "confidence": confidence,
        }
    matched = [(g, ac) for pat, g, ac in GOALS if re.search(pat, goal_src, re.I)] or [(GOALS[3][1], GOALS[3][2])]
    goals = [g for g, _ in matched]
    acceptance = [ac for _, ac in matched]
    if change_type == "brownfield":
        acceptance.append("All pre-existing tests still pass (no regression) and existing API fields are unchanged")
    keywords = []
    for w in re.findall(r"[a-z_]+", text.lower()):
        if w not in STOP and len(w) > 2 and w not in keywords:
            keywords.append(w)
    normalized = (f"Under stated assumptions: {'; '.join(a['assumption'] for a in ambs[:3])}." if clarity == "ambiguous"
                  else f"{change_type.capitalize()} {intent}: {'; '.join(goals)}.")
    return {
        "original": text, "change_type": change_type, "intent": intent, "clarity": clarity, "normalized": normalized,
        "goals": goals, "acceptance_criteria": acceptance, "ambiguities": ambs,
        "assumptions": [a["assumption"] for a in ambs] + ["Single region", "No authentication in the prototype; creation is rate-limited"],
        "out_of_scope": ["Authentication and multi-tenancy", "Malicious-URL scanning", "Custom domains"],
        "keywords": keywords, "confidence": confidence,
    }


# ====================================================================== codebase impact
@handles("CodebaseImpact")
def impact(_: OfflineReferenceClient, request: LLMRequest) -> dict[str, Any]:
    report = request.context.get("tools", {}).get("analyze_codebase", {})
    if not report.get("applicable", True):
        return {"applicable": False, "summary": report.get("note", "no codebase available"), "impacted_modules": []}
    req = _req(request)
    impacted = [{"path": m["module"], "role": m["role"], "symbols": m["symbols"],
                 "reason": f"identifiers match {', '.join(m['matched_keywords'])} (score {m['score']})"} for m in report.get("impacted", [])]
    flows = [{"entrypoint": f["entrypoint"], "chain": [f["handler"], *f["reachable"]], "models": f["models"]} for f in report.get("flows", [])]
    notes = ["Redirect path: route -> services.resolve_url -> services.record_click (click insert is best-effort)",
             "Create path: route -> services.create_short_url (UNIQUE constraint + retry, no check-then-insert)"]
    risks = []
    if _is_max_clicks(req.get("original", "")):
        notes.append("A click limit must be enforced atomically in resolve_url, before record_click, or concurrent redirects overshoot")
        risks.append("Concurrency: a read-then-write click counter can exceed max_clicks under parallel redirects")
    risks.append("Schema change on urls needs a migration for existing rows (nullable / server default)")
    inv = report.get("inventory", {})
    summary = (f"Scanned {len(inv.get('modules', []))} modules, {len(inv.get('routes', []))} routes, {len(inv.get('models', []))} ORM models. "
               f"{len(impacted)} module(s) impacted; {len(report.get('tests_to_update', []))} test file(s) to update.")
    return {"applicable": True, "summary": summary, "impacted_modules": impacted, "flows": flows, "data_flow_notes": notes,
            "tests_to_update": [t["test_file"] for t in report.get("tests_to_update", [])], "risks": risks}


# ====================================================================== decomposition
def _t(tid: str, title: str, owner: str, deps: list[str], deliverable: str, acceptance: str) -> dict[str, Any]:
    return {"id": tid, "title": title, "owner_step": owner, "depends_on": deps, "deliverable": deliverable, "acceptance": acceptance}


@handles("TaskPlan")
def plan(_: OfflineReferenceClient, request: LLMRequest) -> dict[str, Any]:
    req = _req(request)
    analytics = any("analytic" in g.lower() for g in req.get("goals", []))
    if req.get("change_type") == "greenfield":
        tasks = [
            _t("T1", "Design architecture and data model", "design", [], "Design doc, decisions, SLOs", "Every goal maps to a component"),
            _t("T2", "Define API contract and DDL", "contract", ["T1"], "OpenAPI 3 + SQLite/Postgres DDL", "Every endpoint in the design is in the contract"),
            _t("T3", "Implement persistence layer", "codegen", ["T2"], "ORM models, UNIQUE short_code, click index", "Tables match the DDL"),
            _t("T4", "Implement service layer and routes", "codegen", ["T3"], "Create, redirect, error mapping", "Served OpenAPI matches the contract"),
            _t("T6", "Add operability guardrails", "codegen", ["T4"], "health/ready/metrics, rate limit, JSON logs", "Probes and metrics respond"),
            _t("T7", "Write unit, integration and contract tests from the contract", "testgen", ["T2"], "pytest suites", "Every acceptance criterion has a test"),
            _t("T8", "Write service documentation", "docs", ["T2"], "README with API and run instructions", "Docs list every endpoint"),
            _t("T9", "Verify: lint, contract drift, test suite", "verify", ["T6", "T7"], "Verification report", "All checks pass (repair loop on failure)"),
            _t("T10", "Risk review and validation strategy", "risks", ["T9"], "Risk register", "Every risk has a mitigation or is accepted"),
            _t("T11", "Engineering summary for human sign-off", "report", ["T8", "T10"], "engineering_summary.md", "Reviewer approves"),
        ]
        if analytics:
            tasks.insert(4, _t("T5", "Implement click analytics", "codegen", ["T4"], "Click capture + aggregates endpoint", "Analytics acceptance criteria pass"))
            next(t for t in tasks if t["id"] == "T9")["depends_on"] = ["T5", "T6", "T7"]
        return {"template": "greenfield-feature", "tasks": tasks, "rationale": (
            "Contract first: the API contract and DDL are fixed before code, so implementation and tests can be produced in parallel "
            "from the same source of truth and verification can detect drift. Tests are written from the contract, not from the "
            "implementation, so they catch implementation bugs instead of encoding them.")}
    intent = req.get("intent", "feature")
    templates: dict[str, tuple[str, list[dict[str, Any]]]] = {
        "feature": ("brownfield-feature", [
            _t("B1", "Impact analysis of the existing code", "impact", [], "Impacted modules, flows, tests", "Every impacted route has a flow"),
            _t("B2", "Change design (backward compatible)", "design", ["B1"], "Per-module change plan", "No existing field removed or renamed"),
            _t("B3", "Contract and schema delta", "contract", ["B2"], "OpenAPI + DDL with the new fields", "New fields optional"),
            _t("B4", "Targeted code edits", "codegen", ["B3"], "Exact edits to impacted modules", "All edits apply cleanly"),
            _t("B5", "Regression + new behaviour tests", "testgen", ["B3"], "New pytest cases", "New behaviour covered"),
            _t("B6", "Full regression, contract and lint", "verify", ["B4", "B5"], "Verification report", "Old and new tests pass"),
            _t("B7", "Risk review", "risks", ["B6"], "Risk register", "Migration and concurrency risks addressed"),
            _t("B8", "Changelog and summary", "report", ["B7"], "Engineering summary", "Reviewer approves")]),
        "bugfix": ("brownfield-bugfix", [
            _t("X1", "Locate the defect via impact analysis", "impact", [], "Suspect modules and flows", "Root cause hypothesis"),
            _t("X2", "Reproduce with a failing test", "testgen", ["X1"], "Failing test", "Fails before the fix"),
            _t("X3", "Minimal fix", "codegen", ["X1"], "Code edit", "Smallest change that passes"),
            _t("X4", "Full regression and contract check", "verify", ["X2", "X3"], "Verification report", "All tests pass"),
            _t("X5", "RCA note", "report", ["X4"], "Summary", "Reviewer approves")]),
        "refactor": ("brownfield-refactor", [
            _t("R1", "Dependency and impact analysis", "impact", [], "Module graph", "Scope bounded"),
            _t("R2", "Characterisation tests", "testgen", ["R1"], "Behaviour-pinning tests", "Pass before refactor"),
            _t("R3", "Refactor in small steps", "codegen", ["R1"], "Edits", "No API change"),
            _t("R4", "Verify behaviour unchanged", "verify", ["R2", "R3"], "Verification report", "Same tests pass"),
            _t("R5", "Summary", "report", ["R4"], "Summary", "Reviewer approves")]),
        "tests": ("brownfield-tests", [
            _t("E1", "Identify coverage gaps", "impact", [], "Gap list", "Gaps ranked"),
            _t("E2", "Write tests for gaps", "testgen", ["E1"], "pytest cases", "Gaps covered"),
            _t("E3", "Run and stabilise", "verify", ["E2"], "Verification report", "Suite green"),
            _t("E4", "Summary", "report", ["E3"], "Summary", "Reviewer approves")]),
        "docs": ("brownfield-docs", [
            _t("D1", "Audit docs against code", "impact", [], "Drift list", "Every route documented"),
            _t("D2", "Update documentation", "docs", ["D1"], "Docs", "Examples match the API"),
            _t("D3", "Verify nothing else changed", "verify", ["D2"], "Verification report", "Suite green"),
            _t("D4", "Summary", "report", ["D3"], "Summary", "Reviewer approves")]),
    }
    template, tasks = templates[intent]
    return {"template": template, "tasks": tasks, "rationale": (
        "Impact first: changes are scoped by static analysis of the existing code, applied as exact edits to a copy of the "
        "repository, and accepted only if the pre-existing suite still passes alongside the new tests.")}


# ====================================================================== design
@handles("DesignSpec")
def design(client: OfflineReferenceClient, request: LLMRequest) -> dict[str, Any]:
    _guard(client, request)
    req = _req(request)
    impact_out = request.context.get("outputs", {}).get("impact") or {}
    max_clicks = _is_max_clicks(req.get("original", ""))
    change_plan: list[str] = []
    if req.get("change_type") == "brownfield":
        for m in impact_out.get("impacted_modules", []):
            change_plan.append(f"{m['path']} ({m['role']}): {m['reason']}; symbols: {', '.join(m['symbols']) or 'new code'}")
        if max_clicks:
            change_plan += [
                "models.URL: add max_clicks INTEGER NULL and click_count INTEGER NOT NULL DEFAULT 0",
                "schemas: URLCreate.max_clicks (optional, >= 1); URLResponse.max_clicks; AnalyticsResponse.max_clicks/remaining_clicks",
                "services.resolve_url: consume one click with a conditional UPDATE (click_count < max_clicks); 0 rows -> 410 LinkExhausted",
                "services.get_analytics: report max_clicks and remaining_clicks",
            ]
        change_plan.append("Backward compatibility: new request fields optional, no existing field changed; verified by the old suite")
    data_model = {"urls": ["id PK", "short_code VARCHAR(16) UNIQUE INDEX", "long_url TEXT", "created_at", "expires_at NULL"],
                  "clicks": ["id PK", "url_id FK", "clicked_at", "referrer NULL", "user_agent NULL", "INDEX(url_id, clicked_at)"]}
    if max_clicks:
        data_model["urls"] += ["max_clicks NULL", "click_count DEFAULT 0"]
    decisions = [
        {"decision": "Random 7-char base62 codes", "rationale": "62^7 ~ 3.5e12 space, no coordination between replicas, unguessable",
         "alternative": "Counter + base62 (predictable, needs a coordinated sequence) or URL hash (collision/dedupe semantics)"},
        {"decision": "Insert relying on the UNIQUE constraint, retry on conflict", "rationale": "Race-safe; check-then-insert has a TOCTOU window",
         "alternative": "SELECT then INSERT"},
        {"decision": "302 with Cache-Control: no-store", "rationale": "Every hit reaches the service, so analytics stay accurate",
         "alternative": "301 (cacheable and faster, but hides repeat hits)"},
        {"decision": "Click writes synchronous but best-effort", "rationale": "A failed click write is metered and logged but never fails the redirect",
         "alternative": "Queue + batch consumer (needed above ~1K writes/sec)"},
        {"decision": "SQLite for the prototype, PostgreSQL for production", "rationale": "Zero-setup demo; SQLAlchemy makes it a DATABASE_URL change",
         "alternative": "Start on Postgres (heavier setup)"},
        {"decision": "Domain errors in the service layer, mapped to HTTP at the edge", "rationale": "Business logic testable without the web framework",
         "alternative": "HTTPException inside services"},
    ]
    if max_clicks:
        decisions.append({"decision": "Atomic conditional UPDATE for the click limit",
                          "rationale": "One statement both checks and consumes, so parallel redirects cannot overshoot",
                          "alternative": "Read count then write (races) or SELECT ... FOR UPDATE (row lock, not portable to SQLite)"})
    endpoints = [
        {"method": "POST", "path": "/api/v1/urls", "purpose": "Create a short URL (optional alias, expiry" + (", max_clicks)" if max_clicks else ")")},
        {"method": "GET", "path": "/{short_code}", "purpose": "302 redirect and record a click; 404 unknown, 410 expired" + (" or exhausted" if max_clicks else "")},
        {"method": "GET", "path": "/api/v1/urls/{short_code}/analytics", "purpose": "Click analytics"},
        {"method": "GET", "path": "/health", "purpose": "Liveness"},
        {"method": "GET", "path": "/ready", "purpose": "Readiness (database reachable)"},
        {"method": "GET", "path": "/metrics", "purpose": "Prometheus metrics"},
        {"method": "GET", "path": "/ops/golden-signals", "purpose": "Latency, traffic, errors, saturation and SLO status"},
    ]
    return {
        "summary": "Stateless FastAPI service over a relational store: a create path, a hot redirect path that records clicks "
                   "best-effort, and an analytics read path, with health, readiness, metrics and rate limiting built in.",
        "components": ["FastAPI app (stateless, horizontally scalable)", "Service layer (framework-free, domain errors)",
                       "SQLAlchemy persistence (SQLite prototype / PostgreSQL production)",
                       "Observability middleware (request id, JSON access log, Prometheus metrics and latency histogram)",
                       "Golden signals and SLO evaluation (/ops/golden-signals, live /ops/dashboard)", "Per-client fixed-window rate limiter"],
        "endpoints": endpoints, "data_model": data_model, "decisions": decisions,
        "tradeoffs": ["Analytics consistency vs redirect availability: availability wins",
                      "Synchronous click writes (simple) vs queued writes (throughput): simple for the prototype",
                      "In-memory rate limiter is per-process: correct for one replica; use Redis or the gateway for many",
                      "Random codes need collision retries; counters would not, but they couple replicas"],
        "scaling_path": ["Scale API replicas behind a load balancer", "PostgreSQL with read replicas and PgBouncer",
                         "Redis read-through cache for hot code -> URL", "Queue + batch writer for clicks; time-partitioned clicks table",
                         "Rate limiting at the gateway (Redis)", "Multi-region with per-region caches"],
        "slos": ["Redirect availability 99.9% over 30 days", "Redirect p99 < 100 ms server-side",
                 "Create success rate 99.5% excluding 4xx", "Click-write loss < 0.1% of redirects"],
        "change_plan": change_plan,
    }


# ====================================================================== contract
def _openapi(max_clicks: bool) -> dict[str, Any]:
    create_props: dict[str, Any] = {
        "long_url": {"type": "string", "format": "uri", "minLength": 1, "maxLength": 2083},
        "custom_alias": {"type": "string", "minLength": 3, "maxLength": 16, "pattern": "^[A-Za-z0-9_-]+$", "nullable": True},
        "expires_at": {"type": "string", "format": "date-time", "nullable": True, "description": "Must be in the future"},
    }
    url_props: dict[str, Any] = {"short_code": {"type": "string"}, "short_url": {"type": "string"}, "long_url": {"type": "string"},
                                 "created_at": {"type": "string", "format": "date-time"},
                                 "expires_at": {"type": "string", "format": "date-time", "nullable": True}}
    analytics_props: dict[str, Any] = {
        "short_code": {"type": "string"}, "long_url": {"type": "string"}, "total_clicks": {"type": "integer"},
        "created_at": {"type": "string", "format": "date-time"}, "expires_at": {"type": "string", "format": "date-time", "nullable": True},
        "last_accessed": {"type": "string", "format": "date-time", "nullable": True},
        "recent_clicks": {"type": "array", "items": {"type": "object"}}, "clicks_by_day": {"type": "array", "items": {"type": "object"}},
        "top_referrers": {"type": "array", "items": {"type": "object"}}}
    if max_clicks:
        create_props["max_clicks"] = {"type": "integer", "minimum": 1, "nullable": True, "description": "Unset = unlimited"}
        url_props["max_clicks"] = {"type": "integer", "nullable": True}
        analytics_props["max_clicks"] = {"type": "integer", "nullable": True}
        analytics_props["remaining_clicks"] = {"type": "integer", "nullable": True}
    err = {"description": "Error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}

    def ok(ref: str, code: str = "200") -> dict[str, Any]:
        return {code: {"description": "OK", "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{ref}"}}}}}

    return {
        "openapi": "3.1.0",
        "info": {"title": "URL Shortener", "version": "1.1.0" + ("+max-clicks" if max_clicks else "")},
        "paths": {
            "/api/v1/urls": {"post": {"summary": "Create short URL", "operationId": "create_url",
                                      "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/URLCreate"}}}},
                                      "responses": {**ok("URLResponse", "201"), "409": err, "422": err, "429": err, "503": err}}},
            "/api/v1/urls/{short_code}/analytics": {"get": {"summary": "Click analytics", "operationId": "analytics",
                                                            "parameters": [{"name": "short_code", "in": "path", "required": True, "schema": {"type": "string"}}],
                                                            "responses": {**ok("AnalyticsResponse"), "404": err}}},
            "/{short_code}": {"get": {"summary": "Redirect", "operationId": "redirect",
                                      "parameters": [{"name": "short_code", "in": "path", "required": True, "schema": {"type": "string"}}],
                                      "responses": {"302": {"description": "Redirect to long URL"}, "404": err, "410": err}}},
            "/health": {"get": {"summary": "Liveness", "responses": {"200": {"description": "OK"}}}},
            "/ready": {"get": {"summary": "Readiness", "responses": {"200": {"description": "OK"}, "503": {"description": "DB unavailable"}}}},
            "/metrics": {"get": {"summary": "Prometheus metrics", "responses": {"200": {"description": "text/plain"}}}},
            "/ops/golden-signals": {"get": {"summary": "Golden signals and SLO status", "operationId": "golden_signals",
                                            "responses": {"200": {"description": "Latency, traffic, errors, saturation, SLOs"}}}},
        },
        "components": {"schemas": {
            "URLCreate": {"type": "object", "required": ["long_url"], "properties": create_props},
            "URLResponse": {"type": "object", "properties": url_props},
            "AnalyticsResponse": {"type": "object", "properties": analytics_props},
            "Error": {"type": "object", "properties": {"detail": {}}},
        }},
    }


def _ddl(dialect: str, max_clicks: bool) -> str:
    pk = "SERIAL PRIMARY KEY" if dialect == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "TIMESTAMP" if dialect == "postgresql" else "DATETIME"
    extra = ",\n    max_clicks   INTEGER NULL,\n    click_count  INTEGER NOT NULL DEFAULT 0" if max_clicks else ""
    return (f"CREATE TABLE urls (\n    id           {pk},\n    short_code   VARCHAR(16) NOT NULL UNIQUE,\n    long_url     TEXT NOT NULL,\n"
            f"    created_at   {ts} NOT NULL,\n    expires_at   {ts} NULL{extra}\n);\n"
            f"CREATE TABLE clicks (\n    id           {pk},\n    url_id       INTEGER NOT NULL REFERENCES urls(id),\n"
            f"    clicked_at   {ts} NOT NULL,\n    referrer     VARCHAR(512) NULL,\n    user_agent   VARCHAR(512) NULL\n);\n"
            "CREATE INDEX ix_clicks_url_id_clicked_at ON clicks (url_id, clicked_at);")


@handles("ApiContract")
def contract(client: OfflineReferenceClient, request: LLMRequest) -> dict[str, Any]:
    _guard(client, request)
    req = _req(request)
    mc = _is_max_clicks(req.get("original", ""))
    notes = ["Additive only: every new field is optional; no path, method or existing field changed"] if req.get("change_type") == "brownfield" else []
    if mc:
        notes.append("Migration for existing rows: ALTER TABLE urls ADD COLUMN max_clicks INTEGER NULL; "
                     "ALTER TABLE urls ADD COLUMN click_count INTEGER NOT NULL DEFAULT 0")
    return {"openapi": _openapi(mc), "ddl": {"sqlite": _ddl("sqlite", mc), "postgresql": _ddl("postgresql", mc)}, "compatibility_notes": notes}


# ====================================================================== code generation
GREENFIELD_FILES = ["app/__init__.py", "app/database.py", "app/models.py", "app/schemas.py", "app/services.py",
                    "app/metrics.py", "app/ratelimit.py", "app/ops.py", "app/main.py"]
EXPIRY_CHECK = "    if url.expires_at and url.expires_at <= models.utcnow():\n        raise LinkExpired()\n"

MAX_CLICKS_EDITS: list[dict[str, str]] = [
    {"path": "app/models.py", "find": "    expires_at = Column(DateTime, nullable=True)\n",
     "replace": "    expires_at = Column(DateTime, nullable=True)\n    max_clicks = Column(Integer, nullable=True)  # NULL = unlimited\n"
                "    click_count = Column(Integer, nullable=False, default=0, server_default=\"0\")\n"},
    {"path": "app/schemas.py",
     "find": '    expires_at: Optional[datetime] = Field(None, description="ISO-8601; must be in the future. Naive values are treated as UTC.")\n',
     "replace": '    expires_at: Optional[datetime] = Field(None, description="ISO-8601; must be in the future. Naive values are treated as UTC.")\n'
                '    max_clicks: Optional[int] = Field(None, ge=1, description="Redirect limit; unset means unlimited.")\n'},
    {"path": "app/schemas.py",
     "find": "    created_at: datetime\n    expires_at: Optional[datetime] = None\n\n\nclass ClickEvent",
     "replace": "    created_at: datetime\n    expires_at: Optional[datetime] = None\n    max_clicks: Optional[int] = None\n\n\nclass ClickEvent"},
    {"path": "app/schemas.py", "find": "    last_accessed: Optional[datetime] = None\n",
     "replace": "    last_accessed: Optional[datetime] = None\n    max_clicks: Optional[int] = None\n    remaining_clicks: Optional[int] = None\n"},
    {"path": "app/services.py", "find": "from sqlalchemy import desc, func, select\n", "replace": "from sqlalchemy import desc, func, select, update\n"},
    {"path": "app/services.py", "find": 'class LinkExpired(ServiceError):\n    status_code, detail = 410, "Short URL expired"\n',
     "replace": 'class LinkExpired(ServiceError):\n    status_code, detail = 410, "Short URL expired"\n\n\n'
                'class LinkExhausted(ServiceError):\n    status_code, detail = 410, "Short URL click limit reached"\n'},
    {"path": "app/services.py", "find": "long_url=str(payload.long_url), expires_at=payload.expires_at)",
     "replace": "long_url=str(payload.long_url), expires_at=payload.expires_at,\n                         max_clicks=payload.max_clicks)"},
    {"path": "app/services.py", "find": "            expires_at=url.expires_at,\n        )\n",
     "replace": "            expires_at=url.expires_at,\n            max_clicks=url.max_clicks,\n        )\n"},
    {"path": "app/services.py", "find": "    long_url, url_id = url.long_url, url.id  # capture before commit expires the instance\n",
     "replace": "    long_url, url_id, limited = url.long_url, url.id, url.max_clicks is not None  # capture before commit expires the instance\n"
                "    if limited:\n"
                "        # Check and consume in one statement so concurrent redirects cannot overshoot the limit.\n"
                "        consumed = db.execute(update(models.URL)\n"
                "                              .where(models.URL.id == url_id, models.URL.click_count < models.URL.max_clicks)\n"
                "                              .values(click_count=models.URL.click_count + 1))\n"
                "        db.commit()\n"
                "        if consumed.rowcount == 0:\n"
                "            metrics.inc(\"click_limit_reached_total\")\n"
                "            raise LinkExhausted()\n"},
    {"path": "app/services.py", "find": "        last_accessed=last,\n",
     "replace": "        last_accessed=last,\n        max_clicks=url.max_clicks,\n"
                "        remaining_clicks=None if url.max_clicks is None else max(url.max_clicks - url.click_count, 0),\n"},
]


@handles("CodeBundle")
def code(client: OfflineReferenceClient, request: LLMRequest) -> dict[str, Any]:
    _guard(client, request)
    req = _req(request)
    first_round = _round(request) == 0
    defect = client.inject_defect and first_round
    if req.get("change_type") == "greenfield":
        files = [{"path": p, "content": client.read_reference(p)} for p in GREENFIELD_FILES]
        files.append({"path": "requirements.txt", "content": "fastapi>=0.115\nuvicorn>=0.30\nsqlalchemy>=2.0\npydantic>=2.6\nhttpx>=0.27\npytest>=8\n"})
        notes = []
        if defect:
            services = next(f for f in files if f["path"] == "app/services.py")
            services["content"] = services["content"].replace(EXPIRY_CHECK, "")
            notes.append("round 1 (demo: --inject-defect): expiry check omitted")
        elif not first_round:
            notes.append("repair round: restored the expiry check in services.resolve_url based on verification feedback")
        return {"summary": "FastAPI + SQLAlchemy URL shortener: models, schemas, services, routes, metrics, rate limiter",
                "files": files, "edits": [], "notes": notes}
    if _is_max_clicks(req.get("original", "")):
        edits = [dict(e) for e in MAX_CLICKS_EDITS]
        notes = []
        if defect:
            edits[-2]["find"] = edits[-2]["find"].replace("capture before commit", "capture before the commit")
            notes.append("round 1 (demo: --inject-defect): one edit targets text that does not exist")
        elif not first_round:
            notes.append("repair round: corrected the resolve_url edit anchor from verification feedback")
        return {"summary": "Per-link max_clicks: model columns, request/response fields, atomic enforcement, analytics",
                "files": [], "edits": edits, "notes": notes}
    return {"summary": "No code change produced", "files": [], "edits": [],
            "notes": ["The offline reference provider only synthesises code for URL-shortener requirements it recognises "
                      "(greenfield service, max_clicks). Run with --provider anthropic for other changes; the plan, "
                      "impact analysis and design above still apply."]}


# ====================================================================== tests
def _contract_test(openapi: dict[str, Any]) -> str:
    ops = {p: sorted(m for m in item if m in ("get", "post", "put", "patch", "delete")) for p, item in openapi.get("paths", {}).items()}
    fields = sorted(openapi.get("components", {}).get("schemas", {}).get("URLCreate", {}).get("properties", {}))
    return (
        '"""Generated from the agreed API contract: fails on contract drift."""\n'
        "from app.main import app\n\n"
        f"CONTRACT_OPERATIONS = {json.dumps(ops, indent=4, sort_keys=True)}\n"
        f"CREATE_FIELDS = {json.dumps(fields)}\n\n\n"
        "def test_every_contract_operation_is_served():\n"
        '    served = app.openapi()["paths"]\n'
        "    missing = [f\"{m.upper()} {p}\" for p, methods in CONTRACT_OPERATIONS.items() for m in methods if m not in served.get(p, {})]\n"
        "    assert not missing, missing\n\n\n"
        "def test_create_request_accepts_contract_fields():\n"
        '    props = app.openapi()["components"]["schemas"]["URLCreate"]["properties"]\n'
        "    assert set(CREATE_FIELDS) <= set(props), set(CREATE_FIELDS) - set(props)\n"
    )


MAX_CLICKS_TESTS = '''"""Generated for the max_clicks enhancement."""
URL = "https://example.com/limited"


def create(client, **extra):
    return client.post("/api/v1/urls", json={"long_url": URL, **extra})


def test_limit_enforced_then_410(client):
    r = create(client, max_clicks=2)
    assert r.status_code == 201 and r.json()["max_clicks"] == 2
    code = r.json()["short_code"]
    statuses = [client.get(f"/{code}", follow_redirects=False).status_code for _ in range(3)]
    assert statuses == [302, 302, 410]


def test_analytics_reports_remaining_clicks(client):
    code = create(client, max_clicks=3).json()["short_code"]
    client.get(f"/{code}", follow_redirects=False)
    a = client.get(f"/api/v1/urls/{code}/analytics").json()
    assert a["total_clicks"] == 1 and a["max_clicks"] == 3 and a["remaining_clicks"] == 2


def test_unlimited_by_default_is_backward_compatible(client):
    r = create(client)
    assert r.status_code == 201 and r.json()["max_clicks"] is None
    code = r.json()["short_code"]
    assert all(client.get(f"/{code}", follow_redirects=False).status_code == 302 for _ in range(5))
    assert client.get(f"/api/v1/urls/{code}/analytics").json()["remaining_clicks"] is None


def test_max_clicks_must_be_positive(client):
    assert create(client, max_clicks=0).status_code == 422
'''


@handles("TestBundle")
def tests(client: OfflineReferenceClient, request: LLMRequest) -> dict[str, Any]:
    _guard(client, request)
    req = _req(request)
    contract_doc = request.context.get("outputs", {}).get("contract", {}).get("openapi", {})
    files = [{"path": "tests/test_contract.py", "content": _contract_test(contract_doc)}]
    strategy: dict[str, list[str]] = {"contract": ["Every operation in the agreed OpenAPI is served", "Create accepts every contract field"]}
    if req.get("change_type") == "greenfield":
        files += [{"path": f"tests/{n}", "content": client.read_reference(f"tests/{n}")} for n in ("__init__.py", "conftest.py", "test_api.py")]
        strategy["unit/service"] = ["Alias and expiry validation incl. timezone-aware input", "Collision retry and exhaustion (503)",
                                    "Click-write failure does not break the redirect", "Rate limit returns 429"]
        strategy["integration"] = ["Create -> redirect -> analytics through the ASGI app with an isolated in-memory DB",
                                   "404 / 409 / 410 / 422 semantics", "Health, readiness, metrics, request id"]
    else:
        strategy["regression"] = ["The pre-existing suite is copied with the repository and must still pass"]
        if _is_max_clicks(req.get("original", "")):
            files.append({"path": "tests/test_max_clicks.py", "content": MAX_CLICKS_TESTS})
            strategy["new behaviour"] = ["N redirects then 410", "remaining_clicks in analytics", "unset = unlimited", "max_clicks >= 1"]
    return {"strategy": strategy, "files": files, "covers_acceptance": req.get("acceptance_criteria", [])}


# ====================================================================== docs
@handles("DocBundle")
def docs(client: OfflineReferenceClient, request: LLMRequest) -> dict[str, Any]:
    _guard(client, request)
    req = _req(request)
    design_out = request.context.get("outputs", {}).get("design", {})
    rows = "\n".join(f"| {e['method']} | `{e['path']}` | {e['purpose']} |" for e in design_out.get("endpoints", []))
    readme = (f"# URL shortener (generated)\n\n{design_out.get('summary', '')}\n\n## Run\n\n```bash\npip install -r requirements.txt\n"
              "uvicorn app.main:app --reload   # http://127.0.0.1:8000/docs\npytest -q\n```\n\n"
              f"## API\n\n| Method | Path | Purpose |\n|---|---|---|\n{rows}\n\n"
              "Configuration: `DATABASE_URL` (default SQLite file; PostgreSQL URL in production), `RATE_LIMIT_PER_MIN` (0 disables).\n")
    files = [{"path": "README.md" if req.get("change_type") == "greenfield" else "docs/SERVICE.md", "content": readme}]
    if req.get("change_type") == "brownfield":
        entries = "\n".join(f"- {c}" for c in design_out.get("change_plan", []))
        files.append({"path": "CHANGELOG.md", "content": f"# Changelog\n\n## Unreleased\n\n{req.get('normalized', '')}\n\n{entries}\n"})
    return {"files": files}


# ====================================================================== risks
@handles("RiskRegister")
def risks(client: OfflineReferenceClient, request: LLMRequest) -> dict[str, Any]:
    _guard(client, request)
    req = _req(request)
    outputs = request.context.get("outputs", {})
    verified = bool(outputs.get("verify", {}).get("passed"))
    tested = "verified_by_test" if verified else "mitigated_in_code"
    items = [
        ("Short-code collision", "low", "medium", "UNIQUE constraint + up to 5 retries, then 503; collision counter metric", tested),
        ("Analytics failure breaks redirects", "medium", "high", "Click write isolated; click_write_failures_total metric", tested),
        ("Timezone bug on expiry", "medium", "medium", "Normalise to naive UTC at the schema boundary; reject past expiry", tested),
        ("Alias shadows a route (e.g. /docs)", "medium", "medium", "Reserved alias list; alias charset excludes dots and slashes", tested),
        ("Creation floods / abuse", "high", "medium", "Per-client rate limit (429); auth and URL scanning are documented next steps", tested),
        ("Hot links overload the database", "medium", "high", "Read-through Redis cache for code -> URL; CDN for very hot links", "documented"),
        ("Analytics write amplification", "high", "medium", "Queue + batch inserts, pre-aggregated counters, time-partitioned clicks", "documented"),
        ("SQLite single-writer limit", "high", "low", "Accepted for the prototype; PostgreSQL in production", "accepted"),
        ("Generated code merged without review", "medium", "high", "Sandboxed workspace, verification gate and human sign-off before merge", "mitigated_in_code"),
    ]
    if _is_max_clicks(req.get("original", "")):
        items.insert(0, ("Click limit overshoot under concurrency", "medium", "medium",
                         "Single conditional UPDATE consumes a click only while click_count < max_clicks", tested))
        items.insert(1, ("Migration on a populated urls table", "medium", "medium",
                         "Nullable max_clicks and server default 0 for click_count: no backfill, no lock-heavy rewrite", "documented"))
    rounds = sum(1 for e in request.context.get("run", {}).get("events", []) if e.get("kind") == "feedback_loop")
    checks = outputs.get("verify", {}).get("checks", {})
    strategy = [f"{name}: {c.get('summary')}" for name, c in checks.items()]
    strategy += ["Tests are generated from the contract, independently of the code, so they are not shaped by its bugs",
                 "Contract drift is checked against the OpenAPI the generated service actually serves",
                 f"Repair loop: {rounds} round(s) used this run (max 2) before the result reached review",
                 "Human gates on requirement interpretation, plan, and final sign-off",
                 "Live smoke test before merge: uvicorn + curl create/redirect/analytics"]
    return {
        "risks": [{"name": n, "likelihood": lk, "impact": im, "mitigation": m, "status": s} for n, lk, im, m, s in items],
        "failure_scenarios": [
            {"scenario": "Database unavailable", "detection": "/ready returns 503; 5xx rate", "behaviour": "Fail fast; liveness stays green"},
            {"scenario": "Click insert fails", "detection": "click_write_failures_total > 0", "behaviour": "Redirect still served; click lost and logged"},
            {"scenario": "Collision storm", "detection": "short_code_collisions_total spikes", "behaviour": "Retries, then 503 so clients back off"},
            {"scenario": "Create flood from one client", "detection": "rate_limited_total", "behaviour": "429 for that client only"},
            {"scenario": "Model returns invalid output", "detection": "Schema validation in the router", "behaviour": "Rejected; retried with the error; fallback or halt"},
            {"scenario": "Generated code fails verification", "detection": "verify step", "behaviour": "Feedback loop re-opens code generation (bounded rounds)"},
        ],
        "validation_strategy": strategy,
        "limitations": ["Prototype: SQLite, in-process rate limiter and metrics, no auth, no migrations tool",
                        "Offline reference provider: deterministic rules and a curated reference implementation; real synthesis needs a model provider"],
    }
