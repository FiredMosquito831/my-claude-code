"""The provider's own bytes, captured at the transport (8.8.0).

Every streaming door in MCC ends up with someone else's bytes: ``httpx`` hands
back chunks, a reader frames them into SSE, a converter turns the frames into
MCC's own event vocabulary, and the client receives a re-encoding. Until this
release nothing wrote the first link of that chain down, so "the raw log" held
MCC's spelling of the answer and never the answer. ``LOG_RAW_UPSTREAM_BYTES``
closes that gap: one ``RAW_UPSTREAM`` line per transport chunk, written before
anything parses it.

Three rules decide the shape, and each one has a counter-example it exists to
keep out:

* **Bytes, not a rebuild.** What is written is the chunk the transport yielded.
  A stream is reassembled by concatenating a request's lines in order, and the
  result equals what the host sent -- comments, ``id:``/``retry:`` fields,
  ``data: `` prefixes, ``[DONE]`` and all. The Messages door's rebuilt frames
  (``anthropic_messages/streaming.py``) stay exactly as they are; they are a
  different artefact and this one does not derive from them.
* **Additive or nothing.** With the setting off, :func:`capture_chunks` returns
  the very iterator it was given, so the emit sites are one ``if`` and the
  existing files, lines and records are byte-identical.
* **A cap that refuses, never trims.** A chunk larger than
  ``UPSTREAM_CHUNK_MAX_BYTES`` is not written in pieces and not written short:
  it is written as ``UPSTREAM_OVERSIZE`` with its true length and SHA-256, so a
  reader knows the stream has a hole and how large it is, and the viewer says
  so in the box. Silent truncation would be a lie about a file whose whole
  purpose is that nothing is lost.

Text is escaped the way the raw sink escapes every line (``\\r``/``\\n``), so a
chunk holding a newline still occupies exactly one physical line and a reader
can split the file safely.
"""

import codecs
import contextlib
import hashlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from loguru import logger

from my_claude_code.config.logging_config import upstream_capture_enabled
from my_claude_code.config.upstream_doors import (
    DOOR_ANTHROPIC_MESSAGES,
    DOOR_CHATGPT_OAUTH,
    DOOR_MEDIA,
    DOOR_OPENAI_CHAT,
    DOOR_OPENAI_RESPONSES,
    DOOR_REFUSAL,
    DOOR_RESPONSES_TRANSPORT,
    door_label,
)

__all__ = [
    "DOOR_ANTHROPIC_MESSAGES",
    "DOOR_CHATGPT_OAUTH",
    "DOOR_MEDIA",
    "DOOR_OPENAI_CHAT",
    "DOOR_OPENAI_RESPONSES",
    "DOOR_REFUSAL",
    "DOOR_RESPONSES_TRANSPORT",
    "MEDIA",
    "OVERSIZE",
    "PREFIX",
    "REFUSAL",
    "UPSTREAM_CHUNK_MAX_BYTES",
    "capture_chunks",
    "capture_enabled",
    "chunk_bytes_of",
    "chunk_line",
    "describe_door",
    "emit",
    "escape_line",
    "header_fields",
    "is_media",
    "is_oversize",
    "is_refusal",
    "line_door",
    "media_line",
    "record_refusal_bytes",
    "refusal_line",
]

#: The line prefix the raw sink recognises. Kept beside the marker in
#: ``config/logging_config.py``; duplicated here so this module needs no
#: import from the logging layer on a request's hot path.
PREFIX = "UPSTREAM: "

#: A chunk larger than this is recorded as a length + digest instead of being
#: written. One SSE chunk is a few hundred bytes; a whole non-streamed JSON
#: body can be megabytes, and a media answer must never arrive here at all
#: (the media door records metadata only, by the user's decision). 4 MB is
#: generous for any protocol frame and bounds one line of the file.
UPSTREAM_CHUNK_MAX_BYTES = 4 * 1024 * 1024

#: What an oversize chunk's line says instead of its bytes.
OVERSIZE = "OVERSIZE"

#: What a refusal body's line says when the body itself was logged on another
#: line -- the refusal door's own shape, so a reader can tell the two apart.
REFUSAL = "REFUSAL"

#: What a media answer's line says: metadata only, never the body.
MEDIA = "MEDIA"


