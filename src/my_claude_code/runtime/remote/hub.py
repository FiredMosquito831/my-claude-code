"""RemoteRuntimeHub: concrete RemoteSessionPort implementation."""

import asyncio
import contextlib
import os
import secrets
import shutil
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from my_claude_code.application.remote_ports import (
    RemoteConflictError,
    RemoteControlError,
    RemoteDisabledError,
    RemoteForbiddenError,
    RemoteLimitError,
    RemoteNotFoundError,
)
from my_claude_code.cli.claude_env import build_claude_proxy_env
from my_claude_code.config.constants import RC_PERMISSION_MODE_VALUES
from my_claude_code.config.harnesses import HARNESS_SPECS, harness_spec
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import Settings, get_settings

from .claude_driver import ClaudeBidirectionalDriver
from .codex_driver import CodexAppServerDriver
from .discovery import discover_local_sessions, is_within_allowed_roots
from .drivers import StructuredDriver
from .git_workspace import (
    collect_git_diff,
    create_session_worktree,
    detect_git_root,
    remove_session_worktree,
)
from .openclaw_driver import OpenClawGatewayDriver
from .opencode_driver import OPENCODE_FAMILY_HARNESSES, OpenCodeFamilyDriver
from .pty_bridge import PtySession
from .qr import render_qr_svg
from .store import RemoteStore
from .tunnels import TunnelManager

_ACTIVE_STATUSES = frozenset(
    {"starting", "idle", "running", "awaiting_approval", "awaiting_question"}
)

_HARNESS_DRIVER_TIERS: dict[str, str] = {
    "claude": "tier_1b_claude",
    "opencode": "tier_1a_opencode",
    "opencode2": "tier_1a_opencode",
    "kilo": "tier_1a_opencode",
    "codex": "tier_1a_codex",
    "openclaw": "tier_1a_openclaw",
}


def driver_tier_for_harness(harness: str, *, driver_mode: str = "auto") -> str:
    """Return the effective driver tier badge for *harness* and *driver_mode*."""

    if driver_mode == "pty":
        return "tier_3_pty"
    return _HARNESS_DRIVER_TIERS.get(harness, "tier_3_pty")


@dataclass(slots=True)
class _LiveSession:
    session_id: str
    harness: str
    driver_tier: str
    driver: StructuredDriver | None = None
    pty_session: PtySession | None = None
    repo_root: Path | None = None
    worktree_path: Path | None = None


def _parse_allowed_dirs(raw: str) -> list[Path]:
    text = (raw or "").strip()
    if not text:
        return []
    parts: list[str] = []
    for chunk in text.split(","):
        if os.pathsep != ",":
            # Keep Windows drive letters like C:\foo intact when splitting on ';'
            parts.extend(chunk.split(os.pathsep))
        else:
            parts.append(chunk)
    roots: list[Path] = []
    for part in parts:
        cleaned = part.strip()
        if not cleaned:
            continue
        with contextlib.suppress(OSError, RuntimeError):
            roots.append(Path(cleaned).expanduser().resolve())
    return roots


def _default_shell_argv() -> list[str]:
    if os.name == "nt":
        for candidate in ("pwsh", "powershell.exe", "cmd.exe"):
            resolved = shutil.which(candidate)
            if resolved:
                return [resolved]
        return ["cmd.exe"]
    for candidate in ("zsh", "bash", "sh"):
        resolved = shutil.which(candidate)
        if resolved:
            return [resolved]
    return ["/bin/sh"]


