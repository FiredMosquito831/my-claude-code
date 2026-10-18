"""The operator's own statements of what a media model's request takes (7.98.0).

Agnes publishes nothing MCC reads about its request fields, and no catalogue
does either (models.dev: 0 parameter fields on 369 media models). User decision
5 of 2026-10-16: the operator may type a model's fields on the Models page.
This module is that file -- ``~/.mcc/media_request_fields.json``::

    {
      "models": {
        "custom_agnes/agnes-video-2.5": {
          "video_create": {
            "mode": {"state": "required", "values": ["std", "pro"],
                     "since": "2026-10-18"},
            "image_config": {"state": "refused", "since": "2026-10-18"}
          }
        }
      }
    }

A statement is the top rung of the model's published request schema
(``application/media/schemas.py``) and nothing else: **it is never sent, never
checked against a request and never changes a route** (user decision 6). That
is also why it is its own document rather than rows in
``model_overrides.json``, whose parameters ARE written into request bodies and
whose allow-list is a body-injection boundary.

The conventions are ``model_overrides.json``'s: model refs are folded for
lookup, the document is written atomically, and a save keeps the order things
were typed in (``atomic_json`` never sorts keys). Anything this module cannot
read is dropped with one log line; a file it cannot read at all is "nothing
stated".

Leaf module: ``config`` imports nothing first-party outside itself.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from my_claude_code.config.atomic_json import write_json_document_atomically
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CREATE,
)
from my_claude_code.config.model_overrides import normalize_override_key
from my_claude_code.config.paths import media_request_fields_path

MODELS_KEY = "models"

#: What the operator can say about one field.
OPERATOR_ACCEPTED = "accepted"
OPERATOR_REQUIRED = "required"
OPERATOR_REFUSED = "refused"
OPERATOR_STATES: tuple[str, ...] = (
    OPERATOR_ACCEPTED,
    OPERATOR_REQUIRED,
    OPERATOR_REFUSED,
)

#: The JSON types a statement may name for its field.
FIELD_TYPES: tuple[str, ...] = (
    "string",
    "integer",
    "number",
    "boolean",
    "array",
    "object",
)

#: The operations a rail routes -- the only ones a request schema exists for.
STATED_OPERATIONS: tuple[str, ...] = (
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CREATE,
)

#: A field is one top-level JSON key.
FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
#: Bounds on a stated value list, so a typo cannot grow the file without end.
MAX_VALUES = 64
MAX_VALUE_CHARS = 200
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True, slots=True)
class OperatorFieldStatement:
    """What the operator said about one field of one model's operation."""

    state: str
    #: The values the operator listed, or ``None`` for "not said".
    values: tuple[str | int | float | bool, ...] | None = None
    #: The JSON type the operator named, or ``None``.
    type: str | None = None
    #: The day it was saved (``YYYY-MM-DD``), or ``None`` for a hand edit.
    since: str | None = None

    def as_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {"state": self.state}
        if self.values is not None:
            row["values"] = list(self.values)
        if self.type is not None:
            row["type"] = self.type
        if self.since is not None:
            row["since"] = self.since
        return row


#: ``operation -> field -> statement``, in the order they were typed.
type OperationStatements = Mapping[str, Mapping[str, OperatorFieldStatement]]


@dataclass(frozen=True, slots=True)
class MediaRequestFieldStatements:
    """Every statement, keyed by folded model ref."""

    models: Mapping[str, OperationStatements] = field(default_factory=dict)

    def for_model(self, model_ref: str) -> OperationStatements:
        """This model's statements, by operation; empty when none."""

        return self.models.get(normalize_override_key(model_ref), {})

    def as_document(self) -> dict[str, Any]:
        return {
            MODELS_KEY: {
                ref: {
                    operation: {
                        name: statement.as_row() for name, statement in fields.items()
                    }
                    for operation, fields in operations.items()
                }
                for ref, operations in self.models.items()
            }
        }


EMPTY_STATEMENTS = MediaRequestFieldStatements()


def statement_problem(
    operation: str,
    field_name: str,
    state: str,
    values: Sequence[object] | None,
    field_type: str | None,
) -> str | None:
    """Why one statement cannot be stored, or ``None`` when it can."""

    if operation not in STATED_OPERATIONS:
        return f"operation must be one of {', '.join(STATED_OPERATIONS)}"
    if not FIELD_NAME.fullmatch(field_name):
        return "field must be one JSON key: letters, digits and _, at most 64"
    if state not in OPERATOR_STATES:
        return f"state must be one of {', '.join(OPERATOR_STATES)}"
    if field_type is not None and field_type not in FIELD_TYPES:
        return f"type must be one of {', '.join(FIELD_TYPES)}"
    if values is not None:
        if len(values) > MAX_VALUES:
            return f"at most {MAX_VALUES} values"
        for value in values:
            if value is None or isinstance(value, Mapping | list):
                return "each value must be a string, a number or true/false"
            if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
                return f"a value is longer than {MAX_VALUE_CHARS} characters"
    return None


