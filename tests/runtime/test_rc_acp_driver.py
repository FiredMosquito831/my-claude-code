"""Tests for Tier 2 Universal AcpSessionDriver JSON-RPC 2.0 over NDJSON."""

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.remote_ports import RemoteConflictError
from my_claude_code.config.paths import reset_config_dir_cache
from my_claude_code.runtime.remote.acp_driver import (
    ACP_HARNESSES,
    AcpSessionDriver,
    build_acp_argv,
    build_acp_env,
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


def test_acp_argv_all_seven_harnesses_and_env(monkeypatch, tmp_path: Path) -> None:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    reset_config_dir_cache()

    expected_harnesses = frozenset(
        {
            "hermes",
            "gemini_cli",
            "qwen_code",
            "kimi_code",
            "cline_cli",
            "goose",
            "droid",
        }
    )
    assert expected_harnesses == ACP_HARNESSES

    assert build_acp_argv(harness="hermes", binary="hermes") == ["hermes", "acp"]
    assert build_acp_argv(harness="qwen_code", binary="qwen") == ["qwen", "--acp"]
    assert build_acp_argv(harness="kimi_code", binary="kimi") == ["kimi", "acp"]
    assert build_acp_argv(harness="cline_cli", binary="cline") == ["cline", "--acp"]
    assert build_acp_argv(harness="goose", binary="goose") == ["goose", "acp"]
    assert build_acp_argv(harness="droid", binary="droid") == [
        "droid",
        "exec",
        "--output-format",
        "acp",
    ]
    gem_argv = build_acp_argv(
        harness="gemini_cli",
        binary="gemini",
        permission_mode="auto_edits",
        external_session_id="gem-99",
        model="gemini-2.5-pro",
    )
    assert gem_argv[:4] == ["gemini", "--acp", "--approval-mode", "auto_edit"]
    assert "--resume" in gem_argv
    assert "gem-99" in gem_argv
    assert "--model" in gem_argv

    gem_env = build_acp_env(
        harness="gemini_cli",
        session_id="rc_acp_1",
        proxy_root_url="http://127.0.0.1:18569",
        auth_token="tok-acp",
        base_env={},
    )
    assert gem_env["GEMINI_API_BASE_URL"] == "http://127.0.0.1:18569"
    assert gem_env["GOOGLE_GEMINI_BASE_URL"] == "http://127.0.0.1:18569"
    assert gem_env["GEMINI_API_KEY"] == "tok-acp"
    assert gem_env["MCC_RC_SESSION_ID"] == "rc_acp_1"

    goose_env = build_acp_env(
        harness="goose",
        session_id="rc_acp_2",
        proxy_root_url="http://127.0.0.1:18569",
        auth_token="tok-acp",
        model="gpt-5.4",
        base_env={},
    )
    assert goose_env["OPENAI_HOST"] == "http://127.0.0.1:18569"
    assert goose_env["GOOSE_DISABLE_KEYRING"] == "1"
    assert goose_env["GOOSE_MODEL"] == "gpt-5.4"

    droid_env = build_acp_env(
        harness="droid",
        session_id="rc_acp_3",
        proxy_root_url="http://127.0.0.1:18569",
        auth_token="tok-acp",
        base_env={},
    )
    assert droid_env["MCC_DROID_API_KEY"] == "tok-acp"


@pytest.mark.spawns_process
def test_acp_driver_handshake_streaming_permissions_and_cancel(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    reset_config_dir_cache()

    fake_acp_script = """
import json
import sys

next_srv_id = 500
pending_prompts = {}
pending_perms = {}

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
    rid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if rid is not None and method is None:
        prompt_rid = pending_perms.pop(rid, None)
        if prompt_rid is not None:
            outcome = (msg.get("result") or {}).get("outcome") or {}
            emit({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "acp_sess_1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "perm=" + str(outcome.get("optionId"))},
                    },
                },
            })
            emit({"jsonrpc": "2.0", "id": prompt_rid, "result": {"stopReason": "end_turn"}})
        continue

    if method == "initialize":
        emit({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True},
            },
        })
    elif method == "session/load":
        if params.get("sessionId") == "missing-fallback":
            emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
        else:
            emit({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": params.get("sessionId")}})
    elif method == "session/new":
        emit({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "acp_sess_1"}})
    elif method in ("session/set_mode", "session/set_model"):
        emit({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/cancel":
        continue
    elif method == "session/prompt":
        text = params["prompt"][0]["text"]
        if text == "stream":
            emit({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "acp_sess_1",
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "thinking step"},
                    },
                },
            })
            emit({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "acp_sess_1",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc_1",
                        "title": "Read README",
                        "kind": "read",
                        "rawInput": {"path": "README.md"},
                    },
                },
            })
            emit({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "acp_sess_1",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc_1",
                        "status": "completed",
                        "rawOutput": "ok",
                    },
                },
            })
            emit({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "acp_sess_1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "ACP hello"},
                    },
                },
            })
            emit({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        elif text in ("perm_edit", "perm_exec"):
            next_srv_id += 1
            pending_perms[next_srv_id] = rid
            is_edit = text == "perm_edit"
            emit({
                "jsonrpc": "2.0",
                "id": next_srv_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "acp_sess_1",
                    "toolCall": {
                        "toolCallId": "tc_" + text,
                        "title": "Edit app.py" if is_edit else "Run pytest",
                        "kind": "edit" if is_edit else "execute",
                        "rawInput": {"path": "app.py"} if is_edit else {"command": "pytest"},
                    },
                    "options": [
                        {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            })
"""
    fake_bin = _write_fake_cli(tmp_path, "gemini", fake_acp_script)

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

        driver = AcpSessionDriver(
            harness="gemini_cli",
            session_id="rc_gem_1",
            cwd=str(tmp_path),
            proxy_root_url="http://127.0.0.1:18569",
            auth_token="tok",
            permission_mode="ask",
            binary_path=fake_bin,
            on_event=_on_event,
            on_approval_create=_on_approval,
            on_state_update=_on_state,
        )
        await driver.start()
        try:
            assert state["external_session_id"] == "acp_sess_1"

            # 1. Streaming thoughts, tool_call, tool_call_update, text
            await driver.send_user_message("stream")
            for _ in range(60):
                if any(k == "turn.completed" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            assert any(
                k == "message.delta"
                and p.get("channel") == "thinking"
                and "thinking step" in p.get("delta", "")
                for k, p in events
            )
            assert any(
                k == "message.delta"
                and p.get("channel") == "text"
                and "ACP hello" in p.get("delta", "")
                for k, p in events
            )
            assert any(k == "tool.started" for k, _ in events)
            assert any(k == "tool.completed" for k, _ in events)

            # 2. ask mode -> edit prompts for approval
            await driver.send_user_message("perm_edit")
            for _ in range(60):
                if any(k == "approval.requested" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            edit_apr = next(p for k, p in events if k == "approval.requested")
            await driver.resolve_approval(
                edit_apr["approval_id"], decision="allow_once"
            )
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 2:
                    break
                await asyncio.sleep(0.05)

            # 3. auto_edits mode -> edit auto-approved, execute MUST STILL prompt!
            await driver.set_permission_mode("auto_edits")
            before_auto = sum(1 for k, _ in events if k == "approval.requested")
            await driver.send_user_message("perm_edit")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 3:
                    break
                await asyncio.sleep(0.05)
            assert sum(1 for k, _ in events if k == "approval.requested") == before_auto

            await driver.send_user_message("perm_exec")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "approval.requested") > before_auto:
                    break
                await asyncio.sleep(0.05)
            exec_apr = [p for k, p in events if k == "approval.requested"][-1]
            assert exec_apr["kind"] == "execute"
            await driver.resolve_approval(
                exec_apr["approval_id"], decision="allow_once"
            )
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 4:
                    break
                await asyncio.sleep(0.05)

            # 4. yolo mode -> execute auto-approved
            await driver.set_permission_mode("yolo")
            before_yolo = sum(1 for k, _ in events if k == "approval.requested")
            await driver.send_user_message("perm_exec")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 5:
                    break
                await asyncio.sleep(0.05)
            assert sum(1 for k, _ in events if k == "approval.requested") == before_yolo

            await driver.set_model("gemini-2.5-pro")
            await driver.interrupt()
            assert any(k == "turn.aborted" for k, _ in events)
        finally:
            await driver.close()

        # Verify session/load fallback to session/new when agent returns -32601
        fallback_driver = AcpSessionDriver(
            harness="hermes",
            session_id="rc_hermes_fb",
            cwd=str(tmp_path),
            proxy_root_url="http://127.0.0.1:18569",
            auth_token="tok",
            permission_mode="ask",
            external_session_id="missing-fallback",
            binary_path=fake_bin,
            on_event=_on_event,
            on_approval_create=_on_approval,
            on_state_update=_on_state,
        )
        await fallback_driver.start()
        try:
            assert fallback_driver.external_session_id == "acp_sess_1"
            fallback_driver._supports_load_session = False
            with pytest.raises(RemoteConflictError):
                await fallback_driver.fork_session()
        finally:
            await fallback_driver.close()

    asyncio.run(_run())
