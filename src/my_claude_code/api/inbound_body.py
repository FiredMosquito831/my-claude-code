"""The client's own inbound body, recorded beside the outbound one (8.8.0).

``LOG_RAW_API_PAYLOADS`` records the body MCC *sent upstream* -- and, on the
three OpenAI-shaped doors, a pydantic ``model_dump`` of the client's body with
``exclude_none=True``, which is not the client's bytes either: every explicit
``null`` it sent is gone and the key order is pydantic's. The client's octets
were stored nowhere at all. The user's decision of 2026-11-01 (question 5) is
that "raw input on both sides" must finally mean both sides, so this records
the inbound body as a second payload line, under the ``INBOUND_PAYLOAD [<id>]:``
prefix -- the same ``RAW_PAYLOAD`` marker as the outbound line, a different
word, so the Payloads box can say which side of the exchange a line belongs to.

Three properties matter and each shapes the code:

* **Only the model doors.** A dashboard poll, a static asset, a health check:
  none of them is a model request and none of them has a "prompt". Only the
  four paths a model body can arrive on are buffered.
* **Only while the setting is on.** With ``LOG_RAW_API_PAYLOADS`` off the
  middleware hands ``receive`` back untouched -- one attribute lookup per
  request and not one buffered byte -- so the equality with the previous
  release is a property of the code, not a measurement anyone has to trust.
* **A body is replayed, never consumed.** This wrapper returns every ASGI
  message unchanged after copying its chunk, so the app downstream reads
  exactly what arrived: a listener, not a reader.
"""

import hashlib
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from loguru import logger

#: The message prefix the raw sink recognises as a payload line. Kept in step
#: with ``config/logging_config._RAW_PAYLOAD_PREFIXES``.
PREFIX = "INBOUND_PAYLOAD ["

#: The four paths a model body can arrive on. The Gemini family's
#: ``/v1beta/models/<model>:<method>`` is matched by prefix, so one rule covers
#: ``:generateContent``, ``:streamGenerateContent`` and ``:countTokens``.
MODEL_DOOR_PATHS: tuple[str, ...] = (
    "/v1/messages",
    "/v1/responses",
    "/v1/chat/completions",
    "/v1beta/models/",
)

#: One inbound body larger than this is recorded as a length and a digest
#: instead of being written. A model request carrying images is large, and a
#: body of megabytes inside one physical line is exactly what the reader's
#: ``LINE_LIMIT_BYTES`` has to refuse -- so the refusal is stated here, in the
#: file, where the box can explain it.
MAX_BODY_BYTES = 16 * 1024 * 1024


def is_model_door(path: str) -> bool:
    """Whether a body arriving on this path is a model request's."""

    return path.startswith(MODEL_DOOR_PATHS)


def body_line(request_id: str, data: bytes) -> str:
    """One inbound body as one record, escaped as the raw sink escapes a line."""

    if len(data) > MAX_BODY_BYTES:
        digest = hashlib.sha256(data).hexdigest()
        return (
            f"{PREFIX}{request_id}]: OVERSIZE bytes={len(data)} sha256={digest} "
            f"(above the {MAX_BODY_BYTES // (1024 * 1024)} MB one line holds; the "
            "client's body is not shown)"
        )
    text = data.decode("utf-8", "surrogateescape")
    return (
        f"{PREFIX}{request_id}]: {text.replace(chr(13), '\\r').replace(chr(10), '\\n')}"
    )


def wrap_receive(
    receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
    path: str,
    *,
    request_id: str,
    enabled: bool,
) -> Callable[[], Awaitable[MutableMapping[str, Any]]]:
    """A ``receive`` that records the inbound body while passing it through.

    Returns the caller's own function when nothing should be recorded, so the
    off case is the identity function and cannot drift from it.
    """

    if not enabled or not is_model_door(path):
        return receive

    parts: list[bytes] = []
    recorded = False

    async def watched() -> MutableMapping[str, Any]:
        nonlocal recorded
        message = await receive()
        if message.get("type") == "http.request" and not recorded:
            chunk = message.get("body", b"")
            if isinstance(chunk, bytes) and chunk:
                parts.append(chunk)
            if not message.get("more_body", False):
                recorded = True
                _record(request_id, b"".join(parts))
        return message

    return watched


def _record(request_id: str, data: bytes) -> None:
    """Write the line. Total by construction: a logging fault never fails a request."""

    if not data:
        return
    try:
        line = body_line(request_id, data)
    except Exception:  # pragma: no cover - a body that cannot be decoded
        return
    logger.debug("{}", line)
