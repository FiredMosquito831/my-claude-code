"""Persisted RTK token-optimizer state and machine reconciliation."""

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any

from .harnesses import harness_specs, rtk_capable_ids
from .paths import config_dir_path

RTK_STATE_FILENAME = "rtk.json"
RTK_VERSION = "0.45.0"
RTK_RELEASE_BASE_URL = f"https://github.com/rtk-ai/rtk/releases/download/v{RTK_VERSION}"
#: RTK reads this env var as its telemetry opt-out. Verified against
#: rtk-ai/rtk @ master (v0.45.0): ``TelemetryConfig`` derives ``Default`` with
#: ``enabled: bool`` (false), ``core/telemetry.rs::maybe_ping`` returns early
#: unless ``consent_given == Some(true)`` *and* ``enabled``, and
#: ``hooks/init.rs::prompt_telemetry_consent`` short-circuits when this var is
#: set — so an ``rtk init`` we launch never records consent and the PreToolUse
#: hook, which does not inherit our environment, still has nothing to send.
RTK_TELEMETRY_ENV = "RTK_TELEMETRY_DISABLED"
RTK_SUBPROCESS_TIMEOUT_SECONDS = 15

#: Field names of RTK's ``ExportSummary`` struct (``src/analytics/gain.rs``).
RTK_GAIN_SUMMARY_FIELDS: tuple[str, ...] = (
    "total_commands",
    "total_input",
    "total_output",
    "total_saved",
    "avg_savings_pct",
    "total_time_ms",
    "avg_time_ms",
)
#: Optional period breakdowns RTK emits alongside the summary for ``--all``.
RTK_GAIN_PERIOD_FIELDS: tuple[str, ...] = ("daily", "weekly", "monthly")

_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")

_RELEASES: dict[tuple[str, str], tuple[str, str]] = {
    ("linux", "x86_64"): (
        "rtk-x86_64-unknown-linux-musl.tar.gz",
        "c4c036fbf181fc55ef329786c8c17e0d427972b053b825944d968a6aafef1ba4",
    ),
    ("linux", "aarch64"): (
        "rtk-aarch64-unknown-linux-gnu.tar.gz",
        "80a746dd305ef944ff50ef011ae4ce3878dd5ba88dfe35d859d05498191637c3",
    ),
    ("darwin", "x86_64"): (
        "rtk-x86_64-apple-darwin.tar.gz",
        "9ea02f889d5a2779e4fb700df4587824303c5a57cda22e903e30058079fca0ef",
    ),
    ("darwin", "aarch64"): (
        "rtk-aarch64-apple-darwin.tar.gz",
        "064151cfc2d50b24d810b06a0af2e41b9c945e83534e4c438c3d3eae607fc3f4",
    ),
    ("win32", "x86_64"): (
        "rtk-x86_64-pc-windows-msvc.zip",
        "34cea9009a8099acdaf85147b971d95f65efabfa63fb3aea7d3e2b73e6f517c3",
    ),
}

# Derived from the harness registry rather than restated: an agent RTK is
# enabled for and an agent MCC can launch are the same list, and keeping two
# copies is how one of them ends up missing an entry.
_ENABLE_COMMANDS: dict[str, tuple[str, ...]] = {
    spec.id: spec.rtk_enable_args for spec in harness_specs() if spec.rtk_agent
}
_UNINSTALL_COMMANDS: dict[str, tuple[str, ...]] = {
    spec.id: spec.rtk_uninstall_args for spec in harness_specs() if spec.rtk_agent
}


class RtkError(Exception):
    """Raised when RTK state cannot be persisted or reconciled."""


