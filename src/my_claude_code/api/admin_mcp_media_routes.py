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

7.100.0: the card also lists the Desktop apps whose *own* file can carry the
server (``desktop_files``) and what each holds now. Those are written from
each app's card under Desktop apps, never from here, so this setting and this
route never touch a user's file.
"""

import asyncio
import contextlib
import json
import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from my_claude_code.config.desktop_apply import DesktopApplyError
from my_claude_code.config.desktop_apply import apply as apply_desktop
from my_claude_code.config.desktop_apply import probe as probe_desktop
from my_claude_code.config.desktop_apps import (
    DESKTOP_APPS,
    DesktopAppState,
    DesktopAppStatus,
    desktop_app,
)
from my_claude_code.config.mcp_media import (
    MCP_MEDIA_APP_TABLE,
    MCP_MEDIA_SERVER_NAME,
    MCP_PERMANENT_TARGETS,
    McpMediaApp,
    McpMediaShape,
    McpMediaSupport,
    PermanentMcpTargetSpec,
    mcp_media_app,
    mcp_media_command,
    parse_mcp_media_apps,
    permanent_mcp_target,
    server_arguments,
    with_app,
)
from my_claude_code.config.mcp_permanent_apply import (
    PermanentMcpError,
    PermanentMcpProbe,
    PermanentMcpState,
    apply_permanent_mcp,
    permanent_entry_for,
    plan_permanent_mcp,
    probe_permanent_mcp,
)
from my_claude_code.config.paths import (
    mcp_media_claude_config_path,
    mcp_media_output_dir,
)
from my_claude_code.config.restore_record import UndoMode
from my_claude_code.config.settings import Settings
from my_claude_code.mcp_media.tools import derive_tools, tool_list_bytes

from .admin_desktop_app_routes import _documents, _mcp_state
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


class PermanentMcpActionPayload(BaseModel):
    """Preview or apply a permanent MCP entry in a user's own config file."""

    add: bool
    mode: str = Field(default=UndoMode.KEYS_ONLY.value, pattern="^(keys_only|restore)$")


class McpModePayload(BaseModel):
    """Switch one app between ``off``, ``launch``, and ``permanent`` MCP modes."""

    app: str
    mode: Literal["off", "launch", "permanent"]
    undo_mode: str = Field(
        default=UndoMode.KEYS_ONLY.value, pattern="^(keys_only|restore)$"
    )


_MCC_MCP_MISSING = (
    "mcc-mcp is not installed beside this MCC, so there is nothing to "
    "add. Reinstall or update MCC, then try again."
)


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


def _desktop_files() -> list[dict[str, Any]]:
    """Each Desktop apps card that can put the server in the user's own file (7.100.0)."""

    rows: list[dict[str, Any]] = []
    for spec in DESKTOP_APPS:
        if spec.mcp is None or spec.status is not DesktopAppStatus.SERVABLE:
            continue
        state = _mcp_state(spec)
        rows.append(
            {
                "app_id": spec.id,
                "display_name": spec.display_name,
                "display_path": spec.mcp.display_path,
                "fallback": spec.mcp.fallback,
                "state": state["state"] if state else None,
            }
        )
    return rows


def _permanent_probe_summary(
    spec: PermanentMcpTargetSpec, command: str | None
) -> tuple[PermanentMcpProbe, dict[str, Any]]:
    expected = permanent_entry_for(spec, env=os.environ, command=command)
    probe = probe_permanent_mcp(spec, env=os.environ, expected_entry=expected)
    state_str = (
        "absent" if probe.state is PermanentMcpState.UNSUPPORTED else probe.state.value
    )
    summary = {
        "target_id": spec.target_id,
        "display_name": spec.display_name,
        "supported": spec.permanent_supported,
        "state": state_str,
        "display_path": spec.display_path,
        "document_path": probe.document_path,
        "document_exists": probe.document_exists,
        "key_label": probe.key_label,
        "backup_path": probe.backup_path,
        "backup_exists": probe.backup_exists,
        "restorable": probe.restorable,
        "shared_with": list(spec.shared_with),
        "desktop_app_id": spec.desktop_app_id,
        "asks": spec.asks,
        "note": spec.note,
        "reason": probe.reason or spec.unsupported_reason or spec.note,
        "managed_by": probe.managed_by,
        "error": probe.error,
    }
    return probe, summary


def _mode_and_warning(
    launch_on: bool, permanent_supported: bool, perm_state: str
) -> tuple[str, str]:
    perm_active = permanent_supported and perm_state in (
        "configured",
        "drifted",
        "foreign",
    )
    if launch_on and perm_active:
        mode = "both"
    elif launch_on:
        mode = "launch"
    elif perm_active:
        mode = "permanent"
    else:
        mode = "off"

    if mode == "both":
        warning = (
            "Both Launch-only and Permanent MCP are active for this agent; "
            "switch to one mode to avoid duplicate configuration."
        )
    elif perm_state == "foreign":
        warning = (
            "Your config file already defines an mcc_media entry that MCC did "
            "not add (or that was modified). MCC will not overwrite or remove it."
        )
    else:
        warning = ""
    return mode, warning


