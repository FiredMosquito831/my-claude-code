"""Protocol-neutral structured-output, tool-history, and PDF helper primitives.

This module sits inside ``core/anthropic`` so ``core/`` remains dependency-free
(``ALLOWED_PACKAGE_DEPENDENCIES["core"] = set()``) while sharing canonical IR
normalization, prompt-fallback rendering, and tool-history checks across all
inbound doors, outbound converters, and provider recovery rungs.
"""

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from .content import get_block_attr, get_block_type
from .models import MessagesRequest

_JSON_OBJECT_INSTRUCTION = (
    "Respond with a single valid JSON object and nothing else. "
    "Do not wrap it in Markdown code fences and do not add commentary."
)

_DATA_URL_PREFIX = "data:"
_DEFAULT_PDF_FILENAME = "document.pdf"


class StructuredOutputValidationError(ValueError):
    """Raised when an inbound structured-output schema/format is invalid."""

    def __init__(self, message: str, *, param: str) -> None:
        super().__init__(message)
        self.message = message
        self.param = param


def parse_chat_response_format(
    response_format: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate OpenAI Chat ``response_format`` into ``(spec, output_config_format)``."""
    if response_format is None:
        return None, None
    if not isinstance(response_format, Mapping):
        raise StructuredOutputValidationError(
            "response_format must be an object",
            param="response_format",
        )
    format_type = response_format.get("type")
    if format_type is None:
        return None, None
    if format_type == "text":
        return {"type": "text"}, None
    if format_type == "json_object":
        return {"type": "json_object"}, None
    if format_type == "json_schema":
        schema_holder = response_format.get("json_schema")
        if not isinstance(schema_holder, Mapping):
            raise StructuredOutputValidationError(
                "response_format.json_schema must be an object",
                param="response_format",
            )
        schema = schema_holder.get("schema")
        if not isinstance(schema, Mapping):
            raise StructuredOutputValidationError(
                "response_format.json_schema.schema must be an object",
                param="response_format",
            )
        schema_copy = deepcopy(dict(schema))
        spec: dict[str, Any] = {"type": "json_schema", "schema": schema_copy}
        name = schema_holder.get("name")
        if isinstance(name, str) and name:
            spec["name"] = name
        strict = schema_holder.get("strict")
        if isinstance(strict, bool):
            spec["strict"] = strict
        description = schema_holder.get("description")
        if isinstance(description, str) and description:
            spec["description"] = description
        output_format = {"type": "json_schema", "schema": deepcopy(schema_copy)}
        return spec, output_format
    raise StructuredOutputValidationError(
        f"Unsupported response_format type: {format_type!r}. "
        "MCC accepts text, json_object and json_schema.",
        param="response_format",
    )


def parse_responses_text_format(
    text: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate OpenAI Responses ``text.format`` into ``(spec, output_config_format)``."""
    if not isinstance(text, Mapping):
        return None, None
    text_fmt = text.get("format")
    if text_fmt is None:
        return None, None
    if not isinstance(text_fmt, Mapping):
        raise StructuredOutputValidationError(
            "text.format must be an object",
            param="text",
        )
    format_type = text_fmt.get("type")
    if format_type is None:
        return None, None
    if format_type == "text":
        return {"type": "text"}, None
    if format_type == "json_object":
        return {"type": "json_object"}, None
    if format_type == "json_schema":
        schema = text_fmt.get("schema")
        if not isinstance(schema, Mapping):
            raise StructuredOutputValidationError(
                "text.format.schema must be an object",
                param="text",
            )
        schema_copy = deepcopy(dict(schema))
        spec: dict[str, Any] = {"type": "json_schema", "schema": schema_copy}
        name = text_fmt.get("name")
        if isinstance(name, str) and name:
            spec["name"] = name
        strict = text_fmt.get("strict")
        if isinstance(strict, bool):
            spec["strict"] = strict
        description = text_fmt.get("description")
        if isinstance(description, str) and description:
            spec["description"] = description
        output_format = {"type": "json_schema", "schema": deepcopy(schema_copy)}
        return spec, output_format
    raise StructuredOutputValidationError(
        f"Unsupported text.format type: {format_type!r}. "
        "MCC accepts text, json_object and json_schema.",
        param="text",
    )


def attach_structured_output_schema(
    payload: dict[str, Any],
    *,
    schema: Mapping[str, Any],
    name: str = "structured_output",
) -> None:
    """Attach canonical ``structured_output_spec`` and ``output_config.format``."""
    schema_copy = deepcopy(dict(schema))
    payload["structured_output_spec"] = {
        "type": "json_schema",
        "name": name,
        "schema": schema_copy,
    }
    output_config = payload.get("output_config")
    if not isinstance(output_config, dict):
        output_config = {}
        payload["output_config"] = output_config
    output_config["format"] = {
        "type": "json_schema",
        "schema": deepcopy(schema_copy),
    }


def effective_structured_output_spec(
    request: MessagesRequest,
) -> dict[str, Any] | None:
    """Return canonical structured-output spec from ``request`` if configured."""
    spec = getattr(request, "structured_output_spec", None)
    if isinstance(spec, Mapping):
        return deepcopy(dict(spec))
    output_config = getattr(request, "output_config", None)
    if isinstance(output_config, Mapping):
        fmt = output_config.get("format")
        if isinstance(fmt, Mapping):
            fmt_type = fmt.get("type")
            if fmt_type == "json_schema":
                schema = fmt.get("schema")
                if isinstance(schema, Mapping):
                    out: dict[str, Any] = {
                        "type": "json_schema",
                        "schema": deepcopy(dict(schema)),
                    }
                    name = fmt.get("name")
                    if isinstance(name, str) and name:
                        out["name"] = name
                    return out
            elif fmt_type == "json_object":
                return {"type": "json_object"}
            elif fmt_type == "text":
                return {"type": "text"}
    return None


def render_response_format_instruction(spec: Mapping[str, Any] | None) -> str | None:
    """Render a prompt-fallback instruction for any structured-output spec shape."""
    if spec is None:
        return None
    format_type = spec.get("type")
    if format_type in (None, "text"):
        return None
    if format_type == "json_object":
        return _JSON_OBJECT_INSTRUCTION
    if format_type == "json_schema":
        nested = spec.get("json_schema")
        source = nested if isinstance(nested, Mapping) else spec
        schema = source.get("schema")
        if not isinstance(schema, Mapping):
            return None
        name = source.get("name")
        label = f" named {name!r}" if isinstance(name, str) and name else ""
        return (
            f"{_JSON_OBJECT_INSTRUCTION} The object{label} must validate "
            f"against this JSON Schema:\n{json.dumps(schema, sort_keys=True)}"
        )
    return None


def to_openai_chat_response_format(
    spec: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Translate canonical structured-output spec to Chat ``response_format``."""
    if spec is None:
        return None
    format_type = spec.get("type")
    if format_type == "json_object":
        return {"type": "json_object"}
    if format_type == "json_schema":
        schema = spec.get("schema")
        if not isinstance(schema, Mapping):
            return None
        name = spec.get("name")
        inner: dict[str, Any] = {
            "name": name if isinstance(name, str) and name else "structured_output",
            "schema": deepcopy(dict(schema)),
        }
        strict = spec.get("strict")
        if isinstance(strict, bool):
            inner["strict"] = strict
        description = spec.get("description")
        if isinstance(description, str) and description:
            inner["description"] = description
        return {"type": "json_schema", "json_schema": inner}
    return None


def to_responses_text_format(
    spec: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Translate canonical structured-output spec to Responses ``text.format``."""
    if spec is None:
        return None
    format_type = spec.get("type")
    if format_type == "text":
        return {"type": "text"}
    if format_type == "json_object":
        return {"type": "json_object"}
    if format_type == "json_schema":
        schema = spec.get("schema")
        if not isinstance(schema, Mapping):
            return None
        name = spec.get("name")
        out: dict[str, Any] = {
            "type": "json_schema",
            "name": name if isinstance(name, str) and name else "structured_output",
            "schema": deepcopy(dict(schema)),
        }
        strict = spec.get("strict")
        if isinstance(strict, bool):
            out["strict"] = strict
        description = spec.get("description")
        if isinstance(description, str) and description:
            out["description"] = description
        return out
    return None


def inject_chat_system_instruction(body: dict[str, Any], instruction: str) -> None:
    """Append ``instruction`` into an OpenAI Chat body's system message."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        body["messages"] = [{"role": "system", "content": instruction}]
        return
    if (
        messages
        and isinstance(messages[0], dict)
        and messages[0].get("role") == "system"
    ):
        existing = messages[0].get("content")
        if isinstance(existing, str):
            messages[0]["content"] = (
                f"{existing}\n\n{instruction}" if existing else instruction
            )
            return
        if isinstance(existing, list):
            messages[0]["content"] = [*existing, {"type": "text", "text": instruction}]
            return
    messages.insert(0, {"role": "system", "content": instruction})


def apply_chat_structured_output_fallback(
    body: dict[str, Any],
) -> dict[str, Any] | None:
    """Clone Chat ``body`` without ``response_format``, injecting prompt fallback."""
    response_format = body.get("response_format")
    if not isinstance(response_format, Mapping):
        return None
    cloned = deepcopy(body)
    popped = cloned.pop("response_format", None)
    instruction = render_response_format_instruction(
        popped if isinstance(popped, Mapping) else None
    )
    if instruction:
        inject_chat_system_instruction(cloned, instruction)
    return cloned


def inject_anthropic_system_instruction(body: dict[str, Any], instruction: str) -> None:
    """Append ``instruction`` into an Anthropic Messages body's ``system`` prompt."""
    existing = body.get("system")
    if isinstance(existing, str):
        body["system"] = f"{existing}\n\n{instruction}" if existing else instruction
        return
    if isinstance(existing, list):
        body["system"] = [*existing, {"type": "text", "text": instruction}]
        return
    body["system"] = instruction


def apply_anthropic_structured_output_fallback(
    body: dict[str, Any],
    *,
    spec: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Clone Anthropic ``body`` without ``output_config.format`` + prompt fallback."""
    output_config = body.get("output_config")
    fmt = output_config.get("format") if isinstance(output_config, Mapping) else None
    if not isinstance(fmt, Mapping) and not isinstance(spec, Mapping):
        return None
    cloned = deepcopy(body)
    cloned_oc = cloned.get("output_config")
    if isinstance(cloned_oc, dict):
        cloned_oc.pop("format", None)
        if not cloned_oc:
            cloned.pop("output_config", None)
    effective = spec if isinstance(spec, Mapping) else fmt
    instruction = render_response_format_instruction(
        effective if isinstance(effective, Mapping) else None
    )
    if instruction:
        inject_anthropic_system_instruction(cloned, instruction)
    return cloned


def inject_responses_instructions(body: dict[str, Any], instruction: str) -> None:
    """Append ``instruction`` into a Responses body's ``instructions`` field."""
    existing = body.get("instructions")
    if isinstance(existing, str) and existing:
        body["instructions"] = f"{existing}\n\n{instruction}"
        return
    body["instructions"] = instruction


def apply_responses_structured_output_fallback(
    body: dict[str, Any],
) -> dict[str, Any] | None:
    """Clone Responses ``body`` without ``text.format``, injecting prompt fallback."""
    text = body.get("text")
    if not isinstance(text, Mapping) or not isinstance(text.get("format"), Mapping):
        return None
    cloned = deepcopy(body)
    cloned_text = cloned.get("text")
    fmt = cloned_text.pop("format", None) if isinstance(cloned_text, dict) else None
    if isinstance(cloned_text, dict) and not cloned_text:
        cloned.pop("text", None)
    instruction = render_response_format_instruction(
        fmt if isinstance(fmt, Mapping) else None
    )
    if instruction:
        inject_responses_instructions(cloned, instruction)
    return cloned


def has_tool_history(messages: Sequence[Mapping[str, Any]]) -> bool:
    """Return True if any message carries a ``tool_use`` or ``tool_result`` block."""
    for message in messages:
        content = (
            message.get("content")
            if isinstance(message, Mapping)
            else getattr(message, "content", None)
        )
        if not isinstance(content, list):
            continue
        for block in content:
            if get_block_type(block) in {"tool_use", "tool_result"}:
                return True
    return False


def synthesize_missing_history_tools(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Preserve ``tools`` and append minimal stubs for unseen historical tool names."""
    result: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for tool in tools or ():
        if isinstance(tool, Mapping):
            tool_dict = dict(tool)
            result.append(tool_dict)
            name = tool_dict.get("name")
            if isinstance(name, str) and name:
                seen_names.add(name)
    for message in messages:
        content = (
            message.get("content")
            if isinstance(message, Mapping)
            else getattr(message, "content", None)
        )
        if not isinstance(content, list):
            continue
        for block in content:
            if get_block_type(block) != "tool_use":
                continue
            name = get_block_attr(block, "name", None)
            if isinstance(name, str) and name and name not in seen_names:
                seen_names.add(name)
                result.append(
                    {
                        "name": name,
                        "input_schema": {"type": "object", "properties": {}},
                    }
                )
    return result


def disables_parallel_tool_use(tool_choice: Any) -> bool:
    """Return True if Anthropic ``tool_choice`` explicitly disables parallel calls."""
    return (
        isinstance(tool_choice, Mapping)
        and tool_choice.get("disable_parallel_tool_use") is True
        and tool_choice.get("type") != "none"
    )


def parse_pdf_data_url(url: str, *, error_context: str) -> tuple[str, str]:
    """Validate a ``data:application/pdf;base64,...`` URL and return ``(media_type, data)``."""
    if not url.startswith(_DATA_URL_PREFIX):
        raise StructuredOutputValidationError(
            f"{error_context} must be a data:application/pdf;base64,... URL",
            param="messages",
        )
    header, separator, data = url[len(_DATA_URL_PREFIX) :].partition(",")
    if not separator:
        raise StructuredOutputValidationError(
            f"{error_context} is a malformed data URL",
            param="messages",
        )
    if not header.endswith(";base64"):
        raise StructuredOutputValidationError(
            f"Only base64 data URLs are supported for {error_context}",
            param="messages",
        )
    media_type = header[: -len(";base64")].strip().lower()
    if media_type != "application/pdf":
        raise StructuredOutputValidationError(
            f"Unsupported document media type {media_type!r} in {error_context}; "
            "MCC accepts application/pdf.",
            param="messages",
        )
    if not data:
        raise StructuredOutputValidationError(
            f"{error_context} base64 payload must not be empty",
            param="messages",
        )
    try:
        base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise StructuredOutputValidationError(
            f"{error_context} data URL payload is not valid base64",
            param="messages",
        ) from exc
    return media_type, data


def is_http_url(url: str) -> bool:
    """Return True if ``url`` has an ``http`` or ``https`` scheme."""
    return urlsplit(url).scheme.lower() in {"http", "https"}


def document_block_pdf_filename(block: Any) -> str:
    """Return a clean PDF filename for an Anthropic ``document`` block."""
    title = get_block_attr(block, "title", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    return _DEFAULT_PDF_FILENAME
