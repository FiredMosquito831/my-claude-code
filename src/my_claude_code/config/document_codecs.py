"""Reading and editing the four document shapes desktop apps configure.

``config/harness_config_merge.py`` is the merge engine and stays the only place
allowed to write into a user's own configuration file. This module is the
format layer underneath it: given a document's text, read it into a plain
mapping, and apply a small set of key-level edits back to that text.

**Why edits against text rather than a round-tripped object model.** The whole
safety argument for merging into somebody else's file is "every byte MCC does
not own survives". A load-modify-dump cycle cannot promise that in any format
with comments or layout -- it promises "an equivalent document", which is not
the same claim, and which is how the surveyed peers destroyed JSONC comments
and reordered TOML tables. Editing the *text* keeps the promise literally: the
lines MCC does not touch are the same lines, byte for byte, including comments,
blank lines, indentation style and trailing whitespace.

It also means MCC takes no new third-party dependency to gain TOML and YAML
support. ``tomllib`` reads TOML in the standard library and the edits below are
narrow enough to render by hand -- which is a much smaller claim than the
general TOML emitter ``config/harness_toml.py:18-27`` warns against, because
nothing here ever re-renders a table the user wrote.

**What each format supports, and why that is enough.**

``JSON``
    The shape Command Code, OpenCode, Crush and VS Code's ``settings.json``
    use. Edited as text since 6.83.0, and read as **JSONC** -- comments and
    trailing commas and all -- because ``json.loads`` is not the parser VS Code
    uses for the file MCC has to merge one key into. The claim this module made
    until then, that JSON has "no layout to preserve that anyone notices", was
    measured wrong on both halves: a Configure against a four-space
    ``settings.json`` rewrote all 32 lines of it while changing nothing, and a
    ``settings.json`` with the comments VS Code ships in its own default file
    could not be configured at all.

``JSON_ARRAY``
    VS Code's ``chatLanguageModels.json`` is a bare array with no key to own.
    Ownership is instead "the one element whose ``name`` equals MCC's", which
    no key-path edit can express, so this one format is still merged through
    its object model and rewritten canonically -- no other element is
    reordered, rewritten or removed. It is read as JSONC like the rest.

``TOML``
    Codex's ``config.toml``. Two kinds of edit: a whole table at a dotted path
    (``[model_providers.mcc]``), and a top-level scalar (``model``).

``YAML``
    Goose's ``config.yaml`` and, since 7.117.0, any nested mapping at a dotted
    path (``providers.mcc``). Goose itself only ever needed a top-level scalar
    (``active_provider``): its real provider definition is a *whole file* MCC
    owns under ``custom_providers/``. A nested table is different and needs the
    same promise the JSON path already makes, because the document is somebody
    else's and has comments, quoting and an indentation style of its own. The
    edit is therefore line-based: the lines of the map MCC owns are replaced,
    every other line -- above the table, below it, inside it, or in a sibling
    that happens to use the same key -- is the same line, byte for byte. The
    reader is still shallow; nothing here round-trips a document through a YAML
    emitter.

A format is not given an edit it cannot perform safely. Asking for a nested
scalar in YAML raises rather than guessing, because a wrong guess here is a
corrupted config file for somebody who was not even using the feature.

Reading a YAML *merge target* back (7.118.0) goes through
:func:`yaml_mapping_at`, which is the same narrow reader the writer uses --
:func:`_yaml_walk` and :func:`_yaml_body_items` -- rather than through
:func:`parse_document`. That distinction is deliberate.
:func:`_parse_shallow_yaml` answers "which top-level keys are here" and
deliberately reports a nested block as present with no value; the merge engine
and the dashboard need "what does *this* table hold" (Hermes' ``providers.mcc``,
whose ``api`` decides file ownership and whose ``models`` is the card's model
count). Neither needs, or gets, a general YAML document model.
"""

import json
import re
import tomllib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class DocumentFormat(StrEnum):
    """The document shapes a desktop app's configuration file can take."""

    JSON = "json"
    JSON_ARRAY = "json_array"
    TOML = "toml"
    YAML = "yaml"


class DocumentFormatError(ValueError):
    """The document did not parse, or the edit is not one this format supports."""


@dataclass(frozen=True, slots=True)
class SetTable:
    """Replace, or create, a whole mapping at a dotted key path."""

    key_path: tuple[str, ...]
    value: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SetScalar:
    """Replace, or create, one scalar at a key path."""

    key_path: tuple[str, ...]
    value: object
    #: TOML only (7.111.4): the user's own line for this key, as Configure
    #: found it, for Undo to put back verbatim -- comment, indentation, key
    #: spelling and quoting included. Used only when it still says exactly
    #: ``key = value``; otherwise the value alone is edited. Since 7.116.0 the
    #: same field carries the user's raw *value text* for a JSON key (see
    #: :func:`scalar_text`), and is written back verbatim when it still parses
    #: to the value being restored. YAML ignores it.
    prior_line: str = ""


@dataclass(frozen=True, slots=True)
class DeleteKey:
    """Remove one key at a key path, table or scalar, if it is there."""

    key_path: tuple[str, ...]
    #: JSON only: also drop the containers this removal leaves empty. The
    #: default, and what Configure's Undo has always done. False keeps a
    #: container the user had before MCC added its key (7.100.0's media
    #: server entry records which containers it created).
    prune_empty_ancestors: bool = True


Edit = SetTable | SetScalar | DeleteKey


def parse_document(text: str, document_format: DocumentFormat) -> object:
    """Return a document's text as plain Python data.

    Raises :class:`DocumentFormatError` rather than returning a default, so a
    caller can honour the rule that an unparseable document is never written.
    """

    try:
        match document_format:
            case DocumentFormat.JSON | DocumentFormat.JSON_ARRAY:
                return parse_jsonc(text)
            case DocumentFormat.TOML:
                return tomllib.loads(text)
            case DocumentFormat.YAML:
                return _parse_shallow_yaml(text)
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        raise DocumentFormatError(str(exc)) from exc


def empty_document(document_format: DocumentFormat) -> object:
    """Return what a missing file should be treated as before the first edit."""

    if document_format is DocumentFormat.JSON_ARRAY:
        return []
    return {}


def apply_edits(
    text: str, document_format: DocumentFormat, edits: Sequence[Edit]
) -> str:
    """Return the document's text with ``edits`` applied and nothing else changed."""

    match document_format:
        case DocumentFormat.TOML:
            return _apply_toml_edits(text, edits)
        case DocumentFormat.YAML:
            return _apply_yaml_edits(text, edits)
        case DocumentFormat.JSON:
            return _apply_json_edits(text, edits)
        case _:
            raise DocumentFormatError(
                f"{document_format} is edited through its object model, not its text"
            )


# --------------------------------------------------------------------------
# TOML
# --------------------------------------------------------------------------


def _apply_toml_edits(text: str, edits: Sequence[Edit]) -> str:
    for edit in edits:
        match edit:
            case SetTable(key_path=key_path, value=value):
                text = _toml_set_table(text, key_path, value)
            case SetScalar(key_path=key_path, value=value, prior_line=prior_line):
                if len(key_path) != 1:
                    raise DocumentFormatError(
                        "TOML scalar edits are top-level only; a nested scalar "
                        "belongs to a table MCC would have to own whole"
                    )
                text = _toml_set_root_scalar(
                    text, key_path[0], value, prior_line=prior_line
                )
            case DeleteKey(key_path=key_path):
                text = (
                    _toml_delete_root_scalar(text, key_path[0])
                    if len(key_path) == 1
                    else text
                )
                if len(key_path) >= 1:
                    text = _toml_delete_table(text, key_path)
    return text


