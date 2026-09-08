"""``mcc-desktop --print-status``: the JSON status surface a shell consumes.

The window that renders the dashboard runs in a second process, and it must not
learn where the config lives, which port to use, or how long to wait for a
restart. It asks Python. These tests pin the three properties that make that
safe: the document carries every documented key (C3), it is a *pure read* (C2),
and the answers come from the existing single sources rather than from copies.
"""

import io
import json
import sys
from contextlib import redirect_stdout

import pytest

from my_claude_code.cli import desktop as desktop_module
from my_claude_code.cli import desktop_entrypoint
from my_claude_code.cli import desktop_status as desktop_status_module
from my_claude_code.cli.desktop_status import (
    STATUS_KEYS,
    STATUS_SCHEMA,
    desktop_status,
    reconnect_timeout_seconds,
)
from my_claude_code.cli.launchers.common import PreflightResult
from my_claude_code.cli.port_diagnostics import PortOwner
from my_claude_code.config import paths
from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.startup_state import (
    STARTING_MARKER_HEADER,
    STARTING_MARKER_VALUE,
)
from my_claude_code.core.stop_deadline import (
    SHUTDOWN_MARKER_HEADER,
    SHUTDOWN_MARKER_VALUE,
)

#: The type every documented key must carry. Retyping one is exactly as
#: breaking to a reader as removing it, so both are guarded here and both cost
#: a ``schema`` bump.
EXPECTED_TYPES: dict[str, type | tuple[type, ...]] = {
    "schema": int,
    "version": str,
    "config_dir": str,
    "config_dir_source": str,
    "config_dir_is_legacy": bool,
    "host": str,
    "port": int,
    "root_url": str,
    "admin_url": str,
    "health_url": str,
    "server_presence": str,
    # The stage a `starting` server named, and ``null`` for every other
    # presence. Added in 6.59.0 alongside the presence itself; adding a key
    # does not bump `schema`, because a reader must tolerate one it does not
    # know (C3).
    "server_starting_stage": (str, type(None)),
    "port_conflict": (str, type(None)),
    "server_mode": str,
    "window": str,
    "window_open": bool,
    "window_width": int,
    "window_height": int,
    "tray_enabled": bool,
    "minimize_to_tray": bool,
    "close_to_tray": bool,
    "start_at_login": bool,
    "autostart_reconcile": bool,
    "server_log": str,
    "start_timeout_seconds": float,
    "server_start_retries": int,
    "health_check_interval_seconds": float,
    "health_poll_seconds": float,
    "health_failure_threshold": int,
    "activation_poll_seconds": float,
    "reconnect_timeout_seconds": float,
    "reconnect_restatus_seconds": float,
    "shell_tray": bool,
    "shell_binary": (str, type(None)),
    "shell_release_tag": str,
    # What the receipt beside the installed binary says, and ``null`` when
    # there is no receipt we wrote. Added in 6.60.0: the shell compares its own
    # compiled-in tag with ``shell_release_tag`` above and this is the other
    # half of the same question -- "is the file on disk old" as against "is the
    # window running old". Tolerated by the 6.60.0 shell, requirable in 6.61.0.
    "shell_installed_tag": (str, type(None)),
    "shell_ready": bool,
    # ``null`` unless an update helper is installing right now. A document,
    # not a flag, because the window renders it: which stage, which version,
    # how long. See ``config.update_progress``.
    "update": (dict, type(None)),
}


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point every config lookup at a scratch directory, then forget the cache."""

    directory = tmp_path / "config"
    directory.mkdir()
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(directory))
    paths.reset_config_dir_cache()
    return directory


def _settings(monkeypatch, **overrides) -> Settings:
    """Publish a built ``Settings`` the way every other CLI test does.

    ``Settings`` reads the environment once per process here -- the suite
    pins ``model_config["env_file"] = None`` and the first build wins -- so a
    ``monkeypatch.setenv`` inside a test changes nothing. The established
    pattern (``tests/cli/test_entrypoints.py:24``) is to construct the object
    and publish it where the code under test looks it up.
    """

    settings = Settings.model_construct(**overrides)
    monkeypatch.setattr(desktop_status_module, "get_settings", lambda: settings)
    return settings


def _preflight(presence: str) -> PreflightResult:
    """The probe result each rung of the ladder is built from.

    ``draining`` is spelled as the exact head MCC's own shutdown gate sends,
    because the whole point of the value is that it is recognised from that
    head rather than from anything the caller already knew.
    """

    if presence == "healthy":
        return PreflightResult(status_code=200)
    if presence == "draining":
        return PreflightResult(
            status_code=503,
            headers={
                "content-type": "application/json",
                "connection": "close",
                "retry-after": "5",
                SHUTDOWN_MARKER_HEADER: SHUTDOWN_MARKER_VALUE,
            },
            error="returned HTTP 503",
        )
    if presence == "starting":
        # Spelled as the exact head the startup gate sends, for the same reason
        # ``draining`` is: the value only means anything if it is recognised
        # from the wire rather than from what the caller already knew.
        return PreflightResult(
            status_code=503,
            headers={
                "content-type": "application/json",
                "retry-after": "1",
                STARTING_MARKER_HEADER: STARTING_MARKER_VALUE,
            },
            error="returned HTTP 503",
            body=json.dumps(
                {
                    "status": "starting",
                    "stage": "configured-models",
                    "elapsed_ms": 4321,
                }
            ),
        )
    return PreflightResult(error="unreachable")


def _presence(monkeypatch, presence: str) -> None:
    """Drive the real ``probe_server_presence`` ladder from its primitives."""

    monkeypatch.setattr(
        desktop_module, "preflight_result", lambda url: _preflight(presence)
    )
    monkeypatch.setattr(
        desktop_module,
        "probe_port_available",
        # A draining server still holds the socket, so the port is not free.
        lambda host, port: presence == "free",
    )


def test_emits_every_documented_key(config_dir, monkeypatch) -> None:
    """The golden key set: nothing added silently, nothing dropped silently."""
    _presence(monkeypatch, "healthy")

    payload = desktop_status()

    assert tuple(payload) == STATUS_KEYS
    assert payload["schema"] == STATUS_SCHEMA == 1
    wrong = {
        key: type(payload[key]).__name__
        for key, expected in EXPECTED_TYPES.items()
        if not isinstance(payload[key], expected)
    }
    assert not wrong, (
        "these keys changed type, which breaks every reader just as hard as "
        f"removing them -- bump `schema` deliberately: {wrong}"
    )
    assert set(EXPECTED_TYPES) == set(STATUS_KEYS)


def test_reports_healthy_foreign_and_free(config_dir, monkeypatch) -> None:
    """All three rungs of the Q7 ladder reach the document unchanged."""
    for presence in ("healthy", "free", "foreign"):
        _presence(monkeypatch, presence)
        monkeypatch.setattr(
            desktop_module, "diagnose_port_owner", lambda host, port: None
        )

        assert desktop_status()["server_presence"] == presence


def test_a_draining_server_is_not_reported_as_foreign(config_dir, monkeypatch) -> None:
    """The defect this release exists for.

    While the shutdown gate is refusing with 503 the port is still bound, so
    the old ladder fell straight through to ``foreign`` -- and ``foreign``
    carries a message accusing MCC's own process of not being the MCC server.
    That is the page a user lands on every time they close and relaunch the
    desktop app during a slow restart, which is precisely the workaround they
    reported using.
    """

    _presence(monkeypatch, "draining")
    monkeypatch.setattr(
        desktop_module,
        "diagnose_port_owner",
        lambda host, port: PortOwner(pid=42112, name="python.exe", command=None),
    )

    opted_in = desktop_status(presence_v2=True)
    assert opted_in["server_presence"] == "draining"
    # No conflict message: there is no conflict. A stale one here would be a
    # sentence about MCC's own process being an impostor.
    assert opted_in["port_conflict"] is None


def test_the_draining_presence_is_only_reported_to_a_caller_that_asked(
    config_dir, monkeypatch
) -> None:
    """C3: an old shell must never be handed a state it has no branch for.

    The desktop shell refuses an unknown presence loudly rather than guessing
    at the nearest neighbour -- the right instinct, and the reason a new value
    cannot simply be switched on for every reader at once. A window built
    before 6.50.0 does not pass ``--presence-v2`` and keeps seeing the three
    values it was written against.
    """

    _presence(monkeypatch, "draining")
    monkeypatch.setattr(desktop_module, "diagnose_port_owner", lambda host, port: None)

    assert desktop_status()["server_presence"] == "foreign"
    assert desktop_status(presence_v2=False)["server_presence"] == "foreign"
    assert desktop_status(presence_v2=True)["server_presence"] == "draining"


def test_a_stranger_on_the_port_is_still_foreign_while_draining_exists(
    config_dir, monkeypatch
) -> None:
    """A plain 503 from something that is not MCC is not claimed as ours.

    Any reverse proxy on the configured port can answer 503. Reading that as
    "My Claude Code is restarting" would replace one wrong page with another.
    """

    monkeypatch.setattr(
        desktop_module,
        "preflight_result",
        lambda url: PreflightResult(
            status_code=503,
            headers={"server": "nginx"},
            error="returned HTTP 503",
        ),
    )
    monkeypatch.setattr(
        desktop_module, "probe_port_available", lambda host, port: False
    )
    monkeypatch.setattr(
        desktop_module,
        "diagnose_port_owner",
        lambda host, port: PortOwner(pid=99, name="nginx.exe", command=None),
    )

    payload = desktop_status(presence_v2=True)
    assert payload["server_presence"] == "foreign"
    assert "nginx.exe (pid 99)" in payload["port_conflict"]


def test_reconnect_restatus_seconds_is_in_the_golden_key_set(
    config_dir, monkeypatch
) -> None:
    """The cadence a reconnecting window re-reads this document at.

    C9: a shell that compiled in its own "every sixth tick" would be deciding
    how often to run a process on the user's machine.
    """

    assert "reconnect_restatus_seconds" in STATUS_KEYS
    _settings(monkeypatch, desktop_reconnect_restatus_seconds=12.5)
    _presence(monkeypatch, "healthy")

    assert desktop_status()["reconnect_restatus_seconds"] == 12.5


def test_server_start_retries_is_in_the_golden_key_set(config_dir, monkeypatch) -> None:
    """How many attempts a start gets before the window calls it a failure.

    C9 again, and the whole of the reported defect: a window that compiled in
    "one attempt of fifteen seconds" gave up seven seconds before a server
    that took twenty-two seconds to bind actually answered.
    """

    assert "server_start_retries" in STATUS_KEYS
    _settings(monkeypatch, desktop_server_start_retries=2)
    _presence(monkeypatch, "healthy")

    assert desktop_status()["server_start_retries"] == 2


def test_server_start_retries_defaults_to_two_further_attempts(
    config_dir, monkeypatch
) -> None:
    """Three attempts of the start timeout, which is the user's decision.

    15 s x 3 = 45 s of probing before anything that looks like a failure is
    shown, against a measured 22-25 s startup on a real configuration.
    """

    _settings(monkeypatch)
    _presence(monkeypatch, "free")

    payload = desktop_status()
    assert payload["server_start_retries"] == 2
    assert (
        payload["start_timeout_seconds"] * (payload["server_start_retries"] + 1) == 45.0
    )


def test_carries_the_port_conflict_message_when_foreign(
    config_dir, monkeypatch
) -> None:
    """A stranger on the port is named; anything else leaves the key null."""
    monkeypatch.setattr(
        desktop_module,
        "diagnose_port_owner",
        lambda host, port: PortOwner(pid=4242, name="node.exe", command=None),
    )

    _presence(monkeypatch, "foreign")
    payload = desktop_status()
    assert payload["port_conflict"] == desktop_module.port_conflict_message(
        get_settings()
    )
    assert "node.exe (pid 4242)" in payload["port_conflict"]

    _presence(monkeypatch, "healthy")
    assert desktop_status()["port_conflict"] is None


def test_does_not_acquire_the_singleton_lock(config_dir, monkeypatch) -> None:
    """Reading a status must never make a second tray impossible to start."""
    _presence(monkeypatch, "free")

    def explode(*args, **kwargs):
        raise AssertionError("--print-status took the desktop singleton lock")

    monkeypatch.setattr(desktop_module.InterprocessFileLock, "__init__", explode)

    desktop_status()

    assert not (config_dir / desktop_module.LOCK_FILENAME).exists()
    assert not (config_dir / desktop_module.ACTIVATION_FILENAME).exists()


def test_does_not_write_desktop_json(config_dir, monkeypatch) -> None:
    """A pure read: no state file, no autostart registration, no spawn."""
    from my_claude_code.config import desktop as desktop_config

    _presence(monkeypatch, "free")
    for name in ("save_desktop_state", "apply_start_at_login", "remove_start_at_login"):
        monkeypatch.setattr(
            desktop_config,
            name,
            lambda *args, _name=name, **kwargs: pytest.fail(
                f"--print-status called {_name}"
            ),
        )
    monkeypatch.setattr(
        desktop_module.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("--print-status spawned a process"),
    )

    before = sorted(path.name for path in config_dir.iterdir())
    desktop_status()

    assert sorted(path.name for path in config_dir.iterdir()) == before
    assert not (config_dir / desktop_config.DESKTOP_STATE_FILENAME).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="HKCU exists only on Windows")
def test_does_not_touch_the_windows_registry(config_dir, monkeypatch) -> None:
    """Reading a status must never reconcile the HKCU ``Run`` value.

    A previous full-suite run deleted the developer's real autostart entry.
    ``--print-status`` is the one desktop command that must be safe to run on a
    live machine, so every writing registry call is replaced by a recorder and
    the recorder must stay empty.
    """
    import winreg

    calls: list[str] = []
    for name in ("SetValue", "SetValueEx", "DeleteValue", "DeleteKey", "CreateKey"):
        monkeypatch.setattr(
            winreg,
            name,
            lambda *args, _name=name, **kwargs: calls.append(_name),
            raising=False,
        )
    _presence(monkeypatch, "free")

    desktop_status()

    assert calls == []


def test_honours_MCC_CONFIG_DIR(tmp_path, monkeypatch) -> None:
    """``resolve_config_dir`` stays the single source, override included."""
    override = tmp_path / "elsewhere"
    override.mkdir()
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(override))
    paths.reset_config_dir_cache()
    _presence(monkeypatch, "free")

    payload = desktop_status()

    assert payload["config_dir"] == str(override)
    assert payload["config_dir_source"] == "env"
    assert payload["config_dir_is_legacy"] is False
    assert payload["server_log"].startswith(str(override))


def test_reports_a_legacy_fcc_home(tmp_path, monkeypatch) -> None:
    """A legacy home is reported as one, so the shell can say so out loud."""
    home = tmp_path / "home"
    (home / paths.LEGACY_CONFIG_DIRNAME).mkdir(parents=True)
    monkeypatch.delenv(paths.CONFIG_DIR_ENV, raising=False)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        paths, "check_legacy_home", lambda path: paths.LegacyHomeHealth(healthy=True)
    )
    paths.reset_config_dir_cache()
    _presence(monkeypatch, "free")

    payload = desktop_status()

    assert payload["config_dir"] == str(home / paths.LEGACY_CONFIG_DIRNAME)
    assert payload["config_dir_source"] == "legacy"
    assert payload["config_dir_is_legacy"] is True


def test_maps_a_wildcard_bind_to_loopback(config_dir, monkeypatch) -> None:
    """``0.0.0.0`` is a bind, not an address a window can navigate to."""
    _settings(monkeypatch, host="0.0.0.0", port=8199)
    _presence(monkeypatch, "healthy")

    payload = desktop_status()

    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 8199
    assert payload["root_url"] == "http://127.0.0.1:8199"
    assert payload["admin_url"] == "http://127.0.0.1:8199/admin"
    assert payload["health_url"] == "http://127.0.0.1:8199/health"


def test_prints_one_json_document_and_exits_zero(config_dir, monkeypatch) -> None:
    """Stdout is a machine's input: one parseable document, nothing else."""
    _presence(monkeypatch, "healthy")

    stream = io.StringIO()
    with redirect_stdout(stream):
        desktop_entrypoint.launch(["--print-status"])

    payload = json.loads(stream.getvalue())
    assert tuple(payload) == STATUS_KEYS


