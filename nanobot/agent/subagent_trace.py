"""Persist prepared model input separately from the subagent transcript."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import zlib
from collections.abc import Mapping
from typing import Any

from loguru import logger

from nanobot.context_sources import PromptSource
from nanobot.providers.base import ProviderCallContext
from nanobot.session.display_history import HistoryFact, HistoryKind
from nanobot.session.manager import Session
from nanobot.session.sqlite_store import SessionConflictError
from nanobot.session.state import SessionState

MAX_REQUEST_BYTES = 2 * 1024 * 1024


class SubagentRequestTrace:
    def __init__(self, state: SessionState, child: Session, sources: list[PromptSource]) -> None:
        self._state = state
        self._key = child.key
        self._generation = child.generation
        self._sources = list(dict.fromkeys(str(source.path) for source in sources if source.loaded))

    @staticmethod
    def _logical_input(kwargs: Mapping[str, Any]) -> dict[str, Any]:
        data = {key: kwargs[key] for key in (
            "model", "messages", "tools", "temperature", "max_tokens", "reasoning_effort", "retry_mode",
        ) if key in kwargs}
        context: object = kwargs.get("provider_context")
        if isinstance(context, ProviderCallContext):
            continuation = context.conversation_state
            if continuation is not None:
                data["provider_continuation"] = {
                    "kind": continuation.kind, "pending_messages": continuation.pending_messages,
                    "compaction_input_budget": context.compaction_input_budget,
                }
        return data

    def _snapshot(self, request_id: str, transport: str, payload: Mapping[str, Any]) -> HistoryFact:
        data = self._logical_input(payload) if transport == "nanobot" else dict(payload)
        serialized = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        body = serialized.encode("utf-8", errors="backslashreplace")
        return HistoryFact(
            HistoryKind.MODEL_REQUEST_INPUT,
            {
                "request_id": request_id,
                "transport": transport,
                "model": str(data.get("model") or ""),
                "sources": self._sources,
                "body_zlib": base64.b64encode(zlib.compress(body[:MAX_REQUEST_BYTES], level=1)).decode("ascii"),
                "truncated": len(body) > MAX_REQUEST_BYTES,
            },
            created_at_ms=int(time.time() * 1000),
        )

    async def record(self, request_id: str, transport: str, payload: Mapping[str, Any]) -> None:
        try:
            fact = await asyncio.to_thread(self._snapshot, request_id, transport, payload)
            await self._state.append_history(self._key, [fact], expected_generation=self._generation)
        except SessionConflictError:
            return
        except Exception as exc:
            logger.warning("Could not save subagent model request {} ({})", request_id, type(exc).__name__)
