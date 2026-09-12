"""Report the running version and hand an upgrade to the official installer.

**One update path (6.82.0).** This module no longer installs anything. Pressing
Update runs the same command a user would type by hand --
``scripts/install.ps1 -Restart`` on Windows, ``scripts/install.sh --restart``
on Linux and macOS, from the copy of each that ships inside this wheel. What is
left here is the one job only the running server can do: it knows its own
process id, so it writes a short detached helper that waits for itself to exit
and then starts the installer.

Until 6.82.0 there were two implementations of "install an update". The
dashboard's lived in a thousand-line PowerShell template in this file; the
hand-run one lived in ``scripts/install.ps1``; they downloaded the wheel
separately, verified it separately, and disagreed about who restarts the
server. On 2026-09-11 both ran within three minutes of each other, both exited
0, and the machine had no server for fifteen minutes because each believed
somebody else owned the restart. There is now one downloader, one verifier, one
installer and one owner of the restart, and all four are the install command.

**How the install itself works, since 6.72.0 and now in the installer.** It
used to be one call:
``uv tool install --force`` against the live environment. uv empties a tool
environment *in place* before it resolves a single new byte, so that one call
deleted the only working copy of MCC on the machine and then went to the
network. Measured here: ``mcc-server`` answered at t=0, failed with
``ModuleNotFoundError: annotated_types`` at +7.17 s, with
``ModuleNotFoundError: my_claude_code`` -- the error users actually reported --
at +7.99 s, and the executable itself was gone at +9.36 s. Two real updates
took 58 s and 102 s end to end. For all of that there was no server, no
``mcc-server``, and nothing to go back to.

Now the new environment is built **beside** the old one, in a tools root of its
own; it is **executed** once to prove it runs; the two directories are
**exchanged** by two renames (measured: 3.9 ms median); the old one is kept
until the new server **answers /health**, and put back if it does not. That is
the shape VS Code, Squirrel, Caddy, rustup and uv's own ``self update`` all
use, and the shape this project already used for its desktop shell
(``config/desktop_shell.py`` + ``swap.rs``) and not for its server.

The one thing that makes the exchange possible is a property of uv's launcher
shims, verified at the byte level: ``<bin>/mcc-server.exe`` and
``<tool dir>/Scripts/mcc-server.exe`` are the *same file*, and what they embed
is the absolute path ``<tool dir>/Scripts/python.exe``. They do not care which
environment is at that path. So the shims are never rewritten, never locked,
and never a reason an install fails -- the instant a new environment lands at
the canonical path, every already-installed launcher runs the new code.
"""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from contextlib import suppress
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import distribution as installed_distribution
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.config.constants import (
    DASHBOARD_RECONNECT_TIMEOUT_SECONDS,
)
from my_claude_code.config.paths import config_dir_path
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings
from my_claude_code.config.update_progress import (
    INSTALL_LOG_PREFIX,
    INSTALL_LOG_SUFFIX,
    INSTALL_TRANSCRIPTS_KEPT,
    PREVIOUS_ENV_DIRNAME,
    PREVIOUS_ENVS_KEPT,
    STAGING_ENV_DIRNAME,
    UPDATE_PROGRESS_FILENAME,
    UPDATE_PROGRESS_STAGE_ORDER,
    UPDATE_PROGRESS_STAGES,
    UPDATE_STAGE_DIRNAME,
    active_update,
    read_update_progress,
    update_progress_path,
)
from my_claude_code.core.process_handoff import (
    reset_process_handoff_for_tests,
    set_external_upgrade_helper_pending,
)
from my_claude_code.core.stop_deadline import (
    HARD_EXIT_GRACE_SECONDS,
    STOP_TEARDOWN_MARGIN_SECONDS,
    clamp_stop_budget,
)
from my_claude_code.core.version import (
    LEGACY_DISTRIBUTION,
    NATIVE_DISTRIBUTION,
)

# The canonical distribution this release installs.
PACKAGE_NAME = NATIVE_DISTRIBUTION
# Kept in step with the URLs in scripts/install.sh and scripts/install.ps1.
RELEASE_REPO = "FiredMosquito831/my-claude-code"
_LATEST_RELEASE_URL = f"https://api.github.com/repos/{RELEASE_REPO}/releases/latest"
_CACHE_TTL_SECONDS = 6 * 3600.0
_HTTP_TIMEOUT_SECONDS = 10.0
_UPGRADE_TIMEOUT_SECONDS = 900.0
_WHEEL_SUFFIX = ".whl"
_WINDOWS = os.name == "nt"
_STAGE_DIRNAME = UPDATE_STAGE_DIRNAME
_PENDING_RESULT_FILENAME = "pending-upgrade.json"
#: The progress receipt's name, stage vocabulary and readers moved to
#: ``config.update_progress`` in 6.58.3: ``cli`` may not import ``application``
#: (import-boundary contract) and ``cli.desktop_status`` now has to publish
#: whether a helper is running. Re-exported here because the module that
#: *writes* the receipt is still this one, and every existing importer named
#: it here.
__all__ = [
    "UPDATE_PROGRESS_FILENAME",
    "UPDATE_PROGRESS_STAGES",
    "active_update",
    "current_version",
    "get_release_status",
    "perform_upgrade",
    "update_progress",
    "update_progress_path",
    "upgrade_to_latest",
]
# Bound on how long the helper waits for this process to exit before it stops
# waiting and ends the parent itself. It is the SERVER'S OWN stop budget, not a
# number of the helper's own: the parent bounds its stop at
# ``SERVER_GRACEFUL_SHUTDOWN_SECONDS`` plus a fixed teardown margin and
# hard-exits one beat later, so a parent still alive past that is not draining
# any more and no amount of further waiting will change that. The old value was
# a flat 3600, which is why deferred helpers sat resident for an hour behind a
# server that could never finish its stop.
_HELPER_WAIT_FLOOR_SECONDS = 30.0


def _helper_wait_seconds() -> float:
    """Seconds the deferred helper waits for this server to exit."""

    budget = clamp_stop_budget(get_settings().server_graceful_shutdown_seconds)
    return max(
        _HELPER_WAIT_FLOOR_SECONDS,
        budget + STOP_TEARDOWN_MARGIN_SECONDS + HARD_EXIT_GRACE_SECONDS,
    )


# Extra seconds the dashboard waits beyond install + graceful drain for the
# new process to bind and come back online.
_DASHBOARD_RECONNECT_STARTUP_MARGIN_SECONDS = 120.0


