"""The WireGuard runner supervisor: start, watch and stop wireproxy (7.108.0, PR-R2).

One supervisor per server process, attached to
:data:`~my_claude_code.application.proxy_runner_state.RUNNERS` by the
runtime. For each ``runner`` source the operator started it:

1. **Re-verifies the binary** (:func:`~my_claude_code.config.proxy_runner_bins.resolve_wireproxy`)
   and refuses one whose receipt or digest no longer matches.
2. **Regenerates the config** from ``proxy_sources.json`` into
   ``~/.mcc/runners/<source>/wireproxy.conf``, owner-only. It is never logged.
3. **Reaps a stale runner of an earlier server** -- only the pid in
   ``pid.json``, and only if its creation time still matches (a pid alone is
   not an identity).
4. **Starts it** with ``subprocess.Popen([wireproxy, "-c", conf, "-i",
   "127.0.0.1:<health>"])``, ``stdin`` closed, no console window and its own
   process group (Windows) or session (POSIX); on Windows it joins the
   server's kill-on-close Job Object, so a server that dies hard takes it
   along. Its pid goes to ``pid.json`` and to ``cli/process_registry`` (an
   import, never an edit, of that frozen module).
5. **Watches it**: wireproxy's output goes, redacted, into a capped
   ``runner.log`` and the card's last lines; ``starting`` turns ``ready`` once
   its SOCKS5 port accepts and ``/readyz`` answers 200.
6. **On an exit nobody asked for**: with *Restart automatically* ticked
   (off by default -- the user's decision of 2026-10-20 14:32), restarts after
   1 s, 5 s, 30 s and 5 min, then marks it ``crashed`` and leaves it until the
   operator acts; unticked, it is ``stopped`` with its exit code and a Restart
   button.

Stopping is graceful first (``terminate`` on the exact process handle MCC
holds), then ``kill_pid_tree_best_effort`` on that pid, each step bounded, so
the server's shutdown finishes within its own bound. Never a TUN device, a
route change, a service or an administrator prompt; never a kill by name.
"""

import collections
import json
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger

from my_claude_code.application.proxy_runner_sources import (
    RunnerLaunch,
    port_binds,
    runner_launch,
    runner_source_ids,
)
from my_claude_code.application.proxy_runner_state import (
    RESTART_BACKOFF_SECONDS,
    RUNNERS,
    STATE_CRASHED,
    STATE_READY,
    STATE_RESTARTING,
    STATE_STARTING,
    STATE_STOPPED,
    RunnerHub,
    RunnerReading,
)
from my_claude_code.application.proxy_sources import SourceEditError
from my_claude_code.cli.process_registry import (
    kill_pid_tree_best_effort,
    register_pid,
    unregister_pid,
)
from my_claude_code.config.owner_only import (
    write_owner_only_bytes,
    write_owner_only_json,
)
from my_claude_code.config.proxy_runner_bins import (
    RUNNER_CONFIG_FILENAME,
    RUNNER_LOG_FILENAME,
    RUNNER_PID_FILENAME,
    BinaryRefusal,
    ResolvedBinary,
    remove_runner_files,
    resolve_wireproxy,
    runners_dir,
)
from my_claude_code.config.proxy_sources import ProxySources, current_proxy_sources
from my_claude_code.core.diagnostics import redact_sensitive_error_text
from my_claude_code.runtime.proxy_runner_process import (
    KillOnCloseJob,
    creation_token,
    popen_flags,
    runner_environment,
)

CONFIG_FILENAME = RUNNER_CONFIG_FILENAME
PID_FILENAME = RUNNER_PID_FILENAME
LOG_FILENAME = RUNNER_LOG_FILENAME
#: ``runner.log`` is cut back to its newest half once it passes this.
LOG_CAP_BYTES = 256 * 1024
#: How many of wireproxy's last lines the card shows.
LOG_LINES_SHOWN = 12
#: How long a stop waits after each step before taking the next.
GRACEFUL_SECONDS = 2.0
#: How often a starting runner is asked whether it is ready, and how long
#: before the card says it is still waiting on the tunnel's own check.
READY_POLL_SECONDS = 0.25
READY_PATIENCE_SECONDS = 30.0
_LOOPBACK = "127.0.0.1"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _clock(moment: datetime) -> str:
    return moment.strftime("%H:%M:%S UTC")


