from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest

from nanobot.config.schema import Config, ModelPresetConfig
from nanobot.providers import antigravity_provider as ag
from nanobot.providers.antigravity_oauth import AntigravityToken
from nanobot.providers.antigravity_protocol import (
    NativeResponse,
    convert_messages,
    native_tool_schema,
)
from nanobot.providers.base import ProviderCallContext, ProviderConversationState
from nanobot.providers.factory import make_provider
from nanobot.providers.registry import find_by_name
from nanobot.providers.routing import resolve_automatic_model_api

TOOLS = [{"type": "function", "function": {
    "name": "echo", "description": "Echo an integer",
    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
}}]


def sse(parts, finish="STOP", usage=None):
    candidate = {"content": {"role": "model", "parts": parts}}
    if finish is not None:
        candidate["finishReason"] = finish
    response = {"candidates": [candidate]}
    if usage is not None:
        response["usageMetadata"] = usage
    return "data: " + json.dumps({"response": response}) + "\n\n"


@pytest.fixture
def transport(monkeypatch, tmp_path):
    token = AntigravityToken("access", "refresh", int(time.time() * 1000) + 3_600_000, "test@example.com", "project")
    monkeypatch.setattr(ag, "get_antigravity_storage_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(ag, "get_antigravity_login_status", lambda _path: token)
    monkeypatch.setattr(ag, "get_antigravity_token", lambda **_: token)
    real_client = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(ag.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    return install


def test_factory_routes_antigravity_without_api_key(transport):
    config = Config()
    preset = ModelPresetConfig(provider="antigravity", model="antigravity/gemini-3-flash")
    spec = find_by_name("antigravity")
    assert spec is not None and spec.is_oauth
    assert isinstance(make_provider(config, preset=preset), ag.AntigravityProvider)
    assert resolve_automatic_model_api(config, preset=preset) == ("antigravity", "antigravity_generate_content")


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "native_commentary"), [
    ("antigravity/gemini-3-flash", False), ("antigravity/gemini-4-neon-high", True),
])
async def test_streamed_tool_round_trip_retains_signed_native_parts_after_restore(transport, model, native_commentary):
    requests = []
    deltas = []
    progress_text = "I’ll echo the fixture value."

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            payload = sse([{"text": "think", "thought": True}], finish=None)
            payload += sse([{"thoughtSignature": "thinking-signature"}], finish=None)
            payload += sse([{"text": progress_text}], finish=None)
            payload += sse([{"functionCall": {"id": "call-1", "name": "echo", "args": {"value": 7}}, "thoughtSignature": "tool-signature"}])
        else:
            payload = sse([{"text": "OK 7"}], usage={
                "promptTokenCount": 100, "candidatesTokenCount": 3, "thoughtsTokenCount": 5,
                "cachedContentTokenCount": 30, "totalTokenCount": 108,
            })
        return httpx.Response(200, text=payload, headers={"Content-Type": "text/event-stream"})

    transport(handler)
    provider = ag.AntigravityProvider(default_model=model)
    assert provider.supports_commentary() is native_commentary
    context = ProviderCallContext(session_id="session-1", capture_tool_commentary=True)

    async def on_content(value):
        deltas.append(value)

    first = await provider.chat_stream_with_context(
        provider_context=context,
        messages=[{"role": "system", "content": "system"}, {"role": "user", "content": "echo 7"}],
        tools=TOOLS, tool_choice="required", on_content_delta=on_content,
    )
    assert first.should_execute_tools and first.tool_calls[0].arguments == {"value": 7}
    assert first.reasoning_content == "think"
    assert first.content == (None if native_commentary else progress_text)
    assert [item.text for item in first.commentary] == ([progress_text] if native_commentary else [])
    assert first.provider_state is not None
    restored = ProviderConversationState.from_private_record(first.provider_state.to_private_record())
    assert restored is not None
    second = await provider.chat_stream_with_context(
        provider_context=replace(context, conversation_state=restored.with_pending_messages([
            {"role": "tool", "tool_call_id": "call-1", "content": "7"},
        ])), messages=[], tools=TOOLS, on_content_delta=on_content,
    )
    assert second.finish_reason == "stop" and second.content == "OK 7"
    assert deltas == (["OK 7"] if native_commentary else [progress_text, "OK 7"])
    assert second.usage is not None and second.usage.output_tokens == 8
    assert second.usage.cache_read_tokens == 30
    history = requests[1]["request"]["contents"]
    assert history[1]["parts"][0] == {"text": "think", "thought": True, "thoughtSignature": "thinking-signature"}
    assert history[1]["parts"][1] == {"text": progress_text}
    assert history[1]["parts"][2]["thoughtSignature"] == "tool-signature"
    assert history[2]["parts"][0]["functionResponse"] == {"id": "call-1", "name": "echo", "response": {"result": "7"}}
    assert requests[0]["request"]["sessionId"] == requests[1]["request"]["sessionId"]
    assert requests[0]["request"]["tools"][0]["functionDeclarations"][0]["parameters"] == TOOLS[0]["function"]["parameters"]
    assert requests[1]["request"]["systemInstruction"] == {"parts": [{"text": "system"}]}


