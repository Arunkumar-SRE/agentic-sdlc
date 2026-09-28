# Engineering summary

**Run** `ex-2-brownfield` - workflow `sdlc` - verdict **ready for review**

## 1. Requirement

> Enhance the existing URL shortener: add a max_clicks limit per link; once reached, redirects return 410 and analytics report remaining clicks.

- Classified as **brownfield / feature**, clarity **well_defined**, confidence **0.88**
- Normalised: Brownfield feature: Redirect short codes to long URLs with correct 404/410 semantics; Record and expose click analytics; Enforce a per-link click limit.

### Goals
- Redirect short codes to long URLs with correct 404/410 semantics
- Record and expose click analytics
- Enforce a per-link click limit

### Acceptance criteria
- GET /{code} returns 302 to the long URL; unknown code returns 404
- GET /api/v1/urls/{code}/analytics reports total clicks, last access, clicks by day and top referrers
- A link with max_clicks=N redirects N times, then returns 410; analytics shows remaining clicks
- All pre-existing tests still pass (no regression) and existing API fields are unchanged

### Ambiguities (question -> working assumption)
- **limit defaults**: What is the default, and what happens when the limit is unset? -> _Unset means unlimited (backward compatible); an exhausted link returns 410_

## 2. Codebase impact (brownfield)

Scanned 9 modules, 10 routes, 2 ORM models. 5 module(s) impacted; 1 test file(s) to update.

| Module | Role | Why | Symbols |
|---|---|---|---|
| `app/ops.py` | route layer | identifiers match click, redirect (score 12.21) | chaos_before_redirect |
| `app/services.py` | business logic | identifiers match analytic, click, redirect (score 11.54) | get_analytics, record_click |
| `app/main.py` | route layer | identifiers match analytic, redirect (score 8.64) | analytics, redirect |
| `app/schemas.py` | schema / DTO | identifiers match analytic, click (score 6.49) | AnalyticsResponse, ClickEvent |
| `app/models.py` | data model | identifiers match click (score 4.43) | Click |

Request flows:
- `GET /`: main.root (models: -)
- `GET /favicon.ico`: main.favicon (models: -)
- `GET /health`: main.health (models: -)
- `GET /ready`: main.ready (models: -)
- `GET /metrics`: main.prometheus_metrics -> metrics.render (models: -)
- `POST /api/v1/urls`: main.create_url -> services.create_short_url -> services.generate_code -> metrics.inc -> metrics._fmt (models: models.URL(urls))
- `GET /api/v1/urls/{short_code}/analytics`: main.analytics -> services.get_analytics -> services._get_url (models: models.Click(clicks), models.URL(urls))
- `GET /{short_code}`: main.redirect -> ops.chaos_before_redirect -> services.resolve_url -> services._get_url -> metrics.inc -> models.utcnow -> services.record_click -> metrics._fmt (models: models.Click(clicks), models.URL(urls))
- `GET /ops/golden-signals`: ops.golden_signals_endpoint -> ops.golden_signals -> ops._budget -> ops._latency -> ops._pool -> metrics.counter -> metrics.in_flight -> metrics.uptime_seconds -> metrics.window -> ops._percentile (models: -)
- `GET /ops/dashboard`: ops.dashboard (models: -)

Tests to update:
- tests/test_api.py

## 3. Implementation plan and rationale

Impact first: changes are scoped by static analysis of the existing code, applied as exact edits to a copy of the repository, and accepted only if the pre-existing suite still passes alongside the new tests.

| Task | Title | Depends on | Produced by | Deliverable | Acceptance |
|---|---|---|---|---|---|
| B1 | Impact analysis of the existing code | - | `impact` | Impacted modules, flows, tests | Every impacted route has a flow |
| B2 | Change design (backward compatible) | B1 | `design` | Per-module change plan | No existing field removed or renamed |
| B3 | Contract and schema delta | B2 | `contract` | OpenAPI + DDL with the new fields | New fields optional |
| B4 | Targeted code edits | B3 | `codegen` | Exact edits to impacted modules | All edits apply cleanly |
| B5 | Regression + new behaviour tests | B3 | `testgen` | New pytest cases | New behaviour covered |
| B6 | Full regression, contract and lint | B4, B5 | `verify` | Verification report | Old and new tests pass |
| B7 | Risk review | B6 | `risks` | Risk register | Migration and concurrency risks addressed |
| B8 | Changelog and summary | B7 | `report` | Engineering summary | Reviewer approves |

Execution levels (tasks in one level can proceed in parallel): [B1] -> [B2] -> [B3] -> [B4, B5] -> [B6] -> [B7] -> [B8]

Critical path: B1 -> B2 -> B3 -> B4 -> B6 -> B7 -> B8

