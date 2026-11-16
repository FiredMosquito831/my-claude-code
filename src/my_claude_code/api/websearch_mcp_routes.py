"""Pure-FastAPI Streamable HTTP MCP endpoint for web_search, web_fetch, and search_docs."""

import importlib.metadata
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from my_claude_code.config.settings import Settings

from .dependencies import get_services, get_settings, require_proxy_auth
from .ports import ApiServices
from .request_ids import new_request_id
from .websearch_capture import record_rest_web_tool_call
from .wire_surfaces import WEBSEARCH_MCP_HTTP_PATH

try:
    _SERVER_VERSION = importlib.metadata.version("my-claude-code")
except importlib.metadata.PackageNotFoundError:
    _SERVER_VERSION = "8.26.0"

websearch_mcp_router = APIRouter()

MCP_PROTOCOL_VERSION = "2025-03-26"
_SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05")

_WEB_SEARCH_MCP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "description": "Search query string.",
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "description": "Maximum number of results to return (1-20).",
        },
        "allowed_domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional domain allowlist.",
        },
        "blocked_domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional domain blocklist.",
        },
        "provider": {
            "type": "string",
            "description": "Optional web search backend provider override.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_WEB_FETCH_MCP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "minLength": 1,
            "description": "HTTP or HTTPS URL to fetch.",
        },
        "extractor": {
            "type": "string",
            "enum": ["trafilatura", "basic"],
            "description": "Optional HTML extractor override.",
        },
        "max_chars": {
            "type": "integer",
            "minimum": 100,
            "maximum": 100000,
            "description": "Maximum extracted character limit (100-100000).",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}

_SEARCH_DOCS_MCP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "description": "Documentation query or question.",
        },
        "library": {
            "type": "string",
            "minLength": 1,
            "description": "Optional library or package name to focus Context7 lookup.",
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "description": "Maximum number of snippets to return (1-20).",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

MCP_WEBSEARCH_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "web_search",
        "description": (
            "Search the public web through MCC's configured Web Search route/chain "
            "and return normalized results."
        ),
        "inputSchema": _WEB_SEARCH_MCP_SCHEMA,
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch a public HTTP/HTTPS page with SSRF egress checks and extract readable "
            "markdown or text."
        ),
        "inputSchema": _WEB_FETCH_MCP_SCHEMA,
    },
    {
        "name": "search_docs",
        "description": (
            "Search library and framework documentation via MCC's Context7 documentation adapter."
        ),
        "inputSchema": _SEARCH_DOCS_MCP_SCHEMA,
    },
)
_MCP_TOOLS_BY_NAME: dict[str, dict[str, Any]] = {
    tool["name"]: tool for tool in MCP_WEBSEARCH_TOOLS
}


