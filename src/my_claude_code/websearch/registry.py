"""Web search provider registry: build from settings, resolve the active one, search.

Analytics seam: :func:`search` accepts an optional attempt recorder;
:func:`search_with_logging` defaults it to ``websearch.analytics.record_search``.
Route owners may emit one correlated :class:`SearchRouteOutcome` through
:func:`emit_route_outcome`.
"""

import importlib
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from loguru import logger

from my_claude_code.config.credentials import parse_credential_keys
from my_claude_code.config.env_files import env_file_override
from my_claude_code.config.settings import Settings
from my_claude_code.config.websearch_catalog import (
    WEBSEARCH_CATALOG,
    WebSearchDescriptor,
)
from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
    WebSearchResultItem,
)

from .adapters import ADAPTER_CLASSES
from .base import BaseWebSearchProvider, KeyChargeJudge, WebSearchProviderConfig
from .errors import WebSearchConfigError, WebSearchError
from .options import option_enabled, read_websearch_options
from .rotation import (
    ROTATION_POLICIES,
    default_rotation_policy,
)

DEFAULT_HTTP_TIMEOUT = 20.0
ROTATION_ENV_SUFFIX = "_ROTATION"
ON_ERROR_ROTATION_ALIAS = "on_error"
WEBSEARCH_PROXY_ENV = "WEBSEARCH_PROXY"
_QUERY_LOG_CHARS = 256
_ERROR_MESSAGE_LOG_CHARS = 500

_TELEMETRY_DOOR: ContextVar[str | None] = ContextVar(
    "websearch_telemetry_door", default=None
)
_TELEMETRY_TOOL_KIND: ContextVar[str | None] = ContextVar(
    "websearch_telemetry_tool_kind", default=None
)
_TELEMETRY_ROTATION_SINK: ContextVar[Callable[[int], None] | None] = ContextVar(
    "websearch_telemetry_rotation_sink", default=None
)


def current_telemetry_door() -> str | None:
    """Return the active web-tool door tag for the current async task."""

    return _TELEMETRY_DOOR.get()


def current_telemetry_tool_kind() -> str | None:
    """Return the active web-tool kind tag for the current async task."""

    return _TELEMETRY_TOOL_KIND.get()


def reset_telemetry_contextvars() -> None:
    """Reset telemetry ContextVars to defaults (for test isolation)."""

    _TELEMETRY_DOOR.set(None)
    _TELEMETRY_TOOL_KIND.set(None)
    _TELEMETRY_ROTATION_SINK.set(None)


@contextmanager
def websearch_telemetry_context(
    *, door: str | None = None, tool_kind: str | None = None
) -> Iterator[None]:
    """Bind caller ``door`` and ``tool_kind`` for nested websearch telemetry."""

    door_token = _TELEMETRY_DOOR.set(door) if door is not None else None
    kind_token = _TELEMETRY_TOOL_KIND.set(tool_kind) if tool_kind is not None else None
    try:
        yield
    finally:
        if kind_token is not None:
            _TELEMETRY_TOOL_KIND.reset(kind_token)
        if door_token is not None:
            _TELEMETRY_DOOR.reset(door_token)


@contextmanager
def websearch_rotation_sink(callback: Callable[[int], None]) -> Iterator[None]:
    """Capture intra-provider key rotation counts for the active search route."""

    token = _TELEMETRY_ROTATION_SINK.set(callback)
    try:
        yield
    finally:
        _TELEMETRY_ROTATION_SINK.reset(token)


@dataclass(frozen=True, slots=True)
class WebSearchRoute:
    """Resolved provider attempts and terminal behavior for one search."""

    provider_ids: tuple[str, ...]
    use_legacy_scrape: bool
    disabled: bool = False


def resolve_websearch_proxy() -> str | None:
    """Resolve optional ``WEBSEARCH_PROXY`` using env-first dotenv precedence."""

    raw = _env_or_dotenv(WEBSEARCH_PROXY_ENV)
    cleaned = raw.strip() if isinstance(raw, str) else ""
    return cleaned or None


