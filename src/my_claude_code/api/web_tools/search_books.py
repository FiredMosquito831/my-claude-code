"""Search keys on the model path's own rotation, 429, lockout and retry objects.

Used only when ``WEB_SEARCH_CHAIN`` is set (7.112.0). With no chain, every
search runs the route it always ran -- ``BaseWebSearchProvider.search`` and its
``KeyPool`` -- and nothing in this module is touched.

With a chain, each search provider gets one :class:`SearchKeyBooks`: the very
objects a model provider's key pool is built from, constructed for search's
own keys and kept beside the provider cache. Nothing here is a copy of a rule:

* :class:`CredentialRotationState` (``providers/credential_rotation.py``)
  decides which key serves and whether a failure charges it -- the three
  key-shaped signals (401/403, a 429, an exact out-of-credits phrase), the
  rotation allow-list, ``CREDENTIAL_LOCKOUT_TIERS``, and the single-key
  bypass that keeps an only key from being benched into an outage.
* :class:`RateLimitCooldown` (``core/rate_limit.py``) is the operator's 429
  policy (``RATE_LIMIT_COOLDOWN_MODE/SECONDS/MAX_SECONDS``).
* One :class:`ProviderRateLimiter` per key (``providers/rate_limit.py``) runs
  the ``PROVIDER_RETRY_ATTEMPTS`` ladder with the operator's backoff, as the
  model path runs one per single-key sub-provider. ``routes_around_model`` is
  always False: a search service has no "other model on the same key", so a
  429 is retried and then the key rotates, which is the chain-order rule.
  No proactive pacing (``rate_limit=0``) and no concurrency slot is ever
  taken: nobody asked for either on search (user decision Q4).
* One :class:`RouteHealthRegistry` (``application/route_health.py``) for the
  whole chain, with the operator's ``FALLBACK_BENCH_*`` settings -- a no-op
  while benching is off, as it ships.

The one thing this module owns is :func:`translate_search_error`: search's
adapter-boundary classification of a :class:`WebSearchError` into the
``ExecutionFailure`` those objects read -- the "providers classify their own
failures" rule (III.5) -- per the table in
``specs/INVESTIGATION-WEBSEARCH-ROUTING.md`` §3.3.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.config.settings import Settings, parse_lockout_tiers
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.rate_limit import (
    UNLIMITED_RATE_LIMIT,
    RateLimitCooldown,
    retry_after_from_body,
)
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.failure_policy import is_quota_error, quota_failure
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.websearch.base import (
    BaseWebSearchProvider,
    WebSearchProviderConfig,
)
from my_claude_code.websearch.errors import (
    KIND_DETAIL_BAD_JSON,
    KIND_DETAIL_TIMEOUT,
    KIND_DETAIL_TRANSPORT,
    WebSearchConfigError,
    WebSearchError,
    WebSearchRateLimitError,
)

#: The limiter's constructor insists on a positive concurrency bound. The door
#: never takes a concurrency slot (``ProviderRateLimiter.concurrency_slot``),
#: so no bound applies to search; this only satisfies the constructor and
#: reads as "no cap" in the limiter's start-up log line.
_NO_CONCURRENCY_CAP = 2**31 - 1

#: The statuses a credits phrase is read on besides 402 (the model path's own
#: ``providers/failure_policy._QUOTA_PHRASE_STATUSES``).
_QUOTA_PHRASE_STATUSES = frozenset({400, 403})


@dataclass(frozen=True, slots=True)
class SearchBooksPolicy:
    """Every setting the books are built from; a change rebuilds them."""

    rotation: str
    rate_limit_cooldown_seconds: float
    rate_limit_cooldown_mode: str
    rate_limit_cooldown_max_seconds: float
    lockout_tiers: tuple[float, ...]
    model_bench_escalation: int
    retry_attempts: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    backoff_jitter_seconds: float

    @classmethod
    def from_settings(cls, settings: Settings, rotation: str) -> SearchBooksPolicy:
        return cls(
            rotation=rotation,
            rate_limit_cooldown_seconds=settings.rate_limit_cooldown_seconds,
            rate_limit_cooldown_mode=settings.rate_limit_cooldown_mode,
            rate_limit_cooldown_max_seconds=settings.rate_limit_cooldown_max_seconds,
            lockout_tiers=parse_lockout_tiers(settings.credential_lockout_tiers),
            model_bench_escalation=settings.credential_model_bench_escalation,
            retry_attempts=settings.provider_retry_attempts,
            backoff_base_seconds=settings.provider_retry_backoff_base_seconds,
            backoff_max_seconds=settings.provider_retry_backoff_max_seconds,
            backoff_jitter_seconds=settings.provider_retry_backoff_jitter_seconds,
        )

    def cooldown(self) -> RateLimitCooldown:
        """The operator's whole 429 policy, as the model path builds it."""
        return RateLimitCooldown(
            mode=self.rate_limit_cooldown_mode,
            fallback_seconds=self.rate_limit_cooldown_seconds,
            max_seconds=self.rate_limit_cooldown_max_seconds,
        )


