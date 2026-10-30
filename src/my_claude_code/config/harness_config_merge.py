"""Merge one MCC-owned key into a CLI's own configuration file.

The last resort, and named so it stays that way. Every other harness MCC
launches is zero-clobber: Claude Code takes two environment variables, Codex
takes ephemeral ``-c`` assignments, Pi registers its provider in process, and
the OpenCode family reads an *extra* config file named by its own
``OPENCODE_CONFIG`` / ``KILO_CONFIG`` variable. Command Code has none of those.
Its bundled ``dist/cli.mjs`` (1.39.0) resolves exactly one document::

    function homeDir15(e){return e.env().HOME??e.env().USERPROFILE}
    function getUserProvidersConfigPath(e){const t=homeDir15(e.runtime);
      return t?`${t}/.commandcode/providers.json`:void 0}

and ``loadProvidersConfig`` reads that path and nothing else -- no flag, no
environment override, no project-local file. So the only way to declare a
provider is to write into the user's own document, and this module is the one
place allowed to do it.

Four rules make that survivable, and all four are tested:

* **One key, one owner.** MCC writes a single subtree named by
  ``owned_key_path`` and touches nothing else. Every other key in the document
  is read, carried through and written back byte-identical.
* **Back up once, before the first write.** The same policy
  ``config/claude_settings.py`` already applies to the Claude settings file:
  a ``.mcc-backup`` sibling is created the first time MCC edits the document
  and never overwritten afterwards, so the backup is always the user's
  pre-MCC file rather than yesterday's MCC output.
* **Idempotent, on MCC's key rather than on the file.** A refresh that
  resolves the same capabilities leaves the document untouched -- no rewrite,
  no mtime churn, no backup. The comparison is deliberately of the owned
  subtree and not of the file's bytes: the atomic writer emits one canonical
  two-space shape, so comparing bytes would reformat the whole document of
  anyone who indents differently, on every single launch.
* **Reversible.** :func:`remove_owned_block` deletes MCC's key and leaves the
  document otherwise untouched, which is what ``mcc-commandcode --disconnect``
  calls.

The home directory is resolved the way the *CLI* resolves it, not the way
Python does. ``Path.home()`` on Windows reads ``USERPROFILE``; Command Code
prefers ``HOME`` and only falls back to ``USERPROFILE``. A developer with
``HOME`` set -- every Git-for-Windows install -- would otherwise have MCC
merge into one file while the CLI read another, and the only symptom would be
a model picker that never shows MCC's models.
"""

import contextlib
import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from my_claude_code.config.atomic_json import (
    write_json_document_atomically_if_changed,
)
from my_claude_code.config.document_codecs import (
    DeleteKey,
    DocumentFormat,
    DocumentFormatError,
    SetScalar,
    SetTable,
    apply_edits,
    parse_document,
    scalar_text,
    yaml_mapping_at,
    yaml_root_is_sequence,
)
from my_claude_code.config.harnesses import (
    COMMANDCODE_BASE_URL_SENTINEL,
    HarnessConfigMerge,
)
from my_claude_code.config.restore_record import (
    RestoreEntry,
    UndoMode,
    capture_overwritten,
    document_sha256,
    forget_entry,
    mark_undone,
    read_entry,
    refresh_document_sha256,
    value_at,
    write_entry,
)

#: Keys the serialiser puts at the top of its own document that belong inside
#: the owned subtree once merged, so removing MCC's key removes every trace of
#: MCC from the user's file. ``_mcc_defaulted`` is the record of which numbers
#: the CLI guessed; leaving it at the document root would litter a file MCC
#: does not own.
CARRIED_DOCUMENT_KEYS: tuple[str, ...] = ("_mcc_defaulted",)

#: The key Command Code (and every merge target before OpenClaw) spells its
#: base URL with. Passed as the default to :func:`with_base_url` and
#: :func:`owned_block_names`, which take the merge's own key when it has one.
DEFAULT_BASE_URL_KEY = "baseURL"


