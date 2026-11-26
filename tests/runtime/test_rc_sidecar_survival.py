"""Tests for detached RC PTY sidecars, restart survival, auth, and security fences."""

import asyncio
import json
import os
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.app import create_app
from my_claude_code.api.ports import ApiServices
from my_claude_code.config.paths import rc_runtime_path
from my_claude_code.config.settings import Settings
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from my_claude_code.runtime.remote.hub import RemoteRuntimeHub
from my_claude_code.runtime.remote.sidecar import (
    SidecarServer,
    is_pid_alive,
    read_rc_runtime,
    upsert_rc_runtime_entry,
)


def _write_echo_script(directory: Path) -> Path:
    script = directory / "pty_echo.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('SIDECAR_BOOT\\n')\n"
        "sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    text = line.strip()\n"
        "    if text == 'QUIT':\n"
        "        sys.stdout.write('BYE\\n')\n"
        "        sys.stdout.flush()\n"
        "        break\n"
        "    sys.stdout.write('ECHO:' + text + '\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    return script


def _make_settings(
    allowed_dir: Path, *, rc_enabled: bool = True, max_sessions: int = 8
) -> Settings:
    return Settings.model_validate(
        {
            "RC_ENABLED": rc_enabled,
            "RC_ALLOWED_DIRS": str(allowed_dir),
            "RC_MAX_SESSIONS": max_sessions,
            "RC_SESSION_SECRET": "sidecar-test-secret-0123456789",
        }
    )


@contextmanager
def _test_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    rc_enabled: bool = True,
    max_sessions: int = 8,
) -> Iterator[tuple[TestClient, Path, RemoteRuntimeHub]]:
    cfg_dir = tmp_path / "mcc_cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    allowed = tmp_path / "allowed"
    allowed.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    settings = _make_settings(allowed, rc_enabled=rc_enabled, max_sessions=max_sessions)
    manager = ProviderRuntimeManager(settings)
    runtime = ApplicationRuntime(manager, transcriber=None)
    hub = RemoteRuntimeHub(settings=settings)
    app = create_app(
        ApiServices(
            requests=manager,
            admin=runtime,
            tasks=runtime,
            media=None,
            remote=hub,
        )
    )
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            yield client, allowed, hub
    finally:
        hub._store.close()


def test_rc_runtime_file_permissions_and_stale_pid_pruning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))

    upsert_rc_runtime_entry(
        {
            "session_id": "rc_stale",
            "harness": "claude",
            "cwd": str(tmp_path),
            "host": "127.0.0.1",
            "port": 49999,
            "secret": "stale-secret-0123456789",
            "sidecar_pid": 999999999,
            "pid": 999999998,
        }
    )
    upsert_rc_runtime_entry(
        {
            "session_id": "rc_live",
            "harness": "claude",
            "cwd": str(tmp_path),
            "host": "127.0.0.1",
            "port": 49998,
            "secret": "live-secret-0123456789",
            "sidecar_pid": os.getpid(),
            "pid": os.getpid(),
        }
    )

    runtime_file = rc_runtime_path()
    assert runtime_file.is_file()
    if os.name != "nt":
        assert (stat.S_IMODE(runtime_file.stat().st_mode)) == 0o600

    pruned = read_rc_runtime(prune_stale=True)
    assert "rc_stale" not in pruned
    assert "rc_live" in pruned


def test_sidecar_rejects_wrong_secret_over_tcp(tmp_path: Path) -> None:
    echo_script = _write_echo_script(tmp_path)

    async def _run() -> None:
        server = SidecarServer(
            session_id="rc_auth_check",
            harness="claude",
            cwd=str(tmp_path),
            title="auth-test",
            permission_mode="ask",
            argv=[sys.executable, str(echo_script)],
            secret="expected-secret-0123456789",
            register_runtime=False,
        )
        assert server._verify_auth(
            {"type": "auth", "secret": "expected-secret-0123456789"}
        )
        assert not server._verify_auth({"type": "auth", "secret": "wrong-secret"})
        assert not server._verify_auth({"type": "auth", "secret": ""})
        assert not server._verify_auth(
            {"type": "other", "secret": "expected-secret-0123456789"}
        )

        port = await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(b'{"type":"auth","secret":"wrong-secret"}\n')
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), timeout=3.0)
                payload = json.loads(line.decode("utf-8"))
                assert payload["type"] == "error"
                assert payload["error"] == "unauthorized"
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(_run())


