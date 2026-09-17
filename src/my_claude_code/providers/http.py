"""Shared HTTP lifecycle helpers for upstream provider clients."""

import inspect
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from my_claude_code.core.trace import trace_event


async def maybe_await_aclose(response: Any) -> None:
    """Call ``aclose`` on httpx-like responses; ignore sync test doubles."""
    close = getattr(response, "aclose", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def close_provider_stream(
    stream: Any,
    *,
    active_error: BaseException | None,
    provider_name: str,
    request_id: str | None,
) -> None:
    """Close one stream without letting cleanup change its established outcome."""
    try:
        await maybe_await_aclose(stream)
    except Exception as close_error:
        active_error_type = (
            type(active_error).__name__ if active_error is not None else None
        )
        trace_event(
            stage="provider",
            event="provider.stream.close_failed",
            source="provider",
            provider=provider_name,
            request_id=request_id,
            close_exc_type=type(close_error).__name__,
            preserved_exc_type=active_error_type,
        )
        logger.warning(
            "{}_STREAM_CLOSE_FAILED request_id={} close_exc_type={} "
            "preserved_exc_type={}",
            provider_name,
            request_id,
            type(close_error).__name__,
            active_error_type,
        )


#: Raw bytes kept when a ``>=400`` body cannot be decoded. A refusal body is
#: evidence, not a log: enough to read a JSON error or recognise an HTML
#: challenge page, and bounded so a mislabelled megabyte cannot be carried in
#: an exception message.
ERROR_BODY_RAW_MAX_BYTES = 4096

#: Response headers copied onto the recorded head. Every one of these is set
#: by an origin or an edge and describes the *transport*; none of them can
#: carry a credential, which is why the list is an allow-list and not a
#: deny-list.
_HEAD_HEADERS: tuple[tuple[str, str], ...] = (
    ("content_type", "content-type"),
    ("content_encoding", "content-encoding"),
    ("content_length", "content-length"),
    ("transfer_encoding", "transfer-encoding"),
    ("server", "server"),
    ("cf_ray", "cf-ray"),
    ("cf_placement", "cf-placement"),
    ("retry_after", "retry-after"),
)

#: Raw bytes rendered as hex on the recorded head. 128 bytes is enough to tell
#: ``1f8b`` (gzip) from ``{"type"`` (JSON) from ``<html`` (a challenge page),
#: which is the whole question such a head is recorded to answer.
_HEAD_BODY_HEX_BYTES = 128

#: Per-header cap. A header is a short string by construction; a long one is a
#: fault and is cut rather than carried.
_HEAD_VALUE_MAX_CHARS = 200


@dataclass(frozen=True, slots=True)
class ErrorBody:
    """One ``>=400`` body, read in a way that cannot lose the status.

    ``content`` is the decoded body when the ``Content-Encoding`` told the
    truth, and the raw bytes, truncated, when it did not. ``decoded`` says
    which, and ``head`` is the response head recorded for the operator on the
    second case -- ``None`` on the first, so an ordinary refusal's try row is
    byte-identical to what it was before.
    """

    content: bytes
    decoded: bool = True
    head: dict[str, Any] | None = None


def _head_value(value: Any) -> str:
    text = str(value)
    if len(text) > _HEAD_VALUE_MAX_CHARS:
        return text[:_HEAD_VALUE_MAX_CHARS] + "…"
    return text


def response_head_record(
    status_code: int,
    headers: Any,
    raw: bytes,
    *,
    decode_error: str | None = None,
) -> dict[str, Any]:
    """The bounded, allow-listed head of one response, for the ladder.

    Only the transport-describing headers in :data:`_HEAD_HEADERS` are copied,
    and the body is rendered as hex rather than as text: a head is read to
    decide *what kind of thing* answered, and hex cannot accidentally render a
    token an origin echoed back.
    """

    record: dict[str, Any] = {"status": int(status_code)}
    getter = getattr(headers, "get", None)
    for name, header in _HEAD_HEADERS:
        value = getter(header) if callable(getter) else None
        if value:
            record[name] = _head_value(value)
    if raw:
        record["body_head_hex"] = raw[:_HEAD_BODY_HEX_BYTES].hex()
        record["body_bytes"] = len(raw)
    if decode_error:
        record["decode_error"] = _head_value(decode_error)
    return record


async def read_error_body(response: Any) -> ErrorBody:
    """Read the body of a ``>=400`` response without ever losing the status.

    ``httpx.Response.aread()`` decodes, and an edge that labels a body
    ``gzip`` when it is not makes that decode raise ``DecodingError`` --
    *before* the caller has built its ``HTTPStatusError``, so the status, the
    headers and the ``cf-ray`` all vanish and a refusal by the origin is filed
    as a fault of the model. 297 logged events, one address scoring 0/260 on
    one host while carrying 252 successes to three others in the same minutes,
    were this.

    So the bytes are taken off the wire *raw* and decoded afterwards, by
    httpx's own decoders, in a place where failing is allowed. The same
    re-wrap is what the caller needs anyway: a decoded body handed back to
    ``httpx.Response`` together with the original ``Content-Encoding`` header
    raises ``DecodingError`` at construction, which is the second, quieter half
    of the same bug.
    """

    headers = response.headers
    try:
        raw = b"".join([chunk async for chunk in response.aiter_raw()])
    except httpx.StreamError:
        # A response the client already read for us -- the non-streaming
        # branch, where httpx decoded inside ``send()`` and this frame never
        # had a choice. Hand back what it holds.
        return ErrorBody(content=await response.aread())
    try:
        decoded = httpx.Response(
            response.status_code, headers=headers, content=raw
        ).content
    except Exception as exc:
        return ErrorBody(
            content=raw[:ERROR_BODY_RAW_MAX_BYTES],
            decoded=False,
            head=response_head_record(
                response.status_code, headers, raw, decode_error=str(exc)
            ),
        )
    return ErrorBody(content=decoded)


def error_response_headers(headers: Any) -> list[tuple[str, str]]:
    """The response's headers, minus the two that describe bytes we replaced.

    ``read_error_body`` hands back a *decoded* body, so carrying the original
    ``Content-Encoding`` and ``Content-Length`` onto the re-wrapped response
    would describe it wrongly -- and httpx would then try to decode it a second
    time and raise. Everything else, including ``cf-ray`` and ``Retry-After``,
    is carried through untouched, because that is what the classifier and the
    operator read.
    """

    items = getattr(headers, "multi_items", None)
    pairs = (
        list(items())
        if callable(items)
        else list(getattr(headers, "items", lambda: [])())
    )
    return [
        (name, value)
        for name, value in pairs
        if name.lower() not in {"content-encoding", "content-length"}
    ]
