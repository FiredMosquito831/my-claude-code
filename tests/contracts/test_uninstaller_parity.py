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

The second half of this file covers the *other* half of parity: the artefacts
the installers write outside the uv tool directory. ``uv tool uninstall``
removes shims and nothing else, and purging ~/.mcc / ~/.fcc reaches only what
is inside the config directory -- so the Start Menu shortcut, the ``.desktop``
entry and its icon, the macOS ``.app`` bundle, the LaunchAgent plist, the XDG
autostart entry, the systemd user unit and the HKCU ``Run`` value all used to
survive an uninstall. ARTEFACTS below enumerates every one of them together
with the code that creates it and the code that removes it, so adding a
creator without a remover fails here.

The third half -- there are three -- is the Windows *desktop app* installer
(``desktop-shell/installer/windows/MyClaudeCode.iss``, spec S5). It is not a
row in ARTEFACTS because it is self-removing: Inno Setup records every
``[Files]`` and ``[Icons]`` entry into its own uninstall log and deletes them
on uninstall, so parity there means "nothing opts *out* of that", not "a
second script names each file". The ``.iss`` assertions at the bottom of this
file check exactly that, plus the boundary that matters more: the desktop
app's uninstaller must not reach into the *server's* artefacts. Removing the
window is not removing My Claude Code.

The fourth -- there are four -- is the macOS ``.dmg`` (spec S9), and it is the
sharpest case of the same boundary. ``install.sh --desktop`` writes a launcher
bundle at ``~/Applications/My Claude Code.app``; the ``.dmg`` puts a *real*
application with the same display name at ``/Applications/My Claude Code.app``,
and a user may drag it into ``~/Applications`` instead. Two different programs,
one path, one name. The only thing that tells them apart is the
``CFBundleIdentifier``, so the tests at the bottom of this file pin both
identifiers, pin that they differ, pin that the installer steps aside when it
finds the app, and pin that the uninstaller names the app instead of deleting
it.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
UNINSTALL_SH = REPO_ROOT / "scripts" / "uninstall.sh"
UNINSTALL_PS1 = REPO_ROOT / "scripts" / "uninstall.ps1"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
DESKTOP_CONFIG = REPO_ROOT / "src" / "my_claude_code" / "config" / "desktop.py"
INNO_SCRIPT = REPO_ROOT / "desktop-shell" / "installer" / "windows" / "MyClaudeCode.iss"
DESKTOP_SHELL_CONFIG = (
    REPO_ROOT / "src" / "my_claude_code" / "config" / "desktop_shell.py"
)
LINUX_INSTALLER_DIR = REPO_ROOT / "desktop-shell" / "installer" / "linux"
BUILD_DEB = LINUX_INSTALLER_DIR / "build-deb.sh"
INSTALL_DESKTOP_SH = LINUX_INSTALLER_DIR / "install-desktop.sh"
LINUX_DESKTOP_ENTRY_TEMPLATE = LINUX_INSTALLER_DIR / "my-claude-code-desktop.desktop"
SHELL_CARGO_TOML = REPO_ROOT / "desktop-shell" / "src-tauri" / "Cargo.toml"
SMOKE_DIR = REPO_ROOT / "desktop-shell" / "smoke"
LINUX_DEB_SMOKE = SMOKE_DIR / "linux-deb.sh"
MACOS_INSTALLER_DIR = REPO_ROOT / "desktop-shell" / "installer" / "macos"
BUILD_APP = MACOS_INSTALLER_DIR / "build-app.sh"
BUILD_DMG = MACOS_INSTALLER_DIR / "build-dmg.sh"
MACOS_DMG_SMOKE = SMOKE_DIR / "macos-dmg.sh"


@dataclass(frozen=True)
class Artefact:
    """One thing an install creates outside the uv tool bin directory."""

    name: str
    #: Where the artefact ends up, for the failure message.
    location: str
    #: The file that creates it, and text proving it still does.
    creator: Path
    creator_markers: tuple[str, ...]
    #: The uninstaller that must remove it, and text proving it does.
    remover: Path
    remover_markers: tuple[str, ...]