class SearchKeyBooks:
    """One search provider's keys, health and retry ladders."""

    def __init__(
        self,
        provider_id: str,
        keys: tuple[str, ...],
        labels: tuple[str, ...],
        policy: SearchBooksPolicy,
    ) -> None:
        # A keyless provider (ddgs, searxng) has one anonymous slot, which the
        # single-key bypass keeps selectable whatever happens to it.
        self.provider_id = provider_id
        self.keys = keys or ("",)
        self.labels = labels or ("",)
        self.policy = policy
        self.cooldown = policy.cooldown()
        self.state = CredentialRotationState(
            len(self.keys),
            policy.rotation,
            rate_limit_seconds=policy.rate_limit_cooldown_seconds,
            lockout_tiers=policy.lockout_tiers,
            model_bench_escalation=policy.model_bench_escalation,
            cooldown=self.cooldown,
        )
        self.limiters = tuple(
            ProviderRateLimiter(
                rate_limit=UNLIMITED_RATE_LIMIT,
                max_concurrency=_NO_CONCURRENCY_CAP,
                max_retries=max(0, policy.retry_attempts - 1),
                backoff_base_seconds=policy.backoff_base_seconds,
                backoff_max_seconds=policy.backoff_max_seconds,
                backoff_jitter_seconds=policy.backoff_jitter_seconds,
                routes_around_model=False,
                cooldown=self.cooldown,
            )
            for _ in self.keys
        )

    @property
    def key_count(self) -> int:
        return len(self.keys)

    def label(self, index: int) -> str:
        if 0 <= index < len(self.labels):
            return self.labels[index]
        return ""

    def unavailable_now(self) -> frozenset[int]:
        """Keys whose own limiter is blocked this instant.

        ``RotatingProvider._unavailable_now`` with no model: a search has no
        (key, model) benches, so only the limiter windows remain.
        """
        return frozenset(
            index
            for index, limiter in enumerate(self.limiters)
            if limiter.remaining_wait() > 0
        )

    def throttle_remaining(self) -> float:
        """0 while a key can serve, else the shortest wait until one can.

        ``RotatingProvider.throttle_remaining`` with no model, over the same
        two facts: the engine's selectable keys, and each one's limiter.
        """
        ready = self.state.selectable_indexes()
        if not ready:
            return self.state.bench_remaining_now()
        return min(
            (
                self.limiters[index].remaining_wait()
                for index in ready
                if index < len(self.limiters)
            ),
            default=0.0,
        )

    def translate(self, error: BaseException) -> ExecutionFailure | None:
        """The ``provider_failure_override`` the key ladder runs with."""
        return translate_search_error(error, cooldown=self.cooldown)


class _UpstreamRefusal(Exception):
    """The carrier the model path's quota reader reads: ``body`` + status.

    ``providers/failure_policy.quota_phrase`` reads the structured body
    through ``upstream_complaint``, which looks for ``.body`` (and prunes the
    keys under which a request is echoed back) and falls back to the text of
    the exception. A search error keeps its body on ``upstream_body``.
    """

    def __init__(self, error: WebSearchError) -> None:
        # Worded like an HTTP client's error ("HTTP 403 ..."), never as the
        # bare complaint: the country-refusal reader trusts only words that
        # came out of a structured body, and tells them apart from the text
        # of the exception by exactly that difference.
        status = "" if error.status_code is None else f"HTTP {error.status_code} "
        super().__init__(f"{status}{error.message}")
        self.body = error.upstream_body
        self.status_code = error.status_code


