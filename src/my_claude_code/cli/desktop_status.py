"""The machine-readable status surface a second process reads.

``mcc-desktop --status`` prints five ``key=value`` lines for a human. This
module is the other half: one JSON document carrying everything a *program*
needs to render the dashboard in a window it owns -- where the config lives,
which URL to load, whether a server is already answering there, and the two
timing budgets it must not hard-code.

Three rules hold this file together:

* **It never resolves anything itself.** ``resolve_config_dir()`` stays the
  single source of the config directory, ``config/server_urls.py`` the single
  source of the browser-facing URL, ``probe_server_presence()`` the single
  source of the healthy/free/foreign ladder. This module only spells their
  answers as JSON.
* **It is a pure read.** No spawn, no singleton lock, no write to
  ``desktop.json``, no autostart reconciliation, no server start. Reading a
  status must never change one.
* **It is cheap.** No network call off the machine and no heavyweight import,
  so a shell may call it on every launch and on every reconnect.

``schema`` is the compatibility handle. It is bumped when a documented key is
removed or changes type; adding a key does not bump it, because a reader is
required to tolerate keys it does not know.
"""

import json
from typing import Any

from my_claude_code.cli.desktop import port_conflict_message, probe_server_presence
from my_claude_code.config.constants import (
    DASHBOARD_RECONNECT_TIMEOUT_SECONDS,
    SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT,
)
from my_claude_code.config.desktop import load_desktop_state
from my_claude_code.config.paths import config_dir_resolution, server_log_path
from my_claude_code.config.server_urls import (
    local_admin_url,
    local_browser_host,
    local_proxy_root_url,
)
from my_claude_code.config.settings import get_settings
from my_claude_code.core.version import package_version

#: Bumped only when a key below is removed or retyped. See the module docstring.
STATUS_SCHEMA = 1

#: Every key ``desktop_status()`` emits, in emission order. This tuple is the
#: contract: a golden-key-set test compares it against a real payload, so a key
#: cannot be dropped or renamed without the change being deliberate.
STATUS_KEYS: tuple[str, ...] = (
    "schema",
    "version",
    "config_dir",
    "config_dir_source",
    "config_dir_is_legacy",
    "host",
    "port",
    "root_url",
    "admin_url",
    "health_url",
    "server_presence",
    "port_conflict",
    "server_mode",
    "window",
    "window_open",
    "window_width",
    "window_height",
    "tray_enabled",
    "minimize_to_tray",
    "start_at_login",
    "server_log",
    "start_timeout_seconds",
    "health_check_interval_seconds",
    "health_poll_seconds",
    "health_failure_threshold",
    "activation_poll_seconds",
    "reconnect_timeout_seconds",
)


def reconnect_timeout_seconds(settings: Any) -> float:
    """Seconds a client waits for the server to come back after an update.

    The dashboard's own budget, recomputed from the same parts rather than
    copied: ``DASHBOARD_RECONNECT_TIMEOUT_SECONDS`` is install + the *default*
    graceful drain + a startup margin, so swapping the default drain for the
    operator's configured one yields exactly what
    ``application.release_updates`` reports to the page. ``cli`` may not import
    ``application`` (see ``tests/contracts/test_import_boundaries.py``), and a
    contract test pins the two answers together.
    """

    drain = float(getattr(settings, "server_graceful_shutdown_seconds", 0.0))
    return DASHBOARD_RECONNECT_TIMEOUT_SECONDS - (
        SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT - drain
    )


def desktop_status() -> dict[str, Any]:
    """Return the whole status document. Reads only; writes nothing."""

    settings = get_settings()
    resolution = config_dir_resolution()
    state = load_desktop_state()
    presence = probe_server_presence(settings)
    root_url = local_proxy_root_url(settings)

    return {
        "schema": STATUS_SCHEMA,
        "version": package_version(),
        "config_dir": str(resolution.path),
        "config_dir_source": resolution.source,
        "config_dir_is_legacy": resolution.uses_legacy_home,
        "host": local_browser_host(settings),
        "port": int(settings.port),
        "root_url": root_url,
        "admin_url": local_admin_url(settings),
        "health_url": f"{root_url}/health",
        "server_presence": presence,
        # Only a stranger on the port needs explaining, and the explanation
        # names the holding process. Anything else would be noise a shell has
        # to learn to ignore.
        "port_conflict": (
            port_conflict_message(settings) if presence == "foreign" else None
        ),
        "server_mode": state.server_mode,
        "window": state.window,
        "window_open": state.window_open,
        "window_width": int(settings.desktop_window_width),
        "window_height": int(settings.desktop_window_height),
        "tray_enabled": state.tray_enabled,
        "minimize_to_tray": state.minimize_to_tray,
        "start_at_login": state.start_at_login,
        "server_log": str(server_log_path()),
        "start_timeout_seconds": float(settings.desktop_server_start_timeout),
        "health_check_interval_seconds": float(settings.desktop_health_check_interval),
        "health_poll_seconds": float(settings.desktop_health_poll_seconds),
        "health_failure_threshold": int(settings.desktop_health_failure_threshold),
        "activation_poll_seconds": float(settings.desktop_activation_poll_seconds),
        "reconnect_timeout_seconds": reconnect_timeout_seconds(settings),
    }


def print_status() -> None:
    """Write the status document to stdout, and nothing else.

    Stdout is a machine's input here: every diagnostic in this process goes to
    stderr, so a caller can pipe stdout straight into a JSON parser.
    """

    print(json.dumps(desktop_status(), indent=2))
