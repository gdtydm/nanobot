from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest

from nanobot.providers.base import LLMResponse
from nanobot.providers.openai_codex_credentials import get_codex_request_credentials
from nanobot.providers.openai_codex_provider import OpenAICodexProvider


def _access_token(*, user="user", workspace="workspace", expires_in=3600) -> str:
    claims = {
        "iss": "https://auth.openai.com",
        "sub": user,
        "exp": int(time.time()) + expires_in,
        "https://api.openai.com/auth": {"chatgpt_account_id": workspace},
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _write_cli(path: Path, *, user="user", workspace="workspace", expires_in=3600) -> str:
    access = _access_token(user=user, workspace=workspace, expires_in=expires_in)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"access_token": access, "refresh_token": "cli-refresh", "account_id": workspace},
    }), encoding="utf-8")
    return access


def _write_nanobot(path: Path, *, user="user", expires_in=-60) -> str:
    access = _access_token(user=user, expires_in=expires_in)
    path.write_text(json.dumps({
        "access": access,
        "refresh": "nanobot-refresh",
        "expires": (int(time.time()) + expires_in) * 1000,
        "account_id": "workspace",
    }), encoding="utf-8")
    return access


@pytest.fixture
def credential_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    primary = tmp_path / "nanobot.json"
    monkeypatch.setenv("OAUTH_CLI_KIT_TOKEN_PATH", str(primary))
    return primary, tmp_path / ".codex" / "auth.json"


def _install_refresh_server(monkeypatch, handler):
    client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("oauth_cli_kit.flow.httpx.Client", lambda **kwargs: client(transport=transport, **kwargs))


@pytest.mark.asyncio
async def test_chat_borrows_cli_access_after_refresh_rejection_and_prefers_new_nanobot_login(
    credential_files, monkeypatch,
):
    primary, _ = credential_files
    cli = primary.parent / "custom-codex-home" / "auth.json"
    monkeypatch.setenv("CODEX_HOME", str(cli.parent))
    _write_nanobot(primary)
    cli_access = _write_cli(cli)
    original = primary.read_bytes(), cli.read_bytes()
    refresh_requests = []

    def reject(request):
        refresh_requests.append(request.content)
        return httpx.Response(401, json={"error": {"code": "refresh_token_reused"}})

    _install_refresh_server(monkeypatch, reject)
    request_headers = []

    async def respond(_url, headers, _body, **_kwargs):
        request_headers.append(headers)
        return LLMResponse(content="ok")

    monkeypatch.setattr("nanobot.providers.openai_codex_provider._request_codex", respond)
    provider = OpenAICodexProvider()
    messages = [{"role": "user", "content": "hello"}]
    assert (await provider.chat(messages)).content == "ok"
    assert (await provider.chat(messages)).content == "ok"
    assert (primary.read_bytes(), cli.read_bytes()) == original
    assert len(refresh_requests) == 1
    assert b"refresh_token=nanobot-refresh" in refresh_requests[0]

    _write_cli(cli, user="other-user")
    assert (await provider.chat(messages)).error_kind == "oauth_auth_required"

    new_access = _write_nanobot(primary, expires_in=7200)
    assert (await provider.chat(messages)).content == "ok"
    assert len(refresh_requests) == 2
    assert [headers["Authorization"] for headers in request_headers] == [
        f"Bearer {cli_access}", f"Bearer {cli_access}", f"Bearer {new_access}",
    ]
    assert all(headers["chatgpt-account-id"] == "workspace" for headers in request_headers)


@pytest.mark.parametrize(
    ("status", "error", "user", "workspace", "expires_in"),
    [
        (400, "invalid_grant", "other-user", "workspace", 3600),
        (400, "invalid_grant", "user", "other-workspace", 3600),
        (400, "invalid_grant", "user", "workspace", -60),
        (403, "access_denied", "user", "workspace", 3600),
        (429, "rate_limit", "user", "workspace", 3600),
    ],
    ids=["other-user", "other-workspace", "expired", "generic-403", "rate-limit"],
)
def test_refresh_failure_only_borrows_valid_matching_access(
    credential_files, monkeypatch, status, error, user, workspace, expires_in,
):
    primary, cli = credential_files
    _write_nanobot(primary)
    _write_cli(cli, user=user, workspace=workspace, expires_in=expires_in)
    _install_refresh_server(monkeypatch, lambda _request: httpx.Response(status, json={"error": error}))

    with pytest.raises(RuntimeError, match=f"Token refresh failed: {status}"):
        get_codex_request_credentials()


def test_successful_nanobot_refresh_takes_priority_over_cli_login(credential_files, monkeypatch):
    primary, cli = credential_files
    _write_nanobot(primary)
    cli_access = _write_cli(cli)
    new_access = _access_token(expires_in=7200)
    _install_refresh_server(monkeypatch, lambda _request: httpx.Response(200, json={
        "access_token": new_access, "refresh_token": "rotated-nanobot-refresh", "expires_in": 7200,
    }))

    assert get_codex_request_credentials().access == new_access
    assert cli_access != new_access
    assert json.loads(primary.read_text(encoding="utf-8"))["refresh"] == "rotated-nanobot-refresh"


def test_refresh_rejection_is_not_reused_after_sign_in_changes_during_refresh(credential_files, monkeypatch):
    primary, cli = credential_files
    _write_nanobot(primary)
    _write_cli(cli, user="other-user")

    def reject(_request):
        _write_nanobot(primary, user="other-user")
        return httpx.Response(400, json={"error": "invalid_grant"})

    _install_refresh_server(monkeypatch, reject)

    with pytest.raises(RuntimeError, match="Token refresh failed: 400"):
        get_codex_request_credentials()


def test_missing_nanobot_credentials_still_use_existing_cli_import(credential_files):
    primary, cli = credential_files
    cli_access = _write_cli(cli)

    assert get_codex_request_credentials().access == cli_access
    assert json.loads(primary.read_text(encoding="utf-8"))["access"] == cli_access
