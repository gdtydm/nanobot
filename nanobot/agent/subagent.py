from __future__ import annotations

import asyncio
import json
import time
import uuid
import warnings
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast

from loguru import logger

from nanobot.agent.codex_task import CodexTaskError, CodexTurn
from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.hooks import create_file_edit_activity_hook
from nanobot.agent.project_instructions import load_project_instructions
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.session_activity import SessionActivity
from nanobot.agent.skills import SkillsLoader, project_skill_root
from nanobot.agent.subagent_project import (
    ProjectWorktree,
    prepare_project_worktree,
    reclaim_project_worktrees,
)
from nanobot.agent.subagent_review import (
    MAX_REVISIONS,
    REVIEW_INSTRUCTIONS,
    ReviewReadTool,
    ReviewUnavailableError,
    ReviewVerdict,
    ReviewWorktree,
    review_input,
)
from nanobot.agent.subagent_sessions import SubagentSessions
from nanobot.agent.subagent_status import (
    SubagentBackend,
    SubagentKind,
    SubagentReview,
    SubagentSessionError,
    SubagentStatus,
)
from nanobot.agent.subagent_status import SubagentState as SubagentState
from nanobot.agent.subagent_trace import SubagentRequestTrace
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import (
    RequestContext,
    ToolContext,
    bind_request_context,
    reset_request_context,
)
from nanobot.agent.tools.exec_session import ExecSessionManager
from nanobot.agent.tools.file_state import FileStates
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.agent.tools.subagent_config import SubagentProjectConfig, SubagentToolConfig
from nanobot.agent.turn_hooks import AgentTurnHookSpec, build_agent_turn_hook
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.bus.runtime_events import RuntimeEventContext, SubagentTaskChanged
from nanobot.channels.notification_routes import notification_metadata
from nanobot.config.network import NetworkConfig
from nanobot.config.schema import AgentDefaults, ToolsConfig
from nanobot.context_sources import capture_sources, without_source_capture
from nanobot.events import AgentEvent, EventSink
from nanobot.llm_usage.context import LLMUsageSource, current_llm_usage_source
from nanobot.providers.base import LLMProvider, LLMUsage, ToolCallRequest
from nanobot.security.workspace_access import (
    WorkspaceScope,
    bind_workspace_scope,
    build_workspace_scope,
    default_access_mode,
    reset_workspace_scope,
    workspace_sandbox_status,
)
from nanobot.session.history_visibility import HIDDEN_HISTORY_META
from nanobot.session.manager import Session, SessionManager, SessionPolicy
from nanobot.utils.cancellation import shield_and_drain
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.subagent_channel_display import task_display_text, task_query_block

if TYPE_CHECKING:
    from nanobot.agent.memory import Consolidator


SUBAGENT_REPLY_FIRST_META = "_subagent_reply_first"
SUBAGENT_ORIGIN_SENDER_META = "_subagent_origin_sender_id"


class _SubagentOrigin(TypedDict):
    channel: str
    chat_id: str
    session_key: str | None
    sender_id: NotRequired[str | None]
    llm_usage_source: NotRequired[LLMUsageSource]




class SubagentControlError(ValueError):
    """A task control request violates ownership or admission rules."""


class SubagentMessageReceipt(TypedDict):
    task_id: str
    message_id: str
    receipt: Literal["accepted"]
    delivered: Literal[False]


@dataclass(frozen=True, slots=True)
class _SubagentOutcome:
    """Execution outcome, independent of process cleanup errors."""

    state: Literal["done", "incomplete", "error", "cancelled"]
    result: str
    stop_reason: str | None = None
    error: str | None = None


@dataclass(slots=True)
class _SubagentTask:
    """Own one execution, its inbox, and resources until cleanup succeeds."""

    status: SubagentStatus
    session: Session
    runtime: LLMRuntime | None
    origin: _SubagentOrigin
    parent_runtime: LLMRuntime | None = None
    origin_message_id: str | None = None
    workspace_scope: WorkspaceScope | None = None
    project_config: SubagentProjectConfig | None = None
    announce: bool = True
    begun: bool = False
    suppress_notice: bool = False
    inbox: list[tuple[str, str]] = field(default_factory=list)
    reported_milestones: set[str] = field(default_factory=set)
    input_received: asyncio.Event | None = None
    outcome: _SubagentOutcome | None = None
    exec_manager: ExecSessionManager = field(default_factory=ExecSessionManager)
    cleanup_task: asyncio.Task[int] | None = None
    codex: CodexTurn | None = None
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    receipt_attempted: asyncio.Event = field(default_factory=asyncio.Event)

    def raise_if_stopping(self) -> None:
        if self.outcome is not None:
            raise asyncio.CancelledError

    def decide(self, outcome: _SubagentOutcome) -> bool:
        """Seal execution once, before cancellation or cleanup can yield."""
        if self.outcome is not None:
            return False
        self.outcome = outcome
        self.status.state = self.status.phase = "stopping"
        return True

    def finish(self, max_chars: int, cleanup_error: str | None = None) -> _SubagentOutcome:
        execution_outcome = self.outcome
        if execution_outcome is None:
            raise RuntimeError("cannot finish a task before execution has an outcome")
        outcome = execution_outcome
        if cleanup_error is not None:
            outcome = _SubagentOutcome("error", cleanup_error, "error", cleanup_error)
        status = self.status
        has_partial_result = outcome.state != "done" and bool(status.result)
        if outcome.state != "done" and status.result:
            outcome = replace(outcome, result=status.result)
        status.state = status.phase = outcome.state
        status.finished_at = time.monotonic()
        status.completed_at = time.time()
        status.result = outcome.result[:max_chars]
        status.partial = has_partial_result
        status.stop_reason = outcome.stop_reason
        status.error = outcome.error[:max_chars] if outcome.error else None
        status.task_description = status.task_description[:max_chars]
        status.label = status.label[:256]
        status.tool_events = []
        for message_id, _ in self.inbox:
            status.receipts[message_id] = "undelivered"
        self.inbox.clear()
        for message_id, _ in status.pending_inputs:
            status.receipts[message_id] = "undelivered"
        status.pending_inputs.clear()
        status.question = None
        # Failed cleanup can retain this task after its public status is evicted.
        self.outcome = replace(
            execution_outcome,
            result=execution_outcome.result[:max_chars],
            error=execution_outcome.error[:max_chars] if execution_outcome.error else None,
        )
        return outcome

    def snapshot(self) -> SubagentStatus:
        return deepcopy(self.status)

    def cleanup_succeeded(self) -> bool:
        cleanup = self.cleanup_task
        return (
            cleanup is not None and cleanup.done()
            and not cleanup.cancelled() and cleanup.exception() is None
        )


class _SubagentHook(AgentHook):
    """Update task status at runner boundaries."""

    def __init__(self, status: SubagentStatus | None = None,
                 *, check_active: Callable[[], None] | None = None,
                 native_commentary: bool = False,
                 max_result_chars: int = 16000,
                 on_status: Callable[[SubagentStatus], Awaitable[None]] | None = None) -> None:
        super().__init__()
        self._status = status
        self._check_active = check_active
        self._native_commentary = native_commentary
        self._max_result_chars = max_result_chars
        self._on_status = on_status

    def _remember_output(self, context: AgentHookContext) -> None:
        if self._status is None or context.error or context.response is None:
            return
        content = context.response.content
        if content and content.strip():
            self._status.result = content[:self._max_result_chars]
            self._status.partial = True

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        self._remember_output(context)
        self._remember_commentary(context)
        if self._status is not None:
            self._status.tool_events = [
                {"name": call.name, "status": "running"} for call in context.tool_calls
            ]
        if self._status is not None and self._on_status is not None:
            await self._on_status(self._status)

    async def before_execute_tool(
        self, context: AgentHookContext, tool_call: ToolCallRequest,
        tool: Tool | None, params: dict[str, Any],
    ) -> None:
        if self._check_active is not None:
            self._check_active()

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_events)
        self._status.usage = context.usage
        self._remember_output(context)
        if context.response is not None and not context.response.should_execute_tools:
            self._remember_commentary(context)
        if context.error:
            self._status.error = str(context.error)
        if self._on_status is not None:
            await self._on_status(self._status)

    def _remember_commentary(self, context: AgentHookContext) -> None:
        if not self._native_commentary or self._status is None or context.response is None:
            return
        for item in context.response.commentary:
            summary = item.text.strip()[:800]
            if summary:
                self._status.record_progress(item.id or summary, summary)


@tool_parameters(tool_parameters_schema(
    required=["summary"],
    summary=StringSchema(
        "One or two sentences about what changed, with relevant evidence or next steps.",
        min_length=1, max_length=800,
    ),
    milestone=StringSchema("Optional stable key for deduplicating retries.", min_length=1, max_length=120),
    detail=StringSchema("Optional detail shown in the task's on-demand log.", max_length=500),
    next_step=StringSchema("Optional next step shown in the task's on-demand status.", max_length=200),
))
class _ReportProgressTool(Tool):
    """Save a short progress update for on-demand task queries."""

    _plugin_discoverable = False

    def __init__(self, report: Callable[[str, str, str, str], Awaitable[str]]) -> None:
        self._report = report

    @property
    def name(self) -> str:
        return "report_progress"

    @property
    def description(self) -> str:
        return "Record a brief progress update for /task when a meaningful milestone changes the situation."

    async def execute(
        self, *, summary: str = "", milestone: str = "", detail: str = "", next_step: str = "", **kwargs: Any,
    ) -> str:
        summary = summary.strip()
        if not summary:
            return ToolResult.error("A progress update is required.")
        return await self._report(milestone.strip() or summary, summary, detail.strip(), next_step.strip())


