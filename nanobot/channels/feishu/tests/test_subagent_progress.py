import asyncio

import pytest

from nanobot.agent.subagent_status import SubagentStatus
from nanobot.bus.queue import MessageBus
from nanobot.bus.runtime_events import RuntimeEventContext, SubagentTaskChanged
from nanobot.channels.feishu.subagent_progress import FeishuSubagentProgress


@pytest.mark.asyncio
async def test_project_progress_reaches_private_chat_once_per_stage_and_stops_at_completion():
    bus = MessageBus()
    ctx = RuntimeEventContext(channel="feishu.product", chat_id="ou_user", session_key="feishu.product:ou_user")
    status = SubagentStatus(
        task_id="task", owner=ctx.session_key, label="LOC-42", task_description="Edit a project",
        started_at=0, project="location", state="running", phase="preparing_worktree",
    )
    event = SubagentTaskChanged(context=ctx, task_id=status.task_id)
    live = True

    def check(task_id, owner):
        assert (task_id, owner) == (status.task_id, status.owner)
        return status

    progress = FeishuSubagentProgress(bus, check, lambda *_: live, quiet_seconds=0.03)
    try:
        await bus.publish(event)
        first = await asyncio.wait_for(bus.consume_outbound(), 1)
        assert (first.channel, first.chat_id) == (ctx.channel, ctx.chat_id)
        assert "LOC-42" in first.content and "已开始准备" in first.content
        status.worktree = "/worktree"
        await bus.publish(event)
        assert "开始执行" in (await asyncio.wait_for(bus.consume_outbound(), 1)).content
        status.phase = "awaiting_tools"
        status.tool_events = [{"name": "exec", "status": "running"}]
        await bus.publish(event)
        assert "当前工具尚未返回" in (await asyncio.wait_for(bus.consume_outbound(), 1)).content
        await bus.publish(event)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), 0.08)
        status.tool_events = [{"name": "edit_file", "status": "ok"}]
        await bus.publish(event)
        assert "已修改" in (await asyncio.wait_for(bus.consume_outbound(), 1)).content
        status.state = "done"
        live = False
        await bus.publish(event)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), 0.08)
    finally:
        await progress.close()
