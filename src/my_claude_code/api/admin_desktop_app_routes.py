"""Admin API for the Desktop apps group on the Coding agents page.

Six routes, matching how the cards work:

``GET  /admin/api/desktop-apps``                every spec plus its probe state
``POST /admin/api/desktop-apps/{id}/plan``      what Configure would write
``POST /admin/api/desktop-apps/{id}/configure`` write it
``POST /admin/api/desktop-apps/{id}/undo``      take it back out, in one of two modes
``POST /admin/api/desktop-apps/{id}/mcp/plan``  what *Add* / *Remove MCP server*
                                                would write (7.100.0)
``POST /admin/api/desktop-apps/{id}/mcp``       write it: ``{"add": bool}``

The last two are the second key a card owns: MCC's media MCP server entry in
the user's own file (decision 11 of 2026-10-18). Undo takes it out with the
rest of MCC's keys.

Plan and configure are separate so a card can show a real diff of a real
document before anything is written, and configure re-plans server-side rather
than trusting the diff the browser sends back -- the same two-stage the Claude
config editor uses, and for the same reason: the browser's copy is a rendering,
not an authority, and a plan the user looked at for a minute must not clobber a
change made in the meantime.

**What these routes will not do.** They accept an app *id* from the registry
and never a path, so there is no request that writes JSON anywhere on the box.
They never launch an application -- the card shows the open command and a human
runs it. And they never set a user-scope environment variable: that is the one
edit here that would leave both the app's file and MCC's own directory and
could not be undone by deleting a key, so the card says what to export and the
next status poll reports whether it took.

Every route is loopback-and-local-Origin only, through the same
``require_loopback_admin`` the rest of the admin API uses.
"""

import asyncio
import os
import sys
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from my_claude_code.application.catalogue_model import build_catalogue_models
from my_claude_code.application.desktop_documents import (
    base_url_for,
    overwritten_scalars,
    owned_block,
    sidecar_document,
    token_reference,
    writes_literal_credential,
)
from my_claude_code.config.desktop_apply import (
    DesktopApplyError,
    DesktopMcpPlan,
    DesktopMcpProbe,
    DesktopPlan,
    DesktopProbe,
    apply_mcp,
    plan_mcp,
    probe_mcp,
    sidecar_path_for,
)
from my_claude_code.config.desktop_apply import (
    apply as apply_desktop,
)
from my_claude_code.config.desktop_apply import (
    plan as plan_desktop,
)
from my_claude_code.config.desktop_apply import (
    probe as probe_desktop,
)
from my_claude_code.config.desktop_apply import (
    undo as undo_desktop,
)
from my_claude_code.config.desktop_apps import (
    DESKTOP_APPS,
    DesktopAppSpec,
    DesktopAppStatus,
    desktop_app,
)
from my_claude_code.config.harness_tiers import current_harness_tiers
from my_claude_code.config.mcp_media import (
    desktop_file_entry,
    desktop_mcp_server,
    mcp_media_command,
    mcp_media_on,
)
from my_claude_code.config.paths import mcp_media_output_dir
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.restore_record import UndoMode
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import Settings

from .admin_routes import require_loopback_admin
from .dependencies import get_services, get_settings
from .ports import ApiServices

router = APIRouter()


class DesktopUndoPayload(BaseModel):
    """Which of the two undo modes the card's picker asked for."""

    mode: str = Field(default=UndoMode.KEYS_ONLY.value, pattern="^(keys_only|restore)$")


class DesktopMcpPayload(BaseModel):
    """The card's *Add MCP server* (``add: true``) or *Remove MCP server*."""

    add: bool


class DesktopConfigurePayload(BaseModel):
    """Configure takes no choices.

    It carried one field until 7.6.6, ``set_default_model``, behind a checkbox
    that rendered on every servable card except Codex -- and Codex is the only
    spec whose document declares a ``model`` key, which it writes
    unconditionally. The field could not change a byte on any card that offered
    it.

    The model is kept, rather than the routes losing their body, because
    pydantic ignores unknown fields by default: an older ``mcc-apps`` or a
    script still posting ``{"set_default_model": true}`` keeps working and is
    simply not offered a choice it never had.
    """


