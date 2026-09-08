"""What the server answers while it is still coming up, and how long it took.

Before 6.59.0 a starting server was, from the outside, indistinguishable from a
free port. uvicorn awaits the ASGI lifespan startup *before* it creates the
listening socket, and MCC did all of its work in that lifespan -- so for the
whole of a 20-second start nothing was listening, ``probe_server_presence``
answered ``free``, and anything that reads "free port" as "start a server here"
started a second one. The second one lost the bind race and died silently.

Two answers, both in this module:

* **A presence between "nothing" and "healthy".** The listener binds first now,
  and every request until readiness is refused with a 503 carrying
  ``x-mcc-starting: 1`` and a body naming the stage. That is a *distinct*
  answer: not a free port, not a stranger on the port, and -- because the
  shutdown gate is checked first and stamps its own marker -- not a drain
  either. A program can now wait for a start instead of racing it.
* **A stage table.** Every stage marks itself here, which emits one
  ``STARTUP: <stage> +Nms`` line at INFO. Startup cost had roughly tripled
  between 6.41.2 and 6.58.4 without anyone noticing, because nothing measured
  it. Now every release measures it, in the log the user already sends.

Deliberately in ``core``: the ASGI gate (``runtime``), the supervisor (``cli``)
and the presence probe (``cli``) all read it, and ``core`` is the only package
all three may import. It is a sibling of ``core.stop_deadline`` in every way --
one process-wide fact, no configuration of its own, and a marker header that
exists so a *program* can tell one 503 from another.
"""

import ctypes
import os
import platform
import threading
import time

from loguru import logger

#: The signature a program reads on a "still starting" refusal. Distinct from
#: ``x-mcc-shutdown`` on purpose: one means "wait, it is coming", the other
#: means "wait, it is going". A caller that cannot tell them apart cannot
#: decide whether to hold still or to start a replacement.
STARTING_MARKER_HEADER = "x-mcc-starting"
STARTING_MARKER_VALUE = "1"

#: What a refused request is told to wait. Short: a start is measured in
#: seconds, and the caller polling this is a window with a spinner on it.
STARTING_RETRY_AFTER_SECONDS = 1

#: The stage a process is in before anything has marked one.
INITIAL_STAGE = "starting"

#: The stage name that means "the listener is bound and the heavy startup is
#: running behind it". Named here because the supervisor sets it and the tests
#: assert on it.
LISTENER_STAGE = "listener"


def _process_start_monotonic() -> float:
    """A monotonic instant as close to this process's birth as the OS will say.

    Most of a cold start is spent before any of this package's own code runs:
    on the reporter's machine about three seconds of interpreter boot and
    imports, and before 6.59.0 another six of ``Settings``. A clock that
    started when this module was imported would hide the largest rows in its
    own table -- and hiding them is exactly how the cost tripled between 6.41.2
    and 6.58.4 with nobody noticing.

    Windows and Linux can both answer the question exactly. Anywhere else, and
    on any failure, this falls back to "now", which is this module's import
    time: the stage table is then relative rather than absolute, which is worth
    strictly more than no table at all.
    """

    now = time.monotonic()
    system = platform.system()
    try:
        if system == "Windows":
            return now - _windows_process_age_seconds()
        if system == "Linux":
            return now - _linux_process_age_seconds()
    except Exception:
        return now
    return now


def _windows_process_age_seconds() -> float:
    """Seconds since this process was created, from ``GetProcessTimes``."""

    creation = ctypes.c_ulonglong()
    unused = ctypes.c_ulonglong()
    # ``ctypes.windll`` exists only on Windows, and only this branch reaches
    # it. Read through ``getattr`` so the module still type-checks on the
    # platforms where the attribute is genuinely absent.
    # ``ctypes.WinDLL`` exists only on Windows, and only this branch reaches
    # it. It is loaded by name rather than through ``ctypes.windll`` so the
    # module still type-checks on the platforms where neither exists.
    kernel32 = ctypes.WinDLL("kernel32")
    handle = kernel32.GetCurrentProcess()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(unused),
        ctypes.byref(unused),
        ctypes.byref(unused),
    ):
        raise OSError("GetProcessTimes failed")
    now = ctypes.c_ulonglong()
    kernel32.GetSystemTimeAsFileTime(ctypes.byref(now))
    # FILETIME counts 100-nanosecond intervals.
    return max(0.0, (now.value - creation.value) / 1e7)


def _linux_process_age_seconds() -> float:
    """Seconds since this process was created, from ``/proc``."""

    with open("/proc/self/stat", encoding="utf-8") as handle:
        fields = handle.read().rpartition(") ")[2].split()
    # Field 22 of the manual page, which is index 19 after the comm field.
    starttime_ticks = int(fields[19])
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    with open("/proc/uptime", encoding="utf-8") as handle:
        uptime = float(handle.read().split()[0])
    return max(0.0, uptime - starttime_ticks / ticks_per_second)


#: Captured once, at import, and never recomputed. See the function above.
PROCESS_START_MONOTONIC = _process_start_monotonic()


class StartupState:
    """One process's progress from launch to ready. Thread-safe.

    ``elapsed_ms`` is measured from :data:`PROCESS_START_MONOTONIC` -- the
    instant the operating system says this process was created -- and not from
    the moment the ASGI app was built. Roughly three seconds of a
    cold start are the interpreter and the import chain, and another few were
    the ``Settings`` construction; every one of them happened before any of the
    later objects existed, so a clock that started with them would have
    reported a fast start for a slow one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = PROCESS_START_MONOTONIC
        self._stage = INITIAL_STAGE
        self._ready = False
        self._failed = False
        #: How many server generations this process has run. See ``begin``.
        self._generation = 0

    def begin(self) -> None:
        """Restart the clock for a new server generation.

        An in-process RELOAD builds a second application in the same process.
        Without this the second generation would report the first one's elapsed
        time and would already be ``ready`` before it had started.

        The FIRST call keeps the process-start reference: the interpreter
        boot and the import chain are part of how long a cold start took, and
        a table that began after them would hide the largest row in it. A
        later call -- a reload -- starts from now, because the imports are
        long since paid for.
        """

        with self._lock:
            if self._generation:
                self._started_at = time.monotonic()
            self._generation += 1
            self._stage = INITIAL_STAGE
            self._ready = False
            self._failed = False

    def mark(self, stage: str) -> None:
        """Enter ``stage`` and log one ``STARTUP:`` line for it."""

        with self._lock:
            self._stage = stage
            elapsed = self._elapsed_ms_locked()
        logger.info("STARTUP: {stage} +{elapsed}ms", stage=stage, elapsed=elapsed)

    def mark_ready(self) -> None:
        """The application is serving. Every route answers normally now."""

        with self._lock:
            if self._ready:
                return
            self._ready = True
            self._stage = "ready"
            elapsed = self._elapsed_ms_locked()
        logger.info("STARTUP: ready +{elapsed}ms", elapsed=elapsed)

    def mark_failed(self) -> None:
        """Startup raised. The process is going away; say so rather than lie."""

        with self._lock:
            self._failed = True
            self._stage = "failed"

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready

    @property
    def failed(self) -> bool:
        with self._lock:
            return self._failed

    @property
    def stage(self) -> str:
        with self._lock:
            return self._stage

    @property
    def elapsed_ms(self) -> int:
        with self._lock:
            return self._elapsed_ms_locked()

    def _elapsed_ms_locked(self) -> int:
        return int((time.monotonic() - self._started_at) * 1000)


_STATE = StartupState()


def startup_state() -> StartupState:
    """The one startup state of this process."""

    return _STATE
