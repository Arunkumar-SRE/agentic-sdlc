# Submission: Agentic Software Engineering System (URL shortener use case)

This document answers the brief's final note in its own order: approach, key decisions, implementation details,
setup, assumptions and trade-offs. Setup and every command are in the [README](README.md); architecture in depth is
in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Review it in 10 minutes

From the repository folder in PowerShell (Python 3.11+; no API key needed):

| Step | Command | What you should see |
|---|---|---|
| 1. Setup (required, once) | `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` | `76 passed`, then `Setup complete` |
| A. The URL shortener | `powershell -ExecutionPolicy Bypass -File scripts\demo_url_shortener.ps1` | Create, 3 redirects (302), analytics, 409/422/404, health, golden signals with 4 SLOs OK; open `http://127.0.0.1:8000/ops/dashboard` while it waits |
| B. The agentic workflow | `.\.venv\Scripts\python.exe -m agentic_sdlc run -r "Build a scalable URL shortener service with APIs, persistence, and analytics."` | Three `Approve?` prompts (type `y`), then `SUCCEEDED` and the path of `engineering_summary.md` |
| C. Golden signals under load | `.\.venv\Scripts\python.exe scripts\golden_signals_report.py` | Four SLO verdicts and `reports\golden_signals_report.md` |
| Results without running | Open `examples/*/artifacts/engineering_summary.md` and `docs/observability/` | Greenfield, brownfield, ambiguous, recovery and out-of-domain runs; before/after/chaos golden-signal reports |

After setup, A, B and C are independent; run any of them. Golden signals are built into the service and always on
while it runs; C only adds load and writes a report.

## 1. How I approached the problem

I treated the assignment as two deliverables that prove each other:

1. **An agentic SDLC workflow** that turns a requirement into a reviewable engineering outcome: normalised
   requirement with ambiguities, impact analysis for existing code, a task plan, design, API contract, code, tests,
   docs, verification, a risk register and a summary a human signs off.
2. **A runnable URL shortener**, which is both the mandatory use case and the code the workflow produces and then
   tests.

Three principles shaped every choice:

- **Verified by execution, not by the model's opinion.** The workflow builds the generated project in a sandbox and
  runs lint, a contract check against the API the service actually serves, and the full test suite. Failures go
  back to code generation with the exact findings (at most two repair rounds).
- **Autonomy between gates.** Agents work alone on each step inside hard limits (typed outputs, sandbox, budgets,
  tool allowlists); a human approves the interpretation, the plan and the final result. Low-confidence
  requirements stop and ask instead of guessing.
- **Agents are configuration.** Adding a step is a YAML entry, a prompt file and an output schema; the engine never
  changes. The engine itself sits behind an interface.

## 2. Key decisions

| Decision | Why | Alternative considered |
|---|---|---|
| Own a small orchestration core (~1,500 lines) instead of LangChain/LangGraph/CrewAI/AutoGen | Every control path (dependencies, retries, gates, repair loop, checkpoints) is explicit, auditable and pinned by tests. For production I would evaluate LangGraph or a durable workflow platform on persistence, observability and maintenance cost; the `Engine` interface allows that swap | LangGraph (its concepts map one-to-one: state graph, checkpointer, interrupts, conditional edges, cycles) |
| Workflow as a dependency graph, run by readiness | Independent work runs in parallel (code, tests and docs from one contract); a verification failure re-opens upstream steps | A linear pipeline |
| Contract first; tests generated from the contract, not the code | Tests written from code inherit its bugs. The recovery example shows a contract-based test catching a real defect | Tests generated after the code |
| Deterministic final report | The summary is assembled from validated outputs, so it cannot invent results | A model-written summary |
| Default model provider is an offline reference implementation | The demo and tests run with no key, no network and the same result every time. A real provider (Anthropic) plugs in behind the same `LLMClient` interface; agents only name a model tier | Requiring an API key to evaluate |
| Out-of-domain requests refused, not faked | Given a non-URL-shortener requirement, the offline provider plans it, then stops at design with a clear message rather than returning the wrong app | Silently returning the reference app |
| URL shortener: random 7-character base62 codes, UNIQUE constraint + retry | 3.5 trillion codes, no coordination between replicas, race-safe (no check-then-insert window) | Counter or hash-based codes |
| 302 with `Cache-Control: no-store` | Every click reaches the service, so analytics are accurate | 301 (cached by browsers; hides repeat clicks) |
| Click writes best-effort | A failed analytics write never breaks a redirect; it is counted in a metric | Failing the redirect |
| Golden signals and SLOs built into the service | Latency, traffic, errors and saturation measured per request, four SLOs with error budgets, live dashboard | Metrics only, no SLO evaluation |

## 3. Implementation details

**Workflow** (`agentic_sdlc/workflows/sdlc.yaml`): 12 steps in one dependency graph.

```
requirements (gate) -> impact (brownfield only) -> plan (gate) -> design -> contract
  -> codegen + testgen + docs (parallel) -> materialize (sandbox build) -> verify (lint, contract, pytest)
  -> risks -> report (sign-off gate)            verify failure -> back to codegen, max 2 rounds
```

**Components**

