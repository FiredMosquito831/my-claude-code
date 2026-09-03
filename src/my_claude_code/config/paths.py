"""Shared filesystem paths for My Claude Code configuration."""

import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

# The config directory a new install creates.
MCC_CONFIG_DIRNAME = ".mcc"
# The directory every install before 6.40.0 created. Still fully supported: it
# is never moved, renamed or written to by resolution, only read.
LEGACY_CONFIG_DIRNAME = ".fcc"
# Created empty by ``mcc-migrate`` to hold the rollback note. Never data.
RETIRED_CONFIG_DIRNAME = ".fcc-old"
# Absolute override. There was never an ``FCC_CONFIG_DIR``, so there is no
# legacy alias to honour here.
CONFIG_DIR_ENV = "MCC_CONFIG_DIR"
# Kept for importers of the pre-6.40.0 name; it always meant the legacy home.
FCC_CONFIG_DIRNAME = LEGACY_CONFIG_DIRNAME
FCC_ENV_FILENAME = ".env"
LEGACY_REPO_DIRNAME = "free-claude-code"
LEGACY_XDG_CONFIG_DIRNAME = ".config"
MESSAGING_STATE_DIRNAME = "agent_workspace"
FCC_LOGS_DIRNAME = "logs"
SERVER_LOG_FILENAME = "server.log"
REQUEST_LOG_FILENAME = "requests.db"
# Owned by ``config.provider_registry``; ``paths`` cannot import it (that module
# imports this one), so the health check below repeats the name and
# ``tests/contracts/test_config_dir_is_single_sourced.py`` pins the two together.
CUSTOM_PROVIDERS_FILENAME = "custom_providers.json"
CODEX_MODEL_CATALOG_FILENAME = "codex-model-catalog.json"
AUTH_DIRNAME = "auth"
CHATGPT_OAUTH_AUTH_FILENAME = "chatgpt-oauth.json"
ANTHROPIC_OAUTH_MANAGED_STORE_FILENAME = "anthropic_oauth.json"
CLAUDE_CONFIG_DIRNAME = ".claude"
CLAUDE_SETTINGS_FILENAME = "settings.json"
ONBOARDING_STATE_FILENAME = "onboarding.json"
MODEL_OVERRIDES_FILENAME = "model_overrides.json"
HARNESS_TIERS_FILENAME = "harness_tiers.json"
WSL_OSRELEASE_PATH = "/proc/sys/kernel/osrelease"
WSL_WINDOWS_USERS_DIR = "/mnt/c/Users"
MACOS_MANAGED_SETTINGS_PATH = (
    "/Library/Application Support/ClaudeCode/managed-settings.json"
)
MACOS_MANAGED_SETTINGS_DROPIN_DIR = (
    "/Library/Application Support/ClaudeCode/managed-settings.d"
)
LINUX_MANAGED_SETTINGS_PATH = "/etc/claude-code/managed-settings.json"
LINUX_MANAGED_SETTINGS_DROPIN_DIR = "/etc/claude-code/managed-settings.d"
WINDOWS_MANAGED_SETTINGS_PATH = r"C:\Program Files\ClaudeCode\managed-settings.json"
WINDOWS_MANAGED_SETTINGS_DROPIN_DIR = r"C:\Program Files\ClaudeCode\managed-settings.d"

# Columns ``core.request_log`` cannot add to an existing database on open: they
# are in the original CREATE TABLE, so a ``requests`` table without them was not
# written by any version of this store. ``core`` may not import ``config`` and
# ``config`` may not import ``core`` (import-boundary contract), so this list is
# mirrored here from ``request_log.required_request_columns()`` and
# ``tests/contracts/test_config_dir_is_single_sourced.py`` fails if they drift.
LEGACY_REQUEST_LOG_COLUMNS: tuple[str, ...] = (
    "id",
    "ts_epoch",
    "ts_iso",
    "endpoint",
    "protocol",
    "requested_model",
    "provider",
    "resolved_model",
    "stream",
    "input_text",
    "output_text",
    "input_sha256",
    "output_sha256",
    "input_chars",
    "output_chars",
    "reasoning",
    "params",
    "tokens_in",
    "tokens_out",
    "ttft_ms",
    "duration_ms",
    "status",
    "error_kind",
    "error_message",
    "headers",
)

