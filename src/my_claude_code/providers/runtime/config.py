"""Provider configuration construction from neutral catalog metadata."""

import os

from loguru import logger

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.config.credentials import parse_credential_keys
from my_claude_code.config.env_files import env_file_override
from my_claude_code.config.provider_catalog import ProviderDescriptor
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.settings import Settings, parse_lockout_tiers
from my_claude_code.providers.base import ProviderConfig

CREDENTIAL_ROTATION_POLICIES = frozenset(
    {"single", "round_robin", "least_used", "failover", "on_error"}
)
DEFAULT_CREDENTIAL_ROTATION = "single"


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
    proxy = string_setting(settings, descriptor.proxy_attr)
    return ProviderConfig(
        api_key=api_keys[0] if api_keys else credential,
        base_url=resolved_base_url,
        rate_limit=settings.provider_rate_limit,
        rate_window=settings.provider_rate_window,
        max_concurrency=settings.provider_max_concurrency,
        http_read_timeout=settings.http_read_timeout,
        http_write_timeout=settings.http_write_timeout,
        http_connect_timeout=settings.http_connect_timeout,
        proxy=proxy,
        log_raw_sse_events=settings.log_raw_sse_events,
        log_api_error_tracebacks=settings.log_api_error_tracebacks,
        api_keys=api_keys,
        credential_rotation=rotation,
        retry_attempts=settings.provider_retry_attempts,
        early_retry_attempts=settings.stream_early_retry_attempts,
        midstream_recovery_attempts=settings.stream_midstream_recovery_attempts,
        commit_holdback_seconds=settings.stream_commit_holdback_seconds,
        fallback_on_reasoning_only=settings.fallback_on_reasoning_only,
        rate_limit_cooldown_seconds=settings.rate_limit_cooldown_seconds,
        retry_backoff_base_seconds=settings.provider_retry_backoff_base_seconds,
        retry_backoff_max_seconds=settings.provider_retry_backoff_max_seconds,
        retry_backoff_jitter_seconds=settings.provider_retry_backoff_jitter_seconds,
        lockout_tiers=parse_lockout_tiers(settings.credential_lockout_tiers),
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
    return ProviderConfig(
        api_key=entry.api_keys[0] if entry.api_keys else "",
        base_url=entry.base_url,
        rate_limit=settings.provider_rate_limit,
        rate_window=settings.provider_rate_window,
        max_concurrency=settings.provider_max_concurrency,
        http_read_timeout=settings.http_read_timeout,
        http_write_timeout=settings.http_write_timeout,
        http_connect_timeout=settings.http_connect_timeout,
        proxy=entry.proxy or "",
        log_raw_sse_events=settings.log_raw_sse_events,
        log_api_error_tracebacks=settings.log_api_error_tracebacks,
        api_keys=entry.api_keys,
        retry_attempts=settings.provider_retry_attempts,
        early_retry_attempts=settings.stream_early_retry_attempts,
        midstream_recovery_attempts=settings.stream_midstream_recovery_attempts,
        commit_holdback_seconds=settings.stream_commit_holdback_seconds,
        fallback_on_reasoning_only=settings.fallback_on_reasoning_only,
        rate_limit_cooldown_seconds=settings.rate_limit_cooldown_seconds,
        retry_backoff_base_seconds=settings.provider_retry_backoff_base_seconds,
        retry_backoff_max_seconds=settings.provider_retry_backoff_max_seconds,
        retry_backoff_jitter_seconds=settings.provider_retry_backoff_jitter_seconds,
        lockout_tiers=parse_lockout_tiers(settings.credential_lockout_tiers),
        credential_rotation=rotation,
    )
