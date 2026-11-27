"""Tests for unified MediaCapabilityProfile resolution and IV.6 compliance (8.60.0)."""

import ast
import inspect
import json
from pathlib import Path

from my_claude_code.application.litellm_model_map import (
    LiteLLMModel,
    LiteLLMStatement,
)
from my_claude_code.application.media import capabilities
from my_claude_code.application.media.capabilities import (
    FACT_MEDIA_ENCODING_REFUSED,
    FACT_MEDIA_PARAM_ENUM,
    MEDIA_OPERATION_OCR,
    SOURCE_LEARNED,
    SOURCE_LITELLM,
    SOURCE_MODELS_DEV,
    SOURCE_OPENROUTER_LIVE,
    SOURCE_OPERATOR,
    SOURCE_PROVIDER_DEFAULT,
    SOURCE_PROVIDER_LIST,
    MediaCapabilityInputs,
    resolve_media_capability_profile,
)
from my_claude_code.application.media.schemas import (
    FACT_MEDIA_PARAM_REFUSED,
    FACT_MEDIA_PARAM_REQUIRED,
)
from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ProviderModelDeclaration,
    ProviderModelInfo,
)
from my_claude_code.application.openrouter_live import LIVE_MATCH_EXACT, LiveModel
from my_claude_code.config.media_surfaces import (
    MEDIA_ENCODING_JSON,
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_VIDEO_CREATE,
    gemini_speech_surface,
    image_edit_surface,
    image_generation_surface,
    speech_surface,
    video_surfaces,
)


def test_no_model_name_branch() -> None:
    """IV.6: capabilities resolution never branches on a model's name."""

    source = Path(inspect.getfile(capabilities)).read_text(encoding="utf-8")
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


def test_provider_published_row_generation_methods_and_capabilities() -> None:
    """Raw ``supportedGenerationMethods`` and capability flags feed MediaCapabilityProfile."""

    row = {
        "name": "models/hybrid-media-1",
        "supportedGenerationMethods": [
            "generateContent",
            "predict",
            "predictLongRunning",
        ],
        "supported_sizes": ["1024x1024", "1536x1024"],
        "supported_aspect_ratios": ["1:1", "16:9"],
        "supported_resolutions": ["720p", "1080p"],
        "supported_durations": [4, 8],
        "supports_mask": True,
        "max_input_images": 3,
    }
    info = ProviderModelInfo(
        model_id="hybrid-media-1",
        supported_parameters=frozenset({"aspect_ratio", "seed"}),
        declared=ProviderModelDeclaration(endpoints=("generatecontent",)),
        published_row=json.dumps(row),
    )

    profile = resolve_media_capability_profile(
        "p/hybrid-media-1",
        MediaCapabilityInputs(
            provider_info=info,
            surfaces=(image_generation_surface(), *video_surfaces()),
            provider_list_as_of="2026-11-27T00:00:00+00:00",
        ),
    )

    assert profile.supported_generation_methods == frozenset(
        {"generateContent", "predict", "predictLongRunning"}
    )
    assert profile.operations == frozenset(
        {MEDIA_OPERATION_IMAGE_GENERATE, MEDIA_OPERATION_VIDEO_CREATE}
    )
    assert profile.sync_mode == "async_job"
    assert profile.supported_parameters == frozenset({"aspect_ratio", "seed"})
    assert profile.accepted_sizes == ("1024x1024", "1536x1024")
    assert profile.accepted_aspect_ratios == ("1:1", "16:9")
    assert profile.accepted_resolutions == ("720p", "1080p")
    assert profile.accepted_durations == (4, 8)
    assert profile.supports_mask is True
    assert profile.max_input_images == 3
    assert profile.field_provenance["operations"] == (
        SOURCE_PROVIDER_LIST,
        "2026-11-27T00:00:00+00:00",
    )


def test_mistral_capabilities_flags_resolve_ocr_and_speech() -> None:
    row = {
        "id": "doc-speech-1",
        "capabilities": {"ocr": True, "audio_speech": True, "completion_chat": False},
    }
    info = ProviderModelInfo(
        model_id="doc-speech-1",
        declared=ProviderModelDeclaration(
            endpoints=("audio_speech", "non_chat", "ocr")
        ),
        published_row=json.dumps(row),
    )

    profile = resolve_media_capability_profile(
        "p/doc-speech-1",
        MediaCapabilityInputs(provider_info=info),
    )

    assert profile.operations == frozenset(
        {MEDIA_OPERATION_OCR, MEDIA_OPERATION_SPEECH}
    )
    assert profile.sync_mode == "sync"


