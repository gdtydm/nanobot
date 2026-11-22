# oauth-cli-kit does not publish type stubs.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from oauth_cli_kit import get_token
from oauth_cli_kit.models import OAuthToken
from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
from oauth_cli_kit.storage import FileTokenStorage

from nanobot.providers.oauth_catalog_cache import oauth_catalog_auth_rejected


@dataclass(frozen=True, slots=True)
class CodexRequestCredentials:
    access: str = field(repr=False)
    account_id: str | None


@dataclass(frozen=True, slots=True)
class _RefreshRejection:
    path: Path
    proxy: str | None = field(repr=False)
    credential_digest: bytes
    expires_at: int


_last_rejection: _RefreshRejection | None = None


class _RequestStorage(FileTokenStorage):
    """Observe the SDK's reads so a concurrent sign-in can't authorize fallback."""

    def __init__(self) -> None:
        super().__init__(token_filename=OPENAI_CODEX_PROVIDER.token_filename)
        self.token: OAuthToken | None = None
        self.unchanged = True
        self._loaded = False

    def load(self) -> OAuthToken | None:
        token = super().load()
        if self._loaded and token != self.token:
            self.unchanged = False
        self.token = token
        self._loaded = True
        return token


def codex_refresh_rejected(exc: RuntimeError) -> bool:
    # oauth-cli-kit reports refresh failures as strings. Match its exact envelope.
    prefix = "Token refresh failed: "
    detail = str(exc)
    if not detail.startswith(prefix):
        return False
    status, _, body = detail[len(prefix):].partition(" ")
    if status == "401":
        return True
    if status not in {"400", "403"} or len(body) > 16_384:
        return False
    try:
        payload: object = json.loads(body)
    except (ValueError, RecursionError):
        return False
    return oauth_catalog_auth_rejected(int(status), payload)


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _access_claims(access: object) -> dict[str, object]:
    if not isinstance(access, str):
        return {}
    parts = access.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    try:
        return _object(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))))
    except (ValueError, RecursionError):
        return {}


def _identity(claims: dict[str, object], account_id: object) -> tuple[str, str, str] | None:
    issuer = claims.get("iss")
    subject = claims.get("sub")
    auth = _object(claims.get(OPENAI_CODEX_PROVIDER.jwt_claim_path))
    workspace = auth.get(OPENAI_CODEX_PROVIDER.account_id_claim)
    if (
        isinstance(issuer, str) and issuer
        and isinstance(subject, str) and subject
        and isinstance(workspace, str) and workspace and workspace == account_id
    ):
        return issuer, subject, workspace
    return None


def _read_cli_access(primary: OAuthToken) -> tuple[CodexRequestCredentials, int] | None:
    identity = _identity(_access_claims(primary.access), primary.account_id)
    if identity is None:
        return None

    codex_home = os.environ.get("CODEX_HOME")
    path = (Path(codex_home).expanduser() if codex_home else Path.home() / ".codex") / "auth.json"
    try:
        auth = _object(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError):
        return None
    if auth.get("auth_mode") not in (None, "chatgpt"):
        return None

    tokens = _object(auth.get("tokens"))
    access = tokens.get("access_token")
    if not isinstance(access, str):
        return None
    claims = _access_claims(access)
    expires_at = claims.get("exp")
    not_before = claims.get("nbf", 0)
    now = time.time()
    if (
        type(expires_at) is not int or expires_at <= now
        or type(not_before) is not int or not_before > now
        or _identity(claims, tokens.get("account_id")) != identity
    ):
        return None
    return CodexRequestCredentials(access, identity[2]), expires_at


def _credential_digest(token: OAuthToken) -> bytes:
    value = json.dumps((token.access, token.refresh, token.expires, token.account_id))
    return hashlib.sha256(value.encode()).digest()


def get_codex_request_credentials(*, proxy: str | None = None) -> CodexRequestCredentials:
    """Prefer NanoBot OAuth and borrow only a current, matching CLI access token."""
    global _last_rejection

    storage = _RequestStorage()
    path = storage.get_token_path()
    rejection = _last_rejection
    if rejection and rejection.path == path and rejection.proxy == proxy and rejection.expires_at > time.time():
        token = storage.load()
        if token and _credential_digest(token) == rejection.credential_digest:
            fallback = _read_cli_access(token)
            if fallback:
                return fallback[0]

    try:
        token = get_token(storage=storage, proxy=proxy)
    except RuntimeError as exc:
        primary = storage.token
        if not codex_refresh_rejected(exc) or not storage.unchanged or primary is None:
            raise
        fallback = _read_cli_access(primary)
        if fallback is None:
            raise
        credentials, expiry = fallback
        _last_rejection = _RefreshRejection(path, proxy, _credential_digest(primary), expiry)
        return credentials
    return CodexRequestCredentials(token.access, token.account_id)
