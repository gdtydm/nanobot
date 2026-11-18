from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.subagent import SubagentControlError
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.message import record_message_delivery
from nanobot.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.context import RequestContext, ToolContext


def codex_task_available(request: RequestContext | None) -> bool:
    return bool(request and request.persist_session and request.session_key and request.sender_id)


async def create_codex_task(
    manager: SubagentManager, *, task: str, label: str | None = None,
    project: str | None = None, worktree: bool | None = None,
    resume_task_id: str | None = None, base_ref: str | None = None, adopt_branch: str | None = None,
) -> str:
    request = current_request_context()
    if request is None:
        return ToolResult.error("Error: Codex tasks require a persistent conversation")
    if not task.strip():
        return ToolResult.error("Error: create requires a non-empty task")
    try:
        result, receipt_delivered = await manager.spawn_codex(
            task, label=label, project=project, worktree=worktree, request=request,
            base_ref=base_ref, resume_task_id=resume_task_id, adopt_branch=adopt_branch,
        )
    except SubagentControlError as exc:
        return ToolResult.error(f"Error: {exc}")
    if receipt_delivered:
        record_message_delivery(request.channel, request.chat_id)
    return result


@tool_parameters(tool_parameters_schema(
    task=StringSchema("Task scope, constraints, and evidence needed to verify completion.", min_length=1),
    label=StringSchema("Short display label."),
    project=StringSchema("Configured project to run in. Codex loads its own context in that checkout."),
    worktree=BooleanSchema(description="Use true for an isolated project worktree; false uses the existing checkout."),
    resume_task_id=StringSchema("Finished Codex task ID in this chat. Reuse its saved thread and checkout."),
    base_ref=StringSchema("Git revision for a new worktree. Defaults to HEAD.", min_length=1),
    adopt_branch=StringSchema("With resume_task_id, adopt the named branch already checked out there."),
    required=["task"],
))
class CodexTaskTool(Tool):
    name = "codex_task"
    description = (
        "Start or continue a Codex coding task. Prefer it for repository work that needs cross-file debugging, "
        "edits and iterative tests when Codex has the necessary access. Use it when the user requests Codex "
        "or when continuing a finished Codex task. For work that needs NanoBot-only tools, skills or identity, "
        "use a native subagent instead. Codex reports questions and results in this chat or waits inline "
        "when background delivery is unavailable. Use subagent check, send and cancel to manage the result."
    )

    def __init__(self, manager: SubagentManager) -> None:
        self._manager = manager

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return ctx.subagent_manager is not None and ctx.config.subagent.codex.enabled

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        assert ctx.subagent_manager is not None
        return cls(ctx.subagent_manager)

    async def execute(self, task: str, **kwargs: Any) -> str:
        return await create_codex_task(self._manager, task=task, **kwargs)
