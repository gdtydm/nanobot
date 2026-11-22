"""Direct Antigravity subscription transport; no external proxy process required."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import httpx

from nanobot.providers.antigravity_oauth import (
    API_BASE,
    USER_AGENT,
    AntigravityOAuthError,
    AntigravityReauthRequiredError,
    AntigravityToken,
    get_antigravity_login_status,
    get_antigravity_storage_path,
    get_antigravity_token,
    http_client,
    response_object,
)
from nanobot.providers.antigravity_protocol import (
    NativeResponse,
    convert_messages,
    convert_tools,
    object_list,
    object_value,
    tool_config,
)
from nanobot.providers.base import (
    LLMProvider,
    LLMResponse,
    ProviderCallContext,
    ProviderConversationState,
    resolve_stream_idle_timeout_s,
)
from nanobot.providers.images import prepare_message_images
from nanobot.providers.oauth_catalog_cache import (
    OAuthCatalogAuthRequiredError,
    OAuthModelCatalog,
    OAuthModelCatalogSnapshot,
)
from nanobot.providers.registry import ProviderModelSpec

DEFAULT_ANTIGRAVITY_MODEL = "antigravity/gemini-3-flash"
_NEON_COMMENTARY_MODELS = frozenset({
    "gemini-4-neon-low", "gemini-4-neon-medium", "gemini-4-neon-high", "gemini-4-neon-xhigh",
})
_MAX_EVENT_BYTES = 8 * 1024 * 1024
_MESSAGE_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name", "thinking_blocks"})


def _identity(token: AntigravityToken, path: Path) -> str:
    return hashlib.sha256(f"{path.resolve()}\0{token.account_id}\0{token.project_id}".encode()).hexdigest()


def _wire_model(model: str) -> str:
    return model.removeprefix("antigravity/")


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _fetch_models(proxy: str | None) -> tuple[ProviderModelSpec, ...]:
    try:
        token = get_antigravity_token(proxy=proxy)
    except AntigravityReauthRequiredError as exc:
        raise OAuthCatalogAuthRequiredError(str(exc)) from exc
    with http_client(proxy) as client:
        response = client.post(API_BASE + "/v1internal:fetchAvailableModels", json={"project": token.project_id}, headers={
            "Authorization": "Bearer " + token.access, "User-Agent": USER_AGENT,
        })
        response.raise_for_status()
        payload = response_object(response, "model discovery")
    models = object_value(payload.get("models"), "model catalog")
    excluded = set(payload.get("imageGenerationModelIds") or []) | set(payload.get("audioTranscriptionModelIds") or [])
    result: list[ProviderModelSpec] = []
    for name, raw in models.items():
        if name in excluded or "image" in name or name.endswith("-disabled") or not name.startswith(("gemini-", "claude-")):
            continue
        info = object_value(raw, "model metadata")
        label = info.get("displayName")
        reasoning = ("low", "medium", "high") if info.get("supportsThinking") and name.startswith("gemini-") else ()
        result.append(ProviderModelSpec(
            id="antigravity/" + name,
            label=label if isinstance(label, str) else name,
            owned_by="Antigravity", context_window=_positive_int(info.get("maxTokens")),
            max_output_tokens=_positive_int(info.get("maxOutputTokens")), reasoning_efforts=reasoning,
            input_modalities=("text", "image") if info.get("supportsImages") else ("text",),
            output_modalities=("text",),
        ))
    return tuple(result)


_CATALOG = OAuthModelCatalog(fallback_models=(), fetch=_fetch_models)


def get_antigravity_model_catalog(proxy: str | None = None, *, refresh: bool = False) -> OAuthModelCatalogSnapshot:
    path = get_antigravity_storage_path()
    token = get_antigravity_login_status(path)
    key = _identity(token, path) if token else str(path.resolve()) + ":signed-out"
    return _CATALOG.get(cache_key=key + ":" + (proxy or ""), proxy=proxy, refresh=refresh)


def invalidate_antigravity_model_catalog() -> None:
    _CATALOG.invalidate()


class AntigravityProvider(LLMProvider):
    def __init__(
        self, default_model: str = DEFAULT_ANTIGRAVITY_MODEL, proxy: str | None = None,
        extra_body: dict[str, Any] | None = None, *, provider_name: str = "antigravity",
    ) -> None:
        super().__init__(api_key=None, api_base=API_BASE, provider_name=provider_name)
        self.default_model = default_model
        self.proxy = proxy
        self._path = get_antigravity_storage_path()
        self._extra_body = deepcopy(extra_body or {})

    def get_default_model(self) -> str:
        return self.default_model

    def supports_commentary(
        self, model: str | None = None, reasoning_effort: str | None = None,
    ) -> bool:
        return _wire_model(model or self.default_model) in _NEON_COMMENTARY_MODELS

    def can_resume_conversation_state(self, state: ProviderConversationState, model: str | None = None) -> bool:
        token = get_antigravity_login_status(self._path)
        return bool(
            token and state.kind == "antigravity_contents" and state.version == 1
            and state.provider == "antigravity:" + _identity(token, self._path)
            and state.model == _wire_model(model or self.default_model)
        )

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
        reasoning_effort: str | None = None, tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        return await self._call(messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice)

    async def chat_stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
        reasoning_effort: str | None = None, tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        return await self._call(
            messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice,
            on_content_delta, on_thinking_delta, on_tool_call_delta,
        )

    async def chat_with_context(self, *, provider_context: ProviderCallContext, **kwargs: Any) -> LLMResponse:
        return await self._call(provider_context=provider_context, **kwargs)

    async def chat_stream_with_context(self, *, provider_context: ProviderCallContext, **kwargs: Any) -> LLMResponse:
        return await self._call(provider_context=provider_context, **kwargs)

    async def _call(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
        reasoning_effort: str | None = None, tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        provider_context: ProviderCallContext | None = None,
    ) -> LLMResponse:
        started = time.monotonic()
        first_delta: float | None = None
        try:
            messages = self._sanitize_request_messages(self._sanitize_empty_content(messages), _MESSAGE_KEYS)
            token = await asyncio.to_thread(get_antigravity_token, proxy=self.proxy, path=self._path)
            wire_model = _wire_model(model or self.default_model)
            identity = _identity(token, self._path)
            state = provider_context.current_conversation_state if provider_context else None
            resumed = state is not None and self.can_resume_conversation_state(state, wire_model)
            if resumed and state is not None:
                previous = object_list(state.payload.get("contents"), "saved contents")
                raw_system: object = state.payload.get("system")
                system = object_value(raw_system, "saved system") if raw_system is not None else None
                pending = await prepare_message_images(self._sanitize_request_messages(
                    self._sanitize_empty_content(state.pending_messages), _MESSAGE_KEYS,
                ))
                contents, system = convert_messages(pending, previous=previous, system=system, identity=identity, model=wire_model)
                current_system_messages = [message for message in messages if message.get("role") in {"system", "developer"}]
                if current_system_messages:
                    _, system = convert_messages(current_system_messages)
            else:
                contents, system = convert_messages(await prepare_message_images(messages), identity=identity, model=wire_model)
            generation: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
            if reasoning_effort:
                if wire_model.startswith("claude-"):
                    raise ValueError("For Antigravity Claude models, select the Low, Medium, or High model variant.")
                if wire_model.startswith("gemini-2.5"):
                    budgets = {"none": 0, "minimal": 128, "low": 1024, "medium": 8192, "high": 24576, "xhigh": 32768}
                    if reasoning_effort not in budgets:
                        raise ValueError("Unsupported Antigravity reasoning effort.")
                    generation["thinkingConfig"] = {"thinkingBudget": budgets[reasoning_effort], "includeThoughts": reasoning_effort != "none"}
                else:
                    if reasoning_effort not in {"low", "medium", "high"}:
                        raise ValueError("This Antigravity Gemini model supports low, medium, or high reasoning.")
                    generation["thinkingConfig"] = {"thinkingLevel": reasoning_effort, "includeThoughts": True}
            session_key = provider_context.session_id if provider_context else None
            session_key = session_key or (state.payload.get("session_id") if resumed and state else None) or uuid.uuid4().hex
            digest = hashlib.sha256((identity + ":" + str(session_key)).encode()).digest()
            request: dict[str, Any] = {
                **deepcopy(self._extra_body), "contents": contents, "generationConfig": generation,
                "sessionId": "-" + str(int.from_bytes(digest[:8]) & 0x7FFFFFFFFFFFFFFF),
            }
            if system:
                request["systemInstruction"] = system
            declarations = convert_tools(tools)
            if declarations:
                request["tools"] = declarations
                request["toolConfig"] = tool_config(tool_choice)
            # A completed tool call determines whether preceding text is commentary.
            capture_tool_commentary = bool(
                declarations and provider_context and provider_context.capture_tool_commentary
                and self.supports_commentary(wire_model)
            )
            body = {
                "project": token.project_id, "model": wire_model,
                "userAgent": "antigravity", "requestType": "agent",
                "requestId": "agent-" + str(uuid.uuid4()), "request": request,
            }
            assembled = NativeResponse()
            timeout = httpx.Timeout(resolve_stream_idle_timeout_s(), connect=20, write=20, pool=20)
            async with httpx.AsyncClient(proxy=self.proxy, trust_env=not bool(self.proxy), timeout=timeout) as client:
                for attempt in range(2):
                    async with client.stream(
                        "POST", API_BASE + "/v1internal:streamGenerateContent?alt=sse", json=body,
                        headers={"Authorization": "Bearer " + token.access, "User-Agent": USER_AGENT, "Accept": "text/event-stream"},
                    ) as response:
                        if response.status_code == 401 and attempt == 0:
                            token = await asyncio.to_thread(
                                get_antigravity_token, proxy=self.proxy, path=self._path,
                                force_refresh=True, rejected_access=token.access,
                            )
                            if _identity(token, self._path) != identity:
                                raise AntigravityReauthRequiredError("Antigravity account changed during this request. Try again.")
                            continue
                        if not response.is_success:
                            raw_error = bytearray()
                            async for chunk in response.aiter_bytes():
                                raw_error.extend(chunk[:max(0, 16_384 - len(raw_error))])
                                if len(raw_error) >= 16_384:
                                    break
                            error_payload: object = None
                            try:
                                error_payload = json.loads(raw_error)
                            except ValueError:
                                pass
                            return _http_error(response, error_payload)
                        data_lines: list[str] = []
                        size = 0

                        async def consume() -> None:
                            nonlocal first_delta
                            if not data_lines:
                                return
                            raw = "\n".join(data_lines)
                            if raw == "[DONE]":
                                return
                            envelope: object = json.loads(raw)
                            text, thoughts, calls = assembled.feed(envelope)
                            if (text or thoughts) and first_delta is None:
                                first_delta = time.monotonic()
                            if on_content_delta and not capture_tool_commentary:
                                for delta in text:
                                    await on_content_delta(delta)
                            for delta in thoughts:
                                if on_thinking_delta:
                                    await on_thinking_delta(delta)
                            for call in calls:
                                if on_tool_call_delta:
                                    await on_tool_call_delta(call)

                        async for line in response.aiter_lines():
                            size += len(line)
                            if size > _MAX_EVENT_BYTES:
                                raise ValueError("Antigravity response event exceeded the supported size.")
                            if line.startswith("data:"):
                                data_lines.append(line[5:].lstrip(" "))
                            elif not line:
                                await consume()
                                data_lines = []
                                size = 0
                        await consume()
                        break
            result = assembled.result(clean_eof=True, capture_tool_commentary=capture_tool_commentary)
            if (
                capture_tool_commentary and on_content_delta and result.content
                and result.finish_reason in {"tool_calls", "stop", "length"}
            ):
                await on_content_delta(result.content)
            for call in result.tool_calls:
                if call.extra_content:
                    call.extra_content["antigravity"] = {"identity": identity, "model": wire_model}
            for block in result.thinking_blocks or []:
                block["antigravity"] = {"identity": identity, "model": wire_model}
            if first_delta is not None:
                result.ttft_ms = int((first_delta - started) * 1000)
                result.generation_ms = int((time.monotonic() - first_delta) * 1000)
            if provider_context is not None and result.finish_reason in {"stop", "tool_calls"}:
                native_contents = deepcopy(contents)
                native_contents.append({"role": "model", "parts": assembled.parts})
                result.provider_state = ProviderConversationState(
                    kind="antigravity_contents", version=1, model=wire_model,
                    provider="antigravity:" + identity,
                    payload={
                        "contents": native_contents, "system": system, "session_id": session_key,
                        "context_tokens": result.usage.input_tokens + result.usage.output_tokens if result.usage else None,
                    },
                )
            return result
        except AntigravityReauthRequiredError as exc:
            return LLMResponse(content=str(exc), finish_reason="error", error_status_code=401, error_should_retry=False)
        except AntigravityOAuthError as exc:
            return LLMResponse(content=str(exc), finish_reason="error", error_should_retry=False)
        except httpx.TimeoutException:
            return LLMResponse(content="Antigravity request timed out.", finish_reason="error", error_kind="timeout", error_should_retry=True)
        except httpx.HTTPError:
            return LLMResponse(content="Could not connect to Antigravity.", finish_reason="error", error_kind="connection", error_should_retry=True)
        except ValueError as exc:
            return LLMResponse(content=str(exc), finish_reason="error", error_should_retry=False)


def _http_error(response: httpx.Response, payload: object = None) -> LLMResponse:
    status = response.status_code
    retry = status in {408, 429} or status >= 500
    delay = None
    try:
        value = float(response.headers.get("Retry-After", ""))
        if 0 <= value <= 86400:
            delay = value
    except ValueError:
        pass
    quota_exhausted = False
    if isinstance(payload, dict):
        error: object = cast(dict[str, object], payload).get("error")
        if isinstance(error, dict):
            details: object = cast(dict[str, object], error).get("details", [])
            if isinstance(details, list):
                for raw_detail in cast(list[object], details):
                    if not isinstance(raw_detail, dict):
                        continue
                    detail = cast(dict[str, object], raw_detail)
                    reason = detail.get("reason")
                    if isinstance(reason, str) and reason in {"QUOTA_EXHAUSTED", "DAILY_LIMIT_EXCEEDED", "INSUFFICIENT_QUOTA"}:
                        quota_exhausted = True
                    retry_delay = detail.get("retryDelay")
                    if isinstance(retry_delay, str) and re.fullmatch(r"\d+(?:\.\d+)?s", retry_delay):
                        seconds = float(retry_delay[:-1])
                        if seconds <= 86400:
                            delay = seconds
    if quota_exhausted:
        retry = False
    message = {
        400: "Antigravity rejected the request format.",
        401: "Antigravity sign-in expired. Please sign in again.",
        403: "Antigravity denied access. Check this account's subscription and complete any required account setup.",
        429: "Antigravity usage limit reached. Wait for the account quota to recover.",
    }.get(status, f"Antigravity request failed (HTTP {status}).")
    return LLMResponse(
        content=message, finish_reason="error", error_status_code=status,
        error_should_retry=retry, error_retry_after_s=delay,
        error_code="quota_exhausted" if quota_exhausted else None,
    )
