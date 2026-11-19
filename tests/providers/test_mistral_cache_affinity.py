"""Tests for Mistral automatic caching, built-in affinity, and custom card affinity (8.29.0)."""

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.api.model_admin import prompt_cache_lifetimes_payload
from my_claude_code.config import settings as config_settings
from my_claude_code.config.constants import MISTRAL_PROMPT_CACHE_AFFINITY_DEFAULT
from my_claude_code.config.prompt_cache_lifetimes import (
    AUTOMATIC_PROMPT_CACHE,
    AUTOMATIC_PROMPT_CACHE_PROVIDERS,
    chat_prompt_cache_family_id,
)
from my_claude_code.config.provider_registry import (
    CUSTOM_CACHE_AFFINITY_MODES,
    DEFAULT_CUSTOM_CACHE_AFFINITY,
    ProviderRegistry,
    normalize_custom_cache_affinity,
)
from my_claude_code.config.settings import (
    Settings,
    configured_mistral_prompt_cache_affinity,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.client_fingerprint import (
    ClientFingerprint,
    current_affinity_session_id,
    fingerprint_from_headers,
    install_fingerprint,
)
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY
from my_claude_code.core.wire_capture import _WIRE_TRACE, install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.mistral import MistralProvider
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    CacheAffinitySpec,
    OpenAIChatProvider,
    create_openai_chat_provider,
    profile_with_cache_affinity,
    resolve_affinity_digest,
)
from my_claude_code.providers.openai_chat.opencode_identity import (
    OPENCODE_CLIENT_IDENTITY,
)
from my_claude_code.providers.openai_chat.prompt_cache import chat_prompt_cache_choice
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.runtime.factory import _instantiate_leaf
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter

MISTRAL_KEY = "sk-mistral-test-" + "a" * 24
CODESTRAL_KEY = "sk-codestral-test-" + "b" * 24
CUSTOM_KEY = "sk-custom-test-" + "c" * 24

CHAT_SSE = (
    b'data: {"id":"c1","object":"chat.completion.chunk","created":0,'
    b'"model":"devstral-small-latest",'
    b'"choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},'
    b'"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)

RESPONSES_SSE = (
    b'data: {"type":"response.created","response":{"id":"resp_1","model":"m"}}\n\n'
    b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
    b'data: {"type":"response.completed","response":{"id":"resp_1",'
    b'"usage":{"input_tokens":5,"output_tokens":1}}}\n\n'
)


def _set_mistral_affinity(
    monkeypatch: pytest.MonkeyPatch, enabled: bool | None
) -> None:
    if enabled is None:
        monkeypatch.delenv("MISTRAL_PROMPT_CACHE_AFFINITY", raising=False)
    else:
        monkeypatch.setenv(
            "MISTRAL_PROMPT_CACHE_AFFINITY", "true" if enabled else "false"
        )
    config_settings.get_settings.cache_clear()


def _mistral_provider(
    provider_id: str = "mistral",
    handler: Any = None,
) -> MistralProvider:
    provider = MistralProvider(
        ProviderConfig(
            api_key=MISTRAL_KEY,
            base_url="https://api.mistral.ai/v1",
            rate_limit=10,
            rate_window=60,
            max_concurrency=5,
        ),
        rate_limiter=passthrough_rate_limiter(),
        provider_id=provider_id,
    )
    if handler is not None:
        provider._client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
    return provider


def _chat_response_handler(
    captured: list[tuple[dict[str, str], dict[str, Any]]],
):
    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                {k.lower(): v for k, v in request.headers.items()},
                json.loads(request.content.decode("utf-8")),
            )
        )

        async def _sse():
            yield CHAT_SSE

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(),
        )

    return _handler


def _responses_handler(
    captured: list[tuple[dict[str, str], dict[str, Any]]],
):
    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                {k.lower(): v for k, v in request.headers.items()},
                json.loads(request.content.decode("utf-8")),
            )
        )

        async def _sse():
            yield RESPONSES_SSE

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(),
        )

    return _handler


