"""Tests for version reporting and the dashboard-triggered upgrade."""

import os
import subprocess
from pathlib import Path

import pytest

from my_claude_code.application import release_updates
from my_claude_code.application.release_updates import (
    UpgradeResult,
    get_release_status,
    is_newer,
    parse_version,
    perform_upgrade,
    reset_cache_for_tests,
    upgrade_to_latest,
)
from my_claude_code.config import update_progress


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


def _release(tag: str = "v9.9.9", *, digest: str | None = None, name: str = "w.whl"):
    asset: dict[str, object] = {
        "name": name,
        "browser_download_url": f"https://example.invalid/{name}",
    }
    if digest is not None:
        asset["digest"] = f"sha256:{digest}"
    return {
        "tag_name": tag,
        "html_url": f"https://example.invalid/releases/{tag}",
        "name": f"{tag} - title",
        "published_at": "2026-07-30T23:09:20Z",
        "assets": [asset],
    }


# ----------------------------------------------------------------- versions


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4.14.2", (4, 14, 2)),
        ("v4.14.2", (4, 14, 2)),
        ("  V4.14.2  ", (4, 14, 2)),
        ("4.15", (4, 15)),
        ("", ()),
        (None, ()),
        ("not-a-version", ()),
    ],
)
def test_parse_version(text, expected) -> None:
    assert parse_version(text) == expected


def test_version_comparison_is_numeric_not_lexical() -> None:
    """4.14.10 must outrank 4.14.9; string comparison would get this wrong."""
    assert is_newer("4.14.10", "4.14.9") is True
    assert is_newer("4.14.9", "4.14.10") is False
    assert is_newer("v4.15.0", "4.14.2") is True
    assert is_newer("4.14.2", "4.14.2") is False


def test_unknown_versions_never_look_newer() -> None:
    assert is_newer(None, "4.14.2") is False
    assert is_newer("garbage", "4.14.2") is False
    assert is_newer("4.15.0", "unknown") is False


def test_current_version_falls_back_to_the_legacy_distribution(monkeypatch) -> None:
    """A migration install still running the legacy tool must not report unknown."""
    from importlib.metadata import PackageNotFoundError

    from my_claude_code.core.version import LEGACY_DISTRIBUTION, NATIVE_DISTRIBUTION

    def fake_installed(distribution: str) -> str:
        if distribution == NATIVE_DISTRIBUTION:
            raise PackageNotFoundError(NATIVE_DISTRIBUTION)
        assert distribution == LEGACY_DISTRIBUTION
        return "4.30.0"

    monkeypatch.setattr(release_updates, "installed_version", fake_installed)

    assert release_updates.current_version() == "4.30.0"


def test_current_version_prefers_the_native_distribution(monkeypatch) -> None:
    from my_claude_code.core.version import NATIVE_DISTRIBUTION

    def fake_installed(distribution: str) -> str:
        assert distribution == NATIVE_DISTRIBUTION
        return "5.0.1"

    monkeypatch.setattr(release_updates, "installed_version", fake_installed)

    assert release_updates.current_version() == "5.0.1"


def test_current_version_unknown_only_when_no_owner_installed(monkeypatch) -> None:
    from importlib.metadata import PackageNotFoundError

    def fake_installed(distribution: str) -> str:
        raise PackageNotFoundError(distribution)

    monkeypatch.setattr(release_updates, "installed_version", fake_installed)

    assert release_updates.current_version() == "unknown"


# ------------------------------------------------------------------ status


@pytest.mark.asyncio
async def test_status_reports_update_when_release_is_newer(monkeypatch) -> None:
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.14.2")

    async def _fetch():
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    status = await get_release_status()
    assert status.current == "4.14.2"
    assert status.latest == "4.15.0"
    assert status.update_available is True
    assert status.release_url is not None
    assert status.release_url.endswith("v4.15.0")


@pytest.mark.asyncio
async def test_status_has_no_update_when_current(monkeypatch) -> None:
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.15.0")

    async def _fetch():
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    status = await get_release_status()
    assert status.update_available is False


@pytest.mark.asyncio
async def test_offline_still_reports_the_running_version(monkeypatch) -> None:
    """A failed release check must never blank the version panel."""
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.14.2")

    async def _fetch():
        return None, "Could not reach the release feed (ConnectError)."

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    status = await get_release_status()
    assert status.current == "4.14.2"
    assert status.latest is None
    assert status.update_available is False
    assert status.error is not None
    assert "release feed" in status.error