ARTEFACTS: tuple[Artefact, ...] = (
    Artefact(
        name="Start Menu shortcut",
        location=r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\My Claude Code.lnk",
        creator=INSTALL_PS1,
        creator_markers=(
            'Join-Path $env:APPDATA "Microsoft\\Windows\\Start Menu\\Programs"',
            'Join-Path $startMenuDir "My Claude Code.lnk"',
        ),
        remover=UNINSTALL_PS1,
        remover_markers=(
            '$StartMenuRelativeDir = "Microsoft\\Windows\\Start Menu\\Programs"',
            '$StartMenuShortcutName = "My Claude Code.lnk"',
            "function Remove-StartMenuShortcut {",
            "Remove-Item -LiteralPath $shortcutPath -Force",
        ),
    ),
    Artefact(
        name="Windows start-at-login registration",
        location=r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run\MyClaudeCodeDesktop",
        creator=DESKTOP_CONFIG,
        creator_markers=(
            'WINDOWS_RUN_VALUE = "MyClaudeCodeDesktop"',
            r'return r"Software\Microsoft\Windows\CurrentVersion\Run"',
        ),
        remover=UNINSTALL_PS1,
        remover_markers=(
            '$WindowsRunKeyPath = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"',
            '$WindowsRunValueName = "MyClaudeCodeDesktop"',
            "function Remove-StartAtLoginRegistration {",
            "Remove-ItemProperty -LiteralPath $WindowsRunKeyPath -Name $WindowsRunValueName -Force",
        ),
    ),
    Artefact(
        name="Windows shortcut icon",
        location="~/.mcc/app-icon.ico (inside the config directory)",
        creator=INSTALL_PS1,
        creator_markers=('$iconPath = Join-Path $configDir "app-icon.ico"',),
        # Not removed on its own: it lives inside ~/.mcc, which Purge-FccHome
        # deletes whole. This row exists so a future icon written OUTSIDE the
        # config directory cannot slip through unnoticed.
        remover=UNINSTALL_PS1,
        remover_markers=('Purge-ConfigDir -DirName ".mcc"',),
    ),
    Artefact(
        name="Linux .desktop entry",
        location="~/.local/share/applications/my-claude-code.desktop",
        creator=INSTALL_SH,
        creator_markers=(
            'desktop_file="$applications_dir/my-claude-code.desktop"',
            'applications_dir="$HOME/.local/share/applications"',
        ),
        remover=UNINSTALL_SH,
        remover_markers=(
            'LINUX_DESKTOP_ENTRY=".local/share/applications/my-claude-code.desktop"',
            'remove_home_path "$LINUX_DESKTOP_ENTRY"',
        ),
    ),
    Artefact(
        name="Linux .desktop icon",
        location="~/.local/share/icons/hicolor/256x256/apps/my-claude-code.png",
        creator=INSTALL_SH,
        creator_markers=(
            'icons_dir="$HOME/.local/share/icons/hicolor/256x256/apps"',
            'icon_path="$icons_dir/my-claude-code.png"',
        ),
        remover=UNINSTALL_SH,
        remover_markers=(
            'LINUX_DESKTOP_ICON=".local/share/icons/hicolor/256x256/apps/my-claude-code.png"',
            'remove_home_path "$LINUX_DESKTOP_ICON"',
        ),
    ),
    Artefact(
        name="macOS .app bundle",
        location="~/Applications/My Claude Code.app",
        creator=INSTALL_SH,
        creator_markers=('app_dir="$HOME/Applications/My Claude Code.app"',),
        remover=UNINSTALL_SH,
        remover_markers=(
            'MACOS_APP_BUNDLE="Applications/My Claude Code.app"',
            'remove_home_path "$MACOS_APP_BUNDLE"',
        ),
    ),
    Artefact(
        name="macOS LaunchAgent plist",
        location="~/Library/LaunchAgents/com.myclaudecode.tray.plist",
        creator=DESKTOP_CONFIG,
        creator_markers=(
            'LAUNCH_AGENT_LABEL = "com.myclaudecode.tray"',
            'Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"',
        ),
        remover=UNINSTALL_SH,
        remover_markers=(
            'MACOS_LAUNCH_AGENT="Library/LaunchAgents/com.myclaudecode.tray.plist"',
            'remove_home_path "$MACOS_LAUNCH_AGENT"',
        ),
    ),
    Artefact(
        name="Linux XDG autostart entry",
        location="~/.config/autostart/mcc-server.desktop",
        creator=DESKTOP_CONFIG,
        creator_markers=(
            'LINUX_AUTOSTART_ID = "mcc-server"',
            'Path.home() / ".config" / "autostart" / f"{LINUX_AUTOSTART_ID}.desktop"',
        ),
        remover=UNINSTALL_SH,
        remover_markers=(
            'LINUX_AUTOSTART_ENTRY=".config/autostart/mcc-server.desktop"',
            'remove_home_path "$LINUX_AUTOSTART_ENTRY"',
        ),
    ),
    Artefact(
        name="desktop app binary (delivery path A)",
        location="~/.local/bin/MyClaudeCode",
        creator=DESKTOP_SHELL_CONFIG,
        creator_markers=(
            'DESKTOP_SHELL_BINARY_STEM = "MyClaudeCode"',
            'return Path.home() / ".local" / "bin"',
        ),
        remover=UNINSTALL_SH,
        remover_markers=(
            'DESKTOP_APP_BINARY=".local/bin/MyClaudeCode"',
            'remove_home_path "$DESKTOP_APP_BINARY"',
        ),
    ),
    Artefact(
        name="desktop app install receipt (delivery path A)",
        location="~/.local/bin/MyClaudeCode.receipt.json",
        creator=DESKTOP_SHELL_CONFIG,
        creator_markers=(
            'DESKTOP_SHELL_RECEIPT_FILENAME = "MyClaudeCode.receipt.json"',
            "def _write_receipt(",
        ),
        remover=UNINSTALL_SH,
        remover_markers=(
            'DESKTOP_APP_RECEIPT=".local/bin/MyClaudeCode.receipt.json"',
            'remove_home_path "$DESKTOP_APP_RECEIPT"',
        ),
    ),
    Artefact(
        name="desktop app binary (tarball installer)",
        location="~/.local/bin/MyClaudeCode",
        creator=INSTALL_DESKTOP_SH,
        creator_markers=(
            'BINARY_NAME="MyClaudeCode"',
            'binary_path="$bin_dir/$BINARY_NAME"',
        ),
        remover=UNINSTALL_SH,
        remover_markers=(
            'DESKTOP_APP_BINARY=".local/bin/MyClaudeCode"',
            'remove_home_path "$DESKTOP_APP_BINARY"',
        ),
    ),
    Artefact(
        name="desktop app tarball receipt",
        location="~/.local/bin/MyClaudeCode.tarball.receipt.json",
        creator=INSTALL_DESKTOP_SH,
        creator_markers=(
            'RECEIPT_NAME="MyClaudeCode.tarball.receipt.json"',
            'receipt_path="$bin_dir/$RECEIPT_NAME"',
        ),
        remover=UNINSTALL_SH,
        remover_markers=(
            'DESKTOP_APP_TARBALL_RECEIPT=".local/bin/MyClaudeCode.tarball.receipt.json"',
            'remove_home_path "$DESKTOP_APP_TARBALL_RECEIPT"',
        ),
    ),
    Artefact(
        name="desktop app .desktop entry (tarball installer)",
        location="~/.local/share/applications/my-claude-code-desktop.desktop",
        creator=INSTALL_DESKTOP_SH,
        creator_markers=(
            'ENTRY_NAME="my-claude-code-desktop.desktop"',
            'entry_path="$applications_dir/$ENTRY_NAME"',
        ),
        remover=UNINSTALL_SH,
        remover_markers=(
            'DESKTOP_APP_ENTRY=".local/share/applications/my-claude-code-desktop.desktop"',
            'remove_home_path "$DESKTOP_APP_ENTRY"',
        ),
    ),
    Artefact(
        name="desktop app icons (tarball installer)",
        location="~/.local/share/icons/hicolor/<size>/apps/my-claude-code-desktop.png",
        creator=INSTALL_DESKTOP_SH,
        creator_markers=(
            'ICON_NAME="my-claude-code-desktop"',
            'cp "$source_icon" "$icons_root/$size/apps/$ICON_NAME.png"',
        ),
        remover=UNINSTALL_SH,
        remover_markers=(
            'DESKTOP_APP_ICON_NAME="my-claude-code-desktop"',
            'remove_home_path "$DESKTOP_APP_ICONS_ROOT/$_size/apps/$DESKTOP_APP_ICON_NAME.png"',
        ),
    ),
    Artefact(
        name="Linux systemd user unit",
        location="~/.config/systemd/user/mcc-server.service",
        creator=DESKTOP_CONFIG,
        creator_markers=(
            'LINUX_SYSTEMD_UNIT = "mcc-server.service"',
            'Path.home() / ".config" / "systemd" / "user" / LINUX_SYSTEMD_UNIT',
        ),
        remover=UNINSTALL_SH,
        remover_markers=(
            'LINUX_SYSTEMD_UNIT=".config/systemd/user/mcc-server.service"',
            'LINUX_SYSTEMD_UNIT_NAME="mcc-server.service"',
            'remove_home_path "$LINUX_SYSTEMD_UNIT"',
            'systemctl --user disable --now "$LINUX_SYSTEMD_UNIT_NAME"',
        ),
    ),
)

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


