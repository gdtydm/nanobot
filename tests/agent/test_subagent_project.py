import asyncio
import re
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from nanobot.agent.memory import Consolidator
from nanobot.agent.runner import AgentRunResult
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.subagent_project import (
    prepare_project_worktree,
    reclaim_project_worktrees,
)
from nanobot.agent.subagent_status import SubagentStatus
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.subagent import SubagentTool
from nanobot.agent.tools.subagent_config import SubagentProjectConfig, SubagentToolConfig
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import GenerationSettings, LLMProvider
from nanobot.security.workspace_access import (
    bind_workspace_scope,
    current_workspace_scope,
    default_workspace_scope,
    reset_workspace_scope,
)
from nanobot.session.manager import SessionManager
from nanobot.utils.llm_runtime import LLMRuntime

OWNER = "feishu:ou_owner"
PROJECT = "tiktok_location_offline"


@pytest.mark.asyncio
@pytest.mark.parametrize("restricted", [False, True], ids=["full-parent", "restricted-parent"])
@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("python3") is None,
    reason="requires Git and python3 for the worktree setup script",
)
async def test_feishu_project_tasks_use_distinct_worktrees_and_resume_their_own(tmp_path, restricted):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init")
    (repo / "AGENTS.md").write_text("BASE_RULE_MARKER")
    git("add", "AGENTS.md")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial")
    (workspace / "previous-report.txt").write_text("HOST_REPORT_MARKER")
    script = tmp_path / "new_worktree.py"
    script.write_text(
        "import pathlib, subprocess, sys\n"
        "path, _, repo, _, branch, _, ref = sys.argv[1:]\n"
        "subprocess.run(['git', '-C', repo, 'worktree', 'add', '-b', branch, path, ref], check=True)\n"
        "(pathlib.Path(path) / 'AGENTS.override.md').write_text('PERSONAL_RULE_MARKER')\n"
    )
    sessions = SessionManager(workspace, sessions_root=tmp_path / "sessions")
    manager = SubagentManager(
        workspace=workspace, bus=MessageBus(), max_tool_result_chars=16_000,
        consolidator=MagicMock(spec=Consolidator), session_manager=sessions,
        tools_config=ToolsConfig(subagent=SubagentToolConfig(projects={PROJECT: SubagentProjectConfig(
            repository=repo, worktree_script=script, feishu_session=OWNER,
        )})),
        max_concurrent_subagents=2,
    )
    provider = MagicMock(spec=LLMProvider)
    provider.generation = GenerationSettings()
    runtime = LLMRuntime.capture(provider, "test", context_window_tokens=128_000)
    parent = await sessions.state.get(OWNER)
    parent.add_message("user", "Start project tasks")
    await sessions.state.finish_turn(parent)
    tool = SubagentTool(manager)
    ready = asyncio.Queue()
    release = asyncio.Event()

    async def execute(spec):
        prompt = spec.initial_messages[0]["content"]
        scope = current_workspace_scope()
        assert scope is not None and scope.restrict_to_workspace is restricted
        assert scope.project_path == spec.workspace
        assert prompt.count("PERSONAL_RULE_MARKER") == 1
        assert "BASE_RULE_MARKER" not in prompt
        write = spec.tools.get("write_file")
        assert write is not None
        await write.execute(path="worker.txt", content="edited")
        read = spec.tools.get("read_file")
        assert read is not None
        report = await read.execute(path=str(workspace / "previous-report.txt"))
        if restricted:
            assert "outside" in str(report)
        else:
            assert "HOST_REPORT_MARKER" in str(report)
        await ready.put(scope.project_path)
        await release.wait()
        return AgentRunResult(messages=[], final_content="Verified project changes")

    manager.runner.run = execute

    async def start(label, owner=OWNER, **kwargs):
        token = bind_workspace_scope(default_workspace_scope(workspace, restricted))
        try:
            with request_context(RequestContext(
                channel="feishu", chat_id=owner.split(":")[1], session_key=owner,
                runtime=runtime, metadata={"chat_type": "p2p"},
            )):
                return await tool.execute(action="create", task="edit a file", label=label, project=PROJECT, **kwargs)
        finally:
            reset_workspace_scope(token)

    try:
        assert "unavailable" in await start("FOREIGN", owner="feishu:ou_other")
        first = await start("LOC-1")
        second = await start("LOC-2")
        first_id, = re.findall(r"id: ([a-f0-9-]+)", first)
        second_id, = re.findall(r"id: ([a-f0-9-]+)", second)
        paths = {await asyncio.wait_for(ready.get(), 15), await asyncio.wait_for(ready.get(), 15)}
        assert len(paths) == 2
        assert all((path / "worker.txt").read_text() == "edited" for path in paths)
        assert not (repo / "worker.txt").exists()
        assert "finished project task" in await start("LOC-1", resume_task_id=first_id)
        release.set()
        completed_ids = set()
        for _ in range(2):
            event = await asyncio.wait_for(manager.bus.consume_inbound(), 10)
            assert event.session_key == OWNER
            assert "Verified project changes" in event.content
            assert "Worktree:" in event.content
            completed_ids.add(event.metadata["subagent_task_id"])
        assert completed_ids == {first_id, second_id}
        saved = manager.check(first_id, OWNER)
        assert saved.state == "done" and saved.worktree

        resumed = await start("LOC-1", resume_task_id=first_id)
        resumed_id, = re.findall(r"id: ([a-f0-9-]+)", resumed)
        assert resumed_id != first_id
        assert await asyncio.wait_for(ready.get(), 15) == Path(saved.worktree)
        event = await asyncio.wait_for(manager.bus.consume_inbound(), 10)
        assert event.metadata["subagent_task_id"] == resumed_id
    finally:
        release.set()
        await manager.close()


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE).strip()


