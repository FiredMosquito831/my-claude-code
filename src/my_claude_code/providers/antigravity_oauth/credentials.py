"""MCC's own account and token store for Google Antigravity.

The shape is ChatGPT's — ``{version, tokens, accounts, accounts_version}`` —
because that store names its version explicitly and keeps the
"``tokens`` mirrors ``accounts[0]``" rule documented, and because
``providers/oauth_account_store.py`` already carries every record field the two
existing stores share. The platform's own quirks are kept as fields, never as
a composite string: the peers that do not own a store invent
``refreshToken|projectId|managedProjectId`` and then parse it everywhere
(``antigravity-claude-proxy oauth.js:29-48``); a project is a field here.

**P1 makes no token call.** :func:`refresh_tokens` exists, is classified
exactly like the two proven providers, and is exercised only against the fake
upstream in the test suite. Two rules are copied verbatim from
``anthropic_oauth/credentials.py:224-249`` and stay equal to
``chatgpt_oauth/credentials.py:92-102``:

* definitive only on ``400``/``401``/``403`` **with a parseable OAuth error
  body**;
* everything else — ``408``, ``429``, ``5xx``, a transport error, a non-JSON
  edge block — is transient and the credential is kept.

Retirement never deletes: with more than one account stored, the rejected
account alone is removed (written to ``<store>.dead-<epoch>``); otherwise the
whole store is renamed aside.

**Deliberately not in P1:** the refresh POST does not yet leave through the
provider's proxy chain. No production path can reach it in P1 (there is no way
to sign in), and naming the exit is part of the P2a credential path, where the
call first becomes reachable.
"""

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.config.paths import antigravity_oauth_store_path
from my_claude_code.providers.oauth_account_store import (
    ORIGIN_MCC,
    account_record_fields,
    backup_once,
    normalise_epoch_seconds,
    now_iso,
    synthetic_account_id,
)
from my_claude_code.providers.oauth_file_lock import (
    REFRESH_LOCK_TIMING,
    LockBusy,
    hold,
)

from .constants import (
    OAUTH_CLIENT_ID,
    OAUTH_CLIENT_SECRET,
    OAUTH_TOKEN_USER_AGENT,
    PROVIDER_ID,
    REFRESH_LEEWAY_SECONDS,
    TOKEN_URL,
)

#: ``version`` deliberately stays 1: the reader raises on a version it does not
#: know, so a bump would turn a downgrade into a hard failure instead of a
#: degradation to the primary account. ``accounts`` is an additive key an older
#: reader ignores, and ``tokens`` mirrors ``accounts[0]``.
MANAGED_CREDENTIAL_SCHEMA_VERSION = 1
ACCOUNTS_VERSION = 1
ACCOUNTS_VERSION_KEY = "accounts_version"
ACCOUNTS_KEY = "accounts"

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

#: The two existing OAuth providers' set, copied rather than re-derived;
#: ``tests/providers/test_oauth_refresh_parity.py`` pins all three together.
DEFINITIVE_REFRESH_STATUSES: frozenset[int] = frozenset({400, 401, 403})

TOKEN_TIMEOUT_SECONDS = 30.0


class AntigravityOAuthError(Exception):
    """Raised when Antigravity credential handling fails."""


class AntigravityRefreshError(AntigravityOAuthError):
    """A token-endpoint failure, classified.

    ``definitive`` is the only field the caller acts on: a definitive rejection
    may retire the credential (never delete it), a transient one must not.
    """

    definitive = False

    def __init__(
        self,
        status_code: int,
        message: str = "",
        *,
        response: httpx.Response | None = None,
    ) -> None:
        self.status_code = status_code
        self.response = response
        self.message = (
            message or f"Antigravity OAuth refresh failed (HTTP {status_code})"
        )
        super().__init__(self.message)


class AntigravityRefreshRejected(AntigravityRefreshError):
    """The credential itself is finished: re-signing in is the only answer."""

    definitive = True

    def __init__(
        self, status_code: int, *, response: httpx.Response | None = None
    ) -> None:
        super().__init__(
            status_code,
            "Google rejected this Antigravity credential "
            f"(HTTP {status_code}). Sign in again on the provider card.",
            response=response,
        )


