from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from nanobot.providers import antigravity_oauth as oauth


@pytest.fixture
def storage(tmp_path, monkeypatch):
    path = tmp_path / "auth" / "antigravity.json"
    monkeypatch.setattr(oauth, "get_antigravity_storage_path", lambda: path)
    monkeypatch.setenv("NANOBOT_ANTIGRAVITY_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("NANOBOT_ANTIGRAVITY_OAUTH_CLIENT_SECRET", "test-client-secret")
    return path


def callback(flow, **updates):
    query = parse_qs(urlsplit(flow.authorization_url).query)
    values = {"code": "test-code", "state": query["state"][0], **updates}
    return flow.redirect_uri + "?" + urlencode(values)


def install_transport(monkeypatch, handler):
    monkeypatch.setattr(oauth, "http_client", lambda proxy: httpx.Client(transport=httpx.MockTransport(handler)))


def test_pkce_login_discovers_project_and_commits_to_original_instance(storage, monkeypatch, tmp_path):
    flow = oauth.AntigravityOAuthFlow(remote_browser=True).start()
    auth = parse_qs(urlsplit(flow.authorization_url).query)
    assert auth["client_id"] == ["test-client"]
    assert "client_secret" not in auth
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            assert form["client_id"] == ["test-client"]
            assert form["client_secret"] == ["test-client-secret"]
            digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
            assert auth["code_challenge"] == [base64.urlsafe_b64encode(digest).rstrip(b"=").decode()]
            assert form["redirect_uri"] == [flow.redirect_uri]
            return httpx.Response(200, json={"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
        if request.url.path.endswith("userinfo"):
            return httpx.Response(200, json={"email": "test@example.com"})
        assert request.headers["Authorization"] == "Bearer access"
        return httpx.Response(200, json={"cloudaicompanionProject": {"id": "project-1"}})

    install_transport(monkeypatch, handler)
    monkeypatch.setenv("NANOBOT_ANTIGRAVITY_OAUTH_CLIENT_ID", "new-client")
    monkeypatch.setattr(oauth, "get_antigravity_storage_path", lambda: tmp_path / "other" / "auth.json")
    try:
        token = flow.complete(callback(flow))
        assert token is not None and token.project_id == "project-1"
        assert oauth.get_antigravity_login_status(storage) == token
        assert set(json.loads(storage.read_text())) == {"access", "refresh", "expires", "account_id", "project_id"}
        assert storage.stat().st_mode & 0o777 == 0o600
        assert len(requests) == 3
        assert flow.complete() == token
    finally:
        flow.cancel()
        flow.wait_closed()


def test_invalid_pasted_callback_keeps_login_available(storage, monkeypatch):
    flow = oauth.AntigravityOAuthFlow(remote_browser=True).start()
    try:
        with pytest.raises(oauth.AntigravityOAuthInputError):
            flow.complete(callback(flow, state="other-flow"))
        assert flow.complete() is None
        assert not flow.expired
        assert not storage.exists()
    finally:
        flow.cancel()
        flow.wait_closed()


def test_first_login_onboards_using_account_default_tier(storage, monkeypatch):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if request.url.path.endswith("loadCodeAssist"):
            return httpx.Response(200, json={"allowedTiers": [{"id": "pro-tier", "isDefault": True}]})
        return httpx.Response(200, json={"done": True, "response": {"cloudaicompanionProject": "new-project"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with client:
        assert oauth._discover_project(client, "access", threading.Event()) == "new-project"
    assert seen[1]["tier_id"] == "pro-tier"


def test_cancel_during_account_setup_does_not_replace_saved_login(storage, monkeypatch):
    old = oauth.AntigravityToken("old", "old-refresh", 1, "old@example.com", "old-project")
    oauth._write_token(storage, old)
    entered, release = threading.Event(), threading.Event()
    flow = oauth.AntigravityOAuthFlow(remote_browser=True).start()

    def handler(request):
        if request.url.path == "/token":
            entered.set()
            assert release.wait(5)
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "refresh", "expires_in": 3600})
        if request.url.path.endswith("userinfo"):
            return httpx.Response(200, json={"email": "new@example.com"})
        return httpx.Response(200, json={"cloudaicompanionProject": "new-project"})

    install_transport(monkeypatch, handler)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(flow.complete, callback(flow))
        assert entered.wait(5)
        flow.cancel()
        release.set()
        with pytest.raises(oauth.AntigravityOAuthError, match="cancelled"):
            pending.result(5)
    flow.wait_closed()
    assert oauth.get_antigravity_login_status(storage) == old


def test_concurrent_refresh_reuses_rotated_credentials_and_logout_removes_them(storage, monkeypatch):
    oauth._write_token(storage, oauth.AntigravityToken("expired", "refresh", 1, "test@example.com", "project"))
    calls = []

    def handler(request):
        calls.append(request)
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["refresh_token"], "refresh_token": ["refresh"],
            "client_id": ["test-client"], "client_secret": ["test-client-secret"],
        }
        return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "rotated", "expires_in": 3600})

    install_transport(monkeypatch, handler)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: oauth.get_antigravity_token(), range(3)))
    assert len(calls) == 1
    assert all(token.access == "fresh" and token.refresh == "rotated" for token in results)
    assert oauth.logout_antigravity()
    with pytest.raises(oauth.AntigravityReauthRequiredError):
        oauth.get_antigravity_token()


def test_401_refresh_does_not_refresh_a_token_another_request_already_replaced(storage, monkeypatch):
    token = oauth.AntigravityToken("fresh", "refresh", int(time.time() * 1000) + 3_600_000, "test@example.com", "project")
    oauth._write_token(storage, token)
    monkeypatch.delenv("NANOBOT_ANTIGRAVITY_OAUTH_CLIENT_ID")
    assert oauth.get_antigravity_token(force_refresh=True, rejected_access="stale") == token
    oauth._write_token(storage, replace(token, expires=1))
    with pytest.raises(oauth.AntigravityOAuthError, match="NANOBOT_ANTIGRAVITY_OAUTH_CLIENT_ID"):
        oauth.get_antigravity_token()
    assert oauth.get_antigravity_login_status(storage) == replace(token, expires=1)
    monkeypatch.setenv("NANOBOT_ANTIGRAVITY_OAUTH_CLIENT_ID", "test-client")
    install_transport(monkeypatch, lambda _: httpx.Response(400, json={"error": "invalid_grant", "echo": "secret"}))
    with pytest.raises(oauth.AntigravityReauthRequiredError, match="sign-in expired"):
        oauth.get_antigravity_token()


def test_google_denial_finishes_pending_login_without_saving_credentials(storage):
    flow = oauth.AntigravityOAuthFlow(remote_browser=True).start()
    auth = parse_qs(urlsplit(flow.authorization_url).query)
    denied = flow.redirect_uri + "?" + urlencode({"error": "access_denied", "state": auth["state"][0]})
    try:
        with pytest.raises(oauth.AntigravityOAuthError, match="denied"):
            flow.complete(denied)
        assert oauth.get_antigravity_login_status() is None
    finally:
        flow.cancel()
        flow.wait_closed()
