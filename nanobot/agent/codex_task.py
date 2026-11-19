"""Codex App Server transport for one active subagent turn."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from nanobot.agent.tools.subagent_config import CodexTaskConfig
from nanobot.execution.processes import ProcessTree, spawn_process_tree
from nanobot.utils.cancellation import shield_and_drain

_RPC_TIMEOUT = 15
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"enum": ["done", "incomplete", "needs_input"]},
        "summary": {"type": "string"},
        "question": {"type": ["string", "null"]},
    },
    "required": ["state", "summary", "question"],
    "additionalProperties": False,
}


class CodexTaskError(RuntimeError):
    pass


class CodexTurn:
    def __init__(self, config: CodexTaskConfig, cwd: Path) -> None:
        self.config = config
        self.cwd = cwd
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.model: str | None = config.model
        self.effort: str | None = config.effort
        self._terminal = asyncio.Event()
        self._tree: ProcessTree | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._rpc_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=512)

    async def __aenter__(self) -> CodexTurn:
        self._tree = await spawn_process_tree(
            [self.config.executable, "app-server", "--listen", "stdio://"],
            str(self.cwd), dict(os.environ), stdin=asyncio.subprocess.PIPE, stream_limit=8 * 1024 * 1024,
        )
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain_stderr())
        try:
            await self._rpc("initialize", {
                "clientInfo": {"name": "nanobot", "title": "nanobot", "version": "1"},
            })
            await self._write({"method": "initialized"})
        except BaseException:
            await shield_and_drain(self._close())
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await shield_and_drain(self._close())

    async def _close(self) -> None:
        try:
            if self.thread_id and self.turn_id and not self._terminal.is_set():
                with suppress(CodexTaskError, OSError, TimeoutError):
                    async with asyncio.timeout(3):
                        await self._rpc("turn/interrupt", {
                            "threadId": self.thread_id, "turnId": self.turn_id,
                        })
                        await self._terminal.wait()
        finally:
            if self._tree is not None:
                try:
                    await self._tree.terminate()
                finally:
                    workers = [task for task in (self._reader, self._stderr) if task is not None]
                    for task in workers:
                        task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)

    async def _drain_stderr(self) -> None:
        assert self._tree is not None and self._tree.process.stderr is not None
        while await self._tree.process.stderr.read(8192):
            pass

    async def _write(self, message: dict[str, Any]) -> None:
        assert self._tree is not None and self._tree.process.stdin is not None
        stream = self._tree.process.stdin
        stream.write(json.dumps(message).encode() + b"\n")
        await stream.drain()

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._reader is None or self._reader.done():
            raise CodexTaskError("Codex App Server disconnected")
        self._rpc_id += 1
        request_id = self._rpc_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(_RPC_TIMEOUT):
                await self._write({"method": method, "params": params, "id": request_id})
                return await future
        finally:
            self._pending.pop(request_id, None)

    async def _read(self) -> None:
        assert self._tree is not None and self._tree.process.stdout is not None
        stream = self._tree.process.stdout
        try:
            while raw := await stream.readline():
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    continue
                method = payload.get("method")
                if "id" in payload and method:
                    await self._write({
                        "id": payload["id"], "error": {
                            "code": -32601, "message": "Interactive request unavailable for background tasks",
                        },
                    })
                elif "id" in payload:
                    future = self._pending.get(payload["id"])
                    if future is None or future.done():
                        continue
                    result = payload.get("result")
                    if isinstance(result, dict):
                        future.set_result(result)
                    else:
                        future.set_exception(CodexTaskError("Codex rejected a protocol request"))
                elif method in {"item/started", "item/completed", "turn/completed"}:
                    params = payload.get("params")
                    if not isinstance(params, dict):
                        continue
                    observed = {"threadId": params.get("threadId"), "turnId": params.get("turnId")}
                    if method == "turn/completed":
                        if not isinstance(turn := params.get("turn"), dict):
                            continue
                        observed["turn"] = {"id": turn.get("id"), "status": turn.get("status")}
                        if (params.get("threadId") == self.thread_id
                                and (self.turn_id is None or turn.get("id") == self.turn_id)):
                            self._terminal.set()
                    else:
                        if not isinstance(item := params.get("item"), dict):
                            continue
                        observed["item"] = {key: item.get(key) for key in ("id", "type", "phase", "exitCode", "status")}
                        if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                            observed["item"]["text"] = item["text"][:64_000]
                    self._events.put_nowait({"method": method, "params": observed})
        except (OSError, ValueError, TypeError, asyncio.QueueFull):
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CodexTaskError("Codex App Server disconnected"))
            if self._events.full():
                self._events.get_nowait()
            self._events.put_nowait(None)

    async def open_thread(self, thread_id: str | None) -> str:
        params: dict[str, Any] = {
            "cwd": str(self.cwd),
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
            "sandbox": "workspace-write",
        }
        if self.model:
            params["model"] = self.model
        if thread_id:
            params["threadId"] = thread_id
            params["excludeTurns"] = True
        result = await self._rpc("thread/resume" if thread_id else "thread/start", params)
        thread = result.get("thread")
        actual_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(actual_id, str) or (thread_id and actual_id != thread_id):
            raise CodexTaskError("Codex returned an unexpected thread")
        self.thread_id = actual_id
        if isinstance(result.get("model"), str):
            self.model = result["model"]
        if self.effort is None and isinstance(result.get("reasoningEffort"), str):
            self.effort = result["reasoningEffort"]
        return actual_id

    async def start(self, text: str) -> str:
        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": text}],
            "cwd": str(self.cwd),
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
            "sandboxPolicy": {
                "type": "workspaceWrite", "writableRoots": [str(self.cwd)],
                "networkAccess": self.config.network_access,
            },
            "outputSchema": _OUTPUT_SCHEMA,
        }
        if self.effort:
            params["effort"] = self.effort
        result = await self._rpc("turn/start", params)
        turn = result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str):
            raise CodexTaskError("Codex did not identify the started turn")
        self.turn_id = turn_id
        return turn_id

    async def steer(self, text: str, message_id: str) -> None:
        if self.turn_id is None or self._terminal.is_set():
            raise CodexTaskError("Codex turn has already ended")
        result = await self._rpc("turn/steer", {
            "threadId": self.thread_id, "expectedTurnId": self.turn_id,
            "clientUserMessageId": message_id, "input": [{"type": "text", "text": text}],
        })
        if result.get("turnId") != self.turn_id:
            raise CodexTaskError("Codex did not accept input for the active turn")

    async def result(self, on_item: Callable[[str, dict[str, Any]], Awaitable[None]]) -> tuple[str, str, str | None]:
        final = ""
        while (event := await self._events.get()) is not None:
            params = event["params"]
            if params.get("threadId") != self.thread_id or (params.get("turnId") or self.turn_id) != self.turn_id:
                continue
            if event["method"] == "turn/completed":
                turn = params.get("turn")
                if not isinstance(turn, dict) or turn.get("id") != self.turn_id:
                    continue
                if turn.get("status") != "completed":
                    return "incomplete", "Codex turn ended without a verified result.", None
                break
            item = params.get("item")
            if not isinstance(item, dict):
                continue
            if (item.get("type") == "agentMessage" and event["method"] == "item/completed"
                    and item.get("phase") != "commentary"):
                if isinstance(item.get("text"), str):
                    final = item["text"]
            else:
                await on_item(event["method"], item)
        else:
            raise CodexTaskError("Codex disconnected before reporting a terminal turn")
        try:
            output = json.loads(final)
        except ValueError:
            return "incomplete", "Codex did not return a structured result.", None
        if not isinstance(output, dict) or not isinstance(output.get("summary"), str):
            return "incomplete", "Codex did not return a structured result.", None
        state, summary, question = output.get("state"), output["summary"], output.get("question")
        if state == "needs_input" and isinstance(question, str) and question.strip():
            return state, summary, question.strip()
        if state == "done" and summary.strip():
            return state, summary, None
        return "incomplete", summary, None
