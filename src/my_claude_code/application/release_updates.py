"""Report the running version and upgrade to the latest published release.

Fetch the wheel published for the latest tag, verify its SHA-256, then install
it. A successful dashboard upgrade closes the current runtime and starts the
updated server. Windows cannot replace an environment underneath a running
process -- its interpreter and loaded DLLs are held open -- so there the
verified wheel is staged and a detached PowerShell helper takes over after this
process exits.

**How the install itself works, since 6.72.0.** It used to be one call:
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
import tempfile
import time
import tomllib
from contextlib import ExitStack, suppress
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
    UPDATE_HEALTH_GATE_SECONDS,
    UPDATE_HEALTH_POLL_SECONDS,
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
        candidate = bin_dir / ("fcc-server.exe" if os.name == "nt" else "fcc-server")
        if candidate.is_file():
            return candidate
    found = shutil.which("fcc-server")
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
    return config_dir_path() / _STAGE_DIRNAME


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


def _deferred_helper_script(
    *,
    uv_executable: str,
    command: list[str],
    result_path: Path,
    stage_dir: Path,
    server_launcher: Path,
    working_directory: Path,
    bin_dir: Path | None = None,
    tool_dir: Path | None = None,
    commands: list[str] | None = None,
    wait_seconds: float | None = None,
    version: str | None = None,
    no_restart: bool = False,
    install_log: Path | None = None,
    staging_root: Path | None = None,
    previous_root: Path | None = None,
    health_url: str | None = None,
) -> str:
    """PowerShell that waits for this process to exit, then installs.

    Written as PowerShell rather than Python because the only interpreter we
    can rely on is the one inside the environment being replaced -- using it
    would hold the very directory uv needs to delete.

    ``no_restart`` is decision GAP-3, and it is the whole of "the helper stops
    restarting". The helper's restart is a single un-retried ``Start-Process``
    whose failure is recorded and then acted on by nothing, and it starts a
    server no supervisor owns. When a desktop window asked for this update, the
    window is already sitting there with a ten-second tick, a health probe and
    a spawn -- so the helper installs and exits, and the window's very next tick
    starts the server. When nothing is watching (the dashboard in a browser tab,
    a headless machine) the helper still restarts, because otherwise the update
    would leave the machine with no server at all. A flag, not a second owner.

    Receipts go through ``[System.IO.File]::WriteAllText`` with a BOM-less
    ``UTF8Encoding($false)``: Windows PowerShell 5.1's ``Set-Content
    -Encoding utf8`` prepends a UTF-8 BOM and the Python reader parses JSON,
    which refuses a leading U+FEFF.

    ``install_log`` is 6.71.0's, and it is the difference between a window that
    says an install is happening and a window that shows it happening. ``uv``'s
    two streams used to be collected into a PowerShell ``$output`` variable and
    written out at the very end, into a file nothing reads until the episode is
    over -- so for the whole of the two minutes that matter there was literally
    nothing on disk to look at. They are now appended a line at a time, as they
    arrive, with an ``AppendAllText`` per line so the bytes are on disk (and
    readable by another process) the instant they exist rather than whenever a
    stream buffer happens to flush. Every progress record names the file.

    ``staging_root``, ``previous_root`` and ``health_url`` are 6.72.0's, and
    together they are the atomic update. The new environment is built under
    ``staging_root`` while the old one keeps serving, executed once to prove it
    works, exchanged with the live one by two directory renames, and the old
    one is kept under ``previous_root`` until ``health_url`` answers 200. Both
    roots are siblings of uv's tools root rather than children of it: a child
    whose name does not normalise to a valid package name makes ``uv tool
    list`` fail outright. When any of the three is missing -- this is not a uv
    tool environment, or the server could not name its own address -- the
    helper falls back to the in-place ``--force`` install it has always done.
    """

    quoted_args = ", ".join(_powershell_literal(arg) for arg in command[1:])
    progress_path = stage_dir / UPDATE_PROGRESS_FILENAME
    # The caller names the transcript, because it has to hand the path to the
    # dashboard in the very response that triggers the update -- before the
    # server it answered with goes away. A caller that does not care gets one
    # beside the receipt anyway: a helper with nowhere to tee is a helper that
    # goes quiet for two minutes, which is the whole bug.
    if install_log is None:
        install_log = stage_dir / (
            f"{INSTALL_LOG_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}{INSTALL_LOG_SUFFIX}"
        )
    wait_budget = (
        _helper_wait_seconds() if wait_seconds is None else float(wait_seconds)
    )
    names = commands if commands is not None else _published_commands()
    quoted_names = ", ".join(_powershell_literal(name) for name in names)
    bin_dir_literal = _powershell_literal(str(bin_dir) if bin_dir else "")
    tool_dir_literal = _powershell_literal(str(tool_dir) if tool_dir else "")
    version_literal = _powershell_literal(version or "")
    # Launchers the RUNNING desktop shell needs in order to keep asking what is
    # going on. Renaming these aside is what turned an update into a race: the
    # shell reads `NotInstalled` from its status ladder and, by design, starts
    # an install of its own into the same tool directory. They are not shims
    # this update has to move -- uv overwrites them in place, and if one is
    # momentarily locked the staged fallback keeps it, exactly like any other.
    quoted_never_rename = ", ".join(
        _powershell_literal(name)
        for name in ("mcc-desktop.exe", "fcc-desktop.exe", "MyClaudeCode.exe")
    )
    # The staging pass runs the SAME uv command against an empty tools root of
    # its own, minus ``--force``. ``--force`` exists to overwrite a live
    # environment, which is exactly what the staged path is built never to do;
    # leaving it in would be inert but would say the opposite of what this
    # release means (decision Q1: ``--force`` is reserved for repair, and the
    # repair is the in-place fallback further down).
    staging_args = ", ".join(
        _powershell_literal(arg) for arg in command[1:] if arg != "--force"
    )
    staging_root_literal = _powershell_literal(
        str(staging_root) if staging_root else ""
    )
    previous_root_literal = _powershell_literal(
        str(previous_root) if previous_root else ""
    )
    health_url_literal = _powershell_literal(health_url or "")
    # The launcher whose name the staged environment is executed under. Taken
    # from the launcher we already resolved rather than hard-coded, so the
    # legacy and native command families cannot drift apart here.
    server_command = server_launcher.stem
    native_command = "mcc-server"
    install_log_glob = f"{INSTALL_LOG_PREFIX}*{INSTALL_LOG_SUFFIX}"
    # Written out from the Python table rather than typed twice. It WAS typed
    # twice until 6.72.0, and the two copies disagreed the moment a stage was
    # added -- the PowerShell one still ranked `installing` third while Python
    # had moved it to fourth, which a monotonic guard turns into silently
    # dropped records rather than a visible error.
    stage_order_literal = "\n".join(
        f"    {_powershell_literal(stage)} = {rank}"
        for stage, rank in UPDATE_PROGRESS_STAGE_ORDER.items()
    )
    health_gate_seconds = UPDATE_HEALTH_GATE_SECONDS
    health_poll_ms = int(UPDATE_HEALTH_POLL_SECONDS * 1000)
    previous_kept = PREVIOUS_ENVS_KEPT
    transcripts_kept = INSTALL_TRANSCRIPTS_KEPT
    return f"""$ErrorActionPreference = 'Stop'