def socks_port_accepts(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((_LOOPBACK, port), timeout=timeout):
            return True
    except OSError:
        return False


def readyz_answers(port: int, timeout: float = 1.0) -> bool:
    """Whether wireproxy's ``/readyz`` on ``127.0.0.1:<port>`` says 200.

    A plain HTTP/1.0 request over a socket: loopback only, no client library,
    no proxy setting consulted.
    """

    try:
        with socket.create_connection((_LOOPBACK, port), timeout=timeout) as probe:
            probe.settimeout(timeout)
            probe.sendall(
                b"GET /readyz HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            head = probe.recv(64)
    except OSError:
        return False
    parts = head.split(b" ", 2)
    return len(parts) >= 2 and parts[0].startswith(b"HTTP/") and parts[1] == b"200"


class _CappedLog:
    """``runner.log``: appended line by line, cut to its newest half past the cap."""

    def __init__(self, path: Path, cap: int) -> None:
        self.path = path
        self.cap = cap

    def write(self, line: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if self.path.stat().st_size > self.cap:
                data = self.path.read_bytes()[-(self.cap // 2) :]
                cut = data.find(b"\n")
                self.path.write_bytes(data[cut + 1 :] if cut >= 0 else data)
        except OSError:
            return


@dataclass(eq=False)
class _Runner:
    """One running wireproxy and what is known about it."""

    source_id: str
    popen: subprocess.Popen[bytes]
    launch: RunnerLaunch
    binary: str
    started_at: datetime
    created: str
    log: _CappedLog
    lines: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=LOG_LINES_SHOWN)
    )
    stop_requested: bool = False
    stop_sentence: str = ""
    ready_since: datetime | None = None

    @property
    def pid(self) -> int:
        return int(self.popen.pid)


Resolver = Callable[[], ResolvedBinary | BinaryRefusal]


class ProxyRunnerSupervisor:
    """Every runner of this server process. Thread-safe; every call is bounded."""

    def __init__(
        self,
        *,
        resolve: Resolver = resolve_wireproxy,
        load_sources: Callable[[], ProxySources] = current_proxy_sources,
        base_dir: Callable[[], Path] = runners_dir,
        register: Callable[[int], None] = register_pid,
        unregister: Callable[[int], None] = unregister_pid,
        kill_tree: Callable[[int], None] = kill_pid_tree_best_effort,
        hub: RunnerHub = RUNNERS,
        backoff: tuple[float, ...] = RESTART_BACKOFF_SECONDS,
        graceful_seconds: float = GRACEFUL_SECONDS,
        ready_poll_seconds: float = READY_POLL_SECONDS,
        port_free: Callable[[int], bool] = port_binds,
        job_factory: Callable[[], KillOnCloseJob] = KillOnCloseJob,
    ) -> None:
        self._resolve = resolve
        self._load_sources = load_sources
        self._base_dir = base_dir
        self._register = register
        self._unregister = unregister
        self._kill_tree = kill_tree
        self._hub = hub
        self._backoff = backoff
        self._graceful = graceful_seconds
        self._ready_poll = ready_poll_seconds
        self._port_free = port_free
        self._job_factory = job_factory
        self._job: KillOnCloseJob | None = None
        self._lock = threading.RLock()
        self._runners: dict[str, _Runner] = {}
        self._attempts: dict[str, int] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._closed = False

    # ---------------------------------------------------------------- state

    def _set(self, source_id: str, reading: RunnerReading) -> RunnerReading:
        self._hub.set_reading(source_id, reading)
        return reading

    def _stopped(
        self,
        source_id: str,
        sentence: str,
        *,
        state: str = STATE_STOPPED,
        exit_code: int | None = None,
        restarts: int = 0,
    ) -> RunnerReading:
        previous = self._hub.reading(source_id)
        return self._set(
            source_id,
            RunnerReading(
                state=state,
                sentence=sentence,
                since=_iso(_now()),
                exit_code=exit_code,
                restarts=restarts,
                log=previous.log if previous is not None else (),
            ),
        )

    def _running_reading(
        self, runner: _Runner, state: str, sentence: str
    ) -> RunnerReading:
        return self._set(
            runner.source_id,
            RunnerReading(
                state=state,
                sentence=sentence,
                pid=runner.pid,
                since=_iso(runner.ready_since or runner.started_at),
                restarts=self._attempts.get(runner.source_id, 0),
                log=tuple(runner.lines),
            ),
        )

    def directory(self, source_id: str) -> Path:
        return self._base_dir() / source_id

    def running_pid(self, source_id: str) -> int:
        with self._lock:
            runner = self._runners.get(source_id)
            return runner.pid if runner is not None else 0

    # -------------------------------------------------------------- control

    def start(self, source_id: str) -> RunnerReading:
        """Start one runner now, as the operator asked. A running one is left."""

        with self._lock:
            self._cancel_timer(source_id)
            running = self._runners.get(source_id)
            if running is not None and running.popen.poll() is None:
                reading = self._hub.reading(source_id)
                return reading if reading is not None else RunnerReading()
            # An operator's start begins the restart ladder again.
            self._attempts.pop(source_id, None)
            return self._launch(source_id)

    def stop(self, source_id: str, sentence: str = "Stopped by you.") -> RunnerReading:
        """Stop one runner: the exact process MCC started, nothing else."""

        with self._lock:
            self._cancel_timer(source_id)
            self._attempts.pop(source_id, None)
            runner = self._runners.get(source_id)
            if runner is None:
                return self._stopped(source_id, sentence)
            runner.stop_requested = True
            runner.stop_sentence = sentence
        self._terminate(runner)
        return self._stopped(source_id, sentence, exit_code=runner.popen.returncode)

    def restart(self, source_id: str) -> RunnerReading:
        self.stop(source_id, "Restarting.")
        return self.start(source_id)

    def remove(self, source_id: str) -> None:
        """Stop it and delete everything MCC wrote for it under ``runners/``."""

        self.stop(source_id, "Removed.")
        remove_runner_files(source_id, self._base_dir())
        self._hub.forget(source_id)

    def start_enabled(self) -> list[str]:
        """At server start, on a worker thread: every runner left switched on."""

        try:
            wanted = runner_source_ids(self._load_sources(), enabled_only=True)
        except Exception as exc:
            logger.warning(
                "PROXY RUNNERS: could not read the sources: {}", type(exc).__name__
            )
            return []
        for source_id in wanted:
            try:
                self.start(source_id)
            except Exception as exc:
                logger.warning(
                    "PROXY RUNNERS: {} did not start: {}", source_id, type(exc).__name__
                )
        return wanted

    def stop_all(self) -> None:
        """At server shutdown: every runner, concurrently, each step bounded."""

        with self._lock:
            self._closed = True
            for source_id in list(self._timers):
                self._cancel_timer(source_id)
            runners = list(self._runners.values())
            for runner in runners:
                runner.stop_requested = True
                runner.stop_sentence = "Stopped with the server."
        for runner in runners:
            with suppress(OSError):
                runner.popen.terminate()
        deadline = time.monotonic() + self._graceful
        stubborn: list[_Runner] = []
        for runner in runners:
            try:
                runner.popen.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                stubborn.append(runner)
        killers = [
            threading.Thread(
                target=self._kill_tree,
                args=(runner.pid,),
                daemon=True,
                name="mcc-runner-kill",
            )
            for runner in stubborn
        ]
        for killer in killers:
            killer.start()
        for killer in killers:
            killer.join(self._graceful)
        job, self._job = self._job, None
        if job is not None:
            job.close()
        if runners:
            logger.info(
                "PROXY RUNNERS: stopped {} runner(s) with the server", len(runners)
            )

    # ------------------------------------------------------------- internals

    def _cancel_timer(self, source_id: str) -> None:
        timer = self._timers.pop(source_id, None)
        if timer is not None:
            timer.cancel()

    def _job_for_launch(self) -> KillOnCloseJob:
        if self._job is None:
            self._job = self._job_factory()
            if self._job.problem:
                logger.warning(
                    "PROXY RUNNERS: no kill-on-close job ({}); a server that dies "
                    "hard may leave a runner, which its next start reaps by pid",
                    self._job.problem,
                )
        return self._job

    def _reap_stale(self, source_id: str) -> None:
        """A runner an earlier server left: killed only if pid AND creation match."""

        path = self.directory(source_id) / PID_FILENAME
        try:
            recorded = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return
        pid = recorded.get("pid") if isinstance(recorded, dict) else None
        created = recorded.get("created") if isinstance(recorded, dict) else None
        if isinstance(pid, int) and isinstance(created, str) and created:
            if creation_token(pid) == created:
                logger.info(
                    "PROXY RUNNERS: {} -- stopping the runner an earlier server "
                    "left (pid {}, same creation time)",
                    source_id,
                    pid,
                )
                self._kill_tree(pid)
            else:
                logger.info(
                    "PROXY RUNNERS: {} -- pid.json names pid {}, which is no longer "
                    "that runner; left alone",
                    source_id,
                    pid,
                )
        with suppress(OSError):
            path.unlink(missing_ok=True)

    def _launch(self, source_id: str) -> RunnerReading:
        if self._closed:
            return self._stopped(source_id, "Not started: the server is stopping.")
        sources = self._load_sources()
        source = sources.source(source_id)
        if source is None or source.runner is None:
            return self._stopped(source_id, "There is no such WireGuard runner.")
        self._reap_stale(source_id)
        binary = self._resolve()
        if isinstance(binary, BinaryRefusal):
            return self._stopped(source_id, f"Not started: {binary.sentence}")
        try:
            launch = runner_launch(source, sources)
        except SourceEditError as exc:
            return self._stopped(source_id, f"Not started: {exc}")
        busy = [port for port in launch.ports if not self._port_free(port)]
        if busy:
            return self._stopped(
                source_id,
                "Not started: 127.0.0.1:"
                + ", ".join(str(port) for port in busy)
                + " is in use by another program. Press Pick new ports, or free it.",
            )
        directory = self.directory(source_id)
        config = directory / CONFIG_FILENAME
        write_owner_only_bytes(config, launch.config_text.encode("utf-8"))
        argv = [
            *binary.argv,
            "-c",
            str(config),
            "-i",
            f"{_LOOPBACK}:{launch.health_port}",
        ]
        log = _CappedLog(directory / LOG_FILENAME, LOG_CAP_BYTES)
        started_at = _now()
        log.write(
            f"--- {_iso(started_at)} starting {binary.origin} wireproxy "
            f"-c {CONFIG_FILENAME} -i {_LOOPBACK}:{launch.health_port}"
        )
        job = self._job_for_launch()
        creationflags, new_session = popen_flags()
        try:
            popen = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(directory),
                env=runner_environment(),
                close_fds=True,
                creationflags=creationflags,
                start_new_session=new_session,
            )
        except OSError as exc:
            log.write(f"--- could not start: {type(exc).__name__}")
            return self._stopped(
                source_id,
                f"Not started: wireproxy would not run ({type(exc).__name__}).",
            )
        job.assign(popen.pid)
        self._register(popen.pid)
        created = creation_token(popen.pid) or ""
        write_owner_only_json(
            directory / PID_FILENAME,
            {
                "pid": popen.pid,
                "created": created,
                "started_at": _iso(started_at),
                "binary": str(binary.path),
            },
        )
        runner = _Runner(
            source_id=source_id,
            popen=popen,
            launch=launch,
            binary=str(binary.path),
            started_at=started_at,
            created=created,
            log=log,
        )
        self._runners[source_id] = runner
        logger.info(
            "PROXY RUNNERS: {} started ({} wireproxy, pid {}, SOCKS5 127.0.0.1:{})",
            source_id,
            binary.origin,
            popen.pid,
            launch.socks_port,
        )
        threading.Thread(
            target=self._read_output, args=(runner,), daemon=True, name="mcc-runner-log"
        ).start()
        threading.Thread(
            target=self._await_ready,
            args=(runner,),
            daemon=True,
            name="mcc-runner-ready",
        ).start()
        return self._running_reading(
            runner,
            STATE_STARTING,
            f"Starting (pid {popen.pid}): waiting for the SOCKS5 port "
            f"127.0.0.1:{launch.socks_port} and the tunnel's check.",
        )

    def _redact(self, text: str, scrub: tuple[str, ...]) -> str:
        for secret in scrub:
            if secret:
                text = text.replace(secret, "[redacted]")
        return redact_sensitive_error_text(text)

    def _read_output(self, runner: _Runner) -> None:
        stream = runner.popen.stdout
        if stream is not None:
            with suppress(OSError, ValueError):
                for raw in iter(stream.readline, b""):
                    line = self._redact(
                        raw.decode("utf-8", "replace").rstrip()[:2000],
                        runner.launch.scrub,
                    )
                    if not line:
                        continue
                    runner.lines.append(line)
                    runner.log.write(line)
                    with self._lock:
                        current = self._hub.reading(runner.source_id)
                        if (
                            self._runners.get(runner.source_id) is runner
                            and current is not None
                        ):
                            self._hub.set_reading(
                                runner.source_id,
                                replace(current, log=tuple(runner.lines)),
                            )
            with suppress(OSError):
                stream.close()
        code = runner.popen.wait()
        self._exited(runner, code)

    def _await_ready(self, runner: _Runner) -> None:
        launch = runner.launch
        began = time.monotonic()
        told_waiting = False
        while runner.popen.poll() is None and not runner.stop_requested:
            if socks_port_accepts(launch.socks_port) and readyz_answers(
                launch.health_port
            ):
                with self._lock:
                    if self._runners.get(runner.source_id) is not runner:
                        return
                    runner.ready_since = _now()
                    self._running_reading(
                        runner,
                        STATE_READY,
                        f"Ready since {_clock(runner.ready_since)} (pid {runner.pid}) "
                        f"on 127.0.0.1:{launch.socks_port}.",
                    )
                return
            waited = time.monotonic() - began
            if waited >= READY_PATIENCE_SECONDS and not told_waiting:
                told_waiting = True
                with self._lock:
                    if self._runners.get(runner.source_id) is runner:
                        target = launch.check_alive or "the tunnel"
                        self._running_reading(
                            runner,
                            STATE_STARTING,
                            f"Still starting (pid {runner.pid}): no answer from "
                            f"{target} through the tunnel yet. The VPN server may "
                            "be refusing this key, or this device.",
                        )
            time.sleep(self._ready_poll if waited < READY_PATIENCE_SECONDS else 2.0)

    def _exited(self, runner: _Runner, code: int) -> None:
        source_id = runner.source_id
        with self._lock:
            self._unregister(runner.pid)
            runner.log.write(f"--- {_iso(_now())} exited with code {code}")
            pid_path = self.directory(source_id) / PID_FILENAME
            with suppress(OSError, ValueError):
                recorded = json.loads(pid_path.read_text(encoding="utf-8"))
                if isinstance(recorded, dict) and recorded.get("pid") == runner.pid:
                    pid_path.unlink(missing_ok=True)
            current = self._runners.get(source_id)
            if current is runner:
                del self._runners[source_id]
            elif current is not None:
                # A newer runner took this source's place (a restart): the
                # old one's exit says nothing about it.
                return
            if runner.stop_requested:
                self._stopped(
                    source_id, runner.stop_sentence or "Stopped.", exit_code=code
                )
                return
            logger.warning(
                "PROXY RUNNERS: {} exited with code {} (pid {})",
                source_id,
                code,
                runner.pid,
            )
            if self._closed:
                self._stopped(source_id, "Stopped with the server.", exit_code=code)
                return
            source = self._load_sources().source(source_id)
            auto = bool(
                source is not None and source.runner and source.runner.auto_restart
            )
            attempts = self._attempts.get(source_id, 0)
            if (
                runner.ready_since is not None
                and (_now() - runner.ready_since).total_seconds() >= self._backoff[-1]
            ):
                # It ran a whole last back-off step before failing: a new
                # failure, not the same one again.
                attempts = 0
            if not auto:
                self._stopped(
                    source_id,
                    f"wireproxy exited with code {code}. Restart automatically is "
                    "off: press Restart.",
                    exit_code=code,
                )
                return
            if attempts >= len(self._backoff):
                self._stopped(
                    source_id,
                    f"Crashed: wireproxy exited with code {code} after "
                    f"{attempts} automatic restarts. Left stopped until you press "
                    "Start.",
                    state=STATE_CRASHED,
                    exit_code=code,
                    restarts=attempts,
                )
                return
            wait = self._backoff[attempts]
            self._attempts[source_id] = attempts + 1
            timer = threading.Timer(
                wait, self._restart_after_backoff, args=(source_id,)
            )
            timer.daemon = True
            self._timers[source_id] = timer
            due = _now() + timedelta(seconds=wait)
            previous = self._hub.reading(source_id)
            self._set(
                source_id,
                RunnerReading(
                    state=STATE_RESTARTING,
                    sentence=(
                        f"wireproxy exited with code {code}; restarting in "
                        f"{wait:g} s (restart {attempts + 1} of {len(self._backoff)})."
                    ),
                    since=_iso(_now()),
                    exit_code=code,
                    restarts=attempts + 1,
                    restart_at=_iso(due),
                    log=previous.log if previous is not None else (),
                ),
            )
            timer.start()

    def _restart_after_backoff(self, source_id: str) -> None:
        with self._lock:
            if self._timers.pop(source_id, None) is None or self._closed:
                return
            self._launch(source_id)

    def _terminate(self, runner: _Runner) -> None:
        """Graceful, then the process tree of that one pid, each step bounded."""

        popen = runner.popen
        if popen.poll() is not None:
            return
        with suppress(OSError):
            popen.terminate()
        try:
            popen.wait(self._graceful)
            return
        except subprocess.TimeoutExpired:
            pass
        self._kill_tree(runner.pid)
        try:
            popen.wait(self._graceful)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                popen.kill()
            with suppress(subprocess.TimeoutExpired):
                popen.wait(self._graceful)


__all__ = [
    "CONFIG_FILENAME",
    "GRACEFUL_SECONDS",
    "LOG_CAP_BYTES",
    "LOG_FILENAME",
    "PID_FILENAME",
    "ProxyRunnerSupervisor",
    "readyz_answers",
    "socks_port_accepts",
]