def test_the_presence_v2_flag_reaches_the_document(config_dir, monkeypatch) -> None:
    """``--print-status --presence-v2`` is the shell's opt-in, end to end."""

    _presence(monkeypatch, "draining")
    monkeypatch.setattr(desktop_module, "diagnose_port_owner", lambda host, port: None)

    def rendered(argv: list[str]) -> dict:
        stream = io.StringIO()
        with redirect_stdout(stream):
            desktop_entrypoint.launch(argv)
        return json.loads(stream.getvalue())

    assert rendered(["--print-status"])["server_presence"] == "foreign"
    assert (
        rendered(["--print-status", "--presence-v2"])["server_presence"] == "draining"
    )
    # The key set does not move with the flag: one document, one shape.
    assert tuple(rendered(["--print-status", "--presence-v2"])) == STATUS_KEYS


def test_an_unknown_flag_after_print_status_is_still_a_usage_error(
    config_dir, monkeypatch
) -> None:
    """The flag is an allow-list of one, not "anything after --print-status"."""

    _presence(monkeypatch, "healthy")
    with pytest.raises(SystemExit) as exit_info:
        desktop_entrypoint.launch(["--print-status", "--presence-v3"])
    assert exit_info.value.code == 2


def test_never_prints_a_key_or_a_token(config_dir, monkeypatch) -> None:
    """The document is safe to paste into a bug report."""
    _settings(
        monkeypatch,
        anthropic_api_key="sk-ant-secret-value",
        nvidia_nim_api_key="nvapi-secret-value",
        anthropic_auth_token="proxy-secret-value",
    )
    _presence(monkeypatch, "healthy")

    rendered = json.dumps(desktop_status())

    for secret in ("sk-ant-secret-value", "nvapi-secret-value", "proxy-secret-value"):
        assert secret not in rendered
    assert not any(
        token in key for key in STATUS_KEYS for token in ("key", "token", "secret")
    )