@dataclass(frozen=True, slots=True)
class MergeResult:
    """What one merge did to the user's document."""

    path: Path
    changed: bool
    created: bool
    backup_path: Path | None
    #: Whether Undo removed a document MCC itself had created (7.116.0). False
    #: on every Configure, and for every document that existed beforehand.
    removed_file: bool = False


class MergeDocumentError(ValueError):
    """The document could not be parsed, so nothing was written.

    Raised only on the byte-preserving path. Command Code's canonical path
    deliberately replaces a document that is not JSON (its own test says so);
    a document a user hand-edits -- comments and all -- must never be
    rewritten from a failed parse.
    """


def merge_config_path(merge: HarnessConfigMerge, env: Mapping[str, str]) -> Path:
    """Return the file this CLI reads, resolved the way the CLI resolves it.

    Four rungs, in the CLI's own order, each declared by the merge spec and
    each rung the one OpenClaw's own ``resolveConfigPath`` walks:

    1. a variable naming the *file* (``OPENCLAW_CONFIG_PATH``);
    2. a variable naming the *directory* it sits in (``OPENCLAW_STATE_DIR``);
    3. a variable that outranks the OS home (``OPENCLAW_HOME``);
    4. the home folder itself, in the CLI's own preference order.

    A spec that declares none of 1-3 gets exactly the resolution it always
    had: ``HOME``, then ``USERPROFILE``, then ``Path.home()``.

    A spec that declares ``path_rungs`` -- Hermes, whose home is
    ``%LOCALAPPDATA%\\hermes`` on Windows and ``~/.hermes`` elsewhere -- walks
    those instead, first match wins. The resolution itself lives on
    :meth:`HarnessConfigMerge.resolve`, because the dashboard's detection needs
    the same answer and ``config`` cannot import this module back.
    """

    return merge.resolve(env, sys.platform)


def merge_document_format(merge: HarnessConfigMerge) -> DocumentFormat:
    """Return the merge target's format, as the codec layer spells it.

    ``HarnessConfigMerge.document_format`` is a plain string so the registry
    stays free of this package's enum; this is where it becomes one. An
    unknown value is a programming error in the registry rather than a runtime
    condition, and it fails loudly here instead of editing a document with the
    wrong parser.
    """

    try:
        return DocumentFormat(merge.document_format)
    except ValueError as exc:
        raise ValueError(
            f"{merge.owned_key_label}: unknown document format "
            f"{merge.document_format!r}"
        ) from exc


def owned_block(
    document: Mapping[str, object], owned_key_path: Sequence[str]
) -> dict[str, object]:
    """Return the subtree MCC owns out of a serialiser's own document.

    The serialiser emits the same shape it would write to a standalone file --
    the provider map plus a top-level ``_mcc_defaulted`` record -- because that
    is what ``GET /admin/api/catalogue-models`` returns and what the launcher's
    stderr summary reads. Folding the carried keys into the subtree here is
    what keeps the merged file's MCC footprint to exactly one key.
    """

    node: object = document
    for key in owned_key_path:
        if not isinstance(node, Mapping):
            raise ValueError(f"catalogue document has no {'.'.join(owned_key_path)}")
        node = node.get(key)
    if not isinstance(node, dict):
        raise ValueError(f"catalogue document has no {'.'.join(owned_key_path)}")

    block = dict(node)
    for name in CARRIED_DOCUMENT_KEYS:
        carried = document.get(name)
        if carried:
            block[name] = carried
    return block


def with_base_url(
    block: Mapping[str, object],
    proxy_root_url: str,
    *,
    key: str = DEFAULT_BASE_URL_KEY,
    sentinel: str = COMMANDCODE_BASE_URL_SENTINEL,
) -> dict[str, object]:
    """Return the owned block with the base-URL sentinel replaced.

    The CLI refuses a base URL that is not a parseable absolute URL and
    applies no environment substitution to it, so the real proxy root has to
    be written literally. It is not a secret -- it is a loopback address on
    the user's own machine -- and it is the only MCC value that reaches the
    file. The API key beside it stays a ``$VAR`` reference.

    ``key`` is the CLI's own spelling: Command Code's is ``baseURL`` and
    OpenClaw's is ``baseUrl``. ``sentinel`` is the placeholder that serialiser
    wrote there. Both default to Command Code's, which is the path every merge
    target before OpenClaw used.
    """

    resolved = dict(block)
    if resolved.get(key) == sentinel:
        stripped = proxy_root_url.rstrip("/")
        resolved[key] = stripped if stripped.endswith("/v1") else f"{stripped}/v1"
    return resolved


