"""Minimal, strict template rendering: `{{ dotted.path }}`. No logic in templates, no silent blanks."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from .errors import ConfigError

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w\.]*)\s*\}\}")


def lookup(context: Mapping[str, Any], dotted: str) -> Any:
    current: Any = context
    for part in dotted.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            raise ConfigError(f"template reference '{dotted}' not found (missing '{part}')")
    return current


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, default=str)


def render(template: str, context: Mapping[str, Any]) -> str:
    return PLACEHOLDER.sub(lambda m: _stringify(lookup(context, m.group(1))), template)


def resolve(value: Any, context: Mapping[str, Any]) -> Any:
    """Resolve tool arguments: a string that is exactly one placeholder keeps the referenced object's type."""
    if isinstance(value, str):
        whole = PLACEHOLDER.fullmatch(value.strip())
        if whole:
            return lookup(context, whole.group(1))
        return render(value, context)
    if isinstance(value, list):
        return [resolve(v, context) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, context) for k, v in value.items()}
    return value
