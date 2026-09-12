"""The Windows installer's receipt function, RUN rather than grepped.

`scripts/install.ps1` sets ``Set-StrictMode -Version Latest``, under which
*retrieving* an unset variable is a terminating error.
``Write-InstallProgress`` began with ``if (-not $script:InstallProgressPath)``
against a variable nothing assigned, and its own ``catch`` -- there so that a
receipt nobody can write is never the reason an install fails -- swallowed the
error. So the installer wrote **no receipt at all** on Windows, silently, from
6.59.0 to 6.70.1. Measured on 2026-09-10 on Windows PowerShell 5.1 and pwsh 7:
the ``updates`` directory was not even created, and both the ``installing`` and
the ``done`` calls wrote nothing.

The consequence was not cosmetic. The 6.59.0 contract is "a hand-run ``irm
install.ps1 | iex`` is visible to the helper-alive gate", and with no receipt
the gate is inert: the desktop shell sees no installer in flight and is free to
start one of its own into the tool directory this script is writing. On
2026-09-09 at 11:22 this script and the dashboard's update helper installed over
each other on the reporter's machine.

The tests that "pinned" the receipt (``test_install_progress_receipt.py``) only
grep the script's text, which is why every one of them was green throughout.
These RUN the real function body, under StrictMode, on every PowerShell edition
the machine has.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from my_claude_code.config.update_progress import (
    EPISODE_MARKER_STAGE,
    INSTALLING_MESSAGE,
    UPDATE_PROGRESS_FILENAME,
    UPDATE_STAGE_DIRNAME,
    helper_is_alive,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"

#: Every field ``config.update_progress`` reads off a record.
LIVENESS_FIELDS = ("stage", "message", "helper_pid", "started_at", "helper_done")

#: The functions the receipt path needs, and nothing else. Pulled out of the
#: real script so the test can never drift from the code it is pinning: a copy
#: of the function body here would have been just as green as the greps were.
NEEDED = (
    "Get-MccConfigDir",
    "Get-InstallStageRank",
    "Initialize-InstallProgress",
    "Write-InstallLog",
    "Write-InstallProgress",
)

#: The script-scope variables the receipt path reads. Their absence is the bug.
INITIALISERS = (
    "$script:InstallProgressPath",
    "$script:InstallProgressLog",
    "$script:InstallProgressEncoding",
    "$script:InstallProgressStarted",
    "$script:InstallProgressVersion",
    "$script:InstallProgressRank",
    "$script:InstallProgressRestarted",
    "$script:InstallProgressHolder",
)


def _powershells() -> list[tuple[str, str]]:
    """Every PowerShell edition on this machine, as (id, executable).

    Both are tested because they are different parsers with different
    StrictMode implementations, and the reporter's machine runs the one that
    ships with Windows. A machine with only one still tests that one.
    """

    found = []
    for name, executable in (("ps51", "powershell"), ("pwsh7", "pwsh")):
        resolved = shutil.which(executable)
        if resolved:
            found.append((name, resolved))
    return found


def _extract_function(text: str, name: str) -> str:
    """The whole of ``function <name> { ... }`` from the real script."""

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


def _harness(config_dir: Path, *, version: str = "6.71.0") -> str:
    """A script that is the real function body under the real StrictMode."""

    text = INSTALL_PS1.read_text(encoding="utf-8")
    for initialiser in INITIALISERS:
        assert f"{initialiser} =" in text, f"{initialiser} is never assigned"
    bodies = "\n\n".join(_extract_function(text, name) for name in NEEDED)
    # The FIRST assignment of each, which is the initialiser block at the top.
    # A later `$script:InstallProgressVersion = $InstalledVersion` belongs to
    # the script body and would drag an unset variable in with it.
    lines = text.splitlines()
    initialisers = "\n".join(
        next(line for line in lines if line.startswith(f"{name} ="))
        for name in INITIALISERS
    )
    return f"""Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# install.ps1's own -DryRun switch, as the script body sees it.
