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
    The shape Command Code, OpenCode and Crush use. There is no layout to
    preserve that a canonical writer would lose in a way anyone notices, and
    ``config/atomic_json.py`` has emitted the canonical shape since 6.27.0, so
    JSON keeps the load-modify-dump path it already had. Changing it would
    reformat the documents of every user MCC has already configured.

``JSON_ARRAY``
    VS Code's ``chatLanguageModels.json`` is a bare array with no key to own.
    Ownership is instead "the one element whose ``name`` equals MCC's". The
    array is rewritten canonically like JSON, but no other element is
    reordered, rewritten or removed.

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
from collections.abc import Mapping, Sequence
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
                return json.loads(text)
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
