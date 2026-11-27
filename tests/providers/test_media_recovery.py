"""Tests for the intra-attempt media recovery ladder and learned preflight (8.62.0)."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaRail,
    MediaRequest,
    MediaResponse,
)
from my_claude_code.application.routing import ResolvedModel
from my_claude_code.config.media_overrides import (
    MediaOperationOverride,
    MediaOverridesDocument,
)
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_VIDEO_CREATE,
    image_generation_surface,
    video_surfaces,
)
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.media.leaf import MediaLeaf
from my_claude_code.providers.media.param_refusal import (
    note_param_refusal,
    parse_param_enum_refusal,
)
from my_claude_code.providers.media.recovery import (
    CORE_PROTECTED_PARAMS,
    LOSSLESS_STRIPPABLE_PARAMS,
    OPTIONAL_STRIPPABLE_PARAMS,
    apply_media_preflight,
)
from my_claude_code.providers.media.registry import MediaRegistry
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import (
    FACT_MEDIA_PARAM_ENUM,
    FACT_MEDIA_PARAM_REFUSED,
    learned_fact_store,
    reset_learned_fact_store,
)
from tests.api.support import create_test_app


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


def _attempt(
    operation: str,
    body: dict[str, Any],
    *,
    provider_id: str = "prov",
    model_id: str = "gpt-image-1",
) -> MediaAttempt:
    rail = (
        MediaRail.VIDEO
        if operation == MEDIA_OPERATION_VIDEO_CREATE
        else MediaRail.IMAGE
    )
    ref = f"{provider_id}/{model_id}"
    return MediaAttempt(
        request=MediaRequest(operation=operation, rail=rail, model=ref, body=body),
        resolved=ResolvedModel(
            original_model=ref,
            provider_id=provider_id,
            provider_model=model_id,
            provider_model_ref=ref,
            reasoning_preference=ReasoningPreference.INHERIT,
        ),
    )


def test_recovery_param_sets_protect_core_inputs() -> None:
    assert "response_format" in LOSSLESS_STRIPPABLE_PARAMS
    assert "image_config" in OPTIONAL_STRIPPABLE_PARAMS
    assert CORE_PROTECTED_PARAMS.isdisjoint(LOSSLESS_STRIPPABLE_PARAMS)
    assert CORE_PROTECTED_PARAMS.isdisjoint(OPTIONAL_STRIPPABLE_PARAMS)
    assert {"model", "prompt", "input", "file", "image", "images", "document"} <= (
        CORE_PROTECTED_PARAMS
    )


@pytest.mark.parametrize(
    ("body", "sent", "renames", "expected_field", "expected_allowed"),
    [
        (
            {
                "error": {
                    "param": "size",
                    "message": "size must be one of 1024x1024, 1536x1024",
                }
            },
            {"size"},
            None,
            "size",
            ("1024x1024", "1536x1024"),
        ),
        (
            {"error": {"message": "seconds must be 4, 8, or 12"}},
            {"seconds"},
            None,
            "seconds",
            ("4", "8", "12"),
        ),
        (
            {
                "message": "duration expected one of std|pro",
                "data": {"param": "duration"},
            },
            {"seconds"},
            {"duration": "seconds"},
            "seconds",
            ("std", "pro"),
        ),
        (
            {
                "error": {
                    "param": "output_format",
                    "message": "allowed values: png, jpeg, webp",
                }
            },
            {"output_format"},
            None,
            "output_format",
            ("png", "jpeg", "webp"),
        ),
    ],
)
def test_parse_param_enum_refusal_extracts_stated_values(
    body, sent, renames, expected_field, expected_allowed
) -> None:
    (found,) = parse_param_enum_refusal(
        400, json.dumps(body).encode(), sent=sent, renames=renames
    )
    assert found.field == expected_field
    assert found.allowed == expected_allowed


def test_note_param_refusal_records_fact_media_param_enum() -> None:
    reset_learned_fact_store()
    req = httpx.Request("POST", "https://api.example/v1/images/generations")
    resp = httpx.Response(
        400,
        json={
            "error": {
                "param": "size",
                "message": "size must be one of 1024x1024, 1536x1024",
            }
        },
        request=req,
    )
    err = httpx.HTTPStatusError("400", request=req, response=resp)
    note_param_refusal(
        err,
        provider_id="prov",
        model_id="m1",
        surface=image_generation_surface(),
        request=MediaRequest(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            rail=MediaRail.IMAGE,
            model="prov/m1",
            body={"prompt": "cat", "size": "512x512"},
        ),
    )
    facts = [
        f
        for f in learned_fact_store().all_facts()
        if f.fact_kind == FACT_MEDIA_PARAM_ENUM
    ]
    assert len(facts) == 1
    assert facts[0].detail == "image_generate:size"
    assert facts[0].value == ["1024x1024", "1536x1024"]


def test_lossless_mode_recovers_response_format_b64_json_and_preflights_next() -> None:
    reset_learned_fact_store()
    seen_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_bodies.append(payload)
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

    leaf = MediaLeaf(
        provider_id="prov",
        config=ProviderConfig(api_key="k", base_url="https://api.example.invalid/v1"),
        surfaces=(image_generation_surface(),),
        rate_limiter=_limiter(),
        transport=httpx.MockTransport(handler),
        param_recovery_mode="lossless",
    )

    async def run_once() -> MediaResponse:
        chunks = [
            chunk
            async for chunk in leaf.execute(
                _attempt(
                    MEDIA_OPERATION_IMAGE_GENERATE,
                    {"prompt": "cat", "response_format": "b64_json"},
                )
            )
        ]
        assert len(chunks) == 1
        assert isinstance(chunks[0], MediaResponse)
        return chunks[0]

    first = asyncio.run(run_once())
    assert first.status_code == 200
    assert "stripped:response_format(b64_json)" in first.adaptations
    assert len(seen_bodies) == 2
    assert "response_format" in seen_bodies[0]
    assert "response_format" not in seen_bodies[1]

    facts = learned_fact_store().all_facts()
    assert any(
        f.fact_kind == FACT_MEDIA_PARAM_REFUSED
        and f.detail == "image_generate:response_format"
        for f in facts
    )

    # Second request strips proactively in preflight: 1 upstream call.
    seen_bodies.clear()
    second = asyncio.run(run_once())
    assert second.status_code == 200
    assert len(seen_bodies) == 1
    assert "response_format" not in seen_bodies[0]
    assert "stripped:response_format(b64_json)" in second.adaptations


def test_lossless_vs_strip_optional_vs_off_for_optional_and_core_fields(
    monkeypatch, tmp_path
) -> None:
    reset_learned_fact_store()
    request_log.reset_request_log_stores()
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    registry = get_provider_registry()
    provider_id = (
        "custom_optprov"
        if "custom_optprov" in registry.all_descriptors()
        else registry.add(
            display_name="OptProv",
            base_url="https://opt.example/v1",
            api_keys=("sk-opt-" + "a" * 30,),
            media_operations=("image_generate", "video"),
        ).provider_id
    )

    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if "image_config" in payload:
            return httpx.Response(
                400,
                json={
                    "error": {"message": "image_config is not supported", "param": ""}
                },
            )
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

    def call_with_mode(
        mode: str, body: dict[str, Any]
    ) -> tuple[int, list[dict[str, Any]]]:
        seen.clear()
        reset_learned_fact_store()
        settings = Settings.model_validate(
            {
                "MODEL_IMAGE": f"{provider_id}/img-1",
                "PROVIDER_RETRY_ATTEMPTS": "1",
                "MEDIA_PARAM_RECOVERY_MODE": mode,
            }
        )
        media = MediaRegistry(transport=httpx.MockTransport(handler))
        with TestClient(create_test_app(settings, media=media)) as client:
            resp = client.post("/v1/images/generations", json=body)
        return resp.status_code, list(seen)

    # lossless: image_config NOT stripped (400), response_format="b64_json" IS stripped (200)
    status_lossless, calls_lossless = call_with_mode(
        "lossless", {"prompt": "cube", "image_config": {"aspect_ratio": "1:1"}}
    )
    assert status_lossless == 400
    assert len(calls_lossless) == 1

    # strip_optional: image_config IS stripped and retried (200)
    status_strip, calls_strip = call_with_mode(
        "strip_optional", {"prompt": "cube", "image_config": {"aspect_ratio": "1:1"}}
    )
    assert status_strip == 200
    assert len(calls_strip) == 2
    assert "image_config" not in calls_strip[1]
    assert calls_strip[1]["prompt"] == "cube"

    # off: even response_format="b64_json" is NOT retried
    status_off, calls_off = call_with_mode(
        "off", {"prompt": "cube", "response_format": "b64_json"}
    )
    assert status_off == 400
    assert len(calls_off) == 1


def test_enum_clamping_requires_strip_optional_and_approximate_mapping() -> None:
    reset_learned_fact_store()
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if payload.get("seconds") not in (4, 8, 12):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "param": "seconds",
                        "message": "seconds must be 4, 8, or 12",
                    }
                },
            )
        return httpx.Response(200, json={"id": "vid-1", "status": "queued"})

    surface = video_surfaces()[0]
    attempt = _attempt(MEDIA_OPERATION_VIDEO_CREATE, {"prompt": "wave", "seconds": 6})

    # strip_optional without approximate_param_mapping: fails 400 (no guessing)
    leaf_strict = MediaLeaf(
        provider_id="prov",
        config=ProviderConfig(api_key="k", base_url="https://api.example.invalid/v1"),
        surfaces=(surface,),
        rate_limiter=_limiter(),
        transport=httpx.MockTransport(handler),
        param_recovery_mode="strip_optional",
        approximate_param_mapping=False,
    )
    with pytest.raises(ExecutionFailure):
        asyncio.run(leaf_strict.execute(attempt).__anext__())
    assert len(seen) == 1

    # strip_optional + approximate_param_mapping=True: clamps 6 -> 4 (nearest) and succeeds
    seen.clear()
    reset_learned_fact_store()
    leaf_approx = MediaLeaf(
        provider_id="prov",
        config=ProviderConfig(api_key="k", base_url="https://api.example.invalid/v1"),
        surfaces=(surface,),
        rate_limiter=_limiter(),
        transport=httpx.MockTransport(handler),
        param_recovery_mode="strip_optional",
        approximate_param_mapping=True,
    )
    chunk = asyncio.run(leaf_approx.execute(attempt).__anext__())
    assert isinstance(chunk, MediaResponse)
    assert chunk.status_code == 200
    assert len(seen) == 2
    assert seen[1]["seconds"] == 4
    assert any(a.startswith("clamped:seconds(6->4)") for a in chunk.adaptations)


def test_apply_media_preflight_injects_overrides_and_never_touches_core() -> None:
    overrides = MediaOverridesDocument(
        models={
            "prov/gpt-image-1": {
                MEDIA_OPERATION_IMAGE_GENERATE: MediaOperationOverride(
                    param_defaults={"quality": "high", "prompt": "hijack"},
                    unset_params=("style", "prompt"),
                )
            }
        }
    )
    attempt = _attempt(
        MEDIA_OPERATION_IMAGE_GENERATE,
        {"prompt": "my cat", "style": "vivid"},
    )
    updated, adaptations = apply_media_preflight(
        attempt,
        image_generation_surface(),
        mode="lossless",
        approximate_mapping=False,
        overrides=overrides,
    )
    assert updated.request.body == {"prompt": "my cat", "quality": "high"}
    assert "override:unset:style" in adaptations
    assert "override:default:quality" in adaptations
