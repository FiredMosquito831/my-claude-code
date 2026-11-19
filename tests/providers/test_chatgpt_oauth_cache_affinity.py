"""Tests for ChatGPT OAuth per-conversation cache affinity & sticky turn state."""

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from my_claude_code.config import settings as config_settings
from my_claude_code.config.constants import CHATGPT_OAUTH_SESSION_AFFINITY_DEFAULT
from my_claude_code.config.provider_catalog import CHATGPT_OAUTH_DEFAULT_BASE
from my_claude_code.config.settings import (
    Settings,
    configured_chatgpt_oauth_session_affinity,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.client_fingerprint import (
    MAX_CHATGPT_SESSION_CHARS,
    ClientFingerprint,
    chatgpt_session_id_from_headers,
    current_chatgpt_session_id,
    fingerprint_from_headers,
    install_fingerprint,
)
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth.affinity import (
    PROMPT_CACHE_KEY_MAX_CHARS,
    TURN_STATE_MAX_ENTRIES,
    TURN_STATE_TTL_SECONDS,
    TurnStateCache,
    account_scope_key,
    derive_conversation_affinity,
    get_chatgpt_turn_state_cache,
    normalise_turn_state,
    resolve_conversation_seed,
    turn_state_cache_key,
)
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.chatgpt_oauth.credentials import ChatGPTOAuthCredentials
from my_claude_code.providers.chatgpt_oauth.provider import (
    ChatGPTOAuthProvider,
    _build_headers,
)
from tests.providers.support import passthrough_rate_limiter


def _provider_config(**overrides: Any) -> ProviderConfig:
    return ProviderConfig(
        api_key=str(overrides.get("api_key", "test_token")),
        base_url=str(overrides.get("base_url", CHATGPT_OAUTH_DEFAULT_BASE)),
        rate_limit=int(overrides.get("rate_limit", 10)),
        rate_window=int(overrides.get("rate_window", 60)),
        max_concurrency=int(overrides.get("max_concurrency", 5)),
        http_read_timeout=float(overrides.get("http_read_timeout", 300.0)),
        http_write_timeout=float(overrides.get("http_write_timeout", 10.0)),
        http_connect_timeout=float(overrides.get("http_connect_timeout", 60.0)),
        proxy=str(overrides.get("proxy", "")),
    )


def _set_affinity(enabled: bool | None, monkeypatch: pytest.MonkeyPatch) -> None:
    if enabled is None:
        monkeypatch.delenv("CHATGPT_OAUTH_SESSION_AFFINITY", raising=False)
    else:
        monkeypatch.setenv(
            "CHATGPT_OAUTH_SESSION_AFFINITY", "true" if enabled else "false"
        )
    config_settings.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Any:
    config_settings.get_settings.cache_clear()
    yield
    config_settings.get_settings.cache_clear()


def _sample_request(
    *,
    system: str | list[dict[str, str]] | None = "You are helpful.",
    user_text: str = "Hello world",
    metadata: dict[str, Any] | None = None,
    extra_messages: list[dict[str, Any]] | None = None,
) -> MessagesRequest:
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
    if extra_messages:
        messages.extend(extra_messages)
    payload: dict[str, Any] = {
        "model": "gpt-5.6-terra",
        "max_tokens": 128,
        "messages": messages,
    }
    if system is not None:
        payload["system"] = system
    if metadata is not None:
        payload["metadata"] = metadata
    return MessagesRequest.model_validate(payload)


def _install_mock_transport(
    provider: ChatGPTOAuthProvider,
    handler: Any,
) -> None:
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok_sse_response(*, turn_state: str | None = None) -> httpx.Response:
    headers = {"content-type": "text/event-stream"}
    if turn_state is not None:
        headers["x-codex-turn-state"] = turn_state
    payload = (
        b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
        b'data: {"type":"response.completed","response":{}}\n\n'
    )

    async def _body() -> Any:
        yield payload

    return httpx.Response(200, headers=headers, content=_body())


# ------------------------------------------------------------------ M1: OFF default


def test_session_affinity_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHATGPT_OAUTH_SESSION_AFFINITY", raising=False)
    assert CHATGPT_OAUTH_SESSION_AFFINITY_DEFAULT is False
    assert Settings.model_validate({}).chatgpt_oauth_session_affinity is False
    assert configured_chatgpt_oauth_session_affinity() is False


@pytest.mark.asyncio
async def test_off_mode_is_byte_identical_to_v827(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When unset or false, outbound headers and JSON body match v8.27.0 byte-for-byte."""
    captured: list[tuple[dict[str, str], dict[str, Any]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (dict(request.headers), json.loads(request.content.decode("utf-8")))
        )
        return _ok_sse_response(turn_state="ts-should-be-ignored")

    provider = ChatGPTOAuthProvider(
        _provider_config(),
        rate_limiter=passthrough_rate_limiter(),
        account_id="acct_off",
    )
    _install_mock_transport(provider, _handler)
    install_fingerprint({"x-claude-code-session-id": "root-convo-off"})

    # Pre-seed TurnStateCache to prove OFF ignores cached turn state completely.
    creds = ChatGPTOAuthCredentials(access_token="test_token", account_id="acct_off")
    get_chatgpt_turn_state_cache().set(
        turn_state_cache_key(provider._base_url, creds, provider._session_id),
        "ts-preseeded",
    )

    req = _sample_request()
    _set_affinity(None, monkeypatch)
    _ = [chunk async for chunk in provider.stream_response(req)]
    _set_affinity(False, monkeypatch)
    _ = [chunk async for chunk in provider.stream_response(req)]

    assert len(captured) == 2
    expected_headers = {
        k.lower(): v for k, v in _build_headers(creds, provider._session_id).items()
    }
    expected_body = build_chatgpt_oauth_request_body(
        req, reasoning=DEFAULT_REASONING_POLICY
    )
    for raw_headers, body in captured:
        lowered = {k.lower(): v for k, v in raw_headers.items()}
        assert lowered["session-id"] == provider._session_id
        for key, value in expected_headers.items():
            assert lowered[key] == value
        assert "thread-id" not in lowered
        assert "x-client-request-id" not in lowered
        assert "x-codex-turn-state" not in lowered
        assert "prompt_cache_key" not in body
        assert body == expected_body
    # Capturing turn-state on 200 is disabled while OFF.
    assert len(get_chatgpt_turn_state_cache()) == 1


# --------------------------------------------------------- M2/M3/M8: extraction & fallback


def test_header_extraction_precedence_and_bounds() -> None:
    expected_order = (
        "x-claude-code-session-id",
        "x-session-affinity",
        "x-session-id",
        "session-id",
        "session_id",
        "thread-id",
        "anthropic-session-id",
        "x-anthropic-session-id",
        "claude-session-id",
        "x-claude-session-id",
        "x-opencode-session",
    )
    # Present headers in reverse order; highest precedence must still win.
    reversed_headers = {
        name.upper(): f"val-{idx}"
        for idx, name in reversed(list(enumerate(expected_order)))
    }
    for idx, winner in enumerate(expected_order):
        subset = {
            k: v
            for k, v in reversed_headers.items()
            if k.lower() in expected_order[idx:]
        }
        assert chatgpt_session_id_from_headers(subset) == f"val-{idx}", winner

    # Whitespace-only values are skipped, real values are stripped & bounded.
    assert (
        chatgpt_session_id_from_headers(
            {"x-claude-code-session-id": "   ", "session-id": "  abc  "}
        )
        == "abc"
    )
    long_val = chatgpt_session_id_from_headers({"x-claude-code-session-id": "x" * 900})
    assert long_val is not None
    assert len(long_val) == MAX_CHATGPT_SESSION_CHARS
    assert chatgpt_session_id_from_headers({}) is None
    assert chatgpt_session_id_from_headers(None) is None


def test_client_fingerprint_stays_unchanged_for_existing_consumers() -> None:
    install_fingerprint(
        {
            "user-agent": "claude-cli/2.1.0 (external, cli)",
            "x-claude-code-session-id": "cc-root-1",
        }
    )
    fp = fingerprint_from_headers(
        {
            "user-agent": "claude-cli/2.1.0 (external, cli)",
            "x-claude-code-session-id": "cc-root-1",
        }
    )
    assert fp == ClientFingerprint(user_agent="claude-cli/2.1.0 (external, cli)")
    assert fp.session_id is None
    assert fp.opencode_session is None
    assert current_chatgpt_session_id() == "cc-root-1"


def test_seed_precedence_metadata_messages_and_empty_fallback() -> None:
    req_meta_tail = _sample_request(
        metadata={"user_id": "user_abc_account_123_session_sess-9999abcd"}
    )
    assert resolve_conversation_seed(req_meta_tail, header_session_id="") == (
        "metadata",
        "sess-9999abcd",
    )

    req_meta_sid = _sample_request(metadata={"session_id": "meta-sid-42"})
    assert resolve_conversation_seed(req_meta_sid, header_session_id="") == (
        "metadata",
        "meta-sid-42",
    )

    # Multi-turn message fallback hashes system + first user turn identically across turns.
    turn1 = _sample_request(
        system=[{"type": "text", "text": "System prompt"}],
        user_text="Initial task",
    )
    turn2 = _sample_request(
        system=[{"type": "text", "text": "System prompt"}],
        user_text="Initial task",
        extra_messages=[
            {"role": "assistant", "content": "Working on it."},
            {"role": "user", "content": "Continue please."},
        ],
    )
    aff1 = derive_conversation_affinity(
        turn1, fallback_session_id="prov-1", header_session_id=""
    )
    aff2 = derive_conversation_affinity(
        turn2, fallback_session_id="prov-2", header_session_id=""
    )
    assert aff1.source == "messages"
    assert aff1.session_id == aff2.session_id
    assert aff1.thread_id == aff2.thread_id
    assert aff1.prompt_cache_key == aff2.prompt_cache_key

    # Completely empty request falls back to provider instance UUID.
    empty_req = MessagesRequest.model_validate(
        {"model": "gpt-5", "messages": [{"role": "user", "content": "   "}]}
    )
    assert resolve_conversation_seed(empty_req, header_session_id="") is None
    empty_aff1 = derive_conversation_affinity(
        empty_req, fallback_session_id="prov-uuid-1", header_session_id=""
    )
    empty_aff2 = derive_conversation_affinity(
        empty_req, fallback_session_id="prov-uuid-2", header_session_id=""
    )
    assert empty_aff1.source == "fallback"
    assert empty_aff1.session_id == "prov-uuid-1"
    assert empty_aff2.session_id == "prov-uuid-2"
    assert empty_aff1.prompt_cache_key != empty_aff2.prompt_cache_key


# ------------------------------------------------------ M2/M3/M4: ON wire & subagent sharing


@pytest.mark.asyncio
async def test_on_mode_subagent_sharing_distinct_convos_and_cache_key_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_affinity(True, monkeypatch)
    sent: list[tuple[dict[str, str], dict[str, Any]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append(
            (dict(request.headers), json.loads(request.content.decode("utf-8")))
        )
        return _ok_sse_response()

    provider_a = ChatGPTOAuthProvider(
        _provider_config(),
        rate_limiter=passthrough_rate_limiter(),
        account_id="acct_1",
    )
    provider_b = ChatGPTOAuthProvider(
        _provider_config(),
        rate_limiter=passthrough_rate_limiter(),
        account_id="acct_1",
    )
    _install_mock_transport(provider_a, _handler)
    _install_mock_transport(provider_b, _handler)

    # Parent and subagent share root x-claude-code-session-id (even across provider instances).
    install_fingerprint(
        {
            "x-claude-code-session-id": "root-workspace-/secret/repo/path-"
            + ("z" * 200),
            "x-claude-code-agent-id": "main",
        }
    )
    _ = [
        chunk
        async for chunk in provider_a.stream_response(
            _sample_request(user_text="parent turn")
        )
    ]

    install_fingerprint(
        {
            "x-claude-code-session-id": "root-workspace-/secret/repo/path-"
            + ("z" * 200),
            "x-claude-code-agent-id": "subagent-99",
        }
    )
    _ = [
        chunk
        async for chunk in provider_b.stream_response(
            _sample_request(user_text="subagent turn")
        )
    ]

    # Different conversation gets distinct session-id, thread-id, and prompt_cache_key.
    install_fingerprint({"x-claude-code-session-id": "root-other-session"})
    _ = [
        chunk
        async for chunk in provider_a.stream_response(
            _sample_request(user_text="other turn")
        )
    ]

    assert len(sent) == 3
    h_parent, b_parent = sent[0]
    h_sub, b_sub = sent[1]
    h_other, b_other = sent[2]

    # Canonical UUIDv5 session-id == thread-id, shared between parent & subagent.
    assert uuid.UUID(h_parent["session-id"])
    assert h_parent["session-id"] == h_parent["thread-id"]
    assert h_parent["session-id"] == h_sub["session-id"]
    assert h_parent["thread-id"] == h_sub["thread-id"]
    assert h_parent["session-id"] != provider_a._session_id

    # Distinct per-request correlation UUID4s.
    assert uuid.UUID(h_parent["x-client-request-id"]).version == 4
    assert uuid.UUID(h_sub["x-client-request-id"]).version == 4
    assert h_parent["x-client-request-id"] != h_sub["x-client-request-id"]

    # Bounded, opaque prompt_cache_key != session-id.
    key_parent = b_parent["prompt_cache_key"]
    key_sub = b_sub["prompt_cache_key"]
    assert key_parent == key_sub
    assert 1 <= len(key_parent) <= PROMPT_CACHE_KEY_MAX_CHARS
    assert key_parent != h_parent["session-id"]
    assert "/secret/repo/path" not in key_parent
    assert "/secret/repo/path" not in h_parent["session-id"]

    # Other conversation is isolated.
    assert h_other["session-id"] != h_parent["session-id"]
    assert h_other["thread-id"] != h_parent["thread-id"]
    assert b_other["prompt_cache_key"] != key_parent
    assert 1 <= len(b_other["prompt_cache_key"]) <= PROMPT_CACHE_KEY_MAX_CHARS


@pytest.mark.asyncio
async def test_on_vs_off_diff_is_strictly_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[dict[str, str], dict[str, Any], int]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                dict(request.headers),
                json.loads(request.content.decode("utf-8")),
                len(request.content),
            )
        )
        return _ok_sse_response()

    provider = ChatGPTOAuthProvider(
        _provider_config(),
        rate_limiter=passthrough_rate_limiter(),
        account_id="acct_diff",
    )
    _install_mock_transport(provider, _handler)
    install_fingerprint({"x-claude-code-session-id": "diff-session"})
    req = _sample_request()

    _set_affinity(False, monkeypatch)
    _ = [chunk async for chunk in provider.stream_response(req)]
    _set_affinity(True, monkeypatch)
    _ = [chunk async for chunk in provider.stream_response(req)]

    off_headers, off_body, off_len = captured[0]
    on_headers, on_body, on_len = captured[1]

    off_body_with_key = dict(off_body)
    off_body_with_key["prompt_cache_key"] = on_body["prompt_cache_key"]
    assert on_body == off_body_with_key
    assert int(off_headers["content-length"]) == off_len
    assert int(on_headers["content-length"]) == on_len

    added_or_changed = {
        k
        for k in (set(on_headers) | set(off_headers)) - {"content-length"}
        if on_headers.get(k) != off_headers.get(k)
    }
    assert added_or_changed == {"session-id", "thread-id", "x-client-request-id"}


# ---------------------------------------------- M5/M6/M7: TurnStateCache & account fencing


def test_turn_state_cache_ttl_lru_and_sanitisation() -> None:
    now = 100.0
    cache = TurnStateCache(
        ttl_seconds=TURN_STATE_TTL_SECONDS, max_entries=2, clock=lambda: now
    )

    assert TURN_STATE_TTL_SECONDS == 7200.0
    assert TURN_STATE_MAX_ENTRIES == 1024
    assert normalise_turn_state("  ts-valid  ") == "ts-valid"
    assert normalise_turn_state("   ") is None
    assert normalise_turn_state("a\nb") is None
    assert normalise_turn_state("a\rb") is None
    assert normalise_turn_state("non-ascii-\u00e9") is None
    assert normalise_turn_state("x" * 513) is None

    k1 = ("https://chatgpt.com/backend-api", "acct_1", "s1")
    k2 = ("https://chatgpt.com/backend-api", "acct_1", "s2")
    k3 = ("https://chatgpt.com/backend-api", "acct_1", "s3")
    assert cache.set(k1, " ts-1 ") is True
    assert cache.set(k2, "ts-2") is True
    # Touch k1 so k2 becomes LRU.
    assert cache.get(k1) == "ts-1"
    assert cache.set(k3, "ts-3") is True
    assert cache.get(k2) is None
    assert cache.get(k1) == "ts-1"
    assert cache.get(k3) == "ts-3"

    # At +7199s still valid; at +7200s expired.
    now += 7199.0
    assert cache.get(k1) == "ts-1"
    now += 7200.0
    assert cache.get(k1) is None
    assert cache.get(k3) is None

    # Account scope key falls back to token hash when account_id is empty.
    c1 = ChatGPTOAuthCredentials(access_token="tok-alpha", account_id="")
    c2 = ChatGPTOAuthCredentials(access_token="tok-beta", account_id="")
    assert account_scope_key(c1).startswith("tok:")
    assert account_scope_key(c1) != account_scope_key(c2)


@pytest.mark.asyncio
async def test_turn_state_account_fencing_base_url_isolation_and_error_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_affinity(True, monkeypatch)
    seen_headers: list[dict[str, str]] = []
    status_box = [200]
    turn_state_box: list[str | None] = ["ts-acct-a"]

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        if status_box[0] >= 400:
            headers = {"x-codex-turn-state": "ts-poison-on-error"}
            return httpx.Response(status_box[0], headers=headers, json={"error": "bad"})
        return _ok_sse_response(turn_state=turn_state_box[0])

    p_acct_a = ChatGPTOAuthProvider(
        _provider_config(api_key="token-a"),
        rate_limiter=passthrough_rate_limiter(),
        account_id="acct_A",
    )
    p_acct_b = ChatGPTOAuthProvider(
        _provider_config(api_key="token-b"),
        rate_limiter=passthrough_rate_limiter(),
        account_id="acct_B",
    )
    p_other_base = ChatGPTOAuthProvider(
        _provider_config(
            api_key="token-a", base_url="https://alt.chatgpt.local/backend-api/"
        ),
        rate_limiter=passthrough_rate_limiter(),
        account_id="acct_A",
    )
    for prov in (p_acct_a, p_acct_b, p_other_base):
        _install_mock_transport(prov, _handler)

    install_fingerprint({"x-claude-code-session-id": "shared-root"})
    req = _sample_request()

    # Turn 1 on Account A captures ts-acct-a.
    _ = [chunk async for chunk in p_acct_a.stream_response(req)]
    assert "x-codex-turn-state" not in seen_headers[0]

    # Turn 2 on Account A echoes ts-acct-a.
    turn_state_box[0] = None
    _ = [chunk async for chunk in p_acct_a.stream_response(req)]
    assert seen_headers[1]["x-codex-turn-state"] == "ts-acct-a"

    # Account B on the SAME conversation must NOT receive Account A's turn state.
    _ = [chunk async for chunk in p_acct_b.stream_response(req)]
    assert "x-codex-turn-state" not in seen_headers[2]

    # Different base_url on Account A must NOT receive the first host's turn state.
    _ = [chunk async for chunk in p_other_base.stream_response(req)]
    assert "x-codex-turn-state" not in seen_headers[3]

    # Non-2xx (429) on Account A must NOT commit ts-poison-on-error and MUST clear ts-acct-a.
    status_box[0] = 429
    with pytest.raises(ExecutionFailure):
        _ = [chunk async for chunk in p_acct_a.stream_response(req)]
    assert seen_headers[4]["x-codex-turn-state"] == "ts-acct-a"

    status_box[0] = 200
    _ = [chunk async for chunk in p_acct_a.stream_response(req)]
    assert "x-codex-turn-state" not in seen_headers[5]


@pytest.mark.asyncio
async def test_401_refresh_clears_turn_state_and_rebuilds_affinity_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from my_claude_code.config.constants import (
        CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
    )
    from my_claude_code.providers.chatgpt_oauth import provider as provider_module

    _set_affinity(True, monkeypatch)
    provider = ChatGPTOAuthProvider(
        _provider_config(api_key=CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE),
        rate_limiter=passthrough_rate_limiter(),
    )
    rejected = ChatGPTOAuthCredentials(
        access_token="access_old",
        account_id="acct_1",
        refresh_token="refresh_old",
        source_name="fcc-managed",
    )
    refreshed = ChatGPTOAuthCredentials(
        access_token="access_new",
        account_id="acct_1",
        refresh_token="refresh_new",
        source_name="fcc-managed",
    )
    monkeypatch.setattr(
        provider_module, "load_chatgpt_oauth_credentials", lambda **kwargs: rejected
    )
    monkeypatch.setattr(
        provider_module,
        "force_refresh_managed_chatgpt_oauth_credentials",
        lambda account_id=None: refreshed,
    )

    install_fingerprint({"x-claude-code-session-id": "root-401"})
    req = _sample_request()
    aff = derive_conversation_affinity(req, fallback_session_id=provider._session_id)
    get_chatgpt_turn_state_cache().set(
        turn_state_cache_key(provider._base_url, rejected, aff.session_id),
        "ts-stale-before-401",
    )

    unauthorized = MagicMock(status_code=401)
    unauthorized.aclose = AsyncMock()

    async def _raw_stream() -> Any:
        yield b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        yield b'data: {"type":"response.completed","response":{}}\n\n'

    success = MagicMock(status_code=200)
    success.aiter_raw = _raw_stream
    success.aclose = AsyncMock()
    provider._send_stream_request = AsyncMock(side_effect=[unauthorized, success])

    chunks = [chunk async for chunk in provider.stream_response(req)]
    assert any("ok" in chunk for chunk in chunks)
    first_headers = provider._send_stream_request.await_args_list[0].kwargs["headers"]
    second_headers = provider._send_stream_request.await_args_list[1].kwargs["headers"]
    assert first_headers["x-codex-turn-state"] == "ts-stale-before-401"
    assert "x-codex-turn-state" not in second_headers
    assert second_headers["Authorization"] == "Bearer access_new"
    assert second_headers["session-id"] == aff.session_id
    assert second_headers["thread-id"] == aff.thread_id
    assert second_headers["x-client-request-id"] != first_headers["x-client-request-id"]


def test_preflight_stream_never_emits_affinity_or_cache_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from my_claude_code.providers.chatgpt_oauth import provider as provider_module

    _set_affinity(True, monkeypatch)
    install_fingerprint({"x-claude-code-session-id": "root-preflight"})
    seen_kwargs: list[dict[str, Any]] = []
    original = provider_module.build_chatgpt_oauth_request_body

    def _spy(
        request: MessagesRequest, *, reasoning: Any, **kwargs: Any
    ) -> dict[str, Any]:
        seen_kwargs.append(kwargs)
        return original(request, reasoning=reasoning, **kwargs)

    monkeypatch.setattr(provider_module, "build_chatgpt_oauth_request_body", _spy)
    provider = ChatGPTOAuthProvider(
        _provider_config(), rate_limiter=passthrough_rate_limiter()
    )
    provider.preflight_stream(_sample_request(), reasoning=DEFAULT_REASONING_POLICY)
    assert len(seen_kwargs) == 1
    assert seen_kwargs[0].get("prompt_cache_key") is None
