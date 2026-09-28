"""Run-level budgets: a hard ceiling on autonomy (LLM calls, tokens, tool calls, wall-clock)."""

from __future__ import annotations

import threading
import time
from typing import Any

from pydantic import BaseModel

from .errors import BudgetExceeded


class BudgetLimits(BaseModel):
    max_llm_calls: int = 60
    max_tokens: int = 1_500_000
    max_tool_calls: int = 200
    max_seconds: float = 1800.0


class BudgetTracker:
    def __init__(self, limits: BudgetLimits, used: dict[str, Any] | None = None) -> None:
        self.limits = limits
        self._lock = threading.Lock()
        used = used or {}
        self.llm_calls: int = int(used.get("llm_calls", 0))
        self.cache_hits: int = int(used.get("cache_hits", 0))
        self.input_tokens: int = int(used.get("input_tokens", 0))
        self.output_tokens: int = int(used.get("output_tokens", 0))
        self.tool_calls: int = int(used.get("tool_calls", 0))
        self._started = time.monotonic() - float(used.get("elapsed_s", 0.0))

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def check(self) -> None:
        if self.llm_calls > self.limits.max_llm_calls:
            raise BudgetExceeded(f"LLM call budget exhausted ({self.llm_calls}/{self.limits.max_llm_calls})")
        if self.tokens > self.limits.max_tokens:
            raise BudgetExceeded(f"token budget exhausted ({self.tokens}/{self.limits.max_tokens})")
        if self.tool_calls > self.limits.max_tool_calls:
            raise BudgetExceeded(f"tool call budget exhausted ({self.tool_calls}/{self.limits.max_tool_calls})")
        if self.elapsed > self.limits.max_seconds:
            raise BudgetExceeded(f"time budget exhausted ({self.elapsed:.0f}s/{self.limits.max_seconds:.0f}s)")

    def reserve_llm_call(self) -> None:
        """Called before a model call so a runaway loop stops before spending, not after."""
        with self._lock:
            self.llm_calls += 1
            try:
                self.check()
            except BudgetExceeded:
                self.llm_calls -= 1
                raise

    def charge_tokens(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens

    def cache_hit(self) -> None:
        with self._lock:
            self.cache_hits += 1

    def reserve_tool_call(self) -> None:
        with self._lock:
            self.tool_calls += 1
            try:
                self.check()
            except BudgetExceeded:
                self.tool_calls -= 1
                raise

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "llm_calls": self.llm_calls,
                "cache_hits": self.cache_hits,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "tool_calls": self.tool_calls,
                "elapsed_s": round(self.elapsed, 2),
                "limits": self.limits.model_dump(),
            }