def _toml_header_pattern(key_path: Sequence[str]) -> re.Pattern[str]:
    """Match the header line of one table, however its author spaced it.

    ``[model_providers.mcc]``, ``[ model_providers.mcc ]`` and
    ``[model_providers."mcc"]`` are the same table to a TOML reader, so they
    have to be the same table to a remover -- otherwise Undo leaves a block
    behind and the next Configure appends a duplicate.
    """

    parts = r"\s*\.\s*".join(f'"?{re.escape(part)}"?' for part in key_path)
    return re.compile(rf"^\s*\[\s*{parts}\s*\]\s*(#.*)?$")


def _toml_table_span(
    lines: Sequence[str], key_path: Sequence[str]
) -> tuple[int, int] | None:
    """Return the half-open line span of one table, header and children included.

    A table's sub-tables are written as their own headers -- MCC's
    ``http_headers`` becomes ``[model_providers.mcc.http_headers]`` -- and they
    are part of the block MCC owns. Stopping at the first header line after the
    parent's would leave the children behind, so removing MCC's table would
    orphan them and the next Configure would append a second copy.
    """

    header = _toml_header_pattern(key_path)
    start: int | None = None
    for index, line in enumerate(lines):
        if start is None:
            if header.match(line):
                start = index
            continue
        if not line.lstrip().startswith("["):
            continue
        if _toml_header_is_descendant(line, key_path):
            continue
        return start, index
    if start is None:
        return None
    return start, len(lines)


def _toml_header_is_descendant(line: str, key_path: Sequence[str]) -> bool:
    """Return whether a header line names a table nested inside ``key_path``."""

    prefix = r"\s*\.\s*".join(f'"?{re.escape(part)}"?' for part in key_path)
    return re.match(rf"^\s*\[\s*{prefix}\s*\.\s*[^\]]+\]\s*(#.*)?$", line) is not None


def _toml_set_table(
    text: str, key_path: Sequence[str], value: Mapping[str, object]
) -> str:
    rendered = _render_toml_table(key_path, value)
    lines = text.splitlines()
    span = _toml_table_span(lines, key_path)

    if span is None:
        body = text if text.endswith("\n") or not text else text + "\n"
        separator = "\n" if body and not body.endswith("\n\n") else ""
        return f"{body}{separator}{rendered}"

    start, end = span
    # Keep the blank lines that separated the old block from its neighbour, so
    # replacing MCC's own table twice in a row does not creep the file apart.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    replaced = lines[:start] + rendered.rstrip("\n").split("\n") + lines[end:]
    return "\n".join(replaced) + ("\n" if text.endswith("\n") else "")


def _toml_delete_table(text: str, key_path: Sequence[str]) -> str:
    lines = text.splitlines()
    span = _toml_table_span(lines, key_path)
    if span is None:
        return text
    start, end = span
    # Take the blank line the block was separated by, so a Configure/Undo
    # cycle returns the file to its original bytes rather than its original
    # bytes plus a gap.
    while start > 0 and not lines[start - 1].strip():
        start -= 1
    remaining = lines[:start] + lines[end:]
    if not remaining:
        return ""
    return "\n".join(remaining) + ("\n" if text.endswith("\n") else "")


def _toml_root_region_end(lines: Sequence[str]) -> int:
    """Return the first line that belongs to a table rather than the root."""

    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            return index
    return len(lines)


def _toml_root_key_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf'^\s*"?{re.escape(key)}"?\s*=')


def root_scalar_line(
    text: str, document_format: DocumentFormat, key_path: Sequence[str]
) -> str:
    """Return the user's own line for one top-level scalar, or "".

    What Configure records beside the value it replaces (7.111.4), so Undo can
    give the line back byte for byte rather than re-render it from the value.
    TOML only: "" for every other format, a nested key, or a key that is not
    there -- which is what the record stored before this existed.
    """

    if document_format is not DocumentFormat.TOML or len(key_path) != 1:
        return ""
    lines = text.splitlines()
    pattern = _toml_root_key_pattern(key_path[0])
    for index in range(_toml_root_region_end(lines)):
        if pattern.match(lines[index]):
            return lines[index]
    return ""


def _toml_set_root_scalar(
    text: str, key: str, value: object, *, prior_line: str = ""
) -> str:
    """Set one top-level scalar, changing only its value on the user's line.

    Until 7.111.4 the matched line was replaced whole by MCC's own rendering,
    so ``model = "gpt-5.6"   # my default`` lost its comment on Configure and
    Undo -- in both modes -- re-rendered it from the bare value the restore
    record kept. Now:

    * ``prior_line`` -- the user's line as Configure found it -- is put back
      verbatim when it still says exactly ``key = value``. That is Undo.
    * Otherwise only the value's own characters are replaced: the indentation,
      the key's spelling, the spacing around ``=``, the whitespace and comment
      after the value all stay, and a literal-string value stays a literal
      string where the new value can be one. A line already holding the value
      is left alone.
    * A line whose value this cannot find -- one that continues onto the next
      line -- is rewritten whole, as it always was.

    A key that is not there yet is inserted exactly as before.
    """

    lines = text.splitlines()
    limit = _toml_root_region_end(lines)
    pattern = _toml_root_key_pattern(key)
    restored = prior_line if _toml_line_says(prior_line, key, value) else ""
    rendered = restored or f"{_toml_key(key)} = {_toml_value(value)}"

    for index in range(limit):
        if pattern.match(lines[index]):
            lines[index] = restored or _toml_with_value(lines[index], key, value)
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    lines.insert(limit, rendered)
    return "\n".join(lines) + ("\n" if text.endswith("\n") or not text else "")


def _toml_same_value(found: object, value: object) -> bool:
    """Equal and of the same type: ``true`` is not ``1``, though Python says so."""

    return type(found) is type(value) and found == value


def _toml_line_says(line: str, key: str, value: object) -> bool:
    """Return whether one line, read alone, is exactly ``key = value``."""

    if not line or "\n" in line or "\r" in line:
        return False
    try:
        parsed = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return False
    return list(parsed) == [key] and _toml_same_value(parsed[key], value)


def _toml_with_value(line: str, key: str, value: object) -> str:
    """Return ``line`` with only its value replaced by ``value``.

    Where the value ends is asked of ``tomllib`` rather than guessed: the line
    is read up to each ``#`` in turn, and the first prefix that parses is the
    assignment, so a ``#`` inside a string is never taken for a comment.
    """

    canonical = f"{_toml_key(key)} = {_toml_value(value)}"
    match = _toml_root_key_pattern(key).match(line)
    if match is None:
        return canonical
    start = match.end()
    while start < len(line) and line[start] in " \t":
        start += 1
    ends = [index for index, char in enumerate(line) if char == "#" and index > start]
    for end in (*ends, len(line)):
        head = line[:end]
        try:
            parsed = tomllib.loads(head)
        except tomllib.TOMLDecodeError:
            continue
        if list(parsed) != [key]:
            return canonical
        if _toml_same_value(parsed[key], value):
            return line
        stop = len(head.rstrip(" \t"))
        return line[:start] + _toml_value_like(line[start:stop], value) + line[stop:]
    return canonical


def _toml_value_like(previous: str, value: object) -> str:
    """Render ``value`` in the quoting the user's value was written in, if it can be.

    A literal string (``'…'``) holds no escapes, so it can carry a value only
    when that value has no ``'`` and no control character other than tab.
    """

    if (
        isinstance(value, str)
        and previous.startswith("'")
        and not previous.startswith("'''")
        and "'" not in value
        and all(char == "\t" or (char >= " " and char != "\x7f") for char in value)
    ):
        return f"'{value}'"
    return _toml_value(value)


