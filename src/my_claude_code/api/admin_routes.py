"""Local admin UI routes and APIs."""

import asyncio
import ipaddress
import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from my_claude_code.api.docs_content import available_documents
from my_claude_code.api.docs_render import render_document
from my_claude_code.api.optimization_handlers import OPTIMIZATION_RULE_SPECS
from my_claude_code.application.model_metadata import ProviderModelRefreshResult
from my_claude_code.application.release_updates import (
    get_release_status,
    perform_upgrade,
)
from my_claude_code.config.admin.manifest import FIELD_BY_KEY
from my_claude_code.config.admin.persistence import validate_updates
from my_claude_code.config.admin.sources import is_locked_source
from my_claude_code.config.admin.values import load_config_response, load_value_state
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
from my_claude_code.config.model_refs import configured_chat_model_refs
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
from my_claude_code.core.optimization_discovery import (
    DEFAULT_SCAN_ROW_LIMIT,
    MAX_SCAN_ROW_LIMIT,
    discover_families,
)
from my_claude_code.core.request_log import RequestLogStore, store_from_settings
from my_claude_code.providers.anthropic_oauth.credentials import (
    OAuthTokens,
    claude_credentials_path,
    load_claude_code_tokens,
    load_managed_tokens,
)
from my_claude_code.providers.anthropic_oauth.credentials import (
    store_tokens as store_anthropic_oauth_tokens,
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
from my_claude_code.websearch.rotation import mask_key_label, parse_websearch_keys

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


class RtkUpdatePayload(BaseModel):
    """Partial RTK integration update submitted by the admin UI.

    Only these three boolean flags are accepted; anything else is ignored so a
    stray body cannot corrupt the RTK state file.
    """

    claude: bool | None = None
    codex: bool | None = None
    pi: bool | None = None


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
    return {
        "claude": state.claude,
        "codex": state.codex,
        "pi": state.pi,
    }


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

    updates: dict[str, bool] = {}
    for name in ("claude", "codex", "pi"):
        submitted = getattr(payload, name)
        if submitted is not None:
            updates[name] = submitted
    if not updates:
        return await asyncio.to_thread(rtk_status)

    updated = RtkState(
        claude=updates.get("claude", current.claude),
        codex=updates.get("codex", current.codex),
        pi=updates.get("pi", current.pi),
    )
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
    keys = parse_websearch_keys(entry["value"])
    return {
        "provider_id": descriptor.provider_id,
        "env_key": env_key,
        "locked": is_locked_source(entry["source"]),
        "keys": [
            {"index": index, "key_label": mask_key_label(key)}
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
    return list(parse_websearch_keys(entry["value"]))


def _masked_keys(env_key: str) -> list[dict[str, Any]]:
    entry = load_value_state().get(env_key, {"value": "", "source": "default"})
    return [
        {"index": index, "key_label": mask_key_label(key)}
        for index, key in enumerate(parse_websearch_keys(entry["value"]))
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
    configured = {
        ref.model_ref
        for ref in configured_chat_model_refs(services.requests.current_settings())
    }
    infos = services.requests.cached_prefixed_model_infos()
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


class _AnthropicOAuthSourceInfo(BaseModel):
    available: bool
    masked_token: str = ""
    expires_at: int | None = None
    subscription_type: str | None = None


class _AnthropicOAuthSourcesResponse(BaseModel):
    claude_code: _AnthropicOAuthSourceInfo
    mcc: _AnthropicOAuthSourceInfo


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
        raise HTTPException(
            status_code=400,
            detail=f"No Claude Code credential found at {claude_credentials_path()}",
        )
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


# --------------------------------------------------------------------- requests log


def _request_log_store_or_none(
    settings: Settings,
) -> RequestLogStore | None:
    return store_from_settings(settings)


def _validate_request_log_status(status: str | None) -> None:
    if status is not None and status not in {"success", "error", "cancelled"}:
        raise HTTPException(status_code=422, detail="Invalid status filter")


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
    settings: Settings = Depends(get_settings),
):
    """Aggregate request analytics over an optional epoch-second window."""
    require_loopback_admin(request)
    store = _request_log_store_or_none(settings)
    if store is None:
        return {"enabled": False}
    _validate_request_log_status(status)
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
    )
    result["enabled"] = True
    result["capture_bodies"] = bool(settings.request_log_capture_bodies)
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
    )
    result["enabled"] = True
    return result


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
    settings: Settings = Depends(get_settings),
):
    """Delete every persisted request log row."""
    require_loopback_admin(request)
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
