"""Where a request came from: which conversation, which subagent, which folder.

Clients already say this, in three different places, and until 7.42.0 MCC read
none of it into the log:

- **Headers.** Claude Code and the Claude Agent SDK send
  ``x-claude-code-session-id`` on essentially every request (measured: 100 % of
  114,833 Agent SDK rows, 99.98 % of 32,441 Claude Code rows), and Claude Code
  sends ``x-claude-code-agent-id`` when a subagent is speaking (71 % of its
  rows). The header *names* were visible in the log's ``(unlisted)`` field; the
  values were never kept.
- **Request metadata.** ``metadata.user_id`` can carry a session id. It is a
  second source for the same fact, used only when no header stated it.
- **The prompt.** Claude Code's system prompt carries an environment block with
  ``Primary working directory: <path>`` (1,421 of the newest 1,500 Claude Code
  bodies). The Agent SDK's does not (0 of 1,500), so for that harness the
  folder is honestly unknown and stays NULL.

Extraction is declared as data -- :data:`EXTRACTORS` is the whole table -- so a
reader can see every signal MCC trusts, for which harness, and in what order,
without reading code. For each field the first source that answers wins, in
:data:`SOURCE_ORDER`, and the winner is recorded beside the value so the
request detail can say *how* it is known, not just what it is.

NULL means "not measured", as everywhere in the request log -- never "none".

**Nothing here leaves the machine.** These values are written to the local
request log and read back by the local dashboard. No extractor feeds anything a
provider sees; the mirroring rules in :mod:`my_claude_code.core.client_fingerprint`
are unchanged.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

OriginField = Literal["session_id", "agent_id", "parent_session_id", "project_dir"]
OriginSource = Literal["header", "metadata", "prompt", "launcher"]

#: First source that answers wins, per field. A client's own header is the
#: most direct statement; metadata is the same client saying it a second way;
#: the prompt is text the client wrote for the model rather than for us; and a
#: launcher-stated value (PR-O3) is MCC describing a process it started, which
#: is the least specific answer about which conversation is speaking.
SOURCE_ORDER: tuple[OriginSource, ...] = ("header", "metadata", "prompt", "launcher")

#: The fields, in the order they are serialised into ``origin_source``.
FIELD_ORDER: tuple[OriginField, ...] = (
    "session_id",
    "agent_id",
    "parent_session_id",
    "project_dir",
)

#: Session and agent ids are client-side uuids; anything longer is not one.
MAX_ID_CHARS = 128
#: A working directory is stored verbatim up to this length.
MAX_PROJECT_DIR_CHARS = 512
#: The prompt extractor reads at most this many characters of the system
#: blocks. The anchor was measured at offset 5,709 to 15,722 (median 6,570)
#: inside the joined system text on real Claude Code traffic, so 64 KiB is
#: four times the worst observed position and still a small fraction of a
#: large prompt. A future client that moves the block past it fails closed:
#: the folder is NULL, never a guess.
PROMPT_SCAN_MAX_CHARS = 64 * 1024
#: Longest path the prompt extractor accepts on one line. Longer lines do not
#: match at all, which is the same closed failure.
MAX_PROMPT_VALUE_CHARS = 240
#: Bound on a JSON-shaped ``metadata.user_id`` MCC is willing to parse.
MAX_METADATA_CHARS = 2_048
#: Bound on the serialised provenance column.
MAX_ORIGIN_SOURCE_CHARS = 256

#: The ``signal`` a backfilled folder carries. It is read from the same block by
#: the same pattern, but from the stored prompt long after the request, and the
#: detail pane says so.
BACKFILL_SIGNAL = "stored-prompt"

# Claude Code's environment block, one line of it:
#   `` - Primary working directory: C:\Users\you\Projects\app``
# Anchored to a line start, a strict value class (no control characters, so no
# newline can ride along) and a bounded length.
_PRIMARY_WORKING_DIRECTORY = re.compile(
    r"^[ \t-]*Primary working directory:[ \t]*"
    r"(?P<value>[^\x00-\x1f\x7f]{1," + str(MAX_PROMPT_VALUE_CHARS) + r"}?)[ \t\r]*$",
    re.MULTILINE,
)

# ``user_<hash>_account_<uuid>_session_<uuid>``: the session is the tail.
_METADATA_SESSION_TAIL = re.compile(r"(?:^|_)session_(?P<value>[0-9A-Za-z-]{8,128})$")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:[\\/]$")
_PATH_SEPARATORS = re.compile(r"[\\/]+")


@dataclass(frozen=True, slots=True)
class OriginExtractor:
    """One declared way of learning one origin field. Data, not code."""

    field: OriginField
    source: OriginSource
    #: What was read: a header name, ``metadata.user_id``, or the prompt
    #: anchor's short name. Recorded in ``origin_source`` beside the field.
    signal: str
    #: Harness ids this row applies to; empty means every harness.
    harnesses: frozenset[str] = frozenset()
    #: Exact lowercased header name, for ``header`` and ``launcher`` rows.
    header: str | None = None
    #: Path into the request body, for ``metadata`` rows.
    json_path: tuple[str, ...] = ()
    #: Anchored, bounded pattern with a ``value`` group, for ``prompt`` rows
    #: and for a ``metadata`` value that needs a part cut out of it.
    pattern: re.Pattern[str] | None = None
    #: False for a row declared from a client's documentation or source but
    #: never yet seen in a real log. Kept so the table does not overstate what
    #: has been measured.
    observed: bool = True
    #: For ``parent_session_id``: the header's *presence* marks this request
    #: as a child of the session it states, rather than carrying a value.
    marks_child_of_session: bool = False
    #: For ``prompt`` rows: a fixed string every match contains. The pattern
    #: is only run from the line holding its first occurrence, so a prompt
    #: without it costs one substring search rather than a regex over 64 KiB
    #: (measured on real Claude Code prompts: 0.50 ms per request before).
    literal: str | None = None

    def applies_to(self, harness: str | None) -> bool:
        return not self.harnesses or (harness or "") in self.harnesses

    @property
    def id(self) -> str:
        return f"{self.source}.{self.signal}"


_CLAUDE_FAMILY = frozenset({"claude", "claude_agent_sdk"})

#: The whole table. Order inside it does not matter -- :data:`SOURCE_ORDER`
#: decides -- but it is kept grouped by field for the reader.
EXTRACTORS: tuple[OriginExtractor, ...] = (
    OriginExtractor(
        field="session_id",
        source="header",
        signal="x-claude-code-session-id",
        header="x-claude-code-session-id",
        harnesses=_CLAUDE_FAMILY,
    ),
    OriginExtractor(
        field="session_id",
        source="header",
        signal="x-opencode-session",
        header="x-opencode-session",
        harnesses=frozenset({"opencode", "opencode2"}),
        # Zero observations in a 150,000-row sample: OpenCode reaches MCC
        # through a path that does not send it. Declared so it is picked up
        # the day it does; unproven until then.
        observed=False,
    ),
    OriginExtractor(
        field="session_id",
        source="metadata",
        signal="metadata.user_id",
        json_path=("metadata", "user_id"),
        pattern=_METADATA_SESSION_TAIL,
        harnesses=_CLAUDE_FAMILY,
        # Nothing in the log preserved this field before 7.42.0, so its exact
        # shape at the current Claude Code version is unmeasured. The header
        # above answers first on every request that has it.
        observed=False,
    ),
    OriginExtractor(
        field="session_id",
        source="launcher",
        signal="x-mcc-session",
        header="x-mcc-session",
        # No launcher sends it yet (PR-O3). A dict lookup until then.
        observed=False,
    ),
    OriginExtractor(
        field="agent_id",
        source="header",
        signal="x-claude-code-agent-id",
        header="x-claude-code-agent-id",
        harnesses=_CLAUDE_FAMILY,
    ),
    OriginExtractor(
        field="parent_session_id",
        source="header",
        signal="x-claude-code-agent-id",
        header="x-claude-code-agent-id",
        harnesses=frozenset({"claude"}),
        marks_child_of_session=True,
        # That a subagent states its *parent's* session id, rather than one of
        # its own, is the assumption this row encodes. It is unverified until
        # real rows with both columns exist; the grouping that relies on it
        # ships separately, after that check.
        observed=False,
    ),
    OriginExtractor(
        field="project_dir",
        source="prompt",
        signal="env-block",
        pattern=_PRIMARY_WORKING_DIRECTORY,
        literal="Primary working directory:",
        harnesses=frozenset({"claude"}),
    ),
    OriginExtractor(
        field="project_dir",
        source="launcher",
        signal="x-mcc-cwd",
        header="x-mcc-cwd",
        observed=False,
    ),
)

#: Every header name any extractor reads. Only these values are ever looked at.
ORIGIN_HEADERS: frozenset[str] = frozenset(
    extractor.header for extractor in EXTRACTORS if extractor.header is not None
)

#: Harness ids with a declared prompt extractor. Every other harness pays
#: nothing for the prompt scan -- not even the join of its system blocks.
PROMPT_HARNESSES: frozenset[str] = frozenset(
    harness
    for extractor in EXTRACTORS
    if extractor.source == "prompt"
    for harness in extractor.harnesses
)

_SESSION_FIELDS: frozenset[str] = frozenset(
    {"session_id", "agent_id", "parent_session_id"}
)


@dataclass(frozen=True, slots=True)
class OriginInputs:
    """What one request offered, gathered once at the API boundary.

    Header values are copied for the declared names only; nothing else about
    the request's headers is retained here.
    """

    harness: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RequestOrigin:
    """The resolved answer, one value and one provenance per field."""

    session_id: str | None = None
    agent_id: str | None = None
    parent_session_id: str | None = None
    project_dir: str | None = None
    #: ``field=source.signal`` pairs joined with ``;``, in :data:`FIELD_ORDER`.
    origin_source: str | None = None


EMPTY_ORIGIN = RequestOrigin()


def origin_inputs(
    headers: Mapping[str, str] | None,
    *,
    harness: str | None,
    metadata: Any = None,
) -> OriginInputs:
    """Copy the declared header values and the metadata reference.

    One walk over the headers, looking at names only until a declared one is
    found -- the same shape ``capture_headers`` already pays for.
    """
    found: dict[str, str] = {}
    if headers:
        for raw_name, raw_value in headers.items():
            name = str(raw_name).strip().lower()
            if (
                name in ORIGIN_HEADERS
                and name not in found
                and isinstance(raw_value, str)
                and raw_value.strip()
            ):
                found[name] = raw_value
    return OriginInputs(
        harness=harness,
        headers=tuple(sorted(found.items())),
        metadata=metadata if isinstance(metadata, Mapping) else None,
    )


def resolve_origin(
    inputs: OriginInputs,
    *,
    capture_session: bool,
    capture_folder: bool,
    system: Any = None,
) -> RequestOrigin:
    """Apply the declared table to one request.

    ``system`` is the request's system prompt (a string or a list of blocks).
    It is read only when the harness has a declared prompt extractor, the
    folder is wanted, and no higher-ranked source already answered -- so the
    Agent SDK, three quarters of real traffic, never pays for it.
    """
    if not capture_session and not capture_folder:
        return EMPTY_ORIGIN
    headers = dict(inputs.headers)
    values: dict[str, str] = {}
    winners: dict[str, OriginExtractor] = {}
    ranked = sorted(EXTRACTORS, key=lambda item: SOURCE_ORDER.index(item.source))
    prompt_text: str | None = None
    for extractor in ranked:
        if extractor.field in values:
            continue
        if extractor.field in _SESSION_FIELDS and not capture_session:
            continue
        if extractor.field == "project_dir" and not capture_folder:
            continue
        if not extractor.applies_to(inputs.harness):
            continue
        if extractor.marks_child_of_session:
            # Resolved after the session itself, below.
            continue
        value: str | None = None
        if extractor.header is not None:
            value = headers.get(extractor.header)
        elif extractor.source == "metadata":
            value = _metadata_value(inputs.metadata, extractor)
        elif extractor.source == "prompt" and extractor.pattern is not None:
            if prompt_text is None:
                prompt_text = system_prompt_text(system)
            value = _pattern_value(
                extractor.pattern, prompt_text, literal=extractor.literal
            )
        cleaned = _clean(extractor.field, value)
        if cleaned is not None:
            values[extractor.field] = cleaned
            winners[extractor.field] = extractor
    if capture_session and "session_id" in values:
        for extractor in ranked:
            if not extractor.marks_child_of_session:
                continue
            if not extractor.applies_to(inputs.harness):
                continue
            if extractor.header is not None and _clean(
                "agent_id", headers.get(extractor.header)
            ):
                values["parent_session_id"] = values["session_id"]
                winners["parent_session_id"] = extractor
                break
    if not values:
        return EMPTY_ORIGIN
    return RequestOrigin(
        session_id=values.get("session_id"),
        agent_id=values.get("agent_id"),
        parent_session_id=values.get("parent_session_id"),
        project_dir=values.get("project_dir"),
        origin_source=format_origin_source(
            (name, winners[name].source, winners[name].signal)
            for name in FIELD_ORDER
            if name in values
        ),
    )


def system_prompt_text(system: Any) -> str:
    """The system blocks joined, capped at :data:`PROMPT_SCAN_MAX_CHARS`.

    System blocks only -- never the messages. A user who pastes a line that
    looks like an environment block into a message must not move their
    request into a different folder.
    """
    if isinstance(system, str):
        return system[:PROMPT_SCAN_MAX_CHARS]
    if not isinstance(system, list):
        return ""
    parts: list[str] = []
    total = 0
    for block in system:
        text = block.get("text") if isinstance(block, Mapping) else None
        if text is None:
            text = getattr(block, "text", None)
        if not isinstance(text, str) or not text:
            continue
        parts.append(text)
        total += len(text) + 1
        if total >= PROMPT_SCAN_MAX_CHARS:
            break
    return "\n".join(parts)[:PROMPT_SCAN_MAX_CHARS]


def project_dir_from_prompt(text: str | None) -> str | None:
    """The working directory in the first :data:`PROMPT_SCAN_MAX_CHARS` of ``text``.

    Used by the history backfill, which has only the stored prompt: the system
    blocks come first in it, so the head of the stored text is where they are.
    """
    if not text:
        return None
    for extractor in EXTRACTORS:
        if extractor.field == "project_dir" and extractor.source == "prompt":
            if extractor.pattern is None:
                continue
            return _clean(
                "project_dir",
                _pattern_value(
                    extractor.pattern,
                    text[:PROMPT_SCAN_MAX_CHARS],
                    literal=extractor.literal,
                ),
            )
    return None


def format_origin_source(pairs: Iterable[tuple[str, str, str]]) -> str | None:
    """Serialise ``(field, source, signal)`` triples for the ``origin_source`` column."""
    text = ";".join(f"{name}={source}.{signal}" for name, source, signal in pairs)
    return text[:MAX_ORIGIN_SOURCE_CHARS] or None


def merge_origin_source(
    existing: str | None, name: str, source: str, signal: str
) -> str | None:
    """Add or replace one field's provenance in a stored ``origin_source``."""
    parsed = parse_origin_source(existing)
    parsed[name] = (source, signal)
    ordered = [*FIELD_ORDER, *sorted(set(parsed) - set(FIELD_ORDER))]
    return format_origin_source(
        (field_name, parsed[field_name][0], parsed[field_name][1])
        for field_name in ordered
        if field_name in parsed
    )


