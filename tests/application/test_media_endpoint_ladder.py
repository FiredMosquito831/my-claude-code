"""Tests for MediaEndpointLadder resolution and IV.6 compliance (8.61.0)."""

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

from my_claude_code.application.media import (
    FACT_MEDIA_ENDPOINT_REFUSED,
    FACT_MEDIA_ENDPOINT_SURFACE,
    MeasuredMediaEndpointEntry,
    MeasuredMediaEndpointSet,
    endpoint_ladder,
    resolve_media_endpoint_ladder,
)
from my_claude_code.application.media.capabilities import (
    SOURCE_LEARNED,
    SOURCE_MEASURED,
    SOURCE_OPERATOR,
    SOURCE_PROVIDER_DEFAULT,
    SOURCE_PROVIDER_LIST,
    MediaCapabilityProfile,
)
from my_claude_code.config.media_overrides import (
    MediaOperationOverride,
    MediaOverridesDocument,
)
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_VIDEO_CREATE,
    MediaSurface,
    surface_for,
    surface_key,
    surfaces_for,
)
from my_claude_code.providers.recovery import (
    FACT_MEDIA_ENDPOINT_REFUSED as RECOVERY_FACT_REFUSED,
)
from my_claude_code.providers.recovery import (
    FACT_MEDIA_ENDPOINT_SURFACE as RECOVERY_FACT_SURFACE,
)
from my_claude_code.providers.recovery import LearnedFactStore


