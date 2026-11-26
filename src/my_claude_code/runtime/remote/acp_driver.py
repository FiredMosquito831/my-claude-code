"""Tier 2 Universal Agent Client Protocol (ACP) JSON-RPC 2.0 driver."""

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

from my_claude_code.application.remote_ports import RemoteConflictError
from my_claude_code.config.harness_base_url import root_base_url
from my_claude_code.config.harnesses import (
    DROID_API_KEY_ENV,
    GOOSE_BASE_PATH_VALUE,
    GOOSE_PROVIDER_VALUE,
    HERMES_API_KEY_ENV,
    QWEN_API_KEY_ENV,
    harness_spec,
)
from my_claude_code.config.paths import harness_catalogue_path
from my_claude_code.config.proxy_auth import proxy_auth_token

from .drivers import (
    AUTO_EDIT_TOOL_NAMES,
    ApprovalCreateCallback,
    EventEmitCallback,
    StateUpdateCallback,
)
from .pty_bridge import win_exec_argv

ACP_HARNESSES = frozenset(
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

_HARNESS_ALIASES: dict[str, str] = {
    "gemini": "gemini_cli",
    "qwen": "qwen_code",
    "kimi": "kimi_code",
    "cline": "cline_cli",
}

_ACP_SUBCOMMAND_TABLE: dict[str, list[str]] = {
    "hermes": ["acp"],
    "gemini_cli": ["--acp"],
    "qwen_code": ["--acp"],
    "kimi_code": ["acp"],
    "cline_cli": ["--acp"],
    "goose": ["acp"],
    "droid": ["exec", "--output-format", "acp"],
}

_GEMINI_APPROVAL_MODES: dict[str, str] = {
    "ask": "default",
    "auto_edits": "auto_edit",
    "yolo": "yolo",
}

_ACP_EDIT_KINDS = frozenset({"edit", "write", "patch", "delete", "file_change"})
_ACP_NON_EDIT_KINDS = frozenset({"execute", "fetch", "bash", "command"})


def normalize_acp_harness(harness: str) -> str:
    """Normalize short harness aliases (e.g. ``gemini``) to HARNESS_SPECS IDs."""

    cleaned = harness.strip().lower()
    return _HARNESS_ALIASES.get(cleaned, cleaned)


def resolve_acp_binary(harness: str, explicit: str | None = None) -> str:
    """Resolve the executable path for an ACP-capable harness."""

    if explicit:
        return explicit
    canonical = normalize_acp_harness(harness)
    try:
        binary_name = harness_spec(canonical).binary
    except KeyError:
        binary_name = canonical
    return shutil.which(binary_name) or binary_name


def build_acp_argv(
    *,
    harness: str,
    binary: str,
    permission_mode: str = "ask",
    model: str = "",
    external_session_id: str | None = None,
    fork_session: bool = False,
) -> list[str]:
    """Build ACP startup argv for the given harness."""

    canonical = normalize_acp_harness(harness)
    sub_args = _ACP_SUBCOMMAND_TABLE.get(canonical)
    if sub_args is None:
        raise ValueError(f"Unsupported ACP harness: {harness!r}")
    argv = [binary, *sub_args]

    if canonical == "gemini_cli":
        mapped_mode = _GEMINI_APPROVAL_MODES.get(permission_mode, "default")
        argv.extend(["--approval-mode", mapped_mode])
        if external_session_id and external_session_id.strip() and not fork_session:
            argv.extend(["--resume", external_session_id.strip()])
        if model.strip():
            argv.extend(["--model", model.strip()])
    return argv


def build_acp_env(
    *,
    harness: str,
    session_id: str,
    proxy_root_url: str,
    auth_token: str,
    model: str = "",
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build isolated child environment pointing an ACP agent at MCC's proxy."""

    canonical = normalize_acp_harness(harness)
    env = dict(os.environ if base_env is None else base_env)
    clean_root = proxy_root_url.rstrip("/")
    v1_url = clean_root if clean_root.endswith("/v1") else f"{clean_root}/v1"
    token = proxy_auth_token(auth_token)

    env["ANTHROPIC_BASE_URL"] = clean_root
    env["ANTHROPIC_AUTH_TOKEN"] = token
    env["OPENAI_BASE_URL"] = v1_url
    env["OPENAI_API_KEY"] = token
    env["GEMINI_API_BASE_URL"] = clean_root
    env["GOOGLE_GEMINI_BASE_URL"] = root_base_url(clean_root)
    env["GEMINI_API_KEY"] = token
    env["MCC_RC_SESSION_ID"] = session_id
    env["PYTHONIOENCODING"] = "utf-8"

    if canonical == "gemini_cli":
        cfg_path = harness_catalogue_path("gemini-cli-settings.json")
        if cfg_path.is_file():
            env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = str(cfg_path)
    elif canonical == "qwen_code":
        env[QWEN_API_KEY_ENV] = token
        cfg_path = harness_catalogue_path("qwen-code-settings.json")
        if cfg_path.is_file():
            env["QWEN_CODE_SYSTEM_SETTINGS_PATH"] = str(cfg_path)
    elif canonical == "hermes":
        env[HERMES_API_KEY_ENV] = token
    elif canonical == "droid":
        env[DROID_API_KEY_ENV] = token
    elif canonical == "goose":
        env["OPENAI_HOST"] = root_base_url(clean_root)
        env["OPENAI_BASE_PATH"] = GOOSE_BASE_PATH_VALUE
        env["GOOSE_PROVIDER"] = GOOSE_PROVIDER_VALUE
        env["GOOSE_DISABLE_KEYRING"] = "1"
        if model.strip():
            env["GOOSE_MODEL"] = model.strip()

    return env


def _as_dict(value: Any) -> dict[str, Any]:
    return {str(k): v for k, v in value.items()} if isinstance(value, Mapping) else {}


def _extract_acp_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        text = content.get("text")
        if isinstance(text, str):
            return text
        nested = content.get("content")
        if nested is not None:
            return _extract_acp_text(nested)
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            extracted = _extract_acp_text(item)
            if extracted:
                parts.append(extracted)
        return "".join(parts)
    return ""


def _is_acp_edit_tool(kind: str, title: str) -> bool:
    low_kind = kind.strip().lower()
    if low_kind in _ACP_EDIT_KINDS:
        return True
    if low_kind in _ACP_NON_EDIT_KINDS:
        return False
    clean_title = title.strip()
    low_title = clean_title.lower()
    if clean_title in AUTO_EDIT_TOOL_NAMES or low_title in AUTO_EDIT_TOOL_NAMES:
        return True
    return low_title.startswith(("edit", "write", "patch", "multiedit", "apply_patch"))


def _pick_permission_option(
    options: list[dict[str, Any]],
    preferred_kinds: tuple[str, ...],
) -> str | None:
    for target_kind in preferred_kinds:
        for opt in options:
            if str(opt.get("kind") or "").strip().lower() == target_kind:
                opt_id = str(opt.get("optionId") or opt.get("id") or "").strip()
                if opt_id:
                    return opt_id
    for target_kind in preferred_kinds:
        for opt in options:
            opt_id = str(opt.get("optionId") or opt.get("id") or "").strip()
            name = str(opt.get("name") or "").strip().lower()
            if opt_id and (target_kind in opt_id.lower() or target_kind in name):
                return opt_id
    return None


@dataclass(slots=True)
class _PendingAcpPermission:
    approval_id: str
    rpc_id: Any
    tool_name: str
    options: list[dict[str, Any]]
    future: asyncio.Future[dict[str, Any]] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class AcpSessionDriver:
    """Universal Tier 2 ACP JSON-RPC 2.0 driver over NDJSON stdin/stdout."""

    driver_tier: str = "tier_2_acp"

    def __init__(
        self,
        *,
        harness: str,
        session_id: str,
        cwd: str,
        proxy_root_url: str,
        auth_token: str,
        permission_mode: str,
        model: str = "",
        external_session_id: str | None = None,
        fork_session: bool = False,
        binary_path: str | None = None,
        on_event: EventEmitCallback,
        on_approval_create: ApprovalCreateCallback,
        on_state_update: StateUpdateCallback,
    ) -> None:
        self.harness = normalize_acp_harness(harness)
        self.session_id = session_id
        self.cwd = cwd
        self.proxy_root_url = proxy_root_url
        self.auth_token = auth_token
        self.permission_mode = permission_mode
        self.model = model.strip()
        self.external_session_id = (external_session_id or "").strip()
        self.fork_session_on_start = fork_session
        self.binary_path = resolve_acp_binary(self.harness, binary_path)
        self._on_event = on_event
        self._on_approval_create = on_approval_create
        self._on_state_update = on_state_update

        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._active_prompt_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._next_rpc_id = 1
        self._rpc_waiters: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._pending: dict[str, _PendingAcpPermission] = {}
        self._always_allowed: set[str] = set()
        self._stderr_tail: list[str] = []
        self._supports_load_session: bool | None = None
        self._closed = False

    @property
    def pid(self) -> int | None:
        return (
            int(self._proc.pid) if self._proc is not None and self._proc.pid else None
        )

    async def start(self) -> None:
        argv = build_acp_argv(
            harness=self.harness,
            binary=self.binary_path,
            permission_mode=self.permission_mode,
            model=self.model,
            external_session_id=self.external_session_id or None,
            fork_session=self.fork_session_on_start,
        )
        env = build_acp_env(
            harness=self.harness,
            session_id=self.session_id,
            proxy_root_url=self.proxy_root_url,
            auth_token=self.auth_token,
            model=self.model,
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

        init_res = await self._rpc_call(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {
                    "name": "mcc-rc",
                    "title": "MCC Remote Control",
                    "version": "8.55.0",
                },
            },
        )
        caps = _as_dict(init_res.get("agentCapabilities"))
        if "loadSession" in caps:
            self._supports_load_session = bool(caps.get("loadSession"))

        sess_res: dict[str, Any] | None = None
        if self.external_session_id and not self.fork_session_on_start:
            try:
                sess_res = await self._rpc_call(
                    "session/load",
                    {
                        "sessionId": self.external_session_id,
                        "cwd": self.cwd,
                        "mcpServers": [],
                    },
                )
            except RuntimeError as exc:
                logger.debug(
                    f"ACP session/load fallback to session/new for {self.harness}: {exc}"
                )
                sess_res = None

        if sess_res is None:
            sess_res = await self._rpc_call(
                "session/new",
                {"cwd": self.cwd, "mcpServers": []},
            )

        ext_id = str(
            sess_res.get("sessionId")
            or sess_res.get("session_id")
            or self.external_session_id
            or ""
        ).strip()
        if ext_id:
            self.external_session_id = ext_id

        if self.permission_mode != "ask":
            await self._try_set_mode(self.permission_mode)
        if self.model:
            await self._try_set_model(self.model)

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

    async def _write_json(self, payload: Mapping[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None or self._closed:
            raise RuntimeError(f"ACP driver ({self.harness}) process is not running")
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()

    async def _rpc_call(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float | None = 20.0,
    ) -> dict[str, Any]:
        rpc_id = self._next_rpc_id
        self._next_rpc_id += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._rpc_waiters[rpc_id] = fut
        try:
            await self._write_json(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "method": method,
                    "params": dict(params),
                }
            )
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._rpc_waiters.pop(rpc_id, None)

    async def _try_set_mode(self, mode: str) -> None:
        if not self.external_session_id or self._closed:
            return
        mapped = {
            "ask": "default",
            "auto_edits": "auto_edit",
            "yolo": "yolo",
        }.get(mode, mode)
        with contextlib.suppress(Exception):
            await self._rpc_call(
                "session/set_mode",
                {"sessionId": self.external_session_id, "modeId": mapped},
                timeout=3.0,
            )

    async def _try_set_model(self, model: str) -> None:
        if not self.external_session_id or not model.strip() or self._closed:
            return
        with contextlib.suppress(Exception):
            await self._rpc_call(
                "session/set_model",
                {"sessionId": self.external_session_id, "modelId": model.strip()},
                timeout=3.0,
            )

    async def send_user_message(self, text: str) -> None:
        if not self.external_session_id or self._closed:
            raise RuntimeError(f"ACP session ({self.harness}) is not ready")
        await self._on_state_update(status="running")
        await self._on_event("message.user", {"text": text})
        await self._on_event("turn.started", {"prompt": text})
        self._active_prompt_task = asyncio.create_task(self._run_prompt_turn(text))

    async def _run_prompt_turn(self, text: str) -> None:
        try:
            res = await self._rpc_call(
                "session/prompt",
                {
                    "sessionId": self.external_session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=None,
            )
            if self._closed:
                return
            await self._on_state_update(status="idle")
            await self._on_event(
                "turn.completed",
                {
                    "stopReason": str(res.get("stopReason") or "end_turn"),
                    "is_error": False,
                    "usage": res.get("usage"),
                },
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._closed:
                return
            await self._on_state_update(status="idle", last_error=str(exc))
            await self._on_event("session.error", {"message": str(exc)})
            await self._on_event(
                "turn.completed",
                {"stopReason": "error", "is_error": True},
            )

    async def interrupt(self) -> None:
        if not self.external_session_id or self._closed:
            return
        with contextlib.suppress(Exception):
            await self._write_json(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": self.external_session_id},
                }
            )
        await self._on_state_update(status="idle")
        await self._on_event("turn.aborted", {"reason": "operator_interrupt"})

    async def set_permission_mode(self, mode: str) -> None:
        self.permission_mode = mode
        await self._try_set_mode(mode)
        await self._on_state_update(permission_mode=mode)
        await self._on_event("session.mode_changed", {"permission_mode": mode})

    async def set_model(self, model: str) -> None:
        self.model = model.strip()
        await self._try_set_model(self.model)
        await self._on_state_update(model=self.model)
        await self._on_event("session.model_changed", {"model": self.model})

    async def fork_session(self, *, title: str = "") -> dict[str, Any] | None:
        del title
        if self._supports_load_session is False:
            raise RemoteConflictError(
                f"ACP agent ({self.harness}) does not support session/load for forking"
            )
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
        pending = self._pending.get(approval_id)
        if pending is None or pending.future.done():
            raise KeyError(approval_id)

        if decision == "allow_always":
            self._always_allowed.add(pending.tool_name)
            opt_id = _pick_permission_option(
                pending.options, ("allow_always", "allow_once")
            )
            outcome: dict[str, Any] = {
                "outcome": "selected",
                "optionId": opt_id or "allow_always",
            }
        elif decision in {"allow", "allow_once"}:
            opt_id = _pick_permission_option(
                pending.options, ("allow_once", "allow_always")
            )
            outcome = {
                "outcome": "selected",
                "optionId": opt_id or "allow_once",
            }
        else:
            opt_id = _pick_permission_option(
                pending.options, ("reject_once", "reject_always")
            )
            if opt_id:
                outcome = {"outcome": "selected", "optionId": opt_id}
            else:
                outcome = {"outcome": "cancelled"}

        pending.future.set_result({"outcome": outcome})
        await self._on_state_update(status="running")
        await self._on_event(
            "approval.resolved",
            {
                "approval_id": approval_id,
                "decision": decision,
                "tool_name": pending.tool_name,
            },
        )

    async def _reply_rpc(self, rpc_id: Any, result: Mapping[str, Any]) -> None:
        await self._write_json(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": dict(result),
            }
        )

    async def _handle_request_permission(
        self, rpc_id: Any, params: dict[str, Any]
    ) -> None:
        tool_call = _as_dict(params.get("toolCall"))
        tool_use_id = str(tool_call.get("toolCallId") or "")
        kind = str(tool_call.get("kind") or "").strip().lower()
        title = str(
            tool_call.get("title") or tool_call.get("name") or kind or "tool"
        ).strip()
        raw_input = _as_dict(tool_call.get("rawInput"))
        options = [
            _as_dict(item)
            for item in (params.get("options") or [])
            if isinstance(item, Mapping)
        ]

        if self.permission_mode == "yolo":
            opt_id = (
                _pick_permission_option(options, ("allow_once", "allow_always"))
                or "allow_once"
            )
            await self._reply_rpc(
                rpc_id, {"outcome": {"outcome": "selected", "optionId": opt_id}}
            )
            await self._on_event(
                "approval.auto_approved",
                {
                    "mode": "yolo",
                    "tool_name": title,
                    "kind": kind,
                    "option_id": opt_id,
                },
            )
            return

        if self.permission_mode == "auto_edits" and (
            _is_acp_edit_tool(kind, title) or title in self._always_allowed
        ):
            opt_id = (
                _pick_permission_option(options, ("allow_once", "allow_always"))
                or "allow_once"
            )
            await self._reply_rpc(
                rpc_id, {"outcome": {"outcome": "selected", "optionId": opt_id}}
            )
            await self._on_event(
                "approval.auto_approved",
                {
                    "mode": "auto_edits",
                    "tool_name": title,
                    "kind": kind,
                    "option_id": opt_id,
                },
            )
            return

        if self.permission_mode == "ask" and title in self._always_allowed:
            opt_id = (
                _pick_permission_option(options, ("allow_always", "allow_once"))
                or "allow_always"
            )
            await self._reply_rpc(
                rpc_id, {"outcome": {"outcome": "selected", "optionId": opt_id}}
            )
            await self._on_event(
                "approval.auto_approved",
                {
                    "mode": "always_allowed",
                    "tool_name": title,
                    "kind": kind,
                    "option_id": opt_id,
                },
            )
            return

        approval_id = f"apr_{secrets.token_hex(8)}"
        payload_input = {
            **raw_input,
            "kind": kind,
            "title": title,
            "options": options,
        }
        await self._on_approval_create(approval_id, title, tool_use_id, payload_input)
        pending = _PendingAcpPermission(
            approval_id=approval_id,
            rpc_id=rpc_id,
            tool_name=title,
            options=options,
        )
        self._pending[approval_id] = pending
        await self._on_state_update(status="awaiting_approval")
        await self._on_event(
            "approval.requested",
            {
                "approval_id": approval_id,
                "request_id": str(rpc_id),
                "tool_name": title,
                "tool_use_id": tool_use_id,
                "kind": kind,
                "input": payload_input,
                "choices": ["allow_once", "allow_always", "deny"],
            },
        )
        asyncio.create_task(self._await_and_reply(pending))

    async def _await_and_reply(self, pending: _PendingAcpPermission) -> None:
        try:
            result = await pending.future
        except asyncio.CancelledError:
            return
        finally:
            self._pending.pop(pending.approval_id, None)
        if not self._closed:
            with contextlib.suppress(Exception):
                await self._reply_rpc(pending.rpc_id, result)

    async def _handle_session_update(self, params: dict[str, Any]) -> None:
        update = _as_dict(params.get("update")) or params
        utype = str(update.get("sessionUpdate") or "").strip()

        if utype == "agent_message_chunk":
            delta = _extract_acp_text(update.get("content"))
            if delta:
                await self._on_event(
                    "message.delta", {"channel": "text", "delta": delta}
                )
            return

        if utype == "agent_thought_chunk":
            delta = _extract_acp_text(update.get("content"))
            if delta:
                await self._on_event(
                    "message.delta", {"channel": "thinking", "delta": delta}
                )
            return

        if utype == "tool_call":
            tool_id = str(update.get("toolCallId") or "")
            kind = str(update.get("kind") or "")
            title = str(update.get("title") or kind or "tool")
            raw_input = _as_dict(update.get("rawInput"))
            await self._on_event(
                "tool.started",
                {
                    "tool_name": title,
                    "tool_use_id": tool_id,
                    "kind": kind,
                    "input": raw_input,
                },
            )
            return

        if utype == "tool_call_update":
            tool_id = str(update.get("toolCallId") or "")
            status = str(update.get("status") or "").strip().lower()
            text = _extract_acp_text(update.get("content"))
            raw_output = update.get("rawOutput")
            output_val: Any = (
                text
                if text
                else (raw_output if raw_output is not None else status or "")
            )
            if (
                status in {"completed", "failed", "error", "cancelled"}
                or raw_output is not None
            ):
                await self._on_event(
                    "tool.completed",
                    {
                        "tool_use_id": tool_id,
                        "status": status,
                        "is_error": status in {"failed", "error"},
                        "content": output_val,
                    },
                )
            elif text:
                await self._on_event(
                    "tool.output",
                    {"tool_use_id": tool_id, "delta": text},
                )
            return

        if utype in {"plan", "available_commands_update", "current_mode_update"}:
            state_payload: dict[str, Any] = {"update_type": utype}
            if utype == "current_mode_update":
                state_payload["current_mode_id"] = update.get("currentModeId")
            elif utype == "plan":
                state_payload["entries"] = update.get("entries", [])
            elif utype == "available_commands_update":
                state_payload["commands"] = update.get("availableCommands", [])
            await self._on_event("session.state", state_payload)
            return

        await self._on_event("driver.raw", update)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        rpc_id = msg.get("id")
        method = msg.get("method")

        if rpc_id is not None and method is None:
            waiter = self._rpc_waiters.get(rpc_id)
            if waiter is not None and not waiter.done():
                err = msg.get("error")
                if isinstance(err, Mapping):
                    waiter.set_exception(
                        RuntimeError(str(err.get("message") or "ACP RPC error"))
                    )
                else:
                    waiter.set_result(_as_dict(msg.get("result")))
            return

        if isinstance(method, str):
            params = _as_dict(msg.get("params"))
            if rpc_id is not None:
                if method == "session/request_permission":
                    await self._handle_request_permission(rpc_id, params)
                    return
                await self._write_json(
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {
                            "code": -32601,
                            "message": f"Unsupported ACP client method: {method}",
                        },
                    }
                )
                return

            if method == "session/update":
                await self._handle_session_update(params)
                return

            await self._on_event("driver.raw", msg)

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
                    await self._handle_message(parsed)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug(f"ACP stdout reader ended ({self.harness}): {exc}")
        finally:
            if not self._closed:
                rc = await self._proc.wait()
                for waiter in list(self._rpc_waiters.values()):
                    if not waiter.done():
                        waiter.set_exception(
                            RuntimeError(f"ACP process exited with code {rc}")
                        )
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
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()
        for waiter in list(self._rpc_waiters.values()):
            if not waiter.done():
                waiter.cancel()
        self._rpc_waiters.clear()
        if self._active_prompt_task is not None:
            self._active_prompt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._active_prompt_task
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