# ---------------------------------------------------------------------------
# Item 1: AUTOMATIC_PROMPT_CACHE_PROVIDERS (M1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider_id", ["mistral", "mistral_codestral", "mistral_vibe"]
)
def test_mistral_family_is_in_automatic_prompt_cache_providers(
    provider_id: str,
) -> None:
    assert provider_id in AUTOMATIC_PROMPT_CACHE_PROVIDERS
    assert chat_prompt_cache_family_id(provider_id, "mistral-large-latest") == (
        AUTOMATIC_PROMPT_CACHE
    )
    # Zero wire marker added on automatic providers.
    req = make_messages_request("mistral-large-latest")
    assert chat_prompt_cache_choice(req, provider_id=provider_id) is None
    payload = prompt_cache_lifetimes_payload()
    assert provider_id in payload["automatic_providers"]
    assert payload["automatic_providers"] == sorted(payload["automatic_providers"])


# ---------------------------------------------------------------------------
# Item 3: Opaque restart-stable root-session digest (M4, M8)
# ---------------------------------------------------------------------------


def test_affinity_digest_is_opaque_bounded_and_restart_stable() -> None:
    req = make_messages_request("mistral-large-latest", system="System instructions")
    try:
        install_fingerprint({"x-claude-code-session-id": "cc-secret-uuid-9988"})
        digest_a = resolve_affinity_digest(req)
        digest_b = resolve_affinity_digest(req)
    finally:
        install_fingerprint(None)

    assert digest_a is not None
    assert digest_a == digest_b
    assert digest_a.startswith("mcc-aff-")
    assert len(digest_a) == 40
    assert len(digest_a) <= 64
    assert "cc-secret-uuid-9988" not in digest_a
    assert "System instructions" not in digest_a
    expected_hex = hashlib.sha256(
        b"mcc:cache-affinity:v1:header:cc-secret-uuid-9988"
    ).hexdigest()[:32]
    assert digest_a == f"mcc-aff-{expected_hex}"


def test_parent_and_subagent_share_root_session_digest_even_with_different_prompts() -> (
    None
):
    parent_req = make_messages_request(
        "mistral-large-latest",
        system="Parent agent prompt",
        messages=[{"role": "user", "content": "Investigate repo"}],
    )
    subagent_req = make_messages_request(
        "mistral-large-latest",
        system="Subagent prompt",
        messages=[{"role": "user", "content": "Inspect one file"}],
    )
    try:
        install_fingerprint(
            {
                "x-claude-code-session-id": "root-session-alpha",
                "x-claude-code-agent-id": "agent-1",
            }
        )
        parent_digest = resolve_affinity_digest(parent_req)

        install_fingerprint(
            {
                "x-claude-code-session-id": "root-session-alpha",
                "x-claude-code-agent-id": "agent-2",
            }
        )
        subagent_digest = resolve_affinity_digest(subagent_req)
    finally:
        install_fingerprint(None)

    assert parent_digest is not None
    assert parent_digest == subagent_digest


def test_affinity_digest_precedence_and_empty_request_returns_none() -> None:
    empty_req = MessagesRequest.model_validate(
        {
            "model": "mistral-large-latest",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": ""}],
        }
    )
    install_fingerprint(None)
    assert resolve_affinity_digest(empty_req) is None

    # Opening turns fallback when headerless and metadata-less.
    turn1_req = make_messages_request(
        "mistral-large-latest",
        system="You are concise.",
        messages=[{"role": "user", "content": "Hello"}],
    )
    turn2_req = make_messages_request(
        "mistral-large-latest",
        system="You are concise.",
        messages=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Second turn"},
        ],
    )
    d_turn1 = resolve_affinity_digest(turn1_req)
    d_turn2 = resolve_affinity_digest(turn2_req)
    assert d_turn1 is not None and d_turn1 == d_turn2

    # Metadata user_id _session_<id> outranks opening turns.
    meta_uid_req = make_messages_request(
        "mistral-large-latest",
        system="You are concise.",
        messages=[{"role": "user", "content": "Hello"}],
        metadata={"user_id": "user_hash_account_1234_session_abcd1234efgh"},
    )
    d_meta_uid = resolve_affinity_digest(meta_uid_req)
    assert d_meta_uid is not None and d_meta_uid != d_turn1

    # Explicit header outranks metadata.
    try:
        install_fingerprint({"x-session-affinity": "hdr-session-77"})
        assert current_affinity_session_id() == "hdr-session-77"
        d_hdr = resolve_affinity_digest(meta_uid_req)
    finally:
        install_fingerprint(None)
    assert d_hdr is not None and d_hdr != d_meta_uid

    # ClientFingerprint itself stays untouched and blind to x-claude-code-session-id.
    fp = fingerprint_from_headers({"x-claude-code-session-id": "root-only"})
    assert fp == ClientFingerprint()


