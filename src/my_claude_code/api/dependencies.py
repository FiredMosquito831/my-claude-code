"""FastAPI dependencies for the explicit runtime service boundary."""

import secrets

from fastapi import Depends, HTTPException, Request
from loguru import logger

from my_claude_code.application.errors import UnknownProviderError
from my_claude_code.application.ports import ProviderPort, RequestRuntimeLease
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings

from .ports import ApiServices


def get_services(request: Request) -> ApiServices:
    """Return the complete services supplied when the app was constructed."""
    return request.app.state.services


def get_settings(services: ApiServices = Depends(get_services)) -> Settings:
    """Return the current request-runtime settings snapshot."""
    return services.requests.current_settings()


def resolve_provider(
    provider_type: str,
    *,
    lease: RequestRuntimeLease,
) -> ProviderPort:
    """Resolve a provider through one retained generation."""
    should_log_init = not lease.is_provider_cached(provider_type)
    try:
        provider = lease.resolve_provider(provider_type)
    except UnknownProviderError:
        logger.error(
            "Unknown provider_type: '{}'. Supported: {}",
            provider_type,
            ", ".join(f"'{key}'" for key in PROVIDER_CATALOG),
        )
        raise
    if should_log_init:
        logger.info("Provider initialized: {}", provider_type)
    return provider


def require_proxy_auth(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Require the configured proxy token, as bearer authorization or x-api-key.

    Both headers, because both are how a real Anthropic Messages client
    authenticates. Claude Code's ``ANTHROPIC_AUTH_TOKEN`` produces
    ``Authorization: Bearer``, which is all MCC used to read; Anthropic's own
    documented header is ``x-api-key``, and an SDK-built client sends that one.
    Command Code is the case that forced the issue: for a provider declared
    ``api: "anthropic-messages"`` its ``authHeadersFor`` emits
    ``{"x-api-key": key}`` and nothing else, its ``providers.json`` applies no
    substitution to the static ``headers`` map -- so writing a bearer header
    there would have put the proxy token on disk in the user's own file -- and
    the observable symptom was a 401 with no hint as to which of the two
    layers rejected it. Reading both headers costs one lookup and removes an
    entire class of that failure.

    Gemini clients are the third form and the reason this list grew again.
    ``@google/genai`` -- the SDK Gemini CLI bundles -- sends the key as
    ``x-goog-api-key`` and never as a bearer token
    (``NodeAuth.addKeyHeader``, read out of Gemini CLI 0.49.0's own bundle),
    while Google's REST documentation and every ``curl`` example put it in the
    ``key`` query parameter instead. Both are read, and the query form last of
    all: a token on a URL lands in access logs and shell history, so it is
    accepted for compatibility and never preferred over a header.

    ``Authorization`` still wins when several are present, so nothing about an
    existing client's behaviour changes.
    """
    anthropic_auth_token = settings.anthropic_auth_token.strip()
    if not anthropic_auth_token:
        return

    token = _presented_proxy_token(request)
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="Missing proxy authentication token",
        )

    if not token or not secrets.compare_digest(
        token.encode("utf-8"),
        anthropic_auth_token.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid proxy authentication token",
        )


def _presented_proxy_token(request: Request) -> str | None:
    """Return the token a request presented, or None when it presented none.

    An empty string is returned for a header that is present but malformed, so
    the caller reports "invalid" rather than "missing" -- the two say different
    things to someone debugging a launcher.
    """

    authorization = request.headers.get("authorization")
    if authorization:
        parts = authorization.strip().split(maxsplit=1)
        if len(parts) != 2 or parts[0].casefold() != "bearer":
            return ""
        return parts[1].strip()

    api_key = request.headers.get("x-api-key")
    if api_key is not None:
        return api_key.strip()

    goog_api_key = request.headers.get("x-goog-api-key")
    if goog_api_key is not None:
        return goog_api_key.strip()

    query_key = _query_key(request)
    if query_key is not None:
        return query_key.strip()
    return None


def _query_key(request: Request) -> str | None:
    """Return the ``key`` query parameter, or None when there is no query.

    Guarded because ``request.query_params`` reads ``scope["query_string"]``
    directly and raises ``KeyError`` on a scope that omits it. Every real ASGI
    server sets it; a hand-built ``Request`` in a unit test need not, and a
    header-only auth check must not depend on the shape of a scope it never
    reads.
    """

    if "query_string" not in request.scope:
        return None
    return request.query_params.get("key")
