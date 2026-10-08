"""Provider configuration construction from neutral catalog metadata."""

import os
from dataclasses import dataclass

from loguru import logger

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.config.credentials import mask_proxy_label, parse_credential_keys
from my_claude_code.config.env_files import env_file_override
from my_claude_code.config.provider_catalog import ProviderDescriptor
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.proxy_chains import (
    OAUTH_PROVIDER_IDS,
    current_proxy_chains,
    masked_refusal_sentence,
)
from my_claude_code.config.settings import Settings, parse_lockout_tiers
from my_claude_code.core.proxy_attribution import DIRECT_PROXY_LABEL
from my_claude_code.core.proxy_rotation import (
    PROXY_HEALTH,
    PROXY_INTERCEPTION,
    PROXY_REACHABILITY,
)
from my_claude_code.providers.base import (
    MaskedRefusalPlan,
    ProviderConfig,
    ProxyChainPlan,
    ProxyLeg,
)

CREDENTIAL_ROTATION_POLICIES = frozenset(
    {"single", "round_robin", "least_used", "failover", "on_error"}
)
DEFAULT_CREDENTIAL_ROTATION = "single"


@dataclass(frozen=True, slots=True)
class ProxyRoute:
    """What :func:`resolve_proxy_route` resolved one provider's egress to.

    ``proxy`` and ``plan`` are exactly the pair :func:`resolve_proxy_chain` has
    always returned. ``label`` is new in 7.79.2 and says only which chain entry
    a one-entry chain collapsed to -- its name on the Proxying page -- so the
    request log can name it; it is ``""`` in every other case.
    """

    proxy: str
    plan: ProxyChainPlan | None = None
    label: str = ""


def resolve_proxy_chain(
    provider_id: str, static_proxy: str, settings: Settings, *, name: str = ""
) -> tuple[str, ProxyChainPlan | None]:
    """:func:`resolve_proxy_route` as the ``(proxy, plan)`` pair it has always been."""

    route = resolve_proxy_route(provider_id, static_proxy, settings, name=name)
    return route.proxy, route.plan