def parse_origin_source(value: Any) -> dict[str, tuple[str, str]]:
    """``origin_source`` back into ``{field: (source, signal)}``. Tolerant."""
    result: dict[str, tuple[str, str]] = {}
    if not isinstance(value, str):
        return result
    for part in value.split(";"):
        name, sep, rest = part.partition("=")
        if not sep or not name:
            continue
        source, _, signal = rest.partition(".")
        if source:
            result[name.strip()] = (source.strip(), signal.strip())
    return result


def provenance_sentence(source: str, signal: str) -> str:
    """How a value is known, in the words the request detail uses."""
    if source == "header":
        return f"stated by the {signal} header"
    if source == "metadata":
        return f"read from the request's {signal}"
    if source == "prompt" and signal == BACKFILL_SIGNAL:
        return "read later from the stored prompt's environment block (backfill)"
    if source == "prompt":
        return "read from the prompt's environment block"
    if source == "launcher":
        return f"stated by MCC's launcher ({signal} header)"
    return f"from {source}" + (f" ({signal})" if signal else "")


def origin_provenance(value: Any) -> dict[str, dict[str, str]]:
    """Per field: its source, its signal and the sentence the modal shows."""
    return {
        name: {
            "source": source,
            "signal": signal,
            "sentence": provenance_sentence(source, signal),
        }
        for name, (source, signal) in parse_origin_source(value).items()
    }