$DryRun = $false
$env:MCC_CONFIG_DIR = '{config_dir}'
{initialisers}

{bodies}

$script:InstallProgressVersion = '{version}'
Write-InstallProgress -Stage 'installing' -Message '{INSTALLING_MESSAGE}'
Write-InstallLog 'uv tool install --force'
Write-InstallProgress -Stage 'verifying' -Message 'Checking that every command is in place.'
Write-InstallProgress -Stage 'done' -Message 'The new version is installed.'
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


def _records(config_dir: Path) -> list[dict]:
    receipt = config_dir / UPDATE_STAGE_DIRNAME / UPDATE_PROGRESS_FILENAME
    assert receipt.is_file(), (
        f"no receipt at {receipt}. This is the 6.59.0-6.70.1 bug: the "
        "StrictMode failure is swallowed by the function's own catch, so the "
        "updates directory is not even created."
    )
    raw = receipt.read_text(encoding="utf-8-sig")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="PowerShell editions and StrictMode are Windows facts"
)


@pytest.mark.parametrize(
    ("name", "executable"), _powershells(), ids=lambda value: value
)
def test_the_powershell_receipt_function_actually_writes_a_record(
    name: str, executable: str, tmp_path: Path
) -> None:
    """The one assertion the greps could not make: a record lands on disk."""

    config_dir = tmp_path / name
    config_dir.mkdir()
    script = tmp_path / f"harness-{name}.ps1"
    script.write_text(_harness(config_dir), encoding="utf-8")

    completed = _run(executable, script)
    assert completed.returncode == 0, completed.stderr

    records = _records(config_dir)
    # 6.73.0: the marker that OPENS the episode, then the three stages. Nobody
    # truncates this file any more, so the marker is how a reader arriving a
    # minute late still knows where the current episode begins.
    assert len(records) == 4, records
    assert records[0]["stage"] == EPISODE_MARKER_STAGE
    assert records[0]["source"] == "install.ps1"
    assert records[0]["helper_pid"] > 0
    installing = records[1]
    for field in LIVENESS_FIELDS:
        assert field in installing, f"{field} is missing from {installing}"
    assert installing["stage"] == "installing"
    assert installing["message"] == INSTALLING_MESSAGE
    assert installing["helper_pid"] > 0
    assert installing["started_at"] > 0
    assert installing["helper_done"] is False
    assert installing["version"] == "6.71.0"
    assert installing["source"] == "install.ps1"
    # And the record that lets a window show the install happening.
    assert installing["log"].endswith(".log")
    assert isinstance(installing["elapsed_seconds"], int | float)

    # The reader that gates on it agrees this is a live installer -- with the
    # pid of a process that IS still alive, because the harness's own has
    # exited by the time the record is read, and the pid is deliberately the
    # thing that decides (6.58.3).
    assert helper_is_alive(dict(installing, helper_pid=os.getpid())) is True
    assert helper_is_alive(installing) is False, (
        "the installer has exited, so its record must stop gating"
    )
    # ...and stops believing it on the terminal record.
    assert records[-1]["stage"] == "done"
    assert records[-1]["helper_done"] is True
    assert helper_is_alive(records[-1]) is False


