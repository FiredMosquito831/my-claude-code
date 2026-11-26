"""Tests for opt-in ``--rc`` flag handling across all coding-agent CLI launchers."""

import asyncio
import contextlib
import importlib
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.config.settings import Settings
from my_claude_code.runtime.remote.hub import RemoteRuntimeHub

from my_claude_code.cli.launchers import (  # isort: skip
    aider,
    claude,
    cline,
    codex,
    commandcode,
    common,
    crush,
    droid,
    gemini,
    goose,
    hermes,
    kimi,
    openclaw,
    opencode,
    pi,
    qwen,
    rc_common,
)


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cfg = tmp_path / "mcc_cfg"
    home = tmp_path / "home"
    appdata = tmp_path / "appdata"
    local_appdata = tmp_path / "local_appdata"
    for p in (cfg, home, appdata, local_appdata):
        p.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    return cfg


def test_strip_rc_flag_strictly_before_double_dash() -> None:
    assert common.strip_rc_flag([]) == (False, [])
    assert common.strip_rc_flag(["--rc"]) == (True, [])
    assert common.strip_rc_flag(["--model", "sonnet", "--rc", "-p", "hi"]) == (
        True,
        ["--model", "sonnet", "-p", "hi"],
    )
    assert common.strip_rc_flag(["--", "--rc"]) == (False, ["--", "--rc"])
    assert common.strip_rc_flag(["--rc", "--", "--rc", "arg"]) == (
        True,
        ["--", "--rc", "arg"],
    )
    assert common.strip_rc_flag(["--rc-foo", "--rc=true"]) == (
        False,
        ["--rc-foo", "--rc=true"],
    )


_LAUNCHER_CASES: list[tuple[Any, str, str]] = [
    (claude, "launch", "claude"),
    (claude, "launch_legacy", "claude"),
    (codex, "launch", "codex"),
    (opencode, "launch", "opencode"),
    (opencode, "launch_v2", "opencode2"),
    (opencode, "launch_kilo", "kilo"),
    (commandcode, "launch", "commandcode_cli"),
    (gemini, "launch", "gemini_cli"),
    (pi, "launch", "pi"),
    (qwen, "launch", "qwen_code"),
    (hermes, "launch", "hermes"),
    (kimi, "launch", "kimi_code"),
    (cline, "launch", "cline_cli"),
    (goose, "launch", "goose"),
    (aider, "launch", "aider"),
    (droid, "launch", "droid"),
    (crush, "launch", "crush"),
    (openclaw, "launch", "openclaw"),
]


@pytest.mark.parametrize(("module", "fn_name", "expected_harness"), _LAUNCHER_CASES)
def test_all_coding_agent_launchers_preserve_non_rc_and_strip_rc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    fn_name: str,
    expected_harness: str,
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    settings = Settings.model_validate({})

    captured: list[dict[str, Any]] = []

    def _fake_run(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(module, "run_client_process", _fake_run)
    monkeypatch.setattr(module, "preflight_proxy", lambda url: None)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "resolve_harness_binary", lambda spec: "/fake/bin")

    if module is goose:
        monkeypatch.setattr(
            goose, "resolve_model", lambda *_a, **_kw: ("claude-sonnet-4-5", 200000)
        )
    if module is pi:
        monkeypatch.setattr(pi, "pi_binary_is_compatible", lambda _p: True)

    launch_fn = getattr(module, fn_name)

    # 1. Without --rc: zero rc_* kwargs passed, child args untouched.
    launch_fn(["--some-flag"])
    assert len(captured) == 1
    non_rc_call = captured[0]
    assert "rc_enabled" not in non_rc_call
    assert "rc_harness_id" not in non_rc_call
    assert "--some-flag" in non_rc_call["command"]

    # 2. With --rc: stripped from child command and rc_* kwargs enabled.
    captured.clear()
    launch_fn(["--rc", "--some-flag"])
    assert len(captured) == 1
    rc_call = captured[0]
    assert rc_call["rc_enabled"] is True
    assert rc_call["rc_harness_id"] == expected_harness
    assert isinstance(rc_call["rc_proxy_root_url"], str)
    assert "--rc" not in rc_call["command"]
    assert "--some-flag" in rc_call["command"]

    # 3. With `-- --rc`: `--rc` passes straight through to child, rc_enabled not set.
    captured.clear()
    launch_fn(["--", "--rc"])
    assert len(captured) == 1
    double_dash_call = captured[0]
    assert "rc_enabled" not in double_dash_call
    assert double_dash_call["command"][-2:] == ["--", "--rc"]


