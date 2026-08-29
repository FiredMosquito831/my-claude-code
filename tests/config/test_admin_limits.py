"""Limits are editable, and saving cannot delete or invent what it showed."""

import pytest

from my_claude_code.config.admin import sources as admin_sources
from my_claude_code.config.admin.manifest import FIELD_BY_KEY, FIELDS, SECTIONS
from my_claude_code.config.admin.persistence import (
    render_env_file,
    settings_env_aliases,
    target_values_with_updates,
    unmanaged_env_values,
)
from my_claude_code.config.admin.sources import dotenv_values_from_text
from my_claude_code.config.admin.validation import settings_from_values
from my_claude_code.config.admin.values import load_value_state, normalize_for_env
from my_claude_code.config.limits import range_for
from my_claude_code.config.settings import Settings


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Point both env layers at a scratch directory with nothing in them.

    Every test below is about what a Save writes, so the two files it reads --
    the managed one under HOME and the repo-local one beside the working
    directory -- have to be this test's files and nobody else's.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("FCC_ENV_FILE", raising=False)
    for key in FIELD_BY_KEY:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".fcc").mkdir()
    return tmp_path


# The six cards the old flat `limits` grid split into. The values partition
# every resilience field exactly once: a key in two cards, or in none, is the
# failure this mapping exists to make loud.
SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "budgets": (
        "MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT",
        "MAX_OUTPUT_TOKENS_CEILING",
        "MAX_OUTPUT_TOKENS_CONTEXT_MARGIN",
        "MAX_OUTPUT_TOKENS_CONTEXT_FLOOR",
        "REASONING_ANSWER_FLOOR_MAX",
    ),
    "deadlines": (
        "FALLBACK_FIRST_TOKEN_TIMEOUT",
        "FALLBACK_TOTAL_TIMEOUT",
        "FALLBACK_STALL_TIMEOUT",
        "FALLBACK_REASONING_ANSWER_TIMEOUT",
        "FALLBACK_ON_REASONING_ONLY",
        "STREAM_COMMIT_HOLDBACK_SECONDS",
        "HTTP_READ_TIMEOUT",
        "HTTP_WRITE_TIMEOUT",
        "HTTP_CONNECT_TIMEOUT",
        "SERVER_GRACEFUL_SHUTDOWN_SECONDS",
    ),
    "benching": (
        "FALLBACK_BENCH_ENABLED",
        "FALLBACK_BEHAVIOR",
        "FALLBACK_EJECT_WINDOW",
        "FALLBACK_EJECT_FAILURE_RATE",
        "FALLBACK_EJECT_MIN_SAMPLES",
        "FALLBACK_EJECT_AFTER_FAILURES",
        "FALLBACK_EJECT_SECONDS",
        "FALLBACK_RETRY_FIRST",
        "FALLBACK_COOLDOWN_STEP_OVER_FLOOR",
    ),
    "provider_retries": (
        "PROVIDER_RETRY_ATTEMPTS",
        "STREAM_EARLY_RETRY_ATTEMPTS",
        "STREAM_MIDSTREAM_RECOVERY_ATTEMPTS",
        "PROVIDER_RETRY_BACKOFF_BASE_SECONDS",
        "PROVIDER_RETRY_BACKOFF_MAX_SECONDS",
        "PROVIDER_RETRY_BACKOFF_JITTER_SECONDS",
        "PROVIDER_RATE_LIMIT",
        "PROVIDER_RATE_WINDOW",
        "PROVIDER_MAX_CONCURRENCY",
    ),
    "credential_health": (
        "RATE_LIMIT_COOLDOWN_SECONDS",
        "CREDENTIAL_LOCKOUT_TIERS",
    ),
    "request_log": (
        "REQUEST_LOG_ENABLED",
        "REQUEST_LOG_MAX_ROWS",
        "REQUEST_LOG_CAPTURE_BODIES",
        "REQUEST_LOG_COMPRESS_BODIES",
        "REQUEST_LOG_CAPTURE_IMAGES",
        "REQUEST_LOG_IMAGE_MAX_PIXELS",
        "REQUEST_LOG_TEXT_MAX_CHARS",
        "REQUEST_LOG_WIRE_BODY_MAX_CHARS",
        "REQUEST_LOG_COMPRESSION_LEVEL",
        "REQUEST_LOG_QUEUE_MAX_SIZE",
    ),
}

