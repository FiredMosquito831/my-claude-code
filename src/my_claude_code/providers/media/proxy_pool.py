"""The media copy of the proxy chain's loop, with proxy books of its own.

``providers/runtime/proxy_rotating.py`` stays frozen. Its state class writes the
process-wide ``PROXY_REACHABILITY`` and ``PROXY_HEALTH`` ledgers, which chat
selection reads -- so reusing it would let a media failure bench an address for
chat. This module copies ``ProxyRotationState`` and the provider's loop, and
points them at :data:`MEDIA_PROXY_REACHABILITY` and :data:`MEDIA_PROXY_HEALTH`
instead. It still honours ``PROXY_INTERCEPTION`` (read-only): an address the
checker measured terminating TLS is a prohibition, not a health opinion, and no
media request may go out through it either.

The rules it keeps, from the frozen module's contract: re-raise the last error
verbatim on exhaustion; never raise ``ModelRateLimited``; hold no clock; never
switch after the first chunk; a reachability failure always advances (bounded
by ``PROXY_MAX_LIVE_FAILURES``), an armed trigger advances up to the chain's
``max_switches``, anything else re-raises; the direct leg when the chain allows
it.

Since 7.79.2 the direct leg is the user's rule of 2026-10-06 23:03, exactly as
chat's: this computer's address only once every proxy of the chain is
unhealthy by media's own books (:meth:`MediaProxyRotationState.selectable_indexes`);
otherwise the request is refused with the same ``UNAVAILABLE`` 503 chat's
withheld fallback answers (``providers/runtime/direct_leg``). A Direct dial the
system proxy carries is labelled ``direct via system proxy host:port``.

Since 7.81.0 a chain with "Keep trying exits until one answers" ticked
(``ProxyChainPlan.until_served``) behaves here exactly as it does for chat
(``providers/runtime/proxy_rotating``, the rules in
``providers/runtime/exit_rotation``): a country refusal and a dropped
connection before the first byte are the exit's; refused exits are remembered
-- in media's own :data:`MEDIA_EXIT_MEMORY`, the way media keeps its own
reachability ledger -- and mirrored into the engine on every selection;
selection never relaxes; running out is an ``ExitsExhausted``; the switch and
live-failure bounds are the existing ones.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from loguru import logger

from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaChunk,
    MediaRequest,
)
from my_claude_code.config.system_proxy import system_proxy_for
from my_claude_code.core import proxy_rotation
from my_claude_code.core.credential_rotation import RotationEngine
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    failure_kind,
    find_execution_failure,
)
from my_claude_code.core.proxy_attribution import (
    DIRECT_PROXY_LABEL,
    is_direct_label,
    record_proxy,
    system_proxy_label,
)
from my_claude_code.core.proxy_exit_memory import (
    BLOCKED,
    MEDIA_EXIT_MEMORY,
    SPENT,
    ExitMemory,
    add_forget_listener,
)
from my_claude_code.core.proxy_rotation import (
    PROXY_INTERCEPTION,
    PROXY_REFUSED_TRIGGER_KINDS,
    ProxyHealthLedger,
    ReachabilityLedger,
)
from my_claude_code.core.upstream_ladder import record_dial_memory, record_exit_summary
from my_claude_code.providers.base import ProxyChainPlan
from my_claude_code.providers.http import maybe_await_aclose
from my_claude_code.providers.recovery import is_region_refusal
from my_claude_code.providers.runtime.direct_leg import (
    chain_exit_health,
    direct_fallback_withheld,
    direct_withheld_sentence,
)
from my_claude_code.providers.runtime.exit_rotation import (
    dial_memory_text,
    exhaustion_sentence,
    exit_outcome_word,
    exits_exhausted,
    exits_ran_out,
    proxy_transport_failure,
)
from my_claude_code.providers.runtime.proxy_rotating import proxy_reachability_failure

from .leaf import MediaLeaf, MediaNode

#: Media's own reachability bench. Same ladder as chat's, separate records.
MEDIA_PROXY_REACHABILITY = ReachabilityLedger()
#: Media's own per-(provider, address) health record.
MEDIA_PROXY_HEALTH = ProxyHealthLedger()


def reset_media_proxy_books() -> None:
    """Forget every media proxy bench. Called by the test suite between tests."""

    global MEDIA_PROXY_REACHABILITY, MEDIA_PROXY_HEALTH
    MEDIA_PROXY_REACHABILITY = ReachabilityLedger(
        tiers=proxy_rotation.PROXY_REACHABILITY.tiers
    )
    MEDIA_PROXY_HEALTH = ProxyHealthLedger()
    # Media's exit memory (7.81.0) lives in ``core`` so the Proxying page can
    # show it; it is media's all the same.
    MEDIA_EXIT_MEMORY.clear()


def _forget_media_exits(provider_id: str, labels: frozenset[str]) -> None:
    """The Proxying page's Forget, for media's own books (7.81.0).

    The memory itself is cleared by ``core.proxy_exit_memory.forget_exits``;
    this lets media's unreachable exits be dialled again and lifts media's
    cooldown on each, exactly as that function does for chat's books.
    """

    for label in labels:
        if label != DIRECT_PROXY_LABEL and MEDIA_PROXY_REACHABILITY.is_unhealthy(label):
            MEDIA_PROXY_REACHABILITY.note_success(label)
        if MEDIA_PROXY_HEALTH.snapshot(provider_id, label)["state"] == "cooldown":
            MEDIA_PROXY_HEALTH.record(provider_id, label).benched_until = 0.0


add_forget_listener(_forget_media_exits)


class MediaProxyRotationState:
    """``ProxyRotationState`` over media's own ledgers."""

    def __init__(
        self,
        leg_count: int,
        policy: str,
        *,
        labels: Sequence[str],
        provider_id: str,
        scope: str = "provider",
        clock: Callable[[], float] = time.monotonic,
        reachability: ReachabilityLedger | None = None,
        health: ProxyHealthLedger | None = None,
        until_served: bool = False,
        credential: str = "",
        credential_label: str = "",
        memory: ExitMemory | None = None,
    ) -> None:
        canonical = "failover" if policy == "on_error" else policy
        if canonical not in {"single", "round_robin", "least_used", "failover"}:
            canonical = "failover"
        self._engine = RotationEngine(
            leg_count,
            policy=canonical,
            tuning=proxy_rotation.PROXY_TUNING,
            clock=clock,
        )
        self._labels = tuple(labels)
        self._provider_id = provider_id
        self._scope = scope
        self._clock = clock
        self._lock = asyncio.Lock()
        self._reachability = reachability
        self._health = health
        #: "Keep trying exits until one answers" (7.81.0), as chat's state.
        self._until_served = bool(until_served)
        self._credential = credential
        self._credential_label = credential_label
        self._memory = memory

    @property
    def policy(self) -> str:
        return self._engine.policy

    @property
    def until_served(self) -> bool:
        return self._until_served

    @property
    def memory(self) -> ExitMemory:
        return self._memory if self._memory is not None else MEDIA_EXIT_MEMORY

    def _sync_memory(self, scope_key: str) -> None:
        """Chat's ``ProxyRotationState._sync_memory``, over media's memory."""

        if not self._until_served:
            return
        live = self.memory.live(self._provider_id, self._credential)
        now = self._clock()
        for index, label in enumerate(self._labels):
            slot = self._engine.slot(index)
            remaining = live.get(label)
            if remaining is None:
                slot.model_benches.pop(scope_key, None)
            else:
                slot.model_benches[scope_key] = now + remaining

    def remembered_indexes(self, scope_key: str) -> frozenset[int]:
        if not self._until_served:
            return frozenset()
        self._sync_memory(scope_key)
        live = self.memory.live(self._provider_id, self._credential)
        return frozenset(
            index
            for index, label in enumerate(self._labels)
            if label in live
            or (label != DIRECT_PROXY_LABEL and self.reachability.is_unhealthy(label))
        )

    def soonest_memory_expiry(self) -> float | None:
        if not self._until_served:
            return None
        live = self.memory.live(self._provider_id, self._credential)
        waits = [live[label] for label in self._labels if label in live]
        return min(waits) if waits else None

    def memory_verdict(self, index: int, advance: str, *, dropped: bool) -> str:
        label = self._label(index)
        if advance == "reachability":
            return dial_memory_text("dropped" if dropped else "unreachable")
        record = self.memory.recall(self._provider_id, self._credential, label)
        if record is None:
            return ""
        return dial_memory_text(
            record.state,
            seconds=self.memory.remaining(record),
            until_wall=record.until_wall,
            stated_wait=record.stated_wait,
        )

    def _remember(
        self,
        label: str,
        state: str,
        seconds: float,
        reason: str,
        stated_wait: float | None,
    ) -> None:
        self.memory.remember(
            self._provider_id,
            self._credential,
            label,
            state=state,
            seconds=seconds,
            reason=reason,
            stated_wait=stated_wait,
            credential_label=self._credential_label,
        )

    @property
    def reachability(self) -> ReachabilityLedger:
        return (
            self._reachability
            if self._reachability is not None
            else MEDIA_PROXY_REACHABILITY
        )

    @property
    def health(self) -> ProxyHealthLedger:
        return self._health if self._health is not None else MEDIA_PROXY_HEALTH

    def _label(self, index: int) -> str:
        if 0 <= index < len(self._labels):
            return self._labels[index]
        return DIRECT_PROXY_LABEL

    def _unreachable(self) -> frozenset[int]:
        return frozenset(
            index
            for index in range(len(self._labels))
            if self._labels[index] != DIRECT_PROXY_LABEL
            and (
                self.reachability.is_unhealthy(self._labels[index])
                or PROXY_INTERCEPTION.is_refused(self._labels[index])
            )
        )

    def refused(self) -> frozenset[int]:
        return frozenset(
            index
            for index in range(len(self._labels))
            if self._labels[index] != DIRECT_PROXY_LABEL
            and PROXY_INTERCEPTION.is_refused(self._labels[index])
        )

    def scope_key(self, credential: str | None) -> str:
        if self._scope == "credential" and credential:
            return f"{self._provider_id}:{credential}"
        return self._provider_id or "-"

    async def acquire(
        self, attempted: frozenset[int], scope_key: str, *, relax: bool = True
    ) -> int:
        count = len(self._labels)
        refused = self.refused()
        spent = attempted | refused
        async with self._lock:
            self._sync_memory(scope_key)
            avoid = spent | self._unreachable()
            selected = self._engine.choose(avoid, scope_key)
            if selected is None and relax and not self._until_served:
                selected = self._engine.choose(spent, None)
                if selected is None or selected in spent:
                    remaining = [index for index in range(count) if index not in spent]
                    selected = remaining[0] if remaining else None
            if selected is None or selected in spent:
                return -1
            self._engine.mark_acquired(selected)
        self.health.note_acquired(self._provider_id, self._label(selected))
        return selected

    async def report_success(self, index: int) -> None:
        label = self._label(index)
        async with self._lock:
            self._engine.succeed(index)
        if self._until_served:
            self.memory.forget_exit(self._provider_id, self._credential, label)
        self.reachability.note_success(label)
        self.health.note_success(self._provider_id, label)

    async def report_failure(
        self,
        index: int,
        error: BaseException,
        *,
        scope_key: str,
        triggers: frozenset[str],
        before_first_chunk: bool = True,
    ) -> str:
        label = self._label(index)
        reachability = proxy_reachability_failure(
            error,
            proxied=label != DIRECT_PROXY_LABEL,
            before_first_chunk=before_first_chunk,
        )
        if reachability is not None:
            benched = self.reachability.note_failure(label, reachability)
            self.health.note_failure(
                self._provider_id,
                label,
                reason=f"{reachability} -- benched {benched:.0f}s",
            )
            return "reachability"

        if self._until_served and is_region_refusal(error):
            # Chat's rule (7.81.0): a country refusal is the address's.
            async with self._lock:
                self._engine.fail(
                    index, "rate_limit", retry_after=None, model=scope_key
                )
                slot = self._engine.slot(index)
                until = slot.model_benches.get(scope_key)
                benched_for = 0.0 if until is None else max(0.0, until - self._clock())
            self.health.note_failure(
                self._provider_id,
                label,
                benched_for=benched_for,
                reason=f"country refusal -- benched {benched_for:.0f}s for {scope_key}",
            )
            self._remember(label, BLOCKED, benched_for, "country refusal", None)
            return "trigger"

        kind = failure_kind(error)
        if (
            kind is None
            or kind.value in PROXY_REFUSED_TRIGGER_KINDS
            or kind.value not in triggers
        ):
            dropped = (
                proxy_transport_failure(
                    error,
                    proxied=label != DIRECT_PROXY_LABEL,
                    before_first_chunk=before_first_chunk,
                )
                if self._until_served
                else None
            )
            if dropped is not None:
                # Chat's rule (7.81.0): dropped before the first byte.
                benched = self.reachability.note_failure(label, f"dropped: {dropped}")
                self.health.note_failure(
                    self._provider_id,
                    label,
                    reason=f"dropped ({dropped}) before the first byte -- "
                    f"benched {benched:.0f}s",
                )
                return "reachability"
            return ""

        failure = find_execution_failure(error)
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
        self.health.note_failure(
            self._provider_id,
            label,
            benched_for=benched_for,
            reason=f"{kind.value} -- benched {benched_for:.0f}s for {scope_key}",
        )
        if self._until_served:
            self._remember(label, SPENT, benched_for, kind.value, retry_after)
        return "trigger"

    def selectable_indexes(self, scope_key: str) -> tuple[int, ...]:
        self._sync_memory(scope_key)
        held_out = self._unreachable() | self.refused()
        return tuple(
            index
            for index in self._engine.selectable_indexes(scope_key)
            if index not in held_out
        )

    def get_metrics(self) -> list[dict[str, Any]]:
        return [
            dict(self.health.snapshot(self._provider_id, self._label(index)))
            | {"index": index, "label": self._label(index)}
            for index in range(len(self._labels))
        ]


