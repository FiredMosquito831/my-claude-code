"""The six prompt-cache lifetime settings and their Model Config fields (7.104.0)."""

import pytest
from pydantic import ValidationError

from my_claude_code.config.admin.manifest import (
    FIELD_BY_KEY,
    ConfigOptionSpec,
    update_affects_providers,
)
from my_claude_code.config.prompt_cache_lifetimes import (
    PROVIDER_PROMPT_CACHE_FAMILY,
    normalize_prompt_cache_ttl,
    prompt_cache_family_for,
)
from my_claude_code.config.settings import Settings

TIER_KEYS = (
    "PROMPT_CACHE_TTL_MYTHOS",
    "PROMPT_CACHE_TTL_FABLE",
    "PROMPT_CACHE_TTL_OPUS",
    "PROMPT_CACHE_TTL_SONNET",
    "PROMPT_CACHE_TTL_HAIKU",
)
ALL_KEYS = ("PROMPT_CACHE_TTL", *TIER_KEYS)


def test_the_shipped_defaults_choose_nothing() -> None:
    settings = Settings.model_validate({})

    assert settings.prompt_cache_ttl == "client"
    for key in TIER_KEYS:
        assert getattr(settings, key.lower()) == "inherit"


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("", "client"),
        ("client", "client"),
        (" CLIENT ", "client"),
        ("30", "30"),
        ("05", "5"),
        ("1", "1"),
        ("60", "60"),
    ],
)
def test_the_global_value_is_stored_in_one_form(raw: str, stored: str) -> None:
    assert Settings.model_validate({"PROMPT_CACHE_TTL": raw}).prompt_cache_ttl == stored


@pytest.mark.parametrize(
    ("raw", "stored"),
    [("", "inherit"), ("inherit", "inherit"), ("client", "client"), ("45", "45")],
)
def test_a_tier_value_is_stored_in_one_form(raw: str, stored: str) -> None:
    settings = Settings.model_validate({"PROMPT_CACHE_TTL_OPUS": raw})

    assert settings.prompt_cache_ttl_opus == stored


@pytest.mark.parametrize("raw", ["0", "61", "90", "-5", "1.5", "30m", "1h", "forever"])
@pytest.mark.parametrize("key", ALL_KEYS)
def test_a_value_outside_the_vocabulary_is_refused(key: str, raw: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({key: raw})


def test_the_global_value_cannot_inherit() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"PROMPT_CACHE_TTL": "inherit"})
    with pytest.raises(ValueError, match="cannot inherit"):
        normalize_prompt_cache_ttl("inherit", tier=False)


def test_only_the_two_first_party_anthropic_paths_are_declared() -> None:
    assert dict(PROVIDER_PROMPT_CACHE_FAMILY) == {
        "anthropic": "anthropic_messages",
        "anthropic_oauth": "anthropic_messages",
    }
    family = prompt_cache_family_for("anthropic_oauth")
    assert family is not None
    assert [(life.minutes, life.wire) for life in family.lifetimes] == [
        (5, "5m"),
        (60, "1h"),
    ]
    for undeclared in ("commandcode", "opencode", "open_router", "custom_x", ""):
        assert prompt_cache_family_for(undeclared) is None


@pytest.mark.parametrize("key", ALL_KEYS)
def test_each_field_sits_under_reasoning_with_the_rule_in_its_help(key: str) -> None:
    field = FIELD_BY_KEY[key]

    assert field.section_id == "reasoning"
    assert field.field_type == "select"
    assert not field.restart_required
    assert not field.advanced
    assert len(field.description) >= 20
    for phrase in ("5 min", "1 h", "at least", "longest", "1.25x", "2x"):
        assert phrase in field.description, phrase
    values = [
        option.value if isinstance(option, ConfigOptionSpec) else option
        for option in field.options
    ]
    assert values[-60:] == [str(minute) for minute in range(1, 61)]
    assert ("inherit" in values) == (key != "PROMPT_CACHE_TTL")
    assert "client" in values
    assert field.default == ("client" if key == "PROMPT_CACHE_TTL" else "inherit")


def test_a_lifetime_save_does_not_rebuild_providers() -> None:
    """Read by the door per request, never by a provider client."""

    assert update_affects_providers(ALL_KEYS) is False