# Kept flat for the default-matching check, which does not care where a
# field renders -- only that the form and the code agree on its value.
LIMIT_KEYS = tuple(key for keys in SECTION_KEYS.values() for key in keys)
SECTION_KEY_PAIRS = tuple(
    (section, key) for section, keys in SECTION_KEYS.items() for key in keys
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


@pytest.mark.parametrize("section", sorted(SECTION_KEYS))
def test_every_resilience_section_exists(section: str) -> None:
    assert any(spec.section_id == section for spec in SECTIONS)


def test_the_limits_section_no_longer_exists() -> None:
    """One 37-field grid mixed six concerns; half-reverting the split is worse."""
    assert not any(section.section_id == "limits" for section in SECTIONS)
    assert not any(field.section_id == "limits" for field in FIELDS)


def test_log_level_lives_with_the_logging_flags() -> None:
    """How much the server logs is one card, not one field plus eight flags."""
    assert FIELD_BY_KEY["LOG_LEVEL"].section_id == "diagnostics"


def test_the_skip_kinds_field_stays_with_the_routes() -> None:
    """A routing decision belongs beside the chains it ends, and renders once."""
    assert FIELD_BY_KEY["FALLBACK_SKIP_KINDS"].section_id == "models"


def test_the_moved_fields_left_their_old_sections() -> None:
    """A field claimed by two sections would render on two pages."""
    runtime = {field.key for field in FIELDS if field.section_id == "runtime"}
    for key in (
        "HTTP_READ_TIMEOUT",
        "HTTP_WRITE_TIMEOUT",
        "HTTP_CONNECT_TIMEOUT",
        "PROVIDER_RATE_LIMIT",
        "PROVIDER_RATE_WINDOW",
        "PROVIDER_MAX_CONCURRENCY",
    ):
        assert key not in runtime
    models = {field.key for field in FIELDS if field.section_id == "models"}
    assert "FALLBACK_REASONING_ANSWER_TIMEOUT" not in models
    assert "FALLBACK_ON_REASONING_ONLY" not in models


def test_every_numeric_limit_publishes_its_range_once() -> None:
    """The bound is a field of its own now, not the last sentence of the help.

    Scoped by ``settings_attr`` rather than by field type: ``_with_range``
    keys off the attribute, and CREDENTIAL_LOCKOUT_TIERS is text with no range.
    """
    for field in FIELDS:
        if range_for(field.settings_attr) is None:
            continue
        assert field.range_hint, f"{field.key} publishes no range hint"
        assert "Accepts" not in field.description, (
            f"{field.key} still glues its range onto the end of its help text"
        )


@pytest.mark.parametrize(("section", "key"), SECTION_KEY_PAIRS)
def test_every_limit_is_editable_and_bound_to_a_real_setting(
    section: str, key: str
) -> None:
    field = FIELD_BY_KEY[key]
    assert field.section_id == section
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


# Six fields moved in from `runtime`, where the manifest deliberately mirrors
# what `.env.example` ships rather than the code default. That divergence is
# recorded, with reasons, in DEFAULTS_THAT_DIFFER_FROM_THE_CODE below, and
# checked by test_every_manifest_default_matches_the_settings_default -- which
# generalises this check and has the escape hatch this one does not.
DEFAULTS_OWNED_BY_THE_SHIPPED_TEMPLATE = (
    "PROVIDER_RATE_LIMIT",
    "PROVIDER_RATE_WINDOW",
    "HTTP_READ_TIMEOUT",
    "HTTP_WRITE_TIMEOUT",
    "HTTP_CONNECT_TIMEOUT",
)


@pytest.mark.parametrize(
    "key",
    tuple(
        key
        for key in LIMIT_KEYS + OPTIMIZER_KEYS
        if key not in DEFAULTS_OWNED_BY_THE_SHIPPED_TEMPLATE
    ),
)
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

    values, _ = target_values_with_updates({})
    rendered = render_env_file(
        values | {"REQUEST_LOG_MAX_ROWS": "500000"},
        preserved=unmanaged_env_values(env),
    )
    assert rendered.count("REQUEST_LOG_MAX_ROWS=") == 1


def test_retention_survives_a_round_trip_through_the_form() -> None:
    """The exact failure reported: a raised cap reverting to the default."""
    values, _ = target_values_with_updates({"REQUEST_LOG_MAX_ROWS": "500000"})
    assert values["REQUEST_LOG_MAX_ROWS"] == "500000"
    assert "REQUEST_LOG_MAX_ROWS=500000" in render_env_file(values)


@pytest.mark.parametrize("section", sorted(SECTION_KEYS))
def test_no_resilience_field_is_orphaned(section: str) -> None:
    """Every field in the section is listed above, so the list cannot rot."""
    declared = {f.key for f in FIELDS if f.section_id == section}
    assert declared == set(SECTION_KEYS[section])


# --------------------------------------------------------------------------
# A Save records choices. Everything below pins one half of that sentence.
# The bug these come from: the first Save of any field materialised every
# manifest default into the managed file as if the user had picked it, so
# FALLBACK_BENCH_ENABLED=false outlived the release that changed the default
# to true and silently disabled every Eject setting on that install.


def test_a_save_writes_only_what_was_set(isolated_config) -> None:
    values, warnings = target_values_with_updates({"REQUEST_LOG_MAX_ROWS": "500000"})

    assert values == {"REQUEST_LOG_MAX_ROWS": "500000"}
    assert warnings == ()

    rendered = render_env_file(values)
    value_lines = [
        line
        for line in rendered.splitlines()
        if line and not line.startswith("#") and "=" in line
    ]
    assert value_lines == ["REQUEST_LOG_MAX_ROWS=500000"]
    assert "# FALLBACK_BENCH_ENABLED= (default: true)" in rendered


def test_dotenv_sees_only_set_keys_in_the_rendered_file(isolated_config) -> None:
    """The placeholder has to be a comment to dotenv, not a key set to ""."""

    values, _ = target_values_with_updates({"LOG_LEVEL": "DEBUG"})
    parsed = dotenv_values_from_text(render_env_file(values))

    assert parsed == {"LOG_LEVEL": "DEBUG"}


def test_an_unset_field_follows_a_changed_default(isolated_config, monkeypatch) -> None:
    """The regression that started this: a default that could never move.

    A default materialised into the managed file outranks every layer below it
    forever, so an install that pressed Save once was pinned to whatever the
    shipped default was on that day -- which is how FALLBACK_BENCH_ENABLED
    stayed false through the release that made it true. With nothing written,
    changing the shipped default changes what the field reports.
    """

    key = "FALLBACK_BENCH_ENABLED"
    monkeypatch.setattr(
        admin_sources, "load_env_template_or_empty", lambda: f"{key}=false\n"
    )
    assert load_value_state()[key] == {"value": "false", "source": "template"}

    values, _ = target_values_with_updates({})
    assert key not in values

    monkeypatch.setattr(
        admin_sources, "load_env_template_or_empty", lambda: f"{key}=true\n"
    )
    assert load_value_state()[key]["value"] == "true"

    settings, errors = settings_from_values(values)
    assert errors == []
    assert settings is not None
    # Nothing is stored for it, so the running server uses the code default.
    assert settings.fallback_bench_enabled is True


def test_blanking_unsets_when_the_repo_env_is_silent(isolated_config) -> None:
    managed = isolated_config / ".fcc" / ".env"
    managed.write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")

    values, warnings = target_values_with_updates({"LOG_LEVEL": ""})

    assert "LOG_LEVEL" not in values
    assert warnings == ()
    assert "# LOG_LEVEL= (default: INFO)" in render_env_file(values)


def test_blanking_masks_a_repo_env_value_when_the_type_accepts_it(
    isolated_config,
) -> None:
    """MODEL_OPUS is an optional string: "" is how you say "no override"."""

    (isolated_config / ".env").write_text(
        "MODEL_OPUS=open_router/x/y\n", encoding="utf-8"
    )
    managed = isolated_config / ".fcc" / ".env"
    managed.write_text("MODEL_OPUS=open_router/a/b\n", encoding="utf-8")

    values, warnings = target_values_with_updates({"MODEL_OPUS": ""})

    assert values["MODEL_OPUS"] == ""
    assert warnings == ()
    assert "MODEL_OPUS=\n" in render_env_file(values)


def test_blanking_a_boolean_the_repo_env_sets_warns_instead(isolated_config) -> None:
    """A bool has no blank validator: KEY= would stop the server starting."""

    (isolated_config / ".env").write_text(
        "FALLBACK_BENCH_ENABLED=false\n", encoding="utf-8"
    )
    managed = isolated_config / ".fcc" / ".env"
    managed.write_text("FALLBACK_BENCH_ENABLED=true\n", encoding="utf-8")

    values, warnings = target_values_with_updates({"FALLBACK_BENCH_ENABLED": ""})

    assert "FALLBACK_BENCH_ENABLED" not in values
    assert warnings == (
        "FALLBACK_BENCH_ENABLED: cannot be blanked while the repo .env sets it; "
        "remove it there",
    )


def test_a_value_equal_to_the_default_is_still_recorded(isolated_config) -> None:
    """An explicit choice has to survive a later change of that default."""

    field = FIELD_BY_KEY["FALLBACK_BENCH_ENABLED"]
    values, _ = target_values_with_updates({"FALLBACK_BENCH_ENABLED": field.default})

    assert values == {"FALLBACK_BENCH_ENABLED": field.default}


# The managed file now prints ``# KEY= (default: X)`` for every field nobody
# set, so X has to be true. These are the fields where the manifest default is
# deliberately NOT the bare code default, each with the reason. The test fails
# if a listed field starts matching, so the list cannot rot.
_SHIPPED_TEMPLATE_VALUE = (
    "the manifest shows the value shipped in .env.example -- the configuration "
    "this project recommends -- rather than the barer library default"
)
_BUILT_IN_ENDPOINT = (
    "the code default is empty, meaning 'use the provider's own endpoint'; the "
    "manifest names that endpoint so the field is not an unexplained empty box"
)
DEFAULTS_THAT_DIFFER_FROM_THE_CODE = {
    "VERTEX_LOCATION": "the manifest leaves it blank; .env.example supplies it",
    "ANTHROPIC_UPSTREAM_BASE_URL": _BUILT_IN_ENDPOINT,
    "ANTHROPIC_OAUTH_UPSTREAM_BASE_URL": _BUILT_IN_ENDPOINT,
    "CHATGPT_OAUTH_BASE_URL": _BUILT_IN_ENDPOINT,
    "ALIBABA_BASE_URL": _BUILT_IN_ENDPOINT,
    "ALIBABA_CN_BASE_URL": _BUILT_IN_ENDPOINT,
    "ALIBABA_CODING_BASE_URL": _BUILT_IN_ENDPOINT,
    "ALIBABA_CODING_CN_BASE_URL": _BUILT_IN_ENDPOINT,
    "VERTEX_BASE_URL": _SHIPPED_TEMPLATE_VALUE,
    "PROVIDER_RATE_LIMIT": _SHIPPED_TEMPLATE_VALUE,
    "PROVIDER_RATE_WINDOW": _SHIPPED_TEMPLATE_VALUE,
    "HTTP_READ_TIMEOUT": _SHIPPED_TEMPLATE_VALUE,
    "HTTP_WRITE_TIMEOUT": _SHIPPED_TEMPLATE_VALUE,
    "HTTP_CONNECT_TIMEOUT": _SHIPPED_TEMPLATE_VALUE,
    "VOICE_NOTE_ENABLED": _SHIPPED_TEMPLATE_VALUE,
    "WHISPER_DEVICE": _SHIPPED_TEMPLATE_VALUE,
    "WHISPER_MODEL": _SHIPPED_TEMPLATE_VALUE,
    "ANTHROPIC_AUTH_TOKEN": (
        "the code default is the shared local proxy password; a credential "
        "field ships showing nothing rather than pre-filling it"
    ),
}


def _defaults_agree(manifest_default: str, actual: object) -> bool:
    if actual is None:
        return manifest_default == ""
    if isinstance(actual, bool):
        return manifest_default == ("true" if actual else "false")
    if isinstance(actual, int | float):
        try:
            return float(manifest_default) == float(actual)
        except ValueError:
            return False
    return manifest_default == normalize_for_env(actual)


@pytest.mark.parametrize("key", [field.key for field in FIELDS if field.settings_attr])
def test_every_manifest_default_matches_the_settings_default(
    key: str, isolated_config
) -> None:
    """Generalises the limits-only check to every field bound to a setting."""

    field = FIELD_BY_KEY[key]
    attr = field.settings_attr
    assert attr is not None
    # Built the way the admin layer builds it: no dotenv files, no values, so
    # every attribute is the code default and nothing else.
    settings, errors = settings_from_values({})
    assert errors == []
    assert settings is not None
    agree = _defaults_agree(field.default, getattr(settings, attr))
    reason = DEFAULTS_THAT_DIFFER_FROM_THE_CODE.get(key)
    if reason is None:
        assert agree, (
            f"{key} shows a default the code does not use; fix the manifest or "
            "record the reason in DEFAULTS_THAT_DIFFER_FROM_THE_CODE"
        )
        return
    assert not agree, (
        f"{key} now matches the code default -- remove it from "
        f"DEFAULTS_THAT_DIFFER_FROM_THE_CODE ({reason})"
    )
