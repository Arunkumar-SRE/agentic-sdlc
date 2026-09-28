# Engineering summary

**Run** `ex-4-recovery` - workflow `sdlc` - verdict **ready for review**

## 1. Requirement

> Build a scalable URL shortener service with APIs, persistence, and analytics.

- Classified as **greenfield / feature**, clarity **well_defined**, confidence **0.64**
- Normalised: Greenfield feature: Expose REST APIs to create and query short links; Redirect short codes to long URLs with correct 404/410 semantics; Persist links and click events durably; Record and expose click analytics; Design for horizontal scalability.

### Goals
- Expose REST APIs to create and query short links
- Redirect short codes to long URLs with correct 404/410 semantics
- Persist links and click events durably
- Record and expose click analytics
- Design for horizontal scalability

### Acceptance criteria
- POST /api/v1/urls returns 201 with a short_code; malformed or non-http(s) URLs return 422
- GET /{code} returns 302 to the long URL; unknown code returns 404
- Links and clicks are stored in a relational database with a UNIQUE short_code
- GET /api/v1/urls/{code}/analytics reports total clicks, last access, clicks by day and top referrers
- API is stateless; lookups use an indexed unique key; the path to Postgres, caching and async analytics is documented

### Ambiguities (question -> working assumption)
- **scale target**: What load must it sustain (peak redirects/sec, total links, retention)? -> _~1K redirects/sec peak and ~10M links; stateless API behind a load balancer; PostgreSQL in production_
- **analytics dimensions**: Which analytics: totals, referrers, geo, devices, time series? -> _Total clicks, last access, recent clicks, clicks per day and top referrers; no geo or device data_
- **datastore**: Which datastore and durability guarantees? -> _SQLite behind SQLAlchemy for the prototype; PostgreSQL is the production target_

## 3. Implementation plan and rationale

Contract first: the API contract and DDL are fixed before code, so implementation and tests can be produced in parallel from the same source of truth and verification can detect drift. Tests are written from the contract, not from the implementation, so they catch implementation bugs instead of encoding them.

| Task | Title | Depends on | Produced by | Deliverable | Acceptance |
|---|---|---|---|---|---|
| T1 | Design architecture and data model | - | `design` | Design doc, decisions, SLOs | Every goal maps to a component |
| T2 | Define API contract and DDL | T1 | `contract` | OpenAPI 3 + SQLite/Postgres DDL | Every endpoint in the design is in the contract |
| T3 | Implement persistence layer | T2 | `codegen` | ORM models, UNIQUE short_code, click index | Tables match the DDL |
| T4 | Implement service layer and routes | T3 | `codegen` | Create, redirect, error mapping | Served OpenAPI matches the contract |
| T5 | Implement click analytics | T4 | `codegen` | Click capture + aggregates endpoint | Analytics acceptance criteria pass |
| T6 | Add operability guardrails | T4 | `codegen` | health/ready/metrics, rate limit, JSON logs | Probes and metrics respond |
| T7 | Write unit, integration and contract tests from the contract | T2 | `testgen` | pytest suites | Every acceptance criterion has a test |
| T8 | Write service documentation | T2 | `docs` | README with API and run instructions | Docs list every endpoint |
| T9 | Verify: lint, contract drift, test suite | T5, T6, T7 | `verify` | Verification report | All checks pass (repair loop on failure) |
| T10 | Risk review and validation strategy | T9 | `risks` | Risk register | Every risk has a mitigation or is accepted |
| T11 | Engineering summary for human sign-off | T8, T10 | `report` | engineering_summary.md | Reviewer approves |

Execution levels (tasks in one level can proceed in parallel): [T1] -> [T2] -> [T3, T7, T8] -> [T4] -> [T5, T6] -> [T9] -> [T10] -> [T11]

Critical path: T1 -> T2 -> T3 -> T4 -> T5 -> T9 -> T10 -> T11

### Key design decisions
- **Random 7-char base62 codes**: 62^7 ~ 3.5e12 space, no coordination between replicas, unguessable (instead of: Counter + base62 (predictable, needs a coordinated sequence) or URL hash (collision/dedupe semantics))
- **Insert relying on the UNIQUE constraint, retry on conflict**: Race-safe; check-then-insert has a TOCTOU window (instead of: SELECT then INSERT)
- **302 with Cache-Control: no-store**: Every hit reaches the service, so analytics stay accurate (instead of: 301 (cacheable and faster, but hides repeat hits))
- **Click writes synchronous but best-effort**: A failed click write is metered and logged but never fails the redirect (instead of: Queue + batch consumer (needed above ~1K writes/sec))
- **SQLite for the prototype, PostgreSQL for production**: Zero-setup demo; SQLAlchemy makes it a DATABASE_URL change (instead of: Start on Postgres (heavier setup))
- **Domain errors in the service layer, mapped to HTTP at the edge**: Business logic testable without the web framework (instead of: HTTPException inside services)

## 4. Generated artifacts