def current_version() -> str:
    """Version of the running package, or ``unknown`` outside an install.

    The migration leaves either owner installed (a legacy ``free-claude-code``
    tool or the native ``my-claude-code`` tool), and an install that is midway
    between the two can even hold a stale copy under the other name. Try the
    native distribution first, then the legacy one, so the running server always
    reports its real version instead of "unknown".
    """
    for distribution in (NATIVE_DISTRIBUTION, LEGACY_DISTRIBUTION):
        try:
            return installed_version(distribution)
        except PackageNotFoundError:
            continue
    return "unknown"


def parse_version(text: str | None) -> tuple[int, ...]:
    """Parse ``4.14.2`` or ``v4.14.2`` into a comparable tuple.

    Compares numerically rather than lexically so 4.14.10 correctly sorts
    above 4.14.9. Unparseable input sorts lowest so it never looks newer.
    """
    if not text:
        return ()
    cleaned = text.strip().lstrip("vV")
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(candidate: str | None, baseline: str | None) -> bool:
    """Whether ``candidate`` is a strictly newer release than ``baseline``."""
    parsed_candidate = parse_version(candidate)
    parsed_baseline = parse_version(baseline)
    if not parsed_candidate or not parsed_baseline:
        return False
    return parsed_candidate > parsed_baseline


@dataclass(slots=True)
class ReleaseStatus:
    """What the dashboard needs to render the version and update banner."""

    current: str
    latest: str | None = None
    update_available: bool = False
    release_url: str | None = None
    release_name: str | None = None
    release_notes: str | None = None
    published_at: str | None = None
    checked_at: float | None = None
    restart_required: bool = False
    staged_install: bool = False
    # Outcome of a deferred (Windows) install that ran after a shutdown.
    pending_upgrade: dict[str, Any] | None = None
    error: str | None = None
    # Seconds the admin UI should wait for this server to come back after a
    # self-triggered upgrade. Composed from the install + graceful-drain +
    # startup budget (not a fixed client constant), so the dashboard's
    # reconnect window tracks the real cost of the handoff.
    dashboard_reconnect_timeout_seconds: float = DASHBOARD_RECONNECT_TIMEOUT_SECONDS
    # The desktop app's own half of "are you up to date". The wheel updates
    # itself every release; until 6.60.0 the window never did, because the pin
    # was enforced by a process (the Python tray) that need not be running --
    # so a user could sit on a 15-release-old window while the banner said
    # everything was current. The banner now covers both halves.
    shell_installed_tag: str | None = None
    shell_pinned_tag: str | None = None
    shell_update_available: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "update_available": self.update_available,
            "release_url": self.release_url,
            "release_name": self.release_name,
            "release_notes": self.release_notes,
            "published_at": self.published_at,
            "checked_at": self.checked_at,
            "restart_required": self.restart_required,
            "staged_install": self.staged_install,
            "pending_upgrade": self.pending_upgrade,
            "error": self.error,
            "dashboard_reconnect_timeout_seconds": (
                self.dashboard_reconnect_timeout_seconds
            ),
            "shell_installed_tag": self.shell_installed_tag,
            "shell_pinned_tag": self.shell_pinned_tag,
            "shell_update_available": self.shell_update_available,
        }


@dataclass(slots=True)
class UpgradeResult:
    """Outcome of one upgrade attempt."""

    ok: bool
    message: str
    installed_version: str | None = None
    log: list[str] = field(default_factory=list)
    #: Where the installer writes its transcript while it runs, and where the
    #: stage receipt is appended. Both are named in the response because the
    #: browser tab that asked for this update is about to lose the server that
    #: served it (spec F7): once the port stops answering, a page in an
    #: ordinary tab has no channel left -- it cannot read a file on the disk --
    #: so the honest thing is to hand over the two paths BEFORE the outage and
    #: say plainly that the desktop app is the thing that can narrate it.
    #: ``None`` on the POSIX path, where the install happens in-process and
    #: there is no deferred helper to tail.
    log_path: str | None = None
    progress_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "installed_version": self.installed_version,
            "log": self.log,
            "log_path": self.log_path,
            "progress_path": self.progress_path,
        }


class _ReleaseCache:
    """Cache the release lookup and collapse concurrent checks into one call."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._payload: dict[str, Any] | None = None
        self._checked_at: float | None = None
        self._error: str | None = None
        self.restart_required = False
        # True when the install was staged for shutdown rather than applied.
        self.staged_install = False

    async def get(
        self, *, force: bool
    ) -> tuple[dict[str, Any] | None, float | None, str | None]:
        async with self._lock:
            fresh = (
                self._checked_at is not None
                and time.time() - self._checked_at < _CACHE_TTL_SECONDS
            )
            if fresh and not force:
                return self._payload, self._checked_at, self._error
            payload, error = await _fetch_latest_release()
            if payload is not None or not fresh:
                # Keep the last good payload when a refresh fails, so a
                # transient network problem does not blank the version panel.
                self._payload = payload if payload is not None else self._payload
                self._checked_at = time.time()
                self._error = error
            return self._payload, self._checked_at, self._error


_CACHE = _ReleaseCache()


async def _fetch_latest_release() -> tuple[dict[str, Any] | None, str | None]:
    """Read the latest release, returning ``(payload, error)``; never raises."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                _LATEST_RELEASE_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Offline or rate-limited is an expected, non-fatal condition: the
        # dashboard still renders the running version.
        logger.debug("Release check failed: {}", type(exc).__name__)
        return None, f"Could not reach the release feed ({type(exc).__name__})."
    if not isinstance(payload, dict):
        return None, "Unexpected release feed response."
    return payload, None


async def get_release_status(*, force: bool = False) -> ReleaseStatus:
    """Current version plus the latest published release, best effort."""
    running = current_version()
    payload, checked_at, error = await _CACHE.get(force=force)
    pending = pending_upgrade_result()
    status = ReleaseStatus(
        current=running,
        checked_at=checked_at,
        error=error,
        restart_required=_CACHE.restart_required,
        staged_install=_CACHE.staged_install,
        pending_upgrade=pending,
    )
    _apply_desktop_shell_state(status)
    # Track the real handoff cost: install budget + the operator's configured
    # graceful-drain budget + a startup margin, so the dashboard's reconnect
    # window follows the live setting rather than the default constant.
    status.dashboard_reconnect_timeout_seconds = (
        _UPGRADE_TIMEOUT_SECONDS
        + get_settings().server_graceful_shutdown_seconds
        + _DASHBOARD_RECONNECT_STARTUP_MARGIN_SECONDS
    )
    # A deferred helper writes this after the old process has exited. The first
    # version-status response from the relaunched server carries the outcome to
    # the dashboard, then consumes it so a historical success or failure does
    # not become a permanent banner.
    if pending is not None:
        clear_pending_upgrade_result()
    if payload is None:
        return status
    latest = str(payload.get("tag_name") or "").lstrip("vV") or None
    status.latest = latest
    status.release_url = payload.get("html_url")
    status.release_name = payload.get("name")
    status.release_notes = _release_notes(payload.get("body"))
    status.published_at = payload.get("published_at")
    status.update_available = is_newer(latest, running)
    return status


