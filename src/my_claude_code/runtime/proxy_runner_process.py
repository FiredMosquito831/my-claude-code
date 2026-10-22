"""The operating-system half of a WireGuard runner (7.108.0, PR-R2).

Three facts about one child process, each answered without ever signalling
it -- ``os.kill(pid, 0)`` *terminates* a process on Windows:

* :func:`creation_token` -- when the process with this pid was created. A
  pid alone is not an identity (the operating system reuses them); a pid and
  its creation time are. ``pid.json`` records both, and a stale one is acted
  on only when both still match.
* :class:`KillOnCloseJob` -- on Windows, a Job Object with
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. The server holds its one handle; a
  runner is assigned to it right after it starts. When the server exits --
  cleanly, crashed, or killed from Task Manager -- Windows closes the handle
  and ends every process in the job, so a dead server never leaves a tunnel
  running (the ``kernel32`` precedent: ``application/release_updates.py``).
* :func:`popen_flags` -- ``CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`` on
  Windows (no console window, not part of the server's Ctrl+C group);
  ``start_new_session`` on POSIX.

Nothing here kills anything by name or by image: only the pid MCC started.
Win32 types are spelled with plain ``ctypes`` types (a ``HANDLE`` is a
``c_void_p``, a ``DWORD`` a ``c_uint32``, a ``FILETIME`` a ``c_ulonglong``),
as ``core/startup_state.py`` does, so the module imports and type-checks on
every platform.
"""

import ctypes
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

_WINDOWS = sys.platform == "win32"

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_STILL_ACTIVE = 259
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def popen_flags() -> tuple[int, bool]:
    """``(creationflags, start_new_session)`` that detach a runner from the
    server's console: the Windows flags there, a new session elsewhere."""

    if _WINDOWS:
        return (
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
            False,
        )
    return 0, True


def _kernel32() -> Any:
    # ``ctypes.WinDLL`` exists only on Windows, and only Windows branches
    # reach this.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Without explicit types a HANDLE is truncated to 32 bits on 64-bit
    # Windows, and every call that takes one fails silently.
    handle = ctypes.c_void_p
    dword = ctypes.c_uint32
    kernel32.OpenProcess.restype = handle
    kernel32.OpenProcess.argtypes = [dword, ctypes.c_int, dword]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [handle]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.GetProcessTimes.argtypes = [
        handle,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.GetExitCodeProcess.argtypes = [handle, ctypes.POINTER(dword)]
    kernel32.CreateJobObjectW.restype = handle
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.SetInformationJobObject.argtypes = [
        handle,
        ctypes.c_int,
        ctypes.c_void_p,
        dword,
    ]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [handle, handle]
    return kernel32


def _windows_creation(pid: int) -> str | None:
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        if code.value != _STILL_ACTIVE:
            return None
        created = ctypes.c_ulonglong()
        unused = ctypes.c_ulonglong()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(unused),
            ctypes.byref(unused),
            ctypes.byref(unused),
        ):
            return None
        return str(created.value)
    finally:
        kernel32.CloseHandle(handle)


def _linux_creation(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The command name is in parentheses and may contain spaces: the fields
    # after the last ")" are fixed. starttime is field 22 overall.
    fields = text.rsplit(")", 1)[-1].split()
    if len(fields) < 20 or fields[0] == "Z":
        return None
    return fields[19]


def _ps_creation(pid: int) -> str | None:
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except OSError, subprocess.SubprocessError:
        return None
    started = completed.stdout.strip()
    return started or None


def creation_token(pid: int) -> str | None:
    """When the live process ``pid`` was created, as an opaque string.

    ``None`` when no such process is running. Never sends it a signal.
    """

    if pid <= 0:
        return None
    if _WINDOWS:
        return _windows_creation(pid)
    if sys.platform.startswith("linux"):
        return _linux_creation(pid)
    return _ps_creation(pid)


class KillOnCloseJob:
    """A Windows Job Object that ends its processes when its handle closes.

    Created once per server process. Elsewhere it is inert: :meth:`assign`
    answers ``False`` and the pid file is what a later start reaps by.
    """

    def __init__(self) -> None:
        self._handle: int | None = None
        self.problem = ""
        if _WINDOWS:
            self._create()

    def _create(self) -> None:
        kernel32 = _kernel32()
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            self.problem = f"CreateJobObject failed ({ctypes.get_last_error()})"
            return
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            self.problem = f"SetInformationJobObject failed ({ctypes.get_last_error()})"
            kernel32.CloseHandle(handle)
            return
        self._handle = int(handle)

    @property
    def active(self) -> bool:
        return self._handle is not None

    def assign(self, pid: int) -> bool:
        """Put the process ``pid`` in the job. ``False`` when it could not be."""

        if self._handle is None:
            return False
        kernel32 = _kernel32()
        process = kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
        )
        if not process:
            self.problem = f"OpenProcess failed ({ctypes.get_last_error()})"
            return False
        try:
            if not kernel32.AssignProcessToJobObject(self._handle, process):
                self.problem = (
                    f"AssignProcessToJobObject failed ({ctypes.get_last_error()})"
                )
                return False
        finally:
            kernel32.CloseHandle(process)
        return True

    def close(self) -> None:
        """Close the handle: Windows ends every process still in the job."""

        handle, self._handle = self._handle, None
        if handle is not None:
            with suppress(OSError):
                _kernel32().CloseHandle(handle)


def runner_environment() -> dict[str, str]:
    """The environment a runner starts with: the server's, minus proxies.

    wireproxy dials the VPN server itself; an ``HTTP(S)_PROXY`` the server
    was started with is the server's business, not the tunnel's.
    """

    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    }


__all__ = [
    "KillOnCloseJob",
    "creation_token",
    "popen_flags",
    "runner_environment",
]
