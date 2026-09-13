"""The restart is the DEFAULT (7.1.0), and the rule for opening the desktop app.

6.73.0 gave the installer a ``-Restart`` switch. Shipping it opt-in kept the
failure it was built to close: on 2026-09-11 two installs exited 0 fifteen
minutes apart and the machine had no server through either of them, because
nothing in the product would start one unless somebody remembered a flag. The
user asked for the default on 2026-09-10 and again, bindingly, on 2026-09-13.

So: every install and every update stops the one server bound to the configured
port, installs, starts ``mcc-server`` again and waits for ``/health``; and then
opens the desktop app, when one is installed here and is not already running.
``-NoRestart`` never stops a running server, ``-NoStart`` stops and starts
nothing, ``-NoDesktop`` leaves the app alone, and ``-Restart`` is accepted and
does nothing.

The desktop rule is RUN here, not grepped, on every PowerShell edition this
machine has and under the real ``Set-StrictMode -Version Latest``. Greps were
green for eleven releases while ``install.ps1`` wrote no receipt at all.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_CMD = REPO_ROOT / "scripts" / "install.cmd"
RUNTIME_INSTALL_JS = REPO_ROOT / "packaging" / "npm" / "bin" / "runtime-install.js"
RELEASE_UPDATES = (
    REPO_ROOT / "src" / "my_claude_code" / "application" / "release_updates.py"
)


# --------------------------------------------------------------------- static


def test_every_installer_takes_the_three_opt_outs() -> None:
    """One update path, three front doors, and cmd still decides nothing."""

    powershell = INSTALL_PS1.read_text(encoding="utf-8")
    shell = INSTALL_SH.read_text(encoding="utf-8")
    batch = INSTALL_CMD.read_text(encoding="utf-8")

    assert "[switch] $NoRestart," in powershell
    assert "[switch] $NoStart," in powershell
    assert "[switch] $NoDesktop," in powershell
    assert "--no-restart)" in shell
    assert "--no-start)" in shell
    assert "--no-desktop)" in shell
    assert '"%~1"=="--no-restart" goto arg_norestart' in batch
    assert '"%~1"=="--no-start" goto arg_nostart' in batch
    assert '"%~1"=="--no-desktop" goto arg_nodesktop' in batch
    assert "-NoRestart" in batch and "-NoStart" in batch and "-NoDesktop" in batch
    # Still a pass-through, not a second installer.
    assert "netstat" not in batch
    assert "report-holder" not in batch


def test_restart_is_accepted_and_is_a_no_op() -> None:
    """``-Restart``/``--restart`` still parse, and gate nothing any more.

    A caller that passes it -- the update helper does, on every Windows update
    -- must not break, and must not get different behaviour from a caller that
    does not.
    """

    powershell = INSTALL_PS1.read_text(encoding="utf-8")
    shell = INSTALL_SH.read_text(encoding="utf-8")

    assert "[switch] $Restart," in powershell
    assert "--restart)" in shell
    # The 6.73.0 gates are gone. These were the variables every restart branch
    # was conditioned on; if either name comes back, so has the opt-in.
    # (`no_restart_requested` contains the old name as a substring, so the
    # POSIX check is on the assignment, not on the word.)
    assert "$script:RestartRequested" not in powershell
    assert "\nrestart_requested=" not in shell
    assert '"$restart_requested"' not in shell


def test_the_default_branch_is_not_conditioned_on_a_switch() -> None:
    """The last branch of each installer restarts without being asked."""

    powershell = INSTALL_PS1.read_text(encoding="utf-8")
    shell = INSTALL_SH.read_text(encoding="utf-8")

    assert "elseif ((-not $DryRun) -and (-not $script:Deferred)) {" in powershell, (
        "install.ps1's fall-through restart is gated on something again"
    )
    assert 'elif [ "$dry_run" -ne 1 ]; then' in shell


def test_no_restart_is_the_only_thing_that_skips_the_stop() -> None:
    """--no-restart never reaches the classifier, let alone the stop.

    The promise of --no-restart is that a running server is not touched: not
    stopped, not classified, not identified. Structurally that means every
    call to the stop is under the same guard.
    """

    powershell = INSTALL_PS1.read_text(encoding="utf-8")
    shell = INSTALL_SH.read_text(encoding="utf-8")

    assert "if ($script:StopAllowed) {" in powershell
    assert "$stopVerdict = Get-NoStopVerdict -Address $Address" in powershell
    assert 'if [ "$stop_allowed" -eq 1 ]; then' in shell
    assert "no_stop_verdict" in shell
    # And the verdict it produces is reported, never acted on.
    assert "left-running" in powershell
    assert "left-running" in shell


def test_the_update_helper_still_passes_restart() -> None:
    """The desktop-driven update is byte-for-byte what it was in 7.0.0.

    It passed ``-Restart``; the switch is now the default plus a no-op alias,
    so the helper keeps passing it and the alias stays exercised on every real
    Windows update rather than only in a test.
    """

    text = RELEASE_UPDATES.read_text(encoding="utf-8")
    assert 'installer_args = ["-Restart"]' in text
    assert 'installer_args = ["-NoStart"]' in text


def test_the_npm_wrapper_passes_the_opt_outs_through() -> None:
    """And never opens an app it was told not to install."""

    text = RUNTIME_INSTALL_JS.read_text(encoding="utf-8")
    assert '"--no-restart",' in text
    assert '"--no-start",' in text
    assert '"--no-restart": "-NoRestart"' in text
    assert '"--no-start": "-NoStart"' in text
    assert '"--no-desktop": "-NoDesktop"' in text


def test_the_desktop_app_is_never_found_by_image_name() -> None:
    """Invariant: the tool environment is a directory named ``my-claude-code``.

    Every earlier incident on this product came from matching a process by its
    image name or a substring of its command line. The desktop check compares
    executable PATHS, and these assertions are what stops a future edit from
    quietly reintroducing the cheaper thing.
    """

    powershell = INSTALL_PS1.read_text(encoding="utf-8")
    shell = INSTALL_SH.read_text(encoding="utf-8")
    # Only the desktop rule's own functions: the deferred-install path has its
    # own, older, launcher-by-name scan and this is not a test about that.
    rule = "\n".join(_extract_function(powershell, name) for name in DESKTOP_FUNCTIONS)

    assert "-Name" not in rule, "the desktop check went back to matching a name"
    assert "process.Path" in rule.replace("$", ""), "the path comparison is gone"
    assert "taskkill" not in powershell
    assert "pkill" not in shell
    assert "pgrep" not in shell
    # The receipt beside the binary is the proof this product installed it.
    assert "MyClaudeCode.receipt.json" in powershell
    assert "MyClaudeCode.receipt.json" in shell


# ------------------------------------------------------- the rule, actually run


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


#: The desktop rule, and nothing else. Pulled out of the real script so this
#: test can never drift from the code it pins.
DESKTOP_FUNCTIONS = (
    "Get-DesktopShellBinaryCandidates",
    "Get-InstalledDesktopShells",
    "Test-DesktopShellIsRunning",
    "Get-DesktopSkipReason",
)


def _desktop_harness(
    *,
    shell_dir: Path,
    start_allowed: bool = True,
    no_desktop: bool = False,
) -> str:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    bodies = "\n\n".join(_extract_function(text, name) for name in DESKTOP_FUNCTIONS)
    return f"""Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:MCC_DESKTOP_SHELL_DIR = '{shell_dir}'
