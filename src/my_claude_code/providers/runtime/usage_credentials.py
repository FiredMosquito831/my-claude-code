"""Resolve the credential one declared usage read goes out with (8.5.0).

The only place in this feature that touches a secret, and the reason the
adapters under :mod:`my_claude_code.core.usage` can be pure functions of bytes:
they never see one.

**The rule (§6.1.1), and how it is kept here rather than promised:**

1. A read uses a credential **MCC already holds, from the source it already
   reads** -- the accessor below calls the provider's own loader, never
   ``.env`` of its own, never a file it owns, never a copy.
2. It is read **at request time**, for that one request. Nothing is cached
   here; the store holds readings and never a token.
3. It is **never logged, returned in a payload, or put in an exception
   message**. :class:`UsageCredential` marks its token ``repr=False`` and every
   error path below names a *kind*, never a value.
4. **No cookie, no browser session, no management key, ever.** A provider whose
   read would need one has no row in the declared table at all -- see
   ``config/usage_sources.py`` for the ones that were deliberately left out.

**What a refusal looks like.** A provider with no usable credential answers
state 4 *not sent* with a sentence naming what to do, rather than raising into
a 500: "no credential" is a fact the page can print, and a traceback is not.

The two OAuth rows are read from their account stores directly
(``load_managed_tokens`` / ``load_claude_code_tokens`` / ``load_chatgpt_accounts``)
rather than through the refresh machinery. That is deliberate and is the whole
point of the no-charge proof: a usage read may not spend a shared refresh token
inference needs, so it uses the token the store holds if that token is not
expired, and reports "sign in again" if it is. A convenient side effect is that
a read cannot rotate a credential either.
"""

import time
from collections.abc import Callable

from my_claude_code.config.credentials import mask_key_label
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings
from my_claude_code.config.usage_sources import (
    CREDENTIAL_API_KEY,
    CREDENTIAL_OAUTH_STORE,
    UsageEndpoint,
)
from my_claude_code.core.usage.reading import UsageCredential

from .config import provider_credential

__all__ = ["CredentialRefusal", "resolve_credential"]


class CredentialRefusal(Exception):
    """No credential, or not one this read may use. Carries a sentence, never a value."""


#: One resolver per declared OAuth account store, keyed by the value a table row
#: puts in ``UsageEndpoint.oauth_provider``. A declared lookup rather than an
#: ``if`` chain: a third subscription becomes a row in this map plus its own
#: loader, and the code that decides has one shape instead of three.
_OAUTH_RESOLVERS: dict[str, Callable[[], UsageCredential]] = {
    "anthropic_oauth": lambda: _anthropic_credential(),
    "chatgpt_oauth": lambda: _chatgpt_credential(),
}


def resolve_credential(endpoint: UsageEndpoint, settings: Settings) -> UsageCredential:
    """The credential for one declared read, or a refusal naming what to do."""

    if endpoint.credential == CREDENTIAL_API_KEY:
        return _api_key_credential(endpoint, settings)
    if endpoint.credential == CREDENTIAL_OAUTH_STORE:
        loader = _OAUTH_RESOLVERS.get(endpoint.oauth_provider)
        if loader is not None:
            return loader()
    raise CredentialRefusal(
        "MCC has no way to read a credential of this kind for this provider"
    )


def _api_key_credential(endpoint: UsageEndpoint, settings: Settings) -> UsageCredential:
    """The key the provider's inference path already uses, from the same accessor.

    ``provider_credential`` is the existing reader the executor itself calls,
    so a read cannot disagree with a request about which key is in play. The
    descriptor is found **by the variable this row declares**, because env
    names are not derivable from provider ids (V.3: ``OPENROUTER_API_KEY``
    against ``open_router``), and searching the catalogue for it is one loop
    over 59 rows rather than a second table that could drift.

    The **first** key of a pool is used. A pool's other keys are rotated for
    inference by the rotation engine; a usage read is about the account, and
    picking a different key per press would make a card's figures jump between
    keys that answer differently. It is a choice, so it is stated.
    """

    descriptor = None
    for candidate in PROVIDER_CATALOG.values():
        if candidate.credential_env == endpoint.credential_env:
            descriptor = candidate
            break
    if descriptor is None:
        raise CredentialRefusal(
            f"{endpoint.credential_env} does not belong to a provider MCC knows"
        )
    try:
        raw = provider_credential(descriptor, settings)
    except Exception:  # a settings read must never become a 500
        raw = ""
    keys = tuple(part.strip() for part in str(raw).split(",") if part.strip())
    if not keys:
        raise CredentialRefusal(
            f"{endpoint.credential_env} is not set. Add it on the Providers page "
            "and usage can be read for this provider."
        )
    return UsageCredential(token=keys[0], label=mask_key_label(keys[0]))


def _anthropic_credential() -> UsageCredential:
    """The stored Claude subscription token, if one is present and not expired.

    Read from the same two sources the provider reads and in the same
    precedence -- MCC's own store first, then Claude Code's file -- but
    **without** the refresh path: this read may not spend a shared refresh
    token inference needs, and it must not rotate one either. An expired token
    is therefore a refusal that says to re-authorise, which is what §5.4 asks
    the 401/403 case to say.
    """

    from my_claude_code.providers.anthropic_oauth import credentials

    candidates = (
        ("MCC's Claude subscription store", credentials.load_managed_tokens),
        ("Claude Code's credential file", credentials.load_claude_code_tokens),
    )
    expired = False
    for label, loader in candidates:
        try:
            tokens = loader()
        except Exception:  # a store that cannot be read is not a page failure
            continue
        if tokens is None or not tokens.has_access_token:
            continue
        if tokens.is_expired():
            expired = True
            continue
        return UsageCredential(token=tokens.access_token, label=label)
    if expired:
        raise CredentialRefusal(
            "the stored Claude subscription credential has expired. Re-authorise "
            "it on the Providers page and usage can be read again."
        )
    raise CredentialRefusal(
        "no Claude subscription credential is stored. Sign in on the Providers "
        "page and usage can be read."
    )


def _chatgpt_credential() -> UsageCredential:
    """The stored ChatGPT token plus the account id the store already knows.

    ``account_id`` is the ``ChatGPT-Account-ID`` header's value -- the store's
    own field, and the same one the inference request sends (``_build_headers``
    in the provider). The token is the one the store holds; an expired one is a
    refusal rather than a refresh, for the same reason as above.
    """

    from my_claude_code.providers.chatgpt_oauth import credentials

    try:
        records = credentials.load_chatgpt_accounts(migrate=False)
    except Exception:  # a store that cannot be read is not a page failure
        records = []
    expired = False
    for record in records:
        token = record.tokens.get("access_token")
        if not isinstance(token, str) or not token.strip():
            continue
        expires_at = record.tokens.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at <= time.time():
            expired = True
            continue
        return UsageCredential(
            token=token.strip(),
            account_id=str(record.tokens.get("account_id") or record.id or ""),
            label=record.id,
        )
    if expired:
        raise CredentialRefusal(
            "the stored ChatGPT OAuth credential has expired. Sign in again on "
            "the Providers page and usage can be read."
        )
    raise CredentialRefusal(
        "no ChatGPT OAuth credential is stored. Sign in on the Providers page "
        "and usage can be read."
    )
