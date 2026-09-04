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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.config.paths import (
    anthropic_oauth_managed_store_path,
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


def _tokens_from_payload(payload: dict[str, Any], *, source: str) -> OAuthTokens | None:
    access = payload.get("accessToken") or payload.get("access_token")
    if not isinstance(access, str) or not access.strip():
        return None
    refresh = payload.get("refreshToken") or payload.get("refresh_token")
    subscription = payload.get("subscriptionType") or payload.get("subscription_type")
    tier = payload.get("rateLimitTier") or payload.get("rate_limit_tier")
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


def store_tokens(tokens: OAuthTokens) -> None:
    """Persist a credential into MCC's own store."""
    _atomic_write_private_json(
        managed_store_path(),
        {
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
        },
    )


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
    return refreshed


# One lock per credential *file*, not per provider instance. A hot reload
# builds a second provider while the first is still alive; two instance-local
# locks let both refresh at once, and the loser's write clobbers the winner's
# with a refresh token Anthropic has already rotated away. Keyed by the
# resolved store path, so a test pointing at a tmp_path gets its own.
_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


def _refresh_lock() -> asyncio.Lock:
    key = str(managed_store_path())
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


async def refresh_tokens(tokens: OAuthTokens) -> OAuthTokens:
    """Exchange a refresh token for a fresh credential and store it.

    The result is always written to MCC's own store, never back into Claude
    Code's file: rotating the token there would invalidate the copy the user's
    real client is holding.

    Single-flight per credential file, and double-checked inside the lock. A
    burst of concurrent requests that all noticed the same ageing token
    performs one exchange, and whichever of them takes the lock second finds a
    fresh credential already stored and returns that rather than spending the
    refresh token a second time.
    """
    if not tokens.has_refresh_token:
        raise AnthropicOAuthRefreshRejected(400)
    assert tokens.refresh_token is not None

    async with _refresh_lock():
        stored = load_managed_tokens()
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
                # one MCC owns -- Claude Code's file is never touched.
                quarantine_managed_store()
            raise failure

        refreshed = _tokens_from_refresh(response.json(), previous=tokens)
        # Always into MCC's own store, whatever the credential was read from:
        # a token refreshed off Claude Code's file must not be written back
        # into it. This is also what upgrades a pre-6.36.0 store to the current
        # shape (millisecond ``expiresAt``, ``refreshTokenExpiresAt``,
        # ``rateLimitTier``) on the first successful refresh.
        store_tokens(refreshed)

    logger.info(
        "Refreshed Claude subscription OAuth credential (source={} expires_at={})",
        tokens.source,
        refreshed.expires_at,
    )
    return refreshed
