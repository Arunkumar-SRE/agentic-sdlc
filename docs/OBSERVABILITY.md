# Observability: golden signals for the URL shortener

The service measures the four golden signals (latency, traffic, errors, saturation) itself, evaluates its SLOs
with error budgets, and shows them live. A load-test script produces a written report. The first report it
produced found a real tail-latency problem, which was diagnosed and fixed; all three reports are in
[`docs/observability/`](observability/).

## What you can open

| Where | What |
|---|---|
| `http://127.0.0.1:8000/ops/dashboard` | Live dashboard: four signal tiles, SLO table with error budgets, per-route table; refreshes every 5 s |
| `http://127.0.0.1:8000/ops/golden-signals?window=300` | The same data as JSON (window in seconds) |
| `http://127.0.0.1:8000/metrics` | Prometheus text: request counters, a latency **histogram** per route, an in-flight gauge, domain counters |
| `python scripts/golden_signals_report.py` | Starts the service, sends ~630 requests from 8 concurrent clients, writes `reports/golden_signals_report.md` |

## The four signals, as implemented

| Signal | Question it answers | How it is measured here |
|---|---|---|
| **Latency** | How long do requests take? | Server-side duration of every request, per route pattern; p50, p95, p99, max. The SLO uses **redirect p99**, because the redirect is what users feel |
| **Traffic** | How much demand is there? | Requests per second over the window, split into redirects, creates and other routes |
| **Errors** | How many requests fail? | **5xx** rate (the service's fault, burns the error budget) kept apart from **4xx** (correct answers to bad input: 404, 409, 410, 422, 429). Also the *silent* error: click writes that failed while the redirect still succeeded |
| **Saturation** | How full is the service? | Requests in flight (current and peak), rate-limited requests (429), database connection pool use, short-code collisions (key-space pressure) |

All four come from one middleware that records method, route pattern, status, duration and requests in flight for
every request (`app/metrics.py`); `/ops/golden-signals` computes the signals over a sliding window of those
records (`app/ops.py`). Route **patterns** (`/{short_code}`) are used, not raw paths, so metrics stay low-cardinality.

## SLOs and error budgets

| SLO | Target | Counted as good | Why this one |
|---|---|---|---|
| Redirect availability | 99.9% | redirect requests that did not return 5xx | The redirect is the product; a 404 for an unknown code is a correct answer |
| Redirect latency | p99 <= 100 ms (server) | | A short link should feel instant |
| Create success | 99.5% | 2xx out of (2xx + 5xx) | Creation matters but is less frequent and retryable |
| Click-write loss | <= 0.1% | redirects whose click was recorded | Analytics is best-effort by design; this keeps "best-effort" honest |

**Error budget remaining** = how much of the allowed failure is still unspent in the window. At 99.9% over 1,000
redirects, one failure is allowed; one 5xx uses 100% of the budget.

## The three reports, and the story they tell

| Report | Setup | Result |
|---|---|---|
| [1-before-fix](observability/1-before-fix-sqlite-lock-contention.md) | Healthy service, 8 concurrent clients | **Latency SLO breached**: redirect p50 14 ms but p99 393 ms, max 1.1 s. Everything else fine |
| [2-after-fix](observability/2-after-fix.md) | Same load after the fix | **All SLOs met**: redirect p99 about 70 to 80 ms, higher throughput |
| [3-chaos-slo-breach](observability/3-chaos-slo-breach.md) | `--chaos-latency-ms 150 --chaos-error-rate 0.02` | **Two breaches**: availability ~98% (budget exhausted) and p99 ~180 ms. Shows the SLOs and budgets catch a real incident |

**Diagnosis of report 1.** The median was fast and there were no errors, so this was queueing, not slowness. Every
redirect commits a click row; SQLite allows one writer at a time, so under concurrency requests waited for the write
lock. **Fix:** SQLite write-ahead logging, `synchronous=NORMAL` and a busy timeout (`app/database.py`), which lets
reads proceed during writes and cuts fsyncs. **Production fix:** PostgreSQL, and moving click writes off the
redirect path onto a queue with batched inserts. Both are already in the design's scaling path; the load test turned
a documented risk into measured evidence.

## Demo it

```powershell
python scripts/golden_signals_report.py                                             # healthy: all SLOs OK
python scripts/golden_signals_report.py --chaos-latency-ms 150 --chaos-error-rate 0.02 --out reports/chaos.md   # breach
```

For the live dashboard: start the service (`cd targets\url_shortener`, `uvicorn app.main:app`), open
`/ops/dashboard`, and in a second window run
`python scripts/golden_signals_report.py --no-start --base-url http://127.0.0.1:8000 --requests 2000`.
Watch the tiles move. To demo an incident, start the service with `$env:CHAOS_ERROR_RATE="0.05"` set first.

## Limits, and what production would add

- Signals are per instance and in memory; a restart resets them. In production Prometheus scrapes every replica's
  `/metrics` and Grafana computes the same signals across the fleet; the in-app view is for a single instance.
- Alerting should be on **error-budget burn rate** over two windows (for example 1 h and 6 h), not on single spikes.
- Add dependency signals: database query latency and, once click writes are queued, queue depth and consumer lag.
- Chaos injection is environment-variable driven and off by default; it exists only for demonstrations.
