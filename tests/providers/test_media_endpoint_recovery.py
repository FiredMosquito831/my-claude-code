"""Tests for multi-surface wrong-endpoint recovery and learned endpoint facts (8.62.0)."""

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.media.endpoint_ladder import (
    FACT_MEDIA_ENDPOINT_REFUSED,
    FACT_MEDIA_ENDPOINT_SURFACE,
    resolve_media_endpoint_ladder,
)
from my_claude_code.application.media.executor import (
    MediaExecutor,
    reset_media_route_health_registries,
)
from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaPlan,
    MediaRail,
    MediaRequest,
    MediaResponse,
)
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.application.routing import ResolvedModel
from my_claude_code.config.media_overrides import (
    MediaOperationOverride,
    MediaOverridesDocument,
)
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_SHAPE_OPENAI_IMAGES,
    MediaSurface,
)
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.media.leaf import MediaLeaf
from my_claude_code.providers.media.registry import MediaRegistry
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import (
    learned_fact_store,
    reset_learned_fact_store,
)
from tests.api.support import create_test_app, requests_as_read


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


def _surfaces() -> tuple[MediaSurface, MediaSurface]:
    primary = MediaSurface(
        operation=MEDIA_OPERATION_IMAGE_GENERATE,
        shape=MEDIA_SHAPE_OPENAI_IMAGES,
        path="images/generations",
        surface_id="primary_images",
    )
    secondary = MediaSurface(
        operation=MEDIA_OPERATION_IMAGE_GENERATE,
        shape=MEDIA_SHAPE_OPENAI_IMAGES,
        path="alt/images/generations",
        surface_id="alt_images",
    )
    return primary, secondary


def _attempt(model: str = "m1") -> MediaAttempt:
    ref = f"prov/{model}"
    return MediaAttempt(
        request=MediaRequest(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            rail=MediaRail.IMAGE,
            model=ref,
            body={"prompt": "sunset"},
        ),
        resolved=ResolvedModel(
            original_model=ref,
            provider_id="prov",
            provider_model=model,
            provider_model_ref=ref,
            reasoning_preference=ReasoningPreference.INHERIT,
        ),
    )


def test_wrong_endpoint_404_recovers_intra_leaf_without_charging_health() -> None:
    reset_learned_fact_store()
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/v1/images/generations"):
            return httpx.Response(404, json={"error": {"message": "Not found"}})
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": "AAAA"}]})

    surfaces = _surfaces()
    leaf = MediaLeaf(
        provider_id="prov",
        config=ProviderConfig(api_key="k", base_url="https://api.example.invalid/v1"),
        surfaces=surfaces,
        rate_limiter=_limiter(),
        transport=httpx.MockTransport(handler),
        endpoint_ladder=lambda ref, op, pool: resolve_media_endpoint_ladder(
            ref,
            op,
            surfaces=pool,
            learned_facts=learned_fact_store().all_facts(),
        ),
    )
    health = RouteHealthRegistry()
    executor = MediaExecutor(lambda _pid: leaf, health=health)

    async def run() -> MediaResponse:
        stream = executor.execute(
            MediaPlan(attempts=(_attempt("m1"),)),
            request_id="req-1",
        )
        chunks = [c async for c in stream]
        assert len(chunks) == 1
        assert isinstance(chunks[0], MediaResponse)
        return chunks[0]

    answer = asyncio.run(run())
    assert answer.status_code == 200
    assert answer.media_surface == "alt_images"
    assert answer.media_surface_source == "recovery"
    assert "endpoint:primary_images->alt_images" in answer.adaptations
    assert seen_paths == ["/v1/images/generations", "/v1/alt/images/generations"]
    assert health.why("prov/m1") is None

    facts = learned_fact_store().all_facts()
    assert any(
        f.fact_kind == FACT_MEDIA_ENDPOINT_SURFACE
        and f.detail == "image_generate"
        and f.value == ["alt_images"]
        for f in facts
    )
    assert any(
        f.fact_kind == FACT_MEDIA_ENDPOINT_REFUSED
        and f.detail == "image_generate:primary_images"
        for f in facts
    )

    # Next request goes straight to alt_images via learned endpoint ladder.
    seen_paths.clear()
    second = asyncio.run(run())
    assert second.media_surface == "alt_images"
    assert second.media_surface_source == "learned"
    assert seen_paths == ["/v1/alt/images/generations"]


