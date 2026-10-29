"""What a value was before MCC overwrote it, so Undo can put it back.

MCC's merge model is "one key, one owner": it writes a subtree nobody else was
using and every other byte of the user's document survives untouched. Where
that holds, Undo is just a delete and this module is not needed.

Three things in the v1 desktop set break out of it, and one shipped feature
already had:

* Codex's ``model`` and ``model_provider`` are top-level scalars a user may
  already have set to something of their own.
* Goose's ``active_provider`` is the same shape.
* VS Code's ``chatLanguageModels.json`` is a bare array whose elements are
  matched by ``name``, so "MCC's element" is a claim about an existing
  document rather than a fresh key.
* ``config/claude_settings.py`` overwrites ``ANTHROPIC_BASE_URL`` without
  recording what was there. A user who had that variable pointed at another
  gateway lost it on Configure and did not get it back on Undo.

So Configure records, for every value it *overwrote* rather than created, the
prior value and whether the key was present at all; and Undo offers two modes:

``KEYS_ONLY``
    Delete what MCC *created*, put back what MCC *replaced*, and leave every
    foreign byte identical. The default, and the mode that never refuses: with
    no record it can only delete, and it says so rather than erroring. Since
    7.111.4 it does not delete a replaceable key from a document that holds
    nothing of MCC's -- no owned key and no live record -- because there that
    key can only be the user's (it used to delete a never-configured Codex
    user's ``model`` line).

``RESTORE``
    The same, guaranteed: it requires the record and refuses when the document
    has been rewritten since. Only this mode consumes the record.

Both read it, which is the 6.56.0 correction. Before that, ``KEYS_ONLY``
deleted a replaced value outright and then dropped the record, so a user's own
Codex ``model`` was destroyed and ``RESTORE`` could no longer bring it back.

**Why the document hash is stored.** A restore is only honest if the document
is still the one MCC edited. Both mature peers surveyed for this work shipped
a restore that wrote a backup over the wrong path and added hash keying
afterwards; recording ``document_sha256`` at apply time means a restore into a
document that has since been rewritten by hand can *say so* instead of
silently reverting an edit the user made on purpose.

The record lives in MCC's own directory, never beside the user's file, and is
mode 0600 because a prior value can be a credential -- the very case that
makes restoring worth doing.

Since 7.100.0 it also remembers the media MCP server entry MCC adds to a
user's own file (``mcp_servers`` below): which keys it created, the exact text
it inserted, and whether the file or its folder existed at all -- the facts
that let *Remove MCP server* and Undo give the file back byte for byte, and
that let a card tell MCC's entry from one the user wrote under the same name.
"""

import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from my_claude_code.config.atomic_json import write_json_document_atomically
from my_claude_code.config.paths import config_dir_path

#: Name of the side record inside MCC's config directory.
RESTORE_RECORD_FILENAME = "restore-record.json"

#: Sentinel distinguishing "the key was absent" from "the key held null".
#: JSON cannot express the difference, so it is carried in its own field and
#: this constant only names the intent for a reader.
KEY_WAS_ABSENT = "absent"


class UndoMode(StrEnum):
    """Which of the two undo modes the caller asked for."""

    #: Remove MCC's keys, put back any value MCC replaced, and leave every
    #: other byte exactly as it is. Never refuses: no record and no hash check.
    KEYS_ONLY = "keys_only"
    #: The same, but guaranteed against the record: it requires one and
    #: refuses when the document has been rewritten since MCC wrote it. This
    #: is the mode that consumes the record.
    RESTORE = "restore"


@dataclass(frozen=True, slots=True)
class OverwrittenValue:
    """One value MCC replaced, and what it was."""

    #: The key, outermost first, as it is addressed inside the document.
    key_path: tuple[str, ...]
    #: What the key held before MCC wrote to it. Meaningless when absent.
    prior_value: object
    #: Whether the key existed at all. A restore deletes when this is False.
    prior_present: bool
    #: The user's own line for the key, verbatim, where the document is edited
    #: as lines (a TOML top-level scalar, 7.111.4) -- so Undo puts back their
    #: comment and spelling, not a re-rendering of ``prior_value``. "" where
    #: there is no such line, and in every record written before 7.111.4:
    #: those still undo, by value, exactly as they did.
    prior_line: str = ""

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "key_path": list(self.key_path),
            "prior_value": self.prior_value,
            "prior_present": self.prior_present,
        }
        # Only when there is one, so every record written before this field
        # existed is written exactly as before.
        if self.prior_line:
            payload["prior_line"] = self.prior_line
        return payload