def capture_enabled(own_flag: bool = True) -> bool:
    """Whether a particular door should write, right now.

    Two facts decide it, and both must hold: the process switch (the setting,
    which ``configure_raw_diagnostics`` keeps current so a Save reaches a
    running server) and the call site's own opt-in. Passing ``own_flag=False``
    lets the process switch alone gate a call site that has nothing extra to
    say -- the refusal paths, which always apply when the setting is on.
    """

    return own_flag and upstream_capture_enabled()


def escape_line(data: str) -> str:
    """One chunk as one physical line, the way the raw sink escapes a record."""

    return data.replace("\r", "\\r").replace("\n", "\\n")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def chunk_line(door: str, data: bytes) -> str:
    """The line for one transport chunk: door, length, and its text.

    ``data`` is a *complete* UTF-8 sequence, never a fragment of one:
    :func:`capture_chunks` reassembles split characters with an incremental
    decoder before this is called, because a transport hands over whatever its
    socket delivered and a chunk boundary legitimately falls inside a multi-byte
    character. ``surrogateescape`` remains as the last-resort decode for bytes
    that are not UTF-8 at all (a host that answers in Latin-1): the round trip
    through :func:`chunk_bytes_of` is then still exact.

    What is written is the *text*, not an escape of the bytes. A log line is a
    string, and a stream is reassembled by concatenating the decoded chunks --
    which is exactly the answer the provider sent, character for character.
    """

    if len(data) > UPSTREAM_CHUNK_MAX_BYTES:
        return f"{OVERSIZE} door={door} bytes={len(data)} sha256={_sha256(data)}"
    text = escape_line(data.decode("utf-8", "surrogateescape"))
    return f"door={door} bytes={len(data)} {text}"


def refusal_line(door: str, status: int, data: bytes) -> list[str]:
    """The lines for a refusal body: its head, then the undecoded bytes whole.

    Returned as two lines rather than one so a reader can find the refusal by
    status without loading the body, and so a very large refusal (an HTML error
    page behind a CDN) is still one line of text. The body is never capped
    here -- the user's decision is that a refusal is raw output and is recorded
    whole in the raw log -- and never enters the request database.
    """

    head = (
        f"{REFUSAL} door={door} status={status} bytes={len(data)} "
        f"sha256={_sha256(data)}"
    )
    return [head, f"{REFUSAL} door={door} body {chunk_line(door, data)}"]


def media_line(
    door: str,
    *,
    status: int,
    content_type: str,
    nbytes: int,
    refusal_head: bytes = b"",
) -> list[str]:
    """Metadata for one media answer, and the first 4 KB of a refusal.

    Media bytes are deliberately NOT recorded (user decision 4 of 2026-11-01):
    an image is 1.3-1.7 MB, more than a normal request's entire raw record, and
    the user asked for what the host said about the answer rather than the
    answer. So: status, content type and length always; the leading bytes only
    when the answer is a refusal, where the body is the diagnostic.
    """

    head = (
        f"{MEDIA} door={door} status={status} bytes={nbytes} "
        f"content_type={escape_line(content_type) or '-'}"
    )
    if not refusal_head:
        return [head]
    return [head, f"{MEDIA} door={door} refusal_head {chunk_line(door, refusal_head)}"]


def emit(line: str) -> None:
    """Write one upstream line, if and only if the caller already decided to.

    The marker and the request id come from the raw sink's formatter; this
    module only supplies the message. DEBUG is the level ``configure_raw_
    diagnostics`` installs its sink at, and the sink's own filter is what
    decides whether the record reaches a file at all, so an unfiltered DEBUG
    record here costs nothing when the setting is off -- it is never called
    then, which is the point.
    """

    logger.debug("{}{}", PREFIX, line)


