"""Tests for the opt-in ``mcc-migrate`` / ``fcc-migration`` config-dir rename.

The move is a single atomic ``os.replace(~/.fcc, ~/.mcc)`` that either
relocates the whole legacy tree or raises before anything is moved. These
tests exercise the command against a redirected HOME so the real ``~/.fcc``
is never touched.
"""

import json
import os
import platform
import subprocess
import sys
import time
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
    """Point the command at ``tmp_path`` and silence the liveness probe.

    The probe knocks on the port the legacy ``.env`` configures, falling back
    to the ``Settings`` default -- which on a developer machine is the port
    their own MCC server is listening on. Without this stub the whole file
    refuses to migrate because of a real server that has nothing to do with the
    test. ``test_refuses_while_an_mcc_server_answers`` exercises the probe
    itself instead of stubbing it.
    """

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(migrate_config_dir, "_mcc_is_running", lambda home: "")


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


def test_holder_detection_names_processes_on_windows(
    tmp_path: Path, monkeypatch
) -> None:
    """A held file makes the rename refuse; the message names holders."""
    if os.name != "nt":
        return  # the Windows-only open-handle behaviour
    # Without this the command resolves the developer's REAL home and renames
    # their live ``~/.fcc``. Every other test in this file patches it; this one
    # did not, and that is exactly the class of bug the session-scoped
    # ``_never_touch_the_real_home`` guard in ``tests/conftest.py`` now catches.
    _home(tmp_path, monkeypatch)
    legacy = tmp_path / ".fcc"
    legacy.mkdir()
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\n")
    held = legacy / "held.txt"
    held.write_text("open")
    # The child signals that the handle is actually open before we try the
    # rename. Without the handshake the test raced the interpreter's startup
    # and the rename usually won, so the case only "failed correctly" by
    # accident -- and against the real home, where an unrelated MCC process
    # happened to be holding a file, it passed for the wrong reason entirely.
    ready = tmp_path / "held.ready"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import time, pathlib; p = pathlib.Path(r'{held}'); "
            f"f = p.open('a'); pathlib.Path(r'{ready}').write_text('1'); "
            f"time.sleep(30)",
        ],
        env=_redirected_home(tmp_path),
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "the holding subprocess never opened the file"
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


# ----------------------------------------------------------- refusal guards


def test_refuses_while_an_mcc_server_answers(tmp_path: Path, monkeypatch) -> None:
    """A live server must stop the rename on every OS, not only on Windows.

    On POSIX ``os.rename`` succeeds with every handle in the directory still
    open, and the running server -- which cached ``~/.fcc/logs`` at startup --
    then recreates the legacy home behind the migration. The docs promised a
    refusal unconditionally; this probe is what makes that true.
    """
    _home(tmp_path, monkeypatch)
    _legacy_home(tmp_path)
    monkeypatch.setattr(
        migrate_config_dir,
        "_mcc_is_running",
        lambda home: "an MCC server is answering on http://127.0.0.1:8082/health",
    )

    raised = None
    try:
        migrate_config_dir.migrate_config_dir()
    except migrate_config_dir.MigrationError as exc:
        raised = str(exc)

    assert raised is not None
    assert "Refusing to migrate" in raised
    assert "Stop the server and the tray first" in raised
    assert (tmp_path / ".fcc").is_dir()
    assert not (tmp_path / ".mcc").exists()


def test_liveness_probe_reports_a_held_tray_lock(tmp_path: Path, monkeypatch) -> None:
    """A ``desktop.lock`` we cannot acquire means the tray is running."""
    from my_claude_code.core.interprocess_lock import InterprocessFileLock

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    legacy = _legacy_home(tmp_path)
    lock_path = legacy / "desktop.lock"
    holder = InterprocessFileLock(lock_path)
    assert holder.acquire()
    try:
        reason = migrate_config_dir._mcc_is_running(legacy)
    finally:
        holder.release()

    assert "desktop tray" in reason


def test_liveness_probe_never_creates_the_lock_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Probing must not write into the directory it is about to rename."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    legacy = _legacy_home(tmp_path)
    # No port in the .env and no server: the probe falls through to the
    # Settings default, which nothing in this test is listening on.
    monkeypatch.setattr(migrate_config_dir, "_configured_port", lambda home: None)

    assert migrate_config_dir._mcc_is_running(legacy) == ""
    assert not (legacy / "desktop.lock").exists()


