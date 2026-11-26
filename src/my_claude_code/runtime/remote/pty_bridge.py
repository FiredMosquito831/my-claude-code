"""Cross-platform interactive PTY bridge (Windows ctypes ConPTY + POSIX pty)."""

import asyncio
import contextlib
import ctypes
import os
import subprocess
import sys
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

if sys.platform != "win32":
    import fcntl
    import pty
    import signal
    import struct
    import termios

_SCROLLBACK_MAX_BYTES = 256 * 1024
_READ_CHUNK_SIZE = 4096


def win_exec_argv(argv: Sequence[str]) -> list[str]:
    """Wrap ``.cmd`` / ``.bat`` shims with ``cmd.exe /c`` for CreateProcess on Windows."""

    items = list(argv)
    if (
        sys.platform == "win32"
        and items
        and items[0].lower().endswith((".cmd", ".bat"))
    ):
        return ["cmd.exe", "/c", *items]
    return items


if sys.platform == "win32":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _INFINITE = 0xFFFFFFFF

    class _COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_int16), ("Y", ctypes.c_int16)]

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", _STARTUPINFOW),
            ("lpAttributeList", wintypes.LPVOID),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    _kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wintypes.DWORD,
    ]
    _kernel32.CreatePipe.restype = wintypes.BOOL

    _kernel32.CreatePseudoConsole.argtypes = [
        _COORD,
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _kernel32.CreatePseudoConsole.restype = ctypes.c_long

    _kernel32.ResizePseudoConsole.argtypes = [wintypes.HANDLE, _COORD]
    _kernel32.ResizePseudoConsole.restype = ctypes.c_long

    _kernel32.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
    _kernel32.ClosePseudoConsole.restype = None

    _kernel32.InitializeProcThreadAttributeList.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL

    _kernel32.UpdateProcThreadAttribute.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_size_t,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    _kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL

    _kernel32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
    _kernel32.DeleteProcThreadAttributeList.restype = None

    _kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOEXW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    _kernel32.CreateProcessW.restype = wintypes.BOOL

    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL

    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD

    _kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL


def _build_win_env_block(env: Mapping[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    pairs = [
        f"{k}={v}"
        for k, v in sorted(env.items())
        if "\x00" not in k and "\x00" not in v and "=" not in k
    ]
    block = "\x00".join(pairs) + "\x00\x00"
    return ctypes.create_unicode_buffer(block)


class PtySession:
    """Async cross-platform interactive PTY session with scrollback replay."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pid: int | None = None
        self._closed = False
        self._exit_code: int | None = None
        self._on_exit: Callable[[int], None] | None = None
        self._scrollback: deque[bytes] = deque()
        self._scrollback_bytes = 0
        self._subscribers: set[asyncio.Queue[bytes | None]] = set()
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._waiter_thread: threading.Thread | None = None

        # Windows ConPTY handles
        self._hpc: Any = None
        self._in_write: Any = None
        self._out_read: Any = None
        self._h_process: Any = None

        # POSIX handles
        self._master_fd: int | None = None
        self._posix_proc: subprocess.Popen[bytes] | None = None

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    def scrollback_bytes(self) -> bytes:
        with self._lock:
            return b"".join(self._scrollback)

    def _append_output(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._scrollback.append(chunk)
            self._scrollback_bytes += len(chunk)
            while self._scrollback_bytes > _SCROLLBACK_MAX_BYTES and self._scrollback:
                dropped = self._scrollback.popleft()
                self._scrollback_bytes -= len(dropped)
            subs = list(self._subscribers)
        loop = self._loop
        if loop is not None and not loop.is_closed():
            for queue in subs:
                loop.call_soon_threadsafe(queue.put_nowait, chunk)

    def _finish_subscribers(self) -> None:
        with self._lock:
            subs = list(self._subscribers)
        loop = self._loop
        if loop is not None and not loop.is_closed():
            for queue in subs:
                loop.call_soon_threadsafe(queue.put_nowait, None)

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

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        cols: int = 120,
        rows: int = 36,
        on_exit: Callable[[int], None] | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._on_exit = on_exit
        merged_env = dict(os.environ if env is None else env)
        merged_env.setdefault("TERM", "xterm-256color")
        merged_env.setdefault("COLORTERM", "truecolor")
        bounded_cols = max(20, min(int(cols), 500))
        bounded_rows = max(5, min(int(rows), 200))

        if sys.platform == "win32":
            await asyncio.to_thread(
                self._spawn_windows,
                list(argv),
                cwd,
                merged_env,
                bounded_cols,
                bounded_rows,
            )
        else:
            await asyncio.to_thread(
                self._spawn_posix,
                list(argv),
                cwd,
                merged_env,
                bounded_cols,
                bounded_rows,
            )

    def _spawn_windows(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        cols: int,
        rows: int,
    ) -> None:
        assert sys.platform == "win32"
        sa = _SECURITY_ATTRIBUTES(
            nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
            lpSecurityDescriptor=None,
            bInheritHandle=False,
        )
        in_read = wintypes.HANDLE()
        in_write = wintypes.HANDLE()
        out_read = wintypes.HANDLE()
        out_write = wintypes.HANDLE()

        if not _kernel32.CreatePipe(
            ctypes.byref(in_read), ctypes.byref(in_write), ctypes.byref(sa), 0
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not _kernel32.CreatePipe(
            ctypes.byref(out_read), ctypes.byref(out_write), ctypes.byref(sa), 0
        ):
            _kernel32.CloseHandle(in_read)
            _kernel32.CloseHandle(in_write)
            raise ctypes.WinError(ctypes.get_last_error())

        hpc = wintypes.HANDLE()
        hr = _kernel32.CreatePseudoConsole(
            _COORD(cols, rows), in_read, out_write, 0, ctypes.byref(hpc)
        )
        # Close child-side pipe ends in parent immediately so EOF works reliably.
        _kernel32.CloseHandle(in_read)
        _kernel32.CloseHandle(out_write)
        if hr != 0:
            _kernel32.CloseHandle(in_write)
            _kernel32.CloseHandle(out_read)
            raise OSError(
                f"CreatePseudoConsole failed with HRESULT 0x{hr & 0xFFFFFFFF:08x}"
            )

        attr_size = ctypes.c_size_t(0)
        _kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
        attr_buf = (ctypes.c_ubyte * attr_size.value)()
        if not _kernel32.InitializeProcThreadAttributeList(
            attr_buf, 1, 0, ctypes.byref(attr_size)
        ):
            _kernel32.ClosePseudoConsole(hpc)
            _kernel32.CloseHandle(in_write)
            _kernel32.CloseHandle(out_read)
            raise ctypes.WinError(ctypes.get_last_error())

        if not _kernel32.UpdateProcThreadAttribute(
            attr_buf,
            0,
            _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            wintypes.LPVOID(hpc.value),
            ctypes.sizeof(wintypes.HANDLE),
            None,
            None,
        ):
            _kernel32.DeleteProcThreadAttributeList(attr_buf)
            _kernel32.ClosePseudoConsole(hpc)
            _kernel32.CloseHandle(in_write)
            _kernel32.CloseHandle(out_read)
            raise ctypes.WinError(ctypes.get_last_error())

        si_ex = _STARTUPINFOEXW()
        si_ex.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        # Clear inherited redirected parent pipes so ConPTY owns child stdio.
        si_ex.StartupInfo.dwFlags = 0x00000100
        si_ex.lpAttributeList = ctypes.cast(attr_buf, wintypes.LPVOID)
        pi = _PROCESS_INFORMATION()

        cmdline = subprocess.list2cmdline(win_exec_argv(argv))
        cmd_buf = ctypes.create_unicode_buffer(cmdline)
        env_buf = _build_win_env_block(env)

        ok = _kernel32.CreateProcessW(
            None,
            cmd_buf,
            None,
            None,
            False,
            _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
            env_buf,
            cwd,
            ctypes.byref(si_ex),
            ctypes.byref(pi),
        )
        _kernel32.DeleteProcThreadAttributeList(attr_buf)
        if not ok:
            err = ctypes.get_last_error()
            _kernel32.ClosePseudoConsole(hpc)
            _kernel32.CloseHandle(in_write)
            _kernel32.CloseHandle(out_read)
            raise ctypes.WinError(err)

        _kernel32.CloseHandle(pi.hThread)
        self._hpc = hpc
        self._in_write = in_write
        self._out_read = out_read
        self._h_process = pi.hProcess
        self._pid = int(pi.dwProcessId)

        self._reader_thread = threading.Thread(
            target=self._read_loop_windows,
            name=f"rc-conpty-read-{self._pid}",
            daemon=True,
        )
        self._waiter_thread = threading.Thread(
            target=self._wait_loop_windows,
            name=f"rc-conpty-wait-{self._pid}",
            daemon=True,
        )
        self._reader_thread.start()
        self._waiter_thread.start()

    def _read_loop_windows(self) -> None:
        assert sys.platform == "win32"
        buf = ctypes.create_string_buffer(_READ_CHUNK_SIZE)
        bytes_read = wintypes.DWORD(0)
        while True:
            handle = self._out_read
            if handle is None:
                break
            ok = _kernel32.ReadFile(
                handle, buf, _READ_CHUNK_SIZE, ctypes.byref(bytes_read), None
            )
            if not ok or bytes_read.value == 0:
                break
            self._append_output(buf.raw[: bytes_read.value])
        self._finish_subscribers()

    def _wait_loop_windows(self) -> None:
        assert sys.platform == "win32"
        handle = self._h_process
        if handle is None:
            return
        _kernel32.WaitForSingleObject(handle, _INFINITE)
        code = wintypes.DWORD(0)
        if _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            self._exit_code = int(code.value)
        else:
            self._exit_code = -1
        # Close ConPTY once child exits so ReadFile unblocks if no descendant holds it.
        self._cleanup_handles_windows()
        if (
            self._on_exit is not None
            and self._loop is not None
            and not self._loop.is_closed()
        ):
            cb = self._on_exit
            rc = self._exit_code
            self._loop.call_soon_threadsafe(cb, rc)

    def _cleanup_handles_windows(self) -> None:
        assert sys.platform == "win32"
        with self._lock:
            if self._closed:
                return
            self._closed = True
            hpc, self._hpc = self._hpc, None
            in_write, self._in_write = self._in_write, None
            out_read, self._out_read = self._out_read, None
            h_proc, self._h_process = self._h_process, None
        if hpc is not None:
            with contextlib.suppress(OSError):
                _kernel32.ClosePseudoConsole(hpc)
        if in_write is not None:
            with contextlib.suppress(OSError):
                _kernel32.CloseHandle(in_write)
        if out_read is not None:
            with contextlib.suppress(OSError):
                _kernel32.CloseHandle(out_read)
        if h_proc is not None:
            with contextlib.suppress(OSError):
                _kernel32.CloseHandle(h_proc)
        self._finish_subscribers()

    def _spawn_posix(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        cols: int,
        rows: int,
    ) -> None:
        assert sys.platform != "win32"
        master_fd, slave_fd = pty.openpty()
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)
        self._master_fd = master_fd
        self._posix_proc = proc
        self._pid = int(proc.pid)

        self._reader_thread = threading.Thread(
            target=self._read_loop_posix,
            name=f"rc-pty-read-{self._pid}",
            daemon=True,
        )
        self._waiter_thread = threading.Thread(
            target=self._wait_loop_posix,
            name=f"rc-pty-wait-{self._pid}",
            daemon=True,
        )
        self._reader_thread.start()
        self._waiter_thread.start()

    def _read_loop_posix(self) -> None:
        while True:
            fd = self._master_fd
            if fd is None:
                break
            try:
                chunk = os.read(fd, _READ_CHUNK_SIZE)
            except OSError:
                break
            if not chunk:
                break
            self._append_output(chunk)
        self._finish_subscribers()

    def _wait_loop_posix(self) -> None:
        proc = self._posix_proc
        if proc is None:
            return
        rc = proc.wait()
        self._exit_code = int(rc)
        self._cleanup_handles_posix()
        if (
            self._on_exit is not None
            and self._loop is not None
            and not self._loop.is_closed()
        ):
            cb = self._on_exit
            self._loop.call_soon_threadsafe(cb, self._exit_code)

    def _cleanup_handles_posix(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            fd, self._master_fd = self._master_fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        self._finish_subscribers()

    async def write(self, data: bytes) -> None:
        if not data or self._closed:
            return
        if sys.platform == "win32":
            handle = self._in_write
            if handle is None:
                return

            def _write_win() -> None:
                written = wintypes.DWORD(0)
                _kernel32.WriteFile(
                    handle, data, len(data), ctypes.byref(written), None
                )

            await asyncio.to_thread(_write_win)
        else:
            fd = self._master_fd
            if fd is None:
                return
            await asyncio.to_thread(os.write, fd, data)

    async def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        bounded_cols = max(20, min(int(cols), 500))
        bounded_rows = max(5, min(int(rows), 200))
        if sys.platform == "win32":
            hpc = self._hpc
            if hpc is not None:
                await asyncio.to_thread(
                    _kernel32.ResizePseudoConsole,
                    hpc,
                    _COORD(bounded_cols, bounded_rows),
                )
        else:
            fd = self._master_fd
            if fd is not None:
                winsize = struct.pack("HHHH", bounded_rows, bounded_cols, 0, 0)
                with contextlib.suppress(OSError):
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                if self._pid is not None:
                    with contextlib.suppress(OSError):
                        os.killpg(os.getpgid(self._pid), signal.SIGWINCH)

    async def send_signal(self, sig_name: str = "SIGINT") -> None:
        if self._closed:
            return
        if sig_name.upper() == "SIGINT":
            await self.write(b"\x03")
            return
        if sys.platform != "win32" and self._pid is not None:
            sig = getattr(signal, sig_name.upper(), signal.SIGTERM)
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(self._pid), sig)

    async def close(self) -> None:
        if sys.platform == "win32":
            await asyncio.to_thread(self._close_windows)
        else:
            await asyncio.to_thread(self._close_posix)

    def _close_windows(self) -> None:
        assert sys.platform == "win32"
        pid = self._pid
        h_proc = self._h_process
        if pid is not None and h_proc is not None:
            flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5.0,
                    creationflags=flags,
                    check=False,
                )
            with contextlib.suppress(OSError):
                _kernel32.TerminateProcess(h_proc, 1)
        self._cleanup_handles_windows()
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)

    def _close_posix(self) -> None:
        assert sys.platform != "win32"
        proc = self._posix_proc
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=2.0)
        self._cleanup_handles_posix()
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
