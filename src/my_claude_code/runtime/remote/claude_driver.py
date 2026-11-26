"""Tier 1B persistent bidirectional Claude Code stream-json driver."""

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
from typing import Any

from loguru import logger

from my_claude_code.cli.claude_env import CLAUDE_BINARY_NAME, build_claude_proxy_env

from .drivers import (
    AUTO_EDIT_TOOL_NAMES,
    ApprovalCreateCallback,
    EventEmitCallback,
    StateUpdateCallback,
)
from .pty_bridge import win_exec_argv

_PERMISSION_MODE_FLAGS: dict[str, list[str]] = {
    "ask": ["--permission-mode", "default"],
    "auto_edits": ["--permission-mode", "acceptEdits"],
    "yolo": [
        "--permission-mode",
        "bypassPermissions",
        "--allow-dangerously-skip-permissions",
    ],
}


def resolve_claude_binary(explicit: str | None = None) -> str:
    """Locate the Claude Code binary without mutating frozen cli/."""

    if explicit:
        return explicit
    return shutil.which(CLAUDE_BINARY_NAME) or CLAUDE_BINARY_NAME


def build_claude_stream_argv(
    *,
    claude_bin: str,
    permission_mode: str,
    model: str = "",
    external_session_id: str | None = None,
    fork_session: bool = False,
) -> list[str]:
    """Build persistent bidirectional stream-json argv for Claude Code."""

    argv = [claude_bin]
    if external_session_id:
        argv.extend(["--resume", external_session_id])
        if fork_session:
            argv.append("--fork-session")
    if model.strip():
        argv.extend(["--model", model.strip()])
    argv.extend(
        [
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-prompt-tool",
            "stdio",
        ]
    )
    argv.extend(
        _PERMISSION_MODE_FLAGS.get(permission_mode, ["--permission-mode", "default"])
    )
    return argv