def _resolve(app_id: str) -> DesktopAppSpec:
    try:
        return desktop_app(app_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _require_servable(spec: DesktopAppSpec) -> None:
    """Refuse a write to a card that is not a Configure button.

    An INSTRUCTIONS_ONLY card is values a human types into a dialog and a
    NOT_ROUTABLE one is a dated explanation. Neither has a document, so a
    request to write one is a bug in the caller rather than a state to render.
    """

    if spec.status is not DesktopAppStatus.SERVABLE or spec.document is None:
        raise HTTPException(
            status_code=409,
            detail=f"{spec.display_name} is not configured by writing a file",
        )


def _sidecar_path(spec: DesktopAppSpec) -> str:
    """Return where the file MCC owns outright lands on this machine.

    An app whose hook into its own settings is "read my configuration from
    over there" needs the *resolved* path written into that hook, and the
    registry cannot know it: it is MCC's configuration directory on this box.
    Roo Code is the case, and until 6.83.0 nothing supplied it -- the scalar
    was skipped with a comment saying the caller would fill it in, and no
    caller did, so Configure wrote neither the key nor the file it names.
    """

    if spec.sidecar is None or not spec.sidecar_path_key:
        return ""
    resolved = sidecar_path_for(spec, os.environ)
    return str(resolved) if resolved is not None else ""


def _documents(
    spec: DesktopAppSpec,
    services: ApiServices,
    settings: Settings,
) -> tuple[dict[str, Any] | None, dict[str, object], dict[str, Any] | None]:
    """Return the block, the overwritten scalars and the sidecar for one app.

    The models are resolved through the same ladder and the same per-harness
    tier overrides the launchers read, so a desktop card and the CLI it shares
    a catalogue with cannot end up offering different models.
    """

    runtime = services.requests
    harness_tiers = current_harness_tiers()
    models = build_catalogue_models(
        settings,
        runtime,
        harness_id=spec.id,
        harness_tiers=harness_tiers,
    )
    proxy_root_url = local_proxy_root_url(settings)
    # The literal credential, resolved once and handed only to the two
    # functions that may write it: the sidecar builder, for a file MCC owns
    # outright, and the block builder, for an app that resolves no usable
    # reference and takes it in its own document. It is the same token
    # Configure Claude Code writes, so a user who runs both is not handed two
    # different answers to "what is MCC's key".
    token = proxy_auth_token(settings.anthropic_auth_token)
    return (
        owned_block(
            spec,
            models,
            proxy_root_url=proxy_root_url,
            auth_token=token if writes_literal_credential(spec) else "",
        ),
        overwritten_scalars(spec, sidecar_path=_sidecar_path(spec)),
        sidecar_document(
            spec,
            models,
            proxy_root_url=proxy_root_url,
            auth_token=(
                token
                if spec.sidecar is not None and spec.sidecar.holds_credential
                else ""
            ),
            mcp_server=desktop_mcp_server(
                spec.id, apps_value=settings.mcp_media_apps, environ=os.environ
            ),
        ),
    )


def _mcp_entry(spec: DesktopAppSpec) -> dict[str, Any] | None:
    """What *Add MCP server* writes for this app, or None without ``mcc-mcp``.

    No token and no address: ``mcc-mcp`` reads both from MCC's own settings
    when the app starts it (decision 1). ``MCC_CONFIG_DIR`` is passed on only
    when this server itself runs with one, so a scratch MCC's entry names the
    scratch folder and a user's names nothing.
    """

    if spec.mcp is None:
        return None
    command = mcp_media_command()
    if not command:
        return None
    return desktop_file_entry(
        spec.mcp.shape,
        spec.id,
        command,
        os.environ,
        out_dir=str(mcp_media_output_dir()) if spec.mcp.needs_out_dir else None,
    )


def _mcp_probe_payload(spec: DesktopAppSpec, probe: DesktopMcpProbe) -> dict[str, Any]:
    assert spec.mcp is not None
    return {
        "state": probe.state.value,
        "key": probe.key_label,
        "display_path": spec.mcp.display_path,
        "document_path": probe.document_path,
        "document_exists": probe.document_exists,
        "backup_path": probe.backup_path,
        "error": probe.error,
        "managed_by": probe.managed_by,
        "fallback": spec.mcp.fallback,
        "installed": mcp_media_command() is not None,
        "asks": spec.mcp.asks,
        "note": spec.mcp.note,
        "ask_map": ".".join(spec.mcp.ask_map_key),
    }


def _mcp_plan_payload(plan: DesktopMcpPlan) -> dict[str, Any]:
    return {
        "add": plan.add,
        "document_path": plan.document_path,
        "diff": plan.diff,
        "no_op": plan.no_op,
        "creates_file": plan.creates_file,
        "removes_file": plan.removes_file,
        "backup_path": plan.backup_path,
        "backup_exists": plan.backup_exists,
        "ask_keys": list(plan.ask_keys),
    }


def _mcp_state(spec: DesktopAppSpec) -> dict[str, Any] | None:
    """The card's media server block, probed now; None for an app without one."""

    if spec.mcp is None:
        return None
    try:
        probe = probe_mcp(spec, env=os.environ, expected_entry=_mcp_entry(spec))
    except DesktopApplyError:
        return None
    return _mcp_probe_payload(spec, probe)


def _probe_payload(probe: DesktopProbe) -> dict[str, Any]:
    return {
        "state": probe.state.value,
        "document_path": probe.document_path,
        "document_exists": probe.document_exists,
        "error": probe.error,
        "token_env_present": probe.token_env_present,
        "restorable": probe.restorable,
        "managed_by": probe.managed_by,
        "managed_keys": list(probe.managed_keys),
        "repaired": list(probe.repaired),
    }


def _plan_payload(plan: DesktopPlan) -> dict[str, Any]:
    return {
        "document_path": plan.document_path,
        "diff": plan.diff,
        "sidecar_path": plan.sidecar_path,
        "sidecar_diff": plan.sidecar_diff,
        "overwritten_keys": list(plan.overwritten_keys),
        "no_op": plan.no_op,
        "actions": list(plan.actions),
    }


def _spec_payload(
    spec: DesktopAppSpec, settings: Settings, probe: DesktopProbe | None
) -> dict[str, Any]:
    """Return everything a card renders, whether or not it has a button."""

    proxy_root = local_proxy_root_url(settings)
    payload: dict[str, Any] = {
        "id": spec.id,
        "display_name": spec.display_name,
        "summary": spec.summary,
        "status": spec.status.value,
        "doc_url": spec.doc_url,
        "unavailable_reason": spec.unavailable_reason,
        "instructions_reason": spec.instructions_reason,
        "protocol": spec.protocol.value,
        "base_url": base_url_for(spec, proxy_root),
        "token_form": spec.token_form.value,
        "token_reference": token_reference(spec),
        "token_env_var": spec.token_env_var,
        "attribution_header": spec.attribution_header_field,
        "restart_required": spec.restart_required,
        "open_command": spec.open_command,
        "notes": list(spec.notes),
        # ``{root}`` is the registry's placeholder for "this install's proxy
        # root", which the spec cannot know: a card is declared data and the
        # port is a setting. Resolved here, where both are in hand, so the
        # value the reader copies is the one they can paste.
        "instruction_fields": [
            {"label": label, "value": value.replace("{root}", proxy_root)}
            for label, value in spec.instruction_fields
        ],
        "owned_key": spec.owned_key_label,
        "display_path": spec.document.display_path if spec.document else "",
        "sidecar_path": spec.sidecar.display_path if spec.sidecar else "",
        # Key names only. The card says which settings MCC writes into the
        # file it owns; the values include a literal credential and never
        # leave the server.
        "sidecar_keys": (
            [
                *spec.sidecar.fields,
                *([spec.sidecar.headers_key] if spec.sidecar.headers_key else []),
                # 7.99.0: named only while this app's Add MCP server is on.
                *(
                    [spec.sidecar.mcp_servers_key]
                    if spec.sidecar.mcp_servers_key
                    and mcp_media_on(settings.mcp_media_apps, spec.id)
                    else []
                ),
            ]
            if spec.sidecar is not None
            else []
        ),
        # Only the sources that can apply *here*. Listing the macOS managed
        # preferences path on a Windows card is noise the reader has to filter
        # out, and it makes the row read as a list of things that are true
        # rather than as a list of things that could outrank MCC on this box.
        "managed_source_labels": [
            source.label
            for source in spec.managed_sources
            if source.path is None
            or not source.path.platforms
            or sys.platform in source.path.platforms
        ],
        "overwrites": [
            ".".join(key_path)
            for key_path in (spec.document.overwritten_keys if spec.document else ())
        ],
        "sets_default_model": spec.sets_default_model,
    }
    if probe is not None:
        payload["probe"] = _probe_payload(probe)
    # 7.100.0: MCC's media server in this app's own file -- its own state,
    # apart from the inference badge above -- or the dated reason there is
    # no button for it yet.
    if spec.status is DesktopAppStatus.SERVABLE:
        mcp = _mcp_state(spec)
        if mcp is not None:
            payload["mcp"] = mcp
    if spec.mcp_note:
        payload["mcp_note"] = spec.mcp_note
    return payload


def _list_payload(settings: Settings, services: ApiServices) -> dict[str, Any]:
    """Return every card, each probed against what a re-apply *would* write.

    The expected block has to be resolved here, not just on Configure: this is
    the route the page polls, and ``drifted`` is by definition a comparison
    against what MCC would write now. Probing without it could only ever
    answer present-or-absent, so a hand-edited base URL would keep reporting
    ``configured`` -- the one state the drift badge exists to contradict.

    The shared model list is built once and reused, and rebuilt per app only
    where that app has its own tier overrides. That is the same economy
    ``_catalogue_models_payload`` makes for the launchers, and for the same
    reason: the ladder walk is the expensive part and almost nobody overrides.
    """

    runtime = services.requests
    harness_tiers = current_harness_tiers()
    shared = build_catalogue_models(settings, runtime, harness_tiers=harness_tiers)
    proxy_root_url = local_proxy_root_url(settings)

    apps: list[dict[str, Any]] = []
    for spec in DESKTOP_APPS:
        expected_block: dict[str, Any] | None = None
        expected_scalars: dict[str, object] | None = None
        expected_sidecar: dict[str, Any] | None = None
        if spec.status is DesktopAppStatus.SERVABLE and spec.document is not None:
            models = (
                build_catalogue_models(
                    settings,
                    runtime,
                    harness_id=spec.id,
                    harness_tiers=harness_tiers,
                )
                if harness_tiers.for_harness(spec.id)
                else shared
            )
            token = proxy_auth_token(settings.anthropic_auth_token)
            expected_block = owned_block(
                spec,
                models,
                proxy_root_url=proxy_root_url,
                auth_token=token if writes_literal_credential(spec) else "",
            )
            expected_scalars = overwritten_scalars(
                spec, sidecar_path=_sidecar_path(spec)
            )
            expected_sidecar = sidecar_document(
                spec,
                models,
                proxy_root_url=proxy_root_url,
                auth_token=(
                    token
                    if spec.sidecar is not None and spec.sidecar.holds_credential
                    else ""
                ),
                mcp_server=desktop_mcp_server(
                    spec.id, apps_value=settings.mcp_media_apps, environ=os.environ
                ),
            )
        try:
            probe = probe_desktop(
                spec,
                env=os.environ,
                expected_block=expected_block,
                expected_scalars=expected_scalars,
                expected_sidecar=expected_sidecar,
            )
        except DesktopApplyError:
            probe = None
        apps.append(_spec_payload(spec, settings, probe))
    return {"apps": apps}


@router.get("/admin/api/desktop-apps")
async def list_desktop_apps(
    request: Request,
    settings: Settings = Depends(get_settings),
    services: ApiServices = Depends(get_services),
):
    """Return every desktop app MCC knows about and its state on this machine."""

    require_loopback_admin(request)
    return await asyncio.to_thread(_list_payload, settings, services)


@router.post("/admin/api/desktop-apps/{app_id}/plan")
async def plan_desktop_app(
    app_id: str,
    payload: DesktopConfigurePayload,
    request: Request,
    settings: Settings = Depends(get_settings),
    services: ApiServices = Depends(get_services),
):
    """Return the diff Configure would write. Touches no disk."""

    require_loopback_admin(request)
    spec = _resolve(app_id)
    _require_servable(spec)

    def run() -> dict[str, Any]:
        block, scalars, sidecar = _documents(spec, services, settings)
        plan = plan_desktop(
            spec,
            env=os.environ,
            block=block,
            scalars=scalars,
            sidecar_document=sidecar,
        )
        return _plan_payload(plan)

    try:
        return await asyncio.to_thread(run)
    except DesktopApplyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/admin/api/desktop-apps/{app_id}/configure")
async def configure_desktop_app(
    app_id: str,
    payload: DesktopConfigurePayload,
    request: Request,
    settings: Settings = Depends(get_settings),
    services: ApiServices = Depends(get_services),
):
    """Write MCC's keys into the app's document and report the new state."""

    require_loopback_admin(request)
    spec = _resolve(app_id)
    _require_servable(spec)

    def run() -> dict[str, Any]:
        block, scalars, sidecar = _documents(spec, services, settings)
        result = apply_desktop(
            spec,
            env=os.environ,
            block=block,
            scalars=scalars,
            sidecar_document=sidecar,
        )
        probe = probe_desktop(
            spec,
            env=os.environ,
            expected_block=block,
            expected_scalars=scalars,
            expected_sidecar=sidecar,
        )
        return {
            "changed": result.changed,
            "document_path": result.document_path,
            "backup_path": result.backup_path,
            "sidecar_path": result.sidecar_path,
            "probe": _probe_payload(probe),
        }

    try:
        return await asyncio.to_thread(run)
    except DesktopApplyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/admin/api/desktop-apps/{app_id}/undo")
async def undo_desktop_app(
    app_id: str,
    payload: DesktopUndoPayload,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Remove MCC's keys, and on request put back the values MCC replaced."""

    require_loopback_admin(request)
    spec = _resolve(app_id)
    _require_servable(spec)
    mode = UndoMode(payload.mode)

    def run() -> dict[str, Any]:
        result = undo_desktop(spec, env=os.environ, mode=mode)
        probe = probe_desktop(spec, env=os.environ)
        return {
            "changed": result.changed,
            "mode": mode.value,
            "document_path": result.document_path,
            "removed_sidecar": result.removed_sidecar,
            "restored_keys": list(result.restored_keys),
            # 7.100.0: Undo also takes MCC's media server entry out.
            "removed_mcp_server": result.removed_mcp_server,
            "probe": _probe_payload(probe),
        }

    try:
        return await asyncio.to_thread(run)
    except DesktopApplyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _require_mcp(spec: DesktopAppSpec) -> None:
    if spec.status is not DesktopAppStatus.SERVABLE or spec.mcp is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"MCC does not add its media server to {spec.display_name}'s "
                "own files." + (f" {spec.mcp_note}" if spec.mcp_note else "")
            ),
        )


