"""Tests for PR-M4: OpenRouter chat image, Gemini native image/Imagen/Veo LRO,
Rung 4 capability promotion, and ``/v1/images/variations`` + ``/v1/videos/edits``.
"""

import asyncio
import base64
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.media.capabilities import (
    MediaCapabilityInputs,
    resolve_media_capability_profile,
)
from my_claude_code.application.media.endpoint_ladder import (
    resolve_media_endpoint_ladder,
)
from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaRail,
    MediaRequest,
)
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.application.routing import ResolvedModel
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_IMAGE_VARIATION,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_OPERATION_VIDEO_EDIT,
    MEDIA_SHAPE_GEMINI_GENERATE_CONTENT_IMAGE,
    MEDIA_SHAPE_GEMINI_PREDICT_IMAGE,
    MEDIA_SHAPE_GEMINI_VEO_LRO,
    MEDIA_SHAPE_OPENAI_IMAGES,
    MEDIA_SHAPE_OPENAI_VIDEOS,
    MEDIA_SHAPE_OPENROUTER_CHAT_IMAGE,
    effective_surfaces_for,
    job_path,
)
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.core.gemini_native_media import (
    generate_content_image_answer,
    image_generate_request,
    image_predict_request,
    predict_image_answer,
    veo_lro_request,
)
from my_claude_code.core.openai_videos import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    parse_job,
)
from my_claude_code.core.openrouter_media import (
    OpenRouterAnswerError,
    openrouter_chat_image_request,
    parse_openrouter_image_answer,
)
from my_claude_code.providers.media.adapters import build_wire_body
from my_claude_code.providers.media.registry import MediaRegistry
from my_claude_code.providers.model_listing import (
    cached_provider_model_info,
    reset_declared_endpoints_snapshot,
    set_declared_endpoints_snapshot,
)
from tests.api.support import create_test_app

OPENROUTER_KEY = "sk-or-v1-" + "ab12" * 12
OPENAI_KEY = "sk-test-" + "cd34" * 12
GEMINI_KEY = "AIza" + "ef56" * 9

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
PNG_B64 = base64.b64encode(PNG).decode()
PNG_DATA_URI = f"data:image/png;base64,{PNG_B64}"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 24


def _make_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **extra: str
) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "CHATGPT_OAUTH_ACCESS_TOKEN": OPENAI_KEY,
        "OPENROUTER_API_KEY": OPENROUTER_KEY,
        "GEMINI_API_KEY": GEMINI_KEY,
        "PROVIDER_RETRY_ATTEMPTS": "1",
    }
    base.update(extra)
    return Settings.model_validate(base)


def _media_job_rows(tmp_path: Path) -> list[dict[str, Any]]:
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM media_jobs ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


class _RecordingUpstream:
    """Deterministic httpx MockTransport script recording all requests."""

    def __init__(self, routes: dict[tuple[str, str], list[httpx.Response]]) -> None:
        self.routes = {k: list(v) for k, v in routes.items()}
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        key = (request.method, request.url.path)
        queue = self.routes.get(key)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(
            f"Unexpected upstream call: {request.method} {request.url}"
        )


# ------------------------------------------------------------------ core unit


def test_openrouter_chat_image_builder_and_parser() -> None:
    req = openrouter_chat_image_request(
        "red panda on moss",
        "google/gemini-2.5-flash-image-preview",
        image_data_uris=[PNG_DATA_URI],
        aspect_ratio="16:9",
        image_size="2K",
    )
    assert req["model"] == "google/gemini-2.5-flash-image-preview"
    assert req["modalities"] == ["image", "text"]
    assert req["image_config"] == {"aspect_ratio": "16:9", "image_size": "2K"}
    assert req["messages"][0]["content"][0] == {
        "type": "text",
        "text": "red panda on moss",
    }
    assert req["messages"][0]["content"][1]["image_url"]["url"] == PNG_DATA_URI

    raw = json.dumps(
        {
            "created": 1_700_000_123,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Done!",
                        "images": [
                            {"type": "image_url", "image_url": {"url": PNG_DATA_URI}}
                        ],
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 22,
                "total_tokens": 33,
            },
        }
    ).encode("utf-8")
    parsed = parse_openrouter_image_answer(raw)
    assert parsed.text == "Done!"
    parsed_body = json.loads(parsed.body)
    assert parsed_body["data"] == [{"b64_json": PNG_B64}]
    assert parsed_body["created"] == 1_700_000_123
    assert parsed.usage == {
        "prompt_tokens": 11,
        "completion_tokens": 22,
        "total_tokens": 33,
        "input_tokens": 11,
        "output_tokens": 22,
    }

    with pytest.raises(OpenRouterAnswerError):
        parse_openrouter_image_answer(
            json.dumps({"choices": [{"message": {"content": "no pic"}}]}).encode()
        )