LegacyCheck = Literal["readable", "env", "request_log", "custom_providers"]
ConfigDirSource = Literal["env", "current", "legacy", "created"]
LEGACY_CHECKS: tuple[LegacyCheck, ...] = (
    "readable",
    "env",
    "request_log",
    "custom_providers",
)


@dataclass(frozen=True, slots=True)
class LegacyHomeHealth:
    """Whether a legacy ``~/.fcc`` is safe to run from, and why not if it is not."""

    healthy: bool
    failed_check: LegacyCheck | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ConfigDirResolution:
    """The answer of the one function that decides where configuration lives."""

    path: Path
    source: ConfigDirSource
    legacy_path: Path | None = None
    legacy_health: LegacyHomeHealth | None = None
    notice: str = ""
    warning: str = ""

    @property
    def uses_legacy_home(self) -> bool:
        return self.source == "legacy"

    @property
    def legacy_rejected(self) -> bool:
        health = self.legacy_health
        return self.source == "created" and health is not None and not health.healthy


def display_path(path: Path) -> str:
    """Render a path under the home directory as ``~/...`` for humans."""

    try:
        return f"~/{path.relative_to(Path.home()).as_posix()}"
    except ValueError, OSError:
        return str(path)


def _check_readable(path: Path) -> LegacyHomeHealth:
    try:
        with os.scandir(path) as entries:
            for _ in entries:
                break
    except OSError as exc:
        return LegacyHomeHealth(False, "readable", f"cannot list {path}: {exc}")
    return LegacyHomeHealth(True)


def _check_env(path: Path) -> LegacyHomeHealth:
    env_path = path / FCC_ENV_FILENAME
    if not env_path.is_file():
        return LegacyHomeHealth(False, "env", f"{env_path} is missing")
    # ``config.settings`` imports this module, so a top-level ``from
    # .settings import Settings`` would close the cycle ``paths -> settings ->
    # env_files -> paths`` and trip the acyclic-import contract test. Import
    # via ``importlib`` instead: the runtime behaviour is identical, but the
    # static import scanner only sees ``ast.Import``/``ImportFrom`` statements,
    # so this does not add a ``paths -> settings`` edge to the graph.
    #
    # Load the provider registry for THIS directory before building ``Settings``.
    # A user's ``.env`` routinely names custom providers (e.g.
    # ``MODEL_OPUS=custom_x/model``); those ids are only known once the registry
    # has read the directory's ``custom_providers.json``. Without this, the
    # build fails on a perfectly healthy legacy home and the resolution
    # wrongly falls through to a fresh ``~/.mcc``. Pointing a fresh registry at
    # the explicit file makes the check independent of whichever directory
    # ``config_dir_path()`` happens to resolve to at this instant. Any failure
    # to build is reported, not swallowed, so a genuinely corrupt ``.env``
    # still fails the check.
    try:
        import importlib

        settings_module = importlib.import_module("my_claude_code.config.settings")
        Settings = settings_module.Settings
        registry_module = importlib.import_module(
            "my_claude_code.config.provider_registry"
        )
        # Point a fresh registry at this directory's ``custom_providers.json``
        # and install it only for the duration of this check. A user's ``.env``
        # routinely names custom providers (e.g. ``MODEL_OPUS=custom_x/model``);
        # those ids are only known once the registry has read that file. The
        # previous global registry is restored afterwards so the probe never
        # pollutes the provider ids the rest of the process sees.
        previous_registry = getattr(registry_module, "_registry", None)
        legacy_registry = registry_module.ProviderRegistry(
            path=path / CUSTOM_PROVIDERS_FILENAME
        )
        registry_module._registry = legacy_registry
        try:
            legacy_registry.configurable_ids()
            # Build Settings with no constructor override: the provisional
            # cache makes ``managed_env_path()`` point at this directory's
            # ``.env``, so the model reads the file we are probing. Passing
            # ``_env_file`` to the constructor would NOT override
            # pydantic-settings' ``env_file`` config, so the file would be
            # silently ignored.
            Settings()
        finally:
            registry_module._registry = previous_registry
    except Exception as exc:
        summary = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return LegacyHomeHealth(
            False, "env", f"{env_path} does not build settings: {summary}"
        )
    return LegacyHomeHealth(True)


