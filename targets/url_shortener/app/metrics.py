"""In-process metrics: Prometheus text exposition plus a sliding window of recent requests for the golden signals.

No external dependencies. Per-process only: with several replicas, scrape /metrics from each (Prometheus aggregates);
the /ops/golden-signals view describes this instance.
"""
import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_counters: dict = defaultdict(float)
_dur_sum: dict = defaultdict(float)
_dur_count: dict = defaultdict(int)

# Prometheus latency histogram buckets (seconds).
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
_hist: dict = defaultdict(lambda: [0] * len(BUCKETS))

# Sliding window of recent requests: (timestamp, method, route, status, seconds, in_flight_at_start).
WINDOW_CAPACITY = 50_000
_events: deque = deque(maxlen=WINDOW_CAPACITY)
_in_flight = 0
_started = time.time()


def _fmt(name: str, labels: dict) -> str:
    if not labels:
        return name
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return f"{name}{{{inner}}}"


def inc(name: str, value: float = 1.0, **labels) -> None:
    with _lock:
        _counters[_fmt(name, labels)] += value


def counter(name: str) -> float:
    """Sum of a counter across all its label combinations."""
    with _lock:
        return sum(v for k, v in _counters.items() if k == name or k.startswith(name + "{"))


def observe(route: str, seconds: float) -> None:
    with _lock:
        _dur_sum[route] += seconds
        _dur_count[route] += 1
        buckets = _hist[route]
        for i, bound in enumerate(BUCKETS):
            if seconds <= bound:
                buckets[i] += 1


def request_started() -> int:
    """Call when a request arrives; returns the number of requests in flight including this one (saturation)."""
    global _in_flight
    with _lock:
        _in_flight += 1
        return _in_flight


def request_finished(method: str, route: str, status: int, seconds: float, in_flight_at_start: int) -> None:
    global _in_flight
    inc("http_requests_total", method=method, route=route, status=str(status))
    observe(route, seconds)
    with _lock:
        _in_flight -= 1
        _events.append((time.time(), method, route, status, seconds, in_flight_at_start))


def in_flight() -> int:
    with _lock:
        return _in_flight


def window(seconds: float) -> tuple:
    """Recent requests within the last `seconds`, and the actual span covered (shorter right after start-up)."""
    now = time.time()
    with _lock:
        cutoff = now - seconds
        events = [e for e in _events if e[0] >= cutoff]
    span = max(min(seconds, now - _started), 1e-6)
    return events, span


def uptime_seconds() -> float:
    return time.time() - _started


def render() -> str:
    with _lock:
        lines = [f"{key} {val:g}" for key, val in sorted(_counters.items())]
        lines.append(f"http_requests_in_flight {_in_flight}")
        for route in sorted(_dur_sum):
            for bound, count in zip(BUCKETS, _hist[route]):
                lines.append(f'http_request_duration_seconds_bucket{{route="{route}",le="{bound:g}"}} {count}')
            lines.append(f'http_request_duration_seconds_bucket{{route="{route}",le="+Inf"}} {_dur_count[route]}')
            lines.append(f'http_request_duration_seconds_sum{{route="{route}"}} {_dur_sum[route]:.6f}')
            lines.append(f'http_request_duration_seconds_count{{route="{route}"}} {_dur_count[route]}')
    return "\n".join(lines) + "\n"


def reset() -> None:
    global _in_flight, _started
    with _lock:
        _counters.clear()
        _dur_sum.clear()
        _dur_count.clear()
        _hist.clear()
        _events.clear()
        _in_flight = 0
        _started = time.time()
