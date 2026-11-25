"""Echo-safe 400 refusal detection for native structured-output fields."""

import re
from collections.abc import Mapping
from typing import Any

from .complaint import (
    complaint_evidence_snippet,
    is_bad_request,
    upstream_complaint,
    upstream_error_payload,
)

RUNG_STRUCTURED_OUTPUT = "structured_output"

_STRUCTURED_PARAMS_BY_WIRE_FIELD: Mapping[str, frozenset[str]] = {
    "response_format": frozenset(
        {"response_format", "response_format.type", "response_format.json_schema"}
    ),
    "output_config.format": frozenset(
        {"output_config", "output_config.format", "output_config.format.schema"}
    ),
    "text.format": frozenset({"text", "text.format", "text.format.schema"}),
}

_ALL_STRUCTURED_PARAMS = frozenset().union(*_STRUCTURED_PARAMS_BY_WIRE_FIELD.values())

_TOOL_COMPLAINT_RE = re.compile(r"\btools?\b|\bfunction\b|array_above_max_length")
_STRUCTURED_SUBJECT_RE = re.compile(
    r"(?:response_format|output_config(?:\.format|\s+format)|"
    r"text(?:\.format|\s+format)|json_schema|structured[\s_-]+output|json_object)"
)
_REFUSAL_VERB_RE = re.compile(
    r"(?:not supported|unsupported|not allowed|invalid|"
    r"extra inputs are not permitted|unrecognized|unknown(?: field| parameter)?|"
    r"does not support|not permitted|unexpected)"
)


def _extract_param_and_code(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, Mapping):
        return None, None
    error = payload.get("error")
    source = error if isinstance(error, Mapping) else payload
    raw_param = source.get("param")
    raw_code = source.get("code")
    param = raw_param.strip().lower() if isinstance(raw_param, str) else None
    code = raw_code.strip().lower() if isinstance(raw_code, str) else None
    return param, code


def classify_structured_output_refusal(
    error: Exception,
    *,
    wire_field: str,
) -> str | None:
    """Return bounded complaint evidence if ``error`` refuses structured output."""
    if not is_bad_request(error):
        return None

    payload = upstream_error_payload(error)
    param, code = _extract_param_and_code(payload)
    complaint = upstream_complaint(error)

    if code == "array_above_max_length":
        return None
    if param is not None and (
        param == "tools" or param.startswith(("tools.", "tools["))
    ):
        return None
    if _TOOL_COMPLAINT_RE.search(complaint) and param not in _ALL_STRUCTURED_PARAMS:
        return None

    if (
        wire_field == "output_config.format"
        and "effort" in complaint
        and "format" not in complaint
        and "json_schema" not in complaint
        and "structured" not in complaint
    ) or (
        wire_field == "text.format"
        and "verbosity" in complaint
        and "format" not in complaint
        and "json_schema" not in complaint
        and "structured" not in complaint
    ):
        return None

    allowed_params = _STRUCTURED_PARAMS_BY_WIRE_FIELD.get(
        wire_field, _ALL_STRUCTURED_PARAMS
    )
    param_match = param in allowed_params and bool(_REFUSAL_VERB_RE.search(complaint))
    phrase_match = bool(
        _STRUCTURED_SUBJECT_RE.search(complaint) and _REFUSAL_VERB_RE.search(complaint)
    )
    if not (param_match or phrase_match):
        return None
    return complaint_evidence_snippet(complaint)
