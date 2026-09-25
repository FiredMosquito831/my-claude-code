"""Reversible tool names for Anthropic-to-OpenAI protocol conversion."""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any

from .content import get_block_attr, get_block_type
from .models import MessagesRequest

OPENAI_TOOL_NAME_MAX_LENGTH = 64
_ALIAS_DIGEST_LENGTH = 16
_INVALID_TOOL_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9_-]+")

#: The shortest ceiling an alias can still be built under.
#:
#: An alias is ``<readable>_<16 hex>``, so a limit at or below 17 leaves no
#: readable half at all and a limit only a little above it leaves a stub that
#: no longer names the tool. Four readable characters is the floor this module
#: will work with; a host that states anything smaller is not aliased, and the
#: caller fails visibly rather than sending a name it invented.
MIN_TOOL_NAME_MAX_LENGTH = _ALIAS_DIGEST_LENGTH + 1 + 4

#: "This host has a tool catalogue of its own, and these are its spellings."
#:
#: Empty -- the default, and what every caller before 7.28.0 passed -- means
#: the codec behaves exactly as it always has: a name is either portable or it
#: is aliased, and nothing is renamed. A non-empty mapping is ``{client name:
#: this host's name}``, and it buys three rules stated once here so that the
#: encode and decode halves cannot disagree about them:
#:
#: 1. a client name the catalogue lists is **renamed** to the host's spelling;
#: 2. any *other* name that would collide case-insensitively with a catalogue
#:    spelling is **aliased away**, because a host that receives ``bash`` and
#:    ``Bash`` in one request has two tools whose names differ only in case
#:    (measured: HTTP 500 ``server_error``, reproduced twice, 2026-09-18);
#: 3. no generated alias may equal a catalogue spelling, so every spelling is
#:    reserved whether or not this request claimed it.
#:
#: Each rule is a pure function of the one name, so turn N and turn N+1 encode
#: a tool identically and the prompt-cache prefix does not move.
EMPTY_TOOL_CATALOGUE: Mapping[str, str] = MappingProxyType({})


@lru_cache(maxsize=8)
def _portable_tool_name(max_length: int) -> re.Pattern[str]:
    """The "no translation needed" test for one host's stated ceiling.

    Cached because it is asked once per distinct name per request and there is
    only ever a handful of ceilings in a fleet. ``max_length`` of
    :data:`OPENAI_TOOL_NAME_MAX_LENGTH` compiles exactly the pattern this
    module carried as a module constant before 7.23.0.
    """

    return re.compile(rf"^[A-Za-z0-9_-]{{1,{max_length}}}$")


