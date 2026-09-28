# Risk register

| Risk | Likelihood | Impact | Mitigation | Status |
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

## Failure scenarios
| Scenario | Detection | Behaviour |
|---|---|---|
| Database unavailable | /ready returns 503; 5xx rate | Fail fast; liveness stays green |
| Click insert fails | click_write_failures_total > 0 | Redirect still served; click lost and logged |
| Collision storm | short_code_collisions_total spikes | Retries, then 503 so clients back off |
| Create flood from one client | rate_limited_total | 429 for that client only |
| Model returns invalid output | Schema validation in the router | Rejected; retried with the error; fallback or halt |
| Generated code fails verification | verify step | Feedback loop re-opens code generation (bounded rounds) |

## Validation strategy
- edits_applied: all edits applied
- lint: 13 files, 0 blocking issue(s) [compile + ruff]
- contract: served API matches the contract
- tests: 14 tests, 0 failed, 0 errors
- Tests are generated from the contract, independently of the code, so they are not shaped by its bugs
- Contract drift is checked against the OpenAPI the generated service actually serves
- Repair loop: 0 round(s) used this run (max 2) before the result reached review
- Human gates on requirement interpretation, plan, and final sign-off
- Live smoke test before merge: uvicorn + curl create/redirect/analytics