def test_every_artefact_row_names_a_creator_that_still_creates_it() -> None:
    """A stale creator marker makes the removal assertion below vacuous."""

    stale: list[str] = []
    for artefact in ARTEFACTS:
        source = artefact.creator.read_text(encoding="utf-8")
        stale.extend(
            f"{artefact.name}: {artefact.creator.name} no longer contains {marker!r}"
            for marker in artefact.creator_markers
            if marker not in source
        )

    assert not stale, (
        "the installer-artefact table in this file has drifted from the code "
        f"that creates the artefacts: {stale}. Update the markers (and the "
        "matching uninstaller) rather than deleting the row -- a row removed "
        "here is an artefact nobody checks any more."
    )


def test_every_installer_created_artefact_is_removed_by_an_uninstaller() -> None:
    """uv tool uninstall removes shims; everything else must be removed here.

    The Start Menu shortcut, the .desktop entry, the .app bundle and the three
    autostart registrations all live outside both the tool bin directory and
    the config directory, so nothing in the old uninstall path ever touched
    them: uninstalling MCC left a launcher pointing at a deleted shim and an
    autostart entry relaunching a package that was gone.
    """

    unremoved: list[str] = []
    for artefact in ARTEFACTS:
        source = artefact.remover.read_text(encoding="utf-8")
        unremoved.extend(
            f"{artefact.name} ({artefact.location}) is created by "
            f"{artefact.creator.name} but {artefact.remover.name} does not "
            f"contain {marker!r}"
            for marker in artefact.remover_markers
            if marker not in source
        )

    assert not unremoved, (
        "these installer-created artefacts survive an uninstall: "
        f"{unremoved}. Every artefact an installer writes outside the uv tool "
        "bin directory must be removed by the matching uninstaller."
    )


def test_the_artefact_table_covers_both_platforms() -> None:
    """A one-sided table would let the other platform rot unnoticed."""

    removers = {artefact.remover for artefact in ARTEFACTS}

    assert UNINSTALL_PS1 in removers
    assert UNINSTALL_SH in removers


def test_neither_uninstaller_touches_the_retired_rollback_directory() -> None:
    """~/.fcc-old holds the user's rollback note and is deliberately kept.

    The desktop-artefact cleanup added a second class of removals; this pins
    the purge semantics that were already settled so they cannot drift with it.
    """

    shell = UNINSTALL_SH.read_text(encoding="utf-8")
    powershell = UNINSTALL_PS1.read_text(encoding="utf-8")

    assert 'RETIRED_HOME_DIRNAME=".fcc-old"' in shell
    assert 'purge_config_dir "$RETIRED_HOME_DIRNAME"' in shell
    assert 'if [ "$_dir_name" = "$RETIRED_HOME_DIRNAME" ]; then' in shell
    assert 'Purge-ConfigDir -DirName ".fcc-old" -LeaveAlone' in powershell
    assert 'Purge-ConfigDir -DirName ".mcc"' in powershell
    assert 'purge_config_dir "$MCC_HOME_DIRNAME"' in shell


# --------------------------------------------------------------------------
# The Windows desktop-app installer (spec S5).
#
# Inno Setup removes [Files] and [Icons] automatically from its uninstall log,
# so the parity question for this installer is not "does a second script name
# each file" but "does anything opt out of that, and does the uninstaller stay
# inside its own lane". Both are mechanical, and both are checked here.
# --------------------------------------------------------------------------

#: The AppId, and therefore the HKCU uninstall key name (``{AppId}_is1``) and
#: the winget ProductCode. It is a published identity: changing it turns the
#: next release into a second, parallel installation that does not upgrade the
#: first, and orphans the Apps & Features entry of every existing install.
INNO_APP_ID = "{5FC8D5C3-33F7-4366-AD8D-C844D21BC089}"

#: The shell's own application-data directory, relative to ``{userappdata}``.
#: It is Tauri's ``app_data_dir()`` -- ``dirs::data_dir()/<identifier>`` -- and
#: the identifier comes from ``desktop-shell/src-tauri/tauri.conf.json``.
INNO_SHELL_DATA_DIR = "com.myclaudecode.desktop"


def _iss_text() -> str:
    return INNO_SCRIPT.read_text(encoding="utf-8")


def _iss_sections() -> dict[str, list[str]]:
    """Split the ``.iss`` into ``{section: [entry lines]}``.

    Comments (``;`` in the script sections, ``//`` in ``[Code]``) and blank
    lines are dropped; everything else is an entry. ``[Code]`` is kept whole
    because it is Pascal, not entries.
    """

    sections: dict[str, list[str]] = {}
    current = "preamble"
    sections[current] = []
    for raw in _iss_text().splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        if not line:
            continue
        if current != "Code" and line.startswith(";"):
            continue
        if current == "Code" and line.startswith("//"):
            continue
        sections[current].append(line)
    return sections


def test_the_inno_script_is_parsable() -> None:
    """A silent parse miss would make every assertion below vacuous."""

    sections = _iss_sections()
    for name in ("Setup", "Files", "Icons", "Run", "UninstallDelete", "Code"):
        assert name in sections, (
            f"could not find a [{name}] section in {INNO_SCRIPT.name} -- if the "
            "installer was restructured, update this parser rather than "
            "deleting the guard"
        )
    assert len(sections["Files"]) >= 2, "expected the exe and its icon"
    assert sections["Icons"], "expected at least the Start Menu shortcut"


def test_no_installed_file_or_icon_opts_out_of_removal() -> None:
    """``uninsneveruninstall`` is how a file survives an uninstall.

    Inno removes everything in ``[Files]`` and ``[Icons]`` from its uninstall
    log; the single flag that exempts an entry is ``uninsneveruninstall``.
    One of those on the exe would leave a 3.5 MB orphan in
    ``%LOCALAPPDATA%\\Programs`` that nothing ever removes again.
    """

    sections = _iss_sections()
    offenders = [
        line
        for name in ("Files", "Icons", "Dirs")
        for line in sections.get(name, ())
        if "uninsneveruninstall" in line.lower()
    ]
    assert not offenders, (
        "these installer entries opt out of automatic removal: "
        f"{offenders}. Every file and icon this installer writes must come "
        "back out with it."
    )


