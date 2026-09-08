"""Tests for version reporting and the dashboard-triggered upgrade."""

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

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


def test_upgrade_refuses_a_wheel_whose_checksum_does_not_match(
    monkeypatch, tmp_path
) -> None:
    """Same refusal the install scripts make, so the UI path is not weaker."""
    # The Windows branch stages the download under ``_stage_dir()``, which is
    # ``config_dir_path()/"updates"``. Its three siblings each redirect that
    # (or turn ``_WINDOWS`` off); this one did neither, and wrote a real
    # ``~/.fcc/updates/wheel/w.whl`` on every run.
    monkeypatch.setattr(release_updates, "_stage_dir", lambda: tmp_path)
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: "/usr/bin/uv")
    _stub_download(monkeypatch, b"actual-bytes")
    ran = False

    def _run(*_args, **_kwargs):
        nonlocal ran
        ran = True
        raise AssertionError("must not install a mismatched wheel")

    monkeypatch.setattr(release_updates.subprocess, "run", _run)

    result = upgrade_to_latest(_release(digest="0" * 64))
    assert result.ok is False
    assert "checksum mismatch" in result.message
    assert ran is False


def test_upgrade_installs_a_verified_wheel(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(release_updates, "_WINDOWS", False)
    body = b"wheel-bytes"
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: "/usr/bin/uv")
    _stub_download(monkeypatch, body)
    monkeypatch.setattr(
        release_updates, "_installed_extras_and_python", lambda _uv=None: ([], "3.14.0")
    )
    captured: dict[str, list[str]] = {}

    def _run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    monkeypatch.setattr(release_updates.subprocess, "run", _run)

    result = upgrade_to_latest(_release("v4.15.0", digest=digest))
    assert result.ok is True
    assert result.installed_version == "4.15.0"
    assert "restart automatically" in result.message
    command = captured["command"]
    assert "--force" in command
    assert "--refresh-package" in command
    assert "3.14.0" in command


def test_upgrade_preserves_installed_extras(monkeypatch) -> None:
    """A reinstall must not silently drop voice support."""
    monkeypatch.setattr(release_updates, "_WINDOWS", False)
    body = b"wheel-bytes"
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: "/usr/bin/uv")
    _stub_download(monkeypatch, body)
    monkeypatch.setattr(
        release_updates,
        "_installed_extras_and_python",
        lambda _uv=None: (["voice"], "3.14.0"),
    )
    captured: dict[str, list[str]] = {}

    def _run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(release_updates.subprocess, "run", _run)

    result = upgrade_to_latest(_release("v4.15.0", digest=digest))
    assert result.ok is True
    assert any("[voice]" in str(part) for part in captured["command"])


def test_upgrade_reports_a_failing_install_command(monkeypatch) -> None:
    monkeypatch.setattr(release_updates, "_WINDOWS", False)
    body = b"wheel-bytes"
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: "/usr/bin/uv")
    _stub_download(monkeypatch, body)
    monkeypatch.setattr(
        release_updates, "_installed_extras_and_python", lambda _uv=None: ([], "3.14.0")
    )
    monkeypatch.setattr(
        release_updates.subprocess,
        "run",
        lambda command, **_k: subprocess.CompletedProcess(
            command, 2, stdout="", stderr="resolution failed"
        ),
    )
    result = upgrade_to_latest(_release("v4.15.0", digest=digest))
    assert result.ok is False
    assert "exited with code 2" in result.message
    assert any("resolution failed" in line for line in result.log)


def test_upgrade_without_uv_explains_itself(monkeypatch) -> None:
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: None)
    result = upgrade_to_latest(_release())
    assert result.ok is False
    assert "uv was not found" in result.message


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


def _deferred_script(tmp_path: Path, *, command: list[str] | None = None) -> str:
    return release_updates._deferred_helper_script(
        uv_executable="uv",
        command=command or ["uv", "tool", "install", "--force", "pkg"],
        result_path=tmp_path / "r.json",
        stage_dir=tmp_path,
        server_launcher=tmp_path / "bin" / "fcc-server.exe",
        working_directory=tmp_path / "cwd",
    )