def read_document(
    path: Path, document_format: str = "json"
) -> dict[str, object] | None:
    """Return the user's document, or None when it is absent or unreadable.

    ``json`` keeps the strict ``json.loads`` this path has always used -- the
    canonical Command Code merge rebuilds the document from what it parsed, so
    a document it silently half-understood would come back rewritten. ``yaml``
    reads through the codec's own nested reader, which reports exactly the
    mapping entries the writer would write.
    """

    if DocumentFormat(document_format) is DocumentFormat.YAML:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        return yaml_mapping_at(text, ())
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def owned_block_present(
    path: Path,
    owned_key_path: Sequence[str],
    *,
    preserve_bytes: bool = False,
    document_format: str = "json",
) -> bool:
    """Return whether MCC's key already exists in the user's document.

    The background refresh keys off this rather than off the file existing: a
    user with their own ``providers.json`` and no interest in MCC must never
    have a provider block appear in it because a provider key rotated.
    """

    document = (
        read_text_document(path, document_format)
        if preserve_bytes
        else read_document(path, document_format)
    )
    if document is None:
        return False
    node: object = document
    for key in owned_key_path:
        if not isinstance(node, Mapping):
            return False
        node = node.get(key)
    return isinstance(node, Mapping)


def owned_block_names(
    path: Path,
    owned_key_path: Sequence[str],
    proxy_root_url: str,
    *,
    key: str = DEFAULT_BASE_URL_KEY,
    sentinel: str = COMMANDCODE_BASE_URL_SENTINEL,
    preserve_bytes: bool = False,
    document_format: str = "json",
) -> bool:
    """Whether MCC's block in the user's document already names *this* server.

    The merge target is the only file outside MCC's own directory that MCC
    writes into, and its path follows ``HOME`` -- so every MCC server on a
    machine resolves the same file. The background fan-out's only gate used to
    be :func:`owned_block_present`, "is MCC's key already there", and the write
    substitutes the writing server's own proxy root into ``baseURL``. Two
    servers therefore ping-ponged the ``baseURL`` on every publish, which is
    how a scratch server rewrote a user's real ``~/.commandcode/providers.json``
    on 2026-09-12.

    The file has one owner, and the file itself says who: the ``baseURL``
    already in MCC's block. A server that does not find its own address there
    is not the owner and leaves the bytes alone. Claiming -- and re-claiming,
    after a ``PORT`` change -- stays with the explicit ``mcc-<harness>``
    launcher, which is a thing the user ran on purpose.

    The comparison is against the value :func:`with_base_url` would write, so a
    block MCC wrote matches itself exactly.
    """

    document = (
        read_text_document(path, document_format)
        if preserve_bytes
        else read_document(path, document_format)
    )
    if document is None:
        return False
    node: object = document
    for owned_key in owned_key_path:
        if not isinstance(node, Mapping):
            return False
        node = node.get(owned_key)
    if not isinstance(node, Mapping):
        return False
    current = node.get(key)
    if not isinstance(current, str):
        return False
    ours = with_base_url({key: sentinel}, proxy_root_url, key=key, sentinel=sentinel)
    return current.rstrip("/") == str(ours[key]).rstrip("/")


def read_text_document(
    path: Path, document_format: str = "json"
) -> dict[str, object] | None:
    """Return a document read as text, or None when it is unusable.

    The byte-preserving path's reader. ``json.loads`` is not the parser
    OpenClaw uses for ``openclaw.json`` -- that document allows comments and
    trailing commas -- so a block MCC had just written into a commented file
    would read as "not there" and the background refresh would never touch it
    again. ``yaml`` reads through the codec's nested reader for the same
    reason: what MCC must recognise is the text it wrote.
    """

    if DocumentFormat(document_format) is DocumentFormat.YAML:
        return read_document(path, document_format)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = parse_document(text, DocumentFormat.JSON)
    except DocumentFormatError:
        return None
    return _as_object_map(payload)