@pytest.mark.parametrize(
    ("name", "executable"), _powershells(), ids=lambda value: value
)
def test_the_installer_transcript_is_written_where_the_record_says(
    name: str, executable: str, tmp_path: Path
) -> None:
    """Decision Q2: the receipt points at the transcript, and it is really there."""

    config_dir = tmp_path / name
    config_dir.mkdir()
    script = tmp_path / f"harness-log-{name}.ps1"
    script.write_text(_harness(config_dir), encoding="utf-8")

    assert _run(executable, script).returncode == 0

    named = Path(_records(config_dir)[0]["log"])
    assert _records(config_dir)[0]["stage"] == EPISODE_MARKER_STAGE
    assert named.parent == config_dir / UPDATE_STAGE_DIRNAME
    assert named.name.startswith("install-")
    assert named.is_file(), f"the receipt names {named}, which does not exist"
    assert "uv tool install --force" in named.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "executable"), _powershells(), ids=lambda value: value
)
def test_a_shared_transcript_is_appended_to_rather_than_replaced(
    name: str, executable: str, tmp_path: Path
) -> None:
    """``MCC_INSTALL_LOG``: one episode, one transcript.

    The update helper owns a transcript before this script starts, and a second
    installer that truncated it would take away the very thing the window is
    showing.
    """

    config_dir = tmp_path / name
    (config_dir / UPDATE_STAGE_DIRNAME).mkdir(parents=True)
    shared = config_dir / UPDATE_STAGE_DIRNAME / "install-shared.log"
    shared.write_text(
        "[00:00:00] the helper was already writing here\n", encoding="utf-8"
    )

    script = tmp_path / f"harness-shared-{name}.ps1"
    body = _harness(config_dir).replace(
        "$env:MCC_CONFIG_DIR =",
        f"$env:MCC_INSTALL_LOG = '{shared}'\n$env:MCC_CONFIG_DIR =",
        1,
    )
    script.write_text(body, encoding="utf-8")

    assert _run(executable, script).returncode == 0

    assert _records(config_dir)[1]["log"] == str(shared)
    text = shared.read_text(encoding="utf-8")
    assert "the helper was already writing here" in text, "the transcript was truncated"
    assert "uv tool install --force" in text


@pytest.mark.parametrize(
    ("name", "executable"), _powershells(), ids=lambda value: value
)
def test_a_failed_install_still_writes_a_terminal_record(
    name: str, executable: str, tmp_path: Path
) -> None:
    """An episode that ends on ``installing`` is a gate stuck shut.

    Run with the version still unset, which is the state the ``failed`` path is
    reached in -- and the second latent StrictMode bug of the same shape:
    ``$script:InstallProgressVersion`` was read at line 1459 and assigned at
    line 2015, so even with the path fixed this record would have thrown.
    """

    config_dir = tmp_path / name
    config_dir.mkdir()
    script = tmp_path / f"harness-failed-{name}.ps1"
    body = _harness(config_dir)
    body = body.replace("$script:InstallProgressVersion = '6.71.0'\n", "")
    body = body.replace(
        "Write-InstallProgress -Stage 'done' -Message 'The new version is installed.'",
        "Write-InstallProgress -Stage 'failed' -Message 'The install failed.'",
    )
    script.write_text(body, encoding="utf-8")

    completed = _run(executable, script)
    assert completed.returncode == 0, completed.stderr

    records = _records(config_dir)
    assert records[-1]["stage"] == "failed"
    assert records[-1]["helper_done"] is True
    assert helper_is_alive(records[-1]) is False


@pytest.mark.parametrize(
    ("name", "executable"), _powershells(), ids=lambda value: value
)
def test_the_receipt_never_goes_backwards(
    name: str, executable: str, tmp_path: Path
) -> None:
    """Monotonic stages, so the window can draw them as a timeline."""

    config_dir = tmp_path / name
    config_dir.mkdir()
    script = tmp_path / f"harness-monotonic-{name}.ps1"
    body = _harness(config_dir).replace(
        "Write-InstallProgress -Stage 'done' -Message 'The new version is installed.'",
        "Write-InstallProgress -Stage 'installing' -Message 'Installing the new version.'\n"
        "Write-InstallProgress -Stage 'done' -Message 'The new version is installed.'",
    )
    script.write_text(body, encoding="utf-8")

    assert _run(executable, script).returncode == 0

    stages = [record["stage"] for record in _records(config_dir)]
    assert stages == [EPISODE_MARKER_STAGE, "installing", "verifying", "done"], stages


def test_at_least_one_powershell_was_actually_exercised() -> None:
    """A parametrisation over an empty list is a test suite that passed nothing.

    This is the shape the original bug hid behind, so it is worth an assertion
    of its own rather than a silently empty run.
    """

    assert _powershells(), "no PowerShell on PATH; the receipt was never run"


