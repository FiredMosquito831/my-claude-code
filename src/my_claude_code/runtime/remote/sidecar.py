"""Detached PTY sidecar worker, loopback TCP control protocol, and rc_runtime.json registry."""

import argparse
import asyncio
import base64
import contextlib
import ctypes
import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from my_claude_code.cli.process_registry import kill_pid_tree_best_effort
from my_claude_code.config.paths import CONFIG_DIR_ENV, rc_runtime_path
from my_claude_code.runtime.remote.pty_bridge import PtySession
from my_claude_code.runtime.remote.store import _atomic_write_text

_SCROLLBACK_MAX_BYTES = 256 * 1024
_TCP_STREAM_LIMIT = 2 * 1024 * 1024
_AUTH_TIMEOUT_SECONDS = 5.0
_READY_TIMEOUT_SECONDS = 10.0
_LOCK_TIMEOUT_SECONDS = 3.0
_LOCK_STALE_SECONDS = 15.0

_RUNTIME_LOCK = threading.RLock()


# ------------------------------------------------------------ PID liveness --


def is_pid_alive(pid: int | None) -> bool:
    """Return True if *pid* refers to a currently running non-zombie process."""

    if pid is None or isinstance(pid, bool):
        return False
    try:
        pid_int = int(pid)
    except ValueError, TypeError:
        return False
    if pid_int <= 0:
        return False

    if sys.platform == "win32":
        return _is_pid_alive_windows(pid_int)

    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

    stat_path = Path(f"/proc/{pid_int}/stat")
    if stat_path.is_file():
        with contextlib.suppress(OSError):
            text = stat_path.read_text(encoding="utf-8", errors="replace")
            rparen = text.rfind(")")
            if rparen != -1:
                rest = text[rparen + 2 :].lstrip()
                if rest.startswith("Z"):
                    return False
    return True


def _is_pid_alive_windows(pid: int) -> bool:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query_limited = 0x1000
    error_access_denied = 5
    still_active = 259

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited, False, pid)
    if not handle:
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD(0)
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok and code.value == still_active)
    finally:
        kernel32.CloseHandle(handle)


# ------------------------------------------------------- rc_runtime.json I/O --


