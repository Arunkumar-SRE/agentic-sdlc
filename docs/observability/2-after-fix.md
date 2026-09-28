# Golden signals report: URL shortener

Generated 2026-09-28 15:24 by `scripts/golden_signals_report.py`. **All SLOs met.**

Load: 600 requests after 30 seed creates, 8 concurrent clients, 3.0 s. Mix: ~80% redirects, 8% unknown codes, 7% creates, 3% invalid creates, 2% analytics reads. **Chaos injected:** +0 ms per redirect, 0% of redirects fail with 503.

## The four golden signals

| Signal | What it measures | Value | Detail |
|---|---|---|---|
| **Latency** | How long requests take (server-side) | redirect p99 **77.32 ms** | p50 21.97 ms, p95 48.55 ms, max 97.84 ms; all routes p99 76.18 ms |
| **Traffic** | Demand on the service | **172.867 req/s** | 630 requests: 536 redirects, 85 creates |
| **Errors** | Rate of failed requests | **0.0% 5xx** | 0 server errors; 59 client errors (4xx, expected); 0 lost click writes |
| **Saturation** | How full the service is | peak **8 in flight** | rate-limited 0; code collisions 0; DB pool 0/5 (QueuePool) |

## SLOs and error budgets

| SLO | Target | Actual | Error budget left | Status |
|---|---|---|---|---|
| Redirect availability | >= 99.9% | 100.0 | 100.0% | **OK** |
| Redirect latency p99 | <= 100 ms | 77.32 | - | **OK** |
| Create success (excl. 4xx) | >= 99.5% | 100.0 | 100.0% | **OK** |
| Click-write loss (since start) | <= 0.1% | 0.0 | - | **OK** |

## What it means

- OK: Redirect availability (100.0 against >= 99.9%, 100.0% of the error budget left).
- OK: Redirect latency p99 (77.32 against <= 100 ms).
- OK: Create success (excl. 4xx) (100.0 against >= 99.5%, 100.0% of the error budget left).
- OK: Click-write loss (since start) (0.0 against <= 0.1%).
- Redirect tail latency: p99 is 3.5x p50.
- 59 client errors (4xx) are correct answers to bad input and do not burn the availability budget.

## By route (server-side)

| Route | Requests | RPS | p50 ms | p95 ms | p99 ms | 4xx | 5xx |
|---|---|---|---|---|---|---|---|
| `GET /{short_code}` | 536 | 147.074 | 21.97 | 48.55 | 77.32 | 50 | 0 |
| `POST /api/v1/urls` | 85 | 23.323 | 23.97 | 56.42 | 75.01 | 9 | 0 |
| `GET /api/v1/urls/{short_code}/analytics` | 9 | 2.47 | 35.39 | 58.53 | 58.53 | 0 | 0 |

## Client view vs server view

Measured by the load generator (includes the local network and client overhead): p50 26.85 ms, p99 80.94 ms over 630 calls. Status codes seen: 200: 9, 201: 76, 302: 486, 404: 50, 422: 9.
Client p99 is higher than server p99: the gap is time outside the application (network, proxies, client), the first place to look when users report slowness the server does not see.

## How these are measured

- Every request passes through one middleware that records method, route pattern, status, duration and requests in flight (`app/metrics.py`).
- `/ops/golden-signals` computes the four signals and SLOs from a sliding window of recent requests on this instance; `/ops/dashboard` shows them live.
- `/metrics` exposes the same data as Prometheus counters, a latency histogram and an in-flight gauge, so Grafana can aggregate across replicas.
- 4xx responses are excluded from availability: a 404 for an unknown code is the correct answer, not an outage.
- Production next steps: alert on error-budget burn rate (fast and slow windows), aggregate across replicas in Prometheus, add DB latency and queue depth once click writes move to a queue.