def test_deferred_helper_script_waits_then_installs(tmp_path) -> None:
    """The helper must not run uv until this process is gone.

    Installing in place on Windows deletes the environment the running
    interpreter lives in, which fails partway and leaves it unusable.
    """

    script = release_updates._deferred_helper_script(
        uv_executable=r"C:\tools\uv.exe",
        command=[r"C:\tools\uv.exe", "tool", "install", "--force", "pkg"],
        result_path=tmp_path / "result.json",
        stage_dir=tmp_path,
        server_launcher=tmp_path / "bin" / "fcc-server.exe",
        working_directory=tmp_path / "cwd",
    )
    assert f"$parent = {os.getpid()}" in script
    # The wait loop must precede the install, not follow it.
    assert script.index("Get-Process -Id $parent") < script.index("tool")
    assert "'tool', 'install', '--force', 'pkg'" in script
    assert str(tmp_path / "result.json") in script
    result_write = script.index("[System.IO.File]::WriteAllText")
    launch = script.index("Start-Process -FilePath")
    assert result_write < launch
    assert str(tmp_path / "bin" / "fcc-server.exe") in script
    assert str(tmp_path / "cwd") in script
    assert "if ($ok)" in script[:launch]


def test_deferred_helper_script_quotes_hostile_arguments(tmp_path) -> None:
    """Release metadata reaches the wheel name; it must not break out."""

    script = _deferred_script(
        tmp_path, command=["uv", "tool", "install", "it's; rm -rf /"]
    )
    assert "'it''s; rm -rf /'" in script


def test_release_updates_renames_shims_before_reinstall(tmp_path) -> None:
    """The dashboard updater hits the same shim lock as the install script.

    uv writes the launcher shims in ASCII order of the file name including the
    ".exe" suffix and aborts the whole install on the first one it cannot
    overwrite, leaving every entrypoint after it unwritten and no receipt.
    Waiting for the server to exit does not help: an `mcc-claude` window the
    user still has open is a different process holding a different shim. So the
    helper must move every shim aside -- Windows refuses to delete a running
    image but happily renames one -- before it calls uv.
    """

    bin_dir = tmp_path / "bin"
    script = release_updates._deferred_helper_script(
        uv_executable=r"C:\tools\uv.exe",
        command=[r"C:\tools\uv.exe", "tool", "install", "--force", "pkg"],
        result_path=tmp_path / "result.json",
        stage_dir=tmp_path,
        server_launcher=bin_dir / "fcc-server.exe",
        working_directory=tmp_path / "cwd",
        bin_dir=bin_dir,
        commands=["mcc-claude", "mcc-server", "mcc-desktop"],
    )

    rename = script.index("Rename-Item -LiteralPath $shim")
    install = script.index(r"& 'C:\tools\uv.exe'")
    assert rename < install, (
        "the helper calls uv before moving the shims aside, so a launcher "
        "window still open aborts the install exactly as before"
    )
    assert str(bin_dir) in script
    for name in ("mcc-claude", "mcc-server", "mcc-desktop"):
        assert f"'{name}'" in script
    # Renamed aside, never deleted: the shim of a live window must keep working.
    assert "Remove-Item -LiteralPath $shim" not in script
    assert ".exe.old-" in script


def test_release_updates_receipt_lists_missing_commands(tmp_path) -> None:
    """A zero exit code is not proof that the commands exist.

    The shims are version-agnostic launchers, so an OLD shim reports the NEW
    version -- a version check can never catch a missing command. The helper
    must enumerate them and say which ones are absent.
    """

    script = release_updates._deferred_helper_script(
        uv_executable="uv",
        command=["uv", "tool", "install", "--force", "pkg"],
        result_path=tmp_path / "result.json",
        stage_dir=tmp_path,
        server_launcher=tmp_path / "bin" / "fcc-server.exe",
        working_directory=tmp_path / "cwd",
        bin_dir=tmp_path / "bin",
        commands=["mcc-claude", "mcc-rtk"],
    )

    assert "$missing = @()" in script
    assert "missing_commands = $missing" in script
    assert "Installed, but these commands are missing: " in script
    assert "Close the mcc-claude window(s) and re-run the install command." in script
    # ok is not the exit code alone.
    assert "$ok = ($code -eq 0) -and ($missing.Count -eq 0)" in script


