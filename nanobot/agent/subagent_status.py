from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from nanobot.providers.base import LLMUsage

SubagentState = Literal[
    "queued", "running", "waiting_input", "stopping", "done", "incomplete", "error", "cancelled", "interrupted",
]
SubagentBackend = Literal["native", "codex"]
_BACKEND_NAMES: dict[SubagentBackend, str] = {"native": "Subagent", "codex": "Codex"}
SubagentKind = Literal["general", "code_change"]
ReviewDetail = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
ReviewIndex = Annotated[int, Field(strict=True, ge=1)]
ReviewRecheckStatus = Literal["resolved", "open", "unverified", "superseded"]


class SubagentSessionError(ValueError):
    """A private task session could not be read safely."""


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    requirement: ReviewDetail
    path: ReviewDetail
    trigger: ReviewDetail
    change: ReviewDetail
    line: ReviewIndex | None = None


@dataclass(frozen=True, slots=True)
class ReviewRecheck:
    finding: ReviewFinding
    status: ReviewRecheckStatus
    detail: str


@dataclass(frozen=True, slots=True)
class ReviewVerification:
    requirement: str
    detail: str
    source: Literal["static", "command", "unverified"]
    path: str | None = None
    line: int | None = None
    command_id: int | None = None


@dataclass(frozen=True, slots=True)
class SubagentReview:
    verdict: Literal["pass", "changes_requested", "inconclusive", "skipped"]
    summary: str
    round: int = 0
    candidate: str | None = None
    verification: tuple[ReviewVerification, ...] = ()
    rechecks: tuple[ReviewRecheck, ...] = ()


@dataclass(slots=True)
class TaskProgress:
    key: str
    at: float
    summary: str
    detail: str = ""
    next_step: str = ""


@dataclass(slots=True)
class SubagentStatus:
    """Observable task data, detached from execution and process resources."""

    task_id: str
    label: str
    task_description: str
    started_at: float          # time.monotonic()
    finished_at: float | None = None
    # Runner checkpoints refine phase without changing the control lifecycle state.
    phase: str = "queued"
    owner: str = ""
    state: SubagentState = "queued"
    receipts: dict[str, str] = field(default_factory=dict)
    private_receipts: set[str] = field(default_factory=set)
    result: str | None = None
    partial: bool = False
    iteration: int = 0
    tool_events: list[dict[str, str]] = field(default_factory=list)
    usage: LLMUsage | None = None
    stop_reason: str | None = None
    error: str | None = None
    origin_message_id: str | None = None
    origin_turn_id: str | None = None
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    revision: int = 0
    project: str | None = None
    worktree_id: str | None = None
    worktree: str | None = None
    branch: str | None = None
    resume_task_id: str | None = None
    base_ref: str | None = None
    adopt_branch: str | None = None
    reply_first: bool = False
    backend: SubagentBackend = "native"
    kind: SubagentKind = "general"
    review_base: str | None = None
    review_rules: str | None = None
    review_findings: tuple[ReviewFinding, ...] = ()
    review: SubagentReview | None = None
    progress: str | None = None
    progress_at: float | None = None
    recent_progress: list[TaskProgress] = field(default_factory=list)
    question: str | None = None
    question_id: str | None = None
    model: str | None = None
    effort: str | None = None
    cwd: str | None = None
    codex_thread: str | None = None
    codex_turn: str | None = None
    origin_channel: str | None = None
    origin_chat_id: str | None = None
    origin_sender_id: str | None = None
    origin_generation: str | None = None
    origin_route: dict[str, Any] = field(default_factory=dict)
    pending_inputs: list[tuple[str, str]] = field(default_factory=list)
    notice_ids: dict[str, str] = field(default_factory=dict)

    @property
    def reference(self) -> str:
        return self.task_id[:8]

    @property
    def backend_name(self) -> str:
        return _BACKEND_NAMES.get(self.backend, self.backend)

    def record_progress(
        self, key: str, summary: str, *, detail: str = "", next_step: str = "",
    ) -> bool:
        """Store the latest report and a bounded log for task queries."""
        if any(entry.key == key for entry in self.recent_progress):
            return False
        self.progress, self.progress_at = summary, time.time()
        self.recent_progress = [
            *self.recent_progress[-2:], TaskProgress(key, self.progress_at, summary, detail, next_step),
        ]
        return True

    def parent_receipts(self) -> dict[str, str]:
        return {key: value for key, value in self.receipts.items() if key not in self.private_receipts}

    def as_dict(self) -> dict[str, object]:
        """Serialize task observations without exposing ownership or resources."""
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return {
            "task_id": self.task_id,
            "revision": self.revision,
            "origin_message_id": self.origin_message_id,
            "origin_turn_id": self.origin_turn_id,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "label": self.label,
            "task_description": self.task_description,
            "state": self.state,
            "phase": self.phase,
            "elapsed_seconds": round(max(0.0, end - self.started_at), 1),
            "iteration": self.iteration,
            "tool_events": [dict(event) for event in self.tool_events],
            "usage": self.usage.to_dict() if self.usage is not None else None,
            "receipts": self.parent_receipts(),
            "result": self.result,
            "partial": self.partial,
            "stop_reason": self.stop_reason,
            "error": self.error,
            "project": self.project,
            "worktree": self.worktree,
            "branch": self.branch,
            "resume_task_id": self.resume_task_id,
            "base_ref": self.base_ref,
            "backend": self.backend,
            "kind": self.kind,
            "review": ({
                "verdict": self.review.verdict, "summary": self.review.summary,
                "round": self.review.round, "candidate": self.review.candidate,
                "verification": [asdict(item) for item in self.review.verification],
                "rechecks": [asdict(item) for item in self.review.rechecks],
            } if self.review else None),
            "reference": self.reference,
            "progress": self.progress,
            "progress_at": self.progress_at,
            "question": self.question,
            "question_id": self.question_id if self.state == "waiting_input" else None,
            "model": self.model,
            "effort": self.effort,
        }