@dataclass(slots=True)
class PendingClaudeRequest:
    """In-flight control_request awaiting operator resolution in /rc."""

    approval_id: str
    request_id: str
    tool_name: str
    tool_use_id: str
    tool_input: dict[str, Any]
    is_question: bool
    future: asyncio.Future[dict[str, Any]] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class ClaudeBidirectionalDriver:
    """Supervises one persistent ``claude -p --input-format stream-json`` process."""

    driver_tier: str = "tier_1b_claude"

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
        claude_bin: str | None = None,
        on_event: EventEmitCallback,
        on_approval_create: ApprovalCreateCallback,
        on_state_update: StateUpdateCallback,
    ) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.proxy_root_url = proxy_root_url
        self.auth_token = auth_token
        self.permission_mode = permission_mode
        self.model = model
        self.external_session_id = external_session_id or ""
        self.fork_session_on_start = fork_session
        self.claude_bin = resolve_claude_binary(claude_bin)
        self._on_event = on_event
        self._on_approval_create = on_approval_create
        self._on_state_update = on_state_update

        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, PendingClaudeRequest] = {}
        self._always_allowed_tools: set[str] = set()
        self._closed = False

    @property
    def pid(self) -> int | None:
        return (
            int(self._proc.pid) if self._proc is not None and self._proc.pid else None
        )

    async def start(self) -> None:
        argv = build_claude_stream_argv(
            claude_bin=self.claude_bin,
            permission_mode=self.permission_mode,
            model=self.model,
            external_session_id=self.external_session_id or None,
            fork_session=self.fork_session_on_start,
        )
        env = build_claude_proxy_env(
            proxy_root_url=self.proxy_root_url,
            auth_token=self.auth_token,
            base_env=os.environ,
        )
        env["PYTHONIOENCODING"] = "utf-8"
        existing_headers = env.get("ANTHROPIC_CUSTOM_HEADERS", "").strip()
        session_hdr = f"x-mcc-session: {self.session_id}"
        env["ANTHROPIC_CUSTOM_HEADERS"] = (
            f"{existing_headers}\n{session_hdr}" if existing_headers else session_hdr
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
        await self._on_state_update(pid=self.pid, status="idle")
        await self._on_event(
            "session.state",
            {
                "status": "idle",
                "pid": self.pid,
                "permission_mode": self.permission_mode,
            },
        )
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _write_ndjson(self, payload: Mapping[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None or self._closed:
            raise RuntimeError("Claude driver process is not running")
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()

    async def send_user_message(self, text: str) -> None:
        await self._on_state_update(status="running")
        await self._on_event("message.user", {"text": text})
        await self._on_event("turn.started", {"prompt": text})
        await self._write_ndjson(
            {
                "type": "user",
                "message": {"role": "user", "content": text},
            }
        )

    async def interrupt(self) -> None:
        await self._write_ndjson(
            {
                "type": "control_request",
                "request_id": f"ctrl_{secrets.token_hex(6)}",
                "request": {"subtype": "interrupt"},
            }
        )
        await self._on_event("turn.aborted", {"reason": "operator_interrupt"})

    async def fork_session(self, *, title: str = "") -> dict[str, Any] | None:
        """Claude Code forks via CLI flags on a new process supervised by the hub."""
        del title
        return None

    async def set_model(self, model: str) -> None:
        self.model = model.strip()
        await self._write_ndjson(
            {
                "type": "control_request",
                "request_id": f"ctrl_{secrets.token_hex(6)}",
                "request": {"subtype": "set_model", "model": self.model},
            }
        )
        await self._on_state_update(model=self.model)
        await self._on_event("session.model_changed", {"model": self.model})

    async def set_permission_mode(self, mode: str) -> None:
        self.permission_mode = mode
        claude_mode = {
            "ask": "default",
            "auto_edits": "acceptEdits",
            "yolo": "bypassPermissions",
        }.get(mode, "default")
        await self._write_ndjson(
            {
                "type": "control_request",
                "request_id": f"ctrl_{secrets.token_hex(6)}",
                "request": {"subtype": "set_permission_mode", "mode": claude_mode},
            }
        )
        await self._on_state_update(permission_mode=mode)
        await self._on_event("session.mode_changed", {"permission_mode": mode})

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        updated_input: Mapping[str, Any] | None = None,
        answers: Mapping[str, Any] | None = None,
        message: str = "",
    ) -> None:
        pending = self._pending.get(approval_id)
        if pending is None or pending.future.done():
            raise KeyError(approval_id)

        if decision in {"allow", "allow_once", "allow_always"}:
            if decision == "allow_always" and not pending.is_question:
                self._always_allowed_tools.add(pending.tool_name)
            merged_input = dict(updated_input or pending.tool_input)
            if answers is not None:
                merged_input["answers"] = dict(answers)
            body: dict[str, Any] = {
                "behavior": "allow",
                "updatedInput": merged_input,
            }
            if decision == "allow_always" and not pending.is_question:
                body["updatedPermissions"] = [
                    {"type": "toolAlwaysAllow", "tool": pending.tool_name}
                ]
        else:
            body = {
                "behavior": "deny",
                "message": message.strip() or "Denied by operator in Remote Control",
            }
        pending.future.set_result(body)
        await self._on_state_update(status="running")
        await self._on_event(
            "approval.resolved",
            {
                "approval_id": approval_id,
                "decision": decision,
                "tool_name": pending.tool_name,
            },
        )

    async def _send_control_response(
        self, request_id: str, body: dict[str, Any]
    ) -> None:
        await self._write_ndjson(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": body,
                },
            }
        )

    async def _handle_control_request(self, event: dict[str, Any]) -> None:
        request_id = str(event.get("request_id") or "")
        req = event.get("request")
        if not isinstance(req, dict):
            return
        subtype = str(req.get("subtype") or "")
        if subtype != "can_use_tool":
            return

        tool_name = str(req.get("tool_name") or "")
        tool_use_id = str(req.get("tool_use_id") or "")
        raw_input = req.get("input")
        tool_input: dict[str, Any] = (
            {str(k): v for k, v in raw_input.items()}
            if isinstance(raw_input, dict)
            else {}
        )
        is_question = tool_name == "AskUserQuestion"

        # Fast-path local auto-approvals according to current permission mode
        if not is_question:
            if self.permission_mode == "yolo":
                await self._send_control_response(
                    request_id, {"behavior": "allow", "updatedInput": tool_input}
                )
                return
            if self.permission_mode == "auto_edits" and (
                tool_name in AUTO_EDIT_TOOL_NAMES
                or tool_name in self._always_allowed_tools
            ):
                await self._send_control_response(
                    request_id, {"behavior": "allow", "updatedInput": tool_input}
                )
                return
            if (
                self.permission_mode == "ask"
                and tool_name in self._always_allowed_tools
            ):
                await self._send_control_response(
                    request_id, {"behavior": "allow", "updatedInput": tool_input}
                )
                return

        approval_id = f"apr_{secrets.token_hex(8)}"
        await self._on_approval_create(approval_id, tool_name, tool_use_id, tool_input)
        pending = PendingClaudeRequest(
            approval_id=approval_id,
            request_id=request_id,
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            tool_input=tool_input,
            is_question=is_question,
        )
        self._pending[approval_id] = pending

        if is_question:
            await self._on_state_update(status="awaiting_question")
            await self._on_event(
                "question.requested",
                {
                    "approval_id": approval_id,
                    "request_id": request_id,
                    "questions": tool_input.get("questions", []),
                },
            )
        else:
            await self._on_state_update(status="awaiting_approval")
            await self._on_event(
                "approval.requested",
                {
                    "approval_id": approval_id,
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                    "input": tool_input,
                    "choices": ["allow_once", "allow_always", "deny"],
                },
            )

        # Wait in background task so stdout reader keeps streaming while parked.
        asyncio.create_task(self._await_and_reply(pending))

    async def _await_and_reply(self, pending: PendingClaudeRequest) -> None:
        try:
            response_body = await pending.future
        except asyncio.CancelledError:
            return
        finally:
            self._pending.pop(pending.approval_id, None)
        if not self._closed:
            with contextlib.suppress(Exception):
                await self._send_control_response(pending.request_id, response_body)

    async def _dispatch_ndjson(self, event: dict[str, Any]) -> None:
        etype = str(event.get("type") or "")
        ext_id = event.get("session_id")
        if isinstance(ext_id, str) and ext_id and ext_id != self.external_session_id:
            self.external_session_id = ext_id
            await self._on_state_update(external_session_id=ext_id)

        if etype == "control_request":
            await self._handle_control_request(event)
            return

        if etype == "system":
            model = str(event.get("model") or "")
            if model and model != self.model:
                self.model = model
                await self._on_state_update(model=model)
            await self._on_event(
                "session.init",
                {
                    "external_session_id": self.external_session_id,
                    "model": self.model,
                    "subtype": event.get("subtype", ""),
                    "tools": event.get("tools", []),
                },
            )
            return

        if etype == "stream_event":
            inner = event.get("event")
            if isinstance(inner, dict):
                await self._handle_stream_event(inner)
            return

        if etype == "assistant":
            msg = event.get("message")
            if isinstance(msg, dict):
                await self._handle_assistant_message(msg)
            return

        if etype == "user":
            msg = event.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                        ):
                            await self._on_event(
                                "tool.completed",
                                {
                                    "tool_use_id": block.get("tool_use_id", ""),
                                    "is_error": bool(block.get("is_error", False)),
                                    "content": block.get("content", ""),
                                },
                            )
            return

        if etype == "result":
            await self._on_state_update(status="idle")
            await self._on_event(
                "turn.completed",
                {
                    "subtype": event.get("subtype", "success"),
                    "is_error": bool(event.get("is_error", False)),
                    "result": event.get("result", ""),
                    "total_cost_usd": event.get("total_cost_usd"),
                    "duration_ms": event.get("duration_ms"),
                    "usage": event.get("usage"),
                },
            )
            return

        await self._on_event("driver.raw", event)

    async def _handle_stream_event(self, inner: dict[str, Any]) -> None:
        kind = str(inner.get("type") or "")
        if kind == "content_block_delta":
            delta = inner.get("delta")
            if not isinstance(delta, dict):
                return
            dtype = str(delta.get("type") or "")
            idx = int(inner.get("index") or 0)
            if dtype == "text_delta":
                await self._on_event(
                    "message.delta",
                    {
                        "channel": "text",
                        "block_index": idx,
                        "delta": str(delta.get("text") or ""),
                    },
                )
            elif dtype == "thinking_delta":
                await self._on_event(
                    "message.delta",
                    {
                        "channel": "thinking",
                        "block_index": idx,
                        "delta": str(delta.get("thinking") or ""),
                    },
                )
            elif dtype == "input_json_delta":
                await self._on_event(
                    "message.delta",
                    {
                        "channel": "tool_use_json",
                        "block_index": idx,
                        "delta": str(delta.get("partial_json") or ""),
                    },
                )
        elif kind == "content_block_start":
            block = inner.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                await self._on_event(
                    "tool.started",
                    {
                        "tool_use_id": str(block.get("id") or ""),
                        "tool_name": str(block.get("name") or ""),
                        "input": block.get("input", {}),
                    },
                )

    async def _handle_assistant_message(self, msg: dict[str, Any]) -> None:
        content = msg.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype == "text":
                await self._on_event(
                    "message.assistant",
                    {"channel": "text", "text": str(block.get("text") or "")},
                )
            elif btype == "thinking":
                await self._on_event(
                    "message.assistant",
                    {"channel": "thinking", "text": str(block.get("thinking") or "")},
                )
            elif btype == "tool_use":
                await self._on_event(
                    "tool.started",
                    {
                        "tool_use_id": str(block.get("id") or ""),
                        "tool_name": str(block.get("name") or ""),
                        "input": block.get("input", {}),
                    },
                )

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    await self._on_event("driver.raw_line", {"line": line})
                    continue
                if isinstance(event, dict):
                    await self._dispatch_ndjson(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Claude driver stdout error: {}", type(exc).__name__)
            await self._on_state_update(status="error", last_error=str(exc))
            await self._on_event("session.error", {"message": str(exc)})
        finally:
            if not self._closed:
                rc = await self._proc.wait()
                await self._on_state_update(status="exited", pid=None)
                await self._on_event("session.exited", {"returncode": rc})

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                raw = await self._proc.stderr.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    await self._on_event("driver.stderr", {"text": text})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()

        proc = self._proc
        if proc is not None and proc.returncode is None:
            if proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            with contextlib.suppress(OSError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except TimeoutError:
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()

        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await self._on_state_update(status="exited", pid=None)
