from pathlib import Path

import pytest

from my_claude_code.config.env_migrations import (
    HUGGINGFACE_API_KEY_ENV,
    HUGGINGFACE_TOKEN_MIGRATION,
    LEGACY_HUGGINGFACE_TOKEN_ENV,
    REASONING_MIGRATIONS,
    env_text_needs_migration,
    explicit_env_file_migration_warning,
    migrate_env_key_in_file,
    migrate_env_key_in_text,
    migrate_owned_env_files,
)


def test_migrate_env_key_in_text_renames_legacy_hf_token() -> None:
    text = "# comment\nHF_TOKEN=old-token\nMODEL=nvidia_nim/model\n"

    migrated, changed = migrate_env_key_in_text(text, HUGGINGFACE_TOKEN_MIGRATION)

    assert changed is True
    assert migrated == (
        "# comment\nHUGGINGFACE_API_KEY=old-token\nMODEL=nvidia_nim/model\n"
    )


def test_migrate_env_key_in_text_preserves_existing_huggingface_api_key() -> None:
    text = "HF_TOKEN=old-token\nHUGGINGFACE_API_KEY=new-token\n"

    migrated, changed = migrate_env_key_in_text(text, HUGGINGFACE_TOKEN_MIGRATION)

    assert changed is False
    assert migrated == text


def test_migrate_env_key_in_text_ignores_comments() -> None:
    text = "# HF_TOKEN=old-token\n"

    migrated, changed = migrate_env_key_in_text(text, HUGGINGFACE_TOKEN_MIGRATION)

    assert changed is False
    assert migrated == text
    assert not env_text_needs_migration(text, HUGGINGFACE_TOKEN_MIGRATION)


def test_migrate_env_key_in_file_rewrites_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("export HF_TOKEN = quoted-token\n", encoding="utf-8")

    assert migrate_env_key_in_file(env_file, HUGGINGFACE_TOKEN_MIGRATION) is True

    assert env_file.read_text(encoding="utf-8") == (
        "export HUGGINGFACE_API_KEY = quoted-token\n"
    )


def test_migrate_owned_env_files_rewrites_repo_and_managed_env(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    managed = tmp_path / ".fcc" / ".env"
    managed.parent.mkdir()
    (repo / ".env").write_text("HF_TOKEN=repo-token\n", encoding="utf-8")
    managed.write_text("HF_TOKEN=managed-token\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    migrated = migrate_owned_env_files()

    assert migrated == (repo / ".env", managed)
    assert (repo / ".env").read_text(encoding="utf-8") == (
        "HUGGINGFACE_API_KEY=repo-token\n"
    )
    assert managed.read_text(encoding="utf-8") == (
        "HUGGINGFACE_API_KEY=managed-token\n"
    )


def test_explicit_env_file_migration_warning_does_not_rewrite(
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "custom.env"
    explicit.write_text("HF_TOKEN=explicit-token\n", encoding="utf-8")

    warning = explicit_env_file_migration_warning({"FCC_ENV_FILE": str(explicit)})

    assert warning is not None
    assert str(explicit) in warning
    assert LEGACY_HUGGINGFACE_TOKEN_ENV in warning
    assert HUGGINGFACE_API_KEY_ENV in warning
    assert explicit.read_text(encoding="utf-8") == "HF_TOKEN=explicit-token\n"


def test_reasoning_migrations_rename_and_map_boolean_values() -> None:
    text = (
        "ENABLE_MODEL_THINKING=false\n"
        "ENABLE_FABLE_THINKING=true\n"
        "ENABLE_OPUS_THINKING=\n"
    )

    for migration in REASONING_MIGRATIONS:
        text, _ = migrate_env_key_in_text(text, migration)

    assert text == (
        "REASONING_POLICY=off\nREASONING_FABLE=client\nREASONING_OPUS=inherit\n"
    )


@pytest.mark.parametrize(
    ("legacy_value", "expected"),
    [
        ("1", "client"),
        ("TRUE", "client"),
        ("t", "client"),
        ("on", "client"),
        ("yes", "client"),
        ("y", "client"),
        ("0", "off"),
        ("FALSE", "off"),
        ("f", "off"),
        ("off", "off"),
        ("no", "off"),
        ("n", "off"),
    ],
)
def test_reasoning_migration_accepts_every_legacy_boolean_spelling(
    legacy_value: str,
    expected: str,
) -> None:
    text = f"ENABLE_MODEL_THINKING={legacy_value}\n"

    migrated, changed = migrate_env_key_in_text(text, REASONING_MIGRATIONS[0])

    assert changed is True
    assert migrated == f"REASONING_POLICY={expected}\n"


def test_migrate_env_key_in_file_stages_and_swaps_atomically(
    tmp_path: Path, monkeypatch
) -> None:
    from my_claude_code.config import env_migrations

    env_file = tmp_path / ".env"
    env_file.write_text("HF_TOKEN=secret-token\n", encoding="utf-8")
    real_replace = env_migrations.os.replace
    calls: list[tuple[Path, Path]] = []

    def spy(src: Path, dst: Path) -> None:
        calls.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(env_migrations.os, "replace", spy)

    assert migrate_env_key_in_file(env_file, HUGGINGFACE_TOKEN_MIGRATION) is True

    assert len(calls) == 1
    src, dst = calls[0]
    assert dst.name == ".env"
    assert src.name == ".env.fcc-tmp"
    assert src.parent == tmp_path
    assert not src.exists()
    assert env_file.read_text(encoding="utf-8") == "HUGGINGFACE_API_KEY=secret-token\n"


def test_migrate_env_key_in_file_skips_unterminated_multiline_quotes(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    original = 'ENABLE_MODEL_THINKING=true\nNOTE="never closed\n'
    env_file.write_text(original, encoding="utf-8")

    assert migrate_env_key_in_file(env_file, REASONING_MIGRATIONS[0]) is False
    assert env_file.read_text(encoding="utf-8") == original


def test_migrate_env_key_in_file_allows_well_closed_multiline_quotes(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        'ENABLE_MODEL_THINKING=true\nNOTE="spans\ntwo lines"\n',
        encoding="utf-8",
    )

    assert migrate_env_key_in_file(env_file, REASONING_MIGRATIONS[0]) is True
    migrated = env_file.read_text(encoding="utf-8")
    assert migrated.startswith("REASONING_POLICY=")
    assert 'NOTE="spans\ntwo lines"' in migrated
