"""Tier 2 structured RPC driver for Pi Coding Agent (`pi --mode rpc`)."""

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from my_claude_code.cli.launchers.pi import (
    _MODEL_SCOPE as _PI_MODEL_SCOPE,
)
from my_claude_code.cli.launchers.pi import (
    build_pi_launcher_env,
    pi_extension_path,
)

from .drivers import (
    AUTO_EDIT_TOOL_NAMES,
    ApprovalCreateCallback,
    EventEmitCallback,
    StateUpdateCallback,
)
from .pty_bridge import win_exec_argv

_PI_PROVIDER_ID = _PI_MODEL_SCOPE.split("/", 1)[0]

_EDIT_UI_KEYWORDS = (
    "edit",
    "write",
    "patch",
    "modify file",
    "overwrite",
    "create file",
)


def resolve_pi_binary(explicit: str | None = None) -> str:
    """Locate the Pi executable without mutating frozen cli/."""

    if explicit:
        return explicit
    return shutil.which("pi") or "pi"


def build_pi_rpc_argv(
    *,
    pi_bin: str,
    model: str = "",
    thinking: str = "",
    external_session_id: str | None = None,
    fork_session: bool = False,
    extension_path: Path | None = None,
) -> list[str]:
    """Build ``pi --mode rpc`` argv with optional session resume/fork and extension."""

    argv = [pi_bin, "--mode", "rpc"]
    if extension_path is not None and extension_path.is_file():
        argv.extend(["-e", str(extension_path), "--models", _PI_MODEL_SCOPE])
    if model.strip():
        argv.extend(["--model", model.strip()])
    if thinking.strip():
        argv.extend(["--thinking", thinking.strip()])
    if external_session_id and external_session_id.strip():
        flag = "--fork" if fork_session else "--session"
        argv.extend([flag, external_session_id.strip()])
    return argv


