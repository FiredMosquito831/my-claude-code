"""The two status keys 7.70.0 adds, and the pinned shell that must not notice them.

``server_stop_wait_seconds`` (top level) and ``identified`` (inside
``holder``) are the rescue spec's §2.7 contract surface. Contract C9's
two-release rule: the wheel emits them this release, the desktop app pinned
today (v7.26.0) only has to *tolerate* them, and nothing may require them until
the shell pin moves. These tests pin both halves: the keys are there and right,
and the document an old shell parses is otherwise exactly what it was.

The v7.26.0 shell's required fields are vendored below from
``git show v7.26.0:desktop-shell/src-tauri/src/status.rs`` (``struct Status``:
every field without ``#[serde(default)]`` that is not an ``Option``; ``struct
Holder``: ``kind``). Its ``#[derive(Deserialize)]`` carries no
``deny_unknown_fields``, so serde ignores a key it has no field for -- its own
``tolerates_unknown_keys`` test pins that on the Rust side.
"""

import pytest

from my_claude_code.cli import desktop as desktop_module
from my_claude_code.cli import desktop_status as desktop_status_module
from my_claude_code.cli.desktop import PortHolder, ServerState, classify_port_holder
from my_claude_code.cli.desktop_status import STATUS_KEYS, desktop_status
from my_claude_code.cli.launchers.common import PreflightResult
from my_claude_code.cli.port_diagnostics import PortOwner
from my_claude_code.cli.port_takeover import ProcessIdentity
from my_claude_code.config import paths
from my_claude_code.config.settings import Settings

#: ``struct Status`` in the shell pinned today (v7.26.0): field -> JSON type.
V7_26_0_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "schema": int,
    "version": str,
    "config_dir": str,
    "port": int,
    "admin_url": str,
    "health_url": str,
    "server_presence": str,
    "server_mode": str,
    "window_width": int,
    "window_height": int,
    "tray_enabled": bool,
    "minimize_to_tray": bool,
    "close_to_tray": bool,
    "server_log": str,
    "start_timeout_seconds": (int, float),
    "server_start_retries": int,
    "health_check_interval_seconds": (int, float),
    "health_poll_seconds": (int, float),
    "health_failure_threshold": int,
    "activation_poll_seconds": (int, float),
    "reconnect_timeout_seconds": (int, float),
    "reconnect_restatus_seconds": (int, float),
}

#: The keys this release adds. Nothing else may be new.
NEW_IN_7_70_0 = ("server_stop_wait_seconds",)


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    directory.mkdir()
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(directory))
    paths.reset_config_dir_cache()
    return directory


def _healthy(monkeypatch) -> None:
    monkeypatch.setattr(
        desktop_module, "preflight_result", lambda url: PreflightResult(status_code=200)
    )
    monkeypatch.setattr(
        desktop_module, "probe_port_available", lambda host, port: False
    )
    monkeypatch.setattr(
        desktop_module,
        "diagnose_port_owner",
        lambda *a, **k: PortOwner(pid=4242, name="python.exe", command=None),
    )


def test_server_stop_wait_seconds_is_the_servers_own_stop_budget(
    config_dir, monkeypatch
) -> None:
    _healthy(monkeypatch)

    payload = desktop_status()

    # 20 s graceful + 3 s teardown margin + 1 s watchdog beat.
    assert payload["server_stop_wait_seconds"] == 24.0
    assert isinstance(payload["server_stop_wait_seconds"], float)


def test_server_stop_wait_seconds_follows_the_configured_drain(
    config_dir, monkeypatch
) -> None:
    _healthy(monkeypatch)
    settings = Settings.model_validate({"SERVER_GRACEFUL_SHUTDOWN_SECONDS": "60"})
    monkeypatch.setattr(desktop_status_module, "get_settings", lambda: settings)

    assert desktop_status()["server_stop_wait_seconds"] == 64.0


