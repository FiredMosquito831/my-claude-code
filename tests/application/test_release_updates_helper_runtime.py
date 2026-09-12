"""Run the generated update helper for real, against a stand-in installer.

Everything else about this helper is checked by reading the script it renders.
That is how a guard wired to the opposite of its stated intent survived three
releases: ``if ($refused.Count -eq 0)`` reads perfectly well, and it meant that
one ``mcc-claude`` window the user had left open for the afternoon skipped the
whole fast install path. So these cases actually execute the script, with a
real parent process to wait for and a real PowerShell installer to hand over
to, and assert what ends up on disk.

From 6.82.0 the helper is a launcher: it waits for the server that asked for
the update to exit, then runs ``scripts/install.ps1 -Restart``. The staging,
the execute-verify, the swap, the health gate and the rollback moved into that
installer, and they are exercised in ``tests/scripts/``. What is left to prove
here is the hand-over itself:

* the installer is not started until the parent is gone,
* it is started with ``-Restart`` and with this episode's transcript and
  configuration directory in its environment,
* the outcome reaches ``pending-upgrade.json``,
* and a failure that the installer never got far enough to record leaves a
  terminal record anyway, so no watcher waits for ever.

Windows-only, because the deferred helper is a Windows mechanism.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from my_claude_code.application import release_updates

# Skipped at COLLECTION on anything but Windows, rather than per test. A test
# that is skipped never runs the autouse fixture that redirects HOME, so it
# reaches the hermeticity guard's teardown with whatever configuration
# directory the worker had resolved before it -- and on a Linux runner that is
# the real one. Not collecting the module at all leaves nothing to tear down.
if os.name != "nt":  # pragma: no cover - the deferred helper is Windows-only
    pytest.skip("the deferred helper is a Windows mechanism", allow_module_level=True)

pytestmark = [pytest.mark.xdist_group(name="release-updates-helper-runtime")]

_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def _write_fake_installer(path: Path, *, body: str) -> None:
    """A PowerShell script shaped like install.ps1's parameter list."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "param(\n"
        "    [string] $Version = '',\n"
        "    [switch] $Restart,\n"
        "    [switch] $NoStart\n"
        ")\n" + body,
        encoding="utf-8",
    )


