"""Decision Q2: the desktop app stops claiming the restart.

GAP-3 (6.70.0) made the helper hand the restart to whichever window had asked
for the update. The reasoning was sound -- the helper's restart was a single
un-retried ``Start-Process`` with no supervisor, while the window had a
ten-second tick, a health probe and a spawn of its own -- and on 2026-09-11 it
cost the user a fifteen-minute outage: the window that had claimed the job was
a v6.66.0 build that parked on its first failed status read, and because it had
claimed the job, nobody else did it. Three owners of "start the server", no
arbiter.

6.82.0 settles it. There is exactly ONE owner and it is the installer, on every
path, because the installer is the only participant that knows whether a
listener is answering on the configured port. The flag survives with a smaller
meaning: "a window is watching this, so there is no need to open a browser".
The window is an observer -- it reads the receipt and the transcript and
attaches when the server answers -- and its force-start remains only as U1's
never-park safety net, for the case where the installer itself failed.
"""

from pathlib import Path

import pytest

from my_claude_code.application import release_updates


def _script(*, no_restart: bool = False, no_start: bool = False) -> str:
    """Render the helper for a Windows install, with every path spelled out.

    Named arguments rather than a dict of them: the generator's signature is
    the contract this test is about, and a ``**kwargs`` splat hides a renamed
    parameter behind a runtime failure instead of a type error.
    """

    return release_updates._deferred_helper_script(
        result_path=Path("C:/stage/result.json"),
        stage_dir=Path("C:/stage"),
        installer=Path("C:/env/installers/install.ps1"),
        powershell="powershell.exe",
        config_dir=Path("C:/config"),
        working_directory=Path("C:/work"),
        version="9.9.9",
        no_restart=no_restart,
        no_start=no_start,
    )


def test_the_installer_restarts_the_server_whether_or_not_a_window_is_watching():
    """The whole of decision Q2, in one assertion each way."""

    for script in (_script(), _script(no_restart=True)):
        assert "'-Restart'" in script
        assert "'-NoStart'" not in script


def test_the_watching_flag_only_changes_what_the_transcript_says():
    """It is a fact about the machine, not a delegation of work."""

    watched = _script(no_restart=True)
    assert "$noRestart = $true" in watched
    assert "$noRestart = $false" in _script()
    assert "A desktop window ' + $(if ($noRestart)" in watched
    # The two sentences that promised somebody else would act are gone.
    assert "Handing the restart to the desktop app." not in watched
    assert "The desktop app starts it." not in watched
    assert "within ten seconds" not in watched


def test_only_no_start_stops_a_server_from_being_started():
    """MCC_INSTALL_NO_START is the real opt-out, and it is the only one."""

    script = _script(no_start=True)
    assert "'-NoStart'" in script
    assert "'-Restart'" not in script


def test_the_episode_still_ends_on_every_path():
    """The window's post-update tick keys on a terminal record.

    The installer writes it in the ordinary case; the helper writes one when
    the installer never got far enough to. A path that wrote none would turn
    a fix into a hang.
    """

    for script in (_script(), _script(no_restart=True)):
        assert "Write-Stage 'failed'" in script
        assert '"helper_done":true' in script


@pytest.mark.asyncio
async def test_the_flag_reaches_the_installer_from_the_dashboard(monkeypatch):
    """The whole chain, because a flag dropped in the middle is invisible."""

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

    installer = tmp_path / "install.ps1"
    installer.write_text("param()\n", encoding="utf-8")
    monkeypatch.setattr(release_updates, "_deferred_helper_script", fake_script)
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: "powershell.exe")
    monkeypatch.setattr(release_updates, "_stage_dir", lambda: tmp_path)
    monkeypatch.setattr(release_updates, "_bundled_installer", lambda _n: installer)
    monkeypatch.setattr(release_updates.subprocess, "Popen", lambda *a, **k: object())
    monkeypatch.setattr(
        release_updates, "set_external_upgrade_helper_pending", lambda _v: None
    )
    result = release_updates._spawn_deferred_upgrade(
        tag="9.9.9", log=[], no_restart=True
    )
    assert result.ok
    assert seen["no_restart"] is True
    assert seen["installer"] == installer
    # And the message no longer tells the user that something else will start
    # their server.
    assert "desktop app starts" not in result.message
    assert "started again" in result.message