def translate_search_error(
    error: BaseException, *, cooldown: RateLimitCooldown
) -> ExecutionFailure | None:
    """Classify one search failure into the model path's ``ExecutionFailure``.

    ``None`` for anything that is not a classified search failure -- a
    missing key or base URL (``WebSearchConfigError``: the chain skips the
    entry, nothing is charged) or an exception no adapter raised -- so the
    retry ladder and the pool read it exactly as they would read it raw.

    The table (spec §3.3):

    * ``quota`` (402, Tavily's 432), or a 400/403 whose body names an exact
      credits phrase -> ``quota_failure``: benched only on the phrase.
    * ``auth`` 403 -> PERMISSION; any other ``auth`` -> AUTHENTICATION.
    * ``rate_limit`` -> RATE_LIMIT 429, retryable, with the wait the provider
      published (header under the operator's ceiling, else its body).
    * ``invalid`` (400/404/405/422) -> MODEL_REJECTED: the next search service
      builds its own request and may accept it (user decision Q5a).
    * ``upstream``: timeout -> TIMEOUT 504 and transport -> UNAVAILABLE 503
      (both retryable); an unparseable body -> UPSTREAM 502 (not retryable);
      a 5xx -> UPSTREAM with that status (retryable); any other status ->
      UPSTREAM, retryable as a 429/5xx would be; no status at all -> UPSTREAM
      502, not retryable.

    The failure carries the provider's refusal on ``__cause__``, as every
    model-path classifier raises its failure ``from`` the host's error: the
    pool reads a country refusal (a 403 whose body says so) there, and leaves
    the key uncharged exactly as it does for a model key.
    """
    if not isinstance(error, WebSearchError) or isinstance(error, WebSearchConfigError):
        return None
    failure = _translate(error, cooldown)
    failure.__cause__ = _UpstreamRefusal(error)
    return failure


def _translate(error: WebSearchError, cooldown: RateLimitCooldown) -> ExecutionFailure:
    message = str(error)
    status = error.status_code
    if error.kind == "quota" or (
        status in _QUOTA_PHRASE_STATUSES and is_quota_error(_UpstreamRefusal(error))
    ):
        return quota_failure(_UpstreamRefusal(error), cooldown.fallback_seconds)
    if error.kind == "auth":
        if status == 403:
            return _failure(FailureKind.PERMISSION, 403, message, False)
        return _failure(FailureKind.AUTHENTICATION, status or 401, message, False)
    if error.kind == "rate_limit":
        return _failure(
            FailureKind.RATE_LIMIT,
            429,
            message,
            True,
            _published_wait(error, cooldown),
        )
    if error.kind == "invalid":
        return _failure(FailureKind.MODEL_REJECTED, status or 400, message, False)
    detail = error.kind_detail
    if detail == KIND_DETAIL_TIMEOUT:
        return _failure(FailureKind.TIMEOUT, 504, message, True)
    if detail == KIND_DETAIL_TRANSPORT:
        return _failure(FailureKind.UNAVAILABLE, 503, message, True)
    if detail == KIND_DETAIL_BAD_JSON:
        return _failure(FailureKind.UPSTREAM, 502, message, False)
    if status is not None:
        return _failure(
            FailureKind.UPSTREAM, status, message, status == 429 or status >= 500
        )
    return _failure(FailureKind.UPSTREAM, 502, message, False)


def _published_wait(error: WebSearchError, cooldown: RateLimitCooldown) -> float | None:
    """What the provider published, as the model path reads it.

    The header (already read by the adapter) under the operator's
    ``RATE_LIMIT_COOLDOWN_MAX_SECONDS``; else a wait stated in the body, which
    keeps its own day-scale bound (``core/rate_limit.retry_after_from_body``).
    """
    if isinstance(error, WebSearchRateLimitError):
        stated = error.retry_after_seconds
        if stated is not None:
            return cooldown.bound(stated)
    return retry_after_from_body(error.upstream_body)


