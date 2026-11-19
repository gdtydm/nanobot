# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.schema import (
    BooleanSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.agent.tools.task_control import control_task
from nanobot.channels.notification_routes import notification_metadata
from nanobot.security.workspace_access import current_workspace_scope
from nanobot.session.manager import SessionPolicy

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.subagent_status import SubagentKind
    from nanobot.agent.tools.context import ToolContext


@tool_parameters(tool_parameters_schema(
    action=StringSchema(
        "Use create to start a native task, send to queue a message, "
        "cancel to stop a task, and check to read its status.",
        enum=("create", "send", "cancel", "check"),
    ),
    task=StringSchema("Required for create: the task for the subagent to complete.", min_length=1),
    label=StringSchema("Display label for create."),
    kind=StringSchema(
        "Native create: use code_change for repository implementation or fixes so managed worktrees "
        "receive automatic review. Use general for research, diagnosis and routine operations.",
        enum=("general", "code_change"),
    ),
    query=StringSchema("For check, send, or cancel: task reference, title, or project within this conversation."),
    project=StringSchema("For create: configured project that supplies the working directory, rules and skills."),
    worktree=BooleanSchema(
        description=(
            "For a project create: false uses the existing repository checkout; true uses an isolated Git worktree. "
            "Set this explicitly. If omitted, projects with a worktree script use a worktree for compatibility."
        ),
    ),
    resume_task_id=StringSchema(
        "On create, pass the full ID of a finished native task in this conversation. Use the original "
        "project and its prepared worktree. Put remaining work in task."
    ),
    base_ref=StringSchema(
        "Git revision for a new project worktree, such as origin/main or a commit ID. Defaults to HEAD.",
        min_length=1,
    ),
    adopt_branch=StringSchema(
        "With resume_task_id, use the given local branch already checked out in the same project worktree.",
        min_length=1,
    ),
    temperature=NumberSchema(
        description="Sampling temperature for native create. Defaults to the parent runtime's temperature.",
        minimum=0.0,
        maximum=2.0,
    ),
    wait=BooleanSchema(
        description=(
            "For native create: true waits for the result. false runs in the background in a persistent "
            "conversation that supports background results. Otherwise, native waits inline. Native worktrees "
            "require background execution with wait=false."
        ),
        default=False,
    ),
    reply_first=BooleanSchema(
        description=(
            "For native background create: deliver the main agent's current reply before reporting the result. "
            "Use when a verified finding can be shared while dependent work runs. Requires wait=false "
            "and a conversation that can receive background results."
        ),
        default=False,
    ),
    task_id=StringSchema(
        "Full task ID or short reference returned by create. For send/cancel, provide task_id or query. "
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
    """Create and manage the current session's native subagent tasks."""

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
            "Create and manage native NanoBot subagent tasks. Use create with the task scope, "
            "constraints, and expected result. For configured projects, set project and worktree explicitly.\n\n"
            "Native uses the parent model and NanoBot tools. Prefer it for work that needs NanoBot project skills, "
            "tools or identity. Use wait=false for background work "
            "when supported, or wait=true for an inline result. Native worktrees require background execution. To "
            "continue in a finished task's prepared worktree, use create with the original project and resume_task_id. "
            "Give the new task the remaining work and relevant context.\n\n"
            "Use send to add instructions to queued or running native tasks. "
            "Use check to discover projects, find tasks, or read status and results by task_id. Use cancel to stop a task. "
            "If several tasks match, ask the user which one before sending or cancelling. "
            "Supported channels deliver background results automatically. Verify the outcome before reporting completion."
        )

    @property
    def concurrency_safe(self) -> bool:
        return True

    async def execute(
        self, action: str, task_id: str | None = None, message: str | None = None,
        task: str | None = None, label: str | None = None,
        temperature: float | None = None, wait: bool = False,
        project: str | None = None, resume_task_id: str | None = None, reply_first: bool = False,
        worktree: bool | None = None, query: str | None = None,
        base_ref: str | None = None, adopt_branch: str | None = None,
        kind: SubagentKind | None = None,
    ) -> str:
        if action == "create":
            if not task or not task.strip():
                return ToolResult.error("Error: create requires a non-empty task")
            return await self._create_task(
                task, label, temperature, wait, project, resume_task_id, reply_first, worktree,
                base_ref=base_ref, adopt_branch=adopt_branch, kind=kind,
            )
        return await control_task(
            self._manager, backend="native", action=action, task_id=task_id, query=query, message=message,
        )

    async def _create_task(
        self, task: str, label: str | None, temperature: float | None, wait: bool,
        project: str | None = None, resume_task_id: str | None = None, reply_first: bool = False,
        worktree: bool | None = None, *, base_ref: str | None = None, adopt_branch: str | None = None,
        kind: SubagentKind | None = None,
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
            method = partial(
                method, origin_route=notification_metadata(origin_channel, request_ctx.metadata),
                sender_id=request_ctx.sender_id,
            )
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
            base_ref=base_ref,
            adopt_branch=adopt_branch,
            kind=kind,
            session_policy=SessionPolicy(
                persist=request_ctx.persist_session,
                log_content=request_ctx.log_content,
            ),
        )