@dataclass(frozen=True, slots=True)
class RestoreEntry:
    """Everything Undo needs about one document MCC edited."""

    #: Which card wrote it -- a desktop app id, or ``claude_code``.
    subject: str
    #: Absolute path of the document, as a string, for a human to read.
    document_path: str
    #: SHA-256 of the document as MCC left it, so a restore can detect drift.
    document_sha256: str
    #: The values MCC replaced. Empty for a spec that only creates keys.
    overwritten: tuple[OverwrittenValue, ...]
    #: When Undo last ran for this subject, as an ISO-8601 UTC timestamp, or
    #: "" while MCC's configuration is supposed to be in place.
    #:
    #: This is the mark that makes "the user undid it" a different fact from
    #: "the application deleted MCC's keys". Until 6.84.0 there was none:
    #: ``KEYS_ONLY`` deliberately kept its record -- it has to, or the second
    #: undo mode goes away -- so a record for an app whose block is gone meant
    #: nothing at all, and a probe could only report "installed, not
    #: configured". That is the exact ambiguity spec section 2.5 could not
    #: resolve about Codex: MCC's block was written at 02:20 and gone by
    #: 02:36, and nothing on the machine could say whether the user had
    #: pressed Undo or Codex had rewritten the file. A timestamp answers it,
    #: and answers it for the *next* release as well, because the app that
    #: rewrites its own configuration is the failure mode that already cost a
    #: release once.
    undone_at: str = ""
    #: Whether the document itself did not exist before MCC wrote it (7.116.0;
    #: the first harness whose CLI may not have created its file yet). Undo
    #: removes a document MCC created once nothing of MCC's is left in it.
    #: False for every card that required the app's file to exist first, which
    #: is every desktop card, and absent from every record written before it.
    created_file: bool = False
    #: Containers above a key MCC wrote that did not exist beforehand, so Undo
    #: removes exactly those -- ``models``/``providers`` in a document that had
    #: neither -- and leaves an empty container the user already had alone.
    created_parents: tuple[tuple[str, ...], ...] = ()

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "subject": self.subject,
            "document_path": self.document_path,
            "document_sha256": self.document_sha256,
            "overwritten": [value.as_payload() for value in self.overwritten],
            "undone_at": self.undone_at,
        }
        # Only when set, so every record written before 7.116.0 -- every
        # desktop card's -- is written byte for byte as it always was.
        if self.created_file:
            payload["created_file"] = True
        if self.created_parents:
            payload["created_parents"] = [
                list(key_path) for key_path in self.created_parents
            ]
        return payload


def restore_record_path() -> Path:
    """Return the side record's path inside MCC's own config directory."""

    return config_dir_path() / RESTORE_RECORD_FILENAME


def document_sha256(path: Path) -> str:
    """Return the SHA-256 of a document on disk, or "" when it is unreadable."""

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def value_at(document: object, key_path: Sequence[str]) -> tuple[object, bool]:
    """Return ``(value, present)`` for one nested key in a loaded document."""

    node: object = document
    for key in key_path:
        if not isinstance(node, Mapping) or key not in node:
            return None, False
        node = dict(node)[key]
    return node, True


def capture_overwritten(
    document: object,
    key_paths: Sequence[Sequence[str]],
    *,
    prior_lines: Mapping[tuple[str, ...], str] | None = None,
) -> tuple[OverwrittenValue, ...]:
    """Return the pre-edit state of every key a merge is about to replace.

    Called *before* the write, on the document as it was read off disk, which
    is the only moment the prior value still exists. ``prior_lines`` carries
    the user's own line per key for a document edited as lines (7.111.4); a
    key it does not name records none.
    """

    captured: list[OverwrittenValue] = []
    for key_path in key_paths:
        prior_value, prior_present = value_at(document, key_path)
        prior_line = (prior_lines or {}).get(tuple(key_path), "")
        captured.append(
            OverwrittenValue(
                key_path=tuple(key_path),
                prior_value=prior_value,
                prior_present=prior_present,
                prior_line=prior_line if prior_present else "",
            )
        )
    return tuple(captured)


