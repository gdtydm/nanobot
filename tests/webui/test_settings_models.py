from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from nanobot.config.schema import Config
from nanobot.providers.registry import find_by_name
from nanobot.webui.settings_models import (
    WebUISettingsError,
    create_model_configuration,
    model_settings_payload,
    provider_models_payload,
    update_agent_model_settings,
    update_model_configuration,
    update_provider_settings,
)


def test_antigravity_webui_login_pasted_callback_and_logout(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlencode, urlsplit

    from nanobot.providers import antigravity_oauth as oauth
    from nanobot.webui.settings_models import (
        complete_oauth_provider,
        login_oauth_provider,
        logout_oauth_provider,
        oauth_provider_status,
    )
    from nanobot.webui.settings_services import WebUIOAuthFlowRegistry

    path = tmp_path / "auth" / "antigravity.json"
    monkeypatch.setattr(oauth, "get_antigravity_storage_path", lambda: path)
    seen_proxy = []

    def handler(request):
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            assert form["client_id"] == ["test-client"]
            assert form["client_secret"] == ["test-client-secret"]
            if form["grant_type"] == ["refresh_token"]:
                return httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})
            return httpx.Response(200, json={"access_token": "secret", "refresh_token": "refresh", "expires_in": 3600})
        if request.url.path.endswith("userinfo"):
            return httpx.Response(200, json={"email": "test@example.com"})
        return httpx.Response(200, json={"cloudaicompanionProject": "project"})

    def client(proxy):
        seen_proxy.append(proxy)
        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(oauth, "http_client", client)
    config = Config()
    config.network.proxy = "http://127.0.0.1:7890"
    config.providers.antigravity.use_proxy = True
    spec = find_by_name("antigravity")
    registry = WebUIOAuthFlowRegistry()
    try:
        login_query = {"provider": ["antigravity"], "remote_browser": ["true"]}
        monkeypatch.delenv("NANOBOT_ANTIGRAVITY_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("NANOBOT_ANTIGRAVITY_OAUTH_CLIENT_SECRET", raising=False)
        with pytest.raises(WebUISettingsError, match="NANOBOT_ANTIGRAVITY_OAUTH_CLIENT_ID") as error:
            login_oauth_provider(config, login_query, oauth_flows=registry, config_path=None)
        assert error.value.status == 400
        assert oauth_provider_status(spec)["client_setup_required"] is True
        started = login_oauth_provider(
            config, login_query, oauth_flows=registry, config_path=None,
            oauth_client={"client_id": "test-client", "client_secret": "test-client-secret"},
        )
        assert started["completion_input"] == "callback_url"
        assert not path.exists()
        query = {"provider": ["antigravity"], "flow_id": [started["flow_id"]]}
        with pytest.raises(WebUISettingsError, match="complete callback URL"):
            complete_oauth_provider(query, "only-a-code", oauth_flows=registry, config_path=None, settings_payload=lambda **_: {})
        assert registry.get("antigravity", started["flow_id"]) is not None
        auth = parse_qs(urlsplit(started["authorization_url"]).query)
        assert auth["client_id"] == ["test-client"] and "client_secret" not in auth
        callback = auth["redirect_uri"][0] + "?" + urlencode({"code": "test-code", "state": auth["state"][0]})
        result = complete_oauth_provider(query, callback, oauth_flows=registry, config_path=None,
            settings_payload=lambda **_: oauth_provider_status(spec))
        assert result["configured"] and result["account"] == "test@example.com"
        assert result["client_setup_required"] is False
        assert "test-client" not in str(result)
        assert seen_proxy == ["http://127.0.0.1:7890"]
        refreshed = oauth.get_antigravity_token(force_refresh=True)
        assert refreshed.access == "new-access" and refreshed.refresh == "refresh"
        assert oauth.get_antigravity_login_status(path) == refreshed
        signed_out = logout_oauth_provider({"provider": ["antigravity"]}, oauth_flows=registry, config_path=None,
            settings_payload=lambda **_: oauth_provider_status(spec))
        assert signed_out["configured"] is False
        assert not path.exists()
    finally:
        registry.close()


@pytest.mark.parametrize("remote", [False, True])
def test_cliproxyapi_webui_delegates_antigravity_login(monkeypatch, remote):
    from nanobot.providers import cliproxyapi_oauth
    from nanobot.webui.settings_models import (
        complete_oauth_provider,
        login_oauth_provider,
        oauth_provider_status,
    )
    from nanobot.webui.settings_services import WebUIOAuthFlowRegistry

    authenticated = False

    def request(key, method, path, *, params=None, json=None):
        nonlocal authenticated
        assert key == "management-fixture"
        if method == "GET" and path == "/oauth/auth-url":
            assert params == {"provider": "antigravity", "is_webui": "true"}
            return {
                "status": "ok", "url": "https://accounts.google.com/o/oauth2/v2/auth?state=fixture",
                "state": "fixture",
            }
        if method == "POST" and path == "/oauth/callback":
            assert json == {"provider": "antigravity", "redirect_url": callback}
            authenticated = True
            return {"status": "ok"}
        if method == "GET" and path == "/oauth/status":
            assert params == {"state": "fixture"}
            return {"status": "ok" if authenticated else "wait"}
        assert method == "GET" and path == "/credentials"
        return {"files": [{"provider": "antigravity", "disabled": False}] if authenticated else []}

    monkeypatch.setenv("NANOBOT_CLIPROXYAPI_MANAGEMENT_KEY", "management-fixture")
    monkeypatch.setattr(cliproxyapi_oauth, "_request", request)
    config = Config()
    config.providers.cliproxyapi.api_key = "gateway-fixture"
    callback = "http://localhost:51121/oauth-callback?code=fixture-code&state=fixture"
    login_query = {"provider": ["cliproxyapi"], "remote_browser": ["true" if remote else "false"]}
    registry = WebUIOAuthFlowRegistry()
    try:
        started = login_oauth_provider(config, login_query, oauth_flows=registry, config_path=None)
        assert started["completion_input"] == ("callback_url" if remote else "poll")
        query = {"provider": ["cliproxyapi"], "flow_id": [started["flow_id"]]}
        kwargs = {
            "oauth_flows": registry, "config_path": None,
            "settings_payload": lambda **_: oauth_provider_status(find_by_name("cliproxyapi")),
        }
        assert complete_oauth_provider(query, **kwargs)["status"] == "pending"
        if not remote:
            authenticated = True
        result = complete_oauth_provider(query, callback if remote else None, **kwargs)
        assert result["configured"] is True
    finally:
        registry.close()


def test_cliproxyapi_model_catalog_preserves_ids_and_uses_proxy_effort_definitions(monkeypatch):
    from nanobot.providers import cliproxyapi_oauth

    neon = "gemini-4-neon-xhigh"
    opus = "claude-opus-5-5-high"
    tiered = "gemini-3.8-flash-tiered"
    unavailable = "gemini-4-carbon-high"
    config = Config()
    config.providers.cliproxyapi.api_key = "gateway-fixture"
    monkeypatch.setenv("NANOBOT_CLIPROXYAPI_MANAGEMENT_KEY", "management-fixture")

    def management_request(key, method, path):
        assert (key, method, path) == ("management-fixture", "GET", "/routing/model-definitions/antigravity")
        return {"models": [
            {"id": neon, "display_name": "Gemini 4 Neon", "thinking": {"levels": ["low", "medium", "high", "xhigh"]}},
            {"id": opus, "display_name": "Claude Opus 5.5 (High)", "thinking": {"levels": ["low", "medium", "high"]}},
            {"id": tiered, "display_name": "Gemini 3.8 Flash (Tiered)", "thinking": {"levels": ["low", "high"]}},
            {"id": unavailable, "display_name": "Gemini 4 Carbon"},
        ]}

    def http_get(url, *, headers, **kwargs):
        assert url == "http://127.0.0.1:8317/v1/models"
        assert headers["Authorization"] == "Bearer gateway-fixture"
        return httpx.Response(200, json={"data": [{"id": model} for model in (neon, opus, tiered)]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(cliproxyapi_oauth, "_request", management_request)
    result = provider_models_payload(config, {"provider": ["cliproxyapi"]}, http_get=http_get)
    models = result["models"]
    assert [row["id"] for row in models] == [neon, opus, tiered]
    assert models[0]["label"] == "Gemini 4 Neon"
    assert models[0]["reasoning_efforts"] == ["low", "medium", "high", "xhigh"]
    assert models[0]["default_reasoning_effort"] == "xhigh"
    assert models[0]["reasoning_family"] == "gemini-4-neon"
    assert models[1]["label"] == "Claude Opus 5.5"
    assert models[2]["label"] == "Gemini 3.8 Flash (Tiered)"


def test_preset_request_overrides_redact_roundtrip_and_clear():
    config = Config()
    config.providers.openai.api_key = "fixture"
    name = create_model_configuration(config, {
        "name": ["custom"], "provider": ["openai"], "model": ["gpt-4o"],
        "extra_headers": ['{"Authorization":"private-value","X-Route":"one"}'],
        "extra_body": ['{"metadata":{"api_key":"private-body","route":"one"}}'],
    }, oauth_status=_oauth_status)
    payload = model_settings_payload(config, oauth_status=_oauth_status)
    row = next(row for row in payload["model_presets"] if row["name"] == name)
    assert row["extra_headers"]["Authorization"] == "••••••••"
    assert row["extra_body"]["metadata"]["api_key"] == "••••••••"
    assert "private-value" not in str(payload)
    assert "private-body" not in str(payload)
    assert update_model_configuration(config, {
        "name": [name], "extraHeaders": ['{"Authorization":"••••••••","X-Route":"two"}'],
    }, oauth_status=_oauth_status)
    assert config.model_presets[name].extra_headers == {"Authorization": "private-value", "X-Route": "two"}
    assert update_model_configuration(config, {
        "name": [name], "extra_headers": [""], "extra_body": ["{}"],
    }, oauth_status=_oauth_status)
    assert config.model_presets[name].extra_headers is None
    assert config.model_presets[name].extra_body is None
    with pytest.raises(WebUISettingsError, match="JSON object"):
        update_model_configuration(config, {"name": [name], "extra_query": ["[]"]}, oauth_status=_oauth_status)
    assert config.model_presets[name].extra_query is None


def test_preset_provider_switch_clears_secrets():
    config = Config()
    config.providers.openai.api_key = "fixture"
    config.providers.anthropic.api_key = "fixture"
    name = create_model_configuration(config, {
        "name": ["custom"], "provider": ["openai"], "model": ["gpt-4o"],
        "extra_headers": ['{"Authorization":"private-value"}'],
    }, oauth_status=_oauth_status)
    update_model_configuration(config, {
        "name": [name], "provider": ["anthropic"], "model": ["claude-sonnet-4-6"],
    }, oauth_status=_oauth_status)
    assert config.model_presets[name].extra_headers is None


def _oauth_status(_spec: Any) -> dict[str, Any]:
    return {
        "configured": False,
        "account": None,
        "expires_at": None,
        "login_supported": True,
    }


@pytest.mark.parametrize("target", ["tenant_a", "tenant_b"])
@pytest.mark.parametrize("environment_model", [False, True])
def test_auto_preset_model_change_isolates_connection_settings(target, environment_model, monkeypatch):
    monkeypatch.setenv("NANOBOT_TEST_MODEL", "tenant_a/old")
    config = Config.model_validate({
        "providers": {name: {"apiKey": "fixture", "apiBase": "https://shared.test/v1"}
                      for name in ("tenant_b", "tenant_a")},
        "modelPresets": {"primary": {
            "provider": "auto", "model": "${NANOBOT_TEST_MODEL}" if environment_model else "tenant_a/old",
            "extraHeaders": {"X-Token": "private"},
            "extraQuery": {"key": "private"}, "extraBody": {"token": "private"},
        }},
    })
    if environment_model:
        update_model_configuration(config, {
            "name": ["primary"], "temperature": ["0.3"],
        }, oauth_status=_oauth_status)
        assert config.model_presets["primary"].model == "${NANOBOT_TEST_MODEL}"
    update_model_configuration(config, {
        "name": ["primary"], "model": [f"{target}/new"],
    }, oauth_status=_oauth_status)
    preset = config.model_presets["primary"]
    expected = "private" if target == "tenant_a" else None
    assert (preset.extra_headers or {}).get("X-Token") == expected
    assert (preset.extra_query or {}).get("key") == expected
    assert (preset.extra_body or {}).get("token") == expected


def test_auto_routing_retains_same_connection_settings_but_new_connection_accepts_only_new_values():
    config = Config.model_validate({
        "providers": {name: {"apiKey": "fixture", "apiBase": "https://shared.test/v1"}
                      for name in ("tenant_a", "tenant_b")},
        "modelPresets": {"primary": {
            "provider": "tenant_a", "model": "old", "extraHeaders": {"X-Token": "private"},
        }},
    })
    update_model_configuration(config, {
        "name": ["primary"], "provider": ["auto"], "model": ["tenant_a/new"],
    }, oauth_status=_oauth_status)
    assert config.model_presets["primary"].extra_headers == {"X-Token": "private"}
    with pytest.raises(WebUISettingsError):
        update_model_configuration(config.model_copy(deep=True), {
            "name": ["primary"], "provider": ["tenant_b"],
            "extra_headers": ['{"X-Token":"••••••••"}'],
        }, oauth_status=_oauth_status)
    update_model_configuration(config, {
        "name": ["primary"], "provider": ["tenant_b"],
        "extra_headers": ['{"X-Token":"new-secret"}'],
    }, oauth_status=_oauth_status)
    assert config.model_presets["primary"].extra_headers == {"X-Token": "new-secret"}


def test_model_domain_owns_dto_and_config_updates() -> None:
    config = Config()
    config.providers.openrouter.api_key = "sk-before"

    agent_changed = update_agent_model_settings(
        config,
        {
            "model": ["openai/gpt-5.4"],
            "provider": ["openrouter"],
            "context_window_tokens": ["200000"],
        },
        oauth_status=_oauth_status,
    )
    provider_changed, restart_required = update_provider_settings(
        config,
        {
            "provider": ["openrouter"],
            "api_key": ["sk-after"],
        },
    )
    payload = model_settings_payload(config, oauth_status=_oauth_status)

    assert agent_changed is True
    assert provider_changed is True
    assert restart_required is False
    assert config.agents.defaults.model == "openai/gpt-5.4"
    assert config.agents.defaults.provider == "openrouter"
    assert config.agents.defaults.context_window_tokens == 200_000
    assert config.providers.openrouter.api_key == "sk-after"
    assert set(payload) == {
        "agent",
        "model_presets",
        "model_call_order",
        "model_call_order_editable",
        "native_review_model_preset",
        "native_subagent_model_preset",
        "codex_subagent_model",
        "model_configuration_migratable",
        "model_api_resolution_supported",
        "provider_api_configuration_supported",
        "model_request_overrides_supported",
        "providers",
    }
    assert payload["agent"]["model"] == "openai/gpt-5.4"
    assert payload["model_api_resolution_supported"] is True


@pytest.mark.parametrize("tokens", [128_000, 131_072, 1_000_000])
def test_default_context_window_accepts_custom_tokens(tokens: int) -> None:
    config = Config()
    assert update_agent_model_settings(
        config, {"context_window_tokens": [str(tokens)]}, oauth_status=_oauth_status,
    )
    assert config.agents.defaults.context_window_tokens == tokens
    assert model_settings_payload(config, oauth_status=_oauth_status)["agent"]["context_window_tokens"] == tokens


@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "abc"])
def test_default_context_window_rejects_invalid_tokens(value: str) -> None:
    config = Config()
    with pytest.raises(WebUISettingsError, match="context_window_tokens must"):
        update_agent_model_settings(
            config, {"context_window_tokens": [value]}, oauth_status=_oauth_status,
        )
    assert config.agents.defaults.context_window_tokens == 200_000


@pytest.mark.parametrize("provider", ["openai_codex", "xai_grok", "github_copilot"])
@pytest.mark.parametrize("refresh", [False, True])
def test_provider_model_refresh_reaches_oauth_catalog(monkeypatch, provider, refresh):
    from unittest.mock import Mock

    from nanobot.providers.oauth_model_catalog import OAuthModelCatalogSnapshot

    catalog = Mock(return_value=OAuthModelCatalogSnapshot(models=(), source="remote", fetched_at=123))
    monkeypatch.setattr("nanobot.webui.settings_models.get_oauth_model_catalog", catalog)
    query = {"provider": [provider]}
    if refresh:
        query["refresh"] = ["1"]

    config = Config.model_validate({"providers": {provider: {}}})
    provider_models_payload(config, query, http_get=Mock())

    catalog.assert_called_once_with(provider, proxy=None, refresh=refresh)


def _catalog_http_get(body: object) -> Callable[..., httpx.Response]:
    def get(url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    return get


def test_provider_catalog_normalizes_official_model_metadata_without_changing_budgets() -> None:
    config = Config()
    config.providers.deepseek.api_key = "fixture"
    before = config.model_dump()
    payload = provider_models_payload(config, {"provider": ["deepseek"]}, http_get=_catalog_http_get({
        "data": [{
            "id": "deepseek-flash",
            "name": "DeepSeek Flash",
            "owned_by": "deepseek",
            "context_window": 1_048_576,
            "max_output_tokens": 131_072,
            "input_modalities": [" text ", "image", "text", "future-signal"],
            "output_modalities": ["text", " text "],
            "effort": {
                "supported_levels": [" low ", "high", "max", "adaptive", "max"],
                "default_level": " adaptive ",
            },
            "api_capabilities": {"anthropic_messages": {"system_prompt_update": True}},
            "vendor_details": {"arbitrary": "not part of the DTO"},
        }],
    }))

    assert payload["status"] == "available"
    assert payload["model_count"] == 1
    assert payload["models"] == [{
        "id": "deepseek-flash",
        "label": "DeepSeek Flash",
        "owned_by": "deepseek",
        "context_window": 1_048_576,
        "max_output_tokens": 131_072,
        "input_modalities": ["text", "image", "future-signal"],
        "output_modalities": ["text"],
        "reasoning_efforts": ["none", "low", "high", "max", "adaptive"],
        "default_reasoning_effort": "adaptive",
    }]
    assert config.model_dump() == before


def test_provider_catalog_enriches_legacy_known_ids_but_not_unknown_or_custom_models() -> None:
    spec = find_by_name("deepseek")
    assert spec is not None
    known_ids = [model_id for model in spec.builtin_models for model_id in (model.id, *model.aliases)]
    config = Config.model_validate({"providers": {
        "deepseek": {"apiKey": "fixture"},
        "tenant": {"apiKey": "fixture", "apiBase": "https://catalog.test/v1"},
    }})
    raw_rows = [{"id": model_id} for model_id in [*known_ids, "deepseek-unlisted"]]
    payload = provider_models_payload(
        config, {"provider": ["deepseek"]}, http_get=_catalog_http_get({"data": raw_rows}),
    )

    assert [row["id"] for row in payload["models"]] == [*known_ids, "deepseek-unlisted"]
    for row in payload["models"][:-1]:
        metadata = spec.model_metadata(row["id"])
        assert metadata is not None
        assert row["context_window"] == metadata.context_window
        assert row["max_output_tokens"] == metadata.max_output_tokens
        assert row["input_modalities"] == list(metadata.input_modalities)
        assert row["output_modalities"] == list(metadata.output_modalities)
        assert row["reasoning_efforts"] == list(metadata.reasoning_efforts)
        assert row["default_reasoning_effort"] == metadata.default_reasoning_effort
    assert payload["models"][-1] == {
        "id": "deepseek-unlisted", "label": None, "owned_by": None, "context_window": None,
    }

    custom = provider_models_payload(
        config, {"provider": ["tenant"]}, http_get=_catalog_http_get([{"id": known_ids[0]}]),
    )
    assert custom["models"] == [{
        "id": known_ids[0], "label": None, "owned_by": None, "context_window": None,
    }]


def test_provider_catalog_discards_malformed_optional_fields_not_model_rows() -> None:
    config = Config()
    config.providers.deepseek.api_key = "fixture"
    payload = provider_models_payload(config, {"provider": ["deepseek"]}, http_get=_catalog_http_get({
        "data": [
            {
                "id": "invalid-types", "context_window": True, "max_output_tokens": True,
                "input_modalities": ["text", {"type": "image"}], "output_modalities": "text",
                "effort": {"supported_levels": ["low", 7], "default_level": {"level": "low"}},
                "api_capabilities": {"anthropic_messages": {}},
            },
            {
                "id": "future-model", "max_output_tokens": -1,
                "input_modalities": ["", " "], "output_modalities": {"text": True},
                "effort": {"supported_levels": [" none ", "adaptive", "none"], "default_level": "low"},
            },
            {
                "id": "legacy-context", "context_length": 8192.5, "max_output_tokens": 4096.5,
                "input_modalities": None, "output_modalities": [False], "effort": "high",
            },
        ],
    }))

    assert payload["status"] == "available"
    assert payload["model_count"] == 3
    assert payload["models"] == [
        {"id": "invalid-types", "label": None, "owned_by": None, "context_window": None},
        {
            "id": "future-model", "label": None, "owned_by": None, "context_window": None,
            "input_modalities": [], "reasoning_efforts": ["none", "adaptive"],
        },
        {"id": "legacy-context", "label": None, "owned_by": None, "context_window": 8192},
    ]


def test_provider_catalog_combines_valid_online_fields_with_known_fallback() -> None:
    config = Config()
    config.providers.deepseek.api_key = "fixture"
    payload = provider_models_payload(config, {"provider": ["deepseek"]}, http_get=_catalog_http_get({
        "data": [{
            "id": "deepseek-flash", "max_output_tokens": True, "input_modalities": [],
            "effort": {"supported_levels": ["none", "adaptive"], "default_level": "high"},
        }],
    }))

    assert payload["models"] == [{
        "id": "deepseek-flash", "label": None, "owned_by": None, "context_window": 1_000_000,
        "max_output_tokens": 393_216, "input_modalities": [], "output_modalities": ["text"],
        "reasoning_efforts": ["none", "adaptive"],
    }]


def test_known_reasoning_efforts_preserve_default_none_and_custom_roundtrips() -> None:
    config = Config()
    config.providers.deepseek.api_key = "fixture"
    name = create_model_configuration(config, {
        "name": ["Reasoning"], "provider": ["deepseek"], "model": ["deepseek-v4-flash"],
        "reasoning_effort": [""],
    }, oauth_status=_oauth_status)

    def saved_row() -> dict[str, Any]:
        roundtripped = Config.model_validate(config.model_dump())
        payload = model_settings_payload(roundtripped, oauth_status=_oauth_status)
        return next(row for row in payload["model_presets"] if row["name"] == name)

    assert saved_row()["reasoning_effort"] is None
    assert saved_row()["reasoning_effort_values"] == ["", "none", "low", "high", "max"]
    for effort in ("none", "provider-native-mode", ""):
        assert update_model_configuration(config, {
            "name": [name], "reasoning_effort": [effort],
        }, oauth_status=_oauth_status)
        assert saved_row()["reasoning_effort"] == (effort or None)

    unknown = create_model_configuration(config, {
        "name": ["Future"], "provider": ["deepseek"], "model": ["deepseek-unlisted"],
        "reasoning_effort": ["future-effort"],
    }, oauth_status=_oauth_status)
    row = next(row for row in model_settings_payload(config, oauth_status=_oauth_status)["model_presets"]
               if row["name"] == unknown)
    assert row["reasoning_effort"] == "future-effort"
    assert row["reasoning_effort_values"] == ["", "low", "medium", "high"]