def project_hash(path: str) -> str:
    """Six hex characters that tell two same-named folders apart."""
    return hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()[:6]


def project_short(path: Any) -> str | None:
    """``Games\\Phone games · #3f9a21``: the last two segments and a short hash.

    Derived at read time and never stored, so the display can change without
    rewriting history. The hash is over the whole stored path, which is what
    keeps two different roots ending in the same two folders apart. Separators
    are split without ``pathlib``, whose rules follow the machine reading the
    string rather than the machine that wrote it.
    """
    if not isinstance(path, str) or not path:
        return None
    segments = [part for part in _PATH_SEPARATORS.split(path) if part]
    separator = "\\" if "\\" in path else "/"
    tail = separator.join(segments[-2:]) if segments else path
    return f"{tail} · #{project_hash(path)}"


def session_short(session_id: Any) -> str | None:
    """The first eight characters: a uuid's first group, enough to tell rows apart."""
    if not isinstance(session_id, str) or not session_id:
        return None
    return session_id[:8]


def _metadata_value(
    metadata: Mapping[str, Any] | None, extractor: OriginExtractor
) -> str | None:
    if metadata is None:
        return None
    path = (
        extractor.json_path[1:]
        if extractor.json_path[:1] == ("metadata",)
        else extractor.json_path
    )
    current: Any = metadata
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    if not isinstance(current, str) or not current or len(current) > MAX_METADATA_CHARS:
        return None
    text = current.strip()
    if text.startswith("{"):
        # Some client versions pack the ids as a JSON object in the string.
        try:
            decoded = json.loads(text)
        except ValueError:
            return None
        session = decoded.get("session_id") if isinstance(decoded, dict) else None
        return session if isinstance(session, str) else None
    if extractor.pattern is None:
        return text
    return _pattern_value(extractor.pattern, text)