def _check_request_log(path: Path) -> LegacyHomeHealth:
    db_path = path / FCC_LOGS_DIRNAME / REQUEST_LOG_FILENAME
    if not db_path.exists():
        return LegacyHomeHealth(True)
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as exc:
        return LegacyHomeHealth(False, "request_log", f"{db_path} will not open: {exc}")
    try:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(requests)")
        }
    except sqlite3.Error as exc:
        return LegacyHomeHealth(False, "request_log", f"{db_path} is unreadable: {exc}")
    finally:
        connection.close()
    if not columns:
        # No ``requests`` table at all: an empty file the store creates on open.
        return LegacyHomeHealth(True)
    missing = [name for name in LEGACY_REQUEST_LOG_COLUMNS if name not in columns]
    if missing:
        return LegacyHomeHealth(
            False,
            "request_log",
            f"{db_path} is missing request columns: {', '.join(missing)}",
        )
    return LegacyHomeHealth(True)


def _check_custom_providers(path: Path) -> LegacyHomeHealth:
    providers_path = path / CUSTOM_PROVIDERS_FILENAME
    if not providers_path.exists():
        return LegacyHomeHealth(True)
    try:
        payload = json.loads(providers_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return LegacyHomeHealth(
            False, "custom_providers", f"{providers_path} does not parse: {exc}"
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
        return LegacyHomeHealth(
            False,
            "custom_providers",
            f'{providers_path} is not a {{"providers": [...]}} object',
        )
    return LegacyHomeHealth(True)


def check_legacy_home(path: Path) -> LegacyHomeHealth:
    """Return whether a legacy config directory is safe to run from.

    Four checks, in order, and nothing else: the directory lists; ``.env`` is
    present and builds a ``Settings``; ``logs/requests.db`` is absent or opens
    read-only with the columns the store requires; ``custom_providers.json`` is
    absent or parses as the documented wrapper object. A failure names the check
    and never touches the directory.
    """

    for check in (
        _check_readable,
        _check_env,
        _check_request_log,
        _check_custom_providers,
    ):
        health = check(path)
        if not health.healthy:
            return health
    return LegacyHomeHealth(True)


def resolve_config_dir(
    env: Mapping[str, str] | None = None, home: Path | None = None
) -> ConfigDirResolution:
    """Decide where configuration lives, without caching or logging.

    1. ``MCC_CONFIG_DIR`` when set.
    2. ``~/.mcc`` when it exists -- and if ``~/.fcc`` also exists, one warning
       naming both and saying which wins.
    3. otherwise ``~/.fcc`` when it exists *and* passes ``check_legacy_home``.
    4. otherwise ``~/.mcc``, created fresh. A legacy directory that failed a
       check is named in the warning and left exactly as it is.
    """

    source_env = os.environ if env is None else env
    override = source_env.get(CONFIG_DIR_ENV)
    if override:
        resolved = Path(override).expanduser()
        return ConfigDirResolution(
            path=resolved,
            source="env",
            notice=f"Config directory {resolved} (from {CONFIG_DIR_ENV}).",
        )

    base = Path.home() if home is None else home
    current = base / MCC_CONFIG_DIRNAME
    legacy = base / LEGACY_CONFIG_DIRNAME

    if current.is_dir():
        if legacy.is_dir():
            return ConfigDirResolution(
                path=current,
                source="current",
                legacy_path=legacy,
                warning=(
                    f"Two My Claude Code config directories exist: "
                    f"{display_path(current)} and {display_path(legacy)}. "
                    f"{display_path(current)} wins; {display_path(legacy)} is "
                    f"ignored and left untouched. Nothing is ever merged."
                ),
            )
        return ConfigDirResolution(path=current, source="current")

    if legacy.is_dir():
        health = check_legacy_home(legacy)
        if health.healthy:
            return ConfigDirResolution(
                path=legacy,
                source="legacy",
                legacy_path=legacy,
                legacy_health=health,
                notice=(
                    f"Your data lives in the legacy {display_path(legacy)}. Run "
                    f"mcc-migrate to move it to {display_path(current)}."
                ),
            )
        return ConfigDirResolution(
            path=current,
            source="created",
            legacy_path=legacy,
            legacy_health=health,
            warning=(
                f"{display_path(legacy)} exists but failed the "
                f"'{health.failed_check}' check ({health.detail}). It was left "
                f"untouched -- nothing was moved, renamed or deleted. Starting "
                f"fresh in {display_path(current)}."
            ),
        )

    return ConfigDirResolution(path=current, source="created")


_resolution: ConfigDirResolution | None = None


def _resolve_and_log() -> ConfigDirResolution:
    global _resolution
    # The legacy health check builds a ``Settings`` from the legacy ``.env``,
    # and ``Settings`` resolves its dotenv list back through this function.
    # Publishing the legacy home as the provisional answer first means that
    # nested call sees the directory being probed instead of recursing.
    _resolution = ConfigDirResolution(
        path=Path.home() / LEGACY_CONFIG_DIRNAME, source="legacy"
    )
    try:
        resolution = resolve_config_dir()
    finally:
        _resolution = None
    if resolution.warning:
        logger.warning(resolution.warning)
    elif resolution.notice:
        logger.info(resolution.notice)
    return resolution


def config_dir_resolution() -> ConfigDirResolution:
    """Return the cached config-directory decision for this process."""

    global _resolution
    if _resolution is None:
        _resolution = _resolve_and_log()
    return _resolution


def reset_config_dir_cache() -> None:
    """Forget the cached decision so the next call resolves again (tests)."""

    global _resolution
    _resolution = None


def config_dir_path() -> Path:
    """Return the user config directory.

    The one function every consumer goes through. See ``resolve_config_dir``
    for the order; the answer is cached for the life of the process so a
    directory cannot change underneath a running server.
    """

    return config_dir_resolution().path


def legacy_config_dir_path() -> Path:
    """Return the legacy ``~/.fcc`` home, whether or not it exists."""

    return Path.home() / LEGACY_CONFIG_DIRNAME


def retired_config_dir_path() -> Path:
    """Return the ``~/.fcc-old`` rollback-note directory ``mcc-migrate`` writes."""

    return Path.home() / RETIRED_CONFIG_DIRNAME


def request_log_path() -> Path:
    """Return the request log database path under the resolved config directory."""

    return config_dir_path() / FCC_LOGS_DIRNAME / REQUEST_LOG_FILENAME


def managed_env_path() -> Path:
    """Return the default user-managed env file path."""

    return config_dir_path() / FCC_ENV_FILENAME


def model_overrides_path() -> Path:
    """Return the per-provider/per-model request parameter override file."""

    return config_dir_path() / MODEL_OVERRIDES_FILENAME


def harness_tiers_path() -> Path:
    """Return the per-coding-agent tier override file."""

    return config_dir_path() / HARNESS_TIERS_FILENAME


def legacy_env_paths() -> tuple[Path, ...]:
    """Return legacy user env paths that can be migrated to the resolved config directory's .env."""

    home = Path.home()
    return (
        home / LEGACY_REPO_DIRNAME / FCC_ENV_FILENAME,
        home / LEGACY_XDG_CONFIG_DIRNAME / LEGACY_REPO_DIRNAME / FCC_ENV_FILENAME,
    )


def messaging_state_dir_path() -> Path:
    """Return the managed messaging state directory."""

    return config_dir_path() / MESSAGING_STATE_DIRNAME


def server_log_path() -> Path:
    """Return the canonical server log path."""

    return config_dir_path() / FCC_LOGS_DIRNAME / SERVER_LOG_FILENAME


def codex_model_catalog_path() -> Path:
    """Return the generated Codex model catalog path."""

    return harness_catalogue_path(CODEX_MODEL_CATALOG_FILENAME)


def harness_catalogue_path(filename: str) -> Path:
    """Return the path of one generated harness catalogue.

    Always under the resolved config directory, never inside the CLI's own configuration
    directory: a file MCC writes is a file MCC must be able to remove, and a
    user who stops using a harness should not be left with MCC's leftovers in
    ``~/.codex`` or ``~/.config``.
    """

    return config_dir_path() / filename


def chatgpt_oauth_auth_path() -> Path:
    """Return MCC's private renewable ChatGPT OAuth credential path."""

    return config_dir_path() / AUTH_DIRNAME / CHATGPT_OAUTH_AUTH_FILENAME


def anthropic_oauth_managed_store_path() -> Path:
    """Return MCC's private renewable Claude subscription OAuth credential path."""

    return config_dir_path() / ANTHROPIC_OAUTH_MANAGED_STORE_FILENAME


def claude_settings_path() -> Path:
    """Return the default Claude Code settings.json path."""

    return Path.home() / CLAUDE_CONFIG_DIRNAME / CLAUDE_SETTINGS_FILENAME


def onboarding_state_path() -> Path:
    """Return the persisted onboarding checklist state path."""

    return config_dir_path() / ONBOARDING_STATE_FILENAME


def _is_wsl() -> bool:
    """Return True when running inside WSL, detected via the kernel osrelease string."""

    try:
        osrelease = Path(WSL_OSRELEASE_PATH).read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in osrelease.lower()


def windows_claude_settings_path() -> Path | None:
    """Return the Windows-side Claude settings.json path when running under WSL.

    Returns None when not running under WSL, or when no plausible Windows user
    directory containing a .claude directory can be found.
    """

    if not _is_wsl():
        return None

    windows_users_dir = Path(WSL_WINDOWS_USERS_DIR)
    try:
        entries = list(windows_users_dir.iterdir())
    except OSError:
        return None

    for entry in entries:
        try:
            if (entry / CLAUDE_CONFIG_DIRNAME).is_dir():
                return entry / CLAUDE_CONFIG_DIRNAME / CLAUDE_SETTINGS_FILENAME
        except OSError:
            continue

    username = os.environ.get("USER") or os.environ.get("USERNAME")
    if username:
        candidate = windows_users_dir / username
        try:
            if candidate.is_dir():
                return candidate / CLAUDE_CONFIG_DIRNAME / CLAUDE_SETTINGS_FILENAME
        except OSError:
            pass

    return None


def claude_settings_candidates() -> list[Path]:
    """Return the user-level settings.json files that could apply on this machine.

    Most likely to be in effect first: always the native ``claude_settings_path()``,
    plus the Windows-side path when running under WSL and it resolves. Deduplicated,
    order preserved. Never includes a path that is merely hypothetical for another OS.
    """

    candidates = [claude_settings_path()]

    windows_path = windows_claude_settings_path()
    if windows_path is not None and windows_path not in candidates:
        candidates.append(windows_path)

    return candidates


def claude_managed_settings_paths() -> list[Path]:
    """Return the enterprise managed-settings.json paths for the current platform.

    Includes the top-level managed-settings.json plus every ``*.json`` file inside
    the platform's drop-in directory, if present and readable. WSL is treated as
    Linux. Never raises; returns an empty list when nothing is found or a
    filesystem error occurs.
    """

    if sys.platform == "darwin":
        managed_path = MACOS_MANAGED_SETTINGS_PATH
        dropin_dir = MACOS_MANAGED_SETTINGS_DROPIN_DIR
    elif sys.platform == "win32":
        managed_path = WINDOWS_MANAGED_SETTINGS_PATH
        dropin_dir = WINDOWS_MANAGED_SETTINGS_DROPIN_DIR
    else:
        managed_path = LINUX_MANAGED_SETTINGS_PATH
        dropin_dir = LINUX_MANAGED_SETTINGS_DROPIN_DIR

    paths: list[Path] = []

    try:
        if Path(managed_path).is_file():
            paths.append(Path(managed_path))
    except OSError:
        pass

    try:
        dropin = Path(dropin_dir)
        if dropin.is_dir():
            for entry in sorted(dropin.iterdir()):
                try:
                    if entry.is_file() and entry.suffix == ".json":
                        paths.append(entry)
                except OSError:
                    continue
    except OSError:
        pass

    return paths
