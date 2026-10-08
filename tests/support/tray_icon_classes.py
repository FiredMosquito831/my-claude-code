"""Release the window class a never-run pystray ``Icon`` leaves registered.

On Windows, pystray's ``Icon`` registers a window class in its constructor,
named ``f"{name}{id(icon)}SystemTrayIcon"`` (``pystray/_win32.py`` 0.19.5,
lines 34 and 384), and unregisters it only when its run loop ends (line 173).
The tray tests build ``PystrayDesktopTray``, which constructs an ``Icon``, and
never run it, so every one of them left a class registered in its worker
process for the rest of the session. Once such an ``Icon`` was collected,
CPython could hand its address to the next one; that ``Icon`` asked for the
same class name and ``RegisterClassEx`` failed with ``OSError: [WinError 1410]
Class already exists``. Which test met it depended on what else had run in the
worker, which is why it moved between ``test_desktop_rtk.py`` tests from run to
run (the first nightly Windows job on 2026-10-08, and local gates before it).
Production builds one ``Icon`` per process and runs it, so it never meets this.

The tests still construct the real ``Icon``. This keeps each one alive until
its test ends, so no address can be reused while its class is registered, and
then unregisters the class, which is what the run loop would have done.
"""

import sys
from collections.abc import Iterator
from types import ModuleType

import pytest


def release_tray_icon_classes(
    monkeypatch: pytest.MonkeyPatch, tray_module: ModuleType
) -> Iterator[None]:
    """Fixture body: track every ``Icon`` ``tray_module`` builds, release at teardown.

    Use from an autouse fixture with ``yield from``.
    """

    real_icon = tray_module.Icon
    built: list = []

    def tracked_icon(*args, **kwargs):
        icon = real_icon(*args, **kwargs)
        built.append(icon)
        return icon

    monkeypatch.setattr(tray_module, "Icon", tracked_icon)
    yield
    for icon in built:
        atom = getattr(icon, "_atom", None)
        if atom:
            icon._unregister_class(atom)
        elif sys.platform == "win32":
            pytest.fail(
                "pystray's win32 Icon no longer exposes the class atom this "
                "fixture releases (_atom); check how the installed pystray "
                "registers its window class and update "
                "tests/support/tray_icon_classes.py"
            )
    built.clear()
