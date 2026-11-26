"""Multi-door CommandCode & `/models` declared.endpoints routing tests (v8.47.0)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.model_admin import (
    prompt_cache_gateway_models,
    response_surface_payload,
    speakable_surfaces,
)
from my_claude_code.application.model_metadata import (
    ProviderModelDeclaration,
    ProviderModelInfo,
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.config.model_overrides import (
    ModelParameterOverrides,
    save_model_overrides,
)
from my_claude_code.config.paths import config_dir_path
from my_claude_code.config.provider_catalog import COMMANDCODE_DEFAULT_BASE
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.client_family import ClientFamily, client_family_scope
from my_claude_code.core.derived_cache import DerivedCache
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.commandcode import (
    CommandCodeProvider,
    commandcode_uses_anthropic_messages,
    extract_commandcode_model_infos,
)
from my_claude_code.providers.commandcode.client import _PROFILE as _COMMANDCODE_PROFILE
from my_claude_code.providers.model_listing import (
    cached_declared_http_surfaces,
    declared_http_surfaces,
    extract_openai_model_infos,
    http_declared_endpoints,
    set_declared_endpoints_snapshot,
)
from my_claude_code.providers.openai_chat import (
    COMMANDCODE_CHAT_PROFILE,
    catalogue_discovered_surfaces,
    catalogue_surface,
    resolve_surface_ladder,
    surface_profile_for,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import learned_fact_store
from tests.api.support import create_test_app, provider_manager_for_app


def _provider() -> CommandCodeProvider:
    return CommandCodeProvider(
        ProviderConfig(
            api_key="user_secret_commandcode_key",
            base_url=COMMANDCODE_DEFAULT_BASE,
            rate_limit=100,
            rate_window=60,
            max_concurrency=5,
            retry_attempts=1,
            early_retry_attempts=1,
            commit_holdback_seconds=0,
        ),
        rate_limiter=ProviderRateLimiter(
            rate_limit=100,
            rate_window=60,
            max_concurrency=5,
            max_retries=0,
        ),
    )


def _request(model: str) -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=128,
        messages=[Message(role="user", content="ping")],
        stream=True,
    )


async def _drain(stream: Any) -> list[str]:
    return [chunk async for chunk in stream]


def _sse_stream(payload: bytes) -> httpx.Response:
    async def _gen() -> Any:
        yield payload

    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_gen(),
    )


def _responses_sse(text: str = "from-responses") -> httpx.Response:
    events = [
        {
            "type": "response.created",
            "response": {
                "id": "resp_1",
                "model": "deepseek/deepseek-v4.1-flash",
                "status": "in_progress",
            },
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "model": "deepseek/deepseek-v4.1-flash",
                "status": "completed",
                "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            },
        },
    ]
    body = "".join(
        f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in events
    ).encode("utf-8")
    return _sse_stream(body)


def _chat_sse(text: str = "from-chat", reasoning: str = "step 1") -> httpx.Response:
    events = [
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": "deepseek/deepseek-v4.1-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "reasoning_content": reasoning},
                }
            ],
        },
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": "deepseek/deepseek-v4.1-flash",
            "choices": [{"index": 0, "delta": {"content": text}}],
        },
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": "deepseek/deepseek-v4.1-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 6, "completion_tokens": 4},
        },
    ]
    body = (
        "".join(f"data: {json.dumps(item)}\n\n" for item in events) + "data: [DONE]\n\n"
    ).encode("utf-8")
    return _sse_stream(body)


def _messages_sse(text: str = "from-messages") -> httpx.Response:
    frames = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "custom/messages-only",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 2},
        },
        {"type": "message_stop"},
    ]
    body = "".join(
        f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in frames
    ).encode("utf-8")
    return _sse_stream(body)


def test_http_declared_endpoints_filters_mistral_capability_words_without_mutating_record() -> (
    None
):
    """M1 target: capability words are preserved on ``declared.endpoints`` but filtered for HTTP routing."""

    infos = extract_openai_model_infos(
        {
            "data": [
                {
                    "id": "mistral-large-latest",
                    "capabilities": {"completion_chat": True, "vision": True},
                },
                {
                    "id": "mistral-ocr-latest",
                    "capabilities": {"completion_chat": False, "ocr": True},
                },
            ]
        },
        provider_name="mistral",
    )
    by_id = {info.model_id: info for info in infos}
    assert by_id["mistral-large-latest"].declared is not None
    assert by_id["mistral-large-latest"].declared.endpoints == ("chat",)
    assert by_id["mistral-ocr-latest"].declared is not None
    assert by_id["mistral-ocr-latest"].declared.endpoints == ("non_chat", "ocr")

    assert http_declared_endpoints(("chat", "non_chat", "ocr", "fim")) == ()
    assert http_declared_endpoints(
        ("chat", "/chat/completions", "ocr", "/responses", "audio_speech")
    ) == ("/chat/completions", "/responses")
    assert declared_http_surfaces(("chat", "non_chat", "ocr")) == ()
    assert declared_http_surfaces(
        (
            "/api/v1/chat/completions",
            "/api/v1/completions",
            "/api/v1/responses",
            "/api/v1/messages",
        )
    ) == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.RESPONSES,
        ResponseSurface.MESSAGES,
    )
    assert declared_http_surfaces(("anthropic", "openai", "openai-response")) == (
        ResponseSurface.MESSAGES,
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.RESPONSES,
    )


def test_derived_cache_fallback_provides_declared_http_surfaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    cache = DerivedCache(config_dir_path() / "cache" / "derived")
    cache.write(
        "provider-catalogue",
        key="test",
        payload={
            "providers": [
                {
                    "provider_id": "commandcode",
                    "models": [
                        {
                            "model_id": "deepseek/deepseek-v4.1-flash",
                            "declared": {
                                "endpoints": ["/chat/completions", "/responses"]
                            },
                        }
                    ],
                }
            ]
        },
        computed_at=1.0,
    )
    assert cached_declared_http_surfaces(
        "commandcode", "deepseek/deepseek-v4.1-flash"
    ) == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.RESPONSES,
    )


def test_commandcode_profile_contract_and_reasoning_field_preserved() -> None:
    """M1/M2/M3 target: CommandCode speaks Chat/Responses/Messages, unsigned/think-tags on."""

    assert surface_profile_for("commandcode") is COMMANDCODE_CHAT_PROFILE
    assert speakable_surfaces("commandcode") == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.RESPONSES,
        ResponseSurface.MESSAGES,
    )
    assert _COMMANDCODE_PROFILE.response_surfaces == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.RESPONSES,
        ResponseSurface.MESSAGES,
    )
    assert (
        _COMMANDCODE_PROFILE.no_information_surface is ResponseSurface.CHAT_COMPLETIONS
    )
    assert _COMMANDCODE_PROFILE.optimistic_first_try is False
    assert _COMMANDCODE_PROFILE.messages_signs_thinking is False
    assert _COMMANDCODE_PROFILE.messages_extract_think_tags is True
    assert _COMMANDCODE_PROFILE.reasoning_delta_field == "reasoning_content"
    assert (
        _COMMANDCODE_PROFILE.request_policy.reasoning_replay
        is ReasoningReplayMode.REASONING_CONTENT
    )


def test_resolve_surface_ladder_uses_declared_endpoints_for_commandcode() -> None:
    """M2 target: /models declared.endpoints drives dual-door vs single-door surface ordering."""

    infos = extract_commandcode_model_infos(
        {
            "data": [
                {
                    "id": "deepseek/deepseek-v4.1-flash",
                    "context_length": 1_000_000,
                    "supported_endpoints": ["/chat/completions", "/responses"],
                },
                {
                    "id": "foo/chat-only",
                    "context_length": 128_000,
                    "supported_endpoints": ["/chat/completions"],
                },
                {
                    "id": "custom/messages-only",
                    "context_length": 200_000,
                    "supported_endpoints": ["/messages"],
                },
            ]
        },
        provider_name="COMMANDCODE",
    )
    set_declared_endpoints_snapshot({"commandcode": infos})

    # Dual-door model: UNKNOWN (page) keeps discovery order; ANTHROPIC/OPENAI lead with RESPONSES.
    page_dual = catalogue_surface("commandcode", "deepseek/deepseek-v4.1-flash")
    assert page_dual is not None
    assert page_dual.surface is ResponseSurface.CHAT_COMPLETIONS
    assert page_dual.source is ResponseSurfaceSource.REGISTRY
    assert [
        c.surface
        for c in catalogue_discovered_surfaces(
            "commandcode", "deepseek/deepseek-v4.1-flash"
        )
    ] == [ResponseSurface.CHAT_COMPLETIONS, ResponseSurface.RESPONSES]

    for family in (ClientFamily.ANTHROPIC, ClientFamily.OPENAI):
        ladder = resolve_surface_ladder(
            "commandcode",
            "deepseek/deepseek-v4.1-flash",
            client_family=family,
            declared=_COMMANDCODE_PROFILE.response_surfaces,
            no_information=_COMMANDCODE_PROFILE.no_information_surface,
            optimistic=_COMMANDCODE_PROFILE.optimistic_first_try,
        )
        assert [c.surface for c in ladder.candidates] == [
            ResponseSurface.RESPONSES,
            ResponseSurface.CHAT_COMPLETIONS,
        ]
        assert all(
            c.source is ResponseSurfaceSource.REGISTRY for c in ladder.candidates
        )
        assert all(not c.guessed for c in ladder.candidates)

    # Single-door model: ONLY CHAT_COMPLETIONS, never probes /responses.
    for family in ClientFamily:
        single = resolve_surface_ladder(
            "commandcode",
            "foo/chat-only",
            client_family=family,
            declared=_COMMANDCODE_PROFILE.response_surfaces,
            no_information=_COMMANDCODE_PROFILE.no_information_surface,
            optimistic=_COMMANDCODE_PROFILE.optimistic_first_try,
        )
        assert [c.surface for c in single.candidates] == [
            ResponseSurface.CHAT_COMPLETIONS
        ]

    # Non-claude model declaring ["/messages"] uses _anthropic and stays excluded from OpenAI surface UI.
    assert commandcode_uses_anthropic_messages("custom/messages-only") is True
    assert catalogue_surface("commandcode", "custom/messages-only") is None


@pytest.mark.asyncio
async def test_commandcode_cold_start_without_models_cache_stays_on_chat_completions() -> (
    None
):
    provider = _provider()
    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        return _chat_sse("cold-chat")

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider._openai._client._client = mock_http
    try:
        with client_family_scope("messages"):
            chunks = await _drain(
                provider.stream_response(
                    _request("deepseek/deepseek-v4.1-flash"),
                    reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
                )
            )
    finally:
        await provider.cleanup()
        await mock_http.aclose()

    assert seen_paths == ["/provider/v1/chat/completions"]
    assert any("cold-chat" in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_commandcode_dual_door_responses_and_fallback_and_preferred_surface() -> (
    None
):
    set_declared_endpoints_snapshot(
        {
            "commandcode": (
                ProviderModelInfo(
                    model_id="deepseek/deepseek-v4.1-flash",
                    declared=ProviderModelDeclaration(
                        endpoints=("/chat/completions", "/responses")
                    ),
                ),
            )
        }
    )

    # 1. Dual-door defaults to /responses for Anthropic inbound and succeeds without probe.
    provider = _provider()
    seen_paths: list[str] = []

    def ok_responses_handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        return _responses_sse("ok-resp")

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(ok_responses_handler))
    provider._openai._responses._client = mock_http
    try:
        with client_family_scope("messages"):
            chunks = await _drain(
                provider.stream_response(
                    _request("deepseek/deepseek-v4.1-flash"),
                    reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
                )
            )
    finally:
        await provider.cleanup()
        await mock_http.aclose()

    assert seen_paths == ["/provider/v1/responses"]
    assert any("ok-resp" in chunk for chunk in chunks)

    # 2. Wrong-door 400 naming /chat/completions falls back cleanly inside the same attempt.
    provider2 = _provider()
    seen2: list[str] = []

    def fallback_handler(req: httpx.Request) -> httpx.Response:
        seen2.append(req.url.path)
        if req.url.path.endswith("/responses"):
            return httpx.Response(
                400,
                json={"error": {"message": "Use /chat/completions for this model"}},
            )
        return _chat_sse("fallback-chat", "reasoned-on-chat")

    mock_http2 = httpx.AsyncClient(transport=httpx.MockTransport(fallback_handler))
    provider2._openai._responses._client = mock_http2
    provider2._openai._client._client = mock_http2
    try:
        with client_family_scope("messages"):
            chunks2 = await _drain(
                provider2.stream_response(
                    _request("deepseek/deepseek-v4.1-flash"),
                    reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
                )
            )
    finally:
        await provider2.cleanup()
        await mock_http2.aclose()

    assert seen2 == ["/provider/v1/responses", "/provider/v1/chat/completions"]
    joined = "".join(chunks2)
    assert "fallback-chat" in joined
    assert "reasoned-on-chat" in joined


@pytest.mark.asyncio
async def test_commandcode_non_claude_model_declaring_messages_routes_to_anthropic() -> (
    None
):
    """M4 target: model declaring ['/messages'] routes via _anthropic even without 'claude-' prefix."""

    set_declared_endpoints_snapshot(
        {
            "commandcode": (
                ProviderModelInfo(
                    model_id="custom/messages-only",
                    declared=ProviderModelDeclaration(endpoints=("/messages",)),
                ),
            )
        }
    )
    provider = _provider()
    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        return _messages_sse("anthropic-door-ok")

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider._anthropic._client = mock_http
    try:
        chunks = await _drain(
            provider.stream_response(_request("custom/messages-only"))
        )
    finally:
        await provider.cleanup()
        await mock_http.aclose()

    assert seen_paths == ["/provider/v1/messages"]
    assert any("anthropic-door-ok" in chunk for chunk in chunks)
    assert prompt_cache_gateway_models(
        {
            "providers": [
                {
                    "provider_id": "commandcode",
                    "models": [
                        {
                            "model_ref": "commandcode/custom/messages-only",
                            "model_id": "custom/messages-only",
                        }
                    ],
                }
            ]
        }
    ) == {"commandcode/custom/messages-only": "anthropic_messages_gateway"}


def test_commandcode_preferred_surface_and_harness_surfaces_admin_apis_return_200() -> (
    None
):
    settings = Settings.model_validate(
        {
            "COMMANDCODE_API_KEY": "user_" + "9" * 30,
            "MODEL": "commandcode/deepseek/deepseek-v4.1-flash",
        }
    )
    app = create_test_app(settings)
    manager = provider_manager_for_app(app)
    manager.cache_model_infos(
        "commandcode",
        (
            ProviderModelInfo(
                model_id="deepseek/deepseek-v4.1-flash",
                declared=ProviderModelDeclaration(
                    endpoints=("/chat/completions", "/responses")
                ),
            ),
            ProviderModelInfo(
                model_id="claude-sonnet-5",
                declared=ProviderModelDeclaration(endpoints=("/messages",)),
            ),
        ),
    )

    client = TestClient(app, client=("127.0.0.1", 50000))
    resp = client.post(
        "/admin/api/model-admin/preferred-surface",
        json={"scope": "provider", "key": "commandcode", "surface": "chat_completions"},
    )
    assert resp.status_code == 200, resp.text
    providers = {p["provider_id"]: p for p in resp.json()["providers"]}
    assert (
        providers["commandcode"]["surface_preference"]["preferred"]
        == "chat_completions"
    )
    cc_models = {m["model_ref"]: m for m in providers["commandcode"]["models"]}
    assert (
        cc_models["commandcode/deepseek/deepseek-v4.1-flash"]["surface_preference"][
            "preferred"
        ]
        == "chat_completions"
    )
    assert "surface_preference" not in cc_models["commandcode/claude-sonnet-5"]

    harness_resp = client.post(
        "/admin/api/harness-surfaces",
        json={"harness": "claude", "provider": "commandcode", "surface": "responses"},
    )
    assert harness_resp.status_code == 200, harness_resp.text
    assert harness_resp.json()["harnesses"]["claude"]["commandcode"] == "responses"

    # Cleanup override file cleanly
    save_model_overrides(ModelParameterOverrides())
    assert response_surface_payload("commandcode", "claude-sonnet-5") is None


def test_commandcode_triple_door_ladder_and_preferred_surface_rung_1() -> None:
    """M1/M4 target: triple-door resolves MESSAGES->RESPONSES->CHAT for Anthropic."""

    infos = extract_commandcode_model_infos(
        {
            "data": [
                {
                    "id": "deepseek/deepseek-v4.1-flash",
                    "context_length": 1_000_000,
                    "supported_endpoints": [
                        "/chat/completions",
                        "/messages",
                        "/responses",
                    ],
                },
                {
                    "id": "claude-sonnet-5",
                    "context_length": 1_000_000,
                    "supported_endpoints": ["/messages"],
                },
            ]
        },
        provider_name="COMMANDCODE",
    )
    set_declared_endpoints_snapshot({"commandcode": infos})

    anthropic_ladder = resolve_surface_ladder(
        "commandcode",
        "deepseek/deepseek-v4.1-flash",
        client_family=ClientFamily.ANTHROPIC,
        declared=_COMMANDCODE_PROFILE.response_surfaces,
        no_information=_COMMANDCODE_PROFILE.no_information_surface,
        optimistic=_COMMANDCODE_PROFILE.optimistic_first_try,
    )
    assert [c.surface for c in anthropic_ladder.candidates] == [
        ResponseSurface.MESSAGES,
        ResponseSurface.RESPONSES,
        ResponseSurface.CHAT_COMPLETIONS,
    ]
    assert all(
        c.source is ResponseSurfaceSource.REGISTRY for c in anthropic_ladder.candidates
    )

    openai_ladder = resolve_surface_ladder(
        "commandcode",
        "deepseek/deepseek-v4.1-flash",
        client_family=ClientFamily.OPENAI,
        declared=_COMMANDCODE_PROFILE.response_surfaces,
        no_information=_COMMANDCODE_PROFILE.no_information_surface,
        optimistic=_COMMANDCODE_PROFILE.optimistic_first_try,
    )
    assert [c.surface for c in openai_ladder.candidates][:2] == [
        ResponseSurface.RESPONSES,
        ResponseSurface.CHAT_COMPLETIONS,
    ]

    # Models page (ClientFamily.UNKNOWN) preserves discovery order; Claude stays excluded.
    page_triple = catalogue_surface("commandcode", "deepseek/deepseek-v4.1-flash")
    assert page_triple is not None
    assert page_triple.surface is ResponseSurface.CHAT_COMPLETIONS
    assert [
        c.surface
        for c in catalogue_discovered_surfaces(
            "commandcode", "deepseek/deepseek-v4.1-flash"
        )
    ] == [
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.MESSAGES,
        ResponseSurface.RESPONSES,
    ]
    assert commandcode_uses_anthropic_messages("claude-sonnet-5") is True
    assert catalogue_surface("commandcode", "claude-sonnet-5") is None

    # Explicit preferred_surface override wins at Rung 1.
    try:
        for pref, expected_first in (
            ("responses", ResponseSurface.RESPONSES),
            ("chat_completions", ResponseSurface.CHAT_COMPLETIONS),
            ("messages", ResponseSurface.MESSAGES),
        ):
            save_model_overrides(
                ModelParameterOverrides(
                    models={
                        "commandcode/deepseek/deepseek-v4.1-flash": {
                            "preferred_surface": pref
                        }
                    }
                )
            )
            overridden = resolve_surface_ladder(
                "commandcode",
                "deepseek/deepseek-v4.1-flash",
                client_family=ClientFamily.ANTHROPIC,
                declared=_COMMANDCODE_PROFILE.response_surfaces,
                no_information=_COMMANDCODE_PROFILE.no_information_surface,
                optimistic=_COMMANDCODE_PROFILE.optimistic_first_try,
            )
            assert overridden.primary.surface is expected_first
            assert overridden.primary.source is ResponseSurfaceSource.REGISTRY
            assert len(overridden.candidates) == 3
    finally:
        save_model_overrides(ModelParameterOverrides())


@pytest.mark.asyncio
async def test_commandcode_triple_door_messages_first_fallback_and_overrides() -> None:
    """Triple-door /v1/messages tries /messages first, falls back, and honors pins."""

    set_declared_endpoints_snapshot(
        {
            "commandcode": (
                ProviderModelInfo(
                    model_id="deepseek/deepseek-v4.1-flash",
                    declared=ProviderModelDeclaration(
                        endpoints=("/chat/completions", "/messages", "/responses")
                    ),
                ),
            )
        }
    )

    # 1. Auto under /v1/messages hits /provider/v1/messages FIRST with gateway auth + reasoning.
    provider = _provider()
    seen_requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def ok_messages_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen_requests.append((req.url.path, dict(req.headers), body))
        return _messages_sse("ok-messages")

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(ok_messages_handler))
    provider._openai._messages._provider._client = mock_http
    try:
        with client_family_scope("messages"):
            chunks = await _drain(
                provider.stream_response(
                    _request("deepseek/deepseek-v4.1-flash"),
                    reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
                )
            )
    finally:
        await provider.cleanup()
        await mock_http.aclose()

    assert len(seen_requests) == 1
    path, headers, sent_body = seen_requests[0]
    assert path == "/provider/v1/messages"
    assert headers.get("authorization") == "Bearer user_secret_commandcode_key"
    assert headers.get("x-api-key") == "user_secret_commandcode_key"
    assert headers.get("anthropic-version")
    assert sent_body.get("thinking", {}).get("type") == "adaptive"
    assert sent_body.get("output_config", {}).get("effort") == "max"
    assert any("ok-messages" in chunk for chunk in chunks)

    # 2. Same-attempt fallback: /messages 400 -> /responses 400 -> /chat/completions 200.
    provider2 = _provider()
    seen_paths2: list[str] = []

    def triple_fallback_handler(req: httpx.Request) -> httpx.Response:
        seen_paths2.append(req.url.path)
        if req.url.path.endswith("/messages"):
            return httpx.Response(
                400,
                json={"error": {"message": "Use /responses for this model"}},
            )
        if req.url.path.endswith("/responses"):
            return httpx.Response(
                400,
                json={"error": {"message": "Use /chat/completions for this model"}},
            )
        return _chat_sse("triple-fallback-chat", "reasoned-on-chat")

    mock_http2 = httpx.AsyncClient(
        transport=httpx.MockTransport(triple_fallback_handler)
    )
    provider2._openai._messages._provider._client = mock_http2
    provider2._openai._responses._client = mock_http2
    provider2._openai._client._client = mock_http2
    try:
        with client_family_scope("messages"):
            chunks2 = await _drain(
                provider2.stream_response(
                    _request("deepseek/deepseek-v4.1-flash"),
                    reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
                )
            )
    finally:
        await provider2.cleanup()
        await mock_http2.aclose()

    assert seen_paths2 == [
        "/provider/v1/messages",
        "/provider/v1/responses",
        "/provider/v1/chat/completions",
    ]
    joined2 = "".join(chunks2)
    assert "triple-fallback-chat" in joined2
    assert "reasoned-on-chat" in joined2

    # Forget learned surface facts from the fallback test before testing Rung 1 pins.
    learned_fact_store().forget("commandcode", "deepseek/deepseek-v4.1-flash")

    # 3. Explicit preferred_surface="responses" and "chat_completions" override at Rung 1.
    for pref, expected_path, marker in (
        ("responses", "/provider/v1/responses", "pinned-resp"),
        ("chat_completions", "/provider/v1/chat/completions", "pinned-chat"),
    ):
        try:
            save_model_overrides(
                ModelParameterOverrides(
                    models={
                        "commandcode/deepseek/deepseek-v4.1-flash": {
                            "preferred_surface": pref
                        }
                    }
                )
            )
            provider3 = _provider()
            seen3: list[str] = []

            def pinned_handler(
                req: httpx.Request,
                *,
                _marker: str = marker,
                _seen: list[str] = seen3,
            ) -> httpx.Response:
                _seen.append(req.url.path)
                if req.url.path.endswith("/responses"):
                    return _responses_sse(_marker)
                return _chat_sse(_marker)

            mock_http3 = httpx.AsyncClient(
                transport=httpx.MockTransport(pinned_handler)
            )
            provider3._openai._responses._client = mock_http3
            provider3._openai._client._client = mock_http3
            try:
                with client_family_scope("messages"):
                    chunks3 = await _drain(
                        provider3.stream_response(
                            _request("deepseek/deepseek-v4.1-flash"),
                            reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
                        )
                    )
            finally:
                await provider3.cleanup()
                await mock_http3.aclose()

            assert seen3 == [expected_path]
            assert any(marker in chunk for chunk in chunks3)
        finally:
            save_model_overrides(ModelParameterOverrides())


@pytest.mark.asyncio
async def test_commandcode_messages_unsigned_thinking_and_think_tag_extraction() -> (
    None
):
    """M2 & M3 targets: unsigned thinking history replays and <think> tags extract."""

    set_declared_endpoints_snapshot(
        {
            "commandcode": (
                ProviderModelInfo(
                    model_id="deepseek/deepseek-v4.1-flash",
                    declared=ProviderModelDeclaration(
                        endpoints=("/chat/completions", "/messages", "/responses")
                    ),
                ),
            )
        }
    )

    provider = _provider()
    captured_bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(req.content))
        frames = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_think_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "deepseek/deepseek-v4.1-flash",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "text_delta",
                    "text": "<think>step by step</think>final visible reply",
                },
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 4},
            },
            {"type": "message_stop"},
        ]
        payload = "".join(
            f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in frames
        ).encode("utf-8")
        return _sse_stream(payload)

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider._openai._messages._provider._client = mock_http

    multi_turn = MessagesRequest.model_validate(
        {
            "model": "deepseek/deepseek-v4.1-flash",
            "max_tokens": 256,
            "stream": True,
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "unsigned prior reasoning"},
                        {"type": "text", "text": "prior reply"},
                    ],
                },
                {"role": "user", "content": "next"},
            ],
        }
    )

    try:
        with client_family_scope("messages"):
            chunks = await _drain(
                provider.stream_response(
                    multi_turn,
                    reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
                )
            )
    finally:
        await provider.cleanup()
        await mock_http.aclose()

    assert len(captured_bodies) == 1
    sent = captured_bodies[0]
    assistant_msg = next(m for m in sent["messages"] if m["role"] == "assistant")
    assert any(
        block.get("type") == "thinking"
        and block.get("thinking") == "unsigned prior reasoning"
        for block in assistant_msg["content"]
    )

    sse = "".join(chunks)
    assert '"type": "thinking_delta"' in sse or '"type":"thinking_delta"' in sse
    assert "step by step" in sse
    assert "final visible reply" in sse
    assert "<think>" not in sse


@pytest.mark.asyncio
async def test_commandcode_pure_messages_and_claude_delegate_to_anthropic() -> None:
    """M4 target: pure-/messages and cold-start claude-* stay on _anthropic."""

    provider = _provider()
    try:
        assert provider._delegate("claude-sonnet-5") is provider._anthropic

        set_declared_endpoints_snapshot(
            {
                "commandcode": (
                    ProviderModelInfo(
                        model_id="claude-sonnet-5",
                        declared=ProviderModelDeclaration(endpoints=("/messages",)),
                    ),
                    ProviderModelInfo(
                        model_id="deepseek/deepseek-v4.1-flash",
                        declared=ProviderModelDeclaration(
                            endpoints=("/chat/completions", "/messages", "/responses")
                        ),
                    ),
                )
            }
        )
        assert provider._delegate("claude-sonnet-5") is provider._anthropic
        assert provider._delegate("deepseek/deepseek-v4.1-flash") is provider._openai
    finally:
        await provider.cleanup()
