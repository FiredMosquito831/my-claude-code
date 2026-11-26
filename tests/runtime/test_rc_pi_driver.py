"""Tests for Tier 2 PiRpcDriver (`pi --mode rpc`) JSONL protocol."""

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.config.paths import reset_config_dir_cache
from my_claude_code.runtime.remote.pi_driver import (
    PiRpcDriver,
    build_pi_env,
    build_pi_rpc_argv,
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


def test_pi_argv_and_env() -> None:
    argv = build_pi_rpc_argv(
        pi_bin="pi",
        model="anthropic/claude-sonnet-4",
        thinking="high",
        external_session_id="pi_sess_9",
        fork_session=True,
    )
    assert argv[:3] == ["pi", "--mode", "rpc"]
    assert "--model" in argv
    assert "--thinking" in argv
    assert "--fork" in argv
    assert "pi_sess_9" in argv

    resume_argv = build_pi_rpc_argv(
        pi_bin="pi",
        external_session_id="pi_sess_9",
        fork_session=False,
    )
    assert "--session" in resume_argv
    assert "pi_sess_9" in resume_argv

    env = build_pi_env(
        proxy_root_url="http://127.0.0.1:18569",
        auth_token="tok-pi",
        session_id="rc_pi_1",
        base_env={"MCC_PI_API_KEY": "stale", "PATH": "/bin"},
    )
    assert env["MCC_PI_BASE_URL"] == "http://127.0.0.1:18569"
    assert env["MCC_PI_API_KEY"] == "tok-pi"
    assert env["MCC_RC_SESSION_ID"] == "rc_pi_1"


@pytest.mark.spawns_process
def test_pi_driver_streaming_extension_ui_confirm_model_and_abort(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    reset_config_dir_cache()

    fake_pi_script = """
import json
import sys

assert "--mode" in sys.argv and "rpc" in sys.argv

def emit(obj):
    print(json.dumps(obj), flush=True)

while True:
    raw = sys.stdin.readline()
    if not raw:
        break
    line = raw.strip()
    if not line:
        continue
    msg = json.loads(line)
    mtype = msg.get("type")
    rid = msg.get("id")

    if mtype == "get_state":
        emit({
            "type": "response",
            "id": rid,
            "command": "get_state",
            "success": True,
            "data": {"sessionId": "pi_ext_42", "model": {"id": "sonnet-4"}},
        })
    elif mtype == "set_model":
        emit({
            "type": "response",
            "id": rid,
            "command": "set_model",
            "success": True,
            "data": {"id": msg.get("modelId")},
        })
    elif mtype == "abort":
        emit({"type": "response", "id": rid, "command": "abort", "success": True})
    elif mtype == "prompt":
        text = msg.get("message", "")
        emit({"type": "agent_start"})
        if text == "stream":
            emit({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "pi thinking"}})
            emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Pi answer"}})
            emit({"type": "tool_execution_start", "toolCallId": "tc1", "toolName": "bash", "args": {"command": "ls"}})
            emit({"type": "tool_execution_end", "toolCallId": "tc1", "toolName": "bash", "result": "ok", "isError": False})
            emit({"type": "agent_end", "messages": []})
        elif text == "ui_confirm":
            emit({
                "type": "extension_ui_request",
                "id": "ui_req_1",
                "method": "confirm",
                "title": "Run shell command",
                "message": "Execute git status?",
            })
    elif mtype == "extension_ui_response":
        confirmed = bool(msg.get("confirmed", False))
        emit({
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": "ui_confirmed=" + str(confirmed),
            },
        })
        emit({"type": "agent_end", "messages": []})
"""
    fake_bin = _write_fake_cli(tmp_path, "pi", fake_pi_script)

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

        driver = PiRpcDriver(
            session_id="rc_pi_run",
            cwd=str(tmp_path),
            proxy_root_url="http://127.0.0.1:18569",
            auth_token="tok",
            permission_mode="ask",
            pi_bin=fake_bin,
            on_event=_on_event,
            on_approval_create=_on_approval,
            on_state_update=_on_state,
        )
        await driver.start()
        try:
            for _ in range(40):
                if state.get("external_session_id") == "pi_ext_42":
                    break
                await asyncio.sleep(0.05)
            assert state.get("external_session_id") == "pi_ext_42"

            # 1. Stream thinking + text + tool_execution_start/end
            await driver.send_user_message("stream")
            for _ in range(60):
                if any(k == "turn.completed" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            assert any(
                k == "message.delta"
                and p.get("channel") == "thinking"
                and "pi thinking" in p.get("delta", "")
                for k, p in events
            )
            assert any(
                k == "message.delta"
                and p.get("channel") == "text"
                and "Pi answer" in p.get("delta", "")
                for k, p in events
            )
            assert any(k == "tool.started" for k, _ in events)
            assert any(k == "tool.completed" for k, _ in events)

            # 2. extension_ui_request confirm in ask mode -> requires operator approval & extension_ui_response
            await driver.send_user_message("ui_confirm")
            for _ in range(60):
                if any(k == "approval.requested" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            ui_apr = next(p for k, p in events if k == "approval.requested")
            await driver.resolve_approval(ui_apr["approval_id"], decision="allow_once")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 2:
                    break
                await asyncio.sleep(0.05)
            assert any(
                k == "message.delta" and "ui_confirmed=True" in p.get("delta", "")
                for k, p in events
            )

            # 3. yolo mode -> auto-responds confirmed=True without approval.requested
            await driver.set_permission_mode("yolo")
            before_yolo = sum(1 for k, _ in events if k == "approval.requested")
            await driver.send_user_message("ui_confirm")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 3:
                    break
                await asyncio.sleep(0.05)
            assert sum(1 for k, _ in events if k == "approval.requested") == before_yolo

            await driver.set_model("free-claude-code/opus-4")
            assert state.get("model") == "free-claude-code/opus-4"
            await driver.interrupt()
            assert any(k == "turn.aborted" for k, _ in events)
        finally:
            await driver.close()

    asyncio.run(_run())