def _run_helper(tmp_path: Path, *, installer: Path, parent_pid: int) -> int:
    script = release_updates._deferred_helper_script(
        result_path=tmp_path / "pending-upgrade.json",
        stage_dir=tmp_path,
        installer=installer,
        powershell=_POWERSHELL,
        config_dir=tmp_path / "config",
        working_directory=tmp_path,
        version="9.9.9",
        wait_seconds=30.0,
        install_log=tmp_path / "install-test.log",
    )
    # The helper waits for THIS pid, whatever the template baked in.
    script = script.replace(
        f"$parent = {os.getpid()}", f"$parent = {parent_pid}"
    ).replace("$parentStart = ", "$parentStart = 0 # ")
    helper = tmp_path / "apply-upgrade.ps1"
    helper.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        [
            _POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(helper),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return completed.returncode


def _short_lived_parent() -> subprocess.Popen:
    """A process that exits on its own a moment from now."""

    return subprocess.Popen(
        [_POWERSHELL, "-NoProfile", "-Command", "Start-Sleep -Seconds 3"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_the_installer_runs_only_after_the_parent_is_gone(tmp_path) -> None:
    """The installer must not touch the environment the server runs out of.

    The stand-in writes the parent's liveness at the moment it starts, so the
    assertion is about what was true WHEN the installer ran, not about what is
    true now.
    """

    installer = tmp_path / "installers" / "install.ps1"
    marker = tmp_path / "ran.json"
    parent = _short_lived_parent()
    _write_fake_installer(
        installer,
        body=(
            "$alive = [bool] (Get-Process -Id "
            + str(parent.pid)
            + " -ErrorAction SilentlyContinue)\n"
            "$record = @{ parent_alive = $alive; restart = [bool] $Restart; "
            "version = $Version; log = $env:MCC_INSTALL_LOG; "
            "config = $env:MCC_CONFIG_DIR }\n"
            "[System.IO.File]::WriteAllText("
            + f"'{marker.as_posix()}'"
            + ", ($record | ConvertTo-Json))\n"
            "exit 0\n"
        ),
    )
    started = time.monotonic()
    code = _run_helper(tmp_path, installer=installer, parent_pid=parent.pid)
    parent.wait(timeout=30)
    assert code == 0, "the helper must succeed when the installer does"
    assert marker.is_file(), "the installer was never run"
    seen = json.loads(marker.read_text(encoding="utf-8"))
    assert seen["parent_alive"] is False
    assert seen["restart"] is True
    assert seen["version"] == "9.9.9"
    assert seen["log"] == str(tmp_path / "install-test.log")
    assert seen["config"] == str(tmp_path / "config")
    # It really waited rather than racing through: the parent sleeps 3 seconds.
    assert time.monotonic() - started >= 2.0

    result = json.loads((tmp_path / "pending-upgrade.json").read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["restarted"] is True

    records = [
        json.loads(line)
        for line in (tmp_path / "progress.json")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert [record["stage"] for record in records][:2] == [
        "episode",
        "waiting-for-parent",
    ]
    # The receipt is BOM-less, because the Python reader parses JSON and JSON
    # refuses a leading U+FEFF.
    assert (tmp_path / "progress.json").read_bytes()[:3] != b"\xef\xbb\xbf"


def test_an_installer_that_fails_silently_still_ends_the_episode(tmp_path) -> None:
    """No watcher may be left waiting for a record that never comes.

    The installer owns the terminal record and normally writes it. When it
    exits non-zero without having written one -- it could not start, it threw
    before it opened the receipt -- the helper writes one itself, so the
    "one installer at a time" gate reopens and the window stops saying
    "updating".
    """

    installer = tmp_path / "installers" / "install.ps1"
    parent = _short_lived_parent()
    _write_fake_installer(
        installer, body="Write-Output 'nothing was written'\nexit 3\n"
    )
    code = _run_helper(tmp_path, installer=installer, parent_pid=parent.pid)
    parent.wait(timeout=30)
    assert code == 1

    result = json.loads((tmp_path / "pending-upgrade.json").read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert result["exit_code"] == 3

    records = [
        json.loads(line)
        for line in (tmp_path / "progress.json")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert records[-1]["stage"] == "failed"
    assert records[-1]["helper_done"] is True


def test_the_helper_does_not_overrule_a_terminal_record_the_installer_wrote(
    tmp_path,
) -> None:
    """`recovered` means the previous version is back and answering.

    Overwriting it with `failed` would tell the user their machine has no
    server when it has one -- and the installer is the only thing that knows
    which of those is true.
    """

    installer = tmp_path / "installers" / "install.ps1"
    parent = _short_lived_parent()
    receipt = (tmp_path / "progress.json").as_posix()
    _write_fake_installer(
        installer,
        body=(
            "$record = @{ stage = 'recovered'; message = 'rolled back'; "
            "helper_done = $true }\n"
            "[System.IO.File]::AppendAllText("
            + f"'{receipt}'"
            + ", (($record | ConvertTo-Json -Compress) + [Environment]::NewLine), "
            "(New-Object System.Text.UTF8Encoding($false)))\n"
            "exit 1\n"
        ),
    )
    _run_helper(tmp_path, installer=installer, parent_pid=parent.pid)
    parent.wait(timeout=30)

    records = [
        json.loads(line)
        for line in (tmp_path / "progress.json")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert records[-1]["stage"] == "recovered"
    assert [record["stage"] for record in records].count("failed") == 0


def test_a_missing_installer_is_reported_rather_than_guessed_at(tmp_path) -> None:
    """The bundled installer is the only one this helper will run."""

    parent = _short_lived_parent()
    code = _run_helper(
        tmp_path,
        installer=tmp_path / "installers" / "not-here.ps1",
        parent_pid=parent.pid,
    )
    parent.wait(timeout=30)
    assert code == 1
    result = json.loads((tmp_path / "pending-upgrade.json").read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert "not found" in result["message"]