def test_reconnect_budget_follows_the_configured_drain(config_dir, monkeypatch) -> None:
    """C9's number is the dashboard's number, recomputed, never a copy."""
    from my_claude_code.application import release_updates

    settings = _settings(monkeypatch, server_graceful_shutdown_seconds=45.0)

    assert reconnect_timeout_seconds(settings) == (
        release_updates._UPGRADE_TIMEOUT_SECONDS
        + settings.server_graceful_shutdown_seconds
        + release_updates._DASHBOARD_RECONNECT_STARTUP_MARGIN_SECONDS
    )
    _presence(monkeypatch, "healthy")
    assert desktop_status()["reconnect_timeout_seconds"] == 1065.0


def test_reconnect_timeout_matches_release_updates_for_a_300_second_drain(
    config_dir, monkeypatch
) -> None:
    """The exact number this investigation measured on the reporting machine.

    ``SERVER_GRACEFUL_SHUTDOWN_SECONDS=300`` in a user's ``.env`` is what turned
    the banner's "17 minutes" into "22 minutes" and every update's drain into
    five silent minutes. The arithmetic is pinned here so a later change to the
    formula cannot quietly move it again.
    """

    from my_claude_code.application import release_updates

    settings = _settings(monkeypatch, server_graceful_shutdown_seconds=300.0)
    _presence(monkeypatch, "healthy")

    assert desktop_status()["reconnect_timeout_seconds"] == 1320.0
    assert reconnect_timeout_seconds(settings) == (
        release_updates._UPGRADE_TIMEOUT_SECONDS
        + 300.0
        + release_updates._DASHBOARD_RECONNECT_STARTUP_MARGIN_SECONDS
    )

    # And the shipped default is the 17.3 minutes the banner should show.
    default_settings = _settings(monkeypatch, server_graceful_shutdown_seconds=20.0)
    assert reconnect_timeout_seconds(default_settings) == 1040.0


