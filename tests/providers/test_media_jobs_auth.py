"""Tests for PinnedMediaClient surface.auth_header & dialect support (8.61.0)."""

import asyncio
import json

import httpx

from my_claude_code.application.media import resolve_media_endpoint_ladder
from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaRail,
    MediaRequest,
)
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.application.routing import ResolvedModel
from my_claude_code.config.media_overrides import (
    MediaOperationOverride,
    MediaOverridesDocument,
)
from my_claude_code.config.media_surfaces import (
    GEMINI_API_KEY_HEADER,
    MEDIA_ENCODING_JSON,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_VIDEO_CONTENT,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_OPERATION_VIDEO_RETRIEVE,
    MEDIA_SHAPE_OPENAI_IMAGES,
    MediaSurface,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.media.jobs import PinnedMediaClient
from my_claude_code.providers.media.leaf import MediaLeaf, surfaces_serve
from my_claude_code.providers.rate_limit import ProviderRateLimiter


def _config(api_key: str = "secret-key") -> ProviderConfig:
    return ProviderConfig(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )


def _limiter() -> ProviderRateLimiter:
    return ProviderRateLimiter(
        rate_limit=100,
        rate_window=60.0,
        max_concurrency=4,
        max_retries=0,
    )


def test_pinned_client_sends_custom_auth_header_on_call_and_download() -> None:
    seen: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), dict(request.headers)))
        if str(request.url).endswith(":download"):
            return httpx.Response(
                200,
                content=b"MP4DATA",
                headers={"content-type": "video/mp4"},
            )
        return httpx.Response(200, json={"done": True, "name": "operations/op-1"})

    surfaces = (
        MediaSurface(
            operation=MEDIA_OPERATION_VIDEO_CREATE,
            shape="gemini_veo_lro",
            path="models/{model}:predictLongRunning",
            auth_header=GEMINI_API_KEY_HEADER,
        ),
        MediaSurface(
            operation=MEDIA_OPERATION_VIDEO_RETRIEVE,
            shape="gemini_veo_lro",
            path="operations/{id}",
            auth_header=GEMINI_API_KEY_HEADER,
        ),
        MediaSurface(
            operation=MEDIA_OPERATION_VIDEO_CONTENT,
            shape="gemini_veo_lro",
            path="files/{id}:download",
            auth_header=GEMINI_API_KEY_HEADER,
        ),
    )
    leaf = MediaLeaf(
        provider_id="gemini",
        config=_config(),
        surfaces=surfaces,
        rate_limiter=_limiter(),
        transport=httpx.MockTransport(handler),
    )
    client = PinnedMediaClient(
        leaf, key_index=0, state=None, health=RouteHealthRegistry()
    )

    async def run() -> None:
        resp = await client.call(
            MEDIA_OPERATION_VIDEO_RETRIEVE,
            "op-1",
            model="veo-3",
            request_id="req-1",
        )
        assert resp.status_code == 200

        dl_op = await client.download(
            operation=MEDIA_OPERATION_VIDEO_CONTENT,
            upstream_id="file-1",
            url=None,
            model="veo-3",
            request_id="req-2",
        )
        chunks = [chunk async for chunk in dl_op.chunks]
        await dl_op.close()
        assert b"".join(chunks) == b"MP4DATA"

        # Same-host URL download (e.g. LRO response URI) inherits surface auth_header.
        dl_same = await client.download(
            operation=None,
            upstream_id="op-1",
            url="https://generativelanguage.googleapis.com/v1beta/files/file-1:download",
            model="veo-3",
            request_id="req-3",
        )
        _ = [chunk async for chunk in dl_same.chunks]
        await dl_same.close()

        # Off-host CDN URL must carry NO auth header at all.
        dl_cdn = await client.download(
            operation=None,
            upstream_id="op-1",
            url="https://cdn.example.invalid/video.mp4",
            model="veo-3",
            request_id="req-4",
        )
        _ = [chunk async for chunk in dl_cdn.chunks]
        await dl_cdn.close()

    asyncio.run(run())

    assert len(seen) == 4
    for idx in (0, 1, 2):
        headers = seen[idx][2]
        assert headers.get("x-goog-api-key") == "secret-key"
        assert "authorization" not in headers
    cdn_headers = seen[3][2]
    assert "x-goog-api-key" not in cdn_headers
    assert "authorization" not in cdn_headers