def test_published_commands_covers_every_entry_point() -> None:
    """The shim list is read from the distribution, so it cannot drift."""

    commands = release_updates._published_commands()

    assert "mcc-claude" in commands
    assert "mcc-server" in commands
    # gui-scripts count too: a running tray holds its shim like any other.
    assert "mcc-desktop" in commands
    assert commands == sorted(commands)


def test_upgrade_stages_instead_of_installing_on_windows(monkeypatch, tmp_path) -> None:
    """On Windows the install is handed to a helper, never run in place."""

    monkeypatch.setattr(release_updates, "_WINDOWS", True)
    monkeypatch.setattr(release_updates, "_stage_dir", lambda: tmp_path)
    monkeypatch.setattr(release_updates.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        release_updates,
        "_server_launcher",
        lambda _uv=None: tmp_path / "fcc-server.exe",
    )
    monkeypatch.setattr(
        release_updates, "_installed_extras_and_python", lambda _uv=None: ((), "3.14")
    )

    def _boom(*args, **kwargs):
        raise AssertionError("uv must not run while the server is alive")

    monkeypatch.setattr(release_updates.subprocess, "run", _boom)
    spawned: dict[str, Any] = {}
    monkeypatch.setattr(
        release_updates.subprocess,
        "Popen",
        lambda argv, **kwargs: spawned.update(argv=argv, kwargs=kwargs),
    )

    wheel = tmp_path / "src.whl"
    wheel.write_bytes(b"wheel-bytes")
    payload = _release("v9.9.9", name="src.whl")
    monkeypatch.setattr(
        release_updates.httpx, "stream", _stub_stream(wheel.read_bytes())
    )

    result = release_updates.upgrade_to_latest(payload)

    assert result.ok is True
    assert "start the updated server automatically" in result.message.lower()
    assert spawned, "expected a detached helper to be spawned"
    # Detached + new process group so it outlives this server and its console.
    kwargs = spawned["kwargs"]
    # CREATE_NO_WINDOW, never DETACHED_PROCESS: the latter leaves powershell
    # without a console and it exits without running the script at all.
    assert kwargs["creationflags"] == (0x08000000 | 0x00000200)
    assert not kwargs["creationflags"] & 0x00000008


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


def test_deferred_helper_writes_the_receipt_without_a_bom(tmp_path) -> None:
    """Defense in depth: stop emitting the BOM the reader has to tolerate.

    ``Set-Content -Encoding utf8`` under PowerShell 5.1 prepends U+FEFF;
    ``[System.IO.File]::WriteAllText`` with ``UTF8Encoding($false)`` does not.
    Every write site -- timeout, final result, the rewritten uv receipt the
    staged fallback leaves behind, and the progress receipt's truncation and
    appends -- must use it, and the outcome must still be recorded before any
    relaunch attempt.
    """
    script = _deferred_script(tmp_path)
    # Five whole-file writes: the progress truncation, the "could not be
    # stopped" result, and the outcome receipt TWICE -- once before the server
    # is started and once after, so the receipt on disk is complete even if the
    # helper dies during the relaunch, and so it can then say whether a server
    # is running (6.58.3 starts one on the failure branch too).
    assert script.count("[System.IO.File]::WriteAllText") == 5
    assert script.count("[System.IO.File]::AppendAllText") == 1
    # One shared encoder object for the progress receipt, plus one at each of
    # the four whole-file writes that do not share it.
    assert script.count("UTF8Encoding($false)") == 5
    assert "Set-Content" not in script
    first_write = script.index("[System.IO.File]::WriteAllText")
    launch = script.index("Start-Process -FilePath")
    assert first_write < launch


