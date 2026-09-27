"""A custom provider can declare the media endpoints it serves (7.67.0).

Ticked on its card, saved through the same create/update routes as its chat
``surfaces``, turned into OpenAI-shaped media surfaces under its own base URL
at registry build time -- and then routed to by a media rail like any
built-in provider. Nothing ticked is today's custom provider, byte for byte.
The upstream is a MockTransport; every key and URL is fake.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient

from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_OPERATION_VIDEO_RETRIEVE,
)
from my_claude_code.config.provider_registry import (
    ProviderRegistry,
    get_provider_registry,
)
from my_claude_code.config.settings import Settings
from my_claude_code.providers.media.leaf import MediaLeaf
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app, runtime_for_app

KEY = "sk-speaker-" + "a" * 24


def _admin_client(monkeypatch, tmp_path: Path) -> TestClient:
    """The real custom-provider routes over the (test-isolated) registry.

    Discovery and the dialect probe are doubled: they would otherwise ask the
    fake host for its model list.
    """

    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    app = create_test_app()
    runtime = runtime_for_app(app)
    monkeypatch.setattr(runtime, "reload_providers", AsyncMock(return_value={}))
    monkeypatch.setattr(runtime, "cached_model_ids", lambda: {})
    monkeypatch.setattr(
        runtime, "probe_custom_provider_dialect", AsyncMock(return_value={})
    )
    return TestClient(app, client=("127.0.0.1", 50000))


def _create(client: TestClient, **extra: Any) -> httpx.Response:
    return client.post(
        "/admin/api/custom-providers",
        json={
            "display_name": "Speaker",
            "base_url": "https://speaker.example/v1",
            "api_key": KEY,
            **extra,
        },
    )


def _stored(registry: ProviderRegistry) -> dict[str, Any]:
    raw = json.loads(registry._storage_path().read_text(encoding="utf-8"))
    (entry,) = raw["providers"]
    return entry


def test_declared_operations_round_trip_into_descriptor_surfaces(monkeypatch, tmp_path):
    client = _admin_client(monkeypatch, tmp_path)

    created = _create(client, media_operations=["video", "speech"])

    assert created.status_code == 200, created.text
    body = created.json()
    # Canonical order, whatever order the boxes were ticked in.
    assert body["media_operations"] == ["speech", "video"]
    assert [entry["value"] for entry in body["available_media_operations"]] == [
        "image_generate",
        "image_edit",
        "speech",
        "transcribe",
        "translate",
        "video",
    ]
    registry = get_provider_registry()
    assert _stored(registry)["media_operations"] == ["speech", "video"]
    descriptor = registry.all_descriptors()["custom_speaker"]
    assert [(s.operation, s.path) for s in descriptor.media_surfaces] == [
        (MEDIA_OPERATION_SPEECH, "audio/speech"),
        (MEDIA_OPERATION_VIDEO_CREATE, "videos"),
        (MEDIA_OPERATION_VIDEO_RETRIEVE, "videos/{id}"),
    ]
    listed = client.get("/admin/api/custom-providers").json()["providers"]
    assert listed[0]["media_operations"] == ["speech", "video"]


def test_an_update_replaces_the_list_and_absent_leaves_it(monkeypatch, tmp_path):
    client = _admin_client(monkeypatch, tmp_path)
    _create(client, media_operations=["speech"])

    patched = client.patch(
        "/admin/api/custom-providers/custom_speaker",
        json={"media_operations": ["transcribe", "translate"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["media_operations"] == ["transcribe", "translate"]

    renamed = client.patch(
        "/admin/api/custom-providers/custom_speaker",
        json={"display_name": "Speaker"},
    )
    assert renamed.json()["media_operations"] == ["transcribe", "translate"]

    cleared = client.patch(
        "/admin/api/custom-providers/custom_speaker",
        json={"media_operations": []},
    )
    assert cleared.json()["media_operations"] == []
    registry = get_provider_registry()
    assert "media_operations" not in _stored(registry)
    assert registry.all_descriptors()["custom_speaker"].media_surfaces == ()


def test_an_unknown_operation_is_refused_with_its_name(monkeypatch, tmp_path):
    client = _admin_client(monkeypatch, tmp_path)

    refused = _create(client, media_operations=["speech", "hologram"])

    assert refused.status_code == 422
    assert "hologram" in refused.json()["detail"]
    assert get_provider_registry().get("custom_speaker") is None

    _create(client)
    patched = client.patch(
        "/admin/api/custom-providers/custom_speaker",
        json={"media_operations": ["video_create"]},
    )
    assert patched.status_code == 422
    assert "video_create" in patched.json()["detail"]
    entry = get_provider_registry().get("custom_speaker")
    assert entry is not None
    assert entry.media_operations == ()


def test_nothing_ticked_is_todays_custom_provider(monkeypatch, tmp_path):
    client = _admin_client(monkeypatch, tmp_path)

    created = _create(client)

    assert created.json()["media_operations"] == []
    registry = get_provider_registry()
    stored = _stored(registry)
    # The file carries no new key at all, so it is what earlier builds wrote.
    assert "media_operations" not in stored
    assert registry.all_descriptors()["custom_speaker"].media_surfaces == ()


def test_a_file_from_a_newer_build_keeps_what_this_one_knows(tmp_path):
    path = tmp_path / "custom_providers.json"
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider_id": "custom_future",
                        "display_name": "Future",
                        "base_url": "https://future.example/v1",
                        "api_keys": [KEY],
                        "media_operations": ["hologram", "speech"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    (entry,) = ProviderRegistry(path).list_custom()

    assert entry.media_operations == ("speech",)


def test_a_speech_request_is_routed_to_the_custom_provider(monkeypatch, tmp_path):
    client = _admin_client(monkeypatch, tmp_path)
    assert _create(client, media_operations=["speech"]).status_code == 200
    settings = Settings.model_validate(
        {"MODEL_TTS": "custom_speaker/tts-1", "PROVIDER_RETRY_ATTEMPTS": "1"}
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=b"ID3" + b"\x11" * 64, headers={"content-type": "audio/mpeg"}
        )

    media = MediaRegistry(transport=httpx.MockTransport(handler))
    with TestClient(create_test_app(settings, media=media)) as proxy:
        response = proxy.post(
            "/v1/audio/speech",
            json={"input": "Hello", "voice": "alloy"},
        )

    assert response.status_code == 200, response.text
    (sent,) = seen
    assert (sent.url.host, sent.url.path) == ("speaker.example", "/v1/audio/speech")
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(sent.content) == {
        "model": "tts-1",
        "input": "Hello",
        "voice": "alloy",
    }


def test_the_media_stack_is_rebuilt_when_the_declaration_changes(monkeypatch, tmp_path):
    """The stack's config is the same before and after; its surfaces are not."""

    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    registry = get_provider_registry()
    registry.add(
        display_name="Speaker",
        base_url="https://speaker.example/v1",
        api_keys=(KEY,),
    )
    settings = Settings.model_validate({})
    media = MediaRegistry()

    before = media.resolve("custom_speaker", settings)
    assert isinstance(before, MediaLeaf)
    assert before.surfaces == ()
    assert media.resolve("custom_speaker", settings) is before

    registry.update("custom_speaker", media_operations=["speech"])
    after = media.resolve("custom_speaker", settings)

    assert after is not before
    assert isinstance(after, MediaLeaf)
    assert [surface.operation for surface in after.surfaces] == ["speech"]
