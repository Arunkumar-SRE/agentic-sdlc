"""Fixed-window, per-client rate limiter (in-memory; use Redis or an API gateway when running >1 replica)."""
import threading
import time


class FixedWindowLimiter:
    def __init__(self, limit: int, window_seconds: int = 60):
        self.limit = limit  # 0 disables limiting
        self.window = window_seconds
        self._hits: dict = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = int(time.time())
        window_start = now - (now % self.window)
        with self._lock:
            if len(self._hits) > 10_000:  # bound memory: drop stale windows
                self._hits = {k: v for k, v in self._hits.items() if v[0] == window_start}
            start, count = self._hits.get(key, (window_start, 0))
            if start != window_start:
                start, count = window_start, 0
            if count >= self.limit:
                return False
            self._hits[key] = (start, count + 1)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