# -- the fresh-Windows-install defect this PR's own workflow surfaced ----------


NATIVE = (
    "Convert-OutputLine",
    "Format-Argument",
    "Format-Command",
    "Invoke-NativeCommand",
)


@pytest.mark.parametrize(
    ("name", "executable"), _powershells(), ids=lambda value: value
)
def test_running_a_command_does_not_return_what_it_printed(
    name: str, executable: str, tmp_path: Path
) -> None:
    """``Invoke-NativeCommand`` runs a command and SHOWS its output.

    It must not RETURN it. PowerShell returns everything a function writes to
    the output stream, so while this passed the command's two streams down the
    pipeline, ``$InstalledVersion = Install-FreeClaudeCode`` was the version
    string prefixed by every line uv printed -- an array -- and the very next
    statement, ``Configure-AndConfirmFreeClaudeCode -ExpectedVersion
    $InstalledVersion``, refused it with *Cannot convert value to type
    System.String*.

    That is every fresh Windows install through ``install.cmd`` failing at the
    verification step. It shipped in 6.64.0 and was red on main from
    2026-09-08 (install-smoke run 34285771201) with nothing to notice it,
    because that workflow only runs when ``scripts/**`` changes.
    """

    text = INSTALL_PS1.read_text(encoding="utf-8")
    config_dir = tmp_path / name
    config_dir.mkdir()
    noisy = tmp_path / "noisy.cmd"
    noisy.write_text(
        "@echo off\r\necho one\r\necho two 1>&2\r\nexit /b 0\r\n", encoding="ascii"
    )
    quiet = tmp_path / "quiet.cmd"
    quiet.write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")

    bodies = "\n\n".join(
        _extract_function(text, function) for function in NEEDED + NATIVE
    )
    lines = text.splitlines()
    initialisers = "\n".join(
        next(line for line in lines if line.startswith(f"{name} ="))
        for name in INITIALISERS
    )
    script = tmp_path / f"native-{name}.ps1"
    script.write_text(
        f"""Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$DryRun = $false
$env:MCC_CONFIG_DIR = '{config_dir}'
{initialisers}

{bodies}

$captured = @(Invoke-NativeCommand -FilePath '{noisy}' -Arguments @() -CaptureTo '{tmp_path / "capture.txt"}')
Write-Output ("CAPTURED=" + $captured.Count)
$bare = @(Invoke-NativeCommand -FilePath '{noisy}' -Arguments @())
Write-Output ("BARE=" + $bare.Count)
$silent = @(Invoke-NativeCommand -FilePath '{quiet}' -Arguments @() -CaptureTo '{tmp_path / "capture2.txt"}')
Write-Output ("QUIET=" + $silent.Count)
""",
        encoding="utf-8",
    )

    completed = _run(executable, script)
    assert completed.returncode == 0, completed.stderr
    assert "CAPTURED=0" in completed.stdout, completed.stdout
    assert "BARE=0" in completed.stdout, completed.stdout
    assert "QUIET=0" in completed.stdout, completed.stdout
    # ...and the user still watches the install happen: the command's output is
    # shown, it is merely not returned.
    assert "one" in completed.stdout, completed.stdout
    assert "two" in completed.stdout, completed.stdout
    # The capture file the failure classifier reads is still written. Read it
    # leniently: PowerShell 5.1's Tee-Object writes UTF-16LE and pwsh 7 writes
    # UTF-8, and this test is about what was RETURNED, not about the encoding.
    captured = (tmp_path / "capture.txt").read_bytes()
    assert b"one" in captured.replace(b"\x00", b""), captured[:80]