@pytest.mark.asyncio
async def test_antigravity_captures_commentary_after_function_call(transport):
    transport(lambda _: httpx.Response(200, text=sse([
        {"text": "internal thought", "thought": True},
        {"text": "[Milestone]: Backfill verified."},
        {"functionCall": {"id": "call-1", "name": "echo", "args": {"value": 1}}, "thoughtSignature": "sig"},
        {"text": "Checking downstream partitions next."},
    ]), headers={"Content-Type": "text/event-stream"}))
    provider = ag.AntigravityProvider(default_model="antigravity/gemini-4-neon-high")
    result = await provider.chat_stream_with_context(
        provider_context=ProviderCallContext(session_id="s1", capture_tool_commentary=True),
        messages=[{"role": "user", "content": "run"}],
        tools=TOOLS,
    )
    assert result.content is None
    assert [item.text for item in result.commentary] == [
        "[Milestone]: Backfill verified.\nChecking downstream partitions next.",
    ]
    assert result.reasoning_content == "internal thought"


@pytest.mark.asyncio
async def test_cleanly_framed_tool_stream_without_finish_reason_is_complete(transport):
    transport(lambda _: httpx.Response(200, text=sse([
        {"functionCall": {"name": "echo", "args": {"value": 7}}, "thoughtSignature": "signature"},
    ], finish=None)))
    result = await ag.AntigravityProvider().chat([{"role": "user", "content": "echo 7"}], tools=TOOLS)
    assert result.should_execute_tools and result.tool_calls[0].id


@pytest.mark.asyncio
async def test_broken_http_stream_does_not_execute_partial_tools(transport):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse([{"functionCall": {"name": "echo", "args": {"value": 7}}}], finish=None).encode()
            raise httpx.RemoteProtocolError("peer closed before complete chunked body")

    transport(lambda _: httpx.Response(200, stream=BrokenStream()))
    response = await ag.AntigravityProvider().chat([{"role": "user", "content": "echo 7"}], tools=TOOLS)
    assert response.finish_reason == "error" and not response.should_execute_tools
    assert response.error_kind == "connection"


