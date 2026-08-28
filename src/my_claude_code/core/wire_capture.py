"""Record the outbound request body actually handed to a provider SDK.

The dashboard used to report the *client's* numbers: ``max_tokens`` and the
tool count were read off the inbound Anthropic request before routing, before
the output-token budget, before every provider postprocessor. A client asking
for 64,000 tokens against a model capped at 16,384 was logged as 64,000, and
four separate investigations chased a request builder that was working fine.

This module closes that gap the same way credential attribution and stream
recovery close theirs: the API boundary installs one mutable collector for the
life of a request, and the provider writes into it from the commit boundary --
the last statement before the body crosses into the SDK, after every mutation
the body will ever see. Mutating one shared object stays visible through any
number of context copies, which a ``ContextVar`` holding an immutable value
would not be.

Two rules govern what is stored:

* **No prompt text.** Message and system content is reduced to structure
  (roles, block types, character counts). The prompt is already captured once,
  behind a "View" control; duplicating it here would inflate a 163 MB database
  for no new information.
* **No credentials, ever.** Bodies can carry keys in ``extra_body``, in a
  provider-specific auth field, or inside a header-ish blob. Redaction is by
  key *name* and by value *shape*, and it is applied to the whole tree.
"""

import json
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from my_claude_code.core.diagnostics import redact_sensitive_error_text

# The stored JSON per attempt. Generous enough for a 40-tool Claude Code
# request with its sampling and reasoning fields intact, small enough that
# 65,000 attempt rows stay a rounding error next to the prompt blobs.
MAX_WIRE_BODY_CHARS = 8_000

REDACTED = "<redacted>"

# A key whose *name* means its value is a credential, whatever the value looks
# like. Substring matching on purpose: providers spell the same idea
# ``api_key``, ``apiKey``, ``nvidia-api-key`` and ``X-Api-Key``.
_SECRET_KEY_SUBSTRINGS = (
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "auth_token",
    "authtoken",
    "access_token",
    "refresh_token",
    "id_token",
    "bearer",
    "secret",
    "password",
    "passwd",
    "credential",
    "private_key",
    "session_key",
    "cookie",
)

# A bare ``token`` or ``key`` key is ambiguous -- ``max_tokens`` must survive,
# and so must a routing ``key`` -- so those are matched exactly rather than as
# substrings.
_SECRET_KEY_EXACT = frozenset({"key", "token", "auth", "authentication"})

# Fields whose value is prompt text under some dialect. Their *structure* is
# kept; their characters are not.
_CONTENT_FIELDS = ("messages", "input", "system", "prompt", "instructions")

# Every spelling of "this request carries a reasoning instruction" across the
# dialects MCC speaks. Presence alone is not enough: an explicitly disabled
# field is the provider's way of saying reasoning was *not* requested.
_REASONING_KEYS = (
    "reasoning",
    "reasoning_effort",
    "reasoning_content",
    "thinking",
    "thinking_budget",
    "enable_thinking",
    "include_reasoning",
    "chat_template_kwargs",
)

_DISABLED_VALUES = frozenset({"none", "disabled", "off", "false", "0", ""})


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SECRET_KEY_EXACT:
        return True
    return any(marker in lowered for marker in _SECRET_KEY_SUBSTRINGS)


def redact_wire_value(value: Any) -> Any:
    """Recursively redact credentials by key name and by value shape.

    Two passes, because either alone leaks. A key named ``api_key`` is redacted
    whatever it holds, which covers a provider-specific auth field with an
    unrecognizable value; and every remaining string is run through the
    project's credential-shape scrubber, which covers a key smuggled into a
    field nobody thought to name (``sk-``, ``nvapi-``, ``gsk_``, ``Bearer x``).
    """
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            out[key] = REDACTED if _is_secret_key(key) else redact_wire_value(item)
        return out
    if isinstance(value, str):
        return redact_sensitive_error_text(value)
    if isinstance(value, list | tuple):
        return [redact_wire_value(item) for item in value]
    return value


def _text_shape(text: str) -> dict[str, Any]:
    return {"type": "text", "chars": len(text)}


