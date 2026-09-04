"""Local admin UI routes and APIs."""

import asyncio
import ipaddress
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from my_claude_code.api.docs_content import available_documents
from my_claude_code.api.docs_render import render_document
from my_claude_code.api.model_admin import (
    MODEL_SCOPE,
    MODEL_VISIBILITY_BULK_LIMIT,
    PROVIDER_SCOPE,
    REASONING_MEASUREMENT_DAYS,
    GlobMigration,
    apply_visibility_bulk,
    apply_visibility_toggle,
    build_models_page_payload,
    bulk_result_rows,
    hiding_pattern,
    migrate_exact_patterns_to_globs,
    render_patterns,
    visibility_payload,
    with_override_row,
)
from my_claude_code.api.model_catalog import settings_model_visibility
from my_claude_code.api.optimization_handlers import OPTIMIZATION_RULE_SPECS
from my_claude_code.application.model_metadata import ProviderModelRefreshResult
from my_claude_code.application.release_updates import (
    get_release_status,
    perform_upgrade,
)
from my_claude_code.config.admin.manifest import FIELD_BY_KEY
from my_claude_code.config.admin.persistence import validate_updates
from my_claude_code.config.admin.sources import is_locked_source
from my_claude_code.config.admin.values import (
    PAUSE_KEY_FOR_ROUTE,
    load_config_response,
    load_value_state,
)
from my_claude_code.config.claude_discovery import (
    DiscoveredSettings,
    discover_settings_files,
    native_origin,
)
from my_claude_code.config.claude_settings import (
    ClaudeSettingsError,
    ClaudeSettingsStatus,
    apply_proxy_env,
    clear_proxy_env,
    read_status,
)
from my_claude_code.config.constants import (
    ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
    CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
)
from my_claude_code.config.credentials import (
    mask_key_label as mask_credential_label,
)
from my_claude_code.config.credentials import parse_credential_keys
from my_claude_code.config.desktop import (
    SERVER_MODES,
    WINDOW_PREFERENCES,
    DesktopState,
    load_desktop_state,
    resolve_auto_window,
    save_desktop_state,
)
from my_claude_code.config.harnesses import (
    harness_display_name,
    rtk_capable_ids,
)
from my_claude_code.config.model_overrides import (
    current_model_overrides,
    save_model_overrides,
)
from my_claude_code.config.model_refs import (
    configured_chat_model_refs,
    format_model_ref_list,
    parse_model_ref_list,
)
from my_claude_code.config.onboarding import (
    OnboardingState,
    OnboardingStep,
)
from my_claude_code.config.onboarding import (
    build_state as build_onboarding_state,
)
from my_claude_code.config.onboarding import (
    load_persisted as load_onboarding_persisted,
)
from my_claude_code.config.onboarding import (
    save_persisted as save_onboarding_persisted,
)
from my_claude_code.config.paths import (
    claude_settings_path,
    config_dir_path,
    config_dir_resolution,
    legacy_config_dir_path,
    new_config_dir_path,
    retired_config_dir_path,
)
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.rtk import (
    RtkError,
    RtkState,
    apply_rtk_state,
    load_rtk_state,
    read_rtk_gain,
    rtk_status,
    save_rtk_state,
)
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import Settings
from my_claude_code.config.websearch_catalog import (
    WEBSEARCH_CATALOG,
    WebSearchDescriptor,
)
from my_claude_code.core.client_fingerprint import (
    NON_REGISTRY_HARNESS_LABELS,
)
from my_claude_code.core.model_visibility import ModelVisibility
from my_claude_code.core.optimization_discovery import (
    DEFAULT_SCAN_ROW_LIMIT,
    MAX_SCAN_ROW_LIMIT,
    discover_families,
)
from my_claude_code.core.request_log import (
    LOCAL_FILTER_VALUES,
    RequestLogStore,
    store_from_settings,
)
from my_claude_code.providers.anthropic_oauth.constants import (
    INFERENCE_SCOPE as ANTHROPIC_INFERENCE_SCOPE,
)
from my_claude_code.providers.anthropic_oauth.credentials import (
    AnthropicOAuthRefreshError,
    AnthropicOAuthUnavailableError,
    OAuthTokens,
    claude_credentials_path,
    load_claude_code_tokens,
    load_managed_tokens,
    load_tokens,
)
from my_claude_code.providers.anthropic_oauth.credentials import (
    quarantine_managed_store as quarantine_anthropic_oauth_store,
)
from my_claude_code.providers.anthropic_oauth.credentials import (
    refresh_tokens as refresh_anthropic_oauth_tokens,
)
from my_claude_code.providers.anthropic_oauth.credentials import (
    store_tokens as store_anthropic_oauth_tokens,
)
from my_claude_code.providers.anthropic_oauth.loopback import (
    AnthropicOAuthLoopbackUnavailableError,
    loopback_login_status,
    start_loopback_login,
)
from my_claude_code.providers.anthropic_oauth.oauth_login import (
    AnthropicOAuthLoginError,
    build_authorize_url,
    generate_pkce_verifier,
    split_pasted_code,
)
from my_claude_code.providers.anthropic_oauth.oauth_login import (
    exchange_code as exchange_anthropic_oauth_code,
)
from my_claude_code.providers.anthropic_oauth.rate_limit_headers import (
    OBSERVER as ANTHROPIC_RATE_LIMIT_OBSERVER,
)
from my_claude_code.providers.chatgpt_oauth.browser_login import (
    ChatGPTOAuthBrowserUnavailableError,
    browser_login_status,
    start_browser_login,
)
from my_claude_code.providers.chatgpt_oauth.credentials import (
    ChatGPTOAuthError,
    import_codex_cli_tokens,
)
from my_claude_code.providers.chatgpt_oauth.oauth_login import (
    CHATGPT_OAUTH_DEVICE_VERIFICATION_URL,
    _initiate_device_auth,
    exchange_device_auth_for_tokens,
)
from my_claude_code.providers.chatgpt_oauth.oauth_login import (
    ChatGPTOAuthLoginError as ChatGPTOAuthLoginFlowError,
)
from my_claude_code.providers.runtime.rotating import RotatingProvider
from my_claude_code.websearch.errors import WebSearchError
from my_claude_code.websearch.registry import search_with_logging

from .dependencies import get_services, get_settings
from .ports import ApiServices
from .web_tools.search_providers import cached_key_pool_snapshot, runtime_provider

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
LOCAL_PROVIDER_PATHS = {
    "lmstudio": "/models",
    "llamacpp": "/models",
    "ollama": "/api/tags",
}


class AdminConfigPayload(BaseModel):
    """Partial config update submitted by the admin UI."""

    values: dict[str, Any] = Field(default_factory=dict)


class WebSearchKeyPayload(BaseModel):
    """Single web search credential key submitted by the admin UI."""

    key: str


class ClaudeSettingsPathPayload(BaseModel):
    """Optional target path submitted by the Claude settings admin card."""

    path: str | None = None


class OnboardingUpdatePayload(BaseModel):
    """Partial onboarding checklist update submitted by the admin UI."""

    dismissed: bool | None = None
    visited: list[str] | None = None


class DesktopUpdatePayload(BaseModel):
    """Partial desktop preference update submitted by the admin UI.

    Only these fields are accepted; anything else is ignored so a stray body
    cannot corrupt the desktop state file. ``server_mode`` is constrained to
    the ``spawn|attach|off`` enum.
    """

    tray_enabled: bool | None = None
    start_at_login: bool | None = None
    minimize_to_tray: bool | None = None
    server_mode: str | None = None
    window: str | None = None


class ModelVisibilityPayload(BaseModel):
    """The two visibility pattern lists, as the free-text editor submits them.

    Both are raw comma-separated text rather than arrays: they are stored in
    two env values of exactly that shape, and round-tripping through a list
    would quietly normalise whitespace the user can see in the editor.
    """

    allow: str | None = None
    deny: str | None = None


class ModelVisibilityTogglePayload(BaseModel):
    """One model ticked on or off in the provider tree."""

    model_ref: str
    visible: bool


class RoutePausePayload(BaseModel):
    """One chain entry switched off, or back on, for one route.

    ``model_key`` names the route by its primary setting (``MODEL``,
    ``MODEL_OPUS``, ...) rather than by a tier name, because that is the
    identity the page already renders on the card and the only one that maps
    to exactly one pause list.
    """

    model_key: str
    model_ref: str
    paused: bool


class ModelVisibilityBulkPayload(BaseModel):
    """One bulk visibility gesture: a provider button or a picked selection.

    ``whole_provider`` is deliberately *not* a field. The server derives it as
    ``scope == "provider" and not model_refs``, so the rule that decides
    between one glob and N exact patterns lives where the tests do rather than
    in a client flag a stale page could get wrong.
    """

    scope: str
    action: str
    provider_id: str | None = None
    model_refs: list[str] = []


class ModelGlobMigrationPayload(BaseModel):
    """A request to fold exact deny patterns into ``provider/*`` globs.

    ``apply`` defaults to false so the same route answers "what would this do"
    and "do it": the preview and the write must be computed by one function, or
    the counts the user agreed to would not be the counts they got.
    """

    apply: bool = False


class ModelOverridePayload(BaseModel):
    """One provider or model override row, as the parameter editor submits it.

    ``updates`` carries three states per parameter: a value forces it, ``null``
    forces it unset, and the string ``"inherit"`` removes the key so the row
    inherits again. Only keys the user touched need be present.
    """

    scope: str
    key: str
    updates: dict[str, Any] = Field(default_factory=dict)


class RtkUpdatePayload(BaseModel):
    """Partial RTK integration update submitted by the admin UI.

    Keys are harness ids and values are booleans. Extra keys are accepted by
    the model and then filtered against ``rtk_capable_ids()`` in the handler:
    the allow-list is the registry, so a harness added there becomes toggleable
    without touching this model, and a stray key still cannot reach the RTK
    state file.
    """

    model_config = ConfigDict(extra="allow")

    def submitted_agents(self) -> dict[str, bool]:
        """Return only the registered harness flags this body actually set."""

        allowed = rtk_capable_ids()
        return {
            name: value
            for name, value in self.model_dump().items()
            if name in allowed and isinstance(value, bool)
        }


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin_is_local(origin: str | None) -> bool:
    if not origin:
        return True
    parsed = urlsplit(origin)
    return _is_loopback_host(parsed.hostname)


def require_loopback_admin(request: Request) -> None:
    """Allow admin access only from the local machine."""

    client_host = request.client.host if request.client else None
    if not _is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")

    origin = request.headers.get("origin")
    if not _origin_is_local(origin):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")


