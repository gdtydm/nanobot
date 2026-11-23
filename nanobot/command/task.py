"""Read task progress and send follow-ups from the originating chat."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from nanobot.agent.subagent import SubagentControlError
from nanobot.agent.subagent_status import SubagentSessionError, SubagentState, SubagentStatus
from nanobot.bus.events import INBOUND_META_TURN_ID, OutboundMessage
from nanobot.bus.outbound_events import TransientCommandReplyEvent
from nanobot.command.spec import BuiltinCommandSpec
from nanobot.utils.subagent_channel_display import task_display_text, task_query_block

if TYPE_CHECKING:
    from nanobot.command.router import CommandContext, CommandRouter

_STATES: dict[SubagentState, str] = {
    "queued": "排队中",
    "running": "运行中",
    "waiting_input": "等待输入",
    "stopping": "正在停止",
    "done": "已完成",
    "incomplete": "未完成",
    "error": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}
_PHASES = {
    "initializing": "初始化",
    "preparing_worktree": "准备工作目录",
    "worktree_ready": "工作目录已就绪",
    "model_input": "处理任务",
    "awaiting_tools": "等待工具返回",
    "tools_completed": "工具已返回",
    "executing": "执行中",
    "reviewing": "审查改动",
    "revising": "按审查结果修订",
    "final_response": "整理结果",
}
_RECEIPTS = {"accepted": "已入队", "delivered": "已送达", "undelivered": "未送达"}
_SENDABLE = {"queued", "running", "waiting_input"}
_USAGE = "/task\n/task <ID>\n/task <ID> send <指令>\n/task send <指令>（仅有一个活跃任务时）"
_TASK_LABEL_PREFIX = re.compile(r"^[A-Z0-9]+(?:[-_][A-Z0-9]+)+\s*[｜|]\s*(?=\S)")
_PHASE_ICONS = {"running": "▶️", "done": "✅"}


def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _task_label(status: SubagentStatus) -> str:
    label = _TASK_LABEL_PREFIX.sub("", status.label.strip()) or status.task_description
    return task_display_text(_one_line(label, 100))


def _progress_text(status: SubagentStatus, text: str) -> str:
    return text.removeprefix("Codex reported: ") if status.backend == "codex" else text


def _phase(status: SubagentStatus) -> str:
    if status.state != "running":
        return _STATES[status.state]
    if any(event.get("status") == "running" for event in status.tool_events):
        return "等待工具返回"
    return _PHASES.get(status.phase, "执行中")


def _task_detail(status: SubagentStatus, tz: ZoneInfo) -> str:
    today = datetime.now(tz).date()

    def clock(at: float) -> str:
        timestamp = datetime.fromtimestamp(at, tz)
        return timestamp.strftime("%H:%M" if timestamp.date() == today else "%m-%d %H:%M")

    sections = [f"{_PHASE_ICONS.get(status.state, '📍')} 当前阶段：{_phase(status)}"]
    if status.question and status.state == "waiting_input":
        sections.append(f"等待回答：{task_display_text(status.question)}")
    if status.recent_progress:
        sections.append("📌 最近进展")
        for entry in reversed(status.recent_progress):
            summary = task_display_text(_progress_text(status, entry.summary))
            detail = task_display_text(entry.detail)
            sections.append(f"⏰ {clock(entry.at)}｜{summary}")
            if detail and detail != summary:
                sections.append(detail)
        latest = status.recent_progress[-1]
        if latest.next_step and status.state in _SENDABLE:
            sections.append(f"👉 下一步：{task_display_text(latest.next_step)}")
    elif status.progress and status.progress_at is not None:
        summary = task_display_text(_progress_text(status, status.progress))
        sections.extend(("📌 最近进展", f"⏰ {clock(status.progress_at)}｜{summary}"))
    else:
        sections.append("暂无进展记录。")
    if status.receipts:
        message_id = next(reversed(status.receipts))
        delivery = status.receipts[message_id]
        sections.append(f"📨 最近投递：{_RECEIPTS.get(delivery, delivery)} · {message_id[:8]}")
    return "\n\n".join(sections)


async def cmd_task(ctx: CommandContext) -> OutboundMessage:
    def reply(title: str, text: str, *, heading: str = "") -> OutboundMessage:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"【子任务｜{title}】{heading}\n\n{text}",
            metadata=dict(ctx.msg.metadata),
            event=TransientCommandReplyEvent(),
        )

    if not ctx.is_user_turn:
        return reply("提示", "请从当前会话发送 /task。")
    manager = ctx.loop.subagents
    sender = ctx.msg.sender_id
    args = ctx.args.split(maxsplit=2)
    try:
        if not args:
            tasks = await manager.find_tasks(ctx.key, sender_id=sender, active_only=True)
            if not tasks:
                return reply("活跃任务", "当前会话没有活跃任务。")
            lines: list[str] = []
            for task in tasks:
                lines.append(f"{_task_label(task)} · {task.backend_name} · {_STATES[task.state]}")
                latest = task.recent_progress[-1].summary if task.recent_progress else task.progress
                if latest:
                    lines.append(task_display_text(_one_line(_progress_text(task, latest), 120)))
                lines.append(task_query_block(task.reference))
            return reply("活跃任务", "\n\n".join(lines))

        if args[0].lower() == "send":
            if len(args) < 2:
                return reply("用法", _USAGE)
            tasks = await manager.find_tasks(ctx.key, sender_id=sender, active_only=True)
            if not tasks:
                return reply("引导", "当前会话没有活跃任务。")
            if len(tasks) > 1:
                return reply("引导", "请用 /task 查看任务，再用 /task <ID> send <指令> 指定目标。")
            target = tasks[0]
            instruction = ctx.args.split(maxsplit=1)[1]
        else:
            if len(args) > 1 and (len(args) != 3 or args[1].lower() != "send"):
                return reply("用法", _USAGE)
            target = await manager.resolve_task(args[0], ctx.key, sender_id=sender)
            if len(args) == 1:
                heading = f"{target.backend_name} [{target.reference}] · {_task_label(target)}"
                return reply("进展", _task_detail(target, ZoneInfo(ctx.loop.context.timezone or "UTC")), heading=heading)
            instruction = args[2]
        input_id = ctx.msg.metadata.get("message_id") or ctx.msg.metadata.get(INBOUND_META_TURN_ID)
        receipt = await manager.send(
            target.task_id, ctx.key, instruction, sender_id=sender,
            input_id=input_id if isinstance(input_id, str) else None,
            private_receipt=True,
        )
        status = manager.check(target.task_id, ctx.key)
        message_id = receipt["message_id"]
        delivery = status.receipts.get(message_id, receipt["receipt"])
        return reply(
            "引导回执",
            f"{_task_label(status)}\n\n📨 投递：{_RECEIPTS.get(delivery, delivery)} · {message_id[:8]}"
            f"\n\n{task_query_block(status.reference)}",
        )
    except SubagentControlError as exc:
        errors = {
            "task unavailable": "当前会话找不到该任务，或编号无法唯一匹配。",
            "task is not accepting messages": "任务已结束或正在停止，无法接收指令。",
            "task message capacity reached": "任务收件箱已满，暂时无法接收指令。",
            "message must contain text and be at most 8192 UTF-8 bytes": "请输入指令，长度上限为 8192 字节。",
        }
        return reply("提示", errors.get(str(exc), str(exc)))
    except SubagentSessionError:
        return reply("提示", "暂时无法读取任务记录。")


def register_task_command(router: CommandRouter) -> None:
    router.add_spec(BuiltinCommandSpec(
        "/task", "Delegated tasks", "Check progress or send a follow-up to a delegated task.",
        "workflow", "[ID] [send <instruction>]", accepts_args=True, transient=True,
    ), after="/status")
    router.exact("/task", cmd_task)
    router.prefix("/task ", cmd_task)