def _block_shape(block: Any) -> dict[str, Any]:
    """Reduce one content block to what it is, not what it says."""
    if isinstance(block, str):
        return _text_shape(block)
    if not isinstance(block, Mapping):
        return {"type": type(block).__name__}
    kind = str(block.get("type") or "unknown")
    shape: dict[str, Any] = {"type": kind}
    # A tool call's *name* is structure and is worth keeping; its arguments are
    # generated text and are not.
    name = block.get("name")
    if isinstance(name, str):
        shape["name"] = name
    for field_name in ("text", "thinking", "content", "input", "arguments"):
        payload = block.get(field_name)
        if isinstance(payload, str):
            shape["chars"] = len(payload)
            break
        if isinstance(payload, list | dict):
            shape["chars"] = len(json.dumps(payload, default=str))
            break
    source = block.get("source")
    if isinstance(source, Mapping):
        shape["media_type"] = str(source.get("media_type") or "")
    return shape


def _content_shape(content: Any) -> Any:
    if isinstance(content, str):
        return _text_shape(content)
    if isinstance(content, Sequence) and not isinstance(content, str | bytes):
        return [_block_shape(block) for block in content]
    if isinstance(content, Mapping):
        return _block_shape(content)
    return None


def _tool_call_name(call: Any) -> str:
    if not isinstance(call, Mapping):
        return ""
    inner = call.get("function")
    source = inner if isinstance(inner, Mapping) else call
    return str(source.get("name") or "")


def _message_shape(message: Any) -> Any:
    if not isinstance(message, Mapping):
        return _content_shape(message)
    shape: dict[str, Any] = {}
    role = message.get("role")
    if role is not None:
        shape["role"] = str(role)
    kind = message.get("type")
    if kind is not None:
        shape["type"] = str(kind)
    name = message.get("name")
    if isinstance(name, str):
        shape["name"] = name
    if "content" in message:
        shape["content"] = _content_shape(message.get("content"))
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, str | bytes):
        shape["tool_calls"] = [{"name": _tool_call_name(c)} for c in tool_calls]
    return shape


def _tool_shape(tool: Any) -> Any:
    """Keep a tool's identity and parameter names; drop its prose."""
    if not isinstance(tool, Mapping):
        return {"name": str(tool)}
    inner = tool.get("function")
    source = inner if isinstance(inner, Mapping) else tool
    shape: dict[str, Any] = {"name": str(source.get("name") or "")}
    kind = tool.get("type")
    if kind is not None and kind != "function":
        shape["type"] = str(kind)
    schema = source.get("parameters")
    if not isinstance(schema, Mapping):
        schema = source.get("input_schema")
    if isinstance(schema, Mapping):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            shape["params"] = sorted(str(prop) for prop in properties)
    return shape


def strip_request_content(body: Mapping[str, Any]) -> dict[str, Any]:
    """Return the body with prompt text replaced by prompt structure.

    Everything that is not message or system content survives verbatim: model,
    ``max_tokens``, sampling parameters, stream flags, ``extra_body`` and every
    reasoning field. That is the whole point -- the debugging value is in the
    knobs, and the text is captured elsewhere.
    """
    out: dict[str, Any] = {}
    for raw_key, value in body.items():
        key = str(raw_key)
        if key == "tools":
            if isinstance(value, Sequence) and not isinstance(value, str | bytes):
                out[key] = [_tool_shape(tool) for tool in value]
            else:
                out[key] = value
            continue
        if key not in _CONTENT_FIELDS:
            out[key] = value
            continue
        if isinstance(value, str):
            out[key] = _text_shape(value)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            out[key] = [_message_shape(item) for item in value]
        else:
            out[key] = _content_shape(value)
    return out


def _is_reasoning_key(key: str) -> bool:
    lowered = key.lower()
    return any(lowered.startswith(name) for name in _REASONING_KEYS)


