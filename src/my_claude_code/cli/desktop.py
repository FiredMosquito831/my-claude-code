"""Thin controller between the tray UI and the MCC server child process.

The fork has no in-process ``ServerSupervisor``: ``mcc-server`` is a blocking
``serve()`` loop, so the tray runs it as a *child process* and drives it over
the loopback admin API. This controller owns that child -- spawn, health check,
restart, stop -- while the tray adapter owns the visible menu.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from my_claude_code.cli.desktop_window import DesktopWindow, create_window
from my_claude_code.cli.launchers.common import (
    PreflightResult,
    preflight_proxy,
    preflight_result,
)
from my_claude_code.cli.port_diagnostics import (
    PortOwner,
    diagnose_port_owner,
    probe_port_available,
)
from my_claude_code.cli.tool_paths import resolve_installed_command
from my_claude_code.config.claude_discovery import native_origin
from my_claude_code.config.desktop import (
    DesktopState,
    apply_start_at_login,
    load_desktop_state,
    remove_start_at_login,
    set_window_open,
)
from my_claude_code.config.desktop_shell import (
    DesktopShellError,
    desktop_shell_enabled,
    ensure_desktop_shell,
)
from my_claude_code.config.paths import DESKTOP_LOCK_FILENAME, config_dir_path
from my_claude_code.config.server_urls import local_admin_url, local_proxy_root_url
from my_claude_code.config.settings import get_settings
from my_claude_code.config.update_progress import active_update
from my_claude_code.core.interprocess_lock import InterprocessFileLock
from my_claude_code.core.startup_state import (
    STARTING_MARKER_HEADER,
    STARTING_MARKER_VALUE,
)
from my_claude_code.core.stop_deadline import (
    HARD_EXIT_GRACE_SECONDS,
    SHUTDOWN_MARKER_HEADER,
    SHUTDOWN_MARKER_VALUE,
    STOP_TEARDOWN_MARGIN_SECONDS,
    clamp_stop_budget,
)

logger = logging.getLogger(__name__)

_SERVER_MODULE = "my_claude_code.cli.entrypoints"

LOCK_FILENAME = DESKTOP_LOCK_FILENAME
ACTIVATION_FILENAME = "desktop.activate"

#: The server's own "open the dashboard when I am healthy" switch, spelled as
#: the environment variable ``Settings.open_admin_browser`` reads
#: (``AliasChoices("MCC_OPEN_BROWSER", "FCC_OPEN_BROWSER")`` -- the canonical
#: name is the first choice, so setting it wins over a legacy value in a
#: ``.env``).
OPEN_BROWSER_ENV = "MCC_OPEN_BROWSER"

#: How often the close watcher samples ``window.is_open`` (seconds).
WINDOW_CLOSE_POLL_SECONDS = 1.0

#: Set to ``1`` to make every launch-time autostart reconciliation a no-op.
#:
#: The OS autostart registration is *machine-global* -- one HKCU ``Run`` value,
#: one LaunchAgent, one XDG ``.desktop`` -- while the preference that drives it
#: lives in ``desktop.json`` *inside whichever config directory is in force*.
#: Those two facts do not compose: running ``mcc-desktop`` against a scratch
#: ``MCC_CONFIG_DIR`` loads a fresh ``desktop.json`` whose ``start_at_login``
#: is the default ``False``, and the reconciliation below then deletes the
#: registration belonging to the user's *real* install. That is not
#: theoretical; it happened during the S5 installer work, to a real HKCU
#: ``Run`` value.
#:
#: So: any test, smoke or installer run that starts a real ``mcc-desktop``
#: against a config directory that is not the user's own must set this. It is
#: deliberately an environment variable and not a flag, because it has to
#: reach a child process nobody in the middle knows how to pass a flag to.
SKIP_AUTOSTART_ENV = "MCC_DESKTOP_SKIP_AUTOSTART"

#: Six distinguishable states of the configured host:port.
#:
#: ``draining`` is the newest and the narrowest: MCC's own server is answering
#: on the port, but it has been asked to stop and its shutdown gate is refusing
#: everything with a 503 until the drain finishes. Before it existed that state
#: read as ``foreign`` -- a port conflict naming MCC's own process as "not the
#: MCC server" -- which is the page a user landed on every time they relaunched
#: the desktop app during a slow restart.
#:
#: It is reported only to a caller that asks for it (``presence_v2=True``).
#: A reader written before this value existed has no branch for it, and the
#: desktop shell refuses a presence it does not know rather than guessing, so
#: an old window against a new wheel must keep seeing the old three.
#:
#: ``starting`` is MCC's own server, bound and answering, but still working
#: through its startup. Added in 6.59.0 with the bind-first listener: before it, the whole
#: of a twenty-second start read as ``free``, and "free" is what a shell, a
#: tray or a launcher reads as licence to start a server -- so a start could
#: not be told from an absence, and a second server was routinely spawned into
#: the bind race the first one was about to win.
#:
#: ``mcc-stale`` is 6.59.0's other new value: MCC's own process holds the
#: port and answers nothing at all. Measured on this machine -- while a
#: freshly ready server builds its first provider generation it blocks its
#: event loop for seconds at a time, so a probe times out -- and every
#: release before 6.59.0 read "held port, no answer" as a *stranger* and told
#: the user that something else was on the port. It was MCC's own python.exe
#: every time. Deciding this by process rather than by the HTTP answer is the
#: whole of the fix, and it leaves ``foreign`` meaning what it says.
#:
#: Both are behind ``presence_v2``, for the compatibility reason above.
type ServerPresence = Literal[
    "healthy", "foreign", "free", "draining", "starting", "mcc-stale"
]

#: Every value ``probe_server_presence`` can return, opt-in included. Any table
#: that enumerates presences is checked against this.
SERVER_PRESENCES: tuple[ServerPresence, ...] = (
    "healthy",
    "free",
    "foreign",
    "draining",
    "starting",
    "mcc-stale",
)


#: Seconds allowed for the OS to reap a process after the final ``kill``.
FORCE_KILL_REAP_SECONDS = 5.0


def server_stop_wait_seconds(settings: Any) -> float:
    """Seconds the tray waits between "please stop" and "you are being killed".

    The child's own supervisor bounds its stop at
    ``SERVER_GRACEFUL_SHUTDOWN_SECONDS`` plus a fixed teardown margin, and its
    watchdog hard-exits one beat after that. Waiting exactly that long -- rather
    than the hard-coded 5s the tray used to wait -- means the tray never kills a
    server that is legitimately mid-drain under its own configured budget, and
    never waits on one that has stopped honouring it.
    """

    budget = clamp_stop_budget(
        getattr(settings, "server_graceful_shutdown_seconds", 0.0)
    )
    return budget + STOP_TEARDOWN_MARGIN_SECONDS + HARD_EXIT_GRACE_SECONDS


SERVER_DOWN_NOTIFICATION = (
    "The MCC server stopped answering. Choose Restart Server in this menu, "
    "or run mcc-server in a terminal to see why it exited."
)
SERVER_RECOVERED_NOTIFICATION = "The MCC server is answering again."


def is_draining_response(result: PreflightResult) -> bool:
    """Whether this ``/health`` answer is MCC's own shutdown gate.

    The gate refuses every request with 503 from the instant a stop is
    requested (``runtime/asgi.py``), and it stamps
    ``x-mcc-shutdown: 1`` on the refusal precisely so this question has an
    answer. The ``retry-after`` fallback exists for one release of overlap: a
    server from before the marker still sends 503 + ``retry-after``, and
    reading that as a drain is a far better guess than reading it as a
    stranger on the port. Neither branch fires on a plain 503 from something
    that is not MCC, which is the distinction the whole value exists for.
    """

    if result.status_code != 503:
        return False
    if result.header(SHUTDOWN_MARKER_HEADER) == SHUTDOWN_MARKER_VALUE:
        return True
    # The startup gate sends a ``retry-after`` too, and an explicit marker must
    # always beat the heuristic that exists only for servers too old to send
    # one. Without this line 6.59.0's own starting server reads as draining --
    # which is a wait for something that is going away rather than coming up.
    if result.header(STARTING_MARKER_HEADER) == STARTING_MARKER_VALUE:
        return False
    return bool((result.header("retry-after") or "").strip())


def is_starting_response(result: PreflightResult) -> bool:
    """Whether this ``/health`` answer is MCC's own startup gate.

    Strictly by the marker header, with no ``retry-after`` fallback of the kind
    ``is_draining_response`` keeps for one release of overlap. There is nothing
    to be compatible with: no release before 6.59.0 answered anything at all
    while it was starting, so a 503 without this header is somebody else's.
    """

    if result.status_code != 503:
        return False
    return result.header(STARTING_MARKER_HEADER) == STARTING_MARKER_VALUE


def starting_stage(result: PreflightResult) -> str | None:
    """The stage named in a "starting" refusal, if it named one."""

    if not result.body.strip():
        return None
    try:
        payload = json.loads(result.body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    stage = payload.get("stage")
    return stage if isinstance(stage, str) and stage.strip() else None


def probe_server_presence(
    settings: Any, *, presence_v2: bool = False
) -> ServerPresence:
    """Tell a healthy MCC apart from a stranger holding the port.

    ``preflight_result`` answers only one question -- does a healthy MCC reply
    on this port -- and its "no" covers three very different worlds: nothing is
    listening, something that is not MCC is, or MCC itself is mid-drain and
    refusing new work on purpose. Spawning into the second case produces a bind
    failure with no explanation, so the port is probed read-only to separate
    them; the third is separated by the shutdown gate's own marker header
    rather than by another probe.

    ``presence_v2`` is the opt-in for the third answer. Without it this returns
    exactly the three values it always has, so a reader written against the old
    ladder cannot be handed a value it has no branch for.
    """

    return probe_server_state(settings, presence_v2=presence_v2).presence


@dataclass(frozen=True, slots=True)
class ServerState:
    """One probe's whole answer: the presence, and the stage when starting."""

    presence: ServerPresence
    #: The startup stage named by a ``starting`` refusal, else ``None``. It is
    #: the server's own stage name (``configured-models``, ``catalogue``...),
    #: passed through verbatim rather than translated here, so a reader shows
    #: what the log shows.
    stage: str | None = None