- Code: FastAPI + SQLAlchemy URL shortener: models, schemas, services, routes, metrics, rate limiter (10 files, 0 edits)
- Project built in `project/`: 15 files written, 0 edited
- Tests: 4 files (contract, unit/service, integration)
- `artifacts/design.md`
- `artifacts/openapi.json`
- `artifacts/schema.sql`
- `artifacts/test_plan.md`
- `artifacts/risk_register.md`
- `artifacts/verification.md`
- `artifacts/<step>.json`: raw validated output of every step
- `state.json`: checkpoint (resumable)

## 5. Validation

Result: **PASS**

| Check | Result | Summary |
|---|---|---|
| edits_applied | pass | all edits applied |
| lint | pass | 13 files, 0 blocking issue(s) [compile + ruff] |
| contract | pass | served API matches the contract |
| tests | pass | 14 tests, 0 failed, 0 errors |

## 6. Risks, trade-offs and validation approach

| Risk | L | I | Mitigation | Status |
|---|---|---|---|---|
| Short-code collision | low | medium | UNIQUE constraint + up to 5 retries, then 503; collision counter metric | verified_by_test |
| Analytics failure breaks redirects | medium | high | Click write isolated; click_write_failures_total metric | verified_by_test |
| Timezone bug on expiry | medium | medium | Normalise to naive UTC at the schema boundary; reject past expiry | verified_by_test |
| Alias shadows a route (e.g. /docs) | medium | medium | Reserved alias list; alias charset excludes dots and slashes | verified_by_test |
| Creation floods / abuse | high | medium | Per-client rate limit (429); auth and URL scanning are documented next steps | verified_by_test |
| Hot links overload the database | medium | high | Read-through Redis cache for code -> URL; CDN for very hot links | documented |
| Analytics write amplification | high | medium | Queue + batch inserts, pre-aggregated counters, time-partitioned clicks | documented |
| SQLite single-writer limit | high | low | Accepted for the prototype; PostgreSQL in production | accepted |
| Generated code merged without review | medium | high | Sandboxed workspace, verification gate and human sign-off before merge | mitigated_in_code |

Trade-offs:
- Analytics consistency vs redirect availability: availability wins
- Synchronous click writes (simple) vs queued writes (throughput): simple for the prototype
- In-memory rate limiter is per-process: correct for one replica; use Redis or the gateway for many
- Random codes need collision retries; counters would not, but they couple replicas

Validation strategy:
- edits_applied: all edits applied
- lint: 13 files, 0 blocking issue(s) [compile + ruff]
- contract: served API matches the contract
- tests: 14 tests, 0 failed, 0 errors
- Tests are generated from the contract, independently of the code, so they are not shaped by its bugs
- Contract drift is checked against the OpenAPI the generated service actually serves
- Repair loop: 1 round(s) used this run (max 2) before the result reached review
- Human gates on requirement interpretation, plan, and final sign-off
- Live smoke test before merge: uvicorn + curl create/redirect/analytics

## 7. Assumptions and limitations

- ~1K redirects/sec peak and ~10M links; stateless API behind a load balancer; PostgreSQL in production
- Total clicks, last access, recent clicks, clicks per day and top referrers; no geo or device data
- SQLite behind SQLAlchemy for the prototype; PostgreSQL is the production target
- Single region
- No authentication in the prototype; creation is rate-limited
- Prototype: SQLite, in-process rate limiter and metrics, no auth, no migrations tool
- Offline reference provider: deterministic rules and a curated reference implementation; real synthesis needs a model provider
- repair round: restored the expiry check in services.resolve_url based on verification feedback

## 8. Execution trace (controlled autonomy)

| Step | Status | Attempts | Repair rounds | Cached | Seconds |
|---|---|---|---|---|---|
| requirements | succeeded | 1 | 0 |  | 0.02 |
| impact | skipped | 0 | 0 |  | 0.0 |
| plan | succeeded | 2 | 0 |  | 0.5 |
| design | succeeded | 3 | 0 |  | 1.5 |
| contract | succeeded | 1 | 0 |  | 0.0 |
| codegen | succeeded | 2 | 1 |  | 0.03 |
| testgen | succeeded | 1 | 0 |  | 0.01 |
| docs | succeeded | 1 | 0 |  | 0.0 |
| materialize | succeeded | 2 | 0 |  | 0.0 |
| verify | succeeded | 2 | 0 |  | 4.94 |
| risks | succeeded | 1 | 0 |  | 0.0 |
| report | in progress (this report) | 0 | 0 |  | 0.0 |

Human gates:
- requirements (gate): approved by auto auto-approved (unattended run)
- plan (gate): approved by auto auto-approved (unattended run)

Recovery events:
- plan: retry {"attempt": 1, "error": "TaskPlan validation failed: template: Field required; rationale: Field required; tasks: Field required", "backoff_s": 0.5}
- design: fault_injected {"attempt": 1}
- design: retry {"attempt": 1, "error": "injected transient fault", "backoff_s": 0.5}
- design: fault_injected {"attempt": 2}
- design: retry {"attempt": 2, "error": "injected transient fault", "backoff_s": 1.0}
- verify: feedback_loop {"target": "codegen", "round": 1, "reopened": ["codegen", "materialize", "report", "risks", "verify"]} - feedback: Test tests.test_api::test_expired_link_returns_410 failure: AssertionError: assert 302 == 410

Budget used: 10 model calls (0 cache hits), 59875 tokens, 4 tool calls, 7.05 s
