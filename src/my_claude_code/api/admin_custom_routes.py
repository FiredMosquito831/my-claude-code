"""Admin CRUD for dynamic custom OpenAI-compatible providers.

Every route here codes against ``config.provider_registry.ProviderRegistry``
directly. An earlier version of this module carried its own duck-typed mirror
of that contract because the registry landed in a parallel worktree; the two
drifted, and the mismatch made *every* create return HTTP 500 while the tests
stayed green against a fake shaped like the guess.
"""

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from my_claude_code.config.provider_registry import (
    CustomProviderEntry,
    ProviderRegistry,
    custom_provider_id,
    get_provider_registry,
)

from .admin_routes import _mask_credential_key, require_loopback_admin
from .dependencies import get_services
from .ports import ApiServices

router = APIRouter()

ROTATION_POLICIES = ("single", "round_robin", "least_used", "failover")
DEFAULT_ROTATION = "failover"
HOT_RELOAD_REASON = "custom_provider_change"


def get_custom_provider_registry() -> ProviderRegistry:
    """Return the process-wide custom provider registry singleton."""
    return get_provider_registry()


class CustomProviderCreatePayload(BaseModel):
    """Payload for registering one custom provider."""

    display_name: str
    base_url: str
    api_key: str
    credential_rotation: str = DEFAULT_ROTATION
    proxy: str | None = None


class CustomProviderUpdatePayload(BaseModel):
    """Partial update for one custom provider."""

    display_name: str | None = None
    base_url: str | None = None
    proxy: str | None = None
    enabled: bool | None = None
    credential_rotation: str | None = None


class CustomProviderKeyPayload(BaseModel):
    """Single API key appended to one custom provider."""

    api_key: str


def _validate_display_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Display name is empty")
    return name


def _validate_base_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=422,
            detail="base_url must be an http(s) URL",
        )
    return url


def _validate_api_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise HTTPException(status_code=422, detail="API key is empty")
    if "," in key:
        raise HTTPException(
            status_code=422,
            detail="Paste a single API key without commas",
        )
    return key


def _validate_rotation(value: str) -> str:
    rotation = value.strip()
    if rotation not in ROTATION_POLICIES:
        raise HTTPException(
            status_code=422,
            detail=(
                "credential_rotation must be one of: " + ", ".join(ROTATION_POLICIES)
            ),
        )
    return rotation


def _normalize_proxy(value: str | None) -> str | None:
    if value is None:
        return None
    proxy = value.strip()
    if not proxy:
        return None
    parsed = urlsplit(proxy)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
        raise HTTPException(
            status_code=422,
            detail="proxy must be an http(s) or socks5 URL",
        )
    return proxy


def _provider_id_for(display_name: str) -> str:
    """Return the id this display name claims, rejecting names that slug empty."""
    provider_id = custom_provider_id(display_name)
    if not provider_id:
        raise HTTPException(
            status_code=422,
            detail="Display name must contain at least one letter or digit",
        )
    return provider_id