def test_gemini_native_image_predict_and_veo_builders_and_parsers() -> None:
    gen_req = image_generate_request(
        "draw a lighthouse",
        aspect_ratio="3:2",
        image_size="1K",
        input_images=[("image/png", PNG)],
    )
    assert gen_req["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert gen_req["generationConfig"]["imageConfig"] == {
        "aspectRatio": "3:2",
        "imageSize": "1K",
    }
    parts = gen_req["contents"][0]["parts"]
    assert parts[0] == {"text": "draw a lighthouse"}
    assert parts[1]["inlineData"]["mimeType"] == "image/png"
    assert parts[1]["inlineData"]["data"] == PNG_B64

    gen_raw = json.dumps(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Lighthouse sketch"},
                            {"inlineData": {"mimeType": "image/png", "data": PNG_B64}},
                        ]
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 7,
                "candidatesTokenCount": 9,
                "totalTokenCount": 16,
            },
        }
    ).encode("utf-8")
    gen_ans = generate_content_image_answer(gen_raw, created=1_700_000_001)
    assert json.loads(gen_ans.body)["data"] == [{"b64_json": PNG_B64}]
    assert gen_ans.usage == {
        "type": "tokens",
        "input_tokens": 7,
        "output_tokens": 9,
        "total_tokens": 16,
    }

    pred_req = image_predict_request("mountain dawn", n=2, aspect_ratio="16:9")
    assert pred_req == {
        "instances": [{"prompt": "mountain dawn"}],
        "parameters": {"sampleCount": 2, "aspectRatio": "16:9"},
    }
    pred_ans = predict_image_answer(
        json.dumps({"predictions": [{"bytesBase64Encoded": PNG_B64}]}).encode("utf-8"),
        created=1_700_000_002,
    )
    assert json.loads(pred_ans.body)["data"] == [{"b64_json": PNG_B64}]

    veo_req = veo_lro_request(
        "waves rolling",
        params={"aspectRatio": "16:9", "durationSeconds": 8},
        input_image=("image/png", PNG),
    )
    assert veo_req["instances"][0]["prompt"] == "waves rolling"
    assert veo_req["instances"][0]["image"] == {
        "bytesBase64Encoded": PNG_B64,
        "mimeType": "image/png",
    }
    assert veo_req["parameters"] == {"aspectRatio": "16:9", "durationSeconds": 8}

    pending_lro = parse_job(
        json.dumps(
            {"name": "models/veo-3.1-generate-preview/operations/op-123", "done": False}
        ).encode()
    )
    assert pending_lro is not None
    assert pending_lro.id == "models/veo-3.1-generate-preview/operations/op-123"
    assert pending_lro.status == STATUS_IN_PROGRESS

    done_lro = parse_job(
        json.dumps(
            {
                "name": "models/veo-3.1-generate-preview/operations/op-123",
                "done": True,
                "response": {
                    "generateVideoResponse": {
                        "generatedSamples": [
                            {
                                "video": {
                                    "uri": "https://generativelanguage.googleapis.com/v1beta/files/v42:download?alt=media"
                                }
                            }
                        ]
                    }
                },
            }
        ).encode()
    )
    assert done_lro is not None
    assert done_lro.status == STATUS_COMPLETED
    assert (
        done_lro.result_url
        == "https://generativelanguage.googleapis.com/v1beta/files/v42:download?alt=media"
    )


