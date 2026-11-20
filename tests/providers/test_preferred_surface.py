"""Preferred wire surface per provider & model (v8.32.0, PR 1).

Proves the PR 1 contract without live upstream calls:

1. Off by default (IV.7): unset ``preferred_surface`` produces the identical
   ``resolve_surface_ladder`` output across every client family and profile.
2. Precedence: ``pin (response_surface) > preferred_surface (model > provider) >
   learned > measured > registry > marketplace > floor``, with ``null`` on a
   model row cancelling a provider preference, and an undeclared surface on one
   model ignored cleanly rather than collapsing to ``UNSERVABLE``.
3. Non-body enforcement: ``preferred_surface`` never leaks into any upstream
   Chat Completions, Responses, or Messages JSON body.
4. Q4: a declared-but-unproved door is saved, resolves first with
   ``guessed=True``, probes once, learns on 2xx or falls back inside the same
   attempt on wrong-door evidence.
5. Q9: a contradicting learned fact or refused preferred door never rewrites
   ``model_overrides.json`` and never wipes remaining known doors; records
   ``FACT_PREFERRED_SURFACE_REFUSED`` so subsequent turns avoid repeated probes
   within the 7-day TTL; ``response_surface_payload`` reports
   ``"you prefer X; this host refused it at ...; serving Y"``.
6. Q10 & Q11: ``discovered`` is populated on every multi-door model row even
   with no preference set; single-door providers receive no controls and
   ``POST /admin/api/model-admin/preferred-surface`` enforces 422 validation.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.model_admin import (
    FACT_KIND_LABELS,
    PAGE_TABLE_UNLISTED_FACT_KINDS,
    response_surface_payload,
    speakable_surfaces,
    with_preferred_surface_row,
)
from my_claude_code.application.model_metadata import (
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.config.model_overrides import (
    ALLOWED_OVERRIDE_PARAMETERS,
    NON_BODY_OVERRIDE_PARAMETERS,
    PREFERRED_SURFACE_OVERRIDE,
    PREFERRED_SURFACE_VALUES,
    ModelParameterOverrides,
    apply_model_parameter_overrides,
    current_model_overrides,
    load_model_overrides,
    preferred_surface_problem,
    reset_model_overrides_cache,
    save_model_overrides,
)
from my_claude_code.config.provider_registry import CUSTOM_PROVIDER_SURFACES
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.client_family import ClientFamily
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
    learned_surfaces,
    resolve_surface_ladder,
    surface_profile_for,
)
from my_claude_code.providers.openai_chat.response_surface import (
    preferred_surface_of,
    preferred_surface_refusal_fact,
    remember_response_surfaces,
)
from my_claude_code.providers.recovery import (
    ALLOWED_FACT_KINDS,
    FACT_PREFERRED_SURFACE_REFUSED,
    INFERRED_FACT_TTL_SECONDS,
    learned_fact_store,
)
from my_claude_code.providers.recovery.facts import FACT_TTL_SECONDS
from my_claude_code.providers.runtime import models_dev
from tests.api.support import create_test_app, provider_manager_for_app
from tests.providers.support import passthrough_rate_limiter

HERE = Path(__file__).parent
BUCKETS_PATH = HERE / "opencode_go_registry_buckets.json"

GO = OPENAI_CHAT_PROFILES["opencode_go"]
ZEN = OPENAI_CHAT_PROFILES["opencode"]
MISTRAL = surface_profile_for("mistral")
GROQ = OPENAI_CHAT_PROFILES["groq"]
assert MISTRAL is not None

GO_BASE = "https://opencode.ai/inference/go/v1"
MUSE = "muse-spark-1.3-contributor"
DEEPSEEK = "deepseek-v4.1-flash"
MINIMAX = "minimax-m3"
GOOGLE = "gemini-4-argon-contributor"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    models_dev.write_models_dev_cache(
        json.loads(BUCKETS_PATH.read_text(encoding="utf-8"))
    )
    models_dev.reset_models_dev_payload_cache()
    reset_model_overrides_cache()
    learned_fact_store().forget_all()


def _save_overrides(document: dict[str, Any]) -> None:
    save_model_overrides(ModelParameterOverrides.from_document(document))


def _request(model: str = DEEPSEEK) -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=1024,
        messages=[Message(role="user", content="hello")],
    )


async def _drain(stream: Any) -> list[str]:
    return [chunk async for chunk in stream]


def _chat_sse_bytes() -> bytes:
    events = [
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": DEEPSEEK,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}],
        },
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": DEEPSEEK,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    ]
    return (
        "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode("utf-8")
        + b"data: [DONE]\n\n"
    )


def _messages_sse_bytes() -> bytes:
    lines = [
        'event: message_start\ndata: {"type":"message_start","message":{"id":"m1","type":"message","role":"assistant","model":"deepseek-v4.1-flash","content":[],"stop_reason":null,"usage":{"input_tokens":10,"output_tokens":0}}}\n\n',
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n',
        'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    return "".join(lines).encode("utf-8")


def _responses_sse_bytes() -> bytes:
    lines = [
        'event: response.created\ndata: {"type":"response.created","response":{"id":"r1","status":"in_progress"}}\n\n',
        'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":0,"item":{"id":"msg1","type":"message","role":"assistant","content":[]}}\n\n',
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","item_id":"msg1","output_index":0,"content_index":0,"delta":"ok"}\n\n',
        'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r1","status":"completed","usage":{"input_tokens":10,"output_tokens":2}}}\n\n',
    ]
    return "".join(lines).encode("utf-8")


# --------------------------------------------------------------------------
# 1. Config / non-body contract / validation
# --------------------------------------------------------------------------


def test_preferred_surface_is_a_non_body_key_matching_custom_surfaces() -> None:
    assert PREFERRED_SURFACE_OVERRIDE == "preferred_surface"
    assert PREFERRED_SURFACE_OVERRIDE in NON_BODY_OVERRIDE_PARAMETERS
    assert PREFERRED_SURFACE_OVERRIDE not in ALLOWED_OVERRIDE_PARAMETERS
    assert PREFERRED_SURFACE_VALUES == CUSTOM_PROVIDER_SURFACES
    assert "unservable" not in PREFERRED_SURFACE_VALUES


@pytest.mark.parametrize(
    "bad_value",
    ["unservable", "gemini", "", "   ", 123, True, False, ["responses"], {"a": 1}],
)
def test_invalid_preferred_surface_values_are_rejected_and_ignored(
    bad_value: object,
) -> None:
    assert preferred_surface_problem(bad_value) is not None
    table = ModelParameterOverrides.from_document(
        {
            "providers": {
                "opencode_go": {"preferred_surface": bad_value, "top_p": 0.9}
            },
            "models": {
                "opencode_go/deepseek-v4.1-flash": {
                    "preferred_surface": bad_value,
                    "temperature": 0.2,
                }
            },
        }
    )
    assert table.providers == {"opencode_go": {"top_p": 0.9}}
    assert table.models == {"opencode_go/deepseek-v4.1-flash": {"temperature": 0.2}}


def test_preferred_surface_never_leaks_into_request_bodies() -> None:
    table = ModelParameterOverrides.from_document(
        {
            "providers": {"opencode_go": {"preferred_surface": "chat_completions"}},
            "models": {
                "opencode_go/deepseek-v4.1-flash": {
                    "preferred_surface": "messages",
                    "response_surface": "chat_completions",
                    "temperature": 0.3,
                }
            },
        }
    )
    body = {"model": DEEPSEEK}
    applied = apply_model_parameter_overrides(
        body,
        provider_id="opencode_go",
        model_ref="opencode_go/deepseek-v4.1-flash",
        overrides=table,
    )
    assert applied == {"temperature": 0.3}
    assert PREFERRED_SURFACE_OVERRIDE not in body
    assert body == {"model": DEEPSEEK, "temperature": 0.3}


def _sse_response(payload: bytes) -> httpx.Response:
    async def _body() -> Any:
        yield payload

    return httpx.Response(
        200, content=_body(), headers={"content-type": "text/event-stream"}
    )


def _wire_mock_http(provider: Any, handler: Any) -> httpx.AsyncClient:
    """Attach an httpx.MockTransport to Chat, Responses, and Messages transports."""

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider._client._client = mock_http
    if provider._responses is not None:
        provider._responses._client = mock_http
    if provider._messages is not None:
        provider._messages._provider._client = mock_http
    return mock_http


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_surface", ["chat_completions", "responses", "messages"]
)
async def test_preferred_surface_never_reaches_outbound_http_json_body(
    target_surface: str,
) -> None:
    _save_overrides(
        {
            "providers": {"opencode_go": {"preferred_surface": target_surface}},
            "models": {"opencode_go/m": {"preferred_surface": target_surface}},
        }
    )
    remember_response_surfaces("opencode_go", "m", [ResponseSurface(target_surface)])
    seen_bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(req.content.decode("utf-8")))
        if target_surface == "messages":
            return _sse_response(_messages_sse_bytes())
        if target_surface == "responses":
            return _sse_response(_responses_sse_bytes())
        return _sse_response(_chat_sse_bytes())

    config = ProviderConfig(api_key="test-key", base_url=GO_BASE)
    provider = create_openai_chat_provider(
        "opencode_go", config, passthrough_rate_limiter(), profile=GO
    )
    mock_http = _wire_mock_http(provider, handler)
    try:
        chunks = await _drain(provider.stream_response(_request("m")))
    finally:
        await mock_http.aclose()
    assert chunks
    assert seen_bodies
    for payload in seen_bodies:
        assert PREFERRED_SURFACE_OVERRIDE not in payload
        assert "response_surface" not in payload


def test_with_preferred_surface_row_writes_and_clears_both_scopes() -> None:
    empty = ModelParameterOverrides()
    with_provider = with_preferred_surface_row(
        empty, scope="provider", key="opencode_go", surface="chat_completions"
    )
    assert with_provider.providers == {
        "opencode_go": {"preferred_surface": "chat_completions"}
    }
    with_model = with_preferred_surface_row(
        with_provider,
        scope="model",
        key="opencode_go/deepseek-v4.1-flash",
        surface="messages",
    )
    assert with_model.models == {
        "opencode_go/deepseek-v4.1-flash": {"preferred_surface": "messages"}
    }
    cleared_model = with_preferred_surface_row(
        with_model, scope="model", key="opencode_go/deepseek-v4.1-flash", surface=""
    )
    assert cleared_model.models == {}
    cleared_provider = with_preferred_surface_row(
        cleared_model, scope="provider", key="opencode_go", surface=None
    )
    assert cleared_provider.providers == {}


# --------------------------------------------------------------------------
# 2. IV.7 unset equality + precedence
# --------------------------------------------------------------------------


def test_unset_preferred_surface_matches_base_ladder_across_all_families() -> None:
    for family in ClientFamily:
        for model_id in (MUSE, MINIMAX, DEEPSEEK, GOOGLE):
            ladder = resolve_surface_ladder(
                "opencode_go",
                model_id,
                client_family=family,
                registry_provider=GO.surface_registry_provider,
                declared=GO.response_surfaces,
                no_information=GO.no_information_surface,
                by_upstream=GO.surface_by_upstream,
                measured=GO.measured_surfaces,
                optimistic=GO.optimistic_first_try,
            )
            assert ladder.candidates
            # Unset means no override source is injected.
            assert all(
                c.source is not ResponseSurfaceSource.OVERRIDE
                for c in ladder.candidates
            )
        # Single-door profile resolves identically to default.
        single = resolve_surface_ladder(
            "groq",
            "llama-3.3-70b",
            client_family=family,
            declared=GROQ.response_surfaces,
        )
        assert len(single.candidates) == 1
        assert single.primary.surface is ResponseSurface.CHAT_COMPLETIONS
        assert single.primary.source is ResponseSurfaceSource.DEFAULT
        assert single.primary.guessed is False


def test_precedence_pin_beats_model_preferred_beats_provider_preferred_beats_ladder() -> (
    None
):
    # Provider prefers chat_completions, model prefers responses -> model wins.
    _save_overrides(
        {
            "providers": {"opencode_go": {"preferred_surface": "chat_completions"}},
            "models": {f"opencode_go/{MUSE}": {"preferred_surface": "responses"}},
        }
    )
    ladder = resolve_surface_ladder(
        "opencode_go",
        MUSE,
        client_family=ClientFamily.ANTHROPIC,
        registry_provider=GO.surface_registry_provider,
        declared=GO.response_surfaces,
        no_information=GO.no_information_surface,
        measured=GO.measured_surfaces,
    )
    assert [c.surface for c in ladder.candidates] == [
        ResponseSurface.RESPONSES,
        ResponseSurface.MESSAGES,
        ResponseSurface.CHAT_COMPLETIONS,
    ]
    assert ladder.primary.guessed is False

    # Add response_surface pin -> pin wins exclusively and collapses ladder.
    _save_overrides(
        {
            "providers": {"opencode_go": {"preferred_surface": "chat_completions"}},
            "models": {
                f"opencode_go/{MUSE}": {
                    "preferred_surface": "responses",
                    "response_surface": "messages",
                }
            },
        }
    )
    pinned = resolve_surface_ladder(
        "opencode_go",
        MUSE,
        client_family=ClientFamily.OPENAI,
        registry_provider=GO.surface_registry_provider,
        declared=GO.response_surfaces,
        no_information=GO.no_information_surface,
        measured=GO.measured_surfaces,
    )
    assert [c.surface for c in pinned.candidates] == [ResponseSurface.MESSAGES]
    assert pinned.primary.source is ResponseSurfaceSource.OVERRIDE


def test_provider_preferred_applies_to_model_without_override_and_null_cancels_it() -> (
    None
):
    _save_overrides(
        {
            "providers": {"opencode_go": {"preferred_surface": "responses"}},
        }
    )
    # MUSE has measured {MESSAGES, RESPONSES, CHAT_COMPLETIONS}; under ANTHROPIC normally MESSAGES leads.
    with_provider_pref = resolve_surface_ladder(
        "opencode_go",
        MUSE,
        client_family=ClientFamily.ANTHROPIC,
        registry_provider=GO.surface_registry_provider,
        declared=GO.response_surfaces,
        no_information=GO.no_information_surface,
        measured=GO.measured_surfaces,
    )
    assert [c.surface for c in with_provider_pref.candidates] == [
        ResponseSurface.RESPONSES,
        ResponseSurface.MESSAGES,
        ResponseSurface.CHAT_COMPLETIONS,
    ]

    # Model-level null cancels provider preference -> Follow the client (MESSAGES leads).
    _save_overrides(
        {
            "providers": {"opencode_go": {"preferred_surface": "responses"}},
            "models": {f"opencode_go/{MUSE}": {"preferred_surface": None}},
        }
    )
    cancelled = resolve_surface_ladder(
        "opencode_go",
        MUSE,
        client_family=ClientFamily.ANTHROPIC,
        registry_provider=GO.surface_registry_provider,
        declared=GO.response_surfaces,
        no_information=GO.no_information_surface,
        measured=GO.measured_surfaces,
    )
    assert [c.surface for c in cancelled.candidates] == [
        ResponseSurface.MESSAGES,
        ResponseSurface.RESPONSES,
        ResponseSurface.CHAT_COMPLETIONS,
    ]


def test_undeclared_preferred_surface_is_ignored_cleanly_not_unservable() -> None:
    # Mistral declares (CHAT_COMPLETIONS, RESPONSES) — not MESSAGES.
    assert MISTRAL is not None
    _save_overrides({"providers": {"mistral": {"preferred_surface": "messages"}}})
    ladder = resolve_surface_ladder(
        "mistral",
        "mistral-large",
        client_family=ClientFamily.ANTHROPIC,
        declared=MISTRAL.response_surfaces,
        no_information=MISTRAL.no_information_surface,
    )
    assert ladder.primary.surface is ResponseSurface.CHAT_COMPLETIONS


# --------------------------------------------------------------------------
# 3. Q4: declared-but-unproved door -> probe first, learn or fall back
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_q4_probe_succeeds_learns_and_avoids_repeat_probes() -> None:
    _save_overrides(
        {
            "models": {
                f"opencode_go/{MINIMAX}": {"preferred_surface": "chat_completions"}
            }
        }
    )
    # MINIMAX registry is @ai-sdk/anthropic -> messages. chat_completions is unproved.
    ladder = resolve_surface_ladder(
        "opencode_go",
        MINIMAX,
        client_family=ClientFamily.ANTHROPIC,
        registry_provider=GO.surface_registry_provider,
        declared=GO.response_surfaces,
        no_information=GO.no_information_surface,
    )
    assert [c.surface for c in ladder.candidates] == [
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.MESSAGES,
    ]
    assert ladder.candidates[0].guessed is True
    assert ladder.candidates[1].guessed is False

    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        if req.url.path.endswith("/chat/completions"):
            return httpx.Response(200, content=_chat_sse_bytes())
        return httpx.Response(200, content=_messages_sse_bytes())

    config = ProviderConfig(api_key="test-key", base_url=GO_BASE)
    provider = create_openai_chat_provider(
        "opencode_go", config, passthrough_rate_limiter(), profile=GO
    )
    mock_http = _wire_mock_http(provider, handler)
    probe_calls: list[ResponseSurface] = []
    real_probe = provider._probe_surface

    async def tracked_probe(surface: ResponseSurface, model_id: str) -> None:
        probe_calls.append(surface)
        await real_probe(surface, model_id)

    provider.__dict__["_probe_surface"] = tracked_probe

    try:
        # First request: 1 probe to chat_completions, then real request to chat_completions.
        chunks = await _drain(provider.stream_response(_request(MINIMAX)))
        assert chunks
        assert probe_calls == [ResponseSurface.CHAT_COMPLETIONS]
        assert learned_surfaces("opencode_go", MINIMAX) == (
            ResponseSurface.CHAT_COMPLETIONS,
            ResponseSurface.MESSAGES,
        )

        # Second request: chat_completions is now learned -> zero probes!
        chunks_two = await _drain(provider.stream_response(_request(MINIMAX)))
        assert chunks_two
        assert probe_calls == [ResponseSurface.CHAT_COMPLETIONS]
    finally:
        await mock_http.aclose()


# --------------------------------------------------------------------------
# 4. Q9: refused preferred door -> keep preference, preserve learned, no re-probe loop
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_q9_refused_preferred_door_falls_back_keeps_preference_and_skips_reprobe() -> (
    None
):
    _save_overrides(
        {
            "models": {
                f"opencode_go/{MINIMAX}": {"preferred_surface": "chat_completions"}
            }
        }
    )
    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        if req.url.path.endswith("/chat/completions"):
            return httpx.Response(
                500,
                json={
                    "type": "error",
                    "error": {"type": "error", "message": "Internal server error"},
                },
            )
        return httpx.Response(200, content=_messages_sse_bytes())

    config = ProviderConfig(api_key="test-key", base_url=GO_BASE)
    provider = create_openai_chat_provider(
        "opencode_go", config, passthrough_rate_limiter(), profile=GO
    )
    mock_http = _wire_mock_http(provider, handler)
    probe_calls: list[ResponseSurface] = []
    real_probe = provider._probe_surface

    async def tracked_probe(surface: ResponseSurface, model_id: str) -> None:
        probe_calls.append(surface)
        await real_probe(surface, model_id)

    provider.__dict__["_probe_surface"] = tracked_probe

    try:
        # Turn 1: probe chat_completions fails 500 -> falls back to messages in same attempt!
        chunks = await _drain(provider.stream_response(_request(MINIMAX)))
        assert chunks
        assert probe_calls == [ResponseSurface.CHAT_COMPLETIONS]
        assert seen_paths == [
            "/inference/go/v1/chat/completions",
            "/inference/go/v1/messages",
        ]

        # Preference in model_overrides.json was NEVER overwritten!
        assert (
            preferred_surface_of("opencode_go", MINIMAX)
            is ResponseSurface.CHAT_COMPLETIONS
        )
        assert (
            current_model_overrides().models[f"opencode_go/{MINIMAX}"][
                "preferred_surface"
            ]
            == "chat_completions"
        )
        # Remaining known door preserved in FACT_RESPONSE_SURFACE.
        assert learned_surfaces("opencode_go", MINIMAX) == (ResponseSurface.MESSAGES,)
        # Refusal fact recorded with 7-day TTL.
        refusal = preferred_surface_refusal_fact(
            "opencode_go", MINIMAX, ResponseSurface.CHAT_COMPLETIONS
        )
        assert refusal is not None
        assert refusal.ttl_seconds == INFERRED_FACT_TTL_SECONDS

        # Turn 2 within TTL: goes straight to /messages, zero probes, zero failed chat calls!
        seen_paths.clear()
        chunks_two = await _drain(provider.stream_response(_request(MINIMAX)))
        assert chunks_two
        assert seen_paths == ["/inference/go/v1/messages"]
        assert probe_calls == [ResponseSurface.CHAT_COMPLETIONS]
    finally:
        await mock_http.aclose()

    # Models page payload reports Q9 message!
    payload = response_surface_payload("opencode_go", MINIMAX)
    assert payload is not None
    assert payload["preferred"] == "chat_completions"
    assert payload["value"] == "messages"
    assert (
        "you prefer Chat Completions; this host refused it at "
        in payload["preferred_refusal"]
    )
    assert "; serving Messages" in payload["preferred_refusal"]

    # Simulate TTL expiry (8 days old) -> re-opens once per TTL.
    old_iso = (
        (datetime.now(UTC) - timedelta(days=8))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    key = ("opencode_go", MINIMAX, FACT_PREFERRED_SURFACE_REFUSED, "chat_completions")
    stored = learned_fact_store()._facts[key]
    learned_fact_store()._facts[key] = type(stored)(
        provider_id=stored.provider_id,
        model_id=stored.model_id,
        fact_kind=stored.fact_kind,
        value=stored.value,
        learned_at=old_iso,
        last_confirmed_at=old_iso,
        source=stored.source,
        detail=stored.detail,
        evidence=stored.evidence,
        hits=stored.hits,
    )
    reopened = resolve_surface_ladder(
        "opencode_go",
        MINIMAX,
        client_family=ClientFamily.ANTHROPIC,
        registry_provider=GO.surface_registry_provider,
        declared=GO.response_surfaces,
        no_information=GO.no_information_surface,
    )
    assert reopened.primary.surface is ResponseSurface.CHAT_COMPLETIONS
    assert reopened.primary.guessed is True


@pytest.mark.asyncio
async def test_q9_known_preferred_refused_during_stream_records_refusal_and_keeps_remaining_doors() -> (
    None
):
    # Model is already learned on {messages, chat_completions}; operator prefers chat_completions.
    remember_response_surfaces(
        "opencode_go",
        MUSE,
        [ResponseSurface.MESSAGES, ResponseSurface.CHAT_COMPLETIONS],
    )
    _save_overrides(
        {"models": {f"opencode_go/{MUSE}": {"preferred_surface": "chat_completions"}}}
    )
    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        if req.url.path.endswith("/chat/completions"):
            return httpx.Response(
                500,
                json={
                    "type": "error",
                    "error": {"type": "error", "message": "Internal server error"},
                },
            )
        return httpx.Response(200, content=_messages_sse_bytes())

    config = ProviderConfig(api_key="test-key", base_url=GO_BASE)
    provider = create_openai_chat_provider(
        "opencode_go", config, passthrough_rate_limiter(), profile=GO
    )
    mock_http = _wire_mock_http(provider, handler)
    try:
        chunks = await _drain(provider.stream_response(_request(MUSE)))
    finally:
        await mock_http.aclose()
    assert chunks
    assert seen_paths == [
        "/inference/go/v1/chat/completions",
        "/inference/go/v1/messages",
    ]
    assert preferred_surface_of("opencode_go", MUSE) is ResponseSurface.CHAT_COMPLETIONS
    assert learned_surfaces("opencode_go", MUSE) == (ResponseSurface.MESSAGES,)
    assert (
        preferred_surface_refusal_fact(
            "opencode_go", MUSE, ResponseSurface.CHAT_COMPLETIONS
        )
        is not None
    )


# --------------------------------------------------------------------------
# 5. Fact kind contracts + Q10 discovered + Q11 admin API & 422 validation
# --------------------------------------------------------------------------


def test_preferred_surface_refused_fact_contract() -> None:
    assert FACT_PREFERRED_SURFACE_REFUSED in ALLOWED_FACT_KINDS
    assert FACT_TTL_SECONDS[FACT_PREFERRED_SURFACE_REFUSED] == INFERRED_FACT_TTL_SECONDS
    assert FACT_PREFERRED_SURFACE_REFUSED in FACT_KIND_LABELS
    assert FACT_PREFERRED_SURFACE_REFUSED in PAGE_TABLE_UNLISTED_FACT_KINDS


def test_q10_discovered_populated_on_multi_door_even_without_preference() -> None:
    # No preference set at all.
    muse_payload = response_surface_payload("opencode_go", MUSE)
    assert muse_payload is not None
    assert muse_payload["preferred"] == ""
    assert [entry["value"] for entry in muse_payload["discovered"]] == [
        "messages",
        "responses",
        "chat_completions",
    ]
    minimax_payload = response_surface_payload("opencode_go", MINIMAX)
    assert minimax_payload is not None
    assert [entry["value"] for entry in minimax_payload["discovered"]] == ["messages"]
    # Unservable model has empty discovered list (never guesses or exposes unservable).
    google_payload = response_surface_payload("opencode_go", GOOGLE)
    assert google_payload is not None
    assert google_payload["discovered"] == []

    # Single-door provider returns None (Q11).
    assert response_surface_payload("groq", "llama-3.3-70b") is None
    assert speakable_surfaces("groq") == ()


def test_admin_api_preferred_surface_write_read_clear_and_422_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate(
        {
            "OPENCODE_API_KEY": "sk-go-" + "1" * 30,
            "GROQ_API_KEY": "gsk_" + "2" * 30,
            "MODEL": f"opencode_go/{MINIMAX}",
        }
    )
    app = create_test_app(settings)
    manager = provider_manager_for_app(app)
    from my_claude_code.application.model_metadata import ProviderModelInfo

    manager.cache_model_infos(
        "opencode_go",
        (ProviderModelInfo(MINIMAX), ProviderModelInfo(MUSE)),
    )
    manager.cache_model_infos("groq", (ProviderModelInfo("llama-3.3-70b"),))

    client = TestClient(app, client=("127.0.0.1", 50000))

    # Rejects unservable, undeclared surface, and single-door provider with 422.
    for bad in (
        {"scope": "model", "key": f"opencode_go/{MINIMAX}", "surface": "unservable"},
        {"scope": "model", "key": f"opencode_go/{MINIMAX}", "surface": "bogus"},
        {"scope": "provider", "key": "opencode_go", "surface": "unservable"},
        {"scope": "provider", "key": "groq", "surface": "responses"},
        {"scope": "model", "key": "groq/llama-3.3-70b", "surface": "chat_completions"},
    ):
        resp = client.post("/admin/api/model-admin/preferred-surface", json=bad)
        assert resp.status_code == 422, (bad, resp.text)

    # Set provider-level preference on multi-door opencode_go -> 200.
    resp = client.post(
        "/admin/api/model-admin/preferred-surface",
        json={"scope": "provider", "key": "opencode_go", "surface": "chat_completions"},
    )
    assert resp.status_code == 200
    body = resp.json()
    providers = {p["provider_id"]: p for p in body["providers"]}
    assert (
        providers["opencode_go"]["surface_preference"]["preferred"]
        == "chat_completions"
    )
    assert "surface_preference" not in providers["groq"]
    assert providers["groq"]["models"][0]["capabilities"]["response_surface"] is None

    go_models = {m["model_ref"]: m for m in providers["opencode_go"]["models"]}
    assert (
        go_models[f"opencode_go/{MINIMAX}"]["surface_preference"]["preferred"]
        == "chat_completions"
    )
    assert (
        go_models[f"opencode_go/{MINIMAX}"]["surface_preference"]["preferred_scope"]
        == "provider"
    )
    assert go_models[f"opencode_go/{MUSE}"]["surface_preference"]["discovered"]

    # Set model-level preference overriding provider -> 200.
    resp = client.post(
        "/admin/api/model-admin/preferred-surface",
        json={
            "scope": "model",
            "key": f"opencode_go/{MINIMAX}",
            "surface": "responses",
        },
    )
    assert resp.status_code == 200
    go_models = {
        m["model_ref"]: m
        for m in {p["provider_id"]: p for p in resp.json()["providers"]}["opencode_go"][
            "models"
        ]
    }
    rs = go_models[f"opencode_go/{MINIMAX}"]["surface_preference"]
    assert rs["preferred"] == "responses"
    assert rs["preferred_scope"] == "model"
    assert rs["model_preferred"] == "responses"
    assert rs["provider_preferred"] == "chat_completions"

    # Clear model-level and provider-level -> reverts to unset.
    resp = client.post(
        "/admin/api/model-admin/preferred-surface",
        json={"scope": "model", "key": f"opencode_go/{MINIMAX}", "surface": ""},
    )
    assert resp.status_code == 200
    resp = client.post(
        "/admin/api/model-admin/preferred-surface",
        json={"scope": "provider", "key": "opencode_go", "surface": ""},
    )
    assert resp.status_code == 200
    assert load_model_overrides().providers == {}
    assert load_model_overrides().models == {}
