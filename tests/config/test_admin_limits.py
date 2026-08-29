"""Limits are editable, and saving cannot delete what the form never showed."""

import pytest

from my_claude_code.config.admin.manifest import FIELD_BY_KEY, FIELDS, SECTIONS
from my_claude_code.config.admin.persistence import (
    render_env_file,
    settings_env_aliases,
    target_values_with_updates,
    unmanaged_env_values,
)
from my_claude_code.config.settings import Settings

LIMIT_KEYS = (
    "MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT",
    "MAX_OUTPUT_TOKENS_CEILING",
    "MAX_OUTPUT_TOKENS_CONTEXT_MARGIN",
    "MAX_OUTPUT_TOKENS_CONTEXT_FLOOR",
    "REASONING_ANSWER_FLOOR_MAX",
    "FALLBACK_FIRST_TOKEN_TIMEOUT",
    "FALLBACK_TOTAL_TIMEOUT",
    "FALLBACK_STALL_TIMEOUT",
    "FALLBACK_EJECT_AFTER_FAILURES",
    "FALLBACK_EJECT_SECONDS",
    "FALLBACK_BENCH_ENABLED",
    "FALLBACK_BEHAVIOR",
    "FALLBACK_RETRY_FIRST",
    "FALLBACK_EJECT_WINDOW",
    "FALLBACK_EJECT_FAILURE_RATE",
    "FALLBACK_EJECT_MIN_SAMPLES",
    "PROVIDER_RETRY_ATTEMPTS",
    "STREAM_EARLY_RETRY_ATTEMPTS",
    "STREAM_MIDSTREAM_RECOVERY_ATTEMPTS",
    "STREAM_COMMIT_HOLDBACK_SECONDS",
    "RATE_LIMIT_COOLDOWN_SECONDS",
    "CREDENTIAL_LOCKOUT_TIERS",
    "FALLBACK_COOLDOWN_STEP_OVER_FLOOR",
    "PROVIDER_RETRY_BACKOFF_BASE_SECONDS",
    "PROVIDER_RETRY_BACKOFF_MAX_SECONDS",
    "PROVIDER_RETRY_BACKOFF_JITTER_SECONDS",
    "REQUEST_LOG_ENABLED",
    "REQUEST_LOG_MAX_ROWS",
    "REQUEST_LOG_CAPTURE_BODIES",
    "REQUEST_LOG_COMPRESS_BODIES",
    "REQUEST_LOG_TEXT_MAX_CHARS",
    "REQUEST_LOG_CAPTURE_IMAGES",
    "REQUEST_LOG_IMAGE_MAX_PIXELS",
    "SERVER_GRACEFUL_SHUTDOWN_SECONDS",
    "REQUEST_LOG_COMPRESSION_LEVEL",
    "REQUEST_LOG_QUEUE_MAX_SIZE",
    "LOG_LEVEL",
)

# Tool-result trimming and the two local skip rules moved to their own section
# when the Token Optimizer page was added: they are one feature with one page,
# and a setting rendered in two places is a setting that can be shown two
# answers. They are still limits in spirit, so they keep the same guarantees --
# editable, bound to a real setting, and defaulting to what the code defaults
# to -- checked below against the section that now owns them.
OPTIMIZER_KEYS = (
    "ENABLE_TITLE_GENERATION_SKIP",
    "ENABLE_SUGGESTION_MODE_SKIP",
    "ENABLE_TOOL_RESULT_TRIMMING",
    "TOOL_RESULT_TRIM_READ",
    "TOOL_RESULT_TRIM_GREP",
    "TOOL_RESULT_TRIM_GLOB",
    "TOOL_RESULT_TRIM_THRESHOLD_CHARS",
    "TOOL_RESULT_TRIM_KEEP_HEAD_CHARS",
    "TOOL_RESULT_TRIM_KEEP_TAIL_CHARS",
    "TOOL_RESULT_TRIM_PROTECT_RECENT_RESULTS",
)


def test_there_is_a_limits_section() -> None:
    assert any(section.section_id == "limits" for section in SECTIONS)