@pytest.mark.asyncio
async def test_deferred_outcome_is_reported_once_then_consumed(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(release_updates, "_stage_dir", lambda: tmp_path)
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.15.0")
    receipt = tmp_path / release_updates._PENDING_RESULT_FILENAME
    receipt.write_text(
        '{"ok": true, "message": "Deferred install completed."}',
        encoding="utf-8",
    )

    async def _fetch():
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)

    first = await get_release_status()
    second = await get_release_status()

    assert first.pending_upgrade == {
        "ok": True,
        "message": "Deferred install completed.",
    }
    assert second.pending_upgrade is None
    assert not receipt.exists()


@pytest.mark.asyncio
async def test_release_lookup_is_cached_until_forced(monkeypatch) -> None:
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.14.2")
    calls = 0

    async def _fetch():
        nonlocal calls
        calls += 1
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    await get_release_status()
    await get_release_status()
    await get_release_status()
    assert calls == 1, "cached lookups must not re-hit the release feed"
    await get_release_status(force=True)
    assert calls == 2


# ----------------------------------------------------------------- upgrade


def _stub_download(monkeypatch, payload: bytes):
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self):
            yield payload

    class _Stream:
        def __enter__(self):
            return _Response()

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(release_updates.httpx, "stream", lambda *a, **k: _Stream())


def test_upgrade_requires_a_wheel_asset(monkeypatch) -> None:
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: "/usr/bin/uv")
    payload = _release()
    payload["assets"] = [{"name": "notes.txt"}]
    result = upgrade_to_latest(payload)
    assert result.ok is False
    assert "no wheel" in result.message


@pytest.mark.asyncio
async def test_perform_upgrade_declines_when_already_current(monkeypatch) -> None:
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.15.0")

    async def _fetch():
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    result = await perform_upgrade()
    assert result.ok is False
    assert "Already on the latest" in result.message


@pytest.mark.asyncio
async def test_perform_upgrade_runs_off_the_event_loop(monkeypatch) -> None:
    """The install is a slow subprocess and must not block the loop."""
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.14.2")

    async def _fetch():
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    threads: list[str] = []

    def _upgrade(_payload, **_kwargs):
        import threading

        threads.append(threading.current_thread().name)
        return UpgradeResult(ok=True, message="done", installed_version="4.15.0")

    monkeypatch.setattr(release_updates, "upgrade_to_latest", _upgrade)
    result = await perform_upgrade()
    assert result.ok is True
    assert threads and "MainThread" not in threads[0]


