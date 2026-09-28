# Golden signals report: URL shortener

Generated 2026-09-28 15:24 by `scripts/golden_signals_report.py`. **2 SLO breach(es): Redirect availability, Redirect latency p99.**

Load: 600 requests after 30 seed creates, 8 concurrent clients, 12.3 s. Mix: ~80% redirects, 8% unknown codes, 7% creates, 3% invalid creates, 2% analytics reads. **Chaos injected:** +150 ms per redirect, 2% of redirects fail with 503.

## The four golden signals

| Signal | What it measures | Value | Detail |
|---|---|---|---|
| **Latency** | How long requests take (server-side) | redirect p99 **183.3 ms** | p50 155.83 ms, p95 164.92 ms, max 210.18 ms; all routes p99 183.11 ms |
| **Traffic** | Demand on the service | **50.583 req/s** | 630 requests: 523 redirects, 90 creates |
| **Errors** | Rate of failed requests | **1.429% 5xx** | 9 server errors; 62 client errors (4xx, expected); 0 lost click writes |
| **Saturation** | How full the service is | peak **8 in flight** | rate-limited 0; code collisions 0; DB pool 0/5 (QueuePool) |

## SLOs and error budgets

| SLO | Target | Actual | Error budget left | Status |
|---|---|---|---|---|
| Redirect availability | >= 99.9% | 98.279 | 0.0% | **BREACH** |
| Redirect latency p99 | <= 100 ms | 183.3 | - | **BREACH** |
| Create success (excl. 4xx) | >= 99.5% | 100.0 | 100.0% | **OK** |
| Click-write loss (since start) | <= 0.1% | 0.0 | - | **OK** |

## What it means

- **BREACH: Redirect availability**: actual 98.279 against target >= 99.9%.
- **BREACH: Redirect latency p99**: actual 183.3 against target <= 100 ms.
- OK: Create success (excl. 4xx) (100.0 against >= 99.5%, 100.0% of the error budget left).
- OK: Click-write loss (since start) (0.0 against <= 0.1%).
- Redirect tail latency: p99 is 1.2x p50.
- 62 client errors (4xx) are correct answers to bad input and do not burn the availability budget.

## By route (server-side)

| Route | Requests | RPS | p50 ms | p95 ms | p99 ms | 4xx | 5xx |
|---|---|---|---|---|---|---|---|
| `GET /{short_code}` | 523 | 41.992 | 155.83 | 164.92 | 183.3 | 45 | 9 |
| `POST /api/v1/urls` | 90 | 7.226 | 5.31 | 11.87 | 16.95 | 17 | 0 |
| `GET /api/v1/urls/{short_code}/analytics` | 17 | 1.365 | 6.24 | 21.91 | 21.91 | 0 | 0 |

## Client view vs server view

Measured by the load generator (includes the local network and client overhead): p50 157.30 ms, p99 186.53 ms over 630 calls. Status codes seen: 200: 17, 201: 73, 302: 469, 404: 45, 422: 17, 503: 9.
Client p99 is higher than server p99: the gap is time outside the application (network, proxies, client), the first place to look when users report slowness the server does not see.

## How these are measured

- Every request passes through one middleware that records method, route pattern, status, duration and requests in flight (`app/metrics.py`).
- `/ops/golden-signals` computes the four signals and SLOs from a sliding window of recent requests on this instance; `/ops/dashboard` shows them live.
- `/metrics` exposes the same data as Prometheus counters, a latency histogram and an in-flight gauge, so Grafana can aggregate across replicas.
- 4xx responses are excluded from availability: a 404 for an unknown code is the correct answer, not an outage.
- Production next steps: alert on error-budget burn rate (fast and slow windows), aggregate across replicas in Prometheus, add DB latency and queue depth once click writes move to a queue.
