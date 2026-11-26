"""Tier 2 turn-scoped structured driver for Command Code (`command-code -p --output-format json`)."""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

from loguru import logger

from my_claude_code.config.harnesses import COMMANDCODE_API_KEY_ENV, harness_spec
from my_claude_code.config.proxy_auth import proxy_auth_token

from .drivers import ApprovalCreateCallback, EventEmitCallback, StateUpdateCallback
from .pty_bridge import win_exec_argv

_COMMANDCODE_PERMISSION_MODES: dict[str, str] = {
    "ask": "plan",
    "auto_edits": "accept-edits",
    "yolo": "yolo",
}


def resolve_commandcode_binary(explicit: str | None = None) -> str:
    """Locate the Command Code executable without mutating frozen cli/."""

    if explicit:
        return explicit
    binary_name = harness_spec("commandcode_cli").binary
    return shutil.which(binary_name) or binary_name


def build_commandcode_turn_argv(
    *,
    commandcode_bin: str,
    prompt: str,
    permission_mode: str,
    model: str = "",
    external_session_id: str | None = None,
    fork_session: bool = False,
) -> list[str]:
    """Build turn-scoped ``command-code -p --output-format json`` argv.

    Command Code v1.88.2 has no ``acp`` subcommand; continuity across remote
    turns uses ``--resume <session_id>`` (and ``--fork-session`` when forking).
    """

    mapped_mode = _COMMANDCODE_PERMISSION_MODES.get(permission_mode, "plan")
    argv = [
        commandcode_bin,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-mode",
        mapped_mode,
    ]
    if external_session_id and external_session_id.strip():
        argv.extend(["--resume", external_session_id.strip()])
        if fork_session:
            argv.append("--fork-session")
    if model.strip():
        argv.extend(["--model", model.strip()])
    return argv


