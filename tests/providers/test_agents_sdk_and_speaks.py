"""PR 12 (EM-PR4): Agents tab SDK harnesses (`claude_agent_sdk`, etc.) and `HarnessSpec.speaks`."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.request_capture import build_capture
from my_claude_code.config.constants import (
    SDK_HARNESS_IDS,
    SELECTABLE_SDK_HARNESS_IDS,
)
from my_claude_code.config.harnesses import (
    HARNESS_SPECS,
    PROTOCOL_LABELS,
    HarnessProtocol,
    harness_ids,
    harness_spec,
    sdk_harness_spec,
    sdk_harness_specs,
)
from my_claude_code.config.model_overrides import (
    HARNESS_SURFACES_KEY,
    ModelParameterOverrides,
    current_model_overrides,
    load_model_overrides,
    reset_model_overrides_cache,
    save_model_overrides,
    with_harness_surface_row,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.client_fingerprint import (
    current_fingerprint,
    install_fingerprint,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.commandcode import CommandCodeProvider
from my_claude_code.providers.openai_chat.response_surface import (
    preferred_surface_of,
)
from my_claude_code.providers.recovery import learned_fact_store
from tests.api.support import create_test_app
from tests.providers.support import passthrough_rate_limiter

CLAUDE_AGENT_SDK_UA = "claude-cli/2.1.258 (external, sdk-py, agent-sdk/0.1.0)"
COMMANDCODE_MODEL = "deepseek/deepseek-v4.1-flash"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    reset_model_overrides_cache()
    learned_fact_store().forget_all()
    install_fingerprint(None)


def _loopback_client(settings: Settings | None = None) -> TestClient:
    app = create_test_app(settings or Settings(model="nvidia_nim/a"))
    return TestClient(app, client=("127.0.0.1", 50000))


def _sse_stream(payload: bytes) -> httpx.Response:
    async def _gen() -> Any:
        yield payload

    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_gen(),
    )


def _chat_sse_bytes(model: str) -> bytes:
    events = [
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}],
        },
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    ]
    return (
        "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode("utf-8")
        + b"data: [DONE]\n\n"
    )


def _responses_sse_bytes() -> bytes:
    lines = [
        'event: response.created\ndata: {"type":"response.created","response":{"id":"r1","status":"in_progress"}}\n\n',
        'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":0,"item":{"id":"msg1","type":"message","role":"assistant","content":[]}}\n\n',
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","item_id":"msg1","output_index":0,"content_index":0,"delta":"ok"}\n\n',
        'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r1","status":"completed","usage":{"input_tokens":10,"output_tokens":2}}}\n\n',
    ]
    return "".join(lines).encode("utf-8")


# --------------------------------------------------------------------------
# 1. HarnessSpec.speaks & SDK_HARNESS_SPECS schema contracts
# --------------------------------------------------------------------------


def test_every_harness_spec_speaks_is_valid_unique_and_includes_protocol() -> None:
    for spec in HARNESS_SPECS:
        speaks = spec.effective_speaks()
        assert speaks, spec.id
        assert spec.protocol in speaks, spec.id
        assert len(speaks) == len(set(speaks)), spec.id
        for protocol in speaks:
            assert protocol in PROTOCOL_LABELS, (spec.id, protocol)

    assert harness_spec("claude").effective_speaks() == (
        HarnessProtocol.ANTHROPIC_MESSAGES,
    )
    assert harness_spec("codex").effective_speaks() == (
        HarnessProtocol.OPENAI_RESPONSES,
    )
    assert harness_spec("gemini_cli").effective_speaks() == (HarnessProtocol.GEMINI,)
    assert harness_spec("opencode").effective_speaks() == (
        HarnessProtocol.ANTHROPIC_MESSAGES,
        HarnessProtocol.OPENAI_RESPONSES,
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
    )
    assert harness_spec("opencode2").effective_speaks() == (
        HarnessProtocol.ANTHROPIC_MESSAGES,
        HarnessProtocol.OPENAI_RESPONSES,
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
    )
    assert harness_spec("kilo").effective_speaks() == (
        HarnessProtocol.ANTHROPIC_MESSAGES,
        HarnessProtocol.OPENAI_RESPONSES,
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
    )
    assert harness_spec("commandcode_cli").effective_speaks() == (
        HarnessProtocol.ANTHROPIC_MESSAGES,
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
    )
    assert harness_spec("openclaw").effective_speaks() == (
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        HarnessProtocol.ANTHROPIC_MESSAGES,
    )
    assert harness_spec("hermes").effective_speaks() == (
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        HarnessProtocol.ANTHROPIC_MESSAGES,
        HarnessProtocol.OPENAI_RESPONSES,
    )
    assert harness_spec("kimi_code").effective_speaks() == (
        HarnessProtocol.ANTHROPIC_MESSAGES,
        HarnessProtocol.OPENAI_RESPONSES,
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
    )
    assert harness_spec("crush").effective_speaks() == (
        HarnessProtocol.ANTHROPIC_MESSAGES,
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
    )
    assert harness_spec("goose").effective_speaks() == (
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        HarnessProtocol.ANTHROPIC_MESSAGES,
    )
    assert harness_spec("droid").effective_speaks() == (
        HarnessProtocol.ANTHROPIC_MESSAGES,
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
    )


def test_sdk_harness_specs_stay_separate_from_launchable_registry() -> None:
    registry_ids = set(harness_ids())
    for sdk_id in SDK_HARNESS_IDS:
        assert sdk_id not in registry_ids
        sdk = sdk_harness_spec(sdk_id)
        speaks = sdk.effective_speaks()
        assert sdk.protocol in speaks
        assert all(p in PROTOCOL_LABELS for p in speaks)
    assert [sdk.id for sdk in sdk_harness_specs()] == list(SDK_HARNESS_IDS)
    assert "claude_agent_sdk" not in registry_ids


# --------------------------------------------------------------------------
# 2. model_overrides.py persistence for SDK harness IDs
# --------------------------------------------------------------------------


def test_model_overrides_persists_all_sdk_harnesses_and_drops_unknown(
    tmp_path: Path,
) -> None:
    doc: dict[str, Any] = {
        HARNESS_SURFACES_KEY: {
            "claude_agent_sdk": {"commandcode": "chat_completions"},
            "anthropic_sdk": {"opencode_go": "messages"},
            "openai_sdk": {"opencode_go": "responses"},
            "google_sdk": {"mistral": "responses"},
            "ai_sdk": {"opencode": "chat_completions"},
            "unknown": {"opencode_go": "responses"},
            "script": {"opencode_go": "responses"},
            "nonexistent": {"opencode_go": "responses"},
        }
    }
    parsed = ModelParameterOverrides.from_document(doc)
    for sdk_id in SELECTABLE_SDK_HARNESS_IDS:
        assert parsed.harness_surfaces_for(sdk_id), sdk_id
    assert (
        parsed.harness_surface("claude_agent_sdk", "commandcode") == "chat_completions"
    )
    assert parsed.harness_surfaces_for("unknown") == {}
    assert parsed.harness_surfaces_for("script") == {}
    assert parsed.harness_surfaces_for("nonexistent") == {}

    save_model_overrides(parsed)
    reset_model_overrides_cache()
    reloaded = load_model_overrides()
    assert (
        reloaded.harness_surface("claude_agent_sdk", "commandcode")
        == "chat_completions"
    )
    assert reloaded.harness_surface("openai_sdk", "opencode_go") == "responses"

    cleared = with_harness_surface_row(
        reloaded, harness="claude_agent_sdk", provider_id="commandcode", surface=None
    )
    assert cleared.harness_surface("claude_agent_sdk", "commandcode") is None
    save_model_overrides(cleared)
    on_disk = json.loads(
        (tmp_path / "model_overrides.json").read_text(encoding="utf-8")
    )
    assert "claude_agent_sdk" not in on_disk.get(HARNESS_SURFACES_KEY, {})


# --------------------------------------------------------------------------
# 3. Admin API: /admin/api/harnesses & /admin/api/harness-surfaces (Q8 parity)
# --------------------------------------------------------------------------


def test_harness_surfaces_api_accepts_sdk_harnesses_and_keeps_all_doors_q8(
    tmp_path: Path,
) -> None:
    client = _loopback_client()

    get_resp = client.get("/admin/api/harness-surfaces")
    assert get_resp.status_code == 200
    payload = get_resp.json()

    sdk_entries = {entry["id"]: entry for entry in payload["sdk_harnesses"]}
    assert list(sdk_entries) == list(SDK_HARNESS_IDS)
    assert sdk_entries["claude_agent_sdk"]["display_name"] == "Claude Agent SDK"
    assert sdk_entries["claude_agent_sdk"]["speaks"] == ["anthropic_messages"]
    assert "Anthropic Messages" in sdk_entries["claude_agent_sdk"]["speaks_labels"][0]
    for sdk_id in SDK_HARNESS_IDS:
        assert sdk_id in payload["harnesses"]

    # Q8: commandcode exposes BOTH chat_completions and responses regardless of agent speaks.
    providers_by_id = {p["provider_id"]: p for p in payload["providers"]}
    assert "commandcode" in providers_by_id
    cc_doors = [opt["value"] for opt in providers_by_id["commandcode"]["offered"]]
    assert cc_doors == ["chat_completions", "responses"]

    # Save claude_agent_sdk -> commandcode = chat_completions -> 200!
    post_resp = client.post(
        "/admin/api/harness-surfaces",
        json={
            "harness": "claude_agent_sdk",
            "provider": "commandcode",
            "surface": "chat_completions",
        },
    )
    assert post_resp.status_code == 200
    assert (
        post_resp.json()["harnesses"]["claude_agent_sdk"]["commandcode"]
        == "chat_completions"
    )
    disk = json.loads((tmp_path / "model_overrides.json").read_text(encoding="utf-8"))
    assert disk[HARNESS_SURFACES_KEY] == {
        "claude_agent_sdk": {"commandcode": "chat_completions"}
    }

    # Invalid harness IDs and single-door providers still return HTTP 422.
    for bad in (
        {
            "harness": "unknown",
            "provider": "commandcode",
            "surface": "chat_completions",
        },
        {"harness": "script", "provider": "commandcode", "surface": "chat_completions"},
        {"harness": "", "provider": "commandcode", "surface": "chat_completions"},
        {
            "harness": "claude_agent_sdk",
            "provider": "groq",
            "surface": "chat_completions",
        },
    ):
        assert client.post("/admin/api/harness-surfaces", json=bad).status_code == 422

    # Reset via "auto" cleanly deletes the override.
    reset_resp = client.post(
        "/admin/api/harness-surfaces",
        json={
            "harness": "claude_agent_sdk",
            "provider": "commandcode",
            "surface": "auto",
        },
    )
    assert reset_resp.status_code == 200
    assert reset_resp.json()["harnesses"]["claude_agent_sdk"]["commandcode"] == ""


# --------------------------------------------------------------------------
# 4. End-to-end attribution + routing for claude_agent_sdk via MockTransport
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_agent_sdk_user_agent_routes_using_harness_surfaces_override() -> (
    None
):
    save_model_overrides(
        ModelParameterOverrides.from_document(
            {
                HARNESS_SURFACES_KEY: {
                    "claude_agent_sdk": {"commandcode": "chat_completions"}
                }
            }
        )
    )
    req = MessagesRequest(
        model=f"commandcode/{COMMANDCODE_MODEL}",
        max_tokens=32,
        messages=[Message(role="user", content="hello")],
    )
    build_capture(
        Settings(request_log_enabled=False),
        req,
        request_id="req-sdk-1",
        endpoint="/v1/messages",
        protocol="anthropic",
        headers={"user-agent": CLAUDE_AGENT_SDK_UA},
    )
    assert current_fingerprint().harness == "claude_agent_sdk"
    assert preferred_surface_of("commandcode", COMMANDCODE_MODEL) is not None

    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/responses"):
            return _sse_stream(_responses_sse_bytes())
        return _sse_stream(_chat_sse_bytes(COMMANDCODE_MODEL))

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CommandCodeProvider(
        ProviderConfig(api_key="cc-test", base_url="http://127.0.0.1:18563/v1"),
        rate_limiter=passthrough_rate_limiter(),
    )
    provider._openai._client._client = mock_http
    if provider._openai._responses is not None:
        provider._openai._responses._client = mock_http
    try:
        upstream_req = MessagesRequest(
            model=COMMANDCODE_MODEL,
            max_tokens=32,
            messages=[Message(role="user", content="hello")],
        )
        chunks_chat = [c async for c in provider.stream_response(upstream_req)]
        assert chunks_chat
        assert seen_paths[-1].endswith("/chat/completions")

        # Flip override to "responses" (Q8: claude_agent_sdk natively speaks
        # Messages, yet MCC translates to CommandCode /responses when asked).
        save_model_overrides(
            ModelParameterOverrides.from_document(
                {
                    HARNESS_SURFACES_KEY: {
                        "claude_agent_sdk": {"commandcode": "responses"}
                    }
                }
            )
        )
        chunks_resp = [c async for c in provider.stream_response(upstream_req)]
        assert chunks_resp
        assert seen_paths[-1].endswith("/responses")
        assert (
            current_model_overrides().harness_surface("claude_agent_sdk", "commandcode")
            == "responses"
        )
    finally:
        await mock_http.aclose()
