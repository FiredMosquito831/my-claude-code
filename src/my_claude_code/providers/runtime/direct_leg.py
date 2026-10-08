"""The legs of a chain that say truthfully where they went (7.79.2).

``providers/runtime/proxy_rotating.py`` is frozen, and it builds every leg of a
chain through the callable the factory hands it -- including, at index
``len(labels)``, the **Direct fallback**. That callable is the one seam this
release needs, and everything here is built at it. Nothing in the rotation
changes: which address is tried, in what order, how often, what is benched and
what is re-raised are all the frozen loop's, exactly as before.

:class:`DirectFallbackLeg`
    The Direct fallback, answering the user's decision of 2026-10-06 23:03:
    *"we should only fall back to the real IP once ALL proxies are
    unhealthy"*. When the loop reaches it, it asks the chain's own rotation
    state whether every proxy is unhealthy -- :func:`chain_exit_health` -- and
    dials this computer's address only if so. Otherwise it dials nothing,
    takes the ``direct`` it was just announced under back out of the request
    log, and answers a classified ``UNAVAILABLE`` 503 naming the rule and the
    proxies that are still healthy: the credential pool reads that as it reads
    a dead socket (another key, uncharged), the route's health does not count
    it, and the request moves on to the next entry of its fallback chain --
    exactly the path a refused chain has taken since 7.78.8.

:class:`AttributedLeg`
    A leg with one fixed way out, saying which: a static ``<PROVIDER>_PROXY``,
    a custom provider's stored proxy or a one-entry chain records its label
    before every dial (the request log said nothing at all for these before),
    and a Direct leg carried by the system proxy says so
    (``direct via system proxy host:port``) instead of ``direct``.

Both delegate everything else to the leaf they wrap, unchanged; the bytes a
provider sends are the leaf's own.
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from loguru import logger

from my_claude_code.config.system_proxy import system_proxy_for
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.proxy_attribution import (
    DIRECT_PROXY_LABEL,
    amend_proxy,
    record_proxy,
    system_proxy_label,
)
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningDialect,
    ReasoningPolicy,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.http import maybe_await_aclose

from .proxy_rotating import ProxyRotationState


class RotationView(Protocol):
    """What the health question reads of a chain's rotation state.

    Chat's frozen ``ProxyRotationState`` and media's copy of it both answer it.
    """

    @property
    def policy(self) -> str: ...

    def selectable_indexes(self, scope_key: str) -> tuple[int, ...]: ...


#: The status a withheld Direct fallback answers: the provider cannot be reached
#: from here right now under the operator's own rule, which is what a 503 says
#: -- the same status a chain with nothing usable is refused with (7.78.8).
DIRECT_WITHHELD_STATUS = 503

#: How many still-healthy proxies the withheld sentence names before "and N more".
_NAMED_HEALTHY = 3


class DirectFallbackWithheld(ExecutionFailure):
    """Direct fallback reached while a proxy of the chain was still healthy.

    An ``ExecutionFailure`` of kind ``UNAVAILABLE``, so every caller already
    handles it: not retryable -- nothing about the next try differs until a
    proxy's health does -- and ``safe_message``, the marker the model-list log
    reads before it quotes a failure, because the sentence is MCC's own.
    """

    safe_message = True


@dataclass(frozen=True, slots=True)
class ExitHealth:
    """Which proxies of a chain could still carry a request, right now.

    ``considered`` are the chain indexes the question is about: every proxy
    entry, or only the first under the ``single`` policy, which is all that
    policy ever dials (the 7.78.7 probes read the chain the same way).
    ``usable`` are those the rotation would still select. Every considered
    proxy unhealthy -- ``usable`` empty -- is the one case Direct may be used.
    """

    considered: tuple[int, ...]
    usable: tuple[int, ...]

    @property
    def all_unhealthy(self) -> bool:
        return not self.usable


def chain_exit_health(
    state: RotationView, labels: tuple[str, ...], scope_key: str
) -> ExitHealth:
    """Read, never write, the chain's own health: which proxies are unhealthy.

    "Unhealthy" is exactly what the rotation itself holds out of selection --
    nothing new is invented here:

    * **unreachable**: the address failed to connect (or answered ``407``) and
      no check has passed since (``PROXY_REACHABILITY``, 7.19.0);
    * **refused**: the checker measured it terminating TLS
      (``PROXY_INTERCEPTION``);
    * **cooling down**: the upstream refused through it with a switch the
      operator armed, and its bench for this request's scope has not run out
      (the rotation engine's own bench).

    An address nothing has tried yet is *not* unhealthy, and neither is one
    whose last failure was not about the address. The answer is
    :meth:`ProxyRotationState.selectable_indexes` -- the public question the
    frozen state class already answers, with the same lock-free read its own
    ``acquire`` makes.
    """

    proxied = tuple(
        index for index, label in enumerate(labels) if label != DIRECT_PROXY_LABEL
    )
    considered = (
        tuple(index for index in proxied if index == 0)
        if (state.policy == "single")
        else proxied
    )
    selectable = set(state.selectable_indexes(scope_key))
    return ExitHealth(
        considered=considered,
        usable=tuple(index for index in considered if index in selectable),
    )


def direct_withheld_sentence(
    who: str, labels: tuple[str, ...], health: ExitHealth
) -> str:
    """What a request whose Direct fallback was withheld is told."""

    names = [labels[index] for index in health.usable[:_NAMED_HEALTHY]]
    more = len(health.usable) - len(names)
    named = ", ".join(names) + (f" and {more} more" if more > 0 else "")
    count = len(health.considered)
    return (
        f"Not sent from this computer's own address: {len(health.usable)} of "
        f"{count} {'proxy' if count == 1 else 'proxies'} in {who}'s chain "
        f"{'is' if len(health.usable) == 1 else 'are'} not unhealthy ({named}), "
        "and Direct fallback uses this computer's address only once every proxy "
        "in the chain is unhealthy -- unreachable, refused for intercepting "
        "TLS, or cooling down after a refusal. This request used the proxy "
        "tries its limits allow and moves on to the next model of its fallback "
        f"chain. Proxying page -> {who} -> Direct fallback."
    )


def direct_fallback_withheld(message: str) -> ExecutionFailure:
    return DirectFallbackWithheld(
        kind=FailureKind.UNAVAILABLE,
        status_code=DIRECT_WITHHELD_STATUS,
        message=message,
        retryable=False,
    )


class _DelegatingLeg(BaseProvider):
    """A leg that is its leaf in every respect but the one it overrides.

    Built from the leaf's own configuration, and every other attribute is read
    through to the leaf, so a caller that reaches past ``BaseProvider`` to
    something a particular provider class adds finds it exactly where it was.
    """

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

    def _inner(self) -> BaseProvider:
        raise NotImplementedError

    def __getattr__(self, name: str) -> Any:
        # Only reached for names this class does not define. Private names are
        # not forwarded: a missing ``_x`` here is a bug in this class, not a
        # leaf attribute, and forwarding it would build the leaf to find out.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner(), name)

    @property
    def credential_label(self) -> str | None:
        return self._inner().credential_label

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect | None:
        return self._inner().reasoning_dialect(model_id)

    def throttle_remaining(self, model: str | None = None) -> float:
        return self._inner().throttle_remaining(model)

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        self._inner().preflight_stream(request, reasoning=reasoning)

    async def list_model_ids(self) -> frozenset[str]:
        return await self._inner().list_model_ids()

    async def list_model_infos(self):
        return await self._inner().list_model_infos()

    async def cleanup(self) -> None:
        await self._inner().cleanup()


async def _relay(
    leaf: BaseProvider,
    request: MessagesRequest,
    input_tokens: int,
    *,
    request_id: str | None,
    reasoning: ReasoningPolicy,
) -> AsyncIterator[str]:
    """The leaf's own stream, closed when this one is."""

    inner = leaf.stream_response(
        request, input_tokens, request_id=request_id, reasoning=reasoning
    )
    try:
        async for chunk in inner:
            yield chunk
    finally:
        await maybe_await_aclose(inner)


class AttributedLeg(_DelegatingLeg):
    """A leaf with one fixed way out, naming it in the request log (C-9 b, c).

    ``label`` is the masked ``host:port`` (or the chain entry's own name) of a
    static proxy or a one-entry chain: recorded before every dial, exactly as
    a chain's pool records the rung it dials, so the request log, the in-flight
    view and the speed ledger say which address carried the request instead of
    nothing. ``direct`` is a Direct leg the chain's pool has already announced
    as ``direct``: when the operating system's proxy carries this provider's
    host, that announcement is corrected to ``direct via system proxy
    host:port``. Neither touches what is sent.

    ``announce`` is for a Direct leg no pool announces -- a chain collapsed to
    its one entry, which is Direct: the leg records ``direct`` itself first.
    """

    def __init__(
        self,
        leaf: BaseProvider,
        *,
        label: str = "",
        direct: bool = False,
        announce: bool = False,
    ) -> None:
        super().__init__(leaf._config)
        self._leaf = leaf
        self._label = label
        self._direct = direct
        self._announce = announce

    def _inner(self) -> BaseProvider:
        return self._leaf

    @property
    def leaf(self) -> BaseProvider:
        return self._leaf

    def _attribute(self) -> None:
        if self._direct:
            if self._announce:
                record_proxy(DIRECT_PROXY_LABEL)
            address = system_proxy_for(self._config.base_url)
            if address:
                amend_proxy(DIRECT_PROXY_LABEL, system_proxy_label(address))
            return
        record_proxy(self._label)

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        self._attribute()
        return _relay(
            self._leaf,
            request,
            input_tokens,
            request_id=request_id,
            reasoning=reasoning,
        )


class DirectFallbackLeg(_DelegatingLeg):
    """The Direct fallback of a chain: this computer's address, only when allowed.

    Built by the factory at the index the frozen pool reserves for the
    fallback, and only ever reached once the pool's loop has ended without an
    answer. The leaf it wraps -- the same leaf with no proxy every release
    built here -- is constructed only when it is about to be used, so a
    withheld fallback opens no client at all.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        build: Callable[[], BaseProvider],
        state: Callable[[], ProxyRotationState],
        labels: tuple[str, ...],
        scope: str,
        provider_id: str,
        name: str = "",
    ) -> None:
        super().__init__(config)
        self._build = build
        self._state = state
        self._labels = labels
        self._scope = scope
        self._provider_id = provider_id
        self._name = name or provider_id
        self._leaf: BaseProvider | None = None

    def _inner(self) -> BaseProvider:
        if self._leaf is None:
            self._leaf = self._build()
        return self._leaf

    @property
    def leaf(self) -> BaseProvider:
        """The leaf with no proxy this leg dials when allowed (built on first ask)."""

        return self._inner()

    def exit_health(self) -> ExitHealth:
        """The chain's health as the fallback reads it, right now."""

        state = self._state()
        credential = (
            self._inner().credential_label if self._scope == "credential" else None
        )
        return chain_exit_health(state, self._labels, state.scope_key(credential))

    async def cleanup(self) -> None:
        # Only a leaf that was built has anything to close.
        if self._leaf is not None:
            await self._leaf.cleanup()

    def throttle_remaining(self, model: str | None = None) -> float:
        if self._leaf is None:
            return 0.0
        return self._leaf.throttle_remaining(model)

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        return self._gated(
            request, input_tokens, request_id=request_id, reasoning=reasoning
        )

    async def _gated(
        self,
        request: MessagesRequest,
        input_tokens: int,
        *,
        request_id: str | None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        health = self.exit_health()
        if not health.all_unhealthy:
            # Nothing is dialled: the ``direct`` the pool announced is taken
            # back, so the log never names a dial that did not happen.
            amend_proxy(DIRECT_PROXY_LABEL, None)
            logger.info(
                "PROXY CHAIN: {}: Direct fallback withheld -- {} of {} proxies "
                "not unhealthy; the request moves to its next model",
                self._provider_id,
                len(health.usable),
                len(health.considered),
            )
            raise direct_fallback_withheld(
                direct_withheld_sentence(self._name, self._labels, health)
            )
        leaf = self._inner()
        address = system_proxy_for(self._config.base_url)
        if address:
            amend_proxy(DIRECT_PROXY_LABEL, system_proxy_label(address))
        inner = leaf.stream_response(
            request, input_tokens, request_id=request_id, reasoning=reasoning
        )
        try:
            async for chunk in inner:
                yield chunk
        finally:
            await maybe_await_aclose(inner)


__all__ = [
    "DIRECT_WITHHELD_STATUS",
    "AttributedLeg",
    "DirectFallbackLeg",
    "DirectFallbackWithheld",
    "ExitHealth",
    "chain_exit_health",
    "direct_fallback_withheld",
    "direct_withheld_sentence",
]