def _statement(
    raw: object, ref: str, operation: str, field_name: str
) -> OperatorFieldStatement | None:
    where = f"{ref}.{operation}.{field_name}"
    if not isinstance(raw, Mapping):
        logger.warning(
            "MEDIA REQUEST FIELDS: '{}' is not an object; ignoring it", where
        )
        return None
    state = str(raw.get("state") or "").strip()
    raw_values = raw.get("values")
    values = list(raw_values) if isinstance(raw_values, list) else None
    raw_type = raw.get("type")
    field_type = raw_type.strip() if isinstance(raw_type, str) else None
    problem = statement_problem(operation, field_name, state, values, field_type)
    if problem is not None:
        logger.warning("MEDIA REQUEST FIELDS: '{}' {}; ignoring it", where, problem)
        return None
    since = raw.get("since")
    # Every value already passed ``statement_problem``; this only narrows the
    # type to what it proved (a string, a number or true/false).
    scalars = (
        None
        if values is None
        else tuple(value for value in values if isinstance(value, str | int | float))
    )
    return OperatorFieldStatement(
        state=state,
        values=scalars,
        type=field_type or None,
        since=since if isinstance(since, str) and _DATE.fullmatch(since) else None,
    )


def statements_from_document(document: object) -> MediaRequestFieldStatements:
    """Build the table from a parsed document, keeping what it can use."""

    if not isinstance(document, Mapping):
        logger.warning(
            "MEDIA REQUEST FIELDS: top-level JSON value is not an object; ignoring it"
        )
        return EMPTY_STATEMENTS
    section = document.get(MODELS_KEY)
    if section is None:
        return EMPTY_STATEMENTS
    if not isinstance(section, Mapping):
        logger.warning("MEDIA REQUEST FIELDS: '{}' is not an object", MODELS_KEY)
        return EMPTY_STATEMENTS
    models: dict[str, dict[str, dict[str, OperatorFieldStatement]]] = {}
    for raw_ref, raw_operations in section.items():
        ref = normalize_override_key(str(raw_ref))
        if not ref or "/" not in ref or not isinstance(raw_operations, Mapping):
            logger.warning(
                "MEDIA REQUEST FIELDS: '{}' is not a provider/model ref with "
                "operations; ignoring it",
                raw_ref,
            )
            continue
        for raw_operation, raw_fields in raw_operations.items():
            operation = str(raw_operation).strip()
            if not isinstance(raw_fields, Mapping):
                continue
            for raw_name, raw_statement in raw_fields.items():
                name = str(raw_name).strip()
                statement = _statement(raw_statement, ref, operation, name)
                if statement is None:
                    continue
                models.setdefault(ref, {}).setdefault(operation, {})[name] = statement
    return MediaRequestFieldStatements(models=models)


def load_media_request_fields(path: Path | None = None) -> MediaRequestFieldStatements:
    """Read the file, treating every failure as "nothing stated"."""

    resolved = path if path is not None else media_request_fields_path()
    try:
        raw = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY_STATEMENTS
    except OSError as exc:
        logger.warning("MEDIA REQUEST FIELDS: cannot read {}: {}", resolved, exc)
        return EMPTY_STATEMENTS
    if not raw.strip():
        return EMPTY_STATEMENTS
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("MEDIA REQUEST FIELDS: cannot parse {}: {}", resolved, exc)
        return EMPTY_STATEMENTS
    return statements_from_document(document)


def save_media_request_fields(
    statements: MediaRequestFieldStatements, path: Path | None = None
) -> None:
    """Write the file atomically, in the order the statements were typed."""

    resolved = path if path is not None else media_request_fields_path()
    write_json_document_atomically(resolved, statements.as_document())


def with_statement(
    statements: MediaRequestFieldStatements,
    model_ref: str,
    operation: str,
    field_name: str,
    statement: OperatorFieldStatement,
) -> MediaRequestFieldStatements:
    """The table with one statement set; an existing one keeps its place."""

    ref = normalize_override_key(model_ref)
    models = {
        key: {op: dict(fields) for op, fields in operations.items()}
        for key, operations in statements.models.items()
    }
    models.setdefault(ref, {}).setdefault(operation, {})[field_name] = statement
    return MediaRequestFieldStatements(models=models)


def without_statement(
    statements: MediaRequestFieldStatements,
    model_ref: str,
    operation: str,
    field_name: str,
) -> tuple[MediaRequestFieldStatements, bool]:
    """The table with one statement removed, and whether there was one."""

    ref = normalize_override_key(model_ref)
    models = {
        key: {op: dict(fields) for op, fields in operations.items()}
        for key, operations in statements.models.items()
    }
    fields = models.get(ref, {}).get(operation)
    if fields is None or field_name not in fields:
        return statements, False
    del fields[field_name]
    if not fields:
        del models[ref][operation]
    if not models[ref]:
        del models[ref]
    return MediaRequestFieldStatements(models=models), True
