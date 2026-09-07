"""Read and patch Claude Code's settings.json to point at the FCC proxy.

**The two undo modes.** Until 6.55.0 :func:`apply_proxy_env` overwrote
``ANTHROPIC_BASE_URL`` without recording what had been there and
:func:`clear_proxy_env` deleted it, so a user who had that variable pointed at
another gateway lost it on Configure and did not get it back on Undo. The only
copy was the one-shot ``.fcc-backup`` -- the whole file from before MCC's first
ever edit, which is worthless once anything else in it has changed since.

Configure now records the prior value of both variables through
``config/restore_record.py``, and Undo takes a mode:

``KEYS_ONLY``
    Remove MCC's two variables. What Undo has always done, and still the
    default, because it is the mode that cannot surprise anyone.

``RESTORE``
    Remove them and put back whatever they held before MCC first wrote. A
    restore into a settings file that has been rewritten since MCC configured
    it is refused rather than guessed at -- the recorded SHA-256 is what makes
    that check possible.

This is the same mechanism the desktop-app cards use, deliberately: the user
named this file's behaviour as the reference the desktop cards should match, so
the two share one implementation rather than resembling each other.
"""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from my_claude_code.config.atomic_json import write_json_document_atomically
from my_claude_code.config.paths import (
    claude_managed_settings_paths,
)
from my_claude_code.config.restore_record import (
    RestoreEntry,
    UndoMode,
    capture_overwritten,
    document_sha256,
    forget_entry,
    read_entry,
    write_entry,
)

#: The subject name this file's records are filed under, so the desktop cards
#: and Configure Claude Code can share one record without colliding.
CLAUDE_RESTORE_SUBJECT = "claude_code"

CLAUDE_BASE_URL_ENV = "ANTHROPIC_BASE_URL"
CLAUDE_AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"
# Kept ``fcc``-branded on purpose (see ``config.atomic_json``): this names a
# backup file that already exists on users' disks next to their
# ``~/.claude/settings.json``. Renaming it would orphan every backup taken
# before the change -- the one file whose whole job is to still be findable.
CLAUDE_SETTINGS_BACKUP_SUFFIX = ".fcc-backup"


class ClaudeSettingsError(Exception):
    """Raised when the Claude settings file cannot be read or written."""


@dataclass(frozen=True)
class ClaudeSettingsOverride:
    """A settings file that takes precedence over the one being configured.

    ``scope`` is currently always ``"managed"``: an enterprise
    managed-settings.json or drop-in fragment, which outranks every other
    settings file. It stays a string rather than a bool so project scope can be
    added if the UI ever learns which repository the user is in.
    ``variables`` lists which ``ANTHROPIC_*`` env keys the override sets, sorted.
    """

    path: str
    scope: str
    variables: list[str]


@dataclass(frozen=True)
class ClaudeSettingsStatus:
    """Snapshot of a Claude settings.json file relative to the expected FCC proxy env."""

    path: str
    exists: bool
    parsed: bool
    error: str | None
    state: str
    current_base_url: str | None
    base_url_matches: bool
    auth_token_present: bool
    auth_token_matches: bool
    expected_base_url: str
    overrides: list[ClaudeSettingsOverride]