def resolve_proxy_route(
    provider_id: str, static_proxy: str, settings: Settings, *, name: str = ""
) -> ProxyRoute:
    """Turn the stored chain for one provider into what the runtime uses.

    Returns the ``(proxy, plan)`` pair as a :class:`ProxyRoute`, with the
    one-entry chain's ``label`` beside it. The whole upgrade story is in the first branch:
    a provider absent from the store -- or one whose chain is switched off, or
    empty once its paused rungs are dropped -- keeps its ``<PROVIDER>_PROXY``
    untouched and gets no plan at all, which is byte-for-byte the behaviour of
    every release before this one. The ``.env`` key is never rewritten; a chain
    with entries simply stops it being consulted.

    With one exception, the one place the decision is made (7.78.8): a chain
    that is switched on, has **Direct fallback off** and has nothing to route
    through -- every entry paused, every address removed, no entry at all, or
    a chain file that could not be read -- with no ``<PROVIDER>_PROXY`` left to
    carry the provider either. Collapsing that to "no plan, no proxy" built a
    provider that dialled from this computer's own address, the one thing the
    operator who switched Direct fallback off said must never happen. It
    resolves to a :class:`MaskedRefusalPlan` instead, which every construction
    seam builds as a refusal (a 503 naming the setting), and which the
    Providers card's probes read as "not sent". ``name`` is the provider as
    the operator knows it, for that sentence. With a ``<PROVIDER>_PROXY`` to
    fall back to, nothing changes.

    Since 7.79.2 the same refusal holds with Direct fallback ON for a chain
    that has entries but none usable (every one paused or naming a removed
    address) or that stands in for an unreadable file: Direct fallback uses
    this computer's address only once every proxy is *unhealthy* (the user's
    decision of 2026-10-06 23:03), and paused, removed or unreadable is not
    that -- nor is a switched-on chain with no entry at all, which is what a
    chain whose addresses were all removed reads as. Switching the chain
    off is the operator's way to send it from this computer.

    One rung collapses to a static proxy rather than a pool: there is nothing
    to move between, and a one-entry pool would build a second client, a second
    limiter and a second recovery ladder to do nothing with them.

    The switch bound is the per-provider number under the operator's global
    ceiling. Two numbers, and the smaller wins: ``PROXY_MAX_SWITCHES_PER_REQUEST``
    on Limits & Resilience is the most any chain on this install may spend
    inside one attempt, and the card's own value is the choice within it.
    """

    # One read for the chain and the addresses it names. Two reads could
    # straddle a save on a worker thread and resolve the old chain's entries
    # against the new catalogue -- where an address the save removed is simply
    # missing, and a chain left with no legs routes direct (7.72.1).
    store = current_proxy_chains()
    chain = store.chain(provider_id)
    if chain is None or not chain.enabled:
        return ProxyRoute(static_proxy)
    if provider_id in OAUTH_PROVIDER_IDS and not chain.oauth_acknowledged:
        # A subscription login whose operator has not said they understand what
        # changing source address means for it. The rail is inert on the page
        # and the chain is inert here, or the acknowledgement would be theatre.
        return ProxyRoute(static_proxy)

    legs: list[ProxyLeg] = []
    for entry in chain.entries:
        if entry.paused:
            # Kept in the store and in the page, skipped at selection -- the
            # same semantics a paused route entry has. Building no leaf for it
            # is also one fewer client.
            continue
        if entry.is_direct:
            legs.append(ProxyLeg(url="", label=DIRECT_PROXY_LABEL))
            continue
        endpoint = store.endpoint(entry.proxy)
        if endpoint is None:
            continue
        legs.append(
            ProxyLeg(
                url=endpoint.url,
                label=endpoint.label or mask_proxy_label(endpoint.url),
            )
        )

    if not legs:
        if static_proxy:
            # A masked answer: the chain stands aside and ``<PROVIDER>_PROXY``
            # carries it, as it always has.
            return ProxyRoute(static_proxy)
        who = name or provider_id
        return ProxyRoute(
            "",
            MaskedRefusalPlan(
                direct_fallback=chain.direct_fallback,
                reason=masked_refusal_sentence(store, provider_id, who)
                # Unreachable -- the cause and the legs are the same test -- but
                # this line runs on the way to a refusal and must not be empty.
                or (
                    f"Not sent: {who}'s proxy chain has no usable entry and Direct "
                    f"fallback is off (Proxying page -> {who})."
                ),
            ),
        )
    if len(legs) == 1:
        return ProxyRoute(legs[0].url, label=legs[0].label)
    return ProxyRoute(
        "",
        ProxyChainPlan(
            legs=tuple(legs),
            policy=chain.policy,
            on=frozenset(chain.on),
            scope=chain.scope,
            max_switches=min(
                chain.max_switches, int(settings.proxy_max_switches_per_request)
            ),
            direct_fallback=chain.direct_fallback,
        ),
    )


@dataclass(frozen=True, slots=True)
class MaskedExit:
    """Where one out-of-band request to a provider leaves this computer from.

    The Providers card's probes are not requests: nothing above them rotates,
    retries or falls back, so they cannot be handed a chain. They are handed one
    exit, picked by :func:`masked_exit_for` from the chain real requests are
    built from.

    ``proxy`` is what the probe passes to ``httpx`` -- ``None`` or ``""`` is this
    computer's own address. ``label`` is the masked name of the chain entry that
    decided it, and ``None`` when no chain did (the static ``<PROVIDER>_PROXY``
    or the registry entry's proxy, the same value as before). ``refused`` is a
    sentence when nothing may carry the request: the caller sends nothing and
    shows it.
    """

    proxy: str | None
    label: str | None = None
    refused: str = ""