def test_every_installed_file_and_icon_lands_somewhere_the_uninstall_reaches() -> None:
    """Inno only removes what it put under a directory it owns.

    A ``DestDir`` outside ``{app}`` (or a shortcut outside the Start Menu and
    the desktop) is still logged and still removed, but it also means the
    installer is writing into somebody else's directory -- which is how an
    uninstall starts deleting files it did not create.
    """

    sections = _iss_sections()

    stray_files = [line for line in sections["Files"] if 'DestDir: "{app}"' not in line]
    assert not stray_files, (
        f"these [Files] entries install outside {{app}}: {stray_files}. The "
        "desktop app installs one directory and nothing else."
    )

    allowed_icon_roots = ("{autoprograms}", "{autodesktop}")
    stray_icons = [
        line
        for line in sections["Icons"]
        if not any(root in line for root in allowed_icon_roots)
    ]
    assert not stray_icons, (
        f"these [Icons] entries are outside the Start Menu and the desktop: "
        f"{stray_icons}."
    )


def test_the_installer_writes_no_registry_value_of_its_own() -> None:
    """Autostart has exactly one writer, and it is not the installer.

    ``config/desktop.py`` owns the HKCU ``Run`` value and
    ``_reconcile_start_at_login`` is the only code that writes it. An
    installer that also wrote it would make uninstalling the *window* disable
    the *server tray's* autostart -- a value with two owners and one remover.
    Inno's own ``{AppId}_is1`` uninstall key is written by the compiler, not
    by this script, and is removed by the uninstaller.
    """

    sections = _iss_sections()
    assert not sections.get("Registry"), (
        "MyClaudeCode.iss has grown a [Registry] section: "
        f"{sections.get('Registry')}. Anything written there is a registry "
        "value with no remover outside Inno's log, and autostart in "
        "particular belongs to the application (config/desktop.py)."
    )
    text = _iss_text()
    for forbidden in ("CurrentVersion\\Run", "MyClaudeCodeDesktop"):
        assert forbidden not in text, (
            f"MyClaudeCode.iss names {forbidden!r}. The desktop app installer "
            "must not touch the start-at-login registration."
        )


def test_the_uninstaller_stays_out_of_the_servers_directories() -> None:
    """ "Uninstall the desktop app" must not uninstall My Claude Code.

    The split is deliberate and is documented in the .iss header, in the
    README and in USAGE: the Inno uninstaller removes the window; removing the
    server is ``scripts/uninstall.ps1``, on explicit consent.
    """

    # Entries only, not comments: the header talks about ~/.local/bin
    # precisely in order to say that the installer must never go there.
    sections = _iss_sections()
    entries = "\n".join(
        line for name, lines in sections.items() if name != "Code" for line in lines
    ).lower()
    for forbidden in (".local", ".mcc", ".fcc", "{userprofile}", "uninstall.ps1"):
        assert forbidden not in entries, (
            f"an entry in MyClaudeCode.iss names {forbidden!r}. The desktop "
            "app's uninstaller must never reach into the server's install, "
            "the config directory, or the legacy home, and it must not run "
            "the server's uninstaller."
        )


def test_the_only_optional_removal_is_the_shells_own_data_directory() -> None:
    """The one thing worth asking about, asked about in one place.

    ``[UninstallDelete]`` entries are recorded at *install* time, so a
    ``Check:`` on one would ask months before the answer matters. The opt-in
    deletion therefore lives in ``[Code]``, and this pins it there so it
    cannot quietly become unconditional.
    """

    sections = _iss_sections()
    code = "\n".join(sections["Code"])

    assert "CurUninstallStepChanged" in code
    assert "usPostUninstall" in code
    assert "SuppressibleMsgBox" in code, (
        "the uninstall question must use SuppressibleMsgBox so /VERYSILENT "
        "takes the default instead of hanging on a dialog"
    )
    assert "IDNO" in code, (
        "the silent default must be 'keep the data': an unattended uninstall "
        "may not delete something the user was never asked about"
    )
    assert INNO_SHELL_DATA_DIR in _iss_text(), (
        "the .iss no longer names the shell's application-data directory "
        f"({INNO_SHELL_DATA_DIR}); if tauri.conf.json's identifier changed, "
        "change it here too or the old directory is orphaned forever"
    )

    unconditional = [
        line for line in sections["UninstallDelete"] if INNO_SHELL_DATA_DIR in line
    ]
    assert not unconditional, (
        f"these [UninstallDelete] entries remove the shell's data directory "
        f"unconditionally: {unconditional}. It is opt-in, in [Code]."
    )


def test_the_installer_identity_and_privileges_are_pinned() -> None:
    """The four directives that make this a per-user, upgradable install."""

    sections = _iss_sections()
    setup = {}
    for line in sections["Setup"]:
        name, _, value = line.partition("=")
        setup[name.strip()] = value.strip()

    assert setup.get("AppId") == "{" + INNO_APP_ID, (
        f"AppId is {setup.get('AppId')!r}. It must stay "
        f"'{{{INNO_APP_ID}' (Inno's doubled leading brace) forever: it is the "
        "upgrade key, the HKCU uninstall key name and the winget ProductCode."
    )
    assert setup.get("PrivilegesRequired") == "lowest", (
        "PrivilegesRequired must be 'lowest': a per-user install needs no "
        "UAC prompt and puts its uninstall key in HKCU, which is what "
        "winget's Scope: user expects."
    )
    assert setup.get("Uninstallable") == "yes"
    assert setup.get("UninstallDisplayName", "").endswith("(desktop app)"), (
        "the Apps & Features entry must say '(desktop app)' out loud, so "
        "nobody removes the server by trying to remove the window"
    )
    assert setup.get("UninstallDisplayIcon"), (
        "an Apps & Features entry with no icon looks like malware"
    )


def test_the_webview2_bootstrapper_url_is_the_documented_permanent_one() -> None:
    """A rolling download, so the URL is pinned and the digest cannot be.

    The Evergreen Bootstrapper's bytes change every time Microsoft ships a
    runtime. Pinning its SHA-256 would make the next runtime release break
    every install; what is pinnable is the permanent fwlink Microsoft
    documents for this exact purpose, over HTTPS to a Microsoft host.
    """

    text = _iss_text()
    assert "https://go.microsoft.com/fwlink/p/?LinkId=2124703" in text
    assert "F3017226-FE2A-4295-8BDF-00C3A9A7E4C5" in text, (
        "the WebView2 EdgeUpdate client GUID is how the runtime is detected; "
        "without it the bootstrapper runs on every machine"
    )
    assert "/silent /install" in text


