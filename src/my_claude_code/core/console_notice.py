"""One timestamped line on this process's own console (7.70.0).

The server's own log goes to ``server.log`` only -- loguru's console sink is
removed when logging is configured -- so a sentence a person at the console
must see is printed separately. This is that print, in the shape
``runtime/listener_guard.py``'s console line already uses
(``[YYYY-MM-DD HH:MM:SS] ...``), so a transcript made of both reads as one
timeline.

"This process's console" is whatever ``sys.stderr`` is: the PowerShell or cmd
window on Windows, the terminal on Linux and macOS. A windowless interpreter
(``pythonw``, which is what ``mcc-desktop`` runs as on Windows) has no console,
``sys.stderr`` is ``None`` there, and nothing is written -- the caller's log line
is the record in that case.
"""

import sys
import time
from typing import TextIO

#: The stamp shape every console line MCC prints itself carries.
CONSOLE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def console_line(text: str, *, now: float | None = None) -> str:
    """``text`` behind a local-time stamp, as one line."""

    stamp = time.strftime(CONSOLE_TIMESTAMP_FORMAT, time.localtime(now))
    return f"[{stamp}] {' '.join(text.split())}"


def console_available(stream: TextIO | None = None) -> bool:
    """Whether there is a console to write to at all."""

    return (sys.stderr if stream is None else stream) is not None


def write_console_line(
    text: str, *, stream: TextIO | None = None, now: float | None = None
) -> bool:
    """Print ``text`` as one stamped line on stderr. Never raises.

    Returns whether the line was written. ``stream`` exists for tests; the
    default is the ``sys.stderr`` of the moment, read at call time so a
    redirected stream is honoured.
    """

    target = sys.stderr if stream is None else stream
    if target is None:
        return False
    try:
        target.write(console_line(text, now=now) + "\n")
        target.flush()
    except OSError, ValueError, AttributeError:
        return False
    return True


__all__ = [
    "CONSOLE_TIMESTAMP_FORMAT",
    "console_available",
    "console_line",
    "write_console_line",
]