def capture_chunks(
    chunks: AsyncIterator[bytes],
    door: str,
    *,
    enabled: bool,
) -> AsyncIterator[bytes]:
    """Pass a door's chunks through, writing each one when the setting is on.

    Returns the iterator unchanged when the setting is off: the caller compares
    nothing, allocates nothing and awaits nothing extra, so the bytes on the
    wire and the timing of the stream are exactly what they were before this
    function existed. That is the equality the release is judged on, and it is
    why this is a plain function rather than a coroutine -- an off switch must
    not add an ``await`` to a per-chunk path.
    """

    if not enabled:
        return chunks

    #: One decoder per stream, so a character split across two transport chunks
    #: is reassembled before it is written. This is the measured bug it fixes:
    #: a 17-byte chunk boundary landing inside a three-byte character produced
    #: ``UnicodeEncodeError: surrogates not allowed`` from the log sink, which
    #: would have lost that chunk entirely on a real stream.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="surrogateescape")

    async def watched() -> AsyncIterator[bytes]:
        try:
            async for chunk in chunks:
                if chunk:
                    pending = decoder.decode(chunk)
                    if pending:
                        emit(
                            chunk_line(door, pending.encode("utf-8", "surrogateescape"))
                        )
                yield chunk
        finally:
            # A stream that ends mid-character (a provider cut off inside a
            # multi-byte sequence, or a client that closed) leaves that
            # fragment in the decoder. Flushed with the same escaping so the
            # recorded stream is what actually arrived -- a trailing replacement
            # character would be a byte the host never sent. Total: a flush on
            # an abandoned stream must never raise into the request.
            with contextlib.suppress(Exception):
                tail = decoder.decode(b"", final=True)
                if tail:
                    emit(chunk_line(door, tail.encode("utf-8", "surrogateescape")))

    return watched()


def describe_door(door: str) -> str:
    """``Anthropic Messages`` for a raw line's ``door=`` value, tolerantly."""

    return door_label(door)


def line_door(line: str) -> str:
    """The ``door=`` field of a ``RAW_UPSTREAM`` message, or ``""``.

    Tolerant by design: this reads lines written by other releases, including
    ones that had no ``door=`` field at all, and a reader must never raise on
    a file it did not write.
    """

    at = line.find("door=")
    if at < 0:
        return ""
    rest = line[at + 5 :]
    end = rest.find(" ")
    return rest if end < 0 else rest[:end]


def is_oversize(line: str) -> bool:
    return line.startswith(OVERSIZE)


def is_refusal(line: str) -> bool:
    return line.startswith(REFUSAL)


def is_media(line: str) -> bool:
    return line.startswith(MEDIA)


def chunk_bytes_of(line: str) -> tuple[str, bytes] | None:
    """``(door, the chunk's bytes)`` for a chunk line, or ``None`` if it is not one.

    A round trip through the same escaping the writer used: this is what makes
    "every page concatenated equals a direct read" provable for the byte line,
    because the reader hands back exactly what the writer encoded.
    """

    if is_oversize(line) or is_refusal(line) or is_media(line):
        return None
    if not line.startswith("door="):
        return None
    door = line_door(line)
    after_door = line[len("door=") + len(door) :]
    if not after_door.startswith(" bytes="):
        return None
    rest = after_door[len(" bytes=") :]
    space = rest.find(" ")
    if space < 0:
        return None
    text = rest[space + 1 :]
    raw = text.replace("\\r", "\r").replace("\\n", "\n")
    return door, raw.encode("utf-8", "surrogateescape")


def header_fields(line: str) -> dict[str, Any]:
    """The ``key=value`` fields of an oversize/refusal/media head line."""

    fields: dict[str, Any] = {}
    for token in line.split(" ")[1:]:
        name, sep, value = token.partition("=")
        if not sep:
            continue
        fields[name] = value
    return fields


def record_refusal_bytes(
    error: BaseException,
    door: str,
    emit_line: Callable[[str], None],
    build: Callable[[str, int, bytes], list[str]],
) -> None:
    """Record a refusal's body off an SDK error, when one survived.

    The OpenAI SDK reads and parses an error body before it raises, so the only
    copy left is ``error.response.content`` -- the decoded bytes, which is the
    closest thing to "as received" this door can offer and is what the request
    never stored. Total by construction: every step is optional and nothing in
    this function may raise, because it runs on a failure path where a new
    exception would turn a real upstream refusal into an internal 500.
    """

    response = getattr(error, "response", None)
    if response is None:
        return
    content = getattr(response, "content", None)
    if not isinstance(content, bytes) or not content:
        return
    status = getattr(response, "status_code", 0)
    if not isinstance(status, int) or status < 400:
        return
    for line in build(door, status, content):
        emit_line(line)