def _registry_get_or_404(
    registry: ProviderRegistry, provider_id: str
) -> CustomProviderEntry:
    entry = registry.get(provider_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown custom provider")
    return entry


def _serialize_entry(
    entry: CustomProviderEntry, cached_models: dict[str, frozenset[str]]
) -> dict[str, Any]:
    if not entry.enabled:
        status = "disabled"
    elif entry.api_keys:
        status = "configured"
    else:
        status = "missing_key"
    models = sorted(cached_models.get(entry.provider_id, ()))
    return {
        "provider_id": entry.provider_id,
        "display_name": entry.display_name,
        "base_url": entry.base_url,
        "key_count": len(entry.api_keys),
        "masked_keys": [_mask_credential_key(key) for key in entry.api_keys],
        "credential_rotation": entry.credential_rotation,
        "proxy": entry.proxy,
        "enabled": entry.enabled,
        "model_count": len(models),
        "status": status,
        "models": models,
        "added_at": entry.added_at,
    }


def _discovery_target(entry: CustomProviderEntry) -> str | None:
    """Return the id worth probing after a mutation, or ``None``.

    A disabled or keyless entry has no resolvable provider, so asking for its
    ``/models`` would only manufacture an error to show the user.
    """
    if entry.enabled and entry.api_keys:
        return entry.provider_id
    return None


async def _reload_provider_runtime(
    services: ApiServices, *, refresh_provider_id: str | None = None
) -> dict[str, Any]:
    """Republish the provider generation after a registry mutation.

    Passing ``refresh_provider_id`` swaps the generation's blanket background
    sweep for one awaited, provider-scoped discovery -- the whole point being
    that a brand-new upstream is queried exactly once, and that its answer is
    what the response reports.
    """
    return await services.admin.reload_providers(
        reason=HOT_RELOAD_REASON, refresh_provider_id=refresh_provider_id
    )


def _attach_discovery(
    result: dict[str, Any], discovery: Mapping[str, Any]
) -> dict[str, Any]:
    """Fold one discovery outcome into a serialized entry."""
    if not discovery:
        return result
    result["discovery"] = dict(discovery)
    if not discovery.get("ok", True):
        # Kept alongside the richer block: the dashboard has surfaced
        # ``test_error`` since the card existed.
        result["test_error"] = str(discovery.get("error_type", "discovery_failed"))
    return result


@router.get("/admin/api/custom-providers")
async def list_custom_providers(
    request: Request,
    registry: ProviderRegistry = Depends(get_custom_provider_registry),
    services: ApiServices = Depends(get_services),
):
    """List every registered custom provider with cached model metadata."""
    require_loopback_admin(request)
    cached = services.admin.cached_model_ids()
    return {
        "providers": [
            _serialize_entry(entry, cached) for entry in registry.list_custom()
        ]
    }


@router.post("/admin/api/custom-providers")
async def create_custom_provider(
    payload: CustomProviderCreatePayload,
    request: Request,
    registry: ProviderRegistry = Depends(get_custom_provider_registry),
    services: ApiServices = Depends(get_services),
):
    """Register one custom provider, hot reload, and detect its models live."""
    require_loopback_admin(request)
    display_name = _validate_display_name(payload.display_name)
    base_url = _validate_base_url(payload.base_url)
    api_key = _validate_api_key(payload.api_key)
    rotation = _validate_rotation(payload.credential_rotation)
    proxy = _normalize_proxy(payload.proxy)
    # The registry would happily allocate ``custom_acme_2`` for a second
    # provider named "Acme"; two identically-named cards are indistinguishable
    # in the dashboard, so the name is claimed exclusively here instead.
    claimed_id = _provider_id_for(display_name)
    if registry.get(claimed_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Custom provider '{claimed_id}' already exists",
        )
    try:
        entry = registry.add(
            display_name=display_name,
            base_url=base_url,
            api_keys=(api_key,),
            credential_rotation=rotation,
            proxy=proxy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    provider_id = entry.provider_id
    discovery = await _reload_provider_runtime(
        services, refresh_provider_id=_discovery_target(entry)
    )
    stored = registry.get(provider_id) or entry
    # The count and the model list both come from the catalogue the discovery
    # just filled. They used to come from a *second*, independent probe, which
    # is how a card could truthfully advertise 44 models while /v1/models, the
    # Models page and every picker held none.
    result = _serialize_entry(stored, services.admin.cached_model_ids())
    return _attach_discovery(result, discovery)


@router.patch("/admin/api/custom-providers/{provider_id}")
async def update_custom_provider(
    provider_id: str,
    payload: CustomProviderUpdatePayload,
    request: Request,
    registry: ProviderRegistry = Depends(get_custom_provider_registry),
    services: ApiServices = Depends(get_services),
):
    """Apply a partial update to one custom provider and hot reload."""
    require_loopback_admin(request)
    _registry_get_or_404(registry, provider_id)

    changes: dict[str, Any] = {}
    if payload.display_name is not None:
        changes["display_name"] = _validate_display_name(payload.display_name)
    if payload.base_url is not None:
        changes["base_url"] = _validate_base_url(payload.base_url)
    if payload.credential_rotation is not None:
        changes["credential_rotation"] = _validate_rotation(payload.credential_rotation)
    if "proxy" in payload.model_fields_set:
        changes["proxy"] = _normalize_proxy(payload.proxy)
    if payload.enabled is not None:
        changes["enabled"] = payload.enabled
    if not changes:
        raise HTTPException(status_code=422, detail="No updatable fields provided")

    try:
        registry.update(provider_id, **changes)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown custom provider") from exc
    stored = _registry_get_or_404(registry, provider_id)
    discovery = await _reload_provider_runtime(
        services, refresh_provider_id=_discovery_target(stored)
    )
    stored = _registry_get_or_404(registry, provider_id)
    result = _serialize_entry(stored, services.admin.cached_model_ids())
    return _attach_discovery(result, discovery)


@router.post("/admin/api/custom-providers/{provider_id}/keys")
async def add_custom_provider_key(
    provider_id: str,
    payload: CustomProviderKeyPayload,
    request: Request,
    registry: ProviderRegistry = Depends(get_custom_provider_registry),
    services: ApiServices = Depends(get_services),
):
    """Append one API key to a custom provider and hot reload."""
    require_loopback_admin(request)
    entry = _registry_get_or_404(registry, provider_id)
    api_key = _validate_api_key(payload.api_key)
    keys: list[str] = list(entry.api_keys)
    if api_key in keys:
        raise HTTPException(status_code=409, detail="Key is already configured")
    keys.append(api_key)
    try:
        registry.update(provider_id, api_keys=tuple(keys))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown custom provider") from exc
    stored = _registry_get_or_404(registry, provider_id)
    discovery = await _reload_provider_runtime(
        services, refresh_provider_id=_discovery_target(stored)
    )
    stored = _registry_get_or_404(registry, provider_id)
    result = _attach_discovery(
        _serialize_entry(stored, services.admin.cached_model_ids()), discovery
    )
    result["added"] = _mask_credential_key(api_key)
    return result


@router.delete("/admin/api/custom-providers/{provider_id}/keys/{index}")
async def delete_custom_provider_key(
    provider_id: str,
    index: int,
    request: Request,
    registry: ProviderRegistry = Depends(get_custom_provider_registry),
    services: ApiServices = Depends(get_services),
):
    """Remove one API key by index and hot reload (last key keeps the entry)."""
    require_loopback_admin(request)
    entry = _registry_get_or_404(registry, provider_id)
    keys: list[str] = list(entry.api_keys)
    if index < 0 or index >= len(keys):
        raise HTTPException(status_code=404, detail="Key index out of range")
    removed = keys.pop(index)
    try:
        registry.update(provider_id, api_keys=tuple(keys))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown custom provider") from exc
    stored = _registry_get_or_404(registry, provider_id)
    discovery = await _reload_provider_runtime(
        services, refresh_provider_id=_discovery_target(stored)
    )
    stored = _registry_get_or_404(registry, provider_id)
    result = _attach_discovery(
        _serialize_entry(stored, services.admin.cached_model_ids()), discovery
    )
    result["removed"] = _mask_credential_key(removed)
    return result


@router.delete("/admin/api/custom-providers/{provider_id}")
async def delete_custom_provider(
    provider_id: str,
    request: Request,
    registry: ProviderRegistry = Depends(get_custom_provider_registry),
    services: ApiServices = Depends(get_services),
):
    """Remove one custom provider and hot reload."""
    require_loopback_admin(request)
    _registry_get_or_404(registry, provider_id)
    try:
        registry.remove(provider_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown custom provider") from exc
    await _reload_provider_runtime(services)
    return {"applied": True, "provider_id": provider_id, "removed": True}