def test_reports_that_autostart_reconciliation_is_switched_off(
    config_dir, monkeypatch
) -> None:
    """``MCC_DESKTOP_SKIP_AUTOSTART=1`` is visible in the document.

    A reader that sees ``start_at_login: true`` and ``autostart_reconcile:
    false`` knows the preference is stored but nobody is enforcing it, which
    is exactly the state a smoke run against a scratch config directory is
    supposed to be in.
    """

    _presence(monkeypatch, "healthy")

    monkeypatch.delenv(desktop_module.SKIP_AUTOSTART_ENV, raising=False)
    assert desktop_status()["autostart_reconcile"] is True

    monkeypatch.setenv(desktop_module.SKIP_AUTOSTART_ENV, "1")
    assert desktop_status()["autostart_reconcile"] is False

    # Anything but the exact value 1 keeps the normal behaviour: an empty or
    # mistyped variable must not silently disable a user's autostart.
    for value in ("", "0", "yes", "true"):
        monkeypatch.setenv(desktop_module.SKIP_AUTOSTART_ENV, value)
        assert desktop_status()["autostart_reconcile"] is True, value


def test_close_to_tray_is_resolved_for_the_window_that_reads_it(
    config_dir, monkeypatch
) -> None:
    """The window cannot work this out for itself, so Python answers it.

    ``tray_enabled`` in this document means "should YOU draw a tray icon", and
    it is ``false`` on Windows and macOS *because* the Python tray is already
    drawing one. A shell that computed its close behaviour as
    ``minimize_to_tray and tray_enabled`` therefore ended the app on exactly
    the two platforms where closing should have hidden the window -- taking the
    tray and the server with it, which is what was reported.
    """

    from my_claude_code.config import desktop as desktop_config

    _presence(monkeypatch, "healthy")
    monkeypatch.setenv(desktop_status_module.SHELL_TRAY_ENV, "0")

    payload = desktop_status()
    # The shell is told not to draw an icon...
    assert payload["tray_enabled"] is False
    # ...and is still told that closing hides, because a tray does exist.
    assert payload["close_to_tray"] is True
    assert "close_to_tray" in STATUS_KEYS

    # With no tray at all there is nowhere to close to, and the answer flips.
    desktop_config.save_desktop_state(
        desktop_config.DesktopState(tray_enabled=False, close_to_tray=True)
    )
    assert desktop_status()["close_to_tray"] is False

    # And an explicit opt-out is honoured even with a tray.
    desktop_config.save_desktop_state(
        desktop_config.DesktopState(tray_enabled=True, close_to_tray=False)
    )
    assert desktop_status()["close_to_tray"] is False


