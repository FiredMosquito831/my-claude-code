"""Tests for dedicated ~/.mcc/media_overrides.json store and IV.6 compliance (8.61.0)."""

import ast
import inspect
import json
from pathlib import Path

from my_claude_code.config import media_overrides
from my_claude_code.config.media_overrides import (
    EMPTY_MEDIA_OVERRIDES,
    MediaOperationOverride,
    MediaOverridesDocument,
    current_media_overrides,
    effective_media_override,
    load_media_overrides,
    reset_media_overrides_cache,
    save_media_overrides,
    with_media_override,
)
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_VIDEO_CREATE,
)
from my_claude_code.config.paths import MEDIA_OVERRIDES_FILENAME, media_overrides_path


def test_no_model_name_branch() -> None:
    """IV.6: media_overrides never branches on a model's name."""

    source = Path(inspect.getfile(media_overrides)).read_text(encoding="utf-8")
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


def test_media_overrides_roundtrip_and_key_normalization(tmp_path: Path) -> None:
    path = tmp_path / MEDIA_OVERRIDES_FILENAME
    doc = EMPTY_MEDIA_OVERRIDES
    doc = with_media_override(
        doc,
        "  Provider_A ",
        MEDIA_OPERATION_IMAGE_GENERATE,
        MediaOperationOverride(
            preferred_surface="surface_alpha",
            param_defaults={"aspect_ratio": "16:9", "quality": "standard"},
            unset_params=("style",),
        ),
    )
    doc = with_media_override(
        doc,
        "Provider_A/Model-One",
        MEDIA_OPERATION_IMAGE_GENERATE,
        MediaOperationOverride(
            preferred_surface="surface_beta",
            pin_surface=False,
            param_defaults={"aspect_ratio": "1:1"},
            unset_params=("response_format", "quality"),
        ),
    )
    save_media_overrides(doc, path)

    loaded = load_media_overrides(path)
    assert loaded == doc
    assert "provider_a" in loaded.providers
    assert "provider_a/model-one" in loaded.models

    raw_json = json.loads(path.read_text(encoding="utf-8"))
    assert raw_json["models"]["provider_a/model-one"]["image_generate"] == {
        "preferred_surface": "surface_beta",
        "pin_surface": False,
        "param_defaults": {"aspect_ratio": "1:1"},
        "unset_params": ["response_format", "quality"],
    }

    mixed_case_path = tmp_path / "mixed_case_media_overrides.json"
    mixed_case_path.write_text(
        json.dumps(
            {
                "providers": {
                    "  Provider_A ": {
                        "image_generate": {"preferred_surface": "surface_alpha"}
                    }
                },
                "models": {
                    "Provider_A/Model-One": {
                        "image_generate": {"preferred_surface": "surface_beta"}
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    mixed_loaded = load_media_overrides(mixed_case_path)
    assert "provider_a" in mixed_loaded.providers
    assert "provider_a/model-one" in mixed_loaded.models


def test_effective_media_override_merges_provider_and_model() -> None:
    doc = MediaOverridesDocument(
        providers={
            "provider_a": {
                MEDIA_OPERATION_IMAGE_GENERATE: MediaOperationOverride(
                    preferred_surface="surface_alpha",
                    pin_surface=True,
                    param_defaults={"aspect_ratio": "16:9", "quality": "hd"},
                    unset_params=("style",),
                )
            }
        },
        models={
            "provider_a/model-one": {
                MEDIA_OPERATION_IMAGE_GENERATE: MediaOperationOverride(
                    param_defaults={"aspect_ratio": "1:1", "style": "vivid"},
                    unset_params=("response_format", "quality"),
                )
            },
            "provider_a/model-two": {
                MEDIA_OPERATION_IMAGE_GENERATE: MediaOperationOverride(
                    preferred_surface="surface_beta",
                    pin_surface=False,
                )
            },
        },
    )

    # Model-one sets params only -> inherits provider's surface/pin, overrides params.
    eff_one = effective_media_override(
        doc, "Provider_A/Model-One", MEDIA_OPERATION_IMAGE_GENERATE
    )
    assert eff_one.preferred_surface == "surface_alpha"
    assert eff_one.pin_surface is True
    assert eff_one.pinned_surface == "surface_alpha"
    assert eff_one.param_defaults == {"aspect_ratio": "1:1", "style": "vivid"}
    assert eff_one.unset_params == ("response_format", "quality")

    # Model-two sets preferred_surface with pin_surface=False -> replaces provider pin.
    eff_two = effective_media_override(
        doc, "provider_a/model-two", MEDIA_OPERATION_IMAGE_GENERATE
    )
    assert eff_two.preferred_surface == "surface_beta"
    assert eff_two.pin_surface is False
    assert eff_two.pinned_surface is None
    assert eff_two.param_defaults == {"aspect_ratio": "16:9", "quality": "hd"}
    assert eff_two.unset_params == ("style",)

    # Unmentioned model inherits provider row verbatim.
    eff_other = effective_media_override(
        doc, "provider_a/other", MEDIA_OPERATION_IMAGE_GENERATE
    )
    assert eff_other == doc.providers["provider_a"][MEDIA_OPERATION_IMAGE_GENERATE]


def test_missing_and_corrupt_files_degrade_safely(tmp_path: Path) -> None:
    path = tmp_path / MEDIA_OVERRIDES_FILENAME
    assert load_media_overrides(path) == EMPTY_MEDIA_OVERRIDES

    path.write_text("{bad json", encoding="utf-8")
    assert load_media_overrides(path) == EMPTY_MEDIA_OVERRIDES

    path.write_text("[]", encoding="utf-8")
    assert load_media_overrides(path) == EMPTY_MEDIA_OVERRIDES

    path.write_text(
        json.dumps(
            {
                "providers": {"bad_op": {"unknown_op": {"preferred_surface": "x"}}},
                "models": {
                    "no_slash": {"image_generate": {"preferred_surface": "x"}},
                    "prov/m": {
                        "video_create": {
                            "preferred_surface": "surface_x",
                            "pin_surface": "not_a_bool",
                            "param_defaults": {
                                "model": "forbidden",
                                "size": "1024x1024",
                            },
                            "unset_params": ["model", 123, "response_format"],
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    loaded = load_media_overrides(path)
    assert loaded.providers == {}
    ov = effective_media_override(loaded, "prov/m", MEDIA_OPERATION_VIDEO_CREATE)
    assert ov.preferred_surface == "surface_x"
    assert ov.pin_surface is False
    assert ov.param_defaults == {"size": "1024x1024"}
    assert ov.unset_params == ("response_format",)


def test_current_media_overrides_uses_path_and_stat_cache(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    from my_claude_code.config.paths import reset_config_dir_cache

    reset_config_dir_cache()
    reset_media_overrides_cache()
    try:
        assert media_overrides_path() == tmp_path / MEDIA_OVERRIDES_FILENAME
        assert current_media_overrides() == EMPTY_MEDIA_OVERRIDES
        doc = with_media_override(
            EMPTY_MEDIA_OVERRIDES,
            "prov/m1",
            MEDIA_OPERATION_IMAGE_GENERATE,
            MediaOperationOverride(surface="openai_images"),
        )
        save_media_overrides(doc)
        assert current_media_overrides() == doc
    finally:
        reset_media_overrides_cache()
        reset_config_dir_cache()
