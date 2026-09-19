"""Load, store and refresh Claude subscription OAuth credentials.

Two sources, in precedence order:

1. **MCC's own store** (``~/.mcc/anthropic_oauth.json``, in whichever
   directory ``resolve_config_dir`` answered with), written by
   ``mcc-anthropic-oauth-login``. Preferred **while it is viable**, because MCC
   may refresh it without touching state Claude Code owns.
2. **Claude Code's own credential file** (``~/.claude/.credentials.json``,
   ``claudeAiOauth`` object), used whenever the managed store is not viable.

Reading source 2 is deliberately read-only and never refreshed in place: that
file belongs to Claude Code, a refresh rotates the token, and racing its owner
would log the user out of their real client. When a token read from there is
close to expiry, MCC refreshes into *its own* store and leaves the original
alone.

Selection is **viability-based**, not existence-based
-----------------------------------------------------

Before 6.43.0 the managed store won on ``has_access_token`` alone, so a file
holding a token that expired days ago permanently masked a perfectly good
Claude Code credential sitting next to it, and the provider served nothing for
the life of that file. :func:`load_tokens` now asks whether a candidate can
actually be used -- not expired, or expired but holding a refresh token that is
not itself past its stated expiry -- and falls through when it cannot. It says
so once, in the log, naming the source it picked and why.

A refresh failure is not automatically a dead credential
--------------------------------------------------------

Anthropic's token endpoint rate-limits refresh attempts and answers ``429``.
Treating that as "your credential is dead, sign in again" destroys a working
refresh token on the operator's own advice. Only a *definitive* rejection --
``400``/``401``/``403`` carrying a parseable OAuth error body -- retires a
credential (:class:`AnthropicOAuthRefreshRejected`). Everything else, including
a bare ``403`` from the edge that never reached the OAuth handler, is transient
(:class:`AnthropicOAuthRefreshUnavailable`) and the credential is kept.

See ``docs/ANTHROPIC-SUBSCRIPTION.md`` for the policy position on using these
credentials at all.
"""

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.config.paths import (
    anthropic_oauth_managed_store_path,
)
from my_claude_code.providers.oauth_account_store import (
    ORIGIN_CLAUDE_CODE,
    ORIGIN_CODEX,
    ORIGIN_MCC,
    STORAGE_WRITE_LOCK_NAME,
    OAuthStorageLockUnavailable,
    account_record_fields,
    backup_once,
    monotonic_write_allowed,
    normalise_epoch_seconds,
    now_iso,
    storage_write_lock,
    synthetic_account_id,
)
from my_claude_code.providers.oauth_names import (
    forget_account_name,
    move_name,
    seed_default_name,
)

from .constants import (
    CLAUDE_CODE_CLIENT_ID,
    CLAUDE_CODE_USER_AGENT,
    LEGACY_TOKEN_URL,
    OAUTH_REFRESH_SCOPES,
    REFRESH_LEEWAY_SECONDS,
    TOKEN_URL,
)

CLAUDE_CREDENTIALS_DIRNAME = ".claude"
CLAUDE_CREDENTIALS_FILENAME = ".credentials.json"
CLAUDE_CONFIG_FILENAME = ".claude.json"
CLAUDE_OAUTH_KEY = "claudeAiOauth"


class AnthropicOAuthRefreshError(RuntimeError):
    """Base: Anthropic did not complete a token refresh.

    Carries the status code, and never the response body. A token endpoint's
    body can echo the credential just presented to it, and this exception's
    text reaches logs, the request log and HTTP error responses alike.

    ``response`` is the raw :class:`httpx.Response` when there was one. It is
    *not* rendered into the message; it is here so the shared failure policy
    can read a published ``Retry-After`` off it exactly as it does for any
    other provider (``providers/failure_policy.retry_after_from_error``).

    Two subclasses carry the only distinction that matters to a caller, and
    code should catch those rather than this base:

    * :class:`AnthropicOAuthRefreshRejected` -- definitive. The credential is
      finished; quarantining it and telling the operator to sign in again is
      correct.
    * :class:`AnthropicOAuthRefreshUnavailable` -- transient. The credential is
      fine; the endpoint could not answer right now.
    """

    #: Whether this failure means the credential itself is finished.
    definitive: bool = False

    #: This exception's ``str()`` contains no secret and no response body, so
    #: it may be shown to an operator verbatim. Read by
    #: ``providers/runtime/validation.py`` -- a marker attribute rather than an
    #: import, because ``providers.runtime`` has no business importing a
    #: specific provider.
    safe_message = True

    def __init__(
        self,
        status_code: int,
        message: str | None = None,
        *,
        response: httpx.Response | None = None,
    ) -> None:
        self.status_code = status_code
        self.response = response
        super().__init__(
            message or f"Anthropic OAuth refresh failed with HTTP {status_code}."
        )


class AnthropicOAuthRefreshRejected(AnthropicOAuthRefreshError):
    """The token endpoint definitively rejected the refresh token.

    ``400``/``401``/``403`` *carrying a parseable OAuth error body*. The body
    shape is load-bearing: the edge in front of the token endpoint answers a
    bare non-JSON ``403`` for reasons that have nothing to do with the grant
    (an unrecognised ``User-Agent``, for one -- proved live for 6.43.0), and
    retiring a working credential on that would be the same bug this class
    exists to prevent, one layer down.
    """

    definitive = True

    def __init__(
        self,
        status_code: int,
        *,
        response: httpx.Response | None = None,
    ) -> None:
        super().__init__(
            status_code,
            f"Anthropic rejected the refresh token (HTTP {status_code}). "
            "The stored credential has been set aside; sign in again with "
            "`mcc-anthropic-oauth-login`, or import your Claude Code "
            "credential from the dashboard.",
            response=response,
        )


