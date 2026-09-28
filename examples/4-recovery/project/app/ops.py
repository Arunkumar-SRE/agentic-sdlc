"""Golden signals (latency, traffic, errors, saturation), SLO evaluation and a live dashboard.

GET /ops/golden-signals?window=300   JSON, computed from this instance's recent requests
GET /ops/dashboard                    self-contained HTML page that polls the JSON every 5 seconds

Optional chaos for demos (off unless set): CHAOS_LATENCY_MS adds delay to redirects,
CHAOS_ERROR_RATE (0..1) makes that fraction of redirects fail with 503.
"""
import math
import os
import random
import time

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from . import metrics
from .database import engine

router = APIRouter(prefix="/ops", tags=["ops"])

REDIRECT = "/{short_code}"
CREATE = "/api/v1/urls"
# Targets from the design document.
SLOS = {
    "redirect_availability": 0.999,   # non-5xx redirects / all redirect requests
    "redirect_p99_ms": 100.0,         # server-side
    "create_success": 0.995,          # 2xx / (2xx + 5xx); client errors are not the service's fault
    "click_write_loss": 0.001,        # failed click writes / successful redirects
}


def chaos_before_redirect() -> None:
    """Injected faults for demonstrating an SLO breach. No-op unless the environment variables are set."""
    delay_ms = float(os.getenv("CHAOS_LATENCY_MS", "0") or 0)
    if delay_ms > 0:
        time.sleep(delay_ms / 1000)
    error_rate = float(os.getenv("CHAOS_ERROR_RATE", "0") or 0)
    if error_rate > 0 and random.random() < error_rate:
        from .services import ServiceError

        class ChaosError(ServiceError):
            status_code, detail = 503, "Injected fault (CHAOS_ERROR_RATE)"

        raise ChaosError()


def _percentile(sorted_values: list, pct: float) -> float:
    if not sorted_values:
        return 0.0
    rank = max(0, min(len(sorted_values) - 1, math.ceil(pct / 100 * len(sorted_values)) - 1))  # nearest-rank
    return sorted_values[rank]


def _latency(durations: list) -> dict:
    values = sorted(d * 1000 for d in durations)
    return {"count": len(values), "p50_ms": round(_percentile(values, 50), 2), "p95_ms": round(_percentile(values, 95), 2),
            "p99_ms": round(_percentile(values, 99), 2), "max_ms": round(values[-1], 2) if values else 0.0}


def _budget(target: float, good: int, total: int) -> dict:
    """Error budget for a ratio SLO over the window: how much of the allowed failure has been used."""
    if total == 0:
        return {"status": "NO DATA", "actual": None, "budget_remaining_pct": None}
    actual = good / total
    allowed = (1 - target) * total
    used = total - good
    remaining = 100.0 if allowed == 0 and used == 0 else max(0.0, 100 * (1 - used / allowed)) if allowed else 0.0
    return {"status": "OK" if actual >= target else "BREACH", "actual": round(actual * 100, 3),
            "budget_remaining_pct": round(remaining, 1)}


def _pool() -> dict:
    pool = engine.pool
    info = {"class": type(pool).__name__}
    for attr in ("size", "checkedout", "overflow"):
        fn = getattr(pool, attr, None)
        if callable(fn):
            try:
                info[attr] = fn()
            except Exception:  # noqa: BLE001 - diagnostics must never fail the endpoint
                pass
    return info


