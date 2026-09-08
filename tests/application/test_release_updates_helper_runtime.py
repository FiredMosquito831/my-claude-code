"""Run the generated update helper for real, against a locked shim.

Everything else about this helper is checked by reading the script it renders.
That is how a guard wired to the opposite of its stated intent survived three
releases: ``if ($refused.Count -eq 0)`` reads perfectly well, and it meant that
one ``mcc-claude`` window the user had left open for the afternoon skipped the
whole fast install path. So these two cases actually execute the script, with a
genuinely locked file (``CreateFileW`` with a share mode of zero -- the same
lock a running launcher holds), and assert what ends up on disk:

* the install completes with the locked launcher KEPT and named in the receipt,
* the desktop shell's own launcher is never renamed out from under it,
* and a failed install still leaves a server running.

Windows-only, because the deferred helper is a Windows mechanism.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from my_claude_code.application import release_updates

pytestmark = [
    pytest.mark.skipif(os.name != "nt", reason="the deferred helper is Windows-only"),
    pytest.mark.xdist_group(name="release-updates-helper-runtime"),
]

_SHIMS = ("mcc-claude", "mcc-server", "mcc-desktop")

#: What a pre-update shim holds, so a replaced one is told apart from a kept one.
_OLD_SHIM = b"old shim\n"


class _ExclusiveHandle:
    """A file open the way a running launcher holds its own image.

    ``FILE_SHARE_*`` all zero: Windows then refuses a rename, a delete and an
    overwrite of the path while this is open, which is exactly the condition
    the whole kept-shim mechanism exists for. Python's own ``open`` shares
    read, write and delete, so it would not reproduce it.
    """

    def __init__(self, path: Path) -> None:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._close = kernel32.CloseHandle
        handle = kernel32.CreateFileW(
            str(path),
            0x8000_0000,  # GENERIC_READ
            0,  # no sharing at all
            None,
            3,  # OPEN_EXISTING
            0x80,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        if handle == -1:
            raise OSError(f"could not lock {path}")
        self._handle = handle

    def close(self) -> None:
        self._close(self._handle)


def _fake_uv(path: Path, *, succeed_in_stage_bin: bool) -> None:
    """A ``uv`` stand-in that behaves the way the real one does under a lock.

    The real uv aborts the whole install on the first shim it cannot overwrite,
    so the canonical-path run fails while a run into a directory nothing is
    holding succeeds. That difference is the entire reason the staged fallback
    exists, so the double has to reproduce it rather than always succeeding.
    """

    stage_branch = (
        "\n".join(
            f'copy /y "%~f0" "%UV_TOOL_BIN_DIR%\\{name}.exe" >nul' for name in _SHIMS
        )
        + "\nexit /b 0\n"
        if succeed_in_stage_bin
        else "exit /b 1\n"
    )
    path.write_text(
        "@echo off\r\n"
        'echo uv %*>>"%UV_CALL_LOG%"\r\n'
        'if not "%UV_TOOL_BIN_DIR%"=="" goto staged\r\n'
        "exit /b 1\r\n"
        ":staged\r\n" + stage_branch.replace("\n", "\r\n"),
        encoding="utf-8",
    )


def _render(tmp_path: Path, *, uv: Path, launcher: Path, bin_dir: Path) -> Path:
    script = release_updates._deferred_helper_script(
        uv_executable=str(uv),
        command=[str(uv), "tool", "install", "--force", "my-claude-code"],
        result_path=tmp_path / "pending-upgrade.json",
        stage_dir=tmp_path,
        server_launcher=launcher,
        working_directory=tmp_path,
        bin_dir=bin_dir,
        tool_dir=tmp_path / "tooldir",
        commands=list(_SHIMS),
        wait_seconds=1.0,
        version="6.58.3",
    )
    # The helper waits for the process that rendered it and, past its budget,
    # FORCE-KILLS that pid. The pid it bakes in is this test runner's. Point it
    # at a pid that does not exist so the wait ends at once and nothing here is
    # ever a candidate for Stop-Process: the wait itself has its own tests, and
    # what is under test below starts after it.
    script = script.replace(f"$parent = {os.getpid()}\n", "$parent = 999999\n", 1)
    assert "$parent = 999999" in script
    path = tmp_path / "apply-upgrade.ps1"
    path.write_text(script, encoding="utf-8")
    return path


def _prepare(tmp_path: Path, *, succeed_in_stage_bin: bool) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in _SHIMS:
        (bin_dir / f"{name}.exe").write_bytes(_OLD_SHIM)
    # A .cmd rather than a .exe on purpose: the helper starts this file, and a
    # fabricated .exe cannot be started. The rename sweep only looks at *.exe,
    # so this is not part of what is being replaced -- which is true of the
    # real launcher too, in the sense that it is the one file that must survive
    # the install in order to start the server afterwards.
    launcher = bin_dir / "fcc-server.cmd"
    started = tmp_path / "server-started.txt"
    launcher.write_text(
        f'@echo off\r\necho started>"{started}"\r\nexit /b 0\r\n', encoding="utf-8"
    )
    uv = tmp_path / "uv.cmd"
    _fake_uv(uv, succeed_in_stage_bin=succeed_in_stage_bin)
    return bin_dir, launcher, started


def _run(script: Path, tmp_path: Path, *, timeout: float) -> None:
    powershell = (
        Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not powershell.exists():
        pytest.skip("no Windows PowerShell to run the helper with")
    subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ | {"UV_CALL_LOG": str(tmp_path / "uv-calls.log")},
    )


def _wait_for(path: Path, *, seconds: float = 30.0) -> bool:
    """``Start-Process`` returns as soon as the process exists, not when it has
    done anything, so the marker the launcher writes arrives a moment later."""

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.2)
    return path.exists()


def _receipt(tmp_path: Path) -> dict:
    raw = (tmp_path / "pending-upgrade.json").read_text(encoding="utf-8-sig")
    return json.loads(raw)


def _stages(tmp_path: Path) -> list[dict]:
    raw = (tmp_path / release_updates.UPDATE_PROGRESS_FILENAME).read_text(
        encoding="utf-8-sig"
    )
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def test_an_open_mcc_claude_window_does_not_stop_the_update(tmp_path) -> None:
    """The normal case on this machine, run end to end.

    A locked ``mcc-claude.exe`` used to refuse its rename, and the refusal used
    to skip the fast install loop for the whole install. Here the install has
    to finish anyway, with that one launcher kept and named, every other shim
    replaced, and the shell's own launcher never moved.
    """

    bin_dir, launcher, started = _prepare(tmp_path, succeed_in_stage_bin=True)
    script = _render(
        tmp_path, uv=tmp_path / "uv.cmd", launcher=launcher, bin_dir=bin_dir
    )
    locked = _ExclusiveHandle(bin_dir / "mcc-claude.exe")
    try:
        _run(script, tmp_path, timeout=180)
    finally:
        locked.close()

    receipt = _receipt(tmp_path)
    assert receipt["ok"] is True, receipt
    assert (
        receipt["kept_shims"] == "mcc-claude" or "mcc-claude" in receipt["kept_shims"]
    )
    assert receipt["missing_commands"] in ([], None, "")
    # The sentence the user reads. It names the launcher, says why it was kept,
    # and says what restarting it buys.
    assert (
        "kept: mcc-claude.exe (in use) -- restart it to pick up 6.58.3"
        in (receipt["message"])
    )
    # The fast loop RAN. Before 6.58.3 a single refusal skipped it entirely,
    # and `attempts = 5` (a staged loop alone) was the fingerprint.
    assert receipt["attempts"] >= 2, receipt

    # The locked launcher kept the file it had; every other shim was replaced.
    assert (bin_dir / "mcc-claude.exe").read_bytes() == _OLD_SHIM
    assert (bin_dir / "mcc-server.exe").read_bytes() != _OLD_SHIM
    # And the shell's own launcher was never renamed out from under it: no
    # window ever saw `mcc-desktop` go missing, which is what made the shell
    # start a second installer.
    assert not list(bin_dir.glob("mcc-desktop.exe.old-*"))
    assert (bin_dir / "mcc-desktop.exe").exists()

    stages = _stages(tmp_path)
    assert [stage["stage"] for stage in stages][-1] == "done"
    assert stages[-1]["helper_done"] is True
    assert stages[-1]["version"] == "6.58.3"
    # Mid-install the receipt says a helper is running, and names it.
    installing = next(stage for stage in stages if stage["stage"] == "installing")
    assert installing["helper_done"] is False
    assert isinstance(installing["helper_pid"], int)
    assert installing["helper_pid"] > 0
    assert _wait_for(started)


def test_a_failed_install_brings_the_previous_version_back(tmp_path) -> None:
    """Q6, and the half of 2026-09-07 that had no answer at all.

    When the install failed the helper wrote a receipt and stopped. The machine
    was left with a perfectly good previous install on disk and nothing running
    it -- recovery depended entirely on a desktop window that, at the time, had
    already parked itself on a Retry button.
    """

    bin_dir, launcher, started = _prepare(tmp_path, succeed_in_stage_bin=False)
    script = _render(
        tmp_path, uv=tmp_path / "uv.cmd", launcher=launcher, bin_dir=bin_dir
    )
    began = time.monotonic()
    _run(script, tmp_path, timeout=300)

    receipt = _receipt(tmp_path)
    assert receipt["ok"] is False, receipt
    assert receipt["restarted"] is True
    assert "The previous version was restarted." in receipt["message"]
    # It really was started, not merely claimed.
    assert _wait_for(started)
    assert time.monotonic() - began < 300

    # The launchers that were moved aside are back, so there IS a previous
    # version to run. Without this the restart above starts nothing: the old
    # shims sit under `.old-` names and the new ones were never written.
    # `mcc-desktop.exe` is absent from this list precisely because it was never
    # moved: it is the launcher the running window asks, and it is exempt.
    assert set(receipt["restored_shims"]) == {"mcc-claude.exe", "mcc-server.exe"}
    for name in _SHIMS:
        assert (bin_dir / f"{name}.exe").read_bytes() == _OLD_SHIM, name
    # And the aside copies were not swept: on a failure they are the launchers.
    assert not list(bin_dir.glob("*.exe.old-*"))

    stages = _stages(tmp_path)
    assert [stage["stage"] for stage in stages][-2:] == ["failed", "recovered"]
    assert stages[-1]["helper_done"] is True
    assert "The previous version was restarted." in stages[-1]["message"]