$parent = {os.getpid()}
# One JSON object per line, appended as this script moves between stages. It is
# the only trace an update leaves while it is happening: the parent's log stops
# at the stop line, uv writes to a pipe nobody is reading, and the whole window
# between "Update" and the new server answering was, measured on a real
# machine, fourteen minutes of a desktop app showing one unchanging sentence.
# The desktop window reads this file and says which stage it is in.
$progressPath = {_powershell_literal(str(progress_path))}
$progressEncoding = New-Object System.Text.UTF8Encoding($false)
# uv's own two streams, teed here a line at a time WHILE the install happens.
# Until 6.71.0 they went into a PowerShell variable and were written out once,
# at the end, into a file nothing reads until the episode is over -- so during
# the only part of an update a user cares about there was nothing to look at.
# Every progress record below names this path so a reader never guesses it.
$installLog = {_powershell_literal(str(install_log))}
# One line of a native command's merged output, as text. `2>&1` turns every
# stderr line into an ErrorRecord, whose default string form is sometimes the
# exception's TYPE NAME rather than what was written --
# "System.Management.Automation.RemoteException" appeared in the middle of uv's
# own diagnostics on 2026-09-11. The message is the line uv actually printed.
function Convert-OutputLine($value) {{
    if ($null -eq $value) {{ return '' }}
    if ($value -is [System.Management.Automation.ErrorRecord]) {{ return [string] $value.Exception.Message }}
    return [string] $value
}}
function Write-InstallLog($text) {{
    try {{
        $stampNow = (Get-Date).ToUniversalTime().ToString('HH:mm:ss')
        $body = Convert-OutputLine $text
        # One append per line rather than a held stream: a reader in another
        # process must see the line the moment it exists, and this helper can
        # be killed at any point without truncating what it already said.
        [System.IO.File]::AppendAllText($installLog, ('[' + $stampNow + '] ' + $body + [Environment]::NewLine), $progressEncoding)
    }}
    catch {{
        # A transcript nobody can write must never be the reason an update fails.
    }}
}}
# GAP-3, read HERE rather than three hundred lines further down. Until 6.71.0
# the first read of $noRestart was above its own assignment: PowerShell (no
# StrictMode in this generated script) answers $null, $null is falsey, and the
# helper wrote stage 'starting' -- "Starting the updated server." -- under the
# very flag that tells it not to start one. The live 6.66.1 receipt shows it:
# a 'starting' record 22 ms before a 'done' record saying the desktop app
# starts it. The Start-Process itself was always correctly skipped, because
# that guard sat after the assignment.
$noRestart = {"$true" if no_restart else "$false"}
# Liveness, not just narration. Every record carries the helper's own process
# id, when it started, and whether it has finished, so a reader can answer the
# one question that stops an update racing itself: IS AN INSTALLER RUNNING
# RIGHT NOW? A stage name alone cannot answer it -- a helper killed mid-install
# leaves 'installing' behind forever -- and neither can a heartbeat, because
# this is single-threaded PowerShell blocked inside uv for minutes at a time.
# The pid is the fact; `helper_done` is the fast path for the ordinary ending.
$helperPid = $PID
$helperStarted = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$targetVersion = {version_literal}
$script:HelperDone = $false
# Stages are MONOTONIC (decision Q2): an episode only ever moves forward, so a
# window can draw them as a timeline and a reader can tell "still installing"
# from "installed, starting" without guessing. The ranks are
# config/update_progress.py's UPDATE_PROGRESS_STAGE_ORDER; the terminal stages
# share the last rank because an episode ends once and 'failed' may be followed
# by 'recovered'. A stage this table does not know is written rather than
# dropped -- a guard that silently swallows records is worse than no guard.
$stageOrder = @{{
{stage_order_literal}
}}
$script:StageRank = 0
function Write-Stage($stage, $message) {{
    try {{
        $rank = $stageOrder[$stage]
        if ($null -eq $rank) {{ $rank = $script:StageRank }}
        if ($rank -lt $script:StageRank) {{ return }}
        $script:StageRank = $rank
        $elapsed = [math]::Round(([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() - ($helperStarted * 1000)) / 1000.0, 3)
        if ($elapsed -lt 0) {{ $elapsed = 0 }}
        $record = [ordered]@{{
            stage = $stage
            message = $message
            at = (Get-Date).ToUniversalTime().ToString('o')
            parent = $parent
            helper_pid = $helperPid
            started_at = $helperStarted
            elapsed_seconds = $elapsed
            helper_done = $script:HelperDone
            version = $targetVersion
            log = $installLog
        }}
        $line = ($record | ConvertTo-Json -Compress) + [Environment]::NewLine
        [System.IO.File]::AppendAllText($progressPath, $line, $progressEncoding)
    }}
    catch {{
        # A receipt nobody can write must never be the reason an update fails.
    }}
}}
# Every path this script needs, named once, up here, because 6.72.0's staged
# install needs them BEFORE the wait for the parent rather than after it.
# `$toolDir` is the live environment (`<tools root>/my-claude-code`); the
# staging and previous roots are SIBLINGS of the tools root, never children of
# it -- a child whose name does not normalise to a valid package name makes
# `uv tool list` fail outright and list nothing at all, which is worse than the
# malformed-tool warnings the old `.old-<stamp>` directories produce.
$binDir = {bin_dir_literal}
$toolDir = {tool_dir_literal}
$stagingRoot = {staging_root_literal}
$previousRoot = {previous_root_literal}
$healthUrl = {health_url_literal}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stagingDir = if ($stagingRoot) {{ Join-Path $stagingRoot $stamp }} else {{ '' }}
$stagingBin = if ($stagingDir) {{ Join-Path $stagingDir '.bin' }} else {{ '' }}
$previousDir = if ($previousRoot) {{ Join-Path $previousRoot $stamp }} else {{ '' }}
$commandNames = @({quoted_names})
# A fresh episode starts a fresh file: a stale 'done' from the previous update
# would otherwise be the first thing the window reads and believes.
try {{ [System.IO.File]::WriteAllText($progressPath, '', $progressEncoding) }} catch {{ }}
try {{ [System.IO.File]::WriteAllText($installLog, '', $progressEncoding) }} catch {{ }}
Write-InstallLog ('My Claude Code update helper, pid ' + $helperPid + ', target ' + $(if ($targetVersion) {{ $targetVersion }} else {{ 'the latest release' }}) + '.')
Write-InstallLog ('Restart is owned by ' + $(if ($noRestart) {{ 'the desktop app' }} else {{ 'this helper' }}) + '.')
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
    # alone, which is the old behaviour rather than a new failure mode. Never
    # treat "unknown start time" as "parent gone", or we would install while
    # the server is still running -- the exact corruption this avoids.
    if ($parentStart -eq 0) {{ return $true }}
    try {{ return $proc.StartTime.ToFileTimeUtc() -eq $parentStart }}
    catch {{ return $false }}   # access denied reading StartTime => not ours
}}
# ===========================================================================
# STAGE. 6.72.0, and the whole point of this release.
#
# Until now the first thing an update did was hand the LIVE environment to
# `uv tool install --force`, and uv empties a tool environment IN PLACE before
# it resolves a single new byte. Measured on this machine: `mcc-server`
# answered exit 0 at t=0, `ModuleNotFoundError: annotated_types` at +7.17 s,
# `ModuleNotFoundError: my_claude_code` -- the user's exact error -- at
# +7.99 s, and the executable itself was gone at +9.36 s. The whole install
# took 58 s and 102 s on two real updates. For all of it there was no server
# and no way back: the old bits were already deleted.
#
# So build the new environment BESIDE the old one, in a tools root of its own.
# uv never looks at the live directory, `mcc-server` keeps answering the entire
# time, and a wheel that cannot be installed costs nothing at all. This runs
# BEFORE the wait below, so it overlaps the server's own drain instead of
# following it.
$stagingEnv = ''
$stagedOk = $false
# Set only once a swap has actually happened, and read by the in-place fallback
# below: a release that adds a new command swaps first and then asks uv to write
# the launchers, and if THAT fails the canonical path holds a half-written
# environment while a perfectly good one sits aside with nothing to restore it.
$swappedAside = ''
if ($stagingDir -and $toolDir -and $binDir) {{
    Write-Stage 'staging' 'Building the new version beside the running one.'
    Write-InstallLog ('Staging into ' + $stagingDir + '. The running version is not touched.')
    try {{
        New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null
        New-Item -ItemType Directory -Path $stagingBin -Force | Out-Null
        $hadToolDir = Test-Path Env:\\UV_TOOL_DIR
        $previousToolDir = if ($hadToolDir) {{ $env:UV_TOOL_DIR }} else {{ '' }}
        $hadBinDir = Test-Path Env:\\UV_TOOL_BIN_DIR
        $previousBinDir = if ($hadBinDir) {{ $env:UV_TOOL_BIN_DIR }} else {{ '' }}
        $env:UV_TOOL_DIR = $stagingDir
        $env:UV_TOOL_BIN_DIR = $stagingBin
        # uv writes its progress to stderr, and under 'Stop' a native command's
        # stderr is a TERMINATING error. Judge it by its exit code alone.
        $ErrorActionPreference = 'Continue'
        $env:NO_COLOR = '1'
        try {{ [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) }} catch {{ }}
        $stageOutput = & {_powershell_literal(uv_executable)} {staging_args} 2>&1 |
            ForEach-Object {{ $line = Convert-OutputLine $_; Write-InstallLog $line; $line }} |
            Out-String
        $stageCode = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($hadToolDir) {{ $env:UV_TOOL_DIR = $previousToolDir }} else {{ Remove-Item Env:\\UV_TOOL_DIR -ErrorAction SilentlyContinue }}
        if ($hadBinDir) {{ $env:UV_TOOL_BIN_DIR = $previousBinDir }} else {{ Remove-Item Env:\\UV_TOOL_BIN_DIR -ErrorAction SilentlyContinue }}
        Write-InstallLog ('The staged install exited with ' + $stageCode + '.')
        $stagingEnv = Join-Path $stagingDir {_powershell_literal(PACKAGE_NAME)}
        if (($stageCode -eq 0) -and (Test-Path -LiteralPath $stagingEnv -PathType Container)) {{
            $stagedOk = $true
        }} else {{
            Write-InstallLog 'Nothing was staged; the running version is untouched.'
        }}
    }}
    catch {{
        $ErrorActionPreference = 'Stop'
        $stagedOk = $false
        Write-InstallLog ('The staged install could not run: ' + $_.Exception.Message)
    }}
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
# still pinned by its creation time, then install.
if (-not (Test-ParentAlive)) {{
    Write-Stage 'stopping' 'The server has stopped. Preparing to install.'
    Write-InstallLog 'The running server exited; the environment is free.'
}}
if (Test-ParentAlive) {{
    Write-Stage 'stopping' 'The server did not stop in time, so it is being ended.'
    Write-InstallLog ('The server (pid ' + $parent + ') outlived its stop budget; ending it.')
    Stop-Process -Id $parent -Force -ErrorAction SilentlyContinue
    $killDeadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $killDeadline) {{
        if (-not (Test-ParentAlive)) {{ break }}
        Start-Sleep -Milliseconds 250
    }}
}}
if (Test-ParentAlive) {{
    $script:HelperDone = $true
    Write-InstallLog 'The server could not be stopped. Nothing was installed.'
    Write-Stage 'failed' 'The server could not be stopped, so the update was not applied.'
    $result = @{{ ok = $false; message = 'The server could not be stopped, so the update was not applied.' }}
    [System.IO.File]::WriteAllText({_powershell_literal(str(result_path))}, ($result | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
    exit 1
}}
# Ask the new server whether it is actually up. This is the gate the cutover
# turns on: an install that produced a server which never answers is not a
# successful update, it is an outage with a new version number.
function Wait-ForHealth {{
    param($url, $seconds)
    if (-not $url) {{ return $true }}
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {{
        try {{
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5 -Method Get
            if ($response.StatusCode -eq 200) {{ return $true }}
        }}
        catch {{
            # 503 + x-mcc-starting is a server that has bound the socket and is
            # still coming up (6.59.0). Not an answer yet, not a failure yet.
        }}
        Start-Sleep -Milliseconds {health_poll_ms}
    }}
    return $false
}}
# Keep exactly one previous environment: it is the rollback, and a second one is
# only disk. Swept after /health answers, never before, so the copy being
# deleted is never the one a rollback would need.
function Remove-StalePrevious {{
    param($root, $keep)
    if (-not $root) {{ return }}
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {{ return }}
    $all = @(Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)
    if ($all.Count -le $keep) {{ return }}
    foreach ($old in $all[$keep..($all.Count - 1)]) {{
        try {{
            Remove-Item -LiteralPath $old.FullName -Recurse -Force -ErrorAction Stop
            Write-InstallLog ('Removed the superseded previous environment ' + $old.Name + '.')
        }}
        catch {{
            Write-InstallLog ('Could not remove ' + $old.FullName + ': ' + $_.Exception.Message)
        }}
    }}
}}
# One transcript per update is one file per update, for ever. Keep the recent
# ones -- they are the only record of what an update actually did -- and let
# the rest go.
function Remove-StaleTranscripts {{
    $dir = Split-Path -Parent $installLog
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) {{ return }}
    $all = @(Get-ChildItem -Path $dir -Filter '{install_log_glob}' -File -ErrorAction SilentlyContinue | Sort-Object Name -Descending)
    if ($all.Count -le {transcripts_kept}) {{ return }}
    foreach ($old in $all[{transcripts_kept}..($all.Count - 1)]) {{
        Remove-Item -LiteralPath $old.FullName -Force -ErrorAction SilentlyContinue
    }}
}}
if ($stagedOk) {{
    # =======================================================================
    # VERIFY. Run the staged environment once, before it is anywhere near the
    # live path. Caddy's rule: the gate is EXECUTING the new thing, not
    # trusting an installer's exit code. A wheel that resolves, installs and
    # then cannot import itself is a real failure mode, and it used to be
    # discovered by the user.
    # =======================================================================
    Write-Stage 'verifying' 'Running the new version once before it replaces the old one.'
    # The native command first, the legacy launcher only as a fallback: both
    # are published, but `fcc-server --version` prints the LEGACY distribution
    # name ("free-claude-code 6.71.1"), and a version check that reads the
    # wrong product name is a check waiting to be misread.
    $stagedServer = Join-Path $stagingEnv 'Scripts\\{native_command}.exe'
    if (-not (Test-Path -LiteralPath $stagedServer -PathType Leaf)) {{
        $stagedServer = Join-Path $stagingEnv 'Scripts\\{server_command}.exe'
    }}
    $stagedPython = Join-Path $stagingEnv 'Scripts\\python.exe'
    $verified = $false
    $verifyNote = 'The staged version could not be run.'
    $ErrorActionPreference = 'Continue'
    try {{
        if ((Test-Path -LiteralPath $stagedServer -PathType Leaf) -and (Test-Path -LiteralPath $stagedPython -PathType Leaf)) {{
            $versionOut = (& $stagedServer --version 2>&1 | Out-String).Trim()
            $versionCode = $LASTEXITCODE
            Write-InstallLog ('Staged --version said "' + $versionOut + '" (exit ' + $versionCode + ').')
            $importOut = (& $stagedPython -c 'import my_claude_code' 2>&1 | Out-String).Trim()
            $importCode = $LASTEXITCODE
            Write-InstallLog ('Staged import exited with ' + $importCode + '.')
            if ($importOut) {{ Write-InstallLog $importOut }}
            $versionMatches = $true
            if ($targetVersion -and ($versionOut -notmatch [regex]::Escape($targetVersion))) {{
                $versionMatches = $false
                $verifyNote = 'The staged version reported "' + $versionOut + '" rather than ' + $targetVersion + '.'
            }}
            if (($versionCode -eq 0) -and ($importCode -eq 0) -and $versionMatches) {{
                $verified = $true
            }} elseif ($versionMatches) {{
                $verifyNote = 'The staged version did not run: --version exited ' + $versionCode + ', import exited ' + $importCode + '.'
            }}
        }} else {{
            $verifyNote = 'The staged install produced no runnable launcher.'
        }}
    }}
    catch {{
        $verified = $false
        $verifyNote = 'The staged version could not be run: ' + $_.Exception.Message
    }}
    $ErrorActionPreference = 'Stop'

    if (-not $verified) {{
        # Nothing has moved. The live environment is exactly as it was, so the
        # recovery is "start what is already installed" and the whole episode
        # cost the user a download.
        Write-InstallLog $verifyNote
        Remove-Item -LiteralPath $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
        $message = $verifyNote + ' Nothing was replaced; the installed version is unchanged.'
        Write-Stage 'failed' $message
        $result = @{{ ok = $false; exit_code = 1; attempts = 1; staged = $true; verified = $false; swapped = $false; message = $message; restarted = $false }}
        $restarted = $false
        if (-not $noRestart) {{
            try {{ Start-Process -FilePath {_powershell_literal(str(server_launcher))} -WorkingDirectory {_powershell_literal(str(working_directory))}; $restarted = $true }}
            catch {{ Write-InstallLog ('The installed server could not be started: ' + $_.Exception.Message) }}
        }}
        $result['restarted'] = $restarted
        $result['message'] = $message + $(if ($restarted) {{ ' The installed version was restarted.' }} elseif ($noRestart) {{ ' The desktop app starts it again within ten seconds.' }} else {{ ' The installed version could not be restarted either.' }})
        [System.IO.File]::WriteAllText({_powershell_literal(str(result_path))}, ($result | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
        $script:HelperDone = $true
        Write-Stage 'recovered' $result.message
        exit 1
    }}

    # =======================================================================
    # SWAP. Two directory renames on one volume. Measured on this machine over
    # ten rounds: 2.7 ms fastest, 3.9 ms median, 20.0 ms slowest -- against the
    # 7-to-14.5 second hole `uv tool install --force` used to open.
    #
    # The launcher shims in the bin directory are NOT touched, and that is the
    # whole trick. Every one of them is a uv trampoline whose embedded path is
    # `<tools root>/my-claude-code/Scripts/python.exe` (verified at the byte
    # level: the bin shim and the environment's own Scripts copy are the same
    # file, sha256 for sha256). They do not care WHICH environment is at that
    # path -- so the instant the new one lands there, every already-installed
    # launcher runs the new code, and no locked `.exe` can abort anything,
    # because nothing is being written over.
    #
    # The environment's own `Scripts/*.exe` are a different matter: uv baked
    # the STAGING path into them, so after the move they are dead. They are
    # replaced with the bin copies, which carry the canonical path.
    # =======================================================================
    Write-Stage 'swapping' 'Putting the new version in place.'
    $asideEnv = Join-Path $previousDir {_powershell_literal(PACKAGE_NAME)}
    $swapped = $false
    try {{
        New-Item -ItemType Directory -Path $previousDir -Force | Out-Null
        $swapWatch = [Diagnostics.Stopwatch]::StartNew()
        [System.IO.Directory]::Move($toolDir, $asideEnv)
        [System.IO.Directory]::Move($stagingEnv, $toolDir)
        $swapWatch.Stop()
        $swapped = $true
        $swappedAside = $asideEnv
        Write-InstallLog ('Swapped in ' + [math]::Round($swapWatch.Elapsed.TotalMilliseconds, 1) + ' ms. The previous version is at ' + $asideEnv + '.')
    }}
    catch {{
        Write-InstallLog ('The swap failed: ' + $_.Exception.Message)
        # Put the live environment back if the first move succeeded and the
        # second did not. Anything else and nothing moved at all.
        if ((-not (Test-Path -LiteralPath $toolDir -PathType Container)) -and (Test-Path -LiteralPath $asideEnv -PathType Container)) {{
            try {{ [System.IO.Directory]::Move($asideEnv, $toolDir); Write-InstallLog 'The previous environment was put back.' }}
            catch {{ Write-InstallLog ('The previous environment could not be put back: ' + $_.Exception.Message) }}
        }}
    }}

    if ($swapped) {{
        # The environment's own trampolines, re-pointed at the canonical path.
        $repaired = 0
        if (Test-Path -LiteralPath $binDir -PathType Container) {{
            foreach ($file in @(Get-ChildItem -Path $binDir -Filter '*.exe' -ErrorAction SilentlyContinue)) {{
                $target = Join-Path $toolDir ('Scripts\\' + $file.Name)
                if (Test-Path -LiteralPath $target -PathType Leaf) {{
                    try {{ Copy-Item -LiteralPath $file.FullName -Destination $target -Force -ErrorAction Stop; $repaired = $repaired + 1 }}
                    catch {{ }}
                }}
            }}
        }}
        Write-InstallLog ('Re-pointed ' + $repaired + ' launcher(s) inside the new environment.')
        # uv recorded every entry point under the STAGING bin directory, which
        # is about to be deleted; a receipt left as written would send a later
        # uninstall or upgrade at a path that no longer exists. Same rewrite
        # 6.33.1 does for the staged-bin fallback, for the same reason.
        $receiptPath = Join-Path $toolDir 'uv-receipt.toml'
        if (Test-Path -LiteralPath $receiptPath -PathType Leaf) {{
            try {{
                $receiptText = [IO.File]::ReadAllText($receiptPath)
                $realPrefix = $binDir.Replace('\\', '/').TrimEnd('/')
                $backslashPrefix = $stagingBin.Replace('/', '\\').TrimEnd('\\')
                $rewritten = $receiptText
                foreach ($stagePrefix in @($stagingBin.Replace('\\', '/').TrimEnd('/'), $backslashPrefix.Replace('\\', '\\\\'), $backslashPrefix)) {{
                    $rewritten = $rewritten.Replace($stagePrefix, $realPrefix)
                }}
                if ($rewritten -ne $receiptText) {{
                    [System.IO.File]::WriteAllText(($receiptPath + '.new'), $rewritten, (New-Object System.Text.UTF8Encoding($false)))
                    Move-Item -LiteralPath ($receiptPath + '.new') -Destination $receiptPath -Force
                    Write-InstallLog 'Rewrote the receipt entry points to the real bin directory.'
                }}
            }}
            catch {{ Write-InstallLog ('The receipt could not be rewritten: ' + $_.Exception.Message) }}
        }}
        Remove-Item -LiteralPath $stagingDir -Recurse -Force -ErrorAction SilentlyContinue

        # A release that publishes a command this machine has never had cannot
        # be finished by a rename: there is no trampoline anywhere carrying the
        # canonical path for it, and one cannot be written by hand (the path is
        # baked into the binary twice, once as a PE resource and once as the
        # shebang of an appended zip). That case -- rare, and only on releases
        # that ADD an entry point -- falls through to the in-place install
        # below, which is now a repair rather than the ordinary path, and which
        # runs against a fully warm cache because the staging pass just filled
        # it. The previous environment is already aside, so it is still safe.
        $missingShims = @()
        foreach ($name in $commandNames) {{
            if (-not (Test-Path -LiteralPath (Join-Path $binDir ($name + '.exe')) -PathType Leaf)) {{ $missingShims += $name }}
        }}
        if ($missingShims.Count -gt 0) {{
            Write-InstallLog ('This release adds ' + ($missingShims -join ', ') + '; uv has to write the launcher(s), so the install is finished in place.')
        }} else {{
            $result = @{{
                ok = $true
                exit_code = 0
                attempts = 1
                staged = $true
                verified = $true
                swapped = $true
                previous_environment = $asideEnv
                disk_full = $false
                missing_commands = @()
                kept_shims = @()
                restored_shims = @()
                restarted = $false
                message = 'The new version was installed beside the old one and swapped in.'
                output = $stageOutput
            }}
            [System.IO.File]::WriteAllText({_powershell_literal(str(result_path))}, ($result | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
            $restarted = $false
            if ($noRestart) {{
                Write-Stage 'handing-off' 'Installed. Handing the restart to the desktop app.'
            }} else {{
                Write-Stage 'starting' 'Starting the updated server.'
                try {{
                    Start-Process -FilePath {_powershell_literal(str(server_launcher))} -WorkingDirectory {_powershell_literal(str(working_directory))}
                    $restarted = $true
                    Write-InstallLog 'Started the updated server.'
                }}
                catch {{ Write-InstallLog ('The updated server could not be started: ' + $_.Exception.Message) }}
            }}
            $result['restarted'] = $restarted

            # ===============================================================
            # HEALTH GATE. Nothing is deleted until the new server answers.
            # ===============================================================
            Write-InstallLog ('Waiting up to {health_gate_seconds:.0f} s for ' + $healthUrl + ' to answer.')
            $healthy = Wait-ForHealth $healthUrl {health_gate_seconds:.1f}
            if ($healthy) {{
                Write-InstallLog 'The updated server answered /health.'
                Remove-StalePrevious $previousRoot {previous_kept}
                Remove-StaleTranscripts
                $script:HelperDone = $true
                [System.IO.File]::WriteAllText({_powershell_literal(str(result_path))}, ($result | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
                Remove-Item -Path {_powershell_literal(str(stage_dir / "wheel"))} -Recurse -Force -ErrorAction SilentlyContinue
                if ($noRestart) {{
                    Write-Stage 'done' 'The new version is installed. The desktop app starts it.'
                }} else {{
                    Write-Stage 'done' 'The updated server was started.'
                }}
                exit 0
            }}

            # ===============================================================
            # ROLLBACK. The new version is installed and does not answer, so
            # put the one that did back and start it. This is the reason the
            # old environment was renamed rather than deleted.
            # ===============================================================
            Write-Stage 'rolling-back' 'The new version did not answer, so the previous one is being put back.'
            Write-InstallLog ('No answer from ' + $healthUrl + ' within {health_gate_seconds:.0f} s. Rolling back.')
            $rolledBack = $false
            try {{
                $failedDir = Join-Path $stagingRoot ($stamp + '-failed')
                New-Item -ItemType Directory -Path $failedDir -Force | Out-Null
                [System.IO.Directory]::Move($toolDir, (Join-Path $failedDir {_powershell_literal(PACKAGE_NAME)}))
                [System.IO.Directory]::Move($asideEnv, $toolDir)
                $rolledBack = $true
                Write-InstallLog 'The previous environment is back at the canonical path.'
                # The stamp directory it came out of is now empty, and an empty
                # one would be kept as "the rollback" by the next sweep while
                # holding nothing to roll back to.
                Remove-Item -LiteralPath $previousDir -Recurse -Force -ErrorAction SilentlyContinue
                # Its own Scripts trampolines carry the canonical path already
                # -- they were never rewritten -- so nothing else is needed.
            }}
            catch {{
                Write-InstallLog ('The rollback failed: ' + $_.Exception.Message)
            }}
            $restartedPrevious = $false
            if ($rolledBack -and (-not $noRestart)) {{
                try {{ Start-Process -FilePath {_powershell_literal(str(server_launcher))} -WorkingDirectory {_powershell_literal(str(working_directory))}; $restartedPrevious = $true }}
                catch {{ Write-InstallLog ('The previous version could not be started: ' + $_.Exception.Message) }}
            }}
            $result['ok'] = $false
            $result['rolled_back'] = $rolledBack
            $result['restarted'] = $restartedPrevious
            $result['message'] = $(if ($rolledBack) {{ 'The new version was installed but never answered, so the previous version was put back' + $(if ($restartedPrevious) {{ ' and restarted.' }} elseif ($noRestart) {{ '. The desktop app starts it within ten seconds.' }} else {{ ', but it could not be restarted.' }}) }} else {{ 'The new version never answered and the previous version could not be put back. Re-run the install command.' }})
            [System.IO.File]::WriteAllText({_powershell_literal(str(result_path))}, ($result | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
            $script:HelperDone = $true
            Write-Stage 'recovered' $result.message
            exit 1
        }}
    }}
}}
Write-Stage 'installing' 'Installing the new version.'
# Give Windows a moment to release the handles the exiting process held.
Start-Sleep -Seconds 2
# uv writes progress to stderr. Under ErrorActionPreference='Stop' a native
# command's stderr becomes a *terminating* NativeCommandError, which would kill
# this script before it installs anything, so drop back to Continue for the
# call itself and judge the result by exit code alone.
$ErrorActionPreference = 'Continue'
# uv draws its diagnostics with box-drawing characters and colours them when it
# thinks it is talking to a terminal. PowerShell decodes a native command's
# output with the CONSOLE code page, which on this machine is cp437 -- so every
# box character reached the transcript as three mojibake bytes and the window
# showed the user 'GoeGoeCGoe' where uv had drawn a tree. Ask uv for plain text
# and read its bytes as the UTF-8 they are. Both are best-effort: a helper with
# no console attached must not fail over the encoding of a log line.
$env:NO_COLOR = '1'
try {{ [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) }} catch {{ }}
# Handle release is not instantaneous on Windows -- an antivirus scan, the
# search indexer, or a slow shutdown can still hold the environment briefly.
# A single attempt that loses that race leaves uv having deleted part of the
# environment, which is precisely the broken install this whole path exists to
# avoid, so retry with backoff: a later attempt succeeds because the earlier
# one already removed whatever it could.
# uv writes the launcher shims into the uv tool bin directory in ASCII order of
# the file name including its ".exe" suffix, and ABORTS THE WHOLE INSTALL on the
# first one it cannot overwrite. A launcher window the user still has open (an
# `mcc-claude` session, say) holds its own .exe without FILE_SHARE_DELETE, so
# every entrypoint alphabetically after it is never written and uv leaves no
# receipt -- `uv tool list` then calls the tool malformed. Waiting for the
# server does not help: those are different processes.
#
# Windows refuses to DELETE a running image but happily RENAMES one, and the
# process keeps executing from the renamed file. So move every shim aside first
# and let uv write a complete fresh set at the canonical paths.
#
# The shim set is a UNION of the mcc-*/fcc-* family pattern, uv's own receipt,
# and the distribution's entry points, so a command added by a release can never
# be missed by all three -- and a rename that is REFUSED is recorded rather than
# swallowed. A swallowed refusal is what let uv walk into a locked
# `mcc-desktop.exe` and abort an entire install.
#
# One family of shims is EXEMPT from the rename. The desktop shell asks
# `mcc-desktop --print-status` on every pass of its ladder; with that shim
# renamed aside the shell reads NotInstalled and starts its own `uv tool
# install` into this very tool directory. Measured 2026-09-07: the helper's
# five attempts all failed against the shell's concurrent installer, the shell
# won at 23:25:43, and the helper's "start the server again" step never ran
# because it had already written a failure. uv overwrites these in place, and a
# momentarily locked one is kept by the staged fallback exactly like any other,
# so there was never anything the rename bought here.
$neverRename = @({quoted_never_rename})
$managed = @{{}}
$refused = @()
# Every shim actually moved out of the way, so a failed install can put them
# back. Without this list a failure is not recoverable: the old launchers are
# sitting under '.old-' names, the new ones were never written, and the machine
# has no `mcc-server` at all -- measured on a scratch install on 2026-09-08,
# where the helper's own restart step then reported "could not be restarted
# either". The install script has always restored on its failure path
# (`Restore-LauncherShim`); the helper never did.
$movedAside = @()
if ($binDir -and (Test-Path -LiteralPath $binDir -PathType Container)) {{
    foreach ($file in @(Get-ChildItem -Path $binDir -Filter '*.exe' -ErrorAction SilentlyContinue)) {{
        if (($file.Name -match '^(mcc|fcc)-.+\\.exe$') -or (@('my-claude-code.exe', 'free-claude-code.exe') -contains $file.Name.ToLowerInvariant())) {{
            $managed[$file.Name] = $true
        }}
    }}
    if ($toolDir) {{
        $receiptPath = Join-Path $toolDir 'uv-receipt.toml'
        if (Test-Path -LiteralPath $receiptPath -PathType Leaf) {{
            foreach ($hit in [regex]::Matches([IO.File]::ReadAllText($receiptPath), 'install-path\\s*=\\s*"([^"]+)"')) {{
                $leaf = Split-Path -Leaf $hit.Groups[1].Value
                if ($leaf -like '*.exe') {{ $managed[$leaf] = $true }}
            }}
        }}
    }}
    foreach ($name in $commandNames) {{ $managed[($name + '.exe')] = $true }}
    foreach ($fileName in ($managed.Keys | Sort-Object)) {{
        $shim = Join-Path $binDir $fileName
        if (-not (Test-Path -LiteralPath $shim -PathType Leaf)) {{ continue }}
        if ($neverRename -contains $fileName) {{ continue }}
        try {{
            Rename-Item -LiteralPath $shim -NewName ($fileName + '.old-' + $stamp) -ErrorAction Stop
            $movedAside += $fileName
        }}
        catch {{
            $refused += $fileName
        }}
    }}
}}
if ($refused.Count -gt 0) {{
    # Say WHO is holding them. Until 6.72.2 a locked launcher produced a retry
    # and a sentence about a file being "in use", and the user was left to
    # guess. On the machine this was written for the holders were two
    # mcc-server launches from the previous day that were STILL SERVING WORK.
    #
    # Report only. Nothing here stops anything: this cannot tell a finished
    # server from a busy one, the staged fallback below already survives the
    # lock, and stopping a server somebody is using to save one install
    # attempt is not a trade this program gets to make.
    #
    # Matched on the resolved executable path, never on an image name -- every
    # MCC command on Windows is called python.exe, and the bin directory also
    # holds programs (Claude Code's own claude.exe) that this install never
    # touches and must never accuse.
    Write-InstallLog 'These My Claude Code processes are running from the files being replaced:'
    $ourPrefixes = @('mcc-', 'fcc-', 'my-claude-code', 'free-claude-code')
    $toolPrefix = ''
    if ($toolDir) {{ $toolPrefix = $toolDir.TrimEnd('\', '/') + '\' }}
    $binPrefix = $binDir.TrimEnd('\', '/') + '\'
    $namedAny = $false
    foreach ($proc in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {{
        $exe = $proc.ExecutablePath
        if (-not $exe) {{ continue }}
        $inBin = $exe.StartsWith($binPrefix, [StringComparison]::OrdinalIgnoreCase)
        $inTool = $toolPrefix -and $exe.StartsWith($toolPrefix, [StringComparison]::OrdinalIgnoreCase)
        if (-not ($inBin -or $inTool)) {{ continue }}
        $leaf = [IO.Path]::GetFileNameWithoutExtension($exe).ToLowerInvariant()
        $ours = $inTool
        foreach ($prefix in $ourPrefixes) {{
            if ($leaf.StartsWith($prefix)) {{ $ours = $true; break }}
        }}
        if (-not $ours) {{ continue }}
        $started = ''
        if ($proc.CreationDate) {{ $started = $proc.CreationDate.ToString('yyyy-MM-dd HH:mm:ss') }}
        Write-InstallLog ('  pid ' + $proc.ProcessId + '  ' + $proc.Name + '  started ' + $started + '  ' + $exe)
        $namedAny = $true
    }}
    if (-not $namedAny) {{
        Write-InstallLog '  (none -- the lock is something else, such as an antivirus scan)'
    }}
    Write-InstallLog 'None of them will be stopped: one may be a server you are using right now. The new version is placed beside them instead.'
}}
$delays = @(0, 5, 10, 20, 30)
$code = 1
$attempts = 0
$output = ''
# The fast loop ALWAYS runs. It used to be skipped whenever a single rename was
# refused, and one `mcc-claude` window the user had left open for the afternoon
# is enough to refuse one -- which is the normal case on this machine, not an
# edge case. Skipping cost the whole install its cheap path (measured:
# attempts=5, meaning the fast loop never ran at all) for a lock the staged
# fallback below was already written to survive. A refusal does mean uv will
# probably trip over that one file, so it buys a single attempt rather than the
# full backoff: the point is not to pay 65 seconds of sleeps on the way to a
# path that handles the lock properly.
$fastDelays = if ($refused.Count -eq 0) {{ $delays }} else {{ @(0) }}
# The same table scripts/install.ps1 keeps, for the same reason: uv reports a
# full disk and a locked file identically -- a non-zero exit code and a
# sentence -- so the code alone cannot tell them apart. Every retry below
# writes more files, and the staged fallback writes a whole second copy of
# them, so on a volume with no room left the ladder turns one honest failure
# into ten and 130 seconds of sleeps.
$diskFullSignatures = @('os error 112', 'not enough space on the disk', 'no space left on device', 'enospc')
function Test-UvDiskFull($text) {{
    if (-not $text) {{ return $false }}
    $haystack = ([string] $text).ToLowerInvariant()
    foreach ($signature in $diskFullSignatures) {{
        if ($haystack.Contains($signature)) {{ return $true }}
    }}
    return $false
}}
$diskFull = $false
foreach ($wait in $fastDelays) {{
    if ($wait -gt 0) {{
        Write-InstallLog ('Waiting ' + $wait + ' s before the next attempt.')
        Start-Sleep -Seconds $wait
    }}
    Write-InstallLog ('uv tool install, attempt ' + ($attempts + 1) + '.')
    # The tee. `2>&1 |` streams uv's two channels through this pipeline one
    # object at a time, so each line reaches the transcript as uv prints it --
    # `| Out-String` at the end still yields the whole capture for the receipt,
    # but it is no longer the FIRST time the output exists anywhere.
    $output = & {_powershell_literal(uv_executable)} {quoted_args} 2>&1 |
        ForEach-Object {{ $line = Convert-OutputLine $_; Write-InstallLog $line; $line }} |
        Out-String
    $code = $LASTEXITCODE
    $attempts = $attempts + 1
    Write-InstallLog ('uv exited with ' + $code + '.')
    if ($code -eq 0) {{ break }}
    if (Test-UvDiskFull $output) {{ $diskFull = $true; break }}
}}
# Staged fallback: uv writes every shim and a complete receipt into a directory
# nothing can be holding, and the shims are placed one at a time afterwards, so
# one stuck file costs exactly that one file instead of the whole install.
$kept = @()
if (($code -ne 0) -and $binDir -and (-not $diskFull)) {{
    $stageBin = Join-Path ([IO.Path]::GetTempPath()) ('mcc-stage-bin-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stageBin | Out-Null
    $hadBin = Test-Path Env:\\UV_TOOL_BIN_DIR
    $previousBin = if ($hadBin) {{ $env:UV_TOOL_BIN_DIR }} else {{ '' }}
    $env:UV_TOOL_BIN_DIR = $stageBin
    Write-InstallLog 'Retrying into a staging bin directory: a launcher is still locked.'
    foreach ($wait in $delays) {{
        if ($wait -gt 0) {{
            Write-InstallLog ('Waiting ' + $wait + ' s before the next attempt.')
            Start-Sleep -Seconds $wait
        }}
        Write-InstallLog ('uv tool install (staged), attempt ' + ($attempts + 1) + '.')
        $output = & {_powershell_literal(uv_executable)} {quoted_args} 2>&1 |
            ForEach-Object {{ $line = Convert-OutputLine $_; Write-InstallLog $line; $line }} |
            Out-String
        $code = $LASTEXITCODE
        $attempts = $attempts + 1
        Write-InstallLog ('uv exited with ' + $code + '.')
        if ($code -eq 0) {{ break }}
    }}
    if ($hadBin) {{ $env:UV_TOOL_BIN_DIR = $previousBin }} else {{ Remove-Item Env:\\UV_TOOL_BIN_DIR -ErrorAction SilentlyContinue }}
    if ($code -eq 0) {{
        foreach ($staged in @(Get-ChildItem -Path $stageBin -Filter '*.exe' -ErrorAction SilentlyContinue)) {{
            $target = Join-Path $binDir $staged.Name
            $asideName = $staged.Name + '.old-' + $stamp + '-staged'
            $aside = Join-Path $binDir $asideName
            $movedAside = $false
            if (Test-Path -LiteralPath $target -PathType Leaf) {{
                foreach ($attempt in 1..4) {{
                    try {{ Rename-Item -LiteralPath $target -NewName $asideName -ErrorAction Stop; $movedAside = $true; break }}
                    catch {{ Start-Sleep -Milliseconds (150 * $attempt) }}
                }}
            }}
            try {{
                Copy-Item -LiteralPath $staged.FullName -Destination $target -Force -ErrorAction Stop
            }}
            catch {{
                # Keep the old launcher. It is a version-agnostic stub that execs
                # the interpreter under the canonical tool directory, and that
                # directory now holds the new install, so the command already
                # runs the new code.
                if ($movedAside -and (Test-Path -LiteralPath $aside -PathType Leaf)) {{
                    Move-Item -LiteralPath $aside -Destination $target -Force -ErrorAction SilentlyContinue
                }}
                $kept += [IO.Path]::GetFileNameWithoutExtension($staged.Name)
            }}
        }}
        # uv records install-path under whatever UV_TOOL_BIN_DIR was set to, so a
        # receipt left as written would send a later uninstall or upgrade at a
        # temp path that no longer exists.
        if ($toolDir) {{
            $receiptPath = Join-Path $toolDir 'uv-receipt.toml'
            if (Test-Path -LiteralPath $receiptPath -PathType Leaf) {{
                $receiptText = [IO.File]::ReadAllText($receiptPath)
                $realPrefix = $binDir.Replace('\\', '/').TrimEnd('/')
                $backslashPrefix = $stageBin.Replace('/', '\\').TrimEnd('\\')
                $rewritten = $receiptText
                foreach ($stagePrefix in @($stageBin.Replace('\\', '/').TrimEnd('/'), $backslashPrefix.Replace('\\', '\\\\'), $backslashPrefix)) {{
                    $rewritten = $rewritten.Replace($stagePrefix, $realPrefix)
                }}
                if ($rewritten -ne $receiptText) {{
                    [System.IO.File]::WriteAllText(($receiptPath + '.new'), $rewritten, (New-Object System.Text.UTF8Encoding($false)))
                    Move-Item -LiteralPath ($receiptPath + '.new') -Destination $receiptPath -Force
                }}
            }}
        }}
    }}
    Remove-Item -LiteralPath $stageBin -Recurse -Force -ErrorAction SilentlyContinue
}}
$ErrorActionPreference = 'Stop'
# Nothing was installed. Put back what was moved out of the way, or this
# machine has no launchers at all: the old ones are under '.old-' names and the
# new ones were never written. That is the difference between "the update
# failed" and "the update failed and took your server with it".
$restoredShims = @()
if (($code -ne 0) -and $binDir) {{
    foreach ($fileName in $movedAside) {{
        $target = Join-Path $binDir $fileName
        $aside = Join-Path $binDir ($fileName + '.old-' + $stamp)
        if ((Test-Path -LiteralPath $aside -PathType Leaf) -and (-not (Test-Path -LiteralPath $target -PathType Leaf))) {{
            try {{
                Rename-Item -LiteralPath $aside -NewName $fileName -ErrorAction Stop
                $restoredShims += $fileName
            }}
            catch {{
                # Nothing further to try: the file is held by something, which
                # means it is also still runnable under its old name.
            }}
        }}
    }}
}}
# Report every command that is not there, rather than trusting the exit code.
# A version check cannot substitute: the shims are version-agnostic launchers,
# so an OLD shim reports the NEW version and "verified" would be a lie.
Write-Stage 'verifying' 'Checking that every command is in place.'
Write-InstallLog 'Verifying the installed launchers.'
$missing = @()
if ($binDir -and (Test-Path -LiteralPath $binDir -PathType Container)) {{
    foreach ($name in $commandNames) {{
        if (-not (Test-Path -LiteralPath (Join-Path $binDir ($name + '.exe')) -PathType Leaf)) {{
            $missing += $name
        }}
    }}
    # Reap the shims we moved aside -- but only after an install that WORKED.
    # On a failure the '.old-' files are the only launchers left and the sweep
    # above has just put them back; deleting them here is how a failed update
    # used to leave nothing at all behind.
    if ($code -eq 0) {{
        Get-ChildItem -Path $binDir -Filter '*.exe.old-*' -ErrorAction SilentlyContinue |
            ForEach-Object {{ Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }}
    }}
}}
# A shim whose rename was refused is NOT automatically a kept shim: uv may well
# have overwritten it in place, and saying otherwise would tell the user to
# restart a window that is already running the new code. The staged fallback's
# copy is the only thing that knows, because it is the step that fails on a file
# still held -- which is why $kept is built there and nowhere else, in both this
# helper and scripts/install.ps1.
# A shim that could not be replaced is NOT a failure: the command is present and
# already runs the new code through the canonical tool directory. Report it as
# refreshing on the next install and keep ok = true.
$ok = ($code -eq 0) -and ($missing.Count -eq 0)
# The only way to reach here with something already swapped is the
# new-command repair above: the environment was exchanged, and uv was then
# asked to write the launcher for a command this machine has never had. If that
# failed, the canonical path holds whatever uv left behind and a known-good
# environment is sitting aside with nothing to bring it back. Bring it back.
if ((-not $ok) -and $swappedAside -and (Test-Path -LiteralPath $swappedAside -PathType Container)) {{
    Write-InstallLog 'The launcher repair failed after the swap; putting the previous environment back.'
    Write-Stage 'rolling-back' 'The new version could not be finished, so the previous one is being put back.'
    try {{
        $wreckage = Join-Path $stagingRoot ($stamp + '-failed')
        New-Item -ItemType Directory -Path $wreckage -Force | Out-Null
        if (Test-Path -LiteralPath $toolDir -PathType Container) {{
            [System.IO.Directory]::Move($toolDir, (Join-Path $wreckage {_powershell_literal(PACKAGE_NAME)}))
        }}
        [System.IO.Directory]::Move($swappedAside, $toolDir)
        Remove-Item -LiteralPath $previousDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-InstallLog 'The previous environment is back at the canonical path.'
    }}
    catch {{
        Write-InstallLog ('The previous environment could not be put back: ' + $_.Exception.Message)
    }}
}}
$restartNote = if ($targetVersion) {{ ' to pick up ' + $targetVersion }} else {{ '' }}
$keptNote = if ($kept.Count -gt 0) {{ ' kept: ' + (($kept | ForEach-Object {{ $_ + '.exe (in use)' }}) -join ', ') + ' -- restart it' + $restartNote + '. These launchers were locked and kept the file they had. They keep working and will refresh on the next install.' }} else {{ '' }}
$result = @{{
    ok = $ok
    exit_code = $code
    attempts = $attempts
    disk_full = $diskFull
    missing_commands = $missing
    kept_shims = $kept
    restored_shims = $restoredShims
    message = if ($ok) {{ 'Deferred install completed.' + $keptNote }} elseif ($diskFull) {{ 'The update stopped because the disk is full. Free space and update again. uv tool install --force removes the previous environment before it writes the new one, so this machine has no mcc-server until that re-run finishes.' }} elseif ($missing.Count -gt 0) {{ 'Installed, but these commands are missing: ' + ($missing -join ', ') + '. Close the mcc-claude window(s) and re-run the install command.' }} else {{ 'Deferred install failed after ' + $attempts + ' attempt(s).' }}
    output = $output
}}
$result['restarted'] = $false
[System.IO.File]::WriteAllText({_powershell_literal(str(result_path))}, ($result | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
Write-InstallLog $result.message
if ($ok) {{
    if ($noRestart) {{
        Write-Stage 'handing-off' 'Installed. Handing the restart to the desktop app.'
    }} else {{
        Write-Stage 'starting' 'Starting the updated server.'
    }}
}} else {{
    Write-Stage 'failed' $result.message
}}
# The launcher lives in uv's bin directory, OUTSIDE the tool environment that
# was just replaced, and it is a version-agnostic stub. So it starts a server on
# both branches. Until 6.58.3 it ran only under `if ($ok)`, which is how a
# failed update left the machine with no server at all and no automatic
# recovery: the tool directory still held a perfectly good previous install and
# nothing ever started it. A half-installed environment is not a reason to
# withhold the old one -- uv either replaced the environment or it did not.
$restarted = $false
if (-not $noRestart) {{
    try {{
        Start-Process -FilePath {_powershell_literal(str(server_launcher))} -WorkingDirectory {_powershell_literal(str(working_directory))}
        $restarted = $true
        Write-InstallLog 'Started the updated server.'
    }}
    catch {{
        $restarted = $false
        Write-InstallLog ('The updated server could not be started: ' + $_.Exception.Message)
    }}
}}
$result['restarted'] = $restarted
if (-not $ok) {{
    # Say what went wrong AND what was done about it, in the one sentence the
    # dashboard's update banner shows. "It failed" on its own sent the user
    # looking for a server that nobody was going to start.
    $result['message'] = $result.message + $(if ($restarted) {{ ' The previous version was restarted.' }} elseif ($noRestart) {{ ' The desktop app starts the previous version again within ten seconds.' }} else {{ ' The previous version could not be restarted either.' }})
}}
[System.IO.File]::WriteAllText({_powershell_literal(str(result_path))}, ($result | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
$script:HelperDone = $true
Write-InstallLog $result.message
if ($ok) {{
    Remove-Item -Path {_powershell_literal(str(stage_dir / "wheel"))} -Recurse -Force -ErrorAction SilentlyContinue
    if ($noRestart) {{
        Write-Stage 'done' 'The new version is installed. The desktop app starts it.'
    }} else {{
        Write-Stage 'done' 'The updated server was started.'
    }}
}} else {{
    Write-Stage 'recovered' $result.message
}}
"""


def _spawn_deferred_upgrade(
    *,
    uv_executable: str,
    command: list[str],
    tag: str,
    log: list[str],
    no_restart: bool = False,
) -> UpgradeResult:
    """Hand the install to a detached helper that runs after we exit."""

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
    stage_dir = _stage_dir()
    result_path = stage_dir / _PENDING_RESULT_FILENAME
    # One episode, one transcript, named before the helper starts so the
    # response that triggers the update can hand the path to a browser tab
    # while there is still a server to hand it with.
    install_log = stage_dir / (
        f"{INSTALL_LOG_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}{INSTALL_LOG_SUFFIX}"
    )
    progress_path = str(stage_dir / UPDATE_PROGRESS_FILENAME)
    server_launcher = _server_launcher(uv_executable)
    if server_launcher is None:
        return UpgradeResult(
            ok=False,
            message=(
                "fcc-server was not found in uv's tool bin directory, so the "
                "update cannot be restarted safely. Re-run the install command instead."
            ),
            log=log,
        )
    with suppress(OSError):
        result_path.unlink()
    script_path = stage_dir / "apply-upgrade.ps1"
    try:
        script_path.write_text(
            _deferred_helper_script(
                uv_executable=uv_executable,
                command=command,
                result_path=result_path,
                stage_dir=stage_dir,
                install_log=install_log,
                server_launcher=server_launcher,
                working_directory=Path.cwd(),
                # The launcher lives in the uv tool bin directory, so its parent
                # IS that directory -- no second `uv tool dir --bin` call while
                # the server is still alive.
                bin_dir=server_launcher.parent,
                # The staged fallback rewrites the receipt's entrypoint paths
                # back to the real bin directory, so it needs the owner's dir.
                # Derived from sys.executable, not from `uv tool dir`: no uv
                # may run while the server is alive.
                tool_dir=_installed_tool_dir(),
                commands=_published_commands(),
                # Named in the receipt so a window can say WHICH version is
                # being installed while it waits, and so the kept-shim note can
                # tell the user which version a restart of that window buys.
                version=tag or None,
                # GAP-3: the desktop window asked for this update and owns the
                # restart. See `_deferred_helper_script`.
                no_restart=no_restart,
                # 6.72.0: where the new environment is built and where the old
                # one waits until the new one answers. Both derived from
                # `sys.executable`, like the tool directory above and for the
                # same reason -- no uv may run while the server is alive.
                staging_root=_aside_root(STAGING_ENV_DIRNAME),
                previous_root=_aside_root(PREVIOUS_ENV_DIRNAME),
                # The server knows its own address; the helper cannot find it
                # out once the server is gone, so it is baked in here.
                health_url=_health_url(),
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
    # Verified: with CREATE_NO_WINDOW the helper both runs and outlives us.
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
        )
    except (OSError, ValueError) as exc:
        return UpgradeResult(
            ok=False, message=f"Could not start the update helper: {exc!s}", log=log
        )

    log.append(
        "staged for install after shutdown (Windows); the desktop app starts the server"
        if no_restart
        else "staged for install and automatic restart after shutdown (Windows)"
    )
    _CACHE.restart_required = True
    _CACHE.staged_install = True
    set_external_upgrade_helper_pending(True)
    started_by = (
        "the desktop app starts the updated server within ten seconds"
        if no_restart
        else "start the updated server automatically"
    )
    return UpgradeResult(
        ok=True,
        message=(
            f"{tag or 'The latest release'} is verified and staged. The server "
            f"will close, install it after Windows releases the environment, then "
            f"{started_by}."
        ),
        installed_version=tag or None,
        log=log,
        log_path=str(install_log),
        progress_path=progress_path,
    )


def upgrade_to_latest(
    payload: dict[str, Any], *, no_restart: bool = False
) -> UpgradeResult:
    """Download, verify, and install the wheel from ``payload``.

    Synchronous and slow (a full dependency resolve): callers must run this in
    a worker thread so it never blocks the event loop.

    ``no_restart`` says that whoever asked for this update owns the restart --
    the desktop app, which has a ten-second tick and a spawn of its own. It
    only reaches the Windows deferred-helper path, because that is the only
    path on which anything here starts a server at all.
    """
    log: list[str] = []
    uv_executable = shutil.which("uv")
    if uv_executable is None:
        return UpgradeResult(
            ok=False,
            message="uv was not found on PATH; re-run the install script instead.",
        )

    asset = _select_wheel_asset(payload)
    if asset is None:
        return UpgradeResult(ok=False, message="That release publishes no wheel.")
    download_url = asset.get("browser_download_url")
    if not download_url:
        return UpgradeResult(ok=False, message="Release wheel has no download URL.")

    expected_digest = str(asset.get("digest") or "").removeprefix("sha256:").lower()
    tag = str(payload.get("tag_name") or "").lstrip("vV")

    if _wsl_windows_mount_tool_dir(uv_executable):
        return UpgradeResult(
            ok=False,
            message=(
                "The uv tool directory is under /mnt, where Windows file locks can "
                "corrupt an in-place WSL update. Move UV_TOOL_DIR to the WSL "
                "filesystem or re-run the install command after stopping the server."
            ),
        )

    with ExitStack() as stack:
        if _WINDOWS:
            wheel_dir = _stage_dir() / "wheel"
            with suppress(OSError):
                shutil.rmtree(wheel_dir)
            wheel_dir.mkdir(parents=True, exist_ok=True)
        else:
            wheel_dir = Path(
                stack.enter_context(tempfile.TemporaryDirectory(prefix="fcc-upgrade-"))
            )
        wheel_path = wheel_dir / str(asset.get("name"))
        try:
            with httpx.stream(
                "GET",
                download_url,
                timeout=_HTTP_TIMEOUT_SECONDS,
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                with wheel_path.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            return UpgradeResult(
                ok=False, message=f"Could not download the release wheel: {exc!s}"
            )
        log.append(f"downloaded {wheel_path.name}")

        actual_digest = _sha256_of(wheel_path)
        if expected_digest and actual_digest != expected_digest:
            # Same refusal the install scripts make: never install a wheel
            # whose checksum does not match what the release advertises.
            return UpgradeResult(
                ok=False,
                message="Release wheel checksum mismatch; refusing to install.",
                log=log,
            )
        log.append(
            f"verified sha256 {actual_digest[:16]}…"
            if expected_digest
            else "release published no digest; skipped checksum verification"
        )

        extras, python = _installed_extras_and_python(uv_executable)
        spec = wheel_path.as_uri()
        if extras:
            spec = f"{spec}[{','.join(extras)}]"
            log.append(f"preserving extras: {', '.join(extras)}")
        command = [
            uv_executable,
            "tool",
            "install",
            "--force",
            "--refresh-package",
            PACKAGE_NAME,
            "--python",
            python,
            spec,
        ]
        if _WINDOWS:
            return _spawn_deferred_upgrade(
                uv_executable=uv_executable,
                command=command,
                tag=tag,
                log=log,
                no_restart=no_restart,
            )
        try:
            # Fixed argv, never a shell string, so the release metadata cannot
            # inject arguments.
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_UPGRADE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return UpgradeResult(
                ok=False, message=f"Upgrade command failed: {exc!s}", log=log
            )

    tail = (completed.stderr or completed.stdout or "").strip().splitlines()
    log.extend(tail[-8:])
    if completed.returncode != 0:
        return UpgradeResult(
            ok=False,
            message=f"uv tool install exited with code {completed.returncode}.",
            log=log,
        )

    _CACHE.restart_required = True
    return UpgradeResult(
        ok=True,
        message=(
            f"Installed {tag or 'the latest release'}. The server will restart "
            "automatically and reconnect the dashboard."
        ),
        installed_version=tag or None,
        log=log,
    )


async def perform_upgrade(*, no_restart: bool = False) -> UpgradeResult:
    """Fetch the latest release and install it off the event loop.

    ``no_restart`` is passed by the dashboard when it is being shown inside the
    desktop app: that window owns the restart from 6.61.0, and two owners is
    how one update came to start two servers. A dashboard in a browser tab
    sends nothing and the helper restarts, exactly as before.
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