@pytest.mark.parametrize("key", LIMIT_KEYS)
def test_every_limit_is_editable_and_bound_to_a_real_setting(key: str) -> None:
    field = FIELD_BY_KEY[key]
    assert field.section_id == "limits"
    attr = field.settings_attr
    assert attr, f"{key} has no settings attribute"
    assert hasattr(Settings(), attr), f"{key} points at a setting that does not exist"


def test_there_is_an_optimizer_section() -> None:
    assert any(section.section_id == "optimizer" for section in SECTIONS)


@pytest.mark.parametrize("key", OPTIMIZER_KEYS)
def test_every_optimizer_field_is_editable_and_bound_to_a_real_setting(
    key: str,
) -> None:
    field = FIELD_BY_KEY[key]
    assert field.section_id == "optimizer"
    attr = field.settings_attr
    assert attr, f"{key} has no settings attribute"
    assert hasattr(Settings(), attr), f"{key} points at a setting that does not exist"


def test_no_optimizer_field_is_orphaned() -> None:
    """Every field in the section is listed above, so the list cannot rot."""
    declared = {f.key for f in FIELDS if f.section_id == "optimizer"}
    assert declared == set(OPTIMIZER_KEYS)


@pytest.mark.parametrize("key", LIMIT_KEYS + OPTIMIZER_KEYS)
def test_each_limit_default_matches_the_setting_default(key: str) -> None:
    """A form that shows a different default than the code uses is a lie."""
    field = FIELD_BY_KEY[key]
    attr = field.settings_attr
    assert attr is not None
    actual = getattr(Settings(), attr)
    if actual is None:
        # An optional limit that ships unset shows an empty box, not the word
        # "None". MAX_OUTPUT_TOKENS_CEILING is the only one: a ceiling that
        # binds below a model's own published limit is exactly what the
        # per-model output budget exists to avoid, so it is opt-in.
        assert field.default == ""
        return
    if isinstance(actual, bool):
        assert field.default == ("true" if actual else "false")
    elif isinstance(actual, int | float):
        assert float(field.default) == float(actual)
    else:
        assert field.default == str(actual)


def test_saving_keeps_an_env_key_the_form_never_showed(tmp_path) -> None:
    """The bug this covers silently deleted hand-written settings on Save."""
    key = _live_but_unmanaged_key()
    env = tmp_path / ".env"
    env.write_text(f"MODEL=nvidia_nim/a/b\n{key}=42\n", encoding="utf-8")

    preserved = unmanaged_env_values(env)
    assert preserved == {key: "42"}

    rendered = render_env_file({"MODEL": "nvidia_nim/a/b"}, preserved=preserved)
    assert f"{key}=42" in rendered


def _live_but_unmanaged_key() -> str:
    """A setting FCC still reads that the admin form does not show."""
    managed = {field.key for field in FIELDS}
    for alias in sorted(settings_env_aliases() - managed):
        return alias
    pytest.skip("every setting is editable, so nothing can be dropped")


def test_a_key_that_configures_nothing_is_not_written_back(tmp_path) -> None:
    """A retired option should retire, not be preserved forever."""
    env = tmp_path / ".env"
    env.write_text("NOT_A_SETTING_ANY_MORE=1\n", encoding="utf-8")

    assert unmanaged_env_values(env) == {}


def test_a_managed_key_is_not_duplicated_into_the_preserved_block(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("REQUEST_LOG_MAX_ROWS=500000\n", encoding="utf-8")

    assert unmanaged_env_values(env) == {}

    rendered = render_env_file(
        target_values_with_updates({}) | {"REQUEST_LOG_MAX_ROWS": "500000"},
        preserved=unmanaged_env_values(env),
    )
    assert rendered.count("REQUEST_LOG_MAX_ROWS=") == 1


def test_retention_survives_a_round_trip_through_the_form() -> None:
    """The exact failure reported: a raised cap reverting to the default."""
    values = target_values_with_updates({"REQUEST_LOG_MAX_ROWS": "500000"})
    assert values["REQUEST_LOG_MAX_ROWS"] == "500000"
    assert "REQUEST_LOG_MAX_ROWS=500000" in render_env_file(values)


def test_no_limit_field_is_orphaned() -> None:
    """Every field in the section is listed above, so the list cannot rot."""
    declared = {f.key for f in FIELDS if f.section_id == "limits"}
    assert declared == set(LIMIT_KEYS)