$script:StartAllowed = ${str(start_allowed).lower()}
$script:NoDesktopRequested = ${str(no_desktop).lower()}

{bodies}

$reason = Get-DesktopSkipReason
Write-Output ("REASON=[" + $reason + "]")
"""


def _run_powershell(executable: str, script: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    # The harness must decide for itself; a CI runner's own CI=true would
    # answer every one of these questions before the rule was reached.
    environment.pop("CI", None)
    environment.pop("MCC_INSTALL_NO_DESKTOP", None)
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
        timeout=180,
        check=False,
        env=environment,
    )


def _reason(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stdout + result.stderr
    for line in result.stdout.splitlines():
        if line.startswith("REASON=["):
            return line[len("REASON=[") : -1]
    raise AssertionError(f"no REASON line in {result.stdout!r}")


def _write_script(tmp_path: Path, body: str, name: str = "rule.ps1") -> Path:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8", newline="\n")
    return script


@pytest.mark.skipif(sys.platform != "win32", reason="the PowerShell installer")
@pytest.mark.parametrize("edition,executable", _powershells())
def test_a_binary_without_a_receipt_is_never_ours(
    tmp_path: Path, edition: str, executable: str
) -> None:
    """A file called MyClaudeCode.exe that we did not install is somebody else's.

    The receipt is the only proof this product put the binary there, which is
    the whole guard against this launching a stranger's executable.
    """

    shell_dir = tmp_path / "shell"
    shell_dir.mkdir()
    (shell_dir / "MyClaudeCode.exe").write_bytes(b"not ours")

    script = _write_script(tmp_path, _desktop_harness(shell_dir=shell_dir))
    assert _reason(_run_powershell(executable, script)) == (
        "the desktop app is not installed here"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="the PowerShell installer")
@pytest.mark.parametrize("edition,executable", _powershells())
def test_installed_and_not_running_is_the_one_case_that_launches(
    tmp_path: Path, edition: str, executable: str
) -> None:
    shell_dir = tmp_path / "shell"
    shell_dir.mkdir()
    (shell_dir / "MyClaudeCode.exe").write_bytes(b"ours")
    (shell_dir / "MyClaudeCode.receipt.json").write_text(
        '{"tag": "v7.1.0"}', encoding="utf-8"
    )

    script = _write_script(tmp_path, _desktop_harness(shell_dir=shell_dir))
    assert _reason(_run_powershell(executable, script)) == ""


@pytest.mark.skipif(sys.platform != "win32", reason="the PowerShell installer")
@pytest.mark.parametrize("edition,executable", _powershells())
def test_an_already_running_app_is_never_launched_twice(
    tmp_path: Path, edition: str, executable: str
) -> None:
    """The helper-driven update's case: the app is running and watching.

    A real process is started from a real copy of a binary at the path the
    receipt vouches for, so what the rule matches is the executable PATH the
    operating system reports -- not a name, and not a command line.
    """

    shell_dir = tmp_path / "shell"
    shell_dir.mkdir()
    stand_in = Path(os.environ["SYSTEMROOT"]) / "System32" / "ping.exe"
    binary = shell_dir / "MyClaudeCode.exe"
    shutil.copy2(stand_in, binary)
    (shell_dir / "MyClaudeCode.receipt.json").write_text(
        '{"tag": "v7.1.0"}', encoding="utf-8"
    )

    running = subprocess.Popen(
        [str(binary), "-n", "30", "-w", "1000", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        # Give the image a moment to be reported with its path.
        time.sleep(1.0)
        script = _write_script(tmp_path, _desktop_harness(shell_dir=shell_dir))
        assert _reason(_run_powershell(executable, script)) == "it is already running"
    finally:
        running.kill()
        running.wait(timeout=30)


@pytest.mark.skipif(sys.platform != "win32", reason="the PowerShell installer")
@pytest.mark.parametrize("edition,executable", _powershells())
def test_no_desktop_and_no_start_both_refuse(
    tmp_path: Path, edition: str, executable: str
) -> None:
    shell_dir = tmp_path / "shell"
    shell_dir.mkdir()
    (shell_dir / "MyClaudeCode.exe").write_bytes(b"ours")
    (shell_dir / "MyClaudeCode.receipt.json").write_text(
        '{"tag": "v7.1.0"}', encoding="utf-8"
    )

    no_desktop = _write_script(
        tmp_path,
        _desktop_harness(shell_dir=shell_dir, no_desktop=True),
        name="no-desktop.ps1",
    )
    assert _reason(_run_powershell(executable, no_desktop)) == "-NoDesktop was given"

    no_start = _write_script(
        tmp_path,
        _desktop_harness(shell_dir=shell_dir, start_allowed=False),
        name="no-start.ps1",
    )
    assert _reason(_run_powershell(executable, no_start)) == "no server was started"


@pytest.mark.skipif(sys.platform != "win32", reason="the PowerShell installer")
@pytest.mark.parametrize("edition,executable", _powershells())
def test_ci_never_opens_a_window(tmp_path: Path, edition: str, executable: str) -> None:
    shell_dir = tmp_path / "shell"
    shell_dir.mkdir()
    (shell_dir / "MyClaudeCode.exe").write_bytes(b"ours")
    (shell_dir / "MyClaudeCode.receipt.json").write_text(
        '{"tag": "v7.1.0"}', encoding="utf-8"
    )

    script = _write_script(tmp_path, _desktop_harness(shell_dir=shell_dir))
    environment = dict(os.environ)
    environment["CI"] = "true"
    result = subprocess.run(
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
        timeout=180,
        check=False,
        env=environment,
    )
    assert _reason(result) == "this is CI"


# ------------------------------------------------------- the POSIX rule, run


SH_FUNCTIONS = (
    "desktop_shell_candidates",
    "installed_desktop_shells",
    "desktop_shell_is_running",
    "desktop_skip_reason",
)


def _extract_sh_function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"function {name} is not closed")


def _sh() -> str:
    """The POSIX shell, as a path. Every caller is behind a skipif for it."""

    resolved = shutil.which("sh")
    assert resolved is not None
    return resolved


def _sh_harness(*, shell_dir: Path, desktop_allowed: int = 1) -> str:
    text = INSTALL_SH.read_text(encoding="utf-8")
    bodies = "\n\n".join(_extract_sh_function(text, name) for name in SH_FUNCTIONS)
    return f"""set -eu
