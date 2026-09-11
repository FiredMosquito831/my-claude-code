"""The installer's restart, its opt-outs, and the lock the two paths share.

6.73.0. Before it, the product could not update itself without leaving the user
with a dead server, and on 2026-09-11 it did exactly that: the dashboard's
helper stood down because the desktop app said it was watching, the desktop app
was a version that could not act, and the hand-run installer three minutes later
was never allowed to act at all. Two installs exited 0 and the server was down
through both of them and after both of them.

So the success condition of a restart is **a listener answering /health on the
configured port**, and never "the install exited 0". These tests pin the pieces
of that: the switches, the one-server scope, the opt-outs, the exclusive lock,
and the append-instead-of-truncate receipt.

The PowerShell here is RUN, not grepped. ``test_install_progress_receipt.py``
greps, and every one of its assertions was green throughout the eleven releases
in which ``install.ps1`` wrote no receipt at all on Windows.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_CMD = REPO_ROOT / "scripts" / "install.cmd"


# --------------------------------------------------------------------- static


def test_all_three_installers_take_the_restart_switch() -> None:
    """One update path, three front doors, and cmd decides nothing.

    ``install.cmd`` keeps the D5 shape: it downloads ``install.ps1`` and passes
    the switch through. What a restart MEANS lives in one script, not three.
    """

    powershell = INSTALL_PS1.read_text(encoding="utf-8")
    shell = INSTALL_SH.read_text(encoding="utf-8")
    batch = INSTALL_CMD.read_text(encoding="utf-8")

    assert "[switch] $Restart," in powershell
    assert "[switch] $NoStart," in powershell
    assert "--restart)" in shell
    assert "--no-start)" in shell
    assert '"%~1"=="--restart" goto arg_restart' in batch
    assert '"%~1"=="--no-start" goto arg_nostart' in batch
    assert "-Restart" in batch and "-NoStart" in batch
    # cmd is a pass-through, not a second installer.
    assert "netstat" not in batch
    assert "report-holder" not in batch


def test_the_environment_opt_out_exists_in_both_installers() -> None:
    """``MCC_INSTALL_NO_START=1``, for a caller that cannot add a switch.

    The npm wrapper and ``install.cmd`` both pass arguments through a layer
    with its own opinions about quoting, so the env form is not a convenience.
    """

    assert "MCC_INSTALL_NO_START" in INSTALL_PS1.read_text(encoding="utf-8")
    assert "MCC_INSTALL_NO_START" in INSTALL_SH.read_text(encoding="utf-8")


@pytest.mark.parametrize("script", [INSTALL_PS1, INSTALL_SH], ids=["ps1", "sh"])
def test_the_installer_never_decides_for_itself_what_it_may_stop(script: Path) -> None:
    """Invariant 1, structurally: the classification is the product's, not the
    script's.

    A shell script that decided this would be a second opinion about which
    processes My Claude Code is allowed to stop -- and the one time MCC decided
    that by image name, it stopped the user's own application, twice. The uv
    tool environment is a directory literally named ``my-claude-code``, so every
    launcher's command line contains the product's name.
    """

    text = script.read_text(encoding="utf-8")
    assert "--report-holder" in text
    assert "--stop-holder" in text
    # No script-side process hunting, of any shape.
    for forbidden in ("taskkill", "/IM ", "pkill", "killall"):
        assert forbidden not in text, forbidden
    # And no script-side port hunting either: the port holder is asked for.
    assert "Get-NetTCPConnection" not in text
    assert "lsof" not in text


@pytest.mark.parametrize("script", [INSTALL_PS1, INSTALL_SH], ids=["ps1", "sh"])
def test_the_installer_reads_the_port_from_the_config_dir_env(script: Path) -> None:
    """A3: the port is the configured one, never a constant.

    "Restart" means exactly one server: the one bound to the port of the
    configuration directory this install is for.
    """

    text = script.read_text(encoding="utf-8")
    assert ".env" in text
    assert "PORT" in text
    assert "HOST" in text
    # 0.0.0.0 is what a server BINDS, not an address a health probe can dial.
    assert "0.0.0.0" in text


@pytest.mark.parametrize("script", [INSTALL_PS1, INSTALL_SH], ids=["ps1", "sh"])
def test_success_is_a_listener_answering_health(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    assert "/health" in text
    # And a failed start says the one honest thing V1 can say: there is no
    # staged swap in the installer yet, so the previous version is gone.
    assert "run the installer again" in text


@pytest.mark.parametrize("script", [INSTALL_PS1, INSTALL_SH], ids=["ps1", "sh"])
def test_other_servers_are_listed_and_never_stopped(script: Path) -> None:
    """The user's instruction, 2026-09-11 15:35 and 15:37, in the scripts."""

    text = script.read_text(encoding="utf-8")
    assert "None of them is touched" in text