def merge_owned_block(
    *,
    path: Path,
    owned_key_path: Sequence[str],
    block: Mapping[str, object],
    backup_suffix: str,
) -> MergeResult:
    """Write MCC's one key into the user's document, leaving the rest alone."""

    if not owned_key_path:
        raise ValueError("owned_key_path must name at least one key")

    existed = path.exists()
    existing = read_document(path)

    if existing is not None and _owned_block_of(existing, owned_key_path) == dict(
        block
    ):
        # Idempotent, and deliberately measured on *MCC's key* rather than on
        # the file's bytes. A user who indents with four spaces or tabs would
        # otherwise have their whole document reformatted on every launch,
        # because the atomic writer emits one canonical shape. Comparing only
        # what MCC owns means a refresh that resolves the same capabilities
        # leaves the file untouched -- no rewrite, no mtime churn, and no
        # backup taken of MCC's own previous output.
        return MergeResult(path=path, changed=False, created=False, backup_path=None)

    document: dict[str, object] = dict(existing or {})
    _set_in(document, owned_key_path, dict(block))
    backup_path = _backup_if_needed(path, backup_suffix) if existed else None
    write_json_document_atomically_if_changed(path, document)
    return MergeResult(
        path=path, changed=True, created=not existed, backup_path=backup_path
    )


def remove_owned_block(
    *, path: Path, owned_key_path: Sequence[str], backup_suffix: str
) -> MergeResult:
    """Remove MCC's key from the user's document and leave every other key."""

    document = read_document(path)
    if document is None:
        return MergeResult(path=path, changed=False, created=False, backup_path=None)
    if not _delete_in(document, owned_key_path):
        return MergeResult(path=path, changed=False, created=False, backup_path=None)

    backup_path = _backup_if_needed(path, backup_suffix)
    changed = write_json_document_atomically_if_changed(path, document)
    return MergeResult(
        path=path, changed=changed, created=False, backup_path=backup_path
    )


def _owned_block_of(
    document: Mapping[str, object], owned_key_path: Sequence[str]
) -> dict[str, object] | None:
    """Return MCC's key out of a document already on disk, or None."""

    node: object = document
    for key in owned_key_path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return _as_object_map(node)


def _set_in(
    document: dict[str, object], key_path: Sequence[str], value: dict[str, object]
) -> None:
    """Set one nested key, creating the levels above it that are missing.

    A level that exists but is not an object is replaced rather than merged
    into: ``provider: 4`` is not a map MCC can add a key to, and refusing to
    launch over it would be worse than replacing a value the CLI itself would
    reject with "provider must be an object map".
    """

    node = document
    for key in key_path[:-1]:
        nested = _as_object_map(node.get(key))
        if nested is None:
            nested = {}
        node[key] = nested
        node = nested
    node[key_path[-1]] = value


def _delete_in(document: dict[str, object], key_path: Sequence[str]) -> bool:
    head, *rest = key_path
    if not rest:
        if head not in document:
            return False
        del document[head]
        return True
    child = _as_object_map(document.get(head))
    if child is None or not _delete_in(child, rest):
        return False
    document[head] = child
    return True


def _as_object_map(value: object) -> dict[str, object] | None:
    """Return a mapping as a string-keyed dict, or None when it is not one."""

    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _backup_if_needed(path: Path, backup_suffix: str) -> Path | None:
    """Copy the document to its backup path, once and only once."""

    backup_path = path.with_name(path.name + backup_suffix)
    if backup_path.exists():
        return backup_path
    try:
        shutil.copyfile(path, backup_path)
    except OSError:
        return None
    return backup_path


