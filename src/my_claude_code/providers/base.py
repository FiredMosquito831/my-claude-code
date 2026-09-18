"""Base provider interface - extend this to implement your own provider."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from loguru import logger

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.constants import (
    CREDENTIAL_LOCKOUT_TIERS_DEFAULT,
    CREDENTIAL_MODEL_BENCH_ESCALATION_DEFAULT,
    FALLBACK_ON_REASONING_ONLY_DEFAULT,
    HTTP_CONNECT_TIMEOUT_DEFAULT,
    PROVIDER_RETRY_ATTEMPTS_DEFAULT,
    PROVIDER_RETRY_BACKOFF_BASE_SECONDS_DEFAULT,
    PROVIDER_RETRY_BACKOFF_JITTER_SECONDS_DEFAULT,
    PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT,
    RATE_LIMIT_COOLDOWN_MAX_SECONDS_DEFAULT,
    RATE_LIMIT_COOLDOWN_MODE_DEFAULT,
    RATE_LIMIT_COOLDOWN_SECONDS_DEFAULT,
    RATE_LIMIT_ROUTES_AROUND_MODEL_DEFAULT,
    STREAM_COMMIT_HOLDBACK_CHARS_DEFAULT,
    STREAM_COMMIT_HOLDBACK_SECONDS_DEFAULT,
    STREAM_EARLY_RETRY_ATTEMPTS_DEFAULT,
    STREAM_MIDSTREAM_RECOVERY_ATTEMPTS_DEFAULT,
)
from my_claude_code.config.credentials import mask_key_label
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.diagnostics import (
    exception_cause_types,
    redacted_exception_traceback,
)
from my_claude_code.core.rate_limit import (
    DEFAULT_RATE_LIMIT_COOLDOWN,
    RateLimitCooldown,
)
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningDialect,
    ReasoningPolicy,
)
from my_claude_code.core.trace import trace_event
from my_claude_code.providers.model_listing import model_infos_from_ids


@dataclass(frozen=True, slots=True)
class ProxyLeg:
    """One rung of a resolved proxy chain, as the runtime sees it.

    ``url`` is the address a leaf provider is built against; ``""`` is the
    Direct rung, which is a real, deliberate choice and not an absence.
    ``label`` is ``host:port`` with any ``user:pass`` already removed -- it is
    the *only* form of the address that may reach a log line or the request
    log, and it is masked here rather than at every reader.

    Deliberately not ``config.proxy_chains.ProxyChainEntry``: that type names an
    endpoint by id and belongs to the store, which nothing on the request path
    imports. By the time a chain is a ``ProxyLeg`` the ids are resolved, the
    paused rungs are gone, and the runtime needs no second lookup.
    """

    url: str = ""
    label: str = ""


@dataclass(frozen=True, slots=True)
class ProxyChainPlan:
    """A provider's whole egress plan: the rungs, and the rules for moving.

    One field on :class:`ProviderConfig` rather than five, because these five
    values are only ever read together, by one constructor, and a provider with
    no chain carries ``None`` instead of five defaults that mean nothing.
    """

    legs: tuple[ProxyLeg, ...] = ()
    #: One of the four credential-rotation policy names. The same vocabulary,
    #: not a second one that happens to overlap.
    policy: str = "failover"
    #: Canonical ``FailureKind`` values that move the chain along.
    on: frozenset[str] = frozenset()
    #: ``provider`` or ``credential`` -- how wide a triggering bench reaches.
    scope: str = "provider"
    #: How many times one request may move to the next rung *because the
    #: operator armed a trigger chip*. Unchanged in meaning and in range.
    max_switches: int = 2
    #: Whether a request with no healthy rung left goes out on this machine's
    #: own address rather than failing. True for every chain that has not said
    #: otherwise, including every chain stored before the field existed.
    direct_fallback: bool = True


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Resolved immutable configuration for one provider instance.

    Base fields apply to all providers. Provider-specific parameters
    (e.g. NIM temperature, top_p) are passed by the provider constructor.

    ``api_key`` is the first configured credential and remains the value all
    existing providers use. ``api_keys`` carries every configured credential
    (comma-separated in env) for multi-key rotation; ``credential_rotation``
    selects the policy: ``single`` (default), ``round_robin`` or ``on_error``.
    """

    api_key: str
    base_url: str
    rate_limit: int | None = None
    rate_window: int = 60
    max_concurrency: int = 5
    http_read_timeout: float = 300.0
    http_write_timeout: float = 10.0
    http_connect_timeout: float = HTTP_CONNECT_TIMEOUT_DEFAULT
    proxy: str = ""
    log_raw_sse_events: bool = False
    log_api_error_tracebacks: bool = False
    api_keys: tuple[str, ...] = ()
    credential_rotation: str = "single"
    # Resilience: how long a failing model may hold one request.
    retry_attempts: int = PROVIDER_RETRY_ATTEMPTS_DEFAULT
    early_retry_attempts: int = STREAM_EARLY_RETRY_ATTEMPTS_DEFAULT
    midstream_recovery_attempts: int = STREAM_MIDSTREAM_RECOVERY_ATTEMPTS_DEFAULT
    commit_holdback_seconds: float = STREAM_COMMIT_HOLDBACK_SECONDS_DEFAULT
    commit_holdback_chars: int = STREAM_COMMIT_HOLDBACK_CHARS_DEFAULT
    fallback_on_reasoning_only: bool = FALLBACK_ON_REASONING_ONLY_DEFAULT
    rate_limit_cooldown_seconds: float = RATE_LIMIT_COOLDOWN_SECONDS_DEFAULT
    # Backoff schedule for this provider's own retries of a 429 or 5xx.
    retry_backoff_base_seconds: float = PROVIDER_RETRY_BACKOFF_BASE_SECONDS_DEFAULT
    retry_backoff_max_seconds: float = PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT
    retry_backoff_jitter_seconds: float = PROVIDER_RETRY_BACKOFF_JITTER_SECONDS_DEFAULT
    # The escalating bench a credential earns for a 401/403, in seconds. The
    # only ladder a key still walks: a 429 waits exactly as long as the
    # provider asked, and nothing else changes a key's health at all.
    lockout_tiers: tuple[float, ...] = tuple(
        float(part) for part in CREDENTIAL_LOCKOUT_TIERS_DEFAULT.split(",")
    )
    # Distinct models that must be rate-limited on one key at once before the
    # key itself is benched instead of just the (key, model) pair.
    credential_model_bench_escalation: int = CREDENTIAL_MODEL_BENCH_ESCALATION_DEFAULT
    # Whether a 429 is answered by routing to another model instead of by
    # retrying this one and then spending the rest of the key pool on it.
    routes_around_model: bool = RATE_LIMIT_ROUTES_AROUND_MODEL_DEFAULT
    # What a 429 costs at all: honour the host's own wait (7.21.0 and every
    # release before it), always use the operator's number, or nothing.
    rate_limit_cooldown_mode: str = RATE_LIMIT_COOLDOWN_MODE_DEFAULT
    # The ceiling on a wait a host published in a header. 0 removes it.
    rate_limit_cooldown_max_seconds: float = RATE_LIMIT_COOLDOWN_MAX_SECONDS_DEFAULT
    # This provider's egress chain, already resolved: ids looked up, paused
    # rungs dropped, passwords masked into labels. ``None`` -- the default and
    # by far the common case -- means "no chain": ``proxy`` above is the single
    # static address, exactly as it has always been. A plan is present only
    # when there are two or more rungs to move between; one rung is simply
    # ``proxy``, and zero is no chain at all.
    proxy_chain: ProxyChainPlan | None = None

    def rate_limit_cooldown(self) -> RateLimitCooldown:
        """The operator's whole 429 policy, as one value to hand around.

        The three settings only mean anything together, and every bench, every
        (key, model) bench and every reactive block resolves through the one
        object this builds -- there is no second reading of the rule anywhere.
        """
        return RateLimitCooldown(
            mode=self.rate_limit_cooldown_mode,
            fallback_seconds=self.rate_limit_cooldown_seconds,
            max_seconds=self.rate_limit_cooldown_max_seconds,
        )


