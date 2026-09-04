"""Tests for the legacy ``FCC_*`` → canonical ``MCC_*`` env-name aliases.

The new canonical env names are ``MCC_*``; the pre-6.40.0 ``FCC_*`` names are
still accepted as working aliases (via ``AliasChoices`` and the admin
``_OWNED_ENV_PREFIXES`` tuple) and log one deprecation line per process.
"""

import importlib
from pathlib import Path

from my_claude_code.config import settings


def test_fcc_open_browser_still_sets_the_field(monkeypatch) -> None:
    monkeypatch.setitem(settings.Settings.model_config, "env_file", ())
    monkeypatch.setenv("FCC_OPEN_BROWSER", "false")

    assert settings.Settings().open_admin_browser is False


def test_mcc_open_browser_wins_over_fcc_open_browser(monkeypatch) -> None:
    monkeypatch.setitem(settings.Settings.model_config, "env_file", ())
    monkeypatch.setenv("FCC_OPEN_BROWSER", "false")
    monkeypatch.setenv("MCC_OPEN_BROWSER", "true")

    assert settings.Settings().open_admin_browser is True


def test_legacy_env_names_log_one_deprecation_line_per_process(
    monkeypatch, caplog
) -> None:
    importlib.reload(settings)
    from my_claude_code.config import env_files

    # Reset the per-process dedupe so the assertion is deterministic.
    env_files._legacy_env_warnings.clear()
    monkeypatch.setenv("FCC_ENV_FILE", str(Path("/tmp/legacy.env")))

    with caplog.at_level("WARNING"):
        env_files.explicit_env_path()

    assert any(
        "FCC_ENV_FILE" in record.message and "MCC_ENV_FILE" in record.message
        for record in caplog.records
    )


def test_admin_persists_mcc_prefixed_keys(monkeypatch) -> None:
    """The ``_OWNED_ENV_PREFIXES`` trap: MCC_* keys must survive a Save."""
    from my_claude_code.config.admin import persistence

    assert "MCC_" in persistence._OWNED_ENV_PREFIXES
    assert "FCC_" in persistence._OWNED_ENV_PREFIXES
    # A populated MCC_ key is treated as project-owned and preserved.
    assert "MCC_SOMETHING".startswith(persistence._OWNED_ENV_PREFIXES)


def test_saving_retires_the_legacy_alias_line(tmp_path) -> None:
    """A Save must not leave the file holding both spellings of one setting.

    ``settings_env_aliases()`` keys on the first ``AliasChoices`` entry, so
    ``FCC_OPEN_BROWSER`` is not in the managed set; it survived only because it
    matches the ``FCC_`` owned prefix. The result was an ``.env`` with a
    ``FCC_OPEN_BROWSER`` line the user could still see and a
    ``MCC_OPEN_BROWSER`` line that silently outranked it.
    """
    from my_claude_code.config.admin import persistence

    env_file = tmp_path / ".env"
    env_file.write_text(
        "FCC_OPEN_BROWSER=false\nFCC_SMOKE_SOMETHING=keep-me\n", encoding="utf-8"
    )

    preserved = persistence.unmanaged_env_values(env_file)

    assert "FCC_OPEN_BROWSER" not in preserved
    # An FCC_* key that is nobody's alias is still project-owned and kept.
    assert preserved["FCC_SMOKE_SOMETHING"] == "keep-me"


def test_superseded_aliases_are_derived_not_listed() -> None:
    """The retired set comes from ``AliasChoices``, so it cannot go stale."""
    from my_claude_code.config.admin import persistence

    superseded = persistence.superseded_env_aliases()

    assert "FCC_OPEN_BROWSER" in superseded
    # The canonical name is never retired by itself.
    assert "MCC_OPEN_BROWSER" not in superseded