def _commit(repo, name):
    _git(repo, "add", "--all")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def retention_project(tmp_path):
    if shutil.which("git") is None or shutil.which("python3") is None:
        pytest.skip("requires Git and python3")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text(".scratch/\nAGENTS.override.md\n")
    (repo / "tracked.txt").write_text("original")
    _commit(repo, "initial")
    script = tmp_path / "setup.py"
    script.write_text(
        "import pathlib, subprocess, sys\n"
        "path, _, repo, _, branch, _, ref = sys.argv[1:]\n"
        "subprocess.run(['git', '-C', repo, 'worktree', 'add', '-b', branch, path, ref], check=True)\n"
        "(pathlib.Path(path) / 'AGENTS.override.md').write_text('trusted setup')\n"
    )
    return SubagentProjectConfig(repository=repo, worktree_script=script, feishu_session=OWNER), tmp_path / "agent"


def _completed(worktree_id, path, branch, timestamp):
    return SubagentStatus(
        task_id=worktree_id, label="project task", task_description="work",
        started_at=1.0, finished_at=2.0, created_at=timestamp - 1, completed_at=timestamp,
        owner=OWNER, state="done", project=PROJECT, worktree_id=worktree_id,
        worktree=str(path), branch=branch,
    )


@pytest.mark.asyncio
async def test_reclaim_and_resume_preserve_branch_and_local_artifacts(retention_project):
    config, workspace = retention_project
    now = 2_000_000.0
    statuses = {}
    for name in ("clean", "dirty", "artifact", "active", "changed-setup", "assume-unchanged"):
        worktree_id = str(uuid4())
        tree = await prepare_project_worktree(config, workspace, worktree_id, resume=False)
        statuses[name] = _completed(worktree_id, tree.path, tree.branch, now - 8 * 24 * 60 * 60)

    clean = statuses["clean"]
    clean_path = Path(clean.worktree)
    (clean_path / "tracked.txt").write_text("committed task result")
    commit = _commit(clean_path, "task result")
    dirty = Path(statuses["dirty"].worktree)
    (dirty / "tracked.txt").write_text("uncommitted result")
    artifact = Path(statuses["artifact"].worktree) / ".scratch/report.md"
    artifact.parent.mkdir()
    artifact.write_text("analysis result")
    changed_setup = Path(statuses["changed-setup"].worktree) / "AGENTS.override.md"
    changed_setup.write_text("updated local rules")
    hidden_change = Path(statuses["assume-unchanged"].worktree)
    _git(hidden_change, "update-index", "--assume-unchanged", "tracked.txt")
    (hidden_change / "tracked.txt").write_text("uncommitted change hidden from git status")
    active = statuses["active"]

    removed = await reclaim_project_worktrees(
        {PROJECT: config}, workspace, statuses.values(), lambda worktree_id: worktree_id == active.worktree_id,
        now=now,
    )
    assert removed == [clean.worktree_id]
    assert not clean_path.exists()
    assert _git(config.repository, "rev-parse", f"refs/heads/{clean.branch}") == commit
    assert (dirty / "tracked.txt").read_text() == "uncommitted result"
    assert artifact.read_text() == "analysis result"
    assert changed_setup.read_text() == "updated local rules"
    assert (hidden_change / "tracked.txt").read_text() == "uncommitted change hidden from git status"
    assert Path(active.worktree).is_dir()

    resumed = await prepare_project_worktree(config, workspace, clean.worktree_id, resume=True)
    assert resumed.path == clean_path
    assert resumed.branch == clean.branch
    assert _git(clean_path, "rev-parse", "HEAD") == commit
    assert (clean_path / "tracked.txt").read_text() == "committed task result"
    assert (clean_path / "AGENTS.override.md").read_text() == "trusted setup"