# --------------------------------------------------------------------------
# The byte-preserving merge (7.116.0, OpenClaw)
# --------------------------------------------------------------------------
#
# Command Code's path above rebuilds the document from its parsed form and
# writes it canonically. That is defensible for a file of provider maps and
# wrong for a file a user hand-edits: OpenClaw's ``openclaw.json`` is JSON5
# with comments, and anyone who has written one has arranged it their own way.
#
# So this path makes the same promise ``config/document_codecs`` already makes
# for every desktop document: the edit is applied to the *text*, and the lines
# MCC does not own come back byte for byte. Everything else is the engine the
# desktop cards already use --
#
# * the one-time ``.mcc-backup``, taken before the first edit and never
#   overwritten, so it is the user's pre-MCC file rather than yesterday's MCC
#   output;
# * a ``restore_record`` entry for every value MCC *replaced* rather than
#   created, written only when something was replaced (the Command Code rule);
# * the same two Undo modes: ``KEYS_ONLY`` never refuses, ``RESTORE`` requires
#   the record and refuses when the document changed since.
#
# The caller supplies the serialiser's document, exactly as ``desktop_apply``
# does: what MCC writes comes from ``application``, which ``config`` may not
# import.


def configure_owned_block(
    *,
    merge: HarnessConfigMerge,
    path: Path,
    document: Mapping[str, object],
    proxy_root_url: str,
    subject: str,
    claim: bool = True,
    record_path: Path | None = None,
) -> MergeResult:
    """Write MCC's block -- and any value it replaces -- into the user's file.

    ``claim`` separates the two callers. ``True`` is the explicit
    ``mcc-<harness>`` run: it writes the block, writes the replaced values and
    records what they were. ``False`` is the background refresh: it brings
    MCC's own block up to date and touches nothing else, because
    ``agents.defaults.model.primary`` is the user's choice of model and a
    provider refresh must not move it back under them. A refresh also keeps the
    stored document hash current (``refresh_document_sha256``), so a later
    ``RESTORE`` does not refuse over bytes MCC itself wrote.

    Raises :class:`MergeDocumentError` rather than touching a document that
    does not parse: the user's file is not MCC's to replace.
    """

    before_text, existed, crlf = _read_text_or_empty(path)
    document_format = merge_document_format(merge)
    parsed = _parsed_or_raise(before_text, path, document_format)
    block = with_base_url(
        owned_block(document, merge.owned_key_path),
        proxy_root_url,
        key=merge.base_url_key,
        sentinel=merge.base_url_sentinel,
    )

    edits: list[SetTable | SetScalar] = [SetTable(merge.owned_key_path, block)]
    recorded: list[tuple[str, ...]] = []
    if claim:
        for key_path in merge.overwritten_key_paths:
            value, present = value_at(document, key_path)
            if not present:
                continue
            edits.append(SetScalar(tuple(key_path), value))
            # Where OpenClaw's ``agents.defaults.model`` is a plain string (or
            # anything else that is not an object), writing ``primary`` means
            # replacing that whole value. Record the value that goes, not the
            # key MCC could not have written into.
            recorded.append(_interrupted_ancestor(parsed, key_path) or tuple(key_path))

    created_parents = _created_parents(parsed, [edit.key_path for edit in edits])
    overwritten = (
        capture_overwritten(
            parsed,
            recorded,
            prior_lines={
                key_path: scalar_text(before_text, document_format, key_path)
                for key_path in recorded
            },
        )
        if claim
        else ()
    )

    after_text = apply_edits(
        _editable_text(before_text, document_format), document_format, edits
    )
    if after_text == before_text:
        return MergeResult(path=path, changed=False, created=False, backup_path=None)

    before_hash = document_sha256(path) if existed else ""
    backup_path = _backup_if_needed(path, merge.backup_suffix) if existed else None
    _write_document_text(path, after_text, crlf)
    if claim:
        write_entry(
            RestoreEntry(
                subject=subject,
                document_path=str(path),
                document_sha256=document_sha256(path),
                overwritten=overwritten,
                created_file=not existed,
                created_parents=created_parents,
            ),
            path=record_path,
            keep_when_empty=merge.records_document_hash,
        )
    elif existed:
        refresh_document_sha256(
            subject,
            before=before_hash,
            after=document_sha256(path),
            path=record_path,
        )
    return MergeResult(
        path=path, changed=True, created=not existed, backup_path=backup_path
    )


