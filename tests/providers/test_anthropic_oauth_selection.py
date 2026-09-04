"""Credential selection, refresh classification, and the store-change reload.

These are the three behaviours that between them kept the Claude subscription
provider from ever serving a request on the reporter's machine:

* a dead managed store won selection on ``has_access_token`` alone and masked a
  healthy ``~/.claude`` credential forever (B1);
* every refresh failure, ``429`` included, was reported as "your credential is
  dead, sign in again" -- advice that rotates a working refresh token away (B2);
* the running provider read the store once and cached it for the process, so a
  dashboard import or a CLI login in another terminal changed nothing until a
  restart (B3).
"""

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.anthropic_oauth.auth import AnthropicOAuthAuth
from my_claude_code.providers.anthropic_oauth.credentials import (
    AnthropicOAuthRefreshRejected,
    AnthropicOAuthRefreshUnavailable,
    AnthropicOAuthUnavailableError,
    OAuthTokens,
)

_MANAGED_TOKEN = "sk-ant-oat01-managed-not-real"
_CLAUDE_TOKEN = "sk-ant-oat01-claude-code-not-real"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _redirect_stores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path]:
    """Point both credential sources at ``tmp_path``. Nothing real is touched."""
    managed = tmp_path / "fcc" / "anthropic_oauth.json"
    managed.parent.mkdir(parents=True, exist_ok=True)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    claude = claude_dir / ".credentials.json"
    monkeypatch.setattr(creds, "managed_store_path", lambda: managed)
    monkeypatch.setattr(creds, "claude_credentials_path", lambda: claude)
    creds._REFRESH_LOCKS.clear()
    return managed, claude


def _write_managed(path: Path, **overrides: Any) -> None:
    payload: dict[str, Any] = {
        "accessToken": _MANAGED_TOKEN,
        "refreshToken": "managed-refresh",
        "expiresAt": int(time.time()) + 3600,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_claude_code(path: Path, **overrides: Any) -> None:
    payload: dict[str, Any] = {
        "accessToken": _CLAUDE_TOKEN,
        "refreshToken": "claude-refresh",
        "expiresAt": (int(time.time()) + 3600) * 1000,
        "refreshTokenExpiresAt": (int(time.time()) + 86400) * 1000,
        "rateLimitTier": "default_claude_max_5x",
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
    }
    payload.update(overrides)
    path.write_text(json.dumps({"claudeAiOauth": payload}), encoding="utf-8")


def _mock_token_endpoint(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(creds.httpx, "AsyncClient", factory)


# ---------------------------------------------------------------------------
# B1 -- viability-based selection
# ---------------------------------------------------------------------------


def test_load_tokens_prefers_managed_store_when_it_is_still_viable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    managed, claude = _redirect_stores(monkeypatch, tmp_path)
    _write_managed(managed)
    _write_claude_code(claude)

    tokens = creds.load_tokens()

    assert tokens.access_token == _MANAGED_TOKEN
    assert tokens.source == "mcc"


def test_load_tokens_falls_back_to_claude_code_when_managed_store_is_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact live shape: an expired store with no refresh-token expiry.

    Before 6.43.0 this returned the expired managed credential, forever.
    """
    managed, claude = _redirect_stores(monkeypatch, tmp_path)
    # Expired 65 hours ago, seconds not milliseconds, no refreshTokenExpiresAt
    # and no rateLimitTier -- a store written by a pre-6.36.0 MCC.
    _write_managed(managed, expiresAt=int(time.time()) - 65 * 3600)
    _write_claude_code(claude)

    tokens = creds.load_tokens()

    # Still viable, because a refresh token with no stated expiry might work --
    # so the managed store is used and the refresh is what decides.
    assert tokens.source == "mcc"

    # ...but strip the refresh token and it is unambiguously finished.
    _write_managed(managed, expiresAt=int(time.time()) - 65 * 3600, refreshToken="")
    fallback = creds.load_tokens()
    assert fallback.access_token == _CLAUDE_TOKEN
    assert fallback.source == "claude-code"


def test_load_tokens_falls_back_when_managed_refresh_token_is_past_its_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    managed, claude = _redirect_stores(monkeypatch, tmp_path)
    _write_managed(
        managed,
        expiresAt=int(time.time()) - 3600,
        refreshTokenExpiresAt=(int(time.time()) - 60) * 1000,
    )
    _write_claude_code(claude)

    tokens = creds.load_tokens()

    assert tokens.access_token == _CLAUDE_TOKEN
    assert tokens.source == "claude-code"


def test_load_tokens_names_the_source_it_picked_and_the_one_it_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """server.log has to answer "why is it using that credential?" by itself."""
    from loguru import logger

    managed, claude = _redirect_stores(monkeypatch, tmp_path)
    _write_managed(managed, expiresAt=int(time.time()) - 3600, refreshToken="")
    _write_claude_code(claude)

    messages: list[str] = []
    sink_id = logger.add(lambda record: messages.append(record), level="WARNING")
    try:
        creds.load_tokens()
    finally:
        logger.remove(sink_id)

    joined = "".join(messages)
    assert "claude-code" in joined
    assert "mcc" in joined


def test_load_tokens_raises_when_nothing_is_viable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_stores(monkeypatch, tmp_path)

    with pytest.raises(AnthropicOAuthUnavailableError) as excinfo:
        creds.load_tokens()

    # The message says which sources were considered and why each was rejected.
    assert "absent" in str(excinfo.value)


# ---------------------------------------------------------------------------
# B2 -- refresh failure classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403])
@pytest.mark.asyncio
async def test_refresh_rejection_400_401_403_quarantines_the_managed_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int
) -> None:
    managed, _ = _redirect_stores(monkeypatch, tmp_path)
    _write_managed(managed)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "invalid_grant"})

    _mock_token_endpoint(monkeypatch, handler)

    with pytest.raises(AnthropicOAuthRefreshRejected):
        await creds.refresh_tokens(
            OAuthTokens(access_token=_MANAGED_TOKEN, refresh_token="r", source="mcc")
        )

    assert not managed.exists()
    # Renamed aside, never deleted: the evidence has to survive.
    dead = list(managed.parent.glob("anthropic_oauth.json.dead-*"))
    assert len(dead) == 1
    assert json.loads(dead[0].read_text())["accessToken"] == _MANAGED_TOKEN


@pytest.mark.asyncio
async def test_refresh_429_keeps_the_credential_and_raises_the_transient_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reporter's actual failure, reproduced against the real body shape."""
    managed, _ = _redirect_stores(monkeypatch, tmp_path)
    _write_managed(managed)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "42"},
            json={
                "error": {
                    "type": "rate_limit_error",
                    "message": "Rate limited. Please try again later.",
                }
            },
        )

    _mock_token_endpoint(monkeypatch, handler)

    with pytest.raises(AnthropicOAuthRefreshUnavailable) as excinfo:
        await creds.refresh_tokens(
            OAuthTokens(access_token=_MANAGED_TOKEN, refresh_token="r", source="mcc")
        )

    assert excinfo.value.definitive is False
    assert excinfo.value.status_code == 429
    # The store is untouched: nothing quarantined, nothing rewritten.
    assert managed.exists()
    assert list(managed.parent.glob("*.dead-*")) == []


