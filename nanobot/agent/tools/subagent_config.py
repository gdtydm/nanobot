from pathlib import Path
from typing import Annotated

from pydantic import Field, StringConstraints

from nanobot.config_base import Base

ProjectName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
FeishuPrivateSession = Annotated[str, StringConstraints(
    pattern=r"^feishu(?:\.[a-zA-Z0-9_-]+)?:ou_[a-zA-Z0-9_-]+$",
)]


class SubagentProjectConfig(Base):
    """Local gateway policy for a project worker and its Feishu owner."""

    repository: Path
    worktree_script: Path | None = None
    feishu_session: FeishuPrivateSession


class SubagentToolConfig(Base):
    projects: dict[ProjectName, SubagentProjectConfig] = Field(default_factory=dict)
