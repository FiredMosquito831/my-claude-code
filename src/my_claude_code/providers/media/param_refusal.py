"""What a media host's refusal says about one request field (7.98.0).

The user's real media failures were schema failures: 50 of 54 Agnes video
creates answered ``400 {"code": "invalid_request", "message": "mode is
required (request id: ...)", "data": {"param": "mode"}}`` and six image
requests ``400 {"error": {"message": "image_config is not supported by text
image queue (request id: ...)", "type": "invalid_request", "param": "", ...}}``.
No source MCC reads could have told the client either thing. The host's own
refusal can, so it is written down -- and only written down.

**Recording only** (user decision 6 of 2026-10-16). :func:`note_param_refusal`
runs on the media leaf's failure path before the failure is classified, reads
the body the leaf already holds, and records at most a learned fact. It never
raises, never awaits, never changes the error it was handed, and nothing on
the request path reads what it records: the route, its retries, its charge
and its outcome are exactly what they were. The fact is published in the
model's request schema (``application/media/schemas.py``) with Forget.

What counts as naming a field, and nothing else:

* the body's structured field -- OpenAI's ``error.param``, a top-level
  ``param``, the Agnes/new-api ``data.param``, or a pydantic-style
  ``detail[].loc`` -- together with words saying which way (``required`` /
  ``missing`` versus ``not supported`` / ``unknown`` / ``extra``);
* when the structured field is empty, a message that starts with the field's
  name (``image_config is not supported ...``, ``mode is required ...``) or
  names it after "missing required parameter" / "unsupported parameter";
* a refused field must be one this request carried, and a required one one it
  did not -- the host is never believed about a field the request contradicts.

Only on a surface that forwards the client's own field names (the OpenAI
shapes): a Gemini native body is MCC's translation, so a field its host names
is not a client field at all.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import httpx
from loguru import logger

from my_claude_code.application.media.request import MediaRequest
from my_claude_code.config.media_surfaces import (
    MEDIA_SHAPE_OPENAI_IMAGES,
    MEDIA_SHAPE_OPENAI_SPEECH,
    MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS,
    MEDIA_SHAPE_OPENAI_VIDEOS,
    MediaSurface,
)
from my_claude_code.providers.recovery import (
    FACT_MEDIA_PARAM_REFUSED,
    FACT_MEDIA_PARAM_REQUIRED,
    SOURCE_REJECTION,
    learned_fact_store,
)

#: Which way a refusal points.
REQUIRED = "required"
REFUSED = "refused"

_FACT_KIND = {REQUIRED: FACT_MEDIA_PARAM_REQUIRED, REFUSED: FACT_MEDIA_PARAM_REFUSED}

#: The wire shapes whose body is the client's own fields, renamed at most.
PASSTHROUGH_SHAPES: frozenset[str] = frozenset(
    {
        MEDIA_SHAPE_OPENAI_IMAGES,
        MEDIA_SHAPE_OPENAI_SPEECH,
        MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS,
        MEDIA_SHAPE_OPENAI_VIDEOS,
    }
)

#: 4xx answers that are about the key, the account, the address or the rate,
#: never about a field of the body.
_NOT_ABOUT_A_FIELD = frozenset({401, 402, 403, 404, 407, 408, 429})

#: A refusal body larger than this is not a validator's sentence.
_MAX_BODY_BYTES = 64 * 1024

#: MCC fills ``model`` itself: a host refusing it refuses the model, which is
#: a different fact and not a field a client chooses.
_NEVER_A_FIELD_FACT = frozenset({"model"})

#: A structured ``param`` must be one top-level field, nothing nested.
_STRUCTURED_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
#: A field read out of a sentence must look like a JSON field (snake_case),
#: so the capitalised first word of an English sentence is never one.
_SENTENCE_FIELD = r"(?P<field>[a-z_][a-z0-9_]{0,63})"
_QUOTE = r"['\"`]?"

_REQUIRED_WORDS = re.compile(r"\brequired\b|\bmissing\b|\bmandatory\b", re.I)
_REFUSED_WORDS = re.compile(
    r"\bnot\s+(?:supported|allowed|permitted|accepted|recogni[sz]ed)\b"
    r"|\bunsupported\b|\bunknown\b|\bunrecogni[sz]ed\b|\bunexpected\b"
    r"|\bextra\b|\bforbidden\b",
    re.I,
)

_SENTENCE_REQUIRED: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"^\s*{_QUOTE}{_SENTENCE_FIELD}{_QUOTE}\s+(?:is|are)\s+(?:a\s+)?required\b"
    ),
    re.compile(
        r"\bmissing\s+(?:the\s+)?required\s+(?:parameter|field|argument|property)s?"
        rf"\s*:?\s*{_QUOTE}{_SENTENCE_FIELD}{_QUOTE}",
        re.I,
    ),
)
_SENTENCE_REFUSED: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"^\s*{_QUOTE}{_SENTENCE_FIELD}{_QUOTE}\s+(?:is|are)\s+not\s+"
        r"(?:supported|allowed|permitted|accepted)\b"
    ),
    re.compile(
        r"\b(?:unsupported|unknown|unrecogni[sz]ed|unexpected)\s+(?:request\s+)?"
        r"(?:parameter|field|argument|property)s?(?:\s+supplied)?"
        rf"\s*:?\s*{_QUOTE}{_SENTENCE_FIELD}{_QUOTE}",
        re.I,
    ),
)

#: pydantic's own words for the two directions, v1 and v2.
_PYDANTIC_REQUIRED_TYPES = frozenset({"missing", "value_error.missing"})
_PYDANTIC_REFUSED_TYPES = frozenset({"extra_forbidden", "value_error.extra"})


@dataclass(frozen=True, slots=True)
class ParamRefusal:
    """One field a host's refusal named, which way, and its own words."""

    direction: str
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class _Said:
    """One statement read out of a body: a structured field and a sentence."""

    param: str
    message: str
    #: A pydantic entry's own type word, which decides the direction alone.
    kind_word: str = ""


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _statements(document: object) -> list[_Said]:
    """Every (field, sentence) pair a refusal body carries, in body order."""

    if not isinstance(document, Mapping):
        return []
    said: list[_Said] = []
    error = document.get("error")
    if isinstance(error, Mapping):
        said.append(_Said(_text(error.get("param")), _text(error.get("message"))))
    elif isinstance(error, str):
        said.append(_Said("", error.strip()))
    data = document.get("data")
    param = _text(document.get("param"))
    if not param and isinstance(data, Mapping):
        param = _text(data.get("param"))
    message = _text(document.get("message"))
    if param or message:
        said.append(_Said(param, message))
    detail = document.get("detail")
    if isinstance(detail, list):
        for entry in detail:
            if not isinstance(entry, Mapping):
                continue
            loc = entry.get("loc")
            if (
                isinstance(loc, list)
                and loc
                and isinstance(loc[-1], str)
                and (len(loc) == 1 or loc[:-1] == ["body"])
            ):
                said.append(
                    _Said(loc[-1], _text(entry.get("msg")), _text(entry.get("type")))
                )
    elif isinstance(detail, str):
        said.append(_Said("", detail.strip()))
    return said


