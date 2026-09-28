# Architecture

## 1. Components

```
                 workflows/sdlc.yaml  (agents, tiers, budgets, gates, loops)
                            |
   cli.py --> runner.py --> WorkflowSpec (validated: DAG, tiers, schemas, prompts resolved at load time)
                            |
                        RunContext  ------------------------------------------------------+
                        |  llm: ModelRouter  -> LLMClient (offline | anthropic | yours)  |
                        |  tools: ToolRegistry (@tool, allowlisted per agent)            |
                        |  sandbox: workspace/<run-id>/   budget: BudgetTracker          |
                        |  approvals: ApprovalPolicy     log: structlog -> logs/*.jsonl  |
                        +----------------------------------------------------------------+
                            |
                     Engine.execute(workflow, state, ctx)      <- engines/simple.py (default)
                            |
               Agent.run(state, ctx) -> BaseModel             <- LLMAgent | ToolAgent built from YAML
                            |
                     WorkflowState (outputs, step records, feedback, approvals, events) -> state.json
```

| Package | Responsibility | Knows about |
|---|---|---|
| `core/` | State, graph, config models, the Agent protocol and the two generic agent kinds, the LLM interface and router, budgets, cache, sandbox, gates, logging | nothing domain-specific |
| `agents/` | Prompt templates and Pydantic output schemas. Schema validators are guardrails (plan is a DAG, code parses, paths stay relative) | `core.graph`, `core.errors` only (enforced by a test) |
| `engines/` | Scheduling, retries, failure policy, gates, feedback loops, checkpointing | the Agent protocol, not individual agents |
| `tools/` | Capabilities with side effects, registered by decorator: filesystem, build, test, lint, contract, AST analysis, reporting | the sandbox |
| `workflows/` | Which agents exist, their order, models and policies | everything, as data |

## 2. The workflow (`sdlc.yaml`)

```
requirements --(gate: approve interpretation; escalate if confidence < 0.6)
     |
   impact   (only if brownfield: analyze_codebase tool -> model reasoning; fallback if it fails)
     |
    plan  --(gate: approve plan)        output validated as a DAG, levels + critical path computed
     |
   design
     |
  contract   (OpenAPI + DDL: the single source of truth)
   /   |    \
codegen testgen docs      <- run concurrently; tests are written from the contract, not from the code
   \   |    /
  materialize (tool)   build the project in the sandbox; brownfield = copy target + exact edits + changes.diff
     |
   verify (tool)       edits applied? lint (compile + ruff)? served OpenAPI == contract? pytest green?
     |   \__ fail -> feedback loop: re-open codegen (+ descendants) with the findings, max 2 rounds
     |              environment failures (missing libraries) are reported, never "repaired"
   risks
     |
   report (tool) --(gate: human sign-off)   deterministic summary from validated outputs
```

The whole run is one DAG with 12 agents and 10 levels. Independent work overlaps. Verification gives a
cross-step result: it can send work back upstream, so the flow is not a straight line.

## 3. Execution model (engines/simple.py)

**Scheduling is readiness-driven, not level-by-level.** On every loop the engine starts each `PENDING` step whose
dependencies are all satisfied (`succeeded`, `degraded` or `skipped`), on a thread pool (`max_parallel`). It then
waits for the first completion. All state mutation happens on the main thread. Workers get a deep copy of the state,
so agents are pure functions of `(state, ctx)`.

Step lifecycle:

```
PENDING -> (when false) -> SKIPPED
PENDING -> RUNNING -> attempts -> SUCCEEDED -> gate? -> feedback? 
                          |                    | rejected -> FAILED (re-runs on resume), run HALTED
                          |                    | low confidence & not accepted -> escalated, HALTED
                          +-> exhausted -> on_failure: halt -> FAILED, run FAILED
                                                       continue -> FAILED, dependents BLOCKED
                                                       fallback -> DEGRADED (static output from YAML)
```

**Error taxonomy decides retries** (`core/errors.py`):

* `RetryableError` (rate limit, network, truncation, injected fault) is retried with exponential backoff.
* `OutputValidationError` is retried too, and the validation error is added to the agent's feedback. The next
  prompt says exactly what was wrong, so the model self-corrects.
* Any other `AgenticError`, or an unexpected exception, fails fast: retrying a deterministic failure repeats it.
* `BudgetExceeded` and `HumanRejected` halt the whole run.