class _MediaLegPool:
    """The legs of one chain, built on first use, idle ones closed past a bound.

    A copy of ``ProxyLegPool`` over media nodes: a leg in use is never closed.
    """

    def __init__(self, build: Callable[[int], MediaNode], *, max_open: int = 0) -> None:
        self._build = build
        self._max_open = max(0, int(max_open))
        self._open: dict[int, MediaNode] = {}
        self._in_use: dict[int, int] = {}

    def providers(self) -> tuple[MediaNode, ...]:
        return tuple(self._open.values())

    def opened(self) -> dict[int, MediaNode]:
        """Every leg that holds a client, by index. Never builds one."""

        return dict(self._open)

    def get(self, index: int) -> MediaNode:
        provider = self._open.pop(index, None)
        if provider is None:
            provider = self._build(index)
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

    async def reap(self) -> int:
        if self._max_open <= 0:
            return 0
        overflow = len(self._open) - self._max_open
        victims = [index for index in self._open if not self._in_use.get(index)][
            : max(0, overflow)
        ]
        for index in victims:
            provider = self._open.pop(index, None)
            if provider is None:  # pragma: no cover - single-threaded
                continue
            try:
                await provider.cleanup()
            except Exception as exc:
                logger.debug(
                    "Media proxy leg {} did not close cleanly: exc_type={}",
                    index,
                    type(exc).__name__,
                )
        return len(victims)

    async def close_all(self) -> None:
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
            raise ExceptionGroup("One or more media leg cleanups failed", errors)