def test_job_path_slash_preservation_and_effective_surfaces_fallback() -> None:
    assert (
        job_path("/v1beta/{id}", "models/veo-3/operations/op-1")
        == "/v1beta/models/veo-3/operations/op-1"
    )
    assert job_path("/v1beta/{id}", "op-bare") == "/v1beta/operations/op-bare"
    assert job_path("files/{id}:download", "files/abc123") == "files/abc123:download"
    assert job_path("videos/{id}", "dir/sub") == "videos/dir%2Fsub"

    provider_reg = get_provider_registry()
    openai_desc = provider_reg.all_descriptors().get("openai")
    or_desc = provider_reg.all_descriptors().get("open_router")
    assert openai_desc is not None and or_desc is not None
    assert (
        effective_surfaces_for(
            openai_desc.media_surfaces, MEDIA_OPERATION_IMAGE_VARIATION
        )[0].path
        == "images/variations"
    )
    or_var = effective_surfaces_for(
        or_desc.media_surfaces, MEDIA_OPERATION_IMAGE_VARIATION
    )
    assert or_var and or_var[0].shape == MEDIA_SHAPE_OPENROUTER_CHAT_IMAGE
    or_vedit = effective_surfaces_for(
        or_desc.media_surfaces, MEDIA_OPERATION_VIDEO_EDIT
    )
    assert or_vedit and or_vedit[0].operation == MEDIA_OPERATION_VIDEO_CREATE


# -------------------------------------------------------- Rung 4 promotion


def test_rung4_capability_promotion_and_default_declaration_order() -> None:
    gemini = get_provider_registry().all_descriptors().get("gemini")
    assert gemini is not None

    # No discovery cache -> preserves legacy OpenAI-compat declaration order
    default_img = resolve_media_endpoint_ladder(
        "gemini/gemini-2.5-flash-image",
        MEDIA_OPERATION_IMAGE_GENERATE,
        surfaces=gemini.media_surfaces,
    )
    assert default_img.winner is not None
    assert default_img.winner.source == "provider_default"
    assert default_img.winner.surface.shape == MEDIA_SHAPE_OPENAI_IMAGES

    default_vid = resolve_media_endpoint_ladder(
        "gemini/veo-3.1-generate-preview",
        MEDIA_OPERATION_VIDEO_CREATE,
        surfaces=gemini.media_surfaces,
    )
    assert default_vid.winner is not None
    assert default_vid.winner.source == "provider_default"
    assert default_vid.winner.surface.shape == MEDIA_SHAPE_OPENAI_VIDEOS

    try:
        set_declared_endpoints_snapshot(
            {
                "gemini": [
                    ProviderModelInfo(
                        model_id="gemini-2.5-flash-image",
                        published_row=json.dumps(
                            {"supportedGenerationMethods": ["generateContent"]}
                        ),
                    ),
                    ProviderModelInfo(
                        model_id="imagen-3.0-generate-002",
                        published_row=json.dumps(
                            {"supportedGenerationMethods": ["predict"]}
                        ),
                    ),
                    ProviderModelInfo(
                        model_id="veo-3.1-generate-preview",
                        published_row=json.dumps(
                            {"supportedGenerationMethods": ["predictLongRunning"]}
                        ),
                    ),
                ]
            }
        )
        gen_ladder = MediaRegistry._resolve_endpoint_ladder(
            "gemini/gemini-2.5-flash-image",
            MEDIA_OPERATION_IMAGE_GENERATE,
            gemini.media_surfaces,
        )
        assert gen_ladder.winner is not None
        assert gen_ladder.winner.source == "provider_list"
        assert (
            gen_ladder.winner.surface.shape == MEDIA_SHAPE_GEMINI_GENERATE_CONTENT_IMAGE
        )

        pred_ladder = MediaRegistry._resolve_endpoint_ladder(
            "gemini/imagen-3.0-generate-002",
            MEDIA_OPERATION_IMAGE_GENERATE,
            gemini.media_surfaces,
        )
        assert pred_ladder.winner is not None
        assert pred_ladder.winner.source == "provider_list"
        assert pred_ladder.winner.surface.shape == MEDIA_SHAPE_GEMINI_PREDICT_IMAGE

        # Imagen :predict rejects uploads (json-only), so edit with uploads must skip it
        info_pred = cached_provider_model_info("gemini", "imagen-3.0-generate-002")
        assert info_pred is not None
        profile_pred = resolve_media_capability_profile(
            "gemini/imagen-3.0-generate-002",
            MediaCapabilityInputs(
                provider_info=info_pred, surfaces=gemini.media_surfaces
            ),
        )
        assert profile_pred is not None
        edit_ladder = resolve_media_endpoint_ladder(
            "gemini/imagen-3.0-generate-002",
            MEDIA_OPERATION_IMAGE_EDIT,
            surfaces=gemini.media_surfaces,
            capability_profile=profile_pred,
        )
        assert all(
            c.surface.shape != MEDIA_SHAPE_GEMINI_PREDICT_IMAGE
            for c in edit_ladder.candidates
        )

        veo_ladder = MediaRegistry._resolve_endpoint_ladder(
            "gemini/veo-3.1-generate-preview",
            MEDIA_OPERATION_VIDEO_CREATE,
            gemini.media_surfaces,
        )
        assert veo_ladder.winner is not None
        assert veo_ladder.winner.source == "provider_list"
        assert veo_ladder.winner.surface.shape == MEDIA_SHAPE_GEMINI_VEO_LRO
    finally:
        reset_declared_endpoints_snapshot()