@pytest.mark.parametrize("script", [INSTALL_PS1, INSTALL_SH], ids=["ps1", "sh"])
def test_neither_installer_truncates_the_receipt_any_more(script: Path) -> None:
    """B2. The 15:04 defect, pinned in the two files that caused it."""

    text = script.read_text(encoding="utf-8")
    assert '"stage":"episode"' in text or "stage           = 'episode'" in text
    if script is INSTALL_SH:
        assert ': > "$install_progress_path"' not in text
    else:
        assert (
            "[System.IO.File]::WriteAllText($script:InstallProgressPath, ''" not in text
        )


@pytest.mark.parametrize("script", [INSTALL_PS1, INSTALL_SH], ids=["ps1", "sh"])
def test_both_installers_take_the_same_lock(script: Path) -> None:
    """B1. One lock file, one name, taken by both paths and by the helper."""

    from my_claude_code.config.update_progress import UPDATE_LOCK_FILENAME

    assert UPDATE_LOCK_FILENAME in script.read_text(encoding="utf-8")


def test_the_helper_takes_the_same_lock_and_opens_an_episode(tmp_path) -> None:
    """The third writer. Two paths that share a file must share its lock."""

    from my_claude_code.application.release_updates import _deferred_helper_script
    from my_claude_code.config.update_progress import UPDATE_LOCK_FILENAME

    script = _deferred_helper_script(
        uv_executable="uv",
        command=["uv", "tool", "install", "my-claude-code"],
        result_path=tmp_path / "updates" / "pending-upgrade.json",
        stage_dir=tmp_path / "updates",
        server_launcher=tmp_path / "bin" / "mcc-server.exe",
        working_directory=tmp_path,
    )

    assert UPDATE_LOCK_FILENAME in script
    assert "watching it instead" in script
    assert script.index("Enter-UpdateLock") < script.index("Write-Stage 'episode'")
    # Released on every ending, from inside Write-Stage: this helper has a
    # dozen ways of reaching a terminal stage and no ending may forget.
    assert "Exit-UpdateLock" in script


# ------------------------------------------------------------------- RUN (ps1)


pytestmark_windows = pytest.mark.skipif(
    os.name != "nt", reason="PowerShell editions are a Windows fact"
)


def _powershells() -> list[tuple[str, str]]:
    found = []
    for name, executable in (("ps51", "powershell"), ("pwsh7", "pwsh")):
        resolved = shutil.which(executable)
        if resolved:
            found.append((name, resolved))
    return found


def _extract_function(text: str, name: str) -> str:
    start = text.index(f"function {name} {{")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"function {name} is not closed")


LOCK_FUNCTIONS = (
    "Get-MccConfigDir",
    "Get-MccEnvSetting",
    "Get-MccServerAddress",
    "Get-ServerStartTimeoutSeconds",
    "Get-UpdateLockPath",
    "Read-UpdateLockOwner",
    "Test-UpdateLockOwnerAlive",
    "Enter-UpdateLock",
    "Exit-UpdateLock",
)


def _harness(config_dir: Path, body: str) -> str:
    """The real function bodies, under the real StrictMode."""

    text = INSTALL_PS1.read_text(encoding="utf-8")
    bodies = "\n\n".join(_extract_function(text, name) for name in LOCK_FUNCTIONS)
    return f"""Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$DryRun = $false
$env:MCC_CONFIG_DIR = '{config_dir}'
$script:HoldsUpdateLock = $false
$script:UpdateLockPath = ""
$script:UpdateLockOwner = $null

{bodies}

{body}
"""


