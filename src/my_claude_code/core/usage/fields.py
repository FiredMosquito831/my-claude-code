"""The parts every usage adapter reads out of a provider's answer.

One small module rather than five copies: every adapter faces the same three
questions -- "is this JSON object", "is this number a string", "does this field
look like an epoch" -- and answering them five ways is how five adapters start
disagreeing about what a malformed body is.

THE RULE HERE IS THE MIRROR OF `core/usage/reading.py`'s: a figure is a string
the provider wrote. So the JSON is decoded with ``parse_float=str`` and
``parse_int=str``, and ``1.0`` stays ``"1.0"`` rather than becoming a float
that formats as ``1`` on somebody else's machine. Nothing in this package
recomputes a number the provider sent.

What each helper does when the answer is not shaped as the row declares: it
raises :class:`~my_claude_code.core.usage.reading.UsageUnreadable`. The route
turns that into state 4 with a sentence -- never an exception escaping to a
500, and never a zero standing in for the field that was missing.
"""

import json
from typing import Any

from .reading import UsageUnreadable

__all__ = [
    "as_list",
    "as_mapping",
    "decode_json",
    "first_text",
    "scalar_text",
    "text_at",
]


def decode_json(body: bytes) -> Any:
    """Decode a usage route's body, keeping every number as its own text.

    Raises :class:`UsageUnreadable` with a sentence rather than a
    ``JSONDecodeError`` traceback: the route's answer to an unparseable body is
    state 4 with a reason, and a reason a reader can act on is "the host
    answered with something that is not JSON", not "Expecting value: line 1
    column 1 (char 0)".
    """

    try:
        return json.loads(
            body.decode("utf-8", errors="replace") or "", parse_float=str, parse_int=str
        )
    except ValueError, UnicodeDecodeError:
        raise UsageUnreadable(
            "the host answered with something that is not JSON"
        ) from None


def as_mapping(value: Any) -> dict[str, Any]:
    """``value`` as a JSON object, or ``UsageUnreadable``.

    Rebuilt as ``dict[str, Any]`` rather than cast, because ``isinstance(x,
    dict)`` does not narrow for the type checker (III.4) and the rebuild is the
    documented way round it.
    """

    if not isinstance(value, dict):
        raise UsageUnreadable("the host's answer is not a JSON object")
    return {str(key): item for key, item in value.items()}


def as_list(value: Any) -> list[Any]:
    """``value`` as a JSON array, or an empty one.

    An absent list is not an error: a host that sends no ``limits`` array has
    simply reported fewer things, and the row's other fields still stand.
    """

    return list(value) if isinstance(value, list) else []


def scalar_text(value: Any) -> str:
    """One scalar as the provider wrote it, or ``""``.

    ``True``/``False`` become ``"true"``/``"false"`` rather than Python's
    ``"True"``, because the page prints these verbatim and a provider's own
    boolean is a word, not a repr. ``None`` -- which is how OpenRouter says
    *"no limit"*, deliberately and documented -- is ``""``, and the caller
    decides what an absent value means. **Never ``"0"``**: that substitution is
    the defect §4.1 exists to prevent.
    """

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def text_at(mapping: dict[str, Any], *path: str) -> str:
    """Follow a dotted path through nested objects and return its scalar text.

    ``text_at(body, "data", "available_balance")`` is Kimi's
    ``{"data": {"available_balance": …}}``. A missing step, a non-object where
    an object was expected, and a ``None`` leaf all yield ``""`` -- "the host
    did not send this" is a legal answer to most of the questions here, and the
    fields that are **required** check for it themselves (see
    :func:`first_text`).
    """

    current: Any = mapping
    for step in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(step)
    return scalar_text(current)


def first_text(mapping: dict[str, Any], *paths: str) -> str:
    """:func:`text_at` over several candidate paths, first non-empty wins.

    `specs/RESEARCH-BILLING-ENDPOINT-DISCOVERY.md` §3 is explicit that several
    of these shapes are third-party-verified rather than vendor-published, and
    a field that moves from ``limit_remaining`` to ``limit_remaining_usd`` in
    a private API's next deploy must not make the whole row unreadable. Several
    spellings, one value, declared at the call site.
    """

    for path in paths:
        value = text_at(mapping, *path.split("."))
        if value:
            return value
    return ""