@dataclass(frozen=True, slots=True)
class OpenAIToolNameCodec:
    """Map non-portable client tool names to deterministic OpenAI aliases."""

    _original_to_alias: dict[str, str]
    _alias_to_original: dict[str, str]
    _unchanged_names: frozenset[str]

    @classmethod
    def from_request(
        cls,
        request: MessagesRequest,
        *,
        max_length: int = OPENAI_TOOL_NAME_MAX_LENGTH,
        catalogue: Mapping[str, str] = EMPTY_TOOL_CATALOGUE,
    ) -> OpenAIToolNameCodec:
        """Build the codec from every tool identity carried by one request."""
        return cls.from_names(
            _request_tool_names(request), max_length=max_length, catalogue=catalogue
        )

    @classmethod
    def from_names(
        cls,
        names: Iterable[str],
        *,
        max_length: int = OPENAI_TOOL_NAME_MAX_LENGTH,
        catalogue: Mapping[str, str] = EMPTY_TOOL_CATALOGUE,
    ) -> OpenAIToolNameCodec:
        """Build order-independent aliases for the supplied non-empty names.

        ``max_length`` is the host's ceiling. It defaults to the OpenAI one,
        which is the only value anything sent before 7.23.0 ever used, so every
        existing caller keeps byte-identical aliases. It is a parameter at all
        because a host may *state* a different number in a rejection
        (``providers/recovery/responses_refusals.py``), and an alias built for
        64 would be refused again by a host that said 48.

        Whatever the ceiling, an alias stays a pure function of the name: the
        digest is over the name alone, so turn N and turn N+1 of a conversation
        alias a tool identically and the prompt-cache prefix does not move.

        ``catalogue`` is this host's own tool spellings; see
        :data:`EMPTY_TOOL_CATALOGUE` for the three rules it turns on and for
        why the default leaves every pre-7.28.0 caller byte-identical.
        """
        if max_length < MIN_TOOL_NAME_MAX_LENGTH:
            raise ValueError(
                "tool-name aliasing needs a ceiling of at least "
                f"{MIN_TOOL_NAME_MAX_LENGTH}; got {max_length}"
            )
        portable = _portable_tool_name(max_length)
        for client_name, host_name in catalogue.items():
            if not host_name or not portable.fullmatch(host_name):
                raise ValueError(
                    f"catalogue name {host_name!r} for {client_name!r} is not a "
                    f"tool name this host could accept under {max_length} characters"
                )
        # Every spelling the host owns, whether or not this request claimed it:
        # rule 3. ``claimed`` is the case-insensitive shadow of the same set,
        # which is what rule 2 tests against.
        catalogue_names = set(catalogue.values())
        claimed = {host_name.casefold() for host_name in catalogue_names}

        unique_names = {name for name in names if name}
        renamed = {name: catalogue[name] for name in unique_names if name in catalogue}
        collisions = {
            name for name in unique_names - renamed.keys() if name.casefold() in claimed
        }
        portable_names = {
            name
            for name in unique_names - renamed.keys() - collisions
            if portable.fullmatch(name)
        }
        reserved = portable_names | catalogue_names
        original_to_alias: dict[str, str] = dict(renamed)
        alias_to_original: dict[str, str] = {
            host_name: client_name for client_name, host_name in renamed.items()
        }

        for name in sorted(unique_names - portable_names - renamed.keys()):
            alias = _unique_alias(name, reserved, max_length)
            reserved.add(alias)
            original_to_alias[name] = alias
            alias_to_original[alias] = name

        return cls(
            original_to_alias,
            alias_to_original,
            frozenset(portable_names),
        )

    @property
    def has_aliases(self) -> bool:
        """Return whether this request needs any tool-name translation."""
        return bool(self._original_to_alias)

    def encode(self, name: str) -> str:
        """Return the OpenAI wire name for one client name."""
        return self._original_to_alias.get(name, name)

    def decode(self, name: str) -> str:
        """Return the original client name for one known wire alias."""
        return self._alias_to_original.get(name, name)

    def is_alias(self, value: str) -> bool:
        """Return whether value is one complete alias generated for this request."""
        return value in self._alias_to_original

    def is_unchanged_name(self, value: str) -> bool:
        """Return whether value is one complete name sent upstream unchanged."""
        return value in self._unchanged_names

    def is_alias_prefix(self, value: str) -> bool:
        """Return whether value is an incomplete prefix of a generated alias."""
        return bool(value) and any(
            alias != value and alias.startswith(value)
            for alias in self._alias_to_original
        )


def request_tool_names(request: MessagesRequest) -> frozenset[str]:
    """Every tool name one request carries: the set its codec is built from.

    Public so that a caller choosing a catalogue *for* a request reads exactly
    the names :meth:`OpenAIToolNameCodec.from_request` will read -- a catalogue
    chosen from a narrower set could rename a tool the codec then aliases.
    """

    return frozenset(name for name in _request_tool_names(request) if name)


def _request_tool_names(request: MessagesRequest) -> Iterable[str]:
    for tool in request.tools or ():
        yield tool.name

    tool_choice = request.tool_choice
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        if choice_type == "tool":
            name = tool_choice.get("name")
            if isinstance(name, str):
                yield name
        elif choice_type == "function":
            function = tool_choice.get("function")
            if isinstance(function, dict):
                name = function.get("name")
                if isinstance(name, str):
                    yield name

    for message in request.messages:
        if message.role != "assistant" or not isinstance(message.content, list):
            continue
        for block in message.content:
            if get_block_type(block) != "tool_use":
                continue
            name = get_block_attr(block, "name")
            if isinstance(name, str):
                yield name


