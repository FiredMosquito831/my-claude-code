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
from my_claude_code.cli.port_diagnostics import PortOwner
from my_claude_code.config import paths
from my_claude_code.config.settings import Settings, get_settings

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
    "port_conflict": (str, type(None)),
    "server_mode": str,
    "window": str,
    "window_open": bool,
    "window_width": int,
    "window_height": int,
    "tray_enabled": bool,
    "minimize_to_tray": bool,
    "start_at_login": bool,
    "server_log": str,
    "start_timeout_seconds": float,
    "health_check_interval_seconds": float,
    "health_poll_seconds": float,
    "health_failure_threshold": int,
    "activation_poll_seconds": float,
    "reconnect_timeout_seconds": float,
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


def _presence(monkeypatch, presence: str) -> None:
    """Drive the real ``probe_server_presence`` ladder from its two primitives."""

    monkeypatch.setattr(
        desktop_module,
        "preflight_proxy",
        lambda url: None if presence == "healthy" else "unreachable",
    )
    monkeypatch.setattr(
        desktop_module,
        "probe_port_available",
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
