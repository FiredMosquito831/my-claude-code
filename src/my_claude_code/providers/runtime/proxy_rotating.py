"""Provider wrapper that moves one request along a chain of egress addresses.

The credential pool's own docstring says why this exists:
``UNAVAILABLE`` rotates a key because *"another key means another
connection"* (``providers/credential_rotation.py:110-113``). That is the
existing code admitting the connection is a rotatable resource distinct from
the credential. This wrapper names it.

Where it sits
-------------

Below the credential pool, inside :func:`~.factory._create_single_provider`,
as a fan-out over ``dataclasses.replace(config, proxy=...)`` -- the same line
that already fans a provider out per credential::

    RotatingProvider                     credential pool, unchanged
    +-- ProxyRotatingProvider   key A    this module
    |   +-- OpenAIChatProvider  key A, proxy P1
    |   +-- OpenAIChatProvider  key A, proxy P2
    |   +-- OpenAIChatProvider  key A, Direct
    +-- ProxyRotatingProvider   key B
        +-- ...

Everything above keeps receiving one object satisfying ``BaseProvider``, and
with no chain configured this class is never constructed at all.

What it may not do
------------------

Four rules, each of which is a way the thing above it would silently stop
working:

1. **On exhaustion it re-raises the last error verbatim.** Never a synthesised
   ``ApplicationUnavailableError``. Wrapping would make
   ``credential_failure_class`` answer ``None``, the credential pool would stop
   charging health, and the (key, model) bench and the route-around would both
   go quiet without anything failing.
2. **It never raises ``ModelRateLimited``.** That is the credential pool's
   signal to the executor and it carries a key index this wrapper does not
   have.
3. **It holds no clock of its own.** No ``asyncio.wait_for``, no ``timeout=``,
   no deadline arithmetic. It spends the attempt's budget through ordinary
   awaits and is cancelled by the same ``asyncio.wait`` the credential pool is.
   Three dead addresses at one connect timeout each is the real risk, and the
   reachability bench plus the switch bound are what contain it.
4. **It never switches after the first SSE chunk.** Once output has started,
   moving address would duplicate or corrupt the response -- the rule
   ``runtime/rotating.py:60-63`` states and enforces the same way, by pulling
   the first chunk inside the ``try``.

What moves it along
-------------------

Two independent answers, exactly as the credential pool keeps health and
rotation separate:

- A **reachability** failure -- the proxy refused the CONNECT, timed out
  connecting, or answered ``407`` -- always advances, whatever the operator
  selected. The thing that failed *is* the address, and making that
  configurable would let somebody build a chain that cannot route around a
  dead entry. It benches the address globally, on the escalating ladder in
  ``core/proxy_rotation.py``.
- A **triggering** failure -- the upstream answered with a kind in the
  operator's chip set -- advances and benches that address for this provider
  (or for this provider and credential, under the ``credential`` scope).
- Anything else re-raises immediately and untouched, which is what hands the
  request to the next *model*, exactly as today.

A note on 502 and 503: a proxy and an origin produce byte-identical ones, and
reading every 502 as the proxy's fault would override the operator's own chip
set on the commonest upstream fault there is. They stay on the ``upstream`` and
``unavailable`` chips, which are selectable and off by default.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

import httpx
from loguru import logger

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.credential_rotation import RotationEngine
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    failure_kind,
    find_execution_failure,
)
from my_claude_code.core.proxy_attribution import DIRECT_PROXY_LABEL, record_proxy
from my_claude_code.core.proxy_rotation import (
    PROXY_HEALTH,
    PROXY_INTERCEPTION,
    PROXY_REACHABILITY,
    PROXY_REFUSED_TRIGGER_KINDS,
    PROXY_TUNING,
)
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningDialect,
    ReasoningPolicy,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig, ProxyChainPlan
from my_claude_code.providers.http import maybe_await_aclose

#: Proxy-side HTTP statuses that are unambiguously the proxy's own answer and
#: never an origin's. ``407`` is the only one: it is defined as "the *proxy*
#: requires authentication" and an origin cannot send it through a tunnel.
PROXY_STATUS_CODES = frozenset({407})

#: Transport failures that mean the hop to the proxy did not complete. Read off
#: the exception chain, because providers wrap their SDK's errors and the
#: original is carried as ``__cause__``.
_REACHABILITY_TYPES: tuple[type[BaseException], ...] = (
    httpx.ProxyError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
)

_CAUSE_DEPTH = 8


def _chain(error: BaseException) -> list[BaseException]:
    """The exception and the causes behind it, bounded."""

    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(seen) < _CAUSE_DEPTH:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def proxy_reachability_failure(error: BaseException, *, proxied: bool) -> str | None:
    """Name the way *the proxy* failed, or ``None`` if it did not.

    ``proxied`` is False for the Direct rung, where a refused connection is the
    provider's and benching "the address" would bench this machine.
    """

    if not proxied:
        return None
    for link in _chain(error):
        if isinstance(link, _REACHABILITY_TYPES):
            return type(link).__name__
    failure = find_execution_failure(error)
    status = None if failure is None else failure.status_code
    if status in PROXY_STATUS_CODES:
        return f"proxy {status}"
    return None


class ProxyRotationState:
    """Which rung serves this request, and what a failure did to the chain.

    A thin async adapter over the shared :class:`RotationEngine`, exactly as
    ``providers/credential_rotation.CredentialRotationState`` is -- the lock
    semantics and the classification live here, every health-transition rule
    lives once in the engine, and the engine itself is imported and never
    edited.
    """

    def __init__(
        self,
        leg_count: int,
        policy: str,
        *,
        labels: Sequence[str],
        provider_id: str,
        scope: str = "provider",
        clock=time.monotonic,
    ) -> None:
        canonical = "failover" if policy == "on_error" else policy
        if canonical not in {"single", "round_robin", "least_used", "failover"}:
            canonical = "failover"
        self._engine = RotationEngine(
            leg_count, policy=canonical, tuning=PROXY_TUNING, clock=clock
        )
        self._labels = tuple(labels)
        self._provider_id = provider_id
        self._scope = scope
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def policy(self) -> str:
        return self._engine.policy

    def _label(self, index: int) -> str:
        if 0 <= index < len(self._labels):
            return self._labels[index]
        return DIRECT_PROXY_LABEL

    def _unreachable(self) -> frozenset[int]:
        """Rungs no request may go out through right now.

        Two reasons, and they are not the same reason. An address on the
        reachability ladder failed, and since 7.19.0 it stays out until a check
        **passes** -- the tier expiring makes it due for a re-probe, not
        selectable, which is why the question asked here is
        :meth:`~my_claude_code.core.proxy_rotation.ReachabilityLedger.is_unhealthy`
        and not ``remaining() > 0``. An address in :data:`PROXY_INTERCEPTION`
        was measured terminating TLS, and is refused until a later check says
        otherwise -- an address already in a chain when the checker finds that
        out is held out of selection here rather than waiting for somebody to
        edit the chain, because the whole point of finding it is not to route
        through it.
        """

        return frozenset(
            index
            for index in range(len(self._labels))
            if self._labels[index] != DIRECT_PROXY_LABEL
            and (
                PROXY_REACHABILITY.is_unhealthy(self._labels[index])
                or PROXY_INTERCEPTION.is_refused(self._labels[index])
            )
        )

    def refused(self) -> frozenset[int]:
        """Rungs the checker measured terminating TLS.

        Kept apart from :meth:`_unreachable` because the two are relaxed
        differently: a bench is a preference and this is a prohibition. Every
        branch of :meth:`acquire` subtracts this set, including the one that
        relaxes the blocklist when nothing is free, because "everything else is
        benched" is not a reason to carry a credential through a tunnel
        somebody is reading.
        """

        return frozenset(
            index
            for index in range(len(self._labels))
            if self._labels[index] != DIRECT_PROXY_LABEL
            and PROXY_INTERCEPTION.is_refused(self._labels[index])
        )

    def scope_key(self, credential: str | None) -> str:
        """The bench key for this provider under the configured scope."""

        if self._scope == "credential" and credential:
            return f"{self._provider_id}:{credential}"
        return self._provider_id or "-"

    async def acquire(
        self, attempted: frozenset[int], scope_key: str, *, relax: bool = True
    ) -> int:
        """Pick a rung, or ``-1`` when this request has nothing left to try.

        Benched rungs are steered around while anything else is free. What
        happens when nothing is free depends on ``relax``, and that is the one
        decision this method makes:

        * ``relax=True`` is the behaviour every release up to 7.18 had, kept
          byte for byte for a chain whose operator turned the direct fallback
          off: the blocklist is dropped and a benched rung is dispatched into,
          because a chain that refused to dispatch would answer an empty stream
          and there is no honest error to raise in its place.
        * ``relax=False`` is what a chain with the direct fallback on does --
          which is every chain by default. There *is* an honest answer now, and
          it is this machine's own address, so a benched rung is never
          dispatched into and the caller goes Direct instead.

        Neither branch may pick a refused rung: a bench is a preference and an
        intercepted tunnel is a prohibition. A rung this request already spent
        is never handed back, which is where the loop actually terminates.
        """

        count = len(self._labels)
        refused = self.refused()
        spent = attempted | refused
        async with self._lock:
            avoid = spent | self._unreachable()
            selected = self._engine.choose(avoid, scope_key)
            if selected is None and relax:
                selected = self._engine.choose(spent, None)
                if selected is None or selected in spent:
                    remaining = [index for index in range(count) if index not in spent]
                    selected = remaining[0] if remaining else None
            if selected is None or selected in spent:
                return -1
            self._engine.mark_acquired(selected)
        PROXY_HEALTH.note_acquired(self._provider_id, self._label(selected))
        return selected

    async def report_success(self, index: int) -> None:
        label = self._label(index)
        async with self._lock:
            self._engine.succeed(index)
        PROXY_REACHABILITY.note_success(label)
        PROXY_HEALTH.note_success(self._provider_id, label)

    async def report_failure(
        self,
        index: int,
        error: BaseException,
        *,
        scope_key: str,
        triggers: frozenset[str],
    ) -> str:
        """Record one failure against a rung; name what kind of failure it was.

        The two questions are independent, the way the credential pool keeps
        them: whether this address's record moves, and whether another address
        is worth trying.

        Returns ``"reachability"`` when the *address* failed, ``"trigger"``
        when the upstream answered with a class the operator armed, and ``""``
        when neither -- which is the caller's signal to re-raise. The two named
        answers both advance, and the caller charges them to different bounds:
        an operator arming a trigger chip asked for a switch, while a dead
        address is not a switch anybody asked for and is bounded by
        ``PROXY_MAX_LIVE_FAILURES`` instead.
        """

        label = self._label(index)
        reachability = proxy_reachability_failure(
            error, proxied=label != DIRECT_PROXY_LABEL
        )
        if reachability is not None:
            benched = PROXY_REACHABILITY.note_failure(label, reachability)
            PROXY_HEALTH.note_failure(
                self._provider_id,
                label,
                reason=f"{reachability} -- benched {benched:.0f}s",
            )
            return "reachability"

        kind = failure_kind(error)
        if (
            kind is None
            or kind.value in PROXY_REFUSED_TRIGGER_KINDS
            or kind.value not in triggers
        ):
            # Not about the address at all. Nothing is charged and nothing
            # advances: raising here is what hands the request to the next
            # model, exactly as it does with no chain configured.
            return ""

        failure = find_execution_failure(error)
        # The provider's own published wait when it published one; the engine
        # substitutes ``PROXY_COOLDOWN_SECONDS_DEFAULT`` when it did not. No
        # second cooldown number is invented here.
        #
        # Only a ``RATE_LIMIT`` carries a *published* number. On a ``QUOTA``
        # the same field holds the operator's ``RATE_LIMIT_COOLDOWN_SECONDS``,
        # placed there by the classifier as its evidence flag -- a cooldown for
        # a *credential*, not for an address, and honouring it here would bench
        # an exhausted address for a minute when the operator asked for five.
        retry_after = (
            failure.retry_after_seconds
            if failure is not None and failure.kind is FailureKind.RATE_LIMIT
            else None
        )
        async with self._lock:
            self._engine.fail(
                index, "rate_limit", retry_after=retry_after, model=scope_key
            )
            slot = self._engine.slot(index)
            until = slot.model_benches.get(scope_key)
            benched_for = 0.0 if until is None else max(0.0, until - self._clock())
        PROXY_HEALTH.note_failure(
            self._provider_id,
            label,
            benched_for=benched_for,
            reason=f"{kind.value} -- benched {benched_for:.0f}s for {scope_key}",
        )
        return "trigger"

    def selectable_indexes(self, scope_key: str) -> tuple[int, ...]:
        held_out = self._unreachable() | self.refused()
        return tuple(
            index
            for index in self._engine.selectable_indexes(scope_key)
            if index not in held_out
        )

    def get_metrics(self) -> list[dict[str, Any]]:
        """Per-rung snapshots, for tests and for a future admin surface."""

        return [
            dict(PROXY_HEALTH.snapshot(self._provider_id, self._label(index)))
            | {"index": index, "label": self._label(index)}
            for index in range(len(self._labels))
        ]


class ProxyLegPool:
    """The legs of one chain, built on first use and closed when idle.

    Up to 7.18 the factory built every leg of every chain for every credential
    eagerly: five keys and a twelve-entry chain was sixty provider objects, and
    therefore sixty ``httpx.AsyncClient`` connection pools, sixty rate limiters
    and sixty recovery ladders, constructed at startup for a request that would
    touch two of them. That is why chains were capped at twelve. Removing the
    cap without removing the cost would have been the wrong half of the change.

    So a leg is a *string* until something goes out through it. The pool builds
    one on first acquire, keeps it, and closes the ones that have been idle
    longest once more than ``max_open`` are open -- a plain LRU over an ordered
    dict, because ``dict`` has preserved insertion order since 3.7 and a second
    data structure would earn nothing here.

    **A leg in use is never closed.** The counter is incremented before the
    stream starts and decremented in a ``finally``, and eviction skips anything
    the counter says is live. A chain whose every open leg is mid-stream
    therefore exceeds ``max_open`` for as long as that is true, which is the
    right way round: closing a client out from under a response in flight would
    truncate it.
    """

    def __init__(
        self,
        build: Callable[[int], BaseProvider],
        *,
        max_open: int = 0,
    ) -> None:
        self._build = build
        self._max_open = max(0, int(max_open))
        self._open: dict[int, BaseProvider] = {}
        self._in_use: dict[int, int] = {}

    @property
    def open_count(self) -> int:
        return len(self._open)

    @property
    def open_indexes(self) -> tuple[int, ...]:
        return tuple(self._open)

    def providers(self) -> tuple[BaseProvider, ...]:
        """Every leg that currently holds a client. Never builds one."""

        return tuple(self._open.values())

    def get(self, index: int) -> BaseProvider:
        """The leg at ``index``, building it if this is its first use."""

        provider = self._open.pop(index, None)
        if provider is None:
            provider = self._build(index)
        # Re-inserting at the end is the "recently used" half of the LRU.
        self._open[index] = provider
        return provider

    def hold(self, index: int) -> None:
        self._in_use[index] = self._in_use.get(index, 0) + 1

    def release(self, index: int) -> None:
        held = self._in_use.get(index, 0) - 1
        if held > 0:
            self._in_use[index] = held
        else:
            self._in_use.pop(index, None)

    def _evictable(self) -> list[int]:
        if self._max_open <= 0:
            return []
        overflow = len(self._open) - self._max_open
        if overflow <= 0:
            return []
        victims: list[int] = []
        for index in self._open:
            if len(victims) >= overflow:
                break
            if not self._in_use.get(index):
                victims.append(index)
        return victims

    async def reap(self) -> int:
        """Close the idle legs above the bound. Returns how many were closed."""

        closed = 0
        for index in self._evictable():
            provider = self._open.pop(index, None)
            if provider is None:  # pragma: no cover - single-threaded
                continue
            try:
                await provider.cleanup()
            except Exception as exc:
                # An eviction is housekeeping. A client that will not close is
                # not a reason to fail the request that triggered the sweep.
                logger.debug(
                    "Proxy leg {} did not close cleanly: exc_type={}",
                    index,
                    type(exc).__name__,
                )
            closed += 1
        return closed

    async def close_all(self) -> None:
        """Close every open leg, collecting failures the way ``cleanup`` does."""

        providers = list(self._open.values())
        self._open.clear()
        self._in_use.clear()
        errors: list[Exception] = []
        for provider in providers:
            try:
                await provider.cleanup()
            except Exception as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if len(errors) > 1:
            raise ExceptionGroup("One or more proxy leg cleanups failed", errors)


@dataclass(slots=True)
class _LegAttempt:
    """What one rung answered: a stream to yield, or an error and a verdict."""

    done: bool = False
    empty: bool = False
    stream: Callable[[], AsyncIterator[str]] | None = None
    error: Exception | None = None
    #: ``"reachability"``, ``"trigger"`` or ``""`` -- see
    #: :meth:`ProxyRotationState.report_failure`.
    advance: str = ""

    def chunks(self) -> AsyncIterator[str]:
        """The rung's output. Only meaningful when :attr:`done`."""

        factory = self.stream
        if factory is None:
            return _no_chunks()
        return factory()


