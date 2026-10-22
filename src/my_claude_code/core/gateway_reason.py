"""Recognise a gateway 4xx that passed on a trace id and no reason.

Some gateways hide the model host's real error and pass on a generic phrase
and an id. Command Code wraps DeepSeek's refusal as a JSON string inside a
JSON string::

    {"message":"{\\"message\\":\\"invalid request error trace_id: <32 hex>\\",
     \\"type\\":\\"invalid_request_error\\"}\\n","type":"invalid_request_error"}

1,204 of the attempts in the request log on 2026-10-22 carried exactly that,
and nothing else. MCC kept the text whole and labelled it "This model rejected
the request.", so nobody could tell the reason had been withheld before it
reached MCC, or find the id to quote to the gateway's support.

This module reads the attempt's recorded ``error_message`` (the copyable
diagnostic :func:`~my_claude_code.core.diagnostics.format_execution_failure_message`
writes: a status line, then the upstream body), unwraps JSON nested in string
fields, and answers the id only when the body says nothing else:

* every reason-carrying field (``message``, ``error``, ``detail``, ``param``,
  ...) is one declared generic phrase plus an id, and all name the same id;
* every category field (``type``, ``code``, ``status``) is a generic
  invalid-request category, or the status number itself;
* no other field is present. A field path, a ``metadata`` block, a reason
  text, a truncated body: any of these means the host did say something, and
  the text is left to speak for itself.

Classification, charging, rotation and fallback never read this; it is a
label and one parsed field (``params.gateway_trace_id``), nothing more.

The shapes are data (:data:`NO_REASON_SHAPES`): a row per gateway phrase, the
statuses it was measured at, and the evidence. Adding a gateway is adding a
row. Measured on 2026-10-22 over the last 60 days of the request log:

* Command Code's DeepSeek route, HTTP 400, ``invalid request error trace_id:``
  plus 32 lowercase hex: 1,204 attempts;
* B.AI (a new-api gateway), HTTP 400, ``Invalid request body. (request id:
  ...)`` plus 38-39 letters and digits: 12 attempts.

Searched for and absent in those 60 days, so not declared: OpenRouter's
``{"error":{"message":"Provider returned error","metadata":{"raw":...}}}``
(its 4xx there are all 404s with a reason), Nous Portal (every id-carrying
body names a reason, ``Application not found``) and NVIDIA NIM (no id-only
body). Bodies that carry an id beside a reason stay unlabelled by design:
Command Code's ``... is not a "regex" (request_id: ...)``, Agnes's
``max_tokens exceeds the limit of ... (request id: ...)``, Anthropic's
top-level ``request_id`` beside its message.
"""

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class NoReasonShape:
    """One gateway's id-only wording.

    ``message`` must match a reason field in full and name the id in a group
    called ``id``. ``statuses`` are the HTTP statuses the shape was measured
    at: a 429 or a 5xx says something by its status alone, so a shape is never
    assumed beyond what was observed.
    """

    name: str
    statuses: frozenset[int]
    message: re.Pattern[str]
    evidence: str


NO_REASON_SHAPES: tuple[NoReasonShape, ...] = (
    NoReasonShape(
        name="commandcode-trace-id",
        statuses=frozenset({400}),
        message=re.compile(r"invalid request error trace_id: (?P<id>[0-9a-f]{16,64})"),
        evidence=(
            "Command Code (commandcode/deepseek/deepseek-v4.1-flash), HTTP 400, "
            "1,204 attempts in the 60 days to 2026-10-22, nested JSON in a string"
        ),
    ),
    NoReasonShape(
        name="new-api-request-id",
        statuses=frozenset({400}),
        message=re.compile(
            r"Invalid request body\. \(request id: (?P<id>[0-9A-Za-z]{16,64})\)"
        ),
        evidence=(
            "B.AI (custom_b_ai, a new-api gateway), HTTP 400, 12 attempts in the "
            "60 days to 2026-10-22"
        ),
    ),
)

# Fields whose words are the host's reason. Any one of them carrying text that
# is not an id-only phrase means the host gave a reason.
_REASON_KEYS = frozenset(
    {
        "message",
        "error",
        "errors",
        "detail",
        "details",
        "msg",
        "reason",
        "param",
        "loc",
        "title",
        "description",
    }
)