#: The literal a caller must send to destroy the request log. It is not a
#: secret and is not meant to be one -- everything reaching an admin route is
#: already on loopback. It exists because the only thing standing between the
#: whole request history and one line of shell was a ``window.confirm()`` in a
#: page the caller need never load, and a browser dialog is not a guard on an
#: HTTP endpoint. Spelling the intent out on the wire means no request deletes
#: the log unless deleting the log is exactly what it was written to do.
REQUEST_LOG_CLEAR_CONFIRMATION = "delete-all-request-log-rows"


def require_destructive_admin_confirmation(
    request: Request, confirm: str | None, expected: str
) -> None:
    """Gate a state-destroying admin route behind an explicit, deliberate call.

    Two conditions, and they fail differently on purpose:

    * ``confirm`` must equal ``expected``. A missing or wrong value is a 400,
      and the message says what to send -- this is a usability speed bump
      against replay and against a half-remembered curl, not an auth check.
    * an ``Origin`` header must be present and local. Browsers attach one to
      every non-GET request, so the dashboard satisfies this for free, while a
      bare ``curl -X DELETE`` does not send one at all. Read routes still
      accept a missing ``Origin`` (``_origin_is_local(None)`` is ``True``),
      because non-browser tooling legitimately reads the admin API; nothing
      legitimately empties the log without meaning to.
    """

    if confirm != expected:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This action is irreversible and must be confirmed explicitly: "
                f"resend with ?confirm={expected}"
            ),
        )
    origin = request.headers.get("origin")
    if origin is None or not _origin_is_local(origin):
        raise HTTPException(
            status_code=403,
            detail="This action requires a local Origin header",
        )


@lru_cache(maxsize=1)
def _bundled_image_names() -> frozenset[str]:
    directory = Path(__file__).parent / "admin_static" / "img"
    if not directory.is_dir():
        return frozenset()
    return frozenset(p.name for p in directory.iterdir() if p.suffix == ".png")


def _asset_response(filename: str) -> FileResponse:
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return FileResponse(path)


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    require_loopback_admin(request)
    return _asset_response("index.html")


@router.get("/admin/assets/{filename}", include_in_schema=False)
async def admin_asset(filename: str, request: Request):
    require_loopback_admin(request)
    if filename not in {"admin.css", "admin.js"}:
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return _asset_response(filename)


@router.get("/admin/img/{filename}", include_in_schema=False)
async def admin_image(filename: str, request: Request):
    """Serve bundled guide screenshots.

    Names are matched against the files actually shipped rather than joined
    onto a path, so a crafted filename cannot escape the directory.
    """

    require_loopback_admin(request)
    if filename not in _bundled_image_names():
        raise HTTPException(status_code=404, detail="Admin image not found")
    return _asset_response(f"img/{filename}")


@router.get("/admin/api/docs", include_in_schema=False)
async def admin_docs_index(request: Request):
    """List the curated documents that are actually bundled.

    The dashboard renders its nav from this, so a document missing from the
    wheel shows up as an absent entry rather than as a link that 404s.
    """

    require_loopback_admin(request)
    return {
        "documents": [
            {
                "slug": document.slug,
                "title": document.title,
                "summary": document.summary,
                "github_url": document.github_url,
            }
            for document in available_documents()
        ]
    }


@router.get("/admin/api/docs/{slug}", include_in_schema=False)
async def admin_document(slug: str, request: Request):
    """Serve one rendered document.

    ``slug`` is looked up in the curated table and never joined onto a
    directory path, so a crafted value cannot escape the bundle -- the same
    shape as the guide screenshots above. An unknown slug is a plain 404.
    """

    require_loopback_admin(request)
    rendered = render_document(slug)
    if rendered is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return {
        "slug": rendered.slug,
        "title": rendered.title,
        "summary": rendered.summary,
        "html": rendered.html,
        "github_url": rendered.github_url,
        "headings": [
            {"anchor": h.anchor, "text": h.text, "level": h.level}
            for h in rendered.headings
        ],
    }


@router.get("/admin/api/config")
async def get_admin_config(request: Request):
    require_loopback_admin(request)
    return load_config_response()


@router.post("/admin/api/config/validate")
async def validate_admin_config(payload: AdminConfigPayload, request: Request):
    require_loopback_admin(request)
    return validate_updates(_filtered_values(payload.values))