#: The health states a probe never dials into. Both are the two sets
#: ``ProxyRotationState`` holds out of selection: an address on the
#: reachability ladder until a check passes, and an address measured
#: terminating TLS.
_EXIT_STATES_NEVER_DIALLED = frozenset({"unreachable", "intercepted"})


def _exit_state(provider_id: str, label: str) -> str:
    """One chain entry's health, as the Proxying page reads it.

    The Direct entry is never charged a reachability failure or an
    interception -- ``ProxyRotationState`` exempts it the same way -- so only a
    trigger bench (``cooldown``) can hold it back.
    """

    if label != DIRECT_PROXY_LABEL:
        if PROXY_INTERCEPTION.is_refused(label):
            return "intercepted"
        if PROXY_REACHABILITY.is_unhealthy(label):
            return "unreachable"
    return str(PROXY_HEALTH.snapshot(provider_id, label)["state"])


def masked_exit_for(
    provider_id: str,
    static_proxy: str | None,
    settings: Settings,
    *,
    name: str = "",
) -> MaskedExit:
    """The one exit a probe of this provider goes out through.

    The chain is resolved by :func:`resolve_proxy_chain` -- the same single read
    every real request is built from, so paused entries, removed addresses, the
    OAuth acknowledgement and a switched-off chain mean exactly what they mean
    there. Then, in the chain's order (only its first entry under the
    ``single`` policy, which is all a real request ever uses):

    1. the first entry that is neither unreachable, intercepted nor in a
       trigger cooldown for this provider;
    2. else the first entry that is merely in cooldown -- it still answers, and
       this computer's address may be used only once every proxy is unhealthy;
    3. else, every entry being unreachable or intercepted: this computer's own
       address if the chain's Direct fallback is on, and nothing at all if it is
       off (``refused`` says why and names the setting).

    No chain (or one the resolver drops) hands back ``static_proxy`` itself,
    so a provider without a chain probes exactly as it always has. A one-entry
    chain is its one entry, as it is for real requests. A chain the resolver
    refuses (:class:`MaskedRefusalPlan`: Direct fallback off and no usable
    entry) is refused here with the very sentence a real request gets.

    This reads the shared health ledgers and writes nothing to them. A probe's
    outcome reaches its caller as a type name, not the exception the rotation
    classifies, so charging an exit from here would be a second, guessed
    bookkeeping.
    """

    proxy, plan = resolve_proxy_chain(
        provider_id, static_proxy or "", settings, name=name
    )
    if isinstance(plan, MaskedRefusalPlan):
        return MaskedExit(proxy=None, refused=plan.reason)
    if plan is None:
        if proxy == (static_proxy or ""):
            return MaskedExit(proxy=static_proxy)
        return MaskedExit(
            proxy=proxy,
            label=mask_proxy_label(proxy) if proxy else DIRECT_PROXY_LABEL,
        )
    legs = plan.legs[:1] if plan.policy == "single" else plan.legs
    usable: list[tuple[str, str, bool]] = []
    for leg in legs:
        label = leg.label or DIRECT_PROXY_LABEL
        state = _exit_state(provider_id, label)
        if state not in _EXIT_STATES_NEVER_DIALLED:
            usable.append((leg.url, label, state == "cooldown"))
    chosen = next((rung for rung in usable if not rung[2]), None) or next(
        iter(usable), None
    )
    if chosen is not None:
        return MaskedExit(proxy=chosen[0], label=chosen[1])
    if plan.direct_fallback:
        return MaskedExit(proxy="", label=DIRECT_PROXY_LABEL)
    who = name or provider_id
    return MaskedExit(
        proxy=None,
        refused=(
            f"Not sent: no proxy in {who}'s chain can be used right now "
            "(every one is unreachable or refused) and Direct fallback is off, "
            "so it would have gone out from this computer's own address. "
            f"Re-check the chain or turn Direct fallback on: Proxying page, {who}."
        ),
    )


def string_setting(settings: Settings, attr_name: str | None, default: str = "") -> str:
    """Return a string-valued settings attribute, ignoring non-string mocks."""
    if attr_name is None:
        return default
    value = getattr(settings, attr_name, default)
    return value if isinstance(value, str) else default


