"""Who owns an OAuth credential, and what MCC last decided about it.

Every OAuth account MCC serves is in one of two modes:

``shared``
    The credential was read from the official client's own file -- Claude
    Code's ``~/.claude/.credentials.json`` or Codex's ``~/.codex/auth.json`` --
    by an Import or by the automatic fallback. That file is the truth. MCC
    re-reads it whenever its ``(mtime_ns, size)`` moves, never refreshes the
    token early, and refreshes it only when it has expired and a real request
    needs it, under the client's own lock, writing the result back before it
    lets go.

``native``
    MCC signed the account in itself (loopback, paste, device or browser).
    MCC owns it and refreshes it as it always has, now single-flight across
    several MCC servers through a lock beside its own store.

This module holds what both providers share about that: the mode names, the
small state file (``<config dir>/oauth_ownership.json``) that carries the
known-dead refresh-token fingerprints, the migration marker and the last
decision per account, and :func:`record_decision`, which puts one stable code
in three places -- a ``server.log`` line with the request id, the account's
row on the card, and the triggering request's request-log row.

Never a token: the known-dead list holds ``sha256(token)[:16]`` only.
"""

import contextlib
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from loguru import logger

from my_claude_code.config.paths import anthropic_oauth_managed_store_path
from my_claude_code.core.credential_attribution import record_credential_event
from my_claude_code.core.request_tasks import current_entry

MODE_SHARED = "shared"
MODE_NATIVE = "native"

STATE_FILENAME = "oauth_ownership.json"
STATE_VERSION = 1

#: How many dead fingerprints a provider keeps. A bound so the file cannot
#: grow without limit; a fingerprint only matters until the file changes.
KNOWN_DEAD_LIMIT = 32

#: The codes the card colours. Everything else is neutral.
AMBER_CODES = frozenset({"shared:writeback-pending"})
RED_CODES = frozenset({"shared:sign-in-again", "shared:rejected"})


def fingerprint(token: str | None) -> str:
    """The ``sha256[:16]`` of a token -- never the token itself."""

    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:16]


def state_path() -> Path:
    """``oauth_ownership.json`` in the directory MCC keeps its stores in."""

    return anthropic_oauth_managed_store_path().parent / STATE_FILENAME


def _load_state() -> dict[str, Any]:
    try:
        with state_path().open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError, ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    path = state_path()
    state["version"] = STATE_VERSION
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.replace(temporary, path)
    except OSError as error:
        # The state is advisory except for the known-dead list, and even that
        # only saves a request that would be refused anyway. Never fatal.
        logger.warning("Could not write {}: {}", path, error)


def _section(state: dict[str, Any], key: str) -> dict[str, Any]:
    value = state.get(key)
    if not isinstance(value, dict):
        value = {}
        state[key] = value
    return value


# ---------------------------------------------------------------------------
# Known-dead refresh tokens (rule 10)
# ---------------------------------------------------------------------------


def is_known_dead(provider: str, refresh_token: str | None) -> bool:
    """Whether this refresh token was definitively rejected before."""

    if not refresh_token:
        return False
    dead = _load_state().get("knownDead", {})
    listed = dead.get(provider) if isinstance(dead, dict) else None
    return isinstance(listed, list) and fingerprint(refresh_token) in listed


def mark_known_dead(provider: str, refresh_token: str | None) -> None:
    """Remember a definitively rejected refresh token, by fingerprint only."""

    if not refresh_token:
        return
    state = _load_state()
    dead = _section(state, "knownDead")
    listed = dead.get(provider)
    listed = [str(item) for item in listed] if isinstance(listed, list) else []
    mark = fingerprint(refresh_token)
    if mark not in listed:
        listed.append(mark)
    dead[provider] = listed[-KNOWN_DEAD_LIMIT:]
    _save_state(state)


def clear_known_dead(provider: str, refresh_token: str | None) -> None:
    """Forget a fingerprint once the file it came from has changed."""

    if not refresh_token:
        return
    state = _load_state()
    dead = state.get("knownDead")
    if not isinstance(dead, dict):
        return
    listed = dead.get(provider)
    if not isinstance(listed, list):
        return
    mark = fingerprint(refresh_token)
    if mark not in listed:
        return
    dead[provider] = [item for item in listed if item != mark]
    _save_state(state)


# ---------------------------------------------------------------------------
# Decisions (rule 15)
# ---------------------------------------------------------------------------


def _current_request_id() -> str | None:
    with contextlib.suppress(Exception):
        entry = current_entry()
        if entry is not None:
            return entry.request_id
    return None


def record_decision(
    provider: str,
    slot: str,
    code: str,
    *,
    detail: str = "",
    persist: bool = True,
) -> None:
    """Emit one decision: server.log, the card, the request-log row.

    ``slot`` is the account id, or ``claude-code`` for the automatic fallback
    that has no record.
    """

    request_id = _current_request_id()
    logger.info(
        "OAuth credential decision: {} provider={} account={} request_id={}{}",
        code,
        provider,
        slot or "-",
        request_id or "-",
        f" ({detail})" if detail else "",
    )
    record_credential_event(code)
    if not persist:
        return
    state = _load_state()
    decisions = _section(state, "decisions")
    per_provider = decisions.get(provider)
    if not isinstance(per_provider, dict):
        per_provider = {}
        decisions[provider] = per_provider
    per_provider[slot or "-"] = {"code": code, "at": time.time()}
    _save_state(state)


def last_decision(provider: str, slot: str) -> tuple[str, float] | None:
    """The last decision recorded for one account, for the card."""

    decisions = _load_state().get("decisions")
    if not isinstance(decisions, dict):
        return None
    per_provider = decisions.get(provider)
    if not isinstance(per_provider, dict):
        return None
    entry = per_provider.get(slot or "-")
    if not isinstance(entry, dict):
        return None
    code = entry.get("code")
    at = entry.get("at")
    if not isinstance(code, str) or not isinstance(at, (int, float)):
        return None
    return code, float(at)


# ---------------------------------------------------------------------------
# The one-time migration marker (rule 14)
# ---------------------------------------------------------------------------


def migration_marker(provider: str) -> dict[str, Any] | None:
    """The marker the migration left, or ``None`` when it has not run."""

    migration = _load_state().get("migration")
    if not isinstance(migration, dict):
        return None
    entry = migration.get(provider)
    return entry if isinstance(entry, dict) else None


def mark_migration(provider: str, code: str) -> None:
    """Record that the migration ran, and what it decided."""

    state = _load_state()
    migration = _section(state, "migration")
    migration[provider] = {"code": code, "at": time.time()}
    _save_state(state)


# ---------------------------------------------------------------------------
# File stamps (rule 2)
# ---------------------------------------------------------------------------


def file_stamp(path: Path | None) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` of a file, or ``None`` when it cannot be stat-ed.

    Claude Code's own lazy rule: a running session re-reads its credential
    file only when the file's stamp moves.
    """

    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def read_only_reason(
    *,
    target: Path | None,
    write_back_on: bool,
    account_write_back: bool,
    platform: str,
) -> str | None:
    """Why a shared credential cannot be refreshed by MCC, or ``None``.

    ``write-back-off`` / ``macos`` / ``not-writable`` / ``no-file`` -- the
    suffixes of the ``shared:read-only:<reason>`` code (§2 S13, S14).
    """

    if platform == "darwin":
        return "macos"
    if not write_back_on or not account_write_back:
        return "write-back-off"
    if target is None or not target.is_file():
        return "no-file"
    if not os.access(target, os.W_OK) or not os.access(target.parent, os.W_OK):
        return "not-writable"
    return None