def probe_server_state(settings: Any, *, presence_v2: bool = False) -> ServerState:
    """One probe, reported in full. See :func:`probe_server_presence`."""

    result = preflight_result(local_proxy_root_url(settings))
    if result.ok:
        return ServerState("healthy")
    # Drain before start, deliberately: a server asked to stop during a slow
    # start answers with the shutdown marker, and a caller told "starting"
    # about a server that is on its way out would wait for something that is
    # never coming back.
    if presence_v2 and is_draining_response(result):
        return ServerState("draining")
    if is_starting_response(result):
        # Not gated on ``presence_v2`` inside this function: the caller that
        # asked for the old three values gets them from the mapping below, and
        # every other caller -- the tray, the launchers -- wants the truth.
        state = ServerState("starting", starting_stage(result))
        return state if presence_v2 else ServerState("free", state.stage)
    host = (settings.host or "127.0.0.1").strip()
    if probe_port_available(host, settings.port):
        return ServerState("free")
    if presence_v2 and port_is_held_by_mcc(host, settings.port):
        # Held, not answering, and demonstrably ours. Never ``foreign``: the
        # port-conflict page tells the user to stop another program and change
        # the port, and following that advice about MCC's own server is how a
        # routine slow start became a dead end.
        return ServerState("mcc-stale")
    return ServerState("foreign")


