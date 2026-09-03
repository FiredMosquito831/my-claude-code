"""Dotenv file discovery and explicit dotenv override helpers."""

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from loguru import logger

from .paths import managed_env_path

ANTHROPIC_AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"
# The explicit dotenv override. ``MCC_ENV_FILE`` is the canonical name; the
# pre-6.40.0 ``FCC_ENV_FILE`` is accepted as a working alias and logs one
# deprecation line per process when it is the one that is set.
DOTENV_FILE_ENV = "MCC_ENV_FILE"
LEGACY_DOTENV_FILE_ENV = "FCC_ENV_FILE"

_legacy_env_warnings: set[str] = set()


def _log_legacy_env_name_once(legacy_name: str, canonical_name: str) -> None:
    """Log one deprecation line per legacy env name, once per process."""

    if legacy_name in _legacy_env_warnings:
        return
    _legacy_env_warnings.add(legacy_name)
    logger.warning(
        "The {} environment variable is deprecated; use {} instead. It still "
        "works and will keep working, but the canonical name is {}.",
        legacy_name,
        canonical_name,
        canonical_name,
    )


def repo_env_path() -> Path:
    """Return the repo-local env path."""

    return Path(".env")


def explicit_env_path(env: Mapping[str, str] | None = None) -> Path | None:
    """Return the explicit ``MCC_ENV_FILE`` path, when configured.

    The pre-6.40.0 ``FCC_ENV_FILE`` is accepted as a working alias; when it is
    the name that is set, a single deprecation line is logged for the process.
    """

    source = env if env is not None else os.environ
    if explicit := source.get(DOTENV_FILE_ENV):
        return Path(explicit)
    if explicit := source.get(LEGACY_DOTENV_FILE_ENV):
        _log_legacy_env_name_once(LEGACY_DOTENV_FILE_ENV, DOTENV_FILE_ENV)
        return Path(explicit)
    return None


def settings_env_files(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Return Settings dotenv files in low-to-high precedence order."""

    files: list[Path] = [
        repo_env_path(),
        managed_env_path(),
    ]
    if explicit := explicit_env_path(env):
        files.append(explicit)
    return tuple(files)


class LazyEnvFiles(Sequence[Path]):
    """A sequence of dotenv paths resolved on first use, not at import time.

    ``SettingsConfigDict.env_file`` is evaluated while the ``Settings`` class
    body runs -- i.e. during the import of ``config.settings``. At that moment
    the module is partially initialised, and resolving the config directory
    would recurse back into it (the legacy-config health check builds a
    ``Settings`` from the legacy ``.env``). Wrapping the call in this sequence
    defers it until the first ``Settings`` is actually constructed, at which
    point ``config.settings`` is fully initialised and the config directory can
    be resolved. ``configured_env_files`` iterates it, so the deferral reaches
    every consumer.

    Inherits ``collections.abc.Sequence[Path]`` so the type checker accepts it
    for pydantic-settings' ``env_file: DotenvType`` (which is
    ``Path | Sequence[Path | str] | None``).
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = env

    def _resolve(self) -> tuple[Path, ...]:
        # Resolved fresh every time, not cached: the config directory can change
        # during a process lifetime (the legacy-config health check probes a
        # different directory than the one the process finally runs from), and
        # tests that redirect ``HOME`` between cases must see the new path.
        return settings_env_files(self._env)

    def __iter__(self):
        return iter(self._resolve())

    def __len__(self) -> int:
        return len(self._resolve())

    def __getitem__(self, index):
        return self._resolve()[index]

    def __bool__(self) -> bool:
        return bool(self._resolve())

    def __repr__(self) -> str:
        return f"LazyEnvFiles({self._resolve()!r})"


def configured_env_files(model_config: Mapping[str, Any]) -> tuple[Path, ...]:
    """Return the env files currently configured for a Settings model."""

    configured = model_config.get("env_file")
    if configured is None:
        return ()
    if isinstance(configured, (str, Path)):
        return (Path(configured),)
    return tuple(Path(item) for item in configured)


def env_file_value(path: Path, key: str) -> str | None:
    """Return a dotenv value when the file explicitly defines the key."""

    if not path.is_file():
        return None

    try:
        values = dotenv_values(path)
    except OSError:
        return None

    if key not in values:
        return None
    value = values[key]
    return "" if value is None else value


def env_file_override(model_config: Mapping[str, Any], key: str) -> str | None:
    """Return the last configured dotenv value that explicitly defines a key."""

    configured_value: str | None = None
    for env_file in configured_env_files(model_config):
        value = env_file_value(env_file, key)
        if value is not None:
            configured_value = value
    return configured_value


def process_env_key_is_effective(
    model_config: Mapping[str, Any],
    key: str,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return whether a key is coming from process env instead of configured dotenv."""

    source = env if env is not None else os.environ
    if env_file_override(model_config, key) is not None:
        return False
    return key in source
