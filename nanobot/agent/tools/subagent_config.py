from pathlib import Path
from typing import Annotated

from pydantic import Field, StringConstraints

from nanobot.config_base import Base

ProjectName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]


class SubagentProjectConfig(Base):
    """Repository and optional worktree preparation for a project worker."""

    repository: Path
    worktree_script: Path | None = None


class CodexTaskConfig(Base):
    enabled: bool = False
    executable: str = "codex"
    model: str | None = None
    effort: str | None = None
    network_access: bool = True


class SubagentToolConfig(Base):
    projects: dict[ProjectName, SubagentProjectConfig] = Field(default_factory=dict)
    codex: CodexTaskConfig = Field(default_factory=CodexTaskConfig)
