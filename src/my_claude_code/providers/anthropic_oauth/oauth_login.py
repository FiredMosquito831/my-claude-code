"""Browser OAuth (PKCE) login for a Claude subscription credential.

Claude Code offers two redirect targets and so does MCC:

* a **loopback callback** -- ``http://localhost:<port>/callback`` -- which
  completes without the user copying anything. See :mod:`.loopback`.
* the **manual redirect** -- Anthropic's hosted callback page, which shows a
  ``code#state`` string to paste back. This is the fallback for every case
  where the browser cannot reach this process's ``localhost``.

This module holds the pieces both flows share: PKCE, the authorize URL, the
code exchange, and the parsing of whatever the user pastes.

READ ``docs/ANTHROPIC-SUBSCRIPTION.md`` FIRST. Anthropic's published position is
that third-party products may not offer Claude.ai login; this exists because the
operator asked for it for their own account.
"""

import base64
import hashlib
import re
import secrets
import urllib.parse
from typing import Any

import httpx

from .constants import (
    AUTHORIZE_URL,
    CLAUDE_CODE_CLIENT_ID,
    LEGACY_TOKEN_URL,
    OAUTH_SCOPES,
    PKCE_METHOD,
    REDIRECT_URI,
    TOKEN_URL,
)
from .credentials import (
    OAuthTokens,
    _tokens_from_payload,
    store_tokens,
    token_endpoint_headers,
)


class AnthropicOAuthLoginError(RuntimeError):
    """Raised when the authorization-code exchange fails.

    ``safe_message`` marks this text as MCC's own prose, safe to show an
    operator verbatim -- see ``providers/runtime/validation.py``.

    ``detail`` is for text MCC wrote. A token endpoint's response body is
    never passed here: it can echo the code, the verifier or a freshly issued
    token, and this message reaches logs and an HTTP error response.
    """

    #: This exception's ``str()`` contains no secret and no response body.
    safe_message = True

    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        message = f"Anthropic OAuth login failed with HTTP {status_code}"
        super().__init__(f"{message}: {detail}" if detail else message)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_pkce_verifier() -> str:
    """Return a 43-character base64url PKCE verifier."""
    return _b64url(secrets.token_bytes(32))


def pkce_challenge(verifier: str) -> str:
    """Return the S256 challenge for one verifier."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def build_authorize_url(verifier: str, *, redirect_uri: str | None = None) -> str:
    """Build the URL the user opens to approve access.

    ``redirect_uri`` defaults to Anthropic's hosted callback page (the paste
    flow). :mod:`.loopback` passes ``http://localhost:<port>/callback`` instead.
    The parameter set and its order are Claude Code's ``l3t`` (offset
    182767278 of the 2.1.260 bundle).

    ``state`` is the verifier itself, which is what this flow specifies.
    """
    query = urllib.parse.urlencode(
        {
            "code": "true",
            "response_type": "code",
            "client_id": CLAUDE_CODE_CLIENT_ID,
            "redirect_uri": redirect_uri or REDIRECT_URI,
            "scope": OAUTH_SCOPES,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": PKCE_METHOD,
            "state": verifier,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


#: A whole pasted value that is only ``key=value`` pairs. Deliberately strict:
#: an authorization code is an opaque token, and mistaking one for a query
#: string would silently truncate it at the first ``&``.
_BARE_QUERY_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_.-]*=[^&=]*(?:&[A-Za-z_][A-Za-z0-9_.-]*=[^&=]*)*"
)


def split_pasted_code(pasted: str) -> tuple[str, str | None]:
    """Parse whatever the user pasted into ``(code, state)``.

    Three shapes are accepted, because all three are things a person actually
    pastes:

    1. ``code#state`` -- what Anthropic's callback page displays.
    2. a bare ``code``.
    3. **the whole callback URL out of the address bar** --
       ``https://platform.claude.com/oauth/code/callback?code=…&state=…``, or
       the loopback equivalent. Before 6.43.0 this was handed to the token
       endpoint verbatim as the code, which answered 400, which MCC reported as
       "the pasted code was rejected -- it is single-use and short-lived, so
       start the sign-in again". That advice is wrong for this case and sends
       the operator round the loop forever, and pasting the address bar is the
       single most likely mistake in a manual-redirect flow.

    A bare query string (``?code=…&state=…`` or ``code=…&state=…``) counts as
    shape 3: it is what you get by copying half a URL.
    """
    cleaned = pasted.strip().strip("'\"")
    if not cleaned:
        return "", None

    query: str | None = None
    if cleaned.lower().startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(cleaned)
        # A code may arrive in the query (Anthropic's callback) or, for an
        # implicit-style fragment, after the ``#``. Try both.
        query = parsed.query or parsed.fragment
    elif cleaned.startswith(("?", "&")):
        query = cleaned.lstrip("?&")
    elif _BARE_QUERY_RE.fullmatch(cleaned):
        # A bare ``code=…&state=…`` pair with no leading punctuation, which is
        # what copying half a URL produces.
        query = cleaned

    if query:
        fields = urllib.parse.parse_qs(query, keep_blank_values=False)
        code = (fields.get("code") or [""])[0].strip()
        if code:
            state = (fields.get("state") or [""])[0].strip() or None
            return code, state
        if cleaned.lower().startswith(("http://", "https://")):
            # A callback URL that carries no code at all -- typically
            # ``?error=access_denied``. Returning it verbatim would send it to
            # the token endpoint as if it were a code and produce the same
            # misleading "paste a fresh one" advice; an empty code makes the
            # caller say "no code" instead.
            return "", None

    if "#" in cleaned:
        code, _, state = cleaned.partition("#")
        return code.strip(), state.strip() or None
    return cleaned, None


def _exchange_payload(
    code: str,
    verifier: str,
    state: str | None,
    redirect_uri: str | None = None,
) -> dict[str, Any]:
    """The exchange body Claude Code sends (``EAn``, offset 182768091).

    ``redirect_uri`` must be byte-identical to the one the authorize URL
    carried, which is why it is threaded through rather than re-derived.
    """
    payload: dict[str, Any] = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": CLAUDE_CODE_CLIENT_ID,
        "redirect_uri": redirect_uri or REDIRECT_URI,
    }
    if state:
        payload["state"] = state
    return payload


async def exchange_code(
    code: str,
    verifier: str,
    state: str | None = None,
    *,
    redirect_uri: str | None = None,
) -> OAuthTokens:
    """Exchange an authorization code for a credential and store it."""
    payload = _exchange_payload(code, verifier, state, redirect_uri)
    headers = token_endpoint_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(TOKEN_URL, json=payload, headers=headers)
        if response.status_code in (301, 308, 404) and LEGACY_TOKEN_URL != TOKEN_URL:
            # The 2.1.258 host migration, with the old host as the documented
            # fallback. See ``constants.LEGACY_TOKEN_URL``.
            response = await client.post(
                LEGACY_TOKEN_URL, json=payload, headers=headers
            )
    if response.status_code >= 400:
        # Deliberately not the response body: see AnthropicOAuthLoginError.
        raise AnthropicOAuthLoginError(
            response.status_code,
            "the pasted code was rejected -- it is single-use and short-lived, "
            "so start the sign-in again and paste a fresh one",
        )

    tokens = _tokens_from_payload(response.json(), source="mcc")
    if tokens is None:
        raise AnthropicOAuthLoginError(200, "response carried no access token")
    store_tokens(tokens)
    return tokens