def test_openrouter_live_enrichment_resolves_media_operations() -> None:
    """M4 target: OpenRouter live modalities & parameters enrich silent provider records."""

    live = LiveModel(
        slugs=("vendor/pic-1",),
        match=LIVE_MATCH_EXACT,
        modalities=DeclaredModalities(inputs=("text", "image"), outputs=("image",)),
        supported_parameters=frozenset({"seed", "aspect_ratio"}),
    )

    profile = resolve_media_capability_profile(
        "custom_gw/pic-1",
        MediaCapabilityInputs(
            provider_info=ProviderModelInfo(model_id="pic-1"),
            surfaces=(image_generation_surface(), image_edit_surface()),
            openrouter_live=live,
            openrouter_live_as_of="2026-11-27T01:00:00+00:00",
        ),
    )

    assert profile.operations == frozenset(
        {MEDIA_OPERATION_IMAGE_GENERATE, MEDIA_OPERATION_IMAGE_EDIT}
    )
    assert profile.sync_mode == "sync"
    assert profile.supported_parameters == frozenset({"seed", "aspect_ratio"})
    assert profile.accepts_encodings == frozenset({"json", "multipart"})
    assert profile.field_provenance["operations"] == (
        SOURCE_OPENROUTER_LIVE,
        "2026-11-27T01:00:00+00:00",
    )


def test_models_dev_and_litellm_rungs_fill_lower_gaps() -> None:
    md_profile = resolve_media_capability_profile(
        "custom_a/tts-model",
        MediaCapabilityInputs(
            models_dev_entry={"modalities": {"input": ["text"], "output": ["audio"]}},
            models_dev_as_of="2026-11-26T00:00:00Z",
        ),
    )
    assert md_profile.operations == frozenset({MEDIA_OPERATION_SPEECH})
    assert md_profile.field_provenance["operations"] == (
        SOURCE_MODELS_DEV,
        "2026-11-26T00:00:00Z",
    )

    litellm_profile = resolve_media_capability_profile(
        "custom_a/asr-model",
        MediaCapabilityInputs(
            litellm_entry=LiteLLMModel(
                modalities=LiteLLMStatement(
                    value=DeclaredModalities(inputs=("audio",), outputs=("text",)),
                    key="asr-model",
                    match="exact",
                )
            ),
            litellm_as_of="2026-11-25T00:00:00Z",
        ),
    )
    assert litellm_profile.operations == frozenset({MEDIA_OPERATION_TRANSCRIBE})
    assert litellm_profile.field_provenance["operations"] == (
        SOURCE_LITELLM,
        "2026-11-25T00:00:00Z",
    )


def test_surfaces_fallback_learned_facts_and_operator_overrides() -> None:
    surface_only = resolve_media_capability_profile(
        "p/m1",
        MediaCapabilityInputs(
            surfaces=(
                gemini_speech_surface(),
                speech_surface(formats=("mp3",), stream=True),
            ),
        ),
    )
    assert surface_only.operations == frozenset({MEDIA_OPERATION_SPEECH})
    assert surface_only.field_provenance["operations"] == (
        SOURCE_PROVIDER_DEFAULT,
        None,
    )
    assert surface_only.accepted_formats == ("wav", "pcm", "mp3")
    assert surface_only.accepted_voices is not None
    assert len(surface_only.accepted_voices) == 30
    assert surface_only.supports_streaming is True

    learned = [
        {
            "fact_kind": FACT_MEDIA_PARAM_REQUIRED,
            "detail": "image_generate:mode",
            "last_confirmed_at": "2026-11-26T00:00:00Z",
            "stale": False,
        },
        {
            "fact_kind": FACT_MEDIA_PARAM_REFUSED,
            "detail": "image_generate:response_format",
            "last_confirmed_at": "2026-11-27T00:00:00Z",
            "stale": False,
        },
        {
            "fact_kind": FACT_MEDIA_PARAM_REFUSED,
            "detail": "image_generate:stale_param",
            "last_confirmed_at": "2026-11-01T00:00:00Z",
            "stale": True,
        },
        {
            "fact_kind": FACT_MEDIA_PARAM_ENUM,
            "detail": "image_generate:size",
            "value": ["1024x1024", "1536x1024"],
            "stale": False,
        },
        {
            "fact_kind": FACT_MEDIA_ENCODING_REFUSED,
            "detail": "image_edit:multipart",
            "stale": False,
        },
    ]
    with_learned = resolve_media_capability_profile(
        "p/m2",
        MediaCapabilityInputs(
            surfaces=(image_edit_surface(),),
            learned_facts=learned,
            operator_overrides={
                "accepted_formats": ["png"],
                "refused_params": ["style"],
            },
        ),
    )
    assert with_learned.required_params == frozenset({"mode"})
    assert with_learned.refused_params == frozenset({"response_format", "style"})
    assert with_learned.accepted_sizes == ("1024x1024", "1536x1024")
    assert with_learned.field_provenance["accepted_sizes"] == (SOURCE_LEARNED, None)
    assert with_learned.accepted_formats == ("png",)
    assert with_learned.field_provenance["accepted_formats"] == (SOURCE_OPERATOR, None)
    assert with_learned.accepts_encodings == frozenset({MEDIA_ENCODING_JSON})


def test_malformed_published_row_is_ignored_safely() -> None:
    profile = resolve_media_capability_profile(
        "p/broken",
        MediaCapabilityInputs(
            provider_info=ProviderModelInfo(
                model_id="broken", published_row="{not-json"
            ),
            surfaces=(image_generation_surface(),),
        ),
    )
    assert profile.operations == frozenset({MEDIA_OPERATION_IMAGE_GENERATE})
