"""The web search chain: ``WEB_SEARCH_CHAIN``, walked under the model rules.

Reached from exactly one branch at the top of ``outbound._run_web_search``,
and only while ``WEB_SEARCH_CHAIN`` is set (7.112.0). With no chain nothing in
this module runs: every search takes the route it always took.

The chain is an ordered list of search services from the catalogue plus
``legacy`` (the old DuckDuckGo scraper); a service listed twice is tried again
at its place. The walk mirrors the model path's, rule for rule:

* **The entries** (``application/execution.py`` ``_prepare_from``): a paused
  entry is skipped (an all-paused chain is an error naming
  ``WEB_SEARCH_CHAIN_PAUSED``); an entry the route-health registry benched is
  skipped (only when ``FALLBACK_BENCH_ENABLED`` is on); an entry whose key is
  missing is skipped and its row names the variable (user decision Q5b); an
  entry whose every key is cooling down for at least
  ``FALLBACK_COOLDOWN_STEP_OVER_FLOOR`` is stepped over -- never the last
  one, because a skipped chain with nothing behind it is an outage.
* **The keys of one entry** (``providers/runtime/rotating.py``
  ``_stream_with_rotation``): ``acquire`` the next key, avoiding the ones this
  search already spent and the ones whose own limiter is blocked; one key try
  is the ``PROVIDER_RETRY_ATTEMPTS`` ladder of that key's limiter; a failure
  is reported to the pool, which decides both whether the key is charged and
  whether another key is worth trying. A pool with nothing to hand out says
  so in the model path's own sentences.
* **The end of an entry**: a kind in ``FALLBACK_SKIP_KINDS`` ends the route,
  anything else moves to the next entry (a 400 included: each service builds
  its own request -- user decision Q5a); ``FALLBACK_TOTAL_TIMEOUT`` > 0 is the
  route's budget. Route health records every attempted entry.

Every per-try record the shared objects write goes into a private ladder
inside ``paused_ladder()``, so nothing lands on the request's own row (Q13),
and it is stored on the search's own attempt row (``tries_json``). The route
trace, the route row and the client's SSE keep the shape they have without a
chain.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import httpx
from loguru import logger

from my_claude_code.application.deadline_hints import limit_hint
from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.config.model_refs import (
    parse_model_ref_chain,
    parse_model_ref_list,
)
from my_claude_code.config.settings import WEB_SEARCH_CHAIN_LEGACY_ENTRY, Settings
from my_claude_code.core.credential_attribution import (
    NO_CREDENTIAL_INDEX,
    NO_CREDENTIAL_LABEL,
)
from my_claude_code.core.failures import (
    FailureKind,
    failure_kind,
    find_execution_failure,
    parse_failure_kinds,
)
from my_claude_code.core.upstream_ladder import (
    AttemptLadder,
    install_ladder_trace,
    paused_ladder,
)
from my_claude_code.core.websearch.models import WebSearchResponse
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.websearch.base import BaseWebSearchProvider
from my_claude_code.websearch.errors import (
    KIND_DETAIL_TIMEOUT,
    WebSearchAuthError,
    WebSearchConfigError,
    WebSearchError,
    WebSearchQuotaError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from my_claude_code.websearch.registry import (
    SearchOutcome,
    _error_output_payload,
    _provider_config_snapshot,
    _response_output_payload,
    _search_input_payload,
    emit_search_outcome,
)

from .constants import _MAX_SEARCH_RESULTS, _REQUEST_TIMEOUT_S
from .search_books import (
    SearchKeyBooks,
    cached_search_key_books,
    legacy_scraper_books,
    search_key_books,
    search_route_health,
)
from .search_providers import runtime_provider

#: How a chain entry is named to the route-health registry and in its log
#: lines. Namespaced: ``ollama`` is both a model provider and a search service.
ENTRY_REF_PREFIX = "websearch:"

OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"
#: ``search_log.status`` of a skipped entry (``websearch.analytics``
#: excludes it from every success/error count).
SKIPPED_STATUS = "skipped"

_ERROR_MESSAGE_LOG_CHARS = 500
_PAUSED_HINT = (
    " (WEB_SEARCH_CHAIN_PAUSED -- change it on the dashboard under Web Search)"
)
_CREDITS_HINT = " (add or top up a key on the dashboard under Web Search)"


class RouteTrace(Protocol):
    """What the door needs of ``outbound._SearchRouteTrace``."""

    route_id: str
    query: str
    primary_provider: str
    terminal_provider: str
    attempt_count: int

    def begin_attempt(self, provider: str) -> int: ...

    def succeed(
        self, provider: str, *, results_count: int, cost_usd: float | None
    ) -> None: ...

    def fail(self, provider: str, error: BaseException) -> None: ...


LegacyScrape = Callable[[str], Awaitable[list[dict[str, str]]]]
ResponseItems = Callable[[WebSearchResponse], list[dict[str, str]]]


@dataclass(slots=True)
class EntryResult:
    """How one entry's key walk ended."""

    value: object = None
    #: What the chain decides on: the classified failure, or the pool's
    #: ``ApplicationUnavailableError``. ``None`` on success.
    failure: BaseException | None = None
    #: What the client and the attempt row see (a ``WebSearchError``, or the
    #: scraper's own exception). ``None`` on success.
    error: BaseException | None = None
    #: The key that served or failed last; ``NO_CREDENTIAL_INDEX`` when the
    #: pool had nothing to hand out.
    key_index: int = 0
    #: A missing key or base URL met while trying: the entry is skipped.
    skipped: str | None = None
    #: ``FALLBACK_TOTAL_TIMEOUT`` ran out inside this entry: the route ends.
    budget_spent: bool = False
    tries: list[dict[str, object]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failure is None and self.skipped is None


async def run_search_chain(
    query: str,
    settings: Settings,
    trace: RouteTrace,
    *,
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
    legacy_scrape: LegacyScrape,
    response_items: ResponseItems,
) -> list[dict[str, str]]:
    """Search along ``WEB_SEARCH_CHAIN``; the results, or the last failure raised."""

    entries = parse_model_ref_chain(settings.web_search_chain)
    paused = frozenset(parse_model_ref_list(settings.web_search_chain_paused))
    refs = tuple(f"{ENTRY_REF_PREFIX}{entry}" for entry in entries)
    if entries:
        trace.primary_provider = entries[0]
        trace.terminal_provider = entries[0]
    if settings.web_search_provider == "disabled":
        error = WebSearchConfigError(
            "disabled",
            "web search is disabled by WEB_SEARCH_PROVIDER=disabled",
        )
        trace.fail(trace.terminal_provider, error)
        raise error
    context = _route_context(settings, entries, paused)
    health = search_route_health(settings)
    order = health.usable_indexes(
        refs,
        provider_lookup=_throttle_lookup(refs),
        paused=frozenset(f"{ENTRY_REF_PREFIX}{entry}" for entry in paused),
    )
    if not order:
        error = WebSearchConfigError(
            "web_search",
            "every entry of WEB_SEARCH_CHAIN is paused, so there is nothing "
            f"left to try.{_PAUSED_HINT}",
        )
        _skip_rows(trace, entries, range(len(entries)), "paused", query, context)
        trace.fail(trace.terminal_provider, error)
        raise error
    for position in range(len(entries)):
        if position in order:
            continue
        reason = (
            "paused"
            if entries[position] in paused
            else _bench_reason(health, refs[position])
        )
        _skip_rows(trace, entries, (position,), reason, query, context)

    policy_skip = parse_failure_kinds(settings.fallback_skip_kinds)
    floor = settings.fallback_cooldown_step_over_floor
    deadline = (
        time.monotonic() + settings.fallback_total_timeout
        if settings.fallback_total_timeout > 0
        else None
    )
    last_error: BaseException | None = None
    for slot, position in enumerate(order):
        entry = entries[position]
        is_last = slot + 1 >= len(order)
        if deadline is not None and time.monotonic() >= deadline:
            # Starting another entry with no time left only delays the error
            # the caller is already going to see; every entry from here on is
            # unreachable for the same reason, so each says so.
            _skip_rows(
                trace,
                entries,
                order[slot:],
                "request budget spent (FALLBACK_TOTAL_TIMEOUT)",
                query,
                context,
            )
            if last_error is None:
                last_error = _budget_spent_error(entry, settings.fallback_total_timeout)
            break
        started_ts = time.time()
        started = time.perf_counter()
        provider: BaseWebSearchProvider | None = None
        if entry == WEB_SEARCH_CHAIN_LEGACY_ENTRY:
            books = await legacy_scraper_books(settings)
        else:
            try:
                provider = await runtime_provider(settings, entry)
            except WebSearchConfigError as error:
                _skip_row(
                    trace,
                    entry,
                    position,
                    f"not configured: {error.message}",
                    query,
                    context,
                )
                continue
            books = await search_key_books(settings, provider)
        # The step-over: a pool whose every key is benched would only make
        # this search wait or fail. Never on the last entry -- a skipped chain
        # with nothing behind it is an outage, and waiting still beats it.
        cooldown = 0.0 if is_last else books.throttle_remaining()
        if not is_last and cooldown >= floor:
            logger.warning(
                "WEB SEARCH CHAIN: '{}' is cooling down for {:.0f}s;"
                " trying the next entry instead of waiting",
                entry,
                cooldown,
            )
            _skip_row(
                trace,
                entry,
                position,
                f"cooldown {cooldown:.0f}s",
                query,
                context,
            )
            continue

        attempt_number = trace.begin_attempt(entry)
        if provider is None:
            result = await _bounded(
                _run_legacy(books, query, legacy_scrape), deadline, entry, settings
            )
        else:
            result = await _bounded(
                _run_provider(
                    books,
                    provider,
                    query,
                    allowed_domains=allowed_domains,
                    blocked_domains=blocked_domains,
                ),
                deadline,
                entry,
                settings,
            )
        if result.skipped is not None:
            _emit_row(
                trace,
                entry=entry,
                provider=provider,
                position=position,
                attempt_number=attempt_number,
                ts_epoch=started_ts,
                started=started,
                result=result,
                query=query,
                context=context,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
            )
            continue
        if result.ok:
            health.record_success(refs[position])
            if provider is None:
                raw = result.value if isinstance(result.value, list) else []
                results = [
                    {
                        **{str(name): str(value) for name, value in item.items()},
                        "provider": WEB_SEARCH_CHAIN_LEGACY_ENTRY,
                    }
                    for item in raw
                    if isinstance(item, dict)
                ]
                cost = None
            else:
                assert isinstance(result.value, WebSearchResponse)
                results = response_items(result.value)
                cost = result.value.cost_usd
            _emit_row(
                trace,
                entry=entry,
                provider=provider,
                position=position,
                attempt_number=attempt_number,
                ts_epoch=started_ts,
                started=started,
                result=result,
                query=query,
                context=context,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                legacy_results=results if provider is None else None,
            )
            trace.succeed(entry, results_count=len(results), cost_usd=cost)
            return results

        assert result.failure is not None and result.error is not None
        last_error = result.error
        trace.fail(entry, result.error)
        _record_route_failure(health, refs[position], result.failure)
        _emit_row(
            trace,
            entry=entry,
            provider=provider,
            position=position,
            attempt_number=attempt_number,
            ts_epoch=started_ts,
            started=started,
            result=result,
            query=query,
            context=context,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )
        logger.warning(
            "web_search chain entry failed entry={} position={}/{} error={}",
            entry,
            position + 1,
            len(entries),
            result.error,
        )
        if result.budget_spent:
            # The deadline check at the top of the loop ends the walk and
            # records every entry it leaves untried.
            continue
        kind = failure_kind(result.failure)
        if policy_skip and kind is not None and kind in policy_skip:
            # The model rule: a kind the operator listed in
            # FALLBACK_SKIP_KINDS ends the route -- nothing behind it would
            # be asked anything different.
            break

    if last_error is not None:
        trace.fail(trace.terminal_provider, last_error)
        raise last_error
    error = WebSearchConfigError(
        "web_search",
        "no entry of WEB_SEARCH_CHAIN could be tried (every entry was skipped; "
        "see the attempt rows on the Web Search page)",
    )
    trace.fail(trace.terminal_provider, error)
    raise error


# ------------------------------------------------------------- one entry


async def _run_provider(
    books: SearchKeyBooks,
    provider: BaseWebSearchProvider,
    query: str,
    *,
    allowed_domains: tuple[str, ...],
    blocked_domains: tuple[str, ...],
) -> EntryResult:
    async def one_try(index: int, key: str) -> WebSearchResponse:
        return await provider.search_once(
            query,
            key,
            index,
            max_results=_MAX_SEARCH_RESULTS,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )

    return await drive_keys(books, one_try)


async def _run_legacy(
    books: SearchKeyBooks, query: str, legacy_scrape: LegacyScrape
) -> EntryResult:
    async def one_try(_index: int, _key: str) -> list[dict[str, str]]:
        return await legacy_scrape(query)

    return await drive_keys(books, one_try)


async def drive_keys(
    books: SearchKeyBooks,
    one_try: Callable[[int, str], Awaitable[object]],
) -> EntryResult:
    """Walk one entry's keys exactly as ``RotatingProvider`` walks a pool's.

    Runs inside ``paused_ladder()`` with a private ladder installed, so every
    try and every credential decision the shared objects record is this
    entry's alone and never reaches the request's own row.
    """
    with paused_ladder():
        trace = install_ladder_trace()
        ladder = trace.slot()
        result = EntryResult()
        attempted: set[int] = set()
        last_failure: BaseException | None = None
        last_error: BaseException | None = None
        while len(attempted) < books.key_count:
            index = await books.state.acquire(
                books.unavailable_now() | frozenset(attempted)
            )
            if index < 0:
                wait = await books.state.shortest_cooldown_remaining()
                result.key_index = NO_CREDENTIAL_INDEX
                result.tries.append(
                    {
                        "key_index": NO_CREDENTIAL_INDEX,
                        "key_label": NO_CREDENTIAL_LABEL,
                        "kind": "pool_benched",
                        "retry_after": _rounded(wait),
                    }
                )
                result.failure, result.error = _pool_exhausted(books, wait)
                return result
            if index in attempted:
                # The pool handed back a key this search already spent: it
                # has nothing better left (the model rule; reaching past the
                # pool would bypass its health checks).
                break
            attempted.add(index)
            result.key_index = index
            first_try = len(ladder.tries)
            first_decision = len(ladder.decisions)
            limiter = books.limiters[index]
            try:
                value = await limiter.execute_with_retry(
                    one_try,
                    index,
                    books.keys[index],
                    provider_failure_override=books.translate,
                )
            except WebSearchConfigError as error:
                # A missing key or base URL is the operator's, not the key's:
                # nothing is charged and the entry is skipped.
                result.tries.extend(_key_tries(books, index, ladder, first_try))
                result.skipped = f"not configured: {error.message}"
                return result
            except Exception as error:
                failure = _classify(books, index, error)
                last_failure, last_error = failure, error
                rotate = await books.state.report_failure(index, failure)
                result.tries.extend(
                    _key_tries(
                        books,
                        index,
                        ladder,
                        first_try,
                        first_decision=first_decision,
                        rotated=rotate,
                    )
                )
                if not rotate:
                    result.failure, result.error = failure, error
                    return result
                continue
            await books.state.report_success(index)
            result.tries.extend(_key_tries(books, index, ladder, first_try))
            result.value = value
            return result
        if last_failure is None or last_error is None:
            # Unreachable with a non-empty pool: ``acquire`` hands out at
            # least one key before it can repeat one.
            last_failure = ApplicationUnavailableError(
                f"{books.provider_id}: no key could be tried"
            )
            last_error = WebSearchUpstreamError(
                books.provider_id, "no key could be tried"
            )
        result.failure, result.error = last_failure, last_error
        return result


def _classify(books: SearchKeyBooks, index: int, error: Exception) -> BaseException:
    """The ``ExecutionFailure`` the pool and the chain decide this try on.

    A search adapter's error goes through the search classification
    (``SearchKeyBooks.translate``). Anything else -- the scraper's raw
    ``httpx`` errors -- through the model path's own classifier, exactly as a
    model provider's leaf classifies before its pool sees the failure.

    A 429 also blocks that key's own limiter for the wait the operator's
    policy resolves to, as ``classify_provider_failure`` does for a model
    provider with ``RATE_LIMIT_ROUTES_AROUND_MODEL`` off: the step-over and
    the next ``acquire`` read that window.
    """
    limiter = books.limiters[index]
    translated = books.translate(error)
    if translated is None:
        return classify_provider_failure(
            error,
            provider_name=books.provider_id,
            read_timeout_s=None,
            request_id=None,
            mark_rate_limited=limiter.extend_reactive_block,
            cooldown=books.cooldown,
            mark_rate_limited_enabled=True,
        )
    if translated.kind is FailureKind.RATE_LIMIT:
        wait = books.cooldown.resolve(translated.retry_after_seconds)
        if wait > 0:
            limiter.extend_reactive_block(wait)
    return translated


def _pool_exhausted(
    books: SearchKeyBooks, wait: float
) -> tuple[ApplicationUnavailableError, WebSearchError]:
    """The model path's sentence for a pool with nothing to hand out.

    The client's ``error_code`` follows the cause: credits and a rate limit
    are ``too_many_requests``, a locked-out key is ``unavailable``.
    """
    if books.state.benched_only_for_credits():
        sentence = (
            f"All API keys for this provider reported exhausted credits.{_CREDITS_HINT}"
        )
        return (
            ApplicationUnavailableError(sentence),
            WebSearchQuotaError(books.provider_id, sentence),
        )
    sentence = (
        "All API keys for this provider are in cooldown. "
        f"Retry in {max(1, int(wait))}s."
        f"{limit_hint('RATE_LIMIT_COOLDOWN_SECONDS')}"
    )
    states = {entry["state"] for entry in books.state.get_metrics()}
    surface: WebSearchError = (
        WebSearchRateLimitError(books.provider_id, sentence)
        if "COOLDOWN" in states
        else WebSearchAuthError(books.provider_id, sentence)
    )
    return ApplicationUnavailableError(sentence), surface


def _key_tries(
    books: SearchKeyBooks,
    index: int,
    ladder: AttemptLadder,
    first_try: int,
    *,
    first_decision: int | None = None,
    rotated: bool | None = None,
) -> list[dict[str, object]]:
    """This key's tries, read back out of the private ladder."""
    label = books.label(index)
    rows: list[dict[str, object]] = []
    for number, entry in enumerate(ladder.tries[first_try:], start=1):
        rows.append(
            {
                "key_index": index,
                "key_label": label,
                "try": number,
                "status": entry.status,
                "kind": entry.error_kind or entry.kind,
                "retry_after": _rounded(entry.retry_after),
                "ms": _rounded(entry.upstream_ms),
                "slept_s": (
                    None
                    if entry.waited_ms is None
                    else _rounded(entry.waited_ms / 1000)
                ),
                "ok": entry.status is None
                and entry.kind is None
                and entry.error_kind is None,
            }
        )
    if first_decision is not None and rows:
        decisions = ladder.decisions[first_decision:]
        decision = decisions[-1] if decisions else None
        rows[-1]["charged"] = None if decision is None else decision.cls
        rows[-1]["benched_for_s"] = (
            None if decision is None else _rounded(decision.benched_for_s)
        )
        rows[-1]["decision"] = None if decision is None else decision.reason
        rows[-1]["rotated"] = rotated
    return rows


async def _bounded(
    work: Awaitable[EntryResult],
    deadline: float | None,
    entry: str,
    settings: Settings,
) -> EntryResult:
    """Run one entry inside what is left of ``FALLBACK_TOTAL_TIMEOUT``."""
    if deadline is None:
        return await work
    remaining = max(0.0, deadline - time.monotonic())
    try:
        async with asyncio.timeout(remaining):
            return await work
    except TimeoutError:
        # The model executor's total budget ends the attempt in flight and,
        # with no time left, the route: nothing behind it is started.
        error = _budget_spent_error(entry, settings.fallback_total_timeout)
        return EntryResult(
            failure=ApplicationUnavailableError(error.message),
            error=error,
            budget_spent=True,
        )


def _budget_spent_error(entry: str, seconds: float) -> WebSearchUpstreamError:
    return WebSearchUpstreamError(
        entry,
        f"the search's {seconds:g}s budget was spent"
        f"{limit_hint('FALLBACK_TOTAL_TIMEOUT')}",
        kind_detail=KIND_DETAIL_TIMEOUT,
    )


def _record_route_failure(
    health: RouteHealthRegistry, ref: str, failure: BaseException
) -> None:
    """Route health, recorded as ``ProviderExecutor`` records a failed attempt."""
    kind = failure_kind(failure)
    execution_failure = find_execution_failure(failure)
    health.record_failure(
        ref,
        failure_kind=kind.value if kind is not None else None,
        status_code=(
            None if execution_failure is None else execution_failure.status_code
        ),
    )


def _bench_reason(health: RouteHealthRegistry, ref: str) -> str:
    why = health.why(ref)
    return "benched" if why is None else why.sentence()


def _throttle_lookup(refs: tuple[str, ...]) -> Callable[[str], float | None]:
    """``usable_indexes``' rate-based lookup: an entry's live key throttle."""
    from_ref = {ref: ref.removeprefix(ENTRY_REF_PREFIX) for ref in refs}

    def lookup(ref: str) -> float | None:
        provider_id = from_ref.get(ref)
        if provider_id is None:
            return None
        books = cached_search_key_books(provider_id)
        return None if books is None else books.throttle_remaining()

    return lookup


# ------------------------------------------------------------- the rows


def _route_context(
    settings: Settings, entries: tuple[str, ...], paused: frozenset[str]
) -> dict[str, object]:
    return {
        "selected_provider": settings.web_search_provider,
        "fallback_policy": settings.web_search_fallback_policy,
        "resolved_provider_path": list(entries),
        "legacy_fallback": WEB_SEARCH_CHAIN_LEGACY_ENTRY in entries,
        "disabled": False,
        "max_results": _MAX_SEARCH_RESULTS,
        "digest_chars": settings.websearch_digest_chars,
        "digest_answer": settings.websearch_digest_answer,
        "search_chain": list(entries),
        "search_chain_paused": sorted(paused),
    }


def _skip_rows(
    trace: RouteTrace,
    entries: tuple[str, ...],
    positions: range | tuple[int, ...],
    reason: str,
    query: str,
    context: Mapping[str, object],
) -> None:
    for position in positions:
        _skip_row(trace, entries[position], position, reason, query, context)


def _skip_row(
    trace: RouteTrace,
    entry: str,
    position: int,
    reason: str,
    query: str,
    context: Mapping[str, object],
) -> None:
    now = time.time()
    emit_search_outcome(
        SearchOutcome(
            ts_epoch=now,
            ts_iso=_iso(now),
            provider=entry,
            key_index=0,
            key_label="",
            query=query,
            results_count=0,
            duration_ms=0.0,
            status=SKIPPED_STATUS,
            error_kind=None,
            error_message=f"skipped: {reason}"[:_ERROR_MESSAGE_LOG_CHARS],
            cost_usd=None,
            route_id=trace.route_id,
            # Not an attempt: it carries the number the next attempt will.
            attempt_number=trace.attempt_count + 1,
            input_payload=None,
            output_payload=None,
            provider_config={"provider_id": entry, "route": dict(context)},
            chain_position=position,
            outcome=OUTCOME_SKIPPED,
            failure_kind=None,
            tries_json=None,
        )
    )


def _emit_row(
    trace: RouteTrace,
    *,
    entry: str,
    provider: BaseWebSearchProvider | None,
    position: int,
    attempt_number: int,
    ts_epoch: float,
    started: float,
    result: EntryResult,
    query: str,
    context: Mapping[str, object],
    allowed_domains: tuple[str, ...],
    blocked_domains: tuple[str, ...],
    legacy_results: list[dict[str, str]] | None = None,
) -> None:
    """One attempt row for one tried entry, in today's shape plus the chain's."""
    if provider is not None and not provider.SUPPORTS_DOMAINS:
        allowed_domains = ()
        blocked_domains = ()
    input_payload = _search_input_payload(
        query,
        max_results=_MAX_SEARCH_RESULTS,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
    )
    key_index = max(0, result.key_index)
    key_label = (
        provider.key_label(key_index)
        if provider is not None and result.key_index >= 0
        else ""
    )
    if provider is not None:
        provider_config = _provider_config_snapshot(provider, context)
    else:
        provider_config = {
            "provider_id": WEB_SEARCH_CHAIN_LEGACY_ENTRY,
            "credential_rotation": "none",
            "credential_count": 0,
            "base_url": None,
            "proxy": None,
            "http_timeout_seconds": _REQUEST_TIMEOUT_S,
            "supports_domain_filters": False,
            "options": {},
            "route": dict(context),
        }
    tries_json = json.dumps(result.tries, separators=(",", ":"), default=str)
    if result.skipped is not None:
        status = SKIPPED_STATUS
        outcome = OUTCOME_SKIPPED
        error_message: str | None = f"skipped: {result.skipped}"
        error_kind: str | None = None
        kind_value: str | None = None
        results_count = 0
        cost = None
        output_payload: Mapping[str, object] | None = None
    elif result.ok:
        status = "success"
        outcome = OUTCOME_SUCCEEDED
        error_message = None
        error_kind = None
        kind_value = None
        cost = None
        if isinstance(result.value, WebSearchResponse):
            results_count = len(result.value.results)
            cost = result.value.cost_usd
            output_payload = _response_output_payload(result.value)
        else:
            rows = legacy_results or []
            results_count = len(rows)
            output_payload = {
                "provider": WEB_SEARCH_CHAIN_LEGACY_ENTRY,
                "query": query,
                "answer": None,
                "results": rows,
                "result_count": results_count,
                "key_index": 0,
                "cost_usd": None,
            }
    else:
        assert result.error is not None
        status = "error"
        outcome = OUTCOME_FAILED
        error_message = str(result.error)
        error_kind = _error_kind(result.error)
        kind = failure_kind(result.failure) if result.failure is not None else None
        kind_value = None if kind is None else kind.value
        results_count = 0
        cost = None
        output_payload = _error_output_payload(result.error)
    emit_search_outcome(
        SearchOutcome(
            ts_epoch=ts_epoch,
            ts_iso=_iso(ts_epoch),
            provider=entry,
            key_index=key_index,
            key_label=key_label,
            query=query,
            results_count=results_count,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            status=status,
            error_kind=error_kind,
            error_message=(
                error_message[:_ERROR_MESSAGE_LOG_CHARS] if error_message else None
            ),
            cost_usd=cost,
            route_id=trace.route_id,
            attempt_number=attempt_number,
            input_payload=input_payload,
            output_payload=output_payload,
            provider_config=provider_config,
            chain_position=position,
            outcome=outcome,
            failure_kind=kind_value,
            tries_json=tries_json,
        )
    )


def _error_kind(error: BaseException) -> str:
    """The ``error_kind`` column, in the vocabulary rows without a chain use."""
    if isinstance(error, WebSearchError):
        return error.kind
    if isinstance(error, httpx.HTTPError):
        return "upstream"
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    return "internal"


def _iso(ts_epoch: float) -> str:
    return datetime.fromtimestamp(ts_epoch, tz=UTC).isoformat()


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)
