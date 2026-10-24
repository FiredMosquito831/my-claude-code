"""Base web search provider: config contract and KeyPool-owned rotation loop.

Concrete adapters implement :meth:`BaseWebSearchProvider._search_with_key`;
``search`` owns acquire -> try -> report semantics and domain-flag enforcement.
``search_once`` is one try on one named key, for a caller that owns key
selection itself (the web search chain).
"""

import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import ClassVar

import httpx

from my_claude_code.core.websearch.models import WebSearchResponse

from .errors import (
    KIND_DETAIL_BENCHED,
    WebSearchConfigError,
    WebSearchError,
    WebSearchInvalidRequestError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from .rotation import SEARCH_POOL_TUNING, KeyPool, mask_key_label

#: Decides what one failed key try costs the key (7.112.1): ``"auth"`` (the
#: lockout ladder), ``"rate_limit"`` (a 429 window), ``"quota"`` (out of
#: credits; the lockout ladder, as it always was here) or ``None`` (nothing --
#: the key stays usable). The vocabulary is the model path's
#: ``credential_failure_class``; the search route passes the model path's own
#: judgement (``api/web_tools/search_key_charge.py``), which ``websearch``
#: cannot import itself.
type KeyChargeJudge = Callable[[BaseException], str | None]


def _whole_seconds(wait: float) -> int:
    """A wait as the client reads it: whole seconds, rounded up; 0 if none."""

    return max(1, math.ceil(wait)) if wait > 0 else 0


@dataclass(frozen=True, slots=True)
class WebSearchProviderConfig:
    """Resolved immutable configuration for one web search provider instance."""

    api_keys: tuple[str, ...]  # may be empty for keyless providers (ddgs, searxng)
    credential_rotation: str  # single|round_robin|least_used|failover
    base_url: str | None  # override (searxng self-host, testing)
    proxy: str | None
    http_timeout: float  # seconds, default 20
    # Dotenv-only advanced options (env var name -> raw value), catalog-driven.
    options: Mapping[str, str] = field(default_factory=dict)


class BaseWebSearchProvider(ABC):
    """Base class for web search providers. Extend this to add your own."""

    PROVIDER_ID: ClassVar[str]
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        self._config = config
        # Keyless providers rotate over a single anonymous key slot (index 0).
        # 7.112.1: with the model pool's single-key bypass (SEARCH_POOL_TUNING).
        self._pool = KeyPool(
            config.api_keys or ("",),
            policy=config.credential_rotation,
            tuning=SEARCH_POOL_TUNING,
        )
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_id(self) -> str:
        return type(self).PROVIDER_ID

    @property
    def config(self) -> WebSearchProviderConfig:
        return self._config

    @property
    def key_pool(self) -> KeyPool:
        return self._pool

    def key_label(self, key_index: int) -> str:
        """Masked ``first4…last4`` label for the key that served a request."""

        return mask_key_label(self._pool.key_at(key_index))

    async def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        allowed_domains: tuple[str, ...] = (),
        blocked_domains: tuple[str, ...] = (),
        key_charge: KeyChargeJudge | None = None,
    ) -> WebSearchResponse:
        """Run the search with key rotation; each key is tried at most once.

        ``key_charge`` decides what a failed try costs its key (7.112.1). The
        search route passes the model path's judgement, so a 5xx, a timeout,
        a refused connection or an unreadable body leaves the key usable for
        the next search, and only a rejected key, a 429 or an exact
        out-of-credits phrase benches it. Without one, every failure is
        reported to the pool by its search kind, as before 7.112.1. Either
        way rotation is unchanged: every failure but a rejected query or a
        missing configuration still tries the next key.

        Two rules hold whatever the judge says: a keyless provider's
        anonymous slot is never benched, and a pool that can serve from one
        slot only keeps serving it (the pool's single-key bypass) -- except
        inside the wait a 429 asked for, which is answered "rate limited,
        retry after N s" with no call made.
        """

        if not self.SUPPORTS_DOMAINS:
            allowed_domains = ()
            blocked_domains = ()
        waiting = self._pool.bypass_rate_limit_wait()
        if waiting > 0:
            raise self._rate_limited_error(waiting, "the key is")
        excluded: set[int] = set()
        last_error: WebSearchError | None = None
        while True:
            acquired = self._pool.acquire(exclude=frozenset(excluded))
            if acquired is None:
                break
            key_index, key = acquired
            excluded.add(key_index)
            try:
                response = await self._search_with_key(
                    query,
                    key,
                    key_index,
                    max_results=max_results,
                    allowed_domains=allowed_domains,
                    blocked_domains=blocked_domains,
                )
            except WebSearchInvalidRequestError, WebSearchConfigError:
                # Caller/config faults: rotating keys cannot help.
                raise
            except WebSearchError as error:
                error.key_index = key_index
                self._report_failure(key_index, error, key_charge)
                last_error = error
            except Exception as error:
                self._report_failure(key_index, error, key_charge)
                raise
            else:
                self._pool.report_success(key_index)
                return response
        if last_error is not None:
            raise last_error
        raise self._exhausted_error()

    def _report_failure(
        self, key_index: int, error: Exception, key_charge: KeyChargeJudge | None
    ) -> None:
        """Tell the pool what one failed try costs its key (maybe nothing)."""

        if not self._config.api_keys:
            # A keyless provider has nothing to bench: its anonymous slot is
            # the whole service, and the next search simply tries again.
            return
        message = error.message if isinstance(error, WebSearchError) else None
        if key_charge is None:
            # No judge: the pool's own historical reading of the search kind.
            if isinstance(error, WebSearchRateLimitError):
                charge: str | None = "rate_limit"
            elif isinstance(error, WebSearchError):
                charge = error.kind
            else:
                charge, message = "upstream", type(error).__name__
        else:
            charge = key_charge(error)
        if charge is None:
            return
        if charge == "rate_limit":
            self._pool.report_rate_limit(
                key_index,
                message=message,
                retry_after_seconds=(
                    error.retry_after_seconds
                    if isinstance(error, WebSearchRateLimitError)
                    else None
                ),
            )
            return
        self._pool.report_failure(key_index, kind=charge, message=message)

    def _rate_limited_error(self, wait: float, subject: str) -> WebSearchError:
        """The no-call refusal inside a 429 wait, naming how long is left."""

        seconds = _whole_seconds(wait)
        error = WebSearchRateLimitError(
            self.provider_id,
            f"rate limited, retry after {seconds}s ({subject} inside the wait "
            "the provider asked for; no call made)",
            retry_after_seconds=float(seconds) if seconds else None,
            kind_detail=KIND_DETAIL_BENCHED,
        )
        error.key_index = 0
        return error

    def _exhausted_error(self) -> WebSearchError:
        """No key could be tried at all: say which are benched, and how long.

        Before 7.112.1 this read "no API keys available to serve the search"
        whatever had benched them, and a pool benched only by 429s answered
        ``unavailable`` rather than ``too_many_requests``.
        """

        rate_limited, locked_out, other, wait = self._pool.bench_summary()
        count = self._pool.key_count
        if rate_limited and not (locked_out or other):
            return self._rate_limited_error(wait, f"all {count} keys are")
        parts = []
        if locked_out:
            parts.append(
                f"{locked_out} locked out (the provider rejected the key or "
                "reported no credits)"
            )
        if rate_limited:
            parts.append(f"{rate_limited} rate limited")
        if other:
            parts.append(f"{other} cooling down after failures")
        seconds = _whole_seconds(wait)
        when = f"; the first is usable again in {seconds}s" if seconds else ""
        error = WebSearchUpstreamError(
            self.provider_id,
            f"all {count} keys are benched: {', '.join(parts)}{when}; "
            "no call made",
            kind_detail=KIND_DETAIL_BENCHED,
        )
        error.key_index = 0
        return error

    async def search_once(
        self,
        query: str,
        key: str,
        key_index: int,
        *,
        max_results: int = 10,
        allowed_domains: tuple[str, ...] = (),
        blocked_domains: tuple[str, ...] = (),
    ) -> WebSearchResponse:
        """One search with one named key: no pool, no rotation, no health.

        For a caller that owns key selection itself -- the web search chain
        (``api/web_tools/search_chain.py``, 7.112.0) drives the keys with the
        model path's own rotation and retry objects. The domain-flag rule is
        the same one :meth:`search` applies; :meth:`search` and its
        :class:`KeyPool` are untouched and still serve every search made
        without a chain.
        """

        if not self.SUPPORTS_DOMAINS:
            allowed_domains = ()
            blocked_domains = ()
        return await self._search_with_key(
            query,
            key,
            key_index,
            max_results=max_results,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )

    @abstractmethod
    async def _search_with_key(
        self,
        query: str,
        key: str,
        key_index: int,
        *,
        max_results: int,
        allowed_domains: tuple[str, ...],
        blocked_domains: tuple[str, ...],
    ) -> WebSearchResponse:
        """Perform one search attempt with a specific key."""

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise WebSearchConfigError(
                self.provider_id, "HTTP client is not initialized"
            )
        return self._client

    async def close(self) -> None:
        """Release the HTTP client (if any)."""

        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()
