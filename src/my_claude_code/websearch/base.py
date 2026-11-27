"""Base web search provider: config contract and KeyPool-owned rotation loop.

Concrete adapters implement :meth:`BaseWebSearchProvider._search_with_key`;
``search`` owns acquire -> try -> report semantics and domain-flag enforcement.
``search_once`` is one try on one named key, for a caller that owns key
selection itself (the web search chain).
"""

import math
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import ClassVar

import httpx

from my_claude_code.config.constants import WEB_SEARCH_QUOTA_COOLDOWN_SECONDS_DEFAULT
from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
)

from .errors import (
    KIND_DETAIL_BENCHED,
    WebSearchConfigError,
    WebSearchError,
    WebSearchInvalidRequestError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
    consume_last_http_status,
    reset_last_http_status,
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


def _build_try_row(
    *,
    key_index: int,
    key_label: str,
    elapsed_ms: float,
    ok: bool,
    error: BaseException | None = None,
    charged: str | None = None,
    benched_for_s: float | None = None,
    rotated: bool | None = None,
) -> dict[str, object]:
    """Format one non-chain key try in the ``search_chain.tries_json`` shape."""

    if ok:
        return {
            "key_index": key_index,
            "key_label": key_label,
            "try": 1,
            "status": None,
            "kind": None,
            "retry_after": None,
            "ms": round(elapsed_ms, 3),
            "slept_s": None,
            "ok": True,
        }
    retry_after = (
        round(error.retry_after_seconds, 3)
        if isinstance(error, WebSearchRateLimitError)
        and error.retry_after_seconds is not None
        else None
    )
    return {
        "key_index": key_index,
        "key_label": key_label,
        "try": 1,
        "status": error.status_code if isinstance(error, WebSearchError) else None,
        "kind": error.kind
        if isinstance(error, WebSearchError)
        else type(error).__name__,
        "retry_after": retry_after,
        "ms": round(elapsed_ms, 3),
        "slept_s": None,
        "ok": False,
        "charged": charged,
        "benched_for_s": benched_for_s,
        "decision": str(error)[:200] if error is not None else None,
        "rotated": rotated,
    }


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
    quota_cooldown_seconds: float = WEB_SEARCH_QUOTA_COOLDOWN_SECONDS_DEFAULT
    oauth_access_token: str | None = field(default=None, repr=False)
    oauth_account_id: str | None = None


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
            quota_cooldown_seconds=config.quota_cooldown_seconds,
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
        request_options: WebSearchRequestOptions | None = None,
        tries_out: list[dict[str, object]] | None = None,
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
            if tries_out is not None:
                tries_out.append(
                    {
                        "key_index": 0,
                        "key_label": self.key_label(0),
                        "try": 1,
                        "status": 429,
                        "kind": "rate_limit",
                        "retry_after": round(waiting, 3),
                        "ms": 0.0,
                        "slept_s": None,
                        "ok": False,
                        "charged": None,
                        "benched_for_s": round(waiting, 3),
                        "decision": "rate limited (bypass wait)",
                        "rotated": False,
                    }
                )
            raise self._rate_limited_error(waiting, "the key is")
        excluded: set[int] = set()
        last_error: WebSearchError | None = None
        while True:
            acquired = self._pool.acquire(exclude=frozenset(excluded))
            if acquired is None:
                break
            key_index, key = acquired
            excluded.add(key_index)
            try_started = time.perf_counter()
            reset_last_http_status()
            try:
                if request_options is None:
                    response = await self._search_with_key(
                        query,
                        key,
                        key_index,
                        max_results=max_results,
                        allowed_domains=allowed_domains,
                        blocked_domains=blocked_domains,
                    )
                else:
                    response = await self._search_with_key_options(
                        query,
                        key,
                        key_index,
                        max_results=max_results,
                        allowed_domains=allowed_domains,
                        blocked_domains=blocked_domains,
                        request_options=request_options,
                    )
            except (WebSearchInvalidRequestError, WebSearchConfigError) as error:
                reset_last_http_status()
                # Caller/config faults: rotating keys cannot help.
                if tries_out is not None:
                    tries_out.append(
                        _build_try_row(
                            key_index=key_index,
                            key_label=self.key_label(key_index),
                            elapsed_ms=(time.perf_counter() - try_started) * 1000.0,
                            ok=False,
                            error=error,
                            charged=None,
                            benched_for_s=None,
                            rotated=False,
                        )
                    )
                raise
            except WebSearchError as error:
                reset_last_http_status()
                error.key_index = key_index
                charge, benched_for_s = self._report_failure(
                    key_index, error, key_charge
                )
                will_rotate = self._pool.has_selectable(exclude=frozenset(excluded))
                if tries_out is not None:
                    tries_out.append(
                        _build_try_row(
                            key_index=key_index,
                            key_label=self.key_label(key_index),
                            elapsed_ms=(time.perf_counter() - try_started) * 1000.0,
                            ok=False,
                            error=error,
                            charged=charge,
                            benched_for_s=benched_for_s,
                            rotated=will_rotate,
                        )
                    )
                last_error = error
            except Exception as error:
                reset_last_http_status()
                charge, benched_for_s = self._report_failure(
                    key_index, error, key_charge
                )
                if tries_out is not None:
                    tries_out.append(
                        _build_try_row(
                            key_index=key_index,
                            key_label=self.key_label(key_index),
                            elapsed_ms=(time.perf_counter() - try_started) * 1000.0,
                            ok=False,
                            error=error,
                            charged=charge,
                            benched_for_s=benched_for_s,
                            rotated=False,
                        )
                    )
                raise
            else:
                last_status = consume_last_http_status()
                if response.http_status is None and last_status is not None:
                    response = replace(response, http_status=last_status)
                self._pool.report_success(key_index)
                if tries_out is not None:
                    tries_out.append(
                        _build_try_row(
                            key_index=key_index,
                            key_label=self.key_label(key_index),
                            elapsed_ms=(time.perf_counter() - try_started) * 1000.0,
                            ok=True,
                        )
                    )
                return response
        if last_error is not None:
            raise last_error
        if tries_out is not None:
            _, _, _, wait = self._pool.bench_summary()
            tries_out.append(
                {
                    "key_index": -1,
                    "key_label": "",
                    "kind": "pool_benched",
                    "retry_after": round(wait, 3),
                }
            )
        raise self._exhausted_error()

    def _report_failure(
        self, key_index: int, error: Exception, key_charge: KeyChargeJudge | None
    ) -> tuple[str | None, float | None]:
        """Tell the pool what one failed try costs its key; return (charge, benched_s)."""

        if not self._config.api_keys:
            # A keyless provider has nothing to bench: its anonymous slot is
            # the whole service, and the next search simply tries again.
            return None, None
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
            return None, None
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
        else:
            self._pool.report_failure(key_index, kind=charge, message=message)
        facts = self._pool.health_facts()
        remaining = (
            float(facts[key_index]["remaining_s"])
            if 0 <= key_index < len(facts)
            else 0.0
        )
        return charge, round(remaining, 3) if remaining > 0 else None

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
            f"all {count} keys are benched: {', '.join(parts)}{when}; no call made",
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
        request_options: WebSearchRequestOptions | None = None,
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
        reset_last_http_status()
        try:
            if request_options is None:
                response = await self._search_with_key(
                    query,
                    key,
                    key_index,
                    max_results=max_results,
                    allowed_domains=allowed_domains,
                    blocked_domains=blocked_domains,
                )
            else:
                response = await self._search_with_key_options(
                    query,
                    key,
                    key_index,
                    max_results=max_results,
                    allowed_domains=allowed_domains,
                    blocked_domains=blocked_domains,
                    request_options=request_options,
                )
        except BaseException:
            reset_last_http_status()
            raise
        last_status = consume_last_http_status()
        if response.http_status is None and last_status is not None:
            response = replace(response, http_status=last_status)
        return response

    async def _search_with_key_options(
        self,
        query: str,
        key: str,
        key_index: int,
        *,
        max_results: int,
        allowed_domains: tuple[str, ...],
        blocked_domains: tuple[str, ...],
        request_options: WebSearchRequestOptions | None = None,
    ) -> WebSearchResponse:
        """Perform one search attempt with optional per-request overrides.

        Subclasses override this method to honor ``request_options`` without
        mutating shared configuration; default delegates to the legacy 6-arg
        :meth:`_search_with_key` hook so minimal test doubles keep working.
        """
        _ = request_options
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