def build_providers(settings: Settings) -> dict[str, BaseWebSearchProvider]:
    """Build every configured provider (unconfigured ones are skipped)."""

    providers: dict[str, BaseWebSearchProvider] = {}
    for provider_id in WEBSEARCH_CATALOG:
        try:
            providers[provider_id] = build_provider(settings, provider_id)
        except WebSearchConfigError:
            continue
    return providers


def build_provider(settings: Settings, provider_id: str) -> BaseWebSearchProvider:
    """Build one provider or raise :class:`WebSearchConfigError` when unconfigured."""

    descriptor = WEBSEARCH_CATALOG.get(provider_id)
    if descriptor is None:
        raise WebSearchConfigError(
            provider_id, f"unknown web search provider: {provider_id!r}"
        )
    keys = _descriptor_keys(descriptor, settings)
    if descriptor.requires_key and not keys:
        raise WebSearchConfigError(
            provider_id,
            f"{descriptor.credential_env} is not configured "
            f"(set it in your .env to enable {descriptor.display_name})",
        )
    base_url = _descriptor_base_url(descriptor, settings)
    rotation = _resolve_rotation_policy(descriptor, len(keys))
    proxy = resolve_websearch_proxy()
    options = read_websearch_options(provider_id, descriptor)
    adapter_cls = ADAPTER_CLASSES[provider_id]
    return adapter_cls(
        WebSearchProviderConfig(
            api_keys=keys,
            credential_rotation=rotation,
            base_url=base_url,
            proxy=proxy,
            http_timeout=DEFAULT_HTTP_TIMEOUT,
            options=options,
            quota_cooldown_seconds=settings.web_search_quota_cooldown_seconds,
        )
    )


def resolve_provider_id(settings: Settings) -> str | None:
    """Resolve the primary provider; ``off`` and ``disabled`` have no provider."""

    selection = settings.web_search_provider
    if selection in {"off", "disabled"}:
        return None
    if selection != "auto":
        if selection not in WEBSEARCH_CATALOG:
            raise WebSearchConfigError(
                selection,
                f"unknown web search provider: {selection!r}",
            )
        return selection
    for provider_id in WEBSEARCH_CATALOG:
        if provider_id == "ddgs":
            continue
        descriptor = WEBSEARCH_CATALOG[provider_id]
        if not descriptor.auto_select:
            # Manual-only providers (Context7 library docs; DeepSeek paid
            # standalone Search API): chosen by name or as a chain entry only.
            continue
        if _descriptor_is_configured(descriptor, settings):
            return provider_id
    return "ddgs"


def resolve_search_route(settings: Settings) -> WebSearchRoute:
    """Resolve the ordered attempts implied by provider selection and policy.

    ``auto`` fallback policy is deliberately context-aware: automatic provider
    selection cascades across all configured auto-selectable paid providers in
    catalog order, then DDGS, then legacy scrape (``T5-Q6``), while a named
    provider stays strict. Explicit policies override that default. Missing
    configuration is still owned by :func:`build_provider` and must not be
    converted into fallback by callers.
    """

    selection = settings.web_search_provider
    if selection == "disabled":
        return WebSearchRoute((), use_legacy_scrape=False, disabled=True)
    if selection == "off":
        return WebSearchRoute((), use_legacy_scrape=True)

    policy = settings.web_search_fallback_policy
    if policy == "auto":
        policy = "legacy" if selection == "auto" else "none"

    if selection == "auto":
        configured = [
            pid
            for pid, descriptor in WEBSEARCH_CATALOG.items()
            if pid != "ddgs"
            and descriptor.auto_select
            and _descriptor_is_configured(descriptor, settings)
        ]
        if policy == "none":
            provider_ids = [configured[0]] if configured else ["ddgs"]
        else:
            provider_ids = [*configured, "ddgs"]
        return WebSearchRoute(
            tuple(provider_ids),
            use_legacy_scrape=policy == "legacy",
        )

    provider_id = resolve_provider_id(settings)
    if provider_id is None:  # Defensive: handled by the branches above.
        raise WebSearchConfigError(
            selection,
            f"web search provider {selection!r} did not resolve",
        )

    provider_ids = [provider_id]
    if policy in {"ddgs", "legacy"} and provider_id != "ddgs":
        provider_ids.append("ddgs")
    return WebSearchRoute(
        tuple(provider_ids),
        use_legacy_scrape=policy == "legacy",
    )