def test_a_starting_server_is_reported_as_starting_with_its_stage(
    config_dir, monkeypatch
) -> None:
    """Not free, not foreign, not draining.

    Before the listener moved in front of the work, the whole of a start read
    as ``free`` -- and ``free`` is exactly what a shell, a tray or a launcher
    reads as licence to start a server. The second one lost the bind race and
    died without a word.
    """

    _presence(monkeypatch, "starting")

    payload = desktop_status(presence_v2=True)

    assert payload["server_presence"] == "starting"
    assert payload["server_starting_stage"] == "configured-models"
    # A stranger on the port is a different page with a different instruction,
    # and MCC's own starting server must never land on it.
    assert payload["port_conflict"] is None


def test_the_starting_presence_is_only_reported_to_a_caller_that_asked(
    config_dir, monkeypatch
) -> None:
    """Same compatibility window ``draining`` has.

    A shell built before this value existed refuses a presence it has no branch
    for rather than guessing at the nearest neighbour, which is the right
    instinct -- so an old window must keep seeing the values it was written
    against.
    """

    _presence(monkeypatch, "starting")

    assert desktop_status()["server_presence"] == "free"
    assert desktop_status(presence_v2=True)["server_presence"] == "starting"


def test_every_other_presence_reports_no_starting_stage(
    config_dir, monkeypatch
) -> None:
    for presence in ("healthy", "draining", "free"):
        _presence(monkeypatch, presence)
        assert desktop_status(presence_v2=True)["server_starting_stage"] is None