MCC_DESKTOP_SHELL_DIR='{shell_dir}'
export MCC_DESKTOP_SHELL_DIR
desktop_allowed={desktop_allowed}

{bodies}

printf 'REASON=[%s]\\n' "$(desktop_skip_reason)"
"""


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell")
def test_the_posix_rule_refuses_a_binary_without_a_receipt(tmp_path: Path) -> None:
    shell_dir = tmp_path / "shell"
    shell_dir.mkdir()
    (shell_dir / "MyClaudeCode").write_bytes(b"not ours")

    script = tmp_path / "rule.sh"
    script.write_text(_sh_harness(shell_dir=shell_dir), encoding="utf-8", newline="\n")
    environment = dict(os.environ)
    environment.pop("CI", None)
    environment.setdefault("DISPLAY", ":0")
    result = subprocess.run(
        [_sh(), str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REASON=[the desktop app is not installed here]" in result.stdout


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell")
def test_the_posix_rule_accepts_an_installed_app(tmp_path: Path) -> None:
    shell_dir = tmp_path / "shell"
    shell_dir.mkdir()
    (shell_dir / "MyClaudeCode").write_bytes(b"ours")
    (shell_dir / "MyClaudeCode.receipt.json").write_text(
        '{"tag": "v7.1.0"}', encoding="utf-8"
    )

    script = tmp_path / "rule.sh"
    script.write_text(_sh_harness(shell_dir=shell_dir), encoding="utf-8", newline="\n")
    environment = dict(os.environ)
    environment.pop("CI", None)
    environment["DISPLAY"] = ":0"
    result = subprocess.run(
        [_sh(), str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REASON=[]" in result.stdout


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell")
def test_the_posix_rule_refuses_ci_and_no_desktop(tmp_path: Path) -> None:
    shell_dir = tmp_path / "shell"
    shell_dir.mkdir()
    (shell_dir / "MyClaudeCode").write_bytes(b"ours")
    (shell_dir / "MyClaudeCode.receipt.json").write_text(
        '{"tag": "v7.1.0"}', encoding="utf-8"
    )

    script = tmp_path / "rule.sh"
    script.write_text(_sh_harness(shell_dir=shell_dir), encoding="utf-8", newline="\n")
    environment = dict(os.environ)
    environment["DISPLAY"] = ":0"
    environment["CI"] = "1"
    result = subprocess.run(
        [_sh(), str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environment,
    )
    assert "REASON=[this is CI]" in result.stdout

    off = tmp_path / "off.sh"
    off.write_text(
        _sh_harness(shell_dir=shell_dir, desktop_allowed=0),
        encoding="utf-8",
        newline="\n",
    )
    environment.pop("CI")
    result = subprocess.run(
        [_sh(), str(off)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environment,
    )
    assert "REASON=[no server was started or --no-desktop was given]" in result.stdout