### Key design decisions
- **Random 7-char base62 codes**: 62^7 ~ 3.5e12 space, no coordination between replicas, unguessable (instead of: Counter + base62 (predictable, needs a coordinated sequence) or URL hash (collision/dedupe semantics))
- **Insert relying on the UNIQUE constraint, retry on conflict**: Race-safe; check-then-insert has a TOCTOU window (instead of: SELECT then INSERT)
- **302 with Cache-Control: no-store**: Every hit reaches the service, so analytics stay accurate (instead of: 301 (cacheable and faster, but hides repeat hits))
- **Click writes synchronous but best-effort**: A failed click write is metered and logged but never fails the redirect (instead of: Queue + batch consumer (needed above ~1K writes/sec))
- **SQLite for the prototype, PostgreSQL for production**: Zero-setup demo; SQLAlchemy makes it a DATABASE_URL change (instead of: Start on Postgres (heavier setup))
- **Domain errors in the service layer, mapped to HTTP at the edge**: Business logic testable without the web framework (instead of: HTTPException inside services)
- **Atomic conditional UPDATE for the click limit**: One statement both checks and consumes, so parallel redirects cannot overshoot (instead of: Read count then write (races) or SELECT ... FOR UPDATE (row lock, not portable to SQLite))

### Change plan
- app/ops.py (route layer): identifiers match click, redirect (score 12.21); symbols: chaos_before_redirect
- app/services.py (business logic): identifiers match analytic, click, redirect (score 11.54); symbols: get_analytics, record_click
- app/main.py (route layer): identifiers match analytic, redirect (score 8.64); symbols: analytics, redirect
- app/schemas.py (schema / DTO): identifiers match analytic, click (score 6.49); symbols: AnalyticsResponse, ClickEvent
- app/models.py (data model): identifiers match click (score 4.43); symbols: Click
- models.URL: add max_clicks INTEGER NULL and click_count INTEGER NOT NULL DEFAULT 0
- schemas: URLCreate.max_clicks (optional, >= 1); URLResponse.max_clicks; AnalyticsResponse.max_clicks/remaining_clicks
- services.resolve_url: consume one click with a conditional UPDATE (click_count < max_clicks); 0 rows -> 410 LinkExhausted
- services.get_analytics: report max_clicks and remaining_clicks
- Backward compatibility: new request fields optional, no existing field changed; verified by the old suite

## 4. Generated artifacts

- Code: Per-link max_clicks: model columns, request/response fields, atomic enforcement, analytics (0 files, 10 edits)
- Project built in `project/` from base `/home/claude/proj/targets/url_shortener`: 4 files written, 3 edited
- Tests: 2 files (contract, regression, new behaviour)
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
| lint | pass | 14 files, 0 blocking issue(s) [compile + ruff] |
| contract | pass | served API matches the contract |
| tests | pass | 18 tests, 0 failed, 0 errors |

## 6. Risks, trade-offs and validation approach

| Risk | L | I | Mitigation | Status |
|---|---|---|---|---|
| Click limit overshoot under concurrency | medium | medium | Single conditional UPDATE consumes a click only while click_count < max_clicks | verified_by_test |
| Migration on a populated urls table | medium | medium | Nullable max_clicks and server default 0 for click_count: no backfill, no lock-heavy rewrite | documented |
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
- lint: 14 files, 0 blocking issue(s) [compile + ruff]
- contract: served API matches the contract
- tests: 18 tests, 0 failed, 0 errors
- Tests are generated from the contract, independently of the code, so they are not shaped by its bugs
- Contract drift is checked against the OpenAPI the generated service actually serves
- Repair loop: 0 round(s) used this run (max 2) before the result reached review
- Human gates on requirement interpretation, plan, and final sign-off
- Live smoke test before merge: uvicorn + curl create/redirect/analytics

## 7. Assumptions and limitations

- Unset means unlimited (backward compatible); an exhausted link returns 410
- Single region
- No authentication in the prototype; creation is rate-limited
- Prototype: SQLite, in-process rate limiter and metrics, no auth, no migrations tool
- Offline reference provider: deterministic rules and a curated reference implementation; real synthesis needs a model provider

## 8. Execution trace (controlled autonomy)

| Step | Status | Attempts | Repair rounds | Cached | Seconds |
|---|---|---|---|---|---|
| requirements | succeeded | 1 | 0 |  | 0.01 |
| impact | succeeded | 1 | 0 |  | 0.03 |
| plan | succeeded | 1 | 0 |  | 0.0 |
| design | succeeded | 1 | 0 |  | 0.0 |
| contract | succeeded | 1 | 0 |  | 0.0 |
| codegen | succeeded | 1 | 0 |  | 0.01 |
| testgen | succeeded | 1 | 0 |  | 0.0 |
| docs | succeeded | 1 | 0 |  | 0.0 |
| materialize | succeeded | 1 | 0 |  | 0.01 |
| verify | succeeded | 1 | 0 |  | 2.65 |
| risks | succeeded | 1 | 0 |  | 0.0 |
| report | in progress (this report) | 0 | 0 |  | 0.0 |

Human gates:
- requirements (gate): approved by auto auto-approved (unattended run)
- plan (gate): approved by auto auto-approved (unattended run)

Recovery events:
- none

Budget used: 9 model calls (0 cache hits), 38657 tokens, 3 tool calls, 2.75 s
