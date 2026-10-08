"""PARITY CONTRACT: the media engine decides exactly as the chat engine does.

User decision (2026-09-26 03:38, #4): media requests go through a SEPARATE
copy of chat's retry / key / 429 / pause / bench / proxy rules, and "a parity
contract test that runs the same failure scenarios through chat and media and
asserts identical decisions" is the required safeguard.

How: one scripted upstream world, two stacks built over it.

* chat  = ``ProviderExecutor`` -> ``RotatingProvider`` -> ``ProxyRotatingProvider``
  (all frozen, unmodified) -> a chat leaf;
* media = ``MediaExecutor`` -> ``MediaKeyPool`` -> ``MediaProxyPool`` (the copies)
  -> a media leaf.

Both leaves are the same :class:`MediaLeaf` HTTP exchange (the chat leaf wraps
it and speaks SSE), so the leaf's limiter ladder and failure classification are
identical by construction and every difference the test can see comes from the
copied layers. For each scenario the test compares: the ordered upstream calls
(provider, model, key, proxy leg), the final outcome, every ledger verdict, the
route-health bench of every ref, and every key's health record.

Known boundary, stated rather than hidden: the 429 probe is a *chat* request
on both sides, so it goes out through the chat provider's own proxy selection.
Scenarios that combine a probe with a proxy chain would compare two different
proxy books by design, so none is included.
"""

import asyncio
import contextlib
import dataclasses
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from my_claude_code.application.execution import (
    ProviderExecutor,
    RouteAttemptRecord,
    RouteExecutionPolicy,
)
from my_claude_code.application.media.executor import MediaExecutor
from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaPlan,
    MediaRail,
    MediaRequest,
)
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.application.routing import (
    ResolvedModel,
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_OPERATION_VIDEO_RETRIEVE,
    image_generation_surface,
    video_surfaces,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.credential_attribution import install_attribution
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    failure_kind_name,
    find_execution_failure,
)
from my_claude_code.core.openai_videos import parse_job
from my_claude_code.core.proxy_attribution import DIRECT_PROXY_LABEL
from my_claude_code.core.proxy_rotation import (
    PROXY_HEALTH,
    PROXY_INTERCEPTION,
    PROXY_REACHABILITY,
)
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningPolicy,
)
from my_claude_code.providers.base import (
    BaseProvider,
    ProviderConfig,
    ProxyChainPlan,
    ProxyLeg,
)
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.media import proxy_pool as media_proxy_pool
from my_claude_code.providers.media.jobs import PinnedMediaClient
from my_claude_code.providers.media.key_pool import MediaKeyPool
from my_claude_code.providers.media.leaf import MediaLeaf, MediaNode
from my_claude_code.providers.media.proxy_pool import (
    MediaProxyPool,
    MediaProxyRotationState,
)
from my_claude_code.providers.media.registry import _leaf_limiter
from my_claude_code.providers.runtime.direct_leg import DirectFallbackLeg
from my_claude_code.providers.runtime.proxy_rotating import (
    ProxyRotatingProvider,
    ProxyRotationState,
)
from my_claude_code.providers.runtime.rotating import RotatingProvider

# --------------------------------------------------------------- the world


@dataclass(frozen=True)
class Outcome:
    kind: str = "ok"  # ok | status | connect
    status: int = 200
    message: str = ""
    headers: tuple[tuple[str, str], ...] = ()


OK = Outcome()


def status(code: int, message: str = "upstream said no", **headers: str) -> Outcome:
    return Outcome("status", code, message, tuple(headers.items()))


CONNECT = Outcome("connect")

Key = tuple[str, str | None, int | None, str | None]


@dataclass
class Script:
    """Answers by (provider, model, key, leg); ``None`` in a rule is a wildcard.

    A rule with several outcomes answers them in order and then repeats its
    last one. Every call is recorded, in order, on :attr:`calls`.
    """

    rules: dict[Key, list[Outcome]] = field(default_factory=dict)
    calls: list[tuple[str, str, int, str]] = field(default_factory=list)
    _served: dict[Key, int] = field(default_factory=dict)

    def respond(self, provider: str, model: str, key: int, leg: str) -> Outcome:
        self.calls.append((provider, model, key, leg))
        candidates = (
            (provider, model, key, leg),
            (provider, model, key, None),
            (provider, model, None, leg),
            (provider, model, None, None),
            (provider, None, key, leg),
            (provider, None, key, None),
            (provider, None, None, leg),
            (provider, None, None, None),
        )
        for rule in candidates:
            outcomes = self.rules.get(rule)
            if outcomes:
                served = self._served.get(rule, 0)
                self._served[rule] = served + 1
                return outcomes[min(served, len(outcomes) - 1)]
        return OK

    def fresh(self) -> Script:
        return Script(rules=self.rules)