def port_is_held_by_mcc(host: str, port: int) -> bool:
    """Whether the process holding ``host:port`` is one of MCC's own.

    Imported lazily so the probe stays as cheap as its docstring promises: a
    caller that finds a healthy server never pays for the process lookup, and
    only the "held but silent" branch shells out at all. The listener itself is
    found through this module's own ``diagnose_port_owner``, the same call the
    port-conflict message uses, so one probe answers both questions.
    """

    from my_claude_code.cli.port_takeover import identity_for_owner

    identity = identity_for_owner(diagnose_port_owner(host, port))
    return identity is not None and identity.is_mcc


def stale_server_message(settings: Any) -> str:
    """What to say about MCC's own server that holds the port and is silent."""

    return (
        f"A My Claude Code server is holding port {settings.port} but is not "
        f"answering. It is starting, busy, or wedged. Starting the server "
        f"again takes the port back from it."
    )


def starting_message(settings: Any, stage: str | None = None) -> str:
    """What to say about a server that has bound its port and is coming up."""

    where = f" ({stage})" if stage else ""
    return (
        f"The My Claude Code server on port {settings.port} has started and is "
        f"still loading{where}. It answers as soon as it is ready; starting a "
        f"second one would only take the port away from it."
    )


def draining_message(settings: Any) -> str:
    """What to say about a server that is stopping under its own budget."""

    return (
        f"The My Claude Code server on port {settings.port} is shutting down "
        f"and is refusing new requests until it has finished. It stops within "
        f"its configured graceful-shutdown budget; wait for it, then start the "
        f"desktop app again."
    )


def updating_message(record: dict[str, Any]) -> str:
    """What to say about an update helper that is mid-install.

    Names the version and how long it has been going, because the sentence it
    replaces ("could not start the server") sent the user off to reinstall by
    hand -- into the same tool directory the helper was writing.
    """

    version = str(record.get("version") or "").strip()
    target = f" to {version}" if version else ""
    started = record.get("started_at")
    elapsed = ""
    if isinstance(started, int | float):
        seconds = max(0, int(time.time() - float(started)))
        elapsed = f" ({seconds}s so far)"
    return (
        f"My Claude Code is updating{target}: the installer is running{elapsed}. "
        f"It starts the server itself when it is done -- including when the "
        f"install fails, in which case the previous version comes back."
    )