def _jsonrpc_error_payload(
    req_id: Any,
    *,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def _jsonrpc_result_payload(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _render_mcp_response(
    request: Request,
    payload: dict[str, Any],
    *,
    status_code: int = 200,
) -> Response:
    accept = (request.headers.get("accept") or "").lower()
    if (
        status_code == 200
        and "text/event-stream" in accept
        and "application/json" not in accept
    ):
        frame = f"event: message\ndata: {json.dumps(payload)}\n\n"
        return StreamingResponse(
            iter([frame]),
            status_code=200,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    return JSONResponse(status_code=status_code, content=payload)


def _call_tool_error(
    reason: str, *, schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    if schema is not None:
        schema_json = json.dumps(schema, sort_keys=True)
        text = f"{reason} Expected inputSchema: {schema_json}"
        structured: dict[str, Any] = {"error": reason, "inputSchema": schema}
    else:
        text = reason
        structured = {"error": reason}
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": True,
    }


def _validate_domain_strings(
    raw: Any, *, field_name: str, schema: dict[str, Any]
) -> tuple[tuple[str, ...] | None, str | None]:
    if raw is None:
        return (), None
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw
    ):
        return None, (f"Invalid '{field_name}': expected a list of non-empty strings.")
    return tuple(item.strip().lower().rstrip(".") for item in raw if item.strip()), None


def _validate_int_range(
    raw: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
    default: int,
) -> tuple[int | None, str | None]:
    if raw is None:
        return default, None
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or raw < minimum
        or raw > maximum
    ):
        return None, (
            f"Invalid '{field_name}': expected an integer between {minimum} and {maximum}."
        )
    return raw, None


def _validate_tool_arguments(
    tool_name: str, raw_args: Any
) -> tuple[dict[str, Any] | None, str | None]:
    schema = _MCP_TOOLS_BY_NAME[tool_name]["inputSchema"]
    allowed_keys = set(schema["properties"].keys())
    if not isinstance(raw_args, dict):
        return None, f"Invalid arguments for {tool_name}: expected a JSON object."

    unexpected = sorted(set(raw_args.keys()) - allowed_keys)
    if unexpected:
        return None, (
            f"Invalid arguments for {tool_name}: unexpected parameter(s) {', '.join(unexpected)}."
        )

    if tool_name == "web_search":
        query = raw_args.get("query")
        if not isinstance(query, str) or not query.strip():
            return (
                None,
                "Invalid arguments for web_search: 'query' must be a non-empty string.",
            )
        max_results, err = _validate_int_range(
            raw_args.get("max_results"),
            field_name="max_results",
            minimum=1,
            maximum=20,
            default=10,
        )
        if err:
            return None, err
        allowed, err = _validate_domain_strings(
            raw_args.get("allowed_domains"), field_name="allowed_domains", schema=schema
        )
        if err:
            return None, err
        blocked, err = _validate_domain_strings(
            raw_args.get("blocked_domains"), field_name="blocked_domains", schema=schema
        )
        if err:
            return None, err
        provider = raw_args.get("provider")
        if provider is not None and (
            not isinstance(provider, str) or not provider.strip()
        ):
            return None, "Invalid 'provider': expected a non-empty string."
        return {
            "query": query.strip(),
            "max_results": max_results,
            "allowed_domains": allowed,
            "blocked_domains": blocked,
            "provider": provider.strip() if isinstance(provider, str) else None,
        }, None

    if tool_name == "web_fetch":
        url = raw_args.get("url")
        if not isinstance(url, str) or not url.strip():
            return (
                None,
                "Invalid arguments for web_fetch: 'url' must be a non-empty string.",
            )
        extractor = raw_args.get("extractor")
        if extractor is not None and (
            not isinstance(extractor, str) or extractor not in ("trafilatura", "basic")
        ):
            return None, "Invalid 'extractor': expected 'trafilatura' or 'basic'."
        max_chars, err = _validate_int_range(
            raw_args.get("max_chars"),
            field_name="max_chars",
            minimum=100,
            maximum=100000,
            default=24000,
        )
        if err:
            return None, err
        return {
            "url": url.strip(),
            "extractor": extractor,
            "max_chars": max_chars,
        }, None

    # search_docs
    query = raw_args.get("query")
    if not isinstance(query, str) or not query.strip():
        return (
            None,
            "Invalid arguments for search_docs: 'query' must be a non-empty string.",
        )
    library = raw_args.get("library")
    if library is not None and (not isinstance(library, str) or not library.strip()):
        return (
            None,
            "Invalid arguments for search_docs: 'library' must be a non-empty string.",
        )
    max_results, err = _validate_int_range(
        raw_args.get("max_results"),
        field_name="max_results",
        minimum=1,
        maximum=20,
        default=10,
    )
    if err:
        return None, err
    combined_query = (
        f"{library.strip()} {query.strip()}".strip()
        if isinstance(library, str)
        else query.strip()
    )
    return {
        "query": combined_query,
        "raw_query": query.strip(),
        "library": library.strip() if isinstance(library, str) else None,
        "max_results": max_results,
    }, None


def _available_tools_error_data() -> dict[str, Any]:
    return {
        "available_tools": {
            tool["name"]: tool["inputSchema"] for tool in MCP_WEBSEARCH_TOOLS
        }
    }


async def _execute_mcp_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    request: Request,
    settings: Settings,
    request_id: str,
) -> dict[str, Any]:
    from .web_tools import outbound, rest_service, streaming

    started_mono = time.monotonic()

    if tool_name in ("web_search", "search_docs"):
        if (
            not settings.enable_web_server_tools
            or settings.web_search_provider == "disabled"
        ):
            reason = (
                "Web search is disabled on this MCC server "
                "(ENABLE_WEB_SERVER_TOOLS=false or WEB_SEARCH_PROVIDER=disabled)."
            )
            record_rest_web_tool_call(
                settings,
                request_id=request_id,
                endpoint=WEBSEARCH_MCP_HTTP_PATH,
                protocol="mcp",
                optimization=f"mcp:{tool_name}",
                headers=request.headers,
                duration_ms=(time.monotonic() - started_mono) * 1000.0,
                status="error",
                raw_input=json.dumps({"tool": tool_name, **arguments}),
                error_kind="disabled",
                error_message=reason,
            )
            return _call_tool_error(reason)

        provider_override = (
            "context7" if tool_name == "search_docs" else arguments.get("provider")
        )
        allowed_domains = arguments.get("allowed_domains") or ()
        blocked_domains = arguments.get("blocked_domains") or ()
        try:
            response, items, meta = await outbound._run_web_search_response(
                arguments["query"],
                settings,
                max_results=int(arguments["max_results"]),
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                provider_override=provider_override,
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - started_mono) * 1000.0
            summary = outbound._web_tool_client_error_summary(
                "web_search", exc, verbose=bool(settings.log_api_error_tracebacks)
            )
            record_rest_web_tool_call(
                settings,
                request_id=request_id,
                endpoint=WEBSEARCH_MCP_HTTP_PATH,
                protocol="mcp",
                optimization=f"mcp:{tool_name}",
                headers=request.headers,
                duration_ms=duration_ms,
                status="error",
                raw_input=json.dumps({"tool": tool_name, **arguments}),
                error_kind=type(exc).__name__,
                error_message=summary,
            )
            return _call_tool_error(f"{tool_name} failed: {summary}")

        digest = streaming.format_search_digest(arguments["query"], items, settings)
        structured = {
            "provider": meta.terminal_provider,
            "query": arguments["query"],
            "answer": response.answer if response is not None else None,
            "results": items,
        }
        record_rest_web_tool_call(
            settings,
            request_id=request_id,
            endpoint=WEBSEARCH_MCP_HTTP_PATH,
            protocol="mcp",
            optimization=f"mcp:{tool_name}",
            headers=request.headers,
            duration_ms=(time.monotonic() - started_mono) * 1000.0,
            status="success",
            raw_input=json.dumps({"tool": tool_name, **arguments}),
            raw_output=digest,
            params={"provider": meta.terminal_provider, "results_count": len(items)},
        )
        return {
            "content": [{"type": "text", "text": digest}],
            "structuredContent": structured,
            "isError": False,
        }

    # web_fetch
    if not settings.enable_web_server_tools:
        reason = (
            "Web fetch is disabled on this MCC server (ENABLE_WEB_SERVER_TOOLS=false)."
        )
        return _call_tool_error(reason)

    ts_epoch = time.time()
    egress = None
    requested_extractor = arguments.get("extractor") or settings.web_fetch_extractor
    requested_max_chars = int(arguments["max_chars"])
    try:
        egress, _forced = rest_service._resolve_webfetch_egress(
            settings, request, requested_allow_private=None
        )
        fetched, extractor_used, truncated = await outbound._run_web_fetch_detail(
            arguments["url"],
            egress,
            extractor=arguments.get("extractor"),
            max_chars=requested_max_chars,
        )
    except Exception as exc:
        duration_ms = (time.monotonic() - started_mono) * 1000.0
        summary = outbound._web_tool_client_error_summary(
            "web_fetch", exc, verbose=bool(settings.log_api_error_tracebacks)
        )
        rest_service._emit_webfetch_outcome(
            ts_epoch=ts_epoch,
            duration_ms=duration_ms,
            input_url=arguments["url"],
            extractor=requested_extractor,
            max_chars=requested_max_chars,
            status="failed",
            error_kind=type(exc).__name__,
            error_message=summary,
            payload=None,
            extractor_used=requested_extractor,
            truncated=False,
            egress=egress,
            settings=settings,
            proxy_url=outbound.websearch_proxy_url(),
        )
        record_rest_web_tool_call(
            settings,
            request_id=request_id,
            endpoint=WEBSEARCH_MCP_HTTP_PATH,
            protocol="mcp",
            optimization="mcp:web_fetch",
            headers=request.headers,
            duration_ms=duration_ms,
            status="error",
            raw_input=json.dumps({"tool": "web_fetch", **arguments}),
            error_kind=type(exc).__name__,
            error_message=summary,
        )
        return _call_tool_error(f"web_fetch failed: {summary}")

    duration_ms = (time.monotonic() - started_mono) * 1000.0
    rest_service._emit_webfetch_outcome(
        ts_epoch=ts_epoch,
        duration_ms=duration_ms,
        input_url=arguments["url"],
        extractor=requested_extractor,
        max_chars=requested_max_chars,
        status="success",
        error_kind=None,
        error_message=None,
        payload=fetched,
        extractor_used=extractor_used,
        truncated=truncated,
        egress=egress,
        settings=settings,
        proxy_url=outbound.websearch_proxy_url(),
    )
    record_rest_web_tool_call(
        settings,
        request_id=request_id,
        endpoint=WEBSEARCH_MCP_HTTP_PATH,
        protocol="mcp",
        optimization="mcp:web_fetch",
        headers=request.headers,
        duration_ms=duration_ms,
        status="success",
        raw_input=json.dumps({"tool": "web_fetch", **arguments}),
        raw_output=fetched.get("data", ""),
        params={"extractor": extractor_used, "truncated": truncated},
    )
    text_out = (
        f"Title: {fetched.get('title', '')}\n"
        f"URL: {fetched.get('url', arguments['url'])}\n\n"
        f"{fetched.get('data', '')}"
    )
    structured = {
        "url": fetched.get("url", arguments["url"]),
        "title": fetched.get("title", ""),
        "media_type": fetched.get("media_type", "text/plain"),
        "extractor": extractor_used,
        "truncated": truncated,
        "content": fetched.get("data", ""),
    }
    return {
        "content": [{"type": "text", "text": text_out}],
        "structuredContent": structured,
        "isError": False,
    }


