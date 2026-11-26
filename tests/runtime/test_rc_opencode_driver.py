"""Tests for OpenCodeFamilyDriver (opencode, opencode2, kilo) HTTP+SSE protocol."""

import asyncio
import sys
from pathlib import Path
from typing import Any

from my_claude_code.config.paths import reset_config_dir_cache
from my_claude_code.runtime.remote.opencode_driver import (
    OpenCodeFamilyDriver,
    build_opencode_attach_hint,
    build_opencode_env,
    build_opencode_serve_argv,
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


def test_opencode_argv_attach_hints_and_env_isolation(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    reset_config_dir_cache()

    assert build_opencode_serve_argv(
        harness="opencode2", binary="opencode2", port=4096
    ) == [
        "opencode2",
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        "4096",
    ]
    assert build_opencode_serve_argv(
        harness="opencode", binary="opencode", port=4097, pure=True
    ) == ["opencode", "serve", "--hostname", "127.0.0.1", "--port", "4097", "--pure"]

    hint_v2 = build_opencode_attach_hint("opencode2", "http://127.0.0.1:4096", "s-1")
    assert "--server http://127.0.0.1:4096" in hint_v2
    assert "attach" not in hint_v2

    hint_v1 = build_opencode_attach_hint("opencode", "http://127.0.0.1:4096", "s-1")
    assert hint_v1 == "opencode attach http://127.0.0.1:4096 --session s-1"

    hint_kilo = build_opencode_attach_hint("kilo", "http://127.0.0.1:4096")
    assert hint_kilo == "kilo attach http://127.0.0.1:4096"

    env = build_opencode_env(
        harness="opencode2",
        session_id="rc_test1",
        proxy_root_url="http://127.0.0.1:18568",
        auth_token="secret-proxy-tok",
        base_env={},
    )
    cfg_file = Path(env["OPENCODE_CONFIG"])
    assert cfg_file.is_file()
    assert "secret-proxy-tok" not in cfg_file.read_text(encoding="utf-8")
    assert env["MCC_OPENCODE_API_KEY"] == "secret-proxy-tok"
    assert env["MCC_OPENCODE_BASE_URL"] == "http://127.0.0.1:18568/v1"


def test_opencode_driver_sse_permissions_modes_questions_abort_fork(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    reset_config_dir_cache()

    fake_server_script = """
import json
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
events_q: queue.Queue[dict] = queue.Queue()
recorded_actions: list[dict] = []

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        return

    def _send_json(self, code: int, payload: dict):
        if code == 204:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/global/health":
            self._send_json(200, {"healthy": True})
            return
        if path == "/session/oc_1":
            self._send_json(200, {"id": "oc_1"})
            return
        if path == "/actions":
            self._send_json(200, {"actions": recorded_actions})
            return
        if path == "/event":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    ev = events_q.get(timeout=10)
                    chunk = f"data: {json.dumps(ev)}\\n\\n".encode("utf-8")
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json()
        if path == "/session":
            self._send_json(200, {"id": "oc_1", "title": body.get("title", "test")})
            return
        if path == "/session/oc_1/fork":
            recorded_actions.append({"action": "fork", "body": body})
            self._send_json(200, {"id": "oc_fork_1"})
            return
        if path == "/session/oc_1/abort":
            recorded_actions.append({"action": "abort"})
            self._send_json(200, {"aborted": True})
            return
        if path == "/session/oc_1/prompt_async":
            txt = body["parts"][0]["text"]
            if txt == "stream_msg":
                events_q.put({"type": "message.part.delta", "properties": {"sessionID": "oc_1", "partID": "r1", "field": "reasoning", "delta": "think..."}})
                events_q.put({"type": "message.part.delta", "properties": {"sessionID": "oc_1", "partID": "t1", "field": "text", "delta": "Hello world"}})
                events_q.put({"type": "message.part.updated", "properties": {"sessionID": "oc_1", "part": {"id": "tool1", "callID": "c1", "type": "tool", "tool": "read", "state": {"status": "completed", "input": {"file": "a.py"}, "output": "ok"}}}})
                events_q.put({"type": "session.idle", "properties": {"sessionID": "oc_1"}})
            elif txt == "ask_edit":
                events_q.put({"type": "permission.asked", "properties": {"id": "perm_edit_1", "sessionID": "oc_1", "permission": "edit", "metadata": {"file": "a.py"}}})
            elif txt == "ask_bash":
                events_q.put({"type": "permission.asked", "properties": {"id": "perm_bash_1", "sessionID": "oc_1", "permission": "bash", "metadata": {"command": "echo hi"}}})
            elif txt == "ask_q":
                events_q.put({"type": "question.asked", "properties": {"id": "q_1", "sessionID": "oc_1", "questions": [{"question": "Choose", "options": [{"label": "A"}]}]}})
            self._send_json(204, {})
            return
        if path.startswith("/permission/") and path.endswith("/reply"):
            pid = path.split("/")[2]
            recorded_actions.append({"action": "perm_reply", "id": pid, "reply": body.get("reply")})
            events_q.put({"type": "session.idle", "properties": {"sessionID": "oc_1"}})
            self._send_json(200, {"ok": True})
            return
        if path.startswith("/question/") and path.endswith("/reply"):
            qid = path.split("/")[2]
            recorded_actions.append({"action": "q_reply", "id": qid, "answers": body.get("answers")})
            events_q.put({"type": "session.idle", "properties": {"sessionID": "oc_1"}})
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": "unknown"})

server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
server.serve_forever()
"""
    fake_bin = _write_fake_cli(tmp_path, "opencode2", fake_server_script)

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

        driver = OpenCodeFamilyDriver(
            harness="opencode2",
            session_id="rc_oc2",
            cwd=str(tmp_path),
            proxy_root_url="http://127.0.0.1:18568",
            auth_token="tok",
            permission_mode="ask",
            binary=fake_bin,
            on_event=_on_event,
            on_approval_create=_on_approval,
            on_state_update=_on_state,
        )
        await driver.start()
        try:
            assert state["external_session_id"] == "oc_1"
            assert "--server" in state["attach_hint"]
            assert "attach" not in state["attach_hint"]

            # Streaming text + thinking + tool + idle
            await driver.send_user_message("stream_msg")
            for _ in range(60):
                if any(k == "turn.completed" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            assert any(
                k == "message.delta"
                and p.get("channel") == "thinking"
                and "think" in p.get("delta", "")
                for k, p in events
            )
            assert any(
                k == "message.delta"
                and p.get("channel") == "text"
                and "Hello" in p.get("delta", "")
                for k, p in events
            )
            assert any(
                k == "tool.completed" and p.get("content") == "ok" for k, p in events
            )

            # In ask mode, edit permission prompts
            await driver.send_user_message("ask_edit")
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

            # In auto_edits mode, edit is auto-approved, bash still prompts
            before_req = sum(1 for k, _ in events if k == "approval.requested")
            await driver.set_permission_mode("auto_edits")
            await driver.send_user_message("ask_edit")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 3:
                    break
                await asyncio.sleep(0.05)
            assert sum(1 for k, _ in events if k == "approval.requested") == before_req
            assert any(k == "approval.auto_approved" for k, _ in events)

            await driver.send_user_message("ask_bash")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "approval.requested") > before_req:
                    break
                await asyncio.sleep(0.05)
            bash_apr = [p for k, p in events if k == "approval.requested"][-1]
            assert bash_apr["tool_name"] == "bash"
            await driver.resolve_approval(
                bash_apr["approval_id"], decision="allow_always"
            )
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 4:
                    break
                await asyncio.sleep(0.05)

            # In yolo mode, bash is auto-approved with audit event
            await driver.set_permission_mode("yolo")
            before_yolo = sum(1 for k, _ in events if k == "approval.requested")
            await driver.send_user_message("ask_bash")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 5:
                    break
                await asyncio.sleep(0.05)
            assert sum(1 for k, _ in events if k == "approval.requested") == before_yolo
            assert any(
                k == "approval.auto_approved" and p.get("mode") == "yolo"
                for k, p in events
            )

            # Question asked -> reply
            await driver.send_user_message("ask_q")
            for _ in range(60):
                if any(k == "question.requested" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            q_evt = next(p for k, p in events if k == "question.requested")
            await driver.resolve_approval(
                q_evt["approval_id"], decision="allow_once", answers={"Choose": "A"}
            )
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 6:
                    break
                await asyncio.sleep(0.05)

            forked = await driver.fork_session(title="forked")
            assert forked is not None and forked["id"] == "oc_fork_1"
            await driver.interrupt()
            assert any(k == "turn.aborted" for k, _ in events)
        finally:
            await driver.close()

    asyncio.run(_run())