class RtkState:
    """Desired RTK integration state, keyed by harness id.

    Three hard-coded booleans until the harness registry existed, which meant
    every new agent needed this class, the RTK CLI, the tray menu, the admin
    payload and the installer's enable list edited together. It is now a
    mapping whose keys come from ``rtk_capable_ids()``, so an agent is added in
    one place.

    Attribute access (``state.claude``) is kept because it reads better at the
    call sites and because the persisted document has always been a flat
    ``{"claude": true, ...}`` object -- the on-disk format is unchanged, and a
    state file written by an older MCC loads with no migration step beyond
    dropping keys for agents this build does not know.
    """

    __slots__ = ("_agents",)

    def __init__(
        self, agents: Mapping[str, bool] | None = None, **per_agent: bool
    ) -> None:
        values = dict.fromkeys(rtk_capable_ids(), False)
        for source in (agents or {}, per_agent):
            for name, value in source.items():
                if name not in values:
                    raise ValueError(f"unknown RTK agent: {name}")
                values[name] = bool(value)
        self._agents = values

    def enabled(self, harness_id: str) -> bool:
        """Return whether RTK is desired for one harness."""

        return self._agents.get(harness_id, False)

    def as_dict(self) -> dict[str, bool]:
        """Return the persisted flat mapping of harness id to desired state."""

        return dict(self._agents)

    @property
    def any_enabled(self) -> bool:
        """Return whether RTK is desired for at least one harness."""

        return any(self._agents.values())

    def __getattr__(self, name: str) -> bool:
        try:
            return self._agents[name]
        except KeyError:
            raise AttributeError(name) from None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RtkState):
            return NotImplemented
        return self._agents == other._agents

    def __hash__(self) -> int:
        return hash(frozenset(self._agents.items()))

    def __repr__(self) -> str:
        enabled = ", ".join(f"{name}={value}" for name, value in self._agents.items())
        return f"RtkState({enabled})"


def rtk_state_path() -> Path:
    """Return the persisted desired-state path."""

    return config_dir_path() / RTK_STATE_FILENAME


def load_rtk_state() -> RtkState:
    """Load desired RTK state, returning defaults for missing or corrupt data."""

    try:
        data = json.loads(rtk_state_path().read_text(encoding="utf-8"))
    except OSError, ValueError, TypeError:
        return RtkState()
    if not isinstance(data, dict):
        return RtkState()

    # Keys for agents this build does not know are dropped rather than being
    # an error: a state file written by a newer MCC, or by one that supported
    # an agent since removed, must still load.
    values = {
        name: value
        for name, value in data.items()
        if name in rtk_capable_ids() and isinstance(value, bool)
    }
    return RtkState(values)


def save_rtk_state(state: RtkState) -> None:
    """Persist desired RTK state atomically."""

    path = rtk_state_path()
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(state.as_dict()), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        with suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise RtkError(f"Failed to save RTK state: {exc}") from exc


def _normalized_architecture(machine: str) -> str:
    architecture = machine.strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }
    return aliases.get(architecture, architecture)


def _release_for_current_platform() -> tuple[str, str]:
    key = (sys.platform, _normalized_architecture(platform.machine()))
    release = _RELEASES.get(key)
    if release is None:
        raise RtkError(
            f"RTK {RTK_VERSION} has no release for {sys.platform} "
            f"{platform.machine() or 'unknown architecture'}."
        )
    return release


def _managed_binary_path() -> Path:
    filename = "rtk.exe" if sys.platform == "win32" else "rtk"
    return Path.home() / ".local" / "bin" / filename


def _verify_rtk(binary: str | Path) -> str | None:
    env = _rtk_environment()
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=RTK_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RtkError(f"Could not run RTK at {binary}: {exc}") from exc
    version = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        raise RtkError(
            f"RTK verification failed at {binary} with exit code "
            f"{completed.returncode}."
        )
    return version or None


def _extract_binary(archive_path: Path, asset_name: str, destination: Path) -> None:
    executable_name = "rtk.exe" if asset_name.endswith(".zip") else "rtk"
    try:
        if asset_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as archive:
                members = [
                    info
                    for info in archive.infolist()
                    if not info.is_dir()
                    and PurePosixPath(info.filename).name == executable_name
                ]
                if len(members) != 1:
                    raise RtkError(
                        f"Verified RTK archive must contain exactly one {executable_name}."
                    )
                with (
                    archive.open(members[0]) as source,
                    destination.open("wb") as target,
                ):
                    shutil.copyfileobj(source, target)
        else:
            with tarfile.open(archive_path, "r:gz") as archive:
                members = [
                    member
                    for member in archive.getmembers()
                    if member.isfile()
                    and PurePosixPath(member.name).name == executable_name
                ]
                if len(members) != 1:
                    raise RtkError(
                        f"Verified RTK archive must contain exactly one {executable_name}."
                    )
                source = archive.extractfile(members[0])
                if source is None:
                    raise RtkError("Verified RTK executable could not be extracted.")
                with source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
    except RtkError:
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise RtkError(f"Could not extract verified RTK archive: {exc}") from exc

    if destination.stat().st_size == 0:
        raise RtkError("Verified RTK executable was empty.")


