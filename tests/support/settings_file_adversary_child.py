"""A second process on the settings file, for real Windows refusals (7.69.7).

Started by ``tests/config/test_settings_save_two_process.py``. It never reads
or writes anything but the path it is given. Modes:

``hold PATH READY STOP``
    Opens PATH with no sharing at all (``CreateFileW`` share mode 0), so every
    read and every replace of it by another process fails with a sharing
    violation, until STOP appears or 30 s pass.

``replace PATH CONTENT_PATH READY STOP``
    Loops: write CONTENT_PATH's bytes to ``PATH.adversary.tmp``, then
    ``os.replace`` it onto PATH -- what a sync tool, an editor's safe save or a
    second writer does. While that runs, a reader of PATH is refused for a
    moment, again and again.

``read PATH READY STOP``
    Loops: open PATH, read it all, close -- what a backup tool, an indexer or
    an antivirus scan does. While the handle is open, a replace of PATH by
    another process is refused.

READY is created once the mode is in effect; the child exits when STOP
appears or after 30 s, whichever is first.
"""

import ctypes
import os
import sys
import time
from pathlib import Path

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
LIMIT_SECONDS = 30.0


def _stopped(stop: Path, started: float) -> bool:
    return stop.exists() or time.monotonic() - started > LIMIT_SECONDS


def _hold(path: Path, ready: Path, stop: Path) -> int:
    if sys.platform != "win32":
        return 2
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateFileW(
        str(path), GENERIC_READ, 0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return 3
    try:
        ready.write_text("ready\n", encoding="utf-8")
        started = time.monotonic()
        while not _stopped(stop, started):
            time.sleep(0.02)
    finally:
        kernel32.CloseHandle(handle)
    return 0


def _replace(path: Path, content_path: Path, ready: Path, stop: Path) -> int:
    content = content_path.read_bytes()
    staging = path.with_name(path.name + ".adversary.tmp")
    ready.write_text("ready\n", encoding="utf-8")
    started = time.monotonic()
    while not _stopped(stop, started):
        try:
            staging.write_bytes(content)
            os.replace(staging, path)
        except OSError:
            continue
    return 0


def _read(path: Path, ready: Path, stop: Path) -> int:
    ready.write_text("ready\n", encoding="utf-8")
    started = time.monotonic()
    while not _stopped(stop, started):
        try:
            with open(path, "rb") as handle:
                handle.read()
        except OSError:
            continue
    return 0


def main() -> int:
    mode = sys.argv[1]
    if mode == "hold":
        return _hold(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
    if mode == "replace":
        return _replace(
            Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5])
        )
    if mode == "read":
        return _read(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