def test_mcc_holding_the_port_in_silence_is_not_reported_as_foreign(
    config_dir, monkeypatch
) -> None:
    """The page the reporter actually saw, and why it was wrong.

    Their shell said "Port 8082 is held by another program, which is not the
    MCC server" while the only holder was MCC's own python.exe. Reproduced on a
    scratch server: while a freshly ready server builds its first provider
    generation it blocks its event loop for seconds at a time, the probe times
    out, and "held port, no answer" used to read as a stranger.
    """

    _presence(monkeypatch, "unreachable")
    monkeypatch.setattr(desktop_module, "port_is_held_by_mcc", lambda host, port: True)

    payload = desktop_status(presence_v2=True)

    assert payload["server_presence"] == "mcc-stale"
    assert payload["port_conflict"] is None
    # An old reader that did not ask for the new values keeps the old three.
    assert desktop_status()["server_presence"] == "foreign"


def test_a_genuine_stranger_on_the_port_is_still_foreign(
    config_dir, monkeypatch
) -> None:
    """The one case the port-conflict page is for, and it still reaches it."""

    _presence(monkeypatch, "unreachable")
    monkeypatch.setattr(desktop_module, "port_is_held_by_mcc", lambda host, port: False)

    payload = desktop_status(presence_v2=True)

    assert payload["server_presence"] == "foreign"
    assert payload["port_conflict"]
