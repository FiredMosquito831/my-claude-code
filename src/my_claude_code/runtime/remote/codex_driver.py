"""Tier 1A structured driver for OpenAI Codex CLI (`codex app-server --listen stdio://`)."""

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

from my_claude_code.cli.launchers.codex import (
    build_codex_launcher_env,
    build_model_catalog_config_args,
    codex_config_args,
)
from my_claude_code.config.paths import codex_model_catalog_path

from .drivers import (
    ApprovalCreateCallback,
    EventEmitCallback,
    StateUpdateCallback,
)
from .pty_bridge import win_exec_argv


def resolve_codex_binary(explicit: str | None = None) -> str:
    """Resolve the Codex CLI executable path."""

    if explicit:
        return explicit
    return shutil.which("codex") or "codex"


def build_codex_app_server_argv(
    *,
    codex_bin: str,
    proxy_root_url: str,
    session_id: str,
    model: str = "",
) -> list[str]:
    """Build ``codex app-server --listen stdio://`` argv with ephemeral MCC config."""

    del session_id
    clean_root = proxy_root_url.rstrip("/")
    v1_url = clean_root if clean_root.endswith("/v1") else f"{clean_root}/v1"
    argv = [codex_bin, "app-server", "--listen", "stdio://"]

    catalog_path = codex_model_catalog_path()
    if catalog_path.is_file():
        argv.extend(build_model_catalog_config_args(str(catalog_path)))

    argv.extend(
        codex_config_args(
            api_url=v1_url,
            model=model.strip() or None,
        )
    )
    return argv


