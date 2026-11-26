"""Tests for OpenClawGatewayDriver Gateway WebSocket RPC."""

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.config.paths import reset_config_dir_cache
from my_claude_code.runtime.remote.openclaw_driver import (
    OpenClawGatewayDriver,
    build_openclaw_env,
    build_openclaw_gateway_argv,
)


def _write_fake_cli(directory: Path, name: str, script_body: str) -> str:
    py_path = directory / f"{name}.py"
    py_path.write_text(script_body, encoding="utf-8")
    if sys.platform == "win32":
        wrapper = directory / f"{name}.cmd"
        wrapper.write_text(f'@"{sys.executable}" -u "{py_path}" %*\n', encoding="utf-8")
        return str(wrapper)
    wrapper = directory / name
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -u "{py_path}" "$@"\n', encoding="utf-8"
    )
    wrapper.chmod(0o755)
    return str(wrapper)


def test_openclaw_argv_and_env() -> None:
    assert build_openclaw_gateway_argv(openclaw_bin="openclaw", port=18789) == [
        "openclaw",
        "gateway",
        "run",
        "--port",
        "18789",
    ]
    env = build_openclaw_env(
        auth_token="tok-claw",
        proxy_root_url="http://127.0.0.1:18568",
        session_id="rc_oclaw",
        base_env={},
    )
    assert env["MCC_OPENCLAW_API_KEY"] == "tok-claw"
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:18568/v1"
    assert env["MCC_RC_SESSION_ID"] == "rc_oclaw"


@pytest.mark.spawns_process
def test_openclaw_driver_ws_handshake_chat_approvals_and_abort(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    reset_config_dir_cache()

    fake_gateway_script = """
import asyncio
import json
import sys
from websockets.asyncio.server import serve

args = sys.argv[1:]
port = int(args[args.index("--port") + 1])

async def handler(ws):
    await ws.send(json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "n-123"}}))
    async for raw in ws:
        frame = json.loads(raw)
        if frame.get("type") != "req":
            continue
        rid = frame["id"]
        method = frame["method"]
        params = frame.get("params") or {}
        if method == "connect":
            if params.get("nonce") != "n-123":
                await ws.send(json.dumps({"type": "res", "id": rid, "ok": False, "error": "bad nonce"}))
                continue
            await ws.send(json.dumps({"type": "res", "id": rid, "ok": True, "payload": {"type": "hello-ok"}}))
        elif method == "chat.send":
            skey = params["sessionKey"]
            msg = params["message"]
            await ws.send(json.dumps({"type": "res", "id": rid, "ok": True, "payload": {"runId": "r1"}}))
            if msg == "hello":
                await ws.send(json.dumps({"type": "event", "event": "agent", "payload": {"sessionKey": skey, "stream": "thinking", "data": {"delta": "hmm"}}})),
                await ws.send(json.dumps({"type": "event", "event": "chat", "payload": {"sessionKey": skey, "state": "delta", "delta": "Claw hi"}}))
                await ws.send(json.dumps({"type": "event", "event": "chat", "payload": {"sessionKey": skey, "state": "final", "message": {"content": "Claw hi"}}}))
            elif msg == "need_exec":
                await ws.send(json.dumps({"type": "event", "event": "exec.approval.requested", "payload": {"id": "ex_1", "sessionKey": skey, "tool": "exec", "command": "ls"}}))
        elif method == "exec.approval.resolve":
            await ws.send(json.dumps({"type": "res", "id": rid, "ok": True, "payload": {"resolved": params.get("decision")}}))
            await ws.send(json.dumps({"type": "event", "event": "chat", "payload": {"state": "final", "message": {"content": "Resolved:" + str(params.get("decision"))}}}))
        elif method == "chat.abort":
            await ws.send(json.dumps({"type": "res", "id": rid, "ok": True, "payload": {"aborted": True}}))

async def main():
    async with serve(handler, "127.0.0.1", port):
        await asyncio.Future()

asyncio.run(main())
"""
    fake_bin = _write_fake_cli(tmp_path, "openclaw", fake_gateway_script)

    async def _run() -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        state: dict[str, Any] = {}

        async def _on_event(kind: str, payload: dict[str, Any]) -> None:
            events.append((kind, payload))

        async def _on_approval(
            approval_id: str, _tool: str, _tuid: str, _inp: dict[str, Any]
        ) -> dict[str, Any]:
            return {"id": approval_id}

        async def _on_state(**fields: Any) -> None:
            state.update(fields)

        driver = OpenClawGatewayDriver(
            session_id="rc_claw_1",
            cwd=str(tmp_path),
            proxy_root_url="http://127.0.0.1:18568",
            auth_token="tok",
            permission_mode="ask",
            openclaw_bin=fake_bin,
            on_event=_on_event,
            on_approval_create=_on_approval,
            on_state_update=_on_state,
        )
        await driver.start()
        try:
            assert state["server_url"].startswith("ws://127.0.0.1:")

            await driver.send_user_message("hello")
            for _ in range(60):
                if any(k == "turn.completed" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            assert any(
                k == "message.delta"
                and p.get("channel") == "thinking"
                and p.get("delta") == "hmm"
                for k, p in events
            )
            assert any(
                k == "message.assistant" and p.get("text") == "Claw hi"
                for k, p in events
            )

            # Exec approval in ask mode
            await driver.send_user_message("need_exec")
            for _ in range(60):
                if any(k == "approval.requested" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            apr = next(p for k, p in events if k == "approval.requested")
            await driver.resolve_approval(apr["approval_id"], decision="allow_once")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 2:
                    break
                await asyncio.sleep(0.05)
            assert any(
                k == "message.assistant" and "allow-once" in str(p.get("text"))
                for k, p in events
            )

            # In yolo mode, exec approval auto-resolves
            await driver.set_permission_mode("yolo")
            before_yolo = sum(1 for k, _ in events if k == "approval.requested")
            await driver.send_user_message("need_exec")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 3:
                    break
                await asyncio.sleep(0.05)
            assert sum(1 for k, _ in events if k == "approval.requested") == before_yolo
            assert any(
                k == "approval.auto_approved" and p.get("mode") == "yolo"
                for k, p in events
            )

            await driver.interrupt()
            assert any(k == "turn.aborted" for k, _ in events)
        finally:
            await driver.close()

    asyncio.run(_run())