def _app_row(
    app: McpMediaApp,
    *,
    launch_on: bool,
    desktop_state: str | None,
    perm_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    launch_supported = app.support is McpMediaSupport.BUTTON
    perm_supported = bool(perm_summary and perm_summary["supported"])
    perm_state = perm_summary["state"] if perm_summary else "absent"
    mode, conflict_warning = _mode_and_warning(launch_on, perm_supported, perm_state)
    supported_modes = ["off"]
    if launch_supported:
        supported_modes.append("launch")
    if perm_supported:
        supported_modes.append("permanent")
    return {
        "app_id": app.app_id,
        "display_name": app.display_name,
        "support": app.support.value,
        "on": launch_on,
        "asks": app.asks,
        "note": app.note,
        "pending": _pending(app, launch_on, desktop_state),
        "mode": mode,
        "launch_supported": launch_supported,
        "launch_on": launch_on,
        "permanent_supported": perm_supported,
        "permanent_target_id": app.app_id if perm_supported else "",
        "permanent_state": perm_state,
        "permanent_path": perm_summary["document_path"] if perm_summary else "",
        "permanent_display_path": perm_summary["display_path"] if perm_summary else "",
        "permanent_backup_exists": (
            perm_summary["backup_exists"] if perm_summary else False
        ),
        "permanent_backup_path": (perm_summary["backup_path"] if perm_summary else ""),
        "permanent_shared_with": (perm_summary["shared_with"] if perm_summary else []),
        "permanent_reason": perm_summary["reason"] if perm_summary else app.note,
        "restorable": perm_summary["restorable"] if perm_summary else False,
        "conflict_warning": conflict_warning,
        "supported_modes": supported_modes,
    }


def _payload(services: ApiServices, settings: Settings, learned: Any) -> dict[str, Any]:
    command = mcp_media_command()
    on = set(parse_mcp_media_apps(settings.mcp_media_apps))
    desktop_state = _desktop_state(services, settings)
    perm_summaries: dict[str, dict[str, Any]] = {}
    permanent_targets: list[dict[str, Any]] = []
    for target in MCP_PERMANENT_TARGETS:
        _probe, summary = _permanent_probe_summary(target, command)
        perm_summaries[target.target_id] = summary
        permanent_targets.append(summary)

    apps = [
        _app_row(
            app,
            launch_on=app.app_id in on,
            desktop_state=desktop_state,
            perm_summary=perm_summaries.get(app.app_id),
        )
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
        "permanent_targets": permanent_targets,
        **tools,
        "claude_desktop_state": desktop_state,
        "claude_desktop_out_dir": str(mcp_media_output_dir()),
        # The apps whose own file can carry the server, and what each holds.
        "desktop_files": _desktop_files(),
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


def _resolve_permanent(target_id: str) -> PermanentMcpTargetSpec:
    spec = permanent_mcp_target(target_id.strip().lower())
    if spec is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown permanent MCP target: {target_id}"
        )
    return spec


async def _refresh_catalogues_if_needed(
    services: ApiServices, target_ids: set[str]
) -> None:
    if target_ids & {
        "codex",
        "codex_desktop",
        "opencode",
        "opencode2",
        "opencode_desktop",
    }:
        with contextlib.suppress(Exception):
            await services.admin.reload_providers(
                reason="permanent mcp updated",
                sweep=False,
                rebuild_provider_ids=frozenset(),
            )


async def _clear_launch_apps(services: ApiServices, app_ids: tuple[str, ...]) -> bool:
    current = services.requests.current_settings()
    active = set(parse_mcp_media_apps(current.mcp_media_apps))
    to_clear = [app_id for app_id in app_ids if app_id in active]
    if not to_clear:
        return False

    def updater(cfg: Settings) -> dict[str, str]:
        value = cfg.mcp_media_apps
        for item in to_clear:
            value = with_app(value, item, on=False)
        return {"MCP_MEDIA_APPS": value}

    result = await services.admin.apply_admin_config_with(updater)
    if not result.get("applied"):
        raise HTTPException(
            status_code=409,
            detail="; ".join(str(e) for e in result.get("errors") or [])
            or "The setting was not saved.",
        )
    if "claude" in to_clear:
        await asyncio.to_thread(_forget_claude_config)
    if _CLAUDE_DESKTOP in to_clear:
        settings = services.requests.current_settings()
        desktop_before = await asyncio.to_thread(_desktop_state, services, settings)
        if desktop_before == DesktopAppState.CONFIGURED.value:
            with contextlib.suppress(DesktopApplyError):
                await asyncio.to_thread(_rewrite_desktop, services, settings)
    return True


@router.post("/admin/api/mcp-media/permanent/{target_id}/plan")
async def plan_permanent_mcp_route(
    target_id: str,
    payload: PermanentMcpActionPayload,
    request: Request,
):
    """Return the unified diff for adding or removing a permanent MCP entry."""

    require_loopback_admin(request)
    spec = _resolve_permanent(target_id)
    if payload.add and mcp_media_command() is None:
        raise HTTPException(status_code=409, detail=_MCC_MCP_MISSING)

    def run() -> dict[str, Any]:
        plan = plan_permanent_mcp(
            spec.target_id,
            add=payload.add,
            mode=UndoMode(payload.mode),
            env=os.environ,
        )
        return {
            "target_id": plan.target_id,
            "add": plan.add,
            "mode": plan.mode.value,
            "document_path": plan.document_path,
            "backup_path": plan.backup_path,
            "diff": plan.diff,
            "will_create_file": plan.will_create_file,
            "removes_file": plan.removes_file,
            "backup_exists": plan.backup_exists,
            "overwrites": list(plan.overwrites),
            "restorable": plan.restorable,
            "no_op": plan.no_op,
            "ask_keys": list(plan.ask_keys),
        }

    try:
        return await asyncio.to_thread(run)
    except PermanentMcpError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/admin/api/mcp-media/permanent/{target_id}/apply")
async def apply_permanent_mcp_route(
    target_id: str,
    payload: PermanentMcpActionPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Write or remove a permanent MCP entry atomically and refresh state."""

    require_loopback_admin(request)
    spec = _resolve_permanent(target_id)
    if payload.add and mcp_media_command() is None:
        raise HTTPException(status_code=409, detail=_MCC_MCP_MISSING)

    try:
        result = await asyncio.to_thread(
            apply_permanent_mcp,
            spec.target_id,
            add=payload.add,
            mode=UndoMode(payload.mode),
            env=os.environ,
        )
    except PermanentMcpError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    cleared_launch = False
    if payload.add:
        related = (spec.target_id, *spec.shared_with)
        cleared_launch = await _clear_launch_apps(services, related)

    await _refresh_catalogues_if_needed(
        services, {spec.target_id, *spec.shared_with, spec.desktop_app_id}
    )

    settings = services.requests.current_settings()
    learned = services.admin.learned_facts_by_model()
    body = await asyncio.to_thread(_payload, services, settings, learned)
    if result.changed:
        verb = (
            f"Added permanent MCP entry to {result.document_path}."
            if payload.add
            else f"Removed permanent MCP entry from {result.document_path}."
        )
        if result.backup_path and payload.add:
            verb += f" Backup at {result.backup_path}."
        if cleared_launch:
            verb += " Cleared Launch-only mode to prevent duplicate injection."
    else:
        verb = (
            f"Already configured in {result.document_path}."
            if payload.add
            else f"Nothing of MCC's was in {result.document_path}."
        )
    body.update(
        {
            "changed": result.changed,
            "add": result.added,
            "mode": result.mode.value,
            "document_path": result.document_path,
            "backup_path": result.backup_path,
            "created_file": result.created_file,
            "removed_file": result.removed_file,
            "message": verb,
        }
    )
    return body


@router.post("/admin/api/mcp-media/mode")
async def set_mcp_media_mode(
    payload: McpModePayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Enforce clean 3-state mutual exclusion between off, launch, and permanent."""

    require_loopback_admin(request)
    app_id = payload.app.strip().lower()
    app = mcp_media_app(app_id)
    perm_spec = permanent_mcp_target(app_id)
    if app is None and perm_spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown MCP app: {payload.app}")

    launch_supported = app is not None and app.support is McpMediaSupport.BUTTON
    perm_supported = perm_spec is not None and perm_spec.permanent_supported
    undo_mode = UndoMode(payload.undo_mode)

    if payload.mode == "launch" and not launch_supported:
        raise HTTPException(
            status_code=409,
            detail=f"{app_id} does not support Launch-only MCP mode.",
        )
    if payload.mode == "permanent" and not perm_supported:
        reason = (
            perm_spec.unsupported_reason
            if perm_spec is not None and perm_spec.unsupported_reason
            else f"{app_id} does not support Permanent MCP mode."
        )
        raise HTTPException(status_code=409, detail=reason)

    command = mcp_media_command()
    if payload.mode in ("launch", "permanent") and command is None:
        raise HTTPException(status_code=409, detail=_MCC_MCP_MISSING)

    perm_probe: PermanentMcpProbe | None = None
    if perm_spec is not None and perm_supported:
        expected = permanent_entry_for(perm_spec, env=os.environ, command=command)
        perm_probe = await asyncio.to_thread(
            probe_permanent_mcp,
            perm_spec,
            env=os.environ,
            expected_entry=expected,
        )
        if perm_probe.state in (
            PermanentMcpState.FOREIGN,
            PermanentMcpState.UNREADABLE,
            PermanentMcpState.MANAGED,
        ):
            detail = (
                perm_probe.reason
                or perm_probe.error
                or (
                    f"{perm_probe.document_path} already has a user-authored "
                    f"{MCP_MEDIA_SERVER_NAME} entry that MCC will not overwrite or remove."
                )
            )
            raise HTTPException(status_code=409, detail=detail)

    changed = False
    doc_path = perm_probe.document_path if perm_probe else ""
    backup_path = perm_probe.backup_path if perm_probe else ""
    message = ""

    if payload.mode == "off":
        if (
            perm_spec is not None
            and perm_supported
            and perm_probe is not None
            and perm_probe.state
            in (PermanentMcpState.CONFIGURED, PermanentMcpState.DRIFTED)
        ):
            try:
                res = await asyncio.to_thread(
                    apply_permanent_mcp,
                    perm_spec.target_id,
                    add=False,
                    mode=undo_mode,
                    env=os.environ,
                )
            except PermanentMcpError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            changed = changed or res.changed
            doc_path = res.document_path
            backup_path = res.backup_path
        related = (
            (app_id, *perm_spec.shared_with) if perm_spec is not None else (app_id,)
        )
        if await _clear_launch_apps(services, related):
            changed = True
        display = (
            app.display_name
            if app is not None
            else perm_spec.display_name
            if perm_spec is not None
            else app_id
        )
        message = (
            f"MCP set to Off for {display}."
            if changed
            else f"MCP is already Off for {display}."
        )
    elif payload.mode == "launch":
        assert app is not None
        if (
            perm_spec is not None
            and perm_supported
            and perm_probe is not None
            and perm_probe.state
            in (PermanentMcpState.CONFIGURED, PermanentMcpState.DRIFTED)
        ):
            try:
                res = await asyncio.to_thread(
                    apply_permanent_mcp,
                    perm_spec.target_id,
                    add=False,
                    mode=undo_mode,
                    env=os.environ,
                )
            except PermanentMcpError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            changed = changed or res.changed
            doc_path = res.document_path
            backup_path = res.backup_path
        current = services.requests.current_settings()
        if app.app_id not in parse_mcp_media_apps(current.mcp_media_apps):
            desktop_before = (
                await asyncio.to_thread(_desktop_state, services, current)
                if app.app_id == _CLAUDE_DESKTOP
                else None
            )
            applied = await services.admin.apply_admin_config_with(
                lambda cfg: {
                    "MCP_MEDIA_APPS": with_app(cfg.mcp_media_apps, app.app_id, on=True)
                }
            )
            if not applied.get("applied"):
                raise HTTPException(
                    status_code=409,
                    detail="; ".join(str(e) for e in applied.get("errors") or [])
                    or "The setting was not saved.",
                )
            changed = True
            if (
                app.app_id == _CLAUDE_DESKTOP
                and desktop_before == DesktopAppState.CONFIGURED.value
            ):
                with contextlib.suppress(DesktopApplyError):
                    await asyncio.to_thread(
                        _rewrite_desktop,
                        services,
                        services.requests.current_settings(),
                    )
        message = f"MCP set to Launch-only for {app.display_name}. {app.note}".strip()
    else:
        assert perm_spec is not None
        try:
            res = await asyncio.to_thread(
                apply_permanent_mcp,
                perm_spec.target_id,
                add=True,
                mode=undo_mode,
                env=os.environ,
            )
        except PermanentMcpError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        changed = changed or res.changed
        doc_path = res.document_path
        backup_path = res.backup_path
        related = (perm_spec.target_id, *perm_spec.shared_with)
        if await _clear_launch_apps(services, related):
            changed = True
        message = f"MCP set to Permanent for {perm_spec.display_name} ({doc_path})."
        if backup_path:
            message += f" Backup at {backup_path}."

    refresh_ids = {app_id}
    if perm_spec is not None:
        refresh_ids.update(perm_spec.shared_with)
        if perm_spec.desktop_app_id:
            refresh_ids.add(perm_spec.desktop_app_id)
    await _refresh_catalogues_if_needed(services, refresh_ids)

    settings = services.requests.current_settings()
    learned = services.admin.learned_facts_by_model()
    body = await asyncio.to_thread(_payload, services, settings, learned)
    body.update(
        {
            "app": app_id,
            "mode": payload.mode,
            "changed": changed,
            "document_path": doc_path,
            "backup_path": backup_path,
            "message": message,
        }
    )
    return body