def golden_signals(window_s: float = 300) -> dict:
    events, span = metrics.window(window_s)
    ops_prefix = "/ops"
    app_events = [e for e in events if not e[2].startswith(ops_prefix) and e[2] not in ("/metrics", "/health", "/ready")]

    by_route: dict = {}
    for _, method, route, status, seconds, _ in app_events:
        key = f"{method} {route}"
        r = by_route.setdefault(key, {"requests": 0, "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "_d": []})
        r["requests"] += 1
        r[f"{status // 100}xx"] = r.get(f"{status // 100}xx", 0) + 1
        r["_d"].append(seconds)
    routes = []
    for key, r in sorted(by_route.items(), key=lambda kv: -kv[1]["requests"]):
        lat = _latency(r.pop("_d"))
        routes.append({"route": key, **r, "rps": round(r["requests"] / span, 3),
                       "error_rate_pct": round(100 * r["5xx"] / r["requests"], 3), **{k: lat[k] for k in ("p50_ms", "p95_ms", "p99_ms", "max_ms")}})

    total = len(app_events)
    s5 = sum(1 for e in app_events if e[3] >= 500)
    s4 = sum(1 for e in app_events if 400 <= e[3] < 500)
    redirects = [e for e in app_events if e[2] == REDIRECT]
    redirect_ok = sum(1 for e in redirects if e[3] < 500)
    creates = [e for e in app_events if e[2] == CREATE and e[1] == "POST"]
    create_2xx = sum(1 for e in creates if 200 <= e[3] < 300)
    create_5xx = sum(1 for e in creates if e[3] >= 500)
    redirect_lat = _latency([e[4] for e in redirects])

    served = metrics.counter("redirects_total")
    lost = metrics.counter("click_write_failures_total")
    p99_status = "NO DATA" if not redirects else ("OK" if redirect_lat["p99_ms"] <= SLOS["redirect_p99_ms"] else "BREACH")

    slos = [
        {"slo": "Redirect availability", "target": f">= {SLOS['redirect_availability'] * 100:g}%",
         **_budget(SLOS["redirect_availability"], redirect_ok, len(redirects))},
        {"slo": "Redirect latency p99", "target": f"<= {SLOS['redirect_p99_ms']:g} ms", "status": p99_status,
         "actual": redirect_lat["p99_ms"] if redirects else None, "budget_remaining_pct": None},
        {"slo": "Create success (excl. 4xx)", "target": f">= {SLOS['create_success'] * 100:g}%",
         **_budget(SLOS["create_success"], create_2xx, create_2xx + create_5xx)},
        {"slo": "Click-write loss (since start)", "target": f"<= {SLOS['click_write_loss'] * 100:g}%",
         "status": "NO DATA" if not served else ("OK" if lost / served <= SLOS["click_write_loss"] else "BREACH"),
         "actual": round(100 * lost / served, 3) if served else None, "budget_remaining_pct": None},
    ]
    return {
        "instance_uptime_s": round(metrics.uptime_seconds(), 1),
        "window_s": round(span, 1),
        "latency": {"all_requests": _latency([e[4] for e in app_events]), "redirect": redirect_lat},
        "traffic": {"requests": total, "rps": round(total / span, 3), "redirects": len(redirects), "creates": len(creates)},
        "errors": {"5xx": s5, "4xx": s4, "server_error_rate_pct": round(100 * s5 / total, 3) if total else 0.0,
                   "client_error_rate_pct": round(100 * s4 / total, 3) if total else 0.0,
                   "click_write_failures_total": lost, "note": "4xx are correct answers to bad input (404, 409, 410, 422, 429) and do not burn the availability budget"},
        "saturation": {"in_flight_now": metrics.in_flight() - 1, "in_flight_peak": max((e[5] for e in app_events), default=0),
                       "rate_limited_total": metrics.counter("rate_limited_total"),
                       "short_code_collisions_total": metrics.counter("short_code_collisions_total"), "db_pool": _pool()},
        "routes": routes,
        "slos": slos,
        "chaos": {"latency_ms": os.getenv("CHAOS_LATENCY_MS", "0"), "error_rate": os.getenv("CHAOS_ERROR_RATE", "0")},
    }


@router.get("/golden-signals")
def golden_signals_endpoint(window: float = Query(300, ge=10, le=86_400, description="Look-back window in seconds")):
    """Latency, traffic, errors and saturation for this instance, with SLO status and error budgets."""
    return golden_signals(window)


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>URL shortener: golden signals</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14213d;--muted:#5b6475;--line:#dde1e7;--ok:#1a7f4b;--bad:#b42318;--warn:#b54708}
@media (prefers-color-scheme:dark){:root{--bg:#0f1420;--card:#172033;--ink:#e8ecf3;--muted:#9aa5b8;--line:#2a3550;--ok:#4cc38a;--bad:#ff7a6b;--warn:#f5b04c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,Segoe UI,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 6px}
.big{font-size:30px;font-weight:650}.small{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:20px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right;font-variant-numeric:tabular-nums}th:first-child,td:first-child{text-align:left}
th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}h3{font-size:16px;margin:0 0 8px}
.OK{color:var(--ok);font-weight:650}.BREACH{color:var(--bad);font-weight:650}.NO{color:var(--muted)}.warn{color:var(--warn)}
</style></head><body><main>
<h1>URL shortener: golden signals</h1>
<p class="sub" id="meta">Loading&hellip;</p>
<div class="tiles">
<div class="tile"><h2>Latency</h2><div class="big" id="lat">-</div><div class="small" id="lat2">redirect p99</div></div>
<div class="tile"><h2>Traffic</h2><div class="big" id="traf">-</div><div class="small" id="traf2">requests / second</div></div>
<div class="tile"><h2>Errors</h2><div class="big" id="err">-</div><div class="small" id="err2">server errors (5xx)</div></div>
<div class="tile"><h2>Saturation</h2><div class="big" id="sat">-</div><div class="small" id="sat2">peak requests in flight</div></div>
</div>
<h3>SLOs and error budgets</h3>
<table><thead><tr><th>SLO</th><th>Target</th><th>Actual</th><th>Budget left</th><th>Status</th></tr></thead><tbody id="slos"></tbody></table>
<h3>By route</h3>
<table><thead><tr><th>Route</th><th>Requests</th><th>RPS</th><th>p50 ms</th><th>p95 ms</th><th>p99 ms</th><th>4xx</th><th>5xx</th></tr></thead><tbody id="routes"></tbody></table>
<p class="small">Window: last 5 minutes on this instance. Raw Prometheus metrics: <a href="/metrics">/metrics</a> &middot; JSON: <a href="/ops/golden-signals">/ops/golden-signals</a></p>
</main><script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function tick(){try{const g=await (await fetch('/ops/golden-signals?window=300')).json();
$('meta').textContent=`Window ${g.window_s}s · uptime ${g.instance_uptime_s}s · ${g.traffic.requests} requests`+((g.chaos.latency_ms!=='0'||g.chaos.error_rate!=='0')?` · CHAOS latency=${g.chaos.latency_ms}ms error_rate=${g.chaos.error_rate}`:'');
$('lat').textContent=g.latency.redirect.count?g.latency.redirect.p99_ms+' ms':'-';$('lat2').textContent=`redirect p99 (p50 ${g.latency.redirect.p50_ms} ms, p95 ${g.latency.redirect.p95_ms} ms)`;
$('traf').textContent=g.traffic.rps;$('traf2').textContent=`req/s · ${g.traffic.redirects} redirects, ${g.traffic.creates} creates`;
$('err').textContent=g.errors.server_error_rate_pct+'%';$('err2').textContent=`5xx: ${g.errors['5xx']} · 4xx: ${g.errors['4xx']} (client) · lost clicks: ${g.errors.click_write_failures_total}`;
$('sat').textContent=g.saturation.in_flight_peak;$('sat2').textContent=`peak in flight · rate-limited ${g.saturation.rate_limited_total} · pool ${esc(g.saturation.db_pool.checkedout??'-')}/${esc(g.saturation.db_pool.size??'-')}`;
$('slos').innerHTML=g.slos.map(s=>`<tr><td>${esc(s.slo)}</td><td>${esc(s.target)}</td><td>${s.actual===null?'-':esc(s.actual)}</td><td>${s.budget_remaining_pct===null?'-':esc(s.budget_remaining_pct)+'%'}</td><td class="${esc(s.status.split(' ')[0])}">${esc(s.status)}</td></tr>`).join('');
$('routes').innerHTML=g.routes.map(r=>`<tr><td>${esc(r.route)}</td><td>${r.requests}</td><td>${r.rps}</td><td>${r.p50_ms}</td><td>${r.p95_ms}</td><td>${r.p99_ms}</td><td>${r['4xx']}</td><td class="${r['5xx']?'BREACH':''}">${r['5xx']}</td></tr>`).join('')||'<tr><td colspan="8">No traffic yet: create and open a few short links.</td></tr>';
}catch(e){$('meta').textContent='Could not load /ops/golden-signals: '+e}}
tick();setInterval(tick,5000);
</script></body></html>"""
