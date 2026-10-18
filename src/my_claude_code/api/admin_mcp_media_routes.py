"""Admin API for MCC's media MCP server: its card, and each app's button (7.99.0).

``GET  /admin/api/mcp-media``       the MCC media server card: whether ``mcc-mcp``
                                    is installed and where, the tools it would
                                    offer from the rails as they are now, what
                                    that tool list costs per request, a line to
                                    copy into another app, and every app's state
``POST /admin/api/mcp-media/apps``  one card's *Add MCP server* / *Remove MCP
                                    server*: ``{"app": id, "add": bool}``

The button writes one setting, ``MCP_MEDIA_APPS``, inside the config lock
(read-modify-write is one step, so two cards pressed together cannot drop each
other's change). Everything MCC owns follows from it on the same apply: the
generated config layers are republished with or without the server, and the
next ``mcc-claude`` / ``mcc-codex`` launch carries or omits it. Remove puts each
of them back byte for byte; for Claude Code it also deletes MCC's own
``--mcp-config`` file. Claude Desktop's server lives in MCC's own Claude Desktop
document, so when that card is configured the same Configure is re-run to
write it there or take it out -- its restore record keeps the user's original
values, as on every re-apply.

Loopback and local Origin only (``require_loopback_admin``). Nothing here
carries a token.
"""

import asyncio
import json
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from my_claude_code.config.desktop_apply import DesktopApplyError
from my_claude_code.config.desktop_apply import apply as apply_desktop
from my_claude_code.config.desktop_apply import probe as probe_desktop
from my_claude_code.config.desktop_apps import DesktopAppState, desktop_app
from my_claude_code.config.mcp_media import (
    MCP_MEDIA_APP_TABLE,
    MCP_MEDIA_SERVER_NAME,
    McpMediaApp,
    McpMediaShape,
    McpMediaSupport,
    mcp_media_app,
    mcp_media_command,
    parse_mcp_media_apps,
    server_arguments,
    with_app,
)
from my_claude_code.config.paths import (
    mcp_media_claude_config_path,
    mcp_media_output_dir,
)
from my_claude_code.config.settings import Settings
from my_claude_code.mcp_media.tools import derive_tools, tool_list_bytes

from .admin_desktop_app_routes import _documents
from .admin_media_routes import media_request_catalogue, request_schema_inputs
from .admin_routes import require_loopback_admin
from .dependencies import get_services
from .media_catalog_routes import OPERATION_ENDPOINTS
from .ports import ApiServices

router = APIRouter()

#: The desktop app whose MCC-owned document carries the server: the one row
#: of the table with that shape (a test pins that there is exactly one).
_CLAUDE_DESKTOP = next(
    app.app_id
    for app in MCP_MEDIA_APP_TABLE
    if app.shape is McpMediaShape.CLAUDE_DESKTOP_MANAGED
)
#: Placeholder in the line to copy: the reader's own name for their app.
_YOUR_APP = "<your-app>"


class McpMediaAppPayload(BaseModel):
    """One card's button: add the server to this app, or take it out."""

    app: str
    add: bool


def _tools_summary(
    services: ApiServices, settings: Settings, learned: Any
) -> dict[str, Any]:
    """The tool list ``mcc-mcp`` would build from the rails as they are now."""

    catalogue = media_request_catalogue(
        settings, request_schema_inputs(services, learned), OPERATION_ENDPOINTS
    )
    tools = derive_tools(catalogue)
    rails = [
        {
            "rail": rail.get("rail"),
            "label": rail.get("label"),
            "models": list(rail.get("models") or []),
            "tools": [tool.name for tool in tools if tool.rail == rail.get("rail")],
        }
        for rail in catalogue.get("rails") or []
    ]
    return {
        "tools": [tool.name for tool in tools],
        "tool_bytes": tool_list_bytes(tools),
        "rails": rails,
    }


def _desktop_state(services: ApiServices, settings: Settings) -> str | None:
    """Claude Desktop's card state as its probe reads it, or ``None``."""

    spec = desktop_app(_CLAUDE_DESKTOP)
    try:
        block, scalars, sidecar = _documents(spec, services, settings)
        probe = probe_desktop(
            spec,
            env=os.environ,
            expected_block=block,
            expected_scalars=scalars,
            expected_sidecar=sidecar,
        )
    except DesktopApplyError:
        return None
    return probe.state.value


def _pending(app: McpMediaApp, on: bool, desktop_state: str | None) -> str:
    """What still has to happen before an app that is on really has the server."""

    if (
        on
        and app.shape is McpMediaShape.CLAUDE_DESKTOP_MANAGED
        and desktop_state != DesktopAppState.CONFIGURED.value
    ):
        return (
            "Not written yet: MCC writes it into its own Claude Desktop "
            "document, so press Configure on the Claude Desktop card under "
            "Desktop apps, then relaunch Claude Desktop."
        )
    return ""


