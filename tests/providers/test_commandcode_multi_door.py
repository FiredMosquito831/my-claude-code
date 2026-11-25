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
    """M3 target: CommandCode declares Chat + Responses, optimistic=False, reasoning_content kept."""

    assert surface_profile_for("commandcode") is COMMANDCODE_CHAT_PROFILE
    assert speakable_surfaces("commandcode") == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.RESPONSES,
    )
    assert _COMMANDCODE_PROFILE.response_surfaces == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.RESPONSES,
    )
    assert (
        _COMMANDCODE_PROFILE.no_information_surface is ResponseSurface.CHAT_COMPLETIONS
    )
    assert _COMMANDCODE_PROFILE.optimistic_first_try is False
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