class AntigravityRefreshUnavailable(AntigravityRefreshError):
    """The refresh could not be completed, but the credential is intact."""

    definitive = False

    def __init__(
        self,
        status_code: int,
        *,
        detail: str = "",
        response: httpx.Response | None = None,
    ) -> None:
        if status_code == 429:
            summary = (
                "Google is rate-limiting Antigravity token refreshes (HTTP 429). "
                "The stored credential was kept -- this is not a dead token and "
                "signing in again would rotate a working one away."
            )
        elif detail:
            summary = (
                f"Antigravity OAuth refresh could not be completed ({detail}). "
                "The stored credential was kept."
            )
        else:
            summary = (
                "Antigravity OAuth refresh could not be completed "
                f"(HTTP {status_code}). The stored credential was kept."
            )
        super().__init__(status_code, summary, response=response)


def _is_oauth_error_body(response: httpx.Response) -> bool:
    """Whether a response body is a parseable OAuth/API error document.

    The token endpoint answers a rejected grant with JSON — RFC 6749's
    ``{"error": "invalid_grant", ...}`` or Google's
    ``{"error": {"code": …, "message": …}}``. An edge block is a short
    non-JSON body; only the former is evidence about the *credential*.
    """

    try:
        payload = response.json()
    except ValueError, TypeError:
        return False
    return isinstance(payload, dict) and "error" in payload


def classify_refresh_failure(response: httpx.Response) -> AntigravityRefreshError:
    """Turn a non-2xx token-endpoint response into the right exception."""

    status = response.status_code
    if status in DEFINITIVE_REFRESH_STATUSES and _is_oauth_error_body(response):
        return AntigravityRefreshRejected(status, response=response)
    return AntigravityRefreshUnavailable(status, response=response)


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    """One Antigravity credential set, with the project it was provisioned on."""

    access_token: str
    refresh_token: str | None = None
    #: Epoch **seconds**. The store writes milliseconds nowhere; one unit is
    #: one rule.
    expires_at: int | None = None
    scopes: tuple[str, ...] = ()
    #: The ``cloudaicompanionProject`` ``loadCodeAssist`` returned, or the
    #: synthetic ``default-cli-project`` fallback. A field, never part of the
    #: refresh token.
    project_id: str = ""
    source: str = "unknown"

    @property
    def has_access_token(self) -> bool:
        return bool(self.access_token.strip())

    @property
    def has_refresh_token(self) -> bool:
        return bool(self.refresh_token and self.refresh_token.strip())

    def needs_refresh(self, *, now: float | None = None) -> bool:
        """Whether the credential is at or inside the refresh leeway."""

        if self.expires_at is None:
            return False
        stamp = time.time() if now is None else now
        return self.expires_at - REFRESH_LEEWAY_SECONDS <= stamp


@dataclass(frozen=True, slots=True)
class AccountRecord:
    """One stored account: its token block plus the shared record fields."""

    id: str
    tokens: OAuthTokens
    origin: str = ORIGIN_MCC
    origin_path: str = ""
    write_back: bool = False
    added_at: str = ""
    ordinal: int = 1


def managed_store_path() -> Path:
    """MCC's private Antigravity credential store."""

    return antigravity_oauth_store_path()


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def _token_document(tokens: OAuthTokens) -> dict[str, Any]:
    return {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at,
        "scopes": list(tokens.scopes),
        "project_id": tokens.project_id,
    }


