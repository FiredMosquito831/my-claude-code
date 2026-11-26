"""HTTP and WebSocket routes for Remote Control (/rc) and Admin RC controls (/admin/api/rc)."""

import asyncio
import contextlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from my_claude_code.application.remote_ports import (
    RemoteConflictError,
    RemoteControlError,
    RemoteDisabledError,
    RemoteForbiddenError,
    RemoteLimitError,
    RemoteNotFoundError,
    RemoteSessionPort,
)

from .admin_routes import require_loopback_admin
from .remote_auth import RemoteAuthError, RemoteAuthManager

REMOTE_STATIC_DIR = Path(__file__).parent / "remote_static"

_ALLOWED_STATIC_FILES: dict[str, tuple[str, str]] = {
    "remote.css": ("remote.css", "text/css; charset=utf-8"),
    "remote.js": ("remote.js", "application/javascript; charset=utf-8"),
}
_ALLOWED_VENDOR_FILES: dict[str, tuple[str, str]] = {
    "xterm.js": ("vendor/xterm.js", "application/javascript; charset=utf-8"),
    "xterm.css": ("vendor/xterm.css", "text/css; charset=utf-8"),
    "addon-fit.js": ("vendor/addon-fit.js", "application/javascript; charset=utf-8"),
}

_DISABLED_HTML = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>MCC Remote Control Disabled</title>"
    "<style>body{font-family:system-ui,sans-serif;background:#0b1120;color:#e2e8f0;"
    "display:grid;place-items:center;min-height:100vh;margin:0;padding:24px}"
    ".box{max-width:440px;background:#111827;border:1px solid #1e293b;"
    "border-radius:14px;padding:24px}h1{font-size:18px;margin:0 0 10px}"
    "p{color:#94a3b8;line-height:1.5;margin:0}</style></head>"
    "<body><div class='box'><h1>Remote Control is disabled</h1>"
    "<p>Enable <code>RC_ENABLED</code> in the local Admin Dashboard "
    "(&rarr; Remote Control) to use <code>/rc</code>.</p></div></body></html>"
)

remote_router = APIRouter(tags=["remote-control"])
admin_rc_router = APIRouter(
    prefix="/admin/api/rc",
    tags=["admin-remote-control"],
    dependencies=[Depends(require_loopback_admin)],
)


def _auth_manager(request_or_ws: Request | WebSocket) -> RemoteAuthManager:
    app = request_or_ws.app
    manager = getattr(app.state, "rc_auth", None)
    if manager is None:
        manager = RemoteAuthManager()
        app.state.rc_auth = manager
    return manager


def _remote_port(request_or_ws: Request | WebSocket) -> RemoteSessionPort:
    services = getattr(request_or_ws.app.state, "services", None)
    remote: RemoteSessionPort | None = (
        getattr(services, "remote", None) if services else None
    )
    if remote is None:
        raise HTTPException(
            status_code=503,
            detail="Remote Control runtime is not initialized.",
        )
    return remote


def _require_enabled_remote(request: Request) -> RemoteSessionPort:
    remote = _remote_port(request)
    if not remote.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="Remote Control is disabled. Enable RC_ENABLED in Admin -> Remote Control.",
        )
    return remote


def _translate_remote_error(exc: RemoteControlError) -> HTTPException:
    if isinstance(exc, RemoteDisabledError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, RemoteForbiddenError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, RemoteNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RemoteLimitError):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, RemoteConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# ----------------------------------------------------------- Pydantic models --


class PairTokenRequest(BaseModel):
    label: str = Field(default="Mobile Device", max_length=80)


class PairExchangeRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    label: str = Field(default="", max_length=80)


class TunnelStartRequest(BaseModel):
    mode: str | None = Field(default=None, max_length=32)


class CreateSessionRequest(BaseModel):
    harness: str = Field(min_length=1, max_length=64)
    cwd: str = Field(min_length=1, max_length=1024)
    title: str = Field(default="", max_length=160)
    prompt: str = Field(default="", max_length=100_000)
    permission_mode: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=160)
    create_worktree: bool = False
    driver_mode: str = Field(default="auto", max_length=32)
    external_session_id: str | None = Field(default=None, max_length=256)
    fork_session: bool = False
    cols: int = Field(default=120, ge=20, le=500)
    rows: int = Field(default=36, ge=5, le=200)


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class ModeUpdateRequest(BaseModel):
    mode: str = Field(min_length=1, max_length=32)