def test_the_installer_smoke_runs_the_winget_switches() -> None:
    """The switch set winget supplies for `InstallerType: inno` (spec S8).

    If the installer ever prompts under these, every unattended install hangs
    forever, so the release workflow proves it on every build.
    """

    smoke = (REPO_ROOT / "desktop-shell" / "smoke" / "windows-installer.ps1").read_text(
        encoding="utf-8"
    )
    for switch in ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"):
        assert switch in smoke, f"the installer smoke does not pass {switch}"
    assert "unins000.exe" in smoke, "the smoke never uninstalls"
    assert INNO_APP_ID in smoke, (
        "the smoke must look for this exact AppId's uninstall key"
    )

    workflow = (REPO_ROOT / ".github" / "workflows" / "shell-release.yml").read_text(
        encoding="utf-8"
    )
    assert "smoke/windows-installer.ps1" in workflow, (
        "the release workflow no longer runs the installer smoke"
    )
    assert "MyClaudeCode-Setup-windows-x86_64.exe" in workflow, (
        "the release workflow no longer uploads the Windows installer"
    )


# --------------------------------------------------------------------------
# The Linux desktop-app installers (spec S6): a .deb and a per-user tarball.
#
# The two halves are asymmetric on purpose, and the asymmetry is the contract:
#
#   * the .deb writes only under /usr, and dpkg removes exactly what dpkg
#     installed. `scripts/uninstall.sh` must therefore NAME it and never touch
#     it -- deleting a dpkg-owned file behind dpkg's back leaves the package
#     database claiming a file that is gone;
#   * `install-desktop.sh` writes only under $HOME, so both it (`--uninstall`)
#     and `scripts/uninstall.sh` must remove every one of those paths. The
#     ARTEFACTS rows above cover the second; the tests here cover the first,
#     and cover the two ways the two entries could collide with the *server's*
#     own `.desktop` entry.
# --------------------------------------------------------------------------

#: The Debian binary package the .deb declares. It is also the name
#: `scripts/uninstall.sh` prints in its `apt remove` hint.
DEB_PACKAGE = "my-claude-code-desktop"

#: The server's own launcher, written by `create_linux_desktop_entry` in
#: `scripts/install.sh`. The desktop app's entry must not be this file and
#: must not carry this Name.
SERVER_ENTRY_FILENAME = "my-claude-code.desktop"
SERVER_ENTRY_NAME = "Name=My Claude Code"


def _shell_code(path: Path) -> str:
    """Return a shell script's executable lines, with comments dropped.

    The forbidden-token assertions below are about what a script *does*, and
    these files explain themselves at length -- "anywhere with no sudo" in a
    comment is the opposite of a `sudo` call, and a check that cannot tell
    them apart is a check nobody can write documentation around.
    """

    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _linux_entry() -> dict[str, str]:
    """Parse the shared `.desktop` template into `{key: value}`."""

    fields: dict[str, str] = {}
    for raw in LINUX_DESKTOP_ENTRY_TEMPLATE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        fields[key] = value
    return fields


def test_the_linux_installers_are_all_present() -> None:
    """A missing file would make every assertion below vacuous."""

    for path in (BUILD_DEB, INSTALL_DESKTOP_SH, LINUX_DESKTOP_ENTRY_TEMPLATE):
        assert path.is_file(), f"{path} is missing"
    fields = _linux_entry()
    for key in ("Type", "Name", "Exec", "Icon", "Categories", "StartupWMClass"):
        assert key in fields, f"the desktop entry has no {key}="


def test_the_deb_and_the_tarball_share_one_desktop_entry() -> None:
    """Two copies of the entry would drift; there is one file, used twice."""

    template = LINUX_DESKTOP_ENTRY_TEMPLATE.name
    assert template in BUILD_DEB.read_text(encoding="utf-8"), (
        "build-deb.sh must install the shared entry rather than writing its own"
    )
    assert (
        'ENTRY_NAME="my-claude-code-desktop.desktop"'
        in INSTALL_DESKTOP_SH.read_text(encoding="utf-8")
    )


def test_the_apps_entry_can_never_be_confused_with_the_servers() -> None:
    """One machine may carry both; neither may hide or duplicate the other.

    Two defences, because either alone is escapable. The filenames differ, so
    neither installer overwrites the other's file; and the `Name=` differs, so
    a user who somehow ends up with both sees which is which rather than two
    identical tiles.
    """

    fields = _linux_entry()
    assert LINUX_DESKTOP_ENTRY_TEMPLATE.name != SERVER_ENTRY_FILENAME
    assert f"Name={fields['Name']}" != SERVER_ENTRY_NAME, (
        "the desktop app's entry has the same Name= as the server's launcher; "
        "a machine with both would show two identical menu entries"
    )
    assert "desktop app" in fields["Name"]

    # And the icon name differs too. A per-user icon shadows a system one of
    # the same name, so sharing `my-claude-code` between the server's exported
    # PNG and the .deb's would mean the .deb's icon silently never renders.
    assert fields["Icon"] == "my-claude-code-desktop"
    assert fields["Icon"] != "my-claude-code"


def test_the_server_installer_steps_aside_for_the_desktop_app() -> None:
    """`install.sh --desktop` must not add a second tile beside the app's."""

    install_sh = INSTALL_SH.read_text(encoding="utf-8")
    for guarded in (
        "/usr/share/applications/my-claude-code-desktop.desktop",
        '"$applications_dir/my-claude-code-desktop.desktop"',
    ):
        assert guarded in install_sh, (
            "create_linux_desktop_entry must skip its own entry when the "
            f"desktop app is already registered; {guarded!r} is not checked"
        )
    assert "not adding a second launcher" in install_sh


def test_the_startup_wm_class_is_the_shipped_binary_name() -> None:
    """A wrong StartupWMClass detaches the window from its launcher icon.

    GTK derives WM_CLASS from the program name, which is the executable's
    basename. That name is `[[bin]] name` in the shell's Cargo.toml and
    `mainBinaryName` in its Tauri config, and all three must agree.
    """

    cargo = SHELL_CARGO_TOML.read_text(encoding="utf-8")
    binary = re.search(r'^\[\[bin\]\]\s*\nname = "([^"]+)"', cargo, re.MULTILINE)
    assert binary is not None, "could not find [[bin]] name in the shell's Cargo.toml"

    assert _linux_entry()["StartupWMClass"] == binary.group(1)
    assert _linux_entry()["Exec"] == binary.group(1)


def test_the_deb_depends_carry_both_renamed_alternatives() -> None:
    """Ubuntu 24.04 and Debian 13 renamed gtk3 in the time_t transition.

    Naming only `libgtk-3-0` makes the package uninstallable on every
    distribution released since; naming only `libgtk-3-0t64` makes it
    uninstallable on the 22.04 floor it is built against.
    """

    build = BUILD_DEB.read_text(encoding="utf-8")
    depends = re.search(r"^Depends: (.+)$", build, re.MULTILINE)
    assert depends is not None, "build-deb.sh writes no Depends field"
    line = depends.group(1)
    for required in (
        "libwebkit2gtk-4.1-0",
        "libgtk-3-0t64 | libgtk-3-0",
        "libayatana-appindicator3-1 | libappindicator3-1",
    ):
        assert required in line, f"Depends is missing {required!r}: {line}"


