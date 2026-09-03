"""Shared filesystem paths for Free Claude Code configuration."""

import os
import sys
from pathlib import Path

FCC_CONFIG_DIRNAME = ".fcc"
FCC_ENV_FILENAME = ".env"
LEGACY_REPO_DIRNAME = "free-claude-code"
LEGACY_XDG_CONFIG_DIRNAME = ".config"
MESSAGING_STATE_DIRNAME = "agent_workspace"
FCC_LOGS_DIRNAME = "logs"
SERVER_LOG_FILENAME = "server.log"
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


def config_dir_path() -> Path:
    """Return the default user config directory."""

    return Path.home() / FCC_CONFIG_DIRNAME


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
    """Return legacy user env paths that can be migrated to ~/.fcc/.env."""

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

    Always under ``~/.fcc``, never inside the CLI's own configuration
    directory: a file MCC writes is a file MCC must be able to remove, and a
    user who stops using a harness should not be left with MCC's leftovers in
    ``~/.codex`` or ``~/.config``.
    """

    return config_dir_path() / filename


def chatgpt_oauth_auth_path() -> Path:
    """Return FCC's private renewable ChatGPT OAuth credential path."""

    return config_dir_path() / AUTH_DIRNAME / CHATGPT_OAUTH_AUTH_FILENAME


def anthropic_oauth_managed_store_path() -> Path:
    """Return FCC's private renewable Claude subscription OAuth credential path."""

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
