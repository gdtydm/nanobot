"""Delegate Antigravity sign-in to a local CLIProxyAPI v8 installation."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx

from nanobot.config.paths import get_data_dir

_MANAGEMENT_BASE = "http://127.0.0.1:8317/v8/management"
_REASONING_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


class CLIProxyAPIOAuthError(RuntimeError):
    """A safe error to show in the settings UI."""


class CLIProxyAPICallbackError(CLIProxyAPIOAuthError):
    """A callback URL the user can correct and submit again."""


def management_key() -> str:
    value = os.environ.get("NANOBOT_CLIPROXYAPI_MANAGEMENT_KEY", "").strip()
    if value:
        return value
    try:
        return (get_data_dir() / "auth" / "cliproxyapi-management.key").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _request(
    key: str, method: str, path: str, *,
    params: dict[str, str] | None = None, json: dict[str, str] | None = None,
) -> dict[str, object]:
    try:
        with httpx.Client(trust_env=False, timeout=3) as client:
            result = client.request(
                method, _MANAGEMENT_BASE + path, headers={"Authorization": f"Bearer {key}"},
                params=params, json=json,
            )
            result.raise_for_status()
            payload = result.json()
    except (httpx.HTTPError, ValueError):
        raise CLIProxyAPIOAuthError("Could not reach CLIProxyAPI. Check the local service and management key.") from None
    if not isinstance(payload, dict):
        raise CLIProxyAPIOAuthError("CLIProxyAPI returned an invalid response.")
    return payload


def has_antigravity_login(key: str) -> bool:
    try:
        response = _request(key, "GET", "/credentials")
    except CLIProxyAPIOAuthError:
        return False
    files = response.get("files")
    return isinstance(files, list) and any(
        isinstance(item, dict) and (item.get("provider") or item.get("type")) == "antigravity"
        and not item.get("disabled") for item in files
    )


@dataclass(frozen=True)
class CLIProxyAPIModelDefinition:
    label: str
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str | None
    reasoning_family: str | None


def antigravity_model_definitions() -> dict[str, CLIProxyAPIModelDefinition]:
    """Read display and thinking metadata without changing the routable catalog."""
    key = management_key()
    if not key:
        return {}
    try:
        payload = _request(key, "GET", "/routing/model-definitions/antigravity")
    except CLIProxyAPIOAuthError:
        return {}
    models = payload.get("models")
    if not isinstance(models, list):
        return {}
    definitions: dict[str, CLIProxyAPIModelDefinition] = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        label = model.get("display_name")
        if not isinstance(label, str) or not label.strip():
            label = model_id
        label = label.strip()
        thinking = model.get("thinking")
        levels = thinking.get("levels") if isinstance(thinking, dict) else None
        efforts = tuple(level for level in levels if isinstance(level, str) and level) if isinstance(levels, list) else ()
        base, _, suffix = model_id.rpartition("-")
        default = suffix if suffix in _REASONING_LEVELS else None
        if default and label.lower().endswith(f" ({default})"):
            label = label[:-(len(default) + 3)]
        definitions[model_id] = CLIProxyAPIModelDefinition(
            label=label,
            reasoning_efforts=efforts,
            default_reasoning_effort=default,
            reasoning_family=base if default in efforts else None,
        )
    return definitions


@dataclass(frozen=True)
class _SignInConfirmation:
    # The flow registry checks for a nonempty confirmation. Google tokens stay in CLIProxyAPI.
    access: str = "confirmed"


class CLIProxyAPIOAuthFlow:
    def __init__(self, key: str, *, timeout_s: int) -> None:
        self._key = key
        self._deadline = time.monotonic() + timeout_s
        self._state = ""
        self._closed = False
        self._lock = threading.Lock()
        self.authorization_url = ""

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._deadline

    @property
    def has_pending_work(self) -> bool:
        return False

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise CLIProxyAPIOAuthError("Google sign-in was cancelled.")
            payload = _request(
                self._key, "GET", "/oauth/auth-url",
                params={"provider": "antigravity", "is_webui": "true"},
            )
            state, url = payload.get("state"), payload.get("url")
            if isinstance(state, str):
                self._state = state
            if payload.get("status") == "ok" and self._state and isinstance(url, str):
                try:
                    parsed = urlsplit(url)
                    if parsed.scheme == "https" and parsed.netloc == "accounts.google.com":
                        self.authorization_url = url
                        return
                except ValueError:
                    pass
            raise CLIProxyAPIOAuthError("CLIProxyAPI did not return a Google authorization link.")

    def complete(self, response: str | None = None, /) -> _SignInConfirmation | None:
        with self._lock:
            if self._closed or not self._state:
                raise CLIProxyAPIOAuthError("Google sign-in ended. Start again.")
            return self._complete(response)

    def _complete(self, response: str | None) -> _SignInConfirmation | None:
        if response:
            try:
                parsed = urlsplit(response.strip())
                query = parse_qs(parsed.query)
                valid = (
                    parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                    and query.get("state") == [self._state] and ("code" in query or "error" in query)
                )
            except ValueError:
                valid = False
            if not valid:
                raise CLIProxyAPICallbackError("Paste the complete callback URL for this Google sign-in.")
            _request(
                self._key, "POST", "/oauth/callback",
                json={"provider": "antigravity", "redirect_url": response.strip()},
            )
        payload = _request(self._key, "GET", "/oauth/status", params={"state": self._state})
        if payload.get("status") == "wait":
            return None
        if payload.get("status") != "ok":
            raise CLIProxyAPIOAuthError("Google sign-in did not complete. Start again in CLIProxyAPI.")
        self._state = ""
        return _SignInConfirmation()

    def cancel(self) -> None:
        with self._lock:
            self._closed = True
            if self._state:
                try:
                    _request(self._key, "DELETE", "/oauth/session", params={"state": self._state})
                except CLIProxyAPIOAuthError:
                    pass
                self._state = ""

    def wait_closed(self) -> None:
        return