def test_new_shapes_offload_body_encoding_with_to_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)

    async def run() -> None:
        provider_reg = get_provider_registry()
        or_desc = provider_reg.all_descriptors().get("open_router")
        gem_desc = provider_reg.all_descriptors().get("gemini")
        assert or_desc is not None and gem_desc is not None
        or_surf = next(
            s
            for s in or_desc.media_surfaces
            if s.shape == MEDIA_SHAPE_OPENROUTER_CHAT_IMAGE
        )
        gem_surf = next(
            s
            for s in gem_desc.media_surfaces
            if s.shape == MEDIA_SHAPE_GEMINI_GENERATE_CONTENT_IMAGE
        )
        veo_surf = next(
            s for s in gem_desc.media_surfaces if s.shape == MEDIA_SHAPE_GEMINI_VEO_LRO
        )

        resolved_or = ResolvedModel(
            original_model="open_router/google/gemini-2.5-flash-image-preview",
            provider_id="open_router",
            provider_model="google/gemini-2.5-flash-image-preview",
            provider_model_ref="open_router/google/gemini-2.5-flash-image-preview",
            reasoning_preference=ReasoningPreference.INHERIT,
        )
        resolved_gem = ResolvedModel(
            original_model="gemini/gemini-2.5-flash-image",
            provider_id="gemini",
            provider_model="gemini-2.5-flash-image",
            provider_model_ref="gemini/gemini-2.5-flash-image",
            reasoning_preference=ReasoningPreference.INHERIT,
        )
        req_gen = MediaRequest(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            rail=MediaRail.IMAGE,
            model="open_router/google/gemini-2.5-flash-image-preview",
            body={"prompt": "sunset", "aspect_ratio": "16:9"},
        )
        await build_wire_body(
            or_surf, MediaAttempt(request=req_gen, resolved=resolved_or)
        )
        await build_wire_body(
            gem_surf, MediaAttempt(request=req_gen, resolved=resolved_gem)
        )
        req_vid = MediaRequest(
            operation=MEDIA_OPERATION_VIDEO_CREATE,
            rail=MediaRail.VIDEO,
            model="gemini/veo-3.1-generate-preview",
            body={"prompt": "clouds"},
        )
        await build_wire_body(
            veo_surf, MediaAttempt(request=req_vid, resolved=resolved_gem)
        )

    asyncio.run(run())
    assert calls.count("build_request_body") == 3


# ----------------------------------------------------------- mock E2E FastAPI


def test_openrouter_chat_image_generate_and_edit_e2e(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _make_settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="open_router/google/gemini-2.5-flash-image-preview",
    )
    upstream = _RecordingUpstream(
        {
            ("POST", "/api/v1/chat/completions"): [
                httpx.Response(
                    200,
                    json={
                        "created": 1_700_000_555,
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "Generated.",
                                    "images": [
                                        {
                                            "type": "image_url",
                                            "image_url": {"url": PNG_DATA_URI},
                                        }
                                    ],
                                }
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 20,
                            "total_tokens": 30,
                        },
                    },
                )
            ]
        }
    )
    client = TestClient(
        create_test_app(
            settings,
            media=MediaRegistry(transport=httpx.MockTransport(upstream.handler)),
        )
    )
    with client:
        gen = client.post(
            "/v1/images/generations",
            json={"prompt": "ceramic fox", "size": "1024x1024"},
        )
        assert gen.status_code == 200, gen.text
        assert gen.json()["data"] == [{"b64_json": PNG_B64}]

        edit = client.post(
            "/v1/images/edits",
            data={"prompt": "make it cobalt blue"},
            files={"image": ("fox.png", PNG, "image/png")},
        )
        assert edit.status_code == 200, edit.text
        assert edit.json()["data"] == [{"b64_json": PNG_B64}]

    assert [req.url.path for req in upstream.seen] == [
        "/api/v1/chat/completions",
        "/api/v1/chat/completions",
    ]
    first_body = json.loads(upstream.seen[0].content)
    assert first_body["modalities"] == ["image", "text"]
    assert first_body["image_config"] == {"aspect_ratio": "1:1"}
    second_body = json.loads(upstream.seen[1].content)
    assert second_body["modalities"] == ["image", "text"]
    assert second_body["messages"][0]["content"][1]["image_url"]["url"] == PNG_DATA_URI