def _toml_delete_root_scalar(text: str, key: str) -> str:
    lines = text.splitlines()
    limit = _toml_root_region_end(lines)
    pattern = _toml_root_key_pattern(key)
    for index in range(limit):
        if pattern.match(lines[index]):
            del lines[index]
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return text


def _render_toml_table(key_path: Sequence[str], value: Mapping[str, object]) -> str:
    """Render one table MCC owns whole. Never used on a table the user wrote."""

    header = ".".join(_toml_key(part) for part in key_path)
    lines = [f"[{header}]"]
    nested: list[str] = []
    for key, item in value.items():
        if isinstance(item, Mapping):
            nested.append(
                _render_toml_table(
                    (*key_path, str(key)),
                    {str(name): entry for name, entry in item.items()},
                )
            )
            continue
        lines.append(f"{_toml_key(str(key))} = {_toml_value(item)}")
    return "\n".join(lines) + "\n" + "".join(f"\n{block}" for block in nested)


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _toml_string(key)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, Mapping):
        inner = ", ".join(
            f"{_toml_key(str(key))} = {_toml_value(item)}"
            for key, item in value.items()
        )
        return "{" + inner + "}"
    if isinstance(value, Sequence):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise DocumentFormatError(f"cannot render {type(value).__name__} as TOML")


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


# --------------------------------------------------------------------------
# YAML
# --------------------------------------------------------------------------
#
# Still no YAML parser. Two kinds of edit are performed by line, on the text
# as it is:
#
# * a top-level scalar (Goose's ``active_provider``), by matching its line;
# * a nested mapping at a dotted path (7.117.0), by finding the block that
#   owns that path through indentation alone and replacing only that block's
#   own lines.
#
# A full parser would let this module accept edits it has no safe way to write
# back -- and, worse, tempt it to re-render a document somebody else wrote. The
# reader below is as narrow as the writer: a mapping entry is a line at a known
# indentation whose key is a plain or quoted scalar, and everything it cannot
# read is opaque bytes that are preserved exactly where they are.


_YAML_ROOT_SCALAR = re.compile(
    r"^(?P<key>[A-Za-z0-9_.-]+)\s*:\s*(?P<value>.*?)\s*(?:#.*)?$"
)


def _parse_shallow_yaml(text: str) -> dict[str, object]:
    """Return the top-level scalar keys of a YAML document.

    Nested blocks are reported as present with a ``None`` value rather than
    parsed. Status only ever asks about a top-level scalar, and pretending to
    understand more than that would be the beginning of a YAML implementation.
    """

    document: dict[str, object] = {}
    for line in text.splitlines():
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        match = _YAML_ROOT_SCALAR.match(line)
        if match is None:
            continue
        raw = match.group("value")
        document[match.group("key")] = _yaml_scalar(raw) if raw else None
    return document


def _yaml_scalar(raw: str) -> object:
    if raw in {"true", "True"}:
        return True
    if raw in {"false", "False"}:
        return False
    if raw in {"null", "~"}:
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        return raw


def _apply_yaml_edits(text: str, edits: Sequence[Edit]) -> str:
    for edit in edits:
        match edit:
            case SetScalar(key_path=key_path, value=value):
                if len(key_path) != 1:
                    raise DocumentFormatError(
                        "YAML scalar edits are top-level only; a value inside a "
                        "nested table belongs to the mapping SetTable owns whole"
                    )
                text = _yaml_set_root_scalar(text, key_path[0], value)
            case DeleteKey(key_path=key_path, prune_empty_ancestors=prune):
                text = _yaml_delete(text, key_path, prune=prune)
            case SetTable(key_path=key_path, value=value):
                text = _yaml_set_table(text, key_path, value)
    return text


def _yaml_root_key_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(key)}\s*:")


def _yaml_set_root_scalar(text: str, key: str, value: object) -> str:
    rendered = f"{key}: {_yaml_render(value)}"
    lines = text.splitlines()
    pattern = _yaml_root_key_pattern(key)
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = rendered
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    lines.append(rendered)
    return "\n".join(lines) + "\n"


def yaml_root_is_sequence(text: str) -> bool:
    """Whether a YAML document's first content line is a block-sequence item.

    A document whose root is a list cannot be given a mapping edit: the walk
    finds no entry to open and the insert would append ``key:`` after the last
    ``- item``, producing a file that is neither shape. Asked before MCC
    touches a merge target, so the refusal names the file instead of the
    corruption.
    """

    for line in _yaml_lines(text):
        stripped = line.body.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        return _yaml_is_sequence_item(stripped)
    return False


def yaml_mapping_at(text: str, key_path: Sequence[str]) -> dict[str, object] | None:
    """Return the mapping a dotted path names in a YAML document, or ``None``.

    The read half of the 7.117.0 nested-table edit, and the only way MCC reads
    a merge target's block back out of somebody else's ``config.yaml``. It
    exists because three callers need the same answer and none of them may
    guess: :func:`~my_claude_code.config.harness_config_merge.owned_block_names`
    ("does MCC's block name *this* server"), the Coding agents card ("how many
    models did MCC write"), and Undo ("did MCC create the container this
    removal emptied").

    An empty ``key_path`` reads the document root. A path that does not resolve,
    or one that resolves to a scalar or to a flow collection MCC would have to
    parse properly (``{...}`` / ``[...]``), answers ``None`` -- the same
    "nothing MCC can vouch for" answer an absent key gets. A path that resolves
    to an *empty* mapping (``mcc: {}`` or ``mcc:`` with nothing under it)
    answers ``{}``, because "there and empty" is a different fact from "not
    there" and Undo turns on it.

    Deliberately not :func:`parse_document`: that reader reports every nested
    block as present with no value, which is the right answer for the desktop
    engine's top-level scalar edits and the wrong one here. This walks the same
    entry grammar :func:`_yaml_set_table` writes with -- :func:`_yaml_walk` for
    the path, :func:`_yaml_body_items` for each level's own entries, and
    :func:`_yaml_scalar` for leaves -- so what MCC reads back is exactly what it
    would have written, and a line it cannot read (a block scalar, an anchor, a
    list item) is skipped rather than guessed at.
    """

    lines = _yaml_lines(text)
    if not key_path:
        return _yaml_level_mapping(lines, 0, len(lines), 0)
    matched, entry, _level = _yaml_walk(lines, key_path)
    if matched != len(key_path) or entry is None:
        return None
    parsed = _yaml_entry_line(lines[entry.header].body)
    if parsed is None:  # pragma: no cover - every entry came from that grammar
        return None
    value, _comment = _yaml_split_comment(parsed[3])
    scalar = value.strip()
    if scalar == "{}":
        return {}
    if scalar:
        # ``key: value`` is not a mapping, and ``[]`` / ``{a: 1}`` are shapes
        # this reader does not claim to understand.
        return None
    child_indent = _yaml_child_indent(lines, entry.header, entry.last)
    if not child_indent:
        return {}
    return _yaml_level_mapping(lines, entry.header + 1, entry.last, len(child_indent))


