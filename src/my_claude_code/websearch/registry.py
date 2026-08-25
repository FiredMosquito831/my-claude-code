"""Web search provider registry: build from settings, resolve the active one, search.

Analytics seam: :func:`search` accepts an optional attempt recorder;
:func:`search_with_logging` defaults it to ``websearch.analytics.record_search``.
Route owners may emit one correlated :class:`SearchRouteOutcome` through
:func:`emit_route_outcome`.
"""

import importlib
import os
import time
from collections.abc import Callable, Mapping
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
from my_claude_code.core.websearch.models import WebSearchResponse

from .adapters import ADAPTER_CLASSES
from .base import BaseWebSearchProvider, WebSearchProviderConfig
from .errors import WebSearchConfigError, WebSearchError
from .options import read_websearch_options
from .rotation import (
    ROTATION_POLICIES,
    default_rotation_policy,
)

DEFAULT_HTTP_TIMEOUT = 20.0
ROTATION_ENV_SUFFIX = "_ROTATION"
WEBSEARCH_PROXY_ENV = "WEBSEARCH_PROXY"
_QUERY_LOG_CHARS = 256
_ERROR_MESSAGE_LOG_CHARS = 500


@dataclass(frozen=True, slots=True)
class WebSearchRoute:
    """Resolved provider attempts and terminal behavior for one search."""

    provider_ids: tuple[str, ...]
    use_legacy_scrape: bool
    disabled: bool = False


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
    proxy = _env_or_dotenv(WEBSEARCH_PROXY_ENV)
    options = read_websearch_options(provider_id, descriptor)
    adapter_cls = ADAPTER_CLASSES[provider_id]
    return adapter_cls(
        WebSearchProviderConfig(
            api_keys=keys,
            credential_rotation=rotation,
            base_url=base_url,
            proxy=proxy or None,
            http_timeout=DEFAULT_HTTP_TIMEOUT,
            options=options,
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
        if _descriptor_is_configured(descriptor, settings):
            return provider_id
    return "ddgs"


def resolve_search_route(settings: Settings) -> WebSearchRoute:
    """Resolve the ordered attempts implied by provider selection and policy.

    ``auto`` fallback policy is deliberately context-aware: automatic provider
    selection retains the historical provider -> DDGS -> legacy resilience,
    while a named provider is strict. Explicit policies override that default.
    Missing configuration is still owned by :func:`build_provider` and must not
    be converted into fallback by callers.
    """

    selection = settings.web_search_provider
    if selection == "disabled":
        return WebSearchRoute((), use_legacy_scrape=False, disabled=True)
    if selection == "off":
        return WebSearchRoute((), use_legacy_scrape=True)

    provider_id = resolve_provider_id(settings)
    if provider_id is None:  # Defensive: handled by the branches above.
        raise WebSearchConfigError(
            selection,
            f"web search provider {selection!r} did not resolve",
        )

    policy = settings.web_search_fallback_policy
    if policy == "auto":
        policy = "legacy" if selection == "auto" else "none"

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
    status: str  # "success" | "error"
    error_kind: str | None
    error_message: str | None
    cost_usd: float | None
    route_id: str | None = None
    attempt_number: int = 1
    input_payload: Mapping[str, object] | None = None
    output_payload: Mapping[str, object] | None = None
    provider_config: Mapping[str, object] | None = None


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
) -> WebSearchResponse:
    """Run ``provider.search`` and optionally record the outcome via ``recorder``."""

    ts_epoch = time.time()
    started = time.perf_counter()
    input_payload = _search_input_payload(
        query,
        max_results=max_results,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
    )
    provider_config = _provider_config_snapshot(provider, route_context)
    try:
        response = await provider.search(
            query,
            max_results=max_results,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )
    except Exception as error:
        key_index = (
            error.key_index
            if isinstance(error, WebSearchError) and error.key_index is not None
            else 0
        )
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
            ),
        )
        raise
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
) -> WebSearchResponse:
    """Search with analytics recording; defaults to the shared attempt recorder."""

    return await search(
        provider,
        query,
        max_results=max_results,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
        recorder=recorder if recorder is not None else _default_recorder(),
        route_id=route_id,
        attempt_number=attempt_number,
        route_context=route_context,
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
) -> dict[str, object]:
    return {
        "query": query,
        "max_results": max_results,
        "allowed_domains": list(allowed_domains),
        "blocked_domains": list(blocked_domains),
    }


def _response_output_payload(response: WebSearchResponse) -> dict[str, object]:
    return {
        "provider": response.provider,
        "query": response.query,
        "answer": response.answer,
        "results": [
            {
                "title": item.title,
                "url": item.url,
                "snippet": item.snippet,
                "content": item.content,
                "published": item.published,
            }
            for item in response.results
        ],
        "result_count": len(response.results),
        "key_index": response.key_index,
        "cost_usd": response.cost_usd,
    }


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
    return parse_credential_keys(raw if isinstance(raw, str) else None)


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
