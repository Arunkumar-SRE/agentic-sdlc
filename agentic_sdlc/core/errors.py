"""Error taxonomy. The engine decides retry / fail / halt purely from the exception type."""

from __future__ import annotations


class AgenticError(Exception):
    """Base class. Not retryable unless a subclass says so."""

    retryable: bool = False


class ConfigError(AgenticError):
    """Invalid workflow YAML, unknown schema, unknown tool, bad template reference."""


class UnsupportedByProvider(AgenticError):
    """The configured model provider cannot answer this request (e.g. the offline reference provider outside its domain)."""


class RetryableError(AgenticError):
    """Transient failure (rate limit, network, flaky tool). The engine retries with backoff."""

    retryable = True


class OutputValidationError(RetryableError):
    """Model output did not match the agent's Pydantic schema. Retried with the error fed back to the model."""


class ToolError(AgenticError):
    """A tool failed deterministically (bad arguments, missing file)."""


class SandboxViolation(ToolError):
    """A tool tried to touch a path outside the run's workspace or the allowed read roots."""


class BudgetExceeded(AgenticError):
    """A run-level budget (LLM calls, tokens, tool calls, wall clock) is exhausted. Halts the run."""


class HumanRejected(AgenticError):
    """A reviewer rejected a gate. Halts the run."""
