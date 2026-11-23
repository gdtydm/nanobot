"""Instance-owned Google Antigravity subscription authentication.

Protocol reference: router-for-me/CLIProxyAPI at 6960763455c4ac3d4828fb1736aa5de2183ccecf
(MIT, Copyright 2025-2005.9 Luis Pater; 2025.9-present Router-For.ME).
Google's desktop OAuth flow additionally uses PKCE and verifies the full callback.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import queue
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from filelock import FileLock

from nanobot.config.paths import get_data_dir
from nanobot.utils.helpers import _write_text_atomic  # pyright: ignore[reportPrivateUsage]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://daily-cloudcode-pa.googleapis.com"
CLIENT_VERSION = "2.34.0"
CLIENT_CHANGELIST = "1026637980"
_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
USER_AGENT = (
    f"antigravity/hub/{CLIENT_VERSION} (aidev_client; os_type={platform.system().lower()}; "
    f"arch={_ARCH}; cl={CLIENT_CHANGELIST})"
)
CALLBACK_PORT = 51121
SCOPES = tuple("https://www.googleapis.com/auth/" + value for value in (
    "cloud-platform", "userinfo.email", "userinfo.profile", "aicode", "cclog", "experimentsandconfigs",
))


class AntigravityOAuthError(RuntimeError):
    """A sanitized authentication or account initialization failure."""


class AntigravityReauthRequiredError(AntigravityOAuthError):
    """An explicit sign-in is required."""


class AntigravityOAuthInputError(AntigravityOAuthError):
    """The pasted callback is invalid; the pending flow can still be used."""


@dataclass(frozen=True, slots=True)
class AntigravityOAuthClient:
    id: str
    secret: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> AntigravityOAuthClient | None:
        client_id = os.environ.get("NANOBOT_ANTIGRAVITY_OAUTH_CLIENT_ID", "").strip()
        return cls(client_id, os.environ.get("NANOBOT_ANTIGRAVITY_OAUTH_CLIENT_SECRET", "").strip()) if client_id else None


def _require_oauth_client(saved: AntigravityOAuthClient | None) -> AntigravityOAuthClient:
    client = saved or AntigravityOAuthClient.from_environment()
    if client is None:
        raise AntigravityOAuthError(
            "Configure the OAuth client in WebUI, or set NANOBOT_ANTIGRAVITY_OAUTH_CLIENT_ID "
            "in the nanobot process environment."
        )
    return client


@dataclass(frozen=True)
class AntigravityToken:
    access: str = field(repr=False)
    refresh: str | None = field(repr=False)
    expires: int
    account_id: str
    project_id: str
    oauth_client: AntigravityOAuthClient | None = field(default=None, repr=False)


def get_antigravity_storage_path() -> Path:
    return get_data_dir() / "auth" / "antigravity.json"


def _token_lock(path: Path) -> FileLock:
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path.with_suffix(".lock")), timeout=30)


def get_antigravity_login_status(path: Path | None = None) -> AntigravityToken | None:
    path = path or get_antigravity_storage_path()
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    record = cast(dict[str, object], data)
    if any(not isinstance(record.get(key), str) or not record[key] for key in (
        "access", "account_id", "project_id",
    )):
        return None
    expires = record.get("expires")
    if not isinstance(expires, int) or isinstance(expires, bool):
        return None
    refresh = record.get("refresh")
    oauth_client = None
    raw_client = record.get("oauth_client")
    if isinstance(raw_client, dict):
        client_data = cast(dict[str, object], raw_client)
        client_id, client_secret = client_data.get("id"), client_data.get("secret")
        if isinstance(client_id, str) and client_id and isinstance(client_secret, str):
            oauth_client = AntigravityOAuthClient(client_id, client_secret)
    return AntigravityToken(
        access=cast(str, record["access"]),
        refresh=refresh if isinstance(refresh, str) and refresh else None,
        expires=expires,
        account_id=cast(str, record["account_id"]),
        project_id=cast(str, record["project_id"]),
        oauth_client=oauth_client,
    )


def _write_token(path: Path, token: AntigravityToken) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.parent.chmod(0o700)
    record = asdict(token)
    if token.oauth_client is None:
        record.pop("oauth_client")
    _write_text_atomic(path, json.dumps(record, ensure_ascii=False))
    with suppress(OSError):
        path.chmod(0o600)


def logout_antigravity(path: Path | None = None) -> bool:
    path = path or get_antigravity_storage_path()
    with _token_lock(path):
        try:
            path.unlink()
        except FileNotFoundError:
            return False
    return True


def http_client(proxy: str | None = None) -> httpx.Client:
    return httpx.Client(timeout=20, proxy=proxy, trust_env=not bool(proxy), follow_redirects=False)


def response_object(response: httpx.Response, action: str) -> dict[str, Any]:
    if not response.is_success:
        code = None
        with suppress(ValueError):
            payload: object = response.json()
            if isinstance(payload, dict):
                code = cast(dict[str, object], payload).get("error")
        if code in ("invalid_grant", "invalid_token"):
            raise AntigravityReauthRequiredError(
                "Antigravity sign-in expired. Run `nanobot provider login antigravity` again."
            )
        # Do not expose response bodies: Google can echo authorization material.
        raise AntigravityOAuthError(f"Antigravity {action} failed (HTTP {response.status_code}).")
    try:
        value: object = response.json()
    except ValueError as exc:
        raise AntigravityOAuthError(f"Antigravity {action} returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise AntigravityOAuthError(f"Antigravity {action} returned an invalid object.")
    return cast(dict[str, Any], value)


def _token_response(client: httpx.Client, oauth_client: AntigravityOAuthClient, data: dict[str, str]) -> dict[str, Any]:
    form = {**data, "client_id": oauth_client.id}
    if oauth_client.secret:
        form["client_secret"] = oauth_client.secret
    try:
        payload = response_object(client.post(TOKEN_URL, data=form), "token exchange")
    except httpx.HTTPError as exc:
        raise AntigravityOAuthError(f"Antigravity token exchange failed ({type(exc).__name__}).") from exc
    if not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
        raise AntigravityOAuthError("Antigravity returned no access token.")
    ttl = payload.get("expires_in")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        raise AntigravityOAuthError("Antigravity returned an invalid token lifetime.")
    return payload


def get_antigravity_token(
    *, proxy: str | None = None, path: Path | None = None,
    force_refresh: bool = False, rejected_access: str | None = None,
) -> AntigravityToken:
    path = path or get_antigravity_storage_path()
    with _token_lock(path):
        token = get_antigravity_login_status(path)
        if token is None:
            raise AntigravityReauthRequiredError(
                "Antigravity is not signed in. Run `nanobot provider login antigravity` first."
            )
        if (not force_refresh and token.expires > time.time() * 1000 + 300_000) or (
            rejected_access is not None and token.access != rejected_access
        ):
            return token
        if not token.refresh:
            raise AntigravityReauthRequiredError("Antigravity sign-in expired. Please sign in again.")
        oauth_client = _require_oauth_client(token.oauth_client)
        with http_client(proxy) as client:
            payload = _token_response(client, oauth_client, {"grant_type": "refresh_token", "refresh_token": token.refresh})
        refreshed = replace(
            token, access=payload["access_token"],
            refresh=payload.get("refresh_token") or token.refresh,
            expires=int(time.time() * 1000 + payload["expires_in"] * 1000),
        )
        _write_token(path, refreshed)
        return refreshed


def _project_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("cloudaicompanionProject")
    if isinstance(value, dict):
        value = cast(dict[str, Any], value).get("id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _discover_project(client: httpx.Client, access: str, cancelled: threading.Event) -> str:
    headers = {"Authorization": f"Bearer {access}", "User-Agent": USER_AGENT}
    loaded = response_object(client.post(
        API_BASE + "/v1internal:loadCodeAssist", headers=headers,
        json={"metadata": {"ideType": "ANTIGRAVITY"}},
    ), "account discovery")
    project = _project_id(loaded)
    if project:
        return project
    tiers: object = loaded.get("allowedTiers", [])
    default_tier = "free-tier"
    if isinstance(tiers, list):
        for tier in cast(list[object], tiers):
            if not isinstance(tier, dict):
                continue
            record = cast(dict[str, object], tier)
            tier_id = record.get("id")
            if record.get("isDefault") is True and isinstance(tier_id, str):
                default_tier = tier_id
                break
    for _ in range(5):
        if cancelled.is_set():
            raise AntigravityOAuthError("Antigravity sign-in was cancelled.")
        onboarded = response_object(client.post(
            API_BASE + "/v1internal:onboardUser", headers={
                **headers, "User-Agent": USER_AGENT + " google-api-nodejs-client/10.3.0",
                "X-Goog-Api-Client": "gl-node/22.21.1",
            },
            json={"tier_id": default_tier, "metadata": {
                "ide_type": "ANTIGRAVITY", "ide_version": CLIENT_VERSION, "ide_name": "antigravity",
            }},
        ), "account initialization")
        if onboarded.get("done"):
            result = onboarded.get("response")
            project = _project_id(cast(dict[str, Any], result)) if isinstance(result, dict) else None
            if project and not onboarded.get("error"):
                return project
            raise AntigravityOAuthError("Antigravity account initialization returned no usable project.")
        if cancelled.wait(2):
            raise AntigravityOAuthError("Antigravity sign-in was cancelled.")
    raise AntigravityOAuthError("Antigravity account initialization timed out. Please try again.")


class AntigravityOAuthFlow:
    """One instance-bound login shared by the CLI and WebUI."""

    def __init__(
        self, *, proxy: str | None = None, timeout_s: float = 600,
        callback_port: int = CALLBACK_PORT, remote_browser: bool = False,
        oauth_client: AntigravityOAuthClient | None = None,
    ) -> None:
        self._path = get_antigravity_storage_path()
        saved = get_antigravity_login_status(self._path)
        self._stored_oauth_client = oauth_client or (saved.oauth_client if saved else None)
        self._oauth_client = _require_oauth_client(self._stored_oauth_client)
        self._proxy = proxy
        self._state = secrets.token_urlsafe(32)
        self._verifier = secrets.token_urlsafe(64)
        self._expires_at = time.monotonic() + timeout_s
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._callbacks: queue.Queue[str] = queue.Queue(maxsize=1)
        self._token: AntigravityToken | None = None
        self._finishing = False
        self._settled = threading.Event()
        self._settled.set()
        self._thread: threading.Thread | None = None
        self._server: ThreadingHTTPServer | None = None
        if not remote_browser:
            flow = self

            class Handler(BaseHTTPRequestHandler):
                def setup(self) -> None:
                    super().setup()
                    self.connection.settimeout(2)

                def do_GET(self) -> None:  # noqa: N802
                    if urlsplit(self.path).path != "/oauth-callback":
                        self.send_error(404)
                        return
                    try:
                        flow._parse_callback(flow.redirect_uri + "?" + urlsplit(self.path).query)
                    except AntigravityOAuthInputError:
                        self.send_error(400, "Invalid OAuth callback")
                        return
                    with suppress(queue.Full):
                        flow._callbacks.put_nowait(flow.redirect_uri + "?" + urlsplit(self.path).query)
                    body = b"Sign-in received. Return to nanobot to finish account setup."
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, format: str, *args: object) -> None:
                    pass

            self._server = ThreadingHTTPServer(("127.0.0.1", callback_port), Handler)
            self._server.timeout = 0.2
            callback_port = self._server.server_port
        self.redirect_uri = f"http://localhost:{callback_port}/oauth-callback"
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).rstrip(b"=").decode()
        self.authorization_url = AUTH_URL + "?" + urlencode({
            "client_id": self._oauth_client.id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": " ".join(SCOPES), "state": self._state, "access_type": "offline",
            "prompt": "consent", "code_challenge": challenge, "code_challenge_method": "S256",
        })
        self._timer = threading.Timer(timeout_s, self.cancel)
        self._timer.daemon = True

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self._expires_at - time.monotonic()))

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._expires_at

    @property
    def has_pending_work(self) -> bool:
        return self._finishing or bool(self._thread and self._thread.is_alive()) or self._timer.is_alive()

    def start(self) -> AntigravityOAuthFlow:
        with self._lock:
            self._check_active()
            if self._server is not None:
                self._thread = threading.Thread(target=self._serve, daemon=True, name="nanobot-antigravity-oauth")
                self._thread.start()
            self._timer.start()
        return self

    def _serve(self) -> None:
        assert self._server is not None
        try:
            while not self._cancelled.is_set():
                self._server.handle_request()
        finally:
            self._server.server_close()

    def _check_active(self) -> None:
        if self._cancelled.is_set() or self.expired:
            raise AntigravityOAuthError("Antigravity sign-in expired or was cancelled. Start again.")

    def _parse_callback(self, response: str) -> str:
        try:
            uri = urlsplit(response.strip())
            expected = urlsplit(self.redirect_uri)
            values = parse_qs(uri.query)
            state = values.get("state", [""])[0]
            code = values.get("code", [""])[0]
            if (uri.scheme, uri.netloc, uri.path) != (expected.scheme, expected.netloc, expected.path):
                raise ValueError
            if not state or not hmac.compare_digest(state, self._state):
                raise ValueError
            if values.get("error"):
                return ""
            if not code or len(values.get("code", [])) != 1 or len(values.get("state", [])) != 1:
                raise ValueError
            return code
        except ValueError as exc:
            raise AntigravityOAuthInputError("Paste the complete callback URL from this sign-in, including code and state.") from exc

    def complete(self, response: str | None = None) -> AntigravityToken | None:
        with self._lock:
            if self._token is not None:
                return self._token
            self._check_active()
            if self._finishing:
                return None
            if response is None:
                try:
                    response = self._callbacks.get_nowait()
                except queue.Empty:
                    return None
            code = self._parse_callback(response)
            if not code:
                self._cancelled.set()
                self._timer.cancel()
                raise AntigravityOAuthError("Google sign-in was denied. Please try again.")
            self._finishing = True
            self._settled.clear()
        try:
            with http_client(self._proxy) as client:
                payload = _token_response(client, self._oauth_client, {
                    "grant_type": "authorization_code", "code": code,
                    "redirect_uri": self.redirect_uri, "code_verifier": self._verifier,
                })
                account = response_object(client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo?alt=json",
                    headers={"Authorization": "Bearer " + payload["access_token"]},
                ), "account lookup").get("email")
                if not isinstance(account, str) or not account:
                    raise AntigravityOAuthError("Google returned no account email.")
                project = _discover_project(client, payload["access_token"], self._cancelled)
            token = AntigravityToken(
                access=payload["access_token"], refresh=payload.get("refresh_token"),
                expires=int(time.time() * 1000 + payload["expires_in"] * 1000),
                account_id=account, project_id=project,
                oauth_client=self._stored_oauth_client,
            )
            with self._lock:
                self._check_active()
                with _token_lock(self._path):
                    _write_token(self._path, token)
                self._token = token
            return token
        except httpx.HTTPError as exc:
            raise AntigravityOAuthError(f"Antigravity account setup failed ({type(exc).__name__}).") from exc
        finally:
            self.cancel()
            self._finishing = False
            self._settled.set()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled.set()
            self._timer.cancel()
            if self._server is not None and self._thread is None:
                self._server.server_close()

    def wait_closed(self) -> None:
        self._settled.wait()
        if self._thread is not None:
            self._thread.join()
        if self._timer.ident is not None and threading.current_thread() is not self._timer:
            self._timer.join()


def login_antigravity(
    *, proxy: str | None = None, print_fn: Callable[[str], None] = print,
    prompt_fn: Callable[[str], str] = input, open_browser: bool = True,
) -> AntigravityToken:
    remote = not open_browser or bool(os.environ.get("SSH_CONNECTION"))
    flow = AntigravityOAuthFlow(proxy=proxy, remote_browser=remote).start()
    try:
        print_fn("Open this URL to sign in to Antigravity:\n" + flow.authorization_url)
        if remote or not webbrowser.open(flow.authorization_url):
            result = flow.complete(prompt_fn("Paste the complete callback URL: "))
            if result is None:
                raise AntigravityOAuthError("Antigravity sign-in is still pending.")
            return result
        while not flow.expired:
            result = flow.complete()
            if result is not None:
                return result
            time.sleep(0.2)
        raise AntigravityOAuthError("Antigravity sign-in timed out. Please try again.")
    finally:
        flow.cancel()
        flow.wait_closed()
