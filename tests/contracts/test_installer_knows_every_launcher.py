"""The Windows installer must know every command that can hold a shim open.

`install.ps1` cannot replace a launcher's `.exe` while that launcher is
running, so it detects running launchers and defers. That detection reads a
hand-maintained list of command names, and the list silently fell four
features behind `pyproject.toml`: `mcc-desktop`, `mcc-rtk`, `mcc-help` and
`mcc-anthropic-oauth-login` were published as commands and never added.

The consequence was not cosmetic. `mcc-desktop` is a `gui-scripts` entry whose
process runs `pythonw.exe` out of the uv tool environment, so a running desktop
app holds the tool directory open. Invisible to the detection, the installer
took the direct path, `uv tool install --force` tried to delete a live
environment, and Windows refused with "Access is denied (os error 5)" -- leaving
the tool environment half-removed and every shim pointing at nothing.

A list that must mirror another file is a list that drifts. This makes the
drift fail here instead of on a user's machine.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

_LAUNCHER_BLOCK = re.compile(
    r"function Get-LauncherCommands.*?return @\((?P<body>.*?)\)", re.DOTALL
)
_QUOTED = re.compile(r'"([A-Za-z0-9_-]+)"')


def _published_commands() -> set[str]:
    """Every console and GUI command the wheel installs."""

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    return set(project.get("scripts", {})) | set(project.get("gui-scripts", {}))


def _detected_launchers() -> set[str]:
    match = _LAUNCHER_BLOCK.search(INSTALL_PS1.read_text(encoding="utf-8"))
    assert match is not None, (
        "could not find Get-LauncherCommands' return @(...) block in "
        "scripts/install.ps1 -- if the function was restructured, update this "
        "parser rather than deleting the guard"
    )
    return set(_QUOTED.findall(match.group("body")))


def test_the_launcher_block_is_parsable() -> None:
    """A silent parse failure would make the assertion below vacuous."""

    detected = _detected_launchers()

    assert detected, "parsed no command names out of Get-LauncherCommands"
    assert "mcc-server" in detected


def test_every_published_command_is_detected_as_a_running_launcher() -> None:
    """A command the installer cannot see is a lock it cannot avoid."""

    missing = sorted(_published_commands() - _detected_launchers())

    assert not missing, (
        "these commands are published in pyproject.toml but are not in "
        f"Get-LauncherCommands in scripts/install.ps1: {missing}. A running "
        "one is invisible to the installer, so uv will try to delete the tool "
        "environment underneath it and fail with os error 5 or 32. Add each "
        "name to that list."
    )


def test_the_detection_list_names_no_command_that_does_not_exist() -> None:
    """A stale name is dead weight and hides a rename."""

    unknown = sorted(_detected_launchers() - _published_commands())

    assert not unknown, (
        "Get-LauncherCommands in scripts/install.ps1 lists commands that "
        f"pyproject.toml no longer publishes: {unknown}. Remove them, or "
        "restore the entry point if the removal was accidental."
    )


def test_the_installer_strips_ansi_before_using_captured_output() -> None:
    """uv colours its output when it thinks stdout is a terminal.

    PowerShell's capture looks like a terminal to uv while POSIX ``$(...)``
    looks like a pipe, which is why this only ever broke Windows.
    ``uv tool dir --bin`` returned ESC[36m + path + ESC[39m, so the value was
    35 characters where the directory name is 25: ``Test-Path`` failed and the
    "did this command resolve inside the tool bin?" check could never pass.
    """

    source = INSTALL_PS1.read_text(encoding="utf-8")

    assert "function Remove-AnsiEscape" in source, (
        "scripts/install.ps1 must define Remove-AnsiEscape; without it a "
        "coloured `uv tool dir --bin` breaks every path comparison on Windows"
    )
    capture = re.search(r"function Invoke-NativeCapture.*?\n\}", source, re.DOTALL)
    assert capture is not None, "Invoke-NativeCapture not found"
    assert "Remove-AnsiEscape" in capture.group(0), (
        "Invoke-NativeCapture must strip ANSI escapes from captured output "
        "before returning it"
    )


def test_the_windows_verification_enumerates_every_launcher_command() -> None:
    """Verification must cover the same list, or it lies about what installed.

    `Configure-AndConfirmFreeClaudeCode` used to check a second, shorter,
    hand-written list -- and skipped it entirely whenever a shim could not be
    replaced, verifying the install with `mcc-server --version` alone. That
    check cannot fail: the shims are version-agnostic launchers, so an OLD shim
    reports the NEW version. A user was told "installed and verified" while
    seven of their commands did not exist. Driving verification from
    `Get-LauncherCommands` leaves exactly one list to keep in step with
    pyproject.toml, and the tests above are what keep it in step.
    """

    source = INSTALL_PS1.read_text(encoding="utf-8")
    confirm = re.search(
        r"function Configure-AndConfirmFreeClaudeCode.*?\n\}", source, re.DOTALL
    )
    assert confirm is not None, "Configure-AndConfirmFreeClaudeCode not found"
    body = confirm.group(0)

    assert "foreach ($commandName in Get-LauncherCommands)" in body, (
        "the Windows verification must enumerate Get-LauncherCommands, not a "
        "second hand-written list that can fall behind pyproject.toml"
    )
    assert "$missingCommands" in body, (
        "verification must collect every missing command, not stop at the first"
    )
    assert "Installed, but these commands are missing:" in body
    assert "Close the mcc-claude window(s) and re-run the install command." in body
    assert "exit 1" in body, "a missing command must exit non-zero"


def test_the_posix_verification_covers_every_native_command() -> None:
    """install.sh must not quietly verify a shorter list than it installs."""

    published = _published_commands()
    source = INSTALL_SH.read_text(encoding="utf-8")
    block = re.search(r"for command_name in (?P<body>.*?); do", source, re.DOTALL)
    assert block is not None, "install.sh verification loop not found"
    # Line continuations are shell syntax, not command names.
    verified = {word for word in block.group("body").split() if word != "\\"}

    unknown = sorted(verified - published)
    assert not unknown, (
        f"scripts/install.sh verifies commands pyproject.toml does not "
        f"publish: {unknown}"
    )
    native = {name for name in published if name.startswith("mcc-")}
    native.add("my-claude-code")
    missing = sorted(native - verified)
    assert not missing, (
        "scripts/install.sh installs these commands but never checks they "
        f"exist: {missing}. A command it does not check is a command it can "
        "report as verified while it is absent."
    )


def test_the_posix_verification_reports_every_missing_command() -> None:
    """Stopping at the first miss hides how much of the install is absent."""

    source = INSTALL_SH.read_text(encoding="utf-8")

    assert "missing_commands" in source
    assert "Installed, but these commands are missing: %s" in source
