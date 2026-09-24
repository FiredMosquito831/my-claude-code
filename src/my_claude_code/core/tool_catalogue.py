"""A content-addressed fingerprint of the tools array a client sent.

The request log recorded how many tools a request carried (``params.tools_count``)
and never which ones. On 2026-09-20 one tool pattern in a 212-tool catalogue made
every ChatGPT-OAuth request fail, and finding the tool took hours, because the
array itself had never been kept anywhere.

Storing the array per request would cost kilobytes per row. Instead every tool
definition is hashed on its own, and the catalogue is the hash of its members'
hashes, in the order the client sent them. The request keeps one 32-byte
catalogue hash. The definitions are stored once, keyed on their own hash.

Everything here is synchronous and runs on the request log's writer thread,
never on a request path.
"""

import hashlib
import json
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: Bytes in every hash this module produces (SHA-256).
TOOL_SHA_BYTES = 32

# Keys of a tool definition that are not part of what the tool *is*. Claude Code
# puts ``cache_control`` on whichever tool happens to be last, so keeping it
# would give one tool a second identity purely by position.
_NON_IDENTITY_KEYS = frozenset({"cache_control"})

# How many distinct tool names the writer remembers. One per name, so this is
# tools, not arrays: a Claude Code session with every MCP server connected is
# ~200, and each held tool is the client's own object, a few KB.
_RECENT_TOOLS = 1_024


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One tool, as the client defined it, in canonical JSON."""

    sha: bytes
    name: str
    definition: str


@dataclass(frozen=True, slots=True)
class ToolCatalogue:
    """An ordered tools array: its own hash, and each member's."""

    sha: bytes
    members: tuple[ToolDefinition, ...]

    @property
    def member_shas(self) -> bytes:
        """The members' hashes, concatenated in order (32 bytes each)."""

        return b"".join(member.sha for member in self.members)


def _as_mapping(tool: Any) -> Mapping[str, Any] | None:
    dump = getattr(tool, "model_dump", None)
    if callable(dump):
        value = dump(mode="json", exclude_none=True)
        return value if isinstance(value, Mapping) else None
    return tool if isinstance(tool, Mapping) else None


def _name_of(tool: Any) -> str | None:
    name = (
        tool.get("name") if isinstance(tool, Mapping) else getattr(tool, "name", None)
    )
    return name if isinstance(name, str) else None


def canonical_tool_definition(tool: Any) -> str | None:
    """Return one tool's canonical JSON, or None for something not a tool.

    Keys sorted, no insignificant whitespace, non-ASCII kept as written,
    ``None`` fields dropped and ``cache_control`` removed. Two clients sending
    the same tool with keys in a different order get the same text.
    """

    mapping = _as_mapping(tool)
    if mapping is None:
        return None
    identity = {
        key: value for key, value in mapping.items() if key not in _NON_IDENTITY_KEYS
    }
    return json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _define(tool: Any) -> ToolDefinition | None:
    definition = canonical_tool_definition(tool)
    if definition is None:
        return None
    return ToolDefinition(
        sha=hashlib.sha256(definition.encode("utf-8")).digest(),
        name=_name_of(tool) or "",
        definition=definition,
    )


def _catalogue(members: list[ToolDefinition]) -> ToolCatalogue | None:
    if not members:
        return None
    member_shas = b"".join(member.sha for member in members)
    return ToolCatalogue(
        sha=hashlib.sha256(member_shas).digest(), members=tuple(members)
    )


def fingerprint_tools(tools: Iterable[Any]) -> ToolCatalogue | None:
    """Fingerprint a tools array from scratch; None when it holds no tools."""

    members = [member for tool in tools if (member := _define(tool)) is not None]
    return _catalogue(members)


class ToolFingerprinter:
    """Fingerprint arrays, re-serialising only the tools that changed.

    Serialising a 212-tool array costs milliseconds of CPU under the GIL, and
    Claude Code sends the same array on every turn of a session. So the writer
    remembers the last object it saw under each tool name and, when the next
    one compares equal (``==``, a C-level walk of the same dicts), reuses its
    hash instead of serialising it again. Only a tool that actually changed is
    re-serialised; the array's own hash is one SHA-256 over 32 bytes a member.

    Equality is Python's: a schema whose only change is ``1`` to ``1.0`` or
    ``true`` compares equal and keeps the hash it had. JSON parsing never
    produces that from an unchanged client, so it is accepted rather than paid
    for with a type-strict walk.

    One instance per writer thread; not thread-safe.
    """

    def __init__(self, capacity: int = _RECENT_TOOLS) -> None:
        self._capacity = max(1, capacity)
        self._recent: OrderedDict[str, tuple[Any, ToolDefinition]] = OrderedDict()

    def fingerprint(self, tools: Iterable[Any]) -> ToolCatalogue | None:
        members: list[ToolDefinition] = []
        for tool in tools:
            name = _name_of(tool)
            cached = self._recent.get(name) if name is not None else None
            if (
                name is not None
                and cached is not None
                and (cached[0] is tool or cached[0] == tool)
            ):
                self._recent.move_to_end(name)
                members.append(cached[1])
                continue
            member = _define(tool)
            if member is None:
                continue
            members.append(member)
            if name is not None:
                self._recent[name] = (tool, member)
                self._recent.move_to_end(name)
                while len(self._recent) > self._capacity:
                    self._recent.popitem(last=False)
        return _catalogue(members)


def split_member_shas(member_shas: bytes) -> list[bytes]:
    """Split a stored ``member_shas`` blob back into its 32-byte hashes."""

    return [
        member_shas[offset : offset + TOOL_SHA_BYTES]
        for offset in range(0, len(member_shas), TOOL_SHA_BYTES)
    ]