@pytest.mark.asyncio
async def test_cancelled_stream_closes_transport(transport):
    entered, closed = asyncio.Event(), asyncio.Event()

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    transport(lambda _: httpx.Response(200, stream=WaitingStream()))
    task = asyncio.create_task(ag.AntigravityProvider().chat([{"role": "user", "content": "hello"}]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_401_refreshes_once_before_retrying_generation(transport, monkeypatch):
    calls = []
    refreshed = []

    def token(**kwargs):
        refreshed.append(kwargs)
        return AntigravityToken("new" if kwargs.get("force_refresh") else "old", "refresh", 1, "test@example.com", "project")

    monkeypatch.setattr(ag, "get_antigravity_token", token)

    def handler(request):
        calls.append(request.headers["Authorization"])
        return httpx.Response(401) if len(calls) == 1 else httpx.Response(200, text=sse([{"text": "OK"}]))

    transport(handler)
    response = await ag.AntigravityProvider().chat([{"role": "user", "content": "hello"}])
    assert response.content == "OK" and calls == ["Bearer old", "Bearer new"]
    assert refreshed[1]["rejected_access"] == "old"


def test_signed_history_cannot_cross_account_or_model(transport, monkeypatch):
    provider = ag.AntigravityProvider()
    token = ag.get_antigravity_login_status(provider._path)
    identity = ag._identity(token, provider._path)
    state = ProviderConversationState(kind="antigravity_contents", version=1,
        provider="antigravity:" + identity, model="gemini-3-flash")
    assert provider.can_resume_conversation_state(state)
    assert not provider.can_resume_conversation_state(state, "antigravity/claude-sonnet-5-5-low")
    monkeypatch.setattr(ag, "get_antigravity_login_status", lambda _: replace(token, account_id="other@example.com"))
    assert not provider.can_resume_conversation_state(state)
    with pytest.raises(ValueError, match="new chat"):
        convert_messages([{"role": "assistant", "tool_calls": [{"id": "call", "function": {"name": "echo", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": "sig"}, "antigravity": {"identity": identity, "model": "gemini-3-flash"}}}]}],
            identity="different", model="gemini-3-flash")


def test_safety_stop_never_executes_returned_tool():
    response = NativeResponse()
    response.feed({"response": {"candidates": [{"finishReason": "SAFETY", "content": {"parts": [
        {"functionCall": {"id": "call", "name": "echo", "args": {"value": 7}}},
    ]}}]}})
    assert response.result().finish_reason == "content_filter"
    assert not response.result().should_execute_tools


@pytest.mark.asyncio
async def test_http_200_client_version_rejection_is_reported_as_error(transport):
    transport(lambda _: httpx.Response(200, text=sse([
        {"text": "This version of Antigravity is no longer supported. Please download the latest version."},
    ], finish=None)))
    response = await ag.AntigravityProvider().chat([{"role": "user", "content": "hello"}])
    assert response.finish_reason == "error" and response.error_should_retry is False
    assert "Update nanobot" in response.content


def test_native_schema_expands_nullable_refs_without_renaming_tool_data_fields():
    schema = {
        "type": "object", "$defs": {"Value": {"type": ["string", "null"]}},
        "properties": {
            "title": {"$ref": "#/$defs/Value"},
            "default": {"type": "string"},
            "const": {"type": "integer", "minimum": 0},
        }, "required": ["title"],
    }
    native = native_tool_schema(schema)
    assert native["properties"]["title"] == {"type": "string", "nullable": True}
    assert native["properties"]["default"] == {"type": "string"}
    assert "minimum" in native["properties"]["const"]["description"]
    assert schema["properties"]["title"] == {"$ref": "#/$defs/Value"}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,retry", [("QUOTA_EXHAUSTED", False), ("RATE_LIMIT_EXCEEDED", True)])
async def test_quota_exhaustion_and_temporary_throttling_use_distinct_retry_policy(transport, reason, retry):
    transport(lambda _: httpx.Response(429, json={"error": {"details": [
        {"reason": reason}, {"retryDelay": "12.5s"},
    ]}}))
    response = await ag.AntigravityProvider().chat([{"role": "user", "content": "hello"}])
    assert response.error_status_code == 429 and response.error_should_retry is retry
    assert response.error_retry_after_s == 12.5


def test_account_catalog_publishes_chat_models_and_normalized_limits(transport, monkeypatch):
    def handler(request):
        assert request.url.path == "/v1internal:fetchAvailableModels"
        assert json.loads(request.content) == {"project": "project"}
        return httpx.Response(200, json={"models": {
            "gemini-3-flash": {"displayName": "Gemini 3 Flash", "maxTokens": 1_048_576, "maxOutputTokens": 65536, "supportsThinking": True},
            "claude-sonnet-5-5-low": {"displayName": "Claude Sonnet 5.5 (Low)", "supportsImages": True},
            "gemini-3.1-flash-image": {}, "tab_flash_lite_preview": {}, "gemini-4-carbon-disabled": {},
        }, "imageGenerationModelIds": ["gemini-3.1-flash-image"]})

    monkeypatch.setattr(ag, "http_client", lambda _: httpx.Client(transport=httpx.MockTransport(handler)))
    catalog = ag._fetch_models(None)
    assert [item.id for item in catalog] == ["antigravity/gemini-3-flash", "antigravity/claude-sonnet-5-5-low"]
    assert catalog[0].context_window == 1_048_576 and catalog[0].max_output_tokens == 65536
    assert catalog[1].input_modalities == ("text", "image")
