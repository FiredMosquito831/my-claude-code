"""What the Claude subscription provider actually puts on the wire.

Every assertion here is against the header set, body and gate decision that
leave MCC, because this provider's whole failure mode was that nobody had
checked: it had served zero successful requests in its life, and the one test
that covered its headers asserted the bug.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.constants import ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.client_fingerprint import (
    ClientFingerprint,
    fingerprint_from_headers,
    install_fingerprint,
)
from my_claude_code.providers.anthropic_oauth import (
    AnthropicOAuthAuth,
    AnthropicOAuthProvider,
    OAuthTokens,
    capture_rate_limit_headers,
    merge_betas,
)
from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.anthropic_oauth.constants import (
    ANTHROPIC_OAUTH_BETA_FLOOR,
    CLAUDE_CODE_USER_AGENT,
    OAUTH_REFRESH_BETA,
    OAUTH_SCOPES,
    REFRESH_LEEWAY_SECONDS,
    TOKEN_URL,
)
from my_claude_code.providers.anthropic_oauth.oauth_login import build_authorize_url
from my_claude_code.providers.anthropic_oauth.rate_limit_headers import (
    RateLimitObserver,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.rate_limit import ProviderRateLimiter

_TOKEN = "sk-ant-oat01-not-a-real-token"

CLI_MARKER = "x-anthropic-billing-header: cc_version=2.1.258; cc_entrypoint=cli;"

# The exact header set a real Claude Code 2.1.258 session sends into MCC,
# transcribed from the request log's captured headers.
REAL_CLAUDE_CODE_HEADERS = {
    "user-agent": "claude-cli/2.1.251 (external, cli)",
    "x-app": "cli",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": (
        "claude-code-20250219,context-1m-2025-08-07,"
        "interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
        "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
        "mid-conversation-system-2026-04-07,advisor-tool-2026-03-01,"
        "effort-2025-11-24"
    ),
    "accept": "application/json",
    "content-type": "application/json",
}


def _request(system: Any = None, **extra: Any) -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=32,
        system=system,
        messages=[Message(role="user", content="ping")],
        stream=True,
        **extra,
    )


def _config(api_key: str = "") -> ProviderConfig:
    return ProviderConfig(
        api_key=api_key,
        base_url="https://api.anthropic.com/v1",
        rate_limit=100,
        rate_window=60,
        max_concurrency=5,
        retry_attempts=1,
        early_retry_attempts=1,
        commit_holdback_seconds=0,
    )


def _use_mock_token_endpoint(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Point ``credentials``' own httpx client at a MockTransport."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return original(transport=transport, timeout=kwargs.get("timeout", 30.0))

    monkeypatch.setattr(creds.httpx, "AsyncClient", factory)


def _provider(**kwargs: Any) -> AnthropicOAuthProvider:
    return AnthropicOAuthProvider(
        _config(),
        rate_limiter=ProviderRateLimiter(
            rate_limit=100, rate_window=60, max_concurrency=5, max_retries=0
        ),
        auth=AnthropicOAuthAuth(
            OAuthTokens(access_token=_TOKEN, source="test", subscription_type="max")
        ),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# T1 -- the credential
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_token_is_presented_as_a_bearer_not_an_api_key() -> None:
    headers = await AnthropicOAuthAuth(
        OAuthTokens(access_token=_TOKEN, source="test")
    ).headers()

    assert headers["Authorization"] == f"Bearer {_TOKEN}"
    assert not any(name.lower() == "x-api-key" for name in headers)


# ---------------------------------------------------------------------------
# T2/T3 -- anthropic-beta
# ---------------------------------------------------------------------------


def test_beta_header_is_the_floor_unioned_with_the_clients_own() -> None:
    value, dropped = merge_betas(REAL_CLAUDE_CODE_HEADERS["anthropic-beta"])
    names = value.split(",")

    assert dropped == ()
    assert names[: len(ANTHROPIC_OAUTH_BETA_FLOOR)] == list(ANTHROPIC_OAUTH_BETA_FLOOR)
    # Every one of the client's own betas survives, once, in its own order.
    assert names.count("claude-code-20250219") == 1
    assert "context-1m-2025-08-07" in names
    assert "interleaved-thinking-2025-05-14" in names
    assert names.index("context-1m-2025-08-07") < names.index("effort-2025-11-24")


def test_unknown_client_betas_are_dropped_and_reported() -> None:
    value, dropped = merge_betas(
        "interleaved-thinking-2025-05-14,not-a-real-beta-2099-01-01"
    )

    assert "not-a-real-beta-2099-01-01" not in value
    assert dropped == ("not-a-real-beta-2099-01-01",)
    assert "interleaved-thinking-2025-05-14" in value


def test_no_client_betas_leaves_the_floor_alone() -> None:
    assert merge_betas(None) == (",".join(ANTHROPIC_OAUTH_BETA_FLOOR), ())


# ---------------------------------------------------------------------------
# T4/T5 -- the client fingerprint reaches the wire
# ---------------------------------------------------------------------------


def test_client_fingerprint_reads_the_real_claude_code_header_set() -> None:
    client = fingerprint_from_headers(REAL_CLAUDE_CODE_HEADERS)

    assert client.user_agent == "claude-cli/2.1.251 (external, cli)"
    assert client.x_app == "cli"
    assert client.anthropic_version == "2023-06-01"
    assert client.ua_entrypoint == "cli"
    assert client.ua_version == "2.1.251"
    assert client.is_empty is False


def test_fingerprint_reads_the_agent_sdks_user_agent() -> None:
    client = fingerprint_from_headers(
        {"user-agent": "claude-cli/2.1.223 (external, sdk-py, agent-sdk/0.2.131)"}
    )

    assert client.ua_entrypoint == "sdk-py"
    assert client.ua_version == "2.1.223"


def test_fingerprint_ignores_everything_it_does_not_mirror() -> None:
    client = fingerprint_from_headers(
        {"authorization": "Bearer secret", "x-api-key": "secret", "cookie": "secret"}
    )

    assert client.is_empty is True


@pytest.mark.asyncio
async def test_user_agent_and_x_app_mirror_the_client() -> None:
    auth = AnthropicOAuthAuth(OAuthTokens(access_token=_TOKEN, source="test"))

    background = auth._headers_for(
        OAuthTokens(access_token=_TOKEN),
        fingerprint_from_headers(
            {"user-agent": "claude-cli/2.1.251 (external, cli)", "x-app": "cli-bg"}
        ),
    )
    assert background["x-app"] == "cli-bg"
    assert background["user-agent"] == "claude-cli/2.1.251 (external, cli)"

    absent = auth._headers_for(OAuthTokens(access_token=_TOKEN), ClientFingerprint())
    assert absent["x-app"] == "cli"
    assert absent["user-agent"] == CLAUDE_CODE_USER_AGENT
    assert "2.1.258" in CLAUDE_CODE_USER_AGENT


@pytest.mark.asyncio
async def test_installed_fingerprint_reaches_the_upstream_headers() -> None:
    install_fingerprint(REAL_CLAUDE_CODE_HEADERS)
    headers = await AnthropicOAuthAuth(
        OAuthTokens(access_token=_TOKEN, source="test")
    ).headers()

    assert headers["user-agent"] == REAL_CLAUDE_CODE_HEADERS["user-agent"]
    assert "context-1m-2025-08-07" in headers["anthropic-beta"]
    assert headers["anthropic-beta"].startswith("oauth-2025-04-20,")
    install_fingerprint(None)


# ---------------------------------------------------------------------------
# T6 -- tool names go out verbatim
# ---------------------------------------------------------------------------


def _sse(*events: dict[str, Any]) -> bytes:
    """Serialize a well-formed Anthropic SSE stream, message_stop included."""
    parts = [
        {"type": "message_start", "message": {"id": "msg_1", "usage": {}}},
        *events,
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        for event in parts
    )


async def _capture_upstream(
    provider: AnthropicOAuthProvider,
    request: MessagesRequest,
    *,
    frames: bytes | None = None,
    status: int = 200,
) -> tuple[list[httpx.Request], list[bytes], list[str]]:
    sent: list[httpx.Request] = []
    bodies: list[bytes] = []

    body = _sse() if frames is None else frames

    async def handler(http_request: httpx.Request) -> httpx.Response:
        sent.append(http_request)
        bodies.append(http_request.content)
        return httpx.Response(status, content=body)

    provider._messages._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    out = [frame async for frame in provider.stream_response(request)]
    await provider.cleanup()
    return sent, bodies, out


@pytest.mark.asyncio
async def test_tool_names_are_sent_and_returned_verbatim() -> None:
    """The ``cc_`` prefix is gone; see the 6.36.0 release notes.

    A full scan of Claude Code 2.1.258 finds zero occurrences of any ``cc_``
    tool name against 137 for the real ``mcp__`` prefix, and the old reverse
    pass was a blind string replace over each SSE frame -- so a frame whose
    *content* happened to contain the literal was corrupted too. This asserts
    both halves: names go out untouched, and content comes back untouched.
    """
    frame_text = 'the literal "name":"cc_Read" in content'
    frames = _sse(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Read"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": frame_text},
        },
        {"type": "content_block_stop", "index": 0},
    )
    request = _request(
        f"{CLI_MARKER}\nclaude code",
        tools=[{"name": "Read", "input_schema": {"type": "object"}}],
        tool_choice={"type": "tool", "name": "Read"},
    )

    _, bodies, out = await _capture_upstream(_provider(), request, frames=frames)

    body = json.loads(bodies[0])
    assert body["tools"][0]["name"] == "Read"
    assert body["tool_choice"]["name"] == "Read"
    assert '"cc_' not in bodies[0].decode()
    joined = "".join(out)
    assert '"name":"Read"' in joined.replace(" ", "")
    # The content that merely mentioned a prefixed name survives untouched.
    assert "cc_Read" in joined


# ---------------------------------------------------------------------------
# T7/T8/T9 -- the token lifecycle
# ---------------------------------------------------------------------------


def test_refresh_leeway_is_two_minutes() -> None:
    now = time.time()
    assert REFRESH_LEEWAY_SECONDS == 120
    inside = OAuthTokens(
        access_token=_TOKEN, refresh_token="r", expires_at=int(now) + 119
    )
    outside = OAuthTokens(
        access_token=_TOKEN, refresh_token="r", expires_at=int(now) + 121
    )

    assert inside.needs_refresh(now=now) is True
    assert outside.needs_refresh(now=now) is False
    # Inside the window but not yet expired: the refresh may happen off the
    # request's critical path.
    assert inside.is_expired(now=now) is False
    assert (
        OAuthTokens(
            access_token=_TOKEN, refresh_token="r", expires_at=int(now) - 1
        ).is_expired(now=now)
        is True
    )


@pytest.mark.asyncio
async def test_concurrent_refreshes_perform_one_exchange_across_two_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The lock is per credential file, not per provider instance.

    A hot reload builds a second provider while the first is alive. Two
    instance-local locks let both refresh at once, and the loser's write
    clobbers the winner's with a refresh token Anthropic already rotated away.
    """
    store = tmp_path / "anthropic_oauth.json"
    monkeypatch.setattr(creds, "managed_store_path", lambda: store)
    creds._REFRESH_LOCKS.clear()
    posts: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={
                "access_token": "fresh-token",
                "refresh_token": "fresh-refresh",
                "expires_in": 3600,
            },
        )

    _use_mock_token_endpoint(monkeypatch, handler)

    expired = OAuthTokens(
        access_token=_TOKEN,
        refresh_token="stale-refresh",
        expires_at=int(time.time()) - 10,
        source="mcc",
    )
    auths = [AnthropicOAuthAuth(expired), AnthropicOAuthAuth(expired)]
    results = await asyncio.gather(
        *(auth.current_tokens() for auth in auths for _ in range(10))
    )

    assert len(posts) == 1
    assert {tokens.access_token for tokens in results} == {"fresh-token"}