def test_deferred_helper_survives_native_stderr(tmp_path) -> None:
    """uv writes progress to stderr; that must not kill the helper.

    Under ``$ErrorActionPreference = 'Stop'`` a native command's stderr becomes
    a terminating NativeCommandError, so the script died before installing
    anything and never wrote its result file.
    """

    script = _deferred_script(tmp_path)
    invoke = script.index("$output = &")
    # The native call must run with Continue in effect, not Stop.
    preference_before = script.rfind("$ErrorActionPreference = 'Continue'", 0, invoke)
    assert preference_before != -1, "native call must drop back to Continue"
    # Success is judged by the exit code captured immediately after the call --
    # and, since 6.30.1, by every published command actually being there.
    assert "$code = $LASTEXITCODE" in script
    assert "$ok = ($code -eq 0) -and ($missing.Count -eq 0)" in script


def test_deferred_helper_pins_parent_identity_not_just_pid(tmp_path) -> None:
    """Windows recycles pids fast; matching on the id alone hangs the helper.

    Observed in practice: the server was stopped, Windows handed its pid to an
    unrelated python process seconds later, and the helper waited out its whole
    deadline without ever installing.
    """

    script = _deferred_script(tmp_path)
    assert "$parentStart" in script
    assert "StartTime.ToFileTimeUtc()" in script
    # An unknown start time must mean "assume alive", never "assume gone":
    # installing while the server still runs is the corruption we avoid.
    assert "if ($parentStart -eq 0) { return $true }" in script


def test_deferred_helper_retries_the_install(tmp_path) -> None:
    """Handle release lags; one lost race must not leave a broken install."""

    script = _deferred_script(tmp_path)
    assert "$delays = @(0, 5, 10, 20, 30)" in script
    assert "foreach ($wait in $delays)" in script
    assert "if ($code -eq 0) { break }" in script


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


def test_the_helper_writes_a_progress_file_for_each_stage(tmp_path) -> None:
    """The measured 14-minute silent update leaves a trail now.

    The 23:19 update on the reporting machine left *no* trace whatsoever: the
    stop is not logged, the helper wrote no progress, and no pending receipt
    survived. The only forensic evidence that anything had happened was shim
    mtimes. One appended JSON line per stage is a dozen lines of PowerShell and
    it turns the single worst part of the experience -- a blank fourteen-minute
    wait -- into a status line the window can read.
    """

    script = _deferred_script(tmp_path)
    progress = str(tmp_path / release_updates.UPDATE_PROGRESS_FILENAME)

    assert progress in script
    for stage in release_updates.UPDATE_PROGRESS_STAGES:
        assert f"Write-Stage '{stage}'" in script, stage

    # The order the stages are written in is the order they happen in.
    waiting = script.index("Write-Stage 'waiting-for-parent'")
    installing = script.index("Write-Stage 'installing'")
    starting = script.index("Write-Stage 'starting'")
    done = script.index("Write-Stage 'done'")
    assert waiting < installing < starting < done

    # The first stage is written before the wait loop, not after it: the whole
    # point is to say something during the wait.
    assert waiting < script.index("while ((Get-Date) -lt $deadline)")
    # And installing is written before uv is invoked.
    assert installing < script.index("$delays = @(0, 5, 10, 20, 30)")

    # A fresh episode truncates: a stale 'done' from the previous update would
    # otherwise be the first thing a window reads and believes.
    truncate = script.index("[System.IO.File]::WriteAllText($progressPath, ''")
    assert truncate < waiting

    # Appended, never rewritten. The writer is a detached process that may be
    # killed at any point and the reader is a window polling while that
    # happens, so a rewrite could hand the reader a half-written document.
    assert "AppendAllText($progressPath" in script

    # A receipt nobody can write must never be the reason an update fails.
    assert "catch {" in script[script.index("function Write-Stage") :]


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


