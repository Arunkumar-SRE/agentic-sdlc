"""Test doubles: a scripted LLM provider, test tools and schemas. Registered the same way a team would add its own."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from agentic_sdlc.core.config import Settings
from agentic_sdlc.core.gates import ApprovalPolicy, AutoApproval
from agentic_sdlc.core.llm import LLMRequest, LLMResponse, register_provider
from agentic_sdlc.core.state import WorkflowState
from agentic_sdlc.runner import run_workflow
from agentic_sdlc.tools.registry import ToolEnv, tool


class Note(BaseModel):
    text: str
    confidence: float = 1.0
    kind: str = "x"


class Check(BaseModel):
    passed: bool
    feedback: str = ""


class ScriptedClient:
    """Answers per schema name from a queue. Items: dict, callable(request) -> dict, or an exception to raise.
    The last item repeats once the queue is down to one."""

    name = "scripted"
    fingerprint = "scripted"
    script: dict[str, list[Any]] = {}
    calls: list[LLMRequest] = []
    lock = threading.Lock()

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    def complete(self, request: LLMRequest) -> LLMResponse:
        with ScriptedClient.lock:
            ScriptedClient.calls.append(request)
            queue = ScriptedClient.script[request.schema_name]
            item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        data = item(request) if callable(item) else item
        return LLMResponse(data=data, provider=self.name, model=request.model, input_tokens=10, output_tokens=10)

    @classmethod
    def reset(cls, script: dict[str, list[Any]] | None = None) -> None:
        cls.script = script or {}
        cls.calls = []

    @classmethod
    def calls_for(cls, agent: str) -> list[LLMRequest]:
        return [c for c in cls.calls if c.metadata.get("agent") == agent]


@register_provider("scripted")
def _scripted(options: dict[str, Any]) -> ScriptedClient:
    return ScriptedClient(options)


CHECK_RESULTS: list[dict[str, Any]] = []


@tool(side_effects="read")
def scripted_check(env: ToolEnv, value: Any = None) -> dict[str, Any]:
    """Pops the next scripted verification result (last one repeats)."""
    return CHECK_RESULTS.pop(0) if len(CHECK_RESULTS) > 1 else CHECK_RESULTS[0]


@tool(side_effects="read")
def echo(env: ToolEnv, value: Any = None) -> dict[str, Any]:
    """Returns its argument."""
    return {"passed": True, "feedback": "", "value": value}


def slow_note(text: str, seconds: float, log: list[tuple[str, float, float]]) -> Any:
    def answer(request: LLMRequest) -> dict[str, Any]:
        start = time.perf_counter()
        time.sleep(seconds)
        log.append((request.metadata["agent"], start, time.perf_counter()))
        return {"text": text}
    return answer


def write_workflow(directory: Path, agents: list[dict[str, Any]], **top: Any) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "p.md").write_text("req={{ requirement }}\nfeedback={{ feedback }}\n", encoding="utf-8")
    (directory / "sys.md").write_text("system", encoding="utf-8")
    doc = {"name": "t", "system_prompt": "sys.md", "models": {"standard": {"provider": "scripted", "model": "m"}},
           "cache": False, "agents": agents, **top}
    path = directory / "wf.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def llm(agent_id: str, schema: str = "tests.helpers.Note", **extra: Any) -> dict[str, Any]:
    return {"id": agent_id, "prompt": "p.md", "output_schema": schema, "retry": {"max_attempts": 3, "backoff_s": 0}, **extra}


def run(tmp: Path, workflow: Path, approvals: ApprovalPolicy | None = None, run_id: str | None = None,
        resume: bool = False, **settings: Any) -> WorkflowState:
    s = Settings(workspace_dir=tmp / "ws", logs_dir=tmp / "logs", cache_dir=tmp / "cache", **settings)
    return run_workflow("do the thing", str(workflow), s, approvals or AutoApproval(), {"target_dir": str(tmp)},
                        run_id=run_id, resume=resume)
