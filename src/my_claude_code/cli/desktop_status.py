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
required to tolerate keys it does not know. 6.44.0 added four shell keys,
6.45.0 added ``autostart_reconcile``, 6.50.0 added
``reconnect_restatus_seconds``, 6.58.1 added ``server_start_retries`` and
6.60.0 added ``shell_installed_tag``; the schema stayed at 1 every time, for
exactly that reason.

``shell_installed_tag`` is the key BUG-0 needed. The document has carried
``shell_release_tag`` -- what this *wheel* pins -- since 6.44.0, and nothing
read it; a window that compares it with its own compiled-in tag learns it is
stale, which is the whole of the fix. The receipt's tag rides beside it so a
reader can also tell "the window running is old" from "the file on disk is
old", which are different problems with different remedies. Per the two-release
rule below the shell only *tolerates* it in 6.60.0; it may be required from
6.61.0.

Note the asymmetry a new key creates, because it is the one thing to get right
when adding another. An *old* reader tolerates a key it has never heard of, so
a new wheel is always safe under an old shell. A *new* reader refuses a
document that is missing a budget it needs (C9: no compiled-in default), so a
new shell is not safe under an old wheel -- which is why the shell pin moves in
a release *after* the one that starts emitting the key, never before.

**One key is exempt from that promotion, permanently: ``update``.** Decision Q7
of 2026-09-10 freezes it as *informational, never required*. Every other key
here describes the machine, and the command that prints them is expected to
work; ``update`` describes an installer that is in the act of replacing the
environment this very command runs from, so the one circumstance in which it
carries a value is the circumstance in which it cannot be delivered. A reader
that needs the fact reads ``<config dir>/updates/progress.json`` itself, which
is what the desktop shell does. See the comment beside the key below.

**Why ``draining`` is behind a flag, and why it still did not bump the schema.**
6.50.0 gave ``server_presence`` a fourth value: MCC's own server, answering the
port but refusing everything with its shutdown 503. Adding a *value* to a
documented key is not a type change, so by the rule above it is not a schema
bump -- but the desktop shell refuses a presence it has no branch for (it
prints "a server state this window does not know" rather than guessing at the
nearest neighbour, which is the right instinct and the reason unknown values
are safe to add anywhere else). A shell built before 6.50.0 would therefore
turn a routine restart into an error page. So the value is opt-in: a caller
asks for it with ``--print-status --presence-v2``, an old shell does not ask,
and an old shell keeps seeing the three values it was written against. The
flag can retire once no supported shell predates 6.50.0 -- it is a compatibility
window, not a permanent second mode.

