"""Opt-in `--rc` live dual-sync terminal bridge for coding-agent launchers.

Strictly isolated from ``runtime`` at import time so ``cli`` obeys
``tests/contracts/test_import_boundaries.py`` and cold startup stays untouched.
When ``--rc`` is passed, this spawns the detached sidecar module as a child
process, registers it with ``mcc-server`` over loopback admin HTTP, and
mirrors bidirectional TTY/pipe I/O with the sidecar's authenticated TCP control
channel.
"""

import base64
import contextlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from my_claude_code.cli.process_registry import (
    kill_pid_tree_best_effort,
    register_pid,
    unregister_pid,
)
from my_claude_code.config.paths import CONFIG_DIR_ENV, config_dir_path
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

_READY_TIMEOUT_SECONDS = 10.0
_ADMIN_REGISTER_TIMEOUT_SECONDS = 3.0


def _inject_rc_session_env(
    base_env: Mapping[str, str], *, session_id: str
) -> dict[str, str]:
    env = dict(base_env)
    env["MCC_RC_SESSION_ID"] = session_id
    env.setdefault("TERM", "xterm-256color")
    existing_headers = env.get("ANTHROPIC_CUSTOM_HEADERS", "").strip()
    session_hdr = f"x-mcc-session: {session_id}"
    if session_hdr not in existing_headers:
        env["ANTHROPIC_CUSTOM_HEADERS"] = (
            f"{existing_headers}\n{session_hdr}" if existing_headers else session_hdr
        )
    return env


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    text = json.dumps(dict(payload))
    path.write_text(text, encoding="utf-8")
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def _wait_ready(
    ready_path: Path, proc: subprocess.Popen[bytes], *, timeout: float
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
                f"Sidecar exited prematurely with code {proc.returncode}"
            )
        time.sleep(0.025)
    raise TimeoutError("Timed out waiting for --rc sidecar ready signal")


def _register_with_server(
    proxy_root: str, payload: Mapping[str, Any]
) -> tuple[bool, str, str]:
    url = f"{proxy_root.rstrip('/')}/admin/api/rc/sidecars/register"
    body = json.dumps(dict(payload)).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=_ADMIN_REGISTER_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            rc_url = str(
                data.get("rc_url")
                or data.get("local_rc_url")
                or f"{proxy_root.rstrip('/')}/rc?session={payload['session_id']}"
            )
            return True, rc_url, ""
    except HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            err_body = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(err_body)
            if isinstance(parsed, dict) and parsed.get("detail"):
                detail = str(parsed["detail"])
        if exc.code == 503:
            msg = detail or (
                "Remote Control is disabled. Enable RC_ENABLED=true in Admin -> Remote Control."
            )
            return False, "", f"Remote Control disabled ({msg})"
        if exc.code == 403:
            msg = detail or (
                f"Add {payload.get('cwd')} to RC_ALLOWED_DIRS in Admin -> Remote Control."
            )
            return False, "", f"Registration refused ({msg})"
        return False, "", f"HTTP {exc.code}: {detail or exc.reason}"
    except (URLError, OSError, json.JSONDecodeError) as exc:
        return False, "", str(exc)


@contextlib.contextmanager
def _raw_tty_mode():
    is_tty = False
    with contextlib.suppress(Exception):
        is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    if not is_tty:
        yield False
        return

    if sys.platform != "win32":
        import termios
        import tty

        fd = sys.stdin.fileno()
        try:
            old_attrs = termios.tcgetattr(fd)
        except termios.error:
            yield False
            return
        try:
            tty.setraw(fd)
            yield True
        finally:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        return

    # Windows console raw input + VT output mode
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h_in = kernel32.GetStdHandle(-10)
    h_out = kernel32.GetStdHandle(-11)
    old_in = wintypes.DWORD(0)
    old_out = wintypes.DWORD(0)
    got_in = bool(kernel32.GetConsoleMode(h_in, ctypes.byref(old_in)))
    got_out = bool(kernel32.GetConsoleMode(h_out, ctypes.byref(old_out)))
    if not got_in:
        yield False
        return
    try:
        # Disable line input, echo, processed input; enable VT input
        new_in = (old_in.value & ~0x0007) | 0x0200
        kernel32.SetConsoleMode(h_in, new_in)
        if got_out:
            kernel32.SetConsoleMode(h_out, old_out.value | 0x0004)
        yield True
    finally:
        kernel32.SetConsoleMode(h_in, old_in.value)
        if got_out:
            kernel32.SetConsoleMode(h_out, old_out.value)


