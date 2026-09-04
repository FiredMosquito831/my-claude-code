"""A ``winreg`` stand-in every test that exercises the Run key can share.

``tests/cli/test_desktop_rtk.py`` used to redirect ``HOME`` and nothing else, so
the *file* half of "start at login" was isolated and the *registry* half ran for
real: two runs of the suite deleted the developer's own
``MyClaudeCodeDesktop`` autostart value under the HKCU Run key. The session-wide guard in
``tests/support/hermetic.py`` now refuses that write outright; this fixture is
what a test uses when the registry call *is* the thing under test.
"""

import sys
from typing import Any

import pytest

from my_claude_code.config import desktop as desktop_config
from my_claude_code.config.desktop import WINDOWS_RUN_VALUE


class FakeWinreg:
    """Minimal winreg stand-in exercising the same call surface."""

    HKEY_CURRENT_USER = object()
    KEY_SET_VALUE = 1
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.closed = False

    def OpenKey(self, root: Any, subkey: str, reserved: int, access: int) -> FakeWinreg:
        assert root is self.HKEY_CURRENT_USER
        assert subkey == r"Software\Microsoft\Windows\CurrentVersion\Run"
        return self

    def SetValueEx(
        self, key: Any, name: str, reserved: int, kind: int, value: str
    ) -> None:
        assert key is self
        assert name == WINDOWS_RUN_VALUE
        self.values[name] = value

    def DeleteValue(self, key: Any, name: str) -> None:
        assert key is self
        assert name == WINDOWS_RUN_VALUE
        self.values.pop(name, None)

    def CloseKey(self, key: Any) -> None:
        assert key is self
        self.closed = True

    def __enter__(self) -> FakeWinreg:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


@pytest.fixture
def fake_winreg(monkeypatch: pytest.MonkeyPatch) -> FakeWinreg:
    """Run the Windows autostart branch against an in-memory registry."""

    fake = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(desktop_config, "native_origin", lambda: "windows")
    return fake