def port_conflict_message(settings: Any) -> str:
    """Name the process holding the port, not just the fact that it is held."""

    host = (settings.host or "127.0.0.1").strip()
    owner: PortOwner | None = diagnose_port_owner(host, settings.port)
    if owner is None:
        holder = "another program"
    elif owner.name and owner.pid:
        holder = f"{owner.name} (pid {owner.pid})"
    elif owner.pid:
        holder = f"pid {owner.pid}"
    else:
        holder = owner.name or "another program"
    return (
        f"Port {settings.port} is held by {holder}, which is not the MCC "
        f"server. Stop it, or change the port in the admin dashboard, then "
        f"start the desktop app again."
    )


class HealthTracker:
    """Debounce health probes so a self-update restart is not read as death.

    The server can replace its own process during an update, and the dashboard
    already has its own reconnect state machine. A single failed probe
    therefore means nothing: only ``threshold`` consecutive failures raise a
    notification, and only one notification is raised per outage.
    """

    def __init__(self, threshold: int | None = None) -> None:
        resolved = (
            threshold
            if threshold is not None
            else get_settings().desktop_health_failure_threshold
        )
        self._threshold = max(1, resolved)
        self._failures = 0
        self._notified = False

    def record(self, healthy: bool) -> bool:
        """Record one probe; return True exactly when an outage begins."""

        if healthy:
            self._failures = 0
            self._notified = False
            return False
        self._failures += 1
        if self._failures < self._threshold or self._notified:
            return False
        self._notified = True
        return True

    def record_recovery(self, healthy: bool) -> bool:
        """Return True when a probe ends an outage we already reported."""

        return healthy and self._notified


