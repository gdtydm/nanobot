import asyncio

from nanobot.bus.events import InboundMessage
from nanobot.bus.outbound_events import TransientCommandReplyEvent
from nanobot.providers.base import LLMResponse, ToolCallRequest


async def test_task_commands_work_during_a_turn_and_keep_details_in_the_child(loop_factory):
    loop = loop_factory()
    channel, chat = "weixin", "owner"
    owner = f"{channel}:{chat}"
    await loop.sessions.state.get(owner)
    child_reporting = asyncio.Event()
    parent_running = asyncio.Event()
    release_child = asyncio.Event()
    release_parent = asyncio.Event()
    steer_delivered = asyncio.Event()
    terminal_processed = asyncio.Event()
    instruction = "Change the review to January only"
    private_receipt_id = ""
    calls = {"parent": 0, "child": 0}

    def report(number):
        return ToolCallRequest(
            id=f"progress-{number}", name="report_progress",
            arguments={
                "milestone": f"stage-{number}", "summary": f"Confirmed stage {number}",
                "detail": f"Private detail {number}", "next_step": f"Verify phase {number}",
            },
        )

    async def respond(**kwargs):
        messages = kwargs["messages"]
        tool_names = {item["function"]["name"] for item in kwargs.get("tools") or []}
        if "report_progress" not in tool_names:
            calls["parent"] += 1
            transcript = str(messages)
            assert instruction not in transcript and "Private detail" not in transcript
            if "Child result" in transcript:
                assert private_receipt_id and private_receipt_id not in transcript
                terminal_processed.set()
                return LLMResponse(content="Main terminal reply")
            parent_running.set()
            await release_parent.wait()
            return LLMResponse(content="Main reply")

        calls["child"] += 1
        if calls["child"] == 1:
            return LLMResponse(content=None, tool_calls=[report(n) for n in range(1, 5)])
        if calls["child"] == 2:
            child_reporting.set()
            await release_child.wait()
            return LLMResponse(content=None, tool_calls=[report(5)])
        assert any(msg.get("content") == instruction for msg in messages)
        assert "Check the feature window" in str(messages)
        assert "continue with the remaining deliverables and acceptance criteria" in messages[0]["content"]
        steer_delivered.set()
        return LLMResponse(content="Child result")

    async def command(text, *, chat_id=chat):
        await loop.bus.publish_inbound(InboundMessage(
            channel=channel, chat_id=chat_id, sender_id="user", content=text,
        ))
        async with asyncio.timeout(5):
            while True:
                reply = await loop.bus.consume_outbound()
                if isinstance(reply.event, TransientCommandReplyEvent):
                    assert reply.chat_id == chat_id
                    assert reply.content.startswith("【子任务｜")
                    return reply.content

    loop.provider.provider_name = "test"
    loop.provider.chat_stream_with_retry = respond
    worker = asyncio.create_task(loop.run())
    try:
        await loop.subagents.spawn(
            "Check the feature window", label="Feature window", origin_channel=channel,
            origin_chat_id=chat, session_key=owner, runtime=loop.llm_runtime(), reply_first=True,
        )
        await loop.bus.publish_inbound(InboundMessage(
            channel=channel, chat_id=chat, sender_id="user", content="Continue the main investigation",
        ))
        await asyncio.wait_for(child_reporting.wait(), 5)
        await asyncio.wait_for(parent_running.wait(), 5)
        task, = (await loop.subagents.read_statuses_for_session(owner)).values()
        ref = task.reference

        listed = await command("/task")
        assert all(text in listed for text in (ref, "运行中", "Feature window", "Confirmed stage 4"))
        assert f"```text\n/task {ref}\n```" in listed
        detail = await command(f"/task {ref}")
        assert detail.index("Confirmed stage 4") < detail.index("Confirmed stage 3") < detail.index("Confirmed stage 2")
        assert "Confirmed stage 1" not in detail and "Private detail 4" in detail
        assert "当前阶段" in detail and "Verify phase 4" in detail
        checked = await loop.process_direct(f"/task {ref}", session_key=owner, channel=channel, chat_id=chat)
        assert checked.content == detail
        assert "找不到" in await command(f"/task {ref}", chat_id="another-chat")
        assert "已入队" in await command(f"/task send {instruction}")
        assert "已入队" in await command(f"/task {ref}")
        private_receipt_id, = loop.subagents.check(task.task_id, owner).receipts
        assert not loop.subagents.check(task.task_id, owner).as_dict()["receipts"]
        assert calls == {"parent": 1, "child": 2}
        release_child.set()
        await asyncio.wait_for(steer_delivered.wait(), 5)
        detail = await command(f"/task {ref}")
        assert "已送达" in detail and "Confirmed stage 5" in detail
        child = loop.subagents.sessions.status(task.task_id, owner)
        assert [entry.key for entry in child.recent_progress] == ["stage-3", "stage-4", "stage-5"]
        assert child.private_receipts == {private_receipt_id}

        release_parent.set()
        async with asyncio.timeout(5):
            while (await loop.bus.consume_outbound()).content != "Main terminal reply":
                pass
        assert terminal_processed.is_set()
        session = await loop.sessions.state.read(owner)
        assert "Continue the main investigation" in str(session.messages)
        assert all(
            text not in str(session.messages)
            for text in ("/task", "【子任务｜", "Private detail", "Verify phase", instruction, private_receipt_id)
        )
    finally:
        release_parent.set()
        release_child.set()
        loop.stop()
        await asyncio.wait_for(worker, 10)