def _tokens_from_document(document: Any, *, source: str = "mcc") -> OAuthTokens | None:
    if not isinstance(document, dict):
        return None
    access_token = document.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return None
    scopes = document.get("scopes")
    return OAuthTokens(
        access_token=access_token,
        refresh_token=(
            document.get("refresh_token")
            if isinstance(document.get("refresh_token"), str)
            else None
        ),
        expires_at=normalise_epoch_seconds(document.get("expires_at")),
        scopes=tuple(str(scope) for scope in scopes)
        if isinstance(scopes, list)
        else (),
        project_id=str(document.get("project_id") or ""),
        source=source,
    )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as error:
        # A store this build cannot read costs the operator a credential, never
        # a server (the ``load_accounts`` rule the Anthropic store states).
        logger.warning("Could not read the Antigravity credential store: {}", error)
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` atomically, owner-only, creating the directory private."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if os.name != "nt":
        os.chmod(path.parent, PRIVATE_DIR_MODE)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, PRIVATE_FILE_MODE)
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, PRIVATE_FILE_MODE)
    finally:
        temporary.unlink(missing_ok=True)


def _record_from_entry(entry: dict[Any, Any], *, index: int) -> AccountRecord | None:
    fields = account_record_fields(
        entry,
        origin_path_key="origin_path",
        write_back_key="write_back",
        added_at_key="added_at",
    )
    tokens = _tokens_from_document(entry.get("tokens"))
    if tokens is None:
        return None
    return AccountRecord(
        id=fields["id"] or synthetic_account_id(),
        tokens=tokens,
        origin=fields["origin"],
        origin_path=fields["origin_path"],
        write_back=fields["write_back"],
        added_at=fields["added_at"] or now_iso(),
        ordinal=fields["ordinal"] if fields["ordinal"] > 1 else index + 1,
    )


def _entry_from_record(record: AccountRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tokens": _token_document(record.tokens),
        "origin": record.origin,
        "origin_path": record.origin_path,
        "write_back": record.write_back,
        "added_at": record.added_at,
        "ordinal": record.ordinal,
    }


def load_accounts(
    *, path: Path | None = None, migrate: bool = True
) -> list[AccountRecord]:
    """Every stored Antigravity account. Never raises.

    The single-account migration is deliberately narrow: a document whose
    ``accounts`` key is already a non-empty list is the truth and is returned
    untouched, so the second read of a migrated file writes nothing. Only a
    legacy ``tokens``-only document is rewritten, and only after a
    ``.bak-<epoch>`` copy is taken.
    """

    store = path or managed_store_path()
    payload = _load_json(store)
    if payload.get("version") not in (None, MANAGED_CREDENTIAL_SCHEMA_VERSION):
        return []
    raw = payload.get(ACCOUNTS_KEY)
    if isinstance(raw, list) and raw:
        records: list[AccountRecord] = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                continue
            record = _record_from_entry(entry, index=index)
            if record is not None:
                records.append(record)
        if records:
            return records
    tokens = _tokens_from_document(payload.get("tokens"))
    if tokens is None:
        return []
    record = AccountRecord(
        id=synthetic_account_id(),
        tokens=tokens,
        added_at=now_iso(),
        ordinal=1,
    )
    if migrate:
        backup_once(store)
        save_accounts([record], path=store)
    return [record]


def save_accounts(
    records: Sequence[AccountRecord], *, path: Path | None = None
) -> Path:
    """Write the account list, mirroring the primary to ``tokens``."""

    store = path or managed_store_path()
    if not records:
        store.unlink(missing_ok=True)
        return store
    _atomic_write_private_json(
        store,
        {
            "version": MANAGED_CREDENTIAL_SCHEMA_VERSION,
            "tokens": _token_document(records[0].tokens),
            ACCOUNTS_VERSION_KEY: ACCOUNTS_VERSION,
            ACCOUNTS_KEY: [_entry_from_record(record) for record in records],
        },
    )
    return store


def add_or_update_account(
    tokens: OAuthTokens,
    *,
    origin: str = ORIGIN_MCC,
    origin_path: str = "",
    account_id: str | None = None,
    match: OAuthTokens | None = None,
    path: Path | None = None,
) -> AccountRecord:
    """Add an account, or update the one these tokens already belong to.

    A sign-in for an account already stored updates it in place and keeps its
    name, origin and ``added_at``. ``account_id`` names the record outright and
    ``match`` supplies the credential the tokens replace: a refresh rotates the
    refresh token, so matching a refreshed credential against the stored one by
    value would fail and silently append a duplicate. Exactly the Anthropic
    store's rule.
    """

    store = path or managed_store_path()
    records = list(load_accounts(path=store, migrate=False))
    index = _find_record(records, account_id=account_id, match=match, tokens=tokens)
    if index is None:
        record = AccountRecord(
            id=account_id or synthetic_account_id(),
            tokens=tokens,
            origin=origin,
            origin_path=origin_path,
            added_at=now_iso(),
            ordinal=max((record.ordinal for record in records), default=0) + 1,
        )
        records.append(record)
    else:
        previous = records[index]
        record = AccountRecord(
            id=previous.id,
            tokens=tokens,
            origin=previous.origin,
            origin_path=previous.origin_path,
            write_back=previous.write_back,
            added_at=previous.added_at,
            ordinal=previous.ordinal,
        )
        records[index] = record
    save_accounts(records, path=store)
    return record


def _find_record(
    records: Sequence[AccountRecord],
    *,
    account_id: str | None,
    match: OAuthTokens | None,
    tokens: OAuthTokens,
) -> int | None:
    if account_id:
        for index, record in enumerate(records):
            if record.id == account_id:
                return index
    replaced = match or tokens
    for index, record in enumerate(records):
        if record.tokens.refresh_token and (
            record.tokens.refresh_token == replaced.refresh_token
        ):
            return index
        if record.tokens.access_token == replaced.access_token:
            return index
    return None


def remove_account(
    account_id: str, *, path: Path | None = None
) -> AccountRecord | None:
    """Disconnect one account; the others keep serving.

    The removed record is written to ``<store>.dead-<epoch>`` rather than
    dropped — the same "renamed, never deleted" promise
    :func:`quarantine_managed_store` keeps for the whole store.
    """

    store = path or managed_store_path()
    records = list(load_accounts(path=store, migrate=False))
    remaining = [record for record in records if record.id != account_id]
    if len(remaining) == len(records):
        return None
    removed = next(record for record in records if record.id == account_id)
    target = store.with_name(f"{store.name}.dead-{int(time.time())}")
    try:
        _atomic_write_private_json(target, _entry_from_record(removed))
    except OSError as error:  # pragma: no cover - defensive
        logger.warning(
            "Could not set aside the disconnected Antigravity account: {}", error
        )
    save_accounts(remaining, path=store)
    logger.info(
        "Disconnected {} account {}; {} account(s) still stored.",
        PROVIDER_ID,
        account_id,
        len(remaining),
    )
    return removed


def quarantine_managed_store(*, now: float | None = None) -> Path | None:
    """Move a definitively-rejected store aside. Never deletes it."""

    store = managed_store_path()
    if not store.is_file():
        return None
    stamp = int(time.time() if now is None else now)
    target = store.with_name(f"{store.name}.dead-{stamp}")
    try:
        os.replace(store, target)
    except OSError as error:
        logger.warning(
            "Could not set aside the rejected Antigravity credential at {}: {}",
            store,
            error,
        )
        return None
    logger.warning(
        "Google definitively rejected the stored Antigravity credential; moved "
        "it to {} and the provider will read as not signed in until a new "
        "sign-in.",
        target.name,
    )
    return target


def load_tokens_for(
    account_id: str = "", *, path: Path | None = None
) -> OAuthTokens | None:
    """The stored credential for one account, or the primary when unnamed."""

    records = load_accounts(path=path, migrate=False)
    if not records:
        return None
    if account_id:
        for record in records:
            if record.id == account_id:
                return record.tokens
    return records[0].tokens


def credential_viability(tokens: OAuthTokens | None) -> tuple[bool, str]:
    """Whether a stored credential could authenticate a request right now.

    Presence and local expiry only — a refresh is never attempted here, because
    this is read from a page load. An expired token is still *viable*: the
    provider refreshes it on the next real request. This function answers the
    page's question, not the executor's.
    """

    if tokens is None or not tokens.has_access_token:
        return False, "no credential stored"
    return True, ""


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

#: One lock per (store, account), so two accounts never queue behind each
#: other's network round trip.
_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _refresh_lock(path: Path, account_id: str) -> asyncio.Lock:
    key = (str(path), account_id)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def native_refresh_lock_path(path: Path | None = None) -> Path:
    """The cross-process lock for one store's refreshes."""

    store = path or managed_store_path()
    return store.with_name(store.name + ".refresh" + ".lock")


async def _post_refresh(
    refresh_token: str, *, timeout: float = TOKEN_TIMEOUT_SECONDS
) -> httpx.Response:
    """The token POST, shaped exactly as the references document it.

    ``client_secret_post``: the pair goes in the form body, not a Basic header.
    The user-agent is the Go client's default on BOTH token calls (NQ2) — the
    whole profile claims to be that client.

    The installed-app client pair itself arrives with the sign-in surface
    (P2a), which is the first code that can spend it; P1 makes no token call in
    production, so the fields are omitted while the constants are empty and the
    body shape is otherwise the one the references document.
    """

    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if OAUTH_CLIENT_ID:
        form["client_id"] = OAUTH_CLIENT_ID
    if OAUTH_CLIENT_SECRET:
        form["client_secret"] = OAUTH_CLIENT_SECRET
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(
            TOKEN_URL,
            data=form,
            headers={"user-agent": OAUTH_TOKEN_USER_AGENT},
        )


def _tokens_from_refresh(payload: Any, *, previous: OAuthTokens) -> OAuthTokens:
    document = payload if isinstance(payload, dict) else {}
    access_token = document.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise AntigravityRefreshUnavailable(502, detail="no access_token in the answer")
    expires_in = document.get("expires_in")
    expires_at = (
        int(time.time() + expires_in)
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
        else previous.expires_at
    )
    scope = document.get("scope")
    return OAuthTokens(
        access_token=access_token,
        refresh_token=(
            document.get("refresh_token")
            if isinstance(document.get("refresh_token"), str)
            else previous.refresh_token
        ),
        expires_at=expires_at,
        scopes=(
            tuple(str(part) for part in scope.split())
            if isinstance(scope, str) and scope.strip()
            else previous.scopes
        ),
        # A refresh never changes the project: provisioning owns that, and P1
        # makes no provisioning call.
        project_id=previous.project_id,
        source=previous.source,
    )


async def refresh_tokens(
    tokens: OAuthTokens, *, account_id: str = "", path: Path | None = None
) -> OAuthTokens:
    """Exchange a refresh token for a fresh credential and store it.

    Single-flight per ``(store, account)`` with a double check inside the lock:
    a burst of requests that all noticed the same ageing token performs one
    exchange, and the caller that takes the lock second returns the credential
    the first one stored rather than spending the refresh token again.
    """

    store = path or managed_store_path()
    if not tokens.has_refresh_token:
        raise AntigravityRefreshRejected(400)
    assert tokens.refresh_token is not None

    async with _refresh_lock(store, account_id):
        try:
            async with hold(native_refresh_lock_path(store), REFRESH_LOCK_TIMING):
                stored = load_tokens_for(account_id, path=store)
                if (
                    stored is not None
                    and stored.has_access_token
                    and not stored.needs_refresh()
                    and stored.access_token != tokens.access_token
                ):
                    # Somebody else refreshed while this caller waited.
                    return stored
                response = await _post_refresh(tokens.refresh_token)
                if response.status_code >= 400:
                    failure = classify_refresh_failure(response)
                    logger.warning(
                        "Antigravity refresh failed: status={} definitive={}",
                        failure.status_code,
                        failure.definitive,
                    )
                    if failure.definitive:
                        # Only a definitive rejection may retire a credential,
                        # and never by deleting it. With more than one account
                        # stored, retire THAT account only.
                        if (
                            account_id
                            and len(load_accounts(path=store, migrate=False)) > 1
                        ):
                            remove_account(account_id, path=store)
                        else:
                            quarantine_managed_store()
                    raise failure
                refreshed = _tokens_from_refresh(response.json(), previous=tokens)
                add_or_update_account(
                    refreshed,
                    account_id=account_id or None,
                    match=tokens,
                    path=store,
                )
                logger.info(
                    "Refreshed Antigravity OAuth credential (expires_at={})",
                    refreshed.expires_at,
                )
                return refreshed
        except LockBusy as error:
            raise AntigravityRefreshUnavailable(
                503, detail="another MCC server is refreshing this credential"
            ) from error


__all__ = [
    "ACCOUNTS_KEY",
    "ACCOUNTS_VERSION",
    "DEFINITIVE_REFRESH_STATUSES",
    "AccountRecord",
    "AntigravityOAuthError",
    "AntigravityRefreshError",
    "AntigravityRefreshRejected",
    "AntigravityRefreshUnavailable",
    "OAuthTokens",
    "add_or_update_account",
    "classify_refresh_failure",
    "credential_viability",
    "load_accounts",
    "load_tokens_for",
    "managed_store_path",
    "quarantine_managed_store",
    "refresh_tokens",
    "remove_account",
    "save_accounts",
]
