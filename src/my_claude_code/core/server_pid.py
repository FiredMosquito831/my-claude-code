"""The process id a server stamps on every ``/health`` answer (7.70.0).

``x-mcc-pid: <pid>`` names the process that answered, on all four answers the
ASGI gate gives -- ready (200), busy (200 + ``x-mcc-busy``), starting (503 +
``x-mcc-starting``) and shutting down (503 + ``x-mcc-shutdown``). The bodies are
unchanged byte for byte; this is one header beside them.

Three readers want it, and none of them could have it before without asking the
operating system, which is slow exactly when the server is busy:

* a starting server that finds the port held names the process it is backing
  off from (``core/port_holder_answer.py``);
* ``mcc-desktop`` remembers the pid of the server it last heard from, so that
  when the server stops answering it can say whether that process exited or is
  still running without its port (``core/server_watch.py``);
* the desktop app, from its next release, compares it with the pid the OS says
  holds the port (rescue spec, fact K).

An older server sends no such header, so every reader treats its absence as
"unknown" and never as a reason to act.
"""

import os
from collections.abc import Mapping

#: The header's name, lower-case as it travels in an ASGI head.
SERVER_PID_HEADER = "x-mcc-pid"


def server_pid_header() -> tuple[bytes, bytes]:
    """The ``(name, value)`` pair for this process, ready for an ASGI head."""

    return SERVER_PID_HEADER.encode("ascii"), str(os.getpid()).encode("ascii")


def pid_from_headers(headers: Mapping[str, str]) -> int | None:
    """The pid a ``/health`` answer named, or ``None`` when it named none.

    Header names are matched case-insensitively. Anything that is not a
    positive integer is ``None``: a reader acts on a pid, so a value it cannot
    trust must look exactly like no value at all.
    """

    raw: str | None = None
    for name, value in headers.items():
        if str(name).strip().lower() == SERVER_PID_HEADER:
            raw = str(value)
    if raw is None:
        return None
    try:
        pid = int(raw.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


__all__ = ["SERVER_PID_HEADER", "pid_from_headers", "server_pid_header"]
