# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

import json
import re
import time
import uuid
from functools import partial
from typing import TYPE_CHECKING, Any

from nanobot.agent.subagent import SubagentControlError
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context, current_request_session_key
from nanobot.agent.tools.message import record_message_delivery
from nanobot.agent.tools.schema import (
    BooleanSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.channels.notification_routes import notification_metadata
from nanobot.security.workspace_access import current_workspace_scope
from nanobot.session.manager import SessionPolicy

_CODEX_DELEGATION = re.compile(
    r"^(?:/codex(?:@[\w]+)?\s+|"
    r"(?:请\s*)?(?:让|用|交给|调用|委托|派)\s*codex(?![a-z0-9_])|"
    r"(?:(?:please\s+)?(?:have|use|ask|let|run)\s+codex\b))",
    re.IGNORECASE,
)
_CODEX_DRAFT_KEY = "codex_delegation"

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.context import ToolContext


@tool_parameters(tool_parameters_schema(
    action=StringSchema(
        "create starts a task; prepare records an explicit Codex request before asking a clarifying "
        "question; send queues a message or answers a waiting task; cancel stops one task; check reads status.",
        enum=("create", "prepare", "send", "cancel", "check"),
    ),
    task=StringSchema("Required for create: the task for the subagent to complete.", min_length=1),
    label=StringSchema("Display label for create."),
    backend=StringSchema("Use codex only when the user explicitly delegates to Codex.", enum=("native", "codex")),
    query=StringSchema("For check, send, or cancel: task reference, title, or project within this conversation."),
    delegation_id=StringSchema("For Codex create after clarification: ID from prepare in the same conversation."),
    question_id=StringSchema("For send: the pending question ID returned by check, when answering a specific question."),
    project=StringSchema("For create: a gateway-configured project whose rules, skills, and working directory apply."),
    worktree=BooleanSchema(
        description=(
            "For a project create: false uses the existing repository checkout; true uses an isolated Git worktree. "
            "Set this explicitly. If omitted, projects with a worktree script use a worktree for compatibility."
        ),
    ),
    resume_task_id=StringSchema(
        "For a project create: full ID of a finished task in this conversation whose worktree should be reused."
    ),
    temperature=NumberSchema(
        description="Sampling temperature for create; inherits the parent's runtime if omitted.",
        minimum=0.0,
        maximum=2.0,
    ),
    wait=BooleanSchema(
        description=(
            "For create: true waits for the result; false runs in background with a completion notification. "
            "Temporary sessions and hosts unable to receive background results always wait."
        ),
        default=False,
    ),
    reply_first=BooleanSchema(
        description=(
            "For background create: deliver the main agent's current reply before reporting this task's result. "
            "Use when a verified finding can be shared while dependent work runs. Requires wait=false "
            "and a conversation that can receive background results."
        ),
        default=False,
    ),
    task_id=StringSchema(
        "Full task ID returned by create. Required for send/cancel; optional for check. "
        "Omit for check to list the current session's active and recently finished tasks.",
        min_length=1,
    ),
    message=StringSchema(
        "Required for send: text, at most 8192 UTF-8 bytes. Accepted means queued, not delivered. "
        "Delivery receipts mean injected into the task transcript, not that the task acted on the message.",
        max_length=8192,
    ),
    required=["action"],
))
class SubagentTool(Tool):
    """Manage the current session's private subagent tasks."""

    def __init__(self, manager: SubagentManager):
        self._manager = manager

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        if ctx.subagent_manager is None:
            raise RuntimeError("SubagentTool requires an initialized subagent manager")
        return cls(ctx.subagent_manager)

    @property
    def name(self) -> str:
        return "subagent"

    @property
    def description(self) -> str:
        return (
            "Manage the current session's private subagent tasks. "
            "For an explicit Codex delegation, resolve the configured project and criteria, read applicable "
            "project instructions, and create with backend=codex. Use prepare before asking a clarifying question "
            "about an explicit delegation, then pass its delegation_id on create. "
            "Use check for natural-language progress queries and configured project discovery. "
            "If a query finds multiple matches, ask which one. "
            "Use send to supplement or answer and cancel to stop the identified task. "
            "Read the pending question with check and pass its question_id when sending an answer. "
            "Codex background tasks send their own receipt, questions and terminal notice. Avoid duplicating these notices. "
            "Background results arrive automatically; avoid repeated polling. "
            "Check with a known task_id to read older results. "
            "Partial results or iteration limits do not mean completion."
        )

    @property
    def concurrency_safe(self) -> bool:
        return True

    async def execute(
        self, action: str, task_id: str | None = None, message: str | None = None,
        task: str | None = None, label: str | None = None,
        temperature: float | None = None, wait: bool = False,
        project: str | None = None, resume_task_id: str | None = None, reply_first: bool = False,
        worktree: bool | None = None, backend: str = "native", query: str | None = None,
        delegation_id: str | None = None, question_id: str | None = None, **kwargs: Any,
    ) -> str:
        owner = current_request_session_key()
        request = current_request_context()
        try:
            if action == "prepare" or (action == "create" and backend == "codex"):
                return await self._delegate_codex(action, task, label, project, worktree, delegation_id)
            if action == "create":
                if not task or not task.strip():
                    return ToolResult.error("Error: create requires a non-empty task")
                return await self._create_task(
                    task, label, temperature, wait, project, resume_task_id, reply_first, worktree,
                )
            if not owner:
                raise SubagentControlError("task unavailable")
            sender_id = request.sender_id if request else None
            tasks = None
            if query:
                tasks = await self._manager.find_tasks(owner, query, sender_id=sender_id)
                if len(tasks) == 1:
                    task_id = tasks[0].task_id
                elif action in {"send", "cancel"}:
                    raise SubagentControlError("ask the user to identify exactly one task")
            if action == "check":
                if tasks is not None:
                    return json.dumps({"tasks": [status.as_dict() for status in tasks]}, ensure_ascii=False)
                if task_id is not None:
                    return json.dumps(
                        (await self._manager.resolve_task(task_id, owner, sender_id=sender_id)).as_dict(),
                        ensure_ascii=False,
                    )
                return json.dumps({
                    "tasks": [status.as_dict()
                              for status in await self._manager.find_tasks(owner, sender_id=sender_id)],
                    "projects": {
                        name: str(config.repository.expanduser().resolve())
                        for name, config in self._manager.tools_config.subagent.projects.items()
                        if config.allows(owner)
                    },
                }, ensure_ascii=False)
            if action in {"send", "cancel"}:
                if not task_id:
                    return ToolResult.error(f"Error: {action} requires task_id")
                task_id = (await self._manager.resolve_task(task_id, owner, sender_id=sender_id)).task_id
                if action == "send":
                    input_id = (request.message_id or request.turn_id) if request else None
                    return json.dumps(await self._manager.send(
                        task_id, owner, message, sender_id=sender_id,
                        input_id=str(input_id) if input_id else None,
                        question_id=question_id,
                    ))
                status = await self._manager.cancel(task_id, owner, sender_id=sender_id)
                return json.dumps(status.as_dict(), ensure_ascii=False)
            return ToolResult.error("Error: unknown subagent action")
        except SubagentControlError as exc:
            return ToolResult.error(f"Error: {exc}")

    async def _delegate_codex(
        self, action: str, task: str | None, label: str | None, project: str | None,
        worktree: bool | None, delegation_id: str | None,
    ) -> str:
        request = current_request_context()
        if request is None or not request.session_key or not request.persist_session or not request.sender_id:
            raise SubagentControlError("Codex tasks require an explicit request in a persistent conversation")
        owner = request.session_key
        original_command = request.metadata.get("original_command")
        text = original_command if isinstance(original_command, str) else request.original_user_text or ""
        explicit = bool(request.original_user_text and _CODEX_DELEGATION.match(text.strip()))
        sessions = self._manager.sessions
        if sessions is None or not self._manager.tools_config.subagent.codex.enabled:
            raise SubagentControlError("Codex tasks are disabled (tools.subagent.codex.enabled)")
        if action == "prepare":
            if not explicit:
                raise SubagentControlError("the user must explicitly ask Codex to do the task")
            draft_id = str(uuid.uuid4())
            await sessions.sessions.state.update_metadata(
                owner, {_CODEX_DRAFT_KEY: {"id": draft_id, "sender": request.sender_id, "created_at": time.time()}},
                expected_generation=request.session_generation,
            )
            return json.dumps({"delegation_id": draft_id})
        if not task or not task.strip():
            raise SubagentControlError("create requires a non-empty task")
        if not explicit:
            session = await sessions.sessions.state.get_header(owner)
            draft = session.metadata.get(_CODEX_DRAFT_KEY)
            if (not delegation_id or not isinstance(draft, dict) or draft.get("id") != delegation_id
                    or draft.get("sender") != request.sender_id
                    or not isinstance(created := draft.get("created_at"), (int, float))
                    or time.time() - created > 3600):
                raise SubagentControlError("the user must explicitly ask Codex to do the task")
        result, receipt_delivered = await self._manager.spawn_codex(
            task, label=label, project=project, worktree=worktree, request=request,
        )
        if receipt_delivered:
            record_message_delivery(request.channel, request.chat_id)
        if delegation_id and not explicit:
            await sessions.sessions.state.update_metadata(
                owner, {}, remove=(_CODEX_DRAFT_KEY,), expected_generation=request.session_generation,
            )
        return result

    async def _create_task(
        self, task: str, label: str | None, temperature: float | None, wait: bool,
        project: str | None = None, resume_task_id: str | None = None, reply_first: bool = False,
        worktree: bool | None = None,
    ) -> str:
        request_ctx = current_request_context()
        if request_ctx is None or request_ctx.runtime is None:
            return ToolResult.error("Error: create requires an active model runtime")
        origin_channel = request_ctx.channel
        origin_chat_id = request_ctx.chat_id
        session_key = request_ctx.session_key or (
            f"{origin_channel}:{origin_chat_id}" if origin_channel and origin_chat_id else None
        )
        if not session_key:
            return ToolResult.error("Error: create requires an active session identity")
        inline = wait or not request_ctx.can_receive_background_results or not request_ctx.persist_session
        if reply_first and inline:
            return ToolResult.error("Error: reply_first requires wait=false and background result delivery")
        method = self._manager.run_inline if inline else partial(self._manager.spawn, reply_first=reply_first)
        if not inline:
            method = partial(method, origin_route=notification_metadata(origin_channel, request_ctx.metadata))
        return await method(
            task=task,
            runtime=request_ctx.runtime,
            label=label,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            session_key=session_key,
            origin_message_id=request_ctx.message_id,
            origin_turn_id=request_ctx.turn_id,
            temperature=temperature,
            workspace_scope=current_workspace_scope(),
            project=project,
            worktree=worktree,
            resume_task_id=resume_task_id,
            session_policy=SessionPolicy(
                persist=request_ctx.persist_session,
                log_content=request_ctx.log_content,
            ),
        )