def test_hub_restart_preserves_detached_sidecar_and_reconciles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "mcc_cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))

    allowed = tmp_path / "workspace"
    allowed.mkdir(parents=True, exist_ok=True)
    echo_script = _write_echo_script(allowed)

    monkeypatch.setattr(
        RemoteRuntimeHub,
        "_build_pty_argv",
        lambda self, *args, **kwargs: [sys.executable, str(echo_script)],
    )

    settings = _make_settings(allowed)
    sidecar_pid: int | None = None

    async def _drain_until(
        hub: RemoteRuntimeHub, session_id: str, needle: bytes, *, timeout: float = 8.0
    ) -> bytes:
        collected = bytearray()

        async def _consume() -> None:
            async for chunk in hub.subscribe_pty(session_id):
                collected.extend(chunk)
                if needle in collected:
                    return

        await asyncio.wait_for(_consume(), timeout=timeout)
        return bytes(collected)

    async def _run() -> None:
        nonlocal sidecar_pid
        hub1 = RemoteRuntimeHub(settings=settings)
        session = await hub1.create_session(
            harness="claude",
            cwd=str(allowed),
            title="surviving-pty",
            driver_mode="pty",
            detached=True,
        )
        session_id = str(session["id"])
        sidecar_pid = session.get("sidecar_pid")
        assert isinstance(sidecar_pid, int) and sidecar_pid > 0
        assert is_pid_alive(sidecar_pid)
        assert session_id in read_rc_runtime(prune_stale=True)

        await _drain_until(hub1, session_id, b"SIDECAR_BOOT")
        await hub1.pty_input(session_id, b"ALPHA\r\n")
        await _drain_until(hub1, session_id, b"ECHO:ALPHA")

        # Simulate mcc-server shutdown: detach cleanly without killing the sidecar.
        await hub1.close()
        assert is_pid_alive(sidecar_pid)
        assert session_id in read_rc_runtime(prune_stale=True)

        # Simulate new mcc-server startup reconciling rc_runtime.json.
        hub2 = RemoteRuntimeHub(settings=settings)
        try:
            reconciled = await hub2.reconcile_surviving_sidecars()
            assert session_id in [item["id"] for item in reconciled]
            sidecars = hub2.list_sidecars()
            assert any(item["session_id"] == session_id for item in sidecars)

            events = hub2.get_events(session_id)
            assert any(ev["kind"] == "session.reconnected" for ev in events)

            scroll = await _drain_until(hub2, session_id, b"ECHO:ALPHA")
            assert b"SIDECAR_BOOT" in scroll

            await hub2.pty_input(session_id, b"BETA\r\n")
            live_after = await _drain_until(hub2, session_id, b"ECHO:BETA")
            assert b"ECHO:BETA" in live_after

            stopped = await hub2.stop_session(session_id)
            assert stopped["status"] in {"stopped", "exited"}

            deadline = time.monotonic() + 5.0
            while is_pid_alive(sidecar_pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert not is_pid_alive(sidecar_pid)
            assert session_id not in read_rc_runtime(prune_stale=False)
        finally:
            await hub2.close()

    asyncio.run(_run())


def test_register_external_sidecar_enforces_allowed_dirs_enabled_and_admin_fence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload: dict[str, Any] = {
        "session_id": "rc_ext_fence",
        "harness": "claude",
        "cwd": str(tmp_path / "outside"),
        "host": "127.0.0.1",
        "port": 41234,
        "secret": "ext-fence-secret-012345",
        "origin": "cli_rc",
    }

    with _test_client(monkeypatch, tmp_path, rc_enabled=False) as (
        client_disabled,
        _allowed,
        _hub,
    ):
        resp_disabled = client_disabled.post(
            "/admin/api/rc/sidecars/register", json=payload
        )
        assert resp_disabled.status_code == 503

    with _test_client(monkeypatch, tmp_path, rc_enabled=True) as (
        client,
        allowed,
        _hub,
    ):
        # Forwarded header triggers admin loopback fence (403).
        resp_forwarded = client.get(
            "/admin/api/rc/sidecars",
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        assert resp_forwarded.status_code == 403

        resp_reg_forwarded = client.post(
            "/admin/api/rc/sidecars/register",
            json={**payload, "cwd": str(allowed)},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        assert resp_reg_forwarded.status_code == 403

        # Outside RC_ALLOWED_DIRS fails with actionable 403 error message.
        resp_outside = client.post("/admin/api/rc/sidecars/register", json=payload)
        assert resp_outside.status_code == 403
        assert "RC_ALLOWED_DIRS" in resp_outside.json()["detail"]

        # Non-loopback host rejected with 403.
        resp_host = client.post(
            "/admin/api/rc/sidecars/register",
            json={**payload, "cwd": str(allowed), "host": "192.168.1.50"},
        )
        assert resp_host.status_code == 403