def _load_record(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _harden(path: Path) -> None:
    """Restrict the record to its owner. A prior value can be a credential."""

    if sys.platform == "win32":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        return


def write_entry(entry: RestoreEntry, *, path: Path | None = None) -> Path:
    """Record what Configure overwrote, keeping the *first* answer it got.

    The record answers exactly one question -- "what did the user have before
    MCC ever wrote here?" -- so a second Configure must not replace it. By the
    time the second one runs, the values it reads are MCC's own, and storing
    those would bury the user's real value under MCC's and make Restore a
    no-op that looks like it worked. This is the same doctrine as the one-shot
    ``.mcc-backup``, and for the same reason: the copy worth keeping is the
    pre-MCC one, not yesterday's MCC output.

    What *is* refreshed on every write is ``document_sha256``, which answers a
    different question -- "is this still the document MCC left?" -- and has to
    track the latest write or a re-apply would make every later Restore refuse.

    An entry with nothing overwritten is not written at all, and an existing
    one for that subject is dropped: that is the Command Code case, where MCC
    only ever creates a fresh key, and it keeps the record empty for every
    caller that does not need it.
    """

    record_path = path if path is not None else restore_record_path()
    record = _load_record(record_path)
    subjects = record.get("subjects")
    subjects = dict(subjects) if isinstance(subjects, dict) else {}

    if entry.overwritten:
        existing = read_entry(entry.subject, path=record_path)
        if existing is not None and existing.overwritten:
            entry = RestoreEntry(
                subject=existing.subject,
                document_path=entry.document_path,
                document_sha256=entry.document_sha256,
                overwritten=existing.overwritten,
                created_file=existing.created_file,
                created_parents=existing.created_parents,
            )
        subjects[entry.subject] = entry.as_payload()
    else:
        subjects.pop(entry.subject, None)

    record_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_document_atomically(record_path, _with_subjects(record, subjects))
    _harden(record_path)
    return record_path


def read_entry(subject: str, *, path: Path | None = None) -> RestoreEntry | None:
    """Return what MCC overwrote for one subject, or None when nothing is recorded."""

    record_path = path if path is not None else restore_record_path()
    record = _load_record(record_path)
    subjects = record.get("subjects")
    if not isinstance(subjects, dict):
        return None
    payload = subjects.get(subject)
    if not isinstance(payload, dict):
        return None

    raw_values = payload.get("overwritten")
    values: list[OverwrittenValue] = []
    if isinstance(raw_values, list):
        for item in raw_values:
            if not isinstance(item, dict):
                continue
            key_path = item.get("key_path")
            if not isinstance(key_path, list):
                continue
            # Absent from every record written before 7.111.4.
            prior_line = item.get("prior_line")
            values.append(
                OverwrittenValue(
                    key_path=tuple(str(part) for part in key_path),
                    prior_value=item.get("prior_value"),
                    prior_present=bool(item.get("prior_present")),
                    prior_line=prior_line if isinstance(prior_line, str) else "",
                )
            )

    # ``payload`` is a plain dict here (it came out of the JSON document), so
    # the list is pulled the same way the ``overwritten`` values above are.
    raw_parents = payload.get("created_parents")
    created_parents = tuple(
        key_path
        for key_path in (
            _key_path(item)
            for item in (raw_parents if isinstance(raw_parents, list) else [])
        )
        if key_path is not None
    )

    return RestoreEntry(
        subject=str(payload.get("subject", subject)),
        document_path=str(payload.get("document_path", "")),
        document_sha256=str(payload.get("document_sha256", "")),
        overwritten=tuple(values),
        undone_at=str(payload.get("undone_at", "")),
        created_file=bool(payload.get("created_file")),
        created_parents=created_parents,
    )


def mark_undone(subject: str, *, path: Path | None = None) -> None:
    """Stamp one subject's record as undone by the user, keeping the record.

    The counterpart of :func:`forget_entry`, and the reason both exist.
    ``RESTORE`` consumes its record because the values are back in the
    document and the question it answers is settled. ``KEYS_ONLY`` must keep
    its record -- dropping it is what disarmed the second undo mode and made a
    data loss unrecoverable through the UI in 6.56.0 -- so it stamps instead.

    A stamped record means "MCC's keys are gone because somebody asked". An
    unstamped record with MCC's keys gone means the application removed them,
    which is a state the probe can now report rather than guess at.
    """

    record_path = path if path is not None else restore_record_path()
    entry = read_entry(subject, path=record_path)
    if entry is None:
        return
    record = _load_record(record_path)
    subjects = record.get("subjects")
    subjects = dict(subjects) if isinstance(subjects, dict) else {}
    stamped = RestoreEntry(
        subject=entry.subject,
        document_path=entry.document_path,
        document_sha256=entry.document_sha256,
        overwritten=entry.overwritten,
        undone_at=datetime.now(UTC).isoformat(timespec="seconds"),
        created_file=entry.created_file,
        created_parents=entry.created_parents,
    )
    subjects[subject] = stamped.as_payload()
    write_json_document_atomically(record_path, _with_subjects(record, subjects))
    _harden(record_path)


def read_repairs(subject: str, *, path: Path | None = None) -> tuple[str, ...]:
    """Return the repair notes recorded for one subject, oldest first.

    A repair happens **once**, on the probe that finds an identity an earlier
    MCC wrote and the current app rejects -- and it is the only place a user
    ever learns that their configuration was silently not working and that the
    app has to be relaunched. Until 6.84.0 the note existed for exactly one
    poll: the probe that performed the repair returned it, the dashboard
    refreshed a few seconds later, and the sentence was gone. The only thing
    that survived was a timestamped directory under MCC's configuration
    folder, which nobody is looking at. So the note is written down beside the
    restore record and the card shows it until Undo clears it.
    """

    record_path = path if path is not None else restore_record_path()
    record = _load_record(record_path)
    repairs = record.get("repairs")
    if not isinstance(repairs, dict):
        return ()
    lines = repairs.get(subject)
    if not isinstance(lines, list):
        return ()
    return tuple(str(line) for line in lines)


def record_repairs(
    subject: str, lines: Sequence[str], *, path: Path | None = None
) -> tuple[str, ...]:
    """Add repair notes for one subject, keeping every earlier one, and return all.

    Additive and duplicate-free: a repair that runs again -- it should not,
    because every step of it is conditioned on finding the legacy value, but a
    restored backup can put one back -- must not print the same sentence
    twice.
    """

    record_path = path if path is not None else restore_record_path()
    record = _load_record(record_path)
    repairs = record.get("repairs")
    repairs = dict(repairs) if isinstance(repairs, dict) else {}
    existing = read_repairs(subject, path=record_path)
    merged = list(existing)
    for line in lines:
        if line and line not in merged:
            merged.append(line)
    if tuple(merged) == existing:
        return existing
    repairs[subject] = merged
    record["repairs"] = repairs
    record_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_document_atomically(record_path, record)
    _harden(record_path)
    return tuple(merged)


def forget_repairs(subject: str, *, path: Path | None = None) -> None:
    """Drop one subject's repair notes, which Undo does: it is no longer true."""

    record_path = path if path is not None else restore_record_path()
    record = _load_record(record_path)
    repairs = record.get("repairs")
    if not isinstance(repairs, dict) or subject not in repairs:
        return
    repairs = dict(repairs)
    del repairs[subject]
    record["repairs"] = repairs
    write_json_document_atomically(record_path, record)
    _harden(record_path)


def _with_subjects(
    record: dict[str, object], subjects: dict[str, object]
) -> dict[str, object]:
    """Return the record with its subjects replaced and every other key kept.

    The record file grew a second top-level key in 6.84.0 -- ``repairs`` --
    and every writer that rebuilt the document as ``{"subjects": …}`` would
    have deleted it. Rebuilding from the loaded record instead means a third
    key can be added without auditing the writers again.
    """

    updated = dict(record)
    updated["subjects"] = subjects
    return updated


def forget_entry(subject: str, *, path: Path | None = None) -> None:
    """Drop one subject's record, which Undo does once it has been consumed."""

    record_path = path if path is not None else restore_record_path()
    record = _load_record(record_path)
    subjects = record.get("subjects")
    if not isinstance(subjects, dict) or subject not in subjects:
        return
    subjects = dict(subjects)
    del subjects[subject]
    write_json_document_atomically(record_path, _with_subjects(record, subjects))
    _harden(record_path)


def refresh_document_sha256(
    subject: str, *, before: str, after: str, path: Path | None = None
) -> bool:
    """Move one subject's document hash from ``before`` to ``after``, and only then.

    7.100.0. The media server entry is a second key MCC may own in the same
    document as a card's inference keys (Codex's ``config.toml``, OpenCode's
    ``opencode.json``). Adding or removing it rewrites that document, and
    ``RESTORE`` refuses when the document's hash is not the one Configure
    recorded -- so without this, pressing *Add MCP server* after Configure
    would have disarmed "Restore the original values" for good.

    Conditional on purpose: the hash moves only when it still names the bytes
    MCC itself just replaced. A document the user edited by hand since
    Configure keeps the old hash, so ``RESTORE`` still declines to revert their
    edit. An undone record is left alone too: its stamp is the fact the probe
    reads, and rewriting the entry here would drop it.
    """

    record_path = path if path is not None else restore_record_path()
    entry = read_entry(subject, path=record_path)
    if entry is None or entry.undone_at or entry.document_sha256 != before:
        return False
    if before == after:
        return False
    record = _load_record(record_path)
    subjects = record.get("subjects")
    subjects = dict(subjects) if isinstance(subjects, dict) else {}
    subjects[subject] = RestoreEntry(
        subject=entry.subject,
        document_path=entry.document_path,
        document_sha256=after,
        overwritten=entry.overwritten,
        created_file=entry.created_file,
        created_parents=entry.created_parents,
    ).as_payload()
    write_json_document_atomically(record_path, _with_subjects(record, subjects))
    _harden(record_path)
    return True


# --------------------------------------------------------------------------
# MCC's media MCP server entry in a user's own file (7.100.0)
# --------------------------------------------------------------------------
#
# A key of its own in the same side record: ``mcp_servers``, beside
# ``subjects`` and ``repairs``. Every writer above rebuilds the record from
# what it loaded (``_with_subjects``), so a third top-level key needs none of
# them changed -- which is the reason ``_with_subjects`` exists.
#
# The entry answers "is the ``mcc_media`` server in this file MCC's?". A file
# that carries one MCC never wrote -- a user who pasted MCC's own line into it,
# or a different program under the same name -- has no record here, and the
# card says so and offers no button: Add would overwrite something of theirs
# and Remove would delete it.

#: Top-level key of the side record holding MCC's MCP server entries.
MCP_RECORD_KEY = "mcp_servers"


@dataclass(frozen=True, slots=True)
class McpServerRecord:
    """What MCC added to one app's file for its media server, and how to take it out."""

    #: The desktop app id the entry belongs to.
    subject: str
    #: Absolute path of the file, as a string.
    document_path: str
    #: Every key MCC created, outermost first, with the value it wrote. The
    #: server entry comes first; OpenCode's ``permission`` keys follow it.
    created: tuple[tuple[tuple[str, ...], object], ...]
    #: The text each edit inserted and the text it replaced, in the order the
    #: edits were made. Removal takes them back out in reverse, so a file that
    #: has not been touched since comes back byte for byte.
    splices: tuple[tuple[str, str], ...]
    #: Containers that did not exist before MCC's first key went in. Removal
    #: drops one only when MCC's departure leaves it empty.
    created_parents: tuple[tuple[str, ...], ...] = ()
    #: Whether the file itself did not exist before the entry went in.
    created_file: bool = False
    #: Directories created on the way to the file, deepest first.
    created_dirs: tuple[str, ...] = ()
    #: When the entry was written, ISO-8601 UTC.
    added_at: str = ""

    @property
    def server_value(self) -> object:
        """The server entry MCC wrote (the first created key)."""

        return self.created[0][1] if self.created else None

    def as_payload(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "document_path": self.document_path,
            "created": [
                {"key_path": list(key_path), "value": value}
                for key_path, value in self.created
            ],
            "splices": [
                {"inserted": inserted, "replaced": replaced}
                for inserted, replaced in self.splices
            ],
            "created_parents": [list(key_path) for key_path in self.created_parents],
            "created_file": self.created_file,
            "created_dirs": list(self.created_dirs),
            "added_at": self.added_at,
        }


def _string_keyed(value: object) -> dict[str, object] | None:
    """A mapping as a ``dict[str, object]``; ``isinstance`` alone narrows to Unknown."""

    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _items(payload: Mapping[str, object], key: str) -> list[object]:
    value = payload.get(key)
    return list(value) if isinstance(value, list) else []


def _mcp_records(record: Mapping[str, object]) -> dict[str, object]:
    return _string_keyed(record.get(MCP_RECORD_KEY)) or {}


def write_mcp_record(entry: McpServerRecord, *, path: Path | None = None) -> Path:
    """Record the media server entry MCC just wrote into one app's file."""

    record_path = path if path is not None else restore_record_path()
    record = _load_record(record_path)
    entries = _mcp_records(record)
    stamped = entry
    if not entry.added_at:
        stamped = McpServerRecord(
            subject=entry.subject,
            document_path=entry.document_path,
            created=entry.created,
            splices=entry.splices,
            created_parents=entry.created_parents,
            created_file=entry.created_file,
            created_dirs=entry.created_dirs,
            added_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    entries[entry.subject] = stamped.as_payload()
    updated = dict(record)
    updated[MCP_RECORD_KEY] = entries
    record_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_document_atomically(record_path, updated)
    _harden(record_path)
    return record_path


def _key_path(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    return tuple(str(part) for part in value)


def read_mcp_record(
    subject: str, *, path: Path | None = None
) -> McpServerRecord | None:
    """Return the media server entry MCC recorded for one app, or None."""

    record_path = path if path is not None else restore_record_path()
    payload = _string_keyed(_mcp_records(_load_record(record_path)).get(subject))
    if payload is None:
        return None
    created: list[tuple[tuple[str, ...], object]] = []
    for raw in _items(payload, "created"):
        item = _string_keyed(raw)
        key_path = _key_path(item.get("key_path")) if item is not None else None
        if item is not None and key_path is not None:
            created.append((key_path, item.get("value")))
    if not created:
        return None
    splices: list[tuple[str, str]] = []
    for raw in _items(payload, "splices"):
        item = _string_keyed(raw)
        if item is None:
            continue
        inserted, replaced = item.get("inserted"), item.get("replaced")
        if isinstance(inserted, str) and isinstance(replaced, str):
            splices.append((inserted, replaced))
    parents = tuple(
        key_path
        for key_path in (_key_path(item) for item in _items(payload, "created_parents"))
        if key_path is not None
    )
    return McpServerRecord(
        subject=str(payload.get("subject", subject)),
        document_path=str(payload.get("document_path", "")),
        created=tuple(created),
        splices=tuple(splices),
        created_parents=parents,
        created_file=bool(payload.get("created_file")),
        created_dirs=tuple(str(item) for item in _items(payload, "created_dirs")),
        added_at=str(payload.get("added_at", "")),
    )


def forget_mcp_record(subject: str, *, path: Path | None = None) -> None:
    """Drop one app's media server record: the entry is gone, or no longer MCC's."""

    record_path = path if path is not None else restore_record_path()
    record = _load_record(record_path)
    entries = _mcp_records(record)
    if subject not in entries:
        return
    del entries[subject]
    updated = dict(record)
    updated[MCP_RECORD_KEY] = entries
    write_json_document_atomically(record_path, updated)
    _harden(record_path)
