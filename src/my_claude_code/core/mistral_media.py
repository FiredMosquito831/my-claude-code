"""Mistral's ``image_generation`` built-in tool as a media upstream.

Mistral has no ``/v1/images/generations``. An image is asked for on Chat
Completions, as the ``image_generation`` built-in tool, and the answer is a
chat answer: a ``choices[0].message`` whose ``tool_calls`` name the file the
host made, plus whatever prose the model wrote around it. The bytes are then a
SECOND call -- ``GET {base}/files/{file_id}/content`` -- which the leaf makes on
the key and the proxy leg that answered the chat call.

Three answer forms are handled, because no offline source states which one
Mistral returns (spec E11):

* a **file id**, wherever the answer names one -- ``tool_calls[].function
  .arguments`` (a JSON string, a JSON object, or a bare id), a ``file_id`` key
  anywhere in the message, or a ``/files/<id>`` path in the prose;
* **inline base64** (``b64_json``, or an image part), which needs no second
  call;
* a **URL**, handed to the leaf's existing inliner.

The parser SEARCHES rather than assuming one path, and the leaf records what
it found, so an answer form this module never saw either works or fails
loudly -- it is never silently dropped.

Pure and synchronous; everything that touches a whole answer body runs through
``asyncio.to_thread``. Sibling of ``core/gemini_native_media.py``, which exists
for the same reason: ``application`` may not import ``providers``, so an
adapter's parsing lives in ``core`` where both can read it.
"""

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: A Mistral file id as the answers name one: the id itself is opaque, so this
#: is deliberately loose (any run of URL-safe characters) and only ever read
#: from a place the answer has already marked as a file reference.
_FILE_ID = re.compile(r"[A-Za-z0-9_-]{6,}")

#: A ``/files/<id>`` path in prose or in an argument string.
_FILE_PATH = re.compile(r"/files/([A-Za-z0-9_-]{6,})(?:/content)?(?:[?#]|$|\s|\"|')")

#: Keys an answer may name the file under, in the order they are tried.
_FILE_KEYS = ("file_id", "fileId", "file", "id")

#: Keys an answer may carry already-encoded bytes under.
_INLINE_KEYS = ("b64_json", "b64", "image_base64", "base64")

#: Keys an answer may carry a picture's address under.
_URL_KEYS = ("url", "image_url", "download_url")


class MistralAnswerError(ValueError):
    """The answer holds nothing this request can be served with.

    The message completes "<provider> answered ...", e.g. "without an image".
    """


@dataclass(frozen=True, slots=True)
class MistralImageAnswer:
    """What a chat answer said about the picture, before any byte is fetched.

    Exactly one of ``file_id`` / ``inline_bytes`` / ``url`` is normally set;
    an answer carrying several is read in that order (bytes already in hand
    cost nothing, a file id is the documented route, a URL is last).
    ``text`` is the prose the model wrote around the tool call -- it is logged
    on the row's ``output_text`` and is never added to the client's JSON.
    """

    file_id: str | None = None
    inline_bytes: bytes = b""
    url: str | None = None
    text: str | None = None
    usage: dict[str, Any] | None = None
    #: The answer's own ``created``, when it states one: the client's Images
    #: answer carries it, and nothing here invents a clock.
    created: int | None = None


# ----------------------------------------------------------------- requests


def image_tool_request(prompt: str, model: str, tool_type: str) -> dict[str, Any]:
    """The Chat Completions body that asks for one picture.

    The client's ``prompt`` becomes ONE user message and nothing else is sent:
    the tool has no conversation, so there is no system prompt and no history.
    ``stream`` is explicitly off -- the tool's streaming vocabulary is not
    documented, and a stream MCC cannot parse would be a guess.

    ``tool_type`` is passed in rather than imported: ``core`` imports nothing
    from ``config`` (III.5), so the spelling stays the ONE declared constant
    in ``config/media_surfaces.py`` and the caller hands it over.
    """

    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{"type": tool_type}],
        "stream": False,
    }


# ------------------------------------------------------------------ answers


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _payload(raw: bytes) -> dict[str, Any]:
    try:
        payload = _mapping(json.loads(raw))
    except ValueError, UnicodeDecodeError:
        payload = None
    if payload is None:
        raise MistralAnswerError("with a body that is not a JSON object")
    return payload