def _ensure_rtk_binary() -> Path:
    """Return a verified RTK binary, installing the pinned release if absent."""

    existing = shutil.which("rtk")
    if existing is not None:
        _verify_rtk(existing)
        return Path(existing)

    asset_name, expected_sha256 = _release_for_current_platform()
    url = f"{RTK_RELEASE_BASE_URL}/{asset_name}"
    destination = _managed_binary_path()
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mcc-rtk-") as temporary_directory:
        archive_path = Path(temporary_directory) / asset_name
        temporary_binary = destination.with_name(f".{destination.name}.tmp")
        try:
            try:
                with (
                    urllib.request.urlopen(url, timeout=60) as response,
                    archive_path.open("wb") as archive_file,
                ):
                    shutil.copyfileobj(response, archive_file)
            except OSError as exc:
                raise RtkError(f"Could not download RTK {RTK_VERSION}: {exc}") from exc

            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            if digest != expected_sha256:
                raise RtkError(
                    f"RTK checksum verification failed for {asset_name}: "
                    f"expected {expected_sha256}, got {digest}."
                )

            _extract_binary(archive_path, asset_name, temporary_binary)
            temporary_binary.chmod(
                temporary_binary.stat().st_mode
                | stat.S_IXUSR
                | stat.S_IXGRP
                | stat.S_IXOTH
            )
            os.replace(temporary_binary, destination)
        except RtkError:
            with suppress(OSError):
                temporary_binary.unlink(missing_ok=True)
            raise
        except OSError as exc:
            with suppress(OSError):
                temporary_binary.unlink(missing_ok=True)
            raise RtkError(f"Could not install RTK at {destination}: {exc}") from exc

    _verify_rtk(destination)
    return destination


def _available_binary() -> Path | None:
    discovered = shutil.which("rtk")
    if discovered is not None:
        return Path(discovered)
    managed = _managed_binary_path()
    return managed if managed.is_file() else None