@router.post("/admin/api/config/apply")
async def apply_admin_config(
    payload: AdminConfigPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    result = await services.admin.apply_admin_config(_filtered_values(payload.values))
    restart = result.get("restart")
    if isinstance(restart, dict) and restart.get("automatic"):
        background_tasks.add_task(services.admin.request_restart)
    return result


@router.get("/admin/api/status")
async def admin_status(
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    return services.admin.admin_status()


@router.get("/admin/api/providers/local-status")
async def local_provider_status(request: Request):
    require_loopback_admin(request)
    config = load_config_response()
    values = {field["key"]: field["value"] for field in config["fields"]}
    checks = []
    for provider_id, path in LOCAL_PROVIDER_PATHS.items():
        base_url = _local_provider_url(provider_id, values)
        checks.append(await _check_local_provider(provider_id, base_url, path))
    return {"providers": checks}


@router.post("/admin/api/providers/{provider_id}/test")
async def test_provider(
    provider_id: str,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    return await services.admin.test_provider(provider_id)


_CREDENTIAL_ENV_KEYS = frozenset(
    descriptor.credential_env
    for descriptor in PROVIDER_CATALOG.values()
    if descriptor.credential_env is not None
)


class _CredentialKeyAddRequest(BaseModel):
    key: str


def _mask_credential_key(key: str) -> str:
    """Return a display-safe rendering of one credential key."""
    if len(key) <= 4:
        return "****"
    if len(key) <= 10:
        return f"{key[:2]}…{key[-2:]}"
    return f"{key[:6]}…{key[-4:]}"


def _credential_entry_or_404(env_key: str) -> dict[str, Any]:
    if env_key not in _CREDENTIAL_ENV_KEYS:
        raise HTTPException(status_code=404, detail="Unknown credential env key")
    return load_value_state().get(env_key, {"value": "", "source": "default"})


def _require_unlocked_credential(entry: dict[str, Any]) -> None:
    if is_locked_source(entry["source"]):
        raise HTTPException(
            status_code=409,
            detail=(
                "This credential is set via the process environment and cannot "
                "be edited from the dashboard."
            ),
        )


@router.get("/admin/api/credentials/{env_key}/keys")
async def list_credential_keys(
    env_key: str,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """List the configured keys for one provider credential (masked)."""
    require_loopback_admin(request)
    entry = _credential_entry_or_404(env_key)
    keys = parse_credential_keys(str(entry["value"]))

    # Best-effort live key health from the rotating provider, index-aligned
    # with the configured key list.
    health: list[dict[str, Any] | None] = [None] * len(keys)
    provider_id = next(
        (
            descriptor.provider_id
            for descriptor in PROVIDER_CATALOG.values()
            if descriptor.credential_env == env_key
        ),
        None,
    )
    if provider_id is not None:
        try:
            async with await services.requests.acquire() as lease:
                if lease.is_provider_cached(provider_id):
                    provider = lease.resolve_provider(provider_id)
                    if isinstance(provider, RotatingProvider):
                        snapshots = provider.key_health()
                        for i in range(min(len(keys), len(snapshots))):
                            health[i] = snapshots[i]
        except Exception:
            pass  # Health is informational only; never fail the listing.

    return {
        "env_key": env_key,
        "source": entry["source"],
        "locked": is_locked_source(entry["source"]),
        "count": len(keys),
        "keys": [_mask_credential_key(key) for key in keys],
        "health": health,
    }


@router.post("/admin/api/credentials/{env_key}/keys")
async def add_credential_key(
    env_key: str,
    payload: _CredentialKeyAddRequest,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Append one or more keys to a provider credential and apply immediately.

    Accepts a comma-separated list so a pool can be pasted in one go. This used
    to reject commas outright, which left pasting several keys possible only
    through the raw credential field -- a control that replaced the whole pool
    and read as "replace" next to a list that adds and removes. That field is no
    longer offered, so the capability lives here instead.
    """
    require_loopback_admin(request)
    entry = _credential_entry_or_404(env_key)
    _require_unlocked_credential(entry)

    submitted = parse_credential_keys(payload.key)
    if not submitted:
        raise HTTPException(status_code=400, detail="Key is empty")

    keys = list(parse_credential_keys(str(entry["value"])))
    added: list[str] = []
    for candidate in submitted:
        # Skip duplicates rather than failing the batch: pasting a pool that
        # overlaps what is already configured should add the new ones.
        if candidate in keys or candidate in added:
            continue
        added.append(candidate)

    if not added:
        raise HTTPException(
            status_code=409,
            detail=(
                "Key is already configured"
                if len(submitted) == 1
                else "Every key pasted is already configured"
            ),
        )

    keys.extend(added)
    result = await services.admin.apply_admin_config({env_key: ",".join(keys)})
    if not result.get("applied"):
        raise HTTPException(
            status_code=400,
            detail="; ".join(result.get("errors", [])) or "Update rejected",
        )
    return {
        "applied": True,
        "env_key": env_key,
        "count": len(keys),
        "added": ", ".join(_mask_credential_key(key) for key in added),
        "added_count": len(added),
        "skipped": len(submitted) - len(added),
        "restart": result.get("restart"),
    }


@router.delete("/admin/api/credentials/{env_key}/keys/{index}")
async def delete_credential_key(
    env_key: str,
    index: int,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Remove one key from a provider credential and apply immediately."""
    require_loopback_admin(request)
    entry = _credential_entry_or_404(env_key)
    _require_unlocked_credential(entry)

    keys = list(parse_credential_keys(str(entry["value"])))
    if index < 0 or index >= len(keys):
        raise HTTPException(status_code=404, detail="Key index out of range")
    removed = keys.pop(index)

    result = await services.admin.apply_admin_config({env_key: ",".join(keys)})
    if not result.get("applied"):
        raise HTTPException(
            status_code=400,
            detail="; ".join(result.get("errors", [])) or "Update rejected",
        )
    return {
        "applied": True,
        "env_key": env_key,
        "count": len(keys),
        "removed": _mask_credential_key(removed),
        "restart": result.get("restart"),
    }


# --------------------------------------------------------------------- claude settings file


def _resolve_claude_settings_path(raw_path: str | None) -> Path:
    """Expand, resolve and validate a caller-supplied Claude settings path."""

    if raw_path is None:
        return claude_settings_path()

    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail="Path must be absolute")
    if path.suffix != ".json":
        raise HTTPException(status_code=400, detail="Path must point at a .json file")
    return path.resolve()


def _claude_settings_expectations(settings: Settings) -> tuple[str, str]:
    """Return the (base_url, auth_token) this proxy expects Claude Code to use."""

    return (
        local_proxy_root_url(settings),
        proxy_auth_token(settings.anthropic_auth_token),
    )


def _claude_settings_status_response(status: ClaudeSettingsStatus) -> dict[str, Any]:
    return {
        "status": status,
        "default_path": str(claude_settings_path()),
    }


async def _claude_settings_target(
    discovered: DiscoveredSettings, expected_base_url: str, expected_auth_token: str
) -> dict[str, Any]:
    """Evaluate one discovered settings file for the ``targets`` list.

    ``origin`` is the point of the list. A machine running WSL has two Claude
    Code installations and two settings files, and "my setting did not apply"
    is almost always the other one. Naming the world each file belongs to is
    what turns a list of paths into a choice a person can make.
    """

    target_status = await asyncio.to_thread(
        read_status,
        path=Path(discovered.path),
        expected_base_url=expected_base_url,
        expected_auth_token=expected_auth_token,
    )
    return {
        "path": discovered.path,
        "origin": discovered.origin,
        "origin_label": discovered.origin_label,
        "detail": discovered.detail,
        "exists": target_status.exists,
        "state": target_status.state,
        "is_default": Path(discovered.path) == claude_settings_path(),
    }


@router.get("/admin/api/claude-settings")
async def get_claude_settings(
    request: Request,
    path: str | None = None,
    settings: Settings = Depends(get_settings),
):
    require_loopback_admin(request)
    target_path = _resolve_claude_settings_path(path)
    expected_base_url, expected_auth_token = _claude_settings_expectations(settings)
    status = await asyncio.to_thread(
        read_status,
        path=target_path,
        expected_base_url=expected_base_url,
        expected_auth_token=expected_auth_token,
    )
    # Discovery crosses the WSL boundary, which can be slow when the other side
    # is stopped, so it runs off the event loop like every other probe here.
    discovered = await asyncio.to_thread(discover_settings_files)
    targets = [
        await _claude_settings_target(entry, expected_base_url, expected_auth_token)
        for entry in discovered
    ]
    response = _claude_settings_status_response(status)
    response["targets"] = targets
    response["native_origin"] = native_origin()
    return response


@router.post("/admin/api/claude-settings/apply")
async def apply_claude_settings(
    payload: ClaudeSettingsPathPayload,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    require_loopback_admin(request)
    target_path = _resolve_claude_settings_path(payload.path)
    expected_base_url, expected_auth_token = _claude_settings_expectations(settings)
    try:
        status = await asyncio.to_thread(
            apply_proxy_env,
            path=target_path,
            base_url=expected_base_url,
            auth_token=expected_auth_token,
        )
    except ClaudeSettingsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _claude_settings_status_response(status)


@router.post("/admin/api/claude-settings/unset")
async def unset_claude_settings(
    payload: ClaudeSettingsPathPayload,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    require_loopback_admin(request)
    target_path = _resolve_claude_settings_path(payload.path)
    # The expectations do not change what is removed; they keep the response
    # able to describe what a re-apply would write.
    expected_base_url, expected_auth_token = _claude_settings_expectations(settings)
    try:
        status = await asyncio.to_thread(
            clear_proxy_env,
            path=target_path,
            expected_base_url=expected_base_url,
            expected_auth_token=expected_auth_token,
        )
    except ClaudeSettingsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _claude_settings_status_response(status)


# --------------------------------------------------------------------- onboarding checklist


def _onboarding_step_response(step: OnboardingStep) -> dict[str, Any]:
    return {
        "id": step.id,
        "label": step.label,
        "description": step.description,
        "view": step.view,
        "optional": step.optional,
        "done": step.done,
        "instructions": list(step.instructions),
        "target": step.target,
    }


def _onboarding_state_response(state: OnboardingState) -> dict[str, Any]:
    return {
        "dismissed": state.dismissed,
        "steps": [_onboarding_step_response(step) for step in state.steps],
        "required_total": state.required_total,
        "required_done": state.required_done,
        "complete": state.complete,
    }


async def _claude_settings_configured(settings: Settings) -> bool:
    """Return whether the default Claude settings.json points at this proxy.

    Never raises: an unreadable settings file counts as "not done" rather
    than failing the whole onboarding request.
    """

    expected_base_url, expected_auth_token = _claude_settings_expectations(settings)
    try:
        status = await asyncio.to_thread(
            read_status,
            path=claude_settings_path(),
            expected_base_url=expected_base_url,
            expected_auth_token=expected_auth_token,
        )
    except ClaudeSettingsError:
        return False
    return status.state == "configured"


async def _onboarding_has_requests(settings: Settings) -> bool:
    """Return whether the request log has at least one recorded request."""

    store = _request_log_store_or_none(settings)
    if store is None:
        return False
    try:
        _rows, total = await asyncio.to_thread(store.list_requests, limit=1, offset=0)
    except Exception:
        return False
    return total > 0


async def _build_onboarding_state(settings: Settings) -> OnboardingState:
    claude_settings_configured = await _claude_settings_configured(settings)
    has_requests = await _onboarding_has_requests(settings)
    return build_onboarding_state(
        claude_settings_configured=claude_settings_configured,
        has_requests=has_requests,
    )


@router.get("/admin/api/onboarding")
async def get_onboarding(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    require_loopback_admin(request)
    state = await _build_onboarding_state(settings)
    return _onboarding_state_response(state)


@router.post("/admin/api/onboarding")
async def update_onboarding(
    payload: OnboardingUpdatePayload,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    require_loopback_admin(request)
    dismissed, visited = await asyncio.to_thread(load_onboarding_persisted)

    if payload.dismissed is not None:
        dismissed = payload.dismissed
    if payload.visited is not None:
        visited = sorted(set(visited) | set(payload.visited))

    await asyncio.to_thread(
        save_onboarding_persisted, dismissed=dismissed, visited=visited
    )
    state = await _build_onboarding_state(settings)
    return _onboarding_state_response(state)


# --------------------------------------------------------------------- config-dir status
class ConfigDirStatusPayload(BaseModel):
    """Read-only snapshot of the config-dir decision, for the Get Started banner.

    Read-only in the strongest sense: there is no companion write route. The
    only thing that ever turns ``~/.fcc`` into ``~/.mcc`` is the user running
    ``mcc-migrate`` from a shell with the server stopped, so this endpoint tells
    them where their configuration lives and what to type -- nothing more.
    """

    current_dir: str = Field(..., alias="currentDir")
    new_dir: str = Field(..., alias="newDir")
    legacy_dir: str = Field(..., alias="legacyDir")
    retired_dir: str = Field(..., alias="retiredDir")
    uses_legacy_home: bool = Field(..., alias="usesLegacyHome")
    legacy_unhealthy: bool = Field(..., alias="legacyUnhealthy")
    failed_check: str | None = Field(None, alias="failedCheck")
    notice: str = ""
    banner: str = ""

    model_config = ConfigDict(populate_by_name=True)


def _config_dir_status_payload() -> ConfigDirStatusPayload:
    resolution = config_dir_resolution()
    current_dir = config_dir_path()
    new_home = new_config_dir_path()
    banner = ""
    if resolution.uses_legacy_home:
        banner = (
            f"Your configuration lives in {current_dir} (the legacy directory). "
            f"Nothing needs to change -- it stays fully supported. To move it to "
            f"{new_home}: stop the server and the tray, run mcc-migrate in a "
            f"terminal, then start the server again."
        )
        if resolution.legacy_unhealthy:
            health = resolution.legacy_health
            check = health.failed_check if health else "unknown"
            detail = health.detail if health else ""
            banner = (
                f"{current_dir} failed the '{check}' check ({detail}). It is "
                f"still the directory in use and nothing was moved, renamed or "
                f"created. Fix the problem in place; {new_home} is created only "
                f"by running mcc-migrate."
            )
    elif resolution.warning:
        # The dual-directory case: both homes exist, ~/.mcc wins, neither is
        # merged. The resolution already phrases that precisely.
        banner = resolution.warning
    health = resolution.legacy_health
    return ConfigDirStatusPayload(
        currentDir=str(current_dir),
        newDir=str(new_home),
        legacyDir=str(legacy_config_dir_path()),
        retiredDir=str(retired_config_dir_path()),
        usesLegacyHome=resolution.uses_legacy_home,
        legacyUnhealthy=resolution.legacy_unhealthy,
        failedCheck=health.failed_check if health else None,
        notice=resolution.notice,
        banner=banner,
    )


@router.get("/admin/api/config-dir")
async def get_config_dir_status(request: Request):
    """Config-dir decision, for the informational Get Started banner."""
    require_loopback_admin(request)
    return _config_dir_status_payload().model_dump(by_alias=True)


def _models_page_payload(services: ApiServices) -> dict[str, Any]:
    settings = services.requests.current_settings()
    # Runs on a worker thread (see the two ``to_thread`` call sites), so the
    # log query never sits on the event loop. An unavailable log is not an
    # error here: the page renders with no measurement rather than not at all.
    store = _request_log_store_or_none(settings)
    measured: dict[str, dict[str, Any]] = {}
    if store is not None:
        # Floored to the minute so two page loads a few seconds apart share
        # a cache key: the query is cached on ``since``, and a raw
        # ``time.time()`` would miss the cache on every single load.
        window = int(time.time() - REASONING_MEASUREMENT_DAYS * 86_400) // 60 * 60
        measured = {
            str(row["model_ref"]): row for row in store.reasoning_by_model(since=window)
        }
    return build_models_page_payload(
        services.requests.cached_prefixed_model_infos(),
        configured_chat_model_refs(settings),
        settings_model_visibility(settings),
        current_model_overrides(),
        dialect_lookup=services.requests.model_reasoning_dialect,
        measured=measured,
        measured_days=REASONING_MEASUREMENT_DAYS,
    )


@router.get("/admin/api/model-admin")
async def model_admin_page(
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Everything the Models page renders: tree, overrides and capabilities.

    One request rather than three: the three sections describe the same models
    and a partial refresh would show a tree and a capability panel that
    disagreed about which models exist.
    """

    require_loopback_admin(request)
    return await asyncio.to_thread(_models_page_payload, services)


@router.post("/admin/api/model-admin/visibility/preview")
async def preview_model_visibility(
    payload: ModelVisibilityPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Show what a pattern set would hide, without saving it.

    Nothing is persisted here on purpose: the point of the preview is to answer
    "how many models does `*:free` take away" *before* the answer becomes the
    running configuration.
    """

    require_loopback_admin(request)
    settings = services.requests.current_settings()
    visibility = ModelVisibility.from_raw(payload.allow, payload.deny)
    configured = configured_chat_model_refs(settings)
    known = {info.model_id for info in services.requests.cached_prefixed_model_infos()}
    known.update(ref.model_ref for ref in configured)
    return visibility_payload(visibility, sorted(known, key=str.casefold), configured)


@router.post("/admin/api/model-admin/visibility")
async def save_model_visibility(
    payload: ModelVisibilityPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    services: ApiServices = Depends(get_services),
):
    """Persist the two pattern lists through the ordinary settings path."""

    require_loopback_admin(request)
    return await _apply_visibility(
        services,
        background_tasks,
        ModelVisibility.from_raw(payload.allow, payload.deny),
    )


@router.post("/admin/api/model-admin/visibility/toggle")
async def toggle_model_visibility(
    payload: ModelVisibilityTogglePayload,
    request: Request,
    background_tasks: BackgroundTasks,
    services: ApiServices = Depends(get_services),
):
    """Tick one model on or off by writing an exact-match pattern."""

    require_loopback_admin(request)
    built: dict[str, ModelVisibility] = {}

    def build(settings: Settings) -> ModelVisibility:
        return apply_visibility_toggle(
            settings_model_visibility(settings),
            payload.model_ref,
            visible=payload.visible,
        )

    result = await _apply_visibility_with(services, background_tasks, build, built)
    result["model_ref"] = payload.model_ref
    if result.get("errors"):
        # A write that failed validation never reached the file, so claiming it
        # was honored -- which this route used to do unconditionally -- told the
        # user the opposite of what happened.
        return result
    # What the toggle actually achieved, which a user-written glob can still
    # overrule in either direction.
    updated = built["visibility"]
    result["visible"] = updated.is_visible(payload.model_ref)
    result["honored"] = result["visible"] == payload.visible
    # Which pattern, not just "a pattern": with 994 of them in the list, the
    # generic sentence this route used to justify was not actionable.
    result["hidden_by"] = hiding_pattern(updated, payload.model_ref)
    if not result["honored"]:
        result["blocked_by"] = result["hidden_by"]
    return result


@router.post("/admin/api/config/route-pause")
async def set_route_pause(
    payload: RoutePausePayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Switch one model on one route off, or back on, immediately.

    Written through ``apply_admin_config_with`` rather than by reading the
    list here and POSTing a replacement: two pause clicks landing together
    would each derive their new list from a base read before the other
    committed, and the second would silently drop the first. The read and the
    write are one critical section, exactly as a visibility edit is.

    Exactly one key is written. The commit renders the whole managed file from
    the values on disk plus this update, so an unsaved drag elsewhere on the
    page is neither saved nor lost by a pause click.
    """

    require_loopback_admin(request)
    paused_key = PAUSE_KEY_FOR_ROUTE.get(payload.model_key)
    if paused_key is None:
        raise HTTPException(status_code=400, detail=f"Not a route: {payload.model_key}")
    model_ref = payload.model_ref.strip()
    if not model_ref:
        raise HTTPException(status_code=400, detail="A pause needs a model ref.")

    field = FIELD_BY_KEY[paused_key]
    attr = field.settings_attr
    assert attr is not None
    written: dict[str, str] = {}

    def build(settings: Settings) -> dict[str, str]:
        current = list(parse_model_ref_list(getattr(settings, attr) or ""))
        if payload.paused:
            if model_ref not in current:
                current.append(model_ref)
        else:
            current = [entry for entry in current if entry != model_ref]
        written[paused_key] = format_model_ref_list(tuple(current))
        return dict(written)

    result = await services.admin.apply_admin_config_with(build)
    result["model_key"] = payload.model_key
    result["model_ref"] = model_ref
    result["paused_key"] = paused_key
    if result.get("errors"):
        # Nothing reached the file, so saying the model is paused would be the
        # opposite of what happened.
        return result
    result["paused"] = payload.paused
    result["paused_value"] = written.get(paused_key, "")
    return result


@router.post("/admin/api/model-admin/visibility/bulk")
async def bulk_model_visibility(
    payload: ModelVisibilityBulkPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    services: ApiServices = Depends(get_services),
):
    """Hide, show or invert many models in one settings commit.

    One request and one commit rather than N: the per-model route re-reads the
    whole catalogue after every tick, so hiding a 317-model provider from the
    client cost 634 requests and about a gigabyte of JSON -- and, because each
    toggle derived its replacement pattern list from a base it read before the
    others committed, it was lossy as well as slow.
    """

    require_loopback_admin(request)
    if payload.scope not in {"provider", "selection"}:
        raise HTTPException(status_code=400, detail=f"Unknown scope: {payload.scope}")
    if payload.action not in {"hide", "show", "invert"}:
        raise HTTPException(status_code=400, detail=f"Unknown action: {payload.action}")
    if payload.scope == "provider" and not payload.provider_id:
        raise HTTPException(
            status_code=400, detail="A provider scope needs a provider_id."
        )
    if payload.scope == "selection" and not payload.model_refs:
        raise HTTPException(
            status_code=400, detail="A selection scope needs at least one model_ref."
        )
    if len(payload.model_refs) > MODEL_VISIBILITY_BULK_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(payload.model_refs)} refs exceeds "
                f"MODEL_VISIBILITY_BULK_LIMIT ({MODEL_VISIBILITY_BULK_LIMIT})."
            ),
        )

    whole_provider = payload.scope == "provider" and not payload.model_refs
    page = await asyncio.to_thread(_models_page_payload, services)
    known: dict[str, str] = {}
    for provider in page.get("providers", []):
        for model in provider.get("models", []):
            known[str(model["model_ref"])] = str(provider["provider_id"])
    if whole_provider:
        refs = [
            ref
            for ref, provider_id in known.items()
            if provider_id == payload.provider_id
        ]
    else:
        refs = [ref for ref in payload.model_refs if ref in known]

    outcome_box: dict[str, Any] = {}
    previous_box: dict[str, list[str]] = {}

    def build(settings: Settings) -> ModelVisibility:
        base = settings_model_visibility(settings)
        previous_box["allow"] = list(base.allow)
        previous_box["deny"] = list(base.deny)
        outcome = apply_visibility_bulk(
            base,
            action=payload.action,
            refs=refs,
            provider_id=payload.provider_id,
            whole_provider=whole_provider,
        )
        outcome_box["outcome"] = outcome
        return outcome.visibility

    built: dict[str, ModelVisibility] = {}
    result = await _apply_visibility_with(services, background_tasks, build, built)
    result["action"] = payload.action
    result["scope"] = payload.scope
    result["provider_id"] = payload.provider_id
    if result.get("errors"):
        # Nothing landed, so there is nothing to report per ref and nothing to
        # undo. Saying otherwise is the bug this route was written not to have.
        return result
    outcome = outcome_box["outcome"]
    rows = bulk_result_rows(outcome.visibility, outcome.wanted)
    result["results"] = rows
    result["previous"] = previous_box
    result["removed_patterns"] = list(outcome.removed_patterns)
    result["wrote_glob"] = outcome.wrote_glob
    result["changed"] = [row["model_ref"] for row in rows if row["honored"]]
    result["honored_count"] = sum(1 for row in rows if row["honored"])
    result["unhonored_count"] = sum(1 for row in rows if not row["honored"])
    return result


@router.post("/admin/api/model-admin/visibility/migrate-globs")
async def migrate_model_visibility_globs(
    payload: ModelGlobMigrationPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    services: ApiServices = Depends(get_services),
):
    """Preview, or apply, the fold of exact deny patterns into provider globs.

    Offered rather than applied: an install can reach a thousand exact patterns
    without ever having asked for one, but rewriting somebody's configuration
    on their behalf is not a thing a page does quietly. The preview proves the
    two states hide exactly the same models, and the write is undoable.
    """

    require_loopback_admin(request)
    page = await asyncio.to_thread(_models_page_payload, services)
    provider_models = {
        str(provider["provider_id"]): [
            str(model["model_ref"]) for model in provider.get("models", [])
        ]
        for provider in page.get("providers", [])
    }
    settings = services.requests.current_settings()
    base = settings_model_visibility(settings)
    if not payload.apply:
        return _glob_migration_payload(
            base, migrate_exact_patterns_to_globs(base, provider_models), {}
        )

    previous_box: dict[str, list[str]] = {}

    def build(current: Settings) -> ModelVisibility:
        # Recomputed inside the lock rather than reusing the preview: the
        # preview read the patterns outside it, and a concurrent write would
        # otherwise be lost to a fold derived from a base that predates it.
        locked = settings_model_visibility(current)
        previous_box["allow"] = list(locked.allow)
        previous_box["deny"] = list(locked.deny)
        return migrate_exact_patterns_to_globs(locked, provider_models).visibility

    built: dict[str, ModelVisibility] = {}
    result = await _apply_visibility_with(services, background_tasks, build, built)
    if result.get("errors"):
        return result
    committed = ModelVisibility(
        allow=tuple(previous_box["allow"]), deny=tuple(previous_box["deny"])
    )
    result.update(
        _glob_migration_payload(
            committed,
            migrate_exact_patterns_to_globs(committed, provider_models),
            previous_box,
        )
    )
    result["applied"] = True
    return result


def _glob_migration_payload(
    base: ModelVisibility,
    migration: GlobMigration,
    previous: Mapping[str, list[str]],
) -> dict[str, Any]:
    """The same numbers for the preview and for the write that follows it."""

    after = migration.visibility
    return {
        "providers": list(migration.providers),
        "removed_patterns": list(migration.removed_patterns),
        "added_patterns": list(migration.added_patterns),
        "hidden_before": migration.hidden_before,
        "hidden_after": migration.hidden_after,
        "identical": migration.identical,
        "pattern_count_before": len(base.allow) + len(base.deny),
        "pattern_count_after": len(after.allow) + len(after.deny),
        "previous": dict(previous),
        "applied": False,
    }


async def _apply_visibility_with(
    services: ApiServices,
    background_tasks: BackgroundTasks,
    build: Callable[[Settings], ModelVisibility],
    built: dict[str, ModelVisibility],
) -> dict[str, Any]:
    """Compute a visibility edit and write it inside one config lock.

    The read and the write have to be the same critical section: two callers
    that each read the pattern lists and then write a full replacement pair
    derived from what they read will lose one of the two edits, and a bulk
    action loses three hundred patterns rather than one.
    """

    def updates(settings: Settings) -> dict[str, str]:
        visibility = build(settings)
        built["visibility"] = visibility
        return {
            "MODEL_VISIBILITY_ALLOW": render_patterns(visibility.allow),
            "MODEL_VISIBILITY_DENY": render_patterns(visibility.deny),
        }

    result = await services.admin.apply_admin_config_with(updates)
    visibility = built.get("visibility")
    if visibility is None:
        # The runtime refused before it ever asked for the new values, so
        # there is no edit to describe.
        return result
    return _finish_visibility(services, background_tasks, result, visibility)


def _finish_visibility(
    services: ApiServices,
    background_tasks: BackgroundTasks,
    result: dict[str, Any],
    visibility: ModelVisibility,
) -> dict[str, Any]:
    restart = result.get("restart")
    if isinstance(restart, dict) and restart.get("automatic"):
        background_tasks.add_task(services.admin.request_restart)
    result["visibility"] = {
        "allow": list(visibility.allow),
        "deny": list(visibility.deny),
    }
    return result


async def _apply_visibility(
    services: ApiServices,
    background_tasks: BackgroundTasks,
    visibility: ModelVisibility,
) -> dict[str, Any]:
    """Write both pattern lists through ``apply_admin_config``.

    Deliberately not a direct write to the env file: these are two ordinary
    settings fields, and going around the manifest would skip validation, the
    locked-source check and the restart bookkeeping every other field gets.
    """

    result = await services.admin.apply_admin_config(
        {
            "MODEL_VISIBILITY_ALLOW": render_patterns(visibility.allow),
            "MODEL_VISIBILITY_DENY": render_patterns(visibility.deny),
        }
    )
    restart = result.get("restart")
    if isinstance(restart, dict) and restart.get("automatic"):
        background_tasks.add_task(services.admin.request_restart)
    result["visibility"] = {
        "allow": list(visibility.allow),
        "deny": list(visibility.deny),
    }
    return result


@router.post("/admin/api/model-admin/overrides")
async def save_model_override_row(
    payload: ModelOverridePayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Rewrite one provider or model override row and save the file."""

    require_loopback_admin(request)
    if payload.scope not in (PROVIDER_SCOPE, MODEL_SCOPE):
        raise HTTPException(
            status_code=400,
            detail=f"scope must be '{PROVIDER_SCOPE}' or '{MODEL_SCOPE}'",
        )
    if not payload.key.strip():
        raise HTTPException(status_code=400, detail="key must not be empty")
    updated = with_override_row(
        current_model_overrides(),
        scope=payload.scope,
        key=payload.key,
        updates=payload.updates,
    )
    await asyncio.to_thread(save_model_overrides, updated)
    return await asyncio.to_thread(_models_page_payload, services)


@router.get("/admin/api/models")
async def models(
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    return _model_options(services)


# --------------------------------------------------------------------- desktop tray


def _desktop_state_response(state: DesktopState) -> dict[str, Any]:
    """Build the desktop payload, including what ``auto`` resolves to here.

    Blocking: ``resolve_auto_window`` probes the filesystem for a Chromium
    binary and measures 43 ms median / 75 ms max on a developer machine, and
    it does not get cheaper on repeat because ``shutil.which`` is the cost.
    Every caller therefore hands it to a thread -- running it inline on an
    ``async def`` handler stalls the whole event loop for that long.
    """

    resolved_provider, resolved_reason = resolve_auto_window()
    return {
        "tray_enabled": state.tray_enabled,
        "start_at_login": state.start_at_login,
        "minimize_to_tray": state.minimize_to_tray,
        "server_mode": state.server_mode,
        "window": state.window,
        "window_auto_provider": resolved_provider,
        "window_auto_reason": resolved_reason,
    }


@router.get("/admin/api/desktop")
async def get_desktop(request: Request):
    """Return the persisted desktop.json state."""
    require_loopback_admin(request)
    state = await asyncio.to_thread(load_desktop_state)
    return await asyncio.to_thread(_desktop_state_response, state)


@router.get("/admin/api/desktop/autostart-options")
async def desktop_autostart_options(request: Request):
    """Return the platform-applicable autostart targets for the settings page.

    The running server detects its own world with the same ``native_origin()``
    the Claude Code discovery page uses; the dashboard renders only the options
    that make sense for that platform.
    """
    require_loopback_admin(request)
    from my_claude_code.config.claude_discovery import native_origin

    origin = native_origin()
    default_target = "tray" if origin in {"windows", "macos"} else "server"
    return {
        "origin": origin,
        "default_target": default_target,
        "targets": [default_target],
    }


@router.post("/admin/api/desktop")
async def update_desktop(payload: DesktopUpdatePayload, request: Request):
    """Update the desktop deployment preferences.

    Only the JSON file is persisted here -- the server never applies the OS
    autostart entry (it may be running headless, with no tray or desktop
    session). The next ``mcc-desktop``/tray launch reconciles the file with
    the OS via ``apply_start_at_login`` / ``remove_start_at_login``.
    """
    require_loopback_admin(request)
    current = await asyncio.to_thread(load_desktop_state)

    if payload.server_mode is not None and payload.server_mode not in SERVER_MODES:
        raise HTTPException(status_code=422, detail="Invalid server mode")
    if payload.window is not None and payload.window not in WINDOW_PREFERENCES:
        raise HTTPException(status_code=400, detail="Invalid window preference")

    updates: dict[str, Any] = {}
    for name in (
        "tray_enabled",
        "start_at_login",
        "minimize_to_tray",
        "server_mode",
        "window",
    ):
        submitted = getattr(payload, name)
        if submitted is not None:
            updates[name] = submitted
    if not updates:
        return await asyncio.to_thread(_desktop_state_response, current)

    updated = DesktopState(
        tray_enabled=updates.get("tray_enabled", current.tray_enabled),
        start_at_login=updates.get("start_at_login", current.start_at_login),
        minimize_to_tray=updates.get("minimize_to_tray", current.minimize_to_tray),
        server_mode=updates.get("server_mode", current.server_mode),
        window=updates.get("window", current.window),
        # window_open and the last-applied-window-size fields are lifecycle
        # state owned by the desktop tray, not user preferences -- they are
        # never part of the admin payload, but must still be carried forward
        # here or an unrelated dashboard save would silently reset them.
        window_open=current.window_open,
        last_applied_window_width=current.last_applied_window_width,
        last_applied_window_height=current.last_applied_window_height,
    )
    await asyncio.to_thread(save_desktop_state, updated)
    return await asyncio.to_thread(_desktop_state_response, updated)


# --------------------------------------------------------------------- rtk token optimizer


def _rtk_state_response(state: RtkState) -> dict[str, Any]:
    return {**state.as_dict(), "agents": state.as_dict()}


@router.get("/admin/api/rtk")
async def get_rtk(request: Request):
    """Return the persisted RTK desired state plus verified binary status."""
    require_loopback_admin(request)
    status = await asyncio.to_thread(rtk_status)
    state = await asyncio.to_thread(load_rtk_state)
    return {**status, **_rtk_state_response(state)}


@router.get("/admin/api/rtk/gain")
async def get_rtk_gain(request: Request):
    """Return RTK's own token-savings report, or why it is unavailable.

    Always 200: ``read_rtk_gain`` converts every failure mode into an
    ``available: false`` payload so the dashboard never breaks on a missing or
    misbehaving RTK install.
    """
    require_loopback_admin(request)
    return await asyncio.to_thread(read_rtk_gain)


@router.post("/admin/api/rtk")
async def update_rtk(payload: RtkUpdatePayload, request: Request):
    """Update the desired RTK integration and reconcile the machine.

    Unlike the desktop endpoint, this one *does* reconcile: RTK installs hooks
    into each coding agent's global config, so persisting the flag alone would
    leave the machine out of step with the dashboard.
    """
    require_loopback_admin(request)
    current = await asyncio.to_thread(load_rtk_state)

    updates = payload.submitted_agents()
    if not updates:
        return await asyncio.to_thread(rtk_status)

    updated = RtkState({**current.as_dict(), **updates})
    try:
        await asyncio.to_thread(save_rtk_state, updated)
        await asyncio.to_thread(apply_rtk_state, updated)
    except RtkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await asyncio.to_thread(rtk_status)


@router.get("/admin/api/websearch/credentials/{env_key}/keys")
async def list_websearch_credential_keys(env_key: str, request: Request):
    require_loopback_admin(request)
    descriptor = _websearch_descriptor_for_env(env_key)
    state = load_value_state()
    entry = state.get(env_key, {"value": "", "source": "default"})
    keys = parse_credential_keys(entry["value"])
    return {
        "provider_id": descriptor.provider_id,
        "env_key": env_key,
        "locked": is_locked_source(entry["source"]),
        "keys": [
            {"index": index, "key_label": mask_credential_label(key)}
            for index, key in enumerate(keys)
        ],
        "health": cached_key_pool_snapshot(descriptor.provider_id),
    }


@router.post("/admin/api/websearch/credentials/{env_key}/keys")
async def add_websearch_credential_key(
    env_key: str,
    payload: WebSearchKeyPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    descriptor = _websearch_descriptor_for_env(env_key)
    key = payload.key.strip()
    if not key or "," in key:
        raise HTTPException(
            status_code=422,
            detail="API key must be non-empty and must not contain commas",
        )
    keys = _editable_websearch_keys(env_key)
    result = await services.admin.apply_admin_config({env_key: ",".join([*keys, key])})
    return result | {
        "provider_id": descriptor.provider_id,
        "keys": _masked_keys(env_key),
    }


@router.delete("/admin/api/websearch/credentials/{env_key}/keys/{index}")
async def delete_websearch_credential_key(
    env_key: str,
    index: int,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    descriptor = _websearch_descriptor_for_env(env_key)
    keys = _editable_websearch_keys(env_key)
    if index < 0 or index >= len(keys):
        raise HTTPException(status_code=404, detail="Web search key index out of range")
    del keys[index]
    result = await services.admin.apply_admin_config({env_key: ",".join(keys)})
    return result | {
        "provider_id": descriptor.provider_id,
        "keys": _masked_keys(env_key),
    }


@router.post("/admin/api/websearch/providers/{provider_id}/test")
async def test_websearch_provider(
    provider_id: str,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    if provider_id not in WEBSEARCH_CATALOG:
        raise HTTPException(status_code=404, detail="Unknown web search provider")
    settings = services.requests.current_settings()
    started = time.perf_counter()
    try:
        provider = await runtime_provider(settings, provider_id)
        response = await search_with_logging(provider, "web search", max_results=3)
    except WebSearchError as error:
        return {
            "provider_id": provider_id,
            "ok": False,
            "latency_ms": _elapsed_millis(started),
            "error": _websearch_error_payload(error),
        }
    return {
        "provider_id": provider_id,
        "ok": True,
        "latency_ms": _elapsed_millis(started),
        "result_count": len(response.results),
        "titles": [item.title for item in response.results[:3]],
    }


def _websearch_descriptor_for_env(env_key: str) -> WebSearchDescriptor:
    for descriptor in WEBSEARCH_CATALOG.values():
        if descriptor.credential_env == env_key:
            return descriptor
    raise HTTPException(status_code=404, detail="Unknown web search credential")


def _editable_websearch_keys(env_key: str) -> list[str]:
    """Current parsed keys, refusing mutation when an external source owns the value."""

    entry = load_value_state().get(env_key, {"value": "", "source": "default"})
    if is_locked_source(entry["source"]):
        raise HTTPException(
            status_code=409,
            detail=f"{env_key} comes from a locked source ({entry['source']})",
        )
    return list(parse_credential_keys(entry["value"]))


def _masked_keys(env_key: str) -> list[dict[str, Any]]:
    entry = load_value_state().get(env_key, {"value": "", "source": "default"})
    return [
        {"index": index, "key_label": mask_credential_label(key)}
        for index, key in enumerate(parse_credential_keys(entry["value"]))
    ]


def _websearch_error_payload(error: WebSearchError) -> dict[str, Any]:
    return {
        "kind": error.kind,
        "message": error.message,
        "status_code": error.status_code,
    }


def _elapsed_millis(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


@router.post("/admin/api/models/refresh")
async def refresh_models(
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    result = await services.admin.refresh_models()
    return _model_options(services, refresh_result=result)


def _model_options(
    services: ApiServices,
    *,
    refresh_result: ProviderModelRefreshResult | None = None,
) -> dict[str, list[str]]:
    settings = services.requests.current_settings()
    # Configured refs are never filtered here, unlike in `/v1/models`. A picker
    # has to be able to render the value that is actually saved; dropping a
    # hidden-but-configured ref would leave the select showing nothing while
    # the route it names keeps serving traffic. Hiding is for the hundreds of
    # models nobody chose.
    configured = {ref.model_ref for ref in configured_chat_model_refs(settings)}
    visibility = settings_model_visibility(settings)
    infos = tuple(
        info
        for info in services.requests.cached_prefixed_model_infos()
        if visibility.is_visible(info.model_id)
    )
    discovered = {info.model_id for info in infos}
    failed_provider_ids = (
        refresh_result.failed_provider_ids if refresh_result is not None else ()
    )
    # Only models the provider *says* reject images. An unreported capability
    # is not a refusal, so it stays out of this list -- the routing page uses
    # it to say "this tier needs the vision adapter", which would be a lie for
    # a model that simply publishes no modality metadata.
    return {
        "models": sorted(configured | discovered, key=str.casefold),
        "failed_providers": list(failed_provider_ids),
        "blind_models": sorted(
            (info.model_id for info in infos if info.supports_vision is False),
            key=str.casefold,
        ),
    }


def _filtered_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in FIELD_BY_KEY}


def _local_provider_url(provider_id: str, values: dict[str, str]) -> str:
    if provider_id == "lmstudio":
        return values.get("LM_STUDIO_BASE_URL", "")
    if provider_id == "llamacpp":
        return values.get("LLAMACPP_BASE_URL", "")
    if provider_id == "ollama":
        return values.get("OLLAMA_BASE_URL", "")
    return ""


async def _check_local_provider(
    provider_id: str, base_url: str, path: str
) -> dict[str, Any]:
    clean_url = base_url.strip().rstrip("/")
    if not clean_url:
        return {
            "provider_id": provider_id,
            "status": "missing_url",
            "label": "Missing URL",
            "base_url": base_url,
        }

    url = f"{clean_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get(url)
        ok = 200 <= response.status_code < 300
        return {
            "provider_id": provider_id,
            "status": "reachable" if ok else "offline",
            "label": "Reachable" if ok else "Offline",
            "base_url": base_url,
            "status_code": response.status_code,
        }
    except Exception as exc:
        return {
            "provider_id": provider_id,
            "status": "offline",
            "label": "Offline",
            "base_url": base_url,
            "error_type": type(exc).__name__,
        }


class _ChatGPTOAuthInitiateResponse(BaseModel):
    device_auth_id: str
    user_code: str
    verification_url: str


class _ChatGPTOAuthBrowserInitiateResponse(BaseModel):
    authorize_url: str
    expires_in: str


@router.post("/admin/api/chatgpt-oauth/browser/initiate")
async def chatgpt_oauth_browser_initiate(
    request: Request,
    same_host_confirmed: bool = False,
):
    """Start a browser-based ChatGPT OAuth login (PKCE + local callback)."""
    require_loopback_admin(request)
    try:
        payload = await asyncio.to_thread(
            start_browser_login,
            allow_remote=same_host_confirmed,
        )
    except ChatGPTOAuthBrowserUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _ChatGPTOAuthBrowserInitiateResponse(**payload)


@router.post("/admin/api/chatgpt-oauth/browser/status")
async def chatgpt_oauth_browser_status(request: Request):
    """Poll the status of the in-flight browser OAuth login."""
    require_loopback_admin(request)
    return await asyncio.to_thread(browser_login_status)


class _ChatGPTOAuthExchangeRequest(BaseModel):
    device_auth_id: str
    user_code: str


class _ChatGPTOAuthExchangeResponse(BaseModel):
    status: str
    credential_reference: str = ""
    account_id: str = ""
    message: str = ""


@router.post("/admin/api/chatgpt-oauth/initiate")
async def chatgpt_oauth_initiate(request: Request):
    """Start a ChatGPT/Codex OAuth device-auth flow from the admin UI."""
    require_loopback_admin(request)
    try:
        device_auth_id, user_code, _interval_ms = await asyncio.to_thread(
            _initiate_device_auth
        )
    except ChatGPTOAuthLoginFlowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ChatGPTOAuthInitiateResponse(
        device_auth_id=device_auth_id,
        user_code=user_code,
        verification_url=CHATGPT_OAUTH_DEVICE_VERIFICATION_URL,
    )


@router.post("/admin/api/chatgpt-oauth/exchange")
async def chatgpt_oauth_exchange(
    payload: _ChatGPTOAuthExchangeRequest,
    request: Request,
):
    """Poll for ChatGPT/Codex OAuth completion and return tokens."""
    require_loopback_admin(request)
    try:
        tokens = await asyncio.to_thread(
            exchange_device_auth_for_tokens,
            payload.device_auth_id,
            payload.user_code,
            timeout_seconds=8.0,
        )
    except ChatGPTOAuthLoginFlowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if tokens is None:
        return _ChatGPTOAuthExchangeResponse(
            status="pending",
            message="Waiting for authorization. Open the verification URL and enter the code.",
        )
    return _ChatGPTOAuthExchangeResponse(
        status="complete",
        credential_reference=CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
        account_id=tokens.get("account_id", ""),
        message="Login successful. Credentials saved to MCC's private store.",
    )


class _ChatGPTOAuthImportCodexResponse(BaseModel):
    status: str
    credential_reference: str = ""
    account_id: str = ""
    message: str = ""


@router.post("/admin/api/chatgpt-oauth/import-codex")
async def chatgpt_oauth_import_codex(request: Request):
    """Import ChatGPT/Codex OAuth tokens from an existing Codex CLI install."""
    require_loopback_admin(request)
    try:
        credentials = await asyncio.to_thread(import_codex_cli_tokens)
    except ChatGPTOAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _ChatGPTOAuthImportCodexResponse(
        status="complete",
        credential_reference=CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
        account_id=credentials.account_id,
        message="Copied renewable Codex credentials into MCC's private store.",
    )


# The literal the card prints wherever a rate-limit window has no observed
# value. "You hit your 5-hour limit" is a claim MCC may only repeat from a
# header Anthropic actually sent; where no response has carried one, the honest
# answer is that nobody has measured, and it is spelled the same way every time
# so nothing downstream has to guess whether a blank meant zero.
NOT_YET_OBSERVED = "not yet observed"


class _AnthropicOAuthSourceInfo(BaseModel):
    available: bool
    masked_token: str = ""
    expires_at: int | None = None
    subscription_type: str | None = None
    # Parsed away by every release before 6.36.0, and both are exactly what
    # the card needs: one says whether a refresh can still save this
    # credential, the other names the plan's rate-limit tier.
    refresh_token_expires_at: int | None = None
    rate_limit_tier: str | None = None
    scopes: list[str] = []
    has_inference_scope: bool = False
    source: str = ""


class _AnthropicOAuthWindows(BaseModel):
    """Anthropic's unified rate-limit headers, as last received.

    Every field is either a string Anthropic sent or :data:`NOT_YET_OBSERVED`.
    Nothing here is computed from a clock.
    """

    observed: bool = False
    observed_at: float | None = None
    status: str = NOT_YET_OBSERVED
    reset: str = NOT_YET_OBSERVED
    five_hour_utilization: str = NOT_YET_OBSERVED
    five_hour_reset: str = NOT_YET_OBSERVED
    weekly_utilization: str = NOT_YET_OBSERVED
    weekly_reset: str = NOT_YET_OBSERVED
    overage_status: str = NOT_YET_OBSERVED
    overage_reset: str = NOT_YET_OBSERVED
    usage_limit: str = NOT_YET_OBSERVED


class _AnthropicOAuthSourcesResponse(BaseModel):
    claude_code: _AnthropicOAuthSourceInfo
    mcc: _AnthropicOAuthSourceInfo
    windows: _AnthropicOAuthWindows = _AnthropicOAuthWindows()


def _anthropic_oauth_source_info(
    tokens: OAuthTokens | None,
) -> _AnthropicOAuthSourceInfo:
    if tokens is None:
        return _AnthropicOAuthSourceInfo(available=False)
    return _AnthropicOAuthSourceInfo(
        available=True,
        masked_token=mask_credential_label(tokens.access_token),
        expires_at=tokens.expires_at,
        subscription_type=tokens.subscription_type,
        refresh_token_expires_at=tokens.refresh_token_expires_at,
        rate_limit_tier=tokens.rate_limit_tier,
        scopes=list(tokens.scopes),
        # Offset 183645139 of Claude Code 2.1.258: this is the scope its own
        # gate reads before it will send an inference request at all.
        has_inference_scope=ANTHROPIC_INFERENCE_SCOPE in tokens.scopes,
        source=tokens.source,
    )


def _anthropic_oauth_windows() -> _AnthropicOAuthWindows:
    """Report the last observed rate-limit headers, or that there are none."""
    snapshot = ANTHROPIC_RATE_LIMIT_OBSERVER.latest
    if snapshot is None:
        return _AnthropicOAuthWindows()
    values = snapshot.values

    def read(name: str) -> str:
        return values.get(name, NOT_YET_OBSERVED)

    return _AnthropicOAuthWindows(
        observed=True,
        observed_at=snapshot.observed_at,
        status=read("anthropic-ratelimit-unified-status"),
        reset=read("anthropic-ratelimit-unified-reset"),
        five_hour_utilization=read("anthropic-ratelimit-unified-5h-utilization"),
        five_hour_reset=read("anthropic-ratelimit-unified-5h-reset"),
        weekly_utilization=read("anthropic-ratelimit-unified-7d-utilization"),
        weekly_reset=read("anthropic-ratelimit-unified-7d-reset"),
        overage_status=read("anthropic-ratelimit-unified-overage-status"),
        overage_reset=read("anthropic-ratelimit-unified-overage-reset"),
        usage_limit=read("anthropic-usage-limit"),
    )


@router.get("/admin/api/anthropic-oauth/sources")
async def anthropic_oauth_sources(request: Request):
    """Report which Claude subscription credential sources are available.

    Never reads or returns a raw token: only masked labels and expiry.
    """
    require_loopback_admin(request)
    claude_code_tokens = await asyncio.to_thread(load_claude_code_tokens)
    mcc_tokens = await asyncio.to_thread(load_managed_tokens)
    return _AnthropicOAuthSourcesResponse(
        claude_code=_anthropic_oauth_source_info(claude_code_tokens),
        mcc=_anthropic_oauth_source_info(mcc_tokens),
        windows=_anthropic_oauth_windows(),
    )


class _AnthropicOAuthImportResponse(BaseModel):
    status: str
    credential_reference: str = ""
    subscription_type: str | None = None
    message: str = ""


@router.post("/admin/api/anthropic-oauth/import-claude-code")
async def anthropic_oauth_import_claude_code(request: Request):
    """Copy Claude Code's own OAuth credential into MCC's private store.

    Read-only against ``~/.claude/.credentials.json``: this only reads that
    file and writes MCC's own managed store. Claude Code's file is never
    written to and never refreshed in place.
    """
    require_loopback_admin(request)
    tokens = await asyncio.to_thread(load_claude_code_tokens)
    if tokens is None:
        detail = f"No Claude Code credential found at {claude_credentials_path()}"
        if sys.platform == "darwin":
            # Claude Code on macOS stores the credential in the login keychain
            # rather than that file ("secure storage (keychain/credentials
            # file)", 2.1.260), so the file being absent is the *expected*
            # case there and the bare message reads as a bug. Reading the
            # keychain is out of scope for this release; naming it is not.
            detail += (
                ". On macOS, Claude Code usually keeps it in the login "
                "keychain instead, which MCC cannot read -- sign in with "
                "'Sign in with Anthropic' above instead of importing."
            )
        raise HTTPException(status_code=400, detail=detail)
    await asyncio.to_thread(store_anthropic_oauth_tokens, tokens)
    return _AnthropicOAuthImportResponse(
        status="complete",
        credential_reference=ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
        subscription_type=tokens.subscription_type,
        message="Copied Claude Code's credential into MCC's private store.",
    )


class _AnthropicOAuthInitiateResponse(BaseModel):
    authorize_url: str
    verifier: str


@router.post("/admin/api/anthropic-oauth/initiate")
async def anthropic_oauth_initiate(request: Request):
    """Start a Claude subscription OAuth login (PKCE, paste-code flow)."""
    require_loopback_admin(request)
    verifier = generate_pkce_verifier()
    return _AnthropicOAuthInitiateResponse(
        authorize_url=build_authorize_url(verifier),
        verifier=verifier,
    )


class _AnthropicOAuthCompleteRequest(BaseModel):
    pasted_code: str
    verifier: str


class _AnthropicOAuthCompleteResponse(BaseModel):
    status: str
    credential_reference: str = ""
    subscription_type: str | None = None
    message: str = ""


@router.post("/admin/api/anthropic-oauth/complete")
async def anthropic_oauth_complete(
    payload: _AnthropicOAuthCompleteRequest,
    request: Request,
):
    """Finish a Claude subscription OAuth login with the pasted code."""
    require_loopback_admin(request)
    code, state = split_pasted_code(payload.pasted_code)
    try:
        tokens = await exchange_anthropic_oauth_code(code, payload.verifier, state)
    except AnthropicOAuthLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _AnthropicOAuthCompleteResponse(
        status="complete",
        credential_reference=ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
        subscription_type=tokens.subscription_type,
        message="Signed in. Credential stored in MCC's private store.",
    )


class _AnthropicOAuthLoopbackInitiateResponse(BaseModel):
    authorize_url: str
    redirect_uri: str


@router.post("/admin/api/anthropic-oauth/loopback/initiate")
async def anthropic_oauth_loopback_initiate(
    request: Request,
    same_host_confirmed: bool = False,
):
    """Start a Claude subscription sign-in that completes without pasting.

    A callback server binds an ephemeral port on ``127.0.0.1`` and catches
    Anthropic's redirect, exactly as Claude Code's own ``http://localhost:
    <port>/callback`` flow does. The paste flow stays as the fallback.

    ``same_host_confirmed`` is the caller asserting that the browser really
    shares this process's loopback namespace -- the same guard, and the same
    503, as the ChatGPT browser login, because under WSL or over SSH
    "localhost" means two different things and the callback silently never
    arrives.
    """
    require_loopback_admin(request)
    try:
        started = await asyncio.to_thread(
            start_loopback_login,
            allow_remote=same_host_confirmed,
        )
    except AnthropicOAuthLoopbackUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _AnthropicOAuthLoopbackInitiateResponse(**started)


class _AnthropicOAuthLoopbackStatusResponse(BaseModel):
    status: str
    credential_reference: str = ""
    subscription_type: str = ""
    message: str = ""


@router.post("/admin/api/anthropic-oauth/loopback/status")
async def anthropic_oauth_loopback_status(request: Request):
    """Poll the in-flight loopback sign-in."""
    require_loopback_admin(request)
    result = await loopback_login_status()
    return _AnthropicOAuthLoopbackStatusResponse(
        status=result["status"],
        credential_reference=(
            ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE
            if result["status"] == "complete"
            else ""
        ),
        subscription_type=result.get("subscription_type", ""),
        message=result.get("message", ""),
    )


class _AnthropicOAuthRefreshResponse(BaseModel):
    status: str
    credential_reference: str = ""
    expires_at: int | None = None
    subscription_type: str | None = None
    message: str = ""


@router.post("/admin/api/anthropic-oauth/refresh")
async def anthropic_oauth_refresh(request: Request):
    """Refresh the stored Claude subscription credential, now, on demand.

    Before 6.43.0 there was no control for this at all: the card could say the
    access token expired days ago and offer nothing to do about it.

    The two failure classes are reported apart, because they call for opposite
    actions. A *transient* failure (a 429 from a rate-limited token endpoint,
    a 5xx, a transport error) is a 503 here and the credential is untouched --
    wait and try again. A *definitive* rejection is a 401 and the store has
    already been set aside by then -- sign in again or import.
    """
    require_loopback_admin(request)
    try:
        tokens = await asyncio.to_thread(load_tokens)
    except AnthropicOAuthUnavailableError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not tokens.has_refresh_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "The stored credential has no refresh token, so it cannot be "
                "renewed. Sign in again, or import your Claude Code credential."
            ),
        )
    try:
        refreshed = await refresh_anthropic_oauth_tokens(tokens)
    except AnthropicOAuthRefreshError as exc:
        # 401 tells the dashboard "this credential is finished"; 503 tells it
        # "Anthropic could not answer, the credential is fine". Any other
        # mapping loses the distinction the whole 6.43.0 change is about.
        raise HTTPException(
            status_code=401 if exc.definitive else 503,
            detail=str(exc),
        ) from exc
    return _AnthropicOAuthRefreshResponse(
        status="complete",
        credential_reference=ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
        expires_at=refreshed.expires_at,
        subscription_type=refreshed.subscription_type,
        message="Refreshed the Claude subscription credential.",
    )


class _AnthropicOAuthDisconnectResponse(BaseModel):
    status: str
    quarantined_as: str = ""
    message: str = ""


@router.post("/admin/api/anthropic-oauth/disconnect")
async def anthropic_oauth_disconnect(request: Request):
    """Set MCC's own Claude subscription credential aside.

    The store is renamed to ``anthropic_oauth.json.dead-<epoch>``, never
    deleted: the same rule a definitive refresh rejection follows, so that the
    evidence survives for whoever investigates later.

    Claude Code's own ``~/.claude/.credentials.json`` is not touched -- so
    after disconnecting, MCC falls back to it if it is healthy, which is
    usually what the operator wanted. Nothing needs restarting: the provider
    notices the store is gone on its next request.

    ``ANTHROPIC_OAUTH_ACCESS_TOKEN`` is left alone deliberately. It holds a
    non-secret sentinel that ``Settings`` back-fills from the store's mere
    existence, so removing the store is already enough to clear it on the next
    settings load; writing the env file here would fight that validator.
    """
    require_loopback_admin(request)
    quarantined = await asyncio.to_thread(quarantine_anthropic_oauth_store)
    if quarantined is None:
        return _AnthropicOAuthDisconnectResponse(
            status="complete",
            message="There was no MCC-owned credential to disconnect.",
        )
    return _AnthropicOAuthDisconnectResponse(
        status="complete",
        quarantined_as=quarantined.name,
        message=(
            f"Disconnected. The credential was kept as {quarantined.name} "
            "rather than deleted."
        ),
    )


# --------------------------------------------------------------------- requests log


def _request_log_store_or_none(
    settings: Settings,
) -> RequestLogStore | None:
    return store_from_settings(settings)


def _validate_request_log_status(status: str | None) -> None:
    if status is not None and status not in {"success", "error", "cancelled"}:
        raise HTTPException(status_code=422, detail="Invalid status filter")


def _validate_request_log_local(local: str | None) -> None:
    """Reject an unknown ``local`` value instead of silently showing everything.

    Absent means ``all``: the API default is unchanged, so exports, curl users
    and the Guide's examples still see every row. Only the dashboard asks for
    ``hide``.
    """
    if local is not None and local not in LOCAL_FILTER_VALUES:
        raise HTTPException(status_code=422, detail="Invalid local filter")


def _harness_labels(harness_ids: Iterable[str]) -> dict[str, str]:
    """Display names for exactly the harness ids in one payload.

    Two vocabularies meet here and only this layer can see both. ``config``
    owns the registry of agents MCC can launch; ``core`` owns the ids its
    fingerprinter invents for clients that are no such thing -- a bare SDK, a
    curl one-liner, ``unknown``. Neither package may import the other, so the
    union is resolved at the boundary that already imports both.
    """
    return {
        harness_id: NON_REGISTRY_HARNESS_LABELS.get(harness_id)
        or harness_display_name(harness_id)
        for harness_id in harness_ids
    }


@router.get("/admin/api/requests")
async def list_request_log(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    provider: str | None = None,
    model: str | None = None,
    status: str | None = None,
    endpoint: str | None = None,
    key: str | None = None,
    since: float | None = None,
    until: float | None = None,
    q: str | None = None,
    local: str | None = None,
    harness: str | None = None,
    settings: Settings = Depends(get_settings),
):
    """Page through the persisted request log (newest first)."""
    require_loopback_admin(request)
    store = _request_log_store_or_none(settings)
    if store is None:
        return {
            "enabled": False,
            "rows": [],
            "total": 0,
            "limit": limit,
            "offset": offset,
        }
    _validate_request_log_status(status)
    _validate_request_log_local(local)
    # SQLite work is synchronous; run it off the event loop so analytics
    # queries cannot stall proxy traffic.
    rows, total = await asyncio.to_thread(
        store.list_requests,
        limit=limit,
        offset=offset,
        provider=provider,
        model=model,
        status=status,
        endpoint=endpoint,
        key=key,
        since=since,
        until=until,
        q=q,
        local=local,
        harness=harness,
    )
    return {
        "enabled": True,
        "capture_bodies": bool(settings.request_log_capture_bodies),
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/admin/api/requests/stats")
async def request_log_stats(
    request: Request,
    provider: str | None = None,
    model: str | None = None,
    status: str | None = None,
    endpoint: str | None = None,
    key: str | None = None,
    since: float | None = None,
    until: float | None = None,
    q: str | None = None,
    local: str | None = None,
    harness: str | None = None,
    settings: Settings = Depends(get_settings),
):
    """Aggregate request analytics over an optional epoch-second window.

    The payload carries ``served_from``: ``"rollup"`` when it came from the
    pre-aggregated stats tables, ``"rows"`` when it was computed by scanning
    the request log -- which a free-text ``q`` always forces, and which is
    also the answer until the one-time rollup backfill has finished. A
    rollup-served window is snapped outward to whole UTC hours, reported as
    ``window.snapped_since`` / ``window.snapped_until``.
    """
    require_loopback_admin(request)
    store = _request_log_store_or_none(settings)
    if store is None:
        return {"enabled": False}
    _validate_request_log_status(status)
    _validate_request_log_local(local)
    result = await asyncio.to_thread(
        store.stats,
        provider=provider,
        model=model,
        status=status,
        endpoint=endpoint,
        key=key,
        since=since,
        until=until,
        q=q,
        local=local,
        harness=harness,
    )
    result["enabled"] = True
    result["capture_bodies"] = bool(settings.request_log_capture_bodies)
    # Resolved here and shipped with the numbers, because the store cannot do
    # it: ``core`` may not import ``config``, so the harness registry is out of
    # its reach. Sending the labels beside the breakdown also means the
    # dashboard never has to carry a copy of the registry to render a name.
    result["harness_labels"] = _harness_labels(
        row["key"] for row in result.get("by_harness", [])
    )
    # Lets the dashboard say "these totals have stopped rising" when the table
    # is at its cap, instead of leaving the plateau unexplained.
    result["retained_rows_max"] = int(settings.request_log_max_rows)
    # Uptime over the same window, so a flat stretch in the series can be read
    # as "no traffic" or "no server" instead of being ambiguous.
    result["coverage"] = await asyncio.to_thread(
        store.coverage, since=since, until=until
    )
    return result


@router.get("/admin/api/requests/lifetime")
async def request_log_lifetime(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """All-time counters, which retention never prunes.

    ``/admin/api/requests/stats`` aggregates the ``requests`` table, and that
    table is capped at ``REQUEST_LOG_MAX_ROWS``. Once the cap is reached its
    sums stop growing. These come from a separate rollup that is only added to.
    """
    require_loopback_admin(request)
    store = _request_log_store_or_none(settings)
    if store is None:
        return {"enabled": False}
    result = await asyncio.to_thread(store.lifetime)
    result["enabled"] = True
    result["retained_rows_max"] = int(settings.request_log_max_rows)
    return result


@router.get("/admin/api/requests/optimization-stats")
async def request_log_optimization_stats(
    request: Request,
    since: float | None = None,
    until: float | None = None,
    settings: Settings = Depends(get_settings),
):
    """Per-rule fire counts and tokens avoided, for the Token Optimizer page.

    Every rule the proxy can apply appears here, including one that has never
    matched: a rule missing from the list is indistinguishable from a rule that
    does not exist, and "this has never fired" is a thing a reader needs told.
    A rule that has never fired carries ``requests: 0`` and
    ``tokens_saved: null`` -- the count is a real zero, the saving is unknown,
    and the two are not the same claim.

    ``enabled`` is the rule's live setting, so the state beside a number is the
    state that produced it.
    """
    require_loopback_admin(request)
    store = _request_log_store_or_none(settings)
    specs = [
        {
            "rule": spec.rule,
            "label": spec.label,
            "description": spec.description,
            "answer": spec.answer,
            "env_key": spec.env_key,
            "enabled": bool(getattr(settings, spec.settings_attr)),
        }
        for spec in OPTIMIZATION_RULE_SPECS
    ]
    if store is None:
        return {"enabled": False, "rules": specs}
    result = await asyncio.to_thread(store.optimization_stats, since=since, until=until)
    measured = {row["rule"]: row for row in result.get("rules", [])}
    merged: list[dict[str, Any]] = []
    for spec in specs:
        row = measured.pop(spec["rule"], None)
        if row is None:
            merged.append(
                {
                    **spec,
                    "requests": 0,
                    # Not 0: nothing was measured, so nothing is claimed.
                    "tokens_saved": None,
                    "tokens_reported": 0,
                    "first_ts": None,
                    "last_ts": None,
                    "daily": [],
                }
            )
            continue
        merged.append({**spec, **row})
    # Rules retired since the rows were written still hold real savings. They
    # are reported, flagged as no longer present, and never silently dropped.
    merged.extend(
        {
            "label": row["rule"],
            "description": "This rule is no longer part of the proxy.",
            "answer": None,
            "env_key": None,
            "enabled": None,
            "retired": True,
            **row,
        }
        for row in measured.values()
    )
    result["rules"] = merged
    result["enabled"] = True
    return result


@router.get("/admin/api/requests/discover-optimizations")
async def request_log_discover_optimizations(
    request: Request,
    row_limit: int = DEFAULT_SCAN_ROW_LIMIT,
    since: float | None = None,
    until: float | None = None,
    min_requests: int = 2,
    family_limit: int = 50,
    settings: Settings = Depends(get_settings),
):
    """Cluster logged requests into recurring prompt families, on demand.

    Answers "which repeated request shapes are we paying for, and which are
    already answered locally" -- the drift the hand-maintained optimization
    rule list cannot report on itself. Purely observational: it proposes
    nothing and changes nothing about how any request is answered.

    Deliberately not cached and not scheduled. Every call is a fresh
    decompressing scan a human asked for, bounded by ``row_limit`` so the
    default returns in seconds on a large log; the ``scanned`` block in the
    response states what the bound actually covered.
    """
    require_loopback_admin(request)
    store = _request_log_store_or_none(settings)
    if store is None:
        return {"enabled": False}
    if row_limit > MAX_SCAN_ROW_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=f"row_limit must not exceed {MAX_SCAN_ROW_LIMIT}",
        )
    # A decompressing scan of thousands of rows is seconds of blocking CPU;
    # on the event loop it would stall every proxied request for that long.
    result = await asyncio.to_thread(
        discover_families,
        store,
        row_limit=row_limit,
        since=since,
        until=until,
        min_requests=min_requests,
        family_limit=family_limit,
    )
    result["enabled"] = True
    result["capture_bodies"] = bool(settings.request_log_capture_bodies)
    return result


@router.get("/admin/api/requests/pulse")
async def request_log_pulse(
    request: Request,
    provider: str | None = None,
    model: str | None = None,
    status: str | None = None,
    endpoint: str | None = None,
    key: str | None = None,
    since: float | None = None,
    until: float | None = None,
    q: str | None = None,
    local: str | None = None,
    harness: str | None = None,
    settings: Settings = Depends(get_settings),
):
    """Cheap heartbeat for auto-refresh: row count and latest timestamp only.

    Polling this instead of ``/admin/api/requests/stats`` lets an idle
    dashboard detect "nothing changed" without paying for percentiles,
    breakdowns, or series buckets on every tick.
    """
    require_loopback_admin(request)
    store = _request_log_store_or_none(settings)
    if store is None:
        return {"enabled": False}
    _validate_request_log_status(status)
    _validate_request_log_local(local)
    result = await asyncio.to_thread(
        store.pulse,
        provider=provider,
        model=model,
        status=status,
        endpoint=endpoint,
        key=key,
        since=since,
        until=until,
        q=q,
        local=local,
        harness=harness,
    )
    result["enabled"] = True
    return result


@router.get("/admin/api/requests/harness-usage")
async def request_log_harness_usage(
    request: Request,
    days: int = 7,
    settings: Settings = Depends(get_settings),
):
    """Which coding agents have been talking to this proxy lately.

    Declared above ``/admin/api/requests/{request_id}``, and it has to stay
    there: FastAPI matches routes in declaration order, so the path-parameter
    route would otherwise swallow this one and answer 404 for a request id of
    "harness-usage".

    A disabled store answers ``enabled: False`` with an empty body rather than
    a 404 or a 500, like its siblings -- the card is an optional read, and a
    dashboard that has request logging switched off should render it empty
    instead of showing an error the user cannot act on.
    """
    require_loopback_admin(request)
    window_days = max(1, min(int(days), 90))
    store = _request_log_store_or_none(settings)
    if store is None:
        return {"enabled": False, "days": window_days, "counts": {}, "labels": {}}
    since = time.time() - window_days * 86_400
    counts = await asyncio.to_thread(store.harness_usage, since=since)
    return {
        "enabled": True,
        "days": window_days,
        "counts": counts,
        "labels": _harness_labels(counts),
    }


@router.get("/admin/api/requests/{request_id}")
async def get_request_log_entry(
    request_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Return one request log row with full (uncapped) bodies."""
    require_loopback_admin(request)
    store = _request_log_store_or_none(settings)
    row = (
        await asyncio.to_thread(store.get_request, request_id)
        if store is not None
        else None
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Request log entry not found")
    return row


@router.delete("/admin/api/requests")
async def clear_request_log(
    request: Request,
    confirm: str | None = None,
    settings: Settings = Depends(get_settings),
):
    """Delete every persisted request log row. Irreversible; see the guard."""
    require_loopback_admin(request)
    require_destructive_admin_confirmation(
        request, confirm, REQUEST_LOG_CLEAR_CONFIRMATION
    )
    store = _request_log_store_or_none(settings)
    cleared = await asyncio.to_thread(store.clear) if store is not None else 0
    return {"cleared": cleared}


@router.get("/admin/api/version")
async def read_version(request: Request):
    """Running version plus the latest published release, if reachable."""
    require_loopback_admin(request)
    status = await get_release_status()
    return status.as_dict()


@router.post("/admin/api/version/check")
async def check_version(request: Request):
    """Re-query the release feed, bypassing the cached result."""
    require_loopback_admin(request)
    status = await get_release_status(force=True)
    return status.as_dict()


@router.post("/admin/api/version/upgrade")
async def upgrade_version(
    request: Request,
    background_tasks: BackgroundTasks,
    services: ApiServices = Depends(get_services),
):
    """Install the latest release, then restart into the new process.

    The response is committed before the background restart request runs, so
    the dashboard can enter its reconnect state instead of losing the request
    which initiated the update.
    """
    require_loopback_admin(request)
    result = await perform_upgrade()
    payload = result.as_dict()
    payload["restart_required"] = result.ok
    payload["automatic_restart"] = result.ok
    if result.ok:
        background_tasks.add_task(services.admin.request_process_restart)
    return payload
