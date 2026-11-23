from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints

from nanobot.config_base import Base

ProjectName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]


def normalize_subagent_model_preset(value: str | None) -> str | None:
    name = value.strip() if value is not None else ""
    return None if name in {"", "default"} else name


SubagentModelPreset = Annotated[str | None, AfterValidator(normalize_subagent_model_preset)]


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


class NativeReviewConfig(Base):
    enabled: bool = True
    model_preset: SubagentModelPreset = None


class SubagentToolConfig(Base):
    projects: dict[ProjectName, SubagentProjectConfig] = Field(default_factory=dict)
    native_model_preset: SubagentModelPreset = None
    codex: CodexTaskConfig = Field(default_factory=CodexTaskConfig)
    native_review: NativeReviewConfig = Field(default_factory=NativeReviewConfig)