def test_wrong_endpoint_400_naming_generate_content_falls_back_to_next_surface() -> (
    None
):
    reset_learned_fact_store()
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/v1/images/generations"):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Use generateContent for this model; not supported on /images/generations"
                    }
                },
            )
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": "AAAA"}]})

    surfaces = _surfaces()
    leaf = MediaLeaf(
        provider_id="prov",
        config=ProviderConfig(api_key="k", base_url="https://api.example.invalid/v1"),
        surfaces=surfaces,
        rate_limiter=_limiter(),
        transport=httpx.MockTransport(handler),
        endpoint_ladder=lambda ref, op, pool: resolve_media_endpoint_ladder(
            ref, op, surfaces=pool
        ),
    )

    chunk = asyncio.run(leaf.execute(_attempt("m1")).__anext__())
    assert isinstance(chunk, MediaResponse)
    assert chunk.status_code == 200
    assert seen_paths == ["/v1/images/generations", "/v1/alt/images/generations"]


def test_pinned_surface_does_not_fall_back_on_404() -> None:
    reset_learned_fact_store()
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(404, json={"error": {"message": "Not found"}})

    surfaces = _surfaces()
    overrides = MediaOverridesDocument(
        models={
            "prov/m1": {
                MEDIA_OPERATION_IMAGE_GENERATE: MediaOperationOverride(
                    surface="primary_images"
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
    with pytest.raises(ExecutionFailure):
        asyncio.run(leaf.execute(_attempt("m1")).__anext__())
    assert seen_paths == ["/v1/images/generations"]


def test_all_surfaces_failing_charges_route_health_once() -> None:
    reset_learned_fact_store()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "Not found"}})

    leaf = MediaLeaf(
        provider_id="prov",
        config=ProviderConfig(api_key="k", base_url="https://api.example.invalid/v1"),
        surfaces=_surfaces(),
        rate_limiter=_limiter(),
        transport=httpx.MockTransport(handler),
        endpoint_ladder=lambda ref, op, pool: resolve_media_endpoint_ladder(
            ref, op, surfaces=pool
        ),
    )
    health = RouteHealthRegistry(
        bench_enabled=True, mode="consecutive", eject_after_failures=1
    )
    executor = MediaExecutor(lambda _pid: leaf, health=health)

    async def run() -> None:
        stream = executor.execute(
            MediaPlan(attempts=(_attempt("m1"),)), request_id="req-fail"
        )
        await anext(stream)

    with pytest.raises(ExecutionFailure):
        asyncio.run(run())
    assert health.why("prov/m1") is not None


def test_media_capture_records_surface_and_adaptations(
    monkeypatch, tmp_path: Path
) -> None:
    reset_learned_fact_store()
    reset_media_route_health_registries()
    request_log.reset_request_log_stores()
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    registry = get_provider_registry()
    provider_id = (
        "custom_recprov"
        if "custom_recprov" in registry.all_descriptors()
        else registry.add(
            display_name="RecProv",
            base_url="https://rec.example/v1",
            api_keys=("sk-rec-" + "a" * 30,),
            media_operations=("image_generate",),
        ).provider_id
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "response_format" in payload:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "param": "response_format",
                        "message": "Unsupported parameter: 'response_format'",
                    }
                },
            )
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": "AAAA"}]})

    settings = Settings.model_validate(
        {
            "MODEL_IMAGE": f"{provider_id}/img-1",
            "PROVIDER_RETRY_ATTEMPTS": "1",
        }
    )
    media = MediaRegistry(transport=httpx.MockTransport(handler))
    with TestClient(create_test_app(settings, media=media)) as client:
        resp = client.post(
            "/v1/images/generations",
            json={"prompt": "cat", "response_format": "b64_json"},
        )
    assert resp.status_code == 200
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM requests").fetchall()
        (row,) = requests_as_read(conn, rows)
    finally:
        conn.close()
    params = json.loads(row["params"])
    assert params["media"]["surface"] == "openai_images"
    assert params["media"]["surface_source"] == "provider_default"
    assert "stripped:response_format(b64_json)" in params["media"]["adapted"]
