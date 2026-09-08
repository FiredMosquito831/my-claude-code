"""The server brings a stale desktop app up to the pin, by itself.

Until 6.61.0 the pin reached a machine only if somebody *ran* something: the
tray's window factory, or the window noticing it was stale and asking for
``mcc-desktop --ensure-shell``. A user who launches ``MyClaudeCode.exe`` from
the Start Menu and never opens a terminal was left on whatever build they first
received -- which is exactly how one of them ran a fifteen-release-old window
while their wheel moved through fifteen releases of fixes.

So the server does it: once per start, after readiness, on a thread, through
the *same* ``stage_desktop_shell`` the ``--ensure-shell`` command calls. These
tests pin the guards, because a background updater with a weak guard is worse
than no updater at all.
"""

import json

import pytest

from my_claude_code.config import desktop_shell


@pytest.fixture
def shell_dir(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv(desktop_shell.DESKTOP_SHELL_DIR_ENV, str(directory))
    # The suite pins ``DESKTOP_SHELL=off`` globally so no test can reach the
    # release page. These tests are about the switch itself, so they turn it
    # back on and stub the one function that would download anything.
    monkeypatch.setenv(desktop_shell.DESKTOP_SHELL_ENABLED_ENV, "auto")
    return directory


def _install(directory, tag: str) -> None:
    """Put a binary and a receipt where this module installs them."""

    binary = directory / desktop_shell.desktop_shell_binary_name()
    binary.write_bytes(b"MZ not really a binary")
    (directory / desktop_shell.DESKTOP_SHELL_RECEIPT_FILENAME).write_text(
        json.dumps({"tag": tag, "asset": "x", "sha256": "y"}), encoding="utf-8"
    )


def test_nothing_installed_means_nothing_to_update(shell_dir, monkeypatch):
    """The overwhelmingly common install has no desktop app at all.

    It must cost one directory check and no network: an updater that fetched a
    release for a machine that has never had a window would be a download
    nobody asked for.
    """

    def explode(*_args, **_kwargs):  # pragma: no cover - the point is it is not called
        raise AssertionError("nothing to update must not stage anything")

    monkeypatch.setattr(desktop_shell, "stage_desktop_shell", explode)
    result = desktop_shell.auto_update_desktop_shells()
    assert result.skipped == "already at the pin"
    assert result.message == ""


def test_a_binary_without_a_receipt_is_left_alone(shell_dir, monkeypatch):
    """A receipt is the proof this code installed the file beside it.

    Without one there is nothing here to keep up to date, and writing over
    somebody else's ``MyClaudeCode.exe`` because it shares a name would be
    indefensible.
    """

    (shell_dir / desktop_shell.desktop_shell_binary_name()).write_bytes(b"someone else")
    monkeypatch.setattr(
        desktop_shell,
        "stage_desktop_shell",
        lambda *a, **k: pytest.fail("staged a binary with no receipt"),
    )
    assert desktop_shell.auto_update_desktop_shells().skipped == "already at the pin"
    assert desktop_shell.desktop_shell_install_locations() == ()


def test_a_stale_receipt_is_updated_through_the_shared_path(shell_dir, monkeypatch):
    """One mechanism, one more caller. The staging code is not duplicated."""

    _install(shell_dir, "v0.0.1")
    calls: list[object] = []

    def fake_stage(target=None, **_kwargs):
        calls.append(target)
        return {
            "updated": True,
            "from_tag": "v0.0.1",
            "to_tag": desktop_shell.DESKTOP_SHELL_RELEASE_TAG,
            "staged_path": str(target),
            "restart_required": False,
        }

    monkeypatch.setattr(desktop_shell, "stage_desktop_shell", fake_stage)
    result = desktop_shell.auto_update_desktop_shells()
    assert calls == [shell_dir / desktop_shell.desktop_shell_binary_name()]
    assert result.updated == (shell_dir / desktop_shell.desktop_shell_binary_name(),)
    assert result.staged == ()
    assert result.message == (
        f"desktop app updated to {desktop_shell.DESKTOP_SHELL_RELEASE_TAG}"
    )


def test_a_running_binary_is_staged_and_the_line_says_so(shell_dir, monkeypatch):
    """Nothing is ever written over a running image.

    ``stage_desktop_shell`` decides that with one ``os.replace`` and the
    operating system's refusal; this only has to report the two outcomes
    differently, because "updated" and "will be used next time you start it"
    are different sentences to a person.
    """

    _install(shell_dir, "v0.0.1")
    monkeypatch.setattr(
        desktop_shell,
        "stage_desktop_shell",
        lambda target=None, **_k: {
            "updated": True,
            "staged_path": f"{target}.new",
            "restart_required": True,
        },
    )
    result = desktop_shell.auto_update_desktop_shells()
    assert result.staged and not result.updated
    assert result.message == (
        f"desktop app {desktop_shell.DESKTOP_SHELL_RELEASE_TAG} staged; it will "
        f"be used at the next app start"
    )


def test_the_setting_and_the_env_switch_and_a_live_helper_all_stop_it(
    shell_dir, monkeypatch
):
    _install(shell_dir, "v0.0.1")
    monkeypatch.setattr(
        desktop_shell,
        "stage_desktop_shell",
        lambda *a, **k: pytest.fail("staged while it should have been skipped"),
    )

    assert desktop_shell.auto_update_desktop_shells(enabled=False).skipped == "disabled"
    assert (
        desktop_shell.auto_update_desktop_shells(helper_is_installing=True).skipped
        == "an update helper is installing"
    )
    monkeypatch.setenv(desktop_shell.DESKTOP_SHELL_ENABLED_ENV, "off")
    skipped = desktop_shell.auto_update_desktop_shells().skipped
    assert skipped is not None and skipped.endswith("=off")


def test_a_download_that_fails_is_one_line_and_no_loop(shell_dir, monkeypatch):
    """Offline, behind a proxy, out of disk: one sentence, retried next start."""

    _install(shell_dir, "v0.0.1")

    def fail(*_args, **_kwargs):
        raise desktop_shell.DesktopShellError("the release page could not be reached.")

    monkeypatch.setattr(desktop_shell, "stage_desktop_shell", fail)
    result = desktop_shell.auto_update_desktop_shells()
    assert result.updated == () and result.staged == ()
    assert "could not be updated" in result.message
    assert "next time the server starts" in result.message


def test_the_hook_runs_once_per_server_start(monkeypatch):
    """A guard that a second readiness could get past would be no guard."""

    from my_claude_code.runtime import asgi

    asgi.reset_desktop_shell_auto_update_for_tests()
    runs: list[int] = []
    monkeypatch.setattr(asgi, "_desktop_shell_auto_update", lambda: runs.append(1))
    asgi.start_desktop_shell_auto_update()
    asgi.start_desktop_shell_auto_update()
    asgi.start_desktop_shell_auto_update()
    # The thread is a daemon; join through the flag rather than by sleeping.
    for thread in __import__("threading").enumerate():
        if thread.name == "mcc-desktop-shell-auto-update":
            thread.join(timeout=5)
    assert len(runs) == 1
    asgi.reset_desktop_shell_auto_update_for_tests()


def test_building_the_asgi_app_still_does_not_import_the_fetcher():
    """The contract this hook had to be written around.

    ``config/desktop_shell.py`` costs ``tarfile``, ``zipfile``, ``hashlib`` and
    ``urllib.request``, and none of that belongs in ``mcc-server``'s cold
    start. The hook therefore imports it lazily, on a thread, after the server
    is already answering -- so the import is paid for by the machine that has a
    desktop app and by nobody else. The strong version of this assertion lives
    in ``tests/contracts/test_desktop_shell_not_on_the_server_path.py``; this
    is the reminder at the point of use.
    """

    from pathlib import Path

    from my_claude_code.runtime import asgi

    source = Path(asgi.__file__).read_text(encoding="utf-8")
    head, _, body = source.partition("def _desktop_shell_auto_update")
    assert "desktop_shell" not in head, (
        "the shell fetcher must not be imported at module scope in runtime.asgi"
    )
    assert "from my_claude_code.config.desktop_shell import" in body
