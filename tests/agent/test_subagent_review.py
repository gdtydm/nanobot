import asyncio
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.model_presets import build_static_preset_snapshot
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.subagent import SubagentTool
from nanobot.agent.tools.subagent_config import (
    NativeReviewConfig,
    SubagentProjectConfig,
    SubagentToolConfig,
)
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import (
    AgentDefaults,
    AgentsConfig,
    Config,
    ModelPresetConfig,
    ToolsConfig,
)
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.session.manager import SessionManager
from nanobot.utils.llm_runtime import LLMRuntime

_TASK = 'Fix normalization so normalize(" ABC ") returns "abc". Verify the original input.'
_TEST = "import unittest\nfrom normalize import normalize\n\n\nclass Cases(unittest.TestCase):\n"
_BASIC = '    def test_lowercase(self):\n        self.assertEqual(normalize("abc"), "abc")\n'
_REGRESSION = '    def test_original_trigger(self):\n        self.assertEqual(normalize(" ABC "), "abc")\n'
_COMMAND = f"{shlex.quote(sys.executable)} -m unittest discover -v"


def _call(name, **arguments):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(uuid4().hex, name, arguments)])


@pytest.fixture
def project(tmp_path):
    if sys.platform == "win32" or not shutil.which("git") or not shutil.which("python3"):
        pytest.skip("requires POSIX shell, Git and python3")
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    (repo / ".gitignore").write_text("__pycache__/\n")
    (repo / "normalize.py").write_text("def normalize(value):\n    return value\n")
    (repo / "test_normalize.py").write_text(_TEST + _BASIC)
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
    setup = tmp_path / "setup.py"
    setup.write_text(
        "import subprocess, sys\n"
        "path, _, repo, _, branch, _, ref = sys.argv[1:]\n"
        "subprocess.run(['git', '-C', repo, 'worktree', 'add', '-b', branch, path, ref], check=True)\n"
    )
    return SubagentProjectConfig(repository=repo, worktree_script=setup)


class _ReviewScenario(LLMProvider):
    """Script model decisions, and base review verdicts on actual tool evidence."""

    def __init__(self, rework_succeeds, review_model, execution_model):
        super().__init__(provider_name="offline-test")
        self.rework_succeeds = rework_succeeds
        self.review_model = review_model
        self.execution_model = execution_model
        self.reviews = []
        self.first_green = False
        self.before_pass = None
        self.review_started = asyncio.Event()
        self.wait_for_cancel = False

    def get_default_model(self):
        return "test-model"

    async def chat(self, messages, tools=None, **kwargs):
        names = {schema["function"]["name"] for schema in tools or []}
        last = messages[-1]
        if names == {"read_review_file"}:
            assert kwargs["model"] == self.review_model
            evidence = json.loads(messages[1]["content"])
            assert evidence["task"] == _TASK
            assert any(record["command"] == _COMMAND and record["exit_code"] == 0
                       and record["matches_candidate"] for record in evidence["completed_commands"])
            if last["role"] != "tool":
                self.review_started.set()
                if self.wait_for_cancel:
                    await asyncio.Future()
                return _call("read_review_file", path="test_normalize.py")
            if "test_original_trigger" in last["content"]:
                assert "return value.strip().lower()" in evidence["working_diff"]
                if self.before_pass is not None:
                    self.before_pass()
                    self.before_pass = None
                self.reviews.append("pass")
                return LLMResponse(content=json.dumps({"verdict": "pass", "summary": "Original input is tested."}))
            assert "return value.lower()" in evidence["working_diff"]
            self.reviews.append("changes_requested")
            return LLMResponse(content=json.dumps({
                "verdict": "changes_requested", "summary": "The green test misses surrounding whitespace.",
                "findings": [{"requirement": "Normalize the original input", "path": "test_normalize.py", "line": 6,
                              "trigger": '" ABC " still has spaces', "change": "Strip spaces and add a regression test."}],
            }))

        assert kwargs["model"] == self.execution_model
        if last["role"] == "user":
            is_rework = "internal review feedback" in last["content"]
            if is_rework:
                if "changed during review" in last["content"]:
                    return _call("exec", command=_COMMAND, yield_time_ms=1000)
                assert "Strip spaces and add a regression test" in last["content"]
                if not self.rework_succeeds:
                    return _call("exec", command=_COMMAND, yield_time_ms=1000)
            expression = "value.strip().lower()" if is_rework else "value.lower()"
            return _call("write_file", path="normalize.py", content=f"def normalize(value):\n    return {expression}\n")
        last_call = next(message["tool_calls"][-1]["function"] for message in reversed(messages)
                         if message.get("tool_calls"))
        if last_call["name"] == "write_file":
            if "strip().lower()" in str(last_call["arguments"]):
                return _call("write_file", path="test_normalize.py", content=_TEST + _BASIC + _REGRESSION)
            return _call("exec", command=_COMMAND, yield_time_ms=1000)
        assert last_call["name"] == "exec"
        assert "Exit code: 0" in last["content"]
        if not self.reviews:
            assert "Ran 1 test" in last["content"]
            self.first_green = True
        elif self.rework_succeeds:
            assert "Ran 2 tests" in last["content"]
        return LLMResponse(content="Implementation and existing tests are green.")