@router.post("/admin/api/desktop-apps/{app_id}/mcp/plan")
async def plan_desktop_app_mcp(
    app_id: str,
    payload: DesktopMcpPayload,
    request: Request,
):
    """Return the diff *Add* or *Remove MCP server* would write. Touches no disk.

    A file that already carries an ``mcc_media`` server MCC did not write is
    answered 409 with the reason, which the card shows in place of a diff.
    """

    require_loopback_admin(request)
    spec = _resolve(app_id)
    _require_mcp(spec)

    def run() -> dict[str, Any]:
        entry = _mcp_entry(spec) if payload.add else None
        if payload.add and entry is None:
            raise DesktopApplyError(_MCC_MCP_MISSING)
        plan = plan_mcp(spec, env=os.environ, add=payload.add, entry=entry)
        return _mcp_plan_payload(plan)

    try:
        return await asyncio.to_thread(run)
    except DesktopApplyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


#: Said when Add is pressed on an install without ``mcc-mcp`` beside it.
_MCC_MCP_MISSING = (
    "mcc-mcp is not installed beside this MCC, so there is nothing to add. "
    "Reinstall or update MCC, then try again."
)


@router.post("/admin/api/desktop-apps/{app_id}/mcp")
async def set_desktop_app_mcp(
    app_id: str,
    payload: DesktopMcpPayload,
    request: Request,
):
    """Add MCC's media server to the app's own file, or take it out again.

    Re-planned here rather than taken from the browser, as Configure is: the
    file may have changed while the plan was on screen. The first write
    takes the one-time backup beside the file.
    """

    require_loopback_admin(request)
    spec = _resolve(app_id)
    _require_mcp(spec)

    def run() -> dict[str, Any]:
        entry = _mcp_entry(spec) if payload.add else None
        if payload.add and entry is None:
            raise DesktopApplyError(_MCC_MCP_MISSING)
        result = apply_mcp(spec, env=os.environ, add=payload.add, entry=entry)
        return {
            "changed": result.changed,
            "add": result.added,
            "document_path": result.document_path,
            "backup_path": result.backup_path,
            "created_file": result.created_file,
            "removed_file": result.removed_file,
            "mcp": _mcp_state(spec),
        }

    try:
        return await asyncio.to_thread(run)
    except DesktopApplyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
