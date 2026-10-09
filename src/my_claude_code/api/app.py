"""Pure FastAPI application factory."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler as starlette_http_exception_handler,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from my_claude_code.application.errors import ApplicationError
from my_claude_code.config.admin.env_io import SettingsFileBusyError
from my_claude_code.config.proxy_chains import ProxyChainsUnreadableError
from my_claude_code.core.anthropic import anthropic_error_payload
from my_claude_code.core.diagnostics import (
    redacted_exception_traceback,
    safe_exception_message,
)
from my_claude_code.core.gemini_api import gemini_error_payload
from my_claude_code.core.openai_common import openai_error_payload
from my_claude_code.core.trace import (
    extract_claude_session_id_from_headers,
    trace_event,
)
from my_claude_code.core.version import package_version

from .admin_cache import AdminNoStoreMiddleware, attach_admin_no_store
from .admin_claude_config_routes import router as admin_claude_config_router
from .admin_custom_routes import router as admin_custom_router
from .admin_desktop_app_routes import router as admin_desktop_app_router
from .admin_export_routes import router as admin_export_router
from .admin_harness_routes import router as admin_harness_router
from .admin_media_routes import router as admin_media_router
from .admin_proxy_routes import router as admin_proxy_router
from .admin_proxy_source_routes import router as admin_proxy_source_router
from .admin_routes import router as admin_router
from .admin_server_routes import router as admin_server_router
from .admin_websearch_routes import router as admin_websearch_router
from .gemini_media_routes import router as gemini_media_router
from .gemini_routes import gemini_router
from .media_routes import router as media_router
from .media_video_routes import router as media_video_router
from .ports import ApiServices
from .request_errors import ordinary_application_error_response
from .request_ids import (
    RequestCorrelationMiddleware,
    attach_request_id_headers,
    get_request_id,
)
from .routes import router
from .validation_log import summarize_request_validation_body
from .wire_surfaces import is_gemini_shaped, is_openai_shaped, wire_api_for_path


def create_app(services: ApiServices) -> FastAPI:
    """Create the HTTP adapter around explicitly supplied runtime services."""
    app = FastAPI(title="Claude Code Proxy", version=package_version())
    app.state.services = services
    app.add_middleware(RequestCorrelationMiddleware)
    app.add_middleware(AdminNoStoreMiddleware)

    app.include_router(admin_router)
    app.include_router(admin_custom_router)
    app.include_router(admin_claude_config_router)
    app.include_router(admin_harness_router)
    app.include_router(admin_proxy_router)
    app.include_router(admin_proxy_source_router)
    app.include_router(admin_desktop_app_router)
    app.include_router(admin_server_router)
    app.include_router(admin_websearch_router)
    app.include_router(admin_export_router)
    app.include_router(admin_media_router)
    app.include_router(gemini_router)
    app.include_router(gemini_media_router)
    app.include_router(media_router)
    app.include_router(media_video_router)
    app.include_router(router)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        """Report an HTTP-level refusal in the envelope of the surface it hit.

        Every other surface keeps FastAPI's ``{"detail": ...}`` body, which is
        what their clients have always received and what their tests pin. The
        Gemini surface cannot: ``@google/genai`` parses a non-2xx body as
        ``{"error": {code, message, status}}`` and reports "unknown error" for
        anything else, so a rejected proxy token would tell a Gemini CLI user
        nothing at all. Only that surface is reshaped.
        """

        if not is_gemini_shaped(wire_api_for_path(request.url.path)):
            return await starlette_http_exception_handler(request, exc)
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        response = JSONResponse(
            status_code=exc.status_code,
            content=gemini_error_payload(message=detail, code=exc.status_code),
            headers=getattr(exc, "headers", None),
        )
        attach_request_id_headers(
            response,
            request_id=get_request_id(request),
            path=request.url.path,
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        """Log request shape for 422 debugging without content values."""
        body: Any
        try:
            body = await request.json()
        except Exception as error:
            body = {"_json_error": type(error).__name__}

        message_summary, tool_names = summarize_request_validation_body(body)
        trace_event(
            stage="ingress",
            event="server.request.validation_failed",
            source="api",
            path=request.url.path,
            query=dict(request.query_params),
            error_locs=[list(error.get("loc", ())) for error in exc.errors()],
            error_types=[str(error.get("type", "")) for error in exc.errors()],
            message_summary=message_summary,
            tool_names=tool_names,
        )
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(SettingsFileBusyError)
    async def settings_file_busy_handler(request: Request, exc: SettingsFileBusyError):
        """A settings save refused before anything was written (7.69.7).

        Every route that saves settings reaches this one answer: 503 with the
        plain sentence as ``detail`` -- the admin routes' own envelope for a
        transient failure, and what the dashboard's ``api()`` shows -- rather
        than an internal error. Registered once here, so no saving route can
        forget it.
        """

        return await starlette_http_exception_handler(
            request, StarletteHTTPException(status_code=503, detail=str(exc))
        )

    @app.exception_handler(ProxyChainsUnreadableError)
    async def proxy_chains_unreadable_handler(
        request: Request, exc: ProxyChainsUnreadableError
    ):
        """A proxy-chain save refused before anything was written (7.78.8).

        The chain file is there and cannot be read, so a document derived from
        that read would replace every chain in it. Every route that saves the
        store -- a chain edit, a check's verdicts, an offer, an undo -- reaches
        this one answer: 503 with the sentence naming the file and what to do,
        exactly as a busy settings file does. Registered once here, so no
        saving route can forget it.
        """

        return await starlette_http_exception_handler(
            request, StarletteHTTPException(status_code=503, detail=str(exc))
        )

    @app.exception_handler(ApplicationError)
    async def application_error_handler(request: Request, exc: ApplicationError):
        """Serialize defensive application failures in the selected wire protocol."""
        return ordinary_application_error_response(
            exc,
            wire_api=wire_api_for_path(request.url.path),
            request_id=get_request_id(request),
        )

    @app.exception_handler(Exception)
    async def general_error_handler(request: Request, exc: Exception):
        """Handle general errors and return Anthropic format."""
        request_id = get_request_id(request)
        claude_sid = extract_claude_session_id_from_headers(request.headers)
        settings = services.requests.current_settings()
        with logger.contextualize(
            http_method=request.method,
            http_path=request.url.path,
            claude_session_id=claude_sid,
            request_id=request_id,
        ):
            if settings.log_api_error_tracebacks:
                logger.error("General Error: {}", safe_exception_message(exc))
                logger.error(redacted_exception_traceback(exc))
            else:
                logger.error(
                    "General Error: path={} method={} exc_type={}",
                    request.url.path,
                    request.method,
                    type(exc).__name__,
                )
            message = safe_exception_message(exc)
            wire_api = wire_api_for_path(request.url.path)
            if is_gemini_shaped(wire_api):
                content = gemini_error_payload(message=message, code=500)
            elif is_openai_shaped(wire_api):
                content = openai_error_payload(message=message, error_type="api_error")
            else:
                content = anthropic_error_payload(
                    error_type="api_error",
                    message=message,
                    request_id=request_id,
                )
            response = JSONResponse(status_code=500, content=content)
        attach_admin_no_store(response, path=request.url.path)
        attach_request_id_headers(
            response,
            request_id=request_id,
            path=request.url.path,
        )
        return response

    return app