@tool_parameters(tool_parameters_schema(
    required=["question"],
    question=StringSchema("The question the user must answer to continue.", min_length=1, max_length=1000),
))
class _RequestInputTool(Tool):
    """Ask the user one question that blocks work on a background task."""

    _plugin_discoverable = False

    def __init__(self, request: Callable[[str], Awaitable[str]]) -> None:
        self._request = request

    @property
    def name(self) -> str:
        return "request_input"

    @property
    def description(self) -> str:
        return "Ask one concrete question and wait when a user decision blocks further work."

    async def execute(self, *, question: str = "", **kwargs: Any) -> str:
        question = question.strip()
        if not question:
            return ToolResult.error("A question is required.")
        return await self._request(question)


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        workspace: Path | None = None,
        bus: MessageBus | None = None,
        max_tool_result_chars: int | None = None,
        model: str | None = None,
        tools_config: ToolsConfig | None = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        *,
        consolidator: Consolidator,
        network_config: NetworkConfig | None = None,
        session_manager: SessionManager | None = None,
        resolve_model_preset: Callable[[str], LLMRuntime] | None = None,
        subagent_config_loader: Callable[[], SubagentToolConfig] | None = None,
    ):
        if cast(object, consolidator) is None:
            raise TypeError("SubagentManager requires a consolidator")
        if workspace is None:
            raise TypeError("SubagentManager.__init__() missing required argument: 'workspace'")
        if bus is None:
            raise TypeError("SubagentManager.__init__() missing required argument: 'bus'")
        if max_tool_result_chars is None:
            raise TypeError(
                "SubagentManager.__init__() missing required argument: 'max_tool_result_chars'"
            )
        if model is not None and provider is None:
            raise TypeError("SubagentManager model compatibility argument requires provider")

        defaults = AgentDefaults()
        self._compat_runtime: LLMRuntime | None = None
        if provider is not None:
            warnings.warn(
                "SubagentManager provider/model constructor arguments are deprecated; "
                "pass runtime=... to spawn() instead",
                DeprecationWarning,
                stacklevel=2,
            )
            self._compat_runtime = LLMRuntime.capture(
                provider,
                model or provider.get_default_model(),
                context_window_tokens=defaults.context_window_tokens,
            )
        self.workspace = workspace
        self.bus = bus
        self.tools_config = tools_config or ToolsConfig()
        self._resolve_model_preset = resolve_model_preset
        self._subagent_config_loader = subagent_config_loader
        self.network_config = network_config or NetworkConfig()
        self.max_tool_result_chars = max_tool_result_chars
        self.restrict_to_workspace = restrict_to_workspace
        self.disabled_skills = set(disabled_skills or [])
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else defaults.max_tool_iterations
        )
        self.max_concurrent_subagents = (
            max_concurrent_subagents
            if max_concurrent_subagents is not None
            else defaults.max_concurrent_subagents
        )
        self.consolidator = consolidator
        self._run_slots = asyncio.Semaphore(self.max_concurrent_subagents)
        self.runner = AgentRunner()
        self._exec_session_manager = ExecSessionManager()
        self._running_tasks: dict[str, asyncio.Task[str]] = {}
        self._tasks: dict[str, _SubagentTask] = {}
        self._terminal_statuses: dict[str, SubagentStatus] = {}
        self._closed = False
        self._close_task: asyncio.Task[int] | None = None
        self._admitting = 0
        self._admitting_worktrees: set[str] = set()
        self._worktree_lock = asyncio.Lock()
        self._worktree_sweep_needed = asyncio.Event()
        self._worktree_sweeper: asyncio.Task[None] | None = None
        self.sessions = SubagentSessions(session_manager) if session_manager is not None else None
        self.send_task_notice: Callable[[OutboundMessage], Awaitable[str | None]] | None = None
        self._codex_admission = asyncio.Lock()

    MAX_ACTIVE = 128
    MAX_TERMINAL = 128
    MAX_INBOX = 16
    MAX_MESSAGES = 128
    MAX_MESSAGE_BYTES = 8192

    CANCEL_WAIT_SECONDS = 5.0

    async def _save_status(
        self, status: SubagentStatus,
    ) -> None:
        status.revision += 1
        if self.sessions is not None and not await self.sessions.save(status):
            return
        self._publish_status(status)

    def _publish_status(self, status: SubagentStatus) -> None:
        origin = self._tasks[status.task_id].origin
        self.bus.publish_nowait(SubagentTaskChanged(
            context=RuntimeEventContext(
                channel=origin["channel"], chat_id=origin["chat_id"], session_key=status.owner,
                metadata=dict(status.origin_route),
            ),
            task_id=status.task_id,
        ))

    async def recover_interrupted(self) -> None:
        """Recover observations only after the host has claimed execution ownership."""
        if self.sessions is not None:
            await self.sessions.interrupt_pending()
            if self.tools_config.subagent.projects and self._worktree_sweeper is None:
                self._worktree_sweeper = asyncio.create_task(self._maintain_project_worktrees())

    def _worktree_in_use(self, worktree_id: str) -> bool:
        return worktree_id in self._admitting_worktrees or any(
            record.status.worktree_id == worktree_id
            and (record.status.finished_at is None or not record.cleanup_succeeded())
            for record in self._tasks.values()
        )

    async def _maintain_project_worktrees(self) -> None:
        assert self.sessions is not None
        projects = self.tools_config.subagent.projects
        while True:
            self._worktree_sweep_needed.clear()
            try:
                async with self._worktree_lock:
                    statuses = await asyncio.to_thread(self.sessions.load_all)
                    await reclaim_project_worktrees(
                        projects, self.workspace, statuses, self._worktree_in_use, now=time.time(),
                    )
            except (OSError, RuntimeError, SubagentSessionError):
                logger.exception("Project worktree retention check failed")
            try:
                await asyncio.wait_for(self._worktree_sweep_needed.wait(), timeout=3600)
            except TimeoutError:
                pass

    def _start_cleanup(self, record: _SubagentTask, *, retry: bool = False) -> asyncio.Task[int]:
        cleanup = record.cleanup_task
        if retry and cleanup is not None and cleanup.done():
            record.cleanup_task = None
        if record.cleanup_task is None:
            record.cleanup_task = asyncio.create_task(record.exec_manager.close_all())
            record.cleanup_task.add_done_callback(partial(self._cleanup_done, record))
        return record.cleanup_task

    def _cleanup_done(self, record: _SubagentTask, task: asyncio.Task[int]) -> None:
        if not task.cancelled() and task.exception() is None:
            self._release_task(record)

    def _release_task(self, record: _SubagentTask) -> None:
        if (record.status.finished_at is not None
                and record.status.task_id not in self._running_tasks
                and record.cleanup_succeeded()):
            self._tasks.pop(record.status.task_id, None)

    async def _finish(self, record: _SubagentTask,
                cleanup_error: str | None = None) -> _SubagentOutcome:
        outcome = record.finish(self.max_tool_result_chars, cleanup_error)
        await self._save_transcript(record)
        await self._save_status(record.status)
        self._terminal_statuses[record.status.task_id] = record.snapshot()
        while len(self._terminal_statuses) > self.MAX_TERMINAL:
            del self._terminal_statuses[next(iter(self._terminal_statuses))]
        return outcome

    async def _drain_inbox(self, record: _SubagentTask) -> list[dict[str, str]]:
        record.raise_if_stopping()
        snapshot, record.inbox = record.inbox, []
        for message_id, _ in snapshot:
            record.status.receipts[message_id] = "delivered"
        if snapshot:
            await self._save_status(record.status)
        return [{"role": "user", "content": content} for _, content in snapshot]

    def _owned_status(self, task_id: str, owner: str | None) -> SubagentStatus:
        status = self._terminal_statuses.get(task_id)
        record = self._tasks.get(task_id)
        if status is None and record is not None and record.status.finished_at is None:
            status = record.status
        if self.sessions is not None and owner:
            if not self.sessions.exists(owner) or not self.sessions.contains(task_id, owner):
                raise SubagentControlError("task unavailable")
            if status is None:
                status = self.sessions.status(task_id, owner)
        if not owner or status is None or status.owner != owner:
            raise SubagentControlError("task unavailable")
        return status

    def is_execution_live(self, task_id: str, owner: str) -> bool:
        """Owner-scoped execution authority, not a persisted activity claim."""
        status = self._owned_status(task_id, owner)
        task = self._running_tasks.get(task_id)
        return status.state in {"queued", "running"} and task is not None and not task.done()

    def check(self, task_id: str, owner: str | None) -> SubagentStatus:
        """Return detached status for one task owned by the caller's session."""
        return deepcopy(self._owned_status(task_id, owner))

    def accepts_result(self, msg: InboundMessage, owner: str) -> bool:
        """Reject a completion whose owning child was removed after publication."""
        task_id = msg.metadata.get("subagent_task_id")
        if msg.sender_id != "subagent" or not isinstance(task_id, str) or self.sessions is None:
            return True
        return self.sessions.contains(task_id, owner)

    def read_session(self, task_id: str, owner: str) -> tuple[SubagentStatus, Session]:
        """Return task status and detached history through the ownership boundary."""
        status = self.check(task_id, owner)
        child = self.sessions.snapshot(task_id, owner) if self.sessions is not None else None
        if child is None:
            raise SubagentControlError("task unavailable")
        return status, child

    async def send(
        self, task_id: str, owner: str | None, message: str | None, *,
        sender_id: str | None = None, input_id: str | None = None, question_id: str | None = None,
        private_receipt: bool = False,
    ) -> SubagentMessageReceipt:
        """Accept a bounded follow-up only while execution is open."""
        status = self._owned_status(task_id, owner)
        if status.backend == "codex":
            return await self._send_codex(
                status, message, sender_id, input_id, question_id, private_receipt=private_receipt,
            )
        record = self._tasks.get(task_id)
        if record is None or status.state not in {"queued", "running", "waiting_input"}:
            raise SubagentControlError("task is not accepting messages")
        if not message or not message.strip() or len(message.encode("utf-8")) > self.MAX_MESSAGE_BYTES:
            raise SubagentControlError("message must contain text and be at most 8192 UTF-8 bytes")
        if len(record.inbox) >= self.MAX_INBOX or len(status.receipts) >= self.MAX_MESSAGES:
            raise SubagentControlError("task message capacity reached")
        message_id = str(uuid.uuid4())
        record.inbox.append((message_id, message))
        status.receipts[message_id] = "accepted"
        if private_receipt:
            status.private_receipts.add(message_id)
        if record.input_received is not None:
            record.input_received.set()
        await self._save_status(status)
        return {"task_id": task_id, "message_id": message_id,
                "receipt": "accepted", "delivered": False}

    async def cancel(
        self, task_id: str, owner: str | None, *, sender_id: str | None = None,
    ) -> SubagentStatus:
        """Stop an owned task and return its detached state after the bounded wait."""
        status = self._owned_status(task_id, owner)
        record = self._tasks.get(task_id)
        if status.backend == "codex":
            self._check_codex_sender(status, sender_id)
            if record is None and status.state == "waiting_input":
                record = await self._restore_codex(status)
        if record is not None and record.status.finished_at is None:
            await self._cancel_task(record)
            status = record.status
        return deepcopy(status)

    async def _cancel_task(self, record: _SubagentTask,
                           *, suppress_notice: bool = False) -> None:
        record.suppress_notice |= suppress_notice
        task_id = record.status.task_id
        task = self._running_tasks.get(task_id)
        if record.decide(_SubagentOutcome("cancelled", "Task cancelled.", "cancelled")):
            await self._save_status(record.status)
            if task is not None and record.begun and not task.done():
                task.cancel()
        if record.status.finished_at is None:
            self._start_cleanup(record)
            if task is None or task.done():
                task = asyncio.create_task(self._settle_unfinished_task(record))
                self._track_execution(record, task)
        if task is not None:
            # wait_for waits for cancellation acknowledgement and can hang here.
            # Keep resistant work tracked, with its slot held, until it really exits.
            await asyncio.wait({task}, timeout=self.CANCEL_WAIT_SECONDS)

    def runtime_statuses(self) -> Mapping[str, SubagentStatus]:
        """Return detached active and retained terminal data, never task resources."""
        return {
            **{tid: record.snapshot() for tid, record in self._tasks.items()
               if record.status.finished_at is None},
            **deepcopy(self._terminal_statuses),
        }

    def _runtime_statuses_for_session(self, session_key: str) -> dict[str, SubagentStatus]:
        return {
            **{tid: record.snapshot() for tid, record in self._tasks.items()
               if record.status.owner == session_key and record.status.finished_at is None},
            **{tid: deepcopy(status) for tid, status in self._terminal_statuses.items()
               if status.owner == session_key},
        }

    def _merge_session_statuses(
        self, session_key: str, runtime: Mapping[str, SubagentStatus], *, include_history: bool,
    ) -> Mapping[str, SubagentStatus]:
        if self.sessions is not None and not self.sessions.exists(session_key):
            return {}
        statuses = self.sessions.load(session_key) if include_history and self.sessions is not None else {}
        for task_id, status in runtime.items():
            if self.sessions is not None and not self.sessions.contains(task_id, session_key):
                continue
            persisted = statuses.get(task_id)
            if persisted is None or status.revision >= persisted.revision:
                statuses[task_id] = status
        return statuses

    def statuses_for_session(
        self, session_key: str | None, *, include_history: bool = True,
    ) -> Mapping[str, SubagentStatus]:
        """Return only tasks owned by the given session, never a global fallback."""
        if not session_key:
            return {}
        return self._merge_session_statuses(
            session_key, self._runtime_statuses_for_session(session_key), include_history=include_history,
        )

    async def read_statuses_for_session(self, session_key: str) -> Mapping[str, SubagentStatus]:
        """Capture runtime observations on the owner loop; read stored metadata on a worker."""
        runtime = self._runtime_statuses_for_session(session_key)
        return await asyncio.to_thread(
            self._merge_session_statuses, session_key, runtime, include_history=True,
        )

    async def find_tasks(
        self, owner: str, query: str | None = None, *, sender_id: str | None = None,
        backend: SubagentBackend | None = None,
        active_only: bool = False,
    ) -> list[SubagentStatus]:
        tasks = [
            status for status in (await self.read_statuses_for_session(owner)).values()
            if (backend is None or status.backend == backend)
            and (status.backend == "native" or sender_id is None or status.origin_sender_id in (None, sender_id))
            and (not active_only or status.state in {"queued", "running", "waiting_input", "stopping"})
        ]
        tasks.sort(key=lambda status: (status.finished_at is None, status.created_at), reverse=True)
        if not query:
            return tasks if active_only else tasks[:20]
        needle = query.casefold().strip().lstrip("#")
        exact = [status for status in tasks if needle in {status.reference.casefold(), status.task_id.casefold()}]
        return exact or [
            status for status in tasks
            if needle and any(needle in value.casefold() for value in (status.label, status.project or ""))
        ][:20]

    async def resolve_task(
        self, task_id: str, owner: str, *, sender_id: str | None = None,
        backend: SubagentBackend | None = None,
    ) -> SubagentStatus:
        try:
            status = self.check(task_id, owner)
        except SubagentControlError:
            matches = await self.find_tasks(owner, task_id, sender_id=sender_id, backend=backend)
            if len(matches) != 1 or matches[0].reference != task_id.casefold().lstrip("#"):
                raise SubagentControlError("task unavailable") from None
            status = matches[0]
        if backend is not None and status.backend != backend:
            raise SubagentControlError("task unavailable")
        self._check_codex_sender(status, sender_id)
        return status

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        """Update the deprecated runtime source used by legacy ``spawn`` calls."""
        warnings.warn(
            "SubagentManager.set_provider() is deprecated; pass runtime=... to spawn() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        context_window_tokens = (
            self._compat_runtime.context_window_tokens
            if self._compat_runtime is not None
            else AgentDefaults().context_window_tokens
        )
        self._compat_runtime = LLMRuntime.capture(
            provider,
            model,
            context_window_tokens=context_window_tokens,
        )

    def _compat_spawn_runtime(self) -> LLMRuntime:
        runtime = self._compat_runtime
        if runtime is None:
            raise TypeError(
                "SubagentManager.spawn() missing required keyword-only argument: 'runtime'"
            )
        warnings.warn(
            "SubagentManager.spawn() without runtime is deprecated; pass runtime=... explicitly",
            DeprecationWarning,
            stacklevel=3,
        )
        return LLMRuntime.capture(
            runtime.provider,
            runtime.model,
            context_window_tokens=runtime.context_window_tokens,
        )

    def _subagent_tools_config(self) -> ToolsConfig:
        return self.tools_config.model_copy(
            update={"restrict_to_workspace": self.restrict_to_workspace},
        )

    def _build_tools(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
        exec_manager: ExecSessionManager | None = None,
        repository_skill_roots: tuple[Path, ...] = (),
    ) -> ToolRegistry:
        root = self.workspace if workspace is None else workspace
        registry = ToolRegistry()
        cfg = tools_config if tools_config is not None else self._subagent_tools_config()
        if cfg.exec.sandbox and repository_skill_roots:
            exec_config = cfg.exec.model_copy(update={
                "sandbox_ro_binds": [*cfg.exec.sandbox_ro_binds, *(str(path) for path in repository_skill_roots)],
            })
            cfg = cfg.model_copy(update={"exec": exec_config})
        ctx = ToolContext(
            config=cfg,
            network=self.network_config,
            workspace=str(root.resolve()),
            exec_session_manager=exec_manager or self._exec_session_manager,
            file_state_store=FileStates(),
            repository_skill_roots=repository_skill_roots,
            workspace_sandbox=workspace_sandbox_status(
                restrict_to_workspace=cfg.restrict_to_workspace,
                workspace=root,
            ),
        )
        ToolLoader().load(ctx, registry, scope="subagent")
        return registry

    def _load_model_settings(self) -> SubagentToolConfig:
        if self._subagent_config_loader is None:
            return self.tools_config.subagent
        return self._subagent_config_loader()

    def _preset_runtime(self, name: str | None, default: LLMRuntime) -> LLMRuntime:
        if name is None:
            return default
        if self._resolve_model_preset is None:
            raise SubagentControlError("Model presets are unavailable in this subagent runtime")
        return self._resolve_model_preset(name)

    async def _create_task(
        self, task: str, label: str | None, origin_channel: str, origin_chat_id: str,
        session_key: str | None, origin_message_id: str | None,
        temperature: float | None, workspace_scope: WorkspaceScope | None,
        runtime: LLMRuntime | None, *, announce: bool, origin_turn_id: str | None,
        session_policy: SessionPolicy | None,
        project: str | None = None, resume_task_id: str | None = None,
        reply_first: bool = False, worktree: bool | None = None,
        base_ref: str | None = None, adopt_branch: str | None = None,
        backend: SubagentBackend = "native", sender_id: str | None = None,
        origin_route: dict[str, Any] | None = None,
        origin_generation: str | None = None,
        kind: SubagentKind | None = None,
    ) -> _SubagentTask:
        self.bus.admission.check()
        parent_runtime = None
        if backend == "native":
            parent_runtime = runtime or self._compat_spawn_runtime()
            try:
                runtime = self._preset_runtime(self._load_model_settings().native_model_preset, parent_runtime)
            except (KeyError, ValueError) as exc:
                raise SubagentControlError(f"Could not load the native subagent model: {exc}") from exc
            if runtime is parent_runtime:
                parent_runtime = None
        if temperature is not None and runtime is not None:
            runtime = runtime.with_generation_overrides(temperature=temperature)
        owner = session_key or f"{origin_channel}:{origin_chat_id}"
        task_id = str(uuid.uuid4())
        previous = None
        bound_worktree = None
        if resume_task_id is not None:
            previous = self._owned_status(resume_task_id, owner)
            self._check_codex_sender(previous, sender_id)
            if previous.backend != backend:
                raise SubagentControlError(f"resume_task_id must refer to a {backend} task")
            if previous.finished_at is None:
                raise SubagentControlError("resume_task_id requires a finished task")
            if backend == "codex":
                if project is None:
                    project = previous.project
            elif previous.worktree_id is None or project is None:
                raise SubagentControlError("native resume requires a configured project and a prepared worktree")
            if project != previous.project:
                raise SubagentControlError("resume must use the previous task's project")
            if worktree is not None and worktree != (previous.worktree_id is not None):
                raise SubagentControlError("resume must use the previous task's checkout")
            if previous.worktree_id is not None and (previous.worktree is None or previous.branch is None):
                raise SubagentControlError("the previous task did not prepare its project worktree")
            if previous.worktree_id is not None:
                bound_worktree = previous
            history = await self.read_statuses_for_session(owner)
            # An older task ID can point to a checkout adopted by a later execution.
            for status in history.values():
                same_worktree = previous.worktree_id is not None and status.worktree_id == previous.worktree_id
                same_thread = (
                    backend == "codex" and previous.codex_thread is not None
                    and status.codex_thread == previous.codex_thread
                )
                if (same_worktree or same_thread) and status.finished_at is None:
                    raise SubagentControlError("the worktree or Codex thread already has an unfinished task")
                if (same_worktree and status.worktree and status.branch
                        and (bound_worktree is None or status.created_at > bound_worktree.created_at)):
                    bound_worktree = status
        self.bus.admission.check()
        # A failed cleanup owns admission even after its status has been evicted.
        if self._closed or len(self._tasks) + self._admitting >= self.MAX_ACTIVE:
            raise SubagentControlError("subagent manager is closed or at task capacity")
        project_config = None
        worktree_id = previous.worktree_id if previous else None
        codex_thread = previous.codex_thread if previous and backend == "codex" else None
        codex_config = None
        if backend == "codex" and codex_thread is None:
            try:
                codex_config = self._load_model_settings().codex
            except ValueError as exc:
                raise SubagentControlError(f"Could not load the Codex task model: {exc}") from exc
        if base_ref is not None and previous is not None:
            raise SubagentControlError("base_ref applies only to new worktrees")
        if adopt_branch is not None and (not adopt_branch.strip() or worktree_id is None):
            raise SubagentControlError("adopt_branch requires a branch name and a resumed worktree")
        if project is None and (worktree or base_ref is not None):
            raise SubagentControlError("a worktree requires a configured project")
        if project is not None:
            project_config = self.tools_config.subagent.projects.get(project)
            if project_config is None:
                raise SubagentControlError(f"project {project!r} is not configured")
            if self.sessions is None or (session_policy is not None and not session_policy.persist):
                raise SubagentControlError("project tasks require a persistent conversation")
            use_worktree = worktree if worktree is not None else project_config.worktree_script is not None
            if previous is not None:
                use_worktree = worktree_id is not None
            if base_ref is not None and (not base_ref.strip() or not use_worktree):
                raise SubagentControlError("base_ref requires a Git revision and a new worktree")
            if use_worktree:
                if not announce and backend == "native":
                    raise SubagentControlError("worktree tasks require background execution with wait=false")
                if previous is None and project_config.worktree_script is None:
                    raise SubagentControlError("no worktree setup script is configured for this project")
                worktree_id = worktree_id or task_id
            if worktree_id is not None:
                if self._worktree_in_use(worktree_id):
                    raise SubagentControlError("the project worktree already has a task in progress")
                self._admitting_worktrees.add(worktree_id)
        status = SubagentStatus(
            task_id=task_id,
            revision=1,
            label=label or task[:30] + ("..." if len(task) > 30 else ""),
            task_description=task,
            started_at=time.monotonic(),
            owner=owner,
            origin_message_id=origin_message_id,
            origin_turn_id=origin_turn_id,
            project=project,
            worktree_id=worktree_id,
            worktree=bound_worktree.worktree if bound_worktree else None,
            branch=bound_worktree.branch if bound_worktree else None,
            resume_task_id=resume_task_id,
            base_ref=base_ref,
            adopt_branch=adopt_branch,
            reply_first=reply_first,
            backend=backend,
            kind=kind if kind is not None else previous.kind if previous is not None and backend == "native" else "general",
            review_base=previous.review_base if previous is not None and adopt_branch is None else None,
            origin_sender_id=sender_id if backend == "codex" else None,
            origin_generation=origin_generation,
            origin_channel=origin_channel if backend == "codex" else None,
            origin_chat_id=origin_chat_id if backend == "codex" else None,
            origin_route=origin_route or {},
            codex_thread=codex_thread,
        )
        if backend == "codex":
            if previous is not None and codex_thread is not None:
                status.cwd, status.model, status.effort = previous.cwd, previous.model, previous.effort
            else:
                status.cwd = str((workspace_scope.project_path if workspace_scope else self.workspace).resolve())
                assert codex_config is not None
                status.model, status.effort = codex_config.model, codex_config.effort
        self._admitting += 1
        try:
            if self.sessions is not None:
                admission = asyncio.create_task(self.sessions.create(status, policy=session_policy))
                try:
                    child = await shield_and_drain(admission)
                except asyncio.CancelledError:
                    if not admission.cancelled() and admission.exception() is None:
                        child = admission.result()
                        await shield_and_drain(self.sessions.sessions.state.delete(
                            child.key, expected_generation=child.generation,
                        ))
                    raise
            else:
                child = Session(key=SubagentSessions.key(task_id), policy=session_policy or SessionPolicy())
                child.add_message("user", task)
        finally:
            self._admitting -= 1
            if worktree_id is not None:
                self._admitting_worktrees.discard(worktree_id)
        if self._closed:
            if self.sessions is not None:
                await self.sessions.sessions.state.delete(child.key, expected_generation=child.generation)
            raise SubagentControlError("subagent manager is closed")
        record = _SubagentTask(
            status=status,
            session=child,
            runtime=runtime,
            parent_runtime=parent_runtime,
            origin={
                "channel": origin_channel, "chat_id": origin_chat_id,
                "session_key": owner, "sender_id": sender_id,
                "llm_usage_source": current_llm_usage_source(),
            },
            origin_message_id=origin_message_id,
            workspace_scope=workspace_scope,
            project_config=project_config,
            announce=announce,
        )
        self._tasks[task_id] = record
        self._publish_status(status)
        self._start_execution(record)
        logger.info("Started subagent [{}]: {}", task_id,
                    record.status.label if child.policy.log_content else "[content hidden]")
        return record

    def _start_execution(self, record: _SubagentTask) -> None:
        with without_source_capture():
            execution = asyncio.create_task(self._run_subagent(record))
        self._track_execution(record, execution)

    def _track_execution(self, record: _SubagentTask, execution: asyncio.Task[str]) -> None:
        self._running_tasks[record.status.task_id] = execution
        execution.add_done_callback(partial(self._task_done, record))

    async def spawn_codex(
        self, task: str, *, label: str | None, project: str | None, worktree: bool | None,
        request: RequestContext,
        base_ref: str | None = None, resume_task_id: str | None = None, adopt_branch: str | None = None,
    ) -> tuple[str, bool]:
        if not self.tools_config.subagent.codex.enabled:
            raise SubagentControlError("Codex tasks are disabled (tools.subagent.codex.enabled)")
        owner = request.session_key
        if not owner or not request.persist_session or self.sessions is None:
            raise SubagentControlError("Codex tasks require a persistent conversation")
        if not request.sender_id:
            raise SubagentControlError("Codex tasks require an active sender identity")
        input_id = str(request.message_id or request.turn_id or "") or None
        async with self._codex_admission:
            if input_id:
                matches = [
                    status for status in (await self.read_statuses_for_session(owner)).values()
                    if status.backend == "codex" and status.origin_message_id == input_id
                    and status.origin_sender_id == request.sender_id and status.resume_task_id == resume_task_id
                    and (status.finished_at is None or status.state == "done"
                         or (status.base_ref == base_ref and status.adopt_branch == adopt_branch))
                ]
                if matches:
                    status = max(matches, key=lambda candidate: candidate.created_at)
                    return json.dumps(status.as_dict(), ensure_ascii=False), "receipt" in status.notice_ids
            record = await self._create_task(
                task, label, request.channel, request.chat_id, owner, input_id,
                None, request.workspace_scope, None,
                announce=request.can_receive_background_results, origin_turn_id=request.turn_id,
                session_policy=SessionPolicy(persist=True, log_content=request.log_content),
                project=project, worktree=worktree, reply_first=True, backend="codex",
                base_ref=base_ref, resume_task_id=resume_task_id, adopt_branch=adopt_branch,
                sender_id=request.sender_id,
                origin_route=notification_metadata(request.channel, request.metadata),
                origin_generation=request.session_generation,
            )
        if not request.can_receive_background_results:
            status = await self.wait_for_codex_turn(record.status.task_id, owner)
            return json.dumps(status.as_dict(), ensure_ascii=False), False
        elif self.send_task_notice is not None:
            await record.receipt_attempted.wait()
        return json.dumps(record.status.as_dict(), ensure_ascii=False), "receipt" in record.status.notice_ids

    async def wait_for_codex_turn(self, task_id: str, owner: str) -> SubagentStatus:
        """Keep a standalone caller alive through the next Codex result or question."""
        status = self._owned_status(task_id, owner)
        if status.backend != "codex":
            raise SubagentControlError("task is not a Codex task")
        execution = self._running_tasks.get(task_id)
        if execution is not None:
            try:
                await asyncio.shield(execution)
            except asyncio.CancelledError:
                caller = asyncio.current_task()
                if not execution.cancelled() or (caller is not None and caller.cancelling()):
                    raise
        return self.check(task_id, owner)

    @staticmethod
    def _check_codex_sender(status: SubagentStatus, sender_id: str | None) -> None:
        if sender_id is not None and status.origin_sender_id not in (None, sender_id):
            raise SubagentControlError("task unavailable")

    async def _restore_codex(self, status: SubagentStatus) -> _SubagentTask:
        if self.sessions is None or not status.origin_channel or not status.origin_chat_id:
            raise SubagentControlError("task unavailable")
        child = await self.sessions.sessions.state.read(self.sessions.key(status.task_id))
        record = self._tasks.get(status.task_id)
        if record is not None:
            return record
        if child is None or self._closed or not self.sessions.contains(status.task_id, status.owner):
            raise SubagentControlError("task unavailable")
        record = _SubagentTask(
            status=status, session=child, runtime=None,
            origin={"channel": status.origin_channel, "chat_id": status.origin_chat_id,
                    "session_key": status.owner},
            origin_message_id=status.origin_message_id,
            project_config=self.tools_config.subagent.projects.get(status.project or ""),
        )
        self._tasks[status.task_id] = record
        return record

    async def _send_codex(
        self, status: SubagentStatus, message: str | None, sender_id: str | None,
        input_id: str | None, question_id: str | None, *, private_receipt: bool,
    ) -> SubagentMessageReceipt:
        self._check_codex_sender(status, sender_id)
        key = input_id or str(uuid.uuid4())
        receipt: SubagentMessageReceipt = {
            "task_id": status.task_id, "message_id": key, "receipt": "accepted", "delivered": False,
        }
        if key in status.receipts:
            return receipt
        if status.state not in {"queued", "running", "waiting_input"}:
            raise SubagentControlError("task is not accepting messages")
        if not message or not message.strip() or len(message.encode("utf-8")) > self.MAX_MESSAGE_BYTES:
            raise SubagentControlError("message must contain text and be at most 8192 UTF-8 bytes")
        self.bus.admission.check()
        record = self._tasks.get(status.task_id)
        if record is None:
            record = await self._restore_codex(status)
        prior = self._running_tasks.get(status.task_id)
        if status.state == "waiting_input" and prior is not None and not prior.done():
            await asyncio.shield(prior)
        async with record.control_lock:
            status = record.status
            if key in status.receipts:
                return receipt
            if (status.state not in {"queued", "running", "waiting_input"}
                    or (question_id is not None and
                        (status.state != "waiting_input" or status.question_id != question_id))):
                raise SubagentControlError("task or question is no longer accepting this reply")
            if (len(status.pending_inputs) >= self.MAX_INBOX or len(status.receipts) >= self.MAX_MESSAGES):
                raise SubagentControlError("task message capacity reached")
            answers_question = status.state == "waiting_input" and question_id is not None
            if answers_question and not self.tools_config.subagent.codex.enabled:
                raise SubagentControlError("Codex tasks are disabled")
            status.pending_inputs.append((key, message.strip()))
            status.receipts[key] = "accepted"
            if private_receipt:
                status.private_receipts.add(key)
            if answers_question:
                status.state = status.phase = "queued"
            await self._save_status(status)
            if answers_question:
                self._start_execution(record)
            elif record.codex is not None and record.codex.turn_id is not None:
                await self._steer_codex(record)
        return receipt

    async def _steer_codex(self, record: _SubagentTask) -> None:
        assert record.codex is not None
        status = record.status
        while status.pending_inputs and record.outcome is None:
            message_id, message = status.pending_inputs[0]
            try:
                await record.codex.steer(message, message_id)
            except (CodexTaskError, OSError, TimeoutError):
                status.receipts[message_id] = "undelivered"
            else:
                status.receipts[message_id] = "delivered"
            status.pending_inputs.pop(0)
            await self._save_status(status)

    async def _task_notice(self, record: _SubagentTask, kind: str, text: str) -> bool:
        status = record.status
        if not record.announce or self.send_task_notice is None or record.suppress_notice:
            return False
        if kind in status.notice_ids:
            return True
        if self.sessions is None or not self.sessions.contains(status.task_id, status.owner):
            return False
        if kind == "receipt":
            content = (
                f"✅ Codex · {task_display_text(status.label)} · {text}\n\n"
                f"{task_query_block(status.reference)}"
            )
        elif status.backend == "codex":
            content = f"Codex · {status.label} · {status.reference}\n{text}"
        else:
            content = (
                f"【子任务｜待回答】Subagent [{status.reference}] · {task_display_text(status.label)}\n\n"
                f"{task_display_text(text)}\n\n"
                f"回复用法  /task {status.reference} send 你的回答"
            )
        try:
            async with asyncio.timeout(12):
                message_id = await self.send_task_notice(OutboundMessage(
                    channel=record.origin["channel"], chat_id=record.origin["chat_id"], content=content,
                    metadata={**status.origin_route, "message_id": status.origin_message_id,
                              "_session_key": status.owner, "_session_generation": status.origin_generation,
                              **({"render_as": "text"} if kind != "receipt" else {}),
                              "task_notice_id": f"{status.task_id}:{kind}"},
                ))
        except Exception as exc:
            logger.warning("Task notice {} could not be delivered: {}", kind, type(exc).__name__)
            return False
        status.notice_ids[kind] = message_id or ""
        await self._save_status(status)
        self._refresh_terminal_snapshot(record)
        return True

    def _refresh_terminal_snapshot(self, record: _SubagentTask) -> None:
        status = record.status
        if status.task_id in self._terminal_statuses:
            self._terminal_statuses[status.task_id] = record.snapshot()

    async def route_task_reply(self, msg: InboundMessage) -> OutboundMessage | None:
        parent_id = msg.metadata.get("task_reply_to")
        if not msg.is_user_input or not isinstance(parent_id, str) or not parent_id:
            return None
        owner = msg.session_key
        for status in (await self.read_statuses_for_session(owner)).values():
            if status.backend != "codex" or status.origin_channel != msg.channel or status.origin_chat_id != msg.chat_id:
                continue
            key = next((key for key, val in status.notice_ids.items() if val == parent_id), None)
            if key is None or key == "terminal":
                continue
            text = msg.metadata.get("task_reply_text", msg.content)
            if not isinstance(text, str) or not text.strip():
                return None
            input_id = msg.metadata.get("message_id")
            try:
                if text.strip().rstrip("。.!！").casefold() in {
                    "stop", "stop this task", "cancel", "cancel this task", "停掉这单", "停止这单", "取消这单",
                }:
                    result = await self.cancel(status.task_id, owner, sender_id=msg.sender_id)
                    content = f"Codex · {status.reference} · {result.state}"
                else:
                    await self.send(
                        status.task_id, owner, text, sender_id=msg.sender_id,
                        input_id=input_id if isinstance(input_id, str) else None,
                        question_id=None if key == "receipt" else key,
                    )
                    content = f"Codex · {status.reference} · Input accepted"
            except SubagentControlError as exc:
                content = f"Codex · {exc}"
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=msg.metadata,
            )
        return None

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        *,
        runtime: LLMRuntime | None = None,
        origin_turn_id: str | None = None,
        session_policy: SessionPolicy | None = None,
        project: str | None = None,
        resume_task_id: str | None = None,
        reply_first: bool = False,
        worktree: bool | None = None,
        origin_route: dict[str, Any] | None = None,
        origin_generation: str | None = None,
        sender_id: str | None = None,
        base_ref: str | None = None,
        adopt_branch: str | None = None,
        kind: SubagentKind | None = None,
    ) -> str:
        """Start background work and route its terminal result to the parent."""
        try:
            record = await self._create_task(
                task, label, origin_channel, origin_chat_id, session_key,
                origin_message_id, temperature, workspace_scope, runtime, announce=True, origin_turn_id=origin_turn_id,
                session_policy=session_policy, project=project, resume_task_id=resume_task_id,
                reply_first=reply_first, worktree=worktree, origin_route=origin_route, sender_id=sender_id,
                origin_generation=origin_generation, base_ref=base_ref, adopt_branch=adopt_branch, kind=kind,
            )
        except SubagentControlError as exc:
            return ToolResult.error(f"Error: {exc}")
        status = record.status
        if status.reply_first:
            return (
                f"Subagent [{status.label}] accepted for background execution (id: {status.task_id}). "
                "Reply now with the verified findings, follow-up status, and expected output. "
                "The task result will arrive in a separate turn. Don't duplicate its work."
            )
        if status.worktree_id is not None:
            return (f"Subagent [{status.label}] queued (id: {status.task_id}). "
                    "The worktree will be prepared when it runs. I'll notify you when it completes.")
        return (f"Subagent [{status.label}] started (id: {status.task_id}). "
                "I'll notify you when it completes. Don't duplicate its work.")

    async def run_inline(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        *,
        runtime: LLMRuntime | None = None,
        origin_turn_id: str | None = None,
        session_policy: SessionPolicy | None = None,
        project: str | None = None,
        worktree: bool | None = None,
        resume_task_id: str | None = None,
        base_ref: str | None = None,
        adopt_branch: str | None = None,
        kind: SubagentKind | None = None,
    ) -> str:
        """Wait for the same task lifecycle without a background notice."""
        try:
            record = await self._create_task(
                task, label, origin_channel, origin_chat_id, session_key,
                origin_message_id, temperature, workspace_scope, runtime, announce=False, origin_turn_id=origin_turn_id,
                session_policy=session_policy, project=project, worktree=worktree, resume_task_id=resume_task_id,
                base_ref=base_ref, adopt_branch=adopt_branch, kind=kind,
            )
        except SubagentControlError as exc:
            return ToolResult.error(f"Error: {exc}")
        execution = self._running_tasks[record.status.task_id]
        try:
            result = await asyncio.shield(execution)
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if execution.cancelled() and caller is not None and not caller.cancelling():
                partial_result = record.status.result if record.status.partial else None
                return ToolResult.error(
                    "Task cancelled." + (f"\nPartial result:\n{partial_result}" if partial_result else "")
                )
            await self._cancel_task(record, suppress_notice=True)
            raise
        if record.status.state == "error":
            error = record.status.error or result
            return ToolResult.error(error + (f"\nPartial result:\n{result}" if error != result else ""))
        if record.status.state == "incomplete":
            return ToolResult.error(f"Task incomplete ({record.status.stop_reason}).\n{result}")
        if record.status.review is not None and record.status.review.verdict == "skipped":
            return f"{result}\n\n{record.status.review.summary}"
        return result

    def _task_done(self, record: _SubagentTask, task: asyncio.Task[str]) -> None:
        record.receipt_attempted.set()
        if self._running_tasks.get(record.status.task_id) is not task:
            return
        self._running_tasks.pop(record.status.task_id)
        if record.status.backend == "codex" and record.status.state == "waiting_input":
            return
        error = None if task.cancelled() else task.exception()
        if error is not None:
            logger.error("Subagent [{}] failed: {}", record.status.task_id,
                         error if record.session.policy.log_content else type(error).__name__)
        if record.status.finished_at is None:
            # An externally cancelled execution still owns cleanup and a terminal result.
            record.decide(
                _SubagentOutcome("error", f"Error: {error}", "error", str(error))
                if error is not None
                else _SubagentOutcome("cancelled", "Task cancelled.", "cancelled")
            )
            self._start_cleanup(record)
            settlement = asyncio.create_task(self._settle_unfinished_task(record))
            self._running_tasks[record.status.task_id] = settlement
            settlement.add_done_callback(partial(self._task_done, record))
        self._release_task(record)
        if record.status.state == "done" and record.status.worktree is not None:
            self._worktree_sweep_needed.set()

    async def _save_transcript(self, record: _SubagentTask) -> None:
        if self.sessions is not None:
            from nanobot.session.sqlite_store import SessionConflictError

            try:
                await self.sessions.sessions.state.finish_turn(record.session)
            except SessionConflictError:
                # A removed child must not be recreated by a late observation.
                return

    async def _settle_unfinished_task(self, record: _SubagentTask) -> str:
        outcome = await self._finish(record)
        if record.status.backend == "codex" and not record.suppress_notice and not self._closed:
            await self._task_notice(record, "terminal", f"{record.status.state.capitalize()} · {outcome.result}")
        return outcome.result

    async def _run_subagent(self, record: _SubagentTask) -> str:
        status = record.status
        task_text, label = status.task_description, status.label
        record.begun = True
        try:
            record.raise_if_stopping()
            if status.backend == "codex" and status.codex_turn is None and "receipt" not in status.notice_ids:
                try:
                    await self._task_notice(record, "receipt", "Accepted by Codex")
                finally:
                    record.receipt_attempted.set()
            async with self._run_slots:
                record.raise_if_stopping()
                status.state = "running"
                status.phase = "preparing_worktree" if status.worktree_id is not None else "initializing"
                await self._save_status(status)
                record.raise_if_stopping()
                if status.backend == "codex" and status.project:
                    record.project_config = self.tools_config.subagent.projects.get(status.project)
                    if record.project_config is None:
                        raise SubagentControlError(f"project {status.project!r} is not configured")
                if record.project_config is not None:
                    if status.worktree_id is not None:
                        saved = None
                        if status.worktree is not None and status.branch is not None:
                            saved = ProjectWorktree(Path(status.worktree), status.branch)
                        # Reopening and adopting share the checkout with the retention scan.
                        async with self._worktree_lock if saved is not None else nullcontext():
                            worktree = await prepare_project_worktree(
                                record.project_config, self.workspace, status.worktree_id,
                                resume=saved, base_ref=status.base_ref,
                                adopt_branch=status.adopt_branch if status.adopt_branch != status.branch else None,
                            )
                        record.raise_if_stopping()
                        project_root = worktree.path
                        status.worktree = str(worktree.path)
                        status.branch = worktree.branch
                        status.phase = "worktree_ready"
                    else:
                        project_root = record.project_config.repository.expanduser().resolve(strict=True)
                        if not project_root.is_dir():
                            raise RuntimeError("Configured project repository is not a directory")
                    parent_scope = record.workspace_scope
                    record.workspace_scope = build_workspace_scope(
                        project_root,
                        parent_scope.access_mode if parent_scope else default_access_mode(self.restrict_to_workspace),
                        source_channel=parent_scope.source_channel if parent_scope else None,
                    )
                    if status.worktree_id is not None:
                        await self._save_status(status)
                        record.raise_if_stopping()
                outcome = (await self._run_codex(record) if status.backend == "codex"
                           else await self._run_native(record))
                if outcome is not None:
                    record.decide(outcome)
            if outcome is None:
                assert status.question_id and status.question
                await self._task_notice(record, status.question_id, status.question)
                return status.result or ""
        except asyncio.CancelledError:
            record.decide(_SubagentOutcome("cancelled", "Task cancelled.", "cancelled"))
        except Exception as exc:
            logger.opt(exception=record.session.policy.log_content).error("Subagent [{}] failed", status.task_id)
            record.decide(_SubagentOutcome("error", f"Error: {exc}", "error", str(exc)))
        cleanup_error = None
        try:
            await asyncio.shield(self._start_cleanup(record))
        except Exception as exc:
            logger.opt(exception=record.session.policy.log_content).error(
                "Subagent [{}] exec cleanup failed", status.task_id,
            )
            cleanup_error = f"Error cleaning up task processes: {exc}"
        outcome = await self._finish(record, cleanup_error)
        if status.backend == "codex" and not record.suppress_notice and not self._closed:
            await self._task_notice(record, "terminal", f"{status.state.capitalize()} · {outcome.result}")
        elif record.announce and not record.suppress_notice and not self._closed:
            await self._announce_result(
                status.task_id, label, task_text, outcome.result, record.origin,
                "ok" if outcome.state == "done" else outcome.state, record.origin_message_id,
                receipts=status.parent_receipts(),
                stop_reason=status.stop_reason, error=status.error, partial_result=status.partial,
                project_status=status if status.project is not None else None,
                reply_first=status.reply_first,
                review=status.review,
            )
        if outcome.state == "cancelled":
            raise asyncio.CancelledError
        return outcome.result

    async def _run_codex(self, record: _SubagentTask) -> _SubagentOutcome | None:
        status = record.status
        config = self.tools_config.subagent.codex
        if not config.enabled:
            raise SubagentControlError("Codex tasks are disabled")
        configured_root = record.workspace_scope.project_path if record.workspace_scope else Path(status.cwd or self.workspace)
        cwd = configured_root.resolve(strict=True)
        if not cwd.is_dir() or (status.codex_thread is not None and str(cwd) != status.cwd):
            raise SubagentControlError("Codex working directory is unavailable")
        resuming = status.codex_thread is not None
        status.cwd = str(cwd)
        async with CodexTurn(config.model_copy(update={"model": status.model, "effort": status.effort}), cwd) as codex:
            record.codex = codex
            try:
                status.codex_thread = await codex.open_thread(status.codex_thread)
                status.model, status.effort = codex.model, codex.effort
                await self._save_status(status)
                async with record.control_lock:
                    record.raise_if_stopping()
                    inputs = list(status.pending_inputs)
                    task_text = status.task_description
                    if resuming:
                        task_text = f"Continue the same task.\n\n{task_text}"
                        if status.question is not None:
                            task_text += f"\n\nThe user's answer to your question {status.question!r} follows."
                    if inputs:
                        task_text += "\n\nUser follow-up:\n" + "\n".join(message for _, message in inputs)
                    task_text += (
                        "\n\nTreat user follow-ups as changes to the assigned task. Apply the requested changes and "
                        "continue with the remaining deliverables and acceptance criteria. Stop or replace the "
                        "assignment if the user explicitly requests it, within the task's permissions."
                        "\n\nReturn a structured result with state, summary and question. "
                        "Use needs_input and ask one concrete question if a user decision blocks the task. "
                        "Use done only after checking the requested deliverables. "
                        "Include verified tests, external instance IDs and artifact references in the summary. "
                        "Report confirmed milestones and external instance IDs in brief commentary as you work."
                    )
                    status.codex_turn = await codex.start(task_text)
                    status.phase = "executing"
                    status.question = None
                    status.pending_inputs.clear()
                    for message_id, message in inputs:
                        status.receipts[message_id] = "delivered"
                        record.session.add_message("user", message)
                    await self._save_status(status)
                outcome, summary, question = await codex.result(partial(self._record_codex_item, record))
            finally:
                record.codex = None
        record.raise_if_stopping()
        summary = summary[:self.max_tool_result_chars]
        if summary:
            record.session.add_message("assistant", summary)
        if outcome == "needs_input" and question:
            status.question = question[:2000]
            status.question_id = str(uuid.uuid4())
            status.result = summary
            status.state = status.phase = "waiting_input"
            record.session.add_message("assistant", status.question)
            await self._save_transcript(record)
            await self._save_status(status)
            return None
        if outcome == "done":
            return _SubagentOutcome("done", summary, "completed")
        return _SubagentOutcome("incomplete", summary, "codex_incomplete")

    async def _record_codex_item(self, record: _SubagentTask, method: str, item: dict[str, Any]) -> None:
        status = record.status
        kind = item.get("type")
        if kind == "agentMessage" and method == "item/completed" and item.get("phase") == "commentary":
            if isinstance(text := item.get("text"), str) and text.strip():
                summary = text.strip()[:800]
                item_id = item.get("id")
                key = f"{status.codex_turn}:{item_id}" if isinstance(item_id, str) and item_id else summary
                if status.record_progress(key, f"Codex reported: {summary}"):
                    await self._save_status(status)
            return
        if kind not in {"commandExecution", "fileChange", "mcpToolCall", "webSearch"}:
            return
        if method == "item/started":
            progress = f"{kind} in progress"
        else:
            exit_code = item.get("exitCode")
            progress = f"{kind} completed"
            if isinstance(exit_code, int):
                progress += f" (exit {exit_code})"
            elif item.get("status") == "failed":
                progress = f"{kind} failed"
            status.tool_events = [*status.tool_events[-15:], {
                "name": kind, "status": "error" if item.get("status") == "failed" else "ok",
            }]
        if progress != status.progress and not (status.progress or "").startswith("Codex reported:"):
            status.progress, status.progress_at = progress, time.time()
            await self._save_status(status)

    async def _report_progress(
        self, record: _SubagentTask, milestone: str, summary: str, detail: str = "", next_step: str = "",
    ) -> str:
        record.raise_if_stopping()
        if milestone in record.reported_milestones:
            return "Milestone already recorded."
        record.reported_milestones.add(milestone)
        status = record.status
        status.record_progress(milestone, summary, detail=detail, next_step=next_step)
        await self._save_status(status)
        return "Milestone recorded."

    async def _request_native_input(self, record: _SubagentTask, question: str) -> str:
        record.raise_if_stopping()
        if record.inbox:
            return "A user follow-up is already queued. Read it before asking for input."
        status = record.status
        input_received = record.input_received = asyncio.Event()
        question_id = str(uuid.uuid4())
        status.question_id, status.question = question_id, question
        status.state = status.phase = "waiting_input"
        try:
            await self._save_status(status)
            record.raise_if_stopping()
            if not await self._task_notice(record, question_id, question) and not input_received.is_set():
                return ToolResult.error("The question could not be delivered. Include it in your task result.")
            await input_received.wait()
            return "Input received. Read the user's follow-up before continuing."
        finally:
            record.input_received = None
            status.question_id = status.question = None
            if record.outcome is None:
                status.state = "running"
                status.phase = "awaiting_tools"
                await self._save_status(status)

    async def _run_native(self, record: _SubagentTask) -> _SubagentOutcome:
        status = record.status
        if status.kind != "code_change":
            return await self._run_admitted_subagent(record)
        if not self.tools_config.subagent.native_review.enabled or status.worktree is None:
            reason = ("Automatic review is disabled." if status.worktree is not None
                      else "Automatic review requires a managed project worktree.")
            status.review = SubagentReview("skipped", reason)
            return await self._run_admitted_subagent(record)

        review_round = 0
        review_runtime = record.parent_runtime or record.runtime
        assert review_runtime is not None
        candidate_id = None
        outcome = None
        try:
            try:
                worktree = await ReviewWorktree.open(Path(status.worktree), status.review_base)
            except (OSError, TimeoutError, ReviewUnavailableError) as exc:
                outcome = await self._run_admitted_subagent(record)
                if outcome.state != "done":
                    return outcome
                raise ReviewUnavailableError(f"Could not establish the review baseline: {exc}") from exc
            status.review_base = worktree.base
            await self._save_status(status)
            record.exec_manager.capture_version = worktree.command_version
            outcome = await self._run_admitted_subagent(record)
            total_usage = status.usage
            for attempt in range(MAX_REVISIONS + 1):
                if outcome.state != "done":
                    return outcome
                record.raise_if_stopping()
                if record.exec_manager.has_pending_work:
                    feedback = "A command is still running. Collect its result or stop it before handing off."
                elif record.inbox:
                    feedback = "Apply the latest user instructions before handing the result to review."
                else:
                    candidate = await worktree.snapshot()
                    candidate_id = candidate.fingerprint
                    status.phase = "reviewing"
                    await self._save_status(status)
                    record.raise_if_stopping()
                    if review_round == 0:
                        try:
                            review_runtime = self._preset_runtime(
                                self._load_model_settings().native_review.model_preset, review_runtime,
                            )
                        except (KeyError, ValueError) as exc:
                            raise ReviewUnavailableError(f"Could not load the review model: {exc}") from exc
                    review_round += 1
                    verdict, usage = await self._review_native_candidate(record, worktree, review_runtime, review_input(
                        status.task_description,
                        [message["content"] for message in record.session.messages
                         if message.get("role") == "user" and HIDDEN_HISTORY_META not in message
                         and isinstance(message.get("content"), str) and message["content"] != status.task_description],
                        outcome.result, candidate, record.exec_manager.evidence,
                    ))
                    total_usage = self._sum_usage(total_usage, usage)
                    status.usage = total_usage
                    if ((await worktree.snapshot()).fingerprint != candidate_id
                            or record.inbox or record.exec_manager.has_pending_work):
                        feedback = "The worktree or user requirements changed during review. Verify the current candidate."
                    elif verdict.verdict == "pass":
                        status.review = SubagentReview("pass", verdict.summary, review_round, candidate_id)
                        return outcome
                    elif verdict.verdict == "inconclusive":
                        raise ReviewUnavailableError(verdict.summary)
                    else:
                        feedback = "\n".join(
                            f"{item.path}{f':{item.line}' if item.line is not None else ''} · {item.requirement}\n"
                            f"Trigger: {item.trigger}\nRequested change: {item.change}"
                            for item in verdict.findings
                        )
                        status.review = SubagentReview("changes_requested", feedback, review_round, candidate_id)
                if attempt == MAX_REVISIONS:
                    raise ReviewUnavailableError(f"Automatic review reached its revision limit. {feedback}")
                status.phase = "revising"
                await self._save_status(status)
                record.raise_if_stopping()
                outcome = await self._run_admitted_subagent(record, review_feedback=feedback)
                total_usage = self._sum_usage(total_usage, status.usage)
                status.usage = total_usage
        except ReviewUnavailableError as exc:
            summary = str(exc)[:self.max_tool_result_chars]
            status.review = SubagentReview("inconclusive", summary, review_round, candidate_id)
            return _SubagentOutcome("incomplete", outcome.result if outcome else summary, "review_incomplete", summary)
        raise AssertionError("Review loop must settle the native task")

    @staticmethod
    def _sum_usage(left: LLMUsage | None, right: LLMUsage | None) -> LLMUsage | None:
        return left + right if left is not None and right is not None else left or right

    async def _review_native_candidate(
        self, record: _SubagentTask, worktree: ReviewWorktree, runtime: LLMRuntime, evidence: str,
    ) -> tuple[ReviewVerdict, LLMUsage | None]:
        tools = ToolRegistry()
        tools.register(ReviewReadTool(worktree.root))
        definitions = tools.get_definitions()
        result = await self.runner.run(AgentRunSpec(
            initial_messages=[
                {"role": "system", "content": REVIEW_INSTRUCTIONS},
                {"role": "user", "content": evidence},
            ],
            tools=tools, runtime=runtime, max_iterations=6,
            max_tool_result_chars=self.max_tool_result_chars,
            finalize_on_max_iterations=False, error_message=None,
            consolidate_history=partial(
                self.consolidator.summarize_transcript, runtime=runtime,
                session_key=record.session.key, tools=definitions, persist=False,
            ),
            consolidate_provider_compaction=partial(
                self.consolidator.summarize_provider_compaction, runtime=runtime,
                session_key=record.session.key, tools=definitions, persist=False,
            ),
            llm_usage_source=record.origin.get("llm_usage_source"),
        ))
        record.raise_if_stopping()
        if result.stop_reason != "completed" or not result.final_content:
            raise ReviewUnavailableError("The reviewer ended without completing the review.")
        return ReviewVerdict.parse(result.final_content), result.usage

    async def _run_admitted_subagent(
        self, record: _SubagentTask, *, review_feedback: str | None = None,
    ) -> _SubagentOutcome:
        status, origin, runtime = record.status, record.origin, record.runtime
        assert runtime is not None
        task_id, label = status.task_id, status.label
        origin_message_id, workspace_scope = record.origin_message_id, record.workspace_scope
        logger.info("Subagent [{}] starting task: {}", task_id,
                    label if record.session.policy.log_content else "[content hidden]")

        async def _on_checkpoint(payload: dict[str, Any]) -> None:
            record.raise_if_stopping()
            phase = payload.get("phase", status.phase)
            iteration = payload.get("iteration", status.iteration)
            if (phase, iteration) != (status.phase, status.iteration):
                status.phase, status.iteration = phase, iteration
                await self._save_status(status)

        root = workspace_scope.project_path if workspace_scope is not None else self.workspace
        cfg = None
        if workspace_scope is not None:
            cfg = self._subagent_tools_config()
            cfg.restrict_to_workspace = workspace_scope.restrict_to_workspace
        project_skill_roots: tuple[Path, ...] = ()
        repository_skill_roots: tuple[Path, ...] = ()
        if record.project_config is not None:
            checkout_skills = project_skill_root(root)
            repository_skills = project_skill_root(record.project_config.repository)
            project_skill_roots = tuple(dict.fromkeys(
                path for path in (checkout_skills, repository_skills) if path is not None
            ))
            if repository_skills is not None and repository_skills != checkout_skills:
                repository_skill_roots = (repository_skills,)
        # Construct from the agent workspace; the bound scope below supplies the project cwd.
        tools = self._build_tools(
            tools_config=cfg, exec_manager=record.exec_manager, repository_skill_roots=repository_skill_roots,
        )
        native_commentary = runtime.provider.supports_commentary(
            runtime.model, runtime.generation.reasoning_effort,
        ) is True
        if not native_commentary:
            tools.register(_ReportProgressTool(partial(self._report_progress, record)))
        if record.announce and self.send_task_notice is not None and self.sessions is not None:
            tools.register(_RequestInputTool(partial(self._request_native_input, record)))
        observe_model_request = None
        trace_enabled = self.sessions is not None and record.session.policy.persist and record.session.policy.log_content
        with capture_sources() if trace_enabled else nullcontext() as sources:
            system_prompt = self._build_subagent_prompt(
                workspace=root, project=status.project is not None, worktree=status.worktree is not None,
                project_skill_roots=project_skill_roots, native_commentary=native_commentary,
            )
        if self.sessions is not None and sources is not None:
            trace = SubagentRequestTrace(self.sessions.sessions.state, record.session, sources.sources)
            observe_model_request = trace.record
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *record.session.get_history(),
        ]
        if review_feedback is not None:
            messages.append({
                "role": "user",
                "content": "Continue under the original task scope and permissions. Verify this internal review feedback "
                           "against the task, address any remaining issues, and run the relevant checks.\n\n"
                           + review_feedback,
                HIDDEN_HISTORY_META: {"kind": "internal_continuation"},
            })
        activity = SessionActivity()

        async def save_observation(status: SubagentStatus) -> None:
            record.session.messages = activity.transcript()
            await self._save_transcript(record)
            await self._save_status(status)

        async def publish_activity(event: AgentEvent) -> None:
            if activity.remember(event):
                await save_observation(status)

        events = EventSink(publish_activity)
        hook = build_agent_turn_hook(AgentTurnHookSpec(
            events=events,
            streaming=True,
            channel=origin["channel"],
            chat_id=origin["chat_id"],
            message_id=origin_message_id,
            session_key=record.session.key,
            workspace=root,
            registered_hook_factories=[create_file_edit_activity_hook],
            turn_hooks=[activity, _SubagentHook(
                status, check_active=record.raise_if_stopping,
                native_commentary=native_commentary,
                max_result_chars=self.max_tool_result_chars,
                on_status=save_observation,
            )],
            ephemeral=not record.session.policy.persist,
            run_extra_hooks_for_ephemeral=True,
            log_content=record.session.policy.log_content,
        ))

        request_token = bind_request_context(RequestContext(
            channel=origin["channel"],
            chat_id=origin["chat_id"],
            message_id=origin_message_id,
            session_key=record.session.key,
            runtime=runtime,
            workspace=root,
            workspace_scope=workspace_scope,
            log_content=record.session.policy.log_content,
            persist_session=record.session.policy.persist,
        ))
        token = bind_workspace_scope(workspace_scope) if workspace_scope is not None else None
        try:
            tool_definitions = tools.get_definitions()
            consolidate_history = partial(
                self.consolidator.summarize_transcript,
                runtime=runtime,
                session_key=record.session.key,
                tools=tool_definitions,
                persist=False,
            )
            consolidate_provider_compaction = partial(
                self.consolidator.summarize_provider_compaction,
                runtime=runtime,
                session_key=record.session.key,
                tools=tool_definitions,
                persist=False,
            )
            result = await self.runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=tools,
                runtime=runtime,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=hook,
                max_iterations_message="Task stopped at its iteration limit before producing a final response.",
                finalize_on_max_iterations=False,
                error_message=None,
                checkpoint_callback=_on_checkpoint,
                injection_callback=partial(self._drain_inbox, record),
                session_key=record.session.key,
                workspace=root if record.session.policy.persist else None,
                llm_usage_source=origin.get(
                    "llm_usage_source",
                    current_llm_usage_source(),
                ),
                consolidate_history=consolidate_history,
                consolidate_provider_compaction=consolidate_provider_compaction,
                events=events,
                observe_model_request=observe_model_request,
                capture_tool_commentary=native_commentary,
            ))
        finally:
            if token is not None:
                reset_workspace_scope(token)
            reset_request_context(request_token)
        status.usage = result.usage
        if result.messages:
            record.session.messages = activity.transcript(result.messages)
        record.session.provider_state = result.provider_state
        checkpoint = result.summary_checkpoint
        if checkpoint is not None:
            boundary = checkpoint.transcript_boundary
            if 0 <= boundary <= len(result.messages):
                insert_at = sum(message.get("role") != "system" for message in result.messages[:boundary])
                record.session.commit_summary_checkpoint(checkpoint.summary, insert_at=insert_at)
        if result.stop_reason in {"error", "empty_final_response"}:
            final_result = result.error or "Error: subagent execution failed."
            return _SubagentOutcome("error", final_result, result.stop_reason, result.error)
        if result.stop_reason == "max_iterations":
            return _SubagentOutcome("incomplete", result.final_content or "Iteration limit reached.", result.stop_reason)
        final_result = result.final_content or "Task completed but no final response was generated."
        return _SubagentOutcome("done", final_result, result.stop_reason, result.error)

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: _SubagentOrigin,
        status: str,
        origin_message_id: str | None = None,
        *,
        receipts: dict[str, str] | None = None,
        stop_reason: str | None = None,
        error: str | None = None,
        partial_result: bool = False,
        project_status: SubagentStatus | None = None,
        reply_first: bool = False,
        review: SubagentReview | None = None,
    ) -> None:
        status_text = {
            "ok": "completed successfully", "cancelled": "was cancelled",
            "incomplete": "stopped before completing the task",
        }.get(status, "failed")

        announce_content = render_template(
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            task=task,
            result=result,
            stop_reason=stop_reason,
            error=error,
            partial_result=partial_result,
            reply_first=reply_first,
            project=project_status.project if project_status else None,
            worktree=project_status.worktree if project_status else None,
            branch=project_status.branch if project_status else None,
            review=review,
        )

        # Inject as system message to trigger main agent.
        # Use session_key_override to align with the main agent's effective
        # session key (including any explicit override) so the result is
        # routed to the correct pending queue (mid-turn injection) instead of
        # being dispatched as a competing independent task.
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        if self.sessions is not None and (
            not self.sessions.exists(override) or not self.sessions.contains(task_id, override)
        ):
            return
        metadata: dict[str, Any] = {
            **(project_status.origin_route if project_status is not None else {}),
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
            "subagent_state": "done" if status == "ok" else status,
            "subagent_stop_reason": stop_reason,
            "subagent_partial": partial_result,
        }
        metadata["subagent_message_receipts"] = receipts or {}
        if reply_first:
            metadata[SUBAGENT_REPLY_FIRST_META] = True
        if receipts:
            announce_content += "\nMessage receipts: " + json.dumps(receipts)
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id
        if sender_id := origin.get("sender_id"):
            metadata[SUBAGENT_ORIGIN_SENDER_META] = sender_id
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
            require_existing_session=True,
            metadata=metadata,
        )

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    def _build_subagent_prompt(
        self,
        workspace: Path | None = None,
        *,
        project: bool = False,
        worktree: bool = False,
        project_skill_roots: tuple[Path, ...] = (),
        native_commentary: bool = False,
    ) -> str:
        agent_workspace = self.workspace.expanduser().resolve()
        project_workspace = workspace.expanduser().resolve() if workspace else agent_workspace
        skills_summary = SkillsLoader(
            self.workspace,
            disabled_skills=self.disabled_skills,
            project_skill_roots=project_skill_roots,
        ).build_skills_summary(workspace=project_workspace)
        history_log = (
            str(agent_workspace / "memory" / "history.jsonl")
            if agent_workspace != project_workspace
            else "memory/history.jsonl"
        )
        return render_template(
            "agent/subagent_system.md",
            track_source=True,
            workspace=str(project_workspace),
            agent_workspace=str(agent_workspace),
            history_log=history_log,
            skills_summary=skills_summary or "",
            project=project,
            worktree=worktree,
            native_commentary=native_commentary,
            project_instructions=load_project_instructions(project_workspace) if project else "",
        )

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        records = [record for tid, record in self._tasks.items()
                   if record.status.owner == session_key
                   and (record.status.state == "waiting_input"
                        or (tid in self._running_tasks and not self._running_tasks[tid].done()))]
        known = {record.status.task_id for record in records}
        for status in (await self.read_statuses_for_session(session_key)).values():
            if status.backend == "codex" and status.state == "waiting_input" and status.task_id not in known:
                records.append(await self._restore_codex(status))
        # Suppress every notice before cancellation can yield to a sibling.
        for record in records:
            record.suppress_notice = True
        await asyncio.gather(*(self._cancel_task(record, suppress_notice=True) for record in records))
        return len(records)

    async def close(self) -> None:
        """Request cancellation and bounded cleanup, retaining unfinished work."""
        self._closed = True
        if self._worktree_sweeper is not None:
            self._worktree_sweeper.cancel()
            await asyncio.gather(self._worktree_sweeper, return_exceptions=True)
        await asyncio.gather(*(self._cancel_task(self._tasks[tid], suppress_notice=True)
                               for tid in list(self._running_tasks)
                               if self._tasks[tid].status.backend == "native"
                               or self._tasks[tid].status.state != "waiting_input"))
        cleanups: set[asyncio.Task[int]] = set()
        for record in list(self._tasks.values()):
            if record.status.state == "waiting_input" and record.status.task_id not in self._running_tasks:
                self._tasks.pop(record.status.task_id)
                continue
            cleanup = record.cleanup_task
            retry = cleanup is not None and cleanup.done() and (
                cleanup.cancelled() or cleanup.exception() is not None
            )
            cleanups.add(self._start_cleanup(record, retry=retry))
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._exec_session_manager.close_all())
        cleanups.add(self._close_task)
        done, _ = await asyncio.wait(cleanups, timeout=self.CANCEL_WAIT_SECONDS)
        for task in done:
            task.result()
        if self._tasks:
            logger.warning("Subagent shutdown returned with tasks or cleanup still pending")

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    @property
    def has_pending_work(self) -> bool:
        """Completed turns may still own exec cleanup, including failed cleanup."""
        return (
            any(record.status.state != "waiting_input" for record in self._tasks.values())
            or any(not task.done() for task in self._running_tasks.values())
            or self._exec_session_manager.has_pending_work
        )

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        return sum(
            1 for tid, task in self._running_tasks.items()
            if self._tasks[tid].status.owner == session_key and not task.done()
        )

    def should_wait_for_results(self, session_key: str, turn_id: str | None) -> bool:
        """Check whether a turn should wait for a result before sending its reply."""
        active = [
            self._tasks[tid].status for tid, task in self._running_tasks.items()
            if self._tasks[tid].status.owner == session_key and not task.done()
        ]
        if not any(not status.reply_first and status.state != "waiting_input" for status in active):
            return False
        return turn_id is None or not any(
            status.reply_first and status.owner == session_key and status.origin_turn_id == turn_id
            for status in (*active, *self._terminal_statuses.values())
        )
