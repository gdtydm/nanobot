from pathlib import Path

from nanobot.context_sources import record_source
from nanobot.security.workspace_policy import resolve_allowed_path
from nanobot.utils.helpers import load_bundled_template


def load_project_instructions(root: Path) -> str:
    """Load the effective instructions for one workspace directory."""
    for filename in ("AGENTS.override.md", "AGENTS.md"):
        path = root / filename
        try:
            content = resolve_allowed_path(path, allowed_root=root).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        template = load_bundled_template(filename) if filename == "AGENTS.md" else None
        if not content.strip() or (template is not None and content.strip() == template.strip()):
            record_source(path, "", loaded=False)
            continue
        record_source(path, content)
        return f"## {filename}\n\n{content}"
    return ""