@pytest.mark.asyncio
async def test_refresh_429_message_does_not_tell_the_user_to_sign_in_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Signing in again on a 429 rotates a working refresh token away."""
    managed, _ = _redirect_stores(monkeypatch, tmp_path)
    _write_managed(managed)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"type": "rate_limit_error"}})

    _mock_token_endpoint(monkeypatch, handler)

    with pytest.raises(AnthropicOAuthRefreshUnavailable) as excinfo:
        await creds.refresh_tokens(
            OAuthTokens(access_token=_MANAGED_TOKEN, refresh_token="r", source="mcc")
        )

    message = str(excinfo.value)
    assert "sign in again" not in message.lower()
    assert "mcc-anthropic-oauth-login" not in message
    assert "rate-limiting" in message
    assert "kept" in message


@pytest.mark.parametrize("status", [408, 500, 502, 503, 529])
@pytest.mark.asyncio
async def test_refresh_5xx_and_408_are_transient_not_rejections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int
) -> None:
    managed, _ = _redirect_stores(monkeypatch, tmp_path)
    _write_managed(managed)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream trouble")

    _mock_token_endpoint(monkeypatch, handler)

    with pytest.raises(AnthropicOAuthRefreshUnavailable):
        await creds.refresh_tokens(
            OAuthTokens(access_token=_MANAGED_TOKEN, refresh_token="r", source="mcc")
        )
    assert managed.exists()


@pytest.mark.asyncio
async def test_a_transport_error_is_transient_not_a_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    managed, _ = _redirect_stores(monkeypatch, tmp_path)
    _write_managed(managed)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    _mock_token_endpoint(monkeypatch, handler)

    with pytest.raises(AnthropicOAuthRefreshUnavailable) as excinfo:
        await creds.refresh_tokens(
            OAuthTokens(access_token=_MANAGED_TOKEN, refresh_token="r", source="mcc")
        )

    assert excinfo.value.definitive is False
    assert managed.exists()


@pytest.mark.asyncio
async def test_a_403_without_an_oauth_error_body_is_transient(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The live 403: a 17-byte non-JSON body from the edge, not the OAuth handler.

    Retiring a credential on this would be the same bug the classification
    exists to prevent, one layer down -- the request never reached the code
    that can judge the grant.
    """
    managed, _ = _redirect_stores(monkeypatch, tmp_path)
    _write_managed(managed)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="error code: 1010")

    _mock_token_endpoint(monkeypatch, handler)

    with pytest.raises(AnthropicOAuthRefreshUnavailable):
        await creds.refresh_tokens(
            OAuthTokens(access_token=_MANAGED_TOKEN, refresh_token="r", source="mcc")
        )

    assert managed.exists()
    assert list(managed.parent.glob("*.dead-*")) == []