def _transport(
    script: Script, provider: str, key: int, leg: str
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        outcome = script.respond(provider, model, key, leg)
        if outcome.kind == "connect":
            raise httpx.ConnectError("connection refused", request=request)
        if outcome.kind == "status":
            return httpx.Response(
                outcome.status,
                json={"error": {"message": outcome.message}},
                headers=dict(outcome.headers),
            )
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": "aGk="}]})

    return httpx.MockTransport(handler)


@dataclass(frozen=True)
class ProviderSpec:
    keys: int = 1
    legs: int = 0
    triggers: frozenset[str] = frozenset()
    max_switches: int = 2
    direct_fallback: bool = True
    routes_around_model: bool = True
    retry_attempts: int = 1
    #: A header-less 429's bench, in seconds (the shipped default is 60).
    cooldown: float = 0.05
    #: ``PROXY_MAX_LIVE_FAILURES`` for the chain (0 = unbounded).
    max_live_failures: int = 0


def _config(provider: str, spec: ProviderSpec) -> ProviderConfig:
    keys = tuple(f"{provider}-secret-{index}" for index in range(spec.keys))
    plan = None
    if spec.legs:
        plan = ProxyChainPlan(
            legs=tuple(
                ProxyLeg(
                    url=f"http://{provider}-p{index}.test:8080",
                    label=f"{provider}-p{index}",
                )
                for index in range(spec.legs)
            ),
            policy="failover",
            on=spec.triggers,
            scope="provider",
            max_switches=spec.max_switches,
            direct_fallback=spec.direct_fallback,
        )
    return ProviderConfig(
        api_key=keys[0],
        base_url=f"https://{provider}.test/v1",
        api_keys=keys,
        credential_rotation="failover",
        retry_attempts=spec.retry_attempts,
        retry_backoff_base_seconds=0.0,
        retry_backoff_max_seconds=0.0,
        retry_backoff_jitter_seconds=0.0,
        routes_around_model=spec.routes_around_model,
        proxy_chain=plan,
        # A header-less 429 blocks the leaf for this long (the real default is
        # 60 s); both stacks read the same config, so only the wait shrinks.
        rate_limit_cooldown_seconds=spec.cooldown,
    )


def _resolved(provider: str, model: str) -> ResolvedModel:
    return ResolvedModel(
        original_model="client-model",
        provider_id=provider,
        provider_model=model,
        provider_model_ref=f"{provider}/{model}",
        reasoning_preference=ReasoningPreference.INHERIT,
    )


def _leaf(
    script: Script,
    provider: str,
    config: ProviderConfig,
    key: int,
    leg: str,
    proxied: bool,
) -> MediaLeaf:
    return MediaLeaf(
        provider_id=provider,
        config=config,
        surfaces=(image_generation_surface(),),
        rate_limiter=_leaf_limiter(config, proxied_leg=proxied),
        transport=_transport(script, provider, key, leg),
    )


# ---------------------------------------------------------------- chat stack


class _ChatLeaf(BaseProvider):
    """A chat leaf whose HTTP exchange is the media leaf's, spoken as SSE."""

    def __init__(self, config: ProviderConfig, leaf: MediaLeaf, provider: str) -> None:
        super().__init__(config)
        self._leaf = leaf
        self._provider = provider

    def preflight_stream(self, request, *, reasoning=DEFAULT_REASONING_POLICY) -> None:
        return None

    async def cleanup(self) -> None:
        await self._leaf.cleanup()

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset()

    def throttle_remaining(self, model: str | None = None) -> float:
        return self._leaf.throttle_remaining(model)

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        return self._stream(request, request_id)

    async def _stream(
        self, request: MessagesRequest, request_id: str | None
    ) -> AsyncIterator[str]:
        attempt = MediaAttempt(
            request=MediaRequest(
                operation=MEDIA_OPERATION_IMAGE_GENERATE,
                rail=MediaRail.IMAGE,
                model=request.model,
                body={"prompt": "p"},
            ),
            resolved=_resolved(self._provider, request.model),
        )
        async for _chunk in self._leaf.execute(attempt, request_id=request_id):
            yield (
                "event: content_block_delta\ndata: "
                '{"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta","text":"ok"}}\n\n'
            )


def _chat_node(
    script: Script, provider: str, config: ProviderConfig, key: int
) -> BaseProvider:
    plan = config.proxy_chain
    if plan is None or len(plan.legs) < 2:
        return _ChatLeaf(
            config, _leaf(script, provider, config, key, "-", False), provider
        )
    legs = plan.legs
    labels = tuple(leg.label or DIRECT_PROXY_LABEL for leg in legs)

    def build(index: int) -> BaseProvider:
        url = legs[index].url if index < len(legs) else ""
        leg_config = dataclasses.replace(config, proxy=url, proxy_chain=None)
        leg = labels[index] if index < len(legs) else DIRECT_PROXY_LABEL

        def leaf() -> BaseProvider:
            return _ChatLeaf(
                leg_config,
                _leaf(script, provider, leg_config, key, leg, bool(url)),
                provider,
            )

        if index >= len(legs):
            # The Direct fallback, gated exactly as the factory gates it
            # (7.79.2): this computer only once every proxy is unhealthy.
            return DirectFallbackLeg(
                config,
                build=leaf,
                state=lambda: state,
                labels=labels,
                scope=plan.scope,
                provider_id=provider,
            )
        return leaf()

    state = ProxyRotationState(
        len(legs), plan.policy, labels=labels, provider_id=provider, scope=plan.scope
    )
    return ProxyRotatingProvider(
        config,
        build,
        state,
        labels=labels,
        plan=plan,
        provider_id=provider,
        max_live_failures=_LIVE_FAILURES.get(provider, 0),
    )


def _key_state(config: ProviderConfig, count: int) -> CredentialRotationState:
    return CredentialRotationState(
        count,
        config.credential_rotation,
        rate_limit_seconds=config.rate_limit_cooldown_seconds,
        lockout_tiers=config.lockout_tiers,
        model_bench_escalation=config.credential_model_bench_escalation,
        cooldown=config.rate_limit_cooldown(),
    )


def _per_key(config: ProviderConfig) -> list[ProviderConfig]:
    return [
        dataclasses.replace(
            config, api_key=key, api_keys=(key,), credential_rotation="single"
        )
        for key in config.api_keys
    ]


#: Each provider's ``max_live_failures``, set from its spec as a stack is
#: built (both stacks of one scenario read the same spec).
_LIVE_FAILURES: dict[str, int] = {}


def _chat_provider(script: Script, provider: str, spec: ProviderSpec):
    _LIVE_FAILURES[provider] = spec.max_live_failures
    config = _config(provider, spec)
    if len(config.api_keys) <= 1:
        return _chat_node(script, provider, config, 0), None
    nodes = [
        _chat_node(script, provider, sub, index)
        for index, sub in enumerate(_per_key(config))
    ]
    state = _key_state(config, len(nodes))
    return (
        RotatingProvider(
            config,
            nodes,
            state,
            key_labels=tuple(f"k{index}" for index in range(len(nodes))),
            provider_id=provider,
            routes_around_model=config.routes_around_model,
        ),
        state,
    )


# --------------------------------------------------------------- media stack


def _media_node(
    script: Script, provider: str, config: ProviderConfig, key: int
) -> MediaNode:
    plan = config.proxy_chain
    if plan is None or len(plan.legs) < 2:
        return _leaf(script, provider, config, key, "-", False)
    legs = plan.legs
    labels = tuple(leg.label or DIRECT_PROXY_LABEL for leg in legs)

    def build(index: int) -> MediaNode:
        url = legs[index].url if index < len(legs) else ""
        leg_config = dataclasses.replace(config, proxy=url, proxy_chain=None)
        leg = labels[index] if index < len(legs) else DIRECT_PROXY_LABEL
        return _leaf(script, provider, leg_config, key, leg, bool(url))

    state = MediaProxyRotationState(
        len(legs), plan.policy, labels=labels, provider_id=provider, scope=plan.scope
    )
    return MediaProxyPool(
        build,
        state,
        labels=labels,
        plan=plan,
        provider_id=provider,
        max_live_failures=_LIVE_FAILURES.get(provider, 0),
    )


def _media_provider(script: Script, provider: str, spec: ProviderSpec):
    _LIVE_FAILURES[provider] = spec.max_live_failures
    config = _config(provider, spec)
    if len(config.api_keys) <= 1:
        return _media_node(script, provider, config, 0), None
    nodes = [
        _media_node(script, provider, sub, index)
        for index, sub in enumerate(_per_key(config))
    ]
    state = _key_state(config, len(nodes))
    return (
        MediaKeyPool(
            nodes,
            state,
            key_labels=tuple(f"k{index}" for index in range(len(nodes))),
            provider_id=provider,
            routes_around_model=config.routes_around_model,
        ),
        state,
    )


# ------------------------------------------------------------------ scenario


@dataclass
class Scenario:
    name: str
    providers: dict[str, ProviderSpec]
    chain: tuple[tuple[str, str], ...]
    script: Script
    requests: int = 1
    paused: frozenset[str] = frozenset()
    probes: dict[str, str] = field(default_factory=dict)
    retry_first: str = "skip"
    policy: RouteExecutionPolicy = field(default_factory=RouteExecutionPolicy)
    health: Callable[[], RouteHealthRegistry] = RouteHealthRegistry


def _outcome(exc: BaseException | None) -> tuple[Any, ...]:
    if exc is None:
        return ("served",)
    failure = find_execution_failure(exc)
    return (
        type(exc).__name__,
        failure_kind_name(exc),
        None if failure is None else failure.status_code,
        str(exc),
    )


def _ledger(records: list[RouteAttemptRecord]) -> list[tuple[Any, ...]]:
    return [
        (
            r.attempt,
            r.provider_id,
            r.model_ref,
            r.outcome,
            r.error_kind,
            r.error_message,
            r.bench is None,
        )
        for r in records
    ]


def _key_books(state: CredentialRotationState | None) -> list[tuple[Any, ...]] | None:
    if state is None:
        return None
    return [
        (
            entry["state"],
            entry["request_count"],
            entry["failure_count"],
            entry["auth_failures"],
            entry["rate_limits"],
            entry["cooldown_remaining"] > 0,
            entry["lockout_remaining"] > 0,
            tuple(bench["model"] for bench in entry["model_benches"]),
        )
        for entry in state.get_metrics()
    ]


def _reset_chat_proxy_books() -> None:
    PROXY_REACHABILITY.clear()
    PROXY_HEALTH.clear()
    PROXY_INTERCEPTION.clear()


def _run_chat(scenario: Scenario) -> dict[str, Any]:
    _reset_chat_proxy_books()
    script = scenario.script.fresh()
    built = {
        name: _chat_provider(script, name, spec)
        for name, spec in scenario.providers.items()
    }

    def resolver(provider_id: str):
        return built[provider_id][0]

    health = scenario.health()
    executor = ProviderExecutor(
        resolver,
        policy=scenario.policy,
        health=health,
        retry_first=scenario.retry_first,
        provider_lookup=lambda provider_id: resolver(provider_id).throttle_remaining(),
    )
    routed = tuple(
        RoutedMessagesRequest(
            request=MessagesRequest(
                model=model, messages=[Message(role="user", content="p")], stream=False
            ),
            resolved=_resolved(provider, model),
            reasoning=ReasoningPolicy.on(),
            requested_reasoning=ReasoningPolicy.on(),
            reasoning_adaptation=ReasoningAdaptation(
                ReasoningAdaptationKind.UNCHANGED, None
            ),
        )
        for provider, model in scenario.chain
    )
    plan = RoutedMessagesPlan(
        routed,
        paused_refs=scenario.paused,
        probe_candidates={p: _resolved(p, m) for p, m in scenario.probes.items()},
    )
    outcomes: list[tuple[Any, ...]] = []
    ledgers: list[list[tuple[Any, ...]]] = []

    async def one() -> None:
        install_attribution()
        records: list[RouteAttemptRecord] = []
        error: BaseException | None = None
        try:
            stream = executor.stream(
                plan,
                wire_api="messages",
                raw_log_label="X",
                raw_log_payload={},
                request_id="parity",
                on_attempt_result=records.append,
            )
            async for _ in stream:
                pass
        except Exception as exc:
            error = exc
        outcomes.append(_outcome(error))
        ledgers.append(_ledger(records))

    books: dict[str, Any] = {}

    async def run() -> None:
        for _ in range(scenario.requests):
            await one()
        # Read at once, inside the loop: a short bench must not expire
        # between the run and the reading on a loaded machine.
        books.update({name: _key_books(state) for name, (_p, state) in built.items()})

    asyncio.run(run())
    return {
        "calls": script.calls,
        "outcomes": outcomes,
        "ledgers": ledgers,
        "benched": {ref: health.is_ejected(ref) for ref in plan.model_refs()},
        "keys": books,
    }


def _run_media(scenario: Scenario) -> dict[str, Any]:
    _reset_chat_proxy_books()
    media_proxy_pool.reset_media_proxy_books()
    script = scenario.script.fresh()
    built = {
        name: _media_provider(script, name, spec)
        for name, spec in scenario.providers.items()
    }
    # The probe is a chat question on both sides: a chat stack of its own,
    # over the same scripted world, answers it.
    chat_for_probe = {
        name: _chat_provider(script, name, spec)[0]
        for name, spec in scenario.providers.items()
    }

    def resolver(provider_id: str):
        return built[provider_id][0]

    health = scenario.health()
    executor = MediaExecutor(
        resolver,
        policy=scenario.policy,
        health=health,
        retry_first=scenario.retry_first,
        provider_lookup=lambda provider_id: resolver(provider_id).throttle_remaining(),
        chat_provider_resolver=lambda provider_id: chat_for_probe[provider_id],
    )
    request = MediaRequest(
        operation=MEDIA_OPERATION_IMAGE_GENERATE,
        rail=MediaRail.IMAGE,
        model="client-model",
        body={"prompt": "p"},
    )
    plan = MediaPlan(
        attempts=tuple(
            MediaAttempt(request, _resolved(p, m)) for p, m in scenario.chain
        ),
        paused_refs=scenario.paused,
        paused_env_var="MODEL_PAUSED",
        probe_candidates={p: _resolved(p, m) for p, m in scenario.probes.items()},
    )
    outcomes: list[tuple[Any, ...]] = []
    ledgers: list[list[tuple[Any, ...]]] = []

    async def one() -> None:
        install_attribution()
        records: list[RouteAttemptRecord] = []
        error: BaseException | None = None
        try:
            stream = executor.execute(
                plan, request_id="parity", on_attempt_result=records.append
            )
            async for _ in stream:
                pass
        except Exception as exc:
            error = exc
        outcomes.append(_outcome(error))
        ledgers.append(_ledger(records))

    books: dict[str, Any] = {}

    async def run() -> None:
        for _ in range(scenario.requests):
            await one()
        # Read at once, inside the loop: a short bench must not expire
        # between the run and the reading on a loaded machine.
        books.update({name: _key_books(state) for name, (_p, state) in built.items()})

    asyncio.run(run())
    return {
        "calls": script.calls,
        "outcomes": outcomes,
        "ledgers": ledgers,
        "benched": {ref: health.is_ejected(ref) for ref in plan.model_refs()},
        "keys": books,
    }


CREDITS = "Insufficient credits: your account balance is too low"


def _scenarios() -> list[Scenario]:
    one = ProviderSpec()
    two_keys = ProviderSpec(keys=2)
    return [
        Scenario(
            "5xx falls back to the next model",
            {"a": one, "b": one},
            (("a", "m1"), ("b", "m2")),
            Script({("a", None, None, None): [status(500)]}),
        ),
        Scenario(
            "retry_once retries the primary once, then falls back",
            {"a": one, "b": one},
            (("a", "m1"), ("b", "m2")),
            Script({("a", None, None, None): [status(503)]}),
            retry_first="retry_once",
        ),
        Scenario(
            "a malformed 400 ends the route",
            {"a": one, "b": one},
            (("a", "m1"), ("b", "m2")),
            Script({("a", None, None, None): [status(400, "Malformed request body")]}),
        ),
        Scenario(
            "a plain 400 is about the model and moves on",
            {"a": one, "b": one},
            (("a", "m1"), ("b", "m2")),
            Script(
                {("a", None, None, None): [status(400, "model does not support n")]}
            ),
        ),
        Scenario(
            "auth on one key rotates to the next key",
            {"a": two_keys, "b": one},
            (("a", "m1"), ("b", "m2")),
            Script({("a", None, 0, None): [status(401, "invalid api key")]}),
        ),
        Scenario(
            "auth on every key exhausts the pool and falls back",
            {"a": two_keys, "b": one},
            (("a", "m1"), ("b", "m2")),
            Script({("a", None, None, None): [status(401, "invalid api key")]}),
        ),
        Scenario(
            "429 routes around to the same provider's next model",
            {"a": two_keys, "b": one},
            (("a", "m1"), ("b", "x"), ("a", "m2")),
            Script(
                {
                    ("a", "m1", None, None): [
                        status(429, "slow down", **{"retry-after": "30"})
                    ]
                }
            ),
        ),
        Scenario(
            "429 probe answering 429 escalates and retries the same model",
            # A 2 s bench: long enough to still be on the books when they are
            # compared, short enough that the probe (5 s bound) waits it out.
            {"a": ProviderSpec(keys=2, cooldown=2.0), "b": one},
            (("a", "m1"), ("b", "x")),
            Script(
                {
                    ("a", "m1", 0, None): [status(429, "slow down")],
                    ("a", "probe", 0, None): [status(429, "slow down")],
                }
            ),
            probes={"a": "probe"},
        ),
        Scenario(
            "429 probe answering 200 moves to the next model",
            # A 2 s bench, as in the escalation scenario: the (key, model)
            # bench this 429 leaves must still be on the books when both sides
            # are compared -- a 0.05 s one expired first on a loaded CI runner.
            {"a": ProviderSpec(keys=2, cooldown=2.0), "b": one},
            (("a", "m1"), ("b", "x")),
            Script({("a", "m1", None, None): [status(429, "slow down")]}),
            probes={"a": "probe"},
        ),
        Scenario(
            "a 429 block longer than the probe bound leaves it inconclusive",
            {"a": two_keys, "b": one},
            (("a", "m1"), ("b", "x")),
            Script(
                {
                    ("a", "m1", None, None): [
                        status(429, "slow down", **{"retry-after": "30"})
                    ]
                }
            ),
            probes={"a": "probe"},
        ),
        Scenario(
            "429 with nowhere to route spends the rate-limit ladder",
            {"a": one},
            (("a", "m1"),),
            Script({("a", None, None, None): [status(429, "slow down")]}),
            policy=RouteExecutionPolicy(rate_limit_attempts=3),
        ),
        Scenario(
            "a paused model is skipped",
            {"a": one, "b": one},
            (("a", "m1"), ("b", "m2")),
            Script(),
            paused=frozenset({"a/m1"}),
        ),
        Scenario(
            "every model paused is an error naming the setting",
            {"a": one},
            (("a", "m1"),),
            Script(),
            paused=frozenset({"a/m1"}),
        ),
        Scenario(
            "every model out of credits reads as one credits failure",
            {"a": one, "b": one},
            (("a", "m1"), ("b", "m2")),
            Script(
                {
                    ("a", None, None, None): [status(402, CREDITS)],
                    ("b", None, None, None): [status(402, CREDITS)],
                }
            ),
        ),
        Scenario(
            "a dead proxy moves to the next address",
            {"a": ProviderSpec(legs=2), "b": one},
            (("a", "m1"), ("b", "m2")),
            Script({("a", None, None, "a-p0"): [CONNECT]}),
        ),
        Scenario(
            "an armed trigger switches address up to max_switches",
            {
                "a": ProviderSpec(
                    legs=3, triggers=frozenset({"upstream"}), max_switches=1
                ),
                "b": one,
            },
            (("a", "m1"), ("b", "m2")),
            Script({("a", None, None, None): [status(500, "internal error")]}),
        ),
        Scenario(
            "a healthy address left never goes direct",
            {"a": ProviderSpec(legs=3, max_live_failures=1), "b": one},
            (("a", "m1"), ("b", "m2")),
            Script({("a", None, None, "a-p0"): [CONNECT]}),
        ),
        Scenario(
            "every address dead falls back to the direct leg",
            {"a": ProviderSpec(legs=2), "b": one},
            (("a", "m1"), ("b", "m2")),
            Script(
                {
                    ("a", None, None, "a-p0"): [CONNECT],
                    ("a", None, None, "a-p1"): [CONNECT],
                }
            ),
        ),
        Scenario(
            "consecutive failures bench a model for later requests",
            {"a": one, "b": one},
            (("a", "m1"), ("b", "m2")),
            Script({("a", None, None, None): [status(500)]}),
            requests=4,
            health=lambda: RouteHealthRegistry(
                bench_enabled=True,
                mode="consecutive",
                eject_after_failures=2,
                eject_seconds=600,
            ),
        ),
        Scenario(
            "a reactive 429 block makes the next request step over the provider",
            {"a": ProviderSpec(routes_around_model=False), "b": one},
            (("a", "m1"), ("b", "m2")),
            Script(
                {
                    ("a", None, None, None): [
                        status(429, "slow down", **{"retry-after": "120"})
                    ]
                }
            ),
            requests=2,
        ),
    ]


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda scenario: scenario.name)
def test_media_decides_exactly_as_chat(scenario: Scenario) -> None:
    chat = _run_chat(scenario)
    media = _run_media(scenario)
    assert media["calls"] == chat["calls"], "upstream calls diverged"
    assert media["outcomes"] == chat["outcomes"], "final outcome diverged"
    assert media["ledgers"] == chat["ledgers"], "ledger verdicts diverged"
    assert media["benched"] == chat["benched"], "route-health benches diverged"
    assert media["keys"] == chat["keys"], "key health books diverged"