async def _no_chunks() -> AsyncIterator[str]:
    """An upstream that closed without sending anything. Not an error."""

    return
    yield ""  # pragma: no cover - unreachable; makes this an async generator


class ProxyRotatingProvider(BaseProvider):
    """Fan one credential's requests out over its chain of egress addresses.

    Every sub-provider here holds the *same* credential and differs only in
    ``ProviderConfig.proxy``, so anything that is a property of the credential
    -- its masked label, its dialect, its preflight -- is answered by the first
    one, the way ``RotatingProvider`` already answers ``list_model_ids`` from
    its own first sub-provider.

    ``legs`` is either a sequence of already-built providers -- what a test
    hands it -- or a callable that builds the leg at an index, which is what
    the factory hands it and what makes a three-hundred-entry chain cost three
    hundred strings. Index ``len(labels)`` is the **direct** leg: not a rung of
    the chain, built only if the request runs out of healthy addresses and the
    chain's ``direct_fallback`` is on.
    """

    def __init__(
        self,
        config: ProviderConfig,
        legs: Sequence[BaseProvider] | Callable[[int], BaseProvider],
        state: ProxyRotationState,
        *,
        labels: Sequence[str],
        plan: ProxyChainPlan,
        provider_id: str = "",
        max_open_legs: int = 0,
        max_live_failures: int = 0,
    ) -> None:
        super().__init__(config)
        self._labels = tuple(labels)
        if len(self._labels) < 2:
            raise ValueError("ProxyRotatingProvider requires at least two rungs")
        build: Callable[[int], BaseProvider]
        # ``isinstance`` on the sequence rather than ``callable`` on the other
        # arm: a Sequence is the narrow, closed case and a factory is anything
        # that answers to an index, so testing for the closed one is what makes
        # both arms readable to a type checker and to a person.
        if not isinstance(legs, Sequence):
            build = legs
            has_direct_leg = True
        else:
            prebuilt: tuple[BaseProvider, ...] = cast(
                tuple[BaseProvider, ...], tuple(legs)
            )
            if len(prebuilt) < 2:
                raise ValueError("ProxyRotatingProvider requires at least two rungs")
            # A caller that hands over already-built legs supplies the direct
            # one by making the sequence one longer than the chain. When it
            # does not there IS no direct leg, and the fallback is off however
            # the chain is configured -- a chain that cannot go direct must not
            # end up worse off than it was before the fallback existed, so it
            # keeps the pre-7.19 relax instead.
            has_direct_leg = len(prebuilt) > len(self._labels)

            def build(
                index: int, _built: tuple[BaseProvider, ...] = prebuilt
            ) -> BaseProvider:
                if 0 <= index < len(_built):
                    return _built[index]
                raise IndexError(f"no proxy leg at index {index}")

        self._pool = ProxyLegPool(build, max_open=max_open_legs)
        self._state = state
        self._plan = plan
        self._provider_id = provider_id
        self._triggers = frozenset(plan.on)
        self._max_switches = max(1, int(plan.max_switches))
        self._max_live_failures = max(0, int(max_live_failures))
        self._direct_fallback = (
            bool(getattr(plan, "direct_fallback", True)) and has_direct_leg
        )
        #: The rung index the direct leg answers to. One past the chain, so it
        #: can never be chosen by the rotation engine and can never be confused
        #: with a rung the operator wrote down.
        self._direct_index = len(self._labels)

    # ---------------------------------------------------------------- shape

    @property
    def credential_label(self) -> str | None:
        """One credential, many addresses: the sub-providers all agree."""

        return self._pool.get(0).credential_label

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect | None:
        return self._pool.get(0).reasoning_dialect(model_id)

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        self._pool.get(0).preflight_stream(request, reasoning=reasoning)

    async def list_model_ids(self) -> frozenset[str]:
        return await self._pool.get(0).list_model_ids()

    async def list_model_infos(self):
        return await self._pool.get(0).list_model_infos()

    def throttle_remaining(self, model: str | None = None) -> float:
        """0 while any rung can serve; otherwise the shortest wait until one can.

        Each rung is a separate client with its own limiter, which is the
        deliberate consequence of the fan-out on a provider metered per
        address: four addresses really are four allowances. Routing asks for
        the best case, so that is what it is told.

        Only the legs that are *open* are asked. A leg that has never been
        built has never sent anything, so its limiter would answer 0 -- which
        is the same answer this returns for a chain with nothing open, without
        building a client to hear it say so.
        """

        return min(
            (provider.throttle_remaining(model) for provider in self._pool.providers()),
            default=0.0,
        )

    async def cleanup(self) -> None:
        await self._pool.close_all()

    def proxy_health(self) -> list[dict[str, Any]]:
        """Per-rung health snapshots, index-aligned with the chain."""

        return self._state.get_metrics()

    def open_leg_count(self) -> int:
        """How many legs currently hold an HTTP client. For tests and the page."""

        return self._pool.open_count

    # ----------------------------------------------------------------- loop

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        return self._stream_with_rotation(
            request, input_tokens, request_id=request_id, reasoning=reasoning
        )

    async def _stream_with_rotation(
        self,
        request: MessagesRequest,
        input_tokens: int,
        *,
        request_id: str | None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        attempted: set[int] = set()
        last_error: Exception | None = None
        switches = 0
        #: Addresses that failed while carrying *this* request. An address the
        #: chain skipped because it was already known unhealthy is not one of
        #: these, and that is the point: skipping costs nothing, so a chain of
        #: three hundred entries of which two are alive spends two attempts.
        live_failures = 0
        scope_key = self._state.scope_key(self._scope_credential())
        # With the direct fallback on there is an honest answer when every
        # address is benched, so the blocklist is never relaxed and a benched
        # address is never dispatched into. With it off, the pre-7.19 relax is
        # kept exactly: the chain dispatches into a bench rather than fail.
        relax = not self._direct_fallback

        while len(attempted) < len(self._labels):
            if self._max_live_failures and live_failures >= self._max_live_failures:
                break
            index = await self._state.acquire(
                frozenset(attempted), scope_key, relax=relax
            )
            if index < 0 or index in attempted:
                break
            attempted.add(index)
            outcome = await self._attempt(
                index,
                request,
                input_tokens,
                request_id=request_id,
                reasoning=reasoning,
                scope_key=scope_key,
            )
            if outcome.done:
                async for chunk in outcome.chunks():
                    yield chunk
                return
            error = outcome.error
            if error is None:  # pragma: no cover - an attempt is done or errored
                break
            last_error = error
            if not outcome.advance:
                # Not the address's fault. The same exception object the
                # credential pool receives today, raised from the same place,
                # so everything above classifies it identically.
                raise error
            live_failures += 1
            if outcome.advance == "trigger":
                if switches >= self._max_switches:
                    # The operator's own switch bound, unchanged in meaning
                    # and unchanged in what spending it does: the error is
                    # re-raised rather than wrapped, because every switch costs
                    # wall-clock inside one attempt and the executor's
                    # deadlines have not moved.
                    raise error
                switches += 1

        if self._direct_fallback and DIRECT_PROXY_LABEL not in self._labels:
            # The final fallback, attempted at most once and never when the
            # operator already wrote Direct into the chain themselves -- that
            # rung is an ordinary rung and the loop above has had its turn at
            # it.
            outcome = await self._attempt(
                self._direct_index,
                request,
                input_tokens,
                request_id=request_id,
                reasoning=reasoning,
                scope_key=scope_key,
            )
            if outcome.done:
                async for chunk in outcome.chunks():
                    yield chunk
                return
            if outcome.error is not None:
                raise outcome.error
            return

        if last_error is not None:
            raise last_error

        # Nothing was tried and nothing failed: every rung of this chain is an
        # address the checker measured terminating TLS. There is no earlier
        # error to re-raise and no rung to fall back to, so the honest answer
        # is a classified UNAVAILABLE -- which the credential pool reads
        # exactly as it reads a dead socket, and which the executor hands to
        # the model fallback chain. Synthesised here and nowhere else: the
        # exhaustion path above still re-raises the last error verbatim.
        refused = self._state.refused()
        if refused:
            raise ExecutionFailure(
                kind=FailureKind.UNAVAILABLE,
                status_code=502,
                message=(
                    "Every proxy in this provider's chain breaks certificate "
                    "validation and has been refused: "
                    + ", ".join(sorted(self._labels[index] for index in refused))
                    + ". Test them on the Proxying page."
                ),
                retryable=False,
            )

    async def _attempt(
        self,
        index: int,
        request: MessagesRequest,
        input_tokens: int,
        *,
        request_id: str | None,
        reasoning: ReasoningPolicy,
        scope_key: str,
    ) -> _LegAttempt:
        """Open one rung and pull its first chunk.

        Everything that decides *whether* to move lives here; everything that
        decides *where* lives in the loop above. The first chunk is pulled
        inside this frame on purpose -- rule 4 of this module's contract: once
        output has started the request cannot move address, so the last moment
        at which moving is still safe is exactly the moment this returns.
        """

        label = (
            DIRECT_PROXY_LABEL if index >= len(self._labels) else self._labels[index]
        )
        # Every upstream try the leaf's retry frame records from here on
        # carries this address, without a provider signature changing. The
        # direct leg records ``Direct``, so the request log says so.
        record_proxy(label)

        provider = self._pool.get(index)
        self._pool.hold(index)
        try:
            await self._pool.reap()
        except Exception as exc:  # pragma: no cover - reap swallows its own
            logger.debug("Proxy leg reap failed: exc_type={}", type(exc).__name__)

        iterator = provider.stream_response(
            request,
            input_tokens,
            request_id=request_id,
            reasoning=reasoning,
        )
        try:
            first_chunk = await iterator.__anext__()
        except StopAsyncIteration:
            self._pool.release(index)
            await self._settle_success(index, label)
            return _LegAttempt(done=True, empty=True)
        except Exception as error:
            self._pool.release(index)
            await maybe_await_aclose(iterator)
            advance = await self._settle_failure(index, label, error, scope_key)
            return _LegAttempt(error=error, advance=advance)

        return _LegAttempt(
            done=True,
            stream=lambda: self._drain(index, label, iterator, first_chunk, scope_key),
        )

    async def _drain(
        self,
        index: int,
        label: str,
        iterator: AsyncIterator[str],
        first_chunk: str,
        scope_key: str,
    ) -> AsyncIterator[str]:
        settled = False
        try:
            yield first_chunk
            async for chunk in iterator:
                yield chunk
        except Exception as error:
            # Output has started, so this request cannot move address -- but
            # the failure still has to count, or an address that consistently
            # dies mid-stream would never be benched.
            settled = True
            await maybe_await_aclose(iterator)
            await self._settle_failure(index, label, error, scope_key)
            raise
        finally:
            self._pool.release(index)
            if not settled:
                await maybe_await_aclose(iterator)
        await self._settle_success(index, label)

    async def _settle_success(self, index: int, label: str) -> None:
        if index >= len(self._labels):
            # The direct leg is not a rung: it has no slot in the rotation
            # engine and benching or crediting "this machine's address" is not
            # a thing this chain gets to say.
            PROXY_HEALTH.note_success(self._provider_id, label)
            return
        await self._state.report_success(index)

    def _scope_credential(self) -> str | None:
        """The credential the bench scope needs, and only when it needs it.

        Under the default ``provider`` scope the answer is unused, and asking
        for it would build leg 0's client on a request that may never touch
        leg 0 -- which on a three-hundred-entry chain is the whole of the
        laziness, spent to compute a string that is thrown away.
        """

        if self._plan.scope != "credential":
            return None
        return self._pool.get(0).credential_label

    async def _settle_failure(
        self, index: int, label: str, error: BaseException, scope_key: str
    ) -> str:
        if index >= len(self._labels):
            PROXY_HEALTH.note_failure(
                self._provider_id, label, reason=type(error).__name__
            )
            return ""
        return await self._state.report_failure(
            index,
            error,
            scope_key=scope_key,
            triggers=self._triggers,
        )