def _load_document(path: Path) -> tuple[dict[str, object] | None, bool, str | None]:
    """Load a JSON object document, returning (data, parsed, error)."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, False, str(exc)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, False, str(exc)

    if not isinstance(data, dict):
        return None, False, "top-level JSON value is not an object"

    return data, True, None


def _env_block(data: dict[str, object]) -> dict[str, object]:
    """Return the document's env block, rejecting a present-but-wrong-shaped value.

    A non-object ``env`` is not something we can safely merge into, and silently
    replacing it would destroy whatever the user meant by it. Treat it like a
    parse failure so every caller refuses instead of clobbering.
    """

    env = data.get("env", {})
    if not isinstance(env, dict):
        raise ClaudeSettingsError('"env" is present but is not a JSON object')
    return {str(key): value for key, value in env.items()}


def _override_from_document(path: Path, *, scope: str) -> ClaudeSettingsOverride | None:
    """Return an override descriptor if the document sets ANTHROPIC_* env keys."""

    data, parsed, _error = _load_document(path)
    if not parsed or data is None:
        return None

    env = data.get("env")
    if not isinstance(env, dict):
        return None

    variables = sorted(
        name for name in (CLAUDE_BASE_URL_ENV, CLAUDE_AUTH_TOKEN_ENV) if name in env
    )
    if not variables:
        return None

    return ClaudeSettingsOverride(path=str(path), scope=scope, variables=variables)


def _detect_overrides() -> list[ClaudeSettingsOverride]:
    """Return settings files that take precedence over the user file, highest first.

    Covers the one precedence layer above a user-level settings.json that sits at
    a fixed, knowable location: managed/enterprise settings, including drop-in
    fragments. Detection is advisory only: every read/parse error is swallowed
    and the entry is simply omitted, never raised, and never affects
    ``ClaudeSettingsStatus.state``.

    Deliberately does NOT check a sibling ``settings.local.json``. Measured
    against Claude Code 2.1.223: with identical content in a controlled home
    directory, ``~/.claude/settings.json`` routed every request to the proxy
    while ``~/.claude/settings.local.json`` routed none. The ``local`` scope is
    repository-root only, so warning about a user-level one would be a warning
    that can never be true.

    Project-level ``.claude/settings.json`` and ``.claude/settings.local.json``
    do outrank this file, but the server has no way to know which repository the
    user is working in, so they are surfaced as a static note in the UI rather
    than by scanning the filesystem.
    """

    overrides: list[ClaudeSettingsOverride] = []

    for managed_path in claude_managed_settings_paths():
        override = _override_from_document(managed_path, scope="managed")
        if override is not None:
            overrides.append(override)

    return overrides


def read_status(
    *, path: Path, expected_base_url: str, expected_auth_token: str
) -> ClaudeSettingsStatus:
    """Return the current state of a Claude settings.json file relative to the FCC proxy."""

    path = path.absolute()

    if not path.exists():
        return ClaudeSettingsStatus(
            path=str(path),
            exists=False,
            parsed=True,
            error=None,
            state="unset",
            current_base_url=None,
            base_url_matches=False,
            auth_token_present=False,
            auth_token_matches=False,
            expected_base_url=expected_base_url,
            overrides=_detect_overrides(),
        )

    data, parsed, error = _load_document(path)
    if not parsed or data is None:
        return ClaudeSettingsStatus(
            path=str(path),
            exists=True,
            parsed=False,
            error=error,
            state="unreadable",
            current_base_url=None,
            base_url_matches=False,
            auth_token_present=False,
            auth_token_matches=False,
            expected_base_url=expected_base_url,
            overrides=_detect_overrides(),
        )

    try:
        env = _env_block(data)
    except ClaudeSettingsError as exc:
        return ClaudeSettingsStatus(
            path=str(path),
            exists=True,
            parsed=False,
            error=str(exc),
            state="unreadable",
            current_base_url=None,
            base_url_matches=False,
            auth_token_present=False,
            auth_token_matches=False,
            expected_base_url=expected_base_url,
            overrides=_detect_overrides(),
        )

    current_base_url = env.get(CLAUDE_BASE_URL_ENV)
    current_base_url = current_base_url if isinstance(current_base_url, str) else None
    base_url_matches = current_base_url == expected_base_url

    current_auth_token = env.get(CLAUDE_AUTH_TOKEN_ENV)
    auth_token_present = (
        isinstance(current_auth_token, str) and current_auth_token != ""
    )
    auth_token_matches = (
        isinstance(current_auth_token, str)
        and current_auth_token == expected_auth_token
    )

    base_url_key_present = CLAUDE_BASE_URL_ENV in env
    auth_token_key_present = CLAUDE_AUTH_TOKEN_ENV in env

    if base_url_matches and auth_token_matches:
        state = "configured"
    elif base_url_key_present or auth_token_key_present:
        state = "mismatch"
    else:
        state = "unset"

    return ClaudeSettingsStatus(
        path=str(path),
        exists=True,
        parsed=True,
        error=None,
        state=state,
        current_base_url=current_base_url,
        base_url_matches=base_url_matches,
        auth_token_present=auth_token_present,
        auth_token_matches=auth_token_matches,
        expected_base_url=expected_base_url,
        overrides=_detect_overrides(),
    )


def _backup_if_needed(path: Path) -> None:
    """Copy the existing settings file to its backup path, once."""

    backup_path = path.with_name(path.name + CLAUDE_SETTINGS_BACKUP_SUFFIX)
    if path.exists() and not backup_path.exists():
        shutil.copyfile(path, backup_path)


def apply_proxy_env(
    *,
    path: Path,
    base_url: str,
    auth_token: str,
    record_path: Path | None = None,
) -> ClaudeSettingsStatus:
    """Set ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN in the Claude settings file.

    Records what each variable held first, so :func:`clear_proxy_env` can offer
    to put it back. ``record_path`` overrides where that record is kept and
    exists for tests; production passes nothing and it lands in MCC's own
    configuration directory.
    """

    path = path.absolute()

    data: dict[str, object] = {}
    if path.exists():
        loaded, parsed, error = _load_document(path)
        if not parsed or loaded is None:
            raise ClaudeSettingsError(
                f"cannot parse Claude settings file {path}: {error}"
            )
        data = loaded

    env = dict(_env_block(data))

    # Captured before the write, on the document as it was read off disk,
    # which is the only moment the user's prior values still exist.
    overwritten = capture_overwritten(
        data,
        (("env", CLAUDE_BASE_URL_ENV), ("env", CLAUDE_AUTH_TOKEN_ENV)),
    )

    try:
        _backup_if_needed(path)

        env[CLAUDE_BASE_URL_ENV] = base_url
        env[CLAUDE_AUTH_TOKEN_ENV] = auth_token
        data["env"] = env

        write_json_document_atomically(path, data)
    except OSError as exc:
        raise ClaudeSettingsError(
            f"cannot write Claude settings file {path}: {exc}"
        ) from exc

    write_entry(
        RestoreEntry(
            subject=CLAUDE_RESTORE_SUBJECT,
            document_path=str(path),
            document_sha256=document_sha256(path),
            overwritten=overwritten,
        ),
        path=record_path,
    )

    return read_status(
        path=path, expected_base_url=base_url, expected_auth_token=auth_token
    )


def clear_proxy_env(
    *,
    path: Path,
    expected_base_url: str = "",
    expected_auth_token: str = "",
    mode: UndoMode = UndoMode.KEYS_ONLY,
    record_path: Path | None = None,
) -> ClaudeSettingsStatus:
    """Undo Configure Claude Code, in one of two modes.

    ``KEYS_ONLY`` removes MCC's two variables and leaves every other key in the
    file alone. ``RESTORE`` additionally puts back whatever those two variables
    held before MCC first wrote them, read from the side record -- and refuses
    rather than guessing when the file has been rewritten since, because at
    that point "the original value" is a claim about a document that no longer
    exists.

    The expected values do not affect what is removed; they are only carried into
    the returned status so a caller can keep rendering what a re-apply would write.
    """

    path = path.absolute()
    status_args = {
        "expected_base_url": expected_base_url,
        "expected_auth_token": expected_auth_token,
    }

    if not path.exists():
        return read_status(path=path, **status_args)

    data, parsed, error = _load_document(path)
    if not parsed or data is None:
        raise ClaudeSettingsError(f"cannot parse Claude settings file {path}: {error}")

    env = _env_block(data)

    restore: dict[str, object] = {}
    if mode is UndoMode.RESTORE:
        entry = read_entry(CLAUDE_RESTORE_SUBJECT, path=record_path)
        if entry is None:
            raise ClaudeSettingsError(
                "MCC has no record of what these variables held before it "
                "configured this file, so there is nothing to restore. Undo "
                "can still remove MCC's keys."
            )
        current = document_sha256(path)
        if entry.document_sha256 and current and entry.document_sha256 != current:
            raise ClaudeSettingsError(
                f"{path} has changed since MCC configured it, so restoring the "
                "pre-MCC values would overwrite an edit made since. Remove "
                "MCC's keys only, or restore from "
                f"{path.name}{CLAUDE_SETTINGS_BACKUP_SUFFIX} by hand."
            )
        for value in entry.overwritten:
            if value.prior_present and len(value.key_path) == 2:
                restore[value.key_path[1]] = value.prior_value

    if CLAUDE_BASE_URL_ENV not in env and CLAUDE_AUTH_TOKEN_ENV not in env:
        forget_entry(CLAUDE_RESTORE_SUBJECT, path=record_path)
        return read_status(path=path, **status_args)

    try:
        _backup_if_needed(path)

        env = dict(env)
        env.pop(CLAUDE_BASE_URL_ENV, None)
        env.pop(CLAUDE_AUTH_TOKEN_ENV, None)
        env.update(restore)
        if env:
            data["env"] = env
        else:
            data.pop("env", None)

        write_json_document_atomically(path, data)
    except OSError as exc:
        raise ClaudeSettingsError(
            f"cannot write Claude settings file {path}: {exc}"
        ) from exc

    forget_entry(CLAUDE_RESTORE_SUBJECT, path=record_path)
    return read_status(path=path, **status_args)
