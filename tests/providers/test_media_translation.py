"""Tests for off-loop multipart/JSON upload conversion and exact param bijection (8.62.0)."""

import asyncio
import base64
import json
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any

import httpx
import pytest

from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaRail,
    MediaRequest,
    MediaResponse,
    MediaUpload,
)
from my_claude_code.application.routing import ResolvedModel
from my_claude_code.config.media_surfaces import (
    MEDIA_ENCODING_JSON,
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_SHAPE_OPENAI_IMAGES,
    MediaSurface,
    image_edit_surface,
    video_surfaces,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.media import adapters as adapters_module
from my_claude_code.providers.media.leaf import MediaLeaf, surfaces_serve
from my_claude_code.providers.media.translation import (
    ASPECT_TO_DEFAULT_SIZE,
    EXACT_SIZE_TO_ASPECT,
    EXACT_SIZE_TO_RESOLUTION,
    data_uris_to_uploads,
    translate_size_and_aspect,
    upload_to_data_uri,
    uploads_to_data_uris,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import (
    FACT_MEDIA_ENCODING_REFUSED,
    learned_fact_store,
    reset_learned_fact_store,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x05" * 40


def _limiter() -> ProviderRateLimiter:
    return ProviderRateLimiter(
        rate_limit=100,
        rate_window=1,
        max_concurrency=4,
        max_retries=0,
        backoff_base_seconds=0.0,
        backoff_max_seconds=0.0,
        backoff_jitter_seconds=0.0,
    )


def _upload(field: str = "image", data: bytes = PNG) -> MediaUpload:
    import hashlib
    import io

    return MediaUpload(
        field=field,
        filename=f"{field}.png",
        content_type="image/png",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        file=io.BytesIO(data),
    )


def _attempt(
    operation: str,
    body: dict[str, Any],
    *,
    uploads: tuple[MediaUpload, ...] = (),
) -> MediaAttempt:
    rail = (
        MediaRail.VIDEO
        if operation == MEDIA_OPERATION_VIDEO_CREATE
        else MediaRail.IMAGE
    )
    return MediaAttempt(
        request=MediaRequest(
            operation=operation,
            rail=rail,
            model="prov/m1",
            body=body,
            uploads=uploads,
        ),
        resolved=ResolvedModel(
            original_model="prov/m1",
            provider_id="prov",
            provider_model="m1",
            provider_model_ref="prov/m1",
            reasoning_preference=ReasoningPreference.INHERIT,
        ),
    )


@pytest.mark.parametrize(
    ("size", "expected_aspect", "expected_res"),
    [
        ("1024x1024", "1:1", None),
        ("1792x1024", "16:9", None),
        ("1280x720", "16:9", "720p"),
        ("1920x1080", "16:9", "1080p"),
        ("1024x1792", "9:16", None),
        ("720x1280", "9:16", "720p"),
        ("1536x1024", "3:2", None),
        ("1024x1536", "2:3", None),
    ],
)
def test_exact_size_to_aspect_and_resolution_bijection(
    size: str, expected_aspect: str, expected_res: str | None
) -> None:
    assert EXACT_SIZE_TO_ASPECT[size] == expected_aspect
    assert EXACT_SIZE_TO_RESOLUTION.get(size) == expected_res
    translated, adaptations, not_forwarded = translate_size_and_aspect(
        {"prompt": "p", "size": size},
        target_fields=("aspect_ratio", "resolution"),
        approximate=False,
    )
    assert translated["aspect_ratio"] == expected_aspect
    assert "size" not in translated
    if expected_res is not None:
        assert translated["resolution"] == expected_res
    assert not_forwarded == ()
    assert adaptations


def test_reverse_aspect_to_size_bijection() -> None:
    assert ASPECT_TO_DEFAULT_SIZE["3:2"] == "1536x1024"
    translated, adaptations, not_forwarded = translate_size_and_aspect(
        {"prompt": "p", "aspect_ratio": "16:9", "resolution": "720p"},
        target_fields=("size",),
        approximate=False,
    )
    assert translated["size"] == "1280x720"
    assert "aspect_ratio" not in translated
    assert "resolution" not in translated
    assert not_forwarded == ()
    assert adaptations


def test_arbitrary_size_is_not_guessed_unless_approximate_enabled() -> None:
    strict_body, strict_adapt, strict_nf = translate_size_and_aspect(
        {"prompt": "p", "size": "1000x800"},
        target_fields=("aspect_ratio",),
        approximate=False,
    )
    assert "aspect_ratio" not in strict_body
    assert "size" not in strict_body
    assert strict_adapt == ()
    assert strict_nf == ("size",)

    approx_body, approx_adapt, approx_nf = translate_size_and_aspect(
        {"prompt": "p", "size": "1000x800"},
        target_fields=("aspect_ratio",),
        approximate=True,
    )
    assert approx_body["aspect_ratio"] in {"1:1", "3:2"}
    assert approx_nf == ()
    assert approx_adapt[0].startswith("approx:size(1000x800)->aspect_ratio(")


def test_surfaces_serve_allows_upload_translatable_ops_on_json_surface() -> None:
    json_edit = MediaSurface(
        operation=MEDIA_OPERATION_IMAGE_EDIT,
        shape=MEDIA_SHAPE_OPENAI_IMAGES,
        path="images/edits",
        encoding=MEDIA_ENCODING_JSON,
    )
    json_gen = MediaSurface(
        operation=MEDIA_OPERATION_IMAGE_GENERATE,
        shape=MEDIA_SHAPE_OPENAI_IMAGES,
        path="images/generations",
        encoding=MEDIA_ENCODING_JSON,
    )
    assert surfaces_serve(
        (json_edit,),
        MediaRequest(
            operation=MEDIA_OPERATION_IMAGE_EDIT,
            rail=MediaRail.IMAGE,
            model="prov/m1",
            uploads=(_upload(),),
        ),
    )
    assert not surfaces_serve(
        (json_gen,),
        MediaRequest(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            rail=MediaRail.IMAGE,
            model="prov/m1",
            uploads=(_upload(),),
        ),
    )


def test_multipart_to_json_runs_off_loop_and_resets_seek(monkeypatch) -> None:
    off_loop_calls: list[str] = []

    def sentinel(uploads):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            off_loop_calls.append("uploads_to_data_uris")
            return uploads_to_data_uris(uploads)
        raise AssertionError("uploads_to_data_uris ran on the event loop thread")

    monkeypatch.setattr(adapters_module, "uploads_to_data_uris", sentinel)
    upload = _upload()
    surface = MediaSurface(
        operation=MEDIA_OPERATION_IMAGE_EDIT,
        shape=MEDIA_SHAPE_OPENAI_IMAGES,
        path="images/edits",
        encoding=MEDIA_ENCODING_JSON,
    )
    wire = asyncio.run(
        adapters_module.build_wire_body(
            surface,
            _attempt(MEDIA_OPERATION_IMAGE_EDIT, {"prompt": "edit"}, uploads=(upload,)),
        )
    )
    assert off_loop_calls == ["uploads_to_data_uris"]
    assert wire.encoded is not None
    payload = json.loads(wire.encoded)
    assert payload["images"] == [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{base64.b64encode(PNG).decode()}"
            },
        }
    ]
    assert "encoding:multipart->json" in wire.adaptations
    assert upload.file.tell() == 0


def test_video_upload_to_json_runs_off_loop(monkeypatch) -> None:
    off_loop_calls: list[str] = []

    def sentinel(upload):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            off_loop_calls.append("upload_to_data_uri")
            return upload_to_data_uri(upload)
        raise AssertionError("upload_to_data_uri ran on the event loop thread")

    monkeypatch.setattr(adapters_module, "upload_to_data_uri", sentinel)
    surface = video_surfaces(encoding=MEDIA_ENCODING_JSON)[0]
    upload = _upload("input_reference")
    wire = asyncio.run(
        adapters_module.build_wire_body(
            surface,
            _attempt(
                MEDIA_OPERATION_VIDEO_CREATE,
                {"prompt": "animate", "seconds": "4"},
                uploads=(upload,),
            ),
        )
    )
    assert off_loop_calls == ["upload_to_data_uri"]
    assert wire.encoded is not None
    payload = json.loads(wire.encoded)
    assert payload["input_reference"].startswith("data:image/png;base64,")
    assert payload["seconds"] == 4
    assert "encoding:multipart->json" in wire.adaptations


def test_json_data_uri_to_multipart_runs_off_loop(monkeypatch) -> None:
    off_loop_calls: list[str] = []

    def sentinel(images):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            off_loop_calls.append("data_uris_to_uploads")
            return data_uris_to_uploads(images)
        raise AssertionError("data_uris_to_uploads ran on the event loop thread")

    monkeypatch.setattr(adapters_module, "data_uris_to_uploads", sentinel)
    surface = MediaSurface(
        operation=MEDIA_OPERATION_IMAGE_EDIT,
        shape=MEDIA_SHAPE_OPENAI_IMAGES,
        path="images/edits",
        encoding="multipart",
    )
    b64 = base64.b64encode(PNG).decode()
    wire = asyncio.run(
        adapters_module.build_wire_body(
            surface,
            _attempt(
                MEDIA_OPERATION_IMAGE_EDIT,
                {
                    "prompt": "edit",
                    "images": [{"image_url": {"url": f"data:image/png;base64,{b64}"}}],
                },
            ),
        )
    )
    assert off_loop_calls == ["data_uris_to_uploads"]
    assert wire.multipart is not None
    assert len(wire.multipart.uploads) == 1
    assert wire.multipart.uploads[0].size == len(PNG)
    assert "encoding:json->multipart" in wire.adaptations


def test_encoding_415_recovery_switches_multipart_to_json_and_records_fact() -> None:
    reset_learned_fact_store()
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        ctype = request.headers.get("content-type", "")
        seen.append((ctype, body))
        if ctype.startswith("multipart/form-data"):
            return httpx.Response(
                415,
                json={"error": {"message": "multipart/form-data is not supported"}},
            )
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": "AAAA"}]})

    leaf = MediaLeaf(
        provider_id="prov",
        config=ProviderConfig(api_key="k", base_url="https://api.example.invalid/v1"),
        surfaces=(image_edit_surface(),),
        rate_limiter=_limiter(),
        transport=httpx.MockTransport(handler),
    )

    chunk = asyncio.run(
        leaf.execute(
            _attempt(
                MEDIA_OPERATION_IMAGE_EDIT, {"prompt": "edit"}, uploads=(_upload(),)
            )
        ).__anext__()
    )
    assert isinstance(chunk, MediaResponse)
    assert chunk.status_code == 200
    assert "encoding:multipart->json" in chunk.adaptations
    assert len(seen) == 2
    assert seen[0][0].startswith("multipart/form-data")
    assert seen[1][0] == "application/json"
    assert json.loads(seen[1][1])["images"][0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert any(
        f.fact_kind == FACT_MEDIA_ENCODING_REFUSED
        and f.detail == "image_edit:multipart"
        for f in learned_fact_store().all_facts()
    )


def test_encoding_415_recovery_switches_json_to_multipart() -> None:
    reset_learned_fact_store()
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ctype = request.headers.get("content-type", "")
        seen.append((ctype, request.content))
        if ctype == "application/json":
            return httpx.Response(
                415,
                json={"error": {"message": "expected multipart/form-data"}},
            )
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": "AAAA"}]})

    leaf = MediaLeaf(
        provider_id="prov",
        config=ProviderConfig(api_key="k", base_url="https://api.example.invalid/v1"),
        surfaces=(image_edit_surface(),),
        rate_limiter=_limiter(),
        transport=httpx.MockTransport(handler),
    )
    b64 = base64.b64encode(PNG).decode()
    chunk = asyncio.run(
        leaf.execute(
            _attempt(
                MEDIA_OPERATION_IMAGE_EDIT,
                {
                    "prompt": "edit",
                    "images": [{"image_url": {"url": f"data:image/png;base64,{b64}"}}],
                },
            )
        ).__anext__()
    )
    assert isinstance(chunk, MediaResponse)
    assert chunk.status_code == 200
    assert "encoding:json->multipart" in chunk.adaptations
    assert seen[0][0] == "application/json"
    assert seen[1][0].startswith("multipart/form-data")
    head = f"Content-Type: {seen[1][0]}\r\n\r\n".encode()
    msg = BytesParser(policy=HTTP).parsebytes(head + seen[1][1])
    file_parts = [
        p.get_payload(decode=True) for p in msg.iter_parts() if p.get_filename()
    ]
    assert file_parts == [PNG]
