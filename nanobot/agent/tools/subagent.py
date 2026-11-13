# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING, Any

from nanobot.agent.subagent import SubagentControlError
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context, current_request_session_key
from nanobot.agent.tools.schema import (
    BooleanSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.security.workspace_access import current_workspace_scope
from nanobot.session.manager import SessionPolicy

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.context import ToolContext


@tool_parameters(tool_parameters_schema(
    action=StringSchema(
        "create starts a task; send queues a message; cancel requests an idempotent stop; "
        "check returns task status, receipts, and results.",
        enum=("create", "send", "cancel", "check"),
    ),
    task=StringSchema("Required for create: the task for the subagent to complete.", min_length=1),
    label=StringSchema("Display label for create."),
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
        worktree: bool | None = None, **kwargs: Any,
    ) -> str:
        if action == "create":
            if not task or not task.strip():
                return ToolResult.error("Error: create requires a non-empty task")
            return await self._create_task(task, label, temperature, wait, project, resume_task_id, reply_first, worktree)
        owner = current_request_session_key()
        try:
            if action == "check":
                if not owner:
                    raise SubagentControlError("task unavailable")
                if task_id is not None:
                    return json.dumps(self._manager.check(task_id, owner).as_dict(), ensure_ascii=False)
                return json.dumps({
                    "tasks": [status.as_dict()
                              for status in self._manager.statuses_for_session(owner, include_history=False).values()],
                }, ensure_ascii=False)
            if action in {"send", "cancel"}:
                if not task_id:
                    return ToolResult.error(f"Error: {action} requires task_id")
                if action == "send":
                    return json.dumps(await self._manager.send(task_id, owner, message))
                status = await self._manager.cancel(task_id, owner)
                return json.dumps(status.as_dict(), ensure_ascii=False)
            return ToolResult.error("Error: unknown subagent action")
        except SubagentControlError as exc:
            return ToolResult.error(f"Error: {exc}")

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
        if project is not None and request_ctx.metadata.get("chat_type") != "p2p":
            return ToolResult.error("Error: project tasks require a persistent Feishu private chat")
        method = self._manager.run_inline if inline else partial(self._manager.spawn, reply_first=reply_first)
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
