"""Tier 1A structured driver for OpenClaw Gateway WebSocket RPC."""

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loguru import logger

from my_claude_code.config.harnesses import OPENCLAW_API_KEY_ENV
from my_claude_code.config.proxy_auth import proxy_auth_token

from .drivers import (
    AUTO_EDIT_TOOL_NAMES,
    ApprovalCreateCallback,
    EventEmitCallback,
    StateUpdateCallback,
)
from .pty_bridge import win_exec_argv


def resolve_openclaw_binary(explicit: str | None = None) -> str:
    """Resolve the OpenClaw CLI executable path."""

    if explicit:
        return explicit
    return shutil.which("openclaw") or "openclaw"


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_openclaw_gateway_argv(*, openclaw_bin: str, port: int) -> list[str]:
    """Build ``openclaw gateway run --port <port>`` argv."""

    return [openclaw_bin, "gateway", "run", "--port", str(port)]


def build_openclaw_env(
    *,
    auth_token: str,
    proxy_root_url: str,
    session_id: str,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build child env setting MCC's OpenClaw API key and proxy URLs."""

    env = dict(os.environ if base_env is None else base_env)
    token = proxy_auth_token(auth_token)
    clean_root = proxy_root_url.rstrip("/")
    v1_url = clean_root if clean_root.endswith("/v1") else f"{clean_root}/v1"
    env[OPENCLAW_API_KEY_ENV] = token
    env["OPENAI_BASE_URL"] = v1_url
    env["OPENAI_API_KEY"] = token
    env["MCC_RC_SESSION_ID"] = session_id
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _is_edit_exec_request(tool_name: str, payload: dict[str, Any]) -> bool:
    low = tool_name.strip().lower()
    if low in AUTO_EDIT_TOOL_NAMES or tool_name in AUTO_EDIT_TOOL_NAMES:
        return True
    kind = str(payload.get("kind") or "").strip().lower()
    return kind in {"edit", "write", "patch", "file_change"}


@dataclass(slots=True)
class _PendingOpenClawApproval:
    approval_id: str
    exec_id: str
    tool_name: str


class OpenClawGatewayDriver:
    """Supervises an OpenClaw Gateway session over WebSocket RPC."""

    driver_tier: str = "tier_1a_openclaw"

    def __init__(
        self,
        *,
        session_id: str,
        cwd: str,
        proxy_root_url: str,
        auth_token: str,
        permission_mode: str,
        model: str = "",
        external_session_id: str | None = None,
        fork_session: bool = False,
        server_url: str | None = None,
        gateway_token: str = "",
        openclaw_bin: str | None = None,
        on_event: EventEmitCallback,
        on_approval_create: ApprovalCreateCallback,
        on_state_update: StateUpdateCallback,
    ) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.proxy_root_url = proxy_root_url
        self.auth_token = auth_token
        self.permission_mode = permission_mode
        self.model = model.strip()
        raw_ext = (external_session_id or "").strip()
        if raw_ext and fork_session:
            self.external_session_id = f"{raw_ext}-fork-{secrets.token_hex(3)}"
        else:
            self.external_session_id = raw_ext or f"mcc-{session_id}"
        self.server_url = (server_url or "").strip().rstrip("/")
        self._owns_process = not bool(self.server_url)
        self._gateway_token = gateway_token.strip()
        self.openclaw_bin = resolve_openclaw_binary(openclaw_bin)
        self._on_event = on_event
        self._on_approval_create = on_approval_create
        self._on_state_update = on_state_update

        self._proc: asyncio.subprocess.Process | None = None
        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._rpc_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending: dict[str, _PendingOpenClawApproval] = {}
        self._always_allowed: set[str] = set()
        self._stderr_tail: list[str] = []
        self._last_chat_text: str = ""
        self._closed = False

    @property
    def pid(self) -> int | None:
        return (
            int(self._proc.pid) if self._proc is not None and self._proc.pid else None
        )

    async def start(self) -> None:
        from websockets.asyncio.client import connect as ws_connect

        if not self.server_url:
            port = _allocate_loopback_port()
            self.server_url = f"ws://127.0.0.1:{port}"
            argv = build_openclaw_gateway_argv(
                openclaw_bin=self.openclaw_bin, port=port
            )
            env = build_openclaw_env(
                auth_token=self.auth_token,
                proxy_root_url=self.proxy_root_url,
                session_id=self.session_id,
            )
            creationflags = (
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
                if sys.platform == "win32"
                else 0
            )
            self._proc = await asyncio.create_subprocess_exec(
                *win_exec_argv(argv),
                cwd=self.cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            self._stderr_task = asyncio.create_task(self._read_stderr())

        self._ws = await self._connect_with_retry(ws_connect)
        nonce = await self._await_optional_challenge()
        self._reader_task = asyncio.create_task(self._read_ws())

        connect_params: dict[str, Any] = {
            "minProtocol": 3,
            "maxProtocol": 3,
            "client": {
                "id": "mcc-rc",
                "version": "8.54.0",
                "platform": sys.platform,
                "mode": "operator",
            },
            "role": "operator",
            "scopes": ["operator.read", "operator.write", "operator.approvals"],
        }
        if nonce:
            connect_params["nonce"] = nonce
        if self._gateway_token:
            connect_params["auth"] = {"token": self._gateway_token}

        await self._rpc_call("connect", connect_params)

        attach_hint = f"{self.openclaw_bin} tui --url {self.server_url}"
        await self._on_state_update(
            pid=self.pid,
            status="idle",
            external_session_id=self.external_session_id,
            server_url=self.server_url,
            attach_hint=attach_hint,
        )
        await self._on_event(
            "session.state",
            {
                "status": "idle",
                "pid": self.pid,
                "permission_mode": self.permission_mode,
                "server_url": self.server_url,
                "attach_hint": attach_hint,
                "external_session_id": self.external_session_id,
            },
        )

    async def _connect_with_retry(self, ws_connect: Any) -> Any:
        deadline = asyncio.get_running_loop().time() + 15.0
        last_err = ""
        while asyncio.get_running_loop().time() < deadline:
            if self._proc is not None and self._proc.returncode is not None:
                snippet = "\n".join(self._stderr_tail[-10:])
                raise RuntimeError(
                    f"openclaw gateway exited ({self._proc.returncode}): {snippet or 'no stderr'}"
                )
            try:
                return await ws_connect(self.server_url)
            except Exception as exc:
                last_err = str(exc)
                await asyncio.sleep(0.1)
        raise RuntimeError(
            f"OpenClaw gateway WS did not become ready at {self.server_url}: {last_err}"
        )

    async def _await_optional_challenge(self) -> str:
        assert self._ws is not None
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=0.6)
        except TimeoutError:
            return ""
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return ""
        if isinstance(frame, dict) and frame.get("type") == "event":
            if frame.get("event") == "connect.challenge":
                payload = frame.get("payload")
                if isinstance(payload, dict):
                    return str(payload.get("nonce") or "")
            await self._handle_ws_frame(frame)
        return ""

    async def _rpc_call(
        self, method: str, params: Mapping[str, Any], *, timeout: float = 15.0
    ) -> dict[str, Any]:
        if self._ws is None or self._closed:
            raise RuntimeError("OpenClaw gateway WebSocket is not connected")
        req_id = f"req_{secrets.token_hex(6)}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._rpc_waiters[req_id] = fut
        frame = json.dumps(
            {
                "type": "req",
                "id": req_id,
                "method": method,
                "params": dict(params),
            },
            ensure_ascii=False,
        )
        try:
            async with self._write_lock:
                await self._ws.send(frame)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._rpc_waiters.pop(req_id, None)

    async def send_user_message(self, text: str) -> None:
        self._last_chat_text = ""
        await self._on_state_update(status="running")
        await self._on_event("message.user", {"text": text})
        await self._on_event("turn.started", {"prompt": text})
        params: dict[str, Any] = {
            "sessionKey": self.external_session_id,
            "message": text,
            "idempotencyKey": f"idem_{secrets.token_hex(8)}",
        }
        if self.model:
            params["model"] = self.model
        await self._rpc_call("chat.send", params)

    async def interrupt(self) -> None:
        with contextlib.suppress(Exception):
            await self._rpc_call(
                "chat.abort",
                {"sessionKey": self.external_session_id},
                timeout=5.0,
            )
        await self._on_state_update(status="idle")
        await self._on_event("turn.aborted", {"reason": "operator_interrupt"})

    async def set_model(self, model: str) -> None:
        self.model = model.strip()
        await self._on_state_update(model=self.model)
        await self._on_event("session.model_changed", {"model": self.model})

    async def set_permission_mode(self, mode: str) -> None:
        self.permission_mode = mode
        await self._on_state_update(permission_mode=mode)
        await self._on_event("session.mode_changed", {"permission_mode": mode})

    async def fork_session(self, *, title: str = "") -> dict[str, Any] | None:
        del title
        return None

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        updated_input: Mapping[str, Any] | None = None,
        answers: Mapping[str, Any] | None = None,
        message: str = "",
    ) -> None:
        del updated_input, answers, message
        pending = self._pending.pop(approval_id, None)
        if pending is None:
            raise KeyError(approval_id)

        mapped = "deny"
        if decision == "allow_always":
            mapped = "allow-always"
            self._always_allowed.add(pending.tool_name)
        elif decision in {"allow", "allow_once"}:
            mapped = "allow-once"

        await self._rpc_call(
            "exec.approval.resolve",
            {"id": pending.exec_id, "decision": mapped},
        )
        await self._on_state_update(status="running")
        await self._on_event(
            "approval.resolved",
            {
                "approval_id": approval_id,
                "decision": decision,
                "tool_name": pending.tool_name,
            },
        )

    async def _read_ws(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if self._closed:
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(frame, dict):
                    await self._handle_ws_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                logger.debug("OpenClaw WS reader ended: {}", type(exc).__name__)
                await self._on_state_update(status="error", last_error=str(exc))
                await self._on_event("session.error", {"message": str(exc)})
        finally:
            if not self._closed:
                await self._on_state_update(status="exited", pid=None)
                await self._on_event("session.exited", {"returncode": 0})

    async def _handle_ws_frame(self, frame: dict[str, Any]) -> None:
        ftype = str(frame.get("type") or "")
        if ftype == "res":
            req_id = str(frame.get("id") or "")
            waiter = self._rpc_waiters.get(req_id)
            if waiter is not None and not waiter.done():
                if frame.get("ok") is False:
                    err = frame.get("error")
                    waiter.set_exception(RuntimeError(str(err or "RPC error")))
                else:
                    payload = frame.get("payload")
                    waiter.set_result(
                        dict(payload) if isinstance(payload, dict) else {}
                    )
            return

        if ftype != "event":
            return

        ev_name = str(frame.get("event") or "")
        raw_payload = frame.get("payload")
        payload: dict[str, Any] = (
            {str(k): v for k, v in raw_payload.items()}
            if isinstance(raw_payload, dict)
            else {}
        )
        sess_key = str(payload.get("sessionKey") or "")
        if sess_key and sess_key != self.external_session_id:
            return

        if ev_name == "chat":
            await self._handle_chat_event(payload)
        elif ev_name == "agent":
            await self._handle_agent_event(payload)
        elif ev_name in {"exec.approval.requested", "exec.approval.request"}:
            await self._handle_exec_approval(payload)

    async def _handle_chat_event(self, payload: dict[str, Any]) -> None:
        cstate = str(payload.get("state") or "")
        delta = payload.get("delta")
        if cstate == "delta":
            if isinstance(delta, str) and delta:
                self._last_chat_text += delta
                await self._on_event(
                    "message.delta",
                    {"channel": "text", "block_index": 0, "delta": delta},
                )
            else:
                text = _extract_chat_message_text(payload.get("message"))
                if text and len(text) > len(self._last_chat_text):
                    chunk = text[len(self._last_chat_text) :]
                    self._last_chat_text = text
                    await self._on_event(
                        "message.delta",
                        {"channel": "text", "block_index": 0, "delta": chunk},
                    )
        elif cstate == "final":
            text = _extract_chat_message_text(payload.get("message"))
            if text:
                await self._on_event(
                    "message.assistant", {"channel": "text", "text": text}
                )
            self._last_chat_text = ""
            await self._on_state_update(status="idle")
            await self._on_event(
                "turn.completed", {"subtype": "success", "is_error": False}
            )
        elif cstate == "aborted":
            self._last_chat_text = ""
            await self._on_state_update(status="idle")
            await self._on_event("turn.aborted", {"reason": "gateway_aborted"})
        elif cstate == "error":
            msg = str(payload.get("errorMessage") or payload.get("error") or "Error")
            await self._on_state_update(status="error", last_error=msg)
            await self._on_event("session.error", {"message": msg})

    async def _handle_agent_event(self, payload: dict[str, Any]) -> None:
        stream = str(payload.get("stream") or "")
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        if stream == "assistant":
            delta = str(data.get("delta") or "")
            if delta:
                await self._on_event(
                    "message.delta",
                    {"channel": "text", "block_index": 0, "delta": delta},
                )
        elif stream == "thinking":
            delta = str(data.get("delta") or "")
            if delta:
                await self._on_event(
                    "message.delta",
                    {"channel": "thinking", "block_index": 0, "delta": delta},
                )
        elif stream == "tool":
            phase = str(data.get("phase") or "")
            tool_id = str(data.get("toolCallId") or data.get("id") or "")
            tool_name = str(data.get("name") or "tool")
            if phase == "start":
                await self._on_event(
                    "tool.started",
                    {
                        "tool_use_id": tool_id,
                        "tool_name": tool_name,
                        "input": data.get("args", {}),
                    },
                )
            elif phase == "result":
                await self._on_event(
                    "tool.completed",
                    {
                        "tool_use_id": tool_id,
                        "is_error": bool(data.get("isError", False)),
                        "content": data.get("result", ""),
                    },
                )

    async def _handle_exec_approval(self, payload: dict[str, Any]) -> None:
        exec_id = str(
            payload.get("id")
            or payload.get("approvalId")
            or payload.get("requestId")
            or ""
        )
        if not exec_id:
            return
        tool_name = str(
            payload.get("tool")
            or payload.get("command")
            or payload.get("kind")
            or "exec"
        )
        if self.permission_mode == "yolo":
            asyncio.create_task(
                self._auto_resolve_exec(exec_id, tool_name, mode="yolo")
            )
            return
        if self.permission_mode == "auto_edits" and (
            _is_edit_exec_request(tool_name, payload)
            or tool_name in self._always_allowed
        ):
            asyncio.create_task(
                self._auto_resolve_exec(exec_id, tool_name, mode="auto_edits")
            )
            return
        if self.permission_mode == "ask" and tool_name in self._always_allowed:
            asyncio.create_task(self._auto_resolve_exec(exec_id, tool_name, mode=""))
            return

        approval_id = f"apr_{secrets.token_hex(8)}"
        await self._on_approval_create(approval_id, tool_name, exec_id, payload)
        self._pending[approval_id] = _PendingOpenClawApproval(
            approval_id=approval_id,
            exec_id=exec_id,
            tool_name=tool_name,
        )
        await self._on_state_update(status="awaiting_approval")
        await self._on_event(
            "approval.requested",
            {
                "approval_id": approval_id,
                "request_id": exec_id,
                "tool_name": tool_name,
                "tool_use_id": exec_id,
                "input": payload,
                "choices": ["allow_once", "allow_always", "deny"],
            },
        )

    async def _auto_resolve_exec(
        self, exec_id: str, tool_name: str, *, mode: str
    ) -> None:
        with contextlib.suppress(Exception):
            await self._rpc_call(
                "exec.approval.resolve",
                {"id": exec_id, "decision": "allow-once"},
            )
            if mode:
                await self._on_event(
                    "approval.auto_approved",
                    {"mode": mode, "exec_id": exec_id, "tool_name": tool_name},
                )

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                raw = await self._proc.stderr.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    if len(self._stderr_tail) > 50:
                        self._stderr_tail.pop(0)
                    await self._on_event("driver.stderr", {"text": text})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for waiter in list(self._rpc_waiters.values()):
            if not waiter.done():
                waiter.cancel()
        self._rpc_waiters.clear()
        self._pending.clear()

        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        proc = self._proc
        if proc is not None and self._owns_process and proc.returncode is None:
            if sys.platform == "win32" and proc.pid:
                flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=4.0,
                        creationflags=flags,
                        check=False,
                    )
            with contextlib.suppress(OSError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()

        await self._on_state_update(status="exited", pid=None)


def _extract_chat_message_text(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        if isinstance(msg.get("text"), str):
            return msg["text"]
    return ""