def unconfigure_owned_block(
    *,
    merge: HarnessConfigMerge,
    path: Path,
    subject: str,
    mode: UndoMode = UndoMode.KEYS_ONLY,
    record_path: Path | None = None,
    proxy_root_url: str | None = None,
) -> MergeResult:
    """Take MCC's block back out, putting back whatever it replaced.

    The two modes are the desktop engine's, unchanged: ``KEYS_ONLY`` removes
    exactly what MCC created, restores what the record says it replaced, and
    never refuses; ``RESTORE`` does the same but requires the record and
    refuses when the document has moved since MCC wrote it. A file MCC created
    is removed once nothing is left in it.
    """

    before_text, existed, crlf = _read_text_or_empty(path)
    if not existed:
        return MergeResult(path=path, changed=False, created=False, backup_path=None)
    document_format = merge_document_format(merge)
    parsed = _parsed_or_raise(before_text, path, document_format)

    entry = read_entry(subject, path=record_path)
    live_record = entry is not None and not entry.undone_at
    owned_present = _owned_key_present(before_text, parsed, merge, document_format)
    if not owned_present and not live_record:
        # Nothing of MCC's in this file, and no record of a Configure still in
        # place: a replaced key here is the user's own. The desktop engine's
        # ``_holds_nothing_of_mccs`` rule, for the same data-loss reason.
        return MergeResult(path=path, changed=False, created=False, backup_path=None)
    if not live_record and not _names_this_server(merge, path, proxy_root_url):
        # A block under MCC's key that MCC has no record of writing, and that
        # does not name this server's proxy root: somebody else's, and the one
        # rule this engine has never broken is that it does not delete what it
        # cannot prove it wrote. ``mcc-<harness>`` can claim it; no undo can.
        return MergeResult(path=path, changed=False, created=False, backup_path=None)

    if mode is UndoMode.RESTORE and entry is None:
        raise MergeDocumentError(
            f"no record of what MCC replaced in {path}. Removing MCC's block "
            "is still possible with KEYS_ONLY."
        )
    if mode is UndoMode.RESTORE and entry is not None:
        current = document_sha256(path)
        if entry.document_sha256 and current and entry.document_sha256 != current:
            raise MergeDocumentError(
                f"{path} has changed since MCC configured it, so restoring the "
                "pre-MCC values would overwrite an edit made since. Remove "
                f"MCC's block only, or restore from {path.name}"
                f"{merge.backup_suffix} by hand."
            )

    restore: dict[tuple[str, ...], tuple[object, str]] = {}
    for value in entry.overwritten if entry is not None else ():
        if value.prior_present:
            restore[tuple(value.key_path)] = (value.prior_value, value.prior_line)

    # What Undo puts back comes from the record, not from the merge spec: the
    # record is what MCC actually wrote, and it is the only thing that knows a
    # plain-string ``agents.defaults.model`` was replaced whole.
    replaced_paths = (
        tuple(tuple(value.key_path) for value in entry.overwritten)
        if entry is not None and entry.overwritten
        else merge.overwritten_key_paths
    )
    edits: list[DeleteKey | SetScalar] = [
        # With a record, the containers MCC created are named and removed
        # below, one at a time -- so the leaf is removed on its own and a
        # container the user already had is kept, even an empty one. Without a
        # record there is nothing to tell them apart, and an empty container
        # left behind carries no information, so the ordinary prune applies.
        DeleteKey(
            merge.owned_key_path,
            prune_empty_ancestors=entry is None,
        )
    ]
    for key_path in replaced_paths:
        found = restore.get(tuple(key_path))
        if found is None:
            edits.append(DeleteKey(tuple(key_path), prune_empty_ancestors=False))
        else:
            edits.append(SetScalar(tuple(key_path), found[0], prior_line=found[1]))

    after_text = apply_edits(before_text, document_format, edits)
    after_text = _remove_created_parents(
        after_text,
        merge,
        entry.created_parents if entry is not None else (),
        document_format,
    )
    emptied = not _nonempty_document(after_text, document_format)

    backup_path = _backup_if_needed(path, merge.backup_suffix)
    if emptied and entry is not None and entry.created_file:
        with contextlib.suppress(OSError):
            path.unlink()
        removed_file = True
        changed = True
    else:
        removed_file = False
        changed = after_text != before_text
        if changed:
            _write_document_text(path, after_text, crlf)

    if mode is UndoMode.RESTORE:
        forget_entry(subject, path=record_path)
    else:
        mark_undone(subject, path=record_path)
    return MergeResult(
        path=path,
        changed=changed,
        created=False,
        backup_path=backup_path,
        removed_file=removed_file,
    )