def _yaml_level_mapping(
    lines: list[_YamlLine], start: int, end: int, indent_width: int
) -> dict[str, object]:
    """Read one indentation level of a YAML mapping into plain data.

    Keys keep the order they are written in; a key's value is a scalar, a
    nested mapping, ``{}`` for an empty flow mapping, ``[]`` for an empty flow
    sequence, or an empty mapping for a header with nothing under it. Anything
    the entry grammar cannot read is skipped -- See :func:`yaml_mapping_at`.
    """

    document: dict[str, object] = {}
    for item in _yaml_body_items(lines, start, end, indent_width):
        if item.key is None:
            continue
        parsed = _yaml_entry_line(lines[item.start].body)
        if parsed is None:  # pragma: no cover - every item carries a key line
            continue
        value, _comment = _yaml_split_comment(parsed[3])
        scalar = value.strip()
        if scalar == "{}":
            document[item.key] = {}
            continue
        if scalar == "[]":
            document[item.key] = []
            continue
        if scalar:
            document[item.key] = _yaml_scalar(scalar)
            continue
        child_indent = _yaml_child_indent(lines, item.start, item.end)
        document[item.key] = (
            _yaml_level_mapping(lines, item.start + 1, item.end, len(child_indent))
            if child_indent
            else {}
        )
    return document


@dataclass(frozen=True, slots=True)
class _YamlLine:
    """One physical line: its text without the terminator, and the terminator."""

    body: str
    ending: str


@dataclass(frozen=True, slots=True)
class _YamlEntry:
    """One mapping entry: its ``key:`` line and the lines that belong to it."""

    key: str
    #: The key exactly as written, quoting included, so a rewrite keeps the
    #: user's spellings where it can.
    key_text: str
    #: Leading whitespace of the ``key:`` line.
    indent: str
    #: Index of the line carrying ``key:``.
    header: int
    #: One past the entry's last non-blank line. Trailing blank lines are not
    #: part of it, so removing an entry cannot eat the gap after it.
    last: int
    #: Leading whitespace of the first child line, or "" for a scalar entry.
    child_indent: str


@dataclass(frozen=True, slots=True)
class _YamlLevel:
    """A range of lines searched for one path segment, at one indentation."""

    start: int
    end: int
    indent: int


@dataclass(frozen=True, slots=True)
class _YamlItem:
    """One line-range of a mapping's body: a keyed entry, or an opaque line."""

    key: str | None
    start: int
    end: int


#: A mapping entry MCC can read: indentation, then a key that is plain (no
#: spaces, no colon) or quoted, then the colon. A list item, a merge key
#: (``<<``) and a continuation of a multi-line scalar all fail this on purpose
#: and are preserved as opaque lines.
_YAML_ENTRY = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<key>\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'|[A-Za-z0-9_./-]+)"
    r":(?P<rest>.*)$"
)

#: A key, or a string value, that needs no quotes: it starts with a letter or
#: an underscore, so a YAML reader can never give it back as a number.
_YAML_BARE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")

#: Words a plain scalar must not be, because a reader would give back
#: something other than the string.
_YAML_RESERVED = frozenset({"true", "false", "null", "~", "yes", "no", "on", "off"})


def _yaml_delete(text: str, key_path: Sequence[str], *, prune: bool) -> str:
    """Remove one key -- scalar or whole mapping -- and the parents it empties.

    ``prune`` is the same promise :class:`DeleteKey` makes for JSON: an
    ancestor left with nothing in it is only there because MCC created it, and
    leaving ``providers:`` behind is MCC's litter in a document it promised to
    leave alone. An ancestor that still holds a key or a comment of the user's
    is kept. ``prune=False`` removes the key alone, for a container the caller
    knows was there before MCC.

    An absent key returns the text unchanged, byte for byte.
    """

    if not key_path:
        raise DocumentFormatError("a YAML delete needs a key")
    lines = _yaml_lines(text)
    matched, entry, _level = _yaml_walk(lines, key_path)
    if entry is None or matched != len(key_path):
        return text
    lines = _yaml_cut(lines, entry.header, entry.last)
    if not prune:
        return _yaml_text(lines)
    for depth in range(len(key_path) - 1, 0, -1):
        matched, parent, _level = _yaml_walk(lines, key_path[:depth])
        if parent is None or matched != depth:
            break
        if _yaml_entry_has_content(lines, parent):
            break
        full, _last = _yaml_entry_end(lines, parent.header, len(parent.indent))
        lines = _yaml_cut(lines, parent.header, full)
    return _yaml_text(lines)


def _yaml_set_table(
    text: str, key_path: Sequence[str], value: Mapping[str, object]
) -> str:
    """Write a mapping at a dotted path, changing only the lines of that map.

    The map is MCC's: every entry it currently holds is replaced or removed,
    and entries the new value adds go in at the end of it. Comments inside it,
    and every line outside it -- above, below, or in a sibling that happens to
    use the same key -- are the same bytes. A table that is not there is
    created, with the parents the path needs, at the document's own
    indentation and line ending.
    """

    if not key_path:
        raise DocumentFormatError("a YAML table edit needs a key")
    if not isinstance(value, Mapping):
        raise DocumentFormatError("a YAML table edit needs a mapping")
    lines = _yaml_lines(text)
    unit = _yaml_indent_unit(lines)
    eol = _yaml_eol(text)
    matched, container, level = _yaml_walk(lines, key_path)
    if matched == len(key_path) and container is not None:
        return _yaml_text(_yaml_replace_entry(lines, container, dict(value), unit, eol))
    missing = list(key_path[matched:])
    nested: object = dict(value)
    for key in reversed(missing[1:]):
        nested = {key: nested}
    return _yaml_text(
        _yaml_insert(lines, container, level, missing[0], nested, unit, eol)
    )


def _yaml_insert(
    lines: list[_YamlLine],
    container: _YamlEntry | None,
    level: _YamlLevel,
    key: str,
    value: object,
    unit: str,
    eol: str,
) -> list[_YamlLine]:
    """Add one entry at the end of ``level``, opening its parent if needed."""

    if container is None:
        return _yaml_place(
            lines, len(lines), _yaml_render_entry(key, value, "", unit, eol)
        )
    parsed = _yaml_entry_line(lines[container.header].body)
    if parsed is None:
        # Unreachable: every entry came from a line this parser accepted.
        raise DocumentFormatError("cannot read the YAML entry MCC is writing into")
    if _yaml_body_is_sequence(lines, container):
        # The path descends through a list. The object path replaces a
        # non-object with the object MCC needs; so does this one.
        rendered = _yaml_render_entry(parsed[1], {key: value}, parsed[0], unit, eol)
        return _yaml_replace_span(lines, container.header, container.last, rendered)
    indent = container.child_indent
    updated = list(lines)
    if not indent:
        indent = parsed[0] + unit
        updated[container.header] = _yaml_opened_header(
            updated[container.header], parsed
        )
    return _yaml_place(
        updated, level.end, _yaml_render_entry(key, value, indent, unit, eol)
    )


def _yaml_replace_entry(
    lines: list[_YamlLine],
    entry: _YamlEntry,
    value: dict[str, object],
    unit: str,
    eol: str,
) -> list[_YamlLine]:
    """Replace one owned mapping's body, keeping the lines MCC did not write."""

    parsed = _yaml_entry_line(lines[entry.header].body)
    if parsed is None:
        raise DocumentFormatError("cannot read the YAML entry MCC is writing into")
    if not value:
        header = _YamlLine(
            f"{entry.indent}{entry.key_text}: {{}}", lines[entry.header].ending
        )
        return _yaml_replace_span(lines, entry.header, entry.last, [header])
    header = _yaml_opened_header(lines[entry.header], parsed)
    child_indent = entry.child_indent or (entry.indent + unit)
    if _yaml_body_is_sequence(lines, entry):
        # A list is replaced whole: its items carry no key MCC could match.
        body = [header]
        for key, item in value.items():
            body.extend(_yaml_render_entry(key, item, child_indent, unit, eol))
        return _yaml_replace_span(lines, entry.header, entry.last, body)
    remaining = dict(value)
    updated: list[_YamlLine] = []
    for item in _yaml_body_items(
        lines, entry.header + 1, entry.last, len(child_indent)
    ):
        if item.key is None:
            updated.extend(lines[item.start : item.end])
        elif item.key in remaining:
            updated.extend(
                _yaml_render_entry(
                    item.key, remaining.pop(item.key), child_indent, unit, eol
                )
            )
    # New keys go after the last line that is not blank, so a trailing gap
    # stays a trailing gap.
    insert_at = len(updated)
    while insert_at and not updated[insert_at - 1].body.strip():
        insert_at -= 1
    fresh: list[_YamlLine] = []
    for key, item in remaining.items():
        fresh.extend(_yaml_render_entry(key, item, child_indent, unit, eol))
    updated[insert_at:insert_at] = fresh
    return _yaml_replace_span(lines, entry.header, entry.last, [header, *updated])