def test_run_rc_dual_sync_process_registers_sidecar_and_bridges_live_io(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_dir = _isolate_home(monkeypatch, tmp_path)
    allowed = tmp_path / "workspace"
    allowed.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(allowed)

    echo_script = tmp_path / "dual_sync_echo.py"
    echo_script.write_text(
        "import sys\n"
        "sys.stdout.write('DUAL_BOOT\\n')\n"
        "sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    text = line.strip()\n"
        "    if text == 'QUIT':\n"
        "        sys.stdout.write('DUAL_BYE\\n')\n"
        "        sys.stdout.flush()\n"
        "        sys.exit(0)\n"
        "    sys.stdout.write('OUT:' + text + '\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )

    loop = asyncio.new_event_loop()
    hub = RemoteRuntimeHub(
        settings=Settings.model_validate(
            {
                "RC_ENABLED": True,
                "RC_ALLOWED_DIRS": str(allowed),
                "RC_SESSION_SECRET": "dual-sync-test-secret-01234",
            }
        )
    )
    loop_ready = threading.Event()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop_ready.set()
        loop.run_forever()

    loop_thread = threading.Thread(target=_run_loop, daemon=True)
    loop_thread.start()
    loop_ready.wait(timeout=5.0)

    registered_sessions: list[dict[str, Any]] = []
    registered_event = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any, **kwargs: Any) -> None:
            return

        def do_POST(self) -> None:
            if self.path != "/admin/api/rc/sidecars/register":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            fut = asyncio.run_coroutine_threadsafe(
                hub.register_external_sidecar(**body), loop
            )
            session = fut.result(timeout=10.0)
            registered_sessions.append(session)
            registered_event.set()
            payload = json.dumps(
                {
                    "session": session,
                    "rc_url": "http://127.0.0.1:8082/rc",
                    "local_rc_url": "http://127.0.0.1:8082/rc",
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()

    # Use real pipes for local stdin/stdout so _pump_local_terminal runs cleanly.
    in_r_fd, in_w_fd = os.pipe()
    out_r_fd, out_w_fd = os.pipe()
    fake_stdin = io.TextIOWrapper(os.fdopen(in_r_fd, "rb"))
    fake_stdout = io.TextIOWrapper(os.fdopen(out_w_fd, "wb"))
    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    collected_out = bytearray()

    def _drain_out() -> None:
        with os.fdopen(out_r_fd, "rb") as reader:
            while True:
                chunk = reader.read(4096)
                if not chunk:
                    break
                collected_out.extend(chunk)

    out_thread = threading.Thread(target=_drain_out, daemon=True)
    out_thread.start()

    def _remote_driver() -> None:
        try:
            assert registered_event.wait(timeout=10.0)
            sid = registered_sessions[0]["id"]
            deadline = time.monotonic() + 8.0
            while b"DUAL_BOOT" not in collected_out and time.monotonic() < deadline:
                time.sleep(0.05)
            os.write(in_w_fd, b"LOCAL_PING\r\n")
            deadline = time.monotonic() + 8.0
            while (
                b"OUT:LOCAL_PING" not in collected_out and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            asyncio.run_coroutine_threadsafe(
                hub.pty_input(sid, b"REMOTE_PING\r\n"), loop
            ).result(timeout=5.0)
            deadline = time.monotonic() + 8.0
            while (
                b"OUT:REMOTE_PING" not in collected_out and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            asyncio.run_coroutine_threadsafe(
                hub.pty_input(sid, b"QUIT\r\n"), loop
            ).result(timeout=5.0)
        finally:
            with contextlib.suppress(OSError):
                os.close(in_w_fd)

    driver_thread = threading.Thread(target=_remote_driver, daemon=True)
    driver_thread.start()

    try:
        assert (
            importlib.import_module("my_claude_code.cli.launchers.rc_common")
            is rc_common
        )
        with pytest.raises(SystemExit) as exc_info:
            rc_common.run_rc_dual_sync_process(
                command=[sys.executable, str(echo_script)],
                env={**os.environ, "MCC_CONFIG_DIR": str(cfg_dir)},
                binary_name="python",
                display_name="Python",
                install_hint="install python",
                harness_id="claude",
                cwd=str(allowed),
                proxy_root_url=f"http://127.0.0.1:{port}",
            )
        assert exc_info.value.code == 0
        driver_thread.join(timeout=5.0)
        fake_stdout.close()
        out_thread.join(timeout=5.0)
        assert len(registered_sessions) == 1
        sess = registered_sessions[0]
        assert sess["origin"] == "cli_rc"
        assert sess["attach_hint"] == "Live CLI · Dual-Sync"
        assert b"DUAL_BOOT" in collected_out
        assert b"OUT:LOCAL_PING" in collected_out
        assert b"OUT:REMOTE_PING" in collected_out
        assert "Live Remote Control session" in fake_stderr.getvalue()
    finally:
        server.shutdown()
        server.server_close()
        asyncio.run_coroutine_threadsafe(hub.close(), loop).result(timeout=5.0)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5.0)
        loop.close()
