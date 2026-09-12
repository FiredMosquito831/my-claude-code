"""The ``FCC_*`` env-name aliases are gone (7.0.0).

Until 6.84.0 the pre-6.40.0 ``FCC_*`` names were accepted as working aliases:
``FCC_OPEN_BROWSER`` through ``AliasChoices`` on ``Settings.open_admin_browser``,
``FCC_ENV_FILE`` through ``config.env_files``, and any other ``FCC_*`` key
through the owned-prefix rule in ``config.admin.persistence``. 7.0.0 removed all
three. These tests are the proof that each one is inert -- and that the removal
is loud rather than silent: a leftover ``FCC_*`` in the process environment is
named in a warning, and a leftover ``FCC_*`` in the managed ``.env`` is renamed
by ``config.legacy_env_rewrite`` before anything reads the file.
"""

import importlib

from my_claude_code.config import settings


def test_mcc_open_browser_sets_the_field(monkeypatch) -> None:
    monkeypatch.setitem(settings.Settings.model_config, "env_file", ())
    monkeypatch.setenv("MCC_OPEN_BROWSER", "false")

    assert settings.Settings().open_admin_browser is False


def test_fcc_open_browser_no_longer_sets_the_field(monkeypatch) -> None:
    """The alias is removed: the old name must not reach the field at all."""

    monkeypatch.setitem(settings.Settings.model_config, "env_file", ())
    monkeypatch.delenv("MCC_OPEN_BROWSER", raising=False)
    monkeypatch.setenv("FCC_OPEN_BROWSER", "false")

    assert settings.Settings().open_admin_browser is True


def test_fcc_env_file_no_longer_selects_an_explicit_dotenv(monkeypatch) -> None:
    from my_claude_code.config import env_files

    monkeypatch.delenv("MCC_ENV_FILE", raising=False)
    monkeypatch.setenv("FCC_ENV_FILE", "/tmp/legacy.env")

    assert env_files.explicit_env_path() is None


def test_a_leftover_fcc_env_name_is_named_in_a_warning(monkeypatch, caplog) -> None:
    """Removing an alias silently is how a setting stops working unnoticed."""

    importlib.reload(settings)
    monkeypatch.setitem(settings.Settings.model_config, "env_file", ())
    monkeypatch.setenv("FCC_OPEN_BROWSER", "false")

    with caplog.at_level("WARNING"):
        settings.Settings()

    assert any(
        "FCC_OPEN_BROWSER" in record.message and "MCC_OPEN_BROWSER" in record.message
        for record in caplog.records
    )


def test_admin_owns_only_the_mcc_prefix() -> None:
    """The ``_OWNED_ENV_PREFIXES`` trap, now with one prefix."""
    from my_claude_code.config.admin import persistence

    assert persistence._OWNED_ENV_PREFIXES == ("MCC_",)
    assert "MCC_SOMETHING".startswith(persistence._OWNED_ENV_PREFIXES)
    assert not "FCC_SOMETHING".startswith(persistence._OWNED_ENV_PREFIXES)


def test_an_mcc_prefixed_key_still_survives_a_save(tmp_path) -> None:
    from my_claude_code.config.admin import persistence

    env_file = tmp_path / ".env"
    env_file.write_text(
        "MCC_OPEN_BROWSER=false\nMCC_SMOKE_SOMETHING=keep-me\n", encoding="utf-8"
    )

    preserved = persistence.unmanaged_env_values(env_file)

    # MCC_OPEN_BROWSER is a managed alias, so the render writes it itself.
    assert "MCC_OPEN_BROWSER" not in preserved
    assert preserved["MCC_SMOKE_SOMETHING"] == "keep-me"


def test_superseded_aliases_are_derived_not_listed() -> None:
    """The retired set comes from ``AliasChoices``, so it cannot go stale.

    Empty in 7.0.0 -- no field declares more than one choice any more -- but the
    rule has to keep firing for the next alias anybody adds, so the derivation
    is exercised against a synthetic two-choice field rather than asserted away.
    """
    from pydantic import AliasChoices
    from pydantic.fields import FieldInfo

    from my_claude_code.config.admin import persistence

    assert persistence.superseded_env_aliases() == frozenset()

    field = FieldInfo(annotation=str)
    field.validation_alias = AliasChoices("MCC_NEW_NAME", "MCC_OLD_NAME")
    original = dict(persistence.Settings.model_fields)
    try:
        persistence.Settings.model_fields["_probe"] = field
        assert "MCC_OLD_NAME" in persistence.superseded_env_aliases()
        assert "MCC_NEW_NAME" not in persistence.superseded_env_aliases()
    finally:
        persistence.Settings.model_fields.clear()
        persistence.Settings.model_fields.update(original)