def build_pi_env(
    *,
    proxy_root_url: str,
    auth_token: str,
    session_id: str,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build isolated child environment pointing Pi's extension at MCC's proxy."""

    source = os.environ if base_env is None else base_env
    env = build_pi_launcher_env(
        proxy_root_url=proxy_root_url,
        auth_token=auth_token,
        base_env=source,
    )
    env["MCC_RC_SESSION_ID"] = session_id
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _as_dict(value: Any) -> dict[str, Any]:
    return {str(k): v for k, v in value.items()} if isinstance(value, Mapping) else {}


def _is_edit_ui_request(title: str, message: str) -> bool:
    combined = f"{title} {message}".lower()
    if any(tok in combined for tok in ("bash", "shell", "command", "exec", "rm ")):
        return False
    if title.strip() in AUTO_EDIT_TOOL_NAMES:
        return True
    return any(kw in combined for kw in _EDIT_UI_KEYWORDS)


@dataclass(slots=True)
class _PendingPiUiRequest:
    approval_id: str
    rpc_id: str
    method: str
    title: str
    is_question: bool
    options: list[str] = field(default_factory=list)


class PiRpcDriver:
    """Supervises one ``pi --mode rpc`` JSONL process."""

    driver_tier: str = "tier_2_pi_rpc"

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
        pi_bin: str | None = None,
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
        self.external_session_id = (external_session_id or "").strip()
        self.fork_session_on_start = fork_session
        self.pi_bin = resolve_pi_binary(pi_bin)
        self._on_event = on_event
        self._on_approval_create = on_approval_create
        self._on_state_update = on_state_update

        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._next_cmd_id = 1
        self._pending: dict[str, _PendingPiUiRequest] = {}
        self._always_allowed: set[str] = set()
        self._stderr_tail: list[str] = []
        self._is_streaming = False
        self._closed = False

    @property
    def pid(self) -> int | None:
        return (
            int(self._proc.pid) if self._proc is not None and self._proc.pid else None
        )

    async def start(self) -> None:
        ext_path = pi_extension_path()
        argv = build_pi_rpc_argv(
            pi_bin=self.pi_bin,
            model=self.model,
            external_session_id=self.external_session_id or None,
            fork_session=self.fork_session_on_start,
            extension_path=ext_path if ext_path.is_file() else None,
        )
        env = build_pi_env(
            proxy_root_url=self.proxy_root_url,
            auth_token=self.auth_token,
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
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        await self._on_state_update(
            pid=self.pid,
            status="idle",
            external_session_id=self.external_session_id,
        )
        await self._on_event(
            "session.state",
            {
                "status": "idle",
                "pid": self.pid,
                "driver_tier": self.driver_tier,
                "permission_mode": self.permission_mode,
                "external_session_id": self.external_session_id,
            },
        )
        with contextlib.suppress(Exception):
            await self._send_command("get_state")

    def _alloc_id(self) -> str:
        cid = f"pi_{self._next_cmd_id}"
        self._next_cmd_id += 1
        return cid

    async def _write_json(self, payload: Mapping[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None or self._closed:
            raise RuntimeError("Pi RPC driver process is not running")
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()

    async def _send_command(self, cmd_type: str, **fields: Any) -> str:
        cid = self._alloc_id()
        payload: dict[str, Any] = {"type": cmd_type, "id": cid, **fields}
        await self._write_json(payload)
        return cid

    async def send_user_message(self, text: str) -> None:
        await self._on_state_update(status="running")
        await self._on_event("message.user", {"text": text})
        await self._on_event("turn.started", {"prompt": text})
        if self._is_streaming:
            await self._send_command(
                "prompt", message=text, streamingBehavior="followUp"
            )
        else:
            self._is_streaming = True
            await self._send_command("prompt", message=text)

    async def interrupt(self) -> None:
        await self._send_command("abort")
        self._is_streaming = False
        await self._on_state_update(status="idle")
        await self._on_event("turn.aborted", {"reason": "operator_interrupt"})

    async def set_permission_mode(self, mode: str) -> None:
        self.permission_mode = mode
        await self._on_state_update(permission_mode=mode)
        await self._on_event("session.mode_changed", {"permission_mode": mode})

    async def set_model(self, model: str) -> None:
        clean = model.strip()
        self.model = clean
        if "/" in clean:
            provider, model_id = clean.split("/", 1)
        else:
            provider, model_id = _PI_PROVIDER_ID, clean
        await self._send_command("set_model", provider=provider, modelId=model_id)
        await self._on_state_update(model=self.model)
        await self._on_event("session.model_changed", {"model": self.model})

    async def set_thinking_level(self, level: str) -> None:
        await self._send_command("set_thinking_level", level=level.strip())

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
        del updated_input
        pending = self._pending.pop(approval_id, None)
        if pending is None:
            raise KeyError(approval_id)

        allowed = decision in {"allow", "allow_once", "allow_always"}
        if pending.method == "confirm":
            if decision == "allow_always":
                self._always_allowed.add(pending.title)
            await self._write_json(
                {
                    "type": "extension_ui_response",
                    "id": pending.rpc_id,
                    "confirmed": allowed,
                }
            )
        else:
            if not allowed:
                await self._write_json(
                    {
                        "type": "extension_ui_response",
                        "id": pending.rpc_id,
                        "cancelled": True,
                    }
                )
            else:
                ans_val = message.strip()
                if not ans_val and answers:
                    first_val = next(iter(answers.values()), "")
                    ans_val = (
                        str(first_val[0])
                        if isinstance(first_val, list) and first_val
                        else str(first_val)
                    )
                if not ans_val and pending.options:
                    ans_val = pending.options[0]
                await self._write_json(
                    {
                        "type": "extension_ui_response",
                        "id": pending.rpc_id,
                        "value": ans_val,
                    }
                )

        await self._on_state_update(status="running")
        await self._on_event(
            "approval.resolved",
            {
                "approval_id": approval_id,
                "decision": decision,
                "tool_name": pending.title,
            },
        )

    async def _handle_extension_ui_request(self, event: dict[str, Any]) -> None:
        req_id = str(event.get("id") or "")
        method = str(event.get("method") or "").strip()
        title = str(event.get("title") or "").strip()
        msg = str(event.get("message") or "").strip()

        if method not in {"confirm", "select", "input", "editor"}:
            await self._on_event(
                "session.state",
                {
                    "update_type": f"extension_ui:{method}",
                    "title": title,
                    "message": msg,
                },
            )
            return

        if method == "confirm":
            label = title or msg or "Extension Confirm"
            if self.permission_mode == "yolo" or (
                label in self._always_allowed and self.permission_mode == "ask"
            ):
                await self._write_json(
                    {"type": "extension_ui_response", "id": req_id, "confirmed": True}
                )
                await self._on_event(
                    "approval.auto_approved",
                    {"mode": self.permission_mode, "tool_name": label},
                )
                return
            if self.permission_mode == "auto_edits" and (
                _is_edit_ui_request(title, msg) or label in self._always_allowed
            ):
                await self._write_json(
                    {"type": "extension_ui_response", "id": req_id, "confirmed": True}
                )
                await self._on_event(
                    "approval.auto_approved",
                    {"mode": "auto_edits", "tool_name": label},
                )
                return

            approval_id = f"apr_{secrets.token_hex(8)}"
            payload_input = {"title": title, "message": msg, "method": method}
            await self._on_approval_create(approval_id, label, req_id, payload_input)
            self._pending[approval_id] = _PendingPiUiRequest(
                approval_id=approval_id,
                rpc_id=req_id,
                method=method,
                title=label,
                is_question=False,
            )
            await self._on_state_update(status="awaiting_approval")
            await self._on_event(
                "approval.requested",
                {
                    "approval_id": approval_id,
                    "request_id": req_id,
                    "tool_name": label,
                    "tool_use_id": req_id,
                    "input": payload_input,
                    "choices": ["allow_once", "allow_always", "deny"],
                },
            )
            return

        # select / input / editor -> interactive question card
        raw_options = event.get("options")
        options = [str(o) for o in raw_options] if isinstance(raw_options, list) else []
        if self.permission_mode == "yolo" and method == "select" and options:
            await self._write_json(
                {"type": "extension_ui_response", "id": req_id, "value": options[0]}
            )
            await self._on_event(
                "approval.auto_approved",
                {"mode": "yolo", "tool_name": title or "select"},
            )
            return

        approval_id = f"apr_{secrets.token_hex(8)}"
        label = title or msg or f"Extension {method}"
        questions = [
            {
                "id": req_id,
                "question": label,
                "options": [{"label": opt} for opt in options],
            }
        ]
        await self._on_approval_create(
            approval_id,
            "AskUserQuestion",
            req_id,
            {"title": title, "message": msg, "options": options},
        )
        self._pending[approval_id] = _PendingPiUiRequest(
            approval_id=approval_id,
            rpc_id=req_id,
            method=method,
            title=label,
            is_question=True,
            options=options,
        )
        await self._on_state_update(status="awaiting_question")
        await self._on_event(
            "question.requested",
            {
                "approval_id": approval_id,
                "request_id": req_id,
                "questions": questions,
            },
        )

    async def _handle_event(self, event: dict[str, Any]) -> None:
        etype = str(event.get("type") or "").strip()

        if etype == "response":
            cmd = str(event.get("command") or "")
            if not bool(event.get("success", True)):
                await self._on_event(
                    "session.error",
                    {
                        "message": str(
                            event.get("error") or f"Pi command {cmd} failed"
                        ),
                        "command": cmd,
                    },
                )
                return
            data = _as_dict(event.get("data"))
            if cmd == "get_state" and data:
                sid = str(
                    data.get("sessionId") or data.get("sessionFile") or ""
                ).strip()
                updates: dict[str, Any] = {}
                if sid and sid != self.external_session_id:
                    self.external_session_id = sid
                    updates["external_session_id"] = sid
                model_obj = data.get("model")
                model_id = (
                    str(model_obj.get("id") or "")
                    if isinstance(model_obj, Mapping)
                    else str(model_obj or "")
                ).strip()
                if model_id and model_id != self.model:
                    self.model = model_id
                    updates["model"] = model_id
                if updates:
                    await self._on_state_update(**updates)
            return

        if etype == "agent_start":
            self._is_streaming = True
            await self._on_state_update(status="running")
            return

        if etype == "message_update":
            inner = _as_dict(event.get("assistantMessageEvent"))
            itype = str(inner.get("type") or "")
            if itype == "text_delta":
                delta = str(inner.get("delta") or "")
                if delta:
                    await self._on_event(
                        "message.delta", {"channel": "text", "delta": delta}
                    )
            elif itype == "thinking_delta":
                delta = str(inner.get("delta") or "")
                if delta:
                    await self._on_event(
                        "message.delta", {"channel": "thinking", "delta": delta}
                    )
            elif itype == "toolcall_end":
                tc = _as_dict(inner.get("toolCall"))
                if tc:
                    await self._on_event(
                        "tool.started",
                        {
                            "tool_name": str(tc.get("name") or "tool"),
                            "tool_use_id": str(tc.get("id") or ""),
                            "input": _as_dict(tc.get("arguments")),
                        },
                    )
            return

        if etype == "tool_execution_start":
            await self._on_event(
                "tool.started",
                {
                    "tool_name": str(event.get("toolName") or "tool"),
                    "tool_use_id": str(event.get("toolCallId") or ""),
                    "input": _as_dict(event.get("args")),
                },
            )
            return

        if etype == "tool_execution_update":
            partial = event.get("partialResult")
            if partial is not None:
                text = (
                    partial
                    if isinstance(partial, str)
                    else json.dumps(partial, ensure_ascii=False)
                )
                await self._on_event(
                    "tool.output",
                    {
                        "tool_use_id": str(event.get("toolCallId") or ""),
                        "delta": text,
                    },
                )
            return

        if etype == "tool_execution_end":
            await self._on_event(
                "tool.completed",
                {
                    "tool_use_id": str(event.get("toolCallId") or ""),
                    "tool_name": str(event.get("toolName") or ""),
                    "is_error": bool(event.get("isError", False)),
                    "content": event.get("result", ""),
                },
            )
            return

        if etype == "turn_end":
            await self._on_event(
                "turn.step",
                {
                    "tool_results_count": len(event.get("toolResults") or []),
                },
            )
            return

        if etype == "agent_end":
            self._is_streaming = False
            await self._on_state_update(status="idle")
            await self._on_event(
                "turn.completed",
                {"is_error": False, "messages": event.get("messages", [])},
            )
            return

        if etype == "extension_ui_request":
            await self._handle_extension_ui_request(event)
            return

        await self._on_event("driver.raw", event)

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                raw_line = await self._proc.stdout.readline()
                if not raw_line:
                    break
                text = raw_line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    await self._on_event("driver.stdout_text", {"text": text})
                    continue
                if isinstance(parsed, dict):
                    await self._handle_event(parsed)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug(f"Pi RPC stdout reader ended: {exc}")
        finally:
            if not self._closed:
                rc = await self._proc.wait()
                await self._on_state_update(status="exited", pid=None)
                await self._on_event("session.exited", {"returncode": rc})

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                raw_line = await self._proc.stderr.readline()
                if not raw_line:
                    break
                text = raw_line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    if len(self._stderr_tail) > 50:
                        self._stderr_tail.pop(0)
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def close(self) -> None:
        self._closed = True
        self._pending.clear()
        if self._proc is not None:
            if self._proc.stdin is not None:
                with contextlib.suppress(Exception):
                    self._proc.stdin.close()
            if self._proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3.0)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        self._proc.kill()
                    with contextlib.suppress(Exception):
                        await self._proc.wait()
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