def test_the_deb_declares_the_glibc_floor_it_was_built_against() -> None:
    """`E: missing-dependency-on-libc`, and lintian is right about it.

    The package is built on ubuntu-22.04 and glibc is forward-compatible
    only. Without a versioned `libc6`, apt installs it happily on Debian 11
    and the binary then dies at exec with a `GLIBC_2.34 not found` message
    that names no package and offers no fix.
    """

    build = BUILD_DEB.read_text(encoding="utf-8")
    depends = re.search(r"^Depends: (.+)$", build, re.MULTILINE)
    assert depends is not None
    assert "libc6 (>= 2.35)" in depends.group(1), (
        "Depends must pin the build floor's glibc: " + depends.group(1)
    )
    # And the smoke must assert it on the built package, not just here.
    assert '"libc6 (>= 2.35)"' in LINUX_DEB_SMOKE.read_text(encoding="utf-8"), (
        "smoke/linux-deb.sh does not check the glibc dependency on the real package"
    )


def test_the_deb_staging_root_is_world_readable() -> None:
    """`mktemp -d` is 0700, and dpkg-deb packages the staging root as `./`.

    A package whose own `./` is drwx------ is a lintian finding and, on a
    machine whose dpkg does not silently correct it, an unreadable install.
    """

    build = BUILD_DEB.read_text(encoding="utf-8")
    assert 'chmod 0755 "$staging"' in build, (
        "build-deb.sh must widen the mktemp staging root to 0755"
    )


def test_the_deb_writes_nothing_outside_usr() -> None:
    """A package that wrote under $HOME could not be uninstalled by dpkg."""

    build = BUILD_DEB.read_text(encoding="utf-8")
    staged = re.findall(r'"\$staging/([^"]+)"', build)
    assert staged, "could not find any staged paths in build-deb.sh"
    stray = sorted(
        {
            path
            for path in staged
            if path not in ("usr", "DEBIAN")
            and not path.startswith(("usr/", "DEBIAN/"))
        }
    )
    assert not stray, f"the .deb stages files outside /usr and DEBIAN: {stray}"
    code = _shell_code(BUILD_DEB)
    for forbidden in ("$HOME", "~/.local", "~/.mcc", "~/.fcc"):
        assert forbidden not in code, (
            f"build-deb.sh names {forbidden}; the package must not reach into a home"
        )


def test_the_debs_maintainer_scripts_only_refresh_caches() -> None:
    """postinst/postrm are the one place a package can do anything at all."""

    build = BUILD_DEB.read_text(encoding="utf-8")
    for script in ("postinst", "postrm"):
        assert f"DEBIAN/{script}" in build, f"the package declares no {script}"

    # Two cache refreshes, each written twice (once per script), each guarded
    # with `|| true` so a machine without desktop-file-utils still configures.
    assert (
        build.count("update-desktop-database -q /usr/share/applications || true") == 2
    )
    assert (
        build.count("gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true")
        == 2
    )
    for forbidden in ("rm -rf /", "systemctl", "useradd", "chown -R", "curl ", "wget "):
        assert forbidden not in _shell_code(BUILD_DEB), (
            f"a maintainer script would run {forbidden!r}; they may only refresh caches"
        )


def test_the_tarball_installer_removes_everything_it_writes() -> None:
    """`--uninstall` is the only removal a no-root user has."""

    text = INSTALL_DESKTOP_SH.read_text(encoding="utf-8")
    for target in (
        "$binary_path",
        "$receipt_path",
        "$entry_path",
        "$icons_root/$size/apps/$ICON_NAME.png",
    ):
        assert f'remove_path "{target}"' in text, (
            f"install-desktop.sh writes {target} but --uninstall does not remove it"
        )


def test_the_tarball_installer_stays_inside_the_home_directory() -> None:
    """It is the no-root path; a `sudo` or a /usr path in it is a bug."""

    text = _shell_code(INSTALL_DESKTOP_SH)
    for forbidden in ("sudo ", "/usr/bin", "/usr/share", "/etc/"):
        assert forbidden not in text, (
            f"install-desktop.sh names {forbidden!r}; it must be per-user only"
        )


def test_the_uninstaller_names_the_deb_and_never_deletes_its_files() -> None:
    """Removing dpkg-owned files behind dpkg's back corrupts its database."""

    text = UNINSTALL_SH.read_text(encoding="utf-8")
    code = _shell_code(UNINSTALL_SH)
    assert f'DESKTOP_APP_DEB_PACKAGE="{DEB_PACKAGE}"' in text
    assert "sudo apt remove %s" in text, (
        "uninstall.sh must tell the user the command that removes the package"
    )
    assert "dpkg-query -W" in text, "uninstall.sh must detect the package first"
    for forbidden in ("/usr/bin/MyClaudeCode", "/usr/share/applications", "dpkg -r"):
        assert forbidden not in code, (
            f"uninstall.sh names {forbidden!r}; the .deb is dpkg's to remove"
        )


# --------------------------------------------------------------------------
# The macOS desktop app (spec S9): an ad-hoc-signed `.app` in an unsigned
# `.dmg`.
#
# The asymmetry here is the same one the .deb has, for a sharper reason. The
# .deb writes under /usr and dpkg owns those files. The .dmg writes nothing at
# all -- the "install" is a human dragging an icon -- so nothing owns the
# result except the person who dragged it. `scripts/uninstall.sh` must
# therefore NAME the application and never delete it, and it must not delete
# it *by accident either*: the launcher bundle it does own has the same name
# and can sit at the same path.
# --------------------------------------------------------------------------

#: What `build-app.sh` names the bundle, its executable and its identifier.
#: The identifier is the load-bearing one -- it is how both scripts tell the
#: app apart from the launcher bundle.
MACOS_APP_IDENTIFIER = "com.myclaudecode.desktop"

#: What `create_macos_app_bundle` in scripts/install.sh writes instead. These
#: two strings must never converge.
MACOS_LAUNCHER_IDENTIFIER = "com.my-claude-code.desktop"


