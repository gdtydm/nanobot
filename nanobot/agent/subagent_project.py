from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from loguru import logger

from nanobot.agent.subagent_status import SubagentStatus
from nanobot.agent.tools.subagent_config import SubagentProjectConfig
from nanobot.execution.processes import spawn_process_tree
from nanobot.utils.cancellation import shield_and_drain

_RETENTION_SECONDS = 7 * 24 * 60 * 60
_RETAIN_COMPLETED = 10


@dataclass(frozen=True, slots=True)
class ProjectWorktree:
    path: Path
    branch: str


async def _run(command: list[str], cwd: Path) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    tree = await spawn_process_tree(command, str(cwd), env)
    try:
        async with asyncio.timeout(120):
            stdout, stderr = await tree.process.communicate()
    except BaseException:
        await shield_and_drain(tree.terminate())
        raise
    tree.finish()
    if tree.process.returncode:
        detail = stderr.decode(errors="replace").strip()[-2000:]
        raise RuntimeError(f"Project worktree Git or setup command failed: {detail or 'command failed'}")
    return os.fsdecode(stdout)


def _checkout(agent_workspace: Path, worktree_id: str) -> ProjectWorktree:
    worktree_id = str(UUID(worktree_id))
    path = agent_workspace.resolve() / "location-tasks" / f"native-{worktree_id}" / "worktree"
    if path.resolve() != path:
        raise RuntimeError("Project task directory must not contain symlinks")
    return ProjectWorktree(path, f"codex/location-{worktree_id}")


async def _verify_checkout(repository: Path, worktree: ProjectWorktree) -> None:
    path = worktree.path
    if not path.is_dir() or path.resolve() != path:
        raise RuntimeError("Project worktree is unavailable")
    git_common = ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]
    common_dir = Path((await _run(git_common, repository)).strip()).resolve(strict=True)
    if (
        Path((await _run(git_common, path)).strip()).resolve(strict=True) != common_dir
        or Path((await _run(["git", "rev-parse", "--show-toplevel"], path)).strip()).resolve() != path
        or (await _run(["git", "symbolic-ref", "--short", "HEAD"], path)).strip() != worktree.branch
    ):
        raise RuntimeError("Project worktree no longer matches its repository and branch")


async def _untracked(path: Path, *, ignored_only: bool = False) -> list[str]:
    command = ["git", "ls-files", "--others", "-z"]
    if ignored_only:
        command.extend(("--ignored", "--exclude-standard"))
    output = await _run(command, path)
    return [name for name in output.split("\0") if name]


def _setup_file(root: Path, name: str) -> Path:
    path = root / name
    if not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError("Project worktree setup path must not escape its directory or use symlinks")
    return path


def _save_setup(worktree: Path, names: list[str]) -> None:
    """Keep trusted setup files outside the checkout so a branch can be reopened."""
    with tempfile.TemporaryDirectory(prefix=".setup-", dir=worktree.parent) as directory:
        staging = Path(directory) / "files"
        staging.mkdir()
        for name in names:
            source = _setup_file(worktree, name)
            if not stat.S_ISREG(source.stat().st_mode):
                raise ValueError("Project worktree setup contains a non-regular file")
            target = _setup_file(staging, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        staging.rename(worktree.parent / "worktree-setup")


def _setup_names(setup: Path) -> list[str]:
    if not setup.exists():
        return []
    if not setup.is_dir() or setup.resolve() != setup:
        raise ValueError("Invalid project worktree setup snapshot")
    names = []
    for path in setup.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Invalid project worktree setup file")
        if path.is_file():
            names.append(str(path.relative_to(setup)))
    return names


def _restore_setup(worktree: Path) -> None:
    setup = worktree.parent / "worktree-setup"
    for name in _setup_names(setup):
        source = _setup_file(setup, name)
        target = _setup_file(worktree, name)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".restore-", dir=target.parent) as directory:
                staging = Path(directory) / target.name
                shutil.copy2(source, staging)
                os.link(staging, target)


def _matches_setup(worktree: Path, names: list[str]) -> bool:
    setup = worktree.parent / "worktree-setup"
    if set(names) != set(_setup_names(setup)):
        return False
    for name in names:
        current = _setup_file(worktree, name)
        saved = _setup_file(setup, name)
        current_stat, saved_stat = current.stat(), saved.stat()
        if (not stat.S_ISREG(current_stat.st_mode) or current_stat.st_size != saved_stat.st_size
                or current_stat.st_mode != saved_stat.st_mode):
            return False
        with current.open("rb") as actual, saved.open("rb") as original:
            if hashlib.file_digest(actual, "sha256").digest() != hashlib.file_digest(original, "sha256").digest():
                return False
    return True


