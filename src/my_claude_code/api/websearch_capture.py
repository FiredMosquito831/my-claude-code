"""Local request-log capture for standalone ``/v1/websearch`` and ``/v1/webfetch``."""

from collections.abc import Mapping
from typing import Any

from loguru import logger

from my_claude_code.config.settings import Settings
from my_claude_code.core.client_fingerprint import harness_from_headers
from my_claude_code.core.request_headers import capture_headers
from my_claude_code.core.request_log import (
    MAX_TEXT_CHARS,
    RequestRecord,
    RequestStatus,
    store_from_settings,
)


def record_rest_web_tool_call(
    settings: Settings,
    *,
    request_id: str,
    endpoint: str,
    protocol: str,
    optimization: str,
    headers: Mapping[str, str] | None,
    duration_ms: float,
    status: RequestStatus,
    raw_input: str | None = None,
    raw_output: str | None = None,
    params: Mapping[str, Any] | None = None,
    error_kind: str | None = None,
    error_message: str | None = None,
) -> None:
    """Enqueue a failure-safe local ``requests.db`` row for REST search/fetch."""

    try:
        store = store_from_settings(settings)
        if store is None:
            return
        capture_bodies = bool(getattr(settings, "request_log_capture_bodies", True))
        header_map = dict(headers) if headers is not None else {}
        input_text = (
            raw_input[:MAX_TEXT_CHARS]
            if capture_bodies and raw_input is not None
            else None
        )
        output_text = (
            raw_output[:MAX_TEXT_CHARS]
            if capture_bodies and raw_output is not None
            else None
        )
        record = RequestRecord(
            id=request_id,
            endpoint=endpoint,
            protocol=protocol,
            requested_model=None,
            provider=None,
            resolved_model=None,
            stream=False,
            input_text=input_text,
            output_text=output_text,
            input_chars=len(raw_input) if raw_input is not None else None,
            output_chars=len(raw_output) if raw_output is not None else None,
            params=dict(params) if params else None,
            tokens_in=None,
            tokens_out=None,
            duration_ms=round(duration_ms, 3),
            status=status,
            error_kind=error_kind,
            error_message=error_message[:500] if error_message else None,
            headers=capture_headers(header_map) if header_map else None,
            harness=harness_from_headers(header_map).harness,
            cost_usd=None,
            cost_source=None,
            optimization=optimization,
            optimization_tokens_saved=None,
        )
        store.enqueue(record)
    except Exception as exc:
        logger.debug("REST web tool request capture skipped: {}", type(exc).__name__)