# Fields that name a category rather than a reason. Allowed only when the
# category is the generic one: a ``code`` of ``context_length_exceeded`` beside
# an id-only message is a reason, and the label would hide it.
_CATEGORY_KEYS = frozenset({"type", "code", "status", "status_code", "statuscode"})
_GENERIC_CATEGORIES = frozenset(
    {"invalid_request_error", "invalid_request", "api_error", "bad_request"}
)

_STATUS_LINE = re.compile(r"Upstream provider .+ returned HTTP (?P<status>\d{3})\.")
_BODY_MARKER = "\nUpstream error:\n"
_REQUEST_ID_MARKER = "\n\nRequest ID: "
_TRUNCATED_MARKER = "... [truncated after "

# How deep a JSON-in-a-string envelope is unwrapped. Command Code nests two
# levels; a bound keeps a pathological body from recursing without end.
_MAX_DEPTH = 6


def gateway_trace_id(error_message: str | None) -> str | None:
    """The id a gateway passed on instead of a reason, or ``None``.

    ``None`` whenever the recorded message is not an upstream 4xx diagnostic,
    the body was truncated, or the body says anything beyond one declared
    id-only phrase -- unsure is never labelled.
    """
    if not isinstance(error_message, str) or not error_message:
        return None
    status = _status_of(error_message)
    if status is None:
        return None
    body = _body_of(error_message)
    if body is None:
        return None
    for shape in NO_REASON_SHAPES:
        if status not in shape.statuses:
            continue
        found = _id_only(body, shape, status)
        if found is not None:
            return found
    return None


def _status_of(error_message: str) -> int | None:
    first_line = error_message.split("\n", 1)[0]
    match = _STATUS_LINE.fullmatch(first_line)
    return int(match.group("status")) if match else None


def _body_of(error_message: str) -> str | None:
    at = error_message.find(_BODY_MARKER)
    if at < 0:
        return None
    body = error_message[at + len(_BODY_MARKER) :]
    end = body.find(_REQUEST_ID_MARKER)
    if end >= 0:
        body = body[:end]
    if _TRUNCATED_MARKER in body:
        return None
    body = body.strip()
    return body or None


def _id_only(body: str, shape: NoReasonShape, status: int) -> str | None:
    found: set[str] = set()
    for key, value in _leaves(_parse(body), key="message", depth=0):
        if value is None or value == "":
            continue
        name = key.lower()
        if name in _REASON_KEYS:
            if not isinstance(value, str):
                return None
            match = shape.message.fullmatch(value.strip())
            if match is None:
                return None
            found.add(match.group("id"))
        elif name in _CATEGORY_KEYS:
            if not _generic_category(value, status):
                return None
        else:
            return None
    return found.pop() if len(found) == 1 else None


def _generic_category(value: Any, status: int) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == status
    if isinstance(value, str):
        text = value.strip().lower()
        return text in _GENERIC_CATEGORIES or text == str(status)
    return False


def _parse(text: str) -> Any:
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return text
    try:
        return json.loads(stripped)
    # A pathologically nested body raises RecursionError, not ValueError; this
    # runs on the failure path, where raising would turn a refusal into a 500.
    except ValueError, RecursionError:
        return text


def _leaves(value: Any, *, key: str, depth: int) -> Iterator[tuple[str, Any]]:
    """Every (field, scalar) pair, JSON nested inside strings unwrapped.

    A string that parses as an object or a list is read as one: that is the
    whole of Command Code's envelope. A list keeps its parent's field name, so
    ``errors: ["..."]`` is still a reason field.
    """
    if depth > _MAX_DEPTH:
        yield key, value
        return
    if isinstance(value, str):
        parsed = _parse(value)
        if parsed is value:
            yield key, value
        else:
            yield from _leaves(parsed, key=key, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        for name, item in value.items():
            yield from _leaves(item, key=str(name), depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        for item in value:
            yield from _leaves(item, key=key, depth=depth + 1)
        return
    yield key, value


__all__ = ("NO_REASON_SHAPES", "NoReasonShape", "gateway_trace_id")
