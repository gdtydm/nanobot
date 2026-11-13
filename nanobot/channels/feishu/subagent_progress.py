from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from nanobot.agent.subagent import SubagentControlError
from nanobot.agent.subagent_status import SubagentSessionError, SubagentStatus
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.bus.runtime_events import RuntimeEventContext, SubagentTaskChanged

Stage = Literal["queued", "preparing", "working", "edited"]
_MILESTONES = {
    "preparing": "已开始准备独立工作目录。",
    "working": "独立工作目录已就绪，开始执行。",
    "edited": "已修改工作目录中的文件，任务仍在执行。",
}


def _stage(status: SubagentStatus, previous: Stage | None = None) -> Stage | None:
    if status.worktree_id is None or status.reply_first:
        return None
    if status.state == "queued":
        return "queued"
    if status.state == "running":
        if not status.worktree:
            return "preparing"
        if previous == "edited" or any(
            event.get("name") in {"write_file", "edit_file", "apply_patch"} and event.get("status") == "ok"
            for event in status.tool_events
        ):
            return "edited"
        return "working"
    return None


@dataclass(slots=True)
class _Progress:
    stage: Stage
    reminder: asyncio.Task[None]


class FeishuSubagentProgress:
    """Project task milestones and one quiet reminder per stage."""

    def __init__(
        self,
        bus: MessageBus,
        check: Callable[[str, str], SubagentStatus],
        is_live: Callable[[str, str], bool],
        *,
        quiet_seconds: float = 90,
    ):
        self._bus = bus
        self._check = check
        self._is_live = is_live
        self._quiet_seconds = quiet_seconds
        self._stages: dict[str, _Progress] = {}
        self._unsubscribe = bus.subscribe(self._on_change, SubagentTaskChanged)

    async def close(self) -> None:
        self._unsubscribe()
        reminders = [item.reminder for item in self._stages.values()]
        self._stages.clear()
        for reminder in reminders:
            reminder.cancel()
        await asyncio.gather(*reminders, return_exceptions=True)

    def _read(self, task_id: str, context: RuntimeEventContext) -> SubagentStatus | None:
        try:
            return self._check(task_id, context.session_key)
        except (SubagentControlError, SubagentSessionError):
            return None

    async def _on_change(self, event: SubagentTaskChanged) -> None:
        ctx, task_id = event.context, event.task_id
        if (
            ctx.channel.split(".", 1)[0] != "feishu"
            or not ctx.chat_id.startswith("ou_")
            or ctx.session_key != f"{ctx.channel}:{ctx.chat_id}"
        ):
            return
        previous = self._stages.get(task_id)
        status = self._read(task_id, ctx)
        stage = _stage(status, previous.stage if previous else None) if status else None
        if previous is not None:
            if previous.stage == stage:
                return
            previous.reminder.cancel()
            self._stages.pop(task_id)
        if stage is None or status is None:
            return

        self._stages[task_id] = _Progress(stage, asyncio.create_task(self._remind(task_id, ctx, stage)))
        if milestone := _MILESTONES.get(stage):
            await self._send(ctx, status, milestone)

    async def _remind(self, task_id: str, ctx: RuntimeEventContext, stage: Stage) -> None:
        await asyncio.sleep(self._quiet_seconds)
        status = self._read(task_id, ctx)
        if status is None or _stage(status, stage) != stage:
            return
        try:
            if not self._is_live(task_id, ctx.session_key):
                return
        except (SubagentControlError, SubagentSessionError):
            return
        if stage == "queued":
            message = "任务仍在排队，正在等待执行名额。"
        elif stage == "preparing":
            message = "仍在准备独立工作目录。"
        elif any(event.get("status") == "running" for event in status.tool_events):
            message = "任务仍在执行，当前工具尚未返回。"
        else:
            message = "任务仍在执行，完成后会回报结果。"
        await self._send(ctx, status, message)

    async def _send(self, ctx: RuntimeEventContext, status: SubagentStatus, message: str) -> None:
        label = re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}\b", status.label)
        number = label[0] if label else status.task_id[:8]
        await self._bus.publish_outbound(OutboundMessage(
            channel=ctx.channel, chat_id=ctx.chat_id, content=f"任务 {number}：{message}",
        ))