def test_gemini_native_image_imagen_and_veo_lro_e2e(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _make_settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="gemini/gemini-2.5-flash-image",
        MODEL_IMAGE_FALLBACKS="gemini/imagen-3.0-generate-002",
        MODEL_VIDEO="gemini/veo-3.1-generate-preview",
    )
    op_name = "models/veo-3.1-generate-preview/operations/op-veo-42"
    download_url = "https://generativelanguage.googleapis.com/v1beta/files/veo-clip-42:download?alt=media"
    upstream = _RecordingUpstream(
        {
            ("POST", "/v1beta/models/gemini-2.5-flash-image:generateContent"): [
                httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "inlineData": {
                                                "mimeType": "image/png",
                                                "data": PNG_B64,
                                            }
                                        }
                                    ]
                                }
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 5,
                            "candidatesTokenCount": 8,
                            "totalTokenCount": 13,
                        },
                    },
                )
            ],
            ("POST", "/v1beta/models/imagen-3.0-generate-002:predict"): [
                httpx.Response(
                    200,
                    json={"predictions": [{"bytesBase64Encoded": PNG_B64}]},
                )
            ],
            ("POST", "/v1beta/models/veo-3.1-generate-preview:predictLongRunning"): [
                httpx.Response(200, json={"name": op_name, "done": False})
            ],
            ("GET", f"/v1beta/{op_name}"): [
                httpx.Response(
                    200,
                    json={
                        "name": op_name,
                        "done": True,
                        "response": {
                            "generateVideoResponse": {
                                "generatedSamples": [{"video": {"uri": download_url}}]
                            }
                        },
                    },
                )
            ],
            ("GET", "/v1beta/files/veo-clip-42:download"): [
                httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
            ],
        }
    )
    try:
        set_declared_endpoints_snapshot(
            {
                "gemini": [
                    ProviderModelInfo(
                        model_id="gemini-2.5-flash-image",
                        published_row=json.dumps(
                            {"supportedGenerationMethods": ["generateContent"]}
                        ),
                    ),
                    ProviderModelInfo(
                        model_id="imagen-3.0-generate-002",
                        published_row=json.dumps(
                            {"supportedGenerationMethods": ["predict"]}
                        ),
                    ),
                    ProviderModelInfo(
                        model_id="veo-3.1-generate-preview",
                        published_row=json.dumps(
                            {"supportedGenerationMethods": ["predictLongRunning"]}
                        ),
                    ),
                ]
            }
        )
        client = TestClient(
            create_test_app(
                settings,
                media=MediaRegistry(transport=httpx.MockTransport(upstream.handler)),
            )
        )
        with client:
            img = client.post(
                "/v1/images/generations", json={"prompt": "lantern in snow"}
            )
            assert img.status_code == 200, img.text
            assert img.json()["data"] == [{"b64_json": PNG_B64}]

            imagen = client.post(
                "/v1/images/generations",
                json={
                    "model": "gemini/imagen-3.0-generate-002",
                    "prompt": "alpine valley",
                },
            )
            assert imagen.status_code == 200, imagen.text
            assert imagen.json()["data"] == [{"b64_json": PNG_B64}]

            vid = client.post(
                "/v1/videos",
                json={"prompt": "timelapse northern lights", "seconds": "8"},
            )
            assert vid.status_code == 200, vid.text
            vid_id = vid.json()["id"]

            polled = client.get(f"/v1/videos/{vid_id}")
            assert polled.status_code == 200, polled.text
            assert polled.json()["status"] == "completed"

            content = client.get(f"/v1/videos/{vid_id}/content")
            assert content.status_code == 200, content.text
            assert content.content == MP4
    finally:
        reset_declared_endpoints_snapshot()

    rows = _media_job_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["job_dialect"] == MEDIA_SHAPE_GEMINI_VEO_LRO
    assert rows[0]["operation"] == MEDIA_OPERATION_VIDEO_CREATE

    assert [req.url.path for req in upstream.seen] == [
        "/v1beta/models/gemini-2.5-flash-image:generateContent",
        "/v1beta/models/imagen-3.0-generate-002:predict",
        "/v1beta/models/veo-3.1-generate-preview:predictLongRunning",
        f"/v1beta/{op_name}",
        f"/v1beta/{op_name}",
        "/v1beta/files/veo-clip-42:download",
    ]
    for req in upstream.seen:
        assert req.headers.get("x-goog-api-key") == GEMINI_KEY