class MediaProxyPool:
    """Fan one credential's media requests out over its chain of addresses.

    Index ``len(labels)`` is the direct leg, built only if the request runs out
    of healthy addresses and the chain's ``direct_fallback`` is on.
    """

    def __init__(
        self,
        build: Callable[[int], MediaNode],
        state: MediaProxyRotationState,
        *,
        labels: Sequence[str],
        plan: ProxyChainPlan,
        provider_id: str = "",
        max_open_legs: int = 0,
        max_live_failures: int = 0,
        name: str = "",
        base_url: str = "",
    ) -> None:
        self._labels = tuple(labels)
        if len(self._labels) < 2:
            raise ValueError("MediaProxyPool requires at least two rungs")
        #: The provider as the operator knows it, for the withheld sentence,
        #: and the host a Direct dial goes to, for the system-proxy label.
        self._name = name or provider_id
        self._base_url = base_url
        self._pool = _MediaLegPool(build, max_open=max_open_legs)
        self._state = state
        self._plan = plan
        self._provider_id = provider_id
        self._triggers = frozenset(plan.on)
        self._max_switches = max(1, int(plan.max_switches))
        self._max_live_failures = max(0, int(max_live_failures))
        self._direct_fallback = bool(plan.direct_fallback)
        self._direct_index = len(self._labels)
        #: "Keep trying exits until one answers" (7.81.0), as chat's pool.
        self._until_served = bool(getattr(plan, "until_served", False))

    @property
    def state(self) -> MediaProxyRotationState:
        return self._state

    @property
    def credential_label(self) -> str | None:
        return self._pool.get(0).credential_label

    def supports(self, request: MediaRequest) -> bool:
        return self._pool.get(0).supports(request)

    def leaf_for(self, key_index: int, proxy_label: str | None) -> MediaLeaf | None:
        """The leg an accepted job was created through, found by its label.

        The direct label is the direct leg; a label no longer in the chain
        (the operator edited it) falls back to the first leg, said in a log
        line -- the job belongs to the key, and the key is still here.
        """
        if key_index != 0:
            return None
        if proxy_label in self._labels:
            index = self._labels.index(proxy_label)
        elif is_direct_label(proxy_label):
            # Plain Direct, or Direct the system proxy carried (7.79.2): the
            # operator's own Direct entry when the chain has one, else the
            # fallback -- both are this computer's address.
            index = (
                self._labels.index(DIRECT_PROXY_LABEL)
                if DIRECT_PROXY_LABEL in self._labels
                else self._direct_index
            )
        else:
            if proxy_label is not None:
                logger.info(
                    "MEDIA JOB: proxy leg {!r} is no longer in {}'s chain; "
                    "reading the job through the first leg",
                    proxy_label,
                    self._provider_id,
                )
            index = 0
        return self._pool.get(index).leaf_for(0, proxy_label)

    def preflight(self, attempt: MediaAttempt) -> None:
        self._pool.get(0).preflight(attempt)

    def throttle_remaining(self, model: str | None = None) -> float:
        if self._until_served:
            return self._served_throttle(model)
        return min(
            (provider.throttle_remaining(model) for provider in self._pool.providers()),
            default=0.0,
        )

    def _served_throttle(self, model: str | None) -> float:
        """Chat's ``ProxyRotatingProvider._served_throttle`` (7.81.0)."""

        opened = self._pool.opened()
        credential = (
            opened[0].credential_label
            if self._plan.scope == "credential" and 0 in opened
            else None
        )
        selectable = self._state.selectable_indexes(self._state.scope_key(credential))
        waits: list[float] = []
        for index in selectable:
            leg = opened.get(index)
            wait = 0.0 if leg is None else leg.throttle_remaining(model)
            if wait <= 0:
                return 0.0
            waits.append(wait)
        if (
            not selectable
            and self._direct_fallback
            and DIRECT_PROXY_LABEL not in self._labels
        ):
            direct = opened.get(self._direct_index)
            return 0.0 if direct is None else direct.throttle_remaining(model)
        soonest = self._state.soonest_memory_expiry()
        if soonest is not None:
            waits.append(soonest)
        positive = [wait for wait in waits if wait > 0]
        return min(positive) if positive else 0.0

    async def cleanup(self) -> None:
        await self._pool.close_all()

    def execute(
        self, attempt: MediaAttempt, *, request_id: str | None = None
    ) -> AsyncIterator[MediaChunk]:
        return self._execute_with_rotation(attempt, request_id=request_id)

    def _scope_credential(self) -> str | None:
        if self._plan.scope != "credential":
            return None
        return self._pool.get(0).credential_label

    async def _acquire(
        self, attempted: frozenset[int], scope_key: str, *, relax: bool
    ) -> int:
        """Chat's ``ProxyRotatingProvider._acquire`` (7.81.0)."""

        if self._until_served:
            opened = self._pool.opened()
            waiting = frozenset(
                index
                for index, leg in opened.items()
                if index < len(self._labels) and leg.throttle_remaining() > 0
            )
            if waiting - attempted:
                index = await self._state.acquire(
                    attempted | waiting, scope_key, relax=relax
                )
                if index >= 0:
                    return index
        return await self._state.acquire(attempted, scope_key, relax=relax)

    def _skipped(self, attempted: set[int], scope_key: str) -> int:
        return len(self._state.remembered_indexes(scope_key) - attempted)

    def _note_skipped(self, attempted: set[int], scope_key: str) -> None:
        record_exit_summary(skipped=self._skipped(attempted, scope_key))

    def _exhausted(
        self,
        attempted: set[int],
        scope_key: str,
        outcomes: list[str],
        *,
        cap_reached: bool,
    ) -> Exception:
        """Chat's ``ProxyRotatingProvider._exhausted`` (7.81.0)."""

        skipped = self._skipped(attempted, scope_key)
        refused = len(self._state.refused() - attempted)
        sentence = exhaustion_sentence(
            self._name,
            outcomes=outcomes,
            skipped=skipped,
            switch_limit=self._max_switches if cap_reached else None,
            soonest=self._state.soonest_memory_expiry(),
            refused=refused,
        )
        record_exit_summary(skipped=skipped, exhausted=sentence)
        logger.info(
            "PROXY CHAIN: {}: media exits exhausted -- tried {}, {} skipped from "
            "memory; the request moves to its next model",
            self._provider_id,
            len(outcomes),
            skipped,
        )
        return exits_exhausted(sentence)

    async def _execute_with_rotation(
        self, attempt: MediaAttempt, *, request_id: str | None
    ) -> AsyncIterator[MediaChunk]:
        attempted: set[int] = set()
        last_error: Exception | None = None
        switches = 0
        live_failures = 0
        scope_key = self._state.scope_key(self._scope_credential())
        relax = not self._direct_fallback
        #: What each dialled exit answered (ticked chains, 7.81.0).
        outcomes: list[str] = []
        last_advance = ""
        last_region = False

        while len(attempted) < len(self._labels):
            if self._max_live_failures and live_failures >= self._max_live_failures:
                break
            index = await self._acquire(frozenset(attempted), scope_key, relax=relax)
            if index < 0 or index in attempted:
                break
            attempted.add(index)
            outcome = await self._attempt(index, attempt, request_id, scope_key)
            if outcome[0] == "done":
                if self._until_served:
                    self._note_skipped(attempted, scope_key)
                async for chunk in outcome[1]:
                    yield chunk
                return
            error, advance = outcome[2], outcome[3]
            if error is None:  # pragma: no cover - an attempt is done or errored
                break
            last_error = error
            if not advance:
                raise error
            live_failures += 1
            region = (
                self._until_served and advance == "trigger" and is_region_refusal(error)
            )
            last_advance, last_region = advance, region
            if self._until_served:
                outcomes.append(exit_outcome_word(error, advance, region=region))
            if advance == "trigger":
                if switches >= self._max_switches:
                    if self._until_served and exits_ran_out(
                        error, advance, region=region
                    ):
                        raise self._exhausted(
                            attempted, scope_key, outcomes, cap_reached=True
                        ) from error
                    raise error
                switches += 1

        if self._direct_fallback and DIRECT_PROXY_LABEL not in self._labels:
            if self._until_served:
                self._note_skipped(attempted, scope_key)
            # 7.79.2: only once every proxy of the chain is unhealthy. Asked
            # before anything is announced, so a withheld fallback dials
            # nothing and the log names no dial.
            health = chain_exit_health(self._state, self._labels, scope_key)
            if not health.all_unhealthy:
                logger.info(
                    "PROXY CHAIN: {}: media Direct fallback withheld -- {} of {} "
                    "proxies not unhealthy; the request moves to its next model",
                    self._provider_id,
                    len(health.usable),
                    len(health.considered),
                )
                raise direct_fallback_withheld(
                    direct_withheld_sentence(self._name, self._labels, health)
                )
            outcome = await self._attempt(
                self._direct_index, attempt, request_id, scope_key
            )
            if outcome[0] == "done":
                async for chunk in outcome[1]:
                    yield chunk
                return
            if outcome[2] is not None:
                raise outcome[2]
            return

        if self._until_served and exits_ran_out(
            last_error, last_advance, region=last_region
        ):
            exhausted = self._exhausted(
                attempted, scope_key, outcomes, cap_reached=False
            )
            if last_error is not None:
                raise exhausted from last_error
            raise exhausted

        if last_error is not None:
            raise last_error

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
        attempt: MediaAttempt,
        request_id: str | None,
        scope_key: str,
    ) -> tuple[str, AsyncIterator[MediaChunk], Exception | None, str]:
        """Open one rung and pull its first chunk (the last safe moment to move)."""
        label = (
            DIRECT_PROXY_LABEL if index >= len(self._labels) else self._labels[index]
        )
        record_proxy(self._dial_label(label))
        provider = self._pool.get(index)
        self._pool.hold(index)
        try:
            await self._pool.reap()
        except Exception as exc:  # pragma: no cover - reap swallows its own
            logger.debug("Media leg reap failed: exc_type={}", type(exc).__name__)

        iterator = provider.execute(attempt, request_id=request_id)
        try:
            first_chunk = await anext(iterator)
        except StopAsyncIteration:
            self._pool.release(index)
            await self._settle_success(index, label)
            return ("done", _no_chunks(), None, "")
        except Exception as error:
            self._pool.release(index)
            await maybe_await_aclose(iterator)
            advance = await self._settle_failure(
                index, label, error, scope_key, before_first_chunk=True
            )
            if self._until_served and advance:
                dropped = (
                    advance == "reachability"
                    and proxy_reachability_failure(error, proxied=True) is None
                )
                verdict = self._state.memory_verdict(index, advance, dropped=dropped)
                if verdict:
                    record_dial_memory(verdict)
            return ("failed", _no_chunks(), error, advance)
        return (
            "done",
            self._drain(index, label, iterator, first_chunk, scope_key),
            None,
            "",
        )

    def _dial_label(self, label: str) -> str:
        """What the log calls a dial: a Direct one names the system proxy carrying it.

        The health books keep ``label`` itself; only the request log's word for
        the dial changes, and only when the operating system's proxy really
        carries this provider's host.
        """

        if label != DIRECT_PROXY_LABEL:
            return label
        address = system_proxy_for(self._base_url)
        return system_proxy_label(address) if address else label

    async def _drain(
        self,
        index: int,
        label: str,
        iterator: AsyncIterator[MediaChunk],
        first_chunk: MediaChunk,
        scope_key: str,
    ) -> AsyncIterator[MediaChunk]:
        settled = False
        try:
            yield first_chunk
            async for chunk in iterator:
                yield chunk
        except Exception as error:
            settled = True
            await maybe_await_aclose(iterator)
            await self._settle_failure(
                index, label, error, scope_key, before_first_chunk=False
            )
            raise
        finally:
            self._pool.release(index)
            if not settled:
                await maybe_await_aclose(iterator)
        await self._settle_success(index, label)

    async def _settle_success(self, index: int, label: str) -> None:
        if index >= len(self._labels):
            self._state.health.note_success(self._provider_id, label)
            return
        await self._state.report_success(index)

    async def _settle_failure(
        self,
        index: int,
        label: str,
        error: BaseException,
        scope_key: str,
        *,
        before_first_chunk: bool,
    ) -> str:
        if index >= len(self._labels):
            self._state.health.note_failure(
                self._provider_id, label, reason=type(error).__name__
            )
            return ""
        return await self._state.report_failure(
            index,
            error,
            scope_key=scope_key,
            triggers=self._triggers,
            before_first_chunk=before_first_chunk,
        )


async def _no_chunks() -> AsyncIterator[MediaChunk]:
    """An upstream that closed without sending anything. Not an error."""

    return
    yield b""  # pragma: no cover - unreachable; makes this an async generator
