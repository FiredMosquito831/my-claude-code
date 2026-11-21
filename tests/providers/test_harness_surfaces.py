"""Per-harness wire surface preferences (`harness_surfaces`, v8.35.0, PR B).

Proves Track 3 PR B under binding Plan A without live upstream calls:

1. Plan A attribution (`core/client_fingerprint.py`):
   - ``ClientFingerprint.harness`` is populated before the ``EMPTY_FINGERPRINT``
     guard, so a request sending only ``x-mcc-harness`` does not collapse to
     ``EMPTY_FINGERPRINT``.
   - Unknown/auth-only headers still yield ``EMPTY_FINGERPRINT``.
   - No-header harnesses (``kimi_code``, ``goose``, ``aider``, ``droid``,
     ``antigravity``) classify via User-Agent; Claude Desktop shares ``claude``
     (Q8).
   - ``build_capture`` -> ``install_fingerprint(headers)`` propagates harness into
     ``ProviderExecutor`` and internal ``VisionDescribeAdapter`` calls (2026-11-10 Q7)
     with zero edits to ``application/execution.py`` (IV.8).
2. Storage & validation (`config/model_overrides.py`):
   - ``harness_surfaces`` round-trips atomically under ``model_overrides.json``,
     rejects unknown harnesses, ``null``, ``""``, and ``"unservable"``, and
     removes empty harness dicts cleanly on reset.
3. Precedence (`providers/openai_chat/response_surface.py`):
   - ``pin (response_surface) > harness_surfaces[h][p] >
     models[ref].preferred_surface (incl. explicit null) >
     providers[p].preferred_surface > learned > measured > registry >
     marketplace > floor``.
   - Outside a request (``current_fingerprint().harness == "unknown"``),
     ``response_surface_payload`` / Models page stays unchanged (IV.7).
   - Refusal tracking (Q9) records ``FACT_PREFERRED_SURFACE_REFUSED`` and
     falls back inside the same attempt without rewriting ``model_overrides.json``.
4. Admin API (`GET`/`POST /admin/api/harness-surfaces`):
   - Loopback-only (403 on non-loopback), strictly gated to ``>= 2``-door
     providers (Q11), includes Q8 attribution disclaimer, validates with 422,
     and cleanly deletes entries on ``"auto"`` / ``""`` / ``None``.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.handlers import MessagesHandler
from my_claude_code.api.model_admin import response_surface_payload, speakable_surfaces
from my_claude_code.api.request_capture import build_capture
from my_claude_code.application.model_metadata import (
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.application.routing import ModelRouter
from my_claude_code.application.vision_describe import VisionDescribeAdapter
from my_claude_code.config.model_overrides import (
    HARNESS_SURFACES_KEY,
    ModelParameterOverrides,
    current_model_overrides,
    load_model_overrides,
    reset_model_overrides_cache,
    save_model_overrides,
    with_harness_surface_row,
)
from my_claude_code.config.provider_registry import ProviderRegistry
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.client_family import ClientFamily
from my_claude_code.core.client_fingerprint import (
    EMPTY_FINGERPRINT,
    HARNESS_HEADER,
    UNKNOWN_HARNESS,
    current_fingerprint,
    fingerprint_from_headers,
    install_fingerprint,
)
from my_claude_code.core.request_log import store_from_settings
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
    resolve_surface_ladder,
)
from my_claude_code.providers.openai_chat.response_surface import (
    preferred_surface_of,
    preferred_surface_refusal_fact,
    remember_response_surfaces,
)
from my_claude_code.providers.recovery import learned_fact_store
from my_claude_code.providers.runtime import models_dev
from tests.api.support import create_test_app
from tests.providers.support import passthrough_rate_limiter

HERE = Path(__file__).parent
BUCKETS_PATH = HERE / "opencode_go_registry_buckets.json"
GO = OPENAI_CHAT_PROFILES["opencode_go"]
GO_BASE = "https://opencode.ai/inference/go/v1"
DEEPSEEK = "deepseek-v4.1-flash"
MUSE = "muse-spark-1.3-contributor"
PIXEL_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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
    install_fingerprint(None)


def _save_overrides(document: dict[str, Any]) -> None:
    save_model_overrides(ModelParameterOverrides.from_document(document))


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
# 1. Plan A attribution in core/client_fingerprint.py
# --------------------------------------------------------------------------


def test_harness_header_alone_does_not_collapse_to_empty_fingerprint() -> None:
    fp = fingerprint_from_headers({HARNESS_HEADER: "openclaw"})
    assert fp is not EMPTY_FINGERPRINT
    assert fp.is_empty is False
    assert fp.harness == "openclaw"
    assert fp.user_agent is None


def test_unknown_and_auth_headers_still_return_empty_fingerprint() -> None:
    fp = fingerprint_from_headers(
        {
            "authorization": "Bearer sk-secret",
            "x-api-key": "sk-secret",
            "cookie": "session=abc",
            "x-claude-code-session-id": "cc-123",
        }
    )
    assert fp is EMPTY_FINGERPRINT
    assert fp.is_empty is True
    assert fp.harness == UNKNOWN_HARNESS


@pytest.mark.parametrize(
    ("ua", "expected"),
    [
        ("kimi-code/1.2.3", "kimi_code"),
        ("goose/1.0.2", "goose"),
        ("aider/0.82.0", "aider"),
        ("factory-cli/0.210.0", "droid"),
        ("Antigravity/1.0.14", "antigravity"),
        # Q8: Claude Desktop sends Claude Code's UA without x-mcc-harness -> claude
        ("claude-cli/2.1.0 (external, cli)", "claude"),
    ],
)
def test_no_header_harnesses_and_claude_desktop_attribute_via_ua(
    ua: str, expected: str
) -> None:
    fp = fingerprint_from_headers({"user-agent": ua})
    assert fp.harness == expected
    assert fp.is_empty is False


# --------------------------------------------------------------------------
# 2. Storage, parsing & atomic round-trip in config/model_overrides.py
# --------------------------------------------------------------------------


def test_harness_surfaces_round_trip_and_validation(tmp_path: Path) -> None:
    doc = {
        "providers": {"opencode_go": {"preferred_surface": "chat_completions"}},
        "models": {
            "opencode_go/deepseek-v4.1-flash": {"preferred_surface": "messages"}
        },
        HARNESS_SURFACES_KEY: {
            "codex": {"opencode_go": "responses"},
            "claude": {"opencode_go": "chat_completions"},
            "unknown_agent": {"opencode_go": "responses"},
            "opencode": {
                "opencode_go": "unservable",
                "mistral": None,
                "inferhub": "",
                "custom_one": 123,
            },
        },
    }
    parsed = ModelParameterOverrides.from_document(doc)
    assert parsed.is_empty is False
    assert parsed.harness_surface("codex", "opencode_go") == "responses"
    assert parsed.harness_surface("CLAUDE", "OPENCODE_GO") == "chat_completions"
    assert parsed.harness_surface("unknown_agent", "opencode_go") is None
    assert parsed.harness_surfaces_for("opencode") == {}

    save_model_overrides(parsed)
    reset_model_overrides_cache()
    reloaded = load_model_overrides()
    assert reloaded.harness_surface("codex", "opencode_go") == "responses"
    assert reloaded.harness_surface("claude", "opencode_go") == "chat_completions"
    assert HARNESS_SURFACES_KEY in reloaded.as_document()

    cleared = with_harness_surface_row(
        reloaded, harness="codex", provider_id="opencode_go", surface=None
    )
    cleared = with_harness_surface_row(
        cleared, harness="claude", provider_id="opencode_go", surface=""
    )
    assert cleared.harness_surfaces == {}
    assert HARNESS_SURFACES_KEY not in cleared.as_document()

    save_model_overrides(cleared)
    on_disk = json.loads(
        (tmp_path / "model_overrides.json").read_text(encoding="utf-8")
    )
    assert HARNESS_SURFACES_KEY not in on_disk


# --------------------------------------------------------------------------
# 3. Resolver precedence & Plan A propagation
# --------------------------------------------------------------------------


def test_precedence_pin_beats_harness_beats_model_incl_null_beats_provider() -> None:
    _save_overrides(
        {
            "providers": {"opencode_go": {"preferred_surface": "chat_completions"}},
            "models": {
                f"opencode_go/{DEEPSEEK}": {"preferred_surface": None},
                f"opencode_go/{MUSE}": {"preferred_surface": "messages"},
            },
            HARNESS_SURFACES_KEY: {
                "codex": {"opencode_go": "responses"},
                "openclaw": {"opencode_go": "messages"},
            },
        }
    )

    # Outside a request (unknown harness): model null cancels provider pref -> None.
    assert current_fingerprint().harness == UNKNOWN_HARNESS
    assert preferred_surface_of("opencode_go", DEEPSEEK) is None
    # Outside a request: model pref wins over provider pref.
    assert preferred_surface_of("opencode_go", MUSE) is ResponseSurface.MESSAGES

    # Inside codex request: harness_surfaces[codex] beats BOTH model null and model pref!
    install_fingerprint({HARNESS_HEADER: "codex"})
    assert preferred_surface_of("opencode_go", DEEPSEEK) is ResponseSurface.RESPONSES
    assert preferred_surface_of("opencode_go", MUSE) is ResponseSurface.RESPONSES

    # Explicit pin (`response_surface`) STILL beats harness_surfaces!
    _save_overrides(
        {
            "models": {
                f"opencode_go/{DEEPSEEK}": {
                    "response_surface": "chat_completions",
                    "preferred_surface": None,
                }
            },
            HARNESS_SURFACES_KEY: {"codex": {"opencode_go": "responses"}},
        }
    )
    ladder = resolve_surface_ladder(
        "opencode_go",
        DEEPSEEK,
        client_family=ClientFamily.OPENAI,
        registry_provider=GO.surface_registry_provider,
        declared=GO.response_surfaces,
        no_information=GO.no_information_surface,
        measured=GO.measured_surfaces,
        optimistic=GO.optimistic_first_try,
    )
    assert [c.surface for c in ladder.candidates] == [ResponseSurface.CHAT_COMPLETIONS]
    assert ladder.primary.source is ResponseSurfaceSource.OVERRIDE


def test_outside_request_models_page_ignores_harness_surfaces() -> None:
    _save_overrides(
        {
            "providers": {"opencode_go": {"preferred_surface": "chat_completions"}},
            HARNESS_SURFACES_KEY: {"codex": {"opencode_go": "responses"}},
        }
    )
    payload = response_surface_payload("opencode_go", DEEPSEEK)
    assert payload is not None
    assert payload["preferred"] == "chat_completions"
    assert payload["preferred_scope"] == "provider"


def test_build_capture_and_vision_describe_inherit_client_harness() -> None:
    _save_overrides({HARNESS_SURFACES_KEY: {"openclaw": {"opencode_go": "responses"}}})
    req = MessagesRequest(
        model=f"opencode_go/{DEEPSEEK}",
        max_tokens=16,
        messages=[Message(role="user", content="hi")],
    )
    settings = Settings(request_log_enabled=False)
    build_capture(
        settings,
        req,
        request_id="req-plan-a",
        endpoint="/v1/chat/completions",
        protocol="openai_chat",
        headers={HARNESS_HEADER: "openclaw"},
    )
    assert current_fingerprint().harness == "openclaw"
    assert preferred_surface_of("opencode_go", DEEPSEEK) is ResponseSurface.RESPONSES


@pytest.mark.asyncio
async def test_vision_describe_inherits_client_harness(
    tmp_path: Path,
) -> None:
    _save_overrides({HARNESS_SURFACES_KEY: {"openclaw": {"opencode_go": "responses"}}})
    seen_harnesses: list[str] = []
    seen_surfaces: list[ResponseSurface | None] = []

    class _DescribeProvider:
        @property
        def credential_label(self) -> str | None:
            return None

        def throttle_remaining(self, model: str | None = None) -> float:
            return 0.0

        def preflight_stream(self, request: MessagesRequest, *, reasoning: Any) -> None:
            return None

        async def stream_response(
            self,
            request: MessagesRequest,
            input_tokens: int = 0,
            *,
            request_id: str | None = None,
            reasoning: Any = None,
        ):
            seen_harnesses.append(current_fingerprint().harness)
            seen_surfaces.append(preferred_surface_of("opencode_go", DEEPSEEK))
            yield 'event: message_start\ndata: {"type":"message_start","message":{"model":"deepseek-v4.1-flash"}}\n\n'
            yield 'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
            yield 'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"one pixel"}}\n\n'
            yield 'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
            yield 'event: message_stop\ndata: {"type":"message_stop"}\n\n'

    settings = Settings.model_validate(
        {
            "MCC_CONFIG_DIR": str(tmp_path),
            "VISION_ADAPTER_MODE": "describe",
            "MODEL": "nvidia_nim/test-text",
            "MODEL_VISION": f"opencode_go/{DEEPSEEK}",
            "NVIDIA_NIM_API_KEY": "nvapi-test",
            "OPENCODE_API_KEY": "oc-test",
        }
    )
    router = ModelRouter(
        settings,
        vision_lookup=lambda _provider, model: model == DEEPSEEK,
    )
    describe_provider = _DescribeProvider()
    handler = MessagesHandler(
        settings,
        provider_resolver=lambda _pid: describe_provider,
        model_router=router,
    )
    adapter = VisionDescribeAdapter(
        router=router,
        executor=handler._provider_executor,
        store=store_from_settings(settings),
    )

    req = MessagesRequest.model_validate(
        {
            "model": "nvidia_nim/test-text",
            "max_tokens": 16,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": PIXEL_B64,
                            },
                        },
                        {"type": "text", "text": "describe"},
                    ],
                }
            ],
        }
    )
    install_fingerprint({HARNESS_HEADER: "openclaw"})
    result = await adapter.apply(req, request_id="req-vision-q7")
    assert result.applied is True
    assert seen_harnesses == ["openclaw"]
    assert seen_surfaces == [ResponseSurface.RESPONSES]


@pytest.mark.asyncio
async def test_harness_surface_refusal_falls_back_without_mutating_overrides() -> None:
    """Q9 still applies when preference comes from ``harness_surfaces``."""

    _save_overrides({HARNESS_SURFACES_KEY: {"codex": {"opencode_go": "responses"}}})
    remember_response_surfaces(
        "opencode_go",
        DEEPSEEK,
        (ResponseSurface.RESPONSES, ResponseSurface.CHAT_COMPLETIONS),
        evidence="test setup",
    )

    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/responses"):
            return httpx.Response(500, json={"error": "bad endpoint"})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_chat_sse_bytes(),
        )

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = create_openai_chat_provider(
        "opencode_go",
        ProviderConfig(api_key="oc-test", base_url=GO_BASE),
        passthrough_rate_limiter(),
        profile=GO,
    )
    provider._client._client = mock_http
    if provider._responses is not None:
        provider._responses._client = mock_http
    if provider._messages is not None:
        provider._messages._provider._client = mock_http
    try:
        install_fingerprint({HARNESS_HEADER: "codex"})
        req = MessagesRequest(
            model=DEEPSEEK,
            max_tokens=64,
            messages=[Message(role="user", content="hello")],
        )
        chunks = [c async for c in provider.stream_response(req)]
    finally:
        await mock_http.aclose()

    assert any("chat" in p for p in seen_paths)
    assert seen_paths[0].endswith("/responses")
    assert seen_paths[1].endswith("/chat/completions")
    assert chunks
    # Refusal recorded; model_overrides.json NOT overwritten!
    assert (
        preferred_surface_refusal_fact(
            "opencode_go", DEEPSEEK, ResponseSurface.RESPONSES
        )
        is not None
    )
    assert (
        current_model_overrides().harness_surface("codex", "opencode_go") == "responses"
    )


# --------------------------------------------------------------------------
# 4. Admin API: GET / POST /admin/api/harness-surfaces
# --------------------------------------------------------------------------


def _loopback_client(settings: Settings | None = None) -> TestClient:
    app = create_test_app(settings or Settings(model="nvidia_nim/a"))
    return TestClient(app, client=("127.0.0.1", 50000))


def test_harness_surfaces_routes_require_loopback() -> None:
    remote = TestClient(
        create_test_app(Settings(model="nvidia_nim/a")),
        client=("203.0.113.10", 50000),
    )
    assert remote.get("/admin/api/harness-surfaces").status_code == 403
    assert (
        remote.post(
            "/admin/api/harness-surfaces",
            json={
                "harness": "codex",
                "provider": "opencode_go",
                "surface": "responses",
            },
        ).status_code
        == 403
    )


def test_get_harness_surfaces_filters_strictly_to_multi_door_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = ProviderRegistry(path=tmp_path / "custom_providers.json")
    single_entry = registry.add(
        display_name="Single Custom",
        base_url="http://127.0.0.1:18599/v1",
        api_keys=["test-key"],
        surfaces=("chat_completions",),
    )
    multi_entry = registry.add(
        display_name="Multi Custom",
        base_url="http://127.0.0.1:18598/v1",
        api_keys=["test-key"],
        surfaces=("chat_completions", "responses", "messages"),
    )
    monkeypatch.setattr(
        "my_claude_code.config.provider_registry.get_provider_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "my_claude_code.api.admin_harness_routes.get_provider_registry",
        lambda: registry,
    )

    client = _loopback_client()
    resp = client.get("/admin/api/harness-surfaces")
    assert resp.status_code == 200
    payload = resp.json()

    provider_ids = [p["provider_id"] for p in payload["providers"]]
    assert {"opencode", "opencode_go", "inferhub", "mistral", "mistral_vibe"} <= set(
        provider_ids
    )
    assert multi_entry.provider_id in provider_ids
    # Single-door providers MUST NOT appear (Q11).
    for single in (
        "groq",
        "anthropic",
        "open_router",
        "deepseek",
        single_entry.provider_id,
    ):
        assert single not in provider_ids
    for entry in payload["providers"]:
        assert len(entry["offered"]) >= 2
        if not entry["custom"]:
            assert len(speakable_surfaces(entry["provider_id"])) >= 2

    # Claude (no catalogue, so absent from harness-tiers) IS present here!
    assert "claude" in payload["harnesses"]
    assert "codex" in payload["harnesses"]
    assert "x-mcc-harness" in payload["attribution_note"]
    assert "Claude Desktop" in payload["attribution_note"]


def test_post_harness_surfaces_saves_validates_422_and_resets_auto(
    tmp_path: Path,
) -> None:
    client = _loopback_client()

    # Valid save
    r1 = client.post(
        "/admin/api/harness-surfaces",
        json={
            "harness": "codex",
            "provider": "opencode_go",
            "surface": "responses",
        },
    )
    assert r1.status_code == 200
    assert r1.json()["harnesses"]["codex"]["opencode_go"] == "responses"
    disk = json.loads((tmp_path / "model_overrides.json").read_text(encoding="utf-8"))
    assert disk[HARNESS_SURFACES_KEY] == {"codex": {"opencode_go": "responses"}}

    # Invalid cases -> HTTP 422
    for bad in (
        {"harness": "not_a_harness", "provider": "opencode_go", "surface": "responses"},
        {"harness": "codex", "provider": "groq", "surface": "chat_completions"},
        {"harness": "codex", "provider": "groq", "surface": "auto"},
        {"harness": "codex", "provider": "unknown_provider", "surface": "responses"},
        {"harness": "codex", "provider": "opencode_go", "surface": "unservable"},
        {"harness": "codex", "provider": "opencode_go", "surface": "gemini"},
        {"harness": "codex", "provider": "mistral", "surface": "messages"},
    ):
        bad_resp = client.post("/admin/api/harness-surfaces", json=bad)
        assert bad_resp.status_code == 422, (bad, bad_resp.text)

    # Reset via "auto" removes the entry cleanly
    r2 = client.post(
        "/admin/api/harness-surfaces",
        json={"harness": "codex", "provider": "opencode_go", "surface": "auto"},
    )
    assert r2.status_code == 200
    assert r2.json()["harnesses"]["codex"]["opencode_go"] == ""
    disk_after = json.loads(
        (tmp_path / "model_overrides.json").read_text(encoding="utf-8")
    )
    assert HARNESS_SURFACES_KEY not in disk_after