def build_commandcode_env(
    *,
    proxy_root_url: str,
    auth_token: str,
    session_id: str,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build non-destructive child environment for Command Code turns."""

    env = dict(os.environ if base_env is None else base_env)
    clean_root = proxy_root_url.rstrip("/")
    token = proxy_auth_token(auth_token)
    env[COMMANDCODE_API_KEY_ENV] = token
    env["ANTHROPIC_BASE_URL"] = clean_root
    env["ANTHROPIC_AUTH_TOKEN"] = token
    env["MCC_RC_SESSION_ID"] = session_id
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _as_dict(value: Any) -> dict[str, Any]:
    return {str(k): v for k, v in value.items()} if isinstance(value, Mapping) else {}


def _extract_text_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping) and item.get("type") == "text":
                txt = item.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
        return "".join(parts)
    return ""


class CommandCodeStructuredDriver:
    """Supervises turn-scoped ``command-code -p --output-format json`` runs."""

    driver_tier: str = "tier_2_commandcode"

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
        commandcode_bin: str | None = None,
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
        self.commandcode_bin = resolve_commandcode_binary(commandcode_bin)
        self._on_event = on_event
        self._on_approval_create = on_approval_create
        self._on_state_update = on_state_update

        self._proc: asyncio.subprocess.Process | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._turn_lock = asyncio.Lock()
        self._closed = False

    @property
    def pid(self) -> int | None:
        return (
            int(self._proc.pid) if self._proc is not None and self._proc.pid else None
        )

    async def start(self) -> None:
        await self._on_state_update(
            pid=None,
            status="idle",
            external_session_id=self.external_session_id,
        )
        await self._on_event(
            "session.state",
            {
                "status": "idle",
                "driver_tier": self.driver_tier,
                "permission_mode": self.permission_mode,
                "external_session_id": self.external_session_id,
            },
        )

    async def send_user_message(self, text: str) -> None:
        if self._closed:
            raise RuntimeError("CommandCode driver is closed")
        await self._on_state_update(status="running")
        await self._on_event("message.user", {"text": text})
        await self._on_event("turn.started", {"prompt": text})
        self._turn_task = asyncio.create_task(self._run_turn(text))

    async def _run_turn(self, prompt: str) -> None:
        async with self._turn_lock:
            if self._closed:
                return
            fork_now = self.fork_session_on_start
            self.fork_session_on_start = False
            argv = build_commandcode_turn_argv(
                commandcode_bin=self.commandcode_bin,
                prompt=prompt,
                permission_mode=self.permission_mode,
                model=self.model,
                external_session_id=self.external_session_id or None,
                fork_session=fork_now,
            )
            env = build_commandcode_env(
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
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            await self._on_state_update(pid=self.pid)

            saw_result = False
            saw_assistant = False
            saw_error = False
            stderr_lines: list[str] = []

            async def _drain_stderr() -> None:
                assert self._proc is not None and self._proc.stderr is not None
                while True:
                    raw = await self._proc.stderr.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        stderr_lines.append(line)

            stderr_task = asyncio.create_task(_drain_stderr())
            try:
                assert self._proc.stdout is not None
                while True:
                    raw = await self._proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        await self._on_event("driver.stdout_text", {"text": line})
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    ev_result, ev_assistant, ev_error = await self._handle_json_line(
                        parsed, saw_assistant=saw_assistant
                    )
                    saw_result = saw_result or ev_result
                    saw_assistant = saw_assistant or ev_assistant
                    saw_error = saw_error or ev_error
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug(f"CommandCode turn read error: {exc}")
                saw_error = True
            finally:
                with contextlib.suppress(Exception):
                    await stderr_task

            rc = await self._proc.wait()
            self._proc = None
            if self._closed:
                return
            if rc != 0:
                saw_error = True
                if stderr_lines and not saw_result:
                    await self._on_event(
                        "session.error",
                        {"message": "\n".join(stderr_lines[-5:])},
                    )
            if not saw_result:
                await self._on_event(
                    "turn.completed",
                    {"is_error": rc != 0, "returncode": rc},
                )
            if self.permission_mode == "ask" and saw_error:
                await self._on_event(
                    "session.state",
                    {
                        "status": "idle",
                        "recommend_pty": True,
                        "message": (
                            "Command Code headless mode could not complete in plan/ask "
                            "mode. Switch to Interactive TUI (PTY) to approve tools."
                        ),
                    },
                )
            await self._on_state_update(status="idle", pid=None)

    async def _handle_json_line(
        self, event: dict[str, Any], *, saw_assistant: bool
    ) -> tuple[bool, bool, bool]:
        ext_id = event.get("session_id") or event.get("sessionId")
        if (
            isinstance(ext_id, str)
            and ext_id.strip()
            and ext_id.strip() != self.external_session_id
        ):
            self.external_session_id = ext_id.strip()
            await self._on_state_update(external_session_id=self.external_session_id)

        etype = str(event.get("type") or "").strip()
        if etype == "assistant":
            msg = _as_dict(event.get("message"))
            text = _extract_text_blocks(msg.get("content"))
            if text:
                await self._on_event(
                    "message.assistant", {"channel": "text", "text": text}
                )
                return False, True, False
            return False, False, False

        if etype in {"text_delta", "thinking_delta"}:
            channel = "thinking" if etype == "thinking_delta" else "text"
            delta = str(event.get("delta") or event.get("text") or "")
            if delta:
                await self._on_event(
                    "message.delta", {"channel": channel, "delta": delta}
                )
            return False, False, False

        if etype in {"tool_use", "tool_call"}:
            await self._on_event(
                "tool.started",
                {
                    "tool_name": str(
                        event.get("name") or event.get("tool_name") or "tool"
                    ),
                    "tool_use_id": str(
                        event.get("id") or event.get("toolCallId") or ""
                    ),
                    "input": _as_dict(event.get("input") or event.get("rawInput")),
                },
            )
            return False, False, False

        if etype == "tool_result":
            await self._on_event(
                "tool.completed",
                {
                    "tool_use_id": str(event.get("tool_use_id") or ""),
                    "is_error": bool(event.get("is_error", False)),
                    "content": event.get("content", ""),
                },
            )
            return False, False, bool(event.get("is_error", False))

        if etype == "result" or (not etype and "result" in event):
            result_text = event.get("result", "")
            is_err = bool(event.get("is_error", False))
            emitted_assistant = False
            if not saw_assistant and isinstance(result_text, str) and result_text:
                await self._on_event(
                    "message.assistant", {"channel": "text", "text": result_text}
                )
                emitted_assistant = True
            await self._on_event(
                "turn.completed",
                {
                    "subtype": event.get("subtype", "success"),
                    "is_error": is_err,
                    "result": result_text,
                    "usage": event.get("usage"),
                    "total_cost_usd": event.get("total_cost_usd"),
                },
            )
            return True, emitted_assistant, is_err

        await self._on_event("driver.raw", event)
        return False, False, False

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        updated_input: Mapping[str, Any] | None = None,
        answers: Mapping[str, Any] | None = None,
        message: str = "",
    ) -> None:
        del decision, updated_input, answers, message
        raise KeyError(approval_id)

    async def set_permission_mode(self, mode: str) -> None:
        self.permission_mode = mode
        await self._on_state_update(permission_mode=mode)
        await self._on_event("session.mode_changed", {"permission_mode": mode})

    async def set_model(self, model: str) -> None:
        self.model = model.strip()
        await self._on_state_update(model=self.model)
        await self._on_event("session.model_changed", {"model": self.model})

    async def interrupt(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
        await self._on_state_update(status="idle", pid=None)
        await self._on_event("turn.aborted", {"reason": "operator_interrupt"})

    async def fork_session(self, *, title: str = "") -> dict[str, Any] | None:
        """Command Code forks via ``--resume <id> --fork-session`` on a new session."""
        del title
        return None

    async def close(self) -> None:
        self._closed = True
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()
                with contextlib.suppress(Exception):
                    await self._proc.wait()
        if self._turn_task is not None:
            self._turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn_task