def test_the_scenarios_actually_exercise_the_rules() -> None:
    """A parity test over scenarios that never fail proves nothing."""
    chat = {s.name: _run_chat(s) for s in _scenarios()}
    ends = chat["a malformed 400 ends the route"]
    assert [call[0] for call in ends["calls"]] == ["a"]
    assert ends["outcomes"][0][1] == FailureKind.INVALID_REQUEST.value
    retried = chat["retry_once retries the primary once, then falls back"]
    assert [call[0] for call in retried["calls"]] == ["a", "a", "b"]
    around = chat["429 routes around to the same provider's next model"]
    assert [call[1] for call in around["calls"]] == ["m1", "m2"]
    blocked = chat["a 429 block longer than the probe bound leaves it inconclusive"]
    assert [call[1] for call in blocked["calls"]] == ["m1", "x"]
    escalated = chat["429 probe answering 429 escalates and retries the same model"]
    assert escalated["calls"] == [
        ("a", "m1", 0, "-"),
        ("a", "probe", 0, "-"),
        ("a", "m1", 1, "-"),
    ]
    ladder = chat["429 with nowhere to route spends the rate-limit ladder"]
    assert len(ladder["calls"]) == 3
    proxy = chat["a dead proxy moves to the next address"]
    assert [call[3] for call in proxy["calls"]] == ["a-p0", "a-p1"]
    switched = chat["an armed trigger switches address up to max_switches"]
    assert [call[3] for call in switched["calls"]] == ["a-p0", "a-p1", "-"]
    direct = chat["every address dead falls back to the direct leg"]
    assert [call[3] for call in direct["calls"]] == ["a-p0", "a-p1", DIRECT_PROXY_LABEL]
    withheld = chat["a healthy address left never goes direct"]
    # The bound of one live failure ends the chain at a-p0; a-p1 and a-p2 are
    # healthy, so Direct is withheld and the route moves on to b.
    assert [call[3] for call in withheld["calls"]] == ["a-p0", "-"]
    assert [call[0] for call in withheld["calls"]] == ["a", "b"]
    benched = chat["consecutive failures bench a model for later requests"]
    assert benched["benched"]["a/m1"] is True
    stepped = chat["a reactive 429 block makes the next request step over the provider"]
    assert [call[0] for call in stepped["calls"]] == ["a", "b", "b"]