class ActivationSignal:
    """Cross-process "show the window" doorbell backed by one small file.

    A file is chosen over a loopback socket deliberately. Binding a listener
    raises the Windows Defender firewall prompt on first run; a port can be
    taken by exactly the unrelated program the desktop app is already fighting
    over; and the config directory already hosts ``desktop.lock``, so the
    singleton and its doorbell live together. The cost is polling, which at a
    one-second cadence is free.

    Staleness is handled by the reader, not the writer: the instance that owns
    the lock clears the file before it starts watching, so a signal left behind
    by an instance that died without cleanup is discarded instead of causing a
    spurious activation on the next launch.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._seen = ""

    def clear(self) -> None:
        with suppress(OSError):
            self._path.unlink(missing_ok=True)
        self._seen = ""

    def signal(self) -> None:
        token = f"{time.time():.6f}"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
            tmp_path.write_text(token, encoding="utf-8")
            os.replace(tmp_path, self._path)
        except OSError:
            return

    def poll(self) -> bool:
        """Return True once per new signal written by another process."""

        try:
            token = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if not token or token == self._seen:
            return False
        self._seen = token
        return True


class DesktopController:
    """Spawn and supervise the MCC server from the desktop tray.

    The server is always a separate process; this controller never imports or
    embeds the blocking ``serve()`` loop. Every operation goes through the
    loopback admin API, which requires no token.
    """

    def __init__(
        self, *, lock: InterprocessFileLock, window: DesktopWindow | None = None
    ) -> None:
        self._lock = lock
        self._process: subprocess.Popen[bytes] | None = None
        self._window = window
        self._health_stop: threading.Event | None = None

    @property
    def status(self) -> str:
        error = preflight_proxy(local_proxy_root_url(get_settings()))
        return "running" if error is None else "stopped"

    def server_mode(self) -> str:
        """Return the persisted server-ownership mode (``spawn|attach|off``)."""

        return load_desktop_state().server_mode

    # -- process management ------------------------------------------------

    def ensure_server(self) -> None:
        """Spawn ``mcc-server`` only when the tray owns it (``spawn``).

        In ``attach`` and ``off`` the desktop app never starts the server:
        ``attach`` health-checks and reports an existing server, ``off`` does
        not touch the server at all.
        """

        if load_desktop_state().server_mode != "spawn":
            return
        settings = get_settings()
        state = probe_server_state(settings, presence_v2=True)
        presence = state.presence
        if presence == "healthy":
            return
        if presence == "starting":
            # MCC's own server, bound and working through its startup. The
            # whole point of the bind-first listener is that this is now
            # visible: before 6.59.0 it read as a free port, and a free port is
            # what this method reads as "start one".
            raise DesktopError(starting_message(settings, state.stage))
        # An update helper is replacing the tool environment this very command
        # lives in. Spawning here starts a server out of an install that is
        # half-written, and the helper starts one itself the moment it is done
        # -- on both branches now, success or failure -- so the only thing a
        # spawn can add is a second server racing for the port.
        in_flight = active_update()
        if in_flight is not None:
            raise DesktopError(updating_message(in_flight))
        if presence == "draining":
            # MCC's own server, mid-stop. Neither a conflict to report nor a
            # port to bind: a spawn here loses the race with the socket the
            # draining process still holds, and the old code called it a
            # stranger on the port.
            raise DesktopError(draining_message(settings))
        if presence == "mcc-stale":
            # Ours, holding the port, silent. The server's own takeover deals
            # with it on the next start; what this must not do is call it a
            # stranger.
            raise DesktopError(stale_server_message(settings))
        if presence == "foreign":
            raise DesktopError(port_conflict_message(settings))
        self._spawn_server(settings)

    def _spawn_server(self, settings: Any) -> None:
        command = self._server_command()
        if command is None:
            raise DesktopError(
                "Could not resolve mcc-server. Reinstall My Claude Code so the "
                "server command is on PATH, or start it manually."
            )
        try:
            # CREATE_NEW_PROCESS_GROUP is what makes a graceful stop possible at
            # all on Windows: MCC installs no signal handlers of its own, so the
            # only cooperative stop is uvicorn's, and uvicorn's Windows handler
            # is on SIGBREAK. A console control event can only be sent to a
            # process GROUP, so the child has to be the leader of its own.
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            )
            self._process = subprocess.Popen(
                command,
                creationflags=creationflags,
                env=self._server_environment(),
            )
        except OSError as exc:
            raise DesktopError(f"Could not start the MCC server: {exc}") from exc

        # The same total budget the desktop window uses: the timeout is one
        # attempt, and ``desktop_server_start_retries`` buys the rest. A flat
        # 15s against a 22-25s startup is why "the server did not answer" was
        # reported for servers that answered seven seconds later.
        budget = settings.desktop_server_start_timeout * (
            max(0, int(settings.desktop_server_start_retries)) + 1
        )
        deadline = time.monotonic() + budget
        root_url = local_proxy_root_url(settings)
        while time.monotonic() < deadline:
            if preflight_proxy(root_url) is None:
                return
            time.sleep(settings.desktop_health_check_interval)
        raise DesktopError(
            "The MCC server did not become healthy in time. It may be starting "
            "still, or it failed to bind its port."
        )

    def _server_command(self) -> list[str] | None:
        binary = resolve_installed_command("mcc-server")
        if binary is not None:
            return [binary]
        return [sys.executable, "-m", _SERVER_MODULE]

    @staticmethod
    def _server_environment() -> dict[str, str]:
        """Return the child server's environment, with its browser open silenced.

        ``mcc-server`` opens the dashboard in the default browser once it is
        healthy (``Settings.open_admin_browser``, on by default), which is
        right when a human ran it in a terminal and wrong in every case that
        reaches this method: the desktop app is *about* to show the dashboard
        in a window it owns. Left alone, one ``mcc-desktop`` launch produced
        two dashboards -- the app's window and a browser tab nobody asked for.

        The desktop state is the authority on windows here, not the server's
        default. That holds for ``window_open=False`` too: a user who closed
        the window and relaunched to the tray asked for no window, and a
        browser tab is still a window.
        """

        environment = dict(os.environ)
        environment[OPEN_BROWSER_ENV] = "0"
        return environment

    def _stop_child(self) -> None:
        """Ask the server child to stop, wait its own budget, then escalate.

        Three steps, in order, and every one of them addressed to the exact PID
        this controller launched -- never a name, never a path match:

        1. **Ask.** A console control event (Windows) or ``SIGTERM`` (POSIX)
           reaches uvicorn's handler and starts a real graceful drain. The tray
           used to skip this entirely: ``terminate()`` on Windows is
           ``TerminateProcess``, which is a kill, so a tray stop cut every
           in-flight request instantly however long the operator's budget was.
        2. **Wait the server's own bound**, not a hard-coded 5 seconds. The
           child stops within ``SERVER_GRACEFUL_SHUTDOWN_SECONDS`` plus its
           teardown margin, and hard-exits itself one beat later.
        3. **Escalate.** A child that outlived its whole configured budget is
           not draining any more, so terminate and then kill it. A tray that
           hangs forever is worse than a server killed after it was given every
           second it asked for.
        """

        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is not None:
            return
        budget = server_stop_wait_seconds(get_settings())
        self._request_graceful_stop(process)
        try:
            process.wait(timeout=budget)
            return
        except subprocess.TimeoutExpired:
            logger.warning(
                "MCC server pid %s did not stop within %.1fs; terminating it.",
                process.pid,
                budget,
            )
        process.terminate()
        try:
            process.wait(timeout=FORCE_KILL_REAP_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=FORCE_KILL_REAP_SECONDS)

    @staticmethod
    def _request_graceful_stop(process: subprocess.Popen[bytes]) -> None:
        """Send the cooperative stop signal to exactly this child."""

        try:
            if os.name == "nt":
                # Delivered to the group the child leads (see _spawn_server);
                # on Windows a console control event can only be addressed to a
                # group, never to a single process.
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGTERM)
        except (OSError, ValueError) as exc:
            # A child that is already gone, or a platform that refuses the
            # event, simply falls through to the escalation below.
            logger.debug(
                "Graceful stop request failed for pid %s: %s", process.pid, exc
            )

    # -- admin API ---------------------------------------------------------

    def _admin_json(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        root = local_proxy_root_url(get_settings())
        url = f"{root.rstrip('/')}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            url,
            method=method,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urlopen(
                request, timeout=get_settings().desktop_admin_request_timeout
            ) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise DesktopError(
                f"Admin API returned HTTP {exc.code}: {detail.strip() or exc.reason}"
            ) from exc
        except URLError as exc:
            raise DesktopError(f"Admin API is unreachable: {exc.reason}") from exc
        except OSError as exc:
            raise DesktopError(f"Admin API request failed: {exc}") from exc

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    # -- menu actions ------------------------------------------------------

    def attach_window(self, window: DesktopWindow) -> None:
        """Give the controller the window provider chosen at launch."""

        self._window = window

    @property
    def window(self) -> DesktopWindow | None:
        return self._window

    def show_window(self) -> None:
        """Show the dashboard window, raising an existing one when possible."""

        window = self._window
        if window is None:
            return
        if window.is_open and window.focus():
            set_window_open(True)
            return
        window.open(local_admin_url(get_settings()))
        set_window_open(True)

    def close_window(self) -> None:
        """Close the window only.

        Closing a window and quitting the app are different actions: the server
        keeps running either way, and in ``attach``/``off`` mode it was never
        ours to stop.
        """

        window = self._window
        if window is not None:
            window.close()
        set_window_open(False)

    def handle_window_closed(self) -> bool:
        """Return True when the app should keep running after a window close.

        ``close_to_tray`` is the user's answer to "did closing the window mean
        quit?", and since 6.50.0 the answer defaults to no. With a tray to fall
        back to, closing hides the window; with no tray, closing is the only
        way out, so it ends the app.

        Only a close that the app SURVIVES records "no window". When the close
        ends the app the user's last intent was an app with a window, so the
        state is left alone. Recording it either way would mean the ordinary
        act of closing the app stopped it ever opening a window again.
        """

        state = load_desktop_state()
        keep_running = state.close_to_tray and state.tray_enabled
        if keep_running:
            set_window_open(False)
        return keep_running

    # -- health monitor ----------------------------------------------------

    def start_health_monitor(
        self,
        on_unhealthy: Callable[[], None],
        *,
        on_recovered: Callable[[], None] | None = None,
        interval: float | None = None,
        threshold: int | None = None,
    ) -> None:
        """Poll the server on a daemon thread and report an outage once.

        This only *reports*. It never respawns: in ``attach``/``off`` mode the
        server is not ours, and in ``spawn`` mode a silent respawn would race
        the server's own self-update restart.
        """

        if self._health_stop is not None:
            return
        settings = get_settings()
        if interval is None:
            interval = settings.desktop_health_poll_seconds
        if threshold is None:
            threshold = settings.desktop_health_failure_threshold
        stop = threading.Event()
        self._health_stop = stop
        threading.Thread(
            target=self._health_loop,
            args=(on_unhealthy, on_recovered, interval, threshold, stop),
            name="mcc-desktop-health",
            daemon=True,
        ).start()

    def stop_health_monitor(self) -> None:
        stop = self._health_stop
        self._health_stop = None
        if stop is not None:
            stop.set()

    def _health_loop(
        self,
        on_unhealthy: Callable[[], None],
        on_recovered: Callable[[], None] | None,
        interval: float,
        threshold: int,
        stop: threading.Event,
    ) -> None:
        tracker = HealthTracker(threshold)
        root_url = local_proxy_root_url(get_settings())
        while not stop.wait(interval):
            healthy = preflight_proxy(root_url) is None
            recovered = tracker.record_recovery(healthy)
            if tracker.record(healthy):
                with suppress(Exception):
                    on_unhealthy()
            elif recovered and on_recovered is not None:
                with suppress(Exception):
                    on_recovered()

    # -- menu actions ------------------------------------------------------

    def open_admin(self) -> None:
        self.show_window()

    def check_status(self) -> str:
        return self.status

    def restart_server(self) -> None:
        """Restart the running server; if it is down, spawn it fresh.

        In ``attach``/``off`` mode the tray never owns a server, so a restart
        raises :class:`DesktopError` instead of silently spawning one. Prefer
        the loopback ``POST /admin/api/config/apply`` no-op so the server's own
        graceful-drain machinery performs the reload. Fall back to a hard kill
        + respawn when the API is unreachable but the health probe said the
        server was up (a race), or when the caller holds a child.
        """

        if self.server_mode() != "spawn":
            raise DesktopError(
                "Server is managed by the deployment mode; restart is only "
                "available when Server mode is set to 'spawn'."
            )

        settings = get_settings()
        root_url = local_proxy_root_url(settings)
        # The same single probe every other presence question in this file goes
        # through, so there is one place that decides what "the server is
        # answering" means.
        if not preflight_result(root_url).ok:
            self.ensure_server()
            return

        try:
            self._admin_json("POST", "/admin/api/config/apply", {"values": {}})
        except DesktopError:
            # The config apply is unreachable even though the health probe just
            # passed. Kill any child we own and let ensure_server respawn; if we
            # do not own a child, leave the running server alone.
            self._stop_child()
            if not preflight_result(root_url).ok:
                self.ensure_server()

    def stop(self) -> None:
        """Stop the server child we own and release the singleton lock.

        Only the child is stopped; a server the user started outside the tray
        is left running, because the tray is a controller, not an owner.

        The window is closed without recording ``window_open=False``,
        matching :meth:`handle_window_closed`: an app-ending close leaves the
        state alone, because the user's last intent was an app *with* a
        window and the next launch should restore it.
        """

        self.stop_health_monitor()
        window = self._window
        if window is not None:
            window.close()
        self._stop_child()
        self._lock.release()

    def quit(self) -> None:
        """Stop the child server and release the lock; the tray loop ends itself."""

        self.stop()


def _poll_window_transition(
    was_open: bool, controller: DesktopController, tray: Any
) -> bool:
    """Fold one ``is_open`` observation into close handling; return the state.

    Only a True->False edge is acted on: a close the app survives records
    "no window", while a close that ends it stops the tray so its ``run()``
    returns and the launch's finally-block unwinds everything else.
    """

    window = controller.window
    is_open = window is not None and bool(window.is_open)
    if was_open and not is_open and not controller.handle_window_closed():
        tray.stop()
    return is_open


def _watch_window_close(
    controller: DesktopController,
    tray: Any,
    stop: threading.Event,
    interval: float,
) -> None:
    """Watch the window provider's ``is_open`` edge until asked to stop."""

    was_open = False
    while not stop.wait(interval):
        was_open = _poll_window_transition(was_open, controller, tray)


