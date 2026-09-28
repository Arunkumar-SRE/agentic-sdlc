# agentic-sdlc

An agentic software-engineering system in plain Python 3.11+. It turns a requirement into a reviewable engineering
outcome: normalised requirement, (brownfield) impact analysis, task plan, design, API contract and DDL, code, tests
and docs. It builds that code in a sandbox, verifies it with a bounded repair loop, and produces a risk register and
an engineering summary for human sign-off.

The orchestration is our own: **no LangChain, LangGraph, CrewAI or AutoGen**. The core is small. Another team adopts
it by changing YAML, prompts and schemas, not the engine.

**Reviewers, start here:** [SUBMISSION.md](SUBMISSION.md) covers approach, key decisions, implementation, setup,
assumptions and trade-offs, with a 10-minute review path.

* Architecture, control flow and design decisions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
* Observability, golden signals and SLOs: [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md)
* Worked examples with every generated artifact: [examples/](examples/)

## 1. Quickstart (Windows, 3 commands)

Needs Python 3.11+ ([python.org](https://www.python.org/downloads/), tick "Add python.exe to PATH").

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1               # venv + dependencies + 76 tests
powershell -ExecutionPolicy Bypass -File scripts\demo_url_shortener.ps1  # run the URL shortener and call every endpoint
.\.venv\Scripts\Activate.ps1; python -m agentic_sdlc run -r "Build a scalable URL shortener service with APIs, persistence, and analytics."
```

Linux / macOS:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && python -m pytest
cd targets/url_shortener && uvicorn app.main:app      # the URL shortener on http://127.0.0.1:8000/docs
```

`requirements.txt` has three groups. The **framework** needs pydantic, PyYAML and structlog. The **anthropic** SDK
is optional and only needed for `--provider anthropic`. FastAPI, SQLAlchemy, httpx and pytest are there because the
workflow **builds and tests a FastAPI service**, so verification needs them.

### Run the URL shortener by hand

```powershell
cd targets\url_shortener
uvicorn app.main:app            # open http://127.0.0.1:8000/docs to try every endpoint in the browser
```

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/urls` | Create (optional `custom_alias`, `expires_at`): 201, 409 alias taken, 422 invalid or reserved, 429 rate limited |
| `GET /{short_code}` | 302 redirect + records a click: 404 unknown, 410 expired |
| `GET /api/v1/urls/{short_code}/analytics` | Total clicks, last access, clicks per day, top referrers, recent clicks |
| `GET /health`, `/ready`, `/metrics` | Liveness, readiness (database), Prometheus metrics |
| `GET /ops/golden-signals`, `/ops/dashboard` | Golden signals and SLO status (JSON), live dashboard |

Configuration: `DATABASE_URL` (default a local SQLite file; a PostgreSQL URL in production), `RATE_LIMIT_PER_MIN`
(default 120, `0` disables).

### Observability: golden signals

The service measures latency, traffic, errors and saturation itself and evaluates its SLOs with error budgets:
live at `/ops/dashboard`, JSON at `/ops/golden-signals`, Prometheus at `/metrics`. A load test writes a report:

```powershell
python scripts/golden_signals_report.py            # starts the service, ~630 requests, writes reports/golden_signals_report.md
```

Its first run found a real tail-latency breach (redirect p99 393 ms; SQLite write-lock contention), fixed with
write-ahead logging (p99 about 75 ms). Details, SLO definitions and the before / after / chaos reports:
[docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).

## 2. Run the mandatory use case

```bash
python -m agentic_sdlc run -r "Build a scalable URL shortener service with APIs, persistence, and analytics."
```

Without `--auto-approve` you approve three gates in the terminal:

1. The requirement interpretation.
2. The plan.
3. The final summary.

Output lands in `workspace/<run-id>/`. Start with `artifacts/engineering_summary.md`. The generated service is in
`project/`: `cd` there and run `uvicorn app.main:app` to try it. The structured audit log is `logs/<run-id>.jsonl`.

Exit codes: `0` = succeeded, `1` = failed or needs attention, `2` = halted by a human or an escalation.

## 3. Scenarios (pre-generated in `examples/`, regenerate with `examples/run_examples.ps1` or `.sh`)

| Example | Command (all use `--auto-approve`) | What it demonstrates |
|---|---|---|
| `1-greenfield` | `-r "Build a scalable URL shortener service with APIs, persistence, and analytics."` | Full DAG. Code, tests and docs generated in parallel from one contract. 14 tests executed. Contract matches the served OpenAPI. |
| `2-brownfield` | `-r "Enhance the existing URL shortener: add a max_clicks limit per link; once reached, redirects return 410 and analytics report remaining clicks."` | AST impact analysis (modules, request flows, tests to update). Exact edits applied to a **copy** of `targets/url_shortener`. `changes.diff` for review. 12 old + 6 new tests green. |
| `3a-ambiguous-escalated` | `-r "Make links better and track stuff."` | Confidence 0.26 < 0.6: the unattended run **halts** before planning, with the questions a human must answer. |
| `3b-ambiguous-accepted` | same + `--accept-assumptions` | Proceeds on explicit, recorded assumptions. |
| `4-recovery` | mandatory + `--inject-defect --inject-fault design:2 --malformed-once TaskPlan` | Transient failures retried with backoff. Malformed model output rejected by the schema and corrected. A real bug caught by the tests and fixed by one **repair round**. |
| `5-out-of-domain` | `-r "Build a todo list REST API ..."` | Offline provider has no knowledge of this domain: requirement analysis and plan succeed, then design **stops honestly** with a clear message instead of building the wrong app. Use `--provider anthropic` for other domains. |

Other flags:

* `--reject-at STEP`: simulate a reviewer rejecting a gate.
* `--resume --run-id X`: continue from the checkpoint.
* `--mode greenfield|brownfield`: override change-type detection.
* `--target PATH`: point brownfield work at another codebase.
* `--provider anthropic`: use a real model (needs `ANTHROPIC_API_KEY`).
* `--max-llm-calls N`: cap model calls.
* `--no-cache`: bypass the response cache.
* `python -m agentic_sdlc list` / `validate WORKFLOW`: inspect what is registered, or check a workflow YAML.

## 4. Pluggability (treat as first-class)

| To add... | You change | The engine changes |
|---|---|---|
| an **agent** | an entry in `workflows/*.yaml` + a prompt `.md` + a Pydantic schema | nothing |
| a **tool** | a function decorated with `@tool` in any module listed under `plugins:` | nothing |
| a **model / vendor** | `models:` tiers in YAML (agents only name a tier), or `@register_provider` for a new `LLMClient` | nothing |
| an **engine** | a class with `execute(workflow, state, ctx)` + `@register_engine`, then `engine:` in YAML | n/a |

An agent entry declares its id, prompt template, output schema, model tier, dependencies and retry policy. It can
also declare an `on_failure` policy (`halt | continue | fallback`), a `when` condition, a human `gate`, and a
`feedback` loop. `tests/test_config_and_core.py::test_a_team_adds_an_agent_with_yaml_prompt_and_schema_only` proves
the pattern end to end.

The agent contract is one method: `run(state: WorkflowState, ctx: RunContext) -> BaseModel`. The orchestrator knows
nothing else about an agent.

## 5. Controlled autonomy and guardrails

* **Human gates:** requirement interpretation, plan, and final sign-off. A rejected step re-runs on resume with the
  reviewer's note.
* **Confidence escalation:** low-confidence output halts unattended runs unless assumptions are explicitly accepted.
* **Typed outputs:** every agent output is Pydantic-validated. Plans must be DAGs, code must parse, and paths must be
  relative. Invalid output is rejected and retried with the error.
* **Sandbox:** all writes go under `workspace/<run-id>/`, with path, symlink, size and secret-file guards. The
  brownfield repository is read-only and copied in.
* **Budgets:** model calls, tokens, tool calls and wall-clock time. A call is reserved *before* spending.
* **Least privilege:** each agent may only call the tools its YAML entry names.
* **Bounded repair:** at most 2 feedback rounds. Environment failures (a missing library) are reported, not "fixed".
* **Audit:** a structlog JSON line per event in `logs/`, and `state.json` checkpointed after every transition.

## 6. Testing approach

```bash
python -m pytest                    # 76 tests
mypy                                # strict, configured in pyproject.toml (pip install mypy types-PyYAML)
ruff check agentic_sdlc tests
```

| Suite | Covers |
|---|---|
| `test_engine.py` | Real concurrency of independent branches, dependency order, retry/backoff, schema self-correction, fail-fast, fallback, continue/blocked, gate rejection + resume, confidence escalation, feedback loop (reopen + bounded), budget halt, conditions, cache, fault injection, checkpoints |
| `test_config_and_core.py` | Workflow validation (cycles, unknown deps/tiers/schemas, bad feedback targets), add-an-agent-by-config, graph algorithms, sandbox escapes, strict templating, budgets, tool registry, argument validation, allowlists, edits, AST analysis |
| `test_offline_provider.py` | Requirement classification, every plan template is a DAG, brownfield edits apply exactly once to the real target, defect injection |
| `test_anthropic_provider.py` | Forced tool-call request shape, retryable vs fatal errors, truncation, missing key |
| `test_end_to_end.py` | Full workflow: greenfield, brownfield (original untouched), repair round, recovery, escalation, plan rejection, and an out-of-domain requirement stopping honestly |
| `test_constraints.py` | No agent frameworks imported or required; only allowed third-party imports; **every function type-hinted**; `agents/` holds no orchestration logic; model ids only in config; layout matches the brief |
| `targets/url_shortener/tests` | The reference service itself (12 tests) |

## 7. Assumptions, limitations, trade-offs

* **The default provider is `offline`.** It is a deterministic reference implementation of `LLMClient` so the demo
  and tests run with no key or network, and give the same answer every time. It reasons with rules and replays a
  curated URL-shortener implementation, and it synthesises code only for requirements it recognises: the greenfield
  service and `max_clicks`. Given a requirement outside that domain (say, a todo-list API), it still analyses and
  plans, then refuses at the design step with a clear message instead of building the wrong app:
  `examples/5-out-of-domain` shows it. `--provider anthropic` swaps in a real model through the same interface, and
  its output passes the same validation, gates and verification. That path is tested against a stand-in SDK, not a
  live API.
* The repair loop re-opens `codegen` only. If a generated test is wrong, the loop cannot fix it; it stops at its
  bound and flags the run.
* The simple engine is single-process: threads, with the checkpoint in a local file. A distributed engine plugs in
  behind the same `Engine` interface.
* The generated service is prototype-grade: SQLite, an in-process rate limiter, no auth, and no migration tool.
  The design documents the path to production.

## 8. Layout

```
agentic_sdlc/
  core/        Agent protocol, WorkflowState, TaskGraph, LLMClient (+ providers), budgets, cache, sandbox, gates, config
  agents/      prompt templates + Pydantic output schemas only; no orchestration logic
  engines/     base.py (Engine interface), simple.py (default topological executor, stdlib only)
  tools/       decorator-registered tools: read_file, write_file, list_files, materialize, run_pytest, lint,
               check_contract, verify, analyze_codebase, render_report
  workflows/   sdlc.yaml
  cli.py, runner.py
targets/url_shortener/   reference service: brownfield target and the offline provider's reference implementation
scripts/                 setup.ps1 (one-command Windows setup), demo_url_shortener.ps1 (live API demo),
                         golden_signals_report.py (load test + golden signals report)
workspace/               every generated artifact, sandboxed per run
logs/                    JSON-lines audit log per run
tests/  examples/  docs/
```
