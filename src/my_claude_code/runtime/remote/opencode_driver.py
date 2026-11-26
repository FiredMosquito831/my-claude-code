"""Tier 1A structured driver for OpenCode, OpenCode 2, and Kilo CLI."""

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.config.harnesses import (
    OPENCODE_API_KEY_ENV,
    OPENCODE_BASE_URL_ENV,
)
from my_claude_code.config.paths import config_dir_path, harness_catalogue_path
from my_claude_code.config.proxy_auth import proxy_auth_token

from .drivers import (
    AUTO_EDIT_TOOL_NAMES,
    ApprovalCreateCallback,
    EventEmitCallback,
    StateUpdateCallback,
)
from .pty_bridge import win_exec_argv

OPENCODE_FAMILY_HARNESSES = frozenset({"opencode", "opencode2", "kilo"})

_CATALOGUE_FILENAMES: dict[str, str] = {
    "opencode": "opencode-config.json",
    "opencode2": "opencode2-config.json",
    "kilo": "kilo-config.json",
}

_CONFIG_ENV_VARS: dict[str, str] = {
    "opencode": "OPENCODE_CONFIG",
    "opencode2": "OPENCODE_CONFIG",
    "kilo": "KILO_CONFIG",
}


def resolve_opencode_binary(harness: str, explicit: str | None = None) -> str:
    """Resolve the OpenCode-family executable path."""

    if explicit:
        return explicit
    default_name = harness if harness in OPENCODE_FAMILY_HARNESSES else "opencode"
    return shutil.which(default_name) or default_name


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_opencode_serve_argv(
    *,
    harness: str,
    binary: str,
    port: int,
    pure: bool = False,
) -> list[str]:
    """Build headless ``serve`` argv for OpenCode / OpenCode 2 / Kilo."""

    argv = [binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)]
    if pure and harness == "opencode":
        argv.append("--pure")
    return argv


def build_opencode_attach_hint(
    harness: str,
    server_url: str,
    external_session_id: str | None = None,
) -> str:
    """Return the exact CLI attach command for the desktop terminal.

    ``opencode2`` connects via ``--server <url>`` and has no ``attach``
    subcommand; ``opencode`` and ``kilo`` use ``attach <url>``.
    """

    clean_url = server_url.rstrip("/")
    if harness == "opencode2":
        cmd = f"opencode2 --server {clean_url}"
    elif harness == "kilo":
        cmd = f"kilo attach {clean_url}"
    else:
        cmd = f"opencode attach {clean_url}"
    if external_session_id:
        cmd = f"{cmd} --session {external_session_id}"
    return cmd


def _ensure_opencode_config_file(harness: str, session_id: str) -> Path:
    """Return an MCC-owned OpenCode config file path using ``{env:...}`` refs."""

    catalogue_name = _CATALOGUE_FILENAMES.get(harness)
    if catalogue_name:
        existing = harness_catalogue_path(catalogue_name)
        if existing.is_file():
            return existing

    tmp_dir = config_dir_path() / "rc_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fallback_path = tmp_dir / f"{harness}-{session_id}.json"
    doc = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "mcc": {
                "npm": "@ai-sdk/anthropic",
                "name": "My Claude Code",
                "options": {
                    "baseURL": f"{{env:{OPENCODE_BASE_URL_ENV}}}",
                    "apiKey": f"{{env:{OPENCODE_API_KEY_ENV}}}",
                },
            }
        },
    }
    fallback_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return fallback_path


