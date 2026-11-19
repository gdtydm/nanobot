from __future__ import annotations

from typing import TYPE_CHECKING

from nanobot.agent.subagent import SubagentControlError
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.message import record_message_delivery
from nanobot.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema
from nanobot.agent.tools.task_control import control_task

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.context import RequestContext, ToolContext


def codex_task_available(request: RequestContext | None) -> bool:
    return bool(request and request.persist_session and request.session_key and request.sender_id)


@tool_parameters(tool_parameters_schema(
    action=StringSchema(
        "Use create to start or continue a Codex task, check to read status, send to queue a message "
        "or answer a waiting task, and cancel to stop it.",
        enum=("create", "send", "cancel", "check"),
    ),
    task=StringSchema("Required for create: task scope, constraints, and evidence needed to verify completion.", min_length=1),
    label=StringSchema("Short display label."),
    project=StringSchema("Configured project to run in. Codex loads its own context in that checkout."),
    worktree=BooleanSchema(description="Use true for an isolated project worktree; false uses the existing checkout."),
    resume_task_id=StringSchema("Finished Codex task ID in this chat. Reuse its saved thread and checkout."),
    base_ref=StringSchema("Git revision for a new worktree. Defaults to HEAD.", min_length=1),
    adopt_branch=StringSchema("With resume_task_id, adopt the named branch already checked out there."),
    task_id=StringSchema(
        "Full task ID or short reference returned by create. For send/cancel, provide task_id or query. "
        "Omit for check to list this conversation's Codex tasks and configured projects.", min_length=1,
    ),
    query=StringSchema("For check, send, or cancel: Codex task reference, title, or project within this conversation."),
    message=StringSchema(
        "Required for send: text, at most 8192 UTF-8 bytes. Accepted means queued, not delivered. "
        "Delivery receipts mean injected into the task transcript, not that the task acted on the message.",
        max_length=8192,
    ),
    question_id=StringSchema(
        "For send to a task in waiting_input: pass its current question ID from check with the answer.",
    ),
    required=["action"],
))
class CodexTaskTool(Tool):
    name = "codex_task"
    description = (
        "Create and manage Codex coding tasks. Prefer it for repository work that needs cross-file debugging, "
        "edits and iterative tests when Codex has the necessary access. Use it when the user requests Codex "
        "or when continuing a finished Codex task. For work that needs NanoBot-only tools, skills or identity, "
        "use a native subagent instead. Codex reports questions and results in this chat or waits inline "
        "when background delivery is unavailable. Use create with task to start work, or include resume_task_id "
        "to continue a finished task. Use check to find Codex tasks and read status or results. Use send to add "
        "instructions; for waiting_input, get question_id with check and include it with the answer. "
        "Use cancel to stop a Codex task. If several tasks match, ask the user which one before sending or cancelling. "
        "Verify the outcome before reporting completion."
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

    async def execute(
        self, action: str, task_id: str | None = None, message: str | None = None,
        task: str | None = None, label: str | None = None, project: str | None = None,
        worktree: bool | None = None, resume_task_id: str | None = None,
        base_ref: str | None = None, adopt_branch: str | None = None,
        query: str | None = None, question_id: str | None = None,
    ) -> str:
        request = current_request_context()
        if request is None or not codex_task_available(request):
            return ToolResult.error("Error: Codex tasks require a persistent conversation and sender identity")
        if action != "create":
            return await control_task(
                self._manager, backend="codex", action=action, task_id=task_id, query=query,
                message=message, question_id=question_id,
            )
        if not task or not task.strip():
            return ToolResult.error("Error: create requires a non-empty task")
        try:
            result, receipt_delivered = await self._manager.spawn_codex(
                task, label=label, project=project, worktree=worktree, request=request,
                base_ref=base_ref, resume_task_id=resume_task_id, adopt_branch=adopt_branch,
            )
        except SubagentControlError as exc:
            return ToolResult.error(f"Error: {exc}")
        if receipt_delivered:
            record_message_delivery(request.channel, request.chat_id)
        return result