class AnthropicOAuthRefreshUnavailable(AnthropicOAuthRefreshError):
    """The refresh could not be completed, but the credential is intact.

    ``408``/``429``/``5xx``, a transport error, an unparseable success body,
    and any ``4xx`` whose body is not an OAuth error. The credential is kept
    and the failure is handed to the shared retry ladder and provider-health
    machinery exactly as an API-key provider's ``429``/``5xx`` would be -- see
    ``AnthropicOAuthProvider._provider_failure_override``.
    """

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
                "Anthropic is rate-limiting token refreshes (HTTP 429). The "
                "stored credential was kept -- this is not a dead token and "
                "signing in again would rotate a working one away."
            )
        elif detail:
            summary = (
                f"Anthropic OAuth refresh could not be completed ({detail}). "
                "The stored credential was kept."
            )
        else:
            summary = (
                f"Anthropic OAuth refresh could not be completed (HTTP "
                f"{status_code}). The stored credential was kept."
            )
        super().__init__(status_code, summary, response=response)


#: Statuses that *may* be definitive, if the body agrees. Mirrors
#: ``chatgpt_oauth/credentials.py``'s ``{400, 401, 403}`` so the two OAuth
#: providers cannot drift apart; ``tests/providers/test_oauth_refresh_parity.py``
#: pins them together.
DEFINITIVE_REFRESH_STATUSES: frozenset[int] = frozenset({400, 401, 403})


def _is_oauth_error_body(response: httpx.Response) -> bool:
    """Whether a response body is a parseable OAuth/API error document.

    The token endpoint answers a rejected grant with JSON -- either RFC 6749's
    ``{"error": "invalid_grant", ...}`` or Anthropic's
    ``{"error": {"type": ..., "message": ...}}``. An edge block is a short
    non-JSON body. Only the former is evidence about the *credential*.
    """
    try:
        payload = response.json()
    except ValueError, TypeError:
        return False
    return isinstance(payload, dict) and "error" in payload


def classify_refresh_failure(
    response: httpx.Response,
) -> AnthropicOAuthRefreshError:
    """Turn a non-2xx token-endpoint response into the right exception."""
    status = response.status_code
    if status in DEFINITIVE_REFRESH_STATUSES and _is_oauth_error_body(response):
        return AnthropicOAuthRefreshRejected(status, response=response)
    return AnthropicOAuthRefreshUnavailable(status, response=response)


class AnthropicOAuthUnavailableError(RuntimeError):
    """Raised when no subscription credential can be found at all."""

    #: See :class:`AnthropicOAuthRefreshError`.
    safe_message = True


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    """One Claude subscription OAuth credential set."""

    access_token: str
    refresh_token: str | None = None
    expires_at: int | None = None
    scopes: tuple[str, ...] = ()
    subscription_type: str | None = None
    # Both of these sit on Claude Code's own credential file and were parsed
    # away by every MCC release before 6.36.0. ``refreshTokenExpiresAt`` is the
    # difference between "a refresh will fix this" and "you have to sign in
    # again", and ``rateLimitTier`` is the plan detail the dashboard reports.
    refresh_token_expires_at: int | None = None
    rate_limit_tier: str | None = None
    # The ``account`` object Anthropic's token response has always carried and
    # every MCC release before 7.30.0 parsed away. Claude Code 2.1.278 maps it
    # straight through (``formatTokens``, bundle offsets 214580619 and
    # 198914238) and persists it to ``~/.claude.json`` ``oauthAccount``; it
    # arrives on the exchange *and* on every refresh at **zero extra upstream
    # cost**, which is the difference between "Claude account 2" and the user's
    # real address. Optional, because ``formatTokens`` guards it with
    # ``e.account?``: a refresh that omits it must never blank one we hold.
    account_uuid: str | None = None
    account_email: str | None = None
    organization_name: str | None = None
    # Where this came from, for diagnostics. Never contains a secret.
    source: str = "unknown"

    @property
    def has_access_token(self) -> bool:
        return bool(self.access_token.strip())

    @property
    def has_refresh_token(self) -> bool:
        return bool(self.refresh_token and self.refresh_token.strip())

    def seconds_remaining(self, *, now: float | None = None) -> float | None:
        """Seconds until expiry, or ``None`` when the token reports none."""
        if self.expires_at is None:
            return None
        return self.expires_at - (time.time() if now is None else now)

    def needs_refresh(self, *, now: float | None = None) -> bool:
        remaining = self.seconds_remaining(now=now)
        if remaining is None:
            return False
        return remaining <= REFRESH_LEEWAY_SECONDS

    def is_expired(self, *, now: float | None = None) -> bool:
        """Whether the access token is past its stated expiry.

        Distinct from :meth:`needs_refresh`: a token inside the leeway window
        still works, so its refresh can happen in the background, while an
        expired one has to be replaced before the request goes out.
        """
        remaining = self.seconds_remaining(now=now)
        return remaining is not None and remaining <= 0

    def refresh_token_seconds_remaining(
        self, *, now: float | None = None
    ) -> float | None:
        """Seconds until the *refresh* token expires, or ``None``."""
        if self.refresh_token_expires_at is None:
            return None
        return self.refresh_token_expires_at - (time.time() if now is None else now)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _home() -> Path:
    return Path.home()