def _unique_alias(
    name: str,
    reserved: set[str],
    max_length: int = OPENAI_TOOL_NAME_MAX_LENGTH,
) -> str:
    readable = _INVALID_TOOL_NAME_CHARACTERS.sub("_", name).strip("_-") or "tool"
    max_readable_length = max_length - _ALIAS_DIGEST_LENGTH - 1
    readable = readable[:max_readable_length].rstrip("_-") or "tool"
    attempt = 0
    while True:
        digest_input = name if attempt == 0 else f"{name}\0{attempt}"
        digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[
            :_ALIAS_DIGEST_LENGTH
        ]
        alias = f"{readable}_{digest}"
        if alias not in reserved:
            return alias
        attempt += 1


def encode_anthropic_body_tool_names(
    body: dict[str, Any], codec: OpenAIToolNameCodec
) -> dict[str, Any]:
    """Rewrite every tool name in an Anthropic Messages body through ``codec``.

    The Messages surface is the one door where MCC speaks the same protocol on
    both sides, so there is no conversion step to hang this on and it is done
    here instead. Three places carry a tool name outbound -- the catalogue, a
    forced ``tool_choice`` and the ``tool_use`` blocks of replayed assistant
    turns -- and all three must move together or the host sees a history that
    calls tools its catalogue does not list.

    ``tool_result`` blocks are deliberately untouched: they reference a call by
    ``tool_use_id``, never by name.

    A new dict, shallowly rebuilt only along the paths it changes: the caller's
    body is recorded in the wire capture and may be retried, and mutating it
    under either would make the record disagree with itself.
    """

    if not codec.has_aliases:
        return body
    out = dict(body)

    tools = out.get("tools")
    if isinstance(tools, list):
        out["tools"] = [
            {**tool, "name": codec.encode(tool["name"])}
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
            else tool
            for tool in tools
        ]

    choice = out.get("tool_choice")
    if isinstance(choice, dict) and isinstance(choice.get("name"), str):
        out["tool_choice"] = {**choice, "name": codec.encode(choice["name"])}

    messages = out.get("messages")
    if isinstance(messages, list):
        out["messages"] = [
            _encode_anthropic_message(message, codec) for message in messages
        ]
    return out


def _encode_anthropic_message(message: Any, codec: OpenAIToolNameCodec) -> Any:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return message
    content = message.get("content")
    if not isinstance(content, list):
        return message
    return {
        **message,
        "content": [
            {**block, "name": codec.encode(block["name"])}
            if isinstance(block, dict)
            and block.get("type") == "tool_use"
            and isinstance(block.get("name"), str)
            else block
            for block in content
        ],
    }


def decode_anthropic_sse_event(event: str, codec: OpenAIToolNameCodec) -> str:
    """Put the client's own tool name back into one Anthropic SSE frame.

    Only ``content_block_start`` carries a tool name -- the argument deltas
    and the stop frame are matched by block index -- so every other frame is
    returned by identity, and the substring guard means a stream with no tool
    call in it is not parsed at all.

    A frame this cannot parse is passed through unchanged rather than dropped:
    a name that failed to decode is a wrong name, and a swallowed frame is a
    truncated answer, and the first is the smaller harm.
    """

    if not codec.has_aliases or '"tool_use"' not in event:
        return event
    prefix, separator, payload = event.partition("data: ")
    if not separator:
        return event
    try:
        frame = json.loads(payload)
    except ValueError:
        return event
    if not isinstance(frame, dict) or frame.get("type") != "content_block_start":
        return event
    block = frame.get("content_block")
    if not isinstance(block, dict) or block.get("type") != "tool_use":
        return event
    name = block.get("name")
    if not isinstance(name, str):
        return event
    decoded = codec.decode(name)
    if decoded == name:
        return event
    frame["content_block"] = {**block, "name": decoded}
    return f"{prefix}data: {json.dumps(frame)}\n\n"