def build_opencode_env(
    *,
    harness: str,
    session_id: str,
    proxy_root_url: str,
    auth_token: str,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build isolated child environment pointing OpenCode/Kilo at MCC proxy."""

    env = dict(os.environ if base_env is None else base_env)
    token = proxy_auth_token(auth_token)
    clean_root = proxy_root_url.rstrip("/")
    v1_url = clean_root if clean_root.endswith("/v1") else f"{clean_root}/v1"

    config_path = _ensure_opencode_config_file(harness, session_id)
    config_env_var = _CONFIG_ENV_VARS.get(harness, "OPENCODE_CONFIG")
    env[config_env_var] = str(config_path)
    env[OPENCODE_BASE_URL_ENV] = v1_url
    env[OPENCODE_API_KEY_ENV] = token
    env["ANTHROPIC_BASE_URL"] = clean_root
    env["OPENAI_BASE_URL"] = v1_url
    env["OPENAI_API_KEY"] = token
    if harness == "kilo":
        env["KILO_API_KEY"] = token
    else:
        env["OPENCODE_API_KEY"] = token
    env["MCC_RC_SESSION_ID"] = session_id
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _is_edit_permission(perm_type: str) -> bool:
    low = perm_type.strip().lower()
    if low in AUTO_EDIT_TOOL_NAMES or perm_type in AUTO_EDIT_TOOL_NAMES:
        return True
    return low.startswith(("edit", "write", "patch", "multiedit"))


@dataclass(slots=True)
class _PendingOpenCodePrompt:
    approval_id: str
    remote_id: str
    kind: str  # "permission" | "question"
    tool_name: str
    questions: list[dict[str, Any]] = field(default_factory=list)


class OpenCodeFamilyDriver:
    """Supervises OpenCode / OpenCode 2 / Kilo via headless ``serve`` + HTTP/SSE."""

    driver_tier: str = "tier_1a_opencode"

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
        title: str = "",
        external_session_id: str | None = None,
        fork_session: bool = False,
        server_url: str | None = None,
        binary: str | None = None,
        on_event: EventEmitCallback,
        on_approval_create: ApprovalCreateCallback,
        on_state_update: StateUpdateCallback,
    ) -> None:
        self.harness = harness if harness in OPENCODE_FAMILY_HARNESSES else "opencode"
        self.session_id = session_id
        self.cwd = cwd
        self.proxy_root_url = proxy_root_url
        self.auth_token = auth_token
        self.permission_mode = permission_mode
        self.model = model.strip()
        self.title = title.strip()
        self.external_session_id = (external_session_id or "").strip()
        self.fork_session_on_start = fork_session
        self.server_url = (server_url or "").strip().rstrip("/")
        self._owns_process = not bool(self.server_url)
        self.binary = resolve_opencode_binary(self.harness, binary)
        self._on_event = on_event
        self._on_approval_create = on_approval_create
        self._on_state_update = on_state_update

        self._proc: asyncio.subprocess.Process | None = None
        self._client: httpx.AsyncClient | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._sse_ready = asyncio.Event()
        self._stderr_tail: list[str] = []
        self._pending: dict[str, _PendingOpenCodePrompt] = {}
        self._always_allowed: set[str] = set()
        self._part_lengths: dict[str, int] = {}
        self._message_roles: dict[str, str] = {}
        self._emitted_tools: set[str] = set()
        self._completed_tools: set[str] = set()
        self._closed = False

    @property
    def pid(self) -> int | None:
        return (
            int(self._proc.pid) if self._proc is not None and self._proc.pid else None
        )

    async def start(self) -> None:
        if not self.server_url:
            port = _allocate_loopback_port()
            self.server_url = f"http://127.0.0.1:{port}"
            argv = build_opencode_serve_argv(
                harness=self.harness,
                binary=self.binary,
                port=port,
            )
            env = build_opencode_env(
                harness=self.harness,
                session_id=self.session_id,
                proxy_root_url=self.proxy_root_url,
                auth_token=self.auth_token,
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

        self._client = httpx.AsyncClient(
            base_url=self.server_url,
            timeout=httpx.Timeout(30.0, read=None),
        )
        await self._wait_for_ready()

        if self.external_session_id and self.fork_session_on_start:
            forked = await self._http_fork_session(
                self.external_session_id, title=self.title
            )
            self.external_session_id = str(forked.get("id") or self.external_session_id)
        elif self.external_session_id:
            resp = await self._client.get(
                f"/session/{self.external_session_id}",
                params={"directory": self.cwd},
            )
            if resp.status_code == 404:
                created = await self._http_create_session()
                self.external_session_id = str(created.get("id") or "")
        else:
            created = await self._http_create_session()
            self.external_session_id = str(created.get("id") or "")

        attach_hint = build_opencode_attach_hint(
            self.harness, self.server_url, self.external_session_id or None
        )
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
        self._sse_task = asyncio.create_task(self._run_sse_loop())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._sse_ready.wait(), timeout=2.0)

    async def _wait_for_ready(self) -> None:
        assert self._client is not None
        deadline = asyncio.get_running_loop().time() + 15.0
        last_err = ""
        while asyncio.get_running_loop().time() < deadline:
            if self._proc is not None and self._proc.returncode is not None:
                snippet = "\n".join(self._stderr_tail[-10:])
                raise RuntimeError(
                    f"{self.harness} serve exited ({self._proc.returncode}): {snippet or 'no stderr'}"
                )
            for probe_path in ("/global/health", "/session", "/doc"):
                try:
                    resp = await self._client.get(probe_path, timeout=1.5)
                    if resp.status_code < 500:
                        return
                except httpx.HTTPError as exc:
                    last_err = str(exc)
            await asyncio.sleep(0.1)
        snippet = "\n".join(self._stderr_tail[-10:])
        raise RuntimeError(
            f"{self.harness} serve did not become ready at {self.server_url}: "
            f"{last_err or snippet or 'timeout'}"
        )

    async def _http_create_session(self) -> dict[str, Any]:
        assert self._client is not None
        body: dict[str, Any] = {}
        if self.title:
            body["title"] = self.title
        resp = await self._client.post(
            "/session",
            params={"directory": self.cwd},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        return dict(data) if isinstance(data, dict) else {}

    async def _http_fork_session(
        self, source_session_id: str, *, title: str = ""
    ) -> dict[str, Any]:
        assert self._client is not None
        body: dict[str, Any] = {}
        if title.strip():
            body["title"] = title.strip()
        resp = await self._client.post(
            f"/session/{source_session_id}/fork",
            params={"directory": self.cwd},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        return dict(data) if isinstance(data, dict) else {}

    async def fork_session(self, *, title: str = "") -> dict[str, Any] | None:
        if not self.external_session_id or self._client is None:
            return None
        return await self._http_fork_session(self.external_session_id, title=title)

    async def send_user_message(self, text: str) -> None:
        if self._client is None or self._closed:
            raise RuntimeError("OpenCode driver is not running")
        await self._on_state_update(status="running")
        await self._on_event("message.user", {"text": text})
        await self._on_event("turn.started", {"prompt": text})

        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if self.model:
            if "/" in self.model:
                provider_id, model_id = self.model.split("/", 1)
                body["model"] = {"providerID": provider_id, "modelID": model_id}
            else:
                body["model"] = {"providerID": "mcc", "modelID": self.model}

        resp = await self._client.post(
            f"/session/{self.external_session_id}/prompt_async",
            params={"directory": self.cwd},
            json=body,
        )
        if resp.status_code in (404, 405):
            resp = await self._client.post(
                f"/session/{self.external_session_id}/message",
                params={"directory": self.cwd},
                json=body,
            )
        resp.raise_for_status()

    async def interrupt(self) -> None:
        if self._client is None or not self.external_session_id:
            return
        with contextlib.suppress(httpx.HTTPError):
            await self._client.post(
                f"/session/{self.external_session_id}/abort",
                params={"directory": self.cwd},
                json={},
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

        if pending.kind == "question":
            await self._reply_question(
                pending,
                decision=decision,
                answers=answers,
            )
        else:
            mapped = "once"
            if decision == "allow_always":
                mapped = "always"
                self._always_allowed.add(pending.tool_name)
            elif decision not in {"allow", "allow_once", "allow_always"}:
                mapped = "reject"
            await self._reply_permission(
                pending.remote_id, reply=mapped, message=message
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

    async def _reply_permission(
        self, permission_id: str, *, reply: str, message: str = ""
    ) -> None:
        if self._client is None:
            return
        v2_body: dict[str, Any] = {"reply": reply}
        if message.strip():
            v2_body["message"] = message.strip()
        resp = await self._client.post(
            f"/permission/{permission_id}/reply",
            params={"directory": self.cwd},
            json=v2_body,
        )
        if resp.status_code in (404, 405) and self.external_session_id:
            v1_body: dict[str, Any] = {"response": reply}
            if message.strip():
                v1_body["message"] = message.strip()
            resp = await self._client.post(
                f"/session/{self.external_session_id}/permissions/{permission_id}",
                params={"directory": self.cwd},
                json=v1_body,
            )
        resp.raise_for_status()

    async def _reply_question(
        self,
        pending: _PendingOpenCodePrompt,
        *,
        decision: str,
        answers: Mapping[str, Any] | None,
    ) -> None:
        if self._client is None:
            return
        if decision not in {"allow", "allow_once", "allow_always"}:
            resp = await self._client.post(
                f"/question/{pending.remote_id}/reject",
                params={"directory": self.cwd},
                json={},
            )
            resp.raise_for_status()
            return

        formatted: list[list[str]] = []
        ans_map = dict(answers or {})
        if pending.questions:
            for idx, question in enumerate(pending.questions):
                q_key = str(
                    question.get("question")
                    or question.get("header")
                    or question.get("id")
                    or idx
                )
                val = ans_map.get(q_key)
                if val is None and str(idx) in ans_map:
                    val = ans_map[str(idx)]
                if isinstance(val, list):
                    formatted.append([str(item) for item in val])
                elif val is not None:
                    formatted.append([str(val)])
                else:
                    formatted.append([])
        else:
            for val in ans_map.values():
                if isinstance(val, list):
                    formatted.append([str(item) for item in val])
                else:
                    formatted.append([str(val)])

        resp = await self._client.post(
            f"/question/{pending.remote_id}/reply",
            params={"directory": self.cwd},
            json={"answers": formatted},
        )
        resp.raise_for_status()

    async def _run_sse_loop(self) -> None:
        assert self._client is not None
        endpoints = ["/event", "/global/event"]
        endpoint_idx = 0
        try:
            while not self._closed:
                path = endpoints[endpoint_idx]
                try:
                    async with self._client.stream(
                        "GET",
                        path,
                        params={"directory": self.cwd},
                        headers={"Accept": "text/event-stream"},
                    ) as resp:
                        if resp.status_code == 404 and endpoint_idx == 0:
                            endpoint_idx = 1
                            continue
                        resp.raise_for_status()
                        self._sse_ready.set()
                        await self._consume_sse_lines(resp)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._sse_ready.set()
                    if self._closed:
                        break
                    logger.debug("OpenCode SSE reconnect: {}", type(exc).__name__)
                    await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise

    async def _consume_sse_lines(self, resp: httpx.Response) -> None:
        data_lines: list[str] = []
        async for raw_line in resp.aiter_lines():
            if self._closed:
                break
            line = raw_line.rstrip("\r")
            if line == "":
                if data_lines:
                    raw_json = "\n".join(data_lines)
                    data_lines.clear()
                    await self._handle_sse_json(raw_json)
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            await self._handle_sse_json("\n".join(data_lines))

    async def _handle_sse_json(self, raw_json: str) -> None:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            return
        if not isinstance(parsed, dict):
            return
        if "payload" in parsed and "type" not in parsed:
            inner = parsed.get("payload")
            if isinstance(inner, dict):
                parsed = inner
        etype = str(parsed.get("type") or "")
        props = parsed.get("properties")
        if not isinstance(props, dict):
            props = {}
        if not etype:
            return

        # Filter unrelated sessions when sessionID is explicitly attached
        event_session = _extract_event_session_id(props)
        if (
            event_session
            and self.external_session_id
            and event_session != self.external_session_id
        ):
            return

        await self._dispatch_sse_event(etype, props)

    async def _dispatch_sse_event(self, etype: str, props: dict[str, Any]) -> None:
        if etype == "message.updated":
            info = props.get("info")
            if isinstance(info, dict):
                msg_id = str(info.get("id") or "")
                role = str(info.get("role") or "")
                if msg_id and role:
                    self._message_roles[msg_id] = role
                model_id = str(info.get("modelID") or "")
                if model_id and model_id != self.model:
                    self.model = model_id
                    await self._on_state_update(model=model_id)
            return

        if etype == "message.part.delta":
            part_id = str(props.get("partID") or "0")
            field_name = str(props.get("field") or "text")
            delta = str(props.get("delta") or "")
            if delta:
                channel = "thinking" if field_name == "reasoning" else "text"
                prev = self._part_lengths.get(part_id, 0)
                self._part_lengths[part_id] = prev + len(delta)
                await self._on_event(
                    "message.delta",
                    {"channel": channel, "block_index": 0, "delta": delta},
                )
            return

        if etype == "message.part.updated":
            await self._handle_part_updated(props)
            return

        if etype in {"session.idle", "session.status"}:
            if etype == "session.status":
                status_obj = props.get("status")
                stype = (
                    str(status_obj.get("type") or "")
                    if isinstance(status_obj, dict)
                    else str(status_obj or "")
                )
                if stype == "busy":
                    await self._on_state_update(status="running")
                    return
                if stype != "idle":
                    return
            await self._on_state_update(status="idle")
            await self._on_event(
                "turn.completed",
                {"subtype": "success", "is_error": False},
            )
            return

        if etype == "session.error":
            err_obj = props.get("error")
            msg = (
                str(err_obj.get("message") or err_obj)
                if isinstance(err_obj, dict)
                else str(err_obj or props.get("message") or "OpenCode session error")
            )
            await self._on_state_update(status="error", last_error=msg)
            await self._on_event("session.error", {"message": msg})
            return

        if etype in {"permission.asked", "permission.updated"}:
            await self._handle_permission_event(props)
            return

        if etype == "question.asked":
            await self._handle_question_event(props)
            return

    async def _handle_part_updated(self, props: dict[str, Any]) -> None:
        part = props.get("part")
        if not isinstance(part, dict):
            return
        msg_id = str(part.get("messageID") or "")
        if self._message_roles.get(msg_id) == "user":
            return
        ptype = str(part.get("type") or "")
        part_id = str(part.get("id") or msg_id or "0")

        if ptype in {"text", "reasoning"}:
            channel = "thinking" if ptype == "reasoning" else "text"
            delta_raw = props.get("delta")
            if isinstance(delta_raw, str) and delta_raw:
                self._part_lengths[part_id] = self._part_lengths.get(part_id, 0) + len(
                    delta_raw
                )
                await self._on_event(
                    "message.delta",
                    {"channel": channel, "block_index": 0, "delta": delta_raw},
                )
                return
            full_text = str(part.get("text") or "")
            if full_text:
                seen_len = self._part_lengths.get(part_id, 0)
                if len(full_text) > seen_len:
                    new_delta = full_text[seen_len:]
                    self._part_lengths[part_id] = len(full_text)
                    await self._on_event(
                        "message.delta",
                        {"channel": channel, "block_index": 0, "delta": new_delta},
                    )
            return

        if ptype == "tool":
            call_id = str(part.get("callID") or part_id)
            tool_name = str(part.get("tool") or "tool")
            state_obj = part.get("state")
            if not isinstance(state_obj, dict):
                state_obj = {}
            status = str(state_obj.get("status") or "")
            if status in {"pending", "running"} and call_id not in self._emitted_tools:
                self._emitted_tools.add(call_id)
                await self._on_event(
                    "tool.started",
                    {
                        "tool_use_id": call_id,
                        "tool_name": tool_name,
                        "input": state_obj.get("input", {}),
                    },
                )
            elif (
                status in {"completed", "error"}
                and call_id not in self._completed_tools
            ):
                if call_id not in self._emitted_tools:
                    self._emitted_tools.add(call_id)
                    await self._on_event(
                        "tool.started",
                        {
                            "tool_use_id": call_id,
                            "tool_name": tool_name,
                            "input": state_obj.get("input", {}),
                        },
                    )
                self._completed_tools.add(call_id)
                content = state_obj.get("output") or state_obj.get("error") or ""
                await self._on_event(
                    "tool.completed",
                    {
                        "tool_use_id": call_id,
                        "is_error": status == "error",
                        "content": content,
                    },
                )

    async def _handle_permission_event(self, props: dict[str, Any]) -> None:
        perm_id = str(props.get("id") or props.get("permissionID") or "")
        if not perm_id:
            return
        perm_type = str(
            props.get("permission")
            or props.get("type")
            or props.get("title")
            or "permission"
        )
        raw_meta = props.get("metadata") or props.get("input")
        tool_input: dict[str, Any] = (
            {str(k): v for k, v in raw_meta.items()}
            if isinstance(raw_meta, dict)
            else {}
        )
        if isinstance(props.get("patterns"), list):
            tool_input["patterns"] = props["patterns"]

        if self.permission_mode == "yolo":
            await self._reply_permission(perm_id, reply="once")
            await self._on_event(
                "approval.auto_approved",
                {
                    "mode": "yolo",
                    "permission_id": perm_id,
                    "tool_name": perm_type,
                },
            )
            return

        if self.permission_mode == "auto_edits" and (
            _is_edit_permission(perm_type) or perm_type in self._always_allowed
        ):
            await self._reply_permission(perm_id, reply="once")
            await self._on_event(
                "approval.auto_approved",
                {
                    "mode": "auto_edits",
                    "permission_id": perm_id,
                    "tool_name": perm_type,
                },
            )
            return

        if self.permission_mode == "ask" and perm_type in self._always_allowed:
            await self._reply_permission(perm_id, reply="once")
            return

        approval_id = f"apr_{secrets.token_hex(8)}"
        await self._on_approval_create(approval_id, perm_type, perm_id, tool_input)
        self._pending[approval_id] = _PendingOpenCodePrompt(
            approval_id=approval_id,
            remote_id=perm_id,
            kind="permission",
            tool_name=perm_type,
        )
        await self._on_state_update(status="awaiting_approval")
        await self._on_event(
            "approval.requested",
            {
                "approval_id": approval_id,
                "request_id": perm_id,
                "tool_name": perm_type,
                "tool_use_id": perm_id,
                "input": tool_input,
                "choices": ["allow_once", "allow_always", "deny"],
            },
        )

    async def _handle_question_event(self, props: dict[str, Any]) -> None:
        q_id = str(props.get("id") or props.get("questionID") or "")
        if not q_id:
            return
        raw_questions = props.get("questions")
        questions = (
            [dict(q) for q in raw_questions if isinstance(q, dict)]
            if isinstance(raw_questions, list)
            else []
        )
        approval_id = f"apr_{secrets.token_hex(8)}"
        await self._on_approval_create(
            approval_id, "AskUserQuestion", q_id, {"questions": questions}
        )
        self._pending[approval_id] = _PendingOpenCodePrompt(
            approval_id=approval_id,
            remote_id=q_id,
            kind="question",
            tool_name="AskUserQuestion",
            questions=questions,
        )
        await self._on_state_update(status="awaiting_question")
        await self._on_event(
            "question.requested",
            {
                "approval_id": approval_id,
                "request_id": q_id,
                "questions": questions,
            },
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
        self._pending.clear()

        if self._client is not None and self.external_session_id:
            with contextlib.suppress(Exception):
                await self._client.post(
                    f"/session/{self.external_session_id}/abort",
                    params={"directory": self.cwd},
                    json={},
                    timeout=1.5,
                )

        for task in (self._sse_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

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


def _extract_event_session_id(props: dict[str, Any]) -> str:
    for key in ("sessionID", "sessionId"):
        val = props.get(key)
        if isinstance(val, str) and val:
            return val
    for nested_key in ("info", "part"):
        nested = props.get(nested_key)
        if isinstance(nested, dict):
            for key in ("sessionID", "sessionId"):
                val = nested.get(key)
                if isinstance(val, str) and val:
                    return val
    return ""