def test_no_model_name_branch() -> None:
    """IV.6: endpoint ladder resolution never branches on a model's name."""

    source = Path(inspect.getfile(endpoint_ladder)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    def names(node: ast.AST) -> set[str]:
        found: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                found.add(child.id)
            elif isinstance(child, ast.Attribute):
                found.add(child.attr)
        return found

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            parts = [node.left, *node.comparators]
            mentions_model = any(
                "model" in name.lower() for part in parts for name in names(part)
            )
            has_literal = any(
                isinstance(part, ast.Constant) and isinstance(part.value, str)
                for part in parts
            )
            if mentions_model and has_literal:
                offenders.append(ast.unparse(node))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"startswith", "endswith"}
            and any("model" in name.lower() for name in names(node.func.value))
        ):
            offenders.append(ast.unparse(node))
    assert offenders == []

    docstrings = {
        id(body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef)
        and (body := node.body)
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    }
    lowered = " ".join(
        [
            str(node.value).lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        + [name.lower() for name in names(tree)]
    )
    for vendor in (
        "agnes",
        "gpt-",
        "dall-e",
        "sora",
        "veo",
        "imagen",
        "flux",
        "whisper",
        "tts-1",
        "grok",
        "gemini-",
    ):
        assert vendor not in lowered, vendor


def test_fact_constants_match_recovery_facts() -> None:
    assert FACT_MEDIA_ENDPOINT_SURFACE == RECOVERY_FACT_SURFACE
    assert FACT_MEDIA_ENDPOINT_REFUSED == RECOVERY_FACT_REFUSED


def _image_surfaces() -> tuple[MediaSurface, ...]:
    return (
        MediaSurface(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            shape="openai_images",
            path="images/generations",
        ),
        MediaSurface(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            shape="native_generate_content_image",
            path="https://example.invalid/v1beta/models/{model}:generateContent",
            auth_header="x-goog-api-key",
        ),
        MediaSurface(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            shape="native_predict_image",
            path="https://example.invalid/v1beta/models/{model}:predict",
            auth_header="x-goog-api-key",
        ),
    )


def test_surfaces_for_and_surface_for_backward_compat() -> None:
    surfaces = _image_surfaces()
    assert surface_for(surfaces, MEDIA_OPERATION_IMAGE_GENERATE) == surfaces[0]
    assert surfaces_for(surfaces, MEDIA_OPERATION_IMAGE_GENERATE) == surfaces
    assert surface_key(surfaces[0]) == "openai_images"
    custom = MediaSurface(
        operation=MEDIA_OPERATION_IMAGE_GENERATE,
        shape="openai_images",
        path="alt/images",
        surface_id="alt_images",
    )
    assert surface_key(custom) == "alt_images"


def test_endpoint_ladder_precedence() -> None:
    surfaces = _image_surfaces()
    ref = "prov_a/model_one"

    # Default declaration order when no higher rung speaks.
    default_ladder = resolve_media_endpoint_ladder(
        ref, MEDIA_OPERATION_IMAGE_GENERATE, surfaces=surfaces
    )
    assert default_ladder.winner is not None
    assert default_ladder.winner.surface == surfaces[0]
    assert default_ladder.winner.source == SOURCE_PROVIDER_DEFAULT
    assert [c.surface.shape for c in default_ladder.candidates] == [
        "openai_images",
        "native_generate_content_image",
        "native_predict_image",
    ]

    # Rung 4: Capability metadata promotes matching generation method.
    cap = MediaCapabilityProfile(
        operations=frozenset({MEDIA_OPERATION_IMAGE_GENERATE}),
        supported_generation_methods=frozenset({"generateContent"}),
        field_provenance={"supported_generation_methods": (SOURCE_PROVIDER_LIST, None)},
    )
    cap_ladder = resolve_media_endpoint_ladder(
        ref,
        MEDIA_OPERATION_IMAGE_GENERATE,
        surfaces=surfaces,
        capability_profile=cap,
    )
    assert cap_ladder.winner is not None
    assert cap_ladder.winner.surface.shape == "native_generate_content_image"
    assert cap_ladder.winner.source == SOURCE_PROVIDER_LIST

    # Rung 3: Exact measured entry beats capability metadata.
    measured = MeasuredMediaEndpointSet(
        entries={
            ("prov_a", "model_one", MEDIA_OPERATION_IMAGE_GENERATE): (
                MeasuredMediaEndpointEntry(
                    surface_key="native_predict_image", measured_at="2026-11-27"
                )
            )
        }
    )
    measured_ladder = resolve_media_endpoint_ladder(
        ref,
        MEDIA_OPERATION_IMAGE_GENERATE,
        surfaces=surfaces,
        capability_profile=cap,
        measured=measured,
    )
    assert measured_ladder.winner is not None
    assert measured_ladder.winner.surface.shape == "native_predict_image"
    assert measured_ladder.winner.source == SOURCE_MEASURED

    # Rung 2: Learned FACT_MEDIA_ENDPOINT_SURFACE beats measured and capability.
    store = LearnedFactStore()
    store.record(
        "prov_a",
        "model_one",
        FACT_MEDIA_ENDPOINT_SURFACE,
        "native_generate_content_image",
        detail=MEDIA_OPERATION_IMAGE_GENERATE,
    )
    learned_ladder = resolve_media_endpoint_ladder(
        ref,
        MEDIA_OPERATION_IMAGE_GENERATE,
        surfaces=surfaces,
        capability_profile=cap,
        measured=measured,
        learned_facts=store.all_facts(),
    )
    assert learned_ladder.winner is not None
    assert learned_ladder.winner.surface.shape == "native_generate_content_image"
    assert learned_ladder.winner.source == SOURCE_LEARNED

    # Rung 1: Operator preferred_surface beats learned.
    overrides = MediaOverridesDocument(
        models={
            ref: {
                MEDIA_OPERATION_IMAGE_GENERATE: MediaOperationOverride(
                    preferred_surface="openai_images", pin_surface=False
                )
            }
        }
    )
    pref_ladder = resolve_media_endpoint_ladder(
        ref,
        MEDIA_OPERATION_IMAGE_GENERATE,
        surfaces=surfaces,
        capability_profile=cap,
        measured=measured,
        learned_facts=store.all_facts(),
        overrides=overrides,
    )
    assert pref_ladder.winner is not None
    assert pref_ladder.winner.surface.shape == "openai_images"
    assert pref_ladder.winner.source == SOURCE_OPERATOR

    # Refusal demotes unpinned preferred_surface behind non-refused candidates.
    store.record(
        "prov_a",
        "model_one",
        FACT_MEDIA_ENDPOINT_REFUSED,
        True,
        detail=f"{MEDIA_OPERATION_IMAGE_GENERATE}:openai_images",
    )
    refused_ladder = resolve_media_endpoint_ladder(
        ref,
        MEDIA_OPERATION_IMAGE_GENERATE,
        surfaces=surfaces,
        capability_profile=cap,
        measured=measured,
        learned_facts=store.all_facts(),
        overrides=overrides,
    )
    assert refused_ladder.winner is not None
    assert refused_ladder.winner.surface.shape == "native_generate_content_image"
    assert refused_ladder.candidates[-1].surface.shape == "openai_images"
    assert refused_ladder.candidates[-1].refused is True

    # Operator pin_surface=True overrides even refusal.
    pinned_overrides = MediaOverridesDocument(
        models={
            ref: {
                MEDIA_OPERATION_IMAGE_GENERATE: MediaOperationOverride(
                    preferred_surface="openai_images", pin_surface=True
                )
            }
        }
    )
    pinned_ladder = resolve_media_endpoint_ladder(
        ref,
        MEDIA_OPERATION_IMAGE_GENERATE,
        surfaces=surfaces,
        learned_facts=store.all_facts(),
        overrides=pinned_overrides,
    )
    assert pinned_ladder.winner is not None
    assert pinned_ladder.winner.surface.shape == "openai_images"
    assert pinned_ladder.winner.refused is False


def test_stale_or_retired_facts_are_ignored_and_empty_pool_returns_none() -> None:
    surfaces = _image_surfaces()
    stale_at = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    stale_rows = [
        {
            "provider_id": "prov_a",
            "model_id": "model_one",
            "fact_kind": FACT_MEDIA_ENDPOINT_SURFACE,
            "detail": MEDIA_OPERATION_IMAGE_GENERATE,
            "value": "native_predict_image",
            "last_confirmed_at": stale_at,
            "stale": True,
        }
    ]
    ladder = resolve_media_endpoint_ladder(
        "prov_a/model_one",
        MEDIA_OPERATION_IMAGE_GENERATE,
        surfaces=surfaces,
        learned_facts=stale_rows,
    )
    assert ladder.winner is not None
    assert ladder.winner.surface.shape == "openai_images"

    empty = resolve_media_endpoint_ladder(
        "prov_a/model_one",
        MEDIA_OPERATION_VIDEO_CREATE,
        surfaces=surfaces,
    )
    assert empty.candidates == ()
    assert empty.winner is None


def test_capability_predictlongrunning_promotes_lro_surface() -> None:
    video_pool = (
        MediaSurface(
            operation=MEDIA_OPERATION_VIDEO_CREATE,
            shape="openai_videos",
            path="videos",
        ),
        MediaSurface(
            operation=MEDIA_OPERATION_VIDEO_CREATE,
            shape="native_video_lro",
            path="https://example.invalid/v1beta/models/{model}:predictLongRunning",
            auth_header="x-goog-api-key",
        ),
    )
    cap = MediaCapabilityProfile(
        operations=frozenset({MEDIA_OPERATION_VIDEO_CREATE}),
        supported_generation_methods=frozenset({"predictLongRunning"}),
    )
    ladder = resolve_media_endpoint_ladder(
        "prov_a/video_one",
        MEDIA_OPERATION_VIDEO_CREATE,
        surfaces=video_pool,
        capability_profile=cap,
    )
    assert ladder.winner is not None
    assert ladder.winner.surface.shape == "native_video_lro"