class BaseProvider(ABC):
    """Base class for all providers. Extend this to add your own."""

    def __init__(self, config: ProviderConfig):
        self._config = config

    def rate_limit_cooldown(self) -> RateLimitCooldown:
        """This provider's 429 policy: mode, fallback and ceiling, together.

        Read through ``getattr`` rather than ``self._config`` directly because
        a provider's failure classifier is sometimes bound to a bare instance
        (``object.__new__``) to exercise classification without building a
        live provider. A policy is never what such a caller is asking about,
        so it gets the shipped one.
        """
        config = getattr(self, "_config", None)
        if config is None:
            return DEFAULT_RATE_LIMIT_COOLDOWN
        return config.rate_limit_cooldown()

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect | None:
        """Which reasoning fields this host parses for ``model_id``.

        ``None`` -- the default -- means unknown, and unknown never adds a
        restriction: gating then behaves exactly as it did before dialects
        existed. A provider overrides this only once it can say what its
        upstream actually reads, because a wrong answer here suppresses a
        control the gateway would have honoured.

        Deliberately NOT on ``application.ports.ProviderPort``: routing reads
        it through the provider manager, not through the executor's port, so
        no test double has to grow a member for it.
        """
        return None

    def throttle_remaining(self, model: str | None = None) -> float:
        """Seconds this provider's credential is rate-limited for, 0 if free.

        Rotation uses this to prefer a credential that can serve immediately
        over one that would only sit waiting inside its own limiter. Providers
        that do not rate-limit their upstream are never throttled; those that
        do override this to report their limiter.
        """
        return 0.0

    @property
    def credential_label(self) -> str | None:
        """Masked label of the credential this provider uses, for analytics.

        Providers that rotate across several credentials return ``None`` here
        and report the credential they actually picked per request instead.
        """
        return mask_key_label(self._config.api_key) if self._config.api_key else None

    @abstractmethod
    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        """Validate the upstream request before opening an SSE stream."""

    def _log_stream_transport_error(
        self,
        tag: str,
        req_tag: str,
        error: Exception,
        *,
        request_id: str | None = None,
    ) -> None:
        """Log streaming transport failures (metadata-only unless verbose is enabled)."""
        response = getattr(error, "response", None)
        http_status = (
            getattr(response, "status_code", None) if response is not None else None
        )
        cause_types = exception_cause_types(error)
        trace_event(
            stage="provider",
            event="provider.response.transport_error",
            source="provider",
            provider=tag,
            request_id=request_id,
            exc_type=type(error).__name__,
            http_status=http_status,
            cause_types=cause_types,
        )

        if self._config.log_api_error_tracebacks:
            logger.error(
                "{}_ERROR:{} exc_type={}\n{}",
                tag,
                req_tag,
                type(error).__name__,
                redacted_exception_traceback(error),
            )
            return
        logger.error(
            "{}_ERROR:{} exc_type={} http_status={} cause_types={}",
            tag,
            req_tag,
            type(error).__name__,
            http_status,
            ",".join(cause_types) if cause_types else None,
        )

    @abstractmethod
    async def cleanup(self) -> None:
        """Release any resources held by this provider."""

    @abstractmethod
    async def list_model_ids(self) -> frozenset[str]:
        """Return the model ids currently advertised by this provider."""

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        """Return advertised model ids with optional provider capability metadata."""
        return model_infos_from_ids(await self.list_model_ids())

    @abstractmethod
    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        """Stream response in Anthropic SSE format."""
