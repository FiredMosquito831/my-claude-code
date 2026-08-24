"""Both uninstallers must agree with pyproject about what MCC installs.

MCC v5 renamed the distribution to ``my-claude-code``, but the uninstallers
kept calling ``uv tool uninstall free-claude-code``. uv reports that as
"tool not installed", which the idempotency branch swallows, and the
following entry-point check then aborted on the 26 shims the renamed wheel
still publishes -- so no v5 install could ever finish uninstalling. Shell
scripts cannot import ``pyproject.toml``, so both command families and both
package names are embedded directly in each uninstaller; this contract makes
any future drift fail here instead of on a user's machine, in both
directions, exactly like test_installer_knows_every_launcher.py does for
install.ps1.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
UNINSTALL_SH = REPO_ROOT / "scripts" / "uninstall.sh"
UNINSTALL_PS1 = REPO_ROOT / "scripts" / "uninstall.ps1"

_SH_VARIABLE = re.compile(
    r'^(?P<name>PACKAGE_NAME|LEGACY_PACKAGE_NAME|FCC_COMMANDS)="(?P<body>[^"]*)"$',
    re.MULTILINE,
)
_PS1_STRING = re.compile(
    r'\$(?P<name>PackageName|LegacyPackageName)\s*=\s*"(?P<body>[^"]*)"'
)
_PS1_ARRAY = re.compile(
    r"\$(?P<name>FccCommands|GuardProcessImages)\s*=\s*@\((?P<body>.*?)\)",
    re.DOTALL,
)
_QUOTED = re.compile(r'"([A-Za-z0-9_-]+)"')


def _load_pyproject() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _published_commands() -> set[str]:
    project = _load_pyproject()["project"]
    return set(project.get("scripts", {})) | set(project.get("gui-scripts", {}))


def _sh_variables() -> dict[str, str]:
    return {
        match.group("name"): match.group("body")
        for match in _SH_VARIABLE.finditer(UNINSTALL_SH.read_text(encoding="utf-8"))
    }


def _ps1_values() -> dict[str, str]:
    text = UNINSTALL_PS1.read_text(encoding="utf-8")
    values = {
        match.group("name"): match.group("body") for match in _PS1_STRING.finditer(text)
    }
    values.update(
        {
            match.group("name"): match.group("body")
            for match in _PS1_ARRAY.finditer(text)
        }
    )
    return values


def _sh_commands(variables: dict[str, str]) -> set[str]:
    return set(variables["FCC_COMMANDS"].split())


def _ps1_commands(values: dict[str, str]) -> set[str]:
    return set(_QUOTED.findall(values["FccCommands"]))


def test_the_embedded_lists_are_parsable() -> None:
    """A silent regex miss would make every assertion below vacuous."""

    sh_variables = _sh_variables()
    ps1_values = _ps1_values()

    assert "FCC_COMMANDS" in sh_variables, (
        "could not parse FCC_COMMANDS from scripts/uninstall.sh -- if the declaration moved or changed shape, update this parser rather than deleting the guard"
    )
    sh_commands = _sh_commands(sh_variables)
    assert sh_commands, "parsed no commands out of uninstall.sh"
    assert "mcc-server" in sh_commands

    assert "FccCommands" in ps1_values, (
        "could not parse $FccCommands from scripts/uninstall.ps1 -- if the declaration moved or changed shape, update this parser rather than deleting the guard"
    )
    ps1_commands = _ps1_commands(ps1_values)
    assert ps1_commands, "parsed no commands out of uninstall.ps1"
    assert "mcc-server" in ps1_commands
    assert "GuardProcessImages" in ps1_values


def test_every_published_command_appears_in_both_uninstallers() -> None:
    """A command an uninstaller cannot see keeps its shim behind.

    The entry-point verification then aborts and ~/.fcc is left standing --
    the exact failure that made v5 uninstalls impossible.
    """

    published = _published_commands()

    missing_from_shell = sorted(published - _sh_commands(_sh_variables()))
    missing_from_powershell = sorted(published - _ps1_commands(_ps1_values()))

    assert not missing_from_shell, (
        "these commands are published in pyproject.toml but are not in "
        f"FCC_COMMANDS in scripts/uninstall.sh: {missing_from_shell}. Their "
        "shims survive the tool uninstall, verification aborts, and ~/.fcc "
        "is never removed. Add each name to that list."
    )
    assert not missing_from_powershell, (
        "these commands are published in pyproject.toml but are not in "
        f"$FccCommands in scripts/uninstall.ps1: {missing_from_powershell}. "
        "Their shims survive the tool uninstall, verification aborts, and "
        "~/.fcc is never removed. Add each name to that array."
    )


def test_the_uninstaller_lists_name_no_command_pyproject_does_not_publish() -> None:
    """A stale name verifies nothing and hides a rename."""

    published = _published_commands()

    unknown_in_shell = sorted(_sh_commands(_sh_variables()) - published)
    unknown_in_powershell = sorted(_ps1_commands(_ps1_values()) - published)

    assert not unknown_in_shell, (
        "FCC_COMMANDS in scripts/uninstall.sh lists commands pyproject.toml "
        f"no longer publishes: {unknown_in_shell}. Remove them, or restore "
        "the entry point if the removal was accidental."
    )
    assert not unknown_in_powershell, (
        "$FccCommands in scripts/uninstall.ps1 lists commands pyproject.toml "
        f"no longer publishes: {unknown_in_powershell}. Remove them, or "
        "restore the entry point if the removal was accidental."
    )


def test_both_uninstallers_target_the_published_package_name() -> None:
    """uv installs and uninstalls by [project].name -- anything else no-ops.

    This exact mismatch (uninstalling free-claude-code while the wheel is
    my-claude-code) made every v5 uninstall abort with entry points still
    present, so guarding against it is not hypothetical.
    """

    name = str(_load_pyproject()["project"]["name"])
    shell_name = _sh_variables()["PACKAGE_NAME"]
    powershell_name = _ps1_values()["PackageName"]

    assert shell_name == powershell_name == name == "my-claude-code", (
        f"uninstall.sh removes {shell_name!r}, uninstall.ps1 removes "
        f"{powershell_name!r}, pyproject.toml publishes {name!r}: all of "
        "these must match or uv silently no-ops the uninstall. If the "
        "distribution was renamed on purpose, update pyproject.toml, both "
        "uninstallers, and this assertion together."
    )


def test_both_uninstallers_keep_the_legacy_cleanup_target() -> None:
    """Pre-5.14 installs were published as free-claude-code.

    The uninstaller must still clean those up, without ever confusing that
    historical name with the current distribution name.
    """

    current = str(_load_pyproject()["project"]["name"])
    shell_legacy = _sh_variables()["LEGACY_PACKAGE_NAME"]
    powershell_legacy = _ps1_values()["LegacyPackageName"]

    assert shell_legacy == powershell_legacy == "free-claude-code", (
        f"the legacy cleanup target drifted: uninstall.sh has "
        f"{shell_legacy!r}, uninstall.ps1 has {powershell_legacy!r}; both "
        "must stay free-claude-code so pre-5.14 installs keep uninstalling"
    )
    assert shell_legacy != current


def test_windows_guard_covers_gui_script_process_images() -> None:
    """mcc-desktop/fcc-desktop run as pythonw.exe out of the tool env.

    Get-Process -Name mcc-desktop can never see such a process, so the
    guard must also look at interpreter image names -- otherwise a running
    desktop app holds the tool directory open while the uninstaller deletes
    shims out from under it and purges ~/.fcc anyway.
    """

    published = _published_commands()
    images = set(_QUOTED.findall(_ps1_values()["GuardProcessImages"]))

    assert "pythonw" in images, (
        "GuardProcessImages in scripts/uninstall.ps1 must include pythonw: "
        "gui-script processes are invisible under their launcher names, and "
        "a live pythonw.exe makes the tool directory undeletable."
    )
    assert images.isdisjoint(published), (
        "GuardProcessImages duplicates launcher names already covered by "
        f"FccCommands: {sorted(images & published)}. Keep interpreter image "
        "names separate from the parity-checked launcher list."
    )
