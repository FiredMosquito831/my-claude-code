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
    no record it can only delete, and it says so rather than erroring.

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

    def as_payload(self) -> dict[str, object]:
        return {
            "key_path": list(self.key_path),
            "prior_value": self.prior_value,
            "prior_present": self.prior_present,
        }


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

    def as_payload(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "document_path": self.document_path,
            "document_sha256": self.document_sha256,
            "overwritten": [value.as_payload() for value in self.overwritten],
            "undone_at": self.undone_at,
        }


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
    document: object, key_paths: Sequence[Sequence[str]]
) -> tuple[OverwrittenValue, ...]:
    """Return the pre-edit state of every key a merge is about to replace.

    Called *before* the write, on the document as it was read off disk, which
    is the only moment the prior value still exists.
    """

    captured: list[OverwrittenValue] = []
    for key_path in key_paths:
        prior_value, prior_present = value_at(document, key_path)
        captured.append(
            OverwrittenValue(
                key_path=tuple(key_path),
                prior_value=prior_value,
                prior_present=prior_present,
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
            values.append(
                OverwrittenValue(
                    key_path=tuple(str(part) for part in key_path),
                    prior_value=item.get("prior_value"),
                    prior_present=bool(item.get("prior_present")),
                )
            )

    return RestoreEntry(
        subject=str(payload.get("subject", subject)),
        document_path=str(payload.get("document_path", "")),
        document_sha256=str(payload.get("document_sha256", "")),
        overwritten=tuple(values),
        undone_at=str(payload.get("undone_at", "")),
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
