"""Default engine: dependency-driven topological executor on a thread pool. Standard library only.

Scheduling is by readiness, not by level: a step starts the moment its own dependencies are satisfied, so
independent branches (e.g. code generation and test generation) overlap.

Per step: condition check -> attempts with exponential backoff (retryable errors only; schema errors are fed back
to the model) -> on exhaustion halt / continue / fallback -> human gate -> feedback loop (re-open an upstream step
with this step's findings). The state is checkpointed after every transition, so a run can resume.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ..core.agent import Agent, build_agent
from ..core.config import AgentSpec, WorkflowSpec, resolve_schema
from ..core.context import RunContext
from ..core.errors import AgenticError, BudgetExceeded, HumanRejected, OutputValidationError, RetryableError
from ..core.gates import ApprovalRequest
from ..core.state import Approval, RunStatus, StepRecord, StepStatus, WorkflowState, utcnow
from .base import register_engine

FATAL = (BudgetExceeded, HumanRejected)


@dataclass
class Outcome:
    output: BaseModel | None = None
    error: Exception | None = None
    attempts: int = 0
    duration_s: float = 0.0
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class SimpleEngine:
    name = "simple"

    def __init__(self) -> None:
        self._fault_lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def execute(self, workflow: WorkflowSpec, state: WorkflowState, ctx: RunContext) -> WorkflowState:
        graph = workflow.graph()
        agents: dict[str, Agent] = {spec.id: build_agent(spec) for spec in workflow.agents}
        faults = dict(ctx.settings.faults)
        for step in graph.nodes:
            rec = state.steps.setdefault(step, StepRecord())
            if rec.status in (StepStatus.RUNNING, StepStatus.FAILED, StepStatus.BLOCKED):
                rec.status = StepStatus.PENDING  # resume: retry what did not finish
        state.status, state.halted_reason = "running", None
        generation: dict[str, int] = {s: 0 for s in graph.nodes}
        in_flight: dict[Future[Outcome], tuple[str, int]] = {}
        halted = False
        self._save(state, ctx)

        with ThreadPoolExecutor(max_workers=workflow.max_parallel, thread_name_prefix="agent") as pool:
            while True:
                if not halted:
                    self._block_downstream_of_failures(state, graph.deps)
                    for step in graph.order():
                        rec = state.steps[step]
                        busy = any(s == step for s, _ in in_flight.values())
                        if rec.status != StepStatus.PENDING or busy:
                            continue
                        if not all(state.steps[d].status.satisfies_dependents for d in graph.deps[step]):
                            continue
                        spec = workflow.agent(step)
                        if spec.when and not self._condition(spec, state):
                            rec.status, rec.finished_at = StepStatus.SKIPPED, utcnow()
                            ctx.log.info("step_skipped", step=step, condition=spec.when.model_dump(by_alias=True))
                            self._save(state, ctx)
                            continue
                        rec.status, rec.started_at, rec.error = StepStatus.RUNNING, utcnow(), None
                        snapshot = state.model_copy(deep=True)
                        future = pool.submit(self._run_step, agents[step], spec, snapshot, ctx, faults)
                        in_flight[future] = (step, generation[step])
                        ctx.log.info("step_started", step=step, round=rec.rounds)
                if not in_flight:
                    break
                done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
                for future in done:
                    step, gen = in_flight.pop(future)
                    if gen != generation[step]:
                        ctx.log.info("stale_result_discarded", step=step)  # re-opened by a feedback loop meanwhile
                        continue
                    outcome = future.result()
                    halted = self._handle(outcome, step, workflow, state, ctx, graph, generation, halted) or halted
                    self._save(state, ctx)

        state.usage = ctx.budget.snapshot()
        if state.status == "running":
            state.status = self._final_status(workflow, state)
        state.record("run_finished", status=state.status)
        self._save(state, ctx)
        ctx.log.info("run_finished", status=state.status, reason=state.halted_reason)
        return state

    # ------------------------------------------------------------------ worker side
    def _run_step(self, agent: Agent, spec: AgentSpec, state: WorkflowState, ctx: RunContext, faults: dict[str, int]) -> Outcome:
        outcome = Outcome()
        started = time.perf_counter()
        policy = spec.retry
        while outcome.attempts < policy.max_attempts:
            outcome.attempts += 1
            try:
                with self._fault_lock:
                    inject = faults.get(spec.id, 0) > 0
                    if inject:
                        faults[spec.id] -= 1
                if inject:
                    outcome.events.append(("fault_injected", {"attempt": outcome.attempts}))
                    raise RetryableError("injected transient fault")
                ctx.budget.check()
                outcome.output = agent.run(state, ctx)
                outcome.error = None
                break
            except FATAL as exc:
                outcome.error = exc
                break
            except RetryableError as exc:
                outcome.error = exc
                if isinstance(exc, OutputValidationError):
                    # Self-correction: the next attempt sees exactly why the previous output was rejected.
                    state.feedback.setdefault(spec.id, []).append(f"Previous output was rejected: {exc}")
                if outcome.attempts < policy.max_attempts:
                    delay = policy.delay(outcome.attempts)
                    outcome.events.append(("retry", {"attempt": outcome.attempts, "error": str(exc)[:200], "backoff_s": delay}))
                    ctx.log.warning("step_retry", step=spec.id, attempt=outcome.attempts, error=str(exc)[:200], backoff_s=delay)
                    time.sleep(delay)
            except AgenticError as exc:  # deterministic failure: retrying would repeat it
                outcome.error = exc
                break
            except Exception as exc:  # noqa: BLE001 - an agent bug must not kill the engine
                outcome.error = exc
                break
        outcome.duration_s = round(time.perf_counter() - started, 2)
        return outcome

    # ------------------------------------------------------------------ main-thread bookkeeping
    def _handle(self, outcome: Outcome, step: str, workflow: WorkflowSpec, state: WorkflowState, ctx: RunContext,
                graph: Any, generation: dict[str, int], halted: bool) -> bool:
        spec = workflow.agent(step)
        rec = state.steps[step]
        rec.attempts += outcome.attempts
        rec.duration_s = round(rec.duration_s + outcome.duration_s, 2)
        rec.finished_at = utcnow()
        for kind, detail in outcome.events:
            state.record(kind, step=step, **detail)

        if outcome.error is not None:
            err = f"{type(outcome.error).__name__}: {outcome.error}"
            rec.error = err[:500]
            if isinstance(outcome.error, FATAL):
                rec.status = StepStatus.FAILED
                return self._halt(state, ctx, "halted", f"{step}: {err}")
            if spec.on_failure == "fallback" and spec.fallback is not None:
                output = resolve_schema(spec.output_schema).model_validate(spec.fallback)
                self._store(state, ctx, step, output)
                rec.status = StepStatus.DEGRADED
                state.record("fallback", step=step, error=err[:200])
                ctx.log.warning("step_degraded", step=step, error=err[:200])
                return False
            rec.status = StepStatus.FAILED
            ctx.log.error("step_failed", step=step, error=err[:300], attempts=rec.attempts)
            if spec.on_failure == "halt":
                return self._halt(state, ctx, "failed", f"{step}: {err}")
            return False

        assert outcome.output is not None
        self._store(state, ctx, step, outcome.output)
        rec.status = StepStatus.SUCCEEDED
        rec.cached = bool(ctx.extras.get("llm_info", {}).get(step, {}).get("cached"))
        ctx.log.info("step_succeeded", step=step, attempts=outcome.attempts, seconds=outcome.duration_s)
        if halted:
            return False
        if spec.gate and not self._gate(spec, outcome.output, state, ctx):
            return True
        if spec.feedback:
            self._feedback(spec, outcome.output, state, ctx, graph, generation)
        return False

    def _store(self, state: WorkflowState, ctx: RunContext, step: str, output: BaseModel) -> None:
        state.outputs[step] = output.model_dump(mode="json")
        ctx.sandbox.write_text(f"artifacts/{step}.json", json.dumps(state.outputs[step], indent=2))

    def _gate(self, spec: AgentSpec, output: BaseModel, state: WorkflowState, ctx: RunContext) -> bool:
        gate = spec.gate
        assert gate is not None
        data = output.model_dump(mode="json")
        confidence = data.get(gate.confidence_field)
        low = gate.min_confidence is not None and isinstance(confidence, (int, float)) and confidence < gate.min_confidence
        if not gate.required and not low:
            return True
        summary = str(data.get(gate.summary_field, "")) if gate.summary_field else json.dumps(data, indent=2)[:1500]
        request = ApprovalRequest(step=spec.id, reason="low_confidence" if low else "gate", summary=summary,
                                  confidence=confidence if low else None, threshold=gate.min_confidence if low else None)
        decision = ctx.approvals.decide(request)
        state.approvals.append(Approval(step=spec.id, approved=decision.approved, reviewer=decision.reviewer,
                                        reason=request.reason, note=decision.note))
        ctx.log.info("gate_decision", step=spec.id, reason=request.reason, approved=decision.approved, reviewer=decision.reviewer)
        if not decision.approved:
            # A rejected output must not be reused: on resume this step runs again (with the reviewer's note).
            rec = state.steps[spec.id]
            rec.status, rec.error = StepStatus.FAILED, f"not approved at gate: {decision.note}"[:500]
            if decision.note:
                state.feedback.setdefault(spec.id, []).append(f"Reviewer rejected the previous output: {decision.note}")
            if low:
                state.record("escalated", step=spec.id, confidence=confidence, threshold=gate.min_confidence)
            self._halt(state, ctx, "halted", f"gate '{spec.id}' not approved ({request.reason}): {decision.note}")
            return False
        return True

    def _feedback(self, spec: AgentSpec, output: BaseModel, state: WorkflowState, ctx: RunContext,
                  graph: Any, generation: dict[str, int]) -> None:
        loop = spec.feedback
        assert loop is not None
        data = output.model_dump(mode="json")
        if data.get(loop.pass_field):
            return
        if loop.skip_field and data.get(loop.skip_field):
            state.record("feedback_skipped", step=spec.id, target=loop.target, reason=str(data[loop.skip_field])[:300])
            ctx.log.warning("feedback_skipped", step=spec.id, reason=str(data[loop.skip_field])[:300])
            return
        target = state.steps[loop.target]
        if target.rounds >= loop.max_rounds:
            state.record("feedback_exhausted", step=spec.id, target=loop.target, rounds=target.rounds)
            ctx.log.warning("feedback_exhausted", step=spec.id, target=loop.target, rounds=target.rounds)
            return
        text = str(data.get(loop.feedback_field) or f"{spec.id} reported failure")
        state.feedback.setdefault(loop.target, []).append(f"[round {target.rounds + 1} from {spec.id}] {text}")
        reopened = [loop.target, *sorted(graph.descendants(loop.target))]
        for step in reopened:
            generation[step] += 1
            rec = state.steps[step]
            rec.status, rec.error = StepStatus.PENDING, None
            state.outputs.pop(step, None)
        target.rounds += 1
        state.record("feedback_loop", step=spec.id, target=loop.target, round=target.rounds, reopened=reopened,
                     feedback=text[:400])
        ctx.log.warning("feedback_loop", step=spec.id, target=loop.target, round=target.rounds, reopened=reopened)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _condition(spec: AgentSpec, state: WorkflowState) -> bool:
        cond = spec.when
        assert cond is not None
        head, *rest = cond.path.split(".")
        value: Any = state.inputs if head == "inputs" else state.outputs.get(head)
        for part in rest:
            value = value.get(part) if isinstance(value, dict) else None
        matched = value in cond.in_
        return not matched if cond.negate else matched

    @staticmethod
    def _block_downstream_of_failures(state: WorkflowState, deps: dict[str, list[str]]) -> None:
        changed = True
        while changed:
            changed = False
            for step, parents in deps.items():
                rec = state.steps[step]
                if rec.status == StepStatus.PENDING and any(
                        state.steps[p].status in (StepStatus.FAILED, StepStatus.BLOCKED) for p in parents):
                    rec.status, rec.error, changed = StepStatus.BLOCKED, "upstream step failed", True

    @staticmethod
    def _halt(state: WorkflowState, ctx: RunContext, status: RunStatus, reason: str) -> bool:
        if state.status == "running":
            state.status = status
            state.halted_reason = reason
            state.record("halted", status=status, reason=reason[:300])
            ctx.log.error("run_halted", status=status, reason=reason[:300])
        return True

    @staticmethod
    def _final_status(workflow: WorkflowSpec, state: WorkflowState) -> RunStatus:
        bad = {StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.DEGRADED, StepStatus.PENDING}
        if any(r.status in bad for r in state.steps.values()):
            return "needs_attention"
        for spec in workflow.agents:
            if spec.feedback and spec.id in state.outputs and not state.outputs[spec.id].get(spec.feedback.pass_field):
                return "needs_attention"
        return "succeeded"

    @staticmethod
    def _save(state: WorkflowState, ctx: RunContext) -> None:
        state.usage = ctx.budget.snapshot()
        state.save(ctx.sandbox.root / "state.json")


@register_engine("simple")
def _factory() -> SimpleEngine:
    return SimpleEngine()
