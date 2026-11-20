"""PR #13 (Scope C3): Mistral built-in regional hosts & official /v1/responses surface."""

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from my_claude_code.api.model_admin import (
    response_surface_payload,
    speakable_surfaces,
)
from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.application.model_metadata import (
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.config import model_overrides as overrides_module
from my_claude_code.config import settings as config_settings
from my_claude_code.config.admin.manifest import FIELD_BY_KEY
from my_claude_code.config.provider_catalog import (
    CODESTRAL_DEFAULT_BASE,
    MISTRAL_DEFAULT_BASE,
    MISTRAL_EU_BASE,
    MISTRAL_US_BASE,
    PROVIDER_CATALOG,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.client_fingerprint import install_fingerprint
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.mistral import MistralProvider
from my_claude_code.providers.mistral.client import _PROFILE as MISTRAL_PROFILE
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    catalogue_surface,
    remember_response_surfaces,
)
from my_claude_code.providers.runtime.config import build_provider_config
from my_claude_code.providers.runtime.factory import create_provider
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter, reasoning_for

STUDIO = "mistral"
VIBE = "mistral_vibe"
CODESTRAL = "mistral_codestral"
STUDIO_KEY = "sk-studio-region-0001"
VIBE_KEY = "sk-vibe-region-0002"


def _make_provider(
    provider_id: str,
    api_key: str,
    *,
    base_url: str = MISTRAL_DEFAULT_BASE,
    profile: Any = MISTRAL_PROFILE,
) -> MistralProvider:
    return (
        MistralProvider.__new__(MistralProvider)
        if False
        else _build_with_profile(
            provider_id=provider_id,
            api_key=api_key,
            base_url=base_url,
            profile=profile,
        )
    )


def _build_with_profile(
    *,
    provider_id: str,
    api_key: str,
    base_url: str,
    profile: Any,
) -> MistralProvider:
    from my_claude_code.providers.openai_chat import OpenAIChatProvider

    provider = MistralProvider(
        ProviderConfig(
            api_key=api_key,
            base_url=base_url,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
        provider_id=provider_id,
    )
    if profile is not MISTRAL_PROFILE:
        OpenAIChatProvider.__init__(
            provider,
            ProviderConfig(
                api_key=api_key,
                base_url=base_url,
                rate_limit=10,
                rate_window=60,
            ),
            profile=profile,
            rate_limiter=passthrough_rate_limiter(),
            provider_id=provider_id,
        )
    return provider


def _chat_sse_response() -> httpx.Response:
    async def _sse() -> Any:
        yield (
            b'data: {"id":"c1","object":"chat.completion.chunk","created":0,'
            b'"model":"fixture-model","choices":[{"index":0,"delta":'
            b'{"role":"assistant","content":"hello"},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )

    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_sse(),
    )


def _responses_sse_response(text: str = "from-responses") -> httpx.Response:
    events = [
        {
            "type": "response.created",
            "response": {
                "id": "resp_1",
                "model": "fixture-model",
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
                "model": "fixture-model",
                "status": "completed",
                "usage": {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
            },
        },
    ]
    payload = "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode("utf-8")

    async def _sse() -> Any:
        yield payload

    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_sse(),
    )


# --------------------------------------------------------------------------
# 1. M-Q7: Regional hosts & base URL isolation
# --------------------------------------------------------------------------


def test_regional_constants_and_catalog_base_url_attrs() -> None:
    assert MISTRAL_DEFAULT_BASE == "https://api.mistral.ai/v1"
    assert MISTRAL_EU_BASE == "https://api.eu.mistral.ai/v1"
    assert MISTRAL_US_BASE == "https://api.us.mistral.ai/v1"

    assert PROVIDER_CATALOG[STUDIO].base_url_attr == "mistral_base_url"
    assert PROVIDER_CATALOG[STUDIO].default_base_url == MISTRAL_DEFAULT_BASE
    assert PROVIDER_CATALOG[VIBE].base_url_attr == "mistral_vibe_base_url"
    assert PROVIDER_CATALOG[VIBE].default_base_url == MISTRAL_DEFAULT_BASE

    # Codestral stays isolated (M-Q2 / out-of-scope).
    assert PROVIDER_CATALOG[CODESTRAL].default_base_url == CODESTRAL_DEFAULT_BASE
    assert PROVIDER_CATALOG[CODESTRAL].base_url_attr is None
    assert OPENAI_CHAT_PROFILES[CODESTRAL].response_surfaces == ()


def test_empty_base_urls_default_to_global_mistral_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISTRAL_BASE_URL", raising=False)
    monkeypatch.delenv("MISTRAL_VIBE_BASE_URL", raising=False)
    settings = Settings.model_validate(
        {
            "MISTRAL_API_KEY": STUDIO_KEY,
            "MISTRAL_VIBE_KEY": VIBE_KEY,
            "MISTRAL_BASE_URL": "",
            "MISTRAL_VIBE_BASE_URL": "",
        }
    )
    studio = create_provider(STUDIO, settings)
    vibe = create_provider(VIBE, settings)
    assert isinstance(studio, MistralProvider)
    assert isinstance(vibe, MistralProvider)
    assert studio._base_url == MISTRAL_DEFAULT_BASE
    assert vibe._base_url == MISTRAL_DEFAULT_BASE


def test_studio_and_vibe_base_urls_are_strictly_independent() -> None:
    eu_studio = Settings.model_validate(
        {
            "MISTRAL_API_KEY": STUDIO_KEY,
            "MISTRAL_VIBE_KEY": VIBE_KEY,
            "MISTRAL_BASE_URL": MISTRAL_EU_BASE,
            "MISTRAL_VIBE_BASE_URL": "",
        }
    )
    assert (
        build_provider_config(PROVIDER_CATALOG[STUDIO], eu_studio).base_url
        == MISTRAL_EU_BASE
    )
    assert (
        build_provider_config(PROVIDER_CATALOG[VIBE], eu_studio).base_url
        == MISTRAL_DEFAULT_BASE
    )
    studio_eu = create_provider(STUDIO, eu_studio)
    vibe_default = create_provider(VIBE, eu_studio)
    assert isinstance(studio_eu, MistralProvider)
    assert isinstance(vibe_default, MistralProvider)
    assert studio_eu._base_url == MISTRAL_EU_BASE
    assert vibe_default._base_url == MISTRAL_DEFAULT_BASE

    us_vibe = Settings.model_validate(
        {
            "MISTRAL_API_KEY": STUDIO_KEY,
            "MISTRAL_VIBE_KEY": VIBE_KEY,
            "MISTRAL_BASE_URL": "",
            "MISTRAL_VIBE_BASE_URL": MISTRAL_US_BASE,
        }
    )
    studio_default = create_provider(STUDIO, us_vibe)
    vibe_us = create_provider(VIBE, us_vibe)
    assert isinstance(studio_default, MistralProvider)
    assert isinstance(vibe_us, MistralProvider)
    assert studio_default._base_url == MISTRAL_DEFAULT_BASE
    assert vibe_us._base_url == MISTRAL_US_BASE


def test_manifest_fields_disclose_official_hosts_and_1_1x_regional_billing() -> None:
    for key, owner in (
        ("MISTRAL_BASE_URL", STUDIO),
        ("MISTRAL_VIBE_BASE_URL", VIBE),
    ):
        field = FIELD_BY_KEY[key]
        assert field.section_id == "providers"
        assert field.provider == owner
        assert field.affects_providers is True
        assert field.restart_required is False
        assert field.default == MISTRAL_DEFAULT_BASE
        desc = field.description
        assert "1.1x" in desc
        assert MISTRAL_EU_BASE in desc
        assert MISTRAL_US_BASE in desc
        assert MISTRAL_DEFAULT_BASE in desc


# --------------------------------------------------------------------------
# 2. M-Q8: Declarative /v1/responses surface & byte-identical Chat default
# --------------------------------------------------------------------------


def test_mistral_profile_declares_chat_and_responses_with_chat_default() -> None:
    assert MISTRAL_PROFILE.response_surfaces == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.RESPONSES,
    )
    assert MISTRAL_PROFILE.no_information_surface is ResponseSurface.CHAT_COMPLETIONS

    for provider_id in (STUDIO, VIBE):
        assert speakable_surfaces(provider_id) == (
            ResponseSurface.CHAT_COMPLETIONS,
            ResponseSurface.RESPONSES,
        )
        resolved = catalogue_surface(provider_id, "fixture-model")
        assert resolved is not None
        assert resolved.surface is ResponseSurface.CHAT_COMPLETIONS
        assert resolved.source is ResponseSurfaceSource.PROFILE
        payload = response_surface_payload(provider_id, "fixture-model")
        assert payload is not None
        assert [item["value"] for item in payload["offered"]] == [
            "chat_completions",
            "responses",
        ]
        assert payload["source"] == "profile"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", [STUDIO, VIBE])
async def test_default_chat_completions_wire_bytes_are_byte_identical_to_v8_30_0(
    provider_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IV.7 equality: no override/learned fact -> identical Chat Completions request."""
    monkeypatch.delenv("MISTRAL_PROMPT_CACHE_AFFINITY", raising=False)
    config_settings.get_settings.cache_clear()

    legacy_profile = replace(
        MISTRAL_PROFILE,
        response_surfaces=(),
        no_information_surface=None,
    )
    legacy = _make_provider(provider_id, STUDIO_KEY, profile=legacy_profile)
    current = _make_provider(provider_id, STUDIO_KEY, profile=MISTRAL_PROFILE)

    captured_legacy: list[tuple[str, str, dict[str, str], bytes]] = []
    captured_current: list[tuple[str, str, dict[str, str], bytes]] = []

    def _record_into(sink: list[tuple[str, str, dict[str, str], bytes]]):
        def _handler(request: httpx.Request) -> httpx.Response:
            sink.append(
                (
                    request.method,
                    str(request.url),
                    {
                        k.lower(): v
                        for k, v in request.headers.items()
                        if k.lower() != "user-agent"
                    },
                    request.content,
                )
            )
            return _chat_sse_response()

        return _handler

    legacy._client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_record_into(captured_legacy))
    )
    current._client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_record_into(captured_current))
    )

    request = make_messages_request(
        "fixture-model",
        tools=[
            {
                "name": "echo",
                "description": "Echo input.",
                "input_schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            }
        ],
    )
    reasoning = reasoning_for(request)
    legacy_events = [
        ev async for ev in legacy.stream_response(request, reasoning=reasoning)
    ]
    current_events = [
        ev async for ev in current.stream_response(request, reasoning=reasoning)
    ]

    assert len(captured_legacy) == 1
    assert len(captured_current) == 1
    assert captured_current == captured_legacy
    assert captured_current[0][0] == "POST"
    assert captured_current[0][1] == f"{MISTRAL_DEFAULT_BASE}/chat/completions"
    assert len(legacy_events) == len(current_events)


@pytest.mark.asyncio
@pytest.mark.parametrize("affinity_enabled", [False, True])
@pytest.mark.parametrize(
    ("provider_id", "base_url"),
    [(STUDIO, MISTRAL_EU_BASE), (VIBE, MISTRAL_US_BASE)],
)
async def test_responses_surface_uses_configured_region_and_respects_cache_affinity(
    provider_id: str,
    base_url: str,
    affinity_enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MISTRAL_PROMPT_CACHE_AFFINITY", "true" if affinity_enabled else "false"
    )
    config_settings.get_settings.cache_clear()

    table = overrides_module.ModelParameterOverrides.from_document(
        {"models": {f"{provider_id}/fixture-model": {"response_surface": "responses"}}}
    )
    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.response_surface.current_model_overrides",
        lambda: table,
    )

    captured: list[tuple[str, str, dict[str, str], dict[str, Any]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                request.method,
                str(request.url),
                {k.lower(): v for k, v in request.headers.items()},
                json.loads(request.content.decode("utf-8")),
            )
        )
        return _responses_sse_response("regional-responses-ok")

    provider = _make_provider(provider_id, STUDIO_KEY, base_url=base_url)
    install_fingerprint({"x-claude-code-session-id": "mistral-responses-session"})
    try:
        # Force ResponsesTransport creation and inject mock transport.
        provider._responses._client = httpx.AsyncClient(
            transport=httpx.MockTransport(_handler)
        )
        request = make_messages_request(
            "fixture-model",
            extra_body={"metadata": {"trace": "ok"}},
        )
        events = [
            ev
            async for ev in provider.stream_response(
                request, reasoning=reasoning_for(request)
            )
        ]
    finally:
        install_fingerprint(None)
        config_settings.get_settings.cache_clear()

    assert len(captured) == 1
    method, url, headers, body = captured[0]
    assert method == "POST"
    assert url == f"{base_url}/responses"
    assert body["model"] == "fixture-model"
    assert body["metadata"] == {"trace": "ok"}
    if affinity_enabled:
        assert body["prompt_cache_key"].startswith("mcc-aff-")
        assert headers["x-affinity"] == body["prompt_cache_key"]
    else:
        assert "prompt_cache_key" not in body
        assert "x-affinity" not in headers
    assert any("regional-responses-ok" in event for event in events)


@pytest.mark.asyncio
async def test_responses_surface_extra_body_rejects_canonical_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = overrides_module.ModelParameterOverrides.from_document(
        {"models": {f"{STUDIO}/fixture-model": {"response_surface": "responses"}}}
    )
    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.response_surface.current_model_overrides",
        lambda: table,
    )
    provider = _make_provider(STUDIO, STUDIO_KEY, base_url=MISTRAL_EU_BASE)
    bad = make_messages_request("fixture-model", extra_body={"model": "evil"})
    with pytest.raises(InvalidRequestError):
        async for _ in provider.stream_response(bad, reasoning=reasoning_for(bad)):
            pass


@pytest.mark.asyncio
async def test_learned_responses_surface_is_isolated_between_studio_and_vibe() -> None:
    """IV.24: learning /responses on Studio never switches Vibe (or vice versa)."""
    remember_response_surfaces(
        STUDIO,
        "fixture-shared-model",
        (ResponseSurface.RESPONSES,),
        evidence="test learned responses on Studio",
    )

    studio = _make_provider(STUDIO, STUDIO_KEY, base_url=MISTRAL_DEFAULT_BASE)
    vibe = _make_provider(VIBE, VIBE_KEY, base_url=MISTRAL_DEFAULT_BASE)

    assert studio._responses._provider_id == STUDIO
    assert vibe._responses._provider_id == VIBE
    assert studio._responses._memory is not vibe._responses._memory
    assert (
        studio.resolved_surface("fixture-shared-model").surface
        is ResponseSurface.RESPONSES
    )
    assert (
        vibe.resolved_surface("fixture-shared-model").surface
        is ResponseSurface.CHAT_COMPLETIONS
    )

    studio_urls: list[str] = []
    vibe_urls: list[str] = []

    def _studio_handler(request: httpx.Request) -> httpx.Response:
        studio_urls.append(str(request.url))
        return _responses_sse_response("studio-resp")

    def _vibe_handler(request: httpx.Request) -> httpx.Response:
        vibe_urls.append(str(request.url))
        return _chat_sse_response()

    studio._responses._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_studio_handler)
    )
    vibe._client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_vibe_handler)
    )

    req = make_messages_request("fixture-shared-model")
    _ = [ev async for ev in studio.stream_response(req, reasoning=reasoning_for(req))]
    _ = [ev async for ev in vibe.stream_response(req, reasoning=reasoning_for(req))]

    assert studio_urls == [f"{MISTRAL_DEFAULT_BASE}/responses"]
    assert vibe_urls == [f"{MISTRAL_DEFAULT_BASE}/chat/completions"]


@pytest.mark.asyncio
async def test_surface_fallback_probes_responses_and_remembers_per_provider() -> None:
    """When /chat/completions refuses the endpoint, MCC probes /responses & remembers it."""
    studio = _make_provider(STUDIO, STUDIO_KEY, base_url=MISTRAL_EU_BASE)
    vibe = _make_provider(VIBE, VIBE_KEY, base_url=MISTRAL_US_BASE)

    seen_urls: list[str] = []

    def _chat_404(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            404,
            request=request,
            json={"error": {"message": "Not found on chat/completions"}},
        )

    def _responses_ok(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        payload = json.loads(request.content.decode("utf-8"))
        if not payload.get("stream"):
            return httpx.Response(
                200,
                request=request,
                json={"id": "resp_probe", "model": "fixture-probe-model", "output": []},
            )
        return _responses_sse_response("probed-ok")

    studio._client._client = httpx.AsyncClient(transport=httpx.MockTransport(_chat_404))
    studio._responses._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_responses_ok)
    )

    req = make_messages_request("fixture-probe-model")
    events = [
        ev async for ev in studio.stream_response(req, reasoning=reasoning_for(req))
    ]
    assert any("probed-ok" in event for event in events)
    assert seen_urls == [
        f"{MISTRAL_EU_BASE}/chat/completions",
        f"{MISTRAL_EU_BASE}/responses",
        f"{MISTRAL_EU_BASE}/responses",
    ]
    assert (
        studio.resolved_surface("fixture-probe-model").surface
        is ResponseSurface.RESPONSES
    )
    assert (
        vibe.resolved_surface("fixture-probe-model").surface
        is ResponseSurface.CHAT_COMPLETIONS
    )
