"""Tests for CodexAppServerDriver (`codex app-server --listen stdio://`) JSON-RPC 2.0."""

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.config.paths import reset_config_dir_cache
from my_claude_code.runtime.remote.codex_driver import (
    CodexAppServerDriver,
    build_codex_app_server_argv,
    build_codex_env,
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


def test_codex_argv_and_env(monkeypatch, tmp_path: Path) -> None:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    reset_config_dir_cache()

    argv = build_codex_app_server_argv(
        codex_bin="codex",
        proxy_root_url="http://127.0.0.1:18568",
        session_id="rc_cdx",
        model="gpt-5.4",
    )
    assert argv[:4] == ["codex", "app-server", "--listen", "stdio://"]
    joined = " ".join(argv)
    assert "http://127.0.0.1:18568/v1" in joined
    assert "MCC_CODEX_API_KEY" in joined
    assert "gpt-5.4" in joined

    env = build_codex_env(
        auth_token="tok-123",
        session_id="rc_cdx",
        base_env={"OPENAI_API_KEY": "stale", "PATH": "/bin"},
    )
    assert "OPENAI_API_KEY" not in env
    assert env["MCC_CODEX_API_KEY"] == "tok-123"
    assert env["MCC_RC_SESSION_ID"] == "rc_cdx"


@pytest.mark.spawns_process
def test_codex_driver_jsonrpc_approvals_modes_questions_and_interrupt(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    reset_config_dir_cache()

    fake_codex_script = """
import json
import sys

next_srv_id = 9000
pending = {}

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
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if rid is not None and method is None:
        # Response to server->client request
        kind = pending.pop(rid, "")
        res = msg.get("result") or {}
        emit({"method": "turn/completed", "params": {"turn": {"id": "turn_1", "status": "completed"}, "echo_result": res, "kind": kind}})
        continue

    if method == "initialize":
        emit({"id": rid, "result": {"userAgent": "fake-codex/0.172"}})
    elif method == "initialized":
        continue
    elif method == "thread/start":
        emit({"id": rid, "result": {"thread": {"id": "thr_1"}}})
    elif method == "thread/resume":
        emit({"id": rid, "result": {"thread": {"id": params.get("threadId")}}})
    elif method == "thread/fork":
        emit({"id": rid, "result": {"thread": {"id": "thr_fork_1"}}})
    elif method == "turn/interrupt":
        emit({"id": rid, "result": {}})
    elif method == "turn/start":
        emit({"id": rid, "result": {"turn": {"id": "turn_1"}}})
        txt = params["input"][0]["text"]
        emit({"method": "turn/started", "params": {"turn": {"id": "turn_1"}}})
        if txt == "stream":
            emit({"method": "item/reasoning/summaryTextDelta", "params": {"delta": "plan..."}})
            emit({"method": "item/agentMessage/delta", "params": {"delta": "Codex reply"}})
            emit({"method": "item/started", "params": {"item": {"id": "cmd1", "type": "commandExecution", "command": "ls"}}})
            emit({"method": "item/commandExecution/outputDelta", "params": {"itemId": "cmd1", "delta": "file.py\\n"}})
            emit({"method": "item/completed", "params": {"item": {"id": "cmd1", "type": "commandExecution", "status": "completed", "aggregatedOutput": "file.py"}}})
            emit({"method": "turn/completed", "params": {"turn": {"id": "turn_1", "status": "completed"}}})
        elif txt == "ask_file":
            next_srv_id += 1
            pending[next_srv_id] = "file"
            emit({"id": next_srv_id, "method": "item/fileChange/requestApproval", "params": {"itemId": "fc_1", "reason": "Edit a.py"}})
        elif txt == "ask_cmd":
            next_srv_id += 1
            pending[next_srv_id] = "cmd"
            emit({"id": next_srv_id, "method": "item/commandExecution/requestApproval", "params": {"itemId": "ce_1", "command": "pytest"}})
        elif txt == "ask_user":
            next_srv_id += 1
            pending[next_srv_id] = "user_input"
            emit({"id": next_srv_id, "method": "item/tool/requestUserInput", "params": {"itemId": "ui_1", "questions": [{"id": "q1", "question": "Pick target"}]}})
"""
    fake_bin = _write_fake_cli(tmp_path, "codex", fake_codex_script)

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

        driver = CodexAppServerDriver(
            session_id="rc_cdx_1",
            cwd=str(tmp_path),
            proxy_root_url="http://127.0.0.1:18568",
            auth_token="tok",
            permission_mode="ask",
            codex_bin=fake_bin,
            on_event=_on_event,
            on_approval_create=_on_approval,
            on_state_update=_on_state,
        )
        await driver.start()
        try:
            assert state["external_session_id"] == "thr_1"

            # 1. Streamed reasoning + text + tool
            await driver.send_user_message("stream")
            for _ in range(60):
                if any(k == "turn.completed" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            assert any(
                k == "message.delta"
                and p.get("channel") == "thinking"
                and "plan" in p.get("delta", "")
                for k, p in events
            )
            assert any(
                k == "message.delta"
                and p.get("channel") == "text"
                and "Codex reply" in p.get("delta", "")
                for k, p in events
            )
            assert any(k == "tool.output" for k, _ in events)

            # 2. ask mode -> fileChange prompts
            await driver.send_user_message("ask_file")
            for _ in range(60):
                if any(k == "approval.requested" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            fc_apr = next(p for k, p in events if k == "approval.requested")
            assert fc_apr["tool_name"] == "FileChange"
            await driver.resolve_approval(fc_apr["approval_id"], decision="allow_once")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 2:
                    break
                await asyncio.sleep(0.05)

            # 3. auto_edits mode -> fileChange auto-accepted, commandExecution MUST STILL prompt!
            await driver.set_permission_mode("auto_edits")
            before_auto = sum(1 for k, _ in events if k == "approval.requested")
            await driver.send_user_message("ask_file")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 3:
                    break
                await asyncio.sleep(0.05)
            assert sum(1 for k, _ in events if k == "approval.requested") == before_auto

            await driver.send_user_message("ask_cmd")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "approval.requested") > before_auto:
                    break
                await asyncio.sleep(0.05)
            cmd_apr = [p for k, p in events if k == "approval.requested"][-1]
            assert cmd_apr["tool_name"] == "Bash"
            await driver.resolve_approval(
                cmd_apr["approval_id"], decision="allow_always"
            )
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 4:
                    break
                await asyncio.sleep(0.05)

            # 4. yolo mode -> commandExecution auto-accepted
            await driver.set_permission_mode("yolo")
            before_yolo = sum(1 for k, _ in events if k == "approval.requested")
            await driver.send_user_message("ask_cmd")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 5:
                    break
                await asyncio.sleep(0.05)
            assert sum(1 for k, _ in events if k == "approval.requested") == before_yolo

            # 5. requestUserInput -> question.requested
            await driver.send_user_message("ask_user")
            for _ in range(60):
                if any(k == "question.requested" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            q_evt = next(p for k, p in events if k == "question.requested")
            await driver.resolve_approval(
                q_evt["approval_id"], decision="allow_once", answers={"q1": "prod"}
            )
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 6:
                    break
                await asyncio.sleep(0.05)

            forked = await driver.fork_session()
            assert forked is not None and forked["thread"]["id"] == "thr_fork_1"
            await driver.interrupt()
            assert any(k == "turn.aborted" for k, _ in events)
        finally:
            await driver.close()

    asyncio.run(_run())
