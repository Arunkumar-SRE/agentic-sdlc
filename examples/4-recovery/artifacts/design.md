# Design

Stateless FastAPI service over a relational store: a create path, a hot redirect path that records clicks best-effort, and an analytics read path, with health, readiness, metrics and rate limiting built in.

## Components
- FastAPI app (stateless, horizontally scalable)
- Service layer (framework-free, domain errors)
- SQLAlchemy persistence (SQLite prototype / PostgreSQL production)
- Observability middleware (request id, JSON access log, Prometheus metrics and latency histogram)
- Golden signals and SLO evaluation (/ops/golden-signals, live /ops/dashboard)
- Per-client fixed-window rate limiter

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/urls` | Create a short URL (optional alias, expiry) |
| GET | `/{short_code}` | 302 redirect and record a click; 404 unknown, 410 expired |
| GET | `/api/v1/urls/{short_code}/analytics` | Click analytics |
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (database reachable) |
| GET | `/metrics` | Prometheus metrics |
| GET | `/ops/golden-signals` | Latency, traffic, errors, saturation and SLO status |

## Data model
- **urls**: id PK, short_code VARCHAR(16) UNIQUE INDEX, long_url TEXT, created_at, expires_at NULL
- **clicks**: id PK, url_id FK, clicked_at, referrer NULL, user_agent NULL, INDEX(url_id, clicked_at)

## Key decisions
| Decision | Rationale | Alternative considered |
|---|---|---|
| Random 7-char base62 codes | 62^7 ~ 3.5e12 space, no coordination between replicas, unguessable | Counter + base62 (predictable, needs a coordinated sequence) or URL hash (collision/dedupe semantics) |
| Insert relying on the UNIQUE constraint, retry on conflict | Race-safe; check-then-insert has a TOCTOU window | SELECT then INSERT |
| 302 with Cache-Control: no-store | Every hit reaches the service, so analytics stay accurate | 301 (cacheable and faster, but hides repeat hits) |
| Click writes synchronous but best-effort | A failed click write is metered and logged but never fails the redirect | Queue + batch consumer (needed above ~1K writes/sec) |
| SQLite for the prototype, PostgreSQL for production | Zero-setup demo; SQLAlchemy makes it a DATABASE_URL change | Start on Postgres (heavier setup) |
| Domain errors in the service layer, mapped to HTTP at the edge | Business logic testable without the web framework | HTTPException inside services |

## Trade-offs
- Analytics consistency vs redirect availability: availability wins
- Synchronous click writes (simple) vs queued writes (throughput): simple for the prototype
- In-memory rate limiter is per-process: correct for one replica; use Redis or the gateway for many
- Random codes need collision retries; counters would not, but they couple replicas

## SLOs
- Redirect availability 99.9% over 30 days
- Redirect p99 < 100 ms server-side
- Create success rate 99.5% excluding 4xx
- Click-write loss < 0.1% of redirects

## Scaling path
1. Scale API replicas behind a load balancer
2. PostgreSQL with read replicas and PgBouncer
3. Redis read-through cache for hot code -> URL
4. Queue + batch writer for clicks; time-partitioned clicks table
5. Rate limiting at the gateway (Redis)
6. Multi-region with per-region caches