def test_the_helper_never_renames_the_shells_own_launcher(tmp_path) -> None:
    """`mcc-desktop.exe` is what the running window asks; it must stay put.

    The 2026-09-07 failure, in one file rename. The helper moved every managed
    shim aside, `mcc-desktop` included; the Tauri shell calls
    `mcc-desktop --print-status` on every pass of its ladder, read
    `NotInstalled`, and -- by design -- started its own `uv tool install` into
    the very tool directory the helper was writing. The helper lost all five
    attempts and never reached the step that starts a server.

    The rename bought nothing here: uv overwrites these in place, and one that
    happens to be locked is kept by the staged fallback exactly like any other
    shim.
    """

    bin_dir = tmp_path / "bin"
    script = release_updates._deferred_helper_script(
        uv_executable=r"C:\tools\uv.exe",
        command=[r"C:\tools\uv.exe", "tool", "install", "--force", "pkg"],
        result_path=tmp_path / "result.json",
        stage_dir=tmp_path,
        server_launcher=bin_dir / "fcc-server.exe",
        working_directory=tmp_path / "cwd",
        bin_dir=bin_dir,
        commands=["mcc-claude", "mcc-server", "mcc-desktop"],
    )

    assert "$neverRename = @(" in script
    for exempt in ("mcc-desktop.exe", "fcc-desktop.exe", "MyClaudeCode.exe"):
        assert f"'{exempt}'" in script, exempt
    # The exemption is applied in the rename loop, before the rename itself.
    loop = script[script.index("foreach ($fileName in ($managed.Keys") :]
    skip = loop.index("$neverRename -contains $fileName")
    assert skip < loop.index("Rename-Item -LiteralPath $shim")
    # It is an exemption from the RENAME only. The command is still verified as
    # present afterwards, so a genuinely missing mcc-desktop is still reported.
    assert "'mcc-desktop'" in script


def test_a_refused_rename_still_attempts_the_fast_install(tmp_path) -> None:
    """One `mcc-claude` window open must not cost the whole install its fast path.

    The guard was `if ($refused.Count -eq 0)`, so a single refused rename --
    and the user keeps `mcc-claude` windows open for hours, which refuses one --
    skipped the fast loop entirely. The evidence was `attempts = 5` rather than
    the 10 a fast loop plus a staged loop would give.
    """

    script = _deferred_script(tmp_path)

    assert "if ($refused.Count -eq 0) {\n    foreach ($wait in $delays)" not in script
    # The loop is no longer inside a refusal guard at all.
    fast = script.index("$fastDelays =")
    loop = script.index("foreach ($wait in $fastDelays)")
    assert fast < loop
    # A refusal buys one attempt rather than the full backoff: the staged
    # fallback behind it is the path that actually survives the lock, and 65
    # seconds of sleeps on the way there is not a fast path.
    assert (
        "if ($refused.Count -eq 0) {{ $delays }} else {{ @(0) }}".replace(
            "{{", "{"
        ).replace("}}", "}")
        in script
    )
    # And the staged fallback still runs when the fast loop failed.
    assert "if (($code -ne 0) -and $binDir) {" in script


def test_a_failed_install_still_starts_a_server(tmp_path) -> None:
    """A failed update must never leave the machine with no server.

    `Start-Process` used to sit under `if ($ok)`, so a helper that failed left
    the user with the old install on disk, nothing running, and no automatic
    recovery -- which is exactly what happened on 2026-09-07 at 23:24:04.
    """

    script = _deferred_script(tmp_path)

    start = script.index("Start-Process -FilePath")
    # Nothing between the receipt and the start gates it on success.
    preamble = script[script.index("$ok = ($code -eq 0)") : start]
    assert "if ($ok)" in preamble  # the stage line, which is allowed to branch
    assert script.count("Start-Process -FilePath") == 1
    # The start is outside every `if ($ok)` block: the terminal stage after it
    # is 'done' on success and 'recovered' on failure, and both are reached.
    assert "Write-Stage 'recovered'" in script
    assert script.index("Write-Stage 'recovered'") > start
    assert script.index("Write-Stage 'done'") > start
    # The outcome receipt says whether a server is running, and the message
    # says so in words, because the banner shows the message.
    assert "$result['restarted'] = $restarted" in script
    assert "The previous version was restarted." in script
    assert "The previous version could not be restarted either." in script


