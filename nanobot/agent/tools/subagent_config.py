from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, StringConstraints, model_validator

from nanobot.config_base import Base

ProjectName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
SessionOwner = Annotated[str, StringConstraints(pattern=r"^[^\s:]+:[^\s]+$")]


class SubagentProjectConfig(Base):
    """Local gateway policy for a project worker and its owning conversations."""

    repository: Path
    worktree_script: Path | None = None
    sessions: list[SessionOwner] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def read_legacy_owner(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        legacy = value.get("feishu_session", value.get("feishuSession"))
        if legacy is None:
            return value
        sessions = value.get("sessions", [])
        if isinstance(sessions, list):
            return {**value, "sessions": [*sessions, legacy]} if legacy not in sessions else value
        return value

    def allows(self, owner: str) -> bool:
        return owner in self.sessions


class CodexTaskConfig(Base):
    enabled: bool = False
    executable: str = "codex"
    model: str | None = None
    effort: str | None = None
    network_access: bool = False


class SubagentToolConfig(Base):
    projects: dict[ProjectName, SubagentProjectConfig] = Field(default_factory=dict)
    codex: CodexTaskConfig = Field(default_factory=CodexTaskConfig)
