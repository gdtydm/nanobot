"""Shared controls for a tool's own delegated tasks."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from nanobot.agent.subagent import SubagentControlError
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.schema import StringSchema
from nanobot.utils.subagent_channel_display import TASK_RECEIPT_SUMMARY_LIMIT

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.subagent_status import SubagentBackend


TASK_RECEIPT_SUMMARY = StringSchema(
    "For background create: brief user-facing Markdown with the scope, key deliverables, and verification "
    "criteria when known. Include an assigned business task number when available. Omit internal instructions.",
    max_length=TASK_RECEIPT_SUMMARY_LIMIT,
)


async def control_task(
    manager: SubagentManager, *, backend: SubagentBackend, action: str,
    task_id: str | None = None, query: str | None = None, message: str | None = None,
    question_id: str | None = None,
) -> str:
    request = current_request_context()
    if request is None or not request.session_key:
        return ToolResult.error("Error: task unavailable")
    owner, sender_id = request.session_key, request.sender_id
    try:
        tasks = None
        if query:
            tasks = await manager.find_tasks(owner, query, sender_id=sender_id, backend=backend)
            if len(tasks) == 1:
                task_id = tasks[0].task_id
            elif action in {"send", "cancel"}:
                raise SubagentControlError("ask the user to identify exactly one task")
        if action == "check":
            if tasks is not None:
                return json.dumps({"tasks": [status.as_dict() for status in tasks]}, ensure_ascii=False)
            if task_id is not None:
                status = await manager.resolve_task(task_id, owner, sender_id=sender_id, backend=backend)
                return json.dumps(status.as_dict(), ensure_ascii=False)
            return json.dumps({
                "tasks": [status.as_dict() for status in await manager.find_tasks(
                    owner, sender_id=sender_id, backend=backend,
                )],
                "projects": {
                    name: str(config.repository.expanduser().resolve())
                    for name, config in manager.tools_config.subagent.projects.items()
                },
            }, ensure_ascii=False)
        if action in {"send", "cancel"}:
            if not task_id:
                return ToolResult.error(f"Error: {action} requires task_id")
            target = await manager.resolve_task(task_id, owner, sender_id=sender_id, backend=backend)
            if action == "send":
                input_id = request.message_id or request.turn_id
                receipt = await manager.send(
                    target.task_id, owner, message, sender_id=sender_id,
                    input_id=str(input_id) if input_id else None, question_id=question_id,
                )
                if backend == "codex" and question_id and not request.can_receive_background_results:
                    result = await manager.wait_for_codex_turn(target.task_id, owner)
                    return json.dumps({**receipt, "task": result.as_dict()}, ensure_ascii=False)
                return json.dumps(receipt)
            status = await manager.cancel(target.task_id, owner, sender_id=sender_id)
            return json.dumps(status.as_dict(), ensure_ascii=False)
        return ToolResult.error("Error: unknown task action")
    except SubagentControlError as exc:
        return ToolResult.error(f"Error: {exc}")