def test_the_holder_says_whether_it_was_identified(config_dir, monkeypatch) -> None:
    _healthy(monkeypatch)

    holder = desktop_status()["holder"]

    assert holder == {
        "kind": "ours_healthy",
        "pid": 4242,
        "image": "python.exe",
        "identified": True,
    }


def test_a_holder_that_could_not_be_identified_is_still_foreign_but_says_so(
    monkeypatch,
) -> None:
    """The kind an old shell reads is unchanged; the new key tells the truth."""

    import my_claude_code.cli.port_takeover as takeover

    settings = Settings.model_construct(host="127.0.0.1", port=8099)
    monkeypatch.setattr(desktop_module, "probe_port_available", lambda *a, **k: False)
    monkeypatch.setattr(
        desktop_module,
        "diagnose_port_owner",
        lambda *a, **k: PortOwner(pid=31, name=None, command=None),
    )
    monkeypatch.setattr(takeover, "identity_for_owner", lambda *a, **k: None)

    holder = classify_port_holder(settings, ServerState("foreign"))

    assert holder.kind == "foreign"
    assert holder.identified is False
    assert holder.as_dict()["identified"] is False


def test_an_unreadable_process_is_unidentified_and_a_named_one_is_not(
    monkeypatch,
) -> None:
    import my_claude_code.cli.port_takeover as takeover

    settings = Settings.model_construct(host="127.0.0.1", port=8099)
    monkeypatch.setattr(desktop_module, "probe_port_available", lambda *a, **k: False)
    monkeypatch.setattr(
        desktop_module,
        "diagnose_port_owner",
        lambda *a, **k: PortOwner(pid=31, name=None, command=None),
    )
    monkeypatch.setattr(
        takeover,
        "identity_for_owner",
        lambda *a, **k: ProcessIdentity(pid=31, image=None, command=None),
    )
    assert classify_port_holder(settings, ServerState("foreign")).identified is False

    monkeypatch.setattr(
        takeover,
        "identity_for_owner",
        lambda *a, **k: ProcessIdentity(pid=31, image="nginx.exe", command=None),
    )
    named = classify_port_holder(settings, ServerState("foreign"))
    assert named.kind == "foreign"
    assert named.identified is True


def test_the_holder_kinds_an_old_shell_reads_are_unchanged() -> None:
    assert PortHolder("absent").as_dict()["kind"] == "absent"
    assert set(desktop_module.HOLDER_KINDS) == {
        "absent",
        "ours_healthy",
        "ours_starting",
        "ours_draining",
        "ours_stale",
        "foreign",
    }


def test_the_pinned_shell_still_finds_every_field_it_requires(
    config_dir, monkeypatch
) -> None:
    """The v7.26.0 ``struct Status``, field by field, against today's document."""

    _healthy(monkeypatch)

    payload = desktop_status()

    missing = [key for key in V7_26_0_REQUIRED if key not in payload]
    assert missing == []
    wrong = {
        key: type(payload[key]).__name__
        for key, expected in V7_26_0_REQUIRED.items()
        if not isinstance(payload[key], expected)
    }
    assert wrong == {}
    assert isinstance(payload["holder"]["kind"], str)


def test_the_document_without_the_new_keys_is_the_old_document(
    config_dir, monkeypatch
) -> None:
    """Strip what 7.70.0 added and nothing else has moved: purely additive."""

    _healthy(monkeypatch)

    payload = desktop_status()
    old = {key: value for key, value in payload.items() if key not in NEW_IN_7_70_0}
    old_holder = {k: v for k, v in old["holder"].items() if k != "identified"}

    assert tuple(old) == tuple(key for key in STATUS_KEYS if key not in NEW_IN_7_70_0)
    assert old_holder == {"kind": "ours_healthy", "pid": 4242, "image": "python.exe"}
    assert set(payload) - set(old) == set(NEW_IN_7_70_0)