@pytest.mark.parametrize(
    ("name", "executable"), _powershells(), ids=lambda value: value
)
def test_the_install_returns_one_version_and_not_a_transcript(
    name: str, executable: str, tmp_path: Path
) -> None:
    """The exact statement that failed, with everything around it stubbed.

    ``$InstalledVersion = Install-FreeClaudeCode`` followed by
    ``Configure-AndConfirmFreeClaudeCode -ExpectedVersion $InstalledVersion``,
    where that parameter is ``[string]``. When the install's `uv` printed
    anything -- and it always does -- the assignment produced an array and the
    call died with *Cannot process argument transformation on parameter
    'ExpectedVersion'*.

    The CI job that catches this in the wild (``install.cmd on Windows``)
    downloads ``install.ps1`` from ``main``, so it can never go green on a
    branch and it cannot prove a fix before the fix is merged. This can.
    """

    text = INSTALL_PS1.read_text(encoding="utf-8")
    config_dir = tmp_path / name
    config_dir.mkdir()
    tool_bin = tmp_path / "bin"
    tool_bin.mkdir()
    noisy = tmp_path / "uv.cmd"
    noisy.write_text(
        "@echo off\r\n"
        "echo Resolved 87 packages in 1.43s 1>&2\r\n"
        "echo Installed 26 executables 1>&2\r\n"
        "exit /b 0\r\n",
        encoding="ascii",
    )

    bodies = "\n\n".join(
        _extract_function(text, function)
        for function in (*NEEDED, *NATIVE, "Get-InstallPlan", "Install-FreeClaudeCode")
    )
    lines = text.splitlines()
    initialisers = "\n".join(
        next(line for line in lines if line.startswith(f"{name} ="))
        for name in INITIALISERS
    )
    script = tmp_path / f"install-return-{name}.ps1"
    script.write_text(
        f"""Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$DryRun = $false
$TorchBackend = ''
$PythonVersion = '3.14.0'
$env:MCC_CONFIG_DIR = '{config_dir}'
{initialisers}

# Everything Install-FreeClaudeCode leans on, stubbed. The real uv is the
# `uv.cmd` above, which writes to stderr exactly as uv does.
function Resolve-Release {{ return [pscustomobject]@{{ Version = '6.71.0' }} }}
function Get-VerifiedReleaseWheel {{ param($Release) return '{tmp_path.as_posix()}/w.whl' }}
function Get-PackageSpec {{ param([string] $PackageUrl) return 'my-claude-code @ ' + $PackageUrl }}
function Resolve-UvPath {{ param([string] $Purpose) return '{noisy}' }}
function Get-RunningLaunchers {{ return @() }}
function New-CapturePath {{ return '{tmp_path.as_posix()}/capture.txt' }}
function Read-CapturedOutput {{ param([string] $Path) return '' }}
function Get-UvFailureCategory {{ param([string] $Text) return 'unknown' }}
function Get-UvToolDir {{ param([string] $UvPath) return '{tmp_path.as_posix()}/tool' }}
function Write-Step {{ param([string] $Message) }}
# The verification step, with the SIGNATURE that matters: [string].
function Configure-AndConfirmFreeClaudeCode {{
    param([Parameter(Mandatory = $true)] [string] $ExpectedVersion)
    Write-Output ("VERIFIED=" + $ExpectedVersion)
}}

{bodies}

New-Item -ItemType File -Path '{tmp_path.as_posix()}/w.whl' -Force | Out-Null
$InstalledVersion = Install-FreeClaudeCode
Write-Output ("COUNT=" + @($InstalledVersion).Count)
Configure-AndConfirmFreeClaudeCode -ExpectedVersion $InstalledVersion
""",
        encoding="utf-8",
    )

    completed = _run(executable, script)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "COUNT=1" in completed.stdout, (
        "Install-FreeClaudeCode returned uv's output alongside the version:\n"
        + completed.stdout
    )
    assert "VERIFIED=6.71.0" in completed.stdout, completed.stdout


# -- 6.82.0: the stage order of a restart run ---------------------------------