def provider_credential(descriptor: ProviderDescriptor, settings: Settings) -> str:
    """Return the configured credential for a provider descriptor."""
    if descriptor.static_credential is not None:
        return descriptor.static_credential
    if descriptor.credential_attr:
        return string_setting(settings, descriptor.credential_attr)
    return ""


def require_provider_credential(
    descriptor: ProviderDescriptor, credential: str
) -> None:
    """Raise a user-facing configuration error when a required key is missing."""
    if descriptor.credential_env is None:
        return
    if descriptor.credential_discoverable:
        # The provider finds its own credential (a stored login, or a file
        # another tool owns), so an empty setting is not an error here. The
        # provider raises its own, more specific error if discovery fails too.
        return
    if credential and credential.strip():
        return
    message = f"{descriptor.credential_env} is not set. Add it to your .env file."
    if descriptor.credential_url:
        message = f"{message} Get a key at {descriptor.credential_url}"
    raise ApplicationUnavailableError(message)


def credential_rotation_policy(
    descriptor: ProviderDescriptor, settings: Settings
) -> str:
    """Resolve the credential rotation policy for one provider.

    The policy is read from ``{CREDENTIAL_ENV}_ROTATION`` in the configured
    dotenv files first, then the process environment. Unknown values fall back
    to ``single`` so a typo never breaks provider construction.
    """
    if descriptor.credential_env is None:
        return DEFAULT_CREDENTIAL_ROTATION
    env_key = f"{descriptor.credential_env}_ROTATION"
    value = env_file_override(settings.model_config, env_key)
    if value is None:
        value = os.environ.get(env_key, "")
    value = value.strip().lower()
    if value in CREDENTIAL_ROTATION_POLICIES:
        return value
    if value:
        # Silently downgrading a typo to ``single`` pins every request to the
        # first key with no visible signal, so say something.
        logger.warning(
            "{}={!r} is not a rotation policy ({}); using {}.",
            env_key,
            value,
            ", ".join(sorted(CREDENTIAL_ROTATION_POLICIES)),
            DEFAULT_CREDENTIAL_ROTATION,
        )
    return DEFAULT_CREDENTIAL_ROTATION


def build_provider_config(
    descriptor: ProviderDescriptor, settings: Settings
) -> ProviderConfig:
    """Build shared provider configuration for one provider descriptor."""
    if descriptor.dynamic:
        return _build_dynamic_provider_config(descriptor, settings)
    credential = provider_credential(descriptor, settings)
    require_provider_credential(descriptor, credential)
    api_keys = parse_credential_keys(credential)
    rotation = credential_rotation_policy(descriptor, settings)
    base_url = string_setting(
        settings, descriptor.base_url_attr, descriptor.default_base_url or ""
    )
    resolved_base_url = base_url or descriptor.default_base_url
    if not resolved_base_url:
        raise ApplicationUnavailableError(
            f"{descriptor.provider_id.upper()}_BASE_URL is not set. "
            f"Configure the base URL for provider {descriptor.provider_id!r}."
        )
    route = resolve_proxy_route(
        descriptor.provider_id,
        string_setting(settings, descriptor.proxy_attr),
        settings,
        name=descriptor.display_name,
    )
    return ProviderConfig(
        proxy_chain=route.plan,
        proxy_label=route.label,
        api_key=api_keys[0] if api_keys else credential,
        base_url=resolved_base_url,
        rate_limit=settings.provider_rate_limit,
        rate_window=settings.provider_rate_window,
        max_concurrency=settings.provider_max_concurrency,
        http_read_timeout=settings.http_read_timeout,
        http_write_timeout=settings.http_write_timeout,
        http_connect_timeout=settings.http_connect_timeout,
        proxy=route.proxy,
        log_raw_sse_events=settings.log_raw_sse_events,
        log_api_error_tracebacks=settings.log_api_error_tracebacks,
        api_keys=api_keys,
        credential_rotation=rotation,
        retry_attempts=settings.provider_retry_attempts,
        early_retry_attempts=settings.stream_early_retry_attempts,
        midstream_recovery_attempts=settings.stream_midstream_recovery_attempts,
        commit_holdback_seconds=settings.stream_commit_holdback_seconds,
        commit_holdback_chars=settings.stream_commit_holdback_chars,
        fallback_on_reasoning_only=settings.fallback_on_reasoning_only,
        rate_limit_cooldown_seconds=settings.rate_limit_cooldown_seconds,
        rate_limit_cooldown_mode=settings.rate_limit_cooldown_mode,
        rate_limit_cooldown_max_seconds=settings.rate_limit_cooldown_max_seconds,
        retry_backoff_base_seconds=settings.provider_retry_backoff_base_seconds,
        retry_backoff_max_seconds=settings.provider_retry_backoff_max_seconds,
        retry_backoff_jitter_seconds=settings.provider_retry_backoff_jitter_seconds,
        lockout_tiers=parse_lockout_tiers(settings.credential_lockout_tiers),
        credential_model_bench_escalation=settings.credential_model_bench_escalation,
        routes_around_model=settings.rate_limit_routes_around_model,
    )