| Part | Where | What it does |
|---|---|---|
| Engine | `agentic_sdlc/engines/simple.py` | Readiness-based scheduling on a thread pool; retries with backoff; schema-error self-correction; halt / continue / fallback per step; human gates; bounded feedback loop; stale-result protection; checkpoint after every step, resume |
| Core | `agentic_sdlc/core/` | Workflow state, dependency graph, config loading, model router + providers, budgets, response cache, sandbox, approval policies, structured logging |
| Agents | `agentic_sdlc/agents/` | Prompt templates and Pydantic output schemas only (plans must be DAGs, generated code must parse) |
| Tools | `agentic_sdlc/tools/` | Registered with `@tool`: file access, build, lint, pytest, contract check, AST codebase analysis, report |
| URL shortener | `targets/url_shortener/` | FastAPI + SQLAlchemy; create, redirect, analytics, health, readiness, Prometheus metrics, golden signals, rate limiting |

**Brownfield reasoning:** the codebase is parsed (AST) to map routes, services and ORM models, trace request flows
and rank impacted modules. Changes are exact edits applied to a copy of the repository, never the original; a
unified diff is produced for review, and all pre-existing tests must still pass.

**Observability:** one middleware records every request; `/ops/golden-signals` computes the four golden signals
and SLO status with error budgets; `/ops/dashboard` shows them live; `scripts/golden_signals_report.py` load-tests
and writes a report. The first load test found a real tail-latency breach (redirect p99 393 ms against 100 ms,
caused by SQLite write-lock contention); write-ahead logging brought it to about 75 ms. Details:
[docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).

**Example scenarios** (`examples/`, each with every artifact and the console output)

| Example | Result |
|---|---|
| Greenfield: the mandatory requirement | Succeeded; 14 generated tests pass; served API matches the contract |
| Brownfield: add a per-link click limit | Succeeded; 5 impacted modules found; edits on a copy; 12 existing + 6 new tests pass; `changes.diff` |
| Ambiguous: "Make links better and track stuff." | Halted at confidence 0.26 with questions; with `--accept-assumptions` it proceeds on recorded assumptions |
| Recovery: injected faults, malformed output, a real bug | Succeeded after retries, a schema correction and one repair round |
| Out of domain: a todo-list API | Analysis and plan succeed; design stops with a clear "needs a real model" message; nothing is built |

**Testing:** 76 framework tests (engine behaviour, configuration and safety, provider, end-to-end runs, and the
brief's constraints as tests: no agent frameworks, only allowed libraries, every function type-hinted, model ids
only in configuration), plus 12 tests for the URL shortener. Strict mypy and ruff are clean.

## 4. Setup instructions

Python 3.11+ on Windows, from the repository folder:

1. **Setup (required, once):** `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` creates `.venv`, installs
   the dependencies and runs the tests.
2. **Then any of:**
   - **A. URL shortener demo:** `powershell -ExecutionPolicy Bypass -File scripts\demo_url_shortener.ps1`
   - **B. Agentic workflow:** `.\.venv\Scripts\python.exe -m agentic_sdlc run -r "Build a scalable URL shortener service with APIs, persistence, and analytics."` (type `y` at the three approval prompts)
   - **C. Golden signals report:** `.\.venv\Scripts\python.exe scripts\golden_signals_report.py`
3. **Run the service by hand:** `cd targets\url_shortener`, then `..\..\.venv\Scripts\python.exe -m uvicorn app.main:app`,
   then open `http://127.0.0.1:8000/docs` (API) or `http://127.0.0.1:8000/ops/dashboard` (golden signals, live).

Linux/macOS commands and every option are in the [README](README.md).

## 5. Assumptions

- The mandatory requirement leaves scale, analytics dimensions and datastore open. The system records its working
  assumptions: about 1,000 redirects per second peak and 10 million links; total clicks, last access, clicks per
  day and top referrers (no geo or device data); SQLite for the prototype, PostgreSQL in production.
- Single region; no authentication in the prototype (creation is rate-limited instead).
- Reviewers run it locally without an API key, hence the offline reference provider as the default.

## 6. Trade-offs and limitations

- **Offline provider:** it reasons with rules and supplies a prepared URL-shortener implementation, so the offline
  demo proves the orchestration, verification, repair loop and controls rather than a model's code quality. Real
  generation is `--provider anthropic`, through the same interface and the same checks; that path is tested
  against a stand-in SDK, not a live key.
- **Repair loop scope:** it re-opens code generation only. A wrong generated test cannot be repaired
  automatically; the run stops after two rounds and flags a human.
- **Engine:** single machine (threads, local checkpoint file). A distributed or durable engine fits behind the same
  interface.
- **URL shortener is prototype-grade, as the brief allows:** SQLite, in-process rate limiter and metrics, no
  authentication, no migrations tool. The production path is in the design: PostgreSQL with read replicas, a Redis
  cache for popular codes, click events on a queue with batch writes, gateway rate limiting, and burn-rate alerting
  on the SLOs across replicas.
- **Size:** the framework is more elaborate than a minimal answer, because the brief asks for orchestration depth,
  controlled autonomy and validation. The URL shortener is kept small so the workflow's behaviour is easy to see.