**One word about ``tray_enabled``.** It answers "should the reader of this
document draw a tray icon", which is not always the same as the persisted
preference of the same name. While the Python tray is running -- Windows and
macOS today (decision Q2: the shell's tray is the future, the Python one
retires later) -- a second icon beside it is a defect, so ``mcc-desktop`` sets
``MCC_DESKTOP_SHELL_TRAY=0`` in the shell child's environment and this module
reports ``false`` to that child alone. ``shell_tray`` says the same thing
unambiguously for a reader that wants it spelled out, and ``mcc-desktop
--status`` keeps printing the persisted value, so nothing is hidden from a
human. On Linux there is no Python tray, the variable is ``1``, and the shell's
tray is the only one.
"""

import json
import os
from typing import Any

from my_claude_code.cli.desktop import (
    autostart_reconcile_enabled,
    classify_port_holder,
    port_conflict_message,
    probe_server_state,
    server_pid_of,
)
from my_claude_code.cli.desktop_window import SHELL_TRAY_ENV
from my_claude_code.config.constants import (
    DASHBOARD_RECONNECT_TIMEOUT_SECONDS,
    SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT,
)
from my_claude_code.config.desktop import load_desktop_state
from my_claude_code.config.desktop_shell import desktop_shell_report
from my_claude_code.config.paths import config_dir_resolution, server_log_path
from my_claude_code.config.server_urls import (
    local_admin_url,
    local_browser_host,
    local_proxy_root_url,
)
from my_claude_code.config.settings import get_settings
from my_claude_code.config.update_progress import update_report
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
    "server_starting_stage",
    "port_conflict",
    "server_mode",
    "window",
    "window_open",
    "window_width",
    "window_height",
    "tray_enabled",
    "minimize_to_tray",
    "close_to_tray",
    "start_at_login",
    "autostart_reconcile",
    "server_log",
    "start_timeout_seconds",
    "server_start_retries",
    "health_check_interval_seconds",
    "health_poll_seconds",
    "health_failure_threshold",
    "activation_poll_seconds",
    "reconnect_timeout_seconds",
    "reconnect_restatus_seconds",
    "health_probe_timeout_seconds",
    "tick_seconds",
    "start_backoff_seconds",
    "foreign_grace_seconds",
    "status_wall_seconds",
    "holder",
    "server_pid",
    "shell_tray",
    "shell_binary",
    "shell_release_tag",
    "shell_installed_tag",
    "shell_ready",
    "update",
)


def shell_tray_enabled(state: Any) -> bool:
    """Return whether the reader of this document should draw a tray icon.

    See the module docstring. The environment variable is only ever set by
    ``mcc-desktop`` on the shell child it launches, so a human running
    ``--print-status`` sees the persisted preference unchanged.
    """

    if not state.tray_enabled:
        return False
    return os.environ.get(SHELL_TRAY_ENV, "1").strip() != "0"


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


def desktop_status(*, presence_v2: bool = False) -> dict[str, Any]:
    """Return the whole status document. Reads only; writes nothing.

    ``presence_v2`` opts the caller into the ``draining`` presence. See the
    module docstring for why it is a flag rather than the default.
    """

    settings = get_settings()
    resolution = config_dir_resolution()
    state = load_desktop_state()
    server = probe_server_state(settings, presence_v2=presence_v2)
    presence = server.presence
    holder = classify_port_holder(settings, server)
    root_url = local_proxy_root_url(settings)
    shell_tray = shell_tray_enabled(state)

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
        # The server's own stage name while it is starting, and ``null``
        # otherwise. A window that can say "loading provider catalogues"
        # instead of a bare spinner is the difference between a wait a user
        # sits through and one they kill the app over.
        "server_starting_stage": server.stage,
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
        "tray_enabled": shell_tray,
        "minimize_to_tray": state.minimize_to_tray,
        # Already resolved, and that is the whole point of the key. ``close_to
        # _tray`` above is a preference; this is the answer to "if the user
        # closes this window, is there a tray for it to go to?" -- and the
        # window cannot work that out for itself, because the ``tray_enabled``
        # it is handed answers "should YOU draw an icon", which is false on
        # Windows and macOS precisely BECAUSE a Python tray is running. Reusing
        # that as "there is no tray" is how closing the window ended the app on
        # the two platforms that have one.
        "close_to_tray": state.close_to_tray and state.tray_enabled,
        "start_at_login": state.start_at_login,
        # Whether the *next* launch would touch the OS registration at all.
        # A reader that sees ``false`` here knows ``start_at_login`` above is
        # a stored preference nobody is currently enforcing.
        "autostart_reconcile": autostart_reconcile_enabled(),
        "server_log": str(server_log_path()),
        "start_timeout_seconds": float(settings.desktop_server_start_timeout),
        # How many further attempts follow the first one before the window
        # stops calling itself "starting". Three attempts of 15s is what a
        # 22-25s startup needs; one was what parked the window on a Retry
        # button seven seconds before the server answered.
        "server_start_retries": int(settings.desktop_server_start_retries),
        "health_check_interval_seconds": float(settings.desktop_health_check_interval),
        "health_poll_seconds": float(settings.desktop_health_poll_seconds),
        "health_failure_threshold": int(settings.desktop_health_failure_threshold),
        "activation_poll_seconds": float(settings.desktop_activation_poll_seconds),
        "reconnect_timeout_seconds": reconnect_timeout_seconds(settings),
        # How often a client that is waiting out a restart should re-read this
        # document instead of only re-probing /health. A reconnect loop that
        # polls one URL forever cannot notice that the port has gone free and
        # nobody is going to start a server on it -- which is the whole of "the
        # app hangs until I close and reopen it".
        "reconnect_restatus_seconds": float(
            settings.desktop_reconnect_restatus_seconds
        ),
        # One health probe, one timeout, and it lives here rather than in the
        # two constants that were meant to be the same number and were not
        # (``launchers/common.py`` and the shell's ``health.rs``). Audit C9.
        "health_probe_timeout_seconds": float(settings.desktop_health_probe_timeout),
        # The lifecycle tick: how often the desktop app probes, and how often it
        # starts a server that is not there. Decision Q4 (2026-09-08) fixed it
        # at ten seconds, forever, with no attempt cap and no page that parks.
        "tick_seconds": float(settings.desktop_tick_seconds),
        # The shortest gap between two starts. Equal to the tick by default,
        # because Q4 asks for no backoff beyond it; an operator whose server
        # crash-loops can raise it without touching the probe cadence.
        "start_backoff_seconds": float(settings.desktop_start_backoff_seconds),
        # How long an unidentifiable port holder is given before the window
        # calls it foreign and stops starting servers into it. An unknown
        # holder during our own startup is overwhelmingly us (BUG-5).
        "foreign_grace_seconds": float(settings.desktop_foreign_grace_seconds),
        # How long the window may wait for THIS command. Out of the shell's
        # binary in 6.61.0 (audit §5.4): it decides whether a slow machine gets
        # a window at all, which is not a property of the binary.
        "status_wall_seconds": float(settings.desktop_status_wall_seconds),
        # Who holds the port, decided by process and never by a bind test.
        # This is the key BUG-5 needed: the old answer could not tell MCC's own
        # starting python.exe from a stranger, and told the user to go and stop
        # My Claude Code because it was not My Claude Code.
        "holder": holder.as_dict(),
        # The pid of MCC's own server, when the holder is one. ``null`` for a
        # foreign holder: reporting somebody else's pid under this name would
        # be worse than reporting nothing.
        "server_pid": server_pid_of(holder),
        "shell_tray": shell_tray,
        **desktop_shell_report(),
        # ``null`` unless a deferred update helper is running RIGHT NOW. See
        # ``config.update_progress`` for how "running" is decided (the helper's
        # own pid, not a stage name).
        #
        # FROZEN AS INFORMATIONAL, NEVER REQUIRED (decision Q7, 2026-09-10).
        # This is the one documented key the two-release rule in this module's
        # docstring must NOT be exercised on, and the reason is the key's own
        # subject matter: it can only be delivered by ``mcc-desktop
        # --print-status``, and that is precisely the command that stops
        # working while an update is in flight. ``uv tool install --force``
        # empties the tool environment in place before it resolves anything,
        # so for the whole of an install the shim exits 1 with
        # ``ModuleNotFoundError`` -- and a reader that *required* this key
        # would refuse the document exactly when the key would have had
        # something to say. The desktop shell therefore reads the underlying
        # file directly (``update_progress.rs``, ``lib.rs``'s
        # ``refresh_helper``) and only tolerates this key. Keep emitting it: it
        # is useful to a human running ``--print-status`` by hand, and to any
        # reader that has no access to the configuration directory.
        "update": update_report(),
    }


def print_status(*, presence_v2: bool = False) -> None:
    """Write the status document to stdout, and nothing else.

    Stdout is a machine's input here: every diagnostic in this process goes to
    stderr, so a caller can pipe stdout straight into a JSON parser.
    """

    print(json.dumps(desktop_status(presence_v2=presence_v2), indent=2))