# ------------------------------------------------ video jobs (7.64.0)
#
# A video job is chat's commit point in another form: a host that accepted
# the job holds it, the way a client that saw a stream's first words holds
# them. Chat never re-sends a committed stream to the next model; media never
# resubmits an accepted job. And a call on the accepted job goes out on the
# key that took it, charged the way an attempt on that key is charged.


class _CommitsThenFails(BaseProvider):
    """A chat leaf whose stream reaches the client, then dies."""

    def __init__(self, config: ProviderConfig, name: str, calls: list[str]) -> None:
        super().__init__(config)
        self._name = name
        self._calls = calls

    def preflight_stream(self, request, *, reasoning=DEFAULT_REASONING_POLICY) -> None:
        return None

    async def cleanup(self) -> None:
        return None

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset()

    def throttle_remaining(self, model: str | None = None) -> float:
        return 0.0

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[str]:
        self._calls.append(self._name)
        yield (
            "event: content_block_delta\ndata: "
            '{"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"ok"}}\n\n'
        )
        raise ExecutionFailure(
            kind=FailureKind.UPSTREAM,
            status_code=500,
            message="died after the client saw it",
            retryable=True,
        )


def _video_leaf(
    provider: str,
    config: ProviderConfig,
    handler: Callable[[httpx.Request], httpx.Response],
) -> MediaLeaf:
    return MediaLeaf(
        provider_id=provider,
        config=config,
        surfaces=video_surfaces(),
        rate_limiter=_leaf_limiter(config, proxied_leg=False),
        transport=httpx.MockTransport(handler),
    )


