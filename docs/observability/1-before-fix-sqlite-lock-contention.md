# Golden signals report: URL shortener (before the fix)

> Kept as evidence. This run found a real problem: redirect p99 393 ms against a 100 ms SLO while p50 was 14 ms. Root cause: every redirect commits a click row and SQLite allows one writer at a time, so under 8 concurrent clients requests queued for the write lock (max 1.1 s). Fix applied: SQLite write-ahead logging (`app/database.py`). See `2-after-fix.md`.

Generated 2026-09-28 15:23 by `scripts/golden_signals_report.py`. **1 SLO breach(es): Redirect latency p99.**

Load: 600 requests after 30 seed creates, 8 concurrent clients, 3.5 s. Mix: ~80% redirects, 8% unknown codes, 7% creates, 3% invalid creates, 2% analytics reads. **Chaos injected:** +0 ms per redirect, 0% of redirects fail with 503.

## The four golden signals

| Signal | What it measures | Value | Detail |
|---|---|---|---|
| **Latency** | How long requests take (server-side) | redirect p99 **393.31 ms** | p50 14.21 ms, p95 110.94 ms, max 1140.03 ms; all routes p99 248.94 ms |
| **Traffic** | Demand on the service | **149.887 req/s** | 630 requests: 529 redirects, 85 creates |
| **Errors** | Rate of failed requests | **0.0% 5xx** | 0 server errors; 76 client errors (4xx, expected); 0 lost click writes |
| **Saturation** | How full the service is | peak **8 in flight** | rate-limited 0; code collisions 0; DB pool 0/5 (QueuePool) |

## SLOs and error budgets

| SLO | Target | Actual | Error budget left | Status |
|---|---|---|---|---|
| Redirect availability | >= 99.9% | 100.0 | 100.0% | **OK** |
| Redirect latency p99 | <= 100 ms | 393.31 | - | **BREACH** |
| Create success (excl. 4xx) | >= 99.5% | 100.0 | 100.0% | **OK** |
| Click-write loss (since start) | <= 0.1% | 0.0 | - | **OK** |

## What it means

- OK: Redirect availability (100.0 against >= 99.9%, 100.0% of the error budget left).
- **BREACH: Redirect latency p99**: actual 393.31 against target <= 100 ms.
- OK: Create success (excl. 4xx) (100.0 against >= 99.5%, 100.0% of the error budget left).
- OK: Click-write loss (since start) (0.0 against <= 0.1%).
- Redirect tail latency: p99 is 27.7x p50 (a long tail worth investigating).
- 76 client errors (4xx) are correct answers to bad input and do not burn the availability budget.

## By route (server-side)

| Route | Requests | RPS | p50 ms | p95 ms | p99 ms | 4xx | 5xx |
|---|---|---|---|---|---|---|---|
| `GET /{short_code}` | 529 | 125.858 | 14.21 | 110.94 | 393.31 | 58 | 0 |
| `POST /api/v1/urls` | 85 | 20.223 | 8.49 | 28.75 | 66.09 | 18 | 0 |
| `GET /api/v1/urls/{short_code}/analytics` | 16 | 3.807 | 20.84 | 104.45 | 104.45 | 0 | 0 |

## Client view vs server view

Measured by the load generator (includes the local network and client overhead): p50 16.30 ms, p99 252.08 ms over 630 calls. Status codes seen: 200: 16, 201: 67, 302: 471, 404: 58, 422: 18.
The gap between client and server latency is time outside the application (network, TLS, proxies): the first place to look when users report slowness the server does not see.

## How these are measured

- Every request passes through one middleware that records method, route pattern, status, duration and requests in flight (`app/metrics.py`).
- `/ops/golden-signals` computes the four signals and SLOs from a sliding window of recent requests on this instance; `/ops/dashboard` shows them live.
- `/metrics` exposes the same data as Prometheus counters, a latency histogram and an in-flight gauge, so Grafana can aggregate across replicas.
- 4xx responses are excluded from availability: a 404 for an unknown code is the correct answer, not an outage.
- Production next steps: alert on error-budget burn rate (fast and slow windows), aggregate across replicas in Prometheus, add DB latency and queue depth once click writes move to a queue.