def test_the_helper_records_its_own_liveness(tmp_path) -> None:
    """Every receipt line names the helper, so nobody has to guess.

    A stage name alone cannot answer "is an installer running right now?" -- a
    helper killed mid-install leaves 'installing' behind forever -- and the
    answer is what stops the shell starting a second one.
    """

    script = _deferred_script(tmp_path)

    assert "$helperPid = $PID" in script
    assert "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()" in script
    for field in ("helper_pid = $helperPid", "started_at = $helperStarted"):
        assert field in script, field
    assert "helper_done = $script:HelperDone" in script
    assert "version = $targetVersion" in script
    # Set exactly where the helper is finished: before the terminal stages and
    # nowhere before the install.
    assert script.count("$script:HelperDone = $true") == 2
    assert script.index("$script:HelperDone = $true") < script.index(
        "Write-Stage 'installing'"
    )


def test_the_helper_names_the_version_it_is_installing(tmp_path) -> None:
    """So the window can say WHAT it is waiting for, not just that it waits."""

    script = release_updates._deferred_helper_script(
        uv_executable=r"C:\tools\uv.exe",
        command=[r"C:\tools\uv.exe", "tool", "install", "--force", "pkg"],
        result_path=tmp_path / "result.json",
        stage_dir=tmp_path,
        server_launcher=tmp_path / "bin" / "fcc-server.exe",
        working_directory=tmp_path / "cwd",
        bin_dir=tmp_path / "bin",
        version="6.58.3",
    )
    assert "$targetVersion = '6.58.3'" in script
    # And the kept-shim note tells the user what restarting that window buys.
    assert "' to pick up ' + $targetVersion" in script
    assert "'.exe (in use)'" in script
    assert "-- restart it" in script


def test_a_failed_install_puts_the_launchers_it_moved_aside_back(tmp_path) -> None:
    """Otherwise "the update failed" also means "and your server is gone".

    Found on a scratch install on 2026-09-08, running the real update path with
    an install that could not succeed: every managed shim had been renamed to
    `<name>.exe.old-<stamp>`, the new ones were never written, and the sweep at
    the end DELETED the aside copies. The helper's own restart step then
    reported "the previous version could not be restarted either" -- correctly,
    because there was no longer an `fcc-server.exe` to start. `install.ps1` has
    always restored on its failure path; the helper never did.
    """

    script = _deferred_script(tmp_path)

    assert "$movedAside += $fileName" in script
    restore = script.index(
        "if (($code -ne 0) -and $binDir) {\n    foreach ($fileName in $movedAside)"
    )
    verify = script.index("$missing = @()")
    start = script.index("Start-Process -FilePath")
    # Put back first, then judge what is missing, then start something.
    assert restore < verify < start
    # And the sweep that deletes the aside copies runs ONLY after an install
    # that worked -- on a failure they are the only launchers left.
    reap = script.index("Get-ChildItem -Path $binDir -Filter '*.exe.old-*'")
    guard = script.rindex("if ($code -eq 0) {", 0, reap)
    assert guard < reap
    # The receipt says which launchers came back, so a failure is legible.
    assert "restored_shims = $restoredShims" in script


# ------------------------------------------------- the desktop app's own pin
#
# The version panel used to answer "are you up to date" for the wheel alone.
# The wheel updates itself every release; the desktop app did not, because its
# pin was enforced by a process that need not be running -- so a user could sit
# fifteen releases behind and still read "Already up to date" (BUG-0). These
# three keys are what the banner renders.


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