def _disabled_response(request: Request, req_id: Any) -> Response:
    return _render_mcp_response(
        request,
        _jsonrpc_error_payload(
            req_id,
            code=-32000,
            message=(
                "HTTP MCP web search endpoint is disabled "
                "(WEB_SEARCH_MCP_HTTP_ENABLED=false)."
            ),
        ),
        status_code=503,
    )


@websearch_mcp_router.head(
    WEBSEARCH_MCP_HTTP_PATH, dependencies=[Depends(require_proxy_auth)]
)
@websearch_mcp_router.options(
    WEBSEARCH_MCP_HTTP_PATH, dependencies=[Depends(require_proxy_auth)]
)
async def probe_websearch_mcp(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Response:
    """Report supported HTTP methods on the Streamable HTTP MCP endpoint."""
    if not settings.web_search_mcp_http_enabled:
        return _disabled_response(request, None)
    return Response(status_code=204, headers={"Allow": "POST, HEAD, OPTIONS"})


@websearch_mcp_router.post(
    WEBSEARCH_MCP_HTTP_PATH, dependencies=[Depends(require_proxy_auth)]
)
async def handle_websearch_mcp(
    request: Request,
    services: ApiServices = Depends(get_services),
) -> Response:
    """Handle one MCP Streamable HTTP JSON-RPC 2.0 request or notification."""
    lease = await services.requests.acquire()
    try:
        settings = lease.settings
        if not settings.web_search_mcp_http_enabled:
            return _disabled_response(request, None)

        raw_body = await request.body()
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return _render_mcp_response(
                request,
                _jsonrpc_error_payload(
                    None, code=-32700, message="Parse error: invalid JSON payload."
                ),
                status_code=400,
            )

        if (
            not isinstance(payload, dict)
            or payload.get("jsonrpc") != "2.0"
            or not isinstance(payload.get("method"), str)
            or not payload["method"].strip()
        ):
            req_id = payload.get("id") if isinstance(payload, dict) else None
            return _render_mcp_response(
                request,
                _jsonrpc_error_payload(
                    req_id,
                    code=-32600,
                    message="Invalid Request: expected JSON-RPC 2.0 object with non-empty string 'method'.",
                ),
                status_code=400,
            )

        method = payload["method"].strip()
        params = payload.get("params")
        has_id = "id" in payload and payload["id"] is not None

        # Notifications return 202 Accepted with empty body per MCP Streamable HTTP spec.
        if not has_id:
            return Response(status_code=202)

        req_id = payload["id"]

        if method == "initialize":
            client_version = (
                params.get("protocolVersion")
                if isinstance(params, dict)
                and isinstance(params.get("protocolVersion"), str)
                else None
            )
            negotiated = (
                client_version
                if client_version in _SUPPORTED_PROTOCOL_VERSIONS
                else MCP_PROTOCOL_VERSION
            )
            return _render_mcp_response(
                request,
                _jsonrpc_result_payload(
                    req_id,
                    {
                        "protocolVersion": negotiated,
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "mcc-websearch",
                            "version": _SERVER_VERSION,
                        },
                    },
                ),
            )

        if method == "ping":
            return _render_mcp_response(request, _jsonrpc_result_payload(req_id, {}))

        if method == "tools/list":
            return _render_mcp_response(
                request,
                _jsonrpc_result_payload(
                    req_id, {"tools": [dict(t) for t in MCP_WEBSEARCH_TOOLS]}
                ),
            )

        if method == "tools/call":
            if not isinstance(params, dict):
                return _render_mcp_response(
                    request,
                    _jsonrpc_error_payload(
                        req_id,
                        code=-32602,
                        message=(
                            "Invalid params: 'params' must be an object with 'name' and 'arguments'. "
                            f"Available tools: {', '.join(_MCP_TOOLS_BY_NAME)}."
                        ),
                        data=_available_tools_error_data(),
                    ),
                )
            tool_name = params.get("name")
            if not isinstance(tool_name, str) or tool_name not in _MCP_TOOLS_BY_NAME:
                return _render_mcp_response(
                    request,
                    _jsonrpc_error_payload(
                        req_id,
                        code=-32602,
                        message=(
                            f"Invalid params: unknown tool {tool_name!r}. "
                            f"Available tools: {', '.join(_MCP_TOOLS_BY_NAME)}."
                        ),
                        data=_available_tools_error_data(),
                    ),
                )
            raw_args = params.get("arguments", {})
            validated_args, val_err = _validate_tool_arguments(tool_name, raw_args)
            if validated_args is None:
                return _render_mcp_response(
                    request,
                    _jsonrpc_result_payload(
                        req_id,
                        _call_tool_error(
                            val_err or f"Invalid arguments for {tool_name}.",
                            schema=_MCP_TOOLS_BY_NAME[tool_name]["inputSchema"],
                        ),
                    ),
                )
            result = await _execute_mcp_tool(
                tool_name=tool_name,
                arguments=validated_args,
                request=request,
                settings=settings,
                request_id=new_request_id(),
            )
            return _render_mcp_response(
                request, _jsonrpc_result_payload(req_id, result)
            )

        return _render_mcp_response(
            request,
            _jsonrpc_error_payload(
                req_id,
                code=-32601,
                message=f"Method not found: {method!r}",
            ),
        )
    finally:
        await lease.release()