@pytest.mark.asyncio
async def test_refresh_sends_the_oauth_beta_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "anthropic_oauth.json"
    monkeypatch.setattr(creds, "managed_store_path", lambda: store)
    creds._REFRESH_LOCKS.clear()
    posts: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})

    _use_mock_token_endpoint(monkeypatch, handler)

    await creds.refresh_tokens(
        OAuthTokens(access_token=_TOKEN, refresh_token="r", source="mcc")
    )

    assert str(posts[0].url) == TOKEN_URL
    assert posts[0].headers["anthropic-beta"] == OAUTH_REFRESH_BETA


@pytest.mark.asyncio
async def test_a_refresh_error_never_carries_a_response_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "anthropic_oauth.json"
    monkeypatch.setattr(creds, "managed_store_path", lambda: store)
    creds._REFRESH_LOCKS.clear()

    async def handler(request: httpx.Request) -> httpx.Response:
        # A token endpoint can echo what was presented to it.
        return httpx.Response(400, json={"error": "invalid_grant", "seen": _TOKEN})

    _use_mock_token_endpoint(monkeypatch, handler)

    with pytest.raises(creds.AnthropicOAuthRefreshError) as excinfo:
        await creds.refresh_tokens(
            OAuthTokens(access_token=_TOKEN, refresh_token="r", source="mcc")
        )

    assert _TOKEN not in str(excinfo.value)
    assert "mcc-anthropic-oauth-login" in str(excinfo.value)