def _read_text_or_empty(path: Path) -> tuple[str, bool, bool]:
    """Return ``(text with LF line ends, existed, written_with_crlf)``.

    Read as bytes and decoded, then normalised, exactly as
    ``config/desktop_apply._read_mcp_text`` does for the media server entry: a
    document a Notepad user saved with CRLF has to come back with CRLF, and
    ``read_text``'s universal-newline mode would quietly hand back LF.
    """

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "", False, False
    except OSError:
        return "", False, False
    text = raw.decode("utf-8", errors="surrogateescape")
    crlf = "\r\n" in text and "\n" not in text.replace("\r\n", "")
    return text.replace("\r\n", "\n").replace("\r", "\n"), True, crlf


def _owned_key_present(
    text: str,
    parsed: Mapping[str, object],
    merge: HarnessConfigMerge,
    document_format: DocumentFormat,
) -> bool:
    """Whether MCC's own key is in the document.

    JSON asks the parsed document -- the one the byte-preserving path already
    read as JSON5. YAML must ask the codec's text reader instead: the shallow
    parse the rest of this module gets from the codec reports a nested block as
    present with *no value*, which is enough to know ``providers`` exists and
    not enough to know whether ``providers.mcc`` does. Undo depends on the
    second answer -- getting it wrong made ``--disconnect`` a no-op on every
    Hermes file, which the 7.118.0 tests now pin.
    """

    if document_format is DocumentFormat.YAML:
        return yaml_mapping_at(text, merge.owned_key_path) is not None
    _value, present = value_at(parsed, merge.owned_key_path)
    return present


def _names_this_server(
    merge: HarnessConfigMerge, path: Path, proxy_root_url: str | None
) -> bool:
    """Return whether MCC's block in ``path`` names this server's proxy root."""

    if proxy_root_url is None:
        return False
    return owned_block_names(
        path,
        merge.owned_key_path,
        proxy_root_url,
        key=merge.base_url_key,
        sentinel=merge.base_url_sentinel,
        preserve_bytes=merge.preserve_bytes,
        document_format=merge.document_format,
    )


def _parsed_or_raise(
    text: str, path: Path, document_format: DocumentFormat = DocumentFormat.JSON
) -> dict[str, object]:
    """Return the document, or refuse to touch one that does not parse."""

    if not text.strip():
        return {}
    if document_format is DocumentFormat.YAML and yaml_root_is_sequence(text):
        # A list is not a document MCC can own a key inside, and the codec's
        # insert would append a mapping key after the last item rather than
        # refuse. YAML has no "does not parse" state here -- the reader is
        # deliberately narrow rather than strict -- so this is its one
        # structural refusal.
        raise MergeDocumentError(f"{path} is a YAML sequence, not a mapping")
    try:
        payload = parse_document(text, document_format)
    except DocumentFormatError as exc:
        raise MergeDocumentError(f"{path} does not parse: {exc}") from exc
    document = _as_object_map(payload)
    if document is None:
        raise MergeDocumentError(f"{path} is not a {document_format.value} object")
    return document


def _editable_text(text: str, document_format: DocumentFormat) -> str:
    """Return a root object to edit: the file's own text, or an empty one.

    JSON's empty object is ``{}`` and YAML's is the empty document: its
    ``SetTable`` writes the whole chain from the first missing segment, so a
    literal ``{}`` would be a line MCC never means to leave in a config file.
    """

    if text.strip():
        return text
    return "{}" if document_format is DocumentFormat.JSON else ""


