import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from nanobot.agent.memory import Consolidator
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.codex_task import CodexTaskTool
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry, is_tool_error_result
from nanobot.agent.tools.subagent import SubagentTool
from nanobot.agent.tools.subagent_config import CodexTaskConfig, SubagentToolConfig
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import GenerationSettings, LLMProvider
from nanobot.session.manager import SessionManager
from nanobot.utils.llm_runtime import LLMRuntime


@pytest.mark.asyncio
async def test_delegation_tools_control_their_own_task_type(tmp_path):
    owner = "websocket:tasks"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SessionManager(workspace, sessions_root=tmp_path / "sessions")
    await sessions.state.get(owner)
    provider = MagicMock(spec=LLMProvider)
    provider.generation = GenerationSettings()
    manager = SubagentManager(
        workspace=workspace, bus=MessageBus(), max_tool_result_chars=8192,
        consolidator=MagicMock(spec=Consolidator), session_manager=sessions,
        tools_config=ToolsConfig(subagent=SubagentToolConfig(codex=CodexTaskConfig(enabled=True))),
        max_concurrent_subagents=1,
    )
    registry = ToolRegistry()
    registry.register(SubagentTool(manager))
    registry.register(CodexTaskTool(manager))
    request = RequestContext(
        "websocket", "tasks", session_key=owner, sender_id="owner",
        runtime=LLMRuntime.capture(provider, "test-model", context_window_tokens=65_536),
    )
    await manager._run_slots.acquire()
    try:
        with request_context(request):
            for name in ("subagent", "codex_task"):
                result = await registry.execute(name, {
                    "action": "create", "task": "Review candidates", "label": "Shared label",
                })
                assert not is_tool_error_result(result)
            task_ids = {}
            for name in ("subagent", "codex_task"):
                listed = json.loads(await registry.execute(name, {"action": "check"}))["tasks"]
                assert len(listed) == 1 and listed[0]["state"] == "queued"
                task_ids[name] = listed[0]["task_id"]
                found = json.loads(await registry.execute(name, {"action": "check", "query": "Shared label"}))
                assert found["tasks"] == listed
                receipt = json.loads(await registry.execute(name, {
                    "action": "send", "query": "Shared label", "message": f"Follow-up for {name}",
                }))
                assert receipt["task_id"] == task_ids[name] and receipt["receipt"] == "accepted"
            assert task_ids["subagent"] != task_ids["codex_task"]

            for name, other in (("subagent", "codex_task"), ("codex_task", "subagent")):
                for action in ("check", "send", "cancel"):
                    result = await registry.execute(name, {
                        "action": action, "task_id": task_ids[other], "message": "Wrong tool",
                    })
                    assert is_tool_error_result(result) and "task unavailable" in result
                status = json.loads(await registry.execute(other, {
                    "action": "check", "task_id": task_ids[other][:8],
                }))
                assert status["state"] == "queued" and len(status["receipts"]) == 1

        for foreign in (replace(request, sender_id="other"), replace(request, session_key="websocket:other")):
            with request_context(foreign):
                listed = json.loads(await registry.execute("codex_task", {"action": "check"}))
                assert listed["tasks"] == []
                result = await registry.execute("codex_task", {"action": "check", "task_id": task_ids["codex_task"]})
                assert is_tool_error_result(result) and "task unavailable" in result

        with request_context(request):
            for name in ("subagent", "codex_task"):
                result = json.loads(await registry.execute(name, {"action": "cancel", "query": "Shared label"}))
                assert result["task_id"] == task_ids[name] and result["state"] == "cancelled"
    finally:
        await manager.close()
        await sessions.state.aclose()