@pytest.mark.asyncio
async def test_reclaim_keeps_ten_recent_clean_checkouts_and_expires_old_ones(retention_project):
    config, workspace = retention_project
    now = time.time()
    statuses = []
    # Checkouts created before retention was introduced have no setup snapshot.
    for age in (7 * 24 * 60 * 60, *range(11, 0, -1)):
        worktree_id = str(uuid4())
        path = (workspace / "location-tasks" / f"native-{worktree_id}" / "worktree").resolve()
        branch = f"codex/location-{worktree_id}"
        _git(config.repository, "worktree", "add", "-b", branch, str(path))
        statuses.append(_completed(worktree_id, path, branch, now - age))

    sessions = SessionManager(workspace, sessions_root=workspace.parent / "sessions")
    manager = SubagentManager(
        workspace=workspace, bus=MessageBus(), max_tool_result_chars=16_000,
        consolidator=MagicMock(spec=Consolidator), session_manager=sessions,
        tools_config=ToolsConfig(subagent=SubagentToolConfig(projects={PROJECT: config})),
    )
    parent = await sessions.state.get(OWNER)
    parent.add_message("user", "Run a project task")
    await sessions.state.finish_turn(parent)
    for status in statuses:
        await manager.sessions.create(status)

    async def wait_for_removal(removed):
        async with asyncio.timeout(15):
            while any(Path(status.worktree).exists() for status in removed):
                await asyncio.sleep(0.025)

    try:
        await manager.recover_interrupted()
        await wait_for_removal(statuses[:2])
        assert all(Path(status.worktree).is_dir() for status in statuses[2:])

        provider = MagicMock(spec=LLMProvider)
        provider.generation = GenerationSettings()
        runtime = LLMRuntime.capture(provider, "test", context_window_tokens=128_000)

        async def execute(_spec):
            return AgentRunResult(messages=[], final_content="Finished")

        manager.runner.run = execute
        result = await manager.spawn(
            "Run a clean task", origin_channel="feishu", origin_chat_id="ou_owner",
            session_key=OWNER, runtime=runtime, project=PROJECT,
        )
        task_id, = re.findall(r"id: ([a-f0-9-]+)", result)
        await asyncio.wait_for(manager.bus.consume_inbound(), 15)
        await wait_for_removal(statuses[2:3])
        saved = manager.check(task_id, OWNER)
        assert Path(saved.worktree).is_dir()
        assert all(Path(status.worktree).is_dir() for status in statuses[3:])
        assert all(
            _git(config.repository, "show-ref", "--verify", f"refs/heads/{status.branch}")
            for status in statuses
        )

        previous = statuses[0]
        result = await manager.spawn(
            "Continue the reclaimed task", origin_channel="feishu", origin_chat_id="ou_owner",
            session_key=OWNER, runtime=runtime, project=PROJECT, resume_task_id=previous.task_id,
        )
        resumed_id, = re.findall(r"id: ([a-f0-9-]+)", result)
        event = await asyncio.wait_for(manager.bus.consume_inbound(), 15)
        assert event.metadata["subagent_task_id"] == resumed_id
        resumed = manager.check(resumed_id, OWNER)
        assert (resumed.state, resumed.branch, resumed.worktree) == ("done", previous.branch, previous.worktree)
        assert _git(Path(resumed.worktree), "symbolic-ref", "--short", "HEAD") == previous.branch
    finally:
        await manager.close()
        await sessions.state.aclose()