class DesktopError(Exception):
    """Raised when the desktop controller cannot complete an operation."""


def headless_refusal_reason() -> str | None:
    """Return why a desktop window cannot be shown here, or ``None``.

    Reuses the discovery module's platform detection rather than re-sniffing
    ``/proc``. A refusal names the working alternative instead of reporting the
    missing display.
    """

    origin = native_origin()
    if origin in {"windows", "macos"}:
        return None
    # Quote the configured address, never the default: a message that names
    # :8082 on a machine serving :9000 sends the reader somewhere empty.
    admin_url = local_admin_url(get_settings())
    if origin == "wsl":
        return (
            "mcc-desktop needs a desktop session, and WSL does not have one. "
            "Run mcc-server inside WSL instead -- it is headless -- and open "
            f"the dashboard at {admin_url} in a Windows browser, which "
            "reaches the WSL port directly."
        )
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        # Linux has a desktop session here, and since 6.44.0 it can have a
        # window and a tray as well -- both from the shell, because pystray is
        # declared win32/darwin only and always will be. The refusal therefore
        # became conditional rather than disappearing: with the shell, Linux
        # is supported; without it, nothing on this machine can draw a tray and
        # the old advice is still the right advice.
        unavailable = desktop_shell_unavailable_reason()
        if unavailable is None:
            return None
        return (
            "mcc-desktop's tray lives in the Windows and macOS status areas, "
            "and the My Claude Code desktop app -- which carries its own tray "
            f"on Linux -- is not available here: {unavailable} Run mcc-server "
            f"instead -- it is headless -- and open {admin_url} in this "
            "machine's browser."
        )
    return (
        "mcc-desktop needs a desktop session, and neither DISPLAY nor "
        "WAYLAND_DISPLAY is set. Run mcc-server instead -- it is headless -- "
        f"and open {admin_url} from a browser on any machine that can reach "
        "this one."
    )