@pytest.mark.asyncio
async def test_a_rejected_claude_code_credential_never_quarantines_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Claude Code's own file is read-only to MCC, in every direction."""
    managed, claude = _redirect_stores(monkeypatch, tmp_path)
    _write_claude_code(claude)
    before = claude.read_text(encoding="utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    _mock_token_endpoint(monkeypatch, handler)

    with pytest.raises(AnthropicOAuthRefreshRejected):
        await creds.refresh_tokens(
            OAuthTokens(
                access_token=_CLAUDE_TOKEN,
                refresh_token="r",
                source="claude-code",
            )
        )

    assert claude.read_text(encoding="utf-8") == before
    assert list(managed.parent.glob("*.dead-*")) == []


@pytest.mark.asyncio
async def test_a_successful_refresh_upgrades_a_pre_6_36_store_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reporter's store is seconds-based with no refresh expiry or tier."""
    managed, _ = _redirect_stores(monkeypatch, tmp_path)
    _write_managed(managed, expiresAt=int(time.time()) - 3600)
    assert "refreshTokenExpiresAt" not in json.loads(managed.read_text())

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "fresh-access",
                "refresh_token": "fresh-refresh",
                "expires_in": 3600,
                "refresh_token_expires_in": 86400,
                "scope": "user:inference user:profile",
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_5x",
            },
        )

    _mock_token_endpoint(monkeypatch, handler)

    await creds.refresh_tokens(
        OAuthTokens(
            access_token=_MANAGED_TOKEN,
            refresh_token="managed-refresh",
            source="mcc",
        )
    )

    written = json.loads(managed.read_text())
    assert written["accessToken"] == "fresh-access"
    assert written["refreshToken"] == "fresh-refresh"
    # Milliseconds now, matching Claude Code's own file.
    assert written["expiresAt"] > 10_000_000_000
    assert "refreshTokenExpiresAt" in written
    assert "rateLimitTier" in written


# ---------------------------------------------------------------------------
# B3 -- the running provider notices a store that changed on disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_reloads_tokens_when_the_managed_store_changes_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from my_claude_code.providers.anthropic_oauth import auth as auth_module

    managed, _ = _redirect_stores(monkeypatch, tmp_path)
    monkeypatch.setattr(auth_module, "managed_store_path", lambda: managed)
    _write_managed(managed)

    auth = AnthropicOAuthAuth()
    first = await auth.current_tokens()
    assert first.access_token == _MANAGED_TOKEN

    # What the dashboard's Import button does, from another code path.
    _write_managed(managed, accessToken="sk-ant-oat01-imported-not-real")
    # Guarantee a distinguishable (mtime_ns, size) even on a coarse clock.
    import os

    stat = managed.stat()
    os.utime(managed, ns=(stat.st_atime_ns + 10**9, stat.st_mtime_ns + 10**9))

    second = await auth.current_tokens()

    assert second.access_token == "sk-ant-oat01-imported-not-real"


@pytest.mark.asyncio
async def test_auth_does_not_reload_when_the_store_is_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from my_claude_code.providers.anthropic_oauth import auth as auth_module

    managed, _ = _redirect_stores(monkeypatch, tmp_path)
    monkeypatch.setattr(auth_module, "managed_store_path", lambda: managed)
    _write_managed(managed)

    loads = 0
    real_load = creds.load_tokens

    def counting_load() -> OAuthTokens:
        nonlocal loads
        loads += 1
        return real_load()

    monkeypatch.setattr(auth_module, "load_tokens", counting_load)

    auth = AnthropicOAuthAuth()
    for _ in range(5):
        await auth.current_tokens()

    assert loads == 1


@pytest.mark.asyncio
async def test_auth_invalidate_forces_a_re_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Disconnect removes the store; the next request must fall back, not 500."""
    from my_claude_code.providers.anthropic_oauth import auth as auth_module

    managed, claude = _redirect_stores(monkeypatch, tmp_path)
    monkeypatch.setattr(auth_module, "managed_store_path", lambda: managed)
    _write_managed(managed)
    _write_claude_code(claude)

    auth = AnthropicOAuthAuth()
    assert (await auth.current_tokens()).source == "mcc"

    creds.quarantine_managed_store()
    auth.invalidate()

    assert (await auth.current_tokens()).source == "claude-code"