def _failure(
    kind: FailureKind,
    status: int,
    message: str,
    retryable: bool,
    retry_after_seconds: float | None = None,
) -> ExecutionFailure:
    return ExecutionFailure(
        kind=kind,
        status_code=status,
        message=message,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
    )


# ---------------------------------------------------------------- the caches

#: The books of the old DuckDuckGo scraper as a chain entry: one keyless slot.
LEGACY_BOOKS_ID = "legacy"

_lock = asyncio.Lock()
#: provider id -> (what the books were built from, the books).
_books: dict[
    str,
    tuple[tuple[WebSearchProviderConfig | None, SearchBooksPolicy], SearchKeyBooks],
] = {}


async def search_key_books(
    settings: Settings, provider: BaseWebSearchProvider
) -> SearchKeyBooks:
    """The books for this provider's resolved config, rebuilt when it changes.

    Kept beside the provider cache (``search_providers.py``) and keyed the
    same way -- by the resolved immutable config -- plus every setting the
    books read. Health is index-keyed, so a change of keys or of order starts
    clean, as a model provider's pool does.
    """
    config = provider.config
    policy = SearchBooksPolicy.from_settings(settings, config.credential_rotation)
    built_from = (config, policy)
    async with _lock:
        cached = _books.get(provider.provider_id)
        if cached is not None and cached[0] == built_from:
            return cached[1]
        keys = tuple(config.api_keys)
        labels = tuple(provider.key_label(index) for index in range(len(keys) or 1))
        books = SearchKeyBooks(provider.provider_id, keys, labels, policy)
        _books[provider.provider_id] = (built_from, books)
        return books


async def legacy_scraper_books(settings: Settings) -> SearchKeyBooks:
    """One keyless slot for the old DuckDuckGo scraper as a chain entry.

    The scraper raises raw ``httpx`` errors for an HTTP error status or a
    transport failure, which the retry ladder and the pool read natively --
    exactly as they read a model provider's. Since 7.119.0 it also raises two
    failures it classified itself, read through :func:`translate_search_error`
    like any adapter's: DuckDuckGo's bot check (``rate_limit``) and a results
    page no declared profile can read (``upstream``).
    """
    policy = SearchBooksPolicy.from_settings(settings, "single")
    built_from = (None, policy)
    async with _lock:
        cached = _books.get(LEGACY_BOOKS_ID)
        if cached is not None and cached[0] == built_from:
            return cached[1]
        books = SearchKeyBooks(LEGACY_BOOKS_ID, (), (), policy)
        _books[LEGACY_BOOKS_ID] = (built_from, books)
        return books


def cached_search_key_books(provider_id: str) -> SearchKeyBooks | None:
    """The books a chain search built for ``provider_id``; None when never used."""
    cached = _books.get(provider_id)
    return None if cached is None else cached[1]


type _EjectKey = tuple[str, int, int, float, int, float, bool]
_health: dict[_EjectKey, RouteHealthRegistry] = {}


def search_route_health(settings: Settings) -> RouteHealthRegistry:
    """The chain's own bench registry, built with the operator's settings.

    Separate from the model routes' registry (``application.execution
    .route_health_registry``) and keyed the same way, so a search entry's
    failures never bench a model and the reverse.
    """
    key: _EjectKey = (
        settings.fallback_behavior,
        settings.fallback_eject_after_failures,
        settings.fallback_eject_window,
        settings.fallback_eject_failure_rate,
        settings.fallback_eject_min_samples,
        settings.fallback_eject_seconds,
        settings.fallback_bench_enabled,
    )
    registry = _health.get(key)
    if registry is None:
        registry = RouteHealthRegistry(
            mode=cast(Literal["consecutive", "rate_based"], key[0]),
            eject_after_failures=key[1],
            eject_window=key[2],
            eject_failure_rate=key[3],
            eject_min_samples=key[4],
            eject_seconds=key[5],
            bench_enabled=key[6],
        )
        _health[key] = registry
    return registry


def reset_search_books() -> None:
    """Forget every book and bench (tests)."""
    _books.clear()
    _health.clear()


def books_snapshot() -> Mapping[str, SearchKeyBooks]:
    """Every provider's live books, by provider id (diagnostics, tests)."""
    return {provider_id: entry[1] for provider_id, entry in _books.items()}
