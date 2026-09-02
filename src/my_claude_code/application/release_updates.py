"""Report the running version and upgrade to the latest published release.

The upgrade path deliberately mirrors ``scripts/install.sh``: fetch the wheel
published for the latest tag, verify its SHA-256, then hand it to
``uv tool install --force``. Reusing that shape keeps a dashboard-triggered
upgrade byte-identical to one done from the command line, checksum
verification included.

A successful dashboard upgrade automatically closes the current runtime and
starts the updated server. POSIX installs first and then replaces the process
image through the stable ``fcc-server`` launcher. Windows cannot replace the
environment underneath a running process: its interpreter and loaded DLLs are
held open, so ``uv tool install --force`` would fail partway and leave it
broken. There the verified wheel is staged and a detached PowerShell helper
installs only after this process exits, then relaunches that stable launcher.
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
from my_claude_code.config.settings import get_settings
from my_claude_code.core.process_handoff import (
    reset_process_handoff_for_tests,
    set_external_upgrade_helper_pending,
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
_STAGE_DIRNAME = "updates"
_PENDING_RESULT_FILENAME = "pending-upgrade.json"
# Bound on how long the helper waits for this process to exit before giving up,
# so a server left running forever does not leave a helper resident forever.
_HELPER_WAIT_SECONDS = 3600
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
        }


@dataclass(slots=True)
class UpgradeResult:
    """Outcome of one upgrade attempt."""

    ok: bool
    message: str
    installed_version: str | None = None
    log: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "installed_version": self.installed_version,
            "log": self.log,
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
    commands: list[str] | None = None,
) -> str:
    """PowerShell that waits for this process to exit, then installs.

    Written as PowerShell rather than Python because the only interpreter we
    can rely on is the one inside the environment being replaced -- using it
    would hold the very directory uv needs to delete.

    Receipts go through ``[System.IO.File]::WriteAllText`` with a BOM-less
    ``UTF8Encoding($false)``: Windows PowerShell 5.1's ``Set-Content
    -Encoding utf8`` prepends a UTF-8 BOM and the Python reader parses JSON,
    which refuses a leading U+FEFF.
    """

    quoted_args = ", ".join(_powershell_literal(arg) for arg in command[1:])
    names = commands if commands is not None else _published_commands()
    quoted_names = ", ".join(_powershell_literal(name) for name in names)
    bin_dir_literal = _powershell_literal(str(bin_dir) if bin_dir else "")
    return f"""$ErrorActionPreference = 'Stop'
