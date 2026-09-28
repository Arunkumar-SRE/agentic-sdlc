# URL shortener (generated)

Stateless FastAPI service over a relational store: a create path, a hot redirect path that records clicks best-effort, and an analytics read path, with health, readiness, metrics and rate limiting built in.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload   # http://127.0.0.1:8000/docs
pytest -q
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/urls` | Create a short URL (optional alias, expiry, max_clicks) |
| GET | `/{short_code}` | 302 redirect and record a click; 404 unknown, 410 expired or exhausted |
| GET | `/api/v1/urls/{short_code}/analytics` | Click analytics |
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (database reachable) |
| GET | `/metrics` | Prometheus metrics |
| GET | `/ops/golden-signals` | Latency, traffic, errors, saturation and SLO status |

Configuration: `DATABASE_URL` (default SQLite file; PostgreSQL URL in production), `RATE_LIMIT_PER_MIN` (0 disables).
