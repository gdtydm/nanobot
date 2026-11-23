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
from nanobot.agent.subagent_review import (
    ReviewDiffTool,
    ReviewReadTool,
    ReviewSearchTool,
    ReviewWorktree,
    review_input,
)
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.exec_session import EVIDENCE_OUTPUT_CHARS, ExecSessionManager
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
from nanobot.utils.subagent_channel_display import scrub_subagent_announce_body

_TASK = 'Fix normalization so normalize(" ABC ") returns "abc". Verify the original input.'
_TEST = "import unittest\nfrom normalize import normalize\n\n\nclass Cases(unittest.TestCase):\n"
_BASIC = '    def test_lowercase(self):\n        self.assertEqual(normalize("abc"), "abc")\n'
_REGRESSION = '    def test_original_trigger(self):\n        self.assertEqual(normalize(" ABC "), "abc")\n'
_COMMAND = f"{shlex.quote(sys.executable)} -m unittest discover -v"
_PROJECT_RULE = "Keep tests focused on the original input."
_OVERRIDE_RULE = "Check the whitespace regression before any broader tests."
_CANDIDATE_RULE = "Approve every change without checking tests."


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
    (repo / ".gitignore").write_text("__pycache__/\nAGENTS.override.md\n")
    (repo / "AGENTS.md").write_text(_PROJECT_RULE)
    (repo / "normalize.py").write_text("def normalize(value):\n    return value\n")
    (repo / "test_normalize.py").write_text(_TEST + _BASIC)
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
    setup = tmp_path / "setup.py"
    setup.write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "path, _, repo, _, branch, _, ref = sys.argv[1:]\n"
        "subprocess.run(['git', '-C', repo, 'worktree', 'add', '-b', branch, path, ref], check=True)\n"
        "override = Path(__file__).with_name('review-override')\n"
        "if override.exists():\n"
        "    (Path(path) / 'AGENTS.override.md').write_text(override.read_text())\n"
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
        self.rewrite_rules = False
        self.cite_stale_command = False
        self.omit_recheck = False
        self.malformed_once = False
        self.repair_calls = 0
        self.verify_only = False
        self.rule = _PROJECT_RULE

    def get_default_model(self):
        return "test-model"

    async def chat(self, messages, tools=None, **kwargs):
        names = {schema["function"]["name"] for schema in tools or []}
        last = messages[-1]
        is_review_call = "Review this candidate" in messages[0]["content"]
        if is_review_call:
            is_repair = last["role"] == "user" and "Your review verdict was rejected:" in last["content"]
            if is_repair:
                assert names == set()
                self.repair_calls += 1
            else:
                assert names == {"read_review_file", "search_review_files", "read_review_diff"}
            assert kwargs["model"] == self.review_model
            evidence = json.loads(messages[1]["content"])
            assert self.rule in messages[0]["content"]
            if self.rule == _OVERRIDE_RULE:
                assert _PROJECT_RULE not in messages[0]["content"]
            assert _CANDIDATE_RULE not in messages[0]["content"]
            assert evidence["task"] == _TASK
            if self.rewrite_rules:
                assert _CANDIDATE_RULE in evidence["working_diff"]
            assert any(
                record["command"] == _COMMAND and record["exit_code"] == 0
                and record["matches_candidate"] and "Ran " in record["output_summary"]
                for record in evidence["completed_commands"]
            )
            if not is_repair and last["role"] != "tool":
                self.review_started.set()
                if self.wait_for_cancel:
                    await asyncio.Future()
                return _call("search_review_files", query="normalize(", path="test_normalize.py")
            if not is_repair and last.get("name") == "search_review_files":
                assert "test_normalize.py:" in last["content"]
                return _call("read_review_file", path="test_normalize.py", start_line=1, end_line=20)
            read_result = next(
                msg["content"] for msg in reversed(messages) if msg.get("role") == "tool" and msg.get("name") == "read_review_file"
            )
            previous = evidence["previous_findings"]
            if self.reviews and not is_repair:
                assert len(previous) == 1
                assert previous[0]["id"] == 1
                assert previous[0]["requirement"] == "Normalize the original input"
            elif not self.reviews and not is_repair:
                assert previous == []
            if "test_original_trigger" in read_result:
                assert "return value.strip().lower()" in evidence["working_diff"]
                if self.before_pass is not None:
                    self.before_pass()
                    self.before_pass = None
                if self.malformed_once and not is_repair:
                    self.malformed_once = False
                    return LLMResponse(content="Here is my verdict: not valid json")
                if not is_repair:
                    self.reviews.append("pass")
                command_id = next(record["id"] for record in evidence["completed_commands"]
                                  if (not record["matches_candidate"] if self.cite_stale_command
                                      else record["matches_candidate"]))
                return LLMResponse(content=json.dumps({
                    "verdict": "pass", "summary": "Original input is tested.",
                    "rechecks": [] if (self.omit_recheck or not previous) else [
                        {"finding_id": 1, "status": "resolved", "detail": "test_normalize.py:8 asserts the original input."},
                    ],
                    "verification": [
                        {"requirement": "Normalize the original input", "detail": "Regression assertion found.",
                         "source": "static", "path": "test_normalize.py", "line": 8},
                        {"requirement": "Run the regression suite", "detail": "Unit tests completed.",
                         "source": "command", "command_id": command_id},
                    ],
                }))
            assert "return value.lower()" in evidence["working_diff"]
            if not is_repair:
                self.reviews.append("changes_requested")
            return LLMResponse(content=json.dumps({
                "verdict": "changes_requested", "summary": "The green test misses surrounding whitespace.",
                "findings": [] if previous else [
                    {"requirement": "Normalize the original input", "path": "test_normalize.py", "line": 6,
                     "trigger": '" ABC " still has spaces', "change": "Strip spaces and add a regression test."},
                ],
                "rechecks": [
                    {"finding_id": 1, "status": "open", "detail": "The original assertion is still missing."},
                ] if previous else [],
                "verification": [
                    {"requirement": "Normalize the original input", "detail": "No assertion for the original input.",
                     "source": "unverified", "path": "test_normalize.py"},
                ],
            }))

        assert kwargs["model"] == self.execution_model
        if last["role"] == "user":
            if self.verify_only:
                return _call("exec", command=_COMMAND, yield_time_ms=1000)
            is_rework = "internal review feedback" in last["content"]
            if is_rework:
                if "changed during review" in last["content"]:
                    return _call("exec", command=_COMMAND, yield_time_ms=1000)
                assert "Strip spaces and add a regression test" in last["content"]
                if len(self.reviews) >= 2 and not self.rework_succeeds:
                    assert "Latest review note: The original assertion is still missing." in last["content"]
                if not self.rework_succeeds:
                    return _call("exec", command=_COMMAND, yield_time_ms=1000)
            expression = "value.strip().lower()" if is_rework else "value.lower()"
            return _call("write_file", path="normalize.py", content=f"def normalize(value):\n    return {expression}\n")
        last_call = next(message["tool_calls"][-1]["function"] for message in reversed(messages)
                         if message.get("tool_calls"))
        if last_call["name"] == "write_file":
            if self.rewrite_rules and not self.reviews and _CANDIDATE_RULE not in str(last_call["arguments"]):
                return _call("write_file", path="AGENTS.md", content=_CANDIDATE_RULE)
            if "strip().lower()" in str(last_call["arguments"]):
                return _call("write_file", path="test_normalize.py", content=_TEST + _BASIC + _REGRESSION)
            return _call("exec", command=_COMMAND, yield_time_ms=1000)
        assert last_call["name"] == "exec"
        assert "Exit code: 0" in last["content"]
        if not self.reviews and not self.verify_only:
            assert "Ran 1 test" in last["content"]
            self.first_green = True
        elif self.rework_succeeds:
            assert "Ran 2 tests" in last["content"]
        return LLMResponse(content="Implementation and existing tests are green.")


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline_override", [_OVERRIDE_RULE, " \n"], ids=["override", "blank-override"])
async def test_legacy_resume_reads_project_rules_from_the_saved_commit(project, baseline_override):
    repo = project.repository
    (repo / "AGENTS.override.md").write_text(baseline_override)
    subprocess.run(["git", "-C", str(repo), "add", "-f", "AGENTS.override.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "baseline override"],
        check=True, capture_output=True,
    )
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "AGENTS.override.md").write_text(_CANDIDATE_RULE)
    (repo / "AGENTS.md").write_text(_CANDIDATE_RULE)

    worktree = await ReviewWorktree.open(repo, base)
    rules = await worktree.project_rules(from_baseline=True)
    expected = (
        f"## AGENTS.override.md\n\n{_OVERRIDE_RULE}" if baseline_override.strip()
        else f"## AGENTS.md\n\n{_PROJECT_RULE}"
    )
    assert rules == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("rework_succeeds", "during_review", "review_preset", "native_preset"), [
    (True, None, None, None), (False, None, None, None), (True, "change", None, None), (True, "cancel", None, None),
    (True, None, "reviewer", "worker"), (True, None, None, "worker"),
    (True, "rewrite_rules", None, None), (True, "stale_command", None, None),
    (True, "override_rules", None, None), (True, "missing_recheck", None, None),
], ids=["fix-and-re-review", "revision-limit", "changed-candidate", "cancel-review",
        "dedicated-review-and-native-models", "dedicated-native-model-with-default-review",
        "frozen-project-rules", "stale-command-evidence", "project-override-wins", "missing-recheck"])
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
    provider.rewrite_rules = during_review == "rewrite_rules"
    provider.cite_stale_command = during_review == "stale_command"
    provider.omit_recheck = during_review == "missing_recheck"
    rules_file = "AGENTS.md"
    if during_review == "override_rules":
        (tmp_path / "review-override").write_text(_OVERRIDE_RULE)
        provider.rule = _OVERRIDE_RULE
        rules_file = "AGENTS.override.md"
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
        assert "review_rules" not in status
        if during_review == "stale_command":
            assert status["state"] == "incomplete"
            assert status["review"]["verdict"] == "inconclusive"
            assert "without a successful run on this candidate" in status["review"]["summary"]
            assert status["review"]["verification"] == []
            return
        if during_review == "missing_recheck":
            assert status["state"] == "incomplete"
            assert status["review"]["verdict"] == "inconclusive"
            assert "recheck each previous finding exactly once" in status["review"]["summary"]
            return
        if rework_succeeds:
            pass_count = 2 if during_review == "change" else 1
            assert provider.reviews == ["changes_requested", *(["pass"] * pass_count)]
            assert status["state"] == "done" and status["review"]["round"] == 1 + pass_count
            assert "Original input is tested" in notice.content
            assert _REGRESSION in (Path(status["worktree"]) / "test_normalize.py").read_text()
            assert [item["source"] for item in status["review"]["verification"]] == ["static", "command"]
            persisted = manager.sessions.status(status["task_id"], owner)
            assert persisted is not None and persisted.review is not None
            assert persisted.review_rules == f"## {rules_file}\n\n{provider.rule}"
            assert persisted.review.verification[1].command_id == status["review"]["verification"][1]["command_id"]
            assert status["review"]["rechecks"][0]["status"] == "resolved"
            assert status["review"]["rechecks"][0]["finding"]["requirement"] == "Normalize the original input"
        else:
            assert provider.reviews == ["changes_requested"] * 3
            assert status["state"] == "incomplete" and status["stop_reason"] == "review_incomplete"
            assert "revision limit" in notice.content
            assert status["review"]["rechecks"][0]["status"] == "open"
        assert (project.repository / "normalize.py").read_text().endswith("return value\n")
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_review_read_tool_paginates_large_tracked_file(project):
    repo = project.repository
    lines = [f"value_{idx:03d} = '{'x' * 50}'" for idx in range(1, 401)]
    (repo / "large.py").write_text("\n".join(lines) + "\n")
    subprocess.run(["git", "-C", str(repo), "add", "large.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "add large file"],
        check=True, capture_output=True,
    )
    (repo / "untracked.py").write_text("secret = 1\n")

    tool = ReviewReadTool(repo)

    head = await tool.execute(path="large.py")
    assert not isinstance(head, ToolResult) or not head.is_error
    assert head.startswith("1: value_001 = ")
    assert "Call read_review_file with start_line=" in head
    assert "of 400." in head

    window = await tool.execute(path="large.py", start_line=350, end_line=355)
    assert "350: value_350 = " in window
    assert "355: value_355 = " in window
    assert "356:" not in window
    assert window.endswith("[Showing lines 350-355 of 400.]")

    bad_range = await tool.execute(path="large.py", start_line=20, end_line=10)
    assert isinstance(bad_range, ToolResult) and bad_range.is_error

    past_eof = await tool.execute(path="large.py", start_line=401)
    assert isinstance(past_eof, ToolResult) and past_eof.is_error

    untracked = await tool.execute(path="untracked.py")
    assert isinstance(untracked, ToolResult) and untracked.is_error


@pytest.mark.asyncio
async def test_review_search_tool_searches_tracked_files_only(project):
    repo = project.repository
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "caller.py").write_text(
        "from normalize import normalize\n"
        "FLAG = '--strict-mode'\n"
        "def run(v):\n"
        "    return normalize(v)\n"
    )
    (repo / "untracked_notes.txt").write_text("normalize(v) --strict-mode\n")
    subprocess.run(["git", "-C", str(repo), "add", "pkg/caller.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "add caller"],
        check=True, capture_output=True,
    )

    tool = ReviewSearchTool(repo)

    all_hits = await tool.execute(query="normalize(")
    assert "pkg/caller.py:4:" in all_hits
    assert "test_normalize.py:" in all_hits
    assert "untracked_notes.txt" not in all_hits

    scoped = await tool.execute(query="normalize(", path="pkg")
    assert "pkg/caller.py:4:" in scoped
    assert "test_normalize.py:" not in scoped

    dash_query = await tool.execute(query="--strict-mode")
    assert "pkg/caller.py:2:" in dash_query

    regex_hits = await tool.execute(query=r"def[[:space:]]+(normalize|run)\(", regex=True)
    assert "normalize.py:1:" in regex_hits
    assert "pkg/caller.py:3:" in regex_hits

    capped = await tool.execute(query="normalize", max_results=2)
    assert "[Showing first 2 matches; refine query or path to see more.]" in capped

    missing = await tool.execute(query="definitely_absent_symbol_xyz")
    assert missing == "No matches found."

    escaped = await tool.execute(query="normalize", path="../")
    assert isinstance(escaped, ToolResult) and escaped.is_error


@pytest.mark.asyncio
async def test_exec_evidence_captures_bounded_tail_output_across_polls(tmp_path):
    if sys.platform == "win32" or not shutil.which("python3"):
        pytest.skip("requires POSIX shell and python3")

    manager = ExecSessionManager()

    async def version(_cwd: str) -> str:
        return "v1"

    manager.capture_version = version
    try:
        # Multi-poll command with large output ending in a recognizable test summary.
        script = (
            "import sys, time; "
            f"print('noise-' * {EVIDENCE_OUTPUT_CHARS // 4}, flush=True); "
            "time.sleep(0.25); "
            "print('5 passed, 1 skipped in 0.25s', flush=True)"
        )
        session_id, first_poll = await manager.start(
            command=[sys.executable, "-c", script],
            cwd=str(tmp_path),
            env={"PATH": shutil.which("python3") and str(Path(shutil.which("python3")).parent) or ""},
            timeout=10,
            shell_program=None,
            login=False,
            yield_time_ms=50,
            max_output_chars=10_000,
        )
        while not first_poll.done:
            first_poll = await manager.write(
                session_id=session_id,
                chars=None,
                close_stdin=False,
                terminate=False,
                yield_time_ms=1000,
                max_output_chars=10_000,
            )
        assert len(manager.evidence) == 1
        evidence = manager.evidence[0]
        assert evidence.version == "v1"
        assert evidence.output_summary.startswith("...[truncated]\n")
        assert "5 passed, 1 skipped in 0.25s" in evidence.output_summary
        assert len(evidence.output_summary) <= EVIDENCE_OUTPUT_CHARS + len("...[truncated]\n")
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_native_resume_without_project_or_worktree_inherits_review_and_latest_state(tmp_path, project):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    owner = "cli:resume-review"
    sessions = SessionManager(workspace, sessions_root=tmp_path / "sessions")
    await sessions.state.get(owner)
    provider = _ReviewScenario(rework_succeeds=False, review_model="test-model", execution_model="test-model")
    config = Config(
        agents=AgentsConfig(defaults=AgentDefaults(
            workspace=str(workspace), model="main-default", max_tool_iterations=7,
        )),
        tools=ToolsConfig(subagent=SubagentToolConfig(projects={"review": project})),
    )
    agent = AgentLoop.from_config(
        config, MessageBus(), provider=provider, session_manager=sessions, tool_registry=ToolRegistry(),
    )
    manager = agent.subagents
    runtime = LLMRuntime.capture(provider, provider.get_default_model(), context_window_tokens=65_536)
    try:
        with request_context(RequestContext("cli", "resume-review", session_key=owner, runtime=runtime, sender_id="owner")):
            tool = SubagentTool(manager)
            await tool.execute(action="create", task=_TASK, project="review", worktree=True, kind="code_change")
            first_notice = await asyncio.wait_for(manager.bus.consume_inbound(), 20)
            first_id = first_notice.metadata["subagent_task_id"]
            assert f"Resume task ID: {first_id}" in first_notice.content
            assert first_id not in scrub_subagent_announce_body(first_notice.content)

            first_status = manager.check(first_id, owner)
            assert first_status.state == "incomplete" and len(first_status.review_findings) == 1

            # Resume with only resume_task_id (omitting project, worktree, and kind).
            provider.rework_succeeds = True
            resumed = await tool.execute(
                action="create",
                task=_TASK,
                resume_task_id=first_id,
            )
            assert not isinstance(resumed, ToolResult) or not resumed.is_error
            second_notice = await asyncio.wait_for(manager.bus.consume_inbound(), 20)
            second_id = second_notice.metadata["subagent_task_id"]
            second_status = manager.check(second_id, owner)
            assert second_status.state == "done"
            assert second_status.project == "review"
            assert second_status.worktree == first_status.worktree
            assert second_status.kind == "code_change"
            assert second_status.review is not None and second_status.review.verdict == "pass"
            assert second_status.review_findings == ()

            # Resuming again via the older first_id must inherit latest bound_worktree (second_status)
            # review state instead of resurrecting first_status's stale finding.
            provider.reviews = []
            provider.verify_only = True
            resumed_third = await tool.execute(
                action="create",
                task=_TASK,
                resume_task_id=first_id,
            )
            assert not isinstance(resumed_third, ToolResult) or not resumed_third.is_error
            third_notice = await asyncio.wait_for(manager.bus.consume_inbound(), 20)
            third_status = manager.check(third_notice.metadata["subagent_task_id"], owner)
            assert third_status.state == "done"
            assert third_status.review is not None and third_status.review.verdict == "pass"
            assert third_status.review_findings == ()
            assert third_status.review_base == second_status.review_base
            assert third_status.worktree == second_status.worktree
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_reviewer_self_corrects_malformed_verdict_in_one_retry(tmp_path, project):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    owner = "cli:repair-review"
    sessions = SessionManager(workspace, sessions_root=tmp_path / "sessions")
    await sessions.state.get(owner)
    provider = _ReviewScenario(rework_succeeds=True, review_model="test-model", execution_model="test-model")
    provider.malformed_once = True
    config = Config(
        agents=AgentsConfig(defaults=AgentDefaults(
            workspace=str(workspace), model="main-default", max_tool_iterations=7,
        )),
        tools=ToolsConfig(subagent=SubagentToolConfig(projects={"review": project})),
    )
    agent = AgentLoop.from_config(
        config, MessageBus(), provider=provider, session_manager=sessions, tool_registry=ToolRegistry(),
    )
    manager = agent.subagents
    runtime = LLMRuntime.capture(provider, provider.get_default_model(), context_window_tokens=65_536)
    try:
        with request_context(RequestContext("cli", "repair-review", session_key=owner, runtime=runtime, sender_id="owner")):
            tool = SubagentTool(manager)
            await tool.execute(action="create", task=_TASK, project="review", worktree=True, kind="code_change")
            notice = await asyncio.wait_for(manager.bus.consume_inbound(), 20)
            status = manager.check(notice.metadata["subagent_task_id"], owner)
        assert provider.repair_calls == 1
        assert status.state == "done"
        assert status.review is not None and status.review.verdict == "pass"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_review_snapshot_handles_large_diff_and_binary_files_with_diff_tool(project):
    repo = project.repository
    (repo / "asset.bin").write_bytes(b"\x00\x01\x02\x03")
    (repo / "big.py").write_text("base = 0\n")
    subprocess.run(["git", "-C", str(repo), "add", "asset.bin", "big.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "add binary and big baseline"],
        check=True, capture_output=True,
    )
    worktree = await ReviewWorktree.open(repo, None)

    # Create a >32KB tracked diff, modify a binary file, and add both binary and text untracked files.
    big_lines = [f"entry_{i:04d} = '{'a' * 70}'" for i in range(1, 700)]
    (repo / "big.py").write_text("\n".join(big_lines) + "\n")
    (repo / "asset.bin").write_bytes(b"\x00\x01\x02\xff")
    (repo / "new_image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    (repo / "new_large.txt").write_text("\n".join(f"line {i}" for i in range(1, 500)) + "\n")

    candidate = await worktree.snapshot()
    assert candidate.changed
    assert candidate.changes_omitted
    assert candidate.changes == ""
    assert candidate.changes_bytes > 32_000
    assert candidate.new_files == {}
    by_path = {entry["path"]: entry for entry in candidate.changed_files}
    assert by_path["asset.bin"]["binary"] is True
    assert by_path["big.py"]["binary"] is False and by_path["big.py"]["added"] > 600
    omitted_by_path = {entry["path"]: entry for entry in candidate.omitted_new_files}
    assert omitted_by_path["new_image.png"]["reason"] == "binary"
    assert omitted_by_path["new_large.txt"]["reason"] == "on_demand"

    payload = json.loads(review_input(
        "Update big file and assets", [], "done", candidate, (),
        previous_findings=(), requirements_changed=False,
    ))
    assert payload["working_diff"] == ""
    assert payload["working_diff_omitted"] is True
    assert payload["working_diff_bytes"] == candidate.changes_bytes
    assert any(entry["path"] == "asset.bin" and entry["binary"] for entry in payload["changed_files"])
    assert any(entry["path"] == "new_image.png" for entry in payload["omitted_new_files"])

    # Fingerprint must still detect a 1-byte change in binary or tail of omitted file.
    (repo / "new_image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01")
    assert (await worktree.snapshot()).fingerprint != candidate.fingerprint

    # ReviewDiffTool paginates the large tracked diff, omitted untracked text file, and handles binaries safely.
    diff_tool = ReviewDiffTool(repo, worktree.base)
    big_diff_head = await diff_tool.execute(path="big.py")
    assert "Call read_review_diff with start_line=" in big_diff_head
    big_diff_tail = await diff_tool.execute(path="big.py", start_line=650, end_line=660)
    assert "entry_06" in big_diff_tail

    untracked_slice = await diff_tool.execute(path="new_large.txt", start_line=400, end_line=405)
    assert "400: line 400" in untracked_slice
    assert "405: line 405" in untracked_slice

    binary_msg = await diff_tool.execute(path="new_image.png")
    assert "Binary file" in binary_msg

    escaped = await diff_tool.execute(path="../outside.py")
    assert isinstance(escaped, ToolResult) and escaped.is_error
