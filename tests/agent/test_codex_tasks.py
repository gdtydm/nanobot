import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import Consolidator
from nanobot.agent.subagent import SubagentControlError, SubagentManager
from nanobot.agent.subagent_project import reclaim_project_worktrees
from nanobot.agent.tools.codex_task import CodexTaskTool
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
from nanobot.utils.llm_runtime import LLMRuntime

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="test App Server uses a POSIX executable script")


@pytest.fixture
def codex_project(tmp_path, request):
    repo = tmp_path / "beacon"
    repo.mkdir()
    cli = tmp_path / "codex"
    expected_network_access = getattr(request, "param", True)
    cli.write_text(f"#!{sys.executable}\nnetwork_access = {expected_network_access!r}\n" + """
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
resumed_thread = False
for line in sys.stdin:
    msg = json.loads(line)
    method, params = msg["method"], msg.get("params", {})
    if "id" not in msg:
        continue
    if method in ("thread/start", "thread/resume"):
        assert Path(params["cwd"]) == Path.cwd()
        assert params["sandbox"] == "workspace-write"
        assert params["approvalPolicy"] == "on-request" and params["approvalsReviewer"] == "auto_review"
        resumed_thread = method == "thread/resume"
        if resumed_thread:
            assert params["threadId"] == thread
        result = {"thread": {"id": thread}, "model": "test-model", "reasoningEffort": "high"}
    elif method == "turn/start":
        assert params["threadId"] == thread and params["effort"] == "high"
        assert params["approvalPolicy"] == "on-request" and params["approvalsReviewer"] == "auto_review"
        assert params["outputSchema"] and params["sandboxPolicy"] == {
            "type": "workspaceWrite", "writableRoots": [str(Path.cwd())], "networkAccess": network_access,
        }
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
            assert resumed_thread and "January" in text
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
        projects={"beacon": SubagentProjectConfig(repository=repo)},
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
        message_id="request", original_user_text="继续做 Beacon 离线评估",
    )
    try:
        with request_context(context):
            tool = CodexTaskTool(manager)
            assert json.loads(await tool.execute(action="check"))["projects"] == {"beacon": str(repo)}
            result = json.loads(await tool.execute(
                action="create", task="Compare candidates", label="Beacon evaluation", project="beacon",
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


@pytest.mark.asyncio
async def test_codex_continues_terminal_task_on_adopted_worktree_branch(tmp_path, codex_project):
    repo, executable = codex_project

    def git(path, *args):
        return subprocess.check_output(
            ["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            text=True,
        ).strip()

    git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("chosen baseline")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-qm", "Baseline")
    base_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", base_commit)
    (repo / "tracked.txt").write_text("unrelated branch tip")
    git(repo, "commit", "-qam", "Unrelated")
    setup = tmp_path / "setup.py"
    setup.write_text(
        "import subprocess, sys\n"
        "path, _, repo, _, branch, _, ref = sys.argv[1:]\n"
        "subprocess.run(['git', '-C', repo, 'worktree', 'add', '-b', branch, path, ref], check=True)\n"
    )
    project = SubagentProjectConfig(repository=repo, worktree_script=setup)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    owner = "cli:direct"

    def start_manager():
        sessions = SessionManager(workspace, sessions_root=tmp_path / "sessions")
        return SubagentManager(
            workspace=workspace, bus=MessageBus(), max_tool_result_chars=8192,
            consolidator=MagicMock(spec=Consolidator), session_manager=sessions,
            tools_config=ToolsConfig(subagent=SubagentToolConfig(
                projects={"beacon": project}, codex=CodexTaskConfig(enabled=True, executable=str(executable)),
            )),
        ), sessions

    manager, sessions = start_manager()
    await sessions.state.get(owner)
    request = RequestContext(
        "cli", "direct", session_key=owner, sender_id="owner", message_id="create",
        original_user_text="Run the evaluation", can_receive_background_results=False,
    )

    async def use_tool(input_id, *, sender="owner", **kwargs):
        with request_context(replace(request, message_id=input_id, sender_id=sender)):
            return await CodexTaskTool(manager).execute(**kwargs)

    try:
        original = json.loads(await use_tool(
            "create", action="create", task="Compare candidates",
            project="beacon", worktree=True, base_ref="origin/main",
        ))
        assert original["state"] == "waiting_input"
        original_id = original["task_id"]
        path = Path(original["worktree"])
        assert git(path, "rev-parse", "HEAD") == base_commit
        git(path, "switch", "-c", "review/clean")
        notes = path / "notes.txt"
        notes.write_text("Local work from the first turn")
        reply = json.loads(await use_tool(
            "answer", action="send", task_id=original_id, question_id=original["question_id"], message="January",
        ))
        failed = reply["task"]
        assert failed["state"] == "error" and failed["receipts"] == {"answer": "undelivered"}
        assert all(value in failed["error"] for value in (original["branch"], "review/clean", base_commit))
        await manager.close()
        await sessions.state.aclose()

        manager, sessions = start_manager()
        continuation = {
            "action": "create", "resume_task_id": original_id,
            "task": "Evaluate January using the existing notes.",
        }
        rejected = await use_tool("resume-other", sender="other", **continuation)
        assert "task unavailable" in rejected
        mismatch = json.loads(await use_tool("resume", **continuation))
        assert mismatch["state"] == "error" and "branch mismatch" in mismatch["error"]
        continuation["adopt_branch"] = "review/clean"
        result = json.loads(await use_tool("resume", **continuation))
        assert result["state"] == "done" and result["resume_task_id"] == original_id
        assert result["task_id"] != original_id and result["worktree"] == str(path)
        assert result["branch"] == git(path, "branch", "--show-current") == "review/clean"
        assert manager.check(result["task_id"], owner).codex_thread == manager.check(original_id, owner).codex_thread
        assert notes.read_text() == "Local work from the first turn"
        assert json.loads(await use_tool("resume", **continuation))["task_id"] == result["task_id"]

        git(path, "add", "notes.txt")
        git(path, "commit", "-qm", "Save local work")
        new_head = git(path, "rev-parse", "HEAD")
        assert await reclaim_project_worktrees(
            {"beacon": project}, workspace, (await manager.read_statuses_for_session(owner)).values(),
            lambda _: False, now=result["completed_at"] + 8 * 24 * 60 * 60,
        ) == [original_id]
        assert not path.exists()
        continuation.pop("adopt_branch")
        notices: asyncio.Queue[OutboundMessage] = asyncio.Queue()

        async def deliver(msg):
            notices.put_nowait(msg)
            return "notice:" + msg.metadata["task_notice_id"]

        manager.send_task_notice = deliver
        with request_context(replace(request, message_id="resume-again", can_receive_background_results=True)):
            async with asyncio.timeout(5):
                reopened = json.loads(await CodexTaskTool(manager).execute(**continuation))
                receipt = await notices.get()
                finished = await notices.get()
        assert reopened["reference"] in receipt.content and "Queued" in receipt.content
        assert "Evaluated in the original thread" in finished.content
        restored = manager.check(reopened["task_id"], owner)
        assert restored.state == "done" and restored.branch == "review/clean"
        assert git(path, "rev-parse", "HEAD") == new_head
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
        tool = "codex_task"
        arguments = self.answer if self.answer is not None else {"action": "create", "task": "Compare candidates"}
        assert any(schema["function"]["name"] == tool for schema in tools or [])
        return LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(id=f"codex_{uuid4().hex}", name=tool, arguments=arguments)],
        )


@pytest.mark.asyncio
async def test_codex_creation_is_visible_only_in_eligible_requests(tmp_path, codex_project):
    repo, executable = codex_project

    class InventoryProvider(_DelegationProvider):
        async def chat(self, messages, tools=None, **kwargs):
            names = {schema["function"]["name"] for schema in tools or []}
            assert "subagent" in names
            return LLMResponse(content="Codex available" if "codex_task" in names else "Codex unavailable")

    loop = AgentLoop(
        bus=MessageBus(), provider=InventoryProvider(), workspace=repo,
        session_manager=SessionManager(repo, sessions_root=tmp_path / "inventory-sessions"),
        tools_config=ToolsConfig(subagent=SubagentToolConfig(
            codex=CodexTaskConfig(enabled=True, executable=str(executable)),
        )),
    )
    try:
        temporary, persistent = await asyncio.gather(
            loop.process_direct("Temporary capabilities", session_key="cli:temporary", ephemeral=True),
            loop.process_direct("Persistent capabilities", session_key="cli:persistent"),
        )
        assert temporary.content == "Codex unavailable"
        assert persistent.content == "Codex available"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_native_result_can_start_codex_task_for_original_sender(tmp_path, codex_project):
    repo, executable = codex_project

    class HandoffProvider(_DelegationProvider):
        async def chat(self, messages, tools=None, **kwargs):
            if messages[-1]["content"] == "Collect candidate date windows":
                return LLMResponse(content="January and February are available.")
            return await super().chat(messages, tools=tools, **kwargs)

    provider = HandoffProvider()
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=repo,
        session_manager=SessionManager(repo, sessions_root=tmp_path / "sessions"),
        tools_config=ToolsConfig(subagent=SubagentToolConfig(
            codex=CodexTaskConfig(enabled=True, executable=str(executable)),
        )),
    )
    owner = "cli:direct"
    try:
        session = await loop.sessions.state.get(owner)
        await loop.sessions.state.prepare_input(session)
        tool = SubagentTool(loop.subagents)
        with request_context(RequestContext(
            "cli", "direct", session_key=owner, sender_id="owner",
            runtime=LLMRuntime.capture(provider, provider.get_default_model(), context_window_tokens=65_536),
        )):
            await tool.execute(action="create", task="Collect candidate date windows", reply_first=True)
        event = await asyncio.wait_for(loop.bus.consume_inbound(), 5)
        reply = await loop._process_message(event)
        assert reply is not None and "Which date window?" in reply.content

        task, = await loop.subagents.find_tasks(owner, "Compare candidates", sender_id="owner")
        assert await loop.subagents.find_tasks(owner, task.reference, sender_id="other") == []
        with request_context(RequestContext(
            "cli", "direct", session_key=owner, sender_id="owner", can_receive_background_results=False,
        )):
            receipt = json.loads(await CodexTaskTool(loop.subagents).execute(
                action="send", task_id=task.reference, question_id=task.question_id, message="January",
            ))
        assert receipt["task"]["state"] == "done"
        assert receipt["task"]["result"] == "Evaluated in the original thread"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("codex_project", [False], indirect=True)
async def test_codex_command_uses_shared_worker_from_direct_cli(tmp_path, codex_project):
    repo, executable = codex_project
    tools_config = ToolsConfig(
        subagent=SubagentToolConfig(codex=CodexTaskConfig.model_validate({
            "enabled": True, "executable": str(executable), "networkAccess": False,
        })),
    )

    async def run_once(message, *, answer=None):
        loop = AgentLoop(
            bus=MessageBus(), provider=_DelegationProvider(answer),
            workspace=repo, session_manager=SessionManager(repo, sessions_root=tmp_path / "cli-sessions"),
            tools_config=tools_config, max_iterations=4,
        )
        try:
            reply = await loop.process_direct(message)
            status = max(loop.subagents.statuses_for_session("cli:direct").values(), key=lambda s: s.created_at)
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
    reply, continued = await run_once("Continue the evaluation", answer={
        "action": "create", "resume_task_id": completed.task_id,
        "task": "Check January with the updated criteria",
    })
    assert reply is not None and continued.reference in reply.content
    assert continued.state == "done" and continued.codex_thread == completed.codex_thread
    assert continued.resume_task_id == completed.task_id and continued.task_id != completed.task_id


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
