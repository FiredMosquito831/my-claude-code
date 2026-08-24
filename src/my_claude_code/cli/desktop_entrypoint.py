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
        "--tray-enabled | --no-tray-enabled | --status | --export-icon PATH]",
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

    if args:
        _print_usage()
        raise SystemExit(2)

    refusal = headless_refusal_reason()
    if refusal is not None:
        print(refusal, file=sys.stderr)
        raise SystemExit(1)

    try:
        from my_claude_code.cli.desktop_tray import launch as launch_tray

        launch_tray()
    except DesktopError as exc:
        _report_fatal_error(str(exc))
        raise SystemExit(1) from exc