def _shell_assignments(path: Path) -> dict[str, str]:
    """Return the script's top-level ``NAME="value"`` assignments."""

    return {
        match.group(1): match.group(2)
        for match in re.finditer(
            r'^([A-Za-z_][A-Za-z0-9_]*)="([^"$]*)"$',
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    }


def _app_bundle_layout() -> tuple[str, ...]:
    """Every path inside `Contents/` that `build-app.sh` actually writes.

    Parsed out of the script rather than restated here, so a fifth file
    appearing in the bundle fails the uninstaller message that does not
    mention it. `_CodeSignature/CodeResources` is deliberately not found by
    this: `codesign` writes it, `build-app.sh` does not.
    """

    text = BUILD_APP.read_text(encoding="utf-8")
    names = _shell_assignments(BUILD_APP)
    found: set[str] = set()
    for raw in re.findall(r'"\$contents/([^"]+)"', text):
        resolved = raw
        for variable, value in names.items():
            resolved = resolved.replace(f"${variable}", value)
        if "$" in resolved:
            continue
        found.add(resolved)
    return tuple(sorted(found))


def test_the_macos_installers_are_all_present() -> None:
    """A missing file would make every assertion below vacuous."""

    for path in (BUILD_APP, BUILD_DMG, MACOS_DMG_SMOKE):
        assert path.is_file(), f"{path} is missing"
    layout = _app_bundle_layout()
    assert len(layout) >= 4, f"only {layout} was parsed out of build-app.sh"


def test_the_bundle_layout_is_the_one_the_smoke_asserts() -> None:
    """The script and its proof must describe the same application."""

    smoke = MACOS_DMG_SMOKE.read_text(encoding="utf-8")
    for path in _app_bundle_layout():
        # The smoke spells the executable and the icns through its own
        # $EXECUTABLE, so compare on the stable part of each path.
        stem = path.split("/")[0]
        assert stem in smoke, f"smoke/macos-dmg.sh never mentions Contents/{stem}"
    assert "Contents/_CodeSignature/CodeResources" in smoke, (
        "the smoke does not assert the bundle was sealed at all"
    )


def test_the_app_and_the_launcher_bundle_carry_different_identifiers() -> None:
    """One name, one path, two programs -- the identifier is the only tell.

    `install.sh --desktop` writes `~/Applications/My Claude Code.app`. The
    `.dmg` puts an application of the same name in `/Applications`, and a user
    may drag it into `~/Applications` instead. If these two strings ever
    became equal, the installer would overwrite the application and the
    uninstaller would delete it.
    """

    build_app = _shell_assignments(BUILD_APP)
    assert build_app.get("IDENTIFIER") == MACOS_APP_IDENTIFIER, (
        f"build-app.sh's IDENTIFIER is {build_app.get('IDENTIFIER')!r}"
    )

    install_sh = INSTALL_SH.read_text(encoding="utf-8")
    assert f"<string>{MACOS_LAUNCHER_IDENTIFIER}</string>" in install_sh, (
        "create_macos_app_bundle no longer writes the launcher identifier"
    )
    assert MACOS_APP_IDENTIFIER != MACOS_LAUNCHER_IDENTIFIER
    # And the launcher's identifier must not merely *contain* the app's, since
    # both scripts detect the app with a fixed-string grep.
    assert MACOS_APP_IDENTIFIER not in MACOS_LAUNCHER_IDENTIFIER


def test_the_bundle_executable_is_the_shipped_binary_name() -> None:
    """CFBundleExecutable must name the file in Contents/MacOS.

    That file is `[[bin]] name` in the shell's Cargo.toml, which is
    version-agnostic on purpose (decision Q5). The *bundle* is versioned --
    CFBundleShortVersionString carries the tag -- and the two do not conflict,
    because nothing points at a plist key.
    """

    cargo = SHELL_CARGO_TOML.read_text(encoding="utf-8")
    binary = re.search(r'^\[\[bin\]\]\s*\nname = "([^"]+)"', cargo, re.MULTILINE)
    assert binary is not None, "could not find [[bin]] name in the shell's Cargo.toml"

    build_app = _shell_assignments(BUILD_APP)
    assert build_app.get("EXECUTABLE") == binary.group(1)
    text = BUILD_APP.read_text(encoding="utf-8")
    assert "<key>CFBundleExecutable</key>" in text
    assert "<string>$EXECUTABLE</string>" in text
    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        assert f"<key>{key}</key>" in text, f"the bundle declares no {key}"
    assert text.count("<string>$version</string>") == 2, (
        "both version keys must carry the release tag, and only those two"
    )


def test_the_bundle_declares_the_keys_the_window_depends_on() -> None:
    """Three of these are not cosmetic.

    Without `NSAppTransportSecurity > NSAllowsLocalNetworking` the webview
    refuses the cleartext `http://127.0.0.1:<port>` the whole product is;
    without `NSHighResolutionCapable` the dashboard renders through the 1x
    magnifier on every Mac made since 2012; without `LSMinimumSystemVersion`
    macOS makes its own guess about where the app will run.
    """

    text = BUILD_APP.read_text(encoding="utf-8")
    for key in (
        "CFBundleIdentifier",
        "CFBundleName",
        "CFBundleDisplayName",
        "CFBundleExecutable",
        "CFBundleIconFile",
        "CFBundlePackageType",
        "LSApplicationCategoryType",
        "LSMinimumSystemVersion",
        "NSAppTransportSecurity",
        "NSHighResolutionCapable",
    ):
        assert f"<key>{key}</key>" in text, f"Info.plist has no {key}"

    assert "<key>NSAllowsLocalNetworking</key>" in text
    assert "NSAllowsArbitraryLoads" not in _shell_code(BUILD_APP), (
        "the bundle would exempt the whole internet from App Transport "
        "Security; it needs loopback only"
    )
    assert "public.app-category.developer-tools" in text
    assert 'MINIMUM_SYSTEM_VERSION="10.13"' in text, (
        "the minimum system version moved; Tauri v2's own bundler default is "
        "10.13, and the README quotes whatever is here"
    )
    assert "plutil -lint" in text, "build-app.sh does not validate the plist it wrote"


def test_the_bundle_is_ad_hoc_signed_and_claims_nothing_more() -> None:
    """Ad-hoc is a seal, not an identity, and the docs say so.

    On Apple silicon every Mach-O must carry at least an ad-hoc signature, and
    `lipo` produces a fresh file whose inherited signature no longer matches
    it -- so the signing step is mandatory, not decorative. It is also the
    limit of what this project can do: a Developer ID certificate and
    notarisation need a paid Apple Developer account.
    """

    code = _shell_code(BUILD_APP)
    assert "codesign --force --deep --sign - --timestamp=none" in code
    assert "codesign --verify --deep --strict" in code, (
        "build-app.sh signs but never checks the seal it just made"
    )
    for forbidden in ("notarytool", "altool", "stapler", "--entitlements", '--sign "'):
        assert forbidden not in code, (
            f"build-app.sh names {forbidden!r}; v1 ships unsigned and "
            "un-notarised, and the documentation says so"
        )
    # And the script says out loud what it did, so a build log cannot be
    # mistaken for evidence of a Developer ID signature.
    assert "NOT Developer ID, NOT notarised" in code


def test_the_dmg_is_a_udzo_image_a_person_can_drag_out_of() -> None:
    """`-format UDZO` and an /Applications symlink, or it is not a Mac dmg."""

    text = BUILD_DMG.read_text(encoding="utf-8")
    for required in (
        "hdiutil create",
        "-format UDZO",
        '-volname "$volume_name"',
        "-fs HFS+",
        'ln -s /Applications "$staging/Applications"',
    ):
        assert required in text, f"build-dmg.sh does not use {required!r}"
    assert 'ditto "$app"' in text, (
        "build-dmg.sh must copy the bundle with ditto; cp -R can break the seal"
    )
    assert 'chmod 0755 "$staging"' in text, (
        "mktemp -d is 0700, so the mounted volume would look empty to anyone "
        "but its owner"
    )


def test_the_dmg_smoke_expects_gatekeeper_to_reject_the_app() -> None:
    """The proof asserts the documented failure, rather than skipping it.

    `spctl --assess` rejecting this app is what the Gatekeeper paragraph in
    README.md, docs/USAGE.md and the release notes is about. A smoke that
    merely tolerated a green `spctl` would be a smoke that never noticed the
    day that documentation became wrong.
    """

    text = MACOS_DMG_SMOKE.read_text(encoding="utf-8")
    assert "spctl --assess --type execute" in text
    assert "spctl ACCEPTED an unsigned app" in text, (
        "the smoke does not fail when Gatekeeper accepts the app"
    )
    assert "com.apple.quarantine" in text, (
        "the smoke does not exercise the workaround the docs give users"
    )
    # And it must prove it installed nothing: a dmg is not an installer.
    assert "/Applications or ~/Applications changed" in text


def test_the_dmg_smokes_snapshot_survives_a_missing_applications_directory() -> None:
    """`set -euo pipefail` plus a `~/Applications` that is not there.

    A runner has no `~/Applications`, `ls` exits non-zero for it, and a
    failing command in a pipeline is a failing pipeline -- so the unguarded
    form exits 1 on the smoke's second line, before a single assertion has
    run. That is not hypothetical: it is what the v6.45.2 release did.
    """

    text = MACOS_DMG_SMOKE.read_text(encoding="utf-8")
    snapshot = text.split("snapshot() {")[1].split("\n}")[0]
    for directory in ("/Applications", '"$HOME/Applications"'):
        listing = next(
            line for line in snapshot.splitlines() if f"ls -1a {directory}" in line
        )
        assert "|| true" in listing, (
            f"the snapshot's listing of {directory} is not guarded; under "
            "set -euo pipefail a missing directory ends the smoke with no "
            f"assertion run: {listing.strip()}"
        )

    # The same class of bug, two lines down: an assignment whose command
    # substitution fails ends the script silently.
    assert 'defaults read "$plist" "$1" 2>/dev/null || true' in text, (
        "read_key must return empty for a missing key, so the assertion that "
        "follows can name it"
    )


def test_the_server_installer_steps_aside_for_the_macos_desktop_app() -> None:
    """`install.sh --desktop` must not overwrite the application.

    In `/Applications` writing the launcher bundle would be a duplicate. In
    `~/Applications` it would be an *overwrite*, because the two bundles have
    the same name -- a working application replaced by a shell wrapper.
    """

    install_sh = INSTALL_SH.read_text(encoding="utf-8")
    assert f'MACOS_DESKTOP_APP_IDENTIFIER="{MACOS_APP_IDENTIFIER}"' in install_sh
    assert "macos_bundle_is_the_desktop_app() {" in install_sh
    for guarded in (
        '"/Applications/My Claude Code.app"',
        '"$HOME/Applications/My Claude Code.app"',
    ):
        assert guarded in install_sh, (
            "create_macos_app_bundle must step aside when the desktop app is "
            f"already installed; {guarded!r} is not checked"
        )
    assert install_sh.count("not adding a second launcher") == 2, (
        "both create_linux_desktop_entry and create_macos_app_bundle must "
        "step aside, with the same sentence"
    )


def test_the_uninstaller_names_the_macos_app_and_never_deletes_it() -> None:
    """It removes the launcher bundle it wrote, and only that.

    The message it prints must describe the whole bundle, so somebody reading
    it knows what they are dragging to the Trash. The layout is parsed out of
    `build-app.sh`, so a file added to the bundle fails here.
    """

    text = UNINSTALL_SH.read_text(encoding="utf-8")
    assert f'MACOS_DESKTOP_APP_IDENTIFIER="{MACOS_APP_IDENTIFIER}"' in text
    assert 'MACOS_DESKTOP_APP_BUNDLE="/Applications/My Claude Code.app"' in text
    assert "macos_bundle_is_the_desktop_app() {" in text
    assert "remove_macos_launcher_bundle() {" in text
    assert "report_macos_desktop_app() {" in text
    assert (
        "report_macos_desktop_app"
        in _shell_code(UNINSTALL_SH).split("remove_desktop_artifacts() {")[1]
    ), "report_macos_desktop_app is defined but never called"

    # The removal of the launcher bundle is guarded, not unconditional.
    guarded = text.split("remove_macos_launcher_bundle() {")[1].split("\n}")[0]
    assert 'macos_bundle_is_the_desktop_app "$HOME/$MACOS_APP_BUNDLE"' in guarded
    assert 'remove_home_path "$MACOS_APP_BUNDLE"' in guarded

    # And the message covers every part of the bundle build-app.sh writes.
    message = text.split("report_macos_desktop_app() {")[1].split("\n}")[0]
    for path in _app_bundle_layout():
        assert f"Contents/{path}" in message, (
            f"the uninstaller's message does not mention Contents/{path}, "
            "which build-app.sh puts in the bundle"
        )
    assert "does not remove it" in message
    assert "Trash" in message


def test_the_uninstaller_never_reaches_into_the_system_applications_folder() -> None:
    """Only detection may name /Applications; nothing may remove from it."""

    code = _shell_code(UNINSTALL_SH)
    for forbidden in (
        'rm -rf "/Applications',
        'remove_home_path "/Applications',
        'run rm -rf "$MACOS_DESKTOP_APP_BUNDLE"',
    ):
        assert forbidden not in code, (
            f"uninstall.sh names {forbidden!r}; the .dmg's app belongs to "
            "whoever dragged it there"
        )
