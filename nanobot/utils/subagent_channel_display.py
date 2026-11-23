"""Format subagent notices and progress for human-facing channels.

Persisted subagent announcements mirror ``agent/subagent_announce.md``: header,
full ``Task:`` assignment (model context), ``Result:``, and a trailing model-only
``Summarize…`` instruction. External channels (embedded WebUI, session previews)
should show only the header plus a truncated result body.
"""

from __future__ import annotations

import re

# Cap the Result section so session previews stay readable; full text remains on
# disk for LLM replay.
_SUBAGENT_CHANNEL_RESULT_MAX_CHARS = 800
_TASK_MARKDOWN = re.compile(r"([\\`*_{}\[\]<>#~$])")
TASK_RECEIPT_SUMMARY_LIMIT = 900


def task_display_text(text: str) -> str:
    """Keep task text literal alongside the generated Markdown layout."""
    return _TASK_MARKDOWN.sub(r"\\\1", " ".join(text.split()))


def task_query_block(reference: str) -> str:
    """Give the chat client a single copyable command."""
    return f"```text\n/task {reference}\n```"


def task_receipt(
    backend: str, label: str, reference: str, state: str, *,
    summary: str = "", project: str | None = None, worktree: bool = False,
    base_ref: str | None = None, branch: str | None = None,
) -> str:
    fields = [f"【子任务】{backend}", f"**任务：** {task_display_text(label[:160])}"]
    setup: list[str] = []
    if project:
        setup.append(task_display_text(project[:80]))
    if worktree:
        revision = f"，{task_display_text(branch[:100])}" if branch else (
            f"，基于 {task_display_text((base_ref or 'HEAD')[:100])}"
        )
        setup.append(f"独立 worktree{revision}")
    state_line = f"**当前状态：** {state}"
    if setup:
        state_line += f"（{'，'.join(setup)}）"
    fields.append(state_line)
    if summary := summary.strip()[:TASK_RECEIPT_SUMMARY_LIMIT]:
        # Keep the command fence independent of the optional briefing.
        fields.append(summary.replace("```", "′′′").replace("~~~", "∼∼∼"))
    fields.append(task_query_block(reference))
    return "\n\n".join(fields)


def scrub_subagent_announce_body(content: str) -> str:
    """Return channel-safe text derived from a full subagent announce blob."""
    stripped = content.replace("\r\n", "\n").strip()
    lines = stripped.splitlines()
    header = ""
    if lines and lines[0].startswith("[Subagent"):
        header = lines[0].strip()

    lower = stripped.lower()
    key = "\nresult:\n"
    ri = lower.find(key)
    if ri == -1:
        key = "\nresult:"
        ri = lower.find(key)
    if ri == -1:
        return header if header else stripped

    after = stripped[ri + len(key) :].lstrip()
    summ_marker = "summarize this naturally"
    si = after.lower().find(summ_marker)
    if si != -1:
        after = after[:si].rstrip()

    body = after.strip()
    limit = _SUBAGENT_CHANNEL_RESULT_MAX_CHARS
    if limit and len(body) > limit:
        body = body[: limit - 1].rstrip() + "…"
    if header and body:
        return f"{header}\n\n{body}"
    return header or body or stripped
