"""Implementations for installed Free Claude Code commands."""

import errno
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from enum import Enum
from pathlib import Path

import uvicorn
from loguru import logger

from my_claude_code.cli.launchers.common import preflight_proxy
from my_claude_code.cli.port_diagnostics import (
    diagnose_port_owner,
    is_address_in_use,
    probe_port_available,
    wait_for_port_free,
)
from my_claude_code.cli.process_registry import kill_all_best_effort
from my_claude_code.config.env_migrations import (
    explicit_env_file_migration_warning,
    migrate_owned_env_files,
)
from my_claude_code.config.env_template import load_env_template
from my_claude_code.config.paths import (
    config_dir_path,
    legacy_env_paths,
    managed_env_path,
)
from my_claude_code.config.proxy_auth import open_proxy_without_auth_error
from my_claude_code.config.server_urls import local_admin_url, local_proxy_root_url
from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.process_handoff import external_upgrade_helper_pending
from my_claude_code.runtime.bootstrap import build_asgi_app

_WINDOWS = os.name == "nt"


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
        candidate = bin_dir / ("fcc-server.exe" if os.name == "nt" else "fcc-server")
        if candidate.is_file():
            return str(candidate)
    return shutil.which("fcc-server")


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
        logger.error("Updated successfully, but fcc-server could not be found on PATH.")
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


def serve() -> None:
    """Start and supervise the FastAPI server."""
    opened_admin_browser = False
    try:
        try:
            while True:
                _migrate_legacy_env_if_missing()
                _migrate_config_env_keys()
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
        target=open_when_ready, name="fcc-open-admin-browser", daemon=True
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
        raise SystemExit(1)

    requested = ServerExitAction.STOP
    server_holder: dict[str, uvicorn.Server] = {}

    def request(action: ServerExitAction) -> None:
        nonlocal requested
        # Only escalate: a later, weaker action (e.g. RELOAD) must not
        # downgrade an already-requested REPLACE_PROCESS.
        if _ACTION_PRIORITY[action] > _ACTION_PRIORITY[requested]:
            requested = action
        if server := server_holder.get("server"):
            server.should_exit = True

    def request_restart() -> None:
        request(ServerExitAction.RELOAD)

    def request_process_restart() -> None:
        request(ServerExitAction.REPLACE_PROCESS)

    asgi_app = build_asgi_app(
        settings,
        restart_callback=request_restart,
        process_restart_callback=request_process_restart,
    )
    config = uvicorn.Config(
        asgi_app,
        host=settings.host,
        port=settings.port,
        log_level="debug",
        timeout_graceful_shutdown=round(settings.server_graceful_shutdown_seconds),
    )
    server = uvicorn.Server(config)
    server_holder["server"] = server
    if open_admin_browser:
        _schedule_open_admin_browser(settings)
    # A held port is the usual reason a start fails. During a restart the
    # previous generation may still own the socket for a beat (longer under WSL),
    # so wait a bounded moment -- scaled to the configured graceful-shutdown
    # budget -- for it to free before declaring a genuine conflict. Never kills
    # the owner; at worst it is diagnosed and the start is abandoned.
    bind_wait = max(5.0, min(float(settings.server_graceful_shutdown_seconds), 60.0))
    if not probe_port_available(
        settings.host, settings.port
    ) and not wait_for_port_free(settings.host, settings.port, timeout=bind_wait):
        _log_bind_failure(settings, _address_in_use_error(settings))
        raise SystemExit(1)
    try:
        server.run()
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
    if requested is ServerExitAction.STOP:
        return requested
    if asgi_app.runtime.is_closed:
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
    return ServerExitAction.RELOAD


def init() -> None:
    """Scaffold config at ~/.fcc/.env."""
    config_dir = config_dir_path()
    env_file = managed_env_path()

    migrated_from = _migrate_legacy_env_if_missing()
    _migrate_config_env_keys()
    if migrated_from is not None:
        print(f"Config migrated from {migrated_from} to {env_file}")
        print(
            "Edit it to set your API keys and model preferences, then run: fcc-server"
        )
        return

    if env_file.exists():
        print(f"Config already exists at {env_file}")
        print("Delete it first if you want to reset to defaults.")
        return

    config_dir.mkdir(parents=True, exist_ok=True)
    template = load_env_template()
    env_file.write_text(template, encoding="utf-8")
    print(f"Config created at {env_file}")
    print("Edit it to set your API keys and model preferences, then run: fcc-server")


def _migrate_legacy_env_if_missing() -> Path | None:
    """Copy a legacy user env into the managed config path when absent."""

    env_file = managed_env_path()
    if env_file.exists():
        return None

    # TODO: Remove after the ~/.fcc/.env migration has had a release cycle.
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