@pytest.mark.asyncio
@pytest.mark.parametrize(("rework_succeeds", "during_review", "review_preset", "native_preset"), [
    (True, None, None, None), (False, None, None, None), (True, "change", None, None), (True, "cancel", None, None),
    (True, None, "reviewer", "worker"), (True, None, None, "worker"),
], ids=["fix-and-re-review", "revision-limit", "changed-candidate", "cancel-review",
        "dedicated-review-and-native-models", "dedicated-native-model-with-default-review"])
async def test_native_review_checks_original_trigger_despite_green_tests(
    tmp_path, project, rework_succeeds, during_review, review_preset, native_preset,
):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    owner = "cli:review"
    sessions = SessionManager(workspace, sessions_root=tmp_path / "sessions")
    await sessions.state.get(owner)
    review_model = ModelPresetConfig(model="review-model")
    worker_model = ModelPresetConfig(model="worker-model")
    provider = _ReviewScenario(
        rework_succeeds,
        review_model.model if review_preset else "test-model",
        worker_model.model if native_preset else "test-model",
    )
    config = Config(
        agents=AgentsConfig(defaults=AgentDefaults(
            workspace=str(workspace), model="main-default", max_tool_iterations=7,
        )),
        tools=ToolsConfig(subagent=SubagentToolConfig(
            projects={"review": project}, native_model_preset=native_preset,
            native_review=NativeReviewConfig(model_preset=review_preset),
        )),
        model_presets={"reviewer": review_model, "worker": worker_model},
    )
    agent = AgentLoop.from_config(
        config, MessageBus(), provider=provider, session_manager=sessions, tool_registry=ToolRegistry(),
        preset_snapshot_loader=lambda name: build_static_preset_snapshot(provider, name, config.model_presets[name]),
    )
    manager = agent.subagents
    provider.wait_for_cancel = during_review == "cancel"
    if during_review == "change":
        def change_candidate():
            task, = manager.statuses_for_session(owner).values()
            with (Path(task.worktree) / "normalize.py").open("a") as target:
                target.write("\n# Another change while review was running.\n")

        provider.before_pass = change_candidate
    runtime = LLMRuntime.capture(provider, provider.get_default_model(), context_window_tokens=65_536)
    try:
        with request_context(RequestContext("cli", "review", session_key=owner, runtime=runtime, sender_id="owner")):
            tool = SubagentTool(manager)
            await tool.execute(action="create", task=_TASK, project="review", worktree=True, kind="code_change")
            if during_review == "cancel":
                await asyncio.wait_for(provider.review_started.wait(), 15)
                task, = manager.statuses_for_session(owner).values()
                assert task.phase == "reviewing"
                stopped = json.loads(await tool.execute(action="cancel", task_id=task.task_id))
                assert stopped["state"] == "cancelled" and provider.first_green and not provider.reviews
                return
            notice = await asyncio.wait_for(manager.bus.consume_inbound(), 20)
            status = json.loads(await tool.execute(action="check", task_id=notice.metadata["subagent_task_id"]))
        assert provider.first_green
        assert status["review"]["candidate"]
        if rework_succeeds:
            pass_count = 2 if during_review == "change" else 1
            assert provider.reviews == ["changes_requested", *(["pass"] * pass_count)]
            assert status["state"] == "done" and status["review"]["round"] == 1 + pass_count
            assert "Original input is tested" in notice.content
            assert _REGRESSION in (Path(status["worktree"]) / "test_normalize.py").read_text()
        else:
            assert provider.reviews == ["changes_requested"] * 3
            assert status["state"] == "incomplete" and status["stop_reason"] == "review_incomplete"
            assert "revision limit" in notice.content
        assert (project.repository / "normalize.py").read_text().endswith("return value\n")
    finally:
        await agent.aclose()