def _write_stdout_bytes(data: bytes) -> None:
    if not data:
        return
    out_buf = getattr(sys.stdout, "buffer", None)
    if out_buf is not None:
        out_buf.write(data)
        out_buf.flush()
    else:
        sys.stdout.write(data.decode("utf-8", errors="replace"))
        sys.stdout.flush()


def _pump_local_terminal(
    *,
    port: int,
    secret: str,
    initial_cols: int,
    initial_rows: int,
) -> int:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    sock.settimeout(None)
    send_lock = threading.Lock()
    stop_flag = threading.Event()
    last_size = [initial_cols, initial_rows]
    exit_code = 0

    def send_frame(frame: dict[str, Any]) -> None:
        raw = (json.dumps(frame) + "\n").encode("utf-8")
        with send_lock, contextlib.suppress(OSError):
            sock.sendall(raw)

    send_frame({"type": "auth", "secret": secret})
    reader_file = sock.makefile("rb")
    hello_raw = reader_file.readline()
    if not hello_raw:
        sock.close()
        return 1
    hello = json.loads(hello_raw.decode("utf-8", errors="replace"))
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        sock.close()
        return 1

    scroll_b64 = hello.get("scrollback_b64")
    if isinstance(scroll_b64, str) and scroll_b64:
        with contextlib.suppress(ValueError):
            _write_stdout_bytes(base64.b64decode(scroll_b64))
    if hello.get("running") is False:
        rc = int(hello.get("returncode") or 0)
        sock.close()
        return rc

    def _stdin_pump() -> None:
        try:
            stdin_fd = sys.stdin.fileno()
        except Exception:
            return
        while not stop_flag.is_set():
            try:
                chunk = os.read(stdin_fd, 1024)
            except OSError:
                break
            if not chunk:
                break
            send_frame(
                {
                    "type": "stdin",
                    "data_b64": base64.b64encode(chunk).decode("ascii"),
                }
            )

    def _resize_pump(is_tty: bool) -> None:
        if not is_tty:
            return
        while not stop_flag.wait(0.5):
            size = shutil.get_terminal_size((last_size[0], last_size[1]))
            if size.columns != last_size[0] or size.lines != last_size[1]:
                last_size[0] = size.columns
                last_size[1] = size.lines
                send_frame({"type": "resize", "cols": size.columns, "rows": size.lines})

    old_sigwinch = None
    with _raw_tty_mode() as is_tty:
        if is_tty and hasattr(signal, "SIGWINCH"):
            with contextlib.suppress(ValueError, OSError):
                old_sigwinch = signal.getsignal(signal.SIGWINCH)

                def _on_sigwinch(_signum: int, _frame: Any) -> None:
                    size = shutil.get_terminal_size((last_size[0], last_size[1]))
                    if size.columns != last_size[0] or size.lines != last_size[1]:
                        last_size[0] = size.columns
                        last_size[1] = size.lines
                        send_frame(
                            {
                                "type": "resize",
                                "cols": size.columns,
                                "rows": size.lines,
                            }
                        )

                signal.signal(signal.SIGWINCH, _on_sigwinch)

        stdin_thread = threading.Thread(
            target=_stdin_pump,
            name="mcc-rc-stdin-pump",
            daemon=True,
        )
        stdin_thread.start()
        resize_thread = threading.Thread(
            target=_resize_pump,
            args=(is_tty,),
            name="mcc-rc-resize-pump",
            daemon=True,
        )
        resize_thread.start()

        try:
            while True:
                line = reader_file.readline()
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
                            _write_stdout_bytes(base64.b64decode(raw_b64))
                elif ftype == "exited":
                    exit_code = int(frame.get("returncode") or 0)
                    break
        except KeyboardInterrupt:
            send_frame({"type": "signal", "sig": "SIGINT"})
            raise
        finally:
            stop_flag.set()
            if old_sigwinch is not None and hasattr(signal, "SIGWINCH"):
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(signal.SIGWINCH, old_sigwinch)
            with contextlib.suppress(Exception):
                send_frame({"type": "detach"})
            with contextlib.suppress(OSError):
                reader_file.close()
            with contextlib.suppress(OSError):
                sock.close()

    return exit_code