def _payload(services: ApiServices, settings: Settings, learned: Any) -> dict[str, Any]:
    command = mcp_media_command()
    on = set(parse_mcp_media_apps(settings.mcp_media_apps))
    desktop_state = _desktop_state(services, settings)
    apps = [
        {
            "app_id": app.app_id,
            "display_name": app.display_name,
            "support": app.support.value,
            "on": app.app_id in on,
            "asks": app.asks,
            "note": app.note,
            "pending": _pending(app, app.app_id in on, desktop_state),
        }
        for app in MCP_MEDIA_APP_TABLE
    ]
    try:
        tools: dict[str, Any] = _tools_summary(services, settings, learned)
    except Exception as exc:
        tools = {
            "tools": [],
            "tool_bytes": None,
            "rails": [],
            "tools_error": (
                f"The media catalogue could not be built ({type(exc).__name__})."
            ),
        }
    entry = {
        "command": command or "mcc-mcp",
        "args": server_arguments(_YOUR_APP),
    }
    return {
        "server_name": MCP_MEDIA_SERVER_NAME,
        "command": command,
        "installed": command is not None,
        "apps_value": settings.mcp_media_apps,
        "apps": apps,
        **tools,
        "claude_desktop_state": desktop_state,
        "claude_desktop_out_dir": str(mcp_media_output_dir()),
        # For an app MCC does not launch: the program and its arguments, and
        # the ``mcpServers`` entry most apps' own files take. No token: the
        # server reads it from MCC's settings.
        "copy_line": " ".join(
            [json.dumps(entry["command"]), *server_arguments(_YOUR_APP)]
        ),
        "copy_json": json.dumps(
            {"mcpServers": {MCP_MEDIA_SERVER_NAME: entry}}, indent=2
        ),
    }


@router.get("/admin/api/mcp-media")
async def mcp_media_status(
    request: Request, services: ApiServices = Depends(get_services)
):
    """The MCC media server card, and every app's Add / Remove state."""

    require_loopback_admin(request)
    # The learned facts belong to the event loop; the copy goes to the worker.
    learned = services.admin.learned_facts_by_model()
    settings = services.requests.current_settings()
    return await asyncio.to_thread(_payload, services, settings, learned)


def _forget_claude_config() -> bool:
    path = mcp_media_claude_config_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _rewrite_desktop(services: ApiServices, settings: Settings) -> bool:
    """Re-run Claude Desktop's Configure so MCC's document matches the button."""

    spec = desktop_app(_CLAUDE_DESKTOP)
    block, scalars, sidecar = _documents(spec, services, settings)
    result = apply_desktop(
        spec, env=os.environ, block=block, scalars=scalars, sidecar_document=sidecar
    )
    return result.changed


@router.post("/admin/api/mcp-media/apps")
async def set_mcp_media_app(
    payload: McpMediaAppPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Add the media server to one app, or take it out, and say what changed."""

    require_loopback_admin(request)
    app = mcp_media_app(payload.app.strip().lower())
    if app is None or app.support is not McpMediaSupport.BUTTON:
        raise HTTPException(
            status_code=400,
            detail=f"MCC cannot add its media server to {payload.app} yet.",
        )
    if payload.add and mcp_media_command() is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "mcc-mcp is not installed beside this MCC, so there is nothing to "
                "add. Reinstall or update MCC, then try again."
            ),
        )
    desktop_before = (
        await asyncio.to_thread(
            _desktop_state, services, services.requests.current_settings()
        )
        if app.app_id == _CLAUDE_DESKTOP
        else None
    )
    result = await services.admin.apply_admin_config_with(
        lambda current: {
            "MCP_MEDIA_APPS": with_app(
                current.mcp_media_apps, app.app_id, on=payload.add
            )
        }
    )
    if not result.get("applied"):
        raise HTTPException(
            status_code=409,
            detail="; ".join(str(e) for e in result.get("errors") or [])
            or "The setting was not saved.",
        )
    settings = services.requests.current_settings()
    verb = "Added to" if payload.add else "Removed from"
    message = f"{verb} {app.display_name}. {app.note}".strip()
    if app.shape is McpMediaShape.CLAUDE_MCP_CONFIG and not payload.add:
        # The launch line already lost the flag with the setting; the file it
        # named goes too, so nothing of the server is left in MCC's folder.
        await asyncio.to_thread(_forget_claude_config)
    if app.app_id == _CLAUDE_DESKTOP:
        if desktop_before == DesktopAppState.CONFIGURED.value:
            try:
                await asyncio.to_thread(_rewrite_desktop, services, settings)
            except DesktopApplyError as exc:
                message = f"{verb} {app.display_name}'s setting, but {exc}"
            else:
                message = (
                    f"{verb} MCC's Claude Desktop document. Relaunch Claude "
                    "Desktop once to load the change."
                )
        elif payload.add:
            message = (
                "Claude Desktop will get the media server when MCC writes its "
                "document: press Configure on the Claude Desktop card under "
                "Desktop apps, then relaunch Claude Desktop."
            )
    learned = services.admin.learned_facts_by_model()
    body = await asyncio.to_thread(_payload, services, settings, learned)
    body["message"] = message
    return body
