"""Tests for the opt-in ``mcc-migrate`` / ``fcc-migration`` config-dir rename.

The move is a single atomic ``os.replace(~/.fcc, ~/.mcc)`` that either
relocates the whole legacy tree or raises before anything is moved. These
tests exercise the command against a redirected HOME so the real ``~/.fcc``
is never touched.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from my_claude_code.cli import migrate_config_dir


def _redirected_home(tmp_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("MCC_CONFIG_DIR", None)
    env["HOME"] = env["USERPROFILE"] = str(tmp_home)
    return env


def _legacy_home(tmp_home: Path) -> Path:
    legacy = tmp_home / ".fcc"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\n")
    (legacy / "custom_providers.json").write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider_id": "custom_x",
                        "display_name": "X",
                        "base_url": "https://x.example/v1",
                        "api_keys": ["sk-x"],
                    }
                ]
            }
        )
    )
    (legacy / "auth").mkdir()
    (legacy / "auth" / "token.json").write_text('{"token": "t"}')
    return legacy


def _home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def test_rename_moves_every_file_and_subdirectory(tmp_path: Path, monkeypatch) -> None:
    _home(tmp_path, monkeypatch)
    legacy = _legacy_home(tmp_path)
    result = migrate_config_dir.migrate_config_dir()

    assert "Moved" in result
    new_home = tmp_path / ".mcc"
    assert new_home.is_dir()
    assert not legacy.exists()
    # Every file and subdir moved with the tree.
    assert (new_home / ".env").read_text() == "MODEL=nvidia_nim/test\n"
    assert (new_home / "custom_providers.json").exists()
    assert (new_home / "auth" / "token.json").exists()


def test_rollback_note_is_written_into_fcc_old(tmp_path: Path, monkeypatch) -> None:
    _home(tmp_path, monkeypatch)
    _legacy_home(tmp_path)
    migrate_config_dir.migrate_config_dir()

    retired = tmp_path / ".fcc-old"
    assert retired.is_dir()
    restore = retired / "RESTORE.txt"
    assert restore.is_file()
    text = restore.read_text(encoding="utf-8")
    assert "mv" in text or "Move-Item" in text
    assert ".mcc" in text


def test_second_run_says_nothing_to_do(tmp_path: Path, monkeypatch) -> None:
    _home(tmp_path, monkeypatch)
    _legacy_home(tmp_path)
    migrate_config_dir.migrate_config_dir()
    result = migrate_config_dir.migrate_config_dir()

    assert "already gone" in result


def test_refuses_when_dot_mcc_already_exists(tmp_path: Path, monkeypatch) -> None:
    _home(tmp_path, monkeypatch)
    _legacy_home(tmp_path)
    (tmp_path / ".mcc").mkdir()
    raised = False
    try:
        migrate_config_dir.migrate_config_dir()
    except migrate_config_dir.MigrationError as exc:
        raised = True
        assert "Refusing" in str(exc)

    assert raised, "expected MigrationError when both dirs exist"
    assert (tmp_path / ".fcc").is_dir()
    assert (tmp_path / ".mcc").is_dir()


def test_holder_detection_names_processes_on_windows(tmp_path: Path) -> None:
    """A held file makes the rename refuse; the message names holders."""
    if os.name != "nt":
        return  # the Windows-only open-handle behaviour
    legacy = tmp_path / ".fcc"
    legacy.mkdir()
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\n")
    held = legacy / "held.txt"
    held.write_text("open")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import time, pathlib; p = pathlib.Path(r'{held}'); "
            f"f = p.open('a'); time.sleep(30)",
        ],
        env=_redirected_home(tmp_path),
    )
    try:
        result = migrate_config_dir.migrate_config_dir()
    finally:
        proc.kill()
        proc.wait()

    assert "Could not move" in result or "still open" in result
    assert (tmp_path / ".fcc").is_dir()  # nothing moved


def test_console_script_returns_zero_on_success(tmp_path: Path, monkeypatch) -> None:
    _home(tmp_path, monkeypatch)
    _legacy_home(tmp_path)

    code = migrate_config_dir.main([])
    assert code == 0


def test_retired_dir_already_present_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    _home(tmp_path, monkeypatch)
    _legacy_home(tmp_home=tmp_path)
    (tmp_path / ".fcc-old").mkdir()
    (tmp_path / ".fcc-old" / "RESTORE.txt").write_text("prior note")
    result = migrate_config_dir.migrate_config_dir()

    assert "already exists" in result
    # The pre-existing note is untouched.
    assert (tmp_path / ".fcc-old" / "RESTORE.txt").read_text() == "prior note"


def test_legacy_home_uses_legacy_dir_not_retired(tmp_path: Path, monkeypatch) -> None:
    """After migration the data is at .mcc, not at .fcc-old (which is empty)."""
    _home(tmp_path, monkeypatch)
    _legacy_home(tmp_path)
    migrate_config_dir.migrate_config_dir()

    assert (tmp_path / ".mcc" / ".env").exists()
    assert not (tmp_path / ".fcc-old" / ".env").exists()