def _video_attempt(provider: str, model: str) -> MediaAttempt:
    return MediaAttempt(
        MediaRequest(
            operation=MEDIA_OPERATION_VIDEO_CREATE,
            rail=MediaRail.VIDEO,
            model="client-model",
            body={"prompt": "p"},
        ),
        _resolved(provider, model),
    )


def test_video_pin_after_accept_matches_chat_commit() -> None:
    """Accepted is committed: neither engine sends the work to the next model.

    Chat: a streaming answer whose first words reached the client and which
    then dies is not re-sent to ``b`` (the Messages-only continuation,
    ``FALLBACK_RESUME_AFTER_COMMIT``, is off: it has no media meaning).
    Media-only part, stated: the job ``a`` accepted is then read on ``a``
    alone; its poll reporting ``failed`` -- or failing outright -- is the
    answer, and ``b`` never receives a create.
    """
    chat_calls: list[str] = []
    chat_nodes = {
        name: _CommitsThenFails(_config(name, ProviderSpec()), name, chat_calls)
        for name in ("a", "b")
    }
    chat = ProviderExecutor(
        lambda provider_id: chat_nodes[provider_id],
        policy=RouteExecutionPolicy(resume_after_commit=False),
        health=RouteHealthRegistry(),
    )
    plan = RoutedMessagesPlan(
        tuple(
            RoutedMessagesRequest(
                request=MessagesRequest(
                    model=model,
                    messages=[Message(role="user", content="p")],
                    stream=True,
                ),
                resolved=_resolved(provider, model),
                reasoning=ReasoningPolicy.on(),
                requested_reasoning=ReasoningPolicy.on(),
                reasoning_adaptation=ReasoningAdaptation(
                    ReasoningAdaptationKind.UNCHANGED, None
                ),
            )
            for provider, model in (("a", "m1"), ("b", "m2"))
        )
    )

    creates: list[str] = []
    polls = iter(
        [
            httpx.Response(200, json={"id": "job-a", "status": "failed"}),
            httpx.Response(500, json={"error": {"message": "still broken"}}),
        ]
    )

    def media_handler(name: str) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                creates.append(name)
                return httpx.Response(
                    200, json={"id": f"job-{name}", "status": "processing"}
                )
            return next(polls)

        return handler

    leaves = {
        name: _video_leaf(name, _config(name, ProviderSpec()), media_handler(name))
        for name in ("a", "b")
    }
    media = MediaExecutor(
        lambda provider_id: leaves[provider_id], health=RouteHealthRegistry()
    )
    video_plan = MediaPlan(
        attempts=(_video_attempt("a", "m1"), _video_attempt("b", "m2"))
    )
    statuses: list[str | None] = []
    poll_errors: list[str | None] = []

    async def run() -> None:
        install_attribution()
        # However the committed stream ends (cleanly or raised), it is not re-sent.
        with contextlib.suppress(Exception):
            async for _chunk in chat.stream(
                plan,
                wire_api="chat_completions",
                raw_log_label="X",
                raw_log_payload={},
                request_id="parity",
            ):
                pass
        install_attribution()
        async for chunk in media.execute(video_plan, request_id="parity"):
            assert not isinstance(chunk, bytes)
            accepted = parse_job(chunk.body)
            assert accepted is not None
            assert accepted.id == "job-a"
        pinned = PinnedMediaClient(
            leaves["a"], key_index=0, state=None, health=RouteHealthRegistry()
        )
        answer = await pinned.call(
            MEDIA_OPERATION_VIDEO_RETRIEVE, "job-a", model="m1", request_id="parity"
        )
        polled = parse_job(answer.body)
        statuses.append(None if polled is None else polled.status)
        try:
            await pinned.call(
                MEDIA_OPERATION_VIDEO_RETRIEVE,
                "job-a",
                model="m1",
                request_id="parity",
            )
        except Exception as exc:
            poll_errors.append(failure_kind_name(exc))
        for leaf in leaves.values():
            await leaf.cleanup()

    asyncio.run(run())
    assert chat_calls == ["a"]
    assert creates == ["a"]
    assert statuses == ["failed"]
    assert poll_errors == [FailureKind.UPSTREAM.value]