def _run(executable: str, script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytestmark_windows
@pytest.mark.parametrize(("name", "executable"), _powershells(), ids=lambda v: v)
def test_the_installer_reads_a_scratch_port_out_of_a_scratch_env(
    name: str, executable: str, tmp_path: Path
) -> None:
    """A3, RUN: ``MCC_CONFIG_DIR`` + a non-default ``PORT`` in that directory.

    The value has to come out of the file, and 0.0.0.0 has to become an address
    a health probe can actually dial.
    """

    config_dir = tmp_path / name
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "# a comment\nHOST=0.0.0.0\nexport PORT = 8391\nOTHER=ignored\n",
        encoding="utf-8",
    )
    script = tmp_path / f"port-{name}.ps1"
    script.write_text(
        _harness(
            config_dir,
            "$address = Get-MccServerAddress\n"
            'Write-Output ("PORT=" + $address.Port)\n'
            'Write-Output ("BIND=" + $address.BindHost)\n'
            'Write-Output ("REACHABLE=" + $address.ReachableHost)\n',
        ),
        encoding="utf-8",
    )

    completed = _run(executable, script)
    assert completed.returncode == 0, completed.stderr
    assert "PORT=8391" in completed.stdout, completed.stdout
    assert "BIND=0.0.0.0" in completed.stdout
    assert "REACHABLE=127.0.0.1" in completed.stdout


@pytestmark_windows
@pytest.mark.parametrize(("name", "executable"), _powershells(), ids=lambda v: v)
def test_a_second_installer_finds_the_lock_held_and_a_dead_owner_reclaimed(
    name: str, executable: str, tmp_path: Path
) -> None:
    """B1, RUN, both halves.

    A live owner is believed -- the second installer does not install. A dead
    owner's lock is reclaimed rather than waited on, because a lock that
    outlives its owner would take the machine out of updating for the rest of
    the day the first time an installer was killed.
    """

    config_dir = tmp_path / name
    (config_dir / "updates").mkdir(parents=True)
    lock = config_dir / "updates" / "update.lock"

    # A pid that is certainly alive: this test process.
    lock.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started_at": int(time.time()),
                "started_display": "18:45:12",
                "source": "the dashboard update",
            }
        ),
        encoding="utf-8",
    )
    script = tmp_path / f"lock-{name}.ps1"
    script.write_text(
        _harness(
            config_dir,
            "if (Enter-UpdateLock) { Write-Output 'TOOK' } else { Write-Output 'HELD' }\n"
            "$owner = Read-UpdateLockOwner -Path (Get-UpdateLockPath)\n"
            'Write-Output ("OWNER=" + $owner.pid + " " + $owner.started_display)\n',
        ),
        encoding="utf-8",
    )
    completed = _run(executable, script)
    assert completed.returncode == 0, completed.stderr
    assert "HELD" in completed.stdout, completed.stdout
    assert f"OWNER={os.getpid()} 18:45:12" in completed.stdout
    # The live owner's lock is still there, untouched.
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()

    # Now a pid that is certainly gone.
    dead = _dead_pid()
    lock.write_text(
        json.dumps({"pid": dead, "started_at": 0, "started_display": "00:00:00"}),
        encoding="utf-8",
    )
    reclaim = tmp_path / f"reclaim-{name}.ps1"
    reclaim.write_text(
        _harness(
            config_dir,
            "if (Enter-UpdateLock) { Write-Output 'TOOK' } else { Write-Output 'HELD' }\n"
            "$owner = Read-UpdateLockOwner -Path (Get-UpdateLockPath)\n"
            'Write-Output ("OWNER=" + $owner.pid)\n'
            "Exit-UpdateLock\n"
            "if (Test-Path -LiteralPath (Get-UpdateLockPath)) { Write-Output 'STILL' } "
            "else { Write-Output 'RELEASED' }\n",
        ),
        encoding="utf-8",
    )
    completed = _run(executable, reclaim)
    assert completed.returncode == 0, completed.stderr
    assert "TOOK" in completed.stdout, completed.stdout
    assert f"OWNER={dead}" not in completed.stdout
    assert "RELEASED" in completed.stdout
    assert not lock.exists()


def _dead_pid() -> int:
    """A process id that is certainly not running."""

    completed = subprocess.run(
        ["cmd", "/c", "exit", "0"] if os.name == "nt" else ["true"],
        capture_output=True,
        check=False,
    )
    del completed
    # A very high id no live process on a fresh boot will hold. Verified below.
    candidate = 999_999
    while _pid_alive(candidate):  # pragma: no cover - practically never taken
        candidate -= 2
    return candidate


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    completed = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
        capture_output=True,
        text=True,
        check=False,
    )
    return f'"{pid}"' in completed.stdout


# ------------------------------------------- the hang this nearly shipped with