def _looks_enabled(value: Any) -> bool:
    """Decide whether a reasoning field is an instruction or a disablement."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _DISABLED_VALUES
    if isinstance(value, Mapping):
        if not value:
            return False
        kind = value.get("type")
        if isinstance(kind, str) and kind.strip().lower() in _DISABLED_VALUES:
            return False
        if value.get("enabled") is False:
            return False
        # A nested container (``chat_template_kwargs``) counts only if
        # something inside it is itself an enabled reasoning instruction.
        nested = [item for k, item in value.items() if _is_reasoning_key(str(k))]
        if nested:
            return any(_looks_enabled(item) for item in nested)
        return True
    if isinstance(value, list | tuple):
        return bool(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return True


def reasoning_was_emitted(body: Mapping[str, Any]) -> bool:
    """Report whether the outbound body carries a reasoning instruction.

    ``reasoning_adaptation`` records what gating *decided*; this records what
    the encoder actually *did*. They diverge whenever a provider's encoder
    discards the policy -- ``commandcode`` uses an encoder whose ``encode()``
    body is a bare ``return`` -- and ~23,000 requests logged a plausible-looking
    adaptation for a policy that was then thrown away.
    """
    for raw_key, value in body.items():
        if _is_reasoning_key(str(raw_key)) and _looks_enabled(value):
            return True
    extra = body.get("extra_body")
    return isinstance(extra, Mapping) and reasoning_was_emitted(extra)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# Sampling knobs worth pulling out of the body into the compact params summary,
# because they are the ones an operator scans for first.
_SAMPLING_FIELDS = (
    "temperature",
    "top_p",
    "top_k",
    "presence_penalty",
    "frequency_penalty",
    "repetition_penalty",
    "seed",
    "stop",
    "n",
)


def wire_params_summary(body: Mapping[str, Any]) -> dict[str, Any]:
    """Return the compact, always-cheap facts about one outbound body."""
    summary: dict[str, Any] = {}
    model = body.get("model")
    if model is not None:
        summary["model"] = str(model)
    max_tokens = _int_or_none(body.get("max_tokens"))
    if max_tokens is None:
        max_tokens = _int_or_none(body.get("max_completion_tokens"))
    if max_tokens is None:
        max_tokens = _int_or_none(body.get("max_output_tokens"))
    if max_tokens is not None:
        summary["max_tokens"] = max_tokens
    tools = body.get("tools")
    if isinstance(tools, Sequence) and not isinstance(tools, str | bytes):
        summary["tools"] = len(tools)
    for name in _SAMPLING_FIELDS:
        value = body.get(name)
        if value is not None:
            summary[name] = redact_wire_value(value)
    reasoning: dict[str, Any] = {}
    for raw_key, value in body.items():
        key = str(raw_key)
        if _is_reasoning_key(key) and value is not None:
            reasoning[key] = redact_wire_value(value)
    extra = body.get("extra_body")
    if isinstance(extra, Mapping):
        for raw_key, value in extra.items():
            key = str(raw_key)
            if _is_reasoning_key(key) and value is not None:
                reasoning[f"extra_body.{key}"] = redact_wire_value(value)
    if reasoning:
        summary["reasoning"] = reasoning
    return summary


def summarize_wire_body(body: Mapping[str, Any]) -> str:
    """Serialize the redacted, text-free body, capped and visibly truncated."""
    safe = redact_wire_value(strip_request_content(body))
    text = json.dumps(safe, default=str, sort_keys=True)
    if len(text) <= MAX_WIRE_BODY_CHARS:
        return text
    return json.dumps(
        {
            "_truncated": True,
            "_original_chars": len(text),
            "_limit": MAX_WIRE_BODY_CHARS,
            "_preview": text[:MAX_WIRE_BODY_CHARS],
        }
    )


@dataclass(slots=True)
class WireRequest:
    """One outbound body, as it was handed to the provider SDK."""

    params: dict[str, Any]
    body_json: str
    reasoning_emitted: bool


@dataclass(slots=True)
class WireTrace:
    """Mutable per-request collector of outbound bodies, keyed by attempt."""

    current_attempt: int = 0
    requests: dict[int, WireRequest] = field(default_factory=dict)

    def record(self, body: Mapping[str, Any]) -> None:
        # Last write wins: a create-level retry rewrites the body (dropping
        # ``stream_options``, lowering an output cap), and the body that
        # produced the attempt's outcome is the last one sent, not the first.
        self.requests[self.current_attempt] = WireRequest(
            params=wire_params_summary(body),
            body_json=summarize_wire_body(body),
            reasoning_emitted=reasoning_was_emitted(body),
        )


_WIRE_TRACE: ContextVar[WireTrace | None] = ContextVar("fcc_wire_trace", default=None)


def install_wire_trace() -> WireTrace:
    """Start recording outbound bodies for the current request."""
    slot = WireTrace()
    _WIRE_TRACE.set(slot)
    return slot


def record_wire_request(body: Mapping[str, Any], **extra: Any) -> None:
    """Record the body about to cross into a provider SDK, if tracked.

    A no-op outside a tracked request, so providers exercised directly (unit
    tests, token counting, model discovery) need no special handling. ``extra``
    carries arguments the SDK receives alongside the body dict -- ``stream=True``
    is passed as a keyword, not packed into it -- so the recorded body is the
    whole call rather than most of it.
    """
    slot = _WIRE_TRACE.get()
    if slot is None:
        return
    slot.record({**body, **extra} if extra else body)