def test_extras_and_python_come_from_the_uv_receipt(monkeypatch, tmp_path) -> None:
    receipt = tmp_path / "uv-receipt.toml"
    receipt.write_text(
        "[tool]\n"
        'requirements = [{ name = "my-claude-code", path = "/x.whl",'
        ' extras = ["voice"] }]\n'
        'python = "3.14.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(release_updates, "_receipt_path", lambda _uv=None: receipt)
    extras, python = release_updates._installed_extras_and_python()
    assert extras == ["voice"]
    assert python == "3.14.0"


def test_uv_tool_paths_come_from_uv_not_a_posix_home_assumption(
    monkeypatch, tmp_path
) -> None:
    tool_dir = tmp_path / "platform" / "uv" / "tools"
    bin_dir = tmp_path / "platform" / "uv" / "bin"
    launcher = bin_dir / ("fcc-server.exe" if os.name == "nt" else "fcc-server")
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher", encoding="utf-8")

    def run(command, **_kwargs):
        value = str(bin_dir if "--bin" in command else tool_dir)
        return subprocess.CompletedProcess(command, 0, stdout=value + "\n", stderr="")

    monkeypatch.setattr(release_updates.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(release_updates.subprocess, "run", run)

    assert release_updates._uv_tool_dir("/tools/uv") == tool_dir
    assert release_updates._uv_tool_bin_dir("/tools/uv") == bin_dir
    assert release_updates._receipt_path("/tools/uv") == (
        tool_dir / release_updates.PACKAGE_NAME / "uv-receipt.toml"
    )
    assert release_updates._server_launcher("/tools/uv") == launcher


def test_wsl_drvfs_tool_directory_is_detected(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(release_updates, "_WINDOWS", False)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(
        release_updates, "_uv_tool_dir", lambda _uv=None: Path("/mnt/c/uv/tools")
    )

    assert release_updates._wsl_windows_mount_tool_dir("uv") is True

    monkeypatch.setattr(
        release_updates, "_uv_tool_dir", lambda _uv=None: tmp_path / "uv" / "tools"
    )
    assert release_updates._wsl_windows_mount_tool_dir("uv") is False


def test_missing_receipt_falls_back_to_the_running_python(monkeypatch) -> None:
    monkeypatch.setattr(
        release_updates,
        "_receipt_path",
        lambda _uv=None: Path("/definitely/missing.toml"),
    )
    extras, python = release_updates._installed_extras_and_python()
    assert extras == []
    assert python.count(".") == 2


# ------------------------------------------------------------- release notes


@pytest.mark.asyncio
async def test_status_carries_release_notes(monkeypatch) -> None:
    """A version number alone does not tell an operator whether to update."""

    monkeypatch.setattr(release_updates, "current_version", lambda: "4.14.2")
    payload = _release("v4.15.0")
    payload["body"] = "## Highlights\n\nSomething worth knowing about."

    async def _fetch():
        return payload, None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    status = await get_release_status()
    assert status.release_notes == "## Highlights\n\nSomething worth knowing about."
    assert status.as_dict()["release_notes"] == status.release_notes


@pytest.mark.asyncio
async def test_status_release_notes_absent_when_body_is_blank(monkeypatch) -> None:
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.14.2")
    payload = _release("v4.15.0")
    payload["body"] = "   \n  "

    async def _fetch():
        return payload, None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    status = await get_release_status()
    assert status.release_notes is None


def test_release_notes_are_bounded() -> None:
    """The feed is remote, so the banner shows an excerpt and links out."""

    trimmed = release_updates._release_notes("x" * 10_000)
    assert trimmed is not None
    assert len(trimmed) < 10_000
    assert trimmed.endswith("…")


def _stub_stream(body: bytes):
    """Minimal stand-in for httpx.stream yielding a fixed body."""

    class _Response:
        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield body

    class _Ctx:
        def __enter__(self):
            return _Response()

        def __exit__(self, *exc):
            return False

    def _stream(*args, **kwargs):
        return _Ctx()

    return _stream


# ------------------------------------------------- deferred Windows upgrade


def test_the_sweep_moves_old_tool_dirs_out_and_keeps_exactly_one(
    monkeypatch, tmp_path
) -> None:
    """Spec F8: four of these had accumulated, and nothing ever swept one.

    ``install.ps1``'s rename-then-reinstall ladder leaves
    ``my-claude-code.old-<stamp>`` inside uv's own tools root. uv normalises
    that directory name into the tool name ``my-claude-code-old-<stamp>``,
    which is a valid package name, so it reads it as a tool and prints
    ``warning: Ignoring malformed tool`` on every ``uv tool`` command.
    """

    tools_root = tmp_path / "uv" / "tools"
    live = tools_root / "my-claude-code"
    (live / "Scripts").mkdir(parents=True)
    (live / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    stamps = ("20260907-193858", "20260907-232333", "20260909-015046")
    for stamp in stamps:
        (tools_root / f"my-claude-code.old-{stamp}" / "Scripts").mkdir(parents=True)
    # And something that is not ours at all, which must not be touched.
    (tools_root / "ruff").mkdir()

    stage_dir = tmp_path / "config" / "updates"
    stage_dir.mkdir(parents=True)
    for index in range(8):
        (stage_dir / f"install-2026091{index}-000000.log").write_text(
            "x", encoding="utf-8"
        )

    monkeypatch.setattr(release_updates, "_installed_tool_dir", lambda: live)
    monkeypatch.setattr(release_updates, "_stage_dir", lambda: stage_dir)

    message = release_updates.sweep_superseded_environments()

    previous_root = tools_root.parent / update_progress.PREVIOUS_ENV_DIRNAME
    # Out of uv's way entirely: beside the tools root, not inside it.
    assert previous_root.parent == tools_root.parent
    assert not list(tools_root.glob("my-claude-code.old-*"))
    # Exactly one kept, and it is the newest.
    kept = sorted(child.name for child in previous_root.iterdir())
    assert kept == [max(stamps)]
    # The live environment and other people's tools are untouched.
    assert (live / "uv-receipt.toml").is_file()
    assert (tools_root / "ruff").is_dir()
    # Five transcripts, the most recent five.
    logs = sorted(path.name for path in stage_dir.glob("install-*.log"))
    assert len(logs) == 5
    assert logs[-1] == "install-20260917-000000.log"
    assert message is not None
    assert "superseded" in message


def test_the_sweep_says_nothing_when_there_is_nothing_to_do(
    monkeypatch, tmp_path
) -> None:
    """A sweep that ran and found nothing should be silent.

    Otherwise every single server start logs a line about housekeeping it did
    not do, and the one start where it mattered is invisible among them.
    """

    live = tmp_path / "uv" / "tools" / "my-claude-code"
    live.mkdir(parents=True)
    stage_dir = tmp_path / "config" / "updates"
    stage_dir.mkdir(parents=True)
    monkeypatch.setattr(release_updates, "_installed_tool_dir", lambda: live)
    monkeypatch.setattr(release_updates, "_stage_dir", lambda: stage_dir)

    assert release_updates.sweep_superseded_environments() is None


def test_the_sweep_is_harmless_outside_a_uv_tool_install(monkeypatch, tmp_path) -> None:
    """A development checkout has no tools root, and must not grow one."""

    stage_dir = tmp_path / "updates"
    stage_dir.mkdir()
    monkeypatch.setattr(release_updates, "_installed_tool_dir", lambda: None)
    monkeypatch.setattr(release_updates, "_stage_dir", lambda: stage_dir)

    assert release_updates.sweep_superseded_environments() is None
    # It made no directories of its own. (The hermetic-home fixture's own
    # directory is not this test's business.)
    assert not any(child.name.startswith(".mcc-") for child in tmp_path.rglob(".mcc-*"))


def test_the_aside_directory_is_not_inside_uvs_tools_root(tmp_path) -> None:
    """Measured, and the reason decision Q5's own spelling was not used.

    uv normalises a directory name inside its tools root into a *tool name*. A
    dot-prefixed one does not normalise to anything valid, and ``uv tool list``
    then fails outright with

        error: Not a valid package or extra name: ".mcc-previous".

    and lists nothing at all -- strictly worse than the malformed-tool warnings
    the ``my-claude-code.old-<stamp>`` directories produce, which is the bug
    this was meant to fix.
    """

    tool_dir = tmp_path / "uv" / "tools" / "my-claude-code"
    tools_root = tool_dir.parent
    for dirname in (
        update_progress.STAGING_ENV_DIRNAME,
        update_progress.PREVIOUS_ENV_DIRNAME,
    ):
        root = release_updates._aside_root(dirname, tool_dir)
        assert root is not None
        assert root.parent == tools_root.parent
        assert tools_root not in root.parents
        assert root != tools_root


def test_published_commands_covers_every_entry_point() -> None:
    """The shim list is read from the distribution, so it cannot drift."""

    commands = release_updates._published_commands()

    assert "mcc-claude" in commands
    assert "mcc-server" in commands
    # gui-scripts count too: a running tray holds its shim like any other.
    assert "mcc-desktop" in commands
    assert commands == sorted(commands)


def test_pending_upgrade_result_reports_a_failed_deferred_install(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(release_updates, "_stage_dir", lambda: tmp_path)
    (tmp_path / release_updates._PENDING_RESULT_FILENAME).write_text(
        '{"ok": false, "message": "Deferred install failed."}', encoding="utf-8"
    )
    assert release_updates.pending_upgrade_result() == {
        "ok": False,
        "message": "Deferred install failed.",
    }


def test_pending_upgrade_result_tolerates_missing_or_corrupt_file(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(release_updates, "_stage_dir", lambda: tmp_path)
    assert release_updates.pending_upgrade_result() is None
    (tmp_path / release_updates._PENDING_RESULT_FILENAME).write_text(
        "not json", encoding="utf-8"
    )
    assert release_updates.pending_upgrade_result() is None


def test_pending_upgrade_result_parses_a_utf8_bom_receipt(
    monkeypatch, tmp_path
) -> None:
    """Windows PowerShell 5.1 writes UTF-8 with a BOM; json.loads refuses it.

    Receipts written by an older helper sit on disk with that leading U+FEFF.
    The reader must still surface their outcome instead of permanently
    returning None and hiding what happened to the upgrade.
    """
    monkeypatch.setattr(release_updates, "_stage_dir", lambda: tmp_path)
    (tmp_path / release_updates._PENDING_RESULT_FILENAME).write_bytes(
        b'\xef\xbb\xbf{"ok": true}'
    )
    assert release_updates.pending_upgrade_result() == {"ok": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows process times")
def test_process_creation_filetime_matches_powershell() -> None:
    """The value must be comparable with Process.StartTime.ToFileTimeUtc()."""

    import subprocess as sp

    ours = release_updates._process_creation_filetime()
    assert ours > 0
    theirs = sp.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-Process -Id {os.getpid()}).StartTime.ToFileTimeUtc()",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert theirs == str(ours)


def test_creation_time_lookup_keys_off_the_real_platform(monkeypatch) -> None:
    """Flipping _WINDOWS for tests must not reach for a Win32 API.

    The staging test sets _WINDOWS=True to exercise that path on Linux CI; if
    the creation-time lookup keyed off the same flag it would call WinDLL and
    blow up there.
    """

    monkeypatch.setattr(release_updates, "_WINDOWS", True)
    monkeypatch.setattr(release_updates.os, "name", "posix")
    assert release_updates._process_creation_filetime() == 0


@pytest.mark.asyncio
async def test_status_exposes_the_dashboard_reconnect_timeout(monkeypatch) -> None:
    """The dashboard reads the reconnect window from the version payload."""
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.15.0")

    async def _fetch():
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    status = await get_release_status()
    payload = status.as_dict()
    assert "dashboard_reconnect_timeout_seconds" in payload
    assert payload["dashboard_reconnect_timeout_seconds"] > 0


@pytest.mark.asyncio
async def test_reconnect_timeout_tracks_the_configured_graceful_budget(
    monkeypatch,
) -> None:
    """The window uses the live graceful-shutdown setting, not the default."""
    from my_claude_code.config.settings import Settings

    monkeypatch.setattr(release_updates, "current_version", lambda: "4.15.0")
    graceful = 42.0
    monkeypatch.setattr(
        release_updates,
        "get_settings",
        lambda: Settings.model_construct(
            host="0.0.0.0",
            port=8082,
            anthropic_auth_token="freecc",
            model="nvidia_nim/test-model",
            open_admin_browser=False,
            server_graceful_shutdown_seconds=graceful,
        ),
    )

    async def _fetch():
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    status = await get_release_status()
    # install budget (900) + configured graceful (42) + startup margin (120).
    assert status.dashboard_reconnect_timeout_seconds == (
        release_updates._UPGRADE_TIMEOUT_SECONDS
        + graceful
        + release_updates._DASHBOARD_RECONNECT_STARTUP_MARGIN_SECONDS
    )


def test_the_progress_reader_takes_the_last_complete_line(
    tmp_path, monkeypatch
) -> None:
    """Reading back what the helper wrote, torn final line included."""

    # The reader moved to ``config.update_progress`` in 6.58.3 so ``cli`` could
    # use it without importing ``application``; ``release_updates`` re-exports
    # it, and this test still exercises it through the old name.
    monkeypatch.setattr(update_progress, "config_dir_path", lambda: tmp_path)
    assert release_updates.update_progress() is None

    path = release_updates.update_progress_path()
    assert (
        path
        == tmp_path
        / update_progress.UPDATE_STAGE_DIRNAME
        / release_updates.UPDATE_PROGRESS_FILENAME
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(
        '{"stage": "waiting-for-parent", "message": "Waiting."}\n'
        '{"stage": "installing", "message": "Installing the new version."}\n',
        encoding="utf-8",
    )
    assert release_updates.update_progress() == {
        "stage": "installing",
        "message": "Installing the new version.",
    }

    # A line still being appended costs one stale stage, not the whole file.
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"stage": "star')
    torn = release_updates.update_progress()
    assert torn is not None
    assert torn["stage"] == "installing"

    # And a BOM from Windows PowerShell 5.1 does not hide it.
    path.write_bytes(b'\xef\xbb\xbf{"stage": "done"}')
    assert release_updates.update_progress() == {"stage": "done"}


@pytest.mark.asyncio
async def test_status_carries_the_desktop_apps_pin(monkeypatch) -> None:
    from my_claude_code.config import desktop_shell

    monkeypatch.setattr(release_updates, "current_version", lambda: "6.60.0")

    async def _fetch():
        return None, None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    monkeypatch.setattr(desktop_shell, "installed_release_tag", lambda: "v6.43.0")

    payload = (await get_release_status(force=True)).as_dict()

    assert payload["shell_installed_tag"] == "v6.43.0"
    assert payload["shell_pinned_tag"] == desktop_shell.DESKTOP_SHELL_RELEASE_TAG
    assert payload["shell_update_available"] is True


def test_the_desktop_pin_is_decided_in_one_place(monkeypatch) -> None:
    """Not a second implementation of "is the app stale"."""

    from my_claude_code.config import desktop_shell

    status = release_updates.ReleaseStatus(current="6.60.0")
    monkeypatch.setattr(
        desktop_shell,
        "installed_release_tag",
        lambda: desktop_shell.DESKTOP_SHELL_RELEASE_TAG,
    )

    release_updates._apply_desktop_shell_state(status)

    assert status.shell_installed_tag == desktop_shell.DESKTOP_SHELL_RELEASE_TAG
    assert status.shell_update_available is False


def test_no_desktop_app_is_not_an_update_anybody_asked_for(monkeypatch) -> None:
    """Most installs have no window at all; a banner for one is noise."""

    from my_claude_code.config import desktop_shell

    status = release_updates.ReleaseStatus(current="6.60.0")
    monkeypatch.setattr(desktop_shell, "installed_release_tag", lambda: None)

    release_updates._apply_desktop_shell_state(status)

    assert status.shell_installed_tag is None
    assert status.shell_update_available is False


def test_a_receipt_that_cannot_be_read_still_renders_a_version(monkeypatch) -> None:
    """A banner must never be the reason a dashboard request fails."""

    from my_claude_code.config import desktop_shell

    status = release_updates.ReleaseStatus(current="6.60.0")

    def _explode() -> dict[str, object]:
        raise OSError("the receipt is on a drive that is not answering")

    monkeypatch.setattr(desktop_shell, "desktop_shell_update_report", _explode)

    release_updates._apply_desktop_shell_state(status)

    assert status.shell_installed_tag is None
    assert status.shell_pinned_tag is None
    assert status.shell_update_available is False


# -- 6.71.0: see everything happening during an update -------------------------


def test_the_upgrade_response_names_both_files_before_the_server_stops() -> None:
    """Spec F7: a browser tab loses its only channel the moment the server does.

    So the response that TRIGGERS the update carries the two paths, while there
    is still a server to carry them, and the dashboard shows them instead of
    one frozen sentence.
    """

    result = release_updates.UpgradeResult(
        ok=True,
        message="staged",
        log_path="C:/config/updates/install-20260911-082114.log",
        progress_path="C:/config/updates/progress.json",
    )
    payload = result.as_dict()
    assert payload["log_path"] == "C:/config/updates/install-20260911-082114.log"
    assert payload["progress_path"] == "C:/config/updates/progress.json"

    # And a result that has neither -- the POSIX path, where the install
    # happens in this process -- says so rather than inventing one.
    bare = release_updates.UpgradeResult(ok=False, message="no").as_dict()
    assert bare["log_path"] is None
    assert bare["progress_path"] is None


# -- 6.82.0: one update path. The helper is a LAUNCHER of the installer. ------
#
# Everything an update does -- download, verify, stage beside the running
# version, execute-verify, stop exactly one server, swap, start, health-gate,
# roll back -- now happens in scripts/install.ps1 and scripts/install.sh, which
# is the command a user would type. The tests that pinned the thousand-line
# PowerShell template in release_updates.py went with it; what is pinned here
# is the contract that replaced it, and the staged swap itself is pinned in
# tests/scripts/ against the real installers.


def _launcher_script(
    tmp_path: Path,
    *,
    version: str = "9.9.9",
    no_restart: bool = False,
    no_start: bool = False,
) -> str:
    """The helper as the server generates it.

    Named arguments rather than a ``**kwargs`` splat: the generator's signature
    is the contract these tests are about, and a splat hides a renamed
    parameter behind a runtime failure instead of a type error.
    """

    return release_updates._deferred_helper_script(
        result_path=tmp_path / "result.json",
        stage_dir=tmp_path,
        installer=tmp_path / "installers" / "install.ps1",
        powershell=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        config_dir=tmp_path / "config",
        working_directory=tmp_path / "cwd",
        version=version,
        no_restart=no_restart,
        no_start=no_start,
    )


def test_the_helper_is_a_launcher_of_the_official_installer(tmp_path) -> None:
    """Decision Q4/C1: the helper's only install verb is the installer.

    Until 6.82.0 this script was its own installer: it ran ``uv tool install``
    itself, four times over four code paths, and none of them was the command a
    user would type. Two implementations of one job is how they drifted until
    one of them could leave a machine with no server.
    """

    script = _launcher_script(tmp_path)
    assert "uv tool install" not in script
    assert "'tool', 'install'" not in script
    assert "UV_TOOL_DIR" not in script
    assert "[System.IO.Directory]::Move" not in script
    assert str(tmp_path / "installers" / "install.ps1") in script
    assert "'-Restart'" in script


def test_the_helper_waits_for_the_parent_before_it_runs_the_installer(
    tmp_path,
) -> None:
    """The one job only the server can do: it knows its own process id.

    The installer must not touch the environment the running interpreter lives
    in, so the wait comes first and the hand-over second.
    """

    script = _launcher_script(tmp_path)
    assert f"$parent = {os.getpid()}" in script
    assert script.index("Get-Process -Id $parent") < script.index("& $powershell")


def test_the_helper_pins_parent_identity_not_just_pid(tmp_path) -> None:
    """Windows recycles pids fast; a bare id match waits out the deadline."""

    script = _launcher_script(tmp_path)
    assert "$parentStart = " in script
    assert "$proc.StartTime.ToFileTimeUtc() -eq $parentStart" in script


def test_the_helper_never_claims_the_restart_is_somebody_elses(tmp_path) -> None:
    """Decision Q2: the watching flag no longer suppresses the restart.

    On 2026-09-11 the desktop window set this flag, the helper installed and
    started nothing "because the app owns the restart", the app was a build
    that could not act, and the machine had no server for fifteen minutes.
    The flag is now a fact for the transcript; the installer always restarts.
    """

    watched = _launcher_script(tmp_path, no_restart=True)
    unwatched = _launcher_script(tmp_path, no_restart=False)
    assert "'-Restart'" in watched
    assert "'-Restart'" in unwatched
    assert "'-NoStart'" not in watched
    # The only difference is what it says about who is looking.
    assert "A desktop window ' + $(if ($noRestart)" in watched
    # And the stage that used to mean "installed; somebody else starts it" is
    # never written by this script any more. (It stays in the shared stage
    # table, which is generated from Python, so look for a write of it.)
    assert "Write-Stage 'handing-off'" not in watched


def test_no_start_is_the_only_opt_out(tmp_path) -> None:
    """MCC_INSTALL_NO_START, for a machine that must not gain a server."""

    script = _launcher_script(tmp_path, no_start=True)
    assert "'-NoStart'" in script
    assert "'-Restart'" not in script


def test_the_helper_passes_the_version_it_was_offered(tmp_path) -> None:
    """The update that was offered is the update that happens.

    A newer release landing between "press Update" and "the installer runs"
    would otherwise install something the user never saw.
    """

    script = _launcher_script(tmp_path, version="9.9.9")
    assert "'-Version', '9.9.9'" in script


def test_the_helper_hands_over_one_transcript_and_the_config_dir(tmp_path) -> None:
    """One episode, one transcript -- and the restart means ONE server.

    MCC_INSTALL_LOG makes the installer append to this episode's transcript
    rather than open a second one, so a window tailing the file named in the
    receipt sees the whole story. MCC_CONFIG_DIR is explicit rather than merely
    inherited: on 2026-09-11 a start that lost it came up for a different
    configuration home and stopped the server that was already there.
    """

    script = _launcher_script(tmp_path)
    assert "$env:MCC_INSTALL_LOG = $installLog" in script
    assert "$env:MCC_CONFIG_DIR = $configDir" in script
    assert str(tmp_path / "config") in script


def test_the_helper_appends_to_the_receipt_and_never_truncates_it(tmp_path) -> None:
    """Decision Q5. A truncating writer erased a finished helper's whole record.

    And the receipt is written BOM-less: Windows PowerShell 5.1's `Set-Content
    -Encoding utf8` prepends a UTF-8 BOM, and the Python reader parses JSON,
    which refuses a leading U+FEFF.
    """

    script = _launcher_script(tmp_path)
    assert "AppendAllText($progressPath" in script
    assert "WriteAllText($progressPath" not in script
    assert "New-Object System.Text.UTF8Encoding($false)" in script
    assert "Write-Stage 'episode'" in script


def test_the_helper_does_not_overrule_the_installers_terminal_record(
    tmp_path,
) -> None:
    """The installer decides what happened; this script only reports failures
    it can see for itself.

    "The install exited 0" is not success -- a listener answering on the
    configured port is -- and the installer is the only thing that knows.
    """

    script = _launcher_script(tmp_path)
    assert '"helper_done":true' in script
    assert "if (-not $wroteTerminal) { Write-Stage 'failed'" in script


def test_the_stage_table_comes_from_python_not_a_second_copy(tmp_path) -> None:
    """Typed twice, the two copies disagreed the moment a stage was added."""

    script = _launcher_script(tmp_path)
    for stage, rank in update_progress.UPDATE_PROGRESS_STAGE_ORDER.items():
        assert f"'{stage}' = {rank}" in script


def test_the_upgrade_no_longer_downloads_or_installs_anything(
    monkeypatch, tmp_path
) -> None:
    """One downloader, one verifier, one installer -- and it is the installer.

    The dashboard used to download the wheel and check its digest, and then the
    installer it handed to downloaded the same wheel and checked the same
    digest again. Two downloaders is how the two paths came to disagree.
    """

    spawned: dict[str, object] = {}

    def _spawn(*, tag, log, no_restart=False):
        spawned["tag"] = tag
        spawned["no_restart"] = no_restart
        return UpgradeResult(ok=True, message="handed over", installed_version=tag)

    monkeypatch.setattr(release_updates, "_spawn_deferred_upgrade", _spawn)
    monkeypatch.setattr(release_updates, "_spawn_posix_upgrade", _spawn)

    def _never(*args, **kwargs):
        raise AssertionError("the dashboard must not download or install")

    monkeypatch.setattr(subprocess, "run", _never)

    result = upgrade_to_latest(_release("v9.9.9", digest="a" * 64))
    assert result.ok
    assert spawned["tag"] == "9.9.9"


def test_the_helper_does_not_pass_on_a_foreign_powershell_module_path(
    monkeypatch,
) -> None:
    """Measured on 2026-09-12, on the real update flow against a scratch install.

    A server started from a PowerShell 7 prompt inherits PowerShell 7's
    ``PSModulePath``. The helper starts **Windows PowerShell 5.1**, which
    autoloads ``Microsoft.PowerShell.Utility`` off that path, finds PowerShell
    7's copy, cannot load it, and then has no ``Get-FileHash``, no
    ``ConvertTo-Json`` and no ``Invoke-WebRequest`` for the rest of the run.
    The observed failure was the installer dying on the release wheel's
    checksum step:

        Get-FileHash : The term 'Get-FileHash' is not recognized ...
    """

    monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
    monkeypatch.setenv("MCC_KEEP_ME", "yes")
    environment = release_updates._powershell_child_environment()
    assert "PSModulePath" not in environment
    assert not any(name.upper() == "PSMODULEPATH" for name in environment)
    # Everything else is passed through: the child has to see MCC_CONFIG_DIR,
    # UV_TOOL_DIR and the rest of the configuration this server is running for.
    assert environment["MCC_KEEP_ME"] == "yes"


def test_the_upgrade_still_refuses_a_release_with_no_wheel(monkeypatch) -> None:
    """Nothing to install is still nothing to install."""

    payload = _release()
    payload["assets"] = []
    assert upgrade_to_latest(payload).ok is False


def test_the_posix_upgrade_runs_the_installer_detached(monkeypatch, tmp_path) -> None:
    """Decision Q7. Until 6.82.0 the POSIX update ran `uv tool install --force`
    in THIS process, synchronously, against the environment this process runs
    out of -- and then restarted nothing at all, ever, on any platform.
    """

    installer = tmp_path / "installers" / "install.sh"
    installer.parent.mkdir(parents=True)
    installer.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(release_updates, "_bundled_installer", lambda name: installer)
    monkeypatch.setattr(release_updates.shutil, "which", lambda name: "/bin/sh")
    monkeypatch.setattr(release_updates, "_stage_dir", lambda: tmp_path)
    arguments: list[str] = []
    options: dict[str, object] = {}
    environment: dict[str, str] = {}

    class _Popen:
        def __init__(self, command, **kwargs):
            arguments.extend(command)
            options.update(kwargs)
            environment.update(kwargs.get("env") or {})

    monkeypatch.setattr(release_updates.subprocess, "Popen", _Popen)
    result = release_updates._spawn_posix_upgrade(tag="9.9.9", log=[])
    assert result.ok
    assert arguments == [
        "/bin/sh",
        str(installer),
        "--restart",
        "--version",
        "9.9.9",
    ]
    # It must outlive this server and inherit none of its streams: a child that
    # keeps the caller's stdout open holds the caller open too.
    assert options["start_new_session"] is True
    assert environment["MCC_INSTALL_LOG"].endswith(".log")
    assert environment["MCC_CONFIG_DIR"]


def test_the_bundled_installer_is_looked_for_beside_the_package(tmp_path) -> None:
    """It ships inside the wheel, so the installer that runs is the one this
    release was tested with, needs no network of its own, and cannot be
    substituted between the download and the run."""

    found = release_updates._bundled_installer("install.ps1")
    # In a source checkout the wheel layout does not exist; the function must
    # answer None rather than hand back a path that is not there.
    assert found is None or found.is_file()
    assert release_updates._bundled_installer("not-an-installer") is None
