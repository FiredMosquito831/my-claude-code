"""Web search provider error hierarchy with stable error kinds.

The ``kind`` class attribute is the canonical classification consumed by key
rotation (auth -> lockout, rate_limit -> cooldown, etc.) and by analytics.
"""

from typing import ClassVar

#: What kind of ``upstream`` failure carried no HTTP status: the request timed
#: out, the connection itself failed, or the provider answered a body that is
#: not JSON. ``None`` on every other error. Read by the search chain's
#: translation (``api/web_tools/search_books.py``), which needs the same facts
#: the model path's classifier sees, and -- since 7.112.1 -- by the wording of
#: a failed search when no chain is set (``api/web_tools/outbound.py``).
KIND_DETAIL_TIMEOUT = "timeout"
KIND_DETAIL_TRANSPORT = "transport"
KIND_DETAIL_BAD_JSON = "bad_json"
#: 7.112.1: MCC refused the search itself, with no call made -- every key of
#: the provider is benched, or its only key is inside the wait a 429 asked
#: for. Set only by :meth:`BaseWebSearchProvider.search` (the route used when
#: no chain is set); the search chain never raises it.
KIND_DETAIL_BENCHED = "benched"


class WebSearchError(Exception):
    """Base error for all web search provider failures."""

    kind: ClassVar[str] = "upstream"

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        kind_detail: str | None = None,
        upstream_body: object = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.message = message
        self.status_code = status_code
        # Filled in by the rotation loop when a specific key produced the error.
        self.key_index: int | None = None
        # 7.112.0, additive: see ``KIND_DETAIL_*`` above.
        self.kind_detail = kind_detail
        # 7.112.0, additive: the provider's own error body (parsed JSON when it
        # was JSON, else its text), so the quota phrase reader and the
        # Retry-After body reader see what the model path's classifier sees.
        self.upstream_body = upstream_body
        # 7.112.1, additive: the sentence the client is shown for this
        # failure, in MCC's own words (never the provider's body). Set only by
        # the route used when no chain is set (``api/web_tools/outbound.py``);
        # None everywhere else, which keeps every other summary as it was.
        self.client_summary: str | None = None

    def __str__(self) -> str:
        return f"{self.provider}: {self.message}"


class WebSearchConfigError(WebSearchError):
    """Provider is selected but not configured (missing key, missing base URL)."""

    kind: ClassVar[str] = "config"


class WebSearchAuthError(WebSearchError):
    """Authentication/authorization failure (HTTP 401/403, stale keys)."""

    kind: ClassVar[str] = "auth"


class WebSearchRateLimitError(WebSearchError):
    """Rate limit (HTTP 429 or provider-specific throttling signal)."""

    kind: ClassVar[str] = "rate_limit"

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        kind_detail: str | None = None,
        upstream_body: object = None,
    ) -> None:
        super().__init__(
            provider,
            message,
            status_code=status_code,
            kind_detail=kind_detail,
            upstream_body=upstream_body,
        )
        # What the provider itself said about when the limit resets. None means
        # it stayed silent, which is different from "reset immediately".
        self.retry_after_seconds = retry_after_seconds


class WebSearchQuotaError(WebSearchError):
    """Billing/plan quota exhausted (HTTP 402 or provider-specific status)."""

    kind: ClassVar[str] = "quota"


class WebSearchInvalidRequestError(WebSearchError):
    """The request itself was rejected (HTTP 400/422); rotating keys won't help."""

    kind: ClassVar[str] = "invalid"


class WebSearchUpstreamError(WebSearchError):
    """Upstream 5xx, network/transport failure, or malformed provider payload."""

    kind: ClassVar[str] = "upstream"
