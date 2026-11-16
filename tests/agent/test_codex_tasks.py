import asyncio
import json
import sys
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import Consolidator
from nanobot.agent.subagent import SubagentControlError, SubagentManager
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.subagent import SubagentTool
from nanobot.agent.tools.subagent_config import (
    CodexTaskConfig,
    SubagentProjectConfig,
    SubagentToolConfig,
)
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.session.manager import SessionManager

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="test App Server uses a POSIX executable script")


@pytest.fixture
def codex_project(tmp_path):
    repo = tmp_path / "beacon"
    repo.mkdir()
    cli = tmp_path / "codex"
    cli.write_text(f"#!{sys.executable}\n" + """
import json
import sys
from pathlib import Path

def emit(data):
    print(json.dumps(data), flush=True)

def finish(thread, turn, state, summary, question=None):
    emit({"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": {
        "type": "agentMessage", "text": json.dumps({"state": state, "summary": summary, "question": question}),
    }}})
    emit({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}}})

thread = "beacon-original"
turn = ""
for line in sys.stdin:
    msg = json.loads(line)
    method, params = msg["method"], msg.get("params", {})
    if "id" not in msg:
        continue
    if method in ("thread/start", "thread/resume"):
        assert Path(params["cwd"]) == Path.cwd()
        if method == "thread/resume":
            assert params["threadId"] == thread
        result = {"thread": {"id": thread}, "model": "test-model", "reasoningEffort": "high"}
    elif method == "turn/start":
        assert params["threadId"] == thread and params["effort"] == "high"
        assert params["outputSchema"] and params["sandboxPolicy"]["type"] == "workspaceWrite"
        text = params["input"][0]["text"]
        turn = "resume" if "Continue the same task" in text else "initial"
        result = {"turn": {"id": turn}}
    elif method == "turn/steer":
        assert params["threadId"] == thread and params["expectedTurnId"] == turn
        result = {"turnId": turn}
    else:
        result = {}
    emit({"id": msg["id"], "result": result})
    if method == "turn/start":
        if turn == "resume":
            assert "January" in text
            finish(thread, turn, "done", "Evaluated in the original thread")
        elif "keep running" not in text:
            finish(thread, turn, "needs_input", "Candidates ready", "Which date window?")
        else:
            emit({"method": "item/started", "params": {"threadId": thread, "turnId": turn,
                  "item": {"type": "commandExecution"}}})
    if method == "turn/interrupt":
        emit({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "interrupted"}}})
""")
    cli.chmod(0o700)
    return repo, cli


@pytest.mark.asyncio
@pytest.mark.parametrize(("channel", "chat_id"), [("websocket", "beacon-web"), ("telegram", "-100"), ("feishu", "ou_user")])
async def test_codex_waiting_task_resumes_same_thread_after_restart(tmp_path, codex_project, channel, chat_id):
    repo, executable = codex_project
    owner = f"{channel}:{chat_id}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SessionManager(workspace, sessions_root=tmp_path / "sessions")
    await sessions.state.get(owner)
    config = ToolsConfig(subagent=SubagentToolConfig(
        projects={"beacon": SubagentProjectConfig(repository=repo, sessions=[owner])},
        codex=CodexTaskConfig(enabled=True, executable=str(executable)),
    ))
    notices: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def deliver(msg):
        notices.put_nowait(msg)
        return "notice:" + msg.metadata["task_notice_id"]

    def new_manager():
        manager = SubagentManager(
            workspace=workspace, bus=MessageBus(), max_tool_result_chars=8192,
            consolidator=MagicMock(spec=Consolidator), session_manager=sessions,
            tools_config=config, max_concurrent_subagents=1,
        )
        manager.send_task_notice = deliver
        return manager

    manager = new_manager()
    context = RequestContext(
        channel=channel, chat_id=chat_id, session_key=owner, sender_id="owner",
        message_id="request", original_user_text="让 Codex 做 Beacon 离线评估",
    )
    try:
        with request_context(context):
            result = json.loads(await SubagentTool(manager).execute(
                action="create", backend="codex", task="Compare candidates", label="Beacon evaluation", project="beacon",
            ))
        task_id = result["task_id"]
        receipt = await asyncio.wait_for(notices.get(), 5)
        question = await asyncio.wait_for(notices.get(), 5)
        assert "Queued" in receipt.content and result["reference"] in receipt.content
        assert "Which date window?" in question.content
        async with asyncio.timeout(5):
            while manager.get_running_count():
                await asyncio.sleep(0.01)
        assert manager.check(task_id, owner).state == "waiting_input"
        assert not manager.has_pending_work
        await manager.send(task_id, owner, "Use US first", sender_id="owner", input_id="supplement")
        assert manager.check(task_id, owner).state == "waiting_input"

        await manager.close()
        await sessions.state.aclose()
        sessions = SessionManager(workspace, sessions_root=tmp_path / "sessions")
        manager = new_manager()
        await manager.recover_interrupted()
        assert manager.check(task_id, owner).state == "waiting_input"
        assert (await manager.find_tasks(owner, "Beacon"))[0].task_id == task_id
        with pytest.raises(SubagentControlError):
            await manager.send(task_id, owner, "January", sender_id="other")

        reply = InboundMessage(channel=channel, chat_id=chat_id, sender_id="owner", content="quoted text\nJanuary",
                               metadata={"task_reply_to": "notice:" + question.metadata["task_notice_id"],
                                         "task_reply_text": "January", "message_id": "answer"})
        result = await manager.route_task_reply(reply)
        assert result and "Input accepted" in result.content
        assert await manager.route_task_reply(reply) is not None
        terminal = await asyncio.wait_for(notices.get(), 5)
        assert "Evaluated in the original thread" in terminal.content
        status = manager.check(task_id, owner)
        assert status.state == "done" and status.receipts == {"supplement": "delivered", "answer": "delivered"}
        assert status.codex_thread == "beacon-original" and status.codex_turn == "resume"
    finally:
        await manager.close()
        await sessions.state.aclose()