class ModelUpdateRequest(BaseModel):
    model: str = Field(min_length=1, max_length=160)


class ApprovalRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=128)
    decision: str = Field(min_length=1, max_length=32)
    updated_input: Mapping[str, Any] | None = None
    answers: Mapping[str, Any] | None = None
    message: str = Field(default="", max_length=1000)


class ForkSessionRequest(BaseModel):
    title: str = Field(default="", max_length=160)


# ------------------------------------------------------- Admin RC endpoints --


@admin_rc_router.get("/status")
async def admin_rc_status(request: Request) -> dict[str, Any]:
    remote = _remote_port(request)
    snapshot = remote.status_snapshot()
    snapshot["tunnels"] = await remote.inspect_tunnels()
    return snapshot


@admin_rc_router.post("/pair-token")
async def admin_rc_pair_token(
    request: Request, body: PairTokenRequest | None = None
) -> dict[str, Any]:
    remote = _remote_port(request)
    auth = _auth_manager(request)
    label = body.label if body is not None else "Mobile Device"
    record = auth.issue_pairing_token(label=label)
    tunnels = await remote.inspect_tunnels()
    base_url = str(tunnels.get("recommended_url") or tunnels.get("local_url") or "/rc")
    sep = "&" if "?" in base_url else "?"
    encoded = quote(record.token, safe="")
    pair_url = f"{base_url}{sep}pair={encoded}"
    local_base = str(tunnels.get("local_url") or "/rc")
    local_pair_url = f"{local_base}{sep}pair={encoded}"
    qr_svg = remote.generate_qr_svg(pair_url)
    return {
        "token": record.token,
        "label": record.label,
        "pair_url": pair_url,
        "local_pair_url": local_pair_url,
        "expires_in_seconds": 60,
        "expires_at": record.expires_at_epoch,
        "qr_svg": qr_svg,
    }


@admin_rc_router.get("/devices")
def admin_rc_devices(request: Request) -> dict[str, Any]:
    remote = _remote_port(request)
    return {"devices": remote.list_devices()}


@admin_rc_router.post("/devices/revoke-all")
def admin_rc_revoke_all(request: Request) -> dict[str, Any]:
    remote = _remote_port(request)
    revoked = remote.revoke_all_devices()
    return {"revoked": revoked}


@admin_rc_router.post("/devices/{device_id}/revoke")
def admin_rc_revoke_device(request: Request, device_id: str) -> dict[str, Any]:
    remote = _remote_port(request)
    revoked = remote.revoke_device(device_id)
    if not revoked:
        raise HTTPException(
            status_code=404, detail="Device not found or already revoked"
        )
    return {"revoked": True, "device_id": device_id}


@admin_rc_router.post("/tunnel/start")
async def admin_rc_tunnel_start(
    request: Request, body: TunnelStartRequest | None = None
) -> dict[str, Any]:
    remote = _remote_port(request)
    mode = body.mode if body is not None else None
    try:
        return await remote.start_tunnel(mode)
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


@admin_rc_router.post("/tunnel/stop")
async def admin_rc_tunnel_stop(request: Request) -> dict[str, Any]:
    remote = _remote_port(request)
    return await remote.stop_tunnel()


@admin_rc_router.get("/sessions")
def admin_rc_sessions(request: Request) -> dict[str, Any]:
    remote = _remote_port(request)
    return {"sessions": remote.list_sessions()}


@admin_rc_router.post("/sessions/{session_id}/stop")
async def admin_rc_stop_session(request: Request, session_id: str) -> dict[str, Any]:
    remote = _remote_port(request)
    try:
        return await remote.stop_session(session_id)
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


# ----------------------------------------------------------- /rc PWA static --