@pytest.mark.parametrize(
    ("name", "executable"), _powershells(), ids=lambda value: value
)
def test_the_installer_emits_every_stage_of_a_restart_run(
    name: str, executable: str, tmp_path: Path
) -> None:
    """Decision Q8 + §6: the whole sequence reaches the receipt, in order.

    This is a measured defect, not a hypothetical. V1's `install.ps1 -Restart`
    wrote `installing` (rank 4), then `verifying` (rank 5), and only THEN
    `stopping` (rank 3) from inside Invoke-RestartAfterInstall -- so the
    monotonic guard, which exists so a window can draw the records as a
    timeline, silently threw the `stopping` record away. No restart run has
    ever recorded one, and the window that was supposed to be showing
    "Stopping the server..." never had the fact to show.

    6.82.0's order is the update helper's, and the ranks rise all the way:
    staging (2) -> stopping (3) -> verifying (5) -> swapping (6) ->
    starting (7) -> done (9).
    """

    config_dir = tmp_path / name
    config_dir.mkdir()
    script = tmp_path / f"harness-stages-{name}.ps1"
    body = _harness(config_dir).replace(
        f"Write-InstallProgress -Stage 'installing' -Message '{INSTALLING_MESSAGE}'\n"
        "Write-InstallLog 'uv tool install --force'\n"
        "Write-InstallProgress -Stage 'verifying' -Message "
        "'Checking that every command is in place.'\n"
        "Write-InstallProgress -Stage 'done' -Message 'The new version is installed.'",
        "\n".join(
            (
                "Write-InstallProgress -Stage 'staging' -Message "
                "'Building the new version beside the running one.'",
                "Write-InstallProgress -Stage 'stopping' -Message "
                "'Stopping the server on port 8391.'",
                "Write-InstallProgress -Stage 'verifying' -Message "
                "'Running the new version once before it replaces the old one.'",
                "Write-InstallProgress -Stage 'swapping' -Message "
                "'Putting the new version in place.'",
                "Write-InstallProgress -Stage 'starting' -Message "
                "'Starting My Claude Code 6.82.0.'",
                "Write-InstallProgress -Stage 'done' -Message "
                "'My Claude Code 6.82.0 is installed and answering on port 8391.'",
            )
        ),
    )
    script.write_text(body, encoding="utf-8")

    assert _run(executable, script).returncode == 0

    records = _records(config_dir)
    stages = [record["stage"] for record in records]
    assert stages == [
        EPISODE_MARKER_STAGE,
        "staging",
        "stopping",
        "verifying",
        "swapping",
        "starting",
        "done",
    ], stages
    # And `version` is filled from the moment it is known, so a watcher can say
    # WHICH version is being installed rather than an empty string.
    assert all(record["version"] for record in records[1:])
    assert records[-1]["helper_done"] is True


@pytest.mark.parametrize(
    ("name", "executable"), _powershells(), ids=lambda value: value
)
def test_the_v1_stage_order_really_did_lose_the_stopping_record(
    name: str, executable: str, tmp_path: Path
) -> None:
    """The defect above, reproduced, so the fix cannot be undone by accident.

    A test that cannot fail is not a test: this one runs the ORDER V1 used and
    asserts the loss, which is the only way to know the new order is doing
    something.
    """

    config_dir = tmp_path / name
    config_dir.mkdir()
    script = tmp_path / f"harness-v1order-{name}.ps1"
    body = _harness(config_dir).replace(
        "Write-InstallProgress -Stage 'done' -Message 'The new version is installed.'",
        "Write-InstallProgress -Stage 'stopping' -Message 'Stopping the server.'\n"
        "Write-InstallProgress -Stage 'done' -Message 'The new version is installed.'",
    )
    script.write_text(body, encoding="utf-8")

    assert _run(executable, script).returncode == 0
    stages = [record["stage"] for record in _records(config_dir)]
    assert "stopping" not in stages, (
        "the monotonic guard is what dropped it; if this ever passes a "
        "`stopping` through, the guard is gone and the timeline can go "
        "backwards"
    )