@contextlib.contextmanager
def _runtime_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    acquired_fd: int | None = None
    with _RUNTIME_LOCK:
        while True:
            try:
                acquired_fd = os.open(
                    str(lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                break
            except FileExistsError:
                with contextlib.suppress(OSError):
                    age = time.time() - lock_path.stat().st_mtime
                    if age > _LOCK_STALE_SECONDS:
                        lock_path.unlink()
                        continue
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.015)
            except OSError:
                break
        try:
            yield
        finally:
            if acquired_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(acquired_fd)
                with contextlib.suppress(OSError):
                    lock_path.unlink()


def _read_raw_runtime_locked(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    sidecars = raw.get("sidecars")
    if not isinstance(sidecars, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for sid, entry in sidecars.items():
        if isinstance(sid, str) and isinstance(entry, dict):
            out[sid] = dict(entry)
    return out


def _write_raw_runtime_locked(
    path: Path, sidecars: Mapping[str, Mapping[str, Any]]
) -> None:
    payload = (
        json.dumps(
            {"sidecars": dict(sidecars)},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    for attempt in range(4):
        try:
            _atomic_write_text(path, payload, private=True)
            return
        except PermissionError:
            if attempt >= 3:
                raise
            time.sleep(0.02 * (attempt + 1))


def _entry_is_alive(entry: Mapping[str, Any]) -> bool:
    sidecar_pid = entry.get("sidecar_pid")
    child_pid = entry.get("pid")
    if not is_pid_alive(sidecar_pid):
        return False
    return not (child_pid is not None and not is_pid_alive(child_pid))


def read_rc_runtime(*, prune_stale: bool = True) -> dict[str, dict[str, Any]]:
    """Read active sidecar entries from ``rc_runtime.json``, optionally pruning dead PIDs."""

    path = rc_runtime_path()
    with _runtime_file_lock(path):
        entries = _read_raw_runtime_locked(path)
        if not prune_stale or not entries:
            return entries
        alive = {sid: rec for sid, rec in entries.items() if _entry_is_alive(rec)}
        if len(alive) != len(entries):
            _write_raw_runtime_locked(path, alive)
        return alive


def upsert_rc_runtime_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Insert or update one sidecar entry in ``rc_runtime.json`` (0o600, atomic)."""

    session_id = str(entry.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("session_id is required in rc_runtime entry")
    path = rc_runtime_path()
    record = dict(entry)
    with _runtime_file_lock(path):
        entries = _read_raw_runtime_locked(path)
        alive = {sid: rec for sid, rec in entries.items() if _entry_is_alive(rec)}
        alive[session_id] = record
        _write_raw_runtime_locked(path, alive)
    return record


def remove_rc_runtime_entry(session_id: str) -> bool:
    """Remove *session_id* from ``rc_runtime.json`` if present."""

    path = rc_runtime_path()
    with _runtime_file_lock(path):
        entries = _read_raw_runtime_locked(path)
        if session_id not in entries:
            return False
        entries.pop(session_id, None)
        _write_raw_runtime_locked(path, entries)
    return True


# --------------------------------------------------------- Sidecar Server --


class SidecarServer:
    """Owns one ``PtySession`` and serves an authenticated loopback TCP control channel."""

    def __init__(
        self,
        *,
        session_id: str,
        harness: str,
        cwd: str,
        title: str,
        permission_mode: str,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        secret: str,
        cols: int = 120,
        rows: int = 36,
        model: str = "",
        driver_tier: str = "tier_3_pty",
        origin: str = "rc_server",
        register_runtime: bool = True,
    ) -> None:
        self._session_id = session_id
        self._harness = harness
        self._cwd = cwd
        self._title = title
        self._permission_mode = permission_mode
        self._argv = list(argv)
        self._env = dict(os.environ if env is None else env)
        self._secret = secret
        self._cols = cols
        self._rows = rows
        self._model = model
        self._driver_tier = driver_tier
        self._origin = origin
        self._register_runtime = register_runtime

        self._pty = PtySession()
        self._running = False
        self._returncode: int | None = None
        self._scrollback_buf: deque[bytes] = deque()
        self._scrollback_size = 0
        self._clients: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._server: asyncio.Server | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._stopping = False
        self._port: int = 0

    @property
    def port(self) -> int:
        return self._port

    @property
    def child_pid(self) -> int | None:
        return self._pty.pid

    def _append_scrollback(self, chunk: bytes) -> None:
        self._scrollback_buf.append(chunk)
        self._scrollback_size += len(chunk)
        while self._scrollback_size > _SCROLLBACK_MAX_BYTES and self._scrollback_buf:
            dropped = self._scrollback_buf.popleft()
            self._scrollback_size -= len(dropped)

    def _broadcast_frame(self, frame: dict[str, Any]) -> None:
        for queue in list(self._clients):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(frame)

    def _finish_all_clients(self) -> None:
        for queue in list(self._clients):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)

    def _on_pty_exit(self, rc: int) -> None:
        self._running = False
        self._returncode = int(rc)
        if rc_runtime_path().is_file():
            with contextlib.suppress(OSError):
                remove_rc_runtime_entry(self._session_id)
        self._broadcast_frame({"type": "exited", "returncode": int(rc)})
        loop = asyncio.get_running_loop()
        loop.call_later(0.35, self._stop_event.set)

    async def _pump_pty(self) -> None:
        async for chunk in self._pty.subscribe():
            if not chunk:
                continue
            self._append_scrollback(chunk)
            self._broadcast_frame(
                {
                    "type": "output",
                    "data_b64": base64.b64encode(chunk).decode("ascii"),
                }
            )

    async def start(self) -> int:
        self._running = True
        await self._pty.spawn(
            self._argv,
            cwd=self._cwd,
            env=self._env,
            cols=self._cols,
            rows=self._rows,
            on_exit=self._on_pty_exit,
        )
        self._pump_task = asyncio.create_task(self._pump_pty())
        await asyncio.sleep(0)
        self._server = await asyncio.start_server(
            self._handle_client,
            "127.0.0.1",
            0,
            limit=_TCP_STREAM_LIMIT,
        )
        sockets = self._server.sockets
        if not sockets:
            raise RuntimeError("Sidecar failed to bind loopback TCP socket")
        self._port = int(sockets[0].getsockname()[1])

        if self._register_runtime:
            upsert_rc_runtime_entry(
                {
                    "session_id": self._session_id,
                    "harness": self._harness,
                    "cwd": self._cwd,
                    "title": self._title,
                    "permission_mode": self._permission_mode,
                    "model": self._model,
                    "driver_tier": self._driver_tier,
                    "pid": self._pty.pid,
                    "sidecar_pid": os.getpid(),
                    "host": "127.0.0.1",
                    "port": self._port,
                    "secret": self._secret,
                    "started_at": time.time(),
                    "origin": self._origin,
                }
            )
        return self._port

    def _verify_auth(self, msg: Any) -> bool:
        if not isinstance(msg, dict) or msg.get("type") != "auth":
            return False
        provided = msg.get("secret")
        if not isinstance(provided, str) or not provided:
            return False
        return hmac.compare_digest(provided, self._secret)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        out_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=4096)
        writer_task: asyncio.Task[None] | None = None
        try:
            try:
                raw_auth = await asyncio.wait_for(
                    reader.readline(), timeout=_AUTH_TIMEOUT_SECONDS
                )
            except TimeoutError, asyncio.LimitOverrunError:
                return
            if not raw_auth:
                return
            try:
                auth_msg = json.loads(raw_auth.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return

            if not self._verify_auth(auth_msg):
                writer.write(b'{"type":"error","error":"unauthorized"}\n')
                with contextlib.suppress(OSError):
                    await writer.drain()
                return

            initial_bytes = b"".join(self._scrollback_buf)
            self._clients.add(out_queue)
            hello = {
                "type": "hello",
                "session_id": self._session_id,
                "pid": self._pty.pid,
                "sidecar_pid": os.getpid(),
                "running": self._running,
                "returncode": self._returncode,
                "scrollback_b64": base64.b64encode(initial_bytes).decode("ascii"),
            }
            writer.write((json.dumps(hello) + "\n").encode("utf-8"))
            await writer.drain()

            async def _drain_out() -> None:
                while True:
                    frame = await out_queue.get()
                    if frame is None:
                        break
                    writer.write((json.dumps(frame) + "\n").encode("utf-8"))
                    await writer.drain()

            writer_task = asyncio.create_task(_drain_out())

            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    cmd = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(cmd, dict):
                    continue
                ctype = str(cmd.get("type") or "")
                if ctype == "stdin":
                    raw_b64 = cmd.get("data_b64")
                    if isinstance(raw_b64, str) and raw_b64:
                        with contextlib.suppress(ValueError):
                            data = base64.b64decode(raw_b64)
                            await self._pty.write(data)
                elif ctype == "resize":
                    cols = int(cmd.get("cols") or 120)
                    rows = int(cmd.get("rows") or 36)
                    await self._pty.resize(cols, rows)
                elif ctype == "signal":
                    sig_name = str(cmd.get("sig") or "SIGINT")
                    await self._pty.send_signal(sig_name)
                elif ctype == "ping":
                    with contextlib.suppress(asyncio.QueueFull):
                        out_queue.put_nowait({"type": "pong"})
                elif ctype == "detach":
                    writer.write(b'{"type":"detached"}\n')
                    with contextlib.suppress(OSError):
                        await writer.drain()
                    break
                elif ctype == "stop":
                    if not self._stopping and self._stop_task is None:
                        self._stop_task = asyncio.create_task(self.stop())
                    break
        except ConnectionError, OSError, asyncio.CancelledError:
            pass
        finally:
            self._clients.discard(out_queue)
            with contextlib.suppress(asyncio.QueueFull):
                out_queue.put_nowait(None)
            if writer_task is not None:
                writer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await writer_task
            writer.close()
            with contextlib.suppress(OSError, Exception):
                await writer.wait_closed()

    async def wait_until_stopped(self) -> None:
        await self._stop_event.wait()

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._running = False
        if rc_runtime_path().is_file():
            with contextlib.suppress(OSError):
                remove_rc_runtime_entry(self._session_id)
        with contextlib.suppress(Exception):
            await self._pty.close()
        if self._returncode is None:
            self._returncode = (
                self._pty.exit_code if self._pty.exit_code is not None else 0
            )
        self._broadcast_frame({"type": "exited", "returncode": self._returncode})
        await asyncio.sleep(0.05)
        self._finish_all_clients()
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=0.5)
        self._stop_event.set()


# --------------------------------------------------------- Sidecar Client --


class SidecarPtyClient:
    """Async client proxy matching ``PtySession`` over authenticated loopback TCP."""

    is_detached: bool = True

    def __init__(
        self,
        *,
        session_id: str,
        host: str,
        port: int,
        secret: str,
        pid: int | None,
        sidecar_pid: int | None,
        on_exit: Callable[[int], None] | None = None,
    ) -> None:
        self._session_id = session_id
        self._host = host
        self._port = port
        self._secret = secret
        self._pid = pid
        self._sidecar_pid = sidecar_pid
        self._on_exit = on_exit

        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._exit_code: int | None = None
        self._closed = False
        self._detached_by_us = False
        self._scrollback: deque[bytes] = deque()
        self._scrollback_bytes = 0
        self._subscribers: set[asyncio.Queue[bytes | None]] = set()
        self._lock = threading.Lock()
        self._write_lock = asyncio.Lock()

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def sidecar_pid(self) -> int | None:
        return self._sidecar_pid

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def secret(self) -> str:
        return self._secret

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    def scrollback_bytes(self) -> bytes:
        with self._lock:
            return b"".join(self._scrollback)

    def _append_scrollback(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._scrollback.append(chunk)
            self._scrollback_bytes += len(chunk)
            while self._scrollback_bytes > _SCROLLBACK_MAX_BYTES and self._scrollback:
                dropped = self._scrollback.popleft()
                self._scrollback_bytes -= len(dropped)
            subs = list(self._subscribers)
        for queue in subs:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(chunk)

    def _finish_subscribers(self) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for queue in subs:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)

    async def subscribe(self) -> AsyncIterator[bytes]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        with self._lock:
            initial = b"".join(self._scrollback)
            already_closed = self._closed
            if not already_closed:
                self._subscribers.add(queue)
        if initial:
            yield initial
        if already_closed:
            return
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            with self._lock:
                self._subscribers.discard(queue)

    @classmethod
    async def connect(
        cls,
        *,
        session_id: str,
        host: str,
        port: int,
        secret: str,
        sidecar_pid: int | None = None,
        on_exit: Callable[[int], None] | None = None,
        timeout: float = 5.0,
    ) -> SidecarPtyClient:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port), limit=_TCP_STREAM_LIMIT),
            timeout=timeout,
        )
        try:
            auth_frame = json.dumps({"type": "auth", "secret": secret}) + "\n"
            writer.write(auth_frame.encode("utf-8"))
            await writer.drain()
            raw_hello = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not raw_hello:
                raise ConnectionError("Sidecar closed connection before hello")
            hello = json.loads(raw_hello.decode("utf-8", errors="replace"))
            if not isinstance(hello, dict) or hello.get("type") != "hello":
                detail = (
                    hello.get("error") if isinstance(hello, dict) else "invalid hello"
                )
                raise PermissionError(f"Sidecar authentication failed: {detail}")
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise

        client = cls(
            session_id=session_id,
            host=host,
            port=int(port),
            secret=secret,
            pid=hello.get("pid"),
            sidecar_pid=hello.get("sidecar_pid") or sidecar_pid,
            on_exit=on_exit,
        )
        client._loop = asyncio.get_running_loop()
        client._reader = reader
        client._writer = writer
        raw_scrollback = hello.get("scrollback_b64")
        if isinstance(raw_scrollback, str) and raw_scrollback:
            with contextlib.suppress(ValueError):
                client._append_scrollback(base64.b64decode(raw_scrollback))
        if hello.get("running") is False:
            client._closed = True
            client._exit_code = hello.get("returncode")
        else:
            client._read_task = asyncio.create_task(client._read_loop())
        return client

    @classmethod
    async def spawn(
        cls,
        *,
        session_id: str,
        harness: str,
        cwd: str,
        title: str,
        permission_mode: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        cols: int = 120,
        rows: int = 36,
        model: str = "",
        driver_tier: str = "tier_3_pty",
        origin: str = "rc_server",
        on_exit: Callable[[int], None] | None = None,
    ) -> SidecarPtyClient:
        secret = secrets.token_hex(32)
        config_dir = str(rc_runtime_path().parent)
        fd_cfg, cfg_path_str = tempfile.mkstemp(
            prefix=f".rc-sidecar-{session_id}-",
            suffix=".json",
        )
        os.close(fd_cfg)
        cfg_path = Path(cfg_path_str)
        fd_ready, ready_path_str = tempfile.mkstemp(
            prefix=f".rc-ready-{session_id}-",
            suffix=".json",
        )
        os.close(fd_ready)
        ready_path = Path(ready_path_str)
        ready_path.unlink(missing_ok=True)

        cfg_payload = {
            "session_id": session_id,
            "harness": harness,
            "cwd": cwd,
            "title": title,
            "permission_mode": permission_mode,
            "model": model,
            "driver_tier": driver_tier,
            "origin": origin,
            "argv": list(argv),
            "env": dict(env),
            "cols": int(cols),
            "rows": int(rows),
            "secret": secret,
            "ready_file": str(ready_path),
            "unlink_config": True,
            "register_runtime": True,
            "mcc_config_dir": config_dir,
        }
        _atomic_write_text(cfg_path, json.dumps(cfg_payload), private=True)

        spawn_env = dict(os.environ)
        spawn_env[CONFIG_DIR_ENV] = config_dir

        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": spawn_env,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = int(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            )
        else:
            popen_kwargs["start_new_session"] = True
            popen_kwargs["close_fds"] = True

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "my_claude_code.runtime.remote.sidecar",
                "--config-file",
                str(cfg_path),
            ],
            **popen_kwargs,
        )

        try:
            ready = await _wait_for_ready_file(
                ready_path, proc, timeout=_READY_TIMEOUT_SECONDS
            )
        except BaseException:
            cfg_path.unlink(missing_ok=True)
            ready_path.unlink(missing_ok=True)
            if proc.poll() is None:
                kill_pid_tree_best_effort(proc.pid)
            raise
        finally:
            ready_path.unlink(missing_ok=True)

        sidecar_pid = int(ready.get("sidecar_pid") or proc.pid)
        port = int(ready["port"])
        return await cls.connect(
            session_id=session_id,
            host="127.0.0.1",
            port=port,
            secret=secret,
            sidecar_pid=sidecar_pid,
            on_exit=on_exit,
        )

    async def _read_loop(self) -> None:
        reader = self._reader
        if reader is None:
            return
        saw_exit = False
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                ftype = str(frame.get("type") or "")
                if ftype == "output":
                    raw_b64 = frame.get("data_b64")
                    if isinstance(raw_b64, str) and raw_b64:
                        with contextlib.suppress(ValueError):
                            self._append_scrollback(base64.b64decode(raw_b64))
                elif ftype == "exited":
                    saw_exit = True
                    rc = int(frame.get("returncode") or 0)
                    self._exit_code = rc
                    self._closed = True
                    self._finish_subscribers()
                    if self._on_exit is not None:
                        self._on_exit(rc)
                    break
        except ConnectionError, OSError, asyncio.CancelledError:
            pass
        finally:
            if not self._detached_by_us and not saw_exit and not self._closed:
                self._closed = True
                self._finish_subscribers()
                if not is_pid_alive(self._sidecar_pid):
                    self._exit_code = -1
                    if self._on_exit is not None:
                        self._on_exit(-1)

    def _loop_matches(self) -> bool:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return False
        return (
            self._loop is not None
            and not self._loop.is_closed()
            and self._loop is running
        )

    async def _send_oneshot_frame(self, frame: dict[str, Any]) -> None:
        if not self._host or not self._port or not self._secret:
            return
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                self._host, int(self._port), limit=_TCP_STREAM_LIMIT
            ),
            timeout=1.0,
        )
        try:
            auth_bytes = (
                json.dumps({"type": "auth", "secret": self._secret}) + "\n"
            ).encode("utf-8")
            writer.write(auth_bytes)
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=1.0)
            writer.write((json.dumps(frame) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(OSError, Exception):
                await writer.wait_closed()

    async def _send_frame(self, frame: dict[str, Any]) -> None:
        if self._closed or self._detached_by_us:
            return
        if not self._loop_matches():
            with contextlib.suppress(OSError, ConnectionError, TimeoutError):
                await self._send_oneshot_frame(frame)
            return
        writer = self._writer
        if writer is None:
            return
        data = (json.dumps(frame) + "\n").encode("utf-8")
        async with self._write_lock:
            if self._writer is None or self._closed or self._detached_by_us:
                return
            writer.write(data)
            with contextlib.suppress(OSError, ConnectionError):
                await writer.drain()

    async def write(self, data: bytes) -> None:
        if not data or self._closed:
            return
        await self._send_frame(
            {"type": "stdin", "data_b64": base64.b64encode(data).decode("ascii")}
        )

    async def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        await self._send_frame({"type": "resize", "cols": int(cols), "rows": int(rows)})

    async def send_signal(self, sig_name: str = "SIGINT") -> None:
        if self._closed:
            return
        await self._send_frame({"type": "signal", "sig": sig_name})

    async def detach(self) -> None:
        """Close the control socket WITHOUT stopping the sidecar or child process."""

        if self._detached_by_us:
            return
        self._detached_by_us = True
        same_loop = self._loop_matches()
        writer = self._writer if same_loop else None
        if writer is not None:
            with contextlib.suppress(OSError, ConnectionError, TimeoutError):
                writer.write(b'{"type":"detach"}\n')
                await asyncio.wait_for(writer.drain(), timeout=0.5)
        if same_loop and self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
        self._finish_subscribers()
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError, Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
        self._writer = None
        self._reader = None
        self._read_task = None

    async def close(self) -> None:
        """Explicitly stop the sidecar and child process."""

        same_loop = self._loop_matches()
        if not self._closed:
            with contextlib.suppress(OSError, ConnectionError, TimeoutError):
                await asyncio.wait_for(self._send_frame({"type": "stop"}), timeout=1.0)
        self._closed = True
        if same_loop and self._read_task is not None:
            try:
                await asyncio.wait_for(self._read_task, timeout=2.5)
            except TimeoutError, asyncio.CancelledError, Exception:
                self._read_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._read_task
        self._finish_subscribers()
        writer = self._writer if same_loop else None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError, Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
        self._writer = None
        self._reader = None
        self._read_task = None
        with contextlib.suppress(OSError):
            remove_rc_runtime_entry(self._session_id)
        deadline = time.monotonic() + 0.8
        while is_pid_alive(self._sidecar_pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if is_pid_alive(self._pid):
            await asyncio.to_thread(kill_pid_tree_best_effort, int(self._pid or 0))
        if is_pid_alive(self._sidecar_pid):
            await asyncio.to_thread(
                kill_pid_tree_best_effort, int(self._sidecar_pid or 0)
            )


async def _wait_for_ready_file(
    ready_path: Path, proc: subprocess.Popen[Any], *, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_path.is_file():
            with contextlib.suppress(OSError, json.JSONDecodeError):
                text = ready_path.read_text(encoding="utf-8").strip()
                if text:
                    data = json.loads(text)
                    if isinstance(data, dict) and data.get("port"):
                        return data
        if proc.poll() is not None:
            raise RuntimeError(
                f"Detached sidecar exited prematurely with code {proc.returncode}"
            )
        await asyncio.sleep(0.025)
    raise TimeoutError("Timed out waiting for detached sidecar ready signal")


# --------------------------------------------------------- CLI Entrypoint --


def _load_sidecar_config(args: argparse.Namespace) -> dict[str, Any]:
    if args.config_file:
        cfg_path = Path(args.config_file)
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        if data.get("unlink_config"):
            with contextlib.suppress(OSError):
                cfg_path.unlink()
        if not isinstance(data, dict):
            raise ValueError("Sidecar config must be a JSON object")
        return data

    if args.config_stdin:
        raw = sys.stdin.read()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Sidecar stdin config must be a JSON object")
        return data

    if not args.secret_file:
        raise ValueError("--config-file, --config-stdin, or --secret-file is required")
    secret_path = Path(args.secret_file)
    secret = secret_path.read_text(encoding="utf-8").strip()
    if args.unlink_secret:
        with contextlib.suppress(OSError):
            secret_path.unlink()
    if not secret:
        raise ValueError("Sidecar secret file is empty")
    argv = list(args.command or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise ValueError("Child command argv is required")
    return {
        "session_id": args.session_id,
        "harness": args.harness,
        "cwd": args.cwd,
        "title": args.title or f"{args.harness} — {Path(args.cwd).name}",
        "permission_mode": args.permission_mode,
        "cols": args.cols,
        "rows": args.rows,
        "secret": secret,
        "ready_file": args.ready_file or "",
        "register_runtime": True,
        "argv": argv,
    }


async def _run_sidecar(cfg: dict[str, Any]) -> int:
    if cfg.get("mcc_config_dir"):
        os.environ[CONFIG_DIR_ENV] = str(cfg["mcc_config_dir"])

    server = SidecarServer(
        session_id=str(cfg["session_id"]),
        harness=str(cfg.get("harness") or "terminal"),
        cwd=str(cfg["cwd"]),
        title=str(cfg.get("title") or cfg["session_id"]),
        permission_mode=str(cfg.get("permission_mode") or "ask"),
        argv=list(cfg["argv"]),
        env=cfg.get("env"),
        secret=str(cfg["secret"]),
        cols=int(cfg.get("cols") or 120),
        rows=int(cfg.get("rows") or 36),
        model=str(cfg.get("model") or ""),
        driver_tier=str(cfg.get("driver_tier") or "tier_3_pty"),
        origin=str(cfg.get("origin") or "rc_server"),
        register_runtime=bool(cfg.get("register_runtime", True)),
    )
    port = await server.start()
    ready_payload = {
        "port": port,
        "pid": server.child_pid,
        "sidecar_pid": os.getpid(),
    }
    ready_file = str(cfg.get("ready_file") or "").strip()
    if ready_file:
        _atomic_write_text(
            Path(ready_file), json.dumps(ready_payload) + "\n", private=True
        )
    else:
        sys.stdout.write(json.dumps(ready_payload) + "\n")
        sys.stdout.flush()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(server.stop()))

    await server.wait_until_stopped()
    await server.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="my_claude_code.runtime.remote.sidecar",
        description="Detached Remote Control PTY sidecar worker.",
    )
    parser.add_argument("--config-file", default="")
    parser.add_argument("--config-stdin", action="store_true")
    parser.add_argument("--secret-file", default="")
    parser.add_argument("--unlink-secret", action="store_true")
    parser.add_argument("--ready-file", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--harness", default="terminal")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--title", default="")
    parser.add_argument("--permission-mode", default="ask")
    parser.add_argument("--cols", type=int, default=120)
    parser.add_argument("--rows", type=int, default=36)
    parser.add_argument("command", nargs=argparse.REMAINDER)

    args = parser.parse_args(list(argv) if argv is not None else None)
    cfg = _load_sidecar_config(args)
    return asyncio.run(_run_sidecar(cfg))


if __name__ == "__main__":
    raise SystemExit(main())