# ---------------------------------------------------------------------------
# Item 4: Built-in mistral / mistral_vibe vs OFF vs mistral_codestral (M2, M3, M8)
# ---------------------------------------------------------------------------


def test_mistral_prompt_cache_affinity_default_is_false_without_live_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mistral_affinity(monkeypatch, None)
    try:
        assert MISTRAL_PROMPT_CACHE_AFFINITY_DEFAULT is False
        assert configured_mistral_prompt_cache_affinity() is False
    finally:
        config_settings.get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["mistral", "mistral_vibe"])
async def test_builtin_mistral_off_is_byte_identical_and_on_emits_affinity(
    provider_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[dict[str, str], dict[str, Any]]] = []
    provider = _mistral_provider(
        provider_id=provider_id, handler=_chat_response_handler(captured)
    )
    req = make_messages_request("devstral-small-latest", system="Sys")

    try:
        # OFF (default)
        _set_mistral_affinity(monkeypatch, False)
        install_fingerprint({"x-claude-code-session-id": "root-mistral-1"})
        _ = [c async for c in provider.stream_response(req)]
        off_headers, off_body = captured[-1]
        assert "x-affinity" not in off_headers
        assert "prompt_cache_key" not in off_body
        assert provider._profile.client_identity is None

        # Hot-toggle ON without rebuilding the provider!
        _set_mistral_affinity(monkeypatch, True)
        _ = [c async for c in provider.stream_response(req)]
        on_headers, on_body = captured[-1]
        assert "x-affinity" in on_headers
        assert "prompt_cache_key" in on_body
        assert on_headers["x-affinity"] == on_body["prompt_cache_key"]
        assert on_body["prompt_cache_key"].startswith("mcc-aff-")
        assert len(on_body["prompt_cache_key"]) == 40
        # Removing prompt_cache_key restores exact OFF JSON body bytes.
        on_body_stripped = {k: v for k, v in on_body.items() if k != "prompt_cache_key"}
        assert json.dumps(on_body_stripped, sort_keys=True) == json.dumps(
            off_body, sort_keys=True
        )

        # Subagent shares the exact same affinity token via x-claude-code-session-id.
        install_fingerprint(
            {
                "x-claude-code-session-id": "root-mistral-1",
                "x-claude-code-agent-id": "subagent-9",
            }
        )
        sub_req = make_messages_request(
            "devstral-small-latest",
            system="Different subagent prompt",
            messages=[{"role": "user", "content": "Different prompt"}],
        )
        _ = [c async for c in provider.stream_response(sub_req)]
        sub_headers, sub_body = captured[-1]
        assert sub_headers["x-affinity"] == on_headers["x-affinity"]
        assert sub_body["prompt_cache_key"] == on_body["prompt_cache_key"]

        # Different conversation gets a distinct affinity token.
        install_fingerprint({"x-claude-code-session-id": "root-mistral-2"})
        _ = [c async for c in provider.stream_response(req)]
        other_headers, other_body = captured[-1]
        assert other_headers["x-affinity"] != on_headers["x-affinity"]
        assert other_body["prompt_cache_key"] != on_body["prompt_cache_key"]

        # Hot-toggle back OFF -> returns cleanly to zero affinity.
        _set_mistral_affinity(monkeypatch, False)
        _ = [c async for c in provider.stream_response(req)]
        off2_headers, off2_body = captured[-1]
        assert "x-affinity" not in off2_headers
        assert "prompt_cache_key" not in off2_body
        assert json.dumps(off2_body, sort_keys=True) == json.dumps(
            off_body, sort_keys=True
        )
    finally:
        install_fingerprint(None)
        config_settings.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_mistral_codestral_stays_off_even_when_mistral_affinity_is_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[dict[str, str], dict[str, Any]]] = []
    assert OPENAI_CHAT_PROFILES["mistral_codestral"].cache_affinity is None
    assert OPENAI_CHAT_PROFILES["mistral_codestral"].client_identity is None

    provider = create_openai_chat_provider(
        "mistral_codestral",
        ProviderConfig(
            api_key=CODESTRAL_KEY,
            base_url="https://codestral.mistral.ai/v1",
            rate_limit=10,
            rate_window=60,
            max_concurrency=5,
        ),
        passthrough_rate_limiter(),
    )
    provider._client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_chat_response_handler(captured))
    )
    req = make_messages_request("codestral-latest", system="Code")
    try:
        _set_mistral_affinity(monkeypatch, True)
        install_fingerprint({"x-claude-code-session-id": "root-codestral"})
        _ = [c async for c in provider.stream_response(req)]
    finally:
        install_fingerprint(None)
        config_settings.get_settings.cache_clear()

    assert len(captured) == 1
    headers, body = captured[0]
    assert "x-affinity" not in headers
    assert "prompt_cache_key" not in body