@pytest.mark.parametrize(
    "refusal",
    [
        status(429, "slow down", **{"retry-after": "30"}),
        status(401, "invalid api key"),
    ],
    ids=["429", "401"],
)
def test_pinned_poll_charges_key_books_like_a_chat_attempt(refusal: Outcome) -> None:
    """The same refusal on key 0 leaves the same record of key 0 on both books.

    Chat: one attempt on key 0 (a 429 benches the (key, model) pair and stops;
    a 401 locks the key out and the pool moves on to key 1). Media: the job
    was accepted on key 0 -- one request on that key, as chat's attempt is --
    and one pinned poll on key 0 is refused the same way. Media-only part,
    stated: the poll never moves to key 1 (the job exists only on key 0), so
    after a 401 key 1's record differs from chat's by exactly the request chat
    sent it, and media makes no second call.
    """
    spec = ProviderSpec(keys=2, cooldown=2.0)
    chat_script = Script({("a", "m1", 0, None): [refusal]})
    chat_provider, chat_state = _chat_provider(chat_script, "a", spec)
    assert chat_state is not None

    config = _config("a", spec)
    media_calls: list[tuple[str, int]] = []

    def media_handler(key: int) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            media_calls.append((request.method, key))
            if request.method == "POST":
                return httpx.Response(
                    200, json={"id": f"job-{key}", "status": "queued"}
                )
            return httpx.Response(
                refusal.status,
                json={"error": {"message": refusal.message}},
                headers=dict(refusal.headers),
            )

        return handler

    media_state = _key_state(config, 2)
    pool = MediaKeyPool(
        [
            _video_leaf("a", sub, media_handler(index))
            for index, sub in enumerate(_per_key(config))
        ],
        media_state,
        key_labels=("k0", "k1"),
        provider_id="a",
        routes_around_model=config.routes_around_model,
    )
    books: dict[str, Any] = {}

    async def run() -> None:
        install_attribution()
        request = MessagesRequest(
            model="m1", messages=[Message(role="user", content="p")], stream=False
        )
        with contextlib.suppress(Exception):
            async for _chunk in chat_provider.stream_response(
                request, request_id="parity"
            ):
                pass
        books["chat"] = _key_books(chat_state)

        install_attribution()
        async for _chunk in pool.execute(
            _video_attempt("a", "m1"), request_id="parity"
        ):
            pass
        leaf = pool.leaf_for(0, None)
        assert leaf is not None
        pinned = PinnedMediaClient(
            leaf, key_index=0, state=media_state, health=RouteHealthRegistry()
        )
        with pytest.raises(ExecutionFailure):
            await pinned.call(
                MEDIA_OPERATION_VIDEO_RETRIEVE, "job-0", model="m1", request_id="parity"
            )
        books["media"] = _key_books(media_state)
        await pool.cleanup()
        await chat_provider.cleanup()

    asyncio.run(run())
    chat_books, media_books = books["chat"], books["media"]
    assert media_books[0] == chat_books[0], "key 0's record diverged"
    assert media_calls == [("POST", 0), ("GET", 0)], "the pinned poll moved"
    untouched = _key_books(_key_state(config, 2))
    assert untouched is not None
    assert media_books[1] == untouched[1]
    if refusal.status == 429:
        assert media_books == chat_books
        assert chat_books[0][7] == ("m1",)
        assert [call[2] for call in chat_script.calls] == [0]
    else:
        assert chat_books[0][6] is True, "a 401 locks the key out on both books"
        assert [call[2] for call in chat_script.calls] == [0, 1]