def desktop_shell_unavailable_reason() -> str | None:
    """Return why the desktop shell cannot be used here, or ``None``.

    Installing it is the check, because "can this machine have a window" and
    "is the shell installed" are the same question and the honest way to answer
    the second is to try. The attempt costs one JSON read once the shell is in
    place -- :func:`ensure_desktop_shell` short-circuits on its install
    receipt -- so asking here and again when the window chain is built does not
    download anything twice.
    """

    if not desktop_shell_enabled():
        return "it is switched off with DESKTOP_SHELL=off."
    try:
        ensure_desktop_shell()
    except DesktopShellError as exc:
        return str(exc)
    return None


class WindowOnlyHost:
    """A no-op stand-in for the tray, for platforms that do not have one.

    ``launch_desktop`` is built around a tray adapter: it is what owns the
    thread the process blocks on and what a window close stops. On Linux there
    is no pystray to provide one and the shell draws its own icon, so this
    supplies the shape without the icon -- block until stopped, and stop when
    the window closes.
    """

    def __init__(self, controller: DesktopController) -> None:
        self._controller = controller
        self._stopped = threading.Event()

    def run(self) -> None:
        self._stopped.wait()

    def stop(self) -> None:
        self._stopped.set()


def autostart_reconcile_enabled() -> bool:
    """Return whether launch-time autostart reconciliation may touch the OS.

    ``MCC_DESKTOP_SKIP_AUTOSTART=1`` turns it off. See ``SKIP_AUTOSTART_ENV``
    for why that switch has to exist. Only the exact value ``1`` disables it,
    so an empty or accidental value keeps the normal behaviour.
    """

    return os.environ.get(SKIP_AUTOSTART_ENV, "").strip() != "1"


