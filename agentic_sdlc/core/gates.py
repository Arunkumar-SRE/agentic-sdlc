"""Human-in-the-loop approval policies. Swappable: interactive terminal, unattended, scripted (tests/demos)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel


class ApprovalRequest(BaseModel):
    step: str
    reason: str  # "gate" or "low_confidence"
    summary: str
    confidence: float | None = None
    threshold: float | None = None


class ApprovalDecision(BaseModel):
    approved: bool
    reviewer: str
    note: str = ""


class ApprovalPolicy(Protocol):
    def decide(self, request: ApprovalRequest) -> ApprovalDecision: ...


class InteractiveApproval:
    """Asks on the terminal. EOF or anything but 'y' is a rejection: no silent approvals."""

    def __init__(self, reader: Callable[[str], str] = input) -> None:
        self._read = reader

    def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        print(f"\n=== Approval needed: {request.step} ({request.reason}) ===", file=sys.stderr)
        print(request.summary, file=sys.stderr)
        if request.confidence is not None:
            print(f"confidence={request.confidence:.2f} threshold={request.threshold}", file=sys.stderr)
        try:
            answer = self._read("Approve? [y/N/note]: ").strip()
        except EOFError:
            return ApprovalDecision(approved=False, reviewer="terminal", note="no input (EOF)")
        approved = answer.lower() in ("y", "yes")
        return ApprovalDecision(approved=approved, reviewer="terminal", note="" if approved else answer)


class AutoApproval:
    """Unattended mode. Approves routine gates but escalates low-confidence output unless explicitly accepted."""

    def __init__(self, accept_low_confidence: bool = False, reject_at: set[str] | None = None) -> None:
        self.accept_low_confidence = accept_low_confidence
        self.reject_at = reject_at or set()

    def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        if request.step in self.reject_at:
            return ApprovalDecision(approved=False, reviewer="scripted", note="rejected by --reject-at")
        if request.reason == "low_confidence" and not self.accept_low_confidence:
            return ApprovalDecision(
                approved=False, reviewer="auto",
                note=f"escalated: confidence {request.confidence} below {request.threshold}; a human must clarify "
                     "(or re-run with --accept-assumptions)")
        return ApprovalDecision(approved=True, reviewer="auto", note="auto-approved (unattended run)")