class _DelegationProvider(LLMProvider):
    def __init__(self, answer=None):
        super().__init__(provider_name="offline-test")
        self.answer = answer

    def get_default_model(self):
        return "test-model"

    async def chat(self, messages, tools=None, **kwargs):
        if messages[-1]["role"] == "tool":
            status = json.loads(messages[-1]["content"])
            status = status.get("task", status)
            return LLMResponse(content=f"{status['reference']} {status['question'] or status['result']}")
        arguments = self.answer or {"action": "create", "backend": "codex", "task": "Compare candidates"}
        return LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(id=f"codex_{arguments['action']}", name="subagent", arguments=arguments)],
        )


@pytest.mark.asyncio
async def test_codex_command_uses_shared_worker_from_direct_cli(tmp_path, codex_project):
    repo, executable = codex_project
    tools_config = ToolsConfig(
        subagent=SubagentToolConfig(codex=CodexTaskConfig(enabled=True, executable=str(executable))),
    )

    async def run_once(message, *, answer=None):
        loop = AgentLoop(
            bus=MessageBus(), provider=_DelegationProvider(answer),
            workspace=repo, session_manager=SessionManager(repo, sessions_root=tmp_path / "cli-sessions"),
            tools_config=tools_config, max_iterations=4,
        )
        try:
            reply = await loop.process_direct(message)
            status, = loop.subagents.statuses_for_session("cli:direct").values()
            return reply, status
        finally:
            await loop.aclose()

    reply, pending = await run_once("/codex compare Beacon candidates")
    assert reply is not None and pending.reference in reply.content
    assert pending.state == "waiting_input" and "Which date window?" in reply.content

    reply, completed = await run_once("Use January", answer={
        "action": "send", "task_id": pending.reference, "question_id": pending.question_id, "message": "January",
    })
    assert completed.state == "done" and completed.codex_thread == pending.codex_thread
    assert reply is not None and "Evaluated in the original thread" in reply.content


@pytest.mark.asyncio
async def test_codex_steer_and_targeted_stop(tmp_path, codex_project):
    repo, executable = codex_project
    owner = "websocket:beacon"
    sessions = SessionManager(repo, sessions_root=tmp_path / "sessions")
    await sessions.state.get(owner)
    manager = SubagentManager(
        workspace=repo, bus=MessageBus(), max_tool_result_chars=8192,
        consolidator=MagicMock(spec=Consolidator), session_manager=sessions,
        tools_config=ToolsConfig(subagent=SubagentToolConfig(
            codex=CodexTaskConfig(enabled=True, executable=str(executable)),
        )), max_concurrent_subagents=2,
    )
    try:
        task_ids = []
        for message_id in ("one", "two"):
            result, _ = await manager.spawn_codex(
                "keep running", label="Beacon", project=None, worktree=None,
                request=RequestContext("websocket", "beacon", session_key=owner, sender_id="owner",
                                       message_id=message_id),
            )
            task_ids.append(json.loads(result)["task_id"])
        first, second = task_ids
        async with asyncio.timeout(5):
            while any(manager.check(task_id, owner).progress is None for task_id in task_ids):
                await asyncio.sleep(0.01)
        await manager.send(first, owner, "US first", sender_id="owner", input_id="steer")
        assert manager.check(first, owner).receipts["steer"] == "delivered"
        assert (await manager.cancel(first, owner, sender_id="owner")).state == "cancelled"
        assert manager.check(second, owner).state == "running"
    finally:
        await manager.close()
        await sessions.state.aclose()
