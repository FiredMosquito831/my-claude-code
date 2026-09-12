"""Implementations for installed Free Claude Code commands."""

import errno
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from types import FrameType

import uvicorn
from loguru import logger

from my_claude_code.cli.first_start import ensure_config_home_or_exit
from my_claude_code.cli.launchers.common import preflight_proxy
from my_claude_code.cli.port_diagnostics import (
    diagnose_port_owner,
    is_address_in_use,
    probe_port_available,
    wait_for_port_free,
)
from my_claude_code.cli.port_takeover import take_port
from my_claude_code.cli.process_registry import kill_all_best_effort
from my_claude_code.config.env_migrations import (
    explicit_env_file_migration_warning,
    migrate_owned_env_files,
)
from my_claude_code.config.env_template import render_default_env
from my_claude_code.config.logging_config import append_to_server_log
from my_claude_code.config.paths import (
    config_dir_path,
    config_dir_resolution,
    legacy_env_paths,
    managed_env_path,
    new_config_dir_path,
    other_servers_path,
    request_log_path,
    server_log_path,
)
from my_claude_code.config.proxy_auth import open_proxy_without_auth_error
from my_claude_code.config.server_urls import local_admin_url, local_proxy_root_url
from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.process_handoff import external_upgrade_helper_pending
from my_claude_code.core.request_log import set_server_bind_address
from my_claude_code.core.server_inventory import (
    observe_servers,
    report_servers,
    stop_stale_servers,
    write_survey,
)
from my_claude_code.core.startup_state import LISTENER_STAGE, startup_state
from my_claude_code.core.stop_deadline import (
    HARD_EXIT_GRACE_SECONDS,
    STOP_TEARDOWN_MARGIN_SECONDS,
    clamp_stop_budget,
    stop_deadline,
)
from my_claude_code.runtime.bootstrap import build_asgi_app

_WINDOWS = os.name == "nt"

#: Pending-connection queue for the listening socket. uvicorn's own
#: default, kept so binding the socket here changes nothing but who
#: owns it.
SERVER_BACKLOG = 2048


class ServerExitAction(Enum):
    """What the supervisor does after one fully closed server generation."""

    STOP = "stop"
    RELOAD = "reload"
    REPLACE_PROCESS = "replace_process"


# Higher priority wins, so a later, weaker request cannot downgrade a more
# severe one already in flight. REPLACE_PROCESS (a self-update) must not be
# quietly turned into a RELOAD by a config-driven restart that arrives while
# the runtime is shutting down.
_ACTION_PRIORITY = {
    ServerExitAction.STOP: 0,
    ServerExitAction.RELOAD: 1,
    ServerExitAction.REPLACE_PROCESS: 2,
}


def _server_launcher() -> str | None:
    """Return the stable launcher outside the uv-managed tool environment."""
    bin_dir = _uv_tool_bin_dir()
    if bin_dir is not None:
        candidate = bin_dir / ("mcc-server.exe" if os.name == "nt" else "mcc-server")
        if candidate.is_file():
            return str(candidate)
    return shutil.which("mcc-server")