# ---------------------------------------------------------------------------
# Item 5: Custom providers matrix (off/body/header/both) + OpenCode/generic safety (M5, M6)
# ---------------------------------------------------------------------------


def test_custom_cache_affinity_registry_round_trip_and_profile_identity(
    tmp_path: Path,
) -> None:
    reg_path = tmp_path / "custom_providers.json"
    registry = ProviderRegistry(reg_path)

    default_entry = registry.add(
        display_name="Default Custom",
        base_url="https://gateway.example/v1",
        api_keys=(CUSTOM_KEY,),
    )
    assert default_entry.cache_affinity == DEFAULT_CUSTOM_CACHE_AFFINITY == "off"
    raw = json.loads(reg_path.read_text(encoding="utf-8"))
    # "off" must not be serialized into custom_providers.json (M6).
    assert "cache_affinity" not in raw["providers"][0]

    for mode in ("body", "header", "both"):
        updated = registry.update(default_entry.provider_id, cache_affinity=mode)
        assert updated.cache_affinity == mode
        desc = registry.descriptor_for(updated)
        assert desc.cache_affinity == mode
        reloaded = ProviderRegistry(reg_path).get(default_entry.provider_id)
        assert reloaded is not None and reloaded.cache_affinity == mode

    # Updating back to "off" removes the key from disk again.
    registry.update(default_entry.provider_id, cache_affinity="off")
    raw_after = json.loads(reg_path.read_text(encoding="utf-8"))
    assert "cache_affinity" not in raw_after["providers"][0]

    # Invalid value raises ValueError on normalize; corrupt disk value downgrades to "off".
    with pytest.raises(ValueError, match="Unknown cache_affinity"):
        normalize_custom_cache_affinity("sticky")
    raw_after["providers"][0]["cache_affinity"] = "future-mode"
    reg_path.write_text(json.dumps(raw_after), encoding="utf-8")
    corrupt_reloaded = ProviderRegistry(reg_path).get(default_entry.provider_id)
    assert corrupt_reloaded is not None and corrupt_reloaded.cache_affinity == "off"

    # profile_with_cache_affinity returns GENERIC_OPENAI_PROFILE unchanged when "off".
    assert profile_with_cache_affinity(GENERIC_OPENAI_PROFILE, "off") is (
        GENERIC_OPENAI_PROFILE
    )
    assert GENERIC_OPENAI_PROFILE.cache_affinity is None
    assert GENERIC_OPENAI_PROFILE.client_identity is None
    assert set(CUSTOM_CACHE_AFFINITY_MODES) == {"off", "body", "header", "both"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "body", "header", "both"])