**Feedback loops (cross-step coordination).** When a step with `feedback:` finishes with `passed: false`, the engine:

1. Appends its findings to the target's feedback.
2. Resets the target and all its descendants to `PENDING`, and bumps their generation counter.
3. Re-schedules them.

A result from a step that was reset while it was running is recognised as stale by its generation and discarded.
Rounds are bounded (`max_rounds`). A `skip_field` (here `environment_error`) stops the loop for failures that the
upstream step cannot fix.

**Checkpoint and resume.** `state.json` is written atomically after every transition. `--resume` keeps completed
steps and resets `running`, `failed` and `blocked` ones to pending, so the budget carries over. A step rejected at a
gate is `failed`, so it runs again, and the reviewer's note is in its feedback.

## 4. LLM access

Agents never name a model. They name a **tier** (`fast`, `standard`, `strong`). The workflow maps each tier to a
`(provider, model)` pair, and `--provider` / `--model` override it at run time. Anthropic model ids live only in YAML
(a test enforces this). `ModelRouter.structured()` does the following, in order:

1. Checks the budget before spending.
2. Consults the content-addressed cache. The key is (provider fingerprint, model, system, prompt, JSON schema).
3. Calls the provider.
4. Charges the tokens.
5. Validates the response against the schema; invalid output raises `OutputValidationError`.
6. Stores the validated result in the cache.

Providers implement one method, `complete(LLMRequest) -> LLMResponse`:

* `anthropic` uses a forced tool call whose `input_schema` is the agent's Pydantic JSON schema, so output is
  structured by construction.
* `offline` is a deterministic reference implementation used for demos and tests. It receives the same request plus
  the structured values the prompt was rendered from. Its fingerprint includes its own source, so changing its rules
  invalidates its cache.

## 5. Key decisions and trade-offs

| Decision | Why | Alternative rejected |
|---|---|---|
| Own ~1.5k-line orchestrator | Brief forbids agent frameworks. Small enough to read in an afternoon. Every control path is tested. | LangGraph / CrewAI (forbidden; also hide control flow) |
| Agents are data (YAML + prompt + schema) | Another team extends by configuration. The engine stays generic. | Agent subclasses per role (code changes for every new agent) |
| One protocol method `run(state, ctx)` | The narrowest contract that lets engines be swapped | Rich lifecycle hooks (more coupling) |
| Contract-first, tests from the contract, in parallel with code | Tests are not shaped by the implementation's bugs. Verification can detect drift. | Tests generated from the code (they encode its bugs) |
| Verify by executing (build + lint + served-OpenAPI diff + pytest) | Evidence, not model self-assessment | LLM "reviewer" judging its own output |
| Bounded feedback loop to codegen | Real repair with a hard cap on cost | Unbounded agent loops |
| Deterministic final report | The summary cannot hallucinate. It is rendered from validated outputs. | Model-written summary |
| Threads + stdlib in the default engine | Zero extra dependencies. Agents are I/O-bound. | asyncio (every provider and tool would need async), Celery/Temporal (heavy for a prototype; can sit behind `Engine`) |
| Brownfield = exact find/replace edits on a copy | Small, reviewable diffs. Mismatches are detected and fed back. The original is never touched. | Whole-file rewrites (large diffs, silent regressions) |
| Offline reference provider as default | Reproducible demo and CI with no key | Recorded cassettes (brittle when prompts change) |

## 6. Security and safety boundaries

* **Writes:** only under `workspace/<run-id>/`. Absolute paths, `..`, symlink escapes, `.env`, `.git` and key files
  are refused, and so is any write over 512 KB.
* **Reads:** only the workspace plus explicit read roots (the brownfield target).
* **Subprocesses** (pytest, OpenAPI extraction, ruff) run inside the project copy with timeouts and an in-memory
  database URL.
* **Tools:** per-agent allowlist, and arguments validated against the tool's signature.
* **Budget:** the whole run is capped, which bounds cost and runaway loops.

## 7. Extending to production

* Swap `engines/simple.py` for a durable engine (a queue or a workflow service) behind `Engine`. `WorkflowState` is
  already serialisable.
* Add providers via `@register_provider`. Route tiers to cheaper or faster models per agent in YAML.
* Replace `AutoApproval` / `InteractiveApproval` with a policy that posts to a chat or ticketing system and waits.
* Run verification in a container per run instead of a subprocess.