def test_configured_port_comes_from_the_legacy_env(tmp_path: Path) -> None:
    legacy = tmp_path / ".fcc"
    legacy.mkdir()
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\nPORT=18099\n")

    assert migrate_config_dir._configured_port(legacy) == 18099


def test_configured_port_falls_back_to_the_settings_default(tmp_path: Path) -> None:
    """No ``PORT`` line means the server would be on the shipped default."""
    from my_claude_code.config.settings import Settings

    legacy = tmp_path / ".fcc"
    legacy.mkdir()
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\n")

    assert (
        migrate_config_dir._configured_port(legacy)
        == Settings.model_fields["port"].default
    )


def test_a_new_home_appearing_late_is_refused(tmp_path: Path, monkeypatch) -> None:
    """The ``exists()`` -> ``os.replace`` window is re-checked, not assumed.

    POSIX ``rename`` silently replaces an *empty* target directory, so a
    ``~/.mcc`` created after the first check would disappear without a word.
    """
    _home(tmp_path, monkeypatch)
    _legacy_home(tmp_path)

    real_is_running = migrate_config_dir._mcc_is_running

    def create_new_home_then_pass(home):
        (tmp_path / ".mcc").mkdir()
        return ""

    monkeypatch.setattr(
        migrate_config_dir, "_mcc_is_running", create_new_home_then_pass
    )
    try:
        raised = None
        try:
            migrate_config_dir.migrate_config_dir()
        except migrate_config_dir.MigrationError as exc:
            raised = str(exc)
    finally:
        migrate_config_dir._mcc_is_running = real_is_running

    assert raised is not None
    assert "appeared while this command was running" in raised
    assert (tmp_path / ".fcc" / ".env").is_file()


def test_the_command_takes_no_force_flag() -> None:
    """``--force`` was accepted everywhere and implemented nowhere.

    A refusal that advertises an escape hatch which does nothing is worse than
    a refusal that does not. The flag is gone from the signature; ``main``
    ignores unknown arguments rather than pretending.
    """
    import inspect

    signature = inspect.signature(migrate_config_dir.migrate_config_dir)
    assert list(signature.parameters) == []

    from my_claude_code.api import admin_routes

    assert not hasattr(admin_routes, "MigrateConfigDirPayload")


# ------------------------------------------------------- holder attribution


def test_windows_holders_are_mcc_processes_not_every_interpreter(monkeypatch) -> None:
    """A list a user is told to close must not name unrelated work.

    6.40.0 matched ``python``, ``pythonw`` and ``uv``, so the refusal named
    every interpreter on the machine.
    """
    if os.name != "nt":
        return

    class _Completed:
        returncode = 0
        stdout = (
            '"mcc-server.exe","22576","Console","1","120,000 K"\n'
            '"python.exe","4242","Console","1","90,000 K"\n'
            '"uv.exe","4243","Console","1","10,000 K"\n'
            '"fcc-desktop.exe","4244","Console","1","80,000 K"\n'
            '"chrome.exe","4245","Console","1","800,000 K"\n'
        )

    monkeypatch.setattr(
        migrate_config_dir.subprocess, "run", lambda *a, **k: _Completed()
    )
    hints = migrate_config_dir._running_mcc_processes_windows()

    assert hints == ["mcc-server.exe (PID 22576)", "fcc-desktop.exe (PID 4244)"]


def test_restore_note_refuses_to_nest_into_a_recreated_legacy_home(
    tmp_path: Path, monkeypatch
) -> None:
    """The rollback command must fail, not move ``~/.mcc`` *inside* ``~/.fcc``.

    ``Move-Item -Force`` and a bare ``mv`` both move the source into an
    existing target directory. A server left running across the migration
    recreates the legacy home from its cached log path, so "the target exists"
    is the likely case, not the unlikely one.
    """
    _home(tmp_path, monkeypatch)
    _legacy_home(tmp_path)
    migrate_config_dir.migrate_config_dir()

    note = (tmp_path / ".fcc-old" / "RESTORE.txt").read_text(encoding="utf-8")
    assert "-Force" not in note
    if platform.system() == "Windows":
        assert "Test-Path" in note and "throw" in note
    else:
        assert "mv -T" in note and "[ ! -e " in note
    assert "deliberately refuses" in note
