"""structlog setup: human-readable console + one JSON-lines audit file per run under logs/."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, TextIO

import structlog


class _Tee:
    """Writes each rendered JSON line to the run's audit file."""

    def __init__(self, handle: TextIO) -> None:
        self.handle = handle

    def __call__(self, _: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        rendered = structlog.processors.JSONRenderer(default=str)(None, "", dict(event_dict))
        self.handle.write((rendered.decode() if isinstance(rendered, bytes) else rendered) + "\n")
        self.handle.flush()
        return event_dict


def configure(log_file: Path | None, verbose: bool = False) -> structlog.stdlib.BoundLogger:
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        processors.append(_Tee(log_file.open("a", encoding="utf-8")))
    processors.append(structlog.dev.ConsoleRenderer(colors=False))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG if verbose else logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
    logger: structlog.stdlib.BoundLogger = structlog.get_logger("agentic_sdlc")
    return logger