def test_the_started_server_inherits_nothing_from_the_installer() -> None:
    """Measured on 2026-09-11: a successful restart hung its own caller.

    ``Start-Process -RedirectStandardOutput`` asks .NET for
    ``bInheritHandles=TRUE``, and on Windows that is all-or-nothing: the child
    inherits every inheritable handle the installer holds, **including the
    stdout pipe its own caller gave it**. The server then keeps that pipe open
    for as long as it runs, the caller's read never reaches end-of-file, and the
    installer never returns -- with the server answering ``/health``, the
    receipt written, and ``done`` on disk.

    Every caller of this script reads its output through a pipe: a GitHub
    ``run:`` step, ``install.cmd``, ``install.ps1 | tee``, the update helper.
    ``Win32_Process.Create`` inherits nothing and opens no console window, which
    is the whole of what "detached" has to mean here.
    """

    powershell = INSTALL_PS1.read_text(encoding="utf-8")
    start = powershell[powershell.index("function Start-MccServerDetached") :]
    start = start[: start.index("\nfunction ")]
    # No -Redirect* switch anywhere in it: that is the whole fix. PowerShell
    # then uses ShellExecute, which passes this process's ENVIRONMENT and none
    # of its HANDLES. The redirection lives in a one-line .cmd instead.
    # The CODE, not the comment block that explains it.
    code = start[start.index("#>") + 2 :]
    code = "\n".join(
        line for line in code.splitlines() if not line.strip().startswith("#")
    )
    assert "-RedirectStandard" not in code, (
        "a -Redirect* switch is back, and with it bInheritHandles=TRUE"
    )
    assert "Start-Process" in start
    assert "-WindowStyle Hidden" in start
    assert ".cmd" in start, "the redirection is not written to a runner file"
    # And the configuration directory is passed EXPLICITLY, not merely
    # inherited. Win32_Process.Create -- the first attempt at "no inherited
    # handles" -- has no environment parameter at all, so the server it started
    # came up for the DEFAULT configuration home on the default port, and
    # 6.59.0's takeover then stopped the server that was already there.
    # Measured on a real machine at 19:55 on 2026-09-11.
    assert "MCC_CONFIG_DIR=" in start
    assert "Win32_Process" not in code


def test_the_posix_start_redirects_all_three_streams() -> None:
    """The same reasoning on POSIX. ``</dev/null`` is not decoration.

    A child that keeps the installer's stdin open holds its caller open too,
    and ``nohup`` only redirects the streams that are a terminal.
    """

    shell = INSTALL_SH.read_text(encoding="utf-8")
    start = shell[shell.index("start_server_detached() {") :]
    start = start[: start.index("\nrestart_after_install() {")]
    assert "< /dev/null" in start
    assert '> "$start_log" 2>&1 &' in start
    assert "setsid" in start


def test_an_older_mcc_server_is_never_asked_the_port_question() -> None:
    """It does not refuse the flag. It starts a server.

    ``cli.entrypoints.serve`` ignores every argument but ``--version``, so
    ``mcc-server --report-holder 8392`` on 6.72.2 and earlier does not fail --
    it STARTS A SERVER on the configured port, and the installer waiting for a
    document blocks behind it for ever. Measured on the real installer at 20:12
    on 2026-09-11, against the published wheel.

    "Exit non-zero when you do not know the flag" was a reasonable design and
    it is not what the shipped code does, so the installer asks the VERSION --
    the one thing every build has always answered -- and only asks the port
    question of a build new enough to have it.
    """

    powershell = INSTALL_PS1.read_text(encoding="utf-8")
    shell = INSTALL_SH.read_text(encoding="utf-8")

    assert '$RestartAwareVersion = "6.73.0"' in powershell
    assert 'RESTART_AWARE_VERSION="6.73.0"' in shell
    # The gate is BEFORE the call, in both.
    assert powershell.index("Test-VersionAtLeast -Version $InstalledVersion") < (
        powershell.index("$report = Get-PortHolderDocument")
    )
    assert shell.index('version_at_least "${FCC_VERSION:-}"') < (
        shell.index(
            'ask_the_product_about_the_port "$restart_launcher" --report-holder'
        )
    )
    # And an unreadable version is NOT new enough.
    gate = powershell[powershell.index("function Test-VersionAtLeast") :]
    gate = gate[: gate.index("\nfunction ")]
    assert "return $false" in gate