def _build_dynamic_provider_config(
    descriptor: ProviderDescriptor, settings: Settings
) -> ProviderConfig:
    """Build provider configuration for a registry-backed custom provider."""
    entry = get_provider_registry().get(descriptor.provider_id)
    if entry is None:
        raise ApplicationUnavailableError(
            f"Custom provider {descriptor.provider_id!r} is not registered. "
            "Add it again from the admin dashboard."
        )
    rotation = entry.credential_rotation
    if rotation not in CREDENTIAL_ROTATION_POLICIES:
        rotation = DEFAULT_CREDENTIAL_ROTATION
    route = resolve_proxy_route(
        descriptor.provider_id,
        entry.proxy or "",
        settings,
        name=entry.display_name,
    )
    return ProviderConfig(
        proxy_chain=route.plan,
        proxy_label=route.label,
        api_key=entry.api_keys[0] if entry.api_keys else "",
        base_url=entry.base_url,
        rate_limit=settings.provider_rate_limit,
        rate_window=settings.provider_rate_window,
        max_concurrency=settings.provider_max_concurrency,
        http_read_timeout=settings.http_read_timeout,
        http_write_timeout=settings.http_write_timeout,
        http_connect_timeout=settings.http_connect_timeout,
        proxy=route.proxy,
        log_raw_sse_events=settings.log_raw_sse_events,
        log_api_error_tracebacks=settings.log_api_error_tracebacks,
        api_keys=entry.api_keys,
        retry_attempts=settings.provider_retry_attempts,
        early_retry_attempts=settings.stream_early_retry_attempts,
        midstream_recovery_attempts=settings.stream_midstream_recovery_attempts,
        commit_holdback_seconds=settings.stream_commit_holdback_seconds,
        commit_holdback_chars=settings.stream_commit_holdback_chars,
        fallback_on_reasoning_only=settings.fallback_on_reasoning_only,
        rate_limit_cooldown_seconds=settings.rate_limit_cooldown_seconds,
        rate_limit_cooldown_mode=settings.rate_limit_cooldown_mode,
        rate_limit_cooldown_max_seconds=settings.rate_limit_cooldown_max_seconds,
        retry_backoff_base_seconds=settings.provider_retry_backoff_base_seconds,
        retry_backoff_max_seconds=settings.provider_retry_backoff_max_seconds,
        retry_backoff_jitter_seconds=settings.provider_retry_backoff_jitter_seconds,
        lockout_tiers=parse_lockout_tiers(settings.credential_lockout_tiers),
        credential_model_bench_escalation=settings.credential_model_bench_escalation,
        routes_around_model=settings.rate_limit_routes_around_model,
        credential_rotation=rotation,
    )