def _yaml_body_items(
    lines: list[_YamlLine], start: int, end: int, indent_width: int
) -> list[_YamlItem]:
    """Split a mapping's body into keyed entries and opaque line ranges.

    An opaque line -- a comment, a blank line, a merge key this module does not
    read -- is preserved exactly where it is.
    """

    items: list[_YamlItem] = []
    index = start
    while index < end:
        body = lines[index].body
        stripped = body.lstrip(" \t")
        parsed = (
            _yaml_entry_line(body)
            if stripped and len(body) - len(stripped) == indent_width
            else None
        )
        if parsed is None:
            items.append(_YamlItem(None, index, index + 1))
            index += 1
            continue
        _full, last = _yaml_entry_end(lines, index, indent_width)
        last = min(last, end)
        items.append(_YamlItem(parsed[1], index, last))
        index = last
    return items


def _yaml_walk(
    lines: list[_YamlLine], key_path: Sequence[str]
) -> tuple[int, _YamlEntry | None, _YamlLevel]:
    """Return ``(matched, container, level)`` for one dotted path.

    ``container`` is the entry whose body is ``level``: the entry for the whole
    path when every segment matched, the entry a missing next segment belongs
    under otherwise, and None for the document root.
    """

    level = _YamlLevel(0, len(lines), 0)
    container: _YamlEntry | None = None
    for position, key in enumerate(key_path):
        found = _yaml_find_entry(lines, key, level.start, level.end, level.indent)
        if found is None:
            return position, container, level
        container = found
        level = _YamlLevel(found.header + 1, found.last, len(found.child_indent))
    return len(key_path), container, level


def _yaml_find_entry(
    lines: list[_YamlLine], key: str, start: int, end: int, indent_width: int
) -> _YamlEntry | None:
    """Find the mapping entry named ``key`` at exactly ``indent_width``."""

    for index in range(start, end):
        body = lines[index].body
        stripped = body.lstrip(" \t")
        if not stripped:
            continue
        width = len(body) - len(stripped)
        if width < indent_width:
            break
        if width > indent_width:
            continue
        parsed = _yaml_entry_line(body)
        if parsed is None:
            if _yaml_is_sequence_item(stripped):
                # A list item, not a mapping entry: no key can be here.
                return None
            continue
        if parsed[1] != key:
            continue
        _full, last = _yaml_entry_end(lines, index, indent_width)
        return _YamlEntry(
            key=key,
            key_text=parsed[2],
            indent=parsed[0],
            header=index,
            last=last,
            child_indent=_yaml_child_indent(lines, index, last),
        )
    return None


def _yaml_entry_end(
    lines: list[_YamlLine], header: int, indent_width: int
) -> tuple[int, int]:
    """Return ``(full_end, last)`` for the entry whose header line is ``header``.

    ``full_end`` includes the blank lines that follow the entry, because
    indentation cannot terminate a YAML block on a blank line; ``last`` stops at
    the entry's last non-blank line.
    """

    index = header + 1
    last = header + 1
    while index < len(lines):
        body = lines[index].body
        stripped = body.lstrip(" \t")
        if not stripped:
            index += 1
            continue
        if len(body) - len(stripped) <= indent_width:
            break
        index += 1
        last = index
    return index, last


def _yaml_child_indent(lines: list[_YamlLine], header: int, last: int) -> str:
    """The indentation of an entry's first child line, or "" when it has none."""

    for index in range(header + 1, last):
        body = lines[index].body
        stripped = body.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        parsed = _yaml_entry_line(body)
        return parsed[0] if parsed is not None else _yaml_indent(body)
    return ""


def _yaml_entry_has_content(lines: list[_YamlLine], entry: _YamlEntry) -> bool:
    """Whether anything of the document's is left inside one entry.

    Comments count. A parent holding only a comment MCC must not have written
    is the user's, and is kept.
    """

    return any(
        lines[index].body.strip() for index in range(entry.header + 1, entry.last)
    )


def _yaml_body_is_sequence(lines: list[_YamlLine], entry: _YamlEntry) -> bool:
    """Whether an entry's body is a block sequence rather than a mapping."""

    for index in range(entry.header + 1, entry.last):
        stripped = lines[index].body.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        return _yaml_is_sequence_item(stripped)
    return False


def _yaml_is_sequence_item(stripped: str) -> bool:
    """Whether a stripped line is a block-sequence item, not a document marker.

    A list item is ``-`` or ``- value``. ``---`` opens a document and ``...``
    closes one, and a naive ``startswith("-")`` takes the opener for a list --
    which would make the document's root unreadable, and a ``SetTable`` would
    then append a second copy of a table that is already there.
    """

    return stripped == "-" or stripped.startswith("- ")


def _yaml_entry_line(body: str) -> tuple[str, str, str, str] | None:
    """``(indent, key, key_text, rest)`` for one mapping-entry line, or None."""

    match = _YAML_ENTRY.match(body)
    if match is None:
        return None
    raw = match.group("key")
    return match.group("indent"), _yaml_key_name(raw), raw, match.group("rest")


def _yaml_key_name(raw: str) -> str:
    """The string a key's written form stands for."""

    if raw.startswith('"'):
        try:
            return str(json.loads(raw))
        except ValueError:
            return raw[1:-1]
    if raw.startswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw


def _yaml_split_comment(rest: str) -> tuple[str, str]:
    """Return ``(value, comment)``, splitting at the ``#`` that starts a comment.

    YAML starts a comment at a ``#`` that begins the text or follows
    whitespace, never inside a quoted scalar -- so ``"a # b"`` keeps its hash.
    """

    quote = ""
    index = 0
    while index < len(rest):
        char = rest[index]
        if quote:
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                if quote == "'" and rest.startswith("''", index):
                    index += 2
                    continue
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or rest[index - 1] in " \t"):
            return rest[:index], rest[index:]
        index += 1
    return rest, ""


def _yaml_opened_header(
    line: _YamlLine, parsed: tuple[str, str, str, str]
) -> _YamlLine:
    """Turn ``key: value`` into ``key:`` so children can go under it.

    A comment after the value stays; the value is what MCC is replacing. A line
    that is already a block header (or already carries only a comment) is
    returned as it is.
    """

    indent, _key, key_text, rest = parsed
    value, comment = _yaml_split_comment(rest)
    if not value.strip():
        return line
    rebuilt = f"{indent}{key_text}:"
    if comment.strip():
        rebuilt = f"{rebuilt} {comment.strip()}"
    return _YamlLine(rebuilt, line.ending)