async def prepare_project_worktree(
    config: SubagentProjectConfig,
    agent_workspace: Path,
    worktree_id: str,
    *,
    resume: bool,
) -> ProjectWorktree:
    """Provision or reopen the trusted checkout before exposing it to model tools."""
    repository = config.repository.expanduser().resolve(strict=True)
    worktree = _checkout(agent_workspace, worktree_id)
    path = worktree.path
    restoring = path.parent / ".restoring-worktree"
    if restoring.is_symlink():
        raise RuntimeError("Project worktree recovery marker must not be a symlink")
    if not resume:
        if config.worktree_script is None:
            raise RuntimeError("No worktree setup script is configured for this project")
        script = config.worktree_script.expanduser().resolve(strict=True)
        if not repository.is_dir() or not script.is_file():
            raise RuntimeError("Project repository or worktree script is invalid")
        path.parent.mkdir(parents=True, exist_ok=False)
        await _run([
            "python3", str(script), str(path), "--repo", str(repository), "--branch", worktree.branch, "--ref", "HEAD",
        ], repository)
    elif not path.exists():
        # Verify the original branch exists. Git keeps it when removing a checkout.
        await _run(["git", "show-ref", "--verify", f"refs/heads/{worktree.branch}"], repository)
        restoring.touch(exist_ok=True)
        await _run(["git", "worktree", "add", "--", str(path), worktree.branch], repository)

    await _verify_checkout(repository, worktree)
    if not resume:
        try:
            names = await _untracked(path, ignored_only=True)
            if names:
                await shield_and_drain(asyncio.to_thread(_save_setup, path, names))
        except (OSError, RuntimeError, ValueError):
            logger.warning("Could not save project worktree setup; local files will prevent automatic removal")
    elif restoring.exists():
        await shield_and_drain(asyncio.to_thread(_restore_setup, path))
        restoring.unlink()
    return worktree


async def removable_project_worktree(
    config: SubagentProjectConfig, agent_workspace: Path, worktree_id: str,
) -> ProjectWorktree | None:
    """Return a checkout with clean tracked files and only unchanged setup files."""
    worktree = _checkout(agent_workspace, worktree_id)
    path = worktree.path
    if not path.exists() or (path.parent / ".restoring-worktree").exists():
        return None
    await _verify_checkout(config.repository.expanduser().resolve(strict=True), worktree)
    status = await _run(["git", "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=none"], path)
    if status:
        return None
    # Git status can skip assume-unchanged and sparse/skip-worktree entries.
    index = await _run(["git", "ls-files", "-v", "-z"], path)
    if any(entry and (entry[0].islower() or entry[0] == "S") for entry in index.split("\0")):
        return None
    if not await asyncio.to_thread(_matches_setup, path, await _untracked(path)):
        return None
    return worktree


async def remove_project_worktree(config: SubagentProjectConfig, worktree: ProjectWorktree) -> None:
    repository = config.repository.expanduser().resolve(strict=True)
    await _run(["git", "worktree", "remove", "--", str(worktree.path)], repository)


async def reclaim_project_worktrees(
    projects: Mapping[str, SubagentProjectConfig],
    workspace: Path,
    statuses: Iterable[SubagentStatus],
    in_use: Callable[[str], bool],
    *,
    now: float,
) -> list[str]:
    """Apply retention only to verified checkouts of successfully completed tasks."""
    latest: dict[str, SubagentStatus] = {}
    active: set[str] = set()
    for status in sorted(statuses, key=lambda status: status.created_at, reverse=True):
        if status.worktree_id is None:
            continue
        if status.finished_at is None:
            active.add(status.worktree_id)
        latest.setdefault(status.worktree_id, status)

    kept = 0
    reclaimed: list[str] = []
    for worktree_id, status in sorted(
        latest.items(), key=lambda item: item[1].completed_at or 0, reverse=True,
    ):
        config = projects.get(status.project) if status.project is not None else None
        if (
            config is None or not config.allows(status.owner) or status.state != "done"
            or status.completed_at is None or worktree_id in active or in_use(worktree_id)
        ):
            continue
        try:
            expected = _checkout(workspace, worktree_id)
            if status.worktree != str(expected.path) or status.branch != expected.branch:
                continue
            worktree = await removable_project_worktree(config, workspace, worktree_id)
            if worktree is None or in_use(worktree_id):
                continue
            if now - status.completed_at < _RETENTION_SECONDS and kept < _RETAIN_COMPLETED:
                kept += 1
                continue
            await shield_and_drain(asyncio.create_task(remove_project_worktree(config, worktree)))
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("Keeping project worktree {}: {}", worktree_id, exc)
            continue
        reclaimed.append(worktree_id)
        logger.info("Reclaimed completed project worktree {}", worktree.branch)
    return reclaimed