def active_provider(settings: Settings) -> BaseWebSearchProvider | None:
    """Build the selected provider; None when web search is off or disabled."""

    provider_id = resolve_provider_id(settings)
    if provider_id is None:
        return None
    return build_provider(settings, provider_id)


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Analytics fields for one provider attempt."""

    ts_epoch: float
    ts_iso: str
    provider: str
    key_index: int
    key_label: str
    query: str
    results_count: int
    duration_ms: float
    status: str  # "success" | "error" | "skipped" (chain rows only)
    error_kind: str | None
    error_message: str | None
    cost_usd: float | None
    route_id: str | None = None
    attempt_number: int = 1
    input_payload: Mapping[str, object] | None = None
    output_payload: Mapping[str, object] | None = None
    provider_config: Mapping[str, object] | None = None
    # 7.112.0, written only by the web search chain (``WEB_SEARCH_CHAIN``);
    # ``None`` on every row a search without a chain writes -- "not measured",
    # never a zero (III.8).
    chain_position: int | None = None
    #: ``succeeded`` | ``failed`` | ``skipped``. A skipped entry's row also
    #: carries ``status="skipped"`` so the stats keep counting what they did.
    outcome: str | None = None
    #: The ``FailureKind`` value the chain decided this attempt on.
    failure_kind: str | None = None
    #: The per-key tries behind this attempt, as JSON text.
    tries_json: str | None = None
    #: Logical tool kind (``web_search``, ``web_fetch``, ``search_docs``).
    tool_kind: str = "web_search"
    #: Caller door (``anthropic_stream``, ``tool_loop``, ``rest``, ``mcp``, ``admin_test``).
    door: str | None = None
    #: Upstream HTTP status code observed for this attempt (e.g. 200, 402, 429).
    http_status: int | None = None
    #: True when a successful search returned 0 results.
    empty_results: bool = False


SearchRecorder = Callable[[SearchOutcome], None]


@dataclass(frozen=True, slots=True)
class SearchRouteOutcome:
    """Terminal analytics fields for one logical outbound web-search route."""

    route_id: str
    ts_epoch: float
    ts_iso: str
    query: str
    primary_provider: str
    terminal_provider: str
    provider_path: tuple[str, ...]
    attempt_count: int
    fallback_used: bool
    duration_ms: float
    status: str  # "success" | "error"
    results_count: int
    cost_usd: float | None
    error_kind: str | None
    error_message: str | None
    door: str | None = None
    skipped_count: int = 0
    key_rotations: int = 0
    empty_fallback_count: int = 0


SearchRouteRecorder = Callable[[SearchRouteOutcome], None]


async def search(
    provider: BaseWebSearchProvider,
    query: str,
    *,
    max_results: int = 10,
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
    recorder: SearchRecorder | None = None,
    route_id: str | None = None,
    attempt_number: int = 1,
    route_context: Mapping[str, object] | None = None,
    key_charge: KeyChargeJudge | None = None,
    request_options: WebSearchRequestOptions | None = None,
    mark_empty_outcome: bool = False,
    door: str | None = None,
    tool_kind: str | None = None,
) -> WebSearchResponse:
    """Run ``provider.search`` and optionally record the outcome via ``recorder``.

    ``key_charge`` is handed to :meth:`BaseWebSearchProvider.search` (7.112.1).
    """

    ts_epoch = time.time()
    started = time.perf_counter()
    resolved_door = door if door is not None else current_telemetry_door()
    resolved_tool_kind = (
        tool_kind
        if tool_kind is not None
        else (current_telemetry_tool_kind() or "web_search")
    )
    input_payload = _search_input_payload(
        query,
        max_results=max_results,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
        request_options=request_options,
    )
    provider_config = _provider_config_snapshot(provider, route_context)
    tries: list[dict[str, object]] = []
    supports_tries = isinstance(provider, BaseWebSearchProvider)
    try:
        if request_options is None and not supports_tries:
            response = await provider.search(
                query,
                max_results=max_results,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                key_charge=key_charge,
            )
        elif request_options is None:
            response = await provider.search(
                query,
                max_results=max_results,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                key_charge=key_charge,
                tries_out=tries,
            )
        elif not supports_tries:
            response = await provider.search(
                query,
                max_results=max_results,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                key_charge=key_charge,
                request_options=request_options,
            )
        else:
            response = await provider.search(
                query,
                max_results=max_results,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                key_charge=key_charge,
                request_options=request_options,
                tries_out=tries,
            )
    except Exception as error:
        key_index = (
            error.key_index
            if isinstance(error, WebSearchError) and error.key_index is not None
            else 0
        )
        tries_json = (
            json.dumps(tries, separators=(",", ":"), default=str)
            if (len(tries) > 1 or any(not bool(row.get("ok")) for row in tries))
            else None
        )
        rotations = sum(1 for row in tries if bool(row.get("rotated")))
        if rotations:
            sink = _TELEMETRY_ROTATION_SINK.get()
            if sink is not None:
                sink(rotations)
        http_status: int | None = (
            error.status_code if isinstance(error, WebSearchError) else None
        )
        if http_status is None and tries:
            last_status = tries[-1].get("status")
            if isinstance(last_status, int) and not isinstance(last_status, bool):
                http_status = last_status
        _emit(
            recorder,
            SearchOutcome(
                ts_epoch=ts_epoch,
                ts_iso=_iso(ts_epoch),
                provider=provider.provider_id,
                key_index=key_index,
                key_label=provider.key_label(key_index),
                query=query[:_QUERY_LOG_CHARS],
                results_count=0,
                duration_ms=_elapsed_ms(started),
                status="error",
                error_kind=(
                    error.kind if isinstance(error, WebSearchError) else "internal"
                ),
                error_message=str(error)[:_ERROR_MESSAGE_LOG_CHARS],
                cost_usd=None,
                route_id=route_id,
                attempt_number=attempt_number,
                input_payload=input_payload,
                output_payload=_error_output_payload(error),
                provider_config=provider_config,
                tries_json=tries_json,
                tool_kind=resolved_tool_kind,
                door=resolved_door,
                http_status=http_status,
                empty_results=False,
            ),
        )
        raise
    tries_json = (
        json.dumps(tries, separators=(",", ":"), default=str)
        if (len(tries) > 1 or any(not bool(row.get("ok")) for row in tries))
        else None
    )
    rotations = sum(1 for row in tries if bool(row.get("rotated")))
    if rotations:
        sink = _TELEMETRY_ROTATION_SINK.get()
        if sink is not None:
            sink(rotations)
    empty_outcome = "empty" if (mark_empty_outcome and not response.results) else None
    _emit(
        recorder,
        SearchOutcome(
            ts_epoch=ts_epoch,
            ts_iso=_iso(ts_epoch),
            provider=provider.provider_id,
            key_index=response.key_index,
            key_label=provider.key_label(response.key_index),
            query=query[:_QUERY_LOG_CHARS],
            results_count=len(response.results),
            duration_ms=_elapsed_ms(started),
            status="success",
            error_kind=None,
            error_message=None,
            cost_usd=response.cost_usd,
            route_id=route_id,
            attempt_number=attempt_number,
            input_payload=input_payload,
            output_payload=_response_output_payload(response),
            provider_config=provider_config,
            outcome=empty_outcome,
            tries_json=tries_json,
            tool_kind=resolved_tool_kind,
            door=resolved_door,
            http_status=response.http_status,
            empty_results=(len(response.results) == 0),
        ),
    )
    return response


async def search_with_logging(
    provider: BaseWebSearchProvider,
    query: str,
    *,
    max_results: int = 10,
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
    recorder: SearchRecorder | None = None,
    route_id: str | None = None,
    attempt_number: int = 1,
    route_context: Mapping[str, object] | None = None,
    key_charge: KeyChargeJudge | None = None,
    request_options: WebSearchRequestOptions | None = None,
    mark_empty_outcome: bool = False,
    door: str | None = None,
    tool_kind: str | None = None,
) -> WebSearchResponse:
    """Search with analytics recording; defaults to the shared attempt recorder."""

    selected_recorder = recorder if recorder is not None else _default_recorder()
    if (
        request_options is None
        and not mark_empty_outcome
        and door is None
        and tool_kind is None
    ):
        return await search(
            provider,
            query,
            max_results=max_results,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            recorder=selected_recorder,
            route_id=route_id,
            attempt_number=attempt_number,
            route_context=route_context,
            key_charge=key_charge,
        )
    return await search(
        provider,
        query,
        max_results=max_results,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
        recorder=selected_recorder,
        route_id=route_id,
        attempt_number=attempt_number,
        route_context=route_context,
        key_charge=key_charge,
        request_options=request_options,
        mark_empty_outcome=mark_empty_outcome,
        door=door,
        tool_kind=tool_kind,
    )


def emit_search_outcome(
    outcome: SearchOutcome, recorder: SearchRecorder | None = None
) -> None:
    """Emit an attempt outcome without coupling a caller to analytics storage."""

    _emit(recorder if recorder is not None else _default_recorder(), outcome)


def emit_route_outcome(
    outcome: SearchRouteOutcome,
    recorder: SearchRouteRecorder | None = None,
) -> None:
    """Emit one logical route outcome without impacting the search result."""

    selected = recorder if recorder is not None else _default_route_recorder()
    if selected is None:
        return
    try:
        selected(outcome)
    except Exception:
        logger.exception(
            "websearch route recorder failed for route {}", outcome.route_id
        )


def _default_recorder() -> SearchRecorder | None:
    """Resolve ``websearch.analytics.record_search`` without a static cycle."""

    try:
        module = importlib.import_module(f"{__package__}.analytics")
    except ImportError:
        return None
    record_search = getattr(module, "record_search", None)
    return record_search if callable(record_search) else None


def _default_route_recorder() -> SearchRouteRecorder | None:
    try:
        module = importlib.import_module(f"{__package__}.analytics")
    except ImportError:
        return None
    record_route = getattr(module, "record_search_route", None)
    return record_route if callable(record_route) else None


def _emit(recorder: SearchRecorder | None, outcome: SearchOutcome) -> None:
    if recorder is None:
        return
    try:
        recorder(outcome)
    except Exception:
        logger.exception("websearch recorder failed for provider {}", outcome.provider)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _iso(ts_epoch: float) -> str:
    return datetime.fromtimestamp(ts_epoch, tz=UTC).isoformat()


def _search_input_payload(
    query: str,
    *,
    max_results: int,
    allowed_domains: tuple[str, ...],
    blocked_domains: tuple[str, ...],
    request_options: WebSearchRequestOptions | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "query": query,
        "max_results": max_results,
        "allowed_domains": list(allowed_domains),
        "blocked_domains": list(blocked_domains),
    }
    if request_options is not None:
        overrides = request_options.non_empty_dict()
        if overrides:
            payload["request_options"] = overrides
    return payload


def _result_output_dict(item: WebSearchResultItem) -> dict[str, object]:
    row: dict[str, object] = {
        "title": item.title,
        "url": item.url,
        "snippet": item.snippet,
        "content": item.content,
        "published": item.published,
    }
    if item.score is not None:
        row["score"] = item.score
    if item.author is not None:
        row["author"] = item.author
    if item.favicon is not None:
        row["favicon"] = item.favicon
    if item.site_name is not None:
        row["site_name"] = item.site_name
    if item.highlights is not None:
        row["highlights"] = list(item.highlights)
    if item.highlight_scores is not None:
        row["highlight_scores"] = list(item.highlight_scores)
    return row


def _response_output_payload(response: WebSearchResponse) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider": response.provider,
        "query": response.query,
        "answer": response.answer,
        "results": [_result_output_dict(item) for item in response.results],
        "result_count": len(response.results),
        "key_index": response.key_index,
        "cost_usd": response.cost_usd,
    }
    if response.request_id is not None:
        payload["request_id"] = response.request_id
    if response.provider_response_time_ms is not None:
        payload["provider_response_time_ms"] = response.provider_response_time_ms
    if response.usage_credits is not None:
        payload["usage_credits"] = response.usage_credits
    if response.images is not None:
        payload["images"] = list(response.images)
    return payload


def _error_output_payload(error: BaseException) -> dict[str, object]:
    return {
        "error": {
            "kind": error.kind if isinstance(error, WebSearchError) else "internal",
            "type": type(error).__name__,
            "message": str(error)[:_ERROR_MESSAGE_LOG_CHARS],
        }
    }


def _provider_config_snapshot(
    provider: BaseWebSearchProvider,
    route_context: Mapping[str, object] | None,
) -> dict[str, object]:
    config = provider.config
    snapshot: dict[str, object] = {
        "provider_id": provider.provider_id,
        "credential_rotation": config.credential_rotation,
        "credential_count": len(config.api_keys),
        "base_url": _safe_config_url(config.base_url),
        "proxy": _safe_config_url(config.proxy),
        "http_timeout_seconds": config.http_timeout,
        "supports_domain_filters": provider.SUPPORTS_DOMAINS,
        "options": dict(config.options),
    }
    if route_context:
        snapshot["route"] = dict(route_context)
    return snapshot


def _safe_config_url(value: str | None) -> str | None:
    """Keep endpoint identity while removing credentials, query, and fragment."""

    if not value:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return "[configured]"
    if not parsed.scheme or not host:
        return "[configured]"
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


def _descriptor_keys(
    descriptor: WebSearchDescriptor, settings: Settings
) -> tuple[str, ...]:
    if descriptor.settings_attr is None:
        return ()
    raw = getattr(settings, descriptor.settings_attr)
    keys = parse_credential_keys(raw if isinstance(raw, str) else None)
    if (
        not keys
        and descriptor.provider_id == "deepseek"
        and option_enabled(_env_or_dotenv("DEEPSEEK_SEARCH_USE_INFERENCE_KEY"))
    ):
        inference_raw = getattr(settings, "deepseek_api_key", None)
        keys = parse_credential_keys(
            inference_raw if isinstance(inference_raw, str) else None
        )
    return keys


def _descriptor_base_url(
    descriptor: WebSearchDescriptor, settings: Settings
) -> str | None:
    if descriptor.base_url_attr is None:
        return descriptor.default_base_url
    raw = getattr(settings, descriptor.base_url_attr)
    base_url = raw.strip() if isinstance(raw, str) else ""
    if not base_url:
        if descriptor.provider_id == "searxng":
            raise WebSearchConfigError(
                descriptor.provider_id,
                "SEARXNG_BASE_URL is required for the searxng provider "
                "(self-hosted instance with format=json enabled)",
            )
        return descriptor.default_base_url
    return base_url


def _descriptor_is_configured(
    descriptor: WebSearchDescriptor, settings: Settings
) -> bool:
    try:
        if descriptor.requires_key and not _descriptor_keys(descriptor, settings):
            return False
        _descriptor_base_url(descriptor, settings)
    except WebSearchConfigError:
        return False
    return True


def _resolve_rotation_policy(descriptor: WebSearchDescriptor, key_count: int) -> str:
    raw = (
        _env_or_dotenv(f"{descriptor.credential_env}{ROTATION_ENV_SUFFIX}")
        if descriptor.credential_env
        else None
    )
    if not raw:
        return default_rotation_policy(key_count)
    value = raw.strip().lower()
    if value == ON_ERROR_ROTATION_ALIAS:
        # The model side's spelling of ``failover``
        # (``providers/credential_rotation.py``), which docs/WEB-SEARCH.md
        # has always promised search keys accept too (7.112.0, user Q12).
        value = "failover"
    if value not in ROTATION_POLICIES:
        logger.warning(
            "Invalid {} value {!r}; falling back to default rotation policy",
            f"{descriptor.credential_env}{ROTATION_ENV_SUFFIX}",
            raw,
        )
        return default_rotation_policy(key_count)
    return value


def _env_or_dotenv(key: str) -> str | None:
    """Process env wins; otherwise the last configured dotenv value."""

    if key in os.environ:
        return os.environ[key]
    return env_file_override(Settings.model_config, key)