def _direction(said: _Said) -> tuple[str, str] | None:
    """``(direction, field)`` one statement names, or ``None``."""

    if said.param:
        if not _STRUCTURED_FIELD.fullmatch(said.param):
            return None
        if said.kind_word in _PYDANTIC_REQUIRED_TYPES:
            return REQUIRED, said.param
        if said.kind_word in _PYDANTIC_REFUSED_TYPES:
            return REFUSED, said.param
        if said.kind_word:
            # A pydantic entry about a value (a wrong type, out of range) is
            # not a statement about the field being needed or refused.
            return None
        if _REQUIRED_WORDS.search(said.message):
            return REQUIRED, said.param
        if _REFUSED_WORDS.search(said.message):
            return REFUSED, said.param
        return None
    for pattern in _SENTENCE_REQUIRED:
        match = pattern.search(said.message)
        if match is not None:
            return REQUIRED, match.group("field")
    for pattern in _SENTENCE_REFUSED:
        match = pattern.search(said.message)
        if match is not None:
            return REFUSED, match.group("field")
    return None


def param_refusals(
    status_code: int,
    content: bytes,
    *,
    sent: Iterable[str],
    renames: Mapping[str, str] | None = None,
) -> tuple[ParamRefusal, ...]:
    """The fields one refusal names, checked against what the request carried.

    ``sent`` is every top-level field the request carried under the CLIENT's
    names; ``renames`` maps a host's field name back to the client's (a
    surface's declared ``renames``, reversed). Pure: no I/O, no store.
    """

    if not 400 <= status_code < 500 or status_code in _NOT_ABOUT_A_FIELD:
        return ()
    if not content or len(content) > _MAX_BODY_BYTES:
        return ()
    try:
        document = json.loads(content)
    except ValueError:
        return ()
    carried = frozenset(sent)
    back = dict(renames or {})
    found: list[ParamRefusal] = []
    seen: set[tuple[str, str]] = set()
    for said in _statements(document):
        named = _direction(said)
        if named is None:
            continue
        direction, wire_field = named
        field = back.get(wire_field, wire_field)
        if field in _NEVER_A_FIELD_FACT:
            continue
        # Never believed against the request itself: a field it did not carry
        # cannot have been refused, and one it carried was not missing.
        if direction == REFUSED and field not in carried:
            continue
        if direction == REQUIRED and field in carried:
            continue
        if (direction, field) in seen:
            continue
        seen.add((direction, field))
        found.append(ParamRefusal(direction, field, said.message or said.param))
    return tuple(found)


def _sent_fields(request: MediaRequest) -> set[str]:
    """Every field the request carried, under the client's names."""

    fields = {str(name) for name in request.body}
    fields.add("model")
    for upload in request.uploads:
        name = upload.field
        fields.add(name[:-2] if name.endswith("[]") else name)
    return fields


def note_param_refusal(
    error: BaseException,
    *,
    provider_id: str,
    model_id: str,
    surface: MediaSurface,
    request: MediaRequest,
) -> None:
    """Record what one failed media attempt's refusal said about its fields.

    Total by construction: called on the failure path, before the failure is
    classified and re-raised, so anything it could raise is swallowed here
    with one DEBUG line -- an error while writing a note must never become
    the request's error.
    """

    try:
        if not isinstance(error, httpx.HTTPStatusError):
            return
        if surface.shape not in PASSTHROUGH_SHAPES:
            return
        response = error.response
        refusals = param_refusals(
            response.status_code,
            response.content,
            sent=_sent_fields(request),
            renames={wire: client for client, wire in surface.renames},
        )
        if not refusals:
            return
        store = learned_fact_store()
        for refusal in refusals:
            store.record(
                provider_id,
                model_id,
                _FACT_KIND[refusal.direction],
                True,
                source=SOURCE_REJECTION,
                detail=f"{request.operation}:{refusal.field}",
                evidence=refusal.message,
            )
            logger.info(
                "MEDIA FIELDS: {}/{} {} '{}' for {} (shown on the Models page; "
                "nothing sent changes)",
                provider_id,
                model_id,
                refusal.direction,
                refusal.field,
                request.operation,
            )
    except Exception as exc:
        logger.debug(
            "MEDIA FIELDS: could not read a refusal from {}: {}",
            provider_id,
            type(exc).__name__,
        )
