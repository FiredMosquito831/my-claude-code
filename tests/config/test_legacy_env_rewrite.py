"""The one-time ``FCC_*`` -> ``MCC_*`` rewrite of a managed ``.env`` (7.0.0).

The removal of the alias surface has exactly one way to lose a user's setting:
a ``.env`` that still spells a key the old way. These tests pin the rewrite that
prevents it -- including the enumerated list of every ``FCC_*`` name the product
itself ever read, so a name cannot quietly fall off the list.
"""

from datetime import UTC, datetime
from pathlib import Path

from my_claude_code.config.legacy_env_rewrite import (
    PINNED_LEGACY_ENV_KEYS,
    canonical_name,
    rewrite_legacy_env_keys,
)

_NOW = datetime(2026, 9, 13, 4, 5, 6, tzinfo=UTC)


def test_the_pinned_alias_list_is_the_one_from_the_pre_7_0_0_sources() -> None:
    """Enumerated from settings.py, env_files.py and the owned-prefix rule.

    Removing an entry here means a name the product used to read is no longer
    rewritten, so a user's configured value stops taking effect on upgrade. Any
    change to this tuple belongs in the release notes.
    """

    assert PINNED_LEGACY_ENV_KEYS == (
        "FCC_ENV_FILE",
        "FCC_OPEN_BROWSER",
        "FCC_SMOKE_TARGETS",
    )
    for key in PINNED_LEGACY_ENV_KEYS:
        assert canonical_name(key) == "MCC_" + key.removeprefix("FCC_")


def test_every_pinned_key_is_renamed_with_a_byte_identical_value(
    tmp_path: Path,
) -> None:
    env = tmp_path / ".env"
    lines = [f"{key}=value-for-{key}" for key in PINNED_LEGACY_ENV_KEYS]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")

    renamed = rewrite_legacy_env_keys(env, now=_NOW)

    assert {old for old, _ in renamed} == set(PINNED_LEGACY_ENV_KEYS)
    text = env.read_text(encoding="utf-8")
    for key in PINNED_LEGACY_ENV_KEYS:
        assert f"{canonical_name(key)}=value-for-{key}" in text
        assert f"\n{key}=" not in "\n" + text


def test_a_backup_of_the_file_as_it_was_is_written_beside_it(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    original = "FCC_OPEN_BROWSER=false\n"
    env.write_text(original, encoding="utf-8")

    rewrite_legacy_env_keys(env, now=_NOW)

    backup = tmp_path / ".env.bak-20260913-040506"
    assert backup.read_text(encoding="utf-8") == original


def test_one_log_line_names_each_renamed_key_and_no_value(tmp_path, caplog) -> None:
    env = tmp_path / ".env"
    env.write_text("FCC_OPEN_BROWSER=super-secret-value\n", encoding="utf-8")

    with caplog.at_level("INFO"):
        rewrite_legacy_env_keys(env, now=_NOW)

    messages = [record.message for record in caplog.records]
    assert any(
        "FCC_OPEN_BROWSER" in message and "MCC_OPEN_BROWSER" in message
        for message in messages
    )
    assert not any("super-secret-value" in message for message in messages)


def test_the_second_run_is_a_no_op(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("FCC_OPEN_BROWSER=false\n", encoding="utf-8")

    assert rewrite_legacy_env_keys(env, now=_NOW)
    after_first = env.read_text(encoding="utf-8")

    assert rewrite_legacy_env_keys(env, now=_NOW) == ()
    assert env.read_text(encoding="utf-8") == after_first
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".env")) == [
        ".env",
        ".env.bak-20260913-040506",
    ]


def test_quoting_comments_export_prefixes_and_crlf_all_survive(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        'export FCC_OPEN_BROWSER="false"  # chosen 2026-01-01\r\n'
        "MODEL=deepseek/deepseek-chat\r\n",
        encoding="utf-8",
        newline="",
    )

    rewrite_legacy_env_keys(env, now=_NOW)

    with env.open("r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    assert text == (
        'export MCC_OPEN_BROWSER="false"  # chosen 2026-01-01\r\n'
        "MODEL=deepseek/deepseek-chat\r\n"
    )


def test_a_commented_out_legacy_line_is_not_a_setting(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# FCC_OPEN_BROWSER=false\n", encoding="utf-8")

    assert rewrite_legacy_env_keys(env, now=_NOW) == ()
    assert env.read_text(encoding="utf-8") == "# FCC_OPEN_BROWSER=false\n"


def test_a_legacy_key_shadowed_by_its_canonical_name_is_left_alone(
    tmp_path, caplog
) -> None:
    """Renaming would leave two live lines for one setting; dropping would lose
    a value. Leaving it and saying so is the only honest third option."""

    env = tmp_path / ".env"
    original = "MCC_OPEN_BROWSER=true\nFCC_OPEN_BROWSER=false\n"
    env.write_text(original, encoding="utf-8")

    with caplog.at_level("WARNING"):
        renamed = rewrite_legacy_env_keys(env, now=_NOW)

    assert renamed == ()
    assert env.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".env.bak-20260913-040506").exists()
    assert any("FCC_OPEN_BROWSER" in record.message for record in caplog.records)


def test_an_unterminated_quote_refuses_rather_than_writing_a_phantom_key(
    tmp_path, caplog
) -> None:
    env = tmp_path / ".env"
    original = 'FCC_OPEN_BROWSER=false\nSOMETHING="never closed\n'
    env.write_text(original, encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert rewrite_legacy_env_keys(env, now=_NOW) == ()

    assert env.read_text(encoding="utf-8") == original


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    assert rewrite_legacy_env_keys(tmp_path / "nope.env", now=_NOW) == ()


def test_a_file_with_nothing_legacy_is_untouched(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("MCC_OPEN_BROWSER=true\nPORT=8082\n", encoding="utf-8")

    assert rewrite_legacy_env_keys(env, now=_NOW) == ()
    assert [p for p in tmp_path.iterdir() if p.name.startswith(".env")] == [env]


def test_first_start_rewrites_the_managed_env_once(tmp_path, monkeypatch) -> None:
    """End to end through the code path a real first start takes."""

    from my_claude_code.cli import first_start
    from my_claude_code.config import paths

    home = tmp_path / "home"
    (home / ".mcc").mkdir(parents=True)
    (home / ".mcc" / ".env").write_text(
        "FCC_OPEN_BROWSER=false\nPORT=8199\n", encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("MCC_CONFIG_DIR", raising=False)
    paths.reset_config_dir_cache()

    notice = first_start.ensure_config_home()

    assert "MCC_OPEN_BROWSER" in notice
    text = (home / ".mcc" / ".env").read_text(encoding="utf-8")
    assert "MCC_OPEN_BROWSER=false" in text
    assert "FCC_OPEN_BROWSER" not in text
    assert any(p.name.startswith(".env.bak-") for p in (home / ".mcc").iterdir())

    paths.reset_config_dir_cache()
    assert first_start.ensure_config_home() == ""