def test_images_variations_and_videos_edits_endpoints_e2e(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings_openai = _make_settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="openai/dall-e-2",
        MODEL_VIDEO="open_router/openai/sora-2",
    )
    upstream_openai = _RecordingUpstream(
        {
            ("POST", "/backend-api/codex/images/variations"): [
                httpx.Response(
                    200,
                    json={"created": 1_700_000_010, "data": [{"b64_json": PNG_B64}]},
                )
            ],
            ("POST", "/api/v1/videos"): [
                httpx.Response(200, json={"id": "vid_edit_1", "status": "queued"})
            ],
        }
    )
    client_openai = TestClient(
        create_test_app(
            settings_openai,
            media=MediaRegistry(transport=httpx.MockTransport(upstream_openai.handler)),
        )
    )
    with client_openai:
        assert client_openai.head("/v1/images/variations").status_code == 204
        assert client_openai.options("/v1/videos/edits").status_code == 204

        bad_json_var = client_openai.post(
            "/v1/images/variations", json={"prompt": "nope"}
        )
        assert bad_json_var.status_code == 400

        bad_missing_img = client_openai.post("/v1/images/variations", data={"n": "1"})
        assert bad_missing_img.status_code == 400

        ok_var = client_openai.post(
            "/v1/images/variations",
            files={"image": ("src.png", PNG, "image/png")},
            data={"n": "1"},
        )
        assert ok_var.status_code == 200, ok_var.text
        assert ok_var.json()["data"] == [{"b64_json": PNG_B64}]

        ok_vedit = client_openai.post(
            "/v1/videos/edits",
            json={"prompt": "turn sky golden", "seconds": "4"},
        )
        assert ok_vedit.status_code == 200, ok_vedit.text
        assert ok_vedit.json()["status"] == "queued"

    rows = _media_job_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["operation"] == MEDIA_OPERATION_VIDEO_EDIT
    assert [req.url.path for req in upstream_openai.seen] == [
        "/backend-api/codex/images/variations",
        "/api/v1/videos",
    ]


def test_images_variations_fallback_openrouter_e2e(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Variation fallback onto OpenRouter chat image edit when host lacks /variations."""
    settings_or = _make_settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="open_router/google/gemini-2.5-flash-image-preview",
    )
    upstream_or = _RecordingUpstream(
        {
            ("POST", "/api/v1/chat/completions"): [
                httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "images": [{"image_url": {"url": PNG_DATA_URI}}],
                                }
                            }
                        ]
                    },
                )
            ]
        }
    )
    client_or = TestClient(
        create_test_app(
            settings_or,
            media=MediaRegistry(transport=httpx.MockTransport(upstream_or.handler)),
        )
    )
    with client_or:
        fallback_var = client_or.post(
            "/v1/images/variations",
            files={"image": ("src.png", PNG, "image/png")},
        )
        assert fallback_var.status_code == 200, fallback_var.text
        assert fallback_var.json()["data"] == [{"b64_json": PNG_B64}]

    sent = json.loads(upstream_or.seen[0].content)
    assert sent["modalities"] == ["image", "text"]
    assert (
        sent["messages"][0]["content"][0]["text"] == "Create a variation of this image."
    )
