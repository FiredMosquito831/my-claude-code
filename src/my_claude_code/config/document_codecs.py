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
    Goose's ``config.yaml``. One kind of edit: a top-level scalar
    (``active_provider``). Goose's real provider definition is a *whole file*
    MCC owns under ``custom_providers/``, so nothing here ever has to write a
    nested YAML structure into a document somebody else wrote -- which is the
    only reason a hand-rolled YAML edit is defensible at all.

A format is not given an edit it cannot perform safely. Asking for a nested
scalar in YAML raises rather than guessing, because a wrong guess here is a
corrupted config file for somebody who was not even using the feature.
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


@dataclass(frozen=True, slots=True)
class DeleteKey:
    """Remove one key at a key path, table or scalar, if it is there."""

    key_path: tuple[str, ...]


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
            case SetScalar(key_path=key_path, value=value):
                if len(key_path) != 1:
                    raise DocumentFormatError(
                        "TOML scalar edits are top-level only; a nested scalar "
                        "belongs to a table MCC would have to own whole"
                    )
                text = _toml_set_root_scalar(text, key_path[0], value)
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


def _toml_set_root_scalar(text: str, key: str, value: object) -> str:
    lines = text.splitlines()
    limit = _toml_root_region_end(lines)
    pattern = _toml_root_key_pattern(key)
    rendered = f"{_toml_key(key)} = {_toml_value(value)}"

    for index in range(limit):
        if pattern.match(lines[index]):
            lines[index] = rendered
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    lines.insert(limit, rendered)
    return "\n".join(lines) + ("\n" if text.endswith("\n") or not text else "")


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
# Deliberately shallow. MCC edits exactly one top-level scalar in a YAML
# document it does not own (Goose's ``active_provider``), and reads the rest
# only to report status. A full YAML parser would let this module accept edits
# it has no safe way to write back, so the reader is as narrow as the writer.


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
                        "YAML edits are top-level scalars only; MCC owns Goose's "
                        "provider as a whole file rather than a nested block"
                    )
                text = _yaml_set_root_scalar(text, key_path[0], value)
            case DeleteKey(key_path=key_path):
                if len(key_path) != 1:
                    raise DocumentFormatError("YAML deletes are top-level only")
                text = _yaml_delete_root_scalar(text, key_path[0])
            case SetTable():
                raise DocumentFormatError(
                    "MCC never writes a table into a YAML document it does not own"
                )
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


def _yaml_delete_root_scalar(text: str, key: str) -> str:
    lines = text.splitlines()
    pattern = _yaml_root_key_pattern(key)
    for index, line in enumerate(lines):
        if pattern.match(line):
            del lines[index]
            if not lines:
                return ""
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return text


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
            case SetScalar(key_path=key_path, value=value):
                text = _json_set(text, key_path, value)
            case DeleteKey(key_path=key_path):
                text = _json_delete(text, key_path)
    return text


def _json_root_object(text: str) -> _JsonValue:
    root = _json_scan_value(text, 0)
    if root.kind != "object":
        raise DocumentFormatError("MCC edits only the keys of a JSON object")
    return root


def _json_set(text: str, key_path: Sequence[str], value: object) -> str:
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
    return _json_set_member(text, node, key_path[-1], value)


def _json_set_member(text: str, node: _JsonValue, key: str, value: object) -> str:
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
    indent = _json_line_indent(text, member.start)
    rendered = _json_render(value, indent_unit, indent)
    return text[: member.value.start] + rendered + text[member.value.end :]


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


def _json_delete(text: str, key_path: Sequence[str]) -> str:
    """Remove one key, and the ancestors MCC's departure left empty.

    Pruning is the same promise ``config/desktop_apply._prune_empty_ancestors``
    makes for the object path: ``provider.mcc`` in a file that had no
    ``provider`` map means Configure created ``provider`` too, and leaving
    ``"provider": {}`` behind is MCC's litter in a document it promised to
    leave alone. An ancestor still holding somebody else's keys is theirs.
    """

    for depth in range(len(key_path), 0, -1):
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