def _apply_desktop_shell_state(status: ReleaseStatus) -> None:
    """Fill in the three desktop-app keys. Best effort, and a read only.

    Imported inside the function on purpose:
    ``tests/contracts/test_desktop_shell_not_on_the_server_path.py`` asserts
    that a fresh interpreter which builds the ASGI app has never imported
    ``config.desktop_shell`` -- it costs ``tarfile``, ``zipfile``, ``hashlib``
    and ``urllib`` for a download only ``mcc-desktop`` makes. Answering a
    dashboard request is not the server's cold start, so paying for it here is
    fine; paying for it at import time is not.

    Any failure leaves the three keys at their defaults. A version banner that
    could not read a receipt must still render the version.
    """

    try:
        from my_claude_code.config.desktop_shell import desktop_shell_update_report

        report = desktop_shell_update_report()
    except Exception as exc:
        logger.debug("Desktop app pin check failed: {}", type(exc).__name__)
        return
    installed = report.get("shell_installed_tag")
    pinned = report.get("shell_pinned_tag")
    status.shell_installed_tag = installed if isinstance(installed, str) else None
    status.shell_pinned_tag = pinned if isinstance(pinned, str) else None
    status.shell_update_available = bool(report.get("shell_update_available"))


def _uv_tool_dir(uv_executable: str | None = None) -> Path | None:
    """Return uv's platform-correct tool root, honoring ``UV_TOOL_DIR``."""
    uv = uv_executable or shutil.which("uv")
    if uv is None:
        return None
    try:
        completed = subprocess.run(
            [uv, "tool", "dir"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    path = completed.stdout.strip()
    return Path(path) if completed.returncode == 0 and path else None


def _installed_tool_dir() -> Path | None:
    """The tool environment this process runs from, found without calling uv.

    ``sys.executable`` inside a uv tool environment is ``<tool dir>/Scripts/
    python.exe`` (``bin/python`` on POSIX), and uv keeps ``uv-receipt.toml``
    in that same ``<tool dir>``. Deriving it this way matters: the deferred
    upgrade path must not run uv at all while the server is still alive, which
    is the whole reason it defers. Returns None when this is not a uv tool
    environment (a development checkout, say), and the helper then falls back
    to the family pattern and the entry points alone.
    """
    candidate = Path(sys.executable).resolve().parent.parent
    if (candidate / "uv-receipt.toml").is_file():
        return candidate
    return None


def _aside_root(dirname: str, tool_dir: Path | None = None) -> Path | None:
    """``<uv tools root>/../<dirname>``: where an update keeps its spare copies.

    Beside uv's tools root, never inside it. A directory inside the tools root
    whose name does not normalise to a valid package name makes ``uv tool
    list`` fail outright -- measured on uv 0.11.21::

        error: Not a valid package or extra name: ".mcc-previous".

    and then list nothing at all, which is worse than the malformed-tool
    warnings the existing ``my-claude-code.old-<stamp>`` directories produce.
    A sibling is invisible to uv and still on the same volume, so the swap
    stays a rename rather than a copy.

    Derived from ``tool_dir`` (itself derived from ``sys.executable``) rather
    than from ``uv tool dir``, because no uv may run while the server is alive.
    """

    env_dir = tool_dir if tool_dir is not None else _installed_tool_dir()
    if env_dir is None:
        return None
    return env_dir.parent.parent / dirname


def _is_superseded_env_dir(name: str) -> bool:
    """Whether ``name`` is an environment an installer renamed aside.

    ``scripts/install.ps1``'s rename-then-reinstall ladder leaves
    ``my-claude-code.old-<stamp>`` beside the live environment, inside uv's
    tools root. uv normalises that directory name into the *tool name*
    ``my-claude-code-old-<stamp>``, which is a valid package name, so it tries
    to read it as a tool and prints::

        warning: Ignoring malformed tool `my-claude-code-old-20260907-193858`

    on every single ``uv tool`` command. Four of them had accumulated on the
    machine this was found on, and nothing had ever swept one.
    """

    for distribution in (NATIVE_DISTRIBUTION, LEGACY_DISTRIBUTION):
        if name.startswith(f"{distribution}.old-"):
            return True
    return False


def sweep_superseded_environments() -> str | None:
    """Move aside-copies out of uv's tools root and keep exactly one.

    Runs once per server start, on the post-readiness thread, never on a
    request path. Three jobs, all best-effort:

    * every ``<distribution>.old-<stamp>`` directory left in uv's tools root by
      an older installer moves to ``<tools root>/../.mcc-previous/<stamp>/``,
      where uv does not look, so ``uv tool list`` stops warning about it;
    * the previous-environment directory is pruned to
      :data:`PREVIOUS_ENVS_KEPT` -- it is the rollback, and a second one is
      only disk;
    * installer transcripts are pruned to :data:`INSTALL_TRANSCRIPTS_KEPT`.

    Returns one sentence for the log when it did something, and ``None`` when
    there was nothing to do -- a sweep that ran and found nothing should be
    silent, or every start would say so.
    """

    tool_dir = _installed_tool_dir()
    done: list[str] = []
    previous_root = _aside_root(PREVIOUS_ENV_DIRNAME, tool_dir)
    if tool_dir is not None and previous_root is not None:
        tools_root = tool_dir.parent
        moved = 0
        for candidate in sorted(tools_root.iterdir()):
            if not candidate.is_dir() or not _is_superseded_env_dir(candidate.name):
                continue
            stamp = candidate.name.partition(".old-")[2] or candidate.name
            destination = previous_root / stamp / candidate.name.split(".old-")[0]
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                candidate.replace(destination)
                moved += 1
            except OSError as exc:
                logger.debug(f"Could not move {candidate} aside: {exc}")
        if moved:
            done.append(
                f"moved {moved} superseded environment(s) out of uv's tools root"
            )
    if previous_root is not None and previous_root.is_dir():
        removed = 0
        kept = sorted(
            (child for child in previous_root.iterdir() if child.is_dir()),
            key=lambda child: child.name,
            reverse=True,
        )
        for stale in kept[PREVIOUS_ENVS_KEPT:]:
            try:
                shutil.rmtree(stale)
                removed += 1
            except OSError as exc:
                logger.debug(f"Could not remove {stale}: {exc}")
        if removed:
            done.append(f"removed {removed} superseded copy(ies)")
    # A staging directory only survives an update that did not finish -- the
    # machine lost power, the helper was killed, uv refused the wheel. It is a
    # whole environment's worth of disk and nothing will ever read it again,
    # because every episode stamps a directory of its own. The live server
    # reaching readiness is proof that no update is in flight, so this is the
    # safe moment to drop them.
    staging_root = _aside_root(STAGING_ENV_DIRNAME, tool_dir)
    if staging_root is not None and staging_root.is_dir():
        abandoned = 0
        for stale in sorted(staging_root.iterdir()):
            if not stale.is_dir():
                continue
            try:
                shutil.rmtree(stale)
                abandoned += 1
            except OSError as exc:
                logger.debug(f"Could not remove {stale}: {exc}")
        if abandoned:
            done.append(f"removed {abandoned} abandoned staging environment(s)")
    transcripts = sorted(
        _stage_dir().glob(f"{INSTALL_LOG_PREFIX}*{INSTALL_LOG_SUFFIX}"),
        key=lambda path: path.name,
        reverse=True,
    )
    dropped = 0
    for stale_log in transcripts[INSTALL_TRANSCRIPTS_KEPT:]:
        try:
            stale_log.unlink()
            dropped += 1
        except OSError as exc:
            logger.debug(f"Could not remove {stale_log}: {exc}")
    if dropped:
        done.append(f"removed {dropped} old installer transcript(s)")
    if not done:
        return None
    return "Update housekeeping: " + "; ".join(done) + "."


def _health_url() -> str:
    """Where the cutover asks whether the new server actually came up.

    The server knows its own address and the helper does not, so the address is
    baked into the helper at generation time. ``0.0.0.0`` is a bind address,
    not a destination: asking it would be asking every interface at once, so
    the loopback spelling is used to talk to ourselves.
    """

    return f"{local_proxy_root_url(get_settings())}/health"


def _uv_tool_bin_dir(uv_executable: str | None = None) -> Path | None:
    """Return the stable executable directory outside a uv tool environment."""
    uv = uv_executable or shutil.which("uv")
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


def _server_launcher(uv_executable: str | None = None) -> Path | None:
    """Resolve the launcher which survives replacement of the tool environment."""
    bin_dir = _uv_tool_bin_dir(uv_executable)
    if bin_dir is not None:
        candidate = bin_dir / ("mcc-server.exe" if os.name == "nt" else "mcc-server")
        if candidate.is_file():
            return candidate
    found = shutil.which("mcc-server")
    return Path(found) if found else None


def _receipt_path(uv_executable: str | None = None) -> Path:
    """The uv receipt for the installed owner, native first, legacy fallback.

    The migration can leave either ``my-claude-code`` (native) or
    ``free-claude-code`` (legacy) owning the tool environment; a mid-migration
    upgrade may even find the old name still installed. Return the first receipt
    that exists so extras/Python are carried from whichever owner is real.
    """
    root = _uv_tool_dir(uv_executable)
    if root is None:
        # Last-resort compatibility path for an old uv install or tests which
        # deliberately run without uv on PATH.
        root = Path.home() / ".local" / "share" / "uv" / "tools"
    for distribution in (NATIVE_DISTRIBUTION, LEGACY_DISTRIBUTION):
        candidate = root / distribution / "uv-receipt.toml"
        if candidate.is_file():
            return candidate
    return root / NATIVE_DISTRIBUTION / "uv-receipt.toml"


def _wsl_windows_mount_tool_dir(uv_executable: str | None = None) -> bool:
    """Whether WSL stores uv's tool environment on a Windows DrvFs mount."""
    if _WINDOWS or not os.getenv("WSL_DISTRO_NAME"):
        return False
    root = _uv_tool_dir(uv_executable)
    if root is None:
        return False
    # Check the WSL spelling before ``Path.resolve()``. A Windows test runner
    # resolves ``/mnt/c`` as a drive-relative Windows path even when simulating
    # WSL, while inside WSL the uv command returns this POSIX spelling directly.
    normalized = root.as_posix().rstrip("/")
    return normalized == "/mnt" or normalized.startswith("/mnt/")


def _installed_extras_and_python(
    uv_executable: str | None = None,
) -> tuple[list[str], str]:
    """Recover the extras and Python pin uv recorded for this install.

    Reinstalling without them would silently drop optional features such as
    voice support, so they are carried across the upgrade.
    """
    default_python = ".".join(str(part) for part in sys.version_info[:3])
    receipt = _receipt_path(uv_executable)
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except OSError, tomllib.TOMLDecodeError:
        return [], default_python
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return [], default_python
    python = str(tool.get("python") or default_python)
    extras: list[str] = []
    requirements = tool.get("requirements")
    if isinstance(requirements, list):
        for requirement in requirements:
            if (
                isinstance(requirement, dict)
                and requirement.get("name")
                in (NATIVE_DISTRIBUTION, LEGACY_DISTRIBUTION)
                and isinstance(requirement.get("extras"), list)
            ):
                extras = [str(extra) for extra in requirement["extras"]]
                break
    return extras, python


def _select_wheel_asset(payload: dict[str, Any]) -> dict[str, Any] | None:
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if isinstance(asset, dict) and str(asset.get("name", "")).endswith(
            _WHEEL_SUFFIX
        ):
            return asset
    return None


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stage_dir() -> Path:
    """``<config dir>/updates``. Naming it creates nothing; see below."""

    return config_dir_path() / _STAGE_DIRNAME


def _prepared_stage_dir() -> Path:
    """``<config dir>/updates``, created if it is not there yet.

    It used to be created as a side effect of the wheel download
    (``<updates>/wheel``), and 6.82.0 removed that download -- the installer is
    the only downloader now. On a configuration directory that had never seen
    an update the first thing written into it became ``apply-upgrade.ps1``, and
    the update failed with ``[Errno 2] No such file or directory``. Found by
    running the real dashboard path against a scratch install, which is the
    only place it could have been found.

    Separate from ``_stage_dir`` because that one is called by READERS -- the
    start-up sweep, the progress reader -- and a reader that creates a
    directory is a reader that writes. Putting the ``mkdir`` there made the
    post-readiness sweep create ``~/.mcc/updates`` on a machine that had never
    updated, which the test suite's hermeticity guard caught at once.
    """

    stage = _stage_dir()
    with suppress(OSError):
        stage.mkdir(parents=True, exist_ok=True)
    return stage


def update_progress() -> dict[str, Any] | None:
    """The most recent stage the deferred helper reported, if any.

    The reader itself is ``config.update_progress.read_update_progress``; this
    name stays because it is the one the dashboard and the tests already use.
    """

    return read_update_progress()


def pending_upgrade_result() -> dict[str, Any] | None:
    """Outcome written by a deferred (Windows) upgrade, if one has finished.

    Read on status so a deferred install that failed after this process exited
    is still reported instead of vanishing. Read as ``utf-8-sig``: helpers
    written by older builds under Windows PowerShell 5.1 prepend a UTF-8 BOM,
    which ``json.loads`` refuses.
    """

    path = _stage_dir() / _PENDING_RESULT_FILENAME
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def clear_pending_upgrade_result() -> None:
    """Drop a consumed deferred-upgrade outcome."""

    with suppress(OSError):
        (_stage_dir() / _PENDING_RESULT_FILENAME).unlink()


def _process_creation_filetime() -> int:
    """This process's creation time as a Windows FILETIME (UTC ticks).

    Pairs with ``Process.StartTime.ToFileTimeUtc()`` in PowerShell so the helper
    can tell our parent apart from a later process that reused its id.
    """

    # Keyed off the real platform, not ``_WINDOWS``: tests flip that flag to
    # exercise the staging path on Linux CI, and this call must still not
    # reach for a Win32 API that isn't there. 0 makes the helper fall back to
    # matching on the process id alone.
    if os.name != "nt":
        return 0
    # Imported here, not at module scope: ``ctypes.wintypes`` raises on
    # non-Windows, and this module is imported on every platform.
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Without explicit argtypes the HANDLE is truncated to 32 bits on 64-bit
    # Windows and the call fails, silently yielding a useless 0.
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    creation = wintypes.FILETIME()
    unused = (wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME())
    ok = kernel32.GetProcessTimes(
        kernel32.GetCurrentProcess(),
        ctypes.byref(creation),
        ctypes.byref(unused[0]),
        ctypes.byref(unused[1]),
        ctypes.byref(unused[2]),
    )
    if not ok:
        # Without a start time the helper falls back to the id alone, which is
        # the previous behaviour rather than a new failure mode.
        return 0
    return (creation.dwHighDateTime << 32) | creation.dwLowDateTime


def _powershell_literal(value: str) -> str:
    """Quote a value for a PowerShell single-quoted string."""

    return "'" + value.replace("'", "''") + "'"


def _published_commands() -> list[str]:
    """Every console and GUI command this distribution installs as a shim.

    Read from the installed distribution's entry points rather than a
    hand-written list, so it cannot drift from ``pyproject.toml``.
    """

    try:
        entry_points = installed_distribution(PACKAGE_NAME).entry_points
    except PackageNotFoundError:
        return []
    return sorted(
        {
            entry.name
            for entry in entry_points
            if entry.group in {"console_scripts", "gui_scripts"}
        }
    )


def _bundled_installer(name: str) -> Path | None:
    """The installer script that shipped inside this wheel, or ``None``.

    6.82.0. The helper is a launcher of the official installer, so it needs a
    copy of that installer that is guaranteed to be present, to be the one this
    release was tested with, and to need no network of its own. The repository's
    ``scripts/install.ps1`` and ``scripts/install.sh`` are force-included into
    the wheel for exactly this (``pyproject.toml``); a contract test builds a
    real wheel and fails if either is missing from it.
    """

    candidate = Path(__file__).resolve().parent.parent / "installers" / name
    return candidate if candidate.is_file() else None


def _deferred_helper_script(
    *,
    result_path: Path,
    stage_dir: Path,
    installer: Path,
    powershell: str,
    config_dir: Path,
    working_directory: Path,
    wait_seconds: float | None = None,
    version: str | None = None,
    no_restart: bool = False,
    no_start: bool = False,
    install_log: Path | None = None,
) -> str:
    """PowerShell that waits for this process to exit, then runs the installer.

    6.82.0 gutted this. It used to be a thousand lines that downloaded nothing,
    staged an environment, execute-verified it, swapped it in, health-gated the
    result and rolled back -- a complete second implementation of the install,
    reachable only from the dashboard, and therefore the only one that was ever
    exercised by an update. The hand-run installer did something else, which is
    how 2026-09-11 ended with two installs that both exited 0 and a machine
    with no server. All of that logic now lives in ``scripts/install.ps1``,
    which is the command a user would type, and this script's whole job is:

    1. wait for the server that asked for the update to exit, so the tool
       environment is free (this is the one thing only the server can do -- it
       knows its own pid),
    2. run the official installer with ``-Restart``,
    3. record the outcome in ``pending-upgrade.json``.

    ``no_restart`` no longer means "someone else will start the server"
    (decision Q2, 2026-09-11 15:34). That belief is what left the machine dead:
    the desktop app claimed the restart, was an old build that could not act,
    and because it had claimed the job nobody else did it. It now means only
    "a desktop window is watching this", which is a fact for the transcript and
    nothing else -- **the installer always restarts**. ``no_start`` is the real
    opt-out and comes from ``MCC_INSTALL_NO_START`` in the server's own
    environment.

    Written as PowerShell rather than Python because the only interpreter we
    can rely on is the one inside the environment being replaced -- using it
    would hold the very directory uv needs to swap.

    Receipts go through ``[System.IO.File]::AppendAllText`` with a BOM-less
    ``UTF8Encoding($false)``: Windows PowerShell 5.1's ``Set-Content
    -Encoding utf8`` prepends a UTF-8 BOM and the Python reader parses JSON,
    which refuses a leading U+FEFF. The receipt is APPEND-ONLY (decision Q5);
    truncating it is how a hand-run install erased a finished helper's whole
    record at 15:04 on 2026-09-11 while a window was reading it.
    """

    progress_path = stage_dir / UPDATE_PROGRESS_FILENAME
    if install_log is None:
        install_log = stage_dir / (
            f"{INSTALL_LOG_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}{INSTALL_LOG_SUFFIX}"
        )
    wait_budget = (
        _helper_wait_seconds() if wait_seconds is None else float(wait_seconds)
    )
    installer_args = ["-Restart"]
    if no_start:
        installer_args = ["-NoStart"]
    if version:
        installer_args += ["-Version", version]
    quoted_installer_args = ", ".join(
        _powershell_literal(arg) for arg in installer_args
    )
    # Written out from the Python table rather than typed twice. It WAS typed
    # twice until 6.72.0, and the two copies disagreed the moment a stage was
    # added -- which a monotonic guard turns into silently dropped records
    # rather than a visible error.
    stage_order_literal = "\n".join(
        f"    {_powershell_literal(stage)} = {rank}"
        for stage, rank in UPDATE_PROGRESS_STAGE_ORDER.items()
    )
    return f"""$ErrorActionPreference = 'Stop'
$parent = {os.getpid()}
$helperPid = $PID
$helperStarted = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$targetVersion = {_powershell_literal(version or "")}
$noRestart = {"$true" if no_restart else "$false"}
$noStart = {"$true" if no_start else "$false"}
$progressPath = {_powershell_literal(str(progress_path))}
$installLog = {_powershell_literal(str(install_log))}
$resultPath = {_powershell_literal(str(result_path))}
$installer = {_powershell_literal(str(installer))}
$powershell = {_powershell_literal(powershell)}
$configDir = {_powershell_literal(str(config_dir))}
$workingDirectory = {_powershell_literal(str(working_directory))}
$progressEncoding = New-Object System.Text.UTF8Encoding($false)
$stageOrder = @{{
{stage_order_literal}
}}
$script:Rank = 0
function Write-InstallLog($text) {{
    try {{
        $stampNow = (Get-Date).ToUniversalTime().ToString('HH:mm:ss')
        $body = if ($null -eq $text) {{ '' }}
            elseif ($text -is [System.Management.Automation.ErrorRecord]) {{ [string] $text.Exception.Message }}
            else {{ [string] $text }}
        # One append per line rather than a held stream: a reader in another
        # process must see the line the moment it exists, and this helper can
        # be killed at any point without truncating what it already said.
        [System.IO.File]::AppendAllText($installLog, ('[' + $stampNow + '] ' + $body + [Environment]::NewLine), $progressEncoding)
    }}
    catch {{
        # A transcript nobody can write must never be the reason an update fails.
    }}
}}
function Write-Stage($stage, $message) {{
    try {{
        $rank = 0
        if ($stageOrder.ContainsKey($stage)) {{ $rank = [int] $stageOrder[$stage] }}
        if ($rank -eq 0) {{ $rank = $script:Rank }}
        if ($rank -lt $script:Rank) {{ return }}
        $script:Rank = $rank
        $nowSeconds = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $record = [ordered]@{{
            stage           = $stage
            message         = $message
            at              = (Get-Date).ToUniversalTime().ToString('o')
            parent          = $parent
            helper_pid      = $helperPid
            started_at      = $helperStarted
            elapsed_seconds = [math]::Round($nowSeconds - $helperStarted, 3)
            helper_done     = (@('done', 'failed', 'recovered') -contains $stage)
            version         = $targetVersion
            log             = $installLog
            source          = 'apply-upgrade.ps1'
            restarted       = $null
            holder          = ''
        }}
        # APPEND, never truncate (decision Q5). An episode is opened by the
        # 'episode' marker above, so a watcher that looks a minute late can
        # still find where the current episode begins.
        [System.IO.File]::AppendAllText($progressPath, (($record | ConvertTo-Json -Compress) + [Environment]::NewLine), $progressEncoding)
    }}
    catch {{
        # A receipt nobody can write must never be the reason an update fails.
    }}
}}
try {{ [System.IO.File]::WriteAllText($installLog, '', $progressEncoding) }} catch {{ }}
Write-Stage 'episode' 'An update started.'
Write-InstallLog ('My Claude Code update helper, pid ' + $helperPid + ', target ' + $(if ($targetVersion) {{ $targetVersion }} else {{ 'the latest release' }}) + '.')
Write-InstallLog ('The installer owns the restart. A desktop window ' + $(if ($noRestart) {{ 'is' }} else {{ 'is not' }}) + ' watching.')
Write-Stage 'waiting-for-parent' 'Waiting for the running server to stop.'
# Windows recycles process ids quickly, so a bare Get-Process -Id would happily
# match an unrelated process that inherited ours and wait out the full deadline
# without ever installing. Pin the identity with the creation time too: same id
# but a different start time means our parent is gone.
$parentStart = {_process_creation_filetime()}
function Test-ParentAlive {{
    $proc = Get-Process -Id $parent -ErrorAction SilentlyContinue
    if (-not $proc) {{ return $false }}
    # 0 means we could not read our own creation time; fall back to the id
    # alone, which is the old behaviour rather than a new failure mode.
    if ($parentStart -eq 0) {{ return $true }}
    try {{ return $proc.StartTime.ToFileTimeUtc() -eq $parentStart }}
    catch {{ return $false }}   # access denied reading StartTime => not ours
}}
$deadline = (Get-Date).AddSeconds({wait_budget:.1f})
while ((Get-Date) -lt $deadline) {{
    if (-not (Test-ParentAlive)) {{ break }}
    Start-Sleep -Milliseconds 500
}}
# The parent was given its whole configured stop budget plus the teardown
# margin its own watchdog uses. Still alive past that means it is not draining,
# it is stuck -- and the old behaviour (wait an hour, then write a failure
# receipt and exit) left the user with neither a running new version nor an
# installed one. Escalate to the EXACT pid we were given, whose identity is
# still pinned by its creation time.
if (Test-ParentAlive) {{
    Write-InstallLog ('The server (pid ' + $parent + ') outlived its stop budget; ending it.')
    Stop-Process -Id $parent -Force -ErrorAction SilentlyContinue
    $killDeadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $killDeadline) {{
        if (-not (Test-ParentAlive)) {{ break }}
        Start-Sleep -Milliseconds 250
    }}
}}
if (Test-ParentAlive) {{
    Write-InstallLog 'The server could not be stopped. Nothing was installed.'
    Write-Stage 'failed' 'The server could not be stopped, so the update was not applied.'
    $result = @{{ ok = $false; restarted = $false; message = 'The server could not be stopped, so the update was not applied.' }}
    [System.IO.File]::WriteAllText($resultPath, ($result | ConvertTo-Json), $progressEncoding)
    exit 1
}}
Write-InstallLog 'The running server exited; the environment is free.'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {{
    $message = 'The installer that ships with this version was not found at ' + $installer + '. Re-run the install command.'
    Write-InstallLog $message
    Write-Stage 'failed' $message
    $result = @{{ ok = $false; restarted = $false; message = $message }}
    [System.IO.File]::WriteAllText($resultPath, ($result | ConvertTo-Json), $progressEncoding)
    exit 1
}}
# ===========================================================================
# HAND OVER TO THE OFFICIAL INSTALLER. Everything an update does -- download,
# verify, stage beside the running version, execute-verify, stop exactly the
# one server this configuration directory is for, swap, start, health-gate,
# roll back -- happens in there, which is also what a user gets when they type
# the install command by hand. One update path (decision Q1).
#
# MCC_INSTALL_LOG makes it APPEND to this episode's transcript instead of
# opening a second one, so a window tailing this file sees the whole story.
# MCC_CONFIG_DIR is explicit rather than merely inherited: the restart means
# the server of the directory this update is for, and a start that lost it
# once came up for a different configuration home.
# ===========================================================================
$env:MCC_INSTALL_LOG = $installLog
$env:MCC_CONFIG_DIR = $configDir
Write-InstallLog ('Running ' + $installer + ' {" ".join(installer_args)}.')
$installerArgs = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $installer) + @({quoted_installer_args})
$exitCode = 1
try {{
    $ErrorActionPreference = 'Continue'
    & $powershell @installerArgs 2>&1 |
        ForEach-Object {{
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) {{ [string] $_.Exception.Message }} else {{ [string] $_ }}
            Write-InstallLog $line
        }}
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
}}
catch {{
    $ErrorActionPreference = 'Stop'
    Write-InstallLog ('The installer could not be started: ' + $_.Exception.Message)
    $exitCode = 1
}}
Write-InstallLog ('The installer exited with ' + $exitCode + '.')
# The installer wrote the terminal record itself -- `done` with `restarted:
# true` once a listener answered /health, `failed` or `recovered` otherwise --
# so this helper does not write a second one and does not overrule it. What it
# DOES own is `pending-upgrade.json`, which the dashboard reads on its next
# connection to say what the last update did.
$ok = ($exitCode -eq 0)
$result = @{{
    ok = $ok
    exit_code = $exitCode
    attempts = 1
    staged = $true
    restarted = $(if ($noStart) {{ $false }} else {{ $ok }})
    message = $(if ($ok) {{
        if ($noStart) {{ 'The update was installed. No server was started, because MCC_INSTALL_NO_START is set.' }}
        else {{ 'The update was installed and the server was restarted by the installer.' }}
    }} else {{ 'The installer did not finish; see the transcript at ' + $installLog + '.' }})
}}
[System.IO.File]::WriteAllText($resultPath, ($result | ConvertTo-Json), $progressEncoding)
if (-not $ok) {{
    # The installer's own terminal record is the authority on WHAT happened.
    # Only write one here when it never got far enough to write any at all.
    $wroteTerminal = $false
    try {{
        foreach ($line in [System.IO.File]::ReadAllLines($progressPath)) {{
            if ($line -match '"helper_done":true') {{ $wroteTerminal = $true }}
        }}
    }}
    catch {{ }}
    if (-not $wroteTerminal) {{ Write-Stage 'failed' $result.message }}
}}
exit $(if ($ok) {{ 0 }} else {{ 1 }})
"""


def _powershell_child_environment() -> dict[str, str]:
    """This process's environment, minus a ``PSModulePath`` it must not pass on.

    Measured on 2026-09-12, running the real update flow against a scratch
    install. A server started from a PowerShell 7 prompt inherits PowerShell
    7's ``PSModulePath``, whose first entries are
    ``C:\\Program Files\\PowerShell\\7\\Modules``. The helper then starts
    **Windows PowerShell 5.1** (``shutil.which("powershell")``), which
    autoloads ``Microsoft.PowerShell.Utility`` off that path, finds PowerShell
    7's copy, cannot load it -- and every cmdlet in that module simply does not
    exist for the rest of the run::

        Get-FileHash : The term 'Get-FileHash' is not recognized as the name
        of a cmdlet ... At install.ps1:833

    which failed the release wheel's checksum step and ended the update. It
    would equally have taken ``ConvertTo-Json``, ``Invoke-WebRequest`` and
    ``Get-FileHash`` away from the old helper.

    Unsetting the variable is the fix rather than rewriting it: PowerShell
    computes the correct default for its own edition when it is absent, and
    guessing a path here would be this module holding an opinion about a
    layout it cannot see.
    """

    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() != "PSMODULEPATH"
    }


def _spawn_deferred_upgrade(
    *, tag: str, log: list[str], no_restart: bool = False
) -> UpgradeResult:
    """Hand the update to a detached helper that runs the installer after we exit."""

    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        return UpgradeResult(
            ok=False,
            message=(
                "PowerShell was not found, so the update cannot be staged. "
                "Stop the server and re-run the install command instead."
            ),
            log=log,
        )
    installer = _bundled_installer("install.ps1")
    if installer is None:
        return UpgradeResult(
            ok=False,
            message=(
                "The installer that ships with this version was not found, so "
                "the update cannot run. Re-run the install command instead."
            ),
            log=log,
        )
    stage_dir = _prepared_stage_dir()
    result_path = stage_dir / _PENDING_RESULT_FILENAME
    # One episode, one transcript, named before the helper starts so the
    # response that triggers the update can hand the path to a browser tab
    # while there is still a server to hand it with.
    install_log = stage_dir / (
        f"{INSTALL_LOG_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}{INSTALL_LOG_SUFFIX}"
    )
    progress_path = str(stage_dir / UPDATE_PROGRESS_FILENAME)
    no_start = os.environ.get("MCC_INSTALL_NO_START") == "1"
    with suppress(OSError):
        result_path.unlink()
    script_path = stage_dir / "apply-upgrade.ps1"
    try:
        script_path.write_text(
            _deferred_helper_script(
                result_path=result_path,
                stage_dir=stage_dir,
                install_log=install_log,
                installer=installer,
                powershell=powershell,
                config_dir=config_dir_path(),
                working_directory=Path.cwd(),
                # Named in the receipt so a window can say WHICH version is
                # being installed while it waits, and passed to the installer
                # as `-Version` so the update that was offered is the update
                # that happens even if a newer release lands mid-flight.
                version=tag or None,
                # Decision Q2: this says a desktop window is watching. It does
                # NOT say anybody else will start the server -- the installer
                # always restarts.
                no_restart=no_restart,
                no_start=no_start,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        return UpgradeResult(
            ok=False, message=f"Could not stage the update: {exc!s}", log=log
        )

    # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP.
    #
    # Not DETACHED_PROCESS: it denies the child a console entirely and
    # powershell.exe then exits immediately without running the script, so the
    # update silently never happened. CREATE_NO_WINDOW keeps a console the
    # child can use while hiding it, and the new process group means the helper
    # is not signalled along with the console this server was started from.
    creation_flags = 0x08000000 | 0x00000200
    try:
        subprocess.Popen(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            close_fds=True,
            env=_powershell_child_environment(),
        )
    except (OSError, ValueError) as exc:
        return UpgradeResult(
            ok=False, message=f"Could not start the update helper: {exc!s}", log=log
        )

    log.append("staged for install and restart by the official installer (Windows)")
    _CACHE.restart_required = True
    _CACHE.staged_install = True
    set_external_upgrade_helper_pending(True)
    return UpgradeResult(
        ok=True,
        message=(
            f"{tag or 'The latest release'} will be installed by the install "
            "command itself: the server closes, the new version is built beside "
            "it, checked, swapped in, and started again."
        ),
        installed_version=tag or None,
        log=log,
        log_path=str(install_log),
        progress_path=progress_path,
    )


def _spawn_posix_upgrade(
    *, tag: str, log: list[str], no_restart: bool = False
) -> UpgradeResult:
    """Run ``install.sh --restart`` detached, and return without waiting.

    6.82.0. Until now the POSIX update ran ``uv tool install --force`` **in
    this process**, synchronously, against the environment this process is
    running out of -- and then restarted nothing at all, on any platform, ever.
    The same installer that a Linux or macOS user would type now does the whole
    job, with the same staging, the same execute-verify, the same swap and the
    same health gate as Windows (decision Q7).

    ``setsid``/``nohup`` so it outlives this server, and ``/dev/null`` on all
    three streams because a child that keeps this process's stdout open holds
    its caller open too.
    """

    installer = _bundled_installer("install.sh")
    if installer is None:
        return UpgradeResult(
            ok=False,
            message=(
                "The installer that ships with this version was not found, so "
                "the update cannot run. Re-run the install command instead."
            ),
            log=log,
        )
    shell = shutil.which("sh")
    if shell is None:
        return UpgradeResult(
            ok=False,
            message="No POSIX shell was found; re-run the install command instead.",
            log=log,
        )
    stage_dir = _prepared_stage_dir()
    install_log = stage_dir / (
        f"{INSTALL_LOG_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}{INSTALL_LOG_SUFFIX}"
    )
    progress_path = str(stage_dir / UPDATE_PROGRESS_FILENAME)
    arguments = [shell, str(installer)]
    if os.environ.get("MCC_INSTALL_NO_START") == "1":
        arguments.append("--no-start")
    else:
        arguments.append("--restart")
    if tag:
        arguments += ["--version", tag]
    environment = dict(os.environ)
    # One episode, one transcript; and the configuration directory explicitly,
    # because the restart means the server of the directory this update is for.
    environment["MCC_INSTALL_LOG"] = str(install_log)
    environment["MCC_CONFIG_DIR"] = str(config_dir_path())
    try:
        subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=environment,
            cwd=str(Path.cwd()),
        )
    except (OSError, ValueError) as exc:
        return UpgradeResult(
            ok=False, message=f"Could not start the installer: {exc!s}", log=log
        )
    log.append("handed to install.sh --restart (POSIX)")
    _CACHE.restart_required = True
    _CACHE.staged_install = True
    set_external_upgrade_helper_pending(True)
    return UpgradeResult(
        ok=True,
        message=(
            f"{tag or 'The latest release'} will be installed by the install "
            "command itself: the new version is built beside the running one, "
            "checked, swapped in, and the server is started again."
        ),
        installed_version=tag or None,
        log=log,
        log_path=str(install_log),
        progress_path=progress_path,
    )


def upgrade_to_latest(
    payload: dict[str, Any], *, no_restart: bool = False
) -> UpgradeResult:
    """Hand ``payload``'s release to the official installer.

    6.82.0: this function no longer installs anything. It used to download the
    wheel, verify its digest and then run ``uv tool install --force`` -- a
    second, independent implementation of what ``scripts/install.ps1`` and
    ``scripts/install.sh`` already do, reachable only from the dashboard.
    Two downloaders and two installers is how the two paths drifted until one
    of them could leave a machine with no server (2026-09-11 §1). There is now
    one downloader, one verifier and one installer, and it is the install
    command itself (decision Q1).

    Synchronous only in the sense that it writes a script and spawns it;
    callers still run it in a worker thread because ``config_dir_path()`` and
    ``shutil.which`` touch the filesystem.

    ``no_restart`` says a desktop window is watching. It no longer says anyone
    else owns the restart (decision Q2) -- the installer always restarts.
    """

    log: list[str] = []
    tag = str(payload.get("tag_name") or "").lstrip("vV")
    if _select_wheel_asset(payload) is None:
        return UpgradeResult(ok=False, message="That release publishes no wheel.")

    if _WINDOWS and _wsl_windows_mount_tool_dir(shutil.which("uv")):
        return UpgradeResult(
            ok=False,
            message=(
                "The uv tool directory is under /mnt, where Windows file locks can "
                "corrupt an in-place WSL update. Move UV_TOOL_DIR to the WSL "
                "filesystem or re-run the install command after stopping the server."
            ),
        )

    if _WINDOWS:
        return _spawn_deferred_upgrade(tag=tag, log=log, no_restart=no_restart)
    return _spawn_posix_upgrade(tag=tag, log=log, no_restart=no_restart)


async def perform_upgrade(*, no_restart: bool = False) -> UpgradeResult:
    """Fetch the latest release and hand it to the installer off the event loop.

    ``no_restart`` is passed by the dashboard when it is being shown inside the
    desktop app. From 6.82.0 it means only "a window is watching, so do not
    open a browser": the installer restarts the server on every path, and the
    window attaches when the listener answers (decision Q2). Two owners of the
    restart is how one update came to start two servers, and how another
    started none.
    """

    payload, _checked_at, error = await _CACHE.get(force=True)
    if payload is None:
        return UpgradeResult(ok=False, message=error or "No release information.")
    if not is_newer(str(payload.get("tag_name") or ""), current_version()):
        return UpgradeResult(ok=False, message="Already on the latest release.")
    return await asyncio.to_thread(upgrade_to_latest, payload, no_restart=no_restart)


def reset_cache_for_tests() -> None:
    """Clear cached release state so tests start from a known point."""
    global _CACHE
    _CACHE = _ReleaseCache()
    reset_process_handoff_for_tests()


_RELEASE_NOTES_MAX_CHARS = 4000


def _release_notes(body: object) -> str | None:
    """Trim the release body for the dashboard banner.

    Bounded because the feed is remote: the banner shows an excerpt and links
    out for the rest rather than rendering an unbounded blob.
    """

    if not isinstance(body, str):
        return None
    text = body.strip()
    if not text:
        return None
    if len(text) <= _RELEASE_NOTES_MAX_CHARS:
        return text
    return text[:_RELEASE_NOTES_MAX_CHARS].rstrip() + "\n\n…"
