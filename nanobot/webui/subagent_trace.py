"""Read-only diagnostics for a verified child session."""

from __future__ import annotations

import base64
import json
import zlib
from collections.abc import Iterator
from typing import Any, cast

from nanobot.agent.subagent_trace import MAX_REQUEST_BYTES
from nanobot.session.display_history import HistoryReader
from nanobot.session.manager import Session
from nanobot.webui.outbound_wire import project_tool_events

_MAX_TOOL_DETAIL_CHARS = 256 * 1024


def tool_calls(child: Session) -> Iterator[dict[str, Any]]:
    for message in child.messages:
        events: object = message.get("tool_events")
        if isinstance(events, list):
            for event in cast(list[object], events):
                if isinstance(event, dict) and isinstance(event.get("name"), str):
                    yield cast(dict[str, Any], event)


def trace_index(child: Session, history: HistoryReader) -> dict[str, Any]:
    requests: list[dict[str, Any]] = []
    sources: list[str] = []
    for index, fact in history.model_request_inputs():
        if not requests:
            sources = fact.content["sources"]
        requests.append({
            "index": index, "id": fact.content["request_id"], "model": fact.content["model"],
            "transport": fact.content["transport"],
            "created_at_ms": fact.created_at_ms, "truncated": fact.content["truncated"],
        })
    tools: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls(child)):
        raw: object = call.get("arguments")
        arguments = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
        target = next((value for key in ("path", "command", "session_id", "url", "query")
                       if isinstance(value := arguments.get(key), str)), "")
        tools.append({
            "index": index, "id": call.get("call_id") or "",
            "name": call["name"], "phase": call.get("phase") or "",
            "target": target[:512],
        })
    return {"sources": sources, "requests": requests, "tools": tools}


def request_detail(history: HistoryReader, index: int) -> dict[str, Any] | None:
    fact = history.model_request_input(index)
    if fact is None:
        return None
    encoded = base64.b64decode(fact.content["body_zlib"], validate=True)
    body = zlib.decompressobj().decompress(encoded, MAX_REQUEST_BYTES + 1)
    return {
        "body": body[:MAX_REQUEST_BYTES].decode("utf-8", errors="ignore"),
        "truncated": fact.content["truncated"] or len(body) > MAX_REQUEST_BYTES,
    }


def tool_detail(child: Session, index: int, call_id: str) -> dict[str, Any] | None:
    call = next((call for number, call in enumerate(tool_calls(child)) if number == index), None)
    if call is None or (call.get("call_id") or "") != call_id:
        return None
    body = json.dumps(project_tool_events([call])[0], indent=2, ensure_ascii=False)
    body = body.encode("utf-8", errors="backslashreplace").decode("utf-8")
    return {"body": body[:_MAX_TOOL_DETAIL_CHARS], "truncated": len(body) > _MAX_TOOL_DETAIL_CHARS}