# ---------------------------------------------------------------------------
# T10 -- a 401 refreshes once and retries once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_401_refreshes_once_and_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    refreshes: list[int] = []

    async def fake_refresh() -> OAuthTokens | None:
        refreshes.append(1)
        return OAuthTokens(access_token="second-token", source="mcc")

    monkeypatch.setattr(provider._oauth, "force_refresh", fake_refresh)
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if len(seen) == 1:
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, content=_sse())

    provider._messages._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    [frame async for frame in provider.stream_response(_request(f"{CLI_MARKER}\nx"))]
    await provider.cleanup()

    assert refreshes == [1]
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_a_second_401_is_an_authentication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    attempts: list[int] = []

    async def fake_refresh() -> OAuthTokens | None:
        return OAuthTokens(access_token="second-token", source="mcc")

    monkeypatch.setattr(provider._oauth, "force_refresh", fake_refresh)

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"error": "unauthorized"})

    provider._messages._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    with pytest.raises(Exception) as excinfo:
        [
            frame
            async for frame in provider.stream_response(_request(f"{CLI_MARKER}\nx"))
        ]
    await provider.cleanup()

    # Exactly one retry: two upstream calls, never a third.
    assert len(attempts) == 2
    assert "401" in str(excinfo.value)


# ---------------------------------------------------------------------------
# T11/T12 -- the credential file round-trips
# ---------------------------------------------------------------------------


