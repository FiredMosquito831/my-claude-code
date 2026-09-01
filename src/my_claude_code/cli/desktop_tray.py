"""pystray adapter for the Windows tray and macOS menu bar."""

from collections.abc import Callable
from io import BytesIO

from PIL import Image
from pystray import Icon, Menu, MenuItem

from my_claude_code.cli.desktop import DesktopController
from my_claude_code.cli.desktop_assets import tray_icon_bytes
from my_claude_code.config.desktop import (
    load_desktop_state,
    set_server_mode,
    set_start_at_login,
    set_tray_enabled,
)
from my_claude_code.config.harnesses import harness_spec, rtk_capable_ids
from my_claude_code.config.rtk import (
    RtkState,
    apply_rtk_state,
    load_rtk_state,
    save_rtk_state,
)

_APP_NAME = "My Claude Code"

_SERVER_MODE_LABELS = {
    "spawn": "Spawn server",
    "attach": "Attach to server",
    "off": "Off (tray only)",
}


class PystrayDesktopTray:
    """Render desktop lifecycle actions through the native status area."""

    def __init__(self, controller: DesktopController) -> None:
        self._controller = controller
        state = load_desktop_state()
        self._start_at_login = state.start_at_login
        self._tray_enabled = state.tray_enabled
        self._server_mode = state.server_mode
        self._rtk_state = load_rtk_state().as_dict()
        self._icon = Icon(
            "my-claude-code",
            _create_icon(),
            _APP_NAME,
            self._menu(),
        )

    def _menu(self) -> Menu:
        return Menu(
            MenuItem("Open Admin", self._open_admin, default=True),
            MenuItem("Check Server Status", self._check_status),
            MenuItem("Restart Server", self._restart_server),
            MenuItem(
                "Server mode",
                Menu(
                    MenuItem(
                        _SERVER_MODE_LABELS["spawn"],
                        self._set_server_mode_spawn,
                        checked=lambda item: self._server_mode == "spawn",
                    ),
                    MenuItem(
                        _SERVER_MODE_LABELS["attach"],
                        self._set_server_mode_attach,
                        checked=lambda item: self._server_mode == "attach",
                    ),
                    MenuItem(
                        _SERVER_MODE_LABELS["off"],
                        self._set_server_mode_off,
                        checked=lambda item: self._server_mode == "off",
                    ),
                ),
            ),
            Menu.SEPARATOR,
            MenuItem(
                "Start at Login",
                self._toggle_start_at_login,
                checked=lambda item: self._start_at_login,
            ),
            MenuItem(
                "Tray Enabled",
                self._toggle_tray_enabled,
                checked=lambda item: self._tray_enabled,
            ),
            Menu.SEPARATOR,
            MenuItem("Token optimizer", Menu(*self._rtk_menu_items())),
            Menu.SEPARATOR,
            MenuItem("Quit", self._quit),
        )

    def notify(self, message: str) -> None:
        """Surface a controller-side event in the platform notification area."""

        self._icon.notify(message, _APP_NAME)

    def run(self) -> None:
        self._icon.run()

    def stop(self) -> None:
        self._icon.stop()

    def _open_admin(self, _icon: Icon, _item: MenuItem) -> None:
        self._controller.open_admin()

    def _check_status(self, _icon: Icon, _item: MenuItem) -> None:
        status = self._controller.status
        if status == "running":
            self._icon.notify(f"Server is {status}.", _APP_NAME)
            return
        mode = self._server_mode
        if mode == "spawn":
            self._icon.notify("Server is not running.", _APP_NAME)
        elif mode == "attach":
            self._icon.notify(
                "Server not running. Start mcc-server manually to attach.",
                _APP_NAME,
            )
        else:
            self._icon.notify("Server is off (tray only).", _APP_NAME)

    def _restart_server(self, _icon: Icon, _item: MenuItem) -> None:
        try:
            self._controller.restart_server()
            self._icon.notify("Server restarted.", _APP_NAME)
        except Exception as exc:
            self._icon.notify(f"Restart failed: {exc}", _APP_NAME)

    def _set_server_mode(self, mode: str) -> None:
        self._server_mode = mode
        try:
            set_server_mode(mode)
        except Exception as exc:
            self._icon.notify(f"Could not save server mode: {exc}", _APP_NAME)

    def _set_server_mode_spawn(self, _icon: Icon, _item: MenuItem) -> None:
        self._set_server_mode("spawn")

    def _set_server_mode_attach(self, _icon: Icon, _item: MenuItem) -> None:
        self._set_server_mode("attach")

    def _set_server_mode_off(self, _icon: Icon, _item: MenuItem) -> None:
        self._set_server_mode("off")

    def _toggle_start_at_login(self, _icon: Icon, _item: MenuItem) -> None:
        # Flip the value on disk, not the cached one: another process may have
        # changed this field since the tray started, and toggling a stale
        # value computes the wrong direction (the click then appears to do
        # nothing). set_start_at_login itself preserves the other fields.
        self._start_at_login = not load_desktop_state().start_at_login
        set_start_at_login(self._start_at_login)

    def _toggle_tray_enabled(self, _icon: Icon, _item: MenuItem) -> None:
        # Same reasoning as _toggle_start_at_login: derive the new value from
        # persisted state so an externally changed field toggles correctly.
        self._tray_enabled = not load_desktop_state().tray_enabled
        set_tray_enabled(self._tray_enabled)

    def _rtk_menu_items(self) -> list[MenuItem]:
        """Build one checkbox per RTK-capable harness, from the registry.

        Three hand-written menu entries until the registry existed. Each
        closure is built by a factory rather than a lambda with a default
        argument: pystray inspects an action's arity and rejects anything but
        ``()``, ``(icon)`` or ``(icon, item)``, so a captured default would
        raise at menu construction time.
        """

        return [
            MenuItem(
                harness_spec(agent).display_name,
                self._toggle_action(agent),
                checked=self._checked_probe(agent),
            )
            for agent in rtk_capable_ids()
        ]

    def _toggle_action(self, agent: str) -> Callable[[Icon, MenuItem], None]:
        def toggle(_icon: Icon, _item: MenuItem) -> None:
            self._toggle_rtk_agent(agent)

        return toggle

    def _checked_probe(self, agent: str) -> Callable[[MenuItem], bool]:
        def checked(_item: MenuItem) -> bool:
            return self._rtk_checked(agent)

        return checked

    def _rtk_checked(self, agent: str) -> bool:
        return self._rtk_state.get(agent, False)

    def _toggle_rtk_agent(self, agent: str) -> None:
        # Re-read from disk instead of writing from the in-memory cache: the
        # tray and the admin HTTP API are separate processes that both
        # persist RtkState to the same file, and the tray's cache can be
        # stale relative to a change the API made after the tray started.
        # Writing every field reconstructed from a stale cache would
        # silently revert whatever the other process last wrote. Do not
        # "optimise" this back into a cached write.
        rtk_values = load_rtk_state().as_dict()
        rtk_values[agent] = not rtk_values.get(agent, False)
        state = RtkState(rtk_values)
        save_rtk_state(state)
        apply_rtk_state(state)
        self._rtk_state = rtk_values

    def _quit(self, _icon: Icon, _item: MenuItem) -> None:
        self._controller.quit()
        self._icon.stop()


def _create_icon() -> Image.Image:
    """Load the tray-specific cut of the brand mark.

    Deliberately not ``app_icon_bytes``: that render carries a 10% margin for
    window and taskbar use, and a status area draws at 16-24px, where the
    padding costs enough of the glyph to make it unreadable. ``tray-icon.png``
    is the same mark at a 2% margin for exactly this surface.
    """

    with Image.open(BytesIO(tray_icon_bytes())) as image:
        return image.convert("RGBA")


def launch() -> None:
    """Launch the pystray tray adapter around a desktop controller."""

    from my_claude_code.cli.desktop import launch_desktop

    launch_desktop(PystrayDesktopTray)