def build_codex_env(
    *,
    auth_token: str,
    session_id: str,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build isolated child environment pointing Codex at MCC's proxy token."""

    source = os.environ if base_env is None else base_env
    env = build_codex_launcher_env(auth_token=auth_token, base_env=source)
    env["MCC_RC_SESSION_ID"] = session_id
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _approval_policy_for_mode(mode: str) -> tuple[str, str]:
    if mode == "yolo":
        return "never", "danger-full-access"
    return "on-request", "workspace-write"


def _as_dict(value: Any) -> dict[str, Any]:
    return {str(k): v for k, v in value.items()} if isinstance(value, Mapping) else {}


@dataclass(slots=True)
class _PendingCodexRequest:
    approval_id: str
    rpc_id: Any
    method: str
    tool_name: str
    is_question: bool
    questions: list[dict[str, Any]] = field(default_factory=list)
    future: asyncio.Future[dict[str, Any]] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class CodexAppServerDriver:
    """Supervises ``codex app-server --listen stdio://`` over JSON-RPC 2.0."""

    driver_tier: str = "tier_1a_codex"

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
        codex_bin: str | None = None,
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
        self.codex_bin = resolve_codex_binary(codex_bin)
        self._on_event = on_event
        self._on_approval_create = on_approval_create
        self._on_state_update = on_state_update

        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._next_rpc_id = 1
        self._rpc_waiters: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._pending: dict[str, _PendingCodexRequest] = {}
        self._always_allowed: set[str] = set()
        self._stderr_tail: list[str] = []
        self._current_turn_id = ""
        self._closed = False

    @property
    def pid(self) -> int | None:
        return (
            int(self._proc.pid) if self._proc is not None and self._proc.pid else None
        )

    async def start(self) -> None:
        argv = build_codex_app_server_argv(
            codex_bin=self.codex_bin,
            proxy_root_url=self.proxy_root_url,
            session_id=self.session_id,
            model=self.model,
        )
        env = build_codex_env(
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

        await self._rpc_call(
            "initialize",
            {
                "clientInfo": {
                    "name": "mcc-rc",
                    "title": "MCC Remote Control",
                    "version": "8.54.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._write_json({"method": "initialized", "params": {}})

        approval_policy, sandbox = _approval_policy_for_mode(self.permission_mode)
        thread_params: dict[str, Any] = {
            "cwd": self.cwd,
            "approvalPolicy": approval_policy,
            "sandbox": sandbox,
        }
        if self.model:
            thread_params["model"] = self.model

        if self.external_session_id and self.fork_session_on_start:
            thread_params["threadId"] = self.external_session_id
            res = await self._rpc_call("thread/fork", thread_params)
        elif self.external_session_id:
            thread_params["threadId"] = self.external_session_id
            res = await self._rpc_call("thread/resume", thread_params)
        else:
            res = await self._rpc_call("thread/start", thread_params)

        thread_obj = _as_dict(res.get("thread"))
        thread_id = str(
            thread_obj.get("id")
            or res.get("threadId")
            or res.get("id")
            or self.external_session_id
        )
        self.external_session_id = thread_id

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
                "permission_mode": self.permission_mode,
                "external_session_id": self.external_session_id,
            },
        )

    async def _write_json(self, payload: Mapping[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None or self._closed:
            raise RuntimeError("Codex app-server process is not running")
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()

    async def _rpc_call(
        self, method: str, params: Mapping[str, Any], *, timeout: float = 20.0
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
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._rpc_waiters.pop(rpc_id, None)

    async def send_user_message(self, text: str) -> None:
        if not self.external_session_id or self._closed:
            raise RuntimeError("Codex app-server session is not ready")
        await self._on_state_update(status="running")
        await self._on_event("message.user", {"text": text})
        await self._on_event("turn.started", {"prompt": text})

        approval_policy, _sandbox = _approval_policy_for_mode(self.permission_mode)
        params: dict[str, Any] = {
            "threadId": self.external_session_id,
            "input": [{"type": "text", "text": text}],
            "approvalPolicy": approval_policy,
        }
        if self.model:
            params["model"] = self.model
        res = await self._rpc_call("turn/start", params)
        turn = _as_dict(res.get("turn"))
        tid = str(turn.get("id") or res.get("turnId") or "")
        if tid:
            self._current_turn_id = tid

    async def interrupt(self) -> None:
        if not self.external_session_id or self._closed:
            return
        params: dict[str, Any] = {"threadId": self.external_session_id}
        if self._current_turn_id:
            params["turnId"] = self._current_turn_id
        with contextlib.suppress(Exception):
            await self._rpc_call("turn/interrupt", params, timeout=5.0)
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
        if not self.external_session_id or self._closed:
            return None
        approval_policy, sandbox = _approval_policy_for_mode(self.permission_mode)
        return await self._rpc_call(
            "thread/fork",
            {
                "threadId": self.external_session_id,
                "cwd": self.cwd,
                "approvalPolicy": approval_policy,
                "sandbox": sandbox,
            },
        )

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        updated_input: Mapping[str, Any] | None = None,
        answers: Mapping[str, Any] | None = None,
        message: str = "",
    ) -> None:
        del updated_input, message
        pending = self._pending.get(approval_id)
        if pending is None or pending.future.done():
            raise KeyError(approval_id)

        if pending.is_question:
            formatted: dict[str, Any] = {}
            ans_map = dict(answers or {})
            if pending.questions:
                for idx, question in enumerate(pending.questions):
                    qid = str(
                        question.get("id")
                        or question.get("question")
                        or question.get("header")
                        or idx
                    )
                    q_label = str(
                        question.get("question") or question.get("header") or qid
                    )
                    val = ans_map.get(qid)
                    if val is None:
                        val = ans_map.get(q_label)
                    if val is None and str(idx) in ans_map:
                        val = ans_map[str(idx)]
                    if isinstance(val, dict) and "answers" in val:
                        formatted[qid] = val
                    elif isinstance(val, list):
                        formatted[qid] = {"answers": [str(item) for item in val]}
                    elif val is not None:
                        formatted[qid] = {"answers": [str(val)]}
                    else:
                        formatted[qid] = {"answers": []}
            else:
                for key, val in ans_map.items():
                    if isinstance(val, dict) and "answers" in val:
                        formatted[str(key)] = val
                    elif isinstance(val, list):
                        formatted[str(key)] = {"answers": [str(item) for item in val]}
                    else:
                        formatted[str(key)] = {"answers": [str(val)]}
            result: dict[str, Any] = {"answers": formatted}
        else:
            if decision == "allow_always":
                self._always_allowed.add(pending.tool_name)
                rpc_decision = "acceptForSession"
            elif decision in {"allow", "allow_once"}:
                rpc_decision = "accept"
            else:
                rpc_decision = "decline"
            result = {"decision": rpc_decision}

        pending.future.set_result(result)
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

    async def _handle_server_request(
        self, rpc_id: Any, method: str, params: dict[str, Any]
    ) -> None:
        is_command = method in {
            "item/commandExecution/requestApproval",
            "execCommandApproval",
        }
        is_file_change = method in {
            "item/fileChange/requestApproval",
            "applyPatchApproval",
        }
        is_question = method == "item/tool/requestUserInput"

        if not (is_command or is_file_change or is_question):
            await self._write_json(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {
                        "code": -32601,
                        "message": f"Unsupported method {method}",
                    },
                }
            )
            return

        if is_file_change:
            tool_name = "FileChange"
            if self.permission_mode in {"yolo", "auto_edits"} or (
                tool_name in self._always_allowed
            ):
                await self._reply_rpc(rpc_id, {"decision": "accept"})
                await self._on_event(
                    "approval.auto_approved",
                    {
                        "mode": self.permission_mode,
                        "method": method,
                        "tool_name": tool_name,
                    },
                )
                return
        elif is_command:
            tool_name = "Bash"
            if self.permission_mode == "yolo" or (
                self.permission_mode == "ask" and tool_name in self._always_allowed
            ):
                await self._reply_rpc(rpc_id, {"decision": "accept"})
                await self._on_event(
                    "approval.auto_approved",
                    {
                        "mode": self.permission_mode,
                        "method": method,
                        "tool_name": tool_name,
                    },
                )
                return
        else:
            tool_name = "AskUserQuestion"

        approval_id = f"apr_{secrets.token_hex(8)}"
        tool_use_id = str(params.get("itemId") or params.get("callId") or "")
        questions = (
            [dict(q) for q in params["questions"] if isinstance(q, dict)]
            if is_question and isinstance(params.get("questions"), list)
            else []
        )
        await self._on_approval_create(approval_id, tool_name, tool_use_id, params)
        pending = _PendingCodexRequest(
            approval_id=approval_id,
            rpc_id=rpc_id,
            method=method,
            tool_name=tool_name,
            is_question=is_question,
            questions=questions,
        )
        self._pending[approval_id] = pending

        if is_question:
            await self._on_state_update(status="awaiting_question")
            await self._on_event(
                "question.requested",
                {
                    "approval_id": approval_id,
                    "request_id": str(rpc_id),
                    "questions": questions,
                },
            )
        else:
            await self._on_state_update(status="awaiting_approval")
            await self._on_event(
                "approval.requested",
                {
                    "approval_id": approval_id,
                    "request_id": str(rpc_id),
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                    "input": params,
                    "choices": ["allow_once", "allow_always", "deny"],
                },
            )

        asyncio.create_task(self._await_and_reply(pending))

    async def _await_and_reply(self, pending: _PendingCodexRequest) -> None:
        try:
            result = await pending.future
        except asyncio.CancelledError:
            return
        finally:
            self._pending.pop(pending.approval_id, None)
        if not self._closed:
            with contextlib.suppress(Exception):
                await self._reply_rpc(pending.rpc_id, result)

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "turn/started":
            turn = _as_dict(params.get("turn"))
            tid = str(turn.get("id") or "")
            if tid:
                self._current_turn_id = tid
            await self._on_state_update(status="running")
            return

        if method == "item/started":
            item = _as_dict(params.get("item"))
            itype = str(item.get("type") or "")
            if itype in {"commandExecution", "fileChange", "mcpToolCall", "webSearch"}:
                await self._on_event(
                    "tool.started",
                    {
                        "tool_use_id": str(item.get("id") or ""),
                        "tool_name": itype,
                        "input": item,
                    },
                )
            return

        if method == "item/agentMessage/delta":
            delta = str(params.get("delta") or "")
            if delta:
                await self._on_event(
                    "message.delta",
                    {"channel": "text", "block_index": 0, "delta": delta},
                )
            return

        if method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
            delta = str(params.get("delta") or "")
            if delta:
                await self._on_event(
                    "message.delta",
                    {"channel": "thinking", "block_index": 0, "delta": delta},
                )
            return

        if method in {
            "item/commandExecution/outputDelta",
            "item/fileChange/outputDelta",
        }:
            delta = str(params.get("delta") or "")
            if delta:
                await self._on_event(
                    "tool.output",
                    {
                        "tool_use_id": str(params.get("itemId") or ""),
                        "delta": delta,
                    },
                )
            return

        if method == "item/completed":
            item = _as_dict(params.get("item"))
            itype = str(item.get("type") or "")
            if itype == "agentMessage":
                text = str(item.get("text") or "")
                if text:
                    await self._on_event(
                        "message.assistant", {"channel": "text", "text": text}
                    )
            elif itype in {
                "commandExecution",
                "fileChange",
                "mcpToolCall",
                "webSearch",
            }:
                status = str(item.get("status") or "")
                content = (
                    item.get("aggregatedOutput") or item.get("output") or status or ""
                )
                await self._on_event(
                    "tool.completed",
                    {
                        "tool_use_id": str(item.get("id") or ""),
                        "is_error": status == "failed",
                        "content": content,
                    },
                )
            return

        if method == "turn/completed":
            turn = _as_dict(params.get("turn"))
            status = str(turn.get("status") or "completed")
            await self._on_state_update(status="idle")
            await self._on_event(
                "turn.completed",
                {
                    "subtype": status,
                    "is_error": status == "failed",
                    "error": turn.get("error"),
                },
            )
            return

        if method == "error":
            err = params.get("error")
            msg = (
                str(err.get("message") or err)
                if isinstance(err, dict)
                else str(err or params.get("message") or "Codex error")
            )
            await self._on_state_update(status="error", last_error=msg)
            await self._on_event("session.error", {"message": msg})

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
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    await self._on_event("driver.raw_line", {"line": line})
                    continue
                if not isinstance(msg, dict):
                    continue
                has_id = "id" in msg and msg["id"] is not None
                method = msg.get("method")
                if has_id and isinstance(method, str):
                    raw_params = msg.get("params")
                    params = (
                        {str(k): v for k, v in raw_params.items()}
                        if isinstance(raw_params, dict)
                        else {}
                    )
                    await self._handle_server_request(msg["id"], method, params)
                elif has_id:
                    waiter = self._rpc_waiters.get(msg["id"])
                    if waiter is not None and not waiter.done():
                        if "error" in msg and msg["error"] is not None:
                            waiter.set_exception(RuntimeError(str(msg["error"])))
                        else:
                            res = msg.get("result")
                            waiter.set_result(
                                dict(res) if isinstance(res, dict) else {}
                            )
                elif isinstance(method, str):
                    raw_params = msg.get("params")
                    params = (
                        {str(k): v for k, v in raw_params.items()}
                        if isinstance(raw_params, dict)
                        else {}
                    )
                    await self._handle_notification(method, params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Codex driver stdout error: {}", type(exc).__name__)
            await self._on_state_update(status="error", last_error=str(exc))
            await self._on_event("session.error", {"message": str(exc)})
        finally:
            if not self._closed:
                rc = await self._proc.wait()
                if self._stderr_task is not None:
                    with contextlib.suppress(TimeoutError, Exception):
                        await asyncio.wait_for(self._stderr_task, timeout=0.5)
                snippet = "\n".join(self._stderr_tail[-10:])
                for waiter in list(self._rpc_waiters.values()):
                    if not waiter.done():
                        waiter.set_exception(
                            RuntimeError(
                                f"Codex app-server exited unexpectedly ({rc}): {snippet or 'no stderr'}"
                            )
                        )
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