def managed_store_path() -> Path:
    """Where MCC keeps the credential it owns and may refresh."""
    return anthropic_oauth_managed_store_path()


def claude_credentials_path() -> Path:
    """Claude Code's own credential file.

    Honours ``CLAUDE_CONFIG_DIR``, which Claude Code documents as relocating
    ``.credentials.json`` on Linux and Windows.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if override:
        return Path(override) / CLAUDE_CREDENTIALS_FILENAME
    return _home() / CLAUDE_CREDENTIALS_DIRNAME / CLAUDE_CREDENTIALS_FILENAME


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError, ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _expiry_seconds(payload: dict[str, Any]) -> int | None:
    """Normalise Anthropic's millisecond ``expiresAt`` to epoch seconds."""
    for key in ("expiresAt", "expires_at"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # Claude Code stores milliseconds; the token endpoint returns
            # seconds. Anything past year ~2286 in seconds is really millis.
            return int(value / 1000) if value > 10_000_000_000 else int(value)
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        return int(time.time() + expires_in)
    return None


def _scopes(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("scopes") or payload.get("scope")
    if isinstance(raw, str):
        return tuple(part for part in raw.split() if part)
    if isinstance(raw, list):
        return tuple(str(part) for part in raw if str(part).strip())
    return ()


def _timestamp_seconds(payload: dict[str, Any], *keys: str) -> int | None:
    """Read one epoch timestamp, in whichever unit it happened to be written."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value / 1000) if value > 10_000_000_000 else int(value)
    return None


def _text(value: object) -> str | None:
    """A non-empty string, or ``None``. Used for every identity field."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _identity_from_payload(
    payload: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """Read ``account.uuid`` / ``account.email_address`` / the org name.

    Parsed **defensively**. The key names are read out of Claude Code's own
    mapping, not out of a captured response body -- no token call was made to
    write this -- so ``uuid`` or ``id`` and ``email_address`` or ``email`` are
    both accepted, and an absent object is simply an absent identity rather
    than an error. The flat ``accountUuid`` / ``accountEmail`` spellings are
    what :func:`store_tokens` writes, so a stored credential round-trips.
    """
    account = payload.get("account")
    organization = payload.get("organization")
    uuid = _text(payload.get("accountUuid")) or _text(payload.get("account_uuid"))
    email = _text(payload.get("accountEmail")) or _text(payload.get("account_email"))
    org_name = _text(payload.get("organizationName")) or _text(
        payload.get("organization_name")
    )
    if isinstance(account, dict):
        uuid = _text(account.get("uuid")) or _text(account.get("id")) or uuid
        email = (
            _text(account.get("email_address")) or _text(account.get("email")) or email
        )
    if isinstance(organization, dict):
        org_name = _text(organization.get("name")) or org_name
    return uuid, email, org_name


def _tokens_from_payload(payload: dict[str, Any], *, source: str) -> OAuthTokens | None:
    access = payload.get("accessToken") or payload.get("access_token")
    if not isinstance(access, str) or not access.strip():
        return None
    refresh = payload.get("refreshToken") or payload.get("refresh_token")
    subscription = payload.get("subscriptionType") or payload.get("subscription_type")
    tier = payload.get("rateLimitTier") or payload.get("rate_limit_tier")
    account_uuid, account_email, organization_name = _identity_from_payload(payload)
    return OAuthTokens(
        access_token=access.strip(),
        refresh_token=refresh.strip() if isinstance(refresh, str) else None,
        expires_at=_expiry_seconds(payload),
        scopes=_scopes(payload),
        subscription_type=subscription if isinstance(subscription, str) else None,
        refresh_token_expires_at=_timestamp_seconds(
            payload, "refreshTokenExpiresAt", "refresh_token_expires_at"
        ),
        rate_limit_tier=tier if isinstance(tier, str) else None,
        account_uuid=account_uuid,
        account_email=account_email,
        organization_name=organization_name,
        source=source,
    )


def load_managed_tokens() -> OAuthTokens | None:
    """Read the credential MCC owns, if one has been stored."""
    return _tokens_from_payload(_load_json(managed_store_path()), source="mcc")


def load_claude_code_tokens() -> OAuthTokens | None:
    """Read Claude Code's own credential file, without modifying it."""
    payload = _load_json(claude_credentials_path())
    oauth = payload.get(CLAUDE_OAUTH_KEY)
    if not isinstance(oauth, dict):
        return None
    return _tokens_from_payload(oauth, source="claude-code")


def detect_available_sources() -> dict[str, bool]:
    """Report which credential sources exist, without reading any secret.

    The admin UI uses this to offer "use the credentials already on this
    machine" versus "sign in", so it must never surface a token value.
    """
    return {
        "mcc": load_managed_tokens() is not None,
        "claude_code": load_claude_code_tokens() is not None,
    }


def credential_viability(tokens: OAuthTokens | None) -> tuple[bool, str]:
    """Whether a candidate can serve a request, and why not when it cannot.

    Purely local: this never makes a network call. A credential is viable when
    it has an access token and either

    * that access token has not expired, or
    * it has expired but a refresh token is present that is not itself past a
      stated ``refreshTokenExpiresAt``.

    A store with no ``refreshTokenExpiresAt`` (everything MCC wrote before
    6.36.0) is treated as *possibly* renewable rather than dead: the file does
    not say, and the only way to find out is to try. That is safe now, because
    a refusal is classified before it retires anything.
    """
    if tokens is None:
        return False, "absent"
    if not tokens.has_access_token:
        return False, "no access token"
    if not tokens.is_expired():
        return True, "access token still valid"
    if not tokens.has_refresh_token:
        return False, "access token expired and no refresh token"
    refresh_remaining = tokens.refresh_token_seconds_remaining()
    if refresh_remaining is not None and refresh_remaining <= 0:
        return False, "refresh token expired"
    return True, "access token expired but renewable"


def load_tokens() -> OAuthTokens:
    """Return the credential to use, preferring MCC's own store *while viable*.

    Existence is not viability. Before 6.43.0 this returned the first file
    holding a non-empty access token, so a managed store whose tokens had both
    expired masked a healthy ``~/.claude`` credential permanently, and the
    provider served nothing for the life of that file.
    """
    candidates = (
        ("mcc", load_managed_tokens),
        ("claude-code", load_claude_code_tokens),
    )
    rejected: list[str] = []
    for name, loader in candidates:
        tokens = loader()
        viable, reason = credential_viability(tokens)
        if viable and tokens is not None:
            if rejected:
                # The one line that answers "why is it using that one?" in
                # server.log without anybody having to reproduce anything.
                logger.warning(
                    "Claude subscription credential: using {} ({}); skipped {}",
                    name,
                    reason,
                    "; ".join(rejected),
                )
            else:
                logger.debug(
                    "Claude subscription credential: using {} ({})", name, reason
                )
            return tokens
        rejected.append(f"{name} ({reason})")
    raise AnthropicOAuthUnavailableError(
        "No usable Claude subscription credential found ("
        + "; ".join(rejected)
        + "). Either sign in with `mcc-anthropic-oauth-login`, or log in to "
        f"Claude Code so that {claude_credentials_path()} exists."
    )


def quarantine_managed_store(*, now: float | None = None) -> Path | None:
    """Move a definitively-rejected managed store aside. Never deletes it.

    ``chatgpt_oauth`` unlinks its dead credential; this renames instead, to
    ``anthropic_oauth.json.dead-<epoch>``. Same unblocking effect -- the next
    :func:`load_tokens` cannot see it, so the Claude Code credential is reached
    -- and the evidence survives for whoever investigates why a credential died.

    Returns the new path, or ``None`` when there was nothing to move.
    """
    path = managed_store_path()
    if not path.is_file():
        return None
    stamp = int(time.time() if now is None else now)
    target = path.with_name(f"{path.name}.dead-{stamp}")
    try:
        os.replace(path, target)
    except OSError as error:
        logger.warning(
            "Could not set aside the rejected Claude subscription credential at {}: {}",
            path,
            error,
        )
        return None
    logger.warning(
        "Anthropic definitively rejected the stored Claude subscription "
        "credential; moved it to {} and will fall back to any other source.",
        target.name,
    )
    return target


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON 0600, atomically, so a token is never world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)
    # Windows inherits the profile directory's ACL; chmod is a no-op there.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _token_document(tokens: OAuthTokens) -> dict[str, Any]:
    """The seven legacy keys plus the three identity keys, as written."""
    return {
        "accessToken": tokens.access_token,
        "refreshToken": tokens.refresh_token,
        # Milliseconds, matching Claude Code's own file. MCC used to write
        # seconds under a key Claude Code writes as milliseconds; the
        # reader handled both, but the file was a trap for anything else
        # that ever opened it.
        "expiresAt": (
            None if tokens.expires_at is None else int(tokens.expires_at) * 1000
        ),
        "scopes": list(tokens.scopes),
        "subscriptionType": tokens.subscription_type,
        "refreshTokenExpiresAt": (
            None
            if tokens.refresh_token_expires_at is None
            else int(tokens.refresh_token_expires_at) * 1000
        ),
        "rateLimitTier": tokens.rate_limit_tier,
        # Additive, and ignored by every parser that shipped before 7.30.0.
        "accountUuid": tokens.account_uuid,
        "accountEmail": tokens.account_email,
        "organizationName": tokens.organization_name,
    }


def store_tokens(tokens: OAuthTokens) -> None:
    """Persist a credential into MCC's own store.

    Kept as the one-line entry point every caller already had. It is now a
    thin wrapper over :func:`add_or_update_account`, so a refresh written
    through it updates *that account's* record and re-mirrors the primary
    rather than replacing the document and dropping the other accounts.
    """
    add_or_update_account(tokens, origin=ORIGIN_MCC)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

#: Bumping this would be a breaking change for the *reader*, which is why it
#: is not the top-level ``version`` of anything: the seven legacy keys stay
#: mirrored at the top level precisely so a build that has never heard of
#: ``accountsVersion`` still finds a credential.
ACCOUNTS_VERSION = 1
ACCOUNTS_VERSION_KEY = "accountsVersion"
ACCOUNTS_KEY = "accounts"

#: What an account with nothing to name it is called.
PROVIDER_LABEL = "Claude"
PROVIDER_ID = "anthropic_oauth"


@dataclass(frozen=True, slots=True)
class AccountRecord:
    """One stored Claude subscription account."""

    id: str
    tokens: OAuthTokens
    origin: str = ORIGIN_MCC
    origin_path: str = ""
    write_back: bool = False
    added_at: str = ""
    ordinal: int = 1

    @property
    def owns_a_source_file(self) -> bool:
        return self.origin == ORIGIN_CLAUDE_CODE and bool(self.origin_path)


def _account_from_entry(entry: dict[Any, Any], *, index: int) -> AccountRecord | None:
    tokens = _tokens_from_payload(entry, source="mcc")
    if tokens is None:
        return None
    fields = account_record_fields(entry)
    account_id = fields["id"] or tokens.account_uuid or synthetic_account_id()
    return AccountRecord(
        id=account_id,
        tokens=tokens,
        origin=fields["origin"],
        origin_path=fields["origin_path"],
        write_back=fields["write_back"],
        added_at=fields["added_at"] or now_iso(),
        ordinal=fields["ordinal"] if fields["ordinal"] > 1 else index + 1,
    )


def _entry_from_account(record: AccountRecord) -> dict[str, Any]:
    entry = _token_document(record.tokens)
    entry.update(
        {
            "id": record.id,
            "origin": record.origin,
            "originPath": record.origin_path,
            "writeBack": record.write_back,
            "addedAt": record.added_at,
            "ordinal": record.ordinal,
        }
    )
    return entry


def load_accounts(*, migrate: bool = True) -> list[AccountRecord]:
    """Every stored account, migrating the legacy single-account shape once.

    Never raises. An unreadable store is an empty list, because a store this
    build cannot parse must cost the operator a *credential*, not a server.

    The migration is the whole downgrade story and it is deliberately narrow:
    a document whose ``accounts`` key is already a non-empty list is the truth
    and is returned untouched, so the second read of a migrated file writes
    nothing. Only a document holding the legacy top-level shape is rewritten,
    and only after a ``.bak-<epoch>`` copy is taken.
    """
    path = managed_store_path()
    document = _load_json(path)
    raw = document.get(ACCOUNTS_KEY)
    if isinstance(raw, list) and raw:
        records = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                continue
            record = _account_from_entry(entry, index=index)
            if record is not None:
                records.append(record)
        if records:
            return records
    legacy = _tokens_from_payload(document, source="mcc")
    if legacy is None:
        return []
    record = AccountRecord(
        id=legacy.account_uuid or synthetic_account_id(),
        tokens=legacy,
        # Everything written before 7.30.0 was written by MCC into MCC's own
        # store, whatever it was originally copied from: provenance was never
        # persisted (there was no field for it), so claiming an origin now
        # would be inventing one. ``mcc`` is the honest answer, and it is also
        # the safe one -- it means write-back leaves the file alone until the
        # user re-imports and says otherwise.
        origin=ORIGIN_MCC,
        write_back=False,
        added_at=now_iso(),
        ordinal=1,
    )
    if migrate:
        backup_once(path)
        save_accounts([record])
    return [record]


def save_accounts(records: Sequence[AccountRecord]) -> None:
    """Write the account list, mirroring the primary to the legacy keys.

    The mirror is not a convenience: it is the only reason a 7.29.x build can
    still read this file. ``_tokens_from_payload`` as shipped reads top-level
    keys only, so the seven of them beside ``accounts`` are what a downgrade
    finds. It is read-safe, not write-safe -- an older build's own write
    replaces the whole document and drops the list -- which is what the
    ``.bak-<epoch>`` and the CHANGELOG note are for.
    """
    path = managed_store_path()
    if not records:
        # Nothing left to serve. Keep the promise that a store is renamed and
        # never silently emptied: the caller that removed the last account has
        # already written it aside.
        with contextlib.suppress(OSError):
            path.unlink()
        return
    document = _token_document(records[0].tokens)
    document[ACCOUNTS_VERSION_KEY] = ACCOUNTS_VERSION
    document[ACCOUNTS_KEY] = [_entry_from_account(record) for record in records]
    _atomic_write_private_json(path, document)


def _match_index(records: Sequence[AccountRecord], tokens: OAuthTokens) -> int | None:
    """Which stored account these tokens belong to, or ``None`` for a new one.

    The account id is the match. When the tokens carry no identity -- an
    ``account`` object Anthropic did not send, on a store whose id is
    synthetic -- the refresh token is the only other thing that ties a
    credential to the record it came from, so it is the fallback. It is a
    *fallback*: the refresh token rotates, so it can only ever match the
    credential as last stored, which is exactly the case it is there for.
    """
    if tokens.account_uuid:
        for index, record in enumerate(records):
            if record.id == tokens.account_uuid:
                return index
            if record.tokens.account_uuid == tokens.account_uuid:
                return index
        return None
    if tokens.has_refresh_token:
        for index, record in enumerate(records):
            if record.tokens.refresh_token == tokens.refresh_token:
                return index
    if tokens.access_token:
        for index, record in enumerate(records):
            if record.tokens.access_token == tokens.access_token:
                return index
    return None


def add_or_update_account(
    tokens: OAuthTokens,
    *,
    origin: str = ORIGIN_MCC,
    origin_path: str | None = None,
    write_back: bool | None = None,
    default_email: str | None = None,
    account_id: str | None = None,
    match: OAuthTokens | None = None,
) -> AccountRecord:
    """Add an account, or update the one these tokens already belong to.

    Signing a **second** account in appends. Signing in an account that is
    already stored updates it in place -- tokens, expiry, plan, tier -- and
    keeps its name, origin, origin path, write-back flag and ``addedAt``,
    because none of those are things a fresh sign-in learned anything about.
    Nothing is ever duplicated and no account ever replaces a different one.

    ``account_id`` names the record outright, and ``match`` supplies the
    credential the tokens *replace*. The refresh path passes both: a refresh
    rotates the refresh token, so matching a refreshed credential against the
    stored one by token value would fail and silently append a duplicate of
    the account that had just been refreshed.
    """
    records = list(load_accounts())
    index: int | None = None
    if account_id:
        index = next(
            (i for i, record in enumerate(records) if record.id == account_id), None
        )
    if index is None and match is not None:
        index = _match_index(records, match)
    if index is None:
        index = _match_index(records, tokens)
    if index is None:
        ordinal = max((record.ordinal for record in records), default=0) + 1
        record = AccountRecord(
            id=tokens.account_uuid or synthetic_account_id(),
            tokens=tokens,
            origin=origin,
            origin_path=origin_path or "",
            write_back=(
                write_back
                if write_back is not None
                else origin in (ORIGIN_CLAUDE_CODE, ORIGIN_CODEX)
            ),
            added_at=now_iso(),
            ordinal=ordinal,
        )
        records.append(record)
    else:
        previous = records[index]
        new_id = previous.id
        if tokens.account_uuid and previous.id != tokens.account_uuid:
            # The id upgrade: an imported or synthetic account whose first
            # refresh brought the real ``account.uuid`` back. Move the name
            # with it, so the operator's name follows the account.
            new_id = tokens.account_uuid
            move_name(PROVIDER_ID, previous.id, new_id)
        record = AccountRecord(
            id=new_id,
            tokens=tokens,
            origin=previous.origin,
            origin_path=previous.origin_path,
            write_back=previous.write_back,
            added_at=previous.added_at,
            ordinal=previous.ordinal,
        )
        records[index] = record
    save_accounts(records)
    seed_default_name(
        PROVIDER_ID,
        record.id,
        email=tokens.account_email or default_email,
        provider_label=PROVIDER_LABEL,
        ordinal=record.ordinal,
    )
    return record


def remove_account(account_id: str) -> AccountRecord | None:
    """Disconnect one account. The others keep serving.

    The removed record is written to ``anthropic_oauth.json.dead-<epoch>``
    rather than dropped, which is the same "renamed, never deleted" promise
    :func:`quarantine_managed_store` has kept for the whole store since 6.43.0
    -- one file per retired account.
    """
    records = list(load_accounts())
    remaining = [record for record in records if record.id != account_id]
    if len(remaining) == len(records):
        return None
    removed = next(record for record in records if record.id == account_id)
    path = managed_store_path()
    target = path.with_name(f"{path.name}.dead-{int(time.time())}")
    try:
        _atomic_write_private_json(target, _entry_from_account(removed))
    except OSError as error:  # pragma: no cover - defensive
        logger.warning("Could not set aside the disconnected account: {}", error)
    save_accounts(remaining)
    forget_account_name(PROVIDER_ID, account_id)
    logger.info(
        "Disconnected Claude subscription account {}; {} account(s) still stored.",
        account_id,
        len(remaining),
    )
    return removed


def load_tokens_for(account_id: str) -> OAuthTokens | None:
    """The credential of one account, by id. ``None`` when it is gone."""
    for record in load_accounts():
        if record.id == account_id:
            return record.tokens
    return None


def account_for(account_id: str) -> AccountRecord | None:
    """One account's whole record, by id."""
    for record in load_accounts():
        if record.id == account_id:
            return record
    return None


# ---------------------------------------------------------------------------
# Claude Code's own identity block, read-only
# ---------------------------------------------------------------------------


WRITE_BACK_ENV = "ANTHROPIC_OAUTH_WRITE_BACK"


def write_back_enabled() -> bool:
    """Whether write-back is on. Default on; one dashboard switch turns it off.

    Read through ``Settings`` rather than ``os.environ``, because the
    dashboard writes ``.env`` and ``pydantic-settings`` reads that file
    *without* exporting it into the process environment -- a bare
    ``os.environ`` read would silently ignore the switch the operator just
    flipped. The environment is still honoured, because it is what
    ``Settings`` checks first.
    """
    try:
        from my_claude_code.config.settings import get_settings

        return bool(getattr(get_settings(), "anthropic_oauth_write_back", True))
    except Exception:  # pragma: no cover - a settings problem is not a refusal
        raw = os.environ.get(WRITE_BACK_ENV, "").strip().lower()
        return raw not in ("0", "false", "no", "off")


def write_back_if_owned(record: AccountRecord, refreshed: OAuthTokens) -> bool:
    """Write a refreshed token back into the file the account came from.

    Returns whether the file was written. Every "no" is a *correct* no, and
    each one is a separate case the card has to be able to explain:

    * the account is one MCC signed in itself -- there is no source file to
      own, and writing one would be inventing a claim on somebody else's file;
    * ``ANTHROPIC_OAUTH_WRITE_BACK`` is off, or this account's ``writeBack``
      flag is;
    * the file is not there. On macOS Claude Code keeps the credential in the
      login keychain, and a Windows install may be using the ``windows-credman``
      backend instead; in both cases there is no file and write-back is a
      documented no-op rather than a success that did not happen;
    * Claude Code's own ``.storage-write`` lock could not be acquired inside
      its retry budget -- a lock MCC cannot take is a skipped write, never a
      forced one;
    * the target already holds a token at least as new as ours. That is the
      monotonicity guard (C8), and it is the case write-back exists for: the
      user's real client refreshed in the meantime, its token is the live one,
      and overwriting it with ours would log them out of their own client.

    Everything else on the target file is preserved. This machine's
    ``.credentials.json`` carries eight ``mcpOAuth`` entries beside
    ``claudeAiOauth``; replacing the document rather than the one key would
    silently disconnect every one of them.
    """
    if not record.owns_a_source_file or not record.write_back:
        return False
    if not write_back_enabled():
        return False
    target = Path(record.origin_path)
    if not target.is_file():
        logger.debug(
            "Claude subscription write-back skipped for account {}: {} is not a "
            "file (macOS keychain or windows-credman install).",
            record.id,
            target,
        )
        return False
    try:
        with storage_write_lock(target.parent):
            return _write_back_locked(record, refreshed, target)
    except OAuthStorageLockUnavailable:
        logger.warning(
            "Claude subscription write-back skipped for account {}: could not "
            "take Claude Code's {} lock; its owner is mid-write.",
            record.id,
            STORAGE_WRITE_LOCK_NAME,
        )
        return False


def _write_back_locked(
    record: AccountRecord, refreshed: OAuthTokens, target: Path
) -> bool:
    """The write itself, with the target re-read inside the lock."""
    document = _load_json(target)
    existing = document.get(CLAUDE_OAUTH_KEY)
    existing = existing if isinstance(existing, dict) else {}
    if not monotonic_write_allowed(
        target_expires_at=normalise_epoch_seconds(existing.get("expiresAt")),
        target_refresh_expires_at=normalise_epoch_seconds(
            existing.get("refreshTokenExpiresAt")
        ),
        ours_expires_at=refreshed.expires_at,
        ours_refresh_expires_at=refreshed.refresh_token_expires_at,
    ):
        logger.info(
            "Claude subscription write-back skipped for account {}: {} already "
            "holds a token at least as new as ours.",
            record.id,
            target.name,
        )
        return False
    backup_once(target)
    # Replace the one key, keep the rest of the document exactly as found --
    # including every key this build has never heard of.
    block = dict(existing)
    block.update(
        {
            "accessToken": refreshed.access_token,
            "refreshToken": refreshed.refresh_token,
            "expiresAt": (
                None
                if refreshed.expires_at is None
                else int(refreshed.expires_at) * 1000
            ),
            "scopes": list(refreshed.scopes),
            "subscriptionType": refreshed.subscription_type,
            "refreshTokenExpiresAt": (
                None
                if refreshed.refresh_token_expires_at is None
                else int(refreshed.refresh_token_expires_at) * 1000
            ),
            "rateLimitTier": refreshed.rate_limit_tier,
        }
    )
    document[CLAUDE_OAUTH_KEY] = block
    _atomic_write_private_json(target, document)
    logger.info(
        "Wrote the refreshed Claude subscription token back to {} for account {}.",
        target.name,
        record.id,
    )
    return True


def claude_config_path() -> Path:
    """``~/.claude.json`` -- Claude Code's settings file, never written here."""
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if override:
        return Path(override) / CLAUDE_CONFIG_FILENAME
    return _home() / CLAUDE_CONFIG_FILENAME


def claude_code_oauth_account() -> dict[str, str]:
    """The ``oauthAccount`` block of ``~/.claude.json``, or ``{}``.

    Read **only** at import, and only to give an imported account a name
    before its first refresh can bring the real ``account`` object back.
    Claude Code's *credential* file carries no identity at all (measured), so
    without this an imported account is called "Claude account 2" until it
    happens to refresh. Strictly read-only: MCC never writes this file.
    """
    block = _load_json(claude_config_path()).get("oauthAccount")
    if not isinstance(block, dict):
        return {}
    return {
        key: str(value)
        for key, value in block.items()
        if isinstance(value, str) and value.strip()
    }


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def _refresh_payload(refresh_token: str) -> dict[str, str]:
    """The refresh body Claude Code 2.1.260 sends (``$U``, offset 182768825).

    ``scope`` is the field MCC omitted before 6.43.0. Claude Code always sends
    it, defaulting to ``p8`` -- the authorize scope set minus the
    authorize-only ``org:create_api_key``.
    """
    return {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLAUDE_CODE_CLIENT_ID,
        "scope": OAUTH_REFRESH_SCOPES,
    }


def token_endpoint_headers() -> dict[str, str]:
    """The headers MCC sends to the token endpoint, on both grants.

    Claude Code sets only ``Content-Type`` explicitly; its HTTP client supplies
    the ``User-Agent``. Sending no plausible ``User-Agent`` is answered by a
    non-JSON ``403`` at the edge, so MCC sends the same Claude Code identity it
    already presents on ``/v1/messages``. ``anthropic-beta`` is not sent:
    neither ``$U`` nor ``EAn`` carries it. See ``constants.py`` for the full
    derivation and the live evidence.
    """
    return {
        "Content-Type": "application/json",
        "User-Agent": CLAUDE_CODE_USER_AGENT,
    }


def _tokens_from_refresh(
    payload: dict[str, Any],
    *,
    previous: OAuthTokens,
) -> OAuthTokens:
    refreshed = _tokens_from_payload(payload, source="mcc")
    if refreshed is None:
        # A 200 with no access token in it is the endpoint misbehaving, not the
        # credential being dead: keep it and let the ladder retry.
        raise AnthropicOAuthRefreshUnavailable(
            200, detail="the response carried no access token"
        )
    # Anthropic may omit the refresh token on a successful refresh; keeping the
    # previous one is what stops the credential becoming unrenewable. The same
    # is true of every field a refresh response does not restate: dropping the
    # plan or the refresh-token expiry would blank the dashboard card on the
    # first refresh.
    if not refreshed.has_refresh_token:
        refreshed = replace(refreshed, refresh_token=previous.refresh_token)
    if refreshed.refresh_token_expires_at is None:
        refreshed = replace(
            refreshed, refresh_token_expires_at=previous.refresh_token_expires_at
        )
    if refreshed.rate_limit_tier is None:
        refreshed = replace(refreshed, rate_limit_tier=previous.rate_limit_tier)
    if not refreshed.scopes:
        refreshed = replace(refreshed, scopes=previous.scopes)
    if refreshed.subscription_type is None:
        refreshed = replace(refreshed, subscription_type=previous.subscription_type)
    # The same rule, one field wider. ``formatTokens`` guards the account
    # object with ``e.account?``, so a refresh may legitimately omit it --
    # and blanking an id would detach the account's name from the account,
    # which is the exact failure the naming store exists to prevent.
    if refreshed.account_uuid is None:
        refreshed = replace(refreshed, account_uuid=previous.account_uuid)
    if refreshed.account_email is None:
        refreshed = replace(refreshed, account_email=previous.account_email)
    if refreshed.organization_name is None:
        refreshed = replace(refreshed, organization_name=previous.organization_name)
    return refreshed


# One lock per credential *file*, not per provider instance. A hot reload
# builds a second provider while the first is still alive; two instance-local
# locks let both refresh at once, and the loser's write clobbers the winner's
# with a refresh token Anthropic has already rotated away. Keyed by the
# resolved store path, so a test pointing at a tmp_path gets its own.
#
# Keyed on ``(store path, account id)`` since 7.30.0: two accounts in one file
# have two refresh tokens and must be able to refresh at the same time, while
# two provider instances holding the *same* account must not.
_REFRESH_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _refresh_lock(account_id: str = "") -> asyncio.Lock:
    key = (str(managed_store_path()), account_id)
    lock = _REFRESH_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _REFRESH_LOCKS[key] = lock
    return lock


async def _post_refresh(refresh_token: str) -> httpx.Response:
    """POST the refresh, falling back to the pre-2.1.258 token host.

    Claude Code 2.1.258 moved the token endpoint to ``platform.claude.com``
    (offset 181433527). Nothing in-tree proves the old host stopped answering
    or that the new one answers for this client id, so a 404/301/308 from the
    current host retries the legacy one exactly once rather than turning a
    host migration into a forced re-login.
    """
    headers = token_endpoint_headers()
    payload = _refresh_payload(refresh_token)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(TOKEN_URL, json=payload, headers=headers)
            if (
                response.status_code in (301, 308, 404)
                and LEGACY_TOKEN_URL != TOKEN_URL
            ):
                logger.warning(
                    "Anthropic token endpoint {} answered {}; retrying the "
                    "pre-2.1.258 host once.",
                    TOKEN_URL,
                    response.status_code,
                )
                response = await client.post(
                    LEGACY_TOKEN_URL, json=payload, headers=headers
                )
            return response
    except httpx.HTTPError as error:
        # A transport failure reaching the *token* endpoint is not a failure of
        # the inference request, and it is certainly not a dead credential.
        # Wrapping it here is what keeps the two apart downstream.
        raise AnthropicOAuthRefreshUnavailable(
            503, detail=f"{type(error).__name__} reaching the token endpoint"
        ) from error


async def refresh_tokens(tokens: OAuthTokens, *, account_id: str = "") -> OAuthTokens:
    """Exchange a refresh token for a fresh credential and store it.

    The result is always written to MCC's own store. It is written **back** to
    the file the account was imported from only when that account's persisted
    origin says MCC does not own it, write-back is on, and the monotonicity
    guard in :func:`write_back_if_owned` says the write moves time forwards --
    never for an account MCC signed in itself, which has no source file.

    Single-flight per ``(credential file, account id)``, and double-checked
    inside the lock. A burst of concurrent requests that all noticed the same
    ageing token performs one exchange, and whichever of them takes the lock
    second finds a fresh credential already stored and returns that rather
    than spending the refresh token a second time. Two *different* accounts
    never wait on each other.
    """
    if not tokens.has_refresh_token:
        raise AnthropicOAuthRefreshRejected(400)
    assert tokens.refresh_token is not None

    async with _refresh_lock(account_id):
        stored = load_tokens_for(account_id) if account_id else load_managed_tokens()
        if (
            stored is not None
            and stored.has_access_token
            and not stored.needs_refresh()
            and stored.access_token != tokens.access_token
        ):
            # Somebody else already did this while this caller waited.
            return stored

        response = await _post_refresh(tokens.refresh_token)
        if response.status_code >= 400:
            failure = classify_refresh_failure(response)
            logger.warning(
                "Claude subscription refresh failed: status={} definitive={} source={}",
                failure.status_code,
                failure.definitive,
                tokens.source,
            )
            if failure.definitive and tokens.source == "mcc":
                # Only a definitive rejection may retire a store, and only the
                # one MCC owns -- Claude Code's file is never touched. With
                # more than one account stored, retire **that account only**:
                # the others are unaffected by this one's rejection and must
                # keep serving.
                if account_id and len(load_accounts()) > 1:
                    remove_account(account_id)
                else:
                    quarantine_managed_store()
            raise failure

        refreshed = _tokens_from_refresh(response.json(), previous=tokens)
        # Always into MCC's own store, whatever the credential was read from.
        # This is also what upgrades a pre-6.36.0 store to the current shape
        # (millisecond ``expiresAt``, ``refreshTokenExpiresAt``,
        # ``rateLimitTier``) on the first successful refresh.
        record = add_or_update_account(
            refreshed,
            origin=ORIGIN_MCC,
            account_id=account_id or None,
            match=tokens,
        )
        # ...and, for an account MCC does *not* own, back to the file it came
        # from. Inside this account's refresh lock, so no other MCC caller is
        # mid-refresh on the same credential while the target is re-read.
        write_back_if_owned(record, refreshed)

    logger.info(
        "Refreshed Claude subscription OAuth credential (source={} expires_at={})",
        tokens.source,
        refreshed.expires_at,
    )
    return refreshed