def _reconcile_start_at_login(state: DesktopState) -> None:
    """Make the OS registration match what the admin API persisted.

    The admin API only writes the JSON file -- it may be running headless,
    with no tray or desktop session -- so this launch-time step reconciles
    the flag with the OS ("The next ``mcc-desktop``/tray launch reconciles
    the file with the OS"). Both directions are idempotent. A disabled tray
    never registers: an invisible tray must not relaunch at login.

    ``MCC_DESKTOP_SKIP_AUTOSTART=1`` makes the whole thing a no-op, because
    the registration is machine-global and the preference driving it is not.
    """

    if not autostart_reconcile_enabled():
        logger.info(
            "%s=1: leaving the start-at-login registration untouched.",
            SKIP_AUTOSTART_ENV,
        )
        return

    try:
        if state.tray_enabled and state.start_at_login:
            apply_start_at_login()
        else:
            remove_start_at_login()
    except Exception:
        logger.warning(
            "Could not reconcile the start-at-login registration.", exc_info=True
        )


def launch_desktop(
    tray_factory: Any,
    *,
    window_factory: Callable[[str], DesktopWindow] = create_window,
) -> None:
    """Start the singleton desktop host, or activate the one already running.

    ``tray_factory`` is a callable that receives the controller and returns an
    object exposing ``run()`` / ``stop()`` -- the pystray adapter.

    A second launch does not open anything of its own. It rings the running
    instance's doorbell and exits, so "launch again" means "show me the window
    I already have" rather than "make me a second one".
    """

    state = load_desktop_state()
    signal = ActivationSignal(config_dir_path() / ACTIVATION_FILENAME)
    instance_lock = InterprocessFileLock(config_dir_path() / LOCK_FILENAME)
    if not instance_lock.acquire():
        signal.signal()
        return

    signal.clear()
    _reconcile_start_at_login(load_desktop_state())
    controller = DesktopController(
        lock=instance_lock, window=window_factory(state.window)
    )
    tray = tray_factory(controller)
    notify = _tray_notifier(tray)
    stop_watching = threading.Event()
    watcher = threading.Thread(
        target=_watch_activation,
        args=(signal, controller, stop_watching),
        name="mcc-desktop-activation",
        daemon=True,
    )
    close_watcher = threading.Thread(
        target=_watch_window_close,
        args=(controller, tray, stop_watching, WINDOW_CLOSE_POLL_SECONDS),
        name="mcc-desktop-window-close",
        daemon=True,
    )
    try:
        controller.ensure_server()
        if state.window_open:
            controller.show_window()
        controller.start_health_monitor(
            lambda: notify(SERVER_DOWN_NOTIFICATION),
            on_recovered=lambda: notify(SERVER_RECOVERED_NOTIFICATION),
        )
        watcher.start()
        close_watcher.start()
        tray.run()
    finally:
        stop_watching.set()
        controller.stop()
        signal.clear()


def _tray_notifier(tray: Any) -> Callable[[str], None]:
    """Return a notifier that tolerates a tray adapter without notifications."""

    notify = getattr(tray, "notify", None)
    if not callable(notify):
        return lambda _message: None

    def _notify(message: str) -> None:
        with suppress(Exception):
            notify(message)

    return _notify


def _watch_activation(
    signal: ActivationSignal,
    controller: DesktopController,
    stop: threading.Event,
) -> None:
    while not stop.wait(get_settings().desktop_activation_poll_seconds):
        if signal.poll():
            with suppress(Exception):
                controller.show_window()