def _pattern_value(
    pattern: re.Pattern[str], text: str | None, *, literal: str | None = None
) -> str | None:
    if not text:
        return None
    start = 0
    if literal is not None:
        found = text.find(literal)
        if found < 0:
            return None
        # From the start of that line: ``^`` under MULTILINE matches after a
        # newline, so a match can begin no earlier than here and no later
        # match is skipped.
        start = text.rfind("\n", 0, found) + 1
    match = pattern.search(text, start)
    return match.group("value") if match else None


def _clean(field_name: str, value: Any) -> str | None:
    """Normalise one extracted value; None when it is not a plausible one."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or _CONTROL_CHARS.search(text):
        return None
    if field_name == "project_dir":
        if not _DRIVE_ROOT.match(text) and len(text) > 1:
            text = text.rstrip("\\/") or text
        return text[:MAX_PROJECT_DIR_CHARS]
    return text[:MAX_ID_CHARS]


__all__ = [
    "BACKFILL_SIGNAL",
    "EMPTY_ORIGIN",
    "EXTRACTORS",
    "FIELD_ORDER",
    "MAX_ID_CHARS",
    "MAX_PROJECT_DIR_CHARS",
    "MAX_PROMPT_VALUE_CHARS",
    "ORIGIN_HEADERS",
    "PROMPT_HARNESSES",
    "PROMPT_SCAN_MAX_CHARS",
    "SOURCE_ORDER",
    "OriginExtractor",
    "OriginInputs",
    "RequestOrigin",
    "format_origin_source",
    "merge_origin_source",
    "origin_inputs",
    "origin_provenance",
    "parse_origin_source",
    "project_dir_from_prompt",
    "project_hash",
    "project_short",
    "provenance_sentence",
    "resolve_origin",
    "session_short",
    "system_prompt_text",
]