def test_refresh_token_expiry_and_rate_limit_tier_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Shaped like the live ``~/.claude/.credentials.json``."""
    store = tmp_path / "mcc.json"
    monkeypatch.setattr(creds, "managed_store_path", lambda: store)
    payload = {
        "claudeAiOauth": {
            "accessToken": _TOKEN,
            "refreshToken": "r",
            "expiresAt": 1788361487694,
            "refreshTokenExpiresAt": 1788637732694,
            "rateLimitTier": "default_claude_max_5x",
            "scopes": [
                "user:profile",
                "user:inference",
                "user:sessions:claude_code",
                "user:mcp_servers",
                "user:file_upload",
            ],
            "subscriptionType": "max",
        }
    }
    path = tmp_path / "cc" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))

    loaded = creds.load_claude_code_tokens()
    assert loaded is not None
    assert loaded.rate_limit_tier == "default_claude_max_5x"
    assert loaded.refresh_token_expires_at == 1788637732
    assert "user:inference" in loaded.scopes

    creds.store_tokens(loaded)
    again = creds.load_managed_tokens()
    assert again is not None
    assert again.rate_limit_tier == "default_claude_max_5x"
    assert again.refresh_token_expires_at == loaded.refresh_token_expires_at
    assert again.expires_at == loaded.expires_at
    assert again.subscription_type == "max"


def test_stored_expiry_is_milliseconds_and_both_units_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "mcc.json"
    monkeypatch.setattr(creds, "managed_store_path", lambda: store)
    instant = 1788361487

    creds.store_tokens(OAuthTokens(access_token=_TOKEN, expires_at=instant))
    raw = json.loads(store.read_text(encoding="utf-8"))

    assert raw["expiresAt"] > 1e11
    seconds = creds._tokens_from_payload(
        {"accessToken": _TOKEN, "expiresAt": instant}, source="t"
    )
    millis = creds._tokens_from_payload(
        {"accessToken": _TOKEN, "expiresAt": instant * 1000}, source="t"
    )
    assert seconds is not None and millis is not None
    assert seconds.expires_at == millis.expires_at == instant


# ---------------------------------------------------------------------------
# T13 -- login
# ---------------------------------------------------------------------------


def test_login_requests_the_full_claude_code_scope_set() -> None:
    assert OAUTH_SCOPES.split() == [
        "org:create_api_key",
        "user:profile",
        "user:inference",
        "user:sessions:claude_code",
        "user:mcp_servers",
        "user:file_upload",
    ]


def test_authorize_url_uses_the_current_host() -> None:
    url = build_authorize_url("verifier-value")

    assert url.startswith("https://claude.com/cai/oauth/authorize?")
    assert "platform.claude.com%2Foauth%2Fcode%2Fcallback" in url
    assert TOKEN_URL == "https://platform.claude.com/v1/oauth/token"


# ---------------------------------------------------------------------------
# T14 -- the rate-limit headers
# ---------------------------------------------------------------------------


def test_unified_rate_limit_headers_are_captured() -> None:
    captured = capture_rate_limit_headers(
        {
            "anthropic-ratelimit-unified-status": "session-limit-reached",
            "anthropic-ratelimit-unified-5h-utilization": "1.0",
            "anthropic-ratelimit-unified-5h-reset": "2026-09-02T22:00:00Z",
            "anthropic-usage-limit": "max",
        }
    )

    assert captured["anthropic-ratelimit-unified-status"] == "session-limit-reached"
    assert captured["anthropic-ratelimit-unified-5h-reset"] == "2026-09-02T22:00:00Z"


def test_unknown_rate_limit_headers_are_not_stored() -> None:
    captured = capture_rate_limit_headers(
        {"anthropic-ratelimit-invented": "9", "set-cookie": "secret"}
    )

    assert captured == {}


def test_the_observer_keeps_only_the_latest_snapshot() -> None:
    observer = RateLimitObserver()
    assert observer.latest is None

    observer.observe(
        {"anthropic-ratelimit-unified-5h-utilization": "0.1"},
        status_code=200,
        now=1.0,
    )
    observer.observe(
        {"anthropic-ratelimit-unified-5h-utilization": "0.9"},
        status_code=200,
        now=2.0,
    )

    assert observer.latest is not None
    assert observer.latest.values["anthropic-ratelimit-unified-5h-utilization"] == "0.9"
    assert observer.latest.observed_at == 2.0
    # A response with no rate-limit header must not erase the last real one.
    observer.observe({"content-type": "application/json"}, status_code=200, now=3.0)
    assert observer.latest.observed_at == 2.0


@pytest.mark.asyncio
async def test_a_real_response_populates_the_observer() -> None:
    provider = _provider()
    from my_claude_code.providers.anthropic_oauth import rate_limit_headers as rlh

    rlh.OBSERVER._latest = None

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(),
            headers={"anthropic-ratelimit-unified-5h-utilization": "0.42"},
        )

    provider._messages._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    [frame async for frame in provider.stream_response(_request(f"{CLI_MARKER}\nx"))]
    await provider.cleanup()

    assert rlh.OBSERVER.latest is not None
    assert (
        rlh.OBSERVER.latest.values["anthropic-ratelimit-unified-5h-utilization"]
        == "0.42"
    )
    rlh.OBSERVER._latest = None


# ---------------------------------------------------------------------------
# T15/T16 -- attribution and the rotation refusal
# ---------------------------------------------------------------------------


def test_key_label_names_the_plan_and_the_source() -> None:
    provider = _provider()

    assert provider.credential_label == "max · test"


def test_a_comma_separated_raw_token_is_refused() -> None:
    config = _config("sk-ant-oat01-one,sk-ant-oat01-two")

    with pytest.raises(InvalidRequestError) as excinfo:
        AnthropicOAuthProvider(
            config,
            rate_limiter=ProviderRateLimiter(
                rate_limit=100, rate_window=60, max_concurrency=5, max_retries=0
            ),
        )

    message = str(excinfo.value)
    assert "refresh" in message
    assert "one credential" in message


def test_the_managed_reference_is_not_treated_as_a_raw_token() -> None:
    config = _config(ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE)

    provider = AnthropicOAuthProvider(
        config,
        rate_limiter=ProviderRateLimiter(
            rate_limit=100, rate_window=60, max_concurrency=5, max_retries=0
        ),
    )

    assert provider._oauth.tokens is None


# ---------------------------------------------------------------------------
# T21 -- cache_control survives to the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_control_survives_to_the_wire() -> None:
    """For a Max credential this is the 5-hour budget, once versus every turn."""
    request = _request(
        [
            {
                "type": "text",
                "text": f"{CLI_MARKER}\nYou are Claude Code.",
                "cache_control": {"type": "ephemeral"},
            }
        ]
    )

    _, bodies, _ = await _capture_upstream(_provider(), request)

    body = json.loads(bodies[0])
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# T22 -- a recorded Claude Code session replays end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorded_claude_code_session_replays_end_to_end() -> None:
    """The gate admits it and the upstream headers are exactly the contract."""
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "anthropic_oauth"
        / "claude_code_session.json"
    )
    recorded = json.loads(fixture.read_text(encoding="utf-8"))
    assert "sk-ant" not in fixture.read_text(encoding="utf-8")

    install_fingerprint(recorded["headers"])
    request = MessagesRequest.model_validate(recorded["body"])
    provider = _provider()
    sent, bodies, _ = await _capture_upstream(provider, request)
    install_fingerprint(None)

    headers = sent[0].headers
    assert headers["authorization"] == f"Bearer {_TOKEN}"
    assert "x-api-key" not in headers
    assert headers["user-agent"] == recorded["headers"]["user-agent"]
    assert headers["x-app"] == recorded["headers"]["x-app"]
    assert headers["anthropic-version"] == recorded["headers"]["anthropic-version"]
    assert headers["anthropic-dangerous-direct-browser-access"] == "true"
    betas = headers["anthropic-beta"].split(",")
    assert betas[:2] == list(ANTHROPIC_OAUTH_BETA_FLOOR)
    for name in recorded["headers"]["anthropic-beta"].split(","):
        assert name in betas
    # The body goes upstream with its tools and its system prompt untouched.
    body = json.loads(bodies[0])
    assert [tool["name"] for tool in body["tools"]] == [
        tool["name"] for tool in recorded["body"]["tools"]
    ]
    assert body["system"][0]["text"] == recorded["body"]["system"][0]["text"]
