"""Browser OAuth (PKCE) login for a Claude subscription credential.

Anthropic's callback is a hosted page rather than a loopback redirect, so there
is no local callback server to run: the user approves in a browser, the code is
shown on Anthropic's page, and they paste it back. That is also why this is a
paste flow rather than the silent capture ``chatgpt_oauth`` can do.

READ ``docs/ANTHROPIC-SUBSCRIPTION.md`` FIRST. Anthropic's published position is
that third-party products may not offer Claude.ai login; this exists because the
operator asked for it for their own account.
"""

import base64
import hashlib
import secrets
import urllib.parse
from typing import Any

import httpx

from .constants import (
    AUTHORIZE_URL,
    CLAUDE_CODE_CLIENT_ID,
    LEGACY_TOKEN_URL,
    OAUTH_REFRESH_BETA,
    OAUTH_SCOPES,
    PKCE_METHOD,
    REDIRECT_URI,
    TOKEN_ENDPOINT_USER_AGENT,
    TOKEN_URL,
)
from .credentials import OAuthTokens, _tokens_from_payload, store_tokens


class AnthropicOAuthLoginError(RuntimeError):
    """Raised when the authorization-code exchange fails.

    ``detail`` is for text MCC wrote. A token endpoint's response body is
    never passed here: it can echo the code, the verifier or a freshly issued
    token, and this message reaches logs and an HTTP error response.
    """

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


def build_authorize_url(verifier: str) -> str:
    """Build the URL the user opens to approve access.

    ``state`` is the verifier itself, which is what this flow specifies.
    """
    query = urllib.parse.urlencode(
        {
            "code": "true",
            "response_type": "code",
            "client_id": CLAUDE_CODE_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": OAUTH_SCOPES,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": PKCE_METHOD,
            "state": verifier,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def split_pasted_code(pasted: str) -> tuple[str, str | None]:
    """Split the ``code#state`` value Anthropic's callback page shows."""
    cleaned = pasted.strip()
    if "#" in cleaned:
        code, _, state = cleaned.partition("#")
        return code.strip(), state.strip() or None
    return cleaned, None


def _exchange_payload(code: str, verifier: str, state: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": CLAUDE_CODE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
    }
    if state:
        payload["state"] = state
    return payload


async def exchange_code(
    code: str, verifier: str, state: str | None = None
) -> OAuthTokens:
    """Exchange an authorization code for a credential and store it."""
    payload = _exchange_payload(code, verifier, state)
    headers = {
        "Content-Type": "application/json",
        # Offset 180990503: the token endpoint receives this beta and no other.
        "anthropic-beta": OAUTH_REFRESH_BETA,
        "User-Agent": TOKEN_ENDPOINT_USER_AGENT,
    }
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