$parent = {os.getpid()}
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
$deadline = (Get-Date).AddSeconds({_HELPER_WAIT_SECONDS})
while ((Get-Date) -lt $deadline) {{
    if (-not (Test-ParentAlive)) {{ break }}
    Start-Sleep -Milliseconds 500
}}
if (Test-ParentAlive) {{
    $result = @{{ ok = $false; message = 'Timed out waiting for the server to stop.' }}
    [System.IO.File]::WriteAllText({_powershell_literal(str(result_path))}, ($result | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
    exit 1
}}
# Give Windows a moment to release the handles the exiting process held.
Start-Sleep -Seconds 2
# uv writes progress to stderr. Under ErrorActionPreference='Stop' a native
# command's stderr becomes a *terminating* NativeCommandError, which would kill
# this script before it installs anything, so drop back to Continue for the
# call itself and judge the result by exit code alone.
$ErrorActionPreference = 'Continue'
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
$binDir = {bin_dir_literal}
$commandNames = @({quoted_names})
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if ($binDir -and (Test-Path -LiteralPath $binDir -PathType Container)) {{
    foreach ($name in $commandNames) {{
        $shim = Join-Path $binDir ($name + '.exe')
        if (Test-Path -LiteralPath $shim -PathType Leaf) {{
            Rename-Item -LiteralPath $shim -NewName ($name + '.exe.old-' + $stamp) -ErrorAction SilentlyContinue
        }}
    }}
}}
$delays = @(0, 5, 10, 20, 30)
foreach ($wait in $delays) {{
    if ($wait -gt 0) {{ Start-Sleep -Seconds $wait }}
    $output = & {_powershell_literal(uv_executable)} {quoted_args} 2>&1 | Out-String
    $code = $LASTEXITCODE
    $attempts = $delays.IndexOf($wait) + 1
    if ($code -eq 0) {{ break }}
}}
$ErrorActionPreference = 'Stop'
# Report every command that is not there, rather than trusting the exit code.
# A version check cannot substitute: the shims are version-agnostic launchers,
# so an OLD shim reports the NEW version and "verified" would be a lie.
$missing = @()
if ($binDir -and (Test-Path -LiteralPath $binDir -PathType Container)) {{
    foreach ($name in $commandNames) {{
        if (-not (Test-Path -LiteralPath (Join-Path $binDir ($name + '.exe')) -PathType Leaf)) {{
            $missing += $name
        }}
    }}
    # Reap the shims we moved aside. One still held by a live window refuses to
    # delete; it is left for the next install to sweep.
    Get-ChildItem -Path $binDir -Filter '*.exe.old-*' -ErrorAction SilentlyContinue |
        ForEach-Object {{ Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }}
}}
$ok = ($code -eq 0) -and ($missing.Count -eq 0)
$result = @{{
    ok = $ok
    exit_code = $code
    attempts = $attempts
    missing_commands = $missing
    message = if ($ok) {{ 'Deferred install completed.' }} elseif ($missing.Count -gt 0) {{ 'Installed, but these commands are missing: ' + ($missing -join ', ') + '. Close the mcc-claude window(s) and re-run the install command.' }} else {{ 'Deferred install failed after ' + $attempts + ' attempt(s).' }}
    output = $output
}}
[System.IO.File]::WriteAllText({_powershell_literal(str(result_path))}, ($result | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
if ($ok) {{
    Remove-Item -Path {_powershell_literal(str(stage_dir / "wheel"))} -Recurse -Force -ErrorAction SilentlyContinue
    # The launcher lives in uv's bin directory, outside the tool environment
    # which was just replaced. Start it only after a successful install; a
    # failed helper leaves the receipt for the dashboard and never starts a
    # half-installed server.
    Start-Process -FilePath {_powershell_literal(str(server_launcher))} -WorkingDirectory {_powershell_literal(str(working_directory))}
}}
"""


def _spawn_deferred_upgrade(
    *,
    uv_executable: str,
    command: list[str],
    tag: str,
    log: list[str],
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
                server_launcher=server_launcher,
                working_directory=Path.cwd(),
                # The launcher lives in the uv tool bin directory, so its parent
                # IS that directory -- no second `uv tool dir --bin` call while
                # the server is still alive.
                bin_dir=server_launcher.parent,
                commands=_published_commands(),
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

    log.append("staged for install and automatic restart after shutdown (Windows)")
    _CACHE.restart_required = True
    _CACHE.staged_install = True
    set_external_upgrade_helper_pending(True)
    return UpgradeResult(
        ok=True,
        message=(
            f"{tag or 'The latest release'} is verified and staged. The server "
            "will close, install it after Windows releases the environment, then "
            "start the updated server automatically."
        ),
        installed_version=tag or None,
        log=log,
    )


def upgrade_to_latest(payload: dict[str, Any]) -> UpgradeResult:
    """Download, verify, and install the wheel from ``payload``.

    Synchronous and slow (a full dependency resolve): callers must run this in
    a worker thread so it never blocks the event loop.
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


async def perform_upgrade() -> UpgradeResult:
    """Fetch the latest release and install it off the event loop."""
    payload, _checked_at, error = await _CACHE.get(force=True)
    if payload is None:
        return UpgradeResult(ok=False, message=error or "No release information.")
    if not is_newer(str(payload.get("tag_name") or ""), current_version()):
        return UpgradeResult(ok=False, message="Already on the latest release.")
    return await asyncio.to_thread(upgrade_to_latest, payload)


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