def _created_parents(
    parsed: Mapping[str, object], key_paths: Sequence[Sequence[str]]
) -> tuple[tuple[str, ...], ...]:
    """Return the containers above these keys that did not exist beforehand."""

    created: list[tuple[str, ...]] = []
    for key_path in key_paths:
        for depth in range(1, len(key_path)):
            prefix = tuple(key_path[:depth])
            if prefix in created:
                continue
            _, present = value_at(parsed, prefix)
            if not present:
                created.append(prefix)
    return tuple(created)


def _interrupted_ancestor(
    parsed: Mapping[str, object], key_path: Sequence[str]
) -> tuple[str, ...] | None:
    """Return the first ancestor of ``key_path`` that is not an object, if any.

    ``_json_set`` walks the path until the chain of objects stops, then writes
    the rest as one nested value there. This names the level it will stop at,
    so Configure records the value that actually goes -- the plain string
    ``agents.defaults.model`` -- rather than a key underneath it that never
    existed.
    """

    for depth in range(1, len(key_path)):
        prefix = tuple(key_path[:depth])
        value, present = value_at(parsed, prefix)
        if present and not isinstance(value, Mapping):
            return prefix
    return None


def _remove_created_parents(
    text: str,
    merge: HarnessConfigMerge,
    parents: Sequence[Sequence[str]],
    document_format: DocumentFormat = DocumentFormat.JSON,
) -> str:
    """Drop containers MCC created, once its departure has left them empty.

    ``DeleteKey`` prunes every empty ancestor, which is right for MCC's block
    and wrong for a container that was already in the file. The record says
    which ones MCC created, so exactly those go -- outermost first, and only
    while they are empty, so a key the user added afterwards is never lost.

    Applied in passes, because emptying ``models.providers`` is what makes
    ``models`` empty a pass later. Bounded by the number of recorded parents:
    nothing new can become empty that was not already listed.
    """

    if not parents:
        return text
    wanted = {tuple(parent) for parent in parents}
    for _pass in range(len(wanted) + 1):
        empties: list[DeleteKey] = []
        for parent in sorted(wanted, key=len):
            if not _container_is_empty(text, document_format, parent):
                continue
            # A one-part path has no ancestors to prune, and the JSON delete
            # only reaches a key at depth one when pruning is on. YAML's has no
            # such asymmetry, but the flag means the same thing there: this
            # removal is the one that may take an empty container with it.
            empties.append(
                DeleteKey(tuple(parent), prune_empty_ancestors=len(parent) == 1)
            )
        if not empties:
            return text
        try:
            text = apply_edits(text, document_format, empties)
        except DocumentFormatError:
            return text
    return text


def _container_is_empty(
    text: str, document_format: DocumentFormat, key_path: Sequence[str]
) -> bool:
    """Whether ``key_path`` is present and holds nothing at all.

    JSON asks the parsed document. YAML asks the codec's text reader, which is
    the only thing that knows an entry from a comment: a ``providers:`` holding
    only the user's comment is not empty and is never removed.
    """

    if document_format is DocumentFormat.YAML:
        value = yaml_mapping_at(text, key_path)
        return value is not None and not value
    try:
        parsed = parse_document(text, document_format)
    except DocumentFormatError:
        return False
    if not isinstance(parsed, Mapping):
        return False
    value, present = value_at(parsed, key_path)
    return present and isinstance(value, Mapping) and not value


def _nonempty_document(text: str, document_format: DocumentFormat) -> bool:
    """Return whether a document still holds anything at all."""

    try:
        parsed = parse_document(text, document_format)
    except DocumentFormatError:
        return True
    return bool(parsed) if isinstance(parsed, Mapping) else True


def _write_document_text(path: Path, text: str, crlf: bool) -> None:
    """Write an edited document in place, staging a sibling and replacing it.

    ``newline=""`` so the text is written exactly as edited, and CRLF restored
    when the file was written that way: the same rule
    ``config/desktop_apply._write_mcp_text`` follows, and the reason a
    Notepad-edited document keeps its line endings through a cycle.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".mcc-tmp")
    payload = text.replace("\n", "\r\n") if crlf else text
    temporary.write_text(payload, encoding="utf-8", newline="")
    os.replace(temporary, path)
