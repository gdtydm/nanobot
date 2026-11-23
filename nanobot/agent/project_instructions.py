from pathlib import Path

from nanobot.context_sources import record_source
from nanobot.security.workspace_policy import resolve_allowed_path
from nanobot.utils.helpers import load_bundled_template

PROJECT_INSTRUCTION_FILES = ("AGENTS.override.md", "AGENTS.md")


def format_project_instructions(filename: str, content: str) -> str:
    template = load_bundled_template(filename) if filename == "AGENTS.md" else None
    if not content.strip() or (template is not None and content.strip() == template.strip()):
        return ""
    return f"## {filename}\n\n{content}"


def load_project_instructions(root: Path) -> str:
    """Load the effective instructions for one workspace directory."""
    for filename in PROJECT_INSTRUCTION_FILES:
        path = root / filename
        try:
            content = resolve_allowed_path(path, allowed_root=root).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        instructions = format_project_instructions(filename, content)
        if not instructions:
            record_source(path, "", loaded=False)
            continue
        record_source(path, content)
        return instructions
    return ""