def _yaml_render_entry(
    key: str, value: object, indent: str, unit: str, eol: str
) -> list[_YamlLine]:
    """Render one entry of an MCC-owned mapping, children indented by ``unit``."""

    key_text = _yaml_key(key)
    child = indent + unit
    if isinstance(value, Mapping):
        if not value:
            return [_YamlLine(f"{indent}{key_text}: {{}}", eol)]
        rendered = [_YamlLine(f"{indent}{key_text}:", eol)]
        for sub_key, sub_value in value.items():
            rendered.extend(
                _yaml_render_entry(str(sub_key), sub_value, child, unit, eol)
            )
        return rendered
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items = list(value)
        if not items:
            return [_YamlLine(f"{indent}{key_text}: []", eol)]
        rendered = [_YamlLine(f"{indent}{key_text}:", eol)]
        for item in items:
            if isinstance(item, Mapping | Sequence) and not isinstance(
                item, str | bytes | bytearray
            ):
                raise DocumentFormatError(
                    "cannot render a collection inside a YAML sequence"
                )
            rendered.append(_YamlLine(f"{child}- {_yaml_value(item)}", eol))
        return rendered
    return [_YamlLine(f"{indent}{key_text}: {_yaml_value(value)}", eol)]


def _yaml_key(key: str) -> str:
    """A key in a form a reader gives back as the same string."""

    if _YAML_BARE.match(key) and key.lower() not in _YAML_RESERVED:
        return key
    return json.dumps(key, ensure_ascii=False)


def _yaml_value(value: object) -> str:
    """One scalar of an MCC-owned block, in a form a reader gives back as-is.

    Stricter than :func:`_yaml_render`, which the top-level scalar edit has used
    since 6.x and which writes a string that looks like a number or a boolean
    bare -- ``active_provider: 123`` reads back as an int, not the string
    ``"123"``. That edit's rendering is pinned by Goose's documents and is left
    alone; a block MCC writes whole gets the version that round-trips.
    """

    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            return ".nan"
        if value == float("inf"):
            return ".inf"
        if value == float("-inf"):
            return "-.inf"
        return repr(value)
    if isinstance(value, str):
        if _YAML_BARE.match(value) and value.lower() not in _YAML_RESERVED:
            return value
        return json.dumps(value, ensure_ascii=False)
    raise DocumentFormatError(f"cannot render {type(value).__name__} as YAML")


def _yaml_place(
    lines: list[_YamlLine], index: int, rendered: Sequence[_YamlLine]
) -> list[_YamlLine]:
    """Insert lines at ``index``, keeping a document's missing final newline.

    A document that does not end with a line terminator keeps not ending with
    one: the terminator the insertion needs goes on the line before it, and the
    inserted last line inherits the absence.
    """

    insert = list(rendered)
    if index == len(lines) and lines and insert and not lines[-1].ending:
        lines = [*lines[:-1], _YamlLine(lines[-1].body, insert[0].ending)]
        insert[-1] = _YamlLine(insert[-1].body, "")
    return [*lines[:index], *insert, *lines[index:]]


def _yaml_replace_span(
    lines: list[_YamlLine], start: int, end: int, rendered: Sequence[_YamlLine]
) -> list[_YamlLine]:
    """Replace ``lines[start:end]``, keeping the document's final-newline state."""

    replaced = list(rendered)
    if end >= len(lines) and end > start and not lines[end - 1].ending and replaced:
        replaced[-1] = _YamlLine(replaced[-1].body, "")
    return [*lines[:start], *replaced, *lines[end:]]


def _yaml_cut(lines: list[_YamlLine], start: int, end: int) -> list[_YamlLine]:
    """Remove ``lines[start:end]``, undoing the terminator an append added.

    Deleting a block that ends a document whose last line has no terminator
    gives the terminator back to the line before it: that is the exact reverse
    of an append into a document that did not end with a newline.
    """

    lost = end >= len(lines) and end > start and not lines[end - 1].ending
    kept = [*lines[:start], *lines[end:]]
    if lost and kept[-1].ending:
        kept[-1] = _YamlLine(kept[-1].body, "")
    return kept


def _yaml_lines(text: str) -> list[_YamlLine]:
    """Split into physical lines, keeping each line's own terminator.

    ``str.splitlines`` is deliberately not used: it splits on Unicode
    separators YAML does not treat as line breaks, and it drops the
    terminators -- which is how a CRLF document becomes an LF one.
    """

    lines: list[_YamlLine] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
            lines.append(_YamlLine(text[start:index], "\r\n"))
            index += 2
            start = index
            continue
        if char in "\r\n":
            lines.append(_YamlLine(text[start:index], char))
            index += 1
            start = index
            continue
        index += 1
    if start < len(text):
        lines.append(_YamlLine(text[start:], ""))
    return lines


def _yaml_text(lines: Sequence[_YamlLine]) -> str:
    return "".join(f"{line.body}{line.ending}" for line in lines)


def _yaml_eol(text: str) -> str:
    """The terminator new lines are written with, read from the document."""

    for ending in ("\r\n", "\n", "\r"):
        if ending in text:
            return ending
    return "\n"


def _yaml_indent(body: str) -> str:
    """The leading whitespace of one line."""

    return body[: len(body) - len(body.lstrip(" \t"))]


def _yaml_indent_unit(lines: Sequence[_YamlLine]) -> str:
    """The document's own indentation step, or two spaces.

    Read from the first indented content line -- the same reading
    :func:`_json_indent_unit` makes: a document written with four spaces
    stays a four-space document, and a tab-indented one is not rewritten with
    spaces. A file whose first indented line is unusually deep reports that
    step, which is still what every other block in a hand-written file uses.
    """

    for line in lines:
        stripped = line.body.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        indent = _yaml_indent(line.body)
        if indent:
            return indent
    return "  "


def _yaml_render(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int | float):
        return repr(value)
    text = str(value)
    if text == "" or text != text.strip() or text[0] in "&*!{[|>'\"%@`#":
        return json.dumps(text)
    return text


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------
#
# Edited as text, for the same reason TOML and YAML are. Until 6.83.0 JSON was
# the one format here that went round through its object model and back out
# through ``json.dumps(indent=2)``, and the module docstring's argument for why
# that was acceptable -- "there is no layout to preserve that a canonical
# writer would lose in a way anyone notices" -- was measured wrong twice over.
# A Roo Code Configure against a four-space ``settings.json`` rewrote all 32
# lines of it and changed nothing else; and a ``settings.json`` carrying the
# comments VS Code itself documents, ships in its own default file and parses
# without complaint made Configure fail outright with "cannot parse", because
# ``json.loads`` is not the parser VS Code uses.
#
# So JSON gets the two promises the other text formats already had: the lines
# MCC does not own come back byte for byte, comments included, and a re-apply
# of identical content does not touch the file at all.


class _JsonValue:
    """One value in a JSON document, with the span of text it occupies."""

    __slots__ = ("end", "kind", "members", "start")

    def __init__(self, start: int, end: int, kind: str) -> None:
        self.start = start
        self.end = end
        #: ``object``, ``array`` or ``scalar``.
        self.kind = kind
        #: Members in document order, for an object.
        self.members: dict[str, _JsonMember] = {}


class _JsonMember:
    """One ``"key": value`` pair, spanning from the first byte of the key."""

    __slots__ = ("end", "key", "start", "value")

    def __init__(self, key: str, start: int, value: _JsonValue) -> None:
        self.key = key
        self.start = start
        self.end = value.end
        self.value = value


def _json_skip(text: str, index: int) -> int:
    """Return the next index that is neither whitespace nor a comment.

    The comments are JSONC's -- ``//`` to end of line and ``/* */`` -- which is
    what a VS Code ``settings.json`` is written in. They are *skipped*, never
    removed: the bytes carrying them are not MCC's to rewrite.
    """

    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char == "/" and index + 1 < length:
            following = text[index + 1]
            if following == "/":
                end = text.find("\n", index)
                index = length if end == -1 else end + 1
                continue
            if following == "*":
                end = text.find("*/", index + 2)
                if end == -1:
                    return length
                index = end + 2
                continue
        return index
    return length


