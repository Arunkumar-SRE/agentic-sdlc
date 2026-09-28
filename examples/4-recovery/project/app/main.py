import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import metrics, models, ops, schemas, services
from .database import Base, engine, get_db
from .ratelimit import FixedWindowLimiter

logging.basicConfig(level=logging.INFO, format="%(message)s")
access_log = logging.getLogger("access")
limiter = FixedWindowLimiter(limit=int(os.getenv("RATE_LIMIT_PER_MIN", "120")))


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)  # demo convenience; use Alembic migrations in production
    yield


app = FastAPI(
    title="Agentic URL Shortener",
    version="1.1.0",
    description="URL shortener with persistence, analytics and SRE hooks: health, readiness, Prometheus metrics, golden signals, rate limiting.",
    lifespan=lifespan,
)


@app.exception_handler(services.ServiceError)
async def service_error_handler(_: Request, exc: services.ServiceError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.middleware("http")
async def observability(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    start, status = time.perf_counter(), 500
    in_flight = metrics.request_started()  # saturation signal
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        elapsed = time.perf_counter() - start
        metrics.request_finished(request.method, route, status, elapsed, in_flight)  # latency, traffic, errors
        access_log.info(json.dumps({"request_id": request_id, "method": request.method, "route": route, "status": status, "ms": round(elapsed * 1000, 2)}))


def rate_limit(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    if not limiter.allow(client):
        metrics.inc("rate_limited_total")
        raise HTTPException(status_code=429, detail="Too many requests")


app.include_router(ops.router)  # /ops/golden-signals, /ops/dashboard


@app.get("/", include_in_schema=False)
def root():
    """Landing: send browsers to the interactive API docs instead of a 404."""
    return RedirectResponse(url="/docs", status_code=307)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers request this automatically; answer 204 so it is not treated as an unknown short code."""
    return Response(status_code=204)


@app.get("/health", tags=["ops"])
def health():
    """Liveness: process is up."""
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
def ready(db: Session = Depends(get_db)):
    """Readiness: dependencies (database) reachable."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ready"}


@app.get("/metrics", response_class=PlainTextResponse, tags=["ops"])
def prometheus_metrics():
    return metrics.render()


@app.post("/api/v1/urls", response_model=schemas.URLResponse, status_code=201, tags=["urls"], dependencies=[Depends(rate_limit)])
def create_url(payload: schemas.URLCreate, request: Request, db: Session = Depends(get_db)):
    return services.create_short_url(db, payload, str(request.base_url).rstrip("/"))


@app.get("/api/v1/urls/{short_code}/analytics", response_model=schemas.AnalyticsResponse, tags=["urls"])
def analytics(short_code: str, db: Session = Depends(get_db)):
    return services.get_analytics(db, short_code)


@app.get("/{short_code}", tags=["urls"], include_in_schema=True)
def redirect(short_code: str, request: Request, db: Session = Depends(get_db)):
    # 302 (not 301) so every hit reaches us and is counted; 301 would be cached by browsers.
    ops.chaos_before_redirect()  # no-op unless CHAOS_* environment variables are set (demo only)
    long_url = services.resolve_url(db, short_code, request.headers.get("referer"), request.headers.get("user-agent"))
    return RedirectResponse(url=long_url, status_code=302, headers={"Cache-Control": "no-store"})
