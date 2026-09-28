"""Load-test the URL shortener, then write a golden signals report (latency, traffic, errors, saturation + SLOs).

Examples (from the repository root, with the virtual environment active):
    python scripts/golden_signals_report.py                                   # healthy run
    python scripts/golden_signals_report.py --chaos-latency-ms 150 --chaos-error-rate 0.02 --out docs/golden_signals_chaos.md
    python scripts/golden_signals_report.py --service-dir workspace/<run-id>/project   # the app the agent generated
    python scripts/golden_signals_report.py --base-url http://127.0.0.1:8000 --no-start  # an already running service
"""

from __future__ import annotations

import argparse
import os
import random
import string
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent


def start_service(service_dir: Path, port: int, env_extra: dict[str, str]) -> subprocess.Popen[bytes]:
    db = Path(tempfile.gettempdir()) / f"url_shortener_loadtest_{port}.db"  # outside the project, never committed
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db.as_posix()}", **env_extra}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
                            cwd=service_dir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.5)
    proc.terminate()
    raise SystemExit(f"service in {service_dir} did not start on port {port}")


class Load:
    """A realistic mix: mostly redirects, some creates, a few bad requests and unknown codes."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.codes: list[str] = []
        self.lock = threading.Lock()
        self.client_ms: list[float] = []
        self.statuses: dict[int, int] = {}

    def _record(self, status: int, ms: float) -> None:
        with self.lock:
            self.client_ms.append(ms)
            self.statuses[status] = self.statuses.get(status, 0) + 1

    def _call(self, client: httpx.Client, method: str, path: str, **kw: Any) -> httpx.Response | None:
        started = time.perf_counter()
        try:
            resp = client.request(method, self.base + path, **kw)
        except httpx.HTTPError:
            self._record(0, (time.perf_counter() - started) * 1000)
            return None
        self._record(resp.status_code, (time.perf_counter() - started) * 1000)
        return resp

    def create(self, client: httpx.Client, alias: str | None = None) -> None:
        body: dict[str, Any] = {"long_url": f"https://example.com/{''.join(random.choices(string.ascii_lowercase, k=12))}"}
        if alias:
            body["custom_alias"] = alias
        resp = self._call(client, "POST", "/api/v1/urls", json=body)
        if resp is not None and resp.status_code == 201:
            with self.lock:
                self.codes.append(resp.json()["short_code"])

    def one(self, client: httpx.Client) -> None:
        roll = random.random()
        if roll < 0.80 and self.codes:
            code = random.choice(self.codes)
            self._call(client, "GET", f"/{code}", headers={"Referer": random.choice(["https://news.example", "https://mail.example", ""])})
        elif roll < 0.88:
            self._call(client, "GET", "/nosuch" + "".join(random.choices(string.digits, k=4)))  # 404
        elif roll < 0.95:
            self.create(client)
        elif roll < 0.98:
            self._call(client, "POST", "/api/v1/urls", json={"long_url": "not-a-url"})  # 422
        elif self.codes:
            self._call(client, "GET", f"/api/v1/urls/{random.choice(self.codes)}/analytics")


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(-(-p / 100 * len(ordered) // 1)) - 1))]


def run_load(base: str, requests: int, concurrency: int) -> tuple[Load, float]:
    load = Load(base)
    started = time.perf_counter()
    with httpx.Client(follow_redirects=False, timeout=10) as client:
        for i in range(30):  # seed links so redirects have targets
            load.create(client, alias=f"seed{i}" if i < 5 else None)

        def worker(n: int) -> None:
            with httpx.Client(follow_redirects=False, timeout=10) as c:
                for _ in range(n):
                    load.one(c)

        per = [requests // concurrency + (1 if i < requests % concurrency else 0) for i in range(concurrency)]
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(worker, per))
    return load, time.perf_counter() - started


def verdict_lines(g: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for s in g["slos"]:
        if s["status"] == "BREACH":
            out.append(f"- **BREACH: {s['slo']}**: actual {s['actual']} against target {s['target']}.")
        elif s["status"] == "OK":
            budget = f", {s['budget_remaining_pct']}% of the error budget left" if s["budget_remaining_pct"] is not None else ""
            out.append(f"- OK: {s['slo']} ({s['actual']} against {s['target']}{budget}).")
    red = g["latency"]["redirect"]
    if red["count"] and red["p99_ms"] > 0:
        tail = red["p99_ms"] / max(red["p50_ms"], 0.01)
        out.append(f"- Redirect tail latency: p99 is {tail:.1f}x p50" + (" (a long tail worth investigating)." if tail > 5 else "."))
    if g["errors"]["4xx"]:
        out.append(f"- {g['errors']['4xx']} client errors (4xx) are correct answers to bad input and do not burn the availability budget.")
    if g["saturation"]["rate_limited_total"]:
        out.append(f"- {g['saturation']['rate_limited_total']:g} requests were rate-limited (429): the limiter is protecting capacity.")
    return out


def write_report(path: Path, g: dict[str, Any], load: Load, elapsed: float, args: argparse.Namespace) -> None:
    lat, traffic, errors, sat = g["latency"], g["traffic"], g["errors"], g["saturation"]
    breaches = [s for s in g["slos"] if s["status"] == "BREACH"]
    headline = "All SLOs met." if not breaches else f"{len(breaches)} SLO breach(es): " + ", ".join(s["slo"] for s in breaches) + "."
    chaos = g["chaos"]
    lines = [
        "# Golden signals report: URL shortener", "",
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by `scripts/golden_signals_report.py`. **{headline}**", "",
        f"Load: {args.requests} requests after 30 seed creates, {args.concurrency} concurrent clients, {elapsed:.1f} s. "
        "Mix: ~80% redirects, 8% unknown codes, 7% creates, 3% invalid creates, 2% analytics reads."
        + (f" **Chaos injected:** +{chaos['latency_ms']} ms per redirect, {float(chaos['error_rate']) * 100:g}% of redirects fail with 503." if chaos["latency_ms"] != "0" or chaos["error_rate"] != "0" else ""),
        "", "## The four golden signals", "",
        "| Signal | What it measures | Value | Detail |", "|---|---|---|---|",
        f"| **Latency** | How long requests take (server-side) | redirect p99 **{lat['redirect']['p99_ms']} ms** | p50 {lat['redirect']['p50_ms']} ms, p95 {lat['redirect']['p95_ms']} ms, max {lat['redirect']['max_ms']} ms; all routes p99 {lat['all_requests']['p99_ms']} ms |",
        f"| **Traffic** | Demand on the service | **{traffic['rps']} req/s** | {traffic['requests']} requests: {traffic['redirects']} redirects, {traffic['creates']} creates |",
        f"| **Errors** | Rate of failed requests | **{errors['server_error_rate_pct']}% 5xx** | {errors['5xx']} server errors; {errors['4xx']} client errors (4xx, expected); {errors['click_write_failures_total']:g} lost click writes |",
        f"| **Saturation** | How full the service is | peak **{sat['in_flight_peak']} in flight** | rate-limited {sat['rate_limited_total']:g}; code collisions {sat['short_code_collisions_total']:g}; DB pool {sat['db_pool'].get('checkedout', '-')}/{sat['db_pool'].get('size', '-')} ({sat['db_pool'].get('class')}) |",
        "", "## SLOs and error budgets", "", "| SLO | Target | Actual | Error budget left | Status |", "|---|---|---|---|---|",
        *[f"| {s['slo']} | {s['target']} | {'-' if s['actual'] is None else s['actual']} | {'-' if s['budget_remaining_pct'] is None else str(s['budget_remaining_pct']) + '%'} | **{s['status']}** |" for s in g["slos"]],
        "", "## What it means", "", *verdict_lines(g),
        "", "## By route (server-side)", "", "| Route | Requests | RPS | p50 ms | p95 ms | p99 ms | 4xx | 5xx |", "|---|---|---|---|---|---|---|---|",
        *[f"| `{r['route']}` | {r['requests']} | {r['rps']} | {r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} | {r['4xx']} | {r['5xx']} |" for r in g["routes"]],
        "", "## Client view vs server view", "",
        f"Measured by the load generator (includes the local network and client overhead): p50 {pct(load.client_ms, 50):.2f} ms, "
        f"p99 {pct(load.client_ms, 99):.2f} ms over {len(load.client_ms)} calls. Status codes seen: "
        + ", ".join(f"{k}: {v}" for k, v in sorted(load.statuses.items())) + ".",
        ("Client p99 is higher than server p99: the gap is time outside the application (network, proxies, client), the first place to look when users report slowness the server does not see."
         if pct(load.client_ms, 99) > g["latency"]["all_requests"]["p99_ms"] else
         "Here the server p99 is not lower than the client's: on one machine there is no network in between, and the server window also includes the seed creates."),
        "", "## How these are measured", "",
        "- Every request passes through one middleware that records method, route pattern, status, duration and requests in flight (`app/metrics.py`).",
        "- `/ops/golden-signals` computes the four signals and SLOs from a sliding window of recent requests on this instance; `/ops/dashboard` shows them live.",
        "- `/metrics` exposes the same data as Prometheus counters, a latency histogram and an in-flight gauge, so Grafana can aggregate across replicas.",
        "- 4xx responses are excluded from availability: a 404 for an unknown code is the correct answer, not an outage.",
        "- Production next steps: alert on error-budget burn rate (fast and slow windows), aggregate across replicas in Prometheus, add DB latency and queue depth once click writes move to a queue.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--service-dir", type=Path, default=ROOT / "targets" / "url_shortener")
    p.add_argument("--base-url", default=None, help="use a service that is already running (implies --no-start)")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--port", type=int, default=8077)
    p.add_argument("--requests", type=int, default=600)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--rate-limit", type=int, default=1000, help="RATE_LIMIT_PER_MIN for the started service")
    p.add_argument("--chaos-latency-ms", type=int, default=0)
    p.add_argument("--chaos-error-rate", type=float, default=0.0)
    p.add_argument("--out", type=Path, default=ROOT / "reports" / "golden_signals_report.md")
    args = p.parse_args()

    proc = None
    base = args.base_url
    if not base and not args.no_start:
        env = {"RATE_LIMIT_PER_MIN": str(args.rate_limit), "CHAOS_LATENCY_MS": str(args.chaos_latency_ms),
               "CHAOS_ERROR_RATE": str(args.chaos_error_rate)}
        print(f"starting service from {args.service_dir} on port {args.port} ...")
        proc = start_service(args.service_dir.resolve(), args.port, env)
    base = (base or f"http://127.0.0.1:{args.port}").rstrip("/")
    try:
        print(f"sending {args.requests} requests with {args.concurrency} clients to {base} ...")
        load, elapsed = run_load(base, args.requests, args.concurrency)
        g = httpx.get(f"{base}/ops/golden-signals", params={"window": 3600}, timeout=10).json()
        write_report(args.out, g, load, elapsed, args)
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)
    print(f"\nreport: {args.out}")
    for s in g["slos"]:
        print(f"  {s['status']:8} {s['slo']}: {s['actual']} (target {s['target']})")
    print(f"  latency p99 {g['latency']['redirect']['p99_ms']} ms | traffic {g['traffic']['rps']} req/s | "
          f"5xx {g['errors']['server_error_rate_pct']}% | peak in flight {g['saturation']['in_flight_peak']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