async def test_custom_provider_cache_affinity_matrix_on_chat_and_responses(
    mode: str, tmp_path: Path
) -> None:
    registry = ProviderRegistry(tmp_path / "custom_providers.json")
    entry = registry.add(
        display_name="Mistral Custom",
        base_url="https://api.mistral.ai/v1",
        api_keys=(CUSTOM_KEY,),
        surfaces=("chat_completions", "responses"),
        cache_affinity=mode,
    )
    desc = registry.descriptor_for(entry)
    settings = Settings.model_validate({})
    leaf = _instantiate_leaf(
        desc,
        ProviderConfig(
            api_key=CUSTOM_KEY,
            base_url=entry.base_url,
            rate_limit=10,
            rate_window=60,
            max_concurrency=5,
        ),
        settings,
        passthrough_rate_limiter(),
    )
    assert isinstance(leaf, OpenAIChatProvider)
    assert leaf._profile.client_identity is None

    chat_captured: list[tuple[dict[str, str], dict[str, Any]]] = []
    leaf._client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_chat_response_handler(chat_captured))
    )

    responses_captured: list[tuple[dict[str, str], dict[str, Any]]] = []
    leaf._responses._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_responses_handler(responses_captured))
    )

    req = make_messages_request("deepseek-v4-1-flash-1017", system="Sys")
    expect_body = mode in ("body", "both")
    expect_header = mode in ("header", "both")

    try:
        install_fingerprint({"x-claude-code-session-id": "root-custom-session"})
        chat_trace = install_wire_trace()
        _ = [c async for c in leaf.stream_response(req)]
        resp_trace = install_wire_trace()
        r_body, r_headers = leaf._responses.build_body(
            req, reasoning=DEFAULT_REASONING_POLICY, max_output_tokens=None
        )
        _ = [
            c
            async for c in leaf._responses.stream(
                req,
                input_tokens=0,
                reasoning=DEFAULT_REASONING_POLICY,
                body=r_body,
                headers=r_headers,
                surface_label="responses (declared)",
            )
        ]
    finally:
        _WIRE_TRACE.set(None)
        install_fingerprint(None)

    assert len(chat_captured) == 1
    chat_headers, chat_body = chat_captured[0]
    assert ("prompt_cache_key" in chat_body) is expect_body
    assert ("x-affinity" in chat_headers) is expect_header

    assert len(responses_captured) == 1
    resp_headers, resp_body = responses_captured[0]
    assert ("prompt_cache_key" in resp_body) is expect_body
    assert ("x-affinity" in resp_headers) is expect_header

    # Both surfaces derive the exact same opaque root-session digest.
    expected_digest = resolve_affinity_digest(
        req, header_session_id="root-custom-session"
    )
    assert expected_digest is not None
    if expect_body:
        assert chat_body["prompt_cache_key"] == expected_digest
        assert resp_body["prompt_cache_key"] == expected_digest
    if expect_header:
        assert chat_headers["x-affinity"] == expected_digest
        assert resp_headers["x-affinity"] == expected_digest

    # Wire capture reflects the sent affinity cleanly and records nothing when off.
    chat_params = chat_trace.requests[0].params
    resp_params = resp_trace.requests[0].params
    assert ("prompt_cache_key" in chat_params) is expect_body
    assert ("cache_affinity" in chat_params) is expect_header
    assert ("prompt_cache_key" in resp_params) is expect_body
    assert ("cache_affinity" in resp_params) is expect_header


@pytest.mark.asyncio
async def test_responses_transport_never_overwrites_opencode_identity() -> None:
    captured: list[tuple[dict[str, str], dict[str, Any]]] = []
    transport = ResponsesTransport(
        ProviderConfig(
            api_key="sk-opencode-test",
            base_url="https://opencode.ai/zen/v1",
            rate_limit=10,
            rate_window=60,
            max_concurrency=5,
        ),
        base_url="https://opencode.ai/zen/v1",
        provider_name="OPENCODE",
        identity=OPENCODE_CLIENT_IDENTITY,
        api_key="sk-opencode-test",
        rate_limiter=passthrough_rate_limiter(),
        provider_id="opencode",
        # Even if mistakenly passed alongside a ClientIdentity, identity wins (M5).
        cache_affinity=CacheAffinitySpec(header_name="x-affinity", send_body_key=True),
    )
    transport._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_responses_handler(captured))
    )
    req = make_messages_request("gpt-5", system="OpenCode system")
    try:
        install_fingerprint({"x-claude-code-session-id": "root-oc-1"})
        body, headers = transport.build_body(
            req, reasoning=DEFAULT_REASONING_POLICY, max_output_tokens=None
        )
        _ = [
            c
            async for c in transport.stream(
                req,
                input_tokens=0,
                reasoning=DEFAULT_REASONING_POLICY,
                body=body,
                headers=headers,
                surface_label="responses",
            )
        ]
    finally:
        install_fingerprint(None)

    assert len(captured) == 1
    sent_headers, sent_body = captured[0]
    assert "x-affinity" not in sent_headers
    assert "x-opencode-session" in sent_headers
    assert sent_headers["x-opencode-session"].startswith("ses_")
    assert sent_body["prompt_cache_key"] == sent_headers["x-opencode-session"]