class RemoteRuntimeHub:
    """Supervises Remote Control sessions, workspaces, devices, and tunnels."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self._store = RemoteStore()
        self._tunnels = TunnelManager()
        self._sessions: dict[str, _LiveSession] = {}
        self._event_subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._ws_closers: dict[str, set[Callable[[], Any]]] = {}
        self._lock = asyncio.Lock()

    def current_settings(self) -> Settings:
        return self._settings if self._settings is not None else get_settings()

    def update_settings(self, settings: Settings) -> None:
        self._settings = settings

    def is_enabled(self) -> bool:
        return bool(self.current_settings().rc_enabled)

    def _require_enabled(self) -> Settings:
        settings = self.current_settings()
        if not settings.rc_enabled:
            raise RemoteDisabledError(
                "Remote Control is disabled. Enable RC_ENABLED in Admin -> Remote Control."
            )
        return settings

    def session_signing_secret(self) -> str:
        settings = self.current_settings()
        return self._store.get_or_create_signing_secret(settings.rc_session_secret)

    # ----------------------------------------------------- admin & devices --

    def status_snapshot(self) -> dict[str, Any]:
        settings = self.current_settings()
        sessions = self._store.list_sessions()
        active_count = sum(1 for s in sessions if s.get("status") in _ACTIVE_STATUSES)
        return {
            "enabled": bool(settings.rc_enabled),
            "permission_mode": settings.rc_permission_mode,
            "allowed_dirs": [
                str(p) for p in _parse_allowed_dirs(settings.rc_allowed_dirs)
            ],
            "max_sessions": settings.rc_max_sessions,
            "active_sessions": active_count,
            "total_sessions": len(sessions),
            "tunnel_mode": settings.rc_tunnel_mode,
            "active_tunnel_mode": self._tunnels.active_mode(),
            "device_count": len(self._store.list_devices()),
        }

    async def inspect_tunnels(self) -> dict[str, Any]:
        return await self._tunnels.inspect(self.current_settings())

    async def start_tunnel(self, mode: str | None = None) -> dict[str, Any]:
        settings = self.current_settings()
        chosen = (mode or settings.rc_tunnel_mode or "off").strip().lower()
        return await self._tunnels.start(chosen, settings)

    async def stop_tunnel(self) -> dict[str, Any]:
        return await self._tunnels.stop(self.current_settings())

    def generate_qr_svg(self, url: str) -> str:
        return render_qr_svg(url)

    def list_devices(self) -> list[dict[str, Any]]:
        return self._store.list_devices()

    def register_device(
        self,
        *,
        device_id: str,
        label: str,
        user_agent: str,
        ip: str,
    ) -> dict[str, Any]:
        return self._store.register_device(
            device_id=device_id,
            label=label,
            user_agent=user_agent,
            ip=ip,
        )

    def touch_device(self, device_id: str, *, ip: str, user_agent: str) -> bool:
        return self._store.touch_device(device_id, ip=ip, user_agent=user_agent)

    def is_device_active(self, device_id: str) -> bool:
        return self._store.is_device_active(device_id)

    def register_ws_closer(
        self, device_id: str, closer: Callable[[], Any]
    ) -> Callable[[], None]:
        bucket = self._ws_closers.setdefault(device_id, set())
        bucket.add(closer)

        def _unregister() -> None:
            current = self._ws_closers.get(device_id)
            if current is not None:
                current.discard(closer)
                if not current:
                    self._ws_closers.pop(device_id, None)

        return _unregister

    def _close_device_websockets(self, device_id: str) -> None:
        closers = list(self._ws_closers.pop(device_id, ()))
        for closer in closers:
            with contextlib.suppress(Exception):
                result = closer()
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)

    def revoke_device(self, device_id: str) -> bool:
        revoked = self._store.revoke_device(device_id)
        if revoked:
            self._close_device_websockets(device_id)
        return revoked

    def revoke_all_devices(self) -> int:
        count = self._store.revoke_all_devices()
        for dev_id in list(self._ws_closers):
            self._close_device_websockets(dev_id)
        return count

    # ---------------------------------------------------- directory & meta --

    def list_allowed_dirs(self, path: str | None = None) -> dict[str, Any]:
        settings = self._require_enabled()
        roots = _parse_allowed_dirs(settings.rc_allowed_dirs)
        if not roots:
            return {
                "roots": [],
                "current": None,
                "entries": [],
                "message": "No RC_ALLOWED_DIRS configured. Set allowed roots in Admin -> Remote Control.",
            }
        if not path:
            return {
                "roots": [str(r) for r in roots],
                "current": None,
                "entries": [
                    {"name": r.name or str(r), "path": str(r), "is_root": True}
                    for r in roots
                    if r.is_dir()
                ],
            }
        target = Path(path).expanduser()
        if not is_within_allowed_roots(target, roots):
            raise RemoteForbiddenError("Requested directory is outside RC_ALLOWED_DIRS")
        resolved = target.resolve()
        if not resolved.is_dir():
            raise RemoteNotFoundError(f"Directory not found: {resolved}")
        children: list[dict[str, Any]] = []
        try:
            for item in sorted(resolved.iterdir(), key=lambda p: p.name.lower()):
                if not item.is_dir():
                    continue
                if item.name.startswith("."):
                    continue
                if is_within_allowed_roots(item, roots):
                    children.append(
                        {
                            "name": item.name,
                            "path": str(item.resolve()),
                            "is_root": False,
                        }
                    )
        except OSError as exc:
            raise RemoteControlError(f"Cannot list {resolved}: {exc}") from exc
        return {
            "roots": [str(r) for r in roots],
            "current": str(resolved),
            "entries": children[:200],
        }

    def list_harnesses(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for spec in HARNESS_SPECS:
            if not spec.available:
                continue
            tier = driver_tier_for_harness(spec.id)
            items.append(
                {
                    "id": spec.id,
                    "label": spec.display_name,
                    "binary": spec.binary,
                    "installed": shutil.which(spec.binary) is not None,
                    "driver_tier": tier,
                    "supports_structured": tier != "tier_3_pty",
                    "supports_pty": True,
                }
            )
        items.append(
            {
                "id": "terminal",
                "label": "Interactive Shell",
                "binary": _default_shell_argv()[0],
                "installed": True,
                "driver_tier": "tier_3_pty",
                "supports_structured": False,
                "supports_pty": True,
            }
        )
        return items

    def list_discovered_sessions(
        self, *, harness: str | None = None
    ) -> list[dict[str, Any]]:
        settings = self._require_enabled()
        roots = _parse_allowed_dirs(settings.rc_allowed_dirs)
        return discover_local_sessions(roots, harness_filter=harness)

    # ------------------------------------------------------------ sessions --

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._store.list_sessions()

    def get_session(self, session_id: str) -> dict[str, Any]:
        rec = self._store.get_session(session_id)
        if rec is None:
            raise RemoteNotFoundError(f"Session {session_id!r} not found")
        rec["pending_approvals"] = self._store.list_pending_approvals(session_id)
        return rec

    def get_events(
        self, session_id: str, *, since_seq: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        self.get_session(session_id)
        return self._store.list_events(session_id, since_seq=since_seq, limit=limit)

    async def _emit(self, session_id: str, kind: str, payload: dict[str, Any]) -> None:
        event = self._store.append_event(session_id, kind, payload)
        for queue in list(self._event_subs.get(session_id, ())):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    def _build_structured_driver(
        self,
        *,
        harness: str,
        session_id: str,
        cwd: str,
        title: str,
        proxy_root: str,
        auth_token: str,
        permission_mode: str,
        model: str,
        external_session_id: str | None,
        fork_session: bool,
        server_url: str | None,
    ) -> StructuredDriver | None:
        async def _on_event(kind: str, payload: dict[str, Any]) -> None:
            await self._emit(session_id, kind, payload)

        async def _on_approval(
            approval_id: str,
            tool_name: str,
            tool_use_id: str,
            tool_input: dict[str, Any],
        ) -> dict[str, Any]:
            return self._store.create_approval(
                approval_id=approval_id,
                session_id=session_id,
                tool_name=tool_name,
                tool_use_id=tool_use_id,
                tool_input=tool_input,
            )

        async def _on_state(**fields: Any) -> None:
            self._store.update_session(session_id, **fields)

        if harness == "claude":
            return ClaudeBidirectionalDriver(
                session_id=session_id,
                cwd=cwd,
                proxy_root_url=proxy_root,
                auth_token=auth_token,
                permission_mode=permission_mode,
                model=model,
                external_session_id=external_session_id,
                fork_session=fork_session,
                on_event=_on_event,
                on_approval_create=_on_approval,
                on_state_update=_on_state,
            )
        if harness in OPENCODE_FAMILY_HARNESSES:
            return OpenCodeFamilyDriver(
                harness=harness,
                session_id=session_id,
                cwd=cwd,
                proxy_root_url=proxy_root,
                auth_token=auth_token,
                permission_mode=permission_mode,
                model=model,
                title=title,
                external_session_id=external_session_id,
                fork_session=fork_session,
                server_url=server_url,
                on_event=_on_event,
                on_approval_create=_on_approval,
                on_state_update=_on_state,
            )
        if harness == "codex":
            return CodexAppServerDriver(
                session_id=session_id,
                cwd=cwd,
                proxy_root_url=proxy_root,
                auth_token=auth_token,
                permission_mode=permission_mode,
                model=model,
                external_session_id=external_session_id,
                fork_session=fork_session,
                on_event=_on_event,
                on_approval_create=_on_approval,
                on_state_update=_on_state,
            )
        if harness == "openclaw":
            return OpenClawGatewayDriver(
                session_id=session_id,
                cwd=cwd,
                proxy_root_url=proxy_root,
                auth_token=auth_token,
                permission_mode=permission_mode,
                model=model,
                external_session_id=external_session_id,
                fork_session=fork_session,
                server_url=server_url,
                on_event=_on_event,
                on_approval_create=_on_approval,
                on_state_update=_on_state,
            )
        return None

    async def create_session(
        self,
        *,
        harness: str,
        cwd: str,
        title: str = "",
        prompt: str = "",
        permission_mode: str | None = None,
        model: str | None = None,
        create_worktree: bool = False,
        driver_mode: str = "auto",
        external_session_id: str | None = None,
        fork_session: bool = False,
        server_url: str | None = None,
        cols: int = 120,
        rows: int = 36,
    ) -> dict[str, Any]:
        settings = self._require_enabled()
        roots = _parse_allowed_dirs(settings.rc_allowed_dirs)
        if not cwd or not cwd.strip():
            raise RemoteForbiddenError(
                "Workspace directory is required and must be inside RC_ALLOWED_DIRS"
            )
        candidate_cwd = Path(cwd).expanduser()
        if not is_within_allowed_roots(candidate_cwd, roots):
            raise RemoteForbiddenError(
                "Workspace directory is outside RC_ALLOWED_DIRS (or RC_ALLOWED_DIRS is empty)"
            )
        resolved_cwd = candidate_cwd.resolve()
        if not resolved_cwd.is_dir():
            raise RemoteNotFoundError(
                f"Workspace directory does not exist: {resolved_cwd}"
            )

        chosen_mode = (
            (permission_mode or settings.rc_permission_mode or "ask").strip().lower()
        )
        if chosen_mode not in RC_PERMISSION_MODE_VALUES:
            raise RemoteForbiddenError(f"Invalid permission mode: {chosen_mode!r}")

        valid_harnesses = {s.id for s in HARNESS_SPECS if s.available} | {"terminal"}
        if harness not in valid_harnesses:
            raise RemoteControlError(f"Unknown or unavailable harness: {harness!r}")

        async with self._lock:
            all_sessions = self._store.list_sessions()
            active_count = sum(
                1 for s in all_sessions if s.get("status") in _ACTIVE_STATUSES
            )
            if active_count >= settings.rc_max_sessions:
                raise RemoteLimitError(
                    f"Maximum active Remote Control sessions ({settings.rc_max_sessions}) reached"
                )

            session_id = f"rc_{secrets.token_hex(8)}"
            repo_root = await detect_git_root(resolved_cwd)
            effective_cwd = resolved_cwd
            worktree_path_str = ""
            branch_name = ""
            worktree_path_obj: Path | None = None
            if create_worktree and repo_root is not None:
                worktree_path_obj, branch_name = await create_session_worktree(
                    repo_root, session_id
                )
                effective_cwd = worktree_path_obj
                worktree_path_str = str(worktree_path_obj)

            driver_tier = driver_tier_for_harness(harness, driver_mode=driver_mode)
            use_structured = driver_tier != "tier_3_pty" and driver_mode in (
                "auto",
                "structured",
            )
            now = time.time()
            session_title = title.strip() or f"{harness} — {effective_cwd.name}"
            self._store.create_session(
                {
                    "id": session_id,
                    "harness": harness,
                    "driver_tier": driver_tier,
                    "title": session_title,
                    "cwd": str(effective_cwd),
                    "worktree_path": worktree_path_str,
                    "branch_name": branch_name,
                    "permission_mode": chosen_mode,
                    "status": "starting",
                    "model": (model or "").strip(),
                    "provider_id": "",
                    "external_session_id": (external_session_id or "").strip(),
                    "server_url": (server_url or "").strip(),
                    "attach_hint": "",
                    "pid": None,
                    "created_at": now,
                    "updated_at": now,
                    "last_error": "",
                }
            )
            live = _LiveSession(
                session_id=session_id,
                harness=harness,
                driver_tier=driver_tier,
                repo_root=repo_root,
                worktree_path=worktree_path_obj,
            )
            self._sessions[session_id] = live

        try:
            proxy_root = local_proxy_root_url(settings)
            auth_token = settings.anthropic_auth_token

            driver = (
                self._build_structured_driver(
                    harness=harness,
                    session_id=session_id,
                    cwd=str(effective_cwd),
                    title=session_title,
                    proxy_root=proxy_root,
                    auth_token=auth_token,
                    permission_mode=chosen_mode,
                    model=(model or "").strip(),
                    external_session_id=external_session_id,
                    fork_session=fork_session,
                    server_url=server_url,
                )
                if use_structured
                else None
            )

            if driver is not None:
                live.driver = driver
                await driver.start()
                if prompt.strip():
                    await driver.send_user_message(prompt.strip())
            else:
                argv = self._build_pty_argv(
                    harness,
                    model=model,
                    prompt=prompt,
                    external_session_id=external_session_id,
                    server_url=server_url,
                )
                env = self._build_pty_env(settings, session_id=session_id)
                pty_sess = PtySession()
                live.pty_session = pty_sess

                def _on_pty_exit(rc: int) -> None:
                    self._store.update_session(session_id, status="exited", pid=None)
                    asyncio.create_task(
                        self._emit(session_id, "session.exited", {"returncode": rc})
                    )

                await pty_sess.spawn(
                    argv,
                    cwd=str(effective_cwd),
                    env=env,
                    cols=cols,
                    rows=rows,
                    on_exit=_on_pty_exit,
                )
                self._store.update_session(
                    session_id, status="running", pid=pty_sess.pid
                )
                await self._emit(
                    session_id,
                    "session.state",
                    {
                        "status": "running",
                        "pid": pty_sess.pid,
                        "driver_tier": driver_tier,
                    },
                )
        except Exception as exc:
            self._store.update_session(
                session_id, status="error", last_error=str(exc), pid=None
            )
            await self._emit(session_id, "session.error", {"message": str(exc)})
            raise RemoteControlError(f"Failed to start session: {exc}") from exc

        return self.get_session(session_id)

    async def import_session(
        self,
        *,
        external_session_id: str,
        harness: str,
        cwd: str,
        title: str = "",
        permission_mode: str | None = None,
        model: str | None = None,
        fork_session: bool = False,
        create_worktree: bool = False,
        driver_mode: str = "auto",
        server_url: str | None = None,
    ) -> dict[str, Any]:
        settings = self._require_enabled()
        roots = _parse_allowed_dirs(settings.rc_allowed_dirs)
        if not cwd or not cwd.strip():
            raise RemoteForbiddenError(
                "Discovered session has no workspace directory inside RC_ALLOWED_DIRS"
            )
        if not is_within_allowed_roots(Path(cwd).expanduser(), roots):
            raise RemoteForbiddenError(
                "Discovered session workspace is outside RC_ALLOWED_DIRS"
            )
        return await self.create_session(
            harness=harness,
            cwd=cwd,
            title=title,
            permission_mode=permission_mode,
            model=model,
            create_worktree=create_worktree,
            driver_mode=driver_mode,
            external_session_id=external_session_id,
            fork_session=fork_session,
            server_url=server_url,
        )

    async def ensure_pty(
        self, session_id: str, *, cols: int = 120, rows: int = 36
    ) -> dict[str, Any]:
        settings = self._require_enabled()
        rec = self.get_session(session_id)
        live = self._sessions.get(session_id)
        if live is None:
            raise RemoteNotFoundError(f"Active session {session_id!r} not found")
        if live.pty_session is not None:
            return rec

        argv = self._build_pty_argv(
            str(rec["harness"]),
            model=str(rec.get("model") or "") or None,
            external_session_id=str(rec.get("external_session_id") or "") or None,
            server_url=str(rec.get("server_url") or "") or None,
            fallback_shell=True,
        )
        env = self._build_pty_env(settings, session_id=session_id)
        pty_sess = PtySession()
        live.pty_session = pty_sess

        def _on_pty_exit(rc: int) -> None:
            if live.driver is None:
                self._store.update_session(session_id, status="exited", pid=None)
            asyncio.create_task(
                self._emit(session_id, "terminal.exited", {"returncode": rc})
            )

        await pty_sess.spawn(
            argv,
            cwd=str(rec["cwd"]),
            env=env,
            cols=cols,
            rows=rows,
            on_exit=_on_pty_exit,
        )
        return self.get_session(session_id)

    def _build_pty_argv(
        self,
        harness: str,
        *,
        model: str | None = None,
        prompt: str = "",
        external_session_id: str | None = None,
        server_url: str | None = None,
        fallback_shell: bool = False,
    ) -> list[str]:
        if harness == "terminal":
            return _default_shell_argv()
        spec = harness_spec(harness)
        binary = shutil.which(spec.binary)
        if binary is None:
            if fallback_shell:
                return _default_shell_argv()
            raise RemoteControlError(
                f"Harness binary {spec.binary!r} is not installed on PATH"
            )
        if harness in {"opencode", "kilo"} and server_url:
            argv = [binary, "attach", server_url]
            if external_session_id:
                argv.extend(["--session", external_session_id])
            return argv
        if harness == "opencode2" and server_url:
            argv = [binary, "--server", server_url]
            if external_session_id:
                argv.extend(["--session", external_session_id])
            return argv
        if harness == "codex" and external_session_id:
            return [binary, "resume", external_session_id]
        argv = [binary]
        if harness == "claude" and external_session_id:
            argv.extend(["--resume", external_session_id])
        if model and model.strip() and harness == "claude":
            argv.extend(["--model", model.strip()])
        if prompt.strip() and harness == "claude":
            argv.append(prompt.strip())
        return argv

    def _build_pty_env(self, settings: Settings, *, session_id: str) -> dict[str, str]:
        proxy_root = local_proxy_root_url(settings)
        token = settings.anthropic_auth_token
        env = build_claude_proxy_env(
            proxy_root_url=proxy_root,
            auth_token=token,
            base_env=os.environ,
        )
        env["OPENAI_BASE_URL"] = f"{proxy_root}/v1"
        env["OPENAI_API_KEY"] = proxy_auth_token(token)
        env["GEMINI_API_BASE_URL"] = proxy_root
        env["MCC_RC_SESSION_ID"] = session_id
        env["TERM"] = "xterm-256color"
        return env

    async def send_message(self, session_id: str, text: str) -> dict[str, Any]:
        self._require_enabled()
        live = self._sessions.get(session_id)
        if live is None:
            raise RemoteNotFoundError(f"Active session {session_id!r} not found")
        if live.driver is not None:
            await live.driver.send_user_message(text)
            return self.get_session(session_id)
        if live.pty_session is not None:
            await live.pty_session.write((text.rstrip("\r\n") + "\r").encode("utf-8"))
            return self.get_session(session_id)
        raise RemoteConflictError("Session has no active driver")

    async def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        *,
        decision: str,
        updated_input: Mapping[str, Any] | None = None,
        answers: Mapping[str, Any] | None = None,
        message: str = "",
    ) -> dict[str, Any]:
        self._require_enabled()
        live = self._sessions.get(session_id)
        if live is None or live.driver is None:
            raise RemoteNotFoundError(
                f"Active structured session {session_id!r} not found"
            )
        try:
            await live.driver.resolve_approval(
                approval_id,
                decision=decision,
                updated_input=updated_input,
                answers=answers,
                message=message,
            )
        except KeyError as exc:
            raise RemoteNotFoundError(f"Approval {approval_id!r} not pending") from exc
        status = (
            "approved"
            if decision in {"allow", "allow_once", "allow_always"}
            else "denied"
        )
        updated = self._store.resolve_approval(
            approval_id, status=status, decision=decision
        )
        return updated or {"id": approval_id, "status": status, "decision": decision}

    async def set_permission_mode(self, session_id: str, mode: str) -> dict[str, Any]:
        self._require_enabled()
        normalized = mode.strip().lower()
        if normalized not in RC_PERMISSION_MODE_VALUES:
            raise RemoteForbiddenError(f"Invalid permission mode: {mode!r}")
        live = self._sessions.get(session_id)
        if live is None:
            raise RemoteNotFoundError(f"Active session {session_id!r} not found")
        if live.driver is not None:
            await live.driver.set_permission_mode(normalized)
        else:
            self._store.update_session(session_id, permission_mode=normalized)
            await self._emit(
                session_id, "session.mode_changed", {"permission_mode": normalized}
            )
        return self.get_session(session_id)

    async def set_model(self, session_id: str, model: str) -> dict[str, Any]:
        self._require_enabled()
        live = self._sessions.get(session_id)
        if live is None:
            raise RemoteNotFoundError(f"Active session {session_id!r} not found")
        if live.driver is not None:
            await live.driver.set_model(model)
        else:
            self._store.update_session(session_id, model=model.strip())
        return self.get_session(session_id)

    async def interrupt_session(self, session_id: str) -> dict[str, Any]:
        self._require_enabled()
        live = self._sessions.get(session_id)
        if live is None:
            raise RemoteNotFoundError(f"Active session {session_id!r} not found")
        if live.driver is not None:
            await live.driver.interrupt()
        elif live.pty_session is not None:
            await live.pty_session.send_signal("SIGINT")
        return self.get_session(session_id)

    async def fork_session(self, session_id: str, *, title: str = "") -> dict[str, Any]:
        self._require_enabled()
        parent = self.get_session(session_id)
        ext_id = str(parent.get("external_session_id") or "").strip()
        if not ext_id:
            raise RemoteConflictError(
                "Parent session has no external_session_id yet to fork from"
            )
        live = self._sessions.get(session_id)
        if live is not None and live.driver is not None:
            native_fork = await live.driver.fork_session(title=title)
            if isinstance(native_fork, dict):
                thread_obj = native_fork.get("thread")
                forked_id = str(
                    native_fork.get("id")
                    or (thread_obj.get("id") if isinstance(thread_obj, dict) else "")
                    or ""
                )
                if forked_id:
                    return await self.create_session(
                        harness=str(parent["harness"]),
                        cwd=str(parent["cwd"]),
                        title=title or f"{parent['title']} (fork)",
                        permission_mode=str(parent["permission_mode"]),
                        model=str(parent.get("model") or ""),
                        external_session_id=forked_id,
                        fork_session=False,
                        server_url=str(parent.get("server_url") or "") or None,
                    )
        return await self.create_session(
            harness=str(parent["harness"]),
            cwd=str(parent["cwd"]),
            title=title or f"{parent['title']} (fork)",
            permission_mode=str(parent["permission_mode"]),
            model=str(parent.get("model") or ""),
            external_session_id=ext_id,
            fork_session=True,
        )

    async def get_diff(self, session_id: str) -> dict[str, Any]:
        self._require_enabled()
        rec = self.get_session(session_id)
        return await collect_git_diff(Path(rec["cwd"]))

    async def stop_session(self, session_id: str) -> dict[str, Any]:
        self.get_session(session_id)
        live = self._sessions.get(session_id)
        if live is not None:
            if live.driver is not None:
                await live.driver.close()
            if live.pty_session is not None:
                await live.pty_session.close()
        self._store.update_session(session_id, status="stopped", pid=None)
        await self._emit(session_id, "session.state", {"status": "stopped"})
        return self.get_session(session_id)

    async def delete_session(self, session_id: str) -> bool:
        self._require_enabled()
        live = self._sessions.pop(session_id, None)
        if live is not None:
            if live.driver is not None:
                await live.driver.close()
            if live.pty_session is not None:
                await live.pty_session.close()
            if live.repo_root is not None and live.worktree_path is not None:
                await remove_session_worktree(live.repo_root, live.worktree_path)
        return self._store.delete_session(session_id)

    async def pty_input(self, session_id: str, data: bytes) -> None:
        self._require_enabled()
        live = self._sessions.get(session_id)
        if live is None or live.pty_session is None:
            raise RemoteNotFoundError(f"PTY session {session_id!r} not found")
        await live.pty_session.write(data)

    async def pty_resize(self, session_id: str, cols: int, rows: int) -> None:
        self._require_enabled()
        live = self._sessions.get(session_id)
        if live is None or live.pty_session is None:
            return
        await live.pty_session.resize(cols, rows)

    def pty_scrollback(self, session_id: str) -> bytes:
        live = self._sessions.get(session_id)
        if live is None or live.pty_session is None:
            return b""
        return live.pty_session.scrollback_bytes()

    async def subscribe_events(
        self, session_id: str, *, since_seq: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        self.get_session(session_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        bucket = self._event_subs.setdefault(session_id, set())
        bucket.add(queue)
        try:
            last_seq = max(0, since_seq)
            backlog = self._store.list_events(
                session_id, since_seq=last_seq, limit=1000
            )
            for item in backlog:
                last_seq = max(last_seq, int(item["seq"]))
                yield item
            while True:
                event = await queue.get()
                if int(event["seq"]) <= last_seq:
                    continue
                last_seq = int(event["seq"])
                yield event
        finally:
            subs = self._event_subs.get(session_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    self._event_subs.pop(session_id, None)

    async def subscribe_pty(self, session_id: str) -> AsyncIterator[bytes]:
        live = self._sessions.get(session_id)
        if live is None or live.pty_session is None:
            raise RemoteNotFoundError(f"PTY session {session_id!r} not found")
        async for chunk in live.pty_session.subscribe():
            yield chunk

    async def close(self) -> None:
        for session_id in list(self._sessions):
            live = self._sessions.pop(session_id, None)
            if live is None:
                continue
            if live.driver is not None:
                with contextlib.suppress(Exception):
                    await live.driver.close()
            if live.pty_session is not None:
                with contextlib.suppress(Exception):
                    await live.pty_session.close()
        await self._tunnels.close(self.current_settings())
        self._store.close()
