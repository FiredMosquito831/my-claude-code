"""MCC_SMOKE_* fields were retired from the admin manifest.

They never rendered anywhere (no dashboard view claimed the "smoke"
section), yet `render_env_file` wrote all 54 of them into every user's
managed .env on every save. Removing them from the manifest must not
silently delete a value someone actually set to drive the smoke suite --
that is the exact "a form must not delete what it cannot see" bug class
this project has been bitten by before. So the persistence layer now
preserves any unmanaged `FCC_`-prefixed key with a non-empty value, even
though nothing in Settings reads it.
"""

from my_claude_code.config.admin.manifest import FIELDS, SECTIONS
from my_claude_code.config.admin.persistence import (
    render_env_file,
    unmanaged_env_values,
)


def test_no_smoke_section_in_the_manifest() -> None:
    assert not any(section.section_id == "smoke" for section in SECTIONS)


def test_no_smoke_fields_in_the_manifest() -> None:
    assert not any(field.key.startswith("MCC_SMOKE_") for field in FIELDS)


def test_render_env_file_emits_no_smoke_keys_or_header() -> None:
    rendered = render_env_file({}, preserved={})
    assert "MCC_SMOKE" not in rendered
    assert "# Smoke Tests" not in rendered


_OLD_FORMAT_ENV = """\
MODEL=nvidia_nim/old-model
LOG_RAW_API_PAYLOADS=true
MCC_SMOKE_MODEL_OPENAI=gpt-4o-mini
MCC_SMOKE_MODEL_NVIDIA_NIM=
MCC_SMOKE_MODEL_DEEPSEEK=
MCC_SMOKE_NIM_MODELS=
MCC_SMOKE_OPENROUTER_FREE_MODELS=
"""


def test_round_trip_preserves_populated_smoke_key_drops_empty_ones(
    tmp_path,
) -> None:
    """The critical backwards-compatibility case: load an old-format .env,
    save through the real admin path, and confirm the populated smoke value
    survives untouched while the empty smoke keys and the retired manifest
    fields disappear -- and ordinary managed settings are unaffected.
    """

    env = tmp_path / ".env"
    env.write_text(_OLD_FORMAT_ENV, encoding="utf-8")

    preserved = unmanaged_env_values(env)

    assert preserved.get("MCC_SMOKE_MODEL_OPENAI") == "gpt-4o-mini"
    assert "MCC_SMOKE_MODEL_NVIDIA_NIM" not in preserved
    assert "MCC_SMOKE_MODEL_DEEPSEEK" not in preserved
    assert "MCC_SMOKE_NIM_MODELS" not in preserved
    assert "MCC_SMOKE_OPENROUTER_FREE_MODELS" not in preserved

    rendered = render_env_file(
        {"MODEL": "nvidia_nim/old-model", "LOG_RAW_API_PAYLOADS": "true"},
        preserved=preserved,
    )

    assert "MCC_SMOKE_MODEL_OPENAI=gpt-4o-mini" in rendered
    assert "MCC_SMOKE_MODEL_NVIDIA_NIM" not in rendered
    assert "MCC_SMOKE_MODEL_DEEPSEEK" not in rendered
    assert "MCC_SMOKE_NIM_MODELS" not in rendered
    assert "MCC_SMOKE_OPENROUTER_FREE_MODELS" not in rendered
    assert "MODEL=nvidia_nim/old-model" in rendered
    assert "LOG_RAW_API_PAYLOADS=true" in rendered


def test_non_fcc_unmanaged_key_with_a_value_is_still_dropped(tmp_path) -> None:
    """The prefix widening must not weaken the rule for non-FCC_ keys."""

    env = tmp_path / ".env"
    env.write_text("SOME_RANDOM_TOOL_VAR=keep-me-please\n", encoding="utf-8")

    assert unmanaged_env_values(env) == {}


def test_old_format_env_shrinks_by_the_expected_line_count(tmp_path) -> None:
    """4 of the 5 smoke lines in the old file (the empty ones) must vanish;
    the populated one is carried into the rendered preserved block instead.
    """

    env = tmp_path / ".env"
    env.write_text(_OLD_FORMAT_ENV, encoding="utf-8")
    original_line_count = len([line for line in _OLD_FORMAT_ENV.splitlines() if line])

    preserved = unmanaged_env_values(env)
    rendered = render_env_file(
        {"MODEL": "nvidia_nim/old-model", "LOG_RAW_API_PAYLOADS": "true"},
        preserved=preserved,
    )
    rendered_smoke_lines = [
        line for line in rendered.splitlines() if line.startswith("MCC_SMOKE")
    ]

    assert len(rendered_smoke_lines) == 1
    assert original_line_count - len(rendered_smoke_lines) == 6
