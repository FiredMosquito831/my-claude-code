"""Lightweight entrypoint for the optional MCC desktop shell."""

import ctypes
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from my_claude_code.cli.desktop import DesktopError, headless_refusal_reason
from my_claude_code.cli.desktop_assets import export_app_icon
from my_claude_code.config.desktop import (
    SERVER_MODES,
    WINDOW_PREFERENCES,
    apply_tray_registration,
    load_desktop_state,
    set_server_mode,
    set_start_at_login,
    set_window_preference,
)

_MB_ICONERROR = 0x10
_ERROR_BOX_TITLE = "My Claude Code"


def _report_fatal_error(message: str) -> None:
    """Surface a startup failure the GUI-subsystem executable cannot print.

    ``mcc-desktop.exe`` has no console attached on Windows, so stderr goes
    nowhere there: Windows gets a message box instead, every other platform
    keeps the terminal fallback.
    """

    print(message, file=sys.stderr)
    if sys.platform != "win32":
        return
    with suppress(Exception):
        ctypes.windll.user32.MessageBoxW(None, message, _ERROR_BOX_TITLE, _MB_ICONERROR)


def _print_state() -> None:
    state = load_desktop_state()
    print(f"tray_enabled={str(state.tray_enabled).lower()}")
    print(f"start_at_login={str(state.start_at_login).lower()}")
    print(f"minimize_to_tray={str(state.minimize_to_tray).lower()}")
    print(f"server_mode={state.server_mode}")
    print(f"window={state.window}")


def _print_usage() -> None:
    print(
        "Usage: mcc-desktop [--server-mode spawn|attach|off] "
        "[--window auto|app-mode|pywebview|browser] "
        "[--autostart on|off] "
        "[--start-at-login | --no-start-at-login | "
        "--tray-enabled | --no-tray-enabled | "
        "--status | --print-status [--presence-v2] | "
        "--ensure-shell [--target PATH] | --export-icon PATH]",
        file=sys.stderr,
    )


def launch(argv: Sequence[str] | None = None) -> None:
    """Apply a state toggle, export installer assets, or launch the tray."""

    args = tuple(sys.argv[1:] if argv is None else argv)

    if len(args) == 2 and args[0] == "--export-icon":
        export_app_icon(Path(args[1]))
        return

    toggle = {
        "--start-at-login": True,
        "--no-start-at-login": False,
        "--tray-enabled": True,
        "--no-tray-enabled": False,
    }
    if len(args) == 1 and args[0] in toggle:
        if args[0] in {"--start-at-login", "--no-start-at-login"}:
            set_start_at_login(toggle[args[0]])
        else:
            apply_tray_registration(toggle[args[0]])
        return

    if len(args) == 2 and args[0] == "--server-mode":
        if args[1] not in SERVER_MODES:
            print(
                f"Invalid server mode: {args[1]} "
                f"(expected one of {', '.join(SERVER_MODES)})",
                file=sys.stderr,
            )
            raise SystemExit(2)
        set_server_mode(args[1])
        return

    if len(args) == 2 and args[0] == "--window":
        if args[1] not in WINDOW_PREFERENCES:
            print(
                f"Unknown window provider: {args[1]}. Choose one of "
                f"{', '.join(WINDOW_PREFERENCES)}; 'auto' picks the first one "
                f"this machine can run.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        set_window_preference(args[1])
        return

    if len(args) == 2 and args[0] == "--autostart":
        if args[1] == "on":
            set_start_at_login(True)
        elif args[1] == "off":
            set_start_at_login(False)
        else:
            print("--autostart expects 'on' or 'off'", file=sys.stderr)
            raise SystemExit(2)
        return

    if len(args) == 1 and args[0] == "--status":
        _print_state()
        return

    if args and args[0] == "--ensure-shell" and _ensure_shell_target_is_valid(args[1:]):
        _ensure_shell(args[2] if len(args) == 3 else None)
        return

    if args and args[0] == "--print-status" and set(args[1:]) <= {"--presence-v2"}:
        # Imported here, not at module scope, so the toggle paths above
        # keep their current import cost.
        from my_claude_code.cli.desktop_status import print_status

        # ``--presence-v2`` is a reader saying "I have a branch for every
        # presence this wheel can report", which today means ``draining``.
        # Without it the document carries only the three values every shell
        # ever built understands. See cli/desktop_status.py's docstring.
        print_status(presence_v2="--presence-v2" in args[1:])
        return

    if args:
        _print_usage()
        raise SystemExit(2)

    refusal = headless_refusal_reason()
    if refusal is not None:
        print(refusal, file=sys.stderr)
        raise SystemExit(1)

    try:
        _launch_host()
    except DesktopError as exc:
        _report_fatal_error(str(exc))
        raise SystemExit(1) from exc


def _ensure_shell_target_is_valid(rest: tuple[str, ...]) -> bool:
    """Whether what follows ``--ensure-shell`` is nothing, or ``--target PATH``."""

    return not rest or (len(rest) == 2 and rest[0] == "--target")


def _ensure_shell(target: str | None) -> None:
    """Bring one desktop app binary up to the pinned release. Prints JSON.

    The command BUG-0 was missing. Until 6.60.0 the only caller of the pin was
    ``ShellWindow.create()``, reached only when the *Python tray* started -- so
    a window launched from the Start Menu, from the taskbar, or from the
    Programs-folder install ran whatever shell it first received and nothing
    would ever move it. This is that check as a command anyone can run: the
    window itself, on launch, when it finds its own compiled-in tag disagrees
    with ``shell_release_tag``; the installers; and a person.

    ``--target`` is the binary to update, and the window passes its own
    ``current_exe()`` there: the copy that has to change is the one being run,
    which is not always the one in ``~/.local/bin``. Without it the default
    install is updated.

    Nothing is written over. The verified replacement is staged beside the
    running file and the next start swaps it in (decision Q5: the app never
    replaces its own running executable; it asks Python to fetch and verify,
    then relaunches into what Python staged).

    Stdout is JSON and nothing else, because the window parses it; every
    diagnostic goes to stderr.
    """

    # Imported here rather than at module scope: this module is on the path of
    # every ``mcc-desktop`` toggle, and the fetcher costs ``tarfile``,
    # ``zipfile``, ``hashlib`` and ``urllib``.
    import json

    from my_claude_code.config.desktop_shell import (
        DesktopShellError,
        stage_desktop_shell,
    )

    try:
        report = stage_desktop_shell(Path(target) if target else None)
    except DesktopShellError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(report, indent=2))


def _launch_host() -> None:
    """Run the desktop host, with a Python tray only when it owns the icon.

    Two reasons this is not simply "import pystray and use it".

    ``pystray`` is declared ``sys_platform == 'win32' or sys_platform ==
    'darwin'`` in ``pyproject.toml``, so on Linux there is no tray adapter to
    import and there never will be.

    And since 6.61.0 (decision Q2) the desktop app owns the icon on Windows
    too, wherever one is installed: it is the process that survives an update
    and the process that owns the server lifecycle, and a second tray beside it
    offered a second answer to "restart the server" from a process that did not
    know what the first one was doing. So this host runs with a stand-in that
    owns nothing but the thread the process blocks on -- the window's own tray
    is the tray, and its Quit is what ends the app.
    """

    from my_claude_code.cli.desktop_window import shell_owns_tray

    if not shell_owns_tray():
        try:
            from my_claude_code.cli.desktop_tray import launch as launch_tray
        except ImportError:
            pass
        else:
            launch_tray()
            return
    from my_claude_code.cli.desktop import WindowOnlyHost, launch_desktop

    launch_desktop(WindowOnlyHost)