@remote_router.get("/rc")
def rc_index(request: Request) -> Response:
    remote = _remote_port(request)
    if not remote.is_enabled():
        return HTMLResponse(content=_DISABLED_HTML, status_code=503)
    path = REMOTE_STATIC_DIR / "index.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Remote Control UI missing")
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@remote_router.get("/rc/manifest.webmanifest")
def rc_manifest() -> Response:
    path = REMOTE_STATIC_DIR / "manifest.webmanifest"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Manifest missing")
    return FileResponse(
        path,
        media_type="application/manifest+json; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@remote_router.get("/rc/sw.js")
def rc_service_worker() -> Response:
    path = REMOTE_STATIC_DIR / "sw.js"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Service worker missing")
    return FileResponse(
        path,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store", "Service-Worker-Allowed": "/rc"},
    )


@remote_router.get("/rc/assets/vendor/{filename}")
def rc_vendor_asset(filename: str) -> Response:
    spec = _ALLOWED_VENDOR_FILES.get(filename)
    if spec is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    rel_path, media_type = spec
    path = REMOTE_STATIC_DIR / rel_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(path, media_type=media_type)


@remote_router.get("/rc/assets/{filename}")
def rc_asset(filename: str) -> Response:
    spec = _ALLOWED_STATIC_FILES.get(filename)
    if spec is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    rel_path, media_type = spec
    path = REMOTE_STATIC_DIR / rel_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


# -------------------------------------------------------------- /rc Auth API --


@remote_router.post("/rc/api/pair")
def rc_pair(request: Request, body: PairExchangeRequest) -> Response:
    remote = _require_enabled_remote(request)
    auth = _auth_manager(request)
    try:
        device, session_cookie, csrf_token = auth.redeem_pairing_token(
            token=body.token,
            label=body.label,
            request=request,
            remote=remote,
        )
    except RemoteAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    response = JSONResponse(
        content={
            "authenticated": True,
            "device": device,
            "csrf_token": csrf_token,
        },
        headers={"Cache-Control": "no-store"},
    )
    auth.set_auth_cookies(
        response,
        request=request,
        session_cookie=session_cookie,
        csrf_token=csrf_token,
    )
    return response


@remote_router.post("/rc/api/logout")
def rc_logout(request: Request) -> Response:
    remote = _require_enabled_remote(request)
    auth = _auth_manager(request)
    device_id = auth.authenticate_request(request, remote)
    auth.require_csrf(request)
    remote.revoke_device(device_id)
    response = JSONResponse(
        content={"authenticated": False, "revoked": True},
        headers={"Cache-Control": "no-store"},
    )
    auth.clear_auth_cookies(response, request=request)
    return response


@remote_router.get("/rc/api/me")
def rc_me(request: Request) -> dict[str, Any]:
    remote = _require_enabled_remote(request)
    auth = _auth_manager(request)
    try:
        device_id = auth.authenticate_request(request, remote)
    except HTTPException:
        return {"authenticated": False, "enabled": True}
    return {
        "authenticated": True,
        "enabled": True,
        "device_id": device_id,
    }


# ---------------------------------------------------------- /rc Session API --


def _require_rc_read(request: Request) -> tuple[RemoteSessionPort, str]:
    remote = _require_enabled_remote(request)
    auth = _auth_manager(request)
    device_id = auth.authenticate_request(request, remote)
    return remote, device_id


def _require_rc_write(request: Request) -> tuple[RemoteSessionPort, str]:
    remote = _require_enabled_remote(request)
    auth = _auth_manager(request)
    device_id = auth.authenticate_request(request, remote)
    auth.require_csrf(request)
    return remote, device_id


@remote_router.get("/rc/api/harnesses")
def rc_harnesses(request: Request) -> dict[str, Any]:
    remote, _device_id = _require_rc_read(request)
    return {"harnesses": remote.list_harnesses()}


@remote_router.get("/rc/api/fs/dirs")
def rc_fs_dirs(
    request: Request, path: str | None = Query(default=None)
) -> dict[str, Any]:
    remote, _device_id = _require_rc_read(request)
    try:
        return remote.list_allowed_dirs(path=path)
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


@remote_router.get("/rc/api/sessions")
def rc_list_sessions(request: Request) -> dict[str, Any]:
    remote, _device_id = _require_rc_read(request)
    return {"sessions": remote.list_sessions()}


@remote_router.post("/rc/api/sessions")
async def rc_create_session(
    request: Request, body: CreateSessionRequest
) -> dict[str, Any]:
    remote, _device_id = _require_rc_write(request)
    try:
        session = await remote.create_session(
            harness=body.harness,
            cwd=body.cwd,
            title=body.title,
            prompt=body.prompt,
            permission_mode=body.permission_mode,
            model=body.model,
            create_worktree=body.create_worktree,
            driver_mode=body.driver_mode,
            external_session_id=body.external_session_id,
            fork_session=body.fork_session,
            cols=body.cols,
            rows=body.rows,
        )
        return {"session": session}
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


@remote_router.get("/rc/api/sessions/{session_id}")
def rc_get_session(
    request: Request,
    session_id: str,
    since_seq: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    remote, _device_id = _require_rc_read(request)
    try:
        session = remote.get_session(session_id)
        events = remote.get_events(session_id, since_seq=since_seq)
        return {"session": session, "events": events}
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


@remote_router.post("/rc/api/sessions/{session_id}/message")
async def rc_send_message(
    request: Request, session_id: str, body: MessageRequest
) -> dict[str, Any]:
    remote, _device_id = _require_rc_write(request)
    try:
        return {"session": await remote.send_message(session_id, body.text)}
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


@remote_router.post("/rc/api/sessions/{session_id}/interrupt")
async def rc_interrupt_session(request: Request, session_id: str) -> dict[str, Any]:
    remote, _device_id = _require_rc_write(request)
    try:
        return {"session": await remote.interrupt_session(session_id)}
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


@remote_router.post("/rc/api/sessions/{session_id}/mode")
async def rc_set_mode(
    request: Request, session_id: str, body: ModeUpdateRequest
) -> dict[str, Any]:
    remote, _device_id = _require_rc_write(request)
    try:
        return {"session": await remote.set_permission_mode(session_id, body.mode)}
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


@remote_router.post("/rc/api/sessions/{session_id}/model")
async def rc_set_model(
    request: Request, session_id: str, body: ModelUpdateRequest
) -> dict[str, Any]:
    remote, _device_id = _require_rc_write(request)
    try:
        return {"session": await remote.set_model(session_id, body.model)}
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


@remote_router.post("/rc/api/sessions/{session_id}/approve")
async def rc_approve(
    request: Request, session_id: str, body: ApprovalRequest
) -> dict[str, Any]:
    remote, _device_id = _require_rc_write(request)
    try:
        approval = await remote.resolve_approval(
            session_id,
            body.approval_id,
            decision=body.decision,
            updated_input=body.updated_input,
            answers=body.answers,
            message=body.message,
        )
        return {"approval": approval}
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


@remote_router.post("/rc/api/sessions/{session_id}/fork")
async def rc_fork_session(
    request: Request, session_id: str, body: ForkSessionRequest | None = None
) -> dict[str, Any]:
    remote, _device_id = _require_rc_write(request)
    try:
        session = await remote.fork_session(
            session_id, title=body.title if body is not None else ""
        )
        return {"session": session}
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


@remote_router.get("/rc/api/sessions/{session_id}/diff")
async def rc_session_diff(request: Request, session_id: str) -> dict[str, Any]:
    remote, _device_id = _require_rc_read(request)
    try:
        return await remote.get_diff(session_id)
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


@remote_router.post("/rc/api/sessions/{session_id}/stop")
async def rc_stop_session(request: Request, session_id: str) -> dict[str, Any]:
    remote, _device_id = _require_rc_write(request)
    try:
        return {"session": await remote.stop_session(session_id)}
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


@remote_router.delete("/rc/api/sessions/{session_id}")
async def rc_delete_session(request: Request, session_id: str) -> dict[str, Any]:
    remote, _device_id = _require_rc_write(request)
    try:
        deleted = await remote.delete_session(session_id)
        return {"deleted": deleted}
    except RemoteControlError as exc:
        raise _translate_remote_error(exc) from exc


# ------------------------------------------------------------ /rc WebSockets --


@remote_router.websocket("/rc/ws/sessions/{session_id}")
async def rc_ws_session_events(
    websocket: WebSocket,
    session_id: str,
    since_seq: int = Query(default=0, ge=0),
) -> None:
    services = getattr(websocket.app.state, "services", None)
    remote: RemoteSessionPort | None = (
        getattr(services, "remote", None) if services else None
    )
    if remote is None or not remote.is_enabled():
        await websocket.close(code=1008)
        return
    auth = _auth_manager(websocket)
    device_id = auth.authenticate_websocket(websocket, remote)
    if not device_id:
        await websocket.close(code=1008)
        return
    try:
        remote.get_session(session_id)
    except RemoteControlError:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    unregister = remote.register_ws_closer(
        device_id, lambda: websocket.close(code=1008)
    )

    async def _pump_events() -> None:
        async for event in remote.subscribe_events(session_id, since_seq=since_seq):
            await websocket.send_text(json.dumps(event, ensure_ascii=False))

    async def _pump_commands() -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                cmd = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(cmd, dict):
                continue
            ctype = str(cmd.get("type") or "")
            if ctype == "ping":
                await websocket.send_text('{"kind":"pong"}')
            elif ctype == "message" and isinstance(cmd.get("text"), str):
                await remote.send_message(session_id, cmd["text"])
            elif ctype == "interrupt":
                await remote.interrupt_session(session_id)
            elif ctype == "approve" and isinstance(cmd.get("approval_id"), str):
                await remote.resolve_approval(
                    session_id,
                    cmd["approval_id"],
                    decision=str(cmd.get("decision") or "deny"),
                    updated_input=cmd.get("updated_input"),
                    answers=cmd.get("answers"),
                    message=str(cmd.get("message") or ""),
                )

    ev_task = asyncio.create_task(_pump_events())
    cmd_task = asyncio.create_task(_pump_commands())
    try:
        done, pending = await asyncio.wait(
            {ev_task, cmd_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for task in done:
            with contextlib.suppress(
                WebSocketDisconnect, asyncio.CancelledError, Exception
            ):
                task.result()
    finally:
        unregister()


@remote_router.websocket("/rc/ws/sessions/{session_id}/pty")
async def rc_ws_session_pty(websocket: WebSocket, session_id: str) -> None:
    services = getattr(websocket.app.state, "services", None)
    remote: RemoteSessionPort | None = (
        getattr(services, "remote", None) if services else None
    )
    if remote is None or not remote.is_enabled():
        await websocket.close(code=1008)
        return
    auth = _auth_manager(websocket)
    device_id = auth.authenticate_websocket(websocket, remote)
    if not device_id:
        await websocket.close(code=1008)
        return
    try:
        remote.get_session(session_id)
    except RemoteControlError:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    unregister = remote.register_ws_closer(
        device_id, lambda: websocket.close(code=1008)
    )

    async def _pump_pty_out() -> None:
        async for chunk in remote.subscribe_pty(session_id):
            await websocket.send_bytes(chunk)

    async def _pump_pty_in() -> None:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            raw_bytes = msg.get("bytes")
            if isinstance(raw_bytes, (bytes, bytearray)) and raw_bytes:
                await remote.pty_input(session_id, bytes(raw_bytes))
                continue
            raw_text = msg.get("text")
            if isinstance(raw_text, str) and raw_text:
                if raw_text.startswith("{"):
                    try:
                        frame = json.loads(raw_text)
                    except json.JSONDecodeError:
                        frame = None
                    if isinstance(frame, dict):
                        ftype = str(frame.get("type") or "")
                        if ftype == "resize":
                            await remote.pty_resize(
                                session_id,
                                int(frame.get("cols") or 120),
                                int(frame.get("rows") or 36),
                            )
                            continue
                        if ftype == "input" and isinstance(frame.get("data"), str):
                            await remote.pty_input(
                                session_id, frame["data"].encode("utf-8")
                            )
                            continue
                await remote.pty_input(session_id, raw_text.encode("utf-8"))

    out_task = asyncio.create_task(_pump_pty_out())
    in_task = asyncio.create_task(_pump_pty_in())
    try:
        done, pending = await asyncio.wait(
            {out_task, in_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for task in done:
            with contextlib.suppress(
                WebSocketDisconnect, asyncio.CancelledError, Exception
            ):
                task.result()
    finally:
        unregister()