def _uv_tool_bin_dir() -> Path | None:
    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        completed = subprocess.run(
            [uv, "tool", "dir", "--bin"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    path = completed.stdout.strip()
    return Path(path) if completed.returncode == 0 and path else None


def _replace_server_process(settings: Settings) -> None:
    """Hand off to the updated server after the old runtime fully closes."""
    # Windows cannot replace the environment until this interpreter exits. Its
    # external PowerShell helper is already waiting and will launch the stable
    # shim after a successful install, so the only safe action here is to flush
    # and return from serve().
    if _WINDOWS or external_upgrade_helper_pending():
        logger.info("Server closed; the update helper will install and restart it.")
        logger.complete()
        kill_all_best_effort()
        return

    launcher = _server_launcher()
    if launcher is None:
        logger.error("Updated successfully, but mcc-server could not be found on PATH.")
        return
    # The graceful drain already finished (server.run() returned), but the OS can
    # still hold the listening socket for a beat afterward -- especially under
    # WSL. Wait a bounded moment for it to release so the new process binds
    # cleanly instead of failing its own bind and dying. Read-only probe: it
    # never kills anything and returns within the budget regardless.
    wait_budget = max(5.0, min(float(settings.server_graceful_shutdown_seconds), 30.0))
    if not wait_for_port_free(settings.host, settings.port, timeout=wait_budget):
        logger.warning(
            "Port {host}:{port} did not free within {budget}s before restart; "
            "the new server will wait again on bind.",
            host=settings.host,
            port=settings.port,
            budget=wait_budget,
        )
    # ``enqueue=True`` logging uses a background queue. exec() destroys its
    # writer thread, so wait until every queued record reaches its sink first.
    logger.info("Restarting with the updated server...")
    logger.complete()
    kill_all_best_effort()
    recovery_command = " ".join([launcher, *sys.argv[1:]])
    try:
        os.execv(launcher, [launcher, *sys.argv[1:]])
    except OSError as exc:
        # The new image failed to launch in place. Flush the notice and leave a
        # recovery command the operator can run by hand.
        logger.error(
            "Updated server launch failed ({}). Run the updated server manually: {}",
            exc,
            recovery_command,
        )
        logger.complete()
        return


def _start_stop_clock_on_signal(
    server: uvicorn.Server, arm: Callable[[], None]
) -> None:
    """Make uvicorn's own signal handler start the supervisor's stop clock.

    Shadowing the bound method on the instance rather than subclassing keeps
    this one line away from uvicorn's constructor signature, which the
    supervisor does not own and which changes between releases.
    """

    original = getattr(server, "handle_exit", None)
    if not callable(original):
        # A server object with no signal handler of its own has nothing to
        # wrap; the in-process RELOAD / REPLACE_PROCESS paths still arm the
        # clock through the supervisor's own request().
        return

    def handle_exit(sig: int, frame: FrameType | None) -> None:
        arm()
        original(sig, frame)

    # Written into the instance dict, which is what shadowing a bound method
    # actually is; a plain attribute assignment says the same thing but reads
    # as a redefinition of uvicorn's method, which this is not.
    server.__dict__["handle_exit"] = handle_exit


def _log_hard_exit() -> None:
    """Last words before the stop watchdog terminates this process."""

    logger.error(
        "Shutdown did not complete within the graceful shutdown budget plus "
        "its teardown margin; exiting now. Lower or raise "
        "SERVER_GRACEFUL_SHUTDOWN_SECONDS to change how long a stop may take."
    )
    logger.complete()


def _address_in_use_error(settings: Settings) -> OSError:
    """A synthetic EADDRINUSE describing this server's configured bind address."""

    return OSError(
        errno.EADDRINUSE,
        f"{settings.host}:{settings.port} is already in use",
    )


def _log_bind_failure(settings: Settings, exc: OSError) -> None:
    """Explain a failing bind without touching the process that holds it."""
    if is_address_in_use(exc):
        owner = diagnose_port_owner(settings.host, settings.port)
        if owner is not None and owner.pid is not None:
            logger.error(
                "Cannot bind {host}:{port} ({err}); held by PID {pid} ({name}). "
                "Stop that process or change host/port in settings.",
                host=settings.host,
                port=settings.port,
                err=exc,
                pid=owner.pid,
                name=owner.name or "unknown",
            )
            return
        if owner is not None:
            logger.error(
                "Cannot bind {host}:{port} ({err}); another process holds it "
                "(owner unresolved). Stop it or change host/port in settings.",
                host=settings.host,
                port=settings.port,
                err=exc,
            )
            return
        logger.error(
            "Cannot bind {host}:{port} ({err}). The port is already in use; "
            "stop the owner or change host/port in settings.",
            host=settings.host,
            port=settings.port,
            err=exc,
        )
        return
    logger.error(
        "Server failed to start on {host}:{port}: {err}",
        host=settings.host,
        port=settings.port,
        err=exc,
    )


_config_dir_banner_emitted = False


def _survey_other_servers(settings: Settings) -> None:
    """Say which other MCC servers are running, in the background, at start.

    On a daemon thread, and never a gate. Enumerating processes costs about two
    seconds on Windows, and this is a *report*: making the start wait for it
    would trade a measurable slowdown on every start for a log line that is
    only interesting on the rare start that follows a leak.

    What it does about what it finds is the operator's decision and defaults to
    nothing. ``SERVER_STALE_SERVER_ACTION=report`` -- the default -- stops
    nothing at all, because "owns no listening socket in one scan" is not
    evidence that a server is finished: it may be starting, draining, or
    streaming an answer to a request accepted before the socket closed. Two of
    those were running on the machine that produced this feature, with live
    upstream connections, while the port they had been started for belonged to
    somebody else.

    **Both paths are resolved here, on the calling thread, and handed to the
    thread as values.** Resolving them inside it instead was a real defect, not
    a style point: the thread outlives the call, ``config_dir_path()`` reads the
    environment at the moment it is called, and a survey that started under one
    configuration directory would then write its report into whichever one
    happened to be current when it finished. The test suite caught it as a
    hermeticity violation -- a survey started by one test resolved
    ``~/.mcc`` on the *real* home after that test's redirect had been torn down
    -- and the same shape in production is a server writing its report into
    somebody else's config directory.
    """

    log_path = request_log_path()
    survey_path = other_servers_path()
    self_pid = os.getpid()
    stale_after = settings.server_stale_session_seconds
    action = settings.server_stale_server_action

    def survey() -> None:
        try:
            observations = observe_servers(
                request_log_path=log_path,
                self_pid=self_pid,
                stale_after_seconds=stale_after,
            )
        except OSError as exc:
            logger.debug("Could not survey other My Claude Code servers: {}", exc)
            return
        report_servers(observations, context="At start")
        if action == "stop":
            stopped = {item.pids for item in stop_stale_servers(observations)}
            observations = [item for item in observations if item.pids not in stopped]
        write_survey(survey_path, observations)

    threading.Thread(target=survey, name="mcc-server-survey", daemon=True).start()


def _bootstrap_request_log_path() -> None:
    """Register the resolved request-log path for this process (once)."""

    from my_claude_code.core import request_log

    request_log.set_request_log_path(request_log_path())


def _emit_config_dir_banner() -> None:
    """Print the legacy-config notice once per process, if it applies."""

    global _config_dir_banner_emitted
    if _config_dir_banner_emitted:
        return
    _config_dir_banner_emitted = True
    resolution = config_dir_resolution()
    if not resolution.uses_legacy_home:
        return
    if resolution.legacy_unhealthy:
        health = resolution.legacy_health
        check = health.failed_check if health else "unknown"
        logger.warning(
            "{} failed the '{}' check ({}); it is still the config directory in "
            "use and nothing was moved, renamed or created. Fix it in place -- "
            "{} is created only by running mcc-migrate.",
            resolution.path,
            check,
            health.detail if health else "unknown",
            new_config_dir_path(),
        )
        return
    logger.info(
        "Your data lives in the legacy {}. To move it to {}: stop the server "
        "and the tray, run mcc-migrate, then start the server again.",
        resolution.path,
        new_config_dir_path(),
    )


def serve() -> None:
    """Start and supervise the FastAPI server."""
    # First, and before anything resolves a path or reads a setting: make
    # the config home exist, move a legacy one into place, and write a
    # default .env with a token generated on this machine. Until 6.65.0 a
    # machine that had never run mcc-init started here and died two lines
    # later on the 6.30.0 refusal.
    ensure_config_home_or_exit()
    _bootstrap_request_log_path()
    _emit_config_dir_banner()
    opened_admin_browser = False
    try:
        try:
            while True:
                _migrate_legacy_env_if_missing()
                _migrate_config_env_keys()
                # Every generation restarts the clock, including the in-process
                # RELOAD that builds a second application in this same process.
                startup_state().begin()
                startup_state().mark("settings")
                settings = get_settings()
                should_open_admin = (
                    settings.open_admin_browser and not opened_admin_browser
                )
                action = _run_supervised_server(
                    settings, open_admin_browser=should_open_admin
                )
                if action is ServerExitAction.STOP:
                    return
                if action is ServerExitAction.REPLACE_PROCESS:
                    _replace_server_process(settings)
                    return
                opened_admin_browser = opened_admin_browser or should_open_admin
                get_settings.cache_clear()
        except KeyboardInterrupt:
            return
    finally:
        kill_all_best_effort()


def _bind_listening_socket(settings: Settings) -> socket.socket:
    """Bind and listen, exclusively, before uvicorn exists.

    Deliberately NOT ``uvicorn.Config.bind_socket()``. That sets
    ``SO_REUSEADDR``, and ``SO_REUSEADDR`` does not mean on Windows what it
    means on POSIX: there it lets a *second* socket bind an address a live
    listener already holds, and the two then share it with no error and no
    defined winner. Measured on this machine -- a second ``mcc-server`` bound
    straight over a healthy one and both processes reported themselves as
    running. ``SO_EXCLUSIVEADDRUSE`` is the Windows spelling of the guarantee
    POSIX gives by default: one owner, and a bind failure for anybody else.

    Binding here rather than inside uvicorn also closes the window between the
    port takeover above and the bind, and holds the port from the first
    instant of ``server.run`` rather than from the event loop's first pass.
    """

    family = socket.AF_INET6 if ":" in settings.host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if _WINDOWS and exclusive is not None:
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            # POSIX: this only permits rebinding a port left in TIME_WAIT,
            # which is what makes a restart immediate rather than a minute
            # away. It cannot displace a live listener.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((settings.host, int(settings.port)))
        sock.listen(SERVER_BACKLOG)
        sock.set_inheritable(True)
    except BaseException:
        sock.close()
        raise
    return sock


def _schedule_open_admin_browser(settings: Settings) -> None:
    """After /health succeeds, open the admin UI in the default browser (daemon thread)."""

    admin_url = local_admin_url(settings)
    proxy_root_url = local_proxy_root_url(settings)

    def open_when_ready() -> None:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if preflight_proxy(proxy_root_url) is None:
                webbrowser.open(admin_url)
                return
            time.sleep(0.15)

    threading.Thread(
        target=open_when_ready, name="mcc-open-admin-browser", daemon=True
    ).start()


def _run_supervised_server(
    settings: Settings, *, open_admin_browser: bool
) -> ServerExitAction:
    """Run once; act only after the old ownership graph fully closes."""

    if refusal := open_proxy_without_auth_error(
        host=settings.host, auth_token=settings.anthropic_auth_token
    ):
        # Before the socket, not after: an exposed proxy that has already
        # answered one request has already leaked whatever that request cost.
        logger.error(refusal)
        print(refusal, file=sys.stderr)
        # ...and into the file the desktop app's error page names. This
        # guard runs before the composition root configures logging, so
        # until 6.65.0 the one start a user most needs an explanation for
        # produced no server.log at all.
        append_to_server_log(os.getenv("LOG_FILE", server_log_path()), "ERROR", refusal)
        raise SystemExit(1)

    requested = ServerExitAction.STOP
    # Whether the background startup raised. uvicorn used to end the process
    # for us when the lifespan startup failed; now that the listener binds
    # first, ending it is this supervisor's job -- and it has to be ended,
    # because the alternative is a socket that answers "starting" for ever.
    startup_failed = False
    # When the stop clock started, for the "drain finished" line below. ``None``
    # means no stop has been requested yet.
    stop_started_at: float | None = None
    server_holder: dict[str, uvicorn.Server] = {}
    deadline = stop_deadline()
    deadline.clear()

    def request(action: ServerExitAction) -> None:
        nonlocal requested, stop_started_at
        # Only escalate: a later, weaker action (e.g. RELOAD) must not
        # downgrade an already-requested REPLACE_PROCESS.
        if _ACTION_PRIORITY[action] > _ACTION_PRIORITY[requested]:
            requested = action
        # One wall-clock deadline for the WHOLE stop, computed once, here.
        # Every later stage -- uvicorn's connection drain, the response
        # cleanup, the provider drain, the ASGI lifespan -- measures what it
        # has left against this instant instead of starting a budget of its
        # own, so the total stop time is the number on the box however the
        # time was spent. A second request cannot move it.
        deadline.request(settings.server_graceful_shutdown_seconds)
        # The first request wins; a later one neither moves the deadline nor
        # deserves a second log line saying it did.
        if stop_started_at is None:
            stop_started_at = time.monotonic()
            budget = clamp_stop_budget(settings.server_graceful_shutdown_seconds)
            # Without this line a slow shutdown leaves NO evidence at all: the
            # ASGI gate logs nothing, the watchdog only speaks when it wins,
            # and the rotated logs are swept on startup -- so "why did the
            # restart take five minutes" was, measurably, unanswerable.
            logger.info(
                "Stop requested (action={action}, "
                "SERVER_GRACEFUL_SHUTDOWN_SECONDS={configured}, budget={budget:.1f}s, "
                "hard deadline in {total:.1f}s). New requests are refused from now.",
                action=requested.value,
                configured=settings.server_graceful_shutdown_seconds,
                budget=budget,
                total=budget + STOP_TEARDOWN_MARGIN_SECONDS + HARD_EXIT_GRACE_SECONDS,
            )
        # New work is refused from this instant (runtime/asgi.py's gate and
        # ProviderRuntimeManager.acquire), so a busy client can no longer keep
        # a closing server alive by making ordinary requests.
        if requested is not ServerExitAction.RELOAD:
            # A terminal stop must end the process even if a stage ignores its
            # bound outright. A RELOAD must NOT: when its drain overruns the
            # supervisor keeps the server up on a fresh generation instead.
            deadline.arm_hard_exit(on_exit=_log_hard_exit)
        if server := server_holder.get("server"):
            server.should_exit = True

    def start_the_stop_clock_for_a_signal() -> None:
        # uvicorn owns the signal handlers (MCC installs none of its own), and
        # they set ``should_exit`` directly without going through ``request``.
        # A Ctrl+C, a SIGTERM, or the tray's CTRL_BREAK is by far the commonest
        # way this server is stopped, so arming the shared deadline only on the
        # in-process RELOAD / REPLACE_PROCESS paths would leave every bound
        # this release adds inert in exactly the case the user reported.
        request(ServerExitAction.STOP)

    def request_restart() -> None:
        request(ServerExitAction.RELOAD)

    def request_process_restart() -> None:
        request(ServerExitAction.REPLACE_PROCESS)

    def report_startup_failure() -> None:
        nonlocal startup_failed
        startup_failed = True
        request(ServerExitAction.STOP)

    def listener_is_serving() -> bool:
        server = server_holder.get("server")
        return bool(server is not None and server.started)

    startup_state().mark("application")
    asgi_app = build_asgi_app(
        settings,
        restart_callback=request_restart,
        process_restart_callback=request_process_restart,
        startup_failed_callback=report_startup_failure,
        serving_predicate=listener_is_serving,
    )
    config = uvicorn.Config(
        asgi_app,
        host=settings.host,
        port=settings.port,
        log_level="debug",
        timeout_graceful_shutdown=round(settings.server_graceful_shutdown_seconds),
    )
    server = uvicorn.Server(config)
    _start_stop_clock_on_signal(server, start_the_stop_clock_for_a_signal)
    server_holder["server"] = server
    if open_admin_browser:
        _schedule_open_admin_browser(settings)
    # A held port is the usual reason a start fails. During a restart the
    # previous generation may still own the socket for a beat (longer under WSL),
    # so wait a bounded moment -- scaled to the configured graceful-shutdown
    # budget -- for it to free before declaring a genuine conflict. Never kills
    # the owner; at worst it is diagnosed and the start is abandoned.
    bind_wait = max(5.0, min(float(settings.server_graceful_shutdown_seconds), 60.0))
    if not probe_port_available(settings.host, settings.port):
        # The port is held. A short grace first, because a previous generation
        # that is a beat from releasing the socket should not be killed for
        # it -- but only a short one: the user's rule is that starting the
        # server takes the port, and waiting out a full drain budget before
        # even looking at the holder is how a restart came to take half a
        # minute. Under SERVER_PORT_TAKEOVER=never the old, patient wait is
        # still what happens, because there nothing else can.
        grace = bind_wait if settings.server_port_takeover == "never" else 2.0
        if wait_for_port_free(settings.host, settings.port, timeout=grace):
            pass
        else:
            outcome = take_port(
                settings.host,
                settings.port,
                settings.server_port_takeover,
                wait_seconds=bind_wait,
            )
            if not outcome.free:
                _log_bind_failure(settings, _address_in_use_error(settings))
                raise SystemExit(1)
            logger.info(
                "Port {port} taken back: {what}.",
                port=settings.port,
                what=outcome.describe(),
            )
    # After the takeover and before the bind: who else is running? The port
    # takeover has just settled the one process that was in this server's way;
    # this settles the ones that are in an *installer's* way, which nothing has
    # ever been able to see. It reports and returns -- nothing here decides to
    # stop anybody unless SERVER_STALE_SERVER_ACTION says so.
    _survey_other_servers(settings)
    # Bind here, in the supervisor, rather than leaving it to uvicorn.
    #
    # This is the load-bearing half of "bind the listener first". uvicorn
    # creates its socket inside ``Server.startup()``, *after* the ASGI lifespan
    # startup -- so even with the lifespan answered immediately, the socket
    # appears only once the event loop next runs, and the background startup
    # (which is not purely cooperative: provider construction and the
    # configured-model probe block the loop for up to a second at a time) could
    # be scheduled in front of it. Binding before uvicorn exists closes both
    # that window and the one between the port takeover above and the bind:
    # nothing can take the port back in between, because it was never let go.
    try:
        listening_socket = _bind_listening_socket(settings)
    except OSError as exc:
        _log_bind_failure(settings, exc)
        raise SystemExit(1) from exc
    # The session row in the request log was opened while this process was
    # still starting, before there was an address to record. Publishing it here
    # is what lets the NEXT server -- and an installer -- tell "a server that
    # was superseded on this port" from "a server that is busy elsewhere",
    # which is the whole difference between a safe sweep and a destructive one.
    set_server_bind_address(settings.host, settings.port)
    logger.info(
        "Listening on {url}; answering /health with 'starting' until ready.",
        url=local_proxy_root_url(settings),
    )
    startup_state().mark(LISTENER_STAGE)
    try:
        try:
            server.run(sockets=[listening_socket])
        finally:
            # The socket is closed the moment run() returns, so the claim on
            # the port goes with it. A session that kept claiming an address it
            # no longer serves is exactly the stale claim this feature exists
            # to reason about, and it must never be one MCC writes itself.
            set_server_bind_address(None, None)
            # Control is back in the supervisor, so the ordered stop path won
            # and the watchdog has nothing left to guard. Anything after this
            # point carries its own bound.
            deadline.disarm_hard_exit()
    except (OSError, SystemExit) as exc:
        # uvicorn turns a bind failure into SystemExit(1), but it can also exit
        # for other reasons (SSL, etc.), and those surface as OSError. Only
        # claim a port conflict when the port is still actually unavailable;
        # otherwise re-raise without a false owner claim.
        if not probe_port_available(settings.host, settings.port):
            _log_bind_failure(settings, _address_in_use_error(settings))
        else:
            logger.error(
                "Server failed to start on {host}:{port}: {err}",
                host=settings.host,
                port=settings.port,
                err=exc,
            )
        raise
    if startup_failed:
        # Nothing to supervise: the application never came up. Exiting non-zero
        # is what a caller -- the desktop shell, the tray, a terminal -- reads
        # as "this did not work", and it is what the lifespan failure used to
        # produce before the listener moved in front of it.
        raise SystemExit(1)
    if stop_started_at is not None:
        # The other half of the pair. Together the two lines are the whole
        # timeline of a stop, which is what an after-the-fact question about a
        # slow shutdown needs and what no log has ever carried.
        logger.info(
            "Stop complete (action={action}) after {elapsed:.1f}s of a "
            "{budget:.1f}s budget.",
            action=requested.value,
            elapsed=time.monotonic() - stop_started_at,
            budget=clamp_stop_budget(settings.server_graceful_shutdown_seconds),
        )
    # Past this point the socket is closed and no request can arrive, so the
    # gate has nothing left to guard; clearing keeps the next generation (and,
    # in-process, the next test) from inheriting a stop that already happened.
    if requested is ServerExitAction.STOP:
        deadline.clear()
        return requested
    if asgi_app.runtime.is_closed:
        deadline.clear()
        return requested
    # The runtime did not finish closing in-flight requests within the graceful
    # shutdown budget. A process replacement would execv into the new image
    # while the old generation still owns live connections, so refuse it loudly
    # and exit as a failure rather than silently degrading to a plain stop. The
    # update is already installed; the service must be restarted by hand to run
    # the new version.
    if requested is ServerExitAction.REPLACE_PROCESS:
        logger.error(
            "Process replacement refused: the previous runtime did not finish "
            "closing in-flight requests within the graceful shutdown budget "
            "(SERVER_GRACEFUL_SHUTDOWN_SECONDS={}). The updated server is "
            "already installed; restart the service to run it.",
            settings.server_graceful_shutdown_seconds,
        )
        raise SystemExit(1)
    # A config-driven RELOAD must not degrade to a plain stop when the runtime
    # is still draining. The serve() loop rebuilds the app on the next pass and
    # the port-wait at the top of the next iteration handles the lingering
    # socket, so returning RELOAD keeps the server up instead of exiting the
    # process (which is what "the server crashed after I applied a setting"
    # looked like). Writer threads are daemon, so an old runtime still closing
    # does not block the fresh generation.
    #
    # Clearing the deadline is what re-opens the door: the ASGI gate and
    # ProviderRuntimeManager.acquire both read it, so the fresh generation
    # would otherwise be born refusing every request.
    deadline.clear()
    return ServerExitAction.RELOAD


def init() -> None:
    """Scaffold config at the resolved config directory's .env."""
    config_dir = config_dir_path()
    env_file = managed_env_path()

    migrated_from = _migrate_legacy_env_if_missing()
    _migrate_config_env_keys()
    if migrated_from is not None:
        print(f"Config migrated from {migrated_from} to {env_file}")
        print(
            "Edit it to set your API keys and model preferences, then run: mcc-server"
        )
        return

    if env_file.exists():
        print(f"Config already exists at {env_file}")
        print("Delete it first if you want to reset to defaults.")
        return

    config_dir.mkdir(parents=True, exist_ok=True)
    # The same text a first start writes, with the same per-machine token
    # generator: two ways of reaching a first configuration must not produce
    # two different configurations.
    env_file.write_text(render_default_env(), encoding="utf-8")
    print(f"Config created at {env_file}")
    print(
        "A proxy token was generated for this machine; read it on the "
        "dashboard under Providers -> Runtime."
    )
    print("Edit it to set your API keys and model preferences, then run: mcc-server")


def _migrate_legacy_env_if_missing() -> Path | None:
    """Copy a legacy user env into the managed config path when absent."""

    env_file = managed_env_path()
    if env_file.exists():
        return None

    # TODO: Remove after the managed-config-path migration has had a release cycle.
    for legacy_env in legacy_env_paths():
        if not legacy_env.is_file():
            continue
        env_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(legacy_env, env_file)
        return legacy_env

    return None


def _migrate_config_env_keys() -> tuple[Path, ...]:
    """Apply dotenv key migrations before Settings loads config."""

    migrated = migrate_owned_env_files()
    if warning := explicit_env_file_migration_warning(os.environ):
        print(warning, file=sys.stderr)
    return migrated


def chatgpt_oauth_login() -> None:
    """Run the ChatGPT/Codex OAuth device-flow login."""
    from my_claude_code.providers.chatgpt_oauth import chatgpt_oauth_login_command

    chatgpt_oauth_login_command()


def anthropic_oauth_login() -> None:
    """Run the Claude subscription OAuth (PKCE) login."""
    from my_claude_code.providers.anthropic_oauth import (
        anthropic_oauth_login_command,
    )

    anthropic_oauth_login_command()


def compact_log() -> None:
    """Rewrite an existing request log into deduplicated compressed bodies."""
    from my_claude_code.core.request_log import (
        compact_request_log,
        default_request_log_path,
    )

    path = default_request_log_path()
    if not path.exists():
        print(f"No request log at {path}", file=sys.stderr)
        raise SystemExit(1)

    size = path.stat().st_size
    print(f"Compacting {path} ({size / 1e9:.2f} GB)")
    print("Stop the server first, or the final vacuum cannot reclaim space.\n")

    def report(done: int) -> None:
        print(f"\r  converted {done:,} requests", end="", flush=True)

    result = compact_request_log(path, progress=report)
    print()

    before = result["bytes_before"]
    after = result["bytes_after"]
    print(f"\nConverted   {result['converted']:,} requests")
    print(f"Before      {before / 1e9:.2f} GB")
    print(f"After       {after / 1e9:.2f} GB")
    if after:
        print(f"Reduction   {before / after:.1f}x")
    if not result["vacuumed"]:
        print(
            "\nThe vacuum could not run, so the file has not shrunk yet: something"
            " else has the database open. Stop the server and run this again --"
            " the conversion itself is already done and will not repeat.",
            file=sys.stderr,
        )