def _json_scan_string(text: str, index: int) -> tuple[str, int]:
    """Return the string literal at ``index`` and the index just after it."""

    if index >= len(text) or text[index] != '"':
        raise DocumentFormatError(f"expected a string at offset {index}")
    cursor = index + 1
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == '"':
            decoded = json.loads(text[index : cursor + 1])
            return str(decoded), cursor + 1
        cursor += 1
    raise DocumentFormatError(f"unterminated string at offset {index}")


def _json_scan_value(text: str, index: int) -> _JsonValue:
    """Return the value starting at ``index``, spans and members filled in."""

    index = _json_skip(text, index)
    if index >= len(text):
        raise DocumentFormatError("the document ended where a value was expected")
    char = text[index]
    if char == "{":
        return _json_scan_object(text, index)
    if char == "[":
        return _json_scan_array(text, index)
    if char == '"':
        _value, end = _json_scan_string(text, index)
        return _JsonValue(index, end, "scalar")
    end = index
    while end < len(text) and text[end] not in ",}] \t\r\n":
        end += 1
    if end == index:
        raise DocumentFormatError(f"expected a value at offset {index}")
    return _JsonValue(index, end, "scalar")


def _json_scan_object(text: str, index: int) -> _JsonValue:
    node = _JsonValue(index, index, "object")
    cursor = _json_skip(text, index + 1)
    while cursor < len(text) and text[cursor] != "}":
        key, after_key = _json_scan_string(text, cursor)
        colon = _json_skip(text, after_key)
        if colon >= len(text) or text[colon] != ":":
            raise DocumentFormatError(f"expected ':' at offset {colon}")
        value = _json_scan_value(text, colon + 1)
        node.members[key] = _JsonMember(key, cursor, value)
        cursor = _json_skip(text, value.end)
        if cursor < len(text) and text[cursor] == ",":
            cursor = _json_skip(text, cursor + 1)
    if cursor >= len(text):
        raise DocumentFormatError("unterminated object")
    node.end = cursor + 1
    return node


def _json_scan_array(text: str, index: int) -> _JsonValue:
    node = _JsonValue(index, index, "array")
    cursor = _json_skip(text, index + 1)
    while cursor < len(text) and text[cursor] != "]":
        element = _json_scan_value(text, cursor)
        cursor = _json_skip(text, element.end)
        if cursor < len(text) and text[cursor] == ",":
            cursor = _json_skip(text, cursor + 1)
    if cursor >= len(text):
        raise DocumentFormatError("unterminated array")
    node.end = cursor + 1
    return node


def parse_jsonc(text: str) -> object:
    """Return a JSON document that may carry comments and trailing commas.

    The scanner above already knows where every token is, so the value is
    rebuilt from the spans rather than by stripping comments with a regex over
    the whole document first. A regex cannot tell ``//`` inside a string from
    the start of a comment, and the strings in these files are URLs.
    """

    root = _json_scan_value(text, 0)
    trailing = _json_skip(text, root.end)
    if trailing != len(text):
        raise DocumentFormatError(f"trailing content at offset {trailing}")
    return _json_node_value(text, root)


def _json_node_value(text: str, node: _JsonValue) -> object:
    if node.kind == "object":
        return {
            key: _json_node_value(text, member.value)
            for key, member in node.members.items()
        }
    if node.kind == "array":
        return [
            _json_node_value(text, element)
            for element in _json_array_elements(text, node)
        ]
    return json.loads(text[node.start : node.end])


def _json_array_elements(text: str, node: _JsonValue) -> list[_JsonValue]:
    elements: list[_JsonValue] = []
    cursor = _json_skip(text, node.start + 1)
    while cursor < node.end - 1 and text[cursor] != "]":
        element = _json_scan_value(text, cursor)
        elements.append(element)
        cursor = _json_skip(text, element.end)
        if cursor < len(text) and text[cursor] == ",":
            cursor = _json_skip(text, cursor + 1)
    return elements


def mask_json_text(text: str, secret_keys: Collection[str]) -> str:
    """Return the document with every credential-shaped value replaced by ``***``.

    Masking a JSON document as *text* rather than as an object is what lets a
    plan diff show the user's own bytes -- their comments, their indentation --
    without showing their token. The line-based masker the text formats use
    cannot do it: a JSON member is ``"apiKey": "sk-…",`` and rewriting that
    line as ``apiKey : "***"`` costs the quotes around the key and the comma
    after the value, leaving a preview that is not the JSON it claims to be.

    ``secret_keys`` is a denylist of key names, normalised to lowercase with
    ``-`` read as ``_``, and it is the caller's: this module has no opinion
    about which field is a credential.
    """

    names = {key.lower().replace("-", "_") for key in secret_keys}
    try:
        root = _json_scan_value(text, 0)
    except DocumentFormatError:
        return text
    spans: list[tuple[int, int]] = []
    _json_secret_spans(text, root, names, spans)
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + json.dumps(MASK) + text[end:]
    return text


#: What a masked value is rendered as. The same three characters
#: ``config/desktop_apply`` shows, spelled here because this module is what
#: writes them into a JSON document's text.
MASK = "***"


def _json_secret_spans(
    text: str,
    node: _JsonValue,
    names: set[str],
    spans: list[tuple[int, int]],
) -> None:
    if node.kind == "object":
        for key, member in node.members.items():
            if key.lower().replace("-", "_") in names:
                spans.append((member.value.start, member.value.end))
                continue
            _json_secret_spans(text, member.value, names, spans)
        return
    if node.kind == "array":
        for element in _json_array_elements(text, node):
            _json_secret_spans(text, element, names, spans)


_JSON_INDENT = re.compile(r"^([ \t]+)\S", re.MULTILINE)


def _json_indent_unit(text: str) -> str:
    """Return the document's own indentation step, or two spaces.

    Read from the file rather than assumed, because the point of this path is
    that a four-space document stays a four-space document. The first indented
    line settles it: a tab-indented file and a four-space file differ on their
    first nested key.
    """

    match = _JSON_INDENT.search(text)
    return match.group(1) if match else "  "


def _json_line_indent(text: str, index: int) -> str:
    """Return the whitespace starting the line ``index`` sits on, or ""."""

    start = text.rfind("\n", 0, index) + 1
    prefix = text[start:index]
    return prefix if prefix.strip() == "" else ""


def _json_render(value: object, indent_unit: str, indent: str) -> str:
    """Render one value as a member of an object whose members sit at ``indent``."""

    rendered = json.dumps(value, indent=indent_unit)
    return ("\n" + indent).join(rendered.split("\n"))


def _apply_json_edits(text: str, edits: Sequence[Edit]) -> str:
    for edit in edits:
        match edit:
            case SetTable(key_path=key_path, value=value):
                text = _json_set(text, key_path, dict(value))
            case SetScalar(key_path=key_path, value=value, prior_line=prior_line):
                text = _json_set(text, key_path, value, prior_text=prior_line)
            case DeleteKey(key_path=key_path, prune_empty_ancestors=prune):
                text = _json_delete(text, key_path, prune=prune)
    return text


def _json_root_object(text: str) -> _JsonValue:
    root = _json_scan_value(text, 0)
    if root.kind != "object":
        raise DocumentFormatError("MCC edits only the keys of a JSON object")
    return root


