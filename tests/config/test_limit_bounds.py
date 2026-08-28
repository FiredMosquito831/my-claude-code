"""A limit always resolves to something usable, however it was written."""

from typing import Any

import pytest

from my_claude_code.config.admin.manifest import FIELDS
from my_claude_code.config.admin.validation import range_errors
from my_claude_code.config.limits import LIMIT_RANGES, ZSTD_MAX_LEVEL, range_for
from my_claude_code.config.settings import Settings

LIMIT_ATTRS = tuple(LIMIT_RANGES)


def _alias(attr: str) -> str:
    """Settings fields are populated by env alias, not by field name."""
    field = Settings.model_fields[attr]
    return str(field.validation_alias) if field.validation_alias else attr.upper()


def _settings(**env: str) -> Settings:
    # Same shape the admin validator uses: env values arrive as strings and the
    # model coerces them, which a precisely-typed kwargs dict cannot express.
    kwargs: dict[str, Any] = {"_env_file": None, **env}
    return Settings(**kwargs)


def _with(attr: str, value: str) -> Settings:
    return _settings(**{_alias(attr): value})


@pytest.mark.parametrize("attr", LIMIT_ATTRS)
def test_a_blank_value_falls_back_to_the_default(attr: str) -> None:
    """The admin UI writes `KEY=` for a cleared field, so blank is not exotic."""
    default = Settings.model_fields[attr].default
    assert getattr(_with(attr, ""), attr) == default


@pytest.mark.parametrize("attr", LIMIT_ATTRS)
def test_an_absent_value_falls_back_to_the_default(attr: str) -> None:
    default = Settings.model_fields[attr].default
    assert getattr(_settings(), attr) == default


@pytest.mark.parametrize("attr", LIMIT_ATTRS)
def test_a_value_below_the_range_is_clamped_not_fatal(attr: str) -> None:
    """A proxy that will not start is worse than one running a sane number."""
    limit = LIMIT_RANGES[attr]
    resolved = getattr(_with(attr, str(limit.minimum - 1000)), attr)
    assert resolved == type(resolved)(limit.minimum)


@pytest.mark.parametrize("attr", LIMIT_ATTRS)
def test_a_value_above_the_range_is_clamped_not_fatal(attr: str) -> None:
    limit = LIMIT_RANGES[attr]
    resolved = getattr(_with(attr, str(limit.maximum + 1000)), attr)
    assert resolved == type(resolved)(limit.maximum)


@pytest.mark.parametrize("attr", LIMIT_ATTRS)
def test_the_default_sits_inside_its_own_range(attr: str) -> None:
    """A default outside its range would be clamped on every single boot."""
    default = Settings.model_fields[attr].default
    if default is None:
        # An optional limit that ships unset -- MAX_OUTPUT_TOKENS_CEILING is
        # the only one, and deliberately so. There is nothing to keep in range
        # until an operator names a value, which the clamp tests above cover.
        return
    assert LIMIT_RANGES[attr].contains(default)


def test_the_compression_level_cannot_exceed_what_zstd_accepts() -> None:
    """Level 42 validated fine and then failed on every body write."""
    from compression import zstd

    limit = LIMIT_RANGES["request_log_compression_level"]
    assert limit.maximum == ZSTD_MAX_LEVEL
    zstd.compress(b"probe", level=int(limit.maximum))
    with pytest.raises(ValueError):
        zstd.compress(b"probe", level=int(limit.maximum) + 1)


def test_a_provider_is_always_allowed_one_attempt() -> None:
    """0 retries would mean never calling the provider at all."""
    assert LIMIT_RANGES["provider_retry_attempts"].minimum >= 1
    assert _settings(PROVIDER_RETRY_ATTEMPTS="0").provider_retry_attempts == 1


@pytest.mark.parametrize("attr", LIMIT_ATTRS)
def test_the_form_publishes_the_same_range_the_server_clamps_to(attr: str) -> None:
    """One table, so the form cannot accept what the server would change."""
    field = next(f for f in FIELDS if f.settings_attr == attr)
    limit = LIMIT_RANGES[attr]
    assert field.minimum == limit.minimum
    assert field.maximum == limit.maximum
    assert "Accepts" in field.description


def test_the_form_rejects_an_out_of_range_value_instead_of_clamping() -> None:
    assert range_errors({"REQUEST_LOG_COMPRESSION_LEVEL": "42"})
    assert range_errors({"PROVIDER_RETRY_ATTEMPTS": "0"})
    assert not range_errors({"REQUEST_LOG_COMPRESSION_LEVEL": "9"})
    assert not range_errors({"REQUEST_LOG_COMPRESSION_LEVEL": ""})


def test_a_field_without_a_range_is_left_alone() -> None:
    assert range_for("model") is None
    assert range_for(None) is None


def test_a_below_floor_desktop_window_width_is_clamped_to_the_real_floor() -> None:
    """Locks the actual usable floor, not just whatever the table currently says.

    The generic parametrized checks above read their expectation out of
    ``LIMIT_RANGES`` itself, so they cannot catch someone silently widening a
    bound. This hardcodes the real floor a too-small window would be clamped
    to.
    """
    assert _with("desktop_window_width", "100").desktop_window_width == 640
    assert _with("desktop_window_height", "50").desktop_window_height == 480


def test_a_zero_desktop_health_failure_threshold_still_reports_on_first_failure() -> (
    None
):
    """A threshold of 0 would mean "never report an outage"; the floor is 1."""
    assert (
        _with("desktop_health_failure_threshold", "0").desktop_health_failure_threshold
        == 1
    )


# --- RATE_LIMIT_COOLDOWN_SECONDS --------------------------------------------
#
# This limit's floor is a real value, not just a clamp destination: the range
# table documents 0 as "does not pause". These pin that contract by name so a
# future refactor cannot quietly turn the setting into either a crash or a
# refused boot.


def test_zero_cooldown_is_a_real_published_value() -> None:
    resolved = _with("rate_limit_cooldown_seconds", "0")
    assert resolved.rate_limit_cooldown_seconds == 0.0


def test_negative_cooldown_clamps_to_no_pause() -> None:
    resolved = _with("rate_limit_cooldown_seconds", "-5")
    assert resolved.rate_limit_cooldown_seconds == 0.0


def test_blank_cooldown_falls_back_to_the_shipped_default() -> None:
    """The shipped default is 60 seconds."""
    assert Settings.model_fields["rate_limit_cooldown_seconds"].default == 60.0
    resolved = _with("rate_limit_cooldown_seconds", "")
    assert resolved.rate_limit_cooldown_seconds == 60.0