def _message(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The first choice's message; ``{}`` when the answer carries none."""

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return {}
    choice = _mapping(choices[0])
    if choice is None:
        return {}
    return _mapping(choice.get("message")) or {}


def _text_of(content: Any) -> str:
    """A message's prose: a string, or the text parts of a content list."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        part = _mapping(item)
        if part is None:
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def _decode(value: Any) -> bytes:
    """Base64 that decodes to something; ``b""`` for anything else."""

    if not isinstance(value, str) or not value:
        return b""
    try:
        return base64.b64decode(value, validate=False) or b""
    except binascii.Error, ValueError:
        return b""


def _file_id_in(value: Any) -> str | None:
    """The file id an argument value names, if it names one.

    ``value`` is what a tool call's ``arguments`` held: a JSON string, a JSON
    object, or a bare id. A JSON object is searched under a file-shaped key
    first, then in its nested objects, then for a ``/files/<id>`` path -- a
    bare id-shaped string is only believed when it is the WHOLE value or sits
    under a file-shaped key, because an ordinary model name is id-shaped too
    and a wrong file id would be a charged fetch of nothing.
    """

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped[0] in "{[":
            try:
                return _file_id_in(json.loads(stripped))
            except ValueError:
                return _file_path_in(stripped)
        return _file_path_in(stripped) or _bare_id(stripped)
    found = _mapping(value)
    if found is None:
        return None
    for key in _FILE_KEYS:
        named = found.get(key)
        if isinstance(named, str) and named.strip():
            return _file_path_in(named) or _bare_id(named)
    for item in found.values():
        if isinstance(item, dict):
            nested = _file_id_in(item)
            if nested is not None:
                return nested
        elif isinstance(item, str):
            path = _file_path_in(item)
            if path is not None:
                return path
    return None


def _file_path_in(text: str) -> str | None:
    match = _FILE_PATH.search(text)
    return match.group(1) if match else None


def _bare_id(text: str) -> str | None:
    """A string that is nothing but a file-id-shaped token."""

    stripped = text.strip().strip("\"'")
    return stripped if _FILE_ID.fullmatch(stripped) else None


def _first_inline(*candidates: Any) -> bytes:
    for candidate in candidates:
        found = _mapping(candidate)
        if found is None:
            continue
        for key in _INLINE_KEYS:
            data = _decode(found.get(key))
            if data:
                return data
    return b""


def _first_url(*candidates: Any) -> str | None:
    for candidate in candidates:
        found = _mapping(candidate)
        if found is None:
            continue
        for key in _URL_KEYS:
            value = found.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
    return None


def _tool_calls(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [call for call in (_mapping(item) for item in calls) if call is not None]


def _arguments(call: Mapping[str, Any]) -> Any:
    function = _mapping(call.get("function"))
    return None if function is None else function.get("arguments")


def _arguments_object(call: Mapping[str, Any]) -> dict[str, Any] | None:
    """A tool call's arguments as an object, whether they arrived as one."""

    arguments = _arguments(call)
    if isinstance(arguments, dict):
        return _mapping(arguments)
    if isinstance(arguments, str):
        stripped = arguments.strip()
        if stripped[:1] == "{":
            try:
                return _mapping(json.loads(stripped))
            except ValueError:
                return None
    return None


def _usage(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The chat ``usage`` block in the counters the media row reads.

    A chat answer states ``prompt_tokens`` / ``completion_tokens``; the media
    row (and OpenAI's own Images answers) spell them ``input_tokens`` /
    ``output_tokens``. Every other key is carried through untouched, so a host
    that reports ``cost`` / ``is_byok`` is read by the existing reported-cost
    rung exactly as it is on a chat row -- the "Option B is free" half of
    decision Q5.
    """

    block = _mapping(payload.get("usage"))
    if block is None:
        return None
    usage = dict(block)
    for source, target in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
    ):
        value = block.get(source)
        if isinstance(value, int) and not isinstance(value, bool):
            usage.setdefault(target, value)
    return usage


def parse_image_tool_answer(raw: bytes) -> MistralImageAnswer:
    """What a chat answer said about the picture. Run off the loop.

    Raises :class:`MistralAnswerError` when the answer names no picture at
    all, or when the body is not a JSON object -- both are "the host answered
    without an image", which the leaf raises as an ordinary charged failure.
    """

    payload = _payload(raw)
    message = _message(payload)
    calls = _tool_calls(message)
    content = message.get("content")

    inline = _first_inline(message, content, *calls)
    if not inline:
        for call in calls:
            inline = _first_inline(_arguments_object(call))
            if inline:
                break
    url = _first_url(message, content, *(call for call in calls))

    file_id: str | None = None
    for call in calls:
        file_id = _file_id_in(_arguments(call))
        if file_id is not None:
            break
    if file_id is None:
        for key in _FILE_KEYS:
            named = message.get(key)
            if isinstance(named, str) and named.strip():
                file_id = _file_path_in(named) or _bare_id(named)
                if file_id is not None:
                    break
    if file_id is None and isinstance(content, str):
        file_id = _file_path_in(content)

    text = _text_of(content).strip() or None
    if not inline and file_id is None and url is None:
        raise MistralAnswerError("without an image")
    created = payload.get("created")
    return MistralImageAnswer(
        file_id=file_id,
        inline_bytes=inline,
        url=url,
        text=text,
        usage=_usage(payload),
        created=created
        if isinstance(created, int) and not isinstance(created, bool)
        else None,
    )


def images_answer(
    *,
    created: int,
    data: bytes = b"",
    url: str | None = None,
) -> bytes:
    """An OpenAI Images answer carrying one picture. Run off the loop.

    ``{created, data: [{b64_json}]}`` -- the shape every OpenAI Images client
    parses, and the shape MCC already synthesises for a host that answers
    differently (the Gemini native path, the URL-only inliner). A URL that
    could not be inlined stays a ``url`` item, which is also an OpenAI Images
    answer; that only happens when the picture was already in hand.
    """

    item: dict[str, Any] = (
        {"b64_json": base64.b64encode(data).decode("ascii")}
        if data
        else {"url": url or ""}
    )
    return json.dumps({"created": created, "data": [item]}).encode()
