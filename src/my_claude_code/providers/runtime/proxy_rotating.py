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
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

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
        reachability ladder failed and will be tried again when its tier
        expires. An address in :data:`PROXY_INTERCEPTION` was measured
        terminating TLS, and is refused until a later check says otherwise --
        an address already in a chain when the checker finds that out is held
        out of selection here rather than waiting for somebody to edit the
        chain, because the whole point of finding it is not to route through it.
        """

        return frozenset(
            index
            for index in range(len(self._labels))
            if self._labels[index] != DIRECT_PROXY_LABEL
            and (
                PROXY_REACHABILITY.remaining(self._labels[index]) > 0
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

    async def acquire(self, attempted: frozenset[int], scope_key: str) -> int:
        """Pick a rung, or ``-1`` when this request has tried them all.

        Benched rungs are steered around while anything else is free, and the
        blocklist is relaxed when nothing is -- the credential pool's own
        second call, for the same reason: a fully benched chain that refused to
        dispatch would answer an empty stream, and there is no honest error to
        raise in its place. A rung this request already spent is never handed
        back, which is where the loop actually terminates.
        """

        count = len(self._labels)
        refused = self.refused()
        spent = attempted | refused
        async with self._lock:
            avoid = spent | self._unreachable()
            selected = self._engine.choose(avoid, scope_key)
            if selected is None:
                selected = self._engine.choose(spent, None)
            if selected is None or selected in spent:
                remaining = [index for index in range(count) if index not in spent]
                selected = remaining[0] if remaining else None
            if selected is None:
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
    ) -> bool:
        """Record one failure against a rung; return whether to advance.

        The two questions are independent, the way the credential pool keeps
        them: whether this address's record moves, and whether another address
        is worth trying.
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
            return True

        kind = failure_kind(error)
        if (
            kind is None
            or kind.value in PROXY_REFUSED_TRIGGER_KINDS
            or kind.value not in triggers
        ):
            # Not about the address at all. Nothing is charged and nothing
            # advances: raising here is what hands the request to the next
            # model, exactly as it does with no chain configured.
            return False

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
        return True

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


class ProxyRotatingProvider(BaseProvider):
    """Fan one credential's requests out over its chain of egress addresses.

    Every sub-provider here holds the *same* credential and differs only in
    ``ProviderConfig.proxy``, so anything that is a property of the credential
    -- its masked label, its dialect, its preflight -- is answered by the first
    one, the way ``RotatingProvider`` already answers ``list_model_ids`` from
    its own first sub-provider.
    """

    def __init__(
        self,
        config: ProviderConfig,
        providers: Sequence[BaseProvider],
        state: ProxyRotationState,
        *,
        labels: Sequence[str],
        plan: ProxyChainPlan,
        provider_id: str = "",
    ) -> None:
        super().__init__(config)
        if len(providers) < 2:
            raise ValueError("ProxyRotatingProvider requires at least two rungs")
        self._providers = tuple(providers)
        self._state = state
        self._labels = tuple(labels)
        self._plan = plan
        self._provider_id = provider_id
        self._triggers = frozenset(plan.on)
        self._max_switches = max(1, int(plan.max_switches))

    # ---------------------------------------------------------------- shape

    @property
    def credential_label(self) -> str | None:
        """One credential, many addresses: the sub-providers all agree."""

        return self._providers[0].credential_label

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect | None:
        return self._providers[0].reasoning_dialect(model_id)

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        self._providers[0].preflight_stream(request, reasoning=reasoning)

    async def list_model_ids(self) -> frozenset[str]:
        return await self._providers[0].list_model_ids()

    async def list_model_infos(self):
        return await self._providers[0].list_model_infos()

    def throttle_remaining(self, model: str | None = None) -> float:
        """0 while any rung can serve; otherwise the shortest wait until one can.

        Each rung is a separate client with its own limiter, which is the
        deliberate consequence of the fan-out on a provider metered per
        address: four addresses really are four allowances. Routing asks for
        the best case, so that is what it is told.
        """

        return min(
            (provider.throttle_remaining(model) for provider in self._providers),
            default=0.0,
        )

    async def cleanup(self) -> None:
        errors: list[Exception] = []
        for provider in self._providers:
            try:
                await provider.cleanup()
            except Exception as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if len(errors) > 1:
            raise ExceptionGroup("One or more proxy leg cleanups failed", errors)

    def proxy_health(self) -> list[dict[str, Any]]:
        """Per-rung health snapshots, index-aligned with the chain."""

        return self._state.get_metrics()

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
        scope_key = self._state.scope_key(self.credential_label)

        while len(attempted) < len(self._providers):
            index = await self._state.acquire(frozenset(attempted), scope_key)
            if index < 0 or index in attempted:
                break
            attempted.add(index)
            # Every upstream try the leaf's retry frame records from here on
            # carries this address, without a provider signature changing.
            record_proxy(self._labels[index])

            iterator = self._providers[index].stream_response(
                request,
                input_tokens,
                request_id=request_id,
                reasoning=reasoning,
            )
            try:
                first_chunk = await iterator.__anext__()
            except StopAsyncIteration:
                await self._state.report_success(index)
                return
            except Exception as error:
                last_error = error
                await maybe_await_aclose(iterator)
                advance = await self._state.report_failure(
                    index, error, scope_key=scope_key, triggers=self._triggers
                )
                if not advance:
                    # Not the address's fault. The same exception object the
                    # credential pool receives today, raised from the same
                    # place, so everything above classifies it identically.
                    raise
                if switches >= self._max_switches:
                    # The bound is spent. Re-raised unchanged rather than
                    # wrapped: every switch costs wall-clock inside one
                    # attempt, and the executor's deadlines have not moved.
                    raise
                switches += 1
                continue

            settled = False
            try:
                yield first_chunk
                async for chunk in iterator:
                    yield chunk
            except Exception as error:
                # Output has started, so this request cannot move address --
                # but the failure still has to count, or an address that
                # consistently dies mid-stream would never be benched.
                settled = True
                await maybe_await_aclose(iterator)
                await self._state.report_failure(
                    index, error, scope_key=scope_key, triggers=self._triggers
                )
                raise
            finally:
                if not settled:
                    await maybe_await_aclose(iterator)
            await self._state.report_success(index)
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
