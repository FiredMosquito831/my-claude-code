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