def run_rc_dual_sync_process(
    *,
    command: Sequence[str],
    env: Mapping[str, str],
    binary_name: str,
    display_name: str,
    install_hint: str,
    harness_id: str,
    cwd: str | None = None,
    proxy_root_url: str | None = None,
) -> None:
    """Launch a client CLI under a live dual-sync RC sidecar and mirror I/O."""

    cmd_list = list(command)
    if not cmd_list:
        raise SystemExit(1)

    work_dir = os.path.abspath(cwd or os.getcwd())
    session_id = f"rc_{secrets.token_hex(8)}"
    secret = secrets.token_hex(32)
    child_env = _inject_rc_session_env(env, session_id=session_id)

    proxy_root = (proxy_root_url or "").strip()
    if not proxy_root:
        proxy_root = local_proxy_root_url(get_settings())

    term_size = shutil.get_terminal_size((120, 36))
    cols = max(20, min(int(term_size.columns), 500))
    rows = max(5, min(int(term_size.lines), 200))

    cfg_dir = str(config_dir_path())
    fd_cfg, cfg_path_str = tempfile.mkstemp(
        prefix=f".mcc-rc-cli-{session_id}-",
        suffix=".json",
    )
    os.close(fd_cfg)
    cfg_path = Path(cfg_path_str)
    fd_ready, ready_path_str = tempfile.mkstemp(
        prefix=f".mcc-rc-cli-ready-{session_id}-",
        suffix=".json",
    )
    os.close(fd_ready)
    ready_path = Path(ready_path_str)
    ready_path.unlink(missing_ok=True)

    title = f"{harness_id} — {Path(work_dir).name} (Live CLI)"
    _write_private_json(
        cfg_path,
        {
            "session_id": session_id,
            "harness": harness_id,
            "cwd": work_dir,
            "title": title,
            "permission_mode": "ask",
            "driver_tier": "tier_3_pty",
            "origin": "cli_rc",
            "argv": cmd_list,
            "env": child_env,
            "cols": cols,
            "rows": rows,
            "secret": secret,
            "ready_file": str(ready_path),
            "unlink_config": True,
            "register_runtime": False,
            "mcc_config_dir": cfg_dir,
        },
    )

    spawn_env = dict(os.environ)
    spawn_env[CONFIG_DIR_ENV] = cfg_dir
    proc: subprocess.Popen[bytes] | None = None
    try:
        sidecar_cmd = [
            sys.executable,
            "-m",
            "my_claude_code.runtime.remote.sidecar",
            "--config-file",
            str(cfg_path),
        ]
        if sys.platform == "win32":
            creationflags = int(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            )
            started_proc = subprocess.Popen(
                sidecar_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=spawn_env,
                creationflags=creationflags,
            )
        else:
            started_proc = subprocess.Popen(
                sidecar_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=spawn_env,
                start_new_session=True,
                close_fds=True,
            )
        proc = started_proc
        if started_proc.pid:
            register_pid(started_proc.pid)
        ready = _wait_ready(ready_path, started_proc, timeout=_READY_TIMEOUT_SECONDS)
    except FileNotFoundError:
        print(
            f"Could not find {display_name} command: {binary_name}",
            file=sys.stderr,
        )
        print(install_hint, file=sys.stderr)
        raise SystemExit(127) from None
    except Exception as exc:
        cfg_path.unlink(missing_ok=True)
        ready_path.unlink(missing_ok=True)
        if proc is not None and proc.pid:
            kill_pid_tree_best_effort(proc.pid)
            unregister_pid(proc.pid)
        print(
            f"[MCC RC] Failed to start Remote Control PTY sidecar: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    finally:
        ready_path.unlink(missing_ok=True)

    sidecar_port = int(ready["port"])
    child_pid = ready.get("pid")
    sidecar_pid = int(ready.get("sidecar_pid") or started_proc.pid)

    registered, rc_url, err_msg = _register_with_server(
        proxy_root,
        {
            "session_id": session_id,
            "harness": harness_id,
            "cwd": work_dir,
            "host": "127.0.0.1",
            "port": sidecar_port,
            "secret": secret,
            "pid": child_pid,
            "sidecar_pid": sidecar_pid,
            "title": title,
            "permission_mode": "ask",
            "origin": "cli_rc",
        },
    )
    if registered:
        print(
            f"[MCC RC] Live Remote Control session {session_id} ready -> {rc_url}",
            file=sys.stderr,
        )
    else:
        print(
            f"[MCC RC] {err_msg}. Continuing local session without remote sync.",
            file=sys.stderr,
        )

    try:
        return_code = _pump_local_terminal(
            port=sidecar_port,
            secret=secret,
            initial_cols=cols,
            initial_rows=rows,
        )
    except KeyboardInterrupt:
        if proc is not None and proc.pid:
            kill_pid_tree_best_effort(proc.pid)
        raise
    finally:
        if proc is not None and proc.pid:
            unregister_pid(proc.pid)

    raise SystemExit(return_code)