def test_pinned_client_defaults_to_bearer_and_selects_job_dialect() -> None:
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), dict(request.headers)))
        return httpx.Response(200, json={"id": "job-1", "status": "completed"})

    surfaces = (
        MediaSurface(
            operation=MEDIA_OPERATION_VIDEO_RETRIEVE,
            shape="openai_videos",
            path="videos/{id}",
            auth_header=None,
        ),
        MediaSurface(
            operation=MEDIA_OPERATION_VIDEO_RETRIEVE,
            shape="gemini_veo_lro",
            path="operations/{id}",
            auth_header=GEMINI_API_KEY_HEADER,
        ),
    )
    leaf = MediaLeaf(
        provider_id="gemini",
        config=_config(),
        surfaces=surfaces,
        rate_limiter=_limiter(),
        transport=httpx.MockTransport(handler),
    )
    client = PinnedMediaClient(
        leaf, key_index=0, state=None, health=RouteHealthRegistry()
    )

    async def run() -> None:
        await client.call(
            MEDIA_OPERATION_VIDEO_RETRIEVE,
            "job-1",
            model="m1",
            request_id="req-a",
        )
        await client.call(
            MEDIA_OPERATION_VIDEO_RETRIEVE,
            "op-2",
            model="m2",
            request_id="req-b",
            job_dialect="gemini_veo_lro",
        )

    asyncio.run(run())

    assert seen[0][0].endswith("/videos/job-1")
    assert seen[0][1].get("authorization") == "Bearer secret-key"
    assert "x-goog-api-key" not in seen[0][1]

    assert seen[1][0].endswith("/operations/op-2")
    assert seen[1][1].get("x-goog-api-key") == "secret-key"
    assert "authorization" not in seen[1][1]


def test_surfaces_serve_and_leaf_endpoint_ladder_hook() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"b64_json": "AAAA"}]})

    surfaces = (
        MediaSurface(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            shape=MEDIA_SHAPE_OPENAI_IMAGES,
            path="images/generations",
            encoding=MEDIA_ENCODING_JSON,
            surface_id="default_images",
        ),
        MediaSurface(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            shape=MEDIA_SHAPE_OPENAI_IMAGES,
            path="alt/images/generations",
            stream=True,
            surface_id="alt_images",
        ),
    )
    assert surfaces_serve(
        surfaces,
        MediaRequest(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            rail=MediaRail.IMAGE,
            model="prov/m1",
            stream=True,
        ),
    )

    overrides = MediaOverridesDocument(
        models={
            "prov/m1": {
                MEDIA_OPERATION_IMAGE_GENERATE: MediaOperationOverride(
                    preferred_surface="alt_images"
                )
            }
        }
    )
    leaf = MediaLeaf(
        provider_id="prov",
        config=ProviderConfig(api_key="k", base_url="https://api.example.invalid/v1"),
        surfaces=surfaces,
        rate_limiter=_limiter(),
        transport=httpx.MockTransport(handler),
        endpoint_ladder=lambda ref, op, pool: resolve_media_endpoint_ladder(
            ref, op, surfaces=pool, overrides=overrides
        ),
    )

    attempt = MediaAttempt(
        request=MediaRequest(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            rail=MediaRail.IMAGE,
            model="prov/m1",
            body={"prompt": "a cat"},
        ),
        resolved=ResolvedModel(
            original_model="prov/m1",
            provider_id="prov",
            provider_model="m1",
            provider_model_ref="prov/m1",
            reasoning_preference=ReasoningPreference.INHERIT,
        ),
    )

    async def run() -> None:
        chunks = [chunk async for chunk in leaf.execute(attempt)]
        assert len(chunks) == 1

    asyncio.run(run())
    assert seen_urls == ["https://api.example.invalid/v1/alt/images/generations"]
    assert json.loads(json.dumps({"ok": True})) == {"ok": True}