def _json_set(
    text: str, key_path: Sequence[str], value: object, *, prior_text: str = ""
) -> str:
    """Set one key, rewriting only the bytes of the value it replaces."""

    if not key_path:
        raise DocumentFormatError("a JSON edit needs a key")
    node = _json_root_object(text)
    for depth, key in enumerate(key_path[:-1]):
        member = node.members.get(key)
        if member is None or member.value.kind != "object":
            # The chain of objects stops here, so the rest of the path is
            # rendered as one nested value and set at this level. Replacing a
            # non-object with the object MCC needs is what the object path
            # does too.
            nested: object = value
            for part in reversed(key_path[depth + 1 :]):
                nested = {part: nested}
            return _json_set_member(text, node, key, nested)
        node = member.value
    return _json_set_member(text, node, key_path[-1], value, prior_text)


def _json_set_member(
    text: str, node: _JsonValue, key: str, value: object, prior_text: str = ""
) -> str:
    indent_unit = _json_indent_unit(text)
    member = node.members.get(key)
    if member is None:
        return _json_insert_member(text, node, key, value, indent_unit)
    if _json_node_value(text, member.value) == value:
        # The byte-identical re-apply. Rendering an equal value again could
        # still change the file -- the user may have written the same object
        # with different spacing -- and a diff nobody asked for is exactly
        # what this path exists to stop.
        return text
    if prior_text and _json_text_says(prior_text, value):
        # The user's own bytes for exactly this value: Undo of a replaced key
        # gives back their spelling, not a re-render of it. ``json.dumps``
        # would escape non-ASCII and re-quote a string the user wrote some
        # other way; this is the JSON half of the TOML ``prior_line`` rule.
        return text[: member.value.start] + prior_text + text[member.value.end :]
    indent = _json_line_indent(text, member.start)
    rendered = _json_render(value, indent_unit, indent)
    return text[: member.value.start] + rendered + text[member.value.end :]


def _json_text_says(prior_text: str, value: object) -> bool:
    """Return whether one span of JSON text is exactly ``value``.

    Type-aware, because JSON is: ``true`` is not ``1`` and ``1`` is not
    ``1.0``, though Python compares them equal in one direction.
    """

    try:
        parsed = parse_jsonc(prior_text)
    except DocumentFormatError:
        return False
    return type(parsed) is type(value) and parsed == value


def scalar_text(
    text: str, document_format: DocumentFormat, key_path: Sequence[str]
) -> str:
    """Return the exact text one key's value occupies, or "" when it has none.

    JSON only, and the counterpart of :func:`root_scalar_line` for a document
    edited as text: what Configure records beside a value it replaces, so Undo
    writes the user's bytes back rather than a canonical re-render of the same
    value. "" for every other format, for a missing key, and for text that
    does not parse -- which is exactly the record every caller wrote before
    this existed, and those still undo by value.
    """

    if document_format is not DocumentFormat.JSON or not key_path:
        return ""
    try:
        node = _json_root_object(text)
    except DocumentFormatError:
        return ""
    member: _JsonMember | None = None
    for key in key_path:
        if node.kind != "object":
            return ""
        member = node.members.get(key)
        if member is None:
            return ""
        node = member.value
    if member is None or member.value.kind != "scalar":
        return ""
    return text[member.value.start : member.value.end]


def _json_insert_member(
    text: str, node: _JsonValue, key: str, value: object, indent_unit: str
) -> str:
    if node.members:
        anchor = max(member.end for member in node.members.values())
        member_indent = _json_line_indent(
            text, min(member.start for member in node.members.values())
        )
        rendered = _json_render(value, indent_unit, member_indent)
        member_text = f"{json.dumps(key)}: {rendered}"
        cursor = _json_skip(text, anchor)
        if cursor < len(text) and text[cursor] == ",":
            # The document already ends its last member with a comma, which
            # JSONC allows. Going in after it keeps that habit rather than
            # producing two commas or moving the user's.
            return (
                text[: cursor + 1]
                + f"\n{member_indent}{member_text},"
                + text[cursor + 1 :]
            )
        return text[:anchor] + f",\n{member_indent}{member_text}" + text[anchor:]

    base_indent = _json_line_indent(text, node.start)
    member_indent = base_indent + indent_unit
    rendered = _json_render(value, indent_unit, member_indent)
    member_text = f"{json.dumps(key)}: {rendered}"
    if text[node.start + 1 : node.end - 1].strip():
        # An "empty" object that is not empty: it carries a comment. The
        # comment stays and the member goes in after it.
        return (
            text[: node.end - 1]
            + f"\n{member_indent}{member_text}\n{base_indent}"
            + text[node.end - 1 :]
        )
    return (
        text[: node.start]
        + "{\n"
        + f"{member_indent}{member_text}\n{base_indent}}}"
        + text[node.end :]
    )


def _json_delete(text: str, key_path: Sequence[str], *, prune: bool = True) -> str:
    """Remove one key, and the ancestors MCC's departure left empty.

    Pruning is the same promise ``config/desktop_apply._prune_empty_ancestors``
    makes for the object path: ``provider.mcc`` in a file that had no
    ``provider`` map means Configure created ``provider`` too, and leaving
    ``"provider": {}`` behind is MCC's litter in a document it promised to
    leave alone. An ancestor still holding somebody else's keys is theirs.
    ``prune=False`` removes the key alone, for a container the caller knows
    was there before MCC.
    """

    stop = 0 if prune else len(key_path) - 1
    for depth in range(len(key_path), stop, -1):
        path = key_path[:depth]
        node = _json_root_object(text)
        member: _JsonMember | None = None
        for key in path:
            if node.kind != "object":
                member = None
                break
            member = node.members.get(key)
            if member is None:
                break
            node = member.value
        if member is None:
            continue
        if depth < len(key_path) and (
            member.value.kind != "object" or member.value.members
        ):
            break
        text = _json_cut_member(text, member)
    return _json_collapse_empty_root(text)


def _json_collapse_empty_root(text: str) -> str:
    """Return ``{}`` where removing MCC's last key left an empty root.

    Only the root, and only when what is left between the braces is whitespace:
    every deeper container MCC emptied is pruned outright by :func:`_json_delete`
    above, and a body with a comment in it is somebody's writing. Without this,
    a Configure and an Undo against a file that was ``{}`` gave back ``{\\n}``
    -- two bytes away from where it started, which is exactly the promise the
    byte-preserving path exists to keep.
    """

    root = _json_root_object(text)
    if root.members:
        return text
    body = text[root.start + 1 : root.end - 1]
    if not body or body.strip():
        return text
    return text[: root.start] + "{}" + text[root.end :]


def _json_cut_member(text: str, member: _JsonMember) -> str:
    """Return the text with one member, its separator and its blank line gone."""

    start, end = member.start, member.end
    following = _json_skip(text, end)
    if following < len(text) and text[following] == ",":
        end = following + 1
        # And the space the comma was followed by, so removing a member from a
        # single-line object does not leave ``{ "other": 2}`` behind.
        while end < len(text) and text[end] in " \t":
            end += 1
    else:
        # The last member: take the comma that separated it from the one
        # before instead, or the object is left with a dangling separator.
        cursor = start - 1
        while cursor >= 0 and text[cursor] in " \t\r\n":
            cursor -= 1
        if cursor >= 0 and text[cursor] == ",":
            start = cursor
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip() == "":
        trailing = end
        while trailing < len(text) and text[trailing] in " \t\r":
            trailing += 1
        if trailing < len(text) and text[trailing] == "\n":
            # The member had its own line or lines: take the indentation
            # before it and the newline after it, so no blank line is left.
            start, end = line_start, trailing + 1
    return text[:start] + text[end:]
