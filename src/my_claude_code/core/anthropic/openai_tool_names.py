"""Reversible tool names for Anthropic-to-OpenAI protocol conversion."""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

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
    ) -> OpenAIToolNameCodec:
        """Build the codec from every tool identity carried by one request."""
        return cls.from_names(_request_tool_names(request), max_length=max_length)

    @classmethod
    def from_names(
        cls,
        names: Iterable[str],
        *,
        max_length: int = OPENAI_TOOL_NAME_MAX_LENGTH,
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
        """
        if max_length < MIN_TOOL_NAME_MAX_LENGTH:
            raise ValueError(
                "tool-name aliasing needs a ceiling of at least "
                f"{MIN_TOOL_NAME_MAX_LENGTH}; got {max_length}"
            )
        portable = _portable_tool_name(max_length)
        unique_names = {name for name in names if name}
        portable_names = {name for name in unique_names if portable.fullmatch(name)}
        reserved = set(portable_names)
        original_to_alias: dict[str, str] = {}
        alias_to_original: dict[str, str] = {}

        for name in sorted(unique_names - portable_names):
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