def _run_rtk(binary: Path, arguments: tuple[str, ...]) -> None:
    env = _rtk_environment()
    try:
        subprocess.run(
            [str(binary), *arguments],
            check=True,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RtkError(f"RTK command failed: {' '.join(arguments)}: {exc}") from exc


def _ensure_claude_config_directory() -> None:
    """Create the Claude Code config directory RTK writes its hooks into.

    ``rtk init --auto-patch`` writes RTK.md next to the Claude settings file, but
    it does not create the directory first, so an RTK enable on a machine that
    has never run Claude Code fails. Mirror the upstream installer's pre-step.
    """

    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(configured) if configured else Path.home() / ".claude"
    path.mkdir(parents=True, exist_ok=True)


def apply_rtk_state(state: RtkState, *, uninstall: bool = False) -> None:
    """Reconcile installed RTK hooks and optionally remove its managed binary."""

    binary = _ensure_rtk_binary() if state.any_enabled else _available_binary()

    if binary is not None:
        if state.enabled("claude"):
            _ensure_claude_config_directory()
        for agent in rtk_capable_ids():
            enabled = state.enabled(agent)
            command = _ENABLE_COMMANDS[agent] if enabled else _UNINSTALL_COMMANDS[agent]
            _run_rtk(binary, command)

    if uninstall:
        managed = _managed_binary_path()
        try:
            managed.unlink(missing_ok=True)
        except OSError as exc:
            raise RtkError(f"Could not remove RTK binary at {managed}: {exc}") from exc


def _rtk_environment() -> dict[str, str]:
    """Return a subprocess environment with RTK telemetry forced off."""

    env = os.environ.copy()
    env[RTK_TELEMETRY_ENV] = "1"
    return env


def parse_rtk_version(text: str | None) -> str | None:
    """Extract a bare ``MAJOR.MINOR.PATCH`` from ``rtk --version`` output."""

    if not text:
        return None
    match = _VERSION_PATTERN.search(text)
    return match.group(0) if match is not None else None


def rtk_status() -> dict[str, Any]:
    """Return desired agent state and verified binary metadata."""

    state = load_rtk_state()
    binary = _available_binary()
    version: str | None = None
    installed = False
    if binary is not None:
        try:
            version = _verify_rtk(binary)
            installed = True
        except RtkError:
            pass
    installed_version = parse_rtk_version(version)
    matches_pin: bool | None = None
    if installed_version is not None:
        matches_pin = installed_version == RTK_VERSION
    return {
        "installed": installed,
        **state.as_dict(),
        "agents": state.as_dict(),
        "binary_path": str(binary) if binary is not None else None,
        "version": version,
        "installed_version": installed_version,
        "pinned_version": RTK_VERSION,
        "version_matches_pin": matches_pin,
    }


# ------------------------------------------------------------------ rtk gain


def _unavailable_gain(reason: str, detail: str | None = None) -> dict[str, Any]:
    """Return the canonical "no savings data, and here is why" payload."""

    return {
        "available": False,
        "reason": reason,
        "detail": detail,
        "binary_path": None,
        "summary": None,
        "periods": None,
        "raw": None,
    }


def _optional_number(value: object) -> int | float | None:
    """Return ``value`` when it is a real JSON number, otherwise ``None``.

    Booleans are rejected on purpose: a missing or non-numeric field must stay
    distinguishable from a genuine zero RTK reported.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None


def _parse_gain_summary(
    payload: dict[str, Any],
) -> dict[str, int | float | None] | None:
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return None
    parsed: dict[str, int | float | None] = {
        name: _optional_number(summary.get(name)) for name in RTK_GAIN_SUMMARY_FIELDS
    }
    if all(value is None for value in parsed.values()):
        return None
    return parsed


def _parse_gain_periods(payload: dict[str, Any]) -> dict[str, list[Any] | None]:
    periods: dict[str, list[Any] | None] = {}
    for name in RTK_GAIN_PERIOD_FIELDS:
        value = payload.get(name)
        periods[name] = value if isinstance(value, list) else None
    return periods


def read_rtk_gain() -> dict[str, Any]:
    """Read RTK's own token-savings report via ``rtk gain --all --format json``.

    Never raises: every failure mode (no binary, non-zero exit, empty stdout,
    unparseable JSON, unexpected schema, timeout) returns a payload with
    ``available`` false and a machine-readable ``reason``.
    """

    binary = _available_binary()
    if binary is None:
        return _unavailable_gain("not_installed", "No RTK binary was found.")

    try:
        completed = subprocess.run(
            [str(binary), "gain", "--all", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=RTK_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
            env=_rtk_environment(),
        )
    except subprocess.TimeoutExpired:
        return _unavailable_gain(
            "timeout",
            f"rtk gain did not finish within {RTK_SUBPROCESS_TIMEOUT_SECONDS}s.",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _unavailable_gain("run_failed", f"Could not run rtk gain: {exc}")

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return _unavailable_gain(
            "run_failed",
            f"rtk gain exited with code {completed.returncode}."
            + (f" {detail}" if detail else ""),
        )

    stdout = (completed.stdout or "").strip()
    if not stdout:
        return _unavailable_gain("empty_output", "rtk gain produced no output.")

    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        return _unavailable_gain("invalid_json", f"rtk gain output was not JSON: {exc}")

    if not isinstance(payload, dict):
        return _unavailable_gain(
            "unexpected_schema", "rtk gain returned a non-object payload."
        )

    summary = _parse_gain_summary(payload)
    if summary is None:
        return _unavailable_gain(
            "unexpected_schema",
            "rtk gain returned no recognizable summary fields.",
        )

    return {
        "available": True,
        "reason": None,
        "detail": None,
        "binary_path": str(binary),
        "summary": summary,
        "periods": _parse_gain_periods(payload),
        "raw": payload,
    }
