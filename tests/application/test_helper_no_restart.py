"""GAP-3: the update helper stops being a second owner of "start the server".

The helper's restart is a single un-retried ``Start-Process`` whose failure is
recorded in ``progress.json`` and then acted on by nothing at all, and the
server it starts has no supervisor. Meanwhile the desktop window is sitting
there with a ten-second tick, a health probe and a spawn -- and the whole of
BUG-1 and BUG-3 existed because that window had been written to *defer* to the
helper.

So: when a window asked for the update, it owns the restart, and the helper
installs and exits. When nothing is watching -- the dashboard in a browser tab,
a headless machine -- the helper still restarts, because the alternative is an
update that leaves the machine with no server. One flag, not two owners.
"""

from pathlib import Path

import pytest

from my_claude_code.application import release_updates


def _script(*, no_restart: bool = False) -> str:
    """Render the helper for a Windows install, with every path spelled out.

    Named arguments rather than a dict of them: the generator's signature is
    the contract this test is about, and a `**kwargs` splat hides a renamed
    parameter behind a runtime failure instead of a type error.
    """

    return release_updates._deferred_helper_script(
        uv_executable="uv.exe",
        command=["uv.exe", "tool", "install", "my-claude-code"],
        result_path=Path("C:/stage/result.json"),
        stage_dir=Path("C:/stage"),
        server_launcher=Path("C:/bin/fcc-server.exe"),
        working_directory=Path("C:/work"),
        commands=["mcc-server.exe"],
        version="v9.9.9",
        no_restart=no_restart,
    )


def test_by_default_the_helper_still_starts_the_server():
    """The browser-tab path, unchanged. This is the compatibility half."""

    script = _script()
    assert "$noRestart = $false" in script
    assert "Start-Process -FilePath" in script
    assert "Write-Stage 'starting' 'Starting the updated server.'" in script


def test_with_no_restart_the_helper_installs_and_exits():
    script = _script(no_restart=True)
    assert "$noRestart = $true" in script
    # The Start-Process is still in the file -- it is inside `if (-not
    # $noRestart)`, because one script serves both callers and a second script
    # would be a second thing to keep correct.
    assert "if (-not $noRestart) {" in script
    assert "Handing the restart to the desktop app." in script
    assert "The desktop app starts it." in script


def test_the_terminal_stage_is_still_written_on_both_paths():
    """The window's ``RestartPending`` waits for exactly this.

    ``done`` / ``recovered`` is the fact the controller's post-update tick
    keys on: the installer is finished, one way or the other, so whatever is
    watching may start a server. A no-restart helper that stopped writing it
    would turn the fix into a hang.
    """

    for script in (_script(), _script(no_restart=True)):
        assert "Write-Stage 'done'" in script
        assert "Write-Stage 'recovered'" in script


def test_a_failed_install_says_who_will_start_the_old_version():
    script = _script(no_restart=True)
    assert "The desktop app starts the previous version again within ten seconds." in (
        script
    )


@pytest.mark.asyncio
async def test_the_flag_reaches_the_installer_from_the_dashboard(monkeypatch):
    """The whole chain, because a flag dropped in the middle is worse than no
    flag: the helper would not restart and neither would anyone else."""

    seen: dict[str, object] = {}

    async def fake_get(*_args, **_kwargs):
        return {"tag_name": "v99.0.0"}, 0.0, None

    monkeypatch.setattr(release_updates._CACHE, "get", fake_get)
    monkeypatch.setattr(release_updates, "current_version", lambda: "1.0.0")

    def fake_upgrade(payload, *, no_restart=False):
        seen["no_restart"] = no_restart
        return release_updates.UpgradeResult(ok=True, message="staged")

    monkeypatch.setattr(release_updates, "upgrade_to_latest", fake_upgrade)

    assert (await release_updates.perform_upgrade(no_restart=True)).ok
    assert seen["no_restart"] is True
    await release_updates.perform_upgrade()
    assert seen["no_restart"] is False


def test_the_spawner_hands_the_flag_to_the_script(monkeypatch, tmp_path):
    """And the last link: `_spawn_deferred_upgrade` -> the PowerShell itself."""

    seen: dict[str, object] = {}

    def fake_script(**kwargs):
        seen.update(kwargs)
        return "# script"

    monkeypatch.setattr(release_updates, "_deferred_helper_script", fake_script)
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: "powershell.exe")
    monkeypatch.setattr(release_updates, "_stage_dir", lambda: tmp_path)
    monkeypatch.setattr(
        release_updates, "_server_launcher", lambda *_a: tmp_path / "fcc-server.exe"
    )
    monkeypatch.setattr(release_updates, "_installed_tool_dir", lambda *_a: tmp_path)
    monkeypatch.setattr(release_updates.subprocess, "Popen", lambda *a, **k: object())
    monkeypatch.setattr(
        release_updates, "set_external_upgrade_helper_pending", lambda _v: None
    )
    result = release_updates._spawn_deferred_upgrade(
        uv_executable="uv.exe",
        command=["uv.exe", "tool", "install", "x"],
        tag="v9.9.9",
        log=[],
        no_restart=True,
    )
    assert result.ok
    assert seen["no_restart"] is True
    assert "the desktop app starts the updated server" in result.message
